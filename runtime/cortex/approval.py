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
from datetime import datetime, timezone

from . import db, deck, documents, drive, profile, provider, store

ROOT = "/opt/coretex/creative/_approval"
ASSETS_FOLDER = "APPROVAL ASSETS"
STORYBOARD_FOLDER = "STORYBOARD"

SECTIONS = [("character", "CH", "Characters"), ("wardrobe", "WD", "Wardrobe"),
            ("location", "LOC", "Locations"), ("prop", "PR", "Props")]
PREFIX = {k: p for k, p, _ in SECTIONS}
TITLE = {k: t for k, _, t in SECTIONS}

_IMAGE = (".png", ".jpg", ".jpeg", ".webp")
_DOC = (".pdf", ".docx", ".txt", ".md", ".rtf")
# a trailing section word, with or without brackets, and tolerant of the stray unclosed one
_LABEL = re.compile(r"[\s(\[-]*\b(character|wardrobe|location|prop|props)\b\s*[)\]]*\s*$", re.I)
_SCENE = re.compile(r"scene\s*0*(\d+)(?:\s*[-_]\s*0*(\d+))?", re.I)
# a numbered frame, "07 - The weaver, in memory.jpg"; Drive's "Copy of " and a doubled
# extension ("....jpg.png") are how hand-copied files actually arrive
_FRAME = re.compile(r"^\s*(?:copy of\s+)*0*(\d{1,3})\s*[-_.]\s*(.+?)\s*$", re.I)
_EXT = re.compile(r"(\.(?:png|jpe?g|webp))+$", re.I)


# --------------------------------------------------------------------------- labels and codes
def parse_label(filename: str) -> tuple[str | None, str]:
    """('character', 'Daughter') from 'Daughter (Character).png'. (None, stem) when unlabelled."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    m = _LABEL.search(stem)
    if not m:
        return None, re.sub(r"\s{2,}", " ", stem).strip()
    kind = m.group(1).lower().rstrip("s") if m.group(1).lower() != "props" else "prop"
    name = re.sub(r"\s{2,}", " ", stem[:m.start()]).strip(" -(["
                                                           )
    return kind, name or stem


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
    reg = dict(db.setting_get(key + ":codes") or {})
    used = {k: {v for n, v in reg.items() if n.startswith(k + "/")} for k in PREFIX}
    out = []
    for it in items:
        kind = it["kind"]
        slot = "%s/%s" % (kind, it["name"].lower())
        code = reg.get(slot)
        if not code:
            taken = {int(c.split("-")[1]) for c in used.get(kind, set()) if "-" in c}
            n = next(i for i in range(1, 100) if i not in taken)
            code = "%s-%02d" % (PREFIX[kind], n)
            reg[slot] = code
            used.setdefault(kind, set()).add(code)
        out.append({**it, "code": code})
    db.setting_set(key + ":codes", reg)
    return sorted(out, key=lambda x: (x["kind"], x["code"]))


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
    # A client with ONE production often drops its films straight under the assets folder
    # (Shama: Approval Asset/Majlis, /Saqarah, /Sadaf for Qabilah). Take the assets folder as the
    # production when every subfolder in it holds numbered frames, never otherwise: another
    # production's folder must not be swept into this deck.
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

    items, scenes, films, guidance, script, pulled = [], [], [], "", "", 0
    for f in drive.list_folder(fid, tok):
        name = f.get("name") or ""
        low = name.lower()
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            if _slug(name) == _slug(STORYBOARD_FOLDER):
                for g in drive.list_folder(f["id"], tok):
                    if (g.get("name") or "").lower().endswith(_IMAGE) and scene_of(g["name"]):
                        p, new = grab(g, "frames")
                        pulled += new
                        scenes.append({"path": p, "scene": scene_of(g["name"])})
            else:
                film = {"name": name.strip(), "frames": [], "extras": []}
                for g in drive.list_folder(f["id"], tok):
                    gn = g.get("name") or ""
                    if not gn.lower().endswith(_IMAGE):
                        continue
                    p, new = grab(g, os.path.join("films", _slug(name)))
                    pulled += new
                    fr = frame_of(gn)
                    if fr:
                        film["frames"].append({"n": fr[0], "caption": fr[1], "path": p})
                    else:
                        film["extras"].append({"name": _plain(gn), "path": p})
                if film["frames"]:
                    film["frames"].sort(key=lambda x: x["n"])
                    film["extras"].sort(key=lambda x: x["name"].lower())
                    films.append(film)
            continue
        if low.endswith(_IMAGE):
            kind, nm = parse_label(name)
            if kind:
                p, new = grab(f)
                pulled += new
                items.append({"kind": kind, "name": nm, "image": p, "file": name})
            elif scene_of(name):
                p, new = grab(f, "frames")
                pulled += new
                scenes.append({"path": p, "scene": scene_of(name)})
        elif low.endswith(_DOC):
            p, new = grab(f)
            pulled += new
            text = _read(p)
            if re.search(r"script|voice.?over|\bvo\b", low):
                script = text
            else:
                guidance += ("\n\n" if guidance else "") + text
    db.setting_set(key + ":md5", seen)
    scenes.sort(key=lambda s: s["scene"])
    return {"dir": work, "folder": fid, "pulled": pulled, "guidance": guidance.strip(),
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


DECK_PX = 1920


def _deck_copy(path: str, fresh: bool = False) -> str:
    """The picture the deck embeds: at most DECK_PX wide, JPEG. The team drops 4K upscales and
    PNG masters into the folder (Shama 9 Nov 2026: 30 MB PNGs at 5504 px), and embedding those
    as they are made a 93 MB deck the library refuses and no client can open. The original stays
    untouched beside it; documents are passed through as they are."""
    if not path.lower().endswith(_IMAGE):
        return path
    out = os.path.splitext(path)[0] + ".deck.jpg"
    if not fresh and os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        return out
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        if im.width > DECK_PX:
            im = im.resize((DECK_PX, round(im.height * DECK_PX / im.width)), Image.LANCZOS)
        im.save(out, "JPEG", quality=86, optimize=True)
        return out
    except Exception:                                           # noqa: BLE001
        return path


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
         accent: str = "#4DA3FF", label: str = "", scenes_copy: list | None = None) -> dict:
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
         "image": ((by.get("character") or synced["items"] or [{}])[0].get("image")
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


def build(company: str, client: str, production: str, *, accent: str = "#4DA3FF",
          force_captions: bool = False, out_dir: str | None = None) -> dict:
    """Sync, caption, lay the storyboard out, render.

    Returns {path, pages, items, pulled, missing_captions, scenes}."""
    synced = sync(company, client, production)
    if not synced["items"] and not synced["scenes"] and not synced["films"]:
        raise ValueError("the %s folder holds nothing labelled Character, Wardrobe, Location, "
                         "Prop or Scene, and no folder of numbered frames" % production)
    key = _key(company, client, production)
    caps = captions(key, synced["items"], synced["guidance"], company, force=force_captions)
    scenes_copy = scene_pages(key, synced, company, force=force_captions)
    out = out_dir or synced["dir"]
    os.makedirs(out, exist_ok=True)
    sp = spec(company, client, production, synced, caps, accent=accent, scenes_copy=scenes_copy)
    name = "%s - %s - Client approval deck.pdf" % (client, production)
    res = deck.render_spec(company, sp, out_dir=out, filename=name)
    missing = [i["code"] for i in synced["items"] if not caps.get(i["code"])]
    return {**res, "items": synced["items"], "pulled": synced["pulled"], "filename": name,
            "missing_captions": missing, "guidance": bool(synced["guidance"]),
            "scenes": len({s["scene"][0] for s in synced["scenes"]}),
            "films": {f["name"]: {"frames": len(f["frames"]), "extras": len(f["extras"])}
                      for f in synced["films"]}}
