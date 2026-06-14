"""Cortex admin CLI — run from the runtime/ dir.

    python manage.py migrate
    python manage.py status
    python manage.py seed
    python manage.py task [skill_key] ["brief"]
    python manage.py engine          # run the engine loop (foreground)
"""
from __future__ import annotations

import sys

from cortex import config, db, store


def migrate() -> None:
    db.migrate()
    print("migrated")


def status() -> None:
    tables = [r["table_name"] for r in db.query(
        "select table_name from information_schema.tables "
        "where table_schema='public' order by table_name")]
    print("tables:", ", ".join(tables) or "(none)")
    for t in ("companies", "skills", "tasks", "decisions", "settings"):
        try:
            n = db.one(f"select count(*) as n from {t}")["n"]
            print(f"  {t}: {n} rows")
        except Exception as e:  # noqa: BLE001
            print(f"  {t}: ERROR {e}")
    present = lambda k: "set" if config.get(k) else "MISSING"
    print("config:", "DATABASE_URL", present("DATABASE_URL"), "| ANTHROPIC", present("ANTHROPIC_API_KEY"),
          "| TELEGRAM", present("TELEGRAM_BOT_TOKEN"), "| CHAT_ID", present("TELEGRAM_CHAT_ID"))


def seed() -> None:
    co = store.upsert_company(
        "tabscanner", "Tabscanner", "owned",
        context={
            "voice": "Technical, credible, accuracy-first, B2B. Concrete and specific, never hypey.",
            "audience": "Developers, product teams, and fintech / expense / loyalty companies evaluating receipt OCR.",
            "products": "Receipt-OCR / expense-data-extraction (EDE) API with high accuracy across global formats.",
            "donts": "No unqualified financial/tax (YMYL) advice. No vague claims without proof.",
        },
        north_star="Enterprise / sales-qualified leads",
    )
    skill = store.upsert_skill(
        co["id"], "content-seo", "Content & SEO",
        craft=("Write clear, genuinely useful content for a technical B2B audience. Lead with the answer, "
               "back claims with specifics, and keep Tabscanner's accuracy-first, no-hype voice. Structure "
               "for skim-reading with short paragraphs and useful subheadings."),
        authority="ask", stakes="low", auto_threshold=10,
    )
    print(f"seeded company #{co['id']} (tabscanner) + skill #{skill['id']} (content-seo)")


def task() -> None:
    key = sys.argv[2] if len(sys.argv) > 2 else "content-seo"
    brief = (sys.argv[3] if len(sys.argv) > 3 else
             "Write a 120-word LinkedIn post introducing Tabscanner's receipt OCR API to fintech developers.")
    co = store.get_company_by_slug("tabscanner")
    skill = store.get_skill_by_key(co["id"], key)
    if not skill:
        print(f"no skill '{key}' — run seed first"); return
    t = store.create_task(co["id"], skill["id"], "content", {"brief": brief})
    print(f"created task #{t['id']} for {key}: {brief}")


def engine() -> None:
    from cortex import engine as eng
    eng.run()


COMMANDS = {"migrate": migrate, "status": status, "seed": seed, "task": task, "engine": engine}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}. options: {', '.join(COMMANDS)}")
        sys.exit(1)
    fn()
