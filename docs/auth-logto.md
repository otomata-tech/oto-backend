---
title: Auth — Logto
type: reference
description: >-
  Contrat d'authentification JWT entre oto-backend et Logto self-hosted :
  algorithme ES384 (gotcha RS256 → tout rejeté), discovery OAuth RFC 9728 via
  WWW-Authenticate sur 401, registre d'émetteurs `issuer → (tenant, verifier)` et
  qualification du sub par tenant (ADR 0052 — le tenant `oto` garde un sub NU, l'AAD
  du coffre en dérive), et inscription des clients MCP chez Logto par CIMD (Client ID
  Metadata Document, Logto ≥ 1.44) : la métadonnée de ressource protégée annonce
  l'émetteur Logto lui-même (auth/decouverte.py) ; les clients sans CIMD (Le Chat)
  s'inscrivent par DCR RFC 7591 sous le domaine de l'émetteur
  (auth/inscription_dynamique.py, sans relais : l'`iss` reste celui de Logto). Inclut
  les variables d'environnement requises (LOGTO_ENDPOINT, LOGTO_ENDPOINT_ALT,
  OTO_MCP_LOGTO_M2M_*) ; à consulter dès qu'un 401 JWT ou un échec d'installation MCP
  est à diagnostiquer.
---

# Auth — Logto

Le backend valide les bearer JWT émis par le Logto de la plateforme (`LOGTO_ENDPOINT`,
aujourd'hui `auth.oto.ninja/oidc`). Sur 401, le header `WWW-Authenticate` pointe vers
`/.well-known/oauth-protected-resource/mcp` (RFC 9728) ce qui amorce le discovery OAuth
côté client MCP.

⚠️ **Une instance exige un Logto avec CIMD activé (version minimale 1.44).** Le client MCP
s'inscrit chez Logto par CIMD (§ « L'inscription des clients MCP : CIMD, chez Logto ») ;
seuls les clients sans CIMD passent par l'inscription dynamique (§ « Les clients sans
CIMD : inscription dynamique sous le domaine de l'émetteur »). Un Logto sans CIMD ne sait inscrire aucun client à la
volée, et l'installation d'un connecteur MCP y échoue.

### Le corps du 401 de `/mcp` (#1071)

Le 401 naît dans `RequireAuthMiddleware` de fastmcp : corps vide sans jeton, JSON
`{error, error_description}` sur jeton invalide. `mcp_accueil.McpAccueilMiddleware` (ASGI,
posé par `main`) y ajoute un court texte — ce qu'est oto, `https://oto.cx/llms.txt`, les
trois gestes pour se connecter : corps vide → `text/plain` ; JSON → le même JSON avec un
champ `help`. **Le statut, `WWW-Authenticate` et la découverte ne changent pas.** Servi
**sur l'hôte principal d'oto seulement** (`mcp.oto.cx`, `mcp.oto.ninja`, liste fermée) :
une instance servie ailleurs, l'hôte d'un tenant, l'endpoint d'une org ou d'un projet
gardent le 401 d'avant — le texte est de notre marque.

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

### Les accès d'administration de l'annuaire d'un TENANT (`tenants.logto_mgmt`)

Un tenant dont NOUS hébergeons l'annuaire peut déclarer où frapper, dans `tenants.logto_mgmt`
(JSONB) :

```json
{"token_endpoint": "https://logto-<tenant>.<domaine>",
 "api_endpoint":   "https://auth.<tenant>.<domaine>",
 "credential":     "LOGTO_<TENANT>_MGMT"}
```

⚠️ **Aucun secret en base** : `credential` est le NOM d'un couple de variables
d'environnement (`<credential>_ID` / `<credential>_SECRET`) que le process lit à
l'appel — même convention que le primaire (`OTO_MCP_LOGTO_M2M`), qui n'est donc pas un
cas particulier mais le premier annuaire (`auth/annuaire.py`).

⚠️ **Les deux endpoints sont un COUPLE, jamais dérivés l'un de l'autre** : chez Logto le
jeton de management s'obtient sur l'endpoint d'ADMINISTRATION et les appels `/api` vont
sur l'endpoint PRINCIPAL. L'inverse rend `401 aud check_failed`, très loin de sa cause.

⚠️ **Le cache de jeton est clefé par annuaire** (`annuaire._mgmt_toks`, un dict). Partagé,
il servirait à l'un le jeton de l'autre.

L'écran de suivi (`oto_admin_tenant op=list`) rend `logto_mgmt` (déclaré) et
`directory_admin` (déclaré **et** clé présente ici). Depuis le retrait de la façade
d'enregistrement (20/10/2026), plus aucun geste de la plateforme ne s'en sert : la
déclaration reste lisible, son usage est à redécider (et `tenants.oauth_client_id`, le
client que la façade servait sur les hosts d'un tenant, n'a plus de lecteur — sa
suppression passe par une révision, `docs/migrations-versionnees.md` §5.3).

**Onboarding actuel = self-serve ouvert.** Le tenant a sign-up activé par
email magic link, sans allowlist. Quiconque trouve l'URL peut s'inscrire, et
**un compte neuf, sans aucun grant, résout déjà la clé plateforme des connecteurs du
palier gratuit**, sous quota journalier — c'est voulu (tranché le 02/09/2026) : le
palier gratuit est la découverte de la plateforme. Ce paragraphe affirmait jusqu'au
23/09/2026 que les platform keys « ne sont accessibles qu'avec un grant explicite » :
c'était faux (oto-backend#804). Ce que le palier ouvre, connecteur par connecteur et
avec son quota, est tenu dans `SECURITY.md` §4 (« Free tier »), gardé contre le
registre par `tests/test_securite_palier_gratuit.py`. Installer le connecteur n'est
pas une garde : c'est un filtre d'affichage, que `oto_call` traverse.

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

### Durée de vie : 90 jours au plus (10/11/2026)

Un jeton `oto_` NEUF vit au plus **90 jours** (`db.tokens.TTL_MAX_JOURS`) : une demande
au-delà est refusée `400 token_ttl_too_long` (jamais rabotée), une demande sans échéance
reçoit 90 jours, que la réponse rend (`ttl_days`). La règle vit au store
(`db.echeance_bornee`, appelée par `create_api_token`) et les capacités la lisent au
même endroit — membre, admin, clé d'org, et `scripts/issue_token.py`. Les jetons de
délégation (`ttl_seconds`, la durée d'un bail) sont sous le plafond par construction.
⚠️ **Les jetons émis avant le plafond sans échéance expirent 90 jours après la mise en
prod** (décision du 10/11/2026) : la révision Alembic `20261111_0920_jetons_echeance`
pose `expires_at = NOW() + 90 jours` sur chaque jeton NON révoqué dont `expires_at` est
NULL, toutes natures (`user`, `org`, `delegation`). Elle est jouée par
`oto-mcp migrer upgrade head` après le tag (le démarrage ne la joue pas), et ne se
défait pas : rien ne distingue ensuite un jeton qu'elle a borné d'un jeton émis avec 90
jours, d'où un retour arrière qui lève `RetourImpossible` plutôt que de rendre
éternels des jetons qui ne l'étaient pas. ⚠️ Une **clé d'org** sans échéance (intégration tierce)
et un jeton du script de bootstrap expirent désormais à 90 jours : leur porteur doit
les renouveler.

## Mot de passe changé — révocation par webhook Logto (10/11/2026)

Logto ne ferme rien quand un mot de passe change : ni sessions, ni autorisations
(grants, qui portent les jetons de rafraîchissement des clients OAuth), et un jeton
`oto_` ne dépend pas de Logto. Qui avait tenu une session pouvait s'émettre un jeton
`oto_` qui survivait au changement. `POST /api/logto/webhook`
(`api/logto_webhook.py` → `auth/revocation_mot_de_passe.py`) reçoit les webhooks
Logto et, pour un mot de passe changé :

1. révoque tous les jetons `oto_` actifs du compte (natures `user` et `delegation`,
   par le geste unique `db.revoke_api_token`, trace `revoked_by = logto-webhook`) ;
2. ferme toutes ses sessions Logto, autorisations comprises
   (`DELETE /api/users/{id}/sessions/{uid}?revokeGrantsTarget=all`), puis révoque les
   autorisations qui survivent à leur session (`GET /api/users/{id}/grants`,
   `DELETE …/grants/{grantId}`) — `annuaire.fermer_sessions`, sous le credential de
   management existant `OTO_MCP_LOGTO_M2M_ID`/`_SECRET` (ressource Management API,
   scope `all`, le seul que Logto OSS déclare).

**Ce qui déclenche** : `PostResetPassword` (« mot de passe oublié ») et
`User.Data.Updated` émis par `PATCH /users/:userId/password` (le corps porte `method`
et `matchedRoute`). Tout autre événement signé rend `200 {ignored: true}`.
⚠️ **Le changement par l'Account API (`POST /api/my-account/password`) n'est PAS
reconnu** : Logto 1.44 l'annonce par un `User.Data.Updated` qui ne porte que
l'entité utilisateur, sans route ni champ « mot de passe » — trait pour trait la
livraison d'un changement d'email, de profil ou de MFA. Le tableau de bord d'oto ne
change pas de mot de passe par cette voie aujourd'hui.

**La preuve** : HMAC-SHA256 du corps BRUT avec la clé de signature du webhook, digest
hexadécimal dans `logto-signature-sha-256`, comparé à temps constant. Clé déclarée par
`OTO_LOGTO_WEBHOOK_SIGNING_KEY` (secret, réglage d'instance) ; absente, toute livraison
est refusée `503 logto_webhook_not_configured`. La signature n'a pas d'horodatage :
le `createdAt` signé doit tomber à moins d'une heure (`400 stale_event`).
Réponses : `401 invalid_signature`, `400 invalid_event`, `502 logto_sessions_not_closed`
(jetons coupés, Logto pas entièrement fermé ; Logto retente, le geste est idempotent),
`500 logto_webhook_failed`. Classe `webhook` de la limite de débit.

**Brancher une instance** : console Logto → Webhooks → endpoint
`https://<instance>/api/logto/webhook`, événements `PostResetPassword` et
`User.Data.Updated` ; recopier la clé de signature dans
`OTO_LOGTO_WEBHOOK_SIGNING_KEY`. Le credential M2M doit porter le rôle de Management API
(scope `all`). Seul l'annuaire PRIMAIRE est couvert : un tenant à annuaire propre
n'envoie pas ses webhooks ici.

## Identité de service — un client machine, pas un compte (#1068, 24/09/2026)

Un service consommateur de l'API (le commerce, ADR 0070 §7.4(b)) parle au cœur sous
son propre nom : un client machine de l'annuaire (`client_credentials`), jamais une
ligne `users`. Le cœur vérifie un jeton et garde un identifiant ; il ne possède pas
l'annuaire. Code : `oto_mcp/auth/service_identity.py`.

- **Déclaré dans l'annuaire de l'instance, rien dans l'env** : une ressource d'API
  d'identifiant `<OTO_MCP_PUBLIC_URL>/api/service`, sa permission `commerce`, un rôle
  machine qui la porte, et l'application machine qui a ce rôle. Sans ressource, aucun
  service n'existe.
- **Reconnu à l'audience, vérifié pour de vrai** : `_authenticate` lit l'`aud` non
  vérifié pour choisir, puis le verifier des services revalide signature, émetteur
  (le primaire, plus `LOGTO_ENDPOINT_ALT` pendant la fenêtre de drain d'une bascule de
  domaine, comme pour les personnes — sinon le service tombe entre la bascule de l'annuaire
  et celle du cœur ; jamais l'émetteur d'un tenant tiers), audience stricte et expiration. Le jeton doit être celui d'une
  machine (`sub == client_id`) et porter un rôle du catalogue (`ROLES`) dans `scope`.
  Le verifier des personnes (audience MCP) refuse ce jeton : un service n'entre
  jamais par la face agent.
- **Refusé par défaut, deux verrous** : `_authenticate(allow_service=False)` le refuse
  (`service_forbidden`) sur toute route qui ne l'ouvre pas ; l'adaptateur REST ne
  l'ouvre que pour une capacité dont la règle le lit (`_authz.SERVICE_ROLE`,
  `COMMERCE_SERVICE`). Et `_require_sub` le refuse dans toute autre règle.
- **Journalisé à son nom** : principal `service:<client_id>`, `token_kind="service"`.
  Aucun `upsert_user`, aucune pause, aucun view-as.

Refus nommés : `service_forbidden`, `service_machine_required`, `service_role_missing`,
`service_required`, `invalid_token`. Banc : `tests/test_service_identity_1068.py`.
Les capacités ouvertes au rôle `commerce` : `docs/droits-declares.md` §L'API du commerce.

## Coexistence multi-domaine (pré-cutover, 2026-07-02)

Avant le cutover ADR 0040 (cf. ci-dessous), `mcp.oto.cx/mcp` servait le MCP en
plus de `mcp.oto.ninja` — via **`MCP_AUDIENCE_ALT`** (audiences canoniques
secondaires, vide = no-op), resource Logto dédiée, PRM Host-aware
(`config.mcp_audience_alt_hosts`). DNS `mcp.oto.cx` = grey+ACME direct box.

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`,
`OTO_MCP_ADMIN_SUB` (le sub Logto du compte admin **canonique**, pas celui du
dual-sub gmail), `OTO_MCP_LOGTO_M2M_*` (M2M dédié à l'administration de l'annuaire). S3 Scaleway (`OTO_MCP_S3_*`, bucket `oto-media`)
pour les avatars/logos. Tous ces secrets sont dans SOPS `projects/oto-mcp.yaml`.

## L'inscription des clients MCP : CIMD, chez Logto (20/10/2026)

**Ce qui est servi.** Un seul document de découverte : la métadonnée de ressource protégée
(RFC 9728, `auth/decouverte.py`, sur `/.well-known/oauth-protected-resource` et
`…/mcp`), sensible au host. Elle annonce dans `authorization_servers` **l'émetteur Logto
lui-même** — `LOGTO_ENDPOINT` + `/oidc` pour notre annuaire, `tenants.issuer` sur le host
d'un tenant — avec `scopes_supported = openid profile email offline_access` et la
`resource` (l'audience) qui suit le host. Le client lit ensuite la métadonnée
d'autorisation CHEZ Logto, s'y inscrit par CIMD (son `client_id` est l'URL du document de
métadonnées qu'il publie), s'y autorise et y rafraîchit ses jetons. Le backend ne fait
que vérifier : émetteur, signature (JWKS), audience, `iat` — jamais le `client_id`, qui
n'est que journalisé (`auth.hooks.current_client_id_from_token`).

**Ce qui est retiré** : la métadonnée d'autorisation que nous servions
(`/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`), la DCR
(`/oauth/register`, qui ajoutait le rappel du client à UNE application Logto partagée et
rendait son `client_id` fixe), le consentement forcé (`/oauth/authorize`, oto#202), le
relais RFC 9207 (`/oauth/relay/authorize`, `/oauth/callback`, `/oauth/token`), leurs
variables (`OTO_MCP_CLAUDE_APP_ID`, `LOGTO_PUBLIC_ENDPOINT`, `OTO_MCP_OAUTH_RELAY_HOSTS`),
les déclarations par tenant qu'ils lisaient (`logto_mgmt.redirect_uris`,
`logto_mgmt.refresh_tokens`) et la commande `oto-mcp maintenance oauth-relay-callbacks`.
Pas de repli conditionné par un réglage : une instance qui sert ce code exige un Logto
avec CIMD.

**Pourquoi on annonce l'émetteur, et seulement lui.** Logto sert ses endpoints sur chaque
domaine qui l'atteint, mais son `issuer` ne suit pas l'en-tête `Host` : il est gravé
(`auth.oto.ninja/oidc` pour notre annuaire, `auth.oto.cx` sert les mêmes endpoints sous le
même `issuer`). Annoncer un autre domaine que l'émetteur fait lire au client une
métadonnée dont l'`issuer` diffère de celui qu'il a demandé (RFC 8414 §3.3), puis un
retour d'autorisation dont l'`iss` diffère (RFC 9207) : un client strict refuse les deux.
C'est exactement ce que la façade avait dû contourner par un relais. Conséquence assumée :
l'écran de connexion est celui du domaine de l'émetteur — ce que `LOGTO_PUBLIC_ENDPOINT`
évitait jusqu'ici (vécu le 10/09).

**Pièges qui restent :**

- ⚠️ **« openid » sans `resource` donne un jeton OPAQUE**, refusé en 401 : c'est la
  `resource` du PRM (RFC 8707) qui fait émettre un JWT pour l'audience. Un sous-domaine
  de projet publié doit être une ressource d'API Logto (`annuaire.ensure_api_resource`).
- ⚠️ **Le consentement qui délivre un jeton de rafraîchissement n'est plus ajouté par
  nous** : Logto retire `offline_access` d'une demande sans `prompt=consent` (oto#202).
  Un client qui ne l'envoie pas reçoit des jetons d'accès seuls et refait une autorisation
  à leur expiration ; le régler relève de Logto, plus du backend.
- **Les connexions établies avant le retrait** : leurs jetons restent valides (rien de ce
  que vérifie le backend ne dépend du client) et leur rafraîchissement va chez Logto, SAUF
  pour un client qui avait lu la métadonnée d'un host relayé : son `token_endpoint` était
  `<host>/oauth/token`, qui répond désormais 404 — il doit se reconnecter.
- **L'application partagée et ses rappels restent dans Logto** : rien ne les retire. La
  nettoyer est un geste d'exploitation, une fois les clients passés à CIMD.

## Les clients sans CIMD : inscription dynamique sous le domaine de l'émetteur

Un client qui ne porte pas CIMD (Le Chat) ne pouvait plus s'inscrire depuis le retrait de
la façade : la description de Logto n'annonce aucun `registration_endpoint`. Il est rétabli
(`auth/inscription_dynamique.py`) **sans changer d'émetteur** : le PRM annonce toujours
Logto, l'`iss` reste le sien (RFC 9207), et seuls trois chemins du domaine de l'émetteur
(l'origine de `LOGTO_ENDPOINT`) sont menés au backend par son proxy :

| chemin | rendu |
| --- | --- |
| `GET /.well-known/oauth-authorization-server/oidc` | la description de Logto (`/oidc/.well-known/openid-configuration`) TELLE QUELLE + `registration_endpoint` |
| `GET /.well-known/openid-configuration/oidc` | idem |
| `POST /oauth/register` | l'inscription RFC 7591 |

- **Liste fermée des adresses de retour** (`RETOURS_ADMIS`) : égalité exacte (Le Chat,
  claude.ai, claude.com, grok.com) ou préfixe (`https://chatgpt.com/connector/oauth/`). Une seule
  adresse hors liste : `400 invalid_redirect_uri`, refus journalisé.
- **Une application Logto par type et adresses de retour** : `Traditional` (secret rendu)
  pour `client_secret_basic` (défaut) ou `client_secret_post`, `Native` pour `none`, jamais
  SPA. Nom `DCR · <client_name>` ; une demande de même type et mêmes adresses retrouve
  l'application existante, quel que soit le nom.
- **Plafond** : 30 applications `DCR · ` à adresse admise par préfixe ; au-delà, une
  nouvelle inscription à adresse préfixe est refusée (`invalid_client_metadata`). Les
  adresses exactes ne sont jamais plafonnées. Faire le ménage dans la console Logto.
- **Aucun repli** : Logto injoignable (description) ou API de gestion en panne
  (inscription) rendent une erreur 500. Le secret se lit par
  `GET /api/applications/{id}/secrets` (Logto 1.44 ne le rend plus à la création) ; aucun
  secret, erreur. Le secret n'est jamais journalisé.
- **Débit** : la limite globale (`limite_debit`, classe `anonyme`, par adresse) ; pas de
  borne propre à la route.

## MFA par org## MFA par org (« une org impose le 2ᵉ facteur à ses membres »)

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
  le M2M `auth.annuaire._mgmt_token`). `ensure_mirror`/`disable_mirror`/`sync_members` ;
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
  `auth.annuaire.logto_user_primary_email` : administrer un annuaire tiers demanderait
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

## `email_verified` dans `GET /api/me`

`GET /api/me` sert `email_verified`, lu dans le claim OIDC du **jeton d'accès** Logto de la
requête (le script de claims du tenant le pose vrai dès que `primaryEmail` existe) : aucun
appel à l'API de gestion, aucune colonne. `_authenticate` (`api/base.py`) le pose avec le
compte qui le porte, `email_verifie_de(sub)` le rend. **`null` = inconnu, jamais un faux** :
jeton API (`oto_`), jeton sans le claim ou d'une autre forme qu'un booléen (journalisé), et
« voir en tant que » (le jeton est celui de l'opérateur, pas de la cible).

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
