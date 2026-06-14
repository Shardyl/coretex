"""The worker — does the task per its skill (produces the deliverable)."""
from __future__ import annotations

from . import provider


def _company_context(company: dict) -> str:
    ctx = company.get("context") or {}
    parts = [f"Company: {company['name']}"]
    if company.get("north_star"):
        parts.append(f"Primary goal: {company['north_star']}")
    for k, label in (("voice", "Voice/tone"), ("audience", "Audience"),
                     ("products", "Products/services"), ("dos", "Always"), ("donts", "Never")):
        if ctx.get(k):
            parts.append(f"{label}: {ctx[k]}")
    return "\n".join(parts)


def _rules_block(skill: dict) -> str:
    rules = skill.get("rules") or []
    if not rules:
        return ""
    return "Standing rules you MUST follow:\n" + "\n".join(f"- {r}" for r in rules)


def draft(skill: dict, company: dict, request: dict,
          correction: str | None = None, manager_feedback: list[str] | None = None) -> str:
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill.",
        _company_context(company),
        skill.get("craft") or "",
        _rules_block(skill),
        "Produce the deliverable only — no preamble, no explanation, no meta-commentary.",
    ]))
    user = [f"Task: {request.get('brief') if isinstance(request, dict) else request}"]
    if manager_feedback:
        user.append("Your manager flagged these to fix:\n- " + "\n- ".join(manager_feedback))
    if correction:
        user.append(f"The owner corrected your previous draft. Apply this and produce a new version:\n{correction}")
    return provider.think(system, "\n\n".join(user), think_hard=True, max_tokens=6000)


def infer_rule(skill: dict, correction: str, old_draft: str, new_draft: str) -> dict:
    """Turn a one-off correction into a standing rule, if it implies one."""
    return provider.think_json(
        "You convert an owner's correction into a concise general standing rule for a skill — but ONLY "
        "if the correction implies a durable preference. One-offs with no general lesson are not rules.",
        f"Skill: {skill['name']}\nThe owner's correction: {correction}\n\n"
        'Return JSON: {"is_rule": boolean, "rule": "a short imperative rule (or empty string)"}',
    )
