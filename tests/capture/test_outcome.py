import pytest

from bridge.capture.outcome import JOB_KIND, OUTCOME_RANK, classify
from bridge.config import Thresholds

T = Thresholds()


@pytest.mark.parametrize("listened_ms, duration_ms, fully, error, ended_by, expected", [
    (0, None, False, False, "next", ("partial", "no_duration")),
    (0, 0, True, False, "next", ("partial", "no_duration")),
    (200_000, 240_000, False, True, "next", ("partial", "playback_error")),
    (20_000, 20_000, True, False, "next", ("completed", None)),        # short track, natural end
    (216_000, 240_000, False, False, "next", ("completed", None)),     # exactly 90 %
    (130_000, 240_000, False, False, "next", ("listened", None)),      # 54 % and >= 30 s
    (20_000, 33_000, False, False, "next", ("skipped", None)),         # 60 % but < 30 s, moved on
    (10_000, 240_000, False, False, "next", ("skipped", None)),
    (10_000, 240_000, False, False, "idle", ("partial", None)),        # stopped, not moved on
    (29_999, 240_000, False, False, "restart", ("partial", None)),
    (100_000, 240_000, False, False, "next", ("partial", None)),       # 42 %: neither listened nor skipped
])
def test_classify(listened_ms, duration_ms, fully, error, ended_by, expected):
    assert classify(listened_ms, duration_ms, fully, error, ended_by, T) == expected


def test_custom_thresholds():
    t = Thresholds(completed=0.8, listened=0.5, listened_min_s=30, skipped_s=20)
    assert classify(200_000, 240_000, False, False, "next", t) == ("completed", None)
    assert classify(25_000, 240_000, False, False, "next", t) == ("partial", None)


def test_tables():
    assert JOB_KIND == {"completed": "play", "listened": "play", "favorite": "save"}
    assert OUTCOME_RANK["completed"] > OUTCOME_RANK["listened"] > OUTCOME_RANK["skipped"] > OUTCOME_RANK["partial"]
