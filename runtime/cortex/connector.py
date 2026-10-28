"""THE CLAUDE CONNECTOR (28 Oct 2026): a team member's own Claude (Claude Code desktop on their computer, the hello@
account Gino uses) reaches Cortex through ONE MCP endpoint, /api/connector/mcp, signed in as THEM with a long-lived
connector token. It is Talk by another door: every request runs the same Talk turn their cockpit runs, with their
company scope and their gates, so a deck, quotation, skill edit or document is exactly what Talk would produce.

Nothing here reaches code. The computer it runs on has no repo, no box key and no GitHub access; this endpoint is the
only way in, and it exposes three tools: talk to Cortex, download a library document, upload a file to the library.
Outbound still goes through approval cards; money stays behind the owner's step-up, as in Talk.

Mint / revoke a token on the box:
    sudo -u cortex /opt/coretex/.venv/bin/python -m cortex.connector mint gino@sensa.digital
    sudo -u cortex /opt/coretex/.venv/bin/python -m cortex.connector revoke gino@sensa.digital
"""
from __future__ import annotations

import hashlib
import secrets
import sys

from . import db

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFIX = "cxk_"
_schema_ok = False

INSTRUCTIONS = (
    "Cortex is Sensa Productions' operations system: it holds the live skills and rules, company profiles, rate card, "
    "CRM, opportunities, document library and media library, and it builds decks, proposals, storyboards and "
    "quotations. For ANY of that, use cortex_talk and ask in plain words, as you would ask Cortex in Talk. Never "
    "build a Sensa deck, proposal or quotation by hand: ask Cortex to make it, then download it with "
    "cortex_get_document. Cortex drafts and the owner approves anything that goes out.")

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
    {"name": "cortex_upload_document",
     "description": ("Save a file from this computer into the Cortex document library (a brief, a reference image, a "
                     "client document), so Talk can read it and file it on an opportunity. Pass the file as base64. "
                     "Returns its library id; tell Cortex that id in cortex_talk."),
     "inputSchema": {"type": "object", "properties": {
         "filename": {"type": "string"},
         "data_base64": {"type": "string", "description": "The file's bytes, base64 encoded."},
         "company": {"type": "string", "description": "Company slug, default sensa."},
         "kind": {"type": "string", "description": "Library kind, default client-document."}},
         "required": ["filename", "data_base64"]}},
]


class ToolError(Exception):
    pass


def ensure_schema() -> None:
    global _schema_ok
    if not _schema_ok:
        db.execute("alter table users add column if not exists connector_token_hash text")
        _schema_ok = True


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
    if name == "cortex_upload_document":
        d = h["upload"](user, str(a.get("filename") or "file"), str(a.get("data_base64") or ""),
                        (a.get("company") or "sensa"), (a.get("kind") or "client-document"))
        return f"Saved to the Cortex library as #{d['id']} '{d['filename']}'."
    raise ToolError(f"unknown tool {name}")


def handle(msg: dict, user: dict, h: dict) -> dict | None:
    """One JSON-RPC message -> its response (None for a notification). `h` holds the Talk-side handlers:
    talk(user, message, new, company) -> str, file_url(user, id) -> dict, upload(user, name, b64, company, kind) -> dict.
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
                         "serverInfo": {"name": "cortex", "version": "1.0"},
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
