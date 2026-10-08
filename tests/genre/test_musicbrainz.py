import httpx
import pytest

from bridge.genre.musicbrainz import MusicBrainz, MusicBrainzError


def client(handler, sleeps=None):
    async def sleep(s):
        if sleeps is not None:
            sleeps.append(s)
    return MusicBrainz(httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep)


async def test_picks_the_artist_with_the_same_name_and_ranks_its_genres():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/ws/2/artist/":
            return httpx.Response(200, json={"artists": [{"id": "wrong", "name": "Wildmoor Tribute", "score": 100},
                                                         {"id": "right", "name": "WILDMOOR", "score": 95}]})
        assert req.url.path == "/ws/2/artist/right" and req.url.params["inc"] == "genres"
        return httpx.Response(200, json={"genres": [{"name": "experimental", "count": 1},
                                                    {"name": "black metal", "count": 6}]})

    assert await client(handler).artist("Wildmoor") == ("right", [("black metal", 6), ("experimental", 1)])
    assert seen[0].url.params["query"] == 'artist:"Wildmoor"' and "discovery-bridge" in seen[0].headers["user-agent"]


async def test_no_artist_of_that_name_is_an_empty_result():
    def handler(req):
        return httpx.Response(200, json={"artists": [{"id": "x", "name": "Somebody Else", "score": 80}]})

    assert await client(handler).artist("Hillstep") == (None, [])


async def test_waits_between_requests_and_retries_a_busy_server():
    answers = [httpx.Response(503), httpx.Response(200, json={"artists": []})]
    sleeps = []
    assert await client(lambda req: answers.pop(0), sleeps).artist("X") == (None, [])
    assert len(sleeps) == 2 and all(s >= 1.0 for s in sleeps)   # one pause per request: MusicBrainz allows 1 per second


async def test_gives_up_after_repeated_failures():
    with pytest.raises(MusicBrainzError):
        await client(lambda req: httpx.Response(503)).artist("X")
