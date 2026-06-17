"""Gemini Imagen hero images for newsletters. Returns JPEG bytes, or None on any failure — the hero
is OPTIONAL (the newsletter must read fully with images off), so callers degrade gracefully.
"""
from __future__ import annotations

import base64

import httpx

from . import config

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "imagen-4.0-generate-001:predict")


def hero(prompt: str, aspect: str = "16:9") -> bytes | None:
    key = config.get("GEMINI_API_KEY")
    if not key or not prompt:
        return None
    try:
        r = httpx.post(ENDPOINT, params={"key": key}, timeout=120, json={
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": aspect}})
        r.raise_for_status()
        preds = r.json().get("predictions", [])
        b64 = preds[0].get("bytesBase64Encoded") if preds else None
        return base64.b64decode(b64) if b64 else None
    except Exception:  # noqa: BLE001 — hero is optional; never block a send on image-gen
        return None
