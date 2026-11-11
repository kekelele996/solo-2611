#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
NPM=${SOLO_AUTO_NPM:-}

if [ -z "$NPM" ]; then
  for candidate in /opt/homebrew/bin/npm /usr/local/bin/npm; do
    if [ -x "$candidate" ]; then
      NPM="$candidate"
      break
    fi
  done
fi
if [ -z "$NPM" ] || [ ! -x "$NPM" ]; then
  printf '找不到 npm，无法构建前端\n' >&2
  exit 1
fi

cd "$ROOT/frontend"
"$NPM" run build

cd "$ROOT"
exec "$ROOT/backend/.venv/bin/python" -m solo_auto
