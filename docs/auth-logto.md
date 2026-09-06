---
title: Auth — Logto
type: reference
description: >-
  Contrat d'authentification JWT entre oto-backend et Logto self-hosted :
  algorithme ES384 (gotcha RS256 → tout rejeté), discovery OAuth RFC 9728 via
  WWW-Authenticate sur 401, registre d'émetteurs `issuer → (tenant, verifier)` et
  qualification du sub par tenant (ADR 0052 — le tenant `oto` garde un sub NU, l'AAD
  du coffre en dérive), et façade DCR (auth/facade.py) qui émule le Dynamic
  Client Registration absent de Logto pour permettre l'auto-installation par Claude,
  ChatGPT et Mistral sans client_id fixe. Inclut les variables d'environnement
  requises (LOGTO_ENDPOINT, LOGTO_ENDPOINT_ALT, OTO_MCP_CLAUDE_APP_ID,
  OTO_MCP_LOGTO_M2M_*) et les garde-fous _redirect_ok ; à consulter dès qu'un 401 JWT
  ou un échec d'installation MCP est à diagnostiquer.
---

# Auth — Logto

Le backend valide les bearer JWT émis par le Logto de la plateforme (`LOGTO_ENDPOINT`,
aujourd'hui `auth.oto.ninja/oidc`). Sur 401, le header `WWW-Authenticate` pointe vers
`/.well-known/oauth-protected-resource/mcp` (RFC 9728) ce qui amorce le discovery OAuth
côté client MCP.

## Registre d'émetteurs & sub qualifié par tenant (ADR 0052, lot L2)

Le verifier n'est plus mono-émetteur : `server._build_verifier` construit un **registre
`issuer → (tenant, verifier)`** (`tenancy.py`). Sélection par le claim `iss`, **non
vérifié** — il ne sert qu'à CHOISIR ; le verifier retenu revalide l'émetteur pour de vrai
(signature contre SON JWKS + `iss` byte-à-byte), donc un jeton forgé qui revendique
l'émetteur d'un tiers est rejeté par le verifier de ce tiers, et un `iss` inconnu retombe
sur le primaire, qui le rejette.

- **L'émetteur primaire vient de l'ENV**, jamais de la base : l'auth canonique est
  DB-indépendante. `LOGTO_ENDPOINT_ALT` (fenêtre de drain d'une bascule d'instance Logto)
  n'est plus un `fallback` à part — c'est une **entrée du registre sur le même tenant
  `oto`** : deux émetteurs, un tenant. Les tenants TIERS viennent de `tenants`
  (`slug`, `issuer`, `jwks_uri` — `jwks_uri` NULL ⟹ dérivé `<issuer>/jwks`). Registre
  construit **au boot** ⟹ déclarer un tenant demande un restart.
- **Le sub est qualifié à l'entrée, opaque ensuite.** Tenant `oto` : sub **NU**,
  inchangé. Tenant tiers : `"<slug>:<sub>"`. Un seul qualificateur
  (`tenancy.qualify`), appelé aux deux endroits où un jeton devient un sub —
  `_IatGatedVerifier.verify_token` (les faces MCP et REST partagent l'instance, donc
  `api.routes._authenticate` et `auth.hooks` en héritent) et `api.routes._claimed_sub`
  (attribution du journal REST, qui décode sans vérifier). La qualification est le
  DERNIER geste : un jeton recalé par l'audience ou l'iat-gate n'a jamais produit de sub.
  Un jeton d'API `oto_` sort avant : son sub vient de `users`, déjà qualifié.

⚠️ **Pourquoi le tenant `oto` garde un sub nu** : l'AAD du coffre dérive du sub
(`credentials_store._aad`). Qualifier le sub du tenant `oto` rendrait **tous** les
credentials de production indéchiffrables (`InvalidTag`). C'est aussi ce qui rend le
chantier additif : aucune ligne retouchée, rien de rechiffré. Corollaire : une ligne
`tenants` qui réclamerait l'émetteur primaire est **refusée et loggée** — sinon un
`UPDATE` re-tenanterait les comptes existants.

⚠️ **Le sub est une chaîne opaque en aval** — jamais découpé. L'énoncé naïf « aucun
call-site ne parse `:` » est faux : au scope membre `entity_id` vaut `{org}:{sub}` et se
découpe à son PREMIER `:`. L'énoncé gardé (`tests/test_tenant_l2_sub_opaque.py`) est *le
sub n'est jamais découpé ; `entity_id` ne l'est qu'à son premier `:`*, et jamais quand
`entity_type='user'` (où `entity_id` EST le sub). Pour savoir de quel tenant relève un
sub, **classer par préfixe** : `tenancy.current().tenant_of(sub)`.

⚠️ **Pas de fédération d'identités entre tenants** (0052 §6) : `db.users.migrate_sub`
refuse un alias cross-tenant (le merge par email n'a de sens qu'entre deux émetteurs du
MÊME tenant, cas de la bascule `auth.oto.zone`→`auth.oto.ninja`).

Restent au lot **L3** : l'audience stricte par tenant (retrait des `mcp_audience_alts()`
globales) et le PRM/401 Host-aware — d'où la colonne `tenants.hosts`, posée mais **lue
par personne**. Et au lot **L3bis** : la migration des comptes existants vers un tenant
(un sub neuf ⟹ une AAD neuve ⟹ des secrets illisibles ; cf. 0052 §Migrer).

**Gotcha** : Logto self-hosted signe en `ES384` (P-384 ECDSA). Le default de
`JWTVerifier` est RS256 → tous les tokens rejetés. Vérifié sur
`GET /oidc/jwks`.

Logto self-hosted n'expose pas DCR. La **façade DCR** (`auth/facade.py`) le
supplée : métadonnée AS augmentée (`registration_endpoint` à nous) + à chaque
`POST /oauth/register` elle **enregistre dynamiquement le `redirect_uri` du client
dans l'app Logto partagée** (Management API via M2M dédié `OTO_MCP_LOGTO_M2M_*`)
puis renvoie le `client_id` partagé (`OTO_MCP_CLAUDE_APP_ID`). → les clients MCP
qui exigent DCR (Claude, **ChatGPT**, **Mistral**) s'installent **sans coller de
client_id ni intervention manuelle**, même quand le redirect varie par connecteur
(ChatGPT : `chatgpt.com/connector/oauth/<id>`). Garde-fou `_redirect_ok` : n'autorise
QUE des hosts connus (claude.ai/.com, chatgpt.com préfixe `/connector/oauth/`,
callback.mistral.ai, localhost) — pas un registrar ouvert. **Nouveau client qui
échoue** : son redirect est loggé (`DCR refusé — redirect_uris=…` en journalctl) →
ajouter son host à `_redirect_ok`. Fail-open : Management API en panne → `client_id`
renvoyé quand même (Claude, redirect pré-enregistré, jamais cassé).

**Onboarding actuel = self-serve ouvert.** Le tenant a sign-up activé par
email magic link, sans allowlist. Quiconque trouve l'URL peut s'inscrire,
mais c'est sans risque pour les clés serveur car les platform keys ne sont
accessibles qu'avec un grant explicite (cf. `access/`).

## Jetons d'API `oto_` — authentification non-interactive

La face MCP accepte aussi un jeton d'API `oto_` (v1.57.0) en plus des JWT Logto.
`_IatGatedVerifier._verify_api_token` essaie `db.verify_api_token` avant le JWT
(DB **hors de la loop**), et un jeton reconnu rend un `AccessToken` porteur du `sub`
de son émetteur — donc un vrai compte, avec son dashboard et ses connecteurs. Sans ce
chemin, un runtime **non interactif** (Claude Tag dans Slack, une CI) n'avait que
`client_credentials`, donc une app Logto par intégration, donc un compte machine
orphelin (ni email, ni dashboard, org à poser par `PUT /api/me/active-org` faute d'UI).

⚠️ **Un jeton PORTÉ (`scopes`) est refusé ici** : son gate `auth.token_scopes.authorize`
raisonne sur méthode + chemin HTTP, notions absentes d'un appel MCP — l'accepter
élargirait sa portée en silence. Fail-closed figé par `tests/test_mcp_api_token.py`.
Procédure côté utilisateur = guide plateforme `claude-tag` (+ template public
`otomata-tech/oto-claude-tag-template`, Claude Tag n'acceptant qu'un dépôt privé
comme source de plugins).

## Coexistence multi-domaine (pré-cutover, 2026-07-02)

Avant le cutover ADR 0040 (cf. ci-dessous), `mcp.oto.cx/mcp` servait le MCP en
plus de `mcp.oto.ninja` — via **`MCP_AUDIENCE_ALT`** (audiences canoniques
secondaires, vide = no-op), resource Logto dédiée, PRM Host-aware
(`config.mcp_audience_alt_hosts`). DNS `mcp.oto.cx` = grey+ACME direct box.

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`,
`OTO_MCP_ADMIN_SUB` (le sub Logto du compte admin **canonique**, pas celui du
dual-sub gmail), `OTO_MCP_CLAUDE_APP_ID` (client partagé) + `OTO_MCP_LOGTO_M2M_*`
(M2M dédié pour la façade DCR). S3 Scaleway (`OTO_MCP_S3_*`, bucket `oto-media`)
pour les avatars/logos. Tous ces secrets sont dans SOPS `projects/oto-mcp.yaml`.

## MFA par org (« une org impose le 2ᵉ facteur à ses membres »)

But : un `org_admin` peut rendre le MFA **obligatoire** pour tous les membres de
son org. Décision d'archi (vérifiée contre le source Logto `@logto/core@1.38.0` +
l'instance live) :

- **On garde le login ordinaire** (token de resource, org résolue côté serveur, org
  fluide — ADR 0023/0038). PAS de token org-scopé. Le MFA d'org de Logto est évalué
  pendant la sign-in experience sur **l'appartenance** de l'user (agrégation de TOUTES
  ses orgs), pas sur l'org du token — cf. `mfa.ts::isMfaRequiredByUserOrganizations`.
- **Deux réglages combinés** :
  1. **Tenant, une fois** : `mfa.organizationRequiredMfaPolicy = Mandatory` sur la
     Sign-in Experience de `auth.oto.ninja` (`PATCH /api/sign-in-exp`). **Inerte** tant
     qu'aucune org n'a `isMfaRequired`. Défaut rétrocompat = `NoPrompt` (aucun effet).
  2. **Par org** : une **organization Logto MIROIR** avec `isMfaRequired=true` +
     ses membres synchronisés **par `sub`**.
- Résultat : dès qu'un membre appartient à ≥1 org à MFA, Logto le force à enrôler +
  utiliser un 2ᵉ facteur à **chaque login** (le gate général `guardMfaVerificationStatus`
  fait re-vérifier le facteur à chaque sign-in). Le **switch d'org** ne redéclenche
  rien (résolution serveur, pas de nouveau token).

Implémentation :

- Source de vérité = PG oto : `orgs.require_mfa` (drapeau) + `orgs.logto_org_id` (id du
  miroir). L'org Logto n'a **aucune autorité** (juste l'enforcement au login).
- `mfa_mirror.py` = provisioning + sync (client Management API organizations, réutilise
  le M2M `auth.facade._mgmt_token`). `ensure_mirror`/`disable_mirror`/`sync_members` ;
  `on_membership_changed(org_id)` branché sur `org_store.add/remove_org_member`
  (import paresseux, best-effort). ⚠️ Le roster miroir = **tous** les membres, jamais
  filtré sur `org_members.is_active` (ce flag = l'org active par défaut du sub, pas
  l'appartenance).
- **Le MFA d'org est une capacité du tenant `oto`** (arbitrage #274, 11/08) : le miroir
  vit dans NOTRE Logto, donc un membre venu d'un **tenant tiers** (sub qualifié
  `slug:sub`, ADR 0052 L2) n'y est pas inscriptible — et il n'en a pas besoin, son
  émetteur applique sa propre politique. `_split_members_by_tenant` l'écarte du roster
  (le SEUL filtre en plus du tenant : aucun rapport avec `is_active`). Ce n'était pas
  cosmétique : `POST …/users` poste tout le roster d'un coup, donc UN sub qualifié
  faisait échouer la synchro de **toute l'org**, en silence (chemin best-effort). Le
  filtrage est **constatable** — `org.mfa.get` rend `members_other_tenant` (un
  filtrage muet ferait dire « MFA actif » à une org mixte). Les deux helpers de fil
  (`_add_logto_members`, `_remove_logto_member`) refusent en plus un sub qualifié
  (`tenancy.require_primary_tenant` → `ForeignTenantDirectory`), même garde que
  `auth.facade.logto_user_primary_email` : administrer un annuaire tiers demanderait
  ses credentials de management, que la table `tenants` ne porte pas.
- Capacité `org.mfa.{get,set}` (`capabilities/orgs/mfa.py`) → `oto_get/set_org_mfa`
  + REST `/api/orgs/{id}/mfa` (`ORG_MEMBER`/`ORG_ADMIN`). **Pas de fail-open** :
  activation = provisionner AVANT le drapeau (Logto plante → drapeau non posé) ;
  désactivation = baisser `isMfaRequired` AVANT le drapeau (Logto plante → reste
  enforced). Exposé en lecture dans `oto_whoami` + `/api/me` (`active_org_require_mfa`).
  Toggle dashboard : `OrgMfaCard.vue` sur `/org`.

Limite : la **récupération par magic-link email** reste un mono-facteur (backlog, cf.
`infra/docs/logto.md`). Le vrai **step-up par appel** (`acr_values`) n'existe pas dans
Logto → non implémentable côté serveur ; l'enforcement est donc au **login**.

## CUTOVER ADR 0040 (2026-07-06) — `.ninja` ↔ `.cx` inversés

> **⚠️ CUTOVER ADR 0040 (2026-07-06) — `.ninja`↔`.cx` inversés.** Désormais **PROD =
> `mcp.oto.cx`** (:9103, audience canonique `mcp.oto.cx/mcp`, dashboard `manage.oto.cx`) et
> **PREPROD = `mcp.oto.ninja`** (:9105, audience `mcp.oto.ninja/mcp`, dashboard `manage.oto.ninja`).
> DB découplée (backends inchangés, seuls domaines/audiences/dashboards ont basculé ; prod
> reste sur `otomata-main`). ⚠️ **Logto = 2 instances** : la vraie prod/preprod = **`auth.oto.ninja`**
> (creds SOPS `LOGTO_NINJA_MGMT_*`), PAS `auth.oto.zone`. Les mentions `mcp.oto.ninja=prod`
> ailleurs dans ce fichier sont **antérieures au cutover**.
>
> ⚠️ **`MCP_AUDIENCE_ALT` est une LISTE (virgules) : ÉTENDRE, jamais remplacer.** Un
> `sed 's|^MCP_AUDIENCE_ALT=.*|…|'` écrase les audiences déjà déclarées — sans erreur au
> boot, le service démarre : la casse ne se voit qu'au premier `invalid_token` d'un client.
> Vécu 03/08 (un tenant tiers) : la preprod portait `mcp-canari.oto.ninja/mcp`, l'écraser aurait coupé
> le canari. Chaque environnement a SA liste (`/opt/oto-mcp/.env` ≠ `/opt/oto-mcp-canari/.env`) :
> poser une audience sur l'un ne la pose PAS sur l'autre — le symptôme est alors « ça marche en
> prod, pas en preprod ». Même règle pour tout env-liste partagé (`OTO_MCP_CORS_ORIGINS`,
> `MAILER_FROM_DOMAINS`, SPF, redirect URIs OAuth) : lire la valeur, y ajouter, réécrire.

## Ligne de stack (reprise du CLAUDE.md, 2026-08-31)

- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)
