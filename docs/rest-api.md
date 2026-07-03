---
title: REST API (consommée par oto.ninja /account)
type: reference
description: >-
  Inventaire des endpoints REST /api/* de oto-backend : profil /api/me (billing,
  onboarding, connecteurs), settings LinkedIn/API-keys/tools, doctrine org
  /api/me/instructions*, palier org (CRUD orgs, membres, secrets, invitations,
  entitlements namespace), admin users/grants/tokens/monitoring, billing Stripe,
  bibliothèque publique de doctrines (doctrine_library, visibilité public/unlisted).
  Détaille les règles CORS (oto.ninja, app.oto.ninja, dashboard.oto.ninja), l'autz
  (même JWTVerifier ES384 que /mcp, audience mcp.oto.ninja), et les gotchas secrets
  (jamais la clé en réponse, providers per-user refusés en org secrets). À charger
  pour implémenter ou déboguer un endpoint REST ou comprendre le contrat front/back.
---

# REST API (consommée par oto.ninja /account)

- `GET /api/me` — profil + role + statut LinkedIn + statut providers (mode/key/quota) + `active_org`/`active_org_name`/`org_role` + `avatar_url`/`active_org_logo_url`
- `POST|DELETE /api/me/avatar` — upload (multipart `file`, png/jpeg/webp ≤ 2 Mo) / efface l'avatar user → Scaleway Object Storage, URL publique en DB
- `POST|DELETE /api/orgs/{id}/logo` — upload / efface le logo **uploadé** d'org (org_admin, multipart `file`). Le logo AFFICHÉ (`logo_url` des lectures + `active_org_logo_url` de `/api/me`) est l'**effectif** : upload sinon dérivé du CDN logo.dev via le `domain` déclaré (`org_store.effective_logo_url`, token `LOGODEV_TOKEN`) ; `logo_custom` (fiche org) dit si un upload existe.
- `PATCH /api/orgs/{id}` (+ miroir `/api/admin/orgs/{id}`) — profil d'org (org_admin) : `name`, `description`, **`domain`** (domaine de marque, normalisé `org_store.normalize_domain` — `""` efface, saisie URL tolérée, invalide → 400 `invalid_domain`), `industry`, `location`. Capacité `org.update` (MCP `oto_update_org`).
- `POST|DELETE /api/settings/linkedin` — cookie li_at + UA
- `POST|DELETE /api/settings/api-keys/{serper|hunter|sirene}` — user key
- `GET /api/me/tools` + `POST|DELETE /api/me/tools/{name}` — toggle individuel d'un tool MCP
- `GET /api/me/instructions` (agent readme d'org meta + index des procédures) + `GET|PUT|DELETE /api/me/instructions/{slug}` + `GET /api/me/instructions/{slug}/versions` + `POST /api/me/instructions/{slug}/revert` — readme (`claude_md`) & procédures de l'**org active** (cf. §Doctrines). Lecture = membre ; écriture = `org_admin` (ou platform admin). Édité par le dashboard (`/org` pour le readme, `/procedures` pour les procédures).
- `GET|PUT /api/me/agent-readme` — **agent readme personnel** (niveau USER, `user_agent_readme`) : prose markdown injectée à chaque session, cumulée après les readme plateforme/org/équipe. `SUB_ONLY`, REST-only (édité sur `/account` ; corps vide = effacé).
- `POST|DELETE /api/me/projects/{id}/public-share` — **partage public CHIFFRÉ** d'un projet (ADR 0032 §3, zero-knowledge). Le dashboard chiffre le snapshot (brief + pages) côté navigateur et POSTe uniquement `{ciphertext}` ; renvoie `{token, public_base_url}`. Écriture = `ownership.can_access(project, write)`. La clé de déchiffrement n'atteint JAMAIS le serveur (fragment d'URL).
- `GET /api/public/projects/{token}` — **sans auth** : renvoie `{ciphertext, updated_at}` du snapshot chiffré. Déchiffrement côté navigateur (route `/p/p/{token}#<clé>`). Pendant public de `GET /api/public/docs/{token}` (#4a).
- `PUT|POST|GET /api/upload/{token}` — **réception d'un upload signé out-of-bande** (issue #105), **pas de JWT** : le `{token}` est un jeton HMAC scellant `(sub, org, cible)` + TTL court + usage unique (émis par `oto_upload_url`, module `upload_tokens.py`). **PUT** = un agent avec shell y pousse le corps brut (`curl --data-binary @fichier`) ; **POST** multipart `file` = le formulaire humain ; **GET** = page HTML d'upload autoportée (fallback quand l'agent n'a pas de shell, ex. claude.ai : il transmet le lien à l'humain — le jeton n'est PAS consommé au GET). Le backend matérialise dans la cible en **réappliquant** son autz, consomme le jeton (anti-rejeu), renvoie un **accusé léger** (id + compteurs), jamais le body. Cibles : page Documents (`doc`), fichier brut de projet (`project_file`, autz `ownership.can_access(project, write)`), lot de lignes datastore (`datastore` — NDJSON/CSV batch-upsert sur clé, autz `ownership.can_access(datastore_namespace, write)`, ns_id scellé au mint). Évite de faire transiter du gros contenu par le contexte du LLM.
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- `POST /api/admin/users/{sub}/grants/{key_id}` body `{daily_quota}` — set/update quota par grant (admin only)
- `GET|POST /api/admin/users/{sub}/tokens` + `DELETE /api/admin/users/{sub}/tokens/{token_id}` — issue/list/revoke tokens API on behalf of a user (admin only)
- `GET /api/admin/monitoring/summary?days=` + `GET /api/admin/monitoring/calls?limit=&sub=&tool=&errors=&days=` — journal des appels MCP, agrégats + brut (admin only, cf. §Monitoring)
- **Palier org** (`api_routes_orgs.py`, projection 1:1 des meta-tools `oto_admin_*org*` / `oto_list_orgs`) :
  - self-service : `GET|POST /api/me/orgs` (**`POST` = `org.create` self-serve**, créateur→org_admin, cap `OTO_MCP_MAX_ORGS_PER_USER`) ; `GET /api/orgs/{id}` ; `POST|DELETE /api/orgs/{id}/members[/{sub}]` + `PUT|DELETE /api/orgs/{id}/secrets/{provider}` (org_admin)
  - **invitations** (onboarding SaaS) : `POST|GET /api/orgs/{id}/invitations` + `DELETE …/{inv}` (org_admin) ; `POST /api/me/invitations/accept` (`SUB_ONLY`, match email vérifié + expiry). Email via `oto_mcp/email.py` (otomata-mailer `mailer.oto.zone/api/send`, env `OTO_MAILER_SEND_BEARER`, best-effort → `invite_url` en repli ; **plus de Resend**).
  - **fiche admin user** : `GET /api/admin/users/{sub}` = identité + accès effectif par provider (`status_for`) + grants + namespaces + orgs (membership).
  - platform admin : `GET|POST /api/admin/orgs`, `GET /api/admin/orgs/{id}` (+ entitlements), `…/members*`, `…/secrets/{provider}`, `POST|DELETE /api/admin/orgs/{id}/entitlements/{namespace}`, `GET /api/admin/namespace-grants`, `POST|DELETE /api/admin/users/{sub}/namespace-grants/{namespace}`
  - secrets : jamais la clé en réponse (provider/base_url/set_at/set_by) ; providers per-user (slack/linkedin/google/whatsapp) refusés en `400` ; listing lu du coffre canonique `credentials_store` (legacy `org_secrets` plus dual-written sous chiffrement). Gating org_admin/membre via `org_store.get_org_role` (platform admin toujours autorisé). Révocation lazy sur sessions MCP ouvertes. Contrat front : `oto-app/docs/ORG_API_CONTRACT.md`.
- **Bibliothèque publique de doctrines** (marketplace de skills, table `doctrine_library`) :
  capacités `library.*` (`capabilities/doctrine_library.py`, montage auto MCP+REST) —
  `library.list/get` (`SUB_ONLY`, MCP `oto_list_library`/`oto_get_library_doctrine` + REST
  `GET /api/me/doctrines/library[/{slug}]`), `library.publish`/`library.fork` (`ORG_MEMBER` +
  gate org_admin en handler, MCP `oto_publish_doctrine`/`oto_fork_doctrine` + REST
  `POST /api/me/doctrines/{publish,fork}`), `library.unpublish` (auteur/PLATFORM_ADMIN, `DELETE
  /api/me/doctrines/library/{id}`). **Auteur** = `otomata` si publieur platform-operator, sinon
  l'`org`. **Fork** réutilise `org_store.set_instruction` → skill d'org versionné. Surface
  ANONYME pour la vitrine : routes écrites à la main `GET /api/doctrines/library[/{slug}]`
  (deny-by-default `visibility='public'`, l'adaptateur capacité authentifie toujours).
  **`visibility`** : `public` (dans le catalogue) vs `unlisted` = **lien non listé** (style
  YouTube) — servie par `library.get` (slug exact, tout user authentifié) mais **jamais**
  listée (`list` force `include_unlisted=False`) ni servie en anonyme. Partage par lien, pas
  un secret d'org : une doctrine sensible ne se publie pas (reste un skill d'org privé).
- CORS : `oto.ninja`, `app.oto.ninja`, `dashboard.oto.ninja` (+ localhosts dev) — défaut dans `_allowed_origins`, override `OTO_MCP_CORS_ORIGINS`. `account.oto.zone` retiré (surface compte décommissionnée → dashboard.oto.ninja)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`
