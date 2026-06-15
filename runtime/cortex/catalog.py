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
        ("content-blog-posts", "SEO blog posts (research, draft, optimise, publish)",
         "Write genuinely useful, original posts that earn the ranking, not thin SEO filler (Google's "
         "helpful-content bar). FIRST read the live SERP for the target keyword and match the format "
         "Google rewards (guide vs listicle vs tool vs service page) - the wrong format won't rank. Lead "
         "with the answer (inverted pyramid), back every claim with specifics and data, and structure for "
         "skim-reading: question-style H2s, short paragraphs, and a self-contained answer near the top of "
         "each section so AI Overviews can lift it. Put the primary keyword in the title, H1, URL slug and "
         "first ~100 words naturally, never stuffed. Include an FAQ block (with schema) built from real "
         "People-Also-Ask questions. Reinforce the company's one-line entity claim. Hold the company's voice; "
         "no hype, no fluff, no em-dashes."),
        ("content-keyword-research", "Keyword research & content-gap analysis",
         "Find what the audience actually searches and the intent behind it. Read the live SERP for each "
         "candidate term to capture the page FORMAT Google rewards, the real difficulty (who ranks, are they "
         "authority sites), and whether intent is commercial / informational / navigational. Mine autocomplete, "
         "People-Also-Ask and related searches for long-tail and the exact phrasings people use. Where real data "
         "exists (Google Ads keyword ideas, or best of all the Search Terms Report of queries that actually "
         "converted), re-rank by proven demand and intent, not guesses. Output a keyword/cluster map (primary + "
         "secondaries + question-set per cluster) and name the gaps competitors are winning. Decide multilingual "
         "relevance deliberately for self-serve, language-agnostic products."),
        ("content-landing-copy", "Website & landing-page copy",
         "Write landing and service-page copy that converts AND ranks. Detect the page type from the winning "
         "SERP and build to it. Hero with the primary-keyword H1 and the crisp entity claim (what the company "
         "is, does and where), then value props, proof/portfolio, a service or feature grid, an FAQ block with "
         "schema, and a strong CTA. Lead with outcomes, be specific, and vary the section framing down the page "
         "(never two identical layouts in a row). Bake in the on-page SEO + AEO checklist: keyword in the "
         "structural slots, question-headings with extractable answers, descriptive internal links. Match the "
         "company's brand voice exactly."),
        ("content-newsletter", "Newsletter writing",
         "Write newsletters people open and read. A subject line that earns the open (specific, not clickbait), "
         "a strong first line, ONE clear primary message, scannable short sections, and a single clear CTA. "
         "Repurpose recent content into a tight, valuable digest in the company's voice; give a reason to reply; "
         "respect unsubscribes and avoid spam-trigger styling."),
        ("content-editorial-calendar", "Editorial calendar & topic planning",
         "Plan what to publish and when, driven by the keyword/cluster map and the company's north star. Sequence "
         "pillar pages then their supporting cluster posts (topic clusters), balance commercial-intent pieces with "
         "informational/AEO ones, and slot in refreshes of decaying posts. Each item carries its target keyword, "
         "intent, the SERP-rewarded format, and the cluster it fills. Set a cadence the team can actually sustain; "
         "prioritise by demand x conversion potential x gap."),
        ("content-repurposing", "Content repurposing (blog into social & email)",
         "Turn one piece into many without dilution. From a blog post, cut platform-native social posts "
         "(LinkedIn, Instagram, X), a newsletter section, and short hooks or threads - each REWRITTEN for that "
         "channel's format and audience, not copy-pasted. Pull the strongest stat, quote or insight as the hook. "
         "Keep the entity claim and voice consistent and link back to the source where it fits."),
        ("content-onpage-seo", "On-page SEO, meta & schema markup",
         "Make every page technically findable. One H1 with the primary keyword, a logical H2/H3 outline with "
         "secondaries in subheads, a compelling ~150-character meta description containing the primary term, "
         "descriptive alt text and meaningful image filenames, a clean keyword-in-slug URL, and the right JSON-LD "
         "schema for the page type (Organization/LocalBusiness, Service, FAQPage, Article). Keep it fast and "
         "mobile-clean for Core Web Vitals. Anchor the keyword in the structural slots; never stuff."),
        ("content-refresh", "Content refresh (update & re-optimise old posts)",
         "Audit and re-optimise existing posts against Google's helpful-content, E-E-A-T and scaled-content "
         "standards. Score each for genuine usefulness, originality, accuracy and intent-match; flag thin, "
         "outdated, duplicated or AI-filler content. Update facts and stats, tighten to lead with the answer, add "
         "missing depth and E-E-A-T signals, fix on-page SEO and internal links, and strengthen the FAQ for AEO. "
         "Prioritise pages that are decaying or one push from page one. Improve or consolidate; don't leave "
         "low-value pages indexable."),
        ("content-internal-linking", "Internal linking strategy",
         "Build a deliberate internal link graph. Link every new page in three ways when it goes live: from the "
         "pillar/hub and a high-traffic page out to it; from sibling cluster pages wherever their copy naturally "
         "names the topic; and from relevant existing posts. Use descriptive natural anchor text (never 'click "
         "here'), 1-2 contextual links per source, only on real relevance - forced links read as spam. Keep links "
         "root-relative; strengthen the cluster's topical authority and steer users toward conversion pages."),
        ("content-aeo", "Answer-engine optimisation (AEO / GEO)",
         "Win the AI answer, not just the blue links - be the named answer or a cited source in Google AI "
         "Overviews and assistants (ChatGPT, Claude, Perplexity, Gemini). Structure pages as questions to short, "
         "self-contained, extractable answers near the top; add FAQ schema; repeat one crisp, consistent entity "
         "claim (what the company is, does and where) across the site so models confidently name it; and create "
         "the comparison/decision shapes assistants synthesise from ('X vs Y', 'how much does X cost', 'best X "
         "for [use-case]'). Make the site agent-ready: a sane robots.txt for AI crawlers, an llms.txt, clean "
         "structured content (for an API/SaaS product an MCP server is a real growth channel). Track it: "
         "periodically ask the assistants the target questions and record whether the company is named."),
        ("content-page-builder", "Web page builder (structure, imagery, publish)",
         "Turn a brief or ramble into a complete, on-brand web page. FIRST do the SEO/AEO research: primary "
         "keyword, the page FORMAT the live SERP rewards, the question-set, and the single entity claim the "
         "page must reinforce. Detect the page type and structure to its pattern - landing: hero with the "
         "keyword H1 + value props + proof/portfolio + feature grid + FAQ (schema) + strong CTA; deep/module: "
         "hero + thesis + numbered steps with alternating media; blog: lead + sectioned body. VARY the section "
         "framing down the page (never two identical layouts in a row); for richer pages add bespoke CSS "
         "graphics and subtle motion, not just stock images. Generate section imagery that is cinematic, "
         "on-brand and optimised, with descriptive alt text and meaningful filenames. Bake in the on-page SEO "
         "+ AEO checklist (keyword in title/H1/URL/first-100-words, question-headings with extractable answers, "
         "FAQ schema, descriptive internal links). Hold the company's design system and voice; no em-dashes. "
         "Build it as a WordPress DRAFT and hand over the preview link - never publish until approved."),
        ("content-website-management", "Website management & deployment",
         "Manage and maintain the company's WordPress site to one consistent standard. Deploy via the "
         "Git -> WP Engine workflow (push to main, CI sync, then flush the cache). Keep the standard plugin "
         "stack and the in-place CMS editor pattern so every page stays editable without code. Make sure "
         "SSL/HTTPS is valid and, at go-live, DNS points directly at the host (no proxy in front unless "
         "decided). Every new or rebuilt page ships DEINDEXED (noindex + unlinked) by default and is flipped "
         "to indexed - with nav, internal links and sitemap added - only on explicit approval. On a site "
         "migration, verify content parity and run the pre-go-live checklist before cutover. Keep media "
         "optimised and changes versioned; follow the new-site and existing-site checklists, and be clear on "
         "who does what (operator vs Cortex)."),
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
                                 for k, *_ in skills}

# Workers default to Sonnet (cheap, fast). These quality-critical, client-facing skills override to
# Opus (the per-skill "use the better brain" flag the operator locked: Sonnet default + Opus override).
OPUS_SKILLS = {
    "content-blog-posts", "content-landing-copy", "content-page-builder", "content-newsletter",
    "sales-quotation", "sales-proposals", "pr-press-release",
}

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
        for key, name, *rest in skills:
            craft = rest[0] if rest else _craft(name)   # explicit craft when migrated, else generic
            stakes = "high" if key in GATED else "low"
            model = "opus" if key in OPUS_SKILLS else None   # None = Sonnet default
            for slug, *_ in COMPANIES:
                co = store.get_company_by_slug(slug)
                store.upsert_skill(co["id"], key, name, craft=craft, authority="ask",
                                   stakes=stakes, category=cat, department=dept, manager=mgr, model=model)
                n += 1
    return {"companies": len(COMPANIES), "skills_per_company": sum(len(s) for *_, s in CATALOG),
            "rows": n}
