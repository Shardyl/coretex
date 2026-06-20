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


def hero(prompt: str, aspect: str = "16:9", purpose: str = "image", company: str | None = None) -> bytes | None:
    key = config.get("GEMINI_API_KEY")
    if not key or not prompt:
        return None
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
