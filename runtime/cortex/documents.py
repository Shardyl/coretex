"""Company document library — the standing files Cortex can attach to outgoing email.

CANONICAL HOME: the company's Drive `<COMPANY> CORTEX/Documents/` subfolder (decided 2026-08-26 —
the same Drive-first doctrine as the brand kit: the Drive folder is the controlled source). The box
copy under /opt/cortex-knowledge/documents/<slug>/ is a CACHE so send-time attachment is instant and
survives a Drive outage; the registry is `company_documents` (drive_id links the canonical file).
Uploads through Cortex land in Drive first, then the cache. Drafts and sends reference documents by
id — bytes never live in a task row.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re

from . import config, db, store

DOCS_DIR = config.get("CORTEX_DOCUMENTS_DIR") or "/opt/cortex-knowledge/documents"
MAX_BYTES = 15_000_000

_MIGRATE = """
create table if not exists company_documents (
  id bigserial primary key,
  company_id bigint references companies(id) on delete cascade,
  kind text not null default 'document',          -- trade-licence | vat-certificate | company-profile | ...
  filename text not null,
  mime text not null default 'application/octet-stream',
  size bigint not null default 0,
  path text not null,
  sha256 text not null,
  uploaded_by text,
  created_at timestamptz not null default now()
);
create index if not exists idx_company_documents_company on company_documents(company_id);
alter table company_documents add column if not exists drive_id text;   -- canonical Drive file id
"""


def _drive_docs_folder(company_id: int, slug: str) -> str | None:
    """The company's canonical Documents folder on Drive (created under its CORTEX asset_folder on
    first use). None when the company has no asset_folder configured — cache-only then, flagged."""
    from . import drive, profile
    link = (profile.get(company_id) or {}).get("asset_folder")
    if not link:
        return None
    return drive.ensure_subfolder(link, "Documents")


def push_to_drive(doc: dict) -> str | None:
    """Upload a cached document to its canonical Drive home; records + returns the drive_id."""
    from . import drive
    co = store.get_company(doc["company_id"]) or {}
    fid = _drive_docs_folder(doc["company_id"], co.get("slug") or "")
    if not fid:
        return None
    did = drive.upload(fid, doc["filename"], doc["mime"], read_bytes(doc))
    db.execute("update company_documents set drive_id=%s where id=%s", (did, doc["id"]))
    return did


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "document")
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120] or "document"


def save(company_id: int, slug: str, filename: str, mime: str, data: bytes,
         kind: str = "document", uploaded_by: str | None = None, push: bool = True) -> dict:
    """Store the file on disk + register it. A byte-identical re-upload returns the existing row
    (idempotent — 'checking if it has the trade licence' never creates duplicates)."""
    ensure_schema()
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_BYTES:
        raise ValueError(f"file too large ({len(data)} bytes; max {MAX_BYTES})")
    sha = hashlib.sha256(data).hexdigest()
    dup = db.one("select * from company_documents where company_id=%s and sha256=%s", (company_id, sha))
    if dup:
        return dup
    d = os.path.join(DOCS_DIR, slug or f"company-{company_id}")
    os.makedirs(d, exist_ok=True)
    fn = _safe_name(filename)
    path = os.path.join(d, f"{sha[:12]}-{fn}")
    with open(path, "wb") as f:
        f.write(data)
    row = db.execute(
        "insert into company_documents (company_id, kind, filename, mime, size, path, sha256, uploaded_by) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (company_id, (kind or "document").strip().lower(), fn,
         mime or "application/octet-stream", len(data), path, sha, uploaded_by))
    if push:
        try:                    # canonical copy -> the company's Drive Documents folder (cache stays local)
            push_to_drive(row)
        except Exception:  # noqa: BLE001 — a Drive hiccup never blocks the save; sync_drive() catches up later
            pass
    return db.one("select * from company_documents where id=%s", (row["id"],))


def save_data_url(company_id: int, slug: str, filename: str, data_url: str,
                  kind: str = "document", uploaded_by: str | None = None) -> dict:
    if not isinstance(data_url, str) or ";base64," not in data_url:
        raise ValueError("expected a data: URL")
    head, b64 = data_url.split(";base64,", 1)
    return save(company_id, slug, filename, head[5:] or "application/octet-stream",
                base64.b64decode(b64), kind=kind, uploaded_by=uploaded_by)


def listing(company_id: int) -> list[dict]:
    ensure_schema()
    return db.query("select id, kind, filename, mime, size, created_at from company_documents "
                    "where company_id=%s order by kind, created_at desc", (company_id,))


def get(doc_id: int, company_id: int | None = None) -> dict | None:
    ensure_schema()
    r = db.one("select * from company_documents where id=%s", (doc_id,))
    if r and company_id is not None and r["company_id"] != company_id:
        return None                              # never serve another company's document
    return r


def read_bytes(doc: dict) -> bytes:
    with open(doc["path"], "rb") as f:
        return f.read()


def sync_drive(company_id: int) -> dict:
    """Two-way catch-up with the canonical Drive Documents folder: push cached rows that lack a
    drive_id, and pull files someone dropped into the folder by hand. Idempotent by sha256/name."""
    from . import drive
    co = store.get_company(company_id) or {}
    fid = _drive_docs_folder(company_id, co.get("slug") or "")
    if not fid:
        return {"reason": "no asset_folder configured"}
    pushed = pulled = 0
    for r in db.query("select * from company_documents where company_id=%s and drive_id is null", (company_id,)):
        try:
            if push_to_drive(r):
                pushed += 1
        except Exception:  # noqa: BLE001
            pass
    have = {r["filename"] for r in listing(company_id)}
    tok = drive.access_token()
    for f in drive.list_folder(fid, tok):
        if f.get("mimeType") == "application/vnd.google-apps.folder" or f.get("name") in have:
            continue
        try:
            data = drive.download(f["id"], tok)
            d = save(company_id, co.get("slug") or "", f["name"],
                     f.get("mimeType") or "application/octet-stream", data, uploaded_by="drive-sync",
                     push=False)   # it came FROM Drive — record its id, never re-upload a duplicate
            db.execute("update company_documents set drive_id=%s where id=%s and drive_id is null",
                       (f["id"], d["id"]))
            pulled += 1
        except Exception:  # noqa: BLE001
            pass
    return {"pushed": pushed, "pulled": pulled}


def find(company_id: int, query: str) -> list[dict]:
    """Loose match on kind/filename ('trade licence' finds kind trade-licence and 'TradeLicense2026.pdf')."""
    ensure_schema()
    toks = [t for t in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(t) > 2]
    rows = listing(company_id)
    if not toks:
        return rows
    def score(r):
        hay = (r["kind"] + " " + r["filename"]).lower().replace("license", "licence")
        return sum(1 for t in toks if t.replace("license", "licence") in hay)
    scored = [(score(r), r) for r in rows]
    best = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]
    return best or []
