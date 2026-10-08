import asyncio
import json

import httpx
import pytest
import respx

from bridge.spotify.client import PATHFINDER_URL, AuthExpired, RateLimited, SpotifyClient, SpotifyError
from bridge.spotify.session import CookieInvalid, Credentials, SessionError


class FakeSource:
    def __init__(self, expires_in=900.0, now=1000.0, fail=None, hashes=None, delay=0.0):
        self.calls, self.expires_in, self.now, self.fail = 0, expires_in, now, fail
        self.hashes = hashes or {"fetchPlaylist": "h1", "browsePage": "h2"}
        self.delay = delay

    async def capture(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return Credentials(token=f"tok{self.calls}", expires_at=self.now + self.expires_in,
                           app_version="1.3.5", user_agent="UA", hashes=self.hashes)


def page(fixture, n_items, total):
    resp = fixture("check01_op_fetchPlaylist_dw")["response"]
    content = resp["data"]["playlistV2"]["content"]
    content["items"] = content["items"][:n_items]
    content["totalCount"] = total
    return resp


def client(http, src=None, now=1000.0):
    return SpotifyClient(src or FakeSource(), http, clock=lambda: now)


@respx.mock
async def test_fetch_playlist_pages_by_returned_count(fixture):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.headers["authorization"], request.headers["app-platform"],
                      request.headers["spotify-app-version"], request.headers["user-agent"], body))
        offset = body["variables"]["offset"]
        return httpx.Response(200, json=page(fixture, 25 if offset == 0 else 5, 30))

    respx.post(PATHFINDER_URL).mock(side_effect=handler)
    async with httpx.AsyncClient() as http:
        result = await client(http).fetch_playlist("37i9dQZEVXcFakeDw00001")
    assert [b["variables"]["offset"] for *_, b in calls] == [0, 25]
    assert calls[0][:4] == ("Bearer tok1", "WebPlayer", "1.3.5", "UA")
    assert calls[0][4]["extensions"]["persistedQuery"]["sha256Hash"] == "h1"
    assert calls[0][4]["variables"]["uri"] == "spotify:playlist:37i9dQZEVXcFakeDw00001"
    assert len(result.tracks) == 30 and result.revision_id


@respx.mock
async def test_recaptures_when_token_near_expiry(fixture):
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(200, json=page(fixture, 1, 1)))
    src = FakeSource(expires_in=900)
    now = [1000.0]
    async with httpx.AsyncClient() as http:
        c = SpotifyClient(src, http, clock=lambda: now[0])
        await c.fetch_playlist("p")
        now[0] = 1000.0 + 700  # 200 s left < 300 s margin
        await c.fetch_playlist("p")
    assert src.calls == 2


@respx.mock
async def test_short_lived_fresh_token_is_used_not_recaptured(fixture):
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(200, json=page(fixture, 1, 1)))
    src = FakeSource(expires_in=100)  # already below the margin when captured
    async with httpx.AsyncClient() as http:
        c = client(http, src)
        await c.fetch_playlist("p")
        await c.fetch_playlist("p")
    assert src.calls == 1


@respx.mock
async def test_recapture_on_hash_rotation(fixture):
    route = respx.post(PATHFINDER_URL).mock(side_effect=[
        httpx.Response(404, json={}), httpx.Response(200, json=page(fixture, 1, 1))])
    src = FakeSource()
    async with httpx.AsyncClient() as http:
        result = await client(http, src).fetch_playlist("p")
    assert len(result.tracks) == 1 and src.calls == 2 and route.call_count == 2


@respx.mock
async def test_errors_in_200_body_recapture(fixture):
    respx.post(PATHFINDER_URL).mock(side_effect=[
        httpx.Response(200, json={"errors": [{"message": "PersistedQueryNotFound"}], "data": None}),
        httpx.Response(200, json=page(fixture, 1, 1))])
    src = FakeSource()
    async with httpx.AsyncClient() as http:
        assert len((await client(http, src).fetch_playlist("p")).tracks) == 1
    assert src.calls == 2


@respx.mock
async def test_403_is_retried_after_recapture(fixture):
    respx.post(PATHFINDER_URL).mock(side_effect=[httpx.Response(403), httpx.Response(200, json=page(fixture, 1, 1))])
    async with httpx.AsyncClient() as http:
        assert len((await client(http).fetch_playlist("p")).tracks) == 1


@respx.mock
async def test_raises_after_second_failure():
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(400, json={}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(SpotifyError):
            await client(http).fetch_playlist("p")


@respx.mock
async def test_second_401_is_auth_expired():
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(401, json={}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(AuthExpired):
            await client(http).fetch_playlist("p")


async def test_session_error_is_auth_expired():
    async with httpx.AsyncClient() as http:
        with pytest.raises(AuthExpired, match="cookie"):
            await client(http, FakeSource(fail=CookieInvalid("sp_dc cookie invalid"))).fetch_playlist("p")


async def test_browser_failure_is_not_auth_expired():
    async with httpx.AsyncClient() as http:
        with pytest.raises(SpotifyError) as e:
            await client(http, FakeSource(fail=SessionError("capture failed: TimeoutError"))).fetch_playlist("p")
    assert not isinstance(e.value, AuthExpired)


async def test_other_capture_failure_is_spotify_error():
    async with httpx.AsyncClient() as http:
        with pytest.raises(SpotifyError):
            await client(http, FakeSource(fail=RuntimeError("chromium died"))).fetch_playlist("p")


@respx.mock
async def test_not_found_playlist_raises():
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(
        200, json={"data": {"playlistV2": {"__typename": "NotFound"}}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(SpotifyError, match="not found"):
            await client(http).fetch_playlist("gone")


@respx.mock
@pytest.mark.parametrize("header,expected", [("12", 12.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 30.0), ("", 30.0)])
async def test_429_raises_rate_limited(header, expected):
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(429, headers={"retry-after": header}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RateLimited) as e:
            await client(http).fetch_playlist("p")
    assert e.value.retry_after == expected


@respx.mock
async def test_track_metadata(fixture):
    respx.get("https://spclient.wg.spotify.com/metadata/4/track/15d0f19d9b83a5237def0700459fc001?market=from_token").mock(
        return_value=httpx.Response(200, json=fixture("check01_spclient_track")))
    async with httpx.AsyncClient() as http:
        assert await client(http).track_metadata("0FakeTrack000000000001") == ("QZFAK2600001", 357122)


@respx.mock
async def test_made_for_you(fixture):
    respx.post(PATHFINDER_URL).mock(return_value=httpx.Response(200, json=fixture("check01_op_browsePage")["response"]))
    async with httpx.AsyncClient() as http:
        sections = await client(http).made_for_you()
    assert "spotify:section:0JQ5DACFo5h0jxzOyHOsIo" in sections


@respx.mock
async def test_search_isrc(fixture):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((body["operationName"], body["variables"]["searchTerm"],
                     body["extensions"]["persistedQuery"]["sha256Hash"]))
        return httpx.Response(200, json=fixture("check09_op_searchTracks")["response"])

    respx.post(PATHFINDER_URL).mock(side_effect=handler)
    src = FakeSource(hashes={"fetchPlaylist": "h1", "browsePage": "h2", "searchTracks": "h3"})
    async with httpx.AsyncClient() as http:
        cands = await client(http, src).search_isrc("USA3T2600001")
    assert seen == [("searchTracks", "isrc:USA3T2600001", "h3")]
    assert cands[0].id == "0FakeTrack000000000026"


async def test_search_without_hash_is_an_error():
    async with httpx.AsyncClient() as http:
        with pytest.raises(SpotifyError, match="searchTracks"):
            await client(http).search_isrc("X")


async def test_concurrent_callers_share_one_capture():
    src = FakeSource(delay=0.05)
    async with httpx.AsyncClient() as http:
        c = client(http, src)
        await asyncio.gather(c._credentials(), c._credentials(), c._credentials())
    assert src.calls == 1
