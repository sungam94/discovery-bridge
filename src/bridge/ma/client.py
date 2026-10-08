"""Raw Music Assistant WebSocket client (MA 2.10.4, schema 65). Shapes: spike findings checks 3-8."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("bridge.ma")

IDEMPOTENT_COMMANDS = frozenset({
    "music/search", "music/item_by_uri", "music/playlists/get",
    "music/playlists/library_items", "music/playlists/playlist_tracks", "tasks/get",
    # pure reads used by capture (Phase 2): safe to resend after a dropped socket
    "player_queues/get", "players/all", "music/tracks/library_items", "music/albums/album_tracks",
    "music/artists/library_items",
    # Discover rows of other providers, read for their playlist covers
    "music/recommendations", "music/recommendations/items",
})
TASK_OK = frozenset({"success", "partial_success"})
TASK_FAILED = frozenset({"failed", "cancelled", "unknown"})


class MaError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def tidal_uri(track: dict) -> str:
    if str(track.get("provider", "")).startswith("tidal"):
        return track["uri"]
    for pm in track.get("provider_mappings", []):
        if pm.get("provider_domain") == "tidal":
            return f"{pm['provider_instance']}://track/{pm['item_id']}"
    return track["uri"]


class MaClient:
    TASK_POLL_S = 1.0

    def __init__(self, url: str, token: str, call_timeout_s: float = 90) -> None:
        self._url = url
        self._token = token
        self._timeout = call_timeout_s
        self._ws = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        ws = await websockets.connect(self._url, max_size=None)
        self._ws = ws
        try:
            await asyncio.wait_for(ws.recv(), timeout=self._timeout)  # server info
            await self._send("auth", {"token": self._token})
        except BaseException:
            await ws.close()
            self._ws = None
            raise

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _send(self, command: str, args: dict[str, Any]) -> Any:
        mid = str(next(self._ids))
        await self._ws.send(json.dumps({"message_id": mid, "command": command, "args": args}))
        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self._timeout)
            except TimeoutError:
                await self.close()
                raise MaError(f"{command}: no reply within {self._timeout}s") from None
            msg = json.loads(raw)
            if msg.get("message_id") != mid:
                continue  # events and stale replies
            if "error_code" in msg:
                raise MaError(f"{command}: {msg.get('details')}", msg.get("error_code"))
            return msg.get("result")

    async def call(self, command: str, **args: Any) -> Any:
        async with self._lock:
            if self._ws is None:
                await self.connect()
            try:
                return await self._send(command, args)
            except (ConnectionClosed, OSError) as exc:
                await self.close()
                if command not in IDEMPOTENT_COMMANDS:
                    raise MaError(f"{command}: connection lost; not resent") from exc
                await self.connect()
                return await self._send(command, args)

    async def search_tracks(self, query: str, limit: int = 25) -> list[dict]:
        res = await self.call("music/search", search_query=query, media_types=["track"], limit=limit)
        return (res or {}).get("tracks") or []

    async def get_item(self, uri: str) -> dict:
        return await self.call("music/item_by_uri", uri=uri)

    async def get_playlist(self, playlist_id: str) -> dict | None:
        try:
            return await self.call("music/playlists/get", item_id=str(playlist_id),
                                   provider_instance_id_or_domain="library")
        except MaError as exc:
            if exc.code == 2:
                return None
            raise

    async def create_playlist(self, name: str) -> str:
        return str((await self.call("music/playlists/create_playlist", name=name))["item_id"])

    async def find_playlist_by_name(self, name: str) -> str | None:
        items = await self.call("music/playlists/library_items", search=name, limit=50, offset=0)
        found = next((str(p["item_id"]) for p in items if p.get("name") == name), None)
        if found is not None:
            return found
        offset = 0
        while True:
            page = await self.call("music/playlists/library_items", search=None, limit=500, offset=offset)
            found = next((str(p["item_id"]) for p in page if p.get("name") == name), None)
            if found is not None or len(page) < 500:
                return found
            offset += 500

    async def _tracks(self, playlist_id: str) -> list[dict]:
        return await self.call("music/playlists/playlist_tracks", item_id=str(playlist_id),
                               provider_instance_id_or_domain="library")

    async def recommendation_rows(self) -> list[dict]:
        """MA's Discover rows (item_id, provider, name), without their items."""
        return await self.call("music/recommendations") or []

    async def recommendation_items(self, provider: str, item_id: str) -> list[dict]:
        """The items of one Discover row. MA answers an error or a timeout of the provider with an empty row."""
        return await self.call("music/recommendations/items", provider=provider, item_id=item_id) or []

    async def provider_playlist_tracks(self, item_id: str, provider: str) -> list[dict]:
        """Tracks of a playlist as its provider lists them (one that is not in the MA library)."""
        return await self.call("music/playlists/playlist_tracks", item_id=str(item_id),
                               provider_instance_id_or_domain=provider) or []

    async def playlist_track_uris(self, playlist_id: str) -> list[str]:
        tracks = sorted(await self._tracks(playlist_id), key=lambda t: t.get("position", 0))
        return [tidal_uri(t) for t in tracks]

    async def _settle(self, playlist_id: str, target: int, timeout_s: float) -> None:
        """Wait until the track count reaches target or stops changing for 3 reads (MA may skip items)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last, same = -1, 0
        while True:
            n = len(await self._tracks(playlist_id))
            if n == target:
                return
            same = same + 1 if n == last else 0
            last = n
            if same >= 3:
                return
            if loop.time() > deadline:
                raise MaError(f"playlist {playlist_id}: count {n} did not settle within {timeout_s}s")
            await asyncio.sleep(1)

    async def _await_task(self, task: Any, timeout_s: float) -> None:
        """Playlist edits run as MA background tasks; wait until MA reports the task finished."""
        if not isinstance(task, dict) or "id" not in task:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        status = task.get("status")
        while status not in TASK_OK:
            if status in TASK_FAILED:
                raise MaError(f"MA task {task.get('name', task['id'])} {status}: "
                              f"{'; '.join(task.get('failure_messages') or []) or task.get('last_error')}")
            if loop.time() > deadline:
                raise MaError(f"MA task {task['id']} still {status} after {timeout_s}s")
            await asyncio.sleep(self.TASK_POLL_S)
            task = await self.call("tasks/get", task_id=task["id"])
            status = task.get("status")

    async def replace_playlist(self, playlist_id: str, uris: list[str], timeout_s: float = 300) -> list[str]:
        """Add the new items first, then remove the old ones, so a failed add never leaves it empty."""
        old_positions = [t["position"] for t in await self._tracks(playlist_id)]
        for i in range(0, len(uris), 100):
            await self._await_task(await self.call("music/playlists/add_playlist_tracks", db_playlist_id=playlist_id,
                                                   uris=uris[i:i + 100]), timeout_s)
        await self._settle(playlist_id, len(old_positions) + len(uris), timeout_s)
        if old_positions:
            await self._await_task(await self.call("music/playlists/remove_playlist_tracks", db_playlist_id=playlist_id,
                                                   positions_to_remove=old_positions), timeout_s)
            await self._settle(playlist_id, len(uris), timeout_s)
        present = set(await self.playlist_track_uris(playlist_id))
        try:  # best effort: the content is published either way
            await self.refresh_cover(playlist_id)
        except MaError as exc:
            log.warning("cover refresh for MA playlist %s failed: %s", playlist_id, exc)
        return [u for u in uris if u not in present]

    async def add_to_library(self, uri: str) -> None:
        """Add an item to the MA library; MA's library sync-back also adds it on the streaming service."""
        await self.call("music/library/add_item", item=uri)

    async def library_artists(self, page: int = 500) -> list[tuple[str, str]]:
        """(name, item id) of every artist in the MA library."""
        out, offset = [], 0
        while True:
            items = await self.call("music/artists/library_items", limit=page, offset=offset)
            out += [(a["name"], str(a["item_id"])) for a in items]
            if len(items) < page:
                return out
            offset += page

    async def refresh_artist(self, item_id: str) -> str:
        """Make MA ask its metadata providers again (the artist page text comes from spotify_bridge) and return
        the bio MA shows afterwards ("" for none). MA skips the providers when it finds no MusicBrainz id."""
        item = await self.call("metadata/update_metadata", item=f"library://artist/{item_id}", force_refresh=True)
        return ((item or {}).get("metadata") or {}).get("description") or ""

    async def refresh_cover(self, playlist_id: str) -> None:
        """MA rebuilds a playlist's cover collage from its tracks only every 90 days, so a cover made while the
        playlist was still filling (few distinct images, repeated) would stay. Raises MaError on failure."""
        await self.call("metadata/update_metadata", item=f"library://playlist/{playlist_id}", force_refresh=True)
