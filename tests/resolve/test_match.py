from bridge.models import MaCandidate, SpotifyCandidate, SpotifyTrack
from bridge.resolve.match import candidates_from_search, choose, choose_spotify, passes_gate

T = SpotifyTrack(id="s", name="A Low Tide", artists=("Mire Of Dusk",), album="A Low Tide",
                 duration_ms=357122, explicit=False, isrc="QZFAK2600001")


def cand(item_id, dur, album="A Low Tide", explicit=False, compilation=None):
    return MaCandidate(uri=f"tidal--T://track/{item_id}", item_id=str(item_id), isrcs=frozenset({"QZFAK2600001"}),
                       duration_s=dur, explicit=explicit, album=album, compilation=compilation, from_library=False)


def test_gate():
    assert passes_gate(357122, 357)
    assert passes_gate(100000, 105)      # 5 s floor
    assert not passes_gate(100000, 106)
    assert passes_gate(600000, 618)      # 3 % of 600 s = 18 s
    assert not passes_gate(600000, 619)


def test_choose_rejects_all_out_of_gate():
    assert choose(T, [cand(1, 400)]) is None


def test_choose_prefers_close_duration_then_album_then_lowest_id():
    assert choose(T, [cand(30, 360, album="Other"), cand(20, 357, album="Other"), cand(10, 358)]).item_id == "10"
    assert choose(T, [cand(30, 357, album="X"), cand(20, 357, album="Y")]).item_id == "20"
    assert choose(T, [cand(9, 357), cand(100, 357)]).item_id == "9"   # numeric, not lexicographic


def test_filter_that_removes_everything_is_skipped():
    assert choose(T, [cand(5, 357, explicit=True), cand(6, 357, explicit=True)]).item_id == "5"


def test_candidates_from_search(fixture):
    first = candidates_from_search(fixture("check06_search")["tracks"])[0]
    assert first.uri == "tidal--test0001://track/900026000"
    assert first.isrcs == frozenset({"QZFAK2600001"}) and first.duration_s == 357 and first.from_library is False


def test_candidates_from_library_item_with_several_isrcs():
    lib = {"provider": "library", "uri": "library://track/5", "item_id": "5", "duration": 357,
           "external_ids": [["isrc", "XX0000000001"], ["isrc", "qzfak2600001"]], "album": {"name": "A Low Tide"},
           "metadata": {"explicit": False},
           "provider_mappings": [{"provider_domain": "tidal", "provider_instance": "tidal--test0001", "item_id": "900026000"}]}
    (c,) = candidates_from_search([lib])
    assert c.uri == "tidal--test0001://track/900026000" and c.item_id == "900026000" and c.from_library is True
    assert "QZFAK2600001" in c.isrcs


def test_candidates_skip_non_tidal():
    assert candidates_from_search([{"provider": "soundcloud--x", "uri": "soundcloud://track/1",
                                    "provider_mappings": [], "external_ids": []}]) == []


def sc(id_, ms, explicit=False, album="A"):
    return SpotifyCandidate(id=id_, name="n", duration_ms=ms, explicit=explicit, album=album)


def test_choose_spotify_gate_and_filters():
    assert choose_spotify(240, False, "A", [sc("b", 300_000)]) is None              # outside max(5 s, 3 %)
    assert choose_spotify(240, False, "A", [sc("b", 241_000), sc("a", 247_000)]).id == "b"  # within 2 s wins
    assert choose_spotify(240, True, "A", [sc("a", 240_000), sc("b", 240_000, explicit=True)]).id == "b"
    assert choose_spotify(240, None, "Live", [sc("a", 240_000), sc("b", 240_000, album="live")]).id == "b"
    assert choose_spotify(240, False, "X", [sc("b", 240_000), sc("a", 240_000)]).id == "a"  # smallest ID
    assert choose_spotify(240, False, "A", []) is None