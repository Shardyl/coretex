"""Gemini Imagen hero/figure images for blogs + newsletters. Returns JPEG bytes, or None on any failure —
images are OPTIONAL (content must read fully with images off), so callers degrade gracefully.

Cost: uses Imagen 4 STANDARD — Rashad values the image QUALITY (composition / prompt-adherence) and images
are a small slice of spend, so we do NOT drop to Fast. 1K output is ample for web. Every successful image is
logged to `usage_log` (model, purpose, company, cost_usd) so Gemini spend shows in the Cortex cost chip
alongside the LLM spend. (Fast = imagen-4.0-fast-generate-001 @ ~$0.02 is the opt-in cheaper tier if ever
wanted; Ultra @ ~$0.06 the sharper one.)
"""
from __future__ import annotations

import base64
import re

import httpx

from . import config

MODEL = "imagen-4.0-generate-001"   # Imagen 4 Standard: the quality Rashad approved (Fast trades quality for cost)
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:predict"
IMAGE_COST_USD = 0.04   # Imagen 4 Standard per-image (Fast ~0.02, Ultra ~0.06)


def _log_image(purpose: str, company: str | None) -> None:
    """Record one generated image in the shared cost log, so Gemini image spend lands in the cost chip."""
    try:
        from . import db
        db.execute("insert into usage_log (model, purpose, company, input_tokens, output_tokens, "
                   "cache_write, cache_read, cost_usd) values (%s,%s,%s,0,0,0,0,%s)",
                   (MODEL, purpose, company, IMAGE_COST_USD))
    except Exception:  # noqa: BLE001 — logging must never break a generation
        pass


_TEXT_HINT = re.compile(r"receipt|invoice|\btext\b|label|number|price|total|menu|sign|screen|document|field|form|barcode|stamp", re.I)


def _text_accurate_prompt(prompt: str, company: str | None) -> str:
    """Backstop for text-heavy images (e.g. Tabscanner receipts): image models HALLUCINATE gibberish text unless
    given the exact words. If the prompt implies any text/numbers/receipt fields, Claude rewrites it so EVERY such
    piece of text is specified VERBATIM (realistic, legible sample content) -> accurate render, not gibberish.
    No text implied -> unchanged. Never raises."""
    if not _TEXT_HINT.search(prompt or ""):
        return prompt
    try:
        from . import provider
        directive = _company_image_directives(company)
        sysmsg = (
            "You prepare prompts for an AI IMAGE generator that renders TEXT badly unless given the exact words. "
            "If this image will contain ANY text, receipt fields, numbers, labels, prices or UI copy, REWRITE the "
            "prompt so EVERY such piece of text is written VERBATIM in double quotes with realistic, plausible, "
            "legible sample content (for a receipt: a real-sounding store name, 2-4 line items with prices, a "
            "subtotal/total and a date). Keep the visual style and composition direction intact. If the image "
            "genuinely has no text, return the prompt unchanged.")
        if directive:
            sysmsg += (
                " COMPANY RULE you MUST obey while rewriting: " + directive +
                " Therefore any receipt or printed item MUST stay a small, realistic, true-to-life-SIZED everyday "
                "receipt: do NOT enumerate a long fully-legible itemised list, and do NOT enlarge, elongate or "
                "make it prominent to fit text. Describe its print as ordinary small receipt text at natural scale "
                "(a store name and a couple of faint lines is plenty); realism and correct small size take "
                "PRIORITY over text legibility, and keep any 'true-to-life proportions' direction in the prompt.")
        sysmsg += " Return ONLY the final image prompt, nothing else."
        out = provider.think(sysmsg, prompt, fast=True, max_tokens=700,
                             purpose="image_text_prepass", company=company)
        return (out or "").strip() or prompt
    except Exception:  # noqa: BLE001 — the prepass is a quality boost; never block image-gen
        return prompt


def _company_image_directives(company: str | None) -> str:
    """Per-company image guidance kept as EDITABLE blog-skill rules: any content-blog-posts rule that begins
    'IMAGE PROMPT DIRECTIVE:' is appended to every generated image prompt for that company (so e.g. Snap Rewards
    can force normal, realistic receipts). Applied at generation time, so it also takes effect on a re-run."""
    if not company:
        return ""
    try:
        from . import store
        co = store.get_company_by_slug(company)
        if not co:
            return ""
        sk = store.get_skill_by_key(co["id"], "content-blog-posts")
        if not sk:
            return ""
        uni, loc = store.effective_rules(sk)
        ds = [r.split(":", 1)[1].strip() for r in (list(uni) + list(loc))
              if r.strip().upper().startswith("IMAGE PROMPT DIRECTIVE:")]
        return (" " + " ".join(ds)) if ds else ""
    except Exception:  # noqa: BLE001 — directives are optional; never block image-gen
        return ""


def hero(prompt: str, aspect: str = "16:9", purpose: str = "image", company: str | None = None) -> bytes | None:
    key = config.get("GEMINI_API_KEY")
    if not key or not prompt:
        return None
    prompt = _text_accurate_prompt(prompt, company)   # specify any receipt/text content verbatim before Gemini
    prompt = prompt + _company_image_directives(company)   # append company image rules (e.g. realistic receipts)
    try:
        r = httpx.post(ENDPOINT, params={"key": key}, timeout=120, json={
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect}})
        r.raise_for_status()
        preds = r.json().get("predictions", [])
        b64 = preds[0].get("bytesBase64Encoded") if preds else None
        if not b64:
            return None
        _log_image(purpose, company)   # only a SUCCESSFUL generation costs money / gets logged
        return base64.b64decode(b64)
    except Exception:  # noqa: BLE001 — images are optional; never block a send on image-gen
        return None
