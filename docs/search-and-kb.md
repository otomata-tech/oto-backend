---
title: Recherche transverse & KB projets
type: reference
description: >-
  `oto_search` : un seul chemin de code (RRF lexical + sémantique), les grains matchés (page
   / ligne / tableau / fichier), l'invariant « cherchable ⇔ lisible » et son tripwire, les e
  mbeddings Mistral, l'épine de projet, les backlinks et les propositions de modification.
---

# Recherche transverse & KB projets (`oto_search`)

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## `oto_search` — le verbe « retrouver »

**`oto_search`** (capacité `me.search`, MCP + `GET /api/me/search`) = LE verbe « retrouver »,
un seul chemin de code (`search.py` orchestration RRF k=60 · `db/search.py` SQL par source +
**expressions d'index = source unique index↔requête**, GIN d'expression, config `french` +
repli d'accents `translate`). Sources : pages/briefs/procédures/guides (passages, ts_headline
sur la saisie BRUTE) ∪ tableaux/fichiers/connecteurs (conteneurs, matchés en mémoire).
⚠️ **Deux grains distincts pour un tableau** : `tableau` = le CONTENEUR, matché en mémoire
sur le seul **nom du namespace + les labels de colonnes** ; `ligne` = le CONTENU des
tableaux (#67 V2.1, `_match_rows`), FTS sur les lignes elles-mêmes. Chercher « tableau »
seul ne trouvera donc jamais une valeur DANS une ligne — c'est `ligne` qu'il faut. Un
**fichier** reste matché sur `filename+title+description`, **jamais son contenu**.
**Invariant « cherchable ⇔ lisible »** : docs/briefs/fichiers scopés
`ownership.accessible_project_ids` (factorisation du scoping d'`op=list` — JAMAIS
`can_access`, cross-org) ; **tripwire par source = critère de merge**
(`test_search_scope_tripwire.py`). Le catalogue connecteurs est INJECTÉ par la capacité
(pas d'inversion de couche). `oto_doc(op=search)` = rerouté, déprécié. Fichiers matchés sur
`filename+title+description` (jamais `summary`, colonne morte).

## Sémantique + RRF (20/07)

**Sémantique + RRF (20/07, LIVE preprod)** : fusion LEXICAL + SÉMANTIQUE des pages.
`embeddings.py` = client Mistral `mistral-embed` (1024) — **sync `embed_texts`** (worker, batch DÉCOUPÉ sous le
budget de tokens/requête : 400 « too many tokens overall » sinon ; cap ~16k ch/input)
+ **async `embed_query`** (chemin requête). Outbox `docs.embed_dirty` (marqué à
create/update, coût nul) + `doc_embeddings(halfvec(1024))` + index HNSW cosine ; worker
`embed_worker` (boucle de fond composée au lifespan, embed HORS event loop via
`run_in_threadpool`, idempotent par `content_sha`) draine. Handler `oto_search` ASYNC :
embed la requête hors boucle → `search.search(query_embedding=…)` ajoute la source
`page`/`matched_by='semantic'`, la fusion RRF DÉDUPLIQUE (kind,ref) + SOMME les rangs
(une page trouvée par les deux remonte ; passage lexical conservé). **Dégradation
gracieuse** : sans `MISTRAL_API_KEY` ou sur échec → lexical seul, jamais un prérequis.
pgvector 0.8.2 sur otomata-main (`CREATE EXTENSION vector` AVANT `_SCHEMA` car halfvec en
dépend). Le **golden set JB** cale désormais la QUALITÉ (plus le *si*).

## Se repérer : chapô, ordre curé, épine, backlinks, propositions

**Se repérer** : `docs.description` (chapô ; fallback DÉRIVÉ À LA LECTURE `derive_description`,
jamais stocké) + `docs.position` (ordre curé, entiers ×16 ; `move_doc(parent?, position=INDEX)`
réindexe la fratrie ATOMIQUEMENT) + **épine** `oto_project(op=get, include=['spine'], from_doc?,
depth?)` bornée (N+2, plafond 200, compteurs `more`) — la carte que l'agent lit avant
`oto_doc(op=get)`, jamais `op=list` de tout. **KB d'org ancrée PAR ID** (`orgs.kb_project_id`,
claim optimiste anti-doublon, auto-réparation transfert/archive — le nom n'est plus un marqueur).
⚠️ **Le verbe qui CRÉE cette base s'appelle `create` depuis le 04/09 ; il s'appelait
`ensure`, et le nom était le bug.** « ensure X » est un idiome de développeur —
*get-or-create* — dont le mot ne porte aucune trace de l'écriture : un agent qui entend
« ma knowledge base » l'appelait en croyant vérifier, et posait un projet possédé par
l'org, visible de TOUS ses membres. Vécu : un document présenté comme personnel et
marqué non diffusable, exposé le temps qu'on s'en aperçoive. Les deux verbes disent
désormais ce qu'ils font — `op=get` lit l'ancre et ne crée rien (`project_id: null` si
l'org n'a pas de base), `op=create` crée la base de l'ORG et la ré-ancre si la
précédente a été archivée ou transférée. `create` est idempotent : une org qui en a
déjà une la récupère avec `created: false`, jamais un doublon. `op=ensure` reste dans
l'énumération (`kb.OPS_RETIREES`) pour REFUSER en nommant les trois chemins — lire,
créer la base d'org, ou faire un espace à soi (`oto_project op=create`,
`owner_type='user'`) : retiré du `Literal`, pydantic rendrait « Input should be 'get'
or 'create' », qui n'apprend rien. ⚠️ **Ce verbe est le seul qui pose l'ancre**
(`claim_kb_project`) — le retirer sans successeur laisserait toute org neuve sans base
possible. Toute vue de projet porte `visible_to`, en clair, la portée réelle.
Le lien `project_links.target_type='doc'` est RETIRÉ ; relier des pages =
les **backlinks `[[…]]`** (Ship 4, LIVE) : résolus À L'ÉCRITURE (hook `db.create/update/
delete_doc` — JAMAIS capacité, `resolve_change` appelle db en direct), précédence projet >
KB (`db/backlinks.py`), table dérivée `doc_links` (CASCADE 2 côtés), `oto_doc op=backlinks`
= « Cité par » filtré accès.
⚠️ **Le graphe n'est PAS symétrique, et ce n'est pas un bug d'index (#611, 03/09).** La
portée de résolution EST le scope `[projet, KB]` — donc une page qui vit dans la **KB**
résout contre la KB SEULE et ne peut jamais lier une page de projet, pendant que cette
page de projet la lie sans peine. Signalé le 28/08 sur une carte de tête citant six pages
en tableau ET en ligne de liens, dont aucune ne la voyait en retour ; quatre hypothèses
avaient été éprouvées et écartées avant d'arriver ici, et **un `op=update` complet ne
répare rien** puisque la résolution est la même. Conséquence à connaître : *`op=backlinks`
ne vaut pas comme contrôle de complétude ni d'orphelin* — une page bien citée depuis la
carte de l'org s'y lit comme orpheline. Élargir la portée à tous les projets a été écarté :
« Start Here » résoudrait n'importe où, et chaque écriture deviendrait un scan de toute
l'org. **Ce qui manquait n'était pas la portée, c'était de SAVOIR** : un lien-souche n'est
stocké nulle part et n'était dit nulle part. Toute écriture rend donc
`citations_sans_cible` (+ son hint, qui dit la conséquence ET la cause), et la description
servie porte l'asymétrie. Reproduit sur banc factice, pas déduit.
⚠️ **La portée d'ÉCRITURE n'est pas celle de LECTURE, et les deux surfaces se
contredisaient (#696, 03/09, mesuré sur vrai PG).** `refresh_links` résout dans
`[projet, KB]` ; `backlinks_of` rend **toute** ligne `doc_links` pointant vers la page,
**sans aucun filtre de projet** — seul l'accès borne, au call-site. Et rien ne recale les
liens **entrants** d'une page déplacée (`move_doc_to_project` ne re-résout que les
**sortants** des pages déplacées) : une ligne stockée **survit** au déplacement de sa
cible, hors de toute portée de résolution, et ne meurt qu'à la prochaine écriture de la
page qui cite. D'où le signal : l'accusé d'écriture jurait « ce projet puis la KB — et
rien d'autre » pendant qu'`op=backlinks` affichait des entrants venus d'un autre projet ;
faute de savoir laquelle fait foi, un agent a réécrit **tous** ses renvois inter-projets
en clair et perdu la navigation. Les deux disaient vrai de périmètres différents (et la
KB *est* un projet : un backlink ordinaire est déjà inter-projets). Les deux surfaces le
DISENT désormais — hint d'écriture, description servie — et le cran est en outre porté par
`op=move`, **du côté qui le subit**. ⚠️ Corollaire à démentir partout où il traîne :
« déplacer une page est gratuit, le titre est la clé » est **faux** ; après une
réorganisation, réécrire les pages qui citent et lire leur `citations_sans_cible`. Banc :
`tests/test_backlinks.py::test_un_lien_STOCKE_survit_au_deplacement_de_sa_cible_et_reste_rendu`
(base PG dédiée, chemin servi).
**Propositions modif+création + inbox** (Ship 3, LIVE) : « les
lecteurs proposent » — un viewer (lecture sans écriture) qui crée/modifie obtient une
PROPOSITION (`doc_change_requests`, `doc_id` nullable + `project_id` + emplacement + CHECK) ;
le dispatch `docs/core.py` route resolve/list/create-proposal sur request_id/project_id **AVANT
le gate doc_id** (une création doc_id NULL était sinon inatteignable) ; `me.inbox`
(`GET /api/me/inbox`, 2 voies À traiter/Récent, 200-vide sans org).

### Le classement est un cache, pas une seconde source

`search_vec` accélère le classement ; le filtre, lui, reste l'expression indexée du
contenu courant. Les écritures de docs, lignes, textes extraits, briefs/projets,
procédures et pages/guides natifs maintiennent ce cache **dans leur transaction**.

`stamp_rank_vector` **invalide d'abord à NULL**, puis recalcule sous savepoint.
L'ordre n'est pas un détail : le rattrapage de fond ne reprend que les vecteurs
absents. Une ligne qui gardait son ancien vecteur après un recalcul raté n'était
donc **jamais** reprise — le classement n'était pas « daté de quelques secondes »
comme le promettait le commentaire, il était faux indéfiniment.

Donc : l'invalidation est **obligatoire** — si elle échoue, l'écriture échoue, parce
qu'une colonne absente est une erreur de schéma et non une optimisation qu'on saute.
Le recalcul, lui, reste best-effort et **journalisé** : le contenu reste écrit, le
`COALESCE` calcule le rang sur le texte courant, et le worker reprend les NULL.
C'est un coût de calcul explicite, jamais une lecture périmée tolérée.

Ce maintien ne recopie aucun ancien contenu vers `nodes` : il tient une projection
du contenu actif, ce qui est l'inverse d'une synchronisation entre deux modèles.

## Seam `pending_action`

**Seam `pending_action`** (`status_hints.py`, patron connector_verify) : un connecteur à
connexion en deux temps enregistre un hook « quelle étape manque ? » → `ProviderStatus.
pending_action` (fail-open) que le front rend tel quel en verdict+CTA. La spécificité vit
DANS le module connecteur (unipile : « Connecte un canal »), jamais dans le modèle commun.
