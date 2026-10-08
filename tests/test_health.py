import json
from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.health import (
    build_health, record_backup, record_capture, record_poll, write_health,
)
from bridge.repo import set_setting
from bridge.timeutil import iso

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
GB = 1024 ** 3


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def keys(conn, now=NOW, free=50 * GB, interval=3600):
    return {p["key"]: p["text"] for p in build_health(conn, now, interval, free, language="de")["problems"]}


def playlist(conn, slot, state, error=None, since=NOW - timedelta(hours=3), stale=0):
    conn.execute("INSERT INTO playlist(slot, kind, name, last_state, last_error, stale) VALUES (?, 'fixed', ?, ?, ?, ?)",
                 (slot, f"Spotify · {slot}", state, error, stale))
    conn.execute("INSERT INTO ingestion_health(slot, ts, state, detail) VALUES (?, ?, ?, ?)",
                 (slot, iso(since), state, error or ""))


def test_healthy_system_has_no_problems(conn):
    record_poll(conn, NOW - timedelta(minutes=20), ok=True)
    record_capture(conn, NOW - timedelta(minutes=1), up=True)
    record_backup(conn, NOW - timedelta(hours=8), ok=True)
    playlist(conn, "discover_weekly", "ok")
    h = build_health(conn, NOW, 3600, 50 * GB, language="de")
    assert h["problems"] == [] and h["version"] == 1 and h["written_at"] == iso(NOW)


def test_spotify_login_problem(conn):
    set_setting(conn, "ingestion_state", "auth_expired")
    assert "sp_dc" in keys(conn)["spotify"]


def test_playlist_error_is_reported_after_two_hours(conn):
    playlist(conn, "daily_mix_3", "error", "PublishIncomplete: 49 of 50 missing", since=NOW - timedelta(hours=3))
    playlist(conn, "daily_mix_4", "error", "MaError", since=NOW - timedelta(minutes=30))
    playlist(conn, "daily_mix_5", "error", "old", stale=1)
    k = keys(conn)
    assert "49 of 50" in k["playlist:daily_mix_3"] and "playlist:daily_mix_4" not in k
    assert "playlist:daily_mix_5" not in k


def test_poll_failures_and_overdue(conn):
    record_poll(conn, NOW - timedelta(hours=2), ok=False, detail="timeout")
    assert "poll" not in keys(conn)                           # one failure is not reported
    record_poll(conn, NOW - timedelta(hours=1), ok=False, detail="timeout")
    assert "timeout" in keys(conn)["poll"]
    record_poll(conn, NOW - timedelta(hours=4), ok=True)
    assert "poll" in keys(conn)                               # last success 4 h ago: overdue
    record_poll(conn, NOW - timedelta(minutes=5), ok=True)
    assert "poll" not in keys(conn)


def test_capture_down_for_ten_minutes(conn):
    record_capture(conn, NOW - timedelta(minutes=12), up=False)
    record_capture(conn, NOW - timedelta(minutes=1), up=False)  # still down: keeps the first time
    assert "capture" in keys(conn)
    record_capture(conn, NOW, up=True)
    assert "capture" not in keys(conn)


def test_backup_failure_and_age(conn):
    record_backup(conn, NOW - timedelta(hours=2), ok=False, detail="disk full")
    assert "disk full" in keys(conn)["backup"]
    record_backup(conn, NOW - timedelta(hours=1), ok=True)
    assert "backup" not in keys(conn)
    assert "backup" in keys(conn, now=NOW + timedelta(hours=31))


def test_low_disk(conn):
    assert "disk" in keys(conn, free=1 * GB)


def test_write_health_is_atomic(conn, tmp_path):
    out = tmp_path / "h"
    out.mkdir()
    write_health(out / "health.json", build_health(conn, NOW, 3600, 50 * GB, language="de"))
    assert json.loads((out / "health.json").read_text())["version"] == 1
    assert [p.name for p in out.iterdir()] == ["health.json"]


def test_adapter_problem_only_when_feedback_on_and_lasting(conn):
    conn.execute("INSERT INTO adapter_health(ts, state, detail) VALUES (?, 'unreachable', 'ConnectError')",
                 (iso(NOW - timedelta(minutes=40)),))
    assert "adapter" not in keys(conn)                    # feedback is still paused (seeded)
    set_setting(conn, "feedback_paused", "false")
    assert "unreachable" in keys(conn)["adapter"]
    conn.execute("INSERT INTO adapter_health(ts, state, detail) VALUES (?, 'error', 'x')",
                 (iso(NOW - timedelta(minutes=5)),))
    assert "adapter" not in keys(conn)                    # changed 5 min ago: not lasting yet


def test_many_failed_jobs_and_stuck_job(conn):
    for i in range(5):
        conn.execute("INSERT INTO feedback_job(kind, spotify_id, original_ts, status, created_at, finished_at) "
                     "VALUES ('play', 's', 't', 'failed', 't', ?)", (iso(NOW - timedelta(hours=1)),))
    conn.execute("INSERT INTO feedback_job(kind, spotify_id, original_ts, status, created_at, started_at) "
                 "VALUES ('play', 's', 't', 'running', 't', ?)", (iso(NOW - timedelta(minutes=45)),))
    k = keys(conn)
    assert "5" in k["jobs"] and "queue" in k


def covers(conn, artist_genres):
    """One published playlist whose tracks are by the given artists, each with the given genres."""
    from bridge.genre.store import save_artist
    conn.execute("INSERT INTO playlist(slot, kind, name, ma_playlist_id, stale) VALUES ('mix', 'fixed', "
                 "'Spotify · Mix', '7', 0)")
    sid = conn.execute("INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash) "
                       "VALUES ('mix', 'x', 't', 'o', 's')").lastrowid
    for pos, (artist, genres) in enumerate(artist_genres):
        tid = conn.execute("INSERT INTO source_track(provider, provider_item_id, artist, title, album, duration_ms) "
                           "VALUES ('spotify', ?, ?, 't', 'al', 1000)", (f"t{pos}", artist)).lastrowid
        conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)", (sid, pos, tid))
        save_artist(conn, artist, "m", [(g, 1) for g in genres], NOW)
    conn.execute("UPDATE playlist SET published_snapshot_id = ? WHERE slot = 'mix'", (sid,))


def test_no_alert_while_cover_genres_have_their_place_on_the_colour_wheel(conn):
    covers(conn, [(f"A{i}", ["psytrance"]) for i in range(18)] + [("B", ["zeuhl"])])   # 1 of 19 slots: 5 %
    assert "genre_wheel" not in keys(conn)


def test_alert_when_too_many_cover_genres_have_no_fixed_place(conn):
    covers(conn, [(f"A{i}", ["psytrance"]) for i in range(8)] + [("B", ["zeuhl"]), ("C", ["zeuhl"]),
                                                                    ("D", ["kwaito"])])  # 3 of 11 slots: 27 %
    text = keys(conn)["genre_wheel"]
    assert "27 %" in text and "zeuhl" in text and "kwaito" in text
    assert text.index("zeuhl") < text.index("kwaito")        # most used first


def sound_keys(conn, path, now=NOW):
    return {p["key"]: p["text"] for p in build_health(conn, now, 3600, 50 * GB, sound_path=path, language="de")["problems"]}


def test_sound_analysis_stalled_for_six_hours_with_tracks_pending(conn, tmp_path):
    from analysis.store import open_store, save
    from tests.analysis.helpers import playlist as sound_playlist
    sound_playlist(conn, "a", "Spotify · A", ["I1", "I2", "I3"])
    path = tmp_path / "sound.sqlite"
    assert "sound" not in sound_keys(conn, path)                             # analysis not set up
    sound = open_store(path)
    save(sound, "I1", "done", NOW - timedelta(hours=7))
    text = sound_keys(conn, path)["sound"]
    assert text.startswith("Klanganalyse steht seit") and "2 Titel offen" in text
    assert "sound" not in sound_keys(conn, path, now=NOW - timedelta(hours=2))  # last row 5 h before
    save(sound, "I2", "no_preview", NOW - timedelta(hours=7))
    save(sound, "I3", "error", NOW - timedelta(hours=7), error="x")
    assert "sound" not in sound_keys(conn, path)                             # nothing pending


def test_sound_analysis_that_never_wrote_a_row(conn, tmp_path):
    import os
    from analysis.store import open_store
    from tests.analysis.helpers import playlist as sound_playlist
    sound_playlist(conn, "a", "Spotify · A", ["I1"])
    path = tmp_path / "sound.sqlite"
    open_store(path).close()
    os.utime(path, ((NOW - timedelta(hours=8)).timestamp(),) * 2)
    assert "Klanganalyse" in sound_keys(conn, path)["sound"]
    os.utime(path, ((NOW - timedelta(hours=1)).timestamp(),) * 2)
    assert "sound" not in sound_keys(conn, path)


def _every_problem_state(conn, tmp_path):
    """A database in which every problem of build_health is present at once (the first of each either/or pair)."""
    from analysis.store import open_store, save
    from tests.analysis.helpers import playlist as sound_playlist
    set_setting(conn, "ingestion_state", "auth_expired")
    playlist(conn, "discover_weekly", "error", "boom")
    record_poll(conn, NOW - timedelta(hours=1), ok=False, detail="TimeoutError: x")
    record_poll(conn, NOW - timedelta(minutes=30), ok=False, detail="TimeoutError: x")
    record_capture(conn, NOW - timedelta(minutes=20), up=False)
    record_backup(conn, NOW - timedelta(hours=2), ok=False, detail="OSError: full")
    set_setting(conn, "feedback_paused", "false")
    conn.execute("INSERT INTO adapter_health(ts, state, detail) VALUES (?, 'unreachable', 'ConnectError')",
                 (iso(NOW - timedelta(minutes=40)),))
    for _ in range(5):
        conn.execute("INSERT INTO feedback_job(kind, spotify_id, original_ts, status, created_at, finished_at) "
                     "VALUES ('play', 's', 't', 'failed', 't', ?)", (iso(NOW - timedelta(hours=1)),))
    conn.execute("INSERT INTO feedback_job(kind, spotify_id, original_ts, status, created_at, started_at) "
                 "VALUES ('play', 's', 't', 'running', 't', ?)", (iso(NOW - timedelta(minutes=45)),))
    covers(conn, [(f"A{i}", ["psytrance"]) for i in range(8)] + [("B", ["zeuhl"]), ("C", ["zeuhl"]),
                                                                    ("D", ["kwaito"])])
    sound_playlist(conn, "a", "Spotify · A", ["I1", "I2"])
    path = tmp_path / "sound.sqlite"
    save(open_store(path), "I1", "done", NOW - timedelta(hours=7))
    return path


def _other_problem_state(conn):
    """The second branch of each either/or problem: hub error, overdue poll, old backup."""
    set_setting(conn, "ingestion_state", "error")
    record_poll(conn, NOW - timedelta(hours=4), ok=True)
    record_backup(conn, NOW - timedelta(hours=31), ok=True)


def test_every_problem_text_in_german(conn, tmp_path):
    path = _every_problem_state(conn, tmp_path)
    problems = build_health(conn, NOW, 3600, 1 * GB, sound_path=path, language="de")["problems"]
    assert [(p["key"], p["text"]) for p in problems] == [
        ("spotify", "Spotify-Anmeldung abgelaufen: sp_dc-Cookie erneuern"),
        ("playlist:discover_weekly", "Spotify · discover_weekly: boom"),
        ("poll", "Abruf 2x fehlgeschlagen: TimeoutError: x"),
        ("capture", "MA-Ereignisverbindung getrennt seit 2026-10-05T11:40"),
        ("backup", "Backup fehlgeschlagen: 2026-10-05T10:00:00+00:00 OSError: full"),
        ("adapter", "Spotify-Adapter unreachable seit 2026-10-05T11:20: ConnectError"),
        ("jobs", "5 Feedback-Jobs in 24 h fehlgeschlagen"),
        ("queue", "Feedback-Job läuft seit 2026-10-05T11:15"),
        ("genre_wheel", "Genre-Farbkreis neu berechnen: 27 % der Cover-Genres ohne festen Platz (zeuhl, kwaito)"),
        ("sound", "Klanganalyse steht seit 2026-10-05T05:00 (1 Titel offen)"),
        ("disk", "wenig Speicher: 1.0 GB frei"),
    ]
    other = connect(tmp_path / "other.sqlite")
    migrate(other)
    _other_problem_state(other)
    assert [(p["key"], p["text"]) for p in build_health(other, NOW, 3600, 50 * GB, language="de")["problems"]] == [
        ("spotify", "Spotify: Hub nicht abrufbar"),
        ("poll", "kein erfolgreicher Abruf seit 2026-10-05T08:00"),
        ("backup", "letztes Backup 2026-10-04T05:00"),
    ]


def test_every_problem_text_in_english(conn, tmp_path):
    path = _every_problem_state(conn, tmp_path)
    problems = build_health(conn, NOW, 3600, 1 * GB, sound_path=path, language="en")["problems"]
    assert [(p["key"], p["text"]) for p in problems] == [
        ("spotify", "Spotify login expired: renew the sp_dc cookie"),
        ("playlist:discover_weekly", "Spotify · discover_weekly: boom"),
        ("poll", "Poll failed 2 times: TimeoutError: x"),
        ("capture", "MA event connection down since 2026-10-05T11:40"),
        ("backup", "Backup failed: 2026-10-05T10:00:00+00:00 OSError: full"),
        ("adapter", "Spotify adapter unreachable since 2026-10-05T11:20: ConnectError"),
        ("jobs", "5 feedback jobs failed in 24 h"),
        ("queue", "Feedback job running since 2026-10-05T11:15"),
        ("genre_wheel", "Recompute the genre colour wheel: 27 % of the cover genres have no fixed place "
                        "(zeuhl, kwaito)"),
        ("sound", "Sound analysis stalled since 2026-10-05T05:00 (1 waiting)"),
        ("disk", "Low disk space: 1.0 GB free"),
    ]
    other = connect(tmp_path / "other.sqlite")
    migrate(other)
    _other_problem_state(other)
    assert [(p["key"], p["text"]) for p in build_health(other, NOW, 3600, 50 * GB, language="en")["problems"]] == [
        ("spotify", "Spotify: hub not reachable"),
        ("poll", "No successful poll since 2026-10-05T08:00"),
        ("backup", "Last backup 2026-10-04T05:00"),
    ]
