"""Course library (owner, 14 Nov 2026): Matthew Smith's two toolkits, captured privately on the box, playable inside
the fitness app beside each drill and test.

- Videos (720p) + transcript + frames: /opt/cortex-data/<course dir>/lessons/<slug>/video.mp4. Captured from the
  course he bought; private to him, never public, never in the nightly backup (the 4K masters sit on his Desktop and
  personal Drive).
- Our notes (our own words, one per lesson): /opt/cortex-knowledge/fitness/Matthew Smith/... <slug>.md.
- A <video> tag cannot send the app's bearer token, so the app asks for a SHORT-LIVED signed link (sign()), and the
  video route checks that signature instead (verify()). Starlette's FileResponse serves Range requests, so a 75 MB
  lesson streams and seeks instead of downloading whole.
- A lesson that only exists on YouTube is played from YouTube (the drill or test carries `youtube`), not copied.
"""
from __future__ import annotations

import os
import re

COURSES = {
    "mftk": {"videos": "/opt/cortex-data/fitness-course/lessons",
             "notes": "/opt/cortex-knowledge/fitness/Matthew Smith/lessons", "name": "Mobility & Flexibility Toolkit"},
    "handstand": {"videos": "/opt/cortex-data/handstand-course/lessons",
                  "notes": "/opt/cortex-knowledge/fitness/Matthew Smith/handstand/lessons", "name": "Handstand Toolkit"},
}
LINK_TTL = 6 * 3600                       # a signed video link lasts one long session
_SLUG = re.compile(r"[0-9]{1,3}[a-z]?-[a-z0-9_-]{1,90}")


def _root(course: str, kind: str) -> str:
    c = COURSES.get(course)
    if not c:
        raise ValueError("unknown course")
    return os.environ.get(f"CORTEX_COURSE_{course.upper()}_{kind.upper()}") or c[kind]


def _slug(slug: str) -> str:
    if not _SLUG.fullmatch(slug or ""):
        raise ValueError("bad lesson id")
    return slug


def video_path(course: str, slug: str) -> str | None:
    p = os.path.join(_root(course, "videos"), _slug(slug), "video.mp4")
    return p if os.path.isfile(p) else None


def note(course: str, slug: str) -> str | None:
    p = os.path.join(_root(course, "notes"), _slug(slug) + ".md")
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else None


def lesson(course: str, slug: str) -> dict:
    """What the app needs to show one lesson: whether a video exists and our note."""
    return {"course": course, "slug": _slug(slug), "has_video": video_path(course, slug) is not None,
            "note": note(course, slug)}


def payload(course: str, slug: str, exp: int) -> str:
    return f"course:{course}:{_slug(slug)}:{int(exp)}"
