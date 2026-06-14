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
_(not started)_

## C. The three doors
_(not started)_

## D. Roles & permissions
_(not started)_

## E. Data model & integrations
_(not started)_

## F. Accounts module
_(not started)_

## G. Voice & app surfaces
_(not started)_

## H. Reporting, decision log & ops
_(not started)_

## I. Build roadmap
_(derived last from A–H)_
