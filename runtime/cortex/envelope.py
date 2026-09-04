"""Envelope rule compiler — the dumb-waiter doctrine applied to from/cc/bcc.

The skill's RULES are the only source of envelope behaviour. Whenever a skill's rules change
(local or universal, via cockpit, Talk, or a confirmed correction-rule), compile_skill() reads
the effective rule set and distils the envelope-relevant parts into STRUCTURED config stored on
`skills.envelope` — data: inspectable, recompilable, editable by editing the rules themselves.
The send path executes that config deterministically; no per-send model calls, no regex over
prose, no hardcoded cc behaviour. Code keeps exactly two SAFETY invariants (not behaviour):
one recipient per send (gmail.send_message) and catch-all mailboxes never send (_email_envelope).
"""
from __future__ import annotations

import json

from . import db, provider, store

_MIGRATE = "alter table skills add column if not exists envelope jsonb"


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def compile_skill(skill_id: int) -> dict:
    """Distil ONE skill's effective rules (universal + local) into envelope config and store it."""
    ensure_schema()
    skill = store.get_skill(skill_id)
    if not skill:
        return {}
    uni, loc = store.effective_rules(skill)
    rules = list(uni) + list(loc)
    cfg: dict = {"cc_add": [], "bcc_add": [], "never_cc": []}
    if rules:
        try:
            out = provider.think_json(
                "You COMPILE email-envelope behaviour from a skill's standing rules. Extract ONLY explicit "
                "envelope directives. Return JSON: "
                '{"cc_add": ["<emails a rule says to cc on EVERY send from this lane>"], '
                '"bcc_add": ["<emails to bcc on every send>"], '
                '"never_cc": ["sender" if any rule says never to cc/copy the SENDER of the email, '
                '"recipient" if a rule says never to cc the To recipient, plus any specific email '
                'addresses a rule forbids cc\'ing], '
                '"notes": "<one short line on any envelope rule you could NOT express in this schema>"}.'
                " STRICT: an email address mentioned as an example, a contact, or part of some other "
                "process is NOT a cc directive. A cc directive that is CONDITIONAL (only for certain "
                "email types or situations) goes in notes, never in cc_add — cc_add fires on every send.",
                "\n".join(f"- {r}" for r in rules)[:6000],
                model=provider.MODEL_ROUTER, purpose="envelope-compile")
            if isinstance(out, dict):
                cfg["cc_add"] = [e.strip().lower() for e in (out.get("cc_add") or [])
                                 if isinstance(e, str) and "@" in e][:8]
                cfg["bcc_add"] = [e.strip().lower() for e in (out.get("bcc_add") or [])
                                  if isinstance(e, str) and "@" in e][:8]
                cfg["never_cc"] = [str(x).strip().lower() for x in (out.get("never_cc") or [])
                                   if isinstance(x, str) and x.strip()][:8]
                if out.get("notes"):
                    cfg["notes"] = str(out["notes"])[:300]
        except Exception:  # noqa: BLE001 — a failed compile keeps the previous config rather than none
            prev = skill.get("envelope")
            if isinstance(prev, dict):
                return prev
    db.execute("update skills set envelope=%s, updated_at=now() where id=%s", (json.dumps(cfg), skill_id))
    # EVERY rule change re-checks the whole company, so a recipient can never end up covering one lane
    # and silently missing the rest. Fail-soft: an audit hiccup must never block a rule from saving.
    try:
        if skill.get("company_id") and skill.get("skill_key") in EMAIL_LANES:
            check_coverage(skill["company_id"])
    except Exception:  # noqa: BLE001
        pass
    return cfg


def compile_key(skill_key: str) -> int:
    """Universal rules for a skill changed — recompile that skill for every company."""
    n = 0
    for r in db.query("select id from skills where skill_key=%s", (skill_key,)):
        try:
            compile_skill(r["id"])
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def get(skill: dict) -> dict:
    """The compiled envelope config for a skill; compiles lazily on first use."""
    env = (skill or {}).get("envelope")
    if isinstance(env, dict):
        return env
    if skill and skill.get("id"):
        try:
            return compile_skill(skill["id"])
        except Exception:  # noqa: BLE001
            return {}
    return {}


def compile_all() -> int:
    """One-off: compile every skill that carries rules (rule-less skills get the empty config free)."""
    n = 0
    for r in db.query("select id from skills"):
        try:
            compile_skill(r["id"])
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ---------- COMPANY COVERAGE: a cc rule that only covers SOME lanes is a bug, not a config ----------
#
# "Always CC Ben" lived on Tabscanner's sales-first-response skill and nowhere else, so Ben rode inbound
# sales replies and was missed by every owner-composed draft, follow-up and project email (card 474,
# 4 Sep 2026). Rashad set the rule, watched it fail, and reasonably concluded it had been ignored.
#
# A standing recipient belongs in ONE place: the company profile's always_cc, which the envelope reads
# for every email that company sends. These functions find the addresses that are only PARTIALLY
# applied and say so, rather than leaving a rule that works in one lane and silently fails in the rest.

# The lanes that actually send client email; a cc rule on any of them is a claim about our correspondence.
EMAIL_LANES = ("sales-first-response", "sales-followup", "sales-quotation", "email-handling",
               "sales-scheduling", "sales-triage", "lead-qualification")


def audit_company(company_id: int) -> dict:
    """Who is cc'd WHERE for this company, and who is only half-covered.

    Returns {profile_cc, by_address: {email: {"on": [lanes], "missing": [lanes]}}, partial: [emails]}.
    An address already in the profile's always_cc is complete by definition - the profile applies to
    every send - so it is never reported as partial."""
    from . import profile
    prof_cc = {str(e).strip().lower() for e in ((profile.get(company_id) or {}).get("always_cc") or [])
               if isinstance(e, str) and "@" in e}
    lanes = {}
    for r in db.query("select id, skill_key from skills where company_id=%s and skill_key = any(%s)",
                      (company_id, list(EMAIL_LANES))):
        cfg = get(store.get_skill(r["id"])) or {}
        lanes[r["skill_key"]] = {str(e).strip().lower() for e in (cfg.get("cc_add") or [])
                                 if isinstance(e, str) and "@" in e}
    by_addr: dict = {}
    for addr in {a for s in lanes.values() for a in s}:
        if addr in prof_cc:
            continue                     # the profile already covers every send
        on = sorted(k for k, v in lanes.items() if addr in v)
        missing = sorted(k for k in lanes if addr not in lanes[k])
        by_addr[addr] = {"on": on, "missing": missing}
    partial = sorted(a for a, v in by_addr.items() if v["on"] and v["missing"])
    return {"profile_cc": sorted(prof_cc), "lanes": sorted(lanes), "by_address": by_addr,
            "partial": partial}


def promote_to_company(company_id: int, email: str) -> dict:
    """Move a standing recipient to the ONE place that covers every send: the company profile."""
    from psycopg.types.json import Json
    from . import profile
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("not an email address")
    data = dict(profile.get(company_id) or {})
    cc = [str(e).strip().lower() for e in (data.get("always_cc") or []) if isinstance(e, str)]
    if email not in cc:
        cc.append(email)
    data["always_cc"] = cc
    db.execute("update company_profiles set data=%s, updated_at=now() where company_id=%s",
               (Json(data), company_id))
    return {"always_cc": cc}


def check_coverage(company_id: int, notify: bool = True) -> dict:
    """Run the audit and TELL the owner about a half-applied recipient. Deduped per address, so a
    known gap is raised once, not on every rule change."""
    rep = audit_company(company_id)
    if notify and rep["partial"]:
        from . import notifications, store as _st
        co = _st.get_company(company_id) or {}
        for addr in rep["partial"]:
            v = rep["by_address"][addr]
            notifications.notify(
                f"{addr} is copied on some {co.get('name') or 'company'} emails but not others",
                f"On: {', '.join(v['on'])}. NOT on: {', '.join(v['missing'])}. If they should be on "
                "every email, say so and I will move it to the company rule so no lane can miss it.",
                priority="normal", category="reminder", company_id=company_id,
                dedup_key=f"cc-partial:{company_id}:{addr}")
    return rep
