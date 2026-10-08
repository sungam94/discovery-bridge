"""The analysis loop: next pending track, Deezer preview, models, row in sound.sqlite. Clips are deleted at once."""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from analysis.deezer import DeezerUnavailable
from analysis.model import Analyser
from analysis.selection import pending
from analysis.store import save, save_deezer

log = logging.getLogger("analysis")
IDLE_S = 600
BACKOFF_S = 60
BACKOFF_MAX_S = 1800
GIVE_UP_AFTER = 5  # times Deezer was unavailable for the same track before it is stored as an error
ERRORS_IN_A_ROW = 10  # then something is wrong with Deezer or the models: back off instead of storing errors
PROGRESS_EVERY = 25


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Service:
    def __init__(self, bridge: sqlite3.Connection, sound: sqlite3.Connection, deezer, analyser: Analyser,
                 workdir: Path, now: Callable[[], datetime] = utcnow) -> None:
        self._bridge, self._sound, self._deezer, self._analyser = bridge, sound, deezer, analyser
        self._clip = Path(workdir) / "clip.mp3"
        self._now = now
        self._done = 0
        self._streak = 0          # Deezer unavailable this many times in a row
        self._errors = 0          # tracks that ended in an error in a row
        self._fails: dict[str, int] = {}

    def step(self) -> bool:
        """Analyses the next pending track. False when none is pending; DeezerUnavailable means try again later."""
        todo = pending(self._bridge, self._sound, self._now(), limit=1)
        if not todo:
            return False
        isrc = todo[0]
        started = time.monotonic()
        own_error = None
        try:
            status, fields = self._analyse(isrc)
            if status == "error":
                self._errors += 1
                if self._errors >= ERRORS_IN_A_ROW:
                    own_error = fields
                    raise DeezerUnavailable(f"{self._errors} errors in a row ({fields['error']})")
            else:
                self._errors = 0
        except DeezerUnavailable as exc:
            self._streak += 1
            self._fails[isrc] = self._fails.get(isrc, 0) + 1
            if self._fails[isrc] < GIVE_UP_AFTER:
                raise
            status, fields = "error", own_error or {"error": f"DeezerUnavailable: {exc}"[:200]}
        else:
            self._streak = 0
        self._fails.pop(isrc, None)
        save(self._sound, isrc, status, self._now(), **fields)
        self._done += 1
        detail = f": {fields['error']}" if status == "error" else ""
        log.info("%s %s in %.1f s%s", isrc, status, time.monotonic() - started, detail)
        if self._done % PROGRESS_EVERY == 0:
            left = len(pending(self._bridge, self._sound, self._now()))
            log.info("progress: %d tracks analysed since start, %d pending", self._done, left)
        return True

    def _analyse(self, isrc: str) -> tuple[str, dict]:
        facts = None
        try:
            url, facts = self._deezer.lookup(isrc)
            if url is None:
                return "no_preview", {"deezer": facts or {}}   # {}: Deezer knows nothing, not asked again
            self._deezer.download(url, self._clip)
            sound = self._analyser.analyse(self._clip)
        except DeezerUnavailable:
            raise
        except Exception as exc:  # ClipError, a clip that does not decode, a model failure
            return "error", {"error": f"{type(exc).__name__}: {exc}"[:200]}
        finally:
            self._clip.unlink(missing_ok=True)
        return "done", {"moods": sound.moods, "instrumental": sound.instrumental, "embedding": sound.embedding,
                        "deezer": facts}

    def backfill_step(self) -> bool:
        """Deezer facts for one track analysed before they were kept; False when none is left."""
        row = self._sound.execute("SELECT isrc FROM track_sound WHERE deezer IS NULL AND status IN ('done', 'no_preview') "
                                  "LIMIT 1").fetchone()
        if row is None:
            return False
        try:
            _, facts = self._deezer.lookup(row[0])
        except Exception as exc:  # DeezerUnavailable or a per-track error: leave it, the facts are optional
            if not isinstance(exc, DeezerUnavailable):
                save_deezer(self._sound, row[0], {})
            raise DeezerUnavailable(str(exc)) if isinstance(exc, DeezerUnavailable) else exc
        save_deezer(self._sound, row[0], facts)
        return True

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            try:
                busy = self.step()
            except DeezerUnavailable as exc:
                wait = min(BACKOFF_S * 2 ** (self._streak - 1), BACKOFF_MAX_S)
                log.warning("Deezer unavailable (%s); next try in %d s", exc, wait)
                sleep(wait)
                continue
            except Exception:
                log.exception("analysis step failed")
                sleep(BACKOFF_S)
                continue
            if not busy:
                try:
                    busy = self.backfill_step()
                except DeezerUnavailable as exc:
                    log.warning("Deezer unavailable while filling track facts (%s)", exc)
                except Exception:
                    log.exception("filling track facts failed")
            if not busy:
                sleep(IDLE_S)
