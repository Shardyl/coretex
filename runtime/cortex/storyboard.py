"""Storyboards: the shot-by-shot document for ONE film (owner, 26 Oct 2026, Shama / Qabilah).

A storyboard is not a proposal deck. The CREATIVE DECK STANDARD gives every beat its own full-bleed page because
a pitch sells the feeling; a storyboard is what the client APPROVES before production, so it reads like a
production document: three frames to a page, each with its shot number, its rough timecode in the film, what
happens, the camera, the sound, and the voice over where it plays. A 9:16 crop guide sits on every frame when the
film is delivered vertical, so the client sees the vertical cut, not only the 16:9 frame the image was made at.
One document per film, so each film is approved (and revised) on its own.

Plumbing only, like deck.py: the frames are the team's own (supplied, never generated here); timecodes are
COMPUTED by code from the running time; the voice over line is printed verbatim and must already exist in the
owner's words or in a document on the deal (a deck the client has seen), never written by a model; the shot notes
(action, camera, sound) are written by the model from the frames under the live STORYBOARD rules on the
company's sales-proposal skill. Filed on the deal and the client's Drive folder, raised as a review card; a reply
on the card revises it as the next version. Nothing is sent.

CLI: python -m cortex.storyboard dry <deal_id> "<film>" <frames_dir> "<voice over>" [seconds]
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone

from psycopg.types.json import Json

from . import db, deck, documents, notifications, pipeline, provider, store

MODEL = "claude-fable-5"          # a creative stage (owner, 12 Sep 2026: Fable 5 for every creative stage)
ROOT = "/opt/coretex/creative/storyboards"
PER_PAGE = 3
_MEDIA_CAP = 5                    # images per model call (provider.media_blocks takes at most 8)


# --------------------------------------------------------------------------- timing (code, never a model)

def timecodes(n: int, duration: float, end_hold: float | None = None) -> list[tuple[int, int]]:
    """Rough in/out seconds per shot. The last shot (the product, the end card, the voice over) is held longer:
    `end_hold` seconds, default a sixth of the film (5s of 30); the rest share what is left evenly. Whole
    seconds, contiguous, the last ending exactly on the running time."""
    n, duration = max(1, int(n)), max(1, int(round(float(duration))))
    if n == 1:
        return [(0, duration)]
    hold = end_hold if end_hold is not None else round(duration / 6)
    hold = max(1, min(int(round(hold)), duration - (n - 1)))
    body = duration - hold
    cuts = [round(i * body / (n - 1)) for i in range(n)]       # n-1 even shots, then the hold
    cuts[-1] = body
    out = [(cuts[i], cuts[i + 1]) for i in range(n - 1)] + [(body, duration)]
    for i, (a, b) in enumerate(out):                           # never a zero-length shot after rounding
        if b <= a:
            raise ValueError(f"{n} shots do not fit in {duration} seconds")
    return out


def tc(sec: int) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


# --------------------------------------------------------------------------- the voice over is never invented

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (s or "").lower().replace("’", "'"))).strip()


def vo_source(vo: str, said: str, deal_id: int | None) -> str | None:
    """Where the voice over line comes from: the owner's own words this turn, else a document on the deal
    (the deck the client approved). None = nobody wrote it, so it is not printed."""
    v = _norm(vo)
    if not v:
        return None
    if v in _norm(said):
        return "the owner's words"
    for d in documents.for_deal(int(deal_id)) if deal_id else []:
        try:
            if v in _norm(documents.text_of(d)):
                return d.get("filename") or f"document #{d['id']}"
        except Exception:  # noqa: BLE001
            continue
    return None


# --------------------------------------------------------------------------- frames

def _jpg(src, out: str, max_side: int = 2000) -> str:
    from PIL import Image
    im = Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src).convert("RGB")
    im.thumbnail((max_side, max_side))
    im.save(out, "JPEG", quality=88)
    return out


def _order_key(p: str):
    m = re.findall(r"\d+", os.path.basename(p))
    return (int(m[-1]) if m else 10 ** 6, os.path.basename(p).lower())


def prepare_frames(out_dir: str, *, paths: list | None = None, folder: str | None = None,
                   images: list | None = None, drive_folder: str | None = None) -> list[str]:
    """The film's frames, in shot order, as JPEGs in the job folder. From box paths, a box folder (ordered by the
    number in the file name: Majlis_1 ... Majlis_9), data: URLs attached in Talk (in the order sent), or a Drive
    folder (sorted by name). Never generated."""
    os.makedirs(out_dir, exist_ok=True)
    src: list = []
    if folder:
        src = sorted((os.path.join(folder, f) for f in os.listdir(folder)
                      if re.search(r"\.(png|jpe?g|webp)$", f, re.I)), key=_order_key)
    src += list(paths or [])
    out = []
    for i, p in enumerate(src, 1):
        out.append(_jpg(p, os.path.join(out_dir, f"shot-{i:02d}.jpg")))
    for u in images or []:
        if isinstance(u, str) and u.startswith("data:image/") and ";base64," in u:
            out.append(_jpg(base64.b64decode(u.split(";base64,", 1)[1]),
                            os.path.join(out_dir, f"shot-{len(out) + 1:02d}.jpg")))
    if drive_folder and not out:
        got = deck.fetch_drive_photos(drive_folder, os.path.join(out_dir, "drive"), limit=24, max_side=2000)
        imgs = sorted(got.get("images") or [], key=_order_key)
        for p in imgs:
            out.append(_jpg(p, os.path.join(out_dir, f"shot-{len(out) + 1:02d}.jpg")))
    return out


def _thumb(path: str, w: int = 768) -> str:
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((w, w))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------- the words (model, under the rules)

FALLBACKS: list = []        # calls Fable declined and Sonnet answered, shown on the card (never a silent switch)


def _ask(sysmsg: str, user: str, *, keys: tuple, purpose: str, company: str | None, max_tokens: int = 6000,
         images: list | None = None) -> dict:
    """Fable 5 first (the creative-stage model). Fable can DECLINE a harmless request (stop_reason 'refusal', empty
    text: the Majlis framing lines, 26 Oct 2026, twice in a row), so an answer missing its keys is asked once more and
    then of Sonnet, the prose model, and the switch is recorded for the card."""
    for mdl in (MODEL, MODEL, provider.MODEL_FAST):
        got = provider.think_json(sysmsg, user, fast=False, model=mdl, max_tokens=max_tokens, purpose=purpose,
                                  company=company, images=images)
        if all(got.get(k) for k in keys):
            if mdl != MODEL:
                FALLBACKS.append(purpose)
            return got
        print(f"[storyboard] {purpose}: {mdl} returned no {keys}", flush=True)
    return {}


def _rules(co: dict) -> str:
    """The live STORYBOARD rules on the company's sales-proposal skill (behaviour lives in the skill)."""
    sk = store.get_skill_by_key(co["id"], "sales-proposal")
    rules = [r for r in ((sk or {}).get("rules") or []) if isinstance(r, str)]
    sb = [r for r in rules if "STORYBOARD" in r.upper()[:60]]
    keep = [r for r in rules if r.upper().startswith(("EVERY BEAT IS A MOMENT", "AN INTERNAL LABEL"))]
    return "\n".join(f"- {r}" for r in sb + keep)


_SHOT_SCHEMA = ('{"shots": [{"n": 1, "action": "what HAPPENS in this shot, present tense, at most 16 words", '
                '"camera": "the camera, at most 8 words (e.g. slow push in, locked off macro)", '
                '"sound": "what we hear, at most 8 words", '
                '"x": "where the main subject sits across the frame, 0 (left edge) to 100 (right edge), so the 9:16 '
                'vertical cut is reframed on it"}]}')


def write_notes(co: dict, film: str, frames: list[str], times: list[tuple[int, int]], *, vo: str = "",
                vo_from: int = 0, context: str = "", aspect: str = "9:16") -> dict:
    """Shot notes from the frames, plus the cover line, the cover note and the approval line. The model sees
    the frames; it never writes a timecode, a price, a date or the voice over (those are code or verbatim)."""
    rules = _rules(co)
    sysmsg = ("You are the DIRECTOR writing the storyboard of one short film for the client to approve before "
              "production. You are shown the frames in shot order. For each shot write what happens (a moment, "
              "not a view: something moves, is given, poured, lifted, revealed), the camera, and the sound. Write "
              "plainly in British English, no dashes, no hype, no film jargon a marketing manager would not know. "
              "Never describe text, logos or labels you cannot read; never name a person. The voice over is "
              "fixed and printed by code: do not rewrite it."
              + (f"\n\nSTANDING RULES:\n{rules}" if rules else ""))
    shots: list[dict] = []
    n = len(frames)
    for start in range(0, n, _MEDIA_CAP):
        chunk = list(range(start, min(n, start + _MEDIA_CAP)))
        lines = [f"FILM: {film}. Running time {times[-1][1]} seconds, delivered {aspect}. {n} shots."]
        if context:
            lines.append(f"CONTEXT (the approved idea for this film):\n{context[:3000]}")
        if vo:
            lines.append(f"VOICE OVER (verbatim, heard from shot {vo_from + 1}): \"{vo}\"")
        if shots:
            lines.append("SHOTS ALREADY WRITTEN:\n" + "\n".join(
                f"{s['n']}. {s.get('action')} / {s.get('camera')} / {s.get('sound')}" for s in shots))
        lines.append("WRITE SHOTS " + ", ".join(
            f"{i + 1} ({tc(times[i][0])} to {tc(times[i][1])})" for i in chunk)
            + f". The images follow in that order.\nJSON: {_SHOT_SCHEMA}")
        out = _ask(sysmsg, "\n\n".join(lines), keys=("shots",), purpose="storyboard-shots",
                   company=co.get("slug"), images=[_thumb(frames[i]) for i in chunk])
        got = {int(s.get("n") or 0): s for s in (out.get("shots") or []) if isinstance(s, dict)}
        for i in chunk:
            s = got.get(i + 1) or {}
            shots.append({"n": i + 1, "action": _clean(s.get("action")), "camera": _clean(s.get("camera")),
                          "sound": _clean(s.get("sound")), "x": _x(s.get("x"))})
    return {"shots": shots, **framing(co, film, shots, context)}


def framing(co: dict, film: str, shots: list[dict], context: str = "") -> dict:
    """The three short lines around the shots: the cover line, the cover footnote, the closing ask."""
    ask = (f"FILM: {film}.\n" + (f"CONTEXT:\n{context[:3000]}\n" if context else "")
           + "SHOTS:\n" + "\n".join(f"{s['n']}. {s['action']}" for s in shots)
           + '\n\nWrite three lines for the storyboard document. JSON with exactly these keys: '
             '{"line": "one sentence for the cover saying what the film is, at most 22 words", '
             '"note": "one short footnote for the cover: the frames show the look and the flow, the final film is '
             'made from them, at most 20 words", "approve": "one sentence for the last page asking for their notes '
             'on this storyboard by email, at most 24 words"}')
    sysmsg = ("You write the short framing lines of a film storyboard a client will approve. Plain British English, "
              "no dashes, no hype, never a date, price or name.\n\nSTANDING RULES:\n" + _rules(co))
    got = _ask(sysmsg, ask, keys=("line", "note", "approve"), purpose="storyboard-framing",
               company=co.get("slug"), max_tokens=4000)
    return {k: _clean(got.get(k)) for k in ("line", "note", "approve")}


def _x(v) -> int:
    """The subject's position across the frame, 0 to 100; the centre when the model gave nothing usable."""
    try:
        return max(0, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return 50


def _clean(s) -> str:
    s = re.sub(r"\s*[–—]\s*", ", ", str(s or "").strip())      # house rule: no dashes in copy
    return re.sub(r"\s+", " ", s)


# --------------------------------------------------------------------------- layout

_SB_CSS = """
.sbh { position:absolute; top:40px; left:72px; right:72px; }
.sbh h2 { font-size:26px; margin:2px 0 0; }
.sbh .span { position:absolute; right:0; top:22px; font-size:12px; color:#9A9AA4; letter-spacing:1.5px; }
.tl { position:absolute; top:122px; left:72px; right:72px; height:4px; background:#1C1D22; border-radius:2px; }
.tl i { position:absolute; top:0; height:4px; background:#3A3B42; border-right:2px solid #0A0A0A; }
.tl i.on { background:ACCENT; }
.row { position:absolute; top:152px; left:72px; right:72px; display:flex; gap:28px; }
.shot { width:360px; }
.fr { position:relative; width:360px; height:203px; border-radius:6px; overflow:hidden; background:#16171B; }
.fr img { width:360px; height:203px; object-fit:cover; display:block; }
.fr .sh { position:absolute; top:0; bottom:0; background:rgba(6,6,8,.52); }
.fr .ln { position:absolute; top:0; bottom:0; width:0; border-left:1px dashed rgba(255,255,255,.85); }
.fr .ar { position:absolute; top:6px; font-size:8.5px; letter-spacing:1.5px; color:#FFFFFF; font-weight:600;
          background:rgba(10,10,10,.6); padding:2px 5px; border-radius:2px; }
.sn { display:flex; justify-content:space-between; margin-top:12px; font-size:11px; letter-spacing:2px;
      color:ACCENT; font-weight:600; text-transform:uppercase; }
.sn span { color:#C4C4CC; letter-spacing:1px; }
.act { font-size:14px; line-height:1.45; color:#EDEDF2; margin-top:6px; font-weight:400; }
.sub { display:flex; gap:16px; margin-top:14px; }
.vc { flex:0 0 113px; }
.vc img { width:113px; height:200px; object-fit:cover; object-position:center; border-radius:6px; display:block; }
.vc div { font-size:8.5px; letter-spacing:1.5px; color:#7A7A84; font-weight:600; margin-top:5px; text-align:center; }
.tx { flex:1; min-width:0; }
.tx .sn { margin-top:0; }
.cimgr { position:absolute; top:0; right:0; width:740px; height:720px; object-fit:cover; }
.cfade { position:absolute; top:0; right:0; width:740px; height:720px;
         background:linear-gradient(to right, #0A0A0A 0%, rgba(10,10,10,.55) 18%, rgba(10,10,10,0) 42%); }
.kv { display:flex; gap:10px; margin-top:8px; font-size:12px; line-height:1.4; color:#B4B4BC; }
.kv b { flex:0 0 58px; font-size:9px; letter-spacing:1.8px; color:#7A7A84; font-weight:600; padding-top:2px;
        text-transform:uppercase; }
.kv.vo b { color:ACCENT; }
.kv.vo span { color:#FFFFFF; font-style:italic; font-family:Poppins,sans-serif; font-size:13px; }
.sbvo { position:absolute; left:72px; right:200px; bottom:190px; font-family:Poppins,sans-serif; font-size:40px;
        font-weight:500; font-style:italic; color:#FFFFFF; line-height:1.25; text-shadow:0 2px 18px rgba(0,0,0,.75); }
.sbk { position:absolute; left:72px; bottom:300px; }
.sbap { position:absolute; left:72px; bottom:70px; width:640px; background:rgba(14,15,18,.82);
        border-left:3px solid ACCENT; border-radius:0 8px 8px 0; padding:14px 18px; font-size:13.5px; color:#DADAE0; }
.sbap b { display:block; font-size:9.5px; letter-spacing:2px; color:ACCENT; font-weight:600; margin-bottom:4px;
          text-transform:uppercase; }
.cnote { position:absolute; left:72px; bottom:64px; font-size:11px; color:#8A8A94; max-width:620px; }
"""


class StoryboardDeck(deck._Deck):
    """Three shots to a page, timecoded, with a crop guide when the film is delivered vertical."""

    def __init__(self, *a, duration: int, aspect: str = "9:16", **k):
        super().__init__(*a, **k)
        self.duration, self.aspect = int(duration), aspect

    def cover(self, kicker, title, sub, line, stats, image, note="", section="Storyboard"):
        st = "".join(f'<div><div class="pk">{deck._esc(s["k"])}</div><div class="pv">{deck._esc(s["v"])}</div></div>'
                     for s in stats[:4])
        # the frame on the RIGHT, the words on black: a full-bleed cover hid the bottle behind the title (Majlis v1 dry run)
        img = f'<img class="cimgr" src="{deck._b64(image)}"><div class="cfade"></div>' if image else ""
        lg = (f'<img class="logobig" style="position:absolute;top:54px;left:72px;z-index:3" src="{self.logo}">'
              if self.logo else "")
        self.pages.append(
            f'<div class="pg hero">{img}'
            f'<div style="position:absolute;top:0;left:0;width:1280px;height:6px;background:{self.accent}"></div>{lg}'
            f'<div style="position:absolute;left:72px;top:190px;width:450px"><h3>{deck._esc(kicker)}</h3>'
            f'<div class="rule"></div><h1>{deck._esc(title)}</h1>'
            + (f'<div class="psub" style="margin-top:10px">{deck._esc(sub)}</div>' if sub else "")
            + (f'<p style="margin-top:16px;font-size:17px;color:#DADAE0">{deck._esc(line)}</p>' if line else "")
            + f'<div class="hstats">{st}</div></div>'
            + (f'<div class="cnote">{deck._esc(note)}</div>' if note else "")
            + f'{self._foot(section)}</div>')

    @staticmethod
    def _window(x_pct: int) -> tuple[int, int, float]:
        """The 9:16 window on a 360x203 frame, centred on the subject (x as 0-100), kept inside the frame.
        Returns (left, width, css object-position %). A centred window cut the bottle in half on Majlis shot 9."""
        w = round(203 * 9 / 16)
        left = max(0, min(360 - w, round(x_pct / 100 * 360 - w / 2)))
        return left, w, left / (360 - w) * 100

    def _crop(self, x_pct: int = 50) -> str:
        """The 9:16 window on a 16:9 frame, reframed on the subject: the outside darkened, its edges dashed."""
        if self.aspect != "9:16":
            return ""
        x, w, _ = self._window(x_pct)
        return (f'<div class="sh" style="left:0;width:{x}px"></div><div class="sh" style="left:{x + w}px;right:0"></div>'
                f'<div class="ln" style="left:{x}px"></div><div class="ln" style="left:{x + w}px"></div>'
                f'<div class="ar" style="left:{x + 4}px">9:16</div>')

    def shots(self, kicker, shots: list[dict], all_times: list[tuple[int, int]], section="Storyboard"):
        first, last = shots[0], shots[-1]
        heading = (f"Shot {first['n']}" if first is last else f"Shots {first['n']} to {last['n']}")
        bar = "".join(
            f'<i class="{"on" if any(s["n"] == i + 1 for s in shots) else ""}" '
            f'style="left:{a / self.duration * 100:.3f}%;width:{(b - a) / self.duration * 100:.3f}%"></i>'
            for i, (a, b) in enumerate(all_times))
        cells = []
        for s in shots:
            vo = (f'<div class="kv vo"><b>Voice over</b><span>{deck._esc(s["vo"])}</span></div>' if s.get("vo") else "")
            src = deck._b64(s["img"])
            text = (f'<div class="sn">Shot {s["n"]:02d}<span>{tc(s["t"][0])} to {tc(s["t"][1])}</span></div>'
                    f'<div class="act">{deck._esc(s.get("action"))}</div>'
                    f'<div class="kv"><b>Camera</b><span>{deck._esc(s.get("camera"))}</span></div>'
                    f'<div class="kv"><b>Sound</b><span>{deck._esc(s.get("sound"))}</span></div>{vo}')
            # a vertical film also shows the vertical cut itself beside the words: what the phone will show
            xp = _x(s.get("x", 50))
            body = (f'<div class="sub"><div class="vc"><img src="{src}" style="object-position:'
                    f'{self._window(xp)[2]:.1f}% 50%"><div>9:16 CUT</div></div>'
                    f'<div class="tx">{text}</div></div>') if self.aspect == "9:16" else text
            cells.append(f'<div class="shot"><div class="fr"><img src="{src}">{self._crop(xp)}</div>{body}</div>')
        self.pages.append(
            f'<div class="pg"><div class="sbh"><h3>{deck._esc(kicker)}</h3><h2>{heading}</h2>'
            f'<div class="span">{tc(first["t"][0])} to {tc(last["t"][1])} of {tc(self.duration)}</div></div>'
            f'<div class="tl">{bar}</div><div class="row">{"".join(cells)}</div>{self._foot(section)}</div>')

    def closing(self, vo: str, vo_t: tuple[int, int], image, approve: str, section="Storyboard"):
        img = f'<img class="full" src="{deck._b64(image)}">' if image else ""
        k = f"The voice over &middot; {tc(vo_t[0])} to {tc(vo_t[1])}" if vo else "Over to you"
        self.pages.append(
            f'<div class="pg">{img}<div class="fshade" style="background:linear-gradient(to top, rgba(10,10,10,.94) '
            f'18%, rgba(10,10,10,.55) 55%, rgba(10,10,10,.25))"></div>'
            f'<div class="sbk"><h3>{k}</h3><div class="rule" style="margin:0"></div></div>'
            + (f'<div class="sbvo">&ldquo;{deck._esc(vo)}&rdquo;</div>' if vo else "")
            + (f'<div class="sbap"><b>Your notes</b>{deck._esc(approve)}</div>' if approve else "")
            + f'{self._foot(section)}</div>')

    def html(self) -> str:
        css = (deck._css(self.accent) + deck._CREATIVE_CSS.replace("ACCENT", self.accent)
               + _SB_CSS.replace("ACCENT", self.accent))
        return '<html><head><meta charset="utf-8"><style>' + css + "</style></head><body>" + "".join(self.pages) + "</body></html>"


# --------------------------------------------------------------------------- the job

class Job:
    def __init__(self, jid: str):
        self.id, self.dir = jid, os.path.join(ROOT, jid)
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, "state.json")
        self.s = json.load(open(p)) if os.path.exists(p) else {}

    def save(self):
        json.dump(self.s, open(os.path.join(self.dir, "state.json"), "w"), indent=1, default=str)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "film"


def _prefix(s: dict) -> str:
    return " - ".join(x for x in (s["customer"], s.get("project"), s["film"]) if x) + " - Storyboard"


def _next_version(s: dict) -> int:
    pre = _prefix(s)
    rows = db.query("select filename from company_documents where kind='storyboard' and filename like %s "
                    "and (deal_id=%s or deal_id is null)", (pre + " v%", s.get("deal_id")))
    vs = [int(m.group(1)) for r in rows for m in [re.search(r"Storyboard v(\d+)", r["filename"] or "")] if m]
    return max(vs, default=0) + 1


def render(job: Job) -> str:
    """Lay out the storyboard from the job state. No model call: every word is already in the state."""
    s = job.s
    co = store.get_company_by_slug(s["company"])
    accent = s.get("accent") or deck._ACCENT_DEFAULT
    frames, times = s["frames"], [tuple(t) for t in s["times"]]
    label = " · ".join(x for x in (s.get("project"), s["film"]) if x)
    d = StoryboardDeck(co, s["customer"], accent, deck._logo(co), label, duration=s["duration"], aspect=s["aspect"])
    n = len(frames)
    stats = [{"k": "Running time", "v": f"{s['duration']} seconds"},
             {"k": "Format", "v": "9:16 vertical" if s["aspect"] == "9:16" else s["aspect"]},
             {"k": "Shots", "v": str(n)}, {"k": "Version", "v": f"Storyboard v{s['version']}"}]
    cov = frames[min(max(int(s.get("cover", n) or n), 1), n) - 1]
    d.cover(s.get("kicker") or f"{s.get('project') or s['customer']} · Storyboard", s["film"], s.get("subtitle", ""),
            s["notes"].get("line", ""), stats, cov, note=s["notes"].get("note", ""))
    shots = []
    for i, f in enumerate(frames):
        sh = dict(s["notes"]["shots"][i]) if i < len(s["notes"]["shots"]) else {"n": i + 1}
        sh.update({"n": i + 1, "img": f, "t": times[i], "vo": s["vo"] if (s.get("vo") and i == s["vo_from"]) else ""})
        shots.append(sh)
    kick = f"{s['film']} · Storyboard"
    for p in range(0, n, PER_PAGE):
        d.shots(kick, shots[p:p + PER_PAGE], times)
    vo_t = (times[s["vo_from"]][0], s["duration"]) if s.get("vo") else (0, 0)
    d.closing(s.get("vo", ""), vo_t, frames[-1], s["notes"].get("approve", ""))
    s["name"] = f"{_prefix(s)} v{s['version']} - {datetime.now(timezone.utc):%Y-%m-%d}.pdf"
    path = os.path.join(job.dir, s["name"])
    deck.to_pdf(d.html(), path)
    s["path"], s["pages"] = path, len(d.pages)
    job.save()
    return path


def _file(job: Job) -> dict:
    """Library (kind storyboard, on the deal) + the client's Drive folder; this film's earlier versions retired."""
    from . import engine
    s = job.s
    co = store.get_company_by_slug(s["company"])
    old = [r["id"] for r in db.query(
        "select id from company_documents where kind='storyboard' and superseded_by is null and filename like %s "
        "and deal_id=%s", (_prefix(s) + " v%", s["deal_id"]))]
    doc, filed = engine._file_proposal_pdf(co, s["customer"], s["name"], s["path"], s["deal_id"], None,
                                           supersede=old, archive_prefix=_prefix(s), kind="storyboard")
    s["doc"] = {"id": doc["id"], "filename": doc["filename"], "mime": doc["mime"], "size": doc["size"]}
    s["folder"] = filed
    job.save()
    pipeline.log_deal(s["deal_id"], "storyboard", f"Storyboard v{s['version']} filed: {s['name']}"
                                                  + (f" (client folder {filed})" if filed else ""))
    return s["doc"]


def summary(job: Job) -> str:
    s = job.s
    lines = [f"Storyboard v{s['version']} for {s['film']}: {len(s['frames'])} shots over {s['duration']} seconds, "
             f"{s['pages']} pages, three frames a page" + (", 9:16 crop guide on every frame" if s["aspect"] == "9:16" else "")
             + "."]
    if s.get("vo"):
        lines.append(f"Voice over (verbatim, from {s.get('vo_src')}), from {tc(s['times'][s['vo_from']][0])}: \"{s['vo']}\"")
    lines.append("Shots:\n" + "\n".join(f"{x['n']}. {tc(t[0])}-{tc(t[1])} {x.get('action')}"
                                       for x, t in zip(s["notes"]["shots"], s["times"])))
    if s.get("fallbacks"):
        lines.append("Model note: Fable 5 declined " + ", ".join(s["fallbacks"]) + "; Sonnet wrote those lines.")
    lines.append("The PDF is attached" + (f" and filed in the {s['folder']} client folder" if s.get("folder") else "")
                 + ". Nothing has been sent. Reply on this card with any change (a shot's words, the order, a frame "
                   "to drop, the timing, the cover frame) and it is rebuilt as the next version.")
    return "\n\n".join(lines)


def build(company_slug: str, deal_id: int, film: str, *, vo: str = "", said: str = "", duration: int = 30,
          aspect: str = "9:16", project: str = "", subtitle: str = "", context: str = "", end_hold: int | None = None,
          cover: int | None = None, frames_dir: str | None = None, frame_paths: list | None = None,
          images: list | None = None, drive_folder: str | None = None, dry: bool = False,
          background: bool = False) -> dict:
    """One storyboard document for one film. Checks everything a person must supply BEFORE any model call, raises
    the review card (drafting), then writes, lays out and files (on a thread when `background`)."""
    from . import engine
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    deal = db.one("select id, title from crm_projects where id=%s", (int(deal_id),))
    if not deal:
        raise ValueError(f"no opportunity #{deal_id}")
    vo = re.sub(r"\s+", " ", (vo or "").strip().strip('"“”')).strip()
    src = vo_source(vo, said, int(deal_id)) if vo else None
    if vo and not src:
        raise ValueError(f"The voice over line \"{vo}\" is not in Rashad's words or in any document on the deal, so it "
                         "was not used. Ask him for the exact line (or point at the deck it comes from).")
    customer = (engine._deal_client_name(int(deal_id)) or co["name"]).split(",")[0].strip()
    jid = f"{deal_id}-{_slug(film)}-{datetime.now(timezone.utc):%Y%m%d%H%M}-{secrets.token_hex(2)}"
    job = Job(jid)
    frames = prepare_frames(os.path.join(job.dir, "frames"), paths=frame_paths, folder=frames_dir,
                            images=images, drive_folder=drive_folder)
    if not frames:
        raise ValueError("No frames: pass a folder of frames, the frames attached in Talk, or a Drive folder.")
    times = timecodes(len(frames), duration, end_hold)
    job.s.update({"company": company_slug, "deal_id": int(deal_id), "customer": customer, "project": project.strip(),
                  "film": film.strip(), "subtitle": subtitle.strip(), "context": context, "vo": vo, "vo_src": src,
                  "vo_from": len(frames) - 1, "duration": int(duration), "aspect": aspect, "frames": frames,
                  "times": times, "cover": cover or len(frames), "said": said})
    job.s["version"] = _next_version(job.s)
    job.save()
    task = None
    if not dry:
        skill = store.get_skill_by_key(co["id"], "sales-proposal")
        req = {"kind": "storyboard", "job": jid, "customer": customer, "deal_id": int(deal_id), "film": film}
        task = db.execute("insert into tasks (company_id, skill_id, kind, request, draft, status, title, deal_id) "
                          "values (%s,%s,'content',%s,%s,'drafting',%s,%s) returning *",
                          (co["id"], skill["id"], Json(req),
                           f"Building the storyboard for {film}: shot notes from the frames, then the layout. About "
                           "three minutes; the card returns to your Inbox when it is ready.",
                           f"Storyboard: {customer} - {film}", int(deal_id)))
    if background:
        threading.Thread(target=run, args=(jid, (task or {}).get("id"), dry), daemon=True).start()
    else:
        run(jid, (task or {}).get("id"), dry)
    return {"job": jid, "task_id": (task or {}).get("id"), "dir": job.dir, "path": job.s.get("path")}


def run(jid: str, task_id: int | None, dry: bool = False) -> None:
    job = Job(jid)
    task = store.get_task(task_id) if task_id else None
    try:
        co = store.get_company_by_slug(job.s["company"])
        if not job.s.get("notes"):
            FALLBACKS.clear()
            job.s["notes"] = write_notes(co, job.s["film"], job.s["frames"], [tuple(t) for t in job.s["times"]],
                                         vo=job.s.get("vo", ""), vo_from=job.s["vo_from"],
                                         context=job.s.get("context", ""), aspect=job.s["aspect"])
            job.s["fallbacks"] = sorted(set(FALLBACKS))
            job.save()
        render(job)
        if not dry:
            _file(job)
    except Exception as e:  # noqa: BLE001 — a visible card, never a silent failure
        if task:
            store.update_task(task["id"], status="awaiting_correction",
                              draft=f"The storyboard stopped: {type(e).__name__}: {e}. Reply 'resume' to try again.")
            notifications.notify(f"Card #{task['id']}: the storyboard stopped", str(e)[:300], category="approval",
                                 company_id=task.get("company_id"), target_type="task", target_id=str(task["id"]))
        raise
    if task:
        req = dict(task.get("request") or {})
        req.update({"title": job.s["name"], "file": job.s["path"], "version": job.s["version"],
                    "attach_docs": [job.s["doc"]] if job.s.get("doc") else []})
        store.update_task(task["id"], request=req, draft=summary(job), title=job.s["name"], status="awaiting_approval")


# --------------------------------------------------------------------------- revisions on the card

def revise(task: dict, text: str) -> bool:
    """A reply on a storyboard card: the words he asks to change change, the rest stays word for word; frames can
    be dropped or reordered but never invented; the voice over changes only to a line in his own words."""
    req = task.get("request") or {}
    if req.get("kind") != "storyboard" or not req.get("job"):
        return False
    job = Job(req["job"])
    s = job.s
    if not s.get("notes") or re.fullmatch(r"\s*(resume|retry|try again)\W*", text or "", re.I):
        store.update_task(task["id"], status="drafting")
        threading.Thread(target=run, args=(job.id, task["id"]), daemon=True).start()
        return True
    co = store.get_company_by_slug(s["company"])
    cur = {"shots": [{"frame": i + 1, **{k: x.get(k) for k in ("action", "camera", "sound", "x")}}
                     for i, x in enumerate(s["notes"]["shots"])],
           "line": s["notes"].get("line"), "note": s["notes"].get("note"), "approve": s["notes"].get("approve"),
           "cover": s.get("cover"), "duration": s["duration"], "end_hold": s["times"][-1][1] - s["times"][-1][0]}
    out = provider.think_json(
        "You revise a film storyboard from the owner's note. Change EXACTLY what the note asks and keep everything "
        "else word for word. To drop or reorder shots, list the frames in the new order by their frame number; never "
        "add a frame that is not listed. Write plainly, British English, no dashes.\n\nSTANDING RULES:\n" + _rules(co),
        f"CURRENT STORYBOARD:\n{json.dumps(cur, indent=1)}\n\nOWNER'S NOTE:\n{text}\n\nJSON: the same shape as the "
        'current storyboard (shots with frame, action, camera, sound; line; note; approve; cover; duration; end_hold), '
        'plus "vo": the new voice over line ONLY if the note gives one word for word, else "".',
        fast=False, model=MODEL, max_tokens=8000, purpose="storyboard-revise", company=s["company"])
    if not out.get("shots"):
        return False
    old_frames = list(s["frames"])
    new_frames, notes = [], []
    for x in out["shots"]:
        i = int(x.get("frame") or 0)
        if 1 <= i <= len(old_frames):
            new_frames.append(old_frames[i - 1])
            notes.append({"n": len(notes) + 1, "action": _clean(x.get("action")), "camera": _clean(x.get("camera")),
                          "sound": _clean(x.get("sound")), "x": _x(x.get("x", s["notes"]["shots"][i - 1].get("x")))})
    if not new_frames:
        return False
    nvo = re.sub(r"\s+", " ", str(out.get("vo") or "").strip().strip('"“”'))
    if nvo and _norm(nvo) in _norm(text):          # a new line only when he wrote it
        s["vo"], s["vo_src"] = nvo, "the owner's note on the card"
    dur = int(out.get("duration") or s["duration"]) if str(out.get("duration") or "").isdigit() else s["duration"]
    if dur != s["duration"] and not re.search(rf"\b{dur}\b", text or ""):
        dur = s["duration"]                         # a running time changes only to a figure he wrote
    hold = out.get("end_hold")
    s.update({"frames": new_frames, "duration": dur, "vo_from": len(new_frames) - 1,
              "times": timecodes(len(new_frames), dur, int(hold) if str(hold or "").isdigit() else None),
              "cover": min(int(out.get("cover") or s.get("cover") or len(new_frames)), len(new_frames)),
              "notes": {"shots": notes, "line": _clean(out.get("line") or s["notes"].get("line")),
                        "note": _clean(out.get("note") or s["notes"].get("note")),
                        "approve": _clean(out.get("approve") or s["notes"].get("approve"))}})
    s["version"] = _next_version(s)
    job.save()
    render(job)
    _file(job)
    rq = dict(req)
    rq.update({"title": s["name"], "file": s["path"], "version": s["version"], "attach_docs": [s["doc"]]})
    store.update_task(task["id"], request=rq, draft=summary(job), title=s["name"], status="awaiting_approval",
                      attempts=(task.get("attempts") or 0) + 1)
    return True


def approve(task: dict, actor: str) -> dict:
    """Approving a storyboard closes the card; sending it is a separate email the owner asks for."""
    rq = dict(task.get("request") or {})
    if not rq.get("attach_docs"):
        store.update_task(task["id"], status="awaiting_approval")
        return {"blocked": True, "error": "the storyboard has not finished building yet"}
    store.update_task(task["id"], status="done", request={**rq, "approved": True})
    if rq.get("deal_id"):
        pipeline.log_deal(int(rq["deal_id"]), "storyboard", f"Storyboard approved by {actor}: {rq.get('title')}")
    return {"ok": True, "storyboard": rq.get("title")}


if __name__ == "__main__":      # python -m cortex.storyboard dry <deal> "<film>" <frames_dir> "<vo>" [seconds]
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == "dry":
        r = build("sensa", int(a[1]), a[2], frames_dir=a[3], vo=a[4] if len(a) > 4 else "",
                  said=a[4] if len(a) > 4 else "", duration=int(a[5]) if len(a) > 5 else 30, dry=True)
        print(json.dumps(r, indent=1))
    else:
        print(__doc__)
