"""A company's own published help pages, read into Cortex so support replies are answered from them.

Owner, 10 Oct 2026: "I want to start handling support emails and have Cortex draft the responses based on all of the
documentation." Snap Rewards publishes its help centre as a WordPress `docs` post type (snap-rewards.com/docs/...), 16
pages. Cortex had read none of it, so a merchant asking why receipt images did not show (Ralph Delgado, card 1114) got a
generic "which browser?" reply with call times, while the answer (Campaign Data, open the submission, the Campaign Result
screen shows the receipt) was on the site.

- WHERE: the company profile key `help_docs_api` (a WordPress REST collection URL). No key, no docs: nothing here is
  hardcoded to one company.
- STORE: setting `helpdocs:<company_id>` = {synced_at, source, pages: [{slug, title, url, modified, text}]}.
- REFRESH: `maybe_sync()` from the engine loop, at most once an hour it looks, and re-reads a company's pages when the
  copy is older than SYNC_DAYS, so edited or new help pages reach drafts within a week.
- USE: `worker.draft` puts `block(company_id)` in the cached system prompt of that company's email drafts, and
  `profile.trusted_links` admits the page URLs so a reply may link the exact help page.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import db

SYNC_DAYS = 7
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
_last_look = 0.0


def _key(company_id: int) -> str:
    return f"helpdocs:{int(company_id)}"


def _text(raw: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    t = re.sub(r"<\s*(br|/p|/li|/h[1-6]|/tr)\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def source(company_id: int) -> str:
    row = db.one("select data->>'help_docs_api' u from company_profiles where company_id=%s", (int(company_id),))
    return ((row or {}).get("u") or "").strip()


def sync(company_id: int) -> dict:
    """Read every help page from the company's `help_docs_api` and store the text. Returns {pages, chars} or {error}."""
    url = source(company_id)
    if not url:
        return {"error": "no help_docs_api on the profile"}
    pages, page = [], 1
    while page <= 10:
        r = httpx.get(url, params={"per_page": 100, "page": page, "_fields": "slug,title,link,modified,content"},
                      headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=30, follow_redirects=True)
        if r.status_code == 400 and page > 1:
            break
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            break
        for x in rows:
            body = _text(((x.get("content") or {}).get("rendered")) or "")
            if body:
                pages.append({"slug": x.get("slug"), "title": _text(((x.get("title") or {}).get("rendered")) or ""),
                              "url": x.get("link") or "", "modified": x.get("modified") or "", "text": body})
        if len(rows) < 100:
            break
        page += 1
    if not pages:
        return {"error": "the help pages came back empty"}
    db.setting_set(_key(company_id), {"synced_at": datetime.now(timezone.utc).isoformat(), "source": url,
                                      "pages": pages})
    return {"pages": len(pages), "chars": sum(len(p["text"]) for p in pages)}


def maybe_sync() -> None:
    """Engine-loop hook: re-read any company's help pages older than SYNC_DAYS. Looks at most once an hour."""
    global _last_look
    if time.time() - _last_look < 3600:
        return
    _last_look = time.time()
    for row in db.query("select company_id from company_profiles where coalesce(data->>'help_docs_api','') <> ''"):
        cid = int(row["company_id"])
        cur = db.setting_get(_key(cid)) or {}
        try:
            stale = (not cur.get("synced_at") or datetime.now(timezone.utc)
                     - datetime.fromisoformat(cur["synced_at"]) > timedelta(days=SYNC_DAYS))
        except ValueError:
            stale = True
        if stale:
            try:
                print(f"[helpdocs] company {cid}: {sync(cid)}", flush=True)
            except Exception as e:  # noqa: BLE001 - a site outage keeps the last good copy
                print(f"[helpdocs] company {cid}: {type(e).__name__}: {e}", flush=True)


def pages(company_id: int) -> list[dict]:
    return list((db.setting_get(_key(company_id)) or {}).get("pages") or [])


def urls(company_id: int) -> list[str]:
    return [p["url"] for p in pages(company_id) if p.get("url")]


def block(company_id: int, limit: int = 60000) -> str:
    """The help pages as one prompt block, or '' when the company has none."""
    ps = pages(company_id)
    if not ps:
        return ""
    parts, used = [], 0
    for p in ps:
        chunk = f"### {p['title']} ({p['url']})\n{p['text']}"
        if used + len(chunk) > limit:
            break
        parts.append(chunk)
        used += len(chunk)
    return ("PRODUCT DOCUMENTATION: the company's own published help pages, the ONLY source for how the product "
            "works, its screens, settings and steps. Use their exact screen and button names, and link the relevant "
            "page by its URL here when it helps. Never describe a feature, setting or behaviour these pages do not "
            "describe.\n\n" + "\n\n".join(parts))
