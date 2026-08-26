"""Company document library — the standing files Cortex can attach to outgoing email.

Trade licence, VAT certificate, company profile, signed forms: uploaded once (Talk paperclip or a
card's paperclip), stored on the box under /opt/cortex-knowledge/documents/<company-slug>/ (inside
the nightly Drive backup), registered in `company_documents` (in the nightly DB dump). Drafts and
sends reference documents by id — bytes never live in a task row.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re

from . import config, db

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
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "document")
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120] or "document"


def save(company_id: int, slug: str, filename: str, mime: str, data: bytes,
         kind: str = "document", uploaded_by: str | None = None) -> dict:
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
    return db.execute(
        "insert into company_documents (company_id, kind, filename, mime, size, path, sha256, uploaded_by) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (company_id, (kind or "document").strip().lower(), fn,
         mime or "application/octet-stream", len(data), path, sha, uploaded_by))


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
