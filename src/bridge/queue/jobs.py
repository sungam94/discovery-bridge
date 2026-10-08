"""Spec §5.3: each eligible taste event with a job kind becomes one feedback_job."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from bridge.capture.outcome import JOB_KIND
from bridge.timeutil import iso


def _mark(conn: sqlite3.Connection, event_id: int, status: str) -> None:
    conn.execute("UPDATE taste_event SET policy_result = ? WHERE id = ?", (status, event_id))


def create_jobs(conn: sqlite3.Connection, now: datetime, max_age_s: int) -> int:
    rows = conn.execute(
        "SELECT e.* FROM taste_event e LEFT JOIN feedback_job j ON j.taste_event_id = e.id "
        "WHERE e.policy_result = 'eligible' AND j.id IS NULL AND e.spotify_id IS NOT NULL AND e.started_at >= ? "
        "ORDER BY e.id", (iso(now - timedelta(seconds=max_age_s)),)).fetchall()
    created = 0
    for e in rows:
        kind = JOB_KIND.get(e["outcome"])
        if kind is None:
            continue
        if kind == "save" and conn.execute(
                "SELECT 1 FROM feedback_job WHERE kind = 'save' AND spotify_id = ? "
                "AND status IN ('pending', 'running', 'done')", (e["spotify_id"],)).fetchone():
            _mark(conn, e["id"], "duplicate_save")
            continue
        target = None
        if kind == "play":
            target = e["duration_ms"] if e["outcome"] == "completed" else e["listened_ms"]
            if not target:
                _mark(conn, e["id"], "no_target")
                continue
        conn.execute("INSERT INTO feedback_job(taste_event_id, kind, spotify_id, target_ms, source_spotify_playlist_id, "
                     "original_ts, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                     (e["id"], kind, e["spotify_id"], target, e["source_spotify_playlist_id"], e["started_at"],
                      iso(now)))
        created += 1
    return created
