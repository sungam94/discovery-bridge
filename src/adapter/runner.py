"""Spec §6.3 (amended by the Phase 3 spike and plan): one play or save job. Track, pause and device come from
fresh Spotify Connect states; progress comes from the player bar."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from adapter.state import PlayerState

log = logging.getLogger("adapter.runner")
END_MARGIN_MS = 5000      # never let a track end: the Free web player always continues to another track
VERIFY_MIN_MS = 30_000


class LikeStateUnknown(Exception):
    pass


class Driver(Protocol):
    own_device_ids: set[str]  # every Connect device ID this browser session registered (one per page load)

    def state(self) -> tuple[int, PlayerState | None]: ...
    async def position_ms(self) -> int | None: ...
    async def play(self, track_id: str, playlist_id: str | None) -> int | None: ...  # state sequence at the click
    async def seek_start(self) -> None: ...
    async def pause(self) -> bool: ...
    async def is_liked(self, track_id: str) -> bool: ...
    async def like(self, track_id: str) -> None: ...


def _result(outcome, *, verified=False, reason=None, interrupt=None, progress=0, observed=None,
            pause_confirmed=False) -> dict:
    return {"outcome": outcome, "verified": verified, "reason": reason, "interrupt": interrupt,
            "progress_ms": progress, "observed_track": observed, "pause_confirmed": pause_confirmed}


def _matches(st: PlayerState, track: str) -> bool:
    return st.track_id == track or st.linked_from_id == track


class JobRunner:
    def __init__(self, driver: Driver, sleep: Callable[[float], Awaitable[None]], poll_s: float = 1.0,
                 start_timeout_s: float = 8.0, ad_limit_s: float = 600.0) -> None:
        self._d, self._sleep = driver, sleep
        self._poll, self._start_timeout, self._ad_limit_ms = poll_s, start_timeout_s, ad_limit_s * 1000
        self.pause_requested = asyncio.Event()

    def _own(self, st: PlayerState) -> bool:
        ids = self._d.own_device_ids
        return not ids or st.active_device_id is None or st.active_device_id in ids

    def _foreign(self, st: PlayerState) -> bool:
        """Before starting: a playing device that is not provably ours (unknown own IDs count as foreign)."""
        return st.active_device_id is not None and st.active_device_id not in self._d.own_device_ids

    async def run(self, job: dict) -> dict:
        if job["kind"] == "save":
            return await self._save(job["spotify_id"])
        return await self._play(job)

    async def _save(self, track: str) -> dict:
        try:
            if await self._d.is_liked(track):
                return _result("done", observed=track)
            await self._d.like(track)
            liked = await self._d.is_liked(track)
        except LikeStateUnknown:
            return _result("failed", reason="like_state_unknown", observed=track)
        return _result("done" if liked else "failed", reason=None if liked else "like_not_confirmed", observed=track)

    async def _stop(self, outcome: str, **kw) -> dict:
        confirmed = await self._d.pause()
        if confirmed:  # park at 0:00: a later page load resumes the track, which must not be 5 s from its end
            await self._d.seek_start()
        return _result(outcome, pause_confirmed=confirmed, **kw)

    async def _play(self, job: dict) -> dict:
        track, target = job["spotify_id"], job.get("target_ms") or 0
        if self.pause_requested.is_set():
            return _result("interrupted", interrupt="pause")
        seq0, st0 = self._d.state()
        if st0 is not None and not st0.is_paused and self._foreign(st0):
            return _result("interrupted", interrupt="elsewhere", observed=st0.track_id)
        click_seq = await self._d.play(track, job.get("source_spotify_playlist_id"))
        if isinstance(click_seq, int):  # states received while the page loaded (a resumed track) are not fresh
            seq0 = max(seq0, click_seq)
        waited, ad_ms = 0.0, 0
        while True:  # start: a fresh state must show the job's track playing
            if self.pause_requested.is_set():
                return await self._stop("interrupted", interrupt="pause")
            seq, st = self._d.state()
            fresh = st is not None and seq > seq0
            if fresh and st.is_ad:
                ad_ms += int(self._poll * 1000)
                if ad_ms > self._ad_limit_ms:
                    return await self._stop("failed", reason="ads")
            elif fresh and st.track_id is not None and not _matches(st, track):
                return await self._stop("failed", reason="wrong_track", observed=st.track_id)
            elif fresh and _matches(st, track) and not st.is_paused:
                break
            else:
                waited += self._poll
                if waited >= self._start_timeout:
                    return await self._stop("failed", reason="not_started", observed=st.track_id if st else None)
            await self._sleep(self._poll)
        if not st.duration_ms:
            return await self._stop("failed", reason="no_duration", observed=track)
        pos0 = await self._d.position_ms() or 0  # never seek while playing: a seek click paused playback (live smoke)
        log.debug("start: track playing, pos0=%s duration=%s context=%s", pos0, st.duration_ms, st.context_uri)
        stop_at = min(pos0 + target, st.duration_ms - END_MARGIN_MS)
        progress, foreign_polls = 0, 0
        while True:
            if self.pause_requested.is_set():
                return await self._stop("interrupted", interrupt="pause", verified=progress >= VERIFY_MIN_MS,
                                        progress=progress, observed=track)
            _, st = self._d.state()
            if st is not None and st.is_ad:
                ad_ms += int(self._poll * 1000)
                if ad_ms > self._ad_limit_ms:
                    return await self._stop("failed", reason="ads", progress=progress, observed=track)
                await self._sleep(self._poll)
                continue
            if st is not None and not self._own(st):
                foreign_polls += 1   # a new page registers a new device ID; give our own ID a poll to arrive
                if foreign_polls >= 2:
                    log.warning("takeover: another device is active (progress %s ms)", progress)
                    return _result("interrupted", interrupt="takeover", verified=progress >= VERIFY_MIN_MS,
                                   progress=progress, observed=track)
                await self._sleep(self._poll)
                continue
            foreign_polls = 0
            if st is not None and not _matches(st, track):
                return await self._stop("failed", reason="wrong_track", verified=progress >= VERIFY_MIN_MS,
                                        progress=progress, observed=st.track_id)
            if st is not None and st.is_paused:
                log.warning("takeover: paused from outside (progress %s ms, state ts %s)", progress, st.ts_ms)
                return _result("interrupted", interrupt="takeover", verified=progress >= VERIFY_MIN_MS,
                               progress=progress, observed=track)
            pos = await self._d.position_ms()
            log.debug("poll: pos=%s pos0=%s stop_at=%s seq_state_paused=%s", pos, pos0, stop_at, st.is_paused if st else None)
            if pos is not None:
                progress = max(progress, pos - pos0)
                if pos >= stop_at:
                    return await self._stop("done", verified=progress >= VERIFY_MIN_MS, progress=progress,
                                            observed=track)
            remaining = (stop_at - pos) / 1000 if pos is not None else self._poll
            await self._sleep(min(self._poll, max(0.05, remaining)))
