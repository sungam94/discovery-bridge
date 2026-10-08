"""No tracked file that ships carries data of one particular installation (scripts/share_scan.py has the
patterns). Files marked export-ignore in .gitattributes are left out, as the export leaves them out."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("share_scan", ROOT / "scripts" / "share_scan.py")
share_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(share_scan)


def shipped_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    paths = [p for p in files.decode().split("\0") if p]
    attrs = subprocess.run(["git", "check-attr", "-z", "export-ignore", "--stdin"], cwd=ROOT, check=True,
                           input="\0".join(paths).encode(), capture_output=True).stdout.decode().split("\0")
    ignored = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "set"}
    return [p for p in paths if p not in ignored]


def test_shipped_files_carry_no_private_data():
    assert share_scan.scan(ROOT, shipped_files(), dangling=True) == []


def test_private_files_are_not_shipped():
    shipped = shipped_files()
    private = ("docs/" + "sharing/", "spi" + "ke/")
    assert not [p for p in shipped if p.startswith(private) or p.endswith(".DS_Store")]
    assert {".env", "config.yaml"}.isdisjoint(shipped)


@pytest.mark.parametrize("sample", [
    "10." + "1.2.3", "192." + "168.0.4", "172." + "20.1.1", "dc:a6:" + "32:00:11:22", "someone@" + "gmail.com",
    "/ho" + "me/me",
    "37i9dQZEVXc" + "O1234567890", "37i9dQZF1E" + "3a1234567890", "tidal-" + "-Ab3dEfGh", "sp_dc=" + "A" * 30,
    "ey" + "J" + "a" * 12 + "." + "b" * 12, "the server is at 10." + "1.2.3.",
])
def test_scanner_reports_private_shapes(sample):
    assert any(p.search(sample) for p in share_scan.GENERIC.values())


@pytest.mark.parametrize("sample", [
    "192.0.2.10", "00:00:5e:00:53:01", "02:00:00:00:00:05", "you@example.org", "37i9dQZEVXcFakeDw00001",
    "37i9dQZF1EFakeDm000001", "37i9dQZF1EP6YuccBxUcC1", "tidal--test0001", "Python 3.10.12", 'sp_dc=env["X"]',
])
def test_scanner_allows_placeholders(sample):
    assert not any(p.search(sample) for p in share_scan.GENERIC.values())


def test_owner_patterns_match_in_any_letter_case(tmp_path):
    (tmp_path / "notes.md").write_text("built on myserver and on MYSERVER\n")
    assert share_scan.scan(tmp_path, ["notes.md"], ["MyServer"]) == ["notes.md:1: owner pattern #1"]
