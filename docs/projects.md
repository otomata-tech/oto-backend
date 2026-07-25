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
> d'une page Documents), `target='project_file'` (fichier brut « Autre document » — comble
> le gap upload multipart dashboard-only : un agent peut déposer un PDF/CSV, plafond
> `OTO_MCP_UPLOAD_MAX_BYTES`, déf. 25 Mo) et `target='datastore'` (lot de lignes NDJSON/CSV
> → batch-upsert sur clé `schema.key`/param `key`, ns_id résolu+scellé au mint sous l'org
> active, autz réappliquée org-agnostiquement via `ownership.can_access(datastore_namespace,
> write)`). **Double voie pour le MÊME lien** : un agent avec shell PUT le corps brut ; sans
> shell (claude.ai), il transmet l'URL à l'humain qui l'ouvre → **page d'upload HTML
> autoportée** (`GET /api/upload/<token>`, POST multipart `file`, jeton consommé au POST pas
> au GET). L'upload multipart humain dashboard (`POST /api/me/projects/{id}/files`) reste.

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
> org_id`, resource_id = id surrogate) → lecture cross-org **par id** `oto_procedure(op='get', 
> doctrine_id=…)` / `GET /api/me/doctrines/{id}`, gatée `ownership.can_access`. Un projet
> livré remonte chez le client dans `oto_project(op=list)` (flag `shared`+`permission`) ET
> dans le bloc C du handshake (#50) — ouvrable en un message. Reste à cadrer : push des màj
> post-livraison (re-share = re-grant idempotent, mais pas de notification). UI : `oto-dashboard`
`/projects` + page dédiée `/projects/:id` (`ProjectDetailView`, ADR 0030). Reliquats du modèle
(MCP-App rendu, édition temps réel/lock, pré-set vendable=copie) **non faits**.

> **Partage NAVIGABLE d'un projet — `<slug>.share.oto.cx` (ADR 0032).** Un projet publié en
> `secret` est servi sur son sous-domaine `<slug>.share.oto.cx` comme un petit site rendu
> **server-side** (lisible humain ET agent WebFetch), en **lecture seule** : la racine = index
> (brief + procédures/tableaux/docs + carte « brancher »), `/procedures/<id>` (prose), `/data/<ns>`
> (table, gaté par `mcp_expose_datastore`), `/docs/<id>`. Le MCP reste au path `/mcp`. Rendu par
> `share_ui.py` (routeur `build_page`, lectures DB en threadpool, contenu échappé, markdown sûr),
> **gating fail-closed** par appartenance au projet (seules les entités liées sont navigables).
> Même dispatch que `.mcp.oto.cx` (`subdomain_project.HostDispatch`, suffixes `.mcp`/`.share`,
> URL de branchement path-aware). `oto_project(op=get)` expose `mcp_url` (per-mode : `secret` →
> `share.oto.cx/mcp`, sinon `mcp.oto.cx/mcp`) + `share_url` (base navigable, `secret` uniquement).
> Infra : wildcard `*.share.oto.cx` (grey) + Caddy on-demand TLS gaté par `/api/mcp/tls-check`.
> **Remplace** l'ancien partage public **chiffré** zero-knowledge (`/p/p`, `project_public_shares`,
> `lib/crypto.ts`, `PublicProjectView.vue`) — **retiré** (le navigable live le supplante).
>
> **Canal de démonstration / acquisition (otomata-private).** L'index `share_ui` est soigné
> pour « claquer » : un **hero** « brancher dans Claude/Mistral » (URL MCP + copie), les tools
> exposés **groupés par CONNECTEUR** (pastille logo + description au survol + lien vers la fiche
> marketplace `dashboard.oto.ninja/connectors?tab=marketplace&connector=<name>`, dérivé de
> `providers.connector_for_namespace`), et une vue **tableau** pleine largeur avec recherche
> globale + tri 3 états + filtres par colonne (JS inline, opère sur le DOM rendu — pagination
> serveur inchangée). Le hero porte aussi un CTA **« Ajouter à mon Oto »** → deep-link
> `dashboard.oto.ninja/import?slug=<slug>`.
>
> **« Ajouter à mon Oto » — import d'un projet publié par slug.** Capacité `me.import_project`
> (REST-only `POST /api/me/projects/import`, `ORG_MEMBER`) : **forke** un projet PUBLIÉ
> (`mcp_access ∈ {anonymous, secret}`, résolu par `get_project_by_mcp_slug` — le slug non
> devinable = consentement) dans l'**org active** de l'appelant via `duplicate_project`
> (structure only : brief + docs + liens + fichiers ; une **procédure** d'une autre org est
> **copiée** dans l'org cible et le lien repointé — sinon il pendrait sur l'org source ; un
> tableau d'une autre org est re-provisionné à vide ; **jamais** de credentials).
> **Idempotent** : colonne
> `projects.copied_from` + `find_copied_project` → si l'org a déjà forké la source, on la
> RÉCUPÈRE (pas de doublon) ; si la source appartient déjà à l'org active, on l'ouvre. Le
> dashboard (`/import?slug=`, `ImportProjectView.vue`) gère le login puis redirige vers le
> nouveau projet.

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

> **Agent SERVER-SIDE d'un projet (`agent_runtime`) — la 3ᵉ face.** Un projet publié
> avait deux faces : l'**UI navigable** (`share_ui`, lecture seule) et l'**endpoint MCP**
> (`subdomain_project`) que le visiteur devait brancher dans SON client. Manquait la
> troisième : **oto fait tourner la boucle de tool calling** pour qui n'a pas de client
> MCP — au premier chef sur un **projet partagé public**, où la boucle EST le produit.
> Modules : `agent_llm.py` (seam LLM, SDK Anthropic, import **guardé** — sans lib ni
> `ANTHROPIC_API_KEY` la surface agent est simplement absente et le serveur boote
> identique) + `agent_runtime.py` (boucle bornée, exécution d'outil, prompt système).
>
> **Aucune règle d'accès neuve** — trois invariants hérités de l'endpoint MCP :
> 1. **Allowlist FIGÉE = `mcp_tools`** (le preset du projet), fail-closed : un outil
>    demandé hors preset n'est jamais exécuté, il revient au modèle en `tool_result`
>    d'erreur nommant les outils permis (le modèle se corrige au lieu de casser le tour).
>    Sur la face authentifiée, le param `tools` ne peut que **rétrécir** (intersection).
> 2. **Gates d'appel intactes** : exécution par `Tool.run` — MÊME chemin qu'`oto_call`
>    (ADR 0036), hors middleware → credential/RBAC/activation inchangés, et **rédaction
>    de champs ré-appliquée** dans `execute_tool` (sinon un connecteur à PII fuirait).
> 3. **Résolution de credential inchangée** : sur le sous-domaine, `HostDispatch` a déjà
>    posé l'`AnonContext` → les outils tapent l'org **propriétaire** (`_resolve_credential_anon`),
>    exactement comme un appel MCP anonyme.
>
> **Deux surfaces, un moteur** : (a) `POST /agent` du sous-domaine (sans login, gaté
> `agent_enabled`, bucket de rate-limit **séparé et plus serré** que le MCP —
> `OTO_AGENT_RATE_PER_MIN`, déf. 10/min/IP/projet) + la carte « Demander à l'agent »
> rendue par `share_ui` ; (b) capacité **`me.agent`** — MCP `oto_agent`, REST
> `POST /api/me/agent` — ops `run` / `configure` (`can_govern`) / `status`, la boucle
> tournant alors sous l'identité de l'APPELANT.
>
> **Zéro état serveur** : le fil (`messages`, format Anthropic) revient au client à
> chaque tour et lui est rejoué au suivant — pas de session à expirer, un rechargement
> de page repart proprement. **Bornes de coût** : `agent_max_steps` (tours d'outils,
> borné [1,12] à l'écriture ET à la lecture), sortie d'outil tronquée
> (`MAX_TOOL_OUTPUT_CHARS`), historique borné, rate-limit, `output_config.effort`
> (`OTO_AGENT_EFFORT`, déf. `medium`). Colonnes `projects.agent_{enabled,prompt_md,max_steps}`
> — **opt-in STRICT** (défaut FALSE) : un endpoint public qui répond dépense le LLM et
> les clés de l'org propriétaire.
>
> **Frontière d'injection de prompt** : le brief + `agent_prompt_md` (écrits par le
> PROPRIÉTAIRE) vont au niveau système ; le message du VISITEUR reste strictement dans
> le tour user, et le cadre système dit explicitement qu'une consigne du visiteur n'est
> pas une consigne d'administration. Le `stop_reason: refusal` est lu **avant** le
> contenu (jamais un `content[0]` nu) et rendu comme tour terminal.
