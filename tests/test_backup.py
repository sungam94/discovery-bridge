from datetime import date, timedelta

import pytest

from bridge.backup import run_backup, verify_backup
from bridge.db import connect, migrate


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "bridge.sqlite"
    conn = connect(p)
    migrate(conn)
    conn.execute("INSERT INTO playlist(slot, kind, name) VALUES ('discover_weekly', 'fixed', 'x')")
    return p


def test_backup_and_verify(db, tmp_path):
    out = run_backup(db, tmp_path / "backups", date(2026, 10, 4))
    assert out.name == "bridge-2026-10-04.sqlite"
    counts = verify_backup(out)
    assert counts["playlist"] == 1 and counts["setting"] == 2


def test_same_day_replaces(db, tmp_path):
    run_backup(db, tmp_path / "backups", date(2026, 10, 4))
    run_backup(db, tmp_path / "backups", date(2026, 10, 4))
    assert [p.name for p in (tmp_path / "backups").iterdir()] == ["bridge-2026-10-04.sqlite"]


def test_retention_and_copy(db, tmp_path):
    copy = tmp_path / "copy"
    for i in range(16):
        run_backup(db, tmp_path / "backups", date(2026, 10, 1) + timedelta(days=i), keep=14, copy_target=copy)
    files = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert len(files) == 14 and files[0] == "bridge-2026-10-03.sqlite"
    assert [p.name for p in copy.iterdir()] == ["bridge-latest.sqlite"]


def test_keep_must_be_positive(db, tmp_path):
    with pytest.raises(ValueError):
        run_backup(db, tmp_path / "backups", date(2026, 10, 4), keep=0)


def test_a_second_database_gets_its_own_files(db, tmp_path):
    copy = tmp_path / "copy"
    run_backup(db, tmp_path / "backups", date(2026, 10, 4), copy_target=copy)
    out = run_backup(db, tmp_path / "backups", date(2026, 10, 4), copy_target=copy, name="sound")
    assert out.name == "sound-2026-10-04.sqlite"
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == ["bridge-2026-10-04.sqlite",
                                                                        "sound-2026-10-04.sqlite"]
    assert sorted(p.name for p in copy.iterdir()) == ["bridge-latest.sqlite", "sound-latest.sqlite"]
