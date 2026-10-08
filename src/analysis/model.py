"""The models: Discogs-EffNet embeddings with the MTG-Jamendo mood/theme and the voice/instrumental heads.

Essentia is imported only when EssentiaAnalyser is built, so everything else runs and is tested without it."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

EFFNET = "discogs-effnet-bs64-1.pb"
MOODS = "mtg_jamendo_moodtheme-discogs-effnet-1"
VOICE = "voice_instrumental-discogs-effnet-1.pb"


@dataclass(frozen=True)
class Sound:
    moods: dict[str, float]   # score per mood/theme class
    instrumental: float       # 0..1
    embedding: list[float]    # mean EffNet embedding


class AnalysisError(Exception):
    pass


class Analyser(Protocol):
    def analyse(self, path: Path) -> Sound: ...


def _mean(rows: list[list[float]]) -> list[float]:
    if not rows:
        raise AnalysisError("no frames")
    return [sum(col) / len(rows) for col in zip(*rows)]


class EssentiaAnalyser:
    def __init__(self, models: Path) -> None:
        from essentia.standard import MonoLoader, TensorflowPredict2D, TensorflowPredictEffnetDiscogs

        models = Path(models)
        self._loader = MonoLoader
        self._classes = json.loads((models / f"{MOODS}.json").read_text())["classes"]
        self._effnet = TensorflowPredictEffnetDiscogs(graphFilename=str(models / EFFNET), output="PartitionedCall:1")
        self._moods = TensorflowPredict2D(graphFilename=str(models / f"{MOODS}.pb"), output="model/Sigmoid")
        self._voice = TensorflowPredict2D(graphFilename=str(models / VOICE), output="model/Softmax")

    def analyse(self, path: Path) -> Sound:
        audio = self._loader(filename=str(path), sampleRate=16000, resampleQuality=4)()
        emb = self._effnet(audio)
        embedding = _mean(emb.tolist())
        moods = _mean(self._moods(emb).tolist())
        voice = _mean(self._voice(emb).tolist())
        if len(moods) != len(self._classes) or len(voice) != 2:
            raise AnalysisError(f"unexpected output size ({len(moods)} moods, {len(voice)} voice)")
        if not all(math.isfinite(v) for v in embedding + moods + voice):
            raise AnalysisError("output not finite")
        return Sound(dict(zip(self._classes, moods)), voice[0], embedding)
