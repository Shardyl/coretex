"""Per-company SEO / traffic report -> PDF (GA4 + Search Console), for scheduled delivery on the box.

Ported from the local weekly report (seo-campaign skill), scoped to ONE company per call so the calendar
can schedule a clean per-company report. Creds: /etc/cortex/google-ads.yaml + ga4-measurement-ids.json.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.parse
import urllib.request

import yaml
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_YAML = "/etc/cortex/google-ads.yaml"
_IDS = "/etc/cortex/ga4-measurement-ids.json"
SITES = {"tabscanner": ("Tabscanner", "tabscanner.com", "https://tabscanner.com/"),
         "sensa": ("Sensa", "sensa.digital", "https://sensa.digital/"),
         "skyvision": ("SkyVision", "skyvision.film", "https://skyvision.film/"),
         "filmspoke": ("FilmSpoke", "filmspoke.ai", "https://filmspoke.ai/"),
         "snaprewards": ("Snap Rewards", "snap-rewards.com", "https://snap-rewards.com/")}
ACCENT = colors.HexColor("#0b7285"); DARK = colors.HexColor("#15202b")
LIGHT = colors.HexColor("#eef3f5"); GREY = colors.HexColor("#667")
CC = {"are": "UAE", "usa": "USA", "gbr": "UK", "ind": "India", "esp": "Spain", "lbn": "Lebanon",
      "nga": "Nigeria", "phl": "Philippines", "sau": "Saudi Arabia", "deu": "Germany", "pak": "Pakistan",
      "egy": "Egypt", "qat": "Qatar", "kwt": "Kuwait", "can": "Canada", "fra": "France"}
_H: dict = {}


def available() -> dict:
    return {k: v[0] for k, v in SITES.items()}


def _token(cfg):
    data = urllib.parse.urlencode({"client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                   "refresh_token": cfg["refresh_token"], "grant_type": "refresh_token"}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data)))["access_token"]


def _company_token(company: str) -> str:
    """Per-company analytics access token: the company's OWN Internal OAuth client + analytics_refresh_token:<company>.
    Falls back to the legacy shared google-ads.yaml creds (personal Gmail) for any company not yet migrated, so
    reports never break mid-migration."""
    try:
        from . import db
        rt = db.setting_get(f"analytics_refresh_token:{company}")
        cf = f"/etc/cortex/google_oauth_client_{company}.json"
        if rt and os.path.exists(cf):
            c = json.load(open(cf))["web"]
            return _token({"client_id": c["client_id"], "client_secret": c["client_secret"], "refresh_token": rt})
    except Exception:  # noqa: BLE001 — never break a report on the per-company path; fall back to shared creds
        pass
    return _token(yaml.safe_load(open(_YAML, encoding="utf-8")))


def _post(url, body):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=_H, method="POST")))
    except urllib.error.HTTPError as e:
        return {"_error": str(e.code)}


def _get(url):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=_H)))
    except urllib.error.HTTPError as e:
        return {"_error": str(e.code)}


def _ga(pid, body):
    return _post(f"https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport", body)


def _first(rep, i=0):
    return rep["rows"][0]["metricValues"][i]["value"] if rep.get("rows") else "0"


def _rows(rep, n=5):
    return [(r["dimensionValues"][0]["value"] or "(none)", r["metricValues"][0]["value"])
            for r in (rep.get("rows") or [])[:n]]


def _gsc_ok(url):
    info = _get("https://www.googleapis.com/webmasters/v3/sites/" + urllib.parse.quote(url, safe=""))
    return info.get("permissionLevel") in ("siteOwner", "siteFullUser")


def _gsc(url, days, dims, n):
    enc = urllib.parse.quote(url, safe="")
    end = datetime.date.today(); start = end - datetime.timedelta(days=days)
    r = _post(f"https://www.googleapis.com/webmasters/v3/sites/{enc}/searchAnalytics/query",
              {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dims, "rowLimit": 250})
    return (r.get("rows") or [])[:n] if "_error" not in r else []


def generate(company: str, days: int = 28, out_dir: str = "/tmp") -> str:
    """Build the per-company SEO/traffic PDF; returns the file path."""
    global _H
    company = (company or "").lower()
    if company not in SITES:
        raise ValueError(f"no site for {company}")
    label, key, url = SITES[company]
    ids = json.load(open(_IDS))
    _H = {"Authorization": f"Bearer {_company_token(company)}", "Content-Type": "application/json"}
    pid = ids[key]["property"].split("/")[1]
    dr = [{"startDate": f"{days}daysAgo", "endDate": "today"}]
    tot = _ga(pid, {"dateRanges": dr, "metrics": [{"name": "activeUsers"}, {"name": "sessions"},
                                                  {"name": "newUsers"}, {"name": "screenPageViews"}]})
    chan = _rows(_ga(pid, {"dateRanges": dr, "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                           "metrics": [{"name": "sessions"}],
                           "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}], "limit": 5}))
    pages = _rows(_ga(pid, {"dateRanges": dr, "dimensions": [{"name": "pagePath"}],
                            "metrics": [{"name": "screenPageViews"}],
                            "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}], "limit": 5}))
    ok = _gsc_ok(url)
    queries = [(r["keys"][0], int(r["clicks"]), int(r["impressions"]), round(r["position"], 1))
               for r in _gsc(url, days, ["query"], 8)] if ok else None
    opps = None
    if ok:
        rr = [r for r in _gsc(url, days, ["query"], 250) if r["position"] > 10 and r["impressions"] >= 15]
        rr.sort(key=lambda r: r["impressions"], reverse=True)
        opps = [(r["keys"][0], int(r["impressions"]), int(r["clicks"]), round(r["position"], 1)) for r in rr[:6]]

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=21, textColor=DARK, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=GREY)
    LBL = ParagraphStyle("LBL", parent=ss["Normal"], fontSize=7.5, textColor=GREY, alignment=1)
    NUM = ParagraphStyle("NUM", parent=ss["Normal"], fontSize=17, textColor=DARK, alignment=1, leading=19)
    TH = ParagraphStyle("TH", parent=ss["Normal"], fontSize=8.5, textColor=ACCENT, fontName="Helvetica-Bold")
    TD = ParagraphStyle("TD", parent=ss["Normal"], fontSize=8.5, textColor=DARK)
    CAP = ParagraphStyle("CAP", parent=ss["Normal"], fontSize=9.5, textColor=DARK, fontName="Helvetica-Bold",
                         spaceBefore=8, spaceAfter=3)

    def box(lbl, val):
        return Table([[Paragraph(str(val), NUM)], [Paragraph(lbl, LBL)]], colWidths=[40 * mm],
                     rowHeights=[10 * mm, 5 * mm],
                     style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))

    def mini(title, headers, data, widths):
        out = [Paragraph(title, CAP)]
        if not data:
            out.append(Paragraph("<i>no data yet</i>", ParagraphStyle("ni", parent=TD, textColor=GREY)))
            return out
        t = Table([[Paragraph(h, TH) for h in headers]] + [[Paragraph(str(c), TD) for c in row] for row in data],
                  colWidths=widths, style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, ACCENT),
                  ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#dde")),
                  ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        out.append(t)
        return out

    today = datetime.date.today().strftime("%d %b %Y")
    story = [Paragraph(f"{label} — SEO & Traffic Report", H1),
             Paragraph(f"Last {days} days &nbsp;·&nbsp; {today} &nbsp;·&nbsp; Google Analytics 4 + Search Console", SUB),
             HRFlowable(width="100%", thickness=1, color=ACCENT, spaceBefore=6, spaceAfter=12),
             Table([[box("USERS", _first(tot, 0)), box("SESSIONS", _first(tot, 1)),
                     box("NEW USERS", _first(tot, 2)), box("PAGEVIEWS", _first(tot, 3))]],
                   colWidths=[42 * mm] * 4, style=TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")])),
             Spacer(1, 4)]
    story += mini("Top channels", ["Channel", "Sessions"], chan, [110 * mm, 60 * mm])
    story += mini("Top pages", ["Page", "Views"], pages, [130 * mm, 40 * mm])
    if queries is None:
        story.append(Paragraph("<i>Search Console not verified for this site.</i>",
                               ParagraphStyle("ni", parent=TD, textColor=GREY)))
    else:
        story += mini("Top search queries", ["Query", "Clicks", "Impr.", "Avg pos"], queries,
                      [95 * mm, 25 * mm, 25 * mm, 25 * mm])
        if opps:
            story += mini("Opportunities — already on page 2+, a push gets them to page 1",
                          ["Query", "Impr.", "Clicks", "Avg pos"], opps, [95 * mm, 25 * mm, 25 * mm, 25 * mm])
    out = os.path.join(out_dir, f"seo-{company}-{datetime.date.today().isoformat()}.pdf")
    SimpleDocTemplate(out, pagesize=A4, topMargin=15 * mm, bottomMargin=14 * mm, leftMargin=18 * mm,
                      rightMargin=18 * mm, title=f"{label} SEO Report").build(story)

    top_q = ""
    if queries:
        top_q = f" Top query: “{queries[0][0]}” ({queries[0][1]} clicks)."
    ga_err = (f" [GA4 ACCESS ERROR {tot['_error']} - check property access + Analytics Data API enabled]"
              if isinstance(tot, dict) and tot.get("_error") else "")
    gsc_note = ("" if queries is not None
                else " [Search Console: no access - check Full/Owner grant + Search Console API enabled]")
    summary = (f"{label}, last {days} days: {_first(tot, 0)} users, {_first(tot, 1)} sessions, "
               f"{_first(tot, 2)} new, {_first(tot, 3)} pageviews.{top_q}{ga_err}{gsc_note}")
    return {"path": out, "title": f"{label} — SEO & Traffic Report", "summary": summary,
            "company": company, "label": label, "days": days}
