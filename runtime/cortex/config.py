"""Cortex config — loads secrets/settings from /etc/cortex/cortex.env.

The env file is `KEY=value` lines (comments start with #). Real environment
variables of the same name win, so CORTEX_ENV can repoint the file in tests.
"""
from __future__ import annotations

import os
from functools import lru_cache

ENV_PATH = os.environ.get("CORTEX_ENV", "/etc/cortex/cortex.env")


@lru_cache(maxsize=1)
def _env() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    # real env vars override the file
    for k, v in os.environ.items():
        if k.isupper():
            data[k] = v
    return data


def get(key: str, default: str | None = None) -> str | None:
    return _env().get(key, default)


def require(key: str) -> str:
    v = _env().get(key)
    if not v:
        raise RuntimeError(f"Missing required config: {key} (in {ENV_PATH})")
    return v
