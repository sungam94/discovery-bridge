from datetime import datetime, timezone

import pytest

from bridge.db import connect, migrate
from bridge.genre.musicbrainz import MusicBrainzError
from bridge.genre.worker import GenreWorker
from bridge.repo import get_setting
from tests.genre.test_store import playlist

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


class FakeMb:
    def __init__(self, data, broken=()):
        self.data, self.broken, self.asked = data, set(broken), []

    async def artist(self, name):
        self.asked.append(name)
        if name in self.broken:
            raise MusicBrainzError("busy")
        return self.data.get(name, (None, []))


def worker(conn, mb, style=None, hues=None, source_moods=None):
    log = {"layout": 0, "covers": []}

    async def refresh(pid):
        log["covers"].append(pid)

    def layout():
        log["layout"] += 1

    return GenreWorker(conn, mb, lambda: NOW, layout, refresh, style or {"grid": 3}, hues,
                       source_moods=source_moods), log


async def test_fetches_pending_artists_then_writes_layout_and_refreshes_the_cover_once(conn):
    playlist(conn, "a", "Spotify · A", ["One", "Two"])
    mb = FakeMb({"One": ("m1", [("jazz", 3)]), "Two": ("m2", [("jazz", 1)])})
    w, log = worker(conn, mb)
    assert await w.run_once(batch=10) == 2
    assert log == {"layout": 1, "covers": ["7"]}
    assert await w.run_once(batch=10) == 0          # nothing pending, genres unchanged
    assert log == {"layout": 1, "covers": ["7"]} and mb.asked == ["One", "Two"]


async def test_cover_waits_until_all_artists_of_the_playlist_are_looked_up(conn):
    playlist(conn, "a", "Spotify · A", ["One", "Two"])
    w, log = worker(conn, FakeMb({"One": ("m1", [("jazz", 3)]), "Two": ("m2", [("jazz", 1)])}))
    await w.run_once(batch=1)
    assert log["covers"] == []
    await w.run_once(batch=1)
    assert log["covers"] == ["7"]


async def test_a_failed_lookup_is_retried_later_and_does_not_stop_the_batch(conn):
    playlist(conn, "a", "Spotify · A", ["Broken", "Fine"])
    mb = FakeMb({"Fine": ("m", [("jazz", 1)]), "Broken": ("b", [("dub", 1)])}, broken=["Broken"])
    w, log = worker(conn, mb)
    assert await w.run_once(batch=10) == 1 and log["covers"] == []
    mb.broken.clear()
    assert await w.run_once(batch=10) == 1 and log["covers"] == ["7"]


async def test_playlist_without_any_genre_is_redrawn_once_for_the_cover_style(conn):
    playlist(conn, "a", "Spotify · A", ["Nobody"])
    w, log = worker(conn, FakeMb({}))
    await w.run_once(batch=10)
    await w.run_once(batch=10)
    assert log == {"layout": 1, "covers": ["7"]}


async def test_a_changed_cover_style_redraws_the_covers(conn):
    playlist(conn, "a", "Spotify · A", ["One"])
    mb = FakeMb({"One": ("m", [("jazz", 1)])})
    w, log = worker(conn, mb)
    await w.run_once(batch=10)
    w2, log2 = worker(conn, mb, style={"grid": 4})
    await w2.run_once(batch=10)
    await w2.run_once(batch=10)
    assert log["covers"] == ["7"] and log2["covers"] == ["7"]


async def test_a_failed_cover_refresh_is_tried_again(conn):
    playlist(conn, "a", "Spotify · A", ["One"])
    calls = []

    async def refresh(pid):
        calls.append(pid)
        if len(calls) == 1:
            raise RuntimeError("MA down")

    w = GenreWorker(conn, FakeMb({"One": ("m", [("jazz", 1)])}), lambda: NOW, lambda: None, refresh, {"grid": 3})
    await w.run_once(batch=10)
    await w.run_once(batch=10)
    await w.run_once(batch=10)
    assert calls == ["7", "7"]


async def test_a_changed_genre_hue_redraws_the_covers_that_show_the_genre(conn):
    playlist(conn, "a", "Spotify · A", ["One"])
    mb = FakeMb({"One": ("m", [("jazz", 1)])})
    w, log = worker(conn, mb)
    await w.run_once(batch=10)
    w2, log2 = worker(conn, mb, hues={"jazz": 99})
    await w2.run_once(batch=10)
    await w2.run_once(batch=10)
    assert log["covers"] == ["7"] and log2["covers"] == ["7"]


async def test_cover_is_redrawn_once_when_its_moods_appear_or_change(conn):
    playlist(conn, "a", "Spotify · A", ["One"])
    moods = {}
    log = {"layout": 0, "covers": []}

    async def refresh(pid):
        log["covers"].append(pid)

    def layout():
        log["layout"] += 1

    w = GenreWorker(conn, FakeMb({"One": ("m", [("jazz", 1)])}), lambda: NOW, layout, refresh, {"grid": 3},
                    moods=lambda: moods)
    await w.run_once(batch=10)
    await w.run_once(batch=10)
    assert log == {"layout": 1, "covers": ["7"]}
    moods["Spotify · A"] = {"words": ["dark"], "instrumental": 0.9}   # no artist pending: the loop still checks
    await w.run_once(batch=10)
    await w.run_once(batch=10)
    assert log == {"layout": 2, "covers": ["7", "7"]}
    moods["Spotify · A"] = {"words": ["dark", "epic"], "instrumental": 0.9}
    await w.run_once(batch=10)
    assert log == {"layout": 3, "covers": ["7", "7", "7"]}
    moods["Spotify · B"] = {"words": ["calm"], "instrumental": 0.1}  # another playlist: this cover stays
    await w.run_once(batch=10)
    assert log == {"layout": 3, "covers": ["7", "7", "7"]}


async def test_unreadable_moods_skip_the_pass_instead_of_dropping_the_footers(conn):
    playlist(conn, "a", "Spotify · A", ["One"])
    playlist(conn, "b", "Spotify · B", ["Two"])
    moods = {"Spotify · A": {"words": ["dark"], "instrumental": 0.9}}
    log = {"layout": 0, "covers": []}

    async def refresh(pid):
        log["covers"].append(pid)

    def layout():
        log["layout"] += 1

    w = GenreWorker(conn, FakeMb({"One": ("m", [("jazz", 1)]), "Two": ("m2", [("rock", 1)])}), lambda: NOW,
                    layout, refresh, {"grid": 3}, moods=lambda: moods)
    await w.run_once(batch=10)
    assert len(log["covers"]) == 2
    moods = None                                                        # sound.sqlite cannot be read
    await w.run_once(batch=10)
    assert log == {"layout": 1, "covers": ["7", "7"]}
    moods = {"Spotify · A": {"words": ["dark"], "instrumental": 0.9}}   # readable again: nothing to redraw
    await w.run_once(batch=10)
    assert log == {"layout": 1, "covers": ["7", "7"]}


async def test_covers_drawn_before_the_brand_frame_are_redrawn_once(conn):
    import json
    from bridge.repo import set_setting
    playlist(conn, "a", "Spotify · A", ["One"])
    mb = FakeMb({"One": ("m", [("jazz", 1)])})
    w, log = worker(conn, mb)
    await w.run_once(batch=10)
    drawn = json.loads(get_setting(conn, "cover_genres"))
    drawn["Spotify · A"].pop("version")                                 # as stored by the bridge before the frame
    set_setting(conn, "cover_genres", json.dumps(drawn))
    await w.run_once(batch=10)
    await w.run_once(batch=10)
    assert log["covers"] == ["7", "7"]


def discover(conn, uri, kind, artists, tags=()):
    from tests.genre.test_source_genres import source
    source(conn, uri, kind, [(a, list(tags)) for a in artists])


async def test_discover_playlists_rewrite_the_layout_without_asking_ma(conn):
    discover(conn, "tidal--a://playlist/1", "tidal", ["One", "Two"])
    moods = {}
    w, log = worker(conn, FakeMb({"One": ("m", [("jazz", 1)]), "Two": ("n", [("dub", 1)])}),
                    source_moods=lambda: moods)
    await w.run_once(batch=1)                       # Two still pending: not listed yet, nothing to write
    assert log == {"layout": 0, "covers": []}
    await w.run_once(batch=1)
    assert log == {"layout": 1, "covers": []}       # MA is not asked to refresh anything for these
    await w.run_once(batch=1)
    assert log == {"layout": 1, "covers": []}
    moods["tidal--a://playlist/1"] = {"words": ["dark"], "instrumental": 0.5}
    await w.run_once(batch=1)
    assert log == {"layout": 2, "covers": []}
    conn.execute("DELETE FROM source_playlist")     # left its row
    await w.run_once(batch=1)
    assert log == {"layout": 3, "covers": []}


async def test_a_soundcloud_playlist_is_written_at_once_and_unreadable_moods_skip_the_check(conn):
    discover(conn, "soundcloud--b://playlist/2", "soundcloud", ["dj"], tags=["Darkpsy"])
    moods = None
    w, log = worker(conn, FakeMb({}), source_moods=lambda: moods)
    await w.run_once(batch=1)
    assert log == {"layout": 0, "covers": []}
    moods = {}
    await w.run_once(batch=1)
    assert log == {"layout": 1, "covers": []}
