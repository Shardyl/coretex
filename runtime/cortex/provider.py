"""Provider adapter — the 'brain'.

Everything in Cortex talks to the model through here, never importing anthropic
directly, so the provider stays swappable (the locked architecture seam).
"""
from __future__ import annotations

import json
import re

import anthropic

from . import config

MODEL = config.get("CORTEX_MODEL", "claude-opus-4-8")          # reasoning / drafting
MODEL_FAST = config.get("CORTEX_MODEL_FAST", "claude-sonnet-4-6")  # judging / JSON


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"))


def think(system: str, user: str, *, fast: bool = False, think_hard: bool = False,
          max_tokens: int = 6000) -> str:
    """One-shot completion → plain text."""
    kwargs: dict = dict(
        model=MODEL_FAST if fast else MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if think_hard:
        kwargs["thinking"] = {"type": "adaptive"}
    resp = _client().messages.create(**kwargs)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def think_json(system: str, user: str, *, fast: bool = True, max_tokens: int = 2000) -> dict:
    """Completion that must return a JSON object → parsed dict."""
    sys = system + "\n\nRespond with ONLY a valid JSON object — no prose, no markdown fences."
    raw = think(sys, user, fast=fast, max_tokens=max_tokens).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*", "", raw).rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}
