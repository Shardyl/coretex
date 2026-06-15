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
import secrets
import time

import httpx
import websockets
from fastapi import (Depends, FastAPI, File, Header, HTTPException, Response,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import catalog, config, db, engine, gmail, knowledge, personas, profile, provider, questionnaire, skillqa, store, worker

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


# ---------- read views ----------

@app.get("/api/companies")
def companies(_: None = Depends(auth)) -> list[dict]:
    return db.query("select * from companies order by name")


@app.get("/api/companies/{slug}/skills")
def skills(slug: str, _: None = Depends(auth)) -> list[dict]:
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


@app.get("/api/inbox")
def inbox(company: str | None = None, _: None = Depends(auth)) -> list[dict]:
    where = "status in ('awaiting_approval','awaiting_correction')"
    params: list = []
    if company:
        co = store.get_company_by_slug(company)
        where += " and company_id = %s"
        params.append(co["id"] if co else -1)
    rows = db.query(f"select * from tasks where {where} order by id desc", tuple(params))
    for t in rows:
        t["wp"] = db.setting_get(f"wp:{t['id']}")   # preview/edit links for blog drafts
        if t["kind"] in engine.EMAIL_KINDS:         # email replies: show the original enquiry + send envelope
            co = store.get_company(t["company_id"])
            env = engine._email_envelope(t, co)
            inq = (t.get("request") or {}).get("inquiry") or {}
            t["email"] = {**env, "inquiry": {"name": inq.get("name"), "email": inq.get("email"),
                                             "message": inq.get("message") or inq.get("snippet") or ""}}
        sk = store.get_skill(t["skill_id"])         # the lane's autonomy state for the Inbox UI
        if sk:
            offer = (sk["authority"] == "ask" and sk["trust_streak"] >= sk["auto_threshold"]
                     and t["kind"] != "blog" and sk["stakes"] == "low")
            t["lane"] = {"skill_id": sk["id"], "name": sk["name"], "trust_streak": sk["trust_streak"],
                         "auto_threshold": sk["auto_threshold"], "authority": sk["authority"],
                         "stakes": sk["stakes"], "auto_offer": offer}
    return rows


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


# ---- voice: speech-to-text (Deepgram) + text-to-speech (ElevenLabs Flash) ----

@app.post("/api/voice/stt")
def stt(audio: UploadFile = File(...), _: None = Depends(auth)) -> dict:
    """Transcribe a recorded audio clip -> text (Deepgram Nova-3)."""
    key = config.require("DEEPGRAM_API_KEY")
    data = audio.file.read()
    ct = audio.content_type or "audio/webm"
    r = httpx.post(
        "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&punctuate=true",
        headers={"Authorization": f"Token {key}", "Content-Type": ct}, content=data, timeout=60)
    r.raise_for_status()
    j = r.json()
    try:
        text = j["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError):
        text = ""
    return {"text": text}


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
    "FilmSpoke. You are warm, sharp and concise. Your replies are usually read aloud, so write the "
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
     "description": "Produce a draft NOW using a skill — applies that skill's craft + standing rules + the company voice. Returns the draft text to show Rashad. Use for replies, posts, copy.",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string"}, "skill": {"type": "string", "description": "skill_key"},
        "brief": {"type": "string"}, "revision": {"type": "string", "description": "optional: how to change a previous draft"}},
        "required": ["company", "skill", "brief"]}},
    {"name": "create_task",
     "description": "Queue a task; the engine drafts it and it lands in the Inbox + Telegram for approval.",
     "input_schema": {"type": "object", "properties": {
        "company": {"type": "string"}, "skill": {"type": "string"},
        "kind": {"type": "string", "description": "content (default) or blog"}, "brief": {"type": "string"}},
        "required": ["company", "skill", "brief"]}},
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
        text = worker.draft(sk, co, {"brief": inp.get("brief", "")}, correction=inp.get("revision"))
        return "DRAFT (skill craft + rules applied):\n\n" + text
    if name == "create_task":
        sk = store.get_skill_by_key(co["id"], inp.get("skill", ""))
        if not sk:
            return f"no skill '{inp.get('skill')}' for {co['slug']}"
        t = store.create_task(co["id"], sk["id"], inp.get("kind", "content"), {"brief": inp.get("brief", "")})
        return f"created task #{t['id']} — drafting now; it'll appear in the Inbox and Telegram."
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
    return CHAT_SYSTEM_BASE + "\n\n" + note


# A Chief CAN grow the org — create_skill is global-by-nature (added to every company), so there's no
# scope to bleed. But the scoped, bleed-risky part — writing per-company RULES (add_rule/update_craft)
# — stays with the Manager (one keeper of rules). Managers + general Cortex get the full set.
_CHIEF_TOOLS = {"system_knowledge", "list_skills", "list_tasks", "get_task", "create_skill"}


@app.get("/api/heads")
def heads(_: None = Depends(auth)) -> dict:
    """The org as personas you can talk to: Chiefs (strategy) + Managers (standards)."""
    return personas.org()


def _image_blocks(data_urls: list[str]) -> list[dict]:
    """Turn data: URLs (data:image/jpeg;base64,...) into Anthropic image content blocks."""
    blocks = []
    for u in (data_urls or [])[:6]:
        if not isinstance(u, str) or not u.startswith("data:") or ";base64," not in u:
            continue
        head, b64 = u.split(";base64,", 1)
        media = head[5:] or "image/jpeg"
        if media.startswith("image/"):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    return blocks


class ChatTurn(BaseModel):
    messages: list[dict]
    persona: str | None = None     # PINNED head ('' / None = auto-route). 'chief:Demand', 'manager:Content & SEO'
    company: str | None = None     # company slug in focus (the global selector)
    current: str | None = None     # who handled the last turn (sticky auto-routing)
    images: list[str] | None = None  # attached photos/files as data: URLs (multimodal context)


@app.post("/api/chat")
def chat(body: ChatTurn, _: None = Depends(auth)) -> dict:
    msgs = [{"role": m.get("role"), "content": m.get("content", "")} for m in body.messages
            if m.get("content") and m.get("role") in ("user", "assistant")][-20:]
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
            system = psys
            tools = [t for t in SKILL_TOOLS if t["name"] in _CHIEF_TOOLS] if is_chief else SKILL_TOOLS
        else:
            chosen = ""
    reply = provider.chat_tools(system, msgs, tools, _exec_skill_tool)
    return {"reply": reply, "persona": chosen, "persona_label": personas.label(chosen)}


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
    if not _valid_token(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    rate = ws.query_params.get("rate", "48000")
    await ws.accept()
    key = config.require("DEEPGRAM_API_KEY")
    url = ("wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16"
           f"&sample_rate={rate}&channels=1&interim_results=true&smart_format=true&punctuate=true"
           "&endpointing=300")  # finalize words promptly; the cockpit ends the turn on a silence timer
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
                    text = alts[0].get("transcript", "")
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


def _google_client() -> tuple[str, str, str]:
    with open(GOOGLE_OAUTH_CLIENT) as f:
        c = (json.load(f).get("web") or {})
    return c["client_id"], c["client_secret"], (c.get("redirect_uris") or [""])[0]


@app.get("/oauth/google/start")
def google_start(purpose: str = "drive") -> RedirectResponse:
    from urllib.parse import urlencode
    cid, _, redirect = _google_client()
    if purpose in ("gmail", "gmail_send"):   # a Tabscanner mailbox — its own login, stored separately.
        # gmail.modify = read + draft + SEND + label/archive (NOT permanent delete). One consent, full flow;
        # nothing is ever sent without the owner's approval — the guardrail is the approval gate, not the scope.
        # 'gmail'      = the inbox Cortex READS enquiries from (api@tabscanner.com).
        # 'gmail_send' = the mailbox Cortex SENDS replies from, so they land in YOUR Sent folder, as you.
        scope = ("https://www.googleapis.com/auth/gmail.modify openid "
                 "https://www.googleapis.com/auth/userinfo.email")
    else:
        scope = ("https://www.googleapis.com/auth/drive.file "
                 "https://www.googleapis.com/auth/drive.readonly")
    q = urlencode({"client_id": cid, "redirect_uri": redirect, "response_type": "code",
                   "scope": scope, "state": purpose, "access_type": "offline",
                   "prompt": "consent", "include_granted_scopes": "true"})
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + q)


@app.get("/oauth/google/callback")
def google_callback(code: str = "", error: str = "", state: str = "") -> HTMLResponse:
    page = lambda msg: HTMLResponse(
        "<body style='background:#0B0F14;color:#EAF2F8;font-family:system-ui;text-align:center;"
        "padding-top:80px'><h2>" + msg + "</h2></body>")
    if error or not code:
        return page("Authorisation cancelled.")
    cid, secret, redirect = _google_client()
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        return page("Token exchange failed: " + r.text[:200])
    body = r.json()
    rt = body.get("refresh_token")
    if not rt:
        return page("No refresh token returned — revoke Cortex's access in your Google account and try again.")
    if state in ("gmail", "gmail_send"):   # a Tabscanner mailbox — stored separately so it doesn't clobber Drive
        email = ""
        try:
            email = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo",
                              headers={"Authorization": "Bearer " + body.get("access_token", "")},
                              timeout=15).json().get("email", "")
        except Exception:  # noqa: BLE001
            pass
        if state == "gmail_send":   # the mailbox replies are SENT from (lands in this account's Sent folder)
            db.setting_set("gmail_send_refresh_token", rt)
            db.setting_set("gmail_send_account", email)
            return page(f"✓ Cortex will send replies from {email or 'this mailbox'} — "
                        "they'll appear in your Sent folder. You can close this tab.")
        db.setting_set("gmail_refresh_token", rt)
        db.setting_set("gmail_account", email)
        return page(f"✓ Cortex reads enquiries from the {email or 'Gmail'} mailbox. You can close this tab.")
    db.setting_set("google_refresh_token", rt)
    return page("✓ Cortex is connected to your Google Drive. You can close this tab — backups run nightly.")


# ---- serve the cockpit (same origin as the API: no CORS, one domain) ----
# Mounted LAST so the /api/* routes above take precedence; "/" serves web/index.html.
_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web")
if os.path.isdir(_WEB):
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
