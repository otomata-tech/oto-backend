# Apps — des écrans bâtis sur la plateforme

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

Ce **n'est pas une page de mémoire** : ni dans l'arbre des pages, ni dans
`oto_search`, ni dans la base de connaissance. Tables `apps`, `app_revisions`,
`app_projects`, `app_consents` (`db/schema/apps.py`) ; lectures et écritures dans
`db/apps.py` (`db.apps.<fn>`).

## À qui est une app, qui l'ouvre

**Une app a son propriétaire et ses partages, comme un tableau** — kind `app`
d'`ownership` (`apps.owner_type/owner_id`, partages dans `resource_grants`,
`resource_type='app'`). Décidé avec Julien le 28/10/2026 :

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
  `op=file` / `op=unfile` (écrire l'app ET le projet) l'affichent sur la page du
  projet ; une app rangée dans un projet qu'on lit reste invisible si elle n'est pas
  partagée.
- **`op=list`** sans projet : les apps de l'org active — possédées par l'org ou ses
  équipes, mes apps perso créées ici, partagées à l'un des principals de la liste
  (mêmes seams que la liste des projets, « listé ⇔ lisible »). Avec `project_ids` :
  celles qui y sont rangées et que je lis. Chaque carte porte `project_ids` et
  `can_edit`.
- **Migration** (`20261028_1900_apps_owned`) : les apps d'avant prennent le
  propriétaire et l'org de leur projet, y restent rangées, et reçoivent une copie des
  partages du projet — personne ne perd l'accès qu'il avait.

## Ce que le serveur juge

- **À l'écriture** (`capabilities/apps_manifeste.py`) — `create`, `update` ET
  `restore` : la forme du manifeste (mêmes règles que le front, au plus 32 000
  caractères de JSON), puis que chaque tableau déclaré se résout **pour l'auteur**,
  que chaque champ déclaré existe dans son schéma, et que chaque tableau et chaque
  agent **vivent dans l'org de l'app** (`org_de_l_app`) — jamais dans l'org active de
  l'auteur ni du lecteur. Un manifeste qui nomme ce que l'auteur ne voit pas,
  ou ce qui vit ailleurs, est refusé, 400 `invalid_manifest`, chaque problème listé ;
  `op=validate` (`app_id`, sinon l'org active) rend la liste sans rien écrire.
- **Chaque tableau déclaré est rangé par son NUMÉRO** (résolu à l'écriture pour
  l'auteur) : un nom résoudrait dans l'org active de qui le lit.
- **`set`, `add`, `agents` bornent les gestes** de l'appel d'une app (plus bas).

## Qui écrit une app

- **La population bêta seule** : `create`, `update`, `archive`, `unarchive`,
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

### Des fichiers pour les agents (`files: true`)

Une équipe dépose un PDF, un tableur, un document Word dans une app, et un agent le traite.

- **Déposer est un geste** : le cadre parent demande un consentement
  (`POST …/consent`, `action=upload_file`, `file={name, size, media_type}`), puis envoie
  le fichier en multipart à **`POST /api/me/apps/{app_id}/files`** (`file` + `consent`,
  route de nature `api/apps_files.py`, session interactive seule). Ce qui arrive doit
  répondre à ce qui a été annoncé (même nom, même taille, même type déclaré).
- **Le type se juge sur les octets** (`media_store.type_servi`) : ce qu'un agent sait
  LIRE — PDF, Word, Excel, texte, CSV, Markdown, JSON ; ni images ni PowerPoint (pas
  d'OCR, pas d'extracteur : décidé le 28/10/2026). 20 Mo au plus par fichier, **1 Go par
  lecteur et par app** (413 `app_storage_full`). Un fichier qui n'est pas ce qu'il dit,
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
  consentement : personne devant l'écran, et lui seul le lira). L'app le liste
  (`list_files`, `origin`) et le fait télécharger : action `file_url` → lien signé de
  5 minutes, `Content-Disposition: attachment`. Hors de la porte bêta des écritures :
  elle borne qui écrit une app, pas ce qu'un de ses lecteurs y range.
- **Lu en entier par l'agent** : `oto_apps op=read_file` (`app_id`, `file_id`) rend le
  fichier comme les pièces jointes Gmail ou Drive (`file_content.render_for_agent`) :
  texte d'un PDF ou tableur en CSV inline, plus un lien signé vers l'original.
  ⚠️ `oto_apps` est un outil bêta : l'agent doit l'avoir dans ses `tools`, sinon il
  n'a que le texte remis (borné) — `missing_tools` le dit à la pose.
- **Rétention : 30 jours** (décidé le 28/10/2026, comme les pièces jointes du copilote,
  oto-backend#1420) — `oto-mcp maintenance app-files`, dans le timer quotidien : l'objet
  du stockage d'abord, puis la ligne, 5 000 au plus par passage. Un run déjà lancé a
  reçu le texte ; un `read_file` après la purge rend `unknown_file`. Supprimer l'app
  retire aussi les lignes (`ON DELETE CASCADE`). Même famille que `copilot/pieces.py` :
  à converger.

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
