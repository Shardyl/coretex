"""Creative proposals: the concept, the storyboard and the money in ONE run, revised by replying on the card.

Owner, 15 Sep 2026: "I want Cortex to be able to do everything you did" on the SEF'27 / Sheraa pitch (deal 123),
and "one run with the ability to do revisions" rather than a card per stage. This module is that run:

  brief     the deal record (client documents, emails, call notes) + the owner's direction -> a structured brief
  location  web research on the places the brief names, one pick against the constraint, facts with sources;
            a LICENSED photograph for the deck (Wikimedia Commons, credit kept) and the venue's own photographs
            as PRIVATE references for the frames (never shown in the deck)
  concept   three routes, one chosen, the look, verbatim asset descriptors (Fable 5)
  script    beat by beat: timings checked by code, on-screen lines, frame prompts (Fable 5)
  frames    two candidates per beat from Gemini WITH the reference photographs, picked by Fable 5; a frame
            with legible text or a logo is rejected; image spend capped
  quotation blocks priced by CODE from the rate card (ratecard.price_lines); a typed figure survives only if
            the owner said it; exclusions stated; a new version when the deal already has a quote number
  deck      copy by Fable 5 under the CREATIVE DECK STANDARD, laid out by deck.CreativeDeck, investment read
            back from the quotation registry, invented dates removed, page count checked
  filing    library row on the deal + the client's Drive folder, earlier proposals retired, the deck attached
            to the quotation card, a timeline entry, one review card

Models are named here as visible data: Fable 5 for every creative stage (owner decision, 12 Sep 2026).
Nothing ever sends: the card is a review, and the email that sends it is a separate approval."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Json

from . import db, deck, documents, imagegen, notifications, pipeline, profile, provider, ratecard, store, worker

MODEL = "claude-fable-5"          # every creative stage (owner, 12 Sep 2026); never the CORTEX_MODEL tier pin
ROOT = "/opt/coretex/creative"
IMAGE_CAP = 40                    # images per run unless the profile sets creative_image_cap
UA = {"User-Agent": "SensaCortex/1.0 (hello@sensa.digital)"}
LICENCES = re.compile(r"^(cc0|cc by(-sa)?( \d\.\d)?|public domain|pd)", re.I)
_EM = re.compile(r"\s*[—–]\s*")
# a writer's claim about money ("net total is AED 27,800", "Budget tier", "discount"): code states the figures
_MONEY_CLAIM = re.compile(r"\b(total|net|AED|budget tier|discount|target|price[sd]? (?:down|at))\b|\d{1,3},\d{3}", re.I)


# --------------------------------------------------------------------------- small helpers

def _clean_text(x):
    """No em or en dashes anywhere in client copy (house rule); recurses through the JSON."""
    if isinstance(x, dict):
        return {k: _clean_text(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean_text(v) for v in x]
    return _EM.sub(", ", x) if isinstance(x, str) else x


def _thumb(path: str, w: int = 640) -> str:
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((w, w))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _save_jpg(data: bytes, path: str, max_side: int = 1600) -> tuple[int, int] | None:
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    im.thumbnail((max_side, max_side))
    im.save(path, "JPEG", quality=90)
    return im.size


def _secs(t: str) -> int:
    m, s = t.strip().split(":")
    return int(m) * 60 + int(s)


def _timing_problem(beats: list, duration: int) -> str:
    """Beat timings must be contiguous from 0:00 and end exactly on the film's running time."""
    try:
        spans = [tuple(_secs(x) for x in re.split(r"\s*-\s*", b["time"].replace("–", "-"))) for b in beats]
    except Exception:  # noqa: BLE001
        return "the beat timings could not be read"
    if not spans or spans[0][0] != 0:
        return "the first beat must start at 0:00"
    for i in range(1, len(spans)):
        if spans[i][0] != spans[i - 1][1]:
            return f"beat {i + 1} does not start where beat {i} ends"
    if spans[-1][1] != duration:
        return f"the beats end at {spans[-1][1]}s, the film is {duration}s"
    return ""


_MONTHS = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
_DATE = re.compile(rf"\b\d{{1,2}}(st|nd|rd|th)? {_MONTHS}\b|\b{_MONTHS} \d{{1,2}}\b|\b(Monday|Tuesday|Wednesday|"
                   r"Thursday|Friday|Saturday|Sunday)\b", re.I)


def _drop_invented_dates(x, source: str, dropped: list):
    """A date or weekday the source material does not contain is removed with its sentence (the Massar deck
    writer invented a kickoff date). Recurses through the copy JSON."""
    if isinstance(x, dict):
        return {k: _drop_invented_dates(v, source, dropped) for k, v in x.items()}
    if isinstance(x, list):
        return [_drop_invented_dates(v, source, dropped) for v in x]
    if not isinstance(x, str) or not _DATE.search(x):
        return x
    keep = []
    for sent in re.split(r"(?<=[.!?])\s+", x):
        m = _DATE.search(sent)
        if m and m.group(0).lower() not in source:
            dropped.append(sent)
            continue
        keep.append(sent)
    return " ".join(keep) if keep else _DATE.sub("", x).strip(" ,")


_MONEY = re.compile(r"\bAED\b|\bdirhams?\b|\bVAT\b|\bSEN-\d{4}-\d{4}\b", re.I)


def _strip_money(x, dropped: list):
    """Prices, totals, VAT and quotation numbers are printed by CODE from the quotation. The first dry run's
    writer copied 'AED 119,900' out of the deal record onto a deck whose own quote came to 124,850."""
    if isinstance(x, dict):
        return {k: _strip_money(v, dropped) for k, v in x.items()}
    if isinstance(x, list):
        return [_strip_money(v, dropped) for v in x]
    if not isinstance(x, str) or not _MONEY.search(x):
        return x
    keep = []
    for sent in re.split(r"(?<=[.!?])\s+", x):
        if _MONEY.search(sent):
            dropped.append(sent)
            continue
        keep.append(sent)
    return " ".join(keep)


def _label(sec: dict) -> str:
    """A quotation section header as a readable investment row label. The headers are written ALL CAPS, so the
    label used to go through .capitalize() to make it readable, which also destroyed every acronym and brand
    token in them: AI PRODUCTION printed "Ai production" and INTERFACE RESTYLE (GTMx) printed "(gtmx)" on the
    UAS deck (SEN-2026-0026). Word by word: a word the section's OWN item descriptions use is restored to that
    form, so the casing comes from the copy and not a hardcoded acronym list; a word still shouting and found
    nowhere is lowercased; a word that is already mixed case (GTMx) is left exactly as written."""
    h = re.sub(r"^[A-Z]\s*·\s*", "", sec.get("header") or "").strip()
    if not h or sum(c.isupper() for c in h) <= sum(c.islower() for c in h):
        return h                                    # already written in sentence case: leave it alone
    forms: dict[str, str] = {}
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]*",
                        " ".join(str(i.get("desc") or "") for i in sec.get("items") or [])):
        forms.setdefault(w.lower(), w)

    def one(m: re.Match) -> str:
        w = m.group(0)
        return forms.get(w.lower()) or (w.lower() if w.isupper() else w)
    h = re.sub(r"[A-Za-z][A-Za-z0-9]*", one, h)
    return h[:1].upper() + h[1:]


def _person_in_beat(beat: dict, people: list) -> dict | None:
    """The real, named person a beat features, when the client gave us photographs of them. Matched on any
    part of the name appearing in the beat's own text, so "Al-Husary" finds "Mohammed Al-Husary"."""
    hay = " ".join(str(beat.get(k) or "") for k in ("on_screen_text", "caption", "frame_prompt", "voiceover")).lower()
    for p in people:
        parts = [w for w in re.split(r"[\s,]+", str(p.get("name") or "")) if len(w) > 2]
        if any(w.lower() in hay for w in parts):
            return p
    return None


def _likeness_prefix(person: dict) -> str:
    """Hold a REAL person's face. The opposite instruction to the architecture references: there we want new
    people, here we want exactly this one. `look` is the client's own description of them, when we have it."""
    look = (person.get("look") or "").strip()
    return ("The attached photographs are of ONE SPECIFIC REAL PERSON, supplied by their own company for this "
            f"film: {person['name']}. Reproduce THEIR face accurately and recognisably, the same person, the same "
            "bone structure, the same hair and the same build"
            + (f": {look}" if look else "")
            + ". Do NOT substitute a different person and do NOT generalise the face. Hold the likeness exactly "
              "and change only the setting, the light and the camera. ")


def _rules(company: dict, *keys: str) -> str:
    out = []
    for k in keys:
        sk = store.get_skill_by_key(company["id"], k)
        if sk:
            out.append(worker._rules_block(sk))
    return "\n\n".join(o for o in out if o)


# --------------------------------------------------------------------------- the job

class Job:
    """One creative proposal. State lives in <ROOT>/<id>/state.json after every stage, so a failure or a
    restart resumes where it stopped instead of paying for the finished stages again."""

    def __init__(self, job_id: str):
        self.id = job_id
        self.dir = os.path.join(ROOT, job_id)
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, "state.json")
        self.s = json.load(open(p)) if os.path.exists(p) else {}

    def save(self):
        json.dump(self.s, open(os.path.join(self.dir, "state.json"), "w"), ensure_ascii=False, indent=1)

    @property
    def co(self) -> dict:
        return store.get_company_by_slug(self.s["company"])

    def log(self, msg: str):
        self.s.setdefault("log", []).append(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}")
        print(f"[creative {self.id}] {msg}", flush=True)
        self.save()

    def images_used(self) -> int:
        return int(self.s.get("images", 0))

    def image_cap(self) -> int:
        return int((profile.get(self.co["id"]) or {}).get("creative_image_cap") or IMAGE_CAP)

    # ---------------------------------------------------------------- 1 brief
    def stage_brief(self):
        from . import engine
        co = self.co
        facts = engine._deal_facts(co, int(self.s["deal_id"]), budget=40_000) if self.s.get("deal_id") else ""
        self.s["facts"] = facts
        out = provider.think_json(
            worker._now_line() + " You read a client's brief and the deal record for a production company and "
            "return the brief as structured facts. Only what the material says; never invent a count, date, name "
            "or price. The OWNER'S DIRECTION wins over the client's brief where they differ. JSON: "
            '{"client": "", "project": "", "summary": "2 sentences", "duration_s": <the film\'s running time in '
            'seconds, or null>, "deliverables": [""], "constraints": ["every limit: locations, budget, talent, '
            'usage, what the client supplies"], "candidate_locations": ["places the brief or owner names"], '
            '"live_shoot": true|false, "audio_supplied": true|false, "talent": [""], "usage": "", '
            '"timeline": ["dated milestones exactly as stated"], "we_supply": [""], "client_supplies": [""], '
            '"open_points": [""]}',
            f"OWNER'S DIRECTION:\n{self.s.get('words') or '(none)'}\n\nDEAL RECORD:\n{facts or '(none)'}",
            fast=True, max_tokens=4000, purpose="creative-brief", company=self.s["company"])
        if not out:
            raise RuntimeError("the brief could not be read from the deal record")
        self.s["brief"] = _clean_text(out)
        self.log(f"brief: {out.get('project')} ({len(facts):,} chars of deal record)")

    # ---------------------------------------------------------------- 2 location
    def stage_location(self):
        b = self.s["brief"]
        if not (b.get("live_shoot") and (b.get("candidate_locations") or re.search(r"locat", self.s.get("words", ""), re.I))):
            self.s["location"] = None
            self.log("location: not needed (no live shoot, or no place named)")
            return
        text = provider.think_research(
            worker._now_line() + " You are a location producer for a production company. Research the candidate "
            "locations with web search and pick the ONE that best carries the film within the owner's direction "
            "and constraints (cost, permits, one set for the whole film, light, scale, fit with the brief). Every "
            "fact must come from a page you actually read, with its URL. Return ONLY a JSON object: "
            '{"pick": {"name": "", "city": "", "why": "2-3 sentences", "facts": [{"k": "short label", "v": "value", '
            '"source": "url"}], "official_url": "the venue\'s own website", "commons_query": "the search words for '
            'this place on Wikimedia Commons", "features": ["what the camera can use at this one location"]}, '
            '"others": [{"name": "", "why_not": "one line"}]}',
            f"OWNER'S DIRECTION:\n{self.s.get('words') or '(none)'}\n\nBRIEF:\n{json.dumps(b, ensure_ascii=False)}",
            model=MODEL, max_tokens=8000, max_searches=8, purpose="creative-location", company=self.s["company"])
        loc = provider._loads(text) or {}
        if not (loc.get("pick") or {}).get("name"):
            raise RuntimeError("location research returned no pick")
        loc = _clean_text(loc)
        pk = loc["pick"]
        pk["photos"] = self._commons_photos(pk.get("commons_query") or pk["name"])
        pk["refs"] = [p["path"] for p in pk["photos"]] + self._official_refs(pk.get("official_url") or "")
        self.s["location"] = loc
        self.log(f"location: {pk['name']} ({len(pk['photos'])} licensed photos, {len(pk['refs'])} references)")

    def _commons_photos(self, query: str, limit: int = 3) -> list:
        """Photographs we may SHOW: Wikimedia Commons, free licence only, credit and licence recorded."""
        out = []
        try:
            r = httpx.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30, params={
                "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 20,
                "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 2000, "format": "json"})
            pages = sorted((r.json().get("query") or {}).get("pages", {}).values(),
                           key=lambda p: p.get("index", 99))
        except Exception:  # noqa: BLE001
            return out
        words = [w for w in re.findall(r"[a-z]{4,}", query.lower())]
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            meta = ii.get("extmetadata") or {}
            lic = (meta.get("LicenseShortName") or {}).get("value", "")
            title = p.get("title", "")
            if not LICENCES.match(lic) or ii.get("width", 0) < 1600 or not title.lower().endswith((".jpg", ".jpeg")):
                continue
            if words and not any(w in title.lower() for w in words):
                continue
            try:
                data = httpx.get(ii["thumburl"], headers=UA, timeout=60).content
            except Exception:  # noqa: BLE001
                continue
            path = os.path.join(self.dir, f"photo_{len(out) + 1}.jpg")
            if not _save_jpg(data, path, 2000):
                continue
            artist = re.sub("<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
            out.append({"path": path, "title": title, "credit": artist or "unknown", "licence": lic,
                        "page": ii.get("descriptionurl")})
            if len(out) >= limit:
                break
        return out

    def _official_refs(self, url: str, limit: int = 5) -> list:
        """The venue's own photographs, as PRIVATE references for the frames only: they make the generated
        frames look like the real place. They are never placed in the deck (not ours to publish)."""
        if not url:
            return []
        try:
            r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True)
            html = r.text
        except Exception:  # noqa: BLE001
            return []
        base = str(r.url)
        found = re.findall(r'(?:src|data-src|content)="([^"]+\.(?:jpe?g|webp|png)(?:\?[^"]*)?)"', html, re.I)
        seen, cands = set(), []
        for u in found:
            u = httpx.URL(base).join(u.replace("&amp;", "&"))
            k = str(u).split("?")[0]
            if k in seen or re.search(r"logo|icon|favicon|sprite|avatar", k, re.I):
                continue
            seen.add(k)
            cands.append(str(u))
        got = []
        for u in cands[:30]:
            try:
                data = httpx.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).content
            except Exception:  # noqa: BLE001
                continue
            if len(data) < 80_000:
                continue
            path = os.path.join(self.dir, f"ref_{len(got) + 1}.jpg")
            size = _save_jpg(data, path)
            if size and min(size) >= 600:
                got.append((len(data), path))
        got.sort(reverse=True)
        return [p for _, p in got[:limit]]

    # ---------------------------------------------------------------- 3 concept + 4 script
    def stage_concept(self, feedback: str = ""):
        co = self.co
        loc = self.s.get("location") or {}
        refs = ((loc.get("pick") or {}).get("refs") or [])[:8]
        prev = self.s.get("concept") if feedback else None
        out = provider.think_json(
            "\n\n".join(filter(None, [
                "You are the CREATIVE DIRECTOR at a production company writing the concept for a pitch. Give "
                "three genuinely different routes, choose the strongest for the owner's direction and the brief, "
                "and develop it. Simple beats clever when the owner has set a budget or location limit: the "
                "idea must be makeable within every constraint. The attached photographs are the REAL location: "
                "describe what is actually there, and say for each photograph what it shows.",
                worker._now_line(), worker._company_context(co), _rules(co, "sales-proposal"),
                "RULES: never invent a fact, figure, award, date or name; plain, specific language; no em dashes; "
                "no superlatives. Asset descriptors are VERBATIM text pasted into every image prompt, 25 to 60 "
                "words, concrete and visual (architecture, materials, wardrobe, light).",
                'RETURN JSON: {"routes": [{"title": "", "logline": "one sentence"}], "chosen": <index>, "title": '
                '"the chosen idea\'s name, under 6 words", "logline": "one sentence", "idea": "60-90 words", '
                '"duration_s": <running time>, "look": {"light": "", "palette": "", "grade": "", "optics": '
                '"written lens, depth and grain", "type": ""}, "assets": [{"tag": "@loc_...|@char_...|@prop_...", '
                '"descriptor": ""}], "ref_notes": ["what photograph 1, 2... shows"], "voices_tiles": <true if '
                'contributors film themselves elsewhere (UGC) and a page of them helps, else false>}',
            ])),
            (f"PREVIOUS CONCEPT (revise it: change exactly what the owner's feedback asks, keep the rest word for "
             f"word):\n{json.dumps(prev, ensure_ascii=False)}\n\nOWNER'S FEEDBACK:\n{feedback}\n\n" if prev else "")
            + f"OWNER'S DIRECTION:\n{self.s.get('words') or '(none)'}\n\nBRIEF:\n"
            + json.dumps(self.s["brief"], ensure_ascii=False)
            + (f"\n\nLOCATION (researched):\n{json.dumps({k: v for k, v in (loc.get('pick') or {}).items() if k not in ('photos', 'refs')}, ensure_ascii=False)}"
               if loc else ""),
            fast=False, model=MODEL, max_tokens=24000, purpose="creative-concept", company=self.s["company"],
            images=[_thumb(p) for p in refs] or None)
        if not out.get("title"):
            raise RuntimeError("the concept came back empty")
        self.s["concept"] = _clean_text(out)
        self.log(f"concept: {out['title']}")

    def stage_script(self, feedback: str = ""):
        c = self.s["concept"]
        dur = int(c.get("duration_s") or self.s["brief"].get("duration_s") or 60)
        prev = self.s.get("script") if feedback else None
        refs = (((self.s.get("location") or {}).get("pick") or {}).get("refs") or [])[:8]
        ask = ("Write the SCRIPT for the chosen idea, beat by beat. JSON: {\"beats\": [{\"n\": 1, \"time\": "
               "\"0:00-0:06\", \"on_screen_text\": \"short line or empty\", \"voiceover\": \"the narration heard "
               "over this beat, in the reader's own words, or empty where the beat plays on picture and sound "
               "alone\", \"caption\": \"12-20 words, what we "
               "see\", \"frame_prompt\": \"one style frame: subject, action, setting, time of day, light, lens, "
               "camera position, composition; paste the asset descriptors it uses WORD FOR WORD; under 120 "
               "words\", \"refs\": [numbers of the reference photographs that show this view], \"assets\": "
               "[\"the @tags of every asset this beat shows\"], \"cover\": <true on the ONE beat that makes the "
               "best cover: the location at its most striking, never a phone clip>}], \"tiles\": "
               "{\"heading\": \"\", \"body\": \"\", \"items\": [{\"label\": \"\", \"prompt\": \"a vertical phone "
               "selfie still of the contributor, place and action\"}]} or null}. "
               f"Between 7 and 11 beats; contiguous timings from 0:00 ending exactly at {dur // 60}:{dur % 60:02d}. "
               "ON-SCREEN TEXT and VOICEOVER are different things: never put the same words in both, and leave "
               "voiceover empty on a beat the owner's direction says carries no narration. The voiceover across "
               "all beats must read as one continuous piece of writing at a speakable pace for its timings. "
               "No legible text, logos or readable screens inside any frame: typography is added in post. "
               "Asymmetric compositions, one clear focal subject, never two people squared off to camera. "
               "No em dashes. Tiles only if the concept has contributors filming themselves (else null).")
        user = (f"CONCEPT:\n{json.dumps(c, ensure_ascii=False)}\n\nBRIEF:\n{json.dumps(self.s['brief'], ensure_ascii=False)}"
                f"\n\nOWNER'S DIRECTION:\n{self.s.get('words') or '(none)'}")
        if prev:
            user = (f"PREVIOUS SCRIPT (revise it: change exactly what the feedback asks, keep every other beat word "
                    f"for word):\n{json.dumps(prev, ensure_ascii=False)}\n\nOWNER'S FEEDBACK:\n{feedback}\n\n" + user)
        out, why = {}, ""
        for _ in range(2):
            out = provider.think_json(
                "You are the SCRIPTWRITER at a production company. " + ask + (f" FIX THIS: {why}." if why else ""),
                user, fast=False, model=MODEL, max_tokens=32000, purpose="creative-script",
                company=self.s["company"], images=[_thumb(p) for p in refs] or None)
            why = _timing_problem(out.get("beats") or [], dur) if out.get("beats") else "no beats came back"
            if not why:
                break
        if why:
            raise RuntimeError(f"the script failed its timing check: {why}")
        old = {b["n"]: b.get("frame_prompt") for b in (prev or {}).get("beats") or []}
        out = _clean_text(out)
        for i, b in enumerate(out["beats"]):     # numbered by code, never trusted from the writer
            b["n"] = i + 1
            b["refs"] = [int(r) for r in (b.get("refs") or []) if str(r).strip().isdigit()]
            descs = [a["descriptor"] for a in (c.get("assets") or []) if a.get("tag") in (b.get("assets") or [])
                     and a.get("descriptor") and a["descriptor"][:40] not in (b.get("frame_prompt") or "")]
            if descs:     # the asset descriptors ride in every frame BY CODE: the first dry run's writer left the
                b["frame_prompt"] = (b.get("frame_prompt") or "") + " " + " ".join(descs)   # choir out of beat 7
        out["changed_frames"] = [b["n"] for b in out["beats"] if not prev or old.get(b["n"]) != b.get("frame_prompt")]
        self.s["script"] = out
        self.log(f"script: {len(out['beats'])} beats, {dur}s, {len((out.get('tiles') or {}).get('items') or [])} tiles")

    # ---------------------------------------------------------------- 5 frames
    def stage_frames(self, only: list | None = None):
        sc, c = self.s["script"], self.s["concept"]
        refs = ((self.s.get("location") or {}).get("pick") or {}).get("refs") or []
        lk = c.get("look") or {}
        look = f" Look: {lk.get('optics', '')} Grade: {lk.get('grade', '')} Palette: {lk.get('palette', '')}."
        bans = (" Photoreal premium commercial film still, grounded realism, real-world imperfect detail, one clear "
                "focal subject, asymmetric composition. NO text, NO captions, NO logos, NO readable signage, NO "
                "numbers, NO watermark, NO lens flares, NO light streaks, NO floating bokeh circles.")
        use_ref = ("Use the attached reference photographs ONLY for the architecture, materials and setting of the "
                   "real place so it is recognisable; make a completely new photograph with new people, new light "
                   "and a new camera position. ")
        # A REAL NAMED PERSON WE HAVE PHOTOGRAPHS OF IS NEVER A GENERIC FACE (owner, 25 Sep 2026). The UAS deck
        # put an AI stranger under "Mohammed Al-Husary, Founder and Executive President" while the client's own
        # photographs of him sat in the brief. When the client supplies pictures of someone who appears in the
        # film, that beat is generated FROM those pictures and the likeness is held; only the setting, the light
        # and the camera change. The architecture prefix above says the opposite, so it must never reach them.
        people = [p for p in (self.s.get("people_refs") or []) if p.get("name") and p.get("refs")]
        frames = self.s.setdefault("frames", {})
        todo = only if only is not None else [b["n"] for b in sc["beats"]]
        jobs = []
        for b in sc["beats"]:
            if b["n"] not in todo:
                continue
            who = _person_in_beat(b, people)
            if who:
                rp, pre = who["refs"][:3], _likeness_prefix(who)
            else:
                rp = [refs[i - 1] for i in (b.get("refs") or []) if isinstance(i, int) and 0 < i <= len(refs)][:3]
                pre = use_ref if rp else ""
            for v in "ab":
                jobs.append((f"b{b['n']}_{v}", b["frame_prompt"] + look + bans, rp, "16:9", pre))
        if only is None:
            for i, t in enumerate(((sc.get("tiles") or {}).get("items") or [])[:8]):
                jobs.append((f"t{i + 1}", "Vertical still from a phone selfie video, front camera, handheld, natural "
                                          f"light, eye level: {t['prompt']}." + bans, [], "9:16", ""))
        if len(jobs) > self.image_cap():     # the cap is per generation pass (a revision regenerates only a few)
            raise RuntimeError(f"this pass needs {len(jobs)} images, over the cap of {self.image_cap()}; raise "
                               "creative_image_cap on the company profile or cut beats")

        def run(j):
            name, prompt, rp, aspect, pre = j
            data = imagegen.generate(pre + prompt, aspect=aspect, refs=rp, purpose="creative-frames",
                                     company=self.s["company"])
            path = os.path.join(self.dir, f"{name}.jpg")
            return name, (path if data and _save_jpg(data, path, 1920) else None)

        with ThreadPoolExecutor(6) as ex:
            got = dict(ex.map(run, jobs))
        self.s["images"] = self.images_used() + sum(1 for p in got.values() if p)
        for name, path in got.items():
            if name.startswith("t"):
                frames[name] = {"pick": path}
        for n in todo:
            cands = [(v, got.get(f"b{n}_{v}")) for v in "ab"]
            cands = [(v, p) for v, p in cands if p]
            if not cands:
                continue
            frames[f"b{n}"] = {"cands": dict(cands), "pick": cands[0][1]}
        self.save()
        self._cull(todo)
        self.log(f"frames: {sum(1 for p in got.values() if p)} of {len(jobs)} made; {self.images_used()} used in total")

    def _cull(self, beats: list):
        """Fable 5 picks the better of two frames per beat; legible text or a logo is a hard reject."""
        sc = {b["n"]: b for b in self.s["script"]["beats"]}
        assets = {a.get("tag"): a.get("descriptor", "") for a in self.s["concept"].get("assets") or []}
        frames = self.s["frames"]

        def one(n):
            f = frames.get(f"b{n}") or {}
            cands = list((f.get("cands") or {}).items())
            if len(cands) < 2:
                return n, None
            res = provider.think_json(
                "You are the CREATIVE DIRECTOR choosing a style frame for a client pitch. Two candidates for ONE "
                "beat, 'a' then 'b'. Pick the one that best shows the beat and looks least AI-generated. HARD "
                "REJECT any frame with legible text, a logo, readable signage or numbers. Also check: waxy skin, "
                "malformed hands or faces, repeated identical people, flares or light streaks, anything that "
                'contradicts the beat. HARD REJECT a frame whose people, their number, their wardrobe or the '
                'place contradict MUST SHOW. JSON: {"pick": "a|b", "reason": "one sentence", "reject_a": false, '
                '"reject_b": false}', f"BEAT {n}: {sc[n].get('caption')}\nMUST SHOW: "
                + " ".join(assets.get(t, "") for t in (sc[n].get("assets") or [])), fast=False, model=MODEL,
                max_tokens=6000,
                purpose="creative-cull", company=self.s["company"], images=[_thumb(p) for _, p in cands])
            return n, res

        with ThreadPoolExecutor(4) as ex:
            for n, res in ex.map(one, beats):
                if not res:
                    continue
                f = frames[f"b{n}"]
                pick = res.get("pick") if res.get("pick") in f["cands"] else "a"
                rej = {v for v in "ab" if res.get(f"reject_{v}")}
                if pick in rej and (set(f["cands"]) - rej):
                    pick = sorted(set(f["cands"]) - rej)[0]
                f.update({"pick": f["cands"][pick], "reason": res.get("reason"), "weak": set(f["cands"]) <= rej})
        self.save()

    # ---------------------------------------------------------------- 6 quotation
    def stage_quote(self, feedback: str = "", dry: bool = False):
        from . import engine
        from . import quotation as _q
        co, slug = self.co, self.s["company"]
        prev = (self.s.get("quote") or {}).get("spec") if feedback else None
        said = (self.s.get("said") or "") + "\n" + (self.s.get("words") or "") + "\n" + feedback
        presets = sorted((_q.presets() or {}).keys())
        b, c = self.s["brief"], self.s["concept"]
        scope = {"brief": b, "concept": {k: c.get(k) for k in ("title", "logline", "idea", "duration_s")},
                 "beats": [x.get("caption") for x in self.s["script"]["beats"]],
                 "contributor_tiles": len(((self.s["script"].get("tiles") or {}).get("items") or [])),
                 "location": ((self.s.get("location") or {}).get("pick") or {}).get("name")}
        spec = provider.think_json(
            worker._now_line() + " You turn a creative proposal's scope into the arguments for its quotation. "
            'JSON: {"preset": "<one of ' + ", ".join(presets) + '>", "title": "", "sections": [{"header": '
            '"A ·  SECTION NAME", "items": [{"desc": "one amount; name everything the line includes", '
            '"components": [{"item": "<rate card [key]>", "qty": <days, people or films>}], "unit": <a price '
            'ONLY if the OWNER stated that exact figure, else null>, "qty": 1}]}], "deliverables": [""], '
            '"exclusions": ["what is NOT included, one short line each"], "note": "the clauses for under the '
            'totals: exclusions, usage, options", "assumptions": ["each default you chose, for the owner only"], '
            '"target": <the total the OWNER asked for, excluding VAT, ONLY a figure written in his own words, '
            'else null>}. '
            "You never price a line: list its rate-card components and code prices it. Never state a total, a "
            "tier or a discount in the assumptions: code sets every figure and reports it. When the owner gives "
            "a target, put it in `target` and keep the scope he asked for; code moves the prices to reach it. "
            "Quote in BLOCKS as the "
            "quotation skill's rules describe. Everything the concept SHOWS that is made in post (a crowd, an "
            "audience, set extensions) must be priced as post work or stated as not included. No em dashes.\n\n"
            + _rules(co, "sales-quotation"),
            (f"PREVIOUS QUOTATION (revise it from the owner's feedback, keep the rest):\n"
             f"{json.dumps(prev, ensure_ascii=False)}\n\nOWNER'S FEEDBACK:\n{feedback}\n\n" if prev else "")
            + f"OWNER'S OWN WORDS (the only source of typed prices):\n{said.strip() or '(none)'}\n\nSCOPE:\n"
            + json.dumps(scope, ensure_ascii=False) + f"\n\nRATE CARD:\n{ratecard.render(slug)}",
            fast=False, model=provider.MODEL_FAST, max_tokens=16000, purpose="creative-quote", company=slug) or {}
        if not spec.get("sections"):
            raise RuntimeError("the quotation came back with no lines")
        spec = _clean_text(spec)
        stated = engine._stated_numbers(said)
        allowed = stated | ratecard.rates(slug)
        # THE OWNER'S NUMBER IS APPLIED BY CODE (21 Sep 2026): Emergy asked for 28,000; the run never passed a
        # target, the writer claimed 27,800 "at Budget tier" in its assumptions and code issued 39,500. Now the
        # target is honoured only if it is a figure he wrote, and a target code cannot reach stops the quote.
        target = spec.pop("target", None)
        try:
            target = float(target) if target not in (None, "") else None
        except (TypeError, ValueError):
            target = None
        if target is not None and round(target, 2) not in stated:
            target = None
        res = ratecard.price_lines(slug, spec["sections"], allowed=allowed, target=target,
                                   flex_pct=(profile.get(co["id"]) or {}).get("quote_target_flex_pct"))
        if target is not None and res.get("error"):
            raise RuntimeError(f"your figure AED {target:,.0f} was not reached: {res['error']}. Nothing was issued; "
                               "reply with a different figure or scope")
        spec["assumptions"] = [a for a in (spec.get("assumptions") or []) if not _MONEY_CLAIM.search(str(a))]
        if spec.get("preset") not in presets:
            spec["preset"] = "shoot-production" if b.get("live_shoot") else "ai-production"
        q = {"spec": spec, "blanked": res.get("blanked") or [], "missing": res.get("missing") or [],
             "target": target, "scaled": res.get("scaled"), "tier": res.get("tier"),
             "normal_total": res.get("normal_total")}
        if not dry:
            customer = self.s["customer"]
            number = (self.s.get("quote") or {}).get("number") or self._deal_quote_number(customer)
            deal = db.one("select contact_email from crm_projects where id=%s", (int(self.s["deal_id"]),)) or {}
            t = engine.deliver_quotation(slug, preset=spec["preset"], customer=customer, sections=spec["sections"],
                                         title=spec.get("title") or None, note=spec.get("note") or None,
                                         contact_email=deal.get("contact_email"), number=number,
                                         deliverables=spec.get("deliverables") or None,
                                         deal_id=int(self.s["deal_id"]))
            db.execute("update tasks set deal_id=%s where id=%s", (int(self.s["deal_id"]), t["id"]))
            rq = t.get("request") or {}
            q.update({"number": rq.get("number"), "card": t["id"], "summary": rq.get("summary"),
                      "version": len(db.setting_get(f"quote_versions:{rq.get('number')}") or [])})
        self.s["quote"] = q
        self.log(f"quote: {q.get('number') or '(dry run)'} v{q.get('version', '-')}, blank: {q['blanked']}")

    def _deal_quote_number(self, customer: str) -> str | None:
        """The deal's existing quotation number (same client) becomes the next VERSION, never a new number."""
        r = db.one("select request->>'number' n, request->>'customer' c from tasks where kind='quotation' and "
                   "deal_id=%s and request->>'number' is not null order by id desc limit 1", (int(self.s["deal_id"]),))
        return r["n"] if r and (r.get("c") or "").strip().lower() == customer.strip().lower() else None

    def quote_rows(self) -> tuple[list, float]:
        """The investment rows, READ BACK from the issued quotation (or the dry-run spec): one row per block,
        the amount summed by code. The deck can never contradict the quotation."""
        q = self.s["quote"]
        sections = q["spec"]["sections"]
        if q.get("number"):
            reg = db.setting_get(f"quote_versions:{q['number']}") or []
            if reg:
                sections = (reg[-1].get("spec") or {}).get("sections") or sections
        rows, net = [], 0.0
        for s in sections:
            h = _label(s)
            amt = sum(float(i.get("unit") or 0) * float(i.get("qty") or 1) for i in s.get("items") or [])
            blank = any(i.get("unit") in (None, "") for i in s.get("items") or [])
            net += amt
            rows.append({"item": h, "amount": "To be confirmed" if blank and not amt else f"AED {amt:,.0f}",
                         "items": [i.get("desc") for i in s.get("items") or []]})
        return rows, net

    # ---------------------------------------------------------------- 7 copy
    def stage_copy(self, feedback: str = ""):
        co = self.co
        rows, net = self.quote_rows()
        loc = (self.s.get("location") or {}).get("pick") or {}
        prev = self.s.get("copy") if feedback else None
        src = {"brief": self.s["brief"], "concept": self.s["concept"],
               "beats": [{k: b.get(k) for k in ("n", "time", "on_screen_text", "voiceover", "caption")}
                         for b in self.s["script"]["beats"]],
               "tiles": self.s["script"].get("tiles"),
               "location": {k: v for k, v in loc.items() if k not in ("photos", "refs")},
               "quotation": {"blocks": [{"block": r["item"], "lines": r["items"]} for r in rows],
                             "exclusions": self.s["quote"]["spec"].get("exclusions"),
                             "note": self.s["quote"]["spec"].get("note")}}
        out = provider.think_json(
            "\n\n".join(filter(None, [
                "You write the CLIENT-FACING copy of a creative proposal deck. The concept, script and quotation "
                "are decided; you put them into clear, confident words. Code lays out the pages, sets every "
                "price and carries the on-screen lines and the voice over verbatim: you never rewrite either, and "
                "the voice over page is laid out from the script itself, so only its heading, sub and note are yours.",
                worker._now_line(), worker._company_context(co), _rules(co, "sales-proposal"),
                "RULES: write to the client ('you'); plain and specific; no superlatives; no em dashes. Use ONLY "
                "facts in the material: no invented figure, date, award or name. Loglines, not paragraphs: every "
                "card body under 40 words. Faces in the frames are illustrations; anything shown that is made in "
                "post or not quoted (a crowd, an audience) is said plainly. Never write a price, a total, a VAT figure or "
                "a quotation number anywhere: the investment page prints them from the quotation.",
                'RETURN JSON: {"cover": {"kicker": "Creative proposal", "title1": "", "title2": "", "standfirst": '
                '"1-2 sentences"}, "brief": {"heading": "", "cards": [{"title": "", "body": ""}], "bullets": [""]}, '
                '"location": {"heading": "", "sub": "", "body": "50-70 words", "stats": [{"k": "", "v": ""}]}, '
                '"idea": {"heading": "", "sub": "", "line": "", "stats": [{"k": "", "v": ""}]}, "sheet_note": "", '
                '"voscript": {"heading": "", "sub": "", "note": ""}, '
                '"tiles": {"heading": "", "body": "", "note": ""}, "look": {"heading": "", "cols": [{"title": "", '
                '"stat": "", "body": ""}]}, "deliverables": {"heading": "", "cells": [{"title": "", "body": ""}]}, '
                '"make": {"heading": "", "phases": [{"when": "", "title": "", "body": ""}], "cards": [{"title": "", '
                '"body": ""}]}, "samples": {"heading": "", "categories": ["media library slugs"], "captions": ["", '
                '"", ""]}, "investment": {"blurb": "one sentence", "details": {"<block name>": "one line"}}, '
                '"terms": {"heading": "", "cards": [{"title": "", "body": ""}]}, "needs": {"heading": "", "cells": '
                '[{"title": "", "body": ""}]}, "close": {"heading": "", "cards": [{"title": "", "body": ""}], '
                '"steps": [{"title": "", "body": ""}]}}. Limits: brief 3 cards and up to 4 bullets; location up to '
                '4 stats; look 4 columns; deliverables up to 8 cells; make up to 5 phases and 2 cards; terms 3 '
                'cards; needs up to 4 cells; close 2 cards and 3 steps.',
                "MEDIA LIBRARY CATEGORY SLUGS (samples.categories may use ONLY these): "
                + ", ".join(deck.library_slugs(co["id"])),
            ])),
            (f"PREVIOUS COPY (revise it: change exactly what the owner's feedback asks, keep the rest word for word):"
             f"\n{json.dumps(prev, ensure_ascii=False)}\n\nOWNER'S FEEDBACK:\n{feedback}\n\n" if prev else "")
            + f"MATERIAL:\n{json.dumps(src, ensure_ascii=False)}",
            fast=False, model=MODEL, max_tokens=16000, purpose="creative-copy", company=self.s["company"])
        if not out.get("cover"):
            raise RuntimeError("the deck copy came back empty")
        source = (json.dumps(src, ensure_ascii=False) + (self.s.get("facts") or "")
                  + (self.s.get("words") or "")).lower()
        dropped: list = []
        self.s["copy"] = _strip_money(_drop_invented_dates(_clean_text(out), source, dropped), dropped)
        if dropped:
            self.log(f"copy: dropped {len(dropped)} sentence(s) with a date not in the source or a price")
        self.log("copy: written")

    # ---------------------------------------------------------------- 8 render + check
    def render(self, version: int, compact: bool = False) -> dict:
        co, c = self.co, self.s["concept"]
        cp = {k: (self.s["copy"].get(k) or {}) for k in (           # a section the writer left out renders empty,
              "cover", "brief", "location", "idea", "tiles", "look",  # it never crashes the layout
              "deliverables", "make", "samples", "investment", "terms", "needs", "close", "voscript")}
        cp["sheet_note"] = self.s["copy"].get("sheet_note") or ""
        sc, fr = self.s["script"], self.s["frames"]
        beats = sc["beats"]
        pick = lambda n: (fr.get(f"b{n}") or {}).get("pick")        # noqa: E731
        bg = [pick(b["n"]) for b in beats if pick(b["n"])] or [None]
        bgi = lambda i: bg[i % len(bg)]                             # noqa: E731
        q = self.s["quote"]
        rows, net = self.quote_rows()
        # the concept's short title: the full project name wrapped over the logo on the photo page's narrow footer
        label = f"{(c.get('title') or self.s['brief'].get('project') or 'Proposal')[:34]} · Creative proposal"
        d = deck.CreativeDeck(co, self.s["customer"], deck._ACCENT_DEFAULT, deck._logo(co), label[:80],
                              compact=compact)
        today = datetime.now(timezone.utc).date()
        meta = [{"k": "Client", "v": self.s["customer"]},
                {"k": "Film", "v": f"{c.get('title')}, {int(c.get('duration_s') or 60)} seconds"}]
        loc = (self.s.get("location") or {}).get("pick") or {}
        if loc.get("name"):
            meta.append({"k": "Location", "v": f"{loc['name']}{', ' + loc['city'] if loc.get('city') else ''}"})
        if q.get("number"):
            meta.append({"k": "Quotation", "v": f"{q['number']} v{q.get('version')}"})
        meta.append({"k": "Date", "v": f"{today.day} {today:%B %Y}"})
        cov = next((b for b in beats if b.get("cover")), None) or beats[max(0, len(beats) - 2)]
        climax = pick(cov["n"]) or bgi(0)
        d.covermeta(cp["cover"].get("kicker") or "Creative proposal", cp["cover"].get("title1") or c.get("title"),
                    cp["cover"].get("title2") or "", cp["cover"].get("standfirst") or c.get("logline"), meta,
                    climax, "Creative proposal")
        n = 1
        d.cards(f"{n:02d} · The brief", cp["brief"].get("heading"), cp["brief"].get("cards") or [],
                cp["brief"].get("bullets") or None)
        d.with_bg(bgi(0))
        n += 1
        if loc.get("name"):
            ph = (loc.get("photos") or [None])[0]
            d.photo(f"{n:02d} · The location", cp["location"].get("heading") or loc["name"],
                    cp["location"].get("sub") or "", cp["location"].get("body") or loc.get("why"),
                    (cp["location"].get("stats") or [])[:4], ph["path"] if ph else bgi(1),
                    (f"{loc['name']}. Photo: {ph['credit']}, Wikimedia Commons, {ph['licence']}" if ph else
                     "Style frame, an illustration of the location"), section="The location", focus="center")
            n += 1
        d.hero(f"{n:02d} · The idea", cp["idea"].get("heading") or c.get("title"), cp["idea"].get("sub") or "",
               cp["idea"].get("line") or c.get("logline"), (cp["idea"].get("stats") or [])[:3],
               pick(beats[1]["n"]) if len(beats) > 1 else bgi(0), "The idea")
        n += 1
        for b in beats:
            d.beat(c.get("title"), b["n"], len(beats), b["time"], b.get("on_screen_text") or "",
                   b.get("caption") or "", pick(b["n"]), "Storyboard", vo=b.get("voiceover") or "")
        d.sheet(f"{n:02d} · The film at a glance", f"{c.get('title')}: {int(c.get('duration_s') or 60)} seconds",
                [{"img": pick(b["n"]), "time": b["time"], "text": b.get("on_screen_text") or ""} for b in beats],
                "Storyboard", cp.get("sheet_note") or "Style frames show the look and the flow; faces are illustrations.")
        n += 1
        spoken = [b for b in beats if (b.get("voiceover") or "").strip()]
        if spoken:      # the narration end to end, only when the film actually has one
            first, last = spoken[0]["n"], spoken[-1]["n"]
            where = (f"Heard over beat {first}" if first == last else f"Heard over beats {first} to {last}")
            sub = where + (". The remaining beats play on picture and sound alone."
                           if len(spoken) < len(beats) else ".")
            d.voscript(f"{n:02d} · The voice over", cp["voscript"].get("heading") or "The words you hear",
                       cp["voscript"].get("sub") or sub,
                       [{"time": b["time"], "line": b.get("voiceover") or ""} for b in beats], "The voice over",
                       cp["voscript"].get("note") or "Read unhurried, one voice, over the picture.")
            n += 1
        tiles = [(t, (fr.get(f"t{i + 1}") or {}).get("pick")) for i, t in
                 enumerate(((sc.get("tiles") or {}).get("items") or [])[:8])]
        tiles = [{"img": p, "label": t.get("label")} for t, p in tiles if p]
        if tiles:
            d.tiles(f"{n:02d} · {cp['tiles'].get('heading') or 'The voices'}", cp["tiles"].get("heading") or "",
                    cp["tiles"].get("body") or "", tiles, cp["tiles"].get("note") or "", "The voices")
            n += 1
        d.strip(f"{n:02d} · Look and feel", cp["look"].get("heading"), "", (cp["look"].get("cols") or [])[:4])
        d.with_bg(bgi(4))
        n += 1
        cells = [{"num": f"{i + 1:02d}", **x} for i, x in enumerate((cp["deliverables"].get("cells") or [])[:8])]
        d.grid(f"{n:02d} · Deliverables", cp["deliverables"].get("heading") or "What you receive", "", cells)
        d.with_bg(bgi(5))
        n += 1
        d.phases(f"{n:02d} · How we make it", cp["make"].get("heading"), (cp["make"].get("phases") or [])[:5],
                 (cp["make"].get("cards") or [])[:2] or None)
        d.with_bg(bgi(3))
        n += 1
        films = []
        if deck.our_work_page(co, creative=True):   # never on a creative deck (owner, 17 Sep 2026: ChainX card 704)
            for i, f in enumerate(deck.pick_samples(co["id"], (cp.get("samples") or {}).get("categories") or [], 3)):
                caps = (cp.get("samples") or {}).get("captions") or []
                films.append({"youtube_video_id": f["youtube_video_id"], "label": f["title"],
                              "caption": caps[i] if i < len(caps) else "", "thumb": deck.thumbnail(f["youtube_video_id"], self.dir)})
            d.samples(f"{n:02d} · Our work", (cp.get("samples") or {}).get("heading") or "Films we have made", "", films)
            n += 1
        det = (cp.get("investment") or {}).get("details") or {}
        short = lambda t: t if len(t) <= 95 else t[:92].rsplit(" ", 1)[0] + "..."     # noqa: E731
        inv = [{"item": r["item"], "detail": short(str(det.get(r["item"]) or "; ".join(str(x) for x in r["items"]))),
                "amount": r["amount"]} for r in rows]
        exc = [re.split(r"[:;(]", str(x))[0].strip() for x in (q["spec"].get("exclusions") or [])]
        if exc:     # ONE row: a row per exclusion ran the table off the page in the first dry run
            inv.append({"item": "Not included", "detail": short("; ".join(exc)), "amount": "Excluded"})
        if len(inv) > 8:
            d.compact = True
        blurb = ("Excluding VAT" + (f", as Quotation {q['number']} v{q.get('version')}" if q.get("number") else "")
                 + ". " + ((cp.get("investment") or {}).get("blurb") or ""))
        d.investment(f"{n:02d} · Investment", f"AED {net:,.0f}", blurb.strip(), inv)
        d.with_bg(bgi(len(bg) - 1))
        n += 1
        tcards = [x for x in (cp["terms"].get("cards") or [])
                  if not re.search(r"includ|exclu", str(x.get("title", "")), re.I)][:2]
        if exc:     # what is not included comes from the QUOTATION, by code
            _exc, _n = [], 0
            for _x in exc:            # WHOLE exclusions only: a char slice cut one mid-word (UAS, 23 Sep 2026)
                if _n + len(_x) + 2 > 260:
                    break
                _exc.append(_x); _n += len(_x) + 2
            tcards.append({"title": "Not included", "body": "; ".join(_exc or exc[:1])})
        d.cards(f"{n:02d} · Terms at a glance", cp["terms"].get("heading") or "Usage, payment and exclusions",
                tcards[:3], None)
        d.with_bg(bgi(0))
        n += 1
        cells = [{"num": f"{i + 1:02d}", **x} for i, x in enumerate((cp["needs"].get("cells") or [])[:4])]
        d.grid(f"{n:02d} · What we need from you", cp["needs"].get("heading") or "What we need from you", "", cells)
        d.with_bg(bgi(2))
        n += 1
        d.closing(f"{n:02d} · Next", cp["close"].get("heading"), "", (cp["close"].get("cards") or [])[:2],
                  (cp["close"].get("steps") or [])[:3], f"{co['name']} · hello@sensa.digital")
        d.with_bg(bgi(len(bg) - 2))
        from . import engine
        name = engine._proposal_name(self.s["customer"], q.get("number"), version,
                                     today.strftime("%Y-%m-%d"))
        path = deck.to_pdf(d.html(), os.path.join(self.dir, name))
        from pypdf import PdfReader
        pages = len(PdfReader(path).pages)
        return {"path": path, "name": name, "planned": len(d.pages), "pages": pages, "films": [f["youtube_video_id"] for f in films]}

    def stage_render(self, version: int) -> dict:
        out = self.render(version)
        if out["pages"] != out["planned"]:          # a page overflowed: set it tighter once, then say so
            self.log(f"render: {out['pages']} pages for {out['planned']} planned, re-setting compact")
            out = self.render(version, compact=True)
        out["overflow"] = out["pages"] != out["planned"]
        self.s["deck"] = {**out, "version": version}
        self.log(f"render: {out['pages']} pages" + (" (STILL OVERFLOWING)" if out["overflow"] else ""))
        return out

    # ---------------------------------------------------------------- 9 file + card
    def stage_file(self, task: dict | None):
        from . import engine
        co, dk, q = self.co, self.s["deck"], self.s["quote"]
        did = int(self.s["deal_id"])
        old = [r["id"] for r in db.query(
            "select id from company_documents where deal_id=%s and kind='proposal' and superseded_by is null",
            (did,))]
        doc, filed = engine._file_proposal_pdf(co, self.s["customer"], dk["name"], dk["path"], did,
                                               q.get("number"), supersede=old)
        att = {"id": doc["id"], "filename": doc["filename"], "mime": doc["mime"], "size": doc["size"]}
        if q.get("card"):      # the deck rides on the quotation card: one email sends both
            t = db.one("select request from tasks where id=%s", (q["card"],))
            ad = [a for a in ((t or {}).get("request") or {}).get("attach_docs") or [] if "Proposal" not in a.get("filename", "")]
            db.execute("update tasks set request = request || jsonb_build_object('attach_docs', %s::jsonb) where id=%s",
                       (Json(ad + [att]), q["card"]))
        self.s["filed"] = {"doc": att, "folder": filed}
        pipeline.log_deal(did, "proposal", f"Creative proposal v{dk['version']} filed: {dk['name']}"
                                           + (f", with quotation {q['number']} v{q.get('version')}" if q.get("number") else "")
                                           + (f". Card {task['id']}." if task else "."))
        self.log(f"filed: doc {doc['id']} in {filed or 'the library only'}")

    def summary(self, changed: str = "") -> str:
        c, q, dk = self.s["concept"], self.s["quote"], self.s["deck"]
        loc = (self.s.get("location") or {})
        pk = loc.get("pick") or {}
        rows, net = self.quote_rows()
        fr = self.s.get("frames") or {}
        weak = [k for k, v in fr.items() if isinstance(v, dict) and v.get("weak")]
        parts = [f"Creative proposal v{dk['version']} for {self.s['customer']}: {dk['pages']} pages."]
        if changed:
            parts.append(f"Your change: {changed.strip()[:400]}")
        parts.append(f"Concept: {c.get('title')}. {c.get('logline')}")
        others = [r for i, r in enumerate(c.get("routes") or []) if i != c.get("chosen")]
        if others:
            parts.append("Other routes considered: " + "; ".join(f"{r.get('title')} ({r.get('logline')})" for r in others[:2]))
        if pk.get("name"):
            parts.append(f"Location: {pk['name']}. {pk.get('why', '')}"
                         + (" Passed over: " + "; ".join(f"{o.get('name')} ({o.get('why_not')})" for o in (loc.get("others") or [])[:4]) if loc.get("others") else ""))
            if not pk.get("photos"):
                parts.append("No licensed photograph of the location was found, so the location page uses a style frame.")
        if q.get("number"):
            parts.append(f"Quotation {q['number']} v{q.get('version')}: AED {net:,.0f} + VAT, on card #{q.get('card')}, "
                         "and this deck is attached to it.")
        else:
            parts.append(f"Priced (not issued): AED {net:,.0f} + VAT.")
        if q.get("target") is not None:         # stamped by code from the pricing result, never by the writer
            how = []
            if q.get("tier") == "budget":
                how.append(f"Budget tier used (Normal tier came to AED {float(q.get('normal_total') or 0):,.0f})")
            if q.get("scaled"):
                how.append(f"rate-card lines moved {q['scaled']:+.1f}%")
            parts.append(f"Your figure AED {q['target']:,.0f} + VAT: reached"
                         + (" (" + "; ".join(how) + ")" if how else "") + ".")
        if q.get("blanked"):
            parts.append("Left BLANK, no approved price: " + "; ".join(q["blanked"][:6]))
        if q["spec"].get("exclusions"):
            parts.append("Not included: " + "; ".join(q["spec"]["exclusions"][:5]))
        if q["spec"].get("assumptions"):
            parts.append("Assumed: " + "; ".join(str(a) for a in q["spec"]["assumptions"][:8]))
        if weak:
            parts.append("Weak frames (both candidates rejected, worth regenerating): " + ", ".join(weak))
        if dk.get("overflow"):
            parts.append(f"CHECK: the PDF has {dk['pages']} pages for {dk['planned']} planned, a page overflows.")
        parts.append(f"{self.images_used()} images generated on this proposal. Nothing has been sent. Reply on this card "
                     "with any change (the concept, a beat, a frame, the copy, the location or the price) and only "
                     "the parts it touches are redone. Approve when you are happy: the quotation then comes up as its own "
                     "card to check and approve, and the email with both attached follows.")
        return "\n\n".join(parts)


# --------------------------------------------------------------------------- the run

STAGES = ("brief", "location", "concept", "script", "frames", "quote", "copy", "render", "file")


def _run(job: Job, task: dict | None, dry: bool = False, start_at: str = "brief"):
    i = STAGES.index(start_at)
    for st in STAGES[i:]:
        if dry and st == "file":
            break
        job.s["stage"] = st
        job.save()
        if st == "brief":
            job.stage_brief()
        elif st == "location":
            job.stage_location()
        elif st == "concept":
            job.stage_concept()
        elif st == "script":
            job.stage_script()
        elif st == "frames":
            job.stage_frames()
        elif st == "quote":
            job.stage_quote(dry=dry)
        elif st == "copy":
            job.stage_copy()
        elif st == "render":
            job.stage_render(int(job.s.get("next_version") or 1))
        elif st == "file":
            job.stage_file(task)
    job.s["stage"] = "done"
    job.save()


def _next_version(customer: str, number: str | None, deal_id: int) -> int:
    """After every proposal already filed on this deal: the next vN, so the Drive history reads in order."""
    names = [r["filename"] for r in db.query(
        "select filename from company_documents where deal_id=%s and kind='proposal'", (deal_id,))]
    vs = [int(m.group(1)) for n in names for m in [re.search(r" v(\d+) - \d{4}-\d{2}-\d{2}", n)] if m]
    return (max(vs) if vs else (1 if names else 0)) + 1


def start(company_slug: str, deal_id: int, *, direction: str = "", said: str = "", dry: bool = False,
          background: bool = True) -> dict:
    """Talk's entry point: a review card (status drafting) and the run on its own thread."""
    from . import engine
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    deal = db.one("select id, title from crm_projects where id=%s", (int(deal_id),))
    if not deal:
        raise ValueError(f"no opportunity #{deal_id}")
    busy = db.one("select id from tasks where deal_id=%s and status='drafting' and request->>'kind'='creative_proposal' "
                  "and updated_at > now() - interval '45 minutes' limit 1", (int(deal_id),))
    if busy:
        raise ValueError(f"a creative proposal is already being built on this deal (card #{busy['id']})")
    customer = engine._deal_client_name(int(deal_id)) or co["name"]
    jid = f"{deal_id}-{datetime.now(timezone.utc):%Y%m%d%H%M}-{secrets.token_hex(2)}"
    job = Job(jid)
    job.s.update({"company": company_slug, "deal_id": int(deal_id), "customer": customer, "words": direction,
                  "said": said, "next_version": _next_version(customer, None, int(deal_id))})
    job.save()
    task = None
    if not dry:
        skill = store.get_skill_by_key(co["id"], "sales-proposal") or store.get_skill_by_key(co["id"], "sales-quotation")
        req = {"kind": "creative_proposal", "job": jid, "customer": customer, "deal_id": int(deal_id),
               "brief": f"Creative proposal for {customer}: {deal['title']}", "direction": direction}
        task = db.execute("insert into tasks (company_id, skill_id, kind, request, draft, status, title, deal_id) "
                          "values (%s,%s,'content',%s,%s,'drafting',%s,%s) returning *",
                          (co["id"], skill["id"], Json(req),
                           "Building the creative proposal: brief, location, concept, script, frames, quotation and "
                           "deck. This takes about 10 to 15 minutes; the card returns to your Inbox when it is ready.",
                           f"Creative proposal: {customer}", int(deal_id)))
    if background:
        threading.Thread(target=run_task, args=(jid, (task or {}).get("id"), dry), daemon=True).start()
    else:
        run_task(jid, (task or {}).get("id"), dry)
    return {"job": jid, "task_id": (task or {}).get("id"), "dir": job.dir}


def run_task(jid: str, task_id: int | None, dry: bool = False, start_at: str | None = None) -> None:
    job = Job(jid)
    task = store.get_task(task_id) if task_id else None
    try:
        _run(job, task, dry=dry, start_at=start_at or (job.s.get("stage") if job.s.get("stage") not in (None, "done") else "brief"))
    except Exception as e:  # noqa: BLE001 — a visible, resumable card, never a silent failure
        job.log(f"FAILED at {job.s.get('stage')}: {type(e).__name__}: {e}")
        if task:
            store.update_task(task["id"], status="awaiting_correction",
                              draft=f"The creative proposal stopped at the {job.s.get('stage')} stage: {e}. Reply on "
                                    "this card (for example 'resume', or with a change) and it continues from there; "
                                    "the finished stages are kept.")
            notifications.notify(f"Card #{task['id']}: the creative proposal stopped", str(e)[:300],
                                 category="approval", company_id=task.get("company_id"),
                                 target_type="task", target_id=str(task["id"]))
        return
    if task:
        req = dict(task.get("request") or {})
        req.update({"title": job.s["deck"]["name"], "file": job.s["deck"]["path"], "version": job.s["deck"]["version"],
                    "quotation_number": job.s["quote"].get("number"),
                    "attach_docs": [job.s["filed"]["doc"]] if job.s.get("filed") else []})
        store.update_task(task["id"], request=req, draft=job.summary(), title=job.s["deck"]["name"],
                          status="awaiting_approval")
        if (job.s.get("quote") or {}).get("card"):   # the quotation card is the other half of the approval gate
            db.execute("update tasks set request = request || %s::jsonb where id=%s",
                       (json.dumps({"proposal_card": task["id"]}), int(job.s["quote"]["card"])))
        notifications.notify(f"Creative proposal ready: {job.s['customer']}",
                             f"{job.s['deck']['pages']} pages, card #{task['id']}.", category="approval",
                             company_id=task.get("company_id"), target_type="task", target_id=str(task["id"]))


# --------------------------------------------------------------------------- revisions

def revise(task: dict, text: str) -> bool:
    """His reply on the card. An unfinished run resumes; a finished one is revised: a planner decides which
    parts his words touch, only those re-run, and the deck is filed as the next version on the same card."""
    req = task.get("request") or {}
    if not req.get("job"):
        return False
    job = Job(req["job"])
    if job.s.get("stage") not in (None, "done"):
        job.s["words"] = (job.s.get("words") or "") + "\n" + text
        job.save()
        store.update_task(task["id"], status="drafting", draft="Resuming the creative proposal from where it stopped.")
        threading.Thread(target=run_task, args=(job.id, task["id"]), daemon=True).start()
        return True
    plan = provider.think_json(
        "You route an owner's feedback on a creative proposal to the parts that must change. JSON: "
        '{"location": false, "concept": false, "script": false, "regenerate_beats": [beat numbers whose PICTURE '
        'must change], "quote": false, "copy": true, "note": "one line"}. location = pick a different place; '
        "concept = a different idea or a change to the idea; script = beats, timings or on-screen lines; quote = "
        "anything about price, lines, exclusions or clauses; copy = wording on the pages. Mark only what is needed.",
        f"FEEDBACK:\n{text}\n\nCURRENT CONCEPT: {job.s['concept'].get('title')}: {job.s['concept'].get('logline')}\n"
        f"BEATS: " + "; ".join(f"{b['n']} {b.get('caption')}" for b in job.s["script"]["beats"]),
        fast=True, max_tokens=1500, purpose="creative-revise-plan", company=job.s["company"]) or {"copy": True}
    job.s["words"] = (job.s.get("words") or "") + "\n" + text
    job.s.setdefault("revisions", []).append({"text": text, "plan": plan})
    store.update_task(task["id"], status="drafting", draft=f"Revising: {plan.get('note') or text[:200]}")

    def _go():
        try:
            if plan.get("location"):
                job.stage_location()
                plan["concept"] = True
            if plan.get("concept"):
                job.stage_concept(feedback=text)
                plan["script"] = True
            if plan.get("script"):
                job.stage_script(feedback=text)
            regen = sorted(set((job.s["script"].get("changed_frames") or []) if plan.get("script") else [])
                           | {int(n) for n in plan.get("regenerate_beats") or [] if str(n).isdigit()})
            if plan.get("concept"):
                job.stage_frames()
            elif regen:
                job.stage_frames(only=regen)
            if plan.get("quote") or plan.get("concept"):
                job.stage_quote(feedback=text)
            job.stage_copy(feedback=text)
            job.stage_render(int(job.s["deck"]["version"]) + 1)
            job.stage_file(task)
            job.save()
            r = dict(task.get("request") or {})
            r.update({"title": job.s["deck"]["name"], "file": job.s["deck"]["path"],
                      "version": job.s["deck"]["version"], "quotation_number": job.s["quote"].get("number"),
                      "attach_docs": [job.s["filed"]["doc"]]})
            store.update_task(task["id"], request=r, draft=job.summary(changed=text), title=job.s["deck"]["name"],
                              status="awaiting_approval", attempts=(task.get("attempts") or 0) + 1)
            store.log_decision(task["id"], task["skill_id"], "owner", "correct", note=text,
                               snapshot={"version": job.s["deck"]["version"], "plan": plan})
            if (job.s.get("quote") or {}).get("card"):
                db.execute("update tasks set request = request || %s::jsonb where id=%s",
                           (json.dumps({"proposal_card": task["id"]}), int(job.s["quote"]["card"])))
            try:   # an email already waiting with the old deck and quotation takes the new ones
                from . import engine as _eng
                _eng.refresh_proposal_email(task["id"])
            except Exception as _re:  # noqa: BLE001 - the revision stands
                job.log(f"email refresh: {type(_re).__name__}: {_re}")
        except Exception as e:  # noqa: BLE001
            job.log(f"REVISION FAILED: {type(e).__name__}: {e}")
            store.update_task(task["id"], status="awaiting_approval",
                              draft=f"The revision did not complete ({e}). The card still holds the last version. "
                                    + job.summary())
            notifications.notify(f"Card #{task['id']}: the revision did not complete", str(e)[:300],
                                 category="approval", company_id=task.get("company_id"),
                                 target_type="task", target_id=str(task["id"]))

    threading.Thread(target=_go, daemon=True).start()
    return True


if __name__ == "__main__":      # python -m cortex.creative dry <deal_id> "<direction>"  |  resume <job> [task_id]
    import sys
    if sys.argv[1] == "dry":
        print(start(sys.argv[4] if len(sys.argv) > 4 else "sensa", int(sys.argv[2]), direction=sys.argv[3],
                    dry=True, background=False))
    elif sys.argv[1] == "rerun":      # python -m cortex.creative rerun <job> <stage>: dry, nothing issued or filed
        run_task(sys.argv[2], None, dry=True, start_at=sys.argv[3])
    elif sys.argv[1] == "resume":
        run_task(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
