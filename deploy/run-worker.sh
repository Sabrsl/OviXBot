#!/bin/bash
# Lancé par la tâche continue Toolforge (voir jobs.yaml).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_HOME="$(dirname "$SRC_DIR")"
VENV_PYTHON="$PYTHON_HOME/venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Environnement Python introuvable : $VENV_PYTHON" >&2
    echo "Lancez deploy/install.sh sur Toolforge pour créer le venv dans l'image python3.11 des jobs." >&2
    exit 1
fi

if [[ -z "${TOOL_DATA_DIR:-}" ]]; then
    # $PYTHON_HOME pointe vers <données outil>/www/python.
    export TOOL_DATA_DIR="$(cd "$PYTHON_HOME/../.." && pwd)"
fi

cd "$SRC_DIR"
exec "$VENV_PYTHON" -m deadlinkfix.worker
