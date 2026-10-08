import pytest

from bridge.queue.client import AdapterError
from bridge.spotify.fallback import FallbackSource
from bridge.spotify.session import CookieInvalid, Credentials, SessionError

CREDS = Credentials(token="primary", expires_at=1000.0, app_version="1.2", user_agent="UA",
                    hashes={"fetchPlaylist": "h"})


class Primary:
    def __init__(self, results):
        self.results = list(results)

    async def capture(self):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Adapter:
    def __init__(self, fail=False):
        self.fail = fail

    async def token(self):
        if self.fail:
            raise AdapterError("unreachable")
        return {"accessToken": "from-adapter", "accessTokenExpirationTimestampMs": 2_000_000}


class Store:
    def __init__(self, meta=None):
        self.meta = meta

    def load(self):
        return self.meta

    def save(self, meta):
        self.meta = meta


def source(results, adapter, store=None):
    store = store or Store()
    return FallbackSource(Primary(results), adapter, store.load, store.save), store


async def test_primary_success_is_used_and_its_meta_saved():
    src, store = source([CREDS], Adapter())
    assert (await src.capture()).token == "primary"
    assert store.meta == {"app_version": "1.2", "user_agent": "UA", "hashes": {"fetchPlaylist": "h"}}


async def test_falls_back_to_adapter_with_saved_meta():
    src, _ = source([CREDS, SessionError("timeout")], Adapter())
    await src.capture()
    c = await src.capture()
    assert (c.token, c.expires_at, c.hashes, c.app_version) == ("from-adapter", 2000.0, {"fetchPlaylist": "h"}, "1.2")


async def test_fallback_after_restart_uses_stored_meta():
    store = Store({"app_version": "1.2", "user_agent": "UA", "hashes": {"fetchPlaylist": "h"}})
    src, _ = source([CookieInvalid("anonymous")], Adapter(), store)
    assert (await src.capture()).token == "from-adapter"


async def test_both_failing_raises_the_original_error():
    src, _ = source([CREDS, CookieInvalid("anonymous")], Adapter(fail=True))
    await src.capture()
    with pytest.raises(CookieInvalid):
        await src.capture()


async def test_no_meta_or_no_adapter_raises():
    with pytest.raises(SessionError):
        await source([SessionError("x")], Adapter())[0].capture()
    with pytest.raises(SessionError):
        await source([SessionError("x")], None)[0].capture()
