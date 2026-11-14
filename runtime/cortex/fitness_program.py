"""Mobility programs and session logging (owner, 14 Nov 2026).

A PROGRAM is one phase of one pillar: drills in session order (A, B, C, D...) with a dose. It is BUILT BY CODE from
his latest assessment results and the rules that ride in the drill library (setting fitness_mobility_drills):
- pancake: Smith's own phase 1 decision tree (lesson 64), questions answered from the test results where they can be;
- shoulders: our rule set from the course notes (Smith's real builder logic is hidden on his side), marked as ours.
A built program is a DRAFT until he starts it. Nothing here guesses a number: a drill's dose is the one its lesson
note states, else Smith's beginner design rules (lessons 203-208): sets by slot, reps = time under tension / tempo.

A SESSION logs, per drill, the sets done, the depth marker and whether he went deeper than last time. The next target
is Smith's progression ladder (lesson 08), computed by code from that history:
  went deeper -> same dose at the new depth; not deeper -> more time under tension (+2 reps / +15 s hold / +1 contract-
  relax round) until double the start; then +1 set up to 5; then stalled -> check recovery; still stalled -> the
  program has run its course (retest, new phase). About 6 sessions make a phase, then a deload week and a retest.
"""
from __future__ import annotations

import re
import secrets
from datetime import date

from psycopg.types.json import Json

from . import db
from . import fitness_mobility as fm

SESSIONS_PER_PHASE = 6           # lesson 215: a phase repeats its program about 6 times, then deload + retest
SLOT_SETS = {"technique": "1", "isolated": "2-3", "main": "3-4", "attribute": "2-3"}   # lesson 207
TUT_LOW = {"passive": 60, "loaded": 45, "isometric": 20, "contract_relax": 30, "active": 30}   # lesson 206/208
MAX_SETS = 5


# ---------- assessment results -> answers ----------

def latest() -> dict:
    """{test: {side: {measure: value-or-label}}} from the newest day each test/side was measured."""
    out: dict = {}
    for r in db.query("select pillar, test, side, measure, value, label, day from fitness.mobility_results "
                      "where not deleted order by day"):
        v = r["label"] if r["label"] is not None else (float(r["value"]) if r["value"] is not None else None)
        out.setdefault(r["pillar"], {}).setdefault(r["test"], {}).setdefault(r["side"], {})[r["measure"]] = v
    return out


def _val(res: dict, test: str, measure: str, side: str | None = None):
    t = res.get(test) or {}
    if side:
        return (t.get(side) or {}).get(measure)
    for s in ("both", "L", "R"):
        if measure in (t.get(s) or {}):
            return t[s][measure]
    return None


def _worst(res: dict, test: str, measure: str):
    vs = [x for x in (_val(res, test, measure, "L"), _val(res, test, measure, "R")) if isinstance(x, (int, float))]
    if not vs:
        v = _val(res, test, measure, "both")
        return v if isinstance(v, (int, float)) else None
    return min(vs)


def pancake_answers(res: dict, given: dict) -> tuple[dict, list]:
    """Smith's four phase 1 questions. Answered from results where possible; `given` (his answers) wins."""
    a, missing = {}, []
    cal = _val(res, "tailors_pose", "calves_floor")
    if cal is not None:
        a["q1_calves_floor"] = str(cal).lower()
    tp = str(_val(res, "pancake_natural", "touchpoint") or "").lower()
    ang = _val(res, "pancake_natural", "angle")
    if tp in ("forehead", "nose", "chin"):
        a["q2_pancake_type"] = "head_to_floor"
    elif tp in ("chest", "abs"):
        a["q2_pancake_type"] = "chest_to_floor"
    elif isinstance(ang, (int, float)):
        a["q2_pancake_type"] = "more_than_90" if ang > 90 else "less_than_90"
    nar, lift = _val(res, "pancake_narrow", "angle"), _val(res, "pancake_lift", "angle")
    if isinstance(nar, (int, float)) and isinstance(lift, (int, float)):
        a["q3_lift_gap"] = "yes" if abs(nar - lift) > 20 else "no"
    a.update({k: v for k, v in (given or {}).items() if v not in (None, "")})
    if "q4_sport" not in a and given and given.get("pancake_sport"):
        a["q4_sport"] = "no" if given["pancake_sport"] == "no" else "yes"
    for q, why in (("q1_calves_floor", "tailors pose: did both calves touch the floor"),
                   ("q2_pancake_type", "natural width pancake: angle or touchpoint"),
                   ("q4_sport", "training pancake for a sport?")):
        if q not in a:
            missing.append({"id": q, "why": why})
    if a.get("q2_pancake_type") in ("more_than_90", "less_than_90") and "q3_lift_gap" not in a:
        missing.append({"id": "q3_lift_gap", "why": "narrow pancake and pancake lift angles"})
    return a, missing


def _pancake_items(lib: dict, a: dict) -> tuple[list, list]:
    tree = lib["rules"]["pancake_phase1"]
    items, notes = [], []
    for x in tree["always"]:
        items.append({"code": x["slot_code"], "drill_id": x["drill_id"], "optional": bool(x.get("optional")),
                      "why": x.get("why")})
    for b in tree["branches"]:
        if all(a.get(k) == v for k, v in b["when"].items()):
            for x in b["add"]:
                if not any(i["code"] == x["slot_code"] for i in items):
                    items.append({"code": x["slot_code"], "drill_id": x["drill_id"],
                                  "why": "Smith's phase 1 tree: " + ", ".join(f"{k}={v}" for k, v in b["when"].items())})
            if b.get("note"):
                notes.append(b["note"])
    return items, notes


# Our shoulder rules (from the course notes; Smith's builder logic is not visible). Each = (metric test, drills, why).
# Metrics are computed below from the results; a rule whose metric is missing does not fire.
def _shoulder_metrics(res: dict, given: dict) -> dict:
    m = {"leahy_worst": _worst(res, "leahy", "angle"), "active_worst": _worst(res, "flexion_active", "angle"),
         "passive": _val(res, "flexion_passive", "angle"), "pec_worst": _worst(res, "pec_major", "angle"),
         "er_worst": _worst(res, "er_length", "angle"), "ir_worst": _worst(res, "ir_length", "angle"),
         "lat_wall": _val(res, "lat_length", "wall"), "lat_round": _val(res, "lat_length", "round_back"),
         "trap3": _worst(res, "trap3_reps", "reps_exact"), "eok_er": _worst(res, "eok_er_reps", "reps_exact"),
         "powell": _worst(res, "powell_reps", "reps_exact"), "type": (given or {}).get("shoulder_type")}
    if isinstance(m["passive"], (int, float)) and isinstance(m["active_worst"], (int, float)):
        m["pva_gap"] = m["passive"] - m["active_worst"]
    stab = sorted([(v, k) for k, v in (("eok_er", m["eok_er"]), ("trap3", m["trap3"]), ("powell", m["powell"]))
                   if isinstance(v, (int, float)) and v < 5])
    m["weak_stabilisers"] = [k for _, k in stab[:2]]
    return m


def _n(x):
    return isinstance(x, (int, float))


SHOULDER_RULES = [
    (lambda m: _n(m["lat_wall"]) and _n(m["lat_round"]) and (m["lat_round"] < m["lat_wall"] or m["lat_round"] < 180),
     ["lat_stretch_standing_lying"], "lats: round-back flexion lower than flat-back (lesson 143)"),
    (lambda m: _n(m["pec_worst"]) and m["pec_worst"] < 45, ["wall_pec_stretch"], "pec major under 45 deg past the shoulder line (144/150)"),
    (lambda m: _n(m["er_worst"]) and m["er_worst"] < 45, ["er_contract_relax"], "external rotation short (146)"),
    (lambda m: _n(m["ir_worst"]) and m["ir_worst"] < 135, ["sleeper_stretch"], "internal rotation short (148)"),
    (lambda m: _n(m["leahy_worst"]) and m["leahy_worst"] >= 180, ["sleeper_stretch", "ir_contract_relax"],
     "full Leahy flexion: work internal rotation and scapula strength instead (141)"),
    (lambda m: "eok_er" in m["weak_stabilisers"], ["elbow_on_knee_er"], "elbow-on-knee ER under 5 reps at target weight (149)"),
    (lambda m: "trap3" in m["weak_stabilisers"], ["trap3_raise"], "lower trap raise under 5 reps at target weight (149/168)"),
    (lambda m: "powell" in m["weak_stabilisers"], ["powell_raise"], "Powell raise under 5 reps at target weight (149)"),
]


def _shoulder_items(lib: dict, res: dict, given: dict) -> tuple[list, list, list]:
    m = _shoulder_metrics(res, given)
    picked, notes = [], []
    for test, drills, why in SHOULDER_RULES:
        try:
            hit = test(m)
        except Exception:
            hit = False
        if hit:
            for d in drills:
                if d not in [p[0] for p in picked]:
                    picked.append((d, why))
    # the passive vs active gap and shoulder type pick the hang (165): stiff-but-strong -> passive, else active
    hang = "hang_passive" if m.get("type") == "easier_strength_than_flexibility" else "hang_active"
    picked.append((hang, "hanging, by shoulder type and the passive vs active gap (165)"))
    if "elbow_on_knee_er" not in [p[0] for p in picked]:
        picked.append(("elbow_on_knee_er", "almost always included (166)"))
    # the band routine opens every shoulder session as its technique/warm-up drill (152)
    picked.insert(0, ("straps_band_routine", "warm-up routine before every shoulder session (152)"))
    missing = [k for k in ("leahy_worst", "passive", "active_worst", "pec_worst", "er_worst") if m.get(k) is None]
    if missing:
        notes.append("Not yet measured: " + ", ".join(missing) + ". The program uses what is measured; retest to refine.")
    notes.append("Shoulder rules are ours, written from the course notes (Smith's own builder logic is not visible).")
    return [{"drill_id": d, "why": w} for d, w in picked], notes, missing


# ---------- dose ----------

def _tempo_sum(t: str | None) -> int | None:
    if not t:
        return None
    ds = [int(c) for c in str(t) if c.isdigit()]
    return (sum(ds) or None) if len(ds) >= 3 else None


def _hi(x) -> int | None:
    ns = re.findall(r"\d+", str(x or ""))
    return int(ns[1]) if len(ns) > 1 else (int(ns[0]) if ns else None)


def _lo(x) -> int | None:
    m = re.match(r"\s*(\d+)", str(x or ""))
    return int(m.group(1)) if m else None


def dose(drill: dict) -> dict:
    """The drill's own numbers from its note; the gaps filled from the design rules and labelled so."""
    d = dict(drill.get("dose") or {})
    src, filled = d.get("source") or "", []
    slot, meth = drill.get("slot"), drill.get("method")
    if meth == "soft_tissue":                      # by feel (lessons 64/82): no sets, reps or rest to prescribe
        d.update({"sets": d.get("sets") or "1", "by_feel": True})
        d["source"], d["filled"] = src, []
        return d
    if not d.get("sets"):
        d["sets"] = SLOT_SETS.get(slot, "2-3")
        filled.append("sets")
    if not d.get("rest_s"):
        d["rest_s"] = "60-90"
        filled.append("rest")
    if not d.get("reps") and not d.get("hold_s"):
        if meth == "soft_tissue":
            pass                                   # by feel (lesson 64/82): no number to give
        elif meth == "passive":
            d["hold_s"] = "60"
            filled.append("hold")
        elif meth == "isometric":
            d["hold_s"] = "20"
            filled.append("hold")
        elif meth == "contract_relax":
            d["reps"] = "3"
            d["cr"] = d.get("cr") or "C5 R5"
            filled.append("rounds")
        elif meth == "strength":
            d["reps"] = "5-8"
            filled.append("reps")
        elif TUT_LOW.get(meth) and _tempo_sum(d.get("tempo")):
            d["reps"] = str(max(1, round(TUT_LOW[meth] / _tempo_sum(d["tempo"]))))
            filled.append("reps from time under tension / tempo")
        elif meth == "active":
            d["hold_s"] = "30"                     # an active hold with no tempo (e.g. a hang): active TUT 30 s
            filled.append("hold")
    d["source"] = src
    d["filled"] = filled
    return d


def _code_items(items: list, lib: dict) -> list:
    """Order by slot (technique, isolated, main, attribute) and give A1, B1, B2... codes where none is set."""
    by = {d["id"]: d for d in lib["drills"]}
    order = {"technique": 0, "isolated": 1, "main": 2, "attribute": 3}
    letters, out = "ABCDEFG", []
    if all(i.get("code") for i in items):
        items = sorted(items, key=lambda i: i["code"])
    else:
        items = sorted(items, key=lambda i: order.get(by.get(i["drill_id"], {}).get("slot"), 9))
        cur, n, last = -1, 0, None
        for i in items:
            s = order.get(by.get(i["drill_id"], {}).get("slot"), 9)
            if s != last:
                cur, n, last = cur + 1, 0, s
            n += 1
            i["code"] = f"{letters[cur]}{n}"
    for i in items:
        dr = by.get(i["drill_id"])
        if not dr:
            continue
        out.append({**i, "name": dr["name"], "slot": dr.get("slot"), "method": dr.get("method"),
                    "unilateral": bool(dr.get("unilateral")), "dose": dose(dr), "marker": dr.get("marker"),
                    "load": dr.get("load"), "lesson_slug": dr.get("lesson_slug"),
                    "lesson_course": dr.get("lesson_course"), "youtube": dr.get("youtube")})
    return out


def build(pillar: str, answers: dict | None = None) -> dict:
    """A DRAFT program from the latest results. Never saved here."""
    lib = fm.drill_library()
    res = latest().get(pillar, {})
    notes: list = []
    if pillar == "pancake":
        a, missing = pancake_answers(res, answers or {})
        items, notes = _pancake_items(lib, a)
        if missing:
            return {"pillar": pillar, "ready": False, "answers": a, "missing": missing, "items": [], "notes": notes}
        rules = "Smith's pancake phase 1 builder (lesson 64)"
    elif pillar == "shoulder_flexion":
        a = dict(answers or {})
        items, notes, missing = _shoulder_items(lib, res, a)
        if {"leahy_worst", "passive", "active_worst"} <= set(missing):
            # assess first (Smith): at least the flexion tests before any program
            return {"pillar": pillar, "ready": False, "answers": a, "items": [], "notes": [],
                    "missing": [{"id": "flexion", "why": "measure the Leahy test or passive and active flexion first"}]}
        missing = []                      # then build from whatever is measured, and say what is not
        rules = "our shoulder rules from the course notes"
    else:
        return {"pillar": pillar, "ready": False, "missing": [{"id": "pillar", "why": "no program rules for this pillar yet"}],
                "items": [], "notes": []}
    freq = lib["rules"].get("pancake_phase1", {}).get("frequency") if pillar == "pancake" else None
    return {"pillar": pillar, "ready": True, "answers": a, "missing": [], "items": _code_items(items, lib),
            "notes": notes, "rules": rules, "frequency": freq or "1 session a week to start (FAQ: minimum effective dose)",
            "built_on": date.today().isoformat()}


# ---------- store ----------

def save(pillar: str, spec: dict, activate: bool = False) -> dict:
    if not spec.get("items"):
        raise ValueError("no drills in this program")
    ph = db.one("select coalesce(max(phase),0) as n from fitness.mobility_programs where pillar=%s and not deleted "
                "and status in ('active','done')", (pillar,))["n"]
    uid = f"mp_{secrets.token_hex(6)}"
    if activate:
        db.execute("update fitness.mobility_programs set status='done', updated_at=now() where pillar=%s and "
                   "status='active' and not deleted", (pillar,))
    db.execute("insert into fitness.mobility_programs (uid, pillar, phase, status, started, spec) values (%s,%s,%s,%s,%s,%s)",
               (uid, pillar, int(ph) + 1, "active" if activate else "draft", date.today() if activate else None, Json(spec)))
    return {"ok": True, "id": uid}


def log_session(program: str, day: str, items: list, note: str = "") -> dict:
    p = db.one("select uid, pillar from fitness.mobility_programs where uid=%s and not deleted", (program,))
    if not p:
        raise ValueError("no such program")
    clean = []
    for it in items or []:
        sets = []
        for s in (it.get("sets") or [])[:10]:
            row = {k: float(s[k]) for k in ("reps", "hold_s", "kg") if s.get(k) not in (None, "")}
            if s.get("side") in ("L", "R"):
                row["side"] = s["side"]
            if row:
                sets.append(row)
        deeper = it.get("deeper") if it.get("deeper") in ("yes", "no", "same") else None
        if not sets and not it.get("marker") and not deeper:
            continue
        clean.append({"code": str(it.get("code", ""))[:4], "drill_id": str(it.get("drill_id", ""))[:60], "sets": sets,
                      "marker": str(it.get("marker") or "")[:120], "deeper": deeper, "note": str(it.get("note") or "")[:300]})
    if not clean:
        raise ValueError("nothing logged")
    uid = f"ms_{secrets.token_hex(6)}"
    db.execute("insert into fitness.mobility_sessions (uid, program, pillar, day, items, note) values (%s,%s,%s,%s,%s,%s)",
               (uid, program, p["pillar"], date.fromisoformat(day), Json(clean), (note or "")[:300] or None))
    return {"ok": True, "id": uid}


def delete_session(uid: str) -> dict:
    db.execute("update fitness.mobility_sessions set deleted=true where uid=%s", (uid,))
    return {"ok": True}


def discard(uid: str) -> dict:
    db.execute("update fitness.mobility_programs set deleted=true, updated_at=now() where uid=%s and status='draft'", (uid,))
    return {"ok": True}


def start(uid: str) -> dict:
    p = db.one("select pillar from fitness.mobility_programs where uid=%s and not deleted", (uid,))
    if not p:
        raise ValueError("no such program")
    db.execute("update fitness.mobility_programs set status='done', updated_at=now() where pillar=%s and status='active' "
               "and uid<>%s and not deleted", (p["pillar"], uid))
    db.execute("update fitness.mobility_programs set status='active', started=coalesce(started, current_date), "
               "updated_at=now() where uid=%s", (uid,))
    return {"ok": True}


# ---------- progression (lesson 08) ----------

def _base(item: dict) -> dict:
    d = item.get("dose") or {}
    return {"sets": _lo(d.get("sets")) or 2, "reps": _lo(d.get("reps")), "hold": _lo(d.get("hold_s")),
            "cr": bool(d.get("cr"))}


def target(item: dict, history: list) -> dict:
    """Next session's target for one drill. history = this drill's logged entries, oldest first."""
    b = _base(item)
    last = history[-1] if history else None
    if item.get("method") == "strength":
        # strength drills progress by load, not depth (FAQ): reps in the range, all sets at the top -> add weight
        top = _hi((item.get("dose") or {}).get("reps")) or 8
        if not last or not last.get("sets"):
            return {"step": "start", "sets": b["sets"], "reps": b["reps"], "hold_s": None,
                    "text": f"Pick a weight you can do {b['reps'] or 5} good reps with, at the tempo."}
        reps = [s.get("reps") for s in last["sets"] if s.get("reps") is not None]
        kg = max([s.get("kg") or 0 for s in last["sets"]] or [0])
        if reps and min(reps) >= top:
            return {"step": "load", "sets": b["sets"], "reps": b["reps"], "hold_s": None,
                    "text": f"All sets reached {top}: add a little weight{f' (was {kg:g} kg)' if kg else ''} and start again at {b['reps'] or 5} reps."}
        return {"step": "reps", "sets": b["sets"], "reps": (min(reps) + 1) if reps else b["reps"], "hold_s": None,
                "text": f"Same weight{f' ({kg:g} kg)' if kg else ''}: add a rep on each set, up to {top}."}
    if not last:
        return {"step": "start", "sets": b["sets"], "reps": b["reps"], "hold_s": b["hold"],
                "text": "First session: find your depth at the starting dose and log the marker."}
    if last.get("deeper") == "yes":
        return {"step": "deeper", "sets": b["sets"], "reps": b["reps"], "hold_s": b["hold"],
                "text": f"You went deeper last time ({last.get('marker') or 'marker logged'}). Same starting dose, beat that depth."}
    # count the run of sessions without new depth, newest backwards
    k = 0
    for h in reversed(history):
        if h.get("deeper") == "yes":
            break
        k += 1
    reps, hold, sets = b["reps"], b["hold"], b["sets"]
    tut_steps = 0
    if hold:
        tut_steps = max(1, round(hold / 15))           # +15 s per session until the hold has doubled
    elif reps:
        tut_steps = max(1, round(reps / (1 if b["cr"] else 2)))   # +2 reps (CR: +1 round) until doubled
    beat = f" Beat {last.get('marker')}." if last.get("marker") else ""
    if k <= tut_steps:
        if hold:
            hold = b["hold"] + 15 * k
            return {"step": "tut", "sets": sets, "reps": reps, "hold_s": hold,
                    "text": f"No new depth last time: hold {hold} s ({k} of {tut_steps} steps).{beat}"}
        if reps:
            reps = b["reps"] + (1 if b["cr"] else 2) * k
            return {"step": "tut", "sets": sets, "reps": reps, "hold_s": hold,
                    "text": f"No new depth last time: {reps} {'rounds' if b['cr'] else 'reps'} ({k} of {tut_steps} steps).{beat}"}
    extra = k - tut_steps
    if sets + extra <= MAX_SETS:
        full_r = b["reps"] * 2 if b["reps"] and not b["cr"] else (b["reps"] + tut_steps if b["reps"] else None)
        full_h = b["hold"] * 2 if b["hold"] else None
        return {"step": "sets", "sets": sets + extra, "reps": full_r, "hold_s": full_h,
                "text": f"Time under tension maxed: add a set ({sets + extra} sets).{beat}"}
    over = extra - (MAX_SETS - sets)
    if over <= 2:
        return {"step": "recovery", "sets": b["sets"], "reps": b["reps"], "hold_s": b["hold"],
                "text": "Stalled on every step: check recovery (sleep, food, stress, other training), then go back to "
                        "the starting dose and try for depth."}
    return {"step": "run_its_course", "sets": b["sets"], "reps": b["reps"], "hold_s": b["hold"],
            "text": "This drill has run its course: retest and move to the next phase."}


def programs_state() -> dict:
    progs = [{"id": r["uid"], "pillar": r["pillar"], "phase": r["phase"], "status": r["status"],
              "started": r["started"].isoformat() if r["started"] else None, "spec": r["spec"]}
             for r in db.query("select * from fitness.mobility_programs where not deleted and status in ('draft','active') "
                               "order by created_at")]
    ids = [p["id"] for p in progs if p["status"] == "active"]
    sess = [{"id": r["uid"], "program": r["program"], "pillar": r["pillar"], "date": r["day"].isoformat(),
             "items": r["items"], "note": r["note"]}
            for r in (db.query("select * from fitness.mobility_sessions where not deleted and program = any(%s) "
                               "order by day, created_at", (ids,)) if ids else [])]
    for p in progs:
        if p["status"] != "active":
            continue
        mine = [s for s in sess if s["program"] == p["id"]]
        p["sessions_done"] = len(mine)
        p["sessions_per_phase"] = SESSIONS_PER_PHASE
        p["retest_due"] = len(mine) >= SESSIONS_PER_PHASE
        for it in p["spec"].get("items", []):
            hist = [x for s in mine for x in s["items"] if x.get("code") == it["code"] and x.get("drill_id") == it["drill_id"]]
            it["last"] = hist[-1] if hist else None
            it["target"] = target(it, hist)
    return {"programs": progs, "sessions": sess}
