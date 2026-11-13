# -*- coding: utf-8 -*-
"""CLIENT APPROVAL DECKS, built from a Drive folder (owner, 9 Nov 2026).

The brief: Gino drops labelled images into the client's Drive folder with a guidance document,
tells Cortex, and an approval deck comes out. He drops an amended picture in later, tells Cortex
again, and the deck updates.

WHERE THINGS LIVE
    <clients_drive_folder>/<Client>/APPROVAL ASSETS/<Production>/
        Daughter (Character).png        a character turnaround: front, back, close-up
        DSO HQ Location.png             a location plate
        Drone (Prop).png                a prop detail sheet
        Guidance.docx                   Gino's notes: what each item is and what it does
        STORYBOARD/Scene 4 - 2.png      the storyboard frames, named by scene
        DSO_VO_Script.pdf               the voice-over script, if there is one

THE LABEL IS THE FILENAME. A trailing Character, Wardrobe, Location or Prop decides the section,
with or without brackets, any case, any amount of whitespace, because that is how the labels
actually arrive. What is left is the item's name.

CODES ARE STABLE, which is the part that makes an update safe. CH-01 is pinned to an item NAME in
a stored registry, so replacing Daughter (Character).png with a new render leaves CH-01 pointing at
the daughter. A new item takes the next free number; a removed one keeps its number reserved, so a
code a client has already written on never comes back meaning something else.

CAPTIONS COME FROM GINO'S GUIDANCE DOCUMENT, never from the model's imagination: the writer is
given the guidance text and the item names and told to use nothing else, and an item the guidance
does not mention gets no caption rather than an invented one. Captions are stored once and reused,
so a rebuild re-lays the same words out (the `reuse_copy` principle from the capability deck).
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone

from psycopg.types.json import Json

from . import db, deck, documents, drive, profile, provider, store

ROOT = "/opt/coretex/creative/_approval"
ASSETS_FOLDER = "APPROVAL ASSETS"
STORYBOARD_FOLDER = "STORYBOARD"

SECTIONS = [("character", "CH", "Characters"), ("wardrobe", "WD", "Wardrobe"),
            ("location", "LOC", "Locations"), ("prop", "PR", "Props")]
PREFIX = {k: p for k, p, _ in SECTIONS}
TITLE = {k: t for k, _, t in SECTIONS}

# THE TEAM'S FOLDER SHAPE IS READ, NOT DICTATED (owner, 12 Nov 2026). Gino's DSO folder is
# "APPROVAL ASSET" (singular) holding SCENES/ and ASSETS/; Shama's is one subfolder per film. Both
# are read, and so is a flat folder. Nobody is asked to rename a folder to suit the code.
_SCENE_DIRS = ("storyboard", "storyboards", "scenes", "scene", "frames", "shots")
_ASSET_DIRS = ("assets", "asset", "plates", "items", "elements", "approval-assets",
               "approval-asset", "approval")


def _role(name: str) -> str:
    """What a subfolder of the production folder IS: 'scenes', 'assets', or '' (a film)."""
    s = _slug(name)
    return "scenes" if s in _SCENE_DIRS else "assets" if s in _ASSET_DIRS else ""


_IMAGE = (".png", ".jpg", ".jpeg", ".webp")
_DOC = (".pdf", ".docx", ".txt", ".md", ".rtf")
# WHICH DOCUMENT IS THE SCRIPT, matched on a name with its separators flattened to spaces.
# `DSO_VO_by_Scene-v2.pdf` failed `\bvo\b` outright, because an underscore is a word character:
# the voice-over script was filed as the guidance document, so every scene page printed with no
# title, no action and no voice over, and the plate captions were written from the wrong file
# (12 Nov 2026, the first real DSO run).
_SCRIPT_NAME = re.compile(r"script|voice.?over|\bvo\b|\bvo by scene\b", re.I)
# a trailing section word, with or without brackets, and tolerant of the stray unclosed one
_LABEL = re.compile(r"[\s(\[-]*\b(character|wardrobe|location|prop|props)\b\s*[)\]]*\s*$", re.I)
# A leading number is the team's own ordering of a section ("1 - Nurse ( Hospital)").
# A plate labelled (Cover) is the cover picture, not something to approve.
_COVER = re.compile(r"[\s(\[-]*\bcover\b\s*[)\]]*\s*$", re.I)
_ORDER = re.compile(r"^\s*0*(\d{1,3})\s*[-_.)]\s*")
_SCENE = re.compile(r"scene\s*0*(\d+)(?:\s*[-_]\s*0*(\d+))?", re.I)
# a numbered frame, "07 - The weaver, in memory.jpg"; Drive's "Copy of " and a doubled
# extension ("....jpg.png") are how hand-copied files actually arrive
_FRAME = re.compile(r"^\s*(?:copy of\s+)*0*(\d{1,3})\s*[-_.]\s*(.+?)\s*$", re.I)
_EXT = re.compile(r"(\.(?:png|jpe?g|webp))+$", re.I)


# --------------------------------------------------------------------------- labels and codes
def parse_label(filename: str) -> tuple[str | None, str]:
    """('character', 'Daughter') from 'Daughter (Character).png'. (None, stem) when unlabelled.

    A leading number is the team's own ORDER ("1 - Nurse ( Hospital) (Character).png") and is not
    part of the name: it decides where the item prints, and the first one is the cover."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    m = _LABEL.search(stem)
    if not m:
        return None, re.sub(r"\s{2,}", " ", _ORDER.sub("", stem)).strip()
    kind = m.group(1).lower().rstrip("s") if m.group(1).lower() != "props" else "prop"
    name = re.sub(r"\s{2,}", " ", _ORDER.sub("", stem[:m.start()])).strip(" -([")
    return kind, name or stem


def order_of(filename: str) -> int | None:
    """The number the team put at the front of the file name, or None. Their order, not ours."""
    m = _ORDER.match(os.path.splitext(os.path.basename(filename or ""))[0])
    return int(m.group(1)) if m else None


def _slot(kind: str, name: str) -> str:
    """The registry key for an item, with everything that is NOT its identity removed: the order
    number, the bracket spacing, the case, the punctuation. "Ward", "8 - Ward" and "ward " are one
    item, so renumbering a folder never mints a second code (HBMSU Nurse, 13 Nov 2026)."""
    n = _ORDER.sub("", name or "")
    n = re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()
    return "%s/%s" % (kind, n)


def scene_of(filename: str) -> tuple[int, int] | None:
    """(4, 2) from 'Scene 4 - 2.png'; (4, 0) from 'Scene 4.png'; None when it is not a frame."""
    m = _SCENE.search(os.path.basename(filename or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def frame_of(filename: str) -> tuple[int, str] | None:
    """(7, 'The weaver, in memory') from 'Copy of 07 - The weaver, in memory.jpg.png'."""
    stem = _EXT.sub("", os.path.basename(filename or ""))
    m = _FRAME.match(stem)
    return (int(m.group(1)), m.group(2).strip()) if m else None


def _plain(filename: str) -> str:
    return re.sub(r"^(?:copy of\s+)+", "", _EXT.sub("", os.path.basename(filename or "")),
                  flags=re.I).strip()


def film_prefix(name: str, taken: set) -> str:
    """MJ for Majlis, SQ for Saqarah, SD for Sadaf: the first letter and the next consonant,
    so two films sharing an initial still get distinct codes."""
    w = re.sub(r"[^A-Za-z]", "", name or "").upper() or "FM"
    cons = [c for c in w[1:] if c not in "AEIOU"] or list(w[1:]) or ["X"]
    for c in cons:
        if w[0] + c not in taken:
            return w[0] + c
    n = 2
    while "%s%s%d" % (w[0], cons[0], n) in taken:
        n += 1
    return "%s%s%d" % (w[0], cons[0], n)


def _key(company_slug: str, client: str, production: str) -> str:
    return "approval:%s:%s:%s" % (company_slug, _slug(client), _slug(production))


def _today() -> str:
    """Code stamps the date. A model never writes one."""
    return datetime.now(timezone.utc).strftime("%-d %B %Y")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "untitled"


def assign_codes(key: str, items: list[dict]) -> list[dict]:
    """Pin a code to each item NAME, for good.

    A replaced image keeps its code, because the registry is keyed on the name, not on the order
    the folder happens to list. A code belonging to an item that has since gone is never reissued:
    the client may already have written it down."""
    raw = dict(db.setting_get(key + ":codes") or {})
    # EVERY NUMBER EVER ISSUED STAYS TAKEN, read before anything is collapsed: a code the client may
    # have written on must never come back meaning something else.
    taken = {k: set() for k in PREFIX}
    for slot, code in raw.items():
        k = slot.split("/", 1)[0]
        if k in taken and "-" in code:
            taken[k].add(int(code.split("-")[1]))
    # MIGRATE historic keys onto the normalised slot, keeping the lowest code where a rename minted
    # a second one for the same item.
    reg = {}
    for slot, code in raw.items():
        k, _, nm = slot.partition("/")
        s2 = _slot(k, nm)
        if s2 not in reg or code < reg[s2]:
            reg[s2] = code
    out = []
    # ISSUED IN THE ORDER IT WILL PRINT (13 Nov 2026): numbering in arrival order and sorting
    # afterwards gave HBMSU Nurse CH-05, CH-04, CH-03, CH-06, CH-02, CH-01 down the page. A client
    # reading a deck whose codes run backwards does not trust the codes.
    for it in sorted(items, key=lambda x: (x["kind"], x.get("order") is None,
                                           x.get("order") or 0, x["name"].lower())):
        kind = it["kind"]
        slot = _slot(kind, it["name"])
        code = reg.get(slot)
        if not code:
            n = next(i for i in range(1, 100) if i not in taken[kind])
            code = "%s-%02d" % (PREFIX[kind], n)
            reg[slot] = code
            taken[kind].add(n)
        out.append({**it, "code": code})
    if reg != raw:
        db.setting_set(key + ":codes", reg)
    # THEIR ORDER, THEN OURS: a numbered item prints where they numbered it; the rest follow by code.
    return sorted(out, key=lambda x: (x["kind"], x.get("order") is None,
                                      x.get("order") or 0, x["code"]))


# --------------------------------------------------------------------------------- Drive sync
def folder_id(company_id: int, client: str, production: str, tok: str) -> str | None:
    """`<clients_drive_folder>/<Client>/APPROVAL ASSETS/<Production>`, or None if it is not there."""
    parent = ((profile.get(company_id) or {}).get("clients_drive_folder") or "").strip()
    if not parent:
        return None
    cf = drive.ensure_client_folder(client, parent, token=tok)
    if not cf.get("id"):
        return None
    # "APPROVAL ASSETS", "Approval Asset", "approval assets": whatever the team typed. Never
    # create a second one beside it (Shama's folder is "Approval Asset", 9 Nov 2026).
    assets = next((f["id"] for f in drive.list_folder(cf["id"], tok)
                   if f.get("mimeType") == "application/vnd.google-apps.folder"
                   and _slug(f.get("name")).rstrip("s") == _slug(ASSETS_FOLDER).rstrip("s")), None)
    if not assets:
        return None
    subs = [f for f in drive.list_folder(assets, tok)
            if f.get("mimeType") == "application/vnd.google-apps.folder"]
    for f in subs:
        if _slug(f.get("name")) == _slug(production):
            return f["id"]
    # A client with ONE production drops it straight under the assets folder, two ways: named
    # working folders (DSO: SCENES/ and ASSETS/) or a folder per film (Shama: Majlis, Saqarah,
    # Sadaf for Qabilah). Either way the assets folder IS the production folder. Anything else is
    # refused rather than guessed at: another production's folder must not be swept into this deck.
    if subs and any(_role(f.get("name")) for f in subs):
        return assets
    if subs and all(any(frame_of(g.get("name")) for g in drive.list_folder(f["id"], tok))
                    for f in subs):
        return assets
    return None


def sync(company: str, client: str, production: str) -> dict:
    """Pull the folder down, skipping anything whose Drive md5 we already hold.

    Returns {dir, items, scenes, guidance, script, pulled, folder}. Nothing is interpreted here:
    this only decides what each file IS."""
    co = store.get_company_by_slug(company)
    if not co:
        raise ValueError("unknown company %s" % company)
    tok = drive.access_token()
    fid = folder_id(co["id"], client, production, tok)
    if not fid:
        raise ValueError("no '%s/%s' folder under the %s client folder on Drive"
                         % (ASSETS_FOLDER, production, client))
    key = _key(company, client, production)
    work = os.path.join(ROOT, _slug(client), _slug(production))
    os.makedirs(os.path.join(work, "frames"), exist_ok=True)
    seen = dict(db.setting_get(key + ":md5") or {})

    def grab(f, sub=""):
        name = f.get("name") or ""
        dest = os.path.join(work, sub, re.sub(r"[^A-Za-z0-9._ ()-]", "_", name))
        md5 = f.get("md5Checksum") or ""
        if md5 and seen.get(name) == md5 and os.path.exists(dest):
            return _deck_copy(dest), False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(drive.download(f["id"], tok))
        if md5:
            seen[name] = md5
        return _deck_copy(dest, fresh=True), True

    items, scenes, films, guidance, script, pulled, cover = [], [], [], "", "", 0, ""

    def take(f):
        """What this FILE is: a labelled plate, a storyboard frame, the script, or guidance."""
        nonlocal guidance, script, pulled, cover
        name = f.get("name") or ""
        low = name.lower()
        if low.endswith(_IMAGE):
            kind, nm = parse_label(name)
            if _COVER.search(os.path.splitext(name)[0]):
                p, new = grab(f)                  # a plate labelled (Cover) IS the cover, not an item
                pulled += new
                cover = p
            elif kind:
                p, new = grab(f)
                pulled += new
                items.append({"kind": kind, "name": nm, "image": p, "file": name,
                              "order": order_of(name)})
            elif scene_of(name):
                p, new = grab(f, "frames")
                pulled += new
                scenes.append({"path": p, "scene": scene_of(name)})
        elif low.endswith(_DOC):
            p, new = grab(f)
            pulled += new
            text = _read(p)
            if _SCRIPT_NAME.search(re.sub(r"[^a-z0-9]+", " ", low)):
                script = text
            else:
                guidance += ("\n\n" if guidance else "") + text

    def take_film(f):
        """A subfolder of numbered frames IS a film: '07 - The weaver, in memory.jpg' -> MJ-07."""
        nonlocal pulled
        name = (f.get("name") or "").strip()
        film = {"name": name, "frames": [], "extras": []}
        for g in drive.list_folder(f["id"], tok):
            gn = g.get("name") or ""
            if not gn.lower().endswith(_IMAGE):
                continue
            p, new = grab(g, os.path.join("films", _slug(name)))
            pulled += new
            fr = frame_of(gn)
            (film["frames"].append({"n": fr[0], "caption": fr[1], "path": p}) if fr
             else film["extras"].append({"name": _plain(gn), "path": p}))
        if film["frames"]:
            film["frames"].sort(key=lambda x: x["n"])
            film["extras"].sort(key=lambda x: x["name"].lower())
            films.append(film)

    def walk(folder: str, depth: int = 0):
        for f in drive.list_folder(folder, tok):
            if f.get("mimeType") != "application/vnd.google-apps.folder":
                take(f)
            elif _role(f.get("name")) and depth < 2:
                walk(f["id"], depth + 1)    # SCENES / ASSETS: working folders, not films
            elif not _role(f.get("name")):
                take_film(f)

    walk(fid)
    db.setting_set(key + ":md5", seen)
    scenes.sort(key=lambda s: s["scene"])
    return {"dir": work, "folder": fid, "pulled": pulled, "cover": cover,
            "guidance": guidance.strip(),
            "script": script.strip(), "scenes": scenes,
            "films": film_codes(key, sorted(films, key=lambda x: x["name"].lower())),
            "items": assign_codes(key, items)}


def film_codes(key: str, films: list[dict]) -> list[dict]:
    """A frame's code is its film's prefix and its OWN number from the filename (MJ-07 is
    "07 - The weaver"), so a replaced frame keeps its code by construction. The prefix is pinned
    per film name, and extras (a model sheet beside the frames) are pinned by name as M1, M2..."""
    reg = dict(db.setting_get(key + ":films") or {})
    pre = dict(reg.get("prefix") or {})
    extra = dict(reg.get("extra") or {})
    for f in films:
        k = f["name"].lower()
        if k not in pre:
            pre[k] = film_prefix(f["name"], set(pre.values()))
        f["prefix"] = pre[k]
        for fr in f["frames"]:
            fr["code"] = "%s-%02d" % (pre[k], fr["n"])
        mine = extra.setdefault(k, {})
        for x in f["extras"]:
            if x["name"].lower() not in mine:
                mine[x["name"].lower()] = "%s-M%d" % (pre[k], len(mine) + 1)
            x["code"] = mine[x["name"].lower()]
    db.setting_set(key + ":films", {"prefix": pre, "extra": extra})
    return films


# A deck PAGE is 1280 px wide, so nothing in it is ever displayed wider than that: a plate sits
# 2 or 3 across (548 or 362 px) and a storyboard frame 3 across (368 px). 1920 was pure weight.
DECK_PX = 1280


def _deck_copy(path: str, fresh: bool = False, px: int = DECK_PX, quality: int = 84) -> str:
    """The picture the deck embeds: at most `px` wide, JPEG. The team drops 4K upscales and PNG
    masters into the folder (Shama 9 Nov 2026: 30 MB PNGs at 5504 px; DSO 12 Nov: a 139-megapixel
    plate), and embedding those as they are made a 93 MB deck the library refuses and no client can
    open. The original stays untouched beside it; documents are passed through as they are."""
    if not path.lower().endswith(_IMAGE):
        return path
    out = os.path.splitext(path)[0] + ".deck.jpg"
    if not fresh and os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        return out
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 400_000_000      # their masters are genuinely enormous
        im = Image.open(path).convert("RGB")
        if im.width > px:
            im = im.resize((px, round(im.height * px / im.width)), Image.LANCZOS)
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out
    except Exception:                                           # noqa: BLE001
        return path


def shrink(work: str, px: int, quality: int = 78) -> int:
    """Re-encode every picture already prepared for this deck smaller, in place.

    The last resort when a built deck is over the library cap: a deck that cannot be filed cannot
    be sent either, and the client's own mail server is stricter than we are. It re-encodes the
    deck COPIES, never the team's originals."""
    n = 0
    for root, _, files in os.walk(work):
        for f in files:
            if not f.endswith(".deck.jpg"):
                continue
            p = os.path.join(root, f)
            try:
                from PIL import Image
                im = Image.open(p).convert("RGB")
                if im.width > px:
                    im = im.resize((px, round(im.height * px / im.width)), Image.LANCZOS)
                im.save(p, "JPEG", quality=quality, optimize=True)
                n += 1
            except Exception:                                   # noqa: BLE001
                continue
    return n


def _read(path: str) -> str:
    from . import doctext
    ext = os.path.splitext(path)[1].lower()
    mime = {".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            }.get(ext, "text/plain")
    try:
        if ext == ".pdf":
            import subprocess
            r = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True,
                               text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        return doctext.extract(mime, os.path.basename(path), open(path, "rb").read())
    except Exception:                                           # noqa: BLE001
        return ""


# ----------------------------------------------------------------------------------- captions
def captions(key: str, items: list[dict], guidance: str, company: str,
             force: bool = False) -> dict:
    """One line per item, taken from Gino's guidance document and nothing else.

    Stored and reused: a rebuild lays the same words out again, because a layout change is not a
    reason to rewrite copy a client has already read. `force` re-asks, for when the guidance
    document itself has changed."""
    have = dict(db.setting_get(key + ":captions") or {})
    want = [i for i in items if force or not have.get(i["code"])]
    if not want or not guidance.strip():
        return have
    listing = "\n".join("%s  %s (%s)" % (i["code"], i["name"], TITLE[i["kind"]]) for i in want)
    system = (
        "You caption items on a CLIENT APPROVAL DECK for a film production. Each caption is one "
        "sentence saying what the item is and what it does in the film, so the client can decide "
        "whether to approve it.\n\n"
        "HARD RULES. Use ONLY the guidance document you are given. Never invent a detail, a name, "
        "a scene, a material or a number that is not in it. If the guidance does not cover an "
        "item, return an empty string for that item rather than writing something plausible. "
        "Under 24 words. No em dashes or en dashes: use commas or colons. No marketing language, "
        "no superlatives. Plain and specific.\n\n"
        "Return JSON: {\"captions\": {\"<CODE>\": \"<one sentence>\", ...}} and nothing else.")
    user = "GUIDANCE DOCUMENT\n%s\n\nITEMS TO CAPTION\n%s" % (guidance[:24000], listing)
    try:
        # prose, so Sonnet: Haiku is plumbing and this is copy a client reads
        out = provider.think_json(system, user, fast=True, purpose="approval-captions",
                                  company=company) or {}
    except Exception:                                           # noqa: BLE001
        out = {}
    for code, line in (out.get("captions") or {}).items():
        if isinstance(line, str) and line.strip():
            have[code] = re.sub(r"\s*[–—]\s*", ", ", line.strip())
    db.setting_set(key + ":captions", have)
    return have


# --------------------------------------------------------------------------------- storyboard
def scene_pages(key: str, synced: dict, company: str, force: bool = False) -> list[dict]:
    """A page per SCENE, with every frame that belongs to it.

    The title, the action and the voice-over are read OUT OF THE SCRIPT, never invented: the writer
    is given the script and the scene numbers and told to use nothing else. Stored like the
    captions, so a rebuild re-lays the same words. With no script the frames still lay out, with
    the scene number as the heading and no words, which is better than a guess."""
    frames: dict[int, list[str]] = {}
    for s in synced["scenes"]:
        frames.setdefault(s["scene"][0], []).append(s["path"])
    if not frames:
        return []
    have = dict(db.setting_get(key + ":scenes") or {})
    todo = [n for n in sorted(frames) if force or not have.get(str(n))]
    if todo and synced["script"].strip():
        system = (
            "You transcribe a SHOOTING SCRIPT onto a storyboard, one entry per scene.\n\n"
            "For each scene number you are given, return: a short title (under 5 words, taken "
            "from what happens), `action` (what happens on screen, 1 to 2 sentences), `vo` (the "
            "voice-over spoken on that scene, EXACTLY as written in the script, or empty if the "
            "scene is silent) and `tc` (its timecode if the script states one, else empty).\n\n"
            "HARD RULES. Use ONLY the script. Never invent an action, a line, a character or a "
            "timecode. `vo` is the words that are SPOKEN: never a stage direction, never a note "
            "about where a line lands. If the script does not cover a scene number, return empty "
            "strings for it. No em dashes or en dashes: use commas or colons.\n\n"
            "Return JSON: {\"scenes\": {\"<number>\": {\"title\": \"\", \"action\": \"\", "
            "\"vo\": \"\", \"tc\": \"\"}, ...}}")
        user = "SCRIPT\n%s\n\nSCENE NUMBERS\n%s" % (
            synced["script"][:24000], ", ".join(str(n) for n in todo))
        try:
            out = provider.think_json(system, user, fast=True, max_tokens=6000,
                                      purpose="approval-scenes", company=company) or {}
        except Exception:                                       # noqa: BLE001
            out = {}
        for n, v in (out.get("scenes") or {}).items():
            if isinstance(v, dict):
                have[str(int(n))] = {k: re.sub(r"\s*[–—]\s*", ", ", str(v.get(k) or ""))
                                     for k in ("title", "action", "vo", "tc")}
        db.setting_set(key + ":scenes", have)
    pages = []
    for n in sorted(frames):
        got = have.get(str(n)) or {}
        pages.append({"type": "scene", "number": "%02d" % n,
                      "title": got.get("title") or "Scene %02d" % n,
                      "tc": got.get("tc") or "", "frames": frames[n],
                      "action": got.get("action") or "",
                      "vo": got.get("vo") or "",
                      "note": "Silent on this scene." if not got.get("vo") and got else "",
                      "section": "Storyboard"})
    return pages


# -------------------------------------------------------------------------------------- build
def spec(company: str, client: str, production: str, synced: dict, caps: dict, *,
         accent: str = "#4DA3FF", label: str = "", scenes_copy: list | None = None,
         cover: str = "") -> dict:
    """Assemble the render_spec. Sections are laid out two across for the plate types (a
    turnaround or a three-view sheet has to be big enough to judge) and three across for
    locations, which read fine smaller."""
    by = {k: [i for i in synced["items"] if i["kind"] == k] for k, _, _ in SECTIONS}
    counts = [{"title": str(len(by[k])), "sub": TITLE[k].lower()} for k, _, _ in SECTIONS
              if by[k]]
    if synced["scenes"]:
        counts.append({"title": str(len({s["scene"][0] for s in synced["scenes"]})),
                       "sub": "scenes"})
    films = synced.get("films") or []
    counts += [{"title": str(len(f["frames"])), "sub": f["name"].lower() + " frames"}
               for f in films][:4]
    what = ", ".join(f["name"] for f in films)
    codes_line = ", ".join("%s frames are %s" % (f["name"], f["prefix"]) for f in films)
    pages = [
        {"type": "covermeta", "kicker": "CLIENT APPROVAL DECK", "title": production,
         "title2": "Everything to approve, in one place.",
         "standfirst": ((("Every frame from %s, in film order. " % what) if films else
                         "Characters, wardrobe, locations, props and the storyboard. ")
                        + "Each item carries a code. Approve the deck, or quote the code of "
                          "anything you want changed."),
         "meta": [{"k": "CLIENT", "v": client}, {"k": "PRODUCTION", "v": production},
                  {"k": "VERSION", "v": "Approval deck"},
                  {"k": "DATE", "v": _today()}],
         # THE COVER IS THEIRS TO CHOOSE (owner, 13 Nov 2026): the picture named on the card, else
         # a plate labelled (Cover) in the folder, else the FIRST item in their own numbering.
         # HBMSU Nurse put the senior colleague on the cover because CH-01 was whichever file Drive
         # happened to list first, and nothing could change it.
         "image": (cover or synced.get("cover")
                   or (by.get("character") or synced["items"] or [{}])[0].get("image")
                   or (films[0]["frames"][-1]["path"] if films else None)),
         "section": "Approval"},
        {"type": "split", "kicker": "HOW THIS WORKS", "heading": "One code, one *decision*",
         "blocks": [
             {"title": "Everything has a code",
              "body": (((codes_line + ", numbered as the films run. ") if films else
                        "Characters are CH, wardrobe WD, locations LOC, props PR and scenes SC. ")
                       + "Quote the code and we both know exactly what is being discussed.")},
             {"title": "Approve the deck, or name the exceptions",
              "body": ("The fastest route is to approve everything and list what you want "
                       "changed. Anything not named is taken as approved.")}],
         "panel": {"title": "What is in here", "cells": counts,
                   "foot": {"title": "Sign-off",
                            "body": "The last page carries the signature block."}},
         "section": "How this works"},
    ]
    for kind, _, title in SECTIONS:
        got = by.get(kind) or []
        if not got:
            continue
        cols = 3 if kind == "location" else 2
        for i, grp in enumerate([got[j:j + cols] for j in range(0, len(got), cols)]):
            pages.append({
                "type": "approval", "kicker": title.upper(),
                "heading": _heading(kind) if not i else "%s, continued" % title,
                "intro": _intro(kind, len(got)) if not i else "",
                "cols": cols, "section": title,
                "items": [{"code": x["code"], "name": x["name"], "image": x["image"],
                           "body": caps.get(x["code"], "")} for x in grp]})

    for film in films:
        fr = film["frames"]
        for k in range(0, len(fr), 3):
            pages.append({
                "type": "approval", "kicker": film["name"].upper(),
                "heading": ("%s, *frame by frame*" % film["name"]) if not k
                else "%s, continued" % film["name"],
                "intro": ("%d frames in film order." % len(fr)) if not k else "",
                "cols": 3, "section": film["name"],
                "items": [{"code": x["code"], "name": x["caption"], "image": x["path"],
                           "body": ""} for x in fr[k:k + 3]]})
        ex = film["extras"]
        for k in range(0, len(ex), 3):
            pages.append({
                "type": "approval", "kicker": film["name"].upper(),
                "heading": ("%s, *alongside the frames*" % film["name"]) if not k
                else "%s, alongside, continued" % film["name"],
                "intro": ("Also in the %s folder." % film["name"]) if not k else "",
                "cols": 3, "section": film["name"],
                "items": [{"code": x["code"], "name": x["name"], "image": x["path"],
                           "body": ""} for x in ex[k:k + 3]]})

    for pg in (scenes_copy or []):
        pages.append(pg)

    groups = [{"label": f["name"], "codes": "%s to %s%s" % (
        f["frames"][0]["code"], f["frames"][-1]["code"],
        (", plus %s" % _range([{"code": x["code"]} for x in f["extras"]])) if f["extras"] else "")}
        for f in films] + [{"label": TITLE[k], "codes": _range(by[k])} for k, _, _ in SECTIONS if by[k]]
    if synced["scenes"]:
        ns = sorted({s["scene"][0] for s in synced["scenes"]})
        groups.append({"label": "Storyboard", "codes": "Scenes %02d to %02d" % (ns[0], ns[-1])})
    pages.append({
        "type": "signoff", "kicker": "SIGN-OFF", "heading": "Approved to *go into production*",
        "intro": ("Tick each section you are happy with. For anything you want changed, write "
                  "the code and what you want instead, and we will reissue the affected pages "
                  "only."),
        "groups": groups, "light": True, "section": "Sign-off",
        "note": ("Approval covers the look of each item as shown. It does not cover the final "
                 "grade, on-screen captions or music, which are approved at first cut."),
        "contact": {"line": "Return to rashad@sensa.digital. Sensa Productions, Dubai."}})
    return {"accent": accent, "customer": client, "chunky": True,
            "label": label or ("%s approval deck" % production), "pages": pages}


def _heading(kind: str) -> str:
    return {"character": "Who is *in the film*", "wardrobe": "What they *wear*",
            "location": "Where it *happens*", "prop": "What they *touch*"}[kind]


def _intro(kind: str, n: int) -> str:
    return {"character": "Each plate is a turnaround: front, back and frontal close-up.",
            "wardrobe": "Front and back of each costume. Nobody changes during the film.",
            "location": "%d settings." % n,
            "prop": "%d built elements, each shown from several angles." % n}[kind]


def _range(items: list[dict]) -> str:
    cs = sorted(i["code"] for i in items)
    return cs[0] if len(cs) == 1 else "%s to %s" % (cs[0], cs[-1])


def cover_image(synced: dict, want: str = "") -> tuple[str, str]:
    """(path, why) for the cover. `want` is a CODE the team named ("CH-05") or part of an item's
    name ("the nurse"), matched against the items this build actually holds. Named and not found is
    said out loud, never silently ignored: the cover is the first thing the client sees."""
    items = synced.get("items") or []
    w = re.sub(r"\s+", " ", (want or "").strip()).lower()
    if w:
        hit = next((i for i in items if i["code"].lower() == w), None) or next(
            (i for i in items if w in i["name"].lower()), None)
        if hit:
            return hit["image"], "%s %s, as asked" % (hit["code"], hit["name"])
        return "", "%r is not an item in this deck, so the cover was left as it was" % want
    if synced.get("cover"):
        return synced["cover"], "the plate labelled (Cover) in the folder"
    first = next((i for i in items if i["kind"] == "character"), None) or (items or [{}])[0]
    if first.get("image"):
        return first["image"], "%s %s, the first item in your numbering" % (
            first.get("code", ""), first.get("name", ""))
    return "", "the last storyboard frame"


def build(company: str, client: str, production: str, *, accent: str = "#4DA3FF",
          force_captions: bool = False, out_dir: str | None = None, cover: str = "") -> dict:
    """Sync, caption, lay the storyboard out, render.

    Returns {path, pages, items, pulled, missing_captions, scenes}."""
    synced = sync(company, client, production)
    if not synced["items"] and not synced["scenes"] and not synced["films"]:
        raise ValueError("the %s folder holds nothing labelled Character, Wardrobe, Location, "
                         "Prop or Scene, and no folder of numbered frames" % production)
    key = _key(company, client, production)
    # THE CAPTIONS ARE GROUNDED IN A REAL DOCUMENT THE TEAM WROTE, and the card says WHICH. With no
    # guidance document the script stands in rather than the items going out bare, but it is never
    # the model's own words either way (12 Nov 2026: DSO has a voice-over script and no guidance).
    source = "guidance document" if synced["guidance"] else (
        "voice-over script, because the folder holds no guidance document" if synced["script"]
        else "")
    caps = captions(key, synced["items"], synced["guidance"] or synced["script"], company,
                    force=force_captions)
    scenes_copy = scene_pages(key, synced, company, force=force_captions)
    out = out_dir or synced["dir"]
    os.makedirs(out, exist_ok=True)
    pick, cover_why = cover_image(synced, cover)
    sp = spec(company, client, production, synced, caps, accent=accent, scenes_copy=scenes_copy,
              cover=pick)
    name = "%s - %s - Client approval deck.pdf" % (client, production)
    res = deck.render_spec(company, sp, out_dir=out, filename=name)
    # IT HAS TO FIT, AND THAT IS NOT THE LIBRARY'S PROBLEM (12 Nov 2026): the first DSO build came
    # out at 15.9 MB against a 15 MB cap and the whole run was thrown away at the last step. A deck
    # that cannot be filed cannot be sent either, so it is re-encoded smaller until it fits.
    for px in (1024, 820, 660, 520):
        if os.path.getsize(res["path"]) <= documents.MAX_BYTES:
            break
        shrink(out, px)
        res = deck.render_spec(company, sp, out_dir=out, filename=name)
    missing = [i["code"] for i in synced["items"] if not caps.get(i["code"])]
    return {**res, "items": synced["items"], "pulled": synced["pulled"], "filename": name,
            "bytes": os.path.getsize(res["path"]), "cover": cover_why,
            "missing_captions": missing, "caption_source": source,
            "scenes": len({s["scene"][0] for s in synced["scenes"]}),
            "films": {f["name"]: {"frames": len(f["frames"]), "extras": len(f["extras"])}
                      for f in synced["films"]}}


# ------------------------------------------------------------- the card: build, file, revise
def start(company: str, deal_id: int, production: str, *, client: str = "", accent: str = "#4DA3FF",
          rewrite_captions: bool = False, background: bool = True, cover: str = "") -> dict:
    """Raise the review card, then build on a thread. Everything a PERSON must supply is checked
    first, so a bad call fails in the conversation rather than on a card minutes later."""
    from . import engine
    co = store.get_company_by_slug(company)
    if not co:
        raise ValueError("unknown company %s" % company)
    deal = db.one("select id, title from crm_projects where id=%s", (int(deal_id),))
    if not deal:
        raise ValueError("no opportunity #%s" % deal_id)
    client = (client or engine._deal_client_name(int(deal_id)) or "").split(",")[0].strip()
    if not client:
        raise ValueError("opportunity #%s has no client name, so there is no Drive folder to read; "
                         "name the client" % deal_id)
    production = (production or deal["title"] or "").strip()
    if not production:
        raise ValueError("name the production: it titles the deck and finds the folder")
    tok = drive.access_token()
    if not folder_id(co["id"], client, production, tok):
        raise ValueError("no approval folder for %s on Drive. Expected '%s' inside the %s client "
                         "folder, holding the labelled plates (a trailing Character, Wardrobe, "
                         "Location or Prop), the storyboard frames named by scene, and the "
                         "guidance document" % (production, ASSETS_FOLDER, client))
    skill = store.get_skill_by_key(co["id"], "sales-proposal")
    req = {"kind": "approval_deck", "company": company, "client": client,
           "production": production, "accent": accent, "deal_id": int(deal_id),
           **({"cover": cover} if cover else {})}
    task = db.execute("insert into tasks (company_id, skill_id, kind, request, draft, status, "
                      "title, deal_id) values (%s,%s,'content',%s,%s,'drafting',%s,%s) returning *",
                      (co["id"], skill["id"], Json(req),
                       "Building the %s client approval deck from the Drive folder: the plates, "
                       "the storyboard and the sign-off sheet. A few minutes; the card returns to "
                       "your Inbox when it is ready." % production,
                       "Approval deck: %s - %s" % (client, production), int(deal_id)))
    if background:
        threading.Thread(target=run, args=(task["id"], rewrite_captions), daemon=True).start()
    else:
        run(task["id"], rewrite_captions)
    return {"task_id": task["id"], "client": client, "production": production}


def run(task_id: int, rewrite_captions: bool = False) -> None:
    """Build, file on the deal and the client's Drive folder, hand the card back for approval.
    A failure leaves a VISIBLE card saying what stopped, never a silent failed row."""
    from . import engine, pipeline
    task = store.get_task(task_id)
    s = dict((task or {}).get("request") or {})
    try:
        res = build(s["company"], s["client"], s["production"], accent=s.get("accent") or "#4DA3FF",
                    force_captions=bool(rewrite_captions), cover=s.get("cover") or "")
        co = store.get_company_by_slug(s["company"])
        old = [r["id"] for r in db.query(
            "select id from company_documents where kind='approval-deck' and superseded_by is null "
            "and filename=%s and deal_id=%s", (res["filename"], s["deal_id"]))]
        doc, folder = engine._file_proposal_pdf(co, s["client"], res["filename"], res["path"],
                                                s["deal_id"], None, supersede=old,
                                                archive_prefix=os.path.splitext(res["filename"])[0],
                                                kind="approval-deck")
        pipeline.log_deal(s["deal_id"], "approval_deck",
                          "Client approval deck filed: %s (%d pages)%s"
                          % (res["filename"], res.get("pages") or 0,
                             (" (client folder %s)" % folder) if folder else ""))
        s.update({"file": res["path"], "attach_docs": [documents.card_ref(doc)]})
        store.update_task(task_id, request=s, draft=summary(res), status="awaiting_approval",
                          title="Approval deck: %s - %s" % (s["client"], s["production"]))
    except Exception as e:  # noqa: BLE001 - a visible card, never a silent failure
        from . import notifications
        store.update_task(task_id, status="awaiting_correction",
                          draft=("The approval deck stopped: %s: %s.\n\nReply 'rebuild' to try "
                                 "again once the folder is right." % (type(e).__name__, e)))
        notifications.notify("Card #%s: the approval deck stopped" % task_id, str(e)[:300],
                             category="approval", company_id=(task or {}).get("company_id"),
                             target_type="task", target_id=str(task_id))
        raise


def summary(res: dict) -> str:
    """What the card says. Every number is counted from what was actually built."""
    by = {}
    for i in res.get("items") or []:
        by.setdefault(i["kind"], []).append(i)
    counted = ([("%d %s" % (len(by[k]), TITLE[k].lower())) for k, _, _ in SECTIONS if by.get(k)]
               + (["%d scenes" % res["scenes"]] if res.get("scenes") else [])
               + ["%d %s frames" % (v["frames"], k.lower())
                  for k, v in (res.get("films") or {}).items()])
    lines = ["%d pages, %.1f MB. %s." % (res.get("pages") or 0,
                                        (res.get("bytes") or 0) / 1e6,
                                        ", ".join(counted) or "nothing counted")]
    for k, _, title in SECTIONS:
        if by.get(k):
            lines.append("%s: %s" % (title, ", ".join("%s %s" % (i["code"], i["name"])
                                                      for i in by[k])))
    if res.get("missing_captions"):
        lines.append("NO CAPTION, because the guidance document does not cover them: %s. They "
                     "print with the code and the name only." % ", ".join(res["missing_captions"]))
    if res.get("caption_source"):
        lines.append("Captions come from the %s. Nothing in them is written by Cortex."
                     % res["caption_source"])
    else:
        lines.append("The folder holds no guidance document and no script, so no item carries a "
                     "caption: each prints with its code and name. Drop a guidance document in "
                     "and reply 'rewrite the captions'.")
    if res.get("cover"):
        lines.append("Cover: %s. To change it, reply 'cover CH-02' (any code in this deck), or put "
                     "a picture labelled (Cover) in the folder and reply 'rebuild'."
                     % res["cover"])
    lines.append("Reply on this card: 'rebuild' after changing a picture in the folder, or "
                 "'rewrite the captions' after changing the guidance document. A code stays with "
                 "its item, so a replaced plate keeps the code the client has written on.")
    return "\n\n".join(lines)


_REBUILD = re.compile(r"\b(rebuild|rebuilt|re-?run|again|refresh|updated?|new (?:image|picture|"
                      r"plate|frame)s?|changed|replaced|resume|retry)\b", re.I)
_RECAPTION = re.compile(r"\b(caption|captions|wording|guidance|copy|text)\b", re.I)


_COVER_ASK = re.compile(r"\bcover\b", re.I)
_CODE = re.compile(r"\b((?:CH|WD|LOC|PR|[A-Z]{2})-\d{1,2}|[A-Z]{2}-M\d)\b")


def cover_from_words(text: str) -> str:
    """What they want on the cover, from a reply that mentions one: a CODE if they gave one, else
    the words after "cover", which `cover_image` matches against the item names."""
    if not _COVER_ASK.search(text or ""):
        return ""
    m = _CODE.search((text or "").upper())
    if m:
        return m.group(1)
    m = re.search(r"\bcover\b\s*(?:to|:|=|should be|is)?\s*(?:the\s+)?([\w' -]{2,40})",
                  text or "", re.I)
    return re.sub(r"\s+", " ", (m.group(1) if m else "")).strip(" .,'\"") if m else ""


def revise(task: dict, text: str) -> bool:
    """A reply on an approval-deck card. The deck's words are the team's own (the guidance
    document and the script), so a reply never rewrites copy here: it rebuilds from the folder,
    optionally re-reading the guidance. Anything else is answered plainly, never guessed at."""
    req = dict(task.get("request") or {})
    if req.get("kind") != "approval_deck":
        return False
    recap = bool(_RECAPTION.search(text or ""))
    want = cover_from_words(text or "")
    if want:                       # the cover is theirs to choose, and it is remembered
        req["cover"] = want
        store.update_task(task["id"], request=req)
    if not (want or recap or _REBUILD.search(text or "")):
        store.update_task(task["id"], status="awaiting_correction",
                          draft=((task.get("draft") or "") + "\n\n---\nYour note: \"%s\"\n"
                                 "This deck carries the team's own words, from the guidance "
                                 "document and the script in the Drive folder, so it is not "
                                 "rewritten here. Change the file in the folder and reply "
                                 "'rebuild', reply 'rewrite the captions' to re-read the "
                                 "guidance, or 'cover CH-02' to change the cover."
                                 % (text or "").strip()[:300]))
        return True
    store.update_task(task["id"], status="drafting",
                      draft=("Rebuilding the %s approval deck from the Drive folder%s%s."
                             % (req.get("production"),
                                ", re-reading the guidance" if recap else "",
                                (", cover %s" % want) if want else "")))
    threading.Thread(target=run, args=(task["id"], recap), daemon=True).start()
    return True
