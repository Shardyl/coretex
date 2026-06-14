# Cortex build log

## Phase 1 — core engine ✅ (2026-06-14, verified live)
Provider adapter (`claude-opus-4-8` reasoning / `claude-sonnet-4-6` JSON, adaptive thinking) →
worker → manager → Telegram approval rail → decision log + trust streak. Running as the systemd
service `cortex-engine`. DB: companies·skills·tasks·decisions·settings. Admin CLI `manage.py`
(migrate·status·seed·task·blog·engine). Proven end-to-end: seed Tabscanner + `content-seo`,
fire a task, worker drafts in voice, manager judges, approve/correct/skip on Telegram, streak +
auto-offer at threshold. Money kinds never auto.

## Phase 2 — WordPress publisher ✅ (2026-06-15, verified on production)
`integrations/wordpress.py` + `worker.draft_article()` + engine blog-path. A `kind='blog'` task
writes a real article, stages it as a **hidden draft** on tabscanner.com via WP REST, sends it to
Telegram. **Approve = publish live · Discard = trash draft · Correct = redraft+update+learn-rule.**
Verified: task #2 → draft post 472 (`status=draft`) on tabscanner.com + Telegram msg 12.

**Design constraint (probed live):** Rank Math `rank_math_robots` is NOT writable over WP REST, so
there is no reliable "live-but-noindex" state over REST. Cortex therefore gates on **post status**:
`draft` = hidden + non-indexable; `publish` = live + indexable, only on the owner's per-post tap.
Blog tasks are **never auto** regardless of trust streak (enforces the web-page-builder golden rule:
no page is indexed without explicit per-page approval). A true live-noindex preview would need a
small Rank Math meta-registration mu-plugin on Tabscanner — future enhancement, not required.

## Deploy (this box)
`/opt/coretex` is owned by the `cortex` user; the GitHub deploy key is in `/home/cortex/.ssh/`.
Pull **as cortex**: `sudo -u cortex git -C /opt/coretex pull --ff-only`, then
`systemctl restart cortex-engine`. (Root pull → dubious-ownership / GitHub auth failure.)
Secrets in `/etc/cortex/cortex.env` — do NOT `source` it in bash (a value with spaces breaks the
parser); grep the specific key. Tabscanner WP creds: `TABSCANNER_APP_PASSWORD` (note: not
`_WP_APP_PASSWORD`), `TABSCANNER_WP_URL`, `TABSCANNER_WP_USER`. Curl-style UA required past
Tabscanner's Cloudflare.

## Phase 3 — next
PWA cockpit (Cloudflare Pages, coretex.uk) + voice (Deepgram in / ElevenLabs Flash out, 3 modes) +
omnichannel doors, backed by a FastAPI service over the existing engine/DB on the box.
