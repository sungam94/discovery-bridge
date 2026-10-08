"""Spec §5: the long-running capture loop. Events in, taste events out; reconnects with backoff."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta

log = logging.getLogger("bridge.capture")
MAX_BACKOFF_S = 60
FAVORITES_PAGE = 500


class CaptureService:
    def __init__(self, events: Callable[[], AsyncIterator[dict]], ma, tracker, source, recorder, favorites,
                 now: Callable[[], datetime], sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 tick_s: float = 30, retry_every: timedelta = timedelta(minutes=15),
                 on_state: Callable[[bool], None] | None = None) -> None:
        self._events, self._ma, self._tracker, self._source = events, ma, tracker, source
        self._recorder, self._favorites, self._now, self._sleep = recorder, favorites, now, sleep
        self._tick_s, self._retry_every = tick_s, retry_every
        self._last_retry: datetime | None = None
        self._on_state = on_state  # health reporting: is the MA event stream connected?
        self._up = False

    async def _load_favorites(self) -> None:
        uris, offset = [], 0
        while True:
            page = await self._ma.call("music/tracks/library_items", favorite=True, limit=FAVORITES_PAGE, offset=offset)
            uris += [t["uri"] for t in page]
            if len(page) < FAVORITES_PAGE:
                break
            offset += FAVORITES_PAGE
        self._favorites.load(uris)

    async def handle(self, msg: dict) -> None:
        kind, data = msg.get("event"), msg.get("data") or {}
        if kind == "media_item_played":
            closed, started = self._tracker.on_played(data)
            if started is not None:
                src, radio = await self._source.context(started.player_id, started.uri)
                self._tracker.set_source(started.player_id, started.uri, src, radio, started.player_id)
            for c in closed:
                await self._recorder.record_play(c)
        elif kind == "media_item_updated":
            self._favorites.on_item_updated(data)
        elif kind == "tasks_updated":
            self._favorites.on_tasks_updated(msg.get("data"))
        elif kind == "music_sync_completed":
            self._favorites.on_sync_completed()

    async def tick(self) -> None:
        if self._on_state is not None:
            self._on_state(self._up)
        for c in self._tracker.sweep():
            await self._recorder.record_play(c)
        for uri, flagged_at in self._favorites.release_due():
            await self._recorder.record_favorite(uri, flagged_at)
        now = self._now()
        if self._last_retry is None or now - self._last_retry >= self._retry_every:
            self._last_retry = now
            await self._recorder.retry_pending()

    async def _stream_loop(self, max_rounds: int | None = None) -> None:
        backoff, rounds = 1, 0
        while max_rounds is None or rounds < max_rounds:
            rounds += 1
            try:
                stream = self._events()
                await self._load_favorites()
                self._up = True
                async for msg in stream:
                    backoff = 1
                    try:
                        await self.handle(msg)
                    except Exception:
                        log.exception("capture: event %s failed", msg.get("event"))
                self._up = False
                log.warning("capture: event stream ended")
            except Exception as exc:
                self._up = False
                log.warning("capture: event stream error: %s: %s", type(exc).__name__, exc)
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _tick_loop(self) -> None:
        while True:
            await self._sleep(self._tick_s)
            try:
                await self.tick()
            except Exception:
                log.exception("capture: tick failed")

    async def run(self) -> None:
        await asyncio.gather(self._stream_loop(), self._tick_loop())
