---
title: Modèle de connecteur — les 3 couches
type: explanation
description: >-
  Carte conceptuelle canonique des trois couches orthogonales qui gouvernent tout
  connecteur oto (unipile, google, pennylane, sirene…) : disponibilité (connector_activation
  master ± override org + availability self_serve/platform_granted), authentification
  (cascade resolve_api_key BYO-user > groupe > org > tenant > clé plateforme), et option de
  connecteur (has_option d'une option payante = droit déclaré de l'org, org_entitlements,
  ADR 0070 §7 ; option_open = has_option ∪ BYO). Restreindre un connecteur = placer la clé au
  bon niveau (ADR 0053 D1). À lire AVANT de toucher activation, clés ou options ; les autres docs
  (connector-vault, roles-and-resolution) sont le détail de chaque couche.
adr:
  - "0025"
  - "0043"
---

# Modèle de connecteur — les 3 couches

> **Pourquoi ce doc.** Un connecteur (unipile, google, pennylane, sirene…) a son
> comportement gouverné par **trois couches indépendantes** qui se confondent vite.
> Cette page est la carte canonique : avant de toucher activation / clés / options,
> lire ici. Sources de vérité code : `connectors/activation.py`, `access.resolve_api_key`,
> `access.has_option`.

Pour qu'un connecteur **marche** pour un utilisateur, les **trois** doivent être OK :

| # | Couche | Question | Substrat |
|---|--------|----------|----------|
| 1 | **Disponibilité** | le connecteur est-il exposé ? | `connector_activation` (master ± override org) + `availability` |
| 2 | **Authentification** | avec quelle clé appelle-t-il l'API ? | cascade `resolve_api_key` — ordre complet dans [`roles-and-resolution.md`](roles-and-resolution.md) |
| 3 | **Option** *(options gatées only)* | l'option est-elle débloquée ? | `option_open(sub, connector)` = **BYO** ∪ `has_option` (droit déclaré de l'**org** : `org_entitlements`) |

La plupart des connecteurs n'ont que **1 + 2**. Seuls les **connecteurs à option gatée**
(le compte unipile et ses six canaux) ont la couche **3**.

> **⚠️ Un connecteur peut ne PAS porter sa propre clé** (`Connector.credential_of`,
> split unipile du 2026-08-28). Les canaux hébergés — `linkedin_unipile`,
> `whatsapp`, `telegram`, `instagram` — ont leur **couche 1**
> en propre (activation, sélection : c'est tout l'intérêt du split) mais
> empruntent les **couches 2 et 3** au compte `unipile`. Deux questions cohabitent
> donc, et tout site doit choisir laquelle il pose :
>
> | la question | ce qu'elle gouverne | le nom à employer |
> |---|---|---|
> | qui a le DROIT d'appeler ? | couche 1 | le nom **NU** |
> | avec quelle CLÉ ? | couche 2, couche 3, quota, clé plateforme, **pin `_instance=`** | `providers.credential_provider(nom)` |
>
> Le pin `_instance=` est du côté **clé** parce qu'un ref d'instance nomme une LIGNE
> du coffre, et qu'un délégant n'en a aucune : il se compare *et* se lit sous le
> porteur. Ne normaliser que la comparaison fait reconnaître le pin puis le perdre
> à la lecture (`require_credential` refuse le nom d'un délégant).
>
> **Couche 1, cran TENANT (2026-09-26)** : entre le master plateforme et l'override d'org,
> un tenant coupe un connecteur pour toutes ses orgs — un plafond, jamais une exposition
> (`docs/tenants.md` §Le plafond d'activation du tenant).
>
> La normalisation vit dans **`walk_cascade`** — le seam que traversent la résolution,
> le miroir de mode et le statut. La refaire ailleurs, c'est rouvrir la divergence du
> 2026-07-07. Détail : `docs/unipile.md` §Le split.

---

## Couche 1 — Disponibilité (le connecteur est-il exposé ?)

- **Master switch** : `connector_activation` ligne `org_id IS NULL` → activé/désactivé pour
  toute la plateforme. Deny-by-default.
- **Override par org** : `connector_activation` ligne `org_id=<X>` → force on/off pour cette
  org (sinon hérite du master).
- **`availability`** (registre `providers/`, déclaré dans `providers/<nom>.py`) : `self_serve` (l'user l'installe lui-même, BYO
  possible) | `platform_granted` (deny-by-default, débloqué par un **grant de namespace** admin).
- Appliqué à la **visibilité par session** (middleware, fail-open) + au catalogue `/api/connectors`,
  et **à l'appel** (`connectors/activation_gate.py`, fail-closed) : direct comme par `oto_call`,
  un outil d'un connecteur coupé pour l'org — ou l'équipe — sous laquelle l'appel résout est
  refusé `connector_disabled`, cran et geste de réouverture nommés (#1064).
- Surfaces : `/platform/connectors` (master + clé plateforme, super_admin) ; `/org/connectors`
  (override org).
- **Plus de restriction « vers le bas » (retirée le 24/09/2026, ADR 0053 D1).** Réserver un
  connecteur à une partie des membres d'une org ou d'une équipe (ex-RBAC ADR 0025 / 0012 B2,
  table `connector_acl`, outil `oto_connector_access`, routes `/api/{orgs,groups}/{id}/connectors/…/access`)
  n'existe plus. **Restreindre, c'est placer la clé au bon niveau** : une clé perso ne se résout
  que pour son porteur, une clé d'équipe que pour les membres de l'équipe, une clé d'org pour
  toute l'org. La table `connector_acl` n'est plus lue ; elle reste déclarée (schéma gelé) jusqu'à
  un DDL posé à la main.

## Couche 2 — Authentification (quelle clé ?)

`access.resolve_api_key(provider)` — cascade, **la plus spécifique gagne** :

```
clé MEMBRE (BYO, scopée (sub, org))  >  secret groupe  >  secret org  >  clé de TENANT  >  clé PLATEFORME (partagée)
```

> **L'étage TENANT existe depuis le 2026-08-29 (L-clés PR 1, blueprint ADR 0052).** Une clé
> posée sur un tenant (coffre `entity_type='tenant'`, `entity_id` = son slug) sert à
> **toutes les orgs de ce tenant qui n'en ont pas de plus proche** — membre, équipe, org —
> et prime sur la clé plateforme. Trois règles, toutes tenues par le walker et rien d'autre :
> - **le tenant se lit sur le sub qualifié de l'APPELANT** (`tenant_vault.rung_tenant`),
>   jamais sur le rattachement de l'org (lot L1 : aucun chemin de résolution n'en dépend).
>   Conséquence : l'endpoint MCP anonyme (pas de sub) n'a pas cet étage ;
> - **un sub nu relève du tenant `oto`, qui n'a PAS de clé de tenant** : ses clés partagées
>   SONT les instances plateforme (avec leurs grants). Refusé à la pose ET jamais sondé à la
>   lecture — les deux d'un même geste, sinon une ligne acceptée que personne ne lit (#409).
>   Mesurable : pour 99 % du trafic, le barreau ne coûte aucune lecture ;
> - **même gate que l'équipe et l'org** (`ORG_SHAREABLE_PROVIDERS`) : c'est une clé partagée.
>   Et c'est le **BYO du tenant** (`BYO_MODES`) : l'option couche 3 est levée par
>   construction, comme pour une clé d'org.
>
> La chaîne 0053 de la fenêtre L7 calcule le même étage (`chain_shadow`) — sinon chaque clé
> tenant servie compterait une divergence `inconnu`.
>
> **Depuis la PR 2 (2026-08-29)**, l'étage porte l'**arête tenant→org** de 0053 : trois états
> (MUETTE = la clé sert, comme en PR 1 ; ACCORDE = budget par org, partagé — R10 ; REFUSE =
> le barreau est sauté, l'org retombe sur la plateforme), lus par le walker ET la chaîne à la
> même source (`grants_chain.tenant_rung`). L'anonyme n'obtient l'étage que par une arête
> vivante. Le rôle « admin de tenant » (`TENANT_ADMIN_OF`, sur le sub qualifié) pose la clé
> et les arêtes par la face REST. `oto_instance op=list` rend la clé au niveau `tenant`.
> Surface et pièges : `docs/tenants.md`.

> **Scope membre (ADR 0033).** Il n'y a **plus de clé « perso » org-agnostique** : la clé
> BYO d'un membre est keyée **(sub, org de contexte)** — coffre `entity_type='member'`,
> `entity_id="{org}:{sub}"`. Posée dans l'org A, elle ne résout PAS depuis l'org B (avant,
> elle suivait l'user partout et écrasait même la clé d'org). Vaut pour les clés API
> keyed/fields, les sessions browser (Contexts Browserbase), les comptes **Google**
> (l'org du start OAuth voyage dans le state) et les bindings **unipile**
> (`unipile_accounts` PK `(sub, org_id, provider)`). Les **mounts oauth fédérés**
> (atlassian/folkmcp) en étaient la dernière exception — ils restaient `entity_type='user'`
> jusqu'au **2026-09-09**, où ils sont partis avec la fédération MCP elle-même (**ADR 0069**).
> ⚠️ **Plus aucun connecteur n'ÉCRIT à ce scope ; le scope, lui, existe toujours en base** —
> des lignes `("user", sub)` y dorment, seuls leurs écrivains ont disparu.

Deux notions à ne **pas** confondre :
- **BYO** (*bring your own*) : l'entité **pose SA propre clé**, stockée chiffrée dans
  `connector_credentials` (`entity_type` member|group|org). Possible aux **3 niveaux**.
- **Partage de la clé PLATEFORME** : Otomata détient **une** clé (`platform_keys`), et on
  **prête son usage** (métré, **jamais révélée/copiée**) via un **grant**. ⚠️ Aujourd'hui le
  grant de clé plateforme est **per-USER uniquement** (`user_grants`, `access.get_active_grant`).
  **Pas** de partage de clé plateforme au niveau org (trou connu — cf. §Trous).
- `auth_modes` du registre déclare ce qui est permis : `byo_user`, `byo_org`, `platform`.
- Gate de défense : le chemin clé-plateforme n'est valide que si `platform ∈ auth_modes`.

Surfaces : fiche user `/platform/users/<sub>` carte « connector access » → **« grant key »**
(prête la clé plateforme à CET user, métré) ; `/account` (l'user pose sa BYO).

**Attribution côté système tiers (BYO partagé groupe/org).** Un secret **partagé** (byo_org)
= **une seule identité** côté tiers : c'est **oto** qui agit sous le **propriétaire du credential**
(compte de service), pas le membre qui déclenche l'action. Sans effet en **lecture** ; en
**écriture**, l'audit « créé par » est le compte de service, pas l'utilisateur. Mitigation quand
le tiers sépare audit et assignation : poser explicitement le champ **owner** par enregistrement
(map *user oto → user tiers*) — ex. **Zoho CRM** `Owner` (le lead **appartient** au bon
commercial, seul « Created By » reste le compte de service). Attribution **native par personne**
⇒ il faut du **per-user** (BYO user, ou credential OAuth per-user — google, zoho, salesforce), pas un secret
partagé. (Noté 2026-06-24 — pertinent pour l'automatisation d'écriture Zoho (CRM client).)

## Couche 3 — Option de connecteur (unipile, linkedin hébergé)

Certains connecteurs (messagerie hébergée) sont **gatés par une option** : ils consomment des
sièges sur la clé plateforme Otomata — l'accès s'ouvre donc par l'**offre** ou par un **geste
d'admin**.

**Deux seams, deux questions distinctes — ne pas les confondre :**

**`access.has_option(sub, option)`** — « l'option est-elle ACCORDÉE ? ». Pour une option
**payante** (`unipile`), une seule règle (**ADR 0070 §7**) : un droit déclaré **vivant** de
l'org active, `access.org_has(org, option)`, lu dans `org_entitlements` — quelle que soit sa
source (abonnement, don d'org, partenaire, essai). Le cœur ne sait pas qui paie : c'est le
commerce qui écrit ces lignes, avec leur échéance (`billing_droits`, cf. `billing.md`).
⚠️ **Le don fait à une PERSONNE n'ouvre plus d'option payante** (depuis le lot 2 de #806) :
seule l'org porte un droit payant. `user_has_option` ne sert plus qu'aux options non
payantes (`beta`, un drapeau de population), que `has_option` lit sur le compte ou l'org.

**`access.option_open(sub, connector)`** — « l'option est-elle LEVÉE pour cet appel ? », donc
`has_option` **∪ BYO** (clé propre user/groupe/org). C'est le seam que lisent le statut de la
carte connecteur (`connectors_selection.option_ok`) ET le gate « connecter » (`status_for.
subscribed`) : les faire diverger a déjà produit une carte « clé d'org » + « Bloqué »
incohérente (corrigé 2026-07-07). **Un nouveau chemin appelle `option_open`**, pas les sources.

> ⚠️ **Le BYO lève la couche 3 par CONSTRUCTION, pas par faveur** : l'entité gère sa propre
> instance chez le fournisseur, il n'y a donc aucun siège plateforme à protéger. C'est la
> raison, et elle explique pourquoi le gate ne se contourne pas autrement.

> ⚠️ **Ce doc a affirmé le contraire jusqu'au 13/08/2026** (« plus de paiement — le modèle
> billing/Stripe a été retiré, la gouvernance de l'option est purement admin »). C'était vrai
> à l'écriture, faux depuis l'**ADR 0043** (abonnement par org, PSP Mollie, LIVE prod le
> 03/08/2026) : l'abonnement est redevenu une source de `has_option`, et la carte canonique
> disait encore qu'il n'en existait qu'une. Un lecteur qui s'y fiait concluait qu'un client
> abonné devait quand même recevoir un comp.

Surfaces : bouton **« accorder l'option »** (super_admin) sur la fiche **org** (`option_comps`
org, recopié en droit `offered` avec son échéance) ; l'abonnement pose les droits de son plan
(source `subscription`). Le don sur la fiche **user** est encore posable, mais n'ouvre plus
l'option payante.

**Le droit est relu à CHAQUE usage de la clé plateforme** (lot 3 de #806) : au palier
plateforme de `resolve._resolve_credential_impl` et de l'endpoint anonyme, quand
`check_usage` est vrai, un connecteur qui exige une option payante refuse si l'org ne porte
pas le droit vivant (`quotas.exiger_option_payante` ; pour le bénéficiaire d'un projet
partagé à qui rien n'est prêté, aucune org ne le couvre, #480) — le refus nomme la cause,
sans repli.
Une clé propre (BYO) reste servie ; configurer une connexion (`check_usage=False`) ne
change pas. Avant ce lot, l'option ne gardait que l'ENTRÉE : un siège déjà connecté
continuait de fonctionner après la fin de l'abonnement.

---

## Lire les trois couches ENSEMBLE — `ready`, et pourquoi il a fallu l'inventer

⚠️ **`state` ne dit PAS si le connecteur marche.** Ajouté le 28/08/2026 après les
signaux #476, #504, #574 et #452 — quatre formes d'un même défaut : *une surface
publie UNE couche en laissant croire qu'elle répond pour les trois*.

Le cas fondateur (**#476**, 16/08) : la carte rendait `state:"active"` +
`recommended:true`, `oto_instance(op="verify")` répondait `ok:true` — et rien ne
pouvait partir, aucun canal hébergé n'était lié. **Trois lectures vertes, capacité
absente.** L'opérateur a lu « active » comme « connecté », ce qui est la lecture
naturelle, et a cherché **cinq jours** au mauvais endroit. Chaque surface disait vrai
séparément :

| surface | ce qu'elle SAIT | ce qu'on lui faisait dire |
|---|---|---|
| `state` (`connectors.me`) | le membre l'a installé dans sa boîte à outils | « il est connecté » |
| `oto_instance op=verify` | la clé résolue répond | « tout est bon » |
| `oto_identity op=list` | les comptes liés | (vide, sans dire pourquoi — #504) |

**Le seam qui les lit ensemble : `oto_mcp/connectors/readiness.py`** (`diagnose`), qui
rend la **PREMIÈRE** couche manquante dans l'ordre `option (3) → clé (2) → quota →
clé REJETÉE par l'amont → étape restante` — plus le geste, relayé tel quel depuis
`status_hints`. ⚠️ Un connecteur **sans credential** (`secret_kind="none"`, hors
`providers.CREDENTIAL_PROVIDERS` : `droit`, `web`, `culture`, `foncier`…) n'a **pas de
couche 2** : la marche de clé est sautée (`mode = None`), sinon elle rend `forbidden`
par construction et la carte envoie poser une clé qui n'existe pas (oto#173). Deux surfaces le consomment, et **une troisième formulation est
interdite** : c'est ce qui avait déjà fait diverger `option_ok` et
`status_for.subscribed` (corrigé le 07/07/2026).

- **carte connecteur** → `ready` / `not_ready` / `next_step`, sur une lecture **ciblée**
  (`op='list', name=…`). Sur le catalogue entier : `readiness:"not_computed"` + le geste
  pour l'obtenir. Ce n'est pas de la pudeur — **mesuré sur la prod le 28/08 : 1 993 ms
  pour 90 connecteurs** (≈22 ms l'unité, une marche de cascade chacun), sur un serveur
  MONO-LOOP ; un connecteur seul coûte ~244 ms. La règle qui en sort vaut au-delà d'ici :
  **dire « je n'ai pas calculé » coûte moins cher que rassurer à tort.**
- **liste d'identités** → `reason` + `next_step` sur `identities: []` (#504).

### `credential_rejected` — la clé est là, le fournisseur n'en veut pas (#541, 03/09/2026)

Cinquième forme du même défaut. La clé `linear` d'une org était **refusée par Linear**
sur l'appel le plus simple (`AUTHENTICATION_ERROR`) — invalide ou révoquée. Le verdict
EXISTAIT en base : `oto_instance op=verify` l'y écrit depuis toujours
(`connector_credentials.meta.health_ko` + `health_reason`). Il n'avait simplement
**aucun lecteur du côté où l'on regarde** : son seul consommateur, `access.status_for`,
ne lit que les clés de palier **MEMBRE**, donc jamais celle d'un connecteur `byo_org`
only. `ready` répondait `true` sur une clé morte, et le porteur n'apprenait le refus
qu'au premier appel, sous la forme du **message brut du fournisseur**.

Trois pièces, et il fallait les trois :

1. **Le lire** — `access.credential_rejection_for` remarche la cascade et lit la santé
   de la ligne qui résoudrait *vraiment* (une clé perso saine ne doit pas masquer le
   rejet d'une clé d'org, ni l'inverse). ⚠️ **C'est une SECONDE marche** (~22 ms) :
   assumée plutôt que de faire rendre deux choses à `credential_mode_for`, dont le
   contrôle de quota plateforme ne se recopie pas.
2. **L'écrire là où il tombe** — la cible de santé de `op=verify` ne valait que pour le
   palier membre : un `level="auto"` qui résolvait une clé d'ORG n'écrivait **rien**.
   Elle vaut désormais pour la ligne réellement testée (compte compris), **sauf** les
   paliers `tenant` et `platform` : le hoquet réseau d'un seul membre n'a pas à peindre
   en rouge une clé partagée par des orgs entières.
3. **Nommer le refus à l'appel** — `tools/linear.py` portait une branche 401/403 qui
   nommait exactement ce cas, **inatteignable** : Linear répond son refus d'auth dans un
   `errors[]` GraphQL sous **HTTP 200**, donc `LinearGraphQLError` est levée avant tout
   `raise_for_upstream`. Leçon transférable : *une branche d'erreur gardée sur le statut
   HTTP ne voit pas un fournisseur GraphQL* — le code est dans le corps.

Le constat se **lève** en rejouant `op=verify` (un succès écrit `health_ko: false`) ou
en reposant la clé (une repose réécrit `meta`). C'est dit dans le `next_step`, parce
qu'un état qui ne sait pas s'effacer devient un faux positif permanent.

#### Crédits épuisés, vus à l'APPEL (`quota_exhausted`, 25/09/2026)

La sonde classait déjà un 402 en `no_quota`, mais personne ne la rejoue avant de
travailler : un agent tombait sur « crédits épuisés » en plein travail (theirstack,
AI Ark…), recevait `invalid_input` (« corrige ton appel »), et la carte restait verte —
13 signaux avant ce lot. Désormais :

- **la taxonomie** (`error_taxonomy.classify`, cran 0) classe tout **402** amont en
  `quota_exhausted`, non rejouable, **même sous la `McpError` curée** qu'un outil lève
  dans son `except` (son message est gardé ; le code dit la catégorie) ;
- **l'enveloppe** (`ErrorEnvelopeMiddleware`, et `oto_call` qui court-circuite la
  chaîne) marque alors la ligne du coffre **qui a servi l'appel** — le relevé porte
  `credential_row`, posé par le résolveur unique — avec `meta.health_verdict =
  "no_quota"` (`connectors.health.suivre_appel`). Garde de portée inchangée : une clé
  **plateforme ou tenant n'est jamais marquée** (l'agent reçoit quand même
  `quota_exhausted`) ;
- **le premier appel réussi** sur cette clé lève la marque : une seule écriture
  conditionnelle (`credentials_store.clear_health_if_verdict`) par clé et par process,
  hors de la boucle — jamais une autre marque (`unauthorized` ne se lève qu'à la sonde
  ou à la repose) ;
- **la carte** (`readiness`) lit le verdict : « à sec, recharge chez le fournisseur »
  plutôt que « repose la clé » (`credential_health` préfixe la raison par
  `NO_QUOTA_REASON_PREFIX`) ; la sonde `op=verify` persiste aussi son verdict classé.

Un 403 n'est **pas** lu comme un solde vide : c'est un rejet de clé, sauf chez un
fournisseur qui le déclarerait (aucun à ce jour). Limite connue : AI Ark refuse par
point d'accès, une clé peut donc être marquée « à sec » alors qu'un autre point d'accès
répond encore.

**Un fournisseur qui dit « à sec » autrement qu'en 402** se traduit DANS son module, vers
une exception qui porte `status_code = 402` : la taxonomie et la sonde font le reste, sans
chemin parallèle. C'est le cas de **Serper**, qui répond `400 « Not enough credits »`
(`tools/serper.py`, `a_sec` / `SerperASec` ; tout autre 400 reste une entrée invalide).
`web_read`, dont le cran serper n'est qu'un cran, le **saute** en le disant et marque la
clé lui-même (`connectors.health.marquer_quota_epuise`), puis retire `credential_row` du
relevé : un `web_read` réussi par un autre cran ne doit pas effacer la marque.

Le refus « aucune clé » ne propose le **prêt d'une clé plateforme** que si oto en détient
une pour ce connecteur (`access.resolve._poser_ou_accorder`) — et dit qu'il relève des
admins d'oto, pas de ceux de l'org.

⚠️ **`ready` n'inclut PAS l'état de sélection** (`not_selected` / `paused`), et c'est
volontaire : un connecteur non sélectionné reste **appelable par `oto_call`** (dispatch
universel, ADR 0036). La sélection gouverne la **visibilité** des outils, jamais
l'aptitude — les mélanger recréerait la confusion de #476 sous un autre nom.

> Le coût de cette confusion, re-mesuré le **08/09/2026** (org 249, signal 800) : une
> procédure lisait `state` comme un verdict de capacité et a traité Slack comme bloqué
> **quatorze jours** — alors que la même procédure prescrivait `oto_call`, qui
> contourne la sélection par construction. `state: not_selected` + `ready: true` +
> `oto_call` qui passe est l'état NORMAL, et c'est celui qui a été lu comme une panne.

### Une couche que rien ne regardait : QUI la clé authentifie

Les trois couches disent si un appel **partirait**. Aucune ne dit **au nom de qui**. Une
clé remplacée par celle d'une **autre application du même fournisseur** authentifie
parfaitement : le coffre voit une clé saine, `ready` reste vrai, `verify` répond `ok` —
et tout ce que l'application précédente avait acquis est perdu, parce que ça
appartenait à l'app, pas à la clé.

Vécu sur l'org 196 (signaux **802** et **814**, 3→8/09/2026) : le credential Slack
reposé le 07/09 appartenait à une app créée la veille. Les quatre canaux clients
**privés** sont devenus illisibles — `not_in_channel`, le **même code** qu'un canal
jamais rejoint, donc indistinguable d'un problème de droits — et le seul remède
disponible sur un canal public, re-rejoindre, écrivait « a rejoint le canal » **six
fois par jour dans le canal d'un client**. Un canal privé, lui, ne se rejoint par
aucune API : il faut qu'un humain tape `/invite`.

Ce qui manquait n'était pas une sonde de plus : `_verify` **appelait déjà** `auth.test`
et **jetait la réponse**. C'est le seul corps où le fournisseur NOMME son application.
Il est maintenant rendu, sous `identity`, un bloc par jeton posé
(`oto_instance op=verify` / `POST /api/me/connectors/{provider}/verify`) :

```json
"identity": {"bot":  {"app_id": "A0B…", "bot_id": "B0C…", "team": "…", "user_id": "U0C…"},
             "user": {"team": "…", "user_id": "U0B…"}}
```

⚠️ **oto ne compare rien et ne garde aucun hier.** `identity` rend la question
répondable, pas répondue : c'est à l'appelant de retenir la valeur et de la comparer.
Un changement d'app est donc **constatable par qui regarde**, il ne se **signale** pas.
Son absence veut dire « ce connecteur ne l'expose pas », jamais « rien n'a changé ».

### Purge silencieuse des mounts OAuth (atlassian/folk) — oto#25 lot (a), 2026-09-04

> ⚠️ **Récit au passé.** Les deux connecteurs de cette section, `atlassian` et `folkmcp`,
> ont été retirés le **2026-09-09** avec le mécanisme lui-même : la **fédération MCP**
> (`kind="mount"`) ne fait plus partie de la plateforme (**ADR 0069** — trois connecteurs
> fédérés déclarés, aucun jamais utilisé, zéro appel sur 40 jours). La leçon, elle, reste
> vraie et transférable : **un credential rangé à un scope que le batch générique ne
> regarde pas est un credential dont la santé n'est lisible nulle part.**

Sixième forme, propre à la famille des connecteurs OAuth **fédérés « mount »**
(atlassian, folkmcp — Rovo Remote MCP, MCP officiel Folk) : leur credential vivait au
scope **LEGACY** `("user", sub)`, pas au scope MEMBRE `(org, sub)` des connecteurs
keyés (`connectors/link.py` explique pourquoi — un module par module, jamais une
boucle générique qui devinerait le rangement). `access_token_for(sub)` rafraîchissait
l'access token de façon transparente à chaque appel ; jusqu'à ce lot, un refresh token
mort (`invalid_grant`) faisait **PURGER** la ligne (`clear_credential`) — le fait
« ça a été révoqué » redevenait indiscernable de « jamais posé », un repli qui
masque un problème plutôt que de le nommer.

**Le correctif** réutilisait le mécanisme déjà en place pour les connecteurs keyés :
la ligne reste, et se fait marquer via `credentials_store.update_meta(..., {
"health_ko": True, "health_reason": <motif brut>})` — même paire de champs que
`_record_health`, motif fournisseur **brut** en valeur de champ (`invalid_grant`),
pas la seule catégorie opaque `credential_rejected`. Elle se levait en reposant la
clé (reconnexion : `persist_token` écrase `meta`) ou par un futur refresh réussi
(qui écrase `meta` lui aussi). **Ce patron-là n'est pas parti** : il reste celui des
connecteurs keyés, et c'est lui qu'un prochain connecteur OAuth réutilisera.

**Rendre la marque observable avait demandé un second geste**, propre à cette famille :
le batch générique de `access.status_for` qui lit `health_ko`/`health_reason`
(cf. #541 ci-dessus) **ne regarde que le palier MEMBRE** — il ne voyait donc
*jamais* une ligne `("user", sub)`. `connectors/link.py::LinkState` porte
toujours `health_ko`/`health_reason` ; chaque module lit sa propre ligne dans
son `_link_state()` (il sait sous quel scope il range son credential, une boucle
générique se tromperait), et la 4ᵉ boucle de `status_for` (celle qui ferme le
trou « ces connecteurs n'ont aucune entrée dans `me.providers` ») relaie ces deux
champs sur l'entrée `ProviderStatus` — c'est ce que lit la fiche `/api/me` du
dashboard. ⚠️ Depuis le 2026-09-09 ce seam n'a plus qu'**un seul déclarant, `google`**,
et il ne se replie pas pour autant : sa valeur n'a jamais tenu au nombre d'occupants.

✅ **Fermé le 2026-09-05 (oto-backend#876), puis rendu INERTE le 2026-09-09** : le
walker (`access/cascade.py`) porte un barreau dédié au scope legacy `("user", sub)`,
gaté par la liste FERMÉE `LEGACY_USER_SCOPE_PROVIDERS` — à ne pas confondre avec
`connectors.link.entries()` (google y est aussi, migré au scope membre). Cette liste
valait `("atlassian", "folkmcp")` ; elle est **vide** depuis le retrait de la
fédération, donc le barreau ne se déclenche plus pour personne — il est **gardé, pas
supprimé** : ⚠️ le scope legacy porte encore des **lignes en base**, seuls ses
écrivains ont disparu, et c'est la seule marche du walker qui saurait les lire.
`oto_instance op=verify` avait fonctionné pour `atlassian` : sa sonde relisait
`access_token_for` (refresh transparent, marque plutôt que purge) PUIS interrogeait
réellement l'API du fournisseur — leçon qui survit au retrait : **un refresh qui
réussit ne suffit pas à dire « connecté »** si l'app a été révoquée côté admin du
fournisseur. Le banc `tests/test_cascade_legacy_user_rung.py` porte encore l'histoire
de ce barreau. La migration
ADR 0033 de ces deux connecteurs au scope membre (suivant Google, commit 79759702)
était restée une décision **séparée** ; elle est sans objet depuis leur retrait, mais
les lignes dormantes, elles, n'ont jamais été migrées ni purgées.

Changement de comportement **servi** au moment du lot : un connecteur qui, avant lui,
semblait redevenir « à connecter » (purge muette) après un grant mort disait ensuite
`health_ko: true` sur sa fiche.

### La quatrième confusion : la boîte à outils n'est pas l'org de l'appel (#577)

La toolbox d'une session MCP est calculée **au handshake**
(`session_visibility.compute_hidden_tools`, sur `on_initialize`) : à cet instant aucun
jeton `_org=` n'existe, donc `current_org` retombe sur l'**org maison**. Une session
planifiée épingle ensuite `_org=` à **chaque appel** — mais le registre d'outils, lui,
reste figé sur la maison. Prouvé par différentiel le 28/08 : le sub qui fait tourner la
procédure de #577 a pour maison une org à deux connecteurs (`folk`, `grain`) et
travaille sur une AUTRE org, qui en porte treize. Les sept outils « manquants » **existaient, résolvaient, et
ont répondu du premier coup via `oto_call`** — trois matinées de faux rapports « Linear
est en panne » (20-22/08).

`connectors.me` NOMME désormais l'écart (`toolbox_scope`), et seulement quand il y en a
un : un champ toujours présent devient du bruit qu'on cesse de lire.

---

## Récap — « activer unipile pour quelqu'un »

1. **Disponible** ? unipile master ON (✓ par défaut).
2. **Clé** ? il pose sa clé Unipile (BYO) **ou** un admin lui **grant la clé plateforme** (fiche user → « grant key »).
3. **Option débloquée** ? l'org est **abonnée** à un plan qui l'inclut, **ou** un admin
   **accorde l'option** (comp, fiche user ou org), **ou** l'entité est en BYO.
4. Puis **lui** connecte son LinkedIn/WhatsApp (hosted-auth, `/console/connectors`).

## Trous connus (à combler)

- **Clé de tenant (L-clés PR 2, 29/08)** : le dashboard (`keyStack.ts`, oto-front) ne
  connaît pas encore le mode `tenant` de `/api/me`, ni les surfaces REST de l'admin de tenant
  (clés, arêtes, admins). Le budget d'arête est débité à la résolution, pas au succès
  (bascule avec L8). (couche 2)

- **Partage de clé plateforme org-level** : aujourd'hui le grant de clé plateforme est per-user
  seulement ; pas de « partager la clé plateforme à toute une org ». (couche 2)
