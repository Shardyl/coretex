"""Speech to text, in one place.

The cockpit's push-to-talk and WhatsApp voice notes are the same job: bytes of audio in, corrected text
out. This module owns the Deepgram call, the keyterm boost and the brand-name repairs, so both callers
get identical results and the mishearing fixes only ever have to be made once.

`api.py` keeps the HTTP endpoints; the transcription itself lives here.
"""
from __future__ import annotations

import re

import httpx

from . import config

# Boosted at recognition time — Deepgram weights these tokens up, which is why "Sensa" survives at all.
STT_KEYTERMS = ["Sensa", "Sensa Productions", "Sensa Studio", "Tabscanner", "Snap Rewards", "SkyVision",
                "FilmSpoke", "Cortex"]

# Repaired after the fact, because the boost is not perfect: these are the mishearings actually seen.
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


def transcribe(data: bytes, content_type: str = "audio/webm") -> str:
    """Audio bytes -> corrected text (Deepgram Nova-3). Returns "" when nothing was said, rather than
    raising: a silent or unintelligible voice note is a real outcome the callers have to handle, not an
    error. A missing API key DOES raise, because that is a misconfiguration."""
    key = config.require("DEEPGRAM_API_KEY")
    params = [("model", "nova-3"), ("smart_format", "true"), ("punctuate", "true")]
    params += [("keyterm", k) for k in STT_KEYTERMS]
    r = httpx.post("https://api.deepgram.com/v1/listen", params=params,
                   headers={"Authorization": f"Token {key}", "Content-Type": content_type},
                   content=data, timeout=60)
    r.raise_for_status()
    try:
        text = r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError, ValueError):
        text = ""
    return normalize_brand_names(text)
