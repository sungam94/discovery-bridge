"""Spec §6.1/§6.2 (amended by the Phase 3 spike and plan): job status transitions, recovery, timeouts."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from bridge.queue.schedule import backoff_after
from bridge.timeutil import iso

SHORT_PLAY_MS = 30_000
ELSEWHERE_WAIT = timedelta(minutes=5)   # the user listens on another device: try again later


def mark_started(conn: sqlite3.Connection, job_id: int, now: datetime) -> None:
    conn.execute("UPDATE feedback_job SET status = 'running', attempts = attempts + 1, started_at = ?, "
                 "next_attempt_at = NULL WHERE id = ?", (iso(now), job_id))
    conn.execute("INSERT INTO job_start(job_id, ts) VALUES (?, ?)", (job_id, iso(now)))


def max_runtime(job: sqlite3.Row) -> timedelta:
    if job["kind"] == "save":
        return timedelta(minutes=10)
    return timedelta(milliseconds=job["target_ms"] or 0) + timedelta(minutes=15)


def _finish(conn, job_id, status, now, observed=None, error=None) -> str:
    conn.execute("UPDATE feedback_job SET status = ?, finished_at = ?, observed_spotify_id = COALESCE(?, "
                 "observed_spotify_id), error = COALESCE(?, error) WHERE id = ?",
                 (status, iso(now), observed, error, job_id))
    return status


def _back_to_pending(conn, job_id) -> str:
    conn.execute("UPDATE feedback_job SET status = 'pending', attempts = attempts - 1 WHERE id = ?", (job_id,))
    return "pending"


def _attempt_failed(conn, job, now, job_attempts, reason) -> str:
    if job["attempts"] >= job_attempts:
        return _finish(conn, job["id"], "failed", now, error=reason)
    conn.execute("UPDATE feedback_job SET status = 'pending', next_attempt_at = ?, error = ? WHERE id = ?",
                 (iso(now + backoff_after(job["attempts"])), reason, job["id"]))
    return "pending"


def apply_result(conn: sqlite3.Connection, job_id: int, result: dict, now: datetime, job_attempts: int) -> str:
    job = conn.execute("SELECT * FROM feedback_job WHERE id = ?", (job_id,)).fetchone()
    outcome, observed = result.get("outcome"), result.get("observed_track")
    if job["kind"] == "save":
        if outcome == "done":
            return _finish(conn, job_id, "done", now, observed)
        return _attempt_failed(conn, job, now, job_attempts, result.get("reason") or outcome)
    if outcome == "done":
        if (job["target_ms"] or 0) < SHORT_PLAY_MS:
            return _finish(conn, job_id, "done", now, observed)
        return _finish(conn, job_id, "verified" if result.get("verified") else "unverified", now, observed)
    if outcome == "interrupted":
        if result.get("verified"):
            return _finish(conn, job_id, "verified", now, observed)
        if result.get("interrupt") == "pause":
            return _back_to_pending(conn, job_id)
        if result.get("interrupt") == "elsewhere":  # never started: no attempt, no counted start, wait a while
            conn.execute("DELETE FROM job_start WHERE id = (SELECT max(id) FROM job_start WHERE job_id = ?)", (job_id,))
            conn.execute("UPDATE feedback_job SET status = 'pending', attempts = attempts - 1, next_attempt_at = ? "
                         "WHERE id = ?", (iso(now + ELSEWHERE_WAIT), job_id))
            return "pending"
        return _attempt_failed(conn, job, now, job_attempts, result.get("interrupt") or "interrupted")
    return _attempt_failed(conn, job, now, job_attempts, result.get("reason") or "error")


def lose_job(conn: sqlite3.Connection, job_id: int, now: datetime, reason: str) -> str:
    job = conn.execute("SELECT kind FROM feedback_job WHERE id = ?", (job_id,)).fetchone()
    if job["kind"] == "play":
        return _finish(conn, job_id, "unverified", now, error=reason)
    return _back_to_pending(conn, job_id)


def recover_running(conn: sqlite3.Connection, adapter_jobs: dict[int, dict | None], now: datetime,
                    job_attempts: int) -> int:
    handled = 0
    for job in conn.execute("SELECT * FROM feedback_job WHERE status = 'running'").fetchall():
        if job["id"] not in adapter_jobs:
            continue
        record = adapter_jobs[job["id"]]
        if record is not None and record.get("status") == "finished":
            apply_result(conn, job["id"], record["result"], now, job_attempts)
        elif record is not None:
            continue  # still running in the adapter; the queue runner keeps polling it
        else:
            lose_job(conn, job["id"], now, "lost after restart")
        handled += 1
    return handled
