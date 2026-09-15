"""Suspected-phishing check on inbound mail (15 Sep 2026).

A real contact's hacked mailbox (Heba at Jump) sent Sensa and Sky Vision a bcc'd "RFP": an empty email, a
one-page PDF generated minutes before it was sent, and a VIEW RFP DOCUMENT button to a fake Google sign-in
on an unrelated domain. Cortex read the PDF as a genuine brief and drafted two warm replies offering calls.

This is a safety invariant, like the invented-link guard, so it is code: a check of WHERE links point, never
a judgement of what the email says. Two lures, both "a link to somewhere other than the sender":
- a SHORT attached PDF (3 pages or fewer) that asks you to view/access/open a document and links off the
  sender's domain: a document whose only job is to send you elsewhere;
- a call-to-action link in the email body ("View document", "Access the secure link") to an unrelated domain.
  Well-known file-share and video hosts are fine in the BODY (clients share Drive and WeTransfer links every
  day) but not behind a PDF lure, which is exactly how those hosts get abused.
A hit means no reply is drafted and the owner is warned; nothing is deleted.
"""
from __future__ import annotations

import io
import re
from urllib.parse import urlparse

from . import gmail

_CTA = re.compile(
    r"\b(view|access|open|download|review|retrieve|sign in|log ?in)\b[^\n]{0,40}?"
    r"\b(documents?|files?|rfp|rfq|proposal|invoice|pdf|attachment|contract|portal)\b"
    r"|\bsecure link\b|\bshared (a |the )?(document|file)\b", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_SECOND_LEVEL = {"com", "co", "org", "net", "gov", "ac", "edu", "ltd", "plc", "sch", "mil"}
# hosts a genuine client uses to share work in an email body (still suspect behind a PDF lure)
_SHARE_HOSTS = ("google.com", "dropbox.com", "wetransfer.com", "we.tl", "box.com", "sharepoint.com",
                "onedrive.live.com", "1drv.ms", "icloud.com", "frame.io", "vimeo.com", "youtube.com",
                "youtu.be", "canva.com", "figma.com", "notion.so", "linkedin.com", "instagram.com")
# hosts that are only ever a reference (a showreel, a profile), never a sign-in lure, even in a PDF
_MEDIA_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com", "linkedin.com", "facebook.com",
                "tiktok.com", "x.com", "twitter.com")


def base_domain(host: str) -> str:
    """The registered domain: begone602.nobleoak.com.de -> nobleoak.com.de, mail.jump.sa -> jump.sa."""
    parts = [p for p in (host or "").lower().strip(".").split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _on(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _outside(url: str, sender_dom: str, ours: set) -> str:
    """The link's host when it points off the sender's own domain (and off ours), else ''."""
    h = _host(url)
    if not h:
        return ""
    b = base_domain(h)
    return "" if b == base_domain(sender_dom) or b in ours else h


def _pdf(data: bytes) -> tuple[int, str, list[str]]:
    """(pages, text, every link: the clickable annotations plus any URL written in the text)."""
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    text, uris = "", []
    for p in r.pages[:4]:
        text += (p.extract_text() or "") + "\n"
        for an in (p.get("/Annots") or []):
            try:
                act = an.get_object().get("/A") or {}
                if act.get("/URI"):
                    uris.append(str(act.get("/URI")))
            except Exception:  # noqa: BLE001
                continue
    return len(r.pages), text, uris + _URL.findall(text)


def check(e: dict, rt_key: str | None, client: str | None, ours: set | None = None) -> dict | None:
    """{'reasons': [...], 'hosts': [...]} when the email carries a phishing lure, else None.
    `e` is a gmail._parse_generic message; `ours` is our own registered domains (never suspect)."""
    sender = (e.get("email") or "").lower()
    sdom = sender.split("@")[-1] if "@" in sender else ""
    if not sdom:
        return None
    ours = ours or set()
    reasons, hosts = [], []
    for a in (e.get("attachments") or [])[:4]:
        fn, mime = a.get("filename") or "", (a.get("mime") or "").lower()
        if not (mime == "application/pdf" or fn.lower().endswith(".pdf")):
            continue
        if not 0 < int(a.get("size") or 0) <= 8_000_000:
            continue
        try:
            data = gmail.get_attachment(e.get("gmail_id"), a["att_id"], rt_key or "gmail_refresh_token",
                                        company=client)
            pages, text, uris = _pdf(data)
        except Exception:  # noqa: BLE001 — an unreadable PDF is the drafter's problem, not a verdict
            continue
        if pages > 3 or not _CTA.search(text):
            continue
        out = [h for h in (_outside(u, sdom, ours) for u in uris) if h and not _on(h, _MEDIA_HOSTS)]
        if out:
            hosts += out
            reasons.append(f'the attached "{fn}" ({pages} page{"s" if pages != 1 else ""}) asks you to open a '
                           f"document through a link to {out[0]}, which is not {sdom}")
    for href, label in (e.get("links") or []):
        h = _outside(href, sdom, ours)
        if h and not _on(h, _SHARE_HOSTS) and _CTA.search(label or ""):
            hosts.append(h)
            reasons.append(f'the "{(label or "")[:60]}" link in the email goes to {h}, which is not {sdom}')
    if not reasons:
        return None
    return {"reasons": reasons[:3], "hosts": sorted(set(hosts))[:5]}
