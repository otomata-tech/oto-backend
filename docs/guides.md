---
title: Guides & instructions d'org
type: reference
description: >-
  Référence du mécanisme de guide oto-backend : prose opératoire métier par org,
  structurée en skills identifiés par slug et versionnés dans org_instructions +
  org_instruction_revisions. Détaille la surface (consolidée en `oto_procedure`, ADR 0047 — op=get sans slug =
  call de début de session renvoyant base + index, avec slug = skill nommé ;
  op=set/list/delete), l'autz conditionnelle
  org_admin self-service vs platform_admin cross-org, le versioning append-only avec
  revert via from_version, et les gotchas (verrou advisory par org/slug, pas de cache,
  pas d'instruction par namespace d'outil). Aligne sur ADR 0006 (harnais sans état).
adr:
  - "0006"
---

# Guides & instructions d'org

> ⚠️ **« doctrine » = « guide » depuis le 28/08/2026** (#519) : le mot a disparu de l'interne du backend (modules, symboles, prose). Les noms SERVIS qui le portent encore se **doublent** au lot B — le nouveau nom naît, l'ancien reste servi avec une date de retrait écrite (premier tag à partir du 29/10/2026 ; retrait = lot D, #526). **Table unique de ces alias : [`alias-deprecies.md`](alias-deprecies.md).** Déjà fait : l'outil s'appelle `oto_admin_guide` (son ancien nom répond encore).

Prose opératoire métier (workflows validés, règles, vocabulaire) pour les users qui pilotent
oto **sans produit applicatif dédié** (ex. un process avoir compta client
GoCardless → Pennylane → back-office, piloté directement depuis Claude sur un sous-ensemble
de tools). oto est la maison naturelle de cette prose faute de produit. Aligné
**ADR 0006** (harnais-vs-substrat, repo public `otomata-tech/oto`) : une org oto + sa
guide = un **harnais sans état** (étage zéro) ; le jour où un workflow doit persister un
pipeline/des statuts, il graduate en harnais à part.

**Modèle = skills, à la Claude Code.** Une org possède des **instructions markdown**
identifiées par `slug`, chacune versionnée :
- Le **guide de base** (slug réservé **interne** `BASE_SLUG`, jamais vu de l'user) est servi
  d'office — accédée via `oto_procedure(op='get')` **sans slug**.
- Les autres slugs = des **skills** chargés à la demande (progressive disclosure) : la
  guide de base ne porte que l'**index** (slug + titre + quand-l'utiliser), le détail
  se charge au besoin.

**Surface = 4 tools** (refacto 2026-06-18, ex-11 ; « moins d'outils, plus d'args »). Un `org_id`
optionnel **fond membre↔platform-admin** : absent = ton **org active** ; présent = une **autre org**
par id (réservé platform_admin). Autz conditionnelle dans `tools/orgs.py`
(`_resolve_org_read`/`_resolve_org_write`).
- **Lecture** : `oto_procedure(op='get'[, slug, scope, version, with_history])` — sans `slug` =
  `{doctrine, group_doctrine, doctrines[]}` (base org + base groupe + index), le call de **DÉBUT DE
  SESSION** ; avec `slug` = le markdown d'un guide nommé. `oto_procedure(op='list'[, query,
  scope])` = catalogue/recherche. Scopés à l'**org active** (+ groupe actif) — servis aux seuls
  membres. **Vide sans erreur** si pas d'org active (`_SERVER_INSTRUCTIONS` invite à `oto_procedure(op='get')`).
- **Écriture** : `oto_procedure(op='set'[, body_md, slug, scope, org, group, title, desc,
  from_version])` (base = slug omis ; nommée sinon ; `from_version` = revert) +
  `oto_procedure(op='delete', slug[, scope, org, group])`. Autz **par PALIER** (`scope`,
  #681 — 31/08/2026) :
  - `scope='org'` (défaut) : `org` absent → org active, **org_admin** ; présent → autre org,
    **platform_admin** (l'opérateur provisionne n'importe quelle org) ;
  - `scope='group'` : `group` absent → équipe active, présent → l'équipe nommée ; **chef
    d'équipe** requis (escalade `roles.can_admin_group` : org_admin parent, platform_admin).

  ⚠️ **Pourquoi ce second palier existe** : celui qui DÉROULE une procédure est un opérateur
  métier, et le seul qui pouvait l'écrire était un administrateur d'org. Améliorer son propre
  mode d'emploi supposait donc les clés de toute l'organisation — membres, connecteurs,
  secrets — que personne n'accorde pour ça ; la boucle d'auto-amélioration que la procédure
  promet ne se fermait jamais.

  ⚠️ **La garde suit le VERBE, pas la surface** (corrigé le 01/09/2026, avant fusion de
  #695) : au palier équipe, `set` demande d'être **membre** de l'équipe, `delete` d'en être
  le **chef**. La première rédaction du lot gardait l'écriture sur « chef d'équipe » et ça
  s'est payé tout de suite : pour laisser une opératrice annoter le mode d'emploi qu'elle
  déroulait, il a fallu la faire cheffe de son équipe — un rôle qui emporte les **clés
  partagées** de l'équipe. Une garde d'écriture trop grossière force une élévation de droits
  dans un domaine sans rapport. Ce qui rend l'ouverture tenable est que l'écriture est
  **réversible** (une version de plus, `from_version` restaure) alors que la suppression
  emporte l'historique sans corbeille.

  Les faces REST restent **une route par palier** : `/api/me/instructions*` (org_admin de
  l'org active) et `/api/groups/{id}/instructions*` (membre pour écrire et restaurer, chef
  pour supprimer — **même partage que la console**, sinon « qui peut annoter » deviendrait
  une propriété du transport). `scope`/`group` sont des axes de la CONSOLE MCP seulement —
  les publier dans le corps d'une route qui les refuserait décrirait une porte qui n'existe
  pas.

  **Deux gardes ⟹ deux droits SERVIS** (01/09/2026, suite de #695). `can_edit` est resté
  le droit d'ADMINISTRER (readme d'équipe, membres, secrets, suppression) et rendait
  `false` à une membre qui avait pourtant le droit d'écrire : une porte fermée à tort.
  L'élargir en aurait ouvert une autre — le bouton de suppression, que le serveur refuse.
  Le sens s'est donc **dédoublé** : `can_write_instructions` (écrire/restaurer) et
  `can_delete_instructions` (supprimer) sont servis **à côté** de `can_edit`, dont ni la
  valeur ni le sens ne bougent. Mêmes deux noms sur les **deux** bundles de la famille
  (`GET /api/groups/{id}/instructions` et `GET /api/me/instructions`) : les servir d'un
  seul côté remettrait « qui peut annoter » dans les mains de la page.

  ⚠️ **Le drapeau et le refus sont la MÊME fonction.** Chaque droit annoncé NOMME la
  capacité dont il rend la règle (`_DROITS_SERVIS`), et le bundle exécute cette règle
  d'autz déclarée — `_authz.capacite_autorise`, qui ne lance jamais le handler. Aucun
  critère n'est recopié dans le handler du bundle : déplacer une garde déplace son
  drapeau avec elle. C'est le défaut d'origine, et il ne se reconstruit pas un cran plus
  loin. Cliquets : `tests/test_droits_procedure_servis_695.py` (le drapeau vaut ce que
  fait la règle, pour chaque acteur ; et chaque nom de drapeau doit nommer la capacité
  de SON verbe — sans quoi une table qui se trompe de capacité resterait cohérente avec
  elle-même).
- **Versioning** : chaque écriture incrémente `version` (sur le courant) et archive un snapshot
  append-only. Revert = re-poser le corps d'une version → nouvelle version (jamais d'effacement
  d'historique sauf `delete`).
- **Store** : `org_instructions(owner_type, owner_id, slug, org_id, title, description,
  body_md, slots, version, set_by, archived_at, created_at, updated_at)` +
  `org_instruction_revisions(owner_type, owner_id, slug, version PK, …)` (`db/schema/procedures.py`).
  ⚠️ **UN seul jeu de fonctions**, keyé sur `(owner_type, owner_id)` — la clé d'unicité que la
  table porte, sur la table ET sur ses révisions : `org_store.<fn>('org'|'group', id, …)`
  (`org_store/instructions.py`). `org_id` reste la colonne dénormalisée de l'org PARENTE (FK,
  NOT NULL, cascade de suppression) : org et équipe en ont toutes deux une.

  ⚠️ **Il en a existé DEUX jusqu'au 31/08/2026** (#681) : celui-ci filtrait `owner_type='org'`
  en dur, `group_store` filtrait `owner_type='group'` en dur, sur la MÊME table — et ils avaient
  déjà divergé (le palier équipe écrivait `slots='[]'` en dur, ne relisait pas les slots,
  ignorait l'archivage). Ajouter un palier par la même méthode en aurait fait un troisième :
  **le propriétaire est une DIMENSION, pas trois cas particuliers.** Le palier `user` reste
  fermé (`OWNER_TYPES`) tant que `org_id` est NOT NULL — phase 2 du même lot.

  **En clair** (prose, pas un credential → hors coffre chiffré). **Pas de cache** : lecture DB
  à l'appel. Écriture sérialisée par `(owner_type, owner_id, slug)` via verrou advisory.
- **Pas d'instruction par namespace d'outil** : un gotcha d'outil est vrai pour tout le monde et
  évolue avec le code du connecteur → sa place reste le repo (docstring, `_SERVER_INSTRUCTIONS`),
  versionné avec l'outil.

## La forme d'une procédure : digest d'ouverture + schéma

**Toute procédure s'ouvre sur son digest d'auto-amélioration** — `> **Self-improvement
digest** — …` en premier bloc : ce que le dernier déroulé a appris et ce qui a été
corrigé, DATÉ ; une procédure qui n'a jamais tourné le dit en une phrase. C'est le seul
bloc d'une procédure où un fait daté est à sa place. ⚠️ **Jamais fabriqué** : sourcé sur
le journal des runs, sur le relevé daté que le corps porte déjà, ou rien — un digest
décoratif est pire que pas de digest, il se lit comme une preuve. `procedure_digest`
garde la seule chose qu'un serveur peut voir (le bloc est-il là, ET en tête) et rend
`digest_warning`, même régime non bloquant que le reste.

⚠️ **La PLACE du digest vient du rendu** : la page d'un process retire un H1 de tête qui
répète le nom de la procédure (`stripLeadingTitleHeading`) et affiche le sien. Le digest
se pose donc SOUS ce H1 quand il existe (au-dessus, le titre resterait orphelin en
milieu de page), et en tout premier quand le corps n'en a pas (`## Goal`…).

## Le schéma est une section requise (front tiers, issue #108)

Une procédure embarque un **dessin** de son process, et ce n'est pas une illustration :
le front en fait la **vue par défaut** de la page de la procédure — une procédure sans
dessin s'y affiche en état vide. Emplacement fixe : juste après le tableau « At a
glance » (ou après l'intro s'il n'y en a pas), **avant le premier titre de phase**.

⚠️ **RIEN entre le tableau et le dessin**, et c'est encore le rendu qui commande : quand
le corps dessine, la page retire le titre « At a glance » ET son tableau
(`stripDiagramSummary` — les deux disent la même chose, le dessin le dit mieux ; le
tableau reste dans le CORPS, que l'agent exécutant lit). Ce qui traînait entre les deux
se retrouve donc orphelin juste au-dessus du dessin. Une note qui explique le TABLEAU
passe au-dessus de lui ; ce qui explique le DESSIN va directement dessous.

⚠️ **La grammaire du dessin est un CONTRAT, pas un style.** Le bloc n'est pas
typographié tel quel : il est **reparsé en graphe** (`src/lib/ascii-diagram.ts` côté
front) puis redessiné en cartes. Tout ce que la grammaire ne couvre pas est *refusé* —
le parseur préfère refuser plutôt que dessiner faux — et retombe en caractères bruts.
Le front ne regarde qu'**UN** bloc fencé **non tagué** : un dessin dans un ```text ne
sera jamais rendu. Le guide porte aussi les bornes de **densité** (titre ~40 c.,
détail UNE phrase de ~80 c. — ~60 c. pour deux étapes côte à côte, raison de sortie ~35 c.,
note de sortie latérale ~50 c., noms d'outils en note de marge et jamais dans le détail) :
le texte est REFLOWÉ à l'affichage, donc la largeur de la boîte n'est pas la borne — la
carte rendue l'est. La grammaire complète + un exemple qui rend vivent dans le guide
plateforme **`procedure-flowchart`** (`oto_mcp/guides/procedure-flowchart.md`), cité par
le socle injecté à chaque session (`instructions._SECRET_SAUCE`) et par la description
de `oto_procedure op=set`.

Côté serveur, `procedure_diagram.diagram_check` ajoute un **`diagram_warning`** au
retour d'écriture (org **et** équipe), dans le même régime non bloquant que
`unresolved_tools` / `slot_warnings` (ADR 0014/0035) : la procédure est enregistrée, sa
page se rendra vide. ⚠️ Le check est **volontairement grossier** — il porte les deux
seuils du `isDrawing` du front (≥ 3 lignes portant un glyphe, ≥ 20 glyphes au total) et
rien d'autre. Rejouer la grammaire ici fabriquerait **deux vérités** qui divergeraient au
premier changement de rendu : ce module répond « l'auteur a-t-il dessiné ? », jamais « le
dessin est-il valide ? ». Seul le rendu de la page tranche.

⚠️ Un **refus** aurait cassé toute réécriture des ~14 procédures vivantes qui n'avaient
pas de dessin — et le premier effet d'une garde bloquante aurait été qu'on cesse
d'écrire des procédures.

## Renommer un outil = migrer les procédures

Une procédure référence ses outils par `<tool:slug>` (ADR 0014), et ces refs vivent **en DB, par
org** — hors du repo. Un renommage d'outil est donc un breaking qui traverse le **code ET les
données**, dont le CI ne voit que la moitié : `test_tools_client_methods_exist` garde le skew
tool↔oto-core, `connector_docs/<nom>.md` se relit en PR, mais **rien ne lit `org_instructions`**. Une
suite verte ne dit donc rien de l'état des procédures.

Vécu le 2026-07-31 (consolidation pennylane 25→9 outils, v1.38.0, ADR 0047 étendu aux
connecteurs) : `rapprochement-pennylane` (org maison, qui arme une routine planifiée quotidienne) et
`agent-avoirs-compta` (une org cliente, agent sous supervision) sont parties **en prod** avec
respectivement 2 et 10 refs mortes, réparées seulement après coup.

Le détecteur, lui, existe déjà : `tool_registry.manifest_for(body_md)` rend
`referenced_tools[].status` et `unresolved_tools` — c'est ce que `oto_procedure(op='get')` et le
retour d'`op='set'` affichent. La migration est donc mécanique : balayer les orgs, réécrire le
corps, vérifier `unresolved_tools == []`. ⚠️ Vérifier contre le serveur qui porte DÉJÀ la nouvelle
surface — tant que le tag n'est pas en prod, les anciens noms y résolvent encore et le contrôle
est faussement vert. **Aucun garde-fou automatique à ce jour** : la migration reste à la charge
de qui renomme.

⚠️ **À ne pas confondre avec le préfixe d'outils d'un tenant** (`tenants.tool_prefix`,
`oto_mcp/tool_alias.py`) : celui-là n'est PAS un renommage. C'est une traduction posée au bord du
protocole — `oto_doc` devient `acme_doc` dans le `tools/list` servi, et redevient `oto_doc`
avant que quoi que ce soit d'autre ne le lise. Les refs `<tool:slug>` restent donc écrites en
canonique, continuent de résoudre, et **il n'y a rien à migrer**. Les deux formes sont d'ailleurs
acceptées à l'appel, précisément pour que la prose déjà écrite aboutisse.

## Détail accumulé (migré de la carte)

**Livraison au LLM = injection, plus un appel d'outil (otomata-private#49 puis #50, amende ADR 0014).**
Le canal de bootstrap = les `instructions` du `initialize` (FastMCP les relit par
session ; Claude rehandshake par conversation). ⚠️ **Cru « fiable » jusqu'au 2026-09-01,
ce canal ne l'est PAS** (#478, mesuré) : Claude Code coupe l'artefact composé à
**2 048 caractères** et claude.ai ne le transmet pas au modèle. Le bloc A est depuis un
**socle-résumé ≤ 2 000 c.** (budget cassant en CI, `tests/test_instructions_budget.py`)
qui pointe la version intégrale — le **guide plateforme `notice`**
(`oto_mcp/guides/notice.md`) — et `oto_context` ; les couches suivantes (catalogue,
bloc C) restent composées, mais seuls les clients qui ne tronquent pas les reçoivent.
`DynamicInstructionsMiddleware.on_initialize`
(`middleware/dynamic_instructions.py`) **remplace** `result.instructions` par `instructions.compose_session(sub, org_id)`
— un **artefact composé de 2 blocs** (`instructions.py`, #50 ; l'ex-bloc B onboarding a été
retiré le 2026-07-01 — l'onboarding est un projet, ADR 0032 §7) :
- **bloc A « secret sauce »** (posture + boucle d'usage + **catalogue de namespaces** dérivé) —
  prose en DB `platform_instructions['secret_sauce']`, éditable admin plateforme, **inviolable par
  l'org**, toujours injecté (seedé depuis la constante = fallback) ; le catalogue est appendé à la composition ;
- **bloc C « contexte dynamique »** par-(sub, org) — section de contexte résolu (org / équipe /
  connecteurs actifs / N derniers projets / derniers déroulés via `db.recent_runs` / fiche profil
  « situation avec oto » de l'user) + **agent readme cumulés** org → équipe active → user
  (`_format_org_readme`/`_format_group_readme`/`_format_user_readme`), chacun avec substitution
  `{{org}}`/`{{user}}`/`{{équipe}}`/`{{connecteurs_actifs}}`.

⚠️ Corrigé 2026-09-01 : « le guide est injecté, ne plus prescrire sa lecture au
démarrage » ne tient que pour les clients qui livrent l'artefact entier. Pour les
autres (Claude Code, claude.ai — #478), la lecture au démarrage EST le canal : le socle
prescrit `oto_guide op=read slug=notice` puis `oto_context`, et la description
d'`oto_context` (toujours livrée, elle) porte la même consigne.
Les **guides nommés (skills)** ne sont pas des outils → absents de `tools/list` → `on_list_tools`
**enrichit la description de `oto_procedure`** avec leur index per-org (`instructions.skills_index_md`,
Tool non-frozen → `model_copy`). `render()` reste la surface STATIQUE (boot / fallback, sans DB).
Tout **fail-open** (pas de sub/org/guide/DB → surface statique). Édition des blocs A/B : capacité
`oto_admin_platform_instructions` (+ REST `/api/admin/platform-instructions`, `PLATFORM_ADMIN`) →
éditeur dashboard `/platform/instructions`. Transparence : `/api/me/agent-context` rend le même
artefact composé. **Reste (#54)** : anticipation **pilotée** (message proactif amorcé par l'admin).

**Slots de procédure (ADR 0035, B1–B3 déployés).** Une procédure déclare ses **entités
à instance** (quel tableau, quel compte de connecteur, quelle page Documents) en **JSON propre** :
colonne `org_instructions.slots` JSONB (`{name, type ∈ tableau|connecteur|doc,
description?, connector?}`), la prose les référence **par nom** via `<slot:name>` (même
famille que `<tool:slug>` 0014 ; le binding nom→instance vit dans le PROJET,
`project_links.slot` — vocabulaire DU projet, unicité `(project_id, slot)` → 409
`slot_taken` au link). Module `slots.py` = source unique (validation dure
`validate_slots`/`normalize_name` + check croisé non bloquant `slots_check` : refs
mortes, slots jamais cités, cohérence connecteurs déclarés ↔ refs `<tool:>`, suggestion
quand un connecteur à identités est référencé sans slot). Écriture : `oto_procedure(op='set')`/
`PUT /api/me/instructions/{slug}` (param `slots`, warnings en réponse) ; transport
revisions + revert + `copy_instruction_to_org` + publish/fork bibliothèque +
`duplicate_project`. **Runtime (B3)** : les tools `data_*` acceptent
`namespace='slot:<name>'` → `access.resolve_slot_tableau` résout contre les bindings du
**projet actif** ; pas de projet / slot non bindé / binding pendouillant = **McpError
actionnable, jamais de fallback** (bracelet serveur 0023) ; `data_create_namespace`
refuse le préfixe (un slot binde un tableau existant). Bloc A : §« Slots » (⚠️ prose
seedée en DB — une évolution du texte passe par `oto_admin_platform_instructions`, pas
seulement la constante). Grandfathering : procédure sans slots / nom nu = inchangés.
Restent B4 (inventaire dérivé) + B5 (vérifications) — épic otomata-private#59.

## Agent readme (cumulable) & procédures — le vocabulaire produit

Vocabulaire produit (unbundle 2026-07) : **agent readme** = prose libre **injectée à
chaque session**, cumulée du général au spécifique — **plateforme** (bloc A) → **org** →
**équipe active** → **user**. Les 4 étages vivent dans `guides` delivery='init' (0042) ET
**s'éditent par UNE surface** depuis le 28/07 (§Convergence des surfaces) : la capacité
`me.guide{,s}` — `oto_guide(op=…, scope=…, delivery='init')` en MCP, `/api/me/guides/{scope}/readme`
(+ variantes `/api/{orgs,groups}/{id}/…` pour viser une cible explicite) en REST. ⚠️ Le
routage `claude_md`→`guides` qui vivait DANS `org_store`/`group_store` est RETIRÉ : le store
de procédures ne sert plus le readme (`get_instruction` → None, `set_instruction` → ValueError),
les appelants qui le veulent lisent `guide_store.init_guide_body(scope, id)`. `me.agent_readme` +
`/api/me/agent-readme` + `db.{get,set}_user_readme` supprimés (table `user_agent_readme` laissée
en place — elle sert encore de source au backfill de boot ; son DROP est une migration à part). Chaque niveau passe par `_apply_vars`
({{org}}/{{user}}/{{équipe}}/{{connecteurs_actifs}}). **Procédure** = guide nommé
(skill), chargé à la demande. Prose opératoire versionnée par org — le reste de ce
document en détaille le mécanisme.
