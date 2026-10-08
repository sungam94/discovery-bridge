import httpx

from bridge.artist_info.sources import LastFm, Wikipedia
from bridge.genre.musicbrainz import MusicBrainz


def mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def nosleep(s):
    pass


async def test_lastfm_artist_info():
    def handler(req):
        assert req.url.params["method"] == "artist.getinfo" and req.url.params["api_key"] == "k"
        return httpx.Response(200, json={"artist": {
            "bio": {"content": 'Bio text. <a href="x">Read more on Last.fm</a>'},
            "stats": {"listeners": "135318"},
            "tags": {"tag": [{"name": "shoegaze"}, {"name": "post-metal"}, {"name": "seen live"}, {"name": "usa"}]},
            "similar": {"artist": [{"name": "Wren"}, {"name": "Deafheaven"}]}}})
    info = await LastFm(mock(handler), "k").artist("Hazy Fern")
    raw = info.pop("raw")
    assert info == {"bio": "Bio text.", "listeners": 135318, "tags": ["shoegaze", "post-metal", "usa"],
                    "similar": ["Wren", "Deafheaven"]}
    assert raw["stats"] == {"listeners": "135318"} and len(raw["tags"]["tag"]) == 4      # everything kept


async def test_lastfm_unknown_artist_is_none_and_no_key_skips():
    lf = LastFm(mock(lambda req: httpx.Response(200, json={"error": 6, "message": "not found"})), "k")
    assert await lf.artist("Nobody") is None
    assert await LastFm(mock(lambda req: (_ for _ in ()).throw(AssertionError)), None).artist("x") is None


async def test_musicbrainz_details_with_members_and_wikidata():
    def handler(req):
        assert req.url.params["inc"] == "artist-rels+url-rels"
        return httpx.Response(200, json={
            "type": "Group", "life-span": {"begin": "2015-03"}, "area": {"name": "United States"},
            "begin-area": {"name": "Tempe"},
            "relations": [
                {"type": "member of band", "direction": "backward", "artist": {"name": "Alex"}, "ended": False},
                {"type": "member of band", "direction": "backward", "artist": {"name": "Old"}, "ended": True},
                {"type": "member of band", "direction": "forward", "artist": {"name": "Other Band"}},
                {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q113630317"}}]})
    mb = MusicBrainz(mock(handler), sleep=nosleep)
    details = await mb.details("mbid")
    raw = details.pop("raw")
    assert details == {"type": "Group", "begin": "2015", "area": "Tempe", "members": ["Alex"], "wikidata": "Q113630317"}
    assert len(raw["relations"]) == 4


async def test_wikipedia_intro_via_wikidata():
    def handler(req):
        if req.url.host == "www.wikidata.org":
            return httpx.Response(200, json={"entities": {"Q1": {"sitelinks": {"enwiki": {"title": "Hazy Fern"}}}}})
        assert req.url.params["titles"] == "Hazy Fern"
        return httpx.Response(200, json={"query": {"pages": {"1": {"extract": "Hazy Fern is an American band."}}}})
    assert await Wikipedia(mock(handler)).intro("Q1") == "Hazy Fern is an American band."
    none = Wikipedia(mock(lambda req: httpx.Response(200, json={"entities": {"Q1": {"sitelinks": {}}}})))
    assert await none.intro("Q1") is None


async def test_user_agent_carries_the_contact_when_set():
    agents = []

    def handler(req):
        agents.append(req.headers["user-agent"])
        if req.url.host == "musicbrainz.org":
            return httpx.Response(200, json={"relations": []})
        return httpx.Response(200, json={"entities": {"Q1": {"sitelinks": {}}}})

    await MusicBrainz(mock(handler), sleep=nosleep).details("m")
    await Wikipedia(mock(handler)).intro("Q1")
    await MusicBrainz(mock(handler), sleep=nosleep, contact="someone@example.org").details("m")
    await Wikipedia(mock(handler), contact="someone@example.org").intro("Q1")
    assert agents[0] == agents[1] == "discovery-bridge/1.0 (private Music Assistant bridge)"
    assert agents[2] == agents[3] == "discovery-bridge/1.0 (private Music Assistant bridge; someone@example.org)"
