# Cortex — operator runbook for Claude sessions

Cortex is Rashad's voice-first AI ops platform running all five companies (Tabscanner, Sensa,
SkyVision, FilmSpoke, Snap Rewards). This repo is the whole system; production runs on the
Hetzner box `cortex-1`. Read this before touching anything.

> **6th company, automation OFF (2026-08-29):** `flixtonmanor` (Flixton House Ltd t/a Flixton Manor,
> a UK care home) was onboarded as a company row with the uniform 85-skill roster, but it is NOT in
> `inbox_registry`, every skill is authority=ask, and nothing is scheduled — the poller/engine ignore
> it. It exists so the case tooling (a Grenke leasing dispute) can use Cortex's APIs, not as a live
> automated company. Its Google project is `flixton-cortex` (a service account + domain-wide delegation
> gave a one-off all-mailbox read; key held locally, never on the box). The seeder (`onboard.py` via
> `catalog.py`) is 4 skills behind the live roster — it seeds 81, so the 4 newest
> (email-handling, lead-qualification, outreach-anchor-engine, roadmap-ideas-parking-lot) were
> back-filled from the tabscanner baseline. Fix `catalog.CATALOG` before onboarding the next company.

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
- UNIVERSAL (all-company) rules are OWNER-ONLY (2026-08-27): team members (Gino/Ayresh, scoped to
  Sensa) can only add/confirm company-scope rules — enforced in the API, Talk's add_rule, and the
  cockpit UI (scoped users never see the All-companies option).
- ENVELOPE behaviour (from/cc/bcc) is rule-compiled (2026-08-27): `cortex/envelope.py` distils each
  skill's effective rules into `skills.envelope` config on every rule change; `_email_envelope` just
  executes it. Never hardcode cc logic — change the rules and the compiler follows. Safety invariants
  that stay code: one recipient per send; catch-alls never send.
- Telegram is a MIRROR, never the flow — its calls are fail-soft (`integrations/telegram.py`);
  keep it that way.
- Never delete CRM/contact data on your own initiative; merges carry over every non-empty field.
- Any Cloudflare WRITE needs Rashad's explicit OK first (read-only fine, but disclose).
- Batch/recurring LLM jobs default to Haiku (batched + prompt-cached); Sonnet = prose,
  Opus = ideation. Never silently upgrade a model. ONE standing exception (owner-approved
  2026-08-30): `provider.think_research` runs claude-fable-5 with live web search for the
  once-per-opportunity research pass (`pipeline.research_opportunity`, hooked into
  `crm.auto_opportunity`) - low volume, insight quality is the point; and FIRST replies on the
  sales-first-response lane (not thread continuations) draft on Fable 5 in `worker.draft` - the
  opener + insight set the conversation's direction.
- No emoji in the cockpit UI — clean monochrome line-icons (Tabler-style) only.
- Ship a new Talk capability? Add its one-liner to `runtime/cortex/capabilities.py` in the same
  commit — that manifest is injected into every system prompt.
- New scheduled work goes on the unified clock (`tasks` recurring templates +
  `engine.promote_due_tasks`) — `scheduled_tasks` is long dead. Report kinds on the clock:
  `seo_report` (weekly, per company) + `ppc_report` (daily 08:00 GST, Sensa Google Ads via
  `cortex/ppc_report.py`, REST creds `/etc/cortex/google-ads.yaml`, card lands on the
  `ads-google-search` lane). The daily report also runs `cortex/ppc_prune.py` first: Haiku
  classifies yesterday's paid search terms, junk becomes PHRASE negatives in the "Sensa PPC
  shared negatives" set, and the card lists every prune for operator veto. That shared set
  attaches to BUYER campaigns only — never attach it to a tool-capture campaign.

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

## Sender identity + personal voice (2026-08-31)

Every outbound email carries WHO it is written as, and writes in that person's voice.
- `profile.resolve_identity(company_id, email)` reads the person's real **name + role** from
  `data.signatures` (already keyed by email). `worker._identity_block` puts
  "YOU ARE WRITING AS <name>, <role> at <company>" in front of every email draft, with the
  first-person instruction, and passes that person as the voice `author`.
- `profile.resolve_voice` then resolves `data.voice.people.<who>` (matched by key OR email), so the
  person's own distilled voice reaches their own mail. **Sensa/SkyVision have gino, ayresh and rashad**
  (distilled from their real sent mail, 2026-08-31, via Fable 5). Catch-alls (hello@) resolve nothing —
  they never send anyway.
- The `email-handling` house voice rule is the BASELINE: a personal voice governs greetings, rhythm and
  phrasing; the baseline governs structure, formatting and what we never do.
- `manager.check` gets the same identity as a SYSTEM FACT and must flag a draft that names its own
  sender in the third person.
WHY: the engine drafted with `author=None` and never surfaced `request.from_email`, so the model wrote
"the quotation **Gino** sent across this morning" in an email sent AS Gino (card #411, 31 Aug 2026) —
it copied the deal timeline's third-person framing. Everyone also wrote in one shared voice.
To add a person: put `{name, role}` on their signature entry, and a `voice.people.<who>`
`{emails:[...], profile:"..."}` distilled from their sent mail.

## No-draft gate + skipped opportunities (2026-08-31)

`policy.py` compiles each skill's rules into a NO-DRAFT policy evaluated at TRIAGE, before a card
exists (`policy.should_skip`). Standing rule added for **broadcast tender/supplier circulars**: a mass
announcement to a whole supplier register ("Dear Supplier", tender number + closing date, from a
supplier-relations desk, asking nothing of us) gets no drafted reply - those are bid through the
issuing portal, not answered by email.
**The wording of that rule is load-bearing.** A first, looser draft of it ("bulk supplier circulars
and tender-invitation blasts") made the gate skip the Property Finder RFP as well - a deal-losing
false positive caught only by testing it against a real enquiry before shipping. The live rule
therefore carries explicit discriminators AND an explicit carve-out: it NEVER applies to an email that
asks US for something (RFQ/RFP addressed to us, a scope of work, questions, a named person expecting a
reply). **Any change to a no-draft rule must be re-tested against a real in-scope enquiry.**
Skipping used to be silent, so an in-scope tender vanished. `engine._flag_skipped_opportunity` now
judges every skipped mail against the company's services and raises a "Tender worth a look -
closes <date>" notification when it is in scope. Notification only; it never drafts.
Related: `engine._maybe_no_reply` (`_NO_REPLY_RX`) is the LATER net - if a card was created anyway and
the drafter concludes "RECOMMENDATION: skip", the card is closed and the owner notified rather than
leaving sendable text on a live email card (card #408).

## Company document library (2026-08-26)

CANONICAL HOME: the company's Drive `<COMPANY> CORTEX/Documents/` subfolder (same Drive-first doctrine
as the brand kit — the Drive folder is the controlled source; decided with Rashad 2026-08-26). The box
copy at `/opt/cortex-knowledge/documents/<slug>/` is the CACHE (instant send-time attach; also in the
nightly backup) and `company_documents` is the registry (`drive_id` links the canonical file). Saves
push to Drive first (fail-soft); `documents.sync_drive(company_id)` catches up both directions,
including files dropped into the Drive folder by hand. `runtime/cortex/documents.py`.
Storage doctrine overall: Drive CORTEX folders = source of truth for brand assets + official documents;
R2 (media.coretex.uk) = published/web-served derivatives only; the box = cache/runtime only. Upload via the
Talk paperclip + `save_document`, or the Attach button on any email card (fresh uploads save to the
library first). `attach_docs` refs on a card resolve to real bytes only at SEND time — attachments never
bloat task rows, and a client's inbound files can never be re-sent (separate `inbound_attachments` key).
Talk tools: save_document / list_documents / attach_document; `draft_email` takes `attach_documents`
by name. Catch-all mailboxes (INBOXES values) can NEVER be the From: `_draft_direct_reply` routes
catch-all-received replies via the company `reply_from` person, and `_email_envelope` hard-strips any
catch-all From as a backstop. Universal email-handling rule tells the drafter the library exists and
never to claim an attachment the tools didn't confirm.

## Opportunity follow-up automation (2026-08-25)

AUTO IS THE DEFAULT (owner, 31 Aug 2026). `crm.arm_new_deal()` runs on every creation path
(`create_project`, `create_deal`, `auto_opportunity`) and puts the deal straight onto the cadence.
Before this, automation defaulted to NULL/off and only the cockpit toggle ever armed it: 4 of
Tabscanner's 5 open opportunities were silently doing nothing. Never armed for Lost/Dormant/Nurture
(its own account-level loop)/Completed (silent by design). A deal with NO contact anywhere is left
off and raises ONE deduped "no contact" notification rather than nudging into a void every 3 days.
Existing pre-31-Aug deals were NOT retro-armed - that would fire a chase blast.
`_spawn_followup_card` resolves the contact the same way the quotation does: deal contact wins, else
the ACCOUNT's single emailable contact fills in, ambiguity stays blank (a deal whose contact sat on
the account but not the deal, Codexa, silently never drafted). The greeting name comes from
`crm_master` when the deal row has none.

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
AVAILABILITY is merged, not guessed: setting `availability_calendars` lists every calendar that owns
Rashad's time (his Tabscanner calendar, his personal WORK calendar shared in as FREE/BUSY ONLY, and the
Sensa Main Calender), each queried with its own company token. `calendar.free_slots()` turns that into
real openings - bunched next to existing meetings, preferring 10:00-14:00 GST, inside working hours at
BOTH ends (09:00 Dubai is 07:00 Amsterdam), weekends and short-notice excluded. The assembler serves it
as the `availability` shelf and the drafter may ONLY offer times from that list (31 Aug 2026).
PREP GAP: every busy block is padded by `calendar.PREP_GAP_MINUTES` (15) at BOTH ends before slots are
computed, so a proposed meeting can never start the moment another one ends - that gap is when Rashad
reads the pre-meeting brief. Bunching still holds; "next to a meeting" now means 15 to 75 minutes after
it, not touching it. Booking duration is unchanged (default 30 min, `mt.minutes` when the drafter
stamps one).
CALENDARS are per company: `calendar_refresh_token:<slug>` + `calendar_id:<slug>` (sensa = the Main
Calender on hello@; tabscanner = rashad@tabscanner.com's primary, added 31 Aug 2026). A company with
NO calendar is never booked onto another brand's - the pre-book and the send both stop and say so.
A confirmed slot in a draft is booked attendee-less at correction/draft time so the real Meet link is
in the email; the guest is invited by Google on approval. `_prebook_meeting` runs on BOTH the first
draft and the correction path - it was correction-only until 1 Sep 2026, so a first-pass reply that
agreed a time had no link to offer and wrote "I will send the link separately", with the send path
tacking a bare Meet URL onto the end (card 422). Booking before the draft is finished means the
drafter is served the real link and writes around it; it redraws once, only when the link is missing.
A failed pre-book now PRINTS its reason - the bare except made a booking failure look identical to
"no meeting was agreed". `_TIME_HINT` is what triggers that check -
it was deleted by mistake on 29 Aug and NO meeting booked until 31 Aug, so do not "tidy" it away.
DOCUMENTS: Drive is the SOURCE OF TRUTH. The library indexes both `<COMPANY> CORTEX/Documents/` and the
per-client folders under the profile's `clients_drive_folder` (where the quotation generator files its
work); the box copy under /opt/cortex-knowledge/documents is a CACHE and an outage fallback. Sends fetch
the current file from Drive, a deal-linked card may only attach that project's documents
(`documents.find(..., scope=<deal title>)`), and every attachment carries a Drive checksum PIN: if the
canonical file changed, moved or was deleted between approval and send, the send STOPS (31 Aug 2026 -
'attach the accompanying quotation' had matched another client's file).
Every email draft is also served a `media_library` shelf (top-rated live `media_assets` with their real
watch_urls) — sample-work links come ONLY from it; `engine._ensure_real_links` redrafts any email whose
URLs aren't in the served context, and the manager receives the same computed URL allowlist plus a
code-computed 28-day calendar (it never does weekday arithmetic) and may only summarise its own listed
issues (2026-08-30, after invented library links on card 384 and a false date error on 385).

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

## LinkedIn outreach engine (harvest + warm-ladder + connect)

Runs on the office boxes (Patchright runners, code scp-synced NOT git-deployed; brain in
`runtime/cortex/social*.py` + `/api/social/*` behind `_runner_auth`). Two accounts in
`social_accounts`: **rashad** (harvest-only, all write caps 0) + **live** = Paul Anderson
(FilmSpoke persona, the connector). ~5,900 tier-scored buyers harvested off graded anchors sit in
`crm_master` (tag `anchor-harvest`, stage Cold).
- **WARM-LADDER (LOCKED 2026-08-26, rule #3 on FilmSpoke `outreach-linkedin-sequences`):** harvested
  buyers are mostly 3rd-degree Follow-primary profiles LinkedIn walls to cold no-mutual invites. So
  per buyer: **(1) follow** (auto, safe) → **(2) ~5 genuinely insightful comments, EACH an approval
  card** → **(3) only then the silent no-note connect**. `social_warm.connect_targets` ONLY surfaces
  buyers with ≥4 done comment cards (`WARM_COMMENTS_TO_CONNECT`); cold buyers can't be connected.
  `queue_warm` makes ≤1 new comment card per person per run, stops at 5. `record_connect` writes the
  invite outcome to the CRM (sent → `invited`+Contacted; permanent fail/email-wall → `invite-skip`,
  never re-served). `post_action_card` carries `request.person` so warmth counts per person.
- **Runner (`C:\Users\Dell\cortex-runner` on Dell@100.72.188.65):** `warm.py` (follow + read ~3 recent
  posts → comment cards), `run_shift.py` (executes only WARMED-gated connects), `actions.follow/connect`,
  `poller.py`. **Never rely on LinkedIn CSS classes — they're hashed;** `warm_read.read_post_text` picks
  the longest prose `[dir=ltr]` block above comments (drops video chrome + pipe-headlines; warm.py drops
  <180-char extracts). One profile = one Chrome at a time: `runner._open` RETRIES the real Chrome channel
  (no bundled-chromium fallback — it isn't installed), and the poller leaves a "profile busy" job QUEUED
  rather than burning an approved card. Stuck scheduled task → `schtasks /end` then `/run`; stray lock →
  `taskkill /F /IM chrome.exe`. Scheduled (Paul): CortexWarm 09:30, CortexShift 11:00, CortexAccepts 12:00,
  CortexHarvest 13:00, CortexReplies every 4h from 10:15, CortexPoller 10min. Detail + history: memory
  `project_cortex_social_automation.md`.
- **Acceptance monitor (`social_connect.py` + runner `accepts.py`, 2026-08-26):** revisits each invited
  contact (`pending_accept_checks` = tag `invited`, not yet accepted/declined), reads connection degree
  (1st = accepted), and `ingest_accepts` marks them `accepted` + stage Contacted->Engaged + raises a rolling
  "new connections" notification and refreshes `connect_report` (invites/accepted/pending/accept-rate card).
  Detection validated (1st vs 3rd). This is the success-rate loop: invited -> accepted tags on the harvested
  buyers give the accept rate to analyse.
- **Comment-reply watcher (`social_comments.py` + runner `notify.py`, 2026-08-26):** revisits the posts the
  persona recently commented on (`pending_reply_checks` = done comment cards, last 12d), reads replies UNDER
  the persona's own comment that aren't theirs, and `ingest_replies` drafts the persona's response (personable
  voice, deduped on `reply_seen:<account>`) as a `social_action` action='reply' card. The card carries
  `parent` (the persona's comment) so `actions.reply` can locate the thread; governed as a comment, paced.
  A real back-and-forth warms a target far more than one-way comments.

## Sales-loop integrity (pipeline.py, 2026-08-28)

`runtime/cortex/pipeline.py` closes the loop between communications and the pipeline — built because
manual sends and un-tracked promises were leaking (Gino's Property Finder reply bypassed everything,
Aug 2026). Four mechanisms, all fail-soft, wired into the engine:
- **Deal timelines:** every send (`_send_email_reply` -> `pipeline.record_send`) and every inbound on
  an active deal (`_draft_direct_reply` -> `pipeline.record_inbound`) appends to
  `crm_projects.history`. `pipeline.deal_context(deal_id)` renders the timeline into the drafting
  brief, so drafts know the whole flow (what we promised, what they asked, where the deal stands).
- **Commitments:** Haiku extracts the promises an outbound email makes ("revised quote coming",
  "samples to follow") — extraction only, never invention; CODE stamps the check-in date (explicit
  date used as stated; vague hints map to fixed windows; default 3 days) -> timeline entry + reminder
  (`created_by='cortex-pipeline'`, target deal).
- **Client deadlines:** inbound mail is mined for EXPLICITLY stated deadlines ("respond by 1 Sep
  3pm") -> high-priority reminder a day ahead + timeline entry. Never inferred urgency.
- **Sent-folder sweep** (`pipeline.sweep_sent`, engine loop, 30-min self-gate): reads every
  registered mailbox's `in:sent`, skips Cortex's own sends (gmail_id match in `decisions`) and
  internal mail; a manual send to a deal contact logs `email_out_manual` + commitments and re-arms
  the follow-up cadence; a manual send to a known lead with NO open deal raises an "Untracked sales
  email" notification. Seen-sets in `sent_seen:<rt_key>` settings.
Deal lookup (`crm.open_deal_for_email` / `active_deals_for_email`) falls back to the deal's own
`contact_email` when the contact has no account row — account-less opportunities still resolve.

Phase 2 (same day): the full flow-of-intelligence. All triggers are deterministic code; models only
extract/judge, never move stages or invent values:
- **Stage engine:** a send carrying a Quotation/Proposal (attach_docs filename or subject — a fact,
  not a guess) advances Opportunity -> Quote (`maybe_advance_on_send`). `crm.set_project_stage` calls
  `pipeline.on_stage_change`: crossing INTO a won stage logs `project_start` + spawns a kickoff card
  (action reminder -> Inbox, timeline-grounded, "never invent dates or scope") + notification —
  won deals convert to tracked projects automatically; `Close & review` surfaces the wrap-up
  (final files, testimonial, media-library entry).
- **Commitment settling:** each outbound on a deal first settles open commitment reminders it
  genuinely fulfils (conservative Haiku judgement -> `mark_done` + `commitment_done` on the
  timeline), THEN tracks the new promises it makes — a commitment can't be marked done by its own email.
- **Next-step engine** (`suggest_next_step`, on inbound deal mail): when the client's email requires
  an internal deliverable beyond a reply (quote revision, proposal, document), a prep card spawns in
  the Inbox alongside the reply draft — kind `content`, `sales-quotation` lane, timeline-grounded,
  prices/dates the owner hasn't stated marked OWNER TO CONFIRM. Deduped per message
  (`nextstep_seen:<deal>`); conservative (most mail spawns nothing).
- **Meetings feed the loop:** `meetnotes.sweep` -> `pipeline.record_meeting` on deal-matched
  meetings: summary onto the timeline, OUR action items become commitments with reminders.
  BACKFILL: `sweep(days_back=N, min_gap_minutes=0, backfill=True)` recovers OLD meetings as memory
  only - notes + timeline history, but NO commitments-into-reminders and NO post-meeting follow-up
  card. Use it for anything older than the normal 7-day window; a naive backfill dates a four-month-old
  promise from today and drafts thank-you emails for calls held in the spring. Ran 31 Aug 2026 over
  150 days: 10 recovered (2 -> 12 notes), 5 matched to deals, tasks and reminders unchanged.
  GEMINI NOTETAKER IS SENSA ONLY, and that is a DECISION, not a gap: it is licensed on the Sensa
  Workspace and NOT on rashad@tabscanner.com (evidence 31 Aug 2026: 28 Tabscanner meetings over 120
  days, zero with any attachment; Sensa 16 meetings, 9 with Gemini docs). Rashad declined the ~$40/mo
  to add it (31 Aug 2026). Do NOT "fix" this by extending the sweep to Tabscanner: there is nothing
  there to read. Tabscanner call notes reach Cortex by Rashad pasting or dictating them into Talk.
Lead -> opportunity conversion was already automatic on both intake lanes (qualify + auto_opportunity);
with won -> project and Close & review now wired, the lead -> opportunity -> project -> close chain is
closed end to end.

## Rate card (2026-08-28)

`rate_card:<slug>` setting = the company's per-unit pricing reference (`runtime/cortex/ratecard.py`:
get/save/set_item/render). `worker.draft` injects `ratecard.render()` into every quotation-adjacent
draft (sales-quotation, sales-first-response, email-handling). RATES ARE OWNER-APPROVED ONLY: they
enter the card from quotes Rashad approved or figures he states — a missing/unconfirmed item is
drafted as OWNER TO CONFIRM, never priced by a model (rule on both companies' sales-quotation).
When Rashad states a new rate in any session, save it with `ratecard.set_item` so every future
quote uses it. LOCKED v1.0 (2026-08-28, owner-approved, two tiers Budget/Normal): the human-readable
master `Sensa - Rate Card v1.0.xlsx` lives in Rashad's Drive terms folder
(`159mEGnuBsWh_GqfPfPTf3q3zaNautfnX`) beside the T&Cs; amendments = new version there + live setting
updated together. Quotations auto-fill the QUOTATION TO contact (name/email/phone) from the CRM:
explicit `contact_email` wins, an unambiguous single-contact account fills in, ambiguity stays blank
(quotation._contact_for; deliver_quotation passes contact_email). TERMS MODULES: reusable clause modules live as `terms_module:<name>` settings (human-readable docx beside the T&Cs in the Drive terms folder + library). First module: `multi-version` (v1.0, 2026-08-30) - the approval-gate + enhanced-revision-service + masters-first versions structure for multi-cut/multi-language jobs, born on SEN-2026-0004 (Property Finder); the sales-quotation rule tells the drafter to apply it, gen path: pass a per-quote `terms` dict to generate_xlsx with the module clauses swapped into Revisions & Delivery.

## Pre-meeting briefs (`meetingprep.py`, 2026-08-31)

Any calendar entry with an attendee OUTSIDE our domains gets a one-page research brief 24h before it
starts. `calendar.upcoming_events()` reads the registry calendars for detail (the personal calendar is
shared free/busy only, so it returns nothing there and is filtered out as "internal"); `meetingprep
.sweep()` runs from the engine loop, self-throttled to every 10 minutes, one brief per event ever
(ledger table `meeting_briefs`, unique on `event_id`).

The card is INSERTED at `awaiting_approval` with the draft already set, never at `new` - a `new` card
gets worker-drafted and that would throw the research away. Kind `meeting_brief` is registered
`internal` in KIND_CLASS (an unregistered kind fails safe to `outward` and would demand a biometric
step-up to read a brief). Approving means "mark as read": nothing ever sends.

MODEL: names `claude-opus-5` OUTRIGHT via `BRIEF_MODEL`, it does NOT use the `opus` tier.
`/etc/cortex/cortex.env` pins `CORTEX_MODEL`, so a skill marked `opus` resolves to whatever that pin
says (it said `claude-sonnet-4-6` until 31 Aug 2026, meaning every "opus" skill was really Sonnet).
Owner-approved exception: ~$0.50 per brief, a handful a week, the insight is the point.

Behaviour lives in the `meeting-prep` skill craft (uniform roster, all six companies, `ensure_skills()`
is idempotent and never overwrites an edited craft). Sections: THE MEETING / THE COMPANY / WHY WE ARE
MEETING / WHERE WE STAND / WHAT THEIR ASK LIKELY MEANS / SCOPING QUESTIONS (max 6, de-risking, never
generic) / WARM OPENERS (4: two personal, two business, said out loud not read) / DO NOT RAISE /
OUR RELEVANT WORK (dropped entirely when the company has no media library) / SOURCES.

Safety invariants stay in CODE, not the editable skill: `_PERSON_SCOPE` limits people research to
public professional sources and local colour (never family, health, religion, politics, finances or
private accounts), every external claim carries a date, and links are either from the served media
library or a page actually found in search.

WHERE THE BRIEF LIVES. Three places, all written at brief time:
* `coretex.uk/brief/<id>?k=<key>` - standalone phone-first page, `noindex`. The key is
  `meetingprep.brief_key()`, an HMAC of the task id under the API secret, so it cannot be guessed and
  grants nothing else. Signing lives in meetingprep, NOT api, so the engine can build a link without
  importing the web app.
* A PRIVATE 15-minute `Prep: <meeting>` block immediately before the meeting, description = the link.
  NEVER the meeting invite itself: an invite description is visible to every guest, so putting the
  brief link there would hand our own intelligence to the client.
* The deal timeline (`pipeline.log_deal(..., 'brief', ...)`) and the notification body.

## Auto-replies carry real news (`autoreply.py`, 1 Sep 2026)

An "Automatic reply" is correctly never ANSWERED, and the whole class was therefore discarded - which
threw away the single most important thing a chase can come back with. On 1 Sep 2026, one minute after
the ITC payment chase went to Tim Piper, his auto-reply said "I no longer work for EY. Please direct
your queries to konstantinos.kanellaidis@parthenon.ey.com". Nothing recorded it, and the deal would
have kept chasing a dead address.

`autoreply.is_auto()` catches it (Auto-Submitted header or an auto-reply subject) BEFORE the robot
gate in `_draft_direct_reply`, and `handle()` acts without ever drafting: marks the contact
`lead_status='left-company'`, adds the named successor to the same CRM account, repoints every
affected deal's `contact_email`, logs `contact_left` on each timeline, and raises a high-priority
notification. Candidate addresses come from a REGEX over the body and the model may only CHOOSE from
that list, so a successor can never be invented.
Still to do: an out-of-office with a return date should shift the follow-up clock instead of firing
into an empty inbox (`read()` already returns kind='away' for it).

## A card may only delete an event IT created (1 Sep 2026)

`_skip` used to delete any event whose id sat on the card's `request.meeting`, guarded only by
`not mt.invited`. On 1 Sep 2026 Rashad DISMISSED the ECBD pre-meeting brief (card 419) and that
deleted the real client meeting it was briefing, 3.5 hours before it started. Sunwoo Yoo was the only
attendee, so Google emailed her a cancellation and cleared it from her calendar; she did not join, and
the room had to be re-shared by hand mid-meeting. The brief card carried the same `meeting` block as
the email cards, without their `invited: true`.
Three guards now, keep all three: the deleting card must be an EMAIL_RENDER_KIND (only a draft
pre-books a slot), `calendar.event_has_guests` refuses any event with an attendee and FAILS CLOSED,
and `meetingprep` stamps `invited/readonly` on the brief's copy of the meeting. A stale slot costs a
little availability; a wrongly-cancelled client meeting costs the meeting.

## Approvals: the step-up must COMPLETE the action (1 Sep 2026)

`POST /api/stepup/pin/verify` takes an optional intent (`task_id`, `action`, `run_at`) and runs
`engine.approve_task` inside that same request. Do NOT go back to issue-proof-then-let-the-page-retry.
The old flow was: approve (rejected, no proof) -> PIN verify (proof issued) -> page fires approve
again. Card 418 lost the third leg because cortex-api restarted in that 2-second window, so a valid
proof sat unused and the card stayed `awaiting_approval` while the cockpit had moved on. A reload, a
service-worker update or a backgrounded phone does the same. The fingerprint route carries no intent
and still fires its own approve; `stepUp()` resolves `{token, done}` for both.
Related earlier failure, same area, different cause: `_approve(..., stepped_up=True)` - the cockpit
gate had already consumed the proof, and re-checking it inside `_approve` blocked cards 383/384.

## Personal company (`personal`, id 29, 2026-08-31)

`companies.kind='personal'` — Rashad's own life, NOT a business. Created on his instruction to run
automation against his PERSONAL Google account (calendar, Gmail, Drive) rather than a company one.

**`kind` is the fence.** Business-wide loops must filter `where kind='owned'` so a personal company is
never treated as a brand. Already fenced: `crm.marketing_state` (personal is NEVER a marketing
audience - standing rule) and `meetingprep.ensure_skills`. Fence any new all-company loop the same
way, and never add `personal` to `inbox_registry`, a newsletter, a campaign audience, or the
opportunity pipeline.

**The personal Google account needs an EXTERNAL OAuth client** - `/etc/cortex/google_oauth_client_
personal.json`, project of its own, redirect `https://coretex.uk/oauth/google/callback`. A consumer
@gmail.com cannot use an Internal app, and Internal is what every other company uses (which is why
their tokens never expire). Publishing status MUST be **"In production"**: External + "Testing"
issues a refresh token that **expires after 7 days**. Unverified production is fine here - it costs
one "Google hasn't verified this app" click and carries a 100-new-user cap that is irrelevant for one
user. Gmail scopes are RESTRICTED, so a Google password change revokes that token; Calendar + Drive
are only "sensitive" and survive it.

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
- BUILT 31 Aug 2026: company id **29, slug `personal`, kind='personal'** (see "Personal company" below).
  The note below is the original design; the roster question is still open.
- A personal "company" in Cortex is AGREED but NOT BUILT: `companies.kind` would be `personal` with
  its own smaller skill roster (the uniform-85 roster rule applies within `kind='owned'`), and
  personal CRM contacts must be suppressed from every campaign audience. Do not build it uninvited.

## Media library

`media_assets` = the rated, categorised library of every published film (UI: coretex.uk/media; API
`/api/media/library`, tag/rate/edit endpoints). Categories are LOWERCASE SLUGS (`interviews`,
`real-estate`, `aerial`...) — query with slugs, not display names. It is the canonical portfolio
source: sample links in emails and example films in proposals come from category-intersection
queries here (highest-rated first, use `watch_url`), never from ad-hoc YouTube scans.

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
