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

## Start of every session: the repeating-fault list (owner, 18 Oct 2026)

Run `ssh cortex "cd /opt/coretex/runtime && sudo -u cortex /opt/coretex/.venv/bin/python -m cortex.invariants repeats"`.
Anything listed is a fault the nightly health check found on 3+ of the last 7 days: the repair is a patch, so
trace it to the code that keeps creating it and fix it there (with its regress case) before other work.

## Deploy (never scp code; push + pull) - THROUGH THE GATE (13 Oct 2026)

**`bin/deploy.sh`** (from Git Bash): push, pull on the box, run the case library, restart ONLY if every case
passes; a failing case rolls the box back to the commit it was running and the services are not restarted.
`--fast` skips the cases that call Haiku. **Every fix ships with its case** in `runtime/cortex/regress.py` (the
decision function that went wrong, the real message that broke it, what must be true), and, when the fault is a
state that should never exist, a query in `runtime/cortex/invariants.py`. The health check runs every morning
after 05:00 Dubai (`invariants.maybe_run` in the engine loop), applies only safe reversible repairs, and raises one
notification; `python -m cortex.invariants [--fix]` runs it on demand. A fix with no case is not done.

The manual steps below are what the script does:

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
- A STANDING RECIPIENT LIVES ON THE COMPANY PROFILE, never on one skill (4 Sep 2026). `always_cc` /
  `never_cc` / `always_bcc` in `company_profiles.data` apply to EVERY email that company sends; a
  `cc_add` on a skill applies to that lane alone. "Always CC Ben" sat on Tabscanner's
  sales-first-response and nowhere else, so Ben rode inbound sales replies and was missed by every
  owner-composed draft, follow-up and project email (card 474). `envelope.audit_company()` reports who
  is copied on which lanes and who is only half-covered; `check_coverage()` raises a deduped
  notification naming the missing lanes and runs on EVERY rule change, so partial coverage can never
  appear silently again. `promote_to_company()` moves a recipient to the profile.
  ALWAYS-BCC IS THE RECORD COPY (owner, 11 Sep 2026): global setting `always_bcc` (rashadalsafar@gmail.com,
  every company) plus the profile's `always_bcc` (Sensa: gino@sensa.digital) ride EVERY email, replies and
  Talk outbound alike. Unlike cc, an always-bcc address is KEPT when that person is the sender, so Gino gets
  a copy of what went out as him; anyone already on the cc is not bcc'd as well. Only never_cc and the To
  recipient remove it. Checked on a Gino send: cc rashad, dalal, ayresh; bcc rashadalsafar@gmail.com, gino.
  CATCH-ALLS ARE NEVER COPIED, only received on: hello@sensa.digital and fly@skyvision.film are in
  their companies' `never_cc`. Fixing a misfiled rule means editing the RULE TEXT, not just the
  compiled config - a recompile rebuilds `skills.envelope` from the rules.
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
  **COST GUARDS (14 Sep 2026, after a $30 day, double the usual):** "first reply" now means a REAL inbound
  message answered for the first time (`worker.draft` `_first`: not a follow-up, not outbound, not a correction
  or redraft, not a thread continuation). It used to be anything without `thread_reply`, so automatic chases,
  corrections and guard redrafts all ran on Fable: 29 Fable drafts ($10.34) on one Monday, when the weekend's
  chases roll over. The drafter's system prompt is now stable per skill + company + sender (the code-stamped
  date moved to the top of the user message) and prompt-cached; the Manager's stable half (its brief, the
  company, the standing rules) moved into its cached system prompt, the per-draft facts and the draft stay in
  the user message. `anchor_score.classify_leads` runs on Haiku (it said Haiku but `fast=True` meant Sonnet,
  $1.40 to $3.80 a day). Blog compose/revise start at 24,000 tokens and internal/inbound links at 4,000: at
  8,000 every post truncated and was paid for twice. The Manager's verdict gets 4,000 (it truncated at 1,500
  and re-ran). Verified live: a Manager check now reads ~19,000 tokens from cache and sends ~4,300 fresh,
  about $0.027 a check against ~$0.065 before.
- No emoji in the cockpit UI — clean monochrome line-icons (Tabler-style) only.
- Ship a new Talk capability? Add its one-liner to `runtime/cortex/capabilities.py` in the same
  commit — that manifest is injected into every system prompt.
- New scheduled work goes on the unified clock (`tasks` recurring templates +
  `engine.promote_due_tasks`) — `scheduled_tasks` is long dead. Report kinds on the clock:
  `seo_report` (weekly, per company) + `ppc_report` (daily 08:00 GST, Sensa Google Ads via
  `cortex/ppc_report.py`, REST creds `/etc/cortex/google-ads.yaml`, no developer-token header since 2026-09-14 (Google's Cloud-project transition; access = OAuth project sunny-jetty-428307-a4), card lands on the
  `ads-google-search` lane). The daily report also runs `cortex/ppc_prune.py` first: Haiku
  classifies yesterday's paid search terms, junk becomes PHRASE negatives in the "Sensa PPC
  shared negatives" set, and the card lists every prune for operator veto. That shared set
  attaches to BUYER campaigns only — never attach it to a tool-capture campaign.

## Email intake (full-mailbox triage)

**THE SLOW LANE: long generation never holds up email (14 Sep 2026).** `engine.run` is one loop that drafts
every new card (`process_new_tasks`) BEFORE it polls mail, so a run of FilmSpoke blog drafts (a couple of
minutes each, retried at 24k tokens) held the inbox sweep for 20+ minutes: Honor's email asking for the
proposal as a PPT sat unseen and two client replies waited to be drafted. Kinds in `engine._SLOW_KINDS`
(blog, blog_menu, newsletter_idea, seo_report, ppc_report) now drain one at a time on their own thread
(`_start_slow_lane`, like the newsletter drip); emails and every other card stay on the loop. When mail
"never arrived", check `engine_heartbeat` and the seen set before assuming the sweep missed it.

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
**CONTINUING THE EXISTING THREAD IS THE DEFAULT, for every company (owner, 4 Sep 2026).** A follow-up,
a chase or any deal-linked draft continues the real Gmail conversation; only a genuinely cold contact
with no work context opens a new one, and `request.new_thread` is the deliberate override. It is CODE,
not per-company config, so it applies everywhere by construction. Three separate faults had made it
fail silently, all fixed 4 Sep 2026 after card 464 opened a fresh thread with Jonathan Bobo:
  1. `_company_senders` was built ONLY from per-person `gmail_account:<slug>:<who>` settings. Tabscanner
     has none (just the catch-all api@ and one send account), so it returned `{}` and adoption had no
     mailbox to search: EVERY Tabscanner email opened a new thread. The company send identity is now a
     sender in its own right. Never a catch-all - that is a receiving address.
  2. The look-back for established work was 45 days, SHORTER than the 180 the drafter already reads as
     `thread_history`, so a card could be written from a conversation it then refused to continue. Now
     180 for a follow-up or deal-linked card; 21 stays for a cold contact.
  3. It fetched exactly ONE message per mailbox, so a calendar invite or an out-of-office on top of a
     live thread made it give up entirely. Jonathan's French auto-reply sat above a four-message
     thread. It now reads up to eight and takes the newest message that is really the conversation.
Check it with the `existing_thread(<who>)` entry in `request.context_manifest`; its absence on a
deal-linked card means adoption found nothing, which is worth investigating rather than assuming.

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

## Where Cortex files live on Drive (5 Oct 2026)

The owner's rule: shared company files live where the team can see them. Moved 5 Oct 2026; folder ids did not
change, so every `asset_folder` link and every filed `drive_id` still works.

| What | Where (shared drive / path) |
|---|---|
| Sensa brand kit, handovers, official documents | **SENSA SHARED / CORTEX / SENSA CORTEX** (`asset_folder`), final documents in `Documents/` |
| Sky Vision | **SENSA SHARED / CORTEX / SKY VISION CORTEX** |
| FilmSpoke | **SENSA SHARED / CORTEX / FILMSPOKE CORTEX** |
| Client work (proposals, quotations, briefs, deliverables) | **SENSA SHARED / SENSA CLIENTS / <client>** (`clients_drive_folder`) |
| Terms, quotation templates, rate card | **SENSA SHARED / SENSA TERMS AND CONDITIONS TEMPLATES** (`terms_drive_folder`), old in `OLD` |
| Tabscanner, Snap Rewards | COMPANY DOCUMENTS SHARED / CORTEX / <COMPANY> CORTEX (not the Sensa team's) |
| Nightly DB + knowledge backups | COMPANY DOCUMENTS SHARED / CORTEX root (`1oEKRo6...`, private: never move into SENSA SHARED) |

SENSA SHARED / CORTEX = `1ai3ek_67CaGsZNhIHw5WByStVQDyxpF2`. Cortex reaches all of it as rashad@sensa.digital
(organizer on both drives). Its OAuth scope only lets it move files it created itself, so moving folders it did
not create is done in the Drive UI. The same map is a universal rule on the `general` and `prod-asset-library`
skills and a Talk note, so Talk answers "where is X saved" from it.

## Drive filing: latest at the top, history in Archive (11 Sep 2026)

The owner's rule for every Drive folder Cortex files into:
- **SENSA CORTEX / Documents** holds ONLY Sensa's own FINAL official documents (company profiles, AI
  Production Capabilities Deck, trade licence, VAT certificate): the current version at the top, older
  versions in `Documents/Archive`. **No client work is ever filed there, and no terms either.**
- **The terms folder** (profile `terms_drive_folder`, Sensa = `159mEGnuBsWh_GqfPfPTf3q3zaNautfnX`) holds
  the standing contract documents: every terms set, terms modules, the quotation templates and the rate
  card. Current version of each at the top, every lower version in `terms_archive_folder` (Sensa = his
  `OLD` subfolder; Cortex CAN move its own files into it). `documents.push_to_drive` routes any official
  file whose name says Terms / Rate Card / Quotation Template there and archives lower versions of the
  same document (`_file_terms_set`); `_export_templates` reads the folder from the profile and records
  its library rows against the terms-folder file (`push=False`), which is how Documents used to fill up
  with template copies. Two files Rashad made himself stay at the top because Cortex cannot move them:
  Master Terms v1.0 and Short Form Terms v1.0 (drag them into OLD by hand).
- **SENSA CLIENTS / <client>** holds the LATEST version (the one sent or approved) at the top, beside
  anything the team puts there by hand; earlier iterations go into `<client>/Archive`. A client with more
  than one project gets a folder per project, each laid out the same way. An AGENCY (EY, Auditoire) always
  gets a folder per project, because the work is for the agency's client.

How the code keeps it that way:
- `documents.push_to_drive` routes by document: a row with `client` -> that client's folder; an official
  document (`_is_official`: kinds company-profile / trade-licence / vat-certificate / capabilities-deck /
  terms, or a "Sensa ..." / "Sky Vision ..." name) -> Documents, the previous same-name copy moving to
  Documents/Archive; anything else stays in the library only. It used to push EVERYTHING to Documents,
  which is how that folder reached 122 files (one quotation template exported ten times).
- Proposals and rebranded decks save with `push=False` and are filed only in the client folder, their
  library row pointing at that file (`drive_id`, `client`). A new proposal for a quote number moves the
  earlier ones for that number into Archive.
- `_push_quote_to_client_drive`: filing quotation vN calls `drive.archive_superseded(folder, "Quotation
  <number>", keep=just-filed names, exclude="Terms and Conditions")`, so only vN sits at the top and the
  quote's terms document is never swept up. The ALL VERSIONS workbook lives in Archive.
- `drive.ensure_client_folder` checks setting `client_folder_aliases` FIRST (lower-case client name ->
  folder id), so merged or renamed clients keep filing into the merged folder, not the emptied old one.
  Set: honor international fzco -> Honor / Honor - X9D x Noon KSA Campaign; orion global fzco,
  orion global - fzco, orion event management, orion global -> Orion Global FZCO.
- **Cortex's Drive login is `drive.file` + `drive.readonly`: it can only move, rename or modify files IT
  created.** Anything a person put in a folder stays exactly where it is; say so rather than pretend.

The one-off tidy that set this up is logged move by move in setting `drive_tidy_2026_09_11`
(file id, from, to, why) so any of it can be reversed. Nothing was deleted. Two emptied folders were left
for the owner to delete: `Orion Global - FZCO` and `Honor International FZCO`.

## Approving a quotation prep card builds the quotation (11 Sep 2026)

A quotation PREP card (kind `content`, `request.prep_action == "quotation"`, see `_is_quotation_prep`)
used to fall through `_execute` to "mark done": the approve button silently threw the prep away and
built nothing. Now:

- **Answered** (the owner replies on the card): `apply_correction` -> `_prep_build_quotation` builds it
  from his words. Unchanged.
- **Approved without answers**: `_execute` calls the SAME `_prep_build_quotation`, with the card's own
  draft as the brief ("APPROVED AS-IS"). What "the card's defaults" means is NOT in code: it is the
  universal `sales-quotation` rule "APPROVED WITHOUT ANSWERS" (standard rate-card tier, never the budget
  tier; first-listed alternative; open-quantity add-ons unpriced and named in the note; addressed to the
  deal's primary contact; quotation only, never a proposal). `_prep_quote_spec` now reads the quotation
  skill's rules for exactly this reason.
- Prices keep the existing guard either way: a figure survives only if it is in the owner's own words
  on the card or is a rate-card rate. Everything else prints blank.
- The defaults the model assumed come back as `assumptions` and are appended to the CLOSED PREP CARD
  for the owner. They are never printed on the client's quotation.
- A failed build leaves the prep card `awaiting_approval` and returns `blocked`, which `_approve` hands
  straight back (no streak bump) and the cockpit shows as a toast. Nothing is sent in any case: the
  result is a quotation card for review.

## An empty draft is a failure, and the To line owns the reply (11 Sep 2026)

Card 545 (Antoni Entertainment) reached the Inbox with an EMPTY body and the wrong sender. Approving
would have sent "Best regards" and a signature, from Rashad, in reply to an email addressed to Gino.

**Out of room is not an answer.** Both draft attempts used exactly 6,000 of 6,000 output tokens (see
`usage_log`): Sonnet 5's adaptive thinking spent the whole `max_tokens` reasoning and wrote nothing.
`budget_tokens` is REJECTED on Sonnet 5 / Opus 5 / Fable 5, so thinking cannot be capped directly; the
levers are `max_tokens` headroom and `output_config.effort`. `provider.think` used to join the text
blocks and return "" without looking at `stop_reason`. Now, on a `think_hard` call: empty text with
`stop_reason == "max_tokens"` retries once with `max_tokens = max(4x, 24000)` (streamed) and
`output_config={"effort": "medium"}`; still empty raises `provider.EmptyCompletion`. Only `think_hard`
calls raise (`worker.draft`, `newsletter.generate_idea` - the latter runs at 1,200 tokens and is the
likeliest to hit it). Drafts now run at `max_tokens=16000`, a ceiling the model cannot see that costs
nothing unless used. **5 empty-draft cards in the 60 days before this.**

**An empty body never sends.** The approval gate refuses a whitespace-only draft, and
`_send_email_reply` re-checks the row it just claimed and releases it to `awaiting_correction` rather
than sending. `EmptyCompletion` in `process_new_tasks` leaves a visible, unsendable card with the reason
and a notification - never a silent `failed` row, which the Inbox does not show.

**The To line owns the reply.** Everyone is on `always_cc`, so a client writing TO Gino lands in
Rashad's mailbox too, and "personal mailboxes reply as themselves" made the reply Rashad's.
`_addressed_person(co, e, mailbox)`: when the swept mailbox is NOT on the To line and exactly one of our
people IS, the reply is theirs, from their own mailbox (so adoption finds the thread there too). None
when the mailbox is on the To line, or when none or several of ours are. High-value routing outranks it.

## Intake: seen is not the same as handled (11 Sep 2026)

Gino forwarded three enquiries that "never came through Cortex". All three HAD been seen by
`poll_inbox` (their ids were in `inbox_processed:sensa`); each was filtered on purpose by a different
gate. When something "never arrived", check the seen set before assuming the sweep missed it.

1. **A rule's own exception is a fact, so code checks it.** Sheraa's urgent ERF film RFP went DIRECTLY
   to hello@sensa.digital, closing next day. The no-draft check matched it to "broadcast tender/supplier
   circulars ... (not addressed to us specifically)" by reading the body. `policy.should_skip(...,
   own_domain=)` now removes any situation whose own words limit it to mail NOT addressed to us
   (`_ONLY_WHEN_NOT_ADDRESSED`) whenever our domain is on the To/Cc (`_addressed_to_us`), before the
   model sees the list. Edit the rule's wording and the behaviour follows.
2. **Skipping the reply never skips the person.** The skip branch used to `continue` before the CRM
   step, so Massar's RFP (a real BCC blast, rightly not replied to) erased the prospect too.
   `_record_contact()` now runs on both paths.
3. **Which categories get a card is data:** `policy.card_categories(co)`, setting
   `card_categories:<slug>`, default `("lead","client","finance")`. Antoni Entertainment (a `partner`)
   stayed on the default by the owner's call; adding `partner` is now a setting, not a deploy.
4. **Relevant blasts are picked up, not just announced.** `_track_tender` makes an in-scope supplier
   circular an opportunity (title `Tender: <subject> (<domain>)`, one per circular, repeats land on its
   timeline) created on **MANUAL**, because `create_deal` arms the auto cadence and a portal tender must
   never be chased by email. `closing_reminder` takes the date the extractor reads, validates it in
   code, and treats a DATE-ONLY deadline as END of that day: as midnight, a tender closing today counted
   as already closed.

Proven on the real emails: Sheraa -> addressed to us, broadcast rule removed, `should_skip` None (would
now draft). Massar -> not addressed, still skipped. Backfilled as deals 117 (Massar) and 118 (Sheraa).

**Known and not fixed:** the no-draft check gave Massar's skip reason as "sales emails", a rule meant
for vendors pitching TO us. Right outcome, wrong reason, and it means a DIRECT RFP could still be
skipped under "sales emails" on the model's reading. Fix 1 only protects against the broadcast rule.

## Website enquiries: facts before the model (11 Sep 2026)

Form bots hit snap-rewards.com with "I would like more information. Please contact me by email" and
"add me to the newsletter" under fake US names, free-mail or mail.ru addresses and 555 phone numbers.
`triage_inquiry` judged each alone under "generous on doubt", so 13 of Snap Rewards' 15 reply cards
went to robots, one was SENT (card 308, "consider yourself added") and then silence-chased three times,
and 10 bot addresses became CRM contacts. The same address was binned five times under five names and
passed twice under two more.

- **`engine.enquiry_hard_junk(slug, inq)`** runs in `intake_enquiry` BEFORE any model call: a fictional
  NNN-555-NNNN phone number, or an address this company has already filed as junk (free-mail after ONE
  prior verdict, corporate after two). **`remember_junk_sender`** writes every junk verdict, model or
  gate, into setting `enquiry_junk_senders:<slug>` (`{email: {count, last, names[], reason}}`, bounded
  at 3000). Un-bin an address by deleting its key. Seeded for Snap Rewards from the `enquiry_filtered`
  log plus the ten bot addresses (Rickson at NTUC Club, the one genuine lead, excluded).
- **Judgement stays on `lead-qualification` rules**, which is the skill triage reads. The newsletter
  rule the owner taught on 29 Aug sat on `sales-first-response` (the DRAFTING lane), so triage never saw
  it; it now also lives on Snap's lead-qualification beside a rule naming the "more information" template.
  A no-draft rule must be on the skill the gate actually reads.
- **The site has its own gate first** (snap-rewards theme 1.2.8, `wpcf7_spam`): honeypot, 555 numbers,
  bot phrases on short messages, Turnstile when its keys are set. Spam there never reaches Cortex.
- Cleanup 11 Sep: card 522 rejected, the vitaevans chase disarmed, the ten bot contacts marked
  `not_qualified` with a history note (kept, never deleted).

## The relationship decides the sender (8 Sep 2026)

Thread-stickiness answers "who last emailed this CONTACT". That is the wrong question when one person
works on two deals. Ayresh had emailed Shehryar Rahman about the ITC invoice, so the Dubai Police
variation chase was drafted as HER, and it read "Rashad is meeting Major Ibrahim at Dubai Police
headquarters on Thursday" off the deal note and wrote **"I am meeting Major Ibrahim"**.

**`_deal_sender(company_id, deal_id)` runs FIRST**, before anything looks at a mailbox. The deal's
explicit `crm_projects.owner` wins when it is one of the company's send identities; otherwise it is
derived deterministically from the deal's own SENT cards, the address that has actually sent on this
deal most often, most recent breaking a tie. No history means no opinion: it returns "" and the
thread-sticky and profile fallbacks stay in charge. Pinning the sender here also narrows adoption to
that person's mailbox, so the thread id comes from the mailbox that will actually send it.

Measured on the live deals: 73 -> rashad, 102 -> rashad, 107 -> gino, 114 -> rashad, 113 and 115 have
no sent history yet.

**WHO YOU ARE NOT.** `worker._colleagues_named` finds, by string test against the company profile's
signature roster, which OTHER people are actually named in this task's briefing material (brief,
system_note, deal_timeline, contact_notes, owner_feedback, meeting_notes) and the identity block names
them back: these are not you, what the context attributes to them stays theirs. The Manager gets the
same list as a system fact.

**BE HONEST ABOUT WHICH HALF IS WHICH.** The detection is deterministic and testable. The obedience is
not. "I am meeting Major Ibrahim" contains no name, so no output check can catch it the way
`_identity_mismatch` catches "Gino has kept me across" - that one is a string test against the roster
and either fires or does not. This is odds, and the guarantee remains what it always was: the sender
is decided once in code and persisted before drafting, and nothing sends without the owner approving.

**A TRAP, learned the hard way:** `_request_for_draft` calls `_draft_context_for_reply`, which re-runs
adoption and PERSISTS what it picks. Setting `request.thread` or `from_email` by hand and then
redrafting is silently undone. Pin the SENDER and let adoption choose within that mailbox, then read
the result back OUT OF THE DATABASE. Printing local variables verifies your intent, not the outcome
(card 502, twice).

## One contact, two projects: the deal picks the thread (7 Sep 2026)

Shehryar Rahman at EY runs BOTH the Dubai Police road-safety variation (deal 73, the 250k) and the ITC
proforma invoice (deal 58). `_adopt_existing_thread` took whichever thread was newest, so a chase about
the variation was drafted onto the ITC invoice thread, under that subject, from the colleague who had
sent it (card 502). Thread choice is now, in order:

1. a thread THIS DEAL has already been answered on: the threads of its own SENT (`status='done'`)
   cards. Rejected cards never went out, so they do not count.
2. a thread the counterpart has actually replied on.
3. the newest usable message.

Thread ids are MAILBOX-LOCAL, so (1) is applied again when choosing between mailboxes, where a newer
unrelated thread in another mailbox would otherwise win on date.

**The data fault underneath it:** the sent sweep resolves a deal from the RECIPIENT, so Ayresh's ITC
invoice email to Shehryar was filed on deal 73 (his only deal), subject line ignored. Those three
entries are voided on 73 and copied to 58, and reminders 93/94 repointed. When one person appears on
two deals, check where their correspondence actually landed.

## An email's identity is its Message-Id (7 Sep 2026)

`gmail_id` is a **per-mailbox filing number**. One email delivered to hello@, gino@ and ayresh@ carries
THREE of them, and a resend carries a fourth. Every "have we already handled this?" check keyed on it
therefore counted one message several times over. Use **`gmail.mail_ref(e)`**: the sender's RFC
`Message-Id` header, identical in every mailbox it lands in, falling back to the Gmail id only when a
message genuinely carries none.

**The proof.** Dubai Police sent the same tender circular three times in seven minutes (06:59:16,
07:02:08, 07:05:54, three distinct Message-Ids, one subject) to three monitored Sensa mailboxes. Eight
copies arrived. Measured against those eight real messages:

```
distinct Gmail ids     : 8   <- what the old key produced
distinct Message-Ids   : 3   <- what the timeline ref now produces
distinct notification keys : 1
```

**Where it now applies:**
- `pipeline.record_inbound` keys the deal timeline on it. Its "already logged from another mailbox"
  guard could never actually fire before, which is the ChainX triplicate.
- `pipeline.record_send` takes a `ref` and passes it to `log_deal`, which it never did at all: a sent
  email that cc's a colleague is met once per mailbox by the sent sweep.
- the inbound card dedup checks `request.mail_ref`; cards carry BOTH, since attachments still have to
  be fetched with the mailbox-local `gmail_id`.

**Notifications key on the THING, not the message.** `_flag_skipped_opportunity` keys on the
opportunity (sender, subject, day). Each copy's description rides as an `item`, so two genuinely
different tenders under one generic subject expand on the card instead of one hiding the other.
Note `notify` coalesces only into an UNREAD row: once the owner dismisses a card, the next copy makes
a fresh one. That is right for FYIs and worth remembering when judging a "duplicate".

**Cockpit:** grouped `items` are not always contacts. Only `contact`/`lead` cards get the "N new
contacts captured" headline; everything else keeps its own title and body.

**One email, one handling (14 Sep 2026).** The per-step guards above kept leaving gaps: the chase
reschedule in `_draft_direct_reply` ran before any of them, so HONOR's reply (three mailboxes) re-set
deal 119's clock and raised the same "Follow-up rescheduled" notice three times. `poll_inbox` now
CLAIMS each email by `mail_ref` before classifying it (`_claim_mail`, table `inbox_mail_claims`,
atomic insert-or-nothing, primary key company_id + mail_ref). Every other mailbox's copy is marked seen
and skipped, so classification, CRM, cadence, timeline, next step and the reply card all run once per
email. Only the claiming copy may retry; a failed card releases the claim (`_release_mail`). First copy
wins, which is what the card dedup already did, and the To-line sender routing still decides whose reply
it is. A NEW per-message step needs no guard of its own as long as it runs inside `poll_inbox`. The
manual `backfill_missed_client_drafts` bypasses the claim: it checks `mail_ref` on cards, and the
reschedule notice carries `dedup_key reschedule:<deal>:<mail_ref>`.

## The dumb waiter, enforced (7 Sep 2026)

**The skills decide, the code fetches.** Card 501 sent from `gino@sensa.digital` and wrote *"Gino has
kept me across the project"*, because `worker.py` carried a line from June 2026 saying "It is sent BY
the owner... Rashad IS the sender". `_identity_block` replaced that on 31 Aug but was inserted ABOVE
it, so every draft carried both instructions and the hardcoded one won on position. It had been
corrupting Gino's cards since: 358, 411, 455, 472 (all rejected), 494, 500, 501.

**ORDER IS AUTHORITY.** In `worker.draft` the code-written blocks now come FIRST and the editable
rules LAST, closed by `_PRECEDENCE`: the company voice and standing rules win over anything else in
the prompt, except five things they cannot override because they are computed facts or the output
contract, the sender, the code-stamped date, the real links and files, the ban on invented values,
and the shape of the output. Add anything new to the prompt ABOVE the rules, never below.

**Nothing in the drafting path names a person.** Who a message is from is `profile.resolve_identity`
off the company profile. If you ever need to write a name into a prompt string, that is the signal
you are about to make this mistake again.

**`_identity_mismatch` (engine.py) catches both directions**, deterministically, off the signature
roster: a draft claiming to BE a colleague ("Rashad here"), and a draft naming its OWN sender in the
third person ("Gino has kept me across"). It runs at DRAFT time and redraws once, on the first-draft
path and the correction path, with the approval gate keeping it as a backstop. The Manager also
carries the check (`manager.py`) but it is a model judgement: it passed 501, so it is a net, never
the guarantee.

**What moved out of code and onto skills, and where it now lives:**

| Was hardcoded in | Now a rule on |
|---|---|
| `_spawn_followup_card` payment chase tone (a 60k receivable) | `sales-followup` PAYMENT FOLLOW-UPS |
| `_spawn_followup_card` won-work check-in tone | `sales-followup` WON-WORK CHECK-INS |
| `_spawn_followup_card` opportunity chase goal | `sales-followup` OPPORTUNITY CHASES |
| `triage_inquiry` what counts as junk | `lead-qualification` JUNK vs GENUINE |
| worker media shelf: "at most 1-2" samples | `sales-first-response` sharing sample work |
| worker availability shelf: recipient's timezone | `sales-scheduling` |
| `draft_article` question-style H2s | `content-blog-posts` |

The revival branch of `_spawn_followup_card` always did this correctly ("the REVIVAL standing rules
on the sales-followup skill govern"), and is the pattern to copy.

**Model tiers are data.** The Fable 5 first-reply exception is the `first_reply_models` setting
(`{"sales-first-response": "claude-fable-5"}`), not a model id in code, so a tier is visible and
editable rather than a silent upgrade nobody can see.

**`triage_inquiry` refuses to guess a company.** It used to fall back to a literal "Tabscanner, a
receipt-OCR / data-extraction API" whenever the company lookup failed, so another brand's enquiries
were judged as Tabscanner's. An unresolved company now returns `unclear` and goes to a human.

**Still written in code, deliberately** (plumbing and safety invariants, leave them): the
code-stamped date, the Manager's computed 28-day calendar, the real-link allowlist, "never invent a
link, price, date or placeholder", the identity block, hiding cc directives from the drafter,
correction minimality, and the `related_skills` routing map (already DB-overridable).

**Known and NOT yet moved:** `engine._blog_digest_body` still writes a whole outbound email in
Python (the blog review digest to the test group). It is internal, to our own test group, so it was
left; if it ever becomes client-facing it must go through a skill first.

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

THE SENDER IS DECIDED ONCE, BEFORE THE DRAFT (1 Sep 2026). It used to be decided TWICE: the drafter
was briefed at draft time, and `_email_envelope` fell back to the profile's `reply_from` at SEND time.
With no `from_email` the identity block is empty, so the model chose a person for itself - card 451
opened "Rashad here, founder of Sensa" on an email addressed FROM gino@, and 19 of the last 58 email
cards carried no sender at all. `_draft_context_for_reply` now PINS and PERSISTS the fallback sender
right after thread adoption, so the drafter and the send path read the identical value. Spawned cards
(`_spawn_followup_card`, post-meeting, nurture) never set one, which is why revivals were the ones
that broke.
BACKSTOP: `_identity_mismatch` blocks approval of any draft that introduces itself as another member
of the team ("<Name> here", "this is <Name>", "I am <Name>"). Deterministic, read off the company's own
`signatures` roster, first-person self-introductions ONLY - naming a colleague normally is untouched,
and two colleagues sharing a first name is not a mismatch. Cards 444 and 451 were both caught live.

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
by name. **`draft_email` OBEYS THE ONE-OPEN-EMAIL-PER-PERSON RULE (4 Sep 2026).** It used to call
`store.create_task`, which bypasses the conveyor, so Talk built and SENT a second thank-you to Tikkie
while the first sat awaiting approval (cards 460 and 474). It now finds the open card, quotes its
opening line back to Rashad, and asks whether to correct that one or send a genuinely separate email;
only an explicit `separate_email: true` creates a second. **`draft_email` can also OFFER A BOOKED SLOT
(7 Sep 2026):** pass `meeting_start` (plus optional `meeting_minutes`) when Rashad names one specific
day and time. Code parses and range-checks the datetime, stamps `request.meeting`, and the existing
`_prebook_meeting` books a real attendee-less event with a Google Meet room BEFORE the worker writes,
so the email carries the genuine link instead of promising one separately. It reuses an event already
booked for the same person at the same slot rather than creating a second. The guest is added and
invited only on approval. Needed because `_maybe_extract_meeting` reads `email_reply` cards only, so
outbound drafts had no route to a link. NOTE the sender: an outbound card with no `from_email` falls
back to the company `reply_from` (Sensa = rashad@ since 18 Oct 2026; it was gino@), which is right for the sales inbox and WRONG for a
conversation Rashad started himself, so pass `from_email` when it is his (card 498). Catch-all mailboxes (INBOXES values) can NEVER be the From: `_draft_direct_reply` routes
catch-all-received replies via the company `reply_from` person, and `_email_envelope` hard-strips any
catch-all From as a backstop. **THREAD CHOICE: the conversation THEY answered beats the one WE started (7 Sep 2026).**
Adoption took the newest non-auto message in the mailbox, so a follow-up Cortex had itself opened on a
fresh thread became "the thread", and the next chase continued that one-sided thread instead of the
real exchange (Brent Woodhead: five messages under "New enquiry from Brent Woodhead - tabscanner.com",
card 490 pointed at a "your enquiry" we sent a month later). `_adopt_existing_thread` now prefers the
newest usable message whose thread the counterpart has actually replied on, falling back to the newest
usable message; and a `followup` card reads 15 messages back instead of 8, because on a chase the
recent history is mostly our own earlier chases.
Universal email-handling rule tells the drafter the library exists and
never to claim an attachment the tools didn't confirm.

## Opportunity follow-up automation (2026-08-25)

AUTO IS THE DEFAULT (owner, 31 Aug 2026). `crm.arm_new_deal()` runs on every creation path
(`create_project`, `create_deal`, `auto_opportunity`) and puts the deal straight onto the cadence.
Before this, automation defaulted to NULL/off and only the cockpit toggle ever armed it: 4 of
Tabscanner's 5 open opportunities were silently doing nothing. Never armed for Lost/Dormant/Nurture
(its own account-level loop)/Completed (silent by design). A deal with NO contact anywhere is left
off and raises ONE deduped "no contact" notification rather than nudging into a void every 3 days.
Existing pre-31-Aug deals were NOT retro-armed - that would fire a chase blast.

**NEVER CHASE A CLOSED DEAL OR AN UNANSWERED REPLY (14 Sep 2026).** Card 629 chased Yann Prudent (Codexa, deal
80) a fourth time, saying "they have not replied", three days after he had replied ("tested it, works really
well, will keep you in mind") and two days after the deal went to Lost. Three faults, three fixes:
- `crm.NO_CHASE_STAGES` = Lost and Completed. `set_project_stage` into either switches automation off and logs
  it on the timeline; `advance_followup` refuses a deal in either stage whatever its flag says. DORMANT IS NOT
  IN IT on purpose: nobody who approached us is dumped as dead, so a dormant deal keeps its soft revivals.
  Swept on the day: 80 and 104 (Property Finder) were Lost with chases on; both switched off.
- THEY SPOKE LAST: `_spawn_followup_card` reads the thread it quotes (`_deal_thread_msgs`) and
  `_they_spoke_last` returns their newest HUMAN message (auto-replies excluded) when it is newer than anything
  we sent them. "What we sent" is the thread's other messages PLUS the deal timeline's `email_out` /
  `email_out_manual` entries, because the mailbox the thread is read from may never see our replies (Sensa
  reads hello@, which is never copied). If they spoke last: no card, the step is not counted, the cadence
  pauses (`pause_followups`), their message goes on the timeline (`record_inbound`) and a high-priority
  notification quotes it. Tested live: Codexa caught; Sheraa 118 and Massar 117 (we replied last) not.
- rashad@tabscanner.com is now in `inbox_registry` (rt `gmail_send_refresh_token:tabscanner`), so Tabscanner
  replies to Rashad land on the deal and pause the cadence like Sensa's. Before, Tabscanner read api@ only for
  inbound and rashad@ for SENT mail only. Dry run first: 5 recent emails, all correctly ignored, no cards.
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
A LINK A PERSON GIVES IS REAL (owner, 14 Sep 2026: "any link provided through any card, on any company, and
through Talk, should be accepted, otherwise we can't give it the right feedback"). The guard trusted only
links already in the card's context, so a YouTube link typed into card feedback was called invented and
stripped on the redraft. Now every URL in a person's words is kept on the card as `request.owner_links`
(`engine._remember_owner_links`): `apply_correction` does it on the raw feedback before any channel rewrites
it (cockpit, Telegram, Talk `correct_task`), and Talk's `draft_email` / `create_task` / `draft` /
`correct_task` add the links from the person's last three messages (`_TURN_LINKS`, a context variable, never
a tool input). The guard and the Manager trust everything in the request, and the drafter is shown them as
"LINKS THE OWNER OR TEAM GAVE YOU". A link the MODEL invents is still stripped.
NOTHING IS "ATTACHED" UNLESS IT IS (owner, 14 Sep 2026: "you can never say you're attaching something without
a validation that the attachment is there"). Card 631, an automatic chase, told Ahmad at Brandgate "please find
attached our proposal and quotation" with nothing on it and was approved and sent: the drafter read the deal
note naming the files, and follow-up cards never attach. `engine._attachment_claim(draft, request)` finds OUR
claims to attach ("please find attached", "I've attached", "attached is", "we are attaching", "enclosed is"...,
never "the attached brief", which is their file) when the email carries no outgoing file (`attach_docs`, or
our own `attachments`; a client's inbound files never count). `_ensure_clean_email` redrafts once to remove
the claim, and `approve_task` BLOCKS any email that still makes one, naming the sentence. Code, not a rule:
the email-handling rule saying the same thing existed and was not enough.
THE THREAD DECIDES WHO A FOLLOW-UP GOES TO, AND THE GREETING MUST MATCH THE TO (14 Sep 2026). Card 651, an
automatic check-in on MAH Gold (deal 33), was addressed to Hussein (the deal's primary contact) but continued
"LBMA final revised", which is Gino's conversation with Mai (Hussein only in its copy line): the writer
followed the thread and wrote "Hello Mai" to Hussein, and the Manager passed it. Two parts decided "who"
separately. Now, in `_adopt_existing_thread`, a FOLLOW-UP is re-addressed to the person the adopted thread is
actually with (`_thread_counterpart`: the outside sender, else the outside address we wrote to), provided the
deal lists them (`_deal_contact`); if it does not, that thread is not continued. The card's `inquiry` and
`serialize_key` are persisted, and every shelf after adoption reads the new recipient. Backstop,
deterministic like the sender check: `_greeting_mismatch` flags an opening that greets a KNOWN other person
(on the thread, on the deal, or in the CRM at the recipient's domain); `_ensure_clean_email` redrafts once and
`approve_task` blocks. Unknown names (nicknames, spellings) never block. Replies to inbound mail are not
re-addressed: whoever wrote is who we answer.

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

**APPROVED FROM WHATSAPP (owner, 28 Sep 2026).** A wa_reply card is pushed to Rashad's own WhatsApp
(`whatsapp.alert_owner`) with Approve / Edit / Skip buttons. His replies are a CONTROL CHANNEL, never an
enquiry: `ingest_cloud` checks `is_owner(phone)` (last 9 digits) before anything else, so a button tap or a
voice note from him is never triaged, CRM-captured or answered.
- **Edit** accepts typed text OR a voice note (transcribed by `voice.transcribe`, the same Deepgram path as
  the cockpit mic, factored out of `api.py` so the two cannot drift). It is an INSTRUCTION, not verbatim
  text: it runs `engine.apply_correction`, which redrafts AND feeds the standing-rule inference, so teaching
  from the phone is the same act as teaching from the cockpit.
- **The biometric exemption is ONE KIND WIDE.** `engine.approve_wa_reply` refuses anything that is not a
  `wa_reply`. Every other outward kind still takes the step-up. Widening it has to be a deliberate edit
  there, never a side effect of reusing the entry point. WhatsApp's own sender verification is what stands
  in for the step-up.
- His number: setting `wa_owner_number` (falls back to `WHATSAPP_OWNER_NUMBER`), so it changes without a
  deploy. Out-of-window alerts fall back to the template named in setting `wa_alert_template` if one is set;
  with none, the alert fails soft and the Inbox card remains the record.
- **THE TRANSPORT IS CHOSEN, NOT DISCOVERED (6 Oct 2026).** Meta only accepts a free-form message to
  someone inside 24h of THEIR last message in. For a client that window is the point; for the OWNER it is a
  trap, because his own number goes quiet for days. The original code tried the buttoned alert and fell back
  to the template `except Exception` — but Meta **accepts** an out-of-window send (returns a `wamid`) and
  fails it asynchronously on a status webhook, so the exception never came, the fallback was unreachable and
  his alerts simply stopped arriving. Now `note_owner_inbound()` stamps setting `wa_owner_last_inbound`
  every time he writes in, `owner_window_open()` (24h less a 20-min margin) decides template-vs-buttons up
  front, and a `131047` failed status clears the stamp and re-rings the doorbell via `ring_waiting()`. The
  `except` fallback stays as a second line of defence. **If you ever add another owner-facing send, decide
  the transport from `owner_window_open()`; an exception is not a signal here.**
- **ONE ENQUIRY, ONE CARD (6 Oct 2026).** People type a WhatsApp enquiry the way they talk, so three lines
  in fifteen seconds became three cards, three drafts and three alerts (cards 1072-1074). A message from a
  phone that already has an unsent card (`_open_card`, within `BURST_WINDOW_SEC` = 15 min, statuses
  `_FOLDABLE`) is folded in by `_fold_in`: the messages accumulate in `request.messages`, `their_message`
  becomes the joined text, and the reply is REDRAFTED against the whole thing. A card in
  `awaiting_correction` is deliberately NOT foldable — it is mid-edit, and redrafting would throw his
  instruction away.
- **Alerts are ARMED, not fired.** Drafting stamps `request.alert_pending` + `alert_due`
  (now + `ALERT_QUIET_SEC` = 40s); `whatsapp.flush_alerts()`, ticked every 10s from the engine loop, sends
  once the burst has settled. The pending flag is cleared BEFORE the send, so a half-working send can never
  leave the loop ringing his phone. One reply from him pulls at most `ALERT_BATCH` = 3 waiting cards
  through, newest first, and says how many remain.
- **THE SAME TRAP ON THE CLIENT SIDE, AND IT CLOSED THE CARD (6 Oct 2026).** Card 1050 (a 2 Oct enquiry
  approved on the 6th) was marked `done` and delivered NOTHING: `send_text` succeeded, Meta failed it two
  seconds later with `131047`, and a lead we never answered read as answered. Now the window is tracked
  PER NUMBER (`note_inbound` on every inbound message -> setting `wa_in:<digits>`, full digits, never the
  last nine, which collides across countries), `window_open(phone)` is checked in `engine._execute`
  BEFORE sending (refused as `blocked`, card stays in the Inbox with `request.card_problem` saying why),
  and `reopen_undelivered` puts a `done`/`sending`/`queued` card back to `awaiting_approval` when a failed
  status arrives. `last_inbound_at` falls back to the newest card's `created_at`, so cards that predate
  this bookkeeping are judged on the message that created them rather than assumed dead. No margin on the
  client send (we would rather try with ten minutes left); the 20-minute margin is the owner's alerts only.
- **THE CARD SAYS SO BEFORE HE TAPS APPROVE.** `api._enrich_action_card` computes the window live for every
  `wa_reply` (never stored: a window closes by the clock, so a stored flag is wrong by lunchtime) and sets
  `card_problem` when it has gone, or "N minutes left to reply" under three hours. It also lifts
  `request.card_problem` onto the card, which the cockpit renders; before this, every `card_problem` the
  engine wrote into a request was invisible.
- **A FAILED SEND MUST NOT READ AS APPROVED.** The wa_reply branch returned a bare `{"ok": False}` on a
  send failure, but `approve_wa_reply` only treats `blocked`/`needs_confirm` as failure, so it answered
  "Sent." on a send that failed and bumped the skill's trust streak. Both failure paths now return
  `blocked`.
- **STILL MISSING: a client-facing re-engagement template.** `wa_alert_template` (`cortex_enquiry_alert`)
  is the internal doorbell to the owner's phone, wrong content for a client. Until Meta approves one, a
  lead who went quiet for more than 24h cannot be reopened on WhatsApp at all.
- **THE CARD IS THE CONVERSATION, IN BOTH PLACES (owner, 6 Oct 2026: "there's no history in the card...
  how can I approve anything without that information").** A `wa_reply` had no renderer of its own, so
  it fell through the generic card, whose body is `title + draft`: the Inbox showed a reply and no sign
  of what it answered, titled "Task #1096". `api._enrich_action_card` now serves a `wa` block (who, the
  number, every message in `request.messages`, triage category, PPC source, minutes of window left) and
  `waBlock` in the cockpit draws them as chat bubbles above the draft, with the card titled
  "WhatsApp reply to <who>". Bump `sw.js` when touching `web/index.html`.
- **AN OUT-OF-WINDOW ALERT CAN CARRY THE WHOLE CARD.** The bare doorbell template ("reply and Cortex will
  send you the draft") carries nothing, and since his own number goes quiet for days that became EVERY
  alert. A template's quick-reply buttons take a **per-message payload**, so `send_card_template` sends
  the enquiry, the draft and the same `wa:ok` / `wa:edit` / `wa:skip` ids a buttoned alert uses. Setting
  `wa_card_template` (`cortex_wa_card`) is preferred over `wa_alert_template`, which stays as the
  fallback for when no card template is approved.
  - **A TEMPLATE TAP ARRIVES IN A DIFFERENT SHAPE**: `button.payload`, not `interactive.button_reply.id`.
    `_owner_control` reads both. Reading only the second is why taps on a template would do nothing.
  - **A TEMPLATE PARAMETER MAY NOT CONTAIN A NEWLINE**, a tab or more than four consecutive spaces (the
    template's own body may). `_flat()` folds a draft's paragraphs to one line with "  /  " and clamps it.
  - **THE WABA ID ONLY EXISTS IN A WEBHOOK.** Template management needs the WhatsApp Business Account id
    and the Graph API will not give it up from a phone number id or a system-user token (every edge tried
    6 Oct 2026: `owned_whatsapp_business_accounts` wants business_management, `assigned_whatsapp_business_
    accounts` returns empty, `debug_token` shows unscoped grants). `entry.id` on any webhook IS it, so
    `ingest_cloud` stamps setting `wa_waba_id` the first time it sees one. `create_card_template()` /
    `template_status()` then work from the box; never click it together in Meta's UI.
- **THERE IS NO PROFILE PHOTO TO FETCH (7 Oct 2026).** Meta's Cloud API exposes a sender's `wa_id` and
  `profile.name` and NOTHING else; the only profile-picture endpoint is for our OWN business profile. A
  customer's avatar exists solely in the WhatsApp app and WhatsApp Web, so the only route to it is the
  runner, which is the banned path. Do not go looking for the endpoint again. "Do I know this person" is
  answered from our own records instead: `crm.who_is(phone, display_name)` returns the CRM contact matched
  by number (`find_by_phone`, which refuses an ambiguous last-9 key), their account, stage, tier, lead
  source, note, LinkedIn and location, their deals, and when we last emailed them. An unknown number
  returns `known=False` plus, ONLY for a distinctive full display name, up to three EXACT name matches
  labelled as possibles, never a first-name match (noise across 33k contacts) and never as the same
  person. `waWho` draws it above the draft: a green "you know them" panel with an Open contact button and
  clickable deal chips, or "not in the CRM on <number>" with the possibles.
- **REACHING A LEAD WHOSE WINDOW HAS SHUT (7 Oct 2026).** A landing-page enquiry we did not answer inside
  24h is unreachable by any free-form message, so the reply just sits there (cards 1050, 1053). Template
  `sensa_enquiry_reply_waiting` (setting `wa_reengage_template`, created by `create_reengage_template()`)
  needs no window: one quick-reply button, and the moment they tap it the window opens and the REAL
  drafted reply sends through the normal gate. **Deliberately a nudge, not the answer**: a template whose
  variable smuggles free-form content is what Meta has been clamping down on, and the conversation should
  happen in-window where it can be one. **APPROVED, NEVER AUTOMATIC** - it is an outward message to a
  client, so the card OFFERS it (`whatsapp.nudgeable` -> `wa.can_nudge` -> the Inbox button ->
  `POST /api/whatsapp/nudge/{task_id}`) and `whatsapp.nudge` refuses spam, an open window, a card with no
  number, and anything nudged inside `NUDGE_COOLOFF_DAYS` (14). The greeting uses their FIRST name only
  and only when it is a real name, because WhatsApp puts the number where a name goes and "Hi +971..." is
  worse than no greeting.
- Spam and other no-reply messages raise the FYI card only — they never interrupt him on WhatsApp.

**THE SHAPE CHANGED, 10 Oct 2026 (owner). CORTEX CAPTURES, RASHAD CONVERSES.** The links went back to his
personal number that morning because the API route was losing genuine enquiries (the 24h window, drafts that
talked about crew sizes and day rates). His call, same day: stop trying to hold the sales conversation on
WhatsApp and do the part a human is bad at. Both sites point at **+44 7772 480414** again (Sensa 0.8.65,
Sky Vision 0.9.6) and an enquiry now runs:
1. **Triage.** Spam is filtered COMPLETELY: no acknowledgement, no opportunity, nothing written to his phone.
   The FYI card stays, in Cortex only, so a wrong spam call is visible and reversible.
2. **A FIXED acknowledgement**, not a model draft (`whatsapp.auto_reply_text`). Its wording is the
   `AUTO REPLY:` rule on `social-dm-replies`; ONLY THE RULE'S FIRST LINE is sent (the first version sent the
   client the guidance note meant for whoever edits the rule next) and `{name}` / `{company}` are substituted
   by `.replace`, never `.format`, so a literal brace cannot raise. The message NAMES THE NUMBER he will reply
   from: they wrote to the business line, and a stranger on another number is otherwise a cold approach.
3. **CRM row** (`match_or_add_by_phone`, unchanged) and **an opportunity**, always, for anything not spam
   (owner: "if it's not spam, then it's an opportunity ... it won't get missed"; he deletes the duds). The deal
   carries the person by NAME AND PHONE, because there is no email on this lane, and is held off the chase
   clock (`crm.hold_auto`): an email cadence against a contact with no address nudges into a void.
4. **The alert** (`cortex_wa_card` template) carries the whole enquiry and the three buttons, in or out of
   his 24h window.
5. **On approval the acknowledgement sends, and ONLY THEN** `engine._push_wa_contact` writes the person into
   his personal Google Contacts (his sequence: "after I've sent the auto reply, that needs to be added to my
   contacts"). Google syncs it to his Android handset, the handset is where WhatsApp reads its contact list,
   so the stranger he is about to answer already has a name, a company and a note saying where they came from.
6. He answers from his own WhatsApp. Cortex never sees those messages, so the deal timeline stays as he leaves
   it; opportunity notes are his to add.

**GOOGLE CONTACTS (`contacts.py`, 10 Oct 2026).** Scope `auth/contacts` on the personal OAuth client
(`/oauth/google/start?purpose=contacts&company=personal` -> setting `contacts_refresh_token:personal`), People
API enabled on `cortex-personal`. **DEDUPE NEEDS OUR OWN LEDGER:** `people:searchContacts` does not index a
contact immediately after it is written (a contact created a second earlier came back not found) and needs a
WARMUP request before it returns anything at all, so `find_by_phone` checks setting `wa_contact:<digits>`
first and only then the warmed search. Without that a repeat enquirer collected a duplicate every time.
Contacts are named "<Name> (Sensa enquiry, 10 Oct)" so he can spot and prune them; a push name with no letters
(the number, an emoji handle) becomes "Unknown", never "+971 58". This is a convenience copy on his phone;
`crm_master` is still the record.

**BOTH SITES POINT AT THE SAME NUMBER, so the MESSAGE carries the brand (28 Sep 2026).** sensa.digital and
skyvision.film both link `wa.me/447772480414`. Each theme has a `wp_footer` hook (`sensa_wa_lead_source` /
`skyvision_wa_lead_source`) that appends a 5-character click reference to every WhatsApp link's pre-filled
text and beacons the Google Ads click detail to `/api/lp/click`; a link with no pre-filled text gets one
naming the brand. `lptrack.from_message` decodes it. The hook is guarded to run once per page: the two Sensa
landing pages' inline copies were REMOVED when it went site-wide, because two copies double-append the ref.

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

**A SEND ALWAYS REACHES ITS DEAL, AND A QUOTATION SETS ITS VALUE (owner, 12 Sep 2026).** The Massar and Sheraa
quotation emails (cards 586, 579) were drafted through Talk with NO deal on the card: `record_send` returned
at once and the sent sweep skips Cortex's own sends, so both fell through both nets; the deals showed no email,
Massar stayed at Opportunity, and both carried hand-typed values (80,000 / 42,000) with no history behind them.
Now: `draft_email` links the card to its deal (explicit `deal_id`, else the recipient's ONE active deal) and
scopes its library lookup to that deal; `record_send` resolves an unlinked card's deal from the recipient
the same way as a backstop; `record_quotation_sent` reads the quotation number (and version) off the attached
file name or the subject, sums that version's lines from the `quote_versions` registry (plus the agency fee
when the preset has one, never VAT), sets the deal's `value` while it is in FORECAST_STAGES and logs
"Quotation SEN-2026-0014 v2 sent: AED 74,850 + VAT"; the sent sweep passes attachment names so a quotation
sent BY HAND is valued too; `crm.update_deal` logs a hand-typed value as a `value_change` event. Deal value =
BEFORE VAT (owner). Both sends were backfilled through the same code.

**A CLOSED DEAL OWES NOTHING (owner, 12 Sep 2026).** Property Finder (deal 104) was marked Lost on 11 Sep and
a commitment reminder Cortex had set on 7 Sep still fired the next day: closing a deal stopped its chases but
cancelled none of its reminders, and firing never looked at the deal. Now `crm.set_project_stage` into
`CLOSED_STAGES` (Lost, Dormant, Completed; NOT Close & review, where delivery promises can still be owed)
cancels the deal's pending reminders whose `created_by` starts with `cortex` and logs a `reminders_cancelled`
event naming them; the owner's own reminders (created_by a person) are left alone. `reminders.fire_due` drops,
as cancelled, any Cortex reminder whose deal has closed since it was set.

**A SCHEDULED EMAIL SAYS WHAT IT IS (owner, 13 Sep 2026).** Approve-and-schedule left email cards untitled, so
the Calendar showed card 611 (the ChainX follow-up) as "email_draft · Sensa Productions email draft". Now
`api._email_summary` gives every email card on the Calendar its recipient, subject, sender, deal (a chip that
opens the deal) and the email itself behind "Show the email"; Talk's `list_calendar` describes scheduled emails
the same way; and `engine.approve_task(run_at=...)` titles an untitled email card "Email to <name>: <subject>"
when it schedules it, so the scheduled-send notice names it too.

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
  **A CARD THAT CANNOT DO THE WORK IS A REMINDER (owner, 14 Sep 2026).** Only a QUOTATION next step becomes
  an approvable card (marked `prep_action: quotation`; approving builds it). Every other next step (a
  proposal revision, a document, "fix the links in the PPT") is a high-priority REMINDER on the deal plus a
  notification, with no approve button, until a real tool exists for it. `_is_quotation_prep` now reads
  ONLY that marker: it used to match wording, so card 649 counted because it said the quotation "stands",
  and approving it would have built a new Honor quotation.
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
  ATTRIBUTION MUST BE PROVEN (6 Sep 2026): the notes-email sweep matches a Gemini notes email to a
  calendar entry by the MEETING TITLE in the subject (`Notes: "Call with Mai" Sep 1, 2026`), not by
  time proximity. A bare time window only decides it when the subject carries a precise clock time AND
  exactly one meeting sits in the +/-45min window; a date-only subject widens to the whole day and is
  refused unless the title matches. Unattributed notes are still STORED and searchable, get no deal, no
  commitments and no follow-up card, and raise one FYI notification. Why: Mai Almarri's MAH Gold notes
  (date-only subject) were attributed to the ECBD tender call that ran the same morning, putting three
  of her commitments and four timeline entries onto deal 100 as reminders 87/88/89 (cancelled 6 Sep;
  the deal 100 entries are marked `voided`, not deleted). A wrong client is worse than no client.
- **Voided timeline entries:** a `voided` key on a `crm_projects.history` event keeps it on the record
  (cockpit still shows it) but `pipeline.deal_context` no longer reads it back into drafts. That is how
  a misfiled event is corrected: nothing is ever deleted from a deal.
Lead -> opportunity conversion was already automatic on both intake lanes (qualify + auto_opportunity);
with won -> project and Close & review now wired, the lead -> opportunity -> project -> close chain is
closed end to end.

## Talk can now do the sales job end to end (2026-09-01)

Built so that asking Cortex produces what Claude would produce (owner rule: deliver THROUGH Cortex,
build the capability when it is missing).
- **`create_proposal` / `deck.py`** — the house proposal deck. The model writes the copy under the
  company's live `sales-quotation` rules; CODE stamps every fact: sample films from `media_assets` by
  category intersection and rating, original YouTube thumbnails trimmed to true 16:9, a generated cover
  for the client's world carrying no legible text, and prices read from the named quotation via the
  `quote_versions:<number>` registry so the deck can never contradict it. `engine.deliver_proposal`
  files to the document library + the client's Drive folder and raises a review card; it never sends.
- **`create_capabilities_deck`** (2026-09-07) - the CREDENTIALS leave-behind, the other half of
  `deck.py`. `create_proposal` answers one brief and carries a price; this one says what we can do and
  proves it with named case studies. Pages: cover, why it matters, the three studio layers, the seven
  module grid, one page per case study, how we would start. Each case study names its films BY VIDEO
  ID and `films_by_ids` resolves them from the library, DROPPING any we do not hold rather than
  substituting. Every statistic, award and project detail must arrive in `facts` - the writer may use
  nothing else, and the case-study heading never repeats the client name (it prints beside it).
  **Module pages (2026-09-10):** optional `modules` = [{key, title, image, focus, facts}] adds one
  full `photo` page per area of capability after the grid, written ONLY from that module's own
  published text (`facts`, e.g. its page on the company site) beside its own photograph (`image` =
  URL or path, fetched by code; `focus` left/center/right keeps an off-centre subject in the portrait
  crop). First use: the Sensa AI capabilities deck, seven modules from sensa.digital/ai-video-production.
  Owner steer on that deck: talk about what we can DO, not the software or the way we do it.
  Same day, three more owner asks, all built as tool inputs rather than hand edits: `page_images`
  ({opening/platform/grid: [{image, href, caption}]}, pictures on the text pages, by position, code
  fetched), `cover_subject`/`cover_palette` (his words beat the writer's cover choice), and `lead_film`
  ({video_id, title, kicker, caption}: ONE film full frame right after the cover, the whole page a link,
  resolved by `film_any` across every company because FilmSpoke's Farmer John led the Sensa deck). The
  four Farmer John channel films were added to `media_assets` under FilmSpoke (company 5, unrated) so
  the tool could resolve them. PDF links: a PDF cannot force a new tab; the viewer decides.
  Later the same day: `omit` (drop opening/platform/modules/case_studies/close; section numbers are
  restamped by `_renum` in page order, never trusted from the writer), the grid heading goes big when it
  opens the deck, and every build saves its spec beside the PDF (`.spec.json`) so `reuse_copy=true`
  re-lays the SAME words for a layout change instead of rewriting them. Final Sensa deck: cover, Farmer
  John, What we control, seven module pages, four case studies, close (15 pages, card 560).
- **The AI Production Capabilities Deck** (named 11 Sep 2026) is Sensa's STANDARD AI showcase: library doc
  327 (card 562, the approved 10 Sep build), renamed on Drive and in the library from "Brands agencies and
  government clients - Capabilities deck", kind `capabilities-deck`, profile `ai_capabilities_deck_doc`.
  `capabilities-deck` is a CORE kind, and core kinds now pass `documents.find`'s deal scope: that hard filter
  (which stops another client's file going out) also hid our own standing documents, so "attach the AI deck"
  on a deal-linked card came back empty. A rebuild makes a NEW file named from the audience; the standard
  only moves when Rashad says so (rename it, repoint the profile key).
  **A RENAME HAS TO REACH TALK'S OWN NOTES (11 Sep 2026).** After the rename Talk still called doc 327 by its
  old name, "Brands agencies and government clients - Capabilities deck - 2026-09-10.pdf": two of its taught
  notes (setting `chat_self_rules`, injected into every Talk system prompt as "THINGS RASHAD HAS TAUGHT YOU")
  were saved on 10 Sep with that file name, and the rename touched the library, Drive, the profile, the
  email-handling rule and the manifest, but not them. Seven earlier builds of the same deck (docs 320-326)
  also still sat in the library under the old name, so a search offered them beside 327 (card 581 attached
  one before it was corrected). Fixed: the two notes are one note pointing at `ai_capabilities_deck_doc`
  (old list kept as `chat_self_rules:backup-2026-09-11`); 320-326 carry `superseded_by = 327`, a column
  `documents.listing`/`find` now exclude (kept on record, `get()` still resolves them for old cards, and
  `sync_drive` never re-pulls a retired name). `documents.rename()` and the Talk tool `rename_document` rename
  the library row, the Drive file and any taught note using the old name in one step, and list back the
  skill rules that mention it for the owner (never auto-edited). Rename through it, never by hand.
- **THE DEAL SCOPE MATCHES WHOLE WORDS** (11 Sep 2026). `documents.find(scope=...)` is the hard filter that
  keeps another client's file off a deal-linked card, and it matched FRAGMENTS: measured across all 46 live
  deals it let 56 other-client files through and lost no genuine match by stopping (Sheraa's "ERF" inside
  "Perfumes", "graphic" inside a Property Finder "Graphics" quote on Merck, "itc" inside "Ritchie", "003"
  inside a Dawn Christine quote number, "brand" inside "Brandgate"). It now compares whole words from the
  file name, kind and recorded client. NUMBERS ARE NOT CLIENT WORDS: "2026" in one deal's title matched
  168 dated files of other clients, so all-digit tokens are dropped (no deal relies on one to identify it).
  Standing documents (CORE_KINDS) pass any scope. STILL OPEN: ordinary words in a deal's TITLE still act as
  client words ("agency" in Sheraa's title passes an Oliver Karstel Creative Agency quote; "awareness" and
  "campaign" pass the EY/ITC invoice onto other campaign deals; "shoot", "group", "day", "website" likewise).
  The real fix is to scope by the deal's CLIENT (account name, contact domain, the file's recorded client),
  not its title words; ranking usually puts the right file first, but the filter is meant to be a guarantee.
- **`rebrand_deck`** (2026-09-08) - re-lays an existing deck PDF from the document library into the
  house format, keeping the content WORD FOR WORD. `deck.render_spec()` has NO model in it: it lays out
  an explicit page list, because when the words are agreed (or already with the client) a model
  rewriting them is a defect. `deck.rebrand()` extracts the source text and its photographs, runs ONE
  model pass to map pages onto the house vocabulary under a transcribe-do-not-rewrite rule, and renders.
  Photographs are referenced by a code-issued "IMAGE n" label and resolved by code, so a page can never
  cite an image that is not there. Page types added for it: `covermeta` (cover with fact rows), `strip`
  (up to five parallel columns, since `cards` caps at three), `split` (argument beside a panel), `photo`
  (room beside its full-bleed photograph), `schedule` (counted deliverables beside the money), `qa`
  (numbered questions), `closing`. VERIFY A REBRAND BY DIFFING THE TEXT, not by reading it: strip
  punctuation and undo letter-spacing first, or the h3 kickers show as false losses.
- **`create_quotation` takes explicit `sections`** (2026-09-08) - a price per line, not only a `total`
  split by preset weights. A quotation that accompanies a proposal must carry the proposal's own
  figures; `deliver_quotation` always supported it and only the Talk tool was missing it.
- **`rate_card` / `set_rate`** — read the card (both tiers, and the OWNER TO CONFIRM gaps, which matter
  as much as the rates); record a rate Rashad STATES. Never invent one.
- **`media_library` / `rate_film`** — search the canonical portfolio; record his 1-10 rating. An UNRATED
  film ranks below every rated one, so a new upload is invisible to clients until he rates it (the
  sports showreel sat unpicked for exactly this reason until rated 8).
- **`research_client`** — the Fable 5 + web-search brief on demand, not only when an opportunity is
  auto-created.
- **`export_templates` / `_export_templates.py`** — re-export presets + rate card to the Drive terms
  folder. Cortex is the LIVE source, the folder is the human copy; run it after any preset/terms/rate
  change.

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

## Every deal has its organisation, and everyone on it matches (13 Sep 2026)

ECBD deal 100 had its contact (Sunwoo Yoo) and no organisation: only `auto_opportunity` ever filed a deal under
a client organisation, so deals made by hand, through Talk or by `_track_tender` had none. The deal page showed
people ONLY through the organisation, so it looked empty, and a colleague mailing from ecbd.gov.ae would not
have matched it. Seven open deals were like it (Sensa 88, 90, 92, 100, 101, 102; Sky Vision 87).
- `crm.ensure_deal_account(deal_id, email)` runs from `add_deal_contact` (so qualify, tenders and the cockpit's
  add-contact all pass through it) and `create_project`. A deal with a contact and no organisation is filed
  under: the contact's own organisation, else one already on that email domain (by `crm_accounts.domain`, then
  by a colleague already filed there), else a new one named from the contact's company or the domain. The
  contact is filed under it too. Free-mail and our own domains are left alone; an organisation already set is
  never replaced; an `organisation_linked` event lands on the timeline. A domain-named organisation ("Ecbd")
  is a placeholder: rename it properly when you see one.
- `open_deal_for_email` / `active_deals_for_email` also match anyone in the deal's own `contacts` list
  (`_ON_DEAL_CONTACTS`), not only the primary `contact_email` and the account. That was the Sheraa gap (card
  568, Muhanad Aouameh as a secondary contact, fixed by hand on 11 Sep).
- Cockpit: the deal page has a "Contacts on this deal" section (the deal's own `contacts`, with Add contact)
  above the Organisation section, so a deal's people show whether or not an organisation is set.
- The seven were backfilled 13 Sep: ECBD as "Emirates Council for Balanced Development (ECBD)", Maison
  Pyramide onto its existing account 12307 (domain added), the rest named from the deal title.

## A quotation number is never issued twice (11 Sep 2026)

A QUOTATION NUMBER IS NEVER ISSUED TWICE. `quotation_seq` only moved when `_next_number` issued a number; a
number pinned by hand (SEN-2026-0011, cited by BioScience's proposal) never advanced it, so the counter sat
at 10 and handed Honor 0011. Honor's quote was then filed as v4 of BioScience's version history, the shared
box file `quotation-sensa-SEN-2026-0011.pdf` was overwritten under BioScience's cards, and Honor's new Drive
folder got an ALL VERSIONS workbook with BioScience's priced tabs. Caught before anything was sent; undone
(registry restored to v1-v3, box files restored from library doc 352 and Drive, the three Honor files
binned, library doc 372 renamed VOID, card 570 cancelled) and reissued under a fresh number. Now:
`_next_number` skips any number with a version history or a quotation card; `_claim_number` moves the
counter past any pinned number; `generate_xlsx` refuses a pinned number whose history belongs to another
client; and "one open card per quotation number" only ever closes the SAME client's cards.

## Prep cards do the work (11 Sep 2026)

A "Prepare quotation" card (spawned by an owner correction's `prep` channel, or by
`pipeline.suggest_next_step` when a client asks for one; both set `request.prep_action = "quotation"`) is
ACTIONABLE. Answering it, in the cockpit or through Talk's `correct_task`, runs `engine._prep_build_quotation`
at the top of `apply_correction` instead of redrafting the checklist:
- `_prep_quote_spec`: the model STRUCTURES his words (all his notes on the card, oldest first, plus the deal
  timeline and rate card) into create_quotation arguments; CODE decides every price. A unit or total
  survives only if it is a figure he wrote on the card (`_stated_numbers`: 15,000 / 15k / 15 thousand) or a
  rate-card rate; anything else is blanked, and an unstated quantity falls back to 1. Preset, client and
  contact come from the opportunity.
- then `deliver_quotation`, the card linked to the deal, the prep card closed `done` pointing at the new
  quotation card, a note on the deal timeline. Nothing to build from -> the ordinary redraft runs.
Also: a prep card resolves its opportunity AT CREATION (`crm.active_deals_for_email` on the inquiry sender)
rather than only copying its parent's, and its title names it. Card 569 was made two minutes before its
parent 568 was linked to deal 118, so it asked for "the CRM deal number". Internal prep cards get
`worker._INTERNAL_PREP_RULE`: no sender, preparer or signature line (569 wrote "Preparer/approver: Gino
Palmes" on its own; no code put it there), and quotation preps end "Answer on this card with these details
and the quotation is built from them."

## Where Cortex is told how terms travel (11 Sep 2026)

WHERE CORTEX IS TOLD HOW TERMS TRAVEL - keep these four in step with each other and with the code:
- `sales-quotation` rule 1: the live preset map. `ai-production` (AI video, 70/30, AI short form),
  `shoot-production` (any live shoot, 50/25/25, Production Short Form Terms), `retainer`, `assignment`. No
  preset adds an agency fee (the old "film production + 15%" line was retired: no preset applied it).
- `sales-quotation` rule 8: the placement rule. Threshold = profile `high_value_threshold` on the NET value.
  Under it the preset's short form prints on the quotation; at or above it numbers and scope only, and the
  Master Terms go as a separate Terms and Conditions document, both on the card and in the client folder.
- `email-handling` "Sending a quotation": a quotation cover email is drafted on email-handling, which does
  NOT read sales-quotation rules (`related_skills`), so the attach-both rule has to live there.
- `create_quotation`'s tool description and the capability manifest: which preset carries which terms, and
  that the system places them. The Talk tool used to describe only `ai-production`, so a live-shoot quote
  asked for through Talk would have printed the AI short form.

## Shoot cancellation standard (owner decision, 11 Sep 2026)

**Within 4 working days of call time: 50% of the crew and production charges for that shoot. Within 48
hours: 100%.** Earlier than that only third-party costs already committed are charged, at cost, and a
postponed shoot is rebooked subject to availability. Cancelling and postponing are treated alike; working
days are Monday to Friday, excluding UAE public holidays. It replaced the 21 day / 7 day / 72 hour / 24 hour
ladder of 8 Sep, which Rashad judged unrealistic for Dubai, where shoots are often booked inside a week.

Where it lives: **Production Short Form Terms v1.0** clause 4 (24 clauses, locked 11 Sep, the printed short
form for live production, carried by the `shoot-production` preset), Master Terms clause 8.5 from v1.5, and
`sales-quotation` rule 6, which forbids quoting a different ladder without the owner. The previous
`shoot-production` printed terms are kept in `/opt/cortex-knowledge/documents/sensa/_preserved/`.

**The AI short form has NO shoot clause (owner, 11 Sep 2026).** An AI production has no shoot date or call
time, so the 72h/24h clause had nothing to attach to. Short Form Terms v1.3 (library docs 407/408) is v1.2
with clause 18a removed and the version line restamped; the `ai-production` preset's clause 19 now ends at
"non-refundable once work has begun". v1.2 is kept as the record. A quote that genuinely includes a shoot
takes the `shoot-production` preset and its own terms.

WHEN A PRESET'S TERMS CHANGE: regenerate any un-sent quotation built on it and run `export_templates` so
the Drive terms folder matches.

## High-value quotations issue under the Master Terms (11 Sep 2026)

- At or above the profile's `high_value_threshold` (Sensa: AED 250,000 ex VAT) `quotation.master_terms_ref`
  swaps the preset's printed terms for ONE incorporating paragraph: the sheet carries numbers and scope
  only. `engine._issue_master_terms_copy` issues the current Master Terms as their own PDF, built from the
  .docx beside it, as the client's copy (see THE CLIENT'S COPY below); filed in
  the client folder beside the quote and attached to the card with it. A replayed version keeps the terms
  it was issued with (explicit terms win), so an ALL VERSIONS tab never rewrites history.
- TWO PROFILE KEYS, TWO JOBS. `master_terms_doc` = the current Master Terms PDF (Sensa: v1.5 =
  docs 370/371 since 11 Sep; every earlier version kept). `high_value_attach_doc` = the COMPANY PROFILE that rides on a high-value first reply
  (Sensa: doc 7). It pointed at Master Terms v1.2 from 28 Aug to 11 Sep, so a high-value first reply
  would have attached the terms instead of the profile; it never fired. Never repoint it at terms.
- `create_quotation number=` issues a NEW VERSION of an existing quotation, same client only, so a
  proposal, its quotation and its terms carry one reference (BioScience: SEN-2026-0011 v2; v1 is an
  earlier AED 360,000 scope). Drive filing takes vN from the version registry, not the folder count.
- TERMS VERSIONING: a changed clause is a new version number, and a file named vX only ever contains vX.
  Rashad edited Master Terms v1.2 in place on 11 Sep (clause 3.6, per batch); that became v1.3 together
  with the 8.5 shoot ladder, the v1.2 file was restored (md5 8e1810d1), and the untouched original plus
  his raw edit are kept in `/opt/cortex-knowledge/documents/sensa/_preserved/`. Every version stays in the
  library and the Drive terms folder.
- THE CLIENT'S COPY SAYS "TERMS AND CONDITIONS" (owner, 11 Sep 2026). "Master Terms", the version line and
  the "applies at or above AED 250,000" note are our internal names for the template, so the issued copy is
  titled TERMS AND CONDITIONS with "Quotation <number as the quote prints it> · <client>" beneath, and the
  quotation's own line refers to "the Terms and Conditions supplied with it". The master version it came
  from is kept in the PDF metadata (Subject: "Issued from Sensa Master Terms v1.3") and on the card. A
  sales-quotation rule carries the same wording rule for notes and emails.
- THE EXECUTION PAGE lives IN THE TEMPLATE from Master Terms v1.4 (v1.3 + this page, wording unchanged):
  the signature section on its own page, Customer beside Sky Vision, a row per field (company name,
  authorising person, position, signature, date) with a line to write on and a box per company stamp;
  our side pre-filled, the customer side blank. `engine._terms_execution_page` FILLS it on the client's
  copy (the quotation number in the signing line, the client's company name) and only converts old
  run-on signature lines when a template has no table, reading the fields and our signatory from those
  lines. It never builds a second table: a copy showing two means the template changed shape.
- ONE OPEN CARD PER QUOTATION NUMBER: a new version closes the older open cards for that number as
  `cancelled` (never `rejected`, which reads as a judgement on the skill) with `superseded_by`. A
  re-render of the same version replaces its client-folder files in place instead of duplicating them.
- STILL OPEN: the Master Terms carry none of the shoot-specific protections the shoot-production preset
  printed (participant attendance, consent, manifest deadline, on-day sign-off, overtime, retention,
  participant data), and 6.3A licenses talent appearance for 12 months, GCC, online, which reads onto a
  client's own participants. Short Form Terms v1.2 still carries the 50% at 72 hours standard.

RATE CARD v1.7 (11 Sep 2026, LIVE: the owner accepted the component draft as it stood). The draft was built BY
CODE from 331 quotations he issued by hand (the SENSA CLIENTS quotation spreadsheets, final version of each quote
number, template tabs and Cortex-issued SEN- quotes excluded): 8,228 lines placed into 85 components by keyword
rules, normal = 2025-26 median, budget = 25th percentile, permits unrounded at cost, kit packages = each quote's
gear lines summed per day. 73 components were merged into v1.6 (106 items); where v1.6 already held an
owner-stated figure (voice over, music, stock, location, travel, actor, additional operator) the owner's figure
stayed and the component was skipped. Every item now has a stable `key` (shown as [key] in the Talk and drafting
renders). The Shoot packages and Drone packages groups are `reference_only` (never price a line, and excluded
from `ratecard.rates()`), because they sat below their own parts (full crew day 45,000 live vs 50,050 built).
Permits are a group with `at_cost`. v1.6 is kept verbatim as setting `rate_card:sensa:v1.6`; the draft workbook
and Rate Card v1.6.xlsx are in the terms folder's OLD, `Sensa - Rate Card v1.7.xlsx` at the top.

## Quotations: blocks by default, every line priced by code (11 Sep 2026)

The owner wants quotes laid out like Honor SEN-2026-0012: blocks (pre-production, production per day, talent,
locations, post, then voice over / music / versioning as their own lines), each line ONE amount whose
description names what it includes, a full line-item breakdown only when he asks. The layout is RULES on Sensa's
`sales-quotation` skill ("QUOTATION SHAPE"; "SPEC IT, DO NOT INTERVIEW": build from the opportunity, list the
assumptions in the reply, never ask first). Talk reads those rules because the `rate_card` tool now returns the
live sales-quotation rules under the card.

PRICING IS CODE, `ratecard.price_lines(slug, sections, allowed=, target=, flex_pct=)`:
- a line carries `components` ([{item: <rate-card key or description>, qty}]); code prices it as sum(rate x qty).
  One component keeps its own qty and rate on the line (6 x stock clip); several make one block amount. A
  missing, reference-only or unpriced component blanks the whole line and names it. `priced_from` is stored on
  the line in the version registry, so every figure is traceable to the card.
- a typed `unit` survives only if it is in `allowed`: the owner's own words plus `ratecard.rates()`.
- `target` (his number, ex VAT): the component lines move pro rata to reach it, only inside profile
  `quote_target_flex_pct` (Sensa 20); typed and at-cost lines never move; outside the band, or with any line
  blank, nothing moves and the quote is NOT built, the reply says by how much.
- Talk: `create_quotation` runs it before `deliver_quotation`. The owner's words come from `_TURN_SAID`, a
  context variable set by the chat executor from the user turns, never a tool input, so the model cannot supply
  "what he said". A `total` he did not write is dropped. Before this, Talk passed its own figures straight
  through (the prep-card guard only ever ran on prep cards).
- Prep cards: `_prep_quote_spec` asks for components too and runs the same function.

TALK FIXES FOUND BY THE FIRST RUN (Sheraa ERF, card 578):
- Talk had no tool to OPEN a deal. Asked for "opportunity 118" it searched contacts for "118" and asked the owner
  for scope the timeline already held. `deal_timeline` (deal_id or title words) returns the deal, its contacts,
  its quotations and `pipeline.deal_context(limit=40)`; every Talk voice has it (`_CHIEF_TOOLS` is gone, see
  "Talk knows the company").
- `provider.chat_tools` / `chat_tools_stream` run at max_tokens 1,500. A whole quotation as one tool call was
  cut at exactly 1,500 and the owner got an EMPTY reply. A round stopped by `max_tokens` is now re-run once at
  `_ROOMY` (16,000; a ceiling costs nothing unless used). And a turn that runs out of rounds mid-task makes one
  last call with `tool_choice: none` to say what was done and what is left, never "".
Result: SEN-2026-0013 v2, 12 block lines all priced from the card, 40,850 net (v1 was one 45,000 package line,
card 577, closed as superseded). Assumptions it listed: one shoot day, crew of four, travel to Sharjah, English
VO (Arabic offered in the note), six stock clips. The quotation header: the title is right-aligned in the
black band with the logo on its left, so a long title now wraps onto two balanced lines and shrinks to fit
(`generate_xlsx`); at 51 characters it used to run under the logo.

AI PRODUCTION PRICING is a rule on Sensa `sales-quotation` (owner, 12 Sep 2026, first applied SEN-2026-0014
Massar, 74,850 net, his chosen opening price): per film, the per-finished-minute rate pro rata to the APPROVED
running time PLUS generation volume (key frames, AI video generation, upscaling); then motion graphics/edit/grade/
sound; an interface restyle line when client screens appear; one pre-production block per job; perpetual music
for government/semi-government; options in the note only, never in a line. The rate card item carries the
reasoning (the per-minute rate is the director's time, prompting and selection across takes; the generation lines
are the takes), so it is never "corrected" as double counting.

CREATIVE PITCH PIPELINE (designed 12 Sep 2026, desktop-trialled on Massar, NOT yet built into Cortex): ideation
card -> script card -> style frames card -> pitch deck -> quotation, Fable 5 for every creative stage, Gemini
frames. The deck layout standard is LOCKED as rule "CREATIVE DECK STANDARD" on Sensa `sales-proposal` (full-bleed
page per beat, hero per film, contact sheet, imagery behind every text page, minimal words). Build notes and the
failures found on the trial (Fable output ceilings, the imagegen text prepass, the 8-image media cap, a logo in a
frame, an invented date) are in memory `project_cortex_creative_pipeline.md`.

RATE CARD v1.6 (11 Sep 2026, owner-stated while pricing Honor SEN-2026-0012): voiceover for a CAMPAIGN (one
hero film and its cut-downs, every version in that language) is AED 3,000 English (British) and AED 5,000
Arabic; the per-video voiceover rates (2,000 / 3,500) are for a single standalone video, and at per-video
rates a five-film bilingual campaign came to 27,500, which he called far too much. A shoot location is
AED 10,000 each (hire, filming permissions, preparation). v1.5 stays on file in the Drive terms folder.

## Pre-meeting briefs (`meetingprep.py`, 2026-08-31)

ONLY FIRST MEETINGS WITH NEW COMPANIES (owner, 1 Sep 2026). A brief is an INTRODUCTION aid; before a
routine call with a client of six months it is noise, and that noise is what made him dismiss cards on
reflex (which is how the ECBD meeting got cancelled). `_first_meeting` disqualifies a meeting if we
have already met any attendee (`meeting_notes`) or if their organisation has ever been a won deal. A
skipped meeting is still WRITTEN to `meeting_briefs` so the 10-minute sweep does not re-judge it.

A qualifying calendar entry with an attendee OUTSIDE our domains gets a one-page research brief 24h
before it starts. `calendar.upcoming_events()` reads the registry calendars for detail (the personal calendar is
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
WHO HAS LEFT IS ENFORCED AT THE ENVELOPE, not just at detection (1 Sep 2026). `lead_status =
'left-company'` is the flag, and `_email_envelope` drops any such address from cc on EVERY path. That
guard is the one that matters: thread continuity re-adds everyone on a conversation, so Tim Piper
would have gone straight back onto the next ITC and Contract Variation replies. A card addressed TO a
departed contact is BLOCKED at approve with a message naming them, never silently repointed. The
detector also marks the same person's OTHER addresses (same name, same account): Tim existed twice,
@parthenon.ey.com and @ae.ey.com, and only the replying address was flagged.
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

## A card stuck in 'sending' is resolved, not announced (3 Sep 2026)

`_send_email_reply` claims the card as `sending` BEFORE any side effect, so a crash or restart mid-send
strands it there. The startup recovery only raised a notification and left the status alone, and it ran
at STARTUP only, so card 437 sat in `sending` for two days: it never sent, never retried, and the
one-open-card-per-contact conveyor held the next email to that client behind it (card 454 was created
`queued` and would never have drafted).
`engine.sweep_stuck_sends()` runs from the loop and settles it ON EVIDENCE, the sending mailbox's own
Sent folder being the only thing that knows: found -> the card is `done`; not found -> back to
`awaiting_approval` so it is re-approvable; unreadable mailbox -> flagged as genuinely unknown. It
NEVER re-sends by itself - a duplicate to a client is worse than a delay. Notifications are deduped per
card (`stucksend:<id>`), so a stuck card cannot become recurring noise.

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
**BUILT 3 Oct 2026:** Cloud project `cortex-personal-510511` on rashadalsafar@gmail.com, External, **In
production**, web client "Cortex box" -> `/etc/cortex/google_oauth_client_personal.json` (root:cortex 640).
Branding home https://coretex.uk, privacy https://coretex.uk/privacy/ (`web/privacy/`, noindex), authorised
domain coretex.uk. Calendar, Gmail and Drive APIs enabled. Connected via
`/oauth/google/start?purpose=calendar&company=personal` -> `calendar_refresh_token:personal` (calendar +
drive.readonly); sees every calendar on the account (primary, Rashad's Work Calendar, Family, Tabscanner,
Sensa Main). Default `calendar_id:personal` = Rashad's Work Calendar (ietg2ucai1ssf01sbukqcuqcs0@group.calendar.google.com). Gmail (`gmail_refresh_token:personal`, gmail.modify) and Drive (`google_refresh_token:personal`, drive.file + drive.readonly) connected the same day; personal is NOT in inbox_registry, so no mail is triaged automatically.

## Golf tee booking (personal, skill `golf-booking`, 2026-09-13)

Books Rashad's Viya (Wasl golf) tee time the moment a day opens. **Method = phone automation**: the box,
as `cortex`, drives the real Play Store Viya on his S24 (Tailscale `rashads-s24` 100.65.62.23) with adb
over wireless debugging (`runtime/cortex/integrations/android.py`, elements found by visible text, never
coordinates). The API-interception route is parked: apk-mitm'd Viya still rejected the mitmproxy CA.

- **All behaviour lives in the skill**: the craft's single `PLAN = {...}` line (courses, fallback order,
  players, days ahead, release time, phone address). `golf.py` reads only that line; never hardcode.
- **Flow**: `python -m cortex.golf card YYYY-MM-DD` creates a `golf_booking` card (outward, NEVER_AUTO) ->
  approving sets it `armed` (engine `_execute`, nothing books; card 621 was first set `queued`, and
  `store.promote_queued` flipped key-less queued cards back to `new` for the worker to redraft. Fixed
  14 Sep: the conveyor now promotes only cards with a `serialize_key`, so runner-queued `social_action` /
  `wa_reply` cards stay `queued` until the runner takes them) -> a one-off `systemd-run --on-calendar`
  unit runs `python -m cortex.golf run <id>` a few minutes before release (the engine's 60s tick is too
  coarse) -> pre-check (adb reachable, unlocked, Viya in front; critical alert + retry) -> book via
  `viya_flow.book` -> task `done`/`failed` + critical notify. Screenshots in `/var/tmp/cortex-golf/<id>/`.
- adb pairing keys live in the cortex user's `~/.android`. After a phone reboot wireless debugging must be
  re-enabled and reconnected (`adb tcpip 5555` pins the port until the next reboot). Use Google's
  platform-tools adb at `/opt/platform-tools/adb`: the Ubuntu `adb` package cannot wireless-pair.
- **Viya screen facts** (1.2.25, mapped live; full detail in the skill craft and `viya_flow.py`): the form's
  selected day is the one CENTRED on the day strip (last two strip days are padding); the strip's range is
  fixed when the form opens, so a newly released day needs a form opened after release (`reopen_form`);
  day selection taps forward (+2 rightmost, +1 next) because swipes are unreliable. Weekend/function days
  show a "Peak Booking View" (players + preferred time, matched for you): skipped for the other course.
  Confirm = close keyboard (it covers Confirm Players) -> Confirm Players -> 57-second held summary ->
  terms switch -> Proceed -> "Booking Confirmed". **Money guard**: never Proceed if TOTAL > PLAN
  `max_total_aed` (0). Booking detail pages raise calendar-permission popups: answer "Don't allow" /
  "NOT NOW", never grant.
- **Speed**: the box is in Virginia and the phone in Dubai: ~0.4s per tap, ~2.7s per screen read
  (`uiautomator dump /dev/tty`). Measured end-to-end dry run (13 Sep): slot list ~85s after release,
  held summary ~125s, so a confirm lands ~2 min after midnight. The day strip swallows roughly every
  other tap after a change, so reaching a day 20 out takes several read-and-tap rounds. Moving adb to a
  Dubai machine or an on-device automation server is the next speed step.
- **Rehearse** with a fake Run (`dry_run=True`, `rel=now`, a >7-day weekday): `finalize` stops before
  Proceed. Guards that must stay: list date check, summary date+time must equal the chosen slot (a merged
  multi-page row list once landed on 08:30 instead of 07:40), TOTAL <= `max_total_aed`.
- **Test bookings** only more than 7 days ahead, on a weekday with no existing booking, cancelled straight
  after (the app blocks cancelling within ~3-4 days and then charges).

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
`cortex` DB, so it is in the nightly Drive dump like everything else. **STANDALONE since 14 Sep 2026:**
the PWA lives at **fitness.coretex.uk** (source still `web/fitness/` in this repo; served by the
`fitness-web` systemd unit, python http.server on 127.0.0.1:8090, tunnel public hostname -> 8090; ingress edited via API — the box CF token gained Cloudflare Tunnel Write on 14 Sep 2026, so tunnel hostnames need no dashboard).
It calls the API at coretex.uk by absolute URL (the CORS regex admits *.coretex.uk). Auth is a
ten-year owner token delivered ONCE via the setup link's `#k=` fragment and stored in the app's
localStorage (`fitness_device_key`) — no PIN, no cockpit bounce, no 14-day expiry. The owner ruled
security a non-issue here; rotate by minting a new token (patch `api.TOKEN_TTL`, `_make_token('owner')`)
and reopening a new setup link. WHY standalone: borrowing the cockpit's identity caused three real
faults in three weeks — the 14-day token expiry silently stopped sync, the sign-in pill dumped him
into the cockpit from inside the fitness app, and Chrome refused to install the nested-scope app at
coretex.uk/fitness while Cortex was installed at scope /. The old path still serves in a browser but
is NOT the install target. Anti-clobber (14 Sep): applyServerState MERGES by id and refuses to apply
while a local change is queued — a pull once erased a cardio session seconds after it was saved.

- Sync is whole-document (`POST/GET /api/fitness/state`): a few hundred rows, one device, so a full
  push/pull is easier to reason about than field-level merge. Upserts by the client's id, never
  hard-deletes; local deletions are sent as explicit tombstones. Every push is stored verbatim in
  `fitness.snapshots` — that is the restore path, do not prune it.
- localStorage stays the offline cache. The app saves locally FIRST and pushes after, so a gym with
  no signal works exactly as before.
- **A server-side edit to a row is overwritten on the phone's next open**: sync is push-then-pull and
  the push upserts every field the phone holds (`notes=excluded.notes` etc.). Only `deleted` sticks
  (OR'd). So to correct or annotate a row from the box: insert a copy under a NEW uid with the change,
  then set the old row `deleted=true`; the tombstone drops it on the phone and the copy arrives.
  Used 1 Oct 2026 (uids `<exercise>|<date>|r2`: squat 27 Sep, shoulder press 16 + 23 Sep, side
  raises 27 Sep) and to retire a duplicate 28 Sep 4x4 (`id_ou6cxnlg`).
- **FOOD + HEALTH CONNECT (4 Oct 2026).** Food tab in the PWA (`web/fitness/food.js`, loaded after the
  main script; the sw caches it as `food.js?v=N`, so bump the query with APP_VERSION + CACHE). Tables
  `fitness.foods` (per-100 g values) and `fitness.food_log` (each row stamped with the kcal/protein it
  worked out at, so editing a food never rewrites history) ride the normal whole-document sync, with
  tombstones; daily targets live in setting `fitness_targets` (default 2350 kcal / 178 g, edited in the
  app). Food lookup: `GET /api/fitness/food/search?q=` (USDA FoodData Central, key `USDA_API_KEY` in
  cortex.env, else the rate-limited DEMO_KEY; plus Open Food Facts **search-a-licious**
  search.openfoodfacts.org, because the legacy cgi/search.pl returns 503 to server traffic) and
  `/api/fitness/food/barcode/{code}` (OFF v2). Watch/phone data comes from the **Cortex Health Bridge**
  Android app (`android/health-bridge/`), which reads Health Connect and POSTs to
  `/api/fitness/health` -> `fitness.health_daily` / `health_sessions` / `health_weights` (server-owned,
  never pushed back by the PWA; returned in the state pull as `health`). The bridge authenticates with
  its OWN token (`api.bridge_token()`, signed under a separate scope, so `auth()` rejects it on every
  other endpoint); the copy baked into the APK at build time is `/etc/cortex/fitness_bridge_token`.
- **Watch workouts -> cardio log (5 Oct 2026):** `fitness.reconcile_watch()` runs after every Health Bridge
  POST (last 10 days) and was run once over all history. Owner rules: under 10 min is not a workout; a
  hand-logged entry is LINKED (`cardio_sessions.watch` jsonb), never overwritten; unlogged pre-go-live sessions
  become `w:<uid>` entries on the "... - from watch" presets with settings NOT recorded (never guessed); new ones
  are pre-filled at JGE from the last session on that preset and tagged unconfirmed until checked in the app; a
  10-20 min treadmill ending within 15 min of a 4x4 is its warm-up; title "Steam Room" -> preset `id_steam`.
  `watch` is pushed by the app with `coalesce` so an old client never erases it. Creatine: `fitness.supplements`.
- **A session still running is never filed (12 Oct 2026):** the Bridge sends an in-progress Samsung session with end = now, so the 07:00 and 07:30 syncs filed his warm-up at 10:19 and his 30-min 4x4 at 11:02 as plain treadmill entries (an 11-min session fails `is_4x4`). `reconcile_watch` now only files sessions that ended `SETTLE_MIN` (20) minutes ago, and `_refresh_grown` re-files any `w:` entry whose session has since GROWN, as a copy (`<uid>|r2`, preset/settings/notes kept, duration/HR/kcal from the watch) plus a tombstone, because the phone's push overwrites a server edit. It never touches an entry LONGER than the watch figure: that is his own correction (a first version reverted his 15:00 warm-up of 5 Oct to 12:08; restored).
- **Default machine settings (12 Oct 2026):** a watch-filed warm-up (`id_jge_z2_warmup`, fields Incline / Speed added) or 4x4 is prefilled from his LAST session on that preset, else `_DEFAULT_SETTINGS` (owner-stated: warm-up incline 2, speed 9; 4x4 incline 2, work 12.2, rest 6.5). A preset push with no fields never blanks fields the server holds.
- **MASS-DELETE INCIDENT (5 Oct 2026):** a device whose localStorage filled up (4,300 imported food rows) lost its
  foods + meals locally and its next push tombstoned all 242 foods and 13 meals (restored by flipping
  `deleted`). Now: the app never sends more than 5 tombstones per collection in one push, the server strips
  any push carrying >20 bare tombstones for a table, saves warn when storage is full, and the phone keeps only
  the last 120 days of the food log (server pull returns 120 days; full history stays in the DB).
  SAME DAY, separate bug: food-log tombstones ({id, deleted}, no date) were checked AFTER the date check in
  `push`, so every food-entry delete was silently dropped until fixed. Never treat ALL historical tombstones in
  `fitness.snapshots` as intent: the storage-loss push also sent thousands.
- **Burned (app, 5 Oct 2026):** BMR pro rata + Health Connect active kcal (on his S24 this is everyday
  movement ONLY, workouts excluded) + Samsung-SDK workout kcal minus the BMR share of each workout's minutes;
  steam room excluded. Lifting history shows that day's watch weights session above the lifts (date match).
- **Fasting, readings, CGM (5 Oct 2026):** `fitness.fast_days` (break-fast / window-close times) and
  `fitness.readings` (finger-prick ketones/glucose, mmol/L) ride the sync doc. FreeStyle Libre CGM: `cortex/libre.py`
  reads a LibreLinkUp FOLLOWER account (unofficial community API, region redirect to api-<region>.libreview.io,
  `version` header may need raising if login breaks); the owner typed the login into the app's Connect Libre form
  (setting `libre_follower`, never echoed). `cortex-libre.timer` polls every 5 min into `fitness.cgm` (server-owned,
  NOT in the sync doc; the pull returns the last 3 days).
- **MISSION START = 5 Oct 2026** (owner): the day he went strict, tracking everything, long term. Treat it as
  the baseline in every report (before vs after). Start 88.9 kg. GOAL: 79 kg at 12% fat by 31 Dec 2026;
  milestone sub-15% (~82 kg at 69.6 kg lean). Previous best 16.2%. App v43 mission strip (top of every screen)
  reads setting `fitness_targets.mission`; pep line is code-computed from his logs, never generated.
- **Progress photos + waist (8-10 Oct 2026, app v73/v79):** Weight page has a weekly waist card (readings kind `waist`, cm) and progress photos (`cortex/fitness_photos.py`, table `fitness.progress_photos`). Photos are PRIVATE: files in /opt/cortex-knowledge/fitness-photos (700, cortex), never in the repo or a public URL, served only via owner-auth `/api/fitness/photos/{uid}` and shown as blob URLs; the SW never caches /api/. Delete is soft (file kept). They ride the nightly private backup.
- **Glucose by meal (11 Oct 2026, app v91):** `cortex/glucose_meals.py` + `/api/fitness/glucose/meals`: meal = food within 30 min; baseline = median of 30 min before first bite; peak to +150 min or next meal (min 45); rise colours <30 g / 30-50 a / >50 r mg/dL; tags after alcohol (18 h), after training (3 h), breaks the fast; leaderboard = avg rise of meals containing a food (2+ meals, one vote per meal). Today's chart marks meals (tap: panel + Show in log). Food times reliable from 8 Oct 2026.
- **Power & Core plan + rep-range targets (11 Oct 2026, app v92):** plan `wk_power_core` (Tuesday afternoons; order and pairs A/B/C are in each exercise's `rest`). A plan exercise carrying `range:[lo,hi]` (+ `per`, `step`, `unit`, `wtLabel`, `cue`) gets its target BY CODE from the last session (`rangeTarget`): every set at hi -> level up (`step`) and back to lo, else +1 on the weakest set; the "next target" form is hidden for it. `rangeVerdict` colours each logged session vs the target it was set (teal beat/levelled up, amber matched/held, red missed). `getExerciseDef` prefers the selected workout's definition (Rope Facepulls is in two plans). Plan rows' `deleted` is OR'd on upsert, so a tombstoned plan uid can never come back: use a new uid (`wk_power` is a dead draft).
- **Grip test (11 Oct 2026, app v93):** plan `wk_grip` "Grip test" (old `wk_dynamome` tombstoned; exercise name "Dynamometer tests" unchanged so history follows): manual exercise flagged `grip:true`, 6 fields alternating Left/Right over 3 attempts (third all out); score = best per hand (`gripBest`), Grip history table with change vs previous test. MANUAL VALUES NEVER SYNCED before this: the app sends `manualData`, the server only read `readings`, so every manual entry stayed on the device. `fitness._manual()` now reads either, pull returns both, and the upsert coalesces so an empty push never blanks stored values. The four April 2026 grip sessions carry notes only: their numbers were never in this app. Sunday 05:30 reminder includes "grip test" (none logged in the last 6 days).
- **Workouts on the glucose chart (12 Oct 2026, app v94):** Today's CGM chart marks every session of 10+ min with a square at its start (teal; grey for steam/sauna), named from the linked cardio log entry (`watch.uid`). Tap: time, HR, glucose base (15 min before), peak and low up to 45 min after the end or until the next session starts (`workoutResponse`). Flags a likely sensor artefact for heat sessions or any move faster than 5 mg/dL a minute over 3+ minutes: steam room heat makes the Libre swing (12 Oct: 118 -> 64 -> 137 in 18 min).
- **NOW weight colour (12 Oct 2026, app v95):** the mission strip's NOW (7-day average) is judged against TODAY's point on the straight start-to-goal line (`ragPace`), both rounded to 1 decimal: on or under pace green, up to 0.2 kg over amber, more red. The weekly checkpoint bars still judge Sunday's average against Sunday's target (0.3 kg amber band).
- **Colours judge the number SHOWN (12 Oct 2026, app v96):** every RAG helper in food.js rounds its input to the displayed precision before comparing (integers for kcal/protein/steps/deficit/sleep score/glucose mg, 1 decimal for kg/cm and glucose in mmol/L with bands 5.0/7.3, whole minutes for fast/window). Protein 159.6 shown as 160/160 had read amber. Any new threshold must do the same.
- **Exercise glucose is green (13 Oct 2026, app v100):** `kpiGlucose(mg, at)` reads `glucoseContext(at)`: a reading during a 10+ min workout or up to 45 min after it is green whatever the value; 45-60 min after, amber only above 130; in or 15 min after steam/sauna it is grey (heat noise, checked first). Without `at` the plain bands apply (meal peaks keep them).
- **Sleep score bands (15 Oct 2026, app v102):** 80+ green (Eight Sleep's own green), 70-79 amber, under 70 red. Eight Sleep publishes a PROVISIONAL score while `raw.processing` is true (78 at 07:37, final 80 at 07:59); the 30-min poll replaces it.
- **Automatic fasting (15 Oct 2026, app v103):** from `AUTO_FAST_FROM` (2026-10-15) the eating window comes from the food log (`autoFasts`, run on every food-log save and render): first food with kcal > 0 breaks the fast, the last one closes the window once the day is over (04:00 next morning; a food before 04:00 belongs to the evening before, `eatDay`). Zero-kcal items never count. No Break fast / Close / Undo buttons; correct a time by editing the food. Earlier days keep his hand-set times; `settleEatenTimes` never touches automatic days. Emptied times are sent as `cleared` because the server coalesces nulls.
- **One-tap ketones (15 Oct 2026, app v104):** Today shows "+ Ketones" (on the Libre card, or its own small card): one number + optional note, saved to `readings` (kind ketones, mmol/L) at the current date and time, context from the fast state. The full finger-prick form still appears once a reading exists or the Fasting-page switch is on.
- **Photo items become foods (15 Oct 2026, app v105):** `foodPhotoAdd` also saves each ticked item to My foods (`source: 'photo'`, per-100 g worked out from the estimate, unit and usual amount `portion` = the photo's grams), or links an existing food of the same name; the log entry carries `foodId` + 1 x portion. `photoFoodsBackfill` (5 s after load) does the same for older photo entries. My foods tags them "photo estimate".
- **Sunday recap (18 Oct 2026, app v111):** once Sunday's weigh-in is logged, the mission strip's week line and pep line are REPLACED for the rest of Sunday (`weekRecap`/`recapLines` in food.js; gone at midnight): Smashed it (week avg 0.3+ under target) / Done it / Just short (up to 0.3 over) / Missed it, week avg vs target, deficit over the 7 days up to the weigh-in (last Sunday to Saturday, days with food + watch data), and next week's target. No separate box, no dismiss.
- **Edit a weigh-in (17 Oct 2026, app v106):** tap any weigh-in in the Weight page list to correct it (`foodEditWeight`); the entry card has a Date field beside the weight, and the picked day and typed weight survive redraws (`window._wtPick` / `_wtDraft`, app v107). Bodyweight rows are keyed by day and the phone pushes its copy on every sync, so a weight fixed on the server is overwritten by the phone: correct weights in the app.
- **Reminders (10 Oct 2026, app v86):** Fitness-branded web push, separate from the cockpit: `push_subscriptions.app='fitness'` (cockpit sends only to `app is null`), subscribed via `/api/fitness/push/subscribe` from the Today opt-in card; `cortex/fitness_remind.py` via `cortex-fitness-remind-{morning,evening}.timer` at 05:30 / 19:00 Asia/Dubai (OnCalendar with tz). Morning = weigh-in + creatine (+ waist + photo on Sundays), only what is not done; evening = creatine. Tab badges: Today (weigh-in, creatine), Weight (waist, photo when 7 days since last).
- **Household units (7 Oct 2026, app v56):** foods carry `units` jsonb [{label, g}] (USDA `foodMeasures` minus 'guideline'/'not specified' rows, the OFF pack serving, or his own via '+ Add my own unit'); food_log rows carry `unit_label`/`unit_count` and read '2 x egg (100 g)'. Grams stay the truth. USDA search is a POST with one retry (the GET form randomly 400s); key `USDA_API_KEY` in /etc/cortex/cortex.env. MFP imports of non-gram foods were bad ('Glass of 2% Milk' = per glass stored as per 100 g): replaced by USDA 'Milk, whole' with glass 152 g / half glass 76 g (his glass = 150 ml).
- **Remembered amounts + rename (13 Oct 2026, app v99):** the amount screen opens at the last logged entry for that food; when none is left in the log it uses the amount remembered ON THE FOOD (`servingG` + `servingLabel`, overwritten by every add, synced), labelled "Usual amount / Usual unit" in the food editor. Picking a search result that matches one of his saved foods (source id, barcode or name+brand) opens HIS food, not a fresh copy at 100 g. The amount screen has "Edit / rename" for saved foods.
- **Targets sync (6 Oct 2026, app v48):** the doc carries `targets` ONLY after an edit on that device (`fitness_targets_dirty_v1`, cleared after a successful push). Before, every sync pushed targets before pulling, so a device opened days later reset kcal/protein to its stale copy.
- **Bloods + bodyweight lifts (5 Oct 2026, app v45):** Food > Bloods reads server-owned `fitness.lab_results` (one
  row per marker per test date, flag computed in `cortex/labs.py` from the lab's own range) and `fitness.lab_wishlist`
  (next-test checklist; an item is done when a later result with the same marker name exists). NO health data in this
  repo: results load from a one-off import file (`python -m cortex.labs import <file>`, file deleted after) or from the
  app's "Add a lab report" upload (Opus transcribes, owner ticks rows, only then stored). Marker uid keeps "%" distinct
  ("Neutrophils %" vs "Neutrophils"). Pull-ups/chin-ups/dips: weight "BW" or "BW+10" (belt) resolves to the 7-DAY
  AVERAGE of weigh-ins up to the session date (app `bwAt` and server `bodyweight_at` mirror each other); the form
  turns a stale typed 50-130 number back into BW and shows "BW = x kg".
- **CGM via Juggluco (7 Oct 2026):** his UAE Libre 2 Plus is region-locked out of his UK-registered Abbott app (Play country UK), so the sensor runs in Juggluco (open-source, Play Store), which uploads Nightscout-format entries to `https://coretex.uk/api/ns` (`/api/v1/entries`, header `api-secret` = sha1 of `/etc/cortex/nightscout_secret`; GET read-back also needs it). `libre.ns_ingest` stores them in `fitness.cgm` (source `juggluco`, mg/dL -> mmol/L, direction -> trend 1-5). LibreLinkUp poller stays for a future Abbott-app sensor.
- **Sleep + recovery (5 Oct 2026, app v42, Bridge 1.3.0):** Food > Recovery page + sleep tile on Today's top card.
  Bridge 1.3.0 sends the main sleep session's stages, start/end, overnight HR avg/low (5-min), breathing, HRV,
  source (Eight Sleep mattress writes to Health Connect; he wears NO watch at night) and `day_resting_hr` (Samsung
  watch, lowest 30-min avg of 3+ samples 06:00-20:00 excluding workouts +10 min, since Bridge 1.3.1; 1.3.0's 10-min/5-sample rule never fired on his sparse watch sampling). Stored in new `fitness.health_daily` columns,
  COALESCED on upsert so an older bridge never blanks them. The sleep SCORE is not in Health Connect:
  `cortex/eightsleep.py` reads Eight Sleep's unofficial app API (pyEight method) with his login typed into the
  Recovery page's Connect form (setting `eightsleep_login`, never echoed) into `fitness.sleep_8`;
  `cortex-eightsleep.timer` every 30 min. The app's OAuth client id/secret live in
  `/etc/cortex/eightsleep_client.json` (kept out of git). A night belongs to the day it ENDED (wake day).
- **Reading the history:** Feb to ~9 Apr 2026 has 1-exercise rows dated the day after a session (Mon
  chest press, Thu shoulder press/curls): next-day corrections, not extra sessions. Training was ~2
  sessions/week throughout; count adjacent-day entries as one session. Most Feb-Mar cardio (weekly
  Monday 4x4) was never logged. Zone 2 distances after 22 Aug 2026 were guesses and were removed.
- **PR conventions are not decoration, do not "simplify" them:** lifting PR = volume load
  (total reps x kg); bodyweight lifts resolve against the weight logged for the SESSION DATE, so a
  weight change never rewrites old records; cardio PR = Pareto frontier of lowest avg HR vs hardest
  settings, within one preset only. `fitness.py` mirrors the app's maths — change both together.
  ZONE 2 presets (non-HIIT, name contains "Zone 2"; owner, 18 Oct 2026, app v108): a tie on avg HR goes to the
  LOWER MAX HR (it did not run away at the end), and the history/detail rows show avg HR on the right instead of max.
  4x4/HIIT unchanged. App-only: `fitness.py` has no cardio-PR function.
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

### Health Connect bridge (Android, `android/health-bridge/`, 4 Oct 2026)
- Kotlin app `uk.coretex.healthbridge` ("Cortex Health Bridge") on Rashad's S24: reads Samsung
  Health via Android Health Connect (READ only) and POSTs to `POST /api/fitness/health` with
  `Authorization: Bearer <token>`. Daily totals use HC aggregate APIs per local day (HC dedupes
  sources); sessions/weights carry HC `metadata.id`/timestamps. Missing data is null, never a default.
  15-minute WorkManager 7-day sync since 1.3.2 (ExistingPeriodicWorkPolicy.UPDATE; was hourly; work name still `health-bridge-hourly`). 1.3.2 also adds `SyncNowActivity` on `cortexbridge://sync` (no UI: syncs, toasts, finishes back to the caller), which the Fitness app's refresh button fires as `intent://sync#Intent;scheme=cortexbridge;package=uk.coretex.healthbridge;S.browser_fallback_url=...;end` then pulls (app v82). Older: hourly (needs READ_HEALTH_DATA_IN_BACKGROUND), sync on open, Backfill =
  2026-01-01..today in monthly chunks (older than 30 days needs READ_HEALTH_DATA_HISTORY).
- Built ON THE BOX, never on Windows: copy the folder to `/opt/health-bridge-build` (tar over ssh,
  excluding build/.gradle), then run `./build-on-box.sh` there. Toolchain: JDK 21 at
  `/usr/lib/jvm/java-21-openjdk-amd64` (system default java stays 25), SDK `/opt/android-sdk`
  (platforms 35+36, build-tools 35+36; AGP installed its own platform-tools under it, the separate
  `/opt/platform-tools` used by golf booking is untouched), AGP 8.11.1, Gradle 8.14.3 wrapper.
- Secrets live only on the box: signing keystore `/etc/cortex/health-bridge.keystore` + passwords in
  `/etc/cortex/health-bridge.keystore.properties` (root 600; created once, NEVER regenerate or app
  updates will not install over the old one); token `/etc/cortex/fitness_bridge_token` is baked into
  BuildConfig at build time (changing the token = rebuild + reinstall). Output:
  `/opt/health-bridge-build/out/cortex-health-bridge.apk`. Bump versionCode in `app/build.gradle.kts`
  on every release.
- Samsung Health Data SDK path (v1.1.0+, 5 Oct 2026): on the S24 Samsung Health shares only
  steps/distance/active kcal with HC, NOT workouts or heart rate, so `SamsungReader.kt` reads
  `DataTypes.EXERCISE` (+ `HEART_RATE` fallback) straight from Samsung Health and appends them to
  `sessions` as `shd:<uid>[:<idx>]`, source `com.sec.android.app.shealth`. AAR lives ONLY on the box at
  `/opt/samsung-health-sdk/samsung-health-data-api-1.1.0.aar`; its licence forbids redistribution, so
  NEVER commit it (`app/libs/*.aar` is gitignored; `build-on-box.sh` copies it in). Reads work without
  Samsung partner approval only while Samsung Health "Developer mode for data read" is ON (Samsung
  Health > Settings > About, tap version 10x > Developer mode). minSdk 29 (SDK requirement).
- Daily STEPS come from the Samsung SDK since 1.2.0 (`SamsungReader.readDailySteps`: `DataType.StepsType.TOTAL`
  aggregate, `LocalTimeGroup` DAILY x1, grouped per local day by the SDK) and override HC's `steps` per day; a day
  Samsung has no value (or 0) for keeps the HC number, a failed read falls back to HC with the error on the result
  line. WHY: Samsung shares only partial steps with HC (workout steps excluded, written late in batches): watch
  14,600 vs HC 6,060 on 5 Oct 2026. Needs the STEPS read permission (tap Connect Samsung Health again).
- 1.3.0 (vc 5, 5 Oct 2026) adds per-day `sleep_deep_min/rem_min/light_min/awake_min`, `sleep_start/end`, `sleep_hr_avg/min`,
  `sleep_resp_avg`, `sleep_hrv_ms`, `sleep_source` (HC, main = longest session ending that day; Eight Sleep) and `day_resting_hr`
  (Samsung HR 06:00-20:00 minus workouts+10 min, lowest 30-min mean of 3+ samples since 1.3.1, plus `day_hr_samples` / `day_hr_gap_s` diagnostics stored on health_daily); needs READ_RESPIRATORY_RATE (tap Grant permissions).

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

## Newsletter flow (13 Sep 2026)

- ANY request on a company's `content-newsletter` skill becomes a `newsletter_idea` card, whatever kind the
  caller passed (`store._coerce_kind`): Talk's create_task defaulted to `content`, so card 590 arrived as a
  full HTML email on a content card (the cockpit strips tags -> 42 blank lines) and was off the flow entirely.
- Stage 1 `newsletter_idea` = PLAIN TEXT only (`engine._run_newsletter_ideation` -> `newsletter.generate_idea`
  with the operator's brief; the brief's film, angle and facts are fixed, nothing invented on top). Approving
  it (count echo = test-group size + PIN) builds the HTML + plain-text twin, sends the `[TEST]` issue to the
  `newsletter_test_group` table ONLY, and drops the `newsletter_review` card whose "View issue" link renders
  the HTML in-app. Stage 2 approve banks the issue on the company's monthly slot (Sensa = the 5th); Stage 3 on
  the day sends to the live list, drip 250/hr, behind `newsletter_live_sends` (default OFF).
- Sensa test group (owner, 13 Sep 2026): rashadalsafar@gmail.com, dalalalsafar@gmail.com, gino@sensa.digital,
  ayresh@sensa.digital. The table is what the send reads; the skill rule mirrors it.
- OPEN: `newsletter.recipients()` matches `organisation ilike '%<company name>%'`, so Sensa's live audience is
  ~380 ("Sensa Productions") while ~13k contacts carry "Sensa" / "Sensa, Sky Vision". Decide before a real send.
  The cold-cohort warm-up drip is a skill rule only, not enforced in code. View counts are not code-stamped yet.
- BUILD CAN FAIL, A SEND CANNOT BE EMPTY (13 Sep 2026): compose for card 592 hit its 2,600-token output cap,
  the JSON never closed, `_loads` gave `{}` and a header-plus-footer email with the fallback subject went to
  the test group. Now: `provider.think_json` retries ONCE with 3x headroom when stop_reason is max_tokens;
  both compose paths run at 6,000 tokens; `newsletter._require_issue` raises `EmptyIssue` on no subject or
  no body; `execute_idea_approval` catches any build error, keeps the idea card approvable with
  `last_status`, alerts Telegram and returns the error to the cockpit toast. Nothing is sent on a failed build.
- FEATURED FILMS ARE DATA (13 Sep 2026): `newsletter.featured_films` pulls every YouTube id from the brief +
  idea, resolves it in `media_assets`, and the build renders the official YouTube cover art (`i.ytimg.com`
  maxres/sd/hq) as an inline card linked to the video after THE WORK section (both renderers + plain text).
  `check_links` fails the build on any URL that is not the company's own site (profile domains/live_site/send
  root), one of its own social pages, or a real film id (featured or in its library); coretex.uk is always
  blocked. Ideation must repeat every brief URL verbatim. Verified on card 592 -> review 596.
- SENSA NEVER DOES FREE (owner, 13 Sep 2026): no free sample/frame/trial in any newsletter; CTA = see the work
  or talk. Universal rule: film links are public YouTube URLs, never the internal media library.
- AUDIENCE IS DATA (13 Sep 2026): `company_profiles.data.newsletter_audience` = {org_labels, exclude_sources,
  exclude_instantly_campaigns, cold_sources, cold_cap, cold_cap_max}; absent = old behaviour (label = company
  name). `newsletter.audience(cid, task_id)` splits established / cold_batch / cold_waiting / excluded;
  `recipients(cid, task_id)` = established + cold batch, and EVERY count the owner confirms and every frozen
  job use it. Cold contacts graduate via `newsletter_deliveries` (written per drip batch); a finished job with
  new bounces under 3% doubles `nl_cold_cap:{cid}` up to cold_cap_max. Per-issue exclusions live on the built
  issue (`newsletter:{tid}.exclude` = labels/domains/emails): a case-study issue gets the featured film's
  `media_assets.client` automatically (`client_exclusions`), Talk `newsletter_exclude` / POST
  `/api/newsletter/{tid}/exclude` add more, `newsletter_audience` / GET `/audience` read it back.
  Sensa (set 13 Sep 2026): labels ["Sensa"], exclude "Instantly Super Search" + Instantly-campaign contacts,
  cold = the June email scrape, cap 500 -> 4000. Live: 7,764 total = 3,636 established + 4,128 cold.
- THE SUBSCRIBER FLAG IS THE LIST (owner, 13 Sep 2026): `newsletter_audience.mode = "subscribers"` on Sensa and
  Tabscanner: the list = org label + `newsletter_subscriber = 'True'` (TEXT column holding 'True'/'False'/null,
  compare as text). Flags set 13 Sep: Sensa 3,485 -> True, 6,703 -> False (bought prospects, Instantly-campaign,
  unreachable; NOT an opt-out); Tabscanner 13,117 -> True (everyone reachable; the June ~16k send was the test).
  Each changed row carries a `newsletter_list` history line. Adding someone to a newsletter = set the flag.
  Universal rule rewritten to match. Live: Sensa 7,759 (3,634 + 4,125 cold), Tabscanner 15,293. Sky Vision and
  FilmSpoke stay on mode "label" until the owner decides their lists. The flag is ONE column across organisations:
  a contact filed under both Sensa and Tabscanner shares it (per-business layer is the real fix, see memory).
- LIVE-SEND LOCK IS ON, STANDING (owner, 13 Sep 2026): `settings.newsletter_live_sends = true` stays on. The
  safeguards that remain are the ones that matter: a plain approve never sends, the owner types the exact
  recipient count + PIN at Stage 2 (schedule) and again at Stage 3 (send), the send drips at 250/hr with the
  8% bounce auto-pause, and `newsletter_paused` (POST /api/newsletter/pause) is the emergency stop and mid-send
  kill switch. The June rule "keep the lock OFF except for a deliberate real send" is retired.
- SEND NOW (13 Sep 2026): `engine.newsletter_send_now(tid)` (Talk `newsletter_send_now`, POST
  `/api/newsletter/{tid}/send-now`, owner only) turns a review/scheduled issue into the Stage-3 send card;
  the owner still types the count + PIN, then it drips. Sensa phasing is OFF (owner): `cold_sources = []`,
  the whole subscribed list every issue. Card 596 converted 13 Sep: 7,759 recipients, 5 HBMSU excluded.
- COCKPIT (13 Sep 2026): a `newsletter_review` card offers BOTH "Approve & schedule" (next monthly slot) and
  "Approve & send now" (`sendNowNL` -> POST send-now -> the normal count + PIN confirm). Queued newsletters on
  the Calendar get the same move up/down as blogs (the queue code was already generic, `contentqueue.KINDS`)
  plus a send-now icon. The "rolling-6 queue" in the June rule = the Calendar queue of `newsletter_scheduled`
  items, one per monthly slot, reorderable; there is no separate store and none is needed.
- THE STANDARD PAIR (owner, 13 Sep 2026): every outbound card offers BOTH "now" and "schedule". Email: Approve &
  send / Approve & schedule (datetime picker, run_at). Blog: Approve & schedule (monthly slot) / Approve & publish
  now (`blog_publish_now`: approve + PIN, then run_at = now, the clock publishes within a minute). Newsletter
  review: Approve & schedule / Approve & send now (`newsletter_send_now`). Newsletter send card: Approve & send
  now / Approve & schedule (`newsletter_schedule_later` -> back to a review card). Calendar rows: queued blogs
  and newsletters both have move up/down and a now icon. Cockpit `pairBtn(t)` is the one place the pair lives.
- MX ON EVERY news.* DOMAIN (13 Sep 2026): strict UAE corporate/government mail servers rejected the first Sensa
  send ("sender domain does not exist") because the Mailgun subdomains had no MX. Added MX 10 mxa.mailgun.org +
  mxb.mailgun.org: news.sensa.digital in GoDaddy (sensa.digital DNS is GoDaddy, no API), news.skyvision.film +
  news.filmspoke.ai via the Cortex Cloudflare token, all Mailgun-verified. news.tabscanner.com still needs it,
  in the OTHER Cloudflare account (owner). The box CF token is an ACCOUNT token: read it with grep+cut, never
  `source` the env; verify at /accounts/{id}/tokens/verify. See memory reference_dns_access_by_domain.
- CADENCE + STATS (owner, 13 Sep 2026): `company_profiles.data.publish_cadence = {"newsletter_scheduled": 14}`
  makes `contentqueue.next_slot`/`bump_to_front` stack every 14 days instead of monthly (Sensa: 5 Oct, 19 Oct,
  2 Nov ...; the skill rule says so). `newsletter.campaign_stats` / `stats_line` read Mailgun events per send
  (unique recipients: accepted/delivered/failed/opened/clicked/unsubscribed/complained + rates); Talk
  `newsletter_stats`, GET `/api/newsletter/stats?company=`, and the "fully sent" alert carries the line.
  Reminder 115 (22 Oct 2026): review cadence, move to weekly if the numbers hold.
- MONTHLY CAP + HISTORY (owner, 13 Sep 2026): `newsletter.monthly_usage()` / `check_monthly_cap(n)` (cap =
  `settings.mailgun_monthly_cap`, default 50,000/month across all companies; counts jobs created this UTC month
  plus in-flight remainder). Stage-3 confirm and the auto-send REFUSE a send that would break it; the confirm
  prompt shows the month line. Per-send stats are cached on `newsletter_send_jobs.stats` (`job_stats`, 10-min
  refresh while running, never decrements when Mailgun's event retention expires, frozen 14 days after
  `finished_at`). Calendar: "Sending now" and greyed "History" lanes, both click to a stats page
  (`GET /api/newsletter/job/{id}`, `/api/newsletter/history`), with View issue.
- HEADER IMAGE PRECEDENCE (owner, 13 Sep 2026): an image attached to the idea card (Talk attachment, data: URL
  in `request.attachments`) is the hero, full stop (`_upload_from_request`, kept on the issue as
  `hero_upload_b64` so corrections keep it); else the featured film's own YouTube frame when the writer sets
  `hero.source = "film"` (the schema tells it to when the brief/correction asks for a still, frame,
  screenshot or thumbnail); else the generated hero. Corrections on scheduled issues: `newsletter.correct_issue`
  then restore `status='scheduled'` + run_at (the engine only routes review/send cards).

## Blog refill = a pick-list MENU, not a nudge (owner, 13 Sep 2026)

- WHEN a company's blog queue drops to the refill point (`queue_refill_at`, default 3), `contentqueue.check_refills`
  no longer raises a "top up the queue" notification for blogs. It calls `engine.ensure_blog_menu(cid, need)`, which
  creates ONE `blog_menu` card (kind class internal, skill `content-blog-posts`) or, if one is already open for that
  company, re-surfaces it with a push. Newsletters keep the plain nudge until their menu is built.
- WHO: any company with WordPress connected (`wp.for_company`), so FilmSpoke (never published) gets a menu too. The
  old "has ever produced this kind" gate stays for the other kinds only.
- THE CARD: `blog.menu()` proposes N options (`queue_menu_size` setting, default 12; Talk `count`) spread across the
  MENU CATEGORIES, each a specific title plus a two-line angle, grouped under category headings. Categories are the
  universal rule on content-blog-posts that starts `MENU CATEGORIES:` (semicolon-separated, up to the first full stop;
  a company-local rule with the same prefix wins). `blog.menu_exclusions` feeds every blog card title the company has
  plus the site's live posts, so nothing repeats. Nothing is written or staged at this point.
- THE PICK: he replies with numbers. Cockpit = tick-list + "Build selected" (`POST /api/tasks/{id}/menu-pick`);
  typed/voice reply or Telegram reply = `apply_correction` -> `engine.menu_reply` (parses "1, 5 and 7", "number two");
  a bare numbers message on Telegram with nothing awaiting correction goes to the newest open menu; Talk = correct_task
  on the card. `_menu_build` creates one normal `blog` card per pick (count=1, brief = the concept, `menu_id`/`menu_n`
  on the request), closes the menu (status done, `request.picked` + `created`), and the existing flow takes over:
  full-text idea card -> Approve & build -> Approve & schedule -> publishes on the 1st. "more"/"different" regenerates
  the menu. Approve with nothing ticked returns blocked and the card stays open.
- NEVER a streak event: a menu pick or dismiss does not bump or reset `trust_streak` (`_approve`, `_skip`,
  `apply_correction` all special-case `blog_menu`). And the cockpit's "let the Manager auto-approve this lane" offer is
  now hidden on every blog-flow kind (`blog`, `blog_idea`, `blog_scheduled`, `blog_menu`), matching Telegram: the engine
  never auto-runs blogs, so the offer on card 618 would have set authority=auto for nothing.
- On demand: Talk `create_task kind=blog_menu` on content-blog-posts ("give me blog options for Sensa").
- First live run 13 Sep 2026: the three old refill notifications (Tabscanner 662, Sky Vision 636, FilmSpoke 457) were
  dismissed and `check_refills` fired menus 619 (Sky Vision, depth 3) and 620 (FilmSpoke, depth 0).
- SNAP REWARDS PUBLISH PATH (found 13 Sep, real cause found 19 Sep 2026): tasks 122, 124 and 125 (Jul/Aug/Sep) failed
  with 401 on every authenticated call (anonymous GETs work). The 13 Sep diagnosis ("app password rejected, issue a new
  one") was WRONG. The WordPress admin login was renamed `snaprewards` -> `snaprewardsprd` on 28 Jun 2026 (lost-password
  bot flood) and `SNAPREWARDS_WP_USER` in cortex.env still held the old name; the app password was valid all along
  (same password: old user 401, new user 200 administrator). Fixed 19 Sep: `SNAPREWARDS_WP_USER=snaprewardsprd`
  (backup `cortex.env.bak-2026-09-19-snapuser`). `config._env` is `lru_cache`d, so an env edit needs a restart of
  cortex-api + cortex-engine. LESSON: a 401 on a WP site = test `users/me` with the stored creds and check the
  USERNAME against `wp user list` before asking for a new password; renaming a WP login must update `<SLUG>_WP_USER`
  in the same session. `_run_blog_scheduled_task` now catches a publish error, writes
  `last_status = publish failed: <why>`, raises a high-priority Inbox notification and a Telegram line, and leaves the
  staged draft untouched, instead of a silent `failed` row.
- UNSUBSCRIBE MUST BE A REAL LINK (13 Sep 2026): Mailgun substitutes `%unsubscribe_url%` ONLY when the domain's
  unsubscribe tracking is ON. It was off on every news.* domain, so the first 1,900 HBMSU emails carried the
  literal token. Now: tracking ON on all five sending domains; `mailgun.send` auto-enables/refuses when the token
  is present and adds `List-Unsubscribe` + `List-Unsubscribe-Post: One-Click`; verified by reading a test email
  back through the Gmail API (rashad@sensa.digital). The stored copy in Mailgun is PRE-substitution, so never
  judge the link from `storage.url`. Inbox poll: a short "unsubscribe / remove me / stop sending" reply sets
  newsletter_opt_out deterministically (`_is_unsubscribe_request` / `_apply_unsubscribe`), no draft, Telegram line.
  `newsletter.require_unsubscribe` runs on every build (token in html + text, tracking on, else `NoUnsubscribe`);
  `_drain_one` pauses a job whose html lacks the token. Universal rule "UNSUBSCRIBE MUST WORK" on content-newsletter.
  Retro-checked 13 Sep 2026: all 5 queued Sensa issues + job 3 + all 5 domains pass.
- TEST COPIES GO THROUGH A CARD (owner, 13 Sep 2026): `engine.newsletter_test_card(source_task_id, emails)`
  (Talk `newsletter_test_send`, POST `/api/newsletter/{tid}/test-copy`) raises a `newsletter_test` card; approve
  -> type the recipient count -> PIN -> `newsletter.send_test_copy` sends the stored issue (card artifact, or the
  send job's copy after dispatch, via `issue_artifact`) as [TEST] to exactly those addresses. Never script a send.
- DRIP THREAD (14 Sep 2026): `engine._start_drip_thread` runs `drain_newsletter_sends` on a daemon thread with
  its own 60s clock and `_DRIP_LOCK` (non-blocking; a manual drain and the thread never double-send a batch).
  The main loop no longer calls it: a FilmSpoke blog build with repeated 4-minute model calls (plus engine
  restarts from another session) starved the HBMSU send for 25 minutes.
- TRACKED LINKS MUST BE HTTPS (14 Sep 2026, reported by a recipient whose browser blocked the http tracking
  link): Mailgun rewrites every link through `email.<domain>`; `web_scheme` was http on all five domains.
  Now `web_scheme=https` (PUT /v4/domains/{d}) and a Let's Encrypt cert per tracking host (POST
  /v2/x509/email.<d>, GET .../status = active) on all five. `mailgun.https_links_ready(domain)` gates
  `require_unsubscribe` at build and every drip batch (a batch waits, never sends http/uncertified links).
  Everything sent before 08:05 UTC on 14 Sep carries http tracking links: they still redirect, strict browsers
  warn. The Mailgun stored copy (`storage.url`) is PRE-rewrite: never judge links or the unsubscribe from it.

## Talk knows the company (14 Sep 2026)
Owner, after Talk said Sensa's Google review link wasn't in Cortex: it was on the profile (`review_link`) and in
both sales-followup rule sets. Talk only saw rules it chose to look up (`list_skills`, local rules only), could
not read the profile at all, and a Chief persona had a cut-down toolset with no hand-off to anyone who could act.
- ONE TOOLSET: `_CHIEF_TOOLS` is deleted. Every voice (Cortex, any Chief, any Manager) gets all of `SKILL_TOOLS`.
  A persona is a tone and a focus, never a limit (`personas.py` Chief prompt says so; no "hand it to the manager").
- KNOWLEDGE PACK: `api._chat_prepare` appends `_company_knowledge(co)` for the focus company on EVERY message: the
  profile (minus `_TALK_PROFILE_SKIP`: google_access, signatures, signature_html, brand, booking, voice) and every
  skill's effective rules (universal minus overrides, marked "(all companies)", then local; skills with none are
  left out). It rides in the prompt-cached system prompt. Sensa is ~134k chars / ~33k tokens; measured on the
  Convert Chief: first message $0.46 (cache write of ~73k), each later step $0.04. No company in focus ->
  `_NO_FOCUS_NOTE` (use company_profile + list_skills). Labelled as reference, not a style guide for chat.
- `company_profile` Talk tool reads any company's profile facts. `_talk_company(slug, u)` clamps to the user's
  scope: Gino/Ayresh load, read and `list_skills` only their own companies (list_skills was unscoped before).
- Shared rules added: check the loaded knowledge before answering (and admit it if you didn't); read a rule back
  (wording, company, skill) before add_rule / update_craft unless he dictated all three; never promise a hand-off.
- TRUSTED PROFILE LINKS: `profile.trusted_links(cid)` = review_link + live_site. The invented-link guard
  (`engine`, the `_URL_RX` check) and the Manager's REAL LINKS list include them, so a review ask carrying the
  review link is never redrafted or flagged as invented.

## Proposal from the deal record, revisable, quotation on approval (15 Sep 2026)
Owner's target flow: Cortex holds the whole opportunity; Gino asks Talk for a proposal; he revises it by
talking to the card; approving it issues the quotation. Before this, Talk saw only its own one-line brief (SEF'27's
brief PDF was never opened: the tender path reads 1,500 chars of the email), feedback on a proposal card only
rewrote its summary text, Talk could not read a PDF attached to a message, and approving a deck did nothing.
- DOCUMENTS ON A DEAL: `company_documents.deal_id` + `text` (extracted, `documents.extract_text`: pypdf for PDFs,
  doctext for office files; a word-per-line PDF is collapsed to prose). `documents.for_deal`, `documents.text_of`
  (cached on the row). `engine._file_deal_document(co, deal_id, ...)` = library row on the deal + the client's
  Drive folder (once) + a `document_filed` timeline event (ref-deduped). `_file_inbound_attachments` runs on every
  inbound route with a deal: `_draft_direct_reply` (incl. the auto-qualified deal), `_track_tender` (rt_key/client
  now passed through `_flag_skipped_opportunity`), `poll_sales_replies`. Images are never filed (context, not a
  document). `pipeline.deal_context` lists the deal's documents; `deal_timeline` therefore shows them.
- TALK READS FILES: `api._image_blocks(urls, names)` now emits PDF document blocks and office-file text blocks
  (was images only). `_said_text` keeps `_TURN_SAID` correct when a message carries blocks. Files attached in the
  turn ride into `create_proposal`, `create_quotation` and `save_document`; `save_document(deal_id=)` and
  `create_proposal(deal_id=)` file them on the deal first (`_file_turn_files_on_deal`). New tool `read_document`.
- THE DECK IS WRITTEN FROM THE DEAL: `deliver_proposal(deal_id=)` passes `_deal_facts` (timeline, limit 40 + every
  filed document's text, 40k budget) as `extra_facts`; the writer gets the REAL media-library slugs
  (`deck.library_slugs`; it asked for 'hero-film' and got no films) and `pick_samples` falls back to the
  best-rated films when slugs match nothing. Card request carries `proposal_brief, deck_spec, cover, version,
  customer, label, quotation_number`; the cover jpg is kept under the deck's own name. Verified on deal 123: the
  deck read 8,746 chars and carried the five-singer cast, two-location cap, 60s cut, influencers as a separate line.
- REVISE ON THE CARD: `apply_correction` -> `_revise_proposal`: `deck.revise_spec` (Fable, "change exactly what the
  feedback asks, keep the rest word for word"), `deck.render` (split out of `build`; reuses the cover unless the
  cover subject changed), next version filed over the last (`_file_proposal_pdf`: library on the deal, client
  folder, `archive_superseded("<Client> - Proposal")`, `documents.supersede`), same card, `attempts+1`, decision
  logged, rule inference runs. A failure is notified, never a worker redraft. Talk revises via `correct_task`.
- APPROVE ISSUES THE QUOTATION: `_execute` -> `_approve_proposal`: the deck's investment rows + `proposal_brief` +
  deal facts become the prep words for `_prep_quote_spec` (code prices from the rate card; owner-stated figures
  kept), `deliver_quotation` makes the quotation card on the deal, then the deck is rendered once more with
  `deck.investment_from_quotation` stamped on its investment page (v+1, "<Client> - Proposal <SEN-...> vN") and
  appended to the quotation card's `attach_docs`, so the email drafted from that card sends both. A deck built
  against an existing `quotation_number` just closes on approve. Talk rule added: build a deck/quotation only when
  asked by name ("draft an opportunity" produced card 679 for White & Co unasked).
- deck-spec / deck-revise run at 8,000 tokens (4,000 truncated and re-ran at 12,000 on the first SEF'27 build).

## Agency AI production: trade tier, preset, guide quotation, standard deck (15 Sep 2026)
An AGENCY buying AI video to resell (Promocell, card 684: per-video with/without VO, bundles of 5/10/15) was priced
from the direct-client card (one short AI piece 8,000; ten with English VO 100,000). Owner-set trade tier, rate card
group "Agency AI production (trade)", now rate card **v1.9** (v1.7 and v1.8 kept as `rate_card:sensa:v1.7/v1.8`):
single video up to 30s 4,500; batches 5/10/15 = 20,000/36,000/49,500; AI voice-over 500 per video; two social formats
per video included, each further format 400; two consolidated revision rounds PER BATCH, a further round 1,000.
Revision 1 (owner, same day): NO human voice-over ("we just don't do them, and we're pitching AI"); the deck carries
the prices, so no quotation goes with it unless the agency confirms a scope. Keys `agency-*`, priced by
`price_lines` like any component; `install_rate_card` makes any change a new card version.
- `runtime/cortex/agency.py`: `install_rate_card` / `install_preset` (idempotent), `guide_quotation`,
  `build_deck` / `deliver_deck`. Preset **`agency-ai`** = the ai-production terms and 70/30 payment with the agency
  title, lines, deliverables (supplied without Sensa branding, two rounds per video) and note (brief and approved
  script from the agency; concept/script writing, a consistent AI character, second-language captions, rush and
  broadcast/outdoor usage quoted separately).
- GUIDE QUOTATION: 10 videos + AI VO, reference `GUIDE-AGENCY` (never a SEN number), "Sensa - Agency Guide Quotation
  v1.0" .xlsx/.pdf at the top of the terms folder beside `Sensa - Quotation Template - agency-ai.docx`.
- STANDARD DECK "Sensa - Agency AI Production Rates v1.4.pdf" (doc 499; v1.4 only reorders the influencers page to
  Leo Iconik, Zayd Adventure, Leo Iconik; Talk's `chat_self_rules` note names it; first sent on card 684, Promocell,
  as a short cover note with the deck attached and no price list in the body). v1.3 structure, owner-reviewed: cover
  on the CFI commercial, what we make (4 pictured formats), CFI full page as "Full AI brand commercials", CGI social
  films (Zed, Huru, Nameless Ventures), AI influencers (Zayd Adventure, Leo Iconik x2), six brand and product films
  (MAH Gold, HBMSU, Al Rahba / Mercedes E-Class, Orientica Crown, Red Bull; `CreativeDeck.films_grid`), how it works,
  rates + add-ons, terms + next steps. Library kind **`agency-deck`**, official and a CORE kind (a deal's scope never
  hides it), Documents on Drive (older versions in Documents/Archive), profile `agency_rates_deck_doc` (doc 498).
  Every figure read from the rate card by key; its words are the setting `agency_deck_spec:sensa` (edit there, then
  `python -m cortex.agency deck`). A version FOR an agency (`agency_rates_deck(customer=, deal_id=)` or
  `python -m cortex.agency deck "<Agency>"`) is client work, filed on the deal and client folder.
- Talk tool `agency_rates_deck`; `create_quotation` lists `agency-ai`. Rules (Sensa): sales-quotation "AGENCY RATES";
  sales-first-response and email-handling "AGENCY PRICING ENQUIRIES" (give the indication they asked for and attach
  the deck; a call is offered, never a condition for pricing).

## Creative proposals: one run, revised by reply (15 Sep 2026)
Two proposal types now: the **Proposal** (`create_proposal`, words-led, above) and the **Creative proposal**
(`create_creative_proposal`, `runtime/cortex/creative.py`): when a brief asks us to develop the idea itself. Built so
Cortex does what was done BY HAND in session for SEF'27 / Sheraa (deal 123: SEN-2026-0015 v2 + proposal v4/v5, desktop
scripts), on the owner's call "one run with the ability to do revisions" (not a card per stage).
- STAGES (state saved to `/opt/coretex/creative/<job>/state.json` after each, so a failure RESUMES): brief (Sonnet,
  `_deal_facts` + his direction) -> location (`think_research` Fable 5 + web search, one pick against the constraint,
  facts with source URLs; `_commons_photos` = Wikimedia Commons free-licence photos WITH credit, the only photos a deck
  may show; `_official_refs` = the venue site's photos, PRIVATE references for the frames only) -> concept (Fable 5,
  3 routes, one chosen, look, verbatim asset descriptors, reads the reference photos) -> script (Fable 5, 7-11 beats,
  `_timing_problem` checks contiguity and running time, one retry) -> frames (`imagegen.generate`: Gemini WITH up to 3
  reference photos and NO text prepass; 2 per beat + 9:16 contributor tiles; Fable 5 cull, legible text/logo = hard
  reject; cap `creative_image_cap` on the profile, default 40) -> quote (Sonnet structures blocks from the scope, CODE
  prices with `price_lines`, typed figures allowed only from HIS words (`said` = `_TURN_SAID` + direction), exclusions
  listed; the deal's existing same-client quote number gets the next VERSION) -> copy (Fable 5, CREATIVE DECK STANDARD,
  `_drop_invented_dates`, no dashes) -> render (`deck.CreativeDeck`: hero, beat, sheet, tiles, with_bg; investment READ
  BACK from `quote_versions`; pypdf page count vs planned, one compact re-set, else flagged on the card) -> file
  (`_file_proposal_pdf`, earlier proposals on the deal retired, deck attached to the quotation card, timeline entry).
- CARD: kind `content`, `request.kind = 'creative_proposal'`, inserted `drafting` at once, the run on a thread in the
  process that started it (Talk = cortex-api); `awaiting_approval` with a summary (concept, routes passed over,
  location and why, quote, exclusions, assumptions, weak frames, overflow) when done; `awaiting_correction` on failure.
  REPLY = `creative.revise`: an unfinished run resumes; a finished one gets a Sonnet plan of what the words touch
  (location / concept / script / regenerate_beats / quote / copy), only those re-run, next version filed on the card.
  APPROVE = closes (the quotation was issued in the run). A deploy mid-run leaves the card `drafting`; reply to resume.
- COST (measured on the stages' logged equivalents): about USD 8-12 a run, USD 0.50-2 a revision.
- RULES added (Sensa): sales-proposal "CREATIVE PROPOSAL" (licensed photos only, venue photos are references, frames
  are illustrations, post-made crowds stated) and sales-quotation "EXCLUSIONS" (additional NOC fees, influencer costs,
  post-made crowds; permits as their own line).
- `deck.phases()` now shows 5 steps (was 4): SEF'27 v4's fifth step, post and delivery, was silently dropped.
- TEXT CLAMPS + ROW LABELS (23 Sep 2026, UAS deck SEN-2026-0026). Three defects found by reading the rendered
  PDF, all fixed in code: (1) `creative._label()` replaces `.capitalize()` on the investment row label. The
  quotation headers are ALL CAPS, so capitalize() was there to make them readable, but it also flattened every
  acronym: "AI PRODUCTION" printed "Ai production", "(GTMx)" printed "(gtmx)". It now works word by word, and a
  word the section's OWN item descriptions use is restored to THAT form, so the casing comes from the copy, not
  a hardcoded list. (2) the terms page's "Not included" card took `"; ".join(exc)[:260]`, a raw character slice
  that ended mid-word ("not included and pric"); it now takes WHOLE exclusions inside the budget. (3) `deck`'s
  `voscript` footnote clamp was a raw slice at 150 chars and ended the note mid-sentence although the line was
  two thirds across the page; it clamps on a word boundary with an ellipsis, at 240 (one full line at that
  width). LESSON: every clamp in the deck cuts on a boundary, never a character count, and READ the rendered PDF
  before it goes out.
- THE NARRATION IS NEVER TRUNCATED, THE TYPE IS (MENA Water deal 155, 12 Oct 2026). Clamping `voscript`'s two
  FOOTNOTES (23 Sep, above) was not the whole fix: the ROWS had no guard at all, so ten beats of a talky film
  overflowed the page onto a second, orphan page carrying no heading (23 pages rendered against 22 planned, and
  `stage_render`'s compact re-set could not recover it). The page exists so the client can approve the WORDS on
  their own, so clamping a narration line there would defeat the page. `voscript` now steps the row type down by
  how much narration there actually is (14px/1.4 under 900 characters, then 12.5, 11.5, 10.5 with the row padding
  shrinking with it). LESSON, and it generalises past this page: when a page overflows, ask first whether the
  content or the TYPE should give way, and only clamp content a reader is not being asked to approve.
- A CONTRADICTING CORRECTION REPLACES THE DIRECTION, IT NEVER EXTENDS IT (MENA Water deal 155, 12 Oct 2026).
  The owner changed his mind four times on one job (pure AI, then two routes, then one hybrid route, then a
  shorter film). Each change was APPENDED to `job.s["words"]`, so by the fourth the writer was reconciling 19,000
  characters of mutually contradictory instruction and kept reaching back into the stale layers: the deck cover
  still said "half a day" and "240 seconds" against a quotation for one day and two minutes, and one card still
  listed a four-person crew where the quotation bought a single operator. Re-prompting could not fix it, and two
  more copy runs were paid for before that was obvious. When a correction contradicts an earlier one, REPLACE
  `words` with a single clean brief; only append when it genuinely adds.
- A STALE FIGURE USUALLY LIVES IN DATA TOO. The same job's running time was not only in the prose: `concept.
  duration_s` was still 240 after `script.duration_s` had been set to 120, and the cover reads the concept. A
  re-prompt can never fix a number the code stamps. Grep the whole state for the old value, not just the
  direction.
- WHEN THE WORDING IS KNOWN EXACTLY, PATCH IT, DO NOT REGENERATE. Two stubborn phrases ("half-day", "route")
  were fixed by a deterministic pass over the copy dict, keys and values, longest match first, in one go at no
  model cost, after two regenerations had failed to shift them. `stage_render` alone then re-lays the deck.
- VERIFY A DRAFT WITH WORD BOUNDARIES, NEVER A SUBSTRING. Checking `"call" in draft` reported the missing call
  offer as present, because the email said "The film is called ONE DROP". The same pass also reported "seven
  working days" missing when the draft said "seven working day delivery". Both checks were wrong in opposite
  directions; use `` anchors and allow for a singular adjective.
- A LONG MULTI-POINT CORRECTION IS PARTLY EXECUTED. A six point correction on card 1163 produced an email
  covering the first three: no links, no drone paragraph, no call offer, and a vague timeline. `owner_links` was
  on the request and the manifest showed `availability(owner)`, so nothing had been stripped by a guard and
  nothing was missing from the context, the writer simply stopped. Correct in small batches and CHECK each point
  landed rather than reading the result approvingly.
- SOCIAL SERIES PROPOSALS: THE VERTICAL FRAME IS THE DELIVERABLE (Al Hamra deal 143, 12 Oct 2026). A brief
  whose output is 10 to 12 cinematic 9:16 reels could not be pitched: beat frames are 16:9 by construction, and
  the only vertical frame the pipeline made was the CONTRIBUTOR tile, hardcoded "phone selfie video, front
  camera, handheld", capped at 8, carrying no reference photographs, and described to the script writer as
  "tiles only if the concept has contributors filming themselves". Three state keys now steer it, defaults
  unchanged so every existing job behaves exactly as before:
  `tile_style: "cinematic"` swaps `TILE_SELFIE` for `TILE_CINEMATIC` (a film still on a cinema camera, real
  people mid-action, composed for a phone) and passes the concept's look through to the tile; `tile_cap: N`
  raises the count from 8; and a tile may carry `refs` into the location's reference photographs exactly as a
  beat does, so the reels are built from the client's real place. `_tile_schema` / `_tile_rule` swap the script
  prompt to match, asking for exactly N items, one per reel in release order, spread across the dayparts.
  `render` pages the tiles SIX to a page: the old `[:8]` here and `deck.tiles`' own `[:7]` silently dropped five
  reels of a twelve reel series.
- A CAST CHARACTER IS LOCKED BY A SHEET, NOT BY A PHOTOGRAPH (DSO deal 148, 13 Oct 2026). `people_refs`
  held one register only: a REAL person, from photographs their own company supplied. A drama has the same
  problem for invented characters, and worse, because nobody supplies photographs of someone who does not
  exist. The DSO concept one treatment showed it: the story is a scientist and his daughter, the daughter
  appeared in ONE frame of ten and the father read as a different man in several, because every beat was
  generated independently with no face to hold. `cast: True` on a `people_refs` entry switches
  `_likeness_prefix` to the character-sheet register (the attached image is the locked CHARACTER REFERENCE
  SHEET, the same performer plays them in every frame), so the workflow is: generate the sheet FIRST with
  `imagegen`, save it, register the character with `cast: True` and its path, then name that character in
  the beats and `_person_in_beat` binds it. Owner's own production protocol: nothing generates as video
  until the locked reference sheets exist.
- CLI: `python -m cortex.creative dry <deal_id> "<direction>"` (no card, no quote issued, no filing; output in the job
  dir) and `python -m cortex.creative resume <job> [task_id]`.

## Enquiry qualification: budget, dedup, instant webhook (16 Sep 2026)
dmg events' website enquiry (Rana Elbadaoui) got a reply card but no opportunity. `qualify_suggest` ran
`research_json` at max_tokens 900: two web searches plus thinking spent all 900 before a word of the answer was
written (stop_reason max_tokens, zero text), `_loads` gave {}, and "no verdict" silently meant no opportunity.
The site also POSTed the form twice (the research ran inside the request, 14s, so the form timed out and
retried) and the second post made a second card (694 + 695).
- `qualify_suggest`: research at 3,500 tokens; an empty research result falls back to a plain `think_json`
  judgement (900); a no-verdict is printed to the journal (`[qualify] no verdict for <email>`).
- `intake_enquiry`: ONE ENQUIRY, ONE CARD: the same sender with the same message (first 400 chars, whitespace
  normalised) within an hour returns `duplicate_of` the open card instead of a new one.
- `/api/intake/enquiry`: answers the site at once and runs `intake_enquiry` on a daemon thread.
- Re-ran the DMG enquiry through the fixed path: qualified, opportunity created, card 694 linked; 695 cancelled.

## Action reminders carry the deal (16 Sep 2026)
Reminder 149 "check in on the RFP" (deal 100, ECBD tender) fired as card 692 with only those words: no recipient,
no timeline, no deal, and the drafter refused. `reminders._with_deal(r, kind, req)` now runs in `_spawn_task`:
a reminder targeting a deal/project (or an action request carrying deal_id) gets `deal_id`, the deal's primary
contact as `inquiry` (email kinds only; name from crm_master), `thread_reply` + "Re: <last email subject on the
timeline>" (`_last_subject`), the brief prefixed "REMINDER ACTION on opportunity #N ..." + `pipeline.deal_context`,
a `system_note` naming the reminder, and the title "<reminder> (<deal title>)". Talk's `set_reminder` refuses an
email-kind action without target_type=deal + a numeric target_id. 692 cancelled; 149 re-spawned as card 696.

## A correction that quotes the draft is never "no reply needed" (16 Sep 2026)
Gino corrected card 701 with "no need  We will get this over to you shortly." (drop that sentence);
`_understand_correction` read "no need" as no_reply and dismissed the card, so Rana's email went unanswered.
The prompt now defines no_reply strictly, and `_quotes_draft(text, draft)` (a run of 5+ words from the
correction found in the draft) forces no_reply off and turns the words into a remove instruction. 701 was
restored and redrafted with the sentence removed (and the meeting notes, which had landed since).

## Named films reach a deck on every reply (17 Sep 2026)
`deck.films_in_text(company_id, text)` matches library ids, CLIENT names and titles against the owner's own words.
Used by `create_proposal` (his words this turn, `_TURN_SAID`) and by `_revise_proposal` (the reply on the card,
from the cockpit or Talk correct_task): a named film leads `samples.video_ids`; "drop / remove / take out <film>"
removes it. Talk's shared rules: search media_library first and pass `films`, never say a film can't be included.

## Sample films: relevance before rating, captions stamped by code (17 Sep 2026)
Rana's proposal (card 725) asked for "China Innovation Center" and got SEHA, HBMSU and Dubai Police captioned as
"multi-day event coverage". Three causes, all fixed: (1) `pick_samples` intersected the writer's four slugs, found
nothing, then widened to ANY slug ranked by rating alone, so the library's top 8s won every time; films are now
scored by how many asked categories they carry (first slug weighs most), then rating. (2) Captions were written by
the deck writer before any film was picked; `deck.film_caption` now stamps them from the film's own categories and
client. (3) Talk put the named film in the brief text instead of `films`; `create_proposal` now also matches
library CLIENT names against the owner's own words this turn (`_TURN_SAID`), so a film he names rides in
regardless. 725 re-rendered as v2 without a model call.

## Playlists everywhere; deck feedback on the quotation card (17 Sep 2026)
`media_playlists` (28 Sensa category playlists on YouTube) now reach every surface: `deck.playlist_for` /
`deck.playlists`; the Our work page links the lead category's playlist ("See the full Event coverage playlist on
YouTube", clickable, code-supplied); the drafters' media shelf lists every playlist so a reply can share a genre
and the invented-link guard accepts it; Talk's `media_library` returns the playlists for the categories searched.
A reply on a QUOTATION card that is about the deck (no price/total/AED/quote words, no 5-6 digit figure) revises
the deck of the proposal the quotation came from (`_revise_proposal(..., quote_card=)`): the proposal card keeps its
approved status, the new version replaces the old on the quotation card. Money words stay with the quotation.

## A client's stated timeframe moves every clock on the deal (17 Sep 2026)
BioScience wrote "postponed to March 2027": `_pause_or_reschedule_followups` moved the chase to 19 Apr 2027 but
the commitment reminder "schedule a call to walk through the proposal" still nudged on Saturday. Now, when a
timeframe is read from the client's words, Cortex's own pending reminders on that deal (`created_by like
'cortex%'`) that fall before the new date are moved to it (title suffixed with the client's quote), the move is
logged on the deal, and the notice names any OWNER-set reminders left for him to decide. Reminder 172 moved by hand.

## Approval answers the cockpit before it mirrors to Telegram (19 Sep 2026)
Card 769: the owner entered his PIN, the email sent in 4 s and the deal closed, but the cockpit hung until he
reloaded. After a send `_approve` edited the card's Telegram message INSIDE the request, and `telegram._call` had a
70 s timeout. Now `_mirror_approval` (the edit + the auto-lane offer) runs on a daemon thread after the result is
returned, and `_call` defaults to 12 s (the long poll passes its own timeout). No completion line in the access log
for an approve = a request that never returned; look there first.

## An email card can close its deal when it sends; Talk can cc named people (19 Sep 2026)
Pyxis decline (deal 128, card 769): `request.on_sent_stage` (+ `on_sent_note`) moves the deal to that stage in
`_send_email_reply` only after the send succeeds, so "decline, then close as Lost" is one approval and a blocked
or unsent email never closes anything. Talk's `draft_email` takes `cc` (emails the owner names) -> `cc_extra`.

## Our own email restarts the chase clock (17 Sep 2026)
Gino sent Honor (deal 119) a follow-up at 16:19 (card 740, offering Monday 21st) and the auto-chase fired at
19:11 the same day (card 747, "following up on my note from earlier this week"): a send only re-armed a PAUSED
clock (`resume_followups`), never moved a running one. `crm.touch_followups(deal_id, not_before=)` now runs from
`pipeline.record_send` for every outbound on a deal, Cortex-sent or found by the sent sweep: next chase = the
current step's full gap from now, and never before the day after the latest promise that email made (no chase
while we owe them the call or the document). 747 cancelled; 119 pushed to Tue 22 Sep 10:00.

## RFPs addressed to us are enquiries; promises are kept, not repeated (17 Sep 2026)
Emergy's explainer-video RFP came To hello@ with a named sender and was skipped as a "broadcast tender circular"
(the rule's wording escaped `_ONLY_WHEN_NOT_ADDRESSED`), so no reply, a manual tender deal and a wrong "no
contact" notice. Now: `policy._TENDER_SITUATION` drops every tender/circular/broadcast situation when our
address is on To/Cc; a genuine blast that asks us to confirm receipt/participation (`engine._ACK_ASK`) gets an
acknowledgement-only card (`_draft_direct_reply(ack_only=True)`); `crm.create_deal(arm=False)` from
`_track_tender` so a tender deal never raises the no-contact notice.
Antoni (121): "schedule a call" promised on 14 Sep was re-promised by the chase on the 17th and the due reminder
only nudged. Now: open commitments to the recipient ride into every email to them as `request.owed` ("keep them in
this email"); `pipeline.record_send` folds a repeated promise into the open reminder (`_same_commitment`, 60%
word overlap) instead of a twin; a due commitment reminder (`reminders._commitment_action`) spawns the email that
keeps it (sales-followup, deal-aware via `_with_deal`) unless it needs a person on site (`_NOT_BY_EMAIL`).

## A due follow-up is a draft, never a bare notice (24 Sep 2026)
Deal 125 (Asif Khan, opened from Talk with no contact and no account) fired "Follow-up due" notifications twice
and drafted nothing, although its own cover email had gone to asifkhan2030800@gmail.com. `_spawn_followup_card`
now resolves a missing contact from `_contact_from_deal_evidence` (the deal's own email cards, then the timeline's
email_out/email_in lines), attaches them as primary and drafts the chase. Only a deal with no evidence at all gets a
notice, and it says why and what to do (high priority, deduped per deal).
Guard added the same day: no chase to a contact who received any Cortex-sent email in the last 3 days (a manual re-run
drafted card 907 hours after chase 894 had gone).

## Everyone on a deal stays copied (24 Sep 2026)
Card 891 (UAS Meet link) dropped Mounir because the adopted thread message carried no cc; card 889 (ABN AMRO) could
not resolve "Hendrick" (Hendrik-Jan, card had no deal). Now `engine._deal_participants(company_id, deal_id, to)`
collects every external address from the deal's email cards (thread_cc, cc_extra, to_extra, recipients) and their
real send envelopes; stamped as `request.deal_cc` in `_draft_context_for_reply` (falls back to the recipient's one open
deal) and added to cc by `_email_envelope` (never_cc / cc_remove still apply). `_client_contact_named` forgives
spelling (shared first five letters) and also searches the recipient's own client account. UNIVERSAL rule
"EVERYONE ON A DEAL STAYS COPIED" on the three email skills.

## Nurture knows what just happened (1 Oct 2026)
Card 1020 (Brandgate) was a nurture touch drafted two days after their deal was marked Lost and the owner had met
them: nurture resumes the moment an account has no live deal, and its writer only got a one-line history per deal.
`nurture._account_state` reads the account's deal timelines. The sweep now holds a touch for `LOST_COOLING_DAYS` (60)
after a deal goes Lost and for `RECENT_CONTACT_DAYS` (21) after any email, meeting or owner note (Cortex's own
bookkeeping notes excluded, `_SYSTEM_NOTE`); a Haiku `nurture-gate` reads the newest thread + notes before drafting
(no -> pushed 14-90 days); the brief says 'past client' only when there is won work, lists the owner's notes as
fact, and states LOST WORK IS CLOSED (never revisit, re-quote or ask if it is still moving). A Lost DEAL is still
never chased; nurture is the separate relationship programme.
Same day: `_account_state` also returns `meetings` (the three newest `meeting_notes.summary` briefs on the account's
deals); the nurture writer gets them as fact and the pre-draft gate reads them. `_org_name` now returns the label deals
are stored under (`crm._org(slug)`): it returned companies.name, so the live-work guard never matched for Sensa.

## 70/30 shoot presets (29 Sep 2026)
`shoot-production-7030` and `shoot-production-perpetuity-7030` in `settings.quotation_presets` (`variant_of` the base):
identical shoot terms (cancellation/postponement clause kept) with payment_lines 70% on confirmation / 30% on approval.
Pro formas stage from the printed schedule. `_export_templates` now prints the payment schedule and the variant note,
and was re-run so the Drive terms folder matches. Never use ai-production on a filmed job to get 70/30.

## Feedback on a quotation card: one route, no contradiction, no new images (29 Sep 2026)
`engine._quote_card_feedback` runs first for any correction on a quotation card. Haiku only classifies (price / scope
/ wording); code acts. PRICE (a stated % or a stated target, the target turned into a discount % by code) ->
`reissue_quotation(discount_pct=)` + `restamp_proposal_price` (pricing page only). SCOPE -> creative:
`creative.revise(p, text, force_plan={quote, copy})` (new quotation version for the new scope + copy, never location/
concept/script/frames); words-led: `_revise_proposal` (text) and the live quotation card is CANCELLED so approving the
revised proposal issues the matching next version. WORDING -> creative copy-only revise / the words-led deck branch.
Both cards come back for approval. The cockpit quotation card now has Tap to talk / Type.

## WhatsApp lead source from the PPC landing pages (28 Sep 2026)
`lptrack.py`: the Sensa LP templates (page-lp-dubai.php, page-lp-ai-video-production.php) append '(ref XXXXX)' to the
wa.me pre-fill and, on tap, `navigator.sendBeacon` {ref, kw, mt, cid, agid, gclid, page} to PUBLIC `POST /api/lp/click`
(text/plain, no preflight; validated, 60/10min per IP; stored in setting `lp_refs`, capped 3000). The keyword etc. come
from the Google Ads campaigns' final URL suffix `kw={keyword}&mt={matchtype}&cid={campaignid}&agid={adgroupid}`.
`whatsapp._process_message` decodes the ref: CRM source, a FACT line in the brief (never mention the code), and
`lead_source` on the wa_reply card. Talk tool `lead_source(ref)`. Google gives the matched keyword per click, never
the exact search phrase (search terms are aggregate only).

## A meeting that resumes on the same link: both sessions' notes are combined (28 Sep 2026)
The Shama call broke off (Sarah absent) and everyone rejoined the same Meet later. Both sweeps filed ONE notes row per
meeting, so a second Gemini doc/email would have been dropped as a duplicate. Now `meetnotes`: the calendar path reads
every Gemini doc on the event (one row per doc, `file_id`); the email path records the Google Doc id from the email
(`_DOC_RX`); `_family` groups a meeting's rows across both paths (key, or same deal + day + title); `_already_have`
skips copies (same doc id, or >60% shared words); anything new is `_resumed_session`: its own row `<key>#sN`, a brief
distilled from BOTH sessions (the later one decides), `record_meeting` on the deal with commitments, a context note,
and the open post-meeting follow-up redrafted from the combined notes (or one drafted if none). Gemini must be
restarted ('Take notes for me') when the call is rejoined.

## In-perpetuity terms variants (28 Sep 2026)
Owner: keep a separate in-perpetuity version of the standard terms. `settings.quotation_presets` now also holds
`shoot-production-perpetuity` and `ai-production-perpetuity` (`variant_of` = the base): same terms plus a usage clause
granting a worldwide licence in perpetuity in all media (incl. material of the client's own staff and the AI-produced
material), and the music clause aligned. First use: UAS SEN-2026-0026 v7 (v6 was retired: its quotation NOTE still
said 'usage rights (standard): 12 months, GCC'; the note must be edited too). Standard presets unchanged.

## A price change re-stamps the pricing page only; prep cards never mint a new number (28 Sep 2026)
MBK Marine: a 15% discount asked through Talk started a NEW creative run (new frames, a quotation re-priced at the rate
card, SEN-2026-0028 v4 at 67,830) and an approved-as-is prep card built SEN-2026-0029. Now: `engine.restamp_proposal_price
(deal_id, number, quote_card)` runs whenever Talk's reissue_quotation / create_quotation or a prep build issues a
quotation on a deal whose proposal carries that number: creative decks -> `creative.adopt_quotation` (copy + render +
file from the live registry version, no concept/script/frames/quote stage); words-led -> re-render with
`deck.investment_from_quotation`. The proposal card returns for approval. Talk `create_creative_proposal` refuses a
second run on a deal that has one unless `fresh=true`. A quotation prep card closes itself when a quotation was issued on
its deal after it was made, and `_prep_build_quotation` versions the deal's number (`_deal_quote_number`).

## A chase is gated on the thread; a contact's reply finds its deal by thread (28 Sep 2026)
Card 964 asked MAH Gold how the final video was looking, two weeks after Mai signed it off. Two causes: MAH Gold has
four open deals, so her reply (filed only when `len(deals)==1`) went on none; and the follow-up clock drafts a
check-in without asking the thread whether one makes sense. Now `_deal_for_thread(e, deals)` files an inbound on the
deal whose cards carried that Gmail thread (else the one timeline with the same subject); and `_spawn_followup_card`
runs a Haiku `chase-gate` on the newest messages + `deal_context` before drafting: signed off / already covered / on
hold / waiting on their date -> no card, a note on the deal, the step not counted; a sign-off on won work also pauses
the cadence and tells the owner to move it to Final Payment or Completed. When unsure, it drafts as before.

## An inbound holds our scheduled email (25 Sep 2026)
A scheduled email fired as written even when the person had just written to us. Now `engine._hold_scheduled_emails`
(called from the direct-email poll beside `_pause_or_reschedule_followups`) pulls every email card scheduled to that
sender, or on one of their deals, back to awaiting_approval with `card_problem` 'Held, not sent: ...' (decision
'held', high-priority notice). The reply to their message is drafted with the held draft on a `waiting_email` shelf
(worker: fold in only what still applies), and once written, `_rejudge_pending_replies` judges the held draft against
it: covered -> cancelled, partly -> trimmed, else it stays for the owner.

## ONE proposal flow: approve the proposal, approve the quotation, the email follows (25 Sep 2026)
Owner: same process for creative and words-led proposals. Read the proposal, revise by reply, APPROVE it (words-led:
this issues the quotation and restamps the deck; creative: the quotation already exists). The quotation card carries
`request.proposal_card` and shows 'Approve quotation' (`web quotationCard`); APPROVE it. `engine._proposal_approved`
and `_approve_quotation_card` are the two halves of the gate (`request.approved` on each card): when both are approved,
in either order, `_draft_proposal_email` creates the email card with deck + quotation attached. No second approval on
the proposal card any more; a stand-alone quotation card (Talk/prep) still just closes. A block on the email step
now also raises a high-priority notification (both cards may already be closed).

## No duplicate replies: a firing promise reads the thread; a send re-judges pending drafts (24 Sep 2026)
Card 898: commitment reminder 248 (send updated proposals) fired after Shehryar had written they were already in, and
spawned an email repeating reply 895. Card 904: drafted from Farheen's message while our own answer (902) was still
unsent; 902 went, 904 stayed. Now `reminders._commitment_action` asks Haiku, from the newest thread messages, whether
the promise is already kept or overtaken; yes -> reminder done, `commitment_done` on the deal, no card, no nudge
(`{noop: True}` handled in `fire`). And `engine._rejudge_pending_replies` runs off-thread after every send: each open
reply card to that recipient made before the send is judged against the sent body; full -> cancelled with
`cancelled_reason` + a 'superseded' decision; part -> `apply_correction` trims it to what is left; none -> untouched.

## Opportunity context from feedback (24 Sep 2026)
Owner: facts he drops while correcting a draft should stay with the deal, not become rules. `_understand_correction`
has a `deal_context` channel (facts: roles, dates, budget signals, preferences, agreements; never a rule or wording);
`_apply_understood` files each on the deal's timeline as a `[context]` event (deal from the card, else the
recipient's one open deal; none = 'NOT noted' on the card). `pipeline.deal_context` lists every context entry in
full above the recent timeline, so it rides into every draft on that deal. Talk: `note_opportunity(deal_id|search,
note)`. The card shows 'noted on the opportunity: ...' under 'From your corrections'.

## Everyone on the email joins the invite (24 Sep 2026)
Card 904: Farheen asked to add Sarah Shaikh to the meeting invite; the reply promised it, but the send path only ever
invited the To recipient. Now, on send, every external address on the email's To and cc lines is merged onto the
booked event (`gcal.add_attendee`, never replacing), tracked in `meeting.guests`.

## Meeting stamps: honest, re-judged, never a reason to hold an email (24 Sep 2026)
Card 899 (SkyVision, Ausra): her "10 AM Friday" was read as agreeing to 10:30 (hour-only clock forms matched any
minute), the stamp stayed after the redraft became a question, and the send was blocked because SkyVision has no
calendar. Now `_client_agreed_slot` matches "10am" only for an on-the-hour slot; `_maybe_extract_meeting` re-judges
an unbooked stamp on every redraft and clears it when the draft only asks; at send, a company with no calendar
sends anyway and the owner gets a high-priority notice that the slot is not on any calendar (`_NoBooking`).

## A time the client proposes is accepted and booked (24 Sep 2026)
Card 890: Gheis wrote "Would 4:00 PM today work instead?"; the reply said "whenever suits you", and after "accept
the 4pm meeting, send a link, confirm" it said "we will send a Google Meet link" with nothing booked (the owner-words
test only knew 'book it / they confirmed'; 'today' was not a day word). Now: `engine._client_proposed_slot` runs in
`_draft_context_for_reply` BEFORE drafting (Haiku reads THEIR words, code checks `calendar.busy_blocks`); free -> 
`request.meeting` stamped and the writer told to accept and confirm (the booking + link follow via `_prebook_meeting`);
busy -> the writer says so and offers the nearest free times. `_OWNER_BOOKS` = accept/confirm/agree/book on the card
(not inside an 'ask whether...' phrase); 'today/tonight/this afternoon' count as day words; `_ASKS_TO_MEET` knows
'would X work' and 'instead'.

## Every email gets a final read-through (23 Sep 2026)
Card 876 reached the Inbox with "I would also be glad to send over our company profile", the closing line, then
"Please also see our company profile attached": each pass edits one thing (the attachment reconcile said "keep every
other sentence") and nothing re-read the whole. `engine._read_through(company, dreq, draft)` now runs at the end of
`_ensure_clean_email` (every draft, correction and reconcile path): Sonnet reads the finished email with the real
attachment list and fixes ONLY contradictions, duplicated points, sentences after the closing line, offers to send
an attached file, or claims about a file not attached; a rewrite (length off by >50%) is discarded. The reconcile
note now says: mention a newly attached file once, REMOVE any 'send it separately' sentence, nothing after the close.

## Rashad's email register (23 Sep 2026)
His voice profile (`company_profiles.data.voice.people.rashad.profile`, Sensa) was his PUBLIC persona (direct, blunt,
qualify hard, happy to say no) and it drove forceful client emails (card 876: 'two days is tight', 'let's lock in').
An EMAIL REGISTER paragraph is appended: courteous, warm, asks rather than tells, fits around the client, no comment
on deadlines, no conditions. Edit it on the profile, never in code.

## The client agrees a slot, not us (23 Sep 2026)
Owner, on card 876: "the client hasn't confirmed it yet, why are you booking?" and "let's lock in... is very rude."
`engine._client_agreed_slot(task, req, dt)` gates `_maybe_extract_meeting`: a meeting is stamped and pre-booked only
when the CLIENT's own message names that day and clock time (they proposed or accepted it) or the owner said
"book it" / "they confirmed" on the card (`_OWNER_BOOKS`, read from corrections incl. the live one). A time we
propose is a question: UNIVERSAL rule "PROPOSING A TIME IS A QUESTION" on sales-first-response, sales-followup and
email-handling (ask if it suits, offer to fit theirs, no link until they accept, never 'lock in' / 'I confirm').

## A slot confirmed by a correction is booked at once (23 Sep 2026)
"Arrange the call for 3pm tomorrow" on card 876 stamped `request.meeting` after the redraft but nothing was booked
(only the first-draft path pre-booked), so the body promised "we will send over the Google Meet link" with no link.
`apply_correction` now mirrors the first-draft path: `_maybe_extract_meeting` -> `_prebook_meeting` -> redraft once
with the real link when the body lacks it. The guest is still only invited on approval.

## Positive, ready, never a push-back (23 Sep 2026)
Card 876 (UAS brief, proposal due in two days) told the client "two days is tight", offered an outline first, and
made their script a condition. Owner: "we don't put negative vibes out to a client's inquiry." Fixed as a UNIVERSAL
rule (every company) on sales-first-response, sales-followup and email-handling: commit plainly to their date, never
call a deadline/budget tight, never renegotiate the ask, needs are asked as a help and we proceed regardless.

## Call slots: 3-hour notice, one free_slots (23 Sep 2026)
Card 876 (UAS asked for a call tomorrow) said "nothing free tomorrow" against a near-empty calendar: `calendar.py`
defined `free_slots` twice, the live one hardcoded an 18-hour minimum notice while the booking profile says 3 hours.
Now: the shadowed first definition is gone, `MIN_NOTICE_HOURS = 3` (a caller may pass `min_notice_hours`), and the
dead `engine._booking_slots_brief` (it called the deleted signature and always returned "") is removed.

## Our-work films: removal in any wording, library is the truth at render (23 Sep 2026)
Card 868: "I've removed that tag... put another one in there instead" named the Dubai Police delivery-rider film;
the drop test only knew `remove`, so the named film was pinned to LEAD the page. `_revise_proposal` now reads removal
intent in any tense (removed/dropped/took out/swap/replace/instead/wrong/mistake...), keeps `samples.dropped` and
`samples.named` on the spec (`_no_urls` keeps them); `deck.render` evicts a pinned film that no longer carries any of
the page's categories unless the owner named it, never re-picks a dropped one, and refills from `pick_samples`.

## Replies on a website-enquiry thread are read (21 Sep 2026)
`poll_inbox` queried `-subject:"New enquiry from"` to leave form notifications to `poll_inquiries`. That also
dropped every client REPLY on such a thread ("Re: New enquiry from Bohdan Bilych": SFORS pausing the hero video,
never reached deal 101). The query no longer excludes it; only a subject that STARTS with "New enquiry from" (the
notification itself) is skipped, in code. Same sweep found two literal backspace characters where `` was meant in
the reminder date-phrase regex ("31 August" never parsed): fixed. Patch scripts: never put `` in a heredoc.

## Call times only when a call is relevant (21 Sep 2026)
Owner: "I find myself constantly telling Cortex to stop offering dates for meetings." Cards 832 (Massar, decision on
hold to October) and 841 (Emergy, only confirming receipt) both proposed times. Three causes, all fixed:
- RULES (Sensa, live DB): the two sales-first-response rules "goal of every reply is to get them on a call" and
  "keep pushing for the call across follow-ups" are REPLACED by one MEETINGS rule (times once, in the first
  response; after that only when they ask, when there is something new to present, or a decision meeting is the
  next step; on hold = no times, at most one 'available whenever you are ready' line). Same rule added to
  sales-followup and email-handling.
- CODE `engine._call_is_relevant(task, req, email)` gates the slot list in `_draft_context_for_reply`: given only
  for owner (his brief/correction asks), accepted (open 'Meeting commitment'), asked (their words, `_ASKS_TO_MEET`),
  first (no earlier email to this person). `_PUT_ON_HOLD` in their words = never. Otherwise the writer gets
  `_NO_CALL_TIMES` instead of the list; manifest shows `availability(<why>)` or `no_call_times`.
- The gate reads ONLY the owner's own words (corrections incl. the one being applied via `_CORRECTION_NOW`; `brief`
  only on an email he composed: a reply card's brief is system text full of the word 'call'), and `_ASKS_TO_MEET`
  needs a REQUEST ('could we schedule a call'), never a mention ('as discussed on the call').
- CODE `pipeline.extract_commitments`: an OFFER of a call is not a commitment (it was logged as 'Commitment owed:
  arrange a call' and the owed shelf then forced the offer back into every email).

## A revised deck replaces the old one on its waiting email (21 Sep 2026)
Shama (deal 130): email 831 was drafted with deck v3 + quotation v3, then the client confirmed the delivery date.
`engine.refresh_proposal_email(proposal_task_id)` runs at the end of BOTH revision paths (`creative.revise`,
`_revise_proposal`): the open email card made from that proposal (`request.proposal_card`) gets the latest deck and
the live quotation PDF in place of every older file carrying that quotation number, its wording is reconciled, and
the proposal card closes again. A reply on a CLOSED proposal card still revises it (it reopens for the run).

## A correction can change the subject line (21 Sep 2026)
Card 837: "change the subject to media content quotation" went to the body writer and came back unchanged.
`_understand_correction` now has a `subject` channel; `_apply_understood` sets `inquiry.subject` in code, drops the
adopted thread and sets `new_thread` (an adopted thread writes its own subject back, and Gmail only chains a
matching subject). The people in copy stay.

## The same quotation again is a REISSUE, by code (21 Sep 2026)
Cloudlink: the contact was renamed (Yousif Alalawi -> Farah Ali) and the owner asked Talk for the same quotation
with the new name. Talk's only tool was create_quotation: it retyped v1's prices, the price guard blanked them,
and FIVE versions at AED 5,460 (v1 = 168,000) were issued, numbered and filed to the client's Drive folder. It also
could not find the deal (title typed 'Cloud lInk') and attach_document died on 'no company'. Fixed:
- `engine.reissue_quotation(company, number, version=, deliverables=, contact_email=, deal_id=)` + Talk tool
  `reissue_quotation`: copies the stored version's spec exactly (last fully priced entry of `quote_versions:<n>`),
  no model near a figure. The contact person prints from the CRM at render time, so a rename needs nothing else.
  Broken later versions are withdrawn to `quote_versions_withdrawn:<n>`, their library rows superseded; Drive is
  handled by the normal filing step (same-name upsert + archive_superseded).
- A VERSION HE NAMES IS COPIED (16 Oct 2026, Shama SEN-2026-0021): `reissue_quotation(version=N)` copies vN even when a later priced version exists (only an UNNAMED reissue is refused then), and only BROKEN later versions are withdrawn, so v4 copied over a priced v5 issues v6.
- `deliver_quotation`: a NEW VERSION of a number that has versions is refused (ValueError, nothing rendered or
  filed) when any line has no price and no total is given.
- `quotation._record_version` records `contact_name`, so a changed contact is a new version; old tabs in the ALL
  VERSIONS workbook keep the name they carried.
- Talk: a tool that passes `task_id` and no company takes the card's company; `deal_timeline` search ignores
  spaces/case/punctuation and also matches the client account name and the deal's contacts.

## Deal documents may be up to 40MB (21 Sep 2026)
Shama's Qabilah creative brief (22.9MB) was refused by the library's 15MB cap, which exists for files we EMAIL.
`documents.MAX_DEAL_BYTES` = 40MB applies to `kind='client-document'` saved with a `deal_id` (Talk save_document,
`_file_deal_document`, inbound attachments in `_file_inbound_attachments`); everything else keeps 15MB.

## Renaming a contact renames them everywhere (21 Sep 2026)
Owner renamed Yousif Alalawi to Farah Ali (same email) and deal 134 still showed Yousif: `crm_projects.contacts`
stores a COPY of name + email taken when the person was attached. There was never a second CRM record. Now
`crm.sync_contact_everywhere(old_email, new_email)` runs from `update_contact` (cockpit edit, Talk) and from the
`create_contact` upsert: rewrites the person's name/email in every deal's contact list + `contact_email`, and the
recipient name/email on OPEN cards only (sent cards are history). Names are trimmed; `_name_of` collapses spaces.
`crm.resync_all_deal_contacts()` is the backfill (run once 21 Sep: 6 deal contacts refreshed).

## Approving a proposal twice drafts the email (19 Sep 2026)
Owner: "why would I have to go into Talk and ask it to draft it if I've approved it?" Approval 1 of a proposal
card issues the quotation and restamps the deck with its figures (unchanged). Approval 2 (a live quotation card
exists for its number) used to just close the card; now `engine._draft_proposal_email` creates the email_draft
card exactly as Talk's draft_email would: to the deal's primary contact (`crm_projects.contact_email`), deal-linked,
`attach_docs` = this card's latest deck + the quotation PDF from the quotation card (`documents.card_ref`, Drive
pin), skill email-handling. The normal drafting path adopts the deal's real thread and sender. Blocks (card stays
open) when: no deal, no contact, both PDFs not found, or an open email to that person already exists. The proposal
card still closes itself when the email carrying its deck is sent. Nothing sends without the PIN on the email card.
PROVEN on Special Olympics (770 -> 772 -> email 773, sent 19 Sep) and made the standard flow the same day: a
creative proposal card (quotation already issued in its run) drafts the email on its FIRST approval through the same
function and closes on send too; `capabilities.py` tells Talk the flow so it does not draft a twin; `reject` on an
already done/sent card is a no-op (770 was dismissed a minute after its email went and turned 'rejected').
Cloudlink (card 775, deal 134) stalled the same day: approved several times, nothing happened, because the deal had
no contact (Talk had created Yousif on the ACCOUNT only and claimed he was on the deal) and the block reason never
reached the card. Now: `crm._attach_to_contactless_deal` (from `create_contact`) makes a new person the primary
contact of their account's ONE open opportunity when it has no contact; Talk's create_contact result states which
opportunity the person is on, or that they are on none; `_draft_proposal_email` falls back to the account's single
person (several = block and list them); every block is written on the card as "EMAIL NOT DRAFTED: <reason>".
21 Sep 2026, owner: "once it's approved then no need to have a card, period" (815 sat in the Inbox with an Approve
button after its email 831 existed). The proposal card now goes `done` the moment `_draft_proposal_email` creates the
email card; only a block keeps it open. Deck changes after that: reply on the quotation card or ask Talk.
NEXT (owner, not yet asked for): issue the quotation in the same run as the words-led proposal for this kind of enquiry.

## A person asking us for a quote is never a circular, bcc or not (19 Sep 2026)
Special Olympics UAE (deal 133) wrote "Dear Supplier, please quote for the below" with suppliers on bcc, shoot four
days out: our address was not on To/Cc so the 17 Sep fix did not apply, the model matched "mass Dear Supplier
announcements" and no reply was drafted. Now `policy.asks_us_by_email(email)` (code, before any model): the mail
ASKS (`_ASKS`: please quote, RFQ/RFP, submit your proposal, scope of work...), has no portal wording (`_VIA_PORTAL`)
and comes from a named person (not `auto_marker` / `_ROBOT_NAME`) -> every `_TENDER_SITUATION` leaves the list.
The no-draft prompt also says so. Portal robots (takamol@sfd.gov.ae) still never qualify. Checked on 14 days of
hello@: 18 hits, all real enquiries.

## Proposal approval: an unreachable target blocks the quote; re-approval versions the number (17 Sep 2026)
Rana's deck (card 725) said AED 136,000; the rate-card lines came to 173,100 (27 interview edits at the 5-minute
rate), the 20% band could not reach the target, and SEN-2026-0017 was issued at 173,100 with the gap buried in
the card text while deck v3 still said "within your stated budget range". Now in `_approve_proposal`: (1) a
"your figure ..." entry in the prep's blanked list BLOCKS the quotation (card stays open with what it would take);
(2) a deck whose quotation card was CANCELLED re-issues as the next version under the same number
(`deliver_quotation(number=)`), a live card just closes; (3) the writer's investment blurb survives only when its
headline equals the stamped net (within 1%). Owner: rate card gained "Mini interview edit, short sponsor or event
interview" AED 1,500 per video (batches of short interviews); Sensa `quote_target_flex_pct` 20 -> 25.

## "Our work" page: words-led proposals yes, creative proposals never; named films lead it (17 Sep 2026)
Owner's refinement of the 16 Sep switch: `deck.our_work_page(company, creative=False)` returns False for every
CREATIVE deck (creative.py passes creative=True) and True for words-led decks unless a "NO OUR WORK PAGE" rule is
set. The rule on Sensa sales-proposal (168) now reads "OUR WORK PAGE ..." (informational; code carries the
creative rule). `create_proposal(films=[...])`: up to 3 titles/clients/YouTube ids the owner NAMES, resolved by
code against media_assets (an unknown name stops the build and says so), stored as `spec.samples.video_ids`;
`deck.render` puts them first and fills the rest by category and rating (`films_by_ids` + `pick_samples`).
First case: Rana Elbadaoui's event coverage, with China Innovation Center (rated 4, never picked by rating).

## No "Our work" page in proposal decks (owner, 16 Sep 2026)
On the ChainX creative proposal (card 704) the owner replied "remove page 19, our work": the auto-picked films
are not our best; work is shown through the company profile instead. The revision re-ran COPY (the writer
reworded the heading) but the page is laid out by code, so v2 still had it. Now: rule "NO OUR WORK PAGE" on
Sensa sales-proposal (168) is the switch; `deck.our_work_page(company)` reads it (any effective rule on
sales-proposal or sales-quotation starting "NO OUR WORK PAGE") and both `deck.render` and the CreativeDeck
render skip the samples page when it is off. Per company: add the rule to another company's skill to switch
it off there; remove the rule to bring the page back. ChainX re-rendered as v3 from the saved copy (no model
calls) and attached to quotation card 705.

## Quotation filed on the deal by name; one near-duplicate client folder is the folder (16 Sep 2026)
The ChainX cover email (card 707) was drafted "with the quotation attached" but only carried the deck. Chain of
causes: the client folder is "ChainX Mining LLC", `ensure_client_folder("ChainX Mining")` treated it as a
near-duplicate and refused to file; with no Drive filing `deliver_quotation` saved the PDF as
"quotation-sensa-SEN-2026-0016.pdf" with no client and no deal; the deal-scoped `documents.find` (scope =
deal title words, or the row's client) could not see it. Now: `ensure_client_folder` uses the ONE near match
when it is the name plus or minus a trailing word; `deliver_quotation(deal_id=)` names the library row
"<Client> - Quotation <N> vK - date.pdf", tags the client and files it on the deal whatever Drive did (every
caller passes deal_id: Talk create_quotation, prep cards, proposal approval, the creative run); `documents.find`
takes `deal_id` and a row on that deal always qualifies (draft_email, attach_document, corrections pass it).
ChainX docs 503/507/508 re-filed into "ChainX Mining LLC" and renamed; 707 attach refs refreshed.

## A named sender sends from their own mailbox (17 Sep 2026)
Card 711 (Talk draft_email to Maricris Sulit, from=rashad@) went out as "Gino Palmes" over Rashad's signature:
draft_email set `from_email` but never `mailbox_rt`, the send path fell back to `gmail_send_refresh_token:sensa`
(Gino's account), and Gmail sends as the account owner whatever the From header says. Every inbox-reply card
already carried `mailbox_rt` (`gmail_refresh_token:sensa:rashad`), so only Talk-drafted outbound was exposed.
- `draft_email` resolves `mailbox_rt = _rt_for_sender(co, from_email)` and REFUSES a sender no connected
  mailbox can send as (lists the senders it can).
- `_send_email_reply`: no usable `mailbox_rt` + a From address -> `_rt_for_sender` first; if none and the
  address is not the company send account, the send is BLOCKED (card back to awaiting_approval, reason on
  the card) rather than sent under another name.
- The decisions snapshot for 711 says from=rashad@ (the envelope), not what Gmail did.

## Attachments: documents first, signature images ignored (17 Sep 2026)
Maricris's project brief PDF was the 7th attachment on her reply behind six signature PNGs; `_inbound_att_refs`
(cap 4) held four logos and `_file_inbound_attachments` (first 6) never reached the PDF, so the brief was neither
read for card 716 nor filed on deal 128. `_documents_first(atts)` now orders documents before images and drops
images under 120 KB (signature graphics); both functions use it. The brief was then filed on 128 and 716 redrafted.

## One open opportunity per client account (17 Sep 2026)
Shama Perfumes got deals 130 + 131 (Talk called create_deal twice, 18 s apart, different titles; the only guard
was an exact-title match). ADWPJJC got 128 (opened from Maricris's WhatsApp via Talk, no contact email) + 129
(Pyxis's formal RFQ through the tender path, which keyed on the email subject and never looked for the sender's
open deal). Now: `crm.open_opportunity_for_account(account_id, company)`; `crm.create_deal` raises DuplicateDeal
when the account already has an open Opportunity/Quote unless `second_job=True` (Talk's create_deal exposes
`second_job`, to be used only after the owner confirms; the cockpit form passes it, his own hand).
`_track_tender` attaches a circular to the sender's open opportunity (`open_deal_for_email`/`_domain`, sales
stages only) and adds the sender as issuer, creating a tender deal only when there is none. Data: 129 -> 128 and
131 -> 130 folded (contacts, tender note, closing reminder moved; duplicates Lost with a note; nothing deleted).

## Meeting notes: the brief drives drafts, the full write-up lives on the deal (17 Sep 2026)
Owner's standard: keep both. `meeting_notes.full_text` holds Gemini's whole write-up (both sweeps);
`meetnotes._keep_full` also files it on the meeting's deal as a `meeting-notes` document ("Meeting notes - <title> -
<date>.txt"), so `read_document`, `_deal_facts` (proposals, quotation prep) and the creative run read all of it.
Email drafts and commitment reminders keep using the 250-word brief (`latest_for_contact`), which keeps the
per-draft cost small and the invented-detail risk low. A raw TRANSCRIPT needs Meet's "transcribe" switched on for
the Sensa calendar; it would arrive as a second doc and file the same way. Backfilled for the Rana meeting (rows
20/21, deal 127).

## Phishing guard + one email, one company (15 Sep 2026)
Heba at Jump (a real July contact) had her mailbox hacked. It bcc'd Sensa and Sky Vision an empty "Re: RFQ#
Videography Services" with a 1-page PDF (made by Aspose minutes before) whose VIEW RFP DOCUMENT button went to a
fake Google sign-in (`begone602.nobleoak.com.de`). Cortex read the PDF as a brief and made TWO warm reply cards,
one per company (680 Sky Vision, 681 Sensa), offering calls. Owner rejected both; contact set not_qualified +
newsletter_opt_out with a `phishing` history event.
- `phishing.check(e, rt_key, client, ours)`: a safety invariant (like the invented-link guard), so code, judged
  only on WHERE links point. Flags (a) an attached PDF of 3 pages or fewer with a view/access/open-a-document
  phrase (`_CTA`) and a link off the sender's registered domain (`base_domain`; `_MEDIA_HOSTS` exempt), or
  (b) a body link whose label is a `_CTA` and whose host is off the sender's domain and not a `_SHARE_HOSTS`
  file-share. Our own domains never count. `gmail._parse_generic` now returns `links` ([href, label]: HTML
  anchors + the plain part's `LABEL <https://...>`).
- `engine._phishing` wraps it (a failed check never stops mail). Hooked in `poll_inbox` after the claim and in
  `poll_sales_replies` (a hacked lead replying on a real thread). A hit: no card, no qualify, no opportunity;
  `_flag_phishing` sends one CRITICAL security notification per Message-Id (`phish:<mail_ref>`) and logs a
  `phishing` event on the contact. False positive: ask Talk to draft the reply.
- Tested on real mail: the Jump email flagged in all 3 mailboxes it reached; 0 of the other 315 emails from 14
  days across every inbox.
- ONE EMAIL, ONE COMPANY: `_claim_mail` is per company, so a bcc to two companies' mailboxes carded twice.
  `_carded_by_other_company(co, ref, sender)` stops a copy when another company already holds an `email_reply`
  card for the same `mail_ref`, unless this company has an open deal with the sender and that one doesn't. Keyed
  on a CARD, not a claim, so a company that decided "no reply" never swallows mail meant for another.

## The owner's price wins: 40% band, Budget tier by code, figures stamped by code (21 Sep 2026)
Emergy (card 796, deal 132): he replied "bring it down to 28,000 excluding VAT"; the creative run never passed a
target to `price_lines`, the writer's assumptions claimed "net total AED 27,800 at Budget tier", and code issued
v3 at 39,500 (card 808), so the card contradicted its own quotation. Fixed:
- Sensa `quote_target_flex_pct` 25 -> 40 (owner: deductions only ever happen when HE asks for a lower number).
- `ratecard.price_lines`: a target below the band on the Normal tier is retried on the BUDGET tier (code picks
  the tier, only to reach his figure); result carries `tier` + `normal_total`. Every caller gets it (Talk
  create_quotation, prep cards, proposal approval, creative run).
- `creative.stage_quote`: the writer returns `target` (a figure from his own words only, checked against
  `_stated_numbers`); code applies it; an unreachable target RAISES (nothing issued, the card says why).
  Assumptions that state totals, tiers, discounts or amounts are dropped (`_MONEY_CLAIM`); the summary's
  "Your figure AED X + VAT: reached (Budget tier used ...; lines moved -N%)" line is stamped from the result.

## Signed quotation back -> pro forma invoice (21 Sep 2026)
Owner's flow: the client signs a quotation and emails it back; Cortex confirms WHICH stored version they signed;
he confirms; the pro forma for the first payment stage is issued. `runtime/cortex/proforma.py` + the pro forma
block in `engine.py` (above `_push_quote_to_client_drive`).
- DETECTION: `_file_inbound_attachments` calls `_check_signed_quotation` for every PDF filed on a deal. Code gates
  the model call (`proforma.deal_quotations` non-empty, no `quotation-signed:` event on the timeline yet, no open
  confirm card, `worth_reading`: a scan or a PDF naming a quotation). ONE Haiku pass READS the pages
  (`read_returned`: is it our quotation, signature / stamp seen, the total printed, handwritten changes).
  `match_signed` is pure code: number -> the deal's own quotations; version -> the stored version whose gross
  equals the total on the returned copy, else the last version SENT (`quotation-sent:<n>:v<k>` refs). Flags: an
  older version signed, a total matching nothing, handwritten changes, no visible signature. The filed copy
  becomes library kind `signed-quotation`. Photos (jpg) of a signed page are NOT read: use Talk.
- CONFIRM CARD: kind `content`, `request.kind = 'signed_quotation'`, inserted `awaiting_approval` (never drafted
  by the worker), button "Confirm signed & issue pro forma". A reply naming a version ("they signed v2") re-matches
  by code (`_signed_reply`). Approve = `_confirm_signed`: `crm.set_deal_terms` from the version's stages, deal value
  = that version's net, timeline `quotation_signed` (ref `quotation-signed:<n>:v<k>`, which `_signed_record` reads
  back), stage-1 pro forma issued, THEN stage -> Booked (kickoff card etc. fire as before). Talk `quotation_signed`
  raises the same card when he reports a signature by hand; nothing skips the confirmation.
- THE FIGURES: never from the returned PDF, never from a model. `proforma.net_of(entry)` (now also what
  `pipeline.record_quotation_sent` uses), stages parsed by code from the payment lines the version PRINTED
  (`quotation._record_version` keeps them on the registry entry as `payment`, OUTSIDE the spec/sig so no version
  bump; older entries fall back to the preset's `payment_lines`, else `quotation.DEFAULT_PAYMENT_LINES` 70/30).
  Lines that do not total 100% block the issue and ask for the split. VAT from profile `vat`.
- THE DOCUMENT (owner-approved layout, after his Invoice 4300 sample): logo on a black tile, legal entity + TRN +
  address right, PRO FORMA INVOICE centred, Billed To = the client COMPANY (never a person), one line
  "70% down payment against quotation SEN-... for media production", Subtotal / VAT / Total / Amount Due, bank
  details, "not a tax invoice" footer. HTML -> `deck.to_pdf` (weasyprint). No Sky Vision logo (owner: looked bad;
  only white-on-dark logos are on the profiles). Billed-to detail lives on `crm_accounts.billing`
  ({name, address[], trn}), set from his words through Talk `issue_proforma(billed_to=[...])`.
- WORDING + NUMBERING ARE DATA: the `PROFORMA = {...}` line on the company's `finance-quote-to-invoice` craft
  (prefix, seq, description template, work, stage labels, due_days, footer). Sensa and Sky Vision share
  `seq: skyvision-entity` (one legal entity, one number run): `PI-YYYY-NNNN`, counter `proforma_seq:<seq>`,
  atomic upsert. Registry `proformas:<quotation number>`: one row per pro forma (stage, pct, amounts, status
  issued/sent/void, doc_id, task_id). A stage is never invoiced twice; `replace=` voids an UNSENT one (number
  never reused, library file renamed VOID, card cancelled) and refuses a sent one.
- PRO FORMA CARD: `content`, `request.kind = 'proforma'`, PDF on the card, library kind `proforma` on the deal +
  the client's Drive folder. Approve = `_approve_proforma`: an `email_draft` on `finance-invoice-sending`
  (`worker._RELATED_SKILLS_DEFAULT` adds email-handling's voice) to the deal's primary contact, the only figures
  it may state stamped in the brief, `request.money = True`. `engine.is_money(task)` (kind OR that flag) feeds the
  step-up gate and the cockpit's `owner_only`: a team PIN cannot send it. On SEND `_proforma_sent` marks the
  registry row `sent` and arms the 8 / 15 / 22 day payment reminders (`on_done_reminders`).
- STAGE PLAYBOOK: `crm._stage_patterns`' Advance / Balance invoice cards now file on finance-quote-to-invoice,
  carry `prep_action: 'proforma'` (+ `proforma_stage` first/last) and approving one ISSUES that stage's pro forma
  (`_prep_build_proforma`); the Advance card is skipped when stage 1 already has a pro forma. Those cards only ever
  fired when `payment_terms` was set, and it was set on no deal before this build.
- NOT BUILT: a tax invoice (stays with the accountant), payment received / reconciliation, reading a signed page
  sent as a photo, the SignWell e-sign route (contracts branch, undeployed).
- WHICH QUOTATIONS ARE A DEAL'S (`proforma.deal_quotations`): `quotation-sent:` refs on the timeline, plus ANY
  quotation card on the deal whatever its status (Honor's SEN-2026-0012 went out by hand: cards cancelled, no
  send recorded), provided the number's registry customer still equals that card's customer (Honor's cancelled
  0011 cards never offer BioScience's number). A match with no recorded send is flagged on the confirm card.
- TESTED 21 Sep 2026, read-only: all 21 live SEN quotations parse to stages totalling 100% (ai-production,
  retainer and assignment print the 70/30 default; shoot-production 50/25/25); the reading pass on real
  SEN-2026-0013 v2: unsigned copy -> no card; a test-signed copy -> signature, stamp, signer, date and total
  read, matched to v2 with no flags. No pro forma has been issued yet: the first real one is PI-2026-0001.

## A creative deck carries its voice over (23 Sep 2026)
Owner, on the RAFED SAKINA deck: "can you add a really nice heartfelt voiceover to it as well". There was nowhere
for one to live. The script schema had `on_screen_text` and `caption` only, so a revision asking for narration had
no field to land in and `deck.beat` had no place to show it; writing it into `on_screen_text` would have printed
the narration as on-screen typography, which is a different thing.
- `stage_script` beats now carry **`voiceover`**: the words heard over that beat, empty where the beat plays on
  picture and sound alone. The prompt states that on-screen text and voice over are never the same words, that a
  beat the owner's direction says is wordless stays empty, and that the narration across all beats must read as one
  continuous piece at a speakable pace for its timings.
- `deck.beat(..., vo=)` renders it as its own panel (accent rule, italic, bottom right, clear of `.cap`), never in
  the `.ost` on-screen slot.
- `deck.voscript(...)` is a new page, `NN · The voice over`, laid out BY CODE from the script: timecode beside line,
  a wordless beat shown as "No narration" rather than dropped, because the silence is part of the film. It renders
  only when at least one beat has narration, and its sub line is code-computed ("Heard over beats 3 to 9. The
  remaining beats play on picture and sound alone.") unless the copy writer supplies one.
- `stage_copy` is given each beat's `voiceover` and told it carries the on-screen lines AND the voice over verbatim:
  only the voice over page's heading, sub and note are the writer's. Copy schema gained `voscript`.
- `changed_frames` still compares `frame_prompt` only, so adding or rewording narration never regenerates a frame.
- The voice over page's `sub` and `note` are FOOTNOTES and are clamped to 150 characters in `deck.voscript`: on v3
  the copy writer echoed the whole direction back into `note`, the page spilled onto a second one and the planned
  page count and footer numbering both broke. A footnote slot that a model fills needs a code-side clamp.
First applied: SEN-2026-0024 v3 (RAFED / SEHA, deal 136, The Weather Inside), narration on the 60 second film and
none on the 15 second cinema film.
- THE NARRATION IS THE BIG TYPE (owner, 23 Sep 2026: "spread the voiceover out in nice text over the frames ... so
  you can read it and feel it as you go through the deck"). On a SPOKEN beat `deck.beat` sets the narration large
  over the picture (`.vol`, italic Poppins 31px in a `.bstack` so the chip above it needs no height guess) and the
  ON-SCREEN line drops to a small labelled chip (`.ostchip`). A beat with NO narration keeps the old layout, its
  on-screen line in `.ost` at 44px, so a deliberately wordless film still reads as one. The side panel this
  replaced was too small to read the film from, which was the point of having it.
- THE STANDARD IS A RULE, NOT ONLY CODE: "VOICE OVER IN A CREATIVE DECK" on Sensa `sales-proposal` (rule 4) states
  the layout AND when narration is warranted (a services or journey film usually, a short cinema turn usually not,
  because a narrator crowds a single held moment). Company-scoped; promote to universal on the owner's word.
STILL OPEN on the same deck: the contact sheet's subtitle is stamped in `creative.render` as title plus total
duration ("The Weather Inside: 75 seconds"), so on a two-film job it reads as one long film and no feedback can
move it. Same class as the "our work" page: the page is laid out by code, so the fix is code.

## Discounts, "same quotation plus a change", and the start gate (26 Sep 2026)
UAS (deal 138) confirmed the AI film, then asked for a half-day shoot (founder message + reference capture) and
price flexibility, under a USD 10,000 ceiling. Owner: add the half-day shoot, no filming permits (a private
shoot needs none), then 10% off the whole quotation.
- DISCOUNT, BY CODE: `quotation._resolve(discount_pct=)` (threaded through `generate`, `generate_xlsx`,
  `deliver_quotation`) computes the amount from the priced lines and appends it as its OWN negative line in a
  `DISCOUNT` block (item carries `discount_pct`). Every reader of the stored version sums unit x qty, so the pro
  forma (`proforma.net_of`), the creative deck's investment page (`quote_rows`) and the deal value all net it
  with no discount logic of their own. A reissue that copies a discounted version carries the line as is; a
  new percentage replaces it, recomputed from the undiscounted lines. ONLY A PERCENTAGE THE OWNER WROTE:
  Talk guards it against `_TURN_SAID`, the prep card against `_stated_numbers` of his words.
- `reissue_quotation(..., add_sections=, discount_pct=, preset=, title=, note=, said=)`: the stored lines copy
  exactly; added blocks are priced by `ratecard.price_lines` (components, or a figure in `said`), lettered on
  from the stored blocks, placed before the discount; any blank line refuses the reissue. Talk's
  `reissue_quotation` tool exposes all of it (capabilities line updated).
- START GATE is a RULE (owner, 26 Sep 2026), company-scope on Sensa sales-quotation, sales-proposal,
  sales-first-response, email-handling, finance-quote-to-invoice, prod-pipeline, prod-packaging: no work starts
  before a SIGNED quotation and the DEPOSIT; in a rush a bank transfer confirmation starts the storyboard, but
  nothing (storyboard, frame, cut, any content) is DELIVERED until the deposit has CLEARED. Every production
  timeline starts on that day. Cortex has no deposit ledger yet, so this is enforced by the rule and the
  approval card, not by code.
- PRODUCTION TIMELINE PAGE: prototyped for UAS as a scratch job (`/opt/coretex/creative/_draft-138-v6-timeline/tl.py`,
  not repo code): a working-day template (storyboard v1, one round of notes, v2, sign-off, half-day shoot,
  first cut, two rounds, final) dated by code from the start day, rendered as a Gantt page before Investment.
  Pending owner feedback before it becomes a `CreativeDeck` method + per-proposal-type templates on sales-proposal.

## A closed deal takes its unsent Cortex cards with it; conditional promises have no date (1 Oct 2026)
- CAUSE: UAS (deal 138) went Dormant on 30 Sep. `crm._cancel_cortex_reminders` cancelled its reminders, but cards
  1004 and 1005, already drafted from two of them, stayed in the Inbox.
- `crm._cancel_cortex_reminders` (called by `set_project_stage` on a move into Lost/Dormant/Completed) now also
  cancels the deal's unsent email cards that Cortex raised by itself: `request.system_note` starting
  "From reminder #" / "From commitment reminder", or `request.followup` in followup/checkin/chase. Replies to a
  client's own email and owner-requested emails are never touched. Logged on the deal timeline.
- `pipeline._CONDITIONAL`: a commitment that waits on the client ("on receipt of", "once you confirm",
  "as soon as the deposit", "subject to") gets NO dated reminder in `record_send`; it is logged on the deal as
  `[context]` instead.
- `engine._rejudge_pending_replies`: a waiting draft that only restates what the sent email said is "full"
  (cancelled), not "part".

## A paused chase never stays paused with nothing pending (1 Oct 2026)
- CAUSE: Sheraa SEF'27 (deal 123) showed "arming..." for a week. Shahnawaz's 24 Sep reply paused the chase; our
  answer (card 909) went out unlinked because Sheraa has two open deals, so nothing re-armed the clock.
- `engine._deal_for_thread` also matches on the References chain (Gmail thread ids are per mailbox): a deal whose
  cards replied to one of the referenced messages, or whose timeline logged one coming in, owns the email.
- `pipeline.record_send`: an unlinked card sent to a contact with several open deals resolves its deal by thread.
- `engine._rearm_stalled_chases` runs before every cadence sweep: an Opportunity/Quote deal on auto with no clock
  and no open card re-arms when our email was the last word; when theirs was, the owner is notified once
  (timeline ref `unanswered:<ts>`).

## Every open deal is on auto (1 Oct 2026)
- Owner rule: "there should be nothing on manual anymore". Nothing in code sets a new deal to manual now.
- `crm.hold_auto(deal_id)`: automation='auto' with no clock, used where a chase would be wrong right now (they asked
  US for a proposal in `qualify_opportunity`, a portal tender, `create_deal(arm=False)`, no contact yet in
  `arm_new_deal`). The first email we send arms it (`record_send` -> `touch_followups`); the stalled-chase net
  flags an unanswered email of theirs. Lost/Completed/Dormant/Nurture are never set to auto.
- Cockpit: an auto deal with no clock reads "Chase starts when we next email them" (was "arming..."). sw `cortex-v57`.
- 1 Oct audit (`audit_auto.py`, scratch): every Opportunity/Quote deal switched to auto, logged on each timeline.

## Closing rhythm: chases tighten when a quoted client engages (1 Oct 2026)
- Owner: "once we've got close to closing a deal, the follow-ups seem to slow down". DECISION_CADENCE lengthened its
  gaps (3/4/7/14) while the client was weighing the quote.
- `crm.CLOSING_CADENCE` (name "closing", `working_days`: gaps counted Mon-Fri): every 2 working days x5, then 3 x4,
  then 5 x4, then 10-day check-ins x2, then revivals. Company override: `closing_cadence` on the profile
  (`crm.get_closing_cadence`).
- `crm.enter_closing(deal_id)`: on every inbound email resolved to a Quote deal on auto that has a `quotation_sent`
  event, the deal's cadence becomes closing at step 0 (engine inbox path, before the pause/reschedule). Our reply arms
  the clock (`touch_followups`); a date they state still wins. `resume_followups` now honours the per-deal cadence.
- Chase card brief says CLOSING CHASE; the wording lives in the universal sales-followup rule "CLOSING CHASE".
  The chase gate is told "reviewing / will get back" is not a reason to hold on a closing deal.
- Sheraa SEF'27 (123) moved onto it 1 Oct, next chase Fri 2 Oct 09:00.
- 1 Oct: 117, 118, 130, 132, 133, 135, 136, 141 moved onto the closing rhythm. RAFED (136) had its chase pushed to
  3 Jun 2027 by a vendor-affairs bank-details notice read as a client timeframe; the `followup-wait` prompt now
  ignores general notices to all suppliers/customers.

## Quoted deals chase on the closing rhythm from the first day (6 Oct 2026)
- Owner: chases after a quote "seem a little bit slow". Two causes: the chase gate held 11 due chases in a week as
  "our last email already covers it / awaiting their response", and each hold waited the next full gap (4-14 days);
  and DECISION_CADENCE (3/7/14 then fortnightly) ran until the client first replied.
- `crm.set_project_stage` -> Quote now sets CLOSING_CADENCE (every 2 working days x5, 3 x4, 5 x4...).
  `crm.enter_closing` no longer needs a `quotation_sent` event: any Quote deal on auto qualifies (proposal or quote).
- Chase gate on a Quote deal: waiting for their answer is the reason to chase; holds only for a decision, an
  on-hold, a date they gave that has not passed, or our email to them in the last 2 working days.
- A held chase on a Quote deal is re-checked in 2 working days (`next_followup`), not after the next full gap.

## The sent sweep knows Cortex's own sends in any mailbox; newsletter replies have their own address (7 Oct 2026)
- CAUSE: card 1060 (Dawn Christine, Sky Vision deal 103) sent from rashad@skyvision.film; its copy in the owner's
  personal Gmail (connected 3 Oct) has a different gmail id there, so `_sweep_mailbox` called it a "manual" email,
  and it looked for a deal only under company "Personal", so it raised "Untracked sales email ... NO open opportunity".
- `pipeline._sweep_mailbox`: a Cortex `send` decision to the same recipient with the same subject (Re:/Fwd: stripped)
  in the last 2 hours = that send, skipped. No deal under the mailbox's company -> every other company's deals are
  searched (`active_deals_for_email` per slug); a match is filed under the deal's company (`dco`).
- Newsletter Reply-To: profile `newsletter_reply_to` (Sensa = hello@sensa.digital, owner 7 Oct 2026) wins over
  `reply_from` (Gino, the 1:1 sender) in `newsletter._sender`. Editable on the profile (Communications).

## A weekday beside a date is computed, never written by a model (7 Oct 2026)
- CAUSE: card 1110 (Al Hamra RFP acknowledgement) said the deadline was "Wednesday 12 October 2026"; it is a
  Monday. The client wrote only "12 October 2026". The Manager flagged it, but a flag does not change the email.
- `engine._fix_weekdays(text, now)`: every "<weekday> <day> <month> [year]" or "<weekday>, <month> <day>" pair is
  checked against the calendar and the weekday replaced (full/short form and case kept). No year = the year that
  puts the date nearest ahead of today (up to 60 days back). Runs at the end of `_ensure_clean_email` (every draft,
  correction and reconcile path, so the card shows the fix) and in `_clean_email_text` (the send-time backstop).
- `_read_through` also removes words describing how the email was written ("framed as optional", "as instructed").
- The "Sensa Productions" line above the sender's name in a COPIED card is the signature logo's alt text, not a
  duplicated sign-off.

## The contact is recorded before the reply is drafted (7 Oct 2026)
- CAUSE: Al Hamra's RFP (Kiran Pinto, card 1110) qualified "high" with an AED 60,000 estimate but no opportunity
  appeared. `poll_inbox` ran `_draft_direct_reply` (which qualifies and calls `crm.auto_opportunity`) BEFORE
  `_record_contact`, and `auto_opportunity` returns None when the sender has no `crm_master` row, so every
  brand-new direct-email lead was silently left without an opportunity. Four found: Al Hamra, Liebherr, Accor,
  NTUC Club (Snap Rewards).
- `_record_contact()` now runs before the draft (idempotent with the call after). A lead qualified "high" that
  still gets no opportunity raises a high-priority notification (`qualified-no-opp:<email>`); a qualify error
  prints `[qualify] <email>: ...` instead of passing silently.
- Backfilled the four on 7 Oct through `crm.auto_opportunity` (on auto; Al Hamra holds its chase until we send).

## A client's email with no draft is still logged and surfaced (8 Oct 2026)
- CAUSE: Franco Fung (Sampras HK, recurring deal 60) wrote to Rashad about Cyberport stability issues and newer AI
  competitors, Ben copied. Tabscanner's rule "never draft responses to training or support requests, handled by Ben"
  matched it in `policy.should_skip`; the skip branch only recorded the contact, so the email left no card, no deal
  entry and no notice.
- `poll_inbox` skip branch: a skipped email from someone on an active deal is logged on that deal (`email_in`, with
  "[no draft: <rule>]", ref = mail_ref) and raises one high-priority notification naming the rule
  (`skipped-client:<mail_ref>`).
- Rule text (Tabscanner sales-first-response, live DB): the two support/training no-draft rules now carry the
  exception "never applies to an email addressed to Rashad about the relationship, contract, pricing, service
  stability or competitors; only to genuine technical help or training requests".
- THE REAL CAUSE was code, not the rule: `policy.should_skip` matched a compiled handling mailbox (`no_draft:<skill>`
  `addresses`, Tabscanner = support@ + ben@) ANYWHERE in To/Cc/From. Ben is copied on nearly every Tabscanner client
  thread, so any client reply-all was skipped. Now the address must be the sender or on the To line, with no other
  address of ours on the To line beside it. Policy recompiled after the rule edit (`policy.compile_skill`).

## Talk quotations: his figures are read as he writes them; a refused build is never resent (8 Oct 2026)
- CAUSE: SEN-2026-0033 (Special Olympics, card 1126). Rashad wrote "production crew will be 42Kaed total, camera 4K
  total, remove the color grade, photo editing 2K per day". `engine._stated_numbers` needed a word boundary after
  the k, so "42Kaed" was never read; the crew line was blanked; `deliver_quotation` refuses a new version with a blank
  line; Talk resent the identical request eight times and claimed work it had not done. The removal never happened
  because nothing was built.
- `_stated_numbers` now reads 42K, 42Kaed, 42k AED, AED 42,000, 42 thousand, and a trailing full stop.
- `create_quotation` refusal says "Quotation NOT built", names the blank lines and lists the figures read from his
  words, telling Talk to ask him rather than retry.
- `_chat_prepare._exec`: a create/reissue_quotation request refused in a turn is refused again WITHOUT running if
  sent unchanged ("NOT RUN: this exact request was already refused...").

## Support replies are written from the company's own help pages (10 Oct 2026)
- CAUSE: Ralph Delgado (Snap Rewards, card 1114) asked why receipt images did not show on dvbrands.com. Cortex had
  read none of Snap Rewards' 16 help pages, so the draft asked which browser and offered call times, while the answer
  (Campaign Data -> open the submission -> the Campaign Result screen shows the receipt) was on snap-rewards.com/docs.
- `runtime/cortex/helpdocs.py`: profile key `help_docs_api` (a WordPress REST collection; Snap Rewards =
  https://snap-rewards.com/wp-json/wp/v2/docs) -> `sync(company_id)` stores every page's text in setting
  `helpdocs:<company_id>`; `maybe_sync()` (engine loop, looks hourly) re-reads a copy older than 7 days.
  The site's HTML pages sit behind a Cloudflare challenge; the REST API is readable.
- `worker.draft` puts `helpdocs.block()` (PRODUCT DOCUMENTATION) in the cached system prompt of every email draft
  for a company that has help pages, above the rules. `profile.trusted_links` admits the page URLs, so linking the
  exact help page passes the invented-link guard.
- Behaviour is a RULE (Snap Rewards sales-first-response + email-handling, "SUPPORT QUESTIONS"): answer how-to and
  problem emails from the documentation with exact steps and screen names; never invent a feature; not covered or a
  fault -> ask for store URL, submission email and a screenshot and say Ben (CTO) will look into it; no call times.
- Ben is copied on EVERY Snap Rewards email: profile `always_cc` = [ben@snap-rewards.com] (owner, 10 Oct 2026).
- Snap Rewards mail: inbound to loyalty@snap-rewards.com (catch-all, never sends); every reply goes FROM
  rashad@snap-rewards.com (`reply_from`, token `gmail_send_refresh_token:snaprewards`), cc Ben, bcc the owner's Gmail.
- SUPPORT SENDS FROM loyalty@ (owner, 10 Oct 2026: "I don't want them to come from Rashad"). Profile flag
  `inbox_sends: true` lifts "catch-alls never send" for that company only: `_email_envelope` keeps the inbox as From,
  `_draft_direct_reply` keeps the receiving inbox + its token, `_rt_for_sender` returns the inbox's registry token.
  Snap Rewards: `reply_from` = loyalty@snap-rewards.com; `signatures` keyed by email (loyalty@ = "Snap Rewards
  Support Team", rashad@ keeps his own). loyalty@'s token (gmail_refresh_token:snaprewards) has gmail.modify, so it
  can send. Old threads where a contact was last emailed from rashad@ stay with rashad@ (thread-sticky sender).

## Closing chases: code decides, the model answers one narrow question (12 Oct 2026)
- CAUSE: owner "I've not seen many follow-ups recently". In the week to 12 Oct the general chase gate held 27 chases
  on quoted deals ("our last email already covers it", "client is reviewing") and re-checked every 2 working days to
  hold again: two chase cards went out. It ignored its own instruction (RAFED: "waiting for their response is the
  reason for chase, not a reason to hold" -> held) and misread dates (ValuStrat: 3 Oct "not yet passed" on 9 Oct).
- `engine._closing_gate` replaces it for Quote / closing-cadence deals: (1) our email in the last 2 working days
  (deal timeline, code) -> wait; (2) Haiku classifies ONLY their newest message: declined / on_hold / accepted ->
  hold, pause the clock, notify the owner once to move the stage; date -> resolved from THEIR message's date, the
  chase moves to the next working day after it (or goes now if passed); none -> the chase goes. The general gate
  still runs for Opportunity-stage and won-work check-ins.

## A WhatsApp enquiry is filed under the brand it was written to (12 Oct 2026)
- CAUSE: sensa.digital and skyvision.film share one WhatsApp number, `wa_routing` names one company, so every Sky
  Vision enquiry was a Sensa card and the acknowledgement said "Sensa Productions" (card 1188, "Hi Sky Vision, I
  would like to enquire about drone filming"). The owner's correction ("Rashad from Sky Vision") was redrafted by the
  worker, which re-read the AUTO REPLY rule and put the same words back.
- `whatsapp._brand_lane`: setting `wa_brands` = [{company_id, match: [words], hosts: [domains]}]. A brand word in
  the message, or the ad click's page on that host, files the card, the opportunity, the CRM row and the phone
  contact under that company and its `social-dm-replies` skill (and its AUTO REPLY rule). Set: Sky Vision (4),
  match "sky vision" / "skyvision", host skyvision.film.
- `apply_correction`: a correction on an `auto_reply` wa_reply card is a Haiku minimal edit of THAT text
  (`wa-ack-edit`), never a worker redraft.
- `engine._keep_team_names`: voice transcription turns Rashad into "Richard" and the edit copied it. A roster first
  name (profile `signatures`) that was in the old text and vanished is put back over the ONE new capitalised word.

## A client on an open deal is never skipped on who is copied; a client's date moves every chase (13 Oct 2026)
- CAUSE: Mike Enting (ABN AMRO, Tikkie deal 108) wrote on the deal thread on 24 Sep that the bank would answer the
  report in the week of 5-9 October. Ben was copied, the compiled "support is Ben's" address match skipped it: no
  card, nothing on the deal, the chase not moved. Mike was not a deal contact (a colleague at the same account), so
  the 8 Oct skipped-client log would also have missed him. On 8 Oct the general chase gate held "for their date" and
  the deal then waited the next full gap (22 Oct) instead of chasing on Monday 12th.
- `poll_inbox` resolves the sender's deals (`active_deals_for_email`, else `open_deal_for_domain`) BEFORE the no-draft
  check and passes the deal to `policy.should_skip(deal=)`: with a deal, the handling-mailbox address match never
  applies, and the content check is told the deal by name (deal correspondence is never a no-draft situation; only a
  pure technical help/training question is). The skipped-client timeline log + notice use the same deals.
- `engine._resolve_their_date(co, phrase, base)`: one resolver, from THEIR message's date, a range or week = its LAST
  day. Used by `_closing_gate` and now the general chase gate: the gate is given today's date, holds only for a date
  that has NOT passed, returns their words; code resolves them; passed -> the chase goes; future -> next_followup =
  the next working day after it.

## What he corrected earlier on an email stays corrected (13 Oct 2026)
- CAUSE: owner, "I send a correction, then a different correction, and it pulls back the problem I corrected two
  corrections ago". A redraft saw only the NEWEST correction plus the previous draft; this card's own earlier
  corrections were deliberately excluded (`owner_feedback` reads OTHER cards to the same person), so the thread or
  the deal record could put back what he had removed.
- `_draft_context_for_reply` serves `request.card_corrections`: every earlier correction on this card, oldest first
  (`_card_corrections` reads the `correct` decisions); `worker.draft` shows them as STILL IN FORCE, newest wins.
- `engine._hold_corrections(task_id, draft, now_text)` runs on every correction redraft before it is saved. From each
  earlier correction's snapshot (old -> new) CODE finds sentences he removed (no close match after) and reworded
  (0.5-0.85 word overlap); if one comes back it is dropped, or swapped back to his wording. A sentence his own words
  ask for again (this or a later correction) is left alone. Sentences under 4 words (greetings, sign-offs) never
  count. Each action prints `[corrections] task N: ...`. A sentence that returns in a LATER round's draft is cleared
  (he asked for it back, card 1163's call times), and a round that changed over half the email ("redraft it
  completely") records nothing: that is a rewrite, not a list of removals; those rely on the in-force list.
  Checked on 60 real multi-correction cards: no approved draft altered; 36 of 52 injected regressions stripped
  (the rest came from full-rewrite rounds, by design).

## First response means first response; a rule goes where it applies (13 Oct 2026)
- CAUSE: owner, "whenever we add rules it always says first response drafting, even when it's not first responses".
  A taught rule lands on the skill of the card it was taught on, and almost every sales email card was filed on
  sales-first-response: in 30 days 235 reply cards there, 222 of them threaded and 86 automatic chases
  (`_spawn_followup_card` hardcoded it). So 33 of the last 48 confirmed rules went there, leaking first-reply rules
  into chases, and never reaching email-handling cards (Talk drafts, booked projects).
- CARDS: `_draft_direct_reply` files a reply on sales-first-response ONLY when we have never written to the person
  (`_we_wrote_to`: a Cortex send to them, or an email_out on one of their deals); otherwise sales-followup;
  delivery-stage deals stay on email-handling. `_spawn_followup_card`, `_spawn_lead_chase` and
  `poll_sales_replies` draft on sales-followup. Website enquiries (`intake_enquiry`) stay first response.
- `worker._RELATED_SKILLS_DEFAULT["sales-followup"]` reads email-handling, sales-scheduling, lead-qualification (NOT
  sales-first-response). A first-response card still reads sales-followup and email-handling, so a rule on either
  reaches first replies too; only first-reply rules stay on sales-first-response.
- THE MOVE (owner: "use your best logic and do the move", 13 Oct 2026): of 86 rules on sales-first-response (company
  + universal), Haiku sorted each by what it says; 41 stayed (incl. the "automated text messages" no-draft rule,
  which the no-reply check reads from there), 37 moved, 7 already existed on the target lane and were only removed.
  Before-state of every rule list touched: setting `rule_lane_move_2026_10_13` (skills + universal + each move).
- RULES: `worker.RULE_PLACES` (first reply / all sales emails after it / every email) + `rule_places(skill_key)`;
  `infer_rule` also returns `place`, chosen by what the rule says. The proposal stores the chosen skill and
  `places`; the cockpit rule card has an "Applies to" picker (`placeSel`, sent as `skill_key`), and
  `decide_rule(..., skill_key=)` lands it there (only one of the proposal's places). sw `cortex-v62`.

## WhatsApp is sorted exactly like email (13 Oct 2026)
- CAUSE: a job application (Adham Amr, card 1190) became opportunity 159. The WhatsApp lane had its own triage
  written in code, four labels (enquiry/personal/supplier/spam) and "almost every human deserves a reply", so a job
  seeker was an "enquiry"; and `_process_message` opened an opportunity for EVERYTHING not spam, never reading the
  label. None of the sales-triage or lead-qualification rules taught on email reached it.
- NOW (`whatsapp._process_message`): fold into an open card first; then `engine.classify_email` on the company's
  sales-triage rules with the email categories; client override by phone (`_open_deals_for_phone`: the number on a
  deal's contacts, or the CRM contact's email/account, this company only) and the message logged on that deal;
  CRM capture for `to_crm` categories; `policy.card_categories` + `policy.should_skip` decide whether a card exists
  (anything else = FYI notification only, nothing sent, nothing to his phone contacts); a `lead` with no deal runs
  `engine.qualify_suggest` and gets an opportunity ONLY when qualified (else the ack card + a notice). The fixed
  acknowledgement is unchanged. `_classify` is gone; `_name_from` reads a stated name only.
- WHAT WHATSAPP NEEDED ON TOP (found by a dry run over the last 25 real messages): `classify_email(channel_note=)`
  gets `_WA_CHANNEL_NOTE` (a fact: WhatsApp, people text briefly, classify the conversation); the classifier sees
  their last 24h of messages (`_recent_messages`/`_remember_message`, setting `wa_recent:<digits>`); a website-
  button reference (lptrack) makes it a lead whatever the label (ad-click text had read as spam); a bare greeting
  (`_GREETING_ONLY`) is category `greeting`, no card, the next line makes it. Leads: qualification says needs_info
  for nearly every short WhatsApp lead, so on this lane only `not_qualified` withholds the opportunity.
  Dry run result: job seekers -> recruitment, models/agencies -> freelancer/vendor, a friend -> personal, all no
  card; ad-click and real enquiries -> lead with an opportunity.

## A company can show its sister company's films; a named film always reaches the email (13 Oct 2026)
- CAUSE: card 1186 (Sky Vision, Ausra) was asked twice to include the AI drone showreel and never did. Every film in
  `media_assets` sits under Sensa (company 3); Sky Vision (4) has none, so its drafts got no media shelf and the
  invented-link guard stripped any film link. And `deck.films_in_text` only matched a CLIENT name or an exact title
  segment, so "AI drone show reel" never matched "Sensa Productions, AI drone showreel" even for Sensa.
- `deck.library_scope(company_id, named=False)` is the one definition of the films a company may show: its own plus
  profile `media_library_from` = [{company_id, categories}] (borrowed films limited to those categories; a film the
  owner NAMES may be any of the sister's). Used by the email shelf, `pick_samples`, `films_in_text`, `films_by_ids`,
  `library_slugs`, meetingprep; `playlist_for`/`playlists` fall back to the sister's playlists.
  Sky Vision: `media_library_from` = [{3, [aerial, real-estate, construction, showreel]}]. Sensa branding on those
  films is fine (owner).
- `films_in_text` also matches a title's text after the client name with spaces/punctuation removed (3+ words).
- The email shelf puts every film the owner's words on the card name (corrections + the live one + an outbound
  brief) at the top as FILMS THE OWNER NAMED, with exact links (manifest `named_films(N)`).

## One chase system; a skipped chase stops chasing; films matched by meaning (13 Oct 2026)
- CAUSE: Ekaterina (FLC Group, Sky Vision, intern@flc-me.com) got four chases with no opportunity open; three were
  rejected and the next came anyway. She qualified at MEDIUM confidence, `crm.auto_opportunity` only fired at high,
  so the send path armed the separate silence-chase list (`lead_fu:<cid>:<email>`, `run_lead_followups`), which ran
  none of the deal checks and never read a rejection. Brent (deal 93) and Boo Boo Laand (deal 139) were on BOTH
  systems at once, so chased twice.
- `crm.auto_opportunity`: a qualified verdict at ANY confidence makes the opportunity. The send path makes it (and
  logs the email, arms the deal clock) instead of arming `lead_fu`. `run_lead_followups` is a no-op ("retired");
  all `lead_fu` keys were cleared 13 Oct. Leads judged needs_info get no opportunity and so no chase.
- `_skip` -> `_stop_chasing_on_skip`: skipping an automatic chase (followup chase/checkin/followup/revive or a
  lead_chase) pauses the deal clock, logs `chase_stopped` on the timeline and notifies once. `_rearm_stalled_chases`
  skips a deal whose `chase_stopped` is newer than any email; our next email to them (`touch_followups`) restarts it.
- `deck._closest_films`: when the owner's words point at a film (`_FILM_WORDS`) and nothing matched by id, client or
  title, Haiku picks the obvious film(s) from the scoped library; code keeps only ids in that list (max 3). The email
  card manifest shows `named_films(<titles>)` so a wrong pick is visible.

## A reminder is the owner's only when it came from the owner; a closed deal clears its notices (13 Oct 2026)
- CAUSE: Liebherr (Sky Vision deal 144) went Lost on 8 Oct 08:28 and "Prepare and send the proposal" fired at
  12:27 anyway. `reminders.create` defaulted `created_by="rashad"`, so the qualify path's reminder (and the
  feedback asks) were filed as the owner's own, and `_cancel_cortex_reminders` deliberately leaves those alone.
  20 proposal reminders carried the wrong label. Also: the deal had been opened by a hand backfill on 7 Oct from a
  3 July enquiry the owner had chosen not to answer (card 183 rejected); Accor 145 and NTUC 146 likewise (all Lost).
  LESSON for backfills: check the enquiry DATE and whether the owner already declined it before creating anything.
- `reminders.create` now defaults to `created_by="cortex"`. The person is passed explicitly by the cockpit
  (`/api/reminders`), Talk `set_reminder`, and `_apply_understood` (reminders from his correction words). Automatic
  sources name themselves: cortex-qualify, cortex-feedback, cortex-ondone, cortex-commitment (plus the existing
  cortex-pipeline / cortex-nextstep). Existing rows relabelled 13 Oct.
- `crm._cancel_cortex_reminders` (every move into Lost / Dormant / Completed) also marks the deal's unread, read or
  snoozed notifications dismissed (never deleted).

## Update the deal notes, then redraft from them (13 Oct 2026)
- Owner: "update the opportunity notes, go back to the draft and ask it to recheck the latest notes and redraft".
- The deal page's "Add a note" (`crm.add_project_note`, `/api/crm/project/{id}/note`) now files a `context` event
  (with `by`), not `note`: `pipeline.deal_context` gives every context event IN FULL to every draft, where a plain
  note sat among Cortex's own bookkeeping notes and fell out of the last-12 timeline window. Talk `note_opportunity`
  and correction facts already wrote `context`.
- `_draft_context_for_reply` serves `request.new_notes`: context events newer than the card's last draft (its
  creation or last correction). `worker.draft` shows them as NEW NOTES, fact, the newest word, and authorises
  rewriting the parts they affect (overriding "keep every other sentence"). Manifest `new_notes(N)`.
- Use: add the note on the deal, then on the card say "recheck the latest notes and redraft".

## A buyer's portal invitation is a lead on the tender path (13 Oct 2026)
- CAUSE: Wasl Asset Management's eProc invitation (RFQ 2026_1170, Viya corporate festive video) and its reminder
  reached fly@skyvision.film on 12-13 Oct and were classified "vendor", as if Wasl were selling to us. Vendor is not
  a card category, so the no-draft check and `_flag_skipped_opportunity` never ran and no opportunity was tracked.
  Ayresh forwarded them to Sensa; deal 162 was created from them through `_track_tender`.
- `policy.portal_invitation(email)`: an invitation to bid/quote (`_BID_INVITE`) AND portal wording (`_VIA_PORTAL`,
  now also eProcurement, ESOP, Jaggaer, Ariba, "Submit Response", "Intend To Respond", "Username and Password").
  `poll_inbox` checks it right after classification: category lead, contact recorded, then
  `_flag_skipped_opportunity` (scope judged against the company's services; in scope -> tracked opportunity with
  closing reminder, held off the email chase). No reply card: portal bids are not answered by email.

## What a stage means is one table: `stages.py` (audit step 2a, 13 Oct 2026)
- See `docs/ARCHITECTURE-AUDIT-2026-10.md` (the plan). Every question about a deal stage is a lookup in
  `runtime/cortex/stages.py`: group (sales / won / closed), RHYTHM (default, closing, won, recurring, revival, none),
  cancel-on-entry, and whether a client's mail attaches to it. `crm.CLOSED_STAGES`, `NO_CHASE_STAGES`,
  `OPEN_SALES_STAGES`, `engine._DELIVERY_STAGES`, `pipeline.PLAN_STAGES` and the deal lookups now read it. Never write
  a stage list by hand again; add a property to the table.
- `crm._apply_stage_rhythm(p, old, new)` is the ONE place the deal clock follows a stage: no rhythm -> clock off
  (Lost, Completed, Close & review, Nurture); Quote -> closing rhythm; entering won work from sales -> the closing
  cadence is dropped for the company cadence; Recurring -> quarterly; Dormant -> soft revivals only (~90 days, twice).
  Called by `set_project_stage` and by `create_deal` for a deal born in a later stage.
- Fixed by it: Close & review replies now attach to the deal (lookups excluded it); Nurture deals no longer get deal
  check-ins on top of nurture touches; a booked deal can no longer run the closing gate (`_closing` reads the stage's
  rhythm, not a leftover cadence); Completed ends off, not manual. Migration 13 Oct: 9 Dormant deals moved from their
  old chase rhythm to revivals (before-state in setting `stage_migration_2026_10_13`).
- Health checks: `clock_on_stage_without_rhythm`, `won_deal_on_closing_rhythm` (both self-repair),
  `dormant_not_on_revivals` (report).

## One contact gate for every automatic email: `gate.py` (audit step 2b, 13 Oct 2026)
- `gate.check(company_id, email, deal_id, request)` answers "may Cortex contact this person, unprompted, now?" for
  every AUTOMATIC email (`gate.purpose`: followup chase/checkin/followup/revive/nurture/post-meeting, lead_chase, or
  `auto_source` set by `reminders._spawn_task` on a reminder Cortex created). An email a person asked for (a reply,
  Talk, the cockpit, his own reminder) is never gated.
- Checks, in order: the person (left-company, not qualified); the deal (Lost/Completed never; chases only where
  `stages.rhythm` allows, Dormant revivals only; a `chase_stopped` newer than any email); who spoke last on any
  channel we see (deal email_in/whatsapp_in, CRM inbound events vs Cortex sends and email_out); a quiet period
  after our last email (`QUIET_DAYS`: 3 for chases, 2 for reminder/promise emails, 14 for revivals and nurture).
- Applied in exactly two places: `store.create_card` (an automatic card that may not contact is not created; the
  reason goes on the deal via `gate.note_hold`) and `store._release_gate` from `promote_queued` (a queued automatic
  card is re-checked before release and cancelled with the reason if it may no longer go). The deal chase's own
  3-day check moved into the gate; a held chase no longer consumes a cadence step.

## One "they wrote to us" handler: `events.py` (audit step 2c, 13 Oct 2026)
- `events.they_wrote(company, message, deals=None, channel="email"|"whatsapp")` does what a client or lead writing
  in must do to their deal, whatever the channel: `crm.enter_closing`, `_pause_or_reschedule_followups` (pause, or
  re-arm at a date they state), `_hold_scheduled_emails`, and the log (`pipeline.record_inbound` for email: timeline +
  deadlines, deduped by Message-Id; `whatsapp_in` for WhatsApp). Deals: the ones passed, else the sender's, else a
  colleague's at the same client by domain. It never drafts and never decides whether to reply.
- Called from: `_draft_direct_reply` (BEFORE the 40-character check, so "Thanks, will review" now pauses the chase),
  the `poll_inbox` no-draft branch (a client email the policy held), `poll_sales_replies`, and WhatsApp messages from
  a deal client. Before this only `_draft_direct_reply` did any of it.

## What Cortex owes completes itself: `obligations.py` (audit step 2d, 13 Oct 2026)
- `obligations.KINDS` names every kind of reminder Cortex sets for something it owes (by title prefix or creator)
  and what completes it. Settled only for reminders Cortex created; the owner's own are his.
- `on_sent` (from `pipeline.record_send`, every email out on a deal): a quotation or proposal (attachment name or
  subject) settles "Prepare and send the proposal"; then ONE Haiku judgement settles commitments, meeting
  commitments, client deadlines, next steps and conversation commitments the email actually delivers. It replaced
  `pipeline.settle_commitments`, which only knew two kinds and skipped snoozed ones.
- `on_stage` (from `crm.set_project_stage`): leaving Opportunity settles "send the proposal"; won or Lost settles
  "Tender closes"; notices asking about the stage (closing-gate, signed-off) are dismissed.
- `on_task_closed` (from `store.update_task` when a card is sent/done/cancelled/rejected/failed): notices about that
  card are dismissed. `on_contact_added` / `on_deal_for_contact` (from `crm.add_deal_contact`): "no contact" and
  "untracked sales email" notices are dismissed.
- `reminders.create` merges a same-day reminder only with one from the SAME author and neither carrying an action.
- Health check `notice_for_closed_card` (self-repairs).

## One intake, batch 1 (audit step 2e, 13 Oct 2026)
- Web forms (`intake_enquiry`): a sender on a live deal (their own or a colleague's by domain) is a CLIENT: the deal
  hears it (`events.they_wrote`), the card drafts on the deal's lane with the deal on it, no qualification. A new
  lead's card now carries the opportunity `auto_opportunity` made for it (it was dropped) and joins the conveyor.
- `policy.should_skip` reads the compiled no-draft policy of EVERY email lane (`policy.NO_DRAFT_LANES`:
  sales-first-response, sales-followup, email-handling; WhatsApp adds social-dm-replies). It read first response
  only, so no-draft rules moved off it on 13 Oct were never evaluated.
- `poll_sales_replies` claims each message by Message-Id like `poll_inbox` (one email, one handling) and puts the
  deal on its card. Post-meeting follow-ups draft on sales-followup. The WhatsApp runner path ignores the owner's own
  number. `qualify_suggest` reads the EFFECTIVE lead-qualification rules (universal + local).

## One intake, batch 2 (audit step 2e, 13 Oct 2026)
- `store.create_card`: ONE OPEN PREP CARD PER DEAL AND PURPOSE (`prep_action` + `proforma_stage`): a second request
  returns the open card (quotation prep from a correction and from the next-step reader; balance invoices).
  The balance-invoice card is also skipped when the final stage's pro forma already exists (`proforma.for_deal`).
- `_spawn_recurring_child`: the clock never stacks an occurrence on an open one; `run_template_now` forces.
- LinkedIn DMs: `social_dm._classify_dm` uses `engine.classify_email` (same labels as email/WhatsApp); the REPLY rule
  stays LinkedIn's own by the owner's choice (answer every real person; skip spam/automated/marketing only).
- WhatsApp opportunities: the card's `deal_id` column is set; the organisation the qualification names is linked
  (`crm.get_or_create_account`) and its estimate stamped (`crm.stamp_estimate`, only when the deal has no value).

## Tidy-up (audit step 2e, 13 Oct 2026)
- ONE definition of "this address is us": `identity.internal_index()` / `identity.is_internal()` (own company domains,
  every connected mailbox's domain, own_sending_domains, internal_people, the newsletter test group). A FREE-MAIL
  mailbox (the owner's rashadalsafar@gmail.com) adds only that address, never gmail.com. `engine._internal_index`,
  `engine._is_internal` and `pipeline._own_domains` delegate to it.
- `poll_inbox` reads an auto-reply ("I have left, write to X") whoever sends it, before classification; it was only
  reached inside `_draft_direct_reply`. The "Unanswered" notice carries its company (team members saw others').
  The next-step reader dedups on the Message-Id. The manual recovery reads replies on enquiry threads.
- NOT unified (deliberately left): the three contact writers (`add_inbound_contact`, `add_inquiry`,
  `match_or_add_by_phone`) still differ in stage/source wording; behaviour is consistent enough, and merging them is
  data-shaping work best done with the per-business CRM layer.

## Rules audit (Phase 3, 13 Oct 2026)
- 113 Sonnet reviews over every company skill, the universal rules and each company's email lanes produced 120
  findings (`/opt/cortex-knowledge/audits/rules_audit_2026-10-13.json`). Applied, with the owner's answers to 16
  questions: 102 changes (60 edits, 42 deletions: duplicates and the extras absorbed into merged rules); company rules
  406 -> 394, universal 201 -> 186. Before-state of every rule list: setting `rules_audit_backup_2026_10_13`.
- Rejected on review: all 18 "move to another skill" proposals (the reviewer assumed every rule is about email and
  named skills that do not exist) and every CROSS-LANE duplicate deletion: lanes do not all read each other
  (email-handling cards read neither sales lane), so the same rule on two lanes is deliberate.
- Owner decisions now in the rules: Tabscanner Pro is a customisation add-on, a call is offered only when a lead may
  need it (no price in the rule); every blog post has the hero plus at least one in-content image; Sensa signs off
  "Best regards," (the universal sign-off rule now says one sign-off line, signature appended); no markdown/bold;
  greetings mirror theirs; small projects still get a meeting to walk through a new proposal; quotation labels are
  generic, descriptions specific; Sensa case studies stay a monthly blog category (the universal "case studies are
  Pages" rule is OVERRIDDEN for Sensa, `skills.overrides`, exact-text match: re-add the override if that rule is ever
  edited); FilmSpoke blogs visually rich within the design rules; newsletter test sends at the quarterly
  pre-approval for every company; Sky Vision service line "aerial photography and drone filming"; up to three
  sample films per email.
- Script: `rules_apply.py` (scratch) applies by exact text, never by index, so earlier edits cannot shift later ones.

## Sister brands share tender deals (13 Oct 2026)
- CAUSE: Wasl's RFQ 2026_1170 re-sent to fly@skyvision.film opened Sky Vision deal 163 next to Sensa deal 162: every
  deal lookup searched only the brand whose inbox received the mail, and Sensa / Sky Vision / FilmSpoke are one legal
  entity that buyers invite under either name.
- `crm.sister_slugs(slug)` (setting `entity_groups`, default sensa/skyvision/filmspoke), `crm.tender_code(subject)`
  (the buyer's reference: the first code/RFQ/RFP/tender/ref or #-token containing a digit) and
  `crm.find_tender_deal(slug, sender, subject)`: the open sales deal across the sister brands carrying that code in its
  title or record, else one whose contacts include the sender. `_track_tender` uses it before creating anything.
  `closing_reminder` never adds a second reminder for the same deal and date. Health check
  `same_contact_two_sales_deals_sister_brands` (report).

## Remaining map items closed (13 Oct 2026)
- A MEETING IS CONTACT: `gate.check` holds an automatic email when we met the person (meeting_notes attendees) inside
  the quiet window; `pipeline.record_meeting` restarts the deal's chase clock (`touch_followups`) for live meetings
  (never for a backfill).
- A scheduled email re-checks its recipient when it fires (left-company / not qualified) as well as the deal stage,
  and is parked for the owner, never sent blind.
- "Tender worth a look" is raised after the tender is tracked and points at its deal, so it clears when the deal
  closes. Meeting notes that arrive by email draft the post-meeting follow-up too (it dedups on an email since the
  meeting). Closing a deal also cancels an unsent project-plan card raised from a reminder.

## The notification policy (owner, 13 Oct 2026)
- Audit: 454 notices in 30 days (~15 a day, 4.5 per opportunity, Merck 23); ~125 were contact-captured FYIs, ~55
  pointed at cards already in the Inbox, next steps were announced twice, one tender produced four.
- `notifications.notify` applies ONE policy (`_treatment`): KEEP (needs the owner, no card) with ONE live notice per
  deal and topic (`dedup_key deal:<id>:<topic>`; a different earlier notice is kept as a line in `items`); COVERED (a
  notice about a card already open in the Inbox: phone ping via `push_only`, no row); DIGEST (`DIGEST_PREFIXES` /
  `DIGEST_CATEGORIES`: contacts captured, filed messages, new opportunities, robot no-replies) rolled into ONE
  "Today's updates" notice per company per day, each as an item. Critical/security always KEEP.
- At the source: a next step is announced once (when its reminder fires; "Next step (reminder)" at creation and "Next
  step queued" removed); tender deals never raise "Unanswered" (`_rearm_stalled_chases`) or "Not drafted"; a re-sent
  tender for a tracked deal is logged, not announced; our reply on a deal clears its "Unanswered" / "Not drafted" /
  "replied: no ..." notices (`obligations.on_reply_sent`). Health check `unanswered_after_our_reply` (self-repairs).
- Meeting brief CARDS are kept (owner finds them useful); only the duplicate notice pointing at the card is gone.

## Which deal, never guessed; forward to Cortex; own businesses internal; email links (13 Oct 2026)
- NEVER GUESS WHICH DEAL. MAH Gold: Mai is the contact on three deals; the sent sweep took `deals[0]`, so her video
  emails were filed on the SSD-assignment quotation deal (112) and the quote check read her video sign-off as "they
  accepted" the quote. Now `engine._deal_for_thread` tries, in order: the owner's answer bound to the thread
  (setting `thread_deal:<thread or message id>`), a quote number (SEN-YYYY-NNNN) in the subject/body, the thread id,
  the References chain, the subject. When nothing matches and the contact has several open deals,
  `events.ask_which_deal` raises ONE small "Which deal is this?" card (request.kind `deal_pick`, numbered options);
  the owner replies with the number (`events.answer_which_deal` via `apply_correction`), the thread is bound, the
  message is filed (they_wrote / email_out_manual). Approve on that card is blocked (it needs the answer).
  Used by `events.they_wrote` and `pipeline._sweep_mailbox` (no more first-deal pick).
- `_closing_gate` only reads a client message the matcher places on THIS deal. A gate pause (declined, on hold,
  accepted, signed off) logs `chase_paused`; `gate.check` and `_rearm_stalled_chases` respect it like
  `chase_stopped` (the 13 Oct pause on deal 112 was re-armed 26 minutes later before this).
- FORWARD TO CORTEX: a forward from one of us of an email from outside (`_forwarded_original`: Gmail/Outlook/Apple
  forward markers, original From/Subject/body, the forwarder's note kept as context) is handled as that email, unless
  it already reached us (`_seen_original`: same sender + subject on a card or deal timeline in 45 days).
- Every business in Cortex is "us": `identity.internal_index` adds each owned/personal company profile's
  `domains` (two-label domains only). Flixton Manor's profile domain set to flixtonmanor.com (card 1211 drafted Gino
  replying to the owner's own forward).
- `engine._linkify` (outgoing email HTML) leaves trailing punctuation out of links: "(https://meet.google.com/x)."
  linked to "x)." (card 1208), a dead link for the client.
- `bin/deploy.sh` runs `pyflakes` for undefined names before pushing.

## WhatsApp is a door into Talk (owner, 13 Oct 2026)
- The owner's own messages to the business WhatsApp were a control channel only (buttons, Edit instructions, a
  doorbell); a screenshot he sent of a client was dropped silently. Now `whatsapp._owner_control`: after the buttons
  and a pending Edit, a picture/document (`_owner_media`, caption read as text) or anything typed or spoken longer
  than three words goes to TALK (`api.talk_from_whatsapp`, the same `_chat_prepare` + tools as the cockpit, the
  owner's scope), on a thread; the reply comes back on WhatsApp (`_tell`, split at 3,500 characters). The
  conversation rolls on one "WhatsApp" chat in Talk's history (setting `wa_talk_conv`) and starts fresh after a day
  of quiet. A short "ok"/"hi" still rings the doorbell for waiting cards.

## Meeting notes: the full document, one record, the waiting draft follows it (13 Oct 2026)
- CAUSE: card 1212 (Shama Ramadan follow-up) was drafted from Gemini's notes EMAIL two seconds after it landed. The
  email carries a short summary; the Google Doc it links to carries the details and the transcript, where "Thursday
  11:30 at Galleria Mall, Farheen confirms and sends the pin" was. The calendar sweep then stored that doc as a SECOND
  row nobody read. Every meeting since September was stored twice (rows 24/25, 26/27, 28/29, 30/31, 32/33).
- `meetnotes.sweep_email` now finds the doc from the email's button (`_doc_in_email`: the plain part has no URL, the
  HTML anchor does), reads it (`_full_notes`, hello@'s drive.readonly), distils from it and stores the row under the
  DOC id, so the calendar sweep's `file_id` check recognises it. A doc not readable yet: the email waits
  (`FULL_NOTES_WAIT_MIN` = 30) and only then falls back to the email text, stored under the Gmail id.
- `sweep` (calendar): a same-title, same-Dubai-day row with no doc behind it (`_short_copy_of`, `_is_doc_id`) is
  UPGRADED (`_upgrade_notes`: re-distil, full text kept on the row and the deal, commitments recorded), never twinned.
- `_refresh_followup` puts the new notes in the waiting post-meeting card's brief and `engine.refresh_from_notes`
  redrafts it as the SYSTEM (no owner decision, no rule inferred, `_hold_corrections` keeps his corrections). The
  resumed-session path uses it too; it used to call `apply_correction`, which filed the redraft as his correction.
- Attendees on Gmail are clients: meetnotes filtered on NON_CLIENT_DOMAINS (gmail.com counts as ours), so Luna
  Mansour and iTellu's Gmail addresses vanished from the Learning App meeting; it now uses `identity.is_internal`.
- A meeting the owner LINKED to a deal (`link_meeting`) files there even when no attendee is on the deal yet
  (`_linked_deal`), and the link's day is the meeting's DUBAI day (a date-only subject is 20:00 UTC the day before).
- `_identity_mismatch` also catches the sender named as an object ("discussed with you and Rashad", sent from rashad@).
- Health check `meeting_stored_twice` (report). Distil input raised to 40,000 characters (the doc carries the transcript).

## Talk edits opportunities: update_opportunity (13 Oct 2026)
- CAUSE: asked to rename deal 156 and add "the contacts who were in the meeting today at 5pm", Talk had no tool to
  rename a deal or attach anyone, asked the owner for emails the calendar invite already held, and lost the iTellu
  brief PDF sent on WhatsApp (only the words of a WhatsApp turn are kept in the chat).
- `update_opportunity(deal_id|search, title, value, currency, stage, organisation, add_contacts, meeting, documents)`
  (`api._update_opportunity`): `meeting={date,time,title}` reads the invite (`calendar.events_between`, past or
  future; private "Prep:" blocks skipped), attaches every attendee who is not us (created in the CRM if new) and
  links the meeting so its notes file on the deal. `documents` files library rows on it. Every change is logged.
- `talk_from_whatsapp` saves a non-image file to the library the moment it arrives and tells Talk its id.

## Personal touch: WhatsApp and calls beside the email track (owner, 13 Oct 2026)
- Owner: "a lot of merit in me asking these people how they're doing, whether they want to hook up for a coffee,
  trying to get relationships on WhatsApp and calls." `runtime/cortex/touch.py`. Cortex never messages or calls the
  client; it makes sure the owner does and keeps the deal straight when he has. Companies: setting `touch_companies`
  (default sensa, skyvision).
- START (`touch.start`, idempotent per deal, setting `touch:<deal_id>`): wherever a new lead becomes an opportunity
  (`engine._start_touch` at all four `auto_opportunity` sites: direct email, website form, a lead answered, a reply
  thread) and, for WhatsApp, when the acknowledgement is sent (`_push_wa_contact`). The number comes from the
  signature or form by CODE (`numbers_in`/`pick`, Google's libphonenumber, region AE; text below a quoted reply is
  ignored; toll-free/premium dropped; a labelled or genuine mobile wins, a landline is call-only), else the CRM or the
  deal contacts. Saved on `crm_master.phone` and the deal contact ONLY where none is held. Pushed to his personal
  Google Contacts via `contacts.add_enquiry` ("<Name> (Sensa enquiry, 13 Oct)", ledger-deduped). First reminder now.
- REMINDERS: `created_by='cortex-touch'`, one pending per deal. `reminders.fire` hands them to `touch.fire`: a notice
  "WhatsApp or call <name>, <org>" with the opportunity, his last note, `https://wa.me/<digits>` and `tel:+<digits>`
  (the cockpit's `linky` now renders tel: as a Call link; sw cortex-v63), then the next touch after
  `rhythm()` days (profile `touch_rhythm`, default 7, 21, then 30 repeating).
- HIS NOTES DRIVE IT: `crm.add_project_note` (deal page) and Talk `note_opportunity` call `touch.on_note`: ONE Haiku
  question (did contact happen; the words of a stated next contact), code resolves the date
  (`engine._resolve_their_date`) and moves the pending touch there; contact made = `touch_made` on the timeline,
  which `gate.check` counts as contact (no email chase inside the quiet window after a call he logged).
- ENDS: leaving the sales stages cancels the pending touch (`obligations.on_stage`); a closed deal cancels it like
  every Cortex reminder; `touch.fire` never fires on a deal outside Opportunity/Quote.
- WEEKLY (owner, 13 Oct 2026: "if these guys are opportunities, I should be checking in with them once a week"):
  profile `touch_rhythm` = [7] on Sensa and Sky Vision. NO NUMBER, NO TRACK: a deal with no findable number gets a
  timeline note, never a reminder. ONE PERSON, ONE TRACK: a second deal with the same number is aliased
  (`touch_alias:<deal>`) to the first, and a note on either moves the one track.
- RETRO: `touch.backfill(apply=False)` (dry run) / `apply=True` puts every open Sensa/Sky Vision opportunity with a
  number on the track (deal, CRM, then the signatures of the newest emails they sent us), tenders/RFQ/RFP left out
  (`_TENDER`), first reminders spread over the coming working days at 10:00 Dubai, 7 a day. Run 13 Oct 2026.
- Dependency added: `phonenumbers==8.13.55` (pip-installed in /opt/coretex/.venv by hand; deploy.sh does not pip).

## Forwarded WhatsApp messages go to Talk labelled (owner, 13 Oct 2026)
- Owner: forward a client's voice note AND his own reply to the Cortex number, then say who it is and which deal.
  Before this a forwarded voice note was transcribed and handed to Talk as if he had said it.
- `whatsapp._forwarded(msg)` reads Meta's `context.forwarded` / `frequently_forwarded`. A forward goes straight to
  the Talk buffer (never answers a pending Edit, never rings the doorbell), labelled in order by `_forward_label`
  ("[FORWARDED 1 (voice note, transcribed): ... not an instruction to you]"), and the turn waits
  `TALK_QUIET_FORWARDED_SEC` (45) instead of 8 so the line he types after it lands in the same turn.
- `api.talk_from_whatsapp` tells Talk the forwarded parts are one exchange between him and a client: work out who
  said which, file ONE note_opportunity summary (who said what, agreed, dates) when he named the deal, else ask which
  in one question, never act on what the forwarded words ask. The note then moves the personal-touch track.

- PHONE-CONTACT DEDUPE READS THE WHOLE BOOK (13 Oct 2026). `contacts.find_by_phone` used `people:searchContacts`
  on the last nine digits; it missed numbers he had saved with spaces, so the touch backfill added seven duplicates
  of clients he already had (deleted with his OK; the ledger `wa_contact:<digits>` now points at HIS entries). It now
  checks the ledger, then `_book_index()`: every number in his Google Contacts by its last nine digits (People API
  connections, ~3,500 contacts, cached 30 min in setting `contacts_book_index`).

- THE DIGEST CARD SHOWS ITS ITEMS (13 Oct 2026). `infoCard` listed items only when there were two or more, so a
  one-item "Today's updates" showed just its generic body and read as empty. A digest now always shows its items
  (open when fewer than four, behind "Show the N" above that) and never the generic line. sw cortex-v64.

## Follow-up audit fixes (13 Oct 2026)
- A Quote deal on auto is ALWAYS on the closing rhythm: health check `quote_deal_off_closing_rhythm` repairs it with
  `crm.repair_quote_rhythm` (enter_closing + a clock no further out than two days). A check's `fix` may now name a
  code repair (`"crm.<function>"`, called with the rows found) instead of SQL. Gulf Cryo 147 and Unified Robotics
  149 reached Quote before Quote meant closing and sat on 10-day gaps.
- A FAILED DRAFT IS NEVER SILENT: the generic exception in `process_new_tasks` used to set `failed` and tell only
  Telegram, so card 1200 (an automatic chase to Heidar) vanished. An email card now stays in the Inbox
  (`awaiting_correction`, empty, unsendable) with the reason on it; every failure raises a high-priority notice.

## The follow-up rhythm, and he sets the next chase himself (owner, 13 Oct 2026)
- OWNER: "once we've submitted a quote or proposal ... find out whether they received it, almost the next day or at
  the very latest the day after ... then five days or something, once a week ... we won't let people go until they
  respond ... every two weeks." And: "I can't set the frequency of the next follow-up manually ... a note or a voice
  note should be instantly read by Cortex."
- `crm.CLOSING_CADENCE` (Quote, working days): 1 (the RECEIPT CHECK, only on entering Quote), 5 x8 (weekly, about two
  months), then 10 x26 (every two weeks until they answer). `enter_closing` (their reply) restarts at step 1, never
  the receipt check, and never touches an `owner` cadence. `crm.DEFAULT_CADENCE` (Opportunity): 4 chases 3 days
  apart, then every 14 days x26 (the 90-day revivals are gone). `stages.REVIVAL_CADENCE` (Dormant): every 14 days x26.
- The two waits let the receipt check through: `_closing_gate` needs 1 working day since our email for step 0 and 2
  otherwise; `gate.check` uses 0.8 days for a chase on a closing-rhythm deal (the closing gate holds the rest).
- HE SETS IT: `crm.set_next_chase(deal, when, every_days, by)` (deal goes on auto; `every_days` = a per-deal `owner`
  cadence). Deal page "Set next chase" (`POST /api/crm/project/{id}/next-chase`, words via `reminders.parse_when`);
  his notes (`touch.on_note` now also reads `channel` and `every_days`: an email/any next step moves the email chase,
  a call/WhatsApp one the touch track); Talk `update_opportunity(next_chase, chase_every_days)`.
- Migration 13 Oct: open deals moved onto the new rhythm; client- or owner-stated dates kept (Brent 93 end of year,
  Dawn Christine 103 Jan 2027, Bioscience 113 Mar 2027, Noon 1 quarterly nurture). Honor 120 and Merkle 98 set Lost
  (owner).

## An offer is never a commitment; "not needed" closes the whole chain (14 Oct 2026)
- CAUSE (MENA Water deal 155, cards 1220/1221): our 12 Oct email said "We would be glad to quote that route
  separately alongside this one". `extract_commitments` filed it as "Commitment owed: provide separate quotation",
  although its prompt already said an offer is not a commitment. From that ONE misreading: reminder 338, a
  quotation-prep card from the next-step reader (1221), and "As committed, we are finalising the separate quotation"
  in the next reply (1220, via the owed shelf). Separately, our own words quoted under his reply ("approvals at each
  gate") were logged as HIS deadline. The root cause was trusting the model's judgement of offer vs promise.
- SOURCE FIX, CODE DECIDES: the extractor now returns the exact `quote`; `pipeline._in_email` drops any commitment
  whose sentence is not in the email; `pipeline._is_offer` (`_OFFER`: "would be glad", "happy to", "if you'd like",
  "remains an option", "we could", "on request", ...) decides offer vs promise. An offer is logged on the deal as
  context ("Offered, not promised"), owes nothing, sets no reminder, never reaches the owed shelf.
- THEIR NEW WORDS ONLY: `gmail.new_part(body)` cuts the quoted earlier messages; `record_inbound` mines deadlines from
  it and `suggest_next_step` judges it (and is told an earlier offer is owed only if their email asks for it).
- NOT NEEDED CLOSES THE CHAIN: `obligations.cancel_matching(deal, words, why)` cancels the matching Cortex reminders,
  VOIDS the timeline entries that asserted it (commitment / next_step / deadline: kept, no longer read into drafts),
  cancels the matching open prep cards and logs `commitment_cancelled`. Matching (`_same`) needs three meaningful
  shared words. Called from: `_skip` on a prep card (`_cancel_owed_work`); "cancel / not needed / we didn't commit"
  on a prep card (`_cancels_work`: words, then one narrow Haiku yes/no, so "no need for the VO line" still builds the
  quote); and the new `retract` channel of `_understand_correction` on any email card.

## The quotation flow from any card or note (owner, 14 Oct 2026)
- OWNER: "talk directly to the draft, and the next stage is a quotation for me to look at ... once it's approved it
  automatically redrafts ... if he wants two rounds and 50/50 I speak to the next draft and the same process happens
  ... scrap the old quotation and build a brand new one directly from talking to the cards." Also: every quotation
  card must be APPROVED (stand-alone ones only said "Mark as seen", and approving controlled nothing).
- `engine._quote_intent(text)`: `_QUOTE_HINT` regex first (no model call for ordinary corrections), then Haiku reads
  {mode revise|scrap|new_number, target_net, vat_inclusive, revision_rounds, payment [{pct, when}], changes}; CODE
  keeps a figure only if he wrote it (`_stated_numbers`), rounds 1-5 only if said, a split's last part is the
  remainder. A client's ask he has not accepted is not a decision.
- `build_quote_from_words(company, deal, text, intent, for_email)`: the deal's quotation (`_deal_quote`: latest card's
  number, any status) gets its next VERSION (a new number only for new_number). Only rounds/split -> `reissue_quotation`
  copies the lines exactly. Otherwise `_spec_from_words`: the model re-scopes from the previous version + deal record
  and lists `trimmed`; CODE prices every line (`ratecard.price_lines`, his figure as the target, Budget tier when
  needed) and, if it cannot reach the figure, the model is told the priced total once and tries again. Never a
  discount line. What was trimmed / assumed is shown to him on the card (`owner_notes`), never on the document.
- `quotation.set_rounds` / `split_lines` / `set_split_terms`: revision rounds restated on every line, deliverable,
  note and terms clause (grammar fixed for one round); an agreed split printed as the payment lines (must add to 100)
  and the terms' "70% down payment" restated. Both recorded on the version spec (`revision_rounds`, `payment`) so a
  reissue keeps them; the pro forma reads the printed lines as before.
- Hooks: `apply_correction` on an EMAIL card with a deal, or a QUOTATION card not from a proposal, runs `_quote_intent`
  first; a decision builds the quotation, the email card waits (`waiting_quote`, `pending_instruction`, card_problem
  says so, `approve_task` refuses it). `_approve_quotation_card` -> `_quote_to_email` for every quotation not from a
  proposal: attach to `for_email` (else the deal's open email, else a NEW cover email to the deal contact), supersede
  older library versions of the number, redraft the email with his pending instruction (`apply_correction(...,
  _quote_checked=True, infer=False)`). Skipping a quotation releases the email waiting for it.
- NOTES REDIRECT THE DEAL: `crm.add_project_note` and Talk `note_opportunity` call `engine.note_redirect` (on a
  thread): the newest open email card on the deal is redrafted from the note (and a quotation decision in it builds the
  quotation first), else a quotation decision builds the quotation on its own. `apply_correction(infer=False)` = a
  redraft that is not him teaching a rule. Cockpit: every quotation card shows "Approve quotation" (sw cortex-v66).

## No opportunity without a contact; a quotation always belongs to one (owner, 14 Oct 2026)
- CAUSE: Gino asked Talk for the Merck Abu Dhabi Future Health Summit quotation. Deal 161 was opened with NO contact
  (approving its quotation could email nobody, nothing could chase it) and a second quotation came out under a NEW
  number (SEN-2026-0039 beside SEN-2026-0037 v3). Owner: "without a contact on an opportunity, it's pointless ... you
  shouldn't be able to save an opportunity without a contact"; Talk must ask "who is the designated contact".
- `crm.create_deal(..., contact={name, email | phone, role})` refuses without a valid one (`crm.valid_contact`: an
  email address, or a phone of 8+ digits for WhatsApp-only leads) with `DealNeedsContact`, and puts the person on the
  deal itself (primary; a phone-only person by name and number). Every path passes it: qualify, tenders (the issuer),
  WhatsApp (name + number), the cockpit form (new Contact name / email / WhatsApp fields, checked before saving),
  Talk `create_deal` (contact_name/email/phone; without them it returns "ask who the designated contact is").
- Talk `create_quotation`: no deal_id -> the client account's one open opportunity, else it refuses and says to
  create the opportunity first (with its contact); a deal with a quotation -> the next VERSION of that number.
  `update_opportunity(documents=)` filing a quotation PDF also puts that quotation's CARD on the deal.
- Health check `opportunity_without_contact` (report). sw cortex-v67.

## One conversation, one draft, kept current: `conversation.py` (owner, 14 Oct 2026)
- CAUSE: card 1219 (our DSO submission to Alex) was waiting when Alex replied on the same thread; the reply became TWO
  cards (1222 from the inbox sweep, 1223 from the sales-reply sweep), both queued behind 1219 and both about to answer
  "looking forward to your proposal" after he had it. Every event made its own card and nothing reconciled the
  opportunity's waiting cards; the old supersede only covered reply cards, not outbound drafts, queued cards or other
  people on the deal. Owner: "when new things come in classified against an opportunity, readjust the entire inbox".
- A CONVERSATION = PERSON + THREAD. `conversation.absorb(company, request, deal)` runs in `store.create_card` for every
  inbound reply card: the same person's open draft (reply OR outbound, waiting, queued or new) on the same conversation
  (`same_thread`: thread id, our message in their References, or the same subject; else `_same_matter`, one Haiku
  question for a NEW thread) becomes the reply to the new message: their message lands in the brief as THEIR NEWEST
  MESSAGE, the thread moves to it, sender stays sticky, the card goes back to drafting (corrections in force) and shows
  `updated_for`. No second card. `_draft_direct_reply`'s reply-only supersede is gone (identical-copy check kept).
- `conversation.thread_key` = person|subject is the queue key for inbound cards, so two genuinely separate
  conversations with one person are both live; chases keep the person key (they wait behind any open conversation).
- `conversation.recheck_deal` (from `events.they_wrote`, every channel, on a thread): every OTHER open card on the
  opportunity (emails to other people, quotation and prep cards), judged once (Haiku, conservative): cancel (covered,
  reason logged) / redraft (emails: back to drafting with the new message as NEW ON THE OPPORTUNITY) / for quotation and
  prep cards a card_problem + notice instead of a rebuild / keep.
- THE DUPLICATE: `_claim_mail` let "the claimant retrying" through on the mailbox copy alone, so the sales-reply sweep
  reading the same copy passed. Claims now carry a `handler` (inbox / sales) and only the same sweep retries;
  `poll_sales_replies`' open-card check counts queued cards and email_draft; `_rejudge_pending_replies` (after a send)
  judges queued cards too.

## Quotation options: Option A and Option B (owner, 14 Oct 2026)
- OWNER: "sometimes we're giving them two different price options ... make it like an option A and option B."
  Before this a second quotation on a deal was forced into the next VERSION of the first (the Merck guard), the deal
  value swung to whichever was sent last, each approval redrafted the email alone, and nothing closed the unchosen one.
- An option = its own quotation NUMBER on the deal, the card's `request.option` ("A", "B"...), the document titled
  "Option B: ..." (`engine._option_title`). `engine.deal_options(deal)` = the live options, A first (latest card per
  number). `_prepare_option(deal)`: the first time a second option is added, the existing quotation becomes Option A
  (card labelled; reissued with the "Option A:" title when its lines are stored) and the new one is B; later ones C...
- HOW HE ADDS ONE: `_quote_intent` mode "option" ("add a second option at 18,000 without the voice over") on any email or
  quotation card or note -> `build_quote_from_words` builds it FROM the current quotation (his words say how it differs)
  under a new number; "on option B, ..." (`intent.option`) revises that option's own number. Talk:
  `create_quotation(option=true)`.
- `_quote_to_email`: approving one option while another is still open only notes it (the email's `waiting_quote` moves
  to the option still to approve); when every option is approved, ALL their PDFs are attached together and the email is
  redrafted to present them as options, each net stated by code from the version registry.
- Deal value (`pipeline.record_quotation_sent`): every option number on the send is logged as `quotation_sent` (so a
  signed copy of any of them matches); the value is Option A's, the timeline line states each option and the range.
- Words-led proposal decks: `restamp_proposal_price` uses `deck.investment_from_options` (one row per option, headline
  "From AED <lowest> + VAT") when the deal has options; an option's number finds the deal's deck.
- `_confirm_signed`: the client signed one -> every other option's open card is cancelled and its library files
  retired, logged on the deal.
- Creative decks list options too (owner, 14 Oct 2026): `creative.adopt_quotation` sets `quote.options` when the deal
  has more than one, the deck keeps Option A's number, `Job.quote_rows` gives one row per option (its blocks as the
  lines, each net from that option's live version) and the investment headline reads "From AED <lowest>".
  `restamp_proposal_price` finds the deal's creative or words-led deck from any option's number.

## An open cockpit never runs old code; it is an opportunity, not a deal (owner, 14 Oct 2026)
- CAUSE: the contact rule and the new form (contact fields) deployed at 09:23 Dubai; Gino's app had been open since
  before, kept the old form in memory (the service worker and no-store only help on a LOAD), and five creates at
  10:48 were refused with "needs a designated contact" on a screen with no field for one. Not permissions.
- `web/index.html` carries `const APP_BUILD = 'cortex-vNN'`, EQUAL to `sw.js` CACHE (a regress case checks it: bump
  both together). `api()` sends it as `X-Cortex-Build`; the server reads the constant from index.html at start
  (`api.cockpit_build`), serves it at `GET /api/version`, and `_refuse_stale_cockpit` answers any WRITE from another
  build with 409 `stale_build` (the app shows "A new version of Cortex is ready" + Reload). `checkBuild(true)` on
  returning to the app reloads at once when nothing is being typed; every 3 minutes while open it offers the banner.
  Only the cockpit sends the header, so fitness, media, runners and webhooks are untouched.
- `/api/crm/deals/new`: a body with NO contact keys at all is a pre-14-Oct form, answered "This screen is out of
  date ... close Cortex fully and reopen it", never the contact rule.
- Wording: New opportunity / Opportunity name / Create opportunity (New project on the Projects screen, stage Booked);
  Talk's tool is `create_opportunity` (old `create_deal` still executes for old conversations) and Talk says
  opportunity, never deal.

## The team talks to Cortex on WhatsApp too (owner, 14 Oct 2026)
- Setting `wa_team_numbers` = {number: user email} (Gino: +971588300854 -> gino@sensa.digital; Ayresh: +971547886009 ->
  ayresh@sensa.digital, added 15 Oct 2026). `whatsapp.team_member`
  matches on the last 9 digits and returns the active `users` row; a message from that number is never triaged as an
  enquiry: `_team_message` sends it (typed, voice note, picture, file, forwards) to Talk via
  `api.talk_from_whatsapp(user=)`, which runs with THAT user's scope (same as their cockpit: their companies, no
  universal rules, money owner-only), on their own rolling chat (`wa_talk_conv:<email>`, "WhatsApp (Gino)"), and the
  answer goes back to their phone (`_tell(to=)`). Talk buffers are per sender (`_TALK_BUFS`), so two people's turns
  never merge. Approve / Edit / Skip buttons stay the owner's: a team member tapping one is told to use the app.
  Add a person = add their number to the setting; no deploy.

## Ayresh handles her own mail by hand; big files sent to Talk are read, never dropped (owner, 14 Oct 2026)
- MANUAL PEOPLE: profile key `manual_people` = [emails] (Sensa + Sky Vision: ayresh@sensa.digital). CAUSE: mail she
  deals with kept coming back as Gino's draft (card 1233, D-Marin permit thread: the reply took the last sender to
  that CONTACT, Gino). `engine.handled_manually(co, e, rt_key, client)`: the email is theirs when they are on its To
  line, when they were the last of us to write on its thread (`gmail.thread_from_lines`, metadata only), or when
  they own the deal (`crm_projects.owner`). `poll_inbox` and `poll_sales_replies` then run `_manual_hold`: the deal
  still hears it (`events.they_wrote`: clock paused, scheduled emails held, logged) and a note says it is handled by
  hand; NO card. `gate.check` -> `_manual_owner`: no automatic email (chase, reminder email) on a deal they own,
  from them, or to a contact they last emailed. Remove the address from the profile to hand it back to Cortex.
- FILES TO TALK: `_image_blocks` used to DROP anything over 10MB without a word (a 21MB brief forwarded on WhatsApp,
  13 Oct: Talk said it could not read it). A PDF over 10MB or 100 pages now goes in as its extracted TEXT, labelled
  as such (scans: told plainly, ask for a smaller copy); an oversized image is named as too large. WhatsApp files
  save to the library up to 40MB (`documents.save(cap=)`), and a file that cannot be saved is said so in the turn.

## Invites raise no notice; the digest arrives at 18:00 (owner, 14 Oct 2026)
- Farheen's calendar invitation and its update each raised "Not drafted" (keyed per message). `engine._no_reply_mail`
  (calendar subjects, a text/calendar or .ics attachment, the bulk/auto markers) -> a skipped client email of that
  kind is logged on the deal with NO notice; a real skipped client email notifies once per deal per day
  (`skipped-client:<deal>:<day>`).
- "Today's updates" is created SNOOZED until 18:00 Dubai (`notifications.digest_slot`, `DIGEST_HOUR`) and collects
  every for-information item while it waits; it appears in the Inbox once at 18:00. Items after 18:00 go into the
  next day's digest. Change the hour in `DIGEST_HOUR`.

## "Which opportunity?" is answerable; invites never ask; a booked meeting holds chases (owner, 14 Oct 2026)
- CAUSE: the 13 Oct "never guess which deal" card (`events.ask_which_deal`) shipped with no answer UI: it rendered as a
  generic card (Approve refused; the only route was Type -> "Send correction"); Farheen's calendar invite and its
  update raised TWO cards; and the Shama #164 chase was re-armed the day before a booked meeting.
- CARD: request.kind `deal_pick` renders as `dealPickCard`: one button per candidate + "None of these", or his words
  (Tap to talk / Type) -> `POST /api/tasks/{id}/deal-pick {deal_id | text}` -> `events.answer_which_deal(task, text,
  pick_id)`. `_resolve_pick`: code first (the card's option number, any open deal number, 'none', 'a new job: X' which
  creates the opportunity with the sender as contact), else ONE Haiku match of his words against EVERY open opportunity
  of the company (ids checked by code): one -> filed; several -> the card re-asks with only those; none -> stays open
  with the reason. The answer binds every thread on the card (`thread_deal:<id>`).
- ONE QUESTION PER PERSON: an open deal_pick for the same person (request.who, direction) takes their next messages
  (`request.messages`); one answer files them all. INVITES NEVER ASK (`engine._no_reply_mail`).
- GATE: `calendar.next_meeting_with(email)` (the week's events, cached 10 min) -> no automatic email to someone with a
  meeting booked in the next 7 days, read from the shared calendars AND each person's own (setting `people_calendars`:
  rashad@, gino@, ayresh@sensa.digital via the Sensa token; a client invite lands on the person's calendar). The
  words matcher sees each deal's client and contact ("the Merck summit" -> Abu Dhabi Future Health Summit). v69.

## Uploaded files count as attached; a new job opens its own thread; one copy of each core document (14 Oct 2026)
- Card 1239 (Merck summit, two option PDFs uploaded by hand): `_read_through` saw only library `attach_docs`, decided
  nothing was attached and turned "Please find attached our quotation" into "Please find our quotation".
  `engine._outgoing_names(req)` = library documents + uploaded `attachment_names` (never a client's inbound files).
- The same email went out on "National Day Graphics Quotation - Merck", deal #5's thread with the same contact: the
  summit deal had no thread, so the newest thread with Ricola won. `_adopt_existing_thread` now skips any thread another
  deal's sent cards used (or `thread_deal:<id>` binds elsewhere) unless this deal has used it too; with nothing left the
  email opens its own conversation under its own subject.
- `documents.retire_same_name`: a new core-kind document (CORE_KINDS) supersedes earlier live copies with the same name
  (kept on record). Retired 14 Oct: Body Double 808-811 -> 812, AI Avatar Posts 813-814 -> 815. Company profile: doc 7
  (the 2.3MB email copy, profile `high_value_attach_doc`) is now kind company-profile and the one offered/attached;
  doc 1 (9.6MB master, same profile) is superseded by it, kept on record and on Drive.

## Mail to the support mailbox is support, client or not; a client's email is never a tender (14 Oct 2026)
- Alan Cheung (Sampras, deal 60) wrote TO support@tabscanner.com asking for OCR training, then sent a token ID to check.
  The first was skipped, then judged by `_flag_skipped_opportunity` as an in-scope "tender" (deal #165 created, closed
  Lost); the second was DRAFTED (card 1232) because the 13 Oct rule let any open deal override the handling-mailbox
  match. `policy._ROLE_LOCAL`: with a deal, a ROLE mailbox (support@, help@, info@, accounts@...) still decides
  (`{"by_address": True}`); a person's address (ben@) is judged on content, which keeps the ABN AMRO / Mike Enting case.
  A by-address skip goes on the deal with NO "Not drafted" notice. `_flag_skipped_opportunity` never runs for a sender
  on an open deal.

## One record of what is owed; deliverables get lead time (owner, 14 Oct 2026)
- ROOT CAUSE of the recurring duplicate reminders: four writers set reminders for what we owe on a deal (meeting notes,
  twice per meeting; every sent email; client deadlines; next steps), each with its own word-overlap check or none.
  Shama #164 had three reminders for one "2 to 3 creative options" promise; Sampras #60 a deadline and a promise for one
  change. The check now lives where EVERY Cortex reminder passes: `reminders.create` -> `obligations.check_new` (one
  Haiku question, by meaning, against everything open on the deal, the owner's own reminders included): the same thing
  updates that one reminder (a changed stated date replaces; otherwise the earlier stays), never a twin; matching the
  owner's own reminder adds nothing. Touch reminders, recurring and action reminders are outside it.
- DELIVERABLES: when the new item is work we must produce (treatment, proposal, quotation, deck, script...), the
  reminder fires `delivery_lead_working_days` (profile, default 2) working days before it is owed, at 10:00 Dubai
  (`reminders._lead_due`), title "(deliver by Fri 16 Oct)", column `reminders.deliver_by`; `_commitment_action` never
  turns it into an email.
- SAFETY NET: invariant `duplicate_obligations` -> `obligations.merge_twins` each morning (keeps the owner's own or the
  earliest due, cancels the rest, logged on the deal). Invariant fixes may now name any `module.function`.

## An identical quotation is never reissued; a new quotation replaces only quotation files (card 1246, 14 Oct 2026)
- CAUSE: a correction fixing the EMAIL ("the quotation includes set dressing... casting is priced in it") was read by
  `_quote_intent` as a quotation decision; `build_quote_from_words` rebuilt SEN-2026-0043 line for line and issued it
  as v5 (card 1247, identical to v4, AED 76,850). Approving it ran `_quote_to_email`, which dropped every attachment
  whose NAME held the number, so "Shamal Holding - Proposal SEN-2026-0043 v4" left the email with the old quotation.
- `_same_quotation(prev_entry, sections, payment, rounds, other)`: same lines (count, qty, price, in order),
  descriptions differing only in wording (`_near`, 80% words shared), same split, rounds, preset, and note /
  deliverables / title near-equal -> NOT issued (`{"unchanged": True}`); `apply_correction` then treats the words as an
  email correction and redrafts. Options, scrap and new-number builds are never short-circuited. `_quote_intent` is
  also told that describing what the quotation contains is not a decision (the guard, not the prompt, is what we rely on).
- `_replaced_attachments(existing, refs, nums)`: an attachment leaves only when the library files it as the SAME KIND as
  one coming in (quotation, terms) and it carries the number; decks and everything else stay. The email card shows
  `request.attach_changes` ("Added: ... · Removed (replaced): ...") until he changes the files himself. v70.

## A confirmed ad click is a lead; only their own words are judged (14 Oct 2026)
- A.Yousef (Waheguru Travels) tapped the landing page's WhatsApp button from a paid click (ref NFFQ5); qualification
  read OUR pre-filled text and OUR tracking ref as "a vague one-liner with a spammy ref code" and withheld the
  opportunity; Alexander's identical text an hour earlier passed (model noise on our own words).
- `whatsapp.their_words(msg)` removes the pre-fill (setting `wa_prefills`, default the Sensa LP text) and the ref
  before any judgement; with nothing of their own, no qualification runs. A ref matched to a click on record
  (`lead_source.click`) opens the opportunity whatever the verdict. The qualifier is told no email is expected.

## Client accepts a quotation: signed first, then the invoice (owner, 15 Oct 2026)
- Rule on Sensa + Sky Vision `email-handling` (company scope, live DB): on a client's yes, ask for the SIGNED AND
  STAMPED quotation plus their legal name, address and TRN; the pro forma for the first payment follows once it is
  back. Bank details never go in an email: they travel only on the pro forma (proforma.py flow, see "Signed quotation
  back -> pro forma invoice"). First live case: Heidar / Workspace, deal 122, SEN-2026-0027 v2 (card 1257 corrected).

## "Not interested": one tap, nothing sent (owner, 15 Oct 2026)
- Skipping a reply card left its siblings behind (the "Next step: quote for..." prep card, the "which opportunity?"
  question) and left the deal's chase paused with an "Unanswered" notice to come (Ahmed's AccessAbilities Expo RFQ on
  #149, Dubai Police's sustainability-certificate request). `engine.not_interested(task_id)` (POST
  `/api/tasks/{id}/not-interested`, a "Not interested" button on reply, next-step and which-opportunity cards): closes
  every open card the same email raised (same Message-Id or subject, or a next-step card on the same deal, created
  within 3 minutes), status CANCELLED (no streak hit), cancels their owed work (`_cancel_owed_work`), logs
  `not_pursued` on the deal and resumes its chase. `not_pursued` counts as our answer in `gate.check` and
  `_rearm_stalled_chases`. Nothing is ever sent. v71.

## One flow per opportunity: every event re-checks the Inbox (owner, 15 Oct 2026)
- CAUSE: about half the Inbox on 15 Oct was stale: notices for reminders already done or cancelled, "task could not be
  created" for a deliberate gate hold, a 12 Oct next step on a booked project, a promise that waits on the client,
  seven separate call nudges, and a Massar chase two days after the last one (the 13 Oct rhythm migration set Thu 15
  Oct without seeing the chase that went that morning; the closing quiet window was 0.8 days). Each mechanism cleaned
  up only after its own event; nothing looked at the opportunity as a whole.
- `flow.py`: `pipeline.log_deal` (every timeline event) and `store.update_task` (a card closing) call `flow.mark(deal)`
  -> table `flow_queue` (settles 15 s) -> the engine's `inbox-flow` thread (`flow.start_thread`, 10 s) -> `reconcile`:
  RULES first (notice of a finished reminder, closed deal, "Unanswered"/"Not drafted" answered by our email or
  `not_pursued`, a manual person's deal, a promise matching `pipeline._CONDITIONAL`), then ONE Haiku judgement over what
  is still open (Cortex reminders, deal notices, Cortex-raised cards via `_cortex_card`): keep / close / move, plus the
  client's posture (on_hold -> next chase no sooner than 14 days after our last email; declined -> one "move to Lost?"
  question). Never touches his own reminders, replies to their email, cards he corrected, quotations or proposals.
  Every change is logged on the deal ("Inbox tidy: ...") and moves setting `inbox_rev`. `flow.preview(deal)` shows the
  judgement without changing anything. Morning safety net: invariant `stale_inbox_items` -> `flow.reconcile_rows`.
- GATE: `gate._rhythm_floor` = the deal's current cadence step counted from OUR LAST email; an automatic chase before it
  is held and `next_followup` moved there. `gate.last_hold()` (thread-local) tells `reminders.fire` a None from
  `_spawn_task` was a deliberate hold: the reminder waits a day, no notice; a real failure carries the deal and
  `reminder-fail:<id>`.
- CALLS: `touch.fire` adds a line (with WhatsApp / Call buttons and the opportunity) to ONE "Calls and WhatsApps today"
  card per company per day (`touch-day:<company>:<date>`, category `calls`), not a notice per person.
- COCKPIT: `GET /api/inbox/rev`; the Inbox reloads itself when the rev moves (every 12 s while open, never while typing).
  v72.
- Hardened before the one-off run (15 Oct): a Cortex chase raised before `_rhythm_floor` is cancelled by RULE, not
  judgement (Massar 1262, and 1259/1260/1261/1269 from the same 13 Oct migration); `not_pursued` is never the client's
  answer, and a judged `declined` raises "move to Lost?" only when the client's own newest message matches `_NO_RX`
  (Unified Robotics #149 was read as declined from our own not-pursued note). One-off over 59 deals: the Inbox went
  from 23 notices to 4 (one of them the daily calls card); every change is on its deal as "Inbox tidy: ...".
- DIGESTS: a delivered digest from an earlier day is dismissed as soon as the next day's starts collecting.

## A sent proposal stays as sent; one cover email per quotation (16 Oct 2026)
- CAUSE: Shama (deal 130). A note asking for a 35,000 budget built quotation v5, and `restamp_proposal_price` re-opened
  the 21 Sep proposal card 815 (approved and emailed long before) and, through `creative.adopt_quotation`, re-ran the
  COPY WRITER over the whole deck ("keep every page as it is" still rewrote it). The owner then approved quotation v6
  after its email had gone, and `_quote_to_email` drafted a second cover email (card 1290).
- `engine._proposal_sent(p)`: a sent email on the deal carried this proposal (its `proposal_card`, or a "Proposal
  <number>" file). `restamp_proposal_price` then changes nothing and raises one notice (`deal:<id>:proposal-sent-<n>`);
  Talk `update_proposal_pricing(deal_id)` forces it (`force=True`) when he asks.
- `creative.adopt_quotation` runs NO copy stage: the investment page is code from the live quotation; only investment
  copy that could contradict it is cleared (details for blocks that no longer exist, a blurb stating a figure).
- `_quote_to_email`: a quotation whose PDF already went out on a sent email on the deal is approved with no new email.

## Pipeline review (owner, 16 Oct 2026)
- Opportunities screen -> "Review next steps" (`openReview`, sw cortex-v73). One row per Opportunity/Quote deal:
  contact, value, last contact each way, last owner note, and every next step with its date: the email chase clock
  (or why there is none), every pending reminder on the deal (his own marked "Your reminder"), open and scheduled cards.
  "Looks right" is a per-viewer tick (localStorage, cleared when the steps change); "Not right" takes typed or spoken
  words.
- `runtime/cortex/review.py`: `summary(deal)` / `rows(labels)` are code only. `apply_note(deal, note, by)` files the
  note through `crm.add_project_note` (context + chase/touch timing + the waiting email or quotation), then ONE Haiku
  reading (`pipeline-review`) returns verdicts by step id (cancel / move with HIS words / keep, stop or resume the
  chase, add a reminder); code resolves dates (`reminders.parse_when`, then `_resolve_their_date`), acts only on
  reminders still pending, never cancels a quotation or proposal card, never moves a stage (it says so), logs
  "Pipeline review: ..." on the deal and marks the flow. The weekly touch reminder is kept when he says he spoke to them.
- API: `GET /api/pipeline/review?company=`, `GET /api/pipeline/review/{id}`, `POST /api/pipeline/review/{id}/note`.

## Talk audit and fixes (16 Oct 2026)
- AUDIT (read-only, every assistant turn 16 Sep to 16 Oct, 168): 15 find failures, ~35 stuck turns, median reply 220
  words (p90 365). Causes were mostly code: a false "you claimed something" guard (36 replies), every tool round's
  narration glued into the reply (75), and a "speak naturally, no bullets" prompt. Lookups were all substring matches.
- GUARD (`provider.py`): a turn counts as having made something when a creating tool ran (the list now includes
  proposals, reissue, correct_task, update_opportunity, notes, documents, rules) OR a tool result names a card
  (`_CARD_IN_RESULT`). `_CLAIM_RE` is past-tense claims only ("I've drafted"), never "card #N is in your Inbox". The
  leftover note is one line, "(Nothing was saved this turn.)".
- ONLY THE ANSWER IS KEPT: `chat_tools_stream` keeps the final round's text as the reply; tool rounds are painted
  live and cleared (`tool` / `reset` events, cockpit v74).
- ANSWER STYLE is the first always-on rule (`_shared_behaviour`): bottom line first, ~60 words for a simple question,
  at most five short lines otherwise, digits, no recap / narration / unasked strategy / rule offers; "deep dive"
  lifts it. `personas.VOICE`, the Chief ("strategy only when asked") and the Manager (scope question only when
  saving a rule) follow it. Measured after: 70, 71 and 18 words on the audit's failing questions.
- `runtime/cortex/find.py`: ONE fuzzy finder. Word by word: exact/prefix, sound (Postgres `fuzzystrmatch`
  dmetaphone, installed 16 Oct with `pg_trgm`), spelling (trigram, computed in Python), adjacent words joined
  ("Al Hammad" = "Alhamad"); ranked up for open deals, recent activity, clients; only results within 0.2 of the best.
  Aliases: seeded team names + setting `talk_aliases` (Talk `remember_alias`). Used by crm_lookup (adds
  `closest_matches`, phone search, matching `opportunities`), deal_timeline(search=), draft_email name resolution,
  list_tasks(search=, company=, deal_id=), and company names in `_talk_company` (`find.company`). Email-less WhatsApp
  contacts are included; CSV-blob import rows (name 70+ chars) are not.
- New Talk tools: `update_contact` (by email, phone or fuzzy name; rename, number, email, title, organisation),
  `remember_alias`, `pipeline_review`, `update_proposal_pricing`.
- FILES: a document attached in Talk is saved to the library on arrival (`kind client-document`, 40MB) and Talk is
  told its id and to state what it read, because an attachment is only visible on the turn it came with.
- QUOTATION GUARD: unit prices already on the stored versions of the quotation number count as the owner's figures.
- KEPT ON THE SERVER: every tool call -> table `talk_tool_log` (who, tool, input, output); the last finished reply
  per person -> setting `chat_last:<email>`, which the cockpit fetches (`/api/chat/last`) when its stream drops.
- NOT BUILT (from the audit's gap list): a non-destructive deal merge, xlsx -> PDF export.

## Owed work closes only with proof: a database guard (owner, 16 Oct 2026)
- CAUSE: the Inbox tidy (`flow._judge`, Haiku) closed Shama's Ramadan creative options (R350, deliver by 16 Oct) on
  a guess ("the meeting was the deliverable checkpoint"), plus Shama's 20 Oct final-content deadline, two D-Marin
  documents and two HBMSU concept items. Any code could close any reminder with one UPDATE; protection was each
  caller's judgement. Restored (R350, 353, 241, 336, 337, 342, 343).
- THE GUARD IS IN POSTGRES (`obligations.ensure_guard`, once per process from `reminders.ensure_schema`, also from
  `review.summary` / `flow._open_items`): column `reminders.owed`, set by INSERT trigger `reminders_owed_mark` from
  `obligations.KINDS` (Cortex-created proposal owed / commitment / meeting commitment / client deadline / next step /
  conversation commitment / tender close; never touch reminders, never the owner's own) and backfilled. UPDATE trigger
  `reminders_owed_guard` RAISES (`OWED_GUARD`) when an owed reminder moves to done/cancelled without
  `closed_evidence` whose `kind` is in `obligations.EVIDENCE`: owner (who), sent_email (ref + code-verified quote),
  client_words / thread (code-verified quote), deal_closed / stage (stage), duplicate (kept id), rule (a deterministic
  owner rule, e.g. a promise that waits on the client). Reopening clears the evidence. Proven live: a raw
  `update reminders set status='cancelled' where id=350` is refused.
- ONE WAY TO CLOSE: `obligations.close(rid, status, evidence(kind, ...), why)` / `close_where(...)` for bulk (a deal
  closing). Every writer goes through it: crm deal-close and Completed, obligations on_sent / on_stage /
  cancel_matching (owner) / merge_twins (duplicate), reminders fire on a closed deal, the commitment-kept check, the
  cockpit done/cancel buttons (`reminders.mark_done(rid, who)` / `cancel(rid, who)`), the review note, invariant fixes.
- A MODEL'S "IT WAS DONE" MUST QUOTE ITS PROOF: `obligations.on_sent` and `reminders._commitment_action` ask for the
  exact words that deliver it; `obligations.quote_in` checks they are really in the email / thread (6+ words).
- THE TIDY CAN ONLY FLAG: `flow._cancel_rem` on an owed reminder without evidence calls `obligations.flag` (column
  `suspect` {why, by, at}); a judged "move" of owed work is a flag too ("could wait until ..."). Review next steps
  shows the flag with Done (closes, the owner is the proof) / Still owed (clears it):
  `POST /api/pipeline/review/{deal}/owed/{rid}`, `review.settle_flag`. sw cortex-v75.
- Health check `owed_closed_without_proof` (report). Calls card: an earlier day's "Calls and WhatsApps today" is
  dismissed when today's starts, its unmade calls carried over ("carried over").
- STILL OPEN: 86 owed reminders the tidy closed on a guess before the guard (mostly September, open deals) carry no
  proof; listed by `closed_evidence is null and status='cancelled' and owed`. Owner to decide whether to reopen them
  as flagged items on the review screen.
- COMING UP ON THE OPPORTUNITY PAGE (16 Oct 2026, sw cortex-v76): `comingUp(id)` fills `#cuBox` at the top of
  `openProject` from `GET /api/pipeline/review/{id}`: the same dated list, flags with Done / Still owed, and "Change
  what's coming up" (typed or Tap to talk) -> `POST /api/pipeline/review/{id}/note` -> `review.apply_note`.
- OWED WORK WITH A HELD EMAIL STILL SHOWS: when the gate holds a commitment reminder's email (`reminders.fire` hold
  branch), an owed reminder also raises ONE notice ("Still owed. Cortex did not draft the email: <why>",
  `owed-held:<id>`); the email is retried the next day. D-Marin R336 had fired into silence.
- SEVERAL DUE ITEMS ON ONE DEAL CARD: a per-deal notice that coalesces different titles is retitled
  "N things due on <deal>" (`notifications.notify`), each item a line; the Ramadan creative options had sat inside a
  card titled with the brainstorm reminder.

## Who's who: the cast list (owner, 17 Oct 2026)
- CAUSE: Heaven card 1297 thanked Balazs and asked him to pass thanks on; Balazs only made the introduction and left in
  minute one, Abraham ran the call and closed it asking for the proposal, and a 10 Oct note said Balazs asked to be
  copied. CODE chose the recipient (`_adopt_existing_thread` re-addressed follow-ups to the thread counterpart) and the
  sender (`_deal_sender`: whoever of us sent most); the writer saw a Haiku brief, not the meeting. Same rules kept Gino
  as sender after the owner stepped in, and ignored "I'm taking over this project".
- `runtime/cortex/cast.py`: column `crm_projects.cast_list` = {their: [{email, name, role, pinned, why, at}], ours:
  {email, pinned, why}, log}. Roles: lead (written TO), decision, member, cc, support, left. `get` builds it from the
  contacts the first time. `read(deal, material, kind, pin, ours)` asks one model whether the FULL material changes
  who's who (models are data, setting `cast_models`: email/note Sonnet 5, meeting Opus 5); `_apply` (code) accepts
  only enum roles and people the material or cast names, never our own team, never overrides a PIN, keeps one lead,
  logs a `cast` event on the deal. `we_spoke(deal, our_email)`: one of us writing BY HAND leads for us (deterministic).
  `_save` mirrors the lead to `contact_email` and puts every involved person on the deal's contacts.
- HOOKS: `events.they_wrote` (their email / WhatsApp, new part), `pipeline.record_send` (our email; manual ->
  `we_spoke`; a Cortex send never changes our lead, `ours=False`), `pipeline.record_meeting` (the meeting row's
  `full_text`, Opus), `crm.add_project_note` and Talk `note_opportunity` (his words, `pin=True`). All async except Talk.
- CONSUMERS: `_deal_sender` (cast our lead first), `_adopt_existing_thread` (a follow-up goes TO the cast lead whoever
  the thread was with: manifest `addressed_to_lead`), `_deal_participants` (cast copied people in, `left` people out),
  `_draft_context_for_reply` (`request.cast` = `describe()`; for `followup == 'post-meeting'` the deal's newest
  meeting `full_text` as `request.meeting_full`, 45k chars), `worker.draft` (WHO'S WHO block + FULL MEETING block;
  setting `purpose_models` {"post-meeting": "claude-opus-5"} picks the model by `request.followup`).
- SCREENS: `review.summary` returns `who` + `our_lead`; the review rows and the opportunity's Coming up show "Who's
  who" (hover = why). Correct it by saying so in the Change box or Talk ("Abraham leads, copy Balazs"): pinned. v77.
- Proven 17 Oct on Heaven: the meeting read gave Abraham lead, Balazs support, Barbara decision, Miklos team, Rashad
  our lead; card 1297 redrafted on Opus to Abraham with the full meeting.

## A deck that sells content can show it: render_spec page types (17 Oct 2026)
- CAUSE: the Heaven Properties retainer deck came out as columns of type on black. `_Deck.photos` (a 3x2 picture
  grid) and `_Deck.samples` (clickable film thumbnails) existed but `render_spec` had no branch for either, so a
  hand-written deck could show one image per page at most. A deck whose product IS content has to show the content.
- `render_spec` now accepts `photos` ({kicker, heading, intro, images: [paths], section}) and `samples`
  ({kicker, heading, intro, video_ids: [...], playlist, section}). Films are named by ID and resolved BY CODE
  (`films_by_ids` + `thumbnail` + `film_caption`), so an id the library does not hold is dropped, never substituted.
- Any page may already carry `bg` (a darkened photograph behind the type). The `strip` type leaves the lower half of
  a page empty, so a tier/comparison page wants one.
- SAAS PAGE TYPES (owner, 17 Oct 2026: "lay that out like a SaaS company would ... it should feel like an enterprise
  SaaS service"). The house types were built for a film proposal, so a monthly-service deck laid out with them reads
  dry. Four more on `_Deck`, all on `render_spec`: `pricing` (cards with a feature list, the recommended one picked
  out in the accent), `flow` (a process left to right with arrows, optional inline-SVG `icon` per step), `showcase`
  (four captioned stills, 2 x 2: the picture, what the format is, what it does, where `photos` shows pictures with
  no words) and `faq` (the Q&A in two columns at a size you can read, where `qa` leaves most of a 720px page black).
- CHUNKY HEADINGS, BIG ICONS, AND AN ASK THAT LOOKS LIKE ONE (owner, 17 Oct 2026: on the website "the titles
  are a lot more chunkier"; "these dots ... they're crap, let's have proper icons, something big and chunky";
  "we need to make the last page stand out, that last call to action"). The deck's h2 is 27px at weight 600
  against the site's Poppins 800, which is the whole difference. `spec["chunky"] = True` opts ONE deck into
  `_CHUNKY_CSS` (h1 58/800, h2 44/800, mono kickers, 15.5px body) through `_Deck.extra_css`. DELIBERATELY NOT
  GLOBAL: a deck already laid out against 27px headings would overflow at 44px, and render_spec only reports
  an overflow after the fact. A `flow` step's icon is now 40px inside its own 72px panel and the meaningless
  corner node dot is gone; `flow(arrows=False)`, on render_spec too, lets a page be a row of capability cards
  rather than a sequence. New page type `cta`: a 72px headline that may accent a phrase, one line under it,
  the next three steps in a row with the house icons, and a bordered reply-to panel. `closing` summarises a
  proposal, `cta` asks for the decision.
- A SPLIT PAGE CAN ANSWER ITS OWN PROBLEM (owner, 17 Oct 2026). `split` states the client's situation
  and only ever filled the top half of its page. `band` = {label, title, body, items} draws a full-width
  block under the two columns, so the answer sits on the same page as the problem. First use: the Heaven
  retainer deck's "content as a service" block under "Four days, inside fourteen".

## The AI content retainer: its own preset, its own terms (owner, 17 Oct 2026)

- `content-retainer` in `settings.quotation_presets` is the MONTHLY CONTENT PROGRAMME. It is NOT the
  `retainer` preset, which is media CUSTODY (archive plus a minor revision service) and has nothing to do
  with producing content. Both are named in sales-quotation rule 1 and in the placement rule, so the two
  can never be confused again.
- Priced by TIER from the rate card (`AI content retainer packages`, owner-approved 17 Oct 2026):
  retainer-tier-1/2/3 at AED 10,000 / 20,000 / 36,000 a month. A tier is a PACKAGE price, not the sum of
  the per-video items in the `AI content retainer` group, which is why the per-film figure inside a tier
  sits below the per-video rate for the same volume: the tier absorbs the language versions, the avatars,
  the planning cycle and the photography.
- EVERY TIER STATES ITS RUNNING TIME AND ITS RESOLUTION (owner: "it can't be 30 or 60 seconds, we have to
  pick one or the other"). Running time is the biggest single driver of what a film costs, so a tier
  without one is not a price. Tiers are up to 30 seconds, delivered in 1080p.
  `retainer-tier-60s-uplift` and `retainer-4k-uplift` are on the card UNPRICED, OWNER TO CONFIRM.
- Payment is 50% on signature and 50% at the start of month four. The term is a fixed six months that
  AUTO-RENEWS for successive six month terms; 30 days notice before the end of a term, or at any time
  during a renewal with a pro rata refund. A month-to-month tail cannot coexist with billing in two
  instalments, and monthly billing is not workable with UAE clients.
- THE AVATAR CLAUSES ARE A LICENCE, NEVER AN ASSIGNMENT (owner, 17 Oct 2026, after asking whether an AI
  character can be licensed at all). Copyright needs human authorship: purely machine-generated output is
  unprotected in the US (Zarya of the Dawn 2023, Thaler) and UAE Federal Decree-Law 38 of 2021 has no
  carve-out for it, so "Sensa owns the character" was an untested assertion and selling an assignment of
  it would be selling something we may not have. What we hold is the BUILD (reference sheets, trained
  models, voice model, prompt chains) as confidential information, plus what the contract binds the
  client to. Clause 19 is therefore an AVATAR RELEASE: the build assets delivered under a perpetual
  exclusive licence, the restriction lifted, and an undertaking not to reuse the character.
  AED 12,000 custom / AED 20,000 likeness (`retainer-avatar-buyout-*` on the card). The terms state
  openly that rights in AI-generated material are unsettled and that neither party warrants a monopoly in
  a look, because nobody can stop an independently generated lookalike. A LIKENESS avatar's control point
  is consent and data protection (a trained face or voice model is biometric personal data under the
  PDPL), not copyright, which is why those models are held rather than circulated.
- Rate card v2.4; v2.0 to v2.3 kept as `rate_card:sensa:vN`. Preset backups:
  `quotation_presets:backup-2026-10-17{,b,c,d}`; rules backup `sales_quotation_rules_backup_2026_10_17`.
  `python -m cortex._export_templates sensa` was re-run, so the Drive terms folder carries
  `Sensa - Quotation Template - content-retainer.docx` and `Sensa - Rate Card v2.4.xlsx`.
- First use: Heaven Properties deal 150, SEN-2026-0048 (v5 is the live draft). The terms are a v1 and will
  move when the client comes back with scope.

## Meeting notes to a client: the summary only (owner, 18 Oct 2026)
- Clients ask for the meeting notes; card 1301 (Heaven) retold the meeting in the email body. Gemini's doc has three
  parts: Quick notes (summary + next steps), Full notes (Details, with internal asides: the AI models we use, per-video
  maths, misheard names) and the Transcript. Owner: attach the SUMMARY ONLY.
- `meetnotes.summary_lines(text, title)` cuts the Quick notes tab (an email copy's "Quick Notes", else the Full notes'
  Summary + Next steps) BY CODE, verbatim; never Details or the transcript. `summary_pdf(meeting_id)` lays it out as a
  Sensa PDF (logo, title, Dubai date, attendee names, bullets, "[Name] Step:" in bold) via `deck.to_pdf`, files it on
  the deal as kind `meeting-summary` ("Meeting notes - <client> - <date>.pdf", client Drive folder) and records
  `meeting_notes.summary_doc_id` (a rebuild supersedes the old one). `ensure_summary_doc(deal|contact|meeting)`.
- ATTACHED AUTOMATICALLY: `_spawn_post_meeting_followup` puts it on the post-meeting card (`attach_docs`, plus an
  "ATTACHED: ..." fact line above the notes marker); `_refresh_followup` rebuilds it when fuller notes arrive.
  `attach_summary(task_id)` adds it to an existing card and reconciles the wording.
- ON REQUEST: `documents.find` with a deal and a query like "meeting notes / summary / minutes" builds the summary if
  missing, so Talk `draft_email(attach_documents=['meeting notes'])`, `attach_document` and a card correction all work.
- THE TRANSCRIPT NEVER LEAVES: `documents.PRIVATE_KINDS` = ("meeting-notes",), the full .txt record from `_keep_full`.
  `find` never offers it, `card_ref` raises, the send loop refuses it, the cockpit attach returns 400; health check
  `private_record_on_card` (report).
- BEHAVIOUR IS A RULE: universal "MEETING NOTES ATTACHED" on sales-followup and email-handling (thank them, the two or
  three next steps, notes attached; no retelling).
- First use: card 1301, "Meeting notes - Heaven Global - 16 Oct 2026.pdf" (library 1071).

## Health check: his notices are actionable, repairs are a log, repeats are root causes (owner, 18 Oct 2026)
- The nightly card ("Health check: 4 to look at") listed every finding in our words; he could act on none of it.
  `invariants.run(fix=True)` now: logs every finding to table `health_log` (day, key, found, fixed, sample) and raises
  NO summary card. A check with an `owner` block ({deal, title, body} templates) raises ONE plain notice per row
  (`health:<key>:<deal>`, category approval, linked to the opportunity), raised on what is LEFT after the repair, and
  dismissed automatically when the row stops appearing. Owner-facing checks: opportunity_without_contact,
  owed_waiting_for_owner (new: owed items flagged "may be done"; he confirms Done / Still owed on the opportunity),
  same_contact_two_sales_deals_sister_brands, stalled_sales_deal, opportunity_from_stale_enquiry.
- `"sweep": True` (stale_inbox_items, duplicate_obligations) marks a check that re-examines everything open: only its
  real changes count as a fault; the rows it looks at are not "left to look at".
- `invariants.repeats()` / `python -m cortex.invariants repeats`: faults on 3+ of the last 7 days. Read at session start.
- ROOT CAUSE FOUND THE SAME DAY: the daily "fixed 6" was the Inbox tidy RE-FLAGGING the same owed items (Heaven R439,
  R440, R461 flagged eight times in two days; R122, R342/R343, R298, R351, R437 likewise). `flow._awaiting_owner`: an
  owed reminder already flagged (`suspect`) is not put to the judge again; "Still owed" (`review.settle_flag`) stamps
  `reminders.owner_kept_at` and the judge leaves it alone until a newer email, meeting or context note on the deal.
  The flags had never reached him (timeline only); they are now the owed_waiting_for_owner notices.
- duplicate_obligations found deals with several Cortex reminders but merged none (they were different things): no
  leak in `obligations.check_new`; its SQL counts any deal with 2+ reminders, so it stays a sweep.

## Creative treatments as building blocks (owner, 18 Oct 2026)
- Owner: Cortex's fixed flows are "a never-ending development project"; give Talk the tools and let it combine them.
  First area: creative proposals. `create_creative_proposal` (the one-go run, one treatment with its quotation) stays;
  three Talk tools take the same stages apart (`creative.py`, bottom section):
  - `creative_routes` -> `creative.routes()`: brief + location ONCE in a seed job (`set-<id>`), then N different routes
    (title, logline, idea, why), lettered A/B/C. No images, no price, about a minute. Set = setting
    `creative_set:<id>` {deal_id, company, seed, direction, routes, treatments{letter: card}, email_card};
    `creative_set_latest:<deal>` points at the newest.
  - `creative_treatments` -> `start_treatments()`: one card per picked route (letters, or his ideas in words, which
    become routes), job `<set>-<letter>` copied from the seed (no repeat research), `forced_route` makes the concept
    stage develop THAT route, `skip_quote` makes `_run` skip the quotation and `omit_investment` drops the price page.
    Two builds at a time (`_build_set`). A letter already built is skipped (revise it on its card).
  - `creative_quote` -> `quote_treatments()`: one treatment = its quotation (the deal's number as the next version, or
    new); several (or one when a sibling is already quoted) = one NEW number each, titled "Option <letter>: ...",
    `request.option` = the treatment's letter. The deck is re-laid with its own price page (render + file only, copy
    never rewritten, no images). `adopt_quotation` never puts other options on a treatment deck.
- FILING: a treatment is named "<Client> - Treatment <L> - <title> [number] [vN] - date.pdf" (`_treatment_name`) and
  archives/supersedes only ITS OWN earlier decks (`archive_prefix`, `job.s.doc_ids`): the deal-wide supersede and the
  "<Client> - Proposal" Drive archive would otherwise retire its siblings.
- ONE EMAIL PER SET: approving a treatment card goes to `creative.approve_in_set` (`_execute`); a set quotation's
  approval reaches `creative.set_email` through `_draft_proposal_email`. When every live treatment (skipped/cancelled
  ones drop out) and every quotation built for one is approved, ONE email_draft to the deal contact carries every deck
  and quotation PDF (`request.creative_set`), else the note lists what still waits (`_set_missing`).
- Revisions per treatment card work as before (`revise`), and never price a treatment that has no quotation.
- Verified live 18 Oct on iTellu (deal 156, routes only, nothing saved): three distinct routes from the deal record.
  A full treatment set has not yet been run end to end (about USD 8-12 per treatment).

## ONE next-steps list per opportunity (owner, 18 Oct 2026)
- WHY: an opportunity had no "next step", only a pile: an owed reminder minted by every meeting row and every email
  that restated a promise, an independent chase clock, and the Inbox tidy guessing "probably done" from silence.
  Seven live cases the same morning (Shama #164, Heaven #150, Workspace #122, HBMSU #107, Brent #93, Shamal #157, DSO
  #148): the real step buried, four copies of "send the proposal", client-dependent promises treated as ours, an offer
  treated as owed, our own words quoted back filed as their deadline for us, a chase armed while WE owed, and "may
  already be done" notices. Owner: "how do we stop this kind of hallucination and assumptions", and the earlier rule
  patches were "a bodge job".
- `nextsteps.py`, table `next_steps` {deal_id, what, whose us|them, kind email|work|theirs, tool, due_at, status
  open|done|dismissed, source, quote, then_what, card_id, notified_at, closed_*, log}. One list, several items.
- EVIDENCE ONLY: `observe(deal, source, material)` (Sonnet, one call) returns close/add/due ops; CODE checks every quote
  is in the material (`_in`), our email (`email_out`) closes only OURS, their email (`email_in`, new words only)
  closes only THEIRS, an offer is never owed (`pipeline._is_offer`), and an item matching anything on the list (open,
  done in 30 days, or DISMISSED) is never added (`_same`). The owner's note (`note`) may close, dismiss or add.
  Hooks: `pipeline.record_send` (with attachment names), `pipeline.record_inbound`, `pipeline.record_meeting` (Gemini's
  Quick notes via `meetnotes.summary_lines`), `crm.add_project_note` (sync). `crm._cancel_cortex_reminders` ->
  `on_closed` dismisses a closed deal's items.
- RETIRED: commitment/deadline/meeting-commitment reminders and the next-step reader's reminder branch (the code
  paths are left as `for c in []` with notes). `reminders.create` DIVERTS any owed kind (obligations.kind_of, except
  tender_close) onto the list, so no owed reminder is created again.
- DO, DON'T REMIND: `tick()` (engine loop): an OURS item falling due becomes the drafted email (kind email, card to the
  cast lead via `reminders._with_deal`) or ONE notice "To do: ..." (kind work, `nextstep:<id>`, names the Talk tool:
  creative_treatments, create_proposal, create_quotation, issue_proforma). A proposal, quotation or treatments is never
  typed "email" (`tool_for`). Each item is raised once; a new date raises it again.
- THE CHASE FOLLOWS THE LIST: `gate._check` holds any automatic chase while an OURS item is open ("we owe them the next
  step: ..."). Review/Coming up shows "No email chase while we owe them the next step".
- SCREENS AND TALK: `review.summary()["next"]` (`view()`); cockpit `nsBlock` on Coming up and Review next steps: Ours/
  Theirs, date, days overdue, the quote, Done / Dismiss / Edit / Start (opens Talk with the step) / Add a step
  (`POST /api/nextsteps/{deal}` {action add|done|dismiss|reopen|edit, item_id, what, whose, when}). Talk tool
  `next_steps` (list/add/done/dismiss/edit/reopen). `pipeline.deal_context` puts the list at the top of every draft.
  Build v78.
- MIGRATION 18 Oct: `migrate(apply=True)` consolidated every open owed reminder (16 deals) into 21 items; dropped ones
  (done, internal, post-signing, offers, chases of sent items) recorded as DISMISSED (`dismiss_record`) so they never
  return; past-due migrated items marked notified (no burst). 0 owed reminders open after. Tender closing dates stay
  reminders.
- Replay checks (nothing saved): Heaven's 18 Oct email adds only "Theirs: send the Hungarian voice data"; a signed
  quotation from Heidar closes Workspace's THEIRS item with its quote.
- THE INBOX SHOWS WHAT WE OWE (owner, same day: "where's all the stuff on the inbox that should still be there"; the
  migration had dismissed the old notices and marked past-due items notified, so nothing showed): `tick()` raises ONE
  "To do" card (`nextstep:<id>`) for every open OURS item overdue or due within `TODO_AHEAD_DAYS` (7), kept until the
  item is closed. The Inbox card carries `next_step` and shows Done / Not needed (`todoAct` -> /api/nextsteps). An email
  item due today is drafted instead; one more than a day late, or from the migration, is a To do card. v79.

## Sensa: Rashad answers new enquiries (owner, 18 Oct 2026)
- The ONE place the default sender lives is the company profile's `reply_from` (Sensa: gino@ -> rashad@sensa.digital).
  It is what a reply to mail received on the catch-all hello@ goes out as, what a new deal with no send history falls
  back to (`_deal_sender` -> cast our lead -> thread-sticky -> `reply_from`), and Talk's `draft_email` default.
  `high_value_from` was already rashad@. Rashad's mailbox token is `gmail_refresh_token:sensa:rashad`.
- UNCHANGED ON PURPOSE: a conversation Gino or Ayresh is already having stays theirs (thread-sticky sender; mail
  addressed To one of them is theirs, `_addressed_person`); Ayresh stays `manual_people`; `gmail_send_account:sensa`
  stays gino@ because its token IS Gino's Google account (relabelling it would send as Gino under another name).
- RULES (exact-text, before-state in setting `sender_switch_backup_2026_10_18`): sales-first-response[4] (was "Draft
  first-response replies from Ayresh ...") is now "NEW ENQUIRIES ARE ANSWERED BY RASHAD"; sales-first-response[2]
  dropped "never from Gino"; email-handling[4] "Gino states plainly" -> "State plainly". Envelope and no-draft policy
  recompiled. The profile's `google_access` mailbox notes (which Talk reads) no longer say "Cortex sends replies AS Gino".
- QUOTATIONS TOO (owner, same day): sales-quotation[4] is now "QUOTATION AUTHORITY": Rashad prepares and approves every
  Sensa quotation and they are drafted as him (backup `quote_authority_backup_2026_10_18`). Permissions unchanged: Gino
  and Ayresh keep `users.can_approve` and approve on his behalf. No code ever gave Gino quotation weight.
- RE-PLAN THE WHOLE OPPORTUNITY ON EVERY EVENT (owner, same day: "should Cortex not read it and then look at the entire
  opportunity, clean up any inappropriate next steps and create the correct next step?"). `observe()` now runs
  `_replan`: Opus 5 (`REPLAN_MODEL_DEFAULT`, setting `nextsteps_model`), serialised per deal (`_deal_lock`), reads the
  timeline (`_timeline`, last 40 real events), the whole list and the event, and returns close (done | superseded |
  dismissed) / add / due ops. Code: every quote must be in the event or the timeline (`_in(q, corpus)`); items the owner
  made (`source='owner'`) are never touched except by his note; only his note dismisses; offers never owed.
  `superseded` (the plan changed: the proposal step became "theirs: confirm the call") is its own status and never
  blocks a later re-add. Every email (Cortex or by hand via the sent sweep), WhatsApp in (`events.they_wrote`), meeting
  and note re-plans. Quiet: one history line per change. Measured: USD 0.03 a re-plan; the one-off `replan_all()` over 44
  open opportunities cost USD 1.38.
- THE CHASE (owner: "we might be doing a proposal ... but trying to confirm a meeting at the same time"): `gate._check`
  holds a chase only when we owe something AND they owe nothing; any THEIRS item = the chase runs. Universal
  sales-followup rule "CHASE FOLLOWS THE NEXT STEPS": a chase asks only about THEIRS items.
- ONE EMAIL PER PERSON: a due email item joins the email already open to that person (`_draft_email`: brief "ALSO
  COVER ...", system redraft via `engine.refresh_from_notes`), never a second card.
- FIXES FROM ESTITHMAR #172: `crm.qualify_opportunity` no longer creates "Prepare and send the proposal" (an enquiry
  that asks for a proposal with no scope is answered by the reply card; the re-plan after it sets the step);
  `pipeline.maybe_advance_on_send` moves to Quote only on an ATTACHED quotation/proposal file, never the subject;
  sent-email history entries name their attachments ("[attached: ...]") so a re-plan sees what went out (Merck #166:
  the summit quotation went out on the National Day thread's subject and the review re-added "send the quotation").

## Voice that survives a wobble (owner, 18 Oct 2026)
- "It's detecting it for most of the time and then suddenly stops." The api log showed a stream cut with "keepalive
  ping timeout" on a patchy connection, and the cockpit had no `onclose`: the button kept saying recording while the
  audio went nowhere.
- Cockpit (v80): ONE stream helper for tap-to-talk and hands-free (`vStart` / `vOpen` / `vRetry` / `vSend` / `vFinal`
  / `vEnd`). A drop reconnects (up to 3 tries, 0.5 s x try); the audio since the last FINAL phrase (`vrt.buf`, up to ~7 s)
  is replayed on the new socket so no words are lost or doubled; after 3 failures it stops and says the words so far
  are kept. `stopDictation` / `freeStop` call `vEnd()` first so a deliberate stop never reconnects.
- Server: uvicorn runs with `--ws-ping-interval 20 --ws-ping-timeout 60` (deploy/cortex-api.service, installed to
  /etc/systemd/system on 18 Oct; deploy.sh does NOT install units, copy + daemon-reload by hand when the unit changes);
  the Deepgram leg pings 20/60; every stream logs `[voice] stream ended after Ns, N KB up, N results: <reason>`.
- TO DO CARDS DO THE JOB (owner, 18 Oct 2026): notices are category `todo` (chip "To do"; they had been filed as
  `approval`, which read as if they needed approving). Each has Done / Not needed / Tap to talk / Type: his words go to
  Talk as `About the To do on opportunity #N: "<step>". <his words>` (`todoTalk`), and the capability manifest tells
  Talk to do that step with the matching tool and its deal_id. `nextsteps.started_from_tool` (called after every Talk
  tool in `_chat_prepare`): when creative_treatments / create_creative_proposal / creative_quote / create_quotation /
  reissue_quotation / create_proposal / issue_proforma starts the work, the item records the card and its To do card
  says "In progress: card #N". The item still closes only on evidence, via the re-plan. v81.

## A queued blog post goes live with the publish day in its byline (owner, 18 Oct 2026)
- Snap Rewards "Points vs Cashback vs Tiered Rewards" went live 17 Oct (WordPress date right, the blog list showed it)
  but the author line inside the article said 2026-06-24: `blog.py` writes the byline ("Author · Role · YYYY-MM-DD ·
  N min read") into the HTML when the post is BUILT, and a post can sit in the queue for months.
- `WordPress.go_live` now reads the draft and rewrites the FIRST byline date (`restamp_byline`, regex
  `_BYLINE_DATE` on the "&nbsp;&middot;&nbsp; date &nbsp;&middot;&nbsp;" form) to the publish day, in the same call that
  publishes. Regress case "a queued post goes live with the publish day in its byline".
- One-off 18 Oct: 17 live posts corrected to their real publish dates (tabscanner 4, sensa 3, skyvision 4, snaprewards 4,
  filmspoke 2; log in setting `blog_byline_fix_2026_10_18`); Sensa "AI Video Production Is Liberating the Indie
  Filmmaker" has no dated byline and was left. Each read back after the edit.
- CACHE: an edit to a live post is invisible until the WP Engine cache is purged. Per install (Sensa account key):
  `ssh <install>@<install>.ssh.wpengine.net 'bash -s'` with
  `wp eval 'WpeCommon::purge_varnish_cache_all(); WpeCommon::clear_cdn_cache();'` (pipe the script on stdin: inline
  quoting breaks). Installs: sensapro, skyvis, filmspoke, snaprewards, tabscanner.
