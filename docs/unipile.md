---
title: Unipile — le compte, et ses six connexions
type: reference
---

# Unipile — le compte, et ses six connexions

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.

## Le split du 2026-08-28 — six connexions à côté du compte

**`unipile` est le code de production, à une ligne près.** Il garde sa clé, son
hosted-auth, son flux multi-canal, son label, ses modules. La seule chose que le
split lui retire, ce sont ses six namespaces de canal — parce qu'un namespace
n'appartient qu'à UN connecteur, et que chaque canal en devient un :

| connecteur | namespace | canal | label | module `tools/` |
|---|---|---|---|---|
| `unipile` | `unipile` | — (le compte, et un flux « connecter un canal ») | Messagerie hébergée (Unipile) | `unipile` |
| `linkedin_unipile` | `linkedin_unipile` | `LINKEDIN` | LinkedIn | `unipile` |
| `whatsapp` | `whatsapp` | `WHATSAPP` | WhatsApp | `whatsapp` |
| `telegram` | `telegram` | `TELEGRAM` | Telegram | `telegram` |
| `instagram` | `instagram` | `INSTAGRAM` | Instagram | `instagram` |

> **X et Messenger retirés (2026-09-15).** L'API Unipile v2 ne sert ni `TWITTER` ni
> `MESSENGER` : ils sont absents de l'enum `providers` de createAuthLink v2 (la v1 les
> accepte, mais c'est une autre instance Unipile, autre clé et autres comptes). Leurs
> cartes écrivaient un pending puis échouaient au lien ; aucun siège n'a jamais existé
> sur la clé plateforme. Connecteurs, outils (`twitter_chat`, `messenger_chat`), flux et
> guides partent ; les lignes de base (activation, sélection) restent, inertes, comme au
> retrait de `checkcrm`. Les sections plus bas qui les nomment sont l'histoire du split.

**Pourquoi.** Un connecteur est l'unité de gouvernance : activation, ACL d'org,
sélection de membre, visibilité des tools, carte. Avec sept namespaces sous un seul
nom, ces cinq choses étaient indivisibles — réserver WhatsApp à un département sans
ouvrir LinkedIn était impossible, et installer WhatsApp montait 40 outils LinkedIn.

**Les noms de tools ne bougent pas** (au split ; l'envoi en est sorti depuis, #1189,
§Lecture et envoi séparés, puis les messageries sont devenues des paires le 21/10/2026,
§Une paire par messagerie). `linkedin_unipile_*`, `whatsapp`… sont un
CONTRAT consommé hors dépôt (procédures en base de plusieurs orgs, guides plateforme
servis depuis la DB, un métrage d'usage). Ce qui change, c'est la CARTE : elle
porte le nom du réseau (« LinkedIn », « WhatsApp »), renvoie à sa marque, et ne
nomme pas le fournisseur — ce qu'on connecte, c'est son compte LinkedIn. Unipile
reste nommé sur la carte `unipile`, qui EST le compte.

**La ligne de partage, et c'est la seule chose à retenir :**

| la question | la réponse | le nom à employer |
|---|---|---|
| qui a le DROIT d'appeler ? | activation, ACL, sélection, visibilité, pin `_instance=` | le nom **NU** (`whatsapp`) |
| avec quelle CLÉ ? | coffre, cascade, quota, clé plateforme, option | `providers.credential_provider(nom)` → `unipile` |

Le seam est `Connector.credential_of` ; la normalisation vit **dans `walk_cascade` et
nulle part ailleurs** — résolution, miroir de mode et statut le traversent tous les
trois, donc ils ne peuvent pas répondre trois choses différentes. Normaliser dans
chacun rouvrirait la divergence du 2026-07-07 (carte « clé d'org » verte + « Bloqué »
rouge). Conséquences : un canal n'est pas dans `CREDENTIAL_PROVIDERS` (pose refusée,
le refus NOMME le porteur), n'est pas `org_shareable`, n'a pas de `secret_fields`, et
ne recopie **pas** `platform_key_open` (propriété de la clé — le recopier serait la
configuration morte qui a produit la panne all-users de #245). `personal_cross_org`,
lui, EST recopié : `call_axes` résout par namespace et c'est ce drapeau qui porte
l'axe `_account=`, donc les comptes accordés (#55).

**Deux façons de connecter un canal, à dessein** : le flux multi-canal du compte
(code de production) et le flux sans paramètre de chaque carte de canal — le canal y
est dérivé de `Connector.hosted_channel`, on ne peut pas démarrer une connexion
WhatsApp depuis la carte Telegram.

**Ce qui n'a PAS bougé** : les tables de comptes (`unipile_accounts`,
`connector_account_grants`, `unipile_operated_accounts`, `unipile_pending`) étaient
déjà keyées par CANAL (`provider` = `LINKEDIN`/`WHATSAPP`/…), jamais par connecteur.

**Ce qui a dû migrer** (boot, `db/_init.py` — des fail-* qui penchent dans deux
directions opposées et n'auraient rien levé) : `connector_availability` (pas de
ligne ⟹ **OFF** : la messagerie s'éteindrait pour tous), `connector_acl` (pas de
ligne ⟹ **OUVERT** : une restriction d'org s'évaporerait — fan-out retiré le 24/09/2026
avec la restriction elle-même, ADR 0053 D1), `user_selected_connectors`
(non-sélectionné ⟹ **MASQUÉ** : les membres perdraient la surface), plus
`orgs.default_connectors`. Fan-out 1→6 ; `unipile` SURVIT (c'est un split, pas un
renommage). Aucun des six noms n'a jamais été un connecteur, donc aucune ligne
fossile à purger. Tests contre un vrai PostgreSQL :
`tests/connectors/test_unipile_split_fanout.py`.

⚠️ **Ce paragraphe a dit « idempotent, donc rejouable à chaque boot » du 2026-08-28
au 2026-08-29, et c'était FAUX** (corrigé par #543). Le `ON CONFLICT DO NOTHING` ne
protège que les lignes **présentes**, or désélectionner un connecteur **supprime** la
sienne (`unselect` est un `DELETE`) : la garde ne couvrait donc pas le seul cas où
elle comptait. Pendant ces vingt-quatre heures, un canal retiré revenait actif au
redémarrage suivant — avec ses cinq voisins — et de même pour une disponibilité
plateforme éteinte à la main ou une ACL d'org effacée. **Un fan-out de split est un
déménagement, vrai UNE fois** : ce qui doit rester rejouable, c'est le boot, pas
l'écriture. Les quatre gestes sont désormais sous sentinelle
(`connectors.selection.split_fanout_pending`, marqueur `#unipile-split-fanout` dans
`connector_selection_seeded`, même forme que le backfill ADR 0050). La sonde traite
le cas des bases qui ont DÉJÀ reçu le déménagement : une base portant une sélection
sur l'un des six canaux est marquée **sans réécriture**, une base neuve le reçoit
normalement. Angle mort assumé : une base migrée dont plus aucune sélection ne porte
l'un des six canaux se relit « pas encore migrée » et rejouerait une dernière fois
(vérifié le 2026-08-29 : ne concerne pas la prod, les six y sont sélectionnés).

---

Tools `whatsapp` / `telegram` / `instagram` (`op=list|read`, `LECTURE` — fils 1-à-1 ET
de groupe, `is_group`) et `whatsapp_act` / `telegram_act` / `instagram_act` (`op=send`,
`ACTE` — cf. §Une paire par messagerie ci-dessous) = messagerie **hébergée Unipile**, chacun sous **son** connecteur
(cf. le tableau ci-dessus). Générés par la factory
`tools/unipile.register_messaging_tools(mcp, channel)` — l'API `/chats` d'Unipile est
channel-agnostic ; chaque tool résout l'`account_id` du canal pour le user
(no-fallback, `tools/unipile.unipile_client(provider)`, qui résout le credential
**sous le connecteur du canal** pour que l'ACL de ce canal morde).

> **⚠️ La surface LinkedIn s'appelle `linkedin_*` depuis le 2026-08-10**
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
> `linkedin_*` tomberait sous le connecteur `linkedin` (AI Ark) : mauvais
> credential, mauvaise activation, mauvaise sélection. Le changement est additif (aucun
> autre namespace n'est multi-token) et verrouillé par
> `tests/test_linkedin.py::test_linkedin_namespace_resolves_to_unipile`.
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
>    mono-org / déjà keyé). `credential_mode_for` et la résolution du connect **miroitent**
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
> `access.resolve_credential` porte aussi ce palier au connect. Le gate d'option reste
> par org (un grant donne la clé, ne débloque pas l'option). **Débloquer l'option
> = un droit déclaré `unipile`**, posé par le service de facturation (oto-commerce) seul
> (`docs/droits-declares.md`) : depuis la coupure du cœur (#1097), `oto_admin_set_option`
> et `platform.connector.access_set` refusent `unipile` en 409 `billing_moved`, et la clé
> de plateforme se partage seule par `platform.org.grant_key`.
> ⚠️ Pour une option HORS catalogue d'un connecteur en mode plateforme, les deux couches
> (clé=2, option=3) sont **orthogonales en base** mais l'**action
> admin les compose** (`capabilities/users_admin._set_option`) : `oto_admin_set_option`
> `on=true` sur un connecteur en mode plateforme **grant aussi la clé plateforme** (sinon
> `has_option`=true mais aucune clé → 404 au `/connect`, bouton « Connecter » inerte = état
> mort), `on=false` la révoque ; le champ `platform_key` du retour rend l'effet explicite
> (`granted`/`no_platform_key`/`byo_inert`/`revoked`). N'applique PAS à un connecteur keyed
> sans option (serpapi…) : lui se grant via la fiche admin (bouton « grant key » par
> provider, auto-résout la clé unique) ou `oto_admin_key_grant` (par `key_id`).

> **Une résolution au connect.** `hosted_auth_url` résout le canal demandé une seule
> fois, via `resolve_credential(check_usage=False)` : clé, mode BYO et DSN proviennent
> de la même instance. Équipe et tenant passent par la cascade commune ; les droits
> restent ceux du canal. Configurer un compte ne débite pas le budget tenant et ne
> vérifie pas le quota d'exécution. L'option et le plafond de sièges plateforme
> restent vérifiés. Un compte ambigu/refusé ne se transforme pas en « aucune clé ».
>
> **DSN par credential + sélecteur d'identité (ADR 0024).** Chaque clé Unipile est liée
> à SON sous-domaine `api<NN>.unipile.com:port` ; le DSN vit dans le `meta` du credential
> et voyage avec la clé via `resolve_credential` ; la clé plateforme prend le défaut du
> client oto-core (`api.unipile.com`, la gateway v2) — la lib ne lit aucun env, `UNIPILE_DSN`
> n'existe plus. Une clé BYO porte N comptes → capacités génériques **`connectors.identities`/
> `set_default_identity`** (REST `/api/connectors/{c}/identities[/default]`, registre
> `connectors/identities.py` ; unipile = `list_accounts` sur clé+DSN, **valide id∈liste**
> anti-binding, **BYO-only** — en revente la liste est vide, hosted-auth conservé). Vue admin
> **sièges clé plateforme** `GET /api/admin/unipile/seats` (super_admin, `db.unipile_account_owners`) :
> réconcilie les comptes de l'instance partagée ↔ leur owner oto (flag **orphelin**).
>
> **Plafond de sièges hébergés par org — RETIRÉ le 2026-10-23.** La connexion
> (`hosted_auth_url`) ne compte plus les comptes d'une org : aucune org n'est plafonnée,
> `unipile_account_limit_reached` n'est plus émis, et `plafond_de_comptes` rend `0`
> (sans plafond) à ses lecteurs (vue admin `effective_limit`, lecture du commerce). La colonne
> et le défaut ci-dessous restent lisibles et posables, mais **inertes**. Avant :
> `orgs.unipile_account_limit`, lu à la connexion : `NULL` = pas de plafond propre → défaut plateforme
> `OTO_MCP_UNIPILE_DEFAULT_LIMIT` (5 si absent) ; ⚠️ **`0` = sans plafond** (le test est
> `if limit and count >= limit`). Surface admin : `GET /api/admin/orgs/{id}/unipile-limit`
> (admin plateforme, vue `{org_id, limit, default_limit, effective_limit, accounts}`) et
> `PUT` même chemin, corps `{"limit": int | null}` (super admin ; `null` = retour au
> défaut, négatif = `400 invalid_body`). ⚠️ Le plafond ne gouverne que les connexions
> **neuves** — rien n'est déconnecté au-dessus. Souscrire un plan, ou retirer un plan
> offert, **ne touche pas** à la colonne (arbitrage du 23/09, oto-backend#805) : un plan
> sans nombre de sièges (`unipile_accounts=None`, tous les paliers aujourd'hui) n'a pas
> d'avis, ce n'est **pas** « illimité ». Jusque-là, la souscription écrivait `NULL`, relu
> comme le défaut (5) : un plafond posé à la main retombait à 5 au paiement.

> **Compte partagé autorisé (otomata-private#55).** Le **propriétaire** d'un compte
> Unipile accorde à un **user nommé, cross-org** (⚠️ corrigé le 2026-09-02 : ce
> paragraphe disait « d'une org commune, anti-IDOR `users_share_org` » — faux depuis
> l'origine, le code n'a jamais exigé de partager une org, `test_grant_allows_cross_org_grantee`
> le prouve ; le seul garde-fou est l'existence de l'user) le droit d'**opérer son
> compte** sur un canal — la **SEULE exception** au no-fallback anti-usurpation (#5).
> Table `connector_account_grants` (PK `(owner_sub, provider, grantee_sub)`, patron
> ADR 0025, `granted_by`/`granted_at` ; l'`account_id` stocké = snapshot d'audit, la
> résolution relit le handle **LIVE** → owner déconnecté = grant inerte).
>
> **Cible GROUPE (extension 2026-09-02)** : `grantee="group:<id>"` accorde à TOUS LES
> MEMBRES ACTUELS d'un groupe, fan-out **dynamique** — table séparée
> `connector_account_group_grants` (PK `(owner_sub, provider, grantee_group_id)`,
> jamais un `grantee_sub` nullable : Postgres interdit un NULL en PK). La résolution
> (`granted_accounts_for`/`list_account_grants_to`) rejoint `org_group_members` EN
> LIVE à chaque appel — quitter le groupe retire l'accès aussi immédiatement qu'une
> révocation explicite, sans rien à nettoyer côté grant. L'issue d'origine (#55)
> demandait déjà « membres nommés OU un département » ; le groupe n'avait jamais été
> livré (ADR 0051 avait laissé le grant de compte orthogonal au partage d'instance,
> sans jamais trancher SA cible — gap de plomberie, pas une décision de sécu : seul
> « qui peut accorder » [le propriétaire, jamais un org_admin] est une garantie
> délibérée à garder).
>
> Le grantee (nominatif ou de groupe) bascule via le **sélecteur d'identité** (le
> compte accordé apparaît « compte de X » ; le select pose le **pointeur**
> `unipile_operated_accounts`, il n'écrase JAMAIS sa ligne `unipile_accounts`) ou un
> **pin projet** (garde étendue aux comptes accordés). Résolution :
> `connector_identities.resolve_operated_account_id` — pointeur **revalidé contre les
> grants vivants À CHAQUE appel** (révocation = effet immédiat) ; pointeur révoqué =
> **erreur explicite, jamais de repli** sur le compte propre. Capacité
> `capabilities/connectors/account_grants.py` (`oto_{list,grant,revoke}_account_*`,
> REST `/api/me/connector-accounts/*` ; autz `SUB_ONLY`, owner := ctx.sub par
> construction — pas d'escalade org_admin, y compris pour une cible groupe). ⚠️ La clé
> du grantee doit joindre le compte (clé partagée org/plateforme OK ; owner sur une clé
> BYO perso ≠ celle du grantee → 404 Unipile surfacé).

## Lecture et envoi séparés (#1189, 09/10/2026)

**Un client MCP autorise des OUTILS, jamais des opérations.** Sur claude.ai ou ChatGPT,
« toujours autoriser » `whatsapp_chat` pour lire autorisait aussi l'envoi d'un message à
un tiers, sans confirmation — le seul garde-fou par `op` vivait dans le hook du plugin
Claude Code, qui ne protège aucun autre client. Décision d'Alexis le 09/10/2026 : tout ce
qui agit CHEZ un tiers ou EN SON NOM sort des lecteurs, dans un outil dédié, sur le
patron Gmail (`gmail` lit ou simule, `gmail_act` agit — depuis le 17/10/2026, une paire par connecteur).

| lecteur | ops de lecture | écrivain (`ENVOI`, `destructiveHint` + `openWorldHint`) | ops déplacées (dépréciées dans le lecteur jusqu'au 10/12/2026) |
|---|---|---|---|
| ~~`{whatsapp,telegram,instagram}_chat`~~ | list, read | ~~`{canal}_send`~~ | send (sans `op`) — retirés le 21/10/2026, §Une paire par messagerie |
| ~~`linkedin_unipile_chat`~~ | list, read, attendees, contacts | ~~`linkedin_unipile_send`~~ | send, update, react — retirés le 21/10/2026 |
| `linkedin_unipile_network` | relations, invitations | ~~`linkedin_unipile_invite`~~ | invite, handle, cancel — retirés le 21/10/2026, §Un seul acteur LinkedIn |
| `linkedin_unipile_post` | feed, get, engagement | ~~`linkedin_unipile_publish`~~ | create, comment, react — retirés le 21/10/2026 |

Le découpage suit une seule règle : **ce qui agit chez un tiers ou en son nom est un
outil d'écriture.** `react` (un message, une publication) se voit de l'autre côté ;
`handle` (accepter, refuser) et `cancel` (retirer) changent l'état d'une invitation que
l'autre membre voit ; `update` d'un fil peut montrer « lu » à l'interlocuteur
(`setReadStatus`) et, pour le reste, écrit — un lecteur déclaré `readOnlyHint` ne
peut porter aucune op qui écrit (`tools/lecture.py`, garde `tests/test_outils_en_lecture.py`).
Les ops gardaient leur nom dans l'écrivain ; depuis le 21/10/2026 elles portent leur
objet dans `linkedin_unipile_act` (§Un seul acteur LinkedIn).

### Transition (12/10/2026 → 21/10/2026) : terminée

La transition décidée le 12/10/2026 (l'ancienne op servie par l'écrivain jusqu'au
10/12/2026, avec `deprecation_warning`, le lecteur déclaré `ENVOI`) a pris fin le
21/10/2026, avant sa date : les messageries ont quitté la transition (§Une paire par
messagerie), puis la publication et le réseau LinkedIn (§Un seul acteur LinkedIn).
Plus aucun lecteur ne sert d'op d'envoi ; l'ancienne op refuse en nommant son nouvel appel
(`outils_retires.OPS_DEPLACEES`, lu par `error_taxonomy.classify`) — jamais un « Input
should be … » muet, jamais une exécution. Historique : `docs/alias-deprecies.md`.

Côté plugin Claude Code, le hook `guard-outbound.mjs` doit couvrir les outils `*_act`.

## Une paire par messagerie (21/10/2026, retrait IMMÉDIAT)

Un agent a envoyé un WhatsApp sans validation : `whatsapp_chat` était en « toujours
autoriser » dans un projet, et servait encore l'ancienne `op="send"` de la transition
ci-dessus. Tant qu'un lecteur peut envoyer, autoriser la lecture autorise l'envoi.
Décision d'Alexis, pour la sécurité : chaque messagerie sert la PAIRE du 17/10/2026
(`tools/lecture.py`, `docs/tool-visibility.md` §Lire ou simuler, agir) — `<ns>` lit
(`LECTURE`), `<ns>_act` agit (`ACTE` : le client le fait confirmer) — et ses anciennes
ops d'envoi disparaissent avec les anciens noms, sans attendre le 10/12/2026.

| ancien | nouveau |
|---|---|
| `{canal}_chat(op=list/read)` | `{canal}(op=list/read)` |
| `{canal}_chat(op=send)`, `{canal}_send` | `{canal}_act(op="send")`, mêmes arguments |
| `linkedin_unipile_chat(op=list/read/attendees/contacts)` | `linkedin_unipile(op=…)` |
| `linkedin_unipile_chat(op=send/update/react)`, `linkedin_unipile_send(op=…)` | `linkedin_unipile_act(op=…)` |

`{canal}` ∈ whatsapp, telegram, instagram. Les anciens noms refusent en nommant l'appel à
faire (`outils_retires.RENOMMAGES`) ; les listes d'outils enregistrées sont réécrites
par la révision `20261021_1219_outils_act_msg` : autoriser reste étroit, masquer reste large (décision d'Alexis du 21/10/2026) : une liste qui autorisait un ancien lecteur ne reçoit que le nouveau lecteur, jamais `<ns>_act` — même si l'ancien lecteur envoyait encore en transition ; un ancien outil d'envoi dédié ne donne `<ns>_act` que si celui-ci ne fait rien de plus que lui — sinon RIEN : `linkedin_unipile_send`, `_publish` et `_invite` ne donnent pas `linkedin_unipile_act`, qui publie, invite et envoie ; on l'ajoute explicitement, le refus de l'ancien nom le dit ; une liste qui masquait masque tout ce que l'ancien outil faisait.
Pour LinkedIn, `linkedin_unipile` ne lit que la messagerie ; les autres lecteurs du
connecteur (`_post`, `_network`, `_profile`…) gardent leur nom
(`tests/test_convention_act.py`, `A_MIGRER`), et tout ce qui agit est dans
`linkedin_unipile_act` (§Un seul acteur LinkedIn).

## Un seul acteur LinkedIn (21/10/2026, retrait IMMÉDIAT)

Même décision, même jour, pour ce qui agissait encore hors de la messagerie : la
publication, les invitations et les gestes sur un membre. Les lecteurs gardent leur nom
et ne font plus que lire (`LECTURE`) ; tout ce qui agit va dans `linkedin_unipile_act`, une
op par geste et par objet — `react_message` (un emoji sur un message) n'est pas
`react_post` (une réaction à une publication).

| ancien | nouveau |
|---|---|
| `linkedin_unipile_chat`/`_send(op=react)` | `linkedin_unipile_act(op="react_message")` |
| `linkedin_unipile_post(op=create/comment/react)`, `linkedin_unipile_publish(op=…)` | `linkedin_unipile_act(op="publish_post"/"comment_post"/"react_post")` |
| `linkedin_unipile_network(op=invite/cancel)`, `linkedin_unipile_invite(op=…)` | `linkedin_unipile_act(op="invite"/"cancel_invitation")` |
| `…(op=handle, action=accept/decline)` | `linkedin_unipile_act(op="accept_invitation"/"decline_invitation")` |
| `linkedin_unipile_profile(op=endorse/action)` | `linkedin_unipile_act(op="endorse"/"update_member")` |

Les outils d'envoi dédiés (`linkedin_unipile_publish`, `linkedin_unipile_invite`)
refusent en nommant l'appel à faire (`outils_retires.RENOMMAGES_ENVOIS`) ; une ancienne op
d'un lecteur qui reste refuse de même (`outils_retires.OPS_DEPLACEES`). Les listes
d'outils enregistrées sont réécrites par `20261021_1352_outils_act_envois`, même sens :
une liste qui autorise un lecteur mixte n'est pas touchée (il ne fait plus que lire),
un outil d'envoi dédié autorisé ne donne RIEN (`linkedin_unipile_act` fait plus que
publier ou inviter : l'autorisation se perd, on l'ajoute explicitement), une liste qui
masque masque aussi `linkedin_unipile_act`. Si le lot des messageries a été joué seul
avant, `linkedin_unipile_act` autorisé (pour la messagerie) se perd de même.

## « Mon LinkedIn est-il connecté ? » → `linkedin_unipile_account(op="status")`

Ajouté le 28/08/2026 (signal **#452**, 14/08). Le NOM du tool promettait l'état
du compte, l'outil ne servait que **l'ardoise premium** (contrats Recruiter / Sales
Navigator). Un agent venu vérifier la connexion a inventé `op='status'`, s'est pris un
`invalid_arguments` et en a conclu « pas connecté » — alors que le canal
l'était, et un utilisateur a signalé « ça ne marche pas ». Relevé du 28/08 : ce tool
n'a que **trois** appels dans tout `tool_calls`, et les trois ont échoué.

On a fait exister l'op plutôt que de renommer : le renommage aurait cassé les appels qui
marchent **et les procédures qui citent le nom** (refs `<tool:slug>` en DB, cf.
`docs/guides.md`). Deux propriétés non négociables, toutes deux tirées du mode de
panne :

- **Ça RÉPOND, ça ne lève pas.** Sans compte lié, `unipile_client()` lève (refus de
  fallback anti-usurpation) : bâtir le statut dessus aurait remplacé un faux négatif par
  une erreur. `op="status"` court-circuite donc AVANT `unipile_client()`.
- **Ça résout comme un vrai appel** — `connector_identities.resolve_operated_account_id`,
  exactement ce que `unipile_client()` emprunte (pin `_account=`, compte accordé #55,
  compte propre de l'org). « status dit connecté » implique donc « un appel trouvera un
  compte ». Un pointeur orphelin est **rapporté** au lieu d'être levé : c'est justement
  l'état qu'on vient lui demander.

⚠️ **`connected` ≠ `alive`.** Un compte reste LIÉ en base alors que sa session est morte
côté fournisseur (checkpoint, cookie tourné — #236), et c'est précisément l'état où une
carte verte trompe le plus. Trois valeurs, trois faits distincts : `alive=True` (sonde
`users/me` OK), `alive=False` (session morte, tout appel échouera), `alive=None`
(**sonde indisponible** — pas « morte » : annoncer une panne jamais constatée serait le
même défaut à l'envers). Quand rien n'est lié, `next_step` vient du seam PARTAGÉ
`connectors/readiness.py`, pas d'une prose locale — même réponse que la carte connecteur.

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
la résolution du connect) ET le compte (`connector_identities._own_unipile_account_id`, MÊME
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

**Hosted-auth v2 : pas de callback par lien → réconciliation poll-and-bind, LE chemin de
liaison.** Le hosted-auth v2 ne rappelle **aucun** `notify_url` (le webhook v2 est au niveau
APP Unipile, pas par-lien) et le compte connecté **ne porte pas notre nonce** → rien à corréler
au retour. Le chemin : `unipile_connect.reconcile_pending(sub)` liste les comptes Unipile
**non déjà liés, du bon provider, créés APRÈS son pending** (floor = anti-rebind d'un siège
tiers). **Self-heal** dans `GET /api/me/unipile` (no-op sans pending, donc sans appel
Unipile) + endpoint explicite `POST /api/me/unipile/reconcile`.

⚠️ **La réconciliation ne CHOISIT plus (oto#247).** Elle prenait « le plus récent vivant » :
sur une clé PARTAGÉE, deux personnes qui connectent dans la même fenêtre rendent leurs deux
comptes candidats pour chacune, et un compte de messagerie a été rattaché à la mauvaise
personne. Un compte n'est désormais lié que s'il est identifié SANS ambiguïté :
- **l'`account_id`** qu'Unipile ajoute à l'adresse de retour — repassé par le front
  (`POST /api/me/unipile/reconcile`) ou par l'agent (`linkedin_unipile_account(op="status",
  account_id=…)`) ; il restreint à ce compte, sans lever aucune garde ;
- **une ligne morte du sub** (preuve de propriété, reconnexion qui réutilise le compte) —
  seule candidate vivante ;
- **un candidat vivant UNIQUE, sans preuve**, seulement si aucune demande d'un AUTRE sub
  (même canal, même population de clé — plateforme ou BYO) n'attend dans une fenêtre qui le
  couvre. Le nom du lien (notre nonce) ne revient pas dans `/accounts` : la seule chose
  vérifiable sans retour navigateur est l'absence de concurrent, que nos pendings disent.

Tout le reste — plusieurs vivants, ou un seul qu'un autre pourrait réclamer — rend
`reason: ambiguous_candidates`, n'écrit rien, journalise les candidats et **laisse le
pending** : l'appel suivant porteur de l'`account_id` liera. Deux connexions simultanées
sans indice restent donc refusées toutes deux jusqu'à ce que l'une passe l'`account_id`
(ou que les demandes expirent, 1 h) — c'est le prix de ne plus deviner.

⚠️ **Une réconciliation qui ne lie rien DIT pourquoi (03/09/2026, signal #689).** Elle
avait six sorties, toutes rendant le même `{bound: false, accounts: []}` : pas de pending,
pas de credential résoluble, fournisseur injoignable, aucun candidat éligible, candidats
tous morts (session 401), écriture refusée à sa garde. Un utilisateur a suivi le parcours
hosted-auth **deux fois**, la seconde jusqu'à la redirection finale, attendu plusieurs
minutes — et lu `connected:false`, sans un mot. Les trois causes d'un « aucun candidat »
sont d'ailleurs indiscernables sans être nommées : compte jamais créé (parcours
abandonné), compte **antérieur** au pending (donc sous le floor), ou compte appartenant à
quelqu'un d'autre.

La réponse porte désormais `reason` + `detail` quand rien n'a été lié, et `pendings` (le
détail par nonce, pour que deux demandes en attente ne se confondent pas). Rien n'est
ajouté quand la liaison réussit : pas d'écart, pas de bruit. C'est la règle que ce
module écrit déjà en tête, pour `BindOutcome` — *« un refus muet est un refus que personne
ne saura avoir eu »* — et que `reconcile_pending`, sa voisine immédiate, n'appliquait pas.

⚠️ **Ce que ça ne fait PAS** : nommer la cause ne la corrige pas. Si un parcours se termine
côté fournisseur sans produire de compte éligible, le défaut reste entier — on saura
seulement lequel des trois il est, en une lecture au lieu de deux tentatives.

⚠️ **Le webhook de liaison `POST /api/unipile/webhook` a été RETIRÉ le 2026-08-29 (#581) :
dormant depuis la v2 du fournisseur.** Le champ `notify_url` du hosted-auth v1 n'existe plus
en v2, le callback n'est plus rappelé — **zéro appel** sur le mois de journal REST retenu
(2026-07-29 → 2026-08-29, 503 960 requêtes `/api/*`, toutes formes de route confondues). Une
route non authentifiée sans appelant légitime n'a qu'un appelant possible — quelqu'un qui la
vise — et une surface sans appelant se retire, elle ne se garde pas. Le lien hosted-auth ne
porte plus de `notify_url` (en envoyer un ferait pointer le fournisseur sur un 404 et
laisserait croire qu'un webhook existe). Le pending (`unipile_pending`, nonce = `name` du
lien) reste : c'est la moitié oto de la connexion, que la réconciliation consomme. **Si le
webhook d'application v2 (signé HMAC-SHA256, `unipile-signature: t=…,v0=…` — `account.add`,
`account.disconnected`) est branché un jour, ce sera une route DIFFÉRENTE, vérifiée par
signature**, et elle écrira par `bind_account` comme la réconciliation.

⚠️ **Une liaison de compte se garde au point d'ÉCRITURE (#559, corrigé le 2026-08-29).**
Le webhook — alors encore monté — confrontait le **nonce** et rien d'autre : `account_id`
était repris du corps tel quel. Le nonce prouve « c'est bien la session de connexion de cette
personne » ; il ne dit rien de « c'est bien le compte qui vient d'être créé ». Or la clé
Unipile de la plateforme **adresse tout l'abonnement, toutes orgs confondues** — n'importe
quel siège y était donc nommable. Le chemin jumeau, lui, contrôlait
(`bound_unipile_account_ids`, « jamais le siège d'un tiers ») : ce n'était pas une garde
jugée inutile, c'était une garde **qui n'a pas suivi quand le second chemin est apparu**.
Depuis, la règle vit dans **une seule fonction** — `unipile_binding.account_claimable` (un
identifiant attribué à quelqu'un d'AUTRE, ligne vivante ou morte, n'est pas réclamable) — et
l'écriture passe par `unipile_binding.bind_account`. Le webhook est parti (#581), **la garde
reste** : un prochain chemin d'écriture naît gardé au lieu de devoir s'en souvenir. Le
cliquet est `tests/test_unipile_bind_guard.py` : il rejoue l'attaque contre un vrai
PostgreSQL, tient par AST les listes fermées des écrivains de liaison (écritures directes ET
appelants de la garde — cette seconde liste a perdu l'écrivain webhook), et vérifie que la
route rend 404.

**Pourquoi le webhook v1 ne pouvait PAS être authentifié par signature** — vérifié dans la
doc fournisseur le 2026-08-29. Le callback `notify_url` du hosted-auth **v1 n'est pas signé**
et n'accepte aucun en-tête ; en **v2** le champ `notify_url` n'existe même plus (corrélation
par `state` au `redirect_uri`, ou par l'événement applicatif `account.add`). La signature
HMAC-SHA256 (`unipile-signature: t=…,v0=…`) n'existe que sur les **webhooks d'application
v2**, que nous n'utilisons pas. C'est ce qui a tranché le retrait plutôt que le durcissement :
il n'y avait rien de plus à vérifier sur ce chemin, et plus personne pour l'appeler.

⚠️ **Un siège orphelin ne se lie pas non plus (#580, 2026-09-30).** Un compte présent sur
l'abonnement partagé et lié à **personne** chez nous n'est pas libre pour autant : le compte
créé par le fournisseur ne porte pas notre nonce, et sans preuve, le premier qui le nomme
(l'indice `account_id` du retour se forge) le prenait. La garde exige donc une **provenance**
(`unipile_binding.Provenance`) : une ligne du réclamant (la reconnexion réutilise
l'identifiant), ou une **création chez le fournisseur postérieure à la demande de liaison**
(marge d'horloge de 5 min). Ce plancher de date vivait dans la seule réconciliation et y
cédait quand une date était illisible (« date illisible → on garde ») ; il vit au point
d'écriture, et **une date absente ou illisible refuse**. ⚠️ La lecture de la date est donc
sur le chemin de TOUTE connexion : le fournisseur la sert en ISO 8601 avec `Z` final, que
`datetime.fromisoformat` refuse avant Python 3.11. L'ancienne lecture rendait None en 3.10
(les 26 liaisons de l'audit du 30/09 : « date illisible → on garde » laissait donc TOUT
passer) ; `_parse_dt` lit le `Z`, une fraction de 1 à 9 chiffres et l'horodatage Unix, et
un test la tient sur ces formes. L'inventaire vient de
`list_accounts`, déjà lu par la réconciliation : la lecture d'un compte par identifiant
n'était pas nécessaire. **Ce qui restait** — un compte qu'un TIERS crée sur la clé partagée
APRÈS votre demande et avant sa propre réconciliation passait le plancher — est fermé par
oto#247 : sans l'indice `account_id`, la réconciliation refuse dès qu'une autre demande
attend dans la même fenêtre (`ambiguous_candidates`, plus haut).

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
un flag backend. Qui peut utiliser = à qui la clé se résout (ADR 0053 D1 : la clé posée au bon
niveau ; la restriction par ACL, ADR 0025, a disparu le 24/09/2026).

**Le feed est servi EN DIRECT, sans miroir (oto#156, 2026-10-05).** `op="feed"` lit une page
Voyager par appel (`client.get_feed`, sous `_scrape` comme toute lecture LinkedIn), la trie
par `posted_at` en mémoire et rend `{items, cursor, count}` ; la suite se demande par le
`cursor` rendu. Rien n'est écrit : le miroir `linkedin-feed` du datastore (sync à TTL
600 s, `unipile_accounts.feed_synced_at`, retirée par la révision `0039`) se resynchronisait par `upsert_row`, qui
REMPLAÇAIT la ligne entière — toute annotation posée entre deux syncs tombait sans trace.
Décision d'Alexis du 05/10 : plus de miroir ni d'annotation (`signal`, `signal_type`,
`is_target`, `traite_le` abandonnées), `upsert_row` retiré avec lui. Le TTL ne protégeait
aucun quota : il bornait la fraîcheur d'un sync de 5 pages ; en direct, un appel = une
requête, comme `op="get"`. Les tableaux `linkedin-feed` existants restent des tableaux
ordinaires, plus jamais synchronisés.

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
- **Rien ne sort du catalogue** : `fields=["*"]` rend tous les champs à l'octet près,
  `text_max_chars=None` rend le texte entier, et la réponse porte un bloc `projection` qui NOMME les colonnes écartées + le chemin vers
  le brut. Un défaut qui résume doit dire ce qu'il a rogné, sinon il cache.
- `fields` projette (l'`urn` toujours gardé pour adresser le post, champ inconnu signalé
  sans bloquer). `fields=[]` est refusé (l'avaler rendrait plus que le défaut).
- Même extrait par défaut sur `linkedin_unipile_profile(op="posts"/"comments")`, **même
  seam** (`_slim`) : #281 y avait ajouté `fields`/`text_max_chars` sans corriger le
  DÉFAUT, et le même incident s'est rejoué sur le feed. ADR 0047 §Amendement du 11/08 :
  *le défaut est un acte de conception, le chemin paresseux doit être le chemin juste* —
  un paramètre optionnel de plus ne traite pas le signal.
- ⚠️ **Tronquer le texte SEUL ne suffisait pas** (0,76 de la page brute, encore ~46 Ko) :
  c'est la conjonction extrait + projection qui fait tomber le coût par post.

## Lots 2-3 (10/08, #279) — les 5 autres canaux, et `linkedin` déposé au profit d'`aiark`

> ⚠️ **Section HISTORIQUE — les noms de tools qu'elle donne sont périmés depuis le
> 2026-08-28.** Elle décrit la période où l'on croyait que DEUX fournisseurs se
> disputaient la capacité « LinkedIn », d'où les suffixes `linkedin_unipile_*` /
> `linkedin_aiark_*`. La prémisse était fausse : AI Ark vend de la donnée, la
> session EST LinkedIn. Les tools sont aujourd'hui `aiark_*` et `linkedin_*` (cf.
> §Le split plus haut). Ce qui suit reste utile pour comprendre POURQUOI les
> suffixes ont existé — pas pour savoir comment les tools s'appellent.

> **Lots 2-3 (10/08, même issue)** : les 5 autres canaux passent à `{whatsapp,telegram,
> instagram,messenger,twitter}_chat(op=list|read|send)` — 15 → 5, factory commune, le canal
> reste dans le NOM (trouvabilité) ; et **le connecteur `linkedin` est DÉPOSÉ** au profit
> d'`aiark`, dont les tools deviennent `linkedin_aiark_*` (6 → 3 : `search` op=people|companies,
> `person` op=export|reverse|mobile, `credits`). Les deux connecteurs étaient le même vendeur
> et le même client `AiArkClient`, ne différant que par le mode d'auth = une distinction
> d'INSTANCE (ADR 0038/0044 §F), qui coûtait de poser deux fois la même clé pour un seul pool
> de crédits (ADR 0024). Rien à migrer au coffre : aucun grant n'y était posé, ses 5 tools
> étaient **montés et inopérants** depuis leur mise en service. `linkedin_aiark_credits` REFUSE
> en mode plateforme (le solde du pool oto n'est pas celui de l'appelant). Domaine complet :
> **62 → 17 tools** ; catalogue **665 → 619**.
> ⚠️ **Reste à faire au tag prod** : migrer `user_selected_connectors` (119 lignes `linkedin`
> → `aiark`, dédoublonnées) — la DB est partagée preprod/prod, la migrer avant le tag
> retirerait le connecteur de 119 toolbox encore servies par l'ancien code.

### Org hébergée par un tenant tiers

Aucun plan d'oto ne porte aujourd'hui de nombre de sièges : ni la souscription ni le retrait n'écrivent le plafond (§ Plafond de sièges hébergés par org). Si un palier en portait un, il **n'écrirait pas** le plafond d'une org hébergée par un tenant tiers : ce plafond appartient à la facturation du partenaire, qui le pose via `PUT /api/admin/orgs/{id}/unipile-limit` (`billing._hosted_by_partner`). Une lecture de tenant qui échoue retombe sur le chemin des clientes directes : le plan écrit.

## Fin du droit `unipile` : cesser de payer les comptes (#806)

Décision du 23/09/2026. Dès que l'org perd le droit `unipile` (fin d'essai, résiliation,
don échu), ses canaux cessent de marcher (relecture du droit à l'usage). Le propriétaire de
chaque compte est prévenu, et **sept jours plus tard** (réglable), si le droit n'est pas
revenu, ses comptes branchés sur la **clé plateforme** sont supprimés chez unipile. Un
compte sur la propre clé du client (`platform_seat = false`) n'est jamais regardé.

- **Le travail** : `oto-mcp maintenance unipile-fin-de-droit` (`oto_mcp/unipile_fin_de_droit.py`),
  dans le timer quotidien, prod seulement. Il relit l'état à chaque passage, sans
  dépendre d'aucun événement. Il efface les marques des orgs qui ont le droit, marque
  `unipile_accounts.entitlement_lost_at` au premier constat de la perte, envoie **un**
  préavis par (propriétaire, org) et pose `entitlement_notice_at` après l'envoi. Puis il
  supprime, par le geste de `release` (`capabilities/unipile_seats.liberer` : délier,
  puis supprimer), les comptes que l'instance liste encore, dont aucun binding en service
  n'a le droit, préavis fait et délai échu. Chaque suppression est journalisée. Rejouer
  ne double ni un préavis ni une suppression.
- **Le droit** est celui du **titulaire** de chaque binding dans son org,
  `access.has_right(sub, org, "unipile")`, donc `org_entitlements` : le droit de l'org, ou
  une ligne posée sur la personne (dans l'org ou partout, `docs/droits-declares.md`).
  Le retour du droit efface les marques binding par binding. Un compte adopté dans deux
  orgs reste dû tant qu'un de ses bindings a le droit. L'inventaire (`entitled`) et la
  garde de reprise sans `force` lisent le même droit du titulaire, jamais celui de
  l'admin qui appelle.
- ⚠️ **Fermé par défaut** : sans `OTO_UNIPILE_FIN_DE_DROIT=1` dans le `.env` de prod, le
  passage compte ce qu'il ferait et n'écrit rien. On ne l'ouvre qu'une fois la reprise des
  abonnements faite dans `org_entitlements` : avant, `has_right` rend faux pour les abonnés.
  Ouvert, il refuse encore de tourner si aucun droit `unipile` vivant n'existe alors que
  des sièges sont en service.
- ⚠️ **Préavis obligatoire** : un compte dont le propriétaire n'a pas d'adresse n'est pas
  supprimé (`sans_adresse`). Les orgs d'un **tenant tiers** sont écartées et comptées
  (`tenant_tiers_ecartes`).
- **Délai** : `OTO_UNIPILE_FIN_DE_DROIT_DELAI_JOURS` (défaut 7, entier ≥ 1). Il part du
  premier constat de la perte : rebrancher un compte ou retrouver le droit efface la marque.
- **La console** `oto_admin_unipile_seat` : `list` sert, par siège, `entitled`,
  `entitlement_lost_at` et `deletion_scheduled_at` ; `release` libère sans `force` un
  siège en service dont aucune org n'a plus le droit, et refuse toujours (409
  `seat_in_use`) celui dont l'org a le droit.
