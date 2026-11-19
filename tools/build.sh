#!/usr/bin/env bash
# tools/build.sh [output dir]: the collection's tar.gz built from the commit checked out (HEAD, as committed),
# that commit written in plugins/plugin_utils/cassandra_build.py of the copy built: an installed collection has no
# .git, and its reports name the commit they were made by. Plain "ansible-galaxy collection build" works too: its
# reports then say "commit unknown".
set -euo pipefail
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
out=$(realpath "${1:-.}")
commit=$(git -C "$root" rev-parse --short=12 HEAD)
if [ -n "$(git -C "$root" status --porcelain --untracked-files=no)" ]; then
  echo "Local changes are not built: the tar.gz is commit $commit as committed." >&2
fi
work=$(mktemp -d)
trap 'rm -r -- "${work:?}"' EXIT
git -C "$root" archive HEAD | tar -x -C "$work"
file=$work/plugins/plugin_utils/cassandra_build.py
sed -i "s/^COMMIT = \"\"/COMMIT = \"$commit\"/" "$file"
grep -q "^COMMIT = \"$commit\"" "$file" || { echo "No COMMIT line written in $file: not built." >&2; exit 1; }
ansible-galaxy collection build "$work" --output-path "$out"
