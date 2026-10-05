"""Eight Sleep -> fitness.sleep_8 (the nightly sleep score and the mattress's own metrics).

Health Connect has no field for a sleep score, so the score comes from Eight Sleep's app API, read the
way the open-source Home Assistant integration (pyEight) does: password grant on auth-api.8slp.net with the
app's public client id, then GET client-api.8slp.net/v1/users/<id>/trends. Unofficial, so it can break if
Eight Sleep changes it. The owner typed his Eight Sleep login into the Fitness app's Connect form; it lives
only in setting `eightsleep_login` and is never echoed back.

Polled by `cortex-eightsleep.timer` (`python -m cortex.eightsleep poll`), a few times a day: the score for a
night can be revised for a while after waking.
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta

import httpx

from . import db

AUTH_URL = "https://auth-api.8slp.net/v1/tokens"
API = "https://client-api.8slp.net/v1"
CLIENT_FILE = "/etc/cortex/eightsleep_client.json"   # {"client_id": ..., "client_secret": ...}: the Eight Sleep app's
                                                    # own OAuth client (as used by pyEight); kept out of git
TZ = "Asia/Dubai"
_UA = {"user-agent": "okhttp/4.9.3", "accept": "application/json", "content-type": "application/json"}


def _client() -> dict:
    import json
    try:
        with open(CLIENT_FILE) as f:
            return json.load(f)
    except OSError:
        raise ValueError(f"Eight Sleep client config missing on the server ({CLIENT_FILE})")


def login(email: str, password: str) -> dict:
    c = _client()
    r = httpx.post(AUTH_URL, headers=_UA, timeout=20, json={
        "client_id": c["client_id"], "client_secret": c["client_secret"], "grant_type": "password",
        "username": email, "password": password})
    if r.status_code in (400, 401, 403):
        raise ValueError("Eight Sleep rejected the email or password")
    r.raise_for_status()
    j = r.json()
    tok, uid = j.get("access_token"), j.get("userId")
    if not tok:
        raise ValueError("unexpected Eight Sleep login reply")
    if not uid:
        me = httpx.get(API + "/users/me", headers={**_UA, "authorization": "Bearer " + tok}, timeout=20).json()
        uid = (me.get("user") or {}).get("userId")
    return {"token": tok, "user_id": uid, "exp": time.time() + int(j.get("expires_in") or 3600)}


def _session(force: bool = False) -> dict:
    s = db.setting_get("eightsleep_session") or {}
    if not force and s.get("token") and s.get("exp", 0) > time.time() + 300:
        return s
    cred = db.setting_get("eightsleep_login") or {}
    if not cred.get("email"):
        raise RuntimeError("Eight Sleep not connected")
    s = login(cred["email"], cred["password"])
    db.setting_set("eightsleep_session", s)
    return s


def _q(d: dict, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def poll(days: int = 14) -> dict:
    s = _session()
    params = {"tz": TZ, "from": (date.today() - timedelta(days=days)).isoformat(), "to": date.today().isoformat(),
              "include-main": "false", "include-all-sessions": "true", "model-version": "v2"}
    url = f"{API}/users/{s['user_id']}/trends"
    r = httpx.get(url, params=params, headers={**_UA, "authorization": "Bearer " + s["token"]}, timeout=30)
    if r.status_code == 401:
        s = _session(force=True)
        r = httpx.get(url, params=params, headers={**_UA, "authorization": "Bearer " + s["token"]}, timeout=30)
    r.raise_for_status()
    n = 0
    from psycopg.types.json import Json
    for d in (r.json() or {}).get("days") or []:
        day = d.get("day")
        if not day or d.get("score") is None and not d.get("sleepDuration"):
            continue
        q = d.get("sleepQualityScore") or {}
        db.execute(
            "insert into fitness.sleep_8 (day, score, fitness, duration_s, light_s, deep_s, rem_s, presence_s, hr, hrv, "
            "resp, raw) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (day) do update set score=excluded.score, "
            "fitness=excluded.fitness, duration_s=excluded.duration_s, light_s=excluded.light_s, deep_s=excluded.deep_s, "
            "rem_s=excluded.rem_s, presence_s=excluded.presence_s, hr=excluded.hr, hrv=excluded.hrv, resp=excluded.resp, "
            "raw=excluded.raw, updated_at=now()",
            (day, d.get("score"), _q(d, "sleepFitnessScore", "total"), d.get("sleepDuration"), d.get("lightDuration"),
             d.get("deepDuration"), d.get("remDuration"), d.get("presenceDuration"),
             _q(q, "heartRate", "current"), _q(q, "hrv", "current"), _q(q, "respiratoryRate", "current"), Json(d)))
        n += 1
    db.setting_set("eightsleep_last_poll", {"at": time.time(), "days": n})
    return {"ok": True, "days": n}


def connect(email: str, password: str) -> dict:
    s = login(email.strip(), password)
    db.setting_set("eightsleep_login", {"email": email.strip(), "password": password})
    db.setting_set("eightsleep_session", s)
    try:
        return poll(days=60)
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "saved": True, "first_poll_error": str(e)[:200]}


def status() -> dict:
    cred = db.setting_get("eightsleep_login") or {}
    last = db.setting_get("eightsleep_last_poll") or {}
    latest = db.one("select day, score from fitness.sleep_8 order by day desc limit 1")
    return {"connected": bool(cred.get("email")), "email": cred.get("email"), "last_poll": last.get("at"),
            "latest": {"day": latest["day"].isoformat(), "score": latest["score"]} if latest else None}


if __name__ == "__main__":
    if sys.argv[1:] == ["poll"]:
        try:
            print(poll())
        except RuntimeError as e:          # not connected yet: quiet no-op for the timer
            print(str(e))
