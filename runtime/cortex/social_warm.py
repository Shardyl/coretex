"""Warm-up engine: before a persona connects with a target, it warms them by commenting on their recent
posts. This module drafts the comment IN THE PERSONA'S VOICE and lands it as a one-off `social_action`
approval card (post + comment shown together) for the owner to approve. Approved comments execute via the
existing social_action -> queue -> poller path. Likes/views run automatically elsewhere; comments are always
approved first (the persona speaking in public).

Targets come from the harvested buyers (already in the CRM, already the persona's ICP). The runner (warm.py)
reads each target's recent post + text and posts them here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Json

from . import db, social, social_config, store, worker

# Harvested-buyer pool filters shared by warm + connect targeting: never an anchor (they're the watering
# hole, not the prospect), never anyone already invited or marked unreachable.
_POOL_WHERE = (
    "tags @> '[\"anchor-harvest\"]'::jsonb and linkedin is not null "
    "and coalesce(stage,'Cold')='Cold' "
    "and not (tags @> '[\"invited\"]'::jsonb) and not (tags @> '[\"invite-skip\"]'::jsonb) "
    "and lower(trim(coalesce(first_name,'')||' '||coalesce(last_name,''))) not in "
    "(select lower(trim(name)) from social_anchors)")

# The warm-ladder thresholds (LOCKED strategy): comment on a target ~5 times, then unlock the connect.
WARM_COMMENTS_TARGET = 5     # stop queueing new comment cards for a person once this many exist
WARM_COMMENTS_TO_CONNECT = 4  # this many APPROVED (done) comment cards unlocks the connect


def _norm(url: str) -> str:
    return (url or "").split("?")[0].rstrip("/").lower()


def _comment_counts(account: str, person_url: str) -> tuple[int, int]:
    """(cards_total, cards_done) comment cards created for this person on this account. total gates how many
    more to queue; done gates connect-readiness."""
    pn = _norm(person_url)
    rows = db.query(
        "select status from tasks where kind='social_action' and request->>'action'='comment' "
        "and request->>'account'=%s and lower(rtrim(split_part(request->>'person','?',1),'/'))=%s", (account, pn))
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    return total, done


def warm_targets(account: str, n: int = 5) -> list[dict]:
    """The next N harvested buyers to warm (top fit-score first). Skips anyone who already has enough comment
    cards (WARM_COMMENTS_TARGET) - they move on to the connect stage."""
    rows = db.query(
        f"select first_name, last_name, linkedin from crm_master where {_POOL_WHERE} "
        "order by tier::int desc nulls last limit 120")
    out = []
    for r in rows:
        url = r["linkedin"]
        if not url:
            continue
        total, _ = _comment_counts(account, url)
        if total >= WARM_COMMENTS_TARGET:          # already fully warmed -> skip (it's in the connect queue)
            continue
        out.append({"name": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "there", "linkedin": url})
        if len(out) >= n:
            break
    return out


def connect_targets(account: str, n: int = 10) -> list[dict]:
    """The next N harvested buyers to CONNECT with - but ONLY the WARMED ones: those with at least
    WARM_COMMENTS_TO_CONNECT approved comment cards. Cold, un-warmed buyers are never surfaced here; they
    stay in the warm queue until the ladder is climbed. Tier-ranked."""
    rows = db.query(
        f"select id, first_name, last_name, job_title, tier, linkedin from crm_master where {_POOL_WHERE} "
        "order by tier::int desc nulls last limit 200")
    out = []
    for r in rows:
        _, done = _comment_counts(account, r["linkedin"])
        if done < WARM_COMMENTS_TO_CONNECT:
            continue
        out.append({"id": r["id"],
                    "name": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "there",
                    "headline": (r["job_title"] or "")[:120], "tier": r["tier"], "linkedin": r["linkedin"]})
        if len(out) >= n:
            break
    return out


def record_connect(account: str, linkedin: str, ok: bool, detail: str = "") -> dict:
    """Record an invite outcome on the harvested contact. Sent -> tag 'invited', stage Cold->Contacted,
    history event. Failed (Follow-only / pending / already connected) -> tag 'invite-skip' so the target
    never re-queues; nothing else on the record is touched."""
    row = db.one("select id, tags, stage from crm_master where lower(linkedin)=lower(%s)", (linkedin,))
    if not row:
        return {"ok": False, "detail": "no contact with that linkedin url"}
    ev = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "event": "linkedin_invite_sent" if ok else "linkedin_invite_failed",
          "account": account, "detail": (detail or "")[:300]}
    tags = sorted(set(row.get("tags") or []) | {"invited" if ok else "invite-skip"})
    if ok and (row.get("stage") or "Cold") == "Cold":
        db.execute("update crm_master set history = history || %s::jsonb, tags=%s::jsonb, stage='Contacted', "
                   "updated_at=now() where id=%s", (Json([ev]), Json(tags), row["id"]))
    else:
        db.execute("update crm_master set history = history || %s::jsonb, tags=%s::jsonb, updated_at=now() "
                   "where id=%s", (Json([ev]), Json(tags), row["id"]))
    return {"ok": True, "contact_id": row["id"], "recorded": ev["event"]}


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
    people_done: set[str] = set()   # at most ONE new comment card per person per run (natural cadence)
    for it in items or []:
        post = it.get("post_url")
        text = (it.get("post_text") or "").strip()
        name = (it.get("name") or "there").strip()
        person = _norm(it.get("profile", ""))
        if not post or not text or post in seen or post in fresh:
            continue
        if person and person in people_done:
            continue
        total, _ = _comment_counts(account, person) if person else (0, 0)
        if total >= WARM_COMMENTS_TARGET:   # already fully warmed -> no more comment cards
            continue
        fresh.append(post)
        if person:
            people_done.add(person)
        comment = _draft_comment(text, name, company_id, person_key)
        if not comment:
            continue
        t = social.post_action_card(company_id, account, persona, "comment", target=post, content=comment,
                                    person=it.get("profile", ""))
        body = (f"Warming up {name} before connecting.\n\nTHEIR POST:\n\"{text[:500]}\"\n\n"
                f"{persona.upper()}'S COMMENT:\n{comment}")
        store.update_task(t["id"], title=f"{persona}: comment to warm {name}", draft=body)
        queued += 1
    if fresh:
        db.setting_set(f"warm_seen:{account}", (list(seen) + fresh)[-500:])
    return {"queued": queued}
