"""THE CLAUDE CONNECTOR (28 Oct 2026): a team member's own Claude (Claude Code desktop on their computer, the hello@
account Gino uses) reaches Cortex through ONE MCP endpoint, /api/connector/mcp, signed in as THEM with a long-lived
connector token. It is Talk by another door: every request runs the same Talk turn their cockpit runs, with their
company scope and their gates, so a deck, quotation, skill edit or document is exactly what Talk would produce.

Nothing here reaches code. The computer it runs on has no repo, no box key and no GitHub access; this endpoint is the
only way in. Its tools: talk to Cortex, download a library document, upload a file (a document into the library, or a
film to the company's YouTube channel and media library), follow an upload, read the YouTube guide, and change a
film's details. Outbound still goes through approval cards; money stays behind the owner's step-up, as in Talk.

UPLOADS GO BY LINK, NEVER THROUGH THE MODEL (29 Oct 2026). The first version took the file as base64 inside the tool
call, so the model had to type the whole file out: fine for a 2 KB note, impossible for a 5 MB deck or a film. Now
`cortex_upload_link` opens a ticket (`connector_uploads`) and returns a signed URL plus a PowerShell snippet that sends
the file from disk in 32 MB parts (under Cloudflare's 100 MB request ceiling, resumable: a rerun asks the box how far
it got). Finishing a document files it in the library, under its client and deal; finishing a film queues the
YouTube job (youtube.py), which the ENGINE runs.

Mint / revoke a token on the box:
    sudo -u cortex /opt/coretex/.venv/bin/python -m cortex.connector mint gino@sensa.digital
    sudo -u cortex /opt/coretex/.venv/bin/python -m cortex.connector revoke gino@sensa.digital
"""
from __future__ import annotations

import hashlib
import secrets
import sys

from psycopg.types.json import Json

from . import db

UPLOAD_DIR = "/var/tmp/cortex-connector"      # real disk: /tmp is a 3.8 GB tmpfs and a film would fill it
UPLOAD_TTL_H = 24
DOC_CAP = 40_000_000
VIDEO_CAP = 20_000_000_000
PRIVACIES = ("private", "unlisted", "public")

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFIX = "cxk_"
_schema_ok = False

INSTRUCTIONS = (
    "Cortex is Sensa Productions' operations system: it holds the live skills and rules, company profiles, rate card, "
    "CRM, opportunities, document library and media library, and it builds decks, proposals, storyboards and "
    "quotations. For ANY of that, use cortex_talk and ask in plain words, as you would ask Cortex in Talk. Never "
    "build a Sensa deck, proposal or quotation by hand: ask Cortex to make it, then download it with "
    "cortex_get_document. A file from this computer (a brief, a concept document, a film) goes up with "
    "cortex_upload_link. Cortex drafts and the owner approves anything that goes out.")

TOOLS = [
    {"name": "cortex_talk",
     "description": ("Send one message to Cortex Talk, as the signed-in team member, and get its reply. Use it to read "
                     "skills, rules and company profiles; search the CRM, opportunities and document library; make or "
                     "revise decks, proposals, storyboards and quotations; save formats; add or change skill rules. "
                     "The conversation carries on between calls (it starts fresh after a day of quiet, or when "
                     "new_conversation is true). Long jobs such as a deck can take several minutes."),
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "What to ask or tell Cortex, in plain words."},
         "new_conversation": {"type": "boolean", "description": "Start a fresh Talk conversation."},
         "company": {"type": "string", "description": "Company slug in focus, e.g. sensa or skyvision. Optional."}},
         "required": ["message"]}},
    {"name": "cortex_get_document",
     "description": ("Get a download link for a document in the Cortex library by its id (the #number Cortex gives). "
                     "The link works for 30 minutes without signing in: download it with curl or Invoke-WebRequest "
                     "and save the file to the user's Desktop."),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "The library document id."}}, "required": ["id"]}},
    {"name": "cortex_upload_link",
     "description": ("Get a link to send a file from this computer to Cortex, then run the PowerShell it returns. "
                     "what='document': a brief, concept, deck or reference file goes into the document library "
                     "(and the client's Drive folder), filed under its client and opportunity so Cortex can attach "
                     "it to an email draft; up to 40 MB. what='video': a film goes to the company's YouTube channel "
                     "as private or unlisted AND into the Cortex media library, transcribed and profiled; read "
                     "cortex_youtube_guide first for the title and description rules and the library categories. "
                     "Then follow it with cortex_upload_status."),
     "inputSchema": {"type": "object", "properties": {
         "what": {"type": "string", "enum": ["document", "video"]},
         "filename": {"type": "string", "description": "The file's name, e.g. 'Brandgate concept v2.pdf'."},
         "company": {"type": "string", "description": "Company slug: sensa (default) or skyvision."},
         "client": {"type": "string", "description": "The client it belongs to, e.g. 'Brandgate'."},
         "deal_id": {"type": "integer", "description": "The opportunity number, when it belongs to one."},
         "kind": {"type": "string", "description": "document only: library kind, default client-document."},
         "title": {"type": "string", "description": "video only: the YouTube title, per the guide."},
         "description": {"type": "string", "description": "video only: the YouTube description, per the guide."},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "video only: YouTube tags."},
         "privacy": {"type": "string", "enum": ["unlisted", "private"],
                     "description": "video only, default unlisted. Making a film public is an approval card."},
         "categories": {"type": "array", "items": {"type": "string"},
                        "description": "video only: media library categories, from the guide."},
         "content_type": {"type": "string", "description": "video only: library type, default client-film."},
         "ai_production": {"type": "boolean", "description": "video only: the film was made with AI."}},
         "required": ["what", "filename"]}},
    {"name": "cortex_upload_status",
     "description": ("Where an upload has got to: for a document its library #id; for a film the step it is on, then "
                     "its YouTube link and media library entry. A film takes a few minutes after the file arrives."),
     "inputSchema": {"type": "object", "properties": {
         "upload_id": {"type": "integer"}}, "required": ["upload_id"]}},
    {"name": "cortex_youtube_guide",
     "description": ("The live rules for YouTube titles and descriptions, the media library's categories and types, "
                     "and the channel, for one company. Read it before uploading or renaming a film."),
     "inputSchema": {"type": "object", "properties": {
         "company": {"type": "string", "description": "Company slug: sensa (default) or skyvision."}}}},
    {"name": "cortex_video_update",
     "description": ("Change a film's details on YouTube and in the media library, by its YouTube video id: title, "
                     "description, tags, privacy (private/unlisted), client, categories, type, ai_production. Asking "
                     "for public, or changing a film that is ALREADY public, makes an approval card for the owner "
                     "instead of changing it. Ratings are the owner's and cannot be set here."),
     "inputSchema": {"type": "object", "properties": {
         "video_id": {"type": "string"},
         "title": {"type": "string"}, "description": {"type": "string"},
         "tags": {"type": "array", "items": {"type": "string"}},
         "privacy": {"type": "string", "enum": ["private", "unlisted", "public"]},
         "client": {"type": "string"},
         "categories": {"type": "array", "items": {"type": "string"}},
         "content_type": {"type": "string"},
         "ai_production": {"type": "boolean"}},
         "required": ["video_id"]}},
]


class ToolError(Exception):
    pass


def ensure_schema() -> None:
    global _schema_ok
    if not _schema_ok:
        db.execute("alter table users add column if not exists connector_token_hash text")
        db.execute("create table if not exists connector_uploads (id bigserial primary key, user_email text not null, "
                   "company_id int not null, kind text not null, meta jsonb not null default '{}', path text, "
                   "size bigint not null default 0, status text not null default 'open', result jsonb, error text, "
                   "created_at timestamptz default now(), updated_at timestamptz default now())")
        _schema_ok = True


def new_ticket(user: dict, company_id: int, kind: str, meta: dict) -> dict:
    """An upload ticket: the file arrives in parts at <UPLOAD_DIR>/<id>.part."""
    import os
    ensure_schema()
    os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
    t = db.execute("insert into connector_uploads (user_email, company_id, kind, meta) values (%s,%s,%s,%s) "
                   "returning *", (user["email"], company_id, kind, Json(meta)))
    return db.execute("update connector_uploads set path=%s where id=%s returning *",
                      (f"{UPLOAD_DIR}/{t['id']}.part", t["id"]))


def ticket(uid: int) -> dict | None:
    ensure_schema()
    return db.one("select * from connector_uploads where id=%s", (uid,))


def job_update(uid: int, status: str | None = None, result: dict | None = None, error: str | None = None) -> None:
    sets, vals = ["updated_at=now()"], []
    if status:
        sets.append("status=%s"); vals.append(status)
    if result is not None:
        sets.append("result=%s"); vals.append(Json(result))
    if error is not None:
        sets.append("error=%s"); vals.append(error)
    db.execute(f"update connector_uploads set {', '.join(sets)} where id=%s", (*vals, uid))


def push_script(put_url: str, done_url: str) -> str:
    """PowerShell (Windows PowerShell 5.1) that sends one file in 32 MB parts and resumes where it stopped."""
    return ("$file = '<FULL PATH TO THE FILE>'\n"
            f"$url = '{put_url}'\n"
            f"$done = '{done_url}'\n"
            "$off = [int64](Invoke-RestMethod -Uri $url).size\n"
            "$fs = [IO.File]::OpenRead($file); [void]$fs.Seek($off, 0); $buf = New-Object byte[] (32MB)\n"
            "try { while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) {\n"
            "  $part = New-Object byte[] $n; [Array]::Copy($buf, $part, $n)\n"
            "  Invoke-RestMethod -Method Put -Uri \"$url&offset=$off\" -Body $part "
            "-ContentType 'application/octet-stream' | Out-Null\n"
            "  $off += $n; Write-Host ('{0:N0} MB sent' -f ($off / 1MB)) } } finally { $fs.Close() }\n"
            "Invoke-RestMethod -Method Post -Uri $done | ConvertTo-Json")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(email: str) -> str:
    """A new connector token for this team member (any earlier one stops working). Shown once, stored hashed."""
    ensure_schema()
    u = db.one("select id from users where lower(email)=lower(%s) and active", (email.strip(),))
    if not u:
        raise ValueError(f"no active Cortex user {email}")
    token = PREFIX + secrets.token_urlsafe(32)
    db.execute("update users set connector_token_hash=%s where id=%s", (_hash(token), u["id"]))
    return token


def revoke(email: str) -> bool:
    ensure_schema()
    r = db.one("update users set connector_token_hash=null where lower(email)=lower(%s) returning id", (email.strip(),))
    return bool(r)


def user_for(token: str) -> dict | None:
    """The team member a connector token belongs to (active users only). The owner has no connector token: the
    owner works in his own Claude with the box itself."""
    if not token or not token.startswith(PREFIX) or len(token) < 30:
        return None
    ensure_schema()
    return db.one("select id, name, email, role, companies, active from users "
                  "where connector_token_hash=%s and active", (_hash(token),))


def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _call(name: str, a: dict, user: dict, h: dict) -> str:
    if name == "cortex_talk":
        msg = str(a.get("message") or "").strip()
        if not msg:
            raise ToolError("message is empty")
        return h["talk"](user, msg, bool(a.get("new_conversation")), (a.get("company") or None)) or "(no reply)"
    if name == "cortex_get_document":
        try:
            did = int(a.get("id"))
        except (TypeError, ValueError):
            raise ToolError("id must be a number")
        d = h["file_url"](user, did)
        return f"{d['filename']} ({d['size']} bytes, {d['mime']})\nDownload (valid 30 minutes): {d['url']}"
    if name == "cortex_upload_link":
        what = str(a.get("what") or "").strip().lower()
        if what not in ("document", "video"):
            raise ToolError("what must be 'document' or 'video'")
        if not str(a.get("filename") or "").strip():
            raise ToolError("filename is required")
        if what == "video" and not str(a.get("title") or "").strip():
            raise ToolError("a film needs its YouTube title (read cortex_youtube_guide for the rules)")
        if what == "video" and (a.get("privacy") or "unlisted") not in ("unlisted", "private"):
            raise ToolError("a film goes up unlisted or private; ask for public afterwards with cortex_video_update")
        d = h["upload_link"](user, what, a)
        return (f"Upload #{d['id']} is open for 24 hours ({what}, {d['company']}). Run this PowerShell with the real "
                f"path filled in; run it again to resume if it stops:\n\n{d['script']}\n\n"
                f"Then check it with cortex_upload_status upload_id={d['id']}.")
    if name == "cortex_upload_status":
        try:
            uid = int(a.get("upload_id"))
        except (TypeError, ValueError):
            raise ToolError("upload_id must be a number")
        return h["upload_status"](user, uid)
    if name == "cortex_youtube_guide":
        return h["youtube_guide"](user, (a.get("company") or "sensa"))
    if name == "cortex_video_update":
        vid = str(a.get("video_id") or "").strip()
        if not vid:
            raise ToolError("video_id is required")
        if a.get("privacy") and a["privacy"] not in PRIVACIES:
            raise ToolError("privacy must be private, unlisted or public")
        if "rating" in a:
            raise ToolError("ratings are the owner's alone")
        return h["video_update"](user, vid, a)
    raise ToolError(f"unknown tool {name}")


def handle(msg: dict, user: dict, h: dict) -> dict | None:
    """One JSON-RPC message -> its response (None for a notification). `h` holds the Talk-side handlers:
    talk(user, message, new, company) -> str, file_url(user, id) -> dict, upload_link(user, what, args) -> dict,
    upload_status(user, id) -> str, youtube_guide(user, company) -> str, video_update(user, video_id, args) -> str.
    A handler raises ToolError (or ValueError) for a refusal the caller should read."""
    if not isinstance(msg, dict):
        return _err(None, -32600, "invalid request")
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None
    p = msg.get("params") or {}
    if method == "initialize":
        want = p.get("protocolVersion")
        return _ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "cortex", "version": "1.1"},
                         "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        try:
            text = _call(p.get("name"), p.get("arguments") or {}, user, h)
            return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except (ToolError, ValueError) as e:
            return _ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # noqa: BLE001 - a crash is reported to the caller, never a dropped connection
            print(f"[connector] {p.get('name')} failed: {type(e).__name__}: {e}", flush=True)
            return _ok(mid, {"content": [{"type": "text", "text": f"Cortex error: {type(e).__name__}: {e}"}],
                             "isError": True})
    return _err(mid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("mint", "revoke"):
        print("usage: python -m cortex.connector mint|revoke <email>")
        sys.exit(2)
    if sys.argv[1] == "mint":
        print(mint(sys.argv[2]))
    else:
        print("revoked" if revoke(sys.argv[2]) else "no such user")
