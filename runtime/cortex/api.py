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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, engine, provider, store

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
    voice = config.get("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"
    text = (body.text or "").strip()[:2500]
    if not text:
        raise HTTPException(status_code=400, detail="nothing to say")
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=60)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"tts failed: {r.status_code} {r.text[:200]}")
    return Response(content=r.content, media_type="audio/mpeg")


# ---- chat: talk to Cortex (conversational brain) ----

CHAT_SYSTEM = (
    "You are Cortex, Rashad's voice-first AI operations partner. You help him run his businesses: "
    "Tabscanner (receipt-OCR / data-extraction API), Sensa (AI video production), SkyVision, and "
    "FilmSpoke. You are warm, sharp and concise. Your replies are usually read aloud, so write the "
    "way you'd speak: natural sentences, no markdown, no bullet lists, no headings, and keep it brief "
    "unless he asks for depth. Answer directly, help him think, draft copy when asked, and talk through "
    "decisions. If he wants you to actually publish or create a piece of content as a task, tell him to "
    "use the Ask tab for now (that runs it through the draft-and-approve flow)."
)


class ChatTurn(BaseModel):
    messages: list[dict]


@app.post("/api/chat")
def chat(body: ChatTurn, _: None = Depends(auth)) -> dict:
    msgs = [{"role": m.get("role"), "content": m.get("content", "")} for m in body.messages
            if m.get("content") and m.get("role") in ("user", "assistant")][-20:]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="last message must be from the user")
    return {"reply": provider.chat(CHAT_SYSTEM, msgs)}


# ---- voice: live streaming transcription (browser mic -> us -> Deepgram -> back) ----

@app.websocket("/api/voice/stream")
async def voice_stream(ws: WebSocket):
    if not _valid_token(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    rate = ws.query_params.get("rate", "48000")
    await ws.accept()
    key = config.require("DEEPGRAM_API_KEY")
    try:
        gap = max(600, int(ws.query_params.get("gap", "1500")))  # ms of silence that ends a turn
    except ValueError:
        gap = 1500
    url = ("wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16"
           f"&sample_rate={rate}&channels=1&interim_results=true&smart_format=true&punctuate=true"
           f"&endpointing={gap}")  # speech_final fires after `gap` ms of silence = end of your turn
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


# ---- serve the cockpit (same origin as the API: no CORS, one domain) ----
# Mounted LAST so the /api/* routes above take precedence; "/" serves web/index.html.
_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web")
if os.path.isdir(_WEB):
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
