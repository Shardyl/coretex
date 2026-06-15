"""The worker — does the task per its skill (produces the deliverable)."""
from __future__ import annotations

from . import grounding, provider, store


def _company_context(company: dict) -> str:
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
    return base + ("\n\n" + ground if ground else "")


def _model_for(skill: dict) -> str:
    """Workers run on Sonnet by default; a skill tiered model='opus' overrides for high-quality work."""
    return provider.resolve_model(skill.get("model")) or provider.MODEL_FAST


def _rules_block(skill: dict) -> str:
    universal, local = store.effective_rules(skill)  # universal minus this company's overrides, then local
    rules = list(universal) + list(local)
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
    return provider.think(system, "\n\n".join(user), model=_model_for(skill), think_hard=True, max_tokens=6000)


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
    out = provider.think_json(system, "\n\n".join(user), model=_model_for(skill), fast=False, max_tokens=8000)
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
