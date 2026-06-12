#!/bin/bash
# Quick commit + push helper for tenantsentry-app.
# Clears any stale git lock files left behind by Claude's sandbox
# (virtiofs bridge can't unlink its own lock files) before committing.
#
# Usage:
#   ./commit.sh "commit message"
#   ./commit.sh "commit message" file1 file2   # stage specific files only
#
# With no files given, stages everything (git add -A).

set -e

cd "$(dirname "$0")"

if [ -z "$1" ]; then
  echo "Usage: ./commit.sh \"commit message\" [file ...]"
  exit 1
fi

MSG="$1"
shift

# Clean up any stale lock files from interrupted/cross-mount git operations.
rm -f .git/index.lock .git/HEAD.lock .git/refs/heads/*.lock 2>/dev/null || true

if [ "$#" -gt 0 ]; then
  git add -- "$@"
else
  git add -A
fi

git commit -m "$MSG"
git push
