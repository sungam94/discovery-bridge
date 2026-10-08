import json
from datetime import datetime, timedelta, timezone

import pytest

from bridge.db import connect, migrate
from bridge.ma.client import MaError
from bridge.sources.collector import SourceCollector

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
ROWS = {"tidal": ("Custom mixes",), "soundcloud": ("Mixed for", "Made for you")}
TIDAL, SC = "tidal--test0001", "soundcloud--x1"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def pl(provider, item_id, name):
    return {"media_type": "playlist", "item_id": item_id, "provider": provider, "name": name,
            "uri": f"{provider}://playlist/{item_id}"}


def tr(provider, item_id, *artists, genres=(), isrc=None):
    t = {"media_type": "track", "item_id": item_id, "provider": provider, "uri": f"{provider}://track/{item_id}",
         "artists": [{"name": a} for a in artists], "metadata": {"genres": list(genres)}}
    if isrc:
        t["external_ids"] = [["isrc", isrc]]
    return t


class FakeMa:
    def __init__(self):
        self.rows, self.items, self.tracks, self.full = [], {}, {}, {}
        self.broken_items, self.broken_tracks = set(), set()
        self.calls = []

    async def recommendation_rows(self):
        self.calls.append(("rows",))
        return self.rows

    async def recommendation_items(self, provider, item_id):
        self.calls.append(("items", provider, item_id))
        return self.items.get((provider, item_id), [])

    async def provider_playlist_tracks(self, item_id, provider):
        self.calls.append(("tracks", provider, item_id))
        if (provider, item_id) in self.broken_tracks:
            raise MaError("provider busy")
        return self.tracks.get((provider, item_id), [])

    async def get_item(self, uri):
        self.calls.append(("item", uri))
        if uri in self.broken_items:
            raise MaError("busy")
        return self.full.get(uri, {"uri": uri})


def collector(conn, ma, now=NOW, rows=ROWS):
    return SourceCollector(conn, ma, lambda: now, rows, pause_s=0)


def stored(conn):
    return {r["uri"]: (r["name"], r["source"], r["row_name"]) for r in conn.execute("SELECT * FROM source_playlist")}


def tracks(conn, uri):
    return [(r["position"], r["track_uri"], r["artists"], r["isrc"], json.loads(r["genres"])) for r in conn.execute(
        "SELECT * FROM source_playlist_track WHERE playlist_uri = ? ORDER BY position", (uri,))]


def setup_rows(ma):
    ma.rows = [{"item_id": "mixes", "provider": TIDAL, "name": "Custom mixes"},
               {"item_id": "other", "provider": TIDAL, "name": "Popular playlists"},
               {"item_id": "mf", "provider": SC, "name": "Mixed for listener"},
               {"item_id": "layout_for_you", "provider": "spotify_bridge--1", "name": "Spotify · Für dich"}]
    ma.items[(TIDAL, "mixes")] = [pl(TIDAL, "p1", "My Mix 1"), {"media_type": "track", "uri": f"{TIDAL}://track/x"}]
    ma.items[(TIDAL, "other")] = [pl(TIDAL, "p9", "Top 40")]
    ma.items[(SC, "mf")] = [pl(SC, "s1", "Your Mix 1")]
    ma.tracks[(TIDAL, "p1")] = [tr(TIDAL, "1", "Hazy Fern", "Wren"), tr(TIDAL, "2", "Astrix", isrc="IL1")]
    ma.tracks[(SC, "s1")] = [tr(SC, "a", "dj x", genres=["Darkpsy"]), tr(SC, "b", "dj y")]
    ma.full[f"{TIDAL}://track/1"] = {"external_ids": [["barcode", "0"], ["isrc", "US1"]]}


async def test_collects_the_playlists_of_the_configured_rows_with_their_tracks(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    assert stored(conn) == {f"{TIDAL}://playlist/p1": ("My Mix 1", "tidal", "Custom mixes"),
                            f"{SC}://playlist/s1": ("Your Mix 1", "soundcloud", "Mixed for listener")}
    assert tracks(conn, f"{TIDAL}://playlist/p1") == [
        (0, f"{TIDAL}://track/1", "Hazy Fern\x1fWren", "US1", []),
        (1, f"{TIDAL}://track/2", "Astrix", "IL1", [])]           # ISRC from the listing: no extra call
    assert tracks(conn, f"{SC}://playlist/s1") == [
        (0, f"{SC}://track/a", "dj x", None, ["Darkpsy"]), (1, f"{SC}://track/b", "dj y", None, [])]
    assert ("items", TIDAL, "other") not in ma.calls and ("items", "spotify_bridge--1", "layout_for_you") not in ma.calls
    assert [c for c in ma.calls if c[0] == "item"] == [("item", f"{TIDAL}://track/1")]   # SoundCloud: never


async def test_a_tidal_isrc_is_asked_once_per_track_even_when_missing(conn):
    ma = FakeMa()
    setup_rows(ma)
    ma.items[(TIDAL, "mixes")].append(pl(TIDAL, "p2", "My Mix 2"))
    ma.tracks[(TIDAL, "p2")] = [tr(TIDAL, "1", "Hazy Fern"), tr(TIDAL, "3", "Nobody")]   # 3: the item has no ISRC
    await collector(conn, ma).run_once()
    await collector(conn, ma).run_once()
    assert sorted(c[1] for c in ma.calls if c[0] == "item") == [f"{TIDAL}://track/1", f"{TIDAL}://track/3"]
    assert [t[3] for t in tracks(conn, f"{TIDAL}://playlist/p2")] == ["US1", None]


async def test_a_failed_isrc_lookup_is_tried_again_on_the_next_pass(conn):
    ma = FakeMa()
    setup_rows(ma)
    ma.broken_items.add(f"{TIDAL}://track/1")
    await collector(conn, ma).run_once()
    assert tracks(conn, f"{TIDAL}://playlist/p1")[0][3] is None
    ma.broken_items.clear()
    await collector(conn, ma).run_once()
    assert tracks(conn, f"{TIDAL}://playlist/p1")[0][3] == "US1"


async def test_a_playlist_that_left_its_row_is_dropped(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    ma.items[(TIDAL, "mixes")] = [pl(TIDAL, "p2", "My Mix 2")]
    ma.tracks[(TIDAL, "p2")] = [tr(TIDAL, "5", "Astrix", isrc="IL5")]
    await collector(conn, ma).run_once()
    assert set(stored(conn)) == {f"{TIDAL}://playlist/p2", f"{SC}://playlist/s1"}
    assert tracks(conn, f"{TIDAL}://playlist/p1") == []


async def test_an_empty_or_missing_row_keeps_its_playlists_for_a_while(conn):
    """MA answers a provider error or timeout with an empty row; that must not take the covers away."""
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    ma.items[(TIDAL, "mixes")] = []
    ma.rows = [r for r in ma.rows if r["item_id"] != "mf"]
    await collector(conn, ma, now=NOW + timedelta(days=1)).run_once()
    assert set(stored(conn)) == {f"{TIDAL}://playlist/p1", f"{SC}://playlist/s1"}
    await collector(conn, ma, now=NOW + timedelta(days=8)).run_once()
    assert stored(conn) == {}


async def test_a_playlist_whose_tracks_cannot_be_read_keeps_its_old_tracks(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    ma.broken_tracks.add((TIDAL, "p1"))
    ma.tracks[(SC, "s1")] = []
    await collector(conn, ma, now=NOW + timedelta(days=1)).run_once()
    assert len(tracks(conn, f"{TIDAL}://playlist/p1")) == 2 and len(tracks(conn, f"{SC}://playlist/s1")) == 2
    assert set(stored(conn)) == {f"{TIDAL}://playlist/p1", f"{SC}://playlist/s1"}


async def test_a_row_that_fails_keeps_its_playlists_and_the_pass_goes_on(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    original = ma.recommendation_items

    async def flaky(provider, item_id):
        if item_id == "mixes":
            raise MaError("timeout")
        return await original(provider, item_id)

    ma.recommendation_items = flaky
    ma.tracks[(SC, "s1")] = [tr(SC, "c", "dj z", genres=["Trance"])]
    await collector(conn, ma).run_once()
    assert set(stored(conn)) == {f"{TIDAL}://playlist/p1", f"{SC}://playlist/s1"}
    assert tracks(conn, f"{SC}://playlist/s1") == [(0, f"{SC}://track/c", "dj z", None, ["Trance"])]


async def test_playlists_of_a_row_no_longer_configured_are_dropped(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    await collector(conn, ma, rows={"tidal": ("Custom mixes",)}).run_once()
    assert set(stored(conn)) == {f"{TIDAL}://playlist/p1"}


async def test_a_changed_playlist_replaces_its_tracks(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    ma.tracks[(TIDAL, "p1")] = [tr(TIDAL, "2", "Astrix", isrc="IL1")]
    await collector(conn, ma).run_once()
    assert tracks(conn, f"{TIDAL}://playlist/p1") == [(0, f"{TIDAL}://track/2", "Astrix", "IL1", [])]


async def test_row_names_match_their_prefix_whatever_the_case(conn):
    ma = FakeMa()
    setup_rows(ma)
    ma.rows[0]["name"] = "CUSTOM MIXES"
    await collector(conn, ma).run_once()
    assert f"{TIDAL}://playlist/p1" in stored(conn)


async def test_a_configured_source_without_a_matching_row_is_warned_about_with_the_names_seen(conn, caplog):
    ma = FakeMa()
    setup_rows(ma)
    ma.rows = [r for r in ma.rows if r["provider"] != SC] + [{"item_id": "x", "provider": SC, "name": "Für dich"}]
    with caplog.at_level("WARNING", logger="bridge.sources"):
        await collector(conn, ma).run_once()
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1 and "soundcloud" in warned[0] and "Für dich" in warned[0]
    caplog.clear()
    with caplog.at_level("WARNING", logger="bridge.sources"):
        await collector(conn, ma, rows={"tidal": ("Custom mixes",)}).run_once()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def with_image(t, path, provider=TIDAL, where="metadata"):
    image = {"type": "thumb", "path": path, "provider": provider, "remotely_accessible": True}
    if where == "metadata":
        t["metadata"]["images"] = [{"type": "fanart", "path": "wide.jpg", "provider": provider}, image]
    else:
        t["album"] = {"media_type": "album", "image": image}
    return t


def images(conn, uri):
    return [json.loads(r["image"]) if r["image"] else None for r in conn.execute(
        "SELECT image FROM source_playlist_track WHERE playlist_uri = ? ORDER BY position", (uri,))]


async def test_each_track_keeps_the_thumb_of_its_album_for_the_cover(conn):
    ma = FakeMa()
    setup_rows(ma)
    ma.tracks[(TIDAL, "p1")] = [with_image(tr(TIDAL, "1", "A", isrc="I1"), "https://t/1.jpg"),
                                with_image(tr(TIDAL, "2", "B", isrc="I2"), "https://t/2.jpg", where="album"),
                                tr(TIDAL, "3", "C", isrc="I3")]
    await collector(conn, ma).run_once()
    assert images(conn, f"{TIDAL}://playlist/p1") == [["https://t/1.jpg", TIDAL], ["https://t/2.jpg", TIDAL], None]


async def test_no_discover_rows_at_all_fails_the_pass_so_it_is_tried_again_soon(conn):
    ma = FakeMa()
    setup_rows(ma)
    await collector(conn, ma).run_once()
    ma.rows = []                                   # MA still starting: its providers are not loaded yet
    with pytest.raises(MaError):
        await collector(conn, ma).run_once()
    assert len(stored(conn)) == 2                  # nothing dropped
