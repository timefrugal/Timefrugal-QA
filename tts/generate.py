#!/usr/bin/env python3
"""TTS worker — runs on a GitHub Actions runner (full internet).
Reads every tts/narration*.json file, writes tts/out/<id>.mp3 via edge-tts.
"""
import asyncio, glob, json, os

import edge_tts

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
MAX_ATTEMPTS = 5
PACE_SECONDS = 1.2  # edge-tts's backend throttles bursts of back-to-back requests


async def synth(text, voice, rate, path):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            c = edge_tts.Communicate(text, voice, rate=rate)
            await c.save(path)
            return
        except Exception as e:
            if attempt == MAX_ATTEMPTS:
                raise
            wait = 5 * (2 ** (attempt - 1))
            print(f"  retry {attempt}/{MAX_ATTEMPTS} after error ({e}); waiting {wait}s")
            await asyncio.sleep(wait)


async def main():
    os.makedirs(OUT, exist_ok=True)
    for jf in sorted(glob.glob(os.path.join(HERE, "narration*.json"))):
        with open(jf) as f:
            scenes = json.load(f)
        for s in scenes:
            path = os.path.join(OUT, f"{s['id']}.mp3")
            await synth(s["text"], s["voice"], s.get("rate", "+0%"), path)
            print(f"{s['id']}: {os.path.getsize(path)} bytes")
            await asyncio.sleep(PACE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
