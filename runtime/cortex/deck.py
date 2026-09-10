"""Proposal decks — the house-format PDF proposal that accompanies a quotation.

Same separation as quotation.py: this module is PLUMBING. It lays out a structured spec and STAMPS
every fact that must be real (sample films from the media library, prices from the rate card, dates
from the clock). WHAT a proposal says is written by the model under the company's live skill rules,
never invented here.

The house rules this encodes, all owner-established:
  * Cover carries a generated hero image relevant to the CLIENT's project, in tones sympathetic to
    their brand, darkened toward the lower third so the title reads, with NO legible text in it.
  * Sample films come from the media library only (category intersection, highest-rated first), shown
    as the ORIGINAL YouTube thumbnail at native 16:9 — never cropped to a fixed height.
  * Timelines are elapsed-time schedules built from parallel tracks, never a serial sum of phases.
  * Prices come from the rate card; anything not on it is OWNER TO CONFIRM.
"""
from __future__ import annotations

import base64
import html as _html
import io
import os
import re
import subprocess
import tempfile

from . import db, imagegen, provider, ratecard, store, worker

_ACCENT_DEFAULT = "#00DAFF"


# --------------------------------------------------------------------------- assets

def _b64(path: str, mime: str = "image/jpeg") -> str:
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def _logo(company: dict) -> str:
    """The company's dark-background logo from its brand kit. Never invented; empty if absent."""
    try:
        row = db.one("select data->'brand'->>'logo_dark_b64' b from company_profiles where company_id=%s",
                     (company["id"],))
        if row and row.get("b"):
            return "data:image/png;base64," + row["b"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def thumbnail(video_id: str, out_dir: str = "/tmp") -> str | None:
    """The film's ORIGINAL YouTube thumbnail, trimmed to true 16:9 when the source is letterboxed.
    Highest resolution that actually exists; a placeholder-sized response is rejected."""
    import urllib.request
    path = os.path.join(out_dir, f"deck-th-{video_id}.jpg")
    for res in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            urllib.request.urlretrieve(f"https://i.ytimg.com/vi/{video_id}/{res}.jpg", path)
        except Exception:  # noqa: BLE001
            continue
        if os.path.getsize(path) > 12000:
            break
    else:
        return None
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.size
        if h * 16 != w * 9:                      # trim baked-in letterbox to the true frame
            t = int(w * 9 / 16)
            im = im.crop((0, (h - t) // 2, w, (h - t) // 2 + t))
        im.convert("RGB").resize((1280, 720)).save(path, quality=90)
    except Exception:  # noqa: BLE001
        pass
    return path


def pick_samples(company_id: int, categories: list[str], limit: int = 3) -> list[dict]:
    """Sample films from the MEDIA LIBRARY only: the intersection of the enquiry's categories first,
    highest-rated wins; widen to any of the categories if the intersection is thin. Unrated films rank
    below rated ones — the operator's rating is the quality filter."""
    cats = [c.strip().lower() for c in (categories or []) if c and c.strip()]
    out: list[dict] = []
    if cats:
        where = " and ".join(["categories @> %s::jsonb"] * len(cats))
        params = [f'["{c}"]' for c in cats]
        out = db.query(
            "select youtube_video_id, title, rating, duration, categories from media_assets "
            f"where company_id=%s and status='live' and {where} "
            "order by rating desc nulls last, suggested_rating desc nulls last limit %s",
            (company_id, *params, limit))
    if len(out) < limit and cats:                # widen: ANY of the categories
        have = {r["youtube_video_id"] for r in out}
        anyw = " or ".join(["categories @> %s::jsonb"] * len(cats))
        params = [f'["{c}"]' for c in cats]
        for r in db.query(
                "select youtube_video_id, title, rating, duration, categories from media_assets "
                f"where company_id=%s and status='live' and ({anyw}) "
                "order by rating desc nulls last, suggested_rating desc nulls last limit %s",
                (company_id, *params, limit * 3)):
            if r["youtube_video_id"] not in have:
                out.append(r)
            if len(out) >= limit:
                break
    return out[:limit]


def cover_image(subject: str, palette: str, company_slug: str, out_dir: str = "/tmp") -> str | None:
    """The cover hero: relevant to the CLIENT's project, sympathetic to their palette, dark toward the
    lower third for the title, and carrying NO legible text (a generated 'For Sdie' typo on a client
    cover is exactly what this forbids)."""
    prompt = (
        f"Cinematic hero photograph for a premium production proposal. Subject: {subject}. "
        f"Colour: {palette}, restrained and never over-saturated. Style: editorial photography, shot on "
        "a 50mm lens at f2.0, soft natural or volumetric light, gentle film grain, quietly premium. "
        "Composition: the main visual interest in the upper two thirds, the lower third falling into "
        "clean shadow for headline text. Absolutely no legible text, no signage, no logos, no numbers "
        "and no readable writing anywhere in the frame; any people are indistinct or turned away.")
    try:
        data = imagegen.hero(prompt, aspect="16:9", purpose="proposal-cover", company=company_slug)
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    path = os.path.join(out_dir, "deck-cover.jpg")
    with open(path, "wb") as f:
        f.write(data)
    return path


# --------------------------------------------------------------------------- layout

def _css(accent: str) -> str:
    return """
@page { size: 1280px 720px; margin: 0; }
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: Inter, sans-serif; background:#0A0A0A; color:#EDEDF2; }
.pg { width:1280px; height:720px; position:relative; page-break-after:always; background:#0A0A0A; overflow:hidden; }
.pad { padding:58px 72px; }
h1,h2,h3 { font-family: Poppins, sans-serif; }
h1 { font-size:50px; font-weight:700; letter-spacing:-.5px; line-height:1.12; }
h2 { font-size:27px; font-weight:600; margin-bottom:16px; }
h3 { font-size:13px; font-weight:600; color:ACCENT; text-transform:uppercase; letter-spacing:2.5px; margin-bottom:8px; }
p,li,td,th { font-size:14.5px; line-height:1.6; color:#C4C4CC; font-weight:300; }
.rule { width:44px; height:3px; background:ACCENT; margin-bottom:20px; }
.foot { position:absolute; bottom:22px; left:72px; right:72px; display:flex; justify-content:space-between;
        font-size:10.5px; color:#5A5A62; letter-spacing:1.5px; }
.logo { height:19px; } .logobig { height:33px; }
.cols { display:flex; gap:26px; }
.card { background:#101114; border:1px solid #1C1D22; border-radius:10px; padding:20px 22px; flex:1; }
.card b { color:#EDEDF2; font-weight:600; display:block; margin-bottom:6px; font-size:15px; }
.klist li { list-style:none; padding-left:19px; position:relative; margin-bottom:8px; }
.klist li:before { content:''; position:absolute; left:0; top:9px; width:8px; height:2px; background:ACCENT; }
.num { font-size:12px; color:ACCENT; font-weight:600; letter-spacing:2px; margin-bottom:5px; }
.phase { flex:1; border-top:2px solid #2A2A2A; padding-top:13px; }
.phase b { color:#EDEDF2; font-size:15px; display:block; margin-bottom:6px; }
table.t { border-collapse:collapse; width:100%; }
table.t td, table.t th { padding:9px 12px; text-align:left; border-bottom:1px solid #1C1D22;
                         font-size:14px; vertical-align:top; }
table.t th { font-size:11.5px; color:ACCENT; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; }
table.t td.r, table.t th.r { text-align:right; }
.stat { font-family:Poppins,sans-serif; font-size:38px; font-weight:700; color:ACCENT; line-height:1; }
.big { font-family:Poppins,sans-serif; font-size:38px; font-weight:600; color:#EDEDF2; }
table.meta td { font-size:13px; padding:4px 0; color:#C4C4CC; }
table.meta td.mk { color:ACCENT; font-size:10.5px; letter-spacing:2px; text-transform:uppercase;
                   padding-right:26px; white-space:nowrap; font-weight:600; }
.note { font-size:11.5px; color:#7A7A84; line-height:1.5; margin-top:18px; }
.strip { display:grid; gap:14px; }
.scol { background:#101114; border:1px solid #1C1D22; border-top:3px solid ACCENT;
        border-radius:0 0 10px 10px; padding:16px 16px 18px; }
.scol b { color:#EDEDF2; font-weight:600; display:block; font-size:15px; }
.scol p { font-size:12px; line-height:1.5; }
.sstat { color:ACCENT; font-size:11.5px; font-weight:600; margin:4px 0 9px; }
.sblock { margin-bottom:16px; }
.sblock b { color:#EDEDF2; font-weight:600; display:block; font-size:15px; margin-bottom:3px; }
.sblock p { font-size:13.5px; }
.panel { flex:1; background:#101114; border:1px solid #1C1D22; border-radius:10px; padding:20px; }
.pgrid { display:grid; grid-template-columns:repeat(3,1fr); gap:11px; }
.pcell { background:#16171B; border:1px solid #22232A; border-top:2px solid ACCENT; border-radius:0 0 7px 7px;
         padding:11px 12px; }
.pcell b { color:#EDEDF2; font-weight:600; font-size:13.5px; display:block; }
.psub { color:ACCENT; font-size:11.5px; font-weight:600; }
.pfoot { font-size:10px; color:#7A7A84; margin-top:9px; letter-spacing:.3px; }
.pwide { background:#16171B; border:1px solid #22232A; border-radius:7px; padding:11px 12px; margin-top:11px; }
.pwide b { color:ACCENT; font-weight:600; font-size:11.5px; letter-spacing:1.5px; text-transform:uppercase;
           display:block; margin-bottom:4px; }
.pwide .psub { color:#C4C4CC; font-weight:300; font-size:12px; }
.pstat { border-left:2px solid ACCENT; padding-left:12px; margin-bottom:14px; }
.pk { font-size:10px; color:ACCENT; letter-spacing:2px; text-transform:uppercase; font-weight:600; }
.pv { font-size:14px; color:#EDEDF2; margin-top:2px; }
.photocap { position:absolute; bottom:52px; right:22px; background:rgba(10,10,10,.72);
            border-radius:4px; padding:5px 10px; font-size:10px; color:#9A9AA4; letter-spacing:.5px; }
.gtitle { color:ACCENT; font-size:11.5px; font-weight:600; letter-spacing:1.5px; text-transform:uppercase;
          margin:14px 0 4px; }
table.sched td, table.sched th { padding:5px 10px; font-size:13px; }
.irow { display:flex; justify-content:space-between; align-items:center; gap:16px;
        background:#101114; border:1px solid #1C1D22; border-radius:8px; padding:13px 16px; margin-bottom:9px; }
.irow span { font-size:13.5px; color:#C4C4CC; }
.irow b { font-family:Poppins,sans-serif; font-size:16px; color:#EDEDF2; white-space:nowrap; }
.itotal { border-color:ACCENT; background:#0E1416; }
.itotal span { color:#EDEDF2; font-weight:500; }
.itotal b { color:ACCENT; font-size:24px; }
.qgrid { display:grid; grid-template-columns:1fr 1fr; gap:16px 44px; margin-top:6px; }
.qitem { display:flex; gap:13px; }
.qn { font-family:Poppins,sans-serif; color:ACCENT; font-size:15px; font-weight:600; line-height:1.3; }
.qitem b { color:#EDEDF2; font-weight:600; font-size:14.5px; display:block; margin-bottom:2px; }
.qitem p { font-size:12.5px; line-height:1.5; }
.grid { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; }
.gcell { background:#101114; border:1px solid #1C1D22; border-radius:10px; padding:16px 17px; }
.gcell b { color:#EDEDF2; font-weight:600; display:block; margin:3px 0 5px; font-size:14px; }
.gcell p { font-size:12.5px; line-height:1.5; }
.csclient { font-family:Poppins,sans-serif; font-size:27px; font-weight:700; color:ACCENT;
            white-space:nowrap; }
.card.hasimg { padding-top:0; overflow:hidden; }
.card.hasimg .cimg { display:block; width:100%; height:190px; object-fit:cover; margin-bottom:16px; }
.phase.hasimg { border-top:0; padding-top:0; }
.phase.hasimg .cimg { display:block; width:100%; height:132px; object-fit:cover; border-radius:8px;
                      margin-bottom:12px; }
.gcell.hasimg { padding-top:0; overflow:hidden; }
.gcell.hasimg .cimg { display:block; width:100%; height:118px; object-fit:cover; margin-bottom:8px; }
.icap { font-size:10px; color:#7A7A84; letter-spacing:.3px; margin:-8px 0 10px; }
a.imglink { text-decoration:none; color:inherit; display:block; }
.thumbcap { font-size:12.5px; margin-top:8px; color:#C4C4CC; }
.thumbcap b { color:#EDEDF2; font-weight:600; }
""".replace("ACCENT", accent)


def _sentence(text) -> str:
    """Model captions arrive without terminal punctuation, which ran straight into the 'Click to watch'
    the template appends. Close the sentence unless it already closes itself."""
    t = (text or "").strip()
    return t if (not t or t[-1] in ".!?") else t + "."


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=False)


def _pimg(img: dict | None) -> str:
    """An image header for a card, phase or grid cell: {path, href, caption}. Resolved by code, so a
    page can only ever show a file that exists; `href` (a film or clip URL) makes it clickable."""
    if not img or not img.get("path"):
        return ""
    fx = str(img.get("focus") or "").strip()      # e.g. "center 20%": where the subject sits in the frame
    pos = f' style="object-position:{_esc(fx)}"' if fx else ""
    tag = f'<img class="cimg" src="{_b64(img["path"])}"{pos}>'
    if img.get("href"):
        tag = f'<a class="imglink" href="{_esc(img["href"])}">{tag}</a>'
    cap = f'<div class="icap">{_esc(img["caption"])}</div>' if img.get("caption") else ""
    return tag + cap


class _Deck:
    def __init__(self, company: dict, customer: str, accent: str, logo: str, label: str):
        self.co, self.customer, self.accent, self.logo = company, customer, accent, logo
        self.label, self.pages = label, []

    def _foot(self, section: str, right: int = 72) -> str:
        """`right` narrows the footer so a full-bleed photograph cannot swallow the page number."""
        n = len(self.pages) + 1
        lg = f'<img class="logo" src="{self.logo}">' if self.logo else \
             f'<span style="letter-spacing:1.5px">{_esc(self.co.get("name"))}</span>'
        return (f'<div class="foot" style="right:{right}px">{lg}<span>{_esc(section)}</span>'
                f'<span>{_esc(self.label)} &middot; {n:02d}</span></div>')

    def cover(self, title: str, standfirst: str, image: str | None,
              section: str = "Proposal"):
        img = (f'<img style="position:absolute;top:0;left:0;width:1280px;height:720px;object-fit:cover" '
               f'src="{_b64(image)}">'
               '<div style="position:absolute;top:0;left:0;width:1280px;height:720px;background:'
               'linear-gradient(to top, rgba(10,10,10,.95) 20%, rgba(10,10,10,.18) 58%, rgba(10,10,10,.4))"></div>'
               ) if image else ""
        lg = f'<img class="logobig" style="position:absolute;top:54px;left:72px" src="{self.logo}">' if self.logo else ""
        self.pages.append(
            f'<div class="pg">{img}'
            f'<div style="position:absolute;top:0;left:0;width:1280px;height:6px;background:{self.accent}"></div>'
            f'{lg}<div style="position:absolute;bottom:104px;left:72px;right:72px"><div class="rule"></div>'
            f'<h1>{title}</h1><p style="margin-top:16px;max-width:720px">{_esc(standfirst)}</p></div>'
            f'{self._foot(section)}</div>')

    def lead(self, kicker: str, title: str, caption: str, image: str | None, href: str,
             section: str = "Watch"):
        """A film shown FULL FRAME before anything is explained: the whole page is the link. For the
        one piece of work that says more than the deck does."""
        img = (f'<img style="position:absolute;top:0;left:0;width:1280px;height:720px;object-fit:cover" '
               f'src="{_b64(image)}">') if image else ""
        play = ('<div style="position:absolute;top:288px;left:568px;width:144px;height:144px;border-radius:72px;'
                'background:rgba(10,10,10,.55);border:3px solid rgba(255,255,255,.85)"></div>'
                '<div style="position:absolute;top:318px;left:626px;width:0;height:0;border-top:42px solid transparent;'
                'border-bottom:42px solid transparent;border-left:66px solid #FFFFFF"></div>')
        self.pages.append(
            f'<div class="pg">{img}'
            '<div style="position:absolute;top:0;left:0;width:1280px;height:720px;background:'
            'linear-gradient(to top, rgba(10,10,10,.92) 12%, rgba(10,10,10,.08) 45%, rgba(10,10,10,.35))"></div>'
            f'{play}'
            f'<a href="{_esc(href)}" style="position:absolute;top:0;left:0;width:1280px;height:720px;display:block;'
            'text-decoration:none"></a>'
            f'<div style="position:absolute;bottom:96px;left:72px;right:72px"><h3>{_esc(kicker)}</h3>'
            f'<div class="rule"></div><h1 style="font-size:44px">{_esc(title)}</h1>'
            f'<p style="margin-top:12px;max-width:760px">{_esc(caption)}</p></div>'
            f'{self._foot(section)}</div>')

    def cards(self, kicker: str, heading: str, cards: list, bullets: list | None = None, section: str = "",
              images: list | None = None):
        """`images` = one {path, href, caption} per card, by position; a card with an image gets it as a
        full-width header, and the bullet list is capped at three so the page cannot overflow."""
        ims = images or []
        c = "".join(
            f'<div class="card{" hasimg" if (i < len(ims) and ims[i]) else ""}">{_pimg(ims[i] if i < len(ims) else None)}'
            f'<b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
            for i, x in enumerate(cards[:3]))
        b = ""
        if bullets:
            cap = 3 if any(ims) else 5
            b = ('<ul class="klist" style="margin-top:22px;max-width:1040px">'
                 + "".join(f"<li>{_esc(x)}</li>" for x in bullets[:cap]) + "</ul>")
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2><div class="cols" style="margin-top:4px">{c}</div>{b}</div>'
            f'{self._foot(section or kicker)}</div>')

    def phases(self, kicker: str, heading: str, phases: list, cards: list | None = None, section: str = "",
               images: list | None = None):
        """`images` = one {path, href, caption} per phase, by position, shown above the step."""
        ims = images or []
        p = "".join(
            f'<div class="phase{" hasimg" if (i < len(ims) and ims[i]) else ""}">{_pimg(ims[i] if i < len(ims) else None)}'
            f'<div class="num">{_esc(x.get("when"))}</div>'
            f'<b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>' for i, x in enumerate(phases[:4]))
        c = ""
        if cards:
            c = ('<div class="cols" style="margin-top:26px">'
                 + "".join(f'<div class="card"><b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                           for x in cards[:3]) + "</div>")
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2><div class="cols" style="margin-top:4px">{p}</div>{c}</div>'
            f'{self._foot(section or kicker)}</div>')

    def covermeta(self, kicker: str, title: str, title2: str, standfirst: str, meta: list,
                  image: str | None = None, section: str = "Proposal"):
        """Title cover carrying the deal's hard facts: client, date, venue, document number."""
        img = (f'<img style="position:absolute;top:0;left:0;width:1280px;height:720px;object-fit:cover" '
               f'src="{_b64(image)}">'
               '<div style="position:absolute;top:0;left:0;width:1280px;height:720px;background:'
               'linear-gradient(to right, rgba(10,10,10,.97) 46%, rgba(10,10,10,.55))"></div>') if image else ""
        lg = (f'<img class="logobig" style="position:absolute;top:50px;left:72px" src="{self.logo}">'
              if self.logo else "")
        m = "".join(f'<tr><td class="mk">{_esc(x.get("k"))}</td><td>{_esc(x.get("v"))}</td></tr>'
                    for x in (meta or [])[:5])
        t2 = f'<br><span style="color:{self.accent}">{_esc(title2)}</span>' if title2 else ""
        self.pages.append(
            f'<div class="pg">{img}'
            f'<div style="position:absolute;top:0;left:0;width:1280px;height:6px;background:{self.accent}"></div>'
            f'{lg}<div style="position:absolute;top:186px;left:72px;right:72px">'
            f'<h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h1>{_esc(title)}{t2}</h1>'
            f'<p style="margin-top:18px;max-width:660px">{_esc(standfirst)}</p></div>'
            f'<table class="meta" style="position:absolute;bottom:78px;left:72px">{m}</table>'
            f'{self._foot(section)}</div>')

    def strip(self, kicker: str, heading: str, intro: str, cols: list, note: str = "", section: str = ""):
        """Up to five parallel columns, each with an accent top rule, a headline stat and a body.
        cards() caps at three, and a five-format programme has to show all five."""
        cs = (cols or [])[:5]
        c = "".join('<div class="scol">'
                    f'<b>{_esc(x.get("title"))}</b>'
                    f'<div class="sstat">{_esc(x.get("stat"))}</div>'
                    f'<p>{_esc(x.get("body"))}</p></div>' for x in cs)
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2>'
            + (f'<p style="max-width:1060px;margin-bottom:20px">{_esc(intro)}</p>' if intro else "")
            + f'<div class="strip" style="grid-template-columns:repeat({len(cs) or 1},1fr)">{c}</div>'
            + (f'<p class="note">{_esc(note)}</p>' if note else "")
            + f'</div>{self._foot(section or kicker)}</div>')

    def split(self, kicker: str, heading: str, blocks: list, panel: dict, note: str = "",
              section: str = ""):
        """The argument on the left, a diagram or facility panel on the right."""
        b = "".join(f'<div class="sblock"><b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                    for x in (blocks or [])[:5])
        p = panel or {}
        cells = "".join('<div class="pcell">'
                        f'<b>{_esc(x.get("title"))}</b>'
                        f'<div class="psub">{_esc(x.get("sub"))}</div>'
                        f'<div class="pfoot">{_esc(x.get("foot"))}</div></div>'
                        for x in (p.get("cells") or [])[:6])
        pf = p.get("foot") or {}
        foot = (f'<div class="pwide"><b>{_esc(pf.get("title"))}</b>'
                f'<div class="psub">{_esc(pf.get("body"))}</div></div>') if pf else ""
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2>'
            f'<div style="display:flex;gap:40px;margin-top:16px">'
            f'<div style="width:520px">{b}</div>'
            f'<div class="panel"><h3 style="margin-bottom:14px">{_esc(p.get("title"))}</h3>'
            f'<div class="pgrid">{cells}</div>{foot}</div></div>'
            + (f'<p class="note">{_esc(note)}</p>' if note else "")
            + f'</div>{self._foot(section or kicker)}</div>')

    def photo(self, kicker: str, heading: str, sub: str, body: str, stats: list,
              image: str | None, caption: str = "", note: str = "", section: str = "",
              focus: str = "center"):
        """A room, a set or a location: the argument on the left, the photograph bleeding off the right.
        `focus` (left/center/right) is where the subject sits in a landscape source, so the portrait
        crop keeps it: a director standing at frame left is otherwise cropped out entirely."""
        st = "".join(f'<div class="pstat"><div class="pk">{_esc(x.get("k"))}</div>'
                     f'<div class="pv">{_esc(x.get("v"))}</div></div>' for x in (stats or [])[:4])
        fx = focus if focus in ("left", "center", "right") else "center"
        img = (f'<img src="{_b64(image)}" style="position:absolute;top:0;right:0;width:592px;'
               f'height:720px;object-fit:cover;object-position:{fx} center">'
               '<div style="position:absolute;top:0;right:0;width:592px;height:720px;background:'
               'linear-gradient(to right, rgba(10,10,10,.92), rgba(10,10,10,0) 22%)"></div>'
               ) if image else ""
        cap = f'<div class="photocap">{_esc(caption)}</div>' if (image and caption) else ""
        self.pages.append(
            f'<div class="pg">{img}{cap}'
            f'<div style="position:absolute;top:58px;left:72px;width:566px">'
            f'<h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2 style="margin-bottom:6px">{_esc(heading)}</h2>'
            + (f'<div class="psub" style="margin-bottom:14px">{_esc(sub)}</div>' if sub else "")
            + f'<p>{_esc(body)}</p><div style="margin-top:26px">{st}</div></div>'
            + (f'<div class="note" style="position:absolute;bottom:52px;left:72px;width:560px">'
               f'{_esc(note)}</div>' if note else "")
            + f'{self._foot(section or kicker, right=606)}</div>')

    def schedule(self, kicker: str, heading: str, groups: list, invest: dict, footnote: str = "",
                 section: str = ""):
        """The counted deliverables beside the money. Both are stated, never estimated."""
        out = []
        for g in (groups or []):
            head = "".join(f'<th class="r">{_esc(c)}</th>' for c in (g.get("cols") or []))
            rows = "".join(
                f'<tr><td>{_esc(r.get("label"))}</td>'
                + "".join(f'<td class="r">{_esc(v)}</td>' for v in (r.get("values") or []))
                + "</tr>" for r in (g.get("rows") or []))
            out.append(f'<div class="gtitle">{_esc(g.get("title"))}</div>'
                       + (f'<table class="t sched"><tr><th></th>{head}</tr>{rows}</table>' if rows else "")
                       + (f'<p class="note" style="margin:6px 0 0">{_esc(g.get("note"))}</p>'
                          if g.get("note") else ""))
        iv = invest or {}
        irows = "".join(f'<div class="irow"><span>{_esc(x.get("item"))}</span>'
                        f'<b>{_esc(x.get("amount"))}</b></div>' for x in (iv.get("rows") or []))
        tot = iv.get("total") or {}
        total = (f'<div class="irow itotal"><span>{_esc(tot.get("item"))}</span>'
                 f'<b>{_esc(tot.get("amount"))}</b></div>') if tot else ""
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2>'
            f'<div style="display:flex;gap:44px;margin-top:14px">'
            f'<div style="width:640px">{"".join(out)}</div>'
            f'<div style="width:452px">{irows}{total}'
            + (f'<p class="note" style="margin-top:14px">{_esc(iv.get("note"))}</p>'
               if iv.get("note") else "")
            + '</div></div></div>'
            # ABSOLUTE, not in flow: this page's left column grows with the deliverable count, and a
            # footnote in flow pushed it past 720px and spilled a BLANK page out of weasyprint.
            + (f'<p class="note" style="position:absolute;bottom:52px;left:72px;right:72px">'
               f'{_esc(footnote)}</p>' if footnote else "")
            + f'{self._foot(section or kicker)}</div>')

    def qa(self, kicker: str, heading: str, items: list, note: str = "", section: str = ""):
        """Their questions, numbered, answered in their order. Nothing they asked is left off the page."""
        c = "".join(f'<div class="qitem"><div class="qn">{i + 1}</div>'
                    f'<div><b>{_esc(x.get("q"))}</b><p>{_esc(x.get("a"))}</p></div></div>'
                    for i, x in enumerate((items or [])[:8]))
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2><div class="qgrid">{c}</div>'
            + (f'<p class="note">{_esc(note)}</p>' if note else "")
            + f'</div>{self._foot(section or kicker)}</div>')

    def closing(self, kicker: str, heading: str, sub: str, cards: list, steps: list,
                signoff: str = "", section: str = ""):
        """Why us, then what happens next, then who is signing it."""
        c = "".join(f'<div class="card"><b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                    for x in (cards or [])[:3])
        s = "".join(f'<div class="phase"><div class="num">{i + 1}</div>'
                    f'<b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                    for i, x in enumerate((steps or [])[:3]))
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2>'
            + (f'<p style="margin-bottom:18px">{_esc(sub)}</p>' if sub else "")
            + f'<div class="cols">{c}</div>'
            + (f'<h3 style="margin:30px 0 12px">Next steps</h3><div class="cols">{s}</div>' if s else "")
            + (f'<p class="note" style="margin-top:26px">{_esc(signoff)}</p>' if signoff else "")
            + f'</div>{self._foot(section or "Next")}</div>')

    def grid(self, kicker: str, heading: str, intro: str, cells: list, section: str = "",
             images: list | None = None, big: bool = False):
        """A four-across grid of up to eight cells - the capability modules. cards() holds only three.
        `images` = one {path, href, caption} per cell, by position; with images the intro is dropped,
        because two rows of pictured cells fill the page on their own."""
        ims = images or []
        c = "".join(
            f'<div class="gcell{" hasimg" if (i < len(ims) and ims[i]) else ""}">{_pimg(ims[i] if i < len(ims) else None)}'
            f'<div class="num">{_esc(x.get("num"))}</div>'
            f'<b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
            for i, x in enumerate((cells or [])[:8]))
        if any(ims):
            intro = ""
        h = (f'<h1 style="font-size:40px;margin-bottom:18px">{_esc(heading)}</h1>' if big
             else f'<h2>{_esc(heading)}</h2>')
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'{h}'
            + (f'<p style="max-width:1060px;margin-bottom:18px">{_esc(intro)}</p>' if intro else "")
            + f'<div class="grid">{c}</div></div>{self._foot(section or kicker)}</div>')

    def casestudy(self, kicker: str, client: str, heading: str, body: str, bullets: list,
                  films: list, section: str = ""):
        """One named client: what we did, and their OWN films from the library underneath at native
        16:9. The films are supplied by code, so the page can only ever cite work we actually hold."""
        n = max(1, len(films))
        w = {1: 470, 2: 470, 3: 352}.get(n, 352)
        h = int(w * 9 / 16)
        items = []
        for f in films:
            th = f.get("thumb")
            img = (f'<img src="{_b64(th)}" style="width:{w}px;height:{h}px;display:block;border-radius:8px">'
                   if th else f'<div style="width:{w}px;height:{h}px;background:#141414;border-radius:8px"></div>')
            items.append(
                f'<a href="https://www.youtube.com/watch?v={_esc(f["youtube_video_id"])}" '
                f'style="text-decoration:none;display:block;width:{w}px">{img}'
                f'<p class="thumbcap"><b>{_esc(f.get("label") or f.get("title"))}</b> &middot; '
                f'{_esc(_sentence(f.get("caption")))} Click to watch.</p></a>')
        b = ""
        if bullets:
            b = ('<ul class="klist" style="margin-top:14px;max-width:1060px">'
                 + "".join(f"<li>{_esc(x)}</li>" for x in bullets[:3]) + "</ul>")
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<div style="display:flex;align-items:baseline;gap:18px;margin-bottom:10px">'
            f'<span class="csclient">{_esc(client)}</span><h2 style="margin:0">{_esc(heading)}</h2></div>'
            f'<p style="max-width:1060px">{_esc(body)}</p>{b}'
            f'<div style="display:flex;gap:24px;margin-top:20px">{"".join(items)}</div></div>'
            f'{self._foot(section or "Case study")}</div>')

    def samples(self, kicker: str, heading: str, intro: str, films: list, section: str = ""):
        """Films shown at native 16:9, fixed width so the page never overflows."""
        n = max(1, len(films))
        w = {1: 566, 2: 470, 3: 352}.get(n, 352)
        h = int(w * 9 / 16)
        items = []
        for f in films:
            th = f.get("thumb")
            img = (f'<img src="{_b64(th)}" style="width:{w}px;height:{h}px;display:block;border-radius:8px">'
                   if th else f'<div style="width:{w}px;height:{h}px;background:#141414;border-radius:8px"></div>')
            items.append(
                f'<a href="https://www.youtube.com/watch?v={_esc(f["youtube_video_id"])}" '
                f'style="text-decoration:none;display:block;width:{w}px">{img}'
                f'<p class="thumbcap"><b>{_esc(f.get("label") or f.get("title"))}</b> &middot; '
                f'{_esc(_sentence(f.get("caption")))} Click to watch.</p></a>')
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2>'
            + (f'<p style="max-width:1060px;margin-bottom:16px">{_esc(intro)}</p>' if intro else "")
            + f'<div style="display:flex;gap:24px;margin-top:6px">{"".join(items)}</div></div>'
            f'{self._foot(section or "Our work")}</div>')

    def investment(self, kicker: str, headline: str, blurb: str, rows: list, cards: list | None = None,
                   section: str = ""):
        r = "".join(f'<tr><td>{_esc(x.get("item"))}</td><td>{_esc(x.get("detail"))}</td>'
                    f'<td class="r">{_esc(x.get("amount"))}</td></tr>' for x in rows)
        c = ""
        if cards:
            c = ('<div class="cols" style="margin-top:22px">'
                 + "".join(f'<div class="card"><b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                           for x in cards[:3]) + "</div>")
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<div style="display:flex;align-items:baseline;gap:24px;margin-bottom:20px">'
            f'<span class="big">{_esc(headline)}</span>'
            f'<p style="max-width:540px">{_esc(blurb)}</p></div>'
            f'<table class="t" style="max-width:1080px">'
            f'<tr><th>Component</th><th>Included</th><th class="r">Amount</th></tr>{r}</table>{c}</div>'
            f'{self._foot(section or "Investment")}</div>')

    def html(self) -> str:
        return ('<html><head><meta charset="utf-8"><style>' + _css(self.accent) + "</style></head><body>"
                + "".join(self.pages) + "</body></html>")


def to_pdf(html_str: str, out_path: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_str)
        src = f.name
    subprocess.run(["weasyprint", src, out_path], check=True, timeout=300,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.unlink(src)
    return out_path


# --------------------------------------------------------------------------- authoring

_SPEC_SCHEMA = """{
 "accent": "#RRGGBB, sympathetic to the CLIENT's brand palette",
 "cover": {"title": "<=6 words, may contain <br>", "standfirst": "2-3 sentences",
           "image_subject": "what the hero photograph shows - the CLIENT's world, concrete and shootable",
           "image_palette": "the colour treatment, e.g. 'deep charcoal with warm amber accents'"},
 "brief": {"kicker": "01 - The brief, as we read it", "heading": "one line",
           "cards": [{"title": "", "body": ""}], "bullets": ["", ""]},
 "approach": {"kicker": "02 - The approach", "heading": "one line",
              "phases": [{"when": "STEP 1", "title": "", "body": ""}],
              "cards": [{"title": "", "body": ""}]},
 "samples": {"kicker": "03 - Our work", "heading": "one line", "intro": "1-2 sentences",
             "categories": ["media library slugs matching this enquiry, lowercase"],
             "captions": ["one short caption per film, in order, no film titles"]},
 "timeline": {"kicker": "04 - How it runs", "heading": "one line, states the elapsed span",
              "phases": [{"when": "WEEK 1", "title": "", "body": ""}],
              "cards": [{"title": "", "body": ""}]},
 "investment": {"kicker": "05 - The investment", "headline": "e.g. AED 62,600 + VAT",
                "blurb": "one sentence", "rows": [{"item": "", "detail": "", "amount": ""}],
                "cards": [{"title": "", "body": ""}]}
}"""


def author_spec(company: dict, customer: str, brief: str, quotation: dict | None = None,
                extra_facts: str = "") -> dict:
    """The model writes the proposal's COPY under the company's live skill rules. It never sets a price
    or names a sample film: code supplies both afterwards."""
    skill = store.get_skill_by_key(company["id"], "sales-quotation") or \
        store.get_skill_by_key(company["id"], "sales-first-response")
    rules = worker._rules_block(skill) if skill else ""
    money = ""
    if quotation:
        lines = "; ".join(f"{s.get('header')}: " + ", ".join(
            f"{i.get('desc', '')[:70]} = {i.get('unit')}" for i in s.get("items", []))
            for s in (quotation.get("sections") or []))
        money = (f"\nQUOTATION FACTS (use these EXACTLY, never alter a figure): total "
                 f"{quotation.get('currency', 'AED')} {quotation.get('net')} + VAT"
                 f"{', number ' + quotation['number'] if quotation.get('number') else ''}. Lines: {lines}")
    system = "\n\n".join(filter(None, [
        "You write PROPOSAL DECKS for a production company. Return ONLY the JSON spec described below - "
        "the layout is built by code from it.",
        worker._now_line(),
        worker._company_context(company),
        rules,
        "HARD RULES: never invent a price, a date, a statistic or a client name; every figure comes from the "
        "quotation facts you are given. Never name a sample film - you supply the media-library CATEGORY "
        "SLUGS and one caption per film, and the system picks the actual films by rating. Keep every card "
        "body under 45 words. No em dashes. Write plainly, no marketing flourish, no superlatives. The "
        "timeline states an ELAPSED span built from parallel tracks, never a serial sum of phases, and names "
        "the client-side variable that holds the date.",
        "SPEC:\n" + _SPEC_SCHEMA,
    ]))
    return provider.think_json(
        system, f"Client: {customer}\n\nBrief:\n{brief}{money}\n\n{extra_facts}",
        model="claude-fable-5", max_tokens=4000, purpose="deck-spec", company=company.get("slug"))


def build(company_slug: str, customer: str, brief: str, *, quotation: dict | None = None,
          label: str | None = None, extra_facts: str = "", out_dir: str = "/tmp",
          filename: str = "proposal.pdf") -> dict:
    """Author + render a house-format proposal deck. Returns {path, pages, films, spec}."""
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    spec = author_spec(co, customer, brief, quotation, extra_facts) or {}
    accent = (spec.get("accent") or _ACCENT_DEFAULT).strip()
    if not re.match(r"^#[0-9A-Fa-f]{6}$", accent):
        accent = _ACCENT_DEFAULT
    import datetime
    lbl = label or f"Prepared for {customer} · {datetime.date.today():%B %Y}"
    d = _Deck(co, customer, accent, _logo(co), lbl)

    cv = spec.get("cover") or {}
    img = cover_image(cv.get("image_subject") or f"the world of {customer}",
                      cv.get("image_palette") or "deep charcoal with restrained accent light",
                      company_slug, out_dir)
    d.cover(cv.get("title") or _esc(customer), cv.get("standfirst") or "", img)

    for key, fn in (("brief", "cards"), ("approach", "phases")):
        s = spec.get(key) or {}
        if not s:
            continue
        if fn == "cards":
            d.cards(s.get("kicker", ""), s.get("heading", ""), s.get("cards") or [], s.get("bullets"))
        else:
            d.phases(s.get("kicker", ""), s.get("heading", ""), s.get("phases") or [], s.get("cards"))

    films = []
    sm = spec.get("samples") or {}
    if sm:
        picked = pick_samples(co["id"], sm.get("categories") or [], 3)
        caps = sm.get("captions") or []
        for i, f in enumerate(picked):
            films.append({**f, "thumb": thumbnail(f["youtube_video_id"], out_dir),
                          "label": f["title"], "caption": caps[i] if i < len(caps) else ""})
        if films:
            d.samples(sm.get("kicker", ""), sm.get("heading", ""), sm.get("intro", ""), films)

    tl = spec.get("timeline") or {}
    if tl:
        d.phases(tl.get("kicker", ""), tl.get("heading", ""), tl.get("phases") or [], tl.get("cards"))
    inv = spec.get("investment") or {}
    if inv:
        d.investment(inv.get("kicker", ""), inv.get("headline", ""), inv.get("blurb", ""),
                     inv.get("rows") or [], inv.get("cards"))

    path = os.path.join(out_dir, filename)
    to_pdf(d.html(), path)
    return {"path": path, "pages": len(d.pages), "films": [f["youtube_video_id"] for f in films],
            "spec": spec, "accent": accent}


# --------------------------------------------------------------------------- capabilities decks

def films_by_ids(company_id: int, video_ids: list) -> list[dict]:
    """Named films resolved from the library BY CODE, in the order asked for. A video id the library
    does not hold is DROPPED, never substituted: a case study cites our own work or it does not run."""
    ids = [v for v in (video_ids or []) if v]
    if not ids:
        return []
    rows = db.query("select youtube_video_id, title, rating, duration, categories, client "
                    "from media_assets where company_id=%s and status='live' "
                    "and youtube_video_id = any(%s)", (company_id, ids))
    order = {v: i for i, v in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r["youtube_video_id"], 999))


def _renum(kicker: str, n: int) -> str:
    """Section numbers are stamped by CODE in page order. The writer's '03 - What we control' goes
    stale the moment a page is omitted, so any leading number is replaced, never trusted."""
    k = re.sub(r"^\s*\d{1,2}\s*[-.:\u00b7]\s*", "", str(kicker or "")).strip()
    return f"{n:02d} - {k}" if k else f"{n:02d}"


def film_any(video_id: str) -> dict | None:
    """One film resolved from the library across ALL of the owner's companies, for a lead piece that
    belongs to a sister brand (FilmSpoke is a Sensa company). Unknown id = None, never a guess."""
    if not video_id:
        return None
    return db.one("select youtube_video_id, title, rating, duration, categories, client, company_id "
                  "from media_assets where status='live' and youtube_video_id=%s limit 1", (video_id,))


_CAPS_SCHEMA = """{
 "accent": "#RRGGBB, sympathetic to the AUDIENCE's world",
 "cover": {"title": "<=6 words, may contain <br>", "standfirst": "2-3 sentences",
           "image_subject": "what the hero photograph shows, concrete and shootable, no text in frame",
           "image_palette": "the colour treatment"},
 "opening": {"kicker": "01 - Why this matters", "heading": "one line",
             "cards": [{"title": "", "body": ""}], "bullets": ["", ""]},
 "platform": {"kicker": "02 - How it works", "heading": "one line",
              "phases": [{"when": "STEP 01", "title": "", "body": ""}],
              "cards": [{"title": "", "body": ""}]},
 "modules": {"kicker": "03 - What we control", "heading": "one line", "intro": "1-2 sentences",
             "cells": [{"num": "01", "title": "", "body": "under 26 words"}]},
 "module_pages": [{"key": "the module KEY you were given, copied exactly",
                   "kicker": "e.g. 'Module 01 - Characters'", "heading": "one line, what we can do",
                   "sub": "one short strap line", "body": "2-3 sentences, under 75 words, the capability",
                   "stats": [{"k": "SHORT LABEL", "v": "a fact from that module's source, under 8 words"}]}],
 "case_studies": [{"key": "the KEY you were given, copied exactly", "kicker": "04 - Case study",
                   "heading": "one line", "body": "2-3 sentences",
                   "bullets": ["", ""], "captions": ["one short caption per film, in order"]}],
 "close": {"kicker": "How we would start", "heading": "one line",
           "phases": [{"when": "STEP 1", "title": "", "body": ""}],
           "cards": [{"title": "", "body": ""}]}
}"""


def author_capabilities_spec(company: dict, audience: str, focus: str, case_facts: str,
                             extra_facts: str = "", module_facts: str = "") -> dict:
    """The model writes the deck's COPY under the company's live skill rules. Every verifiable fact is
    handed to it; it supplies no film titles, no numbers and no client claims of its own."""
    skill = store.get_skill_by_key(company["id"], "sales-quotation") or \
        store.get_skill_by_key(company["id"], "sales-first-response")
    rules = worker._rules_block(skill) if skill else ""
    system = "\n\n".join(filter(None, [
        "You write CAPABILITY DECKS for a production company: a leave-behind that explains what the "
        "company can do and proves it with its own past work. Return ONLY the JSON spec described "
        "below - the layout is built by code from it.",
        worker._now_line(),
        worker._company_context(company),
        rules,
        "HARD RULES: never invent a statistic, an award, a date, a client name or a project detail. Every "
        "such fact must already appear in the VERIFIED FACTS you are given, and anything not there is "
        "simply left out. Never name or describe a film: the system supplies the actual films from the "
        "media library and you write only the captions, in the order the films are listed for that case "
        "study. Return one case_studies entry per KEY you are given, copying the key exactly. The "
        "client name already prints beside each case-study heading, so the HEADING MUST NOT "
        "CONTAIN IT: write what the work proves, not who it was for. Keep every "
        "card body under 42 words and every module cell under 26 words. No em dashes, no superlatives, no "
        "marketing flourish. Write for a senior public-sector audience: plain, specific, unhurried.",
        ("MODULE PAGES: you are given MODULES, each with a KEY and its own source text. Return one "
         "module_pages entry per key, copied exactly, in the order given. Each page explains that one "
         "area of capability using ONLY its source text: what we can deliver for the client, what they "
         "control and approve. Say what we can do, not how the software does it. Up to four stats per "
         "page, each a fact lifted from that source, never a number you computed. The page carries the "
         "module's own photograph, so do not describe an image.")
        if module_facts else "",
        "SPEC:\n" + _CAPS_SCHEMA,
    ]))
    return provider.think_json(
        system,
        f"Audience: {audience}\n\nWhat the deck is for:\n{focus}\n\n"
        f"VERIFIED FACTS (the only facts you may use):\n{extra_facts}\n\n"
        + (f"MODULES, one module_pages entry each, keys copied exactly:\n{module_facts}\n\n"
           if module_facts else "")
        + f"CASE STUDIES to write, one entry each, keys copied exactly:\n{case_facts}",
        # 16k: seven module pages plus four case studies ran past 6k and the JSON truncated to {}.
        model="claude-fable-5", max_tokens=16000, purpose="caps-deck-spec", company=company.get("slug"))


def _module_image(ref: str | None, key: str, out_dir: str) -> str | None:
    """A module's photograph, resolved BY CODE from a local path or an https URL (the company's own
    site assets). Anything that does not fetch, or is not an image, becomes no image, never a guess."""
    if not ref:
        return None
    if os.path.exists(ref):
        return ref
    if not str(ref).startswith("http"):
        return None
    import urllib.request
    path = os.path.join(out_dir, f"deck-mod-{re.sub(r'[^a-z0-9]', '', key.lower())}.jpg")
    try:
        req = urllib.request.Request(ref, headers={"User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
        from PIL import Image
        Image.open(path).convert("RGB").save(path, quality=90)
        return path
    except Exception:  # noqa: BLE001
        return None


def _page_images(page_images: dict | None, out_dir: str) -> dict:
    """{opening: [{image, href, caption}], platform: [...], grid: [...]} resolved BY CODE to local
    files, in position order. An image that does not fetch becomes an empty slot, never a guess."""
    out = {}
    for page, items in (page_images or {}).items():
        lst = []
        for i, it in enumerate(items or []):
            it = it or {}
            path = _module_image(it.get("image"), f"{page}-{i + 1}", out_dir)
            lst.append({"path": path, "href": it.get("href"), "caption": it.get("caption"),
                        "focus": it.get("focus")} if path else None)
        out[str(page)] = lst
    return out


def build_capabilities(company_slug: str, audience: str, focus: str, *,
                       case_studies: list | None = None, extra_facts: str = "",
                       modules: list | None = None, page_images: dict | None = None,
                       cover_subject: str = "", cover_palette: str = "",
                       lead_film: dict | None = None, omit: list | None = None,
                       spec_path: str | None = None,
                       label: str | None = None, out_dir: str = "/tmp",
                       filename: str = "capabilities.pdf") -> dict:
    """Author + render a house-format CAPABILITY deck: what we do, how it works, and named case
    studies proved with our own films. `case_studies` is [{key, client, video_ids, facts}] - the films
    are resolved from the library by code, so a case study can never cite work we do not have.
    `modules` is [{key, title, image, facts, focus}] - one full page per area of capability, its copy
    written from `facts` (the module's own published text) beside its own photograph (`image` = a
    path or URL, fetched by code; `focus` = left/center/right, where the subject sits)."""
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    mods = []
    for m in (modules or []):
        if not m.get("key") or not m.get("title"):
            continue
        mods.append({**m, "image_path": _module_image(m.get("image"), str(m["key"]), out_dir)})
    module_facts = "\n\n".join(
        f"KEY {m['key']} - {m['title']}\n  source text: {m.get('facts', '')}" for m in mods)
    cases = []
    for cs in (case_studies or []):
        films = films_by_ids(co["id"], cs.get("video_ids") or [])
        if not films:
            continue
        cases.append({**cs, "films": films})
    case_facts = "\n\n".join(
        f"KEY {c['key']} - client {c.get('client')}\n  what is true about it: {c.get('facts', '')}\n"
        "  films the system will show, in this order: "
        + "; ".join(f["title"] for f in c["films"]) for c in cases) or "(none)"

    import json as _json
    spec = {}
    if spec_path and os.path.exists(spec_path):     # the words are agreed: lay them out again, unchanged
        try:
            spec = _json.load(open(spec_path, encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            spec = {}
    reused = bool(spec)
    if not spec:
        spec = author_capabilities_spec(co, audience, focus, case_facts, extra_facts, module_facts) or {}
    spec_out = os.path.join(out_dir, re.sub(r"\.pdf$", "", filename) + ".spec.json")
    try:
        with open(spec_out, "w", encoding="utf-8") as f:
            _json.dump(spec, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        spec_out = None
    skip = {str(x).strip().lower() for x in (omit or [])}
    n = 0
    accent = (spec.get("accent") or _ACCENT_DEFAULT).strip()
    if not re.match(r"^#[0-9A-Fa-f]{6}$", accent):
        accent = _ACCENT_DEFAULT
    import datetime
    lbl = label or f"Prepared for {audience} · {datetime.date.today():%B %Y}"
    d = _Deck(co, audience, accent, _logo(co), lbl)

    cv = spec.get("cover") or {}
    # An owner-stated cover subject beats the model's: he knows what the picture should say. When the
    # words are being reused, the cover he has already seen is reused too, not rolled again.
    kept_cover = os.path.join(out_dir, re.sub(r"\.pdf$", "", filename) + ".cover.jpg")
    if reused and os.path.exists(kept_cover):
        img = kept_cover
    else:
        img = cover_image(cover_subject or cv.get("image_subject") or f"the world of {audience}",
                          cover_palette or cv.get("image_palette") or "deep charcoal with restrained accent light",
                          company_slug, out_dir)
        if img:
            try:
                import shutil
                shutil.copyfile(img, kept_cover)
            except Exception:  # noqa: BLE001
                pass
    d.cover(cv.get("title") or _esc(co.get("name")), cv.get("standfirst") or "", img, section="Capabilities")

    lead_shown = None
    lf = lead_film or {}
    film = film_any(str(lf.get("video_id") or "")) if lf else None
    if film:                                     # a film the library does not hold gets no page
        d.lead(lf.get("kicker") or "Watch first", lf.get("title") or film["title"],
               lf.get("caption") or "", thumbnail(film["youtube_video_id"], out_dir),
               f"https://www.youtube.com/watch?v={film['youtube_video_id']}")
        lead_shown = film["title"]

    pimgs = _page_images(page_images, out_dir)
    op = spec.get("opening") or {}
    if op and "opening" not in skip:
        n += 1
        d.cards(_renum(op.get("kicker", ""), n), op.get("heading", ""), op.get("cards") or [],
                op.get("bullets"), images=pimgs.get("opening"))
    pf = spec.get("platform") or {}
    if pf and "platform" not in skip:
        n += 1
        d.phases(_renum(pf.get("kicker", ""), n), pf.get("heading", ""), pf.get("phases") or [],
                 pf.get("cards"), images=pimgs.get("platform"))
    md = spec.get("modules") or {}
    if md and "modules" not in skip:
        n += 1
        # With no pages before it, this page opens the deck's argument: the heading goes big.
        d.grid(_renum(md.get("kicker", ""), n), md.get("heading", ""), md.get("intro", ""),
               md.get("cells") or [], images=pimgs.get("grid"), big=(n == 1))

    mp = {str(x.get("key")): x for x in (spec.get("module_pages") or [])}
    shown_modules = []
    for m in mods:
        w = mp.get(str(m["key"])) or {}
        if not w:
            continue                             # the writer returned nothing for it: no page, no filler
        d.photo(w.get("kicker") or m["title"], w.get("heading") or m["title"], w.get("sub") or "",
                w.get("body") or "", w.get("stats") or [], m.get("image_path"),
                caption=m.get("caption") or "", section="Capabilities",
                focus=str(m.get("focus") or "center"))
        shown_modules.append(m["title"])

    written = {str(c.get("key")): c for c in (spec.get("case_studies") or [])}
    shown = []
    if cases and "case_studies" not in skip:
        n += 1
    for c in cases:
        if "case_studies" in skip:
            break
        w = written.get(str(c["key"])) or {}
        caps = w.get("captions") or []
        films = [{**f, "thumb": thumbnail(f["youtube_video_id"], out_dir),
                  "label": f["title"], "caption": caps[i] if i < len(caps) else ""}
                 for i, f in enumerate(c["films"])]
        d.casestudy(_renum(w.get("kicker") or "Case study", n), c.get("client") or "",
                    w.get("heading") or "", w.get("body") or "", w.get("bullets") or [], films)
        shown.append({"client": c.get("client"), "films": [f["title"] for f in films]})

    cl = spec.get("close") or {}
    if cl and "close" not in skip:
        d.phases(cl.get("kicker", ""), cl.get("heading", ""), cl.get("phases") or [], cl.get("cards"),
                 section="Next")

    path = to_pdf(d.html(), os.path.join(out_dir, filename))
    return {"path": path, "pages": len(d.pages), "cases": shown, "modules": shown_modules,
            "lead": lead_shown, "spec": spec, "spec_path": spec_out}


# --------------------------------------------------------------------------- explicit-spec rendering

def render_spec(company_slug: str, spec: dict, *, out_dir: str = "/tmp",
                filename: str = "deck.pdf") -> dict:
    """Render a deck whose CONTENT IS ALREADY WRITTEN, page by page, into the house format.

    build() and build_capabilities() have a model author the copy. This one has no model in it at
    all: it takes an explicit page list and lays it out. That is the difference between writing a
    deck and REBRANDING one - when the words are already agreed, or already sent to a client, a
    model rewriting them is a defect, not a feature. Every page type is one of the _Deck builders.
    """
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    accent = str(spec.get("accent") or _ACCENT_DEFAULT).strip()
    if not re.match(r"^#[0-9A-Fa-f]{6}$", accent):
        accent = _ACCENT_DEFAULT
    d = _Deck(co, spec.get("customer") or co["name"], accent, _logo(co), spec.get("label") or "")
    for pg in (spec.get("pages") or []):
        t = (pg.get("type") or "").strip().lower()
        if t == "covermeta":
            d.covermeta(pg.get("kicker", ""), pg.get("title", ""), pg.get("title2", ""),
                        pg.get("standfirst", ""), pg.get("meta") or [], pg.get("image"),
                        pg.get("section", "Proposal"))
        elif t == "cover":
            d.cover(pg.get("title", ""), pg.get("standfirst", ""), pg.get("image"),
                    pg.get("section", "Proposal"))
        elif t == "lead":
            d.lead(pg.get("kicker", ""), pg.get("title", ""), pg.get("caption", ""), pg.get("image"),
                   pg.get("href", ""), pg.get("section", "Watch"))
        elif t == "strip":
            d.strip(pg.get("kicker", ""), pg.get("heading", ""), pg.get("intro", ""),
                    pg.get("cols") or [], pg.get("note", ""), pg.get("section", ""))
        elif t == "split":
            d.split(pg.get("kicker", ""), pg.get("heading", ""), pg.get("blocks") or [],
                    pg.get("panel") or {}, pg.get("note", ""), pg.get("section", ""))
        elif t == "photo":
            d.photo(pg.get("kicker", ""), pg.get("heading", ""), pg.get("sub", ""),
                    pg.get("body", ""), pg.get("stats") or [], pg.get("image"),
                    pg.get("caption", ""), pg.get("note", ""), pg.get("section", ""),
                    focus=str(pg.get("focus") or "center"))
        elif t == "schedule":
            d.schedule(pg.get("kicker", ""), pg.get("heading", ""), pg.get("groups") or [],
                       pg.get("invest") or {}, pg.get("footnote", ""), pg.get("section", ""))
        elif t == "qa":
            d.qa(pg.get("kicker", ""), pg.get("heading", ""), pg.get("items") or [],
                 pg.get("note", ""), pg.get("section", ""))
        elif t == "closing":
            d.closing(pg.get("kicker", ""), pg.get("heading", ""), pg.get("sub", ""),
                      pg.get("cards") or [], pg.get("steps") or [], pg.get("signoff", ""),
                      pg.get("section", ""))
        elif t == "cards":
            d.cards(pg.get("kicker", ""), pg.get("heading", ""), pg.get("cards") or [],
                    pg.get("bullets"), pg.get("section", ""))
        elif t == "phases":
            d.phases(pg.get("kicker", ""), pg.get("heading", ""), pg.get("phases") or [],
                     pg.get("cards"), pg.get("section", ""))
        elif t == "grid":
            d.grid(pg.get("kicker", ""), pg.get("heading", ""), pg.get("intro", ""),
                   pg.get("cells") or [], pg.get("section", ""))
        elif t == "investment":
            d.investment(pg.get("kicker", ""), pg.get("headline", ""), pg.get("blurb", ""),
                         pg.get("rows") or [], pg.get("cards"), pg.get("section", ""))
        else:
            raise ValueError(f"unknown page type '{t}'")
    return {"path": to_pdf(d.html(), os.path.join(out_dir, filename)), "pages": len(d.pages)}


# --------------------------------------------------------------------------- rebranding a deck

_REBRAND_SCHEMA = """{
 "accent": "#RRGGBB - the company's own brand accent unless told otherwise",
 "customer": "who the deck is for",
 "label": "the running footer label, e.g. 'Proposal SEN-2026-0011 - November 2026'",
 "pages": [ one object per page of the source, in the SAME ORDER, each with a "type":
  {"type":"covermeta","kicker":"","title":"first line","title2":"second line, printed in the accent",
   "standfirst":"","meta":[{"k":"CLIENT","v":""}],"image":null,"section":"Proposal"},
  {"type":"strip","kicker":"","heading":"","intro":"","cols":[{"title":"","stat":"","body":""}],
   "note":"the strapline along the foot of the page","section":""},
  {"type":"split","kicker":"","heading":"","blocks":[{"title":"","body":""}],
   "panel":{"title":"","cells":[{"title":"","sub":"","foot":""}],"foot":{"title":"","body":""}},
   "note":"","section":""},
  {"type":"photo","kicker":"","heading":"","sub":"","body":"","stats":[{"k":"","v":""}],
   "image":"IMAGE 3 - the number you were given, or null","caption":"","note":"","section":""},
  {"type":"phases","kicker":"","heading":"","phases":[{"when":"","title":"","body":""}],
   "cards":[{"title":"","body":""}],"section":""},
  {"type":"schedule","kicker":"","heading":"",
   "groups":[{"title":"","cols":["MASTERS","FILES"],
              "rows":[{"label":"","values":["",""]}],"note":""}],
   "invest":{"rows":[{"item":"","amount":""}],"total":{"item":"","amount":""},"note":""},
   "footnote":"","section":""},
  {"type":"qa","kicker":"","heading":"","items":[{"q":"","a":""}],"note":"","section":""},
  {"type":"closing","kicker":"","heading":"","sub":"","cards":[{"title":"","body":""}],
   "steps":[{"title":"","body":""}],"signoff":"","section":""},
  {"type":"grid","kicker":"","heading":"","intro":"","cells":[{"num":"","title":"","body":""}]},
  {"type":"cards","kicker":"","heading":"","cards":[{"title":"","body":""}],"bullets":[""]}
 ]
}"""


def rebrand_spec(company: dict, source_text: str, images: list, notes: str = "") -> dict:
    """Map an existing deck onto the house page vocabulary. This is TRANSCRIPTION, not authoring."""
    imglist = "\n".join(f"IMAGE {i + 1}: a photograph that appeared on source page {p}"
                        for i, (p, _) in enumerate(images)) or "(the source carries no photographs)"
    system = "\n\n".join(filter(None, [
        "You REBRAND an existing deck: the same document, re-laid-out in the house format. Return ONLY "
        "the JSON spec described below.",
        worker._now_line(),
        worker._company_context(company),
        "THE ONE RULE THAT MATTERS: keep the information EXACTLY the same. Reproduce the source's own "
        "wording. Do not rewrite it into your own voice, do not improve it, do not shorten it, do not "
        "add a single fact, and above all do not DROP anything: every heading, every body line, every "
        "number, every footnote and every strapline in the source must appear somewhere in your spec. "
        "This deck may already have gone to the client, so a word you change is a discrepancy they can "
        "see. The only things you may change are punctuation the house style forbids (turn em dashes "
        "into commas or colons) and layout: which house page type carries which page.",
        "ONE PAGE IN, ONE PAGE OUT, in the same order. Choose the page type that fits what the source "
        "page does: a cover with fact rows is covermeta; parallel columns of formats or workstreams is "
        "strip; an argument beside a diagram is split; a room or location beside its photograph is "
        "photo; a timeline is phases; counted deliverables beside the money is schedule; numbered "
        "questions and answers is qa; the closing why-us and next-steps page is closing.",
        "IMAGES: reference a photograph only by the exact label you are given below, e.g. \"IMAGE 3\". "
        "Never invent a filename or a path, and never move a photograph to a page it did not come from.",
        "SPEC:\n" + _REBRAND_SCHEMA,
    ]))
    return provider.think_json(
        system,
        f"PHOTOGRAPHS AVAILABLE:\n{imglist}\n\n"
        + (f"INSTRUCTIONS FROM RASHAD:\n{notes}\n\n" if notes else "")
        + f"THE SOURCE DECK, page by page:\n{source_text}",
        model="claude-fable-5", max_tokens=16000, purpose="deck-rebrand",
        company=company.get("slug"))


def rebrand(company_slug: str, source_pdf: str, *, notes: str = "", out_dir: str = "/tmp",
            filename: str = "rebranded.pdf") -> dict:
    """Re-lay an existing deck PDF into the house format, keeping its content. Photographs are lifted
    out of the source, so the rebrand carries the same imagery rather than generating new."""
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    text = subprocess.run(["pdftotext", "-layout", source_pdf, "-"],
                          capture_output=True, text=True, timeout=120).stdout
    if not (text or "").strip():
        raise ValueError("that PDF has no extractable text, so it cannot be rebranded without "
                         "retyping it. Send the content instead.")
    imgdir = tempfile.mkdtemp(prefix="rebrand-")
    subprocess.run(["pdfimages", "-j", "-p", source_pdf, os.path.join(imgdir, "img")],
                   capture_output=True, timeout=180)
    images = []
    for fn in sorted(os.listdir(imgdir)):
        parts = fn.split("-")                       # img-<page>-<n>.<ext>
        if len(parts) >= 3 and parts[1].isdigit():
            images.append((int(parts[1]), os.path.join(imgdir, fn)))
    spec = rebrand_spec(co, text, images, notes) or {}
    by_label = {f"IMAGE {i + 1}": path for i, (_, path) in enumerate(images)}
    used = set()
    for pg in (spec.get("pages") or []):
        ref = pg.get("image")
        if isinstance(ref, str) and ref.strip():
            path = by_label.get(ref.strip().upper())
            pg["image"] = path                      # an unknown label becomes no image, never a guess
            if path:
                used.add(path)
    out = render_spec(company_slug, spec, out_dir=out_dir, filename=filename)
    return {**out, "images_found": len(images), "images_placed": len(used),
            "source_chars": len(text), "spec": spec}
