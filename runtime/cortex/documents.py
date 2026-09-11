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
alter table company_documents add column if not exists drive_md5 text;  -- Drive's checksum when cached
alter table company_documents add column if not exists client text;     -- the client folder it lives in
alter table company_documents add column if not exists verified_at timestamptz;
alter table company_documents add column if not exists superseded_by bigint; -- retired: kept, never offered
"""


def _drive_docs_folder(company_id: int, slug: str) -> str | None:
    """The company's canonical Documents folder on Drive (created under its CORTEX asset_folder on
    first use). None when the company has no asset_folder configured — cache-only then, flagged."""
    from . import drive, profile
    link = (profile.get(company_id) or {}).get("asset_folder")
    if not link:
        return None
    return drive.ensure_subfolder(link, "Documents")


_OFFICIAL_KINDS = {"company-profile", "trade-licence", "vat-certificate", "capabilities-deck", "terms"}
_OFFICIAL_NAME = re.compile(r"^(Sensa( Productions)?|Sky ?Vision)\b", re.I)


def _is_official(doc: dict) -> bool:
    """One of the company's OWN standing documents (profile, licence, VAT, capabilities deck, terms,
    templates), as opposed to work for a client."""
    return not (doc.get("client") or "").strip() and (
        (doc.get("kind") or "") in _OFFICIAL_KINDS or bool(_OFFICIAL_NAME.match(doc.get("filename") or "")))


_TERMS_SET = re.compile(r"\b(Terms|Rate Card|Quotation Template)\b", re.I)
_VERSION = re.compile(r"\s+v(\d+(?:\.\d+)*)(?=\.[^.]+$)")


def _version(name: str) -> tuple:
    m = _VERSION.search(name or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _file_terms_set(doc: dict, folder: str, archive: str, tok: str) -> str:
    """File one of the company's standing contract documents (terms, a terms module, a quotation template,
    the rate card) in the TERMS FOLDER: the current version at the top, every LOWER version of the same
    document into the archive subfolder. Nothing is ever deleted; a person's file is never moved."""
    from . import drive
    name = doc["filename"]
    did = drive.upsert_in_folder(folder, name, doc["mime"], read_bytes(doc), token=tok)
    archive_lower_versions(folder, name, archive, tok)
    return did


def archive_lower_versions(folder: str, name: str, archive: str, tok: str) -> int:
    """Move every LOWER version of the document `name` (same name apart from ' vX.Y') out of the top of
    `folder` into `archive` (or <folder>/Archive). Cortex can only move its own files. Returns the count."""
    from . import drive
    stem, ver, n_moved = _VERSION.sub("", name), _version(name), 0
    if not ver:
        return 0
    for f in drive.list_folder(folder, tok):
        n = f.get("name") or ""
        if n != name and f.get("mimeType") != "application/vnd.google-apps.folder" \
                and _VERSION.sub("", n) == stem and _version(n) and _version(n) < ver:
            try:
                drive.move_file(f["id"], folder, archive or drive.ensure_subfolder(folder, "Archive", tok), tok)
                n_moved += 1
            except Exception:  # noqa: BLE001 - not Cortex's file: leave it where it is
                pass
    return n_moved


def push_to_drive(doc: dict) -> str | None:
    """Upload a cached document to its ONE Drive home; records + returns the drive_id (owner, 11 Sep 2026):
    - a CLIENT's document goes to that client's folder under the clients drive folder;
    - the company's standing CONTRACT documents (terms, terms modules, quotation templates, the rate card)
      go to the profile's `terms_drive_folder`, older versions into `terms_archive_folder`. They are NOT
      final documents and never belong in Documents;
    - the company's own OFFICIAL document goes to <CORTEX>/Documents, and the previous copy of the same
      file moves into Documents/Archive, so the folder only ever shows the current version;
    - anything else stays in the library (and the nightly backup) and is NOT dumped into Documents.
    Documents used to receive every file saved, client drafts included, which is how it became a pile
    of 122 files with the same quotation template exported ten times."""
    from . import drive, profile
    client = (doc.get("client") or "").strip()
    official = _is_official(doc)
    if not client and not official:
        return None                      # decided before any Drive call: sync_drive retries these hourly
    co = store.get_company(doc["company_id"]) or {}
    prof = profile.get(doc["company_id"]) or {}
    terms_folder = (prof.get("terms_drive_folder") or "").strip()
    tok = drive.access_token()
    if not client and terms_folder and _TERMS_SET.search(doc.get("filename") or ""):
        did = _file_terms_set(doc, terms_folder, (prof.get("terms_archive_folder") or "").strip(), tok)
    elif client:
        parent = ((profile.get(doc["company_id"]) or {}).get("clients_drive_folder") or "").strip()
        f = drive.ensure_client_folder(client, parent, token=tok) if parent else {}
        if not f.get("id"):
            return None                  # no clients folder, or near-duplicate client folders: never guess
        did = drive.upload_to_folder(f["id"], doc["filename"], doc["mime"], read_bytes(doc), token=tok)
    else:
        fid = _drive_docs_folder(doc["company_id"], co.get("slug") or "")
        if not fid:
            return None
        for old in drive.list_folder(fid, tok):       # the previous copy of this document -> Archive
            if old.get("name") == doc["filename"] and old.get("mimeType") != "application/vnd.google-apps.folder":
                try:
                    drive.move_file(old["id"], fid, drive.ensure_subfolder(fid, "Archive", tok), tok)
                except Exception:  # noqa: BLE001 - not Cortex's copy: leave it where it is
                    pass
        did = drive.upload(fid, doc["filename"], doc["mime"], read_bytes(doc), token=tok)
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


# The STANDING company papers - the only documents offered by default when attaching to a card
# (owner, 30 Aug: everything else is project clutter and Cortex attaches what a draft needs itself).
# our own standing documents: they belong to no client, so a deal's document scope never hides them
CORE_KINDS = ("company-profile", "trade-licence", "vat-certificate", "capabilities-deck")


def listing(company_id: int, core_only: bool = False) -> list[dict]:
    """The documents Cortex may OFFER (list, search, attach). A superseded row is left out: it stays on
    record, and any card that already references it by id still resolves through get()."""
    ensure_schema()
    if core_only:
        return db.query("select id, kind, filename, mime, size, created_at, drive_id, client "
                        "from company_documents where company_id=%s and kind = any(%s) "
                        "and superseded_by is null order by kind, created_at desc",
                        (company_id, list(CORE_KINDS)))
    return db.query("select id, kind, filename, mime, size, created_at, drive_id, client "
                    "from company_documents where company_id=%s and superseded_by is null "
                    "order by kind, created_at desc", (company_id,))


def supersede(doc_ids: list, by_id: int) -> list:
    """Retire earlier versions of a document in favour of `by_id`: kept on record (and on Drive), never
    offered for search or attachment again. Nothing is deleted. Returns the ids retired."""
    ensure_schema()
    return [r["id"] for r in db.query(
        "update company_documents set superseded_by=%s where id = any(%s) and id <> %s returning id",
        (int(by_id), [int(i) for i in doc_ids], int(by_id)))]


def rename(doc_id: int, new_name: str) -> dict:
    """Rename a library document EVERYWHERE Cortex knows it by name (owner, 11 Sep 2026): the library row,
    its Drive file (when Cortex created it), and Talk's own taught notes (setting chat_self_rules). The AI
    capabilities deck was renamed in the library and on Drive, but two of Talk's notes still named it by
    its old file name, so Talk kept calling it that. Skill rules that mention the old name are REPORTED,
    never rewritten: those are the owner's words."""
    ensure_schema()
    doc = db.one("select * from company_documents where id=%s", (int(doc_id),))
    if not doc:
        raise ValueError(f"no library document #{doc_id}")
    old = doc["filename"]
    ext = os.path.splitext(old)[1]
    new = _safe_name((new_name or "").strip())
    if not (new_name or "").strip():
        raise ValueError("give the new name")
    if ext and not new.lower().endswith(ext.lower()):
        new += ext
    drive_note = "renamed"
    if doc.get("drive_id"):
        try:
            import httpx
            from . import drive
            r = httpx.patch(f"{drive.API}/files/{doc['drive_id']}", params={"supportsAllDrives": "true"},
                            headers={"Authorization": f"Bearer {drive.access_token()}"}, json={"name": new},
                            timeout=30)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - not Cortex's file, or Drive blinked: say so
            drive_note = f"NOT renamed ({str(e)[:80]}); the library name changed"
    else:
        drive_note = "no Drive file"
    db.execute("update company_documents set filename=%s where id=%s", (new, doc["id"]))
    old_stem, new_stem = os.path.splitext(old)[0], os.path.splitext(new)[0]
    notes, updated = db.setting_get("chat_self_rules") or [], 0
    out = []
    for n in notes:
        m = str(n).replace(old, new).replace(old_stem, new_stem)   # full file name first, then the bare stem
        updated += m != n
        out.append(m)
    if updated:
        db.setting_set("chat_self_rules", out)
    rules = [f"{r['skill_key']} (company {r['company_id']})" for r in db.query(
        "select skill_key, company_id from skills where rules::text ilike %s or coalesce(craft,'') ilike %s",
        (f"%{old_stem}%", f"%{old_stem}%"))]
    return {"old": old, "new": new, "drive": drive_note, "notes_updated": updated, "rules_mentioning_old": rules}


def get(doc_id: int, company_id: int | None = None) -> dict | None:
    ensure_schema()
    r = db.one("select * from company_documents where id=%s", (doc_id,))
    if r and company_id is not None and r["company_id"] != company_id:
        return None                              # never serve another company's document
    return r


def read_bytes(doc: dict) -> bytes:
    """The document's bytes for a send. DRIVE IS THE SOURCE OF TRUTH: when the row has a drive_id we
    fetch the CURRENT file, so what goes out is what the folder holds now. The box copy is a cache and
    a fallback for a Drive outage - a send must never fail because Drive blinked (owner, 31 Aug)."""
    doc = _full(doc)
    if doc.get("drive_id"):
        try:
            from . import drive
            data = drive.download(doc["drive_id"])
            try:    # refresh the cache so an outage still has something current to fall back on
                with open(doc["path"], "wb") as fh:
                    fh.write(data)
                db.execute("update company_documents set sha256=%s, size=%s, verified_at=now() "
                           "where id=%s", (hashlib.sha256(data).hexdigest(), len(data), doc["id"]))
            except Exception:  # noqa: BLE001
                pass
            return data
        except Exception:  # noqa: BLE001 - fall through to the cached copy
            pass
    with open(doc["path"], "rb") as f:
        return f.read()


def _full(doc: dict) -> dict:
    """Search results are metadata-only; anything touching Drive or bytes needs the whole row."""
    if doc.get("path") and "drive_id" in doc:
        return doc
    return db.one("select * from company_documents where id=%s", (doc.get("id"),)) or doc


def card_ref(doc: dict) -> dict:
    """The reference put on a card's attach_docs, carrying the Drive checksum PIN so the send can tell
    whether the canonical file changed after the owner approved it."""
    doc = _full(doc)
    st = drive_state(doc) or {}
    return {"id": doc["id"], "filename": doc["filename"], "mime": doc["mime"], "size": doc["size"],
            **({"pin_md5": st["md5"]} if st.get("md5") else {})}


def drive_state(doc: dict) -> dict | None:
    """The canonical file's identity right now: {md5, name, modified} - or None if it is gone/unreadable.
    Used to PIN what the owner approved: if the Drive file changes between approval and send, the card
    stops instead of quietly sending a different document."""
    doc = _full(doc)
    if not doc.get("drive_id"):
        return None
    try:
        import httpx
        from . import drive
        r = httpx.get(f"{drive.API}/files/{doc['drive_id']}",
                      params={"fields": "id,name,md5Checksum,modifiedTime,trashed",
                              "supportsAllDrives": "true"},
                      headers={"Authorization": f"Bearer {drive.access_token()}"}, timeout=20)
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("trashed"):
            return None
        return {"md5": j.get("md5Checksum") or "", "name": j.get("name") or "",
                "modified": j.get("modifiedTime") or ""}
    except Exception:  # noqa: BLE001
        return None


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
    # every name on record, superseded ones included, so a retired file is never pulled back in as new
    have = {r["filename"] for r in db.query("select filename from company_documents where company_id=%s",
                                            (company_id,))}
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
    pulled += _sync_client_folders(company_id, co.get("slug") or "", tok)
    return {"pushed": pushed, "pulled": pulled}


def _sync_client_folders(company_id: int, slug: str, tok: str) -> int:
    """Index the PER-CLIENT folders too (`<clients_drive_folder>/<Client Name>/`) - that is where the
    quotation/proposal generator files its work, and it is a DIFFERENT store from the flat Documents
    folder. Until this existed the library could not see a client's own quotation: the ChainX card was
    asked to attach 'the accompanying quotation', found nothing under ChainX, and matched another
    client's file instead (31 Aug 2026). Only real deliverables are indexed (pdf/xlsx/docx)."""
    from . import drive, profile
    parent = ((profile.get(company_id) or {}).get("clients_drive_folder") or "").strip()
    if not parent:
        return 0
    # every name on record, superseded ones included, so a retired file is never pulled back in as new
    have = {r["filename"] for r in db.query("select filename from company_documents where company_id=%s",
                                            (company_id,))}
    n = 0
    try:
        folders = [f for f in drive.list_folder(parent, tok)
                   if f.get("mimeType") == "application/vnd.google-apps.folder"]
    except Exception:  # noqa: BLE001
        return 0
    for cf in folders[:120]:
        try:
            files = drive.list_folder(cf["id"], tok)
        except Exception:  # noqa: BLE001
            continue
        for f in files:
            name = f.get("name") or ""
            if (f.get("mimeType") == "application/vnd.google-apps.folder" or name in have
                    or not name.lower().endswith((".pdf", ".xlsx", ".docx"))):
                continue
            try:
                data = drive.download(f["id"], tok)
                kind = ("quotation" if "quotation" in name.lower()
                        else "proposal" if "proposal" in name.lower() else "client-file")
                d = save(company_id, slug, name, f.get("mimeType") or "application/octet-stream",
                         data, uploaded_by=f"drive-client:{cf['name']}", kind=kind, push=False)
                db.execute("update company_documents set drive_id=%s where id=%s and drive_id is null",
                           (f["id"], d["id"]))
                have.add(name)
                n += 1
            except Exception:  # noqa: BLE001
                continue
    return n


def sync_all(min_gap_minutes: int = 60) -> dict:
    """Reconcile EVERY company's library with its Drive Documents folder, at most once per gap.
    Called from the engine loop, so a file hand-dropped into any CORTEX/Documents folder is in the
    library within the hour — no command needed."""
    import time
    last = db.setting_get("documents_sync_ts") or 0
    if time.time() - float(last) < min_gap_minutes * 60:
        return {"skipped": True}
    db.setting_set("documents_sync_ts", time.time())
    out = {}
    for c in db.query("select id, slug from companies order by id"):
        try:
            out[c["slug"]] = sync_drive(c["id"])
        except Exception as e:  # noqa: BLE001 — one company's Drive hiccup never blocks the rest
            out[c["slug"]] = {"error": str(e)[:80]}
    return out


def find(company_id: int, query: str, scope: str = "") -> list[dict]:
    """Loose match on kind/filename ('trade licence' finds kind trade-licence and 'TradeLicense2026.pdf').

    `scope` is a HARD FILTER, not a hint: when a card belongs to a deal, its client/project words are
    passed here and a document must carry one of them to qualify. Without it a request for 'the
    quotation' scored every quotation in the company and returned another client's file - the ChainX
    card nearly went out with Property Finder's pricing attached (31 Aug 2026)."""
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
    # numbers identify no client: "2026" in one deal's title matched every dated file of every client
    stoks = [t for t in re.split(r"[^a-z0-9]+", (scope or "").lower())
             if len(t) > 2 and t not in _GENERIC and not t.isdigit()]
    if stoks:   # a standing company document is never another client's file, so the scope never drops it
        # WHOLE WORDS, not substrings: "erf" from Sheraa's ERF film matched "perfumes" and let a Rasasi
        # Perfumes proposal onto the Sheraa card (11 Sep 2026). The document's recorded client counts too.
        def _words(r):
            return set(re.split(r"[^a-z0-9]+", " ".join(
                (r.get("filename") or "", r.get("kind") or "", r.get("client") or "")).lower()))
        best = [r for r in best if r["kind"] in CORE_KINDS or any(t in _words(r) for t in stoks)]
    return best or []


# words that identify no project on their own - never enough to match a scoped document
_GENERIC = {"the", "and", "for", "film", "video", "project", "production", "quotation", "proposal",
            "sensa", "productions", "ltd", "llc", "company", "with", "from", "abu", "dhabi", "dubai"}
