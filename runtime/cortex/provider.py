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


def media_blocks(urls: list[str] | None) -> list[dict]:
    """Turn data: URLs (data:image/...;base64,... or data:application/pdf;base64,...) into Anthropic
    content blocks — images become image blocks, PDFs become document blocks (Claude reads both)."""
    blocks: list[dict] = []
    for u in (urls or [])[:8]:
        if not isinstance(u, str) or not u.startswith("data:") or ";base64," not in u:
            continue
        head, b64 = u.split(";base64,", 1)
        media = head[5:] or "image/jpeg"
        if media.startswith("image/"):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
        elif media == "application/pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})
    return blocks


def think(system: str, user: str, *, fast: bool = False, model: str | None = None,
          think_hard: bool = False, max_tokens: int = 6000, purpose: str = "think",
          company: str | None = None, cache: bool = False, images: list[str] | None = None) -> str:
    """One-shot completion → plain text. `model` overrides the fast/slow default when given.
    `cache=True` prompt-caches the system prompt — set it on REPEAT jobs (same big system prefix
    re-sent often: the inbox classifier, manager reviews, drafts) so the prefix reads at ~10%.
    `images` = data: URLs (images/PDFs) the worker should actually see when drafting."""
    mdl = model or (MODEL_FAST if fast else MODEL)
    blocks = media_blocks(images)
    content: object = ([{"type": "text", "text": user}] + blocks) if blocks else user
    kwargs: dict = dict(model=mdl, max_tokens=max_tokens,
                        system=_cached_system(system) if cache else system,
                        messages=[{"role": "user", "content": content}])
    if think_hard:
        kwargs["thinking"] = {"type": "adaptive"}
    client = _client()
    if max_tokens >= 4000:   # long generation (e.g. a full blog compose) -> STREAM with a generous timeout so a
        # slow, lengthy response can't hit the short fail-fast per-request timeout (the #101/#116 compose failures).
        with client.with_options(timeout=600.0).messages.stream(**kwargs) as _s:
            resp = _s.get_final_message()
    else:
        resp = client.messages.create(**kwargs)
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


def _cache_history(messages: list[dict]) -> list[dict]:
    """Put a cache breakpoint at the END of the conversation history so the growing message prefix
    is read at ~10% on the next round/turn — this is what makes LONG conversations cheap (the prior
    turns are re-sent every turn; caching them stops paying full price for the whole transcript)."""
    out = [dict(m) for m in messages]
    if not out:
        return out
    last = dict(out[-1])
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(c, list) and c and isinstance(c[-1], dict):
        nc = [dict(b) for b in c]
        nc[-1] = {**nc[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = nc
    out[-1] = last
    return out


def chat(system: str, messages: list[dict], *, max_tokens: int = 1000,
         purpose: str = "chat", company: str | None = None) -> str:
    """Multi-turn conversation → assistant text. Snappy (no extended thinking) for voice back-and-forth.
    The system prompt is PROMPT-CACHED — it's re-sent every turn, so the cache pays for itself immediately."""
    resp = _client().messages.create(model=MODEL, max_tokens=max_tokens,
                                     system=_cached_system(system), messages=messages)
    _log_usage(MODEL, getattr(resp, "usage", None), purpose, company)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


# ---- anti-hallucination guard for the chat agent --------------------------------------------------------
# The conversational agent occasionally TELLS Rashad something was created/queued/"in your Inbox" (even
# inventing a task number) WITHOUT calling the tool that actually does it — silently losing his work. These
# helpers let chat_tools / chat_tools_stream detect a creation CLAIM with no creating-tool call, force one
# real corrective attempt, and never let a false confirmation stand.
_CREATING_TOOLS = {"create_task", "draft_email", "run_report", "schedule_report", "create_quotation",
                   "set_reminder", "create_contact", "create_deal"}
_CLAIM_RE = re.compile(
    r"(?:task\s*#?\s*\d+"
    r"|in your inbox"
    r"|drafting (?:it|this|that|now)"
    r"|i'?ve (?:created|drafted|queued|added|staged|scheduled|set up|put)"
    r"|i have (?:created|drafted|queued|added|staged|scheduled|set up|put)"
    r"|ready (?:to (?:read|review)|in your inbox))",
    re.I)


def _claims_action(text: str) -> bool:
    """True if the reply asserts it created/queued/drafted something (or names a task number)."""
    return bool(text and _CLAIM_RE.search(text))


_ACT_DONT_CLAIM = (
    "SYSTEM CHECK (not from Rashad): your reply told him something was created, drafted, queued, scheduled "
    "or is in his Inbox (or it named a task number), but you did NOT call create_task, draft_email or "
    "run_report this turn — so NOTHING was actually saved. Fix it now: if he wants a post/email/draft "
    "created, call the correct tool (create_task with the FULL brief or content) immediately. If nothing was "
    "actually meant to be created, reply honestly and do NOT claim anything was created or name any task number."
)

_GUARD_NOTE = ("\n\n(Cortex note: I could not confirm a new task was created this turn — if you asked me to "
               "create or draft something, please re-send it so it's saved to your Inbox.)")


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
    msgs = _cache_history(messages)   # + a 3rd breakpoint at the end of the history → long convos read cheap
    resp = None
    called: set = set()       # tool names actually executed this turn
    nudged = False            # have we already forced a corrective round?
    for _ in range(rounds):
        resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=sys_blocks,
                                       tools=cached_tools, messages=msgs)
        _log_usage(MODEL, getattr(resp, "usage", None), purpose, company)
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
            # GUARD: it claims it created/queued something but never called a tool that does -> force one real attempt
            if not nudged and _claims_action(text) and not (called & _CREATING_TOOLS):
                nudged = True
                msgs.append({"role": "assistant", "content": resp.content})
                msgs.append({"role": "user", "content": [{"type": "text", "text": _ACT_DONT_CLAIM}]})
                continue
            break
        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                called.add(b.name)
                try:
                    out = executor(b.name, b.input or {})
                except Exception as e:  # noqa: BLE001
                    out = f"error: {e}"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)})
        msgs.append({"role": "user", "content": results})
    if not resp:
        return ""
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    # FINAL SAFETY NET: never hand back a false confirmation
    if _claims_action(text) and not (called & _CREATING_TOOLS):
        text += _GUARD_NOTE
    return text


def chat_tools_stream(system: str, messages: list[dict], tools: list[dict], executor,
                      *, max_tokens: int = 1500, rounds: int = 6, purpose: str = "chat",
                      company: str | None = None):
    """Streaming twin of chat_tools: a generator that yields (kind, data) events as the agentic loop runs, so the
    HTTP layer can keep the connection alive (no proxy 100s timeout) and paint the reply live. kinds: 'delta'
    {text}, 'tool' {name}, 'done' {reply}. Text from every round is streamed AND accumulated into the final reply.
    Each round streams with a generous 600s timeout (a long turn can't hit the short fail-fast per-call limit)."""
    client = _client()
    sys_blocks, cached_tools = _cached_system(system), _cached_tools(tools)
    msgs = _cache_history(messages)
    final_text = ""
    called: set = set()       # tool names actually executed this turn
    nudged = False            # have we already forced a corrective round?
    for _ in range(rounds):
        round_text = ""
        final = None
        with client.with_options(timeout=600.0).messages.stream(
                model=MODEL, max_tokens=max_tokens, system=sys_blocks, tools=cached_tools, messages=msgs) as stream:
            for ev in stream:
                if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                    round_text += ev.delta.text
                    yield ("delta", {"text": ev.delta.text})
            final = stream.get_final_message()
        _log_usage(MODEL, getattr(final, "usage", None), purpose, company)
        final_text += round_text
        if final.stop_reason != "tool_use":
            # GUARD: claimed an action but no creating tool ran -> force one real corrective round (live)
            if not nudged and _claims_action(final_text) and not (called & _CREATING_TOOLS):
                nudged = True
                msgs.append({"role": "assistant", "content": final.content})
                msgs.append({"role": "user", "content": [{"type": "text", "text": _ACT_DONT_CLAIM}]})
                continue
            break
        msgs.append({"role": "assistant", "content": final.content})
        results = []
        for b in final.content:
            if getattr(b, "type", None) == "tool_use":
                called.add(b.name)
                yield ("tool", {"name": b.name})
                try:
                    out = executor(b.name, b.input or {})
                except Exception as e:  # noqa: BLE001
                    out = f"error: {e}"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)})
        msgs.append({"role": "user", "content": results})
    reply = final_text.strip()
    # FINAL SAFETY NET: never end the stream on a false confirmation
    if _claims_action(reply) and not (called & _CREATING_TOOLS):
        yield ("delta", {"text": _GUARD_NOTE})
        reply += _GUARD_NOTE
    yield ("done", {"reply": reply})


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
