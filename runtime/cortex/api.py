"""Cortex cockpit API — the HTTP surface the PWA (and later voice) talk to.

Reuses the same engine/store/db as the always-on engine service: this process exposes
read views (companies, skills, the approval inbox, the decision log) and write actions
(create a task, approve / skip / correct). The engine service still does the heavy lifting
(drafting new tasks, polling Telegram); the API just lets the cockpit drive the same loop.

Auth: a single operator passcode -> a signed, expiring bearer token (single-operator app;
proper multi-user logins come with the PA/PM roles later). Passcode in CORTEX_PASSCODE.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

import httpx
import websockets
from fastapi import (Body, Depends, FastAPI, File, Header, HTTPException, Response,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Json
from pydantic import BaseModel

from . import (anchor_score, capabilities, catalog, config, contentqueue, crm, db, engine, gmail, knowledge,
               notifications, personas, profile, provider, push, questionnaire, reminders, schedule, seo_report,
               skillqa, social, store, webauthn_auth, worker)

app = FastAPI(title="Cortex API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?coretex\.uk|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

TOKEN_TTL = 60 * 60 * 24 * 14  # 14 days


# ---------- auth ----------

def _secret() -> bytes:
    s = db.setting_get("api_secret")
    if not s:
        s = secrets.token_hex(32)
        db.setting_set("api_secret", s)
    return s.encode()


def _sign(payload: str) -> str:
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _make_token(subject: str = "owner") -> str:
    exp = str(int(time.time()) + TOKEN_TTL)
    payload = f"{subject}.{exp}"
    return f"{payload}.{_sign(payload)}"


def _token_subject(token: str) -> str | None:
    """The token's subject ('owner' or a user id) if valid + unexpired, else None. Accepts the legacy
    2-part owner token (exp.sig) for back-compat."""
    parts = (token or "").split(".")
    if len(parts) == 3:
        subject, exp, sig = parts
        if exp.isdigit() and hmac.compare_digest(sig, _sign(f"{subject}.{exp}")) and int(exp) > time.time():
            return subject
        return None
    if len(parts) == 2:                       # legacy owner token
        exp, sig = parts
        if exp.isdigit() and hmac.compare_digest(sig, _sign(exp)) and int(exp) > time.time():
            return "owner"
    return None


def auth(authorization: str = Header(default="")) -> None:
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if _token_subject(token) is None:
        raise HTTPException(status_code=401, detail="not authenticated")


def current_user(authorization: str = Header(default="")) -> dict:
    """The acting user. 'owner' (Rashad's passcode) = full access, companies=None. A named user = their row +
    company scope (the slugs they may access)."""
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    subj = _token_subject(token)
    if subj is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    if subj == "owner":
        return {"id": None, "name": "Owner", "role": "owner", "companies": None}
    u = db.one("select id, name, email, role, companies, active from users where id=%s", (int(subj),)) if subj.isdigit() else None
    if not u or not u.get("active"):
        raise HTTPException(status_code=401, detail="account disabled")
    return u


def _ensure_users() -> None:
    db.execute("create table if not exists users (id bigserial primary key, name text not null, "
               "email text unique not null, passcode_hash text not null, role text not null default 'admin', "
               "companies jsonb not null default '[]'::jsonb, active boolean not null default true, "
               "created_at timestamptz default now())")
    db.execute("alter table users add column if not exists must_onboard boolean not null default true")
    db.execute("alter table users add column if not exists pin_hash text")    # the user's OWN quick-unlock PIN


class Login(BaseModel):
    passcode: str
    email: str | None = None          # named team user; omit for the owner passcode


@app.post("/api/login")
def login(body: Login) -> dict:
    _ensure_users()
    if body.email:                                       # a named team member (e.g. Gino)
        u = db.one("select * from users where lower(email)=lower(%s) and active", (body.email.strip(),))
        if u and hmac.compare_digest(_pin_hash(body.passcode.strip()), u["passcode_hash"]):
            return {"token": _make_token(str(u["id"])), "ttl": TOKEN_TTL,
                    "user": {"name": u["name"], "email": u["email"], "role": u["role"],
                             "companies": u["companies"], "must_onboard": bool(u.get("must_onboard"))}}
        raise HTTPException(status_code=401, detail="wrong email or passcode")
    expected = config.get("CORTEX_PASSCODE")              # the owner (Rashad)
    if expected and hmac.compare_digest(body.passcode.strip(), expected.strip()):
        return {"token": _make_token("owner"), "ttl": TOKEN_TTL,
                "user": {"name": "Owner", "role": "owner", "companies": None}}
    raise HTTPException(status_code=401, detail="wrong passcode")


class UserBody(BaseModel):
    name: str
    email: str
    passcode: str
    companies: list[str] = []
    role: str = "admin"


@app.post("/api/users")
def create_user(body: UserBody, u: dict = Depends(current_user)) -> dict:
    """Owner-only: create or update a team member's login + their company scope."""
    if u.get("role") != "owner":
        raise HTTPException(status_code=403, detail="owner only")
    _ensure_users()
    db.execute("insert into users (name,email,passcode_hash,role,companies,must_onboard) values (%s,%s,%s,%s,%s,true) "
               "on conflict (email) do update set name=excluded.name, passcode_hash=excluded.passcode_hash, "
               "role=excluded.role, companies=excluded.companies, active=true, must_onboard=true, pin_hash=null",
               (body.name.strip(), body.email.lower().strip(), _pin_hash(body.passcode.strip()),
                body.role, Json(body.companies)))
    return {"ok": True, "email": body.email.lower().strip(), "companies": body.companies, "temp_passcode": True}


@app.get("/api/users")
def list_users(u: dict = Depends(current_user)) -> list:
    if u.get("role") != "owner":
        raise HTTPException(status_code=403, detail="owner only")
    _ensure_users()
    return db.query("select id, name, email, role, companies, active, must_onboard from users order by name")


class UserOnboardBody(BaseModel):
    password: str
    pin: str


@app.post("/api/user/onboard")
def user_onboard(body: UserOnboardBody, u: dict = Depends(current_user)) -> dict:
    """First-login security setup: the user sets THEIR OWN password + quick-unlock PIN (the owner never holds them)."""
    if u.get("role") == "owner" or not u.get("id"):
        raise HTTPException(status_code=400, detail="the owner signs in with the server passcode")
    pw, pin = (body.password or "").strip(), (body.pin or "").strip()
    if len(pw) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        raise HTTPException(status_code=400, detail="PIN must be 4-8 digits")
    db.execute("update users set passcode_hash=%s, pin_hash=%s, must_onboard=false where id=%s",
               (_pin_hash(pw), _pin_hash(pin), u["id"]))
    return {"ok": True}


@app.get("/api/me")
def me(_: None = Depends(auth)) -> dict:
    return {"ok": True}


@app.get("/api/health")
def health() -> dict:
    try:
        db.one("select 1 as ok")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


# ---- app lock (a PIN, server-verified, on top of the session; biometric is a client gate) ----

def _pin_hash(pin: str) -> str:
    return hmac.new(_secret(), ("pin:" + pin).encode(), hashlib.sha256).hexdigest()


class Pin(BaseModel):
    pin: str


@app.get("/api/lock/status")
def lock_status(_: None = Depends(auth)) -> dict:
    return {"pin_set": bool(db.setting_get("pin_hash"))}


@app.post("/api/lock/set")
def lock_set(body: Pin, _: None = Depends(auth)) -> dict:
    p = (body.pin or "").strip()
    if not (p.isdigit() and 4 <= len(p) <= 8):
        raise HTTPException(status_code=400, detail="PIN must be 4-8 digits")
    db.setting_set("pin_hash", _pin_hash(p))
    return {"ok": True}


@app.post("/api/lock/check")
def lock_check(body: Pin, _: None = Depends(auth)) -> dict:
    h = db.setting_get("pin_hash")
    return {"ok": bool(h) and hmac.compare_digest(h, _pin_hash((body.pin or "").strip()))}


# ---- unified login: the 4-digit PIN (and biometric on mobile) ARE the login ----
# The PIN that unlocks the cockpit issues the session token directly; biometric (a registered
# platform passkey, phone only) does the same. The text passcode below stays as a recovery path.

@app.get("/api/auth/mode")
def auth_mode() -> dict:
    """Which login UI to show BEFORE authenticating (no token needed): PIN keypad vs passcode."""
    return {"pin": bool(db.setting_get("pin_hash")), "biometric": webauthn_auth.is_registered()}


class PinLogin(BaseModel):
    pin: str
    email: str | None = None        # a named user's per-user PIN; omit for the owner's PIN


@app.post("/api/login/pin")
def login_pin(body: PinLogin) -> dict:
    pin = (body.pin or "").strip()
    if body.email:                                       # named user -> their own PIN
        u = db.one("select id, name, role, companies, pin_hash, active from users where lower(email)=lower(%s)", (body.email.strip(),))
        if not (u and u.get("active") and u.get("pin_hash") and hmac.compare_digest(u["pin_hash"], _pin_hash(pin))):
            raise HTTPException(status_code=401, detail="wrong PIN")
        return {"token": _make_token(str(u["id"])), "ttl": TOKEN_TTL,
                "user": {"name": u["name"], "role": u["role"], "companies": u["companies"]}}
    h = db.setting_get("pin_hash")                        # owner PIN
    if not h:
        raise HTTPException(status_code=503, detail="no PIN set")
    if not hmac.compare_digest(h, _pin_hash(pin)):
        raise HTTPException(status_code=401, detail="wrong PIN")
    return {"token": _make_token("owner"), "ttl": TOKEN_TTL, "user": {"name": "Owner", "role": "owner", "companies": None}}


@app.post("/api/login/webauthn/options")
def login_wa_options() -> dict:
    if not webauthn_auth.is_registered():
        raise HTTPException(status_code=409, detail="no biometric registered")
    return webauthn_auth.auth_options()


@app.post("/api/login/webauthn/verify")
def login_wa_verify(credential: dict = Body(...)) -> dict:
    r = webauthn_auth.auth_verify(credential)
    if not r.get("ok"):
        raise HTTPException(status_code=401, detail=r.get("error", "biometric failed"))
    return {"token": _make_token(), "ttl": TOKEN_TTL}


# ---------- read views ----------

@app.get("/api/companies")
def companies(u: dict = Depends(current_user)) -> list[dict]:
    rows = db.query("select * from companies order by name")
    if u.get("companies") is None:                    # owner -> every company
        return rows
    allowed = set(u.get("companies") or [])           # a scoped user -> only their companies
    return [r for r in rows if r["slug"] in allowed]


def _user_cids(u: dict) -> list[int] | None:
    """The company_ids a user may access. None = owner (all). A list = their scope."""
    if u.get("companies") is None:
        return None
    out = []
    for slug in (u.get("companies") or []):
        co = store.get_company_by_slug(slug)
        if co:
            out.append(co["id"])
    return out or [-1]


def _request_cids(u: dict, company: str | None) -> list[int] | None:
    """The company_ids a request may touch, CLAMPED to the user's scope. None = no filter (owner, all
    companies). A list = restrict to these company_ids. A scoped user can NEVER widen past their companies:
    a specific in-scope company -> just that one; empty/'all'/out-of-scope -> ALL their companies."""
    req_cid = None
    if company and company not in ("", "all"):
        co = store.get_company_by_slug(company)
        req_cid = co["id"] if co else -1
    cids = _user_cids(u)
    if cids is None:                                  # owner
        return [req_cid] if req_cid is not None else None
    if req_cid is not None and req_cid in cids:       # scoped user asking for one of their companies
        return [req_cid]
    return cids                                       # else -> all THEIR companies


def _assert_company_allowed(u: dict, company_id) -> None:
    """For per-ID detail endpoints: 403 if the item's company is outside the user's scope."""
    cids = _user_cids(u)
    if cids is not None and company_id not in cids:
        raise HTTPException(status_code=403, detail="not in your company scope")


def _company_labels(u: dict) -> set:
    """The org-text labels (+ slugs) a scoped user may touch. Empty set is never returned for owner (use None)."""
    return {_CRM_ORG.get(s, str(s).title()).lower() for s in (u.get("companies") or [])} | set(u.get("companies") or [])


def _guard_task(u: dict, tid) -> None:
    t = db.one("select company_id from tasks where id=%s", (tid,))
    if t:
        _assert_company_allowed(u, t["company_id"])


def _guard_reminder(u: dict, rid) -> None:
    r = db.one("select company_id from reminders where id=%s", (rid,))
    if r and r.get("company_id") is not None:
        _assert_company_allowed(u, r["company_id"])


def _guard_deal(u: dict, deal_id) -> None:
    if u.get("companies") is None:
        return
    p = db.one("select company from crm_projects where id=%s", (deal_id,))
    if p and (p.get("company") or "").lower() not in _company_labels(u):
        raise HTTPException(status_code=403, detail="not in your company scope")


def _guard_contact(u: dict, email: str) -> None:
    if u.get("companies") is None:
        return
    c = db.one("select organisation from crm_master where lower(email)=lower(%s)", (email,))
    if c:
        orgs = {p.strip().lower() for p in ((c.get("organisation") or "")).split(",") if p.strip()}
        if not (orgs & _company_labels(u)):
            raise HTTPException(status_code=403, detail="not in your company scope")


def _scoped_companies(u: dict, company: str | None) -> list[str] | None:
    """The company SLUGS an org-text query should match, clamped to scope. None = owner + all (no filter).
    A scoped user: the requested company if in scope, else ALL their companies (OR-matched)."""
    if u.get("companies") is None:                    # owner
        return [company] if (company and company not in ("", "all")) else None
    allowed = u.get("companies") or []
    if company and company in allowed:
        return [company]
    return allowed or ["__none__"]                    # all THEIR companies; never widen to everyone


@app.get("/api/companies/{slug}/skills")
def skills(slug: str, u: dict = Depends(current_user)) -> list[dict]:
    if u.get("companies") is not None and slug not in (u.get("companies") or []):
        raise HTTPException(status_code=403, detail="not in your company scope")
    co = store.get_company_by_slug(slug)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return db.query(
        "select s.*, coalesce(u.rules, '[]'::jsonb) as universal_rules from skills s "
        "left join universal_skill_rules u on u.skill_key=s.skill_key "
        "where s.company_id=%s order by s.category, s.department, s.name", (co["id"],))


def _skill_with_rules(skill_id: int) -> dict:
    return db.one("select s.*, coalesce(u.rules, '[]'::jsonb) as universal_rules from skills s "
                  "left join universal_skill_rules u on u.skill_key=s.skill_key where s.id=%s", (skill_id,))


class RuleBody(BaseModel):
    rule: str
    scope: str = "company"   # "company" (this company only) | "universal" (all companies)


@app.post("/api/skills/{skill_id}/rule")
def add_skill_rule(skill_id: int, body: RuleBody, _: None = Depends(auth)) -> dict:
    sk = store.get_skill(skill_id)
    if not sk:
        raise HTTPException(status_code=404, detail="no such skill")
    rule = (body.rule or "").strip()
    if not rule:
        raise HTTPException(status_code=400, detail="empty rule")
    if body.scope == "universal":
        store.add_universal_rule(sk["skill_key"], rule)
    else:
        store.add_rule(skill_id, rule)
    return _skill_with_rules(skill_id)


class RuleIdx(BaseModel):
    index: int
    scope: str = "company"


@app.post("/api/skills/{skill_id}/rule/delete")
def del_skill_rule(skill_id: int, body: RuleIdx, _: None = Depends(auth)) -> dict:
    sk = store.get_skill(skill_id)
    if not sk:
        raise HTTPException(status_code=404, detail="no such skill")
    if body.scope == "universal":
        store.remove_universal_rule(sk["skill_key"], body.index)
    else:
        rules = list(sk.get("rules") or [])
        if 0 <= body.index < len(rules):
            rules.pop(body.index)
        db.execute("update skills set rules=%s::jsonb, updated_at=now() where id=%s", (json.dumps(rules), skill_id))
    return _skill_with_rules(skill_id)


@app.get("/api/tasks")
def tasks(status: str | None = None, company: str | None = None, limit: int = 50,
          _: None = Depends(auth)) -> list[dict]:
    where, params = [], []
    if status:
        where.append("status=%s"); params.append(status)
    if company:
        co = store.get_company_by_slug(company)
        where.append("company_id=%s"); params.append(co["id"] if co else -1)
    clause = ("where " + " and ".join(where)) if where else ""
    params.append(limit)
    return db.query(f"select * from tasks {clause} order by id desc limit %s", tuple(params))


def _enrich_action_card(t: dict) -> dict:
    """Enrich a task row as an Inbox ACTION card (the existing approval-card shape)."""
    t["card"] = "action"
    t["wp"] = db.setting_get(f"wp:{t['id']}")   # preview/edit links for blog drafts
    if t["kind"] in engine.EMAIL_RENDER_KINDS:  # emails: show the envelope (to/from/subject) + logo HTML
        co = store.get_company(t["company_id"])
        env = engine._email_envelope(t, co)
        inq = (t.get("request") or {}).get("inquiry") or {}
        t["email"] = {**env, "preview": engine.compose_reply_body(t, co),  # plain fallback
                      "html": engine.compose_reply_html(t, co, for_preview=True)["html"],  # rendered, with logo
                      "inquiry": {"name": inq.get("name"), "email": inq.get("email"),
                                  "message": inq.get("message") or inq.get("snippet") or ""}}
    sk = store.get_skill(t["skill_id"])         # the lane's autonomy state for the Inbox UI
    if sk:
        offer = (sk["authority"] == "ask" and sk["trust_streak"] >= sk["auto_threshold"]
                 and t["kind"] != "blog" and sk["stakes"] == "low")
        t["lane"] = {"skill_id": sk["id"], "name": sk["name"], "trust_streak": sk["trust_streak"],
                     "auto_threshold": sk["auto_threshold"], "authority": sk["authority"],
                     "stakes": sk["stakes"], "auto_offer": offer}
    t["title"] = t.get("title") or (t.get("request") or {}).get("title")
    t["skill"] = sk["name"] if sk else t.get("skill")
    t["approve_label"] = engine.approve_label(t["kind"])   # exact consequence on the Approve button
    t["ts"] = (t.get("updated_at") or t.get("created_at"))
    # NEVER ship the heavy attachment base64 in the list response — just a count (the files stay on the task
    # for the send; fetch them on demand). A single phone screenshot can be ~9MB and chokes the connection.
    req = t.get("request") or {}
    atts = req.get("attachments") or []
    t["att_count"] = len(atts)
    if atts:
        t["request"] = {**req, "attachments": []}
    pr = db.setting_get(f"rule:{t['id']}")     # a correction inferred a standing rule, not yet decided -> persist it on the card
    if pr:
        _p = pr if isinstance(pr, dict) else {}
        t["proposed_rule"] = _p.get("rule") if _p else pr
        t["rule_skill"] = _p.get("skill_name") or (sk["name"] if sk else None)
        t["rule_company"] = _p.get("company") or ((store.get_company(t["company_id"]) or {}).get("name") if t.get("company_id") else None)
    return t


def _info_card(n: dict) -> dict:
    """A notification rendered as an Inbox INFO card (swipe-to-dismiss)."""
    return {"card": "info", "id": n["id"], "title": n["title"], "body": n.get("body"),
            "category": n["category"], "priority": n["priority"], "count": n.get("count", 1),
            "items": n.get("items") or [], "company": ((store.get_company(n["company_id"]) or {}).get("name", "") if n.get("company_id") else ""),
            "target_type": n.get("target_type"), "target_id": n.get("target_id"),
            "state": n["state"], "ts": n.get("fired_at")}


@app.get("/api/inbox")
def inbox(company: str | None = None, u: dict = Depends(current_user)) -> dict:
    """Unified Inbox stream: open tasks (ACTION cards) + active notifications (INFO cards).
    Action cards first (Needs-you), then info newest-first. The task IS its own card — notifications
    are never mirrored for tasks (see merged spec §1). Scoped to the user's company(ies)."""
    cids = _request_cids(u, company)
    where = "status in ('awaiting_approval','awaiting_correction')"
    params: list = []
    if cids is not None:
        where += " and company_id = any(%s)"
        params.append(cids)
    tasks = db.query(f"select * from tasks where {where} order by id desc", tuple(params))
    actions = [_enrich_action_card(t) for t in tasks]
    infos = [_info_card(n) for n in notifications.active(cids)]
    return {"items": actions + infos, "needs_you": len(actions), "updates": len(infos),
            "unread": notifications.unread_count(cids)}


@app.get("/api/inbox/count")
def inbox_count(company: str | None = None, u: dict = Depends(current_user)) -> dict:
    """Tiny counts for the tab badge — so the 25s badge poll never drags the whole (heavy) inbox down."""
    cids = _request_cids(u, company)
    where = "status in ('awaiting_approval','awaiting_correction')"
    params: list = []
    if cids is not None:
        where += " and company_id = any(%s)"
        params.append(cids)
    n = db.one(f"select count(*) c from tasks where {where}", tuple(params))["c"]
    return {"needs_you": int(n), "unread": notifications.unread_count(cids)}


@app.get("/api/tasks/{tid}/attachments")
def task_attachments(tid: int, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, tid)
    """A task's attachments, fetched ON DEMAND (they're stripped from the inbox list for weight). Lets a card
    show image thumbnails + document names so the owner can SEE what they're approving before they send it."""
    t = db.one("select request from tasks where id=%s", (tid,))
    if not t:
        raise HTTPException(status_code=404, detail="not found")
    req = t["request"] or {}
    atts = req.get("attachments") or []
    names = req.get("attachment_names") or []
    out = []
    for i, u in enumerate(atts):
        if not isinstance(u, str) or not u.startswith("data:") or ";base64," not in u:
            continue
        head, b64 = u.split(";base64,", 1)
        media = head[5:] or "application/octet-stream"
        nm = names[i] if i < len(names) and names[i] else None
        out.append({"idx": i, "media_type": media, "is_image": media.startswith("image/"),
                    "size_kb": (len(b64) * 3) // 4 // 1024, "name": nm, "src": u})
    return {"attachments": out}


# ---------- social runner (LinkedIn automation: brain <-> office-box hands) ----------

def _runner_auth(x_runner_key: str = Header(default="")) -> None:
    """The office-box runner authenticates with a shared key (setting 'runner_key'), not the operator passcode."""
    key = db.setting_get("runner_key")
    if not key or x_runner_key != key:
        raise HTTPException(status_code=403, detail="invalid runner key")


class ShiftBody(BaseModel):
    account: str
    persona: str
    company_id: int
    plan: dict
    strategy: str = ""
    connect_targets: list = []
    engage_targets: list = []
    date: str = ""
    week: int | None = None
    phase: str = ""


@app.post("/api/social/shift")
def social_post_shift(body: ShiftBody, _: None = Depends(_runner_auth)) -> dict:
    """The runner posts the day's governed plan -> the approve-the-shift card (deduped per account+date)."""
    dup = db.one("select id from tasks where kind='social_shift' "
                 "and status in ('awaiting_approval','awaiting_correction') "
                 "and request->>'account'=%s and request->>'date'=%s order by id desc limit 1",
                 (body.account, body.date))
    if dup:
        return {"ok": True, "task_id": dup["id"], "deduped": True}
    t = social.post_shift_card(body.company_id, body.account, body.persona, body.plan, body.strategy,
                               connect_targets=body.connect_targets, engage_targets=body.engage_targets,
                               date=body.date, week=body.week, phase=body.phase)
    return {"ok": True, "task_id": t["id"]}


class ReloginBody(BaseModel):
    account: str
    persona: str
    company_id: int


@app.post("/api/social/relogin")
def social_post_relogin(body: ReloginBody, _: None = Depends(_runner_auth)) -> dict:
    """The runner reports a dropped session -> the logged-out alert card (the auto-reopen happens runner-side)."""
    t = social.post_relogin_card(body.company_id, body.account, body.persona)
    return {"ok": True, "task_id": t["id"]}


@app.get("/api/social/state")
def social_state(account: str, date: str = "", _: None = Depends(_runner_auth)) -> dict:
    """The runner reads its green light: is today's shift approved, and is the account flagged logged-out."""
    ok = db.setting_get(f"social_shift_ok:{account}:{date}") or {}
    return {"approved": bool(ok.get("approved")), "plan": ok.get("plan"),
            "logged_out": bool(db.setting_get(f"social_loggedout:{account}"))}


_SOCIAL_ACTIONS = ("post", "like", "comment", "connect", "view", "dm")


class ActionRequest(BaseModel):
    account: str
    company_id: int
    persona: str = "Paul Anderson"
    action: str                  # post | like | comment | connect | view | dm
    target: str = ""             # profile/post URL (like/comment/connect/view/dm)
    content: str = ""            # text (post/comment/dm)


@app.post("/api/social/request")
def social_request(body: ActionRequest, u: dict = Depends(current_user)) -> dict:
    """Operator requests a one-off manual action -> a social_action approval card in the Inbox. The owner
    approves it (fingerprint), which queues it for the runner to perform."""
    if body.action not in _SOCIAL_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action (use {', '.join(_SOCIAL_ACTIONS)})")
    t = social.post_action_card(body.company_id, body.account, body.persona, body.action,
                                target=body.target, content=body.content)
    return {"ok": True, "task_id": t["id"]}


@app.get("/api/social/jobs")
def social_jobs(account: str, _: None = Depends(_runner_auth)) -> dict:
    """The runner pulls APPROVED one-off actions to perform (the queued social_action cards)."""
    rows = db.query("select id, request from tasks where kind='social_action' and status='queued' "
                    "and request->>'account'=%s order by id", (account,))
    return {"jobs": [{"id": r["id"], **(r["request"] or {})} for r in rows]}


class JobResult(BaseModel):
    ok: bool
    detail: str = ""


@app.post("/api/social/jobs/{tid}/result")
def social_job_result(tid: int, body: JobResult, _: None = Depends(_runner_auth)) -> dict:
    """The runner reports the outcome of a one-off action -> mark the card done/failed + notify the owner."""
    t = store.get_task(tid)
    if not t or t["kind"] != "social_action":
        raise HTTPException(status_code=404, detail="no such job")
    store.update_task(tid, status="done" if body.ok else "failed", last_status=body.detail[:300])
    req = t.get("request") or {}
    try:
        notifications.notify(f"{req.get('persona', 'Paul')}: {req.get('action', 'action')} "
                             + ("done" if body.ok else "failed"),
                             body.detail[:300], category="social", company_id=t["company_id"])
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


class HarvestBody(BaseModel):
    account: str
    persona: str = "Paul Anderson"
    company_id: int = 5
    region: str = "UK"
    anchor: str                 # the source anchor (name or profile URL)
    post: str = ""              # source post URL
    leads: list[dict] = []      # [{name, headline, linkedin, location, engagement, type?}]


@app.post("/api/social/harvest")
def social_harvest(body: HarvestBody, _: None = Depends(_runner_auth)) -> dict:
    """The runner pushes a batch of harvested anchor engagers. They are CLASSIFIED at ingest (Haiku: lead vs
    vendor) and ONLY the buyers are stored in crm_master; vendors are DROPPED but counted, so the anchor's
    hit-rate is captured permanently. Reads + CRM writes only - no outward action fires from here."""
    slug = (store.get_company(body.company_id) or {}).get("slug", "filmspoke")
    classified = anchor_score.classify_leads(body.leads, body.company_id)
    ins = upd = buyers = vendors = 0
    scores: list = []
    for lead, cl in zip(body.leads, classified):
        c = (cl.get("classification") or "").strip().lower()
        if c == "vendor":
            vendors += 1
            continue
        if c != "lead":
            continue                          # unclassifiable -> not stored, not counted as a buyer
        buyers += 1
        sc = cl.get("score")
        if isinstance(sc, (int, float)):
            scores.append(sc)
        lead2 = {**lead, "post": lead.get("post") or body.post,
                 "classification": "lead", "type": cl.get("type"), "score": sc}
        try:
            r = crm.upsert_anchor_lead(lead2, body.persona, body.region, body.anchor, company=slug)
        except Exception:  # noqa: BLE001 - one bad lead must never kill the batch
            r = "skipped"
        ins += r == "inserted"
        upd += r == "updated"
    try:
        crm.record_anchor_stats(body.company_id, body.anchor, body.post, len(body.leads), buyers, vendors, scores)
    except Exception:  # noqa: BLE001
        pass
    hr = round(100 * buyers / (buyers + vendors)) if (buyers + vendors) else None
    if buyers:
        try:
            notifications.notify(f"Anchor harvest: {body.anchor}",
                                 f"{buyers} buyers kept ({ins} new), {vendors} vendors dropped, {hr}% hit-rate.",
                                 category="social", company_id=body.company_id, priority="fyi")
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "inserted": ins, "updated": upd, "buyers": buyers, "vendors_dropped": vendors, "hit_rate": hr}


class ScoreBody(BaseModel):
    company_id: int = 5
    limit: int = 60


@app.post("/api/social/score")
def social_score(body: ScoreBody, _: None = Depends(_runner_auth)) -> dict:
    """Score+type the untyped harvested leads (whale/amplifier/decision-maker) via the Haiku batch. Called by
    the runner at the end of a harvest run so the pipeline is one pass: harvest -> CRM -> scored. Safe to re-run
    (only touches rows without a 'scored' tag)."""
    try:
        return {"ok": True, **anchor_score.score_harvested(body.company_id, limit=body.limit)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.get("/api/social/anchors")
def social_anchors(company_id: int = 5, _: None = Depends(auth)) -> dict:
    """The ANCHOR-GRADE REPORT: every harvested anchor ranked by HIT-RATE (buyer-fraction of its audience),
    with avg buyer score + counts. High hit-rate = the partner shortlist. Stats are captured at harvest time
    (before any vendor drop), so this is durable and never recomputed from deleted rows."""
    try:
        rows = db.query(
            "select name, posts, engagers, buyers, vendors, "
            "case when (buyers+vendors)>0 then round(100.0*buyers/(buyers+vendors)) else null end as hit_rate, "
            "case when buyers>0 then round(sum_score/buyers,1) else null end as avg_score, last_harvest "
            "from social_anchors where company_id=%s order by hit_rate desc nulls last, buyers desc", (company_id,))
    except Exception:  # noqa: BLE001 - table may not exist until the first harvest
        rows = []
    return {"anchors": rows}


# ---------- notification actions (info cards) ----------

@app.post("/api/notifications/{nid}/read")
def notif_read(nid: int, _: None = Depends(auth)) -> dict:
    return {"ok": True, "n": notifications.set_state(nid, "read")}


@app.post("/api/notifications/{nid}/dismiss")
def notif_dismiss(nid: int, _: None = Depends(auth)) -> dict:
    return {"ok": True, "n": notifications.set_state(nid, "dismissed")}


class SnoozeBody(BaseModel):
    minutes: int = 60


@app.post("/api/notifications/{nid}/snooze")
def notif_snooze(nid: int, body: SnoozeBody, _: None = Depends(auth)) -> dict:
    from datetime import datetime, timedelta
    until = datetime.now(schedule._GST) + timedelta(minutes=max(1, body.minutes))
    return {"ok": True, "n": notifications.set_state(nid, "snoozed", snooze_until=until)}


# ---------- reminders ----------

class ReminderBody(BaseModel):
    title: str
    when: str | None = None          # natural-language ("next Tuesday 10am") -> parsed
    due_at: str | None = None        # OR an explicit ISO datetime
    company: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    recurrence: str = "none"         # none|daily|weekly|monthly|weekday|custom
    custom_days: int | None = None
    priority: str = "normal"
    action: dict | None = None       # null = nudge; else {company, skill, kind, brief}


@app.get("/api/reminders")
def reminders_list(status: str | None = None, company: str | None = None,
                   u: dict = Depends(current_user)) -> list[dict]:
    cids = _request_cids(u, company)
    return reminders.listing(status=status, company_id=cids)


@app.post("/api/reminders")
def reminders_create(body: ReminderBody, _: None = Depends(auth)) -> dict:
    from datetime import datetime
    due = None
    if body.due_at:
        try:
            due = datetime.fromisoformat(body.due_at)
            due = due.replace(tzinfo=schedule._GST) if due.tzinfo is None else due
        except Exception:  # noqa: BLE001
            due = None
    if due is None and body.when:
        due = reminders.parse_when(body.when)
    if due is None:
        raise HTTPException(status_code=400, detail="could not work out when to remind you")
    cid = None
    if body.company:
        co = store.get_company_by_slug(body.company)
        cid = co["id"] if co else None
    r = reminders.create(body.title, due, company_id=cid, target_type=body.target_type,
                         target_id=body.target_id, recurrence=body.recurrence, custom_days=body.custom_days,
                         priority=body.priority, action=body.action)
    return {"ok": True, "reminder": r}


@app.post("/api/reminders/{rid}/snooze")
def reminder_snooze(rid: int, body: SnoozeBody, u: dict = Depends(current_user)) -> dict:
    _guard_reminder(u, rid)
    from datetime import datetime, timedelta
    until = datetime.now(schedule._GST) + timedelta(minutes=max(1, body.minutes))
    return {"ok": True, "reminder": reminders.snooze(rid, until)}


@app.post("/api/reminders/{rid}/done")
def reminder_done(rid: int, u: dict = Depends(current_user)) -> dict:
    _guard_reminder(u, rid)
    return {"ok": True, "reminder": reminders.mark_done(rid)}


@app.post("/api/reminders/{rid}/cancel")
def reminder_cancel(rid: int, u: dict = Depends(current_user)) -> dict:
    _guard_reminder(u, rid)
    return {"ok": True, "reminder": reminders.cancel(rid)}


# ---------- web push (phone lock screen) ----------

@app.get("/api/push/vapid")
def push_vapid(_: None = Depends(auth)) -> dict:
    return {"public_key": push.public_key()}


class PushSub(BaseModel):
    subscription: dict


@app.post("/api/push/subscribe")
def push_subscribe(body: PushSub, _: None = Depends(auth)) -> dict:
    return push.subscribe(body.subscription)


class PushUnsub(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: PushUnsub, _: None = Depends(auth)) -> dict:
    return push.unsubscribe(body.endpoint)


# ---------- calendar (unified tasks timeline) ----------
# Scheduling lives entirely in `tasks` now (Phase 3). The cockpit uses /api/calendar below; the old
# /api/schedule (scheduled_tasks) endpoints are retired.

KINDS = {"seo_report": "SEO & Traffic report"}


class ScheduleBody(BaseModel):
    company: str
    kind: str = "seo_report"
    title: str | None = None
    cadence: str = "weekly"     # daily | weekly | monthly
    weekday: int = 0            # 0=Mon .. 6=Sun
    hour: int = 12
    minute: int = 0
    days: int = 28             # report look-back window


# ---------- unified Calendar (Phase 3.5): ONE timeline over the tasks table ----------
# Three lanes: NOW (un-dated open work piling up) · RECURRING (templates on cadence) · UPCOMING (one-offs).

def _cal_company_name(r: dict) -> str:
    co = store.get_company(r["company_id"])
    return co["name"] if co else ""


def _cal_card(r: dict) -> dict:
    card = {"id": r["id"], "company": _cal_company_name(r), "company_id": r["company_id"],
            "kind": r["kind"], "title": r.get("title") or KINDS.get(r["kind"], r["kind"])}
    k = str(r["kind"])
    # always give content cards a link to the actual content (blog draft / newsletter issue)
    if k in ("blog", "blog_scheduled"):
        wp = db.setting_get(f"wp:{r['id']}") or {}
        if wp.get("preview"):
            card["link"] = wp["preview"]                    # external WordPress draft preview (opens directly)
            card["link_label"] = "View post"
    elif k.startswith("newsletter"):
        if (db.setting_get(f"newsletter:{r['id']}") or {}).get("html"):
            card["link"] = f"/api/content/preview/{r['id']}"   # rendered HTML, fetched in-app (needs auth)
            card["link_label"] = "View issue"
            card["link_fetch"] = True
    return card


@app.get("/api/calendar")
def calendar_view(company: str | None = None, u: dict = Depends(current_user)) -> dict:
    cids = _request_cids(u, company)          # clamped to the user's company scope (a list, or None=all)
    flt = "" if cids is None else " and company_id = any(%s)"
    p: tuple = () if cids is None else (cids,)
    now = db.query("select * from tasks where status in ('awaiting_approval','awaiting_correction') "
                   "and schedule_kind is null" + flt + " order by created_at", p)
    rec = db.query("select * from tasks where schedule_kind='recurring'" + flt
                   + " order by next_run nulls last", p)
    upc = db.query("select * from tasks where schedule_kind='once' and status='scheduled'" + flt
                   + " order by run_at nulls last", p)
    rflt = "" if cids is None else " and (company_id = any(%s) or company_id is null)"
    rp: tuple = () if cids is None else (cids,)
    rems = db.query("select id, title, due_at, target_type, target_id, company_id, (action is not null) as drafts "
                    "from reminders where status in ('pending','snoozed')" + rflt + " order by due_at limit 100", rp)
    rem_cards = []
    for r in rems:
        co = store.get_company(r["company_id"]) if r.get("company_id") else None
        opp, deal_id = None, None
        if r["target_type"] == "deal" and str(r.get("target_id") or "").isdigit():
            deal_id = int(r["target_id"])
            d = db.one("select title from crm_projects where id=%s", (deal_id,))
            opp = d["title"] if d else None
        rem_cards.append({"id": r["id"], "kind": "reminder", "title": r["title"], "company": co["name"] if co else "",
                          "run_at": r["due_at"], "drafts": r["drafts"], "opportunity": opp, "deal_id": deal_id})
    return {
        "now": [{**_cal_card(r), "status": r["status"], "when": r["created_at"]} for r in now],
        "recurring": [{**_cal_card(r), "cadence": r["cadence"], "weekday": r["weekday"], "hour": r["hour"],
                       "minute": r["minute"], "next_run": r["next_run"], "enabled": r["enabled"],
                       "last_run": r["last_run"], "last_status": r["last_status"]} for r in rec],
        "upcoming": [{**_cal_card(r), "run_at": r["run_at"], "status": r["status"]} for r in upc],
        "reminders": rem_cards,
        "kinds": KINDS, "companies": seo_report.available(),
    }


@app.get("/api/content/preview/{tid}")
def content_preview(tid: int, _: None = Depends(auth)):
    """View the actual content behind a calendar/Inbox card: a blog draft (redirect to the WordPress preview)
    or a newsletter issue (its rendered HTML). So every scheduled blog/newsletter is one click from its content."""
    t = store.get_task(tid)
    if not t:
        raise HTTPException(status_code=404, detail="no such item")
    k = str(t["kind"])
    if k.startswith("newsletter"):
        art = db.setting_get(f"newsletter:{tid}") or {}
        if art.get("html"):
            return HTMLResponse(content=art["html"])
    if k in ("blog", "blog_scheduled"):
        wp = db.setting_get(f"wp:{tid}") or {}
        if wp.get("preview"):
            return RedirectResponse(wp["preview"])
    raise HTTPException(status_code=404, detail="no preview available for this item yet")


@app.post("/api/calendar")
def calendar_create(body: ScheduleBody, _: None = Depends(auth)) -> dict:
    """Add a recurring job to the Calendar as a unified task template."""
    if body.kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind {body.kind}")
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=400, detail=f"unknown company {body.company}")
    skill = store.get_skill_by_key(co["id"], engine.REPORT_SKILL_KEY)
    label = seo_report.available().get(body.company, body.company)
    title = body.title or f"{label} — {KINDS[body.kind]}"
    nr = schedule.next_run(body.cadence, body.weekday, body.hour, body.minute)
    req = {"kind": body.kind, "company": body.company, "days": body.days}
    t = db.execute(
        "insert into tasks (company_id,skill_id,kind,request,status,origin,title,schedule_kind,cadence,"
        "weekday,hour,minute,next_run,enabled) values (%s,%s,%s,%s,'scheduled','calendar',%s,'recurring',"
        "%s,%s,%s,%s,true) returning *",
        (co["id"], skill["id"] if skill else None, body.kind, Json(req), title,
         body.cadence, body.weekday, body.hour, body.minute, nr))
    return {"ok": True, "task": t}


@app.post("/api/calendar/{tid}/toggle")
def calendar_toggle(tid: int, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, tid)
    cur = db.one("select enabled from tasks where id=%s and schedule_kind='recurring'", (tid,))
    if not cur:
        raise HTTPException(status_code=404, detail="not found")
    t = db.execute("update tasks set enabled=%s, updated_at=now() where id=%s returning *",
                   (not cur["enabled"], tid))
    return {"ok": True, "task": t}


@app.post("/api/calendar/{tid}/run")
def calendar_run(tid: int, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, tid)
    """Fire a template now — spawns a child instance the engine drafts into the Inbox."""
    child = engine.run_template_now(tid)
    if not child:
        raise HTTPException(status_code=404, detail="not a runnable scheduled task")
    return {"ok": True}


@app.post("/api/calendar/{tid}/bump")
def calendar_bump(tid: int, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, tid)
    """Bump a queued content item (blog, newsletter, any kind) to the FRONT of its company's queue for that
    kind; every other queued item of the same kind shifts back one month."""
    r = contentqueue.bump_to_front(tid)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error", "could not bump"))
    return r


@app.post("/api/calendar/{tid}/move")
def calendar_move(tid: int, dir: str = "up", u: dict = Depends(current_user)) -> dict:
    """Nudge a queued item ONE slot up (earlier) or down (later) in its queue, swapping its publish date with
    the adjacent item. `dir`=up|down."""
    _guard_task(u, tid)
    r = contentqueue.move_one(tid, "down" if dir == "down" else "up")
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error", "could not move"))
    return r


@app.delete("/api/calendar/{tid}")
def calendar_delete(tid: int, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, tid)
    """Delete a scheduled template / one-off (never a normal Inbox task). Detaches any spawned children."""
    db.execute("update tasks set parent_id=null where parent_id=%s", (tid,))
    db.execute("delete from tasks where id=%s and schedule_kind is not null", (tid,))
    return {"ok": True}


@app.post("/api/report/run")
def report_run(company: str, days: int = 28, _: None = Depends(auth)) -> dict:
    """Generate a one-off SEO report for a company now and put it in the Inbox."""
    if company not in seo_report.available():
        raise HTTPException(status_code=400, detail=f"unknown company {company}")
    t = engine.deliver_seo_report(company, days=days)
    return {"ok": True, "task_id": t["id"], "summary": t.get("draft")}


@app.get("/api/report/{tid}/pdf")
def report_pdf(tid: int, _: None = Depends(auth)) -> FileResponse:
    t = store.get_task(tid)
    if not t or t["kind"] != "report":
        raise HTTPException(status_code=404, detail="not a report")
    path = (t.get("request") or {}).get("file")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="report file missing")
    name = os.path.basename(path)
    return FileResponse(path, media_type="application/pdf", filename=name)


@app.get("/api/inbox/history")
def inbox_history(q: str | None = None, start: str | None = None, end: str | None = None,
                  company: str | None = None, limit: int = 80, u: dict = Depends(current_user)) -> list[dict]:
    """Past Inbox items (approved/sent/skipped/seen) with free-text search + a date range. Scoped to the user."""
    where = ["status not in ('awaiting_approval','awaiting_correction','new','drafting')"]
    params: list = []
    cids = _request_cids(u, company)
    if cids is not None:
        where.append("company_id = any(%s)"); params.append(cids)
    if q:
        where.append("(draft ilike %s or request::text ilike %s or kind ilike %s)")
        like = f"%{q}%"; params += [like, like, like]
    if start:
        where.append("updated_at >= %s::date"); params.append(start)
    if end:
        where.append("updated_at < (%s::date + interval '1 day')"); params.append(end)
    params.append(limit)
    rows = db.query(f"select * from tasks where {' and '.join(where)} order by updated_at desc limit %s", tuple(params))
    out = []
    for t in rows:
        co = store.get_company(t["company_id"]); sk = store.get_skill(t["skill_id"])
        req = t.get("request") or {}
        if t["kind"] == "email_reply":
            inq = req.get("inquiry") or {}
            title = "Reply to " + (inq.get("name") or inq.get("email") or "enquiry")
        elif t["kind"] == "report":
            title = req.get("title") or "Report"
        else:
            title = req.get("title") or (t.get("draft") or "").split("\n")[0][:70] or t["kind"]
        out.append({"card": "action", "id": t["id"], "kind": t["kind"], "status": t["status"],
                    "company": co["slug"] if co else None, "company_name": co["name"] if co else "",
                    "skill": sk["name"] if sk else "", "title": title,
                    "summary": (t.get("draft") or "")[:160], "body": (t.get("draft") or "")[:4000],
                    "when": t["updated_at"].isoformat() if t.get("updated_at") else None})
    if not q:   # merge in dismissed/read notifications (info cards) by date; skip on a text search
        cid = None
        if company:
            co2 = store.get_company_by_slug(company); cid = co2["id"] if co2 else -1
        for n in notifications.history(cid, limit):
            out.append({"card": "info", "id": n["id"], "kind": n["category"], "status": n["state"],
                        "company": None, "company_name": "", "skill": "", "title": n["title"],
                        "summary": (n.get("body") or "")[:160], "body": n.get("body") or "",
                        "when": n["fired_at"].isoformat() if n.get("fired_at") else None})
    out.sort(key=lambda x: x.get("when") or "", reverse=True)
    return out[:limit]


class AuthorityBody(BaseModel):
    authority: str   # ask | auto | never


@app.post("/api/skills/{skill_id}/authority")
def set_skill_authority(skill_id: int, body: AuthorityBody, _: None = Depends(auth)) -> dict:
    """Enable auto (earned-autonomy nudge accepted) or pause a lane back to 'ask'."""
    if body.authority not in ("ask", "auto", "never"):
        raise HTTPException(status_code=400, detail="authority must be ask | auto | never")
    return store.set_authority(skill_id, body.authority)


class ThresholdBody(BaseModel):
    threshold: int   # clean approvals in a row required before auto is offered


@app.post("/api/skills/{skill_id}/threshold")
def set_skill_threshold(skill_id: int, body: ThresholdBody, _: None = Depends(auth)) -> dict:
    if body.threshold < 1:
        raise HTTPException(status_code=400, detail="threshold must be at least 1")
    return store.set_threshold(skill_id, body.threshold)


# ---------- per-skill questionnaires (expert "what it handles" + question set; train via Talk) ----------

@app.get("/api/skills/{skill_key}/questionnaire")
def skill_questionnaire(skill_key: str, company: str | None = None, _: None = Depends(auth)) -> dict:
    qn = skillqa.get(skill_key) or {}
    out = {"skill_key": skill_key, "explanation": qn.get("explanation", ""),
           "questions": qn.get("questions", [])}
    if company:
        co = store.get_company_by_slug(company)
        if co:
            out["progress"] = skillqa.progress(co["id"], skill_key)
            sk = store.get_skill_by_key(co["id"], skill_key)
            if sk:
                out["skill"] = {"name": sk["name"], "department": sk.get("department"),
                                "manager": sk.get("manager")}
                uni, loc = store.effective_rules(sk)
                out["rules"] = list(uni) + list(loc)
    return out


class QLock(BaseModel):
    company: str
    q_idx: int
    rule: str
    conversation_id: str | None = None


class QPark(BaseModel):
    company: str
    q_idx: int
    idea: str
    question: str | None = ""
    conversation_id: str | None = None


@app.post("/api/skills/{skill_key}/questionnaire/lock")
def skill_q_lock(skill_key: str, body: QLock, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return skillqa.lock_in(co["id"], skill_key, body.q_idx, body.rule, body.conversation_id)


@app.post("/api/skills/{skill_key}/questionnaire/park")
def skill_q_park(skill_key: str, body: QPark, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return skillqa.park(co["id"], skill_key, body.q_idx, body.idea, body.question or "", body.conversation_id)


# ---------- Manager questionnaires ----------

def _cid(company: str | None) -> int:
    if not company:
        return 0
    co = store.get_company_by_slug(company)
    return co["id"] if co else 0


# ---------- Company Profile wizard ----------

class ProfileStart(BaseModel):
    company: str
    restart: bool = False


class ProfileAnswer(BaseModel):
    company: str
    value: str


@app.get("/api/profile/status")
def profile_status(company: str, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return profile.status(co["id"])


@app.post("/api/profile/start")
def profile_start(body: ProfileStart, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return profile.start(co["id"], body.restart)


@app.post("/api/profile/answer")
def profile_answer(body: ProfileAnswer, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return profile.answer(co["id"], body.value)


@app.get("/api/profile/questions")
def profile_questions(_: None = Depends(auth)) -> list[dict]:
    return profile.questions()


class ProfileSet(BaseModel):
    company: str
    field: str
    value: str


@app.post("/api/profile/set")
def profile_set(body: ProfileSet, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return {"profile": profile.set_field(co["id"], body.field, body.value)}


@app.get("/api/profile/full")
def profile_full(company: str, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return {"company": company, "profile": profile.get(co["id"])}


class OnboardBody(BaseModel):
    slug: str
    name: str | None = None
    kind: str = "owned"


@app.post("/api/company/onboard")
def company_onboard(body: OnboardBody, _: None = Depends(auth)) -> dict:
    """Apply the Company Standard to a (new or existing) company — see docs/COMPANY-STANDARD.md.
    Idempotent: seeds the uniform roster, ensures a profile, refreshes the brand kit, returns a checklist."""
    from . import onboard
    return onboard.onboard_company(body.slug, body.name, kind=body.kind)


@app.get("/api/usage")
def usage(days: int = 7, _: None = Depends(auth)) -> dict:
    """Anthropic spend from the cost log: total + breakdown by model, purpose, day, company."""
    w = "where ts > now() - make_interval(days => %s)"
    p = (days,)
    tot = db.one(f"select coalesce(sum(cost_usd),0) cost, count(*) calls, "
                 f"coalesce(sum(input_tokens),0) in_tok, coalesce(sum(output_tokens),0) out_tok "
                 f"from usage_log {w}", p)
    by_provider = db.query(
        f"select case when model like 'imagen%%' or model like 'gemini%%' then 'Gemini' else 'Anthropic' end "
        f"provider, sum(cost_usd) cost, count(*) calls from usage_log {w} group by provider order by cost desc", p)
    by_model = db.query(f"select model, sum(cost_usd) cost, count(*) calls from usage_log {w} "
                        f"group by model order by cost desc", p)
    by_purpose = db.query(f"select purpose, sum(cost_usd) cost, count(*) calls from usage_log {w} "
                          f"group by purpose order by cost desc limit 25", p)
    by_day = db.query("select ts::date d, sum(cost_usd) cost, count(*) calls from usage_log "
                      "group by d order by d desc limit %s", (days,))
    return {"days": days, "total": tot, "by_provider": by_provider, "by_model": by_model,
            "by_purpose": by_purpose, "by_day": by_day}


# ---------- CRM: contacts / projects / opportunities (read views for the cockpit pages) ----------

_CRM_ORG = {"tabscanner": "Tabscanner", "sensa": "Sensa", "skyvision": "Sky Vision",
            "filmspoke": "FilmSpoke", "snaprewards": "Snap Rewards"}


def _org_like(company, col: str = "organisation"):
    """(sql_fragment, params) filtering by company Organisation label(s). `company` may be a single slug,
    a LIST of slugs (OR-matched, for multi-company scoped users), or ''/all/None (no filter)."""
    if isinstance(company, (list, tuple)):
        slugs = [s for s in company if s]
    elif company and company not in ("all", ""):
        slugs = [company]
    else:
        slugs = []
    if not slugs:
        return "", []
    labels = [_CRM_ORG.get(s, str(s).title()) for s in slugs]
    frag = "(" + " or ".join([f"{col} ilike %s"] * len(labels)) + ")"
    return frag, [f"%{l}%" for l in labels]


@app.get("/api/crm/summary")
def crm_summary(company: str | None = None, _: None = Depends(auth)) -> dict:
    f, p = _org_like(company)
    w = f"where {f}" if f else ""
    contacts = db.one(f"select count(*) n from crm_master {w}", tuple(p))["n"]
    stages = {r["s"]: r["n"] for r in
              db.query(f"select coalesce(nullif(stage,''),'Cold') s, count(*) n from crm_master {w} group by s", tuple(p))}
    pf, pp = _org_like(company, "company")
    pclause = (" and " + pf) if pf else ""
    won = db.one(f"select count(*) n, coalesce(sum(value),0) v from crm_projects where stage = any(%s){pclause}",
                 tuple([crm.WON_STAGES] + pp))
    fc = db.one(f"select count(*) n, coalesce(sum(value),0) v from crm_projects where stage = any(%s){pclause}",
                tuple([crm.FORECAST_STAGES] + pp))
    return {"contacts": contacts, "stages": stages,
            "opportunities": fc["n"], "opportunity_value": float(fc["v"] or 0),
            "projects": won["n"], "project_value": float(won["v"] or 0)}


def _contact_filter(company: str | None, q: str | None, stage: str | None):
    """Shared WHERE + params for the contacts list AND its count, so the number always matches the rows."""
    clauses, params = [], []
    f, p = _org_like(company)
    if f:
        clauses.append(f); params += p
    if q:
        # tokenise: every word must match SOME field (AND across words) so more words = narrower
        for tok in q.split():
            clauses.append("(first_name ilike %s or last_name ilike %s or email ilike %s or company_name ilike %s "
                           "or (coalesce(first_name,'')||' '||coalesce(last_name,'')) ilike %s "
                           "or organisation ilike %s or market ilike %s or note ilike %s)")
            params += [f"%{tok}%"] * 8
    if stage and stage != "All":
        if stage == "Client":
            clauses.append("is_client = true")          # 'Client' is a flag, not a funnel status
        elif stage == "Opted out":
            clauses.append("newsletter_opt_out = true")  # newsletter opt-out flag, not a status
        elif stage == "Do not market":                  # off all outbound marketing, per company
            if company and company not in ("all", ""):
                clauses.append("do_not_market @> %s::jsonb"); params.append(Json([company]))
            else:
                clauses.append("jsonb_array_length(do_not_market) > 0")
        elif stage == "Test group":                      # the company's newsletter/blog test group
            co = store.get_company_by_slug(company) if company and company not in ("", "all") else None
            if company and company not in ("", "all"):
                clauses.append("lower(email) in (select lower(email) from newsletter_test_group "
                               "where active and company_id=%s)")
                params.append(co["id"] if co else -1)
            else:
                clauses.append("lower(email) in (select lower(email) from newsletter_test_group where active)")
        elif stage == "Quote sent":
            clauses.append("quote_sent = true")              # a quote was sent (own field, not a classification)
        elif stage.lower() in crm.ALL_CLASSIFICATIONS:       # classification (lead/vendor/… + past_client/past_opportunity)
            clauses.append("lower(classification) = %s"); params.append(stage.lower())
        else:
            clauses.append("stage = %s"); params.append(stage)
    return (("where " + " and ".join(clauses)) if clauses else ""), params


@app.get("/api/crm/contacts")
def crm_contacts(company: str | None = None, q: str | None = None, stage: str | None = None,
                 limit: int = 50, offset: int = 0, u: dict = Depends(current_user)) -> list[dict]:
    company = _scoped_companies(u, company)
    where, params = _contact_filter(company, q, stage)
    # relevance: name OR email that STARTS WITH the query floats to the top as you type
    qq = (q or "").strip().lower()
    if qq:
        order = ("order by ((coalesce(lower(first_name),'')||' '||coalesce(lower(last_name),'')) like %s "
                 "or lower(email) like %s) desc, is_client desc, first_name nulls last")
        oparams = [qq + "%", qq + "%"]
    else:
        order, oparams = "order by is_client desc, first_name nulls last", []
    return db.query(
        "select first_name, last_name, email, organisation, company_name, job_title, stage, tier, "
        f"is_client, newsletter_opt_out, do_not_market, classification, quote_sent, lead_source from crm_master {where} "
        f"{order} limit %s offset %s",
        tuple(list(params) + oparams + [limit, offset]))


@app.get("/api/crm/contacts/count")
def crm_contacts_count(company: str | None = None, q: str | None = None, stage: str | None = None,
                       u: dict = Depends(current_user)) -> dict:
    """Total contacts matching the SAME filters as /api/crm/contacts (for the count shown above the list)."""
    company = _scoped_companies(u, company)
    where, params = _contact_filter(company, q, stage)
    r = db.one(f"select count(*) n from crm_master {where}", tuple(params))
    return {"count": int(r["n"]) if r and r.get("n") is not None else 0}


@app.get("/api/crm/contact")
def crm_contact(email: str | None = None, id: int | None = None, u: dict = Depends(current_user)) -> dict:
    # Load by id (harvested LinkedIn contacts have NO email) or by email (the legacy 33k).
    r = (db.one("select * from crm_master where id=%s", (id,)) if id is not None
         else db.one("select * from crm_master where lower(email)=lower(%s) limit 1", (email or "",)))
    if r and u.get("companies") is not None:          # scoped user: only contacts owned by one of their companies
        orgs = {p.strip().lower() for p in ((r.get("organisation") or "")).split(",") if p.strip()}
        allowed_labels = {_CRM_ORG.get(s, s).lower() for s in (u.get("companies") or [])} | set(u.get("companies") or [])
        if not (orgs & allowed_labels):
            raise HTTPException(status_code=403, detail="not in your company scope")
    if r and r.get("email"):
        qs = db.setting_get(f"qual:email:{(r.get('email') or '').strip().lower()}")
        if qs:
            r = {**r, "qual_suggest": qs}
    return r or {}


@app.get("/api/crm/projects")
def crm_projects(company: str | None = None, u: dict = Depends(current_user)) -> dict:
    company = _scoped_companies(u, company)
    f, p = _org_like(company, "company")
    conds = ["stage = any(%s)"]; params: list = [crm.WON_STAGES]
    if f:
        conds.append(f); params += p
    w = "where " + " and ".join(conds)
    rows = db.query(f"select id, company, title, contact_email, value, currency, stage, owner from crm_projects {w} "
                    "order by case stage when 'Booked' then 1 when 'Production' then 2 when 'Recurring' then 3 "
                    "when 'Delivered' then 4 when 'Final Payment' then 5 when 'Close & review' then 6 else 7 end, "
                    "value desc nulls last", tuple(params))
    groups = db.query(f"select stage, count(*) n, coalesce(sum(value),0) v from crm_projects {w} group by stage", tuple(params))
    total = db.one(f"select coalesce(sum(value),0) v, count(*) n from crm_projects {w}", tuple(params))
    cur = db.query(f"select coalesce(nullif(currency,''),'AED') c, coalesce(sum(value),0) v from crm_projects {w} "
                   "and value is not null group by c", tuple(params))
    return {"projects": rows, "groups": {g["stage"]: {"n": g["n"], "value": float(g["v"] or 0)} for g in groups},
            "total_value": float(total["v"] or 0), "count": total["n"],
            "currencies": {r["c"]: float(r["v"] or 0) for r in cur if float(r["v"] or 0) > 0}}


@app.get("/api/crm/project")
def crm_project(id: int, u: dict = Depends(current_user)) -> dict:
    p = db.one("select * from crm_projects where id=%s", (id,))
    if not p:
        return {}
    if u.get("companies") is not None:           # scoped user: only deals owned by one of their companies
        allowed = {_CRM_ORG.get(s, s).lower() for s in (u.get("companies") or [])} | set(u.get("companies") or [])
        if (p.get("company") or "").lower() not in allowed:
            raise HTTPException(status_code=403, detail="not in your company scope")
    if p.get("account_id"):                  # company-mediated: a deal's people = its client company's contacts
        p["account"] = db.one("select id, name, domain from crm_accounts where id=%s", (p["account_id"],))
        p["account_contacts"] = db.query(
            "select first_name, last_name, email, job_title, stage, is_client from crm_master "
            "where account_id=%s order by first_name nulls last", (p["account_id"],))
    # opportunity follow-up schedule: its open reminders (manual or auto) — automation/next_followup are columns on p
    p["reminders"] = db.query(
        "select id, title, due_at, recurrence, priority, (action is not null) as drafts, status from reminders "
        "where target_type='deal' and target_id=%s and status in ('pending','snoozed') order by due_at", (str(id),))
    return p


class NewContactBody(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str
    account_id: int | None = None
    company: str | None = None       # which of YOUR businesses (slug)
    phone: str | None = None
    job_title: str | None = None
    stage: str = "Cold"


@app.post("/api/crm/contacts/new")
def crm_create_contact(body: NewContactBody, _: None = Depends(auth)) -> dict:
    if not (body.email or "").strip():
        raise HTTPException(status_code=400, detail="email required")
    return crm.create_contact(body.first_name, body.last_name, body.email, body.account_id,
                              body.company, body.phone, body.job_title, body.stage)


class NewAccountBody(BaseModel):
    name: str
    domain: str | None = None
    website: str | None = None
    phone: str | None = None
    company: str | None = None      # the business it's created under (so it shows in that business's list)


@app.post("/api/crm/accounts/new")
def crm_create_account(body: NewAccountBody, _: None = Depends(auth)) -> dict:
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="name required")
    return crm.create_account(body.name, body.domain, body.website, body.phone, body.company)


class NewDealBody(BaseModel):
    company: str                     # your business slug (sensa/tabscanner/...)
    title: str
    value: float | None = None
    currency: str = "AED"
    stage: str = "Opportunity"
    account_id: int | None = None


@app.post("/api/crm/deals/new")
def crm_create_deal(body: NewDealBody, _: None = Depends(auth)) -> dict:
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail="title required")
    if body.stage not in crm.DEAL_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {crm.DEAL_STAGES}")
    try:
        return crm.create_deal(body.company, body.title, body.value, body.currency, body.stage, body.account_id)
    except crm.DuplicateDeal as e:
        raise HTTPException(status_code=409, detail=str(e))
    except crm.DealNeedsCompany as e:
        raise HTTPException(status_code=400, detail=str(e))


class ContactEditBody(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    job_title: str | None = None
    phone: str | None = None
    new_email: str | None = None


@app.post("/api/crm/contact/update")
def crm_update_contact(body: ContactEditBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    fields = {"first_name": body.first_name, "last_name": body.last_name,
              "job_title": body.job_title, "phone": body.phone}
    if body.new_email:
        fields["email"] = body.new_email.strip().lower()
    try:
        r = crm.update_contact(body.email, **fields)
    except Exception as e:  # noqa: BLE001 — e.g. email collides with the unique index
        raise HTTPException(status_code=400, detail="that email is already used by another contact")
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


@app.get("/api/crm/contact/companies")
def crm_contact_companies(email: str | None = None, id: int | None = None, _: None = Depends(auth)) -> dict:
    """Per-company membership / subscriber / test-group state for a contact (over the live companies list)."""
    return crm.contact_company_state(email, id)


class CompanyToggleBody(BaseModel):
    email: str
    company: str          # company slug
    field: str            # 'member' | 'test_group'
    on: bool


@app.post("/api/crm/contact/company-toggle")
def crm_contact_company_toggle(body: CompanyToggleBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    """Toggle a contact's membership of, or test-group inclusion for, one company. The test group is the
    SAME table the [TEST] send reads live, so a change here is in effect immediately."""
    from . import newsletter
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s) limit 1", (body.email,))
    if not c:
        raise HTTPException(status_code=404, detail="contact not found")
    if body.field == "member":
        crm.set_membership(body.email, body.company, body.on)
        if not body.on:                       # leaving the company also leaves its test group
            newsletter.set_test_group(body.email, co["id"], False)
    elif body.field == "test_group":
        if body.on:
            crm.set_membership(body.email, body.company, True)   # test-group implies membership
        nm = " ".join(filter(None, [c.get("first_name"), c.get("last_name")])).strip() or None
        newsletter.set_test_group(body.email, co["id"], body.on, name=nm)
    else:
        raise HTTPException(status_code=400, detail="field must be 'member' or 'test_group'")
    return crm.contact_company_state(body.email)


class DealEditBody(BaseModel):
    title: str | None = None
    value: float | None = None
    currency: str | None = None


@app.post("/api/crm/project/{id}/update")
def crm_update_deal(id: int, body: DealEditBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    r = crm.update_deal(id, title=body.title, value=body.value, currency=body.currency)
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


@app.delete("/api/crm/contact")
def crm_delete_contact(email: str, _: None = Depends(auth)) -> dict:
    crm.delete_contact(email)
    return {"ok": True}


@app.delete("/api/crm/account/{id}")
def crm_delete_account(id: int, _: None = Depends(auth)) -> dict:
    crm.delete_account(id)
    return {"ok": True}


@app.delete("/api/crm/project/{id}")
def crm_delete_deal(id: int, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    crm.delete_deal(id)
    return {"ok": True}


class AssignAccountBody(BaseModel):
    account_id: int | None = None
    email: str | None = None


@app.post("/api/crm/contact/company")
def crm_contact_company(body: AssignAccountBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    if not body.email:
        raise HTTPException(status_code=400, detail="email required")
    r = crm.set_contact_account(body.email, body.account_id)
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


@app.post("/api/crm/project/{id}/account")
def crm_deal_company(id: int, body: AssignAccountBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    r = crm.set_deal_account(id, body.account_id)
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


class ProjectStageBody(BaseModel):
    stage: str


@app.post("/api/crm/project/{id}/stage")
def crm_project_stage(id: int, body: ProjectStageBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    if body.stage not in crm.DEAL_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {crm.DEAL_STAGES}")
    r = crm.set_project_stage(id, body.stage)
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


class AutomationBody(BaseModel):
    mode: str | None = None          # 'auto' (re-arm the cadence) | 'manual' | None/'off' (stop)


@app.post("/api/crm/project/{id}/automation")
def crm_project_automation(id: int, body: AutomationBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    """Set an opportunity's follow-up automation. 'auto' arms the company cadence; 'manual'/off stops it."""
    r = crm.set_opportunity_automation(id, body.mode)
    if not r:
        raise HTTPException(status_code=404, detail="no such opportunity")
    return r


class DealReminderBody(BaseModel):
    when: str                        # natural language ("in 3 days") or ISO
    note: str
    recurrence: str = "none"         # none|daily|weekly|monthly|weekday|custom
    draft: bool = False              # true = spawn a drafted follow-up on fire; false = a plain nudge


@app.post("/api/crm/project/{id}/reminder")
def crm_project_reminder(id: int, body: DealReminderBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    """Add a MANUAL reminder to an opportunity (deal-linked) — surfaces in the Inbox + Calendar at its time."""
    from . import reminders as _rem
    p = db.one("select company, title from crm_projects where id=%s", (id,))
    if not p:
        raise HTTPException(status_code=404, detail="no such opportunity")
    when = _rem.parse_when(body.when)
    if not when:
        try:
            from datetime import datetime as _dt
            when = _dt.fromisoformat(body.when)
        except Exception:
            raise HTTPException(status_code=400, detail="couldn't understand the time — try 'in 3 days' or a date")
    slug = crm._slug_for_org(p["company"])
    co = store.get_company_by_slug(slug)
    action = ({"company": slug, "skill": "sales-first-response", "kind": "email_reply", "brief": body.note}
              if body.draft else None)
    r = _rem.create(body.note, when, company_id=(co["id"] if co else None), target_type="deal", target_id=id,
                    recurrence=body.recurrence, action=action)
    return {"ok": True, "reminder": {"id": r["id"], "title": r["title"], "due_at": r["due_at"]}}


@app.get("/api/crm/opportunities")
def crm_opportunities(company: str | None = None, u: dict = Depends(current_user)) -> dict:
    """Forecast deals (Opportunity/Quote) with values + a running total — same deal records as Projects,
    just on the pre-Booked side of the line. Lost deals come back separately (not in the forecast total)."""
    company = _scoped_companies(u, company)
    f, p = _org_like(company, "company")
    base = ["stage = any(%s)"]
    if f:
        base.append(f)

    def run(stages):
        params = [stages] + (p if f else [])
        return db.query("select id, company, title, contact_email, value, currency, stage, owner "
                        f"from crm_projects where {' and '.join(base)} order by value desc nulls last, id", tuple(params))
    rows = run(crm.FORECAST_STAGES)
    lost = run([crm.LOST_STAGE])
    fparams = [crm.FORECAST_STAGES] + (p if f else [])
    total = db.one(f"select coalesce(sum(value),0) v, count(*) n from crm_projects where {' and '.join(base)}", tuple(fparams))
    cur = db.query(f"select coalesce(nullif(currency,''),'AED') c, coalesce(sum(value),0) v from crm_projects "
                   f"where {' and '.join(base)} and value is not null group by c", tuple(fparams))
    return {"opportunities": rows, "lost": lost, "total_value": float(total["v"] or 0), "count": total["n"],
            "currencies": {r["c"]: float(r["v"] or 0) for r in cur if float(r["v"] or 0) > 0}}


class OptOutBody(BaseModel):
    email: str
    on: bool = True


@app.post("/api/crm/contact/optout")
def crm_contact_optout(body: OptOutBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    r = crm.set_newsletter_opt_out(body.email, body.on)
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


class DoNotMarketBody(BaseModel):
    email: str
    on: bool = True
    company: str | None = None   # a company slug (per-company), or null/''/'all' for every company


@app.post("/api/crm/contact/donotmarket")
def crm_contact_donotmarket(body: DoNotMarketBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    r = crm.set_do_not_market(body.email, body.company, body.on)
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


class RegistrationBody(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    company_name: str | None = None
    company: str | None = None
    phone: str | None = None


@app.post("/api/intake/registration")
def intake_registration(body: RegistrationBody, token: str = "", x_token: str = Header(default="")) -> dict:
    """Public webhook for website/store registrations. Auth = a PER-SOURCE token (?token=... or X-Token
    header), NOT the operator passcode. The token maps to {company, source}, so one endpoint serves many
    registration sources (Tabscanner site, Snap Rewards store, etc.)."""
    raw = db.setting_get("intake_tokens")
    # `raw` is a dict when stored as jsonb (setting_set) or a JSON string (legacy) — handle both.
    tokens = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
    legacy = db.setting_get("registration_intake_token")        # back-compat: original single-token URL
    if legacy and legacy not in tokens:
        tokens[legacy] = {"company": "tabscanner", "source": "Tabscanner registrations"}
    cfg = tokens.get(token) or tokens.get(x_token)
    if not cfg:
        raise HTTPException(status_code=403, detail="invalid token")
    reg = {"email": body.email, "first_name": body.first_name, "last_name": body.last_name,
           "name": body.name, "company_name": body.company_name or body.company, "phone": body.phone}
    status, email = crm.add_registration(reg, company=cfg["company"], source=cfg["source"],
                                         waitlist=bool(cfg.get("waitlist")))
    return {"ok": status in ("added", "matched"), "status": status, "email": email}


class EnquiryBody(BaseModel):
    email: str
    name: str = ""
    company_name: str = ""
    company: str = ""        # client company text (kept for parity with the form fields)
    phone: str = ""
    message: str = ""
    subject: str = ""


@app.post("/api/intake/enquiry")
def intake_enquiry_webhook(body: EnquiryBody, token: str = "", x_token: str = Header(default="")) -> dict:
    """Public webhook for website CONTACT-FORM enquiries (direct POST, no email round-trip). Auth = a PER-SOURCE
    token (?token=... or X-Token) -> {company, draft}. The enquiry is TRIAGED server-side (sales-triage): a genuine
    one becomes a CRM lead (+ optional drafted reply); spam/junk is filed but never CRM'd. Always 200 to the site."""
    raw = db.setting_get("enquiry_tokens")
    tokens = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
    cfg = tokens.get(token) or tokens.get(x_token)
    if not cfg:
        raise HTTPException(status_code=403, detail="invalid token")
    inq = {"name": body.name, "email": body.email, "phone": body.phone,
           "subject": body.subject or "Website enquiry", "message": body.message,
           "company_name": body.company_name or body.company}
    res = engine.intake_enquiry(cfg["company"], inq, draft=bool(cfg.get("draft", True)))
    return {"ok": True, "captured": bool(res.get("captured"))}


class ContactStageBody(BaseModel):
    email: str
    stage: str


class ContactClassifyBody(BaseModel):
    email: str
    classification: str | None = None


@app.post("/api/crm/contact/classify")
def crm_contact_classify(body: ContactClassifyBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    r = crm.set_classification(body.email, body.classification)
    if not r:
        raise HTTPException(status_code=404, detail="contact not found, or invalid classification")
    return r


class QualifyBody(BaseModel):
    email: str
    company: str                    # which of YOUR businesses (slug)
    title: str | None = None


@app.post("/api/crm/contact/qualify")
def crm_contact_qualify(body: QualifyBody, u: dict = Depends(current_user)) -> dict:
    """Qualify a lead -> create an Opportunity (linked to their org) + start the Auto follow-up cadence."""
    _guard_contact(u, body.email)
    if u.get("companies") is not None and body.company not in (u.get("companies") or []):
        raise HTTPException(status_code=403, detail="not in your company scope")
    d = crm.qualify_opportunity(body.email, body.company, body.title)
    if not d:
        raise HTTPException(status_code=404, detail="contact not found")
    return {"ok": True, "opportunity": {"id": d["id"], "title": d["title"], "stage": d["stage"]}}


@app.post("/api/crm/contact/disqualify")
def crm_contact_disqualify(body: ContactClassifyBody, u: dict = Depends(current_user)) -> dict:
    """Mark a lead NOT qualified (no opportunity). Only `email` is used."""
    _guard_contact(u, body.email)
    r = crm.disqualify(body.email)
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


@app.post("/api/crm/contact/stage")
def crm_contact_stage(body: ContactStageBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    if body.stage not in crm.CONTACT_STAGES:
        raise HTTPException(status_code=400, detail=f"status must be one of {crm.CONTACT_STAGES}")
    r = crm.set_contact_stage(body.email, body.stage)
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


class NoteBody(BaseModel):
    note: str
    email: str | None = None     # contact note


@app.post("/api/crm/contact/note")
def crm_contact_note(body: NoteBody, u: dict = Depends(current_user)) -> dict:
    _guard_contact(u, body.email)
    if not (body.note or "").strip() or not body.email:
        raise HTTPException(status_code=400, detail="email and note required")
    r = crm.add_contact_note(body.email, body.note.strip())
    if not r:
        raise HTTPException(status_code=404, detail="contact not found")
    return r


@app.post("/api/crm/project/{id}/note")
def crm_project_note(id: int, body: NoteBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    if not (body.note or "").strip():
        raise HTTPException(status_code=400, detail="note required")
    r = crm.add_project_note(id, body.note.strip())
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


class DealContactBody(BaseModel):
    email: str
    role: str = ""
    primary: bool = False


@app.post("/api/crm/project/{id}/contacts")
def crm_deal_add_contact(id: int, body: DealContactBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    if not (body.email or "").strip():
        raise HTTPException(status_code=400, detail="email required")
    r = crm.add_deal_contact(id, body.email.strip(), body.role, body.primary)
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


@app.post("/api/crm/project/{id}/contacts/primary")
def crm_deal_set_primary(id: int, body: DealContactBody, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    r = crm.set_deal_primary(id, body.email.strip())
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


@app.delete("/api/crm/project/{id}/contacts")
def crm_deal_remove_contact(id: int, email: str, u: dict = Depends(current_user)) -> dict:
    _guard_deal(u, id)
    r = crm.remove_deal_contact(id, email)
    if not r:
        raise HTTPException(status_code=404, detail="deal not found")
    return r


def _accounts_where(q: str | None, company: str | None):
    """Shared WHERE + params for the organisations list AND its count, so the number matches the rows."""
    conds, params = [], []
    if q:
        # space/punctuation-insensitive, tokenised: strip non-alphanumerics from BOTH the query and the
        # name, so "filmquip" matches "Film Quip Media" and word order doesn't matter. Each token must hit.
        toks = [t for t in (re.sub(r"[^a-z0-9]", "", w.lower()) for w in q.split()) if t]
        if toks:
            for t in toks:
                conds.append("regexp_replace(lower(a.name), '[^a-z0-9]', '', 'g') like %s")
                params.append(f"%{t}%")
        else:
            conds.append("a.name ilike %s"); params.append(f"%{q}%")
    cos = company if isinstance(company, (list, tuple)) else ([] if (not company or company in ("all", "")) else [company])
    cos = [c for c in cos if c]
    if cos:
        ors = []
        for c in cos:                                 # OR across the user's companies (multi-company scope)
            label = _CRM_ORG.get(c, str(c).title())
            ors.append("(exists (select 1 from crm_projects p where p.account_id=a.id and p.company ilike %s) "
                       "or exists (select 1 from crm_master m where m.account_id=a.id and m.organisation ilike %s) "
                       "or a.company ilike %s)")
            params += [f"%{label}%", f"%{label}%", f"%{label}%"]
        conds.append("(" + " or ".join(ors) + ")")
    return (("where " + " and ".join(conds)) if conds else ""), params


@app.get("/api/crm/accounts")
def crm_accounts(q: str | None = None, company: str | None = None, limit: int | None = None,
                 offset: int = 0, u: dict = Depends(current_user)) -> list[dict]:
    """The client-organisation directory. Scoped to a business when `company` is given (orgs that have a deal
    owned by that business OR a contact tagged to it); global when omitted. `limit` paginates the LIST view;
    omit it (the create/link dropdowns) to get every match."""
    company = _scoped_companies(u, company)
    where, params = _accounts_where(q, company)
    qparams = [crm.WON_STAGES] + params
    # relevance: exact (normalised) match first, then prefix, then alphabetical -> best match rises as you type
    nq = re.sub(r"[^a-z0-9]", "", (q or "").lower())
    if nq:
        order = ("order by (regexp_replace(lower(a.name),'[^a-z0-9]','','g')=%s) desc, "
                 "(regexp_replace(lower(a.name),'[^a-z0-9]','','g') like %s) desc, a.name")
        qparams += [nq, nq + "%"]
    else:
        order = "order by a.name"
    tail = ""
    if limit is not None:
        tail = " limit %s offset %s"; qparams += [limit, offset]
    return db.query(
        "select a.id, a.name, a.domain, "
        "(select count(*) from crm_master m where m.account_id=a.id) contacts, "
        "(select count(*) from crm_projects p where p.account_id=a.id) deals, "
        "(select coalesce(sum(value),0) from crm_projects p where p.account_id=a.id and p.stage = any(%s)) won_value "
        f"from crm_accounts a {where} {order}{tail}", tuple(qparams))


@app.get("/api/crm/accounts/count")
def crm_accounts_count(q: str | None = None, company: str | None = None, u: dict = Depends(current_user)) -> dict:
    company = _scoped_companies(u, company)
    where, params = _accounts_where(q, company)
    return {"count": db.one(f"select count(*) c from crm_accounts a {where}", tuple(params))["c"]}


@app.get("/api/crm/account-dupes")
def crm_account_dupes(_: None = Depends(auth)) -> dict:
    """Domain-based duplicate-organisation candidates for the review flow (own-brand + free-email aware)."""
    g = crm.account_dupe_groups()
    return {"groups": g, "auto_safe": sum(1 for x in g if x["auto_safe"]), "total": len(g)}


class MergeBody(BaseModel):
    winner_id: int
    loser_ids: list[int]
    reason: str | None = ""


@app.post("/api/crm/accounts/merge")
def crm_accounts_merge(body: MergeBody, _: None = Depends(auth)) -> dict:
    return crm.merge_accounts(body.winner_id, body.loser_ids, body.reason or "manual merge")


@app.post("/api/crm/account-merge/{mid}/reverse")
def crm_account_merge_reverse(mid: int, _: None = Depends(auth)) -> dict:
    return crm.reverse_account_merge(mid)


@app.get("/api/crm/account")
def crm_account(id: int, _: None = Depends(auth)) -> dict:
    a = db.one("select * from crm_accounts where id=%s", (id,))
    if not a:
        return {}
    a["contacts"] = db.query("select first_name, last_name, email, job_title, stage, is_client "
                             "from crm_master where account_id=%s order by first_name nulls last", (id,))
    a["deals"] = db.query("select id, title, company, value, currency, stage from crm_projects "
                          "where account_id=%s order by value desc nulls last, id", (id,))
    return a


@app.post("/api/crm/account/{id}/note")
def crm_account_note(id: int, body: NoteBody, _: None = Depends(auth)) -> dict:
    if not (body.note or "").strip():
        raise HTTPException(status_code=400, detail="note required")
    r = crm.add_account_note(id, body.note.strip())
    if not r:
        raise HTTPException(status_code=404, detail="account not found")
    return r


class RenameBody(BaseModel):
    name: str


@app.post("/api/crm/account/{id}/rename")
def crm_account_rename(id: int, body: RenameBody, _: None = Depends(auth)) -> dict:
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="name required")
    r = crm.rename_account(id, body.name.strip())
    if not r:
        raise HTTPException(status_code=404, detail="account not found")
    return r


@app.get("/api/gmail/status")
def gmail_status(_: None = Depends(auth)) -> dict:
    return {"connected": gmail.connected(), "account": db.setting_get("gmail_account"),
            "send_account": db.setting_get("gmail_send_account")}


@app.get("/api/gmail/inquiries")
def gmail_inquiries(days: int = 7, _: None = Depends(auth)) -> dict:
    """Read-only: the Tabscanner contact-form inquiries from the last `days` days."""
    try:
        items = gmail.list_inquiries(days=days)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
    return {"account": db.setting_get("gmail_account"), "days": days,
            "count": len(items), "inquiries": items}


@app.post("/api/gmail/intake")
def gmail_intake(days: int = 7, _: None = Depends(auth)) -> dict:
    """Pull new enquiries now: add each to the CRM and queue a drafted reply for approval (deduped)."""
    out = engine.poll_inquiries_window(days=days)
    engine.process_new_tasks()   # draft the queued replies immediately so they appear in the Inbox
    return out


@app.get("/api/questionnaire/areas")
def q_areas(_: None = Depends(auth)) -> list[dict]:
    return questionnaire.areas()


@app.get("/api/questionnaire/status")
def q_status(area: str, company: str | None = None, _: None = Depends(auth)) -> dict:
    return questionnaire.open_area(area, _cid(company))


class QStart(BaseModel):
    area: str
    tier: str
    company: str | None = None
    restart: bool = False


@app.post("/api/questionnaire/start")
def q_start(body: QStart, _: None = Depends(auth)) -> dict:
    if body.tier not in questionnaire.TIERS:
        raise HTTPException(status_code=400, detail="unknown tier")
    cid = _cid(body.company)
    if questionnaire.TIERS[body.tier]["scope"] == "company" and not cid:
        raise HTTPException(status_code=400, detail="pick a company for a Deeper or Deepest dive")
    return questionnaire.start(body.area, body.tier, cid, body.restart)


class QAnswer(BaseModel):
    run_id: int
    answer: str


@app.post("/api/questionnaire/answer")
def q_answer(body: QAnswer, _: None = Depends(auth)) -> dict:
    return questionnaire.answer(body.run_id, body.answer)


class QRun(BaseModel):
    run_id: int


@app.post("/api/questionnaire/distill")
def q_distill(body: QRun, _: None = Depends(auth)) -> dict:
    return questionnaire.distill(body.run_id)


@app.post("/api/questionnaire/review")
def q_review(body: QStart, _: None = Depends(auth)) -> dict:
    """Re-open a completed run's distilled rules for review + save (without re-answering)."""
    if body.tier not in questionnaire.TIERS:
        raise HTTPException(status_code=400, detail="unknown tier")
    cid = 0 if questionnaire.TIERS[body.tier]["scope"] == "universal" else _cid(body.company)
    run = db.one("select id from questionnaire_runs where department=%s and tier=%s and company_id=%s",
                 (body.area, body.tier, cid))
    if not run:
        raise HTTPException(status_code=404, detail="no run to review yet")
    d = questionnaire.distill(run["id"])
    d["run_id"] = run["id"]
    return d


class QApply(BaseModel):
    company: str | None = None
    area: str
    rules: list[dict] = []      # [{skill, rule, scope, override_of?}]
    roadmap: list[str] = []


@app.post("/api/questionnaire/apply")
def q_apply(body: QApply, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company) if body.company else None
    saved = 0
    for r in body.rules:
        rule, skill_key, scope = r.get("rule"), r.get("skill"), r.get("scope")
        if not rule:
            continue
        if scope == "universal" and skill_key:
            if rule in (store.get_universal_rules(skill_key) or []):
                continue                                              # already there — don't duplicate
            store.add_universal_rule(skill_key, rule)
            saved += 1
        elif co and skill_key:
            sk = store.get_skill_by_key(co["id"], skill_key)
            if sk:
                if rule in (sk["rules"] or []):
                    continue
                if r.get("override_of"):
                    store.add_override(sk["id"], r["override_of"])   # supersede the universal here
                store.add_rule(sk["id"], rule)
                saved += 1
    road = 0
    if body.roadmap and co:
        pl = store.get_skill_by_key(co["id"], "roadmap-ideas-parking-lot")
        if pl:
            for item in body.roadmap:
                store.add_rule(pl["id"], item)
                road += 1
    return {"ok": True, "rules_saved": saved, "roadmap_added": road}


@app.get("/api/tasks/{task_id}")
def task(task_id: int, _: None = Depends(auth)) -> dict:
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="no such task")
    t["wp"] = db.setting_get(f"wp:{task_id}")
    return t


@app.get("/api/decisions")
def decisions(limit: int = 50, company: str | None = None, _: None = Depends(auth)) -> list[dict]:
    if company:
        co = store.get_company_by_slug(company)
        return db.query(
            "select d.* from decisions d join tasks t on t.id = d.task_id "
            "where t.company_id = %s order by d.id desc limit %s",
            (co["id"] if co else -1, limit),
        )
    return db.query("select * from decisions order by id desc limit %s", (limit,))


# ---------- write actions ----------

class NewTask(BaseModel):
    company: str
    skill: str
    kind: str = "content"
    brief: str
    count: int = 1   # blog ideation: how many concepts to propose (1 -> one title+summary, 6 -> six)


@app.post("/api/tasks")
def create_task(body: NewTask, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    sk = store.get_skill_by_key(co["id"], body.skill)
    if not sk:
        raise HTTPException(status_code=404, detail="no such skill")
    req = {"brief": body.brief}
    if body.kind == "blog":
        req["count"] = max(1, min(int(body.count or 1), 10))   # N blog concepts to propose
    t = store.create_task(co["id"], sk["id"], body.kind, req)
    return {"ok": True, "task": t}  # the engine service will draft it on its next poll


class Correction(BaseModel):
    text: str


@app.post("/api/tasks/{task_id}/approve")
def approve(task_id: int, x_stepup: str = Header(default=""), u: dict = Depends(current_user)) -> dict:
    _guard_task(u, task_id)
    return engine.approve_task(task_id, stepup_token=x_stepup or None)


@app.post("/api/tasks/{task_id}/skip")
def skip(task_id: int, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, task_id)
    return engine.skip_task(task_id)


@app.post("/api/tasks/{task_id}/correct")
def correct(task_id: int, body: Correction, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, task_id)
    return engine.correct_task(task_id, body.text)


@app.get("/api/tasks/{task_id}/pending-rule")
def task_pending_rule(task_id: int, _: None = Depends(auth)) -> dict:
    """Cockpit polls this after a correction: the rule the background inference proposed, if any."""
    return engine.pending_rule(task_id)


@app.get("/api/rules/pending")
def rules_pending(_: None = Depends(auth)) -> dict:
    """EVERY un-decided rule proposal across all companies — the cockpit surfaces these as standalone
    'confirm this rule' cards so a taught rule is never silently lost, even if its task has been archived."""
    return {"pending": engine.pending_rules()}


class RuleDecision(BaseModel):
    add: bool
    scope: str = "company"   # 'company' = this company only | 'universal' = every company


@app.post("/api/tasks/{task_id}/rule")
def task_rule(task_id: int, body: RuleDecision, u: dict = Depends(current_user)) -> dict:
    _guard_task(u, task_id)
    """Confirm/dismiss the rule Cortex inferred from a correction, at the owner's chosen scope (from the cockpit)."""
    return engine.decide_rule(task_id, body.add, body.scope)


class SendConfirm(BaseModel):
    count: int


@app.post("/api/tasks/{task_id}/confirm-send")
def task_confirm_send(task_id: int, body: SendConfirm, x_stepup: str = Header(default=""),
                      u: dict = Depends(current_user)) -> dict:
    _guard_task(u, task_id)
    """Confirm a newsletter card with the exact count: Stage 2 schedules for the 1st, Stage 3 sends."""
    return engine.confirm_send_task(task_id, body.count, stepup_token=x_stepup or None)


# ---------- WebAuthn (biometric step-up for public approvals) ----------

@app.get("/api/webauthn/registered")
def wa_registered(_: None = Depends(auth)) -> dict:
    return {"registered": webauthn_auth.is_registered()}


@app.post("/api/webauthn/register/options")
def wa_register_options(_: None = Depends(auth)) -> dict:
    return webauthn_auth.register_options()


@app.post("/api/webauthn/register/verify")
def wa_register_verify(credential: dict = Body(...), _: None = Depends(auth)) -> dict:
    return webauthn_auth.register_verify(credential)


@app.post("/api/webauthn/auth/options")
def wa_auth_options(_: None = Depends(auth)) -> dict:
    return webauthn_auth.auth_options()


@app.post("/api/webauthn/auth/verify")
def wa_auth_verify(credential: dict = Body(...), _: None = Depends(auth)) -> dict:
    return webauthn_auth.auth_verify(credential)


@app.get("/api/stepup/status")
def stepup_status(_: None = Depends(auth)) -> dict:
    return webauthn_auth.status()


class PinBody(BaseModel):
    pin: str


@app.post("/api/stepup/pin/verify")
def stepup_pin_verify(body: PinBody, _: None = Depends(auth)) -> dict:
    """Confirm a public approval with the SAME 4-digit cockpit PIN (pin_hash)."""
    h = db.setting_get("pin_hash")
    if h and hmac.compare_digest(h, _pin_hash((body.pin or "").strip())):
        return {"ok": True, "stepup_token": webauthn_auth._issue_stepup()}
    return {"ok": False, "error": "wrong PIN"}


class PauseToggle(BaseModel):
    paused: bool


@app.get("/api/newsletter/status")
def newsletter_status_get(_: None = Depends(auth)) -> dict:
    return engine.newsletter_status()


@app.post("/api/newsletter/pause")
def newsletter_pause_set(body: PauseToggle, _: None = Depends(auth)) -> dict:
    """Emergency stop for all newsletter sending (pauses in-flight drips + scheduled/auto sends)."""
    return engine.set_newsletter_paused(body.paused)


@app.get("/api/email/status")
def email_status_get(_: None = Depends(auth)) -> dict:
    return engine.email_status()


@app.post("/api/email/pause")
def email_pause_set(body: PauseToggle, _: None = Depends(auth)) -> dict:
    """Emergency stop for all outbound Gmail email (replies from official addresses)."""
    return engine.set_email_sending_paused(body.paused)


class AutoToggle(BaseModel):
    company_id: int
    on: bool


@app.post("/api/newsletter/auto")
def newsletter_auto_set(body: AutoToggle, _: None = Depends(auth)) -> dict:
    """Turn the monthly send to auto (skip the Stage-3 confirm) for a company, or back to manual."""
    return engine.set_newsletter_auto(body.company_id, body.on)


# ---- voice: speech-to-text (Deepgram) + text-to-speech (ElevenLabs Flash) ----

# Brand vocabulary — Deepgram keyterm-boosts these so dictation hears them right, and a backstop normaliser
# rewrites the common mishearings (e.g. "Sensor Productions" -> "Sensa Productions") on EVERY transcript, so a
# misheard name never reaches a draft, a Talk turn, or a taught rule.
_STT_KEYTERMS = ["Sensa", "Sensa Productions", "Sensa Studio", "Tabscanner", "Snap Rewards", "SkyVision",
                 "FilmSpoke", "Cortex"]
_BRAND_FIXES = [
    (re.compile(r"\b[cs]ensor\s+doc\s+digital\b", re.I), "Sensa Digital"),
    (re.compile(r"\b[cs]ensor\s+productions\b", re.I), "Sensa Productions"),
    (re.compile(r"\b[cs]ensor\s+studio\b", re.I), "Sensa Studio"),
    (re.compile(r"\b[cs]ensor[\s.]+digital\b", re.I), "Sensa Digital"),
    (re.compile(r"\btab\s+scanner\b", re.I), "Tabscanner"),
    (re.compile(r"\bfilm\s+spoke\b", re.I), "FilmSpoke"),
]


def normalize_brand_names(text: str) -> str:
    """Fix the common speech-to-text mishearings of the company/product names."""
    for rx, repl in _BRAND_FIXES:
        text = rx.sub(repl, text or "")
    return text


@app.post("/api/voice/stt")
def stt(audio: UploadFile = File(...), _: None = Depends(auth)) -> dict:
    """Transcribe a recorded audio clip -> text (Deepgram Nova-3): keyterm-boosted + brand-name-normalised."""
    key = config.require("DEEPGRAM_API_KEY")
    data = audio.file.read()
    ct = audio.content_type or "audio/webm"
    params = [("model", "nova-3"), ("smart_format", "true"), ("punctuate", "true")]
    params += [("keyterm", k) for k in _STT_KEYTERMS]
    r = httpx.post("https://api.deepgram.com/v1/listen", params=params,
                   headers={"Authorization": f"Token {key}", "Content-Type": ct}, content=data, timeout=60)
    r.raise_for_status()
    j = r.json()
    try:
        text = j["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError):
        text = ""
    return {"text": normalize_brand_names(text)}


class Speak(BaseModel):
    text: str


@app.post("/api/voice/tts")
def tts(body: Speak, _: None = Depends(auth)) -> Response:
    """Speak text aloud -> mp3 audio (ElevenLabs Flash v2.5)."""
    key = config.require("ELEVENLABS_API_KEY")
    voice = config.get("ELEVENLABS_VOICE_ID") or "Xb7hH8MSUJpSbSDYk0k2"  # Alice (British), default
    try:
        speed = float(config.get("ELEVENLABS_SPEED") or "1.14")
    except ValueError:
        speed = 1.14
    try:
        stab = float(config.get("ELEVENLABS_STABILITY") or "0.4")
    except ValueError:
        stab = 0.4
    text = (body.text or "").strip()[:2500]
    if not text:
        raise HTTPException(status_code=400, detail="nothing to say")
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_flash_v2_5", "voice_settings": {
            "stability": stab, "similarity_boost": 0.8, "style": 0.0,
            "use_speaker_boost": True, "speed": speed}}, timeout=60)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"tts failed: {r.status_code} {r.text[:200]}")
    return Response(content=r.content, media_type="audio/mpeg")


# ---- chat: talk to Cortex (conversational brain) ----

CHAT_SYSTEM_BASE = (
    "You are Cortex, Rashad's voice-first AI operations partner. You help him run his businesses: "
    "Tabscanner (receipt-OCR / data-extraction API), Sensa (AI video production), SkyVision, and "
    "FilmSpoke. "
    "You are warm, sharp and concise. Your replies are usually read aloud, so write the "
    "way you'd speak: natural sentences, no markdown, no bullet lists, no headings, and keep it brief "
    "unless he asks for depth. "
    "You manage Cortex's SKILLS and their standing rules. A skill is how a job gets done well plus "
    "rules the worker always follows. Use your tools to view skills (list_skills), add a rule "
    "(add_rule), create a skill (create_skill), or rewrite a skill's craft (update_craft). When Rashad "
    "asks to add a rule, create a skill, or change how something is handled, ACTUALLY DO IT with the "
    "tools, then confirm in one short spoken sentence what you changed. When he describes a case that "
    "went wrong (e.g. a misjudged lead), turn the lesson into one or more concise standing rules and "
    "add them to the right skill. If unsure which skill or company he means, ask. "
    "RULES HAVE A SCOPE: 'universal' (applies to EVERY company) or 'company' (just one). This matters a "
    "lot — never let a company-specific rule spread. Unless Rashad is clearly stating a universal "
    "principle, ASK before saving: 'Universal for all companies, or just <company>?' Default to company "
    "when in doubt, and always confirm which scope you saved it under. "
    "You can also SEE and ACT on the work: list_tasks (recent / Inbox) and get_task (full detail incl. "
    "the draft); when Rashad refers to 'that draft', 'the last post', or a specific Inbox item, FIND it "
    "with those first — don't guess which one. Use draft to write something now with a skill (its craft "
    "and rules get applied) and show it to him; create_task to queue work that drafts into the Inbox; and "
    "on a PENDING task approve_task / skip_task / correct_task (correct redrafts it and learns the rule). "
    "When he gives feedback on a draft you just made, you can re-run draft with a revision AND, if the "
    "lesson is durable, add_rule so it sticks for next time — fix it and teach it in one go. "
    "You ALSO run the CRM and the calendar. Use crm_lookup to find a contact or client company, and crm_pipeline "
    "to read the deal pipeline (open opportunities + forecast, won projects + value) — use these BEFORE answering "
    "'what's in the pipeline / the forecast / who is X'. You can create_company (a client company), create_contact "
    "(a person; a deal's people come from their client company, so set the company), and create_deal (defaults to "
    "the Opportunity stage). You can run_report (SEO & traffic report, lands in the Inbox now), schedule_report (run "
    "it on a cadence), and list_scheduled. When Rashad asks you to add a contact/company/deal or to send/schedule a "
    "report, DO IT with these tools, then confirm in one short spoken sentence. "
    "You ALSO have a KNOWLEDGE BASE about the Cortex system itself — its architecture and how its features "
    "work (the questionnaire, voice routing, the Chief/Manager/Worker org, approvals and earned autonomy, "
    "the nightly backup). When Rashad asks how Cortex or any of its features work, or where to find "
    "something, call system_knowledge to look it up and answer accurately — never guess about the system."
)

SKILL_TOOLS = [
    {"name": "system_knowledge",
     "description": "Look up how the CORTEX SYSTEM ITSELF works — its architecture, what has been built, "
                    "and how its features work (the questionnaire, voice routing, the Chief/Manager/Worker "
                    "org, approvals + earned autonomy, the nightly backup, etc.). Use this BEFORE answering "
                    "ANY question about Cortex itself or 'how does X work / where do I find Y' — never guess "
                    "about the system. Returns the relevant docs from Cortex's own knowledge base.",
     "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "what to look up, e.g. 'how does the questionnaire work'"}},
        "required": ["query"]}},
    {"name": "list_skills",
     "description": "List skills (with their standing rules) for a company and/or department. Omit both for an overview.",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string", "description": "company slug"},
        "department": {"type": "string", "description": "department name e.g. 'Content & SEO' — zooms in with full detail"}}}},
    {"name": "add_rule",
     "description": "Add a standing rule to a skill. scope='universal' applies it to EVERY company; "
                    "scope='company' applies it to just the named company. If Rashad hasn't made the scope "
                    "clear, ASK him ('universal for all companies, or just <company>?') BEFORE calling this.",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string", "description": "the company slug (required when scope=company)"},
        "skill": {"type": "string", "description": "skill_key"}, "rule": {"type": "string"},
        "scope": {"type": "string", "enum": ["universal", "company"],
                  "description": "universal = all companies; company = just one"}},
        "required": ["skill", "rule", "scope"]}},
    {"name": "create_skill",
     "description": "Create a new skill. It is added to EVERY company automatically (skills are global; rules get tuned per company). Always set the department so it files correctly on the Skills screen.",
     "input_schema": {"type": "object", "properties": {
        "skill_key": {"type": "string", "description": "short kebab id, e.g. ideas-parking-lot"},
        "name": {"type": "string"}, "craft": {"type": "string", "description": "how to do this job well"},
        "department": {"type": "string", "description": "department it belongs to, e.g. 'Finance & Admin', 'Content & SEO'"}},
        "required": ["skill_key", "name", "craft", "department"]}},
    {"name": "update_craft",
     "description": "Replace a skill's craft (the core how-to text).",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string"}, "skill": {"type": "string"}, "craft": {"type": "string"}},
        "required": ["company", "skill", "craft"]}},
    {"name": "list_tasks",
     "description": "List recent tasks / Inbox items (newest first). Optional status filter (awaiting_approval, done, rejected).",
     "input_schema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "get_task",
     "description": "Full detail of one task, including its current draft and the manager's verdict.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "draft",
     "description": "INLINE PREVIEW ONLY — produces a draft and returns it to show in the chat, creating NO task and NO Inbox entry. Use this ONLY when Rashad EXPLICITLY says 'just show me a version here' / 'don't commit it yet'. For anything he wants to keep, send, approve, or that should land in his Inbox, use create_task instead (NOT this).",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string"}, "skill": {"type": "string", "description": "skill_key"},
        "brief": {"type": "string"}, "revision": {"type": "string", "description": "optional: how to change a previous draft"}},
        "required": ["company", "skill", "brief"]}},
    {"name": "create_task",
     "description": "THE DEFAULT for any 'draft/write/create' request (emails, notes, posts, replies, copy) and any work to do: it runs through the worker + manager and lands in Rashad's INBOX for approval. Use THIS — never the inline draft tool — whenever he asks you to draft something. After calling it, just tell him it's in his Inbox; do NOT paste a draft into the chat. kind: content (default; emails/notes/copy) or blog. For kind=blog it proposes readable CONCEPT(S) (title + summary) to approve first, then builds the formatted post — set count to how many ideas he asked for (e.g. 6). Pick a REAL skill_key (call list_skills if unsure).",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string"}, "skill": {"type": "string"},
        "kind": {"type": "string", "description": "content (default) or blog"}, "brief": {"type": "string"},
        "count": {"type": "integer", "description": "blog only: how many concepts to propose (1 default; 6 if he asks for six ideas)"}},
        "required": ["company", "skill", "brief"]}},
    {"name": "draft_email",
     "description": "Draft an OUTBOUND email (a new email, not a reply). It lands in his Inbox rendered as a "
                    "proper email — recipient, subject, the company logo + signature. BEFORE calling this you MUST "
                    "look the recipient up with crm_lookup: if exactly one match, use their email; if MORE THAN ONE "
                    "match, ASK Rashad which one (never guess); if none, ask him for the address (or proceed with "
                    "just the name if he says so). SENDING IS LIVE: when Rashad APPROVES the card it actually SENDS "
                    "from the company mailbox (gated by his biometric/PIN). So tell him it's in his Inbox and that "
                    "approving it will send it — do NOT say he has to send it himself.",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string", "description": "your business slug (whose brand/signature/logo to use)"},
        "to_name": {"type": "string", "description": "recipient's name"},
        "to_email": {"type": "string", "description": "recipient's email (resolve via crm_lookup if known)"},
        "subject": {"type": "string"},
        "brief": {"type": "string", "description": "what the email should say"},
        "from_email": {"type": "string", "description": "optional: from address (defaults to the company's)"}},
        "required": ["company", "to_name", "subject", "brief"]}},
    {"name": "correct_task",
     "description": "Give feedback on a pending task's draft; it redrafts and learns the rule.",
     "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "integer"}, "feedback": {"type": "string"}}, "required": ["task_id", "feedback"]}},
    {"name": "approve_task",
     "description": "Approve a pending task (publishes / executes it).",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "skip_task",
     "description": "Skip / discard a pending task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "crm_lookup",
     "description": "Search the CRM for contacts and client companies by name, email or company. Use this to FIND someone or a company before acting on them.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "crm_pipeline",
     "description": "The deal pipeline: open opportunities + forecast value, and won projects + value. Use to answer 'what's in the pipeline / the forecast / what have we won'. Optional business slug to scope it.",
     "input_schema": {"type": "object", "properties": {"company": {"type": "string", "description": "your business slug (sensa/tabscanner/...), omit for all"}}}},
    {"name": "create_company",
     "description": "Create a CLIENT company (account) in the CRM.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "website": {"type": "string"}, "phone": {"type": "string"}}, "required": ["name"]}},
    {"name": "create_contact",
     "description": "Create a contact (a person). 'company' = the client company they work at (linked/created automatically). 'business' = which of YOUR businesses (slug) it relates to.",
     "input_schema": {"type": "object", "properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "email": {"type": "string"}, "company": {"type": "string", "description": "client company name"}, "business": {"type": "string", "description": "your business slug"}, "phone": {"type": "string"}, "job_title": {"type": "string"}}, "required": ["email"]}},
    {"name": "create_deal",
     "description": "Create a deal. 'business' = which of YOUR businesses (slug). 'company' = the CLIENT ORGANISATION name — a real company, NEVER the deal/project title. The deal links to that organisation (its people come from there). If you don't know the client organisation, OMIT company (the deal is created unlinked) — do NOT pass the deal title as the company, or invent an org. Defaults to the Opportunity stage.",
     "input_schema": {"type": "object", "properties": {"business": {"type": "string"}, "title": {"type": "string", "description": "the deal/project name"}, "value": {"type": "number"}, "currency": {"type": "string"}, "stage": {"type": "string"}, "company": {"type": "string", "description": "the CLIENT ORGANISATION (real company), distinct from the deal title; omit if unknown"}}, "required": ["business", "title"]}},
    {"name": "schedule_report",
     "description": "Schedule the per-business SEO & traffic report to run on a cadence; it lands in the Inbox. weekday 0=Mon..6=Sun.",
     "input_schema": {"type": "object", "properties": {"company": {"type": "string", "description": "your business slug"}, "cadence": {"type": "string", "enum": ["daily", "weekly", "monthly"]}, "weekday": {"type": "integer"}, "hour": {"type": "integer"}}, "required": ["company"]}},
    {"name": "run_report",
     "description": "Generate the SEO & traffic report for a business right now; it lands in the Inbox.",
     "input_schema": {"type": "object", "properties": {"company": {"type": "string"}}, "required": ["company"]}},
    {"name": "list_scheduled",
     "description": "List the scheduled recurring jobs (e.g. SEO reports).",
     "input_schema": {"type": "object", "properties": {"company": {"type": "string"}}}},
    {"name": "list_calendar",
     "description": "Read the unified Calendar to answer 'what's on my calendar / what's piling up / what's "
                    "due'. Returns three lanes: now_to_deal_with (un-dated open work in the Inbox), recurring "
                    "(jobs on a cadence), and upcoming (dated one-offs). Optional business slug to scope it.",
     "input_schema": {"type": "object", "properties": {"company": {"type": "string"}}}},
    {"name": "set_reminder",
     "description": "Set a reminder for Rashad. A NUDGE (no action_*) drops an info card in the Inbox at the "
                    "time, pointing at the target. An ACTION reminder (give action_skill + action_brief) spawns "
                    "a task at the time that flows through the normal draft -> approval pipeline. Use for "
                    "'remind me to...', 'follow up with...', 'in N days draft...'. Put the natural-language time "
                    "in `when` (it's parsed server-side).",
     "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "description": "what it says, e.g. 'Follow up with Seb'"},
        "when": {"type": "string", "description": "natural-language time, e.g. 'next Tuesday 10am', 'in 3 days'"},
        "recurrence": {"type": "string", "enum": ["none", "daily", "weekly", "monthly", "weekday", "custom"]},
        "custom_days": {"type": "integer", "description": "for recurrence=custom: every N days"},
        "company": {"type": "string", "description": "your business slug, if it relates to one"},
        "target_type": {"type": "string", "description": "contact|deal|project|account|task if it's about a record"},
        "target_id": {"type": "string", "description": "the record id or email"},
        "action_company": {"type": "string", "description": "ACTION: business slug to draft for"},
        "action_skill": {"type": "string", "description": "ACTION: skill_key the worker uses"},
        "action_kind": {"type": "string", "description": "ACTION: kind (content/email_reply/blog/...)"},
        "action_brief": {"type": "string", "description": "ACTION: what to draft when it fires"}},
        "required": ["title", "when"]}},
    {"name": "remember_preference",
     "description": "Persist something Rashad has taught you: a standing preference, a way he wants you to "
                    "behave, or a durable FACT (e.g. 'Richard is Rashad', 'keep replies short', 'always CC ben "
                    "on Tabscanner emails'). Call it whenever he says 'remember…', 'always…', 'from now on…' or "
                    "states a clear standing rule, then confirm briefly. It joins your standing instructions for "
                    "EVERY future conversation. Keep each one a short, clear sentence. NOTE: this only ADDS your "
                    "operator preferences — it can never change or remove your core safety rules.",
     "input_schema": {"type": "object", "properties": {
        "rule": {"type": "string", "description": "the concise thing to remember"}}, "required": ["rule"]}},
    {"name": "forget_preference",
     "description": "Remove one of the preferences you've remembered. Pass the exact text or a close match.",
     "input_schema": {"type": "object", "properties": {"rule": {"type": "string"}}, "required": ["rule"]}},
    {"name": "list_preferences",
     "description": "List everything Rashad has taught you to remember (your standing operator preferences).",
     "input_schema": {"type": "object", "properties": {}}},
]


def _exec_skill_tool(name: str, inp: dict) -> str:
    if name == "system_knowledge":
        return knowledge.search(inp.get("query", ""))
    if name == "list_skills":
        slug, dept = inp.get("company"), inp.get("department")
        where, params = [], []
        if slug:
            where.append("c.slug=%s"); params.append(slug)
        if dept:
            where.append("s.department=%s"); params.append(dept)
        clause = (" where " + " and ".join(where)) if where else ""
        rows = db.query(
            "select s.skill_key, s.name, s.category, s.department, s.authority, s.rules, s.craft, "
            "c.slug as company from skills s join companies c on c.id=s.company_id" + clause +
            " order by c.name, s.category, s.department, s.name", tuple(params))
        detail = bool(dept)  # include craft only when zoomed into one department
        out = []
        for r in rows:
            item = {"company": r["company"], "department": r["department"], "skill_key": r["skill_key"],
                    "name": r["name"], "authority": r["authority"], "rules": r["rules"] or []}
            if detail:
                item["craft"] = (r["craft"] or "")[:300]
            out.append(item)
        return json.dumps(out) if out else "no skills found"
    if name == "list_tasks":
        status = inp.get("status")
        rows = db.query(
            "select t.id,t.kind,t.status,t.request,t.draft,c.slug company,s.name skill "
            "from tasks t join companies c on c.id=t.company_id left join skills s on s.id=t.skill_id "
            + ("where t.status=%s " if status else "") + "order by t.id desc limit 15",
            (status,) if status else ())
        out = [{"id": r["id"], "company": r["company"], "skill": r["skill"], "kind": r["kind"],
                "status": r["status"],
                "brief": ((r["request"] or {}).get("brief") if isinstance(r["request"], dict) else "") or "",
                "draft_preview": (r["draft"] or "")[:140]} for r in rows]
        return json.dumps(out) if out else "no tasks yet"
    if name == "get_task":
        t = store.get_task(int(inp["task_id"]))
        if not t:
            return "no such task"
        return json.dumps({"id": t["id"], "status": t["status"], "kind": t["kind"],
                           "request": t.get("request"), "draft": t.get("draft"),
                           "manager": t.get("manager")}, default=str)
    if name == "correct_task":
        return json.dumps(engine.correct_task(int(inp["task_id"]), inp.get("feedback", "")), default=str)
    if name == "approve_task":
        return json.dumps(engine.approve_task(int(inp["task_id"])), default=str)
    if name == "skip_task":
        return json.dumps(engine.skip_task(int(inp["task_id"])), default=str)
    if name == "crm_lookup":
        q = (inp.get("query") or "").strip()
        toks = [t for t in q.split() if t] or [q]   # match EVERY word across the full name (handles "Adam Goldwater")
        hay = ("(coalesce(first_name,'')||' '||coalesce(last_name,'')||' '||coalesce(email,'')||' '||"
               "coalesce(company_name,''))")
        where = " and ".join([f"{hay} ilike %s"] * len(toks))
        params = tuple(f"%{t}%" for t in toks)
        cons = db.query(f"select first_name,last_name,email,company_name,organisation,stage,is_client from crm_master "
                        f"where {where} order by is_client desc, first_name nulls last limit 12", params)
        if not cons and len(toks) > 1:   # no exact all-words hit -> loosen to ANY word, so near matches surface
            owhere = " or ".join([f"{hay} ilike %s"] * len(toks))
            cons = db.query(f"select first_name,last_name,email,company_name,organisation,stage,is_client from crm_master "
                            f"where {owhere} order by is_client desc, first_name nulls last limit 12", params)
        awhere = " and ".join(["(coalesce(name,'')||' '||coalesce(domain,'')) ilike %s"] * len(toks))
        accs = db.query(f"select id,name,domain from crm_accounts where {awhere} order by name limit 8", params)
        return json.dumps({"contacts": cons, "companies": accs}, default=str)
    if name == "crm_pipeline":
        slug = inp.get("company")
        opp, proj = crm_opportunities(slug, _=None), crm_projects(slug, _=None)
        return json.dumps({"opportunities_count": opp["count"], "forecast_value": opp["total_value"],
                           "top_opportunities": [{"title": o["title"], "value": o["value"], "company": o["company"]}
                                                 for o in opp["opportunities"][:8]],
                           "projects_count": proj["count"], "won_value": proj["total_value"],
                           "projects": [{"title": p["title"], "value": p["value"], "stage": p["stage"]}
                                        for p in proj["projects"][:10]]}, default=str)
    if name == "create_company":
        a = crm.create_account(inp["name"], website=inp.get("website"), phone=inp.get("phone"))
        return f"created client company '{a['name']}' (id {a['id']})"
    if name == "create_contact":
        aid = crm.get_or_create_account(inp["company"].strip()) if inp.get("company") else None
        c = crm.create_contact(inp.get("first_name", ""), inp.get("last_name", ""), inp["email"],
                               account_id=aid, company=inp.get("business"), phone=inp.get("phone"),
                               job_title=inp.get("job_title"))
        return f"created contact {c['email']}" + (f" at {inp['company']}" if inp.get("company") else "")
    if name == "create_deal":
        comp = (inp.get("company") or "").strip()
        title = (inp.get("title") or "").strip()
        aid, link_note = None, ""
        if comp:
            existing = db.one("select id from crm_accounts where lower(name)=lower(%s)", (comp,))
            if existing:
                aid = existing["id"]                          # link to the real client organisation
            elif crm._basename(comp) and crm._basename(comp) != crm._basename(title):
                aid = crm.get_or_create_account(comp)         # a genuinely NEW client company (distinct from the deal title)
            else:
                # a deal must NOT spawn an organisation named after itself (operator rule 2026-06-22)
                link_note = (f" — note: did NOT create an organisation from '{comp}' (it matches the deal title). "
                             "A deal links to a client ORGANISATION, never to its own title. Pick or create the "
                             "client organisation, then link the deal to it.")
        stage = inp.get("stage") if inp.get("stage") in crm.DEAL_STAGES else "Opportunity"
        try:
            d = crm.create_deal(inp.get("business", "sensa"), title, value=inp.get("value"),
                                currency=inp.get("currency", "AED"), stage=stage, account_id=aid)
        except crm.DuplicateDeal as e:
            return str(e)
        return (f"created deal '{d['title']}' ({d['stage']}, {d.get('value') or 'no value'} {d['currency']})"
                + (f" for {comp}" if comp and aid else "") + link_note)
    if name == "schedule_report":
        slug = inp.get("company", "tabscanner")
        co = store.get_company_by_slug(slug)
        if not co:
            return f"unknown company {slug}"
        skill = store.get_skill_by_key(co["id"], engine.REPORT_SKILL_KEY)
        label = seo_report.available().get(slug, slug)
        cadence, weekday, hour = inp.get("cadence", "weekly"), int(inp.get("weekday", 0)), int(inp.get("hour", 12))
        nr = schedule.next_run(cadence, weekday, hour, 0)
        t = db.execute(
            "insert into tasks (company_id,skill_id,kind,request,status,origin,title,schedule_kind,cadence,"
            "weekday,hour,minute,next_run,enabled) values (%s,%s,'seo_report',%s,'scheduled','talk',%s,"
            "'recurring',%s,%s,0,%s,true) returning *",
            (co["id"], skill["id"] if skill else None, Json({"kind": "seo_report", "company": slug, "days": 28}),
             f"{label} — SEO & Traffic report", cadence, weekday, hour, nr))
        return f"scheduled the {label} SEO report {cadence}; next run {t['next_run']}"
    if name == "run_report":
        t = engine.deliver_seo_report(inp.get("company", "tabscanner"), days=28)
        return f"generated the report — it's in your Inbox now (task #{t['id']})"
    if name == "list_scheduled":
        co = store.get_company_by_slug(inp["company"]) if inp.get("company") else None
        flt, p = (" and company_id=%s", (co["id"],)) if co else ("", ())
        rows = db.query("select title,cadence,next_run,enabled from tasks where schedule_kind='recurring'"
                        + flt + " order by next_run nulls last", p)
        return json.dumps([{"title": r["title"], "cadence": r["cadence"], "next_run": str(r["next_run"]),
                            "enabled": r["enabled"]} for r in rows]) or "no scheduled tasks"
    if name == "list_calendar":
        co = store.get_company_by_slug(inp["company"]) if inp.get("company") else None
        flt, p = (" and company_id=%s", (co["id"],)) if co else ("", ())
        nowq = db.query("select title,kind,status from tasks where status in ('awaiting_approval',"
                        "'awaiting_correction') and schedule_kind is null" + flt + " order by created_at", p)
        recq = db.query("select title,cadence,next_run from tasks where schedule_kind='recurring'" + flt
                        + " order by next_run nulls last", p)
        upcq = db.query("select title,kind,run_at from tasks where schedule_kind='once' and status='scheduled'"
                        + flt + " order by run_at nulls last", p)
        return json.dumps({
            "now_to_deal_with": [(r["title"] or r["kind"]) for r in nowq],
            "recurring": [f"{r['title']} ({r['cadence']}, next {r['next_run']})" for r in recq],
            "upcoming": [f"{(r['title'] or r['kind'])} ({r['run_at']})" for r in upcq],
        })
    if name == "set_reminder":
        due = reminders.parse_when(inp.get("when") or "")
        if not due:
            return "Could not work out the time — ask Rashad to restate it (e.g. 'next Tuesday 10am')."
        cid = None
        if inp.get("company"):
            co = store.get_company_by_slug(inp["company"]); cid = co["id"] if co else None
        action = None
        if inp.get("action_skill") and inp.get("action_brief"):
            aco_slug = inp.get("action_company") or inp.get("company")
            aco = store.get_company_by_slug(aco_slug or "")
            ask = store.get_skill_by_key(aco["id"], inp["action_skill"]) if aco else None
            if not aco or not ask:   # give the model immediate feedback so it picks a REAL skill
                return (f"Can't set that ACTION reminder: "
                        + ("no business '%s'." % aco_slug if not aco else
                           "skill '%s' doesn't exist for %s." % (inp["action_skill"], aco_slug))
                        + " Call list_skills to choose a real skill_key, or set it as a plain nudge "
                          "reminder (drop the action_* fields).")
            action = {"company": aco_slug, "skill": inp["action_skill"],
                      "kind": inp.get("action_kind") or "content", "brief": inp["action_brief"]}
        r = reminders.create(inp["title"], due, company_id=cid, target_type=inp.get("target_type"),
                             target_id=inp.get("target_id"), recurrence=inp.get("recurrence") or "none",
                             custom_days=inp.get("custom_days"), action=action)
        rep = (" repeating " + r["recurrence"]) if r.get("recurrence") not in (None, "none") else ""
        kind = "action reminder (drafts when it fires)" if action else "reminder"
        return f"Set {kind} #{r['id']}: \"{inp['title']}\" for {due.strftime('%a %d %b, %H:%M')} GST{rep}."
    if name == "remember_preference":
        rule = (inp.get("rule") or "").strip()
        if not rule:
            return "nothing to remember"
        rules = db.setting_get("chat_self_rules") or []
        if rule not in rules:
            rules.append(rule)
        db.setting_set("chat_self_rules", rules)
        return f'Remembered: "{rule}". I will honour that in every conversation from now on.'
    if name == "forget_preference":
        q = (inp.get("rule") or "").strip().lower()
        rules = db.setting_get("chat_self_rules") or []
        kept = [r for r in rules if q and q not in r.lower()]
        db.setting_set("chat_self_rules", kept)
        return f"Done — removed {len(rules) - len(kept)}, {len(kept)} preference(s) remaining."
    if name == "list_preferences":
        rules = db.setting_get("chat_self_rules") or []
        return json.dumps(rules) if rules else "I haven't been taught any standing preferences yet."
    if name == "create_skill":
        dept = inp.get("department")
        cat, mgr = catalog.dept_meta(dept) if dept else (None, None)
        slugs = []
        for c in db.query("select id, slug from companies order by name"):
            store.upsert_skill(c["id"], inp["skill_key"], inp["name"], craft=inp["craft"],
                               category=cat, department=dept, manager=mgr)
            slugs.append(c["slug"])
        return (f"created '{inp['skill_key']}' across all companies ({', '.join(slugs)})"
                + (f" in {dept}" if dept else " — but no department set; tell me which department it belongs to"))
    if name == "add_rule":
        if inp.get("scope") == "universal":
            store.add_universal_rule(inp["skill"], inp["rule"])
            return f"added UNIVERSAL rule to '{inp['skill']}' (applies to ALL companies): {inp['rule']}"
        co = store.get_company_by_slug(inp.get("company", ""))
        if not co:
            return "For a company-only rule I need the company; or set scope='universal' for all companies."
        sk = store.get_skill_by_key(co["id"], inp.get("skill", ""))
        if not sk:
            return f"no skill '{inp.get('skill')}' for {co['slug']}"
        store.add_rule(sk["id"], inp["rule"])
        return f"added LOCAL rule to {co['slug']}/{sk['skill_key']} (this company only): {inp['rule']}"
    co = store.get_company_by_slug(inp.get("company", ""))
    if not co:
        known = ", ".join(r["slug"] for r in db.query("select slug from companies"))
        return f"no company '{inp.get('company')}' (known: {known})"
    if name == "update_craft":
        sk = store.get_skill_by_key(co["id"], inp.get("skill", ""))
        if not sk:
            return f"no skill '{inp.get('skill')}'"
        db.execute("update skills set craft=%s, updated_at=now() where id=%s", (inp["craft"], sk["id"]))
        return f"updated craft for {co['slug']}/{sk['skill_key']}"
    if name == "draft":
        sk = store.get_skill_by_key(co["id"], inp.get("skill", ""))
        if not sk:
            return f"no skill '{inp.get('skill')}' for {co['slug']}"
        req = {"brief": inp.get("brief", "")}
        if inp.get("_images"):
            req["attachments"] = inp["_images"]
            req["attachment_names"] = inp.get("_image_names")
        text = worker.draft(sk, co, req, correction=inp.get("revision"))
        return "DRAFT (skill craft + rules applied):\n\n" + text
    if name == "create_task":
        sk = store.get_skill_by_key(co["id"], inp.get("skill", ""))
        if not sk:
            return f"no skill '{inp.get('skill')}' for {co['slug']}"
        req = {"brief": inp.get("brief", "")}
        if inp.get("kind") == "blog":   # blog ideation: how many concepts to propose (default 1)
            req["count"] = max(1, min(int(inp.get("count") or 1), 10))
        if inp.get("_images"):   # files/images shared in this Talk turn -> the worker drafts WITH them
            req["attachments"] = inp["_images"]
            req["attachment_names"] = inp.get("_image_names")
        t = store.create_task(co["id"], sk["id"], inp.get("kind", "content"), req)
        n = len(req.get("attachments", []))
        return (f"created task #{t['id']} — drafting now"
                + (f" using the {n} attached file(s)" if n else "") + "; it'll appear in the Inbox for approval.")
    if name == "draft_email":
        to_name = (inp.get("to_name") or "").strip()
        to_email = (inp.get("to_email") or "").strip()
        # Resolve the recipient from the CRM SERVER-SIDE so the email is real (never a "[email from CRM]" placeholder).
        if (not to_email or "@" not in to_email or "[" in to_email) and to_name:
            toks = [t for t in to_name.split() if t] or [to_name]
            hay = "(coalesce(first_name,'')||' '||coalesce(last_name,''))"
            params = tuple(f"%{t}%" for t in toks)

            def _fmt(rows):   # name <email> [organisation] — so Rashad can tell them apart
                return "; ".join((f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()} "
                                  f"<{r['email']}>" + (f" [{r['organisation']}]" if r.get('organisation') else "")).strip()
                                 for r in rows)
            and_w = " and ".join([f"{hay} ilike %s"] * len(toks))
            ms = db.query(f"select first_name,last_name,email,organisation from crm_master where coalesce(email,'')<>'' "
                          f"and {and_w} order by is_client desc nulls last limit 8", params)
            if len(ms) == 1:
                to_email = ms[0]["email"]
            elif len(ms) > 1:
                return (f"There's more than one '{to_name}' in the CRM: {_fmt(ms)}. Ask Rashad which one, then "
                        "call draft_email again with that exact to_email.")
            else:   # no exact match — offer the closest (any-word) matches as options instead of giving up
                or_w = " or ".join([f"{hay} ilike %s"] * len(toks))
                near = db.query(f"select first_name,last_name,email,organisation from crm_master where "
                                f"coalesce(email,'')<>'' and ({or_w}) order by is_client desc nulls last limit 6", params)
                if near:
                    return (f"No exact '{to_name}' in the CRM, but the closest matches are: {_fmt(near)}. Ask Rashad "
                            "which one he means (or for the right email), then call draft_email with that to_email.")
                return (f"'{to_name}' isn't in the CRM. Ask Rashad for {to_name}'s email address, then call "
                        "draft_email with it.")
        sk = (store.get_skill_by_key(co["id"], "email-handling")
              or store.get_skill_by_key(co["id"], "general-operations")
              or db.one("select * from skills where company_id=%s order by id limit 1", (co["id"],)))
        if not sk:
            return f"no skill available for {co['slug']}"
        req = {"brief": inp.get("brief", ""), "outbound": True,
               "inquiry": {"name": to_name, "email": to_email, "subject": inp.get("subject", ""), "message": ""}}
        if inp.get("from_email"):
            req["from_email"] = inp["from_email"]
        if inp.get("_images"):
            req["attachments"] = inp["_images"]
            req["attachment_names"] = inp.get("_image_names")
        t = store.create_task(co["id"], sk["id"], "email_draft", req)
        addr = f" <{to_email}>" if to_email else " (no email resolved)"
        return (f"Drafting an email to {to_name or 'the recipient'}{addr} — it's in your Inbox as task "
                f"#{t['id']} showing the recipient, subject and logo, for your approval.")
    return f"unknown tool {name}"


def _chat_system() -> str:
    cos = db.query("select slug from companies order by name")
    depts = db.query("select distinct category, department from skills "
                     "where department is not null order by category, department")
    n = db.one("select count(*) as n from skills")["n"]
    co_line = ", ".join(c["slug"] for c in cos) or "(none)"
    dept_line = "; ".join(d["department"] for d in depts) or "(none)"
    note = (f"Every company runs the SAME granular skill catalog ({n} skill rows in total), grouped by "
            f"department. Companies: {co_line}. Departments (all companies have all of them): "
            f"{dept_line}. Most skills are empty (no rules yet) and you tune them one at a time. Use "
            f"list_skills(company, department) to read a department's skills and their rules before "
            f"answering — never assume a skill's rules.")
    return "\n\n".join(p for p in (CHAT_SYSTEM_BASE, note, _shared_behaviour()) if p)


def _shared_behaviour() -> str:
    """Cross-cutting conversational rules appended to EVERY system prompt — general Cortex AND every Chief/
    Manager persona — so behaviour is consistent no matter who answers (identity, attachments, drafting→Inbox,
    proactive CRM lookup, self-learning, and the operator preferences Rashad has taught)."""
    rules = [
        "You are ALWAYS talking to Rashad Al-Safar, owner of every business. Voice-to-text often mis-hears "
        "'Rashad' as 'Richard' — same person; 'Richard' / 'Richard at Tabscanner' means Rashad, and "
        "rashad@tabscanner.com is his Tabscanner business email.",
        "When he names a person or company, crm_lookup them proactively before asking for details (like an "
        "email). If there's no exact match, offer the closest options — never just say you can't find anyone.",
        "FILES / ATTACHMENTS WORK: when he attaches an image or PDF, it IS attached to whatever you draft — saved "
        "on the task, used by the worker, and shown on the Inbox approval card. Just attach it and confirm 'your "
        "screenshot is attached to the draft'. NEVER say you can't attach files, that it won't be included, or hedge.",
        "To draft/write/create ANYTHING use create_task — or draft_email for an outbound email (resolve the "
        "recipient via crm_lookup first; ask which if several, ask for the address if none). It runs through the "
        "worker + manager and lands in his INBOX for approval. NEVER paste a draft in chat or say 'here's the "
        "draft' — it lives in the Inbox. Use the inline `draft` tool only if he explicitly asks to just see a "
        "version inline. Nothing is ever sent or done without his Inbox approval.",
        "NEVER fabricate a confirmation. Do NOT tell him something is created, drafted, queued, scheduled or "
        "'in your Inbox', and NEVER state or invent a task number, unless you ACTUALLY called create_task / "
        "draft_email / run_report THIS turn and saw a 'created task #N' result. When he pastes a brief or "
        "finished content to turn into a post or email, you MUST pass that text to create_task — never "
        "summarise it or paste it back instead of calling the tool. A made-up confirmation silently loses his work.",
        "When he teaches you a durable preference or fact ('remember…', 'always…', 'from now on…'), call "
        "remember_preference to persist it, then confirm. This only adds operator preferences, never safety rules.",
    ]
    block = "ALWAYS-ON RULES (true no matter which persona is speaking):\n" + "\n".join(f"- {r}" for r in rules)
    block += "\n\n" + capabilities.manifest()   # live capability registry — always current as features ship
    learned = db.setting_get("chat_self_rules") or []
    if learned:
        block += "\n\nTHINGS RASHAD HAS TAUGHT YOU (always honour these):\n" + "\n".join(f"- {r}" for r in learned)
    return block


# A Chief CAN grow the org — create_skill is global-by-nature (added to every company), so there's no
# scope to bleed. But the scoped, bleed-risky part — writing per-company RULES (add_rule/update_craft)
# — stays with the Manager (one keeper of rules). Managers + general Cortex get the full set.
_CHIEF_TOOLS = {"system_knowledge", "list_skills", "list_tasks", "get_task", "create_skill", "set_reminder",
                # Chiefs can also DRAFT and look people up — anyone Rashad talks to should be able to act on a
                # request, not just strategise. (Per-company RULE writes stay Manager-only to avoid scope bleed.)
                "create_task", "draft_email", "draft", "crm_lookup", "crm_pipeline", "correct_task",
                "approve_task", "skip_task", "run_report", "schedule_report", "list_scheduled", "list_calendar",
                "remember_preference", "forget_preference", "list_preferences"}


@app.get("/api/heads")
def heads(_: None = Depends(auth)) -> dict:
    """The org as personas you can talk to: Chiefs (strategy) + Managers (standards)."""
    return personas.org()


_IMG_LIMIT = 10 * 1024 * 1024   # Anthropic per-image hard limit (decoded bytes)


def _image_blocks(data_urls: list[str]) -> list[dict]:
    """Turn data: URLs (data:image/jpeg;base64,...) into Anthropic image content blocks. Skips any image whose
    decoded size exceeds the model's 10 MB limit — a backstop so an oversized attachment can never 500 the chat
    (the cockpit downscales on attach, so this should rarely fire)."""
    blocks = []
    for u in (data_urls or [])[:6]:
        if not isinstance(u, str) or not u.startswith("data:") or ";base64," not in u:
            continue
        head, b64 = u.split(";base64,", 1)
        media = head[5:] or "image/jpeg"
        if not media.startswith("image/"):
            continue
        if (len(b64) * 3) // 4 > _IMG_LIMIT:   # decoded-size estimate over the limit -> drop it, don't 400
            continue
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    return blocks


class ChatTurn(BaseModel):
    messages: list[dict]
    persona: str | None = None     # PINNED head ('' / None = auto-route). 'chief:Demand', 'manager:Content & SEO'
    company: str | None = None     # company slug in focus (the global selector)
    current: str | None = None     # who handled the last turn (sticky auto-routing)
    images: list[str] | None = None  # attached photos/files as data: URLs (multimodal context)
    image_names: list[str] | None = None  # original filenames, parallel to images (for display + send)


def _chat_prepare(body: ChatTurn):
    """Shared prep for /api/chat and /api/chat/stream: build the working-memory window, route to a persona,
    attach images, resolve (system, tools), and build the tool executor. Returns (msgs, chosen, system, tools, exec)."""
    msgs = [{"role": m.get("role"), "content": m.get("content", "")} for m in body.messages
            if m.get("content") and m.get("role") in ("user", "assistant")][-100:]   # working memory window (history-cached, cheap)
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="last message must be from the user")
    pinned = (body.persona or "").strip()
    # pinned head wins; otherwise the concierge routes (sticky to `current` unless the subject shifts).
    chosen = pinned if pinned else personas.route(msgs, body.company, (body.current or "").strip())
    # attach any images to the last user turn so Cortex can actually see them (Claude is multimodal).
    blocks = _image_blocks(body.images or [])
    if blocks:
        msgs[-1]["content"] = blocks + [{"type": "text", "text": msgs[-1]["content"]}]
    system, tools = _chat_system(), SKILL_TOOLS
    if chosen:
        psys, _model, is_chief = personas.persona_system(chosen, body.company)
        if psys:
            system = psys + "\n\n" + _shared_behaviour()   # personas get the always-on rules too
            tools = [t for t in SKILL_TOOLS if t["name"] in _CHIEF_TOOLS] if is_chief else SKILL_TOOLS
        else:
            chosen = ""
    def _exec(name: str, inp: dict) -> str:   # carry the turn's attachments through when a tool drafts/creates
        if name in ("create_task", "draft", "draft_email") and body.images:
            inp = {**inp, "_images": body.images, "_image_names": body.image_names}
        return _exec_skill_tool(name, inp)
    return msgs, chosen, system, tools, _exec


@app.post("/api/chat")
def chat(body: ChatTurn, _: None = Depends(auth)) -> dict:
    msgs, chosen, system, tools, _exec = _chat_prepare(body)
    reply = provider.chat_tools(system, msgs, tools, _exec,
                                purpose=f"chat:{chosen}" if chosen else "chat", company=body.company)
    return {"reply": reply, "persona": chosen, "persona_label": personas.label(chosen)}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(body: ChatTurn, _: None = Depends(auth)) -> StreamingResponse:
    """Streaming chat: the SAME agentic loop as /api/chat, emitted as Server-Sent Events so the reply paints live
    AND the connection never idles past a proxy's ~100s ceiling (the Cloudflare 524 that caused the timeouts).
    Events: meta (persona) -> delta* (text) / tool (name) -> done (full reply) -> [error]."""
    msgs, chosen, system, tools, _exec = _chat_prepare(body)   # 400s raised here, before streaming starts

    def gen():
        yield _sse("meta", {"persona": chosen, "persona_label": personas.label(chosen)})
        try:
            for kind, data in provider.chat_tools_stream(
                    system, msgs, tools, _exec,
                    purpose=f"chat:{chosen}" if chosen else "chat", company=body.company):
                yield _sse(kind, data)
        except Exception as e:  # noqa: BLE001 — surface a clean error event instead of a broken stream
            yield _sse("error", {"error": str(e)[:200]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class TitleReq(BaseModel):
    messages: list[dict]


@app.post("/api/chat/title")
def chat_title(body: TitleReq, _: None = Depends(auth)) -> dict:
    """A short subject label for a Talk conversation (auto-naming)."""
    return {"title": personas.name_chat(body.messages)}


# ---- saved conversations (Talk history) ----

class ConvSave(BaseModel):
    messages: list[dict]
    title: str | None = None


@app.get("/api/conversations")
def list_conversations(_: None = Depends(auth)) -> list[dict]:
    return store.conv_list()


@app.post("/api/conversations")
def new_conversation(_: None = Depends(auth)) -> dict:
    return store.conv_create()


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int, _: None = Depends(auth)) -> dict:
    c = store.conv_get(cid)
    if not c:
        raise HTTPException(status_code=404, detail="no such conversation")
    return c


@app.put("/api/conversations/{cid}")
def save_conversation(cid: int, body: ConvSave, _: None = Depends(auth)) -> dict:
    c = store.conv_save(cid, body.messages, body.title)
    if not c:
        raise HTTPException(status_code=404, detail="no such conversation")
    return c


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: int, _: None = Depends(auth)) -> dict:
    store.conv_delete(cid)
    return {"ok": True}


# ---- voice: live streaming transcription (browser mic -> us -> Deepgram -> back) ----

@app.websocket("/api/voice/stream")
async def voice_stream(ws: WebSocket):
    if _token_subject(ws.query_params.get("token", "")) is None:   # accepts owner + named-user tokens
        await ws.close(code=4401)
        return
    rate = ws.query_params.get("rate", "48000")
    await ws.accept()
    key = config.require("DEEPGRAM_API_KEY")
    url = ("wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16"
           f"&sample_rate={rate}&channels=1&interim_results=true&smart_format=true&punctuate=true"
           "&endpointing=300"  # finalize words promptly; the cockpit ends the turn on a silence timer
           + "".join(f"&keyterm={k.replace(' ', '%20')}" for k in _STT_KEYTERMS))
    hdr = [("Authorization", f"Token {key}")]
    try:
        try:
            dg = await websockets.connect(url, additional_headers=hdr)
        except TypeError:  # older websockets uses extra_headers
            dg = await websockets.connect(url, extra_headers=hdr)
    except Exception as e:  # noqa: BLE001
        await ws.send_json({"error": f"deepgram connect failed: {e}"})
        await ws.close()
        return

    async def pump_up():
        try:
            while True:
                await dg.send(await ws.receive_bytes())
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            pass
        finally:
            try:
                await dg.send(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001
                pass

    async def pump_down():
        try:
            async for msg in dg:
                try:
                    d = json.loads(msg)
                except Exception:  # noqa: BLE001
                    continue
                if d.get("type") == "Results":
                    alts = (d.get("channel") or {}).get("alternatives") or [{}]
                    text = normalize_brand_names(alts[0].get("transcript", ""))
                    sf = bool(d.get("speech_final"))
                    if text or sf:
                        await ws.send_json({"final": bool(d.get("is_final")), "speech_final": sf, "text": text})
        except Exception:  # noqa: BLE001
            pass

    try:
        await asyncio.gather(pump_up(), pump_down())
    finally:
        try:
            await dg.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---- Google OAuth (keyless Drive backup: authorise once, store a refresh token) ----

GOOGLE_OAUTH_CLIENT = "/etc/cortex/google_oauth_client.json"


def _google_client(company: str | None = None) -> tuple[str, str, str]:
    """The OAuth web-client config. Default = the shared Cortex-system project (Drive backup + Tabscanner
    email). When `company` is given, use that company's OWN project client (`/etc/cortex/
    google_oauth_client_<company>.json`) — the per-company isolation model for YouTube + per-brand email."""
    path = f"/etc/cortex/google_oauth_client_{company}.json" if company else GOOGLE_OAUTH_CLIENT
    with open(path) as f:
        c = (json.load(f).get("web") or {})
    return c["client_id"], c["client_secret"], (c.get("redirect_uris") or [""])[0]


@app.get("/oauth/google/start")
def google_start(purpose: str = "drive", company: str = "", mailbox: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    company = (company or "").strip().lower()
    mailbox = (mailbox or "").strip().lower()   # an EXTRA named mailbox for the company (e.g. gino) — stored separately
    cid, _, redirect = _google_client(company or None)
    if purpose in ("gmail", "gmail_send"):   # a Tabscanner mailbox — its own login, stored separately.
        # gmail.modify = read + draft + SEND + label/archive (NOT permanent delete). One consent, full flow;
        # nothing is ever sent without the owner's approval — the guardrail is the approval gate, not the scope.
        # 'gmail'      = the inbox Cortex READS enquiries from (api@tabscanner.com).
        # 'gmail_send' = the mailbox Cortex SENDS replies from, so they land in YOUR Sent folder, as you.
        scope = ("https://www.googleapis.com/auth/gmail.modify openid "
                 "https://www.googleapis.com/auth/userinfo.email")
    elif purpose == "analytics":   # per-company GA4 + Search Console (the SEO/traffic reports), READ-ONLY
        scope = ("https://www.googleapis.com/auth/analytics.readonly "
                 "https://www.googleapis.com/auth/webmasters.readonly openid "
                 "https://www.googleapis.com/auth/userinfo.email")
    elif purpose == "youtube":   # per-company YouTube: upload + manage + read + analytics (channel binding = the token)
        scope = ("https://www.googleapis.com/auth/youtube.upload "
                 "https://www.googleapis.com/auth/youtube.force-ssl "
                 "https://www.googleapis.com/auth/youtube.readonly "
                 "https://www.googleapis.com/auth/yt-analytics.readonly openid "
                 "https://www.googleapis.com/auth/userinfo.email")
    elif purpose == "calendar":   # Google Calendar (availability + booking) + Drive-read to harvest Gemini meeting-notes Docs
        scope = ("https://www.googleapis.com/auth/calendar "
                 "https://www.googleapis.com/auth/drive.readonly openid "
                 "https://www.googleapis.com/auth/userinfo.email")
    elif purpose == "google_ads":   # per-company Google Ads API (Keyword Planner) on the Internal token -> never expires
        scope = ("https://www.googleapis.com/auth/adwords openid "
                 "https://www.googleapis.com/auth/userinfo.email")
    else:
        scope = ("https://www.googleapis.com/auth/drive.file "
                 "https://www.googleapis.com/auth/drive.readonly")
    state = f"{purpose}|{company}" if company else purpose   # carry WHICH company is authorising
    if mailbox:
        state = f"{purpose}|{company}|{mailbox}"   # extra mailbox: purpose|company|mailbox -> separate token key
    q = urlencode({"client_id": cid, "redirect_uri": redirect, "response_type": "code",
                   "scope": scope, "state": state, "access_type": "offline",
                   "prompt": "consent", "include_granted_scopes": "true"})
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + q)


@app.get("/oauth/google/callback")
def google_callback(code: str = "", error: str = "", state: str = "") -> HTMLResponse:
    page = lambda msg: HTMLResponse(
        "<body style='background:#0B0F14;color:#EAF2F8;font-family:system-ui;text-align:center;"
        "padding-top:80px'><h2>" + msg + "</h2></body>")
    if error or not code:
        return page("Authorisation cancelled.")
    purpose, _, rest = state.partition("|")   # 'gmail' | 'gmail|sensa' | 'gmail|sensa|gino' (an extra mailbox)
    company, _, mailbox = rest.partition("|")
    company = company.strip().lower(); mailbox = mailbox.strip().lower()
    sfx = (f":{company}" if company else "") + (f":{mailbox}" if mailbox else "")   # legacy keys unchanged
    who = (company.title() if company else "Tabscanner") + (f" / {mailbox}" if mailbox else "")
    cid, secret, redirect = _google_client(company or None)
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        return page("Token exchange failed: " + r.text[:200])
    body = r.json()
    rt = body.get("refresh_token")
    if not rt:
        return page("No refresh token returned — revoke Cortex's access in your Google account and try again.")
    if purpose in ("gmail", "gmail_send"):   # a mailbox — stored separately so it doesn't clobber Drive
        email = ""
        try:
            email = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo",
                              headers={"Authorization": "Bearer " + body.get("access_token", "")},
                              timeout=15).json().get("email", "")
        except Exception:  # noqa: BLE001
            pass
        if purpose == "gmail_send":   # the mailbox replies are SENT from (lands in this account's Sent folder)
            db.setting_set("gmail_send_refresh_token" + sfx, rt)
            db.setting_set("gmail_send_account" + sfx, email)
            return page(f"✓ Cortex will send {who} replies from {email or 'this mailbox'} — "
                        "they'll appear in your Sent folder. You can close this tab.")
        db.setting_set("gmail_refresh_token" + sfx, rt)
        db.setting_set("gmail_account" + sfx, email)
        return page(f"✓ Cortex reads {who} enquiries from the {email or 'Gmail'} mailbox. You can close this tab.")
    if purpose == "analytics":   # per-company GA4 + Search Console token for the SEO/traffic reports
        em = ""
        try:
            em = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo",
                           headers={"Authorization": "Bearer " + body.get("access_token", "")},
                           timeout=15).json().get("email", "")
        except Exception:  # noqa: BLE001
            pass
        db.setting_set("analytics_refresh_token" + sfx, rt)
        db.setting_set("analytics_account" + sfx, em)
        return page(f"✓ Cortex will pull {who}'s SEO reports (GA4 + Search Console) via {em or 'this account'}. "
                    "You can close this tab.")
    if purpose == "calendar":   # per-company Google Calendar (read availability + create booking events)
        em = ""
        try:
            em = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo",
                           headers={"Authorization": "Bearer " + body.get("access_token", "")},
                           timeout=15).json().get("email", "")
        except Exception:  # noqa: BLE001
            pass
        db.setting_set("calendar_refresh_token" + sfx, rt)
        db.setting_set("calendar_account" + sfx, em)
        return page(f"✓ Cortex can read {who}'s calendar availability and propose booking slots "
                    f"via {em or 'this account'}. You can close this tab.")
    if purpose == "google_ads":   # per-company Google Ads API (Keyword Planner) — Internal token, never expires
        em = ""
        try:
            em = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo",
                           headers={"Authorization": "Bearer " + body.get("access_token", "")},
                           timeout=15).json().get("email", "")
        except Exception:  # noqa: BLE001
            pass
        db.setting_set("google_ads_refresh_token" + sfx, rt)
        db.setting_set("google_ads_account" + sfx, em)
        return page(f"✓ Cortex can run Google Ads keyword research for {who} via {em or 'this account'}. "
                    "You can close this tab.")
    if purpose == "youtube":   # per-company YouTube channel access (upload/manage/read/analytics)
        db.setting_set("youtube_refresh_token" + sfx, rt)
        ch_name = ch_id = ""
        try:
            ch = httpx.get("https://www.googleapis.com/youtube/v3/channels",
                           params={"part": "snippet", "mine": "true"},
                           headers={"Authorization": "Bearer " + body.get("access_token", "")},
                           timeout=15).json()
            item = (ch.get("items") or [{}])[0]
            ch_id = item.get("id", "")
            ch_name = ((item.get("snippet") or {}).get("title")) or ""
        except Exception:  # noqa: BLE001
            pass
        if ch_id:
            db.setting_set("youtube_channel_id" + sfx, ch_id)
            db.setting_set("youtube_channel_name" + sfx, ch_name)
        return page(f"✓ Cortex is connected to {who}'s YouTube"
                    + (f" — channel “{ch_name}”." if ch_name else ".")
                    + " You can close this tab.")
    db.setting_set("google_refresh_token" + sfx, rt)
    return page("✓ Cortex is connected to your Google Drive. You can close this tab — backups run nightly.")


# ---- public static assets (e.g. email-signature logos referenced by URL in sent mail) ----
_ASSETS = "/opt/cortex/assets"
if os.path.isdir(_ASSETS):
    app.mount("/assets", StaticFiles(directory=_ASSETS), name="assets")

# ---- serve the cockpit (same origin as the API: no CORS, one domain) ----
# Mounted LAST so the /api/* routes above take precedence; "/" serves web/index.html.
_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web")
if os.path.isdir(_WEB):
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
