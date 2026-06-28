"""The worker — does the task per its skill (produces the deliverable)."""
from __future__ import annotations

import re

from . import grounding, profile, provider, store


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


def _rules_block(skill: dict) -> str:
    universal, local = store.effective_rules(skill)  # universal minus this company's overrides, then local
    rules = [r for r in (list(universal) + list(local)) if not _CC_DIRECTIVE.search(r or "")]
    if not rules:
        return ""
    return "Standing rules you MUST follow:\n" + "\n".join(f"- {r}" for r in rules)


_EMAIL_BODY_RULE = (
    "This is an EMAIL. Write ONLY the email body — the greeting and the message. Do NOT write From/To/Subject "
    "headers, and do NOT write any Cc/Bcc line, recipient list, routing note, 'sending note' or 'system note' in "
    "the body — Cc/Bcc and recipients are actioned by the sending system, never written into the message. Do NOT "
    "add a sign-off (no 'Best regards', no your name) and do NOT add a signature or contact "
    "details — the recipient, signature and logo are attached automatically, so adding them yourself doubles "
    "them up. Do NOT mention, describe or instruct anyone to add attachments — any attached files are shown to "
    "the owner separately. Output is exactly the message a human reads, nothing else, ready to send.")


def draft(skill: dict, company: dict, request: dict,
          correction: str | None = None, manager_feedback: list[str] | None = None,
          author: str | None = None) -> str:
    is_email = isinstance(request, dict) and bool(request.get("outbound") or request.get("inquiry"))
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill.",
        _company_context(company, author),
        skill.get("craft") or "",
        _rules_block(skill),
        _EMAIL_BODY_RULE if is_email else
        "Produce the deliverable only — no preamble, no explanation, no meta-commentary.",
    ]))
    atts = request.get("attachments") if isinstance(request, dict) else None
    user = [f"Task: {request.get('brief') if isinstance(request, dict) else request}"]
    if is_email:   # tell the worker WHO it's writing to, so it greets the recipient (not Rashad/itself)
        inq = request.get("inquiry") or {}
        bits = []
        if inq.get("name") or inq.get("email"):
            bits.append(f"This email is addressed TO {inq.get('name') or inq.get('email')} — greet THEM by "
                        "name and write to them in the second person.")
        if inq.get("subject"):
            bits.append(f"Subject: {inq.get('subject')}.")
        bits.append("It is sent BY the owner of the company in the owner's first-person voice. Do NOT address "
                    "it to Rashad and do NOT write it to yourself — Rashad IS the sender.")
        user.insert(0, " ".join(bits))
    if atts:
        user.append(f"{len(atts)} file(s)/image(s) are attached below — use them as source material for the deliverable.")
    if manager_feedback:
        user.append("Your manager flagged these to fix:\n- " + "\n- ".join(manager_feedback))
    if correction:
        user.append(f"The owner corrected your previous draft. Apply this and produce a new version:\n{correction}")
    return provider.think(system, "\n\n".join(user), model=_model_for(skill), think_hard=True,
                          max_tokens=6000, purpose=f"draft:{skill.get('skill_key', '')}",
                          company=company.get("slug"), images=atts)


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
        user.append(f"The owner corrected your previous draft. Apply this and produce a new version:\n{correction}")
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
