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
