"""ChromeDriver against a fake Playwright: the browser context it starts and the row play button it looks for."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapter import driver as driver_mod
from adapter.driver import ChromeDriver


class FakePage:
    url = "about:blank"

    def on(self, event, handler):
        return None


class FakeContext:
    def __init__(self):
        self.pages, self.cookies = [FakePage()], []

    async def add_cookies(self, cookies):
        self.cookies += cookies

    async def route(self, pattern, handler):
        return None


@pytest.fixture
def launched(monkeypatch):
    rec = {}

    class FakeChromium:
        async def launch_persistent_context(self, path, **kw):
            rec["path"], rec["kw"] = path, kw
            rec["ctx"] = FakeContext()
            return rec["ctx"]

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        async def start(self):
            return self

    async def no_load(self, url):
        rec["loaded"] = url

    monkeypatch.setattr(driver_mod, "async_playwright", FakePlaywright)
    monkeypatch.setattr(ChromeDriver, "_load", no_load)
    return rec


async def test_chrome_context_is_german_with_berlin_time(launched, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    await ChromeDriver("cookie", Path("/profile"), lambda why: None).start()
    kw = launched["kw"]
    assert launched["path"] == "/profile"
    assert (kw["channel"], kw["headless"]) == ("chrome", False)
    assert (kw["locale"], kw["timezone_id"]) == ("de-DE", "Europe/Berlin")
    (cookie,) = launched["ctx"].cookies
    assert (cookie["name"], cookie["value"], cookie["domain"]) == ("sp_dc", "cookie", ".spotify.com")


class FakeLocator:
    """Records which elements a locator would match: one string per alternative, in the order of or_()."""

    def __init__(self, alternatives, clicks):
        self.alternatives, self.clicks = alternatives, clicks

    def get_by_test_id(self, test_id):
        return FakeLocator([f"testid={test_id}"], self.clicks)

    def locator(self, selector):
        return FakeLocator([selector], self.clicks)

    def or_(self, other):
        return FakeLocator(self.alternatives + other.alternatives, self.clicks)

    @property
    def first(self):
        return self

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def hover(self):
        return None

    async def wait_for(self, state=None, timeout=None):
        return None

    async def click(self, timeout=None):
        self.clicks.append(self.alternatives)


async def test_chrome_timezone_defaults_to_utc(launched, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    await ChromeDriver("cookie", Path("/profile"), lambda why: None).start()
    assert (launched["kw"]["locale"], launched["kw"]["timezone_id"]) == ("de-DE", "UTC")


async def test_row_play_button_falls_back_to_the_german_label(monkeypatch):
    clicks = []
    drv = ChromeDriver("cookie", Path("/profile"), lambda why: None)

    async def no_load(url):
        return None

    async def find_row(track_id):
        return FakeLocator(["row"], clicks)

    monkeypatch.setattr(drv, "_load", no_load)
    monkeypatch.setattr(drv, "_find_row", find_row)
    drv.page = SimpleNamespace(url="about:blank")
    await drv._play_locked("track1", "37i9dQZEVXcFakeDw00001")
    (alternatives,) = clicks
    assert alternatives[:2] == ["testid=play-button", "button[aria-label$='abspielen']"]


async def test_row_play_button_also_matches_the_english_label(monkeypatch):
    clicks = []
    drv = ChromeDriver("cookie", Path("/profile"), lambda why: None)

    async def no_load(url):
        return None

    async def find_row(track_id):
        return FakeLocator(["row"], clicks)

    monkeypatch.setattr(drv, "_load", no_load)
    monkeypatch.setattr(drv, "_find_row", find_row)
    drv.page = SimpleNamespace(url="about:blank")
    await drv._play_locked("track1", "37i9dQZEVXcFakeDw00001")
    (alternatives,) = clicks
    assert "button[aria-label^='Play ']" in alternatives[2:]
