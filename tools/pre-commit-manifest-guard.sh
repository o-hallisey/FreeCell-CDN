#!/usr/bin/env bash
# Pre-commit hook — refuses commits that violate manifest.json invariants.
#
# Install per clone (NOT committed under .git/hooks — git ignores any file
# tracked under .git/hooks/ on commit):
#
#   ln -s ../../tools/pre-commit-manifest-guard.sh .git/hooks/pre-commit
#
# Or copy if your shell can't symlink:
#
#   cp tools/pre-commit-manifest-guard.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# The hook only runs when manifest.json is in the staged change set, so
# README / asset-only commits are unaffected.

set -euo pipefail

# Resolve repo root no matter where the hook fires from.
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Skip if manifest.json isn't staged.
if ! git diff --cached --name-only --diff-filter=ACMR | grep -qx 'manifest.json'; then
    exit 0
fi

# Stage manifest.json into a tempfile so the validator sees the to-be-committed
# bytes, not whatever's currently in the working tree (which may carry further
# uncommitted edits).
STAGED_TMP="$(mktemp -t manifest.staged.XXXXXX.json)"
trap 'rm -f "$STAGED_TMP"' EXIT
git show ":manifest.json" > "$STAGED_TMP"

python tools/validate-manifest.py \
    --head-ref HEAD \
    --staged-manifest "$STAGED_TMP"
