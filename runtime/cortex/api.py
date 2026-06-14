"""Cortex cockpit API — the HTTP surface the PWA (and later voice) talk to.

Reuses the same engine/store/db as the always-on engine service: this process exposes
read views (companies, skills, the approval inbox, the decision log) and write actions
(create a task, approve / skip / correct). The engine service still does the heavy lifting
(drafting new tasks, polling Telegram); the API just lets the cockpit drive the same loop.

Auth: a single operator passcode -> a signed, expiring bearer token (single-operator app;
proper multi-user logins come with the PA/PM roles later). Passcode in CORTEX_PASSCODE.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, db, engine, store

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


def _make_token() -> str:
    exp = str(int(time.time()) + TOKEN_TTL)
    return f"{exp}.{_sign(exp)}"


def _valid_token(token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    return int(exp) > time.time()


def auth(authorization: str = Header(default="")) -> None:
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if not _valid_token(token):
        raise HTTPException(status_code=401, detail="not authenticated")


class Login(BaseModel):
    passcode: str


@app.post("/api/login")
def login(body: Login) -> dict:
    expected = config.get("CORTEX_PASSCODE")
    if not expected:
        raise HTTPException(status_code=503, detail="passcode not configured on the server")
    if not hmac.compare_digest(body.passcode.strip(), expected.strip()):
        raise HTTPException(status_code=401, detail="wrong passcode")
    return {"token": _make_token(), "ttl": TOKEN_TTL}


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


# ---------- read views ----------

@app.get("/api/companies")
def companies(_: None = Depends(auth)) -> list[dict]:
    return db.query("select * from companies order by name")


@app.get("/api/companies/{slug}/skills")
def skills(slug: str, _: None = Depends(auth)) -> list[dict]:
    co = store.get_company_by_slug(slug)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    return db.query("select * from skills where company_id=%s order by name", (co["id"],))


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


@app.get("/api/inbox")
def inbox(_: None = Depends(auth)) -> list[dict]:
    return db.query("select * from tasks where status in ('awaiting_approval','awaiting_correction') order by id desc")


@app.get("/api/tasks/{task_id}")
def task(task_id: int, _: None = Depends(auth)) -> dict:
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="no such task")
    t["wp"] = db.setting_get(f"wp:{task_id}")
    return t


@app.get("/api/decisions")
def decisions(limit: int = 50, _: None = Depends(auth)) -> list[dict]:
    return db.query("select * from decisions order by id desc limit %s", (limit,))


# ---------- write actions ----------

class NewTask(BaseModel):
    company: str
    skill: str
    kind: str = "content"
    brief: str


@app.post("/api/tasks")
def create_task(body: NewTask, _: None = Depends(auth)) -> dict:
    co = store.get_company_by_slug(body.company)
    if not co:
        raise HTTPException(status_code=404, detail="no such company")
    sk = store.get_skill_by_key(co["id"], body.skill)
    if not sk:
        raise HTTPException(status_code=404, detail="no such skill")
    t = store.create_task(co["id"], sk["id"], body.kind, {"brief": body.brief})
    return {"ok": True, "task": t}  # the engine service will draft it on its next poll


class Correction(BaseModel):
    text: str


@app.post("/api/tasks/{task_id}/approve")
def approve(task_id: int, _: None = Depends(auth)) -> dict:
    return engine.approve_task(task_id)


@app.post("/api/tasks/{task_id}/skip")
def skip(task_id: int, _: None = Depends(auth)) -> dict:
    return engine.skip_task(task_id)


@app.post("/api/tasks/{task_id}/correct")
def correct(task_id: int, body: Correction, _: None = Depends(auth)) -> dict:
    return engine.correct_task(task_id, body.text)
