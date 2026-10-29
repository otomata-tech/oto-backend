# Apps — des écrans bâtis sur la plateforme, rangés dans un projet

Une **app** est un écran qu'un utilisateur (ou un agent, pour lui) bâtit sur ses
tableaux et ses agents : tableau de bord, file de revue, formulaire, dépôt de fichier
qui lance un agent. Capacité `me.app` : outil MCP `oto_apps`, REST `POST /api/me/apps`
(une route, un `op`, comme `/api/me/docs`).

## Ce qu'est une app

- **Un manifeste** (JSONB) : les tableaux et champs qu'elle lit, les valeurs qu'elle
  peut poser sur un champ (`set`), les colonnes qu'un formulaire peut remplir (`add`),
  les agents qu'elle lit — ou lance, avec `run: true` (et `input: true` pour leur
  passer un texte) —, `files: true` pour déposer des fichiers dans le projet.
- **Du HTML**, exécuté par le front dans un cadre isolé (origine opaque, sans réseau,
  derrière un document-garde qui interdit toute navigation sortante), qui ne parle à
  la plateforme que par le pont exposé sur `window`. Le serveur ne l'exécute jamais.
- **Une vignette** (image publiée sur le stockage média, ou couleur + emoji, et les
  réglages du dessin qui l'a produite). ⚠️ L'image doit être sous la base publique
  du stockage média de l'instance (`media_store.public_base()`) : la liste l'affiche
  hors du cadre isolé, une adresse libre ferait appeler un tiers par chaque lecteur.

Elle **appartient à un projet** et en hérite l'accès (`ownership.can_access`), comme une
page — mais ce **n'est pas une page de mémoire** : ni dans l'arbre des pages, ni dans
`oto_search`, ni dans la base de connaissance. Tables `apps` et `app_revisions`
(`db/schema/apps.py`) ; lectures et écritures dans `db/apps.py` (`db.apps.<fn>`).

## Ce que le serveur juge

- **À l'écriture** (`capabilities/apps_manifeste.py`) — `create`, `update` ET
  `restore` : la forme du manifeste (mêmes règles que le front, au plus 32 000
  caractères de JSON), puis que chaque tableau déclaré se résout **pour l'auteur**,
  que chaque champ déclaré existe dans son schéma, et que chaque tableau et chaque
  agent **vivent dans l'org du projet de l'app** (`org_du_projet` ; un projet perso
  sans org de rangement vit dans l'org perso de son propriétaire) — jamais dans l'org
  active de l'auteur ni du lecteur. Un manifeste qui nomme ce que l'auteur ne voit pas,
  ou ce qui vit ailleurs, est refusé, 400 `invalid_manifest`, chaque problème listé ;
  `op=validate` (`project_id` ou `app_id`) rend la liste sans rien écrire.
- **Le HTML se LIT à l'écriture, sans refuser** (`capabilities/apps_html.py`) —
  `create`, `update`, `restore` et `validate` (qui prend aussi `html`) rendent
  `warnings` : chaque appel `tulina.rows` / `tulina.aggregate` écrit en clair (le pont
  sous un autre nom compris, `const T = window.tulina`) qui trie, filtre, regroupe,
  mesure ou cherche hors de ce que le manifeste déclare, avec sa ligne. Ce sont
  exactement les appels que l'app ouverte se verrait refuser (« An app can only sort
  on a field it declares. ») devant son premier lecteur : l'agent qui écrit les
  corrige tout de suite. Seul le contenu des `<script>` est lu, commentaires blanchis ;
  un appel par variable n'est pas jugé, un `orderBy` / `groupBy` hors appel l'est
  contre tous les tableaux. Avertir, jamais refuser : c'est une lecture du texte, pas
  une exécution.
- **`op=patch` édite le HTML sans le réémettre** (`capabilities/remplacements.py`,
  moteur partagé) : `edits = [{find, replace, all?}]`, appliqués dans l'ordre, tout ou rien ;
  chaque `find` exact et unique (sinon `all: true`), absent ou ambigu → 400 `invalid_edits`
  qui nomme l'edit, rien d'écrit. Même chemin d'écriture que `update` (révision, auteur,
  `expected_rev` → 409), et un **reçu** (`rev`, `replaced`, `warnings`), jamais le HTML : l'op
  existe pour qu'un agent change une ligne d'une app de 40 000 caractères en deux cents
  jetons, au lieu de la réécrire entière — ce qui coupait l'agent intégré à `max_tokens`.
- **Chaque tableau déclaré est rangé par son NUMÉRO** (résolu à l'écriture pour
  l'auteur) : un nom résoudrait dans l'org active de qui le lit.
- **`set`, `add`, `agents`, `files` sont jugés mais inertes** : rien ne les sert
  encore (cf. plus bas).

## Qui écrit une app

- **La population bêta seule** : `create`, `update`, `archive`, `unarchive`,
  `restore` exigent l'option `beta` (`access.has_option`, dans l'org de l'appel) — la
  même population que `oto_apps` (`BETA_TOOLS`) et l'écran du front. Les autres :
  403 `apps_beta_only`, sur les deux faces ; une panne du seam des options refuse
  (fail-closed). Lire (`list`, `get`, `revisions`, `validate`) reste ouvert à qui
  lit le projet.
- **REST : session interactive seule** (`allow_api_token=False` sur `/api/me/apps`
  et sur l'appel) : un jeton `oto_`, délégation d'agent hébergé comprise, est refusé
  en 403 `api_token_forbidden`. Un agent écrit une app par `oto_apps`, en MCP.

## Une app ouverte — auteur ∩ lecteur (`me.app.call`)

Une app ouverte ne parle qu'à `POST /api/me/apps/{app_id}/call` (face REST seule, pas
d'outil MCP : un agent n'est jamais le lecteur d'une app). **Actions servies :
`read_rows` et `read_aggregate`, rien d'autre** — une autre action est une entrée
invalide (400).

- **Droits d'une app = droits de son auteur ∩ droits de son lecteur.** L'auteur est
  `updated_by` ; il est jugé par l'ownership SEUL (numéro du tableau), jamais par sa
  session. Le lecteur est jugé en exécutant POUR LUI les capacités existantes (lignes,
  agrégat). Auteur inconnu ou qui a perdu l'accès : 403 `app_author_unknown` /
  `app_author_lost_access` ; tableau sorti de l'org du projet depuis l'écriture :
  403 `table_outside_project_org`.
- **Borné au manifeste, côté serveur** : projection aux champs déclarés (plus `_id`,
  `_revision`, les dates), filtre / tri / regroupement / mesure sur ces champs seuls,
  recherche plein texte seulement sans liste de champs. `limit` et `offset` sont des
  entiers (sinon 400 `invalid_options`), `limit` ≤ 200.
- **Route lourde** (`api/routes_lourdes.py`) : 240/min par jeton, 8 en parallèle — une
  app laissée ouverte relit ses tableaux en fond. L'appel relit le manifeste sans le
  HTML (`db.apps.get_for_call`).
- **Journal** : l'appel est tracé comme toute requête REST (`tool_calls`, `kind=rest`),
  mais la route y est réduite (`/api/me/apps/:id/call`) : le numéro de l'app et le
  tableau lu n'y sont pas. Aucun journal d'audit des lectures n'existe à ce jour.

### Ce qui n'est pas servi — et viendra avec un consentement

Écrire une valeur (`set`), ajouter une ligne (`add`), lancer un agent, lire ses runs
et ce qu'il a conclu : une app agirait au nom d'un lecteur qui ne l'a pas demandé.
Ces gestes viendront avec un jeton de consentement du lecteur (oto-backend#1386).
Le manifeste les déclare déjà, jugés à l'écriture, inertes à l'appel.

## Révisions et conflits

`rev` est une ETag de contenu (titre, manifeste, HTML, vignette), comme celle des
pages. `update` accepte `expected_rev` et refuse en 409 `conflict` si l'app a changé.
Chaque mise à jour écrit d'abord l'état précédent dans `app_revisions` ; seules les
50 plus récentes sont gardées (`db.apps.MAX_REVISIONS`, purge dans la même
transaction). `restore` remet une révision (en écrivant à son tour l'état courant),
après avoir REJUGÉ son manifeste : celui qui restaure en devient l'auteur.

## Liste

`op=list` prend les `project_ids` dont lister les apps (au plus 200 — ceux que
l'écran tient déjà), juge l'accès à tous en une passe (`ownership.can_access_many`)
et rend des **cartes** : jamais le HTML, qui pèse des dizaines de Ko par app.
