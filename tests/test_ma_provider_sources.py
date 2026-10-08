import copy
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "spotify_bridge_sources", Path(__file__).parents[1] / "ma_provider" / "spotify_bridge" / "sources.py")
sources = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sources)

URI = "tidal--test0001://playlist/abc"
ENTRY = {"name": "My Mix 1", "source": "tidal", "genres": [["deep house", 9], ["house", 4]],
         "moods": {"words": ["chill"], "instrumental": 0.4}}
LAYOUT = {"version": 1, "cover_style": {"font": 230}, "hues": {"deep house": 50, "house": 30, "jazz": 162},
          "sources": {URI: ENTRY}}
ART = "https://resources.tidal.com/images/aa/bb/640x640.jpg"


@dataclass
class Img:
    type: str
    path: str
    provider: str
    remotely_accessible: bool = False
    proxy_id: str | None = None


def playlist(uri=URI, images=None, media_type="playlist"):
    if images is None:
        images = [Img("thumb", ART, "tidal--test0001", True, "old"), Img("fanart", "f.jpg", "tidal--test0001", True)]
    return SimpleNamespace(name="My Mix 1", uri=uri, media_type=media_type, provider=uri.split("://")[0],
                           metadata=SimpleNamespace(images=list(images), description=None))


def ours(name):
    return Img("thumb", name, "spotify_bridge", False)


def name_for(entry=ENTRY, style=LAYOUT["cover_style"], hues=LAYOUT["hues"], art=ART, uri=URI):
    return sources.cover_name(uri, entry, style, hues, art, "tidal--test0001")


def test_cover_name_is_stable_and_safe():
    name = name_for()
    assert name == name_for() and sources.valid_name(name) and name.endswith(".jpg")
    for bad in ("../x.jpg", "sbcover_zz.jpg", name + "/..", "/etc/passwd", name.replace(".jpg", ".png")):
        assert not sources.valid_name(bad)


def test_cover_name_changes_whenever_the_drawing_would():
    base = name_for()
    assert name_for(entry={**ENTRY, "genres": [["house", 9]]}) != base
    assert name_for(entry={**ENTRY, "moods": None}) != base
    assert name_for(style={"font": 200}) != base
    assert name_for(hues={**LAYOUT["hues"], "house": 31}) != base
    assert name_for(art=ART.replace("aa", "cc")) != base          # TIDAL's daily mixes get new artwork
    assert name_for(hues={**LAYOUT["hues"], "jazz": 1}) == base   # a hue this playlist does not use


def test_cover_name_starts_with_a_key_of_the_playlist():
    a, b = name_for(), name_for(entry={**ENTRY, "genres": []})
    assert sources.uri_key(URI) in a and a.split("_")[1] == b.split("_")[1] == sources.uri_key(URI)
    assert name_for(uri=URI + "x").split("_")[1] != a.split("_")[1]


def test_source_playlists_get_our_thumb_on_copies():
    item = playlist()
    original = copy.deepcopy(item)
    (out,), seen = sources.with_source_covers([item], LAYOUT, ours)
    name = name_for()
    assert out.metadata.images[0] == ours(name) and out.metadata.images[0].proxy_id is None
    assert out.metadata.images[1] == item.metadata.images[1]                 # fanart kept
    assert item.metadata.images == original.metadata.images and item.metadata is not out.metadata
    assert seen == {name: {"uri": URI, "path": ART, "provider": "tidal--test0001", "entry": ENTRY,
                           "style": LAYOUT["cover_style"], "hues": {"deep house": 50, "house": 30}}}


def test_other_items_pass_through_untouched():
    other = playlist(uri="tidal--test0001://playlist/other")
    album = playlist(media_type="album")
    bare = playlist(images=[])
    no_meta = SimpleNamespace(name="x", uri=URI, media_type="playlist", metadata=None)
    folder = SimpleNamespace(name="folder")
    items = [other, album, bare, no_meta, folder]
    out, seen = sources.with_source_covers(items, LAYOUT, ours)
    assert out == items and all(a is b for a, b in zip(out, items)) and seen == {}


def test_no_sources_in_the_layout_changes_nothing():
    item = playlist()
    for layout in (None, {}, {"version": 1}, {"version": 1, "sources": "x"}):
        out, seen = sources.with_source_covers([item], layout, ours)
        assert out[0] is item and seen == {}


def test_an_item_mapping_gets_our_image_too():
    mapping = SimpleNamespace(name="My Mix 1", uri=URI, media_type="playlist",
                              image=Img("thumb", ART, "tidal--test0001", True))
    (out,), seen = sources.with_source_covers([mapping], LAYOUT, ours)
    assert out.image == ours(name_for()) and mapping.image.path == ART and list(seen) == [name_for()]


def test_a_thumb_we_serve_already_is_not_wrapped_again():
    item = playlist(images=[ours(name_for())])
    (out,), seen = sources.with_source_covers([item], LAYOUT, ours)
    assert out is item and seen == {}


def test_store_writes_the_cover_and_drops_older_versions_of_the_same_playlist(tmp_path):
    old, other = name_for(entry={**ENTRY, "genres": []}), name_for(uri=URI + "2")
    (tmp_path / old).write_bytes(b"old")
    (tmp_path / other).write_bytes(b"other")
    new = name_for()
    sources.store(tmp_path, new, b"jpeg")
    assert (tmp_path / new).read_bytes() == b"jpeg"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([new, other])


def test_cached_reads_only_valid_names(tmp_path):
    name = name_for()
    assert sources.cached(tmp_path, name) is None
    (tmp_path / name).write_bytes(b"jpeg")
    assert sources.cached(tmp_path, name) == b"jpeg"
    assert sources.cached(tmp_path, "../" + name) is None


def test_index_remembers_records_across_restarts(tmp_path):
    seen = sources.with_source_covers([playlist()], LAYOUT, ours)[1]
    sources.remember(tmp_path, seen)
    assert sources.recall(tmp_path, name_for()) == seen[name_for()]
    newer = sources.with_source_covers([playlist()], {**LAYOUT, "cover_style": {"font": 1}}, ours)[1]
    sources.remember(tmp_path, newer)
    index = json.loads((tmp_path / sources.INDEX).read_text())
    assert list(index) == list(newer)             # the playlist's older name is forgotten
    assert sources.recall(tmp_path, name_for()) is None and sources.recall(tmp_path, "bad") is None


def test_index_tolerates_a_broken_file(tmp_path):
    (tmp_path / sources.INDEX).write_text("{nope")
    assert sources.recall(tmp_path, name_for()) is None
    sources.remember(tmp_path, {name_for(): {"uri": URI}})
    assert sources.recall(tmp_path, name_for()) == {"uri": URI}


def test_remember_removes_cover_files_not_in_the_index_or_not_drawn_lately(tmp_path):
    import os
    import time
    fresh, old, gone = name_for(), name_for(uri=URI + "2"), name_for(uri=URI + "3")
    sources.remember(tmp_path, {old: {"uri": URI + "2"}})
    for name in (fresh, old, gone):
        (tmp_path / name).write_bytes(b"jpeg")
    (tmp_path / "keep.tmp").write_bytes(b"x")
    stale = time.time() - sources.FILE_MAX_AGE_S - 60
    os.utime(tmp_path / old, (stale, stale))
    sources.remember(tmp_path, {fresh: {"uri": URI}})
    assert (tmp_path / fresh).exists()
    assert not (tmp_path / old).exists() and sources.recall(tmp_path, old) == {"uri": URI + "2"}  # redrawn if asked
    assert not (tmp_path / gone).exists()          # its playlist is no longer in the index
    assert (tmp_path / "keep.tmp").exists() and (tmp_path / sources.INDEX).exists()
