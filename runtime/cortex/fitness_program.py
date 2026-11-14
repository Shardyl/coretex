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


# ---------- generic rule sets (14 Nov 2026): every other pillar's rules are DATA in the drill library ----------
# A ruleset = metrics (read from results by code), require (what must be measured first), always / rules / fallback
# drills. Conditions are {metric, op, value}; a condition on a metric that is not measured is false. Smith's published
# trees are encoded exactly ("marked": "smith"); the rest are ours from the notes ("marked": "ours"), shown as such.
RULE_KEYS = {"pancake": "pancake_phase1", "shoulder_flexion": "shoulders_phase1"}


def ruleset(lib: dict, pillar: str) -> dict | None:
    rs = (lib.get("rules") or {}).get(pillar)
    return rs if isinstance(rs, dict) and "metrics" in rs else None


def program_pillars(lib: dict) -> list:
    return sorted(set(RULE_KEYS) | {k for k in (lib.get("rules") or {}) if ruleset(lib, k)})


def metrics(rs: dict, res: dict, answers: dict) -> dict:
    out: dict = {}
    for q in rs.get("questions") or []:
        if (answers or {}).get(q) not in (None, ""):
            out[q] = answers[q]
    for m in rs.get("metrics") or []:
        k, v = m.get("kind"), None
        if k == "value":
            v = _val(res, m.get("test"), m.get("measure"))
        elif k == "worst":
            v = _worst(res, m.get("test"), m.get("measure"))
        elif k == "best":
            vs = [x for x in (_val(res, m.get("test"), m.get("measure"), s) for s in ("L", "R", "both")) if _n(x)]
            v = max(vs) if vs else None
        elif k == "label":
            x = _val(res, m.get("test"), m.get("measure"))
            v = str(x).lower() if x is not None else None
        elif k == "worst_label":
            opts = []
            for pl in fm.pillars():
                for t in pl.get("tests") or []:
                    if t["id"] == m.get("test"):
                        opts = next((mm.get("options") or [] for mm in t.get("measures") or []
                                     if mm["key"] == m.get("measure")), opts)
            got = [str(x) for x in (_val(res, m.get("test"), m.get("measure"), sd) for sd in ("L", "R", "both"))
                   if x is not None]
            ranked = sorted(got, key=lambda x: opts.index(x) if x in opts else 999)
            v = ranked[0].lower() if ranked else None
        elif k in ("diff", "absdiff"):
            a, b = out.get(m.get("a")), out.get(m.get("b"))
            if _n(a) and _n(b):
                v = abs(a - b) if k == "absdiff" else a - b
        if v is not None:
            out[m["id"]] = v
    return out


def _holds(c: dict, mv: dict) -> bool:
    if c.get("metric") not in mv:
        return False
    x, op, y = mv[c["metric"]], c.get("op", "=="), c.get("value")
    try:
        if op == "in":
            return x in (y or []) or str(x).lower() in [str(z).lower() for z in (y or [])]
        if op in ("==", "!="):
            eq = x == y or str(x).lower() == str(y).lower()
            return eq if op == "==" else not eq
        x, y = float(x), float(y)
        return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]
    except (TypeError, ValueError, KeyError):
        return False


def build_generic(pillar: str, lib: dict, rs: dict, res: dict, answers: dict) -> dict:
    mv = metrics(rs, res, answers)
    need = [r for r in rs.get("require") or [] if r not in mv]
    if need:
        pl = next((p for p in fm.pillars() if p["id"] == pillar), {})
        tnames = {t["id"]: t["name"] for t in pl.get("tests") or []}
        qnames = {q["id"]: q.get("name") or q["id"] for q in pl.get("questions") or []}
        mtest = {m["id"]: m.get("test") for m in rs.get("metrics") or []}
        why, seen = [], set()
        for r in need:
            txt = ("answer: " + qnames[r]) if r in qnames else ("measure " + tnames.get(mtest.get(r), str(mtest.get(r) or r)
                                                                                         .replace("_", " ")))
            if txt not in seen:
                seen.add(txt)
                why.append({"id": r, "why": txt})
        return {"pillar": pillar, "ready": False, "answers": dict(answers or {}), "items": [], "notes": [], "missing": why}
    by = {d["id"]: d for d in lib["drills"]}
    items, notes, seen = [], [], set()

    def add(x, why):
        if x.get("drill_id") in by and x["drill_id"] not in seen:
            seen.add(x["drill_id"])
            items.append({k: v for k, v in {"code": x.get("code"), "drill_id": x["drill_id"],
                                            "optional": bool(x.get("optional")), "why": why}.items() if v not in (None, "")})
    for x in rs.get("always") or []:
        add(x, x.get("why") or "always in this program")
    for r in rs.get("rules") or []:
        if all(_holds(c, mv) for c in r.get("when") or []):
            for x in r.get("add") or []:
                add(x, r.get("why") or "")
            if r.get("note"):
                notes.append(r["note"])
    if not any(by[i["drill_id"]].get("slot") in ("main", "attribute") for i in items):
        for x in rs.get("fallback") or []:
            add(x, x.get("why") or "default when no weak link stands out")
    if any(i.get("code") for i in items) and not all(i.get("code") for i in items):
        for i in items:
            i.pop("code", None)             # mixed: let the slot order decide every code
    notes += list(rs.get("notes") or [])
    if rs.get("marked") == "ours":
        notes.append("These rules are ours, written from the course notes (Smith's own builder logic is not visible).")
    missing_m = [m["id"] for m in rs.get("metrics") or [] if m["id"] not in mv]
    if missing_m:
        notes.append("Not measured yet, so not used: " + ", ".join(missing_m).replace("_", " ") + ".")
    return {"pillar": pillar, "ready": bool(items), "answers": dict(answers or {}), "missing": [] if items else
            [{"id": "nothing", "why": "no drill fits the results yet"}], "items": _code_items(items, lib),
            "notes": notes, "rules": ("Smith's own builder, " if rs.get("marked") == "smith" else "our rules, ")
            + str(rs.get("source") or ""), "frequency": rs.get("frequency")
            or "1 session a week to start (FAQ: minimum effective dose)", "built_on": date.today().isoformat(),
            "metrics": mv}


def results_for(pillar: str, all_res: dict | None = None) -> dict:
    """This pillar's latest results, plus any test marked same_as another pillar's test (measured once, used twice)."""
    all_res = all_res if all_res is not None else latest()
    res = dict(all_res.get(pillar, {}))
    pl = next((p for p in fm.pillars() if p["id"] == pillar), {})
    for t in pl.get("tests") or []:
        src = t.get("same_as")
        if src and t["id"] not in res and "." in src:
            sp, st = src.split(".", 1)
            if (all_res.get(sp) or {}).get(st):
                res[t["id"]] = all_res[sp][st]
    return res


def build(pillar: str, answers: dict | None = None) -> dict:
    """A DRAFT program from the latest results. Never saved here."""
    lib = fm.drill_library()
    res = results_for(pillar)
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
    elif ruleset(lib, pillar):
        return build_generic(pillar, lib, ruleset(lib, pillar), res, answers or {})
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


def log_session(program: str, day: str, items: list, note: str = "", blocks: list | None = None) -> dict:
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
        wu = it.get("warmup") if isinstance(it.get("warmup"), dict) else {}
        warm = {"sets_done": int(wu.get("sets_done") or 0), "of": int(wu.get("of") or 0),
                "reached": wu.get("reached") if wu.get("reached") in ("yes", "no") else None}
        clean.append({"code": str(it.get("code", ""))[:4], "drill_id": str(it.get("drill_id", ""))[:60], "sets": sets,
                      "marker": str(it.get("marker") or "")[:120], "deeper": deeper, "note": str(it.get("note") or "")[:300],
                      "warmup": warm})
    if not clean:
        raise ValueError("nothing logged")
    uid = f"ms_{secrets.token_hex(6)}"
    blk = [{"id": str(b.get("id", ""))[:40], "picked": [str(x)[:60] for x in (b.get("picked") or [])][:8]}
           for b in (blocks or []) if isinstance(b, dict)]
    if blk:
        clean.append({"code": "_blocks", "drill_id": "", "sets": [], "blocks": blk})
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


# ---------- warm-ups (lesson 06 + Training Cheatsheet, FAQ) ----------
# Every drill is warmed up right before it, same set-up and tempo, intensity rising set by set, to about 85% of last
# session's depth. The first drill of the session gets the full ladder, later drills the last two rungs. Warm-up sets
# never count as program sets. Bridge adds his general spine + wrist warm-up before and a cool down after.
WARMUP_LESSON = {"lesson_slug": "06-fundamentals_warm-up", "lesson_course": "mftk", "name": "How to warm up"}
BLOCKS = {
    "bridge": {
        "before": [
            {"id": "bridge_general", "name": "General spine warm-up", "lesson_slug": "176-bridge-general-warm-up",
             "how": "Pick 1-2, 1-2 sets each, easy, no max range; lift as you fold.",
             "options": ["8 direction spine movement", "Spine circles", "Cat cow"]},
            {"id": "bridge_wrist", "name": "Wrist warm-up", "lesson_slug": "172-bridge_wrist-warm-up",
             "how": "Pick 2-3, a couple of minutes each, loading the wrists progressively.",
             "options": ["Finger extensions", "Wrist extension side to side", "Wrist extension forward and back",
                         "Wrist raises", "Wrist 360 extension", "Wrist circles"]}],
        "after": [
            {"id": "bridge_cooldown", "name": "Cool down (spine reset)", "lesson_slug": "177-bridge-cooldown",
             "how": "Pick 1-2, a few easy reps.", "options": ["Jefferson curl (5-10 kg)", "Round back pike hang", "Hanging"]}]},
}


def warmup(item: dict, first: bool) -> list:
    """The warm-up ladder for one drill, from its type and last session (cheatsheet rows)."""
    meth, d, last = item.get("method"), item.get("dose") or {}, item.get("last") or {}
    if meth == "soft_tissue" or d.get("by_feel"):
        return []
    weighted = bool((item.get("load") or {}).get("uses_weight"))
    if meth == "strength":
        kg = max([s.get("kg") or 0 for s in last.get("sets") or []] or [0])
        f = (lambda x: f"{round(kg * x * 2) / 2:g} kg") if kg else (lambda x: {0.5: "about half", 0.75: "about 3/4",
                                                                               0.9: "about 90%"}[x] + " of working weight")
        rows = [{"label": "Light", "do": f"{f(0.5)} x 5, both sides", "rest": "10-20 s"},
                {"label": "Medium", "do": f"{f(0.75)} x 3", "rest": "10-20 s"},
                {"label": "Heavy", "do": f"{f(0.9)} x 1-2", "rest": "then full rest"}]
    elif d.get("hold_s") and not d.get("reps"):
        rows = [{"label": "Low", "do": "10-20 s hold, easy depth", "rest": "10-20 s"},
                {"label": "Medium/high", "do": "10-20 s hold, deeper", "rest": "30 s"},
                {"label": "High", "do": "3-10 s at or near your working depth", "rest": "then full rest"}]
    else:
        w = lambda t: (", " + t) if weighted else ""
        rows = [{"label": "Low", "do": "3-4 reps, deeper each rep" + w("no weight"), "rest": "10-20 s"}]
        if weighted:
            rows.append({"label": "Medium", "do": "3-4 reps, 50-100% of working weight", "rest": "10-20 s"})
        rows += [{"label": "Medium/high", "do": "2-3 reps" + w("working weight") + ", push depth", "rest": "30 s"},
                 {"label": "High", "do": "1-2 reps, close to last session's depth", "rest": "then full rest"}]
    return rows if first else rows[-2:]


def programs_state() -> dict:
    """Programs, sessions and which pillars can build one."""
    out = _programs_state()
    out["program_pillars"] = program_pillars(fm.drill_library())
    out["warmup_lesson"] = WARMUP_LESSON
    out["blocks"] = BLOCKS
    for p in out["programs"]:
        if p["status"] == "active":
            for k, it in enumerate(p["spec"].get("items", [])):
                it["warmup"] = warmup(it, k == 0 or (k == 1 and p["spec"]["items"][0].get("optional")))
    return out


def _programs_state() -> dict:
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
