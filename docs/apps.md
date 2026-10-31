# Apps — des écrans bâtis sur la plateforme

Une **app** est un écran qu'un utilisateur (ou un agent, pour lui) bâtit sur ses
tableaux et ses agents : tableau de bord, file de revue, formulaire, dépôt de fichier
qui lance un agent. Capacité `me.app` : outil MCP `oto_apps`, REST `POST /api/me/apps`
(une route, un `op`, comme `/api/me/docs`).

## Ce qu'est une app

- **Un manifeste** (JSONB) : les tableaux et champs qu'elle lit, les valeurs qu'elle
  peut poser sur un champ (`set`), les colonnes qu'un formulaire peut remplir (`add`),
  les agents qu'elle lit — ou lance, avec `run: true` (et `input: true` pour leur
  passer un texte) —, `files: true` pour déposer des fichiers dans l'app.
- **Du HTML**, exécuté par le front dans un cadre isolé (origine opaque, sans réseau,
  derrière un document-garde qui interdit toute navigation sortante), qui ne parle à
  la plateforme que par le pont exposé sur `window.oto` (`oto.rows()`, `oto.set()`,
  `oto.upload()`, `oto.files()`, `oto.runAgent()`…). Le serveur ne l'exécute jamais.
- **Une vignette** (image publiée sur le stockage média, ou couleur + emoji, et les
  réglages du dessin qui l'a produite). ⚠️ L'image doit être sous la base publique
  du stockage média de l'instance (`media_store.public_base()`) : la liste l'affiche
  hors du cadre isolé, une adresse libre ferait appeler un tiers par chaque lecteur.

Ce **n'est pas une page de mémoire** : ni dans l'arbre des pages, ni dans la base de
connaissance ; `oto_search` la trouve par son TITRE seul (kind `app`, celles que
`op=list` rend : « cherchable ⇔ lisible »), jamais par son HTML. Tables `apps`, `app_revisions`,
`app_projects`, `app_consents`, `app_files` (`db/schema/apps.py`) ; lectures et écritures dans
`db/apps.py` (`db.apps.<fn>`).

## À qui est une app, qui l'ouvre

**Une app a son propriétaire et ses partages, comme un tableau** — kind `app`
d'`ownership` (`apps.owner_type/owner_id`, partages dans `resource_grants`,
`resource_type='app'`). Décision du 28/10/2026 :

- **Une app neuve appartient à qui la crée, dans l'org active** (`context_org_id`),
  **privée** — sauf si un admin de l'org a réglé le partage par défaut des apps
  (`oto_org_settings domain=partage_par_defaut`, type `app` : `aucun` | `lecture` |
  `ecriture`) ; ce partage d'office est MARQUÉ et se retire seul. `ecriture` est
  permis pour une app (refusé pour un agent) : une app n'a pas d'identité à elle,
  chaque geste agit au nom de son lecteur, borné par son auteur.
- **Lecteur / éditeur / gérant**, les rôles d'ADR 0048, par `oto_resource`
  (`resource_type=app` : `get`, `share`, `unshare`, `transfer`) — la même surface et le
  même écran « Partager » qu'un projet. Lire l'app = `can_access` lecture (l'ouvrir,
  s'en servir) ; la modifier = écriture ; la repartager = gouvernance (propriétaire,
  admin, gérant). Transférer l'app à l'org ou à une équipe la rend à tous leurs
  membres.
- **L'org d'une app** (`apps_manifeste.org_de_l_app`) est celle de son propriétaire :
  l'org, l'org de l'équipe, et pour une app perso l'org où elle a été créée (sinon
  l'org perso de la personne). C'est là que vivent ses tableaux et ses agents, et là
  qu'elle se liste.
- **Un projet RANGE une app, il ne l'ouvre pas** (`app_projects`, 0..n projets) :
  `op=file` / `op=unfile` (écrire l'app ET le projet ; des écritures pour la garde de
  lecture seule) l'affichent sur la page du projet ; une app rangée dans un projet
  qu'on lit reste invisible si elle n'est pas partagée. Une carte ne nomme
  (`project_ids`) que les projets que son lecteur LIT : ranger ne révèle pas un projet.
- **`op=list`** sans projet : les apps de l'org active — possédées par l'org ou ses
  équipes, mes apps perso créées ici, partagées à l'un des principals de la liste
  (mêmes seams que la liste des projets, « listé ⇔ lisible »). Avec `project_ids` :
  celles qui y sont rangées et que je lis. Chaque carte porte `project_ids` et
  `can_edit`.
- **Migration** (`20261030_1200_apps_owned`) : les apps d'avant prennent le
  propriétaire et l'org de leur projet, y restent rangées, et reçoivent une copie des
  partages du projet — personne ne perd l'accès qu'il avait. ⚠️ **Entre le tag et
  `migrer upgrade head`, les apps sont invisibles** (fail-closed : sans propriétaire,
  personne ne les lit) — le démarrage ne joue que le DDL : jouer la migration juste
  après le tag. Index `apps (owner_id)` et `app_projects (filed_by)` : la fusion de
  comptes repointe ces colonnes d'un bloc.

## Ce que le serveur juge

- **À l'écriture** (`capabilities/apps_manifeste.py`) — `create`, `update` ET
  `restore` : la forme du manifeste (mêmes règles que le front, au plus 32 000
  caractères de JSON), puis que chaque tableau déclaré se résout **pour l'auteur**,
  que chaque champ déclaré existe dans son schéma, et que chaque tableau et chaque
  agent **vivent dans l'org de l'app** (`org_de_l_app`) — jamais dans l'org active de
  l'auteur ni du lecteur. Un manifeste qui nomme ce que l'auteur ne voit pas,
  ou ce qui vit ailleurs, est refusé, 400 `invalid_manifest`, chaque problème listé ;
  `op=validate` (`app_id`, sinon l'org active ; `html` optionnel) rend la liste sans
  rien écrire.
- **Le HTML se LIT à l'écriture, sans refuser** (`capabilities/apps_html.py`) —
  `create`, `update`, `patch`, `restore` et `validate` (qui prend aussi `html`) rendent
  `warnings` : chaque appel `oto.rows` / `oto.aggregate` écrit en clair (le pont
  sous un autre nom compris, `const T = window.oto`) qui trie, filtre, regroupe,
  mesure ou cherche hors de ce que le manifeste déclare, avec sa ligne. Ce sont
  exactement les appels que l'app ouverte se verrait refuser (« An app can only sort
  on a field it declares. ») devant son premier lecteur : l'agent qui écrit les
  corrige tout de suite. Seul le contenu des `<script>` est lu, commentaires blanchis ;
  un appel par variable n'est pas jugé, un `orderBy` / `groupBy` hors appel l'est
  contre tous les tableaux. Avertir, jamais refuser : c'est une lecture du texte, pas
  une exécution. Les avertissements se calculent AVANT l'écriture (un défaut du lecteur
  refuse sans rien écrire, jamais après une création commitée), et la lecture est
  bornée (`MAX_APPELS` appels examinés, `MAX_ARGS` caractères d'arguments par appel).
- **`op=patch` édite le HTML sans le réémettre** (`capabilities/remplacements.py`,
  moteur partagé) : `edits = [{find, replace, all?}]`, appliqués dans l'ordre, tout ou
  rien ; chaque `find` exact et unique (deux occurrences qui se chevauchent sont
  ambiguës aussi ; sinon `all: true`), absent ou ambigu → 400 `invalid_edits` qui
  nomme l'edit, rien d'écrit ; la longueur que le HTML AURAIT est jugée avant chaque
  remplacement (au plus 512 000 caractères). Mêmes gardes que `update` : bêta,
  écriture sur l'app, auteur qui lit ce que l'app déclare (→ 403 `would_break_app`),
  révision ; `expected_rev` → 409, et sans `expected_rev`, la révision LUE (une
  écriture concurrente n'est jamais écrasée sans un mot). Il rend un **reçu** (`rev`,
  `replaced`, `warnings`), jamais le HTML : l'op existe pour qu'un agent change une
  ligne d'une app de 40 000 caractères en deux cents jetons, au lieu de la réécrire
  entière — ce qui coupait l'agent intégré à `max_tokens`.
- **Chaque tableau déclaré est rangé par son NUMÉRO** (résolu à l'écriture pour
  l'auteur) : un nom résoudrait dans l'org active de qui le lit.
- **`set`, `add`, `agents` bornent les gestes** de l'appel d'une app (plus bas).

## Qui écrit une app

- **La population bêta seule** : `create`, `update`, `patch`, `archive`, `unarchive`,
  `restore`, `file`, `unfile` exigent l'option `beta` (`access.has_option`, dans l'org de l'appel) — la
  même population que `oto_apps` (`BETA_TOOLS`) et l'écran du front. Les autres :
  403 `apps_beta_only`, sur les deux faces ; une panne du seam des options refuse
  (fail-closed). Lire (`list`, `get`, `revisions`, `validate`) reste ouvert à qui
  lit l'app.
- **REST : session interactive seule** (`allow_api_token=False` sur `/api/me/apps`
  et sur l'appel) : un jeton `oto_`, délégation d'agent hébergé comprise, est refusé
  en 403 `api_token_forbidden`. Un agent écrit une app par `oto_apps`, en MCP.

## Une app ouverte — auteur ∩ lecteur (`me.app.call`)

Une app ouverte ne parle qu'à `POST /api/me/apps/{app_id}/call` (face REST seule, pas
d'outil MCP : un agent n'est jamais le lecteur d'une app). **Actions servies :**
`read_rows`, `read_aggregate` (lire), `write_cell`, `add_row`, `start_agent`,
`upload_file` (gestes, avec consentement), `list_files`, `agent_runs`, `run_result`.

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

### Écrire, ajouter, lancer, déposer — l'accord du lecteur, donné hors de l'app (`me.app.consent`, #1386)

**Le clic dans l'app VAUT accord, jugé par l'hôte** (décision du 31/10/2026, qui remplace
celle du 29/10). Le serveur ne distingue pas un clic du lecteur d'un appel que l'app fait
seule : c'est l'écran hôte qui en juge, sur l'activation navigateur. Un geste qui écrit ou
lance (`write_cell`, `add_row`, `start_agent`, `upload_file`) exige toujours un **jeton de
consentement**, que seul l'écran hôte obtient :

1. l'app DEMANDE le geste à l'écran hôte (`postMessage`) ;
2. l'hôte appelle `POST /api/me/apps/{app_id}/consent` avec le MÊME corps que l'appel ;
   le serveur juge le geste exactement comme l'appel le jugera (manifeste, auteur ∩
   lecteur, valeur déclarée, révision présente, plafonds…), puis rend `consent`
   (`secrets.token_urlsafe(32)`, stocké en SHA-256 dans `app_consents`), `expires_in`,
   `gestures` (toujours 1) et **`summary`**, construit par le serveur à partir des
   arguments jugés : `action`, `app_id`, `app_title`, `target` (tableau et ligne,
   agent, ou l'app), `content` (ce qui est ÉCRIT ou remis : `{set: {champ: valeur}}`,
   `{row: {colonne: valeur…}}`, `{input, files: [{id, name, media_type, size}]}`,
   `{file: {name, media_type, size}}`) et `text`, les mêmes faits en une phrase ;
3. sur un clic récent dans l'app, l'hôte utilise le jeton aussitôt ; sinon il AFFICHE ce
   résumé dans son propre chrome et attend un clic de confirmation. Puis il exécute
   l'appel (`POST …/call` avec `consent`, ou le dépôt `POST …/files`) ;
4. le serveur rejuge, BRÛLE le jeton en une écriture (`UPDATE … WHERE used_at IS NULL
   AND expires_at > now() RETURNING id` : deux appels concurrents ne le brûlent pas deux
   fois ; `consent_invalid` s'il est brûlé, expiré, d'un autre lecteur ou d'autres
   arguments ; `consent_required` sans jeton), puis agit.

**Un jeton = un geste** (`GESTES_PAR_ACCORD = 1`) : app, lecteur, action et empreinte
des arguments, calculée au serveur aux deux bouts. Valable **deux minutes, une fois** (15 minutes pour un dépôt, plus bas).

**L'émission n'est servie qu'à l'hôte** — 403 `consent_from_host_only` sinon :

- face REST seule (pas d'outil MCP), session interactive (JWT) : ni jeton `oto_` —
  porté, émetteur ou délégation d'agent hébergé —, ni secret de worker ;
- `Origin` présente et déclarée par l'instance (`OTO_MCP_CORS_ORIGINS`) : une requête
  sans origine, d'une origine tierce, ou d'origine **`null`** — celle d'une app dans
  son cadre isolé (`sandbox` sans `allow-same-origin`) — est refusée. Une app dont la
  CSP s'ouvrirait (`connect-src`) n'obtiendrait donc toujours pas de jeton elle-même ;
- pas une requête de cadre (`Sec-Fetch-Dest: iframe`, `frame`, `embed`, `object`).

### Contrat de l'écran hôte

L'écran hôte est le front qui embarque l'app (hors de l'iframe). Pour chaque geste
qu'une app demande :

1. **Recevoir** la demande par `postMessage` depuis l'iframe de CETTE app (vérifier
   `event.source`), sans rien exécuter.
2. **Demander le jeton** : `POST /api/me/apps/{app_id}/consent`, même corps que l'appel,
   depuis le chrome de l'hôte (son origine déclarée, sa session). Un refus (403, 400,
   413…) se montre au lecteur et se rend à l'app ; aucun jeton n'existe.
3. **Juger le clic** : le geste vaut accord si le navigateur montre une activation
   (`navigator.userActivation.isActive`), si le chrome de l'hôte n'a pas été touché
   depuis 5 secondes, et sous 20 gestes en 5 secondes. Sans activation (minuteur,
   ouverture de l'app), le geste est refusé.
4. **Sinon, demander** : afficher `summary` dans le chrome de l'hôte, jamais dans
   l'iframe, et attendre un clic de confirmation dans ce chrome. **Annuler** : n'utiliser
   aucun jeton (il expire en deux minutes), rendre un refus à l'app.
5. **Exécuter** l'appel (ou le dépôt) avec `consent`. Le jeton ne quitte pas l'hôte.
6. L'hôte n'affiche rien après un geste : l'app dessine son propre retour (`oto:done`,
   `oto.undo`, `oto:undone`).

**Ce que le serveur garantit** : l'app seule, dans son cadre isolé, n'obtient pas de
jeton (origine `null`, pas de réseau) ; un agent ou un script porteur de clé non plus ;
un jeton ne sert qu'une fois, deux minutes, au geste résumé — mêmes app, lecteur,
action, arguments ; le résumé décrit les arguments que le serveur a jugés et que l'appel
exécutera ; chaque geste est journalisé (`app_consents`).

**Ce qu'il ne peut pas garantir** : que le lecteur a voulu ce geste. Un clic dans une app
déclenche ce que son manifeste permet, en son nom, sans résumé affiché, y compris une
entrée remise à un agent. Le manifeste, auteur ∩ lecteur et le journal restent les bornes.

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

### Des fichiers pour les agents (`files: true`)

Une équipe dépose un PDF, un tableur, un document Word dans une app, et un agent le traite.

- **Déposer est un geste** : l'écran hôte demande un consentement
  (`POST …/consent`, `action=upload_file`, `file={name, size, media_type}`), affiche le
  résumé (nom, type, taille) et, sur confirmation, envoie le fichier en multipart à
  **`POST /api/me/apps/{app_id}/files`** (`file` + `consent`, route de nature
  `api/apps_files.py`, session interactive seule). Ce qui arrive doit répondre à ce qui
  a été annoncé (même nom, même taille, même type déclaré). Le corps est **borné avant
  d'être lu** : un `Content-Length` au-delà de 20 Mo (plus l'enveloppe multipart) est
  refusé 413 `content_too_large` sans rien lire, et la lecture en flux s'arrête au même
  plafond (`uploads.lire_multipart_borne`).
- **Le type se juge sur les octets** (`media_store.type_servi`) : ce qu'un agent sait
  LIRE — PDF, Word, Excel, texte, CSV, Markdown, JSON ; ni images ni PowerPoint (pas
  d'OCR, pas d'extracteur : décidé le 28/10/2026). 20 Mo au plus par fichier, **1 Go par
  lecteur et par app** (413 `app_storage_full`) et **5 Go par lecteur toutes apps
  confondues** (413 `viewer_storage_full`). Les plafonds sont tenus à l'INSERTION :
  compte et insertion dans une transaction, sous un verrou consultatif propre au
  lecteur (`db.apps.file_add`) — deux dépôts concurrents ne passent pas tous deux ; un
  fichier refusé là voit son objet retiré du stockage. Un fichier qui n'est pas ce qu'il dit,
  ou que le stockage neutraliserait (HTML, SVG, exécutable…), est refusé 415 `file_type`.
- Le jeton d'un dépôt vaut **15 minutes** (deux pour les autres gestes) : il se brûle
  quand les octets sont arrivés, et 20 Mo sur un lien lent dépassent deux minutes.
- **Rangé à part** : `app_files` (app, déposant, clé, nom, type, taille), octets sous
  `app-files/<app_id>/…` (clé propre à chaque dépôt). **Seul celui qui l'a déposé le
  lit**, et les runs qu'il lance — ils tournent en son nom. `list_files` (appel d'app)
  rend les siens.
- **Remis à un run** : `start_agent` prend `files` (5 au plus, de CETTE app, déposés par
  CE lecteur). Le run reçoit, après l'entrée de l'app, chaque fichier — nom, type,
  taille — et son TEXTE quand on sait l'extraire (PDF, Word, tableur, texte :
  `file_extract`), borné pour que tout tienne dans le `handoff` (20 000 caractères).
  Une URL signée n'y figure pas : un run peut attendre une place au-delà de son heure.
- **Rendu par l'agent** : `oto_apps op=put_file` (`app_id`, `name`, `content` texte ou
  `content_base64`, `media_type`) range un fichier RÉSULTAT dans l'app pour le lecteur au
  nom duquel le run tourne (`origin='agent'`, mêmes types, taille et plafond, pas de
  consentement : personne devant l'écran, et lui seul le lira ; une écriture pour la
  garde de lecture seule). Un CSV ou TSV rendu est **neutralisé** : une cellule qui
  commence par `=`, `+`, `-`, `@` (ou une tabulation, un retour) est préfixée d'une
  apostrophe, sauf un nombre signé (`-12,5`, `+33 6…`) — l'agent écrit souvent ce qu'il a
  lu ailleurs, et le lecteur ouvre le fichier dans un tableur. L'app le liste
  (`list_files`, `origin`) et le fait télécharger : action `file_url` → lien signé de
  5 minutes, `Content-Disposition: attachment`. Hors de la porte bêta des écritures :
  elle borne qui écrit une app, pas ce qu'un de ses lecteurs y range.
- **Lu en entier par l'agent** : `oto_apps op=read_file` (`app_id`, `file_id`, une
  lecture pour la garde de lecture seule) rend le
  fichier comme les pièces jointes Gmail ou Drive (`file_content.render_for_agent`) :
  texte d'un PDF ou tableur en CSV inline, plus un lien signé vers l'original.
  ⚠️ `oto_apps` est un outil bêta : l'agent doit l'avoir dans ses `tools`, sinon il
  n'a que le texte remis (borné) — `missing_tools` le dit à la pose.
- **Rétention : 30 jours** (décidé le 28/10/2026, comme les pièces jointes du copilote,
  oto-backend#1420) — `oto-mcp maintenance app-files`, dans le timer quotidien : l'objet
  du stockage d'abord, puis la ligne, 5 000 au plus par passage. Un run déjà lancé a
  reçu le texte ; un `read_file` après la purge rend `unknown_file`. **Supprimer l'app
  ne laisse pas d'orphelin** : `app_files.app_id` est `ON DELETE SET NULL` (révisions
  `20261030_1300_app_files`, `20261030_1400_app_files_origin`) — la ligne reste, sans
  app, invisible, et la purge suivante retire son objet puis la ligne sans attendre les
  30 jours. Même famille que `copilot/pieces.py` : à converger.

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
  tous deux — statut, dates. L'erreur (`error`, le `last_error` du travail) et ce que
  l'agent a conclu (`outcome`, `note`) seulement pour les runs que le LECTEUR a lancés
  (`mine`) : l'erreur du run d'un autre peut citer ses données.
- `run_result` : un run que le lecteur a lancé, d'un agent déclaré ; sinon 404
  `unknown_run`.

## Révisions et conflits

`rev` est une ETag de contenu (titre, manifeste, HTML, vignette), comme celle des
pages. `update` et `patch` acceptent `expected_rev` et refusent en 409 `conflict` si l'app a changé.
Chaque mise à jour écrit d'abord l'état précédent dans `app_revisions` ; seules les
50 plus récentes sont gardées (`db.apps.MAX_REVISIONS`, purge dans la même
transaction). `restore` remet une révision (en écrivant à son tour l'état courant),
après avoir REJUGÉ son manifeste : celui qui restaure en devient l'auteur.

## Liste

`op=list` prend les `project_ids` dont lister les apps (au plus 200 — ceux que
l'écran tient déjà), juge l'accès à tous en une passe (`ownership.can_access_many`)
et rend des **cartes** : jamais le HTML, qui pèse des dizaines de Ko par app.
