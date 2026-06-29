"""Grounding — assemble a company's real context for a worker to draft from.

Pulls the Company Profile (identity, brand assets, channels, signature, reply-from, CRM…) + the live
website's brand guidelines + the on-box site source path, so every draft is grounded in the actual
company (the same source Claude Code reads here). Read-only.
"""
from __future__ import annotations

import os

from . import profile

SITES_ROOT = os.environ.get("CORTEX_SITES_ROOT", "/opt/sites")
# company slug -> on-box repo dir name (slug differs from the dir for sky-vision)
SITE_DIRS = {"tabscanner": "tabscanner", "sensa": "sensa", "skyvision": "sky-vision",
             "filmspoke": "filmspoke", "flixton": "flixton-manor", "snaprewards": "snap-rewards"}

_LABELS = None


def _labels() -> dict:
    global _LABELS
    if _LABELS is None:
        _LABELS = {q["field"]: q["text"] for q in profile.questions()}
    return _LABELS


def site_path(slug: str) -> str | None:
    p = os.path.join(SITES_ROOT, SITE_DIRS.get(slug, slug or ""))
    return p if os.path.isdir(p) else None


def brand_guidelines(slug: str) -> str:
    p = site_path(slug)
    if not p:
        return ""
    for cand in ("strategy/BRAND-GUIDELINES.md", "BRAND-GUIDELINES.md", "brand/BRAND-GUIDELINES.md"):
        f = os.path.join(p, cand)
        if os.path.isfile(f):
            try:
                return open(f, encoding="utf-8").read()[:6000]
            except OSError:
                pass
    return ""


def _profile_block(company_id: int) -> str:
    try:
        data = profile.get(company_id)
    except Exception:  # noqa: BLE001
        data = {}
    if not data:
        return ""
    labels = _labels()
    # signature_html (rich markup) and the cached brand kit are for rendering, not worker reasoning —
    # keep them out of the prompt (they'd add noise + tokens). The plain `signature` still conveys the sign-off.
    # meeting_link + booking config are ALSO hidden: the model must never paste the calendar/booking URL into a
    # reply (it offers real open times instead, via engine._booking_slots_brief); booking is internal plumbing.
    _skip = {"signature_html", "brand", "meeting_link", "booking"}
    lines = [f"- {labels.get(k, k)}: {v}" for k, v in data.items() if v and k not in _skip]
    return ("COMPANY PROFILE (the standard facts — identity, brand assets, channels, signature, "
            "reply-from, team, CRM):\n" + "\n".join(lines)) if lines else ""


def for_company(company: dict) -> str:
    """A grounding block to append to a worker's system prompt for this company."""
    slug = company.get("slug") or ""
    parts = []
    blk = _profile_block(company.get("id"))
    if blk:
        parts.append(blk)
    bg = brand_guidelines(slug)
    if bg:
        parts.append("BRAND GUIDELINES (follow these for any visual/page/asset work):\n" + bg)
    sp = site_path(slug)
    if sp:
        parts.append(f"The live website source is on this server at {sp} — the real theme, page templates "
                     "(page-*.php), design system (theme/assets/css/app.css), and assets. When building a "
                     "page, match that exact design system and reuse its patterns; don't invent new styling.")
    return "\n\n".join(parts)
