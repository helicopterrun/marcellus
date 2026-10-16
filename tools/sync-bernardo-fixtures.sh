#!/usr/bin/env bash
# Vendor Bernardo's golden contract fixtures into this repo.
#
# Usage: tools/sync-bernardo-fixtures.sh <ref>     (branch, tag or commit sha)
#
# Lists helicopterrun/wildlife-cam:fixtures/contract at <ref> via `gh api`,
# downloads every file into tests/fixtures/external/bernardo/contract/ (stale
# local files no longer upstream are deleted) and writes VENDORED.json with the
# resolved full commit sha and a sha256 per file. tests/test_external_bernardo_
# contract.py verifies VENDORED.json against the files on disk.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <ref>" >&2
  exit 2
fi
ref="$1"
repo="helicopterrun/wildlife-cam"
path="fixtures/contract"
root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$root/tests/fixtures/external/bernardo/contract"

sha="$(gh api "repos/$repo/commits/$ref" --jq .sha)"
names="$(gh api "repos/$repo/contents/$path?ref=$sha" --jq '.[] | select(.type == "file") | .name')"
[ -n "$names" ] || { echo "no files found at $repo/$path@$ref" >&2; exit 1; }

mkdir -p "$dest"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

while IFS= read -r name; do
  gh api "repos/$repo/contents/$path/$name?ref=$sha" --jq .content | base64 -d > "$tmp/$name"
done <<< "$names"

# Remove stale vendored files (everything but the generated manifest).
for f in "$dest"/*; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  [ "$base" = "VENDORED.json" ] && continue
  if ! grep -qxF "$base" <<< "$names"; then
    rm -f "$f"
    echo "removed stale $base"
  fi
done

cp "$tmp"/* "$dest"/

python3 - "$dest" "$repo" "$path" "$sha" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

dest, repo, path, sha = Path(sys.argv[1]), *sys.argv[2:]
files = {
    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(dest.iterdir())
    if p.is_file() and p.name != "VENDORED.json"
}
doc = {"repo": repo, "path": path, "ref": sha, "files": files}
(dest / "VENDORED.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
PY
echo "vendored $(echo "$names" | wc -l | tr -d ' ') files from $repo@$sha"
