"""YOLO — the owner's boat maintenance and cost book (personal project, not a company).

Boat: Raffaelli Storm 46 Fly "YOLO", Punat, Krk. The PWA at /yolo is the front end; this module is
the server half, built on the fitness doctrine: the app pushes its whole document (small, hundreds
of rows), the server explodes it into yolo.* tables and keeps every push in yolo.snapshots. Rows are
upserted by the client's own uid and never hard-deleted — deletion is explicit (deleted=true).

Money: amounts are EUR unless a row says otherwise. The boat year runs 1 April to 31 March (the
owner's own cost sheets ran spring to spring, anchored on the March berth renewal), so year_label
"2025-2026" = Apr 2025 to Mar 2026. A row with no parseable date keeps the label it was filed under.

Documents are NEVER stored here: they live on the owner's PERSONAL Google Drive (PERSONAL DRIVE /
YOLO BOAT YACHT, identity rashadalsafar@gmail.com via the cortex-personal OAuth client), and
yolo.documents holds the Drive id + metadata. Uploads from the app go into an APP UPLOADS subfolder
(setting yolo_drive_uploads). Browsing uses the personal token's drive.readonly scope.

CLI (run on the box as cortex):
    python -m cortex.yolo seed db/yolo_seed.json   # one-off import of the spreadsheet history
    python -m cortex.yolo setuplink                # long-lived owner setup link for the PWA
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import sys
import time
from datetime import date, datetime

import httpx
from psycopg.types.json import Json

from . import db, drive

PERSONAL_CLIENT = "/etc/cortex/google_oauth_client_personal.json"

# The YOLO BOAT YACHT folder on the personal Drive. Stored as settings so a Drive reshuffle is a
# setting change, not a deploy: yolo_drive_root (the folder id), yolo_drive_uploads (APP UPLOADS).
ROOT_SETTING = "yolo_drive_root"
UPLOADS_SETTING = "yolo_drive_uploads"


# ---------- boat year ----------


def boat_year(d: date | None) -> str | None:
    """Apr–Mar: 2025-05-10 -> '2025-2026'; 2026-02-01 -> '2025-2026'."""
    if not d:
        return None
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{start + 1}"


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


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- push / pull ----------

_TABLES = {
    "costs": ("yolo.costs",
              ("day", "year_label", "title", "category", "amount", "currency", "paid_by", "method",
               "note", "doc_uids", "source", "deleted")),
    "tasks": ("yolo.tasks",
              ("section", "title", "interval", "due", "completed", "completed_at", "responsible",
               "note", "doc_uids", "deleted")),
    "assets": ("yolo.assets",
               ("name", "category", "installed", "cost", "serial", "manual_url", "note", "doc_uids",
                "deleted")),
    "documents": ("yolo.documents",
                  ("name", "drive_id", "kind", "mime", "day", "note", "deleted")),
}

_DATES = {"day", "due", "completed_at", "installed"}
_NUMS = {"amount", "cost"}
_BOOLS = {"completed", "deleted"}
_JSON = {"doc_uids"}


def push(doc: dict, source: str = "app") -> dict:
    """Upsert the document's rows. A missing key means 'no change to that table', never 'empty'."""
    doc = doc or {}
    db.execute("insert into yolo.snapshots (source, doc) values (%s, %s)", (source, Json(doc)))
    counts = {}
    for key, (table, cols) in _TABLES.items():
        rows = doc.get(key)
        if not isinstance(rows, list):
            continue
        n = 0
        for r in rows:
            uid = str(r.get("uid") or r.get("id") or "").strip()
            if not uid:
                continue
            vals = []
            for c in cols:
                v = r.get(c)
                if c in _DATES:
                    v = _day(v)
                elif c in _NUMS:
                    v = _num(v)
                elif c in _BOOLS:
                    v = bool(v)
                elif c in _JSON:
                    v = Json(v if isinstance(v, list) else [])
                elif v is not None:
                    v = str(v)
                vals.append(v)
            # a cost's year label is derived from its date when it has one; the import's label stands otherwise
            if key == "costs":
                i = cols.index("year_label")
                d = _day(r.get("day"))
                if d:
                    vals[i] = boat_year(d)
            collist = ", ".join(cols)
            sets = ", ".join(f"{c}=excluded.{c}" for c in cols)
            db.execute(
                f"insert into {table} (uid, {collist}, updated_at) values (%s{', %s' * len(cols)}, now()) "
                f"on conflict (uid) do update set {sets}, updated_at=now()",
                tuple([uid] + vals))
            n += 1
        counts[key] = n
    return {"ok": True, "written": counts}


def pull() -> dict:
    out = {}
    for key, (table, cols) in _TABLES.items():
        rows = db.query(f"select uid, {', '.join(cols)} from {table} where not deleted order by updated_at")
        for r in rows:
            for c in list(r.keys()):
                v = r[c]
                if isinstance(v, (date, datetime)):
                    r[c] = str(v)[:10]
                elif hasattr(v, "quantize"):          # Decimal
                    r[c] = float(v)
        out[key] = rows
    out["totals"] = totals()
    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    return out


def totals() -> dict:
    """Running money, computed server-side so every client agrees: per boat year and lifetime."""
    rows = db.query("select coalesce(year_label,'undated') as y, sum(amount)::float as total, count(*) as n "
                    "from yolo.costs where not deleted and amount is not null group by 1 order by 1")
    lifetime = sum(r["total"] or 0 for r in rows)
    return {"by_year": rows, "lifetime": round(lifetime, 2)}


# ---------- personal Drive ----------


def _personal_token() -> str:
    with open(PERSONAL_CLIENT) as f:
        c = json.load(f).get("web") or {}
    rt = db.setting_get("google_refresh_token:personal")
    if not rt:
        raise RuntimeError("No google_refresh_token:personal — connect at /oauth/google/start?purpose=drive&company=personal")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def browse(folder_id: str = "") -> dict:
    """One level of the YOLO Drive tree (read-only). No folder = the YOLO BOAT YACHT root."""
    fid = (folder_id or "").strip() or db.setting_get(ROOT_SETTING)
    if not fid:
        return {"error": f"setting {ROOT_SETTING} is not set"}
    tok = _personal_token()
    files = drive.list_folder(fid, tok)
    files.sort(key=lambda f: (f.get("mimeType") != "application/vnd.google-apps.folder", (f.get("name") or "").lower()))
    for f in files:
        f["url"] = (f"https://drive.google.com/drive/folders/{f['id']}"
                    if f.get("mimeType") == "application/vnd.google-apps.folder"
                    else f"https://drive.google.com/file/d/{f['id']}/view")
    return {"folder": fid, "files": files}


def upload_document(name: str, mime: str, data_b64: str, note: str = "", kind: str = "other") -> dict:
    """App upload -> APP UPLOADS on the personal Drive + a yolo.documents row. Never stores bytes here."""
    data = base64.b64decode(data_b64 or "")
    if not data:
        return {"error": "empty file"}
    if len(data) > 15 * 1024 * 1024:
        return {"error": "over 15MB — put it on Drive directly and link it"}
    tok = _personal_token()
    folder = db.setting_get(UPLOADS_SETTING)
    if not folder:
        root = db.setting_get(ROOT_SETTING)
        if not root:
            return {"error": f"setting {ROOT_SETTING} is not set"}
        folder = drive.ensure_subfolder(root, "APP UPLOADS", tok)
        db.setting_set(UPLOADS_SETTING, folder)
    fid = drive.upload(folder, name, mime or "application/octet-stream", data, tok)
    uid = "doc_" + hashlib.sha1(f"{fid}{name}".encode()).hexdigest()[:12]
    db.execute("insert into yolo.documents (uid, name, drive_id, kind, mime, day, note) "
               "values (%s,%s,%s,%s,%s,%s,%s) on conflict (uid) do nothing",
               (uid, name, fid, kind, mime, date.today(), note or None))
    return {"ok": True, "uid": uid, "drive_id": fid,
            "url": f"https://drive.google.com/file/d/{fid}/view"}


# ---------- CLI ----------


def _setup_link(days: int = 3650) -> str:
    """A long-lived OWNER token in the api's own format (subject.exp.sig over the api_secret), so the
    PWA can be connected once from a link, like the fitness setup link."""
    secret = (db.setting_get("api_secret") or "").encode()
    if not secret:
        raise RuntimeError("api_secret not set")
    payload = f"owner.{int(time.time()) + days * 86400}"
    sig = hmac_mod.new(secret, payload.encode(), hashlib.sha256).digest()
    tok = f"{payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
    return f"https://coretex.uk/yolo/#k={tok}"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        with open(sys.argv[2], encoding="utf-8") as f:
            print(json.dumps(push(json.load(f), source="seed")))
    elif cmd == "setuplink":
        print(_setup_link())
    elif cmd == "totals":
        print(json.dumps(totals(), indent=2))
    else:
        print("usage: python -m cortex.yolo seed <file.json> | setuplink | totals")
