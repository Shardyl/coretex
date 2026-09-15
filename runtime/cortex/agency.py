"""Agency AI production: the trade tier, its quotation preset and guide quotation, and the standard Agency AI
Production Rates deck (owner, 15 Sep 2026).

Why: an agency buying AI video to resell (Promocell, card 684: "cost per <30 second video with and without VO,
bundles of 5, 10 or 15") was being priced from the direct-client rate card, where one short AI piece is AED 8,000
and ten with English voice-over come to AED 100,000. The owner set a trade tier instead and asked for it on the
rate card, as a guide quotation beside the quotation templates, and as a standard deck Cortex offers whenever an
agency asks, rebranded for that agency when useful.

Revision 1 (owner, same day): no human voice-over ("we just don't do them, and we're pitching AI"); two social
formats included per video; revisions are two rounds PER BATCH; the deck carries the prices, so no quotation goes
with it; the deck shows the range of our AI work (CGI social films, AI influencers, AI product and brand films)
with different imagery on every page, and fewer text pages.

Every price here is DATA: the rate card group is the source, the deck and the guide quotation read their figures
from it by key. The deck's words and films live in the setting `agency_deck_spec:<slug>` (seeded from DEFAULT_SPEC
whenever DEFAULT_SPEC's version is newer), editable without a deploy."""
from __future__ import annotations

import copy
import io
import os
from datetime import datetime, timezone

from . import db, deck, documents, drive, profile, quotation, ratecard, store

GROUP = "Agency AI production (trade)"
SOURCE = "owner-stated 15 Sep 2026: agency trade tier"
GROUP_NOTE = ("Trade rates for agencies producing AI video for their own clients. Spec: up to 30 seconds, AI visuals "
              "carry the video, simple edit with on-screen text, licensed social music, AI voice-over when the concept "
              "needs it, two social formats per video, two consolidated revision rounds per batch; the agency supplies "
              "the brief and the approved script. No human voice-over: AI voice only.")
ITEMS = [
    {"key": "agency-ai-video-single", "desc": "Agency: AI video up to 30 seconds, single", "rate": 4500, "unit": "video"},
    {"key": "agency-ai-video-bundle-5", "desc": "Agency: bundle of 5 AI videos up to 30 seconds (AED 4,000 each)",
     "rate": 20000, "unit": "bundle"},
    {"key": "agency-ai-video-bundle-10", "desc": "Agency: bundle of 10 AI videos up to 30 seconds (AED 3,600 each)",
     "rate": 36000, "unit": "bundle"},
    {"key": "agency-ai-video-bundle-15", "desc": "Agency: bundle of 15 AI videos up to 30 seconds (AED 3,300 each)",
     "rate": 49500, "unit": "bundle"},
    {"key": "agency-ai-voice-over", "desc": "Agency: AI voice-over, English or Arabic, per video", "rate": 500,
     "unit": "video"},
    {"key": "agency-additional-aspect-ratio", "desc": "Agency: additional social format, per video (two are included)",
     "rate": 400, "unit": "video/format"},
    {"key": "agency-additional-revision-round", "desc": "Agency: additional revision round, beyond the two included "
     "per batch", "rate": 1000, "unit": "round"},
]
PRESET = "agency-ai"
GUIDE_VERSION = "1.1"
OUT = "/opt/coretex/quotations/agency"


def _aed(v) -> str:
    return f"AED {float(v):,.0f}"


def rates(slug: str = "sensa") -> dict:
    """{key: rate} for the agency group, read from the LIVE rate card (never from ITEMS above)."""
    idx = ratecard.index(slug)
    return {it["key"]: float((idx.get(it["key"]) or {}).get("rate") or 0) for it in ITEMS}


# --------------------------------------------------------------------------- installs (idempotent, versioned)

def install_rate_card(slug: str = "sensa") -> str:
    """Add or change the agency group. Any change is a NEW rate-card version and the previous card is kept whole as
    `rate_card:<slug>:v<old>` (the house rule for every rate change); an unchanged group changes nothing."""
    card = copy.deepcopy(ratecard.get(slug) or {})
    items = [{**it, "note": "", "budget": None, "source": SOURCE} for it in ITEMS]
    grp = next((g for g in card.get("groups") or [] if g.get("heading") == GROUP), None)
    if grp and grp.get("items") == items and grp.get("note") == GROUP_NOTE:
        return f"agency group unchanged on rate card v{card.get('version')}"
    old = str(card.get("version") or "1.0")
    db.setting_set(f"rate_card:{slug}:v{old}", ratecard.get(slug))
    if grp:
        grp.update({"items": items, "note": GROUP_NOTE})
    else:
        card.setdefault("groups", []).append({"heading": GROUP, "note": GROUP_NOTE, "items": items})
    major, _, minor = old.partition(".")
    card["version"] = f"{major}.{int(minor or 0) + 1}"
    ratecard.save(slug, card)
    return f"rate card v{old} -> v{card['version']} (v{old} kept as rate_card:{slug}:v{old})"


def install_preset() -> str:
    """The agency quotation preset: the ai-production preset's terms and 70/30 payment, with the agency title,
    line structure, deliverables and note."""
    pr = copy.deepcopy(quotation.presets())
    base = copy.deepcopy(pr["ai-production"])
    base.update({
        "title": "AI VIDEO PRODUCTION QUOTATION (AGENCY RATES)",
        "agency_fee": False,
        "sections": [
            {"header": "A ·  AI VIDEO PRODUCTION", "items": [
                {"desc": "AI videos up to 30 seconds each, single or a bundle of 5, 10 or 15: key frames, video "
                         "generation, edit with on-screen text, licensed music, two social formats per video",
                 "weight": 8}]},
            {"header": "B ·  VOICE-OVER AND FORMATS", "items": [
                {"desc": "AI voice-over, English or Arabic, per video", "weight": 1},
                {"desc": "Additional social format, per video", "weight": 1}]}],
        "deliverables": ["AI-generated videos, up to 30 seconds each",
                         "Two social formats per video (for example 9:16 and 1:1), 4K MP4",
                         "On-screen text and licensed music (social licence)",
                         "AI voice-over where the concept needs it",
                         "Two consolidated revision rounds per batch",
                         "Supplied without Sensa branding"],
        "note": ("Agency rates, for agencies producing on behalf of their own clients. The agency supplies the brief "
                 "and the approved script. Concept and script writing, a consistent AI character across videos, "
                 "captions in a second language, rush turnaround and broadcast or outdoor usage are quoted "
                 "separately."),
    })
    pr[PRESET] = base
    db.setting_set("quotation_presets", pr)
    return f"preset '{PRESET}' installed ({len(pr)} presets)"


def guide_quotation(slug: str = "sensa") -> list[str]:
    """A worked example beside the quotation templates (a reference for the team, never sent with the deck): ten
    videos with AI voice-over, priced by code from the agency group. Reference GUIDE-AGENCY, never a SEN number.
    A new version moves the older one into the terms folder's archive."""
    os.makedirs(OUT, exist_ok=True)
    r = rates(slug)
    secs = [
        {"header": "A ·  AI VIDEO PRODUCTION", "items": [
            {"desc": f"Bundle of 10 AI videos, up to 30 seconds each ({_aed(r['agency-ai-video-bundle-10'] / 10)} per "
                     "video): key frames, video generation, edit with on-screen text, licensed music, two social formats",
             "components": [{"item": "agency-ai-video-bundle-10", "qty": 1}]}]},
        {"header": "B ·  VOICE-OVER", "items": [
            {"desc": "AI voice-over, English or Arabic, for each of the ten videos",
             "components": [{"item": "agency-ai-voice-over", "qty": 10}]}]}]
    ratecard.price_lines(slug, secs)
    note = (f"Guide quotation at agency rates. Other bundles: single video {_aed(r['agency-ai-video-single'])}; "
            f"5 videos {_aed(r['agency-ai-video-bundle-5'])}; 15 videos {_aed(r['agency-ai-video-bundle-15'])}. "
            f"Additional social format {_aed(r['agency-additional-aspect-ratio'])} per video. Additional revision "
            f"round {_aed(r['agency-additional-revision-round'])}.")
    pdef = quotation.presets()[PRESET]
    x = quotation.generate_xlsx(slug, PRESET, customer="Agency partner (guide)", sections=secs,
                                title="AI VIDEOS FOR AGENCIES: GUIDE QUOTATION", note=note,
                                deliverables=pdef.get("deliverables"), number="GUIDE-AGENCY", out_dir=OUT)
    pdf = quotation.xlsx_to_pdf(x["path"], OUT)
    co = store.get_company_by_slug(slug)
    prof = profile.get(co["id"]) or {}
    folder, arch, tok = prof.get("terms_drive_folder"), prof.get("terms_archive_folder") or "", drive.access_token()
    out = []
    for path, mime, ext in ((x["path"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
                            (pdf, "application/pdf", "pdf")):
        name = f"Sensa - Agency Guide Quotation v{GUIDE_VERSION}.{ext}"
        data = open(path, "rb").read()
        fid = drive.upsert_in_folder(folder, name, mime, data, token=tok) if folder else None
        if folder:
            documents.archive_lower_versions(folder, name, arch, tok)
        row = documents.save(co["id"], slug, name, mime, data, kind="terms", uploaded_by="cortex agency", push=False)
        if fid:
            db.execute("update company_documents set drive_id=%s where id=%s", (fid, row["id"]))
        old = [o["id"] for o in db.query("select id from company_documents where company_id=%s and filename like %s "
                                         "and filename <> %s and superseded_by is null",
                                         (co["id"], f"Sensa - Agency Guide Quotation v%.{ext}", name))]
        if old:
            documents.supersede(old, row["id"])
        out.append(name)
    return out


# --------------------------------------------------------------------------- the deck

DEFAULT_SPEC = {
    "version": "1.1",
    "cover": {"kicker": "AI production for agencies", "title1": "Your AI studio,", "title2": "on call.",
              "standfirst": "Short AI-generated videos for your clients, directed by filmmakers, delivered under "
                            "your name, at trade rates.", "image": "2-3tPJfn3RQ"},
    "make": {"heading": "What we make", "cells": [
        {"title": "CGI social films", "body": "A real scene shot on a phone, with a CGI element dropped in. Portrait, "
                                              "built for feeds.", "film": "WydqL_1V1wI"},
        {"title": "AI influencers", "body": "Characters we create and keep consistent across a series, for a brand's "
                                            "own channels.", "film": "x7pYhmCYZVI"},
        {"title": "AI product films", "body": "Products shown in worlds that would cost a fortune to build, from cars "
                                              "to fragrance.", "film": "5JrI6GmwIBQ"},
        {"title": "AI brand films", "body": "Longer stories told with AI imagery, for brands, hospitals and "
                                            "institutions.", "film": "Gxf2QAdvET4"}]},
    "sections": [
        {"kicker": "CGI social films", "heading": "Real life, with something impossible in it",
         "body": "A real scene filmed on a phone, with a CGI element that could never have been there. Made in portrait "
                 "for Reels, TikTok and Shorts, and short enough to stop the scroll.",
         "films": [["WydqL_1V1wI", "Zed"], ["gGdpiIsOGzU", "Huru, banner"], ["hf4FvzrYr6c", "Huru, drone"],
                   ["49uk8Y24Ruc", "Nameless Ventures, 3D"], ["falzuk31ciE", "Nameless Ventures, social"]]},
        {"kicker": "AI influencers", "heading": "A face for the brand, episode after episode",
         "body": "Characters we design once and keep consistent, so a brand can run a presenter or a guide across a "
                 "whole series without a casting call.",
         "films": [["zlSV_2oM980", "Leo Iconik"], ["x7pYhmCYZVI", "Leo Iconik"], ["IQgqyD_MC-4", "Zayd Adventure, 1"],
                   ["e9y7kj9TWTA", "Zayd Adventure, 2"], ["Gm8PGtOoFgw", "Zayd Adventure, 3"]]}],
    "products": {"heading": "AI product and brand films", "films": [
        ["2-3tPJfn3RQ", "Mercedes E-Class, a showcase built with AI"],
        ["5JrI6GmwIBQ", "Orientica Crown, a fragrance showcase built with AI"],
        ["Gxf2QAdvET4", "Al Rahba Hospital, the heart of our community"]]},
    "how": {"heading": "How it works", "phases": [
        {"when": "STEP 1", "title": "Brief", "body": "You send the brief and approved script for each video."},
        {"when": "STEP 2", "title": "Key frames", "body": "You approve the key frames before any video is generated."},
        {"when": "STEP 3", "title": "The cut", "body": "The edited video with text, music and voice-over, for your review."},
        {"when": "STEP 4", "title": "Formats", "body": "The approved master in two social formats, ready to post."}],
        "cards": [{"title": "Every video includes", "body": "Up to 30 seconds, AI visuals, a clean edit with on-screen "
                                                            "text, licensed music, an AI voice-over when the concept "
                                                            "needs it, and two social formats (for example 9:16 and 1:1)."},
                  {"title": "Revisions", "body": "Two consolidated rounds of revisions per batch, sent as one list by "
                                                 "email. A further round is {agency-additional-revision-round}."}],
        "image": "Gxf2QAdvET4"},
    "prices": {"heading": "From {per_video_min} per video", "blurb": "Excluding VAT. The more videos in a batch, the lower "
                                                                  "the rate per video.",
               "on_request": "Concept and script writing, a consistent AI character across videos, captions in a second "
                             "language, rush turnaround, broadcast or outdoor usage", "image": "5JrI6GmwIBQ"},
    "close": {"heading": "Send us a brief", "cards": [
        {"title": "Payment", "body": "70% to start, 30% on delivery, under our AI production terms."},
        {"title": "White-label", "body": "Delivered without Sensa branding; shown in our portfolio only with your consent."},
        {"title": "Revisions", "body": "Two consolidated rounds per batch are included."}],
        "steps": [{"title": "Send the brief", "body": "The brief and script for your first video, or your first batch."},
                  {"title": "Approve the frames", "body": "Key frames before anything is generated."},
                  {"title": "Receive the videos", "body": "Finished masters in two social formats, ready for your client."}],
        "signoff": "Sensa Productions · hello@sensa.digital · sensa.digital", "image": "IQgqyD_MC-4"},
}


def spec(slug: str = "sensa") -> dict:
    s = db.setting_get(f"agency_deck_spec:{slug}")
    if not s or str(s.get("version", "0")) < DEFAULT_SPEC["version"]:
        s = copy.deepcopy(DEFAULT_SPEC)
        db.setting_set(f"agency_deck_spec:{slug}", s)
    return s


def _fill(text: str, r: dict) -> str:
    """Prices in the deck's words are placeholders filled from the rate card: {agency-...} or {per_video_min}."""
    vals = {k: _aed(v) for k, v in r.items()}
    vals["per_video_min"] = _aed(min(r["agency-ai-video-bundle-15"] / 15, r["agency-ai-video-bundle-10"] / 10,
                                     r["agency-ai-video-bundle-5"] / 5, r["agency-ai-video-single"]))
    for k, v in vals.items():
        text = text.replace("{" + k + "}", v)
    return text


def _vthumb(video_id: str) -> str | None:
    """A PORTRAIT frame for a phone-first film: YouTube's own portrait thumbnail when it has one, else the centre
    of the landscape thumbnail cut to 9:16."""
    import httpx
    from PIL import Image
    path = os.path.join(OUT, f"v-{video_id}.jpg")
    if os.path.exists(path):
        return path
    try:
        r = httpx.get(f"https://i.ytimg.com/vi/{video_id}/oardefault.jpg", timeout=20)
        if r.status_code == 200 and len(r.content) > 5000:
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            if im.size[1] > im.size[0]:
                im.thumbnail((720, 1280))
                im.save(path, "JPEG", quality=88)
                return path
    except Exception:  # noqa: BLE001
        pass
    land = deck.thumbnail(video_id, OUT)
    if not land:
        return None
    im = Image.open(land).convert("RGB")
    w, h = im.size
    cw = int(h * 9 / 16)
    im.crop(((w - cw) // 2, 0, (w - cw) // 2 + cw, h)).save(path, "JPEG", quality=88)
    return path


def _watch(v: str) -> str:
    return f"https://www.youtube.com/watch?v={v}"


def build_deck(slug: str = "sensa", customer: str | None = None) -> dict:
    """The standard deck, or a version with the agency's name on it. Figures from the rate card by key; films from
    the spec by video id, each linked to its film."""
    os.makedirs(OUT, exist_ok=True)
    co = store.get_company_by_slug(slug)
    s, r = spec(slug), rates(slug)
    if not all(r.values()):
        raise ValueError("the agency group is missing from the rate card; run agency.install_rate_card first")
    f = lambda t: _fill(t or "", r)       # noqa: E731
    land = lambda v: deck.thumbnail(v, OUT)   # noqa: E731
    today = datetime.now(timezone.utc).date()
    d = deck.CreativeDeck(co, customer or co["name"], deck._ACCENT_DEFAULT, deck._logo(co), "AI production for agencies")
    meta = ([{"k": "Prepared for", "v": customer}] if customer else [{"k": "For", "v": "Agencies and their clients"}])
    meta += [{"k": "Format", "v": "AI videos up to 30 seconds"}, {"k": "Rates", "v": "Agency trade rates, AED"},
             {"k": "From", "v": "Sensa Productions, Dubai"}, {"k": "Date", "v": f"{today.day} {today:%B %Y}"}]
    c = s["cover"]
    d.covermeta(c["kicker"], c["title1"], c["title2"], f(c["standfirst"]), meta, land(c["image"]),
                "AI production for agencies")
    mk = s["make"]
    d.grid("01 · What we make", mk["heading"], "",
           [{"num": f"{i + 1:02d}", "title": x["title"], "body": x["body"]} for i, x in enumerate(mk["cells"][:4])],
           images=[{"path": land(x["film"]), "href": _watch(x["film"])} for x in mk["cells"][:4]])
    n = 2
    for sec in s["sections"]:
        tiles = [{"img": _vthumb(v), "label": lbl, "href": _watch(v)} for v, lbl in sec["films"][:7]]
        d.tiles(f"{n:02d} · {sec['kicker']}", sec["heading"], sec["body"], [t for t in tiles if t["img"]],
                "Tap any film to watch it.", sec["kicker"])
        n += 1
    films = [{"youtube_video_id": v, "label": lbl.split(",")[0], "caption": lbl.split(",", 1)[1].strip()
              if "," in lbl else "", "thumb": land(v)} for v, lbl in s["products"]["films"][:3]]
    d.samples(f"{n:02d} · Our work", s["products"]["heading"], "", films)
    n += 1
    h = s["how"]
    d.phases(f"{n:02d} · How it works", h["heading"], h["phases"][:5],
             [{"title": x["title"], "body": f(x["body"])} for x in h["cards"][:2]])
    d.with_bg(land(h["image"]))
    n += 1
    p = s["prices"]
    rows = [{"item": "Single video", "detail": "One AI video up to 30 seconds", "amount": _aed(r["agency-ai-video-single"])}]
    for k in (5, 10, 15):
        tot = r[f"agency-ai-video-bundle-{k}"]
        rows.append({"item": f"Batch of {k}", "detail": f"{_aed(tot / k)} per video", "amount": _aed(tot)})
    rows += [{"item": "AI voice-over", "detail": "English or Arabic, per video", "amount": _aed(r["agency-ai-voice-over"])},
             {"item": "Additional social format", "detail": "Per video, beyond the two included",
              "amount": _aed(r["agency-additional-aspect-ratio"])},
             {"item": "Additional revision round", "detail": "Beyond the two included per batch",
              "amount": _aed(r["agency-additional-revision-round"])},
             {"item": "Quoted on request", "detail": p["on_request"], "amount": "On request"}]
    d.investment(f"{n:02d} · Agency rates", f(p["heading"]), f(p["blurb"]), rows)
    d.with_bg(land(p["image"]))
    n += 1
    cl = s["close"]
    d.closing(f"{n:02d} · Start", cl["heading"], "", [{**x, "body": f(x["body"])} for x in cl["cards"][:3]],
              cl["steps"][:3], cl.get("signoff", ""))
    d.with_bg(land(cl["image"]))
    name = (f"{customer} - Agency AI Production Rates - {today:%Y-%m-%d}.pdf" if customer
            else f"Sensa - Agency AI Production Rates v{s.get('version', '1.0')}.pdf")
    path = deck.to_pdf(d.html(), os.path.join(OUT, name))
    from pypdf import PdfReader
    return {"path": path, "name": name, "pages": len(PdfReader(path).pages), "planned": len(d.pages)}


def deliver_deck(slug: str = "sensa", customer: str | None = None, deal_id: int | None = None) -> dict:
    """The STANDARD deck is one of the company's own official documents: Documents on Drive (the older version moved
    to Documents/Archive), library kind 'agency-deck' (a core kind, never hidden by a deal's scope), profile
    `agency_rates_deck_doc`, earlier standard versions retired. A version FOR an agency is client work: filed on its
    deal and client folder."""
    from . import engine
    co = store.get_company_by_slug(slug)
    out = build_deck(slug, customer)
    data = open(out["path"], "rb").read()
    if not customer:
        doc = documents.save(co["id"], slug, out["name"], "application/pdf", data, kind="agency-deck",
                             uploaded_by="cortex agency", push=True)
        old = [x["id"] for x in db.query("select id from company_documents where company_id=%s and kind='agency-deck' "
                                         "and id<>%s and superseded_by is null and coalesce(client,'')=''",
                                         (co["id"], doc["id"]))]
        if old:
            documents.supersede(old, doc["id"])
        try:
            tok = drive.access_token()
            docs = documents._drive_docs_folder(co["id"], slug)
            if docs:
                documents.archive_lower_versions(docs, out["name"], drive.ensure_subfolder(docs, "Archive", tok), tok)
        except Exception as e:  # noqa: BLE001 — the new deck is filed either way
            print(f"[agency] archive older deck: {e}", flush=True)
        db.execute("update company_profiles set data = data || jsonb_build_object('agency_rates_deck_doc', %s::int) "
                   "where company_id=%s", (doc["id"], co["id"]))
        return {**out, "doc_id": doc["id"], "filed_to": "Documents"}
    doc, filed = engine._file_proposal_pdf(co, customer, out["name"], out["path"], deal_id, None)
    return {**out, "doc_id": doc["id"], "filed_to": filed}


if __name__ == "__main__":      # python -m cortex.agency install  |  deck [customer]
    import sys
    if sys.argv[1] == "install":
        print(install_rate_card())
        print(install_preset())
        print(guide_quotation())
        print(deliver_deck())
    elif sys.argv[1] == "deck":
        print(deliver_deck(customer=sys.argv[2] if len(sys.argv) > 2 else None))
