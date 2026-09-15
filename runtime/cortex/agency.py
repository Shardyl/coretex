"""Agency AI production: the trade tier, its quotation preset and guide quotation, and the standard Agency AI
Production Rates deck (owner, 15 Sep 2026).

Why: an agency buying AI video to resell (Promocell, card 684: "cost per <30 second video with and without VO,
bundles of 5, 10 or 15") was being priced from the direct-client rate card, where one short AI piece is AED 8,000
and ten with English voice-over come to AED 100,000. The owner set a trade tier instead and asked for it on the
rate card, as a guide quotation beside the quotation templates, and as a standard deck Cortex offers whenever an
agency asks, rebranded for that agency when useful.

Every price here is DATA: the rate card group below is the source, the deck and the guide quotation read their
figures from it by key, so a rate change on the card changes both. The deck's words live in the setting
`agency_deck_spec:<slug>` (seeded once from DEFAULT_SPEC), editable without a deploy."""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone

from . import db, deck, documents, drive, profile, quotation, ratecard, store

GROUP = "Agency AI production (trade)"
SOURCE = "owner-stated 15 Sep 2026: agency trade tier"
GROUP_NOTE = ("Trade rates for agencies producing AI video for their own clients. Spec: up to 30 seconds, AI visuals "
              "carry the video, simple edit with on-screen text, licensed social music, one master aspect ratio, two "
              "consolidated revision rounds per video; the agency supplies the brief and the approved script.")
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
    {"key": "agency-human-voice-over-english", "desc": "Agency: human voice artist, English, per video", "rate": 1000,
     "unit": "video"},
    {"key": "agency-human-voice-over-arabic", "desc": "Agency: human voice artist, Arabic, per video", "rate": 1800,
     "unit": "video"},
    {"key": "agency-additional-aspect-ratio", "desc": "Agency: additional aspect ratio, per video", "rate": 400,
     "unit": "video/version"},
    {"key": "agency-additional-revision-round", "desc": "Agency: additional revision round, beyond the two included "
     "per video", "rate": 1000, "unit": "round"},
]
PRESET = "agency-ai"
OUT = "/opt/coretex/quotations/agency"


def _aed(v) -> str:
    return f"AED {float(v):,.0f}"


def rates(slug: str = "sensa") -> dict:
    """{key: rate} for the agency group, read from the LIVE rate card (never from ITEMS above)."""
    idx = ratecard.index(slug)
    return {it["key"]: float((idx.get(it["key"]) or {}).get("rate") or 0) for it in ITEMS}


# --------------------------------------------------------------------------- one-off installs (idempotent)

def install_rate_card(slug: str = "sensa") -> str:
    """Add (or refresh) the agency group. Adding it is a new rate-card version; the old one is kept whole as
    `rate_card:<slug>:v<old>` (the house rule for every rate change)."""
    card = copy.deepcopy(ratecard.get(slug) or {})
    items = [{**it, "note": "", "budget": None, "source": SOURCE} for it in ITEMS]
    grp = next((g for g in card.get("groups") or [] if g.get("heading") == GROUP), None)
    if grp:
        grp.update({"items": items, "note": GROUP_NOTE})
        ratecard.save(slug, card)
        return f"refreshed the agency group on rate card v{card.get('version')}"
    old = str(card.get("version") or "1.0")
    db.setting_set(f"rate_card:{slug}:v{old}", ratecard.get(slug))
    card.setdefault("groups", []).append({"heading": GROUP, "note": GROUP_NOTE, "items": items})
    major, _, minor = old.partition(".")
    card["version"] = f"{major}.{int(minor or 0) + 1}"
    card["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
                         "generation, edit with on-screen text, licensed music and a 4K master", "weight": 8}]},
            {"header": "B ·  VOICE-OVER AND VERSIONS", "items": [
                {"desc": "AI voice-over, English or Arabic, per video", "weight": 1},
                {"desc": "Additional aspect ratio, per video", "weight": 1}]}],
        "deliverables": ["AI-generated videos, up to 30 seconds each",
                         "One master aspect ratio per video, 4K MP4",
                         "On-screen text and licensed music (social licence)",
                         "AI voice-over where the concept needs it",
                         "Two consolidated revision rounds per video",
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
    """A worked example beside the quotation templates: ten videos with AI voice-over, priced by code from the
    agency group, the other options listed underneath. Its reference is GUIDE-AGENCY, never a SEN number."""
    os.makedirs(OUT, exist_ok=True)
    r = rates(slug)
    secs = [
        {"header": "A ·  AI VIDEO PRODUCTION", "items": [
            {"desc": f"Bundle of 10 AI videos, up to 30 seconds each ({_aed(r['agency-ai-video-bundle-10'] / 10)} per "
                     "video): key frames, video generation, edit with on-screen text, licensed music, 4K master",
             "components": [{"item": "agency-ai-video-bundle-10", "qty": 1}]}]},
        {"header": "B ·  VOICE-OVER", "items": [
            {"desc": "AI voice-over, English or Arabic, for each of the ten videos",
             "components": [{"item": "agency-ai-voice-over", "qty": 10}]}]}]
    ratecard.price_lines(slug, secs)
    note = (f"Guide quotation at agency rates. Other bundles: single video {_aed(r['agency-ai-video-single'])}; "
            f"5 videos {_aed(r['agency-ai-video-bundle-5'])}; 15 videos {_aed(r['agency-ai-video-bundle-15'])}. "
            f"Human voice artist per video: English {_aed(r['agency-human-voice-over-english'])}, Arabic "
            f"{_aed(r['agency-human-voice-over-arabic'])}. Additional aspect ratio "
            f"{_aed(r['agency-additional-aspect-ratio'])} per video. Additional revision round "
            f"{_aed(r['agency-additional-revision-round'])}.")
    pdef = quotation.presets()[PRESET]
    x = quotation.generate_xlsx(slug, PRESET, customer="Agency partner (guide)", sections=secs,
                                title="AI VIDEOS FOR AGENCIES: GUIDE QUOTATION", note=note,
                                deliverables=pdef.get("deliverables"), number="GUIDE-AGENCY", out_dir=OUT)
    pdf = quotation.xlsx_to_pdf(x["path"], OUT)
    co = store.get_company_by_slug(slug)
    prof = profile.get(co["id"]) or {}
    folder, tok = prof.get("terms_drive_folder"), drive.access_token()
    out = []
    for path, mime, name in ((x["path"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              "Sensa - Agency Guide Quotation v1.0.xlsx"),
                             (pdf, "application/pdf", "Sensa - Agency Guide Quotation v1.0.pdf")):
        data = open(path, "rb").read()
        fid = drive.upsert_in_folder(folder, name, mime, data, token=tok) if folder else None
        row = documents.save(co["id"], slug, name, mime, data, kind="terms", uploaded_by="cortex agency", push=False)
        if fid:
            db.execute("update company_documents set drive_id=%s where id=%s", (fid, row["id"]))
        out.append(name)
    return out


# --------------------------------------------------------------------------- the deck

DEFAULT_SPEC = {
    "version": "1.0",
    "films": ["vfi2IaDgjpw", "IN9Q7RcttlI", "o3aYpr5t1Bc"],
    "backgrounds": ["vfi2IaDgjpw", "KbP1LWYumts", "IN9Q7RcttlI", "o3aYpr5t1Bc", "hM-GMbLsop4", "djOYHilbHps"],
    "cover": {"kicker": "AI production for agencies", "title1": "Your AI studio,", "title2": "on call.",
              "standfirst": "Short AI-generated videos for your clients, directed by filmmakers, delivered under "
                            "your name, at trade rates."},
    "why": {"heading": "Why agencies outsource AI production to us", "cards": [
        {"title": "No shoot, no waiting", "body": "No crew, no location, no weather. The AI visuals carry the video, "
                                                  "directed by people who make films for a living."},
        {"title": "Directed, not generated", "body": "Every shot is chosen from many takes, then edited, graded and "
                                                     "finished like any film we make."},
        {"title": "Your client stays yours", "body": "We work behind you. Files arrive without Sensa branding and we "
                                                     "never contact your client."}]},
    "includes": {"heading": "What one video includes", "cells": [
        {"title": "Up to 30 seconds", "body": "Short-form, built for social feeds."},
        {"title": "AI visuals carry it", "body": "Generated imagery and motion, directed shot by shot."},
        {"title": "Simple finish", "body": "Clean edit, transitions and on-screen text."},
        {"title": "Voice-over option", "body": "An AI voice in English or Arabic, or a human voice artist."},
        {"title": "Licensed music", "body": "A track licensed for social use."},
        {"title": "One master format", "body": "A 4K master; extra aspect ratios per video."},
        {"title": "Two revision rounds", "body": "Consolidated, per video, by email."},
        {"title": "Your brief", "body": "You supply the brief and approved script; we can write them for you."}]},
    "how": {"heading": "How it works", "phases": [
        {"when": "STEP 1", "title": "Brief", "body": "You send the brief and approved script for each video."},
        {"when": "STEP 2", "title": "Key frames", "body": "You approve the key frames before any video is generated."},
        {"when": "STEP 3", "title": "The cut", "body": "The edited video with text, music and voice-over, for your review."},
        {"when": "STEP 4", "title": "Versions", "body": "Approved masters, then any extra aspect ratios, delivered together."}],
        "card": {"title": "Revisions", "body": "Two consolidated rounds per video, sent as one list by email. A further "
                                               "round is {agency-additional-revision-round}."}},
    "samples": {"heading": "AI films we have made", "captions": [
        "An AI campaign now live on billboards across the UAE", "A financial services commercial built with AI",
        "Five centuries of craftsmanship, told with AI"]},
    "prices": {"heading": "From {per_video_min} per video", "blurb": "Excluding VAT. The more videos in a bundle, the "
                                                                  "lower the rate per video."},
    "addons": {"heading": "Add only what the concept needs", "blurb": "Per video, excluding VAT.",
               "on_request": "Concept and script writing, a consistent AI character across videos, captions in a "
                             "second language, rush turnaround, broadcast or outdoor usage"},
    "terms": {"heading": "Terms at a glance", "cards": [
        {"title": "Payment", "body": "70% to start, 30% on delivery, under our AI production terms."},
        {"title": "Revisions", "body": "Two consolidated rounds per video are included."},
        {"title": "White-label", "body": "Delivered without Sensa branding; shown in our portfolio only with your "
                                         "consent."}]},
    "close": {"heading": "Send us a brief", "cards": [
        {"title": "Start small", "body": "One video to see how we work, or a bundle for a campaign."},
        {"title": "Talk to us", "body": "We are happy to walk you through a brief on a call."}],
        "steps": [{"title": "Send the brief", "body": "The brief and script for your first video."},
                  {"title": "Approve the frames", "body": "Key frames before anything is generated."},
                  {"title": "Receive the video", "body": "The finished master, ready for your client."}],
        "signoff": "Sensa Productions · hello@sensa.digital · sensa.digital"},
}


def spec(slug: str = "sensa") -> dict:
    s = db.setting_get(f"agency_deck_spec:{slug}")
    if not s:
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


def build_deck(slug: str = "sensa", customer: str | None = None) -> dict:
    """The standard deck, or a version with the agency's name on it. Figures come from the rate card by key."""
    os.makedirs(OUT, exist_ok=True)
    co = store.get_company_by_slug(slug)
    s, r = spec(slug), rates(slug)
    if not all(r.values()):
        raise ValueError("the agency group is missing from the rate card; run agency.install_rate_card first")
    f = lambda t: _fill(t or "", r)       # noqa: E731
    thumbs = {v: deck.thumbnail(v, OUT) for v in dict.fromkeys(s["films"] + s["backgrounds"])}
    bg = [thumbs[v] for v in s["backgrounds"] if thumbs.get(v)] or [None]
    bgi = lambda i: bg[i % len(bg)]       # noqa: E731
    today = datetime.now(timezone.utc).date()
    d = deck.CreativeDeck(co, customer or co["name"], deck._ACCENT_DEFAULT, deck._logo(co), "AI production for agencies")
    meta = ([{"k": "Prepared for", "v": customer}] if customer else [{"k": "For", "v": "Agencies and their clients"}])
    meta += [{"k": "Format", "v": "AI videos up to 30 seconds"}, {"k": "Rates", "v": "Agency trade rates, AED"},
             {"k": "From", "v": "Sensa Productions, Dubai"}, {"k": "Date", "v": f"{today.day} {today:%B %Y}"}]
    c = s["cover"]
    d.covermeta(c["kicker"], c["title1"], c["title2"], f(c["standfirst"]), meta, bgi(0), "AI production for agencies")
    d.cards("01 · Why us", s["why"]["heading"], s["why"]["cards"], None)
    d.with_bg(bgi(1))
    d.grid("02 · What you get", s["includes"]["heading"], "",
           [{"num": f"{i + 1:02d}", **x} for i, x in enumerate(s["includes"]["cells"][:8])])
    d.with_bg(bgi(2))
    hc = s["how"]["card"]
    d.phases("03 · How it works", s["how"]["heading"], s["how"]["phases"][:5],
             [{"title": hc["title"], "body": f(hc["body"])}])
    d.with_bg(bgi(3))
    films = []
    caps = s["samples"]["captions"]
    for i, v in enumerate(s["films"][:3]):
        row = db.one("select title from media_assets where youtube_video_id=%s limit 1", (v,)) or {}
        films.append({"youtube_video_id": v, "label": row.get("title") or "", "caption": caps[i] if i < len(caps) else "",
                      "thumb": thumbs.get(v)})
    d.samples("04 · Our work", s["samples"]["heading"], "", films)
    p = s["prices"]
    rows = [{"item": "Single video", "detail": "One AI video up to 30 seconds", "amount": _aed(r["agency-ai-video-single"])}]
    for n in (5, 10, 15):
        tot = r[f"agency-ai-video-bundle-{n}"]
        rows.append({"item": f"Bundle of {n}", "detail": f"{_aed(tot / n)} per video", "amount": _aed(tot)})
    d.investment("05 · Agency rates", f(p["heading"]), f(p["blurb"]), rows)
    d.with_bg(bgi(4))
    a = s["addons"]
    add = [{"item": "AI voice-over", "detail": "English or Arabic", "amount": _aed(r["agency-ai-voice-over"])},
           {"item": "Human voice artist", "detail": "English", "amount": _aed(r["agency-human-voice-over-english"])},
           {"item": "Human voice artist", "detail": "Arabic", "amount": _aed(r["agency-human-voice-over-arabic"])},
           {"item": "Additional aspect ratio", "detail": "Per video", "amount": _aed(r["agency-additional-aspect-ratio"])},
           {"item": "Additional revision round", "detail": "Beyond the two included",
            "amount": _aed(r["agency-additional-revision-round"])},
           {"item": "Quoted on request", "detail": a["on_request"], "amount": "On request"}]
    d.investment("06 · Add-ons", f(a["heading"]), f(a["blurb"]), add)
    d.with_bg(bgi(5))
    d.cards("07 · Terms", s["terms"]["heading"], [{**x, "body": f(x["body"])} for x in s["terms"]["cards"][:3]], None)
    d.with_bg(bgi(0))
    cl = s["close"]
    d.closing("08 · Start", cl["heading"], "", cl["cards"][:2], cl["steps"][:3], cl.get("signoff", ""))
    d.with_bg(bgi(1))
    name = (f"{customer} - Agency AI Production Rates - {today:%Y-%m-%d}.pdf" if customer
            else f"Sensa - Agency AI Production Rates v{s.get('version', '1.0')}.pdf")
    path = deck.to_pdf(d.html(), os.path.join(OUT, name))
    from pypdf import PdfReader
    return {"path": path, "name": name, "pages": len(PdfReader(path).pages), "planned": len(d.pages)}


def deliver_deck(slug: str = "sensa", customer: str | None = None, deal_id: int | None = None) -> dict:
    """The STANDARD deck is one of the company's own official documents: Documents on Drive, library kind
    'agency-deck' (a core kind, never hidden by a deal's scope), profile `agency_rates_deck_doc`, earlier
    standard versions retired. A version FOR an agency is client work: filed on its deal and client folder."""
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
