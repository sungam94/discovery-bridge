"""The plugin's glue in __init__.py, run against small stand-ins for the Music Assistant modules it imports."""
import asyncio
import importlib.util
import io
import json
import logging
import sys
import types
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

PKG = Path(__file__).parents[1] / "ma_provider" / "spotify_bridge"
URI = "tidal--test0001://playlist/abc"
ART = "https://resources.tidal.com/images/aa/640x640.jpg"
ENTRY = {"name": "My Mix 1", "source": "tidal", "genres": [["deep house", 9]], "moods": None}


def _stub_ma(monkeypatch, fetch):
    class ImageType(StrEnum):
        THUMB = "thumb"
        FANART = "fanart"

    class ProviderFeature(StrEnum):
        RECOMMENDATIONS = "recommendations"
        ARTIST_METADATA = "artist_metadata"

    @dataclass
    class MediaItemImage:
        type: ImageType
        path: str
        provider: str
        remotely_accessible: bool = False
        proxy_id: str | None = None

    class UniqueList(list):
        pass

    @dataclass
    class MediaItemMetadata:
        description: str | None = None
        description_language: str | None = None
        images: UniqueList = field(default_factory=UniqueList)

    class MetadataProvider:
        def __init__(self, mass, manifest, config, features):
            self.mass, self.instance_id, self.logger = mass, "spotify_bridge", logging.getLogger("sb-test")

    mods = {
        "music_assistant_models": types.ModuleType("music_assistant_models"),
        "music_assistant_models.enums": types.SimpleNamespace(ImageType=ImageType, ProviderFeature=ProviderFeature),
        "music_assistant_models.media_items": types.SimpleNamespace(
            MediaItemImage=MediaItemImage, MediaItemMetadata=MediaItemMetadata, UniqueList=UniqueList,
            RecommendationFolder=SimpleNamespace),
        "music_assistant": types.ModuleType("music_assistant"),
        "music_assistant.helpers": types.ModuleType("music_assistant.helpers"),
        "music_assistant.helpers.images": types.SimpleNamespace(get_image_data=fetch),
        "music_assistant.models": types.ModuleType("music_assistant.models"),
        "music_assistant.models.metadata_provider": types.SimpleNamespace(MetadataProvider=MetadataProvider),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("sb_plugin", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "sb_plugin", plugin)
    spec.loader.exec_module(plugin)
    return plugin, SimpleNamespace(MediaItemImage=MediaItemImage, ImageType=ImageType, UniqueList=UniqueList)


def jpeg(colour=(200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buf, "JPEG")
    return buf.getvalue()


class Mass:
    def __init__(self, storage, row):
        self.storage_path = str(storage)
        self.tasks = []
        self.handler = SimpleNamespace(target=row)
        self.command_handlers = {"music/recommendations/items": self.handler}
        self.covers = SimpleNamespace(_generate_and_write=None, config=None)

    def create_task(self, coro):
        self.tasks.append(asyncio.ensure_future(coro))

    def get_provider(self, _name):
        return self.covers


@pytest.fixture
def env(tmp_path, monkeypatch):
    fetched = []

    async def fetch(mass, path, provider):
        fetched.append((path, provider))
        if path == "broken":
            raise FileNotFoundError(path)
        if path == "slow":
            await asyncio.sleep(5)
        return jpeg()

    plugin, ma = _stub_ma(monkeypatch, fetch)
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps({"version": 1, "rows": [], "hues": {"deep house": 50}, "cover_style": None,
                                  "sources": {URI: ENTRY}}))
    monkeypatch.setattr(plugin, "LAYOUT_FILE", layout)
    tidal = SimpleNamespace(name="My Mix 1", uri=URI, media_type="playlist", provider="tidal--test0001", owner="Tidal",
                            metadata=SimpleNamespace(description=None, images=ma.UniqueList(
                                [ma.MediaItemImage(ma.ImageType.THUMB, ART, "tidal--test0001", True)])))

    async def row(**kwargs):
        return ma.UniqueList([tidal])

    return SimpleNamespace(plugin=plugin, ma=ma, tidal=tidal, row=row, fetched=fetched, storage=tmp_path / "data")


async def started(env):
    mass = Mass(env.storage, env.row)
    prov = env.plugin.SpotifyBridgeProvider(mass, None, None, set())
    await prov.handle_async_init()
    return prov, mass


async def settle(mass):
    while pending := [t for t in mass.tasks if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


def test_tidal_cards_get_a_thumb_our_provider_serves(env):
    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()
        await settle(mass)
        thumb = card.metadata.images[0]
        assert thumb.provider == "spotify_bridge" and not thumb.remotely_accessible
        assert thumb.type == env.ma.ImageType.THUMB and thumb.path.startswith("sbcover_")
        assert env.tidal.metadata.images[0].path == ART           # MA's own object untouched
        data = await prov.resolve_image(thumb.path)
        assert Image.open(io.BytesIO(data)).size == (1500, 1500)
        assert env.fetched == [(ART, "tidal--test0001")]
        assert (env.storage / "spotify_bridge_covers" / thumb.path).read_bytes() == data
        assert await prov.resolve_image(thumb.path) == data and len(env.fetched) == 1   # from the cover directory
        return thumb.path

    asyncio.run(run())


def test_after_a_restart_the_cover_is_found_through_the_index(env):
    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()
        await settle(mass)
        name = card.metadata.images[0].path
        fresh, _ = await started(env)
        data = await fresh.resolve_image(name)
        assert Image.open(io.BytesIO(data)).size == (1500, 1500)

    asyncio.run(run())


def test_unknown_or_unsafe_names_are_not_found(env):
    async def run():
        prov, _ = await started(env)
        for name in ("../../etc/passwd", "sbcover_" + "0" * 16 + "_" + "1" * 16 + ".jpg"):
            with pytest.raises(FileNotFoundError):
                await prov.resolve_image(name)

    asyncio.run(run())


def test_a_drawing_error_raises_so_ma_retries_later_instead_of_keeping_the_plain_artwork(env, monkeypatch):
    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()

        def boom(*args, **kwargs):
            raise RuntimeError("bad font")

        monkeypatch.setattr(env.plugin, "draw_source", boom)
        name = card.metadata.images[0].path
        with pytest.raises(RuntimeError):
            await prov.resolve_image(name)
        await settle(mass)
        assert not (env.storage / "spotify_bridge_covers" / name).exists()

    asyncio.run(run())


def test_a_failed_fetch_of_the_artwork_raises_so_ma_retries_later(env):
    async def run():
        prov, mass = await started(env)
        env.tidal.metadata.images[0].path = "broken"
        (card,) = await mass.handler.target()
        with pytest.raises(FileNotFoundError):
            await prov.resolve_image(card.metadata.images[0].path)
        await settle(mass)

    asyncio.run(run())


def test_the_row_survives_a_broken_layout_and_keeps_subtitles(env):
    async def run():
        _, mass = await started(env)
        env.plugin.LAYOUT_FILE.write_text("{broken")
        (card,) = await mass.handler.target()
        assert card.metadata.images[0].path == ART and card.owner == "Tidal"
        await settle(mass)

    asyncio.run(run())


def test_rows_listed_together_all_reach_the_index(env):
    other = "soundcloud--Ab1://playlist/your-mix-1"
    layout = json.loads(env.plugin.LAYOUT_FILE.read_text())
    layout["sources"][other] = {**ENTRY, "source": "soundcloud"}
    env.plugin.LAYOUT_FILE.write_text(json.dumps(layout))
    images = env.ma.UniqueList([env.ma.MediaItemImage(env.ma.ImageType.THUMB, ART + "?sc", "soundcloud--Ab1", True)])
    sc = SimpleNamespace(**{**vars(env.tidal), "uri": other, "provider": "soundcloud--Ab1", "owner": "x",
                            "metadata": SimpleNamespace(description=None, images=images)})

    async def row(which):
        return env.ma.UniqueList([env.tidal if which == "tidal" else sc])

    env.row = row

    async def run():
        _, mass = await started(env)
        rows = await asyncio.gather(mass.handler.target(which="tidal"), mass.handler.target(which="sc"))
        await settle(mass)
        fresh, _ = await started(env)
        for (card,) in rows:
            data = await fresh.resolve_image(card.metadata.images[0].path)
            assert Image.open(io.BytesIO(data)).size == (1500, 1500)

    asyncio.run(run())


def test_unload_restores_the_row_command(env):
    async def run():
        prov, mass = await started(env)
        await prov.unload()
        assert mass.handler.target is env.row
        await settle(mass)

    asyncio.run(run())


def test_a_cover_file_removed_as_stale_is_drawn_again_when_asked(env):
    import os

    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()
        await settle(mass)
        name = card.metadata.images[0].path
        await prov.resolve_image(name)
        path = env.storage / "spotify_bridge_covers" / name
        sources = sys.modules["sb_plugin.sources"]
        stale = path.stat().st_mtime - sources.FILE_MAX_AGE_S - 60
        os.utime(path, (stale, stale))
        sources.remember(path.parent, {})
        assert not path.exists()
        fresh, _ = await started(env)
        data = await fresh.resolve_image(name)
        assert Image.open(io.BytesIO(data)).size == (1500, 1500) and path.exists()

    asyncio.run(run())


def test_at_most_two_covers_are_drawn_at_once(env, monkeypatch):
    import threading
    import time
    lock, now, most = threading.Lock(), [0], [0]

    def slow(*args, **kwargs):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        time.sleep(0.05)
        with lock:
            now[0] -= 1
        return jpeg()

    async def run():
        prov, _ = await started(env)
        monkeypatch.setattr(env.plugin, "draw_source", slow)
        names = [f"sbcover_{i:016x}_{i:016x}.jpg" for i in range(6)]
        for i, name in enumerate(names):
            prov._covers[name] = {"uri": f"{URI}{i}", "path": ART, "provider": "tidal--test0001", "entry": ENTRY}
        await asyncio.gather(*(prov.resolve_image(n) for n in names))

    asyncio.run(run())
    assert most[0] == 2


def test_a_tidal_cover_is_drawn_on_the_album_covers_of_its_tracks(env, monkeypatch):
    tiles = [["https://t/1.jpg", "tidal--test0001"], ["https://t/2.jpg", "tidal--test0001"], ["https://t/3.jpg", "tidal--test0001"]]
    with_tiles(env, tiles)
    seen = {}

    def capture(*args, **kwargs):
        seen["tiles"] = args[7] if len(args) > 7 else kwargs.get("tiles")
        seen["data"] = args[0]
        return jpeg()

    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()
        monkeypatch.setattr(env.plugin, "draw_source", capture)
        await prov.resolve_image(card.metadata.images[0].path)
        assert sorted(env.fetched) == sorted(tuple(t) for t in tiles)   # not the playlist's own artwork
        assert len(seen["tiles"]) == 3 and seen["data"] is None
        assert all(Image.open(io.BytesIO(t)).size == (64, 64) for t in seen["tiles"])

    asyncio.run(run())


def test_a_tidal_cover_without_tiles_is_drawn_on_its_own_artwork(env, monkeypatch):
    seen = {}

    def capture(*args, **kwargs):
        seen["tiles"] = args[7] if len(args) > 7 else kwargs.get("tiles")
        return jpeg()

    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()
        monkeypatch.setattr(env.plugin, "draw_source", capture)
        await prov.resolve_image(card.metadata.images[0].path)
        assert seen["tiles"] == [] and env.fetched == [(ART, "tidal--test0001")]

    asyncio.run(run())



def with_tiles(env, tiles):
    layout = json.loads(env.plugin.LAYOUT_FILE.read_text())
    layout["sources"][URI] = {**ENTRY, "tiles": tiles}
    env.plugin.LAYOUT_FILE.write_text(json.dumps(layout))


@pytest.mark.parametrize("bad", ["broken", "slow"])
def test_a_tile_that_fails_or_hangs_fails_the_cover_so_ma_asks_again_later(env, monkeypatch, bad):
    with_tiles(env, [["https://t/1.jpg", "tidal--test0001"], [bad, "tidal--test0001"]])
    monkeypatch.setattr(env.plugin, "TILE_TIMEOUT_S", 0.05)

    async def run():
        prov, mass = await started(env)
        (card,) = await mass.handler.target()
        await settle(mass)
        name = card.metadata.images[0].path
        with pytest.raises(Exception):
            await prov.resolve_image(name)
        assert not (env.storage / "spotify_bridge_covers" / name).exists()

    asyncio.run(run())


def test_a_playlist_whose_generated_cover_was_redrawn_gets_its_current_cover(env, tmp_path):
    """MA's Recently played keeps the cover file of the moment it was played; a redraw deletes that file."""
    current = tmp_path / "113_2_thumb.jpg"
    current.write_bytes(jpeg())
    gone = str(tmp_path / "113_1_thumb.jpg")
    old = env.ma.MediaItemImage(env.ma.ImageType.THUMB, gone, "playlist_metadata", False)
    new = env.ma.MediaItemImage(env.ma.ImageType.THUMB, str(current), "playlist_metadata", False)
    mapping = SimpleNamespace(name="Spotify · Daily Mix 1", uri="library://playlist/113", media_type="playlist",
                              item_id="113", provider="library", image=old)
    full = SimpleNamespace(name="Spotify · Daily Mix 5", uri="library://playlist/117", media_type="playlist",
                           item_id="117", provider="library",
                           metadata=SimpleNamespace(images=env.ma.UniqueList([old])))
    fine = SimpleNamespace(name="Spotify · On Repeat", uri="library://playlist/121", media_type="playlist",
                           item_id="121", provider="library", image=new)

    async def row(**kwargs):
        return env.ma.UniqueList([mapping, full, fine])

    async def library_item(item_id):
        return SimpleNamespace(metadata=SimpleNamespace(images=[new]))

    async def run():
        mass = Mass(env.storage, row)
        mass.music = SimpleNamespace(playlists=SimpleNamespace(get_library_item=library_item))
        prov = env.plugin.SpotifyBridgeProvider(mass, None, None, set())
        await prov.handle_async_init()
        a, b, c = await mass.handler.target()
        assert a.image.path == str(current) and b.metadata.images[0].path == str(current)
        assert c is fine                                       # a cover that exists is left alone
        assert mapping.image.path == gone                      # MA's own objects untouched

    asyncio.run(run())


def test_fallback_row_title_is_english(monkeypatch):
    """Shown only until the bridge has written layout.json; afterwards the titles come from the layout file."""
    async def fetch(*a, **kw):
        return b""
    plugin, _ = _stub_ma(monkeypatch, fetch)
    assert plugin.ROWS == {"spotify_current": ("Spotify · Made for you", "mdi-spotify")}


def test_manifest_names_no_owner():
    manifest = json.loads((PKG / "manifest.json").read_text())
    assert manifest["codeowners"] == []
    assert "für" not in manifest["description"].lower()
