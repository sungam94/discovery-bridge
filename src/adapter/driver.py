"""Real Chrome driver for the adapter (Phase 3 spike facts): Playwright, headful under Xvfb, cookie login.
Track, pause and device come from Spotify Connect; progress from the player bar."""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from playwright.async_api import async_playwright

from adapter.runner import LikeStateUnknown
from adapter.state import PlayerState, StateTracker, load_settled, own_device_from_request

log = logging.getLogger("adapter.driver")
TOKEN_URL = "https://open.spotify.com/api/token"
BASE = "https://open.spotify.com"
# pathfinder operations that remove something from the account's library: never let one through
BLOCKED_OPS = re.compile(r"remove", re.I)
PAUSE_CONFIRM_S = 2.0
ADVANCE_CHECK_S = 1.2
SETTLE_S = 4.0
ROW_SCROLL_STEPS = 30
ROW_SCROLL_WAIT_MS = 400
ACTION_BAR_WAIT_MS = 15000
# Tracklist rows carry no play-button test id (live DOM); the row's play button is labelled
# "<title> von <artist> abspielen" in German and "Play <title> by <artist>" in English
ROW_PLAY_LABEL_SUFFIXES = ("abspielen",)
ROW_PLAY_LABEL_PREFIXES = ("Play ",)


def _seconds(text: str | None) -> int | None:
    if not text or ":" not in text:
        return None
    parts = [int(p) for p in text.strip().split(":")]
    return (parts[0] * 60 + parts[1]) if len(parts) == 2 else (parts[0] * 3600 + parts[1] * 60 + parts[2])


def _row_play_button(row):
    """The row's play button: by test id first, then by its label in each known language."""
    button = row.get_by_test_id("play-button")
    for suffix in ROW_PLAY_LABEL_SUFFIXES:
        button = button.or_(row.locator(f"button[aria-label$='{suffix}']"))
    for prefix in ROW_PLAY_LABEL_PREFIXES:
        button = button.or_(row.locator(f"button[aria-label^='{prefix}']"))
    return button.first


class ChromeDriver:
    def __init__(self, sp_dc: str, profile: Path, on_crash: Callable[[str], None]) -> None:
        self._sp_dc, self._profile, self._on_crash = sp_dc, profile, on_crash
        self._pw = self._ctx = self.page = None
        self._tracker = StateTracker()
        self._token: dict | None = None
        self._lock = asyncio.Lock()
        self.own_device_ids: set[str] = set()
        self._logged_keys = False
        self._keep_playing: str | None = None
        self._command_t: float | None = None   # monotonic time of the page's last outgoing Connect command
        self._state_t: float | None = None     # monotonic time of the last accepted state

    # --- lifecycle ---------------------------------------------------------------------------------------------------
    async def start(self) -> None:
        os.environ.setdefault("DISPLAY", ":99")
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            # the locale stays de-DE (nobody sees this browser, and the row play button is found in any language);
            # the timezone follows TZ, which compose sets for every container
            str(self._profile), channel="chrome", headless=False, locale="de-DE",
            timezone_id=os.environ.get("TZ") or "UTC",
            viewport={"width": 1280, "height": 860}, args=["--autoplay-policy=no-user-gesture-required"],
            service_workers="block")  # service-worker requests would bypass the removal guard below
        await self._ctx.add_cookies([{"name": "sp_dc", "value": self._sp_dc, "domain": ".spotify.com",
                                      "path": "/", "secure": True, "httpOnly": True}])
        await self._ctx.route("https://api-partner.spotify.com/pathfinder/**", self._guard)
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.page.on("response", self._on_response)
        self.page.on("request", self._on_request)
        self.page.on("websocket", lambda ws: ws.on("framereceived", self._on_frame))
        self.page.on("crash", lambda _: self._on_crash("page crashed"))
        self.page.on("close", lambda _: self._on_crash("page closed"))
        async with self._lock:
            await self._load(BASE + "/")

    async def close(self) -> None:
        if self._ctx is not None:
            await self._ctx.close()
        if self._pw is not None:
            await self._pw.stop()

    # --- observation -------------------------------------------------------------------------------------------------
    async def _guard(self, route) -> None:
        try:
            op = (route.request.post_data_json or {}).get("operationName", "")
        except Exception:
            op = ""
        if BLOCKED_OPS.search(op or ""):
            log.error("blocked pathfinder operation %s (would remove from the library)", op)
            await route.abort()
            return
        await route.continue_()

    def _on_request(self, req) -> None:
        if "/player/command/" in req.url:
            self._command_t = time.monotonic()
        if "/player/command/" in req.url and log.isEnabledFor(logging.DEBUG):
            try:
                endpoint = ((req.post_data_json or {}).get("command") or {}).get("endpoint")
            except Exception:
                endpoint = "?"
            log.debug("action: connect command out: %s", endpoint)
        own = own_device_from_request(req.method, req.url)
        if own and own not in self.own_device_ids:
            self.own_device_ids.add(own)
            log.info("own Connect device id learned (%d known)", len(self.own_device_ids))

    def _take(self, text: str) -> None:
        accepted = self._tracker.offer(text)
        if accepted:
            self._state_t = time.monotonic()
        if accepted and log.isEnabledFor(logging.DEBUG):
            seq, st = self._tracker.current()
            log.debug("state %s track=%s paused=%s playing=%s active=%s own=%s ts=%s", seq, st.track_id, st.is_paused,
                      st.is_playing, (st.active_device_id or "")[:6], sorted(i[:6] for i in self.own_device_ids), st.ts_ms)
        if accepted and not self._logged_keys:
            self._logged_keys = True  # the smoke test reads this: is a linked-from field ever sent?
            log.info("first player_state seen; linked-from field present=%s", "linked_from_uri" in text)

    async def _on_response(self, resp) -> None:
        try:
            if resp.url.startswith(TOKEN_URL):
                self._token = await resp.json()
            elif "connect-state" in resp.url:
                self._take(await resp.text())
        except Exception:
            pass

    def _on_frame(self, payload) -> None:
        text = payload if isinstance(payload, str) else payload.decode("utf-8", "ignore")
        if '"type":"request"' in text.replace(" ", "") and log.isEnabledFor(logging.DEBUG):
            self._log_command_frame(text)
        self._take(text)

    def _log_command_frame(self, text: str) -> None:
        """A remote Connect command delivered to our device (another device of the account controls it)."""
        try:
            msg = json.loads(text)
            data = (msg.get("payload") or {}).get("compressed")
            inner = json.loads(gzip.decompress(base64.b64decode(data))) if data else (msg.get("payload") or {})
            sender = inner.get("sent_by_device_id") or ""
            log.debug("command in: %s from %s (%s)", (inner.get("command") or {}).get("endpoint"), sender[:6],
                      "own" if sender in self.own_device_ids else "OTHER")
        except Exception as exc:
            log.debug("command in: unreadable (%s)", type(exc).__name__)

    def state(self) -> tuple[int, PlayerState | None]:
        return self._tracker.current()

    async def position_ms(self) -> int | None:
        try:
            text = await self.page.get_by_test_id("playback-position").first.text_content(timeout=3000)
        except Exception:
            return None
        secs = _seconds(text)
        return None if secs is None else secs * 1000

    async def logged_in(self) -> bool:
        async with self._lock:
            if self._token is None or self._token.get("accessTokenExpirationTimestampMs", 0) < time.time() * 1000:
                await self._load(BASE + "/")
        return bool(self._token and self._token.get("accessToken") and not self._token.get("isAnonymous"))

    async def token(self) -> dict | None:
        t = self._token
        if t and not t.get("isAnonymous") and t.get("accessTokenExpirationTimestampMs", 0) > time.time() * 1000 + 60_000:
            return {k: t[k] for k in ("accessToken", "accessTokenExpirationTimestampMs")}
        return None

    async def refresh_token(self) -> None:
        """Between jobs: reload when the token has less than 10 min left. The watchdog pauses what the reload resumes."""
        t = self._token or {}
        if t.get("accessTokenExpirationTimestampMs", 0) - time.time() * 1000 < 600_000:
            async with self._lock:
                await self._load(BASE + "/")

    # --- actions -----------------------------------------------------------------------------------------------------
    async def _load(self, url: str) -> None:
        """Caller holds the lock. A page load resumes the last track (spike), so pause it before returning."""
        log.debug("action: goto %s", url.replace(BASE, ""))
        goto_t = time.monotonic()
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_timeout(4000)
        banner = self.page.locator("#onetrust-reject-all-handler")
        if await banner.count():  # the OneTrust banner covers the player bar; the profile remembers the choice
            try:
                await banner.click(timeout=3000)
            except Exception:
                pass
        deadline = time.monotonic() + SETTLE_S
        while not load_settled(goto_t, self._command_t, self._state_t) and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        if not load_settled(goto_t, self._command_t, self._state_t):
            log.warning("page load did not settle within %.0f s; deciding from the last state", SETTLE_S)
        _, st = self.state()
        own = self.own_device_ids
        if st is not None and not st.is_paused and own and (st.active_device_id is None or st.active_device_id in own) \
                and self._keep_playing not in (st.track_id, st.linked_from_id):
            log.info("page load resumed a track; pausing it")
            await self._pause_locked()

    async def _click_playpause(self) -> None:
        log.debug("action: click playpause")
        await self.page.get_by_test_id("control-button-playpause").click(timeout=10000)

    async def play(self, track_id: str, playlist_id: str | None) -> int:
        """Returns the state sequence number at the click; states before it belong to the page load."""
        async with self._lock:
            try:
                return await self._play_locked(track_id, playlist_id)
            finally:
                self._keep_playing = None

    async def _play_locked(self, track_id: str, playlist_id: str | None) -> int:
        self._keep_playing = track_id  # the fallback load must not pause the job's own track
        if playlist_id:
            await self._load(f"{BASE}/playlist/{playlist_id}")
            seq, st = self.state()
            if self._playing_here(st, track_id):
                return seq - 1  # the load resumed exactly this track; a row click would pause it
            row = await self._find_row(track_id)
            if row is not None:
                await row.scroll_into_view_if_needed(timeout=5000)
                await row.hover()
                button = _row_play_button(row)
                try:
                    await button.wait_for(state="visible", timeout=5000)
                    click_seq = self.state()[0]
                    log.debug("action: click row play")
                    await button.click(timeout=10000)
                    return click_seq
                except Exception as exc:
                    log.warning("row play click failed (%s); falling back to the track page", type(exc).__name__)
        await self._load(f"{BASE}/track/{track_id}")
        click_seq, st = self.state()
        if self._playing_here(st, track_id):
            # already playing here (a row click that reported an error, or resume-on-load): the page's button is
            # "Pause" now, and a click would pause it (spike finding)
            return click_seq - 1
        button = await self._action_button("play-button", f"{BASE}/track/{track_id}")
        click_seq, st = self.state()
        if self._playing_here(st, track_id):
            return click_seq - 1
        log.debug("action: click track-page play")
        await button.click(timeout=15000)
        return click_seq

    async def _find_row(self, track_id: str):
        """The playlist row of the track, or None. Spotify draws only the first rows and adds the rest while the
        list scrolls (live: 26 of 50 rows), so the last drawn row is scrolled into view until the track appears."""
        rows = self.page.get_by_test_id("tracklist-row")
        target = rows.filter(has=self.page.locator(f'a[href*="/track/{track_id}"]')).first
        try:  # the tracklist renders after the load settles (first load after boot: over 10 s, live)
            await rows.first.wait_for(state="attached", timeout=20000)
        except Exception:
            log.warning("no tracklist on the playlist page; falling back to the track page")
            return None
        last_count = -1
        for _ in range(ROW_SCROLL_STEPS):
            if await target.count():
                return target
            count = await rows.count()
            try:
                await rows.last.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                break
            await self.page.wait_for_timeout(ROW_SCROLL_WAIT_MS)
            if count == last_count and await rows.count() == count:
                break  # the list stopped growing: the track is not in this playlist (any more)
            last_count = count
        if await target.count():
            return target
        log.warning("track row not found on the playlist page (%d rows rendered); falling back to the track page",
                    await rows.count())
        return None

    async def _action_button(self, test_id: str, url: str):
        """A button of the track page's action bar; the page is loaded again once when it does not appear
        (live: the play button sometimes never showed within 15 s)."""
        button = self.page.get_by_test_id("action-bar-row").get_by_test_id(test_id).first
        try:
            await button.wait_for(state="visible", timeout=ACTION_BAR_WAIT_MS)
        except Exception:
            log.warning("track page action bar missing; loading the page again")
            await self._load(url)
            await button.wait_for(state="visible", timeout=ACTION_BAR_WAIT_MS)
        return button

    def _playing_here(self, st: PlayerState | None, track_id: str) -> bool:
        return (st is not None and not st.is_paused and track_id in (st.track_id, st.linked_from_id)
                and (st.active_device_id is None or st.active_device_id in self.own_device_ids))

    async def seek_start(self) -> None:
        async with self._lock:
            if self.page.url == "about:blank":
                return
            bar = self.page.get_by_test_id("playback-progressbar")
            box = await bar.bounding_box()
            if box:
                log.debug("action: click progress bar (seek)")
                await self.page.mouse.click(box["x"] + 2, box["y"] + box["height"] / 2)
                await self.page.wait_for_timeout(1000)

    async def _advancing(self) -> bool | None:
        """Local evidence from the player bar: is the position moving? None when it cannot be read."""
        a = await self.position_ms()
        await asyncio.sleep(ADVANCE_CHECK_S)
        b = await self.position_ms()
        return None if a is None or b is None else b > a

    async def pause(self) -> bool:
        async with self._lock:
            return await self._pause_locked()

    async def _pause_locked(self) -> bool:
        """Never touch another device; click once; confirm by a fresh paused state or a stopped player bar;
        leave the page as a last resort so no other track can start."""
        seq, st = self.state()
        own = self.own_device_ids
        if st is not None and own and st.active_device_id is not None and st.active_device_id not in own:
            log.warning("pause skipped: another device of the account is active")
            return False
        if st is None or st.is_paused:  # the state may be stale: trust the player bar
            if not await self._advancing():
                return True
            seq, _ = self.state()
        await self._click_playpause()
        deadline = time.monotonic() + PAUSE_CONFIRM_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            seq2, st2 = self.state()
            if seq2 > seq and st2 is not None and st2.is_paused:
                return True
        if await self._advancing() is False:
            return True
        log.error("pause not confirmed; stopping audio by leaving the page")
        await self.page.goto("about:blank")
        return False

    async def _like_button(self, track_id: str):
        if f"/track/{track_id}" not in (self.page.url or ""):
            await self._load(f"{BASE}/track/{track_id}")
        return await self._action_button("add-button", f"{BASE}/track/{track_id}")

    async def is_liked(self, track_id: str) -> bool:
        async with self._lock:
            btn = await self._like_button(track_id)
            first = await btn.get_attribute("aria-checked", timeout=15000)
            await self.page.wait_for_timeout(1500)
            second = await btn.get_attribute("aria-checked", timeout=15000)
        if first != second or first not in ("true", "false"):
            raise LikeStateUnknown(f"aria-checked {first!r} then {second!r}")
        return first == "true"

    async def like(self, track_id: str) -> None:
        async with self._lock:
            btn = await self._like_button(track_id)
            await btn.click(timeout=15000)
            log.info("like clicked for track %s", track_id)
            await self.page.wait_for_timeout(2000)
