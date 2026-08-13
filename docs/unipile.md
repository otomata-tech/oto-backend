# Unipile — messagerie hébergée (WhatsApp/Telegram/Instagram/LinkedIn)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Tools `whatsapp_chat` / `telegram_chat` / `instagram_chat` / `messenger_chat` /
`twitter_chat` (`op=list|read|send`) = messagerie **hébergée Unipile**, sous le connecteur `unipile`
(`modules`/namespaces = `unipile, whatsapp, telegram, instagram`). Générés par la
factory `tools/unipile.register_messaging_tools(mcp, channel)` — l'API `/chats`
d'Unipile est channel-agnostic ; chaque tool résout l'`account_id` du canal pour le
user (no-fallback, `tools/unipile.unipile_client(provider)`).

> **⚠️ La surface LinkedIn s'appelle `linkedin_unipile_*` depuis le 2026-08-10**
> (ADR 0010 §Amendement + ADR 0047 §Amendement, oto-backend#279) : **38 tools
> `unipile_*` → 8 tools à `op=`** — `search`, `facets`, `profile`, `chat`, `post`,
> `network`, `account`, `job`. Deux raisons cumulées : le namespace portait le
> **fournisseur** (Unipile) pour des tools qui sont du **LinkedIn**, alors que les 4
> autres canaux du MÊME connecteur portaient déjà leur capacité (`whatsapp_*`…) ; et
> le catalogue pesait 51 tools à lui seul. Le suffixe `_unipile` distingue la **session
> opérée** de la donnée achetée (`linkedin_*` = AI Ark) — les deux surfaces ne sont pas
> substituables, aucune ne prend le nom nu.
>
> Conséquence technique : `namespace_of` résout désormais au **plus long préfixe déclaré
> au registre** (`tool_visibility.py`) et non plus au 1er token — sans quoi
> `linkedin_unipile_*` tomberait sous le connecteur `linkedin` (AI Ark) : mauvais
> credential, mauvaise activation, mauvaise sélection. Le changement est additif (aucun
> autre namespace n'est multi-token) et verrouillé par
> `tests/test_linkedin.py::test_linkedin_unipile_namespace_resolves_to_unipile`.
>
> **`unipile_connect_start` garde son nom** : il est multi-canal
> (`channel=linkedin|whatsapp|…`), donc il n'appartient à aucune capacité. Sa place
> cible est `oto_connector op=connect` (lot séparé, #279).
>
> **Les 5 autres canaux ont suivi le même jour** : `{c}_list_chats`/`{c}_read_chat`/
> `{c}_send_message` → **`{c}_chat(op=list|read|send)`**, soit 15 → 5 tools. Même
> factory `register_messaging_tools`, donc un seul jeu de cas de test les couvre tous.
> Le canal reste dans le NOM — c'est ce qui le rend trouvable par l'agent — et ces
> namespaces restent NUS (mono-fournisseur : pas de suffixe, cf. la règle).

Connexion = hosted-auth Unipile (dashboard, `?channel=whatsapp|telegram|instagram`),
`account_id` per-membre dans `unipile_accounts` (PK `(sub, org_id, provider)` — scope
membre ADR 0033 B4 : le binding vaut dans l'org de contexte, un canal se connecte par
org). Même gate d'option par org que LinkedIn (comp admin `access.has_option` ; plus
de paiement).

> **Instance PERSONNELLE cross-org (issue #172, amende ADR 0033).** Un compte de
> messagerie hébergé est intrinsèquement **par-personne** (le login LinkedIn/WhatsApp
> EST l'humain, pas l'appartenance) → le connecteur `unipile` porte le flag registre
> **`Connector.personal_cross_org=True`**. Conséquence : la clé membre d'un `sub` posée
> dans UNE org **le suit dans TOUTES ses orgs** (résolution de proximité, pas seulement
> le pin `instance=` d'ADR 0038). Mécanique — **même seam déterministe**
> `access.personal_instance_org(sub, connector)` (org PERSO d'abord, sinon la clé la plus
> récente ; exclut l'org de contexte) partagé par les points de résolution, pour que
> **clé ET compte restent appariés** (jamais la clé d'ici + le compte de là-bas) :
> 1. **clé** — `_resolve_credential_impl` : quand la clé membre LOCALE manque, un
>    connecteur `personal_cross_org` retombe sur ma clé membre d'une autre org (mode
>    `user`, `entity_id='{org_porteuse}:{sub}'`) AVANT les paliers partagés/plateforme.
>    Même `sub` ⟹ **zéro usurpation** ; ne mord qu'en l'absence de clé locale (nul impact
>    mono-org / déjà keyé). `credential_mode_for` + `unipile_api_key_for` **miroitent**
>    (sinon l'UI/le connect verrait « platform » là où la résolution trouve ma clé perso).
> 2. **compte** — `connector_identities._own_unipile_account_id` (sous
>    `resolve_operated_account_id`) : l'`account_id` du canal manquant dans l'org de
>    contexte est cherché dans la MÊME org perso que la clé.
> 3. **surface** — `oto_instance(op='list')` liste mes instances membre d'un connecteur
>    `personal_cross_org` posées ailleurs (`via='personal_cross_org'`), pinnables — sinon
>    « rien ne signale que j'ai déjà une instance perso ailleurs → je reconnecte → doublon ».
>
> **Garde-fou au connect (piste C)** : `unipile_connect.hosted_auth_url` refuse (409
> `unipile_already_connected_elsewhere`) si `sub` a déjà connecté CE canal dans une AUTRE
> org (2e `account_id` pour le même login = sessions hébergées qui se disputent le cookie
> `li_at` → dégradation) — sauf `force=True` (compte réellement distinct). Reconnexion
> **même org** = remplacement, non concernée. Threadé `unipile_connect_start(force=)` +
> `POST /api/unipile/connect {force}`.

> **Baileys archivé** (ex-WhatsApp self-hosted) : wrappers backend retirés
> (`tools/whatsapp.py` réécrit Unipile, `pairing.py` + routes `/api/whatsapp/pair/*`
> supprimés). L'engine Baileys survit dans **oto-core** (`oto/tools/whatsapp/` + Node)
> + la **CLI `oto whatsapp`** (fallback).

> **Mode plateforme unipile** (revente) : `auth_modes` inclut `platform` → la clé
> Unipile se partage en **clé plateforme + grant** (pas de copie par org) ;
> `access.unipile_api_key_for` a le fallback platform-grant. Le gate d'option reste
> par org (un grant donne la clé, ne débloque pas l'option). **Débloquer l'option
> = comp** : `db.set_option_comp("org", id, "unipile")` (débloque `access.has_option`).
> ⚠️ Les deux couches (clé=2, option=3) sont **orthogonales en base** mais l'**action
> admin les compose** (`capabilities/users_admin._set_option`) : `oto_admin_set_option`
> `on=true` sur un connecteur en mode plateforme **grant aussi la clé plateforme** (sinon
> `has_option`=true mais aucune clé → 404 au `/connect`, bouton « Connecter » inerte = état
> mort), `on=false` la révoque ; le champ `platform_key` du retour rend l'effet explicite
> (`granted`/`no_platform_key`/`byo_inert`/`revoked`). N'applique PAS à un connecteur keyed
> sans option (serpapi…) : lui se grant via la fiche admin (bouton « grant key » par
> provider, auto-résout la clé unique) ou `oto_admin_key_grant` (par `key_id`).

> **DSN par credential + sélecteur d'identité (ADR 0024).** Chaque clé Unipile est liée
> à SON sous-domaine `api<NN>.unipile.com:port` ; le DSN vit dans le `meta` du credential
> et voyage avec la clé via `resolve_credential` (défaut env `UNIPILE_DSN`=api25, instance
> plateforme). Une clé BYO porte N comptes → capacités génériques **`connectors.identities`/
> `set_default_identity`** (REST `/api/connectors/{c}/identities[/default]`, registre
> `connector_identities.py` ; unipile = `list_accounts` sur clé+DSN, **valide id∈liste**
> anti-binding, **BYO-only** — en revente la liste est vide, hosted-auth conservé). Vue admin
> **sièges clé plateforme** `GET /api/admin/unipile/seats` (super_admin, `db.unipile_account_owners`) :
> réconcilie les comptes de l'instance partagée ↔ leur owner oto (flag **orphelin**).

> **Compte partagé autorisé (otomata-private#55).** Le **propriétaire** d'un compte
> Unipile accorde à un **membre nommé** (d'une org commune, anti-IDOR `users_share_org`)
> le droit d'**opérer son compte** sur un canal — la **SEULE exception** au no-fallback
> anti-usurpation (#5). Table `connector_account_grants` (PK `(owner_sub, provider,
> grantee_sub)`, patron ADR 0025, `granted_by`/`granted_at` ; l'`account_id` stocké =
> snapshot d'audit, la résolution relit le handle **LIVE** → owner déconnecté = grant
> inerte). Le grantee bascule via le **sélecteur d'identité** (le compte accordé
> apparaît « compte de X » ; le select pose le **pointeur** `unipile_operated_accounts`,
> il n'écrase JAMAIS sa ligne `unipile_accounts`) ou un **pin projet** (garde étendue
> aux comptes accordés). Résolution : `connector_identities.resolve_operated_account_id`
> — pointeur **revalidé contre les grants vivants À CHAQUE appel** (révocation =
> effet immédiat) ; pointeur révoqué = **erreur explicite, jamais de repli** sur le
> compte propre. Capacité `capabilities/connectors_account_grants.py`
> (`oto_{list,grant,revoke}_account_*`, REST `/api/me/connector-accounts/*` ; autz
> `SUB_ONLY`, owner := ctx.sub par construction — pas d'escalade org_admin). ⚠️ La clé
> du grantee doit joindre le compte (clé partagée org/plateforme OK ; owner sur une clé
> BYO perso ≠ celle du grantee → 404 Unipile surfacé).

## API v1 / v2 (client sélectionnable, v1 par défaut)

Le client Unipile existe en **deux versions** dans oto-core
(`oto/tools/unipile/`) exposant la **même surface publique** (les wrappers
`tools/unipile.py` sont inchangés) — factory `make_unipile_client(api_version=…)` :

- **v1** (`client.UnipileClient`) — DSN par compte `apiXX.unipile.com:port/api/v1`,
  `account_id` en query, enveloppe `{items, cursor}`. **Défaut en prod.**
- **v2** (`client_v2.UnipileClientV2`) — API v2 Unipile : base `https://{dsn}/v2`,
  **`account_id` dans le path** (`/v2/{account_id}/…`), enveloppe `{data,
  total_count, next_cursor}` **normalisée** en `{items, cursor}` (aval inchangé),
  surface éclatée (search people/companies par produit, invitations =
  `users/me/relation-requests`, participants = ex-attendees, solde InMail =
  `inmail-credits`). **Beta Unipile** (nouveau compte + migration de données requis).

**Bascule** (`tools/unipile.unipile_client`) : `rc.config['api_version']` de la clé
résolue (la v2 impose un compte/clé dédiés → la version suit la clé), sinon env
`OTO_UNIPILE_API_VERSION` (bascule globale). Défaut `v1`.

**Fixes feedback intégrés au client v2** (la v2 seule ne les donne pas) : garde
**anti-mismatch** identifier↔réponse sur `get_profile`/`get_company` (rejette une
réponse qui ne correspond pas au membre/à la société demandé·e — bug de réponses
croisées #144-149/#153, retryable) ; erreurs réseau mappées proprement (#177) ;
**account_id caviardé** dans les messages d'erreur (#178). `react_message` exige le
`chat_id` en v2 (route sous le fil) — le wrapper ne le passe que s'il est fourni
(compat oto-core sans le kwarg).

> ⚠️ **Déploiement** : le client v2 vit dans oto-core (pin `pyproject`). Shipper la
> bascule v2 = tagger oto-core + bumper le pin ; tant que le pin n'est pas bumpé,
> seul le chemin v1 (défaut) tourne — donc merge sans risque prod.

## Deux formes d'endpoint de messagerie — inbox vs plate (13/08)

⚠️ **La même opération a DEUX routes chez Unipile, et l'amont répond 501 à la
mauvaise, dans les deux sens.** Le guide de migration messaging v2 le dit tel quel :
« Use `GET /v2/:account_id/chats` **or** `GET /v2/:account_id/inboxes/:inbox_id/chats`
**if the provider uses inboxes** » (idem pour ouvrir un fil : `chats/send` vs
`inboxes/{inbox}/chats/send`). LinkedIn range par **inbox** (`CLASSIC_PRIMARY`…) ; les
cinq autres canaux (`whatsapp`, `telegram`, `instagram`, `messenger`, `twitter`) non.

**Le mode de panne vécu** : quand LinkedIn est passé aux inbox (delta live 2026-07-06,
puis `chats/send` le 08), le client oto-core ne servait QUE LinkedIn — la bascule a
donc été faite en dur. Or ce client est channel-agnostic et partagé par
`register_messaging_tools` : les cinq canaux sans inbox se sont mis à taper la route
inbox, d'où un **501 sur `{canal}_chat(op="list")`** et sur l'ouverture d'un nouveau
fil, avec un compte pourtant connecté. Signalé par un utilisateur sur WhatsApp.
Répondre dans un fil existant et lire ses messages n'ont, eux, qu'une route (pas
d'inbox dans le path) : ils n'ont jamais été touchés — le symptôme est bien la LISTE.

**Ce qui tient maintenant** (oto-core ≥ v1.81.0) : la route est **dérivée du provider
du compte opéré** (`_INBOX_PROVIDERS`, provider non déclaré = LinkedIn), et un 501 fait
basculer sur l'autre route **en le journalisant** — un 501 d'Unipile n'est pas une
panne, c'est l'amont qui NOMME la forme attendue. Donc un reclassement futur (dans un
sens comme dans l'autre) se lit dans les logs au lieu de couper un canal en silence.
Côté backend, `unipile_client(provider)` doit **passer le canal** à
`make_unipile_client` (test d'ancrage `test_channel_reaches_the_client`) : il le
connaissait déjà pour résoudre le compte, mais s'arrêtait là.

**La leçon transposable** : un correctif de route relevé sur UN canal ne se code pas en
dur dans un client partagé par six. Ce qui vaut pour LinkedIn seul est une propriété du
**provider**, pas du client.

## Détail accumulé (migré de la carte)

**Instance PERSONNELLE cross-org (#172, amende ADR 0033).** Un compte de messagerie
hébergé est **par-personne** → flag registre `Connector.personal_cross_org=True`
(unipile). La clé membre d'un `sub` posée dans UNE org **le suit dans toutes ses orgs**
(résolution de proximité, pas seulement le pin `instance=` d'ADR 0038) : seam unique
déterministe `access.personal_instance_org` (org perso > plus récente) partagé par la
clé (`_resolve_credential_impl`, retombe cross-org AVANT groupe/org/plateforme quand la
clé LOCALE manque — même sub = zéro usurpation), le miroir de statut (`credential_mode_for`/
`unipile_api_key_for`) ET le compte (`connector_identities._own_unipile_account_id`, MÊME
org que la clé → appariés). Surfacé pinnable par `oto_instance(op='list')`
(`via='personal_cross_org'`). **Garde-fou** : `unipile_connect_start`/`POST /api/unipile/connect`
refusent (409 `unipile_already_connected_elsewhere`, override `force=true`) une 2e
connexion du même canal déjà lié dans une AUTRE org (anti-doublon `account_id`).

**Siège plateforme cross-org (#221, 2026-07-16, LIVE PROD).** Le cross-org #172 ci-dessus
était accroché à la **clé MEMBRE** (`personal_instance_org` → `list_member_orgs_for`) →
un user sur la **clé PLATEFORME partagée** (donc SANS clé membre) tombait à travers : son
siège hébergé, pourtant par-personne, ne le suivait QUE dans les orgs où une ligne
`unipile_accounts` existait → « je connecte mais ça reste sur Connect » ailleurs. Fix :
le **siège plateforme suit le sub cross-org** — `db.any_unipile_account_id(sub, provider)`
(siège `platform_seat=True` le plus récent, toutes orgs) est un fallback dans
`_own_unipile_account_id` (résolution outils) ET `status_for` (affichage), **gardé sur
`credential_mode_for == 'platform'` ET `subscribed`** (l'org de contexte résout la clé
plateforme ET a l'option) → jamais un siège sous une clé BYO (mismatch) ni un faux
« connecté » sans option. ⚠️ **`db.list_unipile_accounts` doit renvoyer `platform_seat`**
(le SELECT l'omettait → filtre cross-org muet ; bug masqué par les stubs unitaires,
attrapé en test empirique — cf. la leçon « stubs cachent la forme de row » ci-dessus).

**API v2 = seul chemin (V1 COUPÉ, 2026-07-16, LIVE PROD).** La migration des comptes
en v2 étant bouclée, **v1 est retiré du code** : oto-core **≥v1.26.0** n'a plus qu'**une
classe `UnipileClient` (v2)** (`client_v2.py` fusionné dans `client.py` ; `UnipileClientV2`
+ `make_unipile_client(api_version=)` supprimés) ; `DEFAULT_DSN = api.unipile.com` (gateway
v2 unifié). **Plus AUCUN plumbing `api_version`** côté backend (construction client, pose de
clé member/org/plateforme, carte `status_for`) ni de sélecteur dashboard. Le `dsn` par-clé
(`meta.dsn`) reste lu (défaut = gateway v2). Deltas API v2 (account_id-in-path, enveloppe
`{data,next_cursor}` normalisée `items`/`cursor`, inbox model, posts keyés URN, `inmail-credits`)
= **docstrings de `client.py`** oto-core.

**Hosted-auth v2 : webhook non livré → réconciliation poll-and-bind.** Le hosted-auth v2 ne
rappelle **pas** notre `notify_url` (webhook au niveau APP Unipile, pas par-lien) et le compte
connecté **ne porte pas notre nonce** → le compte se crée chez Unipile mais n'est jamais
enregistré côté oto (pending qui traîne). Fix **webhook-indépendant** : `unipile_connect.reconcile_pending(sub)`
liste les comptes Unipile et lie au sub le plus **récent, non déjà lié, du bon provider, créé
APRÈS son pending** (floor = anti-rebind d'un siège tiers). **Self-heal** dans `GET /api/me/unipile`
(no-op sans pending, donc sans appel Unipile) + endpoint explicite `POST /api/me/unipile/reconcile`.
Le webhook `POST /api/unipile/webhook` (handler v1 `CREATION_SUCCESS`) reste mais **dormant** (le v2
ne l'alimente pas) — utile seulement si on branche `account.disconnected` (détection de déco, non fait).

**Consolidation « tout en clé plateforme » (2026-07-16).** Clé plateforme rotée en v2 (scope
PLATFORM, label `env`) ; tous les BYO unipile supprimés ; **option comp** posée pour les orgs
concernées (`db.set_option_comp("org",id,"unipile")`). ⚠️ **GOTCHA share (ADR 0044 §F)** :
`share_mode='open'` n'ouvre à tous que si **`share_down` est VIDE** (`_platform_instance_usable` :
`(not down) or granted`) — sinon seule l'allowlist passe (sinon `404 unipile_not_configured`, la
clé plateforme ne résout pas). Free-tier réel = `open` + `share_down=[]`, l'option couche 3 gardant
qui peut connecter.

**Couche 3 « option » = source unique `access.option_open(sub, connector, org, group)` (2026-07-07).**
« L'option payante est-elle levée ? » était recopiée à 3 endroits (`connectors_selection.option_ok`
+ `unipile.status_for.subscribed` self & admin) → divergence (le **BYO ouvre l'option** — l'user
gère sa propre instance — était oublié dans un seul) → carte incohérente « clé d'org (vert) +
Bloqué (rouge) ». Règle : pas d'option ⟹ ouvert ; sinon **BYO** OU `has_option` (comp/abonnement).
Le **front est backend-driven** (rend `option_ok`/`subscribed`, 0 RBAC recodée client) → il devient
durable car il lit un flag cohérent. **Ne jamais recoder une règle d'accès côté front** : ajouter
un flag backend. Le gate DUR (qui peut utiliser) reste `require_connector_access` (ADR 0025, couvre
le BYO — « pas de clé perso qui contourne ») ; il gate aussi la **pose** (`api_key_save` → 403).

**Le feed est servi en VUE DE TRI (#384, 2026-08-11).** `linkedin_unipile_post(op="feed",
limit=40)` rendait **65-67 Ko**, au-delà du plafond d'un résultat MCP : sur la procédure
`veille-linkedin`, le harnais a déversé la sortie dans un fichier et l'agent a repassé au
`jq` pour la ramener à 42 Ko — deux tours et un détour par le shell avant le vrai travail ;
un client MCP **sans shell** (agent n8n) n'a lui aucun recours et cale sur l'appel. Mesure
sur 40 lignes réelles du miroir : **1 647 caractères par post**, dont 60 % de `text`, ~10 %
d'identifiant répété trois fois (`_id` == `urn` == la queue de `post_url`) et le reste en
comptabilité de miroir. Le défaut coupe donc le texte à **600 caractères** (coupe MARQUÉE
`text_truncated`) et ne rend que les colonnes qui servent à trier → **1 019 car./post**
(65 899 → 40 765 sur la même page ; le plafond passe de ~30 à ~49 posts).
- **Rien ne sort du catalogue** : le miroir garde toutes ses colonnes (`data_rows`),
  `fields=["*"]` les rend à l'octet près, `text_max_chars=None` rend le texte entier, et
  la réponse porte un bloc `projection` qui NOMME les colonnes écartées + le chemin vers
  le brut. Un défaut qui résume doit dire ce qu'il a rogné, sinon il cache.
- `fields` a **exactement la sémantique de `data_rows`** (projection + `_id`/`urn`
  toujours gardés pour adresser la ligne, colonne inconnue signalée sans bloquer) — une
  seule chose à apprendre. `fields=[]` est refusé (l'avaler rendrait plus que le défaut).
- Même extrait par défaut sur `linkedin_unipile_profile(op="posts"/"comments")`, **même
  seam** (`_slim`) : #281 y avait ajouté `fields`/`text_max_chars` sans corriger le
  DÉFAUT, et le même incident s'est rejoué sur le feed. ADR 0047 §Amendement du 11/08 :
  *le défaut est un acte de conception, le chemin paresseux doit être le chemin juste* —
  un paramètre optionnel de plus ne traite pas le signal.
- ⚠️ **Tronquer le texte SEUL ne suffisait pas** (0,76 de la page brute, encore ~46 Ko) :
  c'est la conjonction extrait + projection qui fait tomber le coût par post.
