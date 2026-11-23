# -*- coding: utf-8 -*-
"""Every deck PDF gets its Google Slides twin (owner, 23 Nov 2026: "all of our PDFs should have an accompanying
Google Slides presentation to go with them").

A twin is the PDF page for page: each slide is the page render as its background, every film tile becomes a
playable YouTube video sitting exactly on the tile, and every other link in the PDF becomes an invisible link box.
It is filed BESIDE its PDF (the same Drive folder: a client folder for client work, Documents for our own) and
shared "anyone with the link can view", because a client passes it round their team and presents from it.

How a film tile is laid (learned building the 22 Oct capabilities twin): Slides stretches YouTube's 4:3
letterboxed poster over the video box and plays the film width-fit inside it, so a 4:3 box shows the film in its
middle 16:9 band. The box is sized so that band COVERS the tile, and crops of the page render are laid over
whatever of the box reaches outside the tile, so the slide looks exactly like the page. Tiles go top to bottom,
left to right, so any overlap lands on a neighbour's caption band. Video outlines are off (they draw a hairline).

A film YouTube has not finished processing has no poster and will not play: it stays the page's own picture
with a link box to the film, never an empty video frame (the HBMSU nurse film, 23 Nov 2026).

`twin(doc_id)` builds it and records `slides_id` / `slides_url` on the library row; `start_twin(doc_id)` does
that on a thread, so filing a deck never waits on it. Fail-soft: a twin that cannot be built never stops a card.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import threading

import httpx

from . import db, drive

RW, RH = 1920, 1080                       # the render every layout is measured in
EMU_W, EMU_H = 9144000, 5143500           # a 16:9 Slides page
WORK = "/var/tmp/cortex-slides"           # real disk: never /tmp (a tmpfs, see the backup fault of Oct 2026)
_YT = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})")
SLIDES_SUFFIX = " (Google Slides)"


# ---------- layout: the PDF read into slides (pure, no Google calls) ----------

def pdf_links(pdf_path: str) -> tuple[float, float, list[list[dict]]]:
    """(page width, page height, per page [{url, rect}]) with rects in render pixels, top-left origin."""
    from pypdf import PdfReader
    rd = PdfReader(pdf_path)
    mb = rd.pages[0].mediabox
    pw, ph = float(mb.width), float(mb.height)
    out = []
    for page in rd.pages:
        m = page.mediabox
        left, bottom = float(m.left), float(m.bottom)
        s = RW / float(m.width)
        links = []
        for a in page.get("/Annots") or []:
            a = a.get_object()
            if a.get("/Subtype") != "/Link":
                continue
            uri = ((a.get("/A") or {}).get("/URI")) or ""
            if not uri:
                continue
            x0, y0, x1, y1 = [float(v) for v in a.get("/Rect")]
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            top = float(m.height) - (y1 - bottom)
            links.append({"url": str(uri), "rect": [(x0 - left) * s, top * s, (x1 - left) * s,
                                                     (top + (y1 - y0)) * s]})
        out.append(links)
    return pw, ph, out


def plan_page(links: list[dict], playable=lambda vid: True, mode: str = "caps") -> list[dict]:
    """The element plan for one page, in z-order, from its links. Films -> video (+ masks to cut from the render),
    caption band over a full-page film; a film that cannot play yet and every other link -> a link box.
    Mask and band entries carry a `crop` rect (render pixels) for the builder to cut from the page render."""
    tiles, others = {}, []
    for l in links:
        m = _YT.search(l["url"])
        if not m:
            others.append({"type": "link", "url": l["url"], "rect": l["rect"]})
            continue
        x0, y0, x1, y1 = l["rect"]
        a = (x1 - x0) * (y1 - y0)
        if a > tiles.get(m.group(1), (0, None))[0]:          # one tile per film: its biggest link box
            tiles[m.group(1)] = (a, l["rect"])
    order = sorted(tiles.items(), key=lambda kv: (round(kv[1][1][1] / 20), kv[1][1][0]))
    els, bands = [], []
    for vid, (_, (x0, y0, x1, y1)) in order:
        url = f"https://www.youtube.com/watch?v={vid}"
        full = (x1 - x0) > RW * 0.99 and (y1 - y0) > RH * 0.99
        if not playable(vid):
            others.append({"type": "link", "url": url, "rect": [x0, y0, x1, y1]})
            continue
        if mode == "caps" and not full:
            y1 = y0 + (x1 - x0) * 9 / 16                   # the thumbnail only; its caption sits under it
        tw, th = x1 - x0, y1 - y0
        cw = max(tw, th * 16 / 9)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        vb = [cx - cw / 2, cy - cw * 0.375, cx + cw / 2, cy + cw * 0.375]
        els.append({"type": "video", "id": vid, "rect": vb})
        bx0, by0, bx1, by1 = max(vb[0], 0), max(vb[1], 0), min(vb[2], RW), min(vb[3], RH)
        for a0, b0, a1, b1 in [(bx0, by0, bx1, y0), (bx0, y1, bx1, by1), (bx0, y0, x0, y1), (x1, y0, bx1, y1)]:
            if a1 - a0 < 2 or b1 - b0 < 2:
                continue
            r = [max(int(a0) - 2, 0), max(int(b0) - 2, 0), min(int(round(a1)) + 2, RW), min(int(round(b1)) + 2, RH)]
            els.append({"type": "mask", "crop": r, "rect": r})
        if full:                                             # a full-page film: its title band back on top
            sy = int(RH * 0.605)
            bands.append({"type": "band", "crop": [0, sy, RW, RH], "rect": [0, sy, RW, RH], "fade": True})
    return els + bands + others


def _playable_check():
    seen = {}

    def ok(vid: str) -> bool:
        if vid not in seen:
            try:
                r = httpx.get(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", timeout=15)
                seen[vid] = r.status_code == 200 and len(r.content) > 2000
            except Exception:  # noqa: BLE001 — unreachable: assume it plays, as every finished film does
                seen[vid] = True
        return seen[vid]
    return ok


def layout(pdf_path: str, work: str, mode: str = "caps") -> dict:
    """Render every page and cut its images; returns {slides: [{bg, elements}]} with image files in `work`."""
    from PIL import Image
    os.makedirs(work, exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-scale-to-x", str(RW), "-scale-to-y", str(RH), pdf_path,
                    os.path.join(work, "p")], check=True, timeout=600)
    renders = sorted(glob.glob(os.path.join(work, "p-*.png")))
    _, _, pages = pdf_links(pdf_path)
    ok = _playable_check()
    slides = []
    for i, (png, links) in enumerate(zip(renders, pages), 1):
        render = Image.open(png).convert("RGB")
        els = []
        for n, e in enumerate(plan_page(links, ok, mode)):
            if e["type"] in ("mask", "band"):
                im = render.crop(tuple(e["crop"]))
                if e.get("fade"):
                    im = im.convert("RGBA")
                    ramp = int(RH * 0.025)
                    al = Image.new("L", im.size, 255)
                    for yy in range(ramp):
                        al.paste(int(255 * yy / ramp), (0, yy, im.size[0], yy + 1))
                    im.putalpha(al)
                f = f"e{i:02d}_{n:02d}.png"
                im.save(os.path.join(work, f))
                els.append({"type": "image", "file": f, "rect": e["rect"]})
            else:
                els.append(e)
        slides.append({"page": i, "bg": os.path.basename(png), "elements": els})
    return {"slides": slides}


# ---------- build: the layout into a Google Slides file ----------

class _Api:
    def __init__(self):
        self.tok = drive.access_token()

    def __call__(self, method, url, body=None, raw=None, ctype="application/json"):
        h = {"Authorization": f"Bearer {self.tok}", "Content-Type": ctype}
        r = httpx.request(method, url, headers=h, timeout=120,
                          content=raw if raw is not None else (json.dumps(body).encode() if body is not None else None))
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url[:90]} -> {r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}


def _rect(sid, r):
    sx, sy = EMU_W / RW, EMU_H / RH
    x0, y0, x1, y1 = r
    return {"pageObjectId": sid,
            "size": {"width": {"magnitude": int((x1 - x0) * sx), "unit": "EMU"},
                     "height": {"magnitude": int((y1 - y0) * sy), "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": int(x0 * sx), "translateY": int(y0 * sy),
                          "unit": "EMU"}}


def requests_for(L: dict, url_of) -> list[dict]:
    """The Slides batchUpdate requests for a layout (pure). `url_of(file)` gives each image's fetchable URL."""
    reqs = []
    for k, sl in enumerate(L["slides"]):
        sid = f"slide{sl['page']:02d}"
        reqs.append({"createSlide": {"objectId": sid, "insertionIndex": k,
                                     "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        reqs.append({"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill", "pageProperties": {
            "pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": url_of(sl["bg"])}}}}})
        for n, e in enumerate(sl["elements"]):
            oid = f"{sid}e{n:03d}"
            if e["type"] == "video":
                reqs.append({"createVideo": {"objectId": oid, "source": "YOUTUBE", "id": e["id"],
                                             "elementProperties": _rect(sid, e["rect"])}})
                reqs.append({"updateVideoProperties": {"objectId": oid,
                             "fields": "autoPlay,mute,outline.propertyState",
                             "videoProperties": {"autoPlay": False, "mute": False,
                                                 "outline": {"propertyState": "NOT_RENDERED"}}}})
            elif e["type"] == "image":
                reqs.append({"createImage": {"objectId": oid, "url": url_of(e["file"]),
                                             "elementProperties": _rect(sid, e["rect"])}})
            elif e["type"] == "link":
                reqs.append({"createShape": {"objectId": oid, "shapeType": "RECTANGLE",
                                             "elementProperties": _rect(sid, e["rect"])}})
                reqs.append({"updateShapeProperties": {"objectId": oid,
                             "fields": "shapeBackgroundFill.propertyState,outline.propertyState,link",
                             "shapeProperties": {"shapeBackgroundFill": {"propertyState": "NOT_RENDERED"},
                                                 "outline": {"propertyState": "NOT_RENDERED"},
                                                 "link": {"url": e["url"]}}}})
    return reqs


def build(L: dict, work: str, title: str, folder: str) -> dict:
    """Create the presentation in `folder` (an earlier copy with the same title there is trashed: it is the same
    document regenerated), share it anyone-with-link, return {id, url}."""
    api = _Api()
    D = "https://www.googleapis.com/drive/v3/files"
    q = f"trashed=false and name = '{title.replace(chr(39), chr(92) + chr(39))}' and '{folder}' in parents"
    for f in api("GET", D + "?" + str(httpx.QueryParams({"q": q, "supportsAllDrives": "true",
                                                           "includeItemsFromAllDrives": "true",
                                                           "fields": "files(id)"})))["files"]:
        api("PATCH", f"{D}/{f['id']}?supportsAllDrives=true", {"trashed": True})
    pres = api("POST", f"{D}?supportsAllDrives=true&fields=id",
               {"name": title, "mimeType": "application/vnd.google-apps.presentation", "parents": [folder]})
    pid = pres["id"]
    p = api("GET", f"https://slides.googleapis.com/v1/presentations/{pid}")
    files = sorted({sl["bg"] for sl in L["slides"]} |
                   {e["file"] for sl in L["slides"] for e in sl["elements"] if e["type"] == "image"})
    tmp = {}
    try:
        for f in files:          # Slides fetches images by URL: temporary public copies, trashed afterwards
            bnd = "slidesbnd"
            body = (f"--{bnd}\r\nContent-Type: application/json\r\n\r\n"
                    f"{json.dumps({'name': '_slides_tmp_' + f})}\r\n--{bnd}\r\nContent-Type: image/png\r\n\r\n"
                    ).encode() + open(os.path.join(work, f), "rb").read() + f"\r\n--{bnd}--".encode()
            fid = api("POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id",
                      raw=body, ctype=f"multipart/related; boundary={bnd}")["id"]
            tmp[f] = fid
            api("POST", f"{D}/{fid}/permissions", {"role": "reader", "type": "anyone"})
        reqs = [{"deleteObject": {"objectId": s["objectId"]}} for s in p.get("slides", [])]
        reqs += requests_for(L, lambda f: f"https://drive.google.com/uc?export=download&id={tmp[f]}")
        for i in range(0, len(reqs), 400):
            api("POST", f"https://slides.googleapis.com/v1/presentations/{pid}:batchUpdate",
                {"requests": reqs[i:i + 400]})
        api("POST", f"{D}/{pid}/permissions?supportsAllDrives=true", {"role": "reader", "type": "anyone"})
    except Exception:
        api("PATCH", f"{D}/{pid}?supportsAllDrives=true", {"trashed": True})
        raise
    finally:
        for fid in tmp.values():
            try:
                api("PATCH", f"{D}/{fid}", {"trashed": True})
            except Exception:  # noqa: BLE001
                pass
    return {"id": pid, "url": f"https://docs.google.com/presentation/d/{pid}/view"}


# ---------- the library side ----------

def title_for(filename: str) -> str:
    return re.sub(r"\.pdf$", "", filename or "", flags=re.I) + SLIDES_SUFFIX


def twin(doc_id: int) -> dict:
    """Build (or rebuild) the Slides twin of library PDF `doc_id`, beside its PDF on Drive. Returns {id, url}."""
    d = db.one("select id, filename, mime, path, drive_id from company_documents where id=%s", (int(doc_id),))
    if not d:
        raise ValueError(f"library document #{doc_id} not found")
    if "pdf" not in (d["mime"] or "") and not (d["filename"] or "").lower().endswith(".pdf"):
        raise ValueError(f"'{d['filename']}' is not a PDF")
    if not d.get("drive_id"):
        raise ValueError(f"'{d['filename']}' is not on Drive yet, so there is no folder to put its Slides beside")
    api = _Api()
    parents = api("GET", f"https://www.googleapis.com/drive/v3/files/{d['drive_id']}"
                         "?supportsAllDrives=true&fields=parents").get("parents") or []
    if not parents:
        raise ValueError(f"cannot see the Drive folder of '{d['filename']}'")
    work = os.path.join(WORK, str(d["id"]))
    shutil.rmtree(work, ignore_errors=True)
    try:
        L = layout(d["path"], work)
        res = build(L, work, title_for(d["filename"]), parents[0])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    db.execute("update company_documents set slides_id=%s, slides_url=%s where id=%s", (res["id"], res["url"], d["id"]))
    return res


def start_twin(doc_id: int) -> None:
    """twin() on a thread: filing a deck never waits on its Slides. A failure is printed, never raised."""
    def run():
        try:
            r = twin(doc_id)
            print(f"slides twin for doc {doc_id}: {r['url']}")
        except Exception as e:  # noqa: BLE001
            print(f"slides twin for doc {doc_id} failed: {e}")
    threading.Thread(target=run, daemon=True, name=f"slides-{doc_id}").start()


def links_for(refs) -> dict:
    """{doc_id: slides_url} for the library documents on a card (attach_docs refs), read live."""
    ids = [int(r.get("id")) for r in (refs or []) if isinstance(r, dict) and str(r.get("id") or "").isdigit()]
    if not ids:
        return {}
    try:
        rows = db.query("select id, slides_url from company_documents where id = any(%s) and slides_url is not null",
                        (ids,))
        return {r["id"]: r["slides_url"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}
