#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer an explicitly selected interpreter, then the current Windows account's
# installation when running from WSL or git-bash. Never bind to one username.
if [ -n "${AUTOREG_PYTHON:-}" ]; then
  PY="$AUTOREG_PYTHON"
else
  PY=""
  WINDOWS_HOME="${USERPROFILE:-}"
  if [ -z "$WINDOWS_HOME" ] && command -v cmd.exe >/dev/null 2>&1; then
    WINDOWS_HOME="$(cmd.exe /d /c 'echo %USERPROFILE%' 2>/dev/null || true)"
    WINDOWS_HOME="${WINDOWS_HOME//$'\r'/}"
  fi
  if [ -n "$WINDOWS_HOME" ] && command -v wslpath >/dev/null 2>&1; then
    WINDOWS_HOME="$(wslpath -u "$WINDOWS_HOME")"
  elif [ -n "$WINDOWS_HOME" ] && command -v cygpath >/dev/null 2>&1; then
    WINDOWS_HOME="$(cygpath -u "$WINDOWS_HOME")"
  fi
  for candidate in \
    "$WINDOWS_HOME"/AppData/Local/Programs/Python/Python*/python.exe
  do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
  done
  if [ -z "$PY" ] && command -v python3 >/dev/null 2>&1; then PY=python3; fi
  if [ -z "$PY" ] && command -v python >/dev/null 2>&1; then PY=python; fi
fi

if [ -z "${PY:-}" ]; then
  echo "autoreg: python not found; set AUTOREG_PYTHON" >&2
  exit 127
fi

SCRIPT="$DIR/scripts/autoreg.py"
# Windows python.exe cannot read POSIX paths from WSL (/mnt/c) or git-bash (/c).
if [[ "$PY" == *.exe ]]; then
  if command -v wslpath >/dev/null 2>&1; then
    SCRIPT="$(wslpath -w "$SCRIPT")"
  elif command -v cygpath >/dev/null 2>&1; then
    SCRIPT="$(cygpath -w "$SCRIPT")"
  fi
fi

exec "$PY" "$SCRIPT" "$@"
