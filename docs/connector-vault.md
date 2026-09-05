---
title: Connector vault — registre + coffre chiffré + résolution
type: reference
description: >-
  Architecture centrale des credentials oto-backend : registre source unique providers/
  (dataclass Connector, 3 axes disponibilité/visibilité/credential, schéma multi-champs
  secret_fields dérivé), coffre chiffré unique connector_credentials (table 4-col PK
  entity_type/entity_id/connector/account, AES-256-GCM obligatoire via crypto.py,
  master key en Scaleway Secret Manager), et résolution access/ (resolve_api_key,
  resolve_credential_fields, resolve_mount_token, status_for,
  list_datastore_namespaces_granted_to).
  Inclut le packing multi-champs, Google multi-compte, LinkedIn/Crunchbase en coffre,
  l'instance comme OBJET (table connector_instances posée A COTE du coffre, id stable
  inst:{id}, lot L6 du chantier tenant & instances),
  et le modèle de connecteur remote (ADR 0003, pilote = un connecteur remote client). À consulter pour
  tout ajout de connecteur, débogage de credential, ou compréhension du chiffrement.
adr:
  - "0003"
  - "0011"
---

# Connector vault — registre + coffre chiffré + résolution

Substrat unique des connecteurs, credentials et accès d'oto-mcp. Déployé en prod (2026-06).
Chiffrement **obligatoire** : tous les secrets vivent chiffrés (`secret_enc`), plus aucune colonne plaintext ni dual-write (purge legacy 2026-06-11). Un serveur sans `OTO_MCP_MASTER_KEY` boote mais tout write de credential échoue fort.

## Registre — source unique (package `providers/`)

Package pur (aucun import oto_mcp, comme `tool_visibility.py`). Une dataclass `Connector` par connecteur, 3 axes orthogonaux :

**Où ça vit** (découpage 2026-08-27, ex-`providers.py` monolithique de 1 900 lignes) :
- `providers/<nom>.py` — le **domicile unique** d'un connecteur : `CONNECTOR = _c(…)`, ses commentaires, et ses constantes curées `CATEGORY` / `PUBLISHER` / `DESCRIPTION` / `LOGO_DOMAIN` / `SANS_LOGO_DE_MARQUE`. Le module porte le nom du connecteur (vérifié à l'import).
- `providers/_model.py` — la **forme** : `CredentialField`, `Connector`, la factory `_c`. Un nouveau CHAMP par connecteur s'ajoute ici, puis se renseigne dans le module du connecteur — jamais dans une liste transverse indexée par nom (c'est ce qu'ont acté `account_noun` puis `cardinality`, cf. ci-dessous).

### Ce que la fiche DIT — l'éditeur nomme qui reçoit l'appel

Tranché par Alexis le **2026-09-02** : *« dire la vérité sur les deux »*. Les champs curés décrivent le service **rendu**, jamais la marque dont on emprunte le nom :

- **`PUBLISHER` = qui reçoit réellement l'appel.** Une passerelle tierce se nomme (`reddit` → `redditapis.com`, l'API de Reddit étant fermée en self-serve) ; un service qu'on opère soi-même se nomme aussi (`planity` → `Otomata`, le mount est NOTRE serveur).
- ⚠️ **Et un DÉFAUT ne parle pas à la place d'une déclaration absente — il n'y en a donc PLUS.** Jusqu'au 2026-09-02, sans constante `PUBLISHER`, `publisher_name` retombait sur « Otomata » : une omission était indiscernable du choix « connecteur maison ». C'est ce qui a fait servir le MCP **officiel de Folk** sous notre nom (`folkmcp`) pendant un mois, et les **six canaux de messagerie** hébergés — qui *envoient des messages* par une passerelle tierce — sous notre nom aussi. Le repli est retiré : sans déclaration, la fiche n'affiche **rien** — chaîne vide, jamais `None` ni un libellé de remplacement. Le choix `""` plutôt que `null` se tranche sur le CONTRAT, pas sur le rendu : mesuré le 2026-09-02 en rendant les composants, les cinq surfaces qui affichent l'éditeur traitent les deux à l'identique (elles masquent ou coercent, aucune ne rend « undefined »), mais oto-dashboard déclare `publisher: string` dans `types/api.ts` — `null` le ferait mentir sans rien gagner.

  > **Une absence se voit et se corrige ; une attribution fausse se croit.** *(Alexis, 2026-09-02)*

  Corollaire : **un connecteur légitimement nôtre le DÉCLARE** (`PUBLISHER = "Otomata"`) au lieu d'y retomber — même valeur, mais elle devient un choix relisible. Les trois génériques/maison (`browser`, `web`, `http`) l'ont fait le 2026-09-02 ; les six canaux nomment désormais `Unipile`, la passerelle qui reçoit l'appel et détient la session du compte opéré — déclaré **une seule fois**, chez le porteur de la clé (`providers/unipile.channel`), parce que c'est une propriété du COMPTE et pas du canal. Cliquet : **`tests/test_connector_publisher.py`**, qui exige une déclaration pour **tout** le registre, lit la valeur **SERVIE** (`publisher_name` — donc les deux chemins de déclaration, constante de module *et* champ d'entrée), et refuse aussi la déclaration périmée. ⚠️ Il n'a **aucun filtre de famille**, et il le prouve **par mutation** : un test paramétré sur les familles présentes rend muet un connecteur réel de chacune et exige que le contrôle le voie — glisser `kind == "tools"` dans son calcul fait rougir la famille `mount`, nommément. Un test qui se contenterait de recalculer la population serait aveugle au même défaut.
- **`help` dit qu'un intermédiaire existe**, en une clause et sans jargon — avant l'installation, pas après. Et quand la connexion se fait avec les **identifiants du service** (planity : email + mot de passe rejoués par notre mount), l'aide le dit : c'est ce que la fiche engage de plus lourd.

  ⚠️ **L'éditeur ne dispense PAS de le dire dans l'aide** — les deux champs ne sont pas lus au même endroit. Le **bloc catalogue injecté au handshake** (`render_namespace_catalog`, ~10 400 c. à chaque session) ne sert que `« label : help »` : il ne porte **jamais** l'éditeur. Une fiche dont seul `PUBLISHER` nommerait la passerelle la tairait donc à l'agent, et à la personne au moment de décider. Les **six canaux hébergés** ont reçu la clause le 2026-09-02, la même pour les six — *« Ton compte se connecte chez Unipile, notre prestataire, qui détient la session. »* — au prix mesuré de **+486 c.** sur le bloc injecté (10 402 → 10 888) et d'une recopie par outil dans `oto_list_my_tools`. Cliquet : `tests/test_unipile_split.py`, sur une population **dérivée du registre** (`credential_of == "unipile"`), pas sur une liste écrite à la main — un septième canal y entre tout seul.
- **`LOGO_DOMAIN` ne pose pas une marque tierce sur un service qui n'est pas le sien.** L'absence se DÉCLARE (`SANS_LOGO_DE_MARQUE = True`), elle ne s'invente pas.
- Le **NOM** du connecteur et de ses tools ne bouge pas pour autant : des appelants s'y accrochent (`docs/alias-deprecies.md`).

⚠️ **Les fautes vont dans des sens DIFFÉRENTS, et une seule relecture les confond.** Les trois du 2026-09-02, corrigées ensemble :

| connecteur | ce que la fiche disait | qui rend le service | le sens de la faute |
|---|---|---|---|
| `reddit` | éditeur « Reddit », logo reddit.com | `api.redditapis.com`, un revendeur | on crédite une marque tierce d'un service qu'elle ne rend pas, et l'intermédiaire disparaît |
| `planity` | éditeur « Planity », logo planity.com | `planity-mcp.oto.zone`, NOTRE serveur | on crédite un tiers de ce qu'on opère — ça se lit comme une intégration officielle |
| `folkmcp` | éditeur « Otomata » (le défaut) | `mcp.folk.app`, le MCP officiel de Folk | on se crédite du produit d'un tiers |

Chercher « à qui l'appel arrive-t-il ? » attrape les trois ; chercher « la marque est-elle la bonne ? » n'en attrape aucune.

⚠️ Le ratchet `tests/test_connector_logos.py` a filtré `kind == "tools"` du 2026-08-02 au 2026-09-02, dans ses **deux** directions — donc aucun connecteur fédéré n'était jugé. L'angle mort a coûté aux deux sens : `folkmcp` est resté sans logo ni éditeur déclaré (servi « Otomata »), et la déclaration d'absence légitime de `planity` passait pour une entrée morte. Le filtre est retiré : du point de vue de la fiche, un mount est un connecteur comme un autre.

### La cardinalité d'auth — dérivée, déclarée, et plus jamais listée

Tranché par Alexis le 2026-08-27 : *« ce qui gêne, c'est la LISTE elle-même »*. Trois
crans, dans cet ordre :

1. **Dérivée** du descripteur d'auth — multi dès que le credential se POSE
   (`method=secret`, `secret_kind ∈ {api_key, basic_auth, fields}`, hors
   `personal_cross_org`), mono sinon. C'est le cas normal : **74 connecteurs sur 96**
   n'ont rien à déclarer.
2. **Déclarée** par le connecteur, dans son entrée : `cardinality="mono"|"multi"`, à
   côté de ce qu'elle qualifie et avec son motif. Deux porteurs, et c'est la **mesure**
   qui les a désignés — ceux dont le descripteur dit faux : `google` (OAuth ⟹ dérivé
   mono, mais N consentements = N comptes) et `browser` (cookie ⟹ dérivé mono, mais un
   compte est un **site**). Aucun connecteur ne se déclare `mono` aujourd'hui : ceux
   qui le sont le sont par une condition structurelle.
3. **Surchargée en base** (`connector_settings`), par **org** puis par **plateforme** —
   le patron du bloc d'instructions : constante = défaut, ligne DB = surcharge. Un
   élargissement ne demande plus un déploiement.

**L'ordre est l'arbitrage** : org de contexte > plateforme > registre. Et **une seule
fonction le tranche** : `connectors.cardinality.is_multi_account(connector, org)`.

⚠️ **Le vrai risque du lot n'est pas de mal lire la surcharge, c'est de ne la lire que
d'un côté.** **Trois** chemins très éloignés consultent la cardinalité : la **garde
d'écriture** (« ce deuxième compte a-t-il le droit d'exister ? »), la **résolution**
(« va-t-on le chercher ? ») et la **carte servie** (« l'écran le propose-t-il ? »). Lue
par la première seulement, une surcharge accepterait une ligne que personne n'irait
jamais lire — mot pour mot le défaut d'oto-backend#409, corrigé le 27/08. D'où deux
gardes : une sonde AST qui interdit de lire `Connector.auth_multi_account` hors du seam
et des surfaces d'inventaire, et un test qui pose une surcharge, recharge, et vérifie
que les **verdicts basculent ensemble**.

⚠️ **La carte servie l'a appris à ses dépens (oto-backend#732, 2026-09-01).**
`providers.public_catalog()` posait `auth.cardinality` **depuis le registre**, donc
depuis le défaut du CODE — et c'est cette clé que le dashboard lit pour décider s'il
propose un second compte. Une org élargie par surcharge voyait donc le serveur
**accepter** un geste que l'écran ne **proposait** jamais : le même demi-élargissement
que #409, pris par l'autre bout. Le registre ne pouvait pas faire mieux, il est PUR ;
c'est aux surfaces qui connaissent un requérant — donc une org de contexte — de
repasser par le seam : `cardinality.overlay_for_org(rows, org)`, appliqué dans
`api.public.connectors_catalog` (`GET /api/connectors`) et
`capabilities.connectors.selection._visible_catalog` (`connectors.me` + `oto_search`).
La règle est forcée mécaniquement plutôt qu'écrite : **une fonction qui appelle
`public_catalog()` doit appeler `overlay_for_org()`** — sonde AST dans
`tests/test_connector_cardinality_override.py`, et les deux surfaces sont exercées pour
de vrai dans `tests/connectors/test_carte_cardinalite_servie.py`. Écart **latent** quand
il a été trouvé (aucune ligne de surcharge en base à cette date, donc zéro octet de
différence sur le fil) — un écart qui n'a pas encore coûté, pas une panne. **Le code dit
le possible, la base dit l'exposé.**

⚠️ **Les surcharges vivent en MÉMOIRE, et poser une ligne ne suffit pas.** La
cardinalité est consultée jusqu'à **4× par appel d'outil** (`access/resolve.py`), sur un
serveur mono-loop, contre une base managée distante : une requête par consultation
serait un gel de boucle (`docs/event-loop-perf.md`). Elles sont donc chargées **au
boot** et rechargées par un geste explicite — même patron que le registre d'émetteurs,
et **même conséquence : le rechargement est PAR PROCESS**. Recharger la preprod ne
recharge pas la prod.

```
oto_admin_connector_setting op=set   connector=… value=mono|multi [org_id=…]
oto_admin_connector_setting op=reload          # ← sans lui, la ligne ne fait rien
oto_admin_connector_setting op=list            # `rows` = la base, `active` = CE process
```
(`POST /api/admin/connectors/settings`, SUPER_ADMIN.) `op=list` rend **les deux** —
les lignes de la base et les surcharges vivantes du process : c'est précisément l'écart
qu'un admin doit voir avant de se demander pourquoi son réglage « ne marche pas ».

L'axe d'appel `_account=` fait exception, volontairement : il est **org-agnostique et
permissif** (`accepted_anywhere`). Il est lu par le middleware, où résoudre l'org
coûterait une requête par appel — et il n'autorise rien, il NOMME un compte, la
résolution refusant (actionnable) si ce compte n'existe pas. Le refuser rendrait en
revanche une org élargie incapable de viser son second compte.

⚠️ **`MULTI_ACCOUNT_PROVIDERS` a été retirée le 2026-08-29**, et sa disparition EST le
lot. Elle confondait deux choses sans rapport, et c'est ce qui la rendait
indéboulonnable : la **cardinalité** (qui parle du coffre) et l'**annonce statique de
l'axe `_account=`** (qui parle du schéma des tools, recopié à chaque handshake — donc
curé, `test_call_axes_budget`). La seconde est devenue `account_axis_static`, sur les
mêmes quatre connecteurs. ⚠️ `zoho` et `folk` n'ont **rien** à déclarer : depuis que la
règle couvre les credentials multi-champs, la dérivation les rend multi toute seule —
les garder déclarés aurait reconduit la liste sous un autre nom. Un test AST fige
l'absence de la liste, sous ce nom ou un autre.
- `providers/__init__.py` — l'**agrégateur** : `_DECLARATIONS` (l'ordre, écrit à la main — jamais un `glob`) + toutes les dérivations. Il ne décrit aucun connecteur.

⚠️ La déclaration ne vit **pas** dans `tools/<nom>.py` (le module d'outils du même connecteur) : celui-ci importe `..access`, qui importe le registre — cycle — et `register_all` le charge en try/except, si bien qu'une dépendance optionnelle manquante retirerait le connecteur du **catalogue**, pas seulement ses outils. Le registre doit rester pur et sans dépendance.

Invariants verrouillés par `tests/test_providers_registry_snapshot.py` : le registre EST la concaténation des déclarations, chaque déclaration a exactement un domicile (les deux sens), l'ordre est déterministe.
- **A. Disponibilité** : `availability` ∈ {`self_serve`, `platform_granted`}. platform_granted = grant-only (deny-by-default, ex. `mm`, `gocardless`).
- **B. Visibilité** : `default_active` (ADR 0050 — socle installé d'office au seed d'un nouveau (sub, org) ; **vide depuis le 16/07** : tout le catalogue est en library installable, l'agent guide).
- **C. Credential** : `auth_modes` ⊆ {`byo_user`, `byo_org`, `platform`} ; `keyed` (résolu via `resolve_api_key`) ; `secret_kind` (api_key/basic_auth/fields/refresh_token/oauth/cookie/none) ; `personal_session` ; `env_secret_name` ; `default_quota`.
- **Modèle de saisie multi-champs** (ADR 0011) : `secret_fields` (propriété) = schéma de saisie du credential — `credential_fields` explicites (`CredentialField` name/label/secret/**when**/**choices**) ou dérivés du `secret_kind` (`api_key`=1 champ `key` ; `basic_auth`=`email`+`password`). Vide pour `cookie`/`oauth`/`none` (flux dédiés). SOURCE UNIQUE du formulaire dashboard, de l'endpoint `/api/settings/api-keys`, de `status_for` et du packing — zéro branche par connecteur (ex. Silae = 3 champs déclarés, aucun code spécifique).
- **Un champ peut en SÉLECTIONNER d'autres** (`Connector.field_discriminator`, 2026-08-27) : `http` déclare `auth_mode`, et chaque champ dit par `when=` les modes qui le rendent pertinent. `Connector.fields_for(valeurs)` en dérive les champs à afficher ET ceux dont le `required` s'applique ; `choices=` ferme le jeu de valeurs d'un champ. Le descripteur `auth` publie les trois (`field_discriminator`, `when`, `choices`) pour que le front filtre **sans connaître le connecteur**. Discriminant absent (les ~90 autres) = comportement inchangé.
- **Chargement** : `Connector.modules` = modules `tools/<m>.py` à importer (kind="tools" ; défaut = nom du provider). Voir §Chargement dérivé.

**Tout dérive du registre** (mêmes symboles, ré-export) : `KEY_PROVIDERS`, `ORG_SHAREABLE_PROVIDERS`, `ADMIN_GRANT_ONLY_NAMESPACES`, `QUOTA_DEFAULTS`, `ENV_SECRET_NAMES`, `DEFAULT_BUNDLE/PRESET`. Plus de listes en dur parallèles. `GET /api/connectors` = vue publique.
Helpers : `require_keyed`, `is_byo_user`, `is_org_shareable`, `require_credential(entity_type, name)` (user→byo_user, org→org-partageable).

## Lire et modifier un credential — ce qui sort, ce qui ne sort plus

⚠️ **Une clé posée ne se relit JAMAIS — décision du 2026-08-31 (oto-backend#671).**
Jusqu'à cette date, `GET /api/settings/api-keys/{provider}` rendait la valeur ENTIÈRE,
en clair, de tout champ déclaré `reveal=True` — et c'était le **défaut** : les 49
connecteurs `secret_kind="api_key"` sans `credential_fields` explicites héritaient d'un
`key` révélable, plus 6 déclarés à la main, soit **55 connecteurs**. Le cran `reveal`
est **retiré du registre** (pas neutralisé : le passer lève un `TypeError`) ; `secret`
décide seul, et c'est exactement ce que le front lisait déjà (`auth.fields[].secret`).

Ce que la lecture rend aujourd'hui, à tous les paliers :

- **les champs NON secrets, tels quels** (`base_url`, `auth_mode`, `region`, un email) ;
- **de quoi reconnaître la clé sans la lire** : `configured`, `read_set_at`,
  `read_set_by` (qui l'a posée), et `read_fingerprints` — `{champ: 4 caractères}`, un
  HMAC **lié à la ligne du coffre**, jamais des caractères du secret. Le front affiche
  `•••• 3f7a`. Un champ secret vide n'y figure pas.
- **rien d'autre** : la clé d'un champ secret est **ABSENTE** du corps, pas `null` ni
  `""`. Un champ vidé se lirait « aucune clé posée » et un appelant continuerait sur du
  vide — c'est le mode d'échec de `oto ninja secrets get`, dont l'usage documenté est
  `export FOO=$(…)`, où `export` masque le code de sortie.
- **demander la valeur reçoit un refus nommé** : `?reveal=1` → `403
  secret_never_revealed`, jamais un 200 amputé.

⚠️ **L'empreinte est liée à sa ligne, à dessein.** Une empreinte globale de la valeur
serait la même pour la même clé partout : qui lit l'empreinte d'un palier pourrait poser
un candidat sur une ligne qu'il contrôle et comparer — un oracle de confirmation à
1/65536 sur quatre caractères. Liée à `(entity_type, entity_id, connector, account,
champ)`, la seule façon de comparer est d'écraser la clé qu'on cherchait à confirmer.
Primitive : `journal_secrets.fingerprint()`, même clé HMAC que le masque du journal
(`OTO_MCP_OAUTH_STATE_SECRET`, présent en prod → stable d'un boot à l'autre).

**Ce que la révélation ne servait pas** : la modification PARTIELLE. Elle tient au MERGE
côté serveur (#448), pas à la relecture — le dashboard jetait déjà toute valeur secrète
reçue (`CredentialFieldsDialog.vue`, garde `!f.secret`).

- **L'écriture est un MERGE côté serveur.** Une clé absente du corps est complétée
  depuis le coffre (`credentials_store.merge_with_existing`) ; une clé présente mais
  **vide est un effacement explicite**. Le formulaire du dashboard, qui poste tous
  ses champs, garde donc son comportement de remplacement. **C'est le seul chemin de
  changement d'une clé** : on repose, on ne relit pas.
- **La lecture existe à TOUS les paliers** (oto-backend#448, 2026-08-27).
  `me.credential.get` prend un `scope` (`member` défaut / `group` / `org`, les deux
  derniers gardés admin du palier, l'org_admin subsumant l'admin d'équipe).

⚠️ **Le merge se fait dans la même ligne, jamais par le client.** L'AAD dérive de
`entity_type/entity_id/connector/account` : relire, fusionner et rechiffrer sur place
ne la change pas. Le secret ne repasse à aucun moment sur le fil.

⚠️ **Le corps rendu par le GET est PLAT** : ses autres clés sont celles du connecteur.
`http` déclare un champ nommé `scope` (les scopes oauth2) — d'où les clés d'enveloppe
`read_scope`/`read_account`, préfixées à dessein. Toute clé ajoutée à ce corps doit
rester impossible à confondre avec un `credential_field`.

**Ce que ça a coûté avant** : jusqu'au 2026-08-27, `_get` lisait `MEMBER` **en dur** et
`groups_secrets` n'exposait que set/delete. Un credential d'équipe n'avait donc aucune
surface capable d'en rendre la `base_url`, alors que le registre la déclare non
secrète — et l'écriture étant un remplacement total, changer cette URL exigeait
de réécrire un bearer que rien ne restituait. Piège à perte de données par
construction, sur le chemin de TOUS les ponts clients (ADR 0003/0037). Un repointage
de pont en production a été abandonné devant ce formulaire.

⚠️ Défaut de la même famille, corrigé avec : l'éligibilité de ces trois routes testait
`is_byo_user` **quel que soit le palier** — un connecteur purement `byo_org` comme
`http` se faisait répondre « connecteur inconnu » en lecture comme en retrait, même à
un admin d'org. C'est `is_org_shareable` qui décide aux paliers équipe et org.

## Coffre — `connector_credentials` (table unique)

A remplacé (et les a fait DROP, purge 2026-06-11) les 9 colonnes `users.<provider>_api_key`, `org_secrets`, les colonnes session (`users.linkedin_*`/`crunchbase_*`) et la table `user_google_oauth`. `init_db._drop_legacy_plaintext_stores` exécute les `DROP … IF EXISTS` (idempotent, no-op sur DB fraîche on-prem).

```
connector_credentials(entity_type, entity_id, connector, account, secret_enc,
                      secret_kind, meta JSONB, set_by, set_at,
                      PK(entity_type, entity_id, connector, account))
```
- `entity_type` ∈ {`member`, `user`, `org`, `group`, `tenant`, `platform`} ; `entity_id` = `org:sub` (member, ADR 0033) | `sub` (user, résidu OAuth) | `org_id::text` | `group_id::text` | **slug du tenant** (L-clés PR 1, 2026-08-29 — façade `tenant_vault`) | label de la clé (platform, ADR 0044 §F). Toujours requêter `(entity_type, entity_id)` ENSEMBLE. ⚠️ Cette ligne a dit `{user, org}` du 2026-06 au 2026-08-29 alors que quatre valeurs de plus étaient servies.
- `account` = discriminant **multi-compte** ('' = mono ; ex. email Google). 1 ligne par compte connecté.
- `secret_enc` = enveloppe chiffrée (pas de colonne plaintext). `meta` = satellites NON-secrets (user_agent linkedin/crunchbase, access_token/expires_at/scopes/is_default google).

Store = `credentials_store.py` (calqué sur le palier org, réutilise `db._connect`, jamais d'import circulaire) :
`get_credential` / `get_credential_with_meta` (secret+meta+set_at, déchiffre) / `credential_status` (présence+meta SANS déchiffrer, pour /api/me) / `has_credential` / `set_credential` (chiffre) / `clear_credential` / `update_meta` (merge JSONB sans re-chiffrer) / `list_accounts`.
- **Packing multi-champs** : `pack_secret(connector, fields)` / `unpack_secret(connector, secret)` encodent les `secret_fields` dans l'unique `secret_enc` — 3 formats selon la forme : 1 champ (`api_key`) = valeur brute (back-compat) ; `basic_auth` = `base64("email:password")` (format de fil que le mount distant décode, ex. planity-mcp) ; ≥2 champs = `json`. L'endpoint de saisie et `resolve_credential_fields` passent par là.

## Chiffrement au repos — `crypto.py`

Enveloppe **AES-256-GCM**, **obligatoire** (`set_credential`/`_pk_encrypt` chiffrent toujours ; `crypto.encrypt`/`decrypt` lèvent si master key absente — pas de stockage ni lecture plaintext). Master key **hors-DB** (env `OTO_MCP_MASTER_KEY`, hex64 ou base64-32o ; en prod fetchée de Scaleway Secret Manager au boot, cible KMS unwrap, cf. `ADR 0002 (meta privé otomata-private/docs/adr)`). AAD = `connector_credentials:{entity_type}:{entity_id}:{connector}[:{account}]` (anti-transplant ; segment account omis si vide → compat ascendante mono-compte). Envelope = `key_ref(1o)‖nonce(12o)‖ct`.
- Déchiffrement **JIT** dans `resolve_api_key`/`get_credential` uniquement, jamais loggé ; `status_for` lit la présence (`has_credential`/`credential_status`), ne déchiffre pas. Échec de déchiffrement = LÈVE (pas de fallback silencieux).
- `platform_keys` : secret dans `api_key_enc` (même pattern, AAD `platform_keys:{provider}:{label}`).
- Dump Postgres = **ciphertext only**. Pas de rotation de clé (key_ref réservé). Perte de master key = perte totale → Secret Manager versionné + escrow.

## Résolution + accès (`access/`)

`resolve_api_key(provider) -> (api_key, is_platform)` : (1) clé membre scopée (sub, org de contexte) (`get_member_api_key`→coffre, entity `member`/`{org}:{sub}`, ADR 0033) ; (2) org secret (si `byo_org` + org active) ; (3) platform grant + quota ; (4) McpError actionnable. Le connecteur **`bridge`** universel (ADR 0034) se résout par les **champs standard** (`resolve_credential_fields("bridge")` → `base_url`/`token`/`label`, cascade membre > groupe > org), raise actionnable si absent, **jamais de fallback SOPS serveur** — plus de `meta.base_url` (l'ex-`resolve_remote_credential` per-namespace retiré en B4).
`resolve_credential_fields(provider) -> dict` : credential **multi-champs byo_user** (ex. `silae` : client_id/client_secret/subscription_key) — lit le coffre + `unpack_secret`. **byo-only, pas de platform key ni quota** (le credential EST le grant). Pour les clients in-process s'instanciant avec plusieurs secrets.
`resolve_mount_token(provider)` : token per-user d'un MCP fédéré `kind="mount"` (OAuth atlassian, ou base64 basic_auth planity), injecté en bearer par le proxy.
`status_for` = miroir exact (modes user/org/platform/over_quota/forbidden) — boucle aussi sur les byo_user à `secret_fields` hors `KEY_PROVIDERS` (planity, silae : `user`/`forbidden`). `granted_namespaces_for`/`require_namespace` = gate des namespaces grant-only (deny-by-default), source unique consommée par middleware + meta-tools + REST.

## Palier org

Tables `orgs`/`org_members`(index partiel `org_members_one_active`)/`org_entitlements` ; `org_store/` (`orgs.py` la fiche, `members.py` l'appartenance, `vault.py` les secrets) ; 12 meta-tools `oto_admin_*` (`tools/orgs.py`). Entité = **user ET org, 2 niveaux** (perso prime sur org).

## Folds des secrets de session (cible : coffre unique)

- **LinkedIn / Crunchbase** : cookie chiffré dans `secret_enc`, UA dans `meta` ; `db.set/get/clear_linkedin_cookie`/`crunchbase_session` sur le coffre ; statut /api/me via `credential_status` (sans déchiffrer).
- **Google OAuth multi-compte** : `connector='google'`, `account=email` ; refresh_token chiffré, access_token/expires_at/scopes/is_default/granted_at dans `meta`. Les 6 fns db (`set/get/list/set_default/delete_google_oauth`, `update_google_access_token`) sur le coffre ; `update_google_access_token` = `update_meta` (merge, sans re-chiffrer). Flow OAuth `auth/google.py` inchangé (seule la couche stockage change). ⚠️ access_token reste en **clair dans `meta`** (bearer ~1h, dérivé) ; seul le refresh_token (`secret_enc`) est chiffré.

## Connecteurs remote — bridges (ADR 0003, pilote mm)

`kind="remote"` au registre = **aucun code ni credential client dans oto** : un bridge (service HTTP distant, ex. un bridge back-office client (repo privé)) détient le credential du système client ; oto-mcp = middleware générique `tools/remote.py` (tools `<ns>_describe` + `<ns>_call`, forward bearer M2M + `X-Oto-Sub` pour l'audit côté bridge). Le credential d'org = `secret` = token M2M + `meta.base_url` = endpoint (posé via `oto_admin_set_org_secret(..., base_url=…)`). Gating inchangé : grant-only + `require_namespace` au call-time. Contrat bridge (`/healthz`, `/describe`, `/call`) : ADR 0003 du meta-repo. Le mount MCP-to-MCP (`otomata#16`) = flavor complémentaire pour les remotes déjà-MCP.

## Projection instances (ADR 0038 B4)

Le coffre est relu comme un **listing d'instances possédées nommées** (une ligne
`(entity_type, entity_id, connector, account)` = une instance), en **lecture pure,
sans jamais déchiffrer** : capacité `connectors.instances.list` (MCP
`oto_instance(op="list")` (console ADR 0047), REST `GET /api/me/connector-instances`,
`capabilities/connectors/instances.py`). Agrège les 4 familles que la cascade
résout — membre `(org, sub)` > mes groupes de l'org > org > clés plateforme
(grants user/org + free-tier via `db.list_platform_keys_meta`, le pendant
non-déchiffrant de `list_platform_keys`). Chaque instance porte un **`ref` stable
opaque** (grammaire dans `instance_refs.py`, projection 1:1 de la PK ;
`platform:{id}` pour les clés plateforme) — future cible des bindings B5 et de
l'axe `instance=` B6. Métadonnées seulement (meta public, jamais un bearer) ;
limite : les `config_fields` packés dans `secret_enc` (ex. `data_center`) ne
sortent pas — seule la part `meta` est projetée en `config`. Le « gagnant » de la
cascade reste dit par `status_for` (une seule vérité) ; la projection ne porte
que l'ordre de proximité (tri membre < groupe < org < plateforme).

## L'instance, OBJET — `connector_instances` (lot L6, blueprint ADR 0053-D9)

Depuis le 2026-08-27, une instance de connecteur **existe comme objet**, dans une table
posée **à côté** du coffre : `connector_instances(id, connector, owner_type, owner_id,
account, label, config, visibility, parent_id, created_at, revoked_at)`. Arbitrage R1,
prononcé par Alexis : *« la table à côté »*.

**Pourquoi à côté et pas dans le coffre.** L'AAD lie le ciphertext aux **quatre colonnes
d'identité de SA ligne** (`credentials_store._aad`), pas à la table qui la porte : une
table posée à côté, la ligne du secret gardant ses quatre colonnes, donne l'identifiant
stable **sans un octet de rechiffrement**. Une fois le rechiffrement hors de l'équation,
ce qui décide est le fond : le modèle prévoit des instances **sans secret** — une
instance `http` qui ne fige qu'une `base_url` (0057), une sous-instance qui ne pose
qu'une *détermination* (« le compte de Jane », 0053-D9-3). Un objet qui n'est plus un
credential n'a rien à faire dans une table qui l'est.

**Le lien avec le coffre est le quadruplet, et c'est une FK LOGIQUE** —
`owner_type`/`owner_id`/`connector`/`account` **sont** `entity_type`/`entity_id`/
`connector`/`account`, à l'octet près. Pas de FK déclarée, délibérément : le coffre n'a
aucune clé de substitution (sa PK **est** ce quadruplet, donc rien à quoi un
`credential_id` pourrait pointer), et une vraie FK interdirait les instances sans secret
le jour où elles arrivent. Le sens du pointeur suit la même lecture : un `instance_id`
posé **sur** le coffre serait perdu à chaque `rename_account` (= `_upsert` d'une ligne
neuve + `_delete` de l'ancienne, et la liste de colonnes de l'INSERT ne le nommerait
pas), donc réparable seulement en éditant les primitives d'écriture du coffre.

**Une instance vivante par ligne de coffre**, tenu par la base : index UNIQUE **partiel**
sur le quadruplet (`WHERE revoked_at IS NULL`). Son **jumeau non partiel** n'est pas un
doublon : le backfill demande « existe-t-il une instance, **archivée comprise** ? » —
c'est ce qui l'empêche de *ressusciter* une instance retirée à la main entre deux boots.
⚠️ Ne pas les « harmoniser » : leurs deux lectures sont opposées (même partage des rôles
qu'entre `idx_grants_grantee` et `idx_grants_resource_grantee`).

### L'instance naît à la POSE (pièce 2, 2026-08-28)

Jusqu'au 27/08 l'instance naissait **au boot** — le prix assumé de ne toucher aucun chemin
d'écriture du coffre, et une fenêtre pendant laquelle une clé fraîche n'avait pas
d'identifiant. La pièce 2 ferme la fenêtre, et le fait **au fond**.

**Un seul point d'accroche, parce qu'il n'y en a qu'un à avoir.** Le relevé est net : il
existe **un seul `INSERT INTO connector_credentials`** et **un seul `DELETE`** dans tout
le dépôt — `credentials_store._upsert` et `._delete`. Toutes les surfaces déclaratives
(clé membre, clé d'org, clé d'équipe, clé plateforme, jeton d'API, session navigateur,
flux OAuth google/zoho/salesforce/folk/atlassian) y aboutissent par `set_credential` /
`clear_credential`. Accrocher la naissance aux surfaces aurait demandé une dizaine de
crochets pour le même effet, avec une chance sur dix d'en oublier un.

| Geste sur le coffre | Effet sur l'instance |
|---|---|
| poser une clé (n'importe quelle surface) | **naissance**, dans la même transaction |
| roter le secret d'une clé existante | rien — c'est la même instance |
| retirer une clé | **archivage** (`revoked_at`), jamais un `DELETE` |
| reposer une clé retirée | une instance **NEUVE** (id neuf) ; l'archivée reste archivée |
| renommer un compte (`rename_account`) | l'instance **SUIT**, elle garde son id |
| supprimer une équipe · déconnecter tous les comptes Google · retirer une app d'éditeur | archivage de toutes les instances visées |
| accorder/retirer un accès plateforme, suspendre, marquer un défaut, rafraîchir un jeton | rien — ces gestes n'écrivent que du partage ou du `meta` |
| basculer un compte vers un autre annuaire (`migrate_sub`) | rien, **et c'est le point** (voir plus bas) |

**Pourquoi le renommage a son geste à lui.** `rename_account` rechiffre — l'`account`
entre dans l'AAD — donc il écrit une ligne neuve et supprime l'ancienne. Laissé aux seuls
crochets, il tuerait l'instance et en ferait naître une autre : soit exactement ce qu'un
ref composé fait déjà, et qu'on remplace. Le déplacement se fait donc **en premier**, dans
la même transaction ; après lui les deux crochets sont des non-événements. Le geste rend
un `RenameOutcome` et **ne tait jamais un archivage** : si une instance vivante occupait
déjà l'arrivée (un écart), l'arrivée gagne, le départ s'archive, et l'objet rendu dit
lequel au profit duquel.

**Une bascule de compte ne détache pas l'instance de sa ligne.** Migrer un sub vers
l'annuaire d'un tenant ne repointe **jamais** `connector_credentials.entity_id` (l'AAD :
la ligne deviendrait indéchiffrable ; l'utilisateur repose ses clés). La ligne reste donc
en place, vivante — et son instance aussi. Repointer l'instance seule la **détacherait**
de sa ligne : un objet qui désigne une clé qui n'existe pas, strictement pire que rien.
Le garde-fou d'inventaire (`tests/test_migrate_sub_inventory.py`) portait la consigne
inverse jusqu'au 28/08 ; elle est corrigée sur place.

**Ce que la pièce 2 NE fait pas.** `config` reste **inerte** — la config publique vit
toujours dans `meta`, les `config_fields` packés vivent dans le ciphertext (les sortir
suppose de déchiffrer, c'est un lot à part). `label` reste vide, le nom affiché reste
dérivé. `visibility` et `parent_id` restent posés et inertes.

**Ce que le filet de boot fait, et ne fait pas.** `_init.init_db` continue de nommer les
lignes de coffre orphelines (idempotent, en lots, journalisé, rejoué à chaque boot) — mais
c'est désormais un **filet**, plus le chemin de naissance : après ce lot il ne nomme plus
rien (0 ligne, mesuré en preprod). Il laisse `label` et `config` vides. Aucune ligne du
coffre n'est touchée, aucun secret déchiffré. Un `entity_type` hors vocabulaire est compté
et journalisé, jamais inventé — le CHECK de la table emporterait sinon la transaction de
schéma entière, sur une base partagée avec la production. ⚠️ **À la POSE, le même cas est
un refus NOMMÉ** (`db.connector_instances.OwnerKindUnknown`) qui emporte la pose entière :
nommer fait désormais partie de poser, et une clé que rien ne désigne n'a pas à naître.

⚠️ **L'angle mort du filet, nommé plutôt que bouché.** Sa garde `NOT EXISTS` ne filtre pas
`revoked_at` : il **refuse** de nommer une ligne de coffre qui porte déjà une instance
archivée — c'est ce qui l'empêche de ressusciter une instance retirée à la main. Après la
pièce 2 ce cas ne naît plus que d'un geste manuel en base (archiver une instance en
laissant vivre sa clé). Ce n'est donc pas au filet de le rattraper, c'est à l'**invariant**
de le montrer.

### Qui VOIT une instance — R9, la visibilité dérivée

Arbitrage prononcé par Alexis le 27/08 : *« la visibilité est une propriété de
l'INSTANCE »*, dérivée de la chaîne — **découvrable par les scopes sous son
propriétaire, dans la même org, jamais cross-org**, avec surcharge explicite du
propriétaire. Le domicile de la dérivation est
`oto_mcp/connectors/instance_visibility.py` ; `GET /api/me/connector-instances` et
`oto_instance op=list` servent le résultat en `visible_to`.

**Ce que ça n'est PAS, et c'est la moitié du lot.** `visible_to` ne filtre rien,
n'élargit aucune liste, ne gate aucun appel. Un non-membre continue de voir *aucune
clé configurée* — la divulgation (« il existe un accès à demander, et chez qui ») reste
une question **produit**, que R9 range dans un réglage d'org opt-in, plus tard. Ce qui
est livré est **descriptif** : la même liste qu'avant, où chaque instance dit qui la
voit.

**Pourquoi la question est dérivable.** 0053-D2 a retiré l'enjeu de protection —
masquer ne protège de rien, tout se refuse à l'appel. Ce qui reste est de l'ergonomie,
et sa réponse honnête est *qui peut la résoudre la voit*. La résolution est déjà écrite
une fois pour toutes dans `access.cascade.walk_cascade` ; la dérivation l'**inverse**.

| Palier | Audience | Gate |
|---|---|---|
| membre (et le résidu `user`) | `user:<sub>` — la personne, jamais le couple `(org, sub)` | — |
| équipe | `group:<id>` | `org_shareable` : sinon **personne** (la clé existe, la cascade ne la lit jamais) |
| org | `org:<id>` | idem |
| plateforme | les bénéficiaires | `auth_modes ∋ platform` : sinon **personne** |
| *tous* | + `share_side` (prêts nominatifs, ADR 0044) — une **extension**, jamais une allowlist ; peut viser hors de l'org | — |

Le palier plateforme est le seul dont l'audience n'est pas structurelle. Trois issues,
dans l'ordre où la résolution les prend : la **chaîne accorde** (0053, L5) ⟹ les
bénéficiaires des arêtes vivantes ; la **chaîne refuse** (des arêtes existent, toutes
révoquées) ⟹ personne, **sans repli** (c'est ce qui rend une révocation vraie) ; la
**chaîne est muette** ⟹ l'ancien chemin, à l'identique (`closed` ⟹ l'allowlist ;
`open` ⟹ l'allowlist si elle existe, sinon `platform`, le mot de l'audience non bornée).

**La surcharge** vit dans `connector_instances.visibility` : `inherited` (défaut — la
dérivation décide), `hidden` (le propriétaire seul), `org` (l'org du propriétaire en
plus). ⚠️ **Rien ne l'écrit aujourd'hui** — les deux autres branches sont écrites et
testées, pas servies ; le geste qui les pose est un lot produit, et un test fige
l'absence d'écrivain. ⚠️ `hidden` est un cran d'**ergonomie**, pas de sécurité : celui
qui résout continue de résoudre, il cesse seulement de la voir listée.

⚠️ **Le risque du lot, et son garde-fou.** Inverser un walker, c'est risquer d'en
écrire une deuxième copie — le défaut que `keyStack.ts` porte déjà côté dashboard, et
qui ne casse pas : il **ment**. Deux réponses, toutes deux mécaniques : (1) aucune règle
n'est recopiée — les gates sont lus à leur source (le registre, les colonnes de partage
du coffre, `grants_chain`), et un registre consulté deux fois n'est pas une duplication ;
(2) `tests/test_instance_visibility.py` **confronte** l'audience dérivée au verdict réel
de `walk_cascade` sur un vrai PostgreSQL, pour deux appelants aux droits différents. Si
les deux divergent, ce test rougit.

**Coût** : deux requêtes en lot pour toute la liste (les identifiants et le partage),
jamais deux par instance ; plus, pour les seules clés plateforme d'un connecteur
basculé, la lecture de leurs arêtes. Fail-open loggé et **séparé** de celui de `id` :
le partage peut tomber sans emporter l'identifiant.

### Nettoyer les orphelines d'avant la pièce 2 — une commande, pas un boot

Entre la pièce 1 (le boot NOMMAIT chaque ligne de coffre) et la pièce 2 (la pose nomme,
le retrait archive), **chaque suppression de credential fabriquait une orpheline** : la
ligne partait, l'instance restait vivante. Mesuré sur la base servie le 2026-08-28,
avant déploiement : **2 orphelines** sur 139 instances vivantes (`member … slack` et
`org … aiark`) — exactement les retraits qui contournaient l'entonnoir. La pièce 2 ferme
la source ; elle ne nettoie pas le passé, parce que nettoyer n'est pas le travail d'un
boot.

```
# sur la box, après déploiement — dry-run par défaut : liste, n'écrit rien
cd /opt/oto-mcp && ./.venv/bin/python -m scripts.archive_orphan_instances
#   --apply pour écrire
```

Le script **archive** (`revoked_at`, motif `vault_row_missing`), jamais un DELETE : si un
binding ou une arête a nommé l'orpheline, « elle a été retirée » et « elle n'a jamais
existé » ne sont pas le même verdict. **Idempotent** — son prédicat est « vivante ET sans
ligne de coffre », donc un second passage rend 0, et il ne peut pas mordre sur une
instance saine.

⚠️ **Hors du boot, délibérément** (ADR 0065). Le filet de démarrage ne sait qu'insérer ;
lui faire archiver au boot ferait d'un ordonnanceur de maintenance un écrivain de masse
sur une base **partagée avec la production**.

**Le motif (`revoked_reason`), et pourquoi il vaut une colonne.** Sans lui, un archivage
est muet six mois plus tard : impossible de distinguer « l'utilisateur a retiré sa clé »
d'une réparation de maintenance. Trois valeurs, posées par ce dépôt seul :
`credential_removed` (le cas normal), `renamed_onto_existing` (un renommage vers une
instance déjà vivante), `vault_row_missing` (ce script). Nullable et sans `CHECK` — le
vocabulaire est fermé par le code qui écrit, sinon le prochain motif est une migration.

### L'invariant, et la requête qui le dit

> Chaque ligne de coffre a **exactement une** instance vivante, et chaque instance vivante
> a **sa** ligne de coffre.

Les deux sens comptent : une instance orpheline (« je désigne une clé qui n'existe pas »)
est au moins aussi grave qu'une clé sans nom, et un binding ou une arête peuvent la
nommer. La requête vit dans `tests/test_connector_instances_birth_live.py`
(`INVARIANT_SQL`), où elle est exercée sur les deux écarts — et c'est la même qu'on joue
en preprod après un merge :

```sql
SELECT COALESCE(c.entity_type, i.owner_type) AS owner_type,
       COALESCE(c.entity_id,   i.owner_id)   AS owner_id,
       COALESCE(c.connector,   i.connector)  AS connector,
       COALESCE(c.account,     i.account)    AS account,
       CASE WHEN i.id IS NULL THEN 'coffre sans instance'
            ELSE 'instance sans ligne de coffre' END AS ecart
  FROM connector_credentials c
  FULL OUTER JOIN (SELECT * FROM connector_instances WHERE revoked_at IS NULL) i
    ON  i.owner_type = c.entity_type AND i.owner_id = c.entity_id
    AND i.connector  = c.connector   AND i.account  = c.account
 WHERE c.entity_type IS NULL OR i.id IS NULL
 ORDER BY 1, 2, 3, 4;
```

Zéro ligne = l'invariant tient. Les instances **archivées** sont hors périmètre des deux
côtés : c'est leur raison d'être (une consommation ou un partage passés doivent rester
relisibles).

**Ce que rien ne fait encore.** Ni la cascade (`access.walk_cascade`), ni la résolution
(`access.resolve_credential`) ne lisent cette table — c'est le lot L7. Le coffre, lui, y
**écrit** depuis la pièce 2, et n'y lit jamais : l'intention est gardée à deux grains par
`tests/test_connector_instances_l6.py` — une allowlist de **six** fichiers, et, à
l'intérieur du coffre, un relevé AST par FONCTION qui n'admet que les cinq primitives
d'écriture. Un lecteur de credential qui se mettrait à lire les instances ferait dépendre
la désignation d'une clé d'autre chose que du quadruplet ; c'est le lot L7, avec sa revue.

### `inst:{id}` — la forme de fil

`instance_refs.make_instance_ref(id)` rend `inst:{id}`, et `parse_ref` accepte désormais
**les deux grammaires** : les refs composés sont déjà distribués (bindings de slot B5,
axe `_instance=`, `resource_id` des arêtes de `grants`) et rien ne les réécrit ici.
`GET /api/me/connector-instances` sert donc `id` **en plus** de `ref` — résolu en **une**
requête pour toute la liste, **fail-open** (l'identifiant n'est consommé par rien encore ;
faire tomber le listing des clés de quelqu'un pour lui serait hors de proportion), donc
**`id` peut être absent** et le client garde `ref`. ⚠️ Depuis la pièce 2 (28/08), une
absence n'est plus jamais « la clé est trop fraîche » : elle ne peut venir que du
fail-open. La phrase servie a été corrigée en conséquence — c'est le seul changement
d'empreinte du lot.

⚠️ **`inst:` se PARSE mais ne se résout pas.** Les deux gardes de pose — l'axe
`_instance=` (`access.rbac.guard_instance_access`) et le binding de projet — le refusent
**nommément** : sans cette branche, un `inst:` s'entendrait dire que « les refs
`platform:` ne s'épinglent pas », un message faux qui envoie chercher au mauvais endroit.

## Credentials qui se CONSOMMENT à l'usage (rotation)

Certains fournisseurs invalident le jeton à chaque utilisation et en renvoient un neuf
— **Salesforce l'impose** sur les External Client Apps (contrôle verrouillé, « paramètre
obligatoire », application 2026, donc chez *tous* les clients).

**La règle : sous rotation, toute LECTURE est une ÉCRITURE.** Tout chemin qui touche au
jeton en devient consommateur et doit persister le remplaçant — sinon il détruit le
credential en s'en servant.

Les consommateurs, à traiter **ensemble** (les traiter un par un ne marche pas, chacun
suffit à tuer la connexion) :

| Consommateur | Ce qu'il lui faut |
|---|---|
| appel d'outil | rappel `on_refresh` → réécriture à l'entité résolue |
| sonde de vérification | l'**entité sondée** (`connector_verify.run(instance=…)`) — la cascade désigne la clé la plus PROCHE, pas celle qu'on teste |
| sonde post-écriture d'un callback OAuth | *retirée* — une requête navigateur n'a pas de contexte authentifié, donc aucun moyen de savoir où réécrire |
| script de diagnostic | il consomme comme les autres : préférer la sonde du serveur |

**L'écriture est CONDITIONNELLE**, jamais un écrasement : on ne réécrit que si le jeton
stocké est encore celui qu'on a lu. Deux appels concurrents — ou preprod et prod, qui
partagent la base — peuvent avoir tourné entre-temps ; remettre en place un jeton déjà
consommé est précisément ce que le fournisseur traite comme une compromission (Salesforce
révoque alors le jeton courant *et* tous les access tokens associés).

Deux corollaires qui coûtent cher quand on les découvre en production :

- **un cache d'access token porté par l'instance de client ne sert à rien** côté serveur
  (une instance par appel MCP) : sans cache process-wide, on rafraîchit — donc on fait
  tourner le jeton — à chaque appel d'outil. C'est ce qui transforme la rotation en
  problème explosif plutôt que contraignant ;
- **une sonde n'est plus « sans effet de bord »**, et ne peut pas l'être.

### Application ≠ jeton

Corollaire de modèle, visible sur Salesforce (`auth/salesforce.py`) : l'**application**
OAuth (`client_id`/`client_secret`/`login_url`) est une **infrastructure d'org** — un
admin la pose une fois ; le **refresh token** est une **identité** — il appartient à qui
consent.

D'où une asymétrie délibérée : l'application se **lit en cascade** du scope demandé vers
le haut (membre → équipe → org), le jeton s'**écrit au scope demandé** exactement. Un
membre consent donc avec l'application de son org sans jamais en connaître les
identifiants. La cascade **remonte et ne descend jamais** : consentir pour l'org
n'utilisera pas l'application d'un particulier, sinon la connexion de toute l'org serait
adossée aux identifiants d'une personne.

⚠️ L'aller (`build_auth_url`) et le retour (`read_saved_fields`) doivent appliquer la
**même** règle : un code d'autorisation est émis pour un `client_id` précis, l'échanger
avec un autre échoue — après le consentement de l'utilisateur, au pire moment.

### App d'ÉDITEUR — le cran au-dessus de l'org

Prolongement direct d'« Application ≠ jeton » : si l'application est une infrastructure,
elle peut être fournie par **oto** plutôt que par chaque org. Sans ce cran, un connecteur
à consentement impose le mode **Self Client** — l'utilisateur crée lui-même une app dans
la console du fournisseur et coche ses scopes à la main (3 incidents : #190, #202, Desk
articles-only). Avec, il ne reste que le geste utile : consentir.

- **Rangement** : scope `PLATFORM` du coffre, `entity_id = editor:<data_center>` — une
  app OAuth est enregistrée dans **sa** région (`accounts.zoho.eu` rejette un client
  `.com`), donc la région fait partie de la clé. Accesseurs
  `credentials_store.{set,get,list,clear}_editor_app`.
- **Ordre de lecture** (`auth.zoho.app_fields`) : le BYO **prime** (membre > équipe >
  org) — une org qui veut voir SON app dans ses logs la pose et rien ne change pour
  elle ; l'app d'éditeur n'est le repli que si personne n'a rien apporté.
- ⚠️ **Invariant qui rend le rangement sûr** : `walk_cascade` ne propose le palier
  plateforme que si le connecteur déclare `auth_modes ∋ 'platform'`. Les connecteurs à
  consentement ne le déclarent pas ⟹ l'app d'éditeur n'est **jamais** servie comme
  credential d'appel. Sans ça, un membre qui n'a pas consenti hériterait d'une app
  **nue** (sans `refresh_token`) et se prendrait un échec OAuth opaque au lieu de
  s'entendre dire de se connecter. Figé par `tests/test_editor_app.py` (avec
  contre-épreuve sur un connecteur qui, lui, déclare le mode plateforme).
- **Pose** : `POST /api/admin/editor-apps` (super admin, capacité
  `platform.editor_app.set`). **REST seulement** — un secret brut en argument d'outil
  MCP transiterait par le contexte du modèle.
- **Conséquence de rotation** : `persist()` range une COPIE de l'app dans le credential
  né du consentement. Roter l'app d'éditeur ne casse donc pas les connexions déjà
  établies… jusqu'à leur prochain refresh, qui échouera avec l'ancien `client_secret`.

### Grant mort : marquer, plus jamais purger (oto#25 lot a, 2026-09-04)

Un refresh token révoqué (`invalid_grant`) n'efface plus la ligne du coffre — ça la
rendait indiscernable d'un credential jamais posé, un repli qui masque un problème
plutôt que de le nommer. Elle se fait marquer (`meta.health_ko` + `meta.health_reason`
= motif fournisseur **brut**, pas une catégorie opaque), même mécanisme que la sonde
`verify` des connecteurs keyés. Concerne aujourd'hui les mounts OAuth fédérés au scope
LEGACY `("user", sub)` (`auth/atlassian.py`, `auth/folk.py`) — récit complet, ce que ça
rend observable (`/api/me` via `connectors/link.py::LinkState`) et ce que ça NE rend
PAS observable (`oto_instance op=verify`, faute de sonde enregistrée et d'un walker qui
sache lire ce scope) dans `connector-model.md` §« Purge silencieuse des mounts OAuth ».
Changement de comportement **servi** — à annoncer avant tag.

### L'aide partagée, généralisée à salesforce/zoho (oto#25 lot b2, 2026-09-04)

Le mécanisme du lot (a) était PRIVÉ à `capabilities/connectors/verify.py`
(`_FLAGGABLE` + `_record_health`, sous ces noms). Extrait en module public
`connectors/health.py` — `FLAGGABLE_SCOPES` (member/group/org, **et** le scope
LEGACY `user` : aussi étroit que member, un seul utilisateur, jamais atteint par la
cascade de `verify`) + `record_health` (utilisé par `verify.py`, démarque aussi sur
succès) + `mark_rejected` (la façade neuve : un module qui connaît son ENTITÉ
directement, sans passer par `ResolvedCtx`). `verify.py`, `auth/atlassian.py` et
`auth/folk.py` appellent maintenant ce module au lieu de leurs anciennes
définitions/appels directs à `credentials_store.update_meta` — même comportement,
un seul endroit qui sait marquer une ligne rejetée.

Deux connecteurs NEUFS rejoignent le mécanisme : `tools/salesforce.py` et
`tools/zoho.py` marquent désormais leur ligne au refus du REFRESH
(`SalesforceAuthError`/`ZohoAuthError`, exceptions **typées** que lève le CLIENT
oto-core — jamais un 401 nu d'un geste applicatif ordinaire, ex. permission
manquante sur un enregistrement précis avec une clé par ailleurs saine), PUIS
RE-LÈVENT toujours l'exception d'origine — marquer n'est jamais un fallback qui
avale l'erreur réelle. `tools/zoho.py` bascule au passage de
`access.resolve_credential_fields("zoho")` vers
`access.resolve_credential("zoho", want="byo")` (mêmes champs, plus l'entité
gagnante — nécessaire pour marquer la bonne ligne).

**`google` reste explicitement EXCLU de ce lot** : un WIP concurrent touche son
retour OAuth (`auth/google.py`) au moment du lot b2 — à faire une fois ce WIP
stabilisé, dans un lot séparé.

✅ **Fait dans ce lot séparé (2026-09-05, une fois oto-backend#877 poussé et
tagué)** : `credentials_for` (le seul appelant de `_refresh_access_token`) marque
sur `invalid_grant` (`GoogleReauthRequired`, même règle `oauth_flow.grant_is_dead`)
puis **relève toujours** — contrairement à atlassian/folk, `credentials_for` n'a
jamais rendu de `None` muet et ce lot ne change pas ce contrat. Démarque
explicitement au refresh réussi : `update_google_access_token` MERGE le meta
(`update_meta`, JSONB `||`), donc un `health_ko` posé plus tôt n'aurait jamais
disparu tout seul (même raison que le point 3 ci-dessous pour Salesforce —
contrairement à atlassian/folk, dont le remplacement total du meta démarque déjà
par accident). Bancs : `tests/auth/test_google_health_marking.py`.

### Le démarquage (oto#25 lot b3, 2026-09-05)

Trois déclencheurs, et AUCUN autre — surtout pas un appel `ok=true` quelconque, qui
crierait au loup à tort sur un simple throttle passager :

1. **`oto_instance op=verify` vert** — déjà en place depuis toujours (`record_health`
   appelé avec `ok=True` par `verify.py`), rien à faire ici.
2. **Une nouvelle clé posée (reconnexion)** — déjà acquis, mais par un mécanisme
   ACCIDENTEL qu'il fallait vérifier plutôt que supposer :
   `credentials_store.set_credential` REMPLACE tout le `meta` (jamais un merge, cf.
   son propre docstring) — poser une clé, quel que soit le chemin
   (`persist_token` d'atlassian/folk, ou `capabilities/me_credentials.py::_set` pour
   salesforce/zoho et tous les connecteurs keyés) écrit un `meta` neuf qui ne reporte
   JAMAIS un `health_ko` d'avant. Figé par des tests dédiés
   (`tests/auth/test_oauth_dead_grant_marks_rejected.py`,
   `tests/test_me_credentials_capability.py`) plutôt que laissé implicite : une
   régression de `set_credential` vers un merge romprait cette garantie en silence.
3. **Un refresh réussi** — le point qui manquait réellement :
   - `atlassian`/`folk` : même mécanisme que le point 2 (`access_token_for` écrit
     `meta={access_token, expires_at}` au chemin nominal, un remplacement qui efface
     `health_ko` de la même façon).
   - `salesforce` : `on_refresh` (`_rotation_writer`) n'est invoqué qu'APRÈS un
     refresh d'access token réussi — démarquage ajouté ici, **inconditionnel**
     (qu'il y ait ROTATION du refresh token ou non ; beaucoup de Connected Apps n'en
     imposent pas, et le démarquage ne doit pas attendre un événement qui n'arrivera
     peut-être jamais).
   - `zoho` : **couvert depuis oto-core v1.116.0.** Le client expose désormais un
     `on_refresh` symétrique à celui de Salesforce, et le backend le câble sur le
     démarquage. C'était un manque assumé tant que le refresh réussi restait
     entièrement interne au client (cache process-wide), donc invisible du backend :
     le corriger demandait de toucher oto-core, ce qu'un lot backend-only ne pouvait
     pas faire.
     ⚠️ Le rappel ne part **jamais sur un succès de cache**, et c'est ce qui en fait
     une preuve de vie. Le cache Zoho est process-wide et dure une heure : démarquer
     sur un jeton qu'il a servi repeindrait une ligne en vert sur la foi d'un refresh
     vieux d'une heure. Il ne part pas non plus sur un refresh en échec — un
     credential refusé ne doit surtout pas se faire démarquer comme sain.

## Validation

Pas de framework de tests dans le repo → validation manuelle sur **PG16 jetable (docker)** + revue adversariale par phase. Migrations idempotentes au boot (`init_db` : ALTER additifs, PK 4-col, backfills, encrypt-existing, drop-plaintext gaté).

## Déchiffrer un credential ad-hoc sur la box (ops)

```bash
# ⚠️ Déchiffrer un credential ad-hoc (crypto.decrypt / _reveal / credential_status) :
# `OTO_MCP_MASTER_KEY` n'est PAS dans .env — start-encrypted.sh la fetch au boot
# depuis Scaleway Secret Manager. Un script qui ne source que .env voit
# `encryption_enabled()=False` → tous les déchiffrements lèvent RuntimeError (FAUX
# négatif, ≠ InvalidTag). Pour reproduire le runtime, répliquer le fetch :
#   set -a; . .env; . /etc/oto-mcp/scw.env; set +a
#   RESP=$(curl -s -H "X-Auth-Token: $SCW_SECRET_KEY" \
#     ".../secret-manager/v1beta1/regions/fr-par/secrets/<id>/versions/latest_enabled/access")
#   export OTO_MCP_MASTER_KEY=$(echo "$RESP" | python3 -c 'import json,sys,base64; print(base64.b64decode(json.load(sys.stdin)["data"]).decode())')
# Vécu 2026-06-22 (triage Sentry InvalidTag : 1 ligne de mount corrompue, écrite
# avec une clé ≠ courante — les autres lignes déchiffraient → pas un souci de clé ;
# fix = purge → re-OAuth). `status_for` doit utiliser `credential_status` (présence
# sans déchiffrer), jamais `get_credential_with_meta`, pour ne pas 500 /api/me.
```
