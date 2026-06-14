"""The manager — judges the worker's draft against skill, brand, and rules."""
from __future__ import annotations

from . import provider


def check(skill: dict, company: dict, draft: str, request: dict) -> dict:
    ctx = company.get("context") or {}
    rules = skill.get("rules") or []
    brief = request.get("brief") if isinstance(request, dict) else request
    system = ("You are Cortex's manager. Decide whether a draft is ready to go out: does it match the "
              "company's brand/voice, follow the standing rules, and do the task well? Be strict but fair — "
              "only flag genuine problems, not nitpicks.")
    user = (f"Company: {company['name']} (voice: {ctx.get('voice', 'n/a')})\n"
            f"Task: {brief}\n"
            f"Standing rules: {rules or 'none'}\n\n"
            f"DRAFT:\n{draft}\n\n"
            'Return JSON: {"aligned": boolean, "issues": ["concrete problems, empty if none"], '
            '"summary": "one short line"}')
    out = provider.think_json(system, user, max_tokens=1500)
    return {
        "aligned": bool(out.get("aligned", True)),
        "issues": [i for i in (out.get("issues") or []) if i],
        "summary": out.get("summary", ""),
    }
