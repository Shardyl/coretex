"""Generate the in-app recorder's spoken cues ONCE with ElevenLabs (owner 15 Nov 2026).

The recorder plays a fixed set of short clips (get into position, the numbers one to ten, hold, done) from the box,
so a test never waits on a live voice call and works offline once cached. Run on the box as cortex:
    python -m cortex.fitness_cues          (only missing clips)
    python -m cortex.fitness_cues --force  (regenerate all, e.g. after changing the voice)
Same voice and model as Talk's /api/voice/tts.
"""
from __future__ import annotations

import os
import sys

import httpx

from . import config
from .fitness_mobility import CUE_DIR, CUES


def make(force: bool = False) -> list[str]:
    key = config.require("ELEVENLABS_API_KEY")
    voice = config.get("ELEVENLABS_VOICE_ID") or "Xb7hH8MSUJpSbSDYk0k2"
    os.makedirs(CUE_DIR, exist_ok=True)
    made = []
    for name, text in CUES.items():
        path = os.path.join(CUE_DIR, name + ".mp3")
        if os.path.isfile(path) and not force:
            continue
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                       headers={"xi-api-key": key, "Content-Type": "application/json"},
                       json={"text": text, "model_id": "eleven_flash_v2_5",
                             "voice_settings": {"stability": 0.6, "similarity_boost": 0.8, "style": 0.0,
                                                "use_speaker_boost": True, "speed": 1.0}}, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        made.append(name)
    return made


if __name__ == "__main__":
    print("made", make(force="--force" in sys.argv))
