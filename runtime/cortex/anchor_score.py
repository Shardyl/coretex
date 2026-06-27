"""
anchor_score.py - classify + score harvested anchor engagers as buyer (`lead`) vs service-seller (`vendor`),
using the LIVE FilmSpoke outreach-icp rules (the buyer logic lives in the skill, never hardcoded). Haiku,
batched, prompt-cached. Two entry points:
  - classify_leads(leads): classify RAW leads at INGEST (before insert) -> caller stores ONLY the buyers
                           and counts the vendors for the anchor's hit-rate.
  - score_harvested()    : re-score already-inserted rows not yet scored (legacy / re-runs).
"""
import json

from psycopg.types.json import Json

from . import db, provider, store, crm

TYPES = ("whale", "amplifier", "decision-maker")


def _icp_rules(company_id: int) -> str:
    uni = (db.one("select rules from universal_skill_rules where skill_key='outreach-icp'") or {}).get("rules") or []
    sk = store.get_skill_by_key(company_id, "outreach-icp")
    loc = (sk or {}).get("rules") or []
    return "\n".join(f"- {r}" for r in (list(uni) + list(loc)))


def _system(company_id: int, key: str) -> str:
    return (
        "You classify + score harvested LinkedIn leads for FilmSpoke (which sells finished, broadcast-quality "
        "commercials made with AI). Using ONLY the ICP rules below, for EACH lead return: "
        "classification = 'lead' if they are a genuine BUYER (a brand owner / in-house marketer / founder who "
        "could commission ad production) OR 'vendor' if they are NOT a buyer (a service-seller, agency, "
        "freelancer, AI-tool builder, video editor, or peer); "
        "type = only when classification is 'lead', one of whale|amplifier|decision-maker (else null); "
        "score 0-100 (buyer intent x fit; a frustrated-DIY comment scores UP; vendors/peers score LOW); "
        "and a one-line reason. "
        f'Return JSON: {{"results":[{{"{key}":<{key}>,"classification":"lead|vendor","type":"...","score":<int>,"reason":"..."}}]}}.\n\n'
        "ICP RULES:\n" + _icp_rules(company_id))


def classify_leads(leads: list[dict], company_id: int = 5) -> list[dict]:
    """Classify RAW harvested leads (headline + comment) BEFORE insert. Returns a list aligned to the input
    order; each item is {classification, type, score, reason} (or {} if the model returned none). The caller
    stores only classification=='lead' and counts the rest toward the anchor's hit-rate."""
    if not leads:
        return []
    items = [{"i": i, "title": (l.get("headline") or "")[:160], "comment": (l.get("engagement") or "")[:280]}
             for i, l in enumerate(leads)]
    out = provider.think_json(_system(company_id, "i"), "Classify these leads:\n" + json.dumps(items),
                              fast=True, cache=True, purpose="anchor-classify", company="filmspoke", max_tokens=3500)
    by_i = {r.get("i"): r for r in (out.get("results") or []) if isinstance(r.get("i"), int)}
    return [by_i.get(i, {}) for i in range(len(leads))]


def score_harvested(company_id: int = 5, limit: int = 40) -> dict:
    """Re-score already-inserted harvested rows that aren't scored yet (legacy / re-runs)."""
    rows = db.query("select id, job_title, note, tags from crm_master where tags @> %s::jsonb "
                    "and not (coalesce(tags,'[]'::jsonb) @> %s::jsonb) order by id limit %s",
                    ('["anchor-harvest"]', '["scored"]', limit))
    if not rows:
        return {"scored": 0, "of": 0}
    leads = [{"id": r["id"], "title": r.get("job_title") or "", "comment": (r.get("note") or "")[:280]} for r in rows]
    out = provider.think_json(_system(company_id, "id"), "Score these leads:\n" + json.dumps(leads), fast=True,
                              cache=True, purpose="anchor-score", company="filmspoke", max_tokens=3500)
    n = 0
    for res in (out.get("results") or []):
        rid = res.get("id")
        cls = (res.get("classification") or "").strip().lower()
        typ = (res.get("type") or "").strip().lower()
        sc = res.get("score")
        if rid is None or cls not in ("lead", "vendor"):
            continue
        newtags = set((db.one("select tags from crm_master where id=%s", (rid,)) or {}).get("tags") or []) | {"scored"}
        if cls == "lead" and typ in TYPES:
            newtags.add(f"type:{typ}")
        db.execute("update crm_master set tags=%s::jsonb, tier=%s, classification=%s, updated_at=now() where id=%s",
                   (Json(sorted(newtags)), (str(sc) if sc is not None else None), cls, rid))
        n += 1
    return {"scored": n, "of": len(rows)}
