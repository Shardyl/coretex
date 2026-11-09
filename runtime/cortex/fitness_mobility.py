"""Mobility assessments (owner, 8 Nov 2026): the 8 flexibility pillars from Matthew Smith's toolkit, each with its
tests, results by date and side, and the owner's videos/photos of each test.

- The pillars and their tests are DATA: setting `fitness_mobility_tests`, seeded from DEFAULT_PILLARS the first time
  and never overwritten, so a test is added or reworded without a deploy. Test names are Smith's own.
- Results are server-owned rows (not in the app's sync document), one per day + pillar + test + side + measure.
  Re-saving the same day replaces that day's values; deleting is soft.
- Media is PRIVATE like the progress photos: files on the box, served only to the owner's authenticated app. Videos are
  uploaded in chunks (Cloudflare caps one request at 100 MB) into MEDIA_DIR, which is deliberately OUTSIDE
  /opt/cortex-knowledge: the nightly backup tars that whole folder and keeps 30 copies, so videos there would be
  multiplied thirty times. They are archived to his Google Drive separately (owner: "archive them to Google Drive").
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date

from . import db

MEDIA_DIR = os.environ.get("CORTEX_MOBILITY_MEDIA_DIR") or "/opt/cortex-data/fitness-mobility"
MAX_BYTES = 1_500_000_000          # one clip
CHUNK_MAX = 9_000_000
_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "video/3gpp": "3gp",
          "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}

PIKE_LESSON = "https://www.matthewismith.com/products/mobility-flexibility-toolkit/categories/2156535269/posts/2182813313"
_HAND = ["Beyond Shaka", "Shaka", "Thumbs up", "Bent thumb", "Fist", "4 fingers", "3 fingers", "2 fingers", "1 finger", "Touching"]
_TOUCH = ["Knee", "Upper shin", "Mid shin", "Lower shin", "Top of foot", "Fingertips to floor", "Knuckles to floor",
          "Palms flat", "Beyond palms (raised platform)"]
# Smith's own names for the 10 levels, from the lesson's on-screen list (owner screenshot, 9 Nov 2026)
_NERVE = ["1 Elevate leg", "2 Straighten knee", "3 Dorsiflex", "4 Adduct leg", "5 Internally rotate", "6 Flex hip",
          "7 Flex lumbar", "8 Flex thoracic", "9 Flex cervical", "10 Pull together (pull on the foot)",
          "Clear (no nerve feeling)"]

# A measure may carry "angle": {"mode", "points"} saying how the tap tool reads it: "inside" = the angle at the
# middle point, "flex" = 180 minus that (how far a joint has folded: a fold, read shoulder-hip-ankle; a leg lift is already "inside",
# the angle between the two legs, so both read as hip flexion and compare), "vertical" =
# two points, the line's angle from a plumb line. "pose" (optional) lets Auto-measure find the points in the browser:
# joints = pose-model joints in the same order as points ("other:" the other side's, "mid:" halfway between both,
# "high:"/"low:" whichever of the pair is higher/lower in the frame), side = "visible"
# (one result, the side facing the camera), "each" (L and R) or "lifted" (the leg whose ankle is higher), best = max/min. Units: deg, cm, s. A test's "role" (active/passive) and the
# pillar's "gap" pair make the active vs passive gap for any pillar.
DEFAULT_PILLARS = [
    {"id": "shoulder_flexion", "name": "Shoulder flexion", "tests": []},
    {"id": "shoulder_extension", "name": "Shoulder extension", "tests": []},
    {"id": "pike", "name": "Pike & head to toe", "lesson": PIKE_LESSON,
     "setup": "Barefoot or socks, no shoes, for every test. Test cold, no warm-up. Phone on the floor tilted slightly up, square to your hips; same spot every time. "
              "Camera position 1 for tests 1 to 4 (both sides, same angle); position 2 for test 5.",
     "tests": [
         {"id": "fold", "name": "Standing Forward Fold", "sides": ["both"], "camera": "Position 1, side on",
          "cue": "Stand on a raised platform, feet together, knees straight, fold and slide the hands down. "
                 "Hold the end position for 5 seconds for it to count.",
          "measures": [{"key": "touch", "label": "Reach", "kind": "choice", "options": _TOUCH},
                       {"key": "deg", "label": "Hip flexion", "kind": "number", "unit": "deg",
                        "angle": {"mode": "flex", "points": ["Shoulder", "Hip", "Ankle"],
                                  "pose": {"joints": ["shoulder", "hip", "ankle"], "side": "visible", "best": "max"}}}],
          "role": "passive"},
         {"id": "elbow_head", "name": "Elbow/Head To Toe", "sides": ["L", "R"], "camera": "Position 1, side on",
          "cue": "Front heel down, toes propped on a block, fold over the straight leg: elbow to toe, then head to toe. "
                 "Hold each for 5 seconds for it to count.",
          "measures": [{"key": "elbow", "label": "Elbow gap", "kind": "choice", "options": _HAND},
                       {"key": "head", "label": "Head gap", "kind": "choice", "options": _HAND}]},
         {"id": "leg_lift", "name": "Standing Leg Lift", "sides": ["L", "R"], "camera": "Position 1, side on",
          "cue": "Back to a wall, lift the straight leg as high as you can, no hands. Hold it for 5 seconds for it to "
                 "count. The active side of the pike.",
          "measures": [{"key": "deg", "label": "Hip flexion", "kind": "number", "unit": "deg",
                        "angle": {"mode": "inside", "points": ["Standing foot", "Hip", "Lifted foot"],
                                  "pose": {"joints": ["low:ankle", "mid:hip", "high:ankle"], "side": "lifted", "best": "max"}}}],
          "role": "active"},
         {"id": "nerve", "name": "Sciatic Nerve Level", "sides": ["L", "R"], "camera": "Position 1 (filming optional)",
          "cue": "Foot up on a box. Go through the 10 movements in order, adding one at a time and keeping the ones before. "
                 "Stop at the first nervy sensation and record that number. Level 10: pull on the foot to pull it all "
                 "together. No nervy sensation through all 10 = Clear.",
          "measures": [{"key": "level", "label": "Level reached", "kind": "choice", "options": _NERVE}]},
         {"id": "hip_ir", "name": "Hip Internal Rotation Length Test", "sides": ["L", "R"],
          "camera": "Position 2: camera at BOX HEIGHT, level with the box seat, facing you from the front. This is the only test not filmed from the floor (tests 1 to 4 are filmed with the phone on the floor).",
          "cue": "Sit on a box, knees bent at 90 degrees, a fist or a block between the knees to keep them together "
                 "and still, then swing the foot outward.",
          "measures": [{"key": "deg", "label": "Rotation", "kind": "number", "unit": "deg",
                        "angle": {"mode": "vertical", "points": ["Knee", "Ankle"],
                                  "pose": {"joints": ["knee", "ankle"], "side": "each", "best": "max"}}}]},
     ],
     # the active vs passive gap: the same shape, held by strength vs reached with help (both read as hip flexion)
     "gap": {"passive": {"test": "fold", "measure": "deg"}, "active": {"test": "leg_lift", "measure": "deg"}}},
    {"id": "pancake", "name": "Pancake", "tests": []},
    {"id": "side_split", "name": "Side split", "tests": []},
    {"id": "front_split", "name": "Front split", "tests": []},
    {"id": "bridge", "name": "Bridge", "tests": []},
    {"id": "squat", "name": "Squat", "tests": []},
]


def pillars() -> list:
    cur = db.setting_get("fitness_mobility_tests")
    if not cur:
        db.setting_set("fitness_mobility_tests", DEFAULT_PILLARS)
        cur = DEFAULT_PILLARS
    return cur


def _test(pillar: str, test: str) -> dict | None:
    p = next((p for p in pillars() if p["id"] == pillar), None)
    return next((t for t in (p or {}).get("tests", []) if t["id"] == test), None)


def state() -> dict:
    res = [{"id": r["uid"], "date": r["day"].isoformat(), "pillar": r["pillar"], "test": r["test"], "side": r["side"],
            "measure": r["measure"], "value": float(r["value"]) if r["value"] is not None else None,
            "label": r["label"], "note": r["note"], "provisional": bool(r.get("provisional"))}
           for r in db.query("select * from fitness.mobility_results where not deleted order by day, pillar, test")]
    media = [{"id": r["uid"], "date": r["day"].isoformat(), "pillar": r["pillar"], "test": r["test"], "side": r["side"],
              "mime": r["mime"], "bytes": r["bytes"], "note": r.get("note"),
              "frame_s": float(r["frame_s"]) if r.get("frame_s") is not None else None, "source": r.get("source")}
             for r in db.query("select * from fitness.mobility_media where not deleted and complete order by created_at")]
    return {"pillars": pillars(), "results": res, "media": media}


def save_results(day: str, pillar: str, test: str, side: str, values: dict, note: str = "",
                 provisional: bool = False) -> dict:
    """Store one test's values for a day and side. `values` = {measure_key: number | option label}."""
    d = date.fromisoformat(day)
    t = _test(pillar, test)
    if not t:
        raise ValueError("unknown test")
    if side not in t["sides"]:
        raise ValueError("unknown side")
    db.execute("update fitness.mobility_results set deleted=true, updated_at=now() where day=%s and pillar=%s and "
               "test=%s and side=%s and not deleted", (d, pillar, test, side))
    n = 0
    for m in t["measures"]:
        v = values.get(m["key"])
        if v in (None, ""):
            continue
        num, label = None, None
        if m["kind"] == "choice":
            if v not in m["options"]:
                raise ValueError(f"{m['label']}: not an option")
            label, num = v, m["options"].index(v) + 1          # rank on the ladder, so it can be charted
        else:
            num = float(v)
        db.execute("insert into fitness.mobility_results (uid, day, pillar, test, side, measure, value, label, note, "
                   "provisional) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (f"mr_{secrets.token_hex(6)}", d, pillar, test, side, m["key"], num, label, (note or "")[:300] or None,
                    bool(provisional)))
        n += 1
    return {"ok": True, "saved": n}


def delete_day(day: str, pillar: str) -> dict:
    db.execute("update fitness.mobility_results set deleted=true, updated_at=now() where day=%s and pillar=%s",
               (date.fromisoformat(day), pillar))
    return {"ok": True}


# ---------- media (chunked) ----------

def media_start(day: str, pillar: str, test: str, side: str, mime: str, size: int, note: str = "",
                frame_s: float | None = None, source: str = "") -> dict:
    """`side` may be "both" on a two-sided test: one clip filmed left and right."""
    d = date.fromisoformat(day)
    t = _test(pillar, test)
    if not t:
        raise ValueError("unknown test")
    if side not in t["sides"] and side != "both":
        raise ValueError("unknown side")
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in _TYPES:
        raise ValueError("expected a video or photo")
    if not 0 < int(size) <= MAX_BYTES:
        raise ValueError("file too large (1.5 GB max)")
    os.makedirs(MEDIA_DIR, mode=0o700, exist_ok=True)
    uid = f"mm_{d.isoformat()}_{secrets.token_hex(5)}"
    fname = f"{uid}.{_TYPES[mime]}"
    open(os.path.join(MEDIA_DIR, fname + ".part"), "wb").close()
    db.execute("insert into fitness.mobility_media (uid, day, pillar, test, side, file, mime, bytes, note, frame_s, source) "
               "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (uid, d, pillar, test, side, fname, mime, int(size), (note or "")[:300] or None,
                frame_s, (source or "")[:40] or None))
    return {"ok": True, "id": uid, "chunk": CHUNK_MAX - 1_000_000}


def media_chunk(uid: str, index: int, data: bytes) -> dict:
    r = db.one("select file, bytes, complete from fitness.mobility_media where uid=%s and not deleted", (uid,))
    if not r or r["complete"] or not re.fullmatch(r"mm_[\w-]+", uid):
        raise ValueError("no such upload")
    if len(data) > CHUNK_MAX:
        raise ValueError("chunk too large")
    part = os.path.join(MEDIA_DIR, r["file"] + ".part")
    if index == 0:
        open(part, "wb").close()
    if os.path.getsize(part) + len(data) > int(r["bytes"]):
        raise ValueError("more data than declared")
    with open(part, "ab") as f:
        f.write(data)
    return {"ok": True, "have": os.path.getsize(part)}


def media_finish(uid: str) -> dict:
    r = db.one("select file, bytes from fitness.mobility_media where uid=%s and not deleted", (uid,))
    if not r:
        raise ValueError("no such upload")
    part = os.path.join(MEDIA_DIR, r["file"] + ".part")
    if os.path.getsize(part) != int(r["bytes"]):
        raise ValueError("upload incomplete")
    os.replace(part, os.path.join(MEDIA_DIR, r["file"]))
    db.execute("update fitness.mobility_media set complete=true where uid=%s", (uid,))
    return {"ok": True, "id": uid}


def media_path(uid: str) -> tuple[str, str] | None:
    r = db.one("select file, mime from fitness.mobility_media where uid=%s and not deleted and complete", (uid,))
    if not r:
        return None
    p = os.path.join(MEDIA_DIR, r["file"])
    return (p, r["mime"]) if os.path.isfile(p) else None


def media_delete(uid: str) -> dict:
    """Soft delete: hidden from the app, the file stays on the box (never hard-delete his data unasked)."""
    db.execute("update fitness.mobility_media set deleted=true where uid=%s", (uid,))
    return {"ok": True}
