"""Warm-up engine: before a persona connects with a target, it warms them by commenting on their recent
posts. This module drafts the comment IN THE PERSONA'S VOICE and lands it as a one-off `social_action`
approval card (post + comment shown together) for the owner to approve. Approved comments execute via the
existing social_action -> queue -> poller path. Likes/views run automatically elsewhere; comments are always
approved first (the persona speaking in public).

Targets come from the harvested buyers (already in the CRM, already the persona's ICP). The runner (warm.py)
reads each target's recent post + text and posts them here.
"""
from __future__ import annotations

from . import db, social, social_config, store, worker


def warm_targets(account: str, n: int = 5) -> list[dict]:
    """The next N harvested buyers to warm (top fit-score first, skipping any already queued for warming)."""
    cfg = social_config.get_account(account) or {}
    company_id = cfg.get("company_id", 5)
    done = set(db.setting_get(f"warm_targets_done:{account}") or [])
    rows = db.query(
        "select first_name, last_name, linkedin from crm_master where tags @> '[\"anchor-harvest\"]'::jsonb "
        "and linkedin is not null order by tier::int desc nulls last limit 60")
    out = []
    for r in rows:
        url = r["linkedin"]
        if not url or url in done:
            continue
        out.append({"name": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "there", "linkedin": url})
        if len(out) >= n:
            break
    return out


def _draft_comment(post_text: str, name: str, company_id: int, person_key: str) -> str:
    skill = store.get_skill_by_key(company_id, "outreach-linkedin-sequences")
    co = store.get_company(company_id)
    if not (skill and co):
        return ""
    brief = ("You are warming up a potential connection by commenting on their LinkedIn post BEFORE sending a "
             "connection request. Write a SHORT, genuine comment (1 to 2 sentences) that adds a real thought or a "
             "sharp, friendly take on what they said. Never 'great post', never generic praise, never salesy, "
             "never mention or pitch your own company. Just be a smart peer worth knowing.\n\n"
             f"{name}'s post:\n{post_text}")
    try:
        return worker.draft(skill, co, {"brief": brief}, author=person_key)   # author -> the persona's voice
    except Exception:  # noqa: BLE001
        return ""


def queue_warm(account: str, items: list[dict]) -> dict:
    """For each {name, profile, post_url, post_text}: draft a comment in the persona's voice and create a
    social_action approval card showing the post + the comment. Deduped per post. Returns {queued}."""
    cfg = social_config.get_account(account) or {}
    company_id = cfg.get("company_id", 5)
    persona = cfg.get("persona", "Paul Anderson")
    person_key = cfg.get("person_key", "paul")
    seen = set(db.setting_get(f"warm_seen:{account}") or [])
    queued = 0
    fresh: list[str] = []
    for it in items or []:
        post = it.get("post_url")
        text = (it.get("post_text") or "").strip()
        name = (it.get("name") or "there").strip()
        if not post or not text or post in seen or post in fresh:
            continue
        fresh.append(post)
        comment = _draft_comment(text, name, company_id, person_key)
        if not comment:
            continue
        t = social.post_action_card(company_id, account, persona, "comment", target=post, content=comment)
        body = (f"Warming up {name} before connecting.\n\nTHEIR POST:\n\"{text[:500]}\"\n\n"
                f"{persona.upper()}'S COMMENT:\n{comment}")
        store.update_task(t["id"], title=f"{persona}: comment to warm {name}", draft=body)
        queued += 1
    if fresh:
        db.setting_set(f"warm_seen:{account}", (list(seen) + fresh)[-500:])
    return {"queued": queued}
