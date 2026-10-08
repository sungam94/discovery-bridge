"""The Essentia adapter, run against a stand-in for essentia.standard (essentia itself only exists in the image)."""
import json
import sys
import types

import pytest

from analysis.model import AnalysisError, EssentiaAnalyser


class Arr:
    def __init__(self, rows):
        self.rows = rows

    def tolist(self):
        return self.rows


@pytest.fixture
def models(tmp_path):
    (tmp_path / "mtg_jamendo_moodtheme-discogs-effnet-1.json").write_text(json.dumps({"classes": ["dark", "happy"]}))
    return tmp_path


@pytest.fixture
def essentia(monkeypatch):
    calls = {"loaded": [], "effnet": [], "heads": []}
    out = {"emb": Arr([[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]), "moods": Arr([[0.2, 0.4], [0.4, 0.8]]),
           "voice": Arr([[0.9, 0.1], [0.7, 0.3]])}

    def MonoLoader(**kw):
        calls["loaded"].append(kw)
        return lambda: "audio"

    def TensorflowPredictEffnetDiscogs(**kw):
        calls["effnet"].append(kw)
        return lambda audio: out["emb"]

    def TensorflowPredict2D(**kw):
        calls["heads"].append(kw)
        return lambda emb: out["voice"] if "voice" in kw["graphFilename"] else out["moods"]

    std = types.ModuleType("essentia.standard")
    std.MonoLoader, std.TensorflowPredictEffnetDiscogs, std.TensorflowPredict2D = (
        MonoLoader, TensorflowPredictEffnetDiscogs, TensorflowPredict2D)
    pkg = types.ModuleType("essentia")
    pkg.standard = std
    monkeypatch.setitem(sys.modules, "essentia", pkg)
    monkeypatch.setitem(sys.modules, "essentia.standard", std)
    return calls, out


def test_models_are_loaded_once_with_the_right_outputs(models, essentia):
    calls, _ = essentia
    EssentiaAnalyser(models)
    assert calls["effnet"] == [{"graphFilename": str(models / "discogs-effnet-bs64-1.pb"),
                                "output": "PartitionedCall:1"}]
    assert {"graphFilename": str(models / "mtg_jamendo_moodtheme-discogs-effnet-1.pb"),
            "output": "model/Sigmoid"} in calls["heads"]
    assert {"graphFilename": str(models / "voice_instrumental-discogs-effnet-1.pb"),
            "output": "model/Softmax"} in calls["heads"]


def test_analyse_averages_over_the_clip(models, essentia, tmp_path):
    calls, _ = essentia
    sound = EssentiaAnalyser(models).analyse(tmp_path / "clip.mp3")
    assert calls["loaded"] == [{"filename": str(tmp_path / "clip.mp3"), "sampleRate": 16000, "resampleQuality": 4}]
    assert sound.moods == pytest.approx({"dark": 0.3, "happy": 0.6})
    assert sound.instrumental == pytest.approx(0.8)
    assert sound.embedding == pytest.approx([2.0, 3.0, 4.0])


@pytest.mark.parametrize("key, rows", [("emb", []), ("moods", [[0.1]]), ("emb", [[float("nan"), 1.0, 1.0]])])
def test_empty_or_broken_output_is_an_error(models, essentia, tmp_path, key, rows):
    _, out = essentia
    out[key] = Arr(rows)
    with pytest.raises(AnalysisError):
        EssentiaAnalyser(models).analyse(tmp_path / "clip.mp3")
