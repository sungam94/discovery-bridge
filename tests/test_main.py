from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from bridge.main import ensure_cert, next_backup_time, startup_warnings


def test_next_backup_time_local():
    now = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)  # 02:00 Berlin
    assert next_backup_time(now, "Europe/Berlin").isoformat() == "2026-10-04T01:30:00+00:00"
    later = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)  # 04:00 Berlin
    assert next_backup_time(later, "Europe/Berlin").isoformat() == "2026-10-05T01:30:00+00:00"


def test_ensure_cert_creates_once(tmp_path: Path):
    ensure_cert(tmp_path / "tls", "127.0.0.1", "localhost")
    cert = tmp_path / "tls" / "cert.pem"
    first = cert.read_bytes()
    ensure_cert(tmp_path / "tls", "127.0.0.1", "localhost")
    assert cert.read_bytes() == first and (tmp_path / "tls" / "key.pem").exists()


def test_startup_warnings():
    allow = frozenset({"p"})
    assert startup_warnings(SimpleNamespace(backup_copy_target=None, players_allowlist=allow, feedback_api_token="t")) == [
        "backup_copy_target is not set: backups live only on the same disk as the database"]
    assert startup_warnings(SimpleNamespace(backup_copy_target=Path("/x"), players_allowlist=allow, feedback_api_token="t")) == []


def test_startup_warns_about_empty_allowlist():
    cfg = SimpleNamespace(backup_copy_target="/x", players_allowlist=frozenset(), feedback_api_token="t")
    assert any("players_allowlist" in w for w in startup_warnings(cfg))
    cfg = SimpleNamespace(backup_copy_target="/x", players_allowlist=frozenset({"p"}), feedback_api_token="t")
    assert startup_warnings(cfg) == []


async def test_poll_loop_records_results(tmp_path):
    import asyncio

    from bridge.db import connect, migrate
    from bridge.main import poll_loop
    from bridge.repo import get_setting

    conn = connect(tmp_path / "b.sqlite")
    migrate(conn)

    class Boom:
        async def run_once(self):
            raise RuntimeError("hub down")

    task = asyncio.create_task(poll_loop(Boom(), 3600, asyncio.Event(), conn))
    await asyncio.sleep(0.05)
    task.cancel()
    assert get_setting(conn, "poll_failures") == "1" and "hub down" in get_setting(conn, "last_poll_error")


def test_startup_warns_about_missing_feedback_token():
    cfg = SimpleNamespace(backup_copy_target=Path("/x"), players_allowlist=frozenset({"p"}), feedback_api_token=None)
    assert any("FEEDBACK_API_TOKEN" in w for w in startup_warnings(cfg))


def test_a_missed_backup_is_caught_up_at_start():
    from datetime import datetime, timedelta, timezone
    from bridge.main import backup_overdue
    now = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)
    assert backup_overdue(None, now)
    assert backup_overdue((now - timedelta(hours=26)).isoformat(), now)
    assert not backup_overdue((now - timedelta(hours=8)).isoformat(), now)


async def test_source_loop_runs_at_start_and_survives_a_failure():
    import asyncio

    from bridge.main import source_loop

    class Collector:
        def __init__(self):
            self.runs = 0

        async def run_once(self):
            self.runs += 1
            raise RuntimeError("MA down")

    c = Collector()
    task = asyncio.create_task(source_loop(c, every_s=3600, retry_s=0.01))   # a failed pass is retried soon
    await asyncio.sleep(0.05)
    task.cancel()
    assert c.runs >= 2


def test_ensure_cert_subject_and_alt_names(tmp_path: Path, monkeypatch):
    import bridge.main as main_mod
    calls = []
    monkeypatch.setattr(main_mod.subprocess, "run", lambda args, **kw: calls.append(args))
    ensure_cert(tmp_path / "tls", "192.0.2.10", hostname="homeserver")
    (args,) = calls
    subject = args[args.index("-subj") + 1]
    assert subject.startswith("/CN=")
    name = subject[len("/CN="):]
    assert name
    assert name == "homeserver"
    assert args[args.index("-addext") + 1] == f"subjectAltName=IP:192.0.2.10,DNS:{name}.local"


def test_no_ip_san_for_a_wildcard_bind(tmp_path: Path, monkeypatch):
    import bridge.main as main_mod
    calls = []
    monkeypatch.setattr(main_mod.subprocess, "run", lambda args, **kw: calls.append(args))
    ensure_cert(tmp_path / "tls", "0.0.0.0", "homeserver")
    (args,) = calls
    assert args[args.index("-subj") + 1] == "/CN=homeserver"
    assert args[args.index("-addext") + 1] == "subjectAltName=DNS:homeserver.local"


def test_status_hostname_defaults_to_the_machine_name():
    import socket

    from bridge.main import status_hostname
    assert status_hostname(SimpleNamespace(status_hostname=None)) == socket.gethostname()
    assert status_hostname(SimpleNamespace(status_hostname="homeserver")) == "homeserver"
