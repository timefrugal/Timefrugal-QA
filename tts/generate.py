#!/usr/bin/env python3
"""TTS worker — runs on a GitHub Actions runner (full internet).
Reads tts/narration.json, writes tts/out/<id>.mp3 via edge-tts.
"""
import asyncio, json, os

import edge_tts

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")


async def main():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(HERE, "narration.json")) as f:
        scenes = json.load(f)
    for s in scenes:
        path = os.path.join(OUT, f"{s['id']}.mp3")
        c = edge_tts.Communicate(s["text"], s["voice"], rate=s.get("rate", "+0%"))
        await c.save(path)
        print(f"{s['id']}: {os.path.getsize(path)} bytes")


if __name__ == "__main__":
    asyncio.run(main())
