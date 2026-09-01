"""AUTO-REPLIES CARRY REAL NEWS — read them instead of throwing them away.

An "Automatic reply" is correctly never answered, so the whole class was being discarded. But two of
them change what we should do next, and both were invisible:

  * "I no longer work for EY, please direct your queries to <someone else>" — the person we are
    chasing has GONE. Every future chase to them is dead mail. This happened on the ITC project on
    1 Sep 2026, one minute after a payment chase went to Tim Piper, and nothing recorded it.
  * "I am out of the office until the 14th" — the chase clock should move, not keep firing into an
    empty inbox.

Nothing here ever drafts a reply. It records what the bounce said, moves the CRM on, and tells the
owner. Addresses are taken from the text by REGEX and only ever chosen from that list, so a
successor contact can never be invented.
"""
from __future__ import annotations

import re

from . import db, notifications, provider

# subjects Google/Outlook give an auto-response; `auto_marker` (Auto-Submitted header) catches the rest
AUTO_SUBJECT = ("automatic reply", "auto reply", "autoreply", "out of office", "out-of-office",
                "away from the office", "abwesenheitsnotiz", "réponse automatique")

# a departure, said in the ways people actually say it
_GONE = re.compile(
    r"\b(no longer (work|works|with|employed|at|part of)|has (left|departed)|is no longer|"
    r"have left the (company|firm|business)|left the (company|firm|business)|"
    r"no longer (an? )?(employee|member of staff))\b", re.I)

_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def is_auto(msg: dict) -> bool:
    subj = (msg.get("subject") or "").lower().lstrip()
    return bool(msg.get("auto_marker")) or any(subj.startswith(p) for p in AUTO_SUBJECT)


def _addresses(body: str, exclude: str = "") -> list[str]:
    """Every address written in the body, minus the sender and our own people. These are the ONLY
    values a successor may be chosen from — the model never supplies an address."""
    from .identity import OWN_COMPANY_DOMAINS as OURS   # our own people, not "not a client"
    out, seen = [], set()
    for a in _EMAIL_RX.findall(body or ""):
        low = a.lower().strip(".")
        if low in seen or low == (exclude or "").lower():
            continue
        if low.split("@")[-1] in OURS:
            continue
        seen.add(low)
        out.append(low)
    return out


def read(msg: dict) -> dict:
    """What this auto-reply actually tells us. {'kind': 'departure'|'away'|'none', ...}"""
    body = (msg.get("body") or msg.get("snippet") or "")[:2000]
    sender = (msg.get("email") or "").lower()
    if not body:
        return {"kind": "none"}
    cands = _addresses(body, exclude=sender)
    if _GONE.search(body):
        successor = ""
        if len(cands) == 1:
            successor = cands[0]
        elif cands:
            # more than one address offered: let the model pick which is the REPLACEMENT, but only
            # from the addresses that genuinely appear in the text
            try:
                out = provider.think_json(
                    "This is an automatic email reply saying the person has left. Which of the listed "
                    "addresses is the REPLACEMENT contact to write to now? Choose ONLY from the list, "
                    'exactly as written, or "" if none of them is a replacement. '
                    'Return {"successor": "<address or empty>"}.',
                    f"REPLY:\n{body}\n\nADDRESSES FOUND: {', '.join(cands)}",
                    model=provider.MODEL_ROUTER, max_tokens=80, purpose="autoreply-successor")
                pick = ((out or {}).get("successor") or "").strip().lower()
                successor = pick if pick in cands else ""
            except Exception:  # noqa: BLE001
                successor = ""
        return {"kind": "departure", "successor": successor, "quote": body.strip()[:400]}
    return {"kind": "away", "quote": body.strip()[:300]}


def handle(msg: dict, company: dict, deals: list[dict] | None = None) -> dict | None:
    """Act on an auto-reply. Returns a summary when something was recorded, else None. Never drafts."""
    info = read(msg)
    if info.get("kind") != "departure":
        return None            # 'away' is handled by the follow-up clock, not here
    sender = (msg.get("email") or "").lower()
    successor = info.get("successor") or ""
    cid = (company or {}).get("id")

    # 1. the CRM: the contact is gone, and it must never be chased again
    try:
        db.execute("update crm_master set lead_status='left-company', updated_at=now() "
                   "where lower(email)=lower(%s)", (sender,))
    except Exception:  # noqa: BLE001
        pass
    acct = (db.one("select account_id from crm_master where lower(email)=lower(%s)", (sender,)) or {}).get("account_id")

    # 2. the successor becomes a real contact on the same account (never invented — it was in the text)
    added = False
    if successor and acct:
        # the successor may already exist as a loose contact (Konstantinos did) - put them on the
        # same account either way, or the next chase cannot resolve them through it
        db.execute("update crm_master set account_id=%s, updated_at=now() where lower(email)=lower(%s) "
                   "and account_id is null", (acct, successor))
    if successor and not db.one("select 1 from crm_master where lower(email)=lower(%s)", (successor,)):
        try:
            from . import crm
            crm.add_inbound_contact({"email": successor, "name": ""}, company.get("slug"),
                                    "named as the replacement contact in a departure auto-reply",
                                    stage="Engaged", source="departure auto-reply", newsletter=False)
            if acct:
                db.execute("update crm_master set account_id=%s where lower(email)=lower(%s)", (acct, successor))
            added = True
        except Exception:  # noqa: BLE001
            pass

    # 3. every affected deal: on the timeline, and the contact repointed so chases reach a real person
    touched = []
    for d in (deals or []):
        try:
            from . import pipeline
            pipeline.log_deal(int(d["id"]), "contact_left",
                              f"{sender} has LEFT: \"{info['quote'][:200]}\""
                              + (f" Successor named: {successor}." if successor else
                                 " No replacement was named."))
            if successor:
                db.execute("update crm_projects set contact_email=%s, updated_at=now() "
                           "where id=%s and lower(contact_email)=lower(%s)",
                           (successor, int(d["id"]), sender))
            touched.append(int(d["id"]))
        except Exception:  # noqa: BLE001
            continue

    notifications.notify(
        f"{sender} has left their company"
        + (f", write to {successor} instead" if successor else ", no replacement named"),
        (f"Their auto-reply says: \"{info['quote'][:220]}\"\n"
         + (f"Deal contact repointed on {', '.join('#' + str(t) for t in touched)}. " if touched and successor else "")
         + ("The new contact was added to the CRM." if added else "")),
        priority="high", category="reminder", company_id=cid,
        target_type=("deal" if touched else None), target_id=(str(touched[0]) if touched else None),
        dedup_key=f"left:{sender}")
    return {"departure": sender, "successor": successor, "deals": touched, "contact_added": added}
