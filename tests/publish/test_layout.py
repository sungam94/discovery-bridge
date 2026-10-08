import json

import pytest

from bridge.db import connect, migrate
from bridge.publish.layout import build_layout, write_layout

FOR_YOU, ARTISTS = "spotify:section:0JQ5DACFo5h0jxzOyHOsIe", "spotify:section:0JQ5DACFo5h0jxzOyHOsIa"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def add(conn, slot, kind, name, published=True, stale=0, section=None, pos=None):
    conn.execute("INSERT INTO playlist(slot, kind, name, ma_playlist_id, stale, section, hub_position) "
                 "VALUES (?,?,?,?,?,?,?)", (slot, kind, name, "1" if published else None, stale, section, pos))


def test_rows_in_fixed_order_with_only_published_live_playlists(conn):
    add(conn, "release_radar", "fixed", "Spotify · Release Radar")
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    add(conn, "daily_mix_2", "daily_mix", "Spotify · Daily Mix 2")
    add(conn, "daily_mix_1", "daily_mix", "Spotify · Daily Mix 1")
    add(conn, "daily_mix_3", "daily_mix", "Spotify · Daily Mix 3", published=False)
    add(conn, "daylist", "daylist", "Spotify · daylist")
    add(conn, "hub:b", "hub", "Spotify · Oakmere Mix", section=ARTISTS, pos=1)
    add(conn, "hub:a", "hub", "Spotify · Hazy Fern Mix", section=ARTISTS, pos=0)
    add(conn, "hub:old", "hub", "Spotify · Gone Mix", section=ARTISTS, pos=2, stale=1)
    add(conn, "hub:rep", "hub", "Spotify · On Repeat", section=FOR_YOU, pos=1)
    rows = build_layout(conn, (FOR_YOU, ARTISTS), language="de")
    assert [(r["id"], r["playlists"]) for r in rows] == [
        ("for_you", ["Spotify · Discover Weekly", "Spotify · Release Radar", "Spotify · daylist",
                     "Spotify · On Repeat"]),
        ("daily_mixes", ["Spotify · Daily Mix 1", "Spotify · Daily Mix 2"]),
        ("section_0JQ5DACFo5h0jxzOyHOsIa", ["Spotify · Hazy Fern Mix", "Spotify · Oakmere Mix"]),
    ]
    assert rows[0]["title"] == "Spotify · Für dich"
    assert rows[2]["title"] == "Spotify · Künstler-Mixe"


def test_empty_rows_are_left_out(conn):
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    assert [r["id"] for r in build_layout(conn, (FOR_YOU, ARTISTS), language="de")] == ["for_you"]


def test_write_layout_replaces_file_atomically(conn, tmp_path):
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, language="de")
    write_layout(conn, (), out, language="de")
    data = json.loads((out / "layout.json").read_text())
    assert data["version"] == 1 and data["rows"][0]["playlists"] == ["Spotify · Discover Weekly"]
    assert [p.name for p in out.iterdir()] == ["layout.json"]  # no temp files left behind


def snapshot_with_artists(conn, slot, artists):
    sid = conn.execute("INSERT INTO playlist_snapshot(slot, spotify_id, fetched_at, ordered_hash, set_hash) "
                       "VALUES (?, 'x', 't', 'o', 's')", (slot,)).lastrowid
    for pos, a in enumerate(artists):
        tid = conn.execute("INSERT INTO source_track(provider, provider_item_id, artist, title, album, duration_ms) "
                           "VALUES ('spotify', ?, ?, 't', 'al', 1000)", (f"{slot}{pos}", a)).lastrowid
        conn.execute("INSERT INTO playlist_snapshot_item VALUES (?, ?, ?)", (sid, pos, tid))
    conn.execute("UPDATE playlist SET published_snapshot_id = ? WHERE slot = ?", (sid, slot))


def test_subtitles_are_the_top_three_artists(conn):
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    snapshot_with_artists(conn, "discover_weekly", ["B", "A\x1fC", "A", "C", "D", "A", "B"])
    rows = build_layout(conn, (), language="de")
    assert rows[0]["subtitles"] == {"Spotify · Discover Weekly": "A, B, C"}  # A 3, B 2, C 2 (B first seen), D 1


def test_playlist_without_snapshot_has_no_subtitle(conn):
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    assert build_layout(conn, (), language="de")[0]["subtitles"] == {}


def test_layout_file_carries_playlist_genres_for_the_cover_frames(conn, tmp_path):
    from datetime import datetime, timezone
    from bridge.genre.store import save_artist
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    snapshot_with_artists(conn, "discover_weekly", ["Wildmoor"])
    save_artist(conn, "Wildmoor", "m", [("black metal", 4)], datetime(2026, 10, 4, tzinfo=timezone.utc))
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, language="de")
    assert json.loads((out / "layout.json").read_text())["genres"] == {"Spotify · Discover Weekly": [["black metal", 1]]}


def test_layout_file_carries_the_cover_style(conn, tmp_path):
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, cover_style={"bands": 6, "grid": 3}, language="de")
    assert json.loads((out / "layout.json").read_text())["cover_style"] == {"bands": 6, "grid": 3}


def test_layout_file_carries_the_hues_of_the_playlist_genres(conn, tmp_path):
    from datetime import datetime, timezone
    from bridge.genre.store import save_artist
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    snapshot_with_artists(conn, "discover_weekly", ["Wildmoor", "Someone"])
    save_artist(conn, "Wildmoor", "m", [("black metal", 4)], datetime(2026, 10, 4, tzinfo=timezone.utc))
    save_artist(conn, "Someone", "n", [("jazz", 1)], datetime(2026, 10, 4, tzinfo=timezone.utc))
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, hue_overrides={"jazz": 99}, language="de")
    assert json.loads((out / "layout.json").read_text())["hues"] == {"black metal": 0, "jazz": 99}


def test_layout_file_carries_the_playlist_moods(tmp_path):
    from analysis.store import open_store, save
    from tests.analysis.helpers import bridge_db, playlist
    from tests.publish.test_moods import NOW

    conn = bridge_db(tmp_path / "bridge.sqlite")
    playlist(conn, "discover_weekly", "Spotify · Discover Weekly", [f"I{n}" for n in range(5)])
    sound = open_store(tmp_path / "sound.sqlite")
    for n in range(5):
        save(sound, f"I{n}", "done", NOW, moods={"dark": 0.5, "epic": 0.4}, instrumental=0.2, embedding=[0.0])
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, {"grid": 3}, sound_path=tmp_path / "sound.sqlite", language="de")
    data = json.loads((out / "layout.json").read_text())
    assert data["moods"] == {"Spotify · Discover Weekly": {"words": ["dark", "epic"], "instrumental": 0.2}}
    assert set(data) == {"version", "rows", "genres", "hues", "cover_style", "moods", "sources"}
    (tmp_path / "broken.sqlite").write_bytes(b"not a database" * 512)
    write_layout(conn, (), out, {"grid": 3}, sound_path=tmp_path / "broken.sqlite", language="de")  # keeps the last moods
    assert json.loads((out / "layout.json").read_text())["moods"] == data["moods"]
    write_layout(conn, (), out, language="de")
    assert json.loads((out / "layout.json").read_text())["moods"] == {}


def test_layout_file_carries_the_discover_playlists_of_other_providers(tmp_path):
    from datetime import datetime, timezone
    from analysis.store import open_store, save
    from bridge.genre.store import save_artist
    from tests.analysis.helpers import bridge_db, source_playlist

    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    conn = bridge_db(tmp_path / "bridge.sqlite")
    isrcs = [f"I{n}" for n in range(5)]
    source_playlist(conn, "tidal--a://playlist/1", "tidal", isrcs, name="My Mix 1")
    conn.execute("UPDATE source_playlist_track SET artists = CASE position WHEN 0 THEN 'Zeuhl Band' ELSE 'Astrix' END")
    source_playlist(conn, "tidal--a://playlist/2", "tidal", ["J1"], name="My Mix 2")
    conn.execute("UPDATE source_playlist_track SET artists = 'Unknown' WHERE playlist_uri = 'tidal--a://playlist/2'")
    source_playlist(conn, "soundcloud--b://playlist/3", "soundcloud", [None, None], name="Your Mix 1")
    conn.execute("UPDATE source_playlist_track SET genres = '[\"Darkpsy\", \"Sectio Aurea RMX\"]' "
                 "WHERE playlist_uri = 'soundcloud--b://playlist/3'")
    save_artist(conn, "Zeuhl Band", "m1", [("zeuhl", 5), ("progressive rock", 1)], now)  # zeuhl: no hue
    save_artist(conn, "Astrix", "m2", [("psytrance", 5)], now)
    sound = open_store(tmp_path / "sound.sqlite")
    for i in isrcs:
        save(sound, i, "done", now, moods={"dark": 0.5}, instrumental=0.9, embedding=[0.0])
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, {"grid": 3}, sound_path=tmp_path / "sound.sqlite", now=now, language="de")
    data = json.loads((out / "layout.json").read_text())
    assert data["sources"] == {   # My Mix 2 waits until its artists are looked up
        "tidal--a://playlist/1": {"name": "My Mix 1", "source": "tidal",
                                  "genres": [["psytrance", 4], ["progressive rock", 1]],
                                  "moods": {"words": ["dark"], "instrumental": 0.9}},
        "soundcloud--b://playlist/3": {"name": "Your Mix 1", "source": "soundcloud", "genres": [["darkpsy", 2]]}}
    assert {"psytrance", "progressive rock", "darkpsy"} <= set(data["hues"]) and "zeuhl" not in data["hues"]
    (tmp_path / "broken.sqlite").write_bytes(b"not a database" * 512)
    write_layout(conn, (), out, {"grid": 3}, sound_path=tmp_path / "broken.sqlite", now=now, language="de")  # keeps the moods
    assert json.loads((out / "layout.json").read_text())["sources"] == data["sources"]


def test_row_titles_for_every_section_in_german(conn):
    known = ["spotify:section:0JQ5DACFo5h0jxzOyHOsIa", "spotify:section:0JQ5DACFo5h0jxzOyHOsIc",
             "spotify:section:0JQ5DACFo5h0jxzOyHOsIp", "spotify:section:0JQ5DACFo5h0jxzOyHOsI9",
             "spotify:section:0JQ5DACFo5h0jxzOyHOsIb", "spotify:section:0JQ5DATaxswzruE2nWp3Lr"]
    unknown = "spotify:section:0JQ5DAUnknownSection01"
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    add(conn, "daily_mix_1", "daily_mix", "Spotify · Daily Mix 1")
    for i, sec in enumerate(known + [unknown]):
        add(conn, f"hub:{i}", "hub", f"Spotify · Mix {i}", section=sec, pos=0)
    rows = build_layout(conn, tuple([FOR_YOU] + known + [unknown]), language="de")
    assert [(r["id"], r["title"]) for r in rows] == [
        ("for_you", "Spotify · Für dich"),
        ("daily_mixes", "Spotify · Daily Mixes"),
        ("section_0JQ5DACFo5h0jxzOyHOsIa", "Spotify · Künstler-Mixe"),
        ("section_0JQ5DACFo5h0jxzOyHOsIc", "Spotify · Stimmungs-Mixe"),
        ("section_0JQ5DACFo5h0jxzOyHOsIp", "Spotify · Blends"),
        ("section_0JQ5DACFo5h0jxzOyHOsI9", "Spotify · Genre-Mixe"),
        ("section_0JQ5DACFo5h0jxzOyHOsIb", "Spotify · Jahrzehnte-Mixe"),
        ("section_0JQ5DATaxswzruE2nWp3Lr", "Spotify · Nischen-Mixe"),
        ("section_0JQ5DAUnknownSection01", "Spotify · Mixe"),
    ]


def test_row_titles_in_english(conn):
    known = ["spotify:section:0JQ5DACFo5h0jxzOyHOsIa", "spotify:section:0JQ5DACFo5h0jxzOyHOsIc",
             "spotify:section:0JQ5DACFo5h0jxzOyHOsIp", "spotify:section:0JQ5DACFo5h0jxzOyHOsI9",
             "spotify:section:0JQ5DACFo5h0jxzOyHOsIb", "spotify:section:0JQ5DATaxswzruE2nWp3Lr"]
    unknown = "spotify:section:0JQ5DAUnknownSection01"
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    add(conn, "daily_mix_1", "daily_mix", "Spotify · Daily Mix 1")
    for i, sec in enumerate(known + [unknown]):
        add(conn, f"hub:{i}", "hub", f"Spotify · Mix {i}", section=sec, pos=0)
    rows = build_layout(conn, tuple([FOR_YOU] + known + [unknown]), language="en")
    assert [(r["id"], r["title"]) for r in rows] == [
        ("for_you", "Spotify · Made for you"),
        ("daily_mixes", "Spotify · Daily Mixes"),
        ("section_0JQ5DACFo5h0jxzOyHOsIa", "Spotify · Artist mixes"),
        ("section_0JQ5DACFo5h0jxzOyHOsIc", "Spotify · Mood mixes"),
        ("section_0JQ5DACFo5h0jxzOyHOsIp", "Spotify · Blends"),
        ("section_0JQ5DACFo5h0jxzOyHOsI9", "Spotify · Genre mixes"),
        ("section_0JQ5DACFo5h0jxzOyHOsIb", "Spotify · Decade mixes"),
        ("section_0JQ5DATaxswzruE2nWp3Lr", "Spotify · Niche mixes"),
        ("section_0JQ5DAUnknownSection01", "Spotify · Mixes"),
    ]


def test_write_layout_writes_titles_in_the_given_language(conn, tmp_path):
    add(conn, "discover_weekly", "fixed", "Spotify · Discover Weekly")
    out = tmp_path / "layout"
    out.mkdir()
    write_layout(conn, (), out, language="en")
    assert json.loads((out / "layout.json").read_text())["rows"][0]["title"] == "Spotify · Made for you"
