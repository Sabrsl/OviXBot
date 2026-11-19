#!/bin/bash
# À exécuter SUR Toolforge, connecté au compte outil (`become ovixbot`), depuis le dossier du projet décompressé.
set -euo pipefail

DEST="$HOME/www/python"
BOOT_SCRIPT="$DEST/bootstrap-venv.sh"
trap 'rm -f "$BOOT_SCRIPT"' EXIT

[ -f web/dist/index.html ] || { echo "web/dist/index.html absent : lancez le build frontend avant install.sh." >&2; exit 1; }

mkdir -p "$DEST"
rm -rf "$DEST/src.new" && mkdir "$DEST/src.new"
cp -r app.py deadlinkfix requirements.txt deploy "$DEST/src.new/"
rm -rf "$DEST/src.new/deadlinkfix/__pycache__"
mkdir -p "$DEST/src.new/web"
cp -r web/dist "$DEST/src.new/web/"
rm -rf "$DEST/src.old"; [ -d "$DEST/src" ] && mv "$DEST/src" "$DEST/src.old"
mv "$DEST/src.new" "$DEST/src"
chmod +x "$DEST/src/deploy/run-worker.sh"
for f in "$DEST/src/deploy"/*.sh; do sed -i 's/\r$//' "$f"; done

# Le venv est créé depuis un job python3.11 : même environnement d'exécution que le worker.
cat > "$BOOT_SCRIPT" <<'EOF'
#!/bin/bash
set -euo pipefail

ROOT="${TOOL_DATA_DIR:-$PWD}"
DEST="$ROOT/www/python"
cd "$DEST"

rm -rf venv.new venv.old
python3 -m venv venv.new
venv.new/bin/pip install -q --upgrade pip wheel
venv.new/bin/pip install -q -r src/requirements.txt
venv.new/bin/python - <<'PY'
import flask, requests, dotenv, gunicorn
PY

if [[ -d venv ]]; then
    mv venv venv.old
fi
mv venv.new venv
rm -rf venv.old
EOF
sed -i 's/\r$//' "$BOOT_SCRIPT"
chmod +x "$BOOT_SCRIPT"

BOOT_JOB="ovixbot-bootstrap-venv-$(date +%s)"
toolforge jobs run "$BOOT_JOB" --image python3.11 --command "./www/python/bootstrap-venv.sh" --wait 900 --emails onfailure

echo "Installation terminée. Étapes suivantes : voir README (variables d'environnement, webservice start, jobs load)."
