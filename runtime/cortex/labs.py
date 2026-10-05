"""Lab results (blood panels, body-composition scans) -> fitness.lab_results, plus the next-tests checklist.

Server-owned like fitness.cgm: the app reads them from the pull and never pushes them back. Health data never
lives in this repo: results reach the DB either from a one-off import file the owner's data was transcribed into
(`python -m cortex.labs import <file.json>`, file deleted after) or from a report he uploads in the app, which
the model transcribes (`extract`) into a preview he confirms before anything is stored (`save`). Flags are
computed here from the lab's own range, never taken from the model.

The checklist (fitness.lab_wishlist) is the owner's "Blood Test Quote List". An item counts as done once a
stored result with the same marker name exists dated after the list was made.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date

from . import db

EXTRACT_MODEL = "claude-opus-5-5"          # same owner-approved model as meal photos: accuracy over cost


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def flag(value, low, high) -> str | None:
    v = _num(value)
    if v is None:
        return None
    if high is not None and v > float(high):
        return "high"
    if low is not None and v < float(low):
        return "low"
    return None


def _uid(taken: str, marker: str) -> str:
    return f"lab_{taken}_{re.sub(r'[^a-z0-9]+', '_', marker.lower()).strip('_')}"[:120]


def _store(taken: str, lab: str, source: str, rows: list) -> int:
    n = 0
    for i, r in enumerate(rows):
        panel, marker, value, unit, low, high, ref_text = (list(r) + [None] * 7)[:7]
        if not marker:
            continue
        num = _num(value)
        db.execute(
            "insert into fitness.lab_results (uid, taken, panel, marker, value, value_text, unit, ref_low, ref_high, "
            "ref_text, flag, lab, source, sort) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set panel=excluded.panel, value=excluded.value, value_text=excluded.value_text, "
            "unit=excluded.unit, ref_low=excluded.ref_low, ref_high=excluded.ref_high, ref_text=excluded.ref_text, "
            "flag=excluded.flag, lab=excluded.lab, source=excluded.source, sort=excluded.sort",
            (_uid(taken, marker), taken, panel or "Other", str(marker)[:120], num,
             None if num is not None else (str(value)[:200] if value is not None else None),
             unit, _num(low), _num(high), ref_text, flag(num, low, high), lab, source, i))
        n += 1
    return n


def import_file(path: str) -> dict:
    """{"tests": [{taken, lab, source, rows: [[panel, marker, value, unit, low, high, ref_text]]}],
        "wishlist": [[category, marker, reason, priority]], "wishlist_listed_on": "YYYY-MM-DD"}"""
    doc = json.loads(open(path, encoding="utf-8").read())
    n = sum(_store(t["taken"], t["lab"], t["source"], t["rows"]) for t in doc.get("tests") or [])
    listed = date.fromisoformat(doc.get("wishlist_listed_on") or date.today().isoformat())
    for i, (cat, marker, reason, prio) in enumerate(doc.get("wishlist") or []):
        db.execute("insert into fitness.lab_wishlist (uid, category, marker, reason, priority, listed_on, sort) "
                   "values (%s,%s,%s,%s,%s,%s,%s) on conflict (uid) do update set category=excluded.category, "
                   "reason=excluded.reason, priority=excluded.priority, sort=excluded.sort",
                   (_uid("wish", marker), cat, marker, reason, prio, listed, i))
    return {"results": n, "wishlist": len(doc.get("wishlist") or [])}


def pull() -> dict:
    res = [{"id": r["uid"], "date": r["taken"].isoformat(), "panel": r["panel"], "marker": r["marker"],
            "value": _num(r["value"]), "text": r["value_text"], "unit": r["unit"], "low": _num(r["ref_low"]),
            "high": _num(r["ref_high"]), "ref": r["ref_text"], "flag": r["flag"], "lab": r["lab"]}
           for r in db.query("select * from fitness.lab_results order by taken, panel, sort")]
    have: dict[str, list[str]] = {}
    for r in res:
        have.setdefault(r["marker"].lower(), []).append(r["date"])
    wish = []
    for w in db.query("select * from fitness.lab_wishlist order by sort"):
        done = [d for d in have.get(w["marker"].lower(), []) if d > w["listed_on"].isoformat()]
        wish.append({"id": w["uid"], "category": w["category"], "marker": w["marker"], "reason": w["reason"],
                     "priority": w["priority"],
                     "done": max(done) if done else (w["done_on"].isoformat() if w["done_on"] else None)})
    return {"results": res, "wishlist": wish}


EXTRACT_SYSTEM = """You transcribe a medical laboratory report into JSON, exactly as printed. You never estimate,
round, convert units or fill gaps: a value you cannot read is omitted. Output JSON only."""
EXTRACT_PROMPT = """Read this lab report. Return {"taken": "YYYY-MM-DD" (specimen COLLECTION date as printed),
"lab": "lab name and lab/accession number", "rows": [[panel, marker, value, unit, ref_low, ref_high, ref_text], ...]}.
panel: one of Metabolic, Kidney, Liver, Lipids, Blood count, Iron, Vitamins, Minerals, Hormones, Thyroid, Inflammation,
Heavy metals, Screening, Urine, Body composition, Other. marker: plain English name; use these exact names when it is
the same test: HbA1c, Fasting glucose, Creatinine, eGFR, Urea nitrogen, Uric acid, Potassium, Vitamin D (25-OH),
Vitamin B12, Ferritin, ALT, AST, ALP, Albumin, Total protein, Total bilirubin, Direct bilirubin, Total cholesterol,
LDL cholesterol, HDL cholesterol, Triglycerides, Non-HDL cholesterol, TSH, PSA total, Free T3, Free T4,
Total testosterone, SHBG, GGT, Magnesium, CRP, ESR. value: the number as printed (or text such as "Negative").
ref_low/ref_high: numeric bounds of the normal or desirable range (null when open-ended); ref_text: the range text as
printed when it has tiers."""


def extract(data_url: str) -> dict:
    from . import provider
    out = provider.think_json(EXTRACT_SYSTEM, EXTRACT_PROMPT, model=EXTRACT_MODEL, fast=False, max_tokens=8000,
                              purpose="fitness_lab_extract", images=[data_url])
    rows = []
    for r in out.get("rows") or []:
        if isinstance(r, list) and len(r) >= 3 and r[1]:
            r = (list(r) + [None] * 7)[:7]
            rows.append(r + [flag(_num(r[2]), _num(r[4]), _num(r[5]))])
    return {"taken": str(out.get("taken") or "")[:10], "lab": str(out.get("lab") or "")[:200], "rows": rows}


def save(taken: str, lab: str, source: str, rows: list) -> dict:
    date.fromisoformat(taken)                       # raises on a bad date: never store an undated result
    return {"ok": True, "stored": _store(taken, lab[:200], source[:200], [list(r)[:7] for r in rows])}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "import":
        print(import_file(sys.argv[2]))
