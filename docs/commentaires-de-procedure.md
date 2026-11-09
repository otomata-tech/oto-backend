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
