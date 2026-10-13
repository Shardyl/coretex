# Cortex architecture audit, October 2026

Owner, 13 Oct 2026: "it's just a never ending patch and fix ... we need a system that doesn't just patch and fix
things and looks at the underlying code and makes sure that it's properly constructed and architected."

Four read-only maps of `runtime/cortex/` were made on 13 Oct (card creators, reminders/notifications/scheduled
items, chase and deal state, intake and classifiers). This document is the consolidated finding and the plan.
Phase 0 (safety net) shipped the same day: `invariants.py` (nightly), `regress.py` (case library), `bin/deploy.sh`
(gate). Every step below ships through that gate with its cases.

## The diagnosis in one paragraph

Cortex has no single model of a relationship and what it owes that relationship. The same facts live in five
places (`crm_projects` columns + history JSON, `reminders`, `tasks` incl. scheduled ones, `notifications`, per-entity
`settings` keys), and every feature that reacts to an event (an email in, an email out, a stage change, a skip)
updates the subset it happens to know about. Each job is also done more than once: 44 places create cards, 15
create reminders, 5 loops contact people, 3 classifiers judge enquiries, 4 gates decide whether to send. A fix lands
in one copy; the others keep the fault. That is why the same classes of bug return.

## What the maps found, by class

### A. The meaning of a stage is defined in several places, differently
- `CLOSED_STAGES`, `NO_CHASE_STAGES`, `LIVE_STAGES`, `OPEN_SALES_STAGES`, and the hand-written exclusions in
  `active_deals_for_email`, `_open_deals_for_phone`, `auto_opportunity`, `_track_tender` all disagree.
- Dormant is closed for reminders and cards but open for the chase clock; a cadence-made Dormant deal gets
  `automation=null`, so the promised soft revivals never happen.
- Nurture deals get deal check-ins AND nurture touches. Close & review keeps chasing while a client reply never
  pauses it (the deal lookups exclude that stage).
- Quote sets the closing rhythm and nothing resets it on Booked. Completed ends `manual`, against "nothing on manual".
- `create_deal(stage=...)` skips the stage logic entirely (no closing or recurring cadence).

### B. "They wrote to us" is handled on one path only
`enter_closing`, `_pause_or_reschedule_followups`, `_hold_scheduled_emails`, `record_inbound` run only inside
`_draft_direct_reply`. Skipped client mail, short replies, sales-mailbox replies (`poll_sales_replies`), WhatsApp
from a deal client, domain-matched colleagues, auto-replies and meetings do none of it. The only backstop
(`_they_spoke_last`) reads one mailbox, the primary contact only, and only on the deal-chase path.

### C. Every automatic sender has its own gates
Deal chase (full set), nurture (its own), reminder/commitment/feedback emails (almost none), post-meeting follow-ups
(card check only), scheduled sends (approve gates not re-run at fire time), queued cards released by
`promote_queued` (no re-check at all). Recent-contact windows: 3 days, 2 working days, 21 days, or none. A skip stops
only deal chases.

### D. What Cortex owes never completes itself
"Prepare and send the proposal", client deadlines, next steps, tender closes, payment follow-ups, feedback asks and
conversation commitments have no event that marks them done; only stage closes (and now the nightly check) clear
them. Same-day reminders merge and lose their creator, action and priority. Notifications never clear when the
thing they announce is resolved (only on Lost/Dormant/Completed).

### E. Intake is four pipelines
Email (`poll_inbox`), web forms (`intake_enquiry`), sales-mailbox replies (`poll_sales_replies`), WhatsApp, LinkedIn
DMs: different classifiers (`classify_email`, `triage_inquiry`, `_classify_dm`), different CRM writers, different
client overrides, four ways of opening an opportunity, three "open deal" definitions, phishing on two paths only,
the no-draft policy read from one lane only (so no-draft rules on other lanes never run), and web-form cards never
linked to the deal created for them.

### F. Cards
44 creation sites. `create_task` bypasses the one-open-card conveyor and never sets `deal_id`; several Talk/cockpit
paths create email cards with no check; quotation-prep and invoice cards are created without checking for an open one.

## The target structure (Phase 2)

1. **`stages.py`: one table of what each stage means.** For every stage: open or closed, chase rhythm (none /
   default / closing / won-work / recurring / revival), what is cancelled on entry, whether inbound pauses it. Every
   list above is replaced by a lookup. Fixes class A.
2. **`events.py`: one handler per thing that happens.** `inbound(person, deal, channel, message)`,
   `outbound(...)`, `stage_changed(...)`, `card_skipped(...)`, `card_sent(...)`. Every channel calls the same
   handler, which logs on the deal, pauses or reschedules the clock, holds scheduled emails, settles what is owed and
   clears the notices it resolves. Fixes B and most of D.
3. **`gate.py`: one "may we contact this person now?"** used by every automatic sender, at creation AND at release
   (queued, scheduled): stage, they spoke last (any channel), recent contact (any channel, including meetings and
   manual sends), left-company / not-qualified, a stopped chase, an open card for the person. Fixes C.
4. **Obligations: one ledger.** The reminders table gains `kind` and `completes_on`; each kind names the events that
   complete it (proposal sent, deadline met, payment received, feedback given). `events.py` settles them. Owner
   reminders stay his. No silent merging. Notices carry what resolves them. Fixes the rest of D.
5. **`intake.py`: one pipeline with channel adapters.** normalise -> claim/dedup -> internal/phishing -> one
   classifier on the editable rules -> client override (email, domain, phone) -> one CRM writer -> no-draft policy
   across all lanes -> qualification on full rules -> one opportunity creator -> one lane chooser -> one card creator
   (conveyor, deal_id always set) -> `events.inbound`. Fixes E and F.

## Order of work

| Step | What | Why first |
|---|---|---|
| 2a | `stages.py` | Small, and every later step reads it |
| 2b | `gate.py` + release-time check | Stops wrong emails reaching the Inbox, the most visible fault |
| 2c | `events.py` (inbound, outbound, stage, skip) | Ends "the client replied but Cortex didn't notice" |
| 2d | Obligations ledger | Ends stale reminders and notices |
| 2e | `intake.py` | Unifies channels; largest change, done last on the safety net |
| 3 | Rules audit | Rules are cleaned once the code reading them is single |

Each step: cases written first in `regress.py` (from the maps' concrete findings), a health check in
`invariants.py` for the state it guarantees, shipped through `bin/deploy.sh`, old code paths removed rather than
left beside the new one.
