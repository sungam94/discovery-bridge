import asyncio

import pytest

from adapter.runner import JobRunner, LikeStateUnknown
from adapter.state import PlayerState

T = "0FakeTrack000000000001"
PREVIOUS = PlayerState(track_id="previous", linked_from_id=None, is_paused=True, is_playing=False, ts_ms=0,
                       duration_ms=200_000, context_uri=None, is_ad=False, active_device_id="me")


class Clock:
    def __init__(self):
        self.ms = 0

    async def sleep(self, s):
        self.ms += int(s * 1000)
        await asyncio.sleep(0)   # let other tasks (a pause request) run, as a real sleep would


class FakeDriver:
    """A scripted web player. Timeline entries (at_ms, kind, value) are relative to play():
    kind "ad" (True/False), "track" (id), "pause" (True), "device" (active device id)."""

    def __init__(self, clock, track=T, linked=None, duration=240_000, timeline=(), start_pos=0, stale_calls=0,
                 active="me", liked=False, like_works=True, like_unknown=False, pause_works=True,
                 own="me", resumed_before_click=0):
        self.clock, self.track, self.linked, self.duration = clock, track, linked, duration
        self.timeline, self.start_pos, self.stale_calls = sorted(timeline), start_pos, stale_calls
        self.active, self.own_device_ids = active, ({own} if own else set())
        self.resumed_before_click, self.click_seq = resumed_before_click, None
        self.liked, self.like_works, self.like_unknown, self.pause_works = liked, like_works, like_unknown, pause_works
        self.started_at = self.paused_at = None
        self.calls, self.pauses, self.plays, self.seeks, self.likes, self.offset = 0, 0, [], 0, 0, 0

    def _now(self):
        return (self.paused_at if self.paused_at is not None else self.clock.ms) - self.started_at

    def _view(self, elapsed):
        track, ad, paused, active, ad_since, ad_total = self.track, False, False, self.active, None, 0
        for at, kind, value in self.timeline:
            if at > elapsed:
                break
            if kind == "ad":
                if value and not ad:
                    ad_since = at
                if not value and ad:
                    ad_total += at - ad_since
                ad = value
            elif kind == "track":
                track = value
            elif kind == "pause":
                paused = True
            elif kind == "device":
                active = value
        if ad:
            ad_total += elapsed - ad_since
        return track, ad, paused, active, ad_total

    def state(self):
        if self.started_at is None:
            return 1, PlayerState(**{**PREVIOUS.__dict__, "active_device_id": self.active,
                                     "is_paused": self.active == "me"})
        self.calls += 1
        if self.calls <= self.stale_calls:
            return 1, PREVIOUS
        if self.calls <= self.resumed_before_click:   # a state received during the page load, before the click
            return self.click_seq, PlayerState(**{**PREVIOUS.__dict__, "is_paused": False, "is_playing": True})
        track, ad, paused, active, _ = self._view(self._now())
        return 1 + self.calls, PlayerState(
            track_id=None if ad else track, linked_from_id=None if ad else self.linked,
            is_paused=paused or self.paused_at is not None, is_playing=True, ts_ms=self.clock.ms,
            duration_ms=self.duration, context_uri=None, is_ad=ad, active_device_id=active)

    async def position_ms(self):
        if self.started_at is None:
            return None
        elapsed = self._now()
        return self.start_pos + elapsed - self._view(elapsed)[4] + self.offset

    async def play(self, track_id, playlist_id):
        self.plays.append((track_id, playlist_id))
        self.started_at = self.clock.ms
        self.click_seq = 1 + self.resumed_before_click   # states seen while the page loaded, before the click
        return self.click_seq

    async def seek_start(self):
        self.seeks += 1
        self.offset = -(await self.position_ms() - self.offset)

    async def pause(self):
        self.pauses += 1
        if self.pause_works and self.started_at is not None:
            self.paused_at = self.clock.ms
        return self.pause_works

    async def is_liked(self, track_id):
        if self.like_unknown:
            raise LikeStateUnknown("aria-checked missing")
        return self.liked

    async def like(self, track_id):
        self.likes += 1
        self.liked = self.like_works


def runner(driver, clock, ad_limit_s=600.0):
    return JobRunner(driver, clock.sleep, poll_s=1.0, start_timeout_s=8.0, ad_limit_s=ad_limit_s)


def play_job(target=130_000, playlist="37i9dQZEVXcFakeDw00001"):
    return {"id": 1, "kind": "play", "spotify_id": T, "target_ms": target, "source_spotify_playlist_id": playlist}


async def test_listened_job_stops_at_target():
    clock = Clock()
    d = FakeDriver(clock)
    r = await runner(d, clock).run(play_job(130_000))
    assert (r["outcome"], r["verified"], r["pause_confirmed"], d.pauses) == ("done", True, True, 1)
    assert 130_000 <= r["progress_ms"] < 132_000 and r["observed_track"] == T
    assert d.plays == [(T, "37i9dQZEVXcFakeDw00001")]


async def test_completed_job_stops_five_seconds_before_end():
    clock = Clock()
    d = FakeDriver(clock, duration=240_000)
    r = await runner(d, clock).run(play_job(400_000))
    assert r["outcome"] == "done" and 235_000 <= r["progress_ms"] < 236_000 and d.pauses == 1


async def test_stale_state_from_previous_job_is_ignored():
    clock = Clock()
    d = FakeDriver(clock, stale_calls=2)
    assert (await runner(d, clock).run(play_job(60_000)))["outcome"] == "done"


async def test_wrong_track_at_start_fails():
    clock = Clock()
    d = FakeDriver(clock, track="other")
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["reason"], r["observed_track"], d.pauses) == ("failed", "wrong_track", "other", 1)


async def test_relinked_track_matches():
    clock = Clock()
    d = FakeDriver(clock, track="relinked", linked=T)
    assert (await runner(d, clock).run(play_job(60_000)))["outcome"] == "done"


async def test_track_change_pauses_and_fails():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(60_000, "track", "autoplayed")])
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["reason"], r["verified"], d.pauses) == ("failed", "wrong_track", True, 1)


async def test_not_started_times_out():
    clock = Clock()

    class Dead(FakeDriver):
        def state(self):
            return 1, PREVIOUS

    d = Dead(clock)
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["reason"], d.pauses) == ("failed", "not_started", 1)


async def test_ads_are_not_counted():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(2_000, "ad", True), (32_000, "ad", False)])
    r = await runner(d, clock).run(play_job(60_000))
    assert r["outcome"] == "done" and 60_000 <= r["progress_ms"] < 62_000
    assert clock.ms >= 90_000                    # 60 s of track plus 30 s of ads


async def test_ads_before_start_over_limit_fail():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(0, "ad", True)])
    r = await runner(d, clock, ad_limit_s=60).run(play_job())
    assert (r["outcome"], r["reason"]) == ("failed", "ads")


async def test_ads_during_play_over_limit_fail():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(5_000, "ad", True)])
    r = await runner(d, clock, ad_limit_s=60).run(play_job(900_000))
    assert (r["outcome"], r["reason"]) == ("failed", "ads")


async def test_resumed_mid_track_counts_from_where_it_resumed_without_seeking():
    clock = Clock()
    d = FakeDriver(clock, start_pos=120_000)
    r = await runner(d, clock).run(play_job(60_000))
    # live smoke: a seek click during playback paused it; only the park seek after a confirmed stop remains
    assert d.seeks == 1 and r["outcome"] == "done" and 60_000 <= r["progress_ms"] < 62_000


async def test_other_device_playing_is_not_touched():
    clock = Clock()
    d = FakeDriver(clock, active="phone")
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["interrupt"], d.plays, d.pauses) == ("interrupted", "elsewhere", [], 0)


async def test_takeover_by_other_device_is_not_paused():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(45_000, "device", "phone")])
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["interrupt"], r["verified"], d.pauses) == ("interrupted", "takeover", True, 0)


async def test_external_pause_is_takeover():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(20_000, "pause", True)])
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["interrupt"], r["verified"]) == ("interrupted", "takeover", False)


async def test_pause_request_interrupts():
    clock = Clock()
    d = FakeDriver(clock)
    jr = runner(d, clock)

    async def request_later():
        while clock.ms < 10_000:
            await asyncio.sleep(0)
        jr.pause_requested.set()

    asyncio.get_running_loop().create_task(request_later())
    r = await jr.run(play_job())
    assert (r["outcome"], r["interrupt"], r["verified"], d.pauses) == ("interrupted", "pause", False, 1)


async def test_pause_requested_before_start_never_plays():
    clock = Clock()
    d = FakeDriver(clock)
    jr = runner(d, clock)
    jr.pause_requested.set()
    r = await jr.run(play_job())
    assert (r["outcome"], r["interrupt"], d.plays) == ("interrupted", "pause", [])


async def test_missing_duration_fails():
    clock = Clock()
    d = FakeDriver(clock, duration=None)
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["reason"], d.pauses) == ("failed", "no_duration", 1)


async def test_unconfirmed_pause_is_still_done():
    clock = Clock()
    d = FakeDriver(clock, pause_works=False)
    r = await runner(d, clock).run(play_job(40_000))
    assert (r["outcome"], r["pause_confirmed"]) == ("done", False)


async def test_save_paths():
    clock = Clock()
    job = {"id": 2, "kind": "save", "spotify_id": T, "target_ms": None, "source_spotify_playlist_id": None}
    already = FakeDriver(clock, liked=True)
    assert (await runner(already, clock).run(job))["outcome"] == "done" and already.likes == 0
    fresh = FakeDriver(clock)
    assert (await runner(fresh, clock).run(job))["outcome"] == "done" and fresh.likes == 1
    broken = FakeDriver(clock, like_works=False)
    r = await runner(broken, clock).run(job)
    assert (r["outcome"], r["reason"], broken.likes) == ("failed", "like_not_confirmed", 1)
    unknown = FakeDriver(clock, like_unknown=True)
    r = await runner(unknown, clock).run(job)
    assert (r["outcome"], r["reason"], unknown.likes) == ("failed", "like_state_unknown", 0)


async def test_stopped_track_is_parked_at_its_start():
    clock = Clock()
    d = FakeDriver(clock)
    r = await runner(d, clock).run(play_job(60_000))
    assert r["outcome"] == "done" and d.seeks == 1     # a later page load resumes 0:00, not the last 5 s


async def test_unknown_own_device_never_takes_over_another_playing_device():
    clock = Clock()
    d = FakeDriver(clock, active="phone", own=None)
    r = await runner(d, clock).run(play_job())
    assert (r["outcome"], r["interrupt"], d.plays, d.pauses) == ("interrupted", "elsewhere", [], 0)


async def test_previous_track_resumed_during_page_load_is_not_wrong_track():
    clock = Clock()
    d = FakeDriver(clock, resumed_before_click=2)
    assert (await runner(d, clock).run(play_job(60_000)))["outcome"] == "done"


async def test_a_one_poll_device_flicker_is_not_a_takeover():
    clock = Clock()
    d = FakeDriver(clock, timeline=[(45_000, "device", "fresh-session-id"), (46_000, "device", "me")])
    assert (await runner(d, clock).run(play_job(60_000)))["outcome"] == "done"