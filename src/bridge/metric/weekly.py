"""Spec §5.4: the weekly Discover Weekly metric, one row per track-set window."""
from __future__ import annotations

import sqlite3

from bridge.capture.outcome import OUTCOME_RANK

PLAYED_WELL = ("completed", "listened")


def _windows(conn: sqlite3.Connection, slot: str) -> list[list[sqlite3.Row]]:
    windows: list[list[sqlite3.Row]] = []
    for s in conn.execute("SELECT id, fetched_at, set_hash, coverage_matched FROM playlist_snapshot "
                          "WHERE slot = ? ORDER BY id", (slot,)):
        if windows and windows[-1][0]["set_hash"] == s["set_hash"]:
            windows[-1].append(s)
        else:
            windows.append([s])
    return windows


def compute_weekly(conn: sqlite3.Connection, slot: str = "discover_weekly") -> int:
    windows = _windows(conn, slot)
    for i, w in enumerate(windows):
        first = w[0]
        start = first["fetched_at"]
        end = windows[i + 1][0]["fetched_at"] if i + 1 < len(windows) else None
        ids = [s["id"] for s in w]
        best: dict[str, str] = {}
        for r in conn.execute(f"SELECT spotify_id, outcome FROM taste_event WHERE source = 'ma_play' "
                              f"AND spotify_id IS NOT NULL AND policy_result NOT IN ('player_not_allowed', 'player_unknown') "
                              f"AND source_snapshot_id IN ({','.join('?' * len(ids))})",
                              ids):
            if OUTCOME_RANK.get(r["outcome"], -1) > OUTCOME_RANK.get(best.get(r["spotify_id"]), -1):
                best[r["spotify_id"]] = r["outcome"]
        track_set = {r[0] for r in conn.execute(
            "SELECT s.provider_item_id FROM playlist_snapshot_item i JOIN source_track s ON s.id = i.source_track_id "
            "WHERE i.snapshot_id = ?", (first["id"],))}
        fav_sql = ("SELECT DISTINCT spotify_id FROM taste_event WHERE source = 'ma_favorite' "
                   "AND spotify_id IS NOT NULL AND started_at >= ?") + (" AND started_at < ?" if end else "")
        favorited = {r[0] for r in conn.execute(fav_sql, (start, end) if end else (start,))} & track_set
        conn.execute("INSERT OR REPLACE INTO weekly_metric(snapshot_id, window_start, window_end, matched, played, "
                     "completed, favorited, feedback_days) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                     (first["id"], start, end, first["coverage_matched"] or 0, len(best),
                      sum(1 for o in best.values() if o in PLAYED_WELL), len(favorited)))
    return len(windows)
