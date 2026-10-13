"""What each deal stage MEANS, in one place (architecture audit, 13 Oct 2026, step 2a).

Before this, CLOSED_STAGES, NO_CHASE_STAGES, OPEN_SALES_STAGES, _DELIVERY_STAGES, PLAN_STAGES and a dozen hand-written
SQL lists disagreed: Dormant was closed for reminders but open for the chase clock; Close & review kept chasing while
a client's reply to it was never matched to the deal; a won deal kept the quote-chasing rhythm. Every question about
a stage is now a lookup here. This module is pure data: it imports nothing from Cortex, so anything may import it.

RHYTHM, the automatic contact a deal in that stage gets:
  default    the company's follow-up cadence (chases, check-ins, revivals, then rest as Dormant)
  closing    the quote-chasing rhythm (every 2 working days, then twice a week, then weekly)
  won        won-work check-ins / payment follow-ups on the company cadence, NEVER the closing rhythm
  recurring  a quarterly check-in, forever
  revival    a soft revival every ~90 days, twice, then rest
  None       no automatic contact at all from the deal (Nurture has its own account-level programme)
"""
from __future__ import annotations

STAGES: dict[str, dict] = {
    #                 group      rhythm        cancel what Cortex set on entry   client mail attaches to it
    "Opportunity":   {"group": "sales",  "rhythm": "default",   "cancel_on_entry": False, "correspondence": True},
    "Quote":         {"group": "sales",  "rhythm": "closing",   "cancel_on_entry": False, "correspondence": True},
    "Booked":        {"group": "won",    "rhythm": "won",       "cancel_on_entry": False, "correspondence": True},
    "Production":    {"group": "won",    "rhythm": "won",       "cancel_on_entry": False, "correspondence": True},
    "Delivered":     {"group": "won",    "rhythm": "won",       "cancel_on_entry": False, "correspondence": True},
    "Final Payment": {"group": "won",    "rhythm": "won",       "cancel_on_entry": False, "correspondence": True},
    "Recurring":     {"group": "won",    "rhythm": "recurring", "cancel_on_entry": False, "correspondence": True},
    "Close & review": {"group": "won",   "rhythm": None,        "cancel_on_entry": False, "correspondence": True},
    "Nurture":       {"group": "won",    "rhythm": None,        "cancel_on_entry": False, "correspondence": True},
    "Completed":     {"group": "closed", "rhythm": None,        "cancel_on_entry": True,  "correspondence": True},
    "Dormant":       {"group": "closed", "rhythm": "revival",   "cancel_on_entry": True,  "correspondence": True},
    "Lost":          {"group": "closed", "rhythm": None,        "cancel_on_entry": True,  "correspondence": False},
}

SALES = tuple(s for s, v in STAGES.items() if v["group"] == "sales")                    # Opportunity, Quote
DELIVERY = ("Booked", "Production", "Delivered", "Final Payment", "Recurring")         # live project work
WON = tuple(s for s, v in STAGES.items() if v["group"] == "won") + ("Completed",)
CANCEL_ON_ENTRY = tuple(s for s, v in STAGES.items() if v["cancel_on_entry"])          # Completed, Dormant, Lost
NO_RHYTHM = tuple(s for s, v in STAGES.items() if v["rhythm"] is None)
NO_CORRESPONDENCE = tuple(s for s, v in STAGES.items() if not v["correspondence"])     # Lost

REVIVAL_CADENCE = {"name": "revival", "skip_weekends": True,
                   "steps": [{"after_days": 90, "repeat": 2, "action": "revive"}]}


def rhythm(stage: str | None) -> str | None:
    return (STAGES.get(stage or "") or {}).get("rhythm")


def chases(stage: str | None) -> bool:
    """Does a deal in this stage get any automatic contact from its own clock?"""
    return rhythm(stage) is not None


def sql_list(stages: tuple) -> str:
    """('a','b') for an SQL IN clause, from a tuple of stage names (all constants defined above)."""
    return "(" + ",".join("'" + s.replace("'", "''") + "'" for s in stages) + ")"
