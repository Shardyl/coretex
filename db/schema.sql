-- Cortex core schema (Phase 1)
-- Run idempotently: every statement is CREATE ... IF NOT EXISTS.

create table if not exists companies (
    id          bigserial primary key,
    slug        text unique not null,
    name        text not null,
    kind        text not null default 'owned',          -- owned | client
    context     jsonb not null default '{}'::jsonb,      -- voice, audience, do's & don'ts
    north_star  text,
    active      boolean not null default true,
    created_at  timestamptz not null default now()
);

create table if not exists skills (
    id            bigserial primary key,
    company_id    bigint references companies(id) on delete cascade,
    skill_key     text not null,                         -- e.g. 'content-blog-posts'
    name          text not null,
    category      text,                                  -- Demand | Convert | Deliver | Run the business
    department    text,                                  -- e.g. 'Content & SEO'
    manager       text,                                  -- e.g. 'Content manager'
    craft         text not null default '',              -- the skill instructions (markdown)
    model         text,                                  -- worker model tier: 'opus' | 'sonnet' | null (=Sonnet default)
    authority     text not null default 'ask',           -- ask | auto | never
    stakes        text not null default 'low',           -- low | high (reversibility tier — gates money/auto)
    trust_streak  int  not null default 0,               -- clean approvals in a row
    auto_threshold int not null default 10,              -- streak needed before auto is offered
    paused        boolean not null default false,
    rules         jsonb not null default '[]'::jsonb,    -- confirmed standing rules (this company)
    overrides     jsonb not null default '[]'::jsonb,    -- universal-rule texts this company SUPERSEDES
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (company_id, skill_key)
);

create table if not exists tasks (
    id          bigserial primary key,
    company_id  bigint references companies(id),
    skill_id    bigint references skills(id),
    kind        text not null,                           -- what kind of work
    status      text not null default 'new',             -- new|drafting|awaiting_approval|approved|rejected|done|failed
    request     jsonb not null default '{}'::jsonb,      -- the brief/input
    draft       text,                                    -- current draft output
    manager     jsonb,                                   -- manager verdict
    attempts    int not null default 0,
    tg_message_id bigint,                                -- Telegram message awaiting a tap
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists decisions (
    id          bigserial primary key,
    task_id     bigint references tasks(id) on delete cascade,
    skill_id    bigint references skills(id),
    actor       text not null,                           -- owner | pa | pm | cortex
    action      text not null,                           -- approve | correct | reject | auto | rule_confirmed
    note        text,                                    -- correction text / reason / inferred rule
    snapshot    jsonb,                                   -- before/after for rollback
    created_at  timestamptz not null default now()
);

-- universal rules: apply to EVERY company for a given skill_key (shared layer).
-- Local/per-company rules stay in skills.rules. The worker applies universal + local together.
create table if not exists universal_skill_rules (
    skill_key   text primary key,
    rules       jsonb not null default '[]'::jsonb,
    updated_at  timestamptz not null default now()
);

-- saved Talk conversations (so chats persist + can be switched between).
create table if not exists conversations (
    id          bigserial primary key,
    title       text not null default 'New chat',
    company     text,
    messages    jsonb not null default '[]'::jsonb,      -- [{role, content}, ...]
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- global app key/value (telegram update offset, etc.)
create table if not exists settings (
    key    text primary key,
    value  jsonb not null
);

-- Manager questionnaires. Cortex (the Manager) drafts the question set per (area, tier, scope).
-- tier basic = UNIVERSAL (company_id 0); deeper/deepest = PER COMPANY (company_id = that company).
-- rule_sig records the rule set the questions were built to cover (for rule-aware self-updating).
create table if not exists questionnaires (
    department  text not null,                              -- area key: a department, or 'General Operations'
    tier        text not null,                              -- basic | deeper | deepest
    company_id  bigint not null default 0,                  -- 0 = universal (Basic); else the company
    questions   jsonb not null default '[]'::jsonb,         -- [{id, text, type:'choice'|'open', options?:[]}]
    rule_sig    text not null default '',                   -- signature of rules covered when last built
    updated_at  timestamptz not null default now(),
    primary key (department, tier, company_id)
);

-- A run is one lane answering a questionnaire (resumable). Basic = one universal lane (company_id 0);
-- deeper/deepest = one lane per company. Tracks where you're up to + your answers.
create table if not exists questionnaire_runs (
    id          bigserial primary key,
    company_id  bigint not null default 0,                  -- 0 = universal (Basic)
    department  text not null,
    tier        text not null,
    answers     jsonb not null default '[]'::jsonb,         -- [{q, a}] in order
    idx         int  not null default 0,                    -- next unanswered question index
    status      text not null default 'in_progress',        -- in_progress | done
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (company_id, department, tier)
);

-- ---------------------------------------------------------------------------
-- Fitness (PERSONAL data, kept in its own schema so it never mixes with the
-- company tables and is obvious in a dump). Source of truth for the training
-- PWA, which used to keep everything in phone localStorage with an XLSX export
-- as the only backup. The app now syncs here.
--
-- Two representations on purpose:
--   * normalised rows below  -> queryable by Cortex (PRs, streaks, reports)
--   * fitness.snapshots      -> the exact client document of every push, so a
--                               bad sync can always be rolled back. Never prune
--                               this without asking; it is the safety net that
--                               localStorage never had.
-- Keys are the client's own ids (uid), so a re-push updates rather than
-- duplicates. Rows are never hard-deleted; the app sets deleted=true.
-- ---------------------------------------------------------------------------
create schema if not exists fitness;

-- dated bodyweight log. THE reason bodyweight lifts can be scored: a pull-up
-- session is resolved against the weight that applied on its own date, so
-- gaining or losing weight never rewrites old records. Not carried by the XLSX
-- export, so this table is the only durable home it has ever had.
create table if not exists fitness.bodyweight (
    day        date primary key,
    kg         numeric not null,
    notes      text,
    updated_at timestamptz not null default now()
);

-- lifting plans (the app's "workouts"): ordered exercise list per plan.
create table if not exists fitness.plans (
    uid        text primary key,                        -- client id, e.g. wk_default
    name       text not null,
    exercises  jsonb not null default '[]'::jsonb,      -- [{id, name, sets, rest, mode?, fields?}]
    deleted    boolean not null default false,
    updated_at timestamptz not null default now()
);

-- one logged lifting session = one exercise on one day.
create table if not exists fitness.lift_sessions (
    uid         text primary key,                       -- client id (or exercise|date for migrated rows)
    exercise    text not null,
    day         date not null,
    plan_uid    text,
    weight      text,                                   -- 'BW' or a kg figure, as entered
    kg          numeric,                                -- parsed load, null for bodyweight lifts
    sets        jsonb not null default '[]'::jsonb,     -- reps per set, halves allowed (5.5)
    total_reps  numeric,
    best_set    numeric,
    volume_load numeric,                                -- reps x kg; null until a bodyweight covers the date
    rest        text,
    target      text,
    next_target jsonb,
    readings    jsonb,                                  -- manual-mode readings (e.g. dynamometer grip)
    notes       text,
    deleted     boolean not null default false,
    updated_at  timestamptz not null default now()
);
create index if not exists lift_sessions_day_idx on fitness.lift_sessions (day);
create index if not exists lift_sessions_ex_idx  on fitness.lift_sessions (exercise, day);

-- cardio presets. A PR is only ever compared within one preset, so this is not
-- cosmetic grouping: it is what stops a 30-minute session competing with a 67.
create table if not exists fitness.cardio_presets (
    uid           text primary key,
    name          text not null,
    brand         text,
    location      text,
    machine       text,
    machine_note  text,
    is_hiit       boolean not null default false,
    target_duration text,
    manual_fields jsonb not null default '[]'::jsonb,   -- [{label, type}] — index order matters, extra{} keys to it
    deleted       boolean not null default false,
    updated_at    timestamptz not null default now()
);

create table if not exists fitness.cardio_sessions (
    uid         text primary key,
    preset_uid  text,
    preset_name text,                                   -- denormalised: presets get renamed, history should not shift
    day         date not null,
    duration    text,                                   -- as entered ('1:07:36' or '67.5')
    minutes     numeric,                                -- parsed, for charting
    avg_hr      numeric,
    max_hr      numeric,
    calories    numeric,
    distance_km numeric,
    m_per_beat  numeric,                                -- aerobic efficiency: metres per heartbeat
    extra       jsonb not null default '{}'::jsonb,     -- {"0":"7.5","1":"16"} keyed to preset.manual_fields index
    next_target jsonb,
    notes       text,
    deleted     boolean not null default false,
    updated_at  timestamptz not null default now()
);
create index if not exists cardio_sessions_day_idx on fitness.cardio_sessions (day);

-- lab / test VO2 max results (71.4 in May 2026). Also never carried by the export.
create table if not exists fitness.vo2 (
    uid        text primary key,
    day        date not null,
    value      numeric not null,
    method     text,
    notes      text,
    deleted    boolean not null default false,
    updated_at timestamptz not null default now()
);

-- every client push, verbatim, newest last. Restore path of last resort.
create table if not exists fitness.snapshots (
    id         bigserial primary key,
    source     text not null default 'app',             -- app | import | manual
    rows       int  not null default 0,
    doc        jsonb not null,
    doc_hash   text,                                    -- skip storing a push identical to the last
    created_at timestamptz not null default now()
);

-- media_assets: the media CATALOG from the locked YouTube/media spec — one row per asset,
-- linking r2_key ↔ youtube_video_id ↔ watch_url, plus the Haiku classification layer
-- (content_type / client / ai_production / portfolio_category) so Cortex knows what every
-- video on a company channel IS. Populated by the channel inventory sync (Sensa loaded
-- 2026-08-25, 322 videos). YouTube owns live state; catalog owns linkage + distribution.
-- Rows are never deleted by sync — vanished videos get status='removed'.
create table if not exists media_assets (
    id                 serial primary key,
    company_id         int not null references companies(id),
    r2_key             text,
    youtube_video_id   text unique,
    watch_url          text,
    title              text,
    description        text,
    privacy            text,                             -- public | unlisted | private
    published_at       timestamptz,
    duration           text,                             -- ISO-8601 (PT2M31S)
    definition         text,                             -- hd | sd
    views              int,
    thumb_url          text,
    tags               jsonb default '[]',
    content_type       text,                             -- client-film | showreel | bts | event-coverage | case-study | version-variant | internal-test | brand-content
    client             text,
    ai_production      boolean default false,
    portfolio_category text,                             -- site portfolio slug when the film is on sensa.digital
    status             text default 'live',              -- live | removed
    posted_to          jsonb default '[]',
    source             text default 'youtube-sync',
    classified_at      timestamptz default now(),
    created_at         timestamptz default now(),
    updated_at         timestamptz default now()
);
