import httpx
import pytest

from analysis.deezer import ClipError, Deezer, DeezerUnavailable

PREVIEW = "https://cdnt-preview.dzcdn.net/api/1/1/a/b/c/0/abc.mp3?hdnea=exp=1~acl=*~hmac=secret"


def client(handler, clock=None):
    slept = []
    times = iter(clock or [])
    d = Deezer(httpx.Client(transport=httpx.MockTransport(handler)), min_interval_s=0.25,
               clock=(lambda: next(times)) if clock else (lambda: 0.0), sleep=slept.append)
    return d, slept


def test_preview_url_by_isrc():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"id": 1, "preview": PREVIEW})

    d, _ = client(handler)
    assert d.preview("GBUM71029604") == PREVIEW
    assert seen == ["https://api.deezer.com/track/isrc:GBUM71029604"]


@pytest.mark.parametrize("body", [
    {"id": 1, "preview": ""},
    {"error": {"type": "DataException", "message": "no data", "code": 800}},
])
def test_no_preview(body):
    d, _ = client(lambda req: httpx.Response(200, json=body))
    assert d.preview("X") is None


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}),
    httpx.Response(200, json={"error": {"type": "Exception", "message": "busy", "code": 700}}),
    httpx.Response(503),
    httpx.Response(429),
    httpx.Response(401),
    httpx.Response(403),
    httpx.Response(404),
    httpx.Response(408),
    httpx.Response(200, text="<html>blocked</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
])
def test_anything_but_a_clean_lookup_answer_is_temporary(response):
    # a blocked IP or a broken answer must not settle every track as an error
    d, _ = client(lambda req: response)
    with pytest.raises(DeezerUnavailable):
        d.preview("X")


def test_network_error_is_temporary():
    def handler(req):
        raise httpx.ConnectError("down")

    d, _ = client(handler)
    with pytest.raises(DeezerUnavailable):
        d.preview("X")


def test_download_writes_the_clip(tmp_path):
    d, _ = client(lambda req: httpx.Response(200, content=b"ID3data"))
    d.download(PREVIEW, tmp_path / "clip.mp3")
    assert (tmp_path / "clip.mp3").read_bytes() == b"ID3data"


def test_download_refused_is_a_clip_error_without_the_signed_url(tmp_path):
    d, _ = client(lambda req: httpx.Response(403))
    with pytest.raises(ClipError) as exc:
        d.download(PREVIEW, tmp_path / "clip.mp3")
    assert "hmac" not in str(exc.value) and "403" in str(exc.value)
    assert not (tmp_path / "clip.mp3").exists()


def test_requests_keep_a_minimum_interval():
    # request 1 at t=10.0, request 2 asked at t=10.1 waits 0.15 s, request 3 at t=11 needs no wait
    d, slept = client(lambda req: httpx.Response(200, json={"preview": ""}), clock=[10.0, 10.1, 10.25, 11.0, 11.0])
    d.preview("A")
    d.preview("B")
    d.preview("C")
    assert slept == [pytest.approx(0.15)]


def test_lookup_keeps_the_track_facts_but_never_the_signed_preview_url():
    body = {"id": 1, "bpm": 140.2, "rank": 32009, "release_date": "2021-01-01", "gain": -8.9, "preview": PREVIEW}
    d, _ = client(lambda req: httpx.Response(200, json=body))
    url, info = d.lookup("X")
    assert url == PREVIEW and info == {"id": 1, "bpm": 140.2, "rank": 32009, "release_date": "2021-01-01", "gain": -8.9}
    assert "hmac" not in str(info)


def test_lookup_of_an_unknown_track_has_no_facts():
    d, _ = client(lambda req: httpx.Response(200, json={"error": {"type": "DataException", "code": 800}}))
    assert d.lookup("X") == (None, None)
