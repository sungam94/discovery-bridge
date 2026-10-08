import importlib.util
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "spotify_bridge_rows", Path(__file__).parents[1] / "ma_provider" / "spotify_bridge" / "rows.py")
rows = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rows)


def pl(name):
    return SimpleNamespace(name=name)


LIBRARY = [pl(n) for n in [
    "My Party Mix", "Spotify · Daily Mix 2", "Spotify · DW 2026-09-21", "Spotify · daylist",
    "Spotify · Release Radar", "Spotify · RR 2026-10-02", "Spotify · DW 2026-09-28", "Spotify · Daily Mix 1",
    "Spotify · DW 2026-09-28-2", "Spotify · Discover Weekly", "Spotify · Something Else",
]]


def test_current_rows_in_fixed_order():
    assert [p.name for p in rows.current_rows(LIBRARY)] == [
        "Spotify · Discover Weekly", "Spotify · Release Radar", "Spotify · daylist",
        "Spotify · Daily Mix 1", "Spotify · Daily Mix 2"]


def test_ignores_non_bridge_playlists():
    assert rows.current_rows([pl("Daily Mix 1"), pl("Spotify Discover Weekly")]) == []


LAYOUT = {"version": 1, "rows": [
    {"id": "new_music", "title": "Spotify · Neue Musik", "playlists": ["Spotify · Discover Weekly", "Spotify · Nope"]},
    {"id": "daily_mixes", "title": "Spotify · Daily Mixes", "playlists": ["Spotify · Daily Mix 1"]},
]}


def test_layout_rows_map_names_to_library_playlists():
    out = rows.layout_rows(LAYOUT, LIBRARY)
    assert [(r_id, title, [p.name for p in pls]) for r_id, title, pls in out] == [
        ("new_music", "Spotify · Neue Musik", ["Spotify · Discover Weekly"]),
        ("daily_mixes", "Spotify · Daily Mixes", ["Spotify · Daily Mix 1"]),
    ]


def test_layout_rows_reject_unknown_version():
    assert rows.layout_rows({"version": 2, "rows": []}, LIBRARY) is None
    assert rows.layout_rows(None, LIBRARY) is None


def test_with_subtitles_sets_owner_on_copies():
    p = SimpleNamespace(name="Spotify · Discover Weekly", owner="Music Assistant")
    out = rows.with_subtitles([p], {"Spotify · Discover Weekly": "A, B, C"})
    assert out[0].owner == "A, B, C" and p.owner == "Music Assistant"  # library object untouched
    assert rows.with_subtitles([p], {})[0].owner == "Music Assistant"


def test_layout_rows_keep_subtitles_per_row():
    layout = {"version": 1, "rows": [{"id": "r", "title": "T", "playlists": ["Spotify · Daily Mix 1"],
                                      "subtitles": {"Spotify · Daily Mix 1": "X, Y"}}]}
    (_, _, pls), = rows.layout_rows(layout, LIBRARY)
    assert [p.owner for p in pls] == ["X, Y"]


def item(domain, owner="", description="", uri="x://playlist/1"):
    return SimpleNamespace(name="P", owner=owner, uri=uri, media_type="playlist", provider=domain,
                           metadata=SimpleNamespace(description=description))


def test_top_artists():
    assert rows.top_artists([["B"], ["A", "C"], ["A"], ["C"], ["D"], ["A"], ["B"]]) == "A, B, C"
    assert rows.top_artists([]) is None


def test_soundcloud_subtitle_comes_from_its_description():
    out, todo = rows.enrich([item("soundcloud", description="Depuratus, StormWitch, Setu Ketu, Naturaíz Records")],
                            lambda uri: None)
    assert out[0].owner == "Depuratus, StormWitch, Setu Ketu, Naturaíz Records" and todo == []
    long = item("soundcloud", description="x" * 120)
    assert len(rows.enrich([long], lambda uri: None)[0][0].owner) <= 60


def test_tidal_subtitle_from_cache_or_queued():
    cached = item("tidal--test0001", owner="Created by Tidal", uri="tidal--test0001://playlist/a")
    missing = item("tidal--test0001", owner="Tidal", uri="tidal--test0001://playlist/b")
    out, todo = rows.enrich([cached, missing], {"tidal--test0001://playlist/a": "X, Y, Z"}.get)
    assert [o.owner for o in out] == ["X, Y, Z", "Tidal"] and todo == ["tidal--test0001://playlist/b"]
    assert cached.owner == "Created by Tidal"          # MA's own object is not changed


def test_other_items_are_untouched():
    album = SimpleNamespace(name="A", owner=None, uri="tidal--test0001://album/1", media_type="album", provider="tidal--E")
    lib = item("library", owner="Music Assistant")
    sc_named = item("soundcloud", owner="someone", description="a, b")
    out, todo = rows.enrich([album, lib, sc_named], lambda uri: None)
    assert out == [album, lib, sc_named] and todo == []


def test_discover_cards_carry_no_source_logo():
    from dataclasses import dataclass

    @dataclass(kw_only=True)
    class Mapping:
        item_id: str
        provider_domain: str
        provider_instance: str
        available: bool = True

        def __hash__(self):
            return hash((self.provider_instance, self.item_id))

    original = Mapping(item_id="7", provider_domain="builtin", provider_instance="builtin")
    p = SimpleNamespace(name="Spotify · Discover Weekly", owner="Music Assistant", provider_mappings={original})
    (out,) = rows.without_source_logo([p])
    (m,) = out.provider_mappings
    assert m.provider_domain == rows.NO_LOGO_DOMAIN                     # MA's web UI draws no logo for it
    assert (m.item_id, m.provider_instance, m.available) == ("7", "builtin", True)   # still available and playable
    assert original.provider_domain == "builtin" and p.provider_mappings == {original}   # MA's own object untouched


def test_items_without_provider_mappings_pass_through():
    p = SimpleNamespace(name="x")
    assert rows.without_source_logo([p]) == [p]


def test_artist_page_text_by_artist_name():
    data = {"version": 1, "artists": {"hazy fern": "Hazy Fern is a band."}}
    assert rows.artist_text(data, "Hazy  Fern") == rows.artist_text(data, "HAZY FERN") == "Hazy Fern is a band."
    assert rows.artist_text(data, "Wren") is None
    assert rows.artist_text(None, "Hazy Fern") is None and rows.artist_text({"version": 2}, "Hazy Fern") is None
