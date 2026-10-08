"""Spec §3.3: one poll over all slots. Opens the MA connection lazily and closes it at the end."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime

from bridge.ingest.slots import STALE_AFTER, Slot, assign_daily_mixes
from bridge.metric.weekly import compute_weekly
from bridge.publish.layout import write_layout
from bridge.publish.publisher import ensure_playlist_row
from bridge.repo import get_setting, is_paused, set_setting
from bridge.spotify.client import AuthExpired, RateLimited, SpotifyError
from bridge.spotify.parse import FIXED_PREFIXES, SECTION_DAILY_MIXES, daily_mix_ids, daylist_id, fixed_playlist_ids
from bridge.timeutil import iso, parse_iso

log = logging.getLogger("bridge.poller")
MAX_HUB_WAIT_S = 300


class Poller:
    def __init__(self, conn: sqlite3.Connection, spotify, ingestor, publisher, ma, cfg,
                 now: Callable[[], datetime], sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._conn, self._spotify, self._ingestor, self._publisher = conn, spotify, ingestor, publisher
        self._ma, self._cfg, self._now, self._sleep = ma, cfg, now, sleep

    def _health(self, slot: str | None, state: str, detail: str = "") -> None:
        """Keep the current state per slot; write a history row only when the state changes."""
        if slot is None:
            prev = get_setting(self._conn, "ingestion_state")
            set_setting(self._conn, "ingestion_state", state)
        else:
            row = self._conn.execute("SELECT last_state FROM playlist WHERE slot = ?", (slot,)).fetchone()
            prev = row["last_state"] if row else None
            self._conn.execute("UPDATE playlist SET last_state = ?, last_error = ? WHERE slot = ?",
                               (state, None if state == "ok" else detail[:500], slot))
        if state != prev:
            self._conn.execute("INSERT INTO ingestion_health(slot, ts, state, detail) VALUES (?, ?, ?, ?)",
                               (slot, iso(self._now()), state, detail[:500]))

    def _load_slots(self) -> list[Slot]:
        out = []
        for i in range(1, 7):
            name = f"daily_mix_{i}"
            ensure_playlist_row(self._conn, name, "daily_mix", None)
            r = self._conn.execute("SELECT * FROM playlist WHERE slot = ?", (name,)).fetchone()
            out.append(Slot(name, r["spotify_id"], parse_iso(r["last_seen"]) if r["last_seen"] else None,
                            bool(r["stale"])))
        return out

    def _save_slots(self, slots: list[Slot], unslotted: list[str]) -> None:
        for s in slots:
            self._conn.execute("UPDATE playlist SET spotify_id = ?, last_seen = ?, stale = ? WHERE slot = ?",
                               (s.spotify_id, iso(s.last_seen) if s.last_seen else None, int(s.stale), s.name))
        self._conn.execute("DELETE FROM unslotted_playlist")
        for pid in unslotted:
            self._conn.execute("INSERT INTO unslotted_playlist VALUES (?, ?)", (pid, iso(self._now())))

    def _hub_targets(self, sections: dict[str, list[tuple[str, str]]], skip: set[str]) -> list[tuple[str, str]]:
        """Spotify playlists of the configured hub sections, one slot per playlist ID (stable across polls)."""
        now = self._now()
        hub_sections = getattr(self._cfg, "hub_sections", ())
        for sec in hub_sections:
            for pos, (pid, name) in enumerate(sections.get(sec, [])):
                if pid in skip:
                    continue
                slot = f"hub:{pid}"
                ensure_playlist_row(self._conn, slot, "hub", pid, name=f"Spotify · {name.strip()}")
                self._conn.execute("UPDATE playlist SET section = ?, hub_position = ?, last_seen = ?, stale = 0 "
                                   "WHERE slot = ?", (sec, pos, iso(now), slot))
        targets = []
        for sec in hub_sections:
            for r in self._conn.execute("SELECT slot, spotify_id, last_seen FROM playlist WHERE kind = 'hub' "
                                        "AND section = ? ORDER BY hub_position", (sec,)).fetchall():
                if r["last_seen"] and now - parse_iso(r["last_seen"]) > STALE_AFTER:
                    self._conn.execute("UPDATE playlist SET stale = 1 WHERE slot = ?", (r["slot"],))
                else:
                    targets.append((r["slot"], r["spotify_id"]))
        return targets

    async def _hub(self) -> dict[str, list[tuple[str, str]]]:
        try:
            return await self._spotify.made_for_you()
        except RateLimited as exc:
            await self._sleep(min(exc.retry_after, MAX_HUB_WAIT_S))
            return await self._spotify.made_for_you()

    async def run_once(self) -> dict[str, str]:
        if is_paused(self._conn, "ingestion_paused"):
            return {"*": "paused"}
        try:
            return await self._run()
        finally:
            await self._ma.close()

    async def _run(self) -> dict[str, str]:
        try:
            sections = await self._hub()
            self._health(None, "ok")
        except AuthExpired as exc:
            self._health(None, "auth_expired", str(exc))
            return {"*": "auth_expired"}
        except SpotifyError as exc:
            self._health(None, "error", f"made_for_you: {exc}")
            sections = {}
        targets: list[tuple[str, str]] = self._fixed_targets(sections)
        slots = self._load_slots()
        if SECTION_DAILY_MIXES in sections:
            slots, unslotted = assign_daily_mixes(daily_mix_ids(sections), slots, self._now())
            self._save_slots(slots, unslotted)
        targets += [(s.name, s.spotify_id) for s in slots if s.spotify_id and not s.stale]
        dl = daylist_id(sections)
        if dl:
            ensure_playlist_row(self._conn, "daylist", "daylist", dl)
            self._conn.execute("UPDATE playlist SET spotify_id = ? WHERE slot = 'daylist'", (dl,))
        dl_row = self._conn.execute("SELECT spotify_id FROM playlist WHERE slot = 'daylist'").fetchone()
        if dl_row and dl_row["spotify_id"]:
            targets.append(("daylist", dl_row["spotify_id"]))
        targets += self._hub_targets(sections, {pid for _, pid in targets} | set(daily_mix_ids(sections)))
        out = await self._ingest_all(targets)
        try:
            compute_weekly(self._conn)
        except Exception:  # the metric must never break ingestion
            log.exception("weekly metric failed")
        return out

    def _fixed_targets(self, sections: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
        """Discover Weekly and Release Radar: the ID from config.yaml, else the one in the user's hub, else the one
        stored by an earlier poll. A slot with none of the three is skipped and reported, not fatal."""
        in_hub = fixed_playlist_ids(sections)
        targets = []
        for slot in FIXED_PREFIXES:
            ensure_playlist_row(self._conn, slot, "fixed", getattr(self._cfg, f"{slot}_id") or in_hub.get(slot))
            row = self._conn.execute("SELECT spotify_id FROM playlist WHERE slot = ?", (slot,)).fetchone()
            if row["spotify_id"]:
                targets.append((slot, row["spotify_id"]))
            else:
                self._health(slot, "error", f"not found in your hub; set {slot}_id in config.yaml")
        return targets

    def _write_layout(self) -> None:
        layout_dir = getattr(self._cfg, "ma_layout_dir", None)
        if layout_dir:
            write_layout(self._conn, getattr(self._cfg, "hub_sections", ()), layout_dir,
                         getattr(self._cfg, "cover_style", None), getattr(self._cfg, "genre_hues", None),
                         sound_path=getattr(self._cfg, "sound_db_path", None), language=self._cfg.language)

    async def _ingest_all(self, targets: list[tuple[str, str]]) -> dict[str, str]:
        """The layout is rewritten after every slot, so new rows show up in MA during a long first sync."""
        out: dict[str, str] = {}
        for slot, pid in targets:
            try:
                await self._ingest_one(slot, pid, out)
            finally:
                self._write_layout()
            if out.get(slot) == "auth_expired":
                break
        return out

    async def _ingest_one(self, slot: str, pid: str, out: dict[str, str]) -> None:
        try:
            ingest = await self._ingestor.ingest_slot(slot, pid)
            out[slot] = f"{ingest}/{await self._publisher.check(slot)}"
            empty = self._conn.execute("SELECT empty_count FROM playlist WHERE slot = ?", (slot,)).fetchone()
            problems = getattr(self._ingestor, "last_problems", [])
            if empty and empty["empty_count"] >= 3:
                self._health(slot, "error", f"empty fetch x{empty['empty_count']}")
            elif problems:
                self._health(slot, "error", "; ".join(problems))
            else:
                self._health(slot, "ok", out[slot])
        except AuthExpired as exc:
            self._health(slot, "auth_expired", str(exc))
            out[slot] = "auth_expired"
        except Exception as exc:  # one playlist must not stop the others
            self._health(slot, "error", f"{type(exc).__name__}: {exc}")
            out[slot] = "error"
