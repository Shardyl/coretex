"""One-off: build the client-company Accounts directory from existing deals, deal-driven.

Rules: account name = the linked contact's company_name, else a clean name from the email domain,
else the deal's title prefix. Free-email-only deals with no company stay account-less (individuals).
HBMSU/Merck variants merge. Links each deal + its contacts to the account.
"""
import re

from cortex import crm, db

FREE = {"gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com", "yahoo.fr",
        "live.com", "protonmail.com", "googlemail.com"}
DOMMAP = {"roblox.com": "Roblox", "sobharealty.com": "Sobha Realty", "parthenon.ey.com": "EY-Parthenon",
          "purehealth.ae": "PureHealth", "weride.ai": "WeRide", "sanadak.gov.ae": "Sanadak",
          "hbmsu.ac.ae": "HBMSU", "seedgroup.com": "Seed Group", "accsal.com": "Accsal",
          "ibtikar.io": "Ibtikar", "habitstacker.co": "Habit Stacker", "hayatboulevard.com": "Hayat Boulevard",
          "upstageinfo.com": "Upstage", "qqq-hq.com": "QQQ-HQ", "wani4.com": "W4",
          "evercrestgroup.co.uk": "Evercrest Group", "omc.com": "Omnicom", "rcdubai.net": "Alif Voyage"}


def canon(name: str) -> str:
    n = (name or "").strip()
    if n.upper().startswith("HBMSU"):
        return "HBMSU"
    if n.startswith("Merck"):
        return "Merck"
    return n


def main():
    crm.ensure_deal_schema()
    deals = db.query("select id, title, contact_email, contacts from crm_projects")
    linked, skipped, accts = 0, 0, set()
    for d in deals:
        name, domain = None, None
        ce = d["contact_email"]
        if ce:
            c = db.one("select company_name from crm_master where lower(email)=lower(%s)", (ce,))
            cn = ((c.get("company_name") if c else "") or "").strip()
            dom = ce.split("@")[-1].lower()
            if dom not in FREE:
                domain = dom
            if cn:
                name = cn
            elif dom in DOMMAP:
                name = DOMMAP[dom]
            elif dom not in FREE:
                name = dom.split(".")[0].title()
            # else: free email + no company name -> individual, leave account-less
        else:
            name = re.split(r" - | [(]", d["title"] or "")[0].strip() or None
        if not name:
            skipped += 1
            continue
        name = canon(name)
        aid = crm.get_or_create_account(name, domain)
        accts.add(name)
        crm.link_account(aid, deal_id=d["id"])
        for c in (d["contacts"] or []):
            if c.get("email"):
                crm.link_account(aid, email=c["email"])
        if ce:
            crm.link_account(aid, email=ce)
        linked += 1
    print(f"deals linked: {linked} | individuals skipped: {skipped} | accounts created: {len(accts)}")
    print("accounts:", ", ".join(sorted(accts)))


if __name__ == "__main__":
    main()
