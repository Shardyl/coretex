"""Generate the Cortex 'Build Phases & Specifications' reference PDF to the Desktop."""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                HRFlowable, KeepTogether)

CYAN = HexColor("#0091A8"); INK = HexColor("#10222B"); MUT = HexColor("#5A6B75")
GOOD = HexColor("#1F9D5B"); AMBER = HexColor("#C77A12"); LINE = HexColor("#D9E2E8")
PANEL = HexColor("#F1F7F9")

ss = getSampleStyleSheet()
def S(name, **kw):
    kw.setdefault("fontName", "Helvetica"); kw.setdefault("textColor", INK)
    kw.setdefault("fontSize", 10); kw.setdefault("leading", 14.5); kw.setdefault("spaceAfter", 5)
    return ParagraphStyle(name, parent=ss["Normal"], **kw)
title = S("t", fontName="Helvetica-Bold", fontSize=26, textColor=INK, leading=30, spaceAfter=2)
subtitle = S("st", fontSize=11, textColor=MUT, spaceAfter=2)
intro = S("in", fontSize=10.5, textColor=INK, leading=15, spaceAfter=4)
h = S("h", fontName="Helvetica-Bold", fontSize=14, textColor=CYAN, leading=17, spaceBefore=12, spaceAfter=4)
phh = S("ph", fontName="Helvetica-Bold", fontSize=12.5, textColor=INK, leading=15, spaceBefore=9, spaceAfter=2)
body = S("b")
gate = S("g", fontSize=9.5, textColor=MUT, leading=13, spaceAfter=2, leftIndent=2)
bullet = S("bu", fontSize=10, leading=14, leftIndent=10, bulletIndent=0, spaceAfter=2)
spech = S("sp", fontName="Helvetica-Bold", fontSize=11.5, textColor=CYAN, leading=15, spaceBefore=8, spaceAfter=2)

def esc(t): return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def badge2(text, color):
    return f'<font color="#{color.hexval()[2:]}"><b>[{text}]</b></font>'

story = []
story.append(Paragraph("Cortex", title))
story.append(Paragraph("Build Phases &amp; Specifications &nbsp;·&nbsp; reference", subtitle))
story.append(Paragraph("Generated 15 June 2026", S("d", fontSize=9, textColor=MUT, spaceAfter=8)))
story.append(HRFlowable(width="100%", thickness=2, color=CYAN, spaceAfter=8))
story.append(Paragraph(
    "Cortex is the voice-first AI operations platform that runs Rashad's businesses (Tabscanner, "
    "Sensa, Sky Vision, FilmSpoke). Workers do the work, managers judge and authorise, skills hold the "
    "craft and the learned rules, and Rashad approves on the move. This document is the phased build "
    "plan plus the locked specifications, for reference during the build.", intro))

# status summary
status_rows = [
    [Paragraph("<b>Where we are</b>", S("x", textColor=INK, fontSize=10.5)),
     Paragraph("Phases 0–2 done &nbsp;·&nbsp; Phase 3 (the app + voice) most of the way "
               "&nbsp;·&nbsp; Phases 4–7 ahead", S("x", textColor=INK, fontSize=10.5))]]
st = Table([[Paragraph("Phases 0, 1, 2 complete and verified. Phase 3 (cockpit + voice) is live at "
            "coretex.uk and installed on the phone, with the remaining screens still to build. "
            "Phase 4 (the CRM) is next.", S("ss", fontSize=10, leading=14))]], colWidths=[170*mm])
st.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),PANEL),("BOX",(0,0),(-1,-1),0.6,LINE),
                        ("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),10),
                        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8)]))
story.append(Spacer(1,4)); story.append(st); story.append(Spacer(1,6))

story.append(Paragraph("Part 1 — Build phases", h))

phases = [
    ("Phase 0 — Foundation", "DONE", GOOD,
     "Hetzner box <b>cortex-1</b>, Postgres 18, Python, secrets vault, the Telegram approval rail, the "
     "Shardyl/coretex repo, the Cyan theme, and the full specification.", None),
    ("Phase 1 — The core engine", "DONE", GOOD,
     "The spine every department reuses: the provider adapter (Claude Opus / Sonnet); a <b>worker</b> "
     "(does the task per its skill) and a <b>manager</b> (judges every draft against skill, brand and "
     "rules); the <b>skill system</b> (craft + standing rules + trust streak + ask/auto/never authority "
     "+ per-skill pause + global stop); siloed company data; the decision log / audit / rollback; and "
     "the approval pipeline on Telegram (approve / correct &#8594; redraft-until-happy &#8594; learn the rule).",
     "Gate: a skill drafts &#8594; manager checks &#8594; you approve or correct on Telegram &#8594; it "
     "redrafts &#8594; learns the rule &#8594; logged.  Verified."),
    ("Phase 2 — First vertical: Tabscanner Content &amp; SEO", "DONE", GOOD,
     "The Tabscanner SEO skill plus WordPress publishing. Cortex writes a real article and publishes it "
     "to the Tabscanner blog.",
     "Gate: a real post drafts &#8594; you approve on your phone &#8594; it goes live.  Verified (a post is "
     "live on tabscanner.com).  Note: the private 'see the finished designed page before it goes public' "
     "preview is parked, to be rebuilt as a draft + WordPress-login preview."),
    ("Phase 3 — The app (PWA) + voice", "IN PROGRESS", AMBER,
     "The Cyan cockpit and voice. <b>Built so far:</b> live at coretex.uk, installable on the phone, "
     "light/dark; screens <b>Inbox · Ask · Activity · Talk</b>; voice — streaming dictation, read-aloud, "
     "conversational chat, and hands-free <b>Free mode with barge-in</b>; a British voice (Alice); and "
     "<b>skill-aware chat</b> (Cortex can view and tune skills and rules just by being asked). "
     "<b>Still to come:</b> the remaining screens (Departments, Skills, Incoming, Calendar, Contacts, "
     "Projects, Team, Invoices, Reports, Settings), and letting the chat fire off tasks and actions directly.",
     "Gate: approve a real decision and run a voice exchange from the installed app.  Largely there."),
    ("Phase 4 — Leads &amp; Incoming (the CRM engine)", "NEXT", CYAN,
     "The lead / contact model (Stage × Value-tier + lead scoring); the Bitrix import (35k cold "
     "quarantined + the warm contacts you curate); and the Incoming omnichannel door across Gmail, "
     "website forms, Instantly, social DMs and WhatsApp — with the refine-and-learn reply loop and "
     "value/urgency triage.",
     "Gate: a real inbound lands &#8594; classified &#8594; reply drafted &#8594; you approve or redraft "
     "&#8594; sent &#8594; lead updated.  Needs your lead-import deep-dive."),
    ("Phase 5 — More departments, one at a time", "AHEAD", MUT,
     "Outreach · Social · Paid Ads · Sales &amp; Inquiries · PR · Production &amp; Projects · Support — "
     "each verified before the next. Sensa, Sky Vision and FilmSpoke onboarded here.", None),
    ("Phase 6 — Accounts / billing engine", "AHEAD", MUT,
     "Quote &#8594; invoice and usage-based auto-invoicing; payments (manual + Stripe + Payfort); chasing "
     "overdue invoices; statements; and the full FreshBooks replacement and migration.",
     "Needs your billing deep-dive (Tabscanner billing, FilmSpoke pricing, FreshBooks export, gateway specs)."),
    ("Phase 7 — Scale &amp; trust graduation", "AHEAD", MUT,
     "PA and PM roles, permissions and user-initiated escalation; trust graduation to auto-run; reports "
     "and ops polish; the harder social channels; remaining scale.", None),
]
for ttl, stt, col, bdy, gt in phases:
    blk = [Paragraph(f"{ttl} &nbsp; {badge2(stt, col)}", phh), Paragraph(bdy, body)]
    if gt:
        blk.append(Paragraph(gt, gate))
    story.append(KeepTogether(blk))

story.append(Spacer(1,6))
story.append(HRFlowable(width="100%", thickness=1, color=LINE, spaceAfter=6))
story.append(Paragraph("Part 2 — Specifications (locked)", h))
story.append(Paragraph("The decisions that govern the build, section by section.", S("note", fontSize=9.5, textColor=MUT, spaceAfter=4)))

specs = [
    ("A. Companies &amp; departments", [
        "Four companies in scope: <b>Tabscanner</b> (north star: enterprise / sales-qualified leads), "
        "<b>Sensa</b> (qualified production inquiries), <b>Sky Vision</b> (qualified inquiries), "
        "<b>FilmSpoke</b> (waitlist &amp; early adopters). Tabscanner goes fully live first.",
        "Data is <b>fully siloed</b> per company (contacts, skills, templates, assets walled off).",
        "All 9 departments active for every company by default: Content &amp; SEO, Social, Paid Ads, "
        "Outreach, Sales &amp; Inquiries, PR, Production &amp; Projects, Support, Finance &amp; Admin.",
        "Operators: Owner (Rashad) + a PA + scoped PMs.",
    ]),
    ("B. Worker / manager / skill / trust model", [
        "Every department = a <b>worker</b> (does) + a <b>manager</b> (judges every action vs skill, "
        "brand and your rules) before anything surfaces or runs.",
        "A new skill is <b>always ask</b>. It earns auto via a <b>trust streak</b> of clean approvals "
        "(~10 low-stakes, ~50 higher). Cortex <b>offers</b> auto; you opt in. Nothing self-promotes.",
        "<b>Clean approval = untouched.</b> If you edit before approving, it doesn't count — the edit teaches.",
        "<b>Rules form out loud:</b> on a yes, Cortex states the rule it's inferring; it's saved only when "
        "you confirm. Rejections and edits are lessons too.",
        "<b>Money / payments are permanently owner-only</b> (never auto). Everything else can graduate at "
        "the high bar. Per-skill pause + global stop. Every rule is listed on the skill's page, editable.",
    ]),
    ("C. The three doors", [
        "<b>Chat door</b> (voice-first): classifies the subject, infers which company, loads the right "
        "skill, responds, and captures decisions back into the skill as rules.",
        "<b>Incoming door</b> (reactive omnichannel): Gmail, website forms, Instantly replies, social DMs, "
        "WhatsApp &#8594; draft &#8594; approve/correct &#8594; redraft-until-happy &#8594; learn; reply on "
        "the same channel; queue triaged by value &amp; urgency.",
        "<b>Calendar door</b>: Cortex proposes recurring tasks (and you can set or trigger tasks manually).",
    ]),
    ("D. Roles &amp; permissions", [
        "<b>Owner (Rashad):</b> full access; the sole editor of skills and learned rules.",
        "<b>PA:</b> broad operational access including finance/money authority; cannot edit skills/rules.",
        "<b>PMs:</b> scoped to their company/project; approve routine work; no money, no skill editing.",
        "Money = owner or finance-PA. Any user can push a decision up to Rashad for review (user-initiated escalation).",
    ]),
    ("E. Data model &amp; integrations", [
        "Entities (all siloed): Company, Contact/Lead, Skill, Task, Project, Decision/Audit, User/Role, "
        "Incoming item, Scheduled task, Account record.",
        "Integrations: Anthropic and Telegram (live); WordPress/Rank Math, Instantly, Mailgun, Gmail OAuth; "
        "Google Ads, Gemini/Imagen + Atlas, ElevenLabs, Deepgram, Cloudflare R2; own accounting module. "
        "Social (Meta/LinkedIn/X) is the hardest, sequenced last.",
        "<b>E.1 Lead classification:</b> two axes — <b>Stage</b> (Cold &#8594; Engaged &#8594; Qualified "
        "&#8594; Opportunity &#8594; Client &#8594; Dormant, non-linear) × <b>Value tier</b> (A/B/C). A "
        "lead score from engagement, ICP fit, seniority and past activity drives who to pursue. Human "
        "involvement scales with value: A-tier and any Opportunity always loop in a human. The 35k cold "
        "leads are quarantined and scored; warm contacts are curated by Rashad first; unsubscribes auto-respected.",
    ]),
    ("F. Accounts module (replaces FreshBooks)", [
        "Replaces FreshBooks entirely — the real billing system, not just bookkeeping.",
        "Project businesses (Sensa, Sky Vision): quote &#8594; invoice (deposit up front + balance on "
        "delivery; mark paid manually). Tabscanner: its existing billing wired in later. FilmSpoke: fixed "
        "price per finished 'creative'.",
        "Payments: saved card (auto-charge) or a pay-link per invoice. Stripe (FilmSpoke), Payfort "
        "(Tabscanner). Full FreshBooks migration (clients + open balances + historical). Chasing, "
        "per-company VAT/currency, statements and aging.",
    ]),
    ("G. Voice &amp; app surfaces", [
        "Voice IN: <b>Deepgram</b> (Nova-3 streaming). Voice OUT: <b>ElevenLabs Flash v2.5</b>. Behind a "
        "swappable adapter.",
        "<b>Three modes:</b> Normal (tap to talk, reply on screen); Free (hands-free, voice both ways, "
        "silence turn-taking + barge-in); Talk / gym mode (always voice out, tap-to-talk in).",
        "App surfaces: Home, Inbox, Departments &#8594; Skill, Chat, Incoming, Calendar, Contacts, "
        "Projects, Team, Invoices, Reports, On-the-move, Settings. Hard rules: no horizontal scrollers; "
        "never scroll to reach a decision button; mobile-first.",
    ]),
    ("H. Reporting, decision log &amp; ops", [
        "Reports twice daily + on-demand: what's done / in progress, what needs approval, north-star "
        "numbers per company, problems &amp; escalations.",
        "Decision log / audit (who, what, when + before&#8594;after) — the rollback source and the data "
        "that drives trust. Undo any reversible action from the log.",
        "Daily offsite backup to Google Drive. Per-department API-cost metering against a hard spend cap.",
    ]),
]
for sh, items in specs:
    blk = [Paragraph(sh, spech)]
    for it in items:
        blk.append(Paragraph("• " + it, bullet))
    story.append(KeepTogether(blk))

story.append(Spacer(1,10))
story.append(HRFlowable(width="100%", thickness=1, color=LINE, spaceAfter=5))
story.append(Paragraph("Cortex · coretex.uk · build reference · for Rashad",
                       S("foot", fontSize=8.5, textColor=MUT)))

out = os.path.join(os.path.expanduser("~"), "Desktop", "Cortex - Build Phases & Specs.pdf")
doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm,
                        topMargin=16*mm, bottomMargin=14*mm,
                        title="Cortex - Build Phases & Specifications", author="Cortex")
doc.build(story)
print("WROTE", out)
