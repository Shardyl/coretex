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

**Voice — live streaming ✅ (2026-06-15, verified through edge):** `WS /api/voice/stream` proxies the
browser mic to Deepgram's streaming API (linear16 PCM, interim+final) and relays transcripts back;
keeps the Deepgram key server-side. Cockpit streams mic PCM via ScriptProcessor over the WS and shows
words live in the Ask brief as you speak (interim replaced, finals appended). Start/stop beeps +
"getting ready" warm-up retained. Verified: PCM round-trip on localhost AND through `wss://coretex.uk`
(WebSockets pass the Cloudflare tunnel). Deps: `websockets`; box has `ffmpeg` (for test PCM only).

**Talk (chat) + Free mode ✅ (2026-06-15, verified):** `POST /api/chat` (provider.chat = opus, no
extended thinking for snappiness; CHAT_SYSTEM = Cortex ops-partner persona, voice-friendly: no
markdown, brief). Cockpit **Talk** tab = message bubbles + text input; **Free mode** (🎧) = hands-free
loop: continuous streaming STT, Deepgram `endpointing` → `speech_final` segments each utterance →
/api/chat → ElevenLabs reply spoken (`speakAndWait`) → "go" beep cues your turn. Echo-guarded (sends
silence to Deepgram while Cortex speaks; `freeThinking`/`botSpeaking` serialize turns; AEC on).
Chat-bar positioned off the live tab-bar height. Verified: chat replies + context retention through
`https://coretex.uk`. NOTE: voice barge-in (cut in mid-reply) deferred — v1 is turn-based.

**Voice UX fixes (2026-06-15):** (a) Free-mode cutoff fixed — turn now ends on a **client-side 1.5s
silence timer** (`TURN_SILENCE`, resets on every new word), NOT Deepgram endpointing/speech_final/
UtteranceEnd (all proved twitchy/unreliable on nova-3, esp. with synthetic audio; speech_final at
300ms was cutting mid-sentence). (b) **Live transcription in Free mode** — words show in a dimmed
`.bub.live` bubble as you talk. (c) **Push-to-talk added to Talk** — `dictate(btn,targetId)` now
generic; the 🎙️ button on the Talk bar dictates into the chat box exactly like Ask (tap → live words →
tap stop → review → send). Talk has both: 🎙️ normal + 🎧 hands-free.

**Voice barge-in + feedback ✅ (2026-06-15):** Free mode now sends real mic audio during TTS (AEC
cancels Cortex's own voice) and treats a >=3-word transcript while `botSpeaking` as **barge-in** →
`interruptSpeech()` pauses the audio (speakAndWait resolves on `onpause`) and listens. Feedback now
mirrors Ask: warming-up state → rising "go" beep when listening → **falling handover beep** the moment
your turn ends (you paused) → spoken reply ("talk to cut in") → rising "go" beep = your turn. Hint line
shows warming up / Listening / thinking / speaking. (Barge-in is AEC-dependent; word threshold tunable
if it self-interrupts on speakerphone.)

**Skill-aware chat ✅ (2026-06-15):** `provider.chat_tools` (Claude tool-use loop) + `/api/chat` now
gives Cortex tools `list_skills · add_rule · create_skill · update_craft`; system prompt injects a live
snapshot of existing skills. So Cortex can **view and tune skills + rules by voice/chat** ("add a rule
to the Tabscanner inquiry skill: …" → it actually adds it). Verified live: listed skills, added a rule,
confirmed in DB. Seeded a **Tabscanner `sales-inquiries`** skill (assess-the-lead craft) for the Seb
inquiry case. **Voice = Alice (British)** `Xb7hH8MSUJpSbSDYk0k2`, speed 1.14, stability 0.4 (env
`ELEVENLABS_VOICE_ID`/`ELEVENLABS_SPEED`/`ELEVENLABS_STABILITY`); samples at `coretex.uk/voices/*.mp3`.

**Granular skill catalog seeded ✅ (2026-06-15, operator-locked):** every company gets the FULL granular
catalog from the `Cortex-Skills-Roadmap` doc — 4 categories / 9 departments / ~78 skills, created up
front and empty (authority=ask, no rules) so Cortex + Rashad map to the same sheets. Source of truth
`runtime/cortex/catalog.py`; `manage.py catalog` seeds; skills carry `category`/`department`/`manager`
(new columns, `db._ALTERS`). Seeded live: 4 companies × 78 = 312 rows. Old broad `content-seo`/
`sales-inquiries` folded into `content-blog-posts`/`sales-first-response` (rules carried, tasks/decisions
repointed). Chat `_chat_system` gives a compact overview; `list_skills(company, department)` drills in.
Verified: Cortex correctly lists a department's skills + which have rules.

**Chat-drafts-tasks ✅ (2026-06-15, verified):** chat now has EYES + HANDS via tool use — `list_tasks`,
`get_task`, `draft` (runs `worker.draft` with the skill's craft + rules + company voice, returns text),
`create_task`, and on a pending task `approve_task`/`skip_task`/`correct_task` (reuse engine wrappers).
System prompt tells it to find an item before acting and to fix-and-teach (re-draft + add_rule). Verified
live: "draft a reply to Seb" → Cortex qualified before pricing and **held pricing back "per our rule"**
(the standing rule applied through the worker); "anything in my inbox?" → it called list_tasks and
answered correctly. This is the "refer to it, give feedback, it fixes + learns, no Inbox needed" loop.

**Visual Skills screen ✅ (2026-06-15):** 5th cockpit tab 🧠 Skills — `renderSkills` fetches
`/api/companies/{slug}/skills`, groups by category → department (collapsible), each skill expands to
craft + standing rules (delete ✕) + an add-rule box. New endpoints `POST /api/skills/{id}/rule` and
`/rule/delete`. Render-verified at mobile (5 tabs fit, no overflow).

**Remaining for Phase 3:** "discuss this" deep-link from an item into Talk; Talk gym-mode; the other
cockpit screens (Incoming, Calendar, Contacts, Projects, Team, Invoices, Reports, Settings); omnichannel doors.

**Remaining for Phase 3:** (1) chat that can **trigger tasks/draft content** (currently manages skills +
converses; drafting still routes to Ask); (2) a visual **Skills screen** + remaining cockpit screens
(Departments, Incoming, Calendar, Contacts, Projects, Team, Invoices, Reports, Settings); (3) Talk
gym-mode; (4) omnichannel doors. Reference PDF on Rashad's Desktop: "Cortex - Build Phases & Specs.pdf".
