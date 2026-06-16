# Cortex — Session Handover / Context Primer

Read this first if you're a Claude Code session that doesn't yet know about Cortex. It's the current
state of the system and where everything lives. (Generated 2026-06-16.)

## What Cortex is
A voice-first AI operations platform that runs Rashad's businesses (Tabscanner, Sensa, SkyVision, FilmSpoke).
- **Repo:** `Shardyl/coretex` (canonical docs live in-repo).
- **Box:** Hetzner `cortex-1`, SSH alias **`cortex`** (`ssh cortex`). Postgres + Python.
- **Live URL:** https://coretex.uk (Cloudflare tunnel → cortex-api).
- **Runtime:** `/opt/coretex/runtime`, imported as the package **`cortex`** (WorkingDirectory `/opt/coretex/runtime`,
  venv `/opt/coretex/.venv`). Services: **cortex-api** (uvicorn `cortex.api:app` on 127.0.0.1:8787, also serves the
  cockpit at `/`) + **cortex-engine** (`runtime/main.py` — worker/manager/Telegram/scheduler loop).
- **Nightly backup:** cron at 03:00 pg_dumps the DB + repo/docs + a mirror of Claude's memory → Google Drive.

## Skills — THIS IS THE IMPORTANT BIT
**Skills now live in the Cortex Postgres `skills` table, NOT in `~/.claude/skills/*.md`.**
- **83 distinct skills × 4 companies = 332 rows.** The roster is **uniform**: every skill exists identically for
  every company (filmspoke / sensa / skyvision / tabscanner), across 10 departments. Adding a skill adds it to all
  companies. Only the *rules* inside a skill differ (scope = **universal** = all companies, or **company** = one).
- **ALWAYS query the LIVE DB** for skills / rules / counts. Never trust `catalog.py` (seed only) or a cached number;
  the live list is edited directly and moves. Example:
  `ssh cortex "cd /opt/coretex/runtime && /opt/coretex/.venv/bin/python -c \"from cortex import db; print(db.query('select skill_key,name,department from skills s join companies c on c.id=s.company_id where c.slug=%s', ('sensa',)))\""`
- The old `~/.claude/skills/*.md` docs (seo-campaign, web-page-builder, website-management, content-backtrack-audit,
  sensa-quotation) are **RETIRED as Cortex reference** — their craft was distilled into Cortex skills. Change rules
  IN CORTEX, not the `.md` files. (Those `.md` skills still function as Claude Code *build* skills for website /
  quotation work — that's a separate use; just don't treat them as Cortex's source of truth.)
- Each of the 83 skills has an expert questionnaire (what it handles + a question set); training happens via Talk
  (Skills tab → a question → "Talk it through" → Lock-in a rule / Park an idea).

## Credentials / API keys — NEVER in chat, repo, or memory
Keys live **only** on the box and in the vault. To use one, read it on the box at runtime; never copy a secret into
a chat or commit it.
- **`/etc/cortex/`** on the box holds: `cortex.env` (the main secrets env — Anthropic, Telegram, Deepgram,
  ElevenLabs, DB, etc.), `google-ads.yaml` (Google OAuth refresh token for GA4/GSC/Ads), `ga4-measurement-ids.json`,
  `google_oauth_client.json`.
- **DB `settings` table** holds OAuth refresh tokens: `gmail_refresh_token` (reads api@tabscanner.com),
  `gmail_send_refresh_token` (sends as rashad@tabscanner.com), `google_refresh_token` (Drive, rashad@sensa.digital).
- Other project keys live in their own vaults (e.g. AdDrop: Gemini in addrop-core, Atlas `atlascloud_api_key`).

## Deploying changes
**The box CANNOT `git pull`** (no deploy key / agent forward → "Host key verification failed"). Deploy by copying
files directly, then restarting:
```
scp <files> cortex:/opt/coretex/runtime/cortex/      # backend
scp web/index.html cortex:/opt/coretex/web/           # cockpit (single-file PWA)
ssh cortex "cd /opt/coretex/runtime && /opt/coretex/.venv/bin/python -m py_compile cortex/<f>.py && sudo systemctl restart cortex-api cortex-engine"
```
The cockpit is a single-file PWA with a network-first service worker — a normal page **reload** picks up new code.

## Cross-session memory (the real handover mechanism)
Claude's persistent memory at `C:\Users\rasha\.claude\projects\C--Users-rasha\memory\` is the source of truth across
sessions. `MEMORY.md` is the index (loaded at the start of every NEW session). Already-open chats hold a stale
snapshot from when they started — point them here (or restart them) to catch up. Key files:
`project_cortex.md`, `project_cortex_cockpit.md`, `project_cortex_crm.md`, `project_cortex_questionnaire.md`,
`project_cortex_org_architecture.md`, `feedback_cortex_*`, `feedback_skills_uniform_roster.md`,
`feedback_cortex_live_list_is_truth.md`.
