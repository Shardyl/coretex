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

import re

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


# A no-draft situation that only applies to mail NOT addressed to us. Read from the rule's OWN words
# ("... sent generically to a supplier register (not addressed to us specifically)"), so editing the
# rule changes the behaviour. Nothing about tenders or suppliers is decided here.
_ONLY_WHEN_NOT_ADDRESSED = re.compile(
    r"not\s+(?:addressed|sent|written)\s+(?:to|for)\s+us|sent\s+generically|undisclosed\s+recipients", re.I)


_TENDER_SITUATION = re.compile(r"tender|circular|broadcast|\brfp\b|no named recipient|dear (?:supplier|tenderer|vendor)", re.I)


# AN EMAIL THAT ASKS US FOR SOMETHING IS NEVER A CIRCULAR (the owner's own rule; enforced in code 19 Sep 2026).
# Special Olympics UAE wrote "Dear Supplier, please quote for the below" with the suppliers on bcc: a named
# person asking for a quotation for a shoot four days away. The model saw only the compiled label ("mass 'Dear
# Supplier' announcements with no named recipient"), the rule's exception never reached it, and no reply was
# drafted. Three facts decide it before any model is asked: it ASKS (a quote, a proposal, answers), a PERSON
# sent it, and it is answered by EMAIL rather than through a portal.
_ASKS = re.compile(
    r"\b(?:please|kindly)\s+(?:quote|provide|send|share|submit|confirm|advise)\b|quote for the below|"
    r"request for (?:a\s+)?(?:quotation|quote|proposal)|\brf[qp]\b|"
    r"submit (?:your|a|the) (?:proposal|quotation|quote|offer)|"
    r"your (?:best\s+)?(?:price|prices|quotation|quote|proposal|offer)|scope of work|"
    r"requested (?:media\s+)?deliverables|invite you to (?:submit|quote|bid)", re.I)
_VIA_PORTAL = re.compile(
    r"\bportal\b|e-?tender|tejari|unifiedreg|log ?in to|(?:through|via) the (?:system|platform)|"
    r"supplier registration system|بوابة", re.I)
_ROBOT_NAME = re.compile(r"application|portal|system|no-?reply|notification|mailer|automat", re.I)


def asks_us_by_email(email: dict) -> bool:
    """True when a named PERSON asks us for a quotation, proposal or answers and expects it by email."""
    text = f"{email.get('subject') or ''}\n{email.get('body') or email.get('snippet') or ''}"
    if not _ASKS.search(text) or _VIA_PORTAL.search(text):
        return False
    name = (email.get("name") or "").strip()
    local = (email.get("email") or "").split("@")[0]
    if email.get("auto_marker") or _ROBOT_NAME.search(name) or _ROBOT_NAME.search(local):
        return False
    return len(re.findall(r"[^\W\d_]{2,}", name)) >= 2 or bool(re.search(r"[._]", local))


def _addressed_to_us(email: dict, own_domain: str) -> bool:
    """Is one of OUR addresses on the To or Cc line? A fact from the headers, never a judgement."""
    rcpt = f"{email.get('to') or ''} {email.get('cc') or ''}".lower()
    d = (own_domain or "").strip().lower().lstrip("@")
    return bool(d) and ("@" + d) in rcpt


DEFAULT_CARD_CATEGORIES = ("lead", "client", "finance")


def card_categories(company: dict) -> tuple:
    """Which inbox categories get a drafted reply card. DATA per company (setting
    'card_categories:<slug>'), editable without a deploy; the tuple above is only the default. It was a
    literal inside poll_inbox, so no wording of a partner enquiry could ever produce a card (Antoni
    Entertainment, 9 Sep 2026 - left on the default by the owner's call, but now his to change)."""
    v = db.setting_get(f"card_categories:{(company or {}).get('slug') or ''}")
    if isinstance(v, list) and v:
        return tuple(str(x).strip().lower() for x in v if str(x).strip())
    return DEFAULT_CARD_CATEGORIES


def should_skip(company: dict, email: dict, skill_key: str = "sales-first-response",
                own_domain: str = "") -> dict | None:
    """Does this inbound email fall into a NO-DRAFT situation for this company? Returns {reason} to skip,
    or None to proceed. Cheap: no model call at all unless the company actually has such a rule."""
    try:
        sk = store.get_skill_by_key(company["id"], skill_key)
        if not sk:
            return None
        cfg = get(sk["id"])
        sits, addrs = list(cfg.get("situations") or []), cfg.get("addresses") or []
        # A RULE'S OWN EXCEPTION IS A FACT, SO CODE CHECKS IT. Sheraa's urgent ERF film RFP was sent
        # DIRECTLY to hello@sensa.digital with a deadline the next day; a model reading the body saw
        # "procurement" and matched it to "broadcast circulars ... (not addressed to us specifically)",
        # and it was skipped (10 Sep 2026). Whether we are on the To/Cc is not a judgement. When we are,
        # any situation that only applies to mail NOT addressed to us leaves the list before the model
        # ever sees it.
        if own_domain and _addressed_to_us(email, own_domain):
            sits = [s for s in sits if not _ONLY_WHEN_NOT_ADDRESSED.search(s)]
            # AN RFP WRITTEN TO US IS AN ENQUIRY (17 Sep 2026): Emergy's explainer-video RFP came to hello@ with
            # Marlon's colleagues on cc and was skipped as a "broadcast tender circular" because that rule's
            # text ("no named recipient") did not match the regex above. With our address on the To or Cc
            # line, no tender / circular / broadcast situation applies, whatever its wording.
            sits = [s for s in sits if not _TENDER_SITUATION.search(s)]
        if asks_us_by_email(email):    # bcc or not, "Dear Supplier" or not: a person asking us for a quote
            sits = [s for s in sits if not _TENDER_SITUATION.search(s)]
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
            "skip only when the email clearly IS one of those situations. An email in which a person asks US "
            "for a quotation, a proposal, prices or answers is NEVER a broadcast circular, however generic its "
            "greeting: never skip it.",
            f"From: {email.get('name')} <{email.get('email')}>\nSubject: {email.get('subject')}\n\n"
            + (email.get("body") or email.get("snippet") or "")[:1500],
            model=provider.MODEL_ROUTER, max_tokens=120, purpose="no-draft-check",
            company=company.get("slug"))
        if isinstance(out, dict) and out.get("skip"):
            return {"reason": (out.get("which") or "a standing no-draft rule")}
    except Exception:  # noqa: BLE001 - never block the inbox on this check
        return None
    return None
