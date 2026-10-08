"""MA WebSocket event stream (spike check 3): events flow after auth; there is no subscribe command."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import websockets

from bridge.ma.client import MaError

AUTH_TIMEOUT_S = 30


async def stream_events(url: str, token: str) -> AsyncIterator[dict]:
    async with websockets.connect(url, max_size=None, open_timeout=AUTH_TIMEOUT_S) as ws:
        await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT_S)  # server info
        await ws.send(json.dumps({"message_id": "auth", "command": "auth", "args": {"token": token}}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("message_id") == "auth":
                if "error_code" in msg:
                    raise MaError(f"auth failed: {msg.get('details')}", msg.get("error_code"))
                continue
            if "event" in msg:
                yield msg
