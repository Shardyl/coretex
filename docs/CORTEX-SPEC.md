# Cortex — Technical Specification (in progress)

Status: **being filled via the planning questionnaire** (started 2026-06-14). This is the
single source of truth for what Cortex does and how it's built. Sections fill in as Rashad
answers; unresolved items are parked under "Open" within each section. **Nothing gets built
until its section here is locked**, and the build roadmap (§I) is derived last from §A–H.

## How this session runs
- One area at a time, exhaustively. Questions are numbered (A1, B3…) so answers can be given
  by number, in any order, at any depth.
- "Decide later" parks an item under that section's **Open** list — nothing is dropped.
- Each answered area is written up here in prose + committed, so long sessions never lose state.
- Where a decision has real tradeoffs, the spec records the recommendation + the why.

## Table of contents
- **A. Companies & departments** — roster, per-company context/voice, department activation, priority, shared-vs-siloed
- **B. Worker / manager / skill / trust model** — execution, judgement, escalation, the auto/ask/never authority table, trust-streak graduation
- **C. The three doors** — Chat (classify + capture), Calendar (scheduled), Incoming (omnichannel)
- **D. Roles & permissions** — owner / PA / PM, scoped by company × department × action; owner-only actions
- **E. Data model & integrations** — entities + every external API, its auth, and its exact use
- **F. Accounts module** — invoices, client accounts, statements, outstanding, chasing, exports
- **G. Voice & app surfaces** — every mockup screen, voice latency/feel, "on the move", notifications
- **H. Reporting, decision log & ops** — reports, audit trail, rollback, backups, cost metering
- **I. Build roadmap** — derived last, the phased build order

---

## A. Companies & departments
_Locked 2026-06-14 (context packs = DRAFT, pending Rashad's confirmation)._

**Roster (in scope now):**
| Company | Type | What it is | North star (what Cortex optimises for) |
|---|---|---|---|
| **Tabscanner** | Sensa-owned | Receipt-OCR / expense-data-extraction API (SaaS) | **Enterprise / sales-qualified leads** |
| **Sensa Productions** | Sensa-owned | Video/film production company | **Qualified production inquiries** |
| **Sky Vision** | Sensa-owned | Aerial/drone film (skyvision.film) | **Qualified inquiries** |
| **FilmSpoke** | Sensa-owned | AI commercial "creatives" (filmspoke.ai) | **Waitlist & early adopters** |

Out of scope for now (add later): Snap Rewards, client sites (MAH Gold, Algarve Kitchen Angels).

**First to go fully live:** Tabscanner.

**Data model stance:** **Fully siloed** — each company gets its own contacts/leads, skills,
templates, and assets, walled off from the others.

**Operators:** Owner (Rashad, full access everywhere) + a **PA** + **PMs** (scoped). Exact
permission matrix specced in §D.

**Department activation:** **all 9 departments active for all 4 companies** by default
(Content & SEO, Social, Paid Ads, Outreach, Sales & Inquiries, PR, Production & Projects,
Support, Finance & Admin). Per-company exceptions ("switch X off for company Y") get caught
as each department is specced in detail.

**Context packs (DRAFT — confirm/correct):**
- **Tabscanner** — Audience: developers, product teams, fintech/expense/loyalty companies.
  Voice: technical, credible, accuracy-first, B2B. Watch-out: finance-adjacent (YMYL) content
  needs care (flagged in the content audit). 
- **Sensa Productions** — Audience: brands/agencies/marketing leads. Voice: cinematic, premium,
  creative. Brand: cyan #00DAFF, all-black, Poppins+Inter.
- **Sky Vision** — Audience: brands/agencies needing aerial. Voice: dynamic, cinematic. Brand:
  yellow #FFCC00 (same design system).
- **FilmSpoke** — Audience: brands wanting AI-made commercials. Products are called **"creatives"**
  (finished commercial films). Voice: bold, innovative. Brand: red #E50914. Note: no Dubai imagery.

### Open
- Confirm/correct the four context packs above (voice, audience, hard do's & don'ts).
- Per-department on/off exceptions per company (caught while speccing each department).

## B. Worker / manager / skill / trust model
_Locked 2026-06-14._

**The pair.** Every department runs a **worker** (does the task per its skill) + a **manager**
(judges the output). **The manager reviews every action** against the skill, the company's
brand/voice, and the operator's confirmed rules before it surfaces to Rashad or auto-runs.
(Accepts a small extra per-action cost for quality; revisit if cost demands it.)

**Default authority.** A brand-new skill is **"always ask"** — nothing runs without Rashad's yes.

**Earning auto (graduation).** A skill accumulates a **trust streak** of *clean* approvals.
At the threshold it **OFFERS** auto mode; **Rashad opts in**. Nothing self-promotes.
- Threshold: **~10 clean approvals for low-stakes, ~50 for higher-stakes.**
- **Stakes = reversibility.** Easy-to-undo = low (auto sooner); hard-to-undo = high (higher bar
  or stays manual). _Open: the concrete reversibility→tier mapping per action type._

**Clean approval = untouched.** Only approvals Rashad didn't edit advance the streak. If he
edits before approving, it does **not** count — the edit **teaches** instead.

**Rules form out loud (operator-locked principle: approval ≠ silent rule).** On a "yes",
Cortex **states the rule it's inferring** — *"I'm reading this as: always X when Y — correct?"* —
and Rashad confirms / edits / rejects. Rules are **only** created when explicitly confirmed.
The "yes" still approves that one action and ticks the streak.

**Rejections/edits are lessons.** A "no" or an edit feeds back: Cortex **surfaces the adjustment
it's making** and Rashad confirms (mirrors the approval mechanic). A rejection does **not** reset
the streak.

**Escalation.** Routine asks sit in the approval inbox; the manager **escalates directly** to
Rashad when something is risky, ambiguous, or breaks a rule.

**Permanently owner-only (never auto, regardless of trust): moving money / payments.**
Everything else — messages to people, publishing public content, even quotes/contracts — **can**
graduate to auto after the high (~50) bar. _(Deliberate widening of the original "sends are
owner-only"; the high bar + rule-confirmation keep it safe.)_

**Safety switches.** Per-skill **pause** (yank one skill back to manual) + a **global stop**
(freeze everything), both instant.

**Rule transparency.** Every inferred/confirmed rule is **listed in the skill's page**; Rashad can
read, edit, or delete any of them anytime.

### Open
- The concrete reversibility → stakes-tier mapping (what counts as low vs higher-stakes per action type).
- Whether "higher-stakes" has sub-tiers (e.g. a 50 bar vs a "never auto unless I flip it" tier) beyond money.

## C. The three doors
_Locked 2026-06-14._

**Chat door.** **Voice-first** (the "on the move" vision), type as fallback. Cortex classifies the
subject and **infers which company** it concerns (asks only when ambiguous), loads the right skill +
data, responds, and captures any decision back into the skill via §B's rule mechanic.

**Incoming door (reactive omnichannel).** Channels monitored: **Email (Gmail), website contact forms,
Instantly cold-email replies, social DMs (Meta/LinkedIn), and WhatsApp-from-websites.** Default
handling — the **iterative refine-and-learn loop**: Cortex drafts a reply in the right voice →
Rashad **approves or corrects** → it **redrafts until he's happy** → it then **learns from *why* he
tweaked** and updates the skill. Replies go out on the **same channel**. Queue is **triaged by value
& urgency** (hot leads / time-sensitive first).

**Calendar door (scheduled/proactive).** **Cortex proposes** recurring tasks and runs them once
approved, **and** Rashad can **set or trigger tasks manually**. Both paths coexist.

### Open
- Per-channel auth/setup (Gmail OAuth, WhatsApp-from-website source, social DM access) — specced in §E.
- How "value & urgency" is scored for triage (lead-value signals, time-sensitivity) — detail later.

## D. Roles & permissions
_Locked 2026-06-14._

Three roles:
- **Owner (Rashad)** — full access everywhere; the **sole editor of skills and learned rules**.
- **PA** — broad operational access across all companies, **including finance/money authority**
  (explicitly authorized to handle finances). Can approve sensitive actions (including money) and
  routine work. **Cannot edit skills/rules** (owner-only). A trusted operational deputy.
- **PMs** — scoped to **their assigned company/project only**; approve routine work within their
  area; no money, no skill editing.

**Approvals:** sensitive actions (money, a skill graduating to auto, cross-company) require an
**authorized human — owner or the finance-authorized PA**. PA/PMs approve routine work in scope.

**Money reconciliation (refines §B):** money/financial actions are **never Cortex-auto**, but an
**authorized human (Rashad or PA)** can approve them — it is not Rashad-exclusive.

**Skills/rules:** only the owner shapes skills and confirmed rules.

**User-initiated escalation (review request).** Any in-scope user (PA or PM) can **push a decision
up to Rashad for review** at any time — flagging an item they want his call on. It triggers a
**notification to Rashad** (Telegram now, in-app push later); his decision flows back to the
originating user/queue. Human-initiated, distinct from the manager-agent escalation in §B.

### Open
- Exact PM ↔ company/project assignments (which PMs, which projects) — fill when people are added.
- Whether the PA's finance authority has a ceiling (e.g. per-transaction amount limit) — confirm later.

## E. Data model & integrations
_Locked 2026-06-14 (lead/contact data handling = dedicated deep-dive, see Open)._

**Data model (entities — draft).** All company-scoped data is **siloed** per §A.
- **Company** — context pack, brand, active departments, north star.
- **Contact / Lead** — per company; lead vs warm-contact status; source; owner; activity history.
- **Skill** — craft + company context; trust streak; confirmed rules; authority (ask/auto/never); stakes tier; pause state.
- **Task / Job** — a unit of work (worker run → manager review → decision).
- **Project** — grouping of work (esp. Production & Projects); stages; PM owner.
- **Decision / Audit entry** — every action + who approved + before/after (rollback + the data that drives trust).
- **User / Role** — owner, PA, PMs; permission scope.
- **Incoming item** — inbound message across channels; triage score; draft reply; status.
- **Scheduled task** — recurring/proactive work (Cortex- or owner-set).
- **Account record** — invoices, client balances, statements (§F).

**Integration map.**
| Need | Tool | Status | Notes |
|---|---|---|---|
| The brain | Anthropic API | ✅ live | dedicated key + spend cap on `cortex-1` |
| Approvals | Telegram | ✅ live | bot @Coretextelebot |
| Publish web | WordPress REST per site (Rank Math) | ⚙️ | Tabscanner first; hidden→index toggle |
| Cold email | Instantly | ⚙️ | campaigns + reply capture |
| Newsletters | Mailgun | ⚙️ | bulk send + tracking |
| Email replies | Gmail API (OAuth) | ⚙️ | inbound + reply |
| Social | Meta (IG+FB), LinkedIn, X | ⚠️ hard | post + read DMs; per-platform; sequence last |
| Paid ads | Google Ads API | ✅ proven | feeds Tabscanner / PPC→SEO |
| AI media | Gemini/Imagen + Atlas | ✅ reuse AddDrop | images + video |
| Voice out | ElevenLabs | ✅ owned | §G |
| Voice in | Web Speech → Deepgram | ⚙️ | test on S25 first; §G |
| Media storage | Cloudflare R2 | ✅ pattern | artifacts |
| Accounting | Cortex own module | build | §F (no external tool) |
| WhatsApp (from websites) | channel | ⚙️ | inbound channel; exact source TBD |

### E.1 Lead & contact classification (deep-dive — locked 2026-06-14)

**Two-axis model.** Every lead/contact carries a **Stage** + a **Value tier**, applied within each
(siloed) company.

**Stages (default — customizable per company in settings):**
Cold → Engaged → Qualified → Opportunity → Client → Dormant.
- **Non-linear:** records move backward/forward freely; notably a **Client returns to Opportunity**
  when new business arises (repeat / upsell).

**Value tier:** A / B / C by **revenue / deal potential** (A = enterprise / decision-maker / high
potential; C = small or unlikely).

**Lead → Contact promotion:** a lead becomes a contact on **real engagement / qualification**
(meaningful reply or clear profile fit).

**Lead score** (drives who to pursue / re-engage). Signals that raise it: recent engagement · ICP
fit · seniority / decision-maker · past activity (prior quote/trial/inquiry) · website/pricing-page
visits · email opens & clicks · target-industry match · referral / warm intro.

**Ownership:** Cortex assigns the relationship (by company/stage); owner or PA can reassign anytime.

**Handling (how each is treated):**
- Cortex **drafts all outreach for approval** (graduates to auto per §B's trust model).
- **Human involvement scales with value:** low-tier nurture is Cortex-led; **A-tier and any
  Opportunity always loop in a human (owner/PA).**
- **Opportunity stage (live deal / quote / proposal): human-driven, Cortex preps** the
  quote/proposal/materials. Client commitment stays owner-authorized (§D).
- **The existing 35k cold leads: quarantined**, scored/segmented by Cortex; promising ones surfaced
  for a **re-engagement campaign Rashad approves** (not bulk-blasted).
- **Compliance:** Cortex **auto-respects unsubscribes + a do-not-contact flag** — never contacts
  flagged records.

### Open
- The 157 already-warm contacts: confirm their initial stage/tier on import (likely Qualified/Contact, A-tier) — quick pass at import time.
- Exact re-engagement cadence + which tiers qualify for the 35k campaign.
- WhatsApp-from-websites: confirm exact source (WhatsApp Business API vs click-to-chat forwarding).
- Social DM/post access is the hardest integration set — sequenced last in the build.

## F. Accounts module
_(not started)_

## G. Voice & app surfaces
_(not started)_

## H. Reporting, decision log & ops
_(not started)_

## I. Build roadmap
_(derived last from A–H)_
