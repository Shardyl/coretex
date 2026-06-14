# Cortex

> Repo slug is `coretex`; the product is **Cortex**. (Spelling of the repo name is
> Rashad's call — flagged 2026-06-14; trivial to rename now while it's empty if the
> `coretex` spelling wasn't intended.)

**Cortex** is the voice-first AI operating system Rashad Al-Safar is building to run all
of Sensa's businesses (Tabscanner, Sensa, Sky Vision, FilmSpoke, Snap Rewards). Worker
agents do the actual business work; manager agents judge, authorise, and escalate; Rashad
approves on the go from his phone. Every decision he makes feeds back into the skills so
the system earns trust and graduates low-stakes tasks to auto over time.

Design was **locked 2026-06-14**. This repo is the build.

---

## Architecture (locked)

| Piece | Choice | Notes |
|---|---|---|
| Compute | **Dedicated Hetzner box** | New, separate from AddDrop. Everything runs here; the laptop is dev + a window. |
| Agent runtime | **Claude Agent SDK** behind a thin **provider adapter** | Worker/manager loop, skills, tools. The adapter keeps "switch AI provider later" possible. |
| Database | **Postgres, on Hetzner** | Contacts/leads, decision log, skill metadata + trust streaks, tasks, projects, users, accounts. |
| App | **PWA + backend API** | The Cortex cockpit (see the dark mockup). Phase 2 — built *after* the first slice proves out. |
| Media | **Cloudflare R2** | Reuse the AddDrop pattern. |
| Skills | **Git-backed markdown** | Company context inherited + per-skill craft. Each skill carries a trust streak; promote-to-auto is explicit + logged, never inferred. |
| Approvals (early) | **Telegram bot** | Phone approvals in week one, before the PWA exists. |
| Voice | **Web Speech (test first) → Deepgram if needed** out via ElevenLabs | "On the move" = hands-free full app. |

**Nine departments under four categories:** Demand (Content & SEO, Social, Paid Ads,
Outreach) · Convert (Sales & Inquiries, PR) · Deliver (Production & Projects, Support) ·
Run (Finance & Admin 🔒). Each company activates a subset.

**Three doors:** Chat (classifies → loads skill → responds → captures the decision),
Calendar (scheduled tasks), Incoming (reactive omnichannel). All feed workers; decisions
surface in the Inbox. **Money and outbound sends stay owner-only.**

---

## Phase 0 — first vertical slice (build now)

The Tabscanner SEO blog loop, proven end-to-end on real infrastructure, approvals over
Telegram. No PWA yet.

1. Provision Hetzner — box, Postgres, agent runtime, secrets store, this repo.
2. Clone the **Tabscanner** repo onto Hetzner as the worker's *context* (brand voice,
   page structure, the `strategy/` SEO pack).
3. Pin the Tabscanner publish path — **WordPress REST API** + the Rank Math noindex→index
   toggle. (Tabscanner is WordPress + Rank Math; posts publish via REST, *not* a repo
   push. The repo is design/template code + context only.)
4. Write the **Tabscanner SEO skill** — voice, structure, authority
   (`auto-draft-hidden / ask-to-index / never-money`).
5. Wire **Telegram** — the ✅/✗ index approval.
6. **Verify live:** a real post drafts → publishes **hidden (noindex + unlinked)** → preview
   URL to Telegram → Rashad approves → Cortex flips Rank Math to *index* and links it.

✅ **Gate:** a real Tabscanner post goes hidden → approved on the phone → indexed.

Nothing in Phase 0 is faked. If it can't be demonstrated on real APIs, it isn't done.

---

## Repo layout (intent — fills in as we build)

This is the destination shape, not a pile of half-wired code. Directories land as each
slice needs them.

```
/docs        — CORTEX-BUILD-PLAN.md (canonical), DECISIONS.md, architecture notes
/runtime     — agent runtime + the provider adapter seam
/skills      — git-backed skill markdown (company context + per-skill craft)
/db          — Postgres schema + migrations
/integrations— telegram, wordpress (tabscanner), instantly, mailgun, …
/app         — the PWA (Phase 2)
```

---

## Status

Design locked. **Foundation live 2026-06-14:** Hetzner box `cortex-1` (CPX31, Ubuntu 26.04,
Ashburn) provisioned + hardened (ufw, fail2ban, auto security updates, swap, `cortex` service
user), **PostgreSQL 18** + the `cortex` database, **Python 3.14**, secrets at
`/etc/cortex/cortex.env` (Anthropic key validated — live HTTP 200). Runtime language =
**Python**. Domain `coretex.uk`.

**Next:** clone `coretex` onto the box, then build the Phase 0 slice — the Tabscanner SEO
skill + the WordPress REST publish path + Telegram approval. See `docs/CORTEX-BUILD-PLAN.md`
and `docs/DECISIONS.md`.
