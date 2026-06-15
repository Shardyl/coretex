"""The manager — keeper of the standard.

Reviews the worker's draft against the skill's craft, the company's brand, and the FULL rule set
(universal + this company's local rules — the exact same rules the worker was told to follow), then
renders a verdict the owner can trust: pass / revise / escalate, with a confidence and a one-line
reason. 'escalate' or low confidence means it must reach the owner even on an auto lane.
"""
from __future__ import annotations

from . import provider, store


def check(skill: dict, company: dict, draft: str, request: dict) -> dict:
    ctx = company.get("context") or {}
    universal = store.get_universal_rules(skill.get("skill_key", "")) if skill.get("skill_key") else []
    local = skill.get("rules") or []
    rules = list(universal) + list(local)
    brief = request.get("brief") if isinstance(request, dict) else request
    system = (
        "You are the department Manager at Cortex — the keeper of the standard. Review a worker's draft "
        "and decide if it is ready to go out: does it follow EVERY standing rule, match the company's "
        "brand and voice, and do the task well? Be strict but fair: flag only genuine problems, never "
        "nitpicks. Choose a verdict: 'pass' (ready as-is), 'revise' (fixable issues the worker should "
        "redo), or 'escalate' (needs the owner's judgement: a rule is ambiguous, the draft makes a risky "
        "or unverifiable claim, or you are simply not confident). State your confidence: high, medium, low.")
    user = (
        f"Company: {company['name']} (voice: {ctx.get('voice', 'n/a')})\n"
        f"Task: {brief}\n"
        "Standing rules the draft MUST follow:\n"
        + ("\n".join(f"- {r}" for r in rules) or "- (none set yet)")
        + f"\n\nDRAFT:\n{draft}\n\n"
        'Return JSON: {"verdict":"pass|revise|escalate","confidence":"high|medium|low",'
        '"summary":"one short line the owner reads","issues":["concrete rule/brand problems, [] if none"],'
        '"rule_refs":["the specific rules that were broken, if any"]}')
    out = provider.think_json(system, user, max_tokens=1500)

    verdict = (out.get("verdict") or "pass").lower().strip()
    if verdict not in ("pass", "revise", "escalate"):
        verdict = "pass"
    confidence = (out.get("confidence") or "high").lower().strip()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    issues = [i for i in (out.get("issues") or []) if i]
    return {
        "aligned": verdict == "pass",                                   # back-compat with existing callers
        "verdict": verdict,
        "confidence": confidence,
        "summary": out.get("summary", ""),
        "issues": issues,
        "rule_refs": [r for r in (out.get("rule_refs") or []) if r],
        # escalate = the Manager wants the owner's eyes regardless of any auto setting.
        "escalate": verdict == "escalate" or confidence == "low",
    }
