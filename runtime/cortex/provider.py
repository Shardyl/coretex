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
MODEL_ROUTER = config.get("CORTEX_MODEL_ROUTER", "claude-haiku-4-5")  # routing / chat naming (cheap+fast)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"))


def resolve_model(tier: str | None) -> str:
    """Map a skill's model tier ('opus'|'sonnet'|None) to a concrete model id."""
    if tier == "opus":
        return MODEL
    if tier == "sonnet":
        return MODEL_FAST
    if tier == "haiku":
        return MODEL_ROUTER
    return ""   # caller decides the default


def think(system: str, user: str, *, fast: bool = False, model: str | None = None,
          think_hard: bool = False, max_tokens: int = 6000) -> str:
    """One-shot completion → plain text. `model` overrides the fast/slow default when given."""
    kwargs: dict = dict(
        model=model or (MODEL_FAST if fast else MODEL),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if think_hard:
        kwargs["thinking"] = {"type": "adaptive"}
    resp = _client().messages.create(**kwargs)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def chat(system: str, messages: list[dict], *, max_tokens: int = 1000) -> str:
    """Multi-turn conversation → assistant text. Snappy (no extended thinking) for voice back-and-forth."""
    resp = _client().messages.create(model=MODEL, max_tokens=max_tokens, system=system, messages=messages)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def chat_tools(system: str, messages: list[dict], tools: list[dict], executor,
               *, max_tokens: int = 1500, rounds: int = 6) -> str:
    """Conversation where Claude can call tools to read/act on Cortex (e.g. manage skills).

    `executor(name, input) -> str` runs the tool and returns a result string. Loops until Claude
    stops calling tools or `rounds` is hit, then returns the final assistant text.
    """
    client = _client()
    msgs = [dict(m) for m in messages]
    resp = None
    for _ in range(rounds):
        resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                       tools=tools, messages=msgs)
        if resp.stop_reason != "tool_use":
            break
        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                try:
                    out = executor(b.name, b.input or {})
                except Exception as e:  # noqa: BLE001
                    out = f"error: {e}"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)})
        msgs.append({"role": "user", "content": results})
    if not resp:
        return ""
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def think_json(system: str, user: str, *, fast: bool = True, model: str | None = None,
               max_tokens: int = 2000) -> dict:
    """Completion that must return a JSON object → parsed dict."""
    sys = system + "\n\nRespond with ONLY a valid JSON object — no prose, no markdown fences."
    raw = think(sys, user, fast=fast, model=model, max_tokens=max_tokens).strip()
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
