"""Sync live Instantly lead STATUS into Cortex for every campaign lead.
instantly_lead_status <- derived from the API (Bounced / Completed / Active + contacted/replied) [overwrite].
instantly_interest_status <- 'Reply received' / 'Bounced' only-if-blank (don't clobber master-sheet interest).
Re-runnable; this is the standard status-refresh for Instantly leads."""
from collections import Counter

import httpx
from cortex import config, db

KEY = config.require("INSTANTLY_API_KEY")
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
BASE = "https://api.instantly.ai/api/v2"
CAMPAIGNS = ["e5ecf347-0406-41f7-a9b0-b5e99b9155fb", "a60d8dcb-1ec4-44e1-b175-25a1387c5e6f",
             "735a5a81-bbbe-49dd-a9e6-a1d83e6875c9", "6217f016-102d-4313-8f9c-d2ad9656e002",
             "66ad6eef-3b18-4f44-af02-9e116a91c9fa", "160a4905-2b5d-4dee-8f5d-6e00e007e8e0"]
RANK = {"Bounced": 5, "Completed | Reply received": 4, "Active | Reply received": 4,
        "Completed | Contacted": 3, "Active | Contacted": 2, "Active | Not yet contacted": 1}


def derive(L):
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


best = {}   # email -> status (keep the highest-rank across campaigns)
for cid in CAMPAIGNS:
    cursor = None
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
            st = derive(L)
            if e not in best or RANK[st] > RANK[best[e]]:
                best[e] = st
        cursor = j.get("next_starting_after")
        if not cursor or not items:
            break
print("leads with a live status:", len(best))

upd = 0
for e, st in best.items():
    interest = "Reply received" if "Reply received" in st else ("Bounced" if st == "Bounced" else None)
    if interest:
        db.execute("update crm_master set instantly_lead_status=%s, "
                   "instantly_interest_status=coalesce(nullif(btrim(instantly_interest_status),''),%s), "
                   "updated_at=now() where lower(email)=%s", (st, interest, e))
    else:
        db.execute("update crm_master set instantly_lead_status=%s, updated_at=now() where lower(email)=%s", (st, e))
    upd += 1
print("updated:", upd)
print("status breakdown:", dict(Counter(best.values())))
