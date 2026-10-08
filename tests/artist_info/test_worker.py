import json
from datetime import datetime, timedelta, timezone

import pytest

from bridge.artist_info.sources import SourceError
from bridge.artist_info.worker import ArtistInfoWorker, write_artist_file
from bridge.db import connect, migrate
from bridge.genre.store import save_artist
from tests.genre.test_store import playlist

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


class FakeMa:
    """MA after a refresh shows the bridge's latest text for the artist (what the plugin would hand over)."""

    def __init__(self, library):
        self.library, self.refreshed, self.file = library, [], {}

    async def library_artists(self):
        return self.library

    async def refresh_artist(self, item_id):
        self.refreshed.append(item_id)
        name = next(n for n, i in self.library if i == item_id)
        return self.file.get(" ".join(name.casefold().split()), "")


class FakeLastFm:
    def __init__(self, data, broken=()):
        self.data, self.broken, self.asked = data, set(broken), []

    async def artist(self, name):
        self.asked.append(name)
        if name in self.broken:
            raise SourceError("busy")
        return self.data.get(name)


class FakeMb:
    def __init__(self, ids=None, details=None):
        self.ids, self.det = ids or {}, details or {}

    async def artist(self, name):
        return self.ids.get(name, (None, []))

    async def details(self, mbid):
        return self.det[mbid]


class FakeWiki:
    def __init__(self, intros=None):
        self.intros = intros or {}

    async def intro(self, qid):
        return self.intros.get(qid)


def worker(conn, ma, lastfm, mb=None, wiki=None, now=NOW):
    written = []

    def write(texts):
        written.append(texts)
        ma.file = texts
    return ArtistInfoWorker(conn, ma, mb or FakeMb(), lastfm, wiki or FakeWiki(), lambda: now, write), written


LFM = {"bio": "Hazy Fern is a band.", "listeners": 10, "tags": ["shoegaze"], "similar": []}


async def test_library_artists_first_then_playlist_artists_and_the_file_and_refresh(conn):
    playlist(conn, "a", "Spotify · Warm Mix", ["Hazy Fern", "Wren"])
    ma = FakeMa([("Hazy Fern", "11")])
    lf = FakeLastFm({"Hazy Fern": LFM, "Wren": {**LFM, "bio": "Wren is a band."}})
    w, written = worker(conn, ma, lf)
    assert await w.run_once(batch=1) == 1
    assert lf.asked == ["Hazy Fern"] and ma.refreshed == ["11"]
    assert written[-1]["hazy fern"].startswith("Hazy Fern is a band.\n\nLast.fm: 10 listeners · shoegaze")
    assert "In your Spotify mixes: Warm Mix" in written[-1]["hazy fern"]
    await w.run_once(batch=5)
    assert lf.asked == ["Hazy Fern", "Wren"] and ma.refreshed == ["11"]      # Wren is not in the MA library
    assert set(written[-1]) == {"hazy fern", "wren"}


async def test_nothing_changes_nothing_is_written_or_refreshed(conn):
    w, written = worker(conn, FakeMa([("Hazy Fern", "11")]), FakeLastFm({"Hazy Fern": LFM}))
    await w.run_once(batch=5)
    ma_refreshes, writes = len(w._ma.refreshed), len(written)
    await w.run_once(batch=5)
    assert len(w._ma.refreshed) == ma_refreshes and len(written) == writes


async def test_new_local_facts_update_the_text_without_asking_the_sources_again(conn):
    ma, lf = FakeMa([("Hazy Fern", "11")]), FakeLastFm({"Hazy Fern": LFM})
    w, written = worker(conn, ma, lf)
    await w.run_once(batch=5)
    playlist(conn, "b", "Spotify · Sad Mix", ["Hazy Fern"])
    await w.run_once(batch=5)
    assert lf.asked == ["Hazy Fern"] and ma.refreshed == ["11", "11"]
    assert "Sad Mix" in written[-1]["hazy fern"]


async def test_wikipedia_when_lastfm_has_no_bio_and_musicbrainz_facts(conn):
    save_artist(conn, "Hazy Fern", "mb1", [("shoegaze", 3)], NOW)       # MBID known from the genre lookup
    mb = FakeMb(details={"mb1": {"type": "Group", "begin": "2015", "area": "Tempe", "members": ["Alex"],
                                 "wikidata": "Q1"}})
    w, written = worker(conn, FakeMa([("Hazy Fern", "11")]), FakeLastFm({"Hazy Fern": {**LFM, "bio": ""}}), mb,
                        FakeWiki({"Q1": "Hazy Fern is an American band."}))
    await w.run_once(batch=5)
    text = written[-1]["hazy fern"]
    assert text.startswith("Hazy Fern is an American band.\n\nFormed 2015 in Tempe\nMembers: Alex")
    assert text.endswith("Bio: Wikipedia (CC BY-SA)")


async def test_a_failing_source_leaves_the_artist_for_later(conn):
    lf = FakeLastFm({"Hazy Fern": LFM}, broken=["Hazy Fern"])
    w, written = worker(conn, FakeMa([("Hazy Fern", "11")]), lf)
    assert await w.run_once(batch=5) == 0 and written == []
    lf.broken.clear()
    assert await w.run_once(batch=5) == 1


async def test_info_older_than_30_days_is_fetched_again(conn):
    lf = FakeLastFm({"Hazy Fern": LFM})
    w, _ = worker(conn, FakeMa([("Hazy Fern", "11")]), lf)
    await w.run_once(batch=5)
    later, _ = worker(conn, FakeMa([("Hazy Fern", "11")]), lf, now=NOW + timedelta(days=31))
    await later.run_once(batch=5)
    assert lf.asked == ["Hazy Fern", "Hazy Fern"]


async def test_plays_count_the_bridges_known_tracks_of_the_artist(conn):
    playlist(conn, "a", "Spotify · Warm Mix", ["Hazy Fern"])
    tid = conn.execute("SELECT id FROM source_track WHERE artist = 'Hazy Fern'").fetchone()[0]
    conn.execute("UPDATE source_track SET isrc = 'ISRC1' WHERE id = ?", (tid,))
    for i, outcome in enumerate(["completed", "listened", "skipped"]):
        conn.execute("INSERT INTO taste_event(source, ma_item_uri, provider_item_id, isrc, started_at, outcome, "
                     "policy_result) VALUES ('ma_play', 'x', ?, 'ISRC1', ?, ?, 'eligible')", (f"p{i}", f"t{i}", outcome))
    w, written = worker(conn, FakeMa([]), FakeLastFm({"Hazy Fern": LFM}))
    await w.run_once(batch=5)
    assert "played 2 times in Music Assistant" in written[-1]["hazy fern"]


def test_artist_file_is_replaced_atomically(tmp_path):
    write_artist_file(tmp_path, {"hazy fern": "Text"})
    write_artist_file(tmp_path, {"hazy fern": "Text 2"})
    assert json.loads((tmp_path / "artist_info.json").read_text()) == {"version": 1, "artists": {"hazy fern": "Text 2"}}
    assert [p.name for p in tmp_path.iterdir()] == ["artist_info.json"]


async def test_a_failed_ma_refresh_is_tried_again(conn):
    class FlakyMa(FakeMa):
        async def refresh_artist(self, item_id):
            if not self.refreshed:
                self.refreshed.append(item_id)
                raise RuntimeError("MA down")
            return await super().refresh_artist(item_id)
    ma = FlakyMa([("Hazy Fern", "11")])
    w, _ = worker(conn, ma, FakeLastFm({"Hazy Fern": LFM}))
    await w.run_once(batch=5)
    await w.run_once(batch=5)
    await w.run_once(batch=5)
    assert ma.refreshed == ["11", "11"]


async def test_ma_not_taking_the_text_is_retried_a_few_times_then_left(conn):
    class StubbornMa(FakeMa):
        async def refresh_artist(self, item_id):
            self.refreshed.append(item_id)
            return "Booking: x@y"  # MA kept another source's bio (e.g. it found no MusicBrainz id this time)
    ma = StubbornMa([("Hazy Fern", "11")])
    w, _ = worker(conn, ma, FakeLastFm({"Hazy Fern": LFM}))
    for _ in range(6):
        await w.run_once(batch=5)
    assert ma.refreshed == ["11"] * 3


def test_one_listener_is_singular():
    from bridge.artist_info.compose import compose
    assert compose(bio="", bio_source=None, mb=None, lastfm={"listeners": 1}, local={}) == "Last.fm: 1 listener"


async def test_the_full_source_answers_are_kept_apart_from_the_parts(conn):
    raw_lfm = {"name": "Hazy Fern", "tags": {"tag": [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}, {"name": "t4"}]}}
    save_artist(conn, "Hazy Fern", "mb1", [("shoegaze", 3)], NOW)
    mb = FakeMb(details={"mb1": {"type": "Group", "begin": None, "area": None, "members": [], "wikidata": None,
                                 "raw": {"relations": [1, 2]}}})
    w, _ = worker(conn, FakeMa([("Hazy Fern", "11")]), FakeLastFm({"Hazy Fern": {**LFM, "raw": raw_lfm}}), mb)
    await w.run_once(batch=5)
    parts, raw = conn.execute("SELECT parts, raw FROM artist_info").fetchone()
    assert "raw" not in parts and json.loads(raw) == {"lastfm": raw_lfm, "musicbrainz": {"relations": [1, 2]}}


async def test_artists_fetched_before_raw_answers_were_kept_are_fetched_again(conn):
    conn.execute("INSERT INTO artist_info(artist_key, name, parts, fetched_at) VALUES ('hazy fern', 'Hazy Fern', ?, ?)",
                 (json.dumps({"bio": "", "bio_source": None, "mb": None, "lastfm": None}), NOW.isoformat()))
    lf = FakeLastFm({"Hazy Fern": LFM})
    w, _ = worker(conn, FakeMa([("Hazy Fern", "11")]), lf)
    await w.run_once(batch=5)
    assert lf.asked == ["Hazy Fern"]
