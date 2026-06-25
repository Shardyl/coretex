"""
social.py - turns a runner-produced LinkedIn shift plan into the owner's approval card, and
raises the logged-out alert. The card DATA (governed plan + targets) comes from the runner's
governor; this module only creates the Cortex task the owner approves.

  - social_shift   : the daily approve-the-shift card (outward -> biometric step-up to approve).
  - social_relogin : the session-dropped alert (browser is auto-reopened on the runner; the owner
                     just remotes in, logs back in, and marks this done).
"""
from . import db, store

_DEFAULT_SKILL = "outreach-linkedin-sequences"


def _skill_id(company_id, key=_DEFAULT_SKILL):
    sk = store.get_skill_by_key(company_id, key)
    return sk["id"] if sk else None


def post_shift_card(company_id, account, persona, plan, strategy,
                    connect_targets=None, engage_targets=None, date="", week=None, phase=""):
    """Create today's approve-the-shift card and land it straight in the Inbox.
    plan = {invites, messages, likes, comments, profile_views, invites_week, ...}."""
    req = {
        "account": account, "persona": persona, "date": date, "week": week, "phase": phase,
        "plan": plan, "strategy": strategy,
        "connect_targets": connect_targets or [], "engage_targets": engage_targets or [],
        "invites_week": plan.get("invites_week", ""),
    }
    t = store.create_task(company_id, _skill_id(company_id), "social_shift", req)
    title = f"{persona} - today's LinkedIn shift" + (f"  (Week {week}, {phase})" if week else "")
    store.update_task(t["id"], title=title, draft=strategy, status="awaiting_approval")
    return t


def post_relogin_card(company_id, account, persona,
                      machine="the office computer (Chrome Remote Desktop)"):
    """Raise the logged-out alert, deduped per account (one open card at a time)."""
    open_card = db.one("select id from tasks where kind='social_relogin' "
                       "and status in ('awaiting_approval','awaiting_correction') "
                       "and request->>'account'=%s order by id desc limit 1", (account,))
    if open_card:
        return open_card
    db.setting_set(f"social_loggedout:{account}", True)
    req = {"account": account, "persona": persona, "machine": machine}
    t = store.create_task(company_id, _skill_id(company_id), "social_relogin", req)
    title = f"{persona}'s LinkedIn is logged out - quick re-login needed"
    body = (f"{persona}'s LinkedIn session dropped, so the daily shifts are paused.\n\n"
            f"Chrome is already open on {machine} at the LinkedIn login page. Connect remotely, "
            f"type the password and hit Sign in, then mark this done. Nothing else runs until the "
            f"session is back, so this never silently stalls.")
    store.update_task(t["id"], title=title, draft=body, status="awaiting_approval")
    return t
