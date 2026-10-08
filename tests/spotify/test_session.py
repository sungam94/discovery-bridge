"""HeadlessSession.capture against a fake Playwright: browser context settings and the pages it visits."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from bridge.spotify import session as session_mod
from bridge.spotify.session import HUB_URL, SEARCH_PROBE_URL, HeadlessSession

PROBE = "37i9dQZEVXcFakeDw00001"
OPS_BY_PAGE = {"genre": "browsePage", "playlist": "fetchPlaylist", "search": "searchTracks"}


class FakePage:
    def __init__(self, visits):
        self.visits, self.handlers = visits, {}

    def on(self, event, handler):
        self.handlers[event] = handler

    @asynccontextmanager
    async def expect_response(self, predicate, timeout=None):
        async def value():
            return SimpleNamespace(json=self._token)
        yield SimpleNamespace(value=value())

    async def _token(self):
        return {"accessToken": "tok", "accessTokenExpirationTimestampMs": 2_000_000_000_000, "isAnonymous": False}

    async def goto(self, url, **kw):
        self.visits.append(url)
        kind = url.split("open.spotify.com/", 1)[1].split("/", 1)[0]
        op = OPS_BY_PAGE[kind]
        self.handlers["request"](SimpleNamespace(
            url="https://api-partner.spotify.com/pathfinder/v2/query", method="POST",
            headers={"spotify-app-version": "1.2.3"},
            post_data_json={"operationName": op, "extensions": {"persistedQuery": {"sha256Hash": f"h-{op}"}}}))

    async def wait_for_timeout(self, ms):
        return None


class FakeContext:
    def __init__(self, page):
        self.page, self.cookies = page, []

    async def add_cookies(self, cookies):
        self.cookies += cookies

    async def new_page(self):
        return self.page


class FakeBrowser:
    version = "130.0.1"

    def __init__(self, record):
        self.record = record

    async def new_context(self, **kw):
        self.record["context"] = kw
        self.record["ctx"] = FakeContext(FakePage(self.record["visits"]))
        return self.record["ctx"]

    async def close(self):
        return None


@pytest.fixture
def record(monkeypatch):
    rec = {"visits": []}

    class FakeChromium:
        async def launch(self, **kw):
            rec["launch"] = kw
            return FakeBrowser(rec)

    @asynccontextmanager
    async def fake_async_playwright():
        yield SimpleNamespace(chromium=FakeChromium())

    monkeypatch.setattr(session_mod, "async_playwright", fake_async_playwright)
    return rec


async def test_capture_uses_german_locale_and_berlin_time(record):
    creds = await HeadlessSession("cookie", PROBE, locale="de-DE", timezone="Europe/Berlin").capture()
    assert record["context"]["locale"] == "de-DE"
    assert record["context"]["timezone_id"] == "Europe/Berlin"
    assert record["visits"] == [HUB_URL, f"https://open.spotify.com/playlist/{PROBE}", SEARCH_PROBE_URL]
    assert record["ctx"].cookies[0]["name"] == "sp_dc" and record["ctx"].cookies[0]["domain"] == ".spotify.com"
    assert creds.hashes == {"browsePage": "h-browsePage", "fetchPlaylist": "h-fetchPlaylist",
                            "searchTracks": "h-searchTracks"}


def _cfg(**kw):
    base = {"sp_dc": "cookie", "discover_weekly_id": None, "language": "en", "spotify_locale": None,
            "timezone": "UTC"}
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def conn(tmp_path):
    from bridge.db import connect, migrate
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def test_probe_falls_back_to_a_public_playlist(conn):
    from bridge.publish.publisher import ensure_playlist_row
    from bridge.spotify.session import PUBLIC_PROBE_PLAYLIST, probe_playlist_id
    assert probe_playlist_id(_cfg(), conn) == PUBLIC_PROBE_PLAYLIST
    ensure_playlist_row(conn, "discover_weekly", "fixed", "37i9dQZEVXcFakeDw00001")
    assert probe_playlist_id(_cfg(), conn) == "37i9dQZEVXcFakeDw00001"
    assert probe_playlist_id(_cfg(discover_weekly_id="37i9dQZEVXcFakeDw00002"), conn) == "37i9dQZEVXcFakeDw00002"


def test_locale_follows_language():
    from bridge.spotify.session import spotify_locale
    assert spotify_locale(_cfg(language="en")) == "en-US"
    assert spotify_locale(_cfg(language="de")) == "de-DE"
    assert spotify_locale(_cfg(language="en", spotify_locale="fr-FR")) == "fr-FR"


async def test_made_session_uses_the_configured_locale_and_timezone(record, conn):
    from bridge.spotify.session import PUBLIC_PROBE_PLAYLIST, make_session
    await make_session(_cfg(language="en", timezone="America/New_York"), conn).capture()
    assert record["context"]["locale"] == "en-US"
    assert record["context"]["timezone_id"] == "America/New_York"
    assert record["visits"][1] == f"https://open.spotify.com/playlist/{PUBLIC_PROBE_PLAYLIST}"
