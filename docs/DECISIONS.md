# Cortex — Decisions Log

Append-only, newest at bottom. The "why" matters as much as the call.

| Date | Decision | Why |
|---|---|---|
| 2026-06-14 | Product named **Cortex**; build lives in the `coretex` GitHub repo (Shardyl/coretex). | Single home for the build. Repo-slug spelling flagged to Rashad. |
| 2026-06-14 | Design **locked**: Hetzner + Claude Agent SDK behind a provider adapter, git-backed skills with trust streaks, 9 departments / 4 categories, three doors (Chat/Calendar/Incoming), money & sends owner-only. | End of the design/mockup phase; clickable dark mockup is the destination. |
| 2026-06-14 | **Own database, held on Hetzner** (Postgres). Drop Bitrix; seed from the export already pulled (35,438 leads + 157 warm). | Rashad wants the data under his control on his own box, not behind Bitrix (paid only till April). |
| 2026-06-14 | **Build our own accounts module** (basic FreshBooks): issue invoices, track client accounts, statements, monies outstanding, spreadsheet export. Owner-gated. | Doesn't want to keep books in two places; wants just enough to run accounts. |
| 2026-06-14 | **Voice IN = Deepgram (Nova-3) from day one** (chosen over free Web Speech after pricing — ~$0.46/hr, negligible); **Voice OUT = ElevenLabs Flash v2.5** (~75ms, owned). Behind a provider adapter so swapping stays config-only. | Fluid speech is the top priority and Deepgram's cost is trivial; Flash solves the TTS-latency worry. Supersedes the earlier "test Web Speech first" call. |
| 2026-06-14 | **Telegram bot for approvals in Phase 0/1**, before the PWA. PWA replaces it as the cockpit later (Telegram may stay as a notification fallback). | Free, instant, phone-native approval rail with zero app to build; gets the human-in-the-loop trust loop running immediately. |
| 2026-06-14 | **First slice = Tabscanner SEO blog loop.** Build the DB on Hetzner + the slice; hold the PWA until the slice proves out. | Exercises the full create → judge → approve → publish spine that every department reuses; real artifact on real infra. |
| 2026-06-14 | Tabscanner blog **publishes via the WordPress REST API** (it's WordPress + Rank Math), not via a repo push. The Tabscanner repo is synced to Hetzner as worker *context* + design/template code only. | Posts are CMS content, not repo files; REST is the proven AddDrop/Sensa pattern. Repo gives the worker brand voice + structure + the SEO strategy pack. |
| 2026-06-14 | **Dedicated Anthropic API key + hard spend cap** for Cortex from day one; per-department cost meter. | Cortex bills metered API (separate from Max); the 15 Jun billing split + a known $1,800 `claude -p` surprise make a spend ceiling non-negotiable. |
| 2026-06-14 | **Runtime language = Python.** | Matches AddDrop (portable ops/deploy patterns, systemd discipline); fully supported by the Agent SDK; Cortex is mostly orchestration + integrations, so no decisive TypeScript advantage. Resolves the open language question. |
| 2026-06-14 | **Server = Hetzner `cortex-1`** — CPX31 (4 vCPU / 8 GB / 160 GB), Ubuntu 26.04, Ashburn VA. | Comfortable floor for Postgres + agent runtime + API co-located; resizable live; matches AddDrop's region + x86 tooling. Separate box from AddDrop (isolation). |
| 2026-06-14 | **Web app hosting:** PWA frontend on **Cloudflare Pages**; backend API + Postgres + agent runtime on **Hetzner**; Cloudflare proxied in front of the API subdomain. | Edge CDN for fast phone loads; the backend must sit with the DB + always-on agent loop, which Pages can't host. Unlike AddDrop, there's no WordPress backend. |
| 2026-06-14 | **Access domain = `coretex.uk`** (Cloudflare Registrar). | Only single-word `coretex` available; matches the repo; it's just an internal access URL, not a public brand. Product stays "Cortex" in the UI. |

## Open decisions (before Phase 0 hardens)
- **Runtime architecture:** Hetzner + Agent SDK (current plan) vs **Managed Agents** vs
  **hybrid** (Hetzner orchestrates, heavy workers run as Managed Agents). Leaning hybrid.
