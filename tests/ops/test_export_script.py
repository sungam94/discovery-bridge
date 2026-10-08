"""scripts/export-share.sh, run against a small throwaway repository: it refuses unsafe starts, leaves out
export-ignored files, stops on private data and makes a new repository with one commit and no remote."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="needs git and bash")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "private").mkdir()
    shutil.copy(ROOT / "scripts" / "export-share.sh", r / "scripts")
    shutil.copy(ROOT / "scripts" / "share_scan.py", r / "scripts")
    (r / "README.md").write_text("hello\n")
    (r / "private" / "notes.md").write_text("server at " + "192." + "168.1.5\n")
    (r / ".gitattributes").write_text("private export-ignore\nprivate/** export-ignore\n")
    git(r, "init", "-q", "-b", "share-ready")
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "start")
    return r


def run(repo: Path, target: Path, **env):
    base = {k: v for k, v in os.environ.items() if not k.startswith(("EXPORT_", "GIT_"))}
    empty_patterns = repo.parent / "empty-patterns.txt"
    empty_patterns.write_text("")
    base.update({"EXPORT_AUTHOR_NAME": "Discovery Bridge", "EXPORT_AUTHOR_EMAIL": "bridge@example.org",
                 "EXPORT_TEST_CMD": "true", "SHARE_PATTERNS_FILE": str(empty_patterns)})
    base.update(env)
    base = {k: v for k, v in base.items() if v is not None}
    return subprocess.run(["bash", "scripts/export-share.sh", str(target)], cwd=repo, env=base,
                          capture_output=True, text=True)


def test_export_has_one_commit_without_the_ignored_files(repo, tmp_path):
    out = tmp_path / "export"
    result = run(repo, out)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (out / "README.md").exists() and not (out / "private").exists()
    assert git(out, "log", "--format=%an <%ae>|%s").splitlines() == ["Discovery Bridge <bridge@example.org>|"
                                                                     "Discovery Bridge"]
    assert git(out, "remote").strip() == ""


def test_refuses_without_an_identity(repo, tmp_path):
    result = run(repo, tmp_path / "export", EXPORT_AUTHOR_EMAIL=None)
    assert result.returncode != 0 and "EXPORT_AUTHOR_EMAIL" in result.stderr
    assert not (tmp_path / "export").exists()


def test_refuses_an_existing_target(repo, tmp_path):
    (tmp_path / "export").mkdir()
    assert run(repo, tmp_path / "export").returncode != 0


def test_refuses_a_dirty_tree_or_another_branch(repo, tmp_path):
    (repo / "README.md").write_text("changed\n")
    assert run(repo, tmp_path / "a").returncode != 0
    git(repo, "checkout", "-q", "--", "README.md")
    git(repo, "checkout", "-q", "-b", "other")
    assert run(repo, tmp_path / "b").returncode != 0


def test_stops_on_private_data_in_a_shipped_file(repo, tmp_path):
    (repo / "notes.md").write_text("server at " + "192." + "168.1.5\n")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "leak")
    result = run(repo, tmp_path / "export")
    assert result.returncode != 0 and "notes.md:1: private IPv4 address" in result.stdout + result.stderr
    assert "168.1.5" not in result.stdout + result.stderr  # the finding names the pattern, never the value


def test_owner_patterns_are_checked_and_never_printed(repo, tmp_path):
    (repo / "notes.md").write_text("made on myserver\n")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "name")
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("# my literals\nmyserver\n")
    result = run(repo, tmp_path / "export", SHARE_PATTERNS_FILE=str(patterns))
    assert result.returncode != 0 and "owner pattern #1" in result.stdout + result.stderr
    assert "myserver" not in result.stdout + result.stderr


def test_owner_patterns_match_in_any_letter_case(repo, tmp_path):
    (repo / "notes.md").write_text("made on MYSERVER.lan\n")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "name")
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("MyServer\n")
    result = run(repo, tmp_path / "export", SHARE_PATTERNS_FILE=str(patterns))
    assert result.returncode != 0 and "owner pattern #1" in result.stdout + result.stderr
    assert "myserver" not in (result.stdout + result.stderr).lower()


def test_refuses_when_the_owner_patterns_file_is_missing(repo, tmp_path):
    result = run(repo, tmp_path / "export", SHARE_PATTERNS_FILE=str(tmp_path / "none.txt"))
    assert result.returncode != 0 and "SHARE_ALLOW_NO_PATTERNS" in result.stderr
    assert not (tmp_path / "export").exists()


def test_exports_without_the_patterns_file_only_when_allowed(repo, tmp_path):
    result = run(repo, tmp_path / "export", SHARE_PATTERNS_FILE=str(tmp_path / "none.txt"),
                 SHARE_ALLOW_NO_PATTERNS="1")
    assert result.returncode == 0 and "WARNING" in result.stderr
