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
