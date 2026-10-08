"""Spec §5.2: turn one closed play into an outcome. Rules run in order; the first match wins."""
from __future__ import annotations

from bridge.config import Thresholds

JOB_KIND = {"completed": "play", "listened": "play", "favorite": "save"}
OUTCOME_RANK = {"completed": 3, "listened": 2, "skipped": 1, "partial": 0}


def classify(listened_ms: int, duration_ms: int | None, fully_played: bool, queue_error: bool,
             ended_by: str, t: Thresholds) -> tuple[str, str | None]:
    if not duration_ms:
        return "partial", "no_duration"
    if queue_error:
        return "partial", "playback_error"
    if fully_played or listened_ms >= t.completed * duration_ms:
        return "completed", None
    if listened_ms >= t.listened * duration_ms and listened_ms >= t.listened_min_s * 1000:
        return "listened", None
    if ended_by == "next" and listened_ms < t.skipped_s * 1000:
        return "skipped", None
    return "partial", None
