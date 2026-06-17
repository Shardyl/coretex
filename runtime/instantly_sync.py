"""STANDARD Instantly -> Cortex sync (idempotent, re-runnable; scheduled daily via cron).

For every Instantly CAMPAIGN lead:
  - attribute to the business that emailed it (campaign -> sending domain), tag org NON-DESTRUCTIVELY,
  - set lead_source='Instantly Super Search' + campaign_name only-if-blank,
  - refresh instantly_lead_status (Bounced/Completed/Active + contacted/replied) [overwrite, live truth],
  - set instantly_interest_status 'Reply received'/'Bounced' only-if-blank,
  - import genuinely-new leads as Cold (real Super Search name/company/title carried over).
Safe to run repeatedly. Run:  /opt/coretex/.venv/bin/python /opt/coretex/runtime/instantly_sync.py
"""
from collections import Counter

import httpx
from cortex import config, crm, db

KEY = config.require("INSTANTLY_API_KEY")
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
BASE = "https://api.instantly.ai/api/v2"

# campaign_id -> business slug (separated by the sending domain on each campaign; locked 2026-06-17)
CAMPAIGN_BIZ = {
    "e5ecf347-0406-41f7-a9b0-b5e99b9155fb": "snaprewards",   # SHOPIFY AGENCIES ALL POSITIONS
    "a60d8dcb-1ec4-44e1-b175-25a1387c5e6f": "snaprewards",   # SHOPIFY AGENCIES
    "735a5a81-bbbe-49dd-a9e6-a1d83e6875c9": "snaprewards",   # UK Grocery
    "6217f016-102d-4313-8f9c-d2ad9656e002": "snaprewards",   # SHOPIFY PLUS INSTORE
    "66ad6eef-3b18-4f44-af02-9e116a91c9fa": "sensa",         # SENSA AGENCY CAMPAIGN 2
    "160a4905-2b5d-4dee-8f5d-6e00e007e8e0": "sensa",         # Dubai Marketing Managers
}
RANK = {"Bounced": 5, "Completed | Reply received": 4, "Active | Reply received": 4,
        "Completed | Contacted": 3, "Active | Contacted": 2, "Active | Not yet contacted": 1}


def derive_status(L):
    s = L.get("status")
    replied = (L.get("email_reply_count") or 0) > 0
    contacted = bool((L.get("status_summary") or {}).get("lastStep"))
    if s == -1:
        return "Bounced"
    if s == 3:
        return "Completed | Reply received" if replied else "Completed | Contacted"
    if replied:
        return "Active | Reply received"
    return "Active | Contacted" if contacted else "Active | Not yet contacted"


def main():
    camps = {c["id"]: c["name"] for c in (httpx.get(f"{BASE}/campaigns", headers=H, params={"limit": 100}, timeout=40).json().get("items") or [])}
    leadmap = {}
    for cid, slug in CAMPAIGN_BIZ.items():
        label, cursor = crm._org(slug), None
        while True:
            body = {"limit": 100, "campaign": cid}
            if cursor:
                body["starting_after"] = cursor
            j = httpx.post(f"{BASE}/leads/list", headers=H, json=body, timeout=60).json()
            items = j.get("items") or []
            for L in items:
                e = (L.get("email") or "").strip().lower()
                if not e:
                    continue
                d = leadmap.setdefault(e, {"biz": set(), "first": None, "last": None, "company": None,
                                           "title": None, "campaign": None, "status": None})
                d["biz"].add(label)
                d["first"] = d["first"] or (L.get("first_name") or None)
                d["last"] = d["last"] or (L.get("last_name") or None)
                d["company"] = d["company"] or (L.get("company_name") or None)
                d["title"] = d["title"] or (L.get("job_title") or None)
                d["campaign"] = d["campaign"] or camps.get(cid)
                st = derive_status(L)
                if d["status"] is None or RANK[st] > RANK[d["status"]]:
                    d["status"] = st
            cursor = j.get("next_starting_after")
            if not cursor or not items:
                break

    cortex = {r["e"]: r for r in db.query(
        "select id, lower(btrim(email)) e, organisation from crm_master where email is not null and btrim(email)<>''")}

    tagged = imported = 0
    for e, d in leadmap.items():
        labels = sorted(d["biz"])
        interest = "Reply received" if "Reply received" in (d["status"] or "") else ("Bounced" if d["status"] == "Bounced" else None)
        row = cortex.get(e)
        if row:
            cur = row["organisation"] or ""
            adds = [b for b in labels if b.lower() not in cur.lower()]
            neworg = (cur + ", " + ", ".join(adds)).strip(", ") if adds else (cur or ", ".join(labels))
            db.execute(
                "update crm_master set organisation=%s, "
                "lead_source=coalesce(nullif(btrim(lead_source),''),%s), "
                "campaign_name=coalesce(nullif(btrim(campaign_name),''),%s), "
                "instantly_lead_status=%s, "
                "instantly_interest_status=coalesce(nullif(btrim(instantly_interest_status),''),%s), "
                "updated_at=now() where id=%s",
                (neworg, "Instantly Super Search", d["campaign"], d["status"], interest, row["id"]))
            tagged += 1
        else:
            db.execute(
                "insert into crm_master (organisation, first_name, last_name, email, company_name, job_title, "
                "lead_source, campaign_name, instantly_lead_status, instantly_interest_status, stage) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Cold') on conflict (lower(email)) do nothing",
                (", ".join(labels), d["first"], d["last"], e, d["company"], d["title"],
                 "Instantly Super Search", d["campaign"], d["status"], interest))
            imported += 1

    print(f"instantly_sync: {len(leadmap)} campaign leads | tagged {tagged} | imported {imported} | "
          f"status {dict(Counter(d['status'] for d in leadmap.values()))}")


if __name__ == "__main__":
    main()
