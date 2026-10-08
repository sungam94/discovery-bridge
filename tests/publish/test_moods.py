import logging
from datetime import datetime, timezone

import pytest

from analysis.store import open_store, save
from bridge.publish.moods import mood_words, playlist_moods, source_moods
from tests.analysis.helpers import bridge_db, playlist, source_playlist

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


@pytest.fixture
def bridge(tmp_path):
    return bridge_db(tmp_path / "bridge.sqlite")


@pytest.fixture
def sound(tmp_path):
    return open_store(tmp_path / "sound.sqlite")


def done(sound, isrc, moods, instrumental=0.9):
    save(sound, isrc, "done", NOW, moods=moods, instrumental=instrumental, embedding=[0.0])


def test_words_skip_themes_that_are_not_moods_and_keep_a_close_second():
    assert mood_words({"film": 0.9, "space": 0.5, "energetic": 0.4, "dark": 0.1}) == ["space", "energetic"]
    assert mood_words({"space": 0.5, "energetic": 0.2, "dark": 0.1}) == ["space"]  # second word too weak
    assert mood_words({"background": 0.5, "melodic": 0.4}) == []


def test_missing_sound_file_gives_no_moods(bridge, tmp_path):
    playlist(bridge, "a", "Spotify · A", ["I1"])
    assert playlist_moods(bridge, tmp_path / "nothing.sqlite") == {}


def test_empty_sound_file_gives_no_moods(bridge, tmp_path):
    playlist(bridge, "a", "Spotify · A", ["I1"])
    (tmp_path / "empty.sqlite").touch()
    assert playlist_moods(bridge, tmp_path / "empty.sqlite") == {}


def test_an_unreadable_sound_file_gives_none_and_a_warning(bridge, tmp_path, caplog):
    playlist(bridge, "a", "Spotify · A", ["I1"])
    (tmp_path / "broken.sqlite").write_bytes(b"not a database" * 512)
    caplog.set_level(logging.WARNING)
    assert playlist_moods(bridge, tmp_path / "broken.sqlite") is None
    assert any("sound.sqlite" in r.getMessage() for r in caplog.records)


def test_moods_average_over_the_analysed_tracks(bridge, sound, tmp_path):
    playlist(bridge, "a", "Spotify · A", ["I1", "I2", "I3", "I4", "I5", "I6"])
    for i in range(1, 6):
        done(sound, f"I{i}", {"space": 0.6, "dark": 0.2, "trailer": 0.9}, instrumental=0.8 if i < 5 else 0.3)
    save(sound, "I6", "no_preview", NOW)
    assert playlist_moods(bridge, tmp_path / "sound.sqlite") == {
        "Spotify · A": {"words": ["space"], "instrumental": 0.7}}


def test_a_playlist_needs_90_percent_of_its_tracks_and_five_done(bridge, sound, tmp_path):
    isrcs = [f"I{n:02}" for n in range(20)]
    playlist(bridge, "a", "Spotify · A", isrcs)                      # 17 of 20 have a row: 85 %
    playlist(bridge, "b", "Spotify · B", ["B1", "B2", "B3", "B4"])   # all done, but only four
    for isrc in isrcs[:17] + ["B1", "B2", "B3", "B4"]:
        done(sound, isrc, {"calm": 0.5})
    assert playlist_moods(bridge, tmp_path / "sound.sqlite") == {}
    save(sound, isrcs[17], "error", NOW, error="x")                 # 18 of 20: 90 %
    assert list(playlist_moods(bridge, tmp_path / "sound.sqlite")) == ["Spotify · A"]


def test_only_live_published_playlists_and_tracks_with_isrc_count(bridge, sound, tmp_path):
    playlist(bridge, "a", "Spotify · A", ["I1", "I2", "I3", "I4", "I5", None])
    playlist(bridge, "s", "Spotify · Stale", ["I1", "I2", "I3", "I4", "I5"], stale=1)
    for i in range(1, 6):
        done(sound, f"I{i}", {"calm": 0.5})
    assert list(playlist_moods(bridge, tmp_path / "sound.sqlite")) == ["Spotify · A"]


def test_tidal_discover_playlists_get_moods_by_the_same_rule(bridge, sound, tmp_path):
    source_playlist(bridge, "tidal--a://playlist/1", "tidal", ["I1", "I2", "I3", "I4", "I5", None])
    source_playlist(bridge, "tidal--a://playlist/2", "tidal", ["I1", "I2", "I3", "I4"])        # only four
    source_playlist(bridge, "soundcloud--b://playlist/3", "soundcloud", ["I1", "I2", "I3", "I4", "I5"])
    for i in range(1, 6):
        done(sound, f"I{i}", {"dark": 0.5}, instrumental=0.4)
    assert source_moods(bridge, tmp_path / "sound.sqlite") == {
        "tidal--a://playlist/1": {"words": ["dark"], "instrumental": 0.4}}
    assert source_moods(bridge, None) == {}
    (tmp_path / "broken.sqlite").write_bytes(b"not a database" * 512)
    assert source_moods(bridge, tmp_path / "broken.sqlite") is None
