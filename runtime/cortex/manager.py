"""The manager — keeper of the standard.

Reviews the worker's draft against the skill's craft, the FULL company context (voice, audience,
dos/donts, grounding), and the FULL rule set (universal + local + related skills' rules — the exact
same rules the worker was told to follow), then renders a verdict the owner can trust: pass / revise /
escalate, with a confidence and a one-line reason. 'escalate' or low confidence means it must reach
the owner even on an auto lane.
"""
from __future__ import annotations

from . import provider, store, worker


def check(skill: dict, company: dict, draft: str, request: dict) -> dict:
    # The exact rules the worker drafted under: this skill's + its related skills' (CC directives excluded —
    # they are envelope config the body can never show, so the Manager must not flag their absence).
    rules = list(worker._rule_lines(skill))
    for s in worker.related_skills(skill, company):
        rules += [f"[{s['name']}] {r}" for r in worker._rule_lines(s)]
    brief = request.get("brief") if isinstance(request, dict) else request
    # System facts the Manager must judge WITH, not guess about: what is genuinely attached to the card,
    # that sign-offs/signatures are appended by the send system (never a violation in the body), and any
    # cc set on the envelope. Without these it flags phantom problems the owner then has to disprove.
    facts = []
    if isinstance(request, dict):
        atts = [r.get("filename") for r in (request.get("attach_docs") or []) if r.get("filename")]
        atts += [n for n in (request.get("attachment_names") or []) if n]
        if atts:
            facts.append("Files GENUINELY ATTACHED to this email (claiming these is correct, not a "
                         "violation): " + ", ".join(atts))
        if request.get("cc_extra"):
            facts.append("Extra cc set on the envelope by the owner: " + ", ".join(request["cc_extra"]))
        if request.get("from_email"):
            facts.append("The envelope From address is SET BY THE SYSTEM to " + str(request["from_email"]) +
                         " — the sender is correct by construction, never judge it from the draft.")
        if request.get("high_value"):
            facts.append("HIGH-VALUE routing is active: the company's configured high-value cc set is applied "
                         "to the envelope by the send system — a missing cc in the body is never a violation.")
    facts.append("The send system STRIPS any closing sign-off from the draft and appends the official "
                 "closing + signature block itself — a sign-off in the draft body is cosmetic, never a "
                 "rule violation.")
    facts.append("Where the Task brief carries the owner's own instructions, content he explicitly "
                 "asked for is authorised — never flag it as 'without basis'.")
    try:   # the same URL allowlist the invented-link guard uses — the manager verifies, never guesses
        import json as _json
        import re as _re
        _urls = sorted(set(_re.findall(r"https?://[^\s<>\\\"')\]]+", _json.dumps(request, default=str))))
        if _urls:
            facts.append("REAL LINKS available to the drafter (any URL in the draft NOT on this list is "
                         "invented and must be flagged): " + ", ".join(_urls[:25]))
        else:
            facts.append("NO links were provided to the drafter — ANY URL in the draft is invented and "
                         "must be flagged.")
    except Exception:  # noqa: BLE001
        pass
    facts.append("Lifecycle and cadence instructions in the Task brief (e.g. 'then the Dormant flow', "
                 "'ONE email only', follow-up sequencing, reminders) are executed by the PIPELINE, never "
                 "written into the email — their absence from the draft body is never an issue.")
    # Computed calendar: weekday arithmetic is exactly the kind of fact a model gets wrong (a real
    # manager verdict called Thu 3 Sep a date error). Code stamps the next 4 weeks; the model reads.
    from datetime import datetime, timedelta
    from .schedule import _GST
    today = datetime.now(_GST)
    cal = ", ".join((today + timedelta(days=i)).strftime("%a %-d %b") for i in range(28))
    facts.append(f"CALENDAR (computed, authoritative): today is {today.strftime('%A %-d %B %Y')}. The next "
                 f"28 days are: {cal}. Judge every date/weekday mention in the draft ONLY against this "
                 "list — NEVER compute weekdays yourself, and never call a date wrong that this list "
                 "confirms.")
    facts_block = "SYSTEM FACTS (authoritative — judge with these):\n" + "\n".join(f"- {f}" for f in facts)
    system = (
        "You are the department Manager at Cortex — the keeper of the standard. Review a worker's draft "
        "and decide if it is ready to go out: does it follow EVERY standing rule, match the company's "
        "brand and voice, and do the task well? Be strict but fair: flag only genuine problems, never "
        "nitpicks. Choose a verdict: 'pass' (ready as-is), 'revise' (fixable issues the worker should "
        "redo), or 'escalate' (needs the owner's judgement: a rule is ambiguous, the draft makes a risky "
        "or unverifiable claim, or you are simply not confident). State your confidence: high, medium, low.")
    user = (
        worker._now_line() + "\n\n"
        + worker._company_context(company) + "\n\n"
        f"Task: {brief}\n\n"
        + facts_block + "\n\n"
        "Standing rules the draft MUST follow:\n"
        + ("\n".join(f"- {r}" for r in rules) or "- (none set yet)")
        + f"\n\nDRAFT:\n{draft}\n\n"
        'Return JSON: {"verdict":"pass|revise|escalate","confidence":"high|medium|low",'
        '"summary":"one short line the owner reads","issues":["concrete rule/brand problems, [] if none"],'
        '"rule_refs":["the specific rules that were broken, if any"]}\n'
        "Work in this order: verify facts against SYSTEM FACTS, list issues, THEN write the summary. "
        "The summary is ONE sentence, max 140 characters, and a faithful compression of the issues you "
        "listed and nothing else: no reasoning, no re-evaluation, no mention of things that are correct, "
        "never a claim that is not in issues. If every issue is a style/judgement call rather than a hard "
        "rule break, verdict is 'pass' and the summary says 'style suggestions only' plus the main one. "
        "If issues is empty, verdict is 'pass' and the summary is simply what the draft does well, in "
        "five words or fewer.")
    out = provider.think_json(system, user, max_tokens=1500,
                              purpose=f"manager:{skill.get('skill_key', '')}", company=company.get("slug"))

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
