"""Headless Chromium with the sp_dc cookie: capture token, app version and persisted-query hashes."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

TOKEN_URL = "https://open.spotify.com/api/token"
HUB_URL = "https://open.spotify.com/genre/0JQ5DAt0tbjZptfcdMSKl3"
REQUIRED_OPS = ("fetchPlaylist", "browsePage")
OPTIONAL_OPS = ("searchTracks",)
SEARCH_PROBE_URL = "https://open.spotify.com/search/isrc%3AUSA3T2600001/tracks"  # ISRC from spike check 9
SEARCH_WAIT_MS = 15000
# Fallback for the playlist page the capture visits to learn the fetchPlaylist hash (the hash is the same for
# every playlist): Spotify's own public editorial playlist "Today's Top Hits"
PUBLIC_PROBE_PLAYLIST = "37i9dQZF1DXcBWIGoYBM5M"
LOCALES = {"en": "en-US", "de": "de-DE"}
DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/{major}.0.0.0 Safari/537.36")


class SessionError(Exception):
    pass


class CookieInvalid(SessionError):
    """The sp_dc cookie no longer yields a logged-in session; the user must replace it."""


@dataclass(frozen=True)
class Credentials:
    token: str
    expires_at: float  # epoch seconds
    app_version: str
    user_agent: str
    hashes: Mapping[str, str] = field(default_factory=dict)


class HeadlessSession:
    def __init__(self, sp_dc: str, probe_playlist_id: str, *, locale: str, timezone: str,
                 timeout_ms: int = 60000) -> None:
        self._sp_dc = sp_dc
        self._probe = probe_playlist_id
        self._locale, self._timezone = locale, timezone
        self._timeout = timeout_ms

    async def capture(self) -> Credentials:
        try:
            return await self._capture()
        except SessionError:
            raise
        except (PlaywrightError, KeyError, ValueError) as exc:
            raise SessionError(f"capture failed: {type(exc).__name__}: {exc}") from exc

    async def _wait_for(self, page, done, limit_ms: int | None = None) -> None:
        waited, limit = 0, limit_ms or self._timeout
        while not done() and waited < limit:
            await page.wait_for_timeout(500)
            waited += 500

    async def _capture(self) -> Credentials:
        hashes: dict[str, str] = {}
        app_version: dict[str, str] = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ua = DESKTOP_UA.format(major=browser.version.split(".")[0])
                ctx = await browser.new_context(locale=self._locale, timezone_id=self._timezone, user_agent=ua)
                await ctx.add_cookies([{"name": "sp_dc", "value": self._sp_dc, "domain": ".spotify.com",
                                        "path": "/", "secure": True, "httpOnly": True}])
                page = await ctx.new_page()

                def on_request(req):
                    if "pathfinder" not in req.url or req.method != "POST":
                        return
                    if "spotify-app-version" in req.headers:
                        app_version.setdefault("v", req.headers["spotify-app-version"])
                    try:
                        body = req.post_data_json or {}
                    except Exception:
                        return
                    op = body.get("operationName")
                    sha = ((body.get("extensions") or {}).get("persistedQuery") or {}).get("sha256Hash")
                    if op and sha:
                        hashes.setdefault(op, sha)

                page.on("request", on_request)
                async with page.expect_response(lambda r: r.url.startswith(TOKEN_URL), timeout=self._timeout) as info:
                    await page.goto(HUB_URL, wait_until="domcontentloaded", timeout=self._timeout)
                tok = await (await info.value).json()
                await self._wait_for(page, lambda: "browsePage" in hashes)
                await page.goto(f"https://open.spotify.com/playlist/{self._probe}",
                                wait_until="domcontentloaded", timeout=self._timeout)
                await self._wait_for(page, lambda: all(op in hashes for op in REQUIRED_OPS) and "v" in app_version)
                try:
                    await page.goto(SEARCH_PROBE_URL, wait_until="domcontentloaded", timeout=self._timeout)
                    await self._wait_for(page, lambda: "searchTracks" in hashes, SEARCH_WAIT_MS)
                except PlaywrightError:
                    pass  # optional: reverse resolution stays pending until a later capture has the hash
            finally:
                await browser.close()
        if tok.get("isAnonymous") or not tok.get("accessToken"):
            raise CookieInvalid("anonymous or missing token: sp_dc cookie invalid or expired")
        missing = [op for op in REQUIRED_OPS if op not in hashes]
        if missing or "v" not in app_version:
            raise SessionError(f"capture incomplete: missing ops={missing} app_version={'v' in app_version}")
        return Credentials(
            token=tok["accessToken"],
            expires_at=tok["accessTokenExpirationTimestampMs"] / 1000,
            app_version=app_version["v"],
            user_agent=ua,
            hashes=dict(hashes),
        )


def spotify_locale(cfg) -> str:
    """The web player's language (hub section and mix names): spotify_locale if set, else from language."""
    return cfg.spotify_locale or LOCALES[cfg.language]


def probe_playlist_id(cfg, conn) -> str:
    """The configured Discover Weekly, else the one stored by an earlier poll, else a public playlist."""
    if cfg.discover_weekly_id:
        return cfg.discover_weekly_id
    row = conn.execute("SELECT spotify_id FROM playlist WHERE slot = 'discover_weekly'").fetchone()
    return (row["spotify_id"] if row else None) or PUBLIC_PROBE_PLAYLIST


def make_session(cfg, conn) -> HeadlessSession:
    """The session as the service and bridge.cli build it."""
    return HeadlessSession(cfg.sp_dc, probe_playlist_id(cfg, conn), locale=spotify_locale(cfg),
                           timezone=cfg.timezone)
