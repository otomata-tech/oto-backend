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

> ⚠️ **« doctrine » = « guide » depuis le 28/08/2026** (#519) : le mot a disparu de l'interne du backend (modules, symboles, prose). Les noms SERVIS qui le portent encore se **doublent** au lot B — le nouveau nom naît, l'ancien reste servi avec une date de retrait écrite (premier tag à partir du 30/11/2026 ; retrait = lot D, #526). **Table unique de ces alias : [`alias-deprecies.md`](alias-deprecies.md).** Déjà fait : l'outil s'appelle `oto_admin_guide` (son ancien nom répond encore).

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
- **Lecture** : `oto_procedure(op='get'[, slug, scope, version, with_history, full])` — sans `slug` =
  `{doctrine, group_doctrine, doctrines[]}` (base org + base groupe + index), le call de **DÉBUT DE
  SESSION** ; avec `slug` = le markdown d'un guide nommé — **sur la face MCP, sans son dessin ni
  sa description** (§ « Ce que l'agent lit »), `full=true` rend tout. `oto_procedure(op='list'[,
  query, scope, verbose])` = catalogue/recherche — face MCP, la description est un `summary` de
  200 caractères, `verbose=true` la rend entière. Scopés à l'**org active** (+ groupe actif) — servis aux seuls
  membres. **Vide sans erreur** si pas d'org active (`_SERVER_INSTRUCTIONS` invite à `oto_procedure(op='get')`).
- **Un palier en masque un autre, et le DIT** (#1156) : `op=get slug=…` sans `scope` suit la
  cascade « ma copie perso EN SERVICE, puis l'org ». Une copie perso **archivée ne masque plus**
  (décision du 14/10/2026) : la cascade la saute et sert l'org ; elle reste lisible sur
  `scope='user'`, et la désarchiver la remet en tête (`org_store.get_instruction_en_service`,
  même règle pour l'app de procédure et l'empreinte d'un run, qui journalise le palier
  exécuté, `guide_scope`). La cascade se dit : la
  lecture (par slug, par `guide_id`, et `GET /api/me/instructions/{slug}`) et l'écriture rendent
  **`other_scopes`** (les autres paliers de l'appelant qui portent ce slug : palier, `guide_id`,
  version, date) et, quand une copie perso masque celle de l'org pour une lecture par nom,
  **`shadow_warning`** (laquelle est servie, laquelle est écartée, comment lever le masque :
  `op=archive scope='user'`, réversible, ou `op=delete scope='user'`). Les
  deux clés sont ABSENTES quand il n'y a rien à dire. L'équipe, hors cascade, n'est nommée que
  quand le geste la vise, jamais dite masquée. Seul le palier perso de l'appelant se voit.
  Né de 210 travaux de production servis plus d'un jour sur la copie perso, sans un signal
  (`_paliers_du_slug`, `tests/test_masque_de_palier_1156.py`).
- **Écriture** : `oto_procedure(op='set'[, body_md, slug, scope, org, group, title, desc,
  from_version])` (base = slug omis ; nommée sinon ; `from_version` = revert) +
  `oto_procedure(op='delete', slug[, scope, org, group])`. Autz **par PALIER** (`scope`,
  #681 — 31/08/2026) :
  - `scope='user'` (**défaut** depuis l'ADR 0068, 04/09/2026) : procédure PERSONNELLE,
    visible de toi seul, aucun droit d'org requis. ⚠️ Cette ligne annonçait `org` par
    défaut alors que le code rendait déjà `user` (`_ECRIT_SCOPE`) : un agent y lisait
    qu'il écrivait pour l'équipe en écrivant pour lui — l'objet introuvable, pas la
    fuite, mais la même journée perdue (corrigé le 08/09/2026, texte servi compris) ;
  - `scope='org'` : `org` absent → org active, **org_admin** ; présent → autre org,
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
- **Archiver** (signal #1374) : `oto_procedure(op='archive'|'unarchive', slug[, scope, org,
  group])`, même garde que `set` — rien n'est détruit, le geste se défait par son inverse ;
  idempotent (`archived_at` garde la date du premier retrait). Une procédure archivée sort
  des listes (`op=list include_archived=true` la remontre), se lit encore par son slug, et
  un agent planifié ou webhook qui la nomme est REFUSÉ (`refused_procedure_archived` sur sa
  page, 409 `procedure_archived` au webhook — `procedure_retiree.py` : palier personnel du
  propriétaire, puis l'org ; une procédure d'équipe n'y est pas lue).
- **Renommer** (issue `oto`#261) : `oto_procedure(op='rename', slug, new_slug[, scope, org,
  group])` + `POST /api/me/instructions/{slug}/rename` (palier org). Le slug change, l'IDENTITÉ
  reste : l'`id` stable (`guide_id`, que les liens de projet, les partages et le nœud dérivé
  désignent), la version (renommer n'est pas écrire le contenu) et l'historique entier, qui
  suit sous le nouveau nom dans la même transaction (`org_store.rename_instruction`). Même
  garde que `set` — un membre renomme la procédure de son équipe : rien n'est détruit, et le
  geste se défait en renommant de nouveau. **Pas d'alias** : l'ancien slug ne résout plus et
  redevient libre ; une prose qui le cite (autre procédure, readme) est à reprendre à la main,
  la réponse le rappelle. Refus, rien de changé : `new_slug` pris (409 `slug_taken`).
  **Le runner suit**, dans la même transaction : `runner_triggers.procedure`,
  `runner_fleets.procedure` et la charge des travaux en attente (`runner_jobs.payload`,
  `pending`/`held`) portent le SLUG, pas l'id — ils sont repointés, et leur instruction de
  départ réécrite là où elle cite `` `ancien` `` (la forme que `_instruction` dérive). Seules
  suivent les lignes pour lesquelles l'ancien slug résolvait vers CETTE procédure (cascade
  de lecture de l'agent : sa procédure personnelle d'abord, l'org, puis l'équipe). Un
  travail déjà pris (`claimed`) tourne avec ce qu'il a lu : nommé (`in_flight_jobs`),
  jamais réécrit ; une instruction libre qui cite encore l'ancien nom est nommée
  (`inputs_to_review`). ⚠️ La procédure d'une campagne est figée à sa déclaration pour
  que l'attribution des lignes reste vraie : le gel porte sur l'OBJET, que le renommage
  ne change pas (même id, même contenu, même version). La lecture par slug rend désormais `guide_id` : sans
  lui, l'identité qu'on préserve était illisible.
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
  **le propriétaire est une DIMENSION, pas trois cas particuliers.** Le palier `user` est
  OUVERT depuis l'ADR 0068 (04/09/2026) — `OWNER_TYPES = ("org","group","user")`, `org_id`
  rendu nullable. ⚠️ Cette ligne a annoncé le contraire jusqu'au 09/09/2026, alors que la
  ligne 50 de cette même page disait déjà l'inverse : une page qui se contredit ne se lit
  pas, elle se cite au hasard.

  **En clair** (prose, pas un credential → hors coffre chiffré). **Pas de cache** : lecture DB
  à l'appel. Écriture sérialisée par `(owner_type, owner_id, slug)` via verrou advisory.
- **Pas d'instruction par namespace d'outil** : un gotcha d'outil est vrai pour tout le monde et
  évolue avec le code du connecteur → sa place reste le repo (docstring, `_SERVER_INSTRUCTIONS`),
  versionné avec l'outil.

## Le dessin n'est plus exigé (retiré le 18/09/2026)

⚠️ **Une procédure n'a plus à porter de dessin.** Il avait été rendu obligatoire le
23/08/2026 (b34af1cc) pour un besoin d'affichage d'un front partenaire, qui faisait du
dessin la vue par défaut de sa page de procédure. Une règle de RENDU d'un seul
consommateur était devenue une règle du cœur, servie à tous les agents de toutes les
orgs : dans la description de `oto_procedure`, celle de l'écriture d'org, le socle de
session, le guide `notice` et un guide plateforme dédié (`procedure-flowchart`) — et
figée par des tests qui vérifiaient la présence de chaque renvoi. Décision d'Alexis :
une procédure se lit en prose, en étapes numérotées ; c'est ce que l'agent qui
l'exécute lit, et le dessin lui coûtait des jetons sans rien lui apprendre. Un front qui
veut un dessin le demande au niveau de SON TENANT — et bientôt de sa propre instance
(ADR 0070) —, jamais du cœur ni de chaque org.

Ce qui part : l'avertissement « aucun dessin » (`diagram_check` rend `None` sans
dessin), les quatre textes qui le réclamaient, le guide plateforme `procedure-flowchart`
(fichier seed retiré ; ⚠️ le nœud déjà semé en base se retire à part, par
`oto_guide op=delete scope=platform`), et les tests qui figeaient ces renvois — remplacés
par un cliquet qui refuse leur retour.

Ce qui reste, pour les procédures qui ont DÉJÀ un dessin : il s'affiche comme avant, il
est servi à l'agent sous la forme d'un marqueur et remis à l'écriture (section
suivante), et `diagram_warning` parle encore d'un dessin PRÉSENT — deux dessins dans un
corps (la page n'en rend qu'un), ou un tracé que le parseur du front refusera (lint
ligne à ligne, `procedure_diagram.lint_du_trace`).

⚠️ Le « Self-improvement digest », imposé par le même commit, avait déjà été retiré le
10/09/2026 (oto#159) pour la même raison : la plateforme portait déjà ce qu'il racontait
(version, historique des versions, documents de projet).

## Ce que l'agent lit : la consigne, pas la vitrine

Une procédure est relue à chaque run, et tant qu'elle reste dans le contexte, chaque
jeton lu est repayé à chaque tour. Mesuré le 10/09/2026 sur trois procédures d'une org
cliente (≈100 000 caractères servis par run, ≈27 000 jetons) : le **dessin** pèse ~10 %
de la lecture (3 304 caractères = 983 jetons Haiku — la prose fait 3,6 caractères par
jeton, le tracé 3,4, donc c'est sa taille qui coûte, pas ses caractères), la
**description** recopiée à côté du corps ~5 %. Ni l'un ni l'autre n'apprend rien à
l'agent qui EXÉCUTE : le dessin est la vue de la page (un humain le regarde, les
étapes disent le même flux en prose), la description est la ligne du catalogue (il l'a
lue pour choisir).

Sur la **face MCP seulement** (`ctx.channel == "mcp"` — la face REST nourrit la page,
qui a besoin du dessin ; un appel interne sert tout), et **par défaut** (une économie
qu'il faut demander ne bénéficie à personne) :

- `op=get` sert le corps avec le dessin remplacé par **une ligne**, un marqueur
  `<!-- flowchart: v<n>, <k> lines, … -->`, et sans `description`. `full=true` rend tout.
- `op=list` sert un `summary` de 200 caractères (coupé au dernier espace) à la place de
  `description`, sans `updated_at`. `verbose=true` rend la fiche entière.

⚠️ **Le marqueur n'est pas un commentaire, c'est ce qui garde le dessin.** L'agent qui
édite RELIT puis RÉÉCRIT (`op=get` → `op=set`) ; servi sans dessin et sans marqueur,
chaque édition d'agent viderait la page du process — et personne ne le verrait avant
de l'ouvrir. À l'écriture, `procedure_diagram.avec_le_dessin` remplace le marqueur par
le dessin de la **version courante** ; un corps qui arrive avec un vrai dessin le garde ;
un corps sans marqueur ni dessin s'écrit tel quel, ce qui est permis. Les
corps **stockés** ne portent jamais le marqueur — il ne vit qu'entre les deux appels.
Banc : `tests/test_procedure_servie_lean.py`, dont l'aller-retour à l'identique.

⚠️ **Ce que le marqueur ne promet pas.** Il a d'abord dit « keep this line and op=set
keeps the drawing », sans condition — et `avec_le_dessin` relit le corps courant de la
**ligne visée par l'écriture**. Une écriture qui vise ailleurs n'a rien à relire, et le
marqueur s'efface : `op=create`, un slug neuf, et surtout un **`scope` omis** — le
défaut d'écriture de `oto_procedure` est `user` (`_ECRIT_SCOPE`), donc relire l'org et
réécrire sans `scope` publie chez soi une procédure sans son dessin. La ligne servie dit
désormais « same slug and scope you read ». La perte n'est pas silencieuse
(`diagram_warning`), mais elle est constatée APRÈS.

⚠️ **Marqueur + vrai dessin dans le même corps = DEUX blocs dessinants.** La page n'en
rend qu'un, le premier ; `has_diagram` répondait « oui, il y a un dessin » et se taisait.
`procedure_diagram.compter_les_dessins` les compte, et `diagram_check` le dit
(`DOUBLE`) : c'est le seul cas où la perte était muette.

Ce que ça ne fait **pas** : servir la procédure « par étape ». Découper la lecture en
une lecture par étape échange la résidence (payée au tarif du cache) contre des tours
supplémentaires (chacun relit tout le contexte) — mesuré sur ces trois procédures, le
run y perd 10 à 20 %, et les règles transversales (le filtre sur la société, la liste
blanche des champs, la borne de 255 caractères) vivent hors des étapes. La seule
économie qui tienne est de servir MOINS, sur les blocs que la forme délimite.

## L'écriture rend l'empreinte du corps stocké (oto#133, 24/09/2026)

Publier un corps ne rendait rien qui prouve que la base avait gardé ce texte-là : il
fallait relire (`op=get`) et comparer à la main — contrôle qui a trouvé dix-huit lignes
servies aux agents qu'aucun fichier source ne contenait. `op=set`/`op=create` (et leurs
faces REST, `PUT /api/groups/{id}/instructions/{slug}` compris) rendent désormais
**`body_sha256`** : le SHA-256 hex des octets UTF-8 du corps **relu en base pour la
version écrite** (`procedure_empreinte.empreinte_check`, sur la révision — pas la ligne
vivante, qu'une édition concurrente a pu déjà remplacer). Jamais l'empreinte de l'envoi :
c'est l'écart entre les deux qu'on veut voir.

⚠️ Le corps stocké n'est pas l'envoi à l'octet près : blancs de tête et de fin retirés,
outils cités sous un nom de produit ramenés au canonique, dessin remis à la place du
marqueur. Comparer donc au SHA-256 de l'envoi **stripé** (`sha256sum` d'un fichier qui
finit par `\n` ne correspondra jamais) ; un écart qui subsiste dit que la base porte autre
chose que ce qu'on croit avoir publié.

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

## Le semis des guides plateforme : le dépôt atteint la base

Un guide plateforme (`oto_mcp/guides/<slug>.md`) est un **texte servi à l'agent**.
Les seeds plateforme s'écrivent **en anglais** : les agents hébergés les lisent et
répondent dans la langue de ce qu'ils lisent.
Servi périmé, il lui fait appliquer des gestes retirés et ignorer ceux qui existent.
Mesuré le 13/09/2026 : le guide `datastore-semantics` servi en production avait
**environ 250 lignes de retard** sur le fichier du dépôt, depuis plusieurs livraisons —
il ne connaissait ni `@clear` ni le paramètre de lecture `empties`, et présentait
encore comme active une forme retirée. Cause : le semis de démarrage n'**insérait**
que les slugs absents (`INSERT … ON CONFLICT DO NOTHING`), donc **une mise à jour d'un
guide dans le dépôt n'atteignait jamais un environnement existant**.

**Contrat (validé le 13/09/2026, livré le 23/09/2026 — otomata-tech/oto#236), sans
écart à l'ADR 0042 :**

1. **Qui fait foi.** La base reste la source **éditable** d'un guide plateforme
   (`oto_admin_guide` / `oto_guide` n'y perdent aucun droit) ; le fichier du dépôt est
   la source du **semis**.
2. **Le semis empreinte ce qu'il pose.** `props->>'seed_sha256'` = l'empreinte des
   trois champs écrits (titre, description, corps — `db.empreinte_de_couche`). Au
   démarrage (`guide_store.seed_platform_guides`), pour chaque fichier :
   - fichier changé **et** base encore au dernier semis → le guide est **mis à jour** ;
   - base éditée depuis le dernier semis → le guide est **conservé**, la divergence est
     **signalée** ;
   - même empreinte → **aucune écriture**.

   Décision et écriture dans la même transaction, ligne verrouillée (`FOR UPDATE`) :
   une édition admin concurrente ne se glisse pas entre le constat et l'écrasement.
3. **Aucun refus au démarrage.** Le boot reste en DDL additif et échec ouvert, et la
   fenêtre du healthcheck est finie : un refus empêcherait toute bascule de version.
   Un semis en échec ou un guide divergent est un **défaut de santé d'instance**,
   servi par `oto_admin_guides_semis` (`capabilities/guides_semis.py`, PLATFORM_ADMIN,
   lecture seule) — codes `guides_non_semes`, `guide_divergent`,
   `guides_sans_empreinte`, `semis_absent` — **plus une remontée Sentry** (tag
   `oto.semis_guides`, même canal que `tenancy._refus`). Le témoin est le défaut, pas
   le refus. ⚠️ Le rapport est celui **du process** qui a semé : `fait: false` ne dit
   pas « tout va bien », il dit « ce serveur n'a rien semé ».
4. **Les lignes d'avant l'empreinte** (toute la population de production au 23/09) ne
   sont **jamais devinées au démarrage** : le semis ne saurait pas distinguer « jamais
   touché » de « réécrit par un admin ». Elles sont alignées par un **geste unique**,
   `scripts/aligner_guides_plateforme.py` : il énumère la population (fichiers ∪ base),
   **montre chaque écart**, puis écrit — et seulement avec `--aligner`.
5. **Un guide plateforme servi sans fichier dans le dépôt** (cas mesuré :
   `procedure-en-routine`) est **exporté en fichier** par ce même geste, et ce fichier
   devient sa source de semis. Aucun guide plateforme ne reste servi sans source
   versionnée : sans fichier, il n'est relu par personne et le prochain environnement
   ne l'a pas. ⚠️ Le fichier exporté est à **committer**.
6. **Les sous-dossiers de `oto_mcp/guides/` ne sont pas semés** et le semis n'y descend
   pas (`glob("*.md")`, NON récursif) : la frontière est tenue comme une propriété par
   `tests/test_guides_seeds_foyer.py`. Un fichier posé à la racine par habitude, ou un
   `rglob` « de propreté », sèmerait en production des guides que personne n'a décidés.
7. **Bancs** : `tests/test_semis_guides_plateforme.py` — le rapport et ses défauts sans
   base, puis sur base jetable base neuve, fichier modifié, base éditée, idempotence,
   rejeu du démarrage, et la durée mesurée dans la fenêtre du healthcheck (120 s).

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
(`middleware/dynamic_instructions.py`) **remplace** les instructions de la session par `instructions.compose_session(sub, org_id)`.
⚠️ **Jusqu'au 29/09/2026, ce remplacement n'atteignait AUCUN client** : fastmcp répond au
`initialize` PENDANT `call_next` (réponse construite depuis `session._init_options`), et le
middleware modifiait `result` APRÈS — chaque client recevait la surface statique du
démarrage (`render()`), jamais le composé, ni un socle de tenant, ni les readmes, ni les
noms d'outils du produit. Les tests unitaires appelaient le hook avec un `call_next`
factice et ne voyaient rien. Depuis : tout se résout AVANT `call_next` et se pose sur les
options de CETTE session (`middleware/_handshake.py`, seul lecteur du champ privé) ; ce que
le client reçoit est éprouvé par un vrai `fastmcp.Client`
(`tests/middleware/test_handshake_livre.py`). **Règle : un champ du `initialize` se pose
avant la réponse, jamais sur `result` après.** C'est
un **artefact composé de 2 blocs** (`instructions.py`, #50 ; l'ex-bloc B onboarding a été
retiré le 2026-07-01 — l'onboarding est un projet, ADR 0032 §7) :
- **bloc A « secret sauce »** (posture + boucle d'usage + **catalogue de namespaces** dérivé) —
  prose en DB — une couche de contexte `init` de slug `secret_sauce` dans `nodes` (⚠️ plus
  la table `platform_instructions`, dont le backfill de boot est parti avec la table `guides`
  le 23/09/2026, oto#239 ; la surface d'administration lit et écrit `nodes` depuis le 28/07) —,
  éditable admin plateforme, **inviolable par l'org**, toujours injecté (la constante
  `_SECRET_SAUCE` reste le défaut ET le repli) ; le catalogue est appendé à la composition ;
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

**Le rappel dans les réponses d'outils (oto-backend#1041, 24/09/2026).** Prescrire la
lecture ne suffisait pas : mesuré sur une org cliente, `oto_context` appelé 6 fois contre 46
lectures de procédure, et un README réécrit reçu par personne. Désormais, tant que l'appelant
n'a pas lu `oto_context` **pour l'org de l'appel** depuis la dernière modification d'une
couche de README qu'il cumule (org, équipe active, note de l'utilisateur), **chaque réponse
d'outil porte en tête une ligne** — « contexte de l'org « X » non lu, ou modifié depuis ta
dernière lecture : appelle `oto_context` (`_org=N`) » — qui disparaît dès la lecture.
- **Verdict sans état de session** (ADR 0038) : `db.contexte_non_lu`, UNE requête — les
  couches par l'index d'identité, le journal `tool_calls` par `idx_tool_call_log_tool` —
  prise hors boucle, cachée `TTL_S` = 60 s par `(compte, org)` (`oto_mcp/rappel_contexte.py`).
  Le cache ne décide rien que la base ne dise, sauf la lecture qu'on vient de servir, qui
  éteint le rappel tout de suite (la ligne du journal s'écrit en tâche de fond).
- **Bornes** : ni sur `oto_context` ni sur `oto_whoami` ; rien pour une org sans README ;
  rien pour un travail du runner (jeton de délégation — son allowlist est fixée, il ne
  pourrait pas suivre le rappel) ; une ligne ≤ 300 caractères (nom d'org coupé à 60), jamais
  le README lui-même.
- **Plomberie** (`middleware/rappel_contexte.py`) : la ligne est posée AU-DESSUS du rendu du
  vide et du corps markdown (qui remplacent tout le canal texte), le verdict est pris SOUS
  le contexte d'appel (qui porte l'org de l'appel) — ordre gardé par
  `tests/middleware/test_middleware_order.py`.
- ⚠️ **Canal texte seulement** : un outil qui garde un canal structuré (schéma déclaré, app)
  est lu sur ce canal-là par certains clients, qui ne verront pas la ligne. ⚠️ Elle n'entre
  pas dans `tool_calls.result_size` (le journal est plus interne qu'elle) : sa taille est
  bornée et mesurée par `tests/test_rappel_contexte_1041.py`, pas par le journal.
Les **guides nommés (skills)** ne sont pas des outils → absents de `tools/list` → `on_list_tools`
**enrichit la description de `oto_procedure`** avec leur index per-**(compte, org, équipe
active)** (`instructions.skills_index_md`, Tool non-frozen → `model_copy`). ⚠️ Les TROIS
paliers depuis le 09/09/2026, marqués `[perso]` / `[équipe]` / sans marque pour l'org — cet
index ne lisait que l'org, alors que l'écriture sans `scope` va au palier personnel depuis
l'ADR 0068 : une procédure écrite à soi n'apparaissait ni ici ni dans le bundle de session
(`op=get` sans slug), donc dans RIEN de ce que l'agent reçoit sans le demander. Les deux
index et `op=list` doivent cumuler les mêmes paliers — garde
`tests/test_index_paliers_perso.py::test_les_deux_index_lisent_les_memes_paliers`. Coût
mesuré du cumul en production le 09/09 : +2 lignes / +154 caractères (+1 % sur l'index le
plus gros, +5 % sur un petit). Composé **hors boucle** (`run_in_threadpool`) : ces lectures
tournaient dans l'event loop. `render()` reste la surface STATIQUE (boot / fallback, sans DB).
Tout **fail-open** (pas de sub/org/guide/DB → surface statique). Édition des blocs A/B : capacité
`oto_admin_platform_instructions` (+ REST `/api/admin/platform-instructions`, `PLATFORM_ADMIN`) →
éditeur dashboard `/platform/instructions`. Transparence : `/api/me/agent-context` rend le même
artefact composé. **Aperçu d'un membre type (#1194)** : `GET /api/orgs/{id}/context/preview
?group_id=` (org_admin de l'org ou opérateur) rend les couches socle → readme d'org → readme
d'équipe avec les MÊMES primitives (`instructions.member_preview_layers` : `_choisir_socle` sur le
tenant de l'org, `_render_readme`, préfixe d'outils du tenant) et l'exposition de la visibilité
(`connector_activation.exposed_for`), sans sub : le contexte personnel, la fiche, la note et les
variables d'une personne (`{{user}}`, `{{rôle}}`, `{{connecteurs_actifs}}`, `{{projets_récents}}`,
laissées telles quelles) en sont exclus, et sa lecture est STRICTE (une base illisible lève).
**Reste (#54)** : anticipation **pilotée** (message proactif amorcé par l'admin).

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
**projet actif**, et rend le NUMÉRO du tableau bindé, jamais son nom (#365 : un nom se
résolvait ensuite chez l'homonyme de l'appelant) ; pas de projet / slot non bindé / binding
pendouillant = **McpError
actionnable, jamais de fallback** (bracelet serveur 0023) ; `data_create_namespace`
refuse le préfixe (un slot binde un tableau existant). Bloc A : §« Slots » (⚠️ prose
seedée en DB — une évolution du texte passe par `oto_admin_platform_instructions`, pas
seulement la constante). Grandfathering : procédure sans slots / nom nu = inchangés.
Restent B4 (inventaire dérivé) + B5 (vérifications) — épic otomata-private#59.

## Agent readme (cumulable) & procédures — le vocabulaire produit

Vocabulaire produit (unbundle 2026-07) : **agent readme** = prose libre **injectée à
chaque session**, cumulée du général au spécifique — **plateforme** (bloc A) → **org** →
**équipe active** → **user**. Les 4 étages vivent dans `nodes`, `delivery='init'` (0042 ;
la table `guides` est sortie du code le 23/09/2026, oto#239) ET
**s'éditent par UNE surface** depuis le 28/07 (§Convergence des surfaces) : la capacité
`me.guide{,s}` — `oto_guide(op=…, scope=…, delivery='init')` en MCP, `/api/me/guides/{scope}/readme`
(+ variantes `/api/{orgs,groups}/{id}/…` pour viser une cible explicite) en REST. ⚠️ Le
routage `claude_md`→`guides` qui vivait DANS `org_store`/`group_store` est RETIRÉ : le store
de procédures ne sert plus le readme (`get_instruction` → None, `set_instruction` → ValueError),
les appelants qui le veulent lisent `guide_store.init_guide_body(scope, id)`. `me.agent_readme` +
`/api/me/agent-readme` + `db.{get,set}_user_readme` supprimés (table `user_agent_readme` laissée
en place — plus aucun backfill ne la lit ; son DROP est une migration à part). Chaque niveau passe par `_apply_vars`
({{org}}/{{user}}/{{équipe}}/{{connecteurs_actifs}}). **Procédure** = guide nommé
(skill), chargé à la demande. Prose opératoire versionnée par org — le reste de ce
document en détaille le mécanisme.

## `oto_guide op=patch` : éditer une région sans réémettre le corps

`op=write` REMPLACE le corps. Un guide tenu en journal — une entrée datée par passage —
grossit : vers 60 000 caractères, y ajouter une ligne voulait dire relire le corps entier
et le réémettre à l'identique, avec le risque d'en abîmer le reste sans que rien ne le
signale. `op=patch` modifie UNE région d'un guide **on-demand existant** et ne fait
voyager ni le corps à l'aller, ni le corps au retour.

- **Même grammaire que `oto_doc op=patch`**, même moteur (`oto_mcp/doc_patch.py`,
  fonctions pures) : `mode` replace | append | prepend | delete, et UNE cible —
  `section` (le texte d'un titre markdown) ou `region="preamble"` (ce qui précède le
  premier titre). Les codes de refus sont ceux des pages (`unknown_section`,
  `ambiguous_section`, `ambiguous_target`, `missing_target`, `heading_in_preamble`…).
- **Même droit qu'`op=write`**, résolu par la même ligne (`_owner_for_write`) et AVANT
  toute lecture : un refus ne décrit jamais un guide à qui ne l'écrit pas. Les deux
  bornes de l'écriture valent pour le RÉSULTAT (`body_too_large` au-delà de 65 536
  octets, jamais de guide vide).
- **Un patch ne crée rien** : guide absent = `404 not_found`. Un guide plateforme
  encore servi par son fichier de semis n'a pas de ligne en base — il se pose d'abord
  par `op=write`.
- **Rien ne se perd entre deux auteurs.** Le lire-modifier-écrire se joue dans UNE
  transaction, ligne verrouillée (`db.patch_guide_db`) : deux ajouts simultanés au
  même journal s'enchaînent. `expected_rev` y ajoute le contrôle optimiste des pages —
  `409 conflict` si le guide a changé depuis la lecture. `rev` est un ETag dérivé du
  contenu (titre, description, corps : `db.guide_rev`), rendu par `op=read` et par
  l'accusé d'un patch ; aucune colonne, aucune migration.
- **La réponse est un accusé** : `slug`, `scope`, `title`, `description`, `updated_at`,
  `rev`, `body_md_length`, plus `removed` (replace/delete) et `removed_subsections`
  quand une section parente emporte ses enfants. Le corps d'avant le patch est gardé
  comme version (§ « Les versions d'un guide ») : ce qu'un `replace` emporte se relit
  et se restaure, l'accusé le dit.
- **Pas de readme `init`** (`400 unsupported_delivery`) : il est injecté entier dans
  chaque session, donc tenu court, et se réécrit par `op=write`. Viser son slug
  canonique comme un guide à charger rend `409 delivery_conflict`.
- **Face MCP seule** : les routes `/api/me/guides/…` n'ont pas de verbe de patch.
  `GET` y rend désormais `rev` (ajout au contrat, `GuideView`). Les arguments du patch
  passés à une autre op sont refusés (`unsupported_patch_arg`) ; `expected_rev`, lui,
  vaut aussi pour `op=write` et `op=delete` depuis le 23/10/2026 (§ suivant) et reste
  refusé sur les lectures.

## Écrire un guide sans écraser ce qu'un autre vient d'écrire (signal #1203, 23/10/2026)

Un agent d'onboarding a remplacé par `op=write` le readme d'une org qu'un tiers venait
de poser, entre deux lectures de son contexte : `op=write` REMPLACE, rien ne compare, et
rien ne rend la version d'avant. **La garde est un contrôle optimiste**, le même que
celui des pages et d'`op=patch` :

- **La lecture rend un `rev`**, readme compris (`op=read delivery='init'`, `GET …/guides/
  {scope}/readme?delivery=init` : `rev` y manquait). C'est l'ETag du contenu
  (`db.guide_rev`, aucune colonne). Ce qui n'a pas de contenu — aucune ligne, ou un
  readme vidé — a le rev **`absent`** (`db.REV_ABSENT`) : un écrivain qui crée le
  repasse, et une création concurrente le fait alors échouer au lieu d'être écrasée.
- **L'écriture le renvoie** en `expected_rev` — `op=write`, `op=delete` (vider un readme
  ou supprimer un guide à charger, c'est aussi le réécrire), et leurs faces REST
  (`PUT`/`DELETE /api/me/guides/{scope}/{slug}`, corps ou paramètre). En cas d'écart :
  **`409 conflict`**, le code d'`op=patch` et des pages, avec le rev en place ; rien n'est
  écrit. L'écriture rend `rev` (le nouveau) et **`previous_rev`** (ce qu'elle a remplacé,
  `absent` = rien) : un écrasement qui ne correspond pas à la lecture se voit dans l'accusé.
- **Sous verrou, sans fenêtre** (`db/guides.py::_exiger_la_version`) : le verrou du slug
  (commun aux guides et aux procédures) puis la ligne `FOR UPDATE`, dans la transaction
  de l'écriture. Le readme prend ce verrou à CHAQUE écriture, garde ou pas : sur une clé
  encore vide il n'y a pas de ligne à verrouiller, et une écriture non gardée se serait
  glissée entre le contrôle et l'upsert d'une écriture gardée.
- ⚠️ **Le contrat ne se durcit pas en place, il se double** (`docs/tool-visibility.md`) :
  sur `oto_guide` et sur REST, `expected_rev` est FACULTATIF — l'omettre écrase ce qui
  est en place, défaut historique écrit dans la description servie. **`oto_guide_v2`**
  (bêta, `BETA_TOOLS`) l'EXIGE sur `op=write`/`op=delete` (`400
  expected_rev_required`) ; même entrée, même dispatch (`_guide_op_strict` ne fait que
  poser l'exigence avant `_guide_op`). L'écriture de la console plateforme
  (`oto_admin_platform_instructions`) n'est pas gardée.
- La garde empêche l'écrasement à l'aveugle ; ce qui a été écrasé en connaissance de
  cause se retrouve depuis le 03/11/2026 dans les versions gardées (§ suivant).
- Banc : `tests/test_guide_ecriture_gardee_1203.py` (base réelle : écrasement refusé,
  création concurrente, readme, suppression, surface stricte).

## Les versions d'un guide (03/11/2026)

Jusque-là une écriture de guide REMPLAÇAIT sur place, et une suppression emportait tout :
la garde `expected_rev` (§ précédent) empêchait d'écraser sans avoir lu, rien ne rendait
la version d'avant. Désormais **ce qu'une écriture remplace est gardé** :

- **Quand** : avant chaque remplacement — `op=write` d'un guide à charger
  (`db.set_guide_db`), `op=patch` (`db.patch_guide_db`), l'écriture d'un readme
  (`db.set_init_guide_db`, console plateforme comprise) — et avant chaque suppression
  (`db.delete_guide_db`, et vider un readme). L'état PRÉCÉDENT est écrit dans
  `guide_revisions`, **dans la transaction de l'écriture**, sous son verrou (celui du
  slug, puis la ligne `FOR UPDATE`, `_exiger_la_version`) : l'écriture et sa version
  passent ou échouent ensemble. Une première écriture (rien avant), un readme vide ou une
  réécriture au même contenu (même `rev`) n'en laissent pas. Le semis des guides
  plateforme et son geste de maintenance n'en laissent pas non plus : ils posent le
  contenu d'un fichier du dépôt, que git versionne.
- **Ce qui est gardé** : `title`, `description`, `body_md`, le `rev` de cet état (le même
  ETag que la lecture sert), `edited_by` (qui a fait le geste qui l'a remplacé, la
  convention de `doc_revisions`), `created_at`, et `reason` : `write` (guide réécrit ou
  patché), `init` (readme réécrit), `delete` (guide supprimé, readme vidé), `restore`
  (une restauration est passée par-dessus).
- **L'identité est la clé naturelle** `(owner_type, owner_id, slug, delivery)`, pas l'id
  du nœud — un guide supprimé n'a plus de nœud, et c'est alors que sa dernière version
  compte. Donc aucune clé étrangère ; la fusion de comptes repointe `owner_id` et
  `edited_by` (`_SUB_COLUMNS`), l'export de périmètre les classe comme `nodes`
  (`ParEntite`).
- **Rétention** : une version reste si elle a **moins de 30 jours OU si elle est parmi
  les 50 plus récentes** de son guide — le plus généreux des deux. La purge du surplus se
  fait **au fil de l'écriture**, dans la même transaction, par l'index de la couche
  (`db/guide_revisions.py::_purger`) : aucune tâche planifiée.
- **Lire** (droit de LIRE le guide — `_owner_for_read` : un guide plateforme pour tout
  authentifié, celui d'une org pour ses membres, une note pour son auteur) :
  `oto_guide op=revisions` (slug, scope) → les versions, la plus récente d'abord, sans
  corps (`rev`, `title`, `reason`, `replaced_by`, `replaced_at`, `body_md_bytes`) et le
  `current_rev` (`absent` pour un guide supprimé) ; `op=read` avec `rev` → le contenu
  d'une version (outils cités aux noms du produit, comme à la lecture). `scope` omis :
  celui du guide qu'`op=read` servirait ; un guide supprimé se nomme par son scope
  (`400 missing_scope` sinon). Faces REST : `GET …/guides/{scope}/{slug}/revisions` et
  `GET …/guides/{scope}/{slug}/revisions/{rev}` (`?delivery=init` pour un readme), sous
  `/api/me`, `/api/orgs/{id}` et `/api/groups/{id}`.
- **Restaurer** : `oto_guide op=restore` (slug, scope, `rev`) / `POST …/revisions/{rev}/
  restore` RÉÉCRIT la version choisie comme une **nouvelle écriture**, par le chemin
  d'`op=write` (`capabilities/guides.py::_ecrire`) : mêmes droits (vérifiés AVANT de lire
  la version), mêmes bornes, même garde `expected_rev` (le rev ACTUEL ; `absent` pour un
  guide supprimé, qui est alors recréé), même refus de famille ; et ce qu'elle remplace est
  gardé à son tour (`reason='restore'`). `oto_guide_v2` exige `expected_rev` sur
  `op=restore` comme sur `op=write`.
- **Effets déclarés** (`tools/effets.py`) : `op=write`, `op=patch` et `op=restore`
  ÉCRIVENT (la version remplacée est gardée), `op=revisions` lit, `op=delete` reste
  DESTRUCTIF — le guide quitte le catalogue de tous ses lecteurs, même si sa dernière
  version se restaure.
- **Un `rev` inconnu** (jamais gardé, ou sorti de la rétention) rend **`404
  unknown_revision`**, déclaré : aucune autre version n'est servie ni restaurée à sa
  place.
- Banc : `tests/test_guide_versions.py` (base réelle : instantané, première écriture,
  suppression, rétention, lecture, restauration, droits, rev inconnu) ; migration
  `20261103_1400_guide_revisions` (`tests/test_migration_20261103_guide_revisions.py`).
