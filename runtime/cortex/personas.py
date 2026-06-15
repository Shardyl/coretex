"""Personas — the Chiefs (strategy/ideation) and Managers (standards/QA) layered over the org chart.

Org model: Workers execute one skill (Sonnet). Managers own the STANDARD for a department — they
define each skill's standing rules (via consultation) and enforce them on drafts. Chiefs own a
CATEGORY and think across its departments AND across companies (strategy/brainstorming). Both heads
are universal (cross-company) and company-aware: with a company selected they focus on it but keep
cross-company knowledge. Chiefs + Managers reason on Opus.

Role split is enforced structurally: a Chief gets READ-ONLY tools (it proposes, it never writes a
rule); a Manager gets the full rule-writing tools (it is the single keeper of the rules). That keeps
rules from bleeding — exactly one owner per rule (the Manager).
"""
from __future__ import annotations

from . import catalog, db, provider, store

CHIEF_TITLES = {
    "Demand": "Demand Chief",
    "Convert": "Convert Chief",
    "Deliver": "Delivery Chief",
    "Run the business": "Operations Chief",
}

VOICE = ("Your replies are read aloud, so speak naturally: no markdown, no bullet lists, no headings, "
         "and keep it brief unless Rashad asks for depth.")


def org() -> dict:
    """The org chart as personas: chiefs (one per category) + managers (one per department)."""
    cats: dict[str, list[tuple[str, str]]] = {}
    for cat, dept, mgr, _ in catalog.CATALOG:
        cats.setdefault(cat, [])
        if (dept, mgr) not in cats[cat]:
            cats[cat].append((dept, mgr))
    chiefs = [{"key": f"chief:{cat}", "title": CHIEF_TITLES.get(cat, f"{cat} Chief"),
               "kind": "chief", "category": cat, "departments": [d for d, _ in depts]}
              for cat, depts in cats.items()]
    managers = [{"key": f"manager:{dept}", "title": mgr, "kind": "manager",
                 "department": dept, "category": cat}
                for cat, depts in cats.items() for dept, mgr in depts]
    return {"chiefs": chiefs, "managers": managers}


def _company(slug: str | None) -> dict | None:
    return store.get_company_by_slug(slug) if slug else None


def _ctx_line(co: dict) -> str:
    ctx = co.get("context") or {}
    bits = [co["name"]]
    if co.get("north_star"):
        bits.append(f"goal: {co['north_star']}")
    for k, label in (("voice", "voice"), ("audience", "audience"), ("products", "offer"), ("donts", "never")):
        if ctx.get(k):
            bits.append(f"{label}: {ctx[k]}")
    return " — ".join(bits)


def _all_companies_block() -> str:
    return "\n".join("• " + _ctx_line(c) for c in db.query("select * from companies order by name"))


def _dept_block(dept: str, co: dict | None, full: bool) -> str:
    """A department's skills with their standing rules (for the selected company, else a reference one)."""
    ref = co or store.get_company_by_slug(catalog.COMPANIES[0][0])
    rows = db.query("select skill_key,name,craft,rules from skills where department=%s and company_id=%s "
                    "order by name", (dept, ref["id"]))
    out = []
    for s in rows:
        uni = store.get_universal_rules(s["skill_key"]) or []
        loc = (s["rules"] or []) if co else []
        line = f"• {s['name']}"
        if full and s.get("craft"):
            line += f"\n    craft: {s['craft'][:300]}"
        allrules = list(uni) + list(loc)
        if allrules:
            line += "\n    rules: " + " | ".join(allrules[:8])
        out.append(line)
    return "\n".join(out) or "(no skills)"


def persona_system(persona_key: str, company_slug: str | None = None) -> tuple[str | None, str, bool]:
    """Build (system_prompt, model, is_chief) for a head. Returns (None, MODEL, False) if unknown."""
    co = _company(company_slug)
    scope = f"for {co['name']}" if co else "across all four companies (Tabscanner, Sensa, SkyVision, FilmSpoke)"
    co_block = _ctx_line(co) if co else _all_companies_block()

    if persona_key.startswith("chief:"):
        cat = persona_key.split(":", 1)[1]
        chief = next((c for c in org()["chiefs"] if c["category"] == cat), None)
        if not chief:
            return None, provider.MODEL, False
        depts = chief["departments"]
        summaries = "\n\n".join(f"{d}:\n{_dept_block(d, co, full=False)}" for d in depts)
        system = (
            f"You are the {chief['title']} at Cortex, Rashad's voice-first AI operations partner. You own "
            f"the '{cat}' category {scope}. Departments under you: {', '.join(depts)}. "
            "Your job is STRATEGY and IDEATION: brainstorm, spot opportunities, and weigh trade-offs ACROSS "
            "these departments and across companies — help Rashad decide where to put his limited time and "
            "money. Be sharp, challenge weak assumptions, cross-pollinate what works on one company onto "
            "another, and propose concrete plays, not platitudes. "
            "You do NOT write final deliverables and you do NOT edit drafts — that is the workers and the "
            "department managers. You CAN create a brand-new skill when the org is missing a capability "
            "(use create_skill — it is added to every company automatically and filed under the right "
            "department); growing the skill set is part of your job. But you do NOT write the per-company "
            "standing rules: when a brainstorm lands on a durable rule, name it clearly and say which "
            "skill it belongs to and whether it should be universal or one company, so a manager records "
            "it. You grow the org and propose the rules; the manager is the keeper of the rules. " + VOICE +
            "\n\nBusiness context:\n" + co_block +
            "\n\nWhat your departments do today (skills + their standing rules):\n" + summaries
        )
        return system, provider.MODEL, True

    if persona_key.startswith("manager:"):
        dept = persona_key.split(":", 1)[1]
        _, mgr = catalog.dept_meta(dept)
        mgr = mgr or f"{dept} manager"
        block = _dept_block(dept, co, full=True)
        system = (
            f"You are the {mgr} at Cortex, Rashad's voice-first AI operations partner. You own the "
            f"'{dept}' department {scope}. You are the KEEPER OF THE STANDARD: you define how each skill in "
            "your department should be done (its standing rules), and you make sure the workers' drafts "
            "follow those rules and the company's brand. When Rashad tells you how something should be "
            "handled, or a Chief proposes a rule, turn it into concise standing rules and SAVE them with "
            "your tools. RULES HAVE A SCOPE: always confirm 'universal for all companies, or just "
            "<company>?' before saving — never let a company-specific rule spread. Read a skill's current "
            "rules before changing them. " + VOICE +
            "\n\nBusiness context:\n" + co_block +
            "\n\nYour department's skills (craft + standing rules):\n" + block
        )
        return system, provider.MODEL, False

    return None, provider.MODEL, False


def label(key: str | None) -> str:
    """Human label for a persona key ('' = general Cortex)."""
    if not key:
        return "Cortex"
    o = org()
    for h in o["chiefs"] + o["managers"]:
        if h["key"] == key:
            return h["title"]
    return "Cortex"


def _valid_keys() -> set[str]:
    o = org()
    return {h["key"] for h in o["chiefs"] + o["managers"]}


def route(messages: list[dict], company_slug: str | None, current: str | None) -> str:
    """Sticky-but-adaptive routing: pick who handles the latest turn. Default to `current` unless the
    subject clearly moves to another head's domain. Returns a persona key, or '' for general Cortex.
    Runs on the cheap router model (Haiku)."""
    if not messages:
        return current or ""
    o = org()
    opts = ["(empty string) — general Cortex assistant: chit-chat, greetings, anything not clearly "
            "strategy or one department's standard"]
    opts += [f"{c['key']} — {c['title']}: STRATEGY/ideas across {', '.join(c['departments'])}" for c in o["chiefs"]]
    opts += [f"{m['key']} — {m['title']}: STANDARDS/rules/QA for {m['department']}" for m in o["managers"]]
    convo = "\n".join(f"{m.get('role')}: {str(m.get('content', ''))[:220]}"
                      for m in messages[-5:] if m.get("content"))
    system = (
        "You route a conversation to the right head in Cortex. Heads: a general assistant, 4 Chiefs "
        "(cross-department STRATEGY and ideation), and 9 Managers (a single department's STANDARDS, rules "
        "and draft QA). BE STICKY: if a head is already handling the chat and the latest message stays on "
        "that thread, KEEP them — do not switch on a tangent. Switch only when the subject clearly moves "
        "into another head's domain. Use general Cortex for small talk or anything not clearly strategy or "
        "a specific department.")
    user = (f"Currently handled by: {current or '(general Cortex)'}\n\n"
            f"Recent conversation:\n{convo}\n\n"
            "Choose one (return its key, or an empty string for general):\n" + "\n".join(opts) +
            '\n\nReturn JSON: {"persona":"<key or empty string>"}')
    out = provider.think_json(system, user, model=provider.MODEL_ROUTER, max_tokens=120)
    key = (out.get("persona") or "").strip()
    return key if key in _valid_keys() else ""


def name_chat(messages: list[dict]) -> str:
    """A short, specific subject label (2-5 words, Title Case) for a Talk conversation."""
    convo = "\n".join(f"{m.get('role')}: {str(m.get('content', ''))[:220]}"
                      for m in messages[-6:] if m.get("content"))
    if not convo.strip():
        return "New chat"
    out = provider.think_json(
        "Name this chat with a short, specific subject label of 2 to 5 words in Title Case. Capture the "
        "topic, not the speaker. No surrounding quotes, no trailing punctuation.",
        f"Conversation:\n{convo}\n\nReturn JSON: {{\"title\":\"...\"}}",
        model=provider.MODEL_ROUTER, max_tokens=40)
    t = (out.get("title") or "").strip().strip('"').strip()
    return t[:48] or "New chat"
