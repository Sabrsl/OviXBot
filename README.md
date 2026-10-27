# OviXBot

Bot de réparation de liens morts sur Wikipédia en français. Il détecte les références `<ref>` avec des liens brisés et ajoute automatiquement les archives disponibles.

## Ce que fait le bot

- Détecte les liens morts (HTTP 404, 410, 5xx, soft 404, domaines injoignables)
- Convertit les URLs brutes en `{{Lien web}}` ou `{{Article}}`
- Ajoute `brisé le`, `archive-url`, `archive-date` aux références existantes
- Complète les champs manquants depuis l'archive (titre, site, éditeur) si activé
- Ne touche PAS aux `{{Ouvrage}}`, réseaux sociaux, liens déjà archivés, ou hors `<ref>`

## Modes de fonctionnement

| Mode | Description |
|------|-------------|
| **Simulation** | Analyse et propose, n'écrit jamais |
| **Supervisé** | Chaque modification attend votre validation |
| **Autonome** | Publie seul (drapeau bot requis) |

## Arrêt d'urgence

N'importe qui peut arrêter le robot en ajoutant une section « Arrêt d'urgence » sur sa page de discussion. Le vérifie toutes les 20 secondes et s'arrête immédiatement. La reprise n'est possible que manuellement.

## Installation locale

Python 3.9+ requis.

```bash
# Windows
run-local.bat

# Linux/macOS
./run-local.sh
```

Le tableau de bord s'ouvre sur http://localhost:8000 (jeton : `local`).

## Configuration

Les identifiants se configurent via variables d'environnement ou fichier `.env` :

```bash
DRF_BOT_USER=Compte@Bot
DRF_BOT_PASSWORD=mot_de_passe_robot
DRF_ACCOUNT=NomDuCompte
DRF_ADMIN_TOKEN=jeton_admin
```

## Sécurité

- Intervalles minimaux entre modifications
- Quotas par heure et par jour
- Respect de `{{nobots}}` et `{{bots}}`
- Vérification avant chaque publication (article non modifié entre-temps)
- Arrêt automatique si compte bloqué ou erreurs répétées
