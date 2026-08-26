"""Daily Google Ads PPC report for Sensa -> PDF, delivered as a downloadable Inbox card.

Pulls the Ads account over REST (searchStream) with the shared creds in /etc/cortex/google-ads.yaml
(developer_token + login_customer_id + OAuth refresh token), plus GA4 paid-traffic context via the
same helpers the SEO report uses. Every number is API data; nothing is estimated.
"""
from __future__ import annotations

import datetime
import json
import urllib.request
import os

import yaml
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import seo_report

_YAML = "/etc/cortex/google-ads.yaml"
ACCOUNTS = {"sensa": {"cid": "9859368995", "currency": "AED", "label": "Sensa"}}
ACCENT = colors.HexColor("#0b7285"); DARK = colors.HexColor("#15202b")
LIGHT = colors.HexColor("#eef3f5"); GREY = colors.HexColor("#667")


def _ads_search(cid: str, query: str) -> list[dict]:
    cfg = yaml.safe_load(open(_YAML, encoding="utf-8"))
    tok = seo_report._token(cfg)
    req = urllib.request.Request(
        f"https://googleads.googleapis.com/v25/customers/{cid}/googleAds:searchStream",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {tok}",
                 "developer-token": str(cfg["developer_token"]),
                 "login-customer-id": str(cfg["login_customer_id"]),
                 "Content-Type": "application/json"}, method="POST")
    try:
        chunks = json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ads API {e.code}: {e.read()[:200]}") from e
    rows = []
    for ch in chunks:
        rows.extend(ch.get("results") or [])
    return rows


def _dig(row: dict, path: str, default=0):
    cur = row
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _aed(micros) -> str:
    return f"{int(micros) / 1e6:,.2f}"


def generate(company: str = "sensa", days: int = 1, out_dir: str = "/tmp") -> dict:
    """Build the PPC PDF; returns {path,title,summary,...}. days=1 => yesterday (a full day of data)."""
    acct = ACCOUNTS.get((company or "").lower())
    if not acct:
        raise ValueError(f"no ads account mapped for {company}")
    # Daily search-term pruning (operator-approved routine 2026-08-26): junk terms become
    # shared negatives BEFORE the report renders, and the card shows what was pruned + why.
    prune = {"pruned": [], "kept": []}
    try:
        from . import ppc_prune
        prune = ppc_prune.run(company)
    except Exception:  # noqa: BLE001 — pruning must never block the report
        pass
    cid, cur, label = acct["cid"], acct["currency"], acct["label"]
    end = datetime.date.today() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=max(days, 1) - 1)
    rng = f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"

    camp = _ads_search(cid, f"""
      SELECT campaign.name, campaign.status, metrics.impressions, metrics.clicks,
             metrics.cost_micros, metrics.conversions, metrics.average_cpc
      FROM campaign WHERE {rng} AND campaign.status != 'REMOVED' ORDER BY metrics.cost_micros DESC""")
    # status even on zero-traffic days (the WHERE date range drops silent campaigns above)
    states = _ads_search(cid, "SELECT campaign.name, campaign.status FROM campaign "
                              "WHERE campaign.status != 'REMOVED'")
    conv = _ads_search(cid, f"""
      SELECT conversion_action.name, metrics.all_conversions
      FROM conversion_action WHERE {rng} AND metrics.all_conversions > 0""")
    terms = _ads_search(cid, f"""
      SELECT search_term_view.search_term, campaign.name, metrics.clicks, metrics.impressions,
             metrics.cost_micros FROM search_term_view WHERE {rng}
      ORDER BY metrics.clicks DESC LIMIT 12""")
    kws = _ads_search(cid, f"""
      SELECT ad_group_criterion.keyword.text, ad_group.name, metrics.clicks, metrics.impressions,
             metrics.cost_micros, metrics.conversions FROM keyword_view WHERE {rng}
      ORDER BY metrics.clicks DESC LIMIT 12""")

    tot_impr = sum(int(_dig(r, "metrics.impressions")) for r in camp)
    tot_clicks = sum(int(_dig(r, "metrics.clicks")) for r in camp)
    tot_cost = sum(int(_dig(r, "metrics.costMicros")) for r in camp)
    tot_conv = sum(float(_dig(r, "metrics.conversions")) for r in camp)
    enabled = [_dig(r, "campaign.name", "") for r in states if _dig(r, "campaign.status", "") == "ENABLED"]
    paused = [_dig(r, "campaign.name", "") for r in states if _dig(r, "campaign.status", "") == "PAUSED"]

    # GA4 paid-traffic context (where paid visitors land / go) — best effort, never blocks the report
    ga_pages = []
    try:
        ids = json.load(open(seo_report._IDS))
        seo_report._H = {"Authorization": f"Bearer {seo_report._company_token(company)}",
                         "Content-Type": "application/json"}
        pid = ids[seo_report.SITES[company][1]]["property"].split("/")[1]
        rep = seo_report._ga(pid, {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "landingPage"}], "metrics": [{"name": "sessions"}],
            "dimensionFilter": {"filter": {"fieldName": "sessionMedium",
                                           "stringFilter": {"value": "cpc"}}},
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}], "limit": 8})
        ga_pages = seo_report._rows(rep, 8)
    except Exception:  # noqa: BLE001
        ga_pages = []

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=21, textColor=DARK, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=GREY)
    LBL = ParagraphStyle("LBL", parent=ss["Normal"], fontSize=7.5, textColor=GREY, alignment=1)
    NUM = ParagraphStyle("NUM", parent=ss["Normal"], fontSize=17, textColor=DARK, alignment=1, leading=19)
    TH = ParagraphStyle("TH", parent=ss["Normal"], fontSize=8.5, textColor=ACCENT, fontName="Helvetica-Bold")
    TD = ParagraphStyle("TD", parent=ss["Normal"], fontSize=8.5, textColor=DARK)
    CAP = ParagraphStyle("CAP", parent=ss["Normal"], fontSize=9.5, textColor=DARK,
                         fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=3)

    def box(lbl, val):
        return Table([[Paragraph(str(val), NUM)], [Paragraph(lbl, LBL)]], colWidths=[40 * mm],
                     rowHeights=[10 * mm, 5 * mm],
                     style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                                       ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))

    def mini(title, headers, data, widths):
        out = [Paragraph(title, CAP)]
        if not data:
            out.append(Paragraph("<i>no data</i>", ParagraphStyle("ni", parent=TD, textColor=GREY)))
            return out
        t = Table([[Paragraph(h, TH) for h in headers]] +
                  [[Paragraph(str(c), TD) for c in row] for row in data],
                  colWidths=widths,
                  style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, ACCENT),
                                    ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#dde")),
                                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        out.append(t)
        return out

    span = end.strftime("%d %b %Y") if days == 1 else f"{start.strftime('%d %b')} - {end.strftime('%d %b %Y')}"
    status_line = (f"Live: {', '.join(enabled)}" if enabled else "All campaigns PAUSED (no spend possible)")
    if paused and enabled:
        status_line += f" · Paused: {', '.join(paused)}"
    story = [Paragraph(f"{label} — PPC report", H1),
             Paragraph(f"{span} &nbsp;·&nbsp; Google Ads {cid[:3]}-{cid[3:6]}-{cid[6:]} ({cur}) "
                       f"&nbsp;·&nbsp; {status_line}", SUB),
             HRFlowable(width="100%", thickness=1, color=ACCENT, spaceBefore=6, spaceAfter=12),
             Table([[box(f"SPEND ({cur})", _aed(tot_cost)), box("CLICKS", f"{tot_clicks:,}"),
                     box("IMPRESSIONS", f"{tot_impr:,}"), box("CONVERSIONS", f"{tot_conv:g}")]],
                   colWidths=[42 * mm] * 4, style=TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")])),
             Spacer(1, 4)]
    story += mini("Campaigns", ["Campaign", "Impr.", "Clicks", f"Cost {cur}", "Conv."],
                  [[_dig(r, "campaign.name", ""), f"{int(_dig(r, 'metrics.impressions')):,}",
                    int(_dig(r, "metrics.clicks")), _aed(_dig(r, "metrics.costMicros")),
                    f"{float(_dig(r, 'metrics.conversions')):g}"] for r in camp],
                  [66 * mm, 22 * mm, 20 * mm, 30 * mm, 20 * mm])
    story += mini("Contacts by type (conversions)", ["Action", "Count"],
                  [[_dig(r, "conversionAction.name", ""), f"{float(_dig(r, 'metrics.allConversions')):g}"]
                   for r in conv], [120 * mm, 40 * mm])
    story += mini("What people searched (top by clicks)", ["Search term", "Campaign", "Clicks", f"Cost {cur}"],
                  [[_dig(r, "searchTermView.searchTerm", ""), _dig(r, "campaign.name", ""),
                    int(_dig(r, "metrics.clicks")), _aed(_dig(r, "metrics.costMicros"))]
                   for r in terms], [62 * mm, 52 * mm, 18 * mm, 26 * mm])
    story += mini("Keywords (top by clicks)", ["Keyword", "Ad group", "Clicks", f"Cost {cur}", "Conv."],
                  [[_dig(r, "adGroupCriterion.keyword.text", ""), _dig(r, "adGroup.name", ""),
                    int(_dig(r, "metrics.clicks")), _aed(_dig(r, "metrics.costMicros")),
                    f"{float(_dig(r, 'metrics.conversions')):g}"] for r in kws],
                  [52 * mm, 44 * mm, 18 * mm, 26 * mm, 18 * mm])
    story += mini("Where paid visitors landed (GA4, medium=cpc)", ["Landing page", "Sessions"],
                  ga_pages, [130 * mm, 40 * mm])
    story += mini("Pruned yesterday (auto-added as negatives, veto any of these to restore)",
                  ["Search term", "Why"],
                  [[t, r] for t, r in prune.get("pruned", [])], [90 * mm, 80 * mm])

    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"ppc-{company}-{end.isoformat()}.pdf")
    SimpleDocTemplate(out, pagesize=A4, topMargin=15 * mm, bottomMargin=14 * mm, leftMargin=18 * mm,
                      rightMargin=18 * mm, title=f"{label} PPC report").build(story)

    conv_bits = ", ".join(f"{_dig(r, 'conversionAction.name', '')}: "
                          f"{float(_dig(r, 'metrics.allConversions')):g}" for r in conv) or "no conversions"
    prune_bit = ""
    if prune.get("pruned"):
        prune_bit = " Pruned: " + ", ".join(t for t, _ in prune["pruned"][:6]) + "."
    summary = (f"{label} PPC, {span}: {cur} {_aed(tot_cost)} spent, {tot_clicks:,} clicks, "
               f"{tot_impr:,} impressions, {tot_conv:g} conversions ({conv_bits}). "
               f"{status_line}.{prune_bit}")
    return {"path": out, "title": f"{label} — PPC report ({span})", "summary": summary,
            "company": company, "label": label, "days": days}
