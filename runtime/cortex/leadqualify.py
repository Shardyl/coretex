"""Lead qualification — research the lead, then classify into a tier.

The MECHANISM lives here (live web research + classification + a neutral default). The CRITERIA and the
per-tier handling are the owner's to train, stored on the 'lead-qualification' skill (craft + rules); this
reads whatever has been taught and applies it. Nothing about how to treat each tier is baked in here.
"""
from __future__ import annotations

from . import provider, store

TIER_LABEL = {
    1: "Level 1 — self-serve fit",
    2: "Level 2 — worth a conversation",
    3: "Level 3 — high-value / priority",
}


def _taught(company_id: int) -> tuple[str, list]:
    sk = store.get_skill_by_key(company_id, "lead-qualification")
    if not sk:
        return "", []
    uni, loc = store.effective_rules(sk)
    return (sk.get("craft") or ""), list(uni) + list(loc)


def qualify(inq: dict, company: dict) -> dict:
    """Research the lead and classify it. Returns {genuine, tier, tier_label, reason, research, signals,
    recommended_action}. Falls back to a message-only read (default tier 2) if web research is unavailable."""
    craft, rules = _taught(company["id"])
    taught = "\n".join(f"- {r}" for r in rules) if rules else "(no extra rules taught yet)"
    system = (
        "You qualify an inbound sales lead for Tabscanner, a receipt-OCR / data-extraction API whose core "
        "value-add is custom training for businesses that process receipts at volume.\n\n"
        "Decide first whether this is junk/spam (gibberish, off-topic, bots, SEO/marketing solicitations, "
        "mismatched sender) or a genuine prospect. If genuine, classify into a tier per the owner's frame:\n"
        f"{craft or 'Level 1 = low custom-training potential (self-serve); Level 2 = worth a conversation; Level 3 = high-value, clear volume.'}\n"
        "When genuine but you are unsure of the tier, DEFAULT TO LEVEL 2.\n\n"
        f"The owner's additional trained rules (highest priority):\n{taught}"
    )
    user = (
        f"Lead: {inq.get('name')} <{inq.get('email')}>\nSubject: {inq.get('subject')}\n"
        f"Message:\n{(inq.get('message') or inq.get('snippet') or '').strip()}\n\n"
        "Investigate them (their email-domain website, any app and its traction, scale/funding signals), "
        'then return JSON: {"genuine": boolean, "tier": 1|2|3, "reason": "short why this tier", '
        '"research": "what you found about their size, stage and likely receipt volume", '
        '"signals": ["concrete evidence points"], "recommended_action": "short suggested next step"}'
    )
    researched = True
    try:
        out = provider.research_json(system, user)
    except Exception:  # noqa: BLE001 — web research unavailable, fall back to the message alone
        researched = False
        out = provider.think_json(system, user)
    tier = out.get("tier")
    tier = tier if tier in (1, 2, 3) else 2
    return {"genuine": bool(out.get("genuine", True)), "tier": tier, "tier_label": TIER_LABEL[tier],
            "reason": (out.get("reason") or "").strip(),
            "research": (out.get("research") or "").strip(),
            "signals": out.get("signals") or [], "researched": researched,
            "recommended_action": (out.get("recommended_action") or "").strip()}
