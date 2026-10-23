---
title: REST API (consommée par le tableau de bord)
type: reference
description: >-
  Inventaire des endpoints REST /api/* de oto-backend : profil /api/me (billing,
  onboarding, connecteurs), settings LinkedIn/API-keys/tools, guide org
  /api/me/instructions*, palier org (CRUD orgs, membres, secrets, invitations,
  entitlements datastore), admin users/grants/tokens/monitoring, billing Stripe,
  bibliothèque de guides (`/api/me/guide-library`, visibilité public/unlisted).
  Détaille les règles CORS (oto.ninja, app.oto.ninja, dashboard.oto.ninja), l'autz
  (même JWTVerifier ES384 que /mcp, audience mcp.oto.ninja), et les gotchas secrets
  (jamais la clé en réponse, providers per-user refusés en org secrets). À charger
  pour implémenter ou déboguer un endpoint REST ou comprendre le contrat front/back.
---

# REST API (consommée par le tableau de bord)

## Où vit chaque famille (découpe du 2026-08-27)

`api/routes.py` **assemble** et ne contient plus aucun handler : `make_routes` monte les
modules de routes, monte la couche capacité, et rend la table ordonnée des chemins écrits
à la main. L'ORDRE de cette table est un contrat — Starlette prend le PREMIER match, donc
`…/tools/registry` doit précéder `…/tools/{name}`. Elle est figée par
`tests/api/test_api_routes_table_frozen.py` : retirer, ajouter ou réordonner un chemin fait
rouge, et régénérer `tests/api/api_routes_table.txt` EST la déclaration de l'ajout.

✅ **La dette REST vaut ZÉRO depuis le 2026-08-27.** Les 38 routes écrites à la main
qu'il restait sont devenues des capacités en huit lots ; les **36 chemins encore montés
à la main sont tous de NATURE**, chacun avec sa raison écrite dans
`tests/test_rest_modules_are_capabilities.py::_KNOWN` — callback de redirection,
webhook, surface anonyme, API consommée par un programme externe, corps **multipart**,
réponse **non-JSON**. Ce n'est pas le domaine qui tranche, c'est la **forme** : sur
`/api/me/avatar`, le `DELETE` est une capacité et le `POST` multipart n'en sera jamais une.

Le garde-fou ne mesure donc plus une dette, il est devenu un **cliquet**
(`test_rest_debt_stays_at_zero`) : une route neuve écrite à la main a **deux issues, et
deux seulement** — naître capacité, ou être classée `NATURE` **avec sa raison**. La
classer `DEBT` rouvrirait ce qui vient d'être fermé : c'est un acte, pas un réglage.
Avec `test_no_new_handwritten_rest_route`, qui refuse toute route absente de `_KNOWN`,
il devient impossible d'ajouter une route à la main sans le déclarer.

| famille | où | régime |
| --- | --- | --- |
| ~200 chemins générés (**le compte** `/api/me`, **la toolbox** `/api/me/tools*`, **la session navigateur**, **la messagerie hébergée**, **les verbes du consentement OAuth per-user**, **les jetons API**, **les fichiers de projet**, projets, pages, procédures, ressources, orgs, guide, monitoring, datastore, billing…) | `capabilities/` + `_rest_adapter` | **capacité** : un descripteur, deux faces (ADR 0009/0042) |
| primitives (`_authenticate`, CORS, `_json`/`_json_error`, `OPTIONS`, `bind`) | `api/base.py` | partagées par tous les modules ; **ré-exportées** par `api.routes` |
| favicon, `/api/version`, `/api/mcp/catalog`, `openapi.json`, `/api/connectors`, bibliothèques de procédures & de guides, aperçu d'invitation, docs partagés (`/api/public/docs/{token}`, `/p/d/{token}`) | `api/public.py` | **sans auth** — l'adaptateur capacité authentifie toujours. ⚠️ `/api/connectors` est la seule **MIXTE** : anonyme pour la vitrine, authentifiée pour le dashboard, et depuis le 2026-09-01 (#732) l'en-tête **change ce qu'elle rend** — org de contexte ⟹ `auth.cardinality` effective |
| `POST /api/me/avatar`, `POST /api/orgs/{id}/logo` | `api/media.py` | **multipart** → hors du moule par CONSTRUCTION (classé `NATURE`) |
| `POST` d'un fichier de projet, `/api/me/projects/{id}/export` | `api/projects.py` | **multipart / ZIP** → hors du moule (classé `NATURE`) |
| `POST /api/me/projects/{id}/transcriptions/upload` (audio à transcrire) | `api/transcription.py` | **multipart** → hors du moule (classé `NATURE`) ; la forme par référence est une capacité |
| `/api/upload/{token}` (PUT/POST/GET) | `api/uploads.py` | **pas de JWT** : le jeton de l'URL fait foi |
| SIRENE (`api/sirene.py`), accords (`api/accords.py`), webhook Mollie (`api/billing.py`), **callbacks OAuth** zoho/google/salesforce (`api/{zoho,datastore,salesforce}.py`) | `api/<nom>.py` (antérieurs à la découpe) | gardent leur patron : `make_routes(...)` reçoit les primitives en paramètres. ⚠️ `api/atlassian.py` et `api/folk.py` ont porté les deux callbacks fédérés jusqu'au **2026-09-09** : partis avec la fédération MCP (**ADR 0069**). ⚠️ **Le datastore n'y est plus** depuis le 2026-08-12 (#302) : ses 24 chemins sont des capacités (bloc ci-dessous) ; `api/datastore.py` est un nom vestige qui ne porte QUE le callback Google |

- `GET /api/me` + `GET /api/me/calls` + `GET /api/me/activity-summary` — **le compte**,
  capacités `me.{get,calls,activity_summary}` depuis le 2026-08-27
  (`capabilities/me_account.py` ; `api_routes_account.py` supprimé). **Pas de face MCP** —
  l'identité d'une session MCP est `oto_whoami`, l'activité se lit par les lentilles de
  monitoring. `GET /api/me` rend profil + rôle plateforme + `providers` (accès effectif
  par connecteur : mode/clé/quota) + `features.billing` + le couple **`active_*` / `home_*`** :
  `active_org`/`active_group` sont les valeurs EFFECTIVES de la requête (seam `current_org`,
  ADR 0023 — la consultation `X-Oto-Org` l'emporte), `home_*` le défaut persistant. Un front
  qui scope ses vues sur `home_org` affiche les données d'une autre org que celle qu'il
  annonce. `active_org_readonly` = opérateur plateforme en consultation (bandeau + écran en
  lecture) ; `view_as_read_only` = « voir en tant que » appliqué (`X-Oto-View-As`) : la
  réponse est celle de la cible, dont `active_org_readonly` est faux, alors que toute
  écriture est refusée (403 `view_as_read_only`). Un écran n'écrit que si les DEUX sont
  faux (oto#212). Écriture en view-as ACCEPTÉE (`X-Oto-View-As-Write: 1`, super_admin
  seul, `docs/org-context.md`) : `view_as_read_only` vaut alors faux, jugé par le même
  `ViewAsMiddleware` que l'écriture ; un front qui pose ou retire l'en-tête relit `/api/me`. Les deux lentilles d'activité sont scopées **(sub, org active)** : jamais un
  autre membre, jamais une autre org — filtres `?limit=` (défaut 200, plafond dur 1000),
  `?tool=` (nom EXACT), `?errors=1|true` (littéral : `?errors=yes` ne filtre pas), `?days=`.
  ⚠️ **Le repli de saisie est conservé** : `?days=abc` rend 200 avec la fenêtre par défaut,
  jamais un 400 — c'est le comportement servi depuis toujours. En revanche un **paramètre
  inconnu** est désormais refusé (400 `unknown_fields`, champ nommé) là où il était ignoré
  en silence : c'est la garde de l'adaptateur, et c'est le seul écart visible de la migration.
- `GET /api/me/recent-changes` — **les dernières modifications** (oto#191), capacité
  `me.recent_changes` depuis le 2026-09-12 (`capabilities/recent_changes.py`, requête
  `db/recent_changes.py`) : la source de l'îlot « Dernières modifications » de l'accueil de
  dashboard.oto.cx. **Pas de face MCP.** Rend `{items, limit}` : pages et procédures
  **fusionnées**, triées par `updated_at` décroissant (départage `type`, puis `id`
  décroissant), coupées **en base après le tri**. `?limit=` : défaut 20, borné 1…50 — hors
  bornes (ou non entier) = **400 `invalid_input`**, jamais un plafond appliqué en silence ;
  un paramètre inconnu = 400 `unknown_fields`. Un élément : `type` (`doc` | `procedure`),
  `id` (page : la poignée `doc_id` de `POST /api/me/docs` ; procédure : l'id stable de
  `GET /api/me/guides/{guide_id}`), `title`, `project` `{id, name}` pour une page et `null`
  pour une procédure (elle appartient à un palier, pas à un projet), `slug` et `scope`
  (`user` | `org` | `group`, procédure seulement, `null` sinon), `author` `{sub, name}` ou
  `null`, `updated_at`. **Périmètre = la lecture, jamais plus large** : les pages des projets
  lisibles **dans l'org active** (`ownership.accessible_project_ids`, l'ensemble
  d'`oto_project op=list` et de la recherche — plus étroit que l'ouverture par id, qui
  traverse toutes mes orgs) ; les procédures de mes paliers (moi, l'org active, mes équipes
  — toutes pour un admin d'org, la règle de `roles.can_read_group`). Une procédure qu'on
  m'a seulement **partagée** n'y figure pas. **Dérivée** : ni table ni journal, les
  `updated_at` que les deux stores tiennent déjà. ⚠️ **L'auteur n'est jamais déduit** : une
  page le porte (`docs.updated_by`, posé avec `updated_at` : créateur, puis auteur de chaque
  écriture, `null` après un déplacement — oto#274) ; une procédure, qui ne le porte pas, le
  tient de la révision écrite dans la **même transaction** que la modification (égalité
  exacte des horodatages). Page déplacée, procédure transférée, écrivain inconnu = `null`. **Sans org active : 200 et `items: []`**,
  jamais un 400 — l'accueil charge l'îlot d'office.
- `POST|DELETE /api/me/avatar` — upload (multipart `file`, png/jpeg/webp ≤ 2 Mo) / efface
  l'avatar user → Scaleway Object Storage, URL publique en DB. ⚠️ **Les deux verbes ne
  vivent plus au même endroit** : le `DELETE` est la capacité `me.avatar.clear` depuis le
  2026-08-27 ; le `POST`, multipart, reste écrit à la main et **classé `NATURE`** —
  l'adaptateur lit du JSON, un corps binaire est hors du moule par construction. C'est la
  FORME qui tranche, pas le domaine. Effacer purge aussi l'objet stocké (pas d'orphelin).
- `GET|DELETE /api/me/projects/{project_id}/files` + `POST …/files/{file_id}/public` —
  **les fichiers bruts d'un projet**, capacités `me.project_file.{list,delete,set_public}`
  depuis le 2026-08-27 (`capabilities/media_and_files.py`). Le **dépôt** (`POST …/files`,
  multipart) et l'**export ZIP** (`GET /api/me/projects/{id}/export`) restent écrits à la
  main, classés `NATURE`. ⚠️ **`s3_key` ne sort jamais** : chaque ligne rend une
  `download_url` **signée et temporaire** (elle expire — ne pas la mettre en cache),
  tandis que `public_url` est permanente tant que le partage est ouvert. Un stockage muet
  rend `download_url: null` sans faire disparaître la ligne. Lecture bornée à l'org de
  **consultation** (ADR 0023) : un projet visible via une AUTRE de mes orgs rend `404
  unknown_project`, non-disclosante. ⚠️ **`public` est désormais REQUIS** sur la bascule
  de partage : le handler d'origine traitait un corps sans `public` comme « rendre
  privé », donc un client mal formé **départageait un fichier en silence, avec un 200**.
  Un corps sans `public` (ou illisible) rend maintenant `400 invalid_input` — on refuse
  plutôt qu'on agit.
- ⚠️ **Duplication assumée** entre `me.project_file.{list,delete}` (REST, dashboard) et
  `me.project_files` (MCP `oto_project_files`, agent) : les RÉPONSES sont les mêmes, ce
  qui diffère est la forme des REFUS (la face MCP joint un message à ses 404) et l'entrée
  (`op` + `project_id` contre des paramètres de chemin). Les fusionner changerait les
  corps servis au dashboard — même famille de décision que la toolbox (oto-backend#429).
- `POST /api/me/projects/{project_id}/transcriptions` + `GET /api/me/transcriptions/{job_id}` —
  **la transcription d'un audio** (ADR 0074), capacités `me.transcription.{create,read}`
  (`capabilities/transcription.py`) ; le dépôt DIRECT de l'audio, `POST
  …/transcriptions/upload` (multipart `file` + `vocabulary`, `vocabulary_replace`
  `true|false` facultatifs), est écrit à la main (`api/transcription.py`, `NATURE`). Les
  deux dépôts rendent **`202 {job_id, status:"pending"}`** sans appeler Mistral : le
  worker s'en charge. `create` prend une RÉFÉRENCE `source` (`file_source` :
  `{"kind":"project_file","file_id":N}`, le projet de l'URL par défaut ; `transit`, `url`,
  `drive`, `gmail`). La relecture rend `status` (`pending|running|done|failed`), `project_id`,
  `filename`, et sur `done` la page (`page{id,project_id,title,url}`), `words`,
  `duration_s`, `speakers`, `turns` (le nombre) et **`transcript`**, les tours
  `[{speaker, start, end, text}]` en secondes — que la face MCP (`transcription_status`)
  ne rend jamais : un agent lit la page. Ordre des gardes : projet visible dans l'org de
  consultation (404) → écriture (403) → lecture de l'audio → credential (`400
  credential_unavailable`). Plafond d'un audio : **100 Mo** (`MAX_AUDIO_BYTES`, tenu
  pendant la lecture du multipart, `413 content_too_large`) ; Mistral en accepte 500 et
  3 h. Un travail dont le projet est illisible répond `404 unknown_transcription`, comme
  un inconnu.
- `POST|DELETE /api/orgs/{id}/logo` — upload / efface le logo **uploadé** d'org (org_admin, multipart `file`). Le logo AFFICHÉ (`logo_url` des lectures + `active_org_logo_url` de `/api/me`) est l'**effectif** : upload sinon dérivé du CDN logo.dev via le `domain` déclaré (`org_store.effective_logo_url`, token `LOGODEV_TOKEN`) ; `logo_custom` (fiche org) dit si un upload existe.
- `PATCH /api/orgs/{id}` (+ miroir `/api/admin/orgs/{id}`) — profil d'org (org_admin) : `name`, `description`, **`domain`** (domaine de marque, normalisé `org_store.normalize_domain` — `""` efface, saisie URL tolérée, invalide → 400 `invalid_domain`), `industry`, `location`. Capacité `org.update` (MCP `oto_org(op='update')`, console ADR 0047).
> ⚠️ **`POST|DELETE /api/settings/linkedin` n'existe plus** — ce doc l'a annoncée comme
> servie jusqu'au 2026-09-05. LinkedIn est passé par le connecteur hébergé Unipile, la
> route a été retirée, la ligne est restée. Contrôlé sur la table de routes gelée
> (`tests/api/api_routes_table.txt`) : **zéro** des routes servies ne la porte, pas plus
> que `/api/whatsapp/pair/*` ni `/api/whatsapp/status`, que l'extension appelle encore.
> Cf. oto-backend#423.

- `POST /api/me/connectors/{name}/session/start` + `POST …/session/finalize` — **la
  connexion par SESSION NAVIGATEUR** (Live View Browserbase, ADR 0026), capacités
  `me.browser_session.{start,finalize}` depuis le 2026-08-27
  (`capabilities/browser_sessions.py` ; `api_routes_credentials.py` supprimé).
  `start` ouvre une vraie fenêtre de navigateur hébergé où **l'utilisateur se connecte à
  la main** et rend `{live_view_url, context_id, session_id}` ; `finalize` vérifie le
  login puis persiste la session au coffre — c'est un credential comme un autre.
  ⚠️ **`connected: false` est une 200** : « pas encore logué », pas un échec — la session
  vit, rien n'a été écrit, on réessaie. **Pas de face MCP** : un `context_id` EST le
  credential, et le geste exige un humain ; le pendant agent est
  `POST /api/me/connectors/{name}/connect` (capacité `me.connector_connect`).
  `scope` ∈ `member` (défaut) | `org` | `group` — les deux partagés exigent d'être admin
  du palier ET un connecteur partageable, et **l'ordre des refus est un contrat** :
  `400 no_org_context` → `400 not_org_shareable` → `403 forbidden` (d'où une escalade au
  handler plutôt qu'une règle d'autz, qui trancherait trop tôt). `account` = le compte du
  coffre visé (connecteur générique : le host — une ligne PAR SITE) ; `force` persiste
  sans le verify générique, réservé aux connecteurs account-aware. Autres refus :
  `404 not_a_session_connector`, `400 missing_params`, `400 invalid_scope`,
  `503 browserbase_unavailable`, `502 session_verify_failed` (les deux derniers portent
  un détail actionnable). ⚠️ **Corrigé le 2026-08-27** : ce paragraphe annonçait qu'un
  corps JSON **malformé** rendait `400 missing_params` — l'adaptateur de capacités
  l'avalait alors comme un corps absent, et c'était le site B4 de l'inventaire des
  silences. Il rend maintenant `400 invalid_json` (corps illisible) ou `400 invalid_body`
  (JSON valide mais pas un objet), sur les ~200 routes générées.
  `missing_params` accusait l'appelant de n'avoir rien envoyé alors qu'il avait envoyé
  quelque chose d'inexploitable.
- `GET|POST|DELETE /api/settings/api-keys/{provider}` — **ton credential** pour un
  connecteur, dans l'org de contexte (capacités `me.credential.{get,set,clear}` depuis
  le 2026-08-27 ; **pas de face MCP** — un secret brut ne passe pas en argument d'outil).
  ⚠️ **RUPTURE DE CONTRAT le 2026-08-31 (#671) : le `GET` ne rend plus la valeur d'un
  champ secret.** Il la rendait en clair pour tout champ déclaré `reveal=True` — le
  défaut de 55 connecteurs. Sa clé est désormais **ABSENTE du corps** (pas `null`, pas
  `""` : un champ vidé se lirait « rien de posé » et un appelant continuerait sur du
  vide). À la place, de quoi reconnaître la clé sans la lire : `configured`,
  `read_set_at`, `read_set_by`, et `read_fingerprints` — `{champ: 4 caractères}`, HMAC
  lié à la ligne du coffre, jamais des caractères du secret. Les champs **non secrets**
  (`base_url`, `auth_mode`, `region`, email) continuent de sortir tels quels, donc la
  modification partielle de #448 est intacte. Demander la valeur (`?reveal=1`) rend
  `403 secret_never_revealed`. **Pour changer une clé, on la repose** — le POST
  conserve les champs qu'il ne reçoit pas.
  Le **corps du POST est plat et dynamique** : ses clés sont les `credential_fields` du
  connecteur (publiés par `GET /api/connectors`), plus `account` — le nom du compte visé
  quand le connecteur en porte plusieurs (le mot d'usage est dans `auth.account_noun` :
  « workspace » pour Slack). D'où le cran `body_field` du binding : sans lui, la garde de
  champ inconnu refuserait chaque clé de credential.
  **Le refus de CONNECTEUR dit laquelle des quatre causes a mordu** (depuis le
  2026-09-08 ; les quatre rendaient `404 unknown_provider` avant, et 29 connecteurs du
  registre s'entendaient donc dire « inconnu » au palier membre — deux sessions y ont
  lu, le 08/09, un déploiement manquant) : `404 unknown_provider` = le nom n'est pas au
  registre, le seul cas où chercher ailleurs a un sens ; `400 credential_delegated` = il
  ne porte pas de clé à lui, le refus **nomme le porteur** (un canal unipile →
  `unipile`) ; `400 no_credential_form` = pas de champ de saisie, la connexion passe par
  un flux dédié (session de navigateur, consentement OAuth) ; `400 wrong_credential_scope`
  = il prend bien une clé, mais pas à ce palier — le refus **nomme le palier** qui
  l'accepte (`http` : `scope=org`) et, pour le `POST` qui n'a pas de `scope`, la route
  qui la pose (`PUT /api/orgs/{id}/secrets/{provider}`). Autres refus :
  `400 missing_credentials`
  (le champ vide est NOMMÉ), `409 account_required` (pose anonyme là où des comptes nommés
  existent), `400 single_account_connector` (compte nommé sur un connecteur qui ne les
  résout pas), `400 verify_failed` (sonde avant persistance, #106). `DELETE` prend
  `?scope=member|org|group` (admin du palier pour les deux derniers) et `?account=`.
- `GET /api/me/tools` + `GET /api/me/tools/registry` + `POST|DELETE /api/me/tools/{name}`
  + `GET …/{name}/detail` + `POST …/{name}/call` — **la toolbox du membre**, capacités
  `me.tools.{list,registry,disable,enable,detail,call}` depuis le 2026-08-27
  (`capabilities/tools_me.py` ; `api_routes_tools.py` supprimé).
  ⚠️ **`POST` DÉSACTIVE, `DELETE` RÉACTIVE** — le chemin nomme la ligne de denylist, pas
  le tool : la poser masque, la retirer démasque. Contre-intuitif, historique, figé par
  test. La bascule est **visibilité-only** (ADR 0031) : `enabled` est une préférence
  d'affichage, jamais une autorisation — l'accès réel reste gardé au call-time
  (credential + activation). Un tool **protégé** (anti-lockout)
  refuse le masquage en `400 protected_tool:<nom>`.
  `…/registry` = le registre BOOT (ADR 0014), immunisé à la visibilité de session : il dit
  ce qui EXISTE, pas ce qui m'est visible ; sa `description` est un résumé d'une ligne, la
  fiche complète est `…/detail`. **`…/registry` doit précéder `…/{name}`** dans la table,
  sinon `registry` est servi comme un nom d'outil — les six ont migré EN BLOC pour ça.
  `…/{name}/call` exécute un outil **testable** (open-data en lecture seule) sous
  l'identité de l'appelant ; son corps est **libre** (il EST les arguments, nus ou
  enveloppés dans `{"arguments": {…}}`, d'où `body_field`) et ⚠️ **l'erreur de l'outil
  revient en DONNÉE — `ok:false` en 200** : la voir est le but du test. Les 4xx disent
  qu'on n'a pas pu lancer (`403 not_testable:` — qui passe **avant** la résolution du nom,
  donc un outil inconnu rend 403 et non 404 —, `400 bad_arguments:`).
  **Pas de face MCP** : `oto_list_my_tools`/`oto_enable_tool`/`oto_disable_tool` restent
  écrits à la main, leurs formes diffèrent de celles-ci — réconciliation suivie en
  oto-backend#429.
- `GET /api/me/instructions` (index des procédures ; le readme d'org est un guide `delivery=init`, plus servi ici) + `GET|PUT|DELETE /api/me/instructions/{slug}` + `GET /api/me/instructions/{slug}/versions` + `POST /api/me/instructions/{slug}/revert` — procédures de l'**org active** (le slug `claude_md` est RÉSERVÉ au readme et refusé ici) (cf. §Guides). Lecture = membre ; écriture = `org_admin` (ou platform admin). Édité par le dashboard (`/procedures`). ⚠️ Le bundle sert **trois** droits de l'appelant : `can_edit` (administrer l'org) et — depuis la suite de #695 — `can_write_instructions` / `can_delete_instructions`, un par verbe. Ce sont ces deux-là que les boutons d'une procédure lisent, ici comme sur `GET /api/groups/{id}/instructions` où ils ne valent PAS la même chose (membre pour écrire, chef pour supprimer). Le bundle d'org porte aussi **`archived`** : les procédures archivées, la plus récemment retirée d'abord, avec leur `archived_at` — servie à qui peut les remettre en service (`POST /api/me/instructions/{slug}/unarchive`, org_admin), vide pour les autres. C'est le seul endroit où une procédure archivée se LISTE : sans lui, la remettre en service exigeait de connaître déjà son slug. Jamais dans `instructions`, jamais dans l'index que lit l'IA (`org_store.list_archived_instructions`, à part de `list_instructions`). ⚠️ Le `PUT` renvoie un **`diagram_warning`** (toujours présent ; `null` = rien à signaler) quand le corps n'embarque pas le SCHÉMA requis de la procédure — non bloquant, comme `unresolved_tools`/`slot_warnings` (cf. §Guides). Son accusé porte aussi **`guide_id`**, l'identifiant STABLE de la ligne écrite (deux paliers portent le même slug en production : le slug ne désigne pas une ligne), et — au seul palier personnel — **`scope_note`**, qui dit ce que cette portée implique plutôt que de la nommer. Le `GET` et le `PUT` portent aussi **`other_scopes`** et **`shadow_warning`** quand la copie perso de l'appelant masque la procédure de l'org (#1156, cf. `guides.md`) — absents sinon. Il porte enfin **`body_sha256`** (oto#133, aussi sur `POST /api/me/instructions` et `PUT /api/groups/{id}/instructions/{slug}`) : le SHA-256 hex du corps **relu en base pour la version écrite**, jamais celui de l'envoi — à comparer au SHA-256 de ce qu'on a envoyé, blancs de tête et de fin retirés (le store les retire, et ramène au canonique les outils cités sous un nom de produit). Un écart dit que la base porte autre chose que ce qu'on croit avoir publié (`oto_mcp/procedure_empreinte.py`).
- `GET|PUT|DELETE /api/me/guides/{scope}/{slug}` (+ `GET /api/me/guides`) — **la prose d'instruction**, un seul primitif sur deux axes (ADR 0042 §Convergence des surfaces) : `scope` ∈ platform|org|group|user × `delivery` ∈ `on-demand` (défaut, un how-to chargé au besoin) | `init` (**readme injecté à chaque session**, slug canonique `readme` — corps vide = couche effacée). Miroir REST d'`oto_guide`, mêmes handlers. Le `GET` d'un guide on-demand rend aussi `rev`, l'ETag de son contenu ; l'édition partielle (`oto_guide op=patch`, `docs/guides.md`) est MCP seule, sans route REST. Écriture gatée par scope (platform_admin / org_admin / chef d'équipe / self). Variantes par-id pour viser une org/équipe précise plutôt que l'active : `GET|PUT /api/orgs/{id}/guides/{scope}/{slug}` et `/api/groups/{id}/guides/{scope}/{slug}` — ⚠️ **aucun code ne les appelle avec un slug libre** (mesuré le 09/09/2026 sur les 7 arbres : le seul appelant, `oto-dashboard/frontend/src/api/console.ts:306`, ne les emprunte qu'avec `slug=readme&delivery=init`) ; leur consommateur plausible est un agent qui suit cette page. ⚠️ **`409 delivery_conflict`** (09/09/2026) : l'identifiant de la ligne dérive de `(scope, owner, slug)` et **ignore `delivery`**, alors que toutes les lectures filtrent dessus — un `PUT …/guides/org/readme` on-demand tombait donc sur la MÊME ligne que le readme injecté de l'org, en remplaçait le corps, laissait `delivery='init'` et rendait 200 : prose de toutes les sessions détruite, guide écrit introuvable. L'écriture est désormais refusée dans les deux sens, rien n'est écrit, et le message nomme le geste à faire. *(Remplace `GET|PUT /api/me/agent-readme`, retiré le 2026-07-28.)*
- `POST|DELETE /api/me/projects/{id}/public-share` — **partage public CHIFFRÉ** d'un projet (ADR 0032 §3, zero-knowledge). Le dashboard chiffre le snapshot (brief + pages) côté navigateur et POSTe uniquement `{ciphertext}` ; renvoie `{token, public_base_url}`. Écriture = `ownership.can_access(project, write)`. La clé de déchiffrement n'atteint JAMAIS le serveur (fragment d'URL).
- `GET /api/public/projects/{token}` — **sans auth** : renvoie `{ciphertext, updated_at}` du snapshot chiffré. Déchiffrement côté navigateur (route `/p/p/{token}#<clé>`). Pendant public de `GET /api/public/docs/{token}` (#4a).
- `POST /api/me/upload-url` — **frappe le lien signé** (oto#106, capacité `me.upload_url`, la même que l'outil agent `oto_upload_url` : mêmes paramètres, `Output` et refus déclarés). Avant, la frappe était MCP-only : un client REST pur voyait la réception publiée sans pouvoir obtenir le jeton. Hors portée d'un jeton **porté** (la cible vit dans le corps).
- `POST /api/me/import` — **the server fetches the file** (capability `me.import`, same as the agent tool `oto_import`): a `source` (url, drive, project_file, gmail) into a table or a project, no content through the client. See `docs/datastore.md` §Server-side import.
- `PUT|POST|GET /api/upload/{token}` — **réception d'un upload signé out-of-bande** (issue #105), **pas de JWT** : le `{token}` est un jeton HMAC scellant `(sub, org, cible)` + TTL court + usage unique (émis par `POST /api/me/upload-url` ou `oto_upload_url`, module `upload_tokens.py`). ⚠️ **Route de NATURE à contrat déclaré** (oto#106) : `api/uploads.py` pose un `ContratDeRoute` sur ses handlers (description, corps par verbe, accusé `AccuseDeDepot`, refus `DeclaredError` jugés atteignables par `tests/test_capability_declared_errors.py`) et `/openapi.json` le publie (`security: []`, tag `_nature`) au lieu de la souche « écrite à la main ». ⚠️ **Compatible avec les contrats épinglés d'avant** : les en-têtes `X-Oto-Org`/`X-Oto-Group` restent déclarés (le middleware précède la route) et le corps est décrit `required: false` — le retrait des uns et un corps devenu obligatoire avaient rougi le contrôle « Contrat avant déploiement » d'un front consommateur. Les refus de la cible portent désormais leur phrase dans `detail` (« ligne NDJSON N invalide »). **PUT** = un agent avec shell y pousse le corps brut (`curl --data-binary @fichier`) ; **POST** multipart `file` = le formulaire humain ; **GET** = page HTML d'upload autoportée (fallback quand l'agent n'a pas de shell, ex. claude.ai : il transmet le lien à l'humain — le jeton n'est PAS consommé au GET). Le backend matérialise dans la cible en **réappliquant** son autz, consomme le jeton (anti-rejeu), renvoie un **accusé léger** (id + compteurs), jamais le body. Cibles : page Documents (`doc`), fichier brut de projet (`project_file`, autz `ownership.can_access(project, write)`), lot de lignes datastore (`datastore` — NDJSON/CSV batch-upsert sur clé, autz `ownership.can_access(datastore_namespace, write)`, ns_id scellé au mint). Évite de faire transiter du gros contenu par le contexte du LLM. ⚠️ **Le plafond (`upload_tokens.max_bytes()`, 25 Mo) se tient PENDANT la lecture** depuis #562 : le corps se lit en flux et la lecture s'arrête au premier octet au-delà (`413 content_too_large`), un `Content-Length` annoncé au-delà est refusé sans rien lire, et le POST ne confie au parseur multipart qu'un corps déjà borné (plafond + 64 Kio d'enveloppe, une seule partie `file`, aucun autre champ). Avant, `request.body()`/`request.form()` prenaient tout, puis on comptait. ⚠️ **Le type SERVI d'un `project_file` se décide sur le contenu** (#562, `media_store.type_servi`) : une signature de format reconnue fait foi (le type déclaré — au mint, sinon l'en-tête — ne départage qu'une famille : docx/xlsx dans un zip, m4a/mp4 dans un `ftyp`) ; un texte UTF-8 déclaré passif (`text/plain`, `text/csv`, `text/markdown`, `application/json`…) garde son type s'il ne porte pas de balisage actif ; tout le reste — non reconnu, ou actif (HTML, SVG, XML, JavaScript, quel que soit le type déclaré) — est écrit en `application/octet-stream` avec `Content-Disposition: attachment`. Rien n'est refusé. La ligne du fichier (`mime`) enregistre le type servi ; les objets écrits avant le 23/09/2026 gardent le leur. ⚠️ **Le payload du jeton est LISIBLE, et c'est accepté** (#562, 23/09/2026) : signé HMAC mais pas chiffré, il porte `sub` (compte émetteur), `org`, la cible complète (kind, `project_id`/`doc_id`/`ns_id`, nom de fichier, titre, type déclaré), `jti` et `exp`. Rien de cela n'est un secret — sans la signature il n'ouvre rien, les identifiants n'authentifient pas seuls, et la cible est réautorisée à la réception — et le jeton vit **15 min**, à **usage unique**. Qui émet un lien le confie à un tiers : ne pas y sceller un titre ou un nom de fichier confidentiel. ⚠️ **`project_file` ne rend QUE `filename` + `bytes` depuis oto#86** : l'accusé servait la ligne de base entière (id, `created_by` — le compte DÉPOSANT — état de publication…) à un tiers anonyme porteur du lien, allow-list `redaction.champs_autorises` désormais. Le libellé de la page GET (`target_label`) ne porte plus non plus `doc_id`/`project_id` (cf. `docs/redaction.md` §Face REST).
- `POST /api/datastores/{ns}/rows` — **UNE ligne** : le corps EST la ligne (un objet, une clé par colonne ; capacité `me.datastore.append_row`, 201). Pas de lot sur cette route : un corps dont l'unique clé porte une liste d'objets — ou dont la clé `rows` en porte une, quelles que soient les autres (oto#151) — est refusé `400 batch_body` — sans schéma il faisait une ligne imbriquée en 201 muet, sous `strict: true` une ligne imbriquée avec relevé `hors_schema` (oto#48, mesuré le 04/09/2026) ; une colonne DÉCLARÉE sous ce nom reste écrivable. Garde : `tests/datastore/test_lot_dans_ligne_oto48.py`.
- `POST /api/datastores/{ns}/rows/batch` — **le lot JSON** (oto#151, capacité `me.datastore.write_rows`, 200) : corps `{"rows": [{…}, …], "key": "<colonne>", "upsert": true, "donnees_d_origine": true}`, `key` (défaut : la clé métier déclarée), `upsert` et `donnees_d_origine` facultatifs, `readonly_override`/`force`/`origine_override` comme sur l'ajout d'une ligne. Le moteur est celui de `data_write(rows=…)` : `key` NOMMÉ désigne (une ligne dont la clé existe la modifie, une neuve est créée) ; omis, le lot AJOUTE sur la clé déclarée, et une clé déjà en base ne fusionne qu'avec `upsert=true` (oto#141 — sans lui, lot refusé ENTIER avant sa première ligne, `409 business_key_exists`, `details.doublons`/`details.existantes`) ; deux lignes du lot à la même clé suivent cette dernière règle dans les deux cas ; refus qui nomment la ligne et disent où reprendre (le lot n'est pas atomique), `@keep`/`@clear` jugés sur le lot entier avant la première ligne, notices cumulées. Réponse `{datastore, ns_id, inserted, updated, count, key, ids, fusions?, …relevés}` — `ids` un par ligne envoyée, rang pour rang ; `fusions` = `[{rang, dans_rang, id, cle}]` les lignes qui ont fusionné (`dans_rang` : la ligne du même lot qui a posé la ligne visée, `null` si elle était en base). `POST …/rows` (une ligne) prend en query `?key=` (la clé déclarée : désigne la ligne, sans `upsert`) et `?upsert=true`, et rend le même `409 business_key_exists` sur un ajout (`details` : `key`, `valeur`, `id` de la ligne en place). La frappe d'un upload signé (`POST /api/me/upload-url`) prend `key` (désigne) et `upsert`, SCELLÉS dans le jeton (`400 upsert_without_key` sans clé) ; la réception rend `fusions` sans `id` et `409 business_key_exists` sans nommer de ligne par son identifiant. Avant elle, un client REST pur n'avait que le `PATCH` ligne à ligne (8 907 appels mesurés). Pour les volumes, l'upload signé ci-dessus. Garde : `tests/api/test_rest_lot_oto151_live.py`.
- **Datastore — les 24 chemins sous `/api/datastores`** (= `NS` ; relevés le 2026-09-05 depuis l'`openapi.json` **servi** : `curl -s https://mcp.oto.ninja/api/openapi.json`), tous capacités `me.datastore.*` de `capabilities/datastore/` — aucune route écrite à la main depuis le 2026-08-12 (#302). `{datastore}` = le **numéro** du tableau (`ns_id` ; son nom résout encore jusqu'au 08/11/2026, puis sera refusé — la réponse porte alors `Deprecation: true` et `Sunset`, cf. `docs/datastore.md`), `slot:<nom>` compris (même résolution que la face agent, sinon `400 jeton_mal_place` — qui porte aussi le refus du pronom `@claimed`, retiré le 07/09/2026) ; un tableau d'une autre de mes orgs rend `404 datastore_not_found` **avec l'org où il vit** (rejouer avec `X-Oto-Org`).
  - **tableaux** : `GET NS` (possédés + partagés ; seule réponse *filtrée* par la portée d'un jeton porté) · `POST NS` (créer) · `PATCH NS/{datastore}` (renommer — id, URL et partages stables) · `DELETE NS/{datastore}` (gouvernance) · `GET NS/{datastore}/url` (deep-link dashboard) ;
  - **lignes** : `GET NS/{datastore}/rows` (page `offset` + `limit` ≤ 1000, `total` du jeu filtré, **pas de curseur** — la fin se calcule, un `offset` au-delà rend `rows: []` en 200 ; `filter`/`filters` = JSON dans UNE chaîne de query ; **pas de `fields`**, la ligne entière) · `POST …/rows` (UNE ligne, 201, ci-dessus) · `GET|PATCH|DELETE …/rows/{row_id}` (`?readonly_override=true` sur `POST`/`PATCH`, propriétaire ou gouvernant, journalisé ; `?expected_revision=` sur `PATCH` **et sur `DELETE`** (oto#217), en query et jamais dans le corps, avec le `_revision` que porte chaque ligne lue : précondition d'un geste décidé d'après cette lecture — la révision avance quand les données ou le bail changent ; si elle a bougé, rien n'est écrit ni supprimé, `409 revision_conflict` avec `details.current_revision` ; un bail d'un autre reste `409 row_locked`, y compris sur la suppression, qui est désormais jugée SOUS le verrou de la ligne — bail, révision, puis suppression) ;
  - **schéma** : `GET|PUT|PATCH NS/{datastore}/schema` (`PUT` pose, ou retire avec `schema: null` ; `PATCH` par clé) · `POST NS/{datastore}/drop_column` (destructif, `confirm=true`) ;
  - **file de travail** : `POST NS/{datastore}/claim_next` · `POST …/rows/{row_id}/claim` (409 si bail d'un autre) · `POST …/rows/{row_id}/release` (gardée avec `worker`, forcée sans ; `expected_revision` optionnelle sur les DEUX régimes depuis oto#217 — une réservation reprise entre-temps par un autre travail rend `409 revision_conflict` au lieu de lui retirer sa ligne, et une ligne disparue `404 row_not_found` ; sans la précondition, le geste est inchangé et « rien à rendre » reste `released: false` / `reason: no_lease`) · `GET NS/{datastore}/queue` (lignes sous bail, lecture seule) ;
  - **agrégat** : `GET NS/{datastore}/aggregate` (`group_by` = UNE colonne ; `"a,b"` refusé `400 invalid_aggregate`, oto#50 — la forme liste n'existe que sur `data_aggregate`) ;
  - **partage** : `GET|POST|DELETE NS/{datastore}/share` (gouvernance) ;
  - **activité** : `GET NS/{datastore}/activity` · `GET …/rows/{row_id}/activity` — **sans face MCP** (opt-out explicite : lecture de cockpit).
  Les couches d'une colonne (`comment`/`link`/`origine`) sont servies **à plat** par défaut (`champ.origine`) sur les deux faces ; `?layers=nested` sur `GET …/rows` et `GET …/rows/{row_id}` rend la forme d'écriture `{valeur, + couches renseignées}` (oto#53 ; autre valeur → `400 invalid_layers` ; le défaut basculera vers `nested` avec préavis daté) ; les refus sont nommés (`row_invalid`, `business_key_required`, `invalid_row_input`, `batch_body`, `unknown_fields`, `403 datastore_read_only`, `404 row_not_found`) et **déclarés à l'`openapi.json`** sur l'écriture par id, la suppression, la libération et les deux réservations (oto#217) — ils étaient émis depuis toujours et publiés nulle part, donc traités en panne par un client généré. La sémantique complète et les divergences MCP/REST sont dans le guide servi `datastore-semantics` (oto#51).
- `POST /api/me/unipile/connect` + `POST …/reconcile` + `GET|DELETE /api/me/unipile` —
  **la messagerie hébergée côté membre**, capacités
  `me.unipile.{connect,reconcile,status,disconnect}` depuis le 2026-08-27
  (`capabilities/unipile_me.py`). **Il n'y a plus de webhook de liaison** :
  `POST /api/unipile/webhook` a été retiré le 2026-08-29 (#581, dormant depuis la v2
  du fournisseur — zéro appel sur les 31 jours de journal retenus) ; la liaison passe
  par la réconciliation, sous le JWT de la personne.
  ⚠️ **`connect` a DEUX formes de succès** : `{url}` d'ordinaire, et
  `{adopted, channel, account_name}` **sans `url`** quand le compte était déjà connecté
  sous la même identité dans une autre org — il vient d'être rattaché ici, il n'y a rien
  à consentir, le front doit rafraîchir. ⚠️ **Les refus `409` et `502` servent leur
  MESSAGE dans `error`**, pas leur code machine (forme historique conservée : le front
  affiche `error` tel quel, et pour ces deux-là le message est ce qui est actionnable) ;
  les autres statuts exposent bien leur jeton.
  `GET /api/me/unipile` **réconcilie avant de répondre** (self-heal : c'est LE chemin
  de liaison) — best-effort, jamais fatal pour le statut, no-op sans pending. ⚠️ Son champ `channels` ne montre QUE les comptes liés à l'org **courante** :
  un canal vu déconnecté peut l'être ailleurs, et `elsewhere` dit alors ce qui est
  adoptable ici en un clic. `DELETE` est une **soft**-déconnexion, par org et par canal
  (`?channel=`, défaut `linkedin`) : le compte survit chez le fournisseur et la ligne
  survit comme preuve de propriété, ce qui rend la reconnexion déterministe.
  **Pas de face MCP** : la face agent de ce geste est `me.connector_connect`
  (`POST /api/me/connectors/{name}/connect`), qui **supersède** `…/unipile/connect` —
  celui-ci vit jusqu'à la bascule du front.
- `GET /api/google/oauth/start` + `…/oauth/status` + `DELETE /api/google/oauth` +
  `POST /api/google/oauth/default` —
  **les VERBES du consentement OAuth per-user**, capacités
  `me.federation.google.*` depuis le 2026-08-27
  (`capabilities/federated_oauth.py`). Les **callbacks** (`…/oauth/callback`, un par
  fournisseur) restent écrits à la main et le resteront : le fournisseur y redirige le
  NAVIGATEUR (302, sans en-tête d'auth), or l'adaptateur authentifie toujours et répond
  en JSON — hors du moule par construction.
  ⚠️ **Ils étaient TROIS jusqu'au 2026-09-09** : `atlassian` et `folkmcp` servaient les
  mêmes verbes sous `me.federation.{atlassian,folkmcp}.*` ; ils sont partis avec la
  **fédération MCP** (**ADR 0069** — trois connecteurs fédérés déclarés, aucun jamais
  utilisé, zéro appel sur 40 jours). **Le nom du module et les clés de capacité ne
  bougent PAS** : `me.federation.google.*` est un identifiant **servi**, le renommer
  serait une rupture de contrat sans contrepartie. Et google n'a jamais été fédéré —
  c'est un connecteur NATIF dont le credential s'acquiert par OAuth, comme salesforce
  ou zoho.
  ⚠️ **LA convention de retour, après ce consentement, est UNIQUE depuis
  oto-backend#670** : `?connector=<nom>&connect=connected|error|forbidden|denied`, généralisée
  depuis la forme salesforce et fabriquée une seule fois
  (`auth.flow.connector_return_suffix`/`connector_return_url`) plutôt que composée à la
  main dans chaque callback — c'était le cas avant ce lot, avec cinq formes différentes
  et deux replis cassés (une f-string à accolades doublées sur atlassian/folk, qui
  rendait une chaîne littérale au lieu d'une URL). `zoho` et `google` servaient un
  suffixe LU par le dashboard (`?zoho=connected`, `?google=connected`) doublé le temps
  d'un préavis ; **retiré le 2026-09-17, sans préavis** (décision d'Alexis, voir
  `docs/alias-deprecies.md`) — mesuré : zéro lecteur (dashboard migré depuis le 04/09,
  zéro connexion Zoho/Google chez le seul partenaire concerné). `atlassian` et
  `folkmcp` avaient gagné `connect=` en pur ajout (leur `connector=` déjà servi ne
  bougeait pas) ; ils ne servaient AUCUNE distinction succès/échec avant ce lot (le
  repli cassé rendait toujours la même destination), donc rien n'y avait de lecteur à
  préserver — et ils sont partis le 2026-09-09.
  ⚠️ **Un échec, un mot** (oto-app#11) : `denied` = le fournisseur a refusé
  l'autorisation (`error=access_denied`, ou son équivalent documenté — `success=false`
  chez WordPress), tranché une fois par `auth.flow.etat_sans_code` ; `forbidden` = c'est
  oto qui refuse (plus le droit d'écrire au niveau demandé au départ) ; `error` = une
  vraie panne. Chez Meta, `access_denied` couvre aussi l'app non publiée : c'est bien un
  refus du fournisseur, donc `denied`, sans distinction inventée.
  **Il ne reste qu'une famille.** Jusqu'au 2026-09-09 il y en avait deux : `atlassian`
  et `folkmcp` fédéraient un MCP distant (jeton per-user au coffre, injecté par
  `tools/mount.py`), surface identique au champ près, symétrie **contractuelle** — le
  dashboard les pilotait par un client GÉNÉRIQUE (`/api/${name}/oauth/…`). `google`
  était déjà à part : multi-compte, donc un statut plus riche et un verbe de plus.
  C'est lui, le cas particulier, qui SURVIT ; ⚠️ **le client générique du dashboard
  reste générique** — il ne se replie pas sur un chemin en dur parce que la liste a
  rétréci.
  ⚠️ **Les champs racine de `google/oauth/status` (`granted_at`, `scopes`) décrivent le
  compte PAR DÉFAUT**, pas l'union des comptes — héritage du mono-compte ; la vérité
  multi-compte est `accounts`. Sans défaut posé, la racine est vide alors que `connected`
  est vrai. ⚠️ **`DELETE /api/google/oauth` SANS `?account=` révoque TOUS les comptes** —
  `account: null` en réponse veut dire « tous », pas « aucun ». Un `500
  oauth_misconfigured:` signale une app OAuth mal configurée côté PLATEFORME, pas une
  erreur de l'appelant.
  ⚠️ **Ces chemins NOMMENT leur connecteur**, ce que `test_connector_flow.py` interdit
  depuis que zoho et salesforce sont passés au chemin fixe (v1.19.0). Ils y sont tolérés
  NOMMÉMENT : les vider suppose que le widget OAuth du dashboard (encore appelé « widget
  de fédération » côté front, nom qui a survécu au mécanisme) cesse de construire son URL
  à partir du nom du connecteur — **dette de front**, pas de backend.
  **Pas de face MCP** : ouvrir une page de consentement demande un navigateur, et le
  pendant agent générique existe (`me.connector_connect`).
- `GET /api/me/connectors/{name}/oauth-status` + `DELETE /api/me/connectors/{name}/oauth`
  — **le statut et la déconnexion OAuth GÉNÉRIQUES**, capacités
  `me.connector_status`/`me.connector_disconnect` (`capabilities/connectors/oauth_status.py`,
  depuis le 2026-09-04, oto-dashboard#125 items 2/3) : le chemin fixe qui ne nomme pas
  le connecteur, symétrique de `me.connector_connect` — `{name}` ∈ **google, meta_ads,
  instagram_meta** (les deux derniers depuis le 2026-10-13, oto-backend#1241) ; un autre
  nom rend `400 no_oauth_status`. ⚠️ Ils étaient **trois** jusqu'au 2026-09-09
  (atlassian, folkmcp, google) ; le chemin est resté GÉNÉRIQUE quand la liste a rétréci,
  et c'est par un simple `declare_status` que meta_ads et instagram_meta y sont entrés.
  **Le catalogue le dit** : `connect.disconnect` (booléen, `GET /api/connectors` et
  `GET /api/me/connectors?verbose=true`) vaut `true` pour un connecteur dont la
  déconnexion générique est déclarée — c'est ce que lit un front pour afficher
  « Déconnecter ». **Révocation chez Meta** : meta_ads révoque par
  `DELETE /{user-id}/permissions` (Graph API) puis retire la ligne MEMBRE ; un échec de
  révocation est JOURNALISÉ et la ligne part quand même. instagram_meta n'a pas de
  révocation documentée (API Instagram Login) : la ligne est retirée, le jeton reste
  valable chez Meta jusqu'à son expiration.
  ⚠️ **`me.federation.google.*` ci-dessus RESTE en place** : son retrait est un lot
  séparé, une fois le dashboard basculé sur ces deux-là (même discipline que #519/#670).
  **Contrainte 1** (bloquante) : `me.connector_status` dérive `{connected, set_at,
  health_ko, health_reason}` de la MÊME lecture que `/api/me` (`access.status_for`),
  JAMAIS d'un second appel à `google_oauth.list_accounts` qui pourrait diverger. ⚠️ Pour **google** spécifiquement,
  `access.status_for` ne porte qu'UNE identité par défaut (mono-compte hérité) : ce
  contrat commun ne rend donc RIEN de spécifique à google au-delà de ces quatre champs —
  forcer un `accounts` ici recréerait la seconde vérité que la contrainte interdit. La
  richesse multi-compte reste `connectors.identities` (op=list), hors de ce lot.
  **Contrainte 2** (décision d'Alexis) : `me.connector_disconnect` est **irréversible,
  en UN SEUL appel** — révoque chez le fournisseur quand le mécanisme le permet
  (reprend EXACTEMENT `federated_oauth._google_revoke`) et
  DANS TOUS LES CAS retire la ligne locale, jamais d'état intermédiaire « en attente de
  confirmation ». Sortie `FederationDisconnected{ok, disconnected}`, réutilisée telle
  quelle — **nom conservé** après le retrait de la fédération (2026-09-09) : c'est un
  schéma servi. **Pas de face MCP** sur les deux (`mcp=None`, comme
  `me.federation.google.*`).
- `GET|POST|DELETE /api/admin/connectors/activation` + `GET|POST /api/admin/connectors/{provider}/platform-access`
  — **le palier PLATEFORME des connecteurs**, capacités
  `platform.connector.{activation_list,activation_set,activation_clear,access_list,access_set}`
  depuis le 2026-08-27 (`capabilities/platform_connectors.py`). C'était l'étage qui
  manquait : les paliers **org** et **équipe** de la même famille étaient déjà des
  capacités (`connectors.activation.{set_org,set_group}`).
  **Activation** (ADR 0010 B4) : le code DÉCLARE les connecteurs, la DB décide lesquels
  sont EXPOSÉS. ⚠️ `enabled: null` = **OFF** (jamais posé, deny-by-default), pas
  « indéterminé ». Master **global** comme **override d'org** prennent effet dès la requête
  suivante, **sans redémarrage** : l'activation se lit en base à chaque requête (ADR 0011).
  La réponse du `POST` ne porte plus `restart_required` (retiré le 2026-09-23, #815 : il
  annonçait à tort un redémarrage pour le master global). Refus : `400 unknown_connector`,
  `400 enabled_must_be_bool`, et sur le `DELETE` (query `?connector=&org_id=`)
  `400 connector_and_org_id_required` / `400 org_id_must_be_int`.
  **Accès plateforme** (ADR 0044 §H) : vue connecteur-centrique unique qui remplace les
  leviers dispersés `/platform/orgs` et `/platform/users`. Le `POST` est un **acte
  unique** — il pose ENSEMBLE l'option offerte (couche 3) et le grant de clé plateforme
  (couche 2), ce que le backend couplait déjà. Aucun secret n'en sort. ⚠️ Si
  `open_tier` est vrai, une instance en partage `open` sert le connecteur à **tous sans
  grant** : `beneficiaries` ne dit alors plus la population servie. Chaque bénéficiaire
  porte `daily_quota`, le quota du grant de clé lu dans `meta.rate_limit_by` de
  l'instance (`null` = aucun quota, ou pas de clé) ; ⚠️ `platform_revoke` l'efface, donc
  le relever AVANT de révoquer pour ré-accorder à l'identique. Un connecteur par
  chaîne (`grants_chain`) accorde par ARÊTE, jamais par `share_down` : la vue liste
  alors chaque org ou membre qui en porte une VIVANTE (`has_key`, `daily_quota` = la
  contrainte de l'arête) et le dit par `chain_grants: true` — sans quoi une clé fermée
  d'un tel connecteur se lisait « ne sert personne » après un grant réussi. Lecture =
  `PLATFORM_ADMIN`, écriture = `SUPER_ADMIN`. Refus : `404 unknown_connector`,
  `400 invalid_body`, `404 unknown_org`/`unknown_user`, `400 no_platform_access`.
  **Pas de face MCP** : basculer le master global est un acte de déploiement, ouvrir un
  accès est un acte commercial — les paliers qu'un utilisateur pilote (org, équipe) sont
  déjà servis par `oto_connector_activation`.
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- `POST /api/admin/users/{sub}/reset-mfa` — super admin only ; efface TOUS les facteurs de
  double authentification d'un compte (récupération : appli ET codes de secours perdus,
  aucun autre moyen de rentrer). La politique MFA de son org, si mandatoire, continue de
  s'appliquer — il configurera un facteur neuf à sa prochaine connexion.
- `POST /api/admin/users/{sub}/grants/{key_id}` body `{daily_quota}` — set/update quota par grant (admin only)
- `GET|POST /api/me/tokens` + `DELETE …/{token_id}`, `GET|POST /api/admin/users/{sub}/tokens`
  + `DELETE …/{token_id}`, `GET|POST /api/admin/platform-keys` + `DELETE …/{provider}/{label}`
  — **les jetons API et les clés plateforme**, capacités `me.token.*`,
  `platform.token.*` et `platform.key.{list,create,delete}` depuis le 2026-08-27
  (`capabilities/api_tokens.py` ; `api_routes_admin.py` supprimé).
  ⚠️ **Les six routes de jetons portent `RestBinding.allow_api_token=False`** : un jeton
  `oto_` y est refusé (`403 api_token_forbidden`). Un jeton qui peut en créer d'autres
  rend sa fuite auto-entretenue — révoquer le jeton fuité ne suffit plus. C'est ce cran,
  absent de l'adaptateur jusque-là, qui retenait ces routes en écriture manuelle ; il
  vit désormais sur le binding, et un test le vérifie en JOUANT les six routes (pas en
  relisant le descripteur : c'est son application qui est la garde).
  Exception : le **jeton émetteur** (portée `issue`) franchit les trois routes membre
  (`RestBinding.allow_issuer_token`), jamais les trois admin — § Jetons API ci-dessous.
  ⚠️ **Le secret d'un jeton n'est rendu QU'À LA CRÉATION** — il n'est stocké que haché.
  `scopes: null` = jeton non porté (pleins pouvoirs du sub) ; sinon il est borné à des
  tableaux/projets nommés (`auth.token_scopes`). **Trois asymétries membre/admin conservées** :
  la création membre rend **201** et l'admin **200** ; le `DELETE` membre rend `{ok}` et
  l'admin `{ok, id}` ; seul le palier MEMBRE refuse un tableau que l'émetteur ne voit pas
  (`400 unknown_datastore` — sinon le jeton serait muet et on le croirait branché), et
  seul le palier ADMIN accepte un `ttl_days` (qui n'est retenu que s'il est fait de
  chiffres : `-1` ou un texte donnent « pas d'expiration »).
  **Révoquer ne supprime plus la ligne** (#523, 23/09/2026) : le `DELETE …/{token_id}`
  pose `revoked_at`/`revoked_by` (l'APPELANT)/`revoked_reason` (corps facultatif
  `{"reason": "…"}`, 500 caractères au plus, `400 reason_too_long` au-delà — jamais
  raboté) ; le jeton ne s'authentifie plus. Les deux `GET` ne rendent les révoqués
  qu'avec `?include_revoked=true`. Un jeton déjà révoqué rend `404 unknown_token` : une
  seconde révocation n'écrase pas la trace de la première. Avant, c'était un `DELETE`
  SQL : on ne savait plus qui détenait le jeton, ni quand ni pourquoi il avait été coupé.
  Les **clés plateforme** (ADR 0044 §F) ne rendent jamais leur secret — provider, libellé,
  date de pose. Refus : `400 invalid_provider`, `400 missing_fields`,
  `400 invalid_platform_provider`, `404 unknown_key`. ⚠️ Un corps JSON **illisible** rend
  désormais `400 invalid_provider` et non `400 invalid_json` (même statut).
  **Aucune face MCP** sur les neuf : la garde des jetons n'aurait aucun sens si un outil
  faisait le même geste, et `api_key` est un secret brut.
- `GET /api/admin/monitoring/summary?days=` + `GET /api/admin/monitoring/calls?limit=&sub=&tool=&errors=&days=` — journal des appels MCP, agrégats + brut (admin only, cf. §Monitoring)
- `GET /api/orgs/{id}/monitoring/{summary,calls,calls/{call_id},connectors,adoption,runs,runs/{run_id},gaps,tool-quality}` — **les mêmes lentilles au niveau ORG** (`capabilities/org_monitoring.py`, autz `ORG_ADMIN_OF`, face MCP `oto_org_monitoring`). Scope = `tool_calls.org_id`/`usage_signals.org_id` (ce qui a été émis SOUS l'org), jamais l'appartenance des membres. `adoption` n'existe qu'à cet étage (membre par membre : actif / jamais actif / bloqué par un connecteur). ⚠️ `calls/{call_id}` et `runs/{run_id}` rendent **404** hors de l'org (id séquentiel devinable). Sert la page dashboard `/org/monitoring`. Cf. `docs/monitoring.md` §Trois étages. ⚠️ `calls` rend aussi `scope` / `hors_scope` / `hors_scope_hint` : les appels des runs de l'org résolus sous une AUTRE org (axe `_org` absent) ne sont pas dans `calls` — le compte l'est (#630). ⚠️ `calls` rend par ligne `arg_keys` (les CLÉS des arguments, triées, `[]` sans argument — jamais une valeur) et `result_shape` (`empty` | `non_empty` | `refused(<code>)`, `null` = non mesurée — #644), et pas `args` ; `calls/{call_id}` rend `call.args` **tels que journalisés** (tronqués, jetons masqués #582, `null` sans argument). Aucune clé `arguments` n'existe sur aucune des deux (#634, 2026-08-30). ⚠️ `runs/{run_id}` sert les **mêmes** arguments sous la même règle (même colonne, même voie d'écriture — le contrat de `RunCall.args` le dit depuis le 01/09/2026). La borne est de **4 000 caractères par valeur** (`calllog.MAX_ARG_CHARS`, 300 jusqu'au 23/09/2026 — #413) plus une ellipse, les valeurs composées étant stringifiées AVANT d'être coupées, et toute coupe est **déclarée** dans `args._truncated` = `{"at": <borne>, "sizes": {<argument>: <taille réelle>}}` (clé exclue d'`arg_keys`) ; un argument déclaré secret part en empreinte `#` + 12 hexadécimaux, quelle que soit sa longueur. Les lignes antérieures au 23/09/2026 sont coupées à 300 sans déclaration. Un affichage se borne de son côté.
- `GET /api/admin/tenants?days=` + `GET /api/admin/tenants/{slug}?days=` — **suivi de l'étage tenant** (ADR 0052 ; `capabilities/tenants_admin.py`, autz `PLATFORM_ADMIN`, face MCP `oto_admin_tenant` op=list|get). Par tenant : configuration d'annuaire (émetteur, jwks, hosts, client OAuth, dashboard, chemins de lien), **état dans le process** (`loaded` / `pending_restart` — le registre d'émetteurs est bâti AU BOOT, donc un tenant déclaré depuis n'authentifie encore personne), et l'empreinte sur la fenêtre (orgs via `orgs.tenant_id`, comptes via la qualification du sub, comptes actifs, appels MCP). ⚠️ Ces deux rattachements sont des sources INDÉPENDANTES : `orgs_desalignees` compte les orgs du tenant créées par un compte relevant d'un autre, et la fiche en donne la liste. **Lecture seule** — déclarer un tenant est un runbook de provisioning, pas une route. Sert la page dashboard `/platform/tenants`.
- `POST /api/admin/tenants/{slug}/disablement` `{op: disable|enable, reason}` — **désactiver un tenant** (oto-backend#1165 ; `capabilities/tenant_desactivation.py`, autz `SUPER_ADMIN`, face MCP `oto_admin_tenant op=disable|enable`). Coupe chaque requête de ses comptes dès la suivante (`403 tenant_disabled`, toutes portes), révoque leurs jetons `oto_`, rend les compteurs par type ; `enable` ne rouvre que les nouvelles connexions. Refus : `404 unknown_tenant`, `409 primary_tenant`, `409 self_tenant`, `400 missing_reason|reason_too_long`. Procédure : `docs/tenants.md` §Désactiver un tenant.
- **Niveau d'accès d'un connecteur OAuth** (14/10/2026, `capabilities/connectors/access_level.py`,
  `docs/connector-model.md` §Niveau d'accès) — `GET|PUT|DELETE /api/orgs/{id}/connectors/{name}/access-level`
  (`ORG_ADMIN_OF`) et `GET|PUT|DELETE /api/groups/{id}/connectors/{name}/access-level`
  (`GROUP_ADMIN_OF`), corps du `PUT` `{level}` ; face MCP `oto_connector_access_level`
  (op `get|set|clear`, `scope` `org|group`). La réponse (`AccessLevelState`) rend le niveau
  posé à ce scope (`level`, `null` = rien), celui en vigueur (`effective`, `source` ∈
  `group|org|default`) et les niveaux déclarés avec leurs scopes ; `deleted` sur un retrait
  (idempotent). Refus : `404 unknown_connector`, `400 no_access_levels` (le connecteur n'en
  déclare pas), `400 unknown_access_level`, `409 access_level_wider_than_org` (une équipe
  ne fait que restreindre). Sous un niveau ≠ `full`, un appel non couvert est refusé
  `access_level_denied` (`docs/tool-visibility.md`).
- **App OAuth DÉDIÉE d'une org ou d'une équipe** (14/10/2026, `capabilities/connectors/oauth_app.py`,
  `docs/connector-vault.md` §App DÉDIÉE) — `GET|PUT|DELETE /api/orgs/{id}/connectors/{name}/oauth-app`
  (`ORG_ADMIN_OF`) et `/api/groups/{id}/connectors/{name}/oauth-app` (`GROUP_ADMIN_OF`).
  **REST seule** (secret brut). Corps du `PUT` : `client_id`, `client_secret`, et pour Zoho
  `data_center` (la région fait partie de l'app). Le `GET` ne rend **jamais** le secret ni
  le `client_id` : `configured`, `data_center`, `read_set_at`, `read_set_by`,
  `read_fingerprints` (`{champ: 4 caractères}`, HMAC lié à la ligne — le contrat de #671) ;
  rien de posé = `200 configured:false`, pas un 404. Refus : `404 unknown_connector`,
  `400 oauth_app_unsupported` (aucun flux du connecteur ne lit une app dédiée — liste
  fermée : les trois Zoho), `400 invalid_oauth_app` (app incomplète ou région inconnue).
  Lue par le consentement seul, jamais servie comme clé d'appel.
- **Palier org** (100 % en CAPACITÉS — `capabilities/orgs*.py` ; ⚠️ cette ligne renvoyait à `api_routes_orgs.py` jusqu'au 2026-08-27, fichier **supprimé** lors de la migration : projection 1:1 des meta-tools `oto_admin_*org*` / `oto_list_orgs`) :
  - self-service : `GET|POST /api/me/orgs` (**`POST` = `org.create` self-serve**, créateur→org_admin, cap `OTO_MCP_MAX_ORGS_PER_USER`) ; `GET /api/orgs/{id}` ; `POST|DELETE /api/orgs/{id}/members[/{sub}]` + `GET /api/orgs/{id}/members/events` (journal des entrées, sorties et changements de rôle, le plus récent d'abord, `limit`/`before_id` ; oto#145) + `PUT|DELETE /api/orgs/{id}/secrets/{provider}` (org_admin)
  - **invitations — feature cascade plateforme/org/équipe** (le scope est DÉRIVÉ des cibles : org_id NULL = plateforme, org_id seul = org, org_id+group_id = équipe). Trois faces émettrices, une seule acceptation :
    - **org** : `POST|GET /api/orgs/{id}/invitations` + `DELETE …/{inv}` (org_admin ; `oto_org` op=invite). Le POST refuse en 409 une adresse déjà membre (`already_member`) ou déjà invitée avec une invitation valide (`already_invited`, l'existante dans `details`) — #622, 29/08/2026.
    - **équipe** : `POST|GET /api/groups/{id}/invitations` + `DELETE …/{inv}` (group_admin ; `oto_group` op=invite). L'invité rejoint l'org parente PUIS l'équipe à l'acceptation.
    - **plateforme** : `POST|GET /api/admin/invitations` + `DELETE …/{inv}` (platform_admin ; `oto_admin_invite` op=create/list/revoke). `org_id` optionnel (vide = onboarding pur, sinon rattachement direct).
    - **acceptation commune** : `POST /api/me/invitations/accept` (`SUB_ONLY`, `token` + expiry). ⚠️ **Modèle BEARER** : le secret suffit, l'identité de l'accepteur n'est PAS confrontée à l'email invité — cette ligne a longtemps dit « match email vérifié », ce qui était faux du code servi (corrigé le 2026-09-01, cf. le commentaire de `_invite_accept`). Email via `oto_mcp/email.py` (otomata-mailer `mailer.oto.zone/api/send`, env `OTO_MAILER_SEND_BEARER`, best-effort → `invite_url` en repli ; **plus de Resend**).
    - **refus commun** : `POST /api/me/invitations/reject` (`SUB_ONLY`, `token` — même entrée que l'acceptation ; `oto_org` op=reject_invite), depuis le 2026-09-01 (#654). Ferme l'invitation (`org_invitations.declined_at`/`.declined_sub`) **sans créer ni retirer aucune appartenance** : elle quitte la file de l'émetteur, et plus rien ne peut la consommer — ni `accept`, ni la reprise automatique au signup par l'email (`reconcile_signup_with_invitation`). ⚠️ **Le refus n'est PAS bearer, contrairement à l'acceptation** : l'invitation doit être adressée à l'email du compte appelant (403 `not_the_invitee` sinon, invitation anonyme comprise) — accepter avec un secret qu'on détient est un geste sur soi, refuser détruirait l'invitation d'un tiers. Le refus **ne notifie pas** l'émetteur (aucune préférence de notification n'existe encore) et **ne bloque pas** une réinvitation : le 409 `already_invited` de #622 ne voit plus une invitation refusée.
    - **aperçu PUBLIC, sans auth** : `GET /api/invitations/{token}` (`api/public.py::invite_preview` → `org_store.preview_invitation`), le jeton EST le secret — alimente l'accueil « vous êtes invité·e » avant création de compte (`email`, `inviter`, `org_name`/`group_name`, `scope`). ⚠️ **Le repli de l'inviteur vers son email a été retiré (oto#86)** : `_PREVIEW_SELECT` projetait `COALESCE(nom, email)` — un compte invitant sans nom déclaré servait son adresse à l'anonyme. `inviter` reste `string | null` (le dashboard dégrade déjà vers un message générique). Écrite à la main, hors de l'adaptateur des capacités qui authentifie toujours : son seul frein est la classe `sensible` de la limite de débit applicative (par adresse, 60/min par défaut, et le budget d'échecs que consomme chaque 404 ; mode `report` par défaut — §Limite de débit applicative).
    - ⚠️ **Le CODE COURT d'invitation (7 caractères, ~34 bits, lien `/invitation/<code>` +
      `GET /api/invitations/code/{code}`) a été ENTIÈREMENT RETIRÉ le 15/09/2026**
      (oto-backend#560, brute-forçable) — arbitrage d'Alexis : plus de mécanisme
      parallèle, le seul secret d'accès est désormais le token long (256 bits, seul son
      hash est persisté), envoyé par email avec un lien cliquable. La colonne `code` et
      son index restent en base non écrits/non lus (boot additif seulement, jamais de
      DROP — `docs/live-migrations.md`).
  - **fiche admin user** : `GET /api/admin/users/{sub}` = identité + accès effectif par provider (`status_for`) + grants + datastores + orgs (membership).
  - platform admin : `GET|POST /api/admin/orgs`, `GET /api/admin/orgs/{id}` (+ entitlements), `GET /api/admin/entitlements` (droits déclarés de toute la plateforme, filtrés et paginés — `docs/droits-declares.md`), `…/members*`, `…/secrets/{provider}`, `POST|DELETE /api/admin/orgs/{id}/entitlements/{datastore}`, `GET /api/admin/datastore-grants`, `POST|DELETE /api/admin/users/{sub}/datastore-grants/{datastore}`
  - secrets : jamais la clé en réponse (provider/base_url/set_at/set_by) ; providers per-user (slack/linkedin/google/whatsapp) refusés en `400` ; listing lu du coffre canonique `credentials_store` (legacy `org_secrets` plus dual-written sous chiffrement). Gating org_admin/membre via `org_store.get_org_role` (platform admin toujours autorisé). Révocation lazy sur sessions MCP ouvertes. Contrat front : `oto-app/docs/ORG_API_CONTRACT.md`.
- **Bibliothèque publique de guides** (marketplace de skills, table `doctrine_library`) :
  capacités `library.*` (`capabilities/guide_library.py`, montage auto MCP+REST) —
  `library.list/get` (`SUB_ONLY`, MCP `oto_procedure` op=library_list/library_get + REST
  `GET /api/me/guide-library[/{slug}]`), `library.publish` (`LIBRARY_PUBLISHER` :
  **super_admin plateforme seulement**, depuis le 14/09/2026 — MCP `oto_procedure` op=publish +
  REST `POST /api/me/guide-library/publish`), `library.fork` (`ORG_MEMBER`, palier selon le
  rôle en handler, MCP `oto_procedure` op=fork + REST `POST /api/me/guide-library/fork`),
  `library.unpublish` (auteur/PLATFORM_ADMIN, `DELETE /api/me/guide-library/{id}`).
  **Qui publie** : la bibliothèque publique est une vitrine éditée par la plateforme, donc
  seul un `super_admin` y publie. La règle est DÉCLARÉE (`_authz.LIBRARY_PUBLISHER`, plancher
  `super`) sur les deux entrées qui mènent à `_publish` — `library.publish` et la branche
  `publish` d'`oto_procedure` — pour que `capacite_autorise` annonce le droit par la même
  condition que celle qui refuse (#695) ; `_publish` garde un filet qui lève le même refus.
  Un org_admin ou un opérateur `admin` reçoit 403 `publication_reservee_a_la_plateforme`,
  AVANT l'exigence d'org active et toute lecture de la procédure source ; le refus dit ce qui
  reste ouvert — ses procédures personnelles, et le fork pour tout membre de l'org active. Un super_admin
  sans org active reçoit 400 `no_active_org` (le corps publié s'y lit). ⚠️ Côté MCP la règle
  d'autz tourne avant le handler : hors super_admin, le refus de plateforme parle le premier ;
  pour un super_admin, la garde d'agent `publication_reservee_a_l_humain` (inchangée) le
  renvoie au dashboard.
  **Auteur** = `otomata`. Une entrée signée par une `org` reste possédée par elle : la
  plateforme ne la reprend pas (409 `slug_taken`), son org_admin peut la dépublier.
  **Fork** réutilise `org_store.set_instruction` (`fork_library_entry`) → procédure
  versionnée dont le palier suit le rôle réel dans l'org active (décision du 06/10/2026) :
  procédure d'org pour un org_admin, procédure PERSONNELLE pour un membre ; `scope=user|org`
  le précise, `org` sans org_admin = 403 (le refus d'org_admin, qui nomme le palier ouvert).
  `project_id` rattache la copie au projet (lien `procedure`), écriture sur le projet
  vérifiée AVANT le fork (403 `project_not_writable`, rien de copié). **Aucune surface
  ANONYME** : les vitrines `GET /api/guide-library[/{slug}]` (marché) et
  `GET /api/guides/library[/{slug}]` (guides plateforme), servies sans jeton au build de
  oto.cx, sont retirées avec leurs alias `/api/doctrines/library[/{slug}]`
  (otomata-tech/oto#84) — elles rendaient le corps complet à n'importe qui, et la version
  servie survivait au nettoyage du dépôt. La face authentifiée `/api/me/guide-library`
  sert la ligne entière — `library.fork`/`library.unpublish` visent une entrée par `id`.
  ⚠️ Les chemins `/api/me/guide-library…` s'appelaient `/api/me/doctrines/…` jusqu'au
  2026-08-28 (#519) ; les anciens répondent **308** jusqu'au retrait — cf.
  `docs/alias-deprecies.md`.
  **`visibility`** : `public` (dans le catalogue) vs `unlisted` = **lien non listé** (style
  YouTube) — servie par `library.get` (slug exact, tout user authentifié) mais **jamais**
  listée (`list` force `include_unlisted=False`) ni servie en anonyme. Partage par lien, pas
  un secret d'org : un guide sensible ne se publie pas (reste un skill d'org privé).
- CORS : les origines que l'instance déclare (`OTO_MCP_CORS_ORIGINS`, sans défaut — cf. § CORS). `account.oto.zone` retiré (surface compte décommissionnée → dashboard.oto.ninja)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## Recettes d'une org (PROVISOIRE) — `GET /api/orgs/{id}/recipes`

Les fiches des recettes de l'org, sans corps, membre de l'org (`capabilities/recipes.py`,
`org.recipes`, REST seule) : `docs/recettes.md` §Lecture REST.

## Surface nœuds (PROVISOIRE) — `/api/me/shell`, `/api/me/nodes/{id}`, `/api/me/nodes/{id}/rows`

Trois lectures précoces du modèle de nœuds (`capabilities/shell.py`, `node_view.py`,
`node_rows.py` ; faces MCP `oto_shell`, `oto_node`, `oto_node_rows`), marquées
`x-oto-provisoire` dans l'OpenAPI : les FORMES se contractent, le stockage reste variable.
`type` est une NATURE dérivée d'un rôle (`page` | `table` | `agent` | `execution`), jamais un
`kind` de plus. 404 indistinct entre inexistant et interdit.

**La fiche dit où un nœud s'écrit : `edit_surface` (13/09/2026, oto#198).** `GET
/api/me/nodes/{id}` (et `oto_node`) sert toujours `edit_surface`, jamais `null`, dans un
énuméré fermé. C'est la surface CANONIQUE d'écriture, pas une permission : `access` reste
dans `non_servi`, et un non-propriétaire lit `node` puis reçoit 404 en écriture. L'absence de
`doc_id` n'en dit rien : une racine de projet, une procédure, un tableau converti et une
couche de contexte n'en ont pas non plus.

| stockage du nœud | `edit_surface` | s'écrit par | poignée garantie non nulle |
|---|---|---|---|
| ni `props.legacy` ni `props.delivery` | `node` | `POST /api/me/nodes/edit` | `id` |
| `legacy = doc` | `doc` | `POST /api/me/docs` | `doc_id` |
| `legacy = prj` | `project` | `oto_project` | `project_id` |
| `legacy = prc` | `procedure` | `oto_procedure` | `procedure` |
| `legacy = tbl` | `datastore` | `/api/datastores/{datastore}` | `datastore` (le NUMÉRO du tableau, #365) |
| clé `delivery` présente, quelle que soit sa valeur | `guide` | `oto_guide`, `PUT /api/me/guides/{scope}/{slug}` | aucune sur la fiche |

Dérivé en un seul point (`capabilities/node_keys.edit_surface_de`), que lit aussi la garde
d'écriture : la fiche annonce exactement ce que `POST /api/me/nodes/edit` accepte. Un stockage
incohérent — famille d'origine inconnue, `legacy` et `delivery` ensemble, poignée requise
absente — rend `500 noeud_incoherent` après la garde de lecture, jamais une fiche à poignée
nulle. La poignée `datastore` d'un tableau est son **numéro** (en chaîne), jamais son nom :
un nom se résout dans la portée de l'appelant, donc chez son homonyme (#365, 24/09/2026 —
`rev` a tourné une fois de plus pour tous les tableaux). L'ajout du champ fait tourner `rev`
une fois pour tous les nœuds.

**Écrire un nœud natif — `POST /api/me/nodes/edit`.** Créer une racine privée :
`{"op":"create","kind":"page","scope":"user","title":"…"}` → `200
{"ok":true,"id":"nod_…","op":"create"}`. Renommer : `{"op":"update","node_id":"nod_…","title":"…"}`,
même identifiant. Un `title` absent d'un update n'est pas exigé ; fourni vide ou blanc, il rend
`400 missing_title`, et il est stocké taillé, comme à la création. Refus :

- `404 not_found` — inconnu ou non propriétaire, même corps qu'à la lecture ;
- `409 node_projete` — nœud converti, jugé après la propriété ;
- `409 node_guide` — couche de contexte, jugé après la propriété ; le message nomme `oto_guide`
  et ses arguments, `details` porte `edit_surface`, `scope`, `slug`, `delivery` et, pour une org
  ou une équipe, `owner_id` ;
- `400 missing_title`, `missing_node_id`, `rien_a_ecrire`, `no_active_org`.

⚠️ **Changement de comportement** : un guide possédé s'écrivait, se déplaçait et se supprimait
ici en `200`, en contournant la borne de 65 536 octets de `oto_guide`. C'est désormais refusé ;
l'usage réel de ce passage n'a pas été mesuré. Ces refus ne sont pas déclarés dans
`/openapi.json` : le contrôle des refus déclarés (`tests/_refus_atteignables.py`) ne suit pas
l'aiguillage `_OPS[inp.op]` de la capacité.

**Un nœud `agent` porte la référence de sa procédure — depuis le 29/08 (#417).** Sur le
`RailNode` du rail comme sur la fiche : `procedure: {id, slug, scope}` — `id` est
l'identifiant STABLE que `GET /api/me/guides/{guide_id}` (et `oto_procedure`) accepte,
`slug` la référence lisible (0059-D3 : les deux, toujours), `scope` le propriétaire du nœud
(`org` | `group`), parce que le rail sert aussi les procédures d'ÉQUIPE et qu'un front sans
le scope frappe la mauvaise route de fiche. Lu dans les propriétés du nœud, jamais
reconstruit ni joint. **Absent (rail) / `null` (fiche) sur toute autre nature et sur un
agent sans référence lisible** — jamais un id deviné. Jusque-là, aucun chemin serveur ne
menait d'un nœud agent à sa fiche : son `nod_*` est dérivé (`md5('prc:' || id)`) et la fiche
de guide refuse un `nod_*` — un front devait recalculer un md5 ou apparier par titre. La
fiche de guide ne résout toujours PAS un `nod_*` (laissé de côté, cf. #417).

**`…/rows` refuse ce qu'il ne peut pas appliquer, et son `total` compte la page servie —
depuis le 01/09 (#621, relevé au passage de #418).** Trois défauts de la même famille que
`?filter=` tronqué : *un chemin qui répond juste sur ce qu'il n'a pas regardé*.

| avant | maintenant |
|---|---|
| une entrée de `filter` sans `:` était **ignorée** — la page partait non filtrée, en 200 | `400 invalid_filter`, qui nomme la forme `colonne:valeur` **et** l'entrée fautive |
| `InvalidCursor` n'était rattrapé nulle part → **500** sur un curseur tronqué ou repassé d'un régime de tri dans l'autre | `400 invalid_cursor` (« reprends la liste sans `cursor` »), le MÊME code sur les deux provenances de tableau — le natif rendait `curseur_invalide`, personne ne pouvait prévoir lequel |
| `total` était compté sur les filtres **non résolus**, la page sur les résolus | le compte passe par `store.count_rows`, qui résout comme la page |

Le troisième ne se voit que sur un schéma à **double service** (`contact1_nom` servi en
lecture pour `contacts[0].nom`, oto#22 §6) : le pied du tableau comptait un autre jeu que
celui qu'il coiffait, et rien n'échouait. Les trois refus sont **déclarés**
(`Capability.errors`), donc publiés dans `/openapi.json` : trois gestes différents derrière
un même 400, et un client ne devrait pas avoir à les distinguer en lisant une phrase.

## Renommer un chemin servi : on double, on date, on retire

Un chemin `/api/*` est un contrat avec des appelants qui vivent **hors de ce dépôt**.
Le renommer sec ne casse rien en CI — ça casse en production, chez quelqu'un d'autre,
sans trace. La forme retenue (#519, lot B) :

1. le **nouveau chemin** est la vraie route (capacité, autz, décrite dans l'OpenAPI) ;
2. l'**ancien** reste monté et répond **308** — même méthode, même corps, query string
   reportée. Ni 301 ni 302 : ils autorisent le client à retomber en `GET`, ce qui
   transforme un `POST` en no-op silencieux ;
3. il porte les en-têtes `Deprecation` / `Sunset`, est marqué `deprecated: true` dans
   `/openapi.json` avec son remplaçant et sa date, et **s'en va à une date écrite** ;
4. il est monté **en dernier** — un alias ne capture que ce que rien d'autre ne sert ;
5. le préflight `OPTIONS` n'est **jamais** redirigé, et la 308 porte les en-têtes CORS :
   un navigateur vérifie CORS sur chaque réponse d'une chaîne de redirections.

Déclaration unique : `oto_mcp/deprecations.REST` (montage `api/alias_routes.py`,
document `openapi._alias_deprecies`, garde `tests/api/test_alias_deprecies_rest.py`).
**Table des alias en cours et de leur date : `docs/alias-deprecies.md`.**

⚠️ **Quand retirer : pas de délai fixe** (Alexis, 25/09/2026). L'ancienne forme part
quand **tous les consommateurs déclarés ont suivi** — ce que la garde de contrat constate
(ci-dessous) : tant qu'un contrat épinglé lit encore l'ancienne forme, son retrait rougit
`contrat-front-code` en nommant le consommateur, et la préproduction ne part pas.

## Les consommateurs déclarés du contrat (#966)

Qui appelle cette API hors de ce dépôt se DÉCLARE dans `.github/contrat-consommateurs.json`
(nom, dépôt, chemin du contrat qu'il épingle, secret de lecture — `null` pour un dépôt
public). Les deux jobs de contrat de `deploy-canari.yml` tournent en matrice sur cette
liste, une jambe par consommateur, `fail-fast: false` : un rouge ne masque pas les
suivants, et chaque verdict **nomme** le consommateur (`scripts/contrat-front.py
--consommateur`).

- **avant déploiement** (`contrat-front-code`, une garde : `deploy-preprod` attend toutes
  ses jambes) — le document est dérivé sans serveur de `server.routes_rest`, la source du
  montage, facturation activée comme en préproduction et en production ;
- **après la préproduction** (`contrat-front`, un constat) — le document réellement servi.

Seul le CONTENU d'un contrat peut rougir. Un secret déclaré mais absent rougit aussi
(défaut chez nous, #823) ; un dépôt illisible ou un fichier déplacé chez le consommateur
avertit sans rougir (`scripts/lire-contrat-consommateur.sh`).

Déclarés au 25/09/2026 : un front tiers (extrait des opérations qu'il appelle) et le
dashboard (**l'instantané complet** de l'API : tout retrait ou changement de forme d'une
opération servie le fait rougir, qu'il l'appelle ou non, jusqu'à ce qu'il ait rafraîchi son
instantané). Déclarer un consommateur = ajouter une entrée, plus sa clé de déploiement en
lecture seule s'il est privé.

Déclaré au 28/09/2026 : **oto-commerce**, le service de facturation (dépôt privé), qui épingle
les routes `/api/service/*` qu'il appelle (`api/openapi-service.json`, secret
`OTO_COMMERCE_CONTRACT_KEY`).

### Le second contrat : l'administration (`chemin_admin`, #1196)

Le document public ne contient pas `/api/admin/*` : un consommateur qui appelle
l'administration ne pouvait ni l'épingler ni être prévenu d'une casse. Il déclare donc un
**second** contrat, `chemin_admin` dans son entrée (même dépôt, même clé de lecture) : les
opérations qu'il épingle du document d'administration (`GET /api/admin/openapi.json`,
ci-dessus). Le document est complet ; **le jugement ne porte que sur ce qu'il épingle**.

- **avant déploiement seulement** (Alexis, 09/10/2026) : `contrat-front-code` dérive les
  deux documents par import (`openapi.build(routes, admin=…)`), sans serveur ni jeton, et
  juge le second contrat dans ses propres étapes, qui tournent même si le contrat public a
  rougi (`!cancelled()`) — un rouge sur l'un ne tait pas l'autre ; le second verdict se
  nomme `« <consommateur> (admin) »` ;
- **pas après la préproduction** : le document servi exige un jeton d'admin, et **aucun
  jeton d'admin n'entre dans les secrets de la CI**. `contrat-front` ignore `chemin_admin`.

Mêmes règles de verdict que le contrat public (`scripts/contrat-front.py`), même lecteur
(`scripts/lire-contrat-consommateur.sh`, appelé une seconde fois sur `chemin_admin`) : un
`chemin_admin` absent chez le consommateur avertit sans rougir. Une instance cible
(`docs/instance-cible.md`) déclare de même `chemin_admin` dans `CIBLE_CONSOMMATEUR`, et
`scripts/contrat-cible.sh` le juge contre le document d'administration du tag.

Déclaré au 09/10/2026 : le **dashboard**, pour les opérations d'administration du cœur
qu'appelle sa console (`dashboard/frontend/openapi/oto-openapi-admin.json`, extrait par son
script d'épinglage — pas un instantané complet, contrairement à son contrat public).

## Le contrat dit ce que le serveur rend — retours d'un front tiers (29/08/2026)

Un front tiers, consommateur pur de cette API, a dérivé son comportement du contrat
servi (`/openapi.json` + docstrings) et s'est heurté à quatre endroits où **le contrat
ne disait pas, ou disait faux**. Tout est additif ; rien n'a changé de comportement.

- **`GET /api/me/nodes/{id}` rend `doc_id` et `project_id`** (`NodeOut`, face MCP
  `oto_node` idem — même vue). Ce sont les poignées que `POST /api/me/docs`
  (`op=backlinks`, `op=update`) et `POST /api/resources` (`op=get`, `resource_type:
  "project"`, `resource_id: str(project_id)`) prennent en entrée : sans elles, un nœud
  ouvert ne s'éditait ni ne se partageait. Page = les deux ; projet = `project_id` ;
  tableau / procédure = le projet qui les range, lu sur le fil ; nœud natif = `null`.
  Conséquence : `rev` a tourné une fois pour tous les nœuds.
- **Un bloc `role: list` porte `ordered: true|false`** — la liste de CE bloc est-elle
  numérotée (décidé par son premier item). Dérivé de `md` à la lecture
  (`db/blocks.ordered_of`), jamais stocké : aucune rotation de marqueur. ⚠️ Le
  docstring de `_role_de` refusait ce champ jusque-là pour une collision de sens avec
  le front (« un pas d'une suite ») ; le front l'a demandé avec NOTRE sens, le refus
  est daté et levé.
- **Une route de NATURE déclare aussi son contrat** (oto#106) : `handler.contrat =
  ContratDeRoute(description, Output, errors, corps, authentifiee)` — `/openapi.json` le
  publie à la place de la souche « écrite à la main », et ses refus passent la même garde
  d'atteignabilité que ceux des capacités. Premier cas : `/api/upload/{token}`.
- **Les refus se déclarent** : `Capability.errors=(DeclaredError(status, code,
  quand), …)` sort dans `/openapi.json` comme une réponse par statut, enveloppe
  `Erreur` (`{error, detail, details?}`, composant unique) et énuméré des `error`.
  DÉCRIT, ne fait rien : `tests/test_capability_declared_errors.py` exige que chaque
  code déclaré soit levé dans le module du handler, et chaque déclaration a son test
  qui rejoue le refus **sur la route servie** (`tests/api/test_rest_contract_front_tiers.py`,
  vrai PostgreSQL). Déclarés ce jour : `PATCH /api/groups/{id}` → **409 `group_exists`**
  (le docstring `GroupUpdated` disait le contraire — corrigé et daté) ; `PUT
  /api/me/guides/{scope}/{slug}` → **400 `body_too_large`**, borne **65 536 octets UTF-8**
  publiée en `maxLength` sur `body_md` (nécessaire, pas suffisant : un caractère
  accentué pèse deux octets) ;
  ⚠️ **`PUT /api/me/instructions/{slug}` porte le même code mais PAS la même borne
  depuis le 03/09/2026 : 131 072 octets**, le double. Deux bornes, deux raisons, et
  c'est la raison qui décide — le corps d'un **guide** est injecté dans CHAQUE session
  (sa borne protège un budget réel), celui d'une **procédure** est chargé à la demande
  par `oto_procedure op=get`. La justification historique « injecté à chaque session »
  ne valait donc pas sur ce second chemin, qui refuse d'ailleurs explicitement le slug
  du README. Relevée pour débloquer une procédure de mission qui était à sept octets
  de l'ancienne borne. 128 Ko de français ≈ 34 000 jetons, soit environ un sixième
  d'une fenêtre de 200 k — au plafond, une procédure reste chargeable avec de quoi
  travailler autour ; doubler plutôt que décupler garde la borne comme signal qu'il
  est temps de découper. ⚠️ **La garde est COMMUNE aux deux faces** (`.set`, `.create`
  et `.admin_set` passent tous par `_set_instruction`, que `oto_procedure` atteint via
  l'adaptateur MCP) : le relèvement vaut donc aussi côté connecteur, ce qui est un
  effet constaté avant, pas découvert après.
  ⚠️ **En revanche la borne n'est PUBLIÉE que sur la face REST** — mesuré le 03/09 sur
  le montage réel : la face MCP sert `body_md` en `{anyOf: [string, null]}`, sans
  `maxLength` ni description. ⚠️ **La cause n'est PAS l'aplatissement** (attribution
  corrigée le 08/09/2026) : `oto_procedure` porte `ProcedureInput`, dont
  `body_md` est un `Optional[str]` NU — il ne déclare la borne sur AUCUNE des deux
  faces. Ce sont `InstrSetInput` / `InstrCreateInput` / `AdminInstrSetInput` qui la
  portent, elles sont REST-only, et elles la déclarent en `json_schema_extra` — la
  seule chose que l'aplatissement exclut toujours délibérément (#582). Faire voyager
  les contraintes (08/09/2026) ne ferme donc pas ce manque : un agent du connecteur découvre
  toujours la borne en s'y cognant, et la fermer demande de la déclarer sur
  `ProcedureInput`. `tests/test_param_description_servie.py` fige le manque plutôt que
  de le laisser croire comblé ; `DELETE /api/me/orgs/{id}/membership` → **404
  `unknown_org`, 409 `personal_org`, 404 `not_a_member`, 409 `last_org_admin`**, dans
  l'ordre des gardes. La liste d'une opération n'est pas exhaustive : les 400 de
  l'adaptateur (`invalid_input`, `unknown_fields`, `invalid_json`, `invalid_body`) valent
  partout — le préambule du document le dit.
- **`POST /api/orgs/{id}/invitations` sur une adresse déjà membre → 409
  `already_member` ; déjà invitée (invitation encore valide) → 409 `already_invited`,
  avec `details.invitation = {id, created_at, expires_at}` pour la renvoyer — jamais son
  token** (#622, tranché et livré le 29/08/2026 ; même seam pour `oto_org op=invite`).
  L'adresse est comparée normalisée (strip + minuscules) ; « déjà membre » = un compte
  membre de l'org porte cet email ; une invitation expirée, consommée ou révoquée ne
  bloque pas (nouvelle invitation, 200). Sans adresse (lien à partager), rien à comparer.
  ⚠️ **Jusqu'au 29/08/2026 la même requête rendait 200 et une invitation DE PLUS** — la
  ligne du #618 (matin du même jour) qui déclarait ce 200 tel quel est remplacée par
  celle-ci. Accepter en étant déjà membre n'abaisse toujours pas le rôle (#297).
- **`POST /api/me/invitations/reject` → 400 `missing_token`, 410 `invalid_or_expired`,
  403 `not_the_invitee`**, dans l'ordre des gardes (#654, 2026-09-01). `410` couvre
  d'un seul jeton l'inconnue, l'expirée, la déjà acceptée et la refusée par quelqu'un
  d'autre — comme sur l'acceptation, et pour la même raison : ne pas dire à qui sonde
  lequel des quatre. `403` couvre aussi l'invitation **anonyme** (émise sans adresse) :
  elle n'est adressée à personne, donc à personne en particulier. Refuser deux fois la
  même invitation est **idempotent** (200, même réponse).
- **`POST /api/resources` déclare sa 200 en UNION DISCRIMINÉE** (#659, 2026-09-01), plus
  onze refus. La forme dépend de `resource_type` — `row_count` pour un tableau,
  `archived_at` pour un projet, `version` pour une procédure (`resource_type="procedure"`, oto#65) — donc le document rend un
  `oneOf` + `discriminator: resource_type` pour `op=get`, à l'intérieur d'un `anyOf` qui
  couvre les cinq verbes (six branches : `op=share` en a deux, grant vs publication).
  Modèles dans `capabilities/resources_contract.py`. ⚠️ **Une union PLATE aurait déclaré
  `row_count` sur un projet** : une carte qui ment est pire qu'une carte absente, un
  client généré s'y branche. ⚠️ **`capability_output_debt.txt` avait classé cette
  surface « indéclarable » le 11/08** sur une mesure JUSTE (l'intersection des sept
  `return` est vide) mais qui répondait à une autre question : une intersection vide
  disqualifie l'**enveloppe**, pas l'**union**. La leçon vaut pour `me.project` et
  `me.doc`, qui restent en dette pour la même raison mal lue.
- **Les deux pièges d'entrée sont corrigés sur une SURFACE DOUBLÉE, `POST
  /api/resources/v2`** (2026-09-01) — pas sur `/api/resources`, qui ne bouge pas.
  - `resource_type` y est **OBLIGATOIRE** (`Literal`, énuméré publié). Sur l'héritée il
    vaut `datastore_namespace` par défaut, relique du pilote ADR 0030 : un appelant qui
    vise un projet et omet le champ interroge silencieusement une autre famille, et sur
    `transfer`/`share` **agit sur une autre ressource**.
  - `resource_id` y porte un motif `^\d+$` **publié dans le schéma**. ⚠️ Ce défaut ne se
    corrigeait PAS dans le handler, contrairement à ce que l'issue supposait : la levée
    se produit dans la règle d'autz (`RESOURCE_GOVERN` → `ownership.can_govern` →
    `int(rid)`), qui tourne AVANT lui — mesuré, `owner_getter('abc')` lève `ValueError`
    pour un tableau et pour un projet (**500**, l'adaptateur REST ne rattrape
    qu'`AuthzDenied`) et rend `None` pour un guide (**403**, son owner_getter a son
    propre `isdigit()`).
  - Les deux surfaces ne déclarent donc pas les mêmes refus : l'héritée seule publie
    `unsupported_resource_type` (son champ est un `str` libre, la famille inconnue
    atteint le handler) ; la stricte la refuse à la validation.

  **Pourquoi doublée et pas durcie — et ce que la première version a raté.** #756 avait
  rendu le champ obligatoire **sur `/api/resources`**. Le champ étant déclaré sans défaut
  sur le modèle d'entrée, il devenait obligatoire sur **toutes** les op, pas seulement
  `op=get` : plus large que ce que le lot annonçait. Le journal des appels a montré de
  vrais appelants sur cette route, dont un `op=list` qui serait passé de « fonctionne » à
  « refusé » sans préavis. Reverté (#774) avant d'atteindre un tag — la rupture n'a donc
  jamais été servie en production. Arbitrage : **un contrat servi se double, il ne se
  durcit pas en place**, et l'héritée porte son défaut connu **dans sa description
  servie**, avec le nom de sa remplaçante.

  ⚠️ **CORRECTION DATÉE (2026-09-01).** Cette page affirmait que rendre `resource_type`
  obligatoire faisait *« sortir `scripts/contrat-front.py` en rouge »*. **C'est faux, et
  la façon dont c'est faux compte** : le front consommateur épingle 63 opérations et
  `/api/resources` n'en fait pas partie — mesuré, le contrôle rend le même verdict
  (sortie 0, un seul avertissement préexistant sur `POST /api/me/docs`) avec ou sans la
  rupture. **Un contrôle vert sur une route qu'il n'épingle pas ne dit rien de cette
  route.** Ce qui a réellement attrapé la rupture est le journal des appels, pas ce
  script ; et ce qui l'empêche de revenir est un cliquet dédié
  (`tests/resources_input_legacy.json`, fige le schéma d'entrée servi de l'héritée).

- **L'échéance d'un partage — `ttl_days` sur `POST /api/resources[/v2]` `op=share`**
  (otomata-tech/oto#39, 24/09/2026). Entier ≥ 1, audiences `person`/`team`/`org` ; ailleurs
  `400 ttl_days_grant_only`, illisible `400 invalid_input`. La réponse rend `expires_at`
  (null = sans échéance) ; `op=get` rend pour chaque grant `expires_at` et `expired` — un
  partage échu reste listé, marqué, et ne donne plus aucun accès. Règles complètes :
  `docs/ownership.md` § « L'échéance d'un partage ».

## Le détail de tous les connecteurs en un appel — `GET /api/me/connectors?include=card` (#1245)

Trois projections d'une même route, du plus léger au plus lourd :

| requête | ce que porte chaque ligne | mesuré (145 connecteurs, 14/10/2026) |
|---|---|---|
| `GET /api/me/connectors` | la ligne compacte : identité, axes de tri, `secret_kind`, état du membre | ~66 Ko |
| `GET /api/me/connectors?include=card` | la ligne compacte **+ la carte sans `doc_sections`** : `auth`, `credential_fields`, `connect` (avec `callback_url`, `app_ready`, `app_ready_scopes` — les niveaux `member`/`org`/`group` dont l'app est prête, `null` si le connecteur ne répond pas par niveau —, `fixed` et `warnings`), `namespaces`, `description`, `publisher`, `auth_modes`, `personal_session`, `free_tier`, `identities`, `verifiable`, `href` | ~235 Ko |
| `GET /api/me/connectors?include=card,docs` ou `?verbose=true` | la carte complète, mode d'emploi markdown (`doc_sections`) compris | ~745 Ko |

Le nombre de requêtes SQL est le même pour les trois (≈ 50, borné depuis #1148) : ce
qui distingue la carte complète, c'est son **poids**, dont 72 % de `doc_sections`. Une
page qui affiche le détail de chaque connecteur lit `include=card` **une fois**, au lieu
d'un `?verbose=true&name=<x>` par carte : ce dernier calcule en plus l'aptitude du
connecteur (`ready`, `not_ready`, `next_step`), ~47 requêtes par carte. Le mode
d'emploi d'un connecteur se lit à l'ouverture de son panneau
(`?verbose=true&name=<x>`), ou avec la liste (`include=card,docs`).

- `include` accepte `card` et `docs`, séparés par une virgule ou répétés
  (`?include=card&include=docs`). Une valeur inconnue rend `400`, jamais ignorée.
- `include` et `verbose=true` ensemble rendent `400` : le second EST déjà la carte
  complète, et l'un des deux serait ignoré en silence.
- L'enveloppe renvoie `include` (liste vide par défaut), comme elle renvoie `verbose`.
- Pour un champ repris, `include=card` sert **exactement** la valeur de la carte
  verbeuse (`tests/test_connecteurs_include_card_1245.py`).

## Une adresse web ne transporte pas de liste (#367, garde posée le 28/08)

L'adaptateur verse la query string telle quelle — `dict(request.query_params)`, donc **des
chaînes**. Pydantic coerce `str`→`int`/`bool`, **jamais `str`→`list`** : un `Input` qui
déclare `list[...]` sur une capacité liée en `GET`/`DELETE` répond `400 invalid_input`, et
le refus ne nomme même pas le champ. `?include=procedures` sur `me.project_read` a vécu
ainsi quinze jours — déclaré, testé, documenté, inatteignable (livré le 13/08 par
`c46d81e`, réparé le 28/08 par `22b7dc9`) : les tests d'alors vérifiaient que le champ
était DÉCLARÉ, personne n'avait tapé l'URL.

**Le patron de la maison** : déclarer `Optional[list[str] | str]` — c'est la forme RÉELLE
de l'entrée, pas une facilité — et normaliser une fois, au bord ; ou poser un
`field_validator(mode="before")` qui découpe. **Deux formes disent une liste dans une
URL, et les deux sont servies** :

- **la virgule** dans une valeur unique — `?include=spine,procedures` — découpée par le
  champ lui-même ;
- **la clé répétée** — `?filter=statut:actif&filter=ville:Paris` — assemblée en liste par
  l'adaptateur, dans l'ordre de l'URL, **dès que le champ déclare une liste** (`list[...]`
  quelque part dans l'annotation, `Optional`/`Union` traversés). C'est la sérialisation
  par défaut d'un paramètre `array` en OpenAPI (`style: form`, `explode: true`), donc ce
  qu'un client généré depuis `/openapi.json` envoie.

⚠️ **Corrigé le 29/08 (#418)** : jusque-là, cette section disait « la forme répétée est
inutilisable » — et c'était vrai : l'adaptateur faisait `dict(request.query_params)`, qui
ne garde que la DERNIÈRE valeur. `?filter=a:1&filter=b:2` perdait `a` **sans erreur**,
quand la face MCP recevait la liste entière : deux faces, deux résultats pour la même
demande, et l'OpenAPI promettait la forme perdue. Les deux formes se combinent
(`?k=a,b&k=c` → `["a,b", "c"]`, puis le champ découpe s'il le fait).

## Lire un tableau par son numéro : `GET /api/datastores/{datastore}`

Rend UNE entrée, à la forme d'un élément de `GET /api/datastores` (`DatastoreEntry`).
L'accès est celui de toutes les routes `/api/datastores/{datastore}/…` (possession, org,
équipe, partage à l'org, à l'équipe ou à la personne) : il ne dépend ni des listes ni de
l'org consultée. Depuis le 28/09/2026 une liste d'org ne rend ni le personnel ni les
partages faits à une personne (ADR 0030 §9) : c'est par ici qu'un écran ouvre, depuis
une org, un tableau partagé à l'appelant et lié à un projet de cette org. `shared: true`
quand le tableau n'appartient pas au contexte de l'appel, avec `permission` = le droit
effectif. Inconnu ou inaccessible : **404 `datastore_not_found`**, la même réponse dans
les deux cas. Banc : `tests/test_listes_seule_l_org.py`.

## Créer un tableau : `POST /api/datastores`

Corps : `{"datastore": "<kebab-case>"}`, plus **`owner`** optionnel —
`{"type": "org"|"group", "id": N}`. L'appartenance est vérifiée (403 sinon).

**`schema`** optionnel (oto#124) : le tableau NAÎT avec ce schéma — le même objet que le
corps de `PUT /api/datastores/{datastore}/schema`, posé par le même chemin. Une colonne
non déclarée est refusée à l'écriture : un tableau créé nu refuse sa première ligne. C'est donc le geste normal pour créer un tableau qu'on va remplir.
Refusé ⇒ `400 invalid_schema` (le message nomme la faute) et **le tableau n'est pas
créé**. La réponse ajoute alors `schema`, `enforced` et `warning` (mêmes clés que la pose).
Garde : `tests/datastore/test_creation_avec_schema_124_live.py`.

⚠️ **Sans `owner`, le tableau est PERSONNEL** : visible de son créateur seul, ni des
autres membres de l'org ni de ses administrateurs. C'est le défaut voulu, jamais
implicite (ADR 0068).

⚠️ **`X-Oto-Org` ne change pas le propriétaire.** L'en-tête décide sous quelle org on
lit et on écrit — c'est même ce que la doc du datastore recommande partout pour « agir
dans l'org » — mais il n'entre pas dans le choix du propriétaire. Créé sous cet en-tête
sans `owner`, le tableau naît personnel **et toutes les requêtes suivantes de son
créateur continuent de réussir** : rien ne cloche de son côté. Ça se découvre au second
agent, ou au collègue qui ne trouve pas le tableau et conclut qu'il n'existe pas — une
heure perdue, vécue (otomata-tech/oto#45).

**Où un tableau est LISTÉ** (décision du 28/09/2026, ADR 0030 §9) : `GET /api/datastores`
rend, dans une org, ce que possèdent l'org et ses équipes et ce qui leur est partagé ;
un tableau PERSONNEL — créé sous n'importe quel `X-Oto-Org` — ne se liste que dans
l'**org perso** de son propriétaire. Depuis une autre org, son numéro l'ouvre toujours.
C'est pourquoi l'avertissement le dit aussi.

La réponse (201) rend donc **qui possède** : `{datastore, id, url, owner_type, owner_id,
is_personal}`, plus un champ **`avertissement`** dans ce cas précis — et seulement dans
ce cas : posé quand une org était *explicitement demandée* et que le tableau naît
personnel quand même. Il n'apparaît pas quand l'org active est simplement celle par
défaut, sans quoi il sortirait à chaque création et ne serait plus lu.

Le propriétaire se décide **à la création** — c'est le geste normal, et le seul qui
n'exige rien d'autre. Il se change ensuite par TRANSFERT (`oto_resource op=transfer`,
réservé au propriétaire ou à un admin). ⚠️ Cette ligne disait « ne se change pas après
coup » : c'était faux, et l'avertissement servi le répétait — sur la face agent, où
`owner` n'existe pas, le transfert est même le SEUL geste disponible, et c'est donc lui
que le message y nomme désormais (08/09/2026).

Côté oto-core : `DatastoreClient.create_datastore(datastore, owner=None)` (v1.115.0 —
avant, la lib ne permettait pas de créer un tableau d'org).

**Une clé répétée sur un champ SCALAIRE est REFUSÉE — `400 repeated_scalar`, qui nomme
la clé** — jamais réduite à sa dernière valeur. C'est la même règle que `unknown_fields`
et que le corps illisible : refuser plutôt qu'ignorer. `filters` de
`GET /api/datastores/{ns}/rows` est un JSON dans UNE chaîne : le répéter est une
erreur de forme, pas deux filtres. Une clé inconnue répétée reste `unknown_fields`.
Garde : `tests/test_rest_query_repeated_param.py`.

**Garde au seam** : `tests/test_rest_query_list_fields.py` balaye le registre, exige une
**valeur d'exemple valide** par champ liste (`EXEMPLES`) et exerce le vrai handler avec
cette URL. Un champ liste neuf sans exemple fait rouge — c'est cette ligne qui oblige à
taper l'URL une fois, l'étape qui a manqué à #367. Trois champs concernés au 28/08 :
`me.project_read.include`, `me.search.kinds`, `me.node.rows.filter`, tous atteignables.

## Le run d'une requête — `X-Oto-Run`, `POST /api/me/runs`, `PATCH /api/me/runs/{run_id}` (oto#227, 13/09/2026)

Un consommateur REST qui réservait une ligne ne pouvait pas y écrire. La garde du bail
ne reconnaît le titulaire QUE par son **run** — ni le compte, ni le libellé `worker` —,
la réservation REST en posait un vide, et ouvrir un run n'existait qu'en MCP
(`run_start`). Le parcours :

```
POST  /api/me/runs {label, guide?}                → 201 {run_id, org_id, guide_version}
POST  /api/datastores/{ds}/claim_next             X-Oto-Run: <run_id>   → row._claimed_run, row._revision
PATCH /api/datastores/{ds}/rows/{id}?expected_revision=<_revision>   X-Oto-Run: <run_id>
POST  /api/datastores/{ds}/rows/{id}/release {worker}                X-Oto-Run: <run_id>
PATCH /api/me/runs/{run_id} {outcome, note?}       → {outcome, rows_released}
```

- **L'en-tête est jugé avant la capacité** (`capabilities/run_thread.run_de_l_en_tete`, en
  UNE requête SQL — `db.usage.run_pour_en_tete`), refus nommés et rien d'écrit : run
  inconnu ou d'un autre compte `404 run_not_found` ; org du run dont le porteur n'est pas
  membre `403 forbidden` ; `X-Oto-Org` contradictoire `400 run_org_mismatch` (l'org du run
  primerait en silence) ; run clos `409 run_closed`. Il est posé comme le MCP pose
  `_run_id`, puis remis à zéro en fin de requête. Réservation, écriture et libération ne
  changent pas : elles lisent le contexte.
- **Le run existe par ses FAITS** : ouvrir et clore écrivent les faits `run_start` et
  `run_finish` au journal, comme le MCP — un run se reconstruit du journal, jamais de
  `runs`. Le fait `run_start` porte l'EMPREINTE de la procédure exécutée : sa version et
  son palier (`guide_scope` : `user`, `org` ou `group`), résolus comme la lecture par nom
  — ma copie perso en service d'abord, puis l'org, l'équipe active en complément
  (`tools/guide_run.empreinte_de_procedure`, #1156). Clore rend les lignes que le run tenait encore (`rows_released`, `0` écrit), et
  seul le propriétaire clôt.
- **L'issue** vaut `done` · `partial` · `failed` · `blocked` (`run_status.OUTCOMES`, même
  règle que `run_finish` : `run_status.refus_de_cloture`). `partial` = terminé proprement
  mais pas tout fait : la `note` est alors exigée et dit ce qui est fait et ce qui reste
  (`400 note_required`) ; une issue inconnue `400 invalid_outcome`.
- **Jeton porté** : ouvrir et clore relèvent de la famille des tableaux — il faut écrire au
  moins un tableau (`403 token_scope_forbidden` sinon). Aucune famille ni identité nouvelle.
- ⚠️ **Un run n'est ni une identité ni un droit.** Deux sessions d'un même jeton ont le
  même compte : c'est au consommateur de lier SON run à SA session, et de ne jamais
  reprendre un `_claimed_run` lu sur une ligne. ⚠️ `_run_id` dans le CORPS d'un PATCH est
  refusé `400 jeton_mal_place` : le run se passe en en-tête.
- **Lister MES runs** : `GET /api/me/runs?limit=` (capacité `runs.mine`, membre) rend les
  derniers runs de l'appelant sous l'org active — `run_id`, `label`, `guide`, `outcome`
  (`null` = pas clos), `project_id`, bornes —, plafonné à 50. C'est le bloc C du handshake
  (`db.recent_runs`) en REST : jamais les runs d'un autre membre, l'org entière reste la
  lentille org_admin `GET /api/orgs/{id}/monitoring/runs`. La page d'onboarding la lit
  pour cocher « premier process déroulé » chez un membre.
- **Le journal estampille le run** (oto#229) : la ligne `kind='rest'` d'un appel fait sous
  `X-Oto-Run` porte son `run_id`, et l'org du run pour `org_id` — la timeline du run (et
  celle de son org) montre donc les réservations et écritures faites entre l'ouverture et
  la clôture, comme les appels MCP faits avec `_run_id`. ⚠️ Le journal
  (`api.routes.RestCallLogger`) est un middleware EXTÉRIEUR qui écrit sa ligne APRÈS que
  l'adaptateur a remis la ContextVar du run à zéro : il ne peut pas la lire. L'adaptateur
  publie donc le run JUGÉ dans le scope ASGI (`_rest_adapter.CLE_RUN`), comme
  l'authentification y publie le principal, et le journal le relit — aucune requête SQL
  de plus. Un en-tête refusé (run inconnu, étranger, clos) n'estampille rien.

Bancs : `tests/api/test_runs_rest.py` (vraie base, routes montées) ;
`tests/api/test_runs_rest_journal_229.py` (le journal).

## Descriptif OpenAPI — `GET /openapi.json` (aussi `/api/openapi.json`)

**Sans auth**, comme `/api/mcp/catalog` : un descriptif décrit des FORMES, aucune valeur.
**Dérivé à chaque requête** (`openapi.py`) de deux sources — le registre de capacités
(chemin + verbe + description + JSON Schema de l'`Input` pydantic) et la **table de routes
vivante** de l'app pour les routes encore écrites à la main (chemin + méthodes, sans schéma,
taggées `_legacy`). Rien n'est saisi à la main, donc rien ne peut mentir. `/api/admin/*` en
est retiré (console de la plateforme, pas d'intégrateur tiers).

**Le document d'administration — `GET /api/admin/openapi.json`** (#1196, capacité
`admin.openapi`, garde d'admin plateforme comme `/api/admin/capabilities`). Même recette,
`openapi.build(admin=True)` : **toutes** les opérations `/api/admin/*` et elles seules,
sans liste à tenir — une capacité neuve sous ce préfixe y entre d'elle-même. Le partage
se fait par le seul `openapi._du_document` : un chemin est dans l'un des deux documents,
jamais dans les deux, et le document public n'a pas bougé d'un octet en le séparant. Le
handler ne voit pas la table de routes (ADR 0004) ; il n'en a pas besoin, toute route
d'admin étant une capacité — `tests/test_openapi_admin_1196.py` prouve que le document
servi égale celui que la CI dérive de `server.routes_rest`.

⚠️ **À lire avant de conclure qu'une surface manque.** La consolidation ADR 0047 a déplacé le
verbe dans le CORPS : `POST /api/me/projects {"op":"list"}` n'est pas « créer un projet »,
c'est **toute** la surface projet (list/get/runs/inventory/link/publish_mcp…). Un intégrateur
qui sonde `/api/projects` obtient 404 et conclut « les projets ne sont pas sur REST » — c'est
arrivé (brief scout, 08/2026). Le descriptif rend l'énuméré `op` lisible, ce que le sondage de
chemins ne donnera jamais. Même forme pour `/api/me/docs`, `/api/me/kb`, `/api/resources`.

⚠️ **Le mode d'emploi est DANS le document servi, et ce qui gouverne un appel y est
DÉCLARÉ** (oto-dashboard#155). `info.description` porte la section « Démarrer » (jeton,
premier appel, jeton porté, choix de l'org, verbe dans le corps) : elle vivait dans le
build de docs.oto.cx, collée devant la description, donc invisible à un générateur de
client ou à un agent qui lit le contrat. Les en-têtes de contexte `X-Oto-Org` et
`X-Oto-Group` sont des composants (`XOtoOrg`, `XOtoGroup`) référencés par chaque
opération que `ViewAsMiddleware` précède — capacités ET routes écrites à la main, pas
les alias 308 — avec leur valeur par défaut (l'org maison). La règle : **rien de ce qui
décide d'un appel n'existe seulement dans une page** ; si un site a besoin d'ajouter au
document pour être utilisable, c'est le document qui est incomplet. Aucune adresse
d'instance dans la prose : les exemples notent `$OTO` l'adresse de `servers`.

## Version servie — `GET /api/version` (+ `X-Oto-Version`)

**Sans auth**, comme le descriptif ci-dessus : un ref git, un SHA, deux horodatages —
aucune valeur. Il rend l'étiquette de ce que **le processus exécute** (`v1.2.3+6d5bf16b`),
le tag **oto-core réellement installé**, l'instant du déploiement comme celui du
démarrage, et l'empreinte des dépendances installées avec leur conformité au verrou
`uv.lock` (`deps_sha`, `lock_sha`, `deps_conformes`, relevés au démarrage — #932). Son
contrat est déclaré (`ContratDeRoute`) : le schéma de la réponse est publié. La même étiquette part dans `info.version` de l'OpenAPI et en **en-tête
`X-Oto-Version` de chaque réponse** — l'endpoint sert à qui pense à demander, l'en-tête
à qui relit son journal après coup.

⚠️ Elle ne désigne PAS ce que le dernier workflow a déployé, et ⚠️ **`pip show oto-core`
ment** (numéro gelé à 1.100.0). Le pourquoi de chaque refus, l'ordre de résolution
(env > `.oto-deploy.json` > `"unknown"`) et le piège des deux copies du script de
déploiement : **`docs/version-servie.md`**.

## Jetons API `oto_` — gestion et portée

- **La gestion des jetons demande une session interactive.** `GET|POST /api/me/tokens`,
  `DELETE /api/me/tokens/{id}` et leurs miroirs admin `/api/admin/users/{sub}/tokens*`
  refusent un porteur de jeton (`403 api_token_forbidden`) : seul un JWT Logto y passe.
  Sinon une fuite est **auto-entretenue** — l'attaquant s'émet un second jeton (non-expirant)
  avant qu'on révoque le premier, et peut révoquer les jetons légitimes. Émettre un jeton
  redevient un acte humain, ce qui borne la gravité réelle d'une fuite à la portée du jeton.
- **Exception : le jeton ÉMETTEUR** (décision d'Alexis du 08/10/2026 — que les agents
  n'aient plus besoin d'un humain à chaque clé, modèle du jeton Cloudflare « API Tokens:
  Edit »). `POST /api/me/tokens {"scopes": {"issue": {"namespaces": {"204": "write"}}},
  "ttl_days": 180}` rend un jeton qui émet à son tour, sans session, des jetons bornés par
  son **plafond** (`issue`, tableaux et projets seulement, rangés par identifiant). Le
  plafond peut aussi nommer une **org entière** (même jour, choisir les tableaux un par
  un était le geste à supprimer) : `{"issue": {"orgs": {"2": "write"}}}`. L'enfant nomme
  toujours ses tableaux et projets par identifiant, et chacun doit **appartenir** à l'org
  ou à l'une de ses équipes — jamais un objet personnel, même rangé dans l'org. Le porteur
  doit être membre de l'org nommée (`400 unknown_org`). L'émetteur envoie `X-Oto-Org` :
  c'est dans l'org active que se résolvent les tableaux de l'enfant. Ce qui tient le
  motif de la règle ci-dessus :
  - il ne naît que d'une session humaine, et jamais sans échéance
    (`400 issuer_ttl_required`, aux deux paliers) ;
  - il ne franchit que les trois routes MEMBRE (`allow_issuer_token`), jamais l'admin ;
    tout autre jeton y reste refusé, porté ou non (`403 api_token_forbidden`) ;
  - ce qu'il émet est **toujours porté**, inclus dans son plafond, ni émetteur ni runner
    (`403 scope_exceeds_issuer`), et échéancé au plus 90 jours (`400 issued_token_ttl`) ;
  - il ne liste et ne révoque que **ses** enfants ; chacun le nomme (`parent_id`, rendu
    par la liste) ;
  - un enfant **meurt avec son parent**, révoqué ou échu (`verify_api_token` le vérifie à
    chaque requête), et révoquer le parent révoque ses enfants actifs, motif
    « émetteur N révoqué ». Colonne `user_api_tokens.parent_id`, révision Alembic
    `0045_jetons_emetteurs` ;
  - chaque émission est journalisée comme tout appel REST par jeton, sous l'id du parent.
- **Clés d'API d'ORG** (#1188, `docs/cles-d-org.md`) — `GET|POST /api/orgs/{id}/api-keys`
  et `DELETE …/{token_id}`, réservées aux **admins de l'org**, session interactive seule
  (`allow_api_token=False`), création refusée sur une org archivée. Une clé d'org agit au
  nom du **compte de service** de l'org (`org-<id>`, `<tenant>:org-<id>` pour une org de
  tenant), membre simple de son org et de nulle autre : elle voit ce que l'org possède et
  ce qu'on lui partage, jamais les objets personnels des membres, et survit aux départs.
  `scopes` est **requis** (`400 scopes_required`), sans `runner` ; un plafond `issue` en
  fait une clé émettrice, qui gère ses enfants par `/api/me/tokens`. Chaque clé et chaque
  enfant porte le verrou d'org. `kind='org'`, `created_by` = l'admin émetteur (révision
  Alembic `0047_cles_d_org`) ; la liste rend aussi les enfants (`parent_id`). L'archivage
  de l'org révoque ses clés ; la désactivation de son tenant aussi, par le préfixe du sub.
- **Portée opt-in** (`auth/token_scopes.py`, colonne `user_api_tokens.scopes` JSONB) : à la
  création, `POST /api/me/tokens {"label":"scout", "scopes":{"datastores":{"leads":"read"}}}`
  rend un jeton **porté** — deny-by-default, il n'ouvre QUE les tableaux nommés, en `read`
  ou `write` (write ⊃ read), et **rien d'autre** : ni `/api/me`, ni les connecteurs, ni les
  projets, ni la gouvernance du tableau (créer/supprimer/renommer/partager). Hors portée →
  `403 token_scope_forbidden`. C'est la forme à confier à une intégration tierce ; sans elle,
  un jeton **est** le sub et ouvre toute l'organisation.
  - `scopes` absent ⇒ jeton NON porté = comportement historique. Aucun jeton existant n'est
    touché, aucune migration.
  - Seule réponse **filtrée** plutôt que refusée : `GET /api/datastores` rend les
    tableaux de la portée, droits **rabattus** sur ceux du jeton (`permission`/`can_write`/
    `can_govern`) — sans lui une intégration n'aurait pas le schéma de son tableau
    (`page_rows` ne le rend pas) et ne pourrait pas peindre ses colonnes.
  - La table des routes autorisées (`auth.token_scopes._ALLOWED`) est la **seule** porte : une
    route ajoutée demain est refusée sans qu'on ait à y penser.
  - La portée nomme le tableau par son **identifiant** (oto#158, 29/09/2026) : un renommage
    ne déplace plus ce que le jeton atteint. L'émission accepte encore un nom et le range
    sous son identifiant (la réponse rend la portée rangée ; un nom porté par deux tableaux
    visibles → `400 ambiguous_namespace`). Une URL qui adresse le tableau par son nom est
    jugée sur l'identifiant que ce nom résout pour le porteur, jusqu'au 08/11/2026. ⚠️ Une
    portée émise avant qui nomme encore un tableau par son nom n'ouvre plus rien :
    `403 token_scope_by_name`, qui les liste — elle se migre
    (`scripts/tableaux_par_numero.py --seulement jetons`) ou se réémet.
- **`label` : au plus 32 caractères, REFUSÉ au-delà** (`400 label_too_long`, qui donne la
  longueur reçue **et** la borne). Jusqu'au 03/09/2026 il partait en base en `strip()[:32]`
  sans que rien ne le dise, **et la réponse rendait le brut de l'appelant** — sur un libellé
  long, l'émetteur repartait donc avec la confirmation d'une valeur qui n'existait nulle part,
  puis retrouvait 32 caractères dans `GET /api/me/tokens`, c'est-à-dire sur la liste même où
  l'on décide de révoquer. La colonne est en `TEXT` : cette borne est un choix de surface,
  donc elle est **publiée** (`maxLength` dans le schéma servi, pour qu'un front tiers en
  dérive sa garde de saisie) et un dépassement se refuse au lieu de se raboter — une coupe
  sur une ÉCRITURE ne se répare pas par un drapeau, la fin ne survit nulle part (oto#42,
  4ᵉ règle ; le raisonnement complet est dans `conventions.md`, « Projeter ≠ tronquer »).
  La réponse rend désormais **le libellé écrit**, jamais le brut. ⚠️ 32 reste court et n'a
  jamais été calibré : la coupe muette empêchait de l'apprendre (un libellé raboté ne produit
  pas de plainte, il produit des listes de jetons qu'on ne sait plus distinguer). Un refus, lui,
  se signale — si la borne gêne, on le saura, et la relever ne coûte qu'une constante.

## Descriptif dérivé + jetons portés (03/08)

> **Descriptif dérivé + jetons portés (03/08).** `GET /openapi.json` (et
> `/api/openapi.json`) sert un OpenAPI **dérivé** du registre de capacités + de la table
> de routes vivante (`openapi.py`) — sans auth, `/api/admin/*` exclu. Il existe parce que
> la surface était *indescriptible* : après l'ADR 0047, le verbe vit dans le corps (`op`),
> donc un intégrateur qui sonde `/api/projects` tombe sur 404 et conclut « pas de REST »,
> alors que `POST /api/me/projects {"op":"list"}` sert tout le métier projet. Côté sécurité,
> deux crans sur les jetons `oto_` : leur **gestion** exige une session interactive
> (`allow_api_token=False` sur `/api/me/tokens*` + miroirs admin — un jeton ne fabrique
> plus de jeton, une fuite n'est plus auto-entretenue), et un jeton peut naître **porté**
> (`auth/token_scopes.py`, `user_api_tokens.scopes`) : deny-by-default borné à des tableaux
> nommés en read/write. `scopes` NULL = jeton historique, inchangé. Depuis le 03/08 la
> portée nomme aussi des **projets** (`{"projects": {"12": "read"}}`), servis par
> `GET /api/me/projects/{id}` — la forme POST porte sa cible dans le CORPS, donc aucune
> portée ne peut la borner : **ce qu'un jeton porté atteint doit se lire dans le chemin.**
> C'est la règle à garder en tête avant d'ouvrir une nouvelle surface aux intégrations.

## Routes lourdes — débit par jeton, concurrence par route (#1141, infra#9)

Le 04/10/2026, un seul client a appelé une route d'agrégat plusieurs centaines de fois
par minute ; le pool de connexions s'est vidé pour tout le serveur, et des routes qui
n'avaient rien demandé ont répondu 500. `api/routes_lourdes.py` borne les routes
**déclarées** dans `ROUTES_LOURDES` — le seul endroit où une route devient lourde, avec
sa raison écrite. Deux bornes, sans attente, avant l'authentification et le handler :

1. **débit par jeton** (`par_minute`, fenêtre glissante de 60 s, clé = empreinte du
   jeton, jamais le jeton) → `429 rate_limited`, `Retry-After` = le temps que le plus
   ancien appel sorte de la fenêtre ;
2. **concurrence par route** (`concurrence`, par processus) → `503 route_busy`,
   `Retry-After: 2` ; un appel refusé ici ne consomme pas de débit.

Corps `{error, detail, details: {retryable, retry_after_seconds, …}}` et en-têtes CORS
de l'origine. Middleware ASGI `GardeRoutesLourdes`, monté juste sous `RestCallLogger` : le
refus est journalisé, et cette ligne de journal est tout ce qu'il coûte à la base. ⚠️ Compteurs **par processus** : une protection du pool, pas un
quota. ⚠️ `POST /api/me/runner/jobs` n'y est pas : elle a été la victime du pool vide,
pas sa cause, et ses appelants sondent à cadence régulière. Preuves :
`tests/api/test_routes_lourdes_1141.py`.

**Les routes déclarées (#1145)**, réglées sur les consommateurs réels recensés le 04/10 :

| route | par minute / jeton | concurrence | pourquoi, en bref |
|---|---|---|---|
| `GET /api/orgs/{id}/usage/calls` | 900 | 8 | relevé de facturation d'un tenant : ~17 outils × pages toutes les 120 s, 4 en parallèle, jusqu'à 600/min observés — le débit est AU-DESSUS, la concurrence garde le pool |
| `GET /api/orgs/{id}/usage/tools` | 120 | 4 | une lecture agrégée pour tous les outils |
| `GET /api/me/instructions/{slug}/usage` | 60 | 4 | agrégat 30 j, une lecture par fiche de procédure |
| `GET /api/me/instructions-usage` | 60 | 4 | agrégat 30 j de toutes les procédures de l'org, une lecture par liste |
| `GET /api/me/activity-summary` | 60 | 6 | lue à chaque chargement de page par le front d'un tenant |
| `POST /api/me/projects` | 240 | 8 | ouvrir un projet déclenche plusieurs ops ; écritures comprises |
| `GET /api/admin/monitoring/{summary,rest,connectors,funnel}` | 30 | 2 | supervision plateforme, admin seul |
| `GET /api/orgs/{id}/monitoring/summary` | 30 | 4 | supervision d'org |
| `GET /api/admin/tenants`, `GET /api/admin/tenants/{slug}` | 30 | 2 | lectures de toute la fenêtre du journal |

Non déclarées, et diagnostiquées (#1148) : `GET /api/me/connectors` et
`POST /api/me/runner/triggers`. Leur lenteur venait de lectures par ligne, chacune sur
son emprunt au pool — pas d'un client qui martèle (moins de 150 appels par jour chacune,
lues à l'ouverture d'un écran du tableau de bord). Corrigées à la source
(`docs/event-loop-perf.md` §Une lecture par ligne) : la première tient UNE connexion
par appel, la seconde quelques lectures quelle que soit la longueur de la liste. Une
concurrence bornée ajouterait un `503 route_busy` sur un geste d'écriture
(`op=update`, `delete`) sans protéger le pool de rien de plus.
Chaque gabarit est confronté à la table figée des routes
(`tests/api/test_routes_lourdes_declarees_1145.py`).

## Limite de débit applicative — tout ce que sert le processus (F-025)

Avant elle, rien dans l'application ne bornait `/mcp`, le reste de `/api/*`, les
webhooks ni les routes d'authentification : un scanner, un fuzzer ou un seul client qui
martèle dégradait le service de tous (serveur mono-loop, pool partagé).
`oto_mcp/limite_debit.py` (`LimiteDebit`, posé dans `server.build_root_app` sous la garde
de déconnexion et au-dessus du compteur des refus du transport, donc sur les deux faces
MCP et toute la face REST) range chaque requête dans UNE classe, sans rien vérifier ni
lire en base, et lui fait payer un seau à jetons (débit régulier, rafale = la moitié) :

| classe | quand | clé | défaut / min |
|---|---|---|---|
| `jeton` | la requête porte `Authorization` | empreinte du jeton | 1 200 |
| `sensible` | sans jeton : secret dans le chemin (`/api/invitations/*`, `/api/public/*`, `/p/d/*`, `/o/{u,d,r}/*`) ou geste d'auth interactif (`/api/mcp/auth_callback`, `*/oauth/callback`) | adresse | 60 |
| `webhook` | `/api/hooks/*`, `/api/receivers/*`, `/api/billing/webhook` | (adresse, chemin), plus un plafond par adresse à 10 × ce débit (`webhook_adresse`) | 300 |
| `anonyme` | tout le reste sans jeton (`/.well-known/*`, inscription du shim anonyme d'un projet publié — un client hébergé s'inscrit à chaque connexion, depuis sa sortie partagée —, `/mcp` avant connexion, upload signé, relais) | adresse | 600 |
| `echecs` | chaque 401/403 (et 404 sur `sensible`) en consomme un ; épuisé, l'adresse voit refuser ses jetons **inconnus** et ses requêtes `sensible` | adresse | 120 |

- **Pourquoi un principal, et pas une adresse, pour le trafic authentifié** : un client
  hébergé sert tous ses utilisateurs depuis quelques adresses ; une borne par adresse les
  couperait ensemble. **Pourquoi `echecs`** : la classe `jeton` se décide à la PRÉSENCE
  de l'en-tête — un `Bearer <aléatoire>` par requête aurait chacun son seau neuf.
- **Ce que `echecs` ne touche jamais** : un jeton déjà accepté par ce processus (ni
  refusé, ni débiteur : le 401 d'un jeton expiré ne coûte rien à ses voisins
  d'adresse), la classe `anonyme` (la découverte OAuth), la classe `webhook` (un
  envoyeur partagé retente des jours sur un crochet supprimé : ses 404 n'épuisent pas
  l'adresse de mille autres clients), une adresse exemptée. ⚠️ Prix assumé : depuis une
  adresse au budget épuisé, le premier appel d'un jeton tout juste émis est refusé
  jusqu'à ce que le budget remonte.
- **L'adresse** est la règle unique du dépôt (`client_trace.pick_ip`) : le pair TCP ;
  les en-têtes seulement si ce pair est un proxy déclaré dans `OTO_PROXIES_DE_CONFIANCE`
  (adresses ou CIDR, lu au démarrage, illisible = refus de démarrer), et alors
  `CF-Connecting-IP` > adresse la plus à droite de `X-Forwarded-For` hors des réseaux de
  confiance. ⚠️ Sans ce réglage, derrière un proxy, TOUT le trafic a l'adresse du proxy.
  ⚠️⚠️ Elle ne vaut que ce que vaut le proxy : derrière un proxy déclaré qui n'ÉCRASE pas
  ces deux en-têtes avec le pair qu'il a vu, un client choisit son seau. D'où le défaut **`report`** :
  **`enforce` ne se pose QUE derrière un proxy qui écrase les deux en-têtes** (nos boxes
  depuis F-024), instance par instance, dans sa configuration.
- **Adresses exemptées** (`OTO_DEBIT_ADRESSES_EXEMPTEES`, adresses ou CIDR) : nos propres
  machines sortent des classes par adresse et du budget d'échecs ; leurs jetons paient
  toujours `jeton`. Elles se déclarent dans la configuration de l'instance, jamais dans
  ce dépôt.
- **Refus** (mode `enforce`) : `429 rate_limited`, `Retry-After`, corps `{error, detail,
  details: {retryable, retry_after_seconds, limit_per_minute, scope}}`, en-têtes CORS de
  l'origine et `X-Oto-Version`. Décidé avant d'appeler la suite ; rien n'est mis en
  tampon (le SSE passe intact) ; rien n'est écrit en base sur ce chemin ; une ligne de
  journal par clé et par minute, jamais le jeton.
- **Mesure** (`oto_mcp/limite_debit_mesure.py`) : par classe et par minute, en mémoire,
  `allowed` / `would_refuse` (mode `report`) / `refused` (mode `enforce`) et `peak_pct`
  (plus haut usage d'un seau, en % de sa rafale, toutes clés confondues). Au plus UNE
  ligne par classe et par minute part dans le journal d'appels (`kind='debit'`,
  `tool='debit:<classe>'`, jamais une adresse ni un jeton), en tâche de fond quand la
  minute change. Lue par `oto_admin_monitoring op=transport` (REST
  `GET /api/admin/monitoring/transport`), champ `rate_limit`, par environnement, classe
  et mode : en `report`, `would_refuse` est ce qu'`enforce` couperait — c'est sur lui
  qu'on règle les bornes avant de basculer.
- `OPTIONS` passe toujours. Les bornes plus fines s'appliquent EN PLUS : routes lourdes
  (ci-dessus), seau du relais sur `/oauth/token`, seau des projets publiés.
- ⚠️ Compteurs **par processus** (bleu/vert, canari : chacun les siens) : une protection
  du service, pas un quota.
- **Réglages, lus au DÉMARRAGE — en changer demande un redémarrage** : `OTO_DEBIT_MODE`
  = `report` (défaut) | `enforce` | `off` ;
  `OTO_DEBIT_{JETON,ANONYME,SENSIBLE,WEBHOOK,ECHECS}_PAR_MIN`, `0` désarme la classe ;
  `OTO_DEBIT_ADRESSES_EXEMPTEES`. **Un réglage illisible empêche le démarrage**
  (`ReglageDebitInvalide`) : un mode inconnu, une borne non numérique ou négative, une
  adresse exemptée qui n'est ni une adresse ni un CIDR — jamais un défaut deviné.
  Preuves : `tests/test_limite_debit.py`, `tests/test_transport_refusals_db.py`.

### Deux couches : le proxy, puis l'application (décision du 13/10/2026)

La limite de débit vit à deux endroits, chacun pour ce qu'il sait faire :

| couche | où | ce qu'elle arrête | ce qu'elle ne sait pas faire |
|---|---|---|---|
| **proxy** | le Caddy devant chaque instance, dans la configuration de l'infra de l'instance | les inondations par adresse, toutes instances et couleurs confondues ; les corps trop gros, avant qu'ils n'atteignent le processus | compter par jeton, distinguer une route sensible d'une autre, savoir qu'une réponse est un 401/403 |
| **application** | `LimiteDebit`, ci-dessus | les quotas par jeton, le seuil serré des routes sensibles, le budget d'échecs 401/403 par adresse | voir une inondation dans son ensemble : ses compteurs sont par processus |

- **Mise en service** : l'application tourne en **`report`** (elle mesure, ne refuse
  rien). On ne passe une instance en `enforce` qu'après une semaine de mesures
  (`would_refuse` par classe, `oto_admin_monitoring op=transport`), et seulement derrière
  un proxy qui écrase les en-têtes d'adresse.
- **Ni l'une ni l'autre ne remplace l'autre** : retirer le proxy laisserait passer une
  inondation répartie sur les processus ; retirer l'application perdrait tout ce qui se
  compte par jeton.

## CORS — les origines se DÉCLARENT, il n'y a plus de liste dans le code

⚠️ **CORS : aucune liste dans le code** (#968, décision du 28/09/2026). `_allowed_origins()`
(`api/base.py`) lit `config.cors_origins()`, c'est-à-dire `OTO_MCP_CORS_ORIGINS` — sans
défaut : absente ou vide, l'instance refuse de démarrer (`identite_instance.verifier`).
La liste en dur qui servait de repli mêlait nos domaines, ceux d'un front tiers et des
ports de développement ; elle était déjà morte en prod et en preprod (les deux box posent
la variable), elle n'accueillait plus que les environnements neufs — en pointant chez
nous. **Ajouter une origine = éditer l'env des box + restart** (`/opt/oto-mcp/.env`,
`/opt/oto-mcp-canari/.env`) ; en développement, le `.env` du poste porte ses ports
`localhost`. Diagnostic en 1 appel, sans lire le `.env` : `curl -X OPTIONS
https://mcp.oto.cx/api/mcp/catalog -H 'Origin: <x>'` → l'en-tête `Access-Control-Allow-Origin`
revient si l'origine passe.

⚠️ **Le CORS se pose RÉPONSE PAR RÉPONSE — il n'y a pas de `CORSMiddleware`.** Une
`Response` construite à la main sort donc sans en-tête, et le navigateur la jette
alors que le serveur a répondu 200 : rien n'apparaît dans le journal serveur, tout
apparaît côté client en « Failed to fetch ». Mesuré en production le 2026-09-09 sur
`GET /api/me/billing/invoices/{id}/pdf`, où le préflight et les erreurs JSON
passaient et où seul le 200 qui portait le fichier était nu. **Toute réponse non
JSON passe désormais par `api/base.py::_file`** (PDF de facture, export ZIP,
favicon, markdown public), qui pose le CORS et compose le `Content-Disposition` ;
`tests/api/test_reponses_binaires_cors.py` refuse la prochaine `Response` écrite à
la main. `Content-Disposition` est aussi dans `Access-Control-Expose-Headers` :
sans quoi le front télécharge un fichier qu'il ne peut pas nommer.

⚠️ **Et le piège SYMÉTRIQUE, constaté le 03/09/2026 : conclure qu'une origine est
BLOQUÉE sans le vérifier.** Un lot a retiré `POST /api/contact` en motivant le
retrait par « la route était de toute façon morte, l'origine du site n'est pas dans
`OTO_MCP_CORS_ORIGINS`, donc le navigateur bloquait avant d'arriver ici ».
**Mesuré en production le jour même : l'origine EST autorisée**, le serveur rend son
`Access-Control-Allow-Origin`, et une origine inconnue ne l'obtient pas — le contrôle
est donc bien discriminant. La route était vivante. Ce qui rendait le retrait sûr
était autre chose, et de vérifiable : le formulaire du site avait basculé vers le
service de messagerie une minute plus tôt.

Le geste était bon, le motif était faux — et c'est le motif qui voyage. « Morte par
blocage d'origine » resservira à retirer une autre route, et sera faux la prochaine
fois aussi. Le diagnostic est le même `curl -X OPTIONS` d'un paragraphe plus haut, il
coûte une seconde, **et il répond dans les DEUX sens** : ce qui vaut pour « je crois
que ça passe » vaut pour « je crois que c'est bloqué ». Une croyance qui autorise un
retrait mérite le même contrôle qu'une croyance qui autorise un ajout.

## Le `content-type` d'une réponse JSON porte son charset (#472, 29/08)

`api.base._json` construit une `JSONResponse` : son `media_type` ne commence pas par
`text/`, donc **Starlette n'y ajoute aucun charset de lui-même**. Depuis #472, une couche
ASGI unique (`oto_mcp/response_charset.py`, posée dans `server.build_root_app`) complète
l'en-tête de toute réponse `application/json` ou `text/event-stream` en
`; charset=utf-8` — `/api/*` et `/mcp` par le même geste.

Elle **complète, elle ne réécrit pas** : un `content-type` qui porte déjà un `charset=`
(ex. `text/markdown; charset=utf-8` d'`api/public.py`) est laissé intact à l'octet près,
et les réponses binaires (`application/pdf` d'une facture, `application/zip` d'un export,
`image/svg+xml`) ne sont pas touchées. Un handler n'a donc **rien à faire** pour en
bénéficier — et rien à défaire s'il pose son propre charset. Le pourquoi (défaut
ISO-8859-1 de HTTP/1.1 pour `text/*`, constantes en dur du SDK MCP) est dans
`docs/mcp-spec-watch.md` §Relevé 4.

## Un nom est une donnée, servie telle quelle (relevé du 29/08/2026)

Un front tiers a signalé « `&` arrive `&amp;` dans `/api/me/shell` et `/api/me/nodes/{id}`
— double échappement à l'écriture côté backend ». **Faux, et prouvé sur le chemin servi**
(`tests/api/test_nom_est_une_donnee_servie_telle_quelle.py`, vrai PostgreSQL) : un groupe,
un projet et une page nommés `Finance & Administratif <R&D> "Q1"` ressortent au caractère
près sur chaque route de lecture, corps JSON brut sans entité. Le relevé en base : **0**
groupe et **0** org avec une entité HTML dans le nom, **1** projet et **5** pages — et le
journal des appels montre `&amp;` **dans les arguments reçus** (`oto_doc` / `oto_project
op=create`), le `brief_md` du même appel portant un `&` nu : c'est le client qui échappe son
champ `name` à l'écriture. Le serveur stocke ce qu'il reçoit, ce qui est son rôle ; il ne
déséchappe rien à la lecture, et **aucune réparation en base n'a été faite** — six titres
envoyés ainsi par leurs auteurs (trois d'une session de test, trois d'un agent), à corriger
par eux s'ils le veulent. L'échappement appartient au RENDU HTML (`share_ui`,
`public_doc_page`, l'email), jamais au stockage ni à une réponse JSON.
