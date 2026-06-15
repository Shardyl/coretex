"""Seed the operator's hard-won house rules as UNIVERSAL skill rules.

These are the concrete, recognisable directives distilled from Rashad's existing
skills (seo-campaign, web-page-builder, website-management, content-backtrack-audit).
The skill CRAFT carries the method; these RULES are the non-negotiable house policies
layered on top, visible in the 🌐 Universal section of every company's skill.

Idempotent: a rule is added only if its exact text isn't already present, so it never
duplicates and never clobbers rules added by hand. Re-runnable any time.
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/opt/coretex/runtime")

from cortex import store  # noqa: E402

# skill_key -> list of universal rules (all companies)
RULES: dict[str, list[str]] = {
    "content-onpage-seo": [
        "Put the primary keyword in the title, the single H1, the URL slug and the first ~100 words — naturally, never stuffed.",
        "One H1 per page, then a logical H2/H3 outline with secondary keywords living in the subheads.",
        "Meta description ~150 characters, compelling, and contains the primary term.",
        "Every image gets descriptive alt text and a meaningful filename (loc-hero.jpg, not img1.jpg).",
        "Add the right JSON-LD schema for the page type: Organization/LocalBusiness, Service, FAQPage, Article.",
        "Keep pages fast and mobile-clean (Core Web Vitals).",
    ],
    "content-blog-posts": [
        "Read the live SERP for the target keyword FIRST and match the format Google rewards (guide vs listicle vs tool vs service page).",
        "Lead with the answer (inverted pyramid); keep a self-contained answer near the top of each section so AI Overviews can lift it.",
        "Include an FAQ block with schema, built from real People-Also-Ask questions.",
        "Reinforce the company's one-line entity claim; hold the brand voice; no hype, no fluff.",
        "Build the post as a WordPress DRAFT and hand over the preview link — never publish until the operator approves.",
        "No em-dashes or en-dashes (—, –) in visible body copy; use commas, colons, periods.",
    ],
    "content-landing-copy": [
        "Detect the page type from the winning SERP and build to that format.",
        "Hero leads with the primary-keyword H1 and the crisp entity claim (what the company is, does and where).",
        "Vary the section framing down the page — never two identical layouts in a row.",
        "Bake in the on-page SEO + AEO checklist: keyword in the structural slots, question-headings with extractable answers, descriptive internal links.",
        "Match the company's brand voice exactly; no em-dashes in body copy.",
    ],
    "content-keyword-research": [
        "Read the live SERP for each candidate term to capture the format Google rewards and the real difficulty (who ranks, are they authority sites).",
        "Where real data exists (Google Ads keyword ideas, or best of all the converting Search Terms Report), re-rank by proven demand and intent, not guesses.",
        "Output a keyword/cluster map: primary + secondaries + a question-set per cluster, and name the gaps competitors are winning.",
    ],
    "content-editorial-calendar": [
        "Sequence pillar pages first, then their supporting cluster posts (topic clusters).",
        "Balance commercial-intent pieces with informational/AEO ones, and slot in refreshes of decaying posts.",
        "Each item carries its target keyword, intent, the SERP-rewarded format, and the cluster it fills.",
    ],
    "content-internal-linking": [
        "Link every new page in three ways when it goes live: from the pillar/hub + a high-traffic page; from sibling cluster pages where the copy names the topic; and from relevant existing posts.",
        "Use descriptive natural anchor text — never 'click here'.",
        "1-2 contextual links per source page, only on real relevance; forced links read as spam.",
        "Keep internal links root-relative.",
    ],
    "content-aeo": [
        "Repeat ONE crisp, consistent entity claim (what the company is, does and where) across the whole site so models name it confidently.",
        "Structure pages as questions with short, self-contained, extractable answers near the top, plus FAQ schema.",
        "Create the comparison/decision shapes assistants synthesise from ('X vs Y', 'how much does X cost', 'best X for [use-case]').",
        "Keep the site agent-ready: a sane robots.txt for AI crawlers and an llms.txt.",
        "Track it: periodically ask ChatGPT, Claude, Perplexity and Gemini the target questions and record whether the company is named.",
    ],
    "content-refresh": [
        "Score each existing post for genuine usefulness, originality, accuracy and intent-match against Google's helpful-content + E-E-A-T standards.",
        "Prioritise pages that are decaying or one push from page one.",
        "Improve or consolidate low-value pages; never leave thin, outdated or AI-filler pages indexable.",
    ],
    "content-repurposing": [
        "From one blog post, cut platform-native posts (LinkedIn, Instagram, X), a newsletter section and short hooks — each REWRITTEN for that channel, never copy-pasted.",
        "Pull the strongest stat, quote or insight as the hook; keep the entity claim and voice consistent; link back to the source where it fits.",
    ],
    "content-page-builder": [
        "Do the SEO/AEO research FIRST (primary keyword, the SERP-rewarded page format, the question-set, the single entity claim) before writing a line.",
        "Vary the section framing down the page — never two identical layouts in a row; for richer pages add bespoke CSS graphics and subtle motion, not just stock images.",
        "CMS-wire every page: all copy and content images editable through the CMS from day one, never hardcoded.",
        "Generate section imagery cinematic, on-brand and optimised, with descriptive alt text and meaningful filenames.",
        "Build the page as a WordPress DRAFT and hand over the preview link — never publish until the operator approves.",
        "Every new or rebuilt page ships DEINDEXED (noindex + unlinked) by default and stays that way until the operator explicitly approves indexing.",
        "No em-dashes or en-dashes in visible body copy.",
    ],
    "content-website-management": [
        "Deploy via the Git → WP Engine workflow: push to main, let CI sync, then flush the cache.",
        "Every new or rebuilt page ships DEINDEXED (noindex + unlinked) and is flipped to indexed — with nav, internal links and sitemap added — only on explicit operator approval.",
        "Keep every page CMS-editable (in-place editor pattern) so content changes never need code.",
        "At go-live, point DNS directly at the host and confirm SSL/HTTPS is valid (no proxy in front unless decided).",
        "On a site migration, verify content parity and run the pre-go-live checklist before cutover.",
        "Keep media optimised and changes versioned; be explicit about who does what (operator vs Cortex).",
    ],
}


def main() -> None:
    added = 0
    skipped = 0
    for key, rules in RULES.items():
        existing = [r.strip() for r in store.get_universal_rules(key)]
        for rule in rules:
            if rule.strip() in existing:
                skipped += 1
                continue
            store.add_universal_rule(key, rule)
            added += 1
    print(f"universal rules: added {added}, already-present {skipped}, across {len(RULES)} skills")
    # show the result
    for key in RULES:
        n = len(store.get_universal_rules(key))
        print(f"  {key:28} {n} universal rule(s)")


if __name__ == "__main__":
    main()
