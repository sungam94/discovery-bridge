"""Spec §6.2 (amended 2026-10-05): a play is replayed in the same hour of the day it was heard, on that day or any
later one, until it is older than MAX_PLAY_AGE; the daily cap is spread over the day. All calendar logic in local
time."""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bridge.timeutil import iso, parse_iso

DAYPART_HOURS = (0, 6, 12, 18)  # only for the status page's summary
MAX_PLAY_AGE = timedelta(days=14)


def daypart(dt: datetime, tz: str) -> int:
    hour = dt.astimezone(ZoneInfo(tz)).hour
    return max(i for i, h in enumerate(DAYPART_HOURS) if hour >= h)


def bucket(dt: datetime, tz: str, hours: int = 1) -> int:
    """The slot of the day a play belongs to: the local hour, or a wider slot of `hours` hours."""
    return dt.astimezone(ZoneInfo(tz)).hour // max(1, hours)


def day_allowance(now: datetime, tz: str, max_per_day: int) -> int:
    """Starts allowed so far today: the cap spread over the hours, unused earlier hours carried forward, so a busy
    morning cannot use up the evening's replays."""
    return math.ceil(max_per_day * (now.astimezone(ZoneInfo(tz)).hour + 1) / 24)


def backoff_after(attempts: int) -> timedelta:
    if attempts <= 1:
        return timedelta(minutes=1)
    if attempts == 2:
        return timedelta(minutes=5)
    return timedelta(minutes=5 * 2 ** (attempts - 2))


def _local_day_bounds(now: datetime, tz: str) -> tuple[str, str]:
    z = ZoneInfo(tz)
    day = now.astimezone(z).date()
    start = datetime.combine(day, time(0), tzinfo=z).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=z).astimezone(timezone.utc)
    return iso(start), iso(end)


def started_today(conn: sqlite3.Connection, now: datetime, tz: str) -> int:
    start, end = _local_day_bounds(now, tz)
    return conn.execute("SELECT count(*) FROM job_start WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0]


def _due(row: sqlite3.Row, now: datetime) -> bool:
    return row["next_attempt_at"] is None or parse_iso(row["next_attempt_at"]) <= now


def next_job(conn: sqlite3.Connection, now: datetime, tz: str, max_per_day: int, bucket_hours: int = 1,
             max_play_age: timedelta = MAX_PLAY_AGE) -> sqlite3.Row | None:
    started = started_today(conn, now, tz)
    if started >= max_per_day:
        return None
    for row in conn.execute("SELECT * FROM feedback_job WHERE status = 'pending' AND kind = 'save' "
                            "ORDER BY original_ts, id"):
        if _due(row, now):
            return row
    if started >= day_allowance(now, tz, max_per_day):
        return None
    slot = bucket(now, tz, bucket_hours)
    # a track Spotify already got a replay of tells it little new (looping queues repeat tracks every few hours),
    # so it waits behind new tracks; it still runs when nothing new is waiting
    replayed = {r["spotify_id"] for r in conn.execute(
        "SELECT DISTINCT spotify_id FROM feedback_job WHERE kind = 'play' AND status IN ('verified', 'done')")}
    candidates = []
    for row in conn.execute("SELECT j.*, e.outcome FROM feedback_job j JOIN taste_event e ON e.id = j.taste_event_id "
                            "WHERE j.status = 'pending' AND j.kind = 'play'"):
        original = parse_iso(row["original_ts"])
        if not _due(row, now) or bucket(original, tz, bucket_hours) != slot or now - original > max_play_age:
            continue
        candidates.append(((row["spotify_id"] in replayed, row["outcome"] != "completed", row["original_ts"],
                            row["id"]), row))
    return min(candidates, key=lambda c: c[0])[1] if candidates else None


def expire_jobs(conn: sqlite3.Connection, now: datetime, tz: str, max_age_s: int,
                max_play_age: timedelta = MAX_PLAY_AGE) -> int:
    expired = 0
    for row in conn.execute("SELECT id, kind, original_ts FROM feedback_job WHERE status = 'pending'").fetchall():
        original = parse_iso(row["original_ts"])
        reason = None
        if row["kind"] == "play" and now - original > max_play_age:
            reason = "max_age"
        elif row["kind"] == "save" and now - original > timedelta(seconds=max_age_s):
            reason = "max_age"
        if reason:
            conn.execute("UPDATE feedback_job SET status = 'expired', expire_reason = ?, finished_at = ? WHERE id = ?",
                         (reason, iso(now), row["id"]))
            expired += 1
    return expired
