"""The worker — does the task per its skill (produces the deliverable)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import grounding, profile, provider, store

_GST = timezone(timedelta(hours=4))   # Gulf Standard Time (Dubai) — no DST


def _now_line() -> str:
    """Code-stamped current moment in GST. The drafter must anchor every day/date/time it mentions to
    this — proposing 'Monday or Wednesday' on a Tuesday is exactly the failure this prevents."""
    now = datetime.now(_GST)
    return ("Current date and time, code-stamped (GST / Dubai, UTC+4): "
            + now.strftime("%A %d %B %Y, %I:%M %p")
            + ". Anchor every day, date and time you mention to this moment — never guess or assume.")


def _company_context(company: dict, author: str | None = None) -> str:
    ctx = company.get("context") or {}
    parts = [f"Company: {company['name']}"]
    if company.get("north_star"):
        parts.append(f"Primary goal: {company['north_star']}")
    for k, label in (("voice", "Voice/tone"), ("audience", "Audience"),
                     ("products", "Products/services"), ("dos", "Always"), ("donts", "Never")):
        if ctx.get(k):
            parts.append(f"{label}: {ctx[k]}")
    base = "\n".join(parts)
    ground = grounding.for_company(company)   # Company Profile + brand guidelines + site source
    out = base + ("\n\n" + ground if ground else "")
    # Personal voice: when a piece is written AS a specific person (a LinkedIn comment/outreach/inbox reply,
    # a bylined or opinion post), write in THAT author's own voice (profile.voice.people.<author>). Neutral /
    # institutional content passes author=None and stays in the company voice above. Keyed like signatures.
    if author:
        try:
            pv = profile.resolve_voice(company.get("id"), author)
        except Exception:  # noqa: BLE001
            pv = None
        if pv:
            out += ("\n\nWrite this in the author's OWN first-person voice. Match it closely, keep the "
                    "personality, just keep it clean and professional:\n" + pv)
    return out


def _model_for(skill: dict) -> str:
    """Workers run on Sonnet by default; a skill tiered model='opus' overrides for high-quality work."""
    return provider.resolve_model(skill.get("model")) or provider.MODEL_FAST


# A Cc/Bcc SENDING directive anywhere in a rule, e.g. "...CC ben@x.com and BCC me@y.com". Same shape the
# envelope reads (engine._rule_recipients). These are actioned by the sending system, NOT instructions for the
# writer — hide any rule that sets a recipient from the drafter so the model can never echo it into the email
# body as a visible "system note".
_CC_DIRECTIVE = re.compile(r"\bb?cc\b\s+[\w.+-]+@", re.I)


def _rule_lines(skill: dict) -> list[str]:
    """The skill's effective rules (universal minus overrides + local), minus CC/BCC sending directives."""
    universal, local = store.effective_rules(skill)  # universal minus this company's overrides, then local
    return [r for r in (list(universal) + list(local)) if not _CC_DIRECTIVE.search(r or "")]


def _rules_block(skill: dict) -> str:
    rules = _rule_lines(skill)
    if not rules:
        return ""
    return "Standing rules you MUST follow:\n" + "\n".join(f"- {r}" for r in rules)


# Related skills whose trained rules must ALSO sit in front of the drafter for certain work — the worker is a
# dumb waiter: it fetches every relevant shelf, it never cooks. WHICH shelves relate to which task is plumbing
# (this map); WHAT the rules say lives only in the DB. Keyed by the drafting task's own skill_key.
_RELATED_SKILLS = {
    # email-handling holds the company's general email voice (for Sensa: distilled from Gino's sent mail),
    # so EVERY inbound-reply draft reads it; sales-followup governs thread continuations (never pressure).
    "sales-first-response": ("email-handling", "sales-scheduling", "lead-qualification", "sales-followup"),
    # project correspondence (deal in delivery) drafts ON email-handling and also reads the project skills —
    # corrections/rules about HOW Sensa runs projects accumulate there and reach every project reply.
    "email-handling": ("sales-scheduling", "prod-revisions", "prod-status-reporting", "prod-pipeline"),
}


def related_skills(skill: dict, company: dict) -> list[dict]:
    """The related skills (with their live rules) for this task's skill, resolved from the DB."""
    out = []
    for key in _RELATED_SKILLS.get((skill or {}).get("skill_key", ""), ()):
        s = store.get_skill_by_key(company["id"], key)
        if s:
            out.append(s)
    return out


def _related_block(skill: dict, company: dict) -> str:
    parts = []
    for s in related_skills(skill, company):
        rules = _rule_lines(s)
        if rules:
            parts.append(f"Standing rules from the related '{s['name']}' skill — follow them whenever this "
                         "message touches that ground (the company's email voice, proposing or arranging a "
                         "call, judging or handling the lead, following up on an open thread):\n"
                         + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts)


_EMAIL_BODY_RULE = (
    "This is an EMAIL. Write ONLY the email body, the greeting and the message, nothing else. These are hard "
    "rules, never break them:\n"
    "- NO From/To/Subject headers, NO Cc/Bcc line, NO recipient list, and NO routing note, 'sending note', "
    "'system note', or ANY meta/instruction text whatsoever (e.g. never write 'CC ... add via the sending "
    "system' or 'replace this'). Cc/Bcc and recipients are handled entirely by the sending system and must NEVER "
    "be mentioned in the message.\n"
    "- NEVER invent a booking link, scheduling link, calendar link, any URL, or a PLACEHOLDER of ANY "
    "kind — no '[OWNER TO CONFIRM]', '[TBD]', '[amount]', 'XXX' or bracketed blanks, ever. If a fact or "
    "figure the reply needs does not exist yet (a price, a date), the email says the concrete thing WILL "
    "follow (e.g. 'the updated quotation will follow') — it never ships a skeleton to fill in. "
    "A link that appears in your standing rules or in the task itself is real: use it exactly as written "
    "when it is relevant.\n"
    "- NO sign-off, NO your name, NO signature or contact details, the signature and logo are attached "
    "automatically, so adding them doubles them up.\n"
    "- Do NOT mention or instruct anyone to add attachments.\n"
    "Output is exactly the message the recipient reads, ready to send.")


def draft(skill: dict, company: dict, request: dict,
          correction: str | None = None, manager_feedback: list[str] | None = None,
          author: str | None = None, prev_draft: str | None = None) -> str:
    is_email = isinstance(request, dict) and bool(request.get("outbound") or request.get("inquiry"))
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill.",
        _now_line(),
        _company_context(company, author),
        skill.get("craft") or "",
        _rules_block(skill),
        _related_block(skill, company) if is_email else "",
        _EMAIL_BODY_RULE if is_email else
        "Produce the deliverable only — no preamble, no explanation, no meta-commentary.",
    ]))
    atts = request.get("attachments") if isinstance(request, dict) else None
    user = [f"Task: {request.get('brief') if isinstance(request, dict) else request}"]
    if is_email:   # tell the worker WHO it's writing to, so it greets the recipient (not Rashad/itself)
        inq = request.get("inquiry") or {}
        # THEIR EMAIL must always reach the drafter. Some lanes embed it in the brief (_email_brief);
        # any lane that doesn't gets it appended here — a reply drafted blind is never acceptable
        # (bit MAH Gold card #328: 'no message content to reply to', Aug 2026).
        their = (inq.get("message") or inq.get("snippet") or "").strip()
        if their and their[:200] not in (request.get("brief") or ""):
            user.append("THEIR EMAIL (this is the message you are replying to — address exactly what it says; "
                        "quoted earlier messages below it are thread history for context):\n" + their[:6000])
        bits = []
        if inq.get("name") or inq.get("email"):
            bits.append(f"This email is addressed TO {inq.get('name') or inq.get('email')} — greet THEM by "
                        "name and write to them in the second person.")
        if inq.get("subject"):
            bits.append(f"Subject: {inq.get('subject')}.")
        bits.append("It is sent BY the owner of the company in the owner's first-person voice. Do NOT address "
                    "it to Rashad and do NOT write it to yourself — Rashad IS the sender.")
        # Pass the triage/qualification FACTS through to the drafter — how they shape the reply is governed
        # entirely by the (related) skill rules, never by this code.
        tri = request.get("triage") or {}
        if tri.get("category"):
            bits.append(f"Triage category: {tri['category']}.")
        sug = request.get("qual_suggest") or {}
        if sug.get("verdict") or sug.get("bucket"):
            q = f"Lead-qualification suggestion (advisory; the owner decides): verdict={sug.get('verdict') or 'n/a'}"
            if sug.get("bucket"):
                q += f", handling bucket={sug['bucket']}"
            if sug.get("reason"):
                q += f" — {sug['reason']}"
            bits.append(q + ". Apply the lead-qualification standing rules for that bucket.")
        person = sug.get("person") or {}
        if person.get("location"):
            loc = f"Research on the sender (public sources, advisory): based in {person['location']}"
            if person.get("timezone"):
                loc += f" ({person['timezone']})"
            if person.get("role"):
                loc += f", role: {person['role']}"
            bits.append(loc + ". Apply the standing rules on tailoring suggested call times to the lead's "
                        "region/timezone.")
        user.insert(0, " ".join(bits))
    if atts:
        user.append(f"{len(atts)} file(s)/image(s) are attached below — use them as source material for the deliverable.")
    if (skill.get("skill_key") or "") in ("sales-quotation", "sales-first-response", "email-handling"):
        try:   # the rate card rides every quotation-adjacent draft: approved prices only, never invented
            from . import ratecard
            _rc = ratecard.render((company or {}).get("slug") or "")
            if _rc:
                user.append(_rc)
        except Exception:  # noqa: BLE001
            pass
    _hist = (request.get("thread_history") or "").strip() if isinstance(request, dict) else ""
    if _hist:
        user.append("CONVERSATION HISTORY with this contact (newest first — includes emails WE already sent). "
                    "Stay consistent with it: never re-introduce yourself or the company, never repeat or "
                    "contradict something already sent, and NEVER share a meeting link different from one "
                    "already sent in this history.\n" + _hist[:5000])
    _tl = (request.get("deal_timeline") or "").strip() if isinstance(request, dict) else ""
    if _tl:
        user.append("DEAL STATE (real logged timeline — stay consistent with what was promised and where "
                    "the deal stands):\n" + _tl[:3000])
    _of = (request.get("owner_feedback") or "").strip() if isinstance(request, dict) else ""
    if _of:
        user.append("THE OWNER'S PAST CORRECTIONS on this relationship (lessons already taught — obey them "
                    "without being asked again):\n" + _of[:2000])
    _mn = (request.get("meeting_notes") or "").strip() if isinstance(request, dict) else ""
    if _mn:
        user.append("NOTES FROM OUR LAST MEETING with this contact (distilled from the real meeting notes — "
                    "ground your reply in what was actually discussed, decided and committed; never "
                    "contradict it):\n" + _mn[:3000])
    _exm = request.get("existing_meeting") if isinstance(request, dict) else None
    if _exm and _exm.get("meet"):
        user.append(f"A meeting with this contact is ALREADY BOOKED: '{_exm.get('summary')}' at "
                    f"{_exm.get('start')}, Google Meet {_exm['meet']}. Do NOT propose, arrange or imply any "
                    "other meeting; when the draft mentions the call, use THAT exact link and time.")
    _docs = [r.get("filename") for r in (request.get("attach_docs") or []) if r.get("filename")]         if isinstance(request, dict) else []
    if _docs:
        user.append("FILES ATTACHED TO THIS OUTGOING EMAIL (they genuinely send with it): "
                    + ", ".join(_docs) + " — refer to them as attached NOW ('please find attached'); "
                    "NEVER promise to send them later.")
    _meet = ((request.get("meeting") or {}).get("meet") or "").strip() if isinstance(request, dict) else ""
    if _meet:
        user.append(f"A meeting is CONFIRMED and already booked. Its REAL Google Meet link is {_meet} — "
                    "include this exact link where natural in the email. NEVER write any other meeting link.")
    for at in (request.get("attachment_texts") or []) if isinstance(request, dict) else []:
        user.append(f"ATTACHED DOCUMENT '{at.get('filename') or 'document'}' (text extracted from the "
                    f"client's attachment — read it and address its content):\n{(at.get('text') or '')[:15000]}")
    if prev_draft:
        user.append("YOUR PREVIOUS DRAFT (the one under revision — change ONLY what the correction or "
                    "feedback requires, keep every other sentence exactly as it is):\n" + prev_draft[:6000])
    if manager_feedback:
        user.append("Your manager flagged these to fix:\n- " + "\n- ".join(manager_feedback))
    if correction:
        user.append("The owner corrected your previous draft. Apply it LITERALLY and MINIMALLY: when he asks "
                    "to remove something, remove exactly that and nothing around it (removing 'the date' "
                    "takes out the date, not the day of the week); when he asks to change or add something, "
                    "touch only that. Produce the new version:\n" + correction)
    out = provider.think(system, "\n\n".join(user), model=_model_for(skill), think_hard=True,
                         max_tokens=6000, purpose=f"draft:{skill.get('skill_key', '')}",
                         company=company.get("slug"), images=atts)
    return _no_dashes(out) if is_email else out   # house rule: no em/en dashes in visible email copy


def _no_dashes(s: str) -> str:
    """House rule: no em/en dashes in visible copy (keep numeric-range hyphens)."""
    return (s.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", "-"))


def draft_article(skill: dict, company: dict, request: dict,
                  correction: str | None = None, manager_feedback: list[str] | None = None) -> dict:
    """Write a blog article for the company website. Returns {"title", "html"}."""
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill, writing a blog article for the company website.",
        _company_context(company),
        skill.get("craft") or "",
        _rules_block(skill),
        ('Output a JSON object with exactly two fields: "title" (plain text, no markdown) and '
         '"html" (the article body as clean HTML). Rules for the html: use only <h2>, <h3>, <p>, '
         "<ul>/<li>, <ol>/<li>, <strong>, <em>, <a href>, <blockquote>. Do NOT include an <h1> "
         "(the CMS adds the title from the title field). No markdown, no <html>/<head>/<body>, no "
         "inline styles. Do NOT use em-dashes or en-dashes anywhere; use commas, colons or periods. "
         "Lead with the answer, use natural question-style H2 subheadings, keep paragraphs short."),
    ]))
    user = [f"Brief: {request.get('brief') if isinstance(request, dict) else request}"]
    if manager_feedback:
        user.append("Your manager flagged these to fix:\n- " + "\n- ".join(manager_feedback))
    if correction:
        user.append("The owner corrected your previous draft. Apply it LITERALLY and MINIMALLY: when he asks "
                    "to remove something, remove exactly that and nothing around it (removing 'the date' "
                    "takes out the date, not the day of the week); when he asks to change or add something, "
                    "touch only that. Produce the new version:\n" + correction)
    out = provider.think_json(system, "\n\n".join(user), model=_model_for(skill), fast=False,
                              max_tokens=8000, purpose=f"blog:{skill.get('skill_key', '')}",
                              company=company.get("slug"))
    title = _no_dashes((out.get("title") or "").strip()) or "Untitled"
    html = _no_dashes((out.get("html") or "").strip())
    return {"title": title, "html": html}


def infer_rule(skill: dict, correction: str, old_draft: str, new_draft: str) -> dict:
    """Turn a one-off correction into a standing rule, if it implies one."""
    return provider.think_json(
        "You convert an owner's correction into a concise general standing rule for a skill — but ONLY "
        "if the correction implies a durable preference. One-offs with no general lesson are not rules.",
        f"Skill: {skill['name']}\nThe owner's correction: {correction}\n\n"
        'Return JSON: {"is_rule": boolean, "rule": "a short imperative rule (or empty string)"}',
    )
