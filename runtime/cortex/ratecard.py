"""The company rate card — the single per-unit pricing reference every quotation is built from.

Rates are OWNER-APPROVED numbers only: they enter the card from quotes Rashad has actually approved
or figures he states, never from a model. Stored as the `rate_card:<slug>` setting (data, editable
without a deploy). Drafting for quotation-adjacent lanes gets `render()` injected, with the standing
instruction that a price not on the card is OWNER TO CONFIRM — never invented.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db


def _key(slug: str) -> str:
    return f"rate_card:{(slug or '').strip().lower()}"


def get(slug: str) -> dict:
    return db.setting_get(_key(slug)) or {}


def save(slug: str, card: dict) -> dict:
    card = dict(card or {})
    card["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.setting_set(_key(slug), card)
    return card


def set_item(slug: str, group: str, desc: str, rate: float | None, unit: str = "each",
             note: str = "", source: str = "", budget: float | None = None) -> dict:
    """Add or update one line (matched by description, case-insensitive, within its group).
    rate = the Normal-tier price; budget = the Budget-tier price (deal-closer, owner call only).
    rate=None means the item exists but its price is not yet owner-approved."""
    card = get(slug) or {"currency": "AED", "groups": []}
    grp = next((g for g in card.get("groups", []) if g.get("heading", "").lower() == group.lower()), None)
    if not grp:
        grp = {"heading": group, "items": []}
        card.setdefault("groups", []).append(grp)
    item = {"desc": desc, "unit": unit, "rate": rate, "budget": budget, "note": note, "source": source}
    for i, it in enumerate(grp["items"]):
        if it.get("desc", "").strip().lower() == desc.strip().lower():
            grp["items"][i] = item
            return save(slug, card)
    grp["items"].append(item)
    return save(slug, card)


def render(slug: str) -> str:
    """The card as a drafting-context block. Empty string when no card exists."""
    card = get(slug)
    if not card or not card.get("groups"):
        return ""
    cur = card.get("currency", "AED")
    lines = [f"COMPANY RATE CARD ({cur}, version {card.get('version', '?')}, updated {card.get('updated', '?')}) "
             "— quotation pricing comes ONLY from these owner-approved rates. Normal tier is the default; "
             "the Budget tier is a deal-closer applied ONLY on the owner's call, never silently. An item "
             "marked OWNER TO CONFIRM, or any item not on this card, is NEVER given an invented price: "
             "name it and mark it OWNER TO CONFIRM."]
    for g in card["groups"]:
        lines.append(f"{g.get('heading', '')}:")
        for it in g.get("items", []):
            rate = f"{cur} {it['rate']:,.0f}" if isinstance(it.get("rate"), (int, float)) else "OWNER TO CONFIRM"
            if isinstance(it.get("budget"), (int, float)):
                rate += f" (Budget tier {cur} {it['budget']:,.0f})"
            note = f" ({it['note']})" if it.get("note") else ""
            lines.append(f"  - {it.get('desc', '')} — {rate} per {it.get('unit', 'each')}{note}")
    return "\n".join(lines)


def summary(slug: str) -> str:
    """The card as a readable list for the Talk assistant, including the Budget tier and every
    OWNER TO CONFIRM gap — the gaps matter as much as the rates, because they are what stops a
    quotation being finished."""
    card = get(slug)
    if not card or not card.get("groups"):
        return f"No rate card exists for {slug} yet."
    cur = card.get("currency", "AED")
    lines = [f"Rate card for {slug} ({cur}, version {card.get('version', '?')}, "
             f"updated {card.get('updated', '?')}):"]
    gaps = []
    for g in card["groups"]:
        lines.append(g.get("heading", ""))
        for it in g.get("items", []):
            if isinstance(it.get("rate"), (int, float)):
                bit = f"  - {it.get('desc')}: {cur} {it['rate']:,.0f} per {it.get('unit', 'each')}"
                if isinstance(it.get("budget"), (int, float)):
                    bit += f" (Budget tier {cur} {it['budget']:,.0f})"
            elif isinstance(it.get("budget"), (int, float)):
                bit = (f"  - {it.get('desc')}: {cur} {it['budget']:,.0f} per "
                       f"{it.get('unit', 'each')} (Budget tier only)")
            else:
                bit = f"  - {it.get('desc')}: OWNER TO CONFIRM per {it.get('unit', 'each')}"
                gaps.append(str(it.get("desc")))
            if it.get("note"):
                bit += f" [{it['note']}]"
            lines.append(bit)
    if gaps:
        lines.append("")
        lines.append("Still needing a rate from Rashad: " + "; ".join(gaps[:10]))
    return "\n".join(lines)
