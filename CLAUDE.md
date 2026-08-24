# Cortex — operator runbook for Claude sessions

Cortex is Rashad's voice-first AI ops platform running all five companies (Tabscanner, Sensa,
SkyVision, FilmSpoke, Snap Rewards). This repo is the whole system; production runs on the
Hetzner box `cortex-1`. Read this before touching anything.

## Access (all via the one SSH key `~/.ssh/id_ed25519`)

- **Box:** `ssh cortex` (alias → root@178.156.176.114). Cockpit + API live at https://coretex.uk
  via a Cloudflare tunnel (no open ports).
- **Database — the gotcha that keeps biting:** Postgres uses **peer auth** and there is **no
  `root` role**, so a bare `psql` as root is refused. This is NOT an access problem. Use:
  `ssh cortex "sudo -u postgres psql -d cortex -P pager=off -c '...'"`.
  (Alternative: connect as the app with the `DATABASE_URL` from `/etc/cortex/cortex.env`.)
  Do not "fix" `pg_hba.conf`.
- **Secrets:** `/etc/cortex/cortex.env`. Never `source` it (values contain spaces) — grep the
  key you need. Google OAuth client JSONs also in `/etc/cortex/` (must be `chmod 640 root:cortex`).

## Deploy (never scp code; push + pull)

1. Commit + push to `git@github.com:Shardyl/coretex.git` (main).
2. `ssh cortex "sudo -u cortex git -C /opt/coretex pull --ff-only && sudo systemctl restart cortex-api cortex-engine"`
   — the pull MUST run as the `cortex` user (deploy key is in `/home/cortex/.ssh/`; a root pull fails).
3. Verify: `systemctl is-active cortex-api cortex-engine`. Cockpit SW is network-first — a
   normal reload picks up new web code (bump the sw.js cache version when changing `web/index.html`).

Layout on box: repo at `/opt/coretex`, venv `/opt/coretex/.venv`, WorkingDirectory
`/opt/coretex/runtime`, package imports as `cortex`. Services: `cortex-api` (uvicorn
`cortex.api:app` on 127.0.0.1:8787, also serves the cockpit) + `cortex-engine` (`runtime/main.py`,
60s loop) + `cloudflared`. The engine runs as user `cortex` — files/dirs it writes must be
cortex-owned (a root-owned `/opt/coretex/reports/` silently killed weekly reports for 7 weeks).

## Before acting on any company task (STANDING RULE)

The **live DB is the source of truth** — never trust cached docs, `catalog.py`, or old counts.
Before drafting/doing anything for a company: read the relevant skill rows LIVE
(`skills.rules`/`craft` + `universal_skill_rules`) and the live company profile
(`company_profiles.data`), report what you found, then act. Drafting/behaviour logic lives in
skill craft+rules (editable via cockpit/Talk), never hardcoded — code is schema/plumbing only.

## House rules (from Rashad, standing)

- Cortex DRAFTS, a human approves — never send/publish without an approval card. Outward approvals:
  the owner, or a team member with `users.can_approve` using their own PIN at the step-up gate
  (2026-08-22: Gino + Ayresh for Sensa — Rashad is training them to train the system). MONEY-class
  kinds always require the owner's own step-up. Blogs never auto regardless of trust.
- Telegram is a MIRROR, never the flow — its calls are fail-soft (`integrations/telegram.py`);
  keep it that way.
- Never delete CRM/contact data on your own initiative; merges carry over every non-empty field.
- Any Cloudflare WRITE needs Rashad's explicit OK first (read-only fine, but disclose).
- Batch/recurring LLM jobs default to Haiku (batched + prompt-cached); Sonnet = prose,
  Opus = ideation. Never silently upgrade a model.
- No emoji in the cockpit UI — clean monochrome line-icons (Tabler-style) only.
- Ship a new Talk capability? Add its one-liner to `runtime/cortex/capabilities.py` in the same
  commit — that manifest is injected into every system prompt.
- New scheduled work goes on the unified clock (`tasks` recurring templates +
  `engine.promote_due_tasks`) — `scheduled_tasks` is long dead.

## WhatsApp (inbound)

**Two transports, one brain.** Both funnel into `whatsapp._process_message`, so triage, CRM capture and
drafting can never drift apart:

- **Cloud API (supported, preferred).** Meta POSTs to `/api/whatsapp/webhook`; approval sends via
  `whatsapp.send_text`. App "Sensa Productions Messaging" (ID **4445290622399421**) under portfolio
  **439550807574053**, which is **verified** (SKY VISION AERIAL PHOTOGRAPHY SERVICES, 20 Aug 2026).
  Env keys in `/etc/cortex/cortex.env`: `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_TOKEN`,
  `WHATSAPP_PHONE_NUMBER_ID`. Without the last two, `cloud_ready()` is false and approval falls back to
  queueing for the runner.
- **Office-box runner (fallback).** WhatsApp Web as a linked device, driven by Patchright, `wa.py` on
  `pc@100.68.251.25`. Contract mirrors the social runner: `POST /api/whatsapp/inbox` ->
  `GET /api/whatsapp/jobs` -> `POST /api/whatsapp/jobs/{tid}/result`, behind `_runner_auth`.
  ⚠️ **Automating WhatsApp Web is against WhatsApp's terms and got a fresh number BANNED within hours on
  19 Aug 2026** (six session open/close cycles on a number registered that morning). Ban was reversed on
  appeal. Use the Cloud API.

**The webhook endpoints are PUBLIC** (`GET`/`POST /api/whatsapp/webhook`) — Meta calls them, so they carry no
operator auth. Security is the verify token on the GET handshake and the app-secret HMAC over the RAW body on
every POST. Never add `_runner_auth` (it breaks the subscription) and never relax the signature check (it is
all that stops anyone POSTing fake enquiries into the Inbox). The POST always returns 200 once signed, because
Meta retries non-200s and disables webhooks that keep failing.

- Tone is NOT in code — it comes from the `social-dm-replies` skill craft/rules. Routing (account -> company/
  skill/author) is the live `wa_routing` setting, so a new number never needs a deploy.
- Kind is `wa_reply`: outward, never auto, biometric step-up on approve.
- **CRM matches on PHONE, not email** (`crm.match_or_add_by_phone`). `crm_master` is email-keyed with a
  unique index on `lower(email)`, and `add_inbound_contact` refuses an email-less contact, so WhatsApp gets
  its own path. It creates genuinely email-less rows; we do NOT invent placeholder addresses.
- **The gotcha:** matching falls back to the last 9 digits, and ~1,384 existing rows share a last-9 key.
  Those are SHARED COMPANY SWITCHBOARDS (nine people behind one office line), not duplicates. `find_by_phone`
  refuses an ambiguous key rather than filing a message against the wrong human. Do not "fix" this by taking
  the first row. Likewise a `name` that parses as a phone number is stored as NO name — WhatsApp puts the
  number where a name would go, and the real name is learned from the conversation.
- App must be **published** before Meta delivers production webhooks; unpublished apps get test events only.

## Fitness (personal, not a company)

Rashad's training log. Data lives in the **`fitness` schema** (not the company tables) in the same
`cortex` DB, so it is in the nightly Drive dump like everything else. The PWA is served from the
box at **coretex.uk/fitness** (`web/fitness/`), same origin as the cockpit, so it reuses the
`cortex_token` from the cockpit login: no API key on the phone, no CORS. If the pill top-right says
"Sign in to sync", log into the cockpit in the same browser and reopen.

- Sync is whole-document (`POST/GET /api/fitness/state`): a few hundred rows, one device, so a full
  push/pull is easier to reason about than field-level merge. Upserts by the client's id, never
  hard-deletes; local deletions are sent as explicit tombstones. Every push is stored verbatim in
  `fitness.snapshots` — that is the restore path, do not prune it.
- localStorage stays the offline cache. The app saves locally FIRST and pushes after, so a gym with
  no signal works exactly as before.
- **PR conventions are not decoration, do not "simplify" them:** lifting PR = volume load
  (total reps x kg); bodyweight lifts resolve against the weight logged for the SESSION DATE, so a
  weight change never rewrites old records; cardio PR = Pareto frontier of lowest avg HR vs hardest
  settings, within one preset only. `fitness.py` mirrors the app's maths — change both together.
- Bodyweight and VO2 tables exist but are deliberately EMPTY: the app was never set up for either
  and Rashad holds that data elsewhere. Until a bodyweight is logged, no bodyweight lift can be
  scored (`volume_load` is null by design, not a bug). Parked, to be consolidated later.
- Migration provenance: seeded 24 Aug 2026 from `fitness_2026-08-24.xlsx` (318 lift sessions,
  38 cardio sessions, 13 presets, 2 plans). The old app was a Netlify/standalone install whose only
  copy was phone localStorage.
- A personal "company" in Cortex is AGREED but NOT BUILT: `companies.kind` would be `personal` with
  its own smaller skill roster (the uniform-85 roster rule applies within `kind='owned'`), and
  personal CRM contacts must be suppressed from every campaign audience. Do not build it uninvited.

## Wider context

Full history + current state live in the Claude memory files (mirrored on the box at
`/opt/cortex-knowledge/memory/`, tarred nightly to Google Drive at 03:00 with the DB dump).
Key ones: `project_cortex*.md` (architecture, cockpit, CRM, roadmap), `reference_machine_restore_2026_08.md`
(access provenance). Docs in `docs/` (CORTEX-SPEC.md et al.) are the original build spec —
good for intent, stale for detail; the DB and this file win.

## Keep this file honest

If this session changes anything this file describes (access, deploy mechanics, structure,
standing rules), update this CLAUDE.md in the same commit and push it. This file is the first
thing every new conversation reads; a stale line here costs a future session real time.
