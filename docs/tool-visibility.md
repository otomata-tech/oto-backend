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
par `oto_call` avant de conclure. Le texte est isolé dans `meta.hint_zero_resultat()`
pour être éprouvé seul, et une sonde tient que la liste consulte bien le seam : sans
elle, un remaniement le reperdrait en silence et le signal reviendrait une troisième
fois. **La leçon générale, elle, est dans `docs/conventions.md` : un correctif de cette
classe doit être posé là où l'agent REGARDE, pas là où le défaut a été compris.**

## Source de vérité + retrait des presets

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). **Les presets de tools (snapshots nommés + baselines ALLOWLIST org/équipe) ont été retirés le 2026-07-03** (commit `3951a57` — masquaient tout ce qui n'était pas listé, lourd à maintenir : un tool ajouté après coup arrivait masqué par défaut pour toute baseline posée).

## Denylist org/équipe

**Remplacés depuis par un DENYLIST org/équipe** (`capabilities/tools_visibility.py`) : un org_admin/chef d'équipe masque des tools SPÉCIFIQUES par défaut pour son org/équipe (`org_disabled_tools`/`group_disabled_tools`) — le reste, y compris les tools futurs, reste visible par défaut. Additif entre paliers (union à la lecture) : une équipe ne peut jamais RÉVÉLER un tool que l'org a masqué. Gouvernance de visibilité, PAS une barrière de sécurité (ADR 0031, même esprit que `DEFAULT_HIDDEN_TOOLS`) : `user_enabled_tools` (override perso positif) lève TOUJOURS ce masquage, même échappatoire qu'un masqué-par-défaut plateforme. Calculé fail-open **indépendamment par palier** dans `session_visibility.compute_hidden_tools` (`access.org_admin_hidden_tools`/`group_admin_hidden_tools`) — un hoquet DB sur l'équipe ne prive pas l'org de son denylist. Surfaces : MCP + REST `GET/PUT/DELETE /api/{orgs,groups}/{id}/tools/{name}?/hidden`.

## Sélection par membre — régime NOMINAL (ADR 0019/0050)

**Sélection par membre = régime NOMINAL « non-sélectionné = masqué » (ADR 0019/0050).** La toolbox d'un membre = les connecteurs qu'il a **installés** (`user_selected_connectors`, per (sub, org)). Au premier profil d'un (sub, org), `session_visibility` seed le socle `providers.DEFAULT_ACTIVE_CONNECTORS` ∩ exposé — **VIDE depuis le 16/07** (décision produit : un nouveau compte démarre SANS connecteurs installés ; l'agent guide depuis les tools spine — `oto_connector` op=list/select, `oto_call` — et le catalogue injecté au bloc A) ; tout l'exposé = library installable (capacité `connectors.select`, dashboard). Les pairs pré-0050 ont été backfillés une fois avec leur visible d'alors (`connector_selection.backfill_preexisting`, sentinelle `#adr0050-backfill`). Un connecteur activé pour l'org APRÈS le seed arrive dans la library, pas dans la toolbox. Le grain CONNECTEUR `default_hidden` et les flags `OTO_CONNECTOR_SELECTION_*` ont été **retirés** (0050). **Masqués par défaut, grain OUTIL** (`is_default_hidden` = `DEFAULT_HIDDEN_TOOLS` seul : `email_send`, `fr_egapro_declaration`) : self-activables. Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué par un admin (denylist org/équipe ci-dessus) > masqué-par-défaut plateforme > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève (même logique côté REST `/api/me/tools/{name}`). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Sortir un connecteur du départ = ne PAS le mettre dans le socle `default_active` ; un tool isolé = `DEFAULT_HIDDEN_TOOLS`.

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

## Méta-tools et `PROTECTED_TOOLS`

Méta-tools exposés (`tools/meta.py`) : `oto_list_my_tools`, `oto_disable_tool`, `oto_enable_tool`, `oto_call`, `oto_tool_schema`. **`PROTECTED_TOOLS`** (`tool_visibility.py`, source unique) = quatre familles jamais masquables (default-hidden inclus) **ni désactivables** : méta-toolset + identité (`oto_list_my_tools`/`oto_enable_tool`/`oto_whoami`/`oto_profile`), échappatoires de contexte (`oto_use_org`/`oto_clear_org`/`oto_list_orgs`/`oto_use_group`/`oto_clear_group` — anti-lockout, vécu Sentry 2026-06-30), boucle d'usage (`feedback`/`run_start`/`run_finish` — mandatés par les instructions plateforme ADR 0017 : un toggle qui les masque rend le gap invisible), **dispatch universel** (`oto_call`/`oto_tool_schema` — ADR 0036 : appeler par son nom un outil NON listé (FOD, connecteur non activé) le temps d'un appel, sans muter la visibilité ; exécution par `Tool.run` HORS middleware → gates call-time intactes + rédaction ré-appliquée via `redaction.py`). Garde des deux faces (2026-07-02) : `oto_disable_tool` refuse, `POST /api/me/tools/{name}` → 400 `protected_tool` ; `GET /api/me/tools` expose `protected:bool` (toggle inerte dashboard).

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
pour les outils écrits à la main dont la garde vit dans le handler (`oto_admin_refresh_mount`) :
rien n'y est déclaré, rien n'est dérivable, le préfixe reste le seul indice — au cran
`operator`. Garde : `tests/test_admin_tool_visibility_by_authz.py`, qui fige aussi
l'inventaire des planchers (poser un `oto_admin_*` neuf est une décision, pas un effet de bord).

## Refresh à chaud de la toolbox

**Refresh à chaud de la toolbox sur bascule de profil** : une capacité qui change le profil de visibilité déclare `refresh_visibility=True` (`Capability`) ; l'adaptateur MCP (`capabilities/_mcp_adapter.py`) rejoue alors `apply_session_visibility(reset=True)` sur la session **courante** après le handler → `tools/list_changed` live. Posé sur `org.use_org`/`org.clear`/`org.create`/`org.set_home` + `group.use`/`group.clear`/`group.set_home`. Donc **`oto_use_org <org>` recharge la toolbox dans la conversation en cours** (les credentials, eux, basculent déjà — `resolve_api_key` relit l'org **via le seam `current_org`** à chaque appel, cf. §ADR 0023 ci-dessous).

**Limite connue** : ça ne vaut QUE pour la face MCP (même session). Un toggle/bascule via **REST** (dashboard) passe par une connexion séparée → ne notifie pas une conversation Claude déjà ouverte (visible à la prochaine session). Pousser dashboard→session MCP demanderait un registre `sub → sessions actives` + push hors-requête (non fait).
