"""Build-time check of the image: loads the models and analyses a generated clip, so a missing wheel, a wrong
model file or a wrong output node name fails `docker compose build` and not the first night of analysis.

    python -m analysis.smoke /models"""
from __future__ import annotations

import math
import random
import struct
import sys
import tempfile
import wave
from pathlib import Path

RATE = 16000
SECONDS = 6  # EffNet needs about 2 s of audio for one patch


def write_test_clip(path: Path, seconds: int = SECONDS, rate: int = RATE) -> None:
    """A quiet chord with a little noise: not silence, the same every time."""
    noise = random.Random(1)
    samples = []
    for n in range(seconds * rate):
        t = n / rate
        v = 0.2 * sum(math.sin(2 * math.pi * f * t) for f in (220.0, 277.2, 329.6)) / 3
        samples.append(int(32767 * (v + 0.01 * noise.uniform(-1, 1))))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def main(models: str) -> None:
    from analysis.model import EssentiaAnalyser

    analyser = EssentiaAnalyser(Path(models))
    with tempfile.TemporaryDirectory() as work:
        clip = Path(work) / "clip.wav"
        write_test_clip(clip)
        sound = analyser.analyse(clip)
    if len(sound.moods) != 56 or not 0 <= sound.instrumental <= 1 or len(sound.embedding) != 1280:
        raise SystemExit(f"unexpected model output: {len(sound.moods)} moods, instrumental {sound.instrumental}, "
                         f"embedding {len(sound.embedding)}")
    print(f"models ok: {len(sound.moods)} moods, instrumental {sound.instrumental:.2f}, "
          f"embedding {len(sound.embedding)}")


if __name__ == "__main__":
    main(sys.argv[1])
