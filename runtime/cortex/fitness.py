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
from datetime import date, datetime

from psycopg.types.json import Json

from . import db

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
    """The bodyweight that applied ON the session date (latest entry at or before it).

    This is the whole point of a dated log: a 6 kg swing must never rewrite historic records.
    Sessions predating the log fall back to the earliest entry, matching the app.
    """
    if not day or not log:
        return None
    match = None
    for e in log:                                   # arrives sorted ascending
        if e["day"] <= day:
            match = e
        else:
            break
    return float((match or log[0])["kg"])


def lift_kg(weight, day: date | None, bw_log: list[dict]) -> float | None:
    """Kg on the bar: a logged number, or the dated bodyweight for a 'BW' lift."""
    raw = "" if weight is None else str(weight).strip()
    n = _num(re.sub(r"[^0-9.]", "", raw))
    if n and n > 0:
        return n
    if raw.lower() in ("bw", "bodyweight", "", "—"):
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
               "nextTarget": r["next_target"], "notes": r["notes"]}
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
              "favourite": r["favourite"]}
             for r in db.query("select * from fitness.foods where not deleted order by name")]
    food_log = [{"id": r["uid"], "date": r["day"].isoformat(), "meal": r["meal"], "foodId": r["food_uid"],
                 "name": r["name"], "qtyG": _num(r["qty_g"]), "kcal": _num(r["kcal"]),
                 "protein": _num(r["protein"]), "carbs": _num(r["carbs"]), "fat": _num(r["fat"]),
                 "notes": r["notes"]}
                for r in db.query("select * from fitness.food_log where not deleted order by day, uid")]
    tombstones += [r["uid"] for r in db.query(
        "select uid from fitness.foods where deleted union all "
        "select uid from fitness.food_log where deleted")]
    return {"bodyweight": bw, "plans": plans, "liftSessions": lifts, "cardioPresets": presets,
            "cardioSessions": cardio, "vo2": vo2, "tombstones": tombstones,
            "foods": foods, "foodLog": food_log, "targets": targets(), "health": health_pull(),
            "counts": {"bodyweight": len(bw), "plans": len(plans), "liftSessions": len(lifts),
                       "cardioPresets": len(presets), "cardioSessions": len(cardio), "vo2": len(vo2),
                       "foods": len(foods), "foodLog": len(food_log)}}


# ---------- targets (daily intake goals; the operator's numbers, edited in the app) ----------

DEFAULT_TARGETS = {"kcal": 2350, "protein": 178}


def targets() -> dict:
    t = db.setting_get("fitness_targets") or {}
    return {**DEFAULT_TARGETS, **{k: v for k, v in t.items() if v is not None}}


def set_targets(t: dict) -> dict:
    clean = {}
    for k in ("kcal", "protein", "carbs", "fat"):
        v = _num((t or {}).get(k))
        if v is not None and v > 0:
            clean[k] = round(v)
    if clean:
        db.setting_set("fitness_targets", {**(db.setting_get("fitness_targets") or {}), **clean})
    return targets()


# ---------- Health Connect (watch + phone data from the companion app) ----------


def health_pull(days: int = 400) -> dict:
    """What the app shows: daily totals, watch sessions and weigh-ins for the last `days` days."""
    d = [{"date": r["day"].isoformat(), "steps": r["steps"], "totalKcal": _num(r["total_kcal"]),
          "activeKcal": _num(r["active_kcal"]), "distanceM": _num(r["distance_m"]),
          "floors": _num(r["floors"]), "restingHR": _num(r["resting_hr"]), "hrvMs": _num(r["hrv_ms"]),
          "sleepMin": _num(r["sleep_min"])}
         for r in db.query("select * from fitness.health_daily where day > current_date - %s order by day",
                           (days,))]
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
    return {"days": d, "sessions": s, "weights": w,
            "lastSync": last["t"].isoformat() if last and last.get("t") else None}


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


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
            "insert into fitness.health_daily (day, steps, total_kcal, active_kcal, distance_m, floors, "
            "resting_hr, hrv_ms, sleep_min, device) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (day) do update set steps=excluded.steps, total_kcal=excluded.total_kcal, "
            "active_kcal=excluded.active_kcal, distance_m=excluded.distance_m, floors=excluded.floors, "
            "resting_hr=excluded.resting_hr, hrv_ms=excluded.hrv_ms, sleep_min=excluded.sleep_min, "
            "device=excluded.device, updated_at=now()",
            (d, int(steps) if steps is not None else None, _num(r.get("total_kcal")),
             _num(r.get("active_kcal")), _num(r.get("distance_m")), _num(r.get("floors")),
             _num(r.get("resting_hr")), _num(r.get("hrv_ms")), _num(r.get("sleep_min")), dev))
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
            "servingG": _num(p.get("serving_quantity")), "source": "off", "sourceId": p.get("code")}


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
            "servingLabel": None, "servingG": None, "source": "usda", "sourceId": str(f.get("fdcId"))}


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


def push(doc: dict, source: str = "app") -> dict:
    """Upsert a whole client document. Additive by design: rows absent from the doc are left alone."""
    counts = {k: 0 for k in ("bodyweight", "plans", "liftSessions", "cardioPresets",
                             "cardioSessions", "vo2")}

    for r in doc.get("bodyweight") or []:
        d = _day(r.get("date") or r.get("day"))
        kg = _num(r.get("kg"))
        if not d or kg is None:
            continue
        db.execute("insert into fitness.bodyweight (day, kg, notes) values (%s,%s,%s) "
                   "on conflict (day) do update set kg=excluded.kg, notes=excluded.notes, "
                   "updated_at=now()", (d, kg, r.get("notes")))
        counts["bodyweight"] += 1

    for r in doc.get("plans") or doc.get("liftWorkouts") or []:
        if not r.get("id"):
            continue
        db.execute("insert into fitness.plans (uid, name, exercises, deleted) values (%s,%s,%s,%s) "
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
        db.execute(
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
        db.execute(
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
            row = db.one("select manual_fields from fitness.cardio_presets where uid=%s",
                         (r.get("exerciseId"),))
            fields = (row or {}).get("manual_fields") or []
        dist = None
        for i, f in enumerate(fields):
            if re.search(r"distance", str(f.get("label", "")), re.I):
                dist = _num(extra.get(str(i)))
        avg_hr = _num(r.get("avgHR"))
        # m/beat = metres per heartbeat, the aerobic efficiency metric.
        mpb = round(dist * 1000 / (avg_hr * mins), 3) if (dist and avg_hr and mins) else None
        db.execute(
            "insert into fitness.cardio_sessions (uid, preset_uid, preset_name, day, duration, "
            "minutes, avg_hr, max_hr, calories, distance_km, m_per_beat, extra, next_target, notes, "
            "deleted) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set preset_uid=excluded.preset_uid, "
            "preset_name=excluded.preset_name, day=excluded.day, duration=excluded.duration, "
            "minutes=excluded.minutes, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, "
            "calories=excluded.calories, distance_km=excluded.distance_km, "
            "m_per_beat=excluded.m_per_beat, extra=excluded.extra, next_target=excluded.next_target, "
            "notes=excluded.notes, deleted=cardio_sessions.deleted or excluded.deleted, updated_at=now()",
            (r["id"], r.get("exerciseId"), r.get("exerciseName"), d, r.get("duration"), mins,
             avg_hr, _num(r.get("maxHR")), _num(r.get("calories")), dist, mpb, Json(extra),
             Json(r.get("nextTarget")) if r.get("nextTarget") is not None else None,
             r.get("notes"), bool(r.get("deleted"))))
        counts["cardioSessions"] += 1

    for r in doc.get("vo2") or doc.get("vo2Records") or []:
        d = _day(r.get("date"))
        val = _num(r.get("value") if r.get("value") is not None else r.get("vo2"))
        if not d or val is None:
            continue
        db.execute("insert into fitness.vo2 (uid, day, value, method, notes) values (%s,%s,%s,%s,%s) "
                   "on conflict (uid) do update set day=excluded.day, value=excluded.value, "
                   "method=excluded.method, notes=excluded.notes, updated_at=now()",
                   (str(r.get("id") or f"vo2|{d.isoformat()}"), d, val, r.get("method"),
                    r.get("notes")))
        counts["vo2"] += 1

    counts["foods"] = counts["foodLog"] = 0
    for r in doc.get("foods") or []:
        if r.get("id") and r.get("deleted") and not r.get("name"):   # bare tombstone from the client
            db.execute("update fitness.foods set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        if not r.get("id") or not (r.get("name") or "").strip():
            continue
        db.execute(
            "insert into fitness.foods (uid, name, brand, barcode, serving_label, serving_g, kcal_100g, "
            "protein_100g, carbs_100g, fat_100g, fibre_100g, source, source_id, favourite, deleted) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set name=excluded.name, brand=excluded.brand, barcode=excluded.barcode, "
            "serving_label=excluded.serving_label, serving_g=excluded.serving_g, kcal_100g=excluded.kcal_100g, "
            "protein_100g=excluded.protein_100g, carbs_100g=excluded.carbs_100g, fat_100g=excluded.fat_100g, "
            "fibre_100g=excluded.fibre_100g, source=excluded.source, source_id=excluded.source_id, "
            "favourite=excluded.favourite, deleted=foods.deleted or excluded.deleted, updated_at=now()",
            (r["id"], r["name"].strip(), r.get("brand"), r.get("barcode"), r.get("servingLabel"),
             _num(r.get("servingG")), _num(r.get("kcal100")), _num(r.get("protein100")),
             _num(r.get("carbs100")), _num(r.get("fat100")), _num(r.get("fibre100")), r.get("source"),
             r.get("sourceId"), bool(r.get("favourite")), bool(r.get("deleted"))))
        counts["foods"] += 1
    for r in doc.get("foodLog") or []:
        d = _day(r.get("date"))
        if not r.get("id") or not d:
            continue
        if r.get("deleted") and not r.get("name"):        # bare tombstone from the client
            db.execute("update fitness.food_log set deleted=true, updated_at=now() where uid=%s", (r["id"],))
            continue
        db.execute(
            "insert into fitness.food_log (uid, day, meal, food_uid, name, qty_g, kcal, protein, carbs, fat, "
            "notes, deleted) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set day=excluded.day, meal=excluded.meal, food_uid=excluded.food_uid, "
            "name=excluded.name, qty_g=excluded.qty_g, kcal=excluded.kcal, protein=excluded.protein, "
            "carbs=excluded.carbs, fat=excluded.fat, notes=excluded.notes, "
            "deleted=food_log.deleted or excluded.deleted, updated_at=now()",
            (r["id"], d, r.get("meal"), r.get("foodId"), (r.get("name") or "").strip() or "Food",
             _num(r.get("qtyG")), _num(r.get("kcal")), _num(r.get("protein")), _num(r.get("carbs")),
             _num(r.get("fat")), r.get("notes"), bool(r.get("deleted"))))
        counts["foodLog"] += 1
    if doc.get("targets"):
        set_targets(doc["targets"])

    total = sum(counts.values())
    # The app pushes whenever it regains focus, so most pushes are byte-identical to the last one.
    # Storing those would grow the nightly dump for nothing. Only a document that actually differs
    # is worth keeping — and every distinct version is still kept, because this is the restore path.
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    prev = db.one("select doc_hash from fitness.snapshots order by id desc limit 1")
    if not prev or prev.get("doc_hash") != digest:
        db.execute("insert into fitness.snapshots (source, rows, doc, doc_hash) values (%s,%s,%s,%s)",
                   (source, total, Json(doc), digest))
    return {"ok": True, "written": counts, "total": total}


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
        if _num(re.sub(r"[^0-9.]", "", raw)):
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
