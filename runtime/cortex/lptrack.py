"""PPC LANDING-PAGE LEAD SOURCE (owner, 28 Sep 2026: "is it possible to also know the keywords they came through?").

WhatsApp carries nothing about where a person came from, so the landing page puts it in the message. Google Ads
appends the keyword, match type, campaign and ad group to the landing page URL (the campaigns' final URL suffix,
ValueTrack) plus its own gclid. When the visitor taps the WhatsApp button, the page adds a short reference to the
pre-filled text ("... (ref D7F3K)") and beacons {ref, keyword, ...} here. When the message arrives, the reference is
decoded back to its click. The per-click search phrase is NOT available from Google (only the keyword that matched;
search terms are reported in aggregate), so this records the keyword, never a guessed search term."""
from __future__ import annotations

import re
import time

from . import db

REF_RX = re.compile(r"\(\s*ref\s+([A-HJ-NP-Z2-9]{5})\s*\)", re.I)
_OK_REF = re.compile(r"^[A-HJ-NP-Z2-9]{5}$")
_KEY = "lp_refs"
_CAP = 3000
_RATE: dict = {}
_MATCH = {"e": "exact", "p": "phrase", "b": "broad"}


def _clip(v, n: int = 120) -> str:
    return re.sub(r"[\x00-\x1f]", "", str(v or ""))[:n].strip()


def record(d: dict, ip: str = "") -> bool:
    """Store one landing-page click. Public endpoint input: validated, clipped, rate-limited per IP."""
    now = time.time()
    hits = [t for t in _RATE.get(ip, []) if now - t < 600]
    if len(hits) >= 60:
        return False
    _RATE[ip] = hits + [now]
    ref = _clip(d.get("ref"), 5).upper()
    if not _OK_REF.match(ref):
        return False
    row = {"ref": ref, "kw": _clip(d.get("kw")), "mt": _clip(d.get("mt"), 2).lower(), "cid": _clip(d.get("cid"), 20),
           "agid": _clip(d.get("agid"), 20), "gclid": _clip(d.get("gclid"), 200), "page": _clip(d.get("page"), 80),
           "ts": int(now)}
    refs = dict(db.setting_get(_KEY) or {})
    refs[ref] = row
    if len(refs) > _CAP:
        refs = dict(sorted(refs.items(), key=lambda kv: kv[1].get("ts") or 0)[-_CAP:])
    db.setting_set(_KEY, refs)
    return True


def lookup(ref: str) -> dict | None:
    return (db.setting_get(_KEY) or {}).get((ref or "").strip().upper())


def _campaign_name(cid: str) -> str:
    if not cid.isdigit():
        return ""
    try:
        from . import ppc_report
        rows = ppc_report._ads_search(ppc_report.ACCOUNTS["sensa"]["cid"],
                                      f"SELECT campaign.name FROM campaign WHERE campaign.id = {int(cid)}")
        return ((rows[0].get("campaign") or {}).get("name") or "") if rows else ""
    except Exception:  # noqa: BLE001 - the name is a nicety
        return ""


def describe(row: dict | None) -> str:
    """One line a person can read: keyword, match type, campaign, page, when."""
    if not row:
        return ""
    bits = []
    if row.get("kw"):
        bits.append(f"keyword '{row['kw']}'" + (f" ({_MATCH.get(row.get('mt'), row.get('mt'))} match)" if row.get("mt") else ""))
    camp = _campaign_name(row.get("cid") or "")
    if camp or row.get("cid"):
        bits.append(f"campaign {camp or row.get('cid')}")
    if row.get("page"):
        bits.append(f"page {row['page']}")
    if row.get("gclid"):
        bits.append("paid click (Google Ads)")
    if row.get("ts"):
        bits.append("tapped " + time.strftime("%d %b %H:%M UTC", time.gmtime(row["ts"])))
    return "; ".join(bits) or "landing page, no ad details (not a paid click)"


def from_message(msg: str) -> dict | None:
    """The click behind a WhatsApp message that carries a landing-page reference, with a readable line."""
    m = REF_RX.search(msg or "")
    if not m:
        return None
    row = lookup(m.group(1))
    return {"ref": m.group(1).upper(), "click": row, "line": describe(row) if row else "reference not on record"}
