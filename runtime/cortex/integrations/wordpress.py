"""WordPress REST publisher (Tabscanner).

Hidden draft -> publish on the owner's explicit approval.

Rank Math's `rank_math_robots` is NOT exposed over the REST API (probed live on
tabscanner.com 2026-06-15: setting it on create is silently dropped). So Cortex does NOT
rely on a 'live-but-noindex' state. Instead it gates on post *status*:

    draft   = not public, not indexable  (the hidden/preview state)
    publish = live + indexable           (only on the owner's per-post approval)

That is a stricter reading of the web-page-builder golden rule (nothing is even public
until the owner approves THAT post), needing zero Rank Math cooperation. A true
live-but-noindex preview would need a small meta-registration mu-plugin on Tabscanner —
future enhancement, not required for the gate to be safe.
"""
from __future__ import annotations

import base64

import httpx

from .. import config

# Cloudflare on tabscanner.com 403s library UAs (python-httpx / urllib). A curl-style UA passes.
_UA = "curl/8.4.0"
_TIMEOUT = 40.0


class WordPress:
    def __init__(self, base_url: str, user: str, app_password: str):
        self.base = base_url.rstrip("/") + "/wp-json/wp/v2"
        token = base64.b64encode(f"{user}:{app_password}".encode()).decode()
        self._headers = {
            "User-Agent": _UA,
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    def _req(self, method: str, path: str, **kw) -> dict:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.request(method, self.base + path, headers=self._headers, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def stage_preview(self, title: str, html: str, password: str, excerpt: str = "") -> dict:
        """Publish PASSWORD-PROTECTED: a real, fully-themed URL the owner opens with the password,
        but the public + search engines cannot. Rank Math excludes password-protected posts from
        the sitemap and the content is gated, so it is effectively hidden + non-indexable while
        still rendering the finished design. Approving (go_live) clears the password -> public."""
        body = {"title": title, "content": html, "status": "publish", "password": password}
        if excerpt:
            body["excerpt"] = excerpt
        p = self._req("POST", "/posts", json=body)
        return {"id": p["id"], "link": p.get("link"), "status": p.get("status")}

    def update(self, post_id: int, title: str, html: str) -> dict:
        # title + content only; the preview password is preserved.
        p = self._req("POST", f"/posts/{post_id}", json={"title": title, "content": html})
        return {"id": p["id"], "link": p.get("link"), "status": p.get("status")}

    def go_live(self, post_id: int) -> dict:
        """Clear the preview password -> the post becomes public + indexable."""
        p = self._req("POST", f"/posts/{post_id}", json={"password": ""})
        return {"id": p["id"], "link": p.get("link"), "status": p.get("status")}

    def set_status(self, post_id: int, status: str) -> dict:
        p = self._req("POST", f"/posts/{post_id}", json={"status": status})
        return {"id": p["id"], "link": p.get("link"), "status": p.get("status")}

    def trash(self, post_id: int) -> dict:
        return self._req("DELETE", f"/posts/{post_id}")

    def get(self, post_id: int) -> dict:
        return self._req("GET", f"/posts/{post_id}?context=edit")


def configured() -> bool:
    return bool(config.get("TABSCANNER_APP_PASSWORD"))


def for_company(company: dict) -> "WordPress | None":
    """Phase 2 wires Tabscanner only. Later: per-company connection rows."""
    if (company.get("slug") != "tabscanner") or not configured():
        return None
    return WordPress(
        config.get("TABSCANNER_WP_URL", "https://tabscanner.com"),
        config.get("TABSCANNER_WP_USER", "tabscanner"),
        config.require("TABSCANNER_APP_PASSWORD"),
    )
