"""In-process fake of the MA WebSocket API, enough for MaClient, resolver and publisher tests."""
from __future__ import annotations

import itertools
import json

import websockets


class FakeMa:
    def __init__(self) -> None:
        self.playlists: dict[str, list[str]] = {}
        self.names: dict[str, str] = {}
        self.search_results: list[dict] = []
        self.items: dict[str, dict] = {}
        self.calls: list[str] = []
        self.drop_next = False            # close the socket before running the next non-auth command
        self.drop_after_dispatch = False  # run the next non-auth command, then close without replying
        self.reject: set[str] = set()     # URIs that add_playlist_tracks silently skips
        self.task_delay = 0               # tasks/get polls before a playlist task applies its change
        self.fail_tasks = False           # playlist tasks end as "failed" without changing anything
        self.artists: list = []           # music/artists/library_items
        self.library_added: list = []     # music/library/add_item
        self.fail_metadata = False        # metadata/update_metadata returns an error
        self.metadata_refreshed: list = []  # (item, force_refresh) per metadata/update_metadata call
        self.tasks: dict[str, dict] = {}
        self.rows: list[dict] = []                     # music/recommendations
        self.row_items: dict[tuple, list[dict]] = {}   # (provider, row item_id) -> music/recommendations/items
        self.provider_tracks: dict[tuple, list[dict]] = {}  # (provider, playlist item_id) -> its tracks
        self.events_after_auth: list[dict] = []  # pushed right after a successful auth reply
        self._ids = itertools.count(200)
        self._server = None
        self.url = ""

    async def __aenter__(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    def _tracks(self, pid: str) -> list[dict]:
        return [{"provider": "tidal--T", "uri": u, "item_id": u.rsplit("/", 1)[1], "position": i + 1,
                 "provider_mappings": []} for i, u in enumerate(self.playlists[pid])]

    async def _handle(self, ws):
        await ws.send(json.dumps({"server_version": "2.10.4", "schema_version": 65}))
        async for raw in ws:
            msg = json.loads(raw)
            cmd, args, mid = msg["command"], msg.get("args", {}), msg["message_id"]
            self.calls.append(cmd)
            if self.drop_next and cmd != "auth":
                self.drop_next = False
                await ws.close()
                return
            await ws.send(json.dumps({"event": "queue_time_updated", "data": {}}))  # noise to skip
            try:
                result = self._dispatch(cmd, args)
            except KeyError as exc:
                await ws.send(json.dumps({"message_id": mid, "error_code": 2, "details": str(exc)}))
                continue
            if self.drop_after_dispatch and cmd != "auth":
                self.drop_after_dispatch = False
                await ws.close()
                return
            await ws.send(json.dumps({"message_id": mid, "result": result}))
            if cmd == "auth":
                for e in self.events_after_auth:
                    await ws.send(json.dumps(e))

    def _dispatch(self, cmd: str, a: dict):
        if cmd == "auth":
            if a.get("token") != "good":
                raise KeyError("auth")
            return {"authenticated": True}
        if cmd == "music/search":
            return {"tracks": self.search_results}
        if cmd == "music/item_by_uri":
            return self.items.get(a["uri"], {"uri": a["uri"]})
        if cmd == "music/playlists/create_playlist":
            pid = str(next(self._ids))
            self.playlists[pid], self.names[pid] = [], a["name"]
            return {"item_id": pid, "name": a["name"]}
        if cmd == "music/playlists/get":
            pid = str(a["item_id"])
            if pid not in self.playlists:
                raise KeyError("not found")
            return {"item_id": pid, "name": self.names[pid]}
        if cmd == "music/playlists/library_items":
            search = a.get("search") or ""
            return [{"item_id": pid, "name": n} for pid, n in self.names.items() if search in n]
        if cmd == "music/playlists/playlist_tracks":
            if a.get("provider_instance_id_or_domain", "library") != "library":
                return self.provider_tracks[(a["provider_instance_id_or_domain"], str(a["item_id"]))]
            return self._tracks(str(a["item_id"]))
        if cmd == "music/recommendations":
            return self.rows
        if cmd == "music/recommendations/items":
            return self.row_items.get((a["provider"], a["item_id"]), [])
        if cmd == "music/playlists/remove_playlist_tracks":
            pid = str(a["db_playlist_id"])
            drop = set(a["positions_to_remove"])

            def apply():
                self.playlists[pid] = [u for i, u in enumerate(self.playlists[pid]) if i + 1 not in drop]
            return self._task(apply)
        if cmd == "music/playlists/add_playlist_tracks":
            pid, uris = str(a["db_playlist_id"]), list(a["uris"])
            return self._task(lambda: self.playlists[pid].extend(u for u in uris if u not in self.reject))
        if cmd == "player_queues/get":
            return {"queue_id": a["queue_id"]}
        if cmd == "music/library/add_item":
            self.library_added.append(a["item"])
            return {}
        if cmd == "music/artists/library_items":
            return self.artists[a.get("offset", 0):a.get("offset", 0) + a.get("limit", 500)]
        if cmd == "metadata/update_metadata":
            if self.fail_metadata:
                raise KeyError("metadata provider error")
            self.metadata_refreshed.append((a["item"], a["force_refresh"]))
            return {}
        if cmd == "tasks/get":
            t = self.tasks[a["task_id"]]
            if t["status"] == "running":
                t["polls"] += 1
                if t["polls"] >= self.task_delay:
                    self._finish(t)
            return {"id": a["task_id"], "status": t["status"], "failure_messages": t["failure_messages"]}
        raise KeyError(cmd)

    def _task(self, apply) -> dict:
        tid = f"task{len(self.tasks) + 1}"
        t = self.tasks[tid] = {"status": "running", "polls": 0, "apply": apply, "failure_messages": []}
        if self.task_delay == 0:
            self._finish(t)
        return {"id": tid, "status": t["status"], "failure_messages": t["failure_messages"]}

    def _finish(self, t: dict) -> None:
        if self.fail_tasks:
            t["status"], t["failure_messages"] = "failed", ["provider unavailable"]
        else:
            t["apply"]()
            t["status"] = "success"
