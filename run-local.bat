@echo off
rem Lancement local (Windows) : tableau de bord sur http://localhost:8000  — jeton : local
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv && .venv\Scripts\pip install -q -r requirements.txt )
set DRF_ADMIN_TOKEN=local
set DRF_SECRET=local-dev
set DRF_INSECURE_COOKIES=1
set DRF_EMBED_WORKER=1
set DRF_DATA_DIR=%~dp0data

echo.
echo ============================================
echo Lancement du serveur local...
echo Jeton: local
echo URL: http://localhost:8000
echo ============================================
echo.

echo Demarrage de Python...
.venv\Scripts\python app.py
if errorlevel 1 (
    echo.
    echo ERREUR: Le serveur n'a pas pu demarrer.
    echo Appuyez sur une touche pour fermer...
    pause >nul
)
