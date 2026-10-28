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
d'outil MCP : un agent n'est jamais le lecteur d'une app). **Actions servies :**
`read_rows`, `read_aggregate` (lire), `write_cell`, `add_row`, `start_agent` (gestes,
avec consentement), `agent_runs`, `run_result` (suivre les runs).

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

### Écrire, ajouter, lancer — le consentement du lecteur (`me.app.consent`, #1386)

Le serveur ne distingue pas un clic du lecteur d'un appel que l'app fait seule. Un
geste qui écrit ou lance (`write_cell`, `add_row`, `start_agent`) exige donc un
**jeton de consentement** :

1. le cadre PARENT (le front, hors de l'iframe, qui n'a pas de réseau) demande
   `POST /api/me/apps/{app_id}/consent` avec le MÊME corps que l'appel quand le
   lecteur vient d'agir dans l'app — un clic, une frappe, un fichier déposé :
   l'activation utilisateur du navigateur, transmise par l'iframe à la fenêtre parente
   et que l'app ne peut pas fabriquer. **Le clic est l'accord** (décision du
   28/10/2026). Un geste que l'app tente SEULE (à l'ouverture, sur un minuteur), ou
   au-delà de 20 gestes sur un même clic, demande d'abord confirmation au lecteur.
   Un clic dans l'écran de TULINA (« Ouvrir l'app », Annuler, la barre latérale) active
   aussi la fenêtre : il n'approuve rien — pendant 5 s après lui, un geste demande
   (d'où une confirmation pour le premier geste juste après l'ouverture ou une réponse
   à une confirmation : voulu). Et un lancement d'agent qui porte une **entrée écrite
   par l'app** la montre et demande TOUJOURS : le run tourne au nom du lecteur ;
2. le serveur juge le geste exactement comme l'appel le jugera (manifeste, auteur ∩
   lecteur, valeur déclarée, révision présente…), puis rend un jeton
   (`secrets.token_urlsafe(32)`, stocké en SHA-256 dans `app_consents`), valable
   **deux minutes, une fois, pour CE geste** : app, lecteur, action et empreinte des
   arguments, calculée au serveur aux deux bouts ;
3. l'appel porte `consent` ; le serveur rejuge, BRÛLE le jeton en une écriture
   (`consent_invalid` s'il est brûlé, expiré, d'un autre lecteur ou d'autres
   arguments ; `consent_required` sans jeton), puis agit.

- `write_cell` : un champ déclaré (`set`), à une valeur déclarée, avec la `_revision`
  montrée (sinon 400 `revision_required`) ; l'auteur doit pouvoir ÉCRIRE le tableau.
- `add_row` : colonnes déclarées (`add`) seules.
- `start_agent` : un agent déclaré `run: true`, dans l'org du PROJET de l'app, au nom
  du lecteur par `run_now` (accès en écriture du lecteur, abonnement, modèle, clé) ;
  l'auteur doit pouvoir écrire l'agent. L'entrée (`input: true`, ≤ 300 caractères)
  arrive au run comme donnée venue de l'app, après sa consigne. **Dédupliqué même
  avec une entrée** (409 `run_already_queued`), ce que `run_now` ne fait pas pour un
  `handoff`.
- **Journal** : la ligne de `app_consents` reste une fois brûlée — qui, quelle app,
  quelle action, quand, le `geste_id` des révisions écrites
  (`datastore_row_revisions.geste_id`), le travail lancé (`job_id`). C'est là, pas
  dans `tool_calls` (route réduite), que se lit « cette écriture vient de l'app N ».

### Ce que le cadre isolé laisse sortir

Le cadre de l'app n'appelle rien (`connect-src 'none'`, formulaires et navigations
fermés par le document-garde). Fermé en plus, avant toute ligne de l'auteur, parce
qu'aucune directive CSP ne le fait dans les trois moteurs (mesuré le 28/10/2026) :
WebRTC (constructeurs retirés — `webrtc 'block'` est ignoré, une requête STUN sortait),
le prefetch DNS, et `<link rel=preconnect>` (WebKit ouvrait une connexion). Restent,
par construction : les chemins de requête vers les CDN permis et Google Fonts (une
requête peut y porter des données, mais vers le CDN seul), et ce qu'un LECTEUR tape
dans un faux formulaire d'une app — seule donnée que l'auteur n'a pas déjà.

### Suivre les runs

- `agent_runs` : les derniers runs d'un agent déclaré que l'auteur et le lecteur voient
  tous deux — statut, dates, erreur, comme l'écran de l'agent les montre au lecteur.
  Ce que l'agent a conclu (`outcome`, `note`) seulement pour les runs que le LECTEUR a
  lancés (`mine`).
- `run_result` : un run que le lecteur a lancé, d'un agent déclaré ; sinon 404
  `unknown_run`.

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
