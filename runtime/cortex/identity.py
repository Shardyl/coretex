"""One definition of 'this is US' — every module that needs the company domains imports from here.
The audit found three drifting copies (engine.OWN_COMPANY_DOMAINS, meetnotes._OWN, scrape rules)."""
OWN_COMPANY_DOMAINS = {"sensa.digital", "skyvision.film", "tabscanner.com", "snap-rewards.com",
                       "filmspoke.ai", "coretex.uk", "sensa.film", "sensafilms.com"}
# domains that attend our meetings but are not client organisations (the owner's personal account)
NON_CLIENT_DOMAINS = OWN_COMPANY_DOMAINS | {"gmail.com"}


import re as _re

_PLACEHOLDER_RE = _re.compile(r"^linked\d+@")      # LinkedIn placeholder identities, not real inbound senders


def internal_index() -> dict:
    """Everything that means 'this address is US / internal' (audit step 2e tidy-up: there were three definitions,
    engine's, the sent sweep's own-mailbox domains, and meetnotes'). Own company domains, every connected mailbox's
    domain, the Instantly burner SENDING domains (setting own_sending_domains), the named internal roster (setting
    internal_people) and the newsletter test group. Read live."""
    from . import db
    from .crm import FREE_EMAIL
    domains = set(OWN_COMPANY_DOMAINS)
    domains |= {str(d).lower().strip() for d in (db.setting_get("own_sending_domains") or [])}
    emails = {str(e).lower().strip() for e in (db.setting_get("internal_people") or [])}
    try:
        for r in db.query("select value from settings where key like 'gmail_account%%' or key like 'gmail_send_account%%'"):
            v = (r["value"] if isinstance(r["value"], str) else str(r["value"] or "")).strip('" ').lower()
            if "@" not in v:
                continue
            # a free-mail mailbox (the owner's personal Gmail) is ONE address, never its whole domain: gmail.com as
            # "internal" would have hidden every client writing from Gmail
            if v.split("@")[-1] in FREE_EMAIL:
                emails.add(v)
            else:
                domains.add(v.split("@")[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        emails |= {r["email"].lower() for r in db.query("select lower(email) email from newsletter_test_group where active")}
    except Exception:  # noqa: BLE001
        pass
    return {"domains": domains, "emails": emails}


def is_internal(addr: str, own_domain: str = "", index: dict | None = None) -> bool:
    a = (addr or "").lower().strip()
    d = a.split("@")[-1]
    if own_domain and (d == own_domain or d.endswith("." + own_domain)):
        return True
    if _PLACEHOLDER_RE.match(a):
        return True
    idx = index if index is not None else internal_index()
    return d in idx["domains"] or a in idx["emails"]
