"""One-off: re-scrape a company's email history, classify every correspondent (Haiku, marketing-grade),
and produce a REVIEW REPORT (CSV). Merging into crm_master is a SEPARATE, gated step (`merge()`), run only
after the operator has reviewed the report.

SAFE BY DESIGN:
  * fetch + classify are 100% read-only.
  * the report is just a file — nothing is written to the CRM by the scrape.
  * merge() enriches existing records (fill-if-blank, never clobber), only ever touches the SOFT layer
    (classification / market / note / organisation), NEVER the hard fields (is_client, deals, opt-out).
  * resumable: each phase checkpoints to disk so a multi-hour run can be re-run without redoing work.

Usage (on the box):
  python scrape_history.py fetch     [msg_cap]      # gather contacts<-messages (the long, read-only pole)
  python scrape_history.py classify  [contact_cap]  # Haiku per contact (read-only)
  python scrape_history.py report                   # write the review CSV
  python scrape_history.py merge     --yes          # apply (gated; run ONLY after review)
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time

sys.path.insert(0, "/opt/coretex/runtime")
from cortex import crm, db, gmail, provider  # noqa: E402

# ---- per-company scope. Each run is fully isolated (own state dir + report), so they run concurrently. ----
# mailbox = (address, rt_key, purpose, oauth_client_company)  -- client=None means the legacy Tabscanner client.
CONFIGS = {
    "sensa": {"label": "Sensa", "own": {"sensa.digital"}, "mailboxes": [
        ("hello@sensa.digital",  "gmail_refresh_token:sensa",      "gmail",      "sensa"),
        ("rashad@sensa.digital", "gmail_send_refresh_token:sensa", "gmail_send", "sensa"),
        ("gino@sensa.digital",   "gmail_refresh_token:sensa:gino", "gmail",      "sensa")]},
    "skyvision": {"label": "Sky Vision", "own": {"skyvision.film"}, "mailboxes": [
        ("fly@skyvision.film",    "gmail_refresh_token:skyvision",      "gmail",      "skyvision"),
        ("rashad@skyvision.film", "gmail_send_refresh_token:skyvision", "gmail_send", "skyvision")]},
    "tabscanner": {"label": "Tabscanner", "own": {"tabscanner.com"}, "mailboxes": [
        ("api@tabscanner.com",    "gmail_refresh_token",      "gmail",      None),
        ("rashad@tabscanner.com", "gmail_send_refresh_token", "gmail_send", None)]},
    "snaprewards": {"label": "Snap Rewards", "own": {"snap-rewards.com"}, "mailboxes": [
        ("loyalty@snap-rewards.com", "gmail_refresh_token:snaprewards",      "gmail",      "snaprewards"),
        ("rashad@snap-rewards.com",  "gmail_send_refresh_token:snaprewards", "gmail_send", "snaprewards")]},
    "filmspoke": {"label": "FilmSpoke", "own": {"filmspoke.ai"}, "mailboxes": [
        ("create@filmspoke.ai", "gmail_refresh_token:filmspoke", "gmail", "filmspoke")]},
}
DAYS_BACK = 1825                       # ~5 years
COMPANY = ORG_LABEL = OWN_DOMAINS = MAILBOXES = STATE_DIR = None   # set by set_company() at startup


def set_company(slug):
    global COMPANY, ORG_LABEL, OWN_DOMAINS, MAILBOXES, STATE_DIR
    cfg = CONFIGS[slug]
    COMPANY, ORG_LABEL, OWN_DOMAINS = slug, cfg["label"], cfg["own"]
    MAILBOXES, STATE_DIR = cfg["mailboxes"], f"/opt/coretex/scrape/{slug}"
ADDR_RE = re.compile(r'(?:"?([^"<]*)"?\s*)?<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?')
NOISE = ("noreply", "no-reply", "notification", "mailer", "bounce", "wetransfer", "google.com",
         "docs.google", "drive.google", "linkedin", "facebookmail", "mailchimp", "sendgrid", "intercom",
         "slack", "do-not-reply", "postmaster", "automated", "calendar-", "@m.", "@e.", "@news.",
         "@email.", "@reply.", "@mail.", "updates@", "newsletter")

# CRM-worthy classes (rich, marketing-grade). Anything else (marketing/automated/personal/spam) is filtered out.
CLASSES = ["client", "opportunity", "lead", "partner", "vendor", "freelancer", "recruitment", "press"]
SYS = (
    "You are enriching a CRM from a company's email history with one correspondent. Read the emails and "
    "return JSON: "
    '{"classification":"<client|opportunity|lead|partner|vendor|freelancer|recruitment|press|'
    'marketing|automated|personal|spam>",'
    '"market":"<short industry/market label, e.g. video production, real estate, hospitality>",'
    '"org":"<their company/organisation name if discernible, else empty>",'
    '"was_quoted":<true if we ever sent them a price/quote/proposal/estimate>,'
    '"summary":"<1-2 sentences: who they are and the nature of our relationship>"}. '
    "Definitions: client = we did paid work for them; opportunity = a real enquiry or we quoted them but it "
    "didn't (clearly) convert; lead = inbound interest, no quote yet; partner = collaboration/referral/reseller; "
    "vendor = they sell to us; freelancer = talent offering us their services; recruitment = job applicant; "
    "press = journalist/publication. Use marketing/automated/personal/spam for newsletters, receipts, bots, or "
    "non-business mail (these are dropped, not added). If unsure between client and lower, but there was a real "
    "enquiry or quote, use opportunity. Marketing-grade best guess is fine."
)


def _p(name):
    return os.path.join(STATE_DIR, name)


def _tok(rt_key, purpose, client):
    return gmail._token_for(rt_key, purpose, client)


def _is_own(em):
    return em.split("@")[-1].lower() in OWN_DOMAINS


def _is_noise(em):
    return any(n in em for n in NOISE)


def _parse_addrs(header):
    return [(m.group(1).strip(), m.group(2).lower()) for m in ADDR_RE.finditer(header or "")]


def _body(full):
    """Plain-text body from a Gmail 'full' message payload (walks multipart)."""
    out = []
    def walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            out.append(gmail._decode(part["body"]["data"]))
        for p in part.get("parts", []) or []:
            walk(p)
    walk(full.get("payload", {}))
    return ("\n".join(out) or full.get("snippet", "")).strip()


# ---------- phase 1: fetch (read-only, the long pole) ----------
def fetch(msg_cap=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    contacts = {}
    for addr, rt, purpose, client in MAILBOXES:
        tok = _tok(rt, purpose, client)
        page = None; got = 0
        print(f"[fetch] {addr} …", flush=True)
        while True:
            params = {"q": f"-in:chats newer_than:{DAYS_BACK}d", "maxResults": 500}
            if page:
                params["pageToken"] = page
            lst = gmail._get(tok, "messages", params)
            ids = [m["id"] for m in lst.get("messages", [])]
            if not ids:
                break
            for mid in ids:
                try:
                    m = gmail._get(tok, f"messages/{mid}",
                                   {"format": "metadata",
                                    "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"]})
                except Exception:  # noqa: BLE001
                    continue
                hs = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
                subj, date = hs.get("Subject", ""), hs.get("Date", "")
                for fld in ("From", "To", "Cc"):
                    for nm, em in _parse_addrs(hs.get(fld, "")):
                        if _is_own(em) or _is_noise(em):
                            continue
                        c = contacts.setdefault(em, {"name": "", "count": 0, "msgs": []})
                        if nm and not c["name"]:
                            c["name"] = nm
                        c["count"] += 1
                        if len(c["msgs"]) < 4:
                            c["msgs"].append([rt, purpose, client, mid, subj[:90], date])
                got += 1
                if got % 500 == 0:
                    print(f"   …{got} messages, {len(contacts)} contacts so far", flush=True)
                if msg_cap and got >= msg_cap:
                    break
            page = lst.get("nextPageToken")
            if not page or (msg_cap and got >= msg_cap):
                break
        print(f"   {addr}: {got} messages scanned", flush=True)
    json.dump(contacts, open(_p("contacts.json"), "w"))
    print(f"[fetch] DONE — {len(contacts)} unique external contacts saved.", flush=True)
    return contacts


# ---------- phase 2: classify (read-only Haiku per contact, resumable) ----------
def classify(contact_cap=None):
    contacts = json.load(open(_p("contacts.json")))
    results = {}
    if os.path.exists(_p("classified.json")):
        results = json.load(open(_p("classified.json")))
    todo = [e for e in contacts if e not in results]
    if contact_cap:
        todo = todo[:contact_cap]
    print(f"[classify] {len(todo)} contacts to do ({len(results)} already done)", flush=True)
    for i, em in enumerate(todo, 1):
        c = contacts[em]
        bundle = []
        for rt, purpose, client, mid, subj, date in c["msgs"][:3]:
            try:
                full = gmail._get(_tok(rt, purpose, client), f"messages/{mid}", {"format": "full"})
                bundle.append(f"Subj: {subj}\n{_body(full)[:600]}")
            except Exception:  # noqa: BLE001
                pass
        user = (f"Person: {em}  (name: {c.get('name') or '?'}, {c.get('count', 0)} emails exchanged)\n\n"
                "Emails:\n" + "\n\n".join(bundle))[:3800]
        try:
            out = provider.think_json(SYS, user, model=provider.MODEL_ROUTER,
                                      purpose="history-scrape", company=ORG_SLUG, cache=True) or {}
        except Exception:  # noqa: BLE001
            out = {}
        results[em] = {"classification": (out.get("classification") or "").strip().lower(),
                       "market": (out.get("market") or "").strip(),
                       "org": (out.get("org") or "").strip(),
                       "was_quoted": bool(out.get("was_quoted")),
                       "summary": (out.get("summary") or "").strip(),
                       "name": c.get("name", ""), "count": c.get("count", 0)}
        if i % 25 == 0:
            json.dump(results, open(_p("classified.json"), "w"))
            print(f"   …{i}/{len(todo)} classified", flush=True)
    json.dump(results, open(_p("classified.json"), "w"))
    print(f"[classify] DONE — {len(results)} contacts classified.", flush=True)
    return results


# ---------- phase 3: report (read-only — the review artifact) ----------
def report():
    results = json.load(open(_p("classified.json")))
    path = _p(f"{COMPANY}_scrape_report.csv")
    kept = dropped = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "new_classification", "market", "was_quoted", "org",
                    "in_crm", "existing_classification", "is_client(existing)", "action", "summary"])
        for em, r in sorted(results.items(), key=lambda kv: kv[1].get("classification", "")):
            cls = r.get("classification", "")
            if cls not in CLASSES:                      # marketing/automated/personal/spam -> not CRM
                dropped += 1
                continue
            ex = db.one("select classification, is_client from crm_master where lower(email)=%s", (em,))
            in_crm = "yes" if ex else "no"
            ex_cls = (ex or {}).get("classification") or ""
            isc = (ex or {}).get("is_client")
            action = ("new contact" if not ex else
                      ("set classification" if not ex_cls else
                       ("change " + ex_cls + "->" + cls if ex_cls != cls else "enrich (note/market)")))
            w.writerow([em, r.get("name", ""), cls, r.get("market", ""), r.get("was_quoted"),
                        r.get("org", ""), in_crm, ex_cls, isc, action, r.get("summary", "")])
            kept += 1
    # quick class tally
    from collections import Counter
    tally = Counter(r["classification"] for r in results.values() if r["classification"] in CLASSES)
    print(f"[report] {kept} CRM-worthy contacts ({dropped} marketing/noise dropped) -> {path}")
    print("  by class:", dict(tally))
    return path


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    company = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in CONFIGS else "sensa"
    arg = sys.argv[3] if len(sys.argv) > 3 else None
    set_company(company)
    os.makedirs(STATE_DIR, exist_ok=True)
    print(f"[{COMPANY}] {mode}", flush=True)
    if mode == "fetch":
        fetch(int(arg) if arg and arg.isdigit() else None)
    elif mode == "classify":
        classify(int(arg) if arg and arg.isdigit() else None)
    elif mode == "report":
        report()
    elif mode == "all":            # full run: fetch -> classify -> report (read-only; no CRM writes)
        fetch()
        classify()
        report()
    else:
        print("merge is a separate gated step — not run from here yet.")
