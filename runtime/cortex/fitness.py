"""Fitness — server-side home for the training log that used to live only in phone localStorage.

Sync model: the app pushes its whole document (it is small, a few hundred rows) and the server
explodes it into the fitness.* tables and keeps the raw document in fitness.snapshots. Whole-doc
push beats per-row merge here because there is one operator and one device, so there is no real
conflict to resolve, and a snapshot per push means a bad sync is always recoverable.

Rows are upserted by the client's own uid and never hard-deleted, so a push that is missing rows
(a half-restored phone, say) cannot silently wipe history. Deletion is explicit: deleted=true.

Derived columns (kg, volume_load, minutes, m_per_beat) are computed HERE so Cortex can query
training data with plain SQL. The maths mirrors the app exactly; keep the two in step.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from psycopg.types.json import Json

from . import db

# One push writes thousands of rows (the food log alone is 4,000+). A fresh connection per statement
# costs ~11 ms, so a sync took ~60 s and the app gave up at 25 s ("Sync failed", 5 Oct 2026). During a
# push every statement goes through ONE connection instead.
import contextvars
_conn = contextvars.ContextVar("fitness_conn", default=None)


def _ex(sql, params=()):
    c = _conn.get()
    if c is None:
        return db.execute(sql, params)
    cur = c.execute(sql, params)
    return cur.fetchone() if cur.description else None


def _one(sql, params=()):
    c = _conn.get()
    if c is None:
        return db.one(sql, params)
    rows = c.execute(sql, params).fetchall()
    return rows[0] if rows else None


# ---------- parsing helpers (ports of the app's own maths) ----------


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _day(v) -> date | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_duration_min(v) -> float | None:
    """'1:07:36' -> 67.6, '36:29' -> 36.48, '67.5' -> 67.5.

    Two-part values are mm:ss in this log, never hh:mm. Reading '36:29' as 36 hours was a real bug
    in the app once and it broke every duration chart.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if ":" in s:
        p = [_num(x) or 0 for x in s.split(":")]
        if len(p) >= 3:
            return p[0] * 60 + p[1] + p[2] / 60
        return p[0] + p[1] / 60
    return _num(s)


def bodyweight_at(day: date | None, log: list[dict]) -> float | None:
    """The bodyweight that applied ON the session date: the 7-day average of weigh-ins up to and including
    it (owner's call, 5 Oct 2026: one heavy or light morning must not move the score); with no weigh-in that
    week, the latest entry before it. A 6 kg swing still never rewrites historic records.
    Sessions predating the log fall back to the earliest entry, matching the app (index.html bwAt).
    """
    if not day or not log:
        return None
    wk = [float(e["kg"]) for e in log if day - timedelta(days=6) <= e["day"] <= day]
    if wk:
        return round(sum(wk) / len(wk), 1)
    match = None
    for e in log:                                   # arrives sorted ascending
        if e["day"] <= day:
            match = e
        else:
            break
    return float((match or log[0])["kg"])


_BW_RE = re.compile(r"^(?:bw|bodyweight)\s*(?:\+\s*([\d.]+))?$", re.I)


def lift_kg(weight, day: date | None, bw_log: list[dict]) -> float | None:
    """Kg on the bar: a logged number, or the dated bodyweight for a 'BW' / 'BW+10' (belt) lift."""
    raw = "" if weight is None else str(weight).strip()
    m = _BW_RE.match(raw)
    if m:
        bw = bodyweight_at(day, bw_log)
        return None if bw is None else round(bw + (float(m.group(1)) if m.group(1) else 0), 1)
    n = _num(re.sub(r"[^0-9.]", "", raw))
    if n and n > 0:
        return n
    if raw in ("", "—"):
        return bodyweight_at(day, bw_log)
    return None


def volume_load(kg: float | None, total_reps) -> float | None:
    """The lifting PR metric: total reps x kg. Reps alone are not comparable across loads."""
    reps = _num(total_reps) or 0
    if kg is None or not reps:
        return None
    return round(kg * reps, 1)


# ---------- read ----------


def _bw_log() -> list[dict]:
    return db.query("select day, kg from fitness.bodyweight order by day")


def pull() -> dict:
    """Everything, in the shapes the app already uses, so the client needs no translation layer."""
    bw = [{"date": r["day"].isoformat(), "kg": float(r["kg"]), "notes": r["notes"]}
          for r in db.query("select day, kg, notes from fitness.bodyweight order by day")]
    plans = [{"id": r["uid"], "name": r["name"], "exercises": r["exercises"]}
             for r in db.query("select uid, name, exercises from fitness.plans "
                               "where not deleted order by name")]
    lifts = [{"id": r["uid"], "exercise": r["exercise"], "date": r["day"].isoformat(),
              "workoutId": r["plan_uid"], "weight": r["weight"], "sets": r["sets"],
              "totalReps": _num(r["total_reps"]), "bestSet": _num(r["best_set"]),
              "rest": r["rest"], "target": r["target"], "nextTarget": r["next_target"],
              "readings": r["readings"], "notes": r["notes"]}
             for r in db.query("select * from fitness.lift_sessions where not deleted "
                               "order by day, exercise")]
    presets = [{"id": r["uid"], "name": r["name"], "brand": r["brand"], "location": r["location"],
                "machine": r["machine"], "machineNote": r["machine_note"], "isHIIT": r["is_hiit"],
                "targetDuration": r["target_duration"], "manualFields": r["manual_fields"]}
               for r in db.query("select * from fitness.cardio_presets where not deleted order by name")]
    cardio = [{"id": r["uid"], "exerciseId": r["preset_uid"], "exerciseName": r["preset_name"],
               "date": r["day"].isoformat(), "duration": r["duration"], "avgHR": _num(r["avg_hr"]),
               "maxHR": _num(r["max_hr"]), "calories": _num(r["calories"]), "extra": r["extra"],
               "nextTarget": r["next_target"], "notes": r["notes"], "watch": r.get("watch")}
              for r in db.query("select * from fitness.cardio_sessions where not deleted order by day")]
    vo2 = [{"id": r["uid"], "date": r["day"].isoformat(), "value": float(r["value"]),
            "method": r["method"], "notes": r["notes"]}
           for r in db.query("select * from fitness.vo2 where not deleted order by day")]
    tombstones = [r["uid"] for r in db.query(
        "select uid from fitness.lift_sessions where deleted union all "
        "select uid from fitness.cardio_sessions where deleted union all "
        "select uid from fitness.plans where deleted union all "
        "select uid from fitness.cardio_presets where deleted union all "
        "select uid from fitness.vo2 where deleted")]
    foods = [{"id": r["uid"], "name": r["name"], "brand": r["brand"], "barcode": r["barcode"],
              "servingLabel": r["serving_label"], "servingG": _num(r["serving_g"]),
              "kcal100": _num(r["kcal_100g"]), "protein100": _num(r["protein_100g"]),
              "carbs100": _num(r["carbs_100g"]), "fat100": _num(r["fat_100g"]),
              "fibre100": _num(r["fibre_100g"]), "source": r["source"], "sourceId": r["source_id"],
              "favourite": r["favourite"], "units": r.get("units") or []}
             for r in db.query("select * from fitness.foods where not deleted order by name")]
    food_log = [{"id": r["uid"], "date": r["day"].isoformat(), "meal": r["meal"], "foodId": r["food_uid"],
                 "name": r["name"], "qtyG": _num(r["qty_g"]), "kcal": _num(r["kcal"]),
                 "protein": _num(r["protein"]), "carbs": _num(r["carbs"]), "fat": _num(r["fat"]),
                 "notes": r["notes"], "at": r["eaten_at"].isoformat() if r.get("eaten_at") else None,
                 "unitLabel": r.get("unit_label"), "unitCount": _num(r.get("unit_count"))}
                for r in db.query("select * from fitness.food_log where not deleted and day > current_date - 120 "
                                  "order by day, uid")]   # older history stays here; the phone keeps 120 days
    fast_days = [{"date": r["day"].isoformat(), "brokeAt": r["broke_at"].isoformat() if r["broke_at"] else None,
                  "closedAt": r["closed_at"].isoformat() if r["closed_at"] else None,
                  "freeDay": bool(r.get("free_day")),
                  "freeDayAt": r["free_day_at"].isoformat() if r.get("free_day_at") else None}
                 for r in db.query("select * from fitness.fast_days order by day")]
    readings = [{"id": r["uid"], "at": r["at"].isoformat(), "kind": r["kind"], "value": _num(r["value"]),
                 "context": r["context"], "notes": r["notes"]}
                for r in db.query("select * from fitness.readings where not deleted order by at")]
    supplements = [{"id": r["uid"], "date": r["day"].isoformat(), "name": r["name"], "grams": _num(r["grams"])}
                   for r in db.query("select * from fitness.supplements where not deleted order by day")]
    meals = [{"id": r["uid"], "name": r["name"], "items": r["items"] or [], "source": r["source"],
              "favourite": r["favourite"]}
             for r in db.query("select * from fitness.meals where not deleted order by name")]
    tombstones += [r["uid"] for r in db.query(
        "select uid from fitness.foods where deleted union all "
        "select uid from fitness.food_log where deleted union all "
        "select uid from fitness.meals where deleted union all "
        "select uid from fitness.supplements where deleted union all "
        "select uid from fitness.readings where deleted")]
    return {"bodyweight": bw, "plans": plans, "liftSessions": lifts, "cardioPresets": presets,
            "cardioSessions": cardio, "vo2": vo2, "tombstones": tombstones,
            "foods": foods, "foodLog": food_log, "meals": meals, "supplements": supplements, "fastDays": fast_days, "readings": readings,
            "cgm": cgm_since(None), "targets": targets(), "health": health_pull(), "labs": _labs_pull(),
            "counts": {"bodyweight": len(bw), "plans": len(plans), "liftSessions": len(lifts),
                       "cardioPresets": len(presets), "cardioSessions": len(cardio), "vo2": len(vo2),
                       "foods": len(foods), "foodLog": len(food_log), "meals": len(meals)}}


# ---------- targets (daily intake goals; the operator's numbers, edited in the app) ----------

# bmr: Katch-McArdle from his DEXA lean mass (69.6 kg, 6 Apr 2026) = 370 + 21.6 x 69.6. Editable in the app.
DEFAULT_TARGETS = {"kcal": 2350, "protein": 178, "bmr": 1873, "creatine": 10}


def cgm_since(since: str | None) -> list:
    """CGM points for the app: the last 3 days, or only those after `since` (the app's light 2-minute refresh)."""
    if since:
        rows = db.query("select at, mmol, trend from fitness.cgm where at > %s::timestamptz order by at limit 3000", (since,))
    else:
        rows = db.query("select at, mmol, trend from fitness.cgm where at > now() - interval '3 days' order by at")
    return [{"at": r["at"].isoformat(), "mmol": float(r["mmol"]), "trend": r["trend"]} for r in rows]


def _labs_pull() -> dict:
    from . import labs
    return labs.pull()


def targets() -> dict:
    t = db.setting_get("fitness_targets") or {}
    return {**DEFAULT_TARGETS, **{k: v for k, v in t.items() if v is not None}}


def set_targets(t: dict) -> dict:
    clean = {}
    for k in ("kcal", "protein", "carbs", "fat", "bmr", "creatine"):
        v = _num((t or {}).get(k))
        if v is not None and v > 0:
            clean[k] = round(v)
    m = (t or {}).get("mission")
    if isinstance(m, dict):                 # the goal strip: numbers + ISO dates, set by the owner in the app
        mc = {k: _num(m.get(k)) for k in ("goalKg", "goalFat", "milestoneKg", "milestoneFat", "startKg", "leanKg")}
        mc.update({k: str(m.get(k))[:10] for k in ("goalDate", "startDate") if m.get(k)})
        clean["mission"] = {k: v for k, v in mc.items() if v is not None}
    if clean:
        db.setting_set("fitness_targets", {**(db.setting_get("fitness_targets") or {}), **clean})
    return targets()


# ---------- Health Connect (watch + phone data from the companion app) ----------


def _wake_day(r: dict) -> str:
    """The local (Dubai) day the night ENDED, from Eight Sleep's presenceEnd; its own `day` label as fallback.
    Health Connect sleep is keyed the same way, so 'last night' on a day page means the same night for both."""
    end = _ts((r.get("raw") or {}).get("presenceEnd"))
    if end and end.tzinfo:
        from zoneinfo import ZoneInfo
        return end.astimezone(ZoneInfo("Asia/Dubai")).date().isoformat()
    return r["day"].isoformat()


def health_pull(days: int = 400) -> dict:
    """What the app shows: daily totals, watch sessions and weigh-ins for the last `days` days."""
    d = [{"date": r["day"].isoformat(), "steps": r["steps"], "totalKcal": _num(r["total_kcal"]),
          "activeKcal": _num(r["active_kcal"]), "distanceM": _num(r["distance_m"]),
          "floors": _num(r["floors"]), "restingHR": _num(r["resting_hr"]), "hrvMs": _num(r["hrv_ms"]),
          "sleepMin": _num(r["sleep_min"]), "sleepDeepMin": _num(r["sleep_deep_min"]),
          "sleepRemMin": _num(r["sleep_rem_min"]), "sleepLightMin": _num(r["sleep_light_min"]),
          "sleepAwakeMin": _num(r["sleep_awake_min"]),
          "sleepStart": r["sleep_start"].isoformat() if r["sleep_start"] else None,
          "sleepEnd": r["sleep_end"].isoformat() if r["sleep_end"] else None,
          "sleepHRAvg": _num(r["sleep_hr_avg"]), "sleepHRMin": _num(r["sleep_hr_min"]),
          "sleepResp": _num(r["sleep_resp_avg"]), "sleepHrvMs": _num(r["sleep_hrv_ms"]),
          "sleepSource": r["sleep_source"], "dayRestingHR": _num(r["day_resting_hr"]),
          "dayHrSamples": r["day_hr_samples"], "dayHrGapS": _num(r["day_hr_gap_s"])}
         for r in db.query("select * from fitness.health_daily where day > current_date - %s order by day",
                           (days,))]
    e8 = [{"date": _wake_day(r), "label": r["day"].isoformat(), "score": _num(r["score"]), "fitness": _num(r["fitness"]),
           "durationMin": round(float(r["duration_s"]) / 60) if r["duration_s"] is not None else None,
           "deepMin": round(float(r["deep_s"]) / 60) if r["deep_s"] is not None else None,
           "remMin": round(float(r["rem_s"]) / 60) if r["rem_s"] is not None else None,
           "lightMin": round(float(r["light_s"]) / 60) if r["light_s"] is not None else None,
           "hr": _num(r["hr"]), "hrv": _num(r["hrv"]), "resp": _num(r["resp"])}
          for r in db.query("select * from fitness.sleep_8 where day > current_date - %s order by day", (days,))]
    s = [{"id": r["uid"], "date": r["day"].isoformat(), "start": r["start_at"].isoformat(),
          "end": r["end_at"].isoformat(), "type": r["type"], "typeName": r["type_name"], "title": r["title"],
          "kcal": _num(r["kcal"]), "avgHR": _num(r["avg_hr"]), "maxHR": _num(r["max_hr"]),
          "minHR": _num(r["min_hr"]), "distanceM": _num(r["distance_m"]), "steps": _num(r["steps"]),
          "minutes": round((r["end_at"] - r["start_at"]).total_seconds() / 60, 1), "source": r["source"]}
         for r in db.query("select * from fitness.health_sessions where day > current_date - %s "
                           "order by start_at", (days,))]
    w = [{"at": r["at"].isoformat(), "date": r["at"].date().isoformat(), "kg": _num(r["kg"]),
          "bodyFatPct": _num(r["body_fat_pct"])}
         for r in db.query("select * from fitness.health_weights where at > now() - make_interval(days => %s) "
                           "order by at", (days,))]
    last = db.one("select max(updated_at) as t from fitness.health_daily")
    return {"days": d, "sessions": s, "weights": w, "eightSleep": e8,
            "lastSync": last["t"].isoformat() if last and last.get("t") else None}


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


_SLEEP_COLS = ("sleep_deep_min", "sleep_rem_min", "sleep_light_min", "sleep_awake_min", "sleep_start", "sleep_end",
               "sleep_hr_avg", "sleep_hr_min", "sleep_resp_avg", "sleep_hrv_ms", "sleep_source", "day_resting_hr",
               "day_hr_samples", "day_hr_gap_s")
# Sleep columns coalesce: a bridge older than 1.3.0 does not send them and must not blank them.
_HEALTH_DAILY_UPSERT = (
    "insert into fitness.health_daily (day, steps, total_kcal, active_kcal, distance_m, floors, resting_hr, hrv_ms, "
    f"sleep_min, device, {', '.join(_SLEEP_COLS)}) values ({','.join(['%s'] * (10 + len(_SLEEP_COLS)))}) "
    "on conflict (day) do update set steps=excluded.steps, total_kcal=excluded.total_kcal, "
    "active_kcal=excluded.active_kcal, distance_m=excluded.distance_m, floors=excluded.floors, "
    "resting_hr=excluded.resting_hr, hrv_ms=excluded.hrv_ms, sleep_min=excluded.sleep_min, device=excluded.device, "
    + "".join(f"{c}=coalesce(excluded.{c}, fitness.health_daily.{c}), " for c in _SLEEP_COLS) + "updated_at=now()")


def _sleep_vals(r: dict) -> list:
    """Bridge 1.3.0 sleep detail + daytime resting HR, in the column order of the health_daily upsert."""
    v = [_num(r.get(k)) for k in ("sleep_deep_min", "sleep_rem_min", "sleep_light_min", "sleep_awake_min")]
    v += [_ts(r.get("sleep_start")), _ts(r.get("sleep_end"))]
    v += [_num(r.get(k)) for k in ("sleep_hr_avg", "sleep_hr_min", "sleep_resp_avg", "sleep_hrv_ms")]
    v += [(r.get("sleep_source") or "")[:120] or None, _num(r.get("day_resting_hr"))]
    n = _num(r.get("day_hr_samples"))
    v += [int(n) if n is not None else None, _num(r.get("day_hr_gap_s"))]
    return v


def ingest_health(doc: dict) -> dict:
    """Upsert what the companion read from Health Connect. Values are stored exactly as sent:
    a null stays null (no data that day), never a guessed zero. Re-sending a window is the normal
    case (it re-reads the last 7 days every hour), so every write is an idempotent upsert."""
    dev = f"{doc.get('device') or ''} v{doc.get('app_version') or '?'}"[:80]
    n = {"days": 0, "sessions": 0, "weights": 0}
    for r in doc.get("days") or []:
        d = _day(r.get("day"))
        if not d:
            continue
        steps = _num(r.get("steps"))
        if steps is None and _num(r.get("active_kcal")) is None and _num(r.get("distance_m")) is None:
            # A total with no movement data behind it is Health Connect's default basal estimate
            # (1,564.5 kcal on the S24, every day), not a measurement. Never store it as one.
            r = {**r, "total_kcal": None}
        db.execute(
            _HEALTH_DAILY_UPSERT,
            (d, int(steps) if steps is not None else None, _num(r.get("total_kcal")),
             _num(r.get("active_kcal")), _num(r.get("distance_m")), _num(r.get("floors")),
             _num(r.get("resting_hr")), _num(r.get("hrv_ms")), _num(r.get("sleep_min")), dev,
             *_sleep_vals(r)))
        n["days"] += 1
    for r in doc.get("sessions") or []:
        st, en = _ts(r.get("start")), _ts(r.get("end"))
        if not r.get("id") or not st or not en:
            continue
        db.execute(
            "insert into fitness.health_sessions (uid, start_at, end_at, day, type, type_name, title, kcal, "
            "avg_hr, max_hr, min_hr, distance_m, steps, source) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set start_at=excluded.start_at, end_at=excluded.end_at, "
            "day=excluded.day, type=excluded.type, type_name=excluded.type_name, title=excluded.title, "
            "kcal=excluded.kcal, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, min_hr=excluded.min_hr, "
            "distance_m=excluded.distance_m, steps=excluded.steps, source=excluded.source, updated_at=now()",
            (str(r["id"])[:200], st, en, st.date(), r.get("type"), (r.get("type_name") or "")[:80] or None,
             (r.get("title") or "")[:200] or None, _num(r.get("kcal")), _num(r.get("avg_hr")),
             _num(r.get("max_hr")), _num(r.get("min_hr")), _num(r.get("distance_m")), _num(r.get("steps")),
             (r.get("source") or "")[:120] or None))
        n["sessions"] += 1
    for r in doc.get("weights") or []:
        at, kg = _ts(r.get("time")), _num(r.get("kg"))
        if not at or not kg:
            continue
        db.execute("insert into fitness.health_weights (at, kg, body_fat_pct, source) values (%s,%s,%s,%s) "
                   "on conflict (at) do update set kg=excluded.kg, body_fat_pct=excluded.body_fat_pct, "
                   "source=excluded.source, updated_at=now()",
                   (at, kg, _num(r.get("body_fat_pct")), (r.get("source") or "")[:120] or None))
        n["weights"] += 1
    try:                                     # new watch workouts -> cardio log entries (best effort)
        n["cardio"] = reconcile_watch(since=date.today() - timedelta(days=10))
    except Exception as e:  # noqa: BLE001
        n["cardio_error"] = str(e)[:200]
    return {"ok": True, "counts": n}


# ---------- food lookup (Open Food Facts + USDA FoodData Central) ----------
# Both are free. OFF is crowd-sourced with barcodes and good UAE/UK packaged coverage; USDA is the
# reliable source for plain foods (chicken, rice, eggs). Every figure is per 100 g, read from the
# source; nothing is estimated. Results are cached in-process because the same foods repeat.

_OFF_FIELDS = "code,product_name,brands,nutriments,serving_size,serving_quantity"
_UA = {"User-Agent": "CortexFitness/1.0 (rashadalsafar@gmail.com)"}
_food_cache: dict = {}


def _off_item(p: dict) -> dict | None:
    nm = p.get("nutriments") or {}
    kcal = _num(nm.get("energy-kcal_100g"))
    if kcal is None and _num(nm.get("energy_100g")) is not None:
        kcal = round(_num(nm.get("energy_100g")) / 4.184, 1)      # kJ on the label -> kcal
    name = (p.get("product_name") or "").strip()
    if not name or kcal is None:
        return None
    br = p.get("brands")
    br = (br[0] if br else "") if isinstance(br, list) else (br or "").split(",")[0]
    return {"name": name, "brand": str(br).strip() or None,
            "barcode": p.get("code"), "kcal100": kcal, "protein100": _num(nm.get("proteins_100g")),
            "carbs100": _num(nm.get("carbohydrates_100g")), "fat100": _num(nm.get("fat_100g")),
            "fibre100": _num(nm.get("fiber_100g")), "servingLabel": p.get("serving_size"),
            "servingG": _num(p.get("serving_quantity")),
            "units": ([{"label": _unit_label(str(p.get("serving_size"))) or "serving", "g": _num(p.get("serving_quantity"))}]
                      if _num(p.get("serving_quantity")) else []),
            "source": "off", "sourceId": p.get("code")}


def _usda_item(f: dict) -> dict | None:
    by = {}
    for n in f.get("foodNutrients") or []:
        num = str(n.get("nutrientNumber") or "")
        unit = (n.get("unitName") or "").upper()
        if num == "208" or (num == "" and n.get("nutrientName") == "Energy" and unit == "KCAL"):
            by["kcal"] = n.get("value")
        elif num == "203":
            by["protein"] = n.get("value")
        elif num == "205":
            by["carbs"] = n.get("value")
        elif num == "204":
            by["fat"] = n.get("value")
        elif num == "291":
            by["fibre"] = n.get("value")
    if by.get("kcal") is None:
        return None
    return {"name": (f.get("description") or "").strip().capitalize(), "brand": f.get("brandOwner"),
            "barcode": f.get("gtinUpc"), "kcal100": _num(by.get("kcal")), "protein100": _num(by.get("protein")),
            "carbs100": _num(by.get("carbs")), "fat100": _num(by.get("fat")), "fibre100": _num(by.get("fibre")),
            "servingLabel": None, "servingG": None, "units": _usda_units(f),
            "source": "usda", "sourceId": str(f.get("fdcId"))}


_SKIP_UNIT = re.compile(r"guideline amount|quantity not specified|school container", re.I)


def _unit_label(text: str) -> str:
    """'1 egg' -> 'egg', '1 cup' -> 'cup'; keeps anything that is not a plain '1 x' prefix."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    return re.sub(r"^1\s+", "", t)[:40]


def _usda_units(f: dict) -> list:
    out, seen = [], set()
    for m in f.get("foodMeasures") or []:
        txt, g = m.get("disseminationText") or "", _num(m.get("gramWeight"))
        if not txt or not g or g <= 0 or _SKIP_UNIT.search(txt):
            continue
        lab = _unit_label(txt)
        if lab.lower() in seen:
            continue
        seen.add(lab.lower())
        out.append({"label": lab, "g": round(g, 1)})
    return out[:6]


def food_search(q: str) -> dict:
    import httpx
    from . import config
    q = (q or "").strip()[:80]
    if len(q) < 2:
        return {"items": []}
    key = ("s", q.lower())
    if key in _food_cache:
        return _food_cache[key]
    items, errors = [], []
    try:
        r = httpx.get("https://api.nal.usda.gov/fdc/v1/foods/search", timeout=12, params={
            "api_key": config.get("USDA_API_KEY") or "DEMO_KEY", "query": q, "pageSize": 12,
            "dataType": "Foundation,SR Legacy,Survey (FNDDS)"})
        r.raise_for_status()
        items += [i for i in (_usda_item(f) for f in r.json().get("foods") or []) if i]
    except Exception as e:  # noqa: BLE001
        errors.append(f"usda: {e.__class__.__name__}")
    try:
        # search-a-licious: the legacy cgi/search.pl answers 503 to server traffic
        r = httpx.get("https://search.openfoodfacts.org/search", headers=_UA, timeout=12, params={
            "q": q, "page_size": 15, "fields": _OFF_FIELDS})
        r.raise_for_status()
        items += [i for i in (_off_item(p) for p in r.json().get("hits") or []) if i]
    except Exception as e:  # noqa: BLE001
        errors.append(f"off: {e.__class__.__name__}")
    out = {"items": items, "errors": errors}
    if items:
        _food_cache[key] = out
    return out


def food_barcode(code: str) -> dict:
    import httpx
    code = re.sub(r"\D", "", code or "")[:20]
    if not code:
        return {"item": None}
    key = ("b", code)
    if key in _food_cache:
        return _food_cache[key]
    try:
        r = httpx.get(f"https://world.openfoodfacts.org/api/v2/product/{code}.json", headers=_UA,
                      timeout=12, params={"fields": _OFF_FIELDS})
        p = r.json().get("product") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return {"item": None, "error": "lookup failed"}
    out = {"item": _off_item(p) if p else None}
    if out["item"]:
        _food_cache[key] = out
    return out


# ---------- write ----------


def _lift_uid(row: dict) -> str:
    """Stable id for rows that predate client ids: one exercise on one day is one session."""
    return str(row.get("id") or f"{row.get('exercise', '')}|{str(row.get('date'))[:10]}")


def _units_json(units):
    """A food's household units [{label, g}] as sent by the app; None (keep the server's) when not sent."""
    if not isinstance(units, list):
        return None
    from psycopg.types.json import Json
    clean = [{"label": str(u.get("label"))[:40], "g": round(float(u["g"]), 1)} for u in units
             if isinstance(u, dict) and u.get("label") and _num(u.get("g")) and float(u["g"]) > 0]
    return Json(clean[:12])


def _push_inner(doc: dict, source: str = "app") -> dict:
    # Safety net (5 Oct 2026): a device that lost its storage tombstoned every food and meal. A real
    # user never deletes more than a few rows between syncs, so a push carrying more than 20 bare
    # tombstones for one table has them stripped and the rest of the push applied.
    for k in ("foods", "foodLog", "meals", "liftSessions", "cardioSessions", "plans", "cardioPresets",
              "supplements", "readings"):
        rows = doc.get(k) or []
        tomb = [r for r in rows if isinstance(r, dict) and r.get("deleted") and len(r) <= 2]
        if len(tomb) > 20:
            print(f"[fitness] push: ignored {len(tomb)} tombstones for {k} (mass delete refused)", flush=True)
            doc = {**doc, k: [r for r in rows if r not in tomb]}
    """Upsert a whole client document. Additive by design: rows absent from the doc are left alone."""
    counts = {k: 0 for k in ("bodyweight", "plans", "liftSessions", "cardioPresets",
                             "cardioSessions", "vo2")}

    for r in doc.get("bodyweight") or []:
        d = _day(r.get("date") or r.get("day"))
        kg = _num(r.get("kg"))
        if not d or kg is None:
            continue
        _ex("insert into fitness.bodyweight (day, kg, notes) values (%s,%s,%s) "
                   "on conflict (day) do update set kg=excluded.kg, notes=excluded.notes, "
                   "updated_at=now()", (d, kg, r.get("notes")))
        counts["bodyweight"] += 1

    for r in doc.get("plans") or doc.get("liftWorkouts") or []:
        if not r.get("id"):
            continue
        _ex("insert into fitness.plans (uid, name, exercises, deleted) values (%s,%s,%s,%s) "
                   "on conflict (uid) do update set name=excluded.name, exercises=excluded.exercises, "
                   "deleted=plans.deleted or excluded.deleted, updated_at=now()",
                   (r["id"], r.get("name") or "", Json(r.get("exercises") or []),
                    bool(r.get("deleted"))))
        counts["plans"] += 1

    bw_log = _bw_log()                              # read after the bodyweight upserts, so loads score now
    for r in doc.get("liftSessions") or []:
        d = _day(r.get("date"))
        if not d or not r.get("exercise"):
            continue
        kg = lift_kg(r.get("weight"), d, bw_log)
        _ex(
            "insert into fitness.lift_sessions (uid, exercise, day, plan_uid, weight, kg, sets, "
            "total_reps, best_set, volume_load, rest, target, next_target, readings, notes, deleted) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set exercise=excluded.exercise, day=excluded.day, "
            "plan_uid=excluded.plan_uid, weight=excluded.weight, kg=excluded.kg, sets=excluded.sets, "
            "total_reps=excluded.total_reps, best_set=excluded.best_set, "
            "volume_load=excluded.volume_load, rest=excluded.rest, target=excluded.target, "
            "next_target=excluded.next_target, readings=excluded.readings, notes=excluded.notes, "
            "deleted=lift_sessions.deleted or excluded.deleted, updated_at=now()",
            (_lift_uid(r), r["exercise"], d, r.get("workoutId"), r.get("weight"), kg,
             Json(r.get("sets") or []), _num(r.get("totalReps")), _num(r.get("bestSet")),
             volume_load(kg, r.get("totalReps")), r.get("rest"), r.get("target"),
             Json(r.get("nextTarget")) if r.get("nextTarget") is not None else None,
             Json(r.get("readings")) if r.get("readings") is not None else None,
             r.get("notes"), bool(r.get("deleted"))))
        counts["liftSessions"] += 1

    fields_by_preset = {}
    for r in doc.get("cardioPresets") or doc.get("cardioExercises") or []:
        if not r.get("id"):
            continue
        fields = r.get("manualFields") or []
        fields_by_preset[r["id"]] = fields
        _ex(
            "insert into fitness.cardio_presets (uid, name, brand, location, machine, machine_note, "
            "is_hiit, target_duration, manual_fields, deleted) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set name=excluded.name, brand=excluded.brand, "
            "location=excluded.location, machine=excluded.machine, machine_note=excluded.machine_note, "
            "is_hiit=excluded.is_hiit, target_duration=excluded.target_duration, "
            "manual_fields=excluded.manual_fields, deleted=cardio_presets.deleted or excluded.deleted, updated_at=now()",
            (r["id"], r.get("name") or "", r.get("brand"), r.get("location"), r.get("machine"),
             r.get("machineNote"), bool(r.get("isHIIT")), r.get("targetDuration"),
             Json(fields), bool(r.get("deleted"))))
        counts["cardioPresets"] += 1

    for r in doc.get("cardioSessions") or []:
        d = _day(r.get("date"))
        if not d or not r.get("id"):
            continue
        mins = parse_duration_min(r.get("duration"))
        extra = r.get("extra") or {}
        # extra{} is keyed by the preset's manualFields INDEX, so a distance value only has meaning
        # alongside the preset that names the labels.
        fields = fields_by_preset.get(r.get("exerciseId"))
        if fields is None:
            row = _one("select manual_fields from fitness.cardio_presets where uid=%s",
                         (r.get("exerciseId"),))
            fields = (row or {}).get("manual_fields") or []
        dist = None
        for i, f in enumerate(fields):
            if re.search(r"distance", str(f.get("label", "")), re.I):
                dist = _num(extra.get(str(i)))
        avg_hr = _num(r.get("avgHR"))
        # m/beat = metres per heartbeat, the aerobic efficiency metric.
        mpb = round(dist * 1000 / (avg_hr * mins), 3) if (dist and avg_hr and mins) else None
        _ex(
            "insert into fitness.cardio_sessions (uid, preset_uid, preset_name, day, duration, "
            "minutes, avg_hr, max_hr, calories, distance_km, m_per_beat, extra, next_target, notes, "
            "deleted, watch) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set preset_uid=excluded.preset_uid, "
            "preset_name=excluded.preset_name, day=excluded.day, duration=excluded.duration, "
            "minutes=excluded.minutes, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, "
            "calories=excluded.calories, distance_km=excluded.distance_km, "
            "m_per_beat=excluded.m_per_beat, extra=excluded.extra, next_target=excluded.next_target, "
            "notes=excluded.notes, deleted=cardio_sessions.deleted or excluded.deleted, "
            "watch=coalesce(excluded.watch, cardio_sessions.watch), updated_at=now()",
            (r["id"], r.get("exerciseId"), r.get("exerciseName"), d, r.get("duration"), mins,
             avg_hr, _num(r.get("maxHR")), _num(r.get("calories")), dist, mpb, Json(extra),
             Json(r.get("nextTarget")) if r.get("nextTarget") is not None else None,
             r.get("notes"), bool(r.get("deleted")),
             Json(r["watch"]) if isinstance(r.get("watch"), dict) else None))
        counts["cardioSessions"] += 1

    for r in doc.get("vo2") or doc.get("vo2Records") or []:
        d = _day(r.get("date"))
        val = _num(r.get("value") if r.get("value") is not None else r.get("vo2"))
        if not d or val is None:
            continue
        _ex("insert into fitness.vo2 (uid, day, value, method, notes) values (%s,%s,%s,%s,%s) "
                   "on conflict (uid) do update set day=excluded.day, value=excluded.value, "
                   "method=excluded.method, notes=excluded.notes, updated_at=now()",
                   (str(r.get("id") or f"vo2|{d.isoformat()}"), d, val, r.get("method"),
                    r.get("notes")))
        counts["vo2"] += 1

    counts["foods"] = counts["foodLog"] = 0
    for r in doc.get("foods") or []:
        if r.get("id") and r.get("deleted") and not r.get("name"):   # bare tombstone from the client
            _ex("update fitness.foods set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        if not r.get("id") or not (r.get("name") or "").strip():
            continue
        _ex(
            "insert into fitness.foods (uid, name, brand, barcode, serving_label, serving_g, kcal_100g, "
            "protein_100g, carbs_100g, fat_100g, fibre_100g, source, source_id, favourite, deleted, units) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set name=excluded.name, brand=excluded.brand, barcode=excluded.barcode, "
            "serving_label=excluded.serving_label, serving_g=excluded.serving_g, kcal_100g=excluded.kcal_100g, "
            "protein_100g=excluded.protein_100g, carbs_100g=excluded.carbs_100g, fat_100g=excluded.fat_100g, "
            "fibre_100g=excluded.fibre_100g, source=excluded.source, source_id=excluded.source_id, "
            "favourite=excluded.favourite, deleted=foods.deleted or excluded.deleted, "
            "units=coalesce(excluded.units, foods.units), updated_at=now()",
            (r["id"], r["name"].strip(), r.get("brand"), r.get("barcode"), r.get("servingLabel"),
             _num(r.get("servingG")), _num(r.get("kcal100")), _num(r.get("protein100")),
             _num(r.get("carbs100")), _num(r.get("fat100")), _num(r.get("fibre100")), r.get("source"),
             r.get("sourceId"), bool(r.get("favourite")), bool(r.get("deleted")), _units_json(r.get("units"))))
        counts["foods"] += 1
    for r in doc.get("foodLog") or []:
        # A tombstone is {id, deleted} with NO date, so it must be handled before the date check.
        # (Until 5 Oct 2026 it came after, and every food-entry delete was silently dropped.)
        if r.get("id") and r.get("deleted") and not r.get("name"):
            _ex("update fitness.food_log set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        d = _day(r.get("date"))
        if not r.get("id") or not d:
            continue
        _ex(
            "insert into fitness.food_log (uid, day, meal, food_uid, name, qty_g, kcal, protein, carbs, fat, "
            "notes, deleted, eaten_at, unit_label, unit_count) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set day=excluded.day, meal=excluded.meal, food_uid=excluded.food_uid, "
            "name=excluded.name, qty_g=excluded.qty_g, kcal=excluded.kcal, protein=excluded.protein, "
            "carbs=excluded.carbs, fat=excluded.fat, notes=excluded.notes, "
            "deleted=food_log.deleted or excluded.deleted, "
            "eaten_at=coalesce(excluded.eaten_at, food_log.eaten_at), unit_label=excluded.unit_label, "
            "unit_count=excluded.unit_count, updated_at=now()",
            (r["id"], d, r.get("meal"), r.get("foodId"), (r.get("name") or "").strip() or "Food",
             _num(r.get("qtyG")), _num(r.get("kcal")), _num(r.get("protein")), _num(r.get("carbs")),
             _num(r.get("fat")), r.get("notes"), bool(r.get("deleted")), _ts(r.get("at")),
             (str(r.get("unitLabel"))[:40] if r.get("unitLabel") else None), _num(r.get("unitCount"))))
        counts["foodLog"] += 1
    counts["meals"] = 0
    for r in doc.get("meals") or []:
        if not r.get("id"):
            continue
        if r.get("deleted") and not r.get("name"):
            _ex("update fitness.meals set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        if not (r.get("name") or "").strip():
            continue
        _ex(
            "insert into fitness.meals (uid, name, items, source, favourite, deleted) values (%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set name=excluded.name, items=excluded.items, source=excluded.source, "
            "favourite=excluded.favourite, deleted=meals.deleted or excluded.deleted, updated_at=now()",
            (r["id"], r["name"].strip(), Json(r.get("items") or []), r.get("source") or "app",
             bool(r.get("favourite")), bool(r.get("deleted"))))
        counts["meals"] += 1
    counts["fastDays"] = counts["readings"] = 0
    for r in doc.get("fastDays") or []:
        d = _day(r.get("date"))
        if not d:
            continue
        # An empty time from a device never wipes a time the server has (a stale or reset phone would
        # otherwise erase it, 5 Oct 2026); only a deliberate Undo/Reopen, sent as `cleared`, does.
        clr = set(r.get("cleared") or [])
        # free day: only a toggle NEWER than the server's (freeDayAt) changes it, so a stale device never undoes it
        fat = _ts(r.get("freeDayAt"))
        _ex("insert into fitness.fast_days (day, broke_at, closed_at, free_day, free_day_at) values (%s,%s,%s,%s,%s) "
            "on conflict (day) do update "
            "set broke_at=case when %s then excluded.broke_at else coalesce(excluded.broke_at, fast_days.broke_at) end, "
            "closed_at=case when %s then excluded.closed_at else coalesce(excluded.closed_at, fast_days.closed_at) end, "
            "free_day=case when excluded.free_day_at is not null and (fast_days.free_day_at is null or "
            "excluded.free_day_at > fast_days.free_day_at) then excluded.free_day else fast_days.free_day end, "
            "free_day_at=greatest(excluded.free_day_at, fast_days.free_day_at), updated_at=now()",
            (d, _ts(r.get("brokeAt")), _ts(r.get("closedAt")), bool(r.get("freeDay")) if fat else None, fat,
             "brokeAt" in clr, "closedAt" in clr))
        counts["fastDays"] += 1
    for r in doc.get("readings") or []:
        if not r.get("id"):
            continue
        at = _ts(r.get("at"))
        if r.get("deleted") and not at:
            _ex("update fitness.readings set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        if not at or _num(r.get("value")) is None or r.get("kind") not in ("ketones", "glucose"):
            continue
        _ex("insert into fitness.readings (uid, at, kind, value, context, notes, deleted) values (%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set at=excluded.at, kind=excluded.kind, value=excluded.value, "
            "context=excluded.context, notes=excluded.notes, deleted=readings.deleted or excluded.deleted, updated_at=now()",
            (r["id"], at, r["kind"], _num(r["value"]), r.get("context"), r.get("notes"), bool(r.get("deleted"))))
        counts["readings"] += 1
    counts["supplements"] = 0
    for r in doc.get("supplements") or []:
        d = _day(r.get("date"))
        if not r.get("id"):
            continue
        if r.get("deleted") and not d:
            _ex("update fitness.supplements set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        if not d:
            continue
        _ex("insert into fitness.supplements (uid, day, name, grams, deleted) values (%s,%s,%s,%s,%s) "
                   "on conflict (uid) do update set day=excluded.day, name=excluded.name, grams=excluded.grams, "
                   "deleted=supplements.deleted or excluded.deleted, updated_at=now()",
                   (r["id"], d, (r.get("name") or "creatine")[:40], _num(r.get("grams")), bool(r.get("deleted"))))
        counts["supplements"] += 1
    if doc.get("targets"):
        set_targets(doc["targets"])

    total = sum(counts.values())
    # The app pushes whenever it regains focus, so most pushes are byte-identical to the last one.
    # Storing those would grow the nightly dump for nothing. Only a document that actually differs
    # is worth keeping — and every distinct version is still kept, because this is the restore path.
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    prev = _one("select doc_hash from fitness.snapshots order by id desc limit 1")
    if not prev or prev.get("doc_hash") != digest:
        _ex("insert into fitness.snapshots (source, rows, doc, doc_hash) values (%s,%s,%s,%s)",
                   (source, total, Json(doc), digest))
    return {"ok": True, "written": counts, "total": total}



def push(doc: dict, source: str = "app") -> dict:
    """Upsert a whole client document over ONE connection (see _ex)."""
    with db.connect() as conn:
        tok = _conn.set(conn)
        try:
            return _push_inner(doc, source)
        finally:
            _conn.reset(tok)

def rescore_bodyweight_lifts() -> int:
    """Recompute kg/volume_load for every BW lift after the bodyweight log changes.

    Logging a weight for a past date retro-scores the sessions it covers, which is the entire point
    of the dated log. Only rows whose logged weight is not a number are touched.
    """
    bw_log = _bw_log()
    rows = db.query("select uid, day, weight, total_reps from fitness.lift_sessions where not deleted")
    n = 0
    for r in rows:
        raw = "" if r["weight"] is None else str(r["weight"]).strip()
        if not _BW_RE.match(raw) and _num(re.sub(r"[^0-9.]", "", raw)):
            continue                                # numeric load, nothing to resolve
        kg = lift_kg(r["weight"], r["day"], bw_log)
        db.execute("update fitness.lift_sessions set kg=%s, volume_load=%s, updated_at=now() "
                   "where uid=%s", (kg, volume_load(kg, r["total_reps"]), r["uid"]))
        n += 1
    return n


def summary() -> dict:
    """Compact training picture for Cortex: recent volume, streak, PRs, aerobic drift."""
    return {
        "last_lift": db.one("select day, exercise, volume_load from fitness.lift_sessions "
                            "where not deleted order by day desc limit 1"),
        "last_cardio": db.one("select day, preset_name, avg_hr, minutes from fitness.cardio_sessions "
                              "where not deleted order by day desc limit 1"),
        "sessions_28d": db.one("select count(distinct day) as days from fitness.lift_sessions "
                               "where not deleted and day > current_date - 28")or {},
        "bodyweight": db.one("select day, kg from fitness.bodyweight order by day desc limit 1"),
        "unscored_bw_lifts": (db.one("select count(*) as n from fitness.lift_sessions "
                                     "where not deleted and kg is null and total_reps > 0") or {}).get("n", 0),
    }


# ---------- screenshot scanning ----------

SCAN_SYSTEM = ("You read fitness machine and health-app screenshots and return the numbers on them. "
               "Report only what is visibly printed. Never estimate, infer or fill a gap: a field you "
               "cannot read is null. Getting a session's real numbers wrong is worse than leaving them "
               "blank for the operator to type.")

SCAN_PROMPT = """Extract the workout data from this screenshot (Samsung Health, a treadmill or a
cross-trainer console). Return ONLY this JSON, null for anything not clearly visible:
{"duration":"MM:SS or HH:MM:SS","avgHR":number,"maxHR":number,"calories":number,
 "distance":number,"activityType":"Treadmill|Elliptical|Running|Cycling|other"}
distance is in kilometres. Do not convert or round anything else."""


def scan_screenshot(data_url: str) -> dict:
    """Read a workout screenshot into form fields.

    Runs on the box with the Cortex API key, so no key is ever stored on the phone. Haiku: this is
    mechanical extraction from a clear screen, not reasoning, and it runs every logged session.
    """
    from . import provider
    out = provider.think_json(SCAN_SYSTEM, SCAN_PROMPT, model=provider.MODEL_ROUTER,
                              max_tokens=400, purpose="fitness_scan", images=[data_url])
    return {k: out.get(k) for k in ("duration", "avgHR", "maxHR", "calories", "distance",
                                    "activityType")}


# ---------- MyFitnessPal import (one-off history migration) ----------
# The owner's browser reads his MyFitnessPal diary pages and POSTs the rows here. Figures are taken
# exactly as MFP shows them (kcal, carbs, fat, protein per item). Ids are deterministic per date +
# position, so a re-run updates rather than duplicates.

def _mfp_split(raw: str):
    raw = (raw or "").replace("Quick Add - Myfitnesspal Premium", "Quick add")
    name, _, portion = raw.rpartition(", ")
    if not name:
        name, portion = raw, ""
    if " - " in name:
        brand, _, food = name.partition(" - ")
        b, f = brand.strip(), food.strip()
        name = f if (b.lower() in f.lower() or b.lower() in ("generic", "usda")) else f"{f} ({b})"
    m = re.match(r"^([\d.]+)\s*(g|gram|grams|gr|gm)\b", portion.strip(), re.I)
    return name.strip(), portion.strip(), (float(m.group(1)) if m else None)


def import_mfp_diary(diary: dict) -> dict:
    n = {"days": 0, "rows": 0, "foods": 0}
    for day, rows in (diary or {}).items():
        d = _day(day)
        if not d or not rows:
            continue
        n["days"] += 1
        for i, row in enumerate(rows):
            try:
                meal, raw, kcal, c, f, p = row
            except (TypeError, ValueError):
                continue
            name, portion, grams = _mfp_split(raw)
            kcal, c, f, p = _num(kcal), _num(c), _num(f), _num(p)
            fid = None
            if grams and kcal is not None:
                fid = "mfp_food_" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
                k = 100.0 / grams
                made = db.execute(
                    "insert into fitness.foods (uid, name, kcal_100g, protein_100g, carbs_100g, fat_100g, source) "
                    "values (%s,%s,%s,%s,%s,%s,'mfp') on conflict (uid) do nothing returning uid",
                    (fid, name, round(kcal * k, 1), round(p * k, 2) if p is not None else None,
                     round(c * k, 2) if c is not None else None, round(f * k, 2) if f is not None else None))
                n["foods"] += 1 if made else 0
            label = name if grams else (f"{name}, {portion}" if portion else name)
            db.execute(
                "insert into fitness.food_log (uid, day, meal, food_uid, name, qty_g, kcal, protein, carbs, fat, notes) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'MyFitnessPal import') on conflict (uid) do update set "
                "meal=excluded.meal, food_uid=excluded.food_uid, name=excluded.name, qty_g=excluded.qty_g, "
                "kcal=excluded.kcal, protein=excluded.protein, carbs=excluded.carbs, fat=excluded.fat, updated_at=now()",
                (f"mfp_{d.isoformat()}_{i:02d}", d,
                 meal if meal in ("breakfast", "lunch", "dinner", "snacks") else "snacks",
                 fid, label[:200], grams, kcal, p, c, f))
            n["rows"] += 1
    return n


# ---------- watch workouts -> cardio log (Samsung sessions via the Health Bridge) ----------
# Rules agreed with the owner, 5 Oct 2026:
#  * a watch session under 10 minutes is not a workout;
#  * a hand-logged cardio entry on the same day and machine is LINKED to its watch session, never
#    overwritten (the owner's numbers win);
#  * an unlogged session from BEFORE go-live becomes a "from watch" entry with duration, heart rate and
#    calories only, settings NOT recorded (option A: nothing guessed goes into his records);
#  * an unlogged session from go-live on is pre-filled at JGE with the settings of his LAST session on
#    that preset and tagged unconfirmed until he checks it in the app;
#  * a short treadmill run ending just before a 4x4 is that 4x4's warm-up, attached to it;
#  * a session titled "Steam Room" goes to the Steam Room preset. Walks, outdoor rides and untitled
#    "other" sessions are left alone.

WATCH_GO_LIVE = date(2026, 10, 5)
MIN_WORKOUT_MIN = 10
_PRESETS_WATCH = {
    "id_w_elliptical": ("Elliptical - from watch", "Elliptical", False),
    "id_w_treadmill": ("Treadmill - from watch", "Treadmill", False),
    "id_w_4x4": ("HIIT 4X4 Treadmill - from watch", "Treadmill", True),
    "id_steam": ("Steam Room", "Steam room", False),
}
_JGE = {"z2_30": "id_1kgluk8k", "z2_45": "id_88zxee26", "z2_67": "id_n6yi12xi", "4x4": "id_bv0krd6v"}


def _ensure_watch_presets():
    for uid, (name, machine, hiit) in _PRESETS_WATCH.items():
        db.execute("insert into fitness.cardio_presets (uid, name, machine, location, is_hiit, manual_fields) "
                   "values (%s,%s,%s,%s,%s,'[]'::jsonb) on conflict (uid) do nothing",
                   (uid, name, machine, None if uid != "id_steam" else "JGE", hiit))


def _dur(mins: float) -> str:
    s = int(round(mins * 60))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _last_settings(preset_uid: str) -> dict:
    """Settings from the owner's last session on this preset, distance dropped (he does not log it)."""
    row = db.one("select extra from fitness.cardio_sessions where preset_uid=%s and not deleted "
                 "and extra::text <> '{}' order by day desc limit 1", (preset_uid,))
    pre = db.one("select manual_fields from fitness.cardio_presets where uid=%s", (preset_uid,)) or {}
    fields = pre.get("manual_fields") or []
    out = {}
    for k, v in ((row or {}).get("extra") or {}).items():
        try:
            label = str(fields[int(k)].get("label", ""))
        except (ValueError, IndexError, AttributeError):
            label = ""
        if not re.search(r"distance", label, re.I):
            out[k] = v
    return out


def reconcile_watch(since: date | None = None) -> dict:
    _ensure_watch_presets()
    linked = {r["u"] for r in db.query(
        "select watch->>'uid' u from fitness.cardio_sessions where watch is not null union "
        "select watch->>'warmup_uid' from fitness.cardio_sessions where watch ? 'warmup_uid'") if r["u"]}
    q = ("select uid, day, start_at, end_at, type_name, title, kcal, avg_hr, max_hr, "
         "extract(epoch from end_at-start_at)/60 as mins from fitness.health_sessions "
         "where extract(epoch from end_at-start_at)/60 >= %s")
    params: list = [MIN_WORKOUT_MIN]
    if since:
        q += " and day >= %s"
        params.append(since)
    sess = db.query(q + " order by start_at", tuple(params))
    presets = {p["uid"]: p for p in db.query("select uid, name, machine, is_hiit from fitness.cardio_presets")}
    n = {"linked": 0, "created_history": 0, "created_new": 0, "warmups": 0, "steam": 0, "skipped": 0}

    # warm-ups: a 10-20 min treadmill session ending within 15 min of a 4x4's start
    def is_4x4(s):
        return s["type_name"] == "TREADMILL" and 20 <= float(s["mins"]) <= 45 and (s["max_hr"] or 0) >= 160
    warm = {}
    for s in sess:
        if is_4x4(s):
            for w in sess:
                if (w["type_name"] == "TREADMILL" and w["uid"] != s["uid"] and float(w["mins"]) <= 20
                        and timedelta(0) <= s["start_at"] - w["end_at"] <= timedelta(minutes=15)):
                    warm[s["uid"]] = w
                    warm[w["uid"]] = None          # consumed as a warm-up, not its own entry

    for s in sess:
        if s["uid"] in linked or (s["uid"] in warm and warm[s["uid"]] is None):
            continue
        tn, title = s["type_name"], (s["title"] or "")
        if "steam" in title.lower():
            kind = "steam"
        elif tn == "ELLIPTICAL":
            kind = "elliptical"
        elif tn == "TREADMILL":
            kind = "4x4" if is_4x4(s) else "treadmill"
        else:
            n["skipped"] += 1
            continue
        mins = float(s["mins"])
        wu = warm.get(s["uid"])
        watch = {"uid": s["uid"]}
        if wu:
            watch.update(warmup_uid=wu["uid"], warmup_min=round(float(wu["mins"]), 1))
            n["warmups"] += 1
        # 1. a hand-logged entry that day on the same kind of machine -> link only
        if kind != "steam":
            cands = db.query(
                "select c.uid, c.minutes, c.watch from fitness.cardio_sessions c join fitness.cardio_presets p "
                "on p.uid=c.preset_uid where c.day=%s and not c.deleted and c.watch is null "
                "and lower(p.machine)=%s and p.is_hiit=%s and c.uid not like 'w:%%'",
                (s["day"], "elliptical" if kind == "elliptical" else "treadmill", kind == "4x4"))
            if cands:
                best = min(cands, key=lambda c: abs(float(c["minutes"] or 0) - mins))
                db.execute("update fitness.cardio_sessions set watch=%s, updated_at=now() where uid=%s",
                           (Json({**watch, "confirmed": True, "settings": "manual"}), best["uid"]))
                n["linked"] += 1
                continue
        # 2. no hand-logged entry -> create one
        new = s["day"] >= WATCH_GO_LIVE
        if kind == "steam":
            preset, extra, conf, how = "id_steam", {}, True, "none"
            n["steam"] += 1
        elif new and kind == "elliptical":
            preset = _JGE["z2_67"] if mins >= 60 else _JGE["z2_45"] if mins >= 40 else _JGE["z2_30"]
            extra = _last_settings(preset) or {"1": "17", "2": "60"}   # his stated defaults: level 17, 60 rpm
            conf, how = False, "prefilled"
        elif new and kind == "4x4":
            preset, extra, conf, how = _JGE["4x4"], _last_settings(_JGE["4x4"]), False, "prefilled"
        else:
            preset = {"elliptical": "id_w_elliptical", "4x4": "id_w_4x4"}.get(kind, "id_w_treadmill")
            extra, conf, how = {}, not new, "not recorded"
        watch.update(confirmed=conf, settings=how)
        db.execute(
            "insert into fitness.cardio_sessions (uid, preset_uid, preset_name, day, duration, minutes, avg_hr, "
            "max_hr, calories, extra, notes, watch) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do nothing",
            ("w:" + s["uid"], preset, presets.get(preset, {}).get("name") or _PRESETS_WATCH.get(preset, ("",))[0],
             s["day"], _dur(mins), round(mins, 2),
             round(float(s["avg_hr"])) if s["avg_hr"] is not None else None,
             round(float(s["max_hr"])) if s["max_hr"] is not None else None,
             round(float(s["kcal"])) if s["kcal"] is not None else None,
             Json(extra), "From watch" + ("" if conf or how != "prefilled" else ", check settings"),
             Json(watch)))
        n["created_new" if new else "created_history"] += 1
    return n



# ---------- meal photo -> estimated items (owner-approved Opus, 5 Oct 2026: rare use, accuracy first) ----------
# This is the ONE place nutrition is estimated rather than read from a label or database, so every item
# carries the model's confidence and the app tags the log rows "photo estimate". Totals are summed in code.
MEAL_PHOTO_MODEL = "claude-opus-5-5"
MEAL_PHOTO_SYSTEM = (
    "You estimate the nutrition of a meal from a photo for a personal food log. Identify every distinct food "
    "and drink item visible, estimate each portion in grams from visual cues (plate size, cutlery, hands, "
    "packaging), and estimate calories, protein, carbs and fat for that portion. Assume typical restaurant "
    "preparation, including cooking oil, butter and sauces you can see or that the dish normally contains, and "
    "say so in the item's note. Be realistic, not optimistic: restaurant food is usually heavier than it looks. "
    "Never refuse; if something is unclear, give your best estimate with low confidence.")
MEAL_PHOTO_PROMPT = """Return ONLY this JSON:
{"items":[{"name":"short name","grams":number,"kcal":number,"protein":number,"carbs":number,"fat":number,
"confidence":"high|medium|low","note":"what you assumed, e.g. cooked in oil, creamy sauce"}],
"summary":"one line on the meal and the biggest uncertainty"}
Numbers are for the portion in the photo. Protein/carbs/fat in grams."""


def estimate_meal_photo(data_url: str, note: str = "") -> dict:
    from . import provider
    user = MEAL_PHOTO_PROMPT + (f"\nThe owner adds: {note.strip()[:300]}" if note and note.strip() else "")
    out = provider.think_json(MEAL_PHOTO_SYSTEM, user, model=MEAL_PHOTO_MODEL, fast=False, max_tokens=3000,
                              purpose="fitness_meal_photo", images=[data_url])
    items = []
    for it in out.get("items") or []:
        if not isinstance(it, dict) or not str(it.get("name") or "").strip():
            continue
        items.append({"name": str(it["name"]).strip()[:80], "grams": _num(it.get("grams")),
                      "kcal": _num(it.get("kcal")), "protein": _num(it.get("protein")),
                      "carbs": _num(it.get("carbs")), "fat": _num(it.get("fat")),
                      "confidence": (it.get("confidence") or "medium") if it.get("confidence") in ("high", "medium", "low") else "medium",
                      "note": str(it.get("note") or "")[:200]})
    tot = {k: round(sum((i[k] or 0) for i in items), 1) for k in ("kcal", "protein", "carbs", "fat")}
    return {"items": items, "total": tot, "summary": str(out.get("summary") or "")[:300]}
