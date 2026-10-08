from bridge.models import SpotifyCandidate
from bridge.spotify.parse import (
    base62_to_gid, daily_mix_ids, daylist_id, parse_made_for_you, parse_playlist_page, parse_search_tracks,
    parse_spclient_track,
)


def test_base62_to_gid(fixture):
    assert base62_to_gid("0FakeTrack000000000001") == fixture("check01_spclient_track")["gid"]


def test_parse_playlist_page(fixture):
    resp = fixture("check01_op_fetchPlaylist_dw")["response"]
    page = parse_playlist_page(resp)
    assert page.found and page.total == 30 and page.raw_count == 25 and len(page.tracks) == 25
    assert page.revision_id == resp["data"]["playlistV2"]["revisionId"]
    first, second = page.tracks[0], page.tracks[1]
    assert (first.id, first.name, first.artists, first.duration_ms, first.explicit) == (
        "0FakeTrack000000000001", "A Low Tide", ("Mire Of Dusk",), 357122, False)
    assert first.album == "A Low Tide" and first.isrc is None
    assert second.artists == ("Glass Rivers", "Copper & Thunder")


def test_parse_playlist_page_skips_non_tracks_but_counts_them():
    resp = {"data": {"playlistV2": {"__typename": "Playlist", "content": {"totalCount": 2, "items": [
        {"itemV2": {"data": {"__typename": "Episode", "uri": "spotify:episode:x"}}},
        {"itemV2": {"data": None}},
    ]}}}}
    page = parse_playlist_page(resp)
    assert (page.tracks, page.total, page.raw_count, page.found) == ([], 2, 2, True)


def test_parse_playlist_page_not_found():
    page = parse_playlist_page({"data": {"playlistV2": {"__typename": "NotFound"}}})
    assert page.found is False and page.raw_count == 0


def test_made_for_you_sections(fixture):
    sections = parse_made_for_you(fixture("check01_op_browsePage")["response"])
    assert daily_mix_ids(sections) == [f"37i9dQZF1EFakeDm00000{i}" for i in range(1, 7)]
    assert daylist_id(sections) == "37i9dQZF1EP6YuccBxUcC1"


def test_daylist_missing():
    assert daylist_id({}) is None


def test_parse_spclient_track(fixture):
    assert parse_spclient_track(fixture("check01_spclient_track")) == ("QZFAK2600001", 357122)
    assert parse_spclient_track({"external_id": [{"type": "upc", "id": "1"}]}) == (None, None)


def test_parse_search_tracks(fixture):
    resp = fixture("check09_op_searchTracks")["response"]
    cands = parse_search_tracks(resp)
    assert cands[0] == SpotifyCandidate(id="0FakeTrack000000000026", name="The Edge of Morning - Live",
                                        duration_ms=2347938, explicit=False, album="Slowly Waking Room")


def test_parse_search_tracks_empty():
    assert parse_search_tracks({"data": {"searchV2": {"tracksV2": {"items": []}}}}) == []
    assert parse_search_tracks({"data": {"searchV2": {}}}) == []


def test_daylist_found_by_id_when_named_after_its_mood():
    sections = {"spotify:section:0JQ5DACFo5h0jxzOyHOsIe": [("x", "On Repeat"),
                                                          ("37i9dQZF1EP6YuccBxUcC1", "slow indie sunday morning")]}
    assert daylist_id(sections) == "37i9dQZF1EP6YuccBxUcC1"


def test_daylist_is_found_by_name_alone(fixture, monkeypatch):
    from bridge.spotify import parse
    sections = parse_made_for_you(fixture("check01_op_browsePage")["response"])
    expected = daylist_id(sections)
    assert expected is not None
    monkeypatch.setattr(parse, "DAYLIST_ID", "37i9dQZF1EFakeNotInHub", raising=False)
    assert daylist_id(sections) == expected


def test_discover_weekly_and_release_radar_are_in_the_hub(fixture):
    sections = parse_made_for_you(fixture("check01_op_browsePage")["response"])
    ids = [pid for items in sections.values() for pid, _ in items]
    assert len([pid for pid in ids if pid.startswith("37i9dQZEVXc")]) == 1
    assert len([pid for pid in ids if pid.startswith("37i9dQZEVXb")]) == 1


def test_fixed_playlist_ids_need_exactly_one_match():
    from bridge.spotify.parse import fixed_playlist_ids
    sections = {"a": [("37i9dQZEVXcFakeDw00001", "Discover Weekly"), ("x", "Other")],
                "b": [("37i9dQZEVXbFakeRr00001", "Release Radar"), ("37i9dQZEVXbFakeRr00002", "Release Radar")]}
    assert fixed_playlist_ids(sections) == {"discover_weekly": "37i9dQZEVXcFakeDw00001"}
    assert fixed_playlist_ids({}) == {}


def test_fixed_playlist_ids_from_the_hub_fixture(fixture):
    from bridge.spotify.parse import fixed_playlist_ids
    sections = parse_made_for_you(fixture("check01_op_browsePage")["response"])
    assert fixed_playlist_ids(sections) == {"discover_weekly": "37i9dQZEVXcFakeDw00001",
                                            "release_radar": "37i9dQZEVXbFakeRr00001"}
