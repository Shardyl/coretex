"""Web Push — phone lock-screen delivery (phase 4 of the notifications spec).

`notifications.notify()` calls `send_to_devices()` for normal/critical items. Each device registers a
Web Push subscription once (after the in-app primer + OS permission); we store it here and fan a push
out with the VAPID keypair (private key in settings, never leaves the box). Expired endpoints (404/410)
are dropped automatically. Phone-only by Rashad's decision — desktop has the app open.
"""
from __future__ import annotations

import json
import os
import tempfile

from . import db

_SCHEMA = """
create table if not exists push_subscriptions (
  id bigserial primary key,
  endpoint text unique not null,
  p256dh text not null,
  auth text not null,
  created_at timestamptz default now(),
  last_ok timestamptz);
"""

_ALTERS = "alter table push_subscriptions add column if not exists user_id bigint"


_PEM_PATH = None


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_SCHEMA)
        c.execute(_ALTERS)


def _vapid() -> dict:
    return db.setting_get("vapid") or {}


def public_key() -> str:
    """The VAPID application-server key the browser needs to subscribe (safe to expose)."""
    return _vapid().get("public_key", "")


def subscribe(sub: dict, user_id=None) -> dict:
    """Register a device. user_id None = the OWNER's device (receives everything); a team user's device
    receives only notifications for companies inside their scope (resolved at send time, so a scope
    change applies immediately)."""
    ensure_schema()
    keys = (sub or {}).get("keys") or {}
    ep = (sub or {}).get("endpoint")
    if not ep or not keys.get("p256dh") or not keys.get("auth"):
        return {"ok": False, "error": "bad subscription"}
    db.execute("insert into push_subscriptions (endpoint, p256dh, auth, user_id) values (%s,%s,%s,%s) "
               "on conflict (endpoint) do update set p256dh=excluded.p256dh, auth=excluded.auth, "
               "user_id=excluded.user_id",
               (ep, keys["p256dh"], keys["auth"], user_id))
    return {"ok": True}


def unsubscribe(endpoint: str) -> dict:
    db.execute("delete from push_subscriptions where endpoint=%s", (endpoint,))
    return {"ok": True}


def device_count() -> int:
    ensure_schema()
    r = db.one("select count(*) n from push_subscriptions")
    return int(r["n"]) if r else 0


def _pem_file() -> str | None:
    """Write the VAPID private PEM to a 0600 temp file once; pywebpush reads it by path."""
    global _PEM_PATH
    if _PEM_PATH and os.path.exists(_PEM_PATH):
        return _PEM_PATH
    pem = _vapid().get("private_pem")
    if not pem:
        return None
    fd, path = tempfile.mkstemp(suffix=".pem")
    os.write(fd, pem.encode())
    os.close(fd)
    os.chmod(path, 0o600)
    _PEM_PATH = path
    return path


def send_to_devices(notif: dict) -> bool:
    """Fan a notification out to every subscribed device. Returns True if at least one was delivered."""
    ensure_schema()
    pem = _pem_file()
    if not pem:
        return False
    subs = db.query("select * from push_subscriptions")
    if not subs:
        return False
    # Scope filter: owner devices (user_id null) get everything; a team device gets a notification only
    # when it belongs to a company inside that user's scope. No-company (system) pushes are owner-only.
    slug = None
    if notif.get("company_id"):
        co = db.one("select slug from companies where id=%s", (notif["company_id"],))
        slug = (co or {}).get("slug")
    scoped = []
    for s0 in subs:
        if not s0.get("user_id"):
            scoped.append(s0)
            continue
        u = db.one("select companies from users where id=%s and active", (s0["user_id"],))
        if u and slug and slug in (u.get("companies") or []):
            scoped.append(s0)
    subs = scoped
    if not subs:
        return False
    from pywebpush import WebPushException, webpush
    v = _vapid()
    payload = json.dumps({"title": notif.get("title") or "Cortex", "body": notif.get("body") or "",
                          "tag": f"n{notif.get('id')}", "url": "/", "category": notif.get("category")})
    sent = 0
    for s in subs:
        sub = {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
        try:
            webpush(subscription_info=sub, data=payload, vapid_private_key=pem,
                    vapid_claims={"sub": v.get("subject", "mailto:hello@sensa.digital")})
            sent += 1
            db.execute("update push_subscriptions set last_ok=now() where id=%s", (s["id"],))
        except WebPushException as e:  # noqa: PERF203
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):     # subscription gone -> drop it
                db.execute("delete from push_subscriptions where id=%s", (s["id"],))
        except Exception:  # noqa: BLE001
            pass
    return sent > 0
