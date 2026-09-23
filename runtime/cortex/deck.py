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
    if not cats:                                 # nothing asked for: the owner's best-rated work stands in
        return db.query(
            "select youtube_video_id, title, rating, duration, categories, client from media_assets "
            "where company_id=%s and status='live' and rating is not null "
            "order by rating desc, suggested_rating desc nulls last limit %s", (company_id, limit))
    # RELEVANCE BEFORE RATING (owner, 17 Sep 2026). The old "intersection, then widen to ANY category"
    # meant a writer asking for event-coverage + interviews + corporate + government-projects got the three
    # top-rated films in the library (HBMSU, SEHA, Dubai Police) captioned as event coverage, because the
    # intersection was empty and the widening ranked by rating alone. Films are now scored by how many of
    # the asked categories they carry, the FIRST categories counting most, then rating; a film that
    # matches nothing never appears while any film matches something.
    anyw = " or ".join(["categories @> %s::jsonb"] * len(cats))
    params = [f'["{c}"]' for c in cats]
    rows = db.query(
        "select youtube_video_id, title, rating, duration, categories, client from media_assets "
        f"where company_id=%s and status='live' and ({anyw})", (company_id, *params))
    weights = {c: (len(cats) - i) for i, c in enumerate(cats)}        # first category asked = heaviest

    def score(r):
        have = {str(x).lower() for x in (r.get("categories") or [])}
        return (sum(weights[c] for c in cats if c in have), r.get("rating") or 0, r.get("suggested_rating") or 0)
    out = sorted(rows, key=score, reverse=True)
    if not out:   # the slugs matched nothing (a writer's guess): the owner's best-rated work rather than none
        out = pick_samples(company_id, [], limit)
    return out[:limit]


_CAT_LABELS = {"event-coverage": "event coverage", "interviews": "interviews", "corporate": "corporate film",
               "government-projects": "government", "2d-animation": "2D animation", "aerial": "aerial",
               "products": "product film", "real-estate": "real estate", "f-and-b": "food and drink",
               "social-cut": "social cut", "bts": "behind the scenes", "ai": "AI production",
               "commercial": "commercial", "automotive": "automotive", "lifestyle": "lifestyle",
               "fashion-and-beauty": "fashion and beauty", "healthcare": "healthcare", "showreel": "showreel",
               "construction": "construction", "technology": "technology", "education": "education",
               "documentary": "documentary", "music-video": "music video", "presenter": "presenter-led",
               "event-promotion": "event promotion", "testimonial": "testimonial", "apps": "app"}


def films_in_text(company_id: int, text: str, limit: int | None = None) -> list[str]:
    """YouTube ids of library films the owner NAMES in his own words (a client name, a title, or an id),
    matched by code against media_assets. This is how a spoken suggestion reaches a deck, on the first
    request and on every reply: the deck writer itself is barred from naming films (owner, 17 Sep 2026)."""
    said = re.sub(r"\s+", " ", (text or "").lower())
    if len(said) < 4:
        return []
    out = []
    rows = db.query("select youtube_video_id, title, client, rating, categories from media_assets where "
                    "company_id=%s and status='live' order by rating desc nulls last", (company_id,))
    rows = [r for r in rows if "internal-test" not in [str(c) for c in (r.get("categories") or [])]]
    for r in rows:                                   # an id pasted from the library or a watch link
        if r["youtube_video_id"] and r["youtube_video_id"] in (text or "") and r["youtube_video_id"] not in out:
            out.append(r["youtube_video_id"])
    # ONE FILM PER NAME HE SAID, in the order he said them: a client with many films (Dubai Police, 35 of
    # them, all rated high) must never crowd out a lower-rated client he also named (China Innovation Center,
    # rated 4). For each named client the exact film wins when he said its title, else its best-rated film.
    names = {}
    for r in rows:
        cl = re.sub(r"\s+", " ", (r.get("client") or "").strip().lower())
        head = " ".join(cl.split()[:2])              # "china innovation" for "China Innovation Center"
        if len(cl) >= 5 and (cl in said or (len(cl.split()) >= 2 and len(head) >= 10 and head in said)):
            names.setdefault(cl, []).append(r)
    for cl in sorted(names, key=lambda c: said.find(c)):
        films = names[cl]
        exact = next((r for r in films if "," in (r.get("title") or "")
                      and len((r["title"].split(",", 1)[1].strip().lower()).split()) >= 2
                      and r["title"].split(",", 1)[1].strip().lower() in said), None)
        pick = exact or films[0]
        if pick["youtube_video_id"] not in out:
            out.append(pick["youtube_video_id"])
    return out if limit is None else out[:limit]


def playlist_for(company_id: int, category: str) -> dict | None:
    """The company's YouTube playlist for a library category ({title, url, category}), from media_playlists."""
    if not category:
        return None
    r = db.one("select title, playlist_id, category from media_playlists where company_id=%s and category=%s",
               (company_id, str(category).lower()))
    if not r or not r.get("playlist_id"):
        return None
    return {"title": r.get("title") or category, "category": r["category"],
            "url": f"https://www.youtube.com/playlist?list={r['playlist_id']}"}


def playlists(company_id: int, categories: list | None = None) -> list[dict]:
    """All of a company's category playlists, or those for the given categories, as {title, url, category}."""
    if categories:
        return [p for p in (playlist_for(company_id, c) for c in categories) if p]
    return [{"title": r["title"] or r["category"], "category": r["category"],
             "url": f"https://www.youtube.com/playlist?list={r['playlist_id']}"}
            for r in db.query("select title, playlist_id, category from media_playlists where company_id=%s "
                              "and coalesce(playlist_id,'')<>'' order by title", (company_id,))]


_DRIVE_FOLDER_RX = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})")


def drive_folder_in_text(text: str) -> str | None:
    """The first Google Drive FOLDER link in the owner's words, as its id."""
    m = _DRIVE_FOLDER_RX.search(text or "")
    return m.group(1) if m else None


_PHOTO_NAME_RX = re.compile(r"\b([A-Za-z]{2,4}[_-]?\d{4,6})(?:\.jpe?g|\.png)?\b", re.I)


def photo_names_in_text(text: str) -> list[str]:
    """Camera file names the owner typed ('MHP01150, MHT00028'), matched later against the folder."""
    return [m.group(1).upper() for m in _PHOTO_NAME_RX.finditer(text or "")]


def fetch_drive_photos(folder_id: str, out_dir: str, limit: int = 6, max_side: int = 1400,
                       names: list | None = None) -> dict:
    """Photographs from a Drive folder the owner pointed at. `names` = the frames he chose, by file name
    (in his order); otherwise spread evenly across the folder so six of twenty-three are not the first
    six. Downsized for the deck. Returns {name, images: [paths], total, missing}. Read with Cortex's own
    Drive access; a folder it cannot open returns no images and says so."""
    import io as _io
    from PIL import Image
    from . import drive
    tok = drive.access_token()
    import httpx as _hx
    meta = _hx.get(f"{drive.API}/files/{folder_id}", params={"fields": "id,name", "supportsAllDrives": "true"},
                   headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    name = meta.json().get("name") if meta.status_code == 200 else folder_id
    files = [f for f in drive.list_folder(folder_id, token=tok)
             if str(f.get("mimeType") or "").startswith("image/")]
    files.sort(key=lambda f: f.get("name") or "")
    if not files:
        return {"name": name, "images": [], "total": 0, "missing": list(names or [])}
    missing = []
    if names:
        by_stem = {re.sub(r"\.[a-z0-9]+$", "", f["name"], flags=re.I).upper(): f for f in files}
        picks = []
        for n in names:
            f = by_stem.get(n.upper())
            (picks.append(f) if f and f not in picks else missing.append(n))
        picks = picks[:limit]
    else:
        step = max(1, len(files) // limit)
        picks = files[::step][:limit]
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for f in picks:
        try:
            im = Image.open(_io.BytesIO(drive.download(f["id"], token=tok))).convert("RGB")
            im.thumbnail((max_side, max_side))
            p = os.path.join(out_dir, f"photo-{f['id'][:10]}.jpg")
            im.save(p, "JPEG", quality=86)
            paths.append(p)
        except Exception:  # noqa: BLE001
            continue
    return {"name": name, "images": paths, "total": len(files), "missing": missing}


def film_caption(f: dict) -> str:
    """A caption STAMPED from the film's own record (its categories and client), never written blind by the
    deck writer: card 725 captioned a hospital film 'multi-day event coverage' because the writer wrote
    four captions before any film was picked (17 Sep 2026)."""
    cats = [str(c).lower() for c in (f.get("categories") or [])
            if str(c).lower() not in ("version-variant", "internal-test", "dubai-police")]
    words = [_CAT_LABELS.get(c, c.replace("-", " ")) for c in cats[:3]]
    what = ", ".join(words).capitalize() if words else "Film"
    return f"{what} for {f['client']}" if f.get("client") else what


def our_work_page(company: dict, creative: bool = False) -> bool:
    """Whether a proposal deck carries an 'Our work' sample-films page. CREATIVE proposals never do (owner,
    17 Sep 2026: the company profile shows our work; the deck is the idea). Words-led proposals do, unless a
    standing rule on the company's sales-proposal or sales-quotation skill starts 'NO OUR WORK PAGE'. The
    rule is the switch, editable in Talk; code only reads it."""
    if creative:
        return False
    try:
        for key in ("sales-proposal", "sales-quotation"):
            sk = store.get_skill_by_key(company["id"], key)
            if not sk:
                continue
            uni, loc = store.effective_rules(sk)
            if any(re.match(r"\s*no our work page\b", str(r), re.I) for r in list(uni) + list(loc)):
                return False
    except Exception:  # noqa: BLE001
        pass
    return True


def library_slugs(company_id: int, limit: int = 40) -> list[str]:
    """The category slugs the media library actually uses, most-used first, so the writer picks from the
    real list (SEF'27 asked for 'hero-film' and 'brand-film', which exist nowhere, and got no films)."""
    return [r["c"] for r in db.query(
        "select c, count(*) n from media_assets, jsonb_array_elements_text(categories) c "
        "where company_id=%s and status='live' group by c order by n desc limit %s", (company_id, limit))]


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
table.t td.r, table.t th.r { text-align:right; white-space:nowrap; }
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
            f'<b>{_esc(x.get("title"))}</b><p>{_esc(x.get("body"))}</p></div>' for i, x in enumerate(phases[:5]))
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

    def samples(self, kicker: str, heading: str, intro: str, films: list, section: str = "",
                playlist: dict | None = None):
        """Films shown at native 16:9, fixed width so the page never overflows. `playlist` = {title, url}: a
        clickable line to the category's full YouTube playlist (owner, 17 Sep 2026: "we've covered over 50
        events, link the playlist"); code supplies it from media_playlists, never the writer."""
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
            + f'<div style="display:flex;gap:24px;margin-top:6px">{"".join(items)}</div>'
            + (f'<p style="margin-top:22px"><a href="{_esc(playlist["url"])}" style="color:{self.accent};'
               f'text-decoration:none;font-weight:600">See the full {_esc(playlist["title"])} playlist on YouTube '
               f'&rarr;</a> <span style="color:#7A7A84">{_esc(playlist["url"])}</span></p>' if playlist else "")
            + f'</div>{self._foot(section or "Our work")}</div>')

    def photos(self, kicker: str, heading: str, intro: str, images: list, section: str = ""):
        """A page of REAL photographs the owner pointed at (a Drive folder), six in a 3 x 2 grid at a fixed
        size so the page never overflows. Owner, 17 Sep 2026: "add some event photography to the proposal"."""
        imgs = [p for p in (images or []) if p and os.path.isfile(p)][:6]
        if not imgs:
            return
        w, h = 352, 198     # two rows end well above the footer (the logo overlapped the bottom-left frame at 234)
        cells = "".join(
            f'<img src="{_b64(p)}" style="width:{w}px;height:{h}px;object-fit:cover;border-radius:8px;display:block">'
            for p in imgs)
        self.pages.append(
            f'<div class="pg"><div class="pad"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2>{_esc(heading)}</h2>'
            + (f'<p style="max-width:1060px;margin-bottom:14px">{_esc(intro)}</p>' if intro else "")
            + f'<div style="display:grid;grid-template-columns:repeat(3,{w}px);gap:16px 24px;margin-top:4px">{cells}</div>'
            f'</div>{self._foot(section or "Photography")}</div>')

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


# --------------------------------------------------------------------------- creative proposal pages

_CREATIVE_CSS = """
.bgimg { position:absolute; top:0; left:0; width:1280px; height:720px; object-fit:cover; opacity:.85; }
.bgshade { position:absolute; top:0; left:0; width:1280px; height:720px;
           background:linear-gradient(to bottom, rgba(10,10,10,.62), rgba(10,10,10,.70) 55%, rgba(10,10,10,.86)); }
.onbg .pad { position:relative; z-index:2; }
.onbg .card, .onbg .gcell, .onbg .scol { background:rgba(14,15,18,.80); border-color:rgba(255,255,255,.08); }
.onbg .phase { background:rgba(14,15,18,.80); border-top:2px solid ACCENT; border-radius:0 0 8px 8px; padding:12px 14px 14px; }
.onbg table.t { background:rgba(14,15,18,.72); }
.full { position:absolute; top:0; left:0; width:1280px; height:720px; object-fit:cover; }
.fshade { position:absolute; top:0; left:0; width:1280px; height:720px;
          background:linear-gradient(to top, rgba(10,10,10,.92) 6%, rgba(10,10,10,.35) 38%, rgba(10,10,10,0) 62%); }
.hshade { position:absolute; top:0; left:0; width:1280px; height:720px;
          background:linear-gradient(to right, rgba(10,10,10,.94) 38%, rgba(10,10,10,.55) 58%, rgba(10,10,10,.08) 82%); }
.bk { position:absolute; top:44px; left:60px; font-size:11.5px; letter-spacing:2.5px; color:ACCENT; font-weight:600;
      text-transform:uppercase; background:rgba(10,10,10,.66); padding:7px 12px; border-radius:4px; }
.bk span { color:#C4C4CC; margin-left:14px; }
.ost { position:absolute; left:72px; right:120px; bottom:112px; font-family:Poppins,sans-serif; font-size:44px;
       font-weight:600; color:#FFFFFF; line-height:1.15; letter-spacing:-.3px; }
.ost.none { font-size:13px; letter-spacing:2.5px; color:#9A9AA4; font-weight:500; }
.obar { position:absolute; left:72px; bottom:96px; width:44px; height:3px; background:ACCENT; }
.cap { position:absolute; left:72px; bottom:58px; right:320px; font-size:14px; color:#C4C4CC; }
.bstack { position:absolute; left:72px; right:150px; bottom:106px; }
.vol { font-family:Poppins,sans-serif; font-size:31px; font-weight:500; font-style:italic; color:#FFFFFF;
       line-height:1.3; letter-spacing:-.2px; text-shadow:0 2px 18px rgba(0,0,0,.75); }
.ostchip { display:inline-block; margin-bottom:14px; background:rgba(10,10,10,.66); border-left:2px solid ACCENT;
           padding:5px 11px; border-radius:0 3px 3px 0; font-size:12.5px; color:#EDEDF2; }
.ostchip span { font-size:9px; letter-spacing:2.2px; color:ACCENT; font-weight:600; text-transform:uppercase;
                margin-right:9px; }
.vos { margin-top:2px; }
.vos div { display:flex; gap:18px; padding:6px 0; border-bottom:1px solid #1E1E22; }
.vos div:last-child { border-bottom:none; }
.vos .t { flex:0 0 92px; font-size:10.5px; color:ACCENT; letter-spacing:1.4px; font-weight:600; padding-top:3px; }
.vos .l { flex:1; font-size:14px; line-height:1.4; color:#EDEDF2; font-style:italic; }
.vos .l.none { color:#7A7A84; font-style:normal; font-size:11.5px; letter-spacing:1.6px; text-transform:uppercase; }
.hero h1 { font-size:52px; }
.hstats { display:flex; gap:34px; margin-top:26px; }
.hstats div { border-left:2px solid ACCENT; padding-left:12px; }
.cs { display:grid; grid-template-columns:repeat(5,1fr); gap:14px 14px; margin-top:6px; }
.cs img { width:100%; height:122px; object-fit:cover; border-radius:6px; display:block; }
.csnote { font-size:11px; color:#7A7A84; margin:-6px 0 10px; }
.cs .t { font-size:10px; color:ACCENT; letter-spacing:1.5px; font-weight:600; margin-top:5px; }
.cs .o { font-size:11.5px; color:#EDEDF2; line-height:1.3; }
.gcell.hasimg .cimg { height:210px; }
.tiles { display:flex; gap:14px; margin-top:18px; }
.tiles div { width:150px; }
.tiles img { width:150px; height:267px; object-fit:cover; border-radius:10px; display:block; }
.tiles .t { font-size:11px; color:ACCENT; letter-spacing:1.5px; font-weight:600; margin-top:7px; text-transform:uppercase; }
"""
_COMPACT_CSS = """
.pad { padding:44px 64px; } h2 { font-size:23px; margin-bottom:10px; }
p, li, td, th { font-size:12.5px; line-height:1.45; }
.card p, .gcell p, .scol p, .phase p { font-size:11.5px; }
table.t td, table.t th { padding:6px 10px; font-size:12.5px; }
"""


class CreativeDeck(_Deck):
    """The CREATIVE DECK STANDARD (owner, locked 12 Sep 2026: "people don't want to read, they want to see"):
    a full-bleed page per beat with its on-screen line set on the picture, a hero page for the idea, a contact
    sheet, contributor tiles, and every text page on one of the project's own frames. Built by creative.py;
    first used by hand on Massar (12 Sep) and SEF'27 (15 Sep). `compact` tightens type when a page overflows."""

    def __init__(self, *a, compact: bool = False, **k):
        super().__init__(*a, **k)
        self.compact = compact

    def with_bg(self, image):
        """Put a frame BEHIND the page just added, darkened so its words stay readable."""
        if not image or not self.pages:
            return
        bg = f'<img class="bgimg" src="{_b64(image)}"><div class="bgshade"></div>'
        self.pages[-1] = self.pages[-1].replace('<div class="pg">', '<div class="pg onbg">' + bg, 1)

    def hero(self, kicker, title, sub, line, stats, image, section=""):
        st = "".join(f'<div><div class="pk">{_esc(s.get("k"))}</div><div class="pv">{_esc(s.get("v"))}</div></div>'
                     for s in (stats or [])[:4])
        img = f'<img class="full" src="{_b64(image)}">' if image else ""
        self.pages.append(
            f'<div class="pg hero">{img}<div class="hshade"></div>'
            f'<div style="position:absolute;top:0;left:0;width:1280px;height:6px;background:{self.accent}"></div>'
            f'<div style="position:absolute;left:72px;top:170px;width:540px"><h3>{_esc(kicker)}</h3>'
            f'<div class="rule"></div><h1>{_esc(title)}</h1><div class="psub" style="margin-top:10px">{_esc(sub)}</div>'
            f'<p style="margin-top:16px;font-size:17px;color:#DADAE0">{_esc(line)}</p>'
            f'<div class="hstats">{st}</div></div>{self._foot(section or kicker)}</div>')

    def beat(self, film_label, n, total, time, text, cap, image, section="", vo=""):
        """`text` is the ON-SCREEN line (typography added in post); `vo` is the NARRATION heard over the beat.
        Two different things, never shown in the same place.

        THE NARRATION IS THE BIG TYPE (owner, 23 Sep 2026). When a beat is spoken, its line is set large over the
        picture so the deck reads the way the film plays: you see the frame and hear the words at the same time.
        The on-screen line then rides above it as a small labelled chip, because it is the film's own typography
        and not the narration. A beat with no narration keeps the old behaviour, its on-screen line set large,
        so a deliberately wordless film still reads as one."""
        img = f'<img class="full" src="{_b64(image)}">' if image else ""
        if vo:
            o = (f'<div class="ostchip"><span>On screen</span>{_esc(text)}</div>' if text else "")
            # one absolutely-positioned stack, so the chip sits on top of the narration without guessing its height
            body = f'<div class="bstack">{o}<div class="vol">{_esc(vo)}</div></div>'
        else:
            body = (f'<div class="ost">{_esc(text)}</div>' if text
                    else '<div class="ost none">PICTURE ONLY, NO TEXT</div>')
        self.pages.append(
            f'<div class="pg">{img}<div class="fshade"></div><div class="bk">{_esc(film_label)} · Beat {n} of {total}'
            f'<span>{_esc(time)}</span></div>{body}<div class="obar"></div><div class="cap">{_esc(cap)}</div>'
            f'{self._foot(section)}</div>')

    def voscript(self, kicker, heading, sub, rows, section="", note=""):
        """The narration end to end, timecode beside line, so the client reads the whole voice over in one place.
        A beat with no narration is shown as such rather than omitted: the silence is part of the film.
        `sub` and `note` are FOOTNOTES and are clamped: a writer who echoes the direction back at length spilled
        this page onto a second one (SEN-2026-0024 v3), which breaks the page count and the footer numbering."""
        cut = lambda s, n: (s or "").strip()[:n].rstrip(" ,;:") if s else ""   # noqa: E731
        r = "".join(f'<div><div class="t">{_esc(x.get("time"))}</div>'
                    + (f'<div class="l">{_esc(x["line"])}</div>' if x.get("line")
                       else '<div class="l none">No narration</div>') + "</div>"
                    for x in (rows or [])[:12])
        sub, note = cut(sub, 150), cut(note, 150)
        self.pages.append(
            f'<div class="pg"><div class="pad" style="padding-top:40px"><h3>{_esc(kicker)}</h3>'
            f'<div class="rule" style="margin-bottom:9px"></div>'
            f'<h2 style="font-size:23px;margin-bottom:5px">{_esc(heading)}</h2>'
            + (f'<p class="csnote" style="margin:0 0 6px">{_esc(sub)}</p>' if sub else "")
            + f'<div class="vos">{r}</div>'
            + (f'<p class="csnote" style="margin-top:12px">{_esc(note)}</p>' if note else "")
            + f'</div>{self._foot(section or kicker)}</div>')

    def sheet(self, kicker, heading, cells, section="", note=""):
        c = "".join((f'<div><img src="{_b64(x["img"])}">' if x.get("img") else "<div>")
                    + f'<div class="t">{_esc(x.get("time"))}</div><div class="o">{_esc(x.get("text") or "Picture only")}</div></div>'
                    for x in (cells or [])[:12])
        self.pages.append(
            f'<div class="pg"><div class="pad" style="padding-top:44px"><h3>{_esc(kicker)}</h3>'
            f'<div class="rule" style="margin-bottom:10px"></div><h2 style="font-size:23px;margin-bottom:12px">'
            f'{_esc(heading)}</h2>' + (f'<p class="csnote">{_esc(note)}</p>' if note else "")
            + f'<div class="cs">{c}</div></div>{self._foot(section or kicker)}</div>')

    def tiles(self, kicker, heading, body, tiles, note="", section="", width: int = 150):
        h = int(width * 16 / 9)

        def one(x):     # an `href` makes the tile a link to its film; `width` sizes every tile (portrait 9:16)
            img = f'<img src="{_b64(x["img"])}" style="width:{width}px;height:{h}px">'
            if x.get("href"):
                img = f'<a href="{_esc(x["href"])}" style="display:block">{img}</a>'
            return f'<div style="width:{width}px">{img}<div class="t">{_esc(x.get("label"))}</div></div>'
        t = "".join(one(x) for x in (tiles or [])[:7])
        self.pages.append(
            f'<div class="pg"><div class="pad" style="padding-top:48px"><h3>{_esc(kicker)}</h3>'
            f'<div class="rule"></div><h2 style="margin-bottom:8px">{_esc(heading)}</h2>'
            f'<p style="max-width:1080px">{_esc(body)}</p><div class="tiles">{t}</div>'
            + (f'<p class="csnote" style="margin-top:16px">{_esc(note)}</p>' if note else "")
            + f'</div>{self._foot(section or kicker)}</div>')

    def films_grid(self, kicker, heading, films, section=""):
        """Up to six films, three across in two rows, each at native 16:9 and a link to its film."""
        w = 340
        h = int(w * 9 / 16)
        cells = []
        for f in (films or [])[:6]:
            img = (f'<img src="{_b64(f["img"])}" style="width:{w}px;height:{h}px;display:block;border-radius:8px;'
                   'object-fit:cover">')
            if f.get("href"):
                img = f'<a href="{_esc(f["href"])}" style="display:block">{img}</a>'
            cap = (f'<p class="thumbcap" style="margin-top:6px"><b>{_esc(f.get("label"))}</b>'
                   + (f' &middot; {_esc(f.get("caption"))}' if f.get("caption") else "") + "</p>")
            cells.append(f'<div style="width:{w}px">{img}{cap}</div>')
        self.pages.append(
            f'<div class="pg"><div class="pad" style="padding-top:44px"><h3>{_esc(kicker)}</h3><div class="rule"></div>'
            f'<h2 style="margin-bottom:14px">{_esc(heading)}</h2>'
            f'<div style="display:grid;grid-template-columns:repeat(3,{w}px);gap:18px 28px">{"".join(cells)}</div>'
            f'</div>{self._foot(section or "Our work")}</div>')

    def html(self) -> str:
        css = _css(self.accent) + _CREATIVE_CSS.replace("ACCENT", self.accent) + (_COMPACT_CSS if self.compact else "")
        return '<html><head><meta charset="utf-8"><style>' + css + "</style></head><body>" + "".join(self.pages) + "</body></html>"


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
        "MEDIA LIBRARY CATEGORY SLUGS (samples.categories may use ONLY these): "
        + ", ".join(library_slugs(company["id"])),
        "SPEC:\n" + _SPEC_SCHEMA,
    ]))
    return provider.think_json(
        system, f"Client: {customer}\n\nBrief:\n{brief}{money}\n\n{extra_facts}",
        model="claude-fable-5", max_tokens=8000, purpose="deck-spec", company=company.get("slug"))


def revise_spec(company: dict, customer: str, spec: dict, feedback: str, quotation: dict | None = None,
                extra_facts: str = "") -> dict:
    """The owner's feedback on a built deck, applied to its spec: the model changes what he asked for and
    keeps everything else word for word (a revision, never a fresh deck). Same hard rules as authoring:
    no invented price, date, statistic or film. Code renders the result (15 Sep 2026)."""
    skill = store.get_skill_by_key(company["id"], "sales-quotation") or \
        store.get_skill_by_key(company["id"], "sales-first-response")
    rules = worker._rules_block(skill) if skill else ""
    money = ""
    if quotation:
        money = (f"\nQUOTATION FACTS (use these EXACTLY, never alter a figure): total "
                 f"{quotation.get('currency', 'AED')} {quotation.get('net')} + VAT"
                 f"{', number ' + quotation['number'] if quotation.get('number') else ''}.")
    system = "\n\n".join(filter(None, [
        "You REVISE a proposal deck's JSON spec from the owner's feedback. Return ONLY the complete revised "
        "JSON spec in the same shape. Change exactly what the feedback asks for and keep every other field "
        "word for word; if the feedback names a page that does not exist, add it in the schema's shape. "
        "Keep the cover's image_subject unchanged unless the feedback is about the cover image.",
        worker._now_line(),
        worker._company_context(company),
        rules,
        "HARD RULES: never invent a price, a date, a statistic or a client name; every figure comes from the "
        "quotation facts you are given or the owner's own words in the feedback. Never name a sample film: "
        "you supply media-library CATEGORY SLUGS and captions and the system picks the films. Keep every "
        "card body under 45 words. No em dashes. Write plainly, no marketing flourish.",
        "MEDIA LIBRARY CATEGORY SLUGS (samples.categories may use ONLY these): "
        + ", ".join(library_slugs(company["id"])),
        "SPEC SHAPE:\n" + _SPEC_SCHEMA,
    ]))
    import json as _json
    user = (f"Client: {customer}\n\nCURRENT SPEC:\n{_json.dumps(spec, ensure_ascii=False)}\n\n"
            f"OWNER'S FEEDBACK:\n{feedback}{money}\n\n{extra_facts}")
    return provider.think_json(system, user, model="claude-fable-5", max_tokens=8000,
                               purpose="deck-revise", company=company.get("slug")) or spec


def investment_from_quotation(quotation: dict) -> dict:
    """The investment page's rows and headline, STAMPED from a real quotation: one row per priced line,
    the headline the net figure. A line the quotation left blank prints 'To be confirmed'."""
    cur = quotation.get("currency") or "AED"
    rows, net = [], 0.0
    for sec in (quotation.get("sections") or []):
        for it in sec.get("items", []):
            unit, qty = it.get("unit"), it.get("qty") or 1
            amt = None
            try:
                amt = float(unit) * float(qty) if unit not in (None, "") else None
            except (TypeError, ValueError):
                amt = None
            if amt is not None:
                net += amt
            rows.append({"item": str(sec.get("header") or "").split("·")[-1].strip().title() or "Item",
                         "detail": str(it.get("desc") or "")[:90],
                         "amount": f"{cur} {amt:,.0f}" if amt is not None else "To be confirmed"})
    head = f"{cur} {net:,.0f} + VAT" if net else "To be confirmed"
    return {"rows": rows[:10], "headline": head, "net": round(net, 2)}


def build(company_slug: str, customer: str, brief: str, *, quotation: dict | None = None,
          label: str | None = None, extra_facts: str = "", out_dir: str = "/tmp",
          filename: str = "proposal.pdf", films: list | None = None) -> dict:
    """Author + render a house-format proposal deck. `films` = YouTube ids the owner NAMED for the Our work
    page; they lead it, the rest is filled by category and rating. Returns {path, pages, films, spec, cover}."""
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    spec = author_spec(co, customer, brief, quotation, extra_facts) or {}
    if films:
        spec.setdefault("samples", {})["video_ids"] = [v for v in films if v]
    return render(co, customer, spec, label=label, out_dir=out_dir, filename=filename)


_URL_IN_COPY = re.compile(r"\s*\(?https?://\S+\)?")


def _no_urls(x, keep: tuple = ("video_ids", "images", "folder")):
    """Deck COPY never carries a link: the writer put 'https://www.youtube.com/@SensaProductions/playlists', an
    address that does not exist, into the Our work intro when asked to link the playlist (17 Sep 2026). Every
    link on a deck is placed by code from the library or the playlist table; URLs in text are removed."""
    if isinstance(x, dict):
        return {k: (v if k in keep else _no_urls(v, keep)) for k, v in x.items()}
    if isinstance(x, list):
        return [_no_urls(v, keep) for v in x]
    if isinstance(x, str):
        return re.sub(r"\s{2,}", " ", _URL_IN_COPY.sub("", x)).replace(" :", ":").strip()
    return x


def render(co: dict, customer: str, spec: dict, *, label: str | None = None, out_dir: str = "/tmp",
           filename: str = "proposal.pdf", cover_path: str | None = None) -> dict:
    """Render a spec to PDF. `cover_path` reuses an existing hero image (a revision keeps its cover unless
    the cover subject changed); otherwise one is generated. Returns {path, pages, films, spec, cover}."""
    spec = _no_urls(spec)
    company_slug = co.get("slug") or ""
    accent = (spec.get("accent") or _ACCENT_DEFAULT).strip()
    if not re.match(r"^#[0-9A-Fa-f]{6}$", accent):
        accent = _ACCENT_DEFAULT
    import datetime
    lbl = label or f"Prepared for {customer} · {datetime.date.today():%B %Y}"
    d = _Deck(co, customer, accent, _logo(co), lbl)

    cv = spec.get("cover") or {}
    img = cover_path if cover_path and os.path.isfile(cover_path) else None
    if not img:
        img = cover_image(cv.get("image_subject") or f"the world of {customer}",
                          cv.get("image_palette") or "deep charcoal with restrained accent light",
                          company_slug, out_dir)
        if img:   # keep the hero under the deck's own name, so the next deck's cover never overwrites it
            keep = os.path.join(out_dir, re.sub(r"\.pdf$", "", filename) + "-cover.jpg")
            try:
                os.replace(img, keep)
                img = keep
            except OSError:
                pass
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
    if sm and our_work_page(co):
        # films the owner NAMED lead the page (China Innovation Center for Rana's event coverage, rated 4 and
        # never picked by rating alone, 17 Sep 2026); the rest is filled by category and rating
        picked = films_by_ids(co["id"], sm.get("video_ids") or [])
        have = {f["youtube_video_id"] for f in picked}
        picked += [f for f in pick_samples(co["id"], sm.get("categories") or [], 3)
                   if f["youtube_video_id"] not in have][:max(0, 3 - len(picked))]
        for f in picked:
            films.append({**f, "thumb": thumbnail(f["youtube_video_id"], out_dir),
                          "label": f["title"], "caption": film_caption(f)})
        if films:
            # the page's lead category: the first asked category the lead film actually carries, else the
            # first asked; its playlist (if the company keeps one) is linked under the films
            asked = [str(c).lower() for c in (sm.get("categories") or [])]
            lead_cats = [str(c).lower() for c in (films[0].get("categories") or [])]
            lead = next((c for c in asked if c in lead_cats), None) or (asked[0] if asked else None)
            d.samples(sm.get("kicker", ""), sm.get("heading", ""), sm.get("intro", ""), films,
                      playlist=playlist_for(co["id"], lead) if lead else None)
    ph = spec.get("photos") or {}
    if ph.get("images"):   # real photographs from a folder the owner named (code-placed, never invented)
        d.photos(ph.get("kicker") or f"{len(d.pages):02d} - Photography", ph.get("heading") or "Our photography",
                 ph.get("intro") or "", ph["images"])

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
            "spec": spec, "accent": accent, "cover": img}


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
