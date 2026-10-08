import sqlite3
from datetime import datetime, timezone

import pytest

from bridge.db import connect, migrate
from bridge.repo import get_setting, is_paused, set_setting
from bridge.timeutil import iso, parse_iso


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def test_migrate_is_idempotent(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    assert migrate(c) == ["001_init.sql", "002_hub_playlists.sql", "003_capture.sql", "004_feedback.sql",
                          "005_artist_genre.sql", "006_genre_hue.sql", "007_artist_info.sql", "008_artist_info_raw.sql", "009_played_artist.sql",
                          "010_source_playlists.sql", "011_source_track_image.sql"]
    assert migrate(c) == []
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_migrations", "setting", "source_track", "track_mapping", "excluded_item", "manual_override",
            "playlist", "unslotted_playlist", "playlist_snapshot", "playlist_snapshot_item",
            "ingestion_health", "source_playlist", "source_playlist_track", "source_track_isrc"} <= tables


def test_source_playlist_tracks_go_with_their_playlist(conn):
    conn.execute("INSERT INTO source_playlist(uri, name, source, row_name, fetched_at) "
                 "VALUES ('tidal--a://playlist/1', 'My Mix 1', 'tidal', 'Custom mixes', 't')")
    conn.execute("INSERT INTO source_playlist_track(playlist_uri, position, track_uri, artists) "
                 "VALUES ('tidal--a://playlist/1', 0, 'tidal--a://track/9', 'A')")
    conn.execute("DELETE FROM source_playlist")
    assert conn.execute("SELECT COUNT(*) FROM source_playlist_track").fetchone()[0] == 0


def test_seeded_settings(conn):
    assert is_paused(conn, "ingestion_paused") is False
    assert is_paused(conn, "feedback_paused") is True
    set_setting(conn, "ingestion_paused", "true")
    assert get_setting(conn, "ingestion_paused") == "true"


def test_missing_pause_row_means_paused(conn):
    conn.execute("DELETE FROM setting WHERE key='feedback_paused'")
    assert is_paused(conn, "feedback_paused") is True


def test_forward_mapping_unique_except_invalidated(conn):
    ins = ("INSERT INTO track_mapping(direction, spotify_id, status, resolved_at) "
           "VALUES ('forward', 'X', ?, '2026-10-04T00:00:00+00:00')")
    conn.execute(ins, ("invalidated",))
    conn.execute(ins, ("matched",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("pending",))


def test_iso_roundtrip():
    dt = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    assert parse_iso(iso(dt)) == dt


def test_capture_tables(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    c.execute("INSERT INTO taste_event(source, ma_item_uri, provider_item_id, started_at, outcome, policy_result) "
              "VALUES ('ma_play', 'tidal--T://track/1', '1', 't0', 'completed', 'eligible')")
    c.execute("INSERT OR IGNORE INTO taste_event(source, ma_item_uri, provider_item_id, started_at, outcome, "
              "policy_result) VALUES ('ma_play', 'tidal--T://track/1', '1', 't0', 'completed', 'eligible')")
    assert c.execute("SELECT count(*) FROM taste_event").fetchone()[0] == 1
    cols = {r[1] for r in c.execute("PRAGMA table_info(open_play)")}
    assert {"player_id", "ma_item_uri", "media_type", "seconds_played", "source_radio", "updated_at"} <= cols


def test_feedback_tables(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    c.execute("INSERT INTO taste_event(id, source, ma_item_uri, provider_item_id, started_at, outcome, policy_result) "
              "VALUES (1, 'ma_play', 'u', '1', 't', 'completed', 'eligible')")
    c.execute("INSERT INTO feedback_job(taste_event_id, kind, spotify_id, original_ts, status, created_at) "
              "VALUES (1, 'play', 's', 't', 'pending', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO feedback_job(taste_event_id, kind, spotify_id, original_ts, status, created_at) "
                  "VALUES (1, 'play', 's', 't', 'pending', 't')")
    assert {r[1] for r in c.execute("PRAGMA table_info(adapter_health)")} == {"id", "ts", "state", "detail"}
    assert {r[1] for r in c.execute("PRAGMA table_info(job_start)")} == {"id", "job_id", "ts"}
