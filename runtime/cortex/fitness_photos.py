"""Weekly body progress photos (owner, 10 Oct 2026): taken on Sundays with the waist measurement.

Private by design: files live on the box in /opt/cortex-knowledge/fitness-photos (mode 700, cortex user), are never in
the repo or behind a public URL, and are served only to the owner's authenticated app (fetched as a blob). They ride
the nightly private Drive backup with the rest of /opt/cortex-knowledge. The app shrinks each photo before upload.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
from datetime import date

from . import config, db

DIR = os.path.join(config.get("CORTEX_KNOWLEDGE_DIR") or "/opt/cortex-knowledge", "fitness-photos")
_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def save(data_url: str, day: str, note: str = "") -> dict:
    d = date.fromisoformat(day)                     # raises on a bad date
    m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", data_url or "", re.S)
    if not m:
        raise ValueError("expected a JPEG, PNG or WebP image")
    raw = base64.b64decode(m.group(2))
    if len(raw) > 6_000_000:
        raise ValueError("photo too large")
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    uid = f"ph_{d.isoformat()}_{secrets.token_hex(4)}"
    fname = f"{uid}.{_TYPES[m.group(1)]}"
    with open(os.path.join(DIR, fname), "wb") as f:
        f.write(raw)
    db.execute("insert into fitness.progress_photos (uid, day, file, mime, bytes, note) values (%s,%s,%s,%s,%s,%s)",
               (uid, d, fname, m.group(1), len(raw), (note or "")[:200] or None))
    return {"ok": True, "id": uid}


def listing() -> list:
    return [{"id": r["uid"], "date": r["day"].isoformat(), "note": r["note"]}
            for r in db.query("select uid, day, note from fitness.progress_photos where not deleted order by day, created_at")]


def path_of(uid: str) -> tuple[str, str] | None:
    r = db.one("select file, mime from fitness.progress_photos where uid=%s and not deleted", (uid,))
    if not r:
        return None
    p = os.path.join(DIR, r["file"])
    return (p, r["mime"]) if os.path.isfile(p) else None


def delete(uid: str) -> dict:
    """Soft delete: hidden from the app; the file stays on the box (owner rule: never hard-delete his data unasked)."""
    db.execute("update fitness.progress_photos set deleted=true where uid=%s", (uid,))
    return {"ok": True}
