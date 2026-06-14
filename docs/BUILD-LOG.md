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
writes a real article and stages a **private password-protected preview** on tabscanner.com
(status=publish + generated password): a real, fully-themed URL the owner opens with the password
to judge the **finished design**, while logged-out visitors get only a password box and the post is
excluded from the Rank Math sitemap. Telegram card = link + password + content.
**Approve (Publish live) = clear the password → public+indexed · Discard = trash · Correct =
redraft + update (password preserved) + learn-rule.** Verified: task #3 → post 475 (password form
rendered, 0 article H2s exposed to anon, not in sitemap).

**⚠ PARKED 2026-06-15 — operator rejected password-on-published.** "Publishing something live with
a password on it is not the right solution." Requirement stands (he must see the *rendered design*,
not text), but the delivery must change. **Agreed direction (revisit before Phase 2 is 'done'):**
stage as `status=draft` and hand him the WordPress **logged-in draft preview** of the themed page
("even if I have to log in to WordPress to see the draft, that's fine") — not a public/password URL.
Engine still runs the password path until rebuilt. (Earlier why-password note retained below for
context.)
**Why password was tried, not status=draft:** the operator needs the *rendered design* at a link,
not text — a plain draft needs WP login and didn't obviously show the themed page; password-publish
did. The fix is to use WP's native logged-in draft preview, which does render the theme. **Constraint (probed live):** Rank Math `rank_math_robots` is NOT writable over WP
REST (silently dropped), so there's no clean "live-but-noindex" over REST; password + sitemap
exclusion + unlinked is the hide. Residual: the WP excerpt/meta-description summary still appears in
page source during preview (invisible to a viewer, not indexed) — suppress later with a deliberate
meta field. Blog tasks are **never auto** regardless of trust (enforces the golden rule).

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
