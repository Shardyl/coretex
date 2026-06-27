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
    org = crm._org("filmspoke")
    rows = db.query("select id, job_title, note, tags from crm_master where organisation=%s and stage='harvested' "
                    "order by id desc limit %s", (org, limit * 3))
    rows = [r for r in rows if "scored" not in (r.get("tags") or [])][:limit]
    if not rows:
        return {"scored": 0, "of": 0}

    system = (
        "You score harvested LinkedIn leads for FilmSpoke (which sells finished, broadcast-quality commercials "
        "made with AI). Using ONLY the ICP rules below, for EACH lead return: type (exactly one of "
        "whale|amplifier|decision-maker), score 0-100 (intent x fit: a frustrated-DIY comment scores UP; peers, "
        "AI builders/hype, video editors and UGC creators score LOW), and a one-line reason. "
        "Return JSON: {\"results\":[{\"id\":<id>,\"type\":\"...\",\"score\":<int>,\"reason\":\"...\"}]}.\n\n"
        "ICP RULES:\n" + _icp_rules(company_id))
    leads = [{"id": r["id"], "title": r.get("job_title") or "", "comment": (r.get("note") or "")[:280]} for r in rows]
    out = provider.think_json(system, "Score these leads:\n" + json.dumps(leads), fast=True, cache=True,
                              purpose="anchor-score", company="filmspoke", max_tokens=3500)

    n = 0
    for res in (out.get("results") or []):
        rid, typ, sc = res.get("id"), (res.get("type") or "").strip().lower(), res.get("score")
        if rid is None or typ not in TYPES:
            continue
        cur = (db.one("select tags from crm_master where id=%s", (rid,)) or {}).get("tags") or []
        tags = sorted(set(cur) | {f"type:{typ}", "scored"})
        db.execute("update crm_master set tags=%s::jsonb, tier=%s, classification=%s, updated_at=now() where id=%s",
                   (Json(tags), (str(sc) if sc is not None else None), typ, rid))
        n += 1
    return {"scored": n, "of": len(rows)}
