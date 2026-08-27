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
