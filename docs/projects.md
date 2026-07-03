# Projet — couche d'organisation (ADR 0030/0032)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Conteneur de travail **possédé** (owned resource ADR 0030) : un but + ses entités. Tables
`projects` (owner_type/owner_id, `brief_md` = doc d'entrée, soft-delete `archived_at`),
`project_links` (pointeur typé `target_type∈{tableau,procedure,connecteur,base}` + `target_ref`
+ label, pas de FK cross-store), `docs` (pages markdown **en arbre** `parent_id`, héritent de
l'accès du projet — pas d'ownership propre), `project_activity` (journal best-effort).
Capacités co-déclarées : **`oto_project`** (`capabilities/projects.py`, op create/list/get/
update/archive/link/unlink/activity, `POST /api/me/projects`), **`oto_doc`** (`capabilities/
docs.py`, op create/list/get/update/delete/move, `POST /api/me/docs`). Partage/transfert via
**`oto_resource`** (resource_type=`project` ajouté au dispatch `_OPS`).

> **Push out-of-bande de gros contenu par un agent (issue #105).** Écrire un GROS contenu
> via `oto_doc(body_md=…)`/multipart le fait transiter INLINE par le contexte du LLM (coût
> tokens + troncature/paraphrase sur du verbatim). **`oto_upload_url(target)`** (capacité
> `me.upload_url`, MCP-only) rend une **URL signée** à usage unique + TTL court (15 min) sur
> laquelle l'agent PUT le contenu **hors-bande** (`curl --data-binary @fichier`) ; le backend
> matérialise dans la cible (`PUT /api/upload/<token>`) et renvoie un accusé léger (id +
> longueur), **jamais le body**. Le jeton (`upload_tokens.py`) est **stateless** — HMAC
> signant `{typ:upload, jti, sub, org, target, exp}` (secret `OTO_MCP_OAUTH_STATE_SECRET`) —
> et **scelle la cible** (jamais acceptée d'un param client à la réception : verrou IDOR) ;
> l'autz d'écriture est **réappliquée** à la réception (`ownership.can_access(project, write)`)
> et l'usage unique est garanti par la table `upload_tokens_used` (`db.consume_upload_token`,
> consommée AVANT matérialisation → anti-rejeu). Cibles : `target='doc'` (op create/update
> d'une page Documents) et `target='project_file'` (fichier brut « Autre document » — comble
> le gap upload multipart dashboard-only : un agent peut déposer un PDF/CSV, plafond
> `OTO_MCP_UPLOAD_MAX_BYTES`, déf. 25 Mo). L'upload multipart humain (`POST /api/me/projects/
> {id}/files`) reste la voie dashboard.

> **Livraison d'un projet COMPLET vers l'org d'un client (otomata-private#52).**
> `oto_resource` : share/unshare acceptent un principal **org** (`org_id`, sans exigence
> d'appartenance — on donne un accès) ; **`cascade=true`** sur share/transfer d'un projet
> répercute le geste sur les `project_links` avec rapport par entité — **tableau** = même
> geste (grant/transfert du namespace), **procédure** = grant `read` au partage (modèle
> licence : oto garde le master) / **copie chez la cible + re-pointage du lien** au
> transfert (`org_store.copy_instruction_to_org`, l'originale intacte), **connecteur** =
> `recipient_credential` (le client branche SA clé ; la surcharge identité/instructions du
> lien voyage avec le projet) ; docs/fichiers suivent d'office (héritage d'accès). Kind
> **`doctrine`** enregistré sur la primitive ownership (owner **dérivé** d'`org_instructions.
> org_id`, resource_id = id surrogate) → lecture cross-org **par id** `oto_get_doctrine(
> doctrine_id=…)` / `GET /api/me/doctrines/{id}`, gatée `ownership.can_access`. Un projet
> livré remonte chez le client dans `oto_project(op=list)` (flag `shared`+`permission`) ET
> dans le bloc C du handshake (#50) — ouvrable en un message. Reste à cadrer : push des màj
> post-livraison (re-share = re-grant idempotent, mais pas de notification). UI : `oto-dashboard`
`/projects` + page dédiée `/projects/:id` (`ProjectDetailView`, ADR 0030). Reliquats du modèle
(MCP-App rendu, édition temps réel/lock, pré-set vendable=copie) **non faits**.

> **Partage public CHIFFRÉ d'un projet (ADR 0032 §3, zero-knowledge).** Un projet peut être
> publié en lecture seule derrière un lien, avec le contenu **chiffré côté navigateur**
> (AES-256-GCM, `oto-dashboard` `lib/crypto.ts`). Le backend ne stocke QUE le ciphertext
> (`project_public_shares`, une part par projet, token opaque) — la clé vit dans le **fragment**
> de l'URL (`/p/p/<token>#<clé>`) et n'atteint jamais le serveur → la plateforme ne peut pas lire
> un projet partagé. **REST-only** (le ciphertext vient du front, l'agent MCP ne peut pas chiffrer
> dans le navigateur → pas de binding MCP, esprit « secret jamais en argument MCP ») :
> `POST|DELETE /api/me/projects/{id}/public-share` (autz `can_access write`) + lecture publique
> sans auth `GET /api/public/projects/{token}`. Re-publier fait **tourner** le token+la clé
> (ancien lien caduc). `oto_project(op=get)` expose `public_shared`/`public_shared_at` (présence
> seule, jamais la clé). Pendant du partage public de doc rendu (#4a) mais **chiffré**.

> **Endpoint MCP par projet — `<slug>.mcp.oto.cx` (ADR 0032, amende #44).** Un projet
> se **publie** comme serveur MCP dédié sur son propre sous-domaine (le « preset » de
> l'ADR 0032 §7). Colonnes `projects.mcp_slug`/`mcp_access`(`off|anonymous|secret|org`)/`mcp_tools[]` ;
> capacité `oto_project` op **`publish_mcp`/`unpublish_mcp`** (autz `can_govern`) ; **sonde de
> publication NON bloquante** (`_mcp_unresolvable_tools`) : pour un preset sans login, les tools
> **non credential-less** (`secret_kind≠none`) ou dont le connecteur n'a pas de clé résoluble pour
> l'org propriétaire sont **publiés quand même** mais **échouent proprement à l'appel** (McpError,
> pas de fallback) — la liste remonte en warning `mcp_unresolvable_tools` (choix produit : permettre,
> pas refuser). **Trois modes** :
> - **`anonymous`** (sans login + **listé** dans l'annuaire, contourne 100 % du blocage Logto #44) :
>   allowlist **figée = `mcp_tools`** (fail-closed, aucun autre tool visible), credential résolu via
>   l'**org propriétaire** du projet (`access.current_org(None)`→org du projet, `_resolve_credential_anon` :
>   org_secret > grant > clé plateforme, **sans quota**), rate-limité (token-bucket in-memory par IP+projet).
> - **`secret`** : **identique à `anonymous` côté serving** (même 2ᵉ instance FastMCP sans auth, même
>   résolution de credential, même allowlist), mais **non listé** dans l'annuaire (`list_published_mcp_projects`
>   ne rend que `= 'anonymous'`) et **slug non devinable généré serveur** (`_gen_secret_slug` : préfixe
>   optionnel issu du slug saisi + suffixe `secrets.token_hex(6)`) → une **URL secrète**. Re-publier réutilise
>   le slug existant (ne casse pas l'URL déjà distribuée). Le dispatch traite `anonymous`/`secret` sur le même
>   chemin (`access_mode in ("anonymous","secret")`).
> - **`org`** : JWT Logto, **épingle l'org** ; le sous-domaine est enregistré comme **resource Logto**
>   (`oauth_facade.ensure_api_resource`) + verifier **multi-audience** + PRM **host-aware**.
>
> **Host-routing** (`subdomain_project.HostDispatch`, monté `root_app` dans `server.main`) : une **2ᵉ app
> FastMCP sans auth** (`anon_mcp = mcp`, **réutilise l'instance no-auth module-level** — ne PAS en
> re-build une 3ᵉ, doublait register_all/mounts/init_db → boot timeout) sert les sous-domaines anonymes ;
> tout le reste → app authentifiée **inchangée**. **Même URL, 2 publics** : navigateur (`GET`+`Accept:
> text/html`) → **landing HTML** rendue **live depuis la ligne projet** (`anon_landing.render`, name/brief_md/
> mcp_tools) ; Claude/Mistral (`POST`) → MCP (rewrite path `/`→`/mcp`, `_root_to_mcp` — Claude tape la racine).
> Fichiers : `subdomain_project.py` (routing + rate-limit + `/api/mcp/tls-check` + `/api/public/mcp-projects`),
> `anon_visibility.py` (allowlist fail-closed), `anon_oauth.py` (shim OAuth **auto-approve**, `.well-known/*`
> + `/register` + `/authorize`→302 sans login + `/token`→`anon-…`), `anon_landing.py` (HTML charté).
> **Infra** : **wildcard** `*.mcp.oto.cx` (CF-proxied) + Caddy **on-demand TLS** gaté par `/api/mcp/tls-check`
> (200 uniquement pour un slug **publié** → borne l'émission de certs). `publish_mcp` est la **seule** action
> par projet — **zéro DNS** à chaque publication. **Surface web** : annuaire public **oto.ninja/apps**
> (`web/AppsView.vue`) via `GET /api/public/mcp-projects` (CORS `*`, liste les projets `anonymous` publiés).
