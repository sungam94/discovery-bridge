import pytest

from bridge.ma.client import MaError
from bridge.ma.events import stream_events
from tests.ma.fake_server import FakeMa


async def test_stream_yields_events_after_auth():
    async with FakeMa() as fake:
        fake.events_after_auth = [{"event": "media_item_played", "object_id": "x", "data": {"uri": "x"}},
                                  {"event": "queue_time_updated", "object_id": "q", "data": {}}]
        got = []
        async for msg in stream_events(fake.url, "good"):
            got.append(msg["event"])
            if len(got) == 3:
                break
    # FakeMa sends one queue_time_updated noise event before every reply, then the pushed events
    assert got == ["queue_time_updated", "media_item_played", "queue_time_updated"]


async def test_stream_bad_token_raises():
    async with FakeMa() as fake:
        with pytest.raises(MaError):
            async for _ in stream_events(fake.url, "bad"):
                pass
