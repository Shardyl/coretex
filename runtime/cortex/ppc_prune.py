"""Daily PPC search-term pruning (operator-approved routine, 2026-08-26 "daily pruning it is").

Runs inside the daily ppc_report: yesterday's paid search terms are classified by Haiku
(buyer vs junk for a Dubai production house), junk terms are added as PHRASE negatives to the
shared set, and the daily card reports exactly what was pruned and why (operator can veto).
The shared set attaches to the buyer campaigns only — a future tool-capture campaign that
deliberately targets DIY phrases must never attach it.
"""
from __future__ import annotations

import datetime
import json
import urllib.request

import yaml

from . import ppc_report, provider

SHARED_SET_NAME = "Sensa PPC shared negatives"
MAX_ADDS_PER_DAY = 15

_SYSTEM = """You prune Google Ads search terms for Sensa Productions, a video production house
in Dubai. The ads sell PRODUCTION SERVICES: companies hiring a studio for commercials,
corporate films and AI-built films (deal sizes AED 20k+).

Classify each search term:
- keep: plausibly a buyer looking to HIRE a production company/studio/agency (any wording).
- junk: looking for something else - free/DIY tools or apps, a specific software brand, a
  PLACE or free zone (e.g. Dubai Media City, Production City), jobs/careers/salaries,
  courses/tutorials, equipment purchases, news, or an unrelated business.
When unsure, keep (a wrongly blocked buyer costs more than one junk click)."""


def _mutate_shared_criteria(cid: str, shared_set: str, terms: list[str]) -> None:
    cfg = yaml.safe_load(open(ppc_report._YAML, encoding="utf-8"))
    tok = ppc_report.seo_report._token(cfg)
    ops = [{"create": {"sharedSet": shared_set,
                       "keyword": {"text": t, "matchType": "PHRASE"}}} for t in terms]
    req = urllib.request.Request(
        f"https://googleads.googleapis.com/v25/customers/{cid}/sharedCriteria:mutate",
        data=json.dumps({"operations": ops}).encode(),
        headers={"Authorization": f"Bearer {tok}",
                 "developer-token": str(cfg["developer_token"]),
                 "login-customer-id": str(cfg["login_customer_id"]),
                 "Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30)


def run(company: str = "sensa") -> dict:
    """Classify yesterday's search terms, add junk as shared negatives.
    Returns {"pruned": [[term, reason]], "kept": [terms]} for the daily card."""
    acct = ppc_report.ACCOUNTS[company]
    cid = acct["cid"]
    day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    rows = ppc_report._ads_search(cid, f"""
        SELECT search_term_view.search_term, campaign.name, metrics.clicks, metrics.impressions
        FROM search_term_view WHERE segments.date = '{day}' AND metrics.impressions > 0
        ORDER BY metrics.clicks DESC LIMIT 60""")
    terms = []
    seen = set()
    for r in rows:
        t = (ppc_report._dig(r, "searchTermView.searchTerm", "") or "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            terms.append(t)
    if not terms:
        return {"pruned": [], "kept": []}

    # our own positive keywords are never negated
    kw_rows = ppc_report._ads_search(cid, """
        SELECT ad_group_criterion.keyword.text FROM keyword_view
        WHERE ad_group_criterion.negative = FALSE AND campaign.status = 'ENABLED'""")
    positives = {(ppc_report._dig(r, "adGroupCriterion.keyword.text", "") or "").lower()
                 for r in kw_rows}
    candidates = [t for t in terms if t not in positives]
    if not candidates:
        return {"pruned": [], "kept": terms}

    verdict = provider.think_json(
        _SYSTEM,
        "Search terms (one per line):\n" + "\n".join(candidates) +
        '\n\nReturn {"junk": [{"term": "...", "reason": "<5 words>"}, ...]} '
        "listing ONLY the junk terms.",
        fast=True, purpose="ppc_prune", company=company)
    junk = [(j.get("term", "").strip().lower(), j.get("reason", ""))
            for j in (verdict.get("junk") or []) if j.get("term")]
    junk = [(t, r) for t, r in junk if t in candidates][:MAX_ADDS_PER_DAY]
    if not junk:
        return {"pruned": [], "kept": terms}

    ss_rows = ppc_report._ads_search(cid, f"""
        SELECT shared_set.resource_name, shared_set.name FROM shared_set
        WHERE shared_set.name = '{SHARED_SET_NAME}'""")
    if not ss_rows:
        return {"pruned": [], "kept": terms}
    shared_set = ppc_report._dig(ss_rows[0], "sharedSet.resourceName", "")
    existing = {(ppc_report._dig(r, "sharedCriterion.keyword.text", "") or "").lower()
                for r in ppc_report._ads_search(cid, f"""
        SELECT shared_criterion.keyword.text FROM shared_criterion
        WHERE shared_criterion.shared_set = '{shared_set}'""")}
    to_add = [(t, r) for t, r in junk if t not in existing]
    if to_add:
        _mutate_shared_criteria(cid, shared_set, [t for t, _ in to_add])
    return {"pruned": [[t, r] for t, r in to_add],
            "kept": [t for t in terms if t not in {x for x, _ in to_add}]}
