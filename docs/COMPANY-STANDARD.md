# Company Standard — new-company onboarding SOP

**Purpose.** The single source of truth for *what must be true for every company in Cortex*. When a new
company is added, **all of this happens by default** — nothing is left to memory or done ad-hoc. If a step
isn't yet automated, it is listed here as REQUIRED with its owner so it is never skipped. Keep this doc in
lockstep with `cortex/onboard.py` (the routine that applies the standard).

> Governing principle for assets/media: **Drive = source of truth, Cortex = producer, R2 = Cortex's own
> media library (store of everything it creates) + delivery.**
> The operator uploads the core/master brand assets (logos, brand guidelines) to the company's Google Drive
> `asset_folder` — the human-curated source of truth, NEVER the live website. Cortex *reads* them (read-only),
> *produces* deliverables (signatures, generated images, pages, newsletters, guideline docs), and *stores
> every creation* in Cloudflare R2 under that company's folder — so it can both DELIVER them (public
> `media.coretex.uk` URL) and REUSE them as context later. Site-specific web images may also live in that
> site's own CMS/Media Library.

---

## The storage layout (where everything lives)

| Layer | Location | Role | Access |
|---|---|---|---|
| **Source / masters** | Google Drive `<COMPANY> CORTEX` folder (id in `company_profiles.data.asset_folder`), under the Cortex Drive root `1oEKRo6aH4r1-HE29Qtds5DxyMlyZATEZ` | Brand kit, logos, source files the operator uploads | Box Drive OAuth `rashad@sensa.digital` (drive.readonly), via `cortex/drive.py` |
| **Cache** | `company_profiles.data.brand` | Brand kit Cortex distils from the asset folder (colours/fonts/logos/voice) | `cortex/brand.py` |
| **Delivery / CDN** | Cloudflare R2 bucket **`coretex-media`**, foldered **`<company-slug>/<type>/<status>/<file>`**, served at **`https://media.coretex.uk/...`** | Anything Cortex publishes that needs a public URL (logos, signature logos, newsletter & blog images, social graphics, generated media) | S3 keys `R2_*` in `/etc/cortex/cortex.env`; public via the bound custom domain |
| **Box static (interim)** | `/opt/cortex/assets/`, served at `coretex.uk/assets/` (FastAPI mount in `api.py`) | Stop-gap public host for signature logos until R2 delivery is live | public, no auth |

`type` ∈ {`logos`, `signatures`, `newsletters`, `blog`, `pages`, `social`, `video`, `brand`, `misc`}.
`status` ∈ {`published`, `draft`, `archived`}.

**R2 status (2026-06-19): LIVE.** `media.coretex.uk` is bound to `coretex-media`; the 5 signature logos
(Drive-sourced) are served from R2 at `media.coretex.uk/<slug>/signatures/published/<slug>-sig.png`.
`cortex/media.py` `put()`/`put_file()`/`url()` is the upload path for all future creations.

---

## New-company onboarding checklist (the standard, in order)

### 1. Identity & profile  — `store.upsert_company` + `company_profiles`
- Create the company row (`slug`, `name`, `kind`, `context`, `north_star`).
- Ensure a `company_profiles` row exists. The profile schema (`profile.QUESTIONS`) is identical for every
  company — every field below exists from day one; onboarding fills what it can and flags the rest.
- Legal/finance: `legal_name` (= "<parent legal entity>, trading as <brand>"), `registration` (VAT/licence),
  `bank_details`, `currency`, `vat`. The two parent entities are in `reference_company_legal_entities`.

### 2. Skills & rules — `catalog.seed_all` + `universal_skill_rules`
- Seed the **full uniform skill roster** (every skill exists for every company, even blank). NEVER add a
  skill to one company only. Symmetric roster is the invariant.
- **Universal rules apply automatically** (per `skill_key`, company-agnostic) — including the
  **brand-fidelity rule** (use the real asset from the brand kit / `asset_folder` first, never invent;
  flag-and-ask if missing) on every brand-emitting skill, and the comments-off / noindex / staggered
  publishing / Haiku-for-batch rules. No per-company action needed.

### 3. Brand assets — `cortex/drive.py` + `cortex/brand.py`
- **SOURCE OF TRUTH = the company's Drive `asset_folder`, NEVER the live website.** Even when the company's
  own site shows the logo/asset, take it from the Drive folder (or the cached brand kit built from it) — never
  scrape or hotlink the website (sites get redesigned and break). If an asset isn't in the folder, flag + ask;
  never fall back to the site. Baked as a universal rule on every brand-emitting skill (2026-06-19).
- Record the Drive `asset_folder` link on the profile (operator uploads masters there).
- **Build & cache the brand kit** (`brand.refresh_files` + `set_brand_kit`): colours, fonts, logo
  inventory, voice → `company_profiles.data.brand`.
- Identify the **light-background logo** (sigs/docs render on white) and the dark-bg logo from the folder.
  Flag if a needed variant is missing (e.g. a brand with only a white logo needs a dark/reverse version).

### 4. Email signatures — `profile.signature` + `profile.signature_html` + `engine.compose_reply_*`
- **Plain text** `signature`: name / role / contact lines / address (used for the plain-text email part +
  worker grounding). No "Best regards," and no logo (the composer adds those).
- **Rich HTML** `signature_html`: the designed signature in the house format — table, brand-accent
  left border, logo, name, company, phone line(s), email | web, the P316 address line. Rendered on sends
  AND shown in the cockpit profile (`pfReview` special-cases it).
- **Logo**: the brand's light-background logo, **hosted on R2** at `media.coretex.uk/<slug>/signatures/`
  (the delivery standard — same for every company, decoupled from the live sites so a redesign never breaks
  a signature). Interim fallback only until R2 is live: a live-site URL or `coretex.uk/assets/<slug>-sig.png`.
  Never base64 in the stored field (it would bloat worker grounding — `grounding._profile_block` skips
  `signature_html` + `brand`).
- House signature spec: Arial; `border-left:3px solid <brand-accent>`; name bold `#111`; company/phone
  `#555`; address `#888 12px`; address = "P316 The Binary, Business Bay, Dubai, UAE · PO Box 414195".

### 5. Channels & comms
- **Per-company Google OAuth** (Internal app in the brand's Workspace): client JSON
  `/etc/cortex/google_oauth_client_<slug>.json` (**chmod 640 root:cortex**); connect read + send via
  `/oauth/google/start?purpose=gmail|gmail_send&company=<slug>`; tokens `gmail_refresh_token:<slug>` etc.
- **Inbox classifier** (`engine.poll_all_inboxes`) + **contact-form / waitlist intake** wired per company.
- **Newsletter**: `send_domain` = the company's verified Mailgun `news.<domain>`; a **test group**
  (`newsletter_test_group`) of ~10 addresses; rolling-6 queue; staggered monthly publish day.

### 6. CRM — `crm_master`
- Contacts org-tagged to the company (`organisation`); inbound classified (lead/partner/…); `is_client`
  sticky; per-company scoping in lists, global search with company labels.

### 7. Content & web standards
- Every page/blog ships **noindex by default** + **comments OFF**; CMS-wired; staggered monthly publish
  day; brand-fidelity on all visuals; no em/en dashes in copy.
- **Brand guidelines doc** generated from the house template (`docs` family format: dark doc, numbered
  sections Logo/Colour/Typography/Components/Surfaces) with the company's REAL brand (logos from the asset
  folder, colours/fonts from the live theme). Output HTML + PDF to the operator + the company's Drive folder.

### 8. Delivery / media (R2)
- Create the company's R2 prefix `coretex-media/<slug>/…` and save all published deliverables there,
  referencing `media.coretex.uk` URLs.

---

## Status

- ✅ **`cortex/onboard.py`** BUILT — `onboard_company(slug, …)` (idempotent) + `POST /api/company/onboard`.
  Seeds the uniform roster only when the company has none (never clobbers live edits), ensures a profile,
  refreshes the brand kit from Drive, optionally builds the signature, and returns a did/todo checklist.
- ✅ **`cortex/signature.py`** BUILT — the single house-format builder (`build_html` / `build_plain` /
  `logo_tile` / `logo_plain` / `store_for`). Onboarding calls `store_for(...)` so a new company's signature
  comes out in the standard format.
- ✅ **`cortex/media.py`** BUILT — R2 delivery helper (`put(company, type, name, bytes) -> media.coretex.uk URL`,
  keyed `<slug>/<type>/<status>/<name>`).
- ✅ **R2 LIVE (2026-06-19)** — `media.coretex.uk` bound to `coretex-media`; all 5 signature logos
  (Drive-sourced) uploaded to R2 and the signatures reference the `media.coretex.uk` URLs — no website URLs
  anywhere. R2 is now Cortex's per-company library for all future creations (`cortex/media.py`).

---

*Created 2026-06-19. Companion code: `cortex/onboard.py` (to build), `cortex/brand.py`, `cortex/drive.py`,
`cortex/engine.py` (compose_reply_*), `cortex/profile.py`, `api.py` (/assets, /api/crm/contacts/count).*
