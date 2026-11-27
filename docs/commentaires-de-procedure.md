# Commentaires de procédure

Des clients veulent laisser des remarques sur une page de process (« cette étape ne marche
pas chez nous », « ajouter le cas X »). Les commentaires vivent **à côté** de la procédure,
jamais dedans.

## Le contrat

- Table `procedure_comments` (`db/schema/procedures.py::COMMENTAIRES`) : `instruction_id`
  = l'id STABLE de la procédure (`guide_id`, survit au renommage), `version` = la version
  courante au moment du commentaire, `auteur_sub`, `body` (1 à 5 000 caractères),
  `deleted_at` (retrait doux). Lien logique sans FK, comme `process_shares`.
- Fils à la Notion : un commentaire RACINE porte `quote`, le passage du corps qu'il
  commente (vide = toute la procédure), et `quote_prefix` (≤ 64 caractères avant, pour
  départager deux occurrences) ; une RÉPONSE porte `reply_to` (un seul niveau, sans
  citation). Le passage n'est qu'une ancre d'affichage : le front le retrouve dans le
  corps ; un corps réécrit qui ne le contient plus range le fil « sur une version
  antérieure », sans rien bloquer. Retirer un commentaire ne retire que lui : une racine retirée dont le fil a
  encore des réponses reste servie `deleted: true` (sans texte ni auteur), et le fil disparaît
  avec sa dernière réponse.
- Capacités (`capabilities/commentaires_procedure.py`) :
  - `me.procedure.comments` — MCP `oto_procedure_comments`, `GET /api/me/processes/{guide_id}/comments`
    (`limit`, `before_id`, `total`) ;
  - `me.procedure.comment` — MCP `oto_procedure_comment`, `POST …/comments` (201) ;
  - `me.procedure.delete_comment` — REST seule, `DELETE …/comments/{comment_id}`.
  Sur la face MCP, la procédure se désigne par `guide_id` OU par `slug` (même cascade
  que `oto_procedure op=get` : copie perso en service, puis l'org ; `scope` pour forcer).
- Accès : le droit de LECTURE de la procédure (`ownership.can_access` sur `TYPE_RESSOURCE_PROCEDURE`)
  suffit pour lire et écrire. Retirer : l'auteur, ou qui gouverne la procédure. Illisible
  = absente = `404 procedure_not_found`.

## Optionnels, par construction

Le but est qu'un commentaire ne freine jamais l'adoption ni la mise à jour d'un process :

- commenter ne crée **aucune version** ;
- **aucune** écriture de procédure (`op=set`, revert, archive, publication, copie, fork)
  ne lit, n'exige ou ne vérifie un commentaire ; il n'y a rien à « résoudre » ;
- un commentaire posé sur la v3 reste lisible, étiqueté v3, quand la procédure est en v5 ;
- aucune notification n'est émise.

## Jamais lus pour exécuter

- `oto_procedure op=get` (les deux faces) ne sert **ni les commentaires, ni leur
  nombre** : l'agent qui lit sa procédure pour la suivre ne sait pas qu'il y en a, et un
  texte libre posé par un lecteur n'entre pas dans sa consigne.
- Sous un jeton de délégation (agent hébergé en train de travailler), lire ET écrire est
  refusé : `403 comments_outside_job`.
- Ils se lisent sur DEMANDE : la page du process (une personne), ou un agent de
  conversation à qui l'on demande de les relire. Le copilote lit sans demander
  (`copilot/classement.OUTILS_EN_LECTURE`) ; poster demande, comme pour une ligne.
- Le lien `/p/<token>` (vitrine ou lecteur connecté), la bibliothèque et la copie n'en
  servent aucun.

Tests : `tests/test_commentaires_procedure.py`.

## Pages de mémoire (20/11/2026)

Les pages de mémoire (docs) ont les mêmes commentaires, à l'identique :
`GET|POST /api/me/docs/{doc_id}/comments`, `DELETE …/comments/{comment_id}`
(`capabilities/commentaires_doc.py`, table `doc_comments`, `db/doc_commentaires.py`).

- **Même forme servie** que pour une procédure — fils, `quote`/`quote_prefix`,
  `reply_to`, racine retirée servie `deleted: true` tant qu'elle a des réponses — sauf
  `version`, toujours `null` (une page n'a pas de numéro de version) ; la réponse de
  lecture porte `doc_id` (pas de `slug` ni de `version`).
- **Accès** : lire ET écrire = lire la page (`docs.common.acces_a_la_page` : par son
  projet, ou par un partage de CETTE page) ; une page illisible est un `404
  doc_not_found`. Retirer : l'auteur, ou qui gouverne la page (son projet).
- **Pas une écriture de la page** : ni révision, ni `updated_at`. Une page supprimée
  emporte ses commentaires (FK en cascade).
- **REST seule** : aucun outil agent (un agent lit et écrit la page par `oto_doc`).
  Sous un jeton de délégation, refusés comme ceux d'une procédure
  (`403 comments_outside_job`). Le lien public d'une page n'en sert aucun.

Tests : `tests/test_commentaires_doc.py`.

## Suggestions (26/11/2026)

Une **suggestion** est un commentaire racine qui cite un passage (`quote`) et propose le
texte qui le remplacerait (`suggestion` ; `""` = retirer le passage). Le mode
« suggestion » d'un traitement de texte, porté par les fils existants : une équipe
propose un changement sans écrire la procédure ou la page, et qui peut l'écrire décide.

- **Colonnes** `suggestion`, `resolution` (`accepted` | `rejected`), `resolved_by`,
  `resolved_at` sur `procedure_comments` et `doc_comments`
  (`db/suggestions_commentaires.py`, révision `20261126_1200_suggestions`, posées aussi au
  démarrage). NULL = un commentaire ordinaire.
- **Poser** : `POST …/comments` avec `quote` + `suggestion` ; `body` devient un mot
  facultatif. Refusé sans `quote`, avec `reply_to`, ou identique au passage. Qui lit
  peut suggérer (même accès que commenter) ; l'outil agent `oto_procedure_comment`
  l'accepte aussi.
- **Résoudre** : `POST …/comments/{comment_id}/resolve` `{resolution}` (REST seule), une
  seule fois (`409 suggestion_not_pending`). Droit : **écrire** le texte — pour une
  procédure, la règle de `op=set` à son palier (la sienne ; l'équipe en membre ; l'org en
  admin) ou la gouverner ; pour une page, écrire son projet. Sinon
  `403 cannot_resolve_suggestion`. Le responsable (`owner_sub`) reste un nom, pas un droit.
- **Résoudre n'écrit rien.** Le passage est une ancre du texte affiché, pas du markdown :
  l'application remplace le passage dans son éditeur, enregistre la nouvelle version par
  le chemin ordinaire (qui garde ses propres droits), PUIS marque la suggestion acceptée.
  Aucune écriture ne lit, n'exige ni ne vérifie une suggestion en attente.
- **Servi** : chaque commentaire porte `suggestion`, `resolution`, `resolved_by`(`_email`),
  `resolved_at`, `can_resolve` ; la lecture porte `suggestions: true` (un serveur plus
  ancien l'omet — et rangerait une suggestion comme un simple commentaire : l'application
  n'offre « Suggérer » que si elle le lit) et `can_edit`.
- **Texte tiers** : sur la face agent, `suggestion` est servi enclos (`texte_tiers.cloture`,
  balise `suggested-text`) ; la face REST le rend tel quel.

Tests : `tests/test_suggestions_commentaires.py`, `tests/test_migration_20261126_suggestions.py`.
