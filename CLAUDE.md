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
- **Team login reset (no cockpit UI yet):** passwords/PINs are one-way HMAC hashes (keyed by the
  `api_secret` setting, `pin:<value>` scheme) — never recoverable, only resettable. On the box as
  `cortex`, compute the temp hash with the app's scheme and
  `update users set passcode_hash=<hash>, must_onboard=true, pin_hash=null where email=...` —
  the user re-onboards and may set the SAME password/PIN. Team members only ever type their PIN
  day-to-day, so remembered passwords drift (bit Gino 25 Aug 2026). Cockpit demo mode
  (passcode `demo`, sample data) was removed the same day — no unauthenticated entry.

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

## Email intake (full-mailbox triage)

`poll_all_inboxes()` (60s loop) sweeps every CONNECTED inbox in the `inbox_registry` setting — adding a
mailbox is one OAuth consent + `register_inbox()`, no code. Sensa runs FOUR mailboxes (hello@, gino@,
rashad@, ayresh@). Each email: `classify_email` (Haiku, sales-triage skill; categories incl. `finance`)
→ CRM capture → `_draft_direct_reply` for substantive lead/client/finance mail (skips <40-char bodies).
**Deterministic client override (2026-08-25):** a sender on an ACTIVE deal — exact email
(`crm.open_deal_for_email`) or corporate-domain colleague (`crm.open_deal_for_domain`) — always drafts,
whatever category Haiku picked (it filed a MAH Gold project brief as `support` and the mail was silently
swallowed). A new mail from a sender with an OPEN reply card SUPERSEDES that card (request updated,
redrafted) instead of being dropped. The drafted reply carries `from_email` + `mailbox_rt` so the send
goes out FROM the receiving mailbox with its own token; `deal_id` + project context attach when the
sender belongs to an active deal. Continuations get `thread_reply` (no reference box).
**Thread continuation (2026-08-25):** the poller stashes `request.thread` (Gmail `threadId`,
`Message-ID`, `References`) and the send passes them through `gmail.send_message`, so approved replies
land ON the client's existing thread (subject kept verbatim on Re:/Fwd: mail — never "Re: Re:").
`engine.backfill_missed_client_drafts(slug, days)` is the manual recovery sweep for mail the old gate
swallowed (ignores the seen-set, dedups on `request.gmail_id`, drafts only — never sends).
**Inbound attachments (2026-08-25):** images + PDFs on an inbound email reach the drafter — light refs
(`request.inbound_attachments`) on the card, bytes fetched fresh from Gmail at draft time by
`engine._request_for_draft` (never stored in the DB, never re-attached to outgoing mail). Caps: 4 files,
8MB each. Office documents — docx/xlsx/xls/pptx/csv/txt — are TEXT-EXTRACTED at draft time by
`cortex/doctext.py` (pure python: python-docx/openpyxl/xlrd/python-pptx, in requirements) and reach the
drafter as `request.attachment_texts` blocks; an unreadable file is declared honestly on the card, never
guessed at. Still unread: zip/rar, legacy .doc, video/audio. Attachments on OLDER thread messages are
not fetched — only the message being replied to.
**Project-lane routing (2026-08-25):** client mail on a DELIVERY-stage deal (Booked/Production/Final
Payment/Recurring) drafts on `email-handling` (whose `worker._RELATED_SKILLS` adds the prod-* skills'
rules), so project-management behaviour is trained there; Opportunity-stage and no-deal mail stays on
`sales-first-response`.

## Opportunity follow-up automation (2026-08-25)

Cadence (config: company `followup_cadence` profile override, else `crm.DEFAULT_CADENCE`): 4 chases
3d apart → 2 fortnightly check-ins → soft revivals at ~3 and ~6 months → stage **Dormant**. NEVER
auto-Lost (standing rule: nobody who approached us is dumped as dead; Lost = an explicit "no" only).
Follow-up cards are CONTEXT-AWARE: `_spawn_followup_card` feeds the drafter the deal `note`, open
reminders on the deal, and the real recent correspondence (`_deal_thread_context`, read from the
company's send mailbox). Reply handling: an inbound email from a chased contact PAUSES every armed
cadence on their deals (`crm.pause_followups`); if the email states a timeframe ("give us two weeks"),
Haiku extracts the phrase, CODE stamps the date, and the cadence re-arms for then — a notification
card tells the owner what was decided and quotes their words. Our reply sending re-arms a paused
cadence at the normal gap (`crm.resume_followups`, wired into the send path). Dormant deals stay
visible on the cockpit Opportunities screen (own section) and their senders are still recognised as
deal contacts on inbound.
Own/internal senders are never classified — that guard is what stops the team-CC rule looping.
The company's general email VOICE lives in its `email-handling` skill rules (Sensa's is distilled from
Gino's real sent mail, 2026-08-25) — `worker._RELATED_SKILLS` puts it in front of every
`sales-first-response` reply draft, and Talk-composed `email_draft` tasks read it as their own skill.
Lane-specific behaviour stays on the lane skill (first-response structure on `sales-first-response`,
never-pressure follow-ups on `sales-followup`). Every worker draft and manager check carries
`worker._now_line()` — the code-stamped current date/time in GST — and the universal
`sales-scheduling` rule anchors proposed call days to it (no "Monday or Wednesday" on a Tuesday).
Sensa-wide: no booking/calendar links to clients (email-handling rule); times are proposed in words.

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

## Media library (the YouTube catalog + review UI)

`media_assets` (live DB) is the catalog of every video on a company's YouTube channel — one row
per asset, per the locked YouTube/media spec. Sensa is loaded (322 videos). Each row carries the
inventory (id/title/privacy/views), a Haiku classification (content_type/client), and an
**understanding layer**: full audio transcript (Deepgram) + a 5-frame vision profile
(summary/industry/format/style/language) so Cortex can match enquiries to sample films without
watching anything. Enrichment pipeline note: the box's IP is bot-blocked by YouTube, so downloads
run from Rashad's machine (yt-dlp, residential IP) which ships audio+frames to `/tmp/yt_in/` for
the box worker — after scp, `chmod -R a+rwX` or the cortex user can't read them.

**coretex.uk/media** (`web/media/`, fitness-style same-origin sub-app) is the review UI: films
grouped by format, star ratings, drag-to-reorder. `rating` is the OWNER's subjective score — set
only by a human there, never overwritten by any AI pass (`suggested_rating` is the AI first pass;
shown hollow until he rates). Endpoints: `GET /api/media/library`, `POST /api/media/rate|order|edit`
(all behind cockpit auth). Nothing in this stack writes to YouTube.

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
- Screenshot scanning runs on the box (`POST /api/fitness/scan` -> `fitness.scan_screenshot`),
  Haiku, so the Anthropic key never leaves the server. It was previously called from the phone
  with a key in localStorage. The prompt forbids estimating: an unreadable field comes back null
  rather than guessed, because a wrong number in the log is worse than an empty box.
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
