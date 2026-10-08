#!/usr/bin/env bash
# Export this repository as a new repository with one commit, for sharing a copy without its history.
#
#   EXPORT_AUTHOR_NAME="..." EXPORT_AUTHOR_EMAIL="..." scripts/export-share.sh <target-dir> [--ref <commit>]
#
# 1. Refuses unless the working tree is clean and HEAD is on branch share-ready (or --ref names a commit).
# 2. Refuses unless EXPORT_AUTHOR_NAME and EXPORT_AUTHOR_EMAIL are set and <target-dir> does not exist yet.
# 3. git archive of the commit: only committed files, minus those marked export-ignore in .gitattributes, so
#    untracked files such as config.yaml, .env, data/ or chrome-profile/ cannot end up in the export.
# 4. Scans the export with the generic patterns of scripts/share_scan.py.
# 5. Scans again with literal strings from an untracked patterns file outside the repository:
#    SHARE_PATTERNS_FILE, default ~/.config/discovery-bridge/share-patterns.txt, one string per line (host
#    names, user names, playlist IDs, provider ids, email, subnet), matched in any letter case. Without that
#    file the export is refused, unless SHARE_ALLOW_NO_PATTERNS=1 is set; then it only warns.
# 6. Fails on references to the paths the export leaves out.
# 7. git init -b main and git add -A in the export, so its tests run in their own repository.
# 8. Runs the tests in the export (EXPORT_TEST_CMD, default: uv run pytest -q).
# 9. One commit "Discovery Bridge" with the given identity. Never adds a remote, never pushes.
# Findings name the file, the line and the pattern, never the matched text.
set -euo pipefail

die() { echo "export-share: $*" >&2; exit 1; }

target="" ref=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) ref="${2:-}"; [ -n "$ref" ] || die "--ref needs a commit"; shift 2 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$target" ] || die "only one target directory"; target="$1"; shift ;;
  esac
done
[ -n "$target" ] || die "usage: scripts/export-share.sh <target-dir> [--ref <commit>]"

repo="$(git rev-parse --show-toplevel)"
cd "$repo"

# 1. clean tree, right branch or an explicit commit
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean; commit or stash first"
if [ -z "$ref" ]; then
  branch="$(git symbolic-ref --short -q HEAD || true)"
  [ "$branch" = "share-ready" ] || die "HEAD is on '${branch:-a detached commit}', not share-ready; use --ref to export another commit"
  ref="HEAD"
fi
commit="$(git rev-parse --verify -q "$ref^{commit}")" || die "not a commit: $ref"

# 2. identity and a new target
[ -n "${EXPORT_AUTHOR_NAME:-}" ] || die "set EXPORT_AUTHOR_NAME (the author of the export's single commit)"
[ -n "${EXPORT_AUTHOR_EMAIL:-}" ] || die "set EXPORT_AUTHOR_EMAIL (the author email of the export's single commit)"
[ ! -e "$target" ] || die "$target exists already; choose a new directory"
patterns="${SHARE_PATTERNS_FILE:-$HOME/.config/discovery-bridge/share-patterns.txt}"
if [ ! -f "$patterns" ] && [ "${SHARE_ALLOW_NO_PATTERNS:-}" != "1" ]; then
  die "no owner patterns file at $patterns. Put your host names, user names, playlist IDs, provider ids,
email and addresses there, one per line, or set SHARE_ALLOW_NO_PATTERNS=1 to export with the generic scan only."
fi

# 3. committed files only, without export-ignore
mkdir -p "$target"
target="$(cd "$target" && pwd)"
git archive --format=tar "$commit" | tar -x -C "$target"
echo "export-share: $(git rev-parse --short "$commit") extracted to $target"

scan="$target/scripts/share_scan.py"
[ -f "$scan" ] || die "the export has no scripts/share_scan.py"

# 4. and 6. generic patterns and references to excluded paths
python3 "$scan" "$target" --dangling || die "the export contains private data or references to excluded paths (see above)"

# 5. the owner's own literal strings, from a file that never enters the repository
if [ -f "$patterns" ]; then
  python3 "$scan" "$target" --patterns "$patterns" || die "the export contains strings from $patterns (see above)"
else
  echo "export-share: WARNING: no owner patterns file at $patterns; only the generic scan ran" >&2
  echo "export-share: WARNING: (SHARE_ALLOW_NO_PATTERNS=1 is set)." >&2
  echo "export-share: WARNING: put host names, user names, playlist IDs, provider ids and addresses there, one per line." >&2
fi

# 7. a repository of its own
git -C "$target" init -q -b main
git -C "$target" add -A

# 8. the export's own tests
(cd "$target" && ${EXPORT_TEST_CMD:-uv run pytest -q}) || die "the export's tests failed"

# 9. one commit with the chosen identity; no remote, no push
git -C "$target" add -A
GIT_AUTHOR_NAME="$EXPORT_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$EXPORT_AUTHOR_EMAIL" \
GIT_COMMITTER_NAME="$EXPORT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$EXPORT_AUTHOR_EMAIL" \
  git -C "$target" -c commit.gpgsign=false commit -q -m "Discovery Bridge"
echo "export-share: done. $target has one commit and no remote."
