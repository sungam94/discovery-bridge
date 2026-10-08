"""The sound-analysis image must build the same way every time and fail at build time, not at night."""
import re
import wave
from pathlib import Path

from analysis.smoke import write_test_clip

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "analysis" / "Dockerfile").read_text()


def test_every_requirement_is_pinned_to_one_version():
    lines = [ln.split("#")[0].strip() for ln in (ROOT / "analysis" / "requirements.txt").read_text().splitlines()]
    reqs = [ln for ln in lines if ln]
    assert any(r.startswith("essentia-tensorflow==") for r in reqs)
    assert any(r.startswith("numpy==") for r in reqs)
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", r) for r in reqs), reqs


def test_every_downloaded_model_file_is_checked_against_its_sha256():
    added = re.findall(r"^ADD https://\S+/([^/\s]+) /models/$", DOCKERFILE, re.M)
    sums = dict(reversed(ln.split()) for ln in (ROOT / "analysis" / "models.sha256").read_text().splitlines() if ln)
    assert added and set(added) == set(sums)
    assert all(re.fullmatch(r"[0-9a-f]{64}", s) for s in sums.values())
    assert "sha256sum -c" in DOCKERFILE


def test_the_build_runs_the_models_once_after_the_code_is_in_place():
    copy = DOCKERFILE.index("COPY src/analysis")
    assert DOCKERFILE.index("python -m analysis.smoke /models") > copy


def test_the_smoke_clip_is_long_enough_for_one_model_patch(tmp_path):
    write_test_clip(tmp_path / "clip.wav")
    with wave.open(str(tmp_path / "clip.wav")) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() / w.getframerate() >= 5
        frames = w.readframes(w.getnframes())
    assert any(frames)  # not silence
