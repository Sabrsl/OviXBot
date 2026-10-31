# OviXBot

Robot de maintenance pour Wikipédia. Il repère les liens morts dans les références et les répare en ajoutant une version archivée valide.

## Ce qu'il fait

- Repère les liens morts (erreurs 404, 410, 5xx, fausses pages d'erreur, sites injoignables)
- Ajoute `brisé le`, `archive-url` et `archive-date` aux références existantes
- Complète les champs manquants (titre, site, éditeur) à partir de l'archive, si l'option est activée

Il ne touche pas aux `{{Ouvrage}}`, aux réseaux sociaux, aux liens déjà archivés ni aux liens situés hors des `<ref>`.

## Modes

| Mode | Fonctionnement |
|---|---|
| Simulation | Analyse et propose, n'écrit jamais |
| Supervisé | Chaque modification attend votre validation |
| Autonome | Publie seul (nécessite le drapeau bot et une confirmation explicite) |

Sans drapeau bot, le mode autonome est automatiquement ramené en supervisé.

## Arrêt d'urgence

Tout utilisateur peut arrêter le robot en ouvrant une section « Arrêt d'urgence » sur sa page de discussion. Le robot la consulte toutes les 20 secondes et s'arrête aussitôt. La reprise se fait uniquement à la main, depuis le tableau de bord.

## Installation locale

Python 3.9 ou plus récent.

    # Windows
    run-local.bat

    # Linux / macOS
    ./run-local.sh

Le tableau de bord s'ouvre sur http://localhost:8000 (jeton : `local`).

## Configuration

Les paramètres se règlent dans un fichier `.env` (modèle : `.env.example`) ou dans les variables d'environnement.

| Variable | Rôle |
|---|---|
| `DRF_BOT_USER` | Identifiant du mot de passe de robot (ex. `OviXBot@OviX`) |
| `DRF_BOT_PASSWORD` | Mot de passe de robot (Spécial:MotsDePasseDeRobot) |
| `DRF_ACCOUNT` | Nom du compte (déduit de `DRF_BOT_USER` par défaut) |
| `DRF_ADMIN_TOKEN` | Jeton d'accès au tableau de bord |
| `DRF_SECRET` | Clé de session |
| `DRF_DATA_DIR` | Dossier de la base de données locale |

## Sécurité

- Délai minimal entre deux modifications
- Quotas par heure et par jour
- Respect des modèles `{{nobots}}` et `{{bots}}`
- Vérification avant chaque publication : l'article ne doit pas avoir changé depuis l'analyse
- Arrêt automatique si le compte est bloqué ou si les erreurs se répètent