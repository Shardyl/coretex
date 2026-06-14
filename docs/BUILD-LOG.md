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

## Phase 3 — in progress
**Backend ✅ (2026-06-15, verified):** `cortex/api.py` (FastAPI) running as the `cortex-api` systemd
service on `127.0.0.1:8787`, reusing engine/store/db. Single-passcode auth (`CORTEX_PASSCODE` in env)
→ signed expiring bearer token (HMAC, secret auto-stored in settings). Endpoints: `login·me·health`,
reads `companies·{slug}/skills·tasks·inbox·tasks/{id}·decisions`, actions `POST tasks` +
`tasks/{id}/{approve|skip|correct}` (reuse `engine.approve_task/skip_task/correct_task`; correction
core extracted to `engine.apply_correction` so Telegram and the cockpit share it). Verified: wrong
passcode + no-token → 401; companies/skills/inbox/decisions return real data; CORS locked to
`*.coretex.uk` + localhost.

**Cockpit ✅ built + render-verified (2026-06-15):** `web/index.html` — single-file PWA, Cyan theme
(dark+light toggle), mobile-first, no horizontal scroll. Views: **Inbox** (approval cards →
Publish-live/Approve · Correct · Discard/Skip), **Ask** (company+skill+type+brief → `POST /api/tasks`),
**Activity** (decision log with status LEDs). Talks to the API via bearer token; graceful **demo
fallback** (passcode `demo`) renders sample data offline. Deployable as-is to Cloudflare Pages.

**LIVE ✅ (2026-06-15): https://coretex.uk** — the API serves the cockpit at `/` and the API at
`/api`, exposed through a **Cloudflare Tunnel** (no open ports; box dials out). Built entirely via
the Cloudflare API: tunnel `cortex` (`cf05b0b3-…`, remotely-managed, ingress coretex.uk→localhost:8787),
proxied CNAME apex → `{tid}.cfargotunnel.com`, `cloudflared` running as a systemd service with the
connector token. Verified: `https://coretex.uk/api/health` → 200, `/` serves the cockpit. Login uses
`CORTEX_PASSCODE` (config is `@lru_cache`d → restart `cortex-api` after changing it).

**Installable PWA ✅ (2026-06-15):** `web/manifest.webmanifest` + `web/sw.js` (network-first, API never
cached) + Cortex icons (180/192/512) + apple-touch meta. `display: standalone`; installs to the home
screen as a real app.

**Voice ✅ core (2026-06-15, round-trip verified):** backend `POST /api/voice/stt` (Deepgram Nova-3,
audio upload → transcript) + `POST /api/voice/tts` (ElevenLabs Flash v2.5, text → mp3; voice
`ELEVENLABS_VOICE_ID` env, default `21m00Tcm4TlvDq8ikWAM`). Cockpit: **Speak** button in Ask
(MediaRecorder → /stt → fills the brief), **Read aloud** on Inbox cards (→ /tts → plays), header mic
opens Ask + listens. Verified TTS→STT round-trip on the box ("Cortex voice is working…").

**Remaining for Phase 3:** (1) hands-free **Free mode** (continuous listen + barge-in) and **Talk**
gym-mode on top of the push-to-talk core; (2) omnichannel doors.
