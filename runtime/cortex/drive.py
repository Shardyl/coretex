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


def ensure_subfolder(parent_link_or_id: str, name: str, token: str | None = None) -> str:
    """Id of the named subfolder under parent, creating it if missing (drive.file scope covers creation)."""
    token = token or access_token()
    for f in list_folder(parent_link_or_id, token):
        if f.get("mimeType") == "application/vnd.google-apps.folder" and f.get("name") == name:
            return f["id"]
    r = httpx.post(f"{API}/files", params={"supportsAllDrives": "true"},
                   headers={"Authorization": f"Bearer {token}"},
                   json={"name": name, "mimeType": "application/vnd.google-apps.folder",
                         "parents": [folder_id(parent_link_or_id)]}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def upload(parent_id: str, filename: str, mime: str, data: bytes, token: str | None = None) -> str:
    """Upload one file into a folder; returns the new file id. Multipart, up to ~15MB."""
    token = token or access_token()
    meta = json.dumps({"name": filename, "parents": [folder_id(parent_id)]})
    files = {"metadata": ("metadata", meta, "application/json; charset=UTF-8"),
             "file": (filename, data, mime or "application/octet-stream")}
    r = httpx.post("https://www.googleapis.com/upload/drive/v3/files",
                   params={"uploadType": "multipart", "supportsAllDrives": "true"},
                   headers={"Authorization": f"Bearer {token}"}, files=files, timeout=180)
    r.raise_for_status()
    return r.json()["id"]


def ensure_client_folder(client_name: str, parent_id: str, token: str | None = None) -> dict:
    """Find-or-create the client's folder inside the SENSA CLIENTS shared-drive folder, refusing to
    create near-duplicates: matching is case-insensitive on the trimmed name, and close variants
    (the name contained in an existing folder or vice versa) are returned as `candidates` instead of
    silently creating a twin. Returns {id, name, created, candidates}."""
    tok = token or access_token()
    H = {"Authorization": f"Bearer {tok}"}
    q = {"q": f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
         "includeItemsFromAllDrives": "true", "supportsAllDrives": "true",
         "pageSize": 200, "fields": "files(id,name)"}
    r = httpx.get(f"{API}/files", params=q, headers=H, timeout=30)
    r.raise_for_status()
    existing = r.json().get("files", [])
    want = (client_name or "").strip()
    wl = want.lower()
    for f in existing:                                   # exact (case-insensitive) match wins
        if f["name"].strip().lower() == wl:
            return {"id": f["id"], "name": f["name"], "created": False, "candidates": []}
    near = [f for f in existing if wl in f["name"].lower() or f["name"].strip().lower() in wl]
    if near:                                             # near-duplicate: surface, never create a twin
        return {"id": None, "name": want, "created": False,
                "candidates": [{"id": f["id"], "name": f["name"]} for f in near]}
    c = httpx.post(f"{API}/files", params={"supportsAllDrives": "true"}, headers=H,
                   json={"name": want, "mimeType": "application/vnd.google-apps.folder",
                         "parents": [parent_id]}, timeout=30)
    c.raise_for_status()
    return {"id": c.json()["id"], "name": want, "created": True, "candidates": []}


def update_file(file_id: str, mime: str, data: bytes, token: str | None = None) -> str:
    """Replace an existing Drive file's content in place (same id, same name)."""
    tok = token or access_token()
    r = httpx.patch(f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
                    params={"uploadType": "media", "supportsAllDrives": "true"},
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": mime},
                    content=data, timeout=120)
    r.raise_for_status()
    return r.json()["id"]


def upsert_in_folder(folder_id_: str, filename: str, mime: str, data: bytes, token: str | None = None) -> str:
    """Update the file of this exact name in the folder if it exists, else create it — for living
    documents that keep one identity across iterations (e.g. the ALL VERSIONS quote workbook)."""
    tok = token or access_token()
    for f in list_folder(folder_id_, tok):
        if f.get("name") == filename and f.get("mimeType") != "application/vnd.google-apps.folder":
            return update_file(f["id"], mime, data, token=tok)
    return upload_to_folder(folder_id_, filename, mime, data, token=tok)


def upload_to_folder(folder_id_: str, filename: str, mime: str, data: bytes, token: str | None = None) -> str:
    """Upload bytes as a new file into a shared-drive folder; returns the file id."""
    tok = token or access_token()
    meta = json.dumps({"name": filename, "parents": [folder_id_]})
    crlf = b"\r\n"
    body = (b"--b" + crlf + b"Content-Type: application/json; charset=UTF-8" + crlf + crlf
            + meta.encode() + crlf + b"--b" + crlf + b"Content-Type: " + mime.encode() + crlf + crlf
            + data + crlf + b"--b--")
    r = httpx.post("https://www.googleapis.com/upload/drive/v3/files",
                   params={"uploadType": "multipart", "supportsAllDrives": "true"},
                   headers={"Authorization": f"Bearer {tok}",
                            "Content-Type": "multipart/related; boundary=b"},
                   content=body, timeout=120)
    r.raise_for_status()
    return r.json()["id"]
