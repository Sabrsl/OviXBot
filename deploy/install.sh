#!/bin/bash
# À exécuter SUR Toolforge, connecté au compte outil (`become ovixbot`), depuis le dossier du projet décompressé.
set -e
DEST="$HOME/www/python"
mkdir -p "$DEST"
rm -rf "$DEST/src.new" && mkdir "$DEST/src.new"
cp -r app.py deadreffix web requirements.txt deploy "$DEST/src.new/"
rm -rf "$DEST/src.old"; [ -d "$DEST/src" ] && mv "$DEST/src" "$DEST/src.old"
mv "$DEST/src.new" "$DEST/src"
chmod +x "$DEST/src/deploy/run-worker.sh"
# Vérifier que l'interface React a été construite localement
if [ ! -f "$DEST/src/web/dist/index.html" ]; then
    echo "ERREUR: web/dist/index.html absent. L'interface React doit être construite localement avant déploiement."
    echo "Exécutez 'cd web && npm install && npm run build' sur votre machine avant de créer l'archive."
    exit 1
fi
# environnement virtuel (créé dans l'image du webservice pour une version de Python identique)
webservice python3.11 shell -- bash -c "python3 -m venv $DEST/venv && $DEST/venv/bin/pip install -q --upgrade pip && $DEST/venv/bin/pip install -q -r $DEST/src/requirements.txt"
echo "Installation terminée. Étapes suivantes : voir README (variables d'environnement, webservice start, jobs load)."
