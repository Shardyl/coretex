"""The case library: every real fault, kept as a test that runs before a change goes live (owner, 13 Oct 2026: "we
need a system that doesn't just patch and fix things"). A fix is not done until the case that would have caught it is
here, and a change that breaks an old case does not ship.

Each case names the incident it comes from, calls the DECISION function that went wrong (never a send, never a
write), and states what must be true. Cases marked model=True call Haiku and cost a fraction of a cent each; run
the rest alone with --fast.

    python -m cortex.regress           # everything
    python -m cortex.regress --fast    # code-only cases (no model calls)
Exit code 0 = all passed, 1 = something failed (the deploy script stops on it).
"""
from __future__ import annotations

import inspect
import os
import sys
import traceback
from datetime import datetime, timezone

from . import db, store

# ---------------------------------------------------------------- fixtures (copied from the real messages)

WASL_INVITE = {
    "email": "eproc@wasl.ae", "name": "wasl eProcurement",
    "subject": "Invitation to RFQ - Request for Quotation Code 2026_1170 on Wasl Asset Management",
    "body": "Dear Supplier,\n\nwasl Asset Management invites you to participate in the following RFQ - Request for "
            "Quotation on Wasl Asset Management:\nCode: 2026_1170\nTitle: RFQ: Viya Corporate Festive Video Production "
            "Services\n- 1x full day videography 8hrs\n- 1x hero video\n\nTo view the details please click the following "
            "link and enter your Username and Password: https://eproc.wasl.ae\nClick 'Intend To Respond' ... click "
            "'Submit Response' to send the response to the Buyer.\nwasl eProcurement System URL: https://eproc.wasl.ae"}

MIKE_ENTING = {   # ABN AMRO, Tikkie deal 108: skipped on 24 Sep because Ben was copied
    "email": "mike.enting@nl.abnamro.com", "name": "Mike Enting", "from": "Mike Enting <mike.enting@nl.abnamro.com>",
    "to": "rashad@tabscanner.com, ben@tabscanner.com, hendrik-jan.staal@nl.abnamro.com, naianabezerra@nl.abnamro.com",
    "subject": "Re: Thank you for your time",
    "body": "Hi Rashas,\n\nThank you for your time and effort in setting up the report.\n\nAs I have just returned from "
            "paternity leave, I will use this and the coming week to get up to speed again.\nYou can expect a response "
            "to the report you shared in the week of 5-9 October.\n\nKind regards,\n\nMike Enting\nProduct Owner"}

ADHAM = ("Hi Sensa Team!\n\nI hope you're doing well!\n\nI recently came across your company and was really interested in "
         "the creative work you do, especially in filmmaking and production.\n\nI'm a Marketing graduate with experience "
         "at Nestle and Aster DM Healthcare, where I worked on creative projects, video production, and communications."
         "\n\nI'm currently looking for a job opportunity where I can grow, learn, and contribute to a creative team like "
         "yours. I would love to know if there are any current or upcoming opportunities at Sensa.\n\nI'd be more than "
         "happy to share my CV.")

CARD_1191 = [   # three corrections on one email; the third removed the GDPR paragraph
    ("Remove the third paragraph about GDPR and outsourcing and due diligence. They've not asked for it.",
     "Hi Naiana,\n\nWelcome back. I hope you're well and back in the swing of things.\n\nThe report is ready for your "
     "review whenever suits.\n\nOn the compliance side, since you're part of a regulated bank, your security and "
     "compliance teams will want to review our outsourcing and due diligence documentation.\n\nBest regards,",
     "Hi Naiana,\n\nWelcome back. I hope you're well and back in the swing of things.\n\nThe report is ready for your "
     "review whenever suits.\n\nBest regards,"),
]


def _co(slug):
    return store.get_company_by_slug(slug)


# ---------------------------------------------------------------- the cases

def _cases():
    from . import deck, engine, policy, reminders, whatsapp, worker
    C = []

    def case(name, incident, fn, model=False):
        C.append({"name": name, "incident": incident, "fn": fn, "model": model})

    # --- portal invitations are leads on the tender path (Wasl RFQ 2026_1170 filed as "vendor", 13 Oct)
    case("portal invitation recognised", "Wasl RFQ 2026_1170", lambda: policy.portal_invitation(WASL_INVITE) is True)
    case("a person's RFQ by email is not a portal invitation", "Wasl RFQ 2026_1170",
         lambda: policy.portal_invitation({"subject": "RFQ: drone filming",
                                           "body": "Please quote for the below and email it back to me."}) is False)
    case("an ordinary enquiry is not a portal invitation", "Wasl RFQ 2026_1170",
         lambda: policy.portal_invitation({"subject": "Video", "body": "We need a corporate video."}) is False)

    # --- a client on an open deal is never skipped because Ben is copied (Mike Enting, 24 Sep)
    def _mike():
        co = _co("tabscanner")
        deal = {"id": 108, "title": "Tikkie - receipt scanning solution"}
        return policy.should_skip(co, MIKE_ENTING, own_domain="tabscanner.com", deal=deal) is None
    case("client email with Ben copied is not skipped", "Mike Enting, deal 108", _mike, model=True)

    def _ben_only():
        co = _co("tabscanner")
        e = {**MIKE_ENTING, "to": "ben@tabscanner.com"}
        return (policy.should_skip(co, e, own_domain="tabscanner.com", deal={"id": 108, "title": "Tikkie"}) is None
                and policy.should_skip(co, e, own_domain="tabscanner.com") is not None)
    case("To Ben only: a client is answered, a stranger is Ben's", "Mike Enting / Franco", _ben_only, model=True)

    # --- a client's date resolves to the last day of the range (Tikkie chase pushed to 22 Oct)
    def _date():
        d = engine._resolve_their_date(_co("tabscanner"), "the week of 5-9 October", "Thu, 24 Sep 2026")
        return d is not None and d.date().isoformat() == "2026-10-09"
    case("'week of 5-9 October' means Fri 9 Oct", "Tikkie deal 108", _date, model=True)

    # --- WhatsApp is sorted like email (Adham Amr job application became opportunity 159)
    def _wa(msg, want, src=None, earlier=None):
        def f():
            cls, _e = whatsapp.sort_message(_co("sensa"), "+971500000000", "", msg, src, earlier or [], [])
            return cls["category"] in want
        return f
    case("job application on WhatsApp is recruitment", "Adham Amr, card 1190", _wa(ADHAM, {"recruitment"}), model=True)
    case("a model offering her services is not a lead", "Kimia, card 1094",
         _wa("Hi, I hope you're doing well. My name is Kimia, I'm a Dubai-based model. I'd love to collaborate on your "
             "upcoming shoots.", {"freelancer", "vendor", "partner"}), model=True)
    case("a website-button message is a lead however bare", "card 1117 read as spam",
         _wa("Hi Sensa, I need a video produced in Dubai. My project: (ref RSHCS)", {"lead"},
             src={"ref": "RSHCS", "line": "website button"}), model=True)
    case("a bare greeting waits for the next line", "card 1127",
         _wa("Hi", {"greeting"}), model=True)
    case("a follow-up line is read with what came before", "card 1137",
         _wa("No we want to capture indoor in a big restaurant..", {"lead"},
             earlier=["Hi, do you do drone filming for restaurants?"]), model=True)

    # --- the enquiry names the brand (Sky Vision enquiries acknowledged as Sensa, card 1188)
    def _brand():
        lane = whatsapp._brand_lane(whatsapp.routing("sensa-uk"),
                                    "Hi Sky Vision, I would like to enquire about drone filming. (ref ECFCB)", None)
        return bool(lane) and lane["co"]["slug"] == "skyvision"
    case("a Sky Vision message routes to Sky Vision", "card 1188", _brand)
    case("a Sensa message stays Sensa", "card 1188",
         lambda: whatsapp._brand_lane(whatsapp.routing("sensa-uk"), "Hi Sensa, I need a video", None) is None)

    # --- a misheard team name is put back ("Richard" for Rashad, card 1188)
    case("voice-transcribed name keeps its spelling", "card 1188",
         lambda: "Rashad" in engine._keep_team_names(
             _co("skyvision"), "Rashad will be in touch with you shortly.",
             "Richard will be in touch with you shortly.") and "Richard" not in engine._keep_team_names(
             _co("skyvision"), "Rashad will be in touch with you shortly.", "Richard will be in touch with you shortly."))

    # --- what he corrected stays corrected (a removed paragraph came back two rounds later)
    case("a removed sentence that comes back is removed again", "card 1191",
         lambda: "outsourcing and due diligence" not in engine._hold_corrections(
             0, CARD_1191[0][2].replace("Best regards,", "On the compliance side, since you're part of a regulated "
                                        "bank, your security and compliance teams will want to review our outsourcing "
                                        "and due diligence documentation.\n\nBest regards,"),
             "", corrections=CARD_1191))
    case("an untouched draft passes through unchanged", "card 1191",
         lambda: engine._hold_corrections(0, CARD_1191[0][2], "", corrections=CARD_1191).strip()
         == CARD_1191[0][2].strip())
    case("a sentence he asks for again is left alone", "card 1163",
         lambda: "outsourcing and due diligence" in engine._hold_corrections(
             0, CARD_1191[0][2] + "\n\nOn the compliance side, since you're part of a regulated bank, your security and "
             "compliance teams will want to review our outsourcing and due diligence documentation.",
             "put back the paragraph about outsourcing and due diligence documentation for compliance", corrections=CARD_1191))

    # --- named films, by meaning (AI drone showreel never linked on Sky Vision card 1186)
    case("'AI drone show reel' finds the showreel (Sky Vision)", "card 1186",
         lambda: "zFM7OW-9Iqk" in deck.films_in_text(4, "Can you include a link to the AI drone show reel?"))
    case("'the Mercedes video with Al Rahba' finds it", "film matching",
         lambda: any("Mercedes" in (db.one("select title from media_assets where youtube_video_id=%s", (i,)) or {})
                     .get("title", "") for i in deck.films_in_text(3, "add the Mercedes video we did with Al Rahba")),
         model=True)
    case("words with no film in them find no film", "film matching",
         lambda: deck.films_in_text(3, "thanks for the call today") == [])

    # --- figures read as he writes them (SEN-2026-0033 crew line blank, 8 Oct)
    case("'42Kaed' is 42,000", "SEN-2026-0033", lambda: 42000 in {int(x) for x in engine._stated_numbers(
        "production crew will be 42Kaed total, camera 4K total")})
    case("'15,000.' is 15,000", "SEN-2026-0033", lambda: 15000 in {int(x) for x in engine._stated_numbers(
        "the budget is 15,000.")})

    # --- a weekday beside a date is computed (Al Hamra ack said Wednesday 12 October)
    case("weekday corrected against the calendar", "card 1110",
         lambda: "Monday 12 October 2026" in engine._fix_weekdays(
             "The deadline is Wednesday 12 October 2026.", datetime(2026, 10, 7, tzinfo=timezone.utc))[0])

    # --- structural guarantees
    case("the silence-chase list is retired", "Ekaterina, card 1180",
         lambda: engine.run_lead_followups().get("retired") is True)
    case("a reminder is Cortex's unless a person set it", "Liebherr reminder 311",
         lambda: inspect.signature(reminders.create).parameters["created_by"].default == "cortex")
    case("chases draft on sales-followup, not first response", "first-response rules",
         lambda: "sales-first-response" not in worker._RELATED_SKILLS_DEFAULT.get("sales-followup", ()))
    case("a rule about sign-offs goes to every email", "rule placement",
         lambda: worker.infer_rule(store.get_skill_by_key(3, "sales-first-response"),
                                   "Never sign off with Warm regards, always Best regards.", "", "")
         .get("place") == "email-handling", model=True)

    # --- a person's deal-page reminder is theirs (regression from the 13 Oct created_by change)
    case("deal-page reminder is filed as the person", "13 Oct map: api crm_project_reminder",
         lambda: "created_by=_person(u)" in inspect.getsource(__import__("cortex.api", fromlist=["x"]).crm_project_reminder))
    case("the owner is filed as rashad, a team member by name", "13 Oct map",
         lambda: __import__("cortex.api", fromlist=["x"])._person({"role": "owner"}) == "rashad"
         and __import__("cortex.api", fromlist=["x"])._person({"role": "member", "name": "Gino Palmes"}) == "gino")
    # --- a scheduled email on a deal that closed is parked, never sent
    case("scheduled send checks the deal stage first", "13 Oct map: scheduled chase on a Lost deal",
         lambda: "THE DEAL CLOSED AFTER IT WAS SCHEDULED" in inspect.getsource(engine.promote_due_tasks))

    # --- what a stage means is one table (audit step 2a)
    from . import stages
    case("Nurture and Close & review have no chase rhythm", "audit 2a: double contact / chasing a wrap-up",
         lambda: not stages.chases("Nurture") and not stages.chases("Close & review"))
    case("Quote chases on the closing rhythm, won work never does", "audit 2a: booked client on the quote rhythm",
         lambda: stages.rhythm("Quote") == "closing" and all(stages.rhythm(s) != "closing" for s in stages.WON))
    case("Dormant gets revivals only", "audit 2a: Dormant kept chasing",
         lambda: stages.rhythm("Dormant") == "revival"
         and all(s["action"] == "revive" for s in stages.REVIVAL_CADENCE["steps"]))
    case("a client's mail attaches to every stage but Lost", "audit 2a: Close & review replies never matched",
         lambda: stages.NO_CORRESPONDENCE == ("Lost",))
    case("delivery stages include Delivered", "audit 2a: _DELIVERY_STAGES lacked it",
         lambda: "Delivered" in engine._DELIVERY_STAGES and "Delivered" in __import__("cortex.pipeline", fromlist=["x"]).PLAN_STAGES)
    case("closing gate follows the stage, not a leftover cadence", "audit 2a",
         lambda: "stages.rhythm(stage) == \"closing\"" in inspect.getsource(engine._spawn_followup_card))

    # --- one contact gate for every automatic email (audit step 2b)
    from . import gate
    case("an email a person asked for is never gated", "audit 2b",
         lambda: gate.purpose({"brief": "reply to their email"}) is None
         and gate.check(3, "someone@example.com", 154, {"brief": "x"}) is None)
    case("no automatic email on a Lost deal", "Liebherr / Codexa",
         lambda: "Lost" in (gate.check(3, "someone@example.com", 154, {"followup": "chase"}) or ""))
    case("a Dormant deal gets revivals, not chases", "audit 2a/2b",
         lambda: "revival" in (gate.check(3, "someone@example.com", 94, {"followup": "chase"}) or ""))
    case("nobody who has left their company is contacted", "Tim Piper, EY",
         lambda: "left" in (gate.check(3, "tim.piper@ae.ey.com", None, {"followup": "nurture"}) or ""))
    case("a reminder email Cortex raised is gated, the owner's is not", "audit 2b",
         lambda: gate.purpose({"auto_source": "reminder"}) == "reminder" and gate.purpose({"system_note": "From reminder #5"}) is None)
    case("cards are gated where they are created and where they are released", "audit 2b",
         lambda: "gate.check(" in inspect.getsource(store.create_card) and "gate.check(" in inspect.getsource(store._release_gate))

    # --- one "they wrote to us" handler for every channel (audit step 2c)
    case("every inbound path tells the deal through events.they_wrote", "audit 2c",
         lambda: all("events.they_wrote(" in inspect.getsource(f) for f in (
             engine._draft_direct_reply, engine.poll_inbox, engine.poll_sales_replies,
             __import__("cortex.whatsapp", fromlist=["x"])._process_message)))
    case("a short reply still reaches the deal before the length check", "audit 2c: 'Thanks, will review'",
         lambda: inspect.getsource(engine._draft_direct_reply).index("events.they_wrote(")
         < inspect.getsource(engine._draft_direct_reply).index("len(body) < 40"))
    case("a colleague matched by domain finds the deal", "Mike Enting / deal 108",
         lambda: any(int(d["id"]) == 108 for d in __import__("cortex.events", fromlist=["x"])._deals(
             _co("tabscanner"), "someone.new@nl.abnamro.com", None)))

    # --- what Cortex owes completes itself (audit step 2d)
    from . import obligations
    case("every owed-reminder title has a kind", "MENA Water reminder 334",
         lambda: obligations.kind_of({"title": "Prepare and send the proposal — X"}) == "proposal_owed"
         and obligations.kind_of({"title": "Client deadline 2026-10-20: x"}) == "client_deadline"
         and obligations.kind_of({"title": "anything", "created_by": "cortex-commitment"}) == "conversation_commitment")
    case("sending a quotation settles 'send the proposal'", "MENA Water reminder 334",
         lambda: bool(obligations._QUOTE_RX.search("MENA Water - Quotation SEN-2026-0035 v1.pdf")))
    case("a sent email, a stage change, a closed card and a new contact all settle", "audit 2d",
         lambda: "obligations.on_sent(" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).record_send)
         and "obligations.on_stage(" in inspect.getsource(__import__("cortex.crm", fromlist=["x"]).set_project_stage)
         and "obligations.on_task_closed(" in inspect.getsource(store.update_task)
         and "obligations.on_contact_added(" in inspect.getsource(__import__("cortex.crm", fromlist=["x"]).add_deal_contact))
    case("same-day reminders never merge across authors or actions", "audit 13 Oct",
         lambda: "coalesce(created_by,'')=%s and action is null" in inspect.getsource(reminders.create))

    # --- one intake: the same steps on every channel (audit step 2e, batch 1)
    case("an existing client on the web form is treated as a client", "audit 2e",
         lambda: "events.they_wrote(" in inspect.getsource(engine.intake_enquiry)
         and "deal_id=(opp or {}).get(\"id\")" in inspect.getsource(engine.intake_enquiry))
    case("the no-draft policy reads every email lane", "audit 2e: rules moved off first response",
         lambda: set(policy.NO_DRAFT_LANES) == {"sales-first-response", "sales-followup", "email-handling"})
    case("a sales-mailbox reply is claimed once like any email", "audit 2e",
         lambda: "_claim_mail(" in inspect.getsource(engine.poll_sales_replies))
    case("qualification reads the universal rules too", "audit 2e",
         lambda: "worker._rule_lines(skill)" in inspect.getsource(engine.qualify_suggest))

    # --- one intake, batch 2 (audit step 2e)
    case("a second open prep card for the same deal returns the first", "audit 2e",
         lambda: "ONE OPEN PREP CARD PER DEAL AND PURPOSE" in inspect.getsource(store.create_card))
    case("a recurring job never stacks an occurrence on an open one", "audit 2e",
         lambda: "parent_id=%s and status in" in inspect.getsource(engine._spawn_recurring_child))
    case("LinkedIn uses the one classifier", "audit 2e",
         lambda: "engine.classify_email(" in inspect.getsource(__import__("cortex.social_dm", fromlist=["x"])._classify_dm))
    case("a WhatsApp card carries its opportunity", "audit 2e",
         lambda: '"deal_id": deal_id} if deal_id' in inspect.getsource(__import__("cortex.whatsapp", fromlist=["x"])._process_message))

    # --- tidy-up (audit step 2e)
    from . import identity
    case("one definition of 'this address is us'", "audit 2e tidy-up",
         lambda: identity.is_internal("gino@sensa.digital") and not identity.is_internal("client@abnamro.com")
         and not identity.is_internal("a.client@gmail.com") and identity.is_internal("rashadalsafar@gmail.com")
         and engine._is_internal("ayresh@sensa.digital", "", engine._internal_index()))
    case("a departure auto-reply is read whoever sends it", "audit 2e tidy-up",
         lambda: "AN AUTO-REPLY IS READ WHOEVER SENDS IT" in inspect.getsource(engine.poll_inbox))

    # --- a tender reaching a sister brand lands on the existing deal (Wasl RFQ 2026_1170, deals 162/163)
    from . import crm as _crm
    case("the buyer's RFQ code is read from the subject", "Wasl RFQ 2026_1170",
         lambda: _crm.tender_code("Invitation to RFQ - Request for Quotation Code 2026_1170 on Wasl Asset Management") == "2026_1170"
         and _crm.tender_code("TENDER INVITATION DUBAI POLICE-RFQ- 815902,2 (Visitor counting)") == "815902"
         and _crm.tender_code("Rafed Invitation To Proposal #RAF-REQ-0000161100") == "RAF-REQ-0000161100"
         and _crm.tender_code("Video production enquiry") == "")
    case("Sensa, Sky Vision and FilmSpoke are sister brands", "Wasl RFQ 2026_1170",
         lambda: set(_crm.sister_slugs("skyvision")) == {"sensa", "skyvision", "filmspoke"} and _crm.sister_slugs("tabscanner") == ["tabscanner"])
    case("a Sky Vision invitation finds the Sensa tender deal", "Wasl RFQ 2026_1170",
         lambda: (_crm.find_tender_deal("skyvision", "eproc@wasl.ae",
                  "Invitation to RFQ - Request for Quotation Code 2026_1170 on Wasl Asset Management") or {}).get("id") == 162)

    # --- remaining map items (13 Oct)
    case("a meeting counts as contact in the gate", "audit map: chase the day after a call",
         lambda: "A MEETING IS CONTACT" in inspect.getsource(gate._check)
         and "_meeting_touches_clock(" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).record_meeting))
    case("a scheduled email re-checks its recipient when it fires", "audit map",
         lambda: "left-company" in inspect.getsource(engine.promote_due_tasks))
    case("the tender notice is linked to its deal", "Wasl notice 1130",
         lambda: "target_type\": \"deal\"" in inspect.getsource(engine._flag_skipped_opportunity))

    # --- the notification policy (owner, 13 Oct: one per deal and topic; covered by a card; daily summary)
    from . import notifications as _nt
    case("contact-captured notices go to the daily summary", "notification audit",
         lambda: _nt._treatment("New vendor captured", "fyi", "contact", None, None) == "digest"
         and _nt._treatment("New opportunity: X", "normal", "lead", "deal", "1") == "digest")
    case("security notices always stand alone", "notification audit",
         lambda: _nt._treatment("Suspected phishing from x", "critical", "security", None, None) == "keep")
    case("a client-deadline notice is kept, one per deal and topic", "notification audit",
         lambda: _nt._treatment("Client deadline 2026-10-14: X", "high", "reminder", "deal", "133") == "keep"
         and _nt._topic("Client deadline 2026-10-14: X") == _nt._topic("Client deadline 2026-10-20T15:00: Y") == "client-deadline")
    case("a next step is announced once, tender deals never 'Unanswered'", "notification audit",
         lambda: "Next step (reminder)" not in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).suggest_next_step)
         and "Tender:" in inspect.getsource(engine._rearm_stalled_chases))
    case("our reply clears 'Unanswered'", "Rakez notice 1125",
         lambda: "obligations.on_reply_sent(" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).record_send))

    # --- forward to Cortex (owner, 13 Oct: Ayresh's Wasl forward did nothing)
    _fw_body = ("FYI please see below\n\n---------- Forwarded message ---------\nFrom: wasl eProcurement <eproc@wasl.ae>\n"
                "Date: Mon, Oct 12, 2026 at 7:49 PM\nSubject: Invitation to RFQ - Request for Quotation Code 2026_1170\n"
                "To: <fly@skyvision.film>\n\nDear Supplier,\n\nwasl Asset Management invites you to participate...")
    case("a forward is handled as the email inside it", "Wasl forward by Ayresh",
         lambda: (engine._forwarded_original({"email": "ayresh@sensa.digital", "name": "Ayresh", "subject": "Fwd: x",
                                              "body": _fw_body}) or {}).get("email") == "eproc@wasl.ae"
         and "Invitation to RFQ" in (engine._forwarded_original({"email": "a@sensa.digital", "body": _fw_body}) or {}).get("subject", ""))
    case("an ordinary internal email is not a forward", "forward to Cortex",
         lambda: engine._forwarded_original({"email": "gino@sensa.digital", "body": "Hi team, lunch at 1?"}) is None)
    case("the owner's other businesses are internal", "card 1211, Flixton Manor",
         lambda: __import__("cortex.identity", fromlist=["x"]).is_internal("rashad@flixtonmanor.com"))
    case("a card link never swallows a closing bracket", "card 1208 Meet link",
         lambda: 'href="https://meet.google.com/ert-wnwx-cah"' in engine._linkify("already shared (https://meet.google.com/ert-wnwx-cah).")
         and "cah)" not in engine._linkify("(https://meet.google.com/ert-wnwx-cah)").split('href="')[1].split('"')[0])
    case("the quote check only reads messages about this deal", "MAH Gold deal 112",
         lambda: "theirs = [m for m in theirs if _deal_for_thread(m, [opp])]" in inspect.getsource(engine._closing_gate))
    case("several open deals: never the first, ask instead", "MAH Gold deal 112",
         lambda: "ask_which_deal(" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"])._sweep_mailbox)
         and "ask_which_deal(" in inspect.getsource(__import__("cortex.events", fromlist=["x"]).they_wrote))

    # --- a meeting linked to its deal (Shama Ramadan call, 13 Oct)
    from . import meetnotes as _mn
    case("a meeting with a two-deal contact goes to the deal its title names", "Shama Ramadan meeting",
         lambda: (_mn._bound_deal("", "Ramadan Meeting", "2000-01-01", [{"id": 1, "title": "Shama - Qabiala launch film"},
                                                                          {"id": 2, "title": "Shama - Ramadan commercial"}]) or {}).get("id") == 2
         and _mn._bound_deal("", "Catch up", "2000-01-01", [{"id": 1, "title": "A film"}, {"id": 2, "title": "B shoot"}]) is None)

    # --- WhatsApp is a door into Talk (owner, 13 Oct)
    case("the owner's pictures and longer messages go to Talk", "Farheen screenshot dropped",
         lambda: "_talk_buffer(" in inspect.getsource(__import__("cortex.whatsapp", fromlist=["x"])._owner_control)
         and "_talk_turn(" in inspect.getsource(__import__("cortex.whatsapp", fromlist=["x"])._talk_flush)
         and hasattr(__import__("cortex.api", fromlist=["x"]), "talk_from_whatsapp"))
    case("a caption is read with the picture", "WhatsApp Talk",
         lambda: __import__("cortex.whatsapp", fromlist=["x"])._owner_text(
             {"type": "image", "image": {"id": "x", "caption": "Farheen's number"}}) == "Farheen's number")

    case("a picture and a voice note sent together are one Talk turn", "Farheen screenshot + voice note",
         lambda: "_talk_buffer(" in inspect.getsource(__import__("cortex.whatsapp", fromlist=["x"])._owner_control)
         and "_TALK_RUN" in inspect.getsource(__import__("cortex.whatsapp", fromlist=["x"])._talk_flush))

    # --- the full meeting notes, not the email summary (card 1212, Ramadan meeting, 13 Oct)
    import base64 as _b64
    _html = ('<p>Notes</p><a href="https://docs.google.com/document/d/1ENkEIrmaogZ9rY2WGLMYBa9AzM6wjGHK1MAT-hRlNxw/'
             'edit?usp=meet_tnfm_email">Open meeting notes</a>')
    _payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64.urlsafe_b64encode(b"Notes from the meeting").decode()}},
        {"mimeType": "text/html", "body": {"data": _b64.urlsafe_b64encode(_html.encode()).decode()}}]}
    case("the notes email's document is found from its button", "card 1212",
         lambda: _mn._doc_in_email(_payload, "Notes from the meeting") == "1ENkEIrmaogZ9rY2WGLMYBa9AzM6wjGHK1MAT-hRlNxw")
    case("a Gmail message id is not a notes document; a doc id is", "every meeting stored twice",
         lambda: not _mn._is_doc_id("1a1353a14969790e") and _mn._is_doc_id("1ENkEIrmaogZ9rY2WGLMYBa9AzM6wjGHK1MAT-hRlNxw"))
    case("the email path reads the document and waits for it", "card 1212",
         lambda: all(k in inspect.getsource(_mn.sweep_email) for k in ("_full_notes(", "FULL_NOTES_WAIT_MIN", "_upgrade_notes(")))
    case("the calendar path upgrades a short record, never twins it", "every meeting stored twice",
         lambda: "_short_copy_of(" in inspect.getsource(_mn.sweep) and "_upgrade_notes(" in inspect.getsource(_mn.sweep))
    case("fuller notes redraft the waiting follow-up as the system, not as the owner", "card 1212",
         lambda: "refresh_from_notes(" in inspect.getsource(_mn._refresh_followup)
         and "apply_correction" not in inspect.getsource(_mn._resumed_session)
         and "_hold_corrections(" in inspect.getsource(engine.refresh_from_notes))
    case("a date-only notes subject is the meeting's Dubai day", "Ramadan Meeting row 32",
         lambda: _mn._dubai_day("2026-10-12 20:00:00+00:00") == "2026-10-13"
         and _mn._dubai_day("2026-10-13T16:00:00+04:00") == "2026-10-13")
    case("a client on Gmail is an attendee; our own Gmail is not", "Learning App meeting",
         lambda: not _mn._is_us("lunamansour92@gmail.com") and _mn._is_us("hello@sensa.digital"))

    # --- the draft names its own sender (card 1212: sent from rashad@, "discussed with you and Rashad")
    _sensa = _co("sensa")
    _r1212 = ("Hello Farheen,\n\nThank you for the time today, it was a pleasure to have the Ramadan film discussed "
              "with you and Rashad this afternoon.\n\nBest regards,")
    case("a draft naming its own sender as someone else is caught", "card 1212",
         lambda: bool(engine._identity_mismatch(_r1212, "rashad@sensa.digital", _sensa))
         and not engine._identity_mismatch(_r1212, "gino@sensa.digital", _sensa)
         and not engine._identity_mismatch("Hello Farheen,\n\nThank you for the time today.\n\nBest regards,",
                                           "rashad@sensa.digital", _sensa))

    # --- Talk can edit an opportunity (deal 156: no rename, no way to add the meeting's people)
    case("Talk can rename an opportunity and add a meeting's people", "deal 156, Learning App meeting",
         lambda: "update_opportunity" in {t["name"] for t in __import__("cortex.api", fromlist=["x"]).SKILL_TOOLS}
         and "update_opportunity" in inspect.getsource(__import__("cortex.capabilities", fromlist=["x"]))
         and 'e["title"] or e.get("attendees")' in inspect.getsource(__import__("cortex.api", fromlist=["x"])._meeting_on))
    case("a file sent on WhatsApp is kept for the next message", "iTellu brief PDF",
         lambda: "save_data_url(" in inspect.getsource(__import__("cortex.api", fromlist=["x"]).talk_from_whatsapp))

    # --- personal touch: WhatsApp and calls beside the email track (owner, 13 Oct)
    from . import touch as _t
    _sig = ("Thanks,\nSarah Khan\nMarketing Manager | Emaar\nT: +971 4 366 1688\nM: +971 50 123 4567\n"
            "Quote ref SEN-2026-0021, 13/10/2026\n\nOn Mon, 12 Oct 2026, Gino <gino@sensa.digital> wrote:\n"
            "> call me on +971 55 999 8888")
    case("the mobile is picked from a signature, not the office line", "personal touch",
         lambda: (_t.pick(_t.numbers_in(_sig)) or {}).get("e164") == "+971501234567")
    case("a number in the quoted reply below is not theirs", "personal touch",
         lambda: "+971559998888" not in {n["e164"] for n in _t.numbers_in(_sig)})
    case("a local UAE mobile becomes +971; a quote number is not a phone", "personal touch",
         lambda: (_t.normalise("050 123 4567") or {}).get("e164") == "+971501234567"
         and _t.numbers_in("Quotation SEN-2026-0021 dated 13/10/2026, total 236,250") == [])
    case("a landline is call-only", "personal touch",
         lambda: (_t.normalise("+971 4 366 1688") or {}).get("mobile") is False)
    case("touch reminders fire their own notice and move on", "personal touch",
         lambda: "touch.fire(" in inspect.getsource(reminders.fire)
         and "touch_made" in inspect.getsource(__import__("cortex.gate", fromlist=["x"])._check)
         and "cortex-touch" in inspect.getsource(__import__("cortex.obligations", fromlist=["x"]).on_stage))
    case("every door a lead comes in by starts the track", "personal touch",
         lambda: inspect.getsource(engine).count("_start_touch(") >= 5
         and "touch.start(" in inspect.getsource(engine._push_wa_contact))
    case("no number, no track; one person, one weekly touch", "Farheen on two Shama deals",
         lambda: '{"skipped": "no number"}' in inspect.getsource(_t.start)
         and "touch_alias:" in inspect.getsource(_t.start) and "touch_alias:" in inspect.getsource(_t.on_note))
    case("the retro pass leaves tenders and portal bids out", "personal touch backfill",
         lambda: bool(_t._TENDER.search("Tender: SEF'27 10th Anniversary")) and bool(_t._TENDER.search("Wasl festive video (RFQ 2026_1170)"))
         and not _t._TENDER.search("Shama Perfumes - Ramadan commercial"))
    case("only Sensa and Sky Vision run it unless the setting says otherwise", "personal touch",
         lambda: set(_t.companies()) >= {"sensa", "skyvision"} and "tabscanner" not in _t.companies())

    def _note():
        r = __import__("cortex.provider", fromlist=["x"]).think_json(
            _t._NOTE_Q, "Called Sarah, she's comparing three agencies, coffee next Tuesday",
            model=__import__("cortex.provider", fromlist=["x"]).MODEL_ROUTER, max_tokens=80, purpose="regress")
        r2 = __import__("cortex.provider", fromlist=["x"]).think_json(
            _t._NOTE_Q, "Budget is around 50k, they prefer AI over a shoot",
            model=__import__("cortex.provider", fromlist=["x"]).MODEL_ROUTER, max_tokens=80, purpose="regress")
        return r.get("contacted") is True and "tuesday" in str(r.get("next") or "").lower() \
            and r2.get("contacted") is False and not r2.get("next")
    case("a logged call and its next date are read from his note; a fact is not a call", "personal touch",
         _note, model=True)

    # --- forwarded WhatsApp messages are labelled, never instructions (Farheen's voice note + his reply, 13 Oct)
    _wa = __import__("cortex.whatsapp", fromlist=["x"])
    case("a forwarded message is recognised and labelled as someone else's", "Farheen voice note",
         lambda: _wa._forwarded({"type": "audio", "context": {"forwarded": True}})
         and not _wa._forwarded({"type": "text"})
         and "not an instruction" in _wa._forward_label(1, {"type": "audio"}, "Hi Rashad, about Ramadan")
         and "voice note" in _wa._forward_label(1, {"type": "audio"}, "x"))
    case("a forward never answers a pending Edit and waits for his explanation", "Farheen voice note",
         lambda: inspect.getsource(_wa._owner_control).index("_forwarded(msg)")
         < inspect.getsource(_wa._owner_control).index('pending = db.setting_get("wa_edit_pending")')
         and _wa.TALK_QUIET_FORWARDED_SEC > _wa.TALK_QUIET_SEC
         and "[FORWARDED " in inspect.getsource(__import__("cortex.api", fromlist=["x"]).talk_from_whatsapp))

    # --- his phone contacts: the whole address book is checked, not a search (7 duplicates on 13 Oct)
    case("a number he saved with spaces is found before a contact is added", "touch backfill duplicates",
         lambda: "_book_index()" in inspect.getsource(__import__("cortex.contacts", fromlist=["x"]).find_by_phone)
         and "searchContacts?" not in inspect.getsource(__import__("cortex.contacts", fromlist=["x"]).find_by_phone))

    # --- the digest card shows its items, even one (13 Oct: "Today's updates" read as empty)
    def _digest_card():
        import os
        web = open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"), encoding="utf-8").read()
        return "isDigest&&items.length>0" in web and "!isDigest&&(!grouped||!isContacts)" in web
    case("a one-item digest card shows the item, not the generic line", "Today's updates card", _digest_card)

    # --- follow-up audit (13 Oct): quoted deals on the closing rhythm; a failed draft stays visible
    case("a quoted deal off the closing rhythm is repaired by the health check", "Gulf Cryo 147, Unified Robotics 149",
         lambda: any(c["key"] == "quote_deal_off_closing_rhythm" and c.get("fix") == "crm.repair_quote_rhythm"
                     for c in __import__("cortex.invariants", fromlist=["x"]).CHECKS)
         and callable(getattr(__import__("cortex.crm", fromlist=["x"]), "repair_quote_rhythm", None)))
    case("a draft that fails stays in the Inbox with its reason", "card 1200",
         lambda: "A FAILED DRAFT IS NEVER SILENT" in inspect.getsource(engine)
         and 'dedup_key=f"draftfail:' in inspect.getsource(engine))

    # --- the follow-up rhythm (owner, 13 Oct): receipt check next working day, then weekly, then fortnightly until
    # they answer; he can set the next chase himself
    _crm = __import__("cortex.crm", fromlist=["x"])
    def _rhythm():
        pts = _crm.cadence_points(_crm.CLOSING_CADENCE)
        d = _crm.cadence_points(_crm.DEFAULT_CADENCE)
        rv = _crm.cadence_points(__import__("cortex.stages", fromlist=["x"]).REVIVAL_CADENCE)
        # owner, 19 Oct: the push after a quote (2 to 3 working days for two weeks) before the weekly step
        return (pts[0]["after_days"] == 1 and [p["after_days"] for p in pts[1:5]] == [2, 3, 2, 3]
                and pts[5]["after_days"] == 5 and pts[-1]["after_days"] == 10
                and len(pts) >= 30 and _crm.CLOSING_CADENCE.get("working_days")
                and max(p["after_days"] for p in d) <= 14 and all(p["after_days"] <= 14 for p in rv))
    case("submitted: receipt check next day, the 2-3 day push, then weekly, then every two weeks",
         "owner, follow-up audit + 19 Oct", _rhythm)
    case("a reply restarts at the weekly step; his own rhythm is never reset", "owner, follow-up audit",
         lambda: "followup_step=1" in inspect.getsource(_crm.enter_closing)
         and 'was == "owner"' in inspect.getsource(_crm.enter_closing))
    case("the receipt check is not held by the two-day waits", "owner, follow-up audit",
         lambda: "_need = 1 if not (opp.get(\"followup_step\") or 0) else 2" in inspect.getsource(engine._closing_gate)
         and "quiet = 0.8" in inspect.getsource(__import__("cortex.gate", fromlist=["x"])._check))
    case("he can set the next chase: deal page, note, Talk", "owner, follow-up audit",
         lambda: callable(getattr(_crm, "set_next_chase", None))
         and "set_next_chase(" in inspect.getsource(_t.on_note)
         and "set_next_chase(" in inspect.getsource(__import__("cortex.api", fromlist=["x"])._update_opportunity)
         and hasattr(__import__("cortex.api", fromlist=["x"]), "crm_project_next_chase"))

    # --- an offer is never a commitment (MENA Water deal 155, cards 1220/1221, 14 Oct)
    _pl = __import__("cortex.pipeline", fromlist=["x"])
    _mena = ("If you would prefer a physical drone shoot with real licensed cinema aerials instead, that remains an option, "
             "though it runs on a longer timeline. We would be glad to quote that route separately alongside this one, so "
             "you can see both side by side.")
    case("'we would be glad to quote' is an offer; 'we will send it tomorrow' is a promise", "card 1221",
         lambda: _pl._is_offer("We would be glad to quote that route separately alongside this one")
         and _pl._is_offer("that remains an option") and _pl._is_offer("Happy to walk you through it on a call")
         and not _pl._is_offer("We will send the revised quotation tomorrow")
         and not _pl._is_offer("I'll send the storyboard across by Thursday"))
    case("a commitment must quote a sentence that is really in the email", "card 1221",
         lambda: _pl._in_email("We would be glad to quote that route separately", _mena)
         and not _pl._in_email("We will send a separate drone quotation by Thursday", _mena))
    case("deadlines and next steps read only their new words, not ours quoted below", "MENA Water deadline",
         lambda: __import__("cortex.gmail", fromlist=["x"]).new_part(
             "Dear Gino,\nThank you for the quotation. We will review it.\nThanks\nFrom: gino@sensa.digital <gino@sensa.digital>\n"
             "Sent: Monday\napprovals come back within one working day at each gate").endswith("We will review it.\nThanks")
         and "new_part(" in inspect.getsource(_pl.record_inbound) and "new_part(" in inspect.getsource(_pl.suggest_next_step))
    case("not needed closes the chain: skip, a word on the prep card, or the email card", "card 1221",
         lambda: "_cancel_owed_work(" in inspect.getsource(engine._skip)
         and "_cancels_work(text)" in inspect.getsource(engine.apply_correction)
         and '"retract"' in inspect.getsource(engine._understand_correction)
         and "cancel_matching(" in inspect.getsource(engine._apply_understood))
    case("the same work is matched on three meaningful words, never one", "card 1221",
         lambda: __import__("cortex.obligations", fromlist=["x"])._same(
             "Commitment owed to a@b.com: Provide separate quotation for drone shoot option with real licensed cinema aerials",
             "Next step: Separate quotation for drone shoot option with licensed cinema aerials")
         and not __import__("cortex.obligations", fromlist=["x"])._same("Send the quotation", "Revised quotation for Rakez"))

    def _mena_extract():
        cs = _pl.extract_commitments("Hello Alanraj,\n\n" + _mena + "\n\nBest regards,", stage="Quote")
        return all(c.get("offer") for c in cs)
    case("the MENA email owes nothing", "card 1221", _mena_extract, model=True)

    # --- the quotation flow from any card or note (owner, 14 Oct: Heidar at 25,000, one round, 50/50)
    from . import quotation as _qq
    case("revision rounds are restated everywhere, in good grammar", "quotation flow",
         lambda: _qq.set_rounds("Revisions: Two revision rounds are included per project", 1)
         == "Revisions: One revision round is included per project"
         and _qq.set_rounds("Rounds beyond the two included", 1) == "Rounds beyond the one included"
         and _qq.set_rounds("two rounds of revisions are included per Contract", 3)
         == "three rounds of revisions are included per Contract")
    def _split():
        ok = _qq.split_lines([{"pct": 50}, {"pct": 50}])[0].startswith("50% on confirmation")
        try:
            _qq.split_lines([{"pct": 60}, {"pct": 30}])
            return False
        except ValueError:
            return ok and "50% down payment" in str(_qq.set_split_terms({"a": "70% down payment is required"}, 50))
    case("a payment split must make 100%, and the terms' deposit follows it", "quotation flow", _split)
    case("words with nothing for the quotation never cost a model call", "quotation flow",
         lambda: not engine._QUOTE_HINT.search("Make the opening warmer and drop the last line")
         and engine._QUOTE_HINT.search("go ahead at 25,000 + VAT") and engine._QUOTE_HINT.search("50/50 split")
         and engine._QUOTE_HINT.search("On option B, drop the vertical cut-down")
         and engine._QUOTE_HINT.search("add another option without the voice over"))
    case("every quotation card is approved, and approval makes it live", "quotation flow",
         lambda: "_quote_to_email(" in inspect.getsource(engine._approve_quotation_card)
         and "'Mark as seen'" not in open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "..",
                                                                      "web", "index.html"), encoding="utf-8").read()
         .split("function quotationCard")[1].split("function ")[0])
    case("a decision on an email card builds the quotation; the email waits for it", "quotation flow",
         lambda: "build_quote_from_words(" in inspect.getsource(engine.apply_correction)
         and '"waiting_quote"' in inspect.getsource(engine.apply_correction)
         and "_wq" in inspect.getsource(engine.approve_task))
    case("a note on the deal redirects it", "quotation flow",
         lambda: "note_redirect(" in inspect.getsource(__import__("cortex.crm", fromlist=["x"]).add_project_note))
    case("a quote built from one total is revised from its printed text; trims never claim prices", "SEN-2026-0027",
         lambda: '"printed"' in inspect.getsource(engine.build_quote_from_words)
         and "tier|discount" in inspect.getsource(engine.build_quote_from_words))
    case("a reissue keeps an agreed split and the revision rounds", "quotation flow",
         lambda: 'sp.get("payment")' in inspect.getsource(engine.reissue_quotation)
         and 'sp.get("revision_rounds")' in inspect.getsource(engine.reissue_quotation))

    def _intent():
        i = engine._quote_intent("He's sticking hard at 25,000. We'll go ahead at 25,000 + VAT with a 70% deposit "
                                 "and one revision round.")
        n = engine._quote_intent("He asked for 20,000 but I haven't agreed to anything yet, just acknowledge it.")
        return bool(i) and i.get("target") == 25000 and i.get("rounds") == 1 and \
            [p["pct"] for p in i.get("payment") or []] == [70, 30] and not (n and n.get("target"))
    case("his decision is read: 25,000, one round, 70/30; a client's ask is not a decision", "quotation flow",
         _intent, model=True)

    # --- no opportunity without a contact; a quotation always belongs to one (Merck 161, 14 Oct)
    _cr = __import__("cortex.crm", fromlist=["x"])
    case("an opportunity cannot be created without a contact", "Merck Future Health Summit",
         lambda: not _cr.valid_contact({}) and not _cr.valid_contact({"name": "Ricola"})
         and _cr.valid_contact({"email": "a@b.com"}) and _cr.valid_contact({"phone": "+971 50 123 4567"})
         and "DealNeedsContact" in inspect.getsource(_cr.create_deal))
    case("every creation path passes its contact", "Merck Future Health Summit",
         lambda: "contact=" in inspect.getsource(_cr.qualify_opportunity)
         and 'contact={"email": email, "role": "issuer"}' in inspect.getsource(engine)
         and "contact=" in inspect.getsource(__import__("cortex.whatsapp", fromlist=["x"])))
    case("Talk asks for the designated contact and ties a quotation to its opportunity", "Merck Future Health Summit",
         lambda: "designated contact" in inspect.getsource(__import__("cortex.api", fromlist=["x"])._exec_skill_tool)
         and "which opportunity is it for" in inspect.getsource(__import__("cortex.api", fromlist=["x"])._exec_skill_tool))

    # --- one conversation, one draft, kept current (DSO cards 1219 / 1222 / 1223, 14 Oct)
    _cv = __import__("cortex.conversation", fromlist=["x"])
    _c1219 = {"inquiry": {"email": "con_anoronha@dso.ae", "subject": "Dubai Silicon Oasis - Video production enquiry"},
              "thread": {"id": "1a11aec4096dfe7a", "msg_id": "<ours@mail.gmail.com>"}}
    _alex = {"inquiry": {"email": "con_anoronha@dso.ae", "subject": "RE: Dubai Silicon Oasis - Video production enquiry",
                         "message": "Thank you for the update and looking forward to your proposal."},
             "thread": {"id": "1a11ad664699b526", "msg_id": "<theirs@outlook.com>", "references": "<x> <ours@mail.gmail.com>"},
             "gmail_id": "1a13893336e93c7e", "thread_reply": True}
    _other = {"inquiry": {"email": "con_anoronha@dso.ae", "subject": "Invoice query for September",
                          "message": "Can you resend the September invoice?"}, "thread": {"id": "zz", "references": ""},
              "gmail_id": "x", "thread_reply": True}
    case("his reply on the same thread is the same conversation; a new subject is not", "DSO 1219/1222",
         lambda: _cv.same_thread(_c1219, _alex) and not _cv.same_thread(_c1219, _other)
         and _cv.is_inbound(_alex) and not _cv.is_inbound({"inquiry": {"email": "a@b.com"}, "followup": "chase"}))
    case("a folded reply keeps the card's own thread id and takes their reply headers", "DSO 1219",
         lambda: "THREAD IDS ARE MAILBOX-LOCAL" in inspect.getsource(_cv.absorb))
    case("two conversations with one person get two queues; one conversation one", "two threads",
         lambda: _cv.thread_key(_alex) == _cv.thread_key({"inquiry": {"email": "CON_anoronha@dso.ae",
                                                                      "subject": "Re: Re: Dubai Silicon Oasis - Video production enquiry"}})
         and _cv.thread_key(_alex) != _cv.thread_key(_other))
    case("every card creation folds into the waiting conversation", "DSO 1219/1222",
         lambda: "conversation.absorb(" in inspect.getsource(store.create_card)
         and "conversation.thread_key(" in inspect.getsource(store.create_card)
         and "recheck_deal(" in inspect.getsource(__import__("cortex.events", fromlist=["x"]).they_wrote))
    case("only the same sweep may retry its claim; queued cards count as open", "DSO 1222/1223",
         lambda: 'handler="sales"' in inspect.getsource(engine.poll_sales_replies)
         and "('queued','new','drafting'" in inspect.getsource(engine.poll_sales_replies)
         and "'queued'" in inspect.getsource(engine._rejudge_pending_replies))

    # --- quotation options: Option A and Option B (owner, 14 Oct)
    case("an option is titled by its letter, never twice", "quotation options",
         lambda: engine._option_title("B", "AI brand film") == "Option B: AI brand film"
         and engine._option_title("A", "Option B: AI brand film") == "Option A: AI brand film")
    case("the pricing page lists every option, headline the lowest", "quotation options",
         lambda: deck.investment_from_options([{"letter": "A", "title": "Option A: Full film", "net": 40000},
                                               {"letter": "B", "title": "Option B: Lite", "net": 25000}])["headline"]
         == "From AED 25,000 + VAT")
    case("a creative deck's pricing page lists every option too", "quotation options",
         lambda: 'q.get("options")' in inspect.getsource(__import__("cortex.creative", fromlist=["x"]).Job.quote_rows)
         and "deal_options(" in inspect.getsource(__import__("cortex.creative", fromlist=["x"]).adopt_quotation))
    case("the email waits for every option; signing one withdraws the others", "quotation options",
         lambda: "deal_options(" in inspect.getsource(engine._quote_to_email)
         and "deal_options(" in inspect.getsource(engine._confirm_signed)
         and "deal_options(" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).record_quotation_sent))

    def _opt_intent():
        i = engine._quote_intent("Keep the current quote, and add a second option at 18,000 + VAT without the voice over.")
        j = engine._quote_intent("On option B, drop the vertical cut-down.")
        return bool(i) and i.get("mode") == "option" and i.get("target") == 18000 and bool(j) and j.get("option") == "B"
    case("'add a second option' is an option; 'on option B' targets B", "quotation options", _opt_intent, model=True)

    # --- an open cockpit never runs old code against a newer server (Gino's New deal form, 14 Oct 2026)
    import re as _re_b
    _web = __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(
        __import__("os").path.dirname(__import__("os").path.abspath(__file__)))), "web")
    _sw = open(_web + "/sw.js", encoding="utf-8").read()
    _ix = open(_web + "/index.html", encoding="utf-8").read()
    _api = __import__("cortex.api", fromlist=["x"])
    case("the app's build equals the service worker's cache and the server reads it", "Gino 14 Oct",
         lambda: bool(_api.cockpit_build()) and f"const CACHE = '{_api.cockpit_build()}'" in _sw
         and "'X-Cortex-Build':APP_BUILD" in _ix and "checkBuild(true)" in _ix)
    case("a write from another build is refused with a reload message; an old form is told it is out of date",
         "Gino 14 Oct",
         lambda: "stale_build" in inspect.getsource(_api._refuse_stale_cockpit)
         and "model_fields_set" in inspect.getsource(_api.crm_create_deal))
    case("it is an opportunity, never a deal, on the create form and in Talk", "owner 14 Oct",
         lambda: "New deal" not in _ix and "Create deal" not in _ix and "Deal name" not in _ix
         and any(t["name"] == "create_opportunity" for t in _api.SKILL_TOOLS)
         and not any(t["name"] == "create_deal" for t in _api.SKILL_TOOLS))

    # --- a team member talks to Cortex on WhatsApp as themselves (owner, 14 Oct 2026: Gino)
    _wa = __import__("cortex.whatsapp", fromlist=["x"])
    case("a team member's number goes to Talk as them, with their scope, answered on their phone", "Gino WhatsApp",
         lambda: "_team_message(" in inspect.getsource(_wa.ingest_cloud)
         and "user=" in inspect.getsource(_wa._talk_turn) and "to=to" in inspect.getsource(_wa._talk_turn)
         and "user if team else owner" in inspect.getsource(__import__("cortex.api", fromlist=["x"]).talk_from_whatsapp)
         and _wa.team_member("") is None)

    # --- Ayresh's mail is hers by hand (owner, 14 Oct 2026)
    _co3 = {"id": -1, "slug": "x"}
    _orig_mp = engine.manual_people
    engine.manual_people = lambda co: {"ayresh@sensa.digital"}
    try:
        _hm = engine.handled_manually(_co3, {"to": "Ayresh <ayresh@sensa.digital>", "email": "jlion@d-marin.com"})
        _hn = engine.handled_manually(_co3, {"to": "gino@sensa.digital", "email": "nobody@example.invalid"})
    finally:
        engine.manual_people = _orig_mp
    case("an email to a manual person never becomes a card; their deals are never auto-chased", "D-Marin 1233",
         lambda: _hm == "ayresh@sensa.digital" and _hn == ""
         and "handled_manually(" in inspect.getsource(engine.poll_inbox)
         and "handled_manually(" in inspect.getsource(engine.poll_sales_replies)
         and "_manual_owner(" in inspect.getsource(__import__("cortex.gate", fromlist=["x"])._check))
    # --- a big PDF sent to Talk is read as text, never dropped (13 Oct, 21MB brief on WhatsApp)
    import base64 as _b64
    _big = "data:application/pdf;base64," + _b64.b64encode(b"%PDF-1.4 " + b"0" * (11 * 1024 * 1024)).decode()
    _blk = __import__("cortex.api", fromlist=["x"])._image_blocks([_big], ["brief.pdf"])
    case("a PDF over the page limit becomes a text block saying so", "21MB brief",
         lambda: len(_blk) == 1 and _blk[0]["type"] == "text" and "too large" in _blk[0]["text"]
         and "cap=documents.MAX_DEAL_BYTES" in inspect.getsource(__import__("cortex.api", fromlist=["x"]).talk_from_whatsapp))

    # --- invites raise no notice; the digest arrives at the end of the day (owner, 14 Oct 2026)
    from datetime import datetime as _dtn, timezone as _tzn
    _nt = __import__("cortex.notifications", fromlist=["x"])
    _d1, _due1 = _nt.digest_slot(_dtn(2026, 10, 14, 10, 0, tzinfo=_tzn.utc))    # 14:00 Dubai
    _d2, _due2 = _nt.digest_slot(_dtn(2026, 10, 14, 15, 0, tzinfo=_tzn.utc))    # 19:00 Dubai
    case("a calendar invitation is not mail that waits for a reply; a client's question is", "Farheen invite x2",
         lambda: engine._no_reply_mail({"subject": "Updated invitation: Shama x Sensa @ Thu Oct 15, 2026"})
         and engine._no_reply_mail({"subject": "Hello", "attachments": [{"mime": "text/calendar"}]})
         and not engine._no_reply_mail({"subject": "Re: the quotation", "attachments": []}))
    case("the digest is delivered at 18:00 Dubai; later items go to the next day's", "digest end of day",
         lambda: _d1 == "2026-10-14" and _due1.hour == 14 and _d2 == "2026-10-15" and _due2.day == 15
         and "'snoozed'" in inspect.getsource(_nt.notify))

    # --- "which opportunity?" can be answered, once per person, never for an invite (14 Oct 2026)
    _evm = __import__("cortex.events", fromlist=["x"])
    _req = {"candidates": [{"id": 164, "title": "Shama - Ramadan", "stage": "Opportunity"},
                           {"id": 130, "title": "Shama - Qabilah", "stage": "Quote"}]}
    _coS = {"id": 3, "slug": "sensa"}
    case("the card answers by tap, by its number, by a deal number, 'none' or 'a new job'", "Shama 1230/1231",
         lambda: _evm._resolve_pick(_coS, _req, "1")[1]["id"] == 164 and _evm._resolve_pick(_coS, _req, "3")[0] == "none"
         and _evm._resolve_pick(_coS, _req, "none of these")[0] == "none"
         and _evm._resolve_pick(_coS, _req, "it's a new job: Ramadan billboard") == ("new", "Ramadan billboard")
         and "dealPickCard(" in open(_web + "/index.html", encoding="utf-8").read()
         and "pick_id" in inspect.getsource(_evm.answer_which_deal))
    case("an invite never asks; a second message joins the open question", "Shama 1230/1231",
         lambda: "_no_reply_mail(message)" in inspect.getsource(_evm.ask_which_deal)
         and "ONE QUESTION PER PERSON" in inspect.getsource(_evm.ask_which_deal))
    case("an automatic email is held when a meeting with them is booked", "Shama #164 chase",
         lambda: "next_meeting_with(" in inspect.getsource(__import__("cortex.gate", fromlist=["x"])._check))

    # --- uploaded files count as attached; a new job opens its own thread; one copy of a core document (14 Oct)
    case("the read-through sees files uploaded onto the card", "Merck 1239",
         lambda: engine._outgoing_names({"attachments": [{"x": 1}], "attachment_names": ["OPTION 1.pdf"]}) == ["OPTION 1.pdf"]
         and "_outgoing_names(dreq)" in inspect.getsource(engine._read_through))
    case("a deal-linked card never continues a thread another deal owns", "Merck 1239 on #5's thread",
         lambda: "_another_deals(" in inspect.getsource(engine._adopt_existing_thread))
    case("a new build of a core document retires the earlier copies with its name", "Body Double x5",
         lambda: "retire_same_name(" in inspect.getsource(__import__("cortex.documents", fromlist=["x"]).save))

    # --- mail to the support mailbox is support, client or not; a client's email is never a tender (Sampras, 14 Oct)
    _pol = __import__("cortex.policy", fromlist=["x"])
    case("a role mailbox decides even for a client on a deal; a person's address does not", "Sampras 1232 / #165",
         lambda: bool(_pol._ROLE_LOCAL.match("support")) and not _pol._ROLE_LOCAL.match("ben")
         and "by_address" in inspect.getsource(engine.poll_inbox)
         and "if not _cdeals:" in inspect.getsource(engine.poll_inbox))

    # --- one record of what is owed; deliverables get lead time and never draft an email (owner, 14 Oct 2026)
    _rm = __import__("cortex.reminders", fromlist=["x"])
    _ob = __import__("cortex.obligations", fromlist=["x"])
    from datetime import datetime as _dtw, timezone as _tzw
    case("every Cortex reminder on a deal is checked against what is already open", "Shama #164 x3",
         lambda: "obligations.check_new(" in inspect.getsource(_rm.create)
         and "deliver_by" in inspect.getsource(_rm._commitment_action))
    case("a deliverable owed Friday reminds two working days before (Wednesday)", "Shama treatments",
         lambda: _ob.working_days_before(_dtw(2026, 10, 16, 12, tzinfo=_tzw.utc), 2).day == 14
         and _ob.working_days_before(_dtw(2026, 10, 19, 12, tzinfo=_tzw.utc), 2).day == 15)

    # --- an identical quotation is never reissued; a new quotation replaces only quotation files (card 1246, 14 Oct)
    _qe = {"spec": {"sections": [{"items": [{"desc": "Ideation, storyboard and shot list", "qty": 1, "unit": 7500.0},
                                            {"desc": "Shoot day, crew of four", "qty": 1, "unit": 20000.0}]}]},
           "payment": ["50% on confirmation", "50% on delivery"]}
    _sec = lambda d, u: [{"items": [{"desc": d, "qty": 1, "unit": 7500.0}, {"desc": "Shoot day, crew of four", "qty": 1, "unit": u}]}]  # noqa: E731
    case("a rebuild with the same lines and prices is the same quotation; a changed price or a new line is not",
         "SEN-2026-0043 v5 = v4",
         lambda: engine._same_quotation(_qe, _sec("Ideation, storyboard and the shot list", 20000.0), None, None)
         and not engine._same_quotation(_qe, _sec("Ideation, storyboard and shot list", 18000.0), None, None)
         and not engine._same_quotation(_qe, _sec("Ideation, storyboard and shot list", 20000.0), ["70% now", "30% later"], None)
         and not engine._same_quotation(_qe, _sec("Drone aerial package with licensed pilot", 20000.0), None, None)
         and not engine._same_quotation({**_qe, "spec": {**_qe["spec"], "note": "Usage 12 months GCC online."}},
                                        _sec("Ideation, storyboard and shot list", 20000.0), None, None,
                                        {"note": "Usage in perpetuity, worldwide, all media."})
         and "unchanged" in inspect.getsource(engine.apply_correction))
    _db = __import__("cortex.db", fromlist=["x"])
    _qd = _db.one("select id, filename from company_documents where kind='quotation' and filename ~ 'SEN-2026-0043 v4'")
    _pd = _db.one("select id, filename from company_documents where kind='proposal' and filename ~ 'SEN-2026-0043 v4'")
    _qn = _db.one("select id, filename from company_documents where kind='quotation' and filename ~ 'SEN-2026-0043 v5'")
    case("approving a new quotation takes off the old quotation, never the proposal deck", "Shamal 1246",
         lambda: not (_qd and _pd and _qn) or engine._replaced_attachments([_qd, _pd], [_qn], ["SEN-2026-0043"]) == [_qd])

    # --- a confirmed ad click is a lead; only their own words are judged (A.Yousef, 14 Oct 2026)
    _wam = __import__("cortex.whatsapp", fromlist=["x"])
    case("our pre-filled text and ref are not their words; a confirmed click opens the opportunity", "Waheguru NFFQ5",
         lambda: _wam.their_words("Hi Sensa, I need a video produced in Dubai. My project: (ref NFFQ5)") == ""
         and _wam.their_words("Hi Sensa, I need a video produced in Dubai. My project: (ref NFFQ5) a hotel launch film") == "a hotel launch film"
         and "verified or (sug or {}).get" in inspect.getsource(_wam._process_message))

    # --- "not interested" closes everything an email raised and counts as our answer (owner, 15 Oct 2026)
    case("one tap closes the reply, its question and its next step; the deal's chase carries on", "1276-1279",
         lambda: "not_pursued" in inspect.getsource(engine.not_interested)
         and "resume_followups(" in inspect.getsource(engine.not_interested)
         and "not_pursued" in inspect.getsource(__import__("cortex.gate", fromlist=["x"])._check)
         and "not_pursued" in inspect.getsource(engine._rearm_stalled_chases)
         and "notInterested(" in open(_web + "/index.html", encoding="utf-8").read())

    # --- one flow per opportunity: every event re-checks its open items (owner, 15 Oct 2026)
    _fl = __import__("cortex.flow", fromlist=["x"])
    _gt = __import__("cortex.gate", fromlist=["x"])
    from datetime import datetime as _dtf, timezone as _tzf
    _deal_m = {"cadence": {"name": "closing", "working_days": True, "steps": [{"after_days": 1, "repeat": 1},
                                                                             {"after_days": 5, "repeat": 8}]},
               "followup_step": 1, "company": "Sensa"}
    _fl_floor = _gt._rhythm_floor(_deal_m, [{"event": "email_out", "ts": "2026-10-13T05:36:18Z"}])
    case("every timeline event and every closed card queues the deal for a re-check", "Inbox 15 Oct",
         lambda: "flow.mark(" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).log_deal)
         and "flow.mark(" in inspect.getsource(store.update_task)
         and "reconcile_rows" in inspect.getsource(_fl))
    case("a chase waits its full step from our last email (Massar: 13 Oct -> 20 Oct, not 15 Oct)", "Massar 1262",
         lambda: _fl_floor is not None and _fl_floor.date().isoformat() == "2026-10-20"
         and "_rhythm_floor(" in inspect.getsource(_gt._check))
    case("a held reminder waits silently; a real failure names its deal", "Shamal reminder 363",
         lambda: "gate.last_hold()" in inspect.getsource(__import__("cortex.reminders", fromlist=["x"]).fire)
         and "reminder-fail:" in inspect.getsource(__import__("cortex.reminders", fromlist=["x"]).fire))
    case("the day's calls are one card; the cockpit reloads when the flow changes the Inbox", "7 call notices",
         lambda: "touch-day:" in inspect.getsource(__import__("cortex.touch", fromlist=["x"]).fire)
         and "checkInboxRev" in open(_web + "/index.html", encoding="utf-8").read())

    case("a 'declined' read needs the client's own no; our decision not to pursue a message is not it", "Unified Robotics 149",
         lambda: _fl._client_said_no({"history": [{"event": "email_in", "text": "We have decided to go with another supplier."}]})
         and not _fl._client_said_no({"history": [{"event": "email_in", "text": "Dear Supplier, please quote for the below"},
                                                  {"event": "not_pursued", "text": "We chose not to pursue one message"}]}))

    case("a version he names is copied even when a later version exists", "Shama SEN-2026-0021 v4",
         lambda: "if not version and any(_version_is_priced(e) for e in later)" in inspect.getsource(engine.reissue_quotation)
         and "broken = [e for e in later if not _version_is_priced(e)]" in inspect.getsource(engine.reissue_quotation))

    case("a proposal that went to the client is never re-stamped by a quotation change", "Shama card 815",
         lambda: "_sent = None if force else _proposal_sent(p)" in inspect.getsource(engine.restamp_proposal_price))
    case("a price re-stamp of a creative deck runs no copy writer", "Shama card 815",
         lambda: "stage_copy" not in inspect.getsource(__import__("cortex.creative", fromlist=["x"]).adopt_quotation))
    case("approving a quotation that already went out drafts no second email", "Shama card 1290",
         lambda: "so no new email was drafted" in inspect.getsource(engine._quote_to_email))

    def _review():
        from . import review
        d = db.one("select id from crm_projects where stage in ('Opportunity','Quote') order by id desc limit 1")
        r = review.summary(d["id"]) if d else {"steps": [{"id": "CHASE"}]}
        return bool(r and r["steps"] and r["steps"][0]["id"] == "CHASE" or any(x["id"] == "CHASE" for x in r["steps"]))
    case("the pipeline review lists each opportunity's chase and next steps", "pipeline review", _review)

    from . import provider, api
    case("Talk: reading a card is not a creation claim; a tool result naming a card is proof", "Talk audit 16 Oct",
         lambda: not provider._claims_action("Card #1287 is in your Inbox waiting for you.")
         and provider._claims_action("I've drafted the email for you.")
         and bool(provider._CARD_IN_RESULT.search("Drafting an email ... as task #1287")))
    case("Talk: only the final round's text is the reply", "Talk audit 16 Oct",
         lambda: "final_text = round_text" in inspect.getsource(provider.chat_tools_stream))
    case("Talk: answer style rule is first in the always-on rules", "Talk audit 16 Oct",
         lambda: "ANSWER STYLE (owner, absolute" in api._shared_behaviour())

    from . import find as _fd
    case("Talk finds what he says aloud: Kabila -> Qabilah, Wealth -> WELF, ticky -> Tikkie", "Talk audit 16 Oct",
         lambda: (_fd.deals("Kabila") or [{}])[0].get("id") == 130 and (_fd.deals("Alexander at Wealth") or [{}])[0].get("id") == 167
         and (_fd.deals("ticky") or [{}])[0].get("id") == 108)
    case("Talk finds an email-less WhatsApp contact by a mis-heard name", "Rhonda Alhamad / Randa Al Hammad",
         lambda: any((c.get("first_name") or "").lower() == "randa" for c in _fd.contacts("Rhonda Alhamad")))
    case("a sound-alike needs some shared spelling: 'form' is not 'farheen'", "Talk audit 16 Oct",
         lambda: (_fd._phon(["form", "farheen"]) or True) and _fd.word_score("farheen", "form") < 0.6)

    def _owed_guard():
        """The database refuses to close owed work without proof (Shama R350). Run inside a rolled-back transaction."""
        from psycopg.types.json import Json as _J
        from . import obligations
        obligations.ensure_guard()

        class _Back(Exception):
            pass
        got = {}
        try:
            with db.connect() as c:
                with c.transaction():
                    r = c.execute("insert into reminders (title, due_at, created_by, target_type) values "
                                  "('Meeting commitment: regress guard test', now() + interval '9 days', "
                                  "'cortex-pipeline', 'none') returning id, owed").fetchone()
                    got["owed"] = r["owed"]
                    try:
                        with c.transaction():
                            c.execute("update reminders set status='cancelled' where id=%s", (r["id"],))
                        got["blocked"] = False
                    except Exception as e:  # noqa: BLE001
                        got["blocked"] = "OWED_GUARD" in str(e)
                    c.execute("update reminders set status='done', closed_evidence=%s where id=%s",
                              (_J({"kind": "owner", "who": "regress"}), r["id"]))
                    got["closed"] = True
                    raise _Back
        except _Back:
            pass
        return bool(got.get("owed") and got.get("blocked") and got.get("closed"))
    case("owed work cannot be closed without proof; with proof it can", "Shama R350, 16 Oct", _owed_guard)
    from . import flow as _flw
    case("the Inbox tidy only flags owed work, never closes or moves it", "Shama R350, 16 Oct",
         lambda: "obligations.flag(r[\"id\"], why)" in inspect.getsource(_flw._cancel_rem)
         and "could wait until" in inspect.getsource(_flw._apply))
    case("a model's 'it was delivered' must quote words really in the email", "Shama R350, 16 Oct",
         lambda: __import__("cortex.obligations", fromlist=["x"]).quote_in("please find attached the three creative options",
                                                                            "Hi, please find attached the three creative options.")
         and not __import__("cortex.obligations", fromlist=["x"]).quote_in("the meeting was the deliverable checkpoint",
                                                                           "We met on Thursday."))

    case("owed work whose email is held still shows as a notice", "D-Marin R336, 16 Oct",
         lambda: "owed-held:" in inspect.getsource(reminders.fire))
    case("a card holding several due items says how many", "Shama 1200, 16 Oct",
         lambda: "things due" in inspect.getsource(__import__("cortex.notifications", fromlist=["x"]).notify))

    case("the tidy never judges a reminder's own notice while the reminder is open", "iTellu N1164, 16 Oct",
         lambda: "r.last_notification_id=n.id" in inspect.getsource(_flw._open_items))

    from . import cast as _cst
    case("a follow-up goes to the opportunity's lead, whoever the thread was with", "Heaven card 1297, 17 Oct",
         lambda: "addressed_to_lead" in inspect.getsource(engine._adopt_existing_thread)
         and "cast.our_lead" in inspect.getsource(engine._deal_sender))
    case("an owner's word on who's who is pinned; a read never invents a person or role", "cast list, 17 Oct",
         lambda: "his word stands" in inspect.getsource(_cst._apply) and "never invented" in inspect.getsource(_cst._apply)
         and set(_cst.ROLES) == {"lead", "decision", "member", "cc", "support", "left"})
    case("a post-meeting email reads the full meeting, not the brief", "Heaven card 1297, 17 Oct",
         lambda: "meeting_full" in inspect.getsource(engine._draft_context_for_reply))

    # --- meeting notes to a client: the summary only (owner, 18 Oct, Heaven card 1301)
    from . import meetnotes as _mn, documents as _docs
    _TABBED = ("﻿✍️ Quick notes\nPlease rate the new Quick notes tab by taking a short survey.\nSensa and Heaven\n\n"
               "Oct 16, 2026\nabraham@heavenproperties.com balazs@heavenproperties.com\n\n"
               "Company introductions and AI production capabilities reviewed.\n\nPricing and service packages\n"
               "* Packages include 30 second primary videos.\n\nNext steps\n"
               "* [Abraham Kovacs] Provide Hungarian Data: Send the voice samples.\n\n"
               "Want to see more? View the full notes\n📝 Full notes\nSummary\nLonger summary.\nDetails\n"
               "Pricing: they utilize expensive top models like Kling 3 (00:32:42).\n📖 Transcript\n00:00:01\n"
               "Balazs: Thank you very much.")
    _EMAIL = ("Notes from “Call”\nThese notes have been sent to invited guests in your organization.\n"
              "Quick Notes\nProject Parameters and Budget\nThe estimated project budget is 80,000 to 100,000 AED to  \n"
              "cover 2 influencers.\nSuggested next steps\n[Shahnaz] Send Options: Compile and email a list.\n"
              "Meeting records Document Notes by Gemini\nIs the content of this email helpful?")

    def _summary_only():
        a = " ".join(_mn.summary_lines(_TABBED, "Sensa and Heaven"))
        b = _mn.summary_lines(_EMAIL)
        return ("Provide Hungarian Data" in a and "30 second primary videos" in a and "Kling" not in a
                and "Thank you very much" not in a and "survey" not in a and "@" not in a and "Sensa and Heaven" not in a
                and "to cover 2 influencers." in " ".join(b) and "Is the content" not in " ".join(b))
    case("meeting notes for a client are Gemini's summary and next steps, never Details or the transcript",
         "Heaven card 1301, 18 Oct", _summary_only)

    def _private_refused():
        try:
            _docs.card_ref({"id": 0, "kind": "meeting-notes", "filename": "x.txt", "path": "/x", "drive_id": None})
            return False
        except ValueError:
            return True
    case("the internal meeting record (with the transcript) can never go on a card or be sent", "18 Oct",
         lambda: _private_refused() and "PRIVATE_KINDS" in inspect.getsource(_docs.find)
         and "PRIVATE_KINDS" in inspect.getsource(engine._send_email_reply))
    # --- ONE next-steps list per opportunity (owner, 18 Oct: seven cases, Shama/Heaven/Workspace/HBMSU/Brent/Shamal/DSO)
    from . import nextsteps as _ns, gate as _gt, reminders as _rm, pipeline

    def _ns_rules():
        same = _ns._same("Send Farheen three ideas and three treatments for the Ramadan film",
                         "Send 3 ideas and 3 treatments for the Ramadan film to Farheen")
        tools = (_ns.tool_for("3 ideas and 3 treatments for the Ramadan commercial") == "creative_treatments"
                 and _ns.tool_for("Send the tailored proposal with packages and pricing") == "create_proposal"
                 and _ns.tool_for("Issue the pro forma invoice") == "issue_proforma")
        offer = pipeline._is_offer("We are happy to prepare sample extraction output if needed.")
        theirs_no_date = _ns._due("none", whose="them") is None and _ns._due("none", whose="us") is not None
        proof = _ns._in("We look forward to your consolidated feedback by 19 October",
                        "Thanks. We look forward to your consolidated feedback by 19 October.") and not _ns._in(
                            "they sent the signed quotation", "Lets do it. Send me bank details.")
        return same and tools and offer and theirs_no_date and proof
    case("next steps: a re-mention matches, work names its tool, an offer is never owed, theirs has no invented date",
         "seven cases, 18 Oct", _ns_rules)
    case("every event RE-PLANS the whole opportunity: each change carries a quote checked by code, the owner's items "
         "are his, only his note dismisses, an offer is never owed",
         "owner 18 Oct: 'look at the entire opportunity ... create the correct next step' (Estithmar #172)",
         lambda: all(x in inspect.getsource(_ns._replan) for x in (
             "_in(q, corpus)", 'it.get("source") == "owner"', 'st == "dismissed"', "pipeline._is_offer(q)",
             "_timeline(deal_id)")) and "_deal_lock" in inspect.getsource(_ns.observe)
         and _ns.REPLAN_MODEL_DEFAULT == "claude-opus-5")
    case("a To do card can be dismissed (the step stays on the list); a call about a quotation names no build tool",
         "Welf #167, 19 Oct",
         lambda: _ns.tool_for("Hold the call with Alexander tomorrow to run through the proposal and quotation") is None
         and _ns.tool_for("Send the revised quotation") == "create_quotation"
         and "todoTalkOpen(${t.id},false)\">Type</button><button class=\"btn sm\" onclick=\"notifAct(${t.id},'dismiss')\">Dismiss"
         in open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"), encoding="utf-8").read())
    def _ref_routing():
        from . import crm as _c
        ok_codes = (_c.tender_code("RE: Salma MARKETING-STUDIO ( RAF-REQ-0000127782/ SALMA-PR-0000006721)") == "RAF-REQ-0000127782"
                    and _c.tender_code("RFQ for a promotional campaign for DAWAK, ( RAF-REQ-0000162089/ TLC-PR-26015197)")
                    == "RAF-REQ-0000162089"
                    and _c.tender_code("Re: Quotation SEN-2026-0024 for the SAKINA films") == ""
                    and _c.tender_code("Wasl, Viya corporate festive video (RFQ 2026_1170)") == "2026_1170")
        tool = _ns.tool_for("Prepare the two quotations for the DAWAK campaign covering concept, filming") == "create_quotation"
        return ok_codes and tool and "find_tender_deal(company.get(\"slug\")" in inspect.getsource(
            __import__("cortex.events", fromlist=["x"]).they_wrote)
    case("an email with an RFQ reference goes to that RFQ's opportunity, or opens a new one; a quote step says quotation",
         "RAFED #136, 20 Oct", _ref_routing)

    case("after a correction the card reloads and the box clears, rule offer or not", "Al Barsha card 1374, 20 Oct",
         lambda: "THE REDRAFT ALWAYS SHOWS" in open(os.path.join(os.path.dirname(__file__), "..", "..", "web",
                                                                  "index.html"), encoding="utf-8").read())

    def _people_off():
        from . import nextsteps as _n, crm as _c
        on = ["shahnawaz.khan@sheraa.ae", "a.farhan@sheraa.ae", "jamal@gambit.ae", "shahnaz@gambit.ae", "procurement@sheraa.ae"]
        g = engine._people_matching("all people from Gambit", on)
        return (sorted(g) == ["jamal@gambit.ae", "shahnaz@gambit.ae"]
                and engine._people_matching("everyone at gambit", on) == g
                and engine._people_matching("jamal@gambit.ae", on) == ["jamal@gambit.ae"]
                and engine._people_matching("Sheraa", on) == ["shahnawaz.khan@sheraa.ae", "a.farhan@sheraa.ae",
                                                              "procurement@sheraa.ae"]
                and not engine._people_matching("Unknown Agency", on)
                and bool(_n._EMAIL_STEP.search("Email Luna to propose another call to go through the proposal"))
                and not _n._EMAIL_STEP.search("Send the tailored proposal with pricing, packages and specifications")
                and "role\": \"left\", \"pinned\": True" in inspect.getsource(_c.remove_deal_contact))
    case("'remove everyone from Gambit' takes them off the email; a person removed from an opportunity stays off; an "
         "email step is an email", "Sheraa card 1389 / iTellu #113, 21 Oct", _people_off)

    def _owner_chase():
        from . import gate as _g
        h = [{"event": "email_out", "ts": "2026-10-15T08:00:00+00:00"},
             {"event": "chase_set", "ts": "2026-10-21T04:54:00+00:00", "text": "Next chase set to Wed 21 Oct"}]
        return (_g.owner_set_chase(h) and _g._rhythm_floor({"cadence": None}, h) is None
                and not _g.owner_set_chase([h[1], {"event": "email_out", "ts": "2026-10-21T06:00:00+00:00"}]))
    case("a chase date the owner set beats the rhythm floor", "MBK Marine #141, 21 Oct", _owner_chase)

    def _hundreds():
        from . import ratecard
        lines = [{"unit": 1800.0, "qty": 1}, {"unit": 4200.0, "qty": 1}, {"unit": 900.0, "qty": 1}]
        rem = ratecard._spread_in_steps(lines, -300.0, 100)
        ok_spread = (rem == 0 and all(float(x["unit"]) % 100 == 0 for x in lines)
                     and sum(float(x["unit"]) for x in lines) == 6600.0)
        return (ratecard._round(870, 100) == 900.0 and ok_spread
                and "_spread_in_steps(ones, drift, step)" in inspect.getsource(ratecard.price_lines))
    case("lines moved to reach the owner's figure are priced in whole hundreds", "owner, 22 Oct", _hundreds)

    def _no_busy_booking():
        from . import calendar as _c
        pre, ext = inspect.getsource(engine._prebook_meeting), inspect.getsource(engine._maybe_extract_meeting)
        slot = inspect.getsource(engine._client_proposed_slot)
        send = inspect.getsource(engine._send_email_reply)
        return ("_slot_clash(" in pre and "_slot_clash(" in ext and "meet=not _person" in pre
                and "location" in inspect.signature(_c.create_event).parameters
                and "IS proposing that time" in slot and 'not mt2.get("in_person")' in send)
    case("nothing books into a busy slot; an in-person meeting gets no Meet link and no invite", "DSO card 1422, 22 Oct",
         _no_busy_booking)

    def _include_is_attach():
        return (engine._attach_asked("remove this paragraph\n\nand also include our ai capabilities pdf")
                and engine._attach_asked("add the company profile")
                and not engine._attach_asked("tell him I will send the concepts together with our AI capabilities deck")
                and not engine._attach_asked("we'll include the deck next week")
                and not engine._attach_asked("add a sentence thanking her"))
    case("'include our AI capabilities pdf' attaches it; a later send never does", "Shurooq card 1488, 26 Oct",
         _include_is_attach)

    def _not_interested_deal():
        from . import obligations as ob, invariants as inv, events as ev
        return (engine._subj_norm("Reminder: RE: SSMC RFQ for UAE Flag Day") == engine._subj_norm("SSMC RFQ for UAE Flag Day")
                and "Tender closes" not in ob._owed_sql() and "Commitment owed to" in ob._owed_sql()
                and 'set_project_stage(p["id"], "Lost"' in inspect.getsource(engine.deal_not_interested)
                and "_not_pursued_match(" in inspect.getsource(engine._track_tender)
                and "_tender_in_scope(" in inspect.getsource(engine._track_tender)
                and "_not_pursued_match(" in inspect.getsource(engine._flag_skipped_opportunity)
                and '"dismissed"' in inspect.getsource(inv._owner_notices)
                and "_out_of_scope" in inspect.getsource(ev.they_wrote))
    case("not interested closes the whole opportunity; repeats stay quiet; dismissed health notices stay dismissed",
         "SSMC tender deal 179, 26 Oct", _not_interested_deal)

    def _step_for_a_person():
        from . import nextsteps as ns
        step = "Message Colonel Ebrahim at Dubai Police directly to find out where the submitted BAFO stands"
        to_ey = {"inquiry": {"name": "Shehryar Rahman", "email": "shehryar@nonexistent-regress.test"}}
        return (ns.personal_channel(step)
                and not ns.personal_channel("Email Luna to propose another call to go through the proposal")
                and not ns.personal_channel("Email Alex directly with the revised deck")
                and ns.personal_channel("WhatsApp Shahnawaz about the shoot date")
                and ("Ebrahim", True) in ns.named_people(step)
                and ns.named_people("Email Luna to propose another call")[:1] == [("Luna", False)]
                and ns.name_like("Ebrahim", "Ibrahim") and not ns.name_like("Ibrahim", "Shehryar")
                and bool(engine._greeting_mismatch("Dear Colonel Ibrahim,\n\nI hope you are well.", to_ey))
                and not engine._greeting_mismatch("Dear Shehryar,\n\nThanks.", to_ey)
                and not engine._greeting_mismatch("Hi Kostas,\n\nThanks.",
                                                  {"inquiry": {"name": "Konstantinos Kanellaidis", "email": "k@x.test"}})
                and not engine._greeting_mismatch("Dear Colonel Ibrahim,", {"inquiry": {"email": "a@x.test"}})
                and "personal_channel(what)" in inspect.getsource(ns.add)
                and "route_for(it, lead)" in inspect.getsource(ns._draft_email))
    case("a step for someone else is done with them, or is a To do; a greeting must match the To line",
         "Dubai Police card 1471, 25 Oct", _step_for_a_person)

    def _owner_ask_kept():
        said = ("Yes. This is good. You can confirm that you're avail happy to meet. However, if you could kindly ask if "
                "it's possible, we could do the call on the following meeting link. And then say, if not, it's okay. "
                "Please share the... Your your invite.")
        before = ("Hello Bernadette,\n\nTomorrow at 12:00 PM Doha time works well for us.\n\nWould it be possible to hold "
                  "the call on the following Google Meet link: https://meet.google.com/vpk-fuvi-jbv? If that is not "
                  "convenient, it is no problem at all, please do send through your own invitation and we will join."
                  "\n\nWe are looking forward to the call. We look forward to speaking with you then.")
        cut = ("Hello Bernadette,\n\nTomorrow at 12:00 PM Doha time works well for us.\n\nHere is the Google Meet link "
               "for the call: https://meet.google.com/vpk-fuvi-jbv\n\nWe look forward to speaking with you then.")
        kept = before.replace("We are looking forward to the call. ", "")
        return (bool(engine._owner_ask_lost(before, cut, said)) and not engine._owner_ask_lost(before, kept, said)
                and "owner=text" in inspect.getsource(engine.apply_correction))
    case("the final read-through never cuts what the owner's correction asked for", "Estithmar card 1405, 21 Oct",
         _owner_ask_kept)

    def _tax_invoice():
        from . import nextsteps, taxinvoice
        src = inspect.getsource(taxinvoice.render)
        return (nextsteps.tool_for("Tax invoice for quotation SEN-2026-0021 at final agreed amount") == "issue_tax_invoice"
                and nextsteps.INVOICE_STEP.search("Tax invoice for quotation SEN-2026-0021")
                and "TAX INVOICE" in src and "Signature" not in src and "Terms" not in src
                and "INVOICE_STEP" in inspect.getsource(__import__("cortex.pipeline", fromlist=["x"]).suggest_next_step))
    case("an invoice next step is a tax invoice, never a quotation; the tax invoice carries no signature or terms",
         "Shama card 1401, 21 Oct", _tax_invoice)

    def _deal_email():
        html = open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"), encoding="utf-8").read()
        return ("keep_email(int(did)" in inspect.getsource(pipeline.record_send)
                and "extra={\"email_id\": _eid}" in inspect.getsource(pipeline.record_send)
                and "h.email_id?emailLine(p,h.email_id)" in html and "/api/crm/deal-email/" in html)
    case("every email we send is kept on the opportunity with a one-line summary and opens in full", "owner, 21 Oct",
         _deal_email)

    def _star():
        from . import crm as _c, api as _a
        html = open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"), encoding="utf-8").read()
        return ("set_star(project_id, False" in inspect.getsource(_c.set_project_stage)
                and "stage\") in FORECAST_STAGES" in inspect.getsource(_c.set_star)
                and "starred_currencies" in inspect.getsource(_a.crm_opportunities)
                and "Starred value" in html and "o.starred" in html and "starBtn(p,16)" in html)
    case("a starred opportunity sits at the top by value, its value beside the forecast; it drops off when it leaves "
         "the sales stages", "owner, 21 Oct", _star)

    def _bare_wa():
        sk = {"rules": ["AUTO REPLY: Hi {name}, thanks for getting in touch with {company}. Rashad will be in touch.",
                        "AUTO REPLY NO DETAILS: Hi {name}, thanks for getting in touch with {company}. Can you give us a "
                        "short description of the video you are looking for?\n(guidance)"]}
        co = {"name": "Sensa Productions"}
        return (whatsapp.bare_enquiry("Hi Sensa, I need a video produced in Dubai. My project: (ref LENB3)")
                and not whatsapp.bare_enquiry("Hi Sensa, I need a video produced in Dubai. My project: a 60 second "
                                              "launch film for our perfume (ref LENB3)")
                and whatsapp.bare_enquiry("Hi Sky Vision, I would like to enquire about drone filming. (ref ABCDE)")
                and "short description" in whatsapp.auto_reply_text(sk, co, "Rajanya", bare=True)
                and "(guidance)" not in whatsapp.auto_reply_text(sk, co, "Rajanya", bare=True)
                and "Rashad will be in touch" in whatsapp.auto_reply_text(sk, co, "Rajanya", bare=False)
                and "Rashad will be in touch" in whatsapp.auto_reply_text({"rules": sk["rules"][:1]}, co, "R", bare=True))
    case("a WhatsApp enquiry that is only our pre-filled text gets the acknowledgement that asks what the video is for",
         "Rajanya, ref LENB3, 21 Oct", _bare_wa)

    def _sent_1381():
        edit = ("Remove the sentence claiming the package 'includes the Dubai Police Innovation film and the AI legal "
                "services presenter concept'. Those are sample films shown inside the proposal.")
        return (not engine._QUOTE_CHANGE.search(engine._EMAIL_EDIT.sub(" ", edit))
                and bool(engine._QUOTE_CHANGE.search(engine._EMAIL_EDIT.sub(" ", "remove two films from the quotation")))
                and "SentAlready" in inspect.getsource(engine.apply_correction)
                and "SentAlready" in inspect.getsource(engine.correct_task))
    case("a sent email cannot be corrected (no reopened card, no second send); 'remove the sentence ...' is an email "
         "edit, never a quotation change", "iTellu card 1381, 20 Oct", _sent_1381)

    def _xlsx_1378():
        from . import documents as _docs
        return (bool(_docs.SPREADSHEET.search("Dubai Silicon Oasis _DSO_ - Quotation SEN-2026-0038 v1 - 2026-10-13.xlsx"))
                and not _docs.SPREADSHEET.search("Dubai Silicon Oasis _DSO_ - Quotation SEN-2026-0038 v1 - 2026-10-13.pdf")
                and "pdf_twin(doc)" in inspect.getsource(_docs.card_ref)
                and "we never share spreadsheets" in inspect.getsource(engine._send_email_reply)
                and _docs._identity("X - Quotation SEN-2026-0038 v1 - 2026-10-13.xlsx")
                == _docs._identity("X - Quotation SEN-2026-0038 v1 - 2026-10-13.pdf"))
    case("an email never carries a spreadsheet: the PDF goes instead, and the send refuses one", "DSO card 1378, 20 Oct",
         _xlsx_1378)

    def _qi_1374():
        email_words = ("Redraft this. Don't put a scheduled call in. We've attached two proposals, one for the one off "
                       "eight videos and one for the retainer contract. The retainer contract comes at a considerable "
                       "cost saving. Explain politely that the one off quote is a bit more expensive as far as the "
                       "initial setup cost to create the AI characters. Don't ask for the script.")
        return (not engine._QUOTE_CHANGE.search(email_words)
                and bool(engine._QUOTE_CHANGE.search("remove the voice over from the quotation"))
                and bool(engine._QUOTE_CHANGE.search("bring it down to 28,000"))
                and bool(engine._QUOTE_CHANGE.search("quote them for 10 videos instead of 8"))
                and bool(engine._QUOTE_OPTION_WORDS.search("add a second option without the voice over"))
                and "guarded=task[\"kind\"] in EMAIL_RENDER_KINDS" in inspect.getsource(engine.apply_correction)
                and "a failed quotation build never swallows" in inspect.getsource(engine.apply_correction))
    case("an email instruction that talks about price is not a quotation change; a failed quotation build still "
         "redrafts the email", "Al Barsha card 1374, 20 Oct", _qi_1374)

    def _fold_1374():
        from . import documents as _docs
        labels = {"SEN-2026-0049": "Option A: AI PRESENTER VIDEOS QUOTATION option A",
                  "SEN-2026-0050": "Option B: AI CONTENT RETAINER QUOTATION option B"}
        rows = [{"id": 1140, "kind": "proposal", "deal_id": 170,
                 "filename": "Al Barsha Private Notary - AI content retainer - Proposal v4 - 2026-10-20.pdf"},
                {"id": 1110, "kind": "quotation", "deal_id": 170,
                 "filename": "Al Barsha Private Notary - Quotation SEN-2026-0049 v4 - 2026-10-20.pdf"},
                {"id": 1101, "kind": "quotation", "deal_id": None,
                 "filename": "Al Barsha Private Notary - Quotation SEN-2026-0050 v2 - 2026-10-19.pdf"},
                {"id": 1115, "kind": "quotation", "deal_id": None,
                 "filename": "Al Barsha Private Notary - Quotation SEN-2026-0050 v3 - 2026-10-20.xlsx"},
                {"id": 1107, "kind": "quotation", "deal_id": 170,
                 "filename": "Al Barsha Private Notary - Quotation SEN-2026-0050 v3 - 2026-10-20.pdf"},
                {"id": 1122, "kind": "quotation", "deal_id": None,
                 "filename": "Al Barsha Private Notary - Quotation SEN-2026-0050 v3 - 2026-10-20.pdf"}]
        q = "AI content retainer quotation"
        hits = sorted([r for r in rows if _docs._score(r, q, labels) > 0], key=lambda r: -_docs._score(r, q, labels))
        got, tied = _docs._choose(hits, q, labels, 170)
        deck = [r for r in rows if _docs._score(r, "the retainer proposal", labels) > 0]
        both, tie2 = _docs._choose(sorted(rows, key=lambda r: -_docs._score(r, "the quotation", labels)),
                                   "the quotation", labels, 170)
        return ((got or {}).get("id") == 1107 and not tied and [r["id"] for r in deck] == [1140]
                and both is None and len({_docs._identity(r["filename"]) for r in tie2}) == 2
                and "_fold_files_into_email(em, refs" in inspect.getsource(engine._draft_proposal_email))
    case("a quotation is matched by its card's name, never a proposal; approving a proposal adds its files to the "
         "open email instead of refusing", "Al Barsha card 1374, 20 Oct", _fold_1374)

    def _meet_1365():
        from . import api as _api
        mt = {"start": "2026-10-21T11:00:00+04:00", "minutes": 60, "event_id": "x",
              "meet": "https://meet.google.com/tki-jykz-xvs"}
        decline = ("Hello Ahmed,\n\nThank you for sending over the brief for the EuroLeague One Team Programme media "
                   "coverage.\n\nPreparing a quotation takes us real time on each occasion, and having had no feedback "
                   "or engagement so far on the proposals we have already submitted, including quotation SEN-2026-0033, "
                   "we would like to respectfully step back from submitting on this one.\n\nBest regards,")
        gheis = "Hello Gheis,\n\nYes, 4:00 PM today works well. Here is the link: https://meet.google.com/tki-jykz-xvs"
        words = "Hello Mai,\n\nWednesday 21 October at 11:00 Dubai time works for us; the invitation follows."
        twelve = "Hello Mai,\n\nWould Wed 21 Oct at 11am suit you?"
        bare = {"start": "2026-10-21T11:00:00+04:00", "minutes": 30}
        return (not engine._email_carries_meeting(decline, mt) and engine._email_carries_meeting(gheis, mt)
                and engine._email_carries_meeting(words, bare) and engine._email_carries_meeting(twelve, bare)
                and not engine._email_carries_meeting("Thanks, we will be in touch on the 21st.", bare)
                and "_email_carries_meeting" in inspect.getsource(engine._send_email_reply)
                and "meeting_invite" in inspect.getsource(_api._enrich_action_card)
                and "meeting_off" in inspect.getsource(engine._client_proposed_slot)
                and "meetingLine(t)" in open(os.path.join(os.path.dirname(__file__), "..", "..", "web",
                                                          "index.html"), encoding="utf-8").read())
    case("a meeting rides on an email only when the email carries it, and the card shows the invite before approval",
         "Special Olympics card 1365, 20 Oct", _meet_1365)

    def _dso_1353():
        from . import documents as _docs
        later = ("In the second paragraph, tell him that I will send over the concepts and the quotation also today, "
                 "together with our AI capabilities deck that outlines our previous experience with AI production, "
                 "and also our company portfolio ahead of the meeting.")
        remove = ("On the second paragraph, tell him we will send these over shortly. And remove any attachments "
                  "from this email. We'll send them in a short while. You know?")
        ok_attach = (not engine._attach_asked(later) and not engine._attach_asked(remove)
                     and engine._attach_asked("attach the trade licence and the company profile")
                     and engine._attach_asked("remove the quote and attach the profile instead"))
        ok_detach = (engine._detach_all_asked(remove) and engine._detach_all_asked("send it without attachments")
                     and engine._detach_all_asked("no attachments please")
                     and not engine._detach_all_asked("remove the quotation attachment")
                     and not engine._detach_all_asked(later))
        rows = [{"kind": "capabilities-deck", "filename": "Sensa - AI Avatar Posts.pdf"},
                {"kind": "capabilities-deck", "filename": "Sensa - Body Double - AI Character Social.pdf"},
                {"kind": "capabilities-deck", "filename": "Sensa - AI Production Capabilities Deck.pdf"}]
        best = max(rows, key=lambda r: _docs._score(r, "AI capabilities deck"))
        ok_doc = (best["filename"] == "Sensa - AI Production Capabilities Deck.pdf"
                  and _docs._score({"kind": "document", "filename": "Dubai Municipality - deck.pdf"}, "ai") == 0)
        clocks = engine._clocks_in("tomorrow, 10AM or 2PM, and Thursday at 1PM")
        from datetime import datetime as _dt
        base = _dt(2026, 10, 20, tzinfo=engine._GST)      # a Tuesday
        ok_time = (clocks == {(10, 0), (14, 0), (13, 0)}
                   and engine._day_from_words("tomorrow", base).day == 21
                   and engine._day_from_words("Thursday", base).day == 22
                   and engine._day_from_words("next Monday", base).day == 2
                   and engine._day_from_words("21 October", base).day == 21
                   and "owner_times" in inspect.getsource(worker.draft))
        return ok_attach and ok_detach and ok_doc and ok_time
    case("'we will send X later' never attaches; 'remove any attachments' clears them; a deck is matched by its real "
         "name; the owner's own meeting times beat the slot list", "DSO card 1353, 20 Oct", _dso_1353)

    def _push_rhythm():
        from . import crm as _c
        gaps = [p["after_days"] for p in _c.cadence_points(_c.CLOSING_CADENCE)][:7]
        tg = [p["after_days"] for p in _c.cadence_points(_c.TENDER_CLOSING_CADENCE)][:3]
        return (gaps == [1, 2, 3, 2, 3, 5, 5] and tg == [1, 5, 5]
                and _c.is_formal_tender({"title": "Tender: Video Production Request for Proposal", "history": []})
                and _c.is_formal_tender({"title": "Al Hamra RAK IN MOTION", "history": [
                    {"event": "email_out", "text": "sent to kiran: RFP Invitation - RAK IN MOTION"}]})
                and not _c.is_formal_tender({"title": "Dubai Silicon Oasis - brand film", "history": []})
                and "restart_after_quote" in inspect.getsource(pipeline.record_quotation_sent)
                and "their_dates()" in inspect.getsource(_ns.tick))
    case("after a quote: receipt check, then every 2-3 working days for two weeks (weekly for a formal tender); "
         "their date wins", "owner 19 Oct: DSO / Al Hamra 'too slow'", _push_rhythm)

    def _tender_job():
        from . import crm as _c
        d = {"title": "Wasl, Viya corporate festive video (RFQ 2026_1170)", "history": []}
        return (not _c.same_job(d, "2026_1165", "Invitation to RFQ 2026_1165")
                and _c.same_job(d, "2026_1170", "RFQ 2026_1170 deadline extended")
                and not _c.same_job(d, "", "Digital Transformation Content Creation Agency - Hero Photography for Viya Corporate")
                and _c.same_job(d, "", "Viya festive video: clarification on the festive shoot")
                and "second_job=True" in inspect.getsource(engine._track_tender))
    case("a new RFQ from a client with an open RFQ is a new opportunity, never filed on the other job",
         "Wasl 2026_1165 on 2026_1170, RAFED, 19 Oct", _tender_job)
    case("every newsletter card in the Inbox opens its issue before approval",
         "owner, 19 Oct (card 602)",
         lambda: 'issue_link' in inspect.getsource(__import__("cortex.api", fromlist=["x"])._enrich_action_card)
         and "function viewIssue(" in open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"),
                                           encoding="utf-8").read())

    def _contact_card():
        from . import whatsapp as _wa, crm as _crm
        m = {"type": "contacts", "contacts": [{"name": {"formatted_name": "Matt Brooke"},
                                               "phones": [{"phone": "+971 50 123 4567", "wa_id": "971501234567"}],
                                               "emails": [], "org": {"company": "White & Co"}}]}
        t = _wa._owner_text(m)
        return ("[SHARED CONTACT CARD] Name: Matt Brooke; Phone: +971 50 123 4567; Company: White & Co" == t
                and "add_deal_phone_contact" in inspect.getsource(__import__("cortex.api", fromlist=["x"])._update_opportunity)
                and "phone_key" in inspect.getsource(_crm.add_deal_phone_contact))
    case("a contact card shared on WhatsApp reaches Talk, and a number alone can go on an opportunity",
         "Matt Brooke #124, 19 Oct", _contact_card)

    def _byline_day():
        from .integrations import wordpress as _wpx
        html = ('<div><a href="x">Ben Smith</a> &nbsp;&middot;&nbsp; CTO &nbsp;&middot;&nbsp; 2026-06-24 &nbsp;&middot;&nbsp; '
                '7 min read</div><p>On 2026-05-01 we &nbsp;&middot;&nbsp; 2026-05-02 &nbsp;&middot;&nbsp;</p>')
        out = _wpx.restamp_byline(html, "2026-10-17")
        return ("CTO &nbsp;&middot;&nbsp; 2026-10-17 &nbsp;&middot;&nbsp; 7 min read" in out and "2026-05-02" in out
                and "2026-05-01" in out and "restamp_byline(" in inspect.getsource(_wpx.WordPress.go_live))
    case("a queued post goes live with the publish day in its byline, not the day it was built",
         "Snap Rewards 'Points vs Cashback', 18 Oct", _byline_day)

    def _todo_talk():
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        web = open(os.path.join(root, "web", "index.html"), encoding="utf-8").read()
        return ("function todoTalk(" in web and "About the To do on opportunity #" in web and "'To do'" in web
                and 'category="todo"' in inspect.getsource(_ns._todo_notice)
                and "_nsx.started_from_tool" in inspect.getsource(__import__("cortex.api", fromlist=["x"])._chat_prepare))
    case("a To do card is labelled To do, and Tap to talk sends the step to Talk; a started job shows on the card",
         "owner, 18 Oct", _todo_talk)

    def _voice_survives():
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        web = open(os.path.join(root, "web", "index.html"), encoding="utf-8").read()
        unit = open(os.path.join(root, "deploy", "cortex-api.service"), encoding="utf-8").read()
        src = inspect.getsource(__import__("cortex.api", fromlist=["x"]).voice_stream)
        return ("function vRetry()" in web and "w.onclose=" in web and "vFinal();" in web and "vEnd();" in web
                and "ping_timeout=60" in src and "[voice] stream ended" in src and "--ws-ping-timeout 60" in unit)
    case("a voice stream that drops reconnects and replays the audio since the last phrase; the server tolerates a stall",
         "owner 18 Oct: 'detecting it ... then suddenly stops' (keepalive ping timeout)", _voice_survives)
    case("a sent email's history entry names what it carried, so a re-plan sees a quotation went out",
         "Merck #166, 18 Oct", lambda: "[attached:" in inspect.getsource(pipeline.record_send))
    case("a proposal step is never made from 'they asked for a proposal' alone; Quote needs an attached file",
         "Estithmar #172, 18 Oct",
         lambda: "Prepare and send the proposal" not in inspect.getsource(__import__("cortex.crm", fromlist=["x"]).qualify_opportunity)
         and "subject" not in inspect.getsource(pipeline.maybe_advance_on_send).split("names =")[1].split("if ")[0])
    case("no chase while WE owe the next step; no owed reminder is ever created again (it goes on the list)",
         "Shama #164 chase armed while we owed her, 18 Oct",
         lambda: "_ours and not any(i[\"whose\"] == \"them\" for i in _open)" in inspect.getsource(_gt._check)
         and "nextsteps.add(int(tid)" in inspect.getsource(_rm.create)
         and "for c in []:" in inspect.getsource(pipeline.record_send)
         and "nextsteps.observe_async" in inspect.getsource(pipeline.record_meeting))

    case("everything WE owe that is overdue or due within a week is a To do card in the Inbox, with Done / Not needed",
         "owner, 18 Oct: 'where's all the stuff on the inbox that should still be there'",
         lambda: _ns.TODO_AHEAD_DAYS == 7 and "_todo_notice(it)" in inspect.getsource(_ns.tick)
         and "todoAct(" in open(os.path.join(os.path.dirname(__file__), "..", "..", "web", "index.html"), encoding="utf-8").read())

    # --- creative treatments as building blocks (owner, 18 Oct: "three treatments ... without a quotation", "a
    # quotation for this proposal or that proposal, or for all three")
    from . import creative as _crv
    import types as _types

    def _treat_blocks():
        a = _crv._treatment_name("Heaven Global", "A", "The Glass Villa", None, 1, "2026-10-18")
        b = _crv._treatment_name("Heaven Global", "B", "Sky Tour", "SEN-2026-0050", 2, "2026-10-18")
        names_ok = (a == "Heaven Global - Treatment A - The Glass Villa - 2026-10-18.pdf"
                    and b.startswith(_crv._treatment_prefix("Heaven Global", "B")) and not a.startswith(
                        _crv._treatment_prefix("Heaven Global", "B")) and "Proposal" not in a)
        rows_ok = _crv.Job.quote_rows(_types.SimpleNamespace(s={"quote": {"skipped": True, "spec": {}}})) == ([], 0.0)
        miss = _crv._set_missing([
            {"letter": "A", "status": "done", "approved": True, "number": "SEN-1", "quote_approved": False},
            {"letter": "B", "status": "awaiting_approval", "approved": False, "number": None},
            {"letter": "C", "status": "cancelled", "approved": False, "number": None}])
        return names_ok and rows_ok and miss == ["quotation SEN-1 (treatment A)", "treatment B"]
    case("treatments: own file names, no price page without a quotation, the set waits for every approval",
         "owner, 18 Oct", _treat_blocks)
    case("a treatment never quotes unasked, files only over its own decks, and its set sends ONE email",
         "owner, 18 Oct",
         lambda: "skip_quote" in inspect.getsource(_crv._run)
         and "not (job.s.get(\"quote\") or {}).get(\"skipped\")" in inspect.getsource(_crv.revise)
         and "archive_prefix=prefix" in inspect.getsource(_crv.Job.stage_file)
         and "creative.set_email" in inspect.getsource(engine._draft_proposal_email)
         and "approve_in_set" in inspect.getsource(engine._execute))

    # --- health check: his notices are actionable, repairs are a log, repeats are root causes (owner, 18 Oct)
    from . import invariants as _inv, flow as _flow

    def _health_owner():
        c = next(x for x in _inv.CHECKS if x["key"] == "opportunity_without_contact")
        lines = _inv.owner_lines(c, [{"id": "124", "title": "Matt Brooke real estate agent AI social content",
                                      "stage": "Opportunity"}])
        key, title, body, deal = lines[0]
        sweeps = [x["key"] for x in _inv.CHECKS if x.get("sweep")]
        return (deal == 124 and key == "health:opportunity_without_contact:124" and "no contact" in title
                and "add the contact" in body and set(sweeps) == {"stale_inbox_items", "duplicate_obligations"}
                and "notifications.notify(\n                f\"Health check" not in inspect.getsource(_inv.run))
    case("the health check tells the owner only what he can act on, per opportunity, in plain words",
         "health card 1214, 18 Oct", _health_owner)

    def _flag_waits():
        deal = {"history": [{"event": "email_in", "ts": "2026-10-17T10:00:00Z"}]}
        from datetime import datetime as _dt, timezone as _tz
        return (_flow._awaiting_owner({"suspect": {"why": "x"}}, deal)
                and _flow._awaiting_owner({"owner_kept_at": _dt(2026, 10, 18, tzinfo=_tz.utc)}, deal)
                and not _flow._awaiting_owner({"owner_kept_at": _dt(2026, 10, 16, tzinfo=_tz.utc)}, deal)
                and not _flow._awaiting_owner({}, deal))
    case("a 'may be done' flag waits for his answer; 'still owed' holds until something new happens",
         "Heaven R439/R440 re-flagged 8 times, 18 Oct", _flag_waits)

    case("a post-meeting email carries the summary PDF, refreshed when fuller notes arrive", "Heaven card 1301, 18 Oct",
         lambda: "_attach_summary_to_request" in inspect.getsource(_mn._spawn_post_meeting_followup)
         and "rebuild=True" in inspect.getsource(_mn._refresh_followup))

    # --- the SaaS page builders (Heaven retainer deck, owner 17 Oct)
    def _saas_pages():
        d = deck._Deck(_co("sensa"), "Test", "#2BB3E8", "", "")
        d.pricing("K", "H", "i", [{"name": "Tier 1", "price": "AED 10,000", "period": "per month",
                                   "features": ["one", "two"], "highlight": True}], "n", "s")
        d.flow("K", "H", "i", [{"label": "1", "title": "t", "body": "b"},
                               {"label": "2", "title": "t", "body": "b"}], "", "s")
        d.faq("K", "H", [{"q": "q", "a": "a"}], "", "s")
        # a showcase with no readable image adds nothing rather than an empty page
        d.showcase("K", "H", "i", [{"image": "/no/such/file.jpg", "title": "t"}], "s")
        return (len(d.pages) == 3 and "Recommended" in d.pages[0]
                and "AED 10,000" in d.pages[0] and "&rarr;" in d.pages[1])
    case("the pricing, flow and faq pages build; showcase needs a real image",
         "Heaven retainer deck, 17 Oct", _saas_pages)

    def _solo_pricing_fills_the_page():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.pricing("K", "H", "i", [{"name": "One", "price": "AED 1", "period": "p", "blurb": "b",
                                   "features": ["a", "b", "c", "d", "e", "f", "g", "h"],
                                   "highlight": True}], "n", "s")
        solo = d.pages[0]
        # a lone card at 420px left two thirds of the page empty and pushed the note into the footer
        ok = "width:1136px" in solo and solo.count("display:inline-block;vertical-align:top") == 2
        # and the 8th feature is no longer silently dropped (the cap was [:7])
        ok = ok and ">h</span>" in solo
        three = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        three.pricing("K", "H", "i", [{"name": str(i), "price": "AED 1", "period": "p",
                                       "features": ["a", "b"]} for i in range(3)], "", "s")
        return ok and "width:352px" in three.pages[0] and "width:1136px" not in three.pages[0]
    case("a single pricing plan fills the page and keeps every feature",
         "iTellu one package, 20 Oct", _solo_pricing_fills_the_page)

    def _samples_six_in_two_rows():
        co = _co("sensa")
        ids = [r["youtube_video_id"] for r in __import__("cortex.db", fromlist=["x"]).query(
            "select youtube_video_id from media_assets where company_id=%s and youtube_video_id is not null "
            "limit 6", (co["id"],))]
        if len(ids) < 6:
            return True                      # nothing to prove on a library this small
        films = deck.films_by_ids(co["id"], ids)
        d = deck._Deck(co, "T", "#00DAFF", "", "")
        d.samples("K", "H", "i", films, "s")
        pg = d.pages[0]
        # six at 362px is nearly twice the 1136px the pad allows, so they wrap three to a row
        rows = pg.count('display:flex;gap:24px;margin-top')
        three = deck._Deck(co, "T", "#00DAFF", "", "")
        three.samples("K", "H", "i", films[:3], "s")
        return (rows == 2 and 'class="pg six"' in pg
                and three.pages[0].count('display:flex;gap:24px;margin-top') == 1
                and 'class="pg six"' not in three.pages[0])
    case("a samples page carries six films in two rows", "iTellu page 3, 20 Oct",
         _samples_six_in_two_rows)

    def _site_style():
        # tokens read off sensa.digital/ai-video-production, 17 Oct 2026
        ok = (deck.SITE["accent"] == "#00DAFF" and deck.SITE["accent2"] == "#FF6A2C"
              and "JetBrains Mono" in deck.MONO)
        # a headline accents one phrase, and the copy either side is still escaped
        hl = deck._hl("We give you *control* & more", "#00DAFF")
        ok = ok and '<span style="color:#00DAFF">control</span>' in hl and "&amp;" in hl
        ok = ok and deck._hl("plain <b>", "#00DAFF") == "plain &lt;b&gt;"
        # spec bars clamp a silly percentage rather than drawing off the card
        bars = deck._bars([{"label": "WARDROBE", "pct": 400}], "#00DAFF", "#FF6A2C")
        return ok and "width:100%" in bars and "WARDROBE" in bars
    case("the deck speaks the website's visual language", "sensa.digital styling, 17 Oct",
         _site_style)

    def _hl_everywhere():
        # the accent word reached four OLD builders and missed the new ones on the first pass
        src = inspect.getsource(deck)
        import re as _re
        plain = _re.findall(r"\{_esc\(heading\)\}</h2>", src)
        for fn in (deck._Deck.pricing, deck._Deck.flow, deck._Deck.showcase, deck._Deck.faq):
            if "_hl(heading, self.accent)" not in inspect.getsource(fn):
                return False
        return not plain
    case("every page heading can accent a word", "sensa.digital styling, 17 Oct", _hl_everywhere)

    def _showcase_bars():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        import os as _os
        p = _os.path.join("/tmp", "_rg_bar.jpg")
        open(p, "wb").write(b"not-a-real-jpeg" * 8)   # only isfile + read are used
        d.showcase("K", "H", "", [{"image": p, "label": "L", "title": "t", "body": "b",
                                   "bars": [{"label": "DETAIL", "pct": 60}]}], "s")
        pg = d.pages[0]
        # the frames carry the page, so the still is never smaller than it was when the spec
        # bars were dropped from it (owner, 17 Oct 2026)
        import re as _re_sc
        m = _re_sc.search(r"width:(\d+)px;height:(\d+)px;object-fit:cover", pg)
        return ("DETAIL" in pg and "width:60%" in pg
                and m and int(m.group(2)) >= 200)
    case("a showcase card can carry the module spec bars", "sensa.digital styling, 17 Oct",
         _showcase_bars)

    def _faq_icon_inline():
        d = deck._Deck(_co("sensa"), "Test", "#2BB3E8", "", "")
        d.faq("K", "H", [{"q": "q", "a": "a", "icon": "clock"}], "", "s")
        pg = d.pages[0]
        # the icon sits on the question's own line; stacked above it overflowed a 720px page
        return "display:flex;gap:12px;align-items:center" in pg and "<svg" in pg
    case("a Q&A icon sits beside its question, not above it", "Heaven Q&A overflow, 17 Oct",
         _faq_icon_inline)

    case("render_spec reports a page that overflowed", "Heaven Q&A overflow, 17 Oct",
         lambda: "has overflowed" in inspect.getsource(deck.render_spec))

    def _chunky_optin():
        # chunky is per deck: a deck that does not ask for it keeps the 27px headings it was laid out for
        plain = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        plain.faq("K", "H", [{"q": "q", "a": "a"}], "", "s")
        if "font-weight:800" in plain.html():
            return False
        big = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        big.extra_css = deck._CHUNKY_CSS.replace("MONOFAM", deck.MONO)
        big.faq("K", "H", [{"q": "q", "a": "a"}], "", "s")
        h = big.html()
        return ("font-size:44px" in h and "font-weight:800" in h and "MONOFAM" not in h
                and "JetBrains Mono" in h
                and 'spec.get("chunky")' in inspect.getsource(deck.render_spec))
    case("chunky website headings are opted into per deck, never global",
         "Heaven deck v6, 17 Oct", _chunky_optin)

    def _flow_icons_not_dots():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.flow("K", "H", "", [{"label": "01", "title": "t", "body": "b", "icon": "phone"},
                              {"label": "02", "title": "t", "body": "b", "icon": "bulb"}], "", "s")
        pg = d.pages[0]
        # the meaningless corner dot is gone; a real 40px icon sits in its own panel
        return ("width:7px;height:7px" not in pg and "width:72px;height:72px" in pg
                and pg.count('width="40" height="40"') == 2 and "&rarr;" in pg)
    case("a flow step carries a big framed icon, not a coloured dot",
         "Heaven deck v6, 17 Oct", _flow_icons_not_dots)

    def _flow_without_arrows():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.flow("K", "H", "", [{"label": "a", "title": "t", "body": "b"},
                              {"label": "b", "title": "t", "body": "b"}], "", "s", arrows=False)
        return ("&rarr;" not in d.pages[0]
                and 'pg.get("arrows", True)' in inspect.getsource(deck.render_spec))
    case("a row of cards that is not a sequence takes no arrows",
         "Heaven deck v6 page 3, 17 Oct", _flow_without_arrows)

    def _cta_page():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.cta("NEXT", "Let's get your content *up and online*",
              "Four markets, one pipeline.",
              [{"title": "Choose a tier", "body": "b", "icon": "check"},
               {"title": "Send the captures", "body": "b", "icon": "phone"}],
              {"label": "REPLY TO THIS", "line": "hello@sensa.digital", "sub": "s"},
              "n", "Next")
        pg = d.pages[0]
        # the ask is the size of an ask: a 72px headline, not the 27px h2 closing() uses
        return ("font-size:72px" in pg and "up and online</span>" in pg
                and "hello@sensa.digital" in pg and "Step 1" in pg and "Step 2" in pg
                and "<svg" in pg
                and 'elif t == "cta"' in inspect.getsource(deck.render_spec))
    case("the last page asks for the decision at the size of an ask",
         "Heaven deck v6 page 12, 17 Oct", _cta_page)

    def _flow_fills_page():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.flow("K", "H", "", [{"label": "a", "title": "t", "body": "b"}], "", "s")
        # short steps in 300px boxes left a dead band above the footer on both Heaven flow pages.
        # A flex column is NOT the fix: WeasyPrint gives the free space to the heading, not to a
        # flex:1 row, so the page came out worse. The card floor is taller instead.
        import re as _re_ff
        m = _re_ff.search(r"min-height:([0-9]+)px", d.pages[0])
        return (m and int(m.group(1)) >= 340
                and "flex-direction:column" not in d.pages[0])
    case("a flow page reaches the foot of its frame", "Heaven deck v6 p3 and p10, 17 Oct",
         _flow_fills_page)

    def _saas_type_is_readable():
        # owner, 17 Oct 2026: "the writing just is a bit small". The body copy a client reads on
        # the SaaS pages is at least 13.5px; the grey #7A7A84 footnotes may stay small.
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.pricing("K", "H", "i", [{"name": "T", "price": "AED 1", "period": "p", "blurb": "b",
                                   "features": ["one"]}], "n", "s")
        d.flow("K", "H", "i", [{"label": "a", "title": "t", "body": "b"}], "n", "s")
        import re as _re_rd
        for pg in d.pages:
            for style in _re_rd.findall(r'style="([^"]*)"', pg):
                if "#7A7A84" in style:
                    continue
                m = _re_rd.search(r"font-size:([0-9.]+)px", style)
                if m and 10.5 < float(m.group(1)) < 13.5:
                    return False
        return True
    case("the body copy on a SaaS page is at least 13.5px",
         "Heaven deck v6, 17 Oct", _saas_type_is_readable)

    def _split_panel_hugs():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.split("K", "H", [{"title": "t", "body": "b"}],
                {"title": "p", "cells": [{"title": "c", "sub": "s"}]}, "n", "s")
        # Stretched to a flex row, the panel printed an empty box under its last cell.
        # WeasyPrint honours neither align-items:flex-start nor a flex auto margin here, so the
        # columns are inline-block. Keep it that way.
        pg = d.pages[0]
        return ("display:inline-block;vertical-align:top" in pg
                and "display:flex;gap:40px" not in pg)
    case("a split panel hugs its content instead of stretching",
         "Heaven deck v6 p2, 17 Oct", _split_panel_hugs)

    def _split_band():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.split("K", "H", [{"title": "t", "body": "b"}], {"title": "p", "cells": []}, "",
                "s", {"label": "CONTENT AS A SERVICE", "title": "A platform, not a *company*",
                      "body": "b", "items": [{"title": "one", "body": "b"},
                                             {"title": "two", "body": "b"}]})
        pg = d.pages[0]
        # a split with no band is exactly what it was
        plain = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        plain.split("K", "H", [{"title": "t", "body": "b"}], {"title": "p", "cells": []}, "", "s")
        return ("CONTENT AS A SERVICE" in pg and "A platform, not a" in pg
                and '<span style="color:#00DAFF">company</span>' in pg
                and "one" in pg and "two" in pg
                and "border-top:1px solid #23232A" not in plain.pages[0])
    case("a split page can carry a full-width band under its columns",
         "Heaven deck p2, 17 Oct", _split_band)

    def _mono_style_is_well_formed():
        # MONO goes into a style="..." ATTRIBUTE: a double quote in the font stack closed the
        # attribute, so every kicker rendered as plain white body text (17 Oct 2026).
        out = deck._mono("Week 1", "#00DAFF")
        return ('"' not in deck.MONO and "'JetBrains Mono'" in out
                and out.count('"') == 2                      # only the attribute's own quotes
                and "color:#00DAFF" in out and "letter-spacing" in out)
    case("a mono label's style attribute survives the font stack",
         "every deck kicker rendered plain, 17 Oct", _mono_style_is_well_formed)

    case("a quotation card carries the deal it was built for", "Heaven SEN-2026-0048, 17 Oct",
         lambda: "update tasks set deal_id=%s where id=%s" in inspect.getsource(engine.deliver_quotation))

    def _caption_keeps_its_casing():
        # .capitalize() lowercases the rest: "AI production" printed "Ai production" on every
        # Heaven sample caption. Same fault as creative._label() on 23 Sep.
        cap = deck.film_caption({"categories": ["ai", "social-cut", "presenter"],
                                 "client": "Jumeirah Golf Estates"})
        return (cap == "AI production, social cut, presenter-led for Jumeirah Golf Estates"
                and deck.film_caption({"categories": ["2d-animation"]}) == "2D animation"
                and deck.film_caption({"categories": ["interviews"]}) == "Interviews")
    case("a film caption keeps the casing of its own labels",
         "Heaven deck p5, 17 Oct", _caption_keeps_its_casing)

    def _cta_icon_reads_on_a_photo():
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.cta("K", "H", "s", [{"title": "t", "body": "b", "icon": "check"}], {}, "", "Next")
        # a bare stroked icon vanished over the bright half of the background photograph
        return "background:rgba(10,10,12,.72)" in d.pages[0] and "<svg" in d.pages[0]
    case("a cta step icon stays legible over a background photograph",
         "Heaven deck v6 p12, 17 Oct", _cta_icon_reads_on_a_photo)

    def _icons_are_not_black():
        # stroke="currentColor" is NOT resolved by the PDF renderer: every icon in the Heaven
        # v5 and v6 decks rendered black on black, and the tinted panel behind it hid the fault.
        got = deck.icon("bulb", 40, "#00DAFF")
        if 'stroke="#00DAFF"' not in got or "currentColor" in got or 'width="40"' not in got:
            return False
        if deck.icon("bulb") == "" or "currentColor" not in deck.icon("bulb"):
            return False          # the attribute stays for anything rendering in a browser
        d = deck._Deck(_co("sensa"), "T", "#00DAFF", "", "")
        d.flow("K", "H", "", [{"label": "a", "title": "t", "body": "b", "icon": "bulb"}], "", "s")
        d.faq("K", "H", [{"q": "q", "a": "a", "icon": "clock"}], "", "s")
        d.cta("K", "H", "s", [{"title": "t", "body": "b", "icon": "check"}], {}, "", "N")
        # every page builder that draws an icon names the colour it is drawn in
        return all('stroke="#00DAFF"' in p and 'stroke="currentColor"' not in p for p in d.pages)
    case("a deck icon is drawn in the accent, never left to currentColor",
         "Heaven deck icons invisible, 17 Oct", _icons_are_not_black)

    def _icons():
        return (len(deck.ICONS) >= 16 and deck.icon("bulb").startswith("<svg")
                and deck.icon("nope") == "" and deck.icon("") == ""
                and deck.icon("<svg/>") == "<svg/>"
                and all('stroke="currentColor"' in v for v in deck.ICONS.values()))
    case("the house icon set is named, stroked and drops an unknown name",
         "Sensa deck icons, 17 Oct", _icons)

    def _flow_title_wraps():
        d = deck._Deck(_co("sensa"), "Test", "#2BB3E8", "", "")
        d.flow("K", "H", "", [{"label": "BUILD", "title": "The month in one run",
                               "body": "Every film produced together.", "icon": "layers"}], "", "s")
        pg = d.pages[0]
        # the overlap on Heaven p9 was a two-line title with no line-height. The card floor
        # must also clear the 72px icon panel, so read it rather than pin a number here.
        import re as _re_fl
        m = _re_fl.search(r"min-height:(\d+)px", pg)
        return "line-height:1.3" in pg and "<svg" in pg and m and int(m.group(1)) >= 300
    case("a wrapping step title cannot overlap its body", "Heaven deck p9 overlap, 17 Oct",
         _flow_title_wraps)

    def _faq_fills():
        d = deck._Deck(_co("sensa"), "Test", "#2BB3E8", "", "")
        d.faq("K", "H", [{"q": "q%d" % i, "a": "a"} for i in range(4)], "", "s")   # one column, big
        d.faq("K", "H", [{"q": "q%d" % i, "a": "a"} for i in range(6)], "", "s")   # two columns
        return ("grid-template-columns:1fr;" in d.pages[0] and "font-size:23px" in d.pages[0]
                and "grid-template-columns:1fr 1fr;" in d.pages[1])
    case("a short Q&A fills the page in one column", "Heaven deck black space, 17 Oct", _faq_fills)

    def _lead_play():
        d = deck._Deck(_co("sensa"), "Test", "#2BB3E8", "", "")
        d.lead("K", "T", "c", None, "", "s", play=True)
        d.lead("K", "T", "c", None, "", "s", play=False)
        return "border-left:66px solid #FFFFFF" in d.pages[0] and "66px solid" not in d.pages[1]
    case("a lead page can show a picture that is not a film", "Heaven avatar sheet, 17 Oct",
         _lead_play)

    case("render_spec can reach every SaaS page type", "Heaven retainer deck, 17 Oct",
         lambda: all('elif t == "%s"' % t in inspect.getsource(deck.render_spec)
                     for t in ("pricing", "flow", "showcase", "faq", "photos", "samples")))

    # --- a deck that sells content has to be able to show it (Heaven retainer deck, 17 Oct)
    def _spec_pages():
        src = inspect.getsource(deck.render_spec)
        return 'elif t == "photos"' in src and 'elif t == "samples"' in src
    case("render_spec can lay out a picture grid and a films page", "Heaven retainer deck, 17 Oct",
         _spec_pages)
    case("render_spec still refuses a page type it does not know", "Heaven retainer deck, 17 Oct",
         lambda: 'unknown page type' in inspect.getsource(deck.render_spec))

    # --- every health check query runs
    def _invariants():
        from . import invariants
        for c in invariants.CHECKS:
            db.query(c["sql"])
        return True
    # --- fitness: a warm-up waits for the 4x4 after it to settle (19 Oct 2026)
    _fit = __import__("cortex.fitness", fromlist=["x"])
    case("a watch warm-up is held while the session after it is still running", "fitness warm-up 19 Oct",
         lambda: "hold.add(" in inspect.getsource(_fit.reconcile_watch)
         and "running" in inspect.getsource(_fit.reconcile_watch))
    case("every cardio and lifting row carries its start time", "fitness times 20 Oct",
         lambda: '"startAt"' in inspect.getsource(_fit.pull))
    case("the fitness app pushes only rows changed since its last pull", "stale browser 21 Oct",
         lambda: (lambda h: "recordFp(state)" in h and "h!==fpHash(r)" in h and "if(!loadFp())" in h)(
             open(_web + "/fitness/index.html", encoding="utf-8").read()))
    # --- drone: the gear, crew and post rates were missing from the card, so every drone line
    #     blanked; and the shoot preset printed 50/25/25 under a 70% deposit note (22 Oct 2026)
    def _drone_emergy():
        """The card must reproduce the issued Emergy quotation: one Dubai day, Option A = 31,520."""
        from . import ratecard
        sec = [{"header": "X", "items": [
            {"desc": "g", "components": [{"item": "drone-inspire-3", "qty": 1}]},
            {"desc": "c", "components": [{"item": "drone-licensed-operator", "qty": 1},
                                         {"item": "drone-camera-operator-assistant", "qty": 1}]},
            {"desc": "p", "components": [
                {"item": "dxb-dubai-film-and-tv-commission", "qty": 1},
                {"item": "dxb-dubai-civil-aviation-authority", "qty": 1},
                {"item": "dxb-general-civil-aviation-authority", "qty": 1},
                {"item": "dxb-ministry-of-defence", "qty": 1},
                {"item": "dxb-administrative-fee", "qty": 1}]},
            {"desc": "d", "components": [{"item": "drone-raw-files-ssd", "qty": 1}]}]}]
        r = ratecard.price_lines("skyvision", sec)
        return not r["blanked"] and abs(r["lines_total"] - 31520) < 1

    case("a drone quotation prices off the card and matches the issued Emergy figure",
         "drone lines all blanked 22 Oct", _drone_emergy)

    def _drone_preset():
        """An aerial job holds a permit, not a studio: 70/30, never the shoot preset's 50/25/25."""
        from . import quotation
        p = quotation.presets().get("drone-aerial") or {}
        pay = " ".join(p.get("payment_lines") or [])
        terms = str(p.get("terms") or {})
        return (pay.startswith("70%") and "50%" not in pay and "25%" not in pay
                and "six hours per day inclusive of travel" in terms
                and "doubled for a twelve-hour day" in terms)

    case("the drone preset pays 70/30 and states the six-hour day",
         "50/25/25 printed under a 70% note 22 Oct", _drone_preset)

    def _samples_four():
        """Four films in ONE row: split 2+2 they made the page overflow under a chunky heading
        (Wasl technical proposal rendered 15 pages for 12, 22 Oct 2026)."""
        src = inspect.getsource(deck._Deck.samples)
        return "rows = [films] if n <= 4 else" in src and "4: 260" in src

    case("a samples page puts four films in one row", "Wasl proposal overflowed 22 Oct",
         _samples_four)

    def _permit_units():
        """Irish, 22 Oct 2026: the RATE was right and the UNIT was wrong, which is worse, because a
        unit error scales with the job. A civil-aviation clearance is ONE per application however
        many locations; a filming permit is per location; the MOD is per day and doubles on an
        extended day; the risk assessment is once per emirate, not once per shoot day."""
        from . import ratecard
        idx = ratecard.index("skyvision")
        want = {"dxb-dubai-civil-aviation-authority": "application",
                "dxb-general-civil-aviation-authority": "application",
                "dxb-administrative-fee": "application",
                "auh-gcaa": "application",
                "auh-administrative-fee": "application",
                "dxb-dubai-film-and-tv-commission": "location",
                "auh-nmc": "location",
                "dxb-ministry-of-defence": "day",
                "auh-mod": "day",
                "auh-department-of-transport": "day",
                "drone-risk-assessment-twilight": "emirate"}
        return all((idx.get(k) or {}).get("unit") == u for k, u in want.items())

    case("drone permit fees carry the unit that decides their quantity",
         "Irish's Wasl corrections 22 Oct", _permit_units)

    def _mod_double():
        """The MOD fee doubles on an extended day; the note on both MOD lines has to say so."""
        from . import ratecard
        idx = ratecard.index("skyvision")
        got = [idx.get(k) for k in ("dxb-ministry-of-defence", "auh-mod")]
        return all(g and g["rate"] == 2500 and "DOUBLED" in (g.get("note") or "")
                   and "extended" in (g.get("note") or "") for g in got)

    case("both Ministry of Defence lines carry the extended-day doubling",
         "MOD doubling unrecorded 22 Oct", _mod_double)

    case("a watch session whose log entry was deleted is not served to the burn", "false start 22 Oct",
         lambda: "c.deleted and c.watch->>'uid' = hs.uid" in inspect.getsource(_fit))
    case("a trimmed watch session serves the trimmed minutes and pro-rata kcal", "timer left on 22 Oct",
         lambda: (lambda r: r[2] == 50 and r[1] == 400.0)(_fit._trim({"start_at": __import__("datetime").datetime(2026,10,22,12,31),
             "end_at": __import__("datetime").datetime(2026,10,22,13,31), "kcal": 480, "trim_min": 50})))
    case("the nightly backup streams to disk and uploads in chunks", "backup OOM 23 Oct",
         lambda: (lambda b: "uploadType=resumable" in b and 'f"/tmp/' not in b and "backup_last_ok" in b)(
             open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(
                 __import__("os").path.abspath(__file__))), "backup_drive.py"), encoding="utf-8").read()))
    from . import storyboard as _sb

    def _sb_times():
        t9, t8 = _sb.timecodes(9, 30), _sb.timecodes(8, 30)
        ok = lambda t: t[0][0] == 0 and t[-1][1] == 30 and all(a < b for a, b in t) and             all(t[i][1] == t[i + 1][0] for i in range(len(t) - 1))
        return ok(t9) and ok(t8) and len(t9) == 9 and t9[-1] == (25, 30) and _sb.tc(65) == "1:05"
    case("storyboard timecodes are contiguous, whole seconds, end on the running time", "Qabilah storyboards 26 Oct",
         _sb_times)
    case("a storyboard voice over is printed only from the owner's words or a deal document", "Qabilah storyboards 26 Oct",
         lambda: _sb.vo_source("The door is open.", 'use "The door is open." at the end', None) == "the owner's words"
         and _sb.vo_source("A line nobody wrote", "make it heartfelt", None) is None)
    case("a storyboard's 9:16 cut is reframed on the subject and stays inside the frame", "Majlis shot 9 bottle cut",
         lambda: _sb.StoryboardDeck._window(5)[0] == 0 and _sb.StoryboardDeck._window(95)[0] == 360 - 114
         and _sb.StoryboardDeck._window(50)[0] == 123 and _sb._x("x") == 50)
    return C


def run(fast: bool = False) -> dict:
    results = []
    for c in _cases():
        if fast and c["model"]:
            continue
        try:
            ok = bool(c["fn"]())
            err = ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
            traceback.print_exc()
        results.append({"name": c["name"], "incident": c["incident"], "ok": ok, "error": err})
    return {"passed": sum(r["ok"] for r in results), "failed": [r for r in results if not r["ok"]],
            "total": len(results)}


if __name__ == "__main__":
    res = run(fast="--fast" in sys.argv)
    for f in res["failed"]:
        print(f"FAIL  {f['name']}  [{f['incident']}]  {f['error']}")
    print(f"{res['passed']}/{res['total']} passed")
    sys.exit(1 if res["failed"] else 0)
