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
.thumbcap { font-size:12.5px; margin-top:8px; color:#C4C4CC; }
.thumbcap b { color:#EDEDF2; font-weight:600; }
""".replace("ACCENT", accent)


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=False)


class _Deck:
    def __init__(self, company: dict, customer: str, accent: str, logo: str, label: str):
        self.co, self.customer, self.accent, self.logo = company, customer, accent, logo
        self.label, self.pages = label, []

    def _foot(self, section: str) -> str:
        n = len(self.pages) + 1
        lg = f'<img class="logo" src="{self.logo}">' if self.logo else \
             f'<span style="letter-spacing:1.5px">{_esc(self.co.get("name"))}</span>'
        return (f'<div class="foot">{lg}<span>{_esc(section)}</span>'
                f'<span>{_esc(self.label)} &middot; {n:02d}</span></div>')

    def cover(self, title: str, standfirst: str, image: str | None):
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
            f'{self._foot("Proposal")}</div>')

    def cards(self, kicker: str, heading: str, cards: list, bullets: list | None = None, section: str = ""):
        c = "".join(f'<div class="card"><b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                    for x in cards[:3])
        b = ""
        if bullets:
            b = ('<ul class="klist" style="margin-top:26px;max-width:1040px">'
                 + "".join(f"<li>{_esc(x)}</li>" for x in bullets[:5]) + "</ul>")
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2><div class="cols" style="margin-top:4px">{c}</div>{b}</div>'
            f'{self._foot(section or kicker)}</div>')

    def phases(self, kicker: str, heading: str, phases: list, cards: list | None = None, section: str = ""):
        p = "".join(f'<div class="phase"><div class="num">{_esc(x.get("when"))}</div>'
                    f'<b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>' for x in phases[:4])
        c = ""
        if cards:
            c = ('<div class="cols" style="margin-top:26px">'
                 + "".join(f'<div class="card"><b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>'
                           for x in cards[:3]) + "</div>")
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2><div class="cols" style="margin-top:4px">{p}</div>{c}</div>'
            f'{self._foot(section or kicker)}</div>')

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
                f'<p class="thumbcap"><b>{_esc(f.get("label") or f.get("title"))}</b> &mdash; '
                f'{_esc(f.get("caption"))} Click to watch.</p></a>')
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
