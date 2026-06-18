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
    # Bound the per-attempt timeout (SDK default is 600s) so an overloaded call fails fast instead of
    # holding a request open for minutes; keep a couple of retries for transient 429/529.
    return anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"), timeout=90.0, max_retries=2)


# ---- cost logging: every model call records exact tokens + $ (input, output, cache) ----
# $/token (input, output, cache-write, cache-read)
PRICES = {
    "claude-opus-4-8":   (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 0.5 / 1e6),
    "claude-sonnet-4-6": (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 0.3 / 1e6),
    "claude-haiku-4-5":  (1 / 1e6, 5 / 1e6, 1.25 / 1e6, 0.1 / 1e6),
}


def _log_usage(model: str, usage, purpose: str, company: str | None) -> None:
    """Record one model call's exact token usage + computed cost. Never raises into the caller."""
    if usage is None:
        return
    try:
        from . import db
        i = getattr(usage, "input_tokens", 0) or 0
        o = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        pi, po, pcw, pcr = PRICES.get(model, (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 0.5 / 1e6))
        cost = i * pi + o * po + cw * pcw + cr * pcr
        db.execute("insert into usage_log (model, purpose, company, input_tokens, output_tokens, "
                   "cache_write, cache_read, cost_usd) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                   (model, purpose, company, i, o, cw, cr, cost))
    except Exception:  # noqa: BLE001 — logging must never break a call
        pass


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
          think_hard: bool = False, max_tokens: int = 6000, purpose: str = "think",
          company: str | None = None, cache: bool = False) -> str:
    """One-shot completion → plain text. `model` overrides the fast/slow default when given.
    `cache=True` prompt-caches the system prompt — set it on REPEAT jobs (same big system prefix
    re-sent often: the inbox classifier, manager reviews, drafts) so the prefix reads at ~10%."""
    mdl = model or (MODEL_FAST if fast else MODEL)
    kwargs: dict = dict(model=mdl, max_tokens=max_tokens,
                        system=_cached_system(system) if cache else system,
                        messages=[{"role": "user", "content": user}])
    if think_hard:
        kwargs["thinking"] = {"type": "adaptive"}
    resp = _client().messages.create(**kwargs)
    _log_usage(mdl, getattr(resp, "usage", None), purpose, company)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _cached_system(system: str) -> list[dict]:
    """Wrap the system prompt as a single ephemeral-cached text block (5-min prompt cache)."""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _cached_tools(tools: list[dict]) -> list[dict]:
    """Mark the LAST tool as a cache breakpoint so the whole (static) tools block is cached."""
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def chat(system: str, messages: list[dict], *, max_tokens: int = 1000,
         purpose: str = "chat", company: str | None = None) -> str:
    """Multi-turn conversation → assistant text. Snappy (no extended thinking) for voice back-and-forth.
    The system prompt is PROMPT-CACHED — it's re-sent every turn, so the cache pays for itself immediately."""
    resp = _client().messages.create(model=MODEL, max_tokens=max_tokens,
                                     system=_cached_system(system), messages=messages)
    _log_usage(MODEL, getattr(resp, "usage", None), purpose, company)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def chat_tools(system: str, messages: list[dict], tools: list[dict], executor,
               *, max_tokens: int = 1500, rounds: int = 6, purpose: str = "chat",
               company: str | None = None) -> str:
    """Conversation where Claude can call tools to read/act on Cortex (e.g. manage skills).

    `executor(name, input) -> str` runs the tool and returns a result string. Loops until Claude
    stops calling tools or `rounds` is hit, then returns the final assistant text. EACH round is a
    full Opus call (system + tools + history), so this is the priciest path — logged per round.
    """
    client = _client()
    # PROMPT CACHING: the system prompt + the (static) tool schemas are the big fixed prefix re-sent on
    # every round/turn — cache them so each subsequent call reads them at ~10% instead of full price.
    sys_blocks, cached_tools = _cached_system(system), _cached_tools(tools)
    msgs = [dict(m) for m in messages]
    resp = None
    for _ in range(rounds):
        resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=sys_blocks,
                                       tools=cached_tools, messages=msgs)
        _log_usage(MODEL, getattr(resp, "usage", None), purpose, company)
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


def _loads(raw: str) -> dict:
    raw = (raw or "").strip()
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


def think_json(system: str, user: str, *, fast: bool = True, model: str | None = None,
               max_tokens: int = 2000, purpose: str = "think_json", company: str | None = None,
               cache: bool = False) -> dict:
    """Completion that must return a JSON object → parsed dict. `cache=True` prompt-caches the system."""
    sys = system + "\n\nRespond with ONLY a valid JSON object — no prose, no markdown fences."
    return _loads(think(sys, user, fast=fast, model=model, max_tokens=max_tokens,
                        purpose=purpose, company=company, cache=cache))


def research_json(system: str, user: str, *, model: str | None = None,
                  max_searches: int = 5, max_tokens: int = 3500) -> dict:
    """Completion WITH live web search (Anthropic server tool) → parsed JSON. The model can actually look
    up the lead's domain, website and app before it answers. Returns {} if nothing usable comes back."""
    sys = system + ("\n\nUse web search to investigate before you decide. When you have researched enough, "
                    "respond with ONLY a valid JSON object (no prose, no markdown fences).")
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}]
    mdl = model or MODEL_FAST
    resp = _client().messages.create(
        model=mdl, max_tokens=max_tokens, system=sys,
        tools=tools, messages=[{"role": "user", "content": user}])
    _log_usage(mdl, getattr(resp, "usage", None), "research", None)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    return _loads(text)
