"""scripts/setup.sh, run in a temporary copy of the repository: it creates .env and config.yaml from the examples,
fills in the answers, never prints a secret, keeps .env private, and changes nothing when run again."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from bridge.config import load_config

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

SECRETS = {"SETUP_MA_TOKEN": "ma-token-one", "SETUP_SPOTIFY_SP_DC": "cookie-one",
           "SETUP_STATUS_PASSWORD": "password-one"}
ANSWERS = {**SECRETS, "SETUP_TZ": "Europe/London", "SETUP_FEEDBACK": "no", "SETUP_ANALYSIS": "yes",
           "SETUP_MA_URL": "192.0.2.10", "SETUP_LANGUAGE": "de", "SETUP_PLAYERS": "02:00:00:00:00:05, kitchen"}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "setup.sh", r / "scripts")
    shutil.copy(ROOT / ".env.example", r)
    shutil.copy(ROOT / "config.example.yaml", r)
    return r


def run(repo: Path, *args: str, **answers: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SETUP_")}
    env.update({"SETUP_NONINTERACTIVE": "1", "SETUP_SKIP_NETWORK": "1", **answers})
    return subprocess.run(["bash", "scripts/setup.sh", *args], cwd=repo, env=env, capture_output=True, text=True)


def read_env(repo: Path) -> dict[str, str]:
    out = {}
    for line in (repo / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "setup.sh")]).returncode == 0


def test_first_run_writes_every_answer(repo):
    result = run(repo, **ANSWERS)
    assert result.returncode == 0, result.stderr
    env = read_env(repo)
    assert env["MA_TOKEN"] == "ma-token-one" and env["STATUS_PASSWORD"] == "password-one"
    assert env["SPOTIFY_SP_DC"] == "cookie-one"
    assert len(env["SESSION_SECRET"]) == 64 and int(env["SESSION_SECRET"], 16) >= 0
    assert env["TZ"] == "Europe/London" and env["COMPOSE_PROFILES"] == "analysis"
    assert env["FEEDBACK_API_TOKEN"] == ""  # no adapter: an empty token keeps the feedback queue off
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600
    cfg = load_config(repo / "config.yaml", env)
    assert cfg.ma_url == "ws://192.0.2.10:8095/ws" and cfg.language == "de" and cfg.timezone == "Europe/London"
    assert cfg.players_allowlist == frozenset({"02:00:00:00:00:05", "kitchen"})  # strings, not YAML numbers
    for folder in ("data", "chrome-profile", "backup-copy", "health", "ma_provider/spotify_bridge/state"):
        assert (repo / folder).is_dir()


@pytest.mark.parametrize("answer, expected", [
    ("192.0.2.10", "ws://192.0.2.10:8095/ws"),
    ("192.0.2.10:9000", "ws://192.0.2.10:9000/ws"),
    ("http://127.0.0.1:8095", "ws://127.0.0.1:8095/ws"),
    ("https://ma.example.org:8095/", "wss://ma.example.org:8095/ws"),
    ("ws://192.0.2.10:8095", "ws://192.0.2.10:8095/ws"),
    ("ws://192.0.2.10:8095/ws/", "ws://192.0.2.10:8095/ws"),
    ("wss://ma.example.org/ws", "wss://ma.example.org/ws"),
])
def test_ma_address_becomes_a_websocket_address(repo, answer, expected):
    """The address MA shows in the browser (http://host:8095) is turned into its WebSocket address."""
    result = run(repo, **{**ANSWERS, "SETUP_MA_URL": answer})
    assert result.returncode == 0, result.stderr
    assert load_config(repo / "config.yaml", read_env(repo)).ma_url == expected


def test_no_secret_is_printed(repo):
    result = run(repo, **ANSWERS)
    output = result.stdout + result.stderr
    session_secret = read_env(repo)["SESSION_SECRET"]
    for value in [*SECRETS.values(), session_secret]:
        assert value not in output
    check = run(repo, "--check")
    for value in [*SECRETS.values(), session_secret]:
        assert value not in check.stdout + check.stderr


def test_adapter_on_generates_its_token(repo):
    result = run(repo, **{**ANSWERS, "SETUP_FEEDBACK": "yes"})
    assert result.returncode == 0, result.stderr
    env = read_env(repo)
    assert env["COMPOSE_PROFILES"] == "feedback,analysis" and len(env["FEEDBACK_API_TOKEN"]) == 64


def test_second_run_changes_nothing(repo):
    run(repo, **{**ANSWERS, "SETUP_FEEDBACK": "yes"})
    before = ((repo / ".env").read_text(), (repo / "config.yaml").read_text())
    result = run(repo)  # no answers at all: every value already in the files is kept
    assert result.returncode == 0, result.stderr
    assert ((repo / ".env").read_text(), (repo / "config.yaml").read_text()) == before


def test_existing_files_are_edited_not_replaced(repo):
    (repo / "config.yaml").write_text("ma_url: ws://127.0.0.1:8095/ws\nlanguage: en\npoll_interval: 2h\n")
    result = run(repo, **ANSWERS)
    assert result.returncode == 0, result.stderr
    text = (repo / "config.yaml").read_text()
    assert "poll_interval: 2h" in text and "language: de" in text


def test_missing_required_secret_stops_with_its_name(repo):
    answers = {k: v for k, v in ANSWERS.items() if k != "SETUP_MA_TOKEN"}
    result = run(repo, **answers)
    assert result.returncode != 0 and "SETUP_MA_TOKEN" in result.stderr


def test_value_that_env_files_would_misread_is_refused(repo):
    result = run(repo, **{**ANSWERS, "SETUP_STATUS_PASSWORD": "pass$word"})
    assert result.returncode != 0 and "SETUP_STATUS_PASSWORD" in result.stderr


def test_check_reports_names_and_fails_without_files(repo):
    assert run(repo, "--check").returncode != 0
    run(repo, **ANSWERS)
    check = run(repo, "--check")
    assert check.returncode == 0, check.stdout
    assert "MA_TOKEN: set" in check.stdout and "FEEDBACK_API_TOKEN: empty (optional)" in check.stdout
