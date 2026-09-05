---
title: Le nouvel univers de contenu — nœuds
type: explanation
description: >-
  Le modèle de contenu unique (page, tableau, ligne) et sa surface propre, qui vit
  À CÔTÉ de l'ancienne sans la traduire. Ce que porte `props` et ce que porte `data`,
  pourquoi la recopie au démarrage est arrêtée depuis le 2026-09-01, ce qu'il reste
  du résidu qu'elle a laissé, et ce qui n'est pas encore porté (file de travail,
  filtre et tri sur un tableau natif). À lire avant de toucher `db/nodes.py`,
  `db/node_tables.py` ou une capacité `node_*`.
---

# Le nouvel univers de contenu

## Trois genres, et rien d'autre

Un nœud est **une page, un tableau ou une ligne** (ADR 0054-D5). Projet, guide et
procédure **ne sont pas des genres** : ce sont des rôles portés en propriété par une
page. Le genre dit ce que l'objet EST ; ce qu'il JOUE est une propriété, jamais un
`kind` de plus.

Une ligne est un nœud comme les autres : elle a un genre, un parent (son tableau) et
une place dans la fratrie. C'est pourquoi les **mêmes quatre verbes** — `create`,
`update`, `move`, `delete` — servent les trois genres. Leur donner chacun le sien
créerait trois vocabulaires pour une seule notion, et trois endroits où
l'autorisation, l'ordre et le refus d'écrire une copie devraient rester d'accord.

## Deux colonnes, deux natures

| colonne | ce qu'elle porte | qui l'interprète |
|---|---|---|
| `props` | titre, épingle, livraison, schéma d'enfants | **oto** |
| `data` | les valeurs des colonnes d'une ligne | **personne** |

⚠️ **Ce n'est pas du rangement.** Mêlées dans `props`, une cellule nommée `title` ou
`position` écrase le sens du nœud, et toute lecture doit connaître la liste des clés
réservées pour faire le tri. La frontière est celle du datastore — *oto gère les types
standards, jamais l'interprétation métier d'une valeur*.

Coût de la séparation, **mesuré** au banc du 2026-09-01 (200 000 lignes-tableau de six
champs, deux passes en ordre inversé) : **+4,7 %** de volume, et **−14 %** de temps
d'écriture, parce que séparer évite la concaténation jsonb qu'imposait le mélange.

⚠️ Le schéma de colonnes se stocke sous la clé **`fields`**, pas `columns` : c'est ce
que la face de lecture lit. La surface, elle, dit « colonnes » — c'est le mot du
contrat front, et la traduction se fait à un seul endroit, dans `db/node_tables.py`.
Se tromper produit un tableau qui s'affiche **sans aucune colonne, sans erreur**.

## Deux univers CÔTE À CÔTE, aucun ne traduit l'autre

Arbitrage du 2026-08-31 : *« on ne migre pas, on arrête la recopie ; la surface nœud
vit à côté de docs et part de vide »*.

- L'**ancien** monde — `oto_doc`, `oto_project`, `data_*` et leurs tables — continue de
  servir son contenu, sans rien savoir du nouveau.
- Le **nouveau** — `oto_node`, `oto_node_rows`, `oto_node_edit` — naît vide et ne se
  remplit que par ses propres verbes.

Rien ne traduit l'un vers l'autre. Un contenu créé dans l'ancien monde **n'apparaît
pas** dans le nouveau, et c'est le comportement voulu, pas une régression.

**Corps actif et projections.** `nodes.props.body_md` porte le corps courant ; les
surfaces page et guide le modifient, et l'ouverture `oto_node` lit ses blocs. Ces
écritures maintiennent désormais les blocs **dans la même transaction**, ainsi que
le vecteur de classement (`docs/search-and-kb.md`) — une lecture qui suit une
écriture voit donc un état à jour, sans attendre un rattrapage. Un changement de
titre ne réécrit aucun bloc ; les seeds ne projettent que leurs propres insertions.

Il n'existe pas encore de surface d'édition indépendante des blocs. En ajouter une
imposerait de décider et de tenir le sens de synchronisation — pas d'introduire une
seconde autorité implicite.

⚠️ La recopie `guides → nodes` reste jouée **au démarrage**, avec sa stratégie
*newer-wins*. La sortir du boot demande le moteur de transitions versionnées, qui
n'est pas livré (oto-backend#891) : tant qu'il ne l'est pas, ce paragraphe décrit
un maintien de projection, pas une migration.

## Qui voit ces verbes

Les trois verbes MCP — `oto_node`, `oto_node_rows`, `oto_node_edit` — sont réservés aux
comptes **bêta** depuis le 2026-09-01 : un admin pose l'option `beta` sur l'utilisateur
ou sur son org, sinon ils sont masqués. Ils étaient jusque-là exposés à tout le monde
sans aucun gate — l'inverse de ce qu'on croyait, et zéro appel MCP en 30 jours explique
que personne ne l'ait remarqué.

Motif : la surface part de vide et son contrat est provisoire. La proposer à tous, c'est
offrir à chaque agent une lecture qui ne trouve rien et une écriture dont l'utilisateur
ignore la destination. Détail du grain, du fail-closed et de ses limites :
`docs/tool-visibility.md`.

⚠️ **La face REST n'est pas gatée** — le dashboard qui construit ce nouvel univers la
consomme aujourd'hui. Écart assumé, refermé quand la surface cessera d'être provisoire.

## L'arbre ne boucle pas — et ce qui le garantit

`nodes.parent_id` n'a **aucune clé étrangère** (arbitrage M-e, toujours ouvert) : la
base n'a rien pour refuser qu'un nœud soit rangé sous sa propre descendance. Jusqu'au
**2026-09-01**, rien dans le code ne le refusait non plus.

**Ce que ça coûtait, mesuré de bout en bout.** `move A sous B`, alors que B était déjà
enfant de A, rendait **200**. Le `delete A` qui suivait jouait un
`WITH RECURSIVE … UNION ALL` sans borne sur la boucle ainsi créée et ne terminait
pas : il empilait dans `base/pgsql_tmp` jusqu'au `DiskFull`. Avec une borne
artificielle posée à 20 000 niveaux, la requête produisait 20 001 lignes. **Cette base
est partagée entre la préproduction et la production** (`docs/live-migrations.md`) :
n'importe quel appel authentifié pouvait donc saturer le disque de la prod, et la box a
déjà connu un disque plein — SSL et préproduction cassés à la clé.

**Deux gestes, et il faut les deux.**

| geste | où | ce qu'il protège |
|---|---|---|
| le **refus** — `move_page` lève `ParentCycle` si le parent demandé descend du nœud | `db/nodes.py` | les déplacements **à venir** |
| la **borne** — chaque récursion sur l'arbre porte la clause SQL `CYCLE` | `db/nodes.py`, `db/node_view.py`, `db/projects.py` | ce qui serait **déjà en base** |

Le second ne se déduit pas du premier : une garde amont ne défait pas rétroactivement
une boucle écrite hier, et personne ne peut prouver qu'il n'y en a jamais eu. (Relevé
en production le 2026-09-01, en lecture seule : **0** — 75 721 nœuds tous atteignables
depuis une racine, 1 063 pages `docs` de même.)

⚠️ **La borne est la clause `CYCLE`, jamais un plafond de profondeur.** Un plafond
tronquerait un `DELETE` sur un arbre légitimement profond et laisserait des enfants
accrochés à un identifiant disparu — on guérirait d'un mal en en posant un autre.
`CYCLE id SET …` s'arrête à la **répétition** : chaque nœud est visité une fois, aucun
arbre acyclique n'est tronqué. Un cliquet AST (`test_node_parent_cycle.py`) relève
chaque `WITH RECURSIVE` de `db/` **au grain de la fonction** et exige sa clause.

**La lecture DIT le cycle.** `ancestors_of` était bornée en profondeur (12) et servait,
sur une boucle, douze maillons `A, B, A, B…` avec un succès : un cycle déjà en base
était **invisible à la lecture**. Elle lève désormais `ParentCycle` — l'arbre ne peut
pas répondre à « où est ce nœud », et un fil qui s'arrête sans le dire est un zéro qui
ressemble à un résultat. La boucle se **défait** en supprimant le nœud (`delete_page`,
borné, emporte les nœuds de la boucle).

**Même défaut, une table plus loin.** `docs.parent_id` porte, lui, une FK auto-référente
`ON DELETE CASCADE` — et elle n'empêche rien : une FK exige que la ligne visée existe,
pas que l'arbre soit acyclique. `move_doc` et `move_doc_to_project` refusent donc le
cycle (`DocParentCycle`), et les trois descentes de `db/projects.py` (compter,
supprimer, déplacer) partagent une seule définition bornée.

⚠️ **Ce que ce lot ne fait pas** : `capabilities/node_edit.py` ne rattrape pas encore
`ParentCycle`, donc le refus sort en erreur interne plutôt qu'en 400 nommé. La garde
est correcte — rien n'est écrit — mais la réponse servie ne dit pas encore pourquoi.

## La recopie, et pourquoi elle s'est arrêtée

Jusqu'au 2026-09-01, **cinq conversions** tournaient à chaque démarrage — projets,
pages, procédures, tableaux, lignes — et déposaient dans `nodes` une image des tables
historiques, chaque copie marquée `props.legacy`. Elles préparaient une bascule de
lecture : l'ancienne surface devait finir par lire le nouveau stockage.

Ce plan est abandonné, donc la recopie n'a plus d'objet. Elle est retirée de
`db/_init.py`, et un garde-fou (`tests/test_recopie_arretee.py`) lit l'**AST** du
module de démarrage pour qu'aucune ne revienne : un module qui expliquerait longuement
avoir cessé de recopier tout en gardant l'appel échoue.

**Une seule projection survit** : celle des couches de contexte. `db/guides.py` écrit
ses cinq gestes directement dans `nodes`, mais la table `guides` garde un écrivain — le
seed du readme plateforme — et cette projection est le seul chemin par lequel il
atteint `nodes` sur une base **neuve**. Elle partira quand le seed sèmera nativement ;
la retirer avant, c'est retirer sans remplaçant. Un contre-test l'exige.

### ⚠️ La PURGE s'est arrêtée avec la recopie, et personne ne l'avait vu

Constaté le **2026-09-01 au soir**, après coup — le lot d'arrêt ne l'avait ni prévu ni
écrit. Chaque conversion ne faisait pas que copier : elle **finissait par sa purge**,
dans la même fonction (`PURGE_*_NODES_SQL`, exécutés à la fin de chaque `convert_*`).
Cette purge retirait la copie d'un objet dont l'original avait disparu de l'ancien
stockage. Retirer l'appel à la conversion a donc emporté la purge avec elle, en
silence.

Ce que la purge faisait, et qu'on a perdu **des deux côtés** :

- **sa raison d'être** — une page supprimée voyait sa copie disparaître ;
- **son défaut** — elle supprimait le nœud **sans ses blocs**, laissant un corps
  derrière elle. C'est l'origine des blocs orphelins (§ ci-dessous). Le défaut est
  toujours dans le code ; il n'a simplement plus l'occasion de se produire.

**Depuis, plus rien ne propage une suppression ni une édition vers `nodes`** — vérifié :
`db/projects.py::delete_doc` ne touche pas au nouveau stockage, et aucun autre chemin ne
le fait. Donc, tant que le résidu est là :

- une page **supprimée** garde une copie **entière et lisible** — elle apparaît dans le
  rail comme n'importe quelle page ;
- une page **modifiée** garde une copie **périmée**, qui ne se resynchronise plus.

⚠️ **C'est ce qui rend « garder le résidu jusqu'à la migration » coûteux** : ce n'est
pas un miroir dormant, c'est un miroir qui diverge à chaque édition et survit à chaque
suppression. Au 2026-09-01 il n'existe **aucune** copie fantôme (la dernière purge, à
07:33 UTC, a fait son travail avant de s'éteindre) — la première page supprimée en
créera une.

## Le résidu, et comment il se retire

⚠️ **Le stock N'EST PLUS LÀ.** Relevé en lecture seule sur la base servie le
**2026-09-01 à 21:27 UTC** : **0 nœud marqué `props.legacy`**, **0 bloc orphelin**,
53 nœuds vivants (tous natifs, tous des pages) et 928 blocs, tous rattachés. Ce qui suit
décrit donc un état révolu ; on le garde parce que le geste, lui, reste, et que ses
chiffres disent ce qu'il a coûté. **Ne jamais recopier un de ces nombres dans une
décision** : c'est le mode à blanc qui dit l'état du jour.

⚠️ **Et l'heure des chiffres ci-dessous ne tient pas.** Ils sont annoncés « mesurés le
2026-09-01 à 19:30 », dans un commit **écrit à 18:01** (`cac8fae3`) : aucune lecture de
19:30 ne peut y figurer, quelle que soit la lecture du fuseau. Les COMPTES restent
utiles — ils disent l'ordre de grandeur de ce qui a été retiré ; l'HEURE, elle, ne
permet d'ordonner aucun événement, et personne ne doit s'en servir pour ça.

Ce que la dernière passe avait laissé : **75 668 nœuds sur 75 721** et **34 314 blocs**,
dont 31 447 pendent à une copie. Les **53** nœuds restants sont natifs — tous des pages,
dont 47 portent un corps.

⚠️ **Un chiffre de résidu se DATE.** Ceux d'avant (70 876 / 29 174) étaient justes à
leur heure et faux quatre heures plus tard : la recopie a continué de tourner jusqu'au
déploiement de son arrêt, à **07:33 UTC** — dernière copie créée, rien depuis. Le stock
est figé désormais, mais la leçon vaut pour le prochain : **recompter juste avant de
jouer**, jamais réutiliser une mesure de la veille.

Répartition des copies : 73 851 lignes de tableau, 1 057 pages, 334 projets, 289
tableaux, 137 procédures. **Les 1 057 pages ont TOUTES leur original vivant dans
`docs`** — joint un à un, pas échantillonné : retirer la copie ne perd rien.

### Les 1 939 blocs orphelins ne sont PAS des copies

Mesuré le 2026-09-01 : **1 939 blocs** ne pendent à aucun nœud, répartis sur **57**
nœuds disparus, créés entre le 11 et le 28/08. Ils viennent du défaut de la purge —
nœud retiré, corps laissé.

⚠️ **Et leur original n'existe plus non plus.** La purge ne retirait une copie que si
son original avait disparu de l'ancien stockage : ces corps sont donc les restes de
pages **supprimées**. Mesuré, pas déduit — **2 seulement sur 57** retrouvent leur texte
dans un `docs.body_md` actuel, quand le même contrôle sur 200 copies vivantes le
retrouve **200 fois sur 200** (le contrôle voit donc bien ce qu'il cherche).

**Ce n'est donc pas un contenu à sauver, c'est une suppression qui n'est pas allée
jusqu'au bout** — et l'exporter avant de tirer reconstituerait, hors du système, ce que
quelqu'un a demandé de supprimer. Ce qui ne peut pas être prouvé : que les 55 aient été
supprimées volontairement plutôt que perdues autrement.

### ⚠️ « Le stock est CLOS » était FAUX — corrigé le 2026-09-01 au soir (#800)

Ce paragraphe a dit, pendant quelques heures : *« le stock est CLOS (dernier orphelin le
28/08) : la purge ne tourne plus, donc plus aucun ne se crée »*. Il est conservé ici en
toutes lettres parce qu'une carte qu'on efface ne s'apprend pas.

**Depuis quand c'était faux** : depuis le **2026-08-12**. Ce n'est pas la purge qui a
ouvert la fuite, c'est la conjonction de deux lots — le **10/08**, les guides se
dissolvent dans `nodes` et `db/guides.py::delete_guide_db` devient une suppression de
**nœud** ; le **12/08** (lot M2), le corps d'un nœud se projette en blocs, donc ces
nœuds-là ont un corps à laisser derrière eux. Un orphelin natif est possible depuis ce
jour-là, et l'était encore au moment où la phrase a été écrite.

**Pourquoi on l'a cru** : l'enquête portait sur le RÉSIDU de la recopie, et elle a
trouvé son producteur — la purge. De « le mécanisme que j'ai trouvé est arrêté » on a
conclu « plus rien n'en produit », sans jamais énumérer les AUTRES chemins qui
suppriment un nœud. Ils sont six (`grep 'DELETE FROM nodes'`) ; un seul touche un nœud
qui porte un corps sans emporter ses blocs, et il vivait dans le module le plus proche
du sujet. La leçon se range à côté de « énumérer avant une opération de masse » : **un
stock ne se déclare clos qu'après l'inventaire de ses entrées, jamais après l'arrêt
d'une seule d'entre elles.**

**Ce qui l'a fermé pour de bon** : plus une discipline d'appelant — c'est elle qui a
manqué deux fois — mais une contrainte. `blocks.node_id` porte depuis le 2026-09-01 une
clé étrangère **`blocks_node_fk … ON DELETE CASCADE`** vers `nodes`. Le corps part avec
son nœud quel que soit le chemin, et un bloc ne peut plus naître orphelin.

⚠️ Elle se pose **`NOT VALID`** sur une base qui existe (`db/_init.py::_pose_cascade_blocs`)
et VALIDE dans le `CREATE TABLE` d'une base neuve. Les deux moitiés sont nécessaires et
retirer l'une ne rougit qu'à un seul endroit (`tests/test_blocs_cascade.py`). `NOT VALID`
n'est pas un demi-geste : PostgreSQL crée les triggers référentiels dans tous les cas,
donc **la cascade joue dès la pose** ; seule la vérification des lignes DÉJÀ là est
différée, et elle est refusée tant qu'un orphelin traîne. C'est ce qui permet de poser la
contrainte sur une base partagée prod/preprod sans faire échouer le boot sur un état
qu'on ne contrôle pas au moment du déploiement.

Le retrait est le travail de maintenance **`residu-projete`** (`oto-mcp maintenance`).
Trois choses le gouvernent :

- **hors du boot** — son coût suit la taille de la base, la fenêtre du healthcheck est
  finie (ADR 0065) ;
- **à blanc par défaut** — c'est un acte, pas une routine : il n'est dans aucun timer
  ni dans la passe quotidienne, et `--apply` seul écrit ;
- **le compte est un DIFFÉRENTIEL d'inventaire**, jamais la réponse du geste. Un
  `DELETE` qui ne trouve rien annonce « zéro ligne » exactement comme un `DELETE` qui
  vient de tout prendre ;
- **le mode à blanc annonce TOUTE la surface** qu'`--apply` emporterait — nœuds
  recopiés **et blocs attachés** (`blocs_attaches`). Il taisait les seconds, soit
  34 314 lignes au 01/09, la plus grosse part de ce qui partait : un inventaire dont le
  rôle est de dire ce qu'on s'apprête à détruire et qui en tait l'essentiel donne
  confiance à tort (#800, point ②).

⚠️ **Les blocs partent AVANT leur nœud** — mais depuis le 2026-09-01 c'est une
question de COÛT, plus d'intégrité : la clé étrangère les emporterait de toute façon,
un nœud à la fois, là où un `DELETE` ensembliste prend le lot entier.

⚠️ **Ce travail ne balaie plus les blocs ORPHELINS**, et c'est le point ③ de #800. Il le
faisait sans aucun prédicat de provenance : il emportait tout bloc sans nœud, y compris
le corps d'une page **native** dont le nœud venait d'être supprimé par la fuite ci-dessus
— sous un nom qui promettait de ne toucher qu'à la copie. Le borner « au résidu marqué »
est impossible : un orphelin n'a plus de nœud, donc plus de marque. La contrainte règle
la question autrement — ce qu'un balai aurait dû borner, elle l'empêche de naître.
`blocs_orphelins` reste dans l'inventaire, mais comme **témoin** : il doit valoir 0, et un
non-zéro dit que la contrainte manque sur cette base.

Ce qui pend à un nœud a été **mesuré** avant d'écrire le retrait : aucun partage ne
désigne un nœud, les 22 embeddings de nœuds sont tous natifs, et aucun nœud natif n'a
pour parent un nœud recopié. (La ligne « aucune clé étrangère » de ce relevé est périmée
depuis le 2026-09-01 : il y en a une, `blocks_node_fk`.)

La recherche ne perd rien : elle indexe `docs`, `projects` et `datastore_rows` **en
propre**, et ne lit `nodes` que pour les couches de contexte.

## Ce que le retrait emporte, et qui doit être décidé avant

Trois surfaces lisent aujourd'hui le résidu et deviendront vides sans lui :

1. **le contrat du dashboard** — ouvrir par la surface nœud un contenu créé dans
   l'ancienne. Trois tests sont marqués en échec **attendu strict** : s'ils repassent
   au vert, c'est qu'une recopie est revenue ;
2. **`oto_node_rows` sur un tableau recopié** — il résout son namespace par
   `props.legacy_id` ;
3. **la référence de procédure** (`node_procedure_ref`), qui vise la famille produite
   par la conversion.

⚠️ **L'identifiant dérivé et les poignées `doc_id`/`project_id` sont SERVIS** au
dashboard (`db/shell.py`), qui est le premier consommateur de ce nouvel univers. Les
retirer est un **changement de contrat**, pas un déblaiement de fin de chantier : cela
ne se décide pas ici.

⚠️ **Vocabulaire.** Les tests parlent de « front tiers » : c'est un héritage, pas une
description. Le consommateur de cette surface est **le nouveau dashboard produit**, et
c'est pour lui qu'elle est construite — pas pour un intégrateur extérieur. Lire « tiers »
comme « partenaire externe » fait surestimer le coût d'un changement de contrat.

## Ce qui n'est pas encore porté

- **La file de travail.** `nodes` porte désormais les **cinq** colonnes de bail
  (`claimed_by`, `claimed_until`, `claimed_run`, `claims`, `abandon_reason`), mais le
  chemin de réservation lit encore `datastore_rows`. ⚠️ **L'index du bail n'est pas
  posé**, et c'est délibéré : un index sur un prédicat que personne n'interroge est un
  coût d'écriture pur, et sa forme utile dépend d'un arbitrage — toute forme indexable
  en partiel change l'ordre observable de la file. Un test vérifie qu'il n'est pas là.
- **Filtre, recherche et tri sur un tableau natif.** Ils sont **refusés, pas ignorés** :
  les servir demanderait de fouiller la donnée métier, donc de l'interpréter. Les
  accepter en silence ferait croire à un filtre appliqué sur une page complète.
  ⚠️ **Et depuis le 01/09 (#621), le chemin RECOPIÉ refuse pour la même raison** une
  entrée de `filter` sans `:` (`400 invalid_filter`) : elle était ignorée, et la page
  repartait non filtrée sans le dire — le geste que l'alinéa ci-dessus interdit,
  commis sur l'autre provenance. Un curseur illisible y rend `400 invalid_cursor` (il
  sortait en 500), et sous le MÊME code que le chemin natif : la provenance d'un
  tableau n'est pas servie, un front ne peut donc pas prévoir lequel des deux il aura.

## Où vit quoi

| module | rôle |
|---|---|
| `db/schema/nodes.py` | le DDL : la table, ses colonnes, ses **deux** index de requête, et la clé étrangère `blocks_node_fk` d'une base neuve |
| `db/_init.py::_pose_cascade_blocs` | la même clé étrangère, pour une base qui existe déjà |
| `db/nodes.py` | pages natives, position dans la fratrie, retrait du résidu |
| `db/node_tables.py` | tableaux et lignes **natifs** — écriture et pagination |
| `db/node_view.py` | ouvrir UN nœud |
| `db/blocks.py` | le corps d'une page, en blocs à identité stable |
| `db/shell.py` | le rail de navigation servi au front |
| `capabilities/node_*.py` | les surfaces MCP + REST |
| `maintenance.residu_projete` | le retrait du résidu |
