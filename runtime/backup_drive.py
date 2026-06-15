"""Nightly backup -> Google Drive (keyless OAuth). Backs up EVERYTHING about Cortex:

  1. cortex-db-<ts>.sql.gz       - pg_dump of the DB (skills, universal+local rules, conversations,
                                   tasks, settings). The canonical operating rules live here.
  2. cortex-knowledge-<ts>.tar.gz - the repo (code + docs + BUILD-LOG) AND /opt/cortex-knowledge
                                   (mirror of Claude's memory + the Atlas/Gemini/deploy protocols).
                                   This is "how we build and manage Cortex".

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
REPO_DIR = config.get("CORTEX_REPO_DIR") or "/opt/coretex"
VAULT_DIR = config.get("CORTEX_KNOWLEDGE_DIR") or "/opt/cortex-knowledge"


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


def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _dump_db(ts: str) -> tuple[str, str]:
    name = f"cortex-db-{ts}.sql.gz"
    path = f"/tmp/{name}"
    p = subprocess.run(["pg_dump", config.require("DATABASE_URL")], capture_output=True)
    if p.returncode != 0:
        raise SystemExit("pg_dump failed: " + p.stderr.decode()[:300])
    with gzip.open(path, "wb") as f:
        f.write(p.stdout)
    return name, path


def _archive_knowledge(ts: str) -> tuple[str, str]:
    """Tar the repo (code + docs) and the knowledge vault (memory mirror + protocols)."""
    name = f"cortex-knowledge-{ts}.tar.gz"
    path = f"/tmp/{name}"
    targets = []
    for d in (REPO_DIR, VAULT_DIR):
        if os.path.isdir(d):
            targets.append(os.path.relpath(d, "/"))   # e.g. opt/coretex
    if not targets:
        raise SystemExit("no repo/vault dirs to archive")
    cmd = ["tar", "czf", path, "-C", "/",
           "--exclude=.venv", "--exclude=__pycache__", "--exclude=*.pyc", "--exclude=.git",
           *targets]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise SystemExit("tar failed: " + p.stderr.decode()[:300])
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


def _prune(token: str, contains: str) -> int:
    r = httpx.get("https://www.googleapis.com/drive/v3/files", params={
        "q": f"'{FOLDER_ID}' in parents and name contains '{contains}' and trashed=false",
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
    ts = _ts()
    artifacts = [_dump_db(ts), _archive_knowledge(ts)]
    try:
        for name, path in artifacts:
            res = _upload(token, name, path)
            mb = os.path.getsize(path) / 1e6
            print(f"backed up {res.get('name')} ({mb:.1f} MB, id {res.get('id')})")
        pruned = _prune(token, "cortex-db-") + _prune(token, "cortex-knowledge-")
        # also prune any legacy 'cortex-<ts>.sql.gz' files from before the rename
        print(f"pruned {pruned} old beyond KEEP={KEEP}")
    finally:
        for _, path in artifacts:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    main()
