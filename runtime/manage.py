"""Cortex admin CLI — run from the runtime/ dir.

    python manage.py migrate    # apply db/schema.sql
    python manage.py status     # show tables, row counts, config presence
    python manage.py seed       # seed Tabscanner company + SEO skill (Phase 2)
"""
from __future__ import annotations

import sys

from cortex import db, config


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
    print("config:",
          "DATABASE_URL", present("DATABASE_URL"),
          "| ANTHROPIC", present("ANTHROPIC_API_KEY"),
          "| TELEGRAM", present("TELEGRAM_BOT_TOKEN"),
          "| TELEGRAM_CHAT_ID", present("TELEGRAM_CHAT_ID"))


COMMANDS = {"migrate": migrate, "status": status}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}. options: {', '.join(COMMANDS)}")
        sys.exit(1)
    fn()
