"""Manager-run questionnaires — the deep interview that builds out a skill's rules.

10 areas (General Operations + the 9 departments) x 3 tiers:
  basic   = UNIVERSAL  (answered once, -> universal rules)
  deeper  = PER COMPANY (-> that company's local rules)
  deepest = PER COMPANY (-> deeper local rules)

Cortex (the Manager, Opus) drafts the questions, rule-aware: it covers every rule already set for the
relevant layer, and self-updates (appends questions) when rules change. Runs are resumable per lane.
At the end the Manager distills answers into proposed rules (cross-checked for contradictions) plus
roadmap/idea items for the parking lot.
"""
from __future__ import annotations

import hashlib

from psycopg.types.json import Json

from . import catalog, db, provider, store

TIERS = {
    "basic":   {"label": "Basic",   "scope": "universal", "count": (6, 8)},
    "deeper":  {"label": "Deeper",  "scope": "company",   "count": (12, 15)},
    "deepest": {"label": "Deepest", "scope": "company",   "count": (20, 25)},
}
GENERAL = "General Operations"


def areas() -> list[dict]:
    """General Operations (cross-cutting) + one area per department."""
    out = [{"key": GENERAL, "manager": "Operations lead", "category": "Run the business", "general": True}]
    seen = set()
    for cat, dept, mgr, _ in catalog.CATALOG:
        if dept not in seen:
            seen.add(dept)
            out.append({"key": dept, "manager": mgr, "category": cat, "general": False})
    return out


def _manager_for(area: str) -> str:
    if area == GENERAL:
        return "Operations lead"
    _, mgr = catalog.dept_meta(area)
    return mgr or f"{area} manager"


def _area_skill_keys(area: str) -> list[str]:
    for cat, dept, mgr, skills in catalog.CATALOG:
        if dept == area:
            return [k for k, *_ in skills]
    return []


def _area_skill_desc(area: str) -> str:
    if area == GENERAL:
        return "the company's overall operations: positioning, voice, priorities, and how it is run."
    ref = store.get_company_by_slug(catalog.COMPANIES[0][0])
    rows = db.query("select name, craft from skills where department=%s and company_id=%s order by name",
                    (area, ref["id"]))
    return "\n".join(f"- {r['name']}: {(r['craft'] or '')[:130]}" for r in rows) or f"- {area}"


def _existing_rules(area: str, tier: str, company_id: int) -> list[str]:
    keys = _area_skill_keys(area)
    if not keys:
        return []
    out: list[str] = []
    if TIERS[tier]["scope"] == "universal":
        for k in keys:
            out += store.get_universal_rules(k) or []
    else:
        for k in keys:
            r = db.one("select rules from skills where skill_key=%s and company_id=%s", (k, company_id))
            if r:
                out += r["rules"] or []
    return [str(x) for x in out if x]


def _sig(rules: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(rules)).encode("utf-8")).hexdigest()[:16]


def _norm(qs) -> list[dict]:
    out = []
    for i, q in enumerate(qs or []):
        if not isinstance(q, dict) or not q.get("text"):
            continue
        t = "choice" if q.get("type") == "choice" else "open"
        item = {"id": q.get("id") or f"q{i+1}", "text": str(q["text"]), "type": t}
        if t == "choice" and q.get("options"):
            item["options"] = [str(o) for o in q["options"]][:6]
        out.append(item)
    return out


def _full_generate(area: str, tier: str, rules: list[str]) -> list[dict]:
    lo, hi = TIERS[tier]["count"]
    scope_desc = ("universally — the same across ALL the companies" if TIERS[tier]["scope"] == "universal"
                  else "for THIS specific company")
    sys = (f"You are the {_manager_for(area)} at Cortex, Rashad's AI operations partner. Draft an interview "
           f"that learns how Rashad wants {area} handled {scope_desc}, so his answers can become concrete "
           "standing rules. Ask sharp, specific, decision-focused questions a busy founder can actually "
           "answer; mix multiple-choice (3-5 concrete options) with open questions. No generic fluff.")
    cover = ("\n\nMake sure the questionnaire COVERS these rules he has already set — include a question to "
             "revisit or refine each (pre-load the rule in the question):\n- " + "\n- ".join(rules)) if rules else ""
    usr = (f"Tier: {tier} — about {lo} to {hi} questions.\nThe skills in this area:\n{_area_skill_desc(area)}"
           + cover + '\n\nReturn JSON {"questions":[{"id":"q1","text":"...","type":"choice|open",'
           '"options":["..."]}]} (options only for choice questions).')
    out = provider.think_json(sys, usr, fast=False, max_tokens=4000)
    return _norm(out.get("questions"))


def _augment(area: str, tier: str, existing: list[dict], rules: list[str]) -> list[dict]:
    if not rules:
        return []
    exq = "\n".join(f"- {q.get('text', '')}" for q in existing)
    sys = (f"You are the {_manager_for(area)} at Cortex keeping a questionnaire in sync with the rules.")
    usr = (f"Existing questions:\n{exq}\n\nRules that should each be covered by a question:\n- "
           + "\n- ".join(rules) + "\n\nReturn JSON {\"questions\":[...]} with ONLY NEW questions for rules "
           "NOT already covered by an existing question (empty list if all are covered). Each new question "
           "pre-loads the rule it covers as context.")
    out = provider.think_json(sys, usr, fast=False, max_tokens=2000)
    return _norm(out.get("questions"))


def generate(area: str, tier: str, company_id: int = 0) -> list[dict]:
    """Return the question set for (area, tier, scope), building or self-updating it as needed."""
    cid = 0 if TIERS[tier]["scope"] == "universal" else company_id
    row = db.one("select * from questionnaires where department=%s and tier=%s and company_id=%s",
                 (area, tier, cid))
    rules = _existing_rules(area, tier, cid)
    sig = _sig(rules)
    if row and (row["questions"] or []) and row["rule_sig"] == sig:
        return row["questions"]                                   # up to date
    if row and (row["questions"] or []):
        questions = list(row["questions"]) + _augment(area, tier, row["questions"], rules)  # self-update
    else:
        questions = _full_generate(area, tier, rules)
    db.execute("insert into questionnaires (department,tier,company_id,questions,rule_sig,updated_at) "
               "values (%s,%s,%s,%s,%s,now()) on conflict (department,tier,company_id) do update set "
               "questions=excluded.questions, rule_sig=excluded.rule_sig, updated_at=now()",
               (area, tier, cid, Json(questions), sig))
    return questions


def open_area(area: str, company_id: int = 0) -> dict:
    """Status of all three tiers for an area (for the 'which tier / continue?' prompt)."""
    tiers = []
    for tier, cfg in TIERS.items():
        cid = 0 if cfg["scope"] == "universal" else company_id
        run = db.one("select idx,status from questionnaire_runs where department=%s and tier=%s and company_id=%s",
                     (area, tier, cid))
        q = db.one("select questions from questionnaires where department=%s and tier=%s and company_id=%s",
                   (area, tier, cid))
        total = len(q["questions"]) if q and q["questions"] else None
        tiers.append({"tier": tier, "label": cfg["label"], "scope": cfg["scope"],
                      "status": run["status"] if run else "not_started",
                      "answered": run["idx"] if run else 0, "total": total})
    return {"area": area, "manager": _manager_for(area), "tiers": tiers}


def _state(run: dict, questions: list[dict]) -> dict:
    idx, total = run["idx"], len(questions)
    cur = questions[idx] if idx < total else None
    return {"run_id": run["id"], "idx": idx, "total": total, "question": cur,
            "status": "done" if cur is None else "in_progress"}


def start(area: str, tier: str, company_id: int = 0, restart: bool = False) -> dict:
    cid = 0 if TIERS[tier]["scope"] == "universal" else company_id
    questions = generate(area, tier, cid)
    run = db.one("select * from questionnaire_runs where department=%s and tier=%s and company_id=%s",
                 (area, tier, cid))
    if restart or not run:
        run = db.execute(
            "insert into questionnaire_runs (company_id,department,tier,answers,idx,status) "
            "values (%s,%s,%s,'[]'::jsonb,0,'in_progress') on conflict (company_id,department,tier) "
            "do update set answers='[]'::jsonb, idx=0, status='in_progress', updated_at=now() returning *",
            (cid, area, tier))
    return _state(run, questions)


def answer(run_id: int, ans: str) -> dict:
    run = db.one("select * from questionnaire_runs where id=%s", (run_id,))
    if not run:
        return {"error": "no such run"}
    qs = generate(run["department"], run["tier"], run["company_id"])
    idx = run["idx"]
    if idx < len(qs):
        answers = list(run["answers"] or [])
        answers.append({"q": qs[idx]["text"], "a": ans})
        idx += 1
        status = "done" if idx >= len(qs) else "in_progress"
        run = db.execute("update questionnaire_runs set answers=%s, idx=%s, status=%s, updated_at=now() "
                         "where id=%s returning *", (Json(answers), idx, status, run_id))
    return _state(run, qs)


def distill(run_id: int) -> dict:
    """Turn answers into proposed rules (with contradiction flags) + roadmap items. Nothing saved yet."""
    run = db.one("select * from questionnaire_runs where id=%s", (run_id,))
    if not run:
        return {"error": "no such run"}
    area, tier, cid = run["department"], run["tier"], run["company_id"]
    scope = TIERS[tier]["scope"]
    qa = run["answers"] or []
    existing = _existing_rules(area, tier, cid)
    keys = _area_skill_keys(area)
    sys = (f"You are the {_manager_for(area)} at Cortex — keeper of the standard. Turn Rashad's questionnaire "
           "answers into concrete standing rules for the right skills. "
           f"Scope for these rules: {'UNIVERSAL (all companies)' if scope == 'universal' else 'this company only'}. "
           "CROSS-CHECK each proposed rule against the existing rules and flag any contradiction. Separately, "
           "pull out any PENDING ACTION or IDEA (things to DO, not standing rules) as roadmap items.")
    usr = (f"Area: {area}\nSkill keys you may attach rules to: {', '.join(keys) or '(general)'}\n"
           f"Area skills:\n{_area_skill_desc(area)}\n\nExisting rules (for contradiction check):\n- "
           + ("\n- ".join(existing) if existing else "(none)") + "\n\nAnswers:\n"
           + "\n".join(f"Q: {x.get('q')}\nA: {x.get('a')}" for x in qa)
           + '\n\nReturn JSON {"rules":[{"skill":"<skill_key or empty>","rule":"...",'
           '"scope":"universal|company","contradicts":"<exact existing rule it conflicts with, or empty>"}],'
           '"roadmap":["pending action or idea", ...]}')
    out = provider.think_json(sys, usr, fast=False, max_tokens=4000)
    rules = []
    for r in (out.get("rules") or []):
        if not isinstance(r, dict) or not r.get("rule"):
            continue
        rules.append({"skill": r.get("skill") or (keys[0] if keys else ""),
                      "rule": str(r["rule"]),
                      "scope": "universal" if r.get("scope") == "universal" else ("universal" if scope == "universal" else "company"),
                      "contradicts": str(r.get("contradicts") or "")})
    roadmap = [str(x) for x in (out.get("roadmap") or []) if x]
    return {"rules": rules, "roadmap": roadmap, "scope": scope, "company_id": cid, "area": area, "tier": tier}
