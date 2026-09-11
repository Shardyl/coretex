"""The company rate card — the single per-unit pricing reference every quotation is built from.

Rates are OWNER-APPROVED numbers only: they enter the card from quotes Rashad has actually approved
or figures he states, never from a model. Stored as the `rate_card:<slug>` setting (data, editable
without a deploy). Drafting for quotation-adjacent lanes gets `render()` injected, with the standing
instruction that a price not on the card is OWNER TO CONFIRM — never invented.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import db


def _key(slug: str) -> str:
    return f"rate_card:{(slug or '').strip().lower()}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def key_of(it: dict) -> str:
    """The item's stable reference: its `key`, else its description as a slug."""
    return it.get("key") or _slug(it.get("desc"))


def _group_tag(g: dict) -> str:
    if g.get("reference_only"):
        return " (REFERENCE ONLY: never used to build a quotation)"
    if g.get("at_cost"):
        return " (pass-through at cost, never marked up, never scaled)"
    return ""


def index(slug: str) -> dict:
    """Every item on the card, findable by its key or its description (case-insensitive)."""
    out = {}
    for g in get(slug).get("groups") or []:
        for it in g.get("items") or []:
            rec = {**it, "key": key_of(it), "group": g.get("heading", ""),
                   "at_cost": bool(g.get("at_cost") or it.get("at_cost")),
                   "reference_only": bool(g.get("reference_only"))}
            out[rec["key"]] = rec
            out.setdefault((it.get("desc") or "").strip().lower(), rec)
    return out


def rates(slug: str) -> set:
    """Every approved figure on the card that may price a line (reference-only packages excluded)."""
    return {round(float(it[k]), 2) for g in get(slug).get("groups") or [] if not g.get("reference_only")
            for it in g.get("items") or [] for k in ("rate", "budget") if isinstance(it.get(k), (int, float))}


def _round(v: float) -> float:
    step = 50 if v < 1000 else 100 if v < 10000 else 500
    return float(round(v / step) * step)


def price_lines(slug: str, sections: list, *, allowed: set | None = None, target: float | None = None,
                flex_pct: float | None = None, tier: str = "normal") -> dict:
    """Price every quotation line IN CODE (owner, 11 Sep 2026). The model decides what a line contains;
    it never decides what it costs.
    - A line listing `components` ([{item: <rate-card key or description>, qty}]) is priced from the card:
      the sum of rate x qty. A single component keeps its own qty and rate on the line (4 x actor day);
      several make one block amount whose description names what is inside. A component that is not on
      the card, is a reference-only package, or has no approved rate leaves the whole line BLANK, named.
    - A line with a typed `unit` keeps it only if the figure is in `allowed` (the owner's own words plus the
      card's rates), else it is blanked. allowed=None skips that check (internal callers only).
    - `target` (ex VAT): the lines priced from the card move together, pro rata, to reach it, but only
      inside +/- flex_pct (the company's band). Typed and at-cost lines never move. Outside the band, or
      with any line still blank, nothing moves and `error` says why, so the owner decides the scope.
    Mutates `sections` in place. Returns {blanked, missing, scaled, error, lines_total}."""
    idx = index(slug)
    blanked, missing, movable, fixed_total = [], [], [], 0.0
    for s in sections or []:
        for it in s.get("items") or []:
            label = str(it.get("desc") or "a line")[:70]
            comps = it.get("components") or []
            if comps:
                amt, basis, ok, at_cost = 0.0, [], True, True
                for c in comps:
                    name = str(c.get("item") or "").strip()
                    rec = idx.get(name) or idx.get(name.lower()) or idx.get(_slug(name))
                    price = (rec or {}).get("rate")
                    if rec and tier == "budget" and isinstance(rec.get("budget"), (int, float)):
                        price = rec["budget"]
                    if not rec or rec.get("reference_only") or not isinstance(price, (int, float)):
                        ok = False
                        missing.append(name or "?")
                        continue
                    q = float(c.get("qty") or 1)
                    amt += float(price) * q
                    at_cost = at_cost and rec["at_cost"]
                    basis.append(f"{q:g} x {rec['key']} @ {float(price):,.0f}")
                if not ok:
                    it["unit"], it["qty"] = None, 1
                    blanked.append(label)
                    continue
                if len(comps) == 1:
                    q1 = float(comps[0].get("qty") or 1)
                    it["qty"] = int(q1) if q1.is_integer() else q1      # prints "6", not "6.0"
                    it["unit"] = round(amt / q1, 2)
                else:
                    it["qty"], it["unit"] = 1, round(amt, 2)
                it["priced_from"] = "; ".join(basis)
                if at_cost:
                    fixed_total += amt
                else:
                    movable.append(it)
            elif it.get("unit") not in (None, ""):
                try:
                    u = round(float(it["unit"]), 2)
                except (TypeError, ValueError):
                    u = None
                if u is None or (allowed is not None and u not in allowed):
                    it["unit"] = None
                    blanked.append(label)
                    continue
                fixed_total += u * float(it.get("qty") or 1)
    moved = sum(float(it["unit"]) * float(it.get("qty") or 1) for it in movable)
    out = {"blanked": blanked, "missing": missing, "scaled": None, "error": None, "lines_total": moved + fixed_total}
    if target in (None, ""):
        return out
    target = float(target)
    band = float(flex_pct or 0)
    if blanked:
        out["error"] = (f"{len(blanked)} line(s) have no price yet ({'; '.join(blanked[:4])}), so the target "
                        f"{target:,.0f} was not applied")
        return out
    if moved <= 0:
        out["error"] = "no line is priced from the rate card, so there is nothing to move towards the target"
        return out
    factor = (target - fixed_total) / moved
    if factor <= 0 or abs(factor - 1) * 100 > band + 1e-9:
        out["error"] = (f"the lines come to {moved + fixed_total:,.0f}; reaching {target:,.0f} needs "
                        f"{(factor - 1) * 100:+.1f}% on the rate-card lines, outside the {band:g}% band. "
                        "Drop or add scope, or change the target.")
        return out
    for it in movable:
        it["unit"] = _round(float(it["unit"]) * factor)
    new = fixed_total + sum(float(it["unit"]) * float(it.get("qty") or 1) for it in movable)
    drift = round(target - new, 2)
    ones = [it for it in movable if float(it.get("qty") or 1) == 1]
    if drift and ones:            # park the rounding remainder on the largest block so the total is exact
        big = max(ones, key=lambda it: float(it["unit"]))
        big["unit"] = round(float(big["unit"]) + drift, 2)
    out["scaled"] = round((factor - 1) * 100, 1)
    out["lines_total"] = target
    return out


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
        lines.append(f"{g.get('heading', '')}{_group_tag(g)}:")
        for it in g.get("items", []):
            rate = f"{cur} {it['rate']:,.0f}" if isinstance(it.get("rate"), (int, float)) else "OWNER TO CONFIRM"
            if isinstance(it.get("budget"), (int, float)):
                rate += f" (Budget tier {cur} {it['budget']:,.0f})"
            note = f" ({it['note']})" if it.get("note") else ""
            lines.append(f"  - [{key_of(it)}] {it.get('desc', '')} — {rate} per {it.get('unit', 'each')}{note}")
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
        lines.append(g.get("heading", "") + _group_tag(g))
        for it in g.get("items", []):
            ref = f"[{key_of(it)}] "
            if isinstance(it.get("rate"), (int, float)):
                bit = f"  - {ref}{it.get('desc')}: {cur} {it['rate']:,.0f} per {it.get('unit', 'each')}"
                if isinstance(it.get("budget"), (int, float)):
                    bit += f" (Budget tier {cur} {it['budget']:,.0f})"
            elif isinstance(it.get("budget"), (int, float)):
                bit = (f"  - {ref}{it.get('desc')}: {cur} {it['budget']:,.0f} per "
                       f"{it.get('unit', 'each')} (Budget tier only)")
            else:
                bit = f"  - {ref}{it.get('desc')}: OWNER TO CONFIRM per {it.get('unit', 'each')}"
                gaps.append(str(it.get("desc")))
            if it.get("note"):
                bit += f" [{it['note']}]"
            lines.append(bit)
    if gaps:
        lines.append("")
        lines.append("Still needing a rate from Rashad: " + "; ".join(gaps[:10]))
    return "\n".join(lines)
