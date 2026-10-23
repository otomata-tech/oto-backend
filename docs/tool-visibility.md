---
title: Visibilité des outils
type: reference
description: >-
  Comment la toolbox d'une session est calculée : `UserDisabledToolsMiddleware`, denylist or
  g/équipe, sélection par membre (régime nominal ADR 0019/0050), `PROTECTED_TOOLS`, refresh 
  à chaud sur bascule d'org, et la limite REST → session MCP ouverte.
---

# Visibilité des outils (per-user, org/équipe, socle)

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## Le calcul de la denylist de session

`UserDisabledToolsMiddleware` (`middleware/disabled_tools.py`) applique au handshake `initialize` les visibility rules natives fastmcp (`disable_components` via `_visibility_rules` session state). Plus de filtrage manuel `on_list_tools`/`on_call_tool` — fastmcp émet `tools/list_changed` automatiquement quand les rules changent. Le **calcul** de la denylist `(sub, org active)` + son application vivent dans **`session_visibility.py`** (`compute_hidden_tools` / `apply_session_visibility(ctx, sub, *, reset=…)`), partagés entre le middleware (handshake) et le **refresh à chaud** post-bascule.

### ⚠️ La toolbox est celle de l'org MAISON, pas de l'org que l'appel épingle (#577)

Le calcul lit `access.current_org(sub)` — mais il tourne à **`on_initialize`**, où aucun
jeton `_org=` n'existe encore : le seam retombe donc sur la **maison**. Une session
planifiée (runner, procédure) épingle ensuite `_org=` à **chaque appel**, sur une toolbox
figée pour une AUTRE org.

Prouvé par différentiel sur la prod le 28/08/2026 : le sub qui fait tourner un job
planifié quotidien a pour maison une org à deux connecteurs (`folk`, `grain`
sélectionnés) et travaille sur une AUTRE org, qui en porte treize (dont `granola`,
`slack`, `linear`, `folk`). Le `ToolSearch` de Step 0 n'a rendu que les outils méta et spine — et les sept
outils réputés « manquants » **existaient, résolvaient, et ont répondu du premier coup
via `oto_call`** (vérifié par `connectors/readiness.diagnose` : `granola` et `linear`
sont `READY` sur l'org de travail pour ce sub). Coût réel : trois matinées de faux rapports « Linear
est en panne » (20-22/08/2026).

**Un outil absent de la liste n'est donc PAS un connecteur en panne.** En attendant que
la toolbox suive l'org épinglée, l'écart est NOMMÉ (`toolbox_scope`, cf.
`docs/connector-model.md`) — et seulement quand il y en a un, un champ toujours présent
devenant du bruit qu'on cesse de lire.

⚠️ **Il l'est à DEUX endroits depuis le 03/09/2026, et le second est celui qui compte.**
L'aveu ne vivait que sur `connectors.me` — c'est-à-dire là où on ne va que si l'on
soupçonne déjà quelque chose. Or un agent qui cherche un outil appelle
`oto_list_my_tools`, et y lisait une liste vide **sans un mot**, avec un hint qui disait
« aucun outil ne porte ces mots, reformule ». Sur écart de boîte ce texte est faux et
coûteux : l'outil existe et reste appelable. Deux signalements l'ont payé après le
correctif de #577 — un passage planifié a conclu qu'une source était injoignable et
publié un rapport faux (#616), un autre a lu trois bascules de visibilité en dix jours
là où il n'y avait qu'un handshake monté sur une autre org (#639).

`oto_list_my_tools` porte donc `toolbox_scope`, et son hint de zéro résultat **bascule** :
sans écart il fait reformuler, avec écart il dit l'inverse — l'outil existe, appelle-le
par `oto_call` avant de conclure. Le texte est isolé dans `catalogue.hint_zero_resultat()`
pour être éprouvé seul, et une sonde tient que la liste consulte bien le seam : sans
elle, un remaniement le reperdrait en silence et le signal reviendrait une troisième
fois. **La leçon générale, elle, est dans `docs/conventions.md` : un correctif de cette
classe doit être posé là où l'agent REGARDE, pas là où le défaut a été compris.**

## Source de vérité + retrait des presets

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). **Les presets de tools (snapshots nommés + baselines ALLOWLIST org/équipe) ont été retirés le 2026-07-03** (commit `3951a57` — masquaient tout ce qui n'était pas listé, lourd à maintenir : un tool ajouté après coup arrivait masqué par défaut pour toute baseline posée).

## Denylist org/équipe

**Remplacés depuis par un DENYLIST org/équipe** (`capabilities/tools_visibility.py`) : un org_admin/chef d'équipe masque des tools SPÉCIFIQUES par défaut pour son org/équipe (`org_disabled_tools`/`group_disabled_tools`) — le reste, y compris les tools futurs, reste visible par défaut. Additif entre paliers (union à la lecture) : une équipe ne peut jamais RÉVÉLER un tool que l'org a masqué. Gouvernance de visibilité, PAS une barrière de sécurité (ADR 0031, même esprit que `DEFAULT_HIDDEN_TOOLS`) : `user_enabled_tools` (override perso positif) lève TOUJOURS ce masquage, même échappatoire qu'un masqué-par-défaut plateforme. Calculé fail-open **indépendamment par palier** dans `session_visibility.compute_hidden_tools` (`access.org_admin_hidden_tools`/`group_admin_hidden_tools`) — un hoquet DB sur l'équipe ne prive pas l'org de son denylist. Surfaces : MCP + REST `GET/PUT/DELETE /api/{orgs,groups}/{id}/tools/{name}?/hidden`.

## Sélection par membre — régime NOMINAL (ADR 0019/0050)

**Sélection par membre = régime NOMINAL « non-sélectionné = masqué » (ADR 0019/0050).** La toolbox d'un membre = les connecteurs qu'il a **installés** (`user_selected_connectors`, per (sub, org)). Au premier profil d'un (sub, org), `session_visibility` seed le socle `providers.DEFAULT_ACTIVE_CONNECTORS` ∩ exposé — **VIDE depuis le 16/07** (décision produit : un nouveau compte démarre SANS connecteurs installés ; l'agent guide depuis les tools spine — `oto_connector` op=list/select, `oto_call` — et le catalogue injecté au bloc A) ; tout l'exposé = library installable (capacité `connectors.select`, dashboard). Les pairs pré-0050 ont été backfillés une fois avec leur visible d'alors (`connector_selection.backfill_preexisting`, sentinelle `#adr0050-backfill`). **Trace (ADR 0050 §E7, oto#166)** : chaque ligne porte sa **provenance** `origin` — `socle` / `kit` (semis ou geste d'admin sur le kit) / `admin` (poussée nominative) / `membre` (installation ou reprise par le membre ; une pause ne la change pas) / `inconnue` (écrite avant la trace, ou par un code qui ne la connaît pas — jamais retirée par un geste d'org). Le **retrait** par le membre (`connectors.unselect`) efface la ligne ET se retient dans `connector_selection_removed` avec sa date ; un `select`/`pause` du membre l'efface. Les gestes d'org et le semis passent par `connector_selection.install_for_member`, qui ne réécrit jamais une ligne existante et ne remet jamais un retrait. La carte (`GET /api/me/connectors`) sert `origin` sur une ligne installée et `removed_at` sur un retrait du membre. **Kit d'organisation (ADR 0050 §E8, oto#166)** : `orgs.default_connectors` est le kit que l'org installe à ses membres. Les trois gestes — poser le kit entier (`connectors.recommend`, `PUT /api/orgs/{id}/default-connectors`, `oto_connector op=recommend`), y ajouter (`connectors.bulk_select`, `POST …/bulk-select`), en retirer (`connectors.unset_default`, `DELETE …/bulk-select`) — passent par **une** fonction, `connectors.kit.appliquer` : le kit et les boîtes des membres dans une transaction (ligne de l'org verrouillée), **seule la différence** avec le kit d'avant s'applique (décision Q4 : un connecteur déjà au kit ne se rejoue pas, `unchanged` + `unchanged_note` disent pourquoi et comment), un ajout est installé chez tous les membres de l'org (provenance `kit`, jamais par-dessus une ligne existante ni un retrait du membre), et la réponse est chiffrée par connecteur (`installed` / `already_active` / `paused` / `removed_by_member`). Un **retrait du kit** (décision Q1) désinstalle là où le KIT a posé la ligne (provenance `kit`, active ou en pause) et nulle part ailleurs (`uninstalled`, et `kept` par provenance). La **poussée à un membre** (`connectors.force.member`, `oto_connector op=force`, décision Q2) passe par la même fonction (`pousser_a=`) : elle installe chez lui seul, provenance `admin`, et se REFUSE (409 `paused_by_member` / `removed_by_member`, avec la date) plutôt que de passer outre son choix ; elle n'écrit plus de préférence de visibilité par outil. Les membres pas encore venus reçoivent la ligne au geste ; leur semis la garde. **Garde d'écriture (§E2)** : un AJOUT au kit nomme un connecteur connu du registre et exposé pour l'org, sinon tout le geste est refusé, raison nommée (`unknown_connector` 404, `org_disabled` / `platform_disabled` 409, `details.refused`), rien n'est écrit. Un connecteur coupé APRÈS sa mise au kit y reste : le **semis ne filtre plus le kit par l'exposition** (il installe le kit entier ; le socle, lui, reste filtré), la ligne est masquée par le bloc d'activation tant que la coupure dure et redevient visible seule à la réouverture. Les gestes du kit le listent (`cut`, `cut_note`) ; la coupure ou la réouverture d'un connecteur du kit le dit (`connectors.activation.set_org` / `clear_org` : `in_kit`, `kit_note`). **La route du calcul de l'agent (§E10)** : `GET /api/me/agent-toolbox` (`me.agent_toolbox`, REST-only, org consultée par `X-Oto-Org`) rend l'ensemble EXACT des outils visibles de la poignée de main (`compute_hidden_tools` sur l'instance liée), groupés par connecteur avec la provenance, plus les connecteurs installés sans outil visible et la raison (`paused` / `cut` / `no_tools`). Tout compteur d'écran (« connecteurs actifs », « ce qu'il peut faire ») lit cette route au lieu de recalculer ; `available:false` = non dérivable, jamais zéro. Un connecteur activé pour l'org APRÈS le seed arrive dans la library, pas dans la toolbox. Le grain CONNECTEUR `default_hidden` et les flags `OTO_CONNECTOR_SELECTION_*` ont été **retirés** (0050). **Masqués par défaut, grain OUTIL** (`is_default_hidden` = `DEFAULT_HIDDEN_TOOLS` seul : `email_send`, `fr_egapro_declaration`) : self-activables. Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué par un admin (denylist org/équipe ci-dessus) > masqué-par-défaut plateforme > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève (la même capacité sert `/api/me/tools/{name}`, #429). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Sortir un connecteur du départ = ne PAS le mettre dans le socle `default_active` ; un tool isolé = `DEFAULT_HIDDEN_TOOLS`.

> **Un `unselect` qui ne retire rien REFUSE (oto#42, oto-backend#868, 04/09).**
> `connectors.unselect` répondait `ok` avec `removed: false` sur un `DELETE` qui n'avait
> touché aucune ligne — un succès qui n'a rien fait, pire qu'un refus (même patron que
> l'unlink de projet, `d3c5de40`). Il refuse désormais nommément (`connector_not_selected`,
> 404) ; `removed` vaut toujours `True` sur un succès. La cause régulièrement en jeu n'est
> pas propre à ce verbe : `select`/`pause`/`unselect` lisent et écrivent tous sous
> `ctx.org_id or 0`, l'org **active au moment de l'appel** — un membre dont la sélection a
> été posée sous une autre org (legacy `org_id=0` d'avant la suppression du « perso sans
> org », ADR 0030 §8 ; ou une org active qui a changé depuis) ne verra ni ne pourra jamais
> toucher cette ligne-là par ce chemin, quel que soit le nombre de tentatives.

## Actif = utilisable : la sélection suit les clés (22/10/2026)

**La règle.** Un connecteur est ACTIF pour un membre, dans une org, exactement quand une
clé y est utilisable pour lui — la sienne, celle de son équipe active, de l'org, du
tenant ou de la plateforme (`access.status_for` : `mode` ∈ user, group, org, tenant,
platform, over_quota) — ou qu'il n'en demande aucune (`secret_kind` `none`, `settings`).
Rien d'utilisable ⟹ pas actif. Le tableau de bord n'a plus d'interrupteur Actif.
La sélection (`user_selected_connectors`) reste la vérité du montage ; elle est
**réconciliée** depuis l'instantané des clés par `connectors/usable.py` :

- **où** : à la poignée de main (`session_visibility`, juste après le semis), donc pour
  tout membre ET tout run hébergé ; au plus une fois par `TTL` (30 min) par `(sub, org)`
  — marque `connector_selection_reconciled` — et aussitôt après toute écriture du coffre
  qui atteint le membre (`credentials_store._upsert/_delete` efface les marques
  concernées : membre → lui dans l'org, `user` → lui partout, org → tous ses membres,
  équipe/tenant/plateforme → tout le monde) ;
- **quoi** : un connecteur EXPOSÉ et utilisable qui n'est pas actif s'active (provenance
  `cle`, un retrait du membre ne tient plus) ; un connecteur actif sans clé passe en
  pause ; le reste ne bouge pas ;
- **ce qui ne décide rien** (`unknown`, la ligne reste telle quelle) : une étape en
  attente (`pending_action` — un canal hébergé dont le compte n'est pas lié), une entrée
  absente de l'instantané, un connecteur sans outil (une clé de modèle) ;
- **une clé refusée** (`health_ko`) reste utilisable : elle est là pour être réparée,
  l'agent doit voir l'échec plutôt que perdre l'outil ;
- **un seul calcul par rafale** : le tour est PRIS avant le calcul (`claim`, une
  instruction) — au premier déploiement ou après une clé posée, la flotte de runs
  hébergés d'une même personne ne paie qu'un instantané ; un échec rend le tour ;
- ⚠️ **fail-INERTE** : instantané illisible, exception ⟹ rien n'est écrit, la boîte monte
  ce que la table porte. Une passe dont la lecture des équipes échoue ne met RIEN en
  pause (`status_for` retombe sinon sur « aucune équipe » et une clé d'équipe y lirait
  `forbidden`). Reste une fenêtre étroite — l'instantané qui se dégrade APRÈS cette
  lecture — où un connecteur à clé d'équipe peut passer en pause ≤ `TTL`, réactivé à la
  passe suivante.

**Les gestes qui contrediraient la règle sont refusés** : `connectors.pause` et
`connectors.unselect` (REST et `oto_connector op=pause|unselect`) rendent 409
`connector_usable` sur un connecteur utilisable — la pause serait défaite à la session
suivante. Pour l'arrêter, le détenteur retire la clé. Une lecture impossible laisse passer
le geste (courtoisie, pas barrière : la réconciliation suivante dit la règle).

**Ce que ça change au reste** : le kit (`orgs.default_connectors`) et la poussée gardent
leur sens pour un connecteur SANS clé utilisable… qui est mis en pause à la session
suivante ; un connecteur avec clé n'a plus besoin du kit. Les lignes `cle` ne sont jamais
retirées par un retrait du kit (qui ne retire que `kit`). ⚠️ La taille de la boîte croît :
tout l'open data et les six waterfalls (~63 outils, ~143 Ko de schémas sérialisés,
mesuré le 22/10/2026) sont actifs pour tout membre — décision produit assumée.
⚠️ Le run hébergé monte toujours contre l'org MAISON du délégant (§ « La toolbox est celle
de l'org MAISON » ci-dessus) : la réconciliation hérite de ce choix, elle ne le corrige pas.

## Surfaces BÊTA : une population CHOISIE, pas une découvrabilité (2026-09-01)

**Troisième grain de masquage, et il ne se confond avec aucun des deux autres.** Un
`DEFAULT_HIDDEN_TOOLS` est self-activable — n'importe qui le révèle d'un geste ; une
denylist org/équipe est levée par l'override perso positif. Un outil **bêta**, lui, ne
se voit que si un admin a posé l'option `beta` sur l'UTILISATEUR ou sur son ORG
(`oto_admin_set_option`, lue par le seam unique `access.has_option`) : l'utilisateur ne
peut pas se l'accorder, et aucun override perso ne le lève.

`BETA_TOOLS` (source unique, `tool_visibility.py`) porte **deux** familles, entrées pour
des raisons différentes.

**1. Les trois verbes du nouvel univers de contenu** — `oto_node`, `oto_node_rows`,
`oto_node_edit`. Motif : cette surface part de VIDE (la recopie depuis l'ancien monde est
arrêtée, cf. `docs/noeuds.md`) et son contrat est provisoire. La servir à tous, c'est
proposer à chaque agent une lecture qui ne trouve rien et une écriture dont l'utilisateur
ignore la destination.

**2. `oto_resource_v2`, une surface DOUBLÉE — le régime que prend un contrat qu'on
durcit.** C'est le second motif d'entrée dans `BETA_TOOLS`, et il vaut d'être nommé parce
qu'il se réutilisera. Le 2026-09-01, #756 a rendu `resource_type` obligatoire sur
`oto_resource` : correction juste sur le fond — un défaut implicite sur un DISCRIMINANT
fait agir `transfer`/`share` sur une autre ressource que celle visée — mais le champ était
déclaré sans défaut, donc obligatoire sur **toutes** les op, et le journal des appels a
montré de vrais appelants dessus, dont un `op=list` qui serait passé de « fonctionne » à
« refusé » sans préavis. Reverté (#774) avant le tag.

> **Un contrat servi ne se durcit pas en place : il se double.** L'ancienne surface
> continue de servir son défaut — **écrit dans sa description servie** comme défaut connu,
> avec le nom de sa remplaçante — et la nouvelle exige le champ. La bêta lui donne une
> population choisie pendant que les appelants migrent, **sans date-couperet** : c'est
> l'inverse d'un alias déprécié (`docs/alias-deprecies.md`), où l'ancien nom part à une
> date écrite. Ici l'ancien contrat reste tant que quelqu'un s'en sert.

**Une sortie de la bêta.** `oto_fleet` y était entré le 2026-09-01 (surface neuve,
contrat provisoire) ; il en est SORTI le 2026-09-24, quand les agents hébergés se sont
ouverts à toute org. Sa porte n'était pas un contrat provisoire mais un coût : ses
déroulés pouvaient tourner sur notre clé de modèle. Ce qui la remplace — modèle
obligatoire, clé de modèle de l'org — vit dans `docs/runner-et-automatisations.md`.
`oto_recipe` y était entré à sa naissance (contrat provisoire, refusée dans un agent
hébergé) ; il en est SORTI le 2026-10-19 : le jeton d'un travail porte désormais la liste
d'outils de son déclencheur (`outils_du_travail.py`), un agent hébergé lance une recette
comme tout client (`docs/recettes.md`), et la page publique d'aide la présente comme
ouverte à tous.

⚠️ **N'entrent dans `BETA_TOOLS` que des noms NEUFS.** Le bloc masque fail-closed : y
poser le nom d'une surface vivante la retirerait d'un coup à tous les comptes sans
l'option — la rupture de #756 en pire, parce que silencieuse. Cliquet :
`tests/test_resources_deux_surfaces.py` garde que `oto_resource` n'y est pas, et fige le
schéma d'entrée servi de l'héritée (`tests/resources_input_legacy.json`).

⚠️ **Ils étaient exposés à TOUT LE MONDE depuis leur création** — mesuré le 2026-09-01,
aucun gate, et c'est l'inverse de ce qu'on croyait (on les pensait invisibles). Zéro
appel MCP en 30 jours, ce qui explique que personne ne l'ait vu.

⚠️ **FAIL-CLOSED, à contre-courant de tous les autres blocs du calcul.** Eux sont
fail-open parce qu'un hoquet de base ne doit pas priver quelqu'un de ses outils : le
pire y est une toolbox trop pauvre pendant une seconde. Ici le pire est l'inverse — une
surface non finie qui réapparaît pour tout le monde, en silence. Ne pas proposer une
bêta n'a jamais bloqué personne.

⚠️ **Et ce n'est pas une barrière de sécurité** (ADR 0031), comme aucune règle de ce
module : un compte qui connaît le nom peut appeler le verbe, ou le matérialiser par
`oto_call`. Ce qui protège est l'autorisation de la capacité — membre de l'org pour
lire, palier du propriétaire pour écrire. Ce gate décide qui se les voit PROPOSER.

⚠️ **La face REST n'est pas gatée**, écart assumé : le dashboard qui construit ce nouvel
univers la consomme aujourd'hui, et la couper arrêterait le travail qui justifie la
surface. Il se referme quand la surface cesse d'être provisoire. Sur une surface
**doublée**, l'écart est moins gênant : le chemin `/api/resources/v2` est lui-même
l'opt-in, personne n'y arrive par accident — au contraire d'un verbe qui apparaîtrait
dans une toolbox sans avoir été demandé.

## Niveau d'accès d'un connecteur : la couche `access_level` (14/10/2026)

Un connecteur OAuth qui déclare des niveaux (`docs/connector-model.md` §Niveau d'accès)
peut être restreint par l'org, puis par l'équipe active (`read` pour `zoho`, `zohodesk`).
**Sous un niveau autre que `full`, et seulement là** :

- **couche `access_level`** de `session_visibility` (`access_level_gate.hidden_tools`) : un
  outil que le niveau ne couvre EN RIEN sort de la boîte — un outil simple dont il n'a pas
  les scopes, un outil multiplexé dont aucune op n'est couverte, un outil du connecteur
  sans déclaration. Fail-open comme l'activation ; ce n'est **pas** une couche
  d'installation (`COUCHES_INSTALLABLES`) : le catalogue le dit `not_exposed` ;
- **schéma servi par appelant** : `CallContextMiddleware.on_list_tools` retire de l'enum
  `op` les ops non couvertes (`restricted_ops` + `restrict_schema`) ; un défaut qui n'est
  plus couvert est retiré et `op` devient requis. Une liste sans outil multiplexé d'un
  connecteur à niveaux ne lit rien (ni contexte ni réglage) ;
- **garde d'appel fail-closed** `access_level_denied` (`access_level_gate.require_covered`),
  aux DEUX sites, juste après la garde d'activation et sur le même contexte : le middleware
  de contexte (appel direct) et `tools/meta.executer_cible` (`oto_call`, recettes). Elle
  lit les arguments : une op non couverte est refusée, un appel sans `op` est jugé sur l'op
  par défaut du schéma, une op ou un outil non déclaré est refusé. Le refus nomme le
  niveau, l'outil, l'op, les scopes exigés, qui l'a posé et le geste qui le lève.

Au niveau `full` — le défaut — rien ne change : aucun outil masqué, aucun octet de schéma
touché, aucune garde qui morde. Un connecteur sans niveaux ne coûte aucune lecture.
`oto_tool_schema` sert le schéma brut (toutes les ops) : la garde d'appel reste la barrière.
Le réglage se pose par `oto_connector_access_level` (`refresh_visibility` : la session
courante se recharge, celles des autres membres à leur prochaine session — la garde, elle,
vaut dès leur prochain appel).

## Lire ou simuler, agir : deux outils par connecteur (17/10/2026)

Un client MCP autorise des OUTILS, jamais des arguments : « toujours autoriser » un outil
autorise toutes ses ops. Décision d'Alexis, pour la sécurité : un connecteur sert **une
paire**, `<ns>` et `<ns>_act`.

- `<ns>` lit ou simule. Annoté `lecture.LECTURE` s'il ne fait que lire, `lecture.SIMULATION`
  (`readOnlyHint=False`, `destructiveHint=False`, `openWorldHint=False`) s'il prépare
  aussi ce que personne ne voit et que rien ne détruit — un brouillon.
- `<ns>_act` engage ou détruit : envoyer, mettre à la corbeille, archiver, déplacer,
  supprimer, publier. Annoté `lecture.ACTE` (`destructiveHint` et `openWorldHint` écrits,
  pas déduits) : c'est l'outil que le client fait confirmer.

Garde : `tests/test_convention_act.py` — une op à effet hors d'un `*_act` rougit, un
`*_act` sans son annotation aussi ; les connecteurs pas encore migrés sont dans sa liste
fermée `A_MIGRER`, qui ne peut que rétrécir. Les anciens noms d'une migration refusent en
nommant l'appel à faire (`outils_retires.RENOMMAGES`) ; les listes d'outils
enregistrées (presets de projet, flottes, déclencheurs, masquages) sont réécrites par
migration : autoriser reste étroit, masquer reste large (décision d'Alexis du 21/10/2026) : une liste qui autorisait un ancien lecteur ne reçoit que le nouveau lecteur, jamais `<ns>_act` — même si l'ancien lecteur envoyait encore en transition ; un ancien outil d'envoi dédié ne donne `<ns>_act` que si celui-ci ne fait rien de plus que lui — sinon RIEN : `linkedin_unipile_send`, `_publish` et `_invite` ne donnent pas `linkedin_unipile_act`, qui publie, invite et envoie ; on l'ajoute explicitement, le refus de l'ancien nom le dit ; une liste qui masquait masque tout ce que l'ancien outil faisait (le premier lot, joué avant cette règle, avait donné tous les
remplaçants aux deux sens). Premier lot : Gmail et Outlook (17/10/2026). Deuxième lot, les messageries
(21/10/2026) : `whatsapp`, `telegram`, `instagram`, `teams`, `chat` (Google Chat) et la
messagerie LinkedIn (`linkedin_unipile` / `linkedin_unipile_act`, le reste du connecteur
restant à part) ; leurs anciennes ops d'envoi en transition disparaissent avec les anciens
noms. Troisième lot, les envois (21/10/2026) : ce qui atteint un tiers depuis un lecteur
ou un outil d'envoi dédié passe à `<ns>_act` (LinkedIn — publication, invitations, gestes
sur un membre —, Apollo, Lightfield, Luma, Pennylane, SignWell, Productlane, Ahrefs,
lemlist) ; le lecteur garde son nom et refuse l'ancienne op en nommant le nouvel appel
(`outils_retires.OPS_DEPLACEES`). `lemlist_act` (ex-`lemlist_inbox_send`) reste masqué par
défaut.

## Méta-tools et `PROTECTED_TOOLS`

Méta-tools exposés : `oto_call` et `oto_tool_schema` (`tools/meta.py`, MCP-only par nature) ; la toolbox du membre est une capacité (`capabilities/tools_me.py`, #429) — `oto_disable_tool`/`oto_enable_tool` sont la face MCP des mêmes capacités que `POST|DELETE /api/me/tools/{name}`, `oto_list_my_tools` une capacité MCP-only (`me.tools.search`) sur le noyau `tools/catalogue.py`, que la grille du dashboard (`GET /api/me/tools`) ne partage pas : elle pose une autre question. **`PROTECTED_TOOLS`** (`tool_visibility.py`, source unique) = quatre familles jamais masquables (default-hidden inclus) **ni désactivables** : méta-toolset + identité (`oto_list_my_tools`/`oto_enable_tool`/`oto_whoami`/`oto_profile`), échappatoires de contexte (`oto_use_org`/`oto_clear_org`/`oto_list_orgs`/`oto_use_group`/`oto_clear_group` — anti-lockout, vécu Sentry 2026-06-30), boucle d'usage (`feedback`/`run_start`/`run_finish` — mandatés par les instructions plateforme ADR 0017 : un toggle qui les masque rend le gap invisible), **dispatch universel** (`oto_call`/`oto_tool_schema` — ADR 0036 : appeler par son nom un outil NON listé (FOD, connecteur non installé) le temps d'un appel, sans muter la visibilité ; exécution par `Tool.run` HORS middleware → gates call-time intactes + rédaction ré-appliquée via `redaction.py`. Un connecteur COUPÉ — master plateforme, override d'org, coupure d'équipe — n'est pas « non listé » : refus `connector_disabled` sur les deux chemins, joué par `connectors/activation_gate.py` après la pose du contexte d'appel, #1064). Garde des deux faces, désormais un seul handler : masquer un outil protégé → 400 `protected_tool`, un nom inconnu → 404 `unknown_tool` ; `GET /api/me/tools` expose `protected:bool` (toggle inerte dashboard).

## Outils admin : c'est l'AUTORISATION qui masque, pas le nom (28/08)

`compute_hidden_tools` masquait toute la famille `oto_admin_*` à qui n'était pas **super**
admin — un test de préfixe, doublé d'un test de rôle trop haut. Deux gestes légitimes s'y
perdaient : `oto_admin_org_member op=remove`, dont l'autz est `ORG_ADMIN_OF("org_id")` et
dont le dashboard se sert depuis juin, était **injoignable depuis un agent** pour le
responsable d'organisation à qui elle est destinée (#471) ; et un opérateur plateforme
`admin` — le palier pour lequel `PLATFORM_ADMIN` a été écrit — ne voyait aucun outil admin
non plus. ⚠️ **Le masquage bloque aussi l'appel** : fastmcp filtre `get_tool` comme
`list_tools`, l'outil n'était donc pas seulement discret, il n'existait pas pour la session.

Le masquage se **dérive maintenant de l'autz déclarée** (`_authz.platform_floor`), source
unique — un nom ne porte pas un droit. Trois crans ordonnés : `None` (l'accès dépend d'une
CIBLE que le handshake ne connaît pas — une org, une équipe, une ressource), `operator`
(`PLATFORM_ADMIN`), `super` (`SUPER_ADMIN`). Le plancher d'un combinateur `ADMIN_BY_OP`/
`BY_OP` = **le plus BAS de ses branches** : l'outil sert au moins une fois, ses autres ops
continuent de répondre 403 à l'appel. `oto_admin_org_member` et `oto_admin_doctrine` sont
donc visibles de tous ; les douze autres restent au palier plateforme.

⚠️ Le défaut est `None`, soit **fail-open côté visibilité** — une règle future qui oublie
de se déclarer rend son outil visible, jamais appelable. C'est le bon sens du fail : la
visibilité est une gouvernance, pas une barrière (ADR 0031), donc l'erreur coûteuse est de
CACHER un geste légitime, pas d'en montrer un de trop. Le **repli par le nom** subsiste
pour les outils `oto_admin_*` écrits à la main dont la garde vit dans le handler :
rien n'y est déclaré, rien n'est dérivable, le préfixe reste le seul indice — au cran
`operator`. ⚠️ **Son dernier occupant, `oto_admin_refresh_mount`, est parti le
2026-09-09** avec la fédération MCP (**ADR 0069**) : le repli n'a plus de porteur
aujourd'hui, et il est **gardé quand même** — il vaut pour le prochain outil écrit à la
main, et le retirer ferait de son absence une règle qu'on ne redécouvrirait qu'en la
violant. Garde : `tests/test_admin_tool_visibility_by_authz.py`, qui fige aussi
l'inventaire des planchers (poser un `oto_admin_*` neuf est une décision, pas un effet de bord).

## Refresh à chaud de la toolbox

**Refresh à chaud de la toolbox sur bascule de profil** : une capacité qui change le profil de visibilité déclare `refresh_visibility=True` (`Capability`) ; l'adaptateur MCP (`capabilities/_mcp_adapter.py`) rejoue alors `apply_session_visibility(reset=True)` sur la session **courante** après le handler → `tools/list_changed` live. Posé sur `org.use_org`/`org.clear`/`org.create`/`org.set_home` + `group.use`/`group.clear`/`group.set_home`. Donc **`oto_use_org <org>` recharge la toolbox dans la conversation en cours** (les credentials, eux, basculent déjà — `resolve_api_key` relit l'org **via le seam `current_org`** à chaque appel, cf. §ADR 0023 ci-dessous).

**Limite connue** : ça ne vaut QUE pour la face MCP (même session). Un toggle/bascule via **REST** (dashboard) passe par une connexion séparée → ne notifie pas une conversation Claude déjà ouverte (visible à la prochaine session). Pousser dashboard→session MCP demanderait un registre `sub → sessions actives` + push hors-requête (non fait).
