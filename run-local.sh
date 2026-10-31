#!/bin/bash
# Lancement local (Linux/macOS) : http://localhost:8000 — jeton : local
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
export DRF_ADMIN_TOKEN=local DRF_SECRET=local-dev DRF_INSECURE_COOKIES=1 DRF_DATA_DIR="$PWD/data" DRF_EMBED_WORKER=1
exec .venv/bin/python app.py
