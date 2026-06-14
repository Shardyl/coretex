# CORTEX — BUILD PLAN

The voice-first AI operating system for Rashad's businesses.
Design spec: the clickable dark mockup (`Ops Control Mockup/ops-control-v2-dark.html` on
Desktop). This plan = how we turn that mockup into something that genuinely works.

> This is the **canonical** plan and now lives in version control. The copy on the Desktop
> (`Desktop/CORTEX-BUILD-PLAN.md`) is a historical snapshot from 2026-06-14.

---

## 1. BUILD PHILOSOPHY

- **No big-bang.** Build one *vertical slice* end to end that genuinely works, then extend.
- **Integration map first.** Every external connection is mapped and auth-tested before
  building on it.
- **Verification gate at every phase.** "Done" = the real-world outcome is demonstrated —
  the post published, the lead landed, the email sent — not "the code looks right."
- **Provable, reversible, owner-gated.** Money and outbound sends stay owner-approved until
  trust is explicitly earned.

---

## 2. INFRASTRUCTURE

| Piece | Choice | Notes |
|---|---|---|
| Compute | New, dedicated Hetzner box | Separate from AddDrop. |
| Agent runtime | Claude Agent SDK behind a provider adapter | Worker/manager loop, skills, tools. |
| Database | **Postgres, on Hetzner** | Locked: own DB, held on Hetzner. Seed from the Bitrix export already pulled (35,438 leads + 157 warm contacts). |
| App | PWA + backend API | Phase 2, after the first slice proves out. |
| Media | Cloudflare R2 | Reuse AddDrop pattern. |
| Skills | Git repo (this repo, `/skills`) | Versioned, diffable, one-tap rollback. |
| Secrets | Server env / secret store | Never in git. Dedicated Anthropic API key with a hard spend cap. |
| Approvals (early) | Telegram bot | Phone approvals before the PWA. |
| Backups | DB + config → daily offsite | Don't let Hetzner be a single point of failure. |

### Billing note (verified 2026-06-14)
Cortex runs programmatically, so it bills on the **metered Console API at standard rates**,
separate from the Max subscription. As of **15 June 2026** Anthropic split programmatic use
(Agent SDK, `claude -p`, third-party apps) into its own metered credit pool — a Max plan's
included programmatic allowance (~$100 Max 5x / ~$200 Max 20x) is metered at API rates with
overflow billing, so it gives no discount at Cortex's volume. **Action:** dedicated API key
+ hard spend cap + per-department cost meter from day one.

### Runtime alternative to weigh (open decision)
**Managed Agents (CMA)** — Anthropic hosts the agent loop *and* a per-session sandbox, with
versioned agent configs, MCP connectors, Skills, Vaults (credential storage), Memory stores,
Outcomes (rubric-graded loops), and Webhooks. Maps closely onto Cortex's spec. Three options:
Hetzner+Agent-SDK (full control, current plan) · Managed Agents (less infra, more coupling —
cuts against the provider-adapter goal) · **Hybrid** (Hetzner orchestrates; heavy workers run
as Managed Agents) — likely best. Decide before Phase 0 hardens.

---

## 3. INTEGRATION MAP

| Need | Tool | Method | Status |
|---|---|---|---|
| The brain | Anthropic | Agent SDK (metered API key) | ✅ ready |
| Voice OUT | ElevenLabs | TTS API | ✅ owned |
| Voice IN | Web Speech (test first) / Deepgram | Browser / streaming | ⚙️ test on S25 |
| Publish web | Tabscanner (WordPress + Rank Math) | **WP REST API** + Rank Math noindex→index | ✅ proven pattern (AddDrop/Sensa) |
| Contacts / leads | **Cortex Postgres** | own DB, seeded from Bitrix export | ⚙️ build |
| Cold email | Instantly | API + reply webhook | ⚙️ wire |
| Newsletters | Mailgun | API | ⚙️ wire |
| Email in/out | Gmail | Gmail API (OAuth) | ⚙️ set up |
| Paid ads | Google Ads | API | ✅ proven |
| Creative | Gemini/Imagen, Atlas | APIs | ✅ proven (AddDrop) |
| Social post + DM | Meta, LinkedIn | Graph / limited | ⚠️ hard — last |
| Accounting | **Cortex own module** | basic FreshBooks: invoices, client accounts, statements, outstanding, spreadsheet export | ⚙️ build (owner-gated) |
| Notifications | Telegram + Web Push | Bot API / push | ⚙️ Telegram first |

---

## 4. ARCHITECTURE

- **Per department:** a *worker* (task skill — how to do it) + a *manager* (judges
  alignment, holds the authority table, escalates).
- **Skills:** git-backed markdown; company context inherited + per-skill craft. Each carries
  a trust streak + a configurable auto-promote threshold. Promotion to auto is explicit and
  logged, never inferred (approval ≠ rule).
- **Two job triggers:** the task calendar (scheduled) + event webhooks (reactive). Both fire
  jobs → workers → results → the approval queue (or auto, if graduated).
- **Approval routing:** by permission scope (you / PA / PM). Money & outbound sends stay
  owner-only.
- **Decision log:** every action + who approved it → audit trail + rollback + the data that
  drives trust graduation.

---

## 5. PHASED BUILD SEQUENCE

**Phase 0 — Foundation + first slice (Telegram-first, no PWA yet)**
- Stand up: Hetzner box, agent runtime, Postgres, this skill repo, secrets.
- One slice: **Tabscanner SEO blog** — draft → publish hidden (noindex+unlinked) → approve
  on Telegram → index (Rank Math).
- ✅ Gate: a real post publishes hidden → approved on the phone → indexes.

**Phase 1 — Manager + the loop**
- Manager layer, decision log, trust streaks, twice-daily report.
- ✅ Gate: a report lands; a category shows a real streak; an escalation routes correctly.

**Phase 2 — The PWA**
- Build the Cortex cockpit (the mockup): inbox, chat, voice, departments.
- ✅ Gate: approve a real decision from the phone; talk to it hands-free.

**Phase 3 — More departments (one at a time, each verified)**
- Lead-capture (Instantly reply → Cortex DB → flagged) · Outreach · Newsletters ·
  Quotations · Incoming (email/forms first).

**Phase 4 — Scale**
- More companies · graduate trusted categories to auto · the accounts module · social.

---

## 6. PHASE 0 IN DETAIL

1. Provision the Hetzner box + agent runtime + Postgres + skill repo + secrets (dedicated
   API key + spend cap).
2. Clone the **Tabscanner** repo onto Hetzner as worker context (brand voice, structure,
   `strategy/` SEO pack).
3. Pin the Tabscanner publish path — WP REST credentials + Rank Math noindex→index toggle.
4. Write the **Tabscanner SEO skill** (voice, structure, authority:
   `auto-draft-hidden / ask-to-index / never-money`). Re-read the `web-page-builder` skill
   before building and again before publishing (every page ships noindex by default).
5. Wire **Telegram** approval — "index this post? ✅ / ✗".
6. **Verify live:** real post → hidden → Telegram approve → indexed.

Only then: Phase 1.

---

*Status: design locked, repo initialised 2026-06-14. Next: provision Hetzner (needs the
Hetzner account + an SSH key). Open decision before Phase 0 hardens: runtime = Hetzner+
Agent-SDK vs Managed Agents vs hybrid.*
