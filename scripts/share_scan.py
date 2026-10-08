#!/usr/bin/env python3
"""Scan files for data that must not leave one installation: private network addresses, MAC addresses, email
addresses, home folders, per-account Spotify playlist IDs, Music Assistant provider instance ids, cookie
values and tokens. Placeholders that the repository uses on purpose are allowed (192.0.2.x, 00:00:5e:00:53:xx,
02:00:00:00:00:xx, example domains, IDs with "Fake", provider ids ending in "--test...").

Used by tests/test_share_hygiene.py (every tracked file that is not export-ignored) and by
scripts/export-share.sh (every file of an export).

    python3 scripts/share_scan.py <folder> [--patterns <file>] [--dangling]

--patterns adds literal strings, one per line (blank lines and lines starting with # are skipped), that are
reported wherever they occur, in any letter case. --dangling also reports references to the folders the export leaves out.
The exit code is 1 when anything is found. Findings show the file, line and pattern name, never the match.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# The patterns avoid spelling out what they look for, so this file does not report itself.
GENERIC = {
    "private IPv4 address": re.compile(
        r"(?<![\d.])(?:1[0]\.\d{1,3}|19[2]\.168|17[2]\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?!\.?\d)"),
    "MAC address": re.compile(
        r"(?<![0-9A-Fa-f:])(?!00:00:5[eE]:00:53:)(?!02:00:00:00:00:)(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"),
    "email address": re.compile(
        r"[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*example\.(?:org|com|net)\b)(?!example\b)"
        r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)*\.[A-Za-z]{2,}\b"),
    "home or server folder": re.compile(r"/(?:hom[e]|User[s]|sr[v])/"),
    "per-account Spotify playlist ID": re.compile(
        r"37i9dQZEV[X][bc](?!Fake)[A-Za-z0-9]|37i9dQZF1[E](?!Fake)(?!P6YuccBxUcC1)[A-Za-z0-9]"),
    "provider instance id": re.compile(r"\b[a-z][a-z_]*-[-](?!test)[A-Za-z0-9]{8}\b"),
    "cookie value": re.compile(r"sp_d[c]=[A-Za-z0-9%_-]{20,}", re.I),
    "token": re.compile(r"\bey[J][A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|\bBeare[r] [A-Za-z0-9._~+/-]{20,}"),
}
# The daylist ID "37i9dQZF1EP6YuccBxUcC1" is allowed: it is the same for every account.
DANGLING = re.compile(r"docs/(?:review[s]|spik[e]|superpower[s]|sharin[g])\b|(?<![/\w.])spik[e]/|idea\.tx[t]")
# these name the excluded paths on purpose
DANGLING_SKIP = {".gitattributes", ".gitignore", "share_scan.py", "export-share.sh"}
BINARY_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".sqlite",
                   ".pb", ".zip", ".gz"}


def is_text(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES or path.name == ".DS_Store":
        return False
    try:
        return b"\0" not in path.read_bytes()[:8192]
    except OSError:
        return False


def load_patterns(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def scan(root: Path, paths: list[str], literals: list[str] = (), dangling: bool = False) -> list[str]:
    """Findings as "path:line: pattern name". Literal patterns match in any letter case and are named by their
    position in the file only."""
    findings = []
    folded_literals = [literal.casefold() for literal in literals]
    for rel in paths:
        path = root / rel
        if not path.is_file() or not is_text(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for name, pattern in GENERIC.items():
                if pattern.search(line):
                    findings.append(f"{rel}:{n}: {name}")
            folded = line.casefold()
            for i, literal in enumerate(folded_literals, 1):
                if literal in folded:
                    findings.append(f"{rel}:{n}: owner pattern #{i}")
            if dangling and Path(rel).name not in DANGLING_SKIP and DANGLING.search(line):
                findings.append(f"{rel}:{n}: reference to an excluded path")
    return findings


def all_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(root).parts)


def main(argv: list[str]) -> int:
    args, literals, dangling = [], [], False
    it = iter(argv)
    for a in it:
        if a == "--patterns":
            literals = load_patterns(Path(next(it)))
        elif a == "--dangling":
            dangling = True
        else:
            args.append(a)
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(args[0])
    findings = scan(root, all_files(root), literals, dangling)
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
