"""Cortex's own Google Drive access (keyless OAuth — the SAME refresh token as the nightly backup).

Identity = whoever consented once at /oauth/google/start (rashad@sensa.digital), scope includes
`drive.readonly`, so the box can read any file/folder that account can see — including each company's
asset_folder, which lives INSIDE the Cortex Drive folder. This is the canonical way Cortex reads a
company's brand assets. NOTE: this is a DIFFERENT identity from the chat-side Drive connector
(hello@sensa.digital); always read company assets through here, not the connector.
"""
from __future__ import annotations

import json

import httpx

from . import config, db

API = "https://www.googleapis.com/drive/v3"
_CLIENT_PATH = config.get("GOOGLE_OAUTH_CLIENT") or "/etc/cortex/google_oauth_client.json"


def access_token() -> str:
    with open(_CLIENT_PATH) as f:
        c = json.load(f).get("web") or {}
    rt = db.setting_get("google_refresh_token")
    if not rt:
        raise RuntimeError("No google_refresh_token — authorise once at https://coretex.uk/oauth/google/start")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def folder_id(link_or_id: str) -> str:
    """Accept a full Drive folder URL or a bare id; return the id."""
    s = (link_or_id or "").strip()
    if "/folders/" in s:
        s = s.split("/folders/")[1]
    s = s.split("?")[0].split("/")[0]
    return s


def list_folder(link_or_id: str, token: str | None = None) -> list[dict]:
    token = token or access_token()
    fid = folder_id(link_or_id)
    r = httpx.get(f"{API}/files", params={
        "q": f"'{fid}' in parents and trashed=false",
        "fields": "files(id,name,mimeType,size,modifiedTime)",
        "pageSize": "200", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("files", [])


def download(file_id: str, token: str | None = None) -> bytes:
    token = token or access_token()
    r = httpx.get(f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                  headers={"Authorization": f"Bearer {token}"}, timeout=120)
    r.raise_for_status()
    return r.content
