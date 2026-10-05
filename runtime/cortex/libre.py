"""FreeStyle Libre CGM -> fitness.cgm, through a LibreLinkUp FOLLOWER account.

Abbott publishes no API. LibreLinkUp (the family-sharing app) is read by the same endpoints the open-source
diabetes community uses (Nightscout's libre connector, libre-link-up-api-client). The owner created the
follower login himself and typed it into the Fitness app's Connect Libre form, so the credentials live only
in setting `libre_follower` and are never shown back.

The follower endpoints return roughly the last 12 hours of readings, so this is polled every 5 minutes by
the `cortex-libre.timer` systemd unit (`python -m cortex.libre poll`) and every reading is kept. Values are
read as mg/dL from the API and stored as mmol/L (/18.0182), the same unit as the finger-prick readings.

Region: login on the US host answers {"redirect": true, "region": "ae"} for a UAE account; the session then
uses https://api-<region>.libreview.io.
"""
from __future__ import annotations

import hashlib
import sys
import time
from datetime import datetime, timezone

import httpx

from . import db

VERSION = "4.16.0"            # LibreLinkUp app version the API expects; raise if login starts failing
MGDL_PER_MMOL = 18.0182


def _headers(token: str | None = None, account_hash: str | None = None) -> dict:
    h = {"accept-encoding": "gzip", "cache-control": "no-cache", "connection": "Keep-Alive",
         "content-type": "application/json", "product": "llu.android", "version": VERSION}
    if token:
        h["authorization"] = "Bearer " + token
    if account_hash:
        h["account-id"] = account_hash
    return h


def login(email: str, password: str) -> dict:
    """Log in, following the region redirect. Returns {base, token, account_hash, exp} or raises."""
    base = "https://api-us.libreview.io"
    for _ in range(2):
        r = httpx.post(base + "/llu/auth/login", json={"email": email, "password": password},
                       headers=_headers(), timeout=20)
        j = r.json() if r.content else {}
        d = j.get("data") or {}
        if d.get("redirect") and d.get("region"):
            base = f"https://api-{d['region']}.libreview.io"
            continue
        if j.get("status") == 2:
            raise ValueError("LibreLinkUp rejected the email or password")
        if j.get("status") == 4:
            raise ValueError("LibreLinkUp needs an action in the app first (accept the terms in LibreLinkUp, then retry)")
        tick = d.get("authTicket") or {}
        uid = (d.get("user") or {}).get("id")
        if not tick.get("token") or not uid:
            raise ValueError(f"unexpected LibreLinkUp login reply (status {j.get('status')}, http {r.status_code})")
        return {"base": base, "token": tick["token"], "exp": int(tick.get("expires") or time.time() + 3600),
                "account_hash": hashlib.sha256(uid.encode()).hexdigest()}
    raise ValueError("LibreLinkUp kept redirecting")


def _session(force: bool = False) -> dict:
    s = db.setting_get("libre_session") or {}
    if not force and s.get("token") and s.get("exp", 0) > time.time() + 300:
        return s
    cred = db.setting_get("libre_follower") or {}
    if not cred.get("email"):
        raise RuntimeError("Libre not connected")
    s = login(cred["email"], cred["password"])
    db.setting_set("libre_session", s)
    return s


def _ts(v: str | None) -> datetime | None:
    """'10/5/2026 9:12:00 AM' (FactoryTimestamp = UTC) -> aware datetime."""
    if not v:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def poll() -> dict:
    s = _session()
    for attempt in range(2):
        h = _headers(s["token"], s["account_hash"])
        r = httpx.get(s["base"] + "/llu/connections", headers=h, timeout=20)
        if r.status_code == 401 and attempt == 0:
            s = _session(force=True)
            continue
        conns = (r.json() or {}).get("data") or []
        break
    if not conns:
        return {"ok": False, "error": "no connection: accept the LibreLinkUp invite and check sharing is on in LibreLink"}
    pid = conns[0]["patientId"]
    g = httpx.get(f"{s['base']}/llu/connections/{pid}/graph", headers=h, timeout=20).json().get("data") or {}
    pts = list(g.get("graphData") or [])
    cur = ((g.get("connection") or {}).get("glucoseMeasurement"))
    if cur:
        pts.append(cur)
    n = 0
    for p in pts:
        at = _ts(p.get("FactoryTimestamp"))
        mg = p.get("ValueInMgPerDl")
        if not at or mg is None:
            continue
        db.execute("insert into fitness.cgm (at, mmol, trend, source) values (%s,%s,%s,'libre') "
                   "on conflict (at) do nothing", (at, round(float(mg) / MGDL_PER_MMOL, 2), p.get("TrendArrow")))
        n += 1
    db.setting_set("libre_last_poll", {"at": time.time(), "points": n,
                                      "sensor": ((g.get("connection") or {}).get("sensor") or {}).get("sn")})
    return {"ok": True, "points": n}


def connect(email: str, password: str) -> dict:
    """Save the follower login after proving it works, then pull once."""
    s = login(email.strip(), password)
    db.setting_set("libre_follower", {"email": email.strip(), "password": password})
    db.setting_set("libre_session", s)
    try:
        return poll()
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "saved": True, "first_poll_error": str(e)[:200]}


def status() -> dict:
    cred = db.setting_get("libre_follower") or {}
    last = db.setting_get("libre_last_poll") or {}
    latest = db.one("select at, mmol, trend from fitness.cgm order by at desc limit 1")
    return {"connected": bool(cred.get("email")), "email": cred.get("email"),
            "last_poll": last.get("at"), "latest": {"at": latest["at"].isoformat(), "mmol": float(latest["mmol"]),
                                                     "trend": latest["trend"]} if latest else None}


if __name__ == "__main__":
    if sys.argv[1:] == ["poll"]:
        try:
            print(poll())
        except RuntimeError as e:          # not connected yet: quiet no-op for the timer
            print(str(e))
