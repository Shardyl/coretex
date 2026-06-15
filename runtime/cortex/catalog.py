"""The canonical Cortex skills catalog — the full granular map (Cortex-Skills-Roadmap).

Every company gets EVERY department and EVERY skill, created up front (empty: authority=ask,
no rules yet) so Cortex and Rashad both map to the same skill sheets. Skills are tuned (rules
added, graduated to auto) one at a time. Source of truth = this file; keep it in sync with the
roadmap doc and §A/§B of CORTEX-SPEC.md.

Structure: (category, department, manager, [(skill_key, name), ...]).
"""
from __future__ import annotations

CATALOG = [
    ("Demand", "Content & SEO", "Content manager", [
        ("content-blog-posts", "SEO blog posts (research, draft, optimise, publish)"),
        ("content-keyword-research", "Keyword research & content-gap analysis"),
        ("content-landing-copy", "Website & landing-page copy"),
        ("content-newsletter", "Newsletter writing"),
        ("content-editorial-calendar", "Editorial calendar & topic planning"),
        ("content-repurposing", "Content repurposing (blog into social & email)"),
        ("content-onpage-seo", "On-page SEO, meta & schema markup"),
        ("content-refresh", "Content refresh (update & re-optimise old posts)"),
        ("content-internal-linking", "Internal linking strategy"),
        ("content-aeo", "Answer-engine optimisation (AEO / GEO)"),
    ]),
    ("Demand", "Social (organic)", "Social manager", [
        ("social-instagram-posts", "Instagram posts & carousels"),
        ("social-instagram-reels", "Instagram Reels (hooks, scripts, captions)"),
        ("social-linkedin-posts", "LinkedIn posts (personal + company)"),
        ("social-facebook-posts", "Facebook posts"),
        ("social-youtube", "YouTube (titles, descriptions, scripts, chapters)"),
        ("social-captions", "Caption & hook writing"),
        ("social-dm-replies", "DM & comment replies"),
        ("social-strategy", "Hashtag, timing & format strategy"),
        ("social-listening", "Social listening / mention monitoring"),
        ("social-calendar", "Per-platform content calendar"),
    ]),
    ("Demand", "Paid Ads", "Ads manager", [
        ("ads-google-search", "Google Search ads (copy + keywords)"),
        ("ads-google-pmax", "Google Performance Max / Display"),
        ("ads-meta", "Meta ads (Facebook + Instagram)"),
        ("ads-linkedin", "LinkedIn ads"),
        ("ads-creative-briefs", "Ad-creative briefs (feeds Creative / AddDrop)"),
        ("ads-audiences", "Audience building & retargeting"),
        ("ads-ab-testing", "A/B testing & creative rotation"),
        ("ads-budget-pacing", "Budget pacing & bid management [gated - spend]"),
        ("ads-reporting", "Performance reporting & optimisation"),
    ]),
    ("Demand", "Outreach & Lead Gen", "Outreach manager", [
        ("outreach-cold-email-campaigns", "Cold-email campaigns (Instantly)"),
        ("outreach-cold-email-copy", "Cold-email copy & sequences"),
        ("outreach-linkedin-sequences", "LinkedIn outreach sequences"),
        ("outreach-lead-list", "Lead-list building / sourcing"),
        ("outreach-lead-enrichment", "Lead enrichment (emails, titles, firmographics)"),
        ("outreach-icp", "ICP & targeting definition"),
        ("outreach-reply-routing", "Reply handling & routing (warm to CRM)"),
        ("outreach-crm-sync", "CRM sync (Bitrix)"),
        ("outreach-deliverability", "Deliverability / inbox-health monitoring"),
    ]),
    ("Convert", "Sales & Inquiries", "Sales manager", [
        ("sales-triage", "Inquiry triage & routing (by type / company)"),
        ("sales-first-response", "Inquiry first-response drafting"),
        ("sales-quotation", "Quotation generation"),
        ("sales-proposal", "Proposal writing"),
        ("sales-followup", "Follow-up & nurture sequences"),
        ("sales-reactivation", "Dead-lead reactivation (revive warm-then-cold)"),
        ("sales-scheduling", "Meeting / call scheduling"),
        ("sales-quote-chasing", "Quote follow-up & chasing"),
        ("sales-objections", "Objection handling & FAQ responses"),
    ]),
    ("Convert", "PR & Reputation", "PR manager", [
        ("pr-press-release", "Press-release writing"),
        ("pr-media-list", "Media-list building & journalist outreach"),
        ("pr-pitching", "Podcast / feature pitching"),
        ("pr-review-requests", "Review requests (ask happy clients)"),
        ("pr-review-responses", "Review responses"),
        ("pr-monitoring", "Brand-mention & reputation monitoring"),
        ("pr-case-studies", "Case studies & testimonials"),
        ("pr-awards", "Awards & directory submissions"),
    ]),
    ("Deliver", "Production & Projects", "Production manager", [
        ("prod-pipeline", "Project pipeline management (stages)"),
        ("prod-stage-automations", "Stage automations (contract on Booked, invoice on Delivered)"),
        ("prod-briefs", "Creative production briefs (feeds AddDrop)"),
        ("prod-scheduling", "Shoot scheduling & logistics"),
        ("prod-asset-library", "Asset library - ingest, tag, organise"),
        ("prod-packaging", "Deliverable packaging & client handoff"),
        ("prod-status-reporting", "Project status reporting"),
        ("prod-revisions", "Revision & feedback handling"),
    ]),
    ("Deliver", "Customer Support", "Support manager", [
        ("support-tickets", "Helpdesk / support-ticket replies"),
        ("support-kb", "Knowledge-base / FAQ answers"),
        ("support-onboarding", "Client onboarding sequences"),
        ("support-nps", "Satisfaction / NPS follow-ups"),
        ("support-feature-requests", "Feature-request logging & routing"),
        ("support-escalation", "Issue escalation"),
    ]),
    ("Run the business", "Finance & Admin", "Finance manager", [
        ("finance-invoice-creation", "Invoice creation (from projects / quotes)"),
        ("finance-invoice-sending", "Invoice sending [gated - owner approves]"),
        ("finance-chasing", "Payment chasing & reminders"),
        ("finance-reconciliation", "Payment reconciliation & tracking"),
        ("finance-quote-to-invoice", "Quote-to-invoice conversion"),
        ("finance-expenses", "Expense tracking & categorisation"),
        ("finance-recurring-billing", "Recurring / subscription billing"),
        ("finance-reporting", "Financial reporting (owed, revenue, per company)"),
        ("finance-bookkeeping-sync", "Bookkeeping sync (Xero / QuickBooks)"),
    ]),
]

# Higher-stakes (slower to graduate to auto); Finance dept + ad-spend are gated.
GATED = {"ads-budget-pacing"} | {k for _, dept, _, skills in CATALOG if dept == "Finance & Admin"
                                 for k, _ in skills}

# The four companies in scope (context packs from CORTEX-SPEC §A).
COMPANIES = [
    ("tabscanner", "Tabscanner", "owned", "Enterprise / sales-qualified leads", {
        "voice": "Technical, credible, accuracy-first, B2B. Concrete and specific, never hypey.",
        "audience": "Developers, product teams, and fintech / expense / loyalty companies evaluating receipt OCR.",
        "products": "Receipt-OCR / expense-data-extraction (EDE) API with high accuracy across global formats.",
        "donts": "No unqualified financial/tax (YMYL) advice. No vague claims without proof."}),
    ("sensa", "Sensa Productions", "owned", "Qualified production inquiries", {
        "voice": "Cinematic, premium, creative.",
        "audience": "Brands, agencies and marketing leads.",
        "products": "Video / film production (incl. AI-assisted)."}),
    ("skyvision", "Sky Vision", "owned", "Qualified inquiries", {
        "voice": "Dynamic, cinematic.",
        "audience": "Brands and agencies needing aerial / drone film.",
        "products": "Aerial / drone film production (skyvision.film)."}),
    ("filmspoke", "FilmSpoke", "owned", "Waitlist & early adopters", {
        "voice": "Bold, innovative.",
        "audience": "Brands wanting AI-made commercials.",
        "products": "AI commercial 'creatives' (finished commercial films). Products are called creatives.",
        "donts": "No Dubai imagery."}),
]


def dept_meta(department: str):
    """Return (category, manager) for a department name, or (None, None)."""
    for cat, dept, mgr, _ in CATALOG:
        if dept.lower() == (department or "").lower():
            return cat, mgr
    return None, None


def _craft(name: str) -> str:
    return (f"{name}. Do this to a high standard in the company's voice — lead with what matters, be "
            f"specific and accurate, and follow this skill's standing rules. Ask the owner when unsure.")


def seed_all() -> dict:
    """Create every company + every skill (idempotent). Returns counts."""
    from . import store
    for slug, name, kind, north, ctx in COMPANIES:
        store.upsert_company(slug, name, kind, context=ctx, north_star=north)
    n = 0
    for cat, dept, mgr, skills in CATALOG:
        for key, name in skills:
            stakes = "high" if key in GATED else "low"
            for slug, *_ in COMPANIES:
                co = store.get_company_by_slug(slug)
                store.upsert_skill(co["id"], key, name, craft=_craft(name), authority="ask",
                                   stakes=stakes, category=cat, department=dept, manager=mgr)
                n += 1
    return {"companies": len(COMPANIES), "skills_per_company": sum(len(s) for *_, s in CATALOG),
            "rows": n}
