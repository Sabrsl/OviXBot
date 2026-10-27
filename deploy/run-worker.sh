#!/bin/bash
# Lancé par la tâche continue Toolforge (voir jobs.yaml).
cd "$HOME/www/python/src" || exit 1
exec "$HOME/www/python/venv/bin/python" -m deadreffix.worker
