"""Nightly backup: pg_dump the Cortex DB -> gzip -> upload to Google Drive (keyless OAuth).

Auth is keyless: the OAuth client config lives at /etc/cortex/google_oauth_client.json and a
refresh token (from the one-time /oauth/google/start consent) is stored in settings. Run from
cron as the cortex user:  /opt/coretex/.venv/bin/python /opt/coretex/runtime/backup_drive.py
"""
from __future__ import annotations

import datetime
import gzip
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
from cortex import config, db

FOLDER_ID = config.get("GOOGLE_BACKUP_FOLDER") or "1oEKRo6aH4r1-HE29Qtds5DxyMlyZATEZ"
CLIENT_PATH = config.get("GOOGLE_OAUTH_CLIENT") or "/etc/cortex/google_oauth_client.json"
KEEP = 30


def _access_token() -> str:
    with open(CLIENT_PATH) as f:
        c = json.load(f).get("web") or {}
    rt = db.setting_get("google_refresh_token")
    if not rt:
        raise SystemExit("No google_refresh_token — authorise first at https://coretex.uk/oauth/google/start")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _dump() -> tuple[str, str]:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"cortex-{ts}.sql.gz"
    path = f"/tmp/{name}"
    p = subprocess.run(["pg_dump", config.require("DATABASE_URL")], capture_output=True)
    if p.returncode != 0:
        raise SystemExit("pg_dump failed: " + p.stderr.decode()[:300])
    with gzip.open(path, "wb") as f:
        f.write(p.stdout)
    return name, path


def _upload(token: str, name: str, path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    b = "cortexbkpboundary"
    body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps({"name": name, "parents": [FOLDER_ID]})
            + f"\r\n--{b}\r\nContent-Type: application/gzip\r\n\r\n").encode() + data + f"\r\n--{b}--".encode()
    r = httpx.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={b}"},
        content=body, timeout=180)
    r.raise_for_status()
    return r.json()


def _prune(token: str) -> int:
    r = httpx.get("https://www.googleapis.com/drive/v3/files", params={
        "q": f"'{FOLDER_ID}' in parents and name contains 'cortex-' and trashed=false",
        "orderBy": "name desc", "fields": "files(id,name)", "pageSize": "200",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    old = r.json().get("files", [])[KEEP:]
    for f in old:
        httpx.delete(f"https://www.googleapis.com/drive/v3/files/{f['id']}?supportsAllDrives=true",
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return len(old)


def main() -> None:
    token = _access_token()
    name, path = _dump()
    try:
        res = _upload(token, name, path)
        pruned = _prune(token)
        print(f"backed up {res.get('name')} (id {res.get('id')}); pruned {pruned} old")
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    main()
