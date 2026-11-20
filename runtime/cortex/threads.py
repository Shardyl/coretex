"""THE THREAD PICKER (owner, 20 Nov 2026).

"Cortex basically starting new threads for everything." D-Marin's mail ran to nine conversations in 75 days, and
HBMSU got two "Learn from anywhere batch 2" threads after the team wrote three times "do not create new one". The
owner's answer: every email card says WHICH conversation it will send on, labelled by its subject, with a drop-down
of the real conversations with that client to pick the right one, and "New thread" as a deliberate last option.

- `options(task)` reads every conversation with the recipient (their whole company when it is a corporate domain)
  from EVERY one of our mailboxes, and merges them by subject: one Gmail conversation lives under a different
  thread id in each mailbox, and a reply that lost its headers splits it again inside one mailbox, so D-Marin's
  main thread was eight entries in Gino's mailbox alone. One option per subject, newest first, with who wrote last,
  when, how many messages, and which opportunity it already belongs to.
- `choose(task, req, ...)` points the card at one of them IN PLACE (the caller persists `req`): the thread's own
  subject, and reply headers taken from the NEWEST message in that conversation, whichever mailbox holds it. The
  thread id is the sending mailbox's own one when it has a copy; otherwise only the headers travel, which is
  enough for the client's mail program to keep it in one conversation.
- `current(task)` is the cheap label the card always shows (no Gmail call).
- A pick is remembered on the opportunity (`deal_thread:<deal_id>`), and adoption prefers it next time.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

import httpx

from . import db, store

# Every prefix a mail system puts in front of a subject: replies and forwards in several languages, and the
# "[EXTERNAL]" tag HBMSU's gateway adds ("Re: [EXTERNAL]Re: Request for Creative Approach ...").
_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|fw|aw|wg|sv|tr|rv|antw)\s*:\s*|\[\s*ext(?:ernal)?\s*\]\s*:?\s*|"
                     r"ext(?:ernal)?\s*:\s*|\*?external\*?\s*:\s*)+", re.I)
_AUTO_FROM = re.compile(r"noreply|no-reply|donotreply|do-not-reply|notifications?@|mailer-daemon|postmaster|"
                        r"calendar-notification|@fireflies\.ai|@otter\.ai|@zoom\.us|@calendly\.com", re.I)
LOOKBACK_DAYS = 180
_CACHE: dict = {}
_TTL = 300


def clean_subject(s: str | None) -> str:
    return re.sub(r"\s+", " ", _PREFIX.sub("", s or "")).strip()


def norm_subject(s: str | None) -> str:
    """The conversation's identity across mailboxes: prefixes off, case, spacing and dash style ignored."""
    t = clean_subject(s).lower()
    t = re.sub(r"\s*[‐-―]\s*|\s+-\s*|\s*-\s+", " - ", t)     # spaced or typographic dashes are one dash
    return re.sub(r"\s+", " ", t).strip(" .")


def _addr(h: str) -> tuple:
    n, a = parseaddr(h or "")
    return (n or "").strip().strip('"'), (a or "").strip().lower()


def _free(dom: str) -> bool:
    try:
        from .newsletter import _FREEMAIL
        return dom in _FREEMAIL
    except Exception:  # noqa: BLE001
        return dom in {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "live.com"}


def _query_for(email: str) -> str:
    dom = email.split("@")[-1].lower()
    who = email if _free(dom) else dom
    return f"(from:{who} OR to:{who} OR cc:{who}) newer_than:{LOOKBACK_DAYS}d"


def _mailbox_threads(rt_key: str, q: str, company: str | None, limit: int = 25) -> list:
    """[{thread_id, messages: [{name, email, to, cc, subject, msg_id, references, ms}]}] for one mailbox,
    headers only (format=metadata), so a picker load is a few dozen light calls, not full message bodies."""
    from . import gmail
    tok = gmail._token_for(rt_key, "gmail", company)
    res = gmail._get(tok, "threads", {"q": q, "maxResults": limit})
    ids = [t["id"] for t in res.get("threads", [])]

    def one(tid):
        try:
            r = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{tid}",
                          params={"format": "metadata",
                                  "metadataHeaders": ["Subject", "From", "To", "Cc", "Message-ID", "References"]},
                          headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            r.raise_for_status()
            out = []
            for m in r.json().get("messages", []):
                h = {x["name"].lower(): x["value"] for x in (m.get("payload") or {}).get("headers", [])}
                name, em = _addr(h.get("from", ""))
                out.append({"name": name, "email": em, "to": h.get("to", ""), "cc": h.get("cc", ""),
                            "from": h.get("from", ""), "subject": h.get("subject", ""),
                            "msg_id": h.get("message-id", ""), "references": h.get("references", ""),
                            "ms": int(m.get("internalDate") or 0)})
            return {"thread_id": tid, "messages": out}
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        return [t for t in ex.map(one, ids) if t]


def _thread_deals(company_id: int) -> dict:
    """thread id -> the opportunity whose sent cards (or whose owner's pick) used it."""
    out = {}
    for r in db.query("select request->'thread'->>'id' t, deal_id from tasks where company_id=%s and deal_id is not "
                      "null and status='done' and coalesce(request->'thread'->>'id','')<>'' and created_at > now() - "
                      "interval '200 days'", (company_id,)):
        out.setdefault(r["t"], r["deal_id"])
    return out


def options(task: dict, fresh: bool = False) -> list:
    """Every conversation with this card's recipient, one entry per subject, newest first (max 15)."""
    from . import engine
    req = task.get("request") or {}
    email = ((req.get("inquiry") or {}).get("email") or "").strip().lower()
    if "@" not in email:
        return []
    co = store.get_company(task["company_id"]) or {}
    q = _query_for(email)
    ck = (task["company_id"], q)
    if not fresh and ck in _CACHE and time.time() - _CACHE[ck][0] < _TTL:
        return _CACHE[ck][1]
    senders = engine._company_senders(task["company_id"])
    ours = {v["email"] for v in senders.values()}
    client = engine._inbox_client_company(co.get("slug") or "")
    pool, seen = [], set()
    for s in senders.values():
        if s["rt_key"] not in seen:
            seen.add(s["rt_key"])
            pool.append(s)
    with ThreadPoolExecutor(max_workers=4) as ex:
        per = list(ex.map(lambda s: (s, _safe(lambda: _mailbox_threads(s["rt_key"], q, client))), pool))
    tdeals = _thread_deals(task["company_id"])
    groups: dict = {}
    for s, ths in per:
        for th in ths or []:
            msgs = [m for m in th["messages"] if not _AUTO_FROM.search(m["email"])]
            if not msgs:
                continue
            first = min(msgs, key=lambda m: m["ms"])
            key = norm_subject(first["subject"])
            if not key or engine._notice_subject(first["subject"]):
                continue
            g = groups.setdefault(key, {"key": key, "subject": clean_subject(first["subject"]), "msgs": {},
                                        "mailboxes": {}, "deals": set()})
            for m in msgs:
                g["msgs"][m["msg_id"] or f"{th['thread_id']}:{m['ms']}"] = m
            newest_here = max(msgs, key=lambda m: m["ms"])
            prev = g["mailboxes"].get(s["rt_key"])
            if not prev or newest_here["ms"] > prev["ms"]:
                g["mailboxes"][s["rt_key"]] = {"thread_id": th["thread_id"], "ms": newest_here["ms"]}
            if tdeals.get(th["thread_id"]):
                g["deals"].add(tdeals[th["thread_id"]])
    out = []
    for g in groups.values():
        ms = sorted(g["msgs"].values(), key=lambda m: m["ms"])
        last = ms[-1]
        # reply to the newest message that carries a Message-Id: that is what chains our email on to it
        head = next((m for m in reversed(ms) if m["msg_id"]), last)
        out.append({
            "key": g["key"], "subject": g["subject"], "count": len(ms),
            "last_from": last["name"] or last["email"], "last_email": last["email"],
            "last_ours": last["email"] in ours,
            "last_at": datetime.fromtimestamp(last["ms"] / 1000, timezone.utc).isoformat() if last["ms"] else "",
            "msg_id": head["msg_id"], "references": head["references"],
            "head": {"from": head["from"], "to": head["to"], "cc": head["cc"], "email": head["email"]},
            "mailboxes": {k: v["thread_id"] for k, v in g["mailboxes"].items()},
            "deals": sorted(g["deals"]), "_ms": last["ms"]})
    out.sort(key=lambda o: -o["_ms"])
    out = out[:15]
    _CACHE[ck] = (time.time(), out)
    return out


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return []


def _thread_in_mailbox(rt_key: str, msg_id: str, company: str | None) -> str:
    """The sending mailbox's OWN thread id for a message, found by its Message-Id ('' when it holds no copy)."""
    if not (rt_key and msg_id):
        return ""
    try:
        from . import gmail
        tok = gmail._token_for(rt_key, "gmail", company)
        res = gmail._get(tok, "messages", {"q": f"rfc822msgid:{msg_id.strip('<>')}", "maxResults": 1})
        m = (res.get("messages") or [None])[0]
        return (m or {}).get("threadId", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def match_words(opts: list, words: str) -> dict | None:
    """The option his words name ('the batch 2 thread', 'D-Marin Marasi Bay Marina Brand Video Shoot'): the best
    word overlap with a subject, and only when it is clearly the best."""
    stop = {"the", "thread", "email", "chain", "trail", "conversation", "our", "this", "that", "on", "in", "put", "it",
            "to", "of", "and", "a", "an", "latest", "existing", "same", "one", "re", "fw", "fwd"}
    if not opts:
        return None
    ws = [w for w in re.split(r"[^a-z0-9]+", norm_subject(words)) if w and w not in stop]
    if not ws:
        return opts[0]        # "put it on our latest thread": no name given, the newest conversation
    scored = []
    for o in opts:
        sw = set(re.split(r"[^a-z0-9]+", o["key"]))
        hit = sum(1 for w in ws if w in sw)
        scored.append((hit / len(ws), hit, o))
    scored.sort(key=lambda x: (-x[0], -x[2]["_ms"]))
    best = scored[0]          # a tie goes to the newest, which is what "our latest thread" means anyway
    if best[1] == 0 or best[0] < 0.5:
        return None
    return best[2]


def choose(task: dict, req: dict, key: str | None = None, words: str | None = None, actor: str = "owner") -> dict:
    """Point the card at an existing conversation, IN PLACE on `req` (the caller persists it).
    Returns {ok, subject} or {ok: False, error}."""
    from . import engine
    opts = options(task)
    o = None
    if key:
        o = next((x for x in opts if x["key"] == key), None) or next(
            (x for x in options(task, fresh=True) if x["key"] == key), None)
    elif words:
        o = match_words(opts, words) or match_words(options(task, fresh=True), words)
    if not o:
        return {"ok": False, "error": "No conversation with them matches" + (f" '{words}'" if words else "")}
    co = store.get_company(task["company_id"]) or {}
    senders = engine._company_senders(task["company_id"])
    rt = req.get("mailbox_rt") or next((v["rt_key"] for v in senders.values()
                                        if v["email"] == (req.get("from_email") or "").lower()), "")
    tid = o["mailboxes"].get(rt) or _thread_in_mailbox(rt, o["msg_id"], engine._inbox_client_company(co.get("slug") or ""))
    req["thread"] = {"id": tid or "", "msg_id": o["msg_id"] or "", "references": o["references"] or ""}
    req["thread_label"] = {"subject": o["subject"], "last_from": o["last_from"], "last_at": o["last_at"],
                           "count": o["count"], "key": o["key"], "picked_by": actor}
    req["inquiry"] = {**(req.get("inquiry") or {}), "subject": o["subject"]}
    req.pop("new_thread", None)
    try:
        req["thread_cc"] = engine._thread_participants(o["head"], (req.get("inquiry") or {}).get("email") or "")
    except Exception:  # noqa: BLE001
        pass
    did = req.get("deal_id") or task.get("deal_id")
    if did:
        db.setting_set(f"deal_thread:{int(did)}", {"key": o["key"], "subject": o["subject"],
                                                   "at": datetime.now(timezone.utc).isoformat(), "by": actor})
    return {"ok": True, "subject": o["subject"]}


def start_new(req: dict, subject: str) -> dict:
    subject = re.sub(r"\s+", " ", subject or "").strip(" .\"'")
    if not subject:
        return {"ok": False, "error": "A new thread needs a subject"}
    req["new_thread"] = True
    req.pop("thread", None)
    req.pop("thread_label", None)
    req["inquiry"] = {**(req.get("inquiry") or {}), "subject": subject[0].upper() + subject[1:]}
    return {"ok": True, "subject": req["inquiry"]["subject"]}


def current(task: dict) -> dict:
    """What the card will send on, from the stored request alone (no Gmail call)."""
    req = task.get("request") or {}
    th = req.get("thread") or {}
    subj = clean_subject((req.get("inquiry") or {}).get("subject") or "")
    if th.get("id") or th.get("msg_id"):
        lab = req.get("thread_label") or {}
        return {"mode": "continue", "subject": lab.get("subject") or subj, "last_from": lab.get("last_from") or "",
                "last_at": lab.get("last_at") or "", "count": lab.get("count") or 0}
    return {"mode": "new", "subject": subj, "deliberate": bool(req.get("new_thread"))}


def remembered(deal_id) -> str:
    """The conversation the owner last picked for this opportunity (normalised subject), or ''."""
    if not deal_id:
        return ""
    try:
        v = db.setting_get(f"deal_thread:{int(deal_id)}") or {}
        return (v.get("key") or "") if isinstance(v, dict) else ""
    except Exception:  # noqa: BLE001
        return ""
