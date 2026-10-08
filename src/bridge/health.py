"""Health summary for external alerting: problems as stable keys with a short text.

The bridge writes health.json every few minutes (config key health_file). Any watchdog can read it: a cron
job, Uptime Kuma, or the example in ops/hermes/. A watcher reports new keys, recoveries, and a file that
stopped updating. The keys are the same in every language; the texts follow the language setting. No
secrets go into the file.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bridge.genre.store import all_playlist_genres
from bridge.genre.wheel import anchor_hue
from bridge.publish.moods import live_isrcs, open_sound
from bridge.repo import get_setting, set_setting
from bridge.texts import check_language, text
from bridge.timeutil import iso, parse_iso

PLAYLIST_ERROR_GRACE = timedelta(hours=2)
POLL_FAILURES_MAX = 2
POLL_OVERDUE_FACTOR = 3
CAPTURE_DOWN_GRACE = timedelta(minutes=10)
BACKUP_MAX_AGE = timedelta(hours=30)
DISK_FREE_MIN = 2 * 1024 ** 3
ADAPTER_PROBLEM_AFTER = timedelta(minutes=30)
FAILED_JOBS_MAX = 5
STUCK_JOB_AFTER = timedelta(minutes=30)
WHEEL_UNPLACED_MAX = 0.10  # share of genre labels on the covers whose colour the wheel does not fix by name
WHEEL_NAMES_SHOWN = 5
SOUND_STALLED_AFTER = timedelta(hours=6)


def record_poll(conn: sqlite3.Connection, at: datetime, ok: bool, detail: str = "") -> None:
    if ok:
        set_setting(conn, "last_poll_ok_at", iso(at))
        set_setting(conn, "poll_failures", "0")
    else:
        set_setting(conn, "poll_failures", str(int(get_setting(conn, "poll_failures", "0") or 0) + 1))
        set_setting(conn, "last_poll_error", detail[:200])
        if get_setting(conn, "last_poll_ok_at") is None:
            set_setting(conn, "last_poll_ok_at", iso(at))  # start the overdue clock at the first attempt


def record_capture(conn: sqlite3.Connection, at: datetime, up: bool) -> None:
    if up:
        set_setting(conn, "capture_down_since", "")
    elif not get_setting(conn, "capture_down_since"):
        set_setting(conn, "capture_down_since", iso(at))


def record_backup(conn: sqlite3.Connection, at: datetime, ok: bool, detail: str = "") -> None:
    if ok:
        set_setting(conn, "last_backup_at", iso(at))
        set_setting(conn, "last_backup_error", "")
    else:
        set_setting(conn, "last_backup_error", f"{iso(at)} {detail[:200]}")


def _since(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def build_health(conn: sqlite3.Connection, now: datetime, poll_interval_s: int, disk_free_bytes: int,
                 sound_path: Path | None = None, *, language: str) -> dict:
    """Problem texts are in `language` (bridge/texts.py); the keys are the same in every language."""
    check_language(language)
    problems: list[dict[str, str]] = []

    def add(key: str, message: str) -> None:
        problems.append({"key": key, "text": message})

    def say(name: str, **values) -> str:
        return text(language, name, **values)

    state = get_setting(conn, "ingestion_state")
    if state == "auth_expired":
        add("spotify", say("spotify_auth_expired"))
    elif state == "error":
        add("spotify", say("spotify_error"))

    for r in conn.execute(
            "SELECT p.slot, p.name, p.last_state, p.last_error, "
            "(SELECT max(ts) FROM ingestion_health h WHERE h.slot = p.slot) AS since "
            "FROM playlist p WHERE p.stale = 0 AND p.last_state IS NOT NULL AND p.last_state != 'ok' ORDER BY p.slot"):
        since = _since(r["since"])
        if since is None or now - since >= PLAYLIST_ERROR_GRACE:
            add(f"playlist:{r['slot']}", f"{r['name']}: {(r['last_error'] or r['last_state'])[:150]}")

    failures = int(get_setting(conn, "poll_failures", "0") or 0)
    last_ok = _since(get_setting(conn, "last_poll_ok_at"))
    if failures >= POLL_FAILURES_MAX:
        add("poll", say("poll_failures", failures=failures, error=get_setting(conn, "last_poll_error", "")))
    elif last_ok is not None and now - last_ok > timedelta(seconds=POLL_OVERDUE_FACTOR * poll_interval_s):
        add("poll", say("poll_overdue", since=iso(last_ok)[:16]))

    down = _since(get_setting(conn, "capture_down_since"))
    if down is not None and now - down >= CAPTURE_DOWN_GRACE:
        add("capture", say("capture", since=iso(down)[:16]))

    backup_error = get_setting(conn, "last_backup_error")
    last_backup = _since(get_setting(conn, "last_backup_at"))
    if backup_error:
        add("backup", say("backup_error", error=backup_error))
    elif last_backup is not None and now - last_backup > BACKUP_MAX_AGE:
        add("backup", say("backup_age", at=iso(last_backup)[:16]))

    if get_setting(conn, "feedback_paused", "true") != "true":
        row = conn.execute("SELECT ts, state, detail FROM adapter_health ORDER BY id DESC LIMIT 1").fetchone()
        if row and row["state"] in ("unreachable", "auth_expired", "error") \
                and now - parse_iso(row["ts"]) >= ADAPTER_PROBLEM_AFTER:
            add("adapter", say("adapter", state=row["state"], since=row["ts"][:16], detail=(row["detail"] or "")[:100]))
    failed = conn.execute("SELECT count(*) FROM feedback_job WHERE status = 'failed' AND finished_at >= ?",
                          (iso(now - timedelta(hours=24)),)).fetchone()[0]
    if failed >= FAILED_JOBS_MAX:
        add("jobs", say("jobs", failed=failed))
    stuck = conn.execute("SELECT started_at FROM feedback_job WHERE status = 'running' AND started_at < ?",
                         (iso(now - STUCK_JOB_AFTER),)).fetchone()
    if stuck:
        add("queue", say("queue", since=stuck["started_at"][:16]))
    wheel = _wheel_problem(conn, language)
    if wheel:
        add("genre_wheel", wheel)
    sound = _sound_problem(conn, sound_path, now, language) if sound_path is not None else None
    if sound:
        add("sound", sound)
    if disk_free_bytes < DISK_FREE_MIN:
        add("disk", say("disk", free_gb=disk_free_bytes / 1024 ** 3))

    return {"version": 1, "written_at": iso(now), "problems": problems}


def _wheel_problem(conn: sqlite3.Connection, language: str) -> str | None:
    """Covers show genres the colour wheel has no fixed place for (grey, or placed from data only): once these
    pass WHEEL_UNPLACED_MAX of the labels, the wheel (bridge/genre/wheel.py) needs new anchors."""
    weights: dict[str, int] = {}
    total = 0
    for shares in all_playlist_genres(conn).values():
        for genre, tracks in shares:
            total += tracks
            if anchor_hue(genre) is None:
                weights[genre] = weights.get(genre, 0) + tracks
    unplaced = sum(weights.values())
    if not total or unplaced / total <= WHEEL_UNPLACED_MAX:
        return None
    names = ", ".join(sorted(weights, key=lambda g: (-weights[g], g))[:WHEEL_NAMES_SHOWN])
    return text(language, "genre_wheel", percent=round(100 * unplaced / total), names=names)


def _sound_problem(conn: sqlite3.Connection, path: Path, now: datetime, language: str) -> str | None:
    """The sound-analysis container wrote nothing for SOUND_STALLED_AFTER although tracks are waiting.
    Not reported while sound.sqlite does not exist (the container is not set up)."""
    sound = open_sound(Path(path))
    if sound is None:
        return None
    try:
        last = sound.execute("SELECT max(analysed_at) FROM track_sound").fetchone()[0]
        have = {r[0] for r in sound.execute("SELECT isrc FROM track_sound").fetchall()}
    except sqlite3.Error:
        last, have = None, set()
    finally:
        sound.close()
    waiting = set().union(*live_isrcs(conn).values()) - have
    since = parse_iso(last) if last else datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    if not waiting or now - since < SOUND_STALLED_AFTER:
        return None
    return text(language, "sound", since=iso(since)[:16], waiting=len(waiting))


def write_health(path: Path, data: dict) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".health-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
