"""Spotify Bridge: Discover rows for the playlists that discovery-bridge publishes into the MA library.

Reads only MA's own library (playlists named "Spotify · …"); no connection to the bridge itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, ProviderFeature
from music_assistant_models.media_items import MediaItemImage, MediaItemMetadata, RecommendationFolder, UniqueList

from music_assistant.helpers.images import get_image_data
from music_assistant.models.metadata_provider import MetadataProvider

from .frames import draw_source, restyle_file
from .rows import (artist_text, current_rows, enrich, layout_rows, stale_generated, top_artists, with_thumb,
                   without_source_logo)
from .sources import cached, recall, remember, store, valid_name, with_source_covers

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES = {ProviderFeature.RECOMMENDATIONS, ProviderFeature.ARTIST_METADATA}
# Fallback row when the bridge has not written state/layout.json yet. The dated history playlists
# ("Spotify · DW 2026-10-04") get no row on purpose; they stay reachable under Playlists.
ROWS = {"spotify_current": ("Spotify · Made for you", "mdi-spotify")}
LAYOUT_FILE = Path(__file__).parent / "state" / "layout.json"
ARTIST_FILE = Path(__file__).parent / "state" / "artist_info.json"  # artist page texts, written by the bridge
ITEMS_COMMAND = "music/recommendations/items"   # wrapped to add artist subtitles to TIDAL/SoundCloud rows
ARTIST_CACHE_S = 24 * 3600
ARTIST_TRACKS = 50
COVER_PROVIDER = "playlist_metadata"   # MA's built-in cover generator; its thumb gets the type stack
COVER_HOOK_TRIES, COVER_HOOK_WAIT_S = 24, 5
COVER_PREFIX = "Spotify · "            # only the bridge's playlists get the type stack and the larger tiles
GRID_TEMPLATE = "album_grid"           # larger tiles are cut from this template's regular grid only
COVER_DIR = "spotify_bridge_covers"    # under MA's storage path; the plugin's own state directory is read-only
COVERS_KEPT = 1000                     # names remembered in memory for resolve_image
DRAWS_AT_ONCE = 2                      # a Discover page asks for all its covers together; each draw is heavy
TILE_TIMEOUT_S = 10                    # per album cover fetched for a TIDAL cover


_artist_cache: tuple[float, dict | None] = (-1.0, None)


def _read_artists() -> dict | None:
    """artist_info.json, re-read only when the bridge has replaced it."""
    global _artist_cache
    try:
        mtime = ARTIST_FILE.stat().st_mtime
        if mtime != _artist_cache[0]:
            _artist_cache = (mtime, json.loads(ARTIST_FILE.read_text(encoding="utf-8")))
        return _artist_cache[1]
    except (OSError, ValueError):
        return None


def _read_layout() -> dict | None:
    try:
        return json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def setup(mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig) -> SpotifyBridgeProvider:
    """Initialize provider(instance) with given configuration."""
    return SpotifyBridgeProvider(mass, manifest, config, SUPPORTED_FEATURES)


class SpotifyBridgeProvider(MetadataProvider):
    """Recommendation rows for Spotify-origin playlists; artist subtitles for TIDAL/SoundCloud playlist cards."""

    async def handle_async_init(self) -> None:
        """Wrap MA's row-items command. Any error returns MA's original result; unload restores the original."""
        self._artists: dict[str, tuple[float, str]] = {}
        self._pending: set[str] = set()
        self._limit = asyncio.Semaphore(2)
        self._hooked = None
        self._cover_hook = None
        self._covers: dict[str, dict] = {}   # cover name -> the playlist's own artwork and layout entry
        self._cover_dir = Path(self.mass.storage_path) / COVER_DIR
        self._index_lock = asyncio.Lock()
        self._draw_limit = asyncio.Semaphore(DRAWS_AT_ONCE)
        try:
            await asyncio.to_thread(self._cover_dir.mkdir, parents=True, exist_ok=True)
        except OSError:
            self.logger.warning("cannot create %s: TIDAL/SoundCloud covers are drawn but not kept", self._cover_dir)
        self.mass.create_task(self._hook_covers())
        handler = self.mass.command_handlers.get(ITEMS_COMMAND)
        if handler is None:
            self.logger.warning("%s not found: no artist subtitles for other providers", ITEMS_COMMAND)
            return
        original = handler.target

        async def with_subtitles(**kwargs):
            items = await original(**kwargs)
            out = list(items)
            try:
                out, todo = enrich(out, self._cached_artists)
                for uri in todo:
                    self._schedule(uri)
            except Exception:
                self.logger.exception("artist subtitles failed; the row keeps its own subtitles")
                out = list(items)
            try:
                out = self._with_covers(out)
            except Exception:
                self.logger.exception("source covers failed; keeping the playlists' own artwork")
            try:
                out = await self._fresh_covers(out)
            except Exception:
                self.logger.exception("current covers not looked up; keeping the row's images")
            return UniqueList(out)

        handler.target = with_subtitles
        self._hooked = (handler, original)

    async def unload(self, is_removed: bool = False) -> None:
        if getattr(self, "_hooked", None):
            handler, original = self._hooked
            handler.target = original
            self._hooked = None
        if getattr(self, "_cover_hook", None):
            self._cover_hook.__dict__.pop("_generate_and_write", None)  # back to the class's own method
            self._cover_hook = None

    async def _hook_covers(self) -> None:
        """Draw the type stack on covers MA generates for the bridge's playlists. Genres, colours and moods come
        from the layout file. Any error keeps MA's original cover; unload removes the hook."""
        for _ in range(COVER_HOOK_TRIES):  # the cover generator may load after this provider
            covers = self.mass.get_provider(COVER_PROVIDER)
            if covers is not None and hasattr(covers, "_generate_and_write"):
                break
            await asyncio.sleep(COVER_HOOK_WAIT_S)
        else:
            self.logger.warning("%s provider not found: no type stack on covers", COVER_PROVIDER)
            return
        original = covers._generate_and_write

        async def framed(playlist, template=None, fanart=False):
            image = await original(playlist, template, fanart)
            if image is None or fanart:
                return image
            try:
                if playlist.name.startswith(COVER_PREFIX):
                    layout = _read_layout() or {}
                    genres = (layout.get("genres") or {}).get(playlist.name) or []
                    regrid = (template or str(covers.config.get_value("template"))) == GRID_TEMPLATE
                    mood = (layout.get("moods") or {}).get(playlist.name)
                    await asyncio.to_thread(restyle_file, image.path, list(genres), layout.get("cover_style"), regrid,
                                            layout.get("hues"), mood)
            except Exception:
                self.logger.exception("type stack failed for %s; keeping the plain cover", playlist.name)
            return image

        covers._generate_and_write = framed
        self._cover_hook = covers

    async def _fresh_covers(self, items: list) -> list:
        """Playlists whose generated cover file is gone (Recently played keeps old file names) get the library
        playlist's current thumb."""
        stale = await asyncio.to_thread(stale_generated, items, COVER_PROVIDER, os.path.exists)
        for i in stale:
            library = await self.mass.music.playlists.get_library_item(items[i].item_id)
            thumb = next((img for img in getattr(getattr(library, "metadata", None), "images", None) or []
                          if getattr(getattr(img, "type", None), "value", getattr(img, "type", None)) == "thumb"), None)
            if thumb is not None:
                items[i] = with_thumb(items[i], thumb)
        return items

    def _with_covers(self, items: list) -> list:
        """TIDAL/SoundCloud playlists named in the layout's "sources" get a thumb this provider serves
        (resolve_image). Nothing is drawn here; MA asks for the image when a client shows it."""
        out, seen = with_source_covers(items, _read_layout(), self._cover_image)
        new = {name: record for name, record in seen.items() if name not in self._covers}
        if new:
            self._covers.update(new)
            while len(self._covers) > COVERS_KEPT:
                del self._covers[next(iter(self._covers))]
            self.mass.create_task(self._remember(new))
        return out

    def _cover_image(self, name: str) -> MediaItemImage:
        return MediaItemImage(type=ImageType.THUMB, path=name, provider=self.instance_id, remotely_accessible=False)

    async def _remember(self, new: dict) -> None:
        """Keep the records on disk too: MA keeps image ids for a year, so after a restart a client may ask for a
        cover before Discover is listed again."""
        try:
            async with self._index_lock:   # rows arrive together; each write rereads the index
                await asyncio.to_thread(remember, self._cover_dir, new)
        except Exception as exc:
            self.logger.debug("cover index not written: %s", exc)

    async def resolve_image(self, path: str) -> str | bytes:
        """A TIDAL/SoundCloud cover by name: from the cover directory, or drawn on the playlist's own artwork.
        MA caches the result under (provider, path) for good, so a failed fetch or drawing raises: MA keeps the
        failure for five minutes and then asks again."""
        if not valid_name(path):
            raise FileNotFoundError(f"not a Spotify Bridge cover: {path}")
        if (data := await asyncio.to_thread(cached, self._cover_dir, path)) is not None:
            return data
        record = self._covers.get(path) or await asyncio.to_thread(recall, self._cover_dir, path)
        if not record or record.get("provider") == self.instance_id:   # never fetch our own images through MA
            raise FileNotFoundError(f"unknown Spotify Bridge cover: {path}")
        entry = record["entry"]
        tiles = await self._tiles(entry)   # before taking a draw slot: a slow image server must not hold one
        async with self._draw_limit:
            if (data := await asyncio.to_thread(cached, self._cover_dir, path)) is not None:   # drawn meanwhile
                return data
            original = None if tiles else await get_image_data(self.mass, record["path"], record["provider"])
            try:
                data = await asyncio.to_thread(draw_source, original, entry["source"], list(entry.get("genres") or []),
                                               record.get("style"), record.get("hues"), entry.get("moods"),
                                               record["uri"], tiles)
            except Exception:
                self.logger.exception("cover for %s failed; MA asks again in a few minutes", record.get("uri"))
                raise
            try:
                await asyncio.to_thread(store, self._cover_dir, path, data)
            except Exception as exc:
                self.logger.debug("cover %s not kept: %s", path, exc)
        return data

    async def _tiles(self, entry: dict) -> list[bytes]:
        """The album covers listed in a TIDAL entry, fetched together. One that fails or takes longer than
        TILE_TIMEOUT_S fails the cover: MA keeps the failure for a few minutes and then asks again, rather than
        keeping a cover with missing albums for good."""
        pairs = [(str(p[0]), str(p[1])) for p in entry.get("tiles") or []
                 if isinstance(p, list) and len(p) == 2 and p[1] != self.instance_id]
        return list(await asyncio.gather(*(asyncio.wait_for(get_image_data(self.mass, path, provider), TILE_TIMEOUT_S)
                                           for path, provider in pairs)))

    def _cached_artists(self, uri: str) -> str | None:
        hit = self._artists.get(uri)
        return hit[1] if hit and time.time() - hit[0] < ARTIST_CACHE_S else None

    def _schedule(self, uri: str) -> None:
        if uri not in self._pending:
            self._pending.add(uri)
            self.mass.create_task(self._compute_artists(uri))

    async def _compute_artists(self, uri: str) -> None:
        try:
            async with self._limit:
                provider, rest = uri.split("://", 1)
                item_id = rest.split("/", 1)[1]
                names: list[list[str]] = []
                async for track in self.mass.music.playlists.tracks(item_id, provider):
                    names.append([a.name for a in (getattr(track, "artists", None) or [])])
                    if len(names) >= ARTIST_TRACKS:
                        break
                sub = top_artists(names)
                if sub:
                    self._artists[uri] = (time.time(), sub)
        except Exception as exc:
            self.logger.debug("no artists for %s: %s", uri, exc)
        finally:
            self._pending.discard(uri)

    @property
    def priority(self) -> int:
        """Before TheAudioDB (20) and Wikipedia (25), so the bridge's artist text is the bio MA picks."""
        return 10

    async def get_artist_metadata(self, artist) -> MediaItemMetadata | None:
        """Bio and facts for the artist page, prepared by the bridge (Last.fm, MusicBrainz, Wikipedia, own data)."""
        text = artist_text(_read_artists(), artist.name)
        return MediaItemMetadata(description=text, description_language="en") if text else None

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return the row headers (items are fetched per row)."""
        layout = _read_layout()
        if layout_rows(layout, []) is None:
            headers = [(item_id, name, icon) for item_id, (name, icon) in ROWS.items()]
        else:
            headers = [(f"layout_{row['id']}", row["title"], "mdi-spotify") for row in layout.get("rows", [])]
        return [RecommendationFolder(item_id=item_id, provider=self.instance_id, name=name, icon=icon)
                for item_id, name, icon in headers]

    async def get_recommendation_items(self, item_id: str) -> UniqueList:
        """Return the playlists for one row."""
        playlists = await self.mass.music.playlists.library_items(search="Spotify", limit=500)
        if item_id == "spotify_current":
            return UniqueList(without_source_logo(current_rows(playlists)))
        for row_id, _title, found in layout_rows(_read_layout(), playlists) or []:
            if item_id == f"layout_{row_id}":
                return UniqueList(without_source_logo(found))
        return UniqueList()
