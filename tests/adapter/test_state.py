import json

from adapter.state import PlayerState, StateTracker, parse_player_state

T = "0FakeTrack000000000001"


def ps(uri=f"spotify:track:{T}", paused=False, ts="1791100000000", dur="357159", **track_extra):
    return {"track": {"uri": uri, **track_extra}, "is_paused": paused, "is_playing": True,
            "position_as_of_timestamp": "30115", "timestamp": ts, "duration": dur,
            "context_uri": "spotify:playlist:37i9dQZEVXcFakeDw00001"}


def test_parse_connect_state_response():
    st = parse_player_state(json.dumps({"active_device_id": "dev1", "player_state": ps()}))
    assert st == PlayerState(track_id=T, linked_from_id=None, is_paused=False, is_playing=True, ts_ms=1791100000000,
                             duration_ms=357159, context_uri="spotify:playlist:37i9dQZEVXcFakeDw00001", is_ad=False,
                             active_device_id="dev1")


def test_parse_nested_dealer_frame_and_linked_from():
    frame = {"payloads": [{"cluster": {"active_device_id": "d2",
                                       "player_state": ps(paused=True, metadata={"linked_from_uri": "spotify:track:OLD"})}}]}
    st = parse_player_state(json.dumps(frame))
    assert (st.is_paused, st.linked_from_id, st.active_device_id) == (True, "OLD", "d2")


def test_ad_and_garbage():
    ad = parse_player_state(json.dumps({"player_state": ps(uri="spotify:ad:abc")}))
    assert ad.is_ad is True and ad.track_id is None
    assert parse_player_state("not json") is None
    assert parse_player_state(json.dumps({"no": "state"})) is None
    assert parse_player_state(json.dumps({"player_state": {"is_paused": True}})) is None   # no track


def test_tracker_keeps_order_and_counts():
    t = StateTracker()
    assert t.current() == (0, None)
    assert t.offer(json.dumps({"player_state": ps(ts="2000")}))
    assert not t.offer(json.dumps({"player_state": ps(ts="1000", paused=True)}))   # older: ignored
    assert t.offer(json.dumps({"player_state": ps(ts="2000", paused=True)}))       # same time: accepted
    seq, st = t.current()
    assert seq == 2 and st.is_paused is True
    assert not t.offer("garbage")


def test_own_device_comes_only_from_our_state_reports():
    from adapter.state import own_device_from_request
    base = "https://gew4-spclient.spotify.com/track-playback/v1/devices/"
    assert own_device_from_request("PUT", base + "aae36d1bef0123456789/state") == "aae36d1bef0123456789"
    assert own_device_from_request("DELETE", base + "36f86106de0123456789") is None          # an old session
    assert own_device_from_request("POST", base) is None
    assert own_device_from_request("DELETE", "https://x/connect-state/v1/devices/hobs_36f86106de01234") is None


def test_own_device_from_our_transfer_requests():
    from adapter.state import own_device_from_request
    url = "https://gew4-spclient.spotify.com/connect-state/v1/connect/transfer/from/6d1befebd2aa00/to/aae36d1bef0123456789"
    assert own_device_from_request("POST", url) == "aae36d1bef0123456789"

def test_load_settles_when_page_commands_have_a_later_state():
    from adapter.state import load_settled
    assert load_settled(goto_t=10.0, last_command_t=None, last_state_t=None)        # no command at all
    assert load_settled(goto_t=10.0, last_command_t=9.0, last_state_t=None)         # command from before the load
    assert not load_settled(goto_t=10.0, last_command_t=14.5, last_state_t=14.0)    # resume-on-load still in flight
    assert not load_settled(goto_t=10.0, last_command_t=14.5, last_state_t=None)
    assert load_settled(goto_t=10.0, last_command_t=14.5, last_state_t=15.2)        # its state arrived
