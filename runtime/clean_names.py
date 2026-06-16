"""Database-wide name cleanup. first_name must hold a plausible human first name OR be blank.
 - Backfill: blank first_name + a name in last_name -> extract the first name (split where 'First Last').
 - Fold-out: a company/brand/gibberish sitting in first_name -> move it to last_name, blank the first name.
AI (fast model) judges each candidate word so company/brand names are rejected. DRY RUN unless --apply.
Non-destructive: words move between fields, nothing is dropped. Run with no args = dry run.
"""
import re
import sys

from cortex import db, provider

APPLY = "--apply" in sys.argv


def legible(w):
    w = (w or "").strip(".,-_/()").strip()
    if len(w) < 2 or len(w) > 20 or re.search(r"\d", w):
        return None
    if sum(c.isalpha() for c in w) / max(len(w), 1) < 0.85:
        return None
    if any(c.isalpha() and ord(c) > 0x017F for c in w):     # mojibake / non-Latin gibberish
        return None
    return w


rows = db.query("select id, first_name, last_name from crm_master")
backfill, present, junk_first = [], [], []
words = set()
for r in rows:
    fn = (r["first_name"] or "").strip()
    ln = (r["last_name"] or "").strip()
    if not fn and ln:
        tok = ln.split()
        w = legible(tok[0])
        if w:
            backfill.append((r["id"], w, " ".join(tok[1:]), ln))
            words.add(w.lower())
    elif fn:
        w = legible(fn.split()[0])
        if w is None:
            junk_first.append((r["id"], fn, ln))           # clearly junk in first_name -> fold out (no AI)
        else:
            present.append((r["id"], w, fn, ln))
            words.add(w.lower())

SYS = ("You judge whether each token is a plausible HUMAN FIRST NAME (a given name from ANY culture — English, "
       "Arabic, Indian, Spanish, Chinese/Korean transliterated, African, etc., including lowercase or uncommon "
       "ones), versus NOT a first name. Reject: company/brand/product names (e.g. Speedycash, Snapcart, Verticurl, "
       "Rappi), generic words (Best, Pay, Online, Hello, Test, Admin), job titles, and gibberish. "
       "Return a JSON object mapping each token EXACTLY as given to true (is a first name) or false.")
cls = {}
wl = sorted(words)
for i in range(0, len(wl), 80):
    batch = wl[i:i + 80]
    try:
        r = provider.think_json(SYS, "Tokens: " + ", ".join(batch), fast=True, max_tokens=3500, purpose="name-classify")
        r = {k.lower(): v for k, v in r.items()}
        for w in batch:
            cls[w] = bool(r.get(w, False))
    except Exception as e:  # noqa: BLE001
        for w in batch:
            cls[w] = False
    print(f"  classified {min(i+80,len(wl))}/{len(wl)} words…")

changes = []   # (id, new_first, new_last, kind)
for cid, w, rest, ln in backfill:
    if cls.get(w.lower()):
        changes.append((cid, w, (rest or None), "backfill"))
for cid, w, fn, ln in present:
    if not cls.get(w.lower()):                              # a non-name sitting in first_name -> fold out
        newlast = ((ln + " " + fn).strip()) or None
        changes.append((cid, None, newlast, "foldout"))
for cid, fn, ln in junk_first:
    newlast = ((ln + " " + fn).strip()) or None
    changes.append((cid, None, newlast, "foldout"))

from collections import Counter
kinds = Counter(k for *_, k in changes)
print(f"\nrows scanned: {len(rows)} | distinct words judged: {len(wl)} | AI said 'name': {sum(cls.values())}")
print(f"proposed changes: {len(changes)}  ->  {dict(kinds)}")
print("\n--- sample BACKFILL (blank first name filled) ---")
n = 0
for cid, nf, nl, k in changes:
    if k == "backfill" and n < 12:
        old = db.one("select last_name from crm_master where id=%s", (cid,))["last_name"]
        print(f"  '{old}'  ->  first='{nf}'  last='{nl or ''}'"); n += 1
print("\n--- sample FOLD-OUT (company/junk moved out of first name) ---")
n = 0
for cid, nf, nl, k in changes:
    if k == "foldout" and n < 10:
        old = db.one("select first_name, last_name from crm_master where id=%s", (cid,))
        print(f"  first='{old['first_name']}' last='{old['last_name']}'  ->  first=''  last='{nl or ''}'"); n += 1

if APPLY:
    for cid, nf, nl, k in changes:
        db.execute("update crm_master set first_name=%s, last_name=%s, updated_at=now() where id=%s", (nf, nl, cid))
    print(f"\nAPPLIED {len(changes)} changes.")
else:
    print(f"\nDRY RUN — nothing changed. Re-run with --apply to commit {len(changes)} changes.")
