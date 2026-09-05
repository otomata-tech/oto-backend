"""Conversion des projets, des pages et des tableaux en NŒUDS — lots M2 et M3.

Le lot M1 a fait des couches de contexte (`guides`) des nœuds. Celui-ci fait le
reste du contenu : les **projets** et les **pages** (blueprint ADR 0054/0063,
oto-backend#287), puis les **tableaux** (`user_datastores`, #301). C'est la
conversion structurante — celle où le modèle unique cesse d'être une idée.

**Le projet devient une ÉPINGLE** (0054-D5). Il ne disparaît pas en tant que
contenu, il disparaît en tant qu'*objet* : c'est un nœud comme un autre, marqué
`props->>'pinned'`, dont le nom devient le titre, le brief devient le **corps**, et
dont les pages deviennent l'**arbre**. L'épingle donne deux choses, et rien d'autre :
la borne de localisation (« vous êtes dans *Refonte de la marque* ») et le grain du
contexte dynamique.

**Le point dur du lot est l'ownership** (0063-D1, §2 du chantier). Il vit sur
`projects` (`owner_type`/`owner_id`), **pas sur `docs`** : une page n'a jamais eu de
propriétaire, elle héritait de celui de son projet par la contrainte
`project_id NOT NULL`. La conversion, c'est exactement **poser ce propriétaire sur
chaque page** — et c'est ce couple qui interdisait d'étendre la table des pages.

**Le namespace d'un tableau devient une POSITION** (0054-D4, lot M3). Le système de
nommage parallèle des datastores disparaît : un tableau est un nœud, nommé par sa
place dans l'arbre — sous le nœud du projet qui le lie, à défaut à la racine de son
propriétaire. Et son schéma de colonnes descend dans les propriétés : c'est la
**dimension**, le schéma que ses enfants porteront. ⚠️ **Les LIGNES ne bougent pas**
(lot M4, le volume en dernier), ni le bail de la file de travail qui vit sur elles.

## Ce que ce module fait, et ce qu'il ne fait pas

**Il PROJETTE, à chaque boot** : `projects`, `docs` et `user_datastores` restent la
source de vérité et la cible des écritures ; `nodes` en reçoit une image fidèle,
rafraîchie par `_init`. Tant que la bascule n'est pas faite, **personne ne lit ces
nœuds-là**.

**La BASCULE DE LECTURE (0063-D4) n'est PAS dans ce lot**, et ce n'est pas un renvoi
de travail : c'est une **décision** qui n'a pas été prise, mesurée ici pour que le
lot suivant parte de faits.

1. **Un nœud ne peut pas garder l'id de sa ligne legacy.** `docs.id` et
   `projects.id` sont deux séquences INDÉPENDANTES qui convergent vers UNE table :
   la page 12 et le projet 12 ne peuvent pas être tous deux `nodes.id = 12`, et les
   24 nœuds du lot M1 occupent déjà le bas de la séquence. L'id legacy vit donc dans
   `props->>'legacy_id'`. Or **les surfaces distribuent cet id** — routes du
   dashboard (`/data/:id`), `oto_doc`, `project_links.target_ref`,
   `resource_grants(resource_type='project')`, `runs.project_id`,
   `orgs.kb_project_id`, la portée d'un jeton porté (`{"projects": {"12": "read"}}`).
   Lire depuis `nodes` en gardant les surfaces à l'identique impose donc de projeter
   `props->>'legacy_id'` comme `id` — ce qui n'est plus une conversion, c'est un
   régime.
2. **La lecture ne peut pas basculer sans l'écriture.** La conversion tourne au
   BOOT : une page créée après lui n'est pas dans `nodes`. Lire `nodes` en écrivant
   `docs` servirait une page qui n'existe pas encore.
3. **L'écriture ne peut pas basculer sans les satellites.** 13 colonnes de 8 tables
   pointent `docs(id)` / `projects(id)` en clé étrangère (révisions, propositions,
   backlinks, embeddings de page et de chunk, liens de projet, journal, fichiers).
   Une page qui ne vivrait QUE dans `nodes` n'a pas de ligne `docs` : son premier
   `update_doc` violerait la FK de `doc_revisions`. Déplacer ce keying est un lot en
   soi — et il touche `doc_revisions`, dont 0063-D2 dit qu'elle ne bouge pas.

Le pan qui manque est donc : *que deviennent les satellites, et l'identifiant que
les surfaces ont déjà distribué ?* Tant que ce n'est pas tranché, projeter est ce
qu'on peut livrer qui tienne.

**⚠️ L'invariant que ce lot doit tenir, et que rien ne gardait avant lui** : la
recherche des couches de contexte (`db/search.py`, `db/aux_embed.py`) discrimine un
guide par `props->>'delivery' = 'on-demand'`, **pas par le `kind`** — or les pages et
les projets convertis arrivent ici en `kind='page'` eux aussi. **Aucun nœud converti
ne porte de `delivery`**, sans quoi il se met à remonter comme un guide, dans le scope
des guides. Tenu par `tests/test_nodes_m2_conversion.py` et son pendant M3, contre un
vrai PostgreSQL.

## La forme des conversions (mêmes techniques qu'au lot M1)

Chaque famille se convertit en **trois temps**, tous rejouables :

1. **le contenu** — un `INSERT … ON CONFLICT (public_id) DO UPDATE` **newer-wins**
   (`WHERE EXCLUDED.updated_at > nodes.updated_at`) : rejouer sans écriture entre
   deux passes est intégralement no-op, et une écriture faite par la PROD pendant la
   fenêtre de promotion est rattrapée au boot suivant. ⚠️ **Les tableaux font
   exception** : `user_datastores` ne porte pas d'`updated_at`, donc l'arbitre y est
   le CONTENU (mêmes deux propriétés, cf. `CONVERT_TABLES_TO_NODES_SQL`) ;
2. **la structure** — un `UPDATE` qui réconcilie propriétaire, parent et rang
   **quoi qu'en dise `updated_at`**. Gardé par un `IS DISTINCT FROM` → no-op au
   rejeu ;
3. **la purge** — les nœuds dont la ligne legacy n'existe plus. Sans elle, un
   contenu supprimé survivrait dans `nodes` jusqu'à la fin des temps.

Une **famille** distincte par source (`prj`, `doc`, `tbl`) : elle sépare les
identifiants publics de trois séquences indépendantes, et elle borne chaque purge à
ce qu'elle a elle-même écrit.

Tout est gardé `to_regclass` par l'appelant (docs/live-migrations.md) : après le DROP
des tables legacy, un boot reste un no-op au lieu de casser, quel que soit l'ordre
des déploiements.
"""
from __future__ import annotations

import json
import secrets
from typing import Optional

from ._conn import _connect

# --- Identité publique (0059-D3) ---------------------------------------------

# ⚠️ La FAMILLE est ce qui empêche deux identifiants de se recouvrir. `docs.id` et
# `projects.id` sont deux séquences INDÉPENDANTES qui convergent vers une seule
# table : sans préfixe distinct, la page 12 et le projet 12 réclameraient le même
# identifiant public et l'une écraserait l'autre au premier boot, en silence.
_FAMILY_PROJECT = "prj"
_FAMILY_DOC = "doc"


def _public_id_sql(family: str, id_expr: str) -> str:
    """Le SQL de l'identifiant public d'un nœud converti, DÉRIVÉ de sa clé legacy.

    Même raisonnement qu'au lot M1 (`db/guides.py`) : dérivé, et pas tiré au sort,
    parce que c'est ce qui rend la conversion REJOUABLE sans index supplémentaire —
    la même ligne convertie deux fois produit le MÊME identifiant, `ON CONFLICT`
    arbitre, personne ne duplique. La clé dérivée doit être IMMUABLE : un id de
    séquence l'est (un projet ne change pas d'id ; il peut changer de nom, de
    propriétaire et de place — aucun n'entre ici).

    ⚠️ Ne pas généraliser : un nœud NATIF (créé par une surface, pas converti) tire
    son identifiant au sort, comme le veut 0059-D3.
    """
    return f"'nod_' || substr(md5('{family}:' || ({id_expr})::text), 1, 24)"


# Le genre d'un projet converti — et, au lot suivant, d'une page convertie. **Une
# seule valeur pour les deux**, et ce n'est pas un raccourci : l'épingle est un FLAG
# posé sur un nœud ordinaire (0054-D5), pas un genre. Un `kind='project'`
# réintroduirait l'objet que ce lot retire.
_KIND = "page"


# --- Projets → nœuds épinglés ------------------------------------------------

# Le brief devient le CORPS (`body_md`, la même clé que les couches de contexte —
# donc indexé par la même expression de recherche, cf. `db/search.NODES_TEXT`), le
# nom devient le TITRE, et `pinned` porte l'épingle.
#
# ⚠️ Aucune clé `delivery` ici, et c'est un invariant, pas une omission : la
# recherche des guides discrimine là-dessus (cf. l'en-tête du module).
#
# `jsonb_strip_nulls` : une icône absente ne doit pas s'écrire `"icon": null` — une
# clé présente à null se lit comme une valeur, et `props ? 'icon'` répondrait vrai.
#
# NE SONT PAS PORTÉS ici, délibérément : la machinerie de PUBLICATION du projet
# (`mcp_slug`, `mcp_access`, `mcp_tools`, `mcp_expose_*`, `mcp_instructions_md`) et
# le lignage de fork (`copied_from`). Ce sont des propriétés de l'objet-projet et de
# ses surfaces, pas du contenu — elles suivront la bascule des surfaces, avec les
# questions qu'elles posent (un endpoint publié, est-ce une propriété de nœud ?).
# Rien n'est perdu : la table `projects` reste intacte.
CONVERT_PROJECTS_TO_NODES_SQL = f"""
    INSERT INTO nodes (public_id, kind, owner_type, owner_id, props,
                       created_at, updated_at)
    SELECT {_public_id_sql(_FAMILY_PROJECT, 'p.id')},
           '{_KIND}', p.owner_type, p.owner_id,
           jsonb_strip_nulls(jsonb_build_object(
               'legacy', '{_FAMILY_PROJECT}', 'legacy_id', p.id,
               'pinned', TRUE,
               'title', COALESCE(p.name, ''),
               'body_md', COALESCE(p.brief_md, ''),
               'icon', p.icon,
               'is_template', p.is_template,
               'archived_at', p.archived_at,
               'context_org_id', p.context_org_id,
               'created_by', p.created_by)),
           p.created_at, p.updated_at
      FROM projects p
    ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET
        props = EXCLUDED.props, updated_at = EXCLUDED.updated_at
     WHERE EXCLUDED.updated_at > nodes.updated_at
"""

# Réconciliation STRUCTURELLE, hors newer-wins. Un projet épinglé est une RACINE
# (`parent_id IS NULL`) : c'est le contrat de l'épingle — on remonte jusqu'à elle,
# pas au-delà. Le propriétaire, lui, change sans que le contenu bouge
# (`reparent_project` réécrit `owner_*` et rien d'autre de substantiel), donc il ne
# peut pas dépendre d'une comparaison d'horodatage.
# Piloté par `nodes.public_id` (index d'identité), pas par un prédicat sur `props` :
# la table portera des millions de lignes en M4, ce balayage-là ne doit pas naître.
RECONCILE_PROJECT_NODES_SQL = f"""
    UPDATE nodes n
       SET owner_type = p.owner_type, owner_id = p.owner_id, parent_id = NULL
      FROM projects p
     WHERE n.public_id = {_public_id_sql(_FAMILY_PROJECT, 'p.id')}
       AND (n.owner_type, n.owner_id, n.parent_id)
           IS DISTINCT FROM (p.owner_type, p.owner_id, NULL::bigint)
"""

# Une page ou un projet supprimé laisserait sinon son nœud derrière lui pour
# toujours — et la projection cesserait d'être fidèle sans que rien ne le dise.
#
# ⚠️ **Le prédicat porte sur `props->>'legacy'`, et c'est ce qui rend la purge sûre
# le jour de la bascule d'écriture** : un nœud NATIF (créé par une surface, sans
# ligne legacy) n'a pas cette clé, donc n'est jamais candidat. Ne pas relâcher ce
# prédicat en croyant simplifier — ce serait effacer le contenu neuf.
PURGE_PROJECT_NODES_SQL = f"""
    DELETE FROM nodes n
     WHERE n.props->>'legacy' = '{_FAMILY_PROJECT}'
       AND NOT EXISTS (SELECT 1 FROM projects p
                        WHERE p.id = (n.props->>'legacy_id')::bigint)
"""


# --- Pages → nœuds ------------------------------------------------------------

# **LE POINT DUR DU LOT, et il tient en une ligne** : `p.owner_type, p.owner_id` —
# le propriétaire du PROJET, posé sur chaque page.
#
# L'ownership vit sur `projects`, **pas sur `docs`** (0063-D1, §2 du chantier) : une
# page n'a jamais eu de propriétaire, elle héritait de celui de son projet par la
# contrainte `project_id NOT NULL`. C'est ce couple qui interdisait d'étendre la
# table des pages — retirer la contrainte laisserait des pages sans propriétaire, et
# le poser sur chaque page EST la conversion. Tout le reste de ce fichier déménage
# des colonnes ; cette ligne-là crée quelque chose qui n'existait pas.
#
# `docs.kind` ('doc' humain | 'note' agent | 'source' import) devient
# `props->>'doc_kind'` : la colonne `kind` du nœud dit ce que l'objet EST (une page),
# pas d'où il vient. Les confondre rendrait la provenance structurante — et
# rappellerait le `kind='guide'` que M1 a précisément dissous.
#
# `public_token` voyage tel quel (**une seule page en porte un** en production,
# relevé du 11/08). Savoir s'il DEVIENT un accès du modèle de 0053 se tranchera
# quand la chaîne de grants sera vivante : décider maintenant, ce serait calibrer un
# modèle d'accès sur une population de un — l'erreur exacte qui a produit #282.
CONVERT_DOCS_TO_NODES_SQL = f"""
    INSERT INTO nodes (public_id, kind, owner_type, owner_id, position, props,
                       created_at, updated_at)
    SELECT {_public_id_sql(_FAMILY_DOC, 'd.id')},
           '{_KIND}', p.owner_type, p.owner_id, d.position,
           jsonb_strip_nulls(jsonb_build_object(
               'legacy', '{_FAMILY_DOC}', 'legacy_id', d.id,
               'title', COALESCE(d.title, ''),
               'description', d.description,
               'body_md', COALESCE(d.body_md, ''),
               'doc_kind', d.kind,
               'public_token', d.public_token,
               'project_id', d.project_id,
               'created_by', d.created_by)),
           d.created_at, d.updated_at
      FROM docs d JOIN projects p ON p.id = d.project_id
    ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET
        props = EXCLUDED.props, position = EXCLUDED.position,
        updated_at = EXCLUDED.updated_at
     WHERE EXCLUDED.updated_at > nodes.updated_at
"""

# L'ARBRE, et le propriétaire hérité. Deux raisons de sortir ceci du newer-wins, et
# la seconde est un piège silencieux :
#
# 1. **le parent ne peut pas être résolu à l'insertion** — le nœud du parent peut
#    naître dans le MÊME `INSERT … SELECT`, donc n'être visible qu'après ;
# 2. **le propriétaire d'une page ne dépend pas de son horodatage** : transférer un
#    projet (`reparent_project`) ne touche AUCUNE ligne de `docs`. Sous newer-wins
#    seul, `EXCLUDED.updated_at > nodes.updated_at` serait faux pour toutes ses
#    pages — qui resteraient donc chez l'ANCIEN propriétaire, indéfiniment et sans
#    un mot. C'est précisément la classe de panne que ce lot a pour objet d'éviter.
#
# Le rattachement : le nœud de `docs.parent_id` s'il existe, sinon celui du PROJET.
# C'est ce qui fait de l'épingle la racine de son sous-arbre (0054-D5) — une page de
# premier niveau n'était rattachée à son projet que par une colonne, elle l'est
# maintenant par l'arbre.
RECONCILE_DOC_NODES_SQL = f"""
    UPDATE nodes n
       SET owner_type = p.owner_type, owner_id = p.owner_id,
           parent_id = COALESCE(par.id, prj.id)
      FROM docs d
      JOIN projects p ON p.id = d.project_id
      JOIN nodes prj ON prj.public_id = {_public_id_sql(_FAMILY_PROJECT, 'd.project_id')}
      LEFT JOIN nodes par
             ON d.parent_id IS NOT NULL
            AND par.public_id = {_public_id_sql(_FAMILY_DOC, 'd.parent_id')}
     WHERE n.public_id = {_public_id_sql(_FAMILY_DOC, 'd.id')}
       AND (n.owner_type, n.owner_id, n.parent_id)
           IS DISTINCT FROM (p.owner_type, p.owner_id, COALESCE(par.id, prj.id))
"""

PURGE_DOC_NODES_SQL = f"""
    DELETE FROM nodes n
     WHERE n.props->>'legacy' = '{_FAMILY_DOC}'
       AND NOT EXISTS (SELECT 1 FROM docs d
                        WHERE d.id = (n.props->>'legacy_id')::bigint)
"""


def convert_projects(conn) -> None:
    """Projets → nœuds épinglés : contenu, structure, purge. Rejouable."""
    conn.execute(CONVERT_PROJECTS_TO_NODES_SQL)
    conn.execute(RECONCILE_PROJECT_NODES_SQL)
    conn.execute(PURGE_PROJECT_NODES_SQL)


# ══ Lot ⑧ — les PROCÉDURES deviennent des nœuds ═══════════════════════════════
#
# Dernière famille convertie, et celle qui ferme un trou VISIBLE depuis le lot ③ : un
# partage direct de procédure (`resource_grants.resource_type = 'doctrine'`) ne désignait
# aucun nœud, donc n'entrait pas dans la section « Partagé » du rail. On le comptait
# (`grants_sans_noeud`) pour qu'une section vide ne se lise pas « rien de partagé » —
# la conversion le fait tomber à zéro.
#
# ⚠️ **La clé dérivée est `org_instructions.id`, pas `(owner, slug)`.** Le slug est la
# clé NATURELLE de la table, mais il se renomme ; l'`id` (colonne de séquence posée par
# `_init`) ne bouge jamais, et c'est exactement lui que `resource_grants` désigne
# (`get_instruction_by_id(int(resource_id))`). Dériver du slug aurait produit un
# identifiant de nœud qui change au premier renommage — la classe de défaut que #362
# vient de retirer sur les blocs.
#
# `kind = 'page'` comme tout le reste : une procédure est de la prose possédée par un
# scope. 0054-D5 — le genre dit ce que l'objet EST, et ce qu'il JOUE (procédure, agent)
# est un rôle porté en propriété, jamais un `kind` de plus. Même arbitrage qu'au lot ⑦.
_FAMILY_GUIDE = "prc"

CONVERT_GUIDES_TO_NODES_SQL = f"""
    INSERT INTO nodes (public_id, kind, owner_type, owner_id, props,
                       created_at, updated_at)
    SELECT {_public_id_sql(_FAMILY_GUIDE, 'd.id')},
           '{_KIND}', d.owner_type, d.owner_id,
           jsonb_strip_nulls(jsonb_build_object(
               'legacy', '{_FAMILY_GUIDE}', 'legacy_id', d.id,
               'role', 'procedure',
               'slug', d.slug,
               'title', COALESCE(NULLIF(d.title, ''), d.slug),
               'description', NULLIF(d.description, ''),
               'body_md', COALESCE(d.body_md, ''),
               'slots', NULLIF(d.slots, '[]'::jsonb),
               'doctrine_version', d.version,
               'created_by', d.set_by,
               'org_id', d.org_id))
           || CASE WHEN d.slots IS NULL OR d.slots = '[]'::jsonb THEN '{{}}'::jsonb
                   ELSE jsonb_build_object('slots', d.slots) END,
           d.created_at, d.updated_at
      FROM org_instructions d
     WHERE d.id IS NOT NULL
    ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET
        props = EXCLUDED.props, updated_at = EXCLUDED.updated_at
     WHERE EXCLUDED.updated_at > nodes.updated_at
"""

# Le propriétaire d'une procédure change sans que son contenu bouge (transfert de
# ressource) : comme pour un projet, il ne peut pas dépendre d'un newer-wins.
RECONCILE_GUIDE_NODES_SQL = f"""
    UPDATE nodes n
       SET owner_type = d.owner_type, owner_id = d.owner_id
      FROM org_instructions d
     WHERE n.public_id = {_public_id_sql(_FAMILY_GUIDE, 'd.id')}
       AND (n.owner_type, n.owner_id) IS DISTINCT FROM (d.owner_type, d.owner_id)
"""

PURGE_GUIDE_NODES_SQL = f"""
    DELETE FROM nodes n
     WHERE n.props->>'legacy' = '{_FAMILY_GUIDE}'
       AND NOT EXISTS (SELECT 1 FROM org_instructions d
                        WHERE d.id = (n.props->>'legacy_id')::bigint)
"""


def convert_guides(conn) -> None:
    """Procédures → nœuds : contenu, propriétaire, purge. Rejouable."""
    conn.execute(CONVERT_GUIDES_TO_NODES_SQL)
    conn.execute(RECONCILE_GUIDE_NODES_SQL)
    conn.execute(PURGE_GUIDE_NODES_SQL)


def convert_docs(conn) -> None:
    """Pages → nœuds : contenu, puis propriétaire hérité + arbre, puis purge.

    ⚠️ L'ORDRE avec `convert_projects` compte : le rattachement d'une page de
    premier niveau vise le nœud de son PROJET. S'il n'existe pas encore, la
    jointure ne rend rien et la page reste orpheline jusqu'au boot suivant — un
    arbre à moitié posé, qu'aucune erreur ne signale."""
    conn.execute(CONVERT_DOCS_TO_NODES_SQL)
    conn.execute(RECONCILE_DOC_NODES_SQL)
    conn.execute(PURGE_DOC_NODES_SQL)


# ══ Lot M3 — le rang d'une fratrie, et les tableaux (#301) ═══════════════════

# --- M-g : les positions par INTERVALLE ---------------------------------------

# **L'écart entre deux voisins**, et le seul chiffre de tout ce module qui vienne
# d'une mesure plutôt que d'un goût. 2^16 : seize insertions successives *au même
# endroit* avant que l'intervalle ne soit épuisé, et un million de frères ne
# portent que 6,5e10 — un dix-millionième de ce qu'un BIGINT sait compter.
POSITION_GAP = 1 << 16


def midpoint(after: int | None, before: int | None) -> int | None:
    """Le rang libre entre deux voisins — `None` quand l'intervalle est ÉPUISÉ.

    **C'est la règle M-g du chantier** (blueprint `chantier-modele-contenu.md` §5),
    et elle règle une question de tarif, pas d'élégance. Le banc M0 a chiffré les
    deux gestes possibles pour ordonner une fratrie :

    - **renuméroter la fratrie entière** — le pattern hérité de `docs.position`
      (« entiers espacés, réindexés atomiquement au déplacement », 0063-D2) :
      **20 secondes** sur 45 000 frères ;
    - **insérer dans l'intervalle** entre deux voisins : **1,4 milliseconde**.

    Quatorze mille fois moins cher, et l'écart n'est pas théorique : une table du
    datastore de production porte aujourd'hui 43 584 lignes, qui deviendront autant
    de nœuds frères au lot M4. « Réindexer atomiquement » y coûterait vingt secondes
    de transaction — sur le chemin nominal d'un `data_write`.

    D'où l'inversion que ce module pose : **l'insertion dans l'intervalle est
    l'opération nominale, la réindexation devient un RATTRAPAGE** (`reindex_siblings`),
    joué le jour où l'écart ne peut plus absorber. `None` est ce jour-là.

    Fonction PURE, et ce n'est pas un hasard : le cas qui compte (l'épuisement) ne
    s'observe qu'après seize insertions au même point, ou sur deux voisins collés
    hérités d'ailleurs. Il doit se tester sans base."""
    if after is None and before is None:
        return POSITION_GAP                      # fratrie vide : le premier rang
    if before is None:
        return int(after) + POSITION_GAP         # en fin : l'écart nominal
    if after is None:
        mid = int(before) // 2                   # en tête : la moitié du premier
        return mid if mid >= 1 else None
    mid = (int(after) + int(before)) // 2
    return mid if after < mid < before else None


def _sibling_scope(parent_id: int | None, owner_type: str, owner_id: str) -> tuple[str, list]:
    """Le prédicat SQL d'une FRATRIE, et la définition qui va avec.

    Frères = même parent. **À la racine (`parent_id IS NULL`), même propriétaire** —
    parce que « tous les nœuds sans parent » n'est pas une fratrie mais la table
    entière : deux orgs qui ne se connaissent pas y partageraient un ordre, et le
    premier rattrapage renumérotererait le contenu de tout le monde."""
    if parent_id is not None:
        return "parent_id = %s", [parent_id]
    return "parent_id IS NULL AND owner_type = %s AND owner_id = %s", [owner_type, owner_id]


def reindex_siblings(conn, *, parent_id: int | None, owner_type: str,
                     owner_id: str) -> int:
    """LE RATTRAPAGE : renumérote une fratrie à l'écart nominal. Rend son cardinal.

    ⚠️ **Ceci n'est pas un chemin nominal, et le jour où il le redevient, la règle
    est perdue** (cf. `midpoint` : 20 s contre 1,4 ms). On ne l'appelle que lorsque
    l'intervalle est épuisé — jamais « pour faire propre », jamais à chaque
    déplacement. `ORDER BY position NULLS LAST, id` : un frère jamais placé (rang
    nul) passe en fin, dans l'ordre de sa création."""
    where, params = _sibling_scope(parent_id, owner_type, owner_id)
    rows = conn.execute(
        f"SELECT id FROM nodes WHERE {where} ORDER BY position NULLS LAST, id",
        tuple(params)).fetchall()
    for rank, r in enumerate(rows, start=1):
        conn.execute("UPDATE nodes SET position = %s WHERE id = %s",
                     (rank * POSITION_GAP, r["id"]))
    return len(rows)


def place_after(conn, node_id: int, *, after_id: int | None, parent_id: int | None,
                owner_type: str, owner_id: str) -> int:
    """Place `node_id` juste après son frère `after_id` (`None` = en tête).

    ⚠️ **L'ancre est un NŒUD, pas un rang**, et c'est ce qui rend le rattrapage sûr :
    une réindexation change tous les rangs de la fratrie, donc un rang capturé avant
    elle désignerait ensuite un autre endroit. En repartant de l'identité du frère,
    la seconde passe vise toujours la même place.

    Deux passes au plus : après un rattrapage, l'écart vaut `POSITION_GAP` partout,
    donc `midpoint` ne peut plus refuser."""
    where, params = _sibling_scope(parent_id, owner_type, owner_id)
    for _ in range(2):
        after = None
        if after_id is not None:
            row = conn.execute("SELECT position FROM nodes WHERE id = %s",
                               (after_id,)).fetchone()
            after = None if row is None else row["position"]
        # Le voisin de droite : le plus petit rang STRICTEMENT au-dessus de l'ancre
        # (ou le plus petit de la fratrie quand on insère en tête). `node_id` s'exclut
        # lui-même — un nœud qu'on déplace est déjà de la fratrie, et se prendre pour
        # son propre voisin le figerait sur place.
        sql = (f"SELECT min(position) AS p FROM nodes WHERE {where} AND id <> %s "
               "AND position IS NOT NULL")
        args = list(params) + [node_id]
        if after is not None:
            sql += " AND position > %s"
            args.append(after)
        before = conn.execute(sql, tuple(args)).fetchone()["p"]
        pos = midpoint(after, before)
        if pos is not None:
            conn.execute("UPDATE nodes SET position = %s WHERE id = %s", (pos, node_id))
            return pos
        reindex_siblings(conn, parent_id=parent_id, owner_type=owner_type,
                         owner_id=owner_id)
    raise RuntimeError("rang introuvable après rattrapage")        # pragma: no cover


def place_at_end(conn, node_id: int, *, parent_id: int | None, owner_type: str,
                 owner_id: str) -> int:
    """Place `node_id` en FIN de fratrie — le cas nominal d'une conversion.

    Dégénérescence de `place_after` : après le dernier frère il n'y a pas de voisin
    de droite, donc `midpoint` rend `dernier + POSITION_GAP` et aucun rang existant
    ne bouge. Un lot de conversion coûte ainsi un `UPDATE` par nœud converti, et
    zéro écriture sur ce qui était déjà là."""
    where, params = _sibling_scope(parent_id, owner_type, owner_id)
    row = conn.execute(
        f"SELECT id FROM nodes WHERE {where} AND id <> %s AND position IS NOT NULL "
        "ORDER BY position DESC LIMIT 1", tuple(list(params) + [node_id])).fetchone()
    return place_after(conn, node_id, after_id=(row["id"] if row else None),
                       parent_id=parent_id, owner_type=owner_type, owner_id=owner_id)


# --- Tableaux → nœuds-tableaux (#301) -----------------------------------------

_FAMILY_TABLE = "tbl"

# **Le genre d'un tableau EST `tableau`**, et c'est le seul endroit du chantier où
# un genre s'ajoute plutôt que de se dissoudre. Le raisonnement de M2 (« l'épingle
# est un flag, pas un genre ») ne s'applique pas ici, et la raison est mesurée :
# 0054-D4 fait d'un nœud un tableau **parce qu'il déclare un schéma d'enfants**, or
# **29 des 83 tableaux de production n'en déclarent aucun** (`schema` NULL = table
# libre, colonnes découvertes des lignes). La dimension ne peut donc pas servir de
# discriminant — elle dirait de ces 29-là qu'ils sont des pages. Le `kind` dit ce
# que l'objet EST, la dimension dit ce que ses enfants PORTENT.
_KIND_TABLE = "tableau"

# Les clés de `props` que la conversion POSSÈDE. Tout ce qui n'est pas là (une clé
# posée par une surface, un jour) survit à un rafraîchissement — la projection écrase
# ce qu'elle a écrit, pas ce qu'elle trouve. ⚠️ Liste exhaustive : une clé projetée
# qui manquerait ici ne serait jamais RETIRÉE (un schéma effacé resterait).
_TABLE_PROPS_KEYS = "'{legacy,legacy_id,title,child_schema,semantic_search}'::text[]"

# ── Où vit un tableau dans l'arbre ────────────────────────────────────────────
#
# **Le namespace devient une position** (0054-D4 : « le système de nommage namespace
# disparaît — un tableau est un nœud, nommé par sa place dans l'arbre »). Cette place
# est celle du PROJET qui le lie, à défaut la racine de son propriétaire.
#
# ⚠️ Trois pièges, tous relevés sur la production du 12/08 — aucun ne se devine :
#
# 1. **`project_links.target_ref` n'est pas toujours un id.** 14 liens `tableau` sur
#    65 portent un NOM de namespace (#117 : l'agent lie par nom, le dashboard par id).
#    Un `target_ref::bigint` ferait donc tomber le boot sur `invalid input syntax`.
#    D'où la comparaison en TEXTE (`pl.target_ref = d.id::text`), qui n'a besoin
#    d'aucun garde-fou de forme.
# 2. **Un nom ne désigne un tableau que CHEZ UN PROPRIÉTAIRE** (l'unicité est
#    `(owner_type, owner_id, namespace)`, et 4 noms sont portés par plusieurs
#    propriétaires en production). Un lien par nom ne résout donc que dans le
#    périmètre du projet qui le porte — sinon on rattache le tableau d'autrui. La
#    voie par id, elle, désigne sans ambiguïté : elle n'est pas scopée (3 liens
#    pointent en production un tableau dont le propriétaire diffère de celui du
#    projet — un partage, pas une erreur).
# 3. **Un tableau peut être lié par PLUSIEURS projets** (2 cas en production), alors
#    qu'un nœud n'a qu'un parent. `MIN(project_id)` tranche : le plus ancien lien,
#    donc un arbre STABLE d'un boot à l'autre — le critère importe moins que le fait
#    qu'il ne dépende pas de l'ordre de lecture.
#
# ⚠️ **La place ne transfère pas la propriété.** Un tableau posé sous le nœud d'un
# projet garde SON propriétaire, contrairement aux pages du lot M2 qui n'en avaient
# jamais eu. Une page héritait faute de mieux ; un tableau, lui, en a un depuis la
# Phase H — le lui reprendre serait une régression d'accès déguisée en rangement.
_TABLE_PLACE_CTE = f"""
    lien AS (
        SELECT d.id AS ds_id, MIN(pl.project_id) AS project_id
          FROM user_datastores d
          JOIN project_links pl ON pl.target_type = 'tableau'
          JOIN projects p ON p.id = pl.project_id
         WHERE pl.target_ref = d.id::text
            OR (pl.target_ref = d.namespace
                AND p.owner_type = d.owner_type AND p.owner_id = d.owner_id)
         GROUP BY d.id
    ),
    place AS (
        SELECT d.id AS ds_id, d.owner_type, d.owner_id, prj.id AS parent_id
          FROM user_datastores d
          LEFT JOIN lien ON lien.ds_id = d.id
          LEFT JOIN nodes prj
                 ON prj.public_id = {_public_id_sql(_FAMILY_PROJECT, 'lien.project_id')}
         WHERE d.owner_id IS NOT NULL
    )
"""

# ⚠️ **`user_datastores` n'a PAS d'`updated_at`**, et ce fait commande tout ce bloc.
# Le newer-wins des lots M1/M2 est donc impossible : `EXCLUDED.updated_at >
# nodes.updated_at` serait faux à jamais, et un schéma de colonnes édité par la PROD
# pendant la fenêtre de promotion ne serait JAMAIS rattrapé — la projection mentirait
# en silence, ce qui est exactement le mode d'échec que ces lots cherchent à éviter.
#
# L'arbitre est donc le CONTENU : on récrit quand le résultat diffère de ce qui est
# en base, et pas autrement. Deux propriétés, les mêmes que le newer-wins :
# le rejeu sans écriture est intégralement no-op (`updated_at` compris), et une
# écriture prod de la fenêtre est rattrapée au boot suivant.
#
# La fusion `(props - clés) || EXCLUDED.props` est ADDITIVE là où M1/M2 remplacent :
# elle retire ce que la conversion possède, repose sa version, et laisse intact ce
# qu'un autre aurait écrit. Le prédicat compare le RÉSULTAT de cette fusion à
# l'existant — une seule expression, donc aucune chance qu'un jour la garde et
# l'écriture divergent.
#
# ⚠️ **Le schéma de colonnes ne passe PAS par `jsonb_strip_nulls`** : c'est une
# donnée du CLIENT, pas un champ de la conversion, et `strip_nulls` est RÉCURSIF —
# un `{{"label": null}}` déclaré dans un champ y perdrait sa clé, silencieusement.
# Les champs de la conversion, eux, y passent (une clé absente doit être absente, pas
# présente à null : `props ? 'child_schema'` répondrait vrai).
#
# **Pas de `body_md`** : un tableau n'a pas de corps (0054 §2 — le corps est
# optionnel). Conséquence utile : ces nœuds sortent d'eux-mêmes du parse en blocs,
# qui ne sélectionne que `props ? 'body_md'`. Et **pas de `delivery`** : l'invariant
# de M2 vaut ici sans changement (la recherche des guides discrimine là-dessus).
#
# `position` n'est pas posé ici : le rang se prend dans l'intervalle, nœud par nœud,
# après la purge (cf. `_place_table_nodes`). C'est le point où M-g s'applique.
CONVERT_TABLES_TO_NODES_SQL = f"""
    WITH {_TABLE_PLACE_CTE}
    INSERT INTO nodes (public_id, kind, owner_type, owner_id, parent_id, props,
                       created_at, updated_at)
    SELECT {_public_id_sql(_FAMILY_TABLE, 'd.id')},
           '{_KIND_TABLE}', d.owner_type, d.owner_id, place.parent_id,
           jsonb_strip_nulls(jsonb_build_object(
               'legacy', '{_FAMILY_TABLE}', 'legacy_id', d.id,
               'title', d.namespace,
               'semantic_search', d.semantic_search))
           || CASE WHEN d.schema IS NULL THEN '{{}}'::jsonb
                   ELSE jsonb_build_object('child_schema', d.schema) END,
           d.created_at, d.created_at
      FROM user_datastores d JOIN place ON place.ds_id = d.id
    ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET
        props = (nodes.props - {_TABLE_PROPS_KEYS}) || EXCLUDED.props,
        updated_at = NOW()
     WHERE ((nodes.props - {_TABLE_PROPS_KEYS}) || EXCLUDED.props)
           IS DISTINCT FROM nodes.props
"""

# Réconciliation STRUCTURELLE, hors comparaison de contenu — même raison qu'au lot
# M2, et ici elle est plus aiguë encore : **lier un tableau à un projet ne touche pas
# `user_datastores`** (l'attache vit dans `project_links`), pas plus qu'un transfert
# de propriétaire ne réécrit un contenu. Sans cet UPDATE, un tableau rangé dans un
# projet après le premier boot resterait à la racine pour toujours.
#
# ⚠️ **Le rang est ANNULÉ quand le parent change**, et c'est le seul endroit où un
# rang se perd : une position ne veut rien dire hors de sa fratrie — la garder ferait
# arriver le tableau au milieu d'une fratrie qu'il n'a jamais connue, voire sur le
# rang d'un autre. Le `NULL` est ce que `_place_table_nodes` reprend juste après.
RECONCILE_TABLE_NODES_SQL = f"""
    WITH {_TABLE_PLACE_CTE}
    UPDATE nodes n
       SET owner_type = place.owner_type, owner_id = place.owner_id,
           parent_id = place.parent_id,
           position = CASE WHEN n.parent_id IS DISTINCT FROM place.parent_id
                           THEN NULL ELSE n.position END
      FROM place
     WHERE n.public_id = {_public_id_sql(_FAMILY_TABLE, 'place.ds_id')}
       AND (n.owner_type, n.owner_id, n.parent_id)
           IS DISTINCT FROM (place.owner_type, place.owner_id, place.parent_id)
"""

# Même prédicat qu'aux lots M1/M2, et même raison de ne PAS le relâcher : il porte
# sur `props->>'legacy'`, donc un nœud NATIF (créé par une surface, sans ligne
# legacy) n'est jamais candidat. La famille est distincte (`tbl`) : purger les
# tableaux n'effleure ni les pages, ni les projets, ni les couches de contexte.
PURGE_TABLE_NODES_SQL = f"""
    DELETE FROM nodes n
     WHERE n.props->>'legacy' = '{_FAMILY_TABLE}'
       AND NOT EXISTS (SELECT 1 FROM user_datastores d
                        WHERE d.id = (n.props->>'legacy_id')::bigint)
"""


def _place_table_nodes(conn) -> int:
    """Donne son rang à tout nœud-tableau qui n'en a pas — **c'est ici que M-g vit**.

    Deux populations, une seule règle : le tableau qui vient d'être converti, et
    celui que la réconciliation vient de reparenter (son rang a été annulé, une
    position n'ayant pas de sens hors de sa fratrie). Chacun se place EN FIN de sa
    fratrie, donc dans l'intervalle qui suit le dernier frère — aucun rang existant
    ne bouge, et le coût est d'un `UPDATE` par nœud placé au lieu d'une
    renumérotation de fratrie (`midpoint` : 1,4 ms contre 20 s).

    Rejouable : un nœud placé a un rang, donc n'est plus sélectionné. En régime
    établi, ce pas coûte une requête qui ne rend rien."""
    rows = conn.execute(
        "SELECT id, parent_id, owner_type, owner_id FROM nodes "
        f"WHERE props->>'legacy' = '{_FAMILY_TABLE}' AND position IS NULL "
        "ORDER BY id").fetchall()
    for r in rows:
        place_at_end(conn, int(r["id"]), parent_id=r["parent_id"],
                     owner_type=r["owner_type"], owner_id=r["owner_id"])
    return len(rows)


def convert_tables(conn) -> None:
    """Tableaux → nœuds-tableaux : contenu, structure, purge, puis les rangs.

    ⚠️ L'ORDRE avec `convert_projects` compte, comme pour les pages : un tableau lié
    à un projet se rattache au NŒUD de ce projet. S'il n'existe pas encore, la
    jointure ne rend rien et le tableau atterrit à la racine de son propriétaire —
    un rangement faux, qu'aucune erreur ne signale.

    ⚠️ **Les LIGNES ne bougent pas** (`datastore_rows`) : c'est le lot M4, celui du
    volume, et il attend d'avoir appris sur trois types plus simples (0063-D4). Le
    **bail de la file de travail** (`claimed_by`/`claimed_until`) ne bouge pas non
    plus — il vit sur les lignes, et migrera avec elles (0063-D3)."""
    conn.execute(CONVERT_TABLES_TO_NODES_SQL)
    conn.execute(RECONCILE_TABLE_NODES_SQL)
    conn.execute(PURGE_TABLE_NODES_SQL)
    _place_table_nodes(conn)


# ══ Lot M4 — les LIGNES de tableau (#308) ════════════════════════════════════
#
# Le dernier lot de conversion, et le seul qui porte du VOLUME : 43 584 lignes en
# production contre quelques centaines de nœuds pour tout le reste réuni. Les trois
# lots précédents ont établi le patron sur des populations où une maladresse ne se
# voyait pas ; ici, chaque geste est multiplié par soixante.
#
# Ce que ce lot NE fait pas, et qui n'est pas un oubli :
#
# - **Le bail ne bouge pas.** `claimed_by`/`claimed_until` restent lus et écrits sur
#   `datastore_rows`, qui demeure la table de vérité jusqu'à la bascule (0063-D3).
#   La projection ne les COPIE même pas : un bail est volatile (il change à chaque
#   réservation, sans passer par un boot), donc un bail projeté serait périmé dans la
#   seconde. Une colonne vide est un manque visible ; une colonne qui ment ne l'est
#   pas. Le backfill appartient à la bascule, qui saura le faire dans la même
#   transaction que le basculement du lecteur.
# - **La recherche des lignes ne bouge pas** : le FTS des lignes reste l'index de
#   `datastore_rows` (`db/search.DATASTORE_ROWS_TEXT`). C'est ce qui commande de loger
#   la donnée sous `props->'data'` **et pas** dans `title`/`body_md` : `NODES_TEXT`
#   n'indexe que ces clés-là, donc les deux GIN de `nodes` restent INERTES pour les
#   lignes. Ce n'est pas une économie de bout de chandelle — le banc M0 les chiffre à
#   **99 % du temps d'écriture d'un vivier** et à deux fois le poids de la table
#   qu'ils indexent. Les faire mordre ici doublerait le coût de toute écriture de
#   ligne, pour une recherche que personne ne lirait avant M5.
_FAMILY_ROW = "row"

# Le genre d'une ligne. **Le mot est déjà celui de la recherche** (`oto_search` rend
# `kind='ligne'` pour le contenu d'un tableau depuis #67 V2.1) : le prendre ici évite
# qu'un même objet porte deux noms selon la surface qui le regarde.
_KIND_ROW = "ligne"

# ⚠️ **La clé legacy d'une ligne est COMPOSITE** — c'est le fait qui commande tout ce
# bloc, et il n'a pas d'équivalent dans les lots M1/M2/M3. `datastore_rows` a pour clé
# primaire `(ns_id, row_id)` et **aucune colonne `id`** : il n'existe donc pas de
# bigint à dériver, et `(n.props->>'legacy_id')::bigint` — la jointure de purge des
# trois lots précédents — n'a ici aucun sens (le cast tomberait sur un `row_id`
# textuel, uuid7 en production). La famille garde donc DEUX clés, `legacy_ns` et
# `legacy_row`, et la purge joint sur les deux.
#
# `ns_id` est un entier : il ne contient pas de `:`, donc le premier séparateur de la
# chaîne dérivée découpe sans ambiguïté quelle que soit la forme de `row_id`. Deux
# lignes distinctes ne peuvent pas produire le même identifiant public.
_ROW_LEGACY_KEY = "r.ns_id::text || ':' || r.row_id"

_ROW_PROPS_KEYS = "'{legacy,legacy_ns,legacy_row,data}'::text[]"

# Le parent ET le propriétaire viennent du **nœud-tableau**, pas de `user_datastores`.
# Une seule jointure pour les deux, et c'est délibéré : 0054-D4 dit qu'une ligne
# hérite de son tableau, donc lire l'ownership ailleurs que sur le nœud parent, c'est
# se ménager la possibilité que les deux divergent. Ils ne peuvent pas diverger s'ils
# ont une seule source.
#
# ⚠️ Une ligne dont le tableau n'a pas de nœud n'est pas convertie (jointure interne).
# Le cas est transitoire par construction — `convert_tables` tourne juste avant, dans
# la même transaction — et se rattrape au boot suivant. C'est l'ordre déjà exigé par
# les pages et les tableaux, pour la même raison.
_ROW_PLACE_JOIN = f"""
      JOIN nodes tbl ON tbl.public_id = {_public_id_sql(_FAMILY_TABLE, 'r.ns_id')}
"""

# Même arbitre que M3 — le CONTENU, faute d'un `updated_at` fiable à comparer. Ici
# `datastore_rows` en a bien un, mais le newer-wins reste le mauvais outil : la
# fusion additive `(props - clés) || EXCLUDED.props` doit aussi rattraper un
# `props->'data'` que la projection aurait écrit de travers, ce qu'un test de date ne
# voit pas. Le rejeu reste intégralement no-op, `updated_at` compris.
CONVERT_ROWS_TO_NODES_SQL = f"""
    INSERT INTO nodes (public_id, kind, owner_type, owner_id, parent_id, props,
                       created_at, updated_at)
    SELECT {_public_id_sql(_FAMILY_ROW, _ROW_LEGACY_KEY)},
           '{_KIND_ROW}', tbl.owner_type, tbl.owner_id, tbl.id,
           jsonb_build_object(
               'legacy', '{_FAMILY_ROW}', 'legacy_ns', r.ns_id,
               'legacy_row', r.row_id, 'data', r.data),
           r.created_at, r.updated_at
      FROM datastore_rows r {_ROW_PLACE_JOIN}
    ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET
        props = (nodes.props - {_ROW_PROPS_KEYS}) || EXCLUDED.props,
        updated_at = NOW()
     WHERE ((nodes.props - {_ROW_PROPS_KEYS}) || EXCLUDED.props)
           IS DISTINCT FROM nodes.props
"""

# Réconciliation STRUCTURELLE : un tableau qu'on range dans un projet, ou qu'on
# transfère, change le propriétaire de ses lignes sans qu'aucune ligne ne soit
# réécrite — `datastore_rows` ne bouge pas d'un octet. Sans cet UPDATE, les lignes
# garderaient l'ownership du premier boot pour toujours.
#
# Le rang est annulé quand le parent change, même règle qu'en M3 : une position ne
# veut rien dire hors de sa fratrie. `_place_row_nodes` le reprend juste après.
RECONCILE_ROW_NODES_SQL = f"""
    UPDATE nodes n
       SET owner_type = tbl.owner_type, owner_id = tbl.owner_id, parent_id = tbl.id,
           position = CASE WHEN n.parent_id IS DISTINCT FROM tbl.id
                           THEN NULL ELSE n.position END
      FROM datastore_rows r {_ROW_PLACE_JOIN}
     WHERE n.public_id = {_public_id_sql(_FAMILY_ROW, _ROW_LEGACY_KEY)}
       AND (n.owner_type, n.owner_id, n.parent_id)
           IS DISTINCT FROM (tbl.owner_type, tbl.owner_id, tbl.id)
"""

# La purge, bornée à SA famille comme aux trois lots précédents — et c'est ELLE qui
# porte l'intégrité de l'arbre, puisque `parent_id` n'a pas de clé étrangère
# (arbitrage M-e, tranché : « je n'aime pas que de la logique soit dans la base »).
#
# Elle couvre les DEUX façons dont une ligne peut cesser d'exister :
#   1. la ligne est supprimée → plus de `datastore_rows` correspondante ;
#   2. le TABLEAU entier est supprimé → `datastore_rows` part en cascade (clé
#      étrangère `ns_id`), donc le cas 1 s'applique à chacune de ses lignes.
# Sans clé étrangère sur `parent_id`, rien ne supprime les nœuds-lignes quand le
# nœud-tableau disparaît : ce DELETE est le seul garant, et c'est pourquoi il est
# testé sur le cas orphelin explicitement (`test_nodes_m4_rows.py`).
#
# Ce que la clé étrangère aurait coûté, mesuré au banc M0 : **+36 %** sur chaque
# écriture de masse (contrôle par ligne, à vie), et **×118** à la suppression d'un
# tableau — 75 secondes de verrou sur un vivier, parce que la cascade cherche les
# enfants de chacun des 45 000 enfants. Le prix d'un DELETE au boot est sans commune
# mesure.
PURGE_ROW_NODES_SQL = f"""
    DELETE FROM nodes n
     WHERE n.props->>'legacy' = '{_FAMILY_ROW}'
       AND NOT EXISTS (
           SELECT 1 FROM datastore_rows r
            WHERE r.ns_id = (n.props->>'legacy_ns')::bigint
              AND r.row_id = n.props->>'legacy_row')
"""

# ⚠️ **Le placement est ENSEMBLISTE ici, alors qu'il est ligne à ligne en M3** — et
# c'est le seul endroit où ce lot s'écarte du patron qu'il suit par ailleurs.
#
# `_place_table_nodes` boucle en Python et appelle `place_at_end` par nœud : un
# `SELECT` du dernier frère puis un `UPDATE`, deux allers-retours par nœud. Sur 83
# tableaux, c'est instantané et parfaitement lisible. Sur 43 584 lignes, le même
# geste a été mesuré (conteneur jetable peuplé à l'échelle, 12/08) à **297 s
# extrapolées**, contre **0,66 s** pour l'UPDATE unique ci-dessous — un facteur
# **449**. Et le chiffre est un PLANCHER : le banc tourne sur un PostgreSQL local,
# quand la production parle à une base managée à travers le réseau, où 87 000
# allers-retours se paient une deuxième fois.
#
# La sémantique, elle, est identique à `place_at_end` : chaque nœud se range APRÈS le
# dernier frère déjà placé (`base`), aucun rang existant ne bouge, et l'écart nominal
# est le même `POSITION_GAP`. L'ordre d'arrivée est l'ordre de création, `public_id`
# départageant les ex æquo — un tri TOTAL, donc un arbre stable d'un boot à l'autre
# (`created_at` seul ne l'est pas : une insertion en masse partage l'horodatage).
#
# Rejouable pour la même raison qu'en M3 : un nœud placé a un rang, donc sort du
# `WHERE`. En régime établi, ce pas est une requête qui ne rend rien.
PLACE_ROW_NODES_SQL = f"""
    WITH base AS (
        SELECT parent_id, MAX(position) AS max_pos
          FROM nodes
         WHERE parent_id IS NOT NULL AND position IS NOT NULL
         GROUP BY parent_id
    ),
    ranked AS (
        SELECT n.id,
               COALESCE(b.max_pos, 0)
               + row_number() OVER (PARTITION BY n.parent_id
                                    ORDER BY n.created_at, n.public_id)
                 * {POSITION_GAP} AS pos
          FROM nodes n
          LEFT JOIN base b ON b.parent_id = n.parent_id
         WHERE n.props->>'legacy' = '{_FAMILY_ROW}' AND n.position IS NULL
    )
    UPDATE nodes n SET position = ranked.pos
      FROM ranked WHERE n.id = ranked.id
"""


def convert_rows(conn) -> None:
    """Lignes de tableau → nœuds-lignes : contenu, structure, purge, puis les rangs.

    ⚠️ L'ORDRE avec `convert_tables` compte, et plus durement qu'ailleurs : une ligne
    se rattache au NŒUD de son tableau, et la jointure est INTERNE — si le nœud-tableau
    n'existe pas encore, la ligne n'est pas convertie du tout (au lieu d'atterrir au
    mauvais endroit, comme une page l'aurait fait). L'écart se rattrape au boot
    suivant, mais un `convert_rows` appelé avant `convert_tables` ne convertirait
    jamais rien sur une base neuve.

    **Coût mesuré du premier boot** (conteneur jetable à l'échelle de la production,
    43 584 lignes / 83 tableaux) : de l'ordre de la **seconde**, là où l'extrapolation
    naïve depuis M2 annonçait 45 s — le brief #308 en faisait, à raison, le risque
    principal du lot. Deux raisons à l'écart : le travail est ENSEMBLISTE (quatre
    requêtes, pas 43 584), et les deux GIN de recherche de `nodes` restent inertes
    faute de `title`/`body_md` sur ces nœuds. La projection reste donc bloquante au
    boot, comme les trois lots précédents ; la rendre reprenable par tranches aurait
    ajouté un curseur, un état à réconcilier et un mode dégradé pour rien.
    """
    conn.execute(CONVERT_ROWS_TO_NODES_SQL)
    conn.execute(RECONCILE_ROW_NODES_SQL)
    conn.execute(PURGE_ROW_NODES_SQL)
    conn.execute(PLACE_ROW_NODES_SQL)


# --- Écriture NATIVE d'une page (surface `oto_node`, chantier modèle de contenu) ---
#
# Le nouvel univers ne PROJETTE pas ici : ces fonctions écrivent des nœuds qui
# n'ont aucune source dans l'ancien monde — comme les couches de contexte le font
# depuis M1. C'est ce qui distingue un nœud NATIF d'un nœud converti : `props` ne
# porte pas de `legacy`, donc rien ne le rafraîchit et rien ne le purge avec les
# copies.
#
# ⚠️ **Un nœud natif ne porte JAMAIS `delivery`.** La recherche discrimine une
# couche de contexte par cette propriété, pas par le genre : la poser sur une page
# ordinaire la ferait remonter dans le périmètre des couches, servi au handshake.

def _new_node_id() -> str:
    """L'identifiant public d'un nœud natif est un TIRAGE (0059-D3).

    Jamais dérivé du contenu ni du rang — même raison qu'un bloc (#362) : une
    identité calculée se recalcule, donc se casse au premier renommage ou
    réordonnancement, et toute référence externe part avec. Les nœuds CONVERTIS,
    eux, dérivent leur id de leur clé d'origine : c'est ce qui rend leur conversion
    idempotente. Deux régimes, deux besoins — celui-ci n'a pas de clé naturelle.
    """
    return "nod_" + secrets.token_hex(12)


def create_page(*, owner_type: str, owner_id: str, title: str,
                body_md: str = "", description: str = "",
                parent_id: Optional[int] = None) -> dict:
    """Crée une page NATIVE et rend sa fiche. Le corps est parsé en blocs.

    Positionnée EN FIN de fratrie : une création n'a pas d'opinion sur son rang, et
    `place_after` reste le geste de qui en a une. Les deux passent par `midpoint`,
    donc aucune renumérotation.
    """
    if not (title or "").strip():
        raise ValueError("title requis")
    from .blocks import write_node_blocks
    public_id = _new_node_id()
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO nodes (public_id, kind, owner_type, owner_id, parent_id, props) "
                "VALUES (%s, %s, %s, %s, %s, jsonb_build_object("
                "    'title', %s::text, 'description', %s::text, "
                "    'body_md', %s::text, 'embed_dirty', TRUE)) "
                "RETURNING id, public_id, kind, owner_type, owner_id, parent_id, props",
                (public_id, _KIND, owner_type, str(owner_id), parent_id,
                 title.strip(), description or "", body_md or ""),
            ).fetchone()
            place_at_end(conn, row["id"], parent_id=parent_id,
                         owner_type=owner_type, owner_id=str(owner_id))
            write_node_blocks(conn, row["id"], body_md or "")
            from .search import stamp_rank_vector
            stamp_rank_vector(conn, "nodes", "id = %s", (row["id"],))
    return dict(row)


def update_page(node_id: int, *, title: Optional[str] = None,
                description: Optional[str] = None,
                body_md: Optional[str] = None) -> bool:
    """Met à jour une page native. `None` = champ conservé.

    ⚠️ Le corps ne se réécrit QUE s'il est fourni : les blocs portent des identifiants
    stables qu'une prose peut citer, et les réécrire à chaque édition de titre les
    ferait tous changer. `write_node_blocks` rapproche les blocs existants et
    CONSERVE leur identité quand le texte n'a pas bougé.
    """
    from .blocks import write_node_blocks
    champs = {"title": title, "description": description, "body_md": body_md}
    poses = {k: v for k, v in champs.items() if v is not None}
    if not poses:
        return False
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "UPDATE nodes SET props = props || %s::jsonb || "
                "                 jsonb_build_object('embed_dirty', TRUE), "
                "       updated_at = NOW() "
                " WHERE id = %s AND props->>'legacy' IS NULL RETURNING id",
                (json.dumps(poses), node_id),
            ).fetchone()
            if row is None:
                return False
            if body_md is not None:
                write_node_blocks(conn, node_id, body_md)
            from .search import stamp_rank_vector
            stamp_rank_vector(conn, "nodes", "id = %s", (node_id,))
    return True


# --- Le cycle de parenté : refusé à l'écriture, borné partout ------------------
#
# `nodes.parent_id` n'a **pas** de clé étrangère (arbitrage M-e, ouvert) : rien en base
# n'empêche un nœud d'être rangé sous sa propre descendance. Il faut DEUX gestes, et
# ils ne se remplacent pas :
#
# - `move_page` **refuse** le cycle. C'est la correction — elle protège les
#   déplacements à venir.
# - toute récursion sur l'arbre porte la clause `CYCLE`. C'est la **borne** — elle
#   protège de ce qui serait DÉJÀ en base, qu'aucune garde amont ne peut défaire
#   rétroactivement.
#
# Sans la borne, `WITH RECURSIVE … UNION ALL` sur un cycle ne termine pas : il empile
# dans `base/pgsql_tmp` jusqu'au `DiskFull`. Mesuré de bout en bout le 2026-09-01 —
# `move A sous B` (B déjà enfant de A) rendait 200, puis `delete A` remplissait le
# disque. **Cette base est partagée avec la production** (`docs/live-migrations.md`) :
# un appel authentifié quelconque saturait donc le disque de la prod.
#
# ⚠️ **La borne est la clause `CYCLE`, jamais un plafond de profondeur.** Un plafond
# tronquerait un `DELETE` sur un arbre légitimement profond et laisserait des enfants
# accrochés à un identifiant disparu — on guérirait d'un mal en en posant un autre.
# `CYCLE id SET …` s'arrête à la RÉPÉTITION : chaque nœud est visité une fois, et
# aucun arbre acyclique n'est tronqué.


class ParentCycle(RuntimeError):
    """L'arbre boucle : `node_id` est son propre ancêtre par `parent_id`.

    Levée à l'ÉCRITURE quand `move_page` refuse de fermer la boucle — `parent_id`
    porte alors le parent demandé. Levée à la LECTURE quand la remontée du fil en
    rencontre une déjà en base — `parent_id` vaut `None`, personne ne l'a demandée,
    c'est un constat de corruption.
    """

    def __init__(self, node_id: int, parent_id: Optional[int] = None):
        super().__init__(
            f"cycle_de_parente: le nœud {node_id} est son propre ancêtre — "
            "son fil ne peut pas être remonté"
            if parent_id is None else
            f"cycle_de_parente: le nœud {node_id} ne peut pas être rangé sous "
            f"{parent_id}, qui descend de lui")
        self.node_id = node_id
        self.parent_id = parent_id


# La DESCENTE : le nœud et toute sa descendance, une ligne par nœud, terminant même
# si l'arbre porte déjà un cycle. La ligne de répétition sort en plus, marquée
# `boucle` — un `IN (SELECT id …)` s'en moque, un identifiant en double ne supprime
# pas deux fois.
_DESCENDANCE = (
    "WITH RECURSIVE descendance AS ("
    "    SELECT id, parent_id FROM nodes WHERE id = %s"
    "  UNION ALL"
    "    SELECT n.id, n.parent_id FROM nodes n"
    "      JOIN descendance d ON n.parent_id = d.id"
    ") CYCLE id SET boucle USING chemin "
)

# La REMONTÉE : le nœud et ses ancêtres. C'est par elle que se pose la question de
# `move_page`, et c'est délibéré — remonter coûte la PROFONDEUR de l'arbre, alors que
# descendre coûterait le sous-arbre entier à chaque déplacement.
_ASCENDANCE = (
    "WITH RECURSIVE ascendance AS ("
    "    SELECT id, parent_id FROM nodes WHERE id = %s"
    "  UNION ALL"
    "    SELECT n.id, n.parent_id FROM nodes n"
    "      JOIN ascendance a ON n.id = a.parent_id"
    ") CYCLE id SET boucle USING chemin "
)


def move_page(node_id: int, *, parent_id: Optional[int],
              after_id: Optional[int] = None) -> bool:
    """Déplace une page dans l'arbre — nouveau parent, et rang optionnel.

    Change le PARENT et la POSITION, jamais l'identité : c'est l'opération
    élémentaire du modèle, et c'est elle qui garantit mécaniquement que ce qui pend
    au nœud (ses blocs, ses enfants, ce qui le cite) survit au déplacement.

    ⚠️ **Refuse de ranger un nœud sous sa propre descendance** (`ParentCycle`). Sans
    ce refus, l'arbre acceptait une boucle, et la suppression qui la rencontrait ne
    terminait pas — cf. le préambule de ce module. Le refus est levé, pas rendu en
    valeur : `move_page` rend déjà `False` pour « ce nœud n'existe pas », et servir le
    même `False` pour deux faits opposés donnerait à l'appelant un succès muet.

    La question se pose PAR LE HAUT (`_ASCENDANCE` depuis le parent demandé) : si le
    nœud déplacé y figure, la boucle se fermerait. Le cas `parent_id == node_id` est
    couvert par le même geste — la remontée s'amorce sur le parent demandé, donc elle
    le contient toujours.
    """
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT owner_type, owner_id FROM nodes "
                " WHERE id = %s AND props->>'legacy' IS NULL", (node_id,)).fetchone()
            if row is None:
                return False
            if parent_id is not None and conn.execute(
                    _ASCENDANCE + "SELECT 1 FROM ascendance WHERE id = %s LIMIT 1",
                    (parent_id, node_id)).fetchone() is not None:
                raise ParentCycle(node_id, parent_id)
            conn.execute("UPDATE nodes SET parent_id = %s, updated_at = NOW() "
                         " WHERE id = %s", (parent_id, node_id))
            place_after(conn, node_id, after_id=after_id, parent_id=parent_id,
                        owner_type=row["owner_type"], owner_id=row["owner_id"])
    return True


def delete_page(node_id: int) -> bool:
    """Supprime une page native ET sa descendance.

    L'arbre n'a **pas** de clé étrangère (arbitrage M-e, encore ouvert pour
    `parent_id`) : la descendance se ramasse ici, par le code. Sans ça,
    supprimer un parent laisserait des enfants rattachés à un identifiant disparu —
    des orphelins qu'aucun lecteur ne trouve et qu'aucune purge ne voit.

    ⚠️ **Une table plus loin, la réponse a changé** (2026-09-01, #800) : le corps ne
    dépend plus de ce que fait l'appelant. `blocks.node_id` porte désormais une clé
    étrangère `ON DELETE CASCADE` — supprimer le nœud emporte ses blocs, ici comme
    partout ailleurs. Le `DELETE FROM blocks` explicite ci-dessous reste, et pour une
    raison de COÛT et non d'intégrité : il retire le corps de toute la descendance en
    UNE instruction ensembliste, là où la cascade referait le geste nœud par nœud.

    La descente est BORNÉE (`_DESCENDANCE`, clause `CYCLE`) : une boucle déjà en base
    la faisait tourner jusqu'à remplir `pgsql_tmp`. Bornée, elle emporte au contraire
    les nœuds de la boucle — c'est le seul geste qui défait un cycle existant.
    """
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT id FROM nodes WHERE id = %s AND props->>'legacy' IS NULL",
                (node_id,)).fetchone()
            if row is None:
                return False
            conn.execute(
                _DESCENDANCE
                + "DELETE FROM blocks WHERE node_id IN (SELECT id FROM descendance)",
                (node_id,))
            conn.execute(
                _DESCENDANCE
                + "DELETE FROM nodes WHERE id IN (SELECT id FROM descendance)",
                (node_id,))
    return True


# --- Le RÉSIDU de la recopie (2026-09-01) --------------------------------------
#
# Cinq conversions déposaient à chaque boot dans `nodes` une image des tables
# historiques, marquée `props.legacy`. Elles sont arrêtées (`db/_init.py`) : ce que
# la dernière passe a laissé — 75 668 nœuds sur 75 721, mesuré en production le
# 2026-09-01 — n'a plus ni écrivain ni lecteur.
#
# ⚠️ **Arrêter et retirer sont deux gestes.** L'arrêt ne détruit rien et se déploie
# seul ; le retrait détruit, et se déroule HORS du boot, par lots, sous `--apply`.
# D'où ce partage : ce module ne porte que la mécanique, la décision de tirer vit
# dans `maintenance.residu_projete`.
#
# Ce qui pend à un nœud — mesuré en production avant d'écrire ces lignes, pas
# supposé : **0** embedding sur un nœud recopié (les 22 embeddings de nœuds sont tous
# natifs) ; **aucun** partage ne désigne un nœud ; et **0** nœud natif n'a pour parent
# un nœud recopié — le retrait ne détache donc aucun enfant vivant.
#
# ⚠️ Une phrase de ce paragraphe est PÉRIMÉE depuis le 2026-09-01 (#800) et vaut
# d'être datée plutôt qu'effacée : « aucune clé étrangère ne pointe `nodes` ». Il y
# en a une maintenant — `blocks_node_fk`, `ON DELETE CASCADE`. C'est ce qui rend
# `delete_orphan_blocks()` sans objet : le mode d'échec qu'il ramassait ne peut plus
# se produire, et il balayait TOUT bloc sans nœud, marqué ou non.


def count_projected_nodes() -> int:
    """Combien de nœuds portent encore la marque de la recopie."""
    with _connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM nodes WHERE props ? 'legacy'"
        ).fetchone()["n"]


def count_projected_blocks() -> int:
    """Combien de blocs pendent à un nœud recopié — donc partiraient AVEC lui.

    C'est la moitié de l'inventaire qui manquait au mode à blanc (#800) : il
    annonçait les nœuds et les orphelins, et taisait les 34 314 blocs attachés que
    `--apply` emportait. Un inventaire dont le rôle est de dire ce qu'on s'apprête à
    détruire et qui en tait la plus grosse part donne confiance à tort.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM blocks b "
            " JOIN nodes n ON n.id = b.node_id WHERE n.props ? 'legacy'"
        ).fetchone()["n"]


def delete_projected_nodes(*, batch_size: int = 2000, max_batches: int = 100) -> None:
    """Retire le résidu par lots, les blocs d'abord.

    **Par lots, chacun dans sa transaction** : un `DELETE` unique de 70 000 lignes
    tiendrait un verrou de plusieurs secondes sur une table que le serveur lit à
    chaque affichage de contenu — et cette base est partagée avec la production
    (`docs/live-migrations.md`). Un lot interrompu laisse simplement du résidu, que
    la passe suivante reprend : le geste se REPREND, il ne se répare pas.

    **Les blocs d'abord**, mais plus pour la raison d'origine : depuis #800,
    `blocks.node_id` porte une clé étrangère `ON DELETE CASCADE`, donc l'ordre ne
    décide plus de l'intégrité. Il décide du COÛT — un `DELETE` ensembliste sur le
    lot entier, plutôt qu'une cascade rejouée nœud par nœud.

    ⚠️ **Cette fonction ne rend RIEN de comptable, et c'est délibéré.** Le nombre de
    lignes qu'un `DELETE` déclare avoir touchées ne distingue pas un retrait qui a
    réussi d'un retrait qui n'a rien trouvé : les deux finissent à zéro. Le compte
    se prend par DIFFÉRENTIEL d'inventaire, de part et d'autre de l'appel.
    """
    with _connect() as conn:
        for _ in range(max_batches):
            with conn.transaction():
                lot = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM nodes WHERE props ? 'legacy' LIMIT %s",
                        (batch_size,)).fetchall()
                ]
                if not lot:
                    return
                conn.execute("DELETE FROM blocks WHERE node_id = ANY(%s)", (lot,))
                conn.execute("DELETE FROM nodes WHERE id = ANY(%s)", (lot,))


def count_orphan_blocks() -> int:
    """Blocs dont le nœud n'existe plus — un TÉMOIN, plus une liste de travail.

    Depuis #800 la contrainte `blocks_node_fk` rend ce mode d'échec impossible : ce
    compte doit valoir 0, et un non-zéro ne dit plus « il y a du ménage à faire »
    mais **« la contrainte n'est pas posée sur cette base »**. C'est la seule
    lecture qui reste juste, et elle vaut d'être servie dans l'inventaire.

    ⚠️ **Il n'y a plus de `delete_orphan_blocks()`, et son retrait est le point ③ de
    #800.** Le balai n'avait aucun prédicat `legacy` : il emportait tout bloc sans
    nœud, quelle qu'en fût l'origine — y compris le corps d'une page NATIVE dont le
    nœud venait d'être supprimé par le défaut que cette même issue corrige. Le
    borner « au résidu marqué » est impossible : un orphelin n'a plus de nœud, donc
    plus de marque. La contrainte, elle, supprime la question — ce qu'un balai
    bornerait, elle l'empêche de naître.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM blocks b "
            " WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = b.node_id)"
        ).fetchone()["n"]
