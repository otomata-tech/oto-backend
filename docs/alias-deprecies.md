---
title: Alias dépréciés, compteurs d'usage et dates de revue
type: reference
description: >-
  La table UNIQUE des noms servis qui portent encore l'ancien vocabulaire du
  produit (#519, « doctrine » → « guide ») : pour chaque surface — outil MCP, clé
  de capacité, chemin REST, clé de réponse, schéma OpenAPI, code d'erreur, objet en
  base — l'ancien nom, le nouveau, la forme de la coexistence. Depuis la décision
  du 23/11/2026, aucune date de retrait n'est servie : un ancien nom reste servi
  tant que son usage compté n'est pas nul, et la PR de retrait pose seule une date
  ferme. Dit comment mesurer cet usage (args.deprecated_name, args.deprecated_value,
  lignes des 308) et où vivent les dates de REVUE (oto_mcp/deprecations.REVUE*).
  À charger avant de renommer une surface, avant de consommer un nom qui figure
  ici, et avant d'ouvrir une PR de retrait (lot D, #526).
adr:
  - "0042"
---

# Alias dépréciés, compteurs d'usage et dates de revue

## La règle du retrait (décision du 23/11/2026)

**Plus aucun couperet par date.** Un ancien nom — outil MCP, chemin REST, valeur d'un
champ — reste servi **tant que son usage compté n'est pas à zéro**.

- **Aucun texte servi ne porte de date de retrait.** Chaque avis dit « déprécié,
  utilise X ; l'ancien nom sera retiré avec un préavis »
  (`deprecations.PREAVIS_DE_RETRAIT`) : description et réponse d'un ancien nom d'outil,
  `deprecation_warning` d'une valeur réécrite, annonce du connecteur `api`, entrées
  dépréciées et schémas d'hier de `/openapi.json`. Les 308 des anciens chemins REST
  portent `Deprecation: true`, **sans `Sunset`**.
- **Le retrait réel est une PR séparée**, ouverte quand les mesures ci-dessous sont
  vides. C'est elle qui pose une date ferme, **au moins deux mois après le premier
  avis daté qu'elle servira** (art. 8.2 du contrat de service,
  `deprecations.PREAVIS_MOIS`).
- **Les constantes `REVUE`, `REVUE_CONNECTEUR_API`,
  `REVUE_DATASTORE`** (`oto_mcp/deprecations.py`) sont des **dates de revue** : le jour
  où relire les compteurs. Elles n'ont aucun effet sur ce qui est servi — une date de
  revue passée ne change rien, et aucun test ne les compare au jour
  (`tests/test_alias_deprecies_outils.py` vérifie qu'aucun texte servi ne porte de date
  et qu'une date de revue passée laisse tout à l'identique).

## Mesurer l'usage

Tout se lit dans le journal `tool_calls` :

| Ce qu'on compte | Où | Exemple |
| --- | --- | --- |
| Un appel sous un ancien nom d'outil | `args.deprecated_name` (`tool` = le nom d'aujourd'hui) | `tool='api_get'`, `args.deprecated_name='http_get'` |
| Une valeur dépréciée réécrite (`deprecations.valeur_canonique`), faces MCP et REST | `args.deprecated_value = "<champ>=<valeur reçue>"` | `args.deprecated_value='resource_type=doctrine'` |
| Une redirection 308 d'un ancien chemin REST | une ligne `kind='rest'` : `tool` = verbe + chemin d'aujourd'hui, `args.deprecated_name` = l'ancien chemin **gabarit** (jamais le chemin réel) ; org, compte et jeton s'ils sont connus | `tool='GET /api/me/guides/{guide_id}'`, `args.deprecated_name='/api/me/doctrines/{doctrine_id}'` |

```sql
SET statement_timeout = '15s';
SELECT args->>'deprecated_name' AS ancien, args->>'deprecated_value' AS valeur,
       count(*), max(created_at)
  FROM tool_calls
 WHERE created_at > now() - interval '30 days'
   AND (args ? 'deprecated_name' OR args ? 'deprecated_value')
 GROUP BY 1, 2 ORDER BY 3 DESC;
```

La ligne d'une 308 est écrite par le journal des routes (`api.routes.RestCallLogger`,
écriture asynchrone habituelle) : la redirection n'authentifie pas et ne requête rien.
La mesure ne dépend donc plus des sept jours du journal du proxy. Une PR de retrait
s'ouvre quand ces compteurs sont vides sur la fenêtre de rétention du journal.

⚠️ Ce qui suit, jusqu'à la règle ci-dessus, garde l'historique des dates qui étaient
servies : elles ne le sont plus.

Le produit a changé de mot le 28/08/2026 (#519, décision d'Alexis) : il dit **guide**
(ADR 0042 — le guide est la primitive unique d'instruction) et **procédure** pour ce
qui s'exécute. L'ancien mot servait pour deux choses à la fois, l'objet produit *et*
« principe maison » ; c'est cette double vie qui prête à confusion.

⚠️ **Cette page couvre CE renommage-là (#519) — pas tous les renommages datés.**

⚠️ **23/11/2026 — la règle générale est désormais `docs/compat-surfaces-servies.md`**
(décision d'Alexis) : tout changement cassant d'une surface servie rend le code métier
propre tout de suite et traduit l'ancienne forme EN BORDURE, par une entrée du tampon
`oto_mcp/deprecations.py`. Chaque entrée = un réglage d'instance éteint par défaut, une
trace par appel, un critère de retrait (compteur à zéro chez toutes les instances qui
l'ont allumée) et une date de REVUE, jamais une date couperet. Les entrées sous cette
règle :

| Entrée | Réglage d'instance | Trace (`tool_calls.args`) | Revue |
| --- | --- | --- | --- |
| Tableau désigné par son NOM (§ ci-dessous) | `OTO_MCP_COMPAT_TABLEAU_PAR_NOM` | `ref_kind = 'name'` | 14/12/2026 (#1296) |
| Renommages de vocabulaire (outils, chemins REST en 308, valeurs) | — (toujours servis) | `deprecated_name`, `deprecated_value`, ligne de chaque 308 | `REVUE*` |

Les renommages qui suivent sont antérieurs à cette règle. Depuis la décision du
23/11/2026, ils ne servent plus de date de retrait non plus : §La règle du retrait
ci-dessus.

⚠️ **25/09/2026 — la règle de retrait change** (Alexis, #966) : plus de délai fixe (les
« deux mois habituels » ci-dessous). Une ancienne forme se retire quand **tous les
consommateurs déclarés** (`.github/contrat-consommateurs.json`) ont suivi, ce que la garde
de contrat constate : tant que l'un la lit encore, la retirer rougit en le nommant
(`docs/rest-api.md` §Les consommateurs déclarés du contrat).

**17/09/2026 — retrait SANS préavis, exception à la règle ci-dessous** : la
convention de retour OAuth unifiée (`?connector=<nom>&connect=connected|error|
forbidden`, oto-backend#670) doublait deux suffixes hérités (`?zoho=connected`,
`?google=connected`) depuis le 04/09. Décision d'Alexis : retrait immédiat,
sans les deux mois habituels, après mesure qu'AUCUN consommateur ne lisait
encore l'ancienne forme — le dashboard lit `connect=` depuis le 04/09
(v1.55.0), et le seul partenaire susceptible de dépendre de ces deux
connecteurs (un tenant) n'a mesuré aucune connexion Zoho ni Google. Contrairement
à doctrine→guide, ce n'était pas un renommage consommé par des tiers actifs :
c'était un doublage de précaution devenu sans objet. `ANNONCE_RETOUR_OAUTH` /
`RETRAIT_RETOUR_OAUTH` / `dans_le_preavis_retour_oauth()` sont retirés
d'`oto_mcp/deprecations.py`.

Le **lot A** (PR #525) l'a retiré de l'interne sans changer un octet servi. Le **lot
B** renomme les SURFACES — et une surface ne se renomme pas, elle se **double**.

## La règle

> **Rien de servi ne disparaît dans le lot B.** Tout gagne son nouveau nom ;
> l'ancien devient un alias déprécié, avec une date de retrait écrite là où le
> consommateur la lit.

Trois raisons, dans l'ordre de ce qu'elles coûtent :

1. **Nos appelants vivent hors de ce dépôt** : dashboard, extension, CLI, oto-core,
   plugin, fronts partenaires, flotte d'agents. Un renommage sec ne casse rien en
   CI — il casse en production, chez quelqu'un d'autre, sans trace.
2. **La prose déjà écrite cite les anciens noms** : procédures d'org, guides, corps
   de guide, messages d'erreur archivés. Personne ne la réécrit d'un coup. Un agent
   qui suit une procédure de 2026-07 doit continuer à aboutir.
3. **La base est PARTAGÉE prod/preprod** (ADR 0065) : un objet renommé sur `main`
   est renommé sous la prod du même geste. Les renommages en base sont donc
   **additifs** (vue d'abord, table ensuite, au tag).

## La date de revue (historique : c'était la date de retrait servie)

**Date de revue : 30/11/2026** (`deprecations.REVUE`). Elle était servie comme date de
retrait jusqu'à la décision du 23/11/2026, qui en fait une date de revue sans effet.
Historique : retrait au premier tag posé à partir du 30/11/2026, reporté le 30/10/2026
depuis le 29/10/2026, qui tombait **deux mois** après la mise en production du préavis
(le plancher dû, `deprecations.PREAVIS_DU`). Pourquoi le report : le dashboard lit
encore les clés d'hier, et le journal `tool_calls` ne voit pas les anciens noms
d'outils (`ToolAliasMiddleware` les traduit avant lui) — l'usage se mesure avant la
traduction, puis le lot D part.

D'où viennent ces deux mois : **l'Art 8.2 du contrat de service**, qui promet un
préavis de deux mois avant toute rupture d'interface. Le mécanisme en a servi 30 jours
du 29/08 au 01/09/2026 — constat, référence de l'article et correction dans
oto-backend#767 ; la formulation qui fait foi est celle de la pièce, pas celle de
cette page. La durée vit dans
`deprecations.PREAVIS_MOIS`, et un test en fait un plancher : allonger est libre,
descendre sous deux mois rougit. Elle ne produit plus de date servie : c'est la PR de
retrait qui datera le retrait, au moins cette durée après son premier avis daté.

⚠️ **Un tag, pas un merge — aux deux bouts.** `main` est la PREPROD : un alias retiré
au merge serait retiré du serveur que les intégrateurs sondent, avec deux mois de
préavis annoncés et zéro jour servi. Par symétrie, la fenêtre ne s'OUVRE pas au merge
non plus : les trois lots d'alias ont été mergés le 28/08/2026 et servis en production
par le tag `v1.159.0` du 29/08/2026, et c'est cette dernière date qui compte
(`deprecations.ANNONCE`).

⚠️ **Ce que ce préavis ne couvre toujours pas** (#767, ouverte sur ces deux volets) :
il ne porte que des **renommages de vocabulaire** — un paramètre qui devient
obligatoire, un champ retiré, un type changé n'ont ni alias ni date ; et il est
**passif** — les en-têtes se voient en inspectant ses réponses, personne n'est
prévenu. Un préavis actif demande le canal de notification qui manque à la plateforme
(#766).

Le texte servi vit à **un seul endroit** dans le code — `deprecations.avis`,
`avis_d_appel`, `valeur_canonique`, `PREAVIS_DE_RETRAIT` — et ne cite aucune date. Plus
aucun test ne compare une date au jour : la sonnerie du lot D est le compteur d'usage,
pas le calendrier.

Le retrait lui-même = **lot D, issue #526**, qui porte la liste complète, le
préalable de blocage (le lot C — dashboard, oto-core, oto-cli, plugin — doit avoir
basculé) et la migration en base.

## La table

Une ligne par surface. « Forme » dit comment les deux noms coexistent — ou depuis quand
l'ancien est retiré. **Lot D (#526) : outil, chemins REST et clés de réponse retirés le
23/11/2026**, les mesures étant à zéro sur toutes les instances (décision du 23/11/2026 :
l'avis déjà servi valait préavis). Restent servis : paramètres d'entrée, code d'erreur
hérité, schéma OpenAPI d'hier ; la relation en base attend sa migration.

| Surface | Ancien nom | Nouveau nom | Forme | Lot |
| --- | --- | --- | --- | --- |
| Outil MCP | `oto_admin_doctrine` | `oto_admin_guide` | **retiré le 23/11/2026** (lot D) : nom inconnu | B1 |
| Chemin REST | `GET /api/me/doctrines/library` | `GET /api/me/guide-library` | **retiré le 23/11/2026** (lot D) : 404 | B2 |
| Chemin REST | `GET /api/me/doctrines/library/{slug}` | `GET /api/me/guide-library/{slug}` | **retiré le 23/11/2026** (lot D) : 404 | B2 |
| Chemin REST | `DELETE /api/me/doctrines/library/{id}` | `DELETE /api/me/guide-library/{id}` | **retiré le 23/11/2026** (lot D) : 404 | B2 |
| Chemin REST | `POST /api/me/doctrines/publish` | `POST /api/me/guide-library/publish` | **retiré le 23/11/2026** (lot D) : 404 | B2 |
| Chemin REST | `POST /api/me/doctrines/fork` | `POST /api/me/guide-library/fork` | **retiré le 23/11/2026** (lot D) : 404 | B2 |
| Chemin REST | `GET /api/me/doctrines/{doctrine_id}` | `GET /api/me/guides/{guide_id}` | **retiré le 23/11/2026** (lot D) : 404 | B2 |
| Clé de capacité | `org.doctrine.get` | `org.guide.get` | **renommée, sans alias** ; son `operationId` historique, porté par l'ancien chemin, est parti avec lui le 23/11/2026 | B2 |
| Clé de capacité | `org.doctrine.admin_get` | `org.guide.admin_get` | **renommée, sans alias** ; son `operationId` historique, porté par l'ancien chemin, est parti avec lui le 23/11/2026 | B2 |
| Clé de capacité | `org.doctrine.admin_list` | `org.guide.admin_list` | **renommée, sans alias** ; son `operationId` historique, porté par l'ancien chemin, est parti avec lui le 23/11/2026 | B2 |
| Clé de capacité | `admin.doctrine` | `admin.guide` | **renommée, sans alias** ; son `operationId` historique, porté par l'ancien chemin, est parti avec lui le 23/11/2026 | B2 |
| Clé de réponse | `doctrine_id` | `guide_id` | **retiré le 23/11/2026** (lot D) : seule la clé d'aujourd'hui est servie | B3 |
| Clé de réponse | `doctrine_version` | `guide_version` | **retiré le 23/11/2026** (lot D) partout, SAUF dans les lignes de `GET /api/orgs/{id}/monitoring/runs`, où un client la lit encore : servie à côté de `guide_version`, même valeur, non déclarée dans `/openapi.json` ; part au lot suivant | B3 |
| Clé de réponse | `doctrine_ref_count` | `guide_ref_count` | **retiré le 23/11/2026** (lot D) : seule la clé d'aujourd'hui est servie | B3 |
| Clé de réponse | `doctrines` | `guides` | **retiré le 23/11/2026** (lot D) : seule la clé d'aujourd'hui est servie | B3 |
| Clé de réponse | `group_doctrine` | `group_guide` | **retiré le 23/11/2026** (lot D) : seule la clé d'aujourd'hui est servie | B3 |
| Clé de réponse | `doctrine` | `guide` | **retiré le 23/11/2026** (lot D) : seule la clé d'aujourd'hui est servie | B3 |
| Paramètre d'entrée | `doctrine_id` | `guide_id` | les deux acceptés | B3 |
| Paramètre d'entrée | `run_start(doctrine=)` | `run_start(guide=)` | les deux acceptés | B3 |
| Code d'erreur | `unknown_doctrine` | `unknown_guide` | l'ancien dans `details.legacy_code` | B3 |
| Schéma OpenAPI | `DoctrineMeta` | `GuideMeta` | `$ref` déprécié vers le neuf | B3 |
| Relation en base | table `doctrine_library` | vue `guide_library` | la vue sert, la table reste ; le renommage physique est au lot D | B4 |

### Une valeur d'entrée renommée : la famille « procédure » d'`oto_resource` (oto#65)

**Retirée le 23/11/2026** avec le lot D : l'ancienne valeur est inconnue, refusée comme
toute valeur invalide. Son usage, compté par `args.deprecated_value`, était à zéro.

Arbitrage d'Alexis du 23/09/2026 : « doctrine est l'ancien terme, ne doit plus
apparaître ». `oto_resource` / `oto_resource_v2` (et `POST /api/resources[/v2]`) ne
publient plus que `resource_type ∈ {datastore_namespace, project, procedure, doc}` —
énuméré, discriminant de la réponse, descriptions, refus.

| Surface | Ancien nom | Nouveau nom | Forme |
| --- | --- | --- | --- |
| Valeur d'entrée `resource_type` | `doctrine` | `procedure` | **retiré le 23/11/2026** (lot D) : valeur inconnue, refusée comme toute valeur invalide (`unsupported_resource_type` sur la surface héritée, refus de validation sur la stricte) |
| Motif de cascade `reason` | `doctrine_needs_org_owner` | — | **retiré sec** : inatteignable depuis l'ADR 0068 (les trois paliers d'un nouveau propriétaire sont ceux d'une procédure) ; un palier inconnu ressort `status: "failed"` avec la raison du store |

**Pourquoi un alias** : un appelant vivant l'envoie — l'écran de partage d'une procédure
du dashboard (`DoctrineView` → `SharePrincipalDialog`, valeur codée en dur, op
`get`/`share`/`unshare`), mesuré le 23/09/2026. Ni le nouveau front, ni le plugin, ni
oto-core, ni la CLI ne l'emploient. **Le porteur du pont** est donc le dashboard : sa
valeur est passée à `procedure`, puis l'alias est parti, à compteur nul.

**Ce qui ne change pas** : la valeur ÉCRITE en base (`resource_grants.resource_type`)
garde le mot d'avant #519 — nommée une seule fois, `ownership.TYPE_RESSOURCE_PROCEDURE`,
et traduite à la frontière de la surface par `resources_contract.KIND_OF`. Sa migration
reste au lot D (ci-dessous).

Ce qui reste EN BASE et **n'a pas de doublure** — colonne `runs.doctrine`, valeur
d'énumération `missing_doctrine` (contrainte `CHECK`), kind d'ownership `doctrine`
(`resource_grants.resource_type`, une VALEUR de ligne — plus jamais servie depuis
oto#65, voir ci-dessus), clé `doctrine_version` écrite
dans les `props` d'un nœud : ce sont des **données déjà écrites**, pas des noms.
Aucune vue ne les renomme ; il faut les migrer, et une migration de données est un
acte nommé et daté (ADR 0065 étage 2), pas une ligne de boot. **Lot D, #526.**

⚠️ **`DoctrineView` n'est pas dans cette table**, et ce n'est pas un oubli : un
modèle `Output` de premier niveau n'est pas un composant OpenAPI. Son schéma est
INLINE dans la réponse 200, et son nom n'y apparaît que comme `title` — ce
qu'aucun `$ref` ne peut viser. Le renommer n'engage personne ; publier un alias
pour lui inventerait un contrat qui n'a jamais existé.

Les vitrines anonymes `/api/guide-library[/{slug}]` (marché) et `/api/guides/library[/{slug}]`
(guides plateforme) sont retirées, et avec elles les alias `/api/doctrines/library[/{slug}]`
qui n'y menaient plus que vers un 404 (otomata-tech/oto#84). La face authentifiée
`/api/me/guide-library` reste.

### Une adresse : le NOM d'un tableau (lot 0 du retrait, 29/09/2026) — RETIRÉ du métier, TAMPON par instance

⚠️ **Retiré du code métier** : un nom de tableau y est refusé, sans repli, sur toutes les
faces (`400 datastore_by_name`, `datastore/errors.TableauParNom`, #1786). C'est ce qui est
servi par défaut.

**Tampon depuis le 23/11/2026** : une instance dont des consommateurs désignent encore un
tableau par son nom allume `OTO_MCP_COMPAT_TABLEAU_PAR_NOM`. Le nom est alors traduit en
numéro au bord (MCP, REST, portée d'un jeton), avec la résolution d'avant le retrait, et
chaque désignation laisse `args.ref_kind = 'name'` au journal. Un nom qui ne désigne rien
reste refusé comme introuvable. La réponse MCP le signale comme accepté et déprécié, avec
le numéro à passer. La description des outils ne documente que le numéro. Revue le
14/12/2026 (#1296) : l'entrée part quand le compteur est à zéro chez toutes les instances
qui l'ont allumée. Détails, lieux couverts et requête :
`docs/compat-surfaces-servies.md` §Les entrées. Ce qui suit est l'histoire.

**Sa propre date : un nom de tableau est refusé à partir du 08/11/2026**
(`deprecations.RETRAIT_NOM_DE_TABLEAU` = `RETRAIT_DATASTORE`, décision d'Alexis : un seul
geste pour le nom et pour `namespace`). Ce n'est pas un renommage mais une forme
d'adresse qui s'en va : le numéro (`ns_id`) reste. ⚠️ **L'écart est assumé** : le retrait
est servi sans date depuis le 07/09/2026 (`ANNONCE_NOM_DE_TABLEAU`), la date ne l'est
qu'à partir de ce lot — à moins de deux mois. `PREAVIS_MOIS` ne bouge pas.

Forme de la coexistence : **servi, et dit**. Une réponse MCP obtenue par un nom
(`data_*`, `oto_project op=link`, `oto_fleet op=create`) porte en tête l'avis qui donne le
numéro et la date ; une route REST adressée par un nom répond `Deprecation: true` (sans
`Sunset` depuis le 23/11/2026). La portée d'un jeton range ses tableaux par numéro (oto#158) : une portée
émise avant, encore par nom, est refusée en le disant (`token_scope_by_name`). Les
données qui gardent un nom se migrent par `scripts/tableaux_par_numero.py`.

### Des ops d'envoi sorties des lecteurs de messagerie (oto-backend#1189, 12/10/2026)

⚠️ **Remplacé le 21/10/2026** : toutes ces ops et ces outils sont retirés sans attendre le
10/12/2026 (§Messageries et §Envois ci-dessous). Ce qui suit est l'histoire.

**Sa propre date : servies jusqu'au 10/12/2026 inclus, retirées au premier tag posé à
partir du 11/12/2026** (ancienne `deprecations.RETRAIT_OPS_ENVOI`, décision d'Alexis du
12/10/2026 : deux mois de transition au lieu d'un retrait sec). Ce n'est pas un
renommage : un client MCP autorise des outils, pas des opérations, donc ce qui agit chez
un tiers sort des lecteurs vers un outil d'envoi (`docs/unipile.md` §Lecture et envoi
séparés).

| Surface | Ancien appel (part le 10/12/2026 au soir) | Nouvel appel | Forme |
| --- | --- | --- | --- |
| ~~Op d'outil MCP~~ | ~~`whatsapp_chat(op=send)`, `telegram_chat(op=send)`, `instagram_chat(op=send)`~~ | retirés le 21/10/2026, sans attendre : §Messageries ci-dessous | — |
| ~~Op d'outil MCP~~ | ~~`linkedin_unipile_chat(op=send/update/react)`~~ | retiré le 21/10/2026 : §Messageries ci-dessous | — |
| ~~Op d'outil MCP~~ | ~~`linkedin_unipile_network(op=invite/handle/cancel)`~~ | ~~`linkedin_unipile_invite(op=…)`~~ — retirés le 21/10/2026 : §Envois ci-dessous | — |
| ~~Op d'outil MCP~~ | ~~`linkedin_unipile_post(op=create/comment/react)`~~ | ~~`linkedin_unipile_publish(op=…)`~~ — retirés le 21/10/2026 : §Envois ci-dessous | — |

⚠️ **Pendant la transition, le lecteur reste déclaré `ENVOI`, pas `LECTURE`** : il
envoie encore, et un client qui le croirait en lecture seule enverrait sans demander. Au
retrait, les ops quittent son schéma, il repasse `LECTURE`, et un appel qui les envoie
encore est refusé en nommant l'outil d'envoi. La transition a pris fin le 21/10/2026 :
le refus est `outils_retires.OPS_DEPLACEES`, et sa garde datée a disparu avec elle.

### Des ops d'envoi sorties de `chat_message` et `teams_message` (oto-backend#1231, 13/10/2026)

⚠️ **Remplacé le 21/10/2026** : ces ops et ces noms sont retirés sans attendre le
10/12/2026 (§Messageries ci-dessous). Ce qui suit est l'histoire.

**La même date que #1189 : servies jusqu'au 10/12/2026 inclus, retirées au premier tag
posé à partir du 11/12/2026** (ancienne `deprecations.RETRAIT_OPS_ENVOI`, décision
d'Alexis du 13/10/2026 : même découpage, même transition). Même raison : un client MCP autorise des
outils, pas des opérations — « toujours autoriser » la lecture d'un espace Google Chat ou
d'un canal Teams autorisait aussi d'y poster en ton nom.

| Surface | Ancien appel (part le 10/12/2026 au soir) | Nouvel appel | Forme |
| --- | --- | --- | --- |
| Op d'outil MCP | `chat_message(op=send)` | `chat_send` (mêmes arguments `space` OU `user`, `text`, `account`, sans `op`) | servie PAR la fonction de `chat_send` (mêmes refus, avant toute résolution de compte) ; la réponse porte `deprecation_warning` ; la description de `chat_message` commence par « ⚠️ op=send est déprécié, utiliser `chat_send` ; retrait le 10/12/2026. » |
| Op d'outil MCP | `teams_message(op=post/reply)` | `teams_send(op=post/reply)` (mêmes `op` et arguments) | idem ; « ⚠️ op=post/reply est déprécié, utiliser `teams_send` ; retrait le 10/12/2026. » |

`chat_send` est un nom qui a déjà servi : l'envoi Google Chat d'avant la consolidation
`op=` (11/08/2026), retiré sans alias. Il revient pour le même geste, `text` toujours
requis.

Comme pour #1189, les lecteurs restaient déclarés `ENVOI` pendant la transition. Depuis
le 21/10/2026, `chat` et `teams` sont `LECTURE` et leurs acteurs `chat_act` / `teams_act`
(garde : `tests/test_chat_teams_lecture_envoi.py`).

### Gmail et Outlook : lire ou simuler, agir — deux outils (17/10/2026, retrait IMMÉDIAT)

**Retrait SANS préavis, exception à l'Art 8.2, décidée par Alexis le 17/10/2026 pour la
sécurité.** Un client MCP autorise des outils, pas des arguments :
`gmail_compose(mode="send")` envoyait sous l'autorisation donnée au brouillon, et
`gmail_message(op="trash")` vidait une boîte sous celle donnée à la lecture. L'ancien nom
étant lui-même la faille, il n'est pas servi pendant un préavis : il refuse tout de suite,
en nommant l'appel à faire (`oto_mcp/outils_retires.py`, `RENOMMAGES_ACT`, la table de
correspondance dont dérivent le refus, la migration 0062 des listes d'outils enregistrées
et l'annonce servie aux agents jusqu'au 17/12/2026). Convention : chaque connecteur sert
`<ns>` (lit ou simule ; un brouillon est une simulation) et `<ns>_act` (envoie, supprime,
déplace — le client le fait confirmer), gardée par `tests/test_convention_act.py`.

| Ancien appel (refusé depuis le 17/10/2026) | Nouvel appel |
| --- | --- |
| `gmail_list_accounts` | `gmail(op="accounts")` |
| `gmail_message(op=search/get/attachment/drafts)` | `gmail(op=…)`, mêmes arguments |
| `gmail_message(op=archive/trash)` | `gmail_act(op=…)`, mêmes arguments |
| `gmail_compose` (brouillon) | `gmail(op="draft")`, mêmes arguments sans `mode` |
| `gmail_compose(mode="send")` | `gmail_act(op="send")` : un brouillon par `draft_id`, ou le message complet |
| `outlook_message(op=search/get/attachment/drafts/folders)` | `outlook(op=…)` |
| `outlook_message(op=archive/trash/move)` | `outlook_act(op=…)` |
| `outlook_compose` (brouillon) | `outlook(op="draft")` |
| `outlook_compose(mode="send")` | `outlook_act(op="send")` : un brouillon par `draft_id`, ou le message complet |

### Messageries : lire, agir — deux outils (21/10/2026, retrait IMMÉDIAT)

**Même exception à l'Art 8.2, décidée par Alexis le 21/10/2026 pour la sécurité**, et
même convention que Gmail et Outlook. Un agent a envoyé un WhatsApp sans validation :
`whatsapp_chat`, le lecteur, était en « toujours autoriser » dans un projet et servait
encore l'ancienne `op="send"` de la transition #1189. Tant qu'un lecteur peut envoyer,
autoriser la lecture autorise l'envoi : la transition jusqu'au 10/12/2026 est abandonnée
pour TOUTES les messageries (puis, le même jour, pour la publication et le réseau
LinkedIn : §Envois ci-dessous). Table du lot : `RENOMMAGES_MESSAGERIES` →
refus nommé, annonce aux agents jusqu'au 21/12/2026, et la révision
`20261021_1219_outils_act_msg` des listes d'outils enregistrées. Leur sens : autoriser reste étroit, masquer reste large (décision d'Alexis du 21/10/2026) : une liste qui autorisait un ancien lecteur ne reçoit que le nouveau lecteur, jamais `<ns>_act` — même si l'ancien lecteur envoyait encore en transition ; un ancien outil d'envoi dédié ne donne `<ns>_act` que si celui-ci ne fait rien de plus que lui — sinon RIEN : `linkedin_unipile_send`, `_publish` et `_invite` ne donnent pas `linkedin_unipile_act`, qui publie, invite et envoie ; on l'ajoute explicitement, le refus de l'ancien nom le dit ; une liste qui masquait masque tout ce que l'ancien outil faisait.

| Ancien appel (refusé depuis le 21/10/2026) | Nouvel appel |
| --- | --- |
| `whatsapp_chat(op=list/read)` (idem `telegram_chat`, `instagram_chat`) | `whatsapp(op=…)`, mêmes arguments — fils 1-à-1 ET de groupe |
| `whatsapp_chat(op=send)`, `whatsapp_send` (idem `telegram_*`, `instagram_*`) | `whatsapp_act(op="send")`, mêmes arguments |
| `linkedin_unipile_chat(op=list/read/attendees/contacts)` | `linkedin_unipile(op=…)` |
| `linkedin_unipile_chat(op=send/update)`, `linkedin_unipile_send(op=…)` | `linkedin_unipile_act(op=…)` |
| `linkedin_unipile_chat(op=react)`, `linkedin_unipile_send(op=react)` | `linkedin_unipile_act(op="react_message")` |
| `teams_spaces(op=teams/channels/chats)` | `teams(op=…)` |
| `teams_message(op=list/replies)` | `teams(op=…)` |
| `teams_message(op=post/reply)`, `teams_send(op=…)` | `teams_act(op=…)` |
| `chat_spaces` | `chat(op="spaces")` |
| `chat_message(op=list)` | `chat(op="list")` |
| `chat_message(op=send)`, `chat_send` | `chat_act(op="send")`, mêmes arguments |

### Envois : ce qui atteint un tiers passe à `<ns>_act` (21/10/2026, retrait IMMÉDIAT)

**Même exception, même jour, même décision**, pour les lecteurs qui envoyaient encore à
un tiers hors messageries. Deux formes, deux tables de `outils_retires` :

- l'outil d'envoi DÉDIÉ est retiré dans `<ns>_act` et refuse en nommant l'appel à faire
  (`RENOMMAGES_ENVOIS`) ;
- le lecteur MIXTE reste servi sous son nom et perd ses ops d'envoi : l'ancienne op
  refuse, en nommant le nouvel appel (`OPS_DEPLACEES`, lu par `error_taxonomy.classify`).

Révision des listes d'outils enregistrées : `20261021_1352_outils_act_envois`, même sens
— une liste qui autorise un lecteur mixte n'est pas touchée (il ne fait plus que lire),
un outil d'envoi dédié autorisé devient `<ns>_act` seulement si celui-ci ne fait rien de
plus que lui (`lemlist_inbox_send` → `lemlist_act`), sinon RIEN (`linkedin_unipile_publish`,
`linkedin_unipile_invite` : `linkedin_unipile_act` envoie aussi des messages — le refus de
l'ancien nom dit de l'ajouter explicitement) ; `linkedin_unipile_act` déjà autorisé par le
lot des messageries joué seul se perd de même ; une liste qui masque masque aussi
`<ns>_act`.

| Ancien appel (refusé depuis le 21/10/2026) | Nouvel appel |
| --- | --- |
| `linkedin_unipile_publish(op=create/comment/react)`, `linkedin_unipile_post(op=…)` | `linkedin_unipile_act(op="publish_post"/"comment_post"/"react_post")` |
| `linkedin_unipile_invite(op=invite/cancel)`, `linkedin_unipile_network(op=…)` | `linkedin_unipile_act(op="invite"/"cancel_invitation")` |
| `…(op=handle, action=accept/decline)` | `linkedin_unipile_act(op="accept_invitation"/"decline_invitation")` |
| `linkedin_unipile_profile(op=endorse/action)` | `linkedin_unipile_act(op="endorse"/"update_member")` |
| `lemlist_inbox_send(op=email/linkedin/whatsapp)` | `lemlist_act(op="send_email"/"send_linkedin"/"send_whatsapp")` |
| `apollo_email(op=send)` | `apollo_act(op="send_email")` |
| `lightfield_emails(op=send)` | `lightfield_act(op="send_email")` |
| `luma_blasts(op=send)` | `luma_act(op="send_blast")` |
| `pennylane_invoice(op=send)` | `pennylane_act(op="send_invoice")` |
| `signwell_document(op=send/remind)` | `signwell_act(op="send_document"/"remind_document")` |
| `productlane_threads(op=send)` | `productlane_act(op="send_message")` |
| `productlane_changelogs(op=broadcast)` | `productlane_act(op="broadcast_changelog")` |
| `ahrefs_social(op=publish)` | `ahrefs_act(op="publish_post")` |

`lemlist_inbox` n'envoyait rien (brouillons et labels seulement) : il ne change pas.

### Personas lemlist : `lemlist_database` ne fait plus que lire (23/10/2026)

Pour qu'une recette puisse chercher dans la base partagée de lemlist, `lemlist_database`
perd ses deux ops d'écriture au profit de l'acteur unique du connecteur
(`OPS_DEPLACEES_PERSONAS`) ; l'ancienne op refuse en nommant le nouvel appel. Révision
`20261023_0713_lemlist_personas`, même sens : une liste qui autorise `lemlist_database`
n'est pas touchée, une liste qui le masque masque aussi `lemlist_act`. Une liste qui autorise
`lemlist_act` le garde, et `lemlist_inbox_send` le donne toujours : l'acteur porte désormais
aussi les personas, qu'un agent qui l'avait reçoit (choix assumé du 23/10/2026, aucun agent
vivant ne perd l'envoi). La révision du lot des envois lit `OPS_DEPLACEES_ENVOIS`, sans ce
lot.

| Ancien appel (refusé depuis le 23/10/2026) | Nouvel appel |
| --- | --- |
| `lemlist_database(op=persona_create/persona_delete)` | `lemlist_act(op="create_persona"/"delete_persona")` |

### Le connecteur Custom API renommé `api` (12/11/2026)

**Sa propre date de revue : 31/01/2027** (`deprecations.REVUE_CONNECTEUR_API`), sans
effet sur ce qui est servi. L'ancien nom part quand
**tous les consommateurs déclarés ont suivi** (règle du 25/09/2026) et que **l'usage compté
est nul** — chaque appel servi par un ancien nom d'outil porte `args.deprecated_name` dans
le journal `tool_calls` (le nom `tool` reste celui d'aujourd'hui), et chaque 308 d'un ancien
chemin laisse sa ligne `kind='rest'`.

| Surface | Ancien nom | Nouveau nom | Forme |
| --- | --- | --- | --- |
| Outil MCP | `http`, `http_act`, `http_get`, `http_post`, `http_doc` | `api`, `api_act`, `api_get`, `api_post`, `api_doc` | les deux listés et appelables, aussi par `oto_call` et depuis une recette ; l'ancien porte l'avis en tête de sa description ET de chaque réponse (`deprecations.avis_d_appel`) |
| Chemin REST | `GET`/`PUT /api/orgs/{id}/connectors/http/operations` | `…/connectors/api/operations` | 308, dérivé des routes montées (`deprecations._alias_connecteur_api`) |
| Chemin REST | `POST /api/orgs/{id}/connectors/http/operations/import` | `…/connectors/api/operations/import` | 308 |
| Chemin REST | `GET`/`PUT /api/orgs/{id}/connectors/http/definition` | `…/connectors/api/definition` | 308 |
| Nom de connecteur | `http` | `api` | **sans alias** : refusé en nommant `api` (`providers.CONNECTEURS_RENOMMES`) |
| Source de fichier | `kind="http"` | `kind="api"` | **sans alias** : refusée en nommant `kind="api"` |

Les listes d'outils enregistrées (projets, flottes, déclencheurs, travaux du runner,
outils activés ou masqués) sont réécrites par la révision `20261112_1200_connecteur_api` :
c'est de la configuration. Une liste qui nomme encore `http_get` continue de fonctionner,
l'outil s'y lisant par son nom d'aujourd'hui (`deprecations.tool_canonique`). Le texte des
tenants (procédures, guides, consignes d'agent) n'est pas réécrit : l'alias le sert.

**Les recettes aussi** — le corps d'une recette est du texte de tenant, lui non plus
n'est pas réécrit. Une recette-code (`oto.call("http_get", …)`), une recette
déclarative (`tool: http_get`), ses écritures déclarées (`writes: ["http_post"]`) et
son `data_write(via=…)` se lisent sous le nom d'aujourd'hui, par la même table
(`recipes/moteur.sous_le_nom_d_aujourd_hui`, porté par le pont des recettes-code et par
le moteur) : l'appel compte l'alias dans le journal et l'avis tombe une fois en tête de
la réponse qui joue la recette. ⚠️ Jusqu'au 18/11/2026, le pont cherchait l'ancien nom
dans le catalogue et rendait `unknown_tool` — le même appel passait par `oto_call`.

### Les outils de la plateforme sous le préfixe du tenant primaire (21/11/2026)

**Sans date de retrait.** Sur une instance dont le tenant primaire déclare un
`tool_prefix` (`docs/tenants.md`), ses comptes reçoivent les outils de la plateforme sous
`<préfixe>_x`, et l'ancien nom `oto_x` reste listé et appelable à côté. Il part quand les
consommateurs ont suivi (règle du 25/09/2026), rien de daté ; l'usage se compte au
journal `tool_calls` (`args.deprecated_name`). Le mécanisme n'est pas
`deprecations.TOOLS`, qui porte une table fixe et une date : le doublon dépend du préfixe
de l'instance (`tool_alias.sert_aussi_l_ancien_nom`, `middleware/alias`).

| Surface | Ancien nom | Nouveau nom | Forme |
| --- | --- | --- | --- |
| Outil MCP | `oto_x` (tout outil de la plateforme) | `<préfixe>_x` | les deux listés et appelables, comptes du tenant primaire seulement ; l'ancien porte « ⚠️ Ancien nom, utiliser `<préfixe>_x` » en tête de sa description et de chaque réponse ; `data_*`, `run_*`, `feedback`, connecteurs inchangés ; stockage en canonique |

### Pourquoi les clés de capacité n'ont pas d'alias

Une clé de capacité ne sort du serveur qu'à deux endroits : `/api/admin/capabilities`
— le navigateur d'objets de la plateforme, réservé à l'admin plateforme, sans
intégrateur tiers — et l'`operationId` de `/openapi.json`. Seul le second engage
quelqu'un dehors, et il est **préservé sur l'entrée dépréciée du chemin d'avant**. Il
n'y a donc rien à aliaser.

⚠️ L'`operationId` suit la **capacité**, pas le chemin. Quand la clé ne change pas
(`library.list`), c'est le NOUVEAU chemin qui en hérite : regénérer un client ne
renomme aucune méthode, seule l'URL bouge. L'entrée dépréciée reçoit alors un id
dérivé de son chemin — deux entrées ne peuvent pas partager un `operationId` sans
faire disparaître une méthode d'un client généré, en silence.

La même règle vaut pour une capacité à **plusieurs bindings du même verbe**
(`me.guides.get` / `me.guides.set` sur `/api/me/…`, `/api/orgs/{id}/…`,
`/api/groups/{id}/…`) : le PREMIER binding déclaré garde l'id de la capacité, les
suivants reçoivent l'id dérivé de leur chemin (`openapi._capability_operation_ids`,
#436). L'ordre des bindings d'une capacité fait donc partie du contrat : en ajouter
un se fait en queue. Unicité gardée sur tout le document servi
(`tests/test_openapi.py`).

### Pourquoi une clé de réponse se double, et ne se renomme jamais

C'est la panne la plus chère de tout ce chantier, et la plus **silencieuse** : un
client qui lit `doctrine` sur une réponse qui ne sert plus que `guide` reçoit `null`.
Pas d'erreur, pas de log, rien qui s'allume. Il affiche un readme vide, ou range une
liste vide dans son cache. On l'apprend par un utilisateur, des jours plus tard.

Le doublage est fait par `deprecations.avec_les_deux_noms`, appelé **site par site**,
jamais posé globalement : un passage automatique sur toute réponse traverserait aussi
les données de l'utilisateur — la ligne d'un tableau dont il a nommé une colonne
« doctrine » gagnerait une colonne fantôme. Une compatibilité ne doit jamais inventer
un champ dans la donnée de quelqu'un.

⚠️ **Un nom doublé en SORTIE doit rester accepté en ENTRÉE**, et réciproquement.
Servir `guide_id` sans l'accepter en paramètre serait un piège : l'agent recopie ce
qu'il lit, et échoue.

⚠️ **Un code d'erreur, lui, ne se double pas** — il n'y a qu'un champ `error`. Le
nouveau prend la place, l'ancien est conservé dans `details.legacy_code`.

⚠️ **L'exception mesurée du 10/09/2026 — le doublage a une CONDITION.** Le renommage
`namespace` → `datastore` a doublé ses clés, puis a retiré le doublon deux jours plus
tard, à sec. La règle ci-dessus n'est pas fausse ; elle est incomplète. Ce qui manquait :

> Un doublage ne vaut que s'il est POSÉ PARTOUT. Doublé sur certaines surfaces
> seulement, il ne protège plus personne — il fabrique une troisième chose à connaître.

Ce renommage-là avait trois politiques dans le même produit : la liste basculée à sec,
les réponses unitaires doublées, l'upload de lignes jamais basculé. Un consommateur ne
pouvait pas écrire une règle simple ; nos deux fronts portaient chacun un pont, et un
pont qu'on ne retire pas masque le renommage suivant. **La rupture annoncée a été jugée
moins chère que la dette permanente répartie sur trois dépôts.**

Ce que ça donne comme test avant de doubler, plutôt qu'une préférence :

1. **Le doublage est-il posé sur TOUTES les surfaces qui servent la clé ?** Si une seule
   reste à l'ancien nom, ou bascule à sec, le doublage ne tient pas sa promesse.
   `tests/datastore/test_cle_namespace_doublee.py` est l'instrument qui répond à cette
   question par un balayage AST — c'est faisable, ce n'est pas une affaire de vigilance.
2. **Le retrait est-il celui des chemins ?** Leurs dates avaient été liées ici par
   commodité et découplées le 10/09 : un chemin qui change rend un 308 (ça se voit), une
   clé qui disparaît ne rend rien. Deux modes d'échec, deux calendriers.
3. **Qui porte le pont chez le consommateur, et qui le retirera ?** Un pont sans porteur
   nommé est permanent.

Le doublage reste le défaut pour `doctrine` → `guide` : ses surfaces sont doublées
uniformément, et la table ci-dessus le prouve ligne à ligne. C'est exactement ce que
l'autre renommage n'avait pas.

### La prose, elle, n'a pas d'alias

Un texte se réécrit — il n'a pas deux noms. Le lot B5 est donc **le seul endroit du
chantier où l'ancien mot disparaît pour de bon** : le socle d'instructions injecté au
`initialize`, les descriptions d'outils et de capacités, les docstrings des modèles
`Output` (servis comme `description` du schéma 200), les messages d'erreur.

⚠️ **Ce lot-là se PRÉAVISE avant le tag**, pas avant le merge : le socle d'instructions
est ce qu'un agent lit en début de session, sans passer par `tools/list`. C'est la
seule partie du chantier qu'un client ne peut pas ignorer.

Ce qui RESTE dans la prose servie après B5 n'est plus de la prose : ce sont des
**valeurs** qu'un agent passe ou lit — `kind=missing_doctrine`, le paramètre `doctrine` de `run_start`, `doctrine_id`. Elles
vivent en base ou sont des alias ; elles partent au lot D, à compteur nul.

## Ce qu'un consommateur doit faire

1. **Lire la liste ci-dessus** et chercher les anciens noms dans son code.
2. **Basculer sur le nouveau nom** — il répond déjà, aujourd'hui, à l'identique.
3. Ne pas attendre le retrait pour le découvrir : il sera annoncé avec un préavis
   daté, puis l'ancien nom ne répondra plus du tout.

## Comment c'est fait, côté serveur

**La relation en base** : la table garde son nom, une **vue** `guide_library` porte
celui d'aujourd'hui, et tout le code passe par elle — au lot D, il ne restera qu'à
droper la vue et renommer la table, sans toucher une ligne de Python. La vue est
recréée à CHAQUE boot, **après tous les `ALTER`** : une vue `SELECT *` fige ses
colonnes à sa création (vérifié sur PostgreSQL 16), donc posée avant un
`ADD COLUMN` elle masquerait la colonne neuve — sans erreur, sans log, avec un `None`
là où le code attend une valeur. Elle est auto-updatable : `INSERT` avec DEFAULT et
`RETURNING`, `ON CONFLICT DO UPDATE`, `UPDATE`, `DELETE` la traversent — mesuré sur
une vraie base, pas supposé (`tests/test_guide_library_view.py`, qui compare aussi
les colonnes des deux relations).

⚠️ Une exception assumée : `db/users.py` (l'inventaire des colonnes porteuses d'un
`sub`) nomme encore la TABLE. Cet inventaire est vérifié contre le DDL, où une vue
n'apparaît pas ; y mettre la vue rendrait ce garde-fou aveugle à une entrée
réellement morte.

**Les chemins REST** : l'ancien chemin reste monté (`oto_mcp/api/alias_routes.py`) et
répond **308** vers le nouveau. Quatre crans, chacun payé par un incident possible :

- **308, pas 301/302.** Les deux derniers autorisent le client à retomber en `GET` :
  un `POST …/publish` deviendrait un `GET` — 405, ou pire, un no-op silencieux.
- **La query string est reportée.** Un client qui appelle
  `…?limit=200` : la perdre rendrait 100 entrées au lieu de 200, sans code d'erreur.
- **Les en-têtes CORS sont sur la redirection elle-même**, et le préflight `OPTIONS`
  n'est jamais redirigé. Un navigateur vérifie CORS sur chaque réponse d'une chaîne ;
  et un `OPTIONS` redirigé le ferait abandonner avant d'essayer la vraie requête.
- **Les alias sont montés EN DERNIER.** Un alias ne peut alors capturer que ce que
  rien d'autre ne sert : impossible qu'un de ses placeholders éclipse une vraie route.

L'ancien chemin porte aussi `Deprecation: true` — un intégrateur qui lit ses logs voit
la dépréciation sans ouvrir cette page —, **sans `Sunset`** (décision du 23/11/2026).
Chaque redirection laisse sa ligne au journal `tool_calls` (§Mesurer l'usage).

**Les outils MCP** : le doublage se fait au **bord du protocole**, dans
`ToolAliasMiddleware` (`oto_mcp/middleware/alias.py`), le middleware le plus externe
de tout ce qui touche au NOM.
`tools/list` sert les deux entrées ; `tools/call` rétablit le nom canonique **avant**
que quoi que ce soit d'autre ne le lise. C'est ce qui garantit que rien en aval —
gates de contexte d'appel, denylist de visibilité, journal `tool_calls`, refs
`<tool:slug>` des procédures — n'apprend qu'un alias existe, donc que rien en aval ne
peut diverger. L'alias n'est **jamais monté comme un vrai outil** : monté, il
doublerait le journal, échapperait au toggle posé sur son canonique, et survivrait au
lot D sans qu'on le voie.

L'alias est **dérivé de la liste réellement servie**, jamais du registre : il hérite
donc du filtrage de visibilité de son outil. Un outil masqué pour un compte ne
réapparaît pas par son ancien nom — ce serait un contournement de la denylist, pas
une compatibilité.

## Le cliquet

`tests/test_vocabulaire_guide.py` compte les occurrences de l'ancien mot dans
`oto_mcp/`, fichier par fichier, et refuse trois choses : un fichier neuf qui le
reprend, un fichier qui en porte **plus**, et un plafond **qui n'est plus atteint**.
Le lot B baisse le compte à chaque PR ; le lot D le met à zéro et supprime la table.
