"""Couches de contexte & how-to : la façade `guides`, servie par la table `nodes`.

Historiquement la table `guides` (ADR 0042). Depuis le **lot M1** du chantier
modèle de contenu (blueprint ADR 0054/0063), les lignes vivent dans **`nodes`** —
et le mot « guide » ne désigne plus qu'une **surface** (`oto_guide`,
`/api/me/guides/*`), plus un objet du modèle :

- **une couche de contexte EST une page** (0055-D4) — d'où `kind='page'` ici, et
  pas un hypothétique `kind='guide'` ;
- la **livraison** (`init` injecté au handshake / `on-demand` chargé par
  `oto_guide`) était la NATURE d'une ligne de `guides` ; ce n'est plus qu'une
  **propriété** du nœud, `props->>'delivery'`. Les deux familles de fonctions
  ci-dessous ne survivent que parce que les surfaces, elles, ne changent pas :
  elles ne se distinguent plus que par la valeur d'une clé JSON.

**Contrat inchangé** : mêmes signatures, mêmes clés de retour (`scope`,
`owner_id`, `slug`, `title`, `description`, `body_md`, `delivery`, `updated_at`)
que du temps de la table `guides` — `guide_store` et tout ce qui est au-dessus
n'a pas bougé d'une ligne, et les tests qui bouchonnent ces fonctions non plus.
Le `scope` de la surface est l'`owner_type` du nœud (même vocabulaire :
platform | org | group | user), le `owner_id` reste le même texte.

L'identifiant public (0059-D3) d'une couche de contexte est **dérivé de sa clé
naturelle** `(scope, owner, slug)` — cf. `_public_id_sql`. Distinct des
PROCÉDURES (`org_instructions`, slots/versioning), qui restent une table à part
jusqu'à leur propre lot. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

# Le `kind` d'une couche de contexte. Une page, comme les autres (0055-D4) : ce
# qui la distingue d'une page de projet est une PROPRIÉTÉ (`delivery`), pas sa
# nature — c'est tout le propos du lot M1.
_KIND = "page"


def _public_id_sql(scope: str, owner: str, slug: str) -> str:
    """Le SQL de l'identifiant public d'une couche de contexte, à partir de trois
    EXPRESSIONS SQL (colonnes de `guides` pour la conversion, placeholders pour la
    façade). Une seule définition, deux appelants — un identifiant qui divergerait
    entre les deux dupliquerait silencieusement les lignes au boot suivant.

    **Pourquoi dérivé, alors que 0059-D3 veut un opaque tiré au sort** : c'est ce
    qui rend la conversion REJOUABLE sans index supplémentaire. La même page
    convertie deux fois, ou écrite par la surface puis re-vue par la conversion,
    produit le MÊME identifiant → `ON CONFLICT (public_id)` arbitre, personne ne
    duplique. Au passage, l'unicité de `public_id` porte exactement l'invariant
    que `guides` portait en `UNIQUE (scope, owner_id, slug)` : une couche par
    (scope, propriétaire, slug).

    La dérivation exige que la clé naturelle soit IMMUABLE — elle l'est ici : la
    surface guide n'a pas de renommage (on écrit et on supprime par slug). Un
    nœud NATIF (pages, tableaux, lignes — lots M2+) se renomme, lui : son
    identifiant sera **tiré au sort**, jamais dérivé. Ne pas généraliser ceci.
    """
    return (f"'nod_' || substr(md5('ctx:' || {scope} || ':' || {owner} "
            f"|| ':' || {slug}), 1, 24)")


# L'identifiant public depuis trois paramètres liés (scope, owner_id, slug). Les
# casts `::text` sont nécessaires : sans eux, `||` sur des paramètres de type
# inconnu laisse PostgreSQL sans résolution d'opérateur.
_PID = _public_id_sql("%s::text", "%s::text", "%s::text")

# Projection nœud → forme historique d'une ligne `guides`. `scope` EST l'owner_type
# (même vocabulaire), les champs de prose vivent dans `props`. COALESCE parce que
# `guides` les portait NOT NULL DEFAULT '' : une clé absente ne doit pas rendre None.
_COLS = ("id, owner_type AS scope, owner_id, props->>'slug' AS slug, "
         "COALESCE(props->>'title', '') AS title, "
         "COALESCE(props->>'description', '') AS description, "
         "COALESCE(props->>'body_md', '') AS body_md, "
         "props->>'delivery' AS delivery, created_at, updated_at")

# --- Conversion `guides` → `nodes` (lot M1) -----------------------------------

# ⚠️ Exécutée à CHAQUE boot par `_init` (`_init.py`, garde `to_regclass`) : tant que
# la table legacy existe, on recopie — la PROD tourne l'ancien code sur la MÊME base
# et continue d'y écrire pendant la fenêtre de promotion. **Newer-wins** sur
# `updated_at` : une page éditée depuis la nouvelle surface n'est jamais écrasée par
# la conversion, et une écriture prod de la fenêtre est rattrapée au boot suivant.
# Rejouable par construction : sans écriture entre deux passes, la garde `>` rend la
# seconde passe intégralement no-op.
#
# Ce que ce lot change : rien ici. Il maintient les projections (blocs, rang) DANS la
# transaction d'écriture, au lieu de compter sur un rattrapage. Sortir cette recopie
# du démarrage est un autre chantier — et quand elle en sortira, le remplaçant devra
# insérer les seuls absents plutôt que d'arbitrer sur les horodatages : la stratégie
# newer-wins est bonne pour une fenêtre de promotion, pas comme synchronisation
# permanente (oto-backend#891).
#
# `embed_dirty` est posé à TRUE sans regarder `g.embed_dirty` (#282) : le drapeau
# legacy dit « embeddé sous `aux_embeddings(kind='guide', ref=guides.id)` », ce qui
# ne renseigne en rien le nouveau keying (`kind='node'`, `ref=nodes.id`). Une ligne
# rattrapée de la prod a par ailleurs changé — donc à ré-indexer.
CONVERT_GUIDES_TO_NODES_SQL = f"""
    INSERT INTO nodes (public_id, kind, owner_type, owner_id, props,
                       created_at, updated_at)
    SELECT {_public_id_sql('g.scope', 'g.owner_id', 'g.slug')},
           '{_KIND}', g.scope, g.owner_id,
           jsonb_build_object('slug', g.slug, 'delivery', g.delivery,
                              'title', COALESCE(g.title, ''),
                              'description', COALESCE(g.description, ''),
                              'body_md', COALESCE(g.body_md, ''),
                              'embed_dirty', TRUE),
           g.created_at, g.updated_at
      FROM guides g
    ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET
        props = EXCLUDED.props, updated_at = EXCLUDED.updated_at
     WHERE EXCLUDED.updated_at > nodes.updated_at
"""


# --- On-demand (catalogue `oto_guide`) : delivery='on-demand' UNIQUEMENT ------

def _body_before_write(conn, scope: str, owner_id: str, slug: str) -> Optional[str]:
    row = conn.execute(
        f"SELECT COALESCE(props->>'body_md', '') AS body FROM nodes WHERE public_id = {_PID} FOR UPDATE",
        (scope, str(owner_id), slug),
    ).fetchone()
    return row["body"] if row else None


def _maintain_projections(conn, row: dict, body_md: Optional[str]) -> None:
    # Corps explicite modifié seulement : une édition de titre ne réécrit aucun
    # bloc. Même transaction que le contenu, aucune réparation nocturne requise.
    from .blocks import write_node_blocks
    from .search import stamp_rank_vector
    if body_md is not None:
        write_node_blocks(conn, row["id"], body_md)
    stamp_rank_vector(conn, "nodes", "id = %s", (row["id"],))

def list_guides_db(scope: str, owner_id: str) -> list[dict]:
    """Guides ON-DEMAND d'un (scope, owner), triés par slug — métadonnées + corps.
    Exclut les readmes init (delivery='init')."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM nodes "
            f"WHERE kind = '{_KIND}' AND owner_type = %s AND owner_id = %s "
            "AND props->>'delivery' = 'on-demand' ORDER BY props->>'slug'",
            (scope, str(owner_id)),
        ).fetchall()
        return [dict(r) for r in rows]


def _get_one(scope: str, owner_id: str, slug: str, delivery: str) -> Optional[dict]:
    """La couche de contexte `(scope, owner, slug)` SI elle a cette livraison.
    Lookup par identifiant public dérivé = un accès à l'index d'identité."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM nodes WHERE public_id = {_PID} "
            "AND props->>'delivery' = %s",
            (scope, str(owner_id), slug, delivery),
        ).fetchone()
        return dict(row) if row else None


def get_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    return _get_one(scope, owner_id, slug, "on-demand")


def set_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                 title: str = "", description: str = "") -> dict:
    """Crée ou met à jour (upsert par `(scope, owner_id, slug)`) un guide ON-DEMAND.

    La mise à jour ne touche QUE la prose — `delivery` n'est posé qu'à l'insertion,
    exactement comme la table `guides` ne le mettait pas à jour. `embed_dirty` suit
    la prose (#282) : écrire une couche la remet dans l'outbox sémantique, comme
    `guides` le faisait par sa colonne."""
    with _connect() as conn:
        previous_body = _body_before_write(conn, scope, owner_id, slug)
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'on-demand', "
            "                           'title', %s::text, 'description', %s::text, "
            "                           'body_md', %s::text, 'embed_dirty', TRUE)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET "
            "  props = nodes.props || jsonb_build_object("
            "      'title', %s::text, 'description', %s::text, 'body_md', %s::text, "
            "      'embed_dirty', TRUE), "
            "  updated_at = NOW() "
            f"RETURNING {_COLS}",
            (scope, str(owner_id), slug,                       # public_id dérivé
             scope, str(owner_id),                             # owner_type, owner_id
             slug, title, description, body_md,                # props à l'insertion
             title, description, body_md),                     # prose à la mise à jour
        ).fetchone()
        _maintain_projections(conn, row, body_md if body_md != previous_body else None)
        return dict(row)


def seed_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                  title: str = "", description: str = "") -> None:
    """Pose le défaut d'un guide ON-DEMAND s'il n'existe pas (boot, idempotent).
    Ne touche JAMAIS une ligne déjà posée/éditée (les fichiers `guides/*.md` sont
    des seeds, la DB est la source de vérité éditable)."""
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'on-demand', "
            "                           'title', %s::text, 'description', %s::text, "
            "                           'body_md', %s::text, 'embed_dirty', TRUE)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO NOTHING RETURNING id",
            (scope, str(owner_id), slug, scope, str(owner_id),
             slug, title, description, body_md),
        ).fetchone()
        if row is not None:
            _maintain_projections(conn, row, body_md)


def delete_guide_db(scope: str, owner_id: str, slug: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM nodes WHERE public_id = {_PID} "
            "AND props->>'delivery' = 'on-demand'",
            (scope, str(owner_id), slug),
        )
        return (cur.rowcount or 0) > 0


# --- Init (readme injecté au handshake) : delivery='init' UNIQUEMENT ----------

def get_init_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    """Le readme INIT d'un (scope, owner, slug), ou None. `{body_md, updated_at, …}`."""
    return _get_one(scope, owner_id, slug, "init")


def set_init_guide_db(scope: str, owner_id: str, slug: str, body_md: str) -> dict:
    """Upsert d'un readme INIT (édition admin/org/user). Corps vide = readme effacé,
    la ligne reste (comme les ex-tables)."""
    with _connect() as conn:
        previous_body = _body_before_write(conn, scope, owner_id, slug)
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'init', "
            "                           'body_md', %s::text)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO UPDATE SET "
            "  props = nodes.props || jsonb_build_object('body_md', %s::text), "
            "  updated_at = NOW() "
            f"RETURNING {_COLS}",
            (scope, str(owner_id), slug, scope, str(owner_id),
             slug, body_md or "", body_md or ""),
        ).fetchone()
        _maintain_projections(conn, row, (body_md or "") if body_md != previous_body else None)
        return dict(row)


def seed_init_guide_db(scope: str, owner_id: str, slug: str, body_md: str) -> None:
    """Pose le défaut d'un readme INIT s'il n'existe pas (boot, idempotent). Ne touche
    JAMAIS une ligne déjà éditée."""
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            f"VALUES ({_PID}, '{_KIND}', %s, %s, "
            "        jsonb_build_object('slug', %s::text, 'delivery', 'init', "
            "                           'body_md', %s::text)) "
            "ON CONFLICT ON CONSTRAINT nodes_public_id_key DO NOTHING RETURNING id",
            (scope, str(owner_id), slug, scope, str(owner_id), slug, body_md or ""),
        ).fetchone()
        if row is not None:
            _maintain_projections(conn, row, body_md or "")
