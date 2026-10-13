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
         lambda: "A MEETING IS CONTACT" in inspect.getsource(gate.check)
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
         and "update_opportunity" in inspect.getsource(__import__("cortex.capabilities", fromlist=["x"])))
    case("a file sent on WhatsApp is kept for the next message", "iTellu brief PDF",
         lambda: "save_data_url(" in inspect.getsource(__import__("cortex.api", fromlist=["x"]).talk_from_whatsapp))

    # --- every health check query runs
    def _invariants():
        from . import invariants
        for c in invariants.CHECKS:
            db.query(c["sql"])
        return True
    case("every health check query runs", "invariants", _invariants)
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
