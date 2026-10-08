import json
import logging
from datetime import datetime, timezone

import pytest

from analysis.deezer import ClipError, DeezerUnavailable
from analysis.model import AnalysisError, Sound
from analysis.service import Service
from analysis.store import open_store
from tests.analysis.helpers import bridge_db, playlist

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
URL = "https://cdn.example/clip.mp3?hmac=secret"


class FakeDeezer:
    def __init__(self, previews, fail=None):
        self.previews, self.fail, self.asked = previews, fail or {}, []

    def lookup(self, isrc):
        self.asked.append(isrc)
        if isrc in self.fail:
            raise self.fail[isrc]
        url = self.previews.get(isrc)
        return url, ({"id": 1, "bpm": 140.0, "rank": 5000} if isrc in self.previews else None)

    def download(self, url, path):
        path.write_bytes(b"mp3")


class FakeAnalyser:
    def __init__(self, broken=False):
        self.broken, self.seen = broken, []

    def analyse(self, path):
        assert path.read_bytes() == b"mp3"
        self.seen.append(path)
        if self.broken:
            raise AnalysisError("no frames")
        return Sound({"dark": 0.7, "happy": 0.1}, 0.8, [0.5] * 4)


class Stop(Exception):
    pass


@pytest.fixture
def dbs(tmp_path):
    return bridge_db(tmp_path / "bridge.sqlite"), open_store(tmp_path / "sound.sqlite")


def service(dbs, tmp_path, deezer, analyser=None):
    bridge, sound = dbs
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Service(bridge, sound, deezer, analyser or FakeAnalyser(), work, now=lambda: NOW)


def rows(sound):
    return {r[0]: r[1:] for r in sound.execute("SELECT isrc, status, error FROM track_sound ORDER BY isrc")}


def test_a_track_is_analysed_stored_and_its_clip_deleted(dbs, tmp_path):
    bridge, sound = dbs
    playlist(bridge, "a", "Spotify · A", ["I1"])
    analyser = FakeAnalyser()
    s = service(dbs, tmp_path, FakeDeezer({"I1": URL}), analyser)
    assert s.step() is True
    moods, instrumental = sound.execute("SELECT moods, instrumental FROM track_sound WHERE isrc = 'I1'").fetchone()
    assert json.loads(moods) == {"dark": 0.7, "happy": 0.1} and instrumental == 0.8
    assert rows(sound) == {"I1": ("done", None)}
    assert len(analyser.seen) == 1 and not analyser.seen[0].exists()
    assert list((tmp_path / "work").iterdir()) == []
    assert s.step() is False


def test_no_preview_is_stored_without_analysis(dbs, tmp_path):
    bridge, sound = dbs
    playlist(bridge, "a", "Spotify · A", ["I1"])
    analyser = FakeAnalyser()
    service(dbs, tmp_path, FakeDeezer({}), analyser).step()
    assert rows(sound) == {"I1": ("no_preview", None)} and analyser.seen == []


def test_a_failed_analysis_is_stored_as_error_and_the_clip_deleted(dbs, tmp_path):
    bridge, sound = dbs
    playlist(bridge, "a", "Spotify · A", ["I1", "I2"])
    s = service(dbs, tmp_path, FakeDeezer({"I1": URL, "I2": URL}, fail={"I2": ClipError("clip HTTP 403")}),
                FakeAnalyser(broken=True))
    s.step()
    s.step()
    assert rows(sound) == {"I1": ("error", "AnalysisError: no frames"), "I2": ("error", "ClipError: clip HTTP 403")}
    assert list((tmp_path / "work").iterdir()) == []


def test_deezer_unavailable_stores_nothing_until_it_happened_five_times(dbs, tmp_path):
    bridge, sound = dbs
    playlist(bridge, "a", "Spotify · A", ["I1"])
    s = service(dbs, tmp_path, FakeDeezer({}, fail={"I1": DeezerUnavailable("HTTP 503")}))
    for _ in range(4):
        with pytest.raises(DeezerUnavailable):
            s.step()
    assert rows(sound) == {}
    s.step()
    assert rows(sound) == {"I1": ("error", "DeezerUnavailable: HTTP 503")}


def test_ten_errors_in_a_row_stop_storing_errors_until_a_track_succeeds(dbs, tmp_path):
    bridge, sound = dbs
    isrcs = [f"E{n:02}" for n in range(12)]
    playlist(bridge, "a", "Spotify · A", isrcs)
    deezer = FakeDeezer({i: URL for i in isrcs}, fail={i: ClipError("clip HTTP 403") for i in isrcs})
    s = service(dbs, tmp_path, deezer)
    for _ in range(9):
        s.step()
    assert len(rows(sound)) == 9
    with pytest.raises(DeezerUnavailable, match="10 errors in a row"):
        s.step()
    assert len(rows(sound)) == 9                                   # the tenth is not stored
    deezer.fail.clear()
    assert s.step() is True
    assert rows(sound)["E09"] == ("done", None)
    deezer.fail["E10"] = ClipError("clip HTTP 403")
    s.step()                                                       # one error after a success is stored
    assert rows(sound)["E10"] == ("error", "ClipError: clip HTTP 403")


def test_a_track_given_up_after_the_error_streak_keeps_its_own_error(dbs, tmp_path):
    bridge, sound = dbs
    isrcs = [f"E{n:02}" for n in range(12)]
    playlist(bridge, "a", "Spotify · A", isrcs)
    s = service(dbs, tmp_path, FakeDeezer({}, fail={i: ClipError("clip HTTP 403") for i in isrcs}))
    for _ in range(9):
        s.step()
    for _ in range(4):
        with pytest.raises(DeezerUnavailable):
            s.step()
    s.step()
    assert rows(sound)["E09"] == ("error", "ClipError: clip HTTP 403")
    with pytest.raises(DeezerUnavailable):                         # the streak goes on for the next track
        s.step()


def test_run_sleeps_when_idle_and_backs_off_when_deezer_is_unavailable(dbs, tmp_path):
    bridge, _ = dbs
    playlist(bridge, "a", "Spotify · A", ["I1", "I2"])
    deezer = FakeDeezer({}, fail={"I1": DeezerUnavailable("HTTP 503")})
    s = service(dbs, tmp_path, deezer)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 1:
            deezer.fail.clear()
        if len(slept) == 2:
            raise Stop

    with pytest.raises(Stop):
        s.run(sleep=sleep)
    assert slept == [60, 600] and deezer.asked == ["I1", "I1", "I2"]


def test_back_off_grows_while_deezer_stays_unavailable(dbs, tmp_path):
    bridge, _ = dbs
    playlist(bridge, "a", "Spotify · A", ["I1", "I2"])
    s = service(dbs, tmp_path, FakeDeezer({}, fail={"I1": DeezerUnavailable("x"), "I2": DeezerUnavailable("x")}))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 7:
            raise Stop

    with pytest.raises(Stop):
        s.run(sleep=sleep)
    assert slept == [60, 120, 240, 480, 1800, 1800, 1800]  # no wait after giving up on I1


def test_one_log_line_per_track_and_progress_every_25(dbs, tmp_path, caplog):
    bridge, _ = dbs
    isrcs = [f"I{n:02}" for n in range(30)]
    playlist(bridge, "a", "Spotify · A", isrcs)
    s = service(dbs, tmp_path, FakeDeezer({i: URL for i in isrcs}))
    caplog.set_level(logging.INFO, logger="analysis")
    while s.step():
        pass
    lines = [r.getMessage() for r in caplog.records]
    assert [m.split()[0] for m in lines if " done " in m] == isrcs
    progress = [m for m in lines if m.startswith("progress")]
    assert progress == ["progress: 25 tracks analysed since start, 5 pending"]
    assert not any("hmac" in m or "cdn.example" in m for m in lines)


def test_deezer_track_facts_are_kept_with_the_analysis(dbs, tmp_path):
    bridge, sound = dbs
    playlist(bridge, "a", "Spotify · A", ["I1", "I2"])
    s = service(dbs, tmp_path, FakeDeezer({"I1": URL, "I2": None}))
    s.step()
    s.step()
    facts = dict(sound.execute("SELECT isrc, deezer FROM track_sound"))
    assert json.loads(facts["I1"]) == {"id": 1, "bpm": 140.0, "rank": 5000}
    assert json.loads(facts["I2"]) == {"id": 1, "bpm": 140.0, "rank": 5000}   # no clip, but the facts exist


def test_tracks_analysed_before_the_facts_were_kept_get_them_when_idle(dbs, tmp_path):
    bridge, sound = dbs
    playlist(bridge, "a", "Spotify · A", ["I1"])
    sound.execute("INSERT INTO track_sound(isrc, status, analysed_at) VALUES ('I1', 'done', '2026-10-05T00:00:00+00:00')")
    deezer = FakeDeezer({"I1": URL})
    s = service(dbs, tmp_path, deezer)
    assert s.step() is False                       # nothing to analyse
    assert s.backfill_step() is True and s.backfill_step() is False
    assert json.loads(sound.execute("SELECT deezer FROM track_sound").fetchone()[0])["bpm"] == 140.0
    assert deezer.asked == ["I1"]
