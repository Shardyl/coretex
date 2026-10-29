"""YOUTUBE UPLOADS INTO THE MEDIA LIBRARY (29 Oct 2026): a film sent from a team member's computer (through the Claude
connector's upload link) goes to the company's own YouTube channel as PRIVATE or UNLISTED, and lands in
`media_assets` in the same pass, transcribed and profiled, so Cortex can match it to enquiries straight away.

- The channel is the company's own `youtube_refresh_token:<slug>` (minted by its own OAuth client, upload scope).
- Nothing is made PUBLIC here, and a PUBLIC video's title or description is never changed here: those are
  `youtube_change` cards, approved by the owner at the step-up gate (engine._execute applies them).
- What a good title and description look like is the `social-youtube` skill's rules, not code: the connector hands
  those rules to whoever is writing them.
- The rating is the owner's alone and is never set here (an unrated film ranks below every rated one).

Jobs live in `connector_uploads` (connector.py). The API receives the file; the ENGINE runs the job on its own
thread (`kick`), so a deploy that restarts the API never loses one, and a job cut off mid-run is re-queued at
engine start (`recover`) and skips the YouTube upload when the video id was already recorded.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time

import httpx

from . import db, store

API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 64 * 256 * 1024          # 16 MB, a multiple of 256 KB as the resumable protocol requires
PRIVACY_HERE = ("private", "unlisted")
_thread: threading.Thread | None = None

PROFILE_SYSTEM = """You profile films from a production company's catalog.
You get: title, duration, the full audio transcript (may be empty = music only), and 5 frames.
Return JSON:
{"summary": "2-3 sentences: what the film is, what happens, its message",
 "industry": "client industry (automotive/F&B/government/real-estate/beauty/tech/hospitality/retail/healthcare/education/energy/fashion/entertainment/other)",
 "format": "commercial|corporate-film|event-aftermovie|animation|stop-motion|music-video|testimonial|product-video|recruitment|documentary|showreel|social-cut",
 "style": ["up to 4: cinematic, drone-led, interview-led, motion-graphics, ai-built, vfx-heavy, handheld, studio, lifestyle, luxury, fast-cut, emotional, humorous"],
 "language": "en|ar|mixed|none",
 "has_voiceover": bool,
 "suggested_rating": 1-5 craft quality (5 = flagship portfolio piece, 1 = rough/draft quality)}"""


# ---------------------------------------------------------------- the channel

def access_token(slug: str) -> str:
    rt = db.setting_get(f"youtube_refresh_token:{slug}")
    if not rt:
        raise ValueError(f"No YouTube channel is connected for {slug}.")
    with open(f"/etc/cortex/google_oauth_client_{slug}.json") as f:
        c = json.load(f).get("web") or {}
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def channel_name(slug: str) -> str:
    return db.setting_get(f"youtube_channel_name:{slug}") or slug


def _house_category(company_id: int, tok: str) -> str:
    """The category the channel already uses (its newest library film), so an upload files like the rest."""
    r = db.one("select youtube_video_id from media_assets where company_id=%s and status='live' "
               "and youtube_video_id is not null order by id desc limit 1", (company_id,))
    if r:
        try:
            it = httpx.get(f"{API}/videos", params={"part": "snippet", "id": r["youtube_video_id"]},
                           headers={"Authorization": f"Bearer {tok}"}, timeout=20).json().get("items") or []
            if it and it[0]["snippet"].get("categoryId"):
                return it[0]["snippet"]["categoryId"]
        except Exception:  # noqa: BLE001
            pass
    return "1"                    # Film & Animation


def upload(slug: str, company_id: int, path: str, mime: str, title: str, description: str,
           tags: list | None, privacy: str) -> str:
    """Resumable upload of a local file to the company's channel. Returns the video id."""
    if privacy not in PRIVACY_HERE:
        raise ValueError("an upload goes up private or unlisted; making it public is an approval card")
    tok = access_token(slug)
    size = os.path.getsize(path)
    meta = {"snippet": {"title": title[:100], "description": (description or "")[:4900],
                        "tags": [str(t)[:60] for t in (tags or [])][:30],
                        "categoryId": _house_category(company_id, tok)},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    init = httpx.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                      headers={"Authorization": f"Bearer {tok}", "X-Upload-Content-Length": str(size),
                               "X-Upload-Content-Type": mime or "video/*"}, timeout=60)
    if init.status_code >= 300:
        raise RuntimeError(f"YouTube refused the upload: {init.status_code} {init.text[:300]}")
    loc = init.headers["Location"]
    sent, fails = 0, 0
    with open(path, "rb") as f:
        while sent < size:
            f.seek(sent)
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            try:
                r = httpx.put(loc, content=chunk, timeout=300, headers={
                    "Authorization": f"Bearer {tok}", "Content-Range": f"bytes {sent}-{end}/{size}"})
            except httpx.HTTPError:
                r = None
            if r is not None and r.status_code in (200, 201):
                return r.json()["id"]
            if r is not None and r.status_code == 308:
                rng = r.headers.get("Range")             # what YouTube actually holds, e.g. "bytes=0-16777215"
                sent = int(rng.split("-")[1]) + 1 if rng else 0
                fails = 0
                continue
            if r is not None and r.status_code < 500:
                raise RuntimeError(f"YouTube upload failed: {r.status_code} {r.text[:300]}")
            fails += 1                                   # 5xx or a dropped connection: ask where it got to
            if fails > 6:
                raise RuntimeError("YouTube upload kept failing; try again later")
            time.sleep(min(60, 2 ** fails))
            if fails % 3 == 0:
                tok = access_token(slug)
            q = httpx.put(loc, content=b"", timeout=60, headers={
                "Authorization": f"Bearer {tok}", "Content-Range": f"bytes */{size}"})
            if q.status_code in (200, 201):
                return q.json()["id"]
            rng = q.headers.get("Range") if q.status_code == 308 else None
            sent = int(rng.split("-")[1]) + 1 if rng else 0
    raise RuntimeError("upload ended without a video id")


def update(slug: str, video_id: str, *, title: str | None = None, description: str | None = None,
           tags: list | None = None, privacy: str | None = None) -> dict:
    """Change a video's snippet and/or privacy on YouTube, keeping every field not named."""
    tok = access_token(slug)
    h = {"Authorization": f"Bearer {tok}"}
    it = httpx.get(f"{API}/videos", params={"part": "snippet,status", "id": video_id}, headers=h,
                   timeout=20).json().get("items") or []
    if not it:
        raise ValueError(f"Video {video_id} is not on the {channel_name(slug)} channel.")
    sn, st = it[0]["snippet"], it[0]["status"]
    body: dict = {"id": video_id}
    parts = []
    if title is not None or description is not None or tags is not None:
        snippet = {"title": (title if title is not None else sn.get("title", ""))[:100],
                   "description": (description if description is not None else sn.get("description", ""))[:4900],
                   "tags": [str(t)[:60] for t in (tags if tags is not None else sn.get("tags", []))][:30],
                   "categoryId": sn.get("categoryId", "1")}
        for k in ("defaultLanguage", "defaultAudioLanguage"):
            if sn.get(k):
                snippet[k] = sn[k]
        body["snippet"] = snippet
        parts.append("snippet")
    if privacy is not None:
        body["status"] = {"privacyStatus": privacy,
                          "selfDeclaredMadeForKids": st.get("selfDeclaredMadeForKids", False)}
        parts.append("status")
    if not parts:
        return {}
    r = httpx.put(f"{API}/videos", params={"part": ",".join(parts)}, json=body, headers=h, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"YouTube refused the change: {r.status_code} {r.text[:300]}")
    return r.json()


def record_change(video_id: str, yt: dict) -> None:
    """Mirror a change made on YouTube into media_assets (the first title is kept in orig_title)."""
    sets, vals = [], []
    if yt.get("title") is not None:
        sets += ["orig_title=coalesce(orig_title, title)", "title=%s"]
        vals.append(str(yt["title"])[:100])
    if yt.get("description") is not None:
        sets.append("description=%s")
        vals.append(yt["description"])
    if yt.get("tags") is not None:
        sets.append("tags=%s")
        vals.append(json.dumps(yt["tags"]))
    if yt.get("privacy"):
        sets.append("privacy=%s")
        vals.append(yt["privacy"])
    if sets:
        db.execute(f"update media_assets set {', '.join(sets)}, updated_at=now() where youtube_video_id=%s",
                   (*vals, video_id))


# ---------------------------------------------------------------- understanding the film (locally, from the file)

def _probe_seconds(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=120)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def iso_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return "PT" + (f"{h}H" if h else "") + f"{m}M{s}S"


def enrich(path: str, title: str, slug: str, work: str) -> dict:
    """Transcript + 5-frame profile from the file itself (the box cannot download from YouTube)."""
    from . import provider, voice
    secs = _probe_seconds(path)
    audio = os.path.join(work, "a.m4a")
    transcript = ""
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
                        audio], check=True, timeout=1800)
        if os.path.getsize(audio) > 1000:
            with open(audio, "rb") as f:
                transcript = voice.transcribe(f.read(), "audio/mp4", timeout=600)
    except Exception as e:  # noqa: BLE001 - a film with no sound track is still a film
        print(f"[youtube] no transcript for {title}: {e}", flush=True)
    imgs = []
    for i, frac in enumerate((0.1, 0.3, 0.5, 0.7, 0.9)):
        jp = os.path.join(work, f"f{i}.jpg")
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{secs * frac:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=768:-2", jp], check=True, timeout=300)
            with open(jp, "rb") as f:
                imgs.append("data:image/jpeg;base64," + base64.b64encode(f.read()).decode())
        except Exception:  # noqa: BLE001
            pass
    prof = {}
    if imgs or transcript:
        prof = provider.think_json(PROFILE_SYSTEM, json.dumps({
            "title": title, "duration": iso_duration(secs), "transcript": transcript[:6000]}),
            model=provider.MODEL_ROUTER, max_tokens=800, purpose="youtube-catalog-enrich", company=slug,
            images=imgs)
    return {"seconds": secs, "transcript": transcript, "profile": prof}


# ---------------------------------------------------------------- the job

def run_job(job: dict) -> dict:
    """One uploaded film: to YouTube (unless already there), into media_assets, understood. Idempotent."""
    from . import connector, notifications
    meta, res = job.get("meta") or {}, dict(job.get("result") or {})
    co = store.get_company(job["company_id"]) or {}
    slug = co.get("slug") or ""
    path = job["path"]
    work = path + ".work"
    os.makedirs(work, exist_ok=True)
    if not res.get("video_id"):
        connector.job_update(job["id"], status="processing", result={**res, "step": "uploading to YouTube"})
        res["video_id"] = upload(slug, co["id"], path, meta.get("mime") or "video/*", meta["title"],
                                 meta.get("description") or "", meta.get("tags"), meta.get("privacy") or "unlisted")
        res["watch_url"] = f"https://www.youtube.com/watch?v={res['video_id']}"
        connector.job_update(job["id"], result={**res, "step": "adding to the media library"})
    vid = res["video_id"]
    db.execute(
        "insert into media_assets (company_id, youtube_video_id, watch_url, title, description, privacy, published_at, "
        "thumb_url, tags, content_type, client, categories, ai_production, status, source, orig_title) "
        "values (%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,'live',%s,%s) on conflict (youtube_video_id) do nothing",
        (co["id"], vid, res["watch_url"], meta["title"][:100], meta.get("description") or "",
         meta.get("privacy") or "unlisted", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
         json.dumps(meta.get("tags") or []), meta.get("content_type") or "client-film", meta.get("client") or None,
         json.dumps(meta.get("categories") or []), bool(meta.get("ai_production")),
         f"connector:{job['user_email']}", meta.get("filename")))
    connector.job_update(job["id"], result={**res, "step": "transcribing and profiling the film"})
    try:
        e = enrich(path, meta["title"], slug, work)
        prof = e["profile"] or {}
        db.execute("update media_assets set duration=%s, transcript=%s, profile=%s, suggested_rating=%s, "
                   "enriched_at=now(), updated_at=now() where youtube_video_id=%s",
                   (iso_duration(e["seconds"]) if e["seconds"] else None, e["transcript"] or None,
                    json.dumps(prof) if prof else None,
                    (int(prof.get("suggested_rating") or 0) * 2) or None, vid))
        res["profiled"] = bool(prof)
    except Exception as ex:  # noqa: BLE001 - the film is up and in the library either way
        res["profile_error"] = f"{type(ex).__name__}: {ex}"
    row = db.one("select id from media_assets where youtube_video_id=%s", (vid,)) or {}
    res.update({"media_id": row.get("id"), "step": "done"})
    connector.job_update(job["id"], status="done", result=res)
    try:
        notifications.notify(f"New film on YouTube ({meta.get('privacy')}): {meta['title']}",
                             f"Uploaded by {job['user_email']} to {channel_name(slug)} and added to the media "
                             f"library, unrated. {res['watch_url']}", company_id=co["id"], category="media",
                             dedup_key=f"ytupload:{vid}")
    except Exception:  # noqa: BLE001
        pass
    _cleanup(path, work)
    return res


def _cleanup(path: str, work: str) -> None:
    import shutil
    try:
        os.remove(path)
    except OSError:
        pass
    shutil.rmtree(work, ignore_errors=True)


def recover() -> int:
    """At engine start: a job that was mid-run when the engine stopped goes back on the queue."""
    from . import connector
    connector.ensure_schema()
    return len(db.query("update connector_uploads set status='queued', updated_at=now() "
                        "where kind='video' and status='processing' returning id"))


def kick() -> None:
    """Drain queued video jobs on one background thread (never on the engine loop: an upload takes minutes)."""
    global _thread
    from . import connector
    if _thread is not None and _thread.is_alive():
        return
    connector.ensure_schema()
    if not db.one("select 1 from connector_uploads where kind='video' and status='queued' limit 1"):
        return

    def _drain() -> None:
        while True:
            job = db.one("update connector_uploads set status='processing', updated_at=now() where id = ("
                         "select id from connector_uploads where kind='video' and status='queued' "
                         "order by id limit 1 for update skip locked) returning *")
            if not job:
                return
            try:
                run_job(job)
            except Exception as e:  # noqa: BLE001
                print(f"[youtube] job {job['id']} failed: {type(e).__name__}: {e}", flush=True)
                connector.job_update(job["id"], status="failed", error=f"{type(e).__name__}: {e}")
                _cleanup(job["path"], job["path"] + ".work")     # a new upload link starts it again

    _thread = threading.Thread(target=_drain, name="cortex-youtube", daemon=True)
    _thread.start()
