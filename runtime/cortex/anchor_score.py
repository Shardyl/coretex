"""
anchor_score.py - score & type freshly-harvested anchor leads (whale / amplifier / decision-maker) using the
LIVE FilmSpoke outreach-icp rules (the buyer logic lives in the skill, never hardcoded here). Haiku, batched,
prompt-cached system. Origin facts (the score itself is the model's judgement; counts/dates are code-stamped).
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


def score_harvested(company_id: int = 5, limit: int = 40) -> dict:
    """Score the untyped harvested leads for the company. Returns {scored, of}."""
    rows = db.query("select id, job_title, note, tags from crm_master where tags @> %s::jsonb "
                    "order by id desc limit %s", ('["anchor-harvest"]', limit * 3))
    rows = [r for r in rows if "scored" not in (r.get("tags") or [])][:limit]
    if not rows:
        return {"scored": 0, "of": 0}

    system = (
        "You classify + score harvested LinkedIn leads for FilmSpoke (which sells finished, broadcast-quality "
        "commercials made with AI). Using ONLY the ICP rules below, for EACH lead return: "
        "classification = 'lead' if they are a genuine BUYER (a brand owner / in-house marketer / founder who "
        "could commission ad production) OR 'vendor' if they are NOT a buyer (a service-seller, agency, "
        "freelancer, AI-tool builder, video editor, or peer); "
        "type = only when classification is 'lead', one of whale|amplifier|decision-maker (else null); "
        "score 0-100 (buyer intent x fit; a frustrated-DIY comment scores UP; vendors/peers score LOW); "
        "and a one-line reason. "
        "Return JSON: {\"results\":[{\"id\":<id>,\"classification\":\"lead|vendor\",\"type\":\"...\",\"score\":<int>,\"reason\":\"...\"}]}.\n\n"
        "ICP RULES:\n" + _icp_rules(company_id))
    leads = [{"id": r["id"], "title": r.get("job_title") or "", "comment": (r.get("note") or "")[:280]} for r in rows]
    out = provider.think_json(system, "Score these leads:\n" + json.dumps(leads), fast=True, cache=True,
                              purpose="anchor-score", company="filmspoke", max_tokens=3500)

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
