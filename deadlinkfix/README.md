# DeadLinkFix — plugin AWB (frwiki)

Détecte, analyse et répare les liens externes morts **uniquement dans les références**
(`<ref>…</ref>`), pour les modèles `{{Lien web}}`, `{{Ouvrage}}` et `{{Article}}`.

## Philosophie

**Vérifier d'abord, réparer ensuite** :
1. Vérifier si le lien est mort (404, 410, etc.)
2. Si mort ET aucune archive présente : chercher une archive valide (Wayback, archive.ph)
3. Si archive validée (même ressource) : ajouter `archive-url`, `archive-date` (date de l'archive) et `brisé le` (date actuelle)
4. **NE JAMAIS** : modifier `consulté le`, remplacer sur simple erreur HTTP sans preuve

## Règles (garde-fous)

| Situation | Action |
|---|---|
| Lien vivant (2xx) | rien |
| Erreur temporaire / anti-robot : 401, 403, 408, 429, 5xx, timeout, TLS, boucle, hors-ligne | **rien** (état « inconnu ») |
| 404 / 410 confirmés avec un 2ᵉ User-Agent, ou domaine inexistant (DNS, connexion OK) | lien « mort » |
| Redirection vers l'accueil / page d'erreur du même site (soft 404) | lien « mort » |
| Mort + archive déjà présente | ajoute `|brisé le=<date>` dans le modèle (**`{{Lien web}}` uniquement**, jamais `{{Article}}`) |
| Mort + pas d'archive + capture trouvée et **validée** (même domaine, même lien) | ajoute `archive-url` / `archive-date` (`archiveurl` / `archivedate` pour `{{Article}}`) et, pour `{{Lien web}}` seulement, `|brisé le=<date>` |
| Mort + pas d'archive + pas de capture | rien |

* **« consulté le » n'est jamais lu, ajouté ni modifié.**
* Jamais de remplacement sur la seule base d'une erreur HTTP. Une preuve est requise (slug, identifiant, chemin identique).
* `{{Ouvrage}}` n'a pas de paramètres d'archive : seules les corrections par redirection s'y appliquent.
* Ignorés : commentaires, `<nowiki>`, `<code>`, `<pre>`…, modèles hors `<ref>`, URL contenant un modèle, un espace,
  un commentaire, paramètre `url` en double, hôtes locaux/privés, URL d'archives.
* Jusqu'à 50 liens distincts par article (réglable), vérifiés en parallèle (une file par hôte, 8 hôtes en parallèle).
* Idempotent : un second passage ne change rien. Aucune exception ne remonte vers AWB.

## Installation

1. Compiler (Windows, MSBuild / Visual Studio) : `build.cmd` — ou ouvrir `src\DeadLinkFix.Awb\DeadLinkFix.Awb.csproj`.
   La référence `WikiFunctions.dll` est cherchée en `..\..\..\..\WikiFunctions.dll` (la DLL doit être à la racine du projet AWB).
2. Copier `DeadLinkFix.Awb.dll` (+ `DeadLinkFix.ini`) dans le dossier **Plugins** d'AWB.
3. AWB → menu *Plugins* → activer **DeadLinkFix**.

## Réglages (`DeadLinkFix.ini`, optionnel)

`MaxLinksPerArticle`, `MaxParallelism`, `TimeoutSeconds`, `FixHttpToHttps`, `RepairRedirects`, `AddArchive`,
`MarkBrokenWithArchive`, `UseArchivePh`, `BrokenMarkerFormat`.

`BrokenMarkerFormat` vaut par défaut `{date}` — format utilisé pour la valeur du paramètre `brisé le`
(normalement `{date}` pour utiliser la date du jour).

## Tests

`cd tests/DeadLinkFix.Tests && dotnet run` — 70 tests (analyse wikicode, preuves, moteur, HTTP sur serveur local,
archives, plugin via doublure de `WikiFunctions.Plugin`).
