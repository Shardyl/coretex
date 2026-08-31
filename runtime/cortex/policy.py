"""NO-DRAFT POLICY — the rules that decide whether an email gets a drafted reply AT ALL.

The dumb-waiter problem this solves: a rule like "never draft responses to support emails, Ben
handles those" was being written onto the DRAFTING skill, where it can never work. By the time the
drafter reads its rules the card already exists, and the worker's job is to write — it cannot decline
to be created. The decision "should this email produce a card at all" happens earlier, in triage.

So the same rules are compiled into a NO-DRAFT policy (like envelope.py compiles cc behaviour) and
evaluated at triage, before any card is made. Behaviour still lives in the owner's rules; code only
executes them.
"""
from __future__ import annotations

from . import db, provider, store


def _key(skill_id: int) -> str:
    return f"no_draft:{int(skill_id)}"


def compile_skill(skill_id: int) -> dict:
    """Distil this skill's rules into the situations where NO reply must be drafted."""
    sk = store.get_skill(skill_id)
    if not sk:
        return {}
    from . import worker
    rules = list(worker._rule_lines(sk))
    cfg: dict = {"situations": [], "addresses": []}
    if rules:
        try:
            out = provider.think_json(
                "You are reading a company's standing rules for handling email. Find ONLY the rules that "
                "say a reply must NOT be drafted at all for some kind of message (e.g. 'support emails are "
                "handled by X, never draft a reply', 'never respond to recruitment'). Ignore every rule "
                "about HOW to write, tone, pricing, cc or attachments.\n"
                'Return JSON {"situations": ["<short description of the message type that must NEVER get a '
                'drafted reply, e.g. \'support or technical help requests\'>"], "addresses": ["<any email '
                'address the rule says handles those instead>"]}. Empty lists if no such rule exists.',
                "\n".join(f"- {r}" for r in rules), model=provider.MODEL_FAST, max_tokens=400,
                purpose="compile-no-draft")
            if isinstance(out, dict):
                cfg["situations"] = [str(s).strip() for s in (out.get("situations") or []) if str(s).strip()][:8]
                cfg["addresses"] = [str(a).strip().lower() for a in (out.get("addresses") or [])
                                    if "@" in str(a)][:8]
        except Exception:  # noqa: BLE001 - a compile hiccup must never block a rule change
            return db.setting_get(_key(skill_id)) or cfg
    db.setting_set(_key(skill_id), cfg)
    return cfg


def get(skill_id: int) -> dict:
    return db.setting_get(_key(skill_id)) or {"situations": [], "addresses": []}


def should_skip(company: dict, email: dict, skill_key: str = "sales-first-response") -> dict | None:
    """Does this inbound email fall into a NO-DRAFT situation for this company? Returns {reason} to skip,
    or None to proceed. Cheap: no model call at all unless the company actually has such a rule."""
    try:
        sk = store.get_skill_by_key(company["id"], skill_key)
        if not sk:
            return None
        cfg = get(sk["id"])
        sits, addrs = cfg.get("situations") or [], cfg.get("addresses") or []
        if not sits and not addrs:
            return None
        # deterministic first: the rule named the mailbox that handles these
        blob = " ".join(str(email.get(k) or "") for k in ("to", "cc", "from", "email")).lower()
        for a in addrs:
            if a in blob:
                return {"reason": f"handled by {a} (standing rule)"}
        if not sits:
            return None
        out = provider.think_json(
            "Decide whether this inbound email is one the company has told us to NEVER draft a reply to. "
            "The no-draft situations are:\n" + "\n".join(f"- {s}" for s in sits) +
            '\nReturn JSON {"skip": boolean, "which": "<the matching situation, or empty>"}. Be strict: '
            "skip only when the email clearly IS one of those situations.",
            f"From: {email.get('name')} <{email.get('email')}>\nSubject: {email.get('subject')}\n\n"
            + (email.get("body") or email.get("snippet") or "")[:1500],
            model=provider.MODEL_ROUTER, max_tokens=120, purpose="no-draft-check",
            company=company.get("slug"))
        if isinstance(out, dict) and out.get("skip"):
            return {"reason": (out.get("which") or "a standing no-draft rule")}
    except Exception:  # noqa: BLE001 - never block the inbox on this check
        return None
    return None
