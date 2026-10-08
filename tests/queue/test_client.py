import httpx
import pytest
import respx

from bridge.queue.client import AdapterClient, AdapterError, AdapterRejected

URL = "http://127.0.0.1:8791"


@respx.mock
async def test_calls_and_auth_header():
    route = respx.get(f"{URL}/health").mock(return_value=httpx.Response(200, json={"state": "paused"}))
    respx.post(f"{URL}/jobs").mock(return_value=httpx.Response(202, json={"id": 1, "status": "running"}))
    respx.get(f"{URL}/jobs/1").mock(return_value=httpx.Response(200, json={"id": 1, "status": "finished"}))
    respx.get(f"{URL}/jobs/2").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        c = AdapterClient(URL, "tok", http)
        assert (await c.health())["state"] == "paused"
        assert route.calls[0].request.headers["authorization"] == "Bearer tok"
        assert (await c.submit({"id": 1}))["status"] == "running"
        assert (await c.get_job(1))["status"] == "finished"
        assert await c.get_job(2) is None


@respx.mock
async def test_transport_errors_and_rejections():
    respx.get(f"{URL}/health").mock(side_effect=httpx.ConnectError("refused"))
    respx.post(f"{URL}/jobs").mock(return_value=httpx.Response(409, json={"state": "busy"}))
    respx.post(f"{URL}/pause").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        c = AdapterClient(URL, "tok", http)
        with pytest.raises(AdapterError) as e:
            await c.health()
        assert not isinstance(e.value, AdapterRejected)
        with pytest.raises(AdapterRejected, match="409"):
            await c.submit({"id": 1})
        with pytest.raises(AdapterError):
            await c.pause()
