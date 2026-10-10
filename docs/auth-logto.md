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
  ChatGPT et Mistral sans client_id fixe — y compris sur le host d'un TENANT, où le
  rappel est enregistré dans SON annuaire (`tenants.logto_mgmt`) ou refusé en le
  disant — et le relais d'autorisation (auth/relay.py, OTO_MCP_OAUTH_RELAY_HOSTS) qui
  fait porter à la réponse d'autorisation `iss` = l'issuer annoncé (RFC 9207), sans quoi
  un client strict (SDK MCP Python 2.0) refuse le flux. Inclut les variables
  d'environnement requises (LOGTO_ENDPOINT, LOGTO_ENDPOINT_ALT, OTO_MCP_CLAUDE_APP_ID,
  OTO_MCP_LOGTO_M2M_*) et les garde-fous _redirect_ok ; à consulter dès qu'un 401 JWT
  ou un échec d'installation MCP est à diagnostiquer.
---

# Auth — Logto

Le backend valide les bearer JWT émis par le Logto de la plateforme (`LOGTO_ENDPOINT`,
aujourd'hui `auth.oto.ninja/oidc`). Sur 401, le header `WWW-Authenticate` pointe vers
`/.well-known/oauth-protected-resource/mcp` (RFC 9728) ce qui amorce le discovery OAuth
côté client MCP.

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
ajouter son host à `_redirect_ok` — sauf un client HÉBERGÉ qui pose un rappel par instance
(sous-domaine en libre-service) : jamais un motif, voir §« Le rappel d'un client hébergé ».
Fail-open : Management API en panne → `client_id`
renvoyé quand même (Claude, redirect pré-enregistré, jamais cassé).

### Sur le host d'un TENANT : enregistrer chez lui, ou dire qu'on ne l'a pas fait

Un host réclamé par un tenant (`tenants.hosts`) est servi par la MÊME façade, mais
l'annuaire visé est le sien. Deux choses en découlent, et il a manqué les deux jusqu'au
2026-09-08 (oto-backend#909) :

- **le client rendu est celui du tenant** (`tenants.oauth_client_id`) — c'est là que
  l'utilisateur va s'authentifier ;
- **le rappel doit être enregistré dans SON annuaire.** `_register_redirects` n'était
  appelé que sur le host de la plateforme : sur le domaine MCP du partenaire la façade rendait **201
  sans avoir rien posé**, et le client se faisait refuser deux secondes plus tard à
  l'`/authorize` (`oidc.invalid_redirect_uri`), sans indice.

Un tenant déclare donc où frapper, dans `tenants.logto_mgmt` (JSONB) :

```json
{"token_endpoint": "https://logto-<tenant>.oto.zone",
 "api_endpoint":   "https://auth.<tenant>.ai",
 "credential":     "LOGTO_<TENANT>_MGMT"}
```

⚠️ **Aucun secret en base** : `credential` est le NOM d'un couple de variables
d'environnement (`<credential>_ID` / `<credential>_SECRET`) que le process lit à
l'appel — même convention que le primaire (`OTO_MCP_LOGTO_M2M`), qui n'est donc pas un
cas particulier mais le premier annuaire.

⚠️ **Les deux endpoints sont un COUPLE, jamais dérivés l'un de l'autre** : chez Logto le
jeton de management s'obtient sur l'endpoint d'ADMINISTRATION et les appels `/api` vont
sur l'endpoint PRINCIPAL. L'inverse rend `401 aud check_failed`, très loin de sa cause.
Sur notre annuaire les deux coïncident — ce qui est exactement ce qui a permis de vivre
avec une base unique jusqu'au premier annuaire où ils diffèrent.

⚠️ **Le cache de jeton est clefé par annuaire** (`facade._mgmt_toks`, un dict — c'était
un singleton de module). Partagé, il servirait à l'un le jeton de l'autre : au mieux des
refus intermittents, au pire une écriture dirigée vers le mauvais annuaire.

**Ce que la façade refuse maintenant, en nommant sa destination** (503
`temporarily_unavailable`, journal + Sentry `has:oto.dcr`) : pas de client OAuth déclaré
pour le tenant ; pas d'accès d'annuaire déclaré (→ demander à l'administrateur du tenant
d'ajouter le rappel à la main) ; accès déclaré mais credential absent de l'environnement
du process (→ l'injecter). L'écran de suivi (`oto_admin_tenant op=list`) rend
`logto_mgmt` (déclaré) et `directory_admin` (déclaré **et** clé présente ici) : c'est
cette confrontation qui rattache un refus à sa cause.

⚠️ **Ordre d'activation d'un tenant** : injecter le credential dans l'environnement du
process AVANT que sa ligne ne porte `logto_mgmt`, sinon la façade refuse pendant la
fenêtre. La colonne, elle, est posée par `init_db` au boot — et preprod et prod
partagent la même base.

#### Le rappel d'un client hébergé : l'URL EXACTE, déclarée par tenant, jamais un motif

Un client hébergé dont le tableau de bord pose son PROPRE rappel, un par instance
(Hermes Cloud : `https://<id>.agents.<domaine>/api/mcp/oauth/callback/<serveur>`), n'entre
pas dans la liste globale de `_redirect_ok`. **Un joker n'y entre pas non plus, à aucun
prix** : `POST /oauth/register` n'est pas authentifié et pose le rappel demandé dans
l'application partagée. Avec un motif `*.agents.<domaine>`, n'importe quel client du produit
hébergé (le sous-domaine est en libre-service) enregistrerait le sien, enverrait à un
utilisateur un lien d'autorisation, et recevrait le code — le PKCE n'y change rien, c'est
lui qui ouvre le flux. Les hôtes fixes de la liste (chatgpt.com, callback.mistral.ai) n'ont
pas ce défaut : c'est le serveur du fournisseur qui reçoit le code. Et `_redirect_ok` vaut
pour TOUS les hosts, `mcp.oto.cx` compris, pour le bénéfice d'un seul tenant.

Un tenant DÉCLARE donc les URLs exactes qu'il accepte, dans `tenants.logto_mgmt` (la
même colonne, la même déclaration que ses accès d'annuaire — elle n'a d'effet que si la façade
sait poser le rappel chez lui) :

```json
{"token_endpoint": "…", "api_endpoint": "…", "credential": "LOGTO_<TENANT>_MGMT",
 "redirect_uris": ["https://<id>.agents.<domaine>/api/mcp/oauth/callback/<serveur>"]}
```

- **Égalité de chaîne, rien d'autre** : ni préfixe, ni joker, ni suffixe de domaine, ni
  variante de casse ou de port. Une instance de plus = une entrée de plus, choisie par
  l'exploitant du tenant.
- ⚠️ **Déclarer l'instance d'un opérateur, c'est lui confier les codes d'autorisation de TOUS
  les utilisateurs du tenant**, pas seulement de ses clients. L'application est première
  partie : Logto accorde le consentement tout seul (`koaAutoConsent`, lu dans Logto 1.38), et
  un utilisateur dont la session Logto est ouverte qui suit un lien d'autorisation vers ce
  rappel voit son code partir SANS aucun écran — le lien, c'est l'opérateur de l'instance qui
  le fabrique. **L'instance déclarée doit donc appartenir à l'exploitant du tenant**, jamais
  à un client de son produit hébergé ; le déclarer pour un tiers, c'est l'accord écrit de
  l'exploitant, en connaissance de cause.
- **Sur les hosts de CE tenant seulement** : la liste, l'application et l'annuaire viennent
  de la MÊME entrée de registre, résolue une fois d'après le `Host` de la requête. Le `Host`
  n'est pas authentifié à ce niveau, mais forger celui d'un tenant ne donne que
  l'enregistrement, chez lui, d'un rappel qu'il a déjà déclaré — jamais un rappel au choix de
  l'appelant, jamais chez nous. `_redirect_ok` (liste globale) reste inchangée, et
  `auth/anon.py` n'appelle qu'elle.
- **La DCR et l'autorisation relayée appliquent la MÊME garde**
  (`facade.redirect_autorise`) : un rappel refusé à l'enregistrement ne s'autorise pas, même
  posé dans l'application par un autre chemin.
- **Validée au chargement, entrée par entrée** : une entrée qui n'est pas une URL https
  complète (joker, requête, fragment, identité, chemin absent) est écartée et alertée
  (`tenancy._rappel_declarable`) — une ligne écartée ne peut que RÉDUIRE ce qui est accepté.
- **Refus nommé** sur le host d'un tenant : 400 `invalid_redirect_uri` qui dit QUEL rappel est
  écarté et à qui le demander (l'exploitant du tenant), sans nommer nos colonnes.
- **Poser une entrée** (geste d'exploitation, comme tout le reste de `tenants` ; le tenant
  doit déjà porter `logto_mgmt`), puis `oto_admin_tenant op=reload` — le registre est lu au
  boot, et prod et preprod ne partagent que la base :

  ```sql
  UPDATE tenants
     SET logto_mgmt = jsonb_set(logto_mgmt, '{redirect_uris}',
           COALESCE(logto_mgmt->'redirect_uris', '[]'::jsonb) || to_jsonb(%s::text))
   WHERE slug = %s AND logto_mgmt IS NOT NULL;
  ```

  **Retirer une entrée ferme le chemin de la FAÇADE, pas l'accès.** Elle n'est relue que par
  la DCR et par l'autorisation relayée (`facade.redirect_autorise`) : sur un host RELAYÉ
  (`OTO_MCP_OAUTH_RELAY_HOSTS`), le client qui suit la métadonnée est refusé. Sur un host NON
  relayé, le client s'autorise directement chez l'annuaire du tenant, qui accepte TOUT rappel
  posé sur son application — et il en va de même, sur tout host, pour un lien d'autorisation
  construit directement vers cet annuaire. **Tant que l'URL reste dans l'application Logto,
  l'instance reçoit encore des codes.** Le geste complet, dans cet ordre :
  1. retirer l'entrée ici, puis `oto_admin_tenant op=reload` (la façade ne la reposera plus) ;
  2. retirer l'URL de l'application Logto DU TENANT (celle de `oauth_client_id`) : sa console
     → Applications → l'application → « Redirect URIs », ou la Management API
     `PATCH /api/applications/<id>` avec `oidcClientMetadata` RELU en entier, `redirectUris`
     moins l'entrée. ⚠️ Logto remplace la liste entière (cf. « Le rappel de la façade peut
     disparaître » ci-dessous) : relire juste avant, garder `https://<host>/oauth/callback`,
     relire après. L'origine ajoutée à `customClientMetadata.corsAllowedOrigins` par la DCR se
     retire au même endroit.

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
  (primaire seulement), audience stricte et expiration. Le jeton doit être celui d'une
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
dual-sub gmail), `OTO_MCP_CLAUDE_APP_ID` (client partagé) + `OTO_MCP_LOGTO_M2M_*`
(M2M dédié pour la façade DCR). S3 Scaleway (`OTO_MCP_S3_*`, bucket `oto-media`)
pour les avatars/logos. Tous ces secrets sont dans SOPS `projects/oto-mcp.yaml`.

## Ce qui signe n'est pas ce qu'on annonce (`LOGTO_PUBLIC_ENDPOINT`, 10/09/2026)

`LOGTO_ENDPOINT` porte **deux rôles** longtemps confondus : l'`issuer` des jetons (donc
ce que le verifier attend, et l'adresse de la Management API) **et** l'adresse de Logto
que la façade RFC 8414 **publie aux clients** (`authorization_endpoint`, `token_endpoint`,
`jwks_uri`). Or l'`issuer` est gravé dans l'instance Logto — il vient de son `ENDPOINT`, le
changer invalide toute session vivante — et vaut `https://auth.oto.ninja/oidc`. Conséquence
vécue le 10/09 : un utilisateur qui autorise Claude sur `mcp.oto.cx` lisait `auth.oto.ninja`
dans la métadonnée et se connectait donc là, seul écran du produit hors du domaine canonique.

**`LOGTO_PUBLIC_ENDPOINT` sépare les deux** (`auth.facade._logto_public_oidc`) : posée, elle
ne change QUE les endpoints annoncés ; l'`issuer` de la métadonnée reste **nous**
(`OTO_MCP_PUBLIC_URL`, exigence RFC 8414 §3.3) et la vérification continue de lire
`LOGTO_ENDPOINT`. C'est licite parce que **dans Logto tout suit l'en-tête `Host` sauf
l'`issuer`** : les deux domaines servent les mêmes endpoints et les mêmes clés, un jeton
obtenu par l'un est identique à un jeton obtenu par l'autre. Absente = pas de domaine public
distinct, on annonce celui qui signe (preprod, on-premise). Un host réclamé par un **tenant
tiers** garde son propre annuaire : la variable ne le touche pas.

⚠️ Le cookie de session Logto est **par domaine** : à la bascule, une session ouverte sur
`auth.oto.ninja` ne vaut pas sur `auth.oto.cx` — une reconnexion, une fois.

## Le consentement que réclame le jeton de rafraîchissement (`/oauth/authorize`, oto#202, 13/09/2026)

**Le symptôme.** Le connecteur Oto de Codex Apps redemandait l'authentification toutes les heures.

**La cause, mesurée.** Logto (oidc-provider, `check_scope`) retire `offline_access` d'une demande
d'autorisation qui ne porte pas `prompt=consent`, et ne délivre alors **aucun jeton de
rafraîchissement**. Le connecteur Codex Apps envoie `offline_access` sans `prompt` : il ne recevait
qu'un jeton d'accès de 3 600 s et refaisait une autorisation complète à chaque expiration. Les
clients qui envoient `prompt=consent` avec `offline_access` n'étaient pas touchés. Tant que la
métadonnée publiait le point d'autorisation de Logto en direct, Oto ne voyait pas passer la demande
et ne pouvait rien y faire.

**Le correctif.** Pour NOTRE annuaire, `authorization_endpoint` annonce la façade
(`https://<host>/oauth/authorize`, même hôte que l'`issuer` et que `/oauth/register`). La route
(`auth/authorize_consent.py`) répond 302 vers `<LOGTO_PUBLIC_ENDPOINT ou LOGTO_ENDPOINT>/oidc/auth`,
recopie la requête **à l'octet près**, et n'ajoute `consent` à `prompt` que lorsque `scope` contient
`offline_access` sans consentement explicite. L'`issuer`, le jeton, les clés et l'enregistrement ne
changent pas.

- **Laissés tels quels** : `prompt=none` (jamais combiné à `consent`, combinaison que Logto refuse),
  un `scope` ou un `prompt` répété, `request`/`request_uri`. Logto les traite comme avant.
- **Aucun droit ajouté** : sans `offline_access` (Mistral n'envoie aucun scope), la requête arrive
  inchangée. claude.ai envoie déjà `prompt=login consent` : rien ne change pour lui.
- **Aucune redirection ouverte** : la destination est résolue côté serveur ; la requête n'est
  recopiée qu'après le `?` ; un caractère de contrôle est refusé (400 `invalid_request`) ; la
  réponse n'est pas mise en cache.
- **Hôtes d'un tenant : inchangés, sauf opt-in.** Leur métadonnée annonce toujours le point
  d'autorisation de leur annuaire (sauf host DÉCLARÉ au relais, section suivante :
  `/oauth/relay/authorize`, sans `consent` tant que le tenant n'a pas déclaré
  `refresh_tokens`), et cette route répond 404 sur leur hôte, déclaré ou non. Délivrer des jetons
  de rafraîchissement à leurs utilisateurs est la décision du partenaire (même principe que le
  TTL du 10/09, `/data/infra/docs/logto-oto-dedicated.md`) : il la prend par l'opt-in de la
  section « Jetons de rafraîchissement d'un tenant relayé » ci-dessous.
- **Effet visible** (lu dans le code de Logto 1.38, non mesuré) : pour une application première
  partie comme `Claude (oto MCP)`, `prompt=consent` n'affiche **aucun écran** — `koaAutoConsent`
  accorde le consentement côté serveur. Avec une session Logto ouverte, l'autorisation enchaîne
  quatre redirections sans page visible ; sans session, la page de connexion, comme avant. Si
  l'application devenait « tierce partie », l'écran de consentement apparaîtrait à chaque
  autorisation.
- ⚠️ **Prise en compte côté client** : la métadonnée d'AS n'est pas servie avec un `max-age` (le PRM,
  lui, porte `max-age=3600`), mais un client peut garder celle lue à l'installation. Un connecteur
  existant peut continuer d'appeler Logto directement — sans casse, sans correction — tant qu'il
  n'est pas reconnecté ou recréé ; l'aide OpenAI conseille de recréer l'app pour relire la
  métadonnée.
- ⚠️ **Après correctif** : Codex reçoit des jetons de rafraîchissement **tournants** (client public).
  Deux renouvellements concurrents avec le même jeton déclenchent « refresh token already used » et
  la révocation de toute la délégation : à surveiller.
- Banc : `tests/auth/test_authorize_consent.py`.

### Jetons de rafraîchissement d'un tenant relayé : opt-in par tenant

**Le défaut mesuré.** Sur un host de tenant relayé, Codex fait un OAuth complet puis, une heure
plus tard (durée du jeton d'accès), un 401 → la découverte → et s'arrête : Logto ne lui a
délivré aucun jeton de rafraîchissement, faute de `prompt=consent` (oto#202, ci-dessus), et le
relais passait `consent` à NOTRE annuaire seulement. claude.ai envoie `prompt=login consent` et
se rétablit seul ; un client qui n'envoie pas `prompt` ne le peut pas.

**Le drapeau.** Le tenant le déclare dans `tenants.logto_mgmt`, la même colonne que ses accès
d'annuaire et ses rappels exacts (elle n'a d'effet que si la façade administre son annuaire) :

```json
{"token_endpoint": "…", "api_endpoint": "…", "credential": "LOGTO_<TENANT>_MGMT",
 "refresh_tokens": true}
```

- **Éteint par défaut** : absent, `null` ou `false`, l'autorisation relayée d'un tenant est celle
  d'avant, à l'octet près. Seul le booléen JSON `true` allume ; `"true"`, `1`, une liste, une
  chaîne vide sont écartés au chargement, en alertant (`tenancy._normalize_refresh_tokens`),
  drapeau éteint — une faute de frappe ne délivre jamais de jetons de longue durée.
- **Ce qu'il fait, et seulement cela** : sur les hosts de CE tenant, l'autorisation relayée passe
  par `redirection(..., consentement=True)` (`relay.cible_pour_host`, `Cible.consentement`) :
  `consent` s'ajoute à `prompt` quand `scope` demande `offline_access`. Aucun scope n'est ajouté,
  `prompt=none` et un `consent` déjà présent restent tels quels, un client qui ne demande pas
  `offline_access` suit le parcours d'avant. Un autre tenant et NOTRE annuaire sont inchangés.
- **Il ne se lit que dans la déclaration** posée par l'administrateur de la plateforme : aucun
  paramètre de requête, en-tête ou corps de DCR ne peut l'activer.
- **Il ne vaut que sur un host RELAYÉ** (`OTO_MCP_OAUTH_RELAY_HOSTS`, annuaire administrable).
  Sur un host non relayé le client s'autorise chez l'annuaire du tenant : la plateforme ne voit
  pas la demande et ne peut rien y ajouter. `/oauth/authorize` reste un 404 sur un host de
  tenant, drapeau ou pas.
- **Poser** (geste d'exploitation ; le tenant doit déjà porter `logto_mgmt`), puis
  `oto_admin_tenant op=reload` — le registre est lu au boot, et prod et preprod ne partagent que
  la base :

  ```sql
  UPDATE tenants
     SET logto_mgmt = jsonb_set(logto_mgmt, '{refresh_tokens}', 'true'::jsonb)
   WHERE slug = %s AND logto_mgmt IS NOT NULL;
  ```

  Constater : `oto_admin_tenant op=get slug=<tenant>` → `refresh_tokens_effectif: true` sur
  CHAQUE process (prod et preprod ont leur registre). `logto_mgmt.refresh_tokens` y montre le
  DÉCLARÉ : un `"true"` en chaîne s'y lit, mais le drapeau est éteint.

  **Retirer** : `'false'::jsonb`, ou supprimer la clé (`logto_mgmt - 'refresh_tokens'`), puis
  `op=reload`. ⚠️ **Éteindre le drapeau ne révoque RIEN** : il cesse seulement de demander le
  consentement aux PROCHAINES autorisations. Les jetons de rafraîchissement déjà délivrés
  vivent dans le Logto du tenant et tournent jusqu'à leur TTL (`refreshTokenTtlInDays`, la
  rotation ne les raccourcit pas) ; nous n'en détenons aucun, le relais ne fait que les
  transmettre. Pour couper, révoquer chez le tenant, par sa Management API (Logto ≥ 1.38) :
  pour chaque utilisateur, `GET /api/users/<userId>/grants?appType=firstParty`, puis
  `DELETE /api/users/<userId>/grants/<grantId>` pour chaque autorisation de l'application
  `oauth_client_id` — Logto invalide alors les jetons d'accès opaques et de rafraîchissement
  de cette autorisation. ⚠️ L'autorisation est par (utilisateur, application), pas par
  client : tous les clients de cet utilisateur sur l'application (claude.ai compris) doivent
  se reconnecter. Réduire `refreshTokenTtlInDays` ne suffit pas à couper vite.

**Le risque accepté.** Des jetons de rafraîchissement de longue durée, délivrés à des clients
PUBLICS (Codex, ChatGPT : ni secret, ni contrôle du poste). Un jeton volé se rejoue jusqu'à son
expiration ou sa révocation. Deux garde-fous existent, tous deux dans l'annuaire du TENANT : ils
ne se règlent pas dans notre code, qui ne peut pas les imposer.

**Checklist d'activation, à vérifier chez le tenant AVANT de poser le drapeau :**

1. **L'accord explicite du partenaire**, par écrit : c'est sa décision, pas la nôtre.
2. **`refreshTokenTtlInDays`** sur l'application OAuth du tenant (celle de `oauth_client_id`),
   dans SON Logto : le plafond qu'il accepte. **Sans réglage, c'est le défaut de Logto : 14
   jours**, donc une reconnexion complète tous les 14 jours. Notre annuaire est à 90 jours (défaut
   Logto 14, maximum 180, `/data/infra/docs/logto-oto-dedicated.md` § « Déconnexions tous les 14
   jours ») ; la rotation ne prolonge rien : la fenêtre court depuis la première autorisation.
3. **La rotation** : Logto fait tourner les jetons de rafraîchissement d'un client public. Deux
   renouvellements concurrents avec le même jeton déclenchent « refresh token already used » et
   la révocation de toute la délégation : un client qui rafraîchit en parallèle se déconnecte.
4. **L'application est « première partie »** dans son Logto : pour une application tierce,
   l'écran de consentement s'afficherait à chaque autorisation (`koaAutoConsent` accorde le
   consentement côté serveur pour une première partie : lu dans le code de Logto 1.38, non
   mesuré, cf. « Effet visible » ci-dessus).
5. **Le host est relayé** et l'annuaire administrable (`OTO_MCP_OAUTH_RELAY_HOSTS`, `logto_mgmt` +
   credential présent) : sans cela, le drapeau ne change rien.
6. **Après activation** : les clients déjà connectés ont un jeton SANS rafraîchissement ; il leur
   faut UNE nouvelle autorisation. Puis, sur ce host, `token grant=refresh_token … upstream=200`
   dans le journal, environ une heure après la connexion, à la place de la boucle 401 →
   découverte → arrêt.

## Le relais d'autorisation : `iss` = l'issuer annoncé (RFC 9207, `OTO_MCP_OAUTH_RELAY_HOSTS`, 13/09/2026)

**Le symptôme.** Un agent sur le SDK MCP Python 2.0 (client à boucle locale) s'enregistrait,
ouvrait le navigateur, l'utilisateur se connectait — puis le client s'arrêtait sur
`Authorization response iss mismatch: https://auth.<annuaire>/oidc != https://mcp.<host>/`,
sans qu'aucun jeton ne soit jamais demandé. Vécu deux fois en production le 08/09, sur le host
d'un tenant ; le même client échoue pareil sur notre host.

**La cause.** La façade s'annonce serveur d'autorisation (`issuer` = le host, RFC 8414 §3.3 —
c'est ce qui permet d'offrir la DCR), mais la réponse d'autorisation sortait de Logto, qui y
estampille SON émetteur (`iss`, oidc-provider, sur les succès comme sur les erreurs). RFC 9207
§2.4 impose au client de comparer `iss` à l'`issuer` découvert dès que le paramètre est PRÉSENT :
ne pas annoncer `authorization_response_iss_parameter_supported` ne protège de rien. Les clients
servis jusque-là (claude.ai, ChatGPT, Mistral) ne le vérifiaient pas ; le SDK 2.0 le fait.

**Le correctif** (`auth/relay.py`, sceaux dans `auth/relay_seals.py`). Sur un host déclaré, la
métadonnée annonce `authorization_endpoint = <host>/oauth/relay/authorize` et
`token_endpoint = <host>/oauth/token` :

- l'autorisation ne réécrit que `redirect_uri` (→ `<host>/oauth/callback`, UN rappel posé sur
  l'application partagée de l'annuaire) et `state` (qui scelle, sous HMAC
  `OTO_MCP_OAUTH_STATE_SECRET`, le rappel du client, son `state`, le host et l'heure — **10 min** :
  un sceau s'obtient sans connexion, il ne doit rester rejouable que le temps d'une connexion) ;
  PKCE et le reste arrivent chez Logto à l'octet près, `consent` ajouté pour NOTRE annuaire
  (oto#202) et pour un tenant qui l'a DÉCLARÉ (`logto_mgmt.refresh_tokens`, opt-in, ci-dessus) ;
- le retour vérifie le sceau et, quand il est présent, l'`iss` de Logto (un émetteur que cet
  annuaire ne signe pas ⟹ 400, sans redirection), puis renvoie le client sur son rappel —
  paramètres de réponse REMPLACÉS, le reste de sa requête à l'octet près — avec `iss` =
  l'`issuer` servi ;
- l'échange de jeton remet le rappel de la façade pour un code MARQUÉ (`oto1.<étiquette>.<code>`,
  qui lie le code au rappel du client) et transmet le reste tel quel, segment par segment. Logto
  émet et signe comme avant ; rien n'est stocké.

**Le relais n'élargit rien de ce que Logto aurait accepté** (revue de la PR, 14/09) :

- **rappel enregistré à l'octet près.** Le `redirect_uri` du client est comparé aux rappels
  RELUS sur l'application (Management API, cache de 60 s, lecture bornée à 10 s hors boucle) —
  pas au filtre `_redirect_ok`, qui reste une garde en plus. Chaque DCR oublie ce cache : le
  client qui s'enregistre puis autorise dans la foulée trouve son rappel. D'où une condition :
  **le relais exige un annuaire que la plateforme administre** (`tenants.logto_mgmt` + credential
  présent pour un tenant, `OTO_MCP_LOGTO_M2M_*` pour le nôtre) ;
- **l'échange ne sert que le client de l'application** : `client_id` exigé une fois et égal à
  l'application partagée, aucun en-tête `Authorization` accepté ni relayé (la métadonnée
  n'annonce que `token_endpoint_auth_methods_supported: none`), `redirect_uri` exigé pour un code ;
- **sur un host non déclaré**, `/oauth/token` refuse tout, sauf l'échange d'un code déjà relayé
  (marqué), que rien d'autre ne saurait échanger.

**Il ne se contourne pas en silence** : ce qu'il ne relaie pas reçoit une erreur NOMMÉE, jamais le
trajet direct —

| cas | réponse |
|---|---|
| host non déclaré | 400 `invalid_request` (« relancer la découverte ») |
| requête hors forme : sans PKCE S256, `response_type` ≠ `code`, `response_mode` ≠ `query`, objet `request`, paramètre répété | 400 `invalid_request`, cause nommée |
| client ≠ application partagée | 400 `invalid_client` |
| rappel hors `_redirect_ok`, ou rappel de la façade | 400 `invalid_request` |
| rappel non enregistré sur l'application | 400 `invalid_request` (« l'enregistrer par /oauth/register ») |
| annuaire non administré / application illisible | 503 `temporarily_unavailable` |

— et **un host déclaré sans `OTO_MCP_OAUTH_STATE_SECRET` empêche le démarrage** (`server.main`,
avant la base et les boucles de fond), au lieu d'un relais dégradé dit par une ligne de journal.

**Ce qui ne change pas :**

- **un host non déclaré** sert la métadonnée, `/oauth/authorize` et la DCR d'avant (les bancs de
  la façade passent inchangés) ;
- **`/oauth/authorize` ne relaie jamais** : un client qui a lu la métadonnée d'oto#202 garde
  « autorisation là, jeton chez Logto », et Logto refuserait un code relayé ;
- **aucun drapeau RFC 9207 n'est annoncé** : un client qui exige `iss` sur la foi du drapeau
  (ChatGPT/Codex d'après leur doc ; le parcours « tableau de bord » d'un agent, qui ne transmet pas
  `iss`) casserait sur tout trajet qui ne l'estampille pas.

**Ce qui vaut partout dès le déploiement, déclaré ou non :**

- **`_redirect_ok` durci** (DCR et shim anonyme compris) : ASCII imprimable, aucune barre oblique
  inverse, autorité réduite à `host[:port]` (ni `user@`, ni `%`), aucun segment qui se décode en
  `.`/`..`, aucun fragment, un port valide, et **chaque préfixe borné à un segment**
  (`…/auth_callback` ou `…/auth_callback/<segment>`, `/connector/oauth/<segment>`,
  `/v1/integrations_auth/<segment>`). `http://evil\@127.0.0.1/cb` est local pour Python et part
  chez `evil` dans un navigateur ; `…/auth_callback\..\..\x` sort du chemin pour la même raison ;
- **l'écriture Logto n'envoie que la colonne qui change** (`_register_redirects`) ;
- **les trois routes du relais répondent** (et refusent en le disant) ;
- **le journal d'accès** ne garde plus `code` ni `state` sur `/oauth/callback` — ni sur
  aucune route : `journal_secrets.MasqueCheminAcces`, posé sur `uvicorn.access` au démarrage,
  masque la valeur de toute clé de query secrète (cf. `docs/monitoring.md`).

**Mettre un host en service — dans cet ordre :**

1. déployer la version qui porte le relais (les trois routes sont servies partout, rien n'est
   annoncé) ;
2. ajouter le host à `OTO_MCP_OAUTH_RELAY_HOSTS` dans le `.env` de la box — ⚠️ **la liste
   s'ÉTEND, ne se remplace jamais** ; vérifier que `OTO_MCP_OAUTH_STATE_SECRET` y est (sans lui,
   le service ne démarrera pas). **Ne pas redémarrer encore** ;
3. `oto-mcp maintenance oauth-relay-callbacks` (même `.env` chargé, cf. `docs/commands.md`) :
   À BLANC, il constate `présent`/`absent` ; avec `--apply`, il pose
   `https://<host>/oauth/callback` sur l'application de l'annuaire et RELIT l'application pour
   rendre `posé` (ou `NON CONSTATÉ après écriture`). Chaque host est tenté et rendu, `échec :
   <type>` compris ; une base illisible fait échouer la commande. Un host dont nous n'administrons
   pas l'annuaire rend `refusé` : le relais y est impossible ;
4. redémarrer, puis prouver avec un VRAI client strict (un venv jetable, `mcp==2.0.0`) :
   connexion complète jusqu'à un appel `/mcp` authentifié.

⚠️ **Le rappel de la façade peut disparaître** : Logto remplace la liste entière des rappels à
chaque écriture, sans contrôle de concurrence (une DCR d'un autre environnement sur la même
application, une sauvegarde dans la console). Deux parades : `_register_redirects` n'écrit plus
que la colonne qui change ; et chaque DCR sur un host relayé REPOSE le rappel de la façade. Pas
de verrou : il retiendrait un fil du pool partagé pendant toute la file, sur une route non
authentifiée. Symptôme s'il manque : la page d'erreur `invalid_redirect_uri` de Logto, sans
retour chez nous — rejouer l'étape 3.

⚠️ **Retirer une déclaration n'est pas anodin** : un client qui a lu la métadonnée du relais la
garde, et reçoit alors une erreur nommée (autorisation et rafraîchissement) jusqu'à sa prochaine
découverte. **Revenir à une version antérieure au relais** lui rendrait des 404 — un SDK efface
ses jetons sur un rafraîchissement non-200.

**Limites connues :**

- l'`id_token` reste signé par Logto avec SON `iss` : un client qui valide l'OIDC (et pas
  seulement RFC 9207) le refuserait — ce n'est pas une régression, et ça ne se relaie pas ;
- le rafraîchissement passe désormais par la boucle du serveur : un gel de boucle
  (`docs/event-loop-perf.md`) coupe aussi les rafraîchissements ;
- le jeton part vers l'ORIGINE de notre annuaire (`LOGTO_ENDPOINT`), pas vers le domaine public
  (même jeton, sans le pare-feu applicatif) ; 30 s au plus, jamais de nouvel essai (un code ou
  un jeton de rafraîchissement rejoué fait révoquer toute la délégation), 504 au-delà. Freins sur
  `/oauth/token` : corps borné à 64 Ko (`Content-Length` puis lecture en flux, 413 au-delà), un
  seau de 120/min par IP — le pair TCP, ou le dernier segment de `X-Forwarded-For` quand le pair
  est le proxy de la box (boucle locale), jamais `CF-Connecting-IP` — et 32 échanges en vol au
  plus (503).

**Lire le relais en production** : `journalctl … | grep oauth.relay` (les valeurs écrites par le
client sont entre guillemets, `%r` : un saut de ligne n'y forge pas de ligne) —
- `authorize mode=relay host=… redirect_host='…'`, ou
  `authorize refused reason=<not_declared|request_object|param_shape|response_mode|pkce|foreign_client|redirect_not_allowed|redirect_not_registered|directory_unreadable> client='…'` ;
- `callback outcome='code'|'error:<e>' redirect_host='…' age_s=`, ou, refusé :
  `outcome=bad_state:<sig|ttl|host|format|no_secret>`, `outcome=iss_mismatch`, `outcome=empty` ;
- `token grant=<authorization_code|refresh_token> code=<marked|unmarked|-> upstream=<statut> error='<champ>|-' ms=`
  puis `refresh_token=<oui|non|?|->` et `expires_in=<secondes|->` — **le seul endroit où lire si un
  client reçoit un refresh token** (Caddy ne voit pas les corps) : un booléen et un entier, jamais
  la valeur d'un jeton ; `-` = réponse en erreur ou champ illisible, `?` = succès au JSON illisible
  (`tests/auth/test_relais_journal_echange_jeton.py`). ⚠️ Ne couvre que les hosts DÉCLARÉS dans
  `OTO_MCP_OAUTH_RELAY_HOSTS` : ailleurs le `token_endpoint` annoncé est celui de Logto et ce
  backend ne voit pas l'échange ; ou, refusé ou en panne : `code=tag_mismatch`, `code=mark_stripped`,
  `upstream=<timeout|saturated|NomDeLException>`.

- Bancs : `tests/auth/test_authorization_relay.py` (routes, sceaux, déclaration, refus nommés,
  gardes), `tests/auth/test_authorization_relay_client_mcp.py` (parcours complet par
  `OAuthClientProvider` contre un Logto factice strict, tenant et plateforme, avec le témoin
  sans déclaration) et `tests/auth/test_facade_routes_table_frozen.py` (la table des routes de la
  façade, figée comme celle de `/api/*`). Le SDK épinglé (1.x) ne compare pas `iss` : le banc le
  fait à sa place, et passe le même fichier sous `mcp==2.0.0` dans un venv jetable.

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
