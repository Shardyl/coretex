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
back to the company `reply_from` (Sensa = gino@), which is right for the sales inbox and WRONG for a
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
- SNAP REWARDS PUBLISH PATH (found 13 Sep 2026): tasks 122 + 124 failed because the site's WordPress app password
  (`SNAPREWARDS_WP_APP_PASSWORD` in cortex.env) is REJECTED: every authenticated call returns 401 (anonymous GETs work).
  Task 125 (17 Sep) will fail the same way until the owner issues a new Application Password in Snap Rewards wp-admin
  (Users -> Profile) and it is placed in cortex.env. `_run_blog_scheduled_task` now catches a publish error, writes
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
