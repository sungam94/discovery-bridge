"""Spec §11: daily VACUUM INTO backup, retention, off-array copy, restore verification."""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path


def run_backup(db_path: Path, backup_dir: Path, day: date, keep: int = 14,
               copy_target: Path | None = None, name: str = "bridge") -> Path:
    """Backup of one database as <name>-<day>.sqlite (and <name>-latest.sqlite in copy_target)."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / f"{name}-{day.isoformat()}.sqlite"
    tmp = backup_dir / f".tmp-{name}-{day.isoformat()}.sqlite"
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(str(db_path))
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()
    os.replace(tmp, final)
    for old in sorted(backup_dir.glob(f"{name}-*.sqlite"))[:-keep]:
        old.unlink()
    if copy_target is not None:
        copy_target.mkdir(parents=True, exist_ok=True)
        part = copy_target / f".{name}-latest.part"
        shutil.copy2(final, part)
        os.replace(part, copy_target / f"{name}-latest.sqlite")
    return final


def verify_backup(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"integrity check failed: {path}")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()
