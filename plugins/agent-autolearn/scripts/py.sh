#!/bin/sh
# Ejecuta un script del plugin con Python 3.9+: python3 en macOS/Linux; en Windows (Git Bash)
# suele existir solo python o el lanzador py. El alias python3 de la Microsoft Store no cuenta.
# Uso: sh py.sh <script.py> [args...]   (stdin y codigo de salida pasan tal cual)
export PYTHONDONTWRITEBYTECODE=1
script="$(dirname "$0")/$1"
shift
ok='import sys; sys.exit(sys.version_info < (3, 9))'
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "$ok" </dev/null >/dev/null 2>&1; then
    exec "$py" "$script" "$@"
  fi
done
if command -v py >/dev/null 2>&1 && py -3 -c "$ok" </dev/null >/dev/null 2>&1; then
  exec py -3 "$script" "$@"
fi
echo "agent-autolearn: no se encontro Python 3.9+ (python3, python o py -3)." >&2
exit 127
