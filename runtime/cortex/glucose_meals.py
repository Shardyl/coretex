"""Meal-by-meal glucose responses (owner, 11 Oct 2026): what each meal did to his CGM, for the Fitness app's Glucose tab.

Everything here is measured from his own data, never estimated:
  * a MEAL is food logged within 30 minutes of the previous item (food_log.eaten_at);
  * BASELINE = median glucose in the 30 minutes before the first bite (else the nearest reading within 15 min);
  * PEAK = highest reading from the first bite to +150 min, or to the next meal if that comes first (min 45 min);
  * RISE = peak - baseline; TIME TO PEAK; BACK = minutes until glucose is back within 5 mg/dL of baseline;
  * colour by RISE (what the food caused): < 30 green, 30-50 amber, > 50 red;
  * context tags: alcohol in the previous 18 h, a workout ending within 3 h before, the meal that broke the fast.
Values in mg/dL (his meter's unit). Food log times are only reliable from 8 Oct 2026 (he logs at the first bite).
"""
from __future__ import annotations

import re
from datetime import timedelta
from statistics import median

from . import db

MG = 18.0182
GAP = timedelta(minutes=30)
WIN = timedelta(minutes=150)
ALCOHOL = re.compile(r"\b(beer|lager|ale|wine|prosecco|champagne|vodka|whisk(?:e)?y|gin|rum|tequila|cocktail|spirit|alcohol)", re.I)


def _rag(rise):
    if rise is None:
        return None
    return "green" if rise < 30 else "amber" if rise <= 50 else "red"


def meals(days: int = 30) -> dict:
    cgm = db.query("select at, mmol from fitness.cgm where at > now() - make_interval(days => %s) order by at", (days + 1,))
    if not cgm:
        return {"meals": [], "leaderboard": []}
    first_cgm = cgm[0]["at"]
    pts = [(r["at"], float(r["mmol"]) * MG) for r in cgm]
    food = db.query("select uid, eaten_at, name, kcal, carbs, protein from fitness.food_log where not deleted "
                    "and eaten_at is not null and eaten_at >= %s order by eaten_at", (first_cgm,))
    sess = db.query("select end_at, coalesce(title, type_name) t from fitness.health_sessions where end_at >= %s "
                    "and extract(epoch from end_at-start_at) >= 600", (first_cgm - timedelta(hours=4),))
    fasts = [r["broke_at"] for r in db.query("select broke_at from fitness.fast_days where broke_at is not null")]
    alc = [(f["eaten_at"], f["name"]) for f in food if ALCOHOL.search(f["name"] or "")]

    groups: list[list[dict]] = []
    for f in food:
        if groups and f["eaten_at"] - groups[-1][-1]["eaten_at"] <= GAP:
            groups[-1].append(f)
        else:
            groups.append([f])

    out = []
    for i, g in enumerate(groups):
        t0 = g[0]["eaten_at"]
        nxt = groups[i + 1][0]["eaten_at"] if i + 1 < len(groups) else None
        end = t0 + WIN
        if nxt and nxt < end:
            end = max(nxt, t0 + timedelta(minutes=45))
        before = [v for t, v in pts if t0 - timedelta(minutes=30) <= t <= t0]
        if not before:
            near = [(abs((t - t0).total_seconds()), v) for t, v in pts if abs((t - t0).total_seconds()) <= 900]
            before = [min(near)[1]] if near else []
        after = [(t, v) for t, v in pts if t0 <= t <= end]
        if not before or not after:
            continue
        base = median(before)
        pt, pv = max(after, key=lambda x: x[1])
        rise = pv - base
        back = None
        for t, v in pts:
            if t > pt and t <= t0 + WIN + timedelta(minutes=60) and v <= base + 5:
                back = round((t - t0).total_seconds() / 60)
                break
        curve = []
        for t, v in pts:
            off = (t - t0).total_seconds() / 60
            if -30 <= off <= 150:
                curve.append([round(off), round(v)])
        curve = curve[::5] if len(curve) > 60 else curve
        tags = []
        if any(timedelta(0) < t0 - t <= timedelta(hours=18) for t, _ in alc) and not any(ALCOHOL.search(x["name"] or "") for x in g):
            tags.append("after alcohol")
        if any(timedelta(0) <= t0 - s["end_at"] <= timedelta(hours=3) and "steam" not in (s["t"] or "").lower() for s in sess):
            tags.append("after training")
        if any(abs((t0 - b).total_seconds()) <= 900 for b in fasts):
            tags.append("breaks the fast")
        if nxt and nxt < t0 + WIN:
            tags.append("next meal within 2.5 h")
        out.append({
            "at": t0.isoformat(), "date": t0.date().isoformat(),
            "foods": [{"id": f["uid"], "name": f["name"], "kcal": f["kcal"] and round(float(f["kcal"])),
                       "carbs": f["carbs"] and round(float(f["carbs"]), 1)} for f in g],
            "carbs": round(sum(float(f["carbs"] or 0) for f in g), 1),
            "kcal": round(sum(float(f["kcal"] or 0) for f in g)),
            "baseline": round(base), "peak": round(pv), "rise": round(rise),
            "toPeak": round((pt - t0).total_seconds() / 60), "back": back,
            "rag": _rag(rise), "tags": tags, "curve": curve,
        })
    out.reverse()

    board: dict[str, dict] = {}
    for m in out:
        for f in m["foods"]:
            k = (f["name"] or "").strip()
            b = board.setdefault(k.lower(), {"name": k, "rises": []})
            b["rises"].append(m["rise"])
    lead = [{"name": b["name"], "meals": len(b["rises"]), "avgRise": round(sum(b["rises"]) / len(b["rises"])),
             "min": min(b["rises"]), "max": max(b["rises"]), "rag": _rag(sum(b["rises"]) / len(b["rises"]))}
            for b in board.values() if len(b["rises"]) >= 2]
    lead.sort(key=lambda x: x["avgRise"])
    return {"meals": out, "leaderboard": lead}
