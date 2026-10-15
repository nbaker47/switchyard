#!/usr/bin/env bash
# Put `cr` on your PATH. Idempotent; touches nothing else.
set -euo pipefail
ROOT=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BIN="${CR_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN"
chmod +x "$ROOT/bin/cr"
ln -sf "$ROOT/bin/cr" "$BIN/cr"
echo "linked $BIN/cr -> $ROOT/bin/cr"
command -v claude >/dev/null || echo "  ! claude not found - install Claude Code first"
if command -v agy >/dev/null; then
  "$ROOT/bin/cr" skills   # give agy the skills Claude Code has
else
  echo "  - agy not found - Antigravity routes will be skipped"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  ! $BIN is not on your PATH" ;; esac
