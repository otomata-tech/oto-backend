---
title: Droits déclarés — ce qu'une personne a le droit de faire, dans une org ou partout, et à quelle valeur
type: reference
description: >-
  Le modèle des droits déclarés (ADR 0070 §7, oto-backend#1066) : le cœur applique des
  limites DÉCLARÉES sans savoir qui paie. Une ligne = une portée (l'org, une personne
  dans l'org, ou une personne dans toutes ses orgs), une clé du catalogue, une valeur entière jamais vide, une fenêtre de dates, une
  source opaque. Un seul point de lecture (`access.entitlements.value_for`) : la valeur
  de l'org (ses lignes, sinon le défaut DÉCLARÉ par l'instance, `OTO_ENTITLEMENT_DEFAULTS`
  — jamais un défaut du code), qu'une ligne de personne ne fait que relever. Les points
  d'usage branchés sur la personne, ceux qui restent sur l'org, les limites connues, et
  les révisions de la base servie.
---

# Droits déclarés

**Le cœur ne sait pas qui paie.** Il applique des droits qu'un seul producteur, le service
de facturation **oto-commerce**, a DÉCLARÉS dans la table `org_entitlements`, par l'API de
service (ci-dessous). Le cœur les relit à chaque usage, jamais mis en cache comme acquis.

⚠️ **Depuis la coupure du cœur (#1097), le cœur ne pose plus AUCUN droit.** La
réconciliation interne qui dérivait les lignes des abonnements et des dons
(`billing_droits`) est retirée à tous ses appels — démarrage, `oto-mcp maintenance
droits` (timer quotidien), gestes de facturation et dons d'option d'org — et le module
avec elle. Les gestes qui vendaient, offraient ou déclaraient un droit refusent en 409
`billing_moved` (§ Ce que le cœur refuse).

## Un droit

| champ | ce qu'il dit |
|---|---|
| `org_id` | l'org où le droit s'applique ; NULL = la personne `sub` dans **toutes** ses orgs |
| `sub` | la personne : NULL = la ligne vaut pour l'org (tous ses membres) ; posé = pour cette personne, dans l'org `org_id` ou partout |
| `right_key` | une clé du **catalogue** (ci-dessous) — toute autre est refusée à la pose |
| `value` | un **entier, jamais vide** : oui/non = `1`/`0` ; sinon le nombre (plafond, quota par jour). « Sans plafond » = `SANS_PLAFOND` (2 147 483 647), une valeur explicite |
| `starts_at`, `expires_at` | la fenêtre : **début inclus, fin exclue**, fin nulle = sans échéance |
| `source` | qui l'a posé, au sens du producteur. **Informative** (affichage, reprise) : la règle d'application ne dépend jamais d'elle |

**Trois portées** (#1089), et jamais aucune :

| portée | `org_id` | `sub` | vaut pour |
|---|---|---|---|
| org | posé | NULL | chaque membre de l'org, dans cette org |
| personne dans l'org | posé | posé | cette personne, dans cette org |
| personne partout | NULL | posé | cette personne, quelle que soit l'org où elle agit — et hors de toute org |

Une ligne sans org ni personne est refusée par la base (contrainte
`org_entitlements_une_portee`, `CHECK (org_id IS NOT NULL OR sub IS NOT NULL)`) et, avant
elle, par le code (`entitlement_scope_required`). La personne partout n'a pas à être
membre d'une org : le droit lui appartient, pas à une appartenance. Supprimer une org
emporte ses lignes d'org et de personnes dans l'org (`ON DELETE CASCADE`), jamais une
ligne de personne partout.

**Une ligne par (org, personne, droit, source)** : la contrainte
`org_entitlements_une_ligne` (`UNIQUE NULLS NOT DISTINCT`) le tient, `sub` nul comme
`org_id` nul — deux lignes `(NULL, sub, droit, source)` sont égales pour elle. Reposer la
même quadruple remplace la ligne, bornes et valeur comprises : le producteur dit l'état
entier de son droit à chaque pose.

**Pourquoi une valeur jamais vide.** Une valeur nulle voulait dire « pas d'avis », et un
plan « sans avis » sur les sièges n'écrivait rien (#805) — l'absence se lisait comme un
défaut que personne n'avait décidé. Oui, non, un nombre, sans plafond : chacun s'écrit.

## Le catalogue — `oto_mcp/entitlements_catalogue.py`

Le catalogue vit **dans le cœur, en code**. Un producteur ne pose jamais une clé que le
cœur ne sait pas appliquer.

| clé | genre | aujourd'hui |
|---|---|---|
| `unipile` | oui/non | la messagerie hébergée — lue pour la personne par l'option payante (`access.quotas`), le branchement d'un compte, l'inventaire et la fin de droit de la messagerie |
| `platform_unmetered` | oui/non | quotas levés sur les clés de plateforme — lue pour la personne par `access.quotas.plafond_du_jour` (refus, sonde, mode affiché, `/api/me`) |
| `unipile_seats` | nombre | nombre de comptes de messagerie — **pas lue** : aucune org n'est plafonnée depuis le 2026-10-23 (`unipile_connect.plafond_de_comptes` rend `0`) ; `orgs.unipile_account_limit` et `OTO_MCP_UNIPILE_DEFAULT_LIMIT` sont inertes |
| `platform_key:<connecteur>` | nombre (quota par jour, `0` = pas d'accès) | l'accès à notre clé de plateforme d'un connecteur du registre `providers` (suffixe vérifié) — **pas encore lue** : le registre (`platform_key_open`, `default_quota`) reste la règle appliquée |
| `members_max` | nombre | ⚠️ **hérité, sans lecteur** : le nombre de licences d'un abonnement réglé hors plateforme. Le cœur ne connaît aucun plafond de membres ; la clé sort du catalogue quand le commerce posera les droits payants par personne |

Hors catalogue, délibérément : `beta`, `claude_subscription` (des drapeaux de population
du cœur, lus dans `option_comps`, pas des droits vendus). **Une clé du catalogue ne se lit
jamais dans `option_comps` pour appliquer un droit** : `access.has_option` lève
`catalogue_key_via_option_comps` si on le lui demande hors de la branche payante, et les
gestes qui posaient un don d'une telle clé la refusent (`billing_moved`).
`entitlements_catalogue.est_du_catalogue(clé)` dit le territoire : une clé fixe ou la
famille `platform_key:`.

## Le point de lecture unique — `access.entitlements`

```python
valeur_expliquee(sub, org_id, key, now=None) -> ValeurExpliquee  # valeur, par, defaut
value_for(sub, org_id, key, now=None) -> int                      # valeur_expliquee(...).valeur
has_right(sub, org_id, key) -> bool     # value_for(...) >= 1
org_has(org_id, key) -> bool            # has_right(None, org_id, key)
```

`valeur_expliquee` est le SEUL code qui applique la règle ; il rend aussi sa provenance
(`par` : les lignes valides lues, `defaut` : le défaut d'instance a fourni la valeur de
l'org). `value_for` n'en rend que la valeur, et la route de service de la valeur
effective (ci-dessous) la sert telle quelle : ce qu'elle montre est ce que lisent les
points d'usage, par construction.

**Une ligne par personne ne fait qu'ajouter** (règle tranchée le 28/09/2026) :

1. seules comptent les lignes **valides** à `now` (défaut : l'horloge de la BASE, la même
   pour tous les processus), toutes sources ;
2. **la valeur de l'org** se calcule comme avant : le maximum de ses lignes d'org
   (`sub` NULL) s'il y en a une valide, **sinon le défaut déclaré par l'instance** ;
3. **la personne la relève** : le résultat est le plus généreux entre la valeur de
   l'org et les lignes valides de la personne, dans l'org et partout. Une ligne de
   personne ne retire jamais rien — ni un « oui » de l'org, ni le défaut.

Une lecture (`db/entitlements.valeurs_posees`) rend les deux maxima séparés, et les
lignes valides dont ils sont tirés (portée `org`, `personne_dans_org`,
`personne_partout` ; source ; valeur) ; la règle vit dans `valeur_expliquee`.

**Le plus généreux est le plus GRAND**, pour toute clé du catalogue : un oui/non vaut
`1`/`0`, un quota `platform_key:<connecteur>` vaut `0` pour « pas d'accès », et « sans
plafond » s'écrit `SANS_PLAFOND`, le plus grand entier. Aucune clé du catalogue ne donne
au `0` le sens « illimité » — c'est ce qui rend le maximum juste (voir les limites).

⚠️ Une ligne d'**org** vaut même quand elle est MOINS généreuse que le défaut : elle le
remplace. Une ligne de **personne** plus basse que la valeur de l'org ne compte pas.

`sub` None : la lecture d'org, exactement celle d'avant la règle. `org_id` None : le
défaut, relevé par les lignes de la personne partout. Les deux None : le défaut. La
propriété est prouvée sur vraie base, sur une grille exhaustive des trois portées et de
trois défauts (`tests/test_org_entitlements_live.py`,
`test_monotonie_par_propriete_sur_les_trois_portees`) : `value_for(None, org)` égale la
lecture d'org, `value_for(sub, org)` lui est toujours supérieure ou égale.

### Les points d'usage branchés sur la personne

Chacun connaît la personne qui agit — ou, pour les sièges, le titulaire du binding — et
lit `has_right(sub, org, clé)` :

| point d'usage | clé | la personne lue |
|---|---|---|
| `access.quotas.exiger_option_payante` → `paid_option_refusal` (palier plateforme de `access.resolve`) | `unipile` | l'appelant, dans l'org qu'il peut consommer (#480 : aucune pour le bénéficiaire d'un projet partagé sans prêt, restent ses lignes partout) |
| `access.quotas.has_option`, branche **payante** (← `views.option_open` ← carte des connecteurs, diagnostic `connectors/readiness`, statut unipile) | `unipile` | l'appelant (ou le tiers d'une fiche admin) |
| `access.quotas.plafond_du_jour` (← `resolve._win_quota` : refus et `platform_quota_hint` ; `views.credential_mode_for` ; `status.status_for`) | `platform_unmetered` | l'appelant, dans l'org qu'il peut consommer (#480) ; une seule lecture par snapshot `/api/me` |
| `unipile_connect.hosted_auth_url` (refus 402 `unipile_option_required`) | `unipile` | la personne qui connecte |
| `capabilities/unipile_seats._droit_vivant` (inventaire `entitled`, garde de reprise sans `force`) | `unipile` | le **titulaire** `sub` de chaque binding, jamais l'admin qui appelle ; cache par (titulaire, org) |
| `unipile_fin_de_droit.balayer` (le travail de fin de droit) | `unipile` | le **titulaire** de chaque ligne de `sieges_plateforme`, cache par (titulaire, org) ; le retour du droit efface les marques **par binding** (`effacer_perte`), plus par org |

Ces six points partent ensemble : l'usage sans le travail de fin de droit et la garde
de reprise servirait une personne tout en lui supprimant son compte.

L'endpoint anonyme (`access.resolve_anon`) passe `sub` None : il lit l'org seule, comme
avant.

### Ce qui reste sur la lecture d'org, et pourquoi

- `org_has` pour une question **sur l'org**, sans personne : le cockpit d'activation
  (`capabilities/connectors/activation._org_subscribed`, « l'org est-elle souscrite ? »)
  et le champ `option_source.org_comp` du statut unipile (`tools/unipile.py`), qui
  affiche le droit de l'org à côté de la marque de compte ;
- les options **non payantes** (`beta`) : une marque de compte ou d'org dans
  `option_comps`, pas un droit déclaré ; la lecture directe (`has_option_comp`,
  `user_has_option`) reste, et la marque de compte n'ouvre toujours aucune option
  payante ;
- `unipile_seats` et `platform_key:<connecteur>` : **pas encore lues** (ci-dessus) ;
- une lecture directe de la table, qui n'applique rien : le témoin « la table
  est-elle remplie ? » de la fin de droit (`db/unipile_fin_de_droit.py`, toutes
  portées).

**Personne d'autre ne lit la table pour appliquer un droit.**

⚠️ Hors org (`org_id` None — bénéficiaire d'un projet partagé sans prêt), le défaut
d'instance répond désormais, relevé par la personne partout : une instance qui déclare
`unipile: 1` ou `platform_unmetered: 1` pour tous l'ouvre aussi là, où l'ancienne
lecture refusait sans lire le défaut.

### Limites connues

- **(b) Des lectures héritées remplacent au lieu de prendre le maximum, et y `0` veut
  dire « illimité »** : le `daily_quota` de l'arête de grant d'une clé de plateforme
  (`grant.daily_quota or quota_for(...)`) et le plafond de messagerie
  `orgs.unipile_account_limit`. Leur `0` (ou NULL) n'a pas le sens du catalogue, où `0`
  = non / pas d'accès. **Toute reprise d'une valeur héritée en ligne de droit traduit
  le `0` « illimité » en `SANS_PLAFOND`** — recopié tel quel, il deviendrait « aucun
  accès » et le maximum le laisserait perdre face au défaut.
- **(e) La ligne « personne dans l'org » d'un ancien membre reste lue** s'il agit dans
  cette org par un projet partagé AVEC prêt (`heritage.org_partagee` rend l'org) :
  `value_for(sub, org)` ne vérifie pas l'appartenance. Sans prêt, l'org n'est pas
  servie et seule sa ligne partout compte. Retirer la ligne au départ du membre est
  l'affaire du producteur.

## L'écriture — `db/entitlements.py`

`grant(org_id, key, source, *, value, sub=None, starts_at=None, expires_at=None,
granted_by=None)` : pose idempotente, `org_id` None = la personne partout ; clé hors
catalogue, valeur vide ou hors genre, ni org ni personne → `ValueError` nommée
(`entitlement_unknown_key`, `entitlement_value_required`, `entitlement_value_invalid`,
`entitlement_scope_required`), rien n'est écrit. `revoke(org_id, key, source, *,
sub=None)` retire UNE ligne, de la portée exacte que disent `org_id` et `sub` — jamais
une autre. Listes : `list_for_org` (org et personnes dans l'org, échues comprises — une
console doit voir un droit échu), `list_for_person_everywhere` (la personne partout),
`list_for_person` (toutes les lignes de la personne, partout en dernier),
`list_for_right` (« quelles orgs ou personnes ont X », vivantes par défaut).

## La lecture d'administration — `capabilities/droits_admin.py` (oto-app#13)

Un admin plateforme lit les droits sans ouvrir les orgs une par une. Trois lectures,
une seule requête chacune (`db/entitlements.lignes_filtrees`, jointe aux orgs et aux
comptes pour leurs libellés), une seule forme de ligne (`EntitlementRow` : `org_id`,
`sub`, `scope`, `right_key`, `value`, `source`, `starts_at`, `expires_at`, `active`,
et en vue complète `org_name`, `org_created_at`, `email`, `granted_by`, `granted_at`) :

| lecture | où | ce qu'elle rend |
|---|---|---|
| liste transversale | `GET /api/admin/entitlements`, `oto_admin_org op=entitlements` | les trois portées, dons à une personne partout compris ; filtres `right`, `source`, `scope` (`org`, `personne_dans_org`, `personne_partout`, `personne` = les deux portées d'une personne), `active` (vrai = valides maintenant, faux = échues ou à venir, omis = toutes), `expires_before`, `org_created_after` (une ligne sans org n'y passe jamais), `org_id` ; page `limit` (100, max 500) + `cursor` ; vue resserrée par défaut, `full=true` la complète |
| fiche d'org admin | `GET /api/admin/orgs/{id}`, `oto_admin_org op=get` | `entitlements` : les lignes posées dans l'org (org et personnes dans l'org), échues comprises, vue complète — **à côté** de `billing`, jamais dedans. La fiche d'un membre (`GET /api/orgs/{id}`) n'en dit rien |
| fiche de compte | `GET /api/admin/users/{sub}`, `oto_admin_user op=get` | `entitlements` : les lignes posées sur la personne, dans ses orgs et partout, échues comprises, vue complète |

Même contrôle d'accès que la fiche d'org : admin plateforme. ⚠️ **Rien ne lit
`option_comps`, `org_subscriptions` ni `billing*`** — ils partent du cœur avec le lot 6
(oto-backend#1104) ; le banc `tests/test_droits_admin_live.py` le juge sur le SQL
réellement exécuté, et y borne le nombre de requêtes. Ces lectures n'appliquent rien.

L'inventaire des sièges unipile (`oto_admin_unipile_seat op=list`) dit aussi **ce qui
ouvre** le droit d'un siège (`entitled_by` : titulaire, org, lignes valides qui ouvrent
avec portée, source et échéance, ou le défaut d'instance), lu par
`access.valeur_expliquee` au seuil de `has_right` (`access.est_ouvert`) : `entitled` ne
peut pas différer des points d'usage.

## L'API du commerce — `capabilities/service_commerce.py` (#1069)

Un service de commerce pose et retire des droits **par l'API**, jamais en SQL, sous son
identité de service (`docs/auth-logto.md` §Identité de service). REST seule, sous
`/api/service/`, règle `COMMERCE_SERVICE` — aucun compte n'y passe, fût-il super admin :

| Route | Rend |
|---|---|
| `GET /api/service/orgs` | orgs non archivées par id, curseur `after_id` |
| `GET /api/service/orgs/{id}/members` | membres **par ancienneté** (`joined_at`), email, dernière activité sous l'org |
| `GET /api/service/orgs/{id}/usage` | par personne sur `[since, until)` (défaut : le mois en cours) : appels réussis, dont sur clé de plateforme |
| `GET /api/service/orgs/{id}/entitlements` | `list_for_org` |
| `PUT /api/service/orgs/{id}/entitlements/{right_key}/{source}` | `grant`, `granted_by = service:<client_id>` ; rend la ligne |
| `DELETE /api/service/orgs/{id}/entitlements/{right_key}/{source}` | `revoke` |
| `GET /api/service/users/{sub}` | le rôle **plateforme** d'un compte (#1087), relu par l'API d'administration du commerce |
| `GET /api/service/users/{sub}/entitlements` | `list_for_person_everywhere` |
| `PUT /api/service/users/{sub}/entitlements/{right_key}/{source}` | `grant` sur la personne partout, `granted_by = service:<client_id>` ; rend la ligne |
| `DELETE /api/service/users/{sub}/entitlements/{right_key}/{source}` | `revoke` de la personne partout seule |
| `GET /api/service/users/{sub}/entitlements/{right_key}/effective?org_id=N` | la **valeur effective** (#1096) : ce que lisent les points d'usage pour la personne dans l'org, et d'où elle vient (ci-dessous). Lecture seule |
| `GET /api/service/billing/export` | **temporaire** (#1085) : l'état de facturation du cœur (factures et PDF compris), pour sa reprise par le commerce ; part avec le retrait |

Sous `/orgs/{id}/`, `sub` (corps ou requête) vise une personne, qui doit être membre ;
omis, le droit vaut pour l'org. Sous `/users/{sub}/`, le droit vaut pour la personne
dans toutes ses orgs : elle doit avoir un compte (`404 unknown_user`), pas une
appartenance. **Chaque ligne se liste par une seule route** : une ligne d'org ou de
personne dans l'org par l'org, une ligne de personne partout par la personne (la valeur
effective ne liste pas : elle nomme les lignes valides qui entrent dans UNE lecture). `source`
est prise dans la liste fermée `entitlements_catalogue.SOURCES`.
Depuis la coupure du cœur (#1097), plus rien dans le cœur ne retire une ligne que le
service a posée : il écrit sous n'importe quelle source de la liste fermée, à toutes les
portées.

### La valeur effective d'un droit (#1096)

Un producteur externe doit pouvoir **prouver que ses lignes suffisent** avant le retrait
de la lecture héritée : relire ses lignes ne le dit pas, il faut ce que le cœur
APPLIQUE. `GET /api/service/users/{sub}/entitlements/{right_key}/effective`, même
identité et même règle (`COMMERCE_SERVICE`) que les routes voisines ; `org_id` en
requête = l'org où la personne agit, omis = la personne hors de toute org. Aucune
appartenance n'est exigée (comme `value_for`, limite (e)). Rend :

| champ | ce qu'il dit |
|---|---|
| `sub`, `org_id`, `right_key` | la question posée |
| `valeur` | `access.valeur_expliquee(sub, org_id, clé).valeur` — **le même code** que `value_for` / `has_right` des points d'usage, pas une seconde implémentation |
| `par` | les lignes valides lues, chacune `{portee, source, valeur}` (`org`, `personne_dans_org`, `personne_partout`), dans cet ordre ; une ligne échue, à venir ou d'une autre personne n'y est pas |
| `defaut` | vrai si c'est le défaut d'instance qui a fourni la valeur de l'org (aucune ligne d'org valide, ou hors org) |
| `lecture_directe` | ce que rend la lecture **héritée** pour cette personne et ce droit, un seul champ selon la clé ; `null` s'il n'y en a pas |

`lecture_directe`, clé par clé, valeurs dans LEUR sens hérité (limite (b)) :

| clé | champ | lu par |
|---|---|---|
| `unipile`, `platform_unmetered` | `option_comps: {personne, org}` | `db.has_option_comp` sur le compte et sur l'org (`org` null hors org) — des dons qui n'ouvrent plus rien : la réconciliation qui traduisait ceux de l'org est retirée (#1097) |
| `unipile_seats` | `plafond_messagerie: {plafond}` | `unipile_connect.plafond_de_comptes`, le plafond que le branchement applique — `0` (sans plafond) depuis le 2026-10-23 ; `lecture_directe` null hors org |
| `platform_key:<connecteur>` | `registre: {cle_ouverte, quota_du_jour}` | `platform_key_open` du registre et `quotas.quota_for` (`0` = illimité) — sans l'arête de grant |
| `members_max` | — | aucune lecture héritée : `null` |

⚠️ **`lecture_directe` part avec la lecture héritée** : quand plus aucun point d'usage
ne lit `option_comps`, le plafond de messagerie de l'org ni le registre pour ces clés,
le champ est retiré de la route (le consommateur est prévenu par le contrat épinglé).

Erreurs : `401`, `403 service_required` (celles des routes voisines), `404 unknown_user`,
`404 unknown_org` (org inconnue ou archivée, comme sous `/orgs/{id}/`), `400
entitlement_unknown_key`. La preuve : `tests/test_org_entitlements_live.py`,
`test_la_valeur_effective_servie_est_la_lecture_des_points_d_usage`, sur la grille de
monotonie (3 défauts × lignes d'org × personne dans l'org × personne partout, dans l'org
et hors org).

## Les défauts de l'instance — `OTO_ENTITLEMENT_DEFAULTS`

Le **gratuit**, c'est ce qu'a une personne sans aucun droit posé : les défauts déclarés
par l'instance, clé par clé. Une variable d'environnement **requise**, en JSON :

```json
{"unipile": 0, "platform_unmetered": 0, "unipile_seats": 5,
 "members_max": "unlimited", "platform_key:*": 0, "platform_key:<connecteur>": 100}
```

- `platform_key:*` est un **joker** : il couvre tout connecteur sans surcharge ;
- `unlimited` s'écrit en toutes lettres ;
- **une clé du catalogue ni déclarée ni couverte refuse le démarrage** (`server.main` →
  `verifier_defauts`, avant de préparer la base), de même qu'une clé inconnue ou une
  valeur hors genre. Jamais un `0` silencieux au premier usage.

`python -m scripts.defauts_des_droits` imprime les défauts que le code applique
aujourd'hui, dérivés du registre (`platform_key_open`, `default_quota` et sa surcharge
`OTO_MCP_QUOTA_<P>_DAILY`) et du plafond de messagerie : de quoi amorcer la variable
d'une instance qui veut notre comportement. Une instance tierce naît avec les siens —
typiquement sans aucune de nos clés de plateforme.

## Le producteur : oto-commerce, et ce que la coupure a laissé en base

oto-commerce pose et retire les droits par l'API de service, sous les étiquettes de la
liste fermée `subscription`, `trial`, `offered`, `partner`, `contract`.

⚠️ **Les lignes posées AVANT la coupure restent** : la réconciliation interne en avait
posé sous `subscription`, `offered`, `partner` et `contract`, à la portée org, et plus
personne dans le cœur ne les réaligne ni ne les retire. Elles valent jusqu'à leur
échéance — une ligne sans échéance (plan offert, contrat reconduit, don perpétuel) vaut
donc jusqu'à ce qu'oto-commerce la repose ou la retire. Reposer la même quadruple
(org, personne, droit, source) la remplace : c'est ainsi qu'oto-commerce les reprend.

⚠️ **Écart de vocabulaire avec la conception**, qui nomme les sources
`abonnement | essai | don | instance` : les étiquettes en place sont conservées, pas
renommées — la source étant opaque pour le cœur, le renommage est l'affaire du
producteur, et il réécrirait des lignes servies.

## Ce que le cœur refuse — 409 `billing_moved`

Un geste qui vendait, offrait ou déclarait un droit n'aurait plus d'effet : il refuse,
nommément, avant d'écrire quoi que ce soit (`capabilities/_facturation_externe.py`, une
seule fabrique, déclarée sur chaque capacité) :

| capacité | refusé |
|---|---|
| `billing.subscribe`, `billing.confirm`, `billing.method_change{,_confirm}` | toujours |
| `billing.cancel`, `billing.resume` | toujours |
| `billing.admin_set_plan` (`oto_admin_set_plan`, plan offert ou retiré) | toujours |
| `billing.admin_set_contract`, `billing.admin_cancel_contract` | toujours |
| `platform.option.set` (`oto_admin_set_option`) | pour une clé du catalogue ; `beta`, `claude_subscription`… restent posables, org et personne |
| `platform.connector.access_set` | pour un connecteur dont l'option est une clé du catalogue (`unipile`) ; la clé de plateforme se partage alors par `platform.org.grant_key` (une org) ou `platform.key.grant` (un compte) |

Le runner de facturation est gelé (`OTO_BILLING_RUNNER_ENABLED=0`) ; s'il tourne quand
même, il refuse chaque échéance due et le journalise en erreur, jamais un débit
(`billing_runner._refuser_l_echeance`). `confirm` lui-même, encore atteint par le webhook
et le rattrapage du runner, grave un encaissement puis refuse, sans ouvrir d'abonnement
(une erreur au journal nomme l'org et le paiement). Restent servis : `billing.status`, le
catalogue, les paiements, les factures, le webhook Mollie.

**Un seul écrivain** : seule l'API de service (`capabilities/service_commerce.py`)
appelle `db/entitlements.grant` et `revoke` — un banc AST le tient, quel que soit l'alias
d'import (`tests/test_coupure_du_coeur.py`).

## La migration de la base servie — les révisions

La base neuve reçoit la forme cible du fragment `db/schema/entitlements.py`. La base
PARTAGÉE (préproduction et production) la reçoit en deux temps, parce que le code d'avant
#1066 pose `value` NULL et cible l'ancienne clé primaire `(org_id, right_key, source)`
dans son `ON CONFLICT` :

| révision | contenu | quand |
|---|---|---|
| `0014_droits_portee_personne` | `sub`, `value` NULL → 1, contrainte `org_entitlements_une_ligne` — additif pour l'ancien code | **avant la fusion** : le code du lot lit `sub` et cible la contrainte |
| `0015_droits_valeur_obligatoire` | `value` NULL → 1, `SET NOT NULL`, retrait de la PK | **après le tag de production** : plus aucun processus ne sert l'ancien code |

Entre les deux, une ligne de personne du même (org, droit, source) qu'une ligne d'org
serait refusée par la PK encore en place — aucun producteur n'en pose encore.

La portée personne partout vient ensuite, en une révision additive :

| révision | contenu | quand |
|---|---|---|
| `0024_droits_personne_partout` | `CHECK org_entitlements_une_portee`, puis `org_id` DROP NOT NULL | avant la fusion de préférence : sans elle, seule la POSE d'une ligne de personne partout échoue (500 `NotNullViolation`) ; la lecture et l'ancien code passent |

Détail et ordre : `docs/migrations-versionnees.md` §5.1, et l'en-tête de chaque révision.

## Ce qui n'est pas encore fait

- faire lire `unipile_seats` et `platform_key:<connecteur>` par `value_for` (messagerie,
  quotas et cascade des clés de plateforme) — en traduisant le `0` « illimité » hérité
  (limite (b)) ;
- les droits payants par personne, le retrait de `members_max`.

**La fin d'essai.** La date de fin d'essai est tenue par le commerce ; le cœur la lit.
Le commerce la pousse pour chaque org (`service.org.trial`, `orgs.trial_ends_at`), et
elle est servie dans la fiche d'org ; le cœur n'agit pas dessus. Ce que le commerce
accorde pendant un essai, et ce qu'il fait à sa fin, relèvent du commerce. L'étiquette
de source `trial` reste acceptée par l'API de service.

## Ce qu'on ne fait pas

- **Pas de clé libre** : un producteur ne pose que ce que le catalogue connaît.
- **Pas de défaut dans le code** : l'instance déclare, sinon elle ne démarre pas.
- **Pas de droit mis en cache** : relu à chaque usage.
- **Pas de portée tenant** : le budget d'un tenant reste un mécanisme du cœur, hors droits
  déclarés.
