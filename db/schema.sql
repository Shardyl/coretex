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
    authority     text not null default 'ask',           -- ask | auto | never
    stakes        text not null default 'low',           -- low | high (reversibility tier)
    trust_streak  int  not null default 0,               -- clean approvals in a row
    auto_threshold int not null default 10,              -- streak needed before auto is offered
    paused        boolean not null default false,
    rules         jsonb not null default '[]'::jsonb,    -- confirmed standing rules
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

-- global app key/value (telegram update offset, etc.)
create table if not exists settings (
    key    text primary key,
    value  jsonb not null
);
