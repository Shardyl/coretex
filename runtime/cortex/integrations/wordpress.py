"""WordPress REST publisher.

Cortex never publishes a blog post straight to the live site. It creates a real WordPress
**draft** (status=draft) - a fully-themed, unpublished page - and hands the owner a PREVIEW
LINK. Opened while logged into wp-admin, that link renders the finished page exactly as it
will look, but it is not public and not indexed. The owner then approves to publish
(draft -> publish), corrects (updates the draft), or discards (trash). No Rank Math needed.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .. import config

_UA = "curl/8.4.0"   # tabscanner.com Cloudflare 403s library UAs; a curl-style UA passes
_TIMEOUT = 40.0


class WordPress:
    def __init__(self, base_url: str, user: str, app_password: str):
        self.site = base_url.rstrip("/")
        self.base = self.site + "/wp-json/wp/v2"
        token = base64.b64encode(f"{user}:{app_password}".encode()).decode()
        self._headers = {"User-Agent": _UA, "Authorization": f"Basic {token}",
                         "Content-Type": "application/json"}

    def _req(self, method: str, path: str, **kw) -> dict:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.request(method, self.base + path, headers=self._headers, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _links(self, p: dict) -> tuple[str, str, str]:
        link = p.get("link") or f"{self.site}/?p={p.get('id')}"
        raw_preview = link + ("&" if "?" in link else "?") + "preview=true"
        # route through wp-login so an unauthenticated tap logs in then lands on the rendered draft.
        preview = f"{self.site}/wp-login.php?redirect_to={quote(raw_preview, safe='')}"
        edit = f"{self.site}/wp-admin/post.php?post={p.get('id')}&action=edit"
        return link, preview, edit

    def upload_media(self, src_url: str, filename: str = "featured.jpg", alt: str = "") -> int | None:
        """Fetch an image from its (public R2) URL and upload it into the WP media library so it can be the
        post's FEATURED IMAGE. Returns the attachment id, or None on any failure (never blocks the publish)."""
        if not src_url:
            return None
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                img = c.get(src_url, headers={"User-Agent": _UA})
                img.raise_for_status()
                ctype = img.headers.get("content-type", "image/jpeg")
                h = {"User-Agent": _UA, "Authorization": self._headers["Authorization"],
                     "Content-Type": ctype, "Content-Disposition": f'attachment; filename="{filename}"'}
                m = c.post(self.base + "/media", headers=h, content=img.content)
                m.raise_for_status()
                mid = m.json().get("id")
            if mid and alt:
                self._req("POST", f"/media/{mid}", json={"alt_text": alt})
            return mid
        except Exception:   # noqa: BLE001 — a missing featured image must never fail the draft
            return None

    def _set_featured(self, post_id: int, featured_url: str | None, alt: str = "") -> None:
        if not featured_url:
            return
        mid = self.upload_media(featured_url, "featured.jpg", alt)
        if mid:
            self._req("POST", f"/posts/{post_id}", json={"featured_media": mid})

    def ensure_category(self, name: str) -> int | None:
        """Find a WordPress category by name (case-insensitive, HTML-entity tolerant: WP stores '&' as '&amp;')
        or create it; returns its id. Never raises."""
        name = (name or "").strip()
        if not name:
            return None
        key = name.replace("&amp;", "&").lower()
        try:
            for cat in (self._req("GET", "/categories?per_page=100&_fields=id,name") or []):
                if (cat.get("name") or "").replace("&amp;", "&").strip().lower() == key:
                    return cat["id"]
            c = self._req("POST", "/categories", json={"name": name})
            return c.get("id")
        except Exception:   # noqa: BLE001 — categorisation must never block a publish
            return None

    def _set_category(self, post_id: int, category: str | None) -> None:
        cid = self.ensure_category(category) if category else None
        if cid:
            self._req("POST", f"/posts/{post_id}", json={"categories": [cid]})

    def stage_draft(self, title: str, html: str, excerpt: str = "", featured_url: str | None = None,
                    featured_alt: str = "", category: str | None = None) -> dict:
        """Create an unpublished WordPress draft + set its featured image (theme banner) + its category."""
        body = {"title": title, "content": html, "status": "draft"}
        if excerpt:
            body["excerpt"] = excerpt
        p = self._req("POST", "/posts", json=body)
        self._set_featured(p["id"], featured_url, featured_alt)
        self._set_category(p["id"], category)
        link, preview, edit = self._links(p)
        return {"id": p["id"], "link": link, "preview": preview, "edit": edit, "status": p.get("status")}

    def update(self, post_id: int, title: str, html: str, featured_url: str | None = None,
               featured_alt: str = "", category: str | None = None) -> dict:
        # title + content; the post stays a draft. Re-set featured image / category only if passed.
        p = self._req("POST", f"/posts/{post_id}", json={"title": title, "content": html})
        self._set_featured(post_id, featured_url, featured_alt)
        self._set_category(post_id, category)
        link, preview, edit = self._links(p)
        return {"id": p["id"], "link": link, "preview": preview, "edit": edit, "status": p.get("status")}

    def go_live(self, post_id: int) -> dict:
        """Publish the draft -> public + indexable (only on the owner's approval). Stamp the post date to the
        publish MOMENT (go_live runs on the scheduled publishing day), so the live post carries the day it
        actually goes out, not the day the draft was created."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        p = self._req("POST", f"/posts/{post_id}", json={"status": "publish", "date_gmt": now})
        return {"id": p["id"], "link": p.get("link"), "status": p.get("status")}

    def trash(self, post_id: int) -> dict:
        return self._req("DELETE", f"/posts/{post_id}")

    def get(self, post_id: int) -> dict:
        return self._req("GET", f"/posts/{post_id}?context=edit")


def configured() -> bool:
    return bool(config.get("TABSCANNER_APP_PASSWORD"))


def for_company(company: dict) -> "WordPress | None":
    """Per-company WordPress connection from env creds. Tabscanner keeps the legacy unsuffixed keys;
    every other company uses `<SLUG>_WP_URL` / `<SLUG>_WP_USER` / `<SLUG>_WP_APP_PASSWORD` (slug upper-cased,
    hyphens to underscores, e.g. FILMSPOKE_WP_*, SNAP_REWARDS_WP_*). Returns None if no app password is set,
    so a company with no WP connected simply doesn't get the publish/test-group-digest path."""
    slug = company.get("slug") or ""
    if slug == "tabscanner":
        pw = config.get("TABSCANNER_APP_PASSWORD")
        if not pw:
            return None
        return WordPress(config.get("TABSCANNER_WP_URL", "https://tabscanner.com"),
                         config.get("TABSCANNER_WP_USER", "tabscanner"), pw)
    pre = slug.upper().replace("-", "_")
    pw = config.get(f"{pre}_WP_APP_PASSWORD")
    url = config.get(f"{pre}_WP_URL")
    if not pw or not url:
        return None
    return WordPress(url, config.get(f"{pre}_WP_USER", slug), pw)
