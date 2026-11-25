"""La bibliothèque d'apps de la communauté (`app_library`) — le stockage seul.

Publier (upsert par `slug`, BORNÉ À L'AUTEUR sous verrou advisory, comme
`org_store.library.publish_guide`), lister, lire, dépublier. Chaque lecture porte
l'état de modération et les compteurs d'installations (`db/communaute.jointures`,
`kind='app'`). Qui peut publier, voir une entrée retirée ou installer se juge dans la
capacité (`capabilities/apps_bibliotheque.py`) ; ce module ne connaît aucun rôle.
"""
from __future__ import annotations

from typing import Optional

from psycopg.types.json import Jsonb

from . import communaute
from ._conn import _connect

KIND = "app"

_META = ("id, slug, title, description, category, tags, thumbnail, tables, author_kind, "
         "author_org_id, author_display, visibility, version, created_at, updated_at")
_TOUT = _META + ", html, manifest, source_app_id, published_by"


class SlugPris(Exception):
    """Le slug est porté par l'entrée d'un AUTRE auteur. Volontairement pauvre : le
    refus rendu ne nomme jamais le propriétaire (un slug `unlisted` est un lien)."""


def _pref(cols: str) -> str:
    return ", ".join(f"e.{c.strip()}" for c in cols.split(","))


def publier(*, slug: str, title: str, description: str, category: str, tags: list,
            thumbnail: Optional[dict], html: str, manifest: dict, tables: dict,
            author_kind: str, author_org_id: Optional[int], author_display: str,
            visibility: str, source_app_id: Optional[int], published_by: str) -> dict:
    """Publie ou RE-publie la sienne. Le couple `(author_kind, author_org_id)` d'une
    entrée existante ne change jamais : un autre auteur lève `SlugPris`."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"al:{slug}",))
            cur = conn.execute("SELECT version, author_kind, author_org_id FROM app_library "
                               "WHERE slug = %s", (slug,)).fetchone()
            if cur and (cur["author_kind"], cur["author_org_id"]) != (author_kind,
                                                                      author_org_id):
                raise SlugPris(slug)
            version = (cur["version"] + 1) if cur else 1
            row = conn.execute(
                f"""
                INSERT INTO app_library (slug, title, description, category, tags,
                    thumbnail, html, manifest, tables, author_kind, author_org_id,
                    author_display, visibility, source_app_id, version, published_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    category = EXCLUDED.category, tags = EXCLUDED.tags,
                    thumbnail = EXCLUDED.thumbnail, html = EXCLUDED.html,
                    manifest = EXCLUDED.manifest, tables = EXCLUDED.tables,
                    -- l'appartenance n'est PAS dans le SET : vérifiée sous le verrou.
                    author_display = EXCLUDED.author_display,
                    visibility = EXCLUDED.visibility,
                    source_app_id = EXCLUDED.source_app_id,
                    version = EXCLUDED.version, published_by = EXCLUDED.published_by,
                    updated_at = NOW()
                RETURNING {_TOUT}
                """,
                (slug, title, description, category, list(tags or []),
                 Jsonb(thumbnail) if thumbnail is not None else None, html,
                 Jsonb(manifest), Jsonb(tables), author_kind, author_org_id,
                 author_display, visibility, source_app_id, version, published_by),
            ).fetchone()
    return dict(row)


def lister(*, category: Optional[str] = None, author_kind: Optional[str] = None,
           query: Optional[str] = None, include_unlisted: bool = False,
           sort: Optional[str] = None, limit: int = 100,
           removed_for_org: Optional[int] = None,
           include_all_removed: bool = False) -> list[dict]:
    """Le catalogue (métadonnées, SANS html ni manifeste). Mêmes règles que la
    bibliothèque de guides : `public` seulement sauf `include_unlisted`, entrées retirées
    exclues sauf celles de `removed_for_org` ou toutes avec `include_all_removed`."""
    if sort not in communaute.ORDRES:
        raise ValueError("sort invalide (trending|most_added|new)")
    where = [] if include_unlisted else ["e.visibility = 'public'"]
    params: list = []
    if not include_all_removed:
        if removed_for_org is not None:
            where.append("(cm.state IS DISTINCT FROM 'removed' OR e.author_org_id = %s)")
            params.append(int(removed_for_org))
        else:
            where.append("cm.state IS DISTINCT FROM 'removed'")
    q = (query or "").strip()
    if q:
        where.append("(e.title ILIKE %s OR e.description ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("e.category = %s")
        params.append(category)
    if author_kind:
        where.append("e.author_kind = %s")
        params.append(author_kind)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    cols, joins = communaute.jointures(KIND, "e")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_pref(_META)}, {cols} FROM app_library e{joins}{clause} "
            f"ORDER BY {communaute.ORDRES[sort].format(e='e')} LIMIT %s",
            tuple(params) + (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def lire(*, entry_id: Optional[int] = None, slug: Optional[str] = None,
         include_unlisted: bool = False, include_removed: bool = False) -> Optional[dict]:
    """Une entrée ENTIÈRE (html, manifeste, schémas) par id ou slug."""
    if entry_id is None and not slug:
        raise ValueError("entry_id ou slug requis")
    vis = "" if include_unlisted else " AND e.visibility = 'public'"
    if not include_removed:
        vis += " AND cm.state IS DISTINCT FROM 'removed'"
    cols, joins = communaute.jointures(KIND, "e")
    cle, val = ("e.id", int(entry_id)) if entry_id is not None else ("e.slug", slug)
    with _connect() as conn:
        row = conn.execute(f"SELECT {_pref(_TOUT)}, {cols} FROM app_library e{joins} "
                           f"WHERE {cle} = %s{vis}", (val,)).fetchone()
    return dict(row) if row else None


def depublier(entry_id: int) -> bool:
    """Retire une entrée publiée, avec son état de modération et ses installations."""
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute("DELETE FROM app_library WHERE id = %s", (int(entry_id),))
            communaute.purger(conn, KIND, entry_id)
            return (cur.rowcount or 0) > 0
