"""Cortex's knowledge base about ITSELF.

Reads the on-box knowledge vault (/opt/cortex-knowledge: Claude's memory files + the protocols — the
same set mirrored to Google Drive nightly) so Cortex can answer questions about its own architecture,
what's been built, and how its features work (the questionnaire, routing, the Chief/Manager/Worker org,
approvals, backups). Small corpus (~50 short markdown files) so a lightweight keyword search is plenty.
"""
from __future__ import annotations

import os
import re

from . import config

VAULT = config.get("CORTEX_KNOWLEDGE_DIR") or "/opt/cortex-knowledge"
_STOP = {"the", "a", "an", "is", "are", "how", "does", "do", "what", "of", "to", "in", "on", "and",
         "for", "it", "this", "that", "work", "works", "cortex", "about"}


def _files() -> list[str]:
    out = []
    for sub in ("memory", "protocols"):
        d = os.path.join(VAULT, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".md") and f != "MEMORY.md":
                    out.append(os.path.join(d, f))
    return out


def search(query: str, k: int = 3) -> str:
    """Return the most relevant knowledge-base docs for a query."""
    words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if w not in _STOP and len(w) > 2]
    if not words:
        return "(ask a more specific question about how Cortex works)"
    scored = []
    for path in _files():
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        body = text.lower()
        name = os.path.basename(path).lower()
        score = sum(body.count(w) for w in words) + 6 * sum(1 for w in words if w in name)
        if score > 0:
            scored.append((score, path, text))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return "(nothing relevant found in Cortex's knowledge base)"
    return "\n\n".join(f"### {os.path.basename(p)}\n{t[:2400]}" for _, p, t in scored[:k])
