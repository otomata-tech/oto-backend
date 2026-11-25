"""La BIBLIOTHÈQUE PUBLIQUE de guides (vue `guide_library`).

Le catalogue cherchable des guides publiés : publier (borné à son auteur),
lister, lire, dépublier — et **forker** une entrée dans son org, ce qui la
transforme en procédure d'org versionnée.

Étage 1 du package : consomme `instructions` (le fork écrit une procédure d'org).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import instructions
from ..db import _connect
from ..db import communaute
from ..db.guides import guide_du_slug

logger = logging.getLogger(__name__)


# --- bibliothèque publique de guides (marketplace) ---
#
# ⚠️ Tout passe par la VUE `guide_library`, jamais par la table qui la porte :
# le renommage physique est un acte de TAG (base partagée prod/preprod, #519
# lot D / #526), et ce module ne doit alors plus bouger d'une ligne. La vue est
# auto-updatable — `INSERT … RETURNING`, `ON CONFLICT DO UPDATE`, `UPDATE` et
# `DELETE` y passent (mesuré sur PostgreSQL 16, rejoué par
# `tests/test_guide_library_view.py`).
#
# Un catalogue cherchable de guides PUBLIÉS, chaque entrée portant un AUTEUR
# ('otomata' = la plateforme, ou 'org' = un créateur privé). Preview + fork dans
# son org (copie versionnée via set_instruction). En clair (prose publiable).
# Deny-by-default sur la surface anonyme : visibility='public' uniquement, jamais
# 'unlisted' (servi seulement par slug exact à un appelant authentifié).

_LIBRARY_COLS = (
    "id, slug, title, description, body_md, slots, author_kind, author_org_id, "
    "author_display, category, tags, visibility, source_org_id, source_slug, "
    "forked_from, version, published_by, created_at, updated_at"
)
_LIBRARY_META_COLS = (
    "id, slug, title, description, author_kind, author_org_id, author_display, "
    "category, tags, visibility, version, created_at, updated_at"
)


#: Le genre de ces entrées dans les tables de la communauté (`db/communaute.py`).
KIND = "procedure"

#: Les tris servis par `list_library` (`None` = dernière publication d'abord).
SORTS = ("trending", "most_added", "new")


def _prefixe(cols: str, alias: str) -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in cols.split(","))


class LibrarySlugTaken(Exception):
    """Le slug public est déjà porté par une entrée qui appartient à QUELQU'UN
    D'AUTRE (autre org, ou la plateforme). Volontairement pauvre : le message
    rendu à l'appelant ne doit jamais nommer le propriétaire — une entrée
    `unlisted` se lit par slug exact, son slug tient donc lieu de lien secret et
    un refus précis en confirmerait l'existence (même règle que le 404
    non-disclosant du gate d'org, ADR 0023)."""


class LibrarySlotsInvalid(ValueError):
    """Un slot publié porte un schéma cible que la plateforme refuse (oto#34). Levée
    APRÈS le contrôle d'appartenance, sous le verrou : jugés contre les slots d'une
    entrée d'autrui, le refus et la tolérance feraient un oracle sur le contenu d'une
    entrée `unlisted`."""


def publish_guide(*, slug: str, title: str = "", description: str = "",
                     body_md: str, author_kind: str, author_org_id: Optional[int] = None,
                     author_display: str = "", category: str = "",
                     tags: Optional[list] = None, visibility: str = "public",
                     source_org_id: Optional[int] = None, source_slug: Optional[str] = None,
                     forked_from: Optional[int] = None,
                     published_by: Optional[str] = None,
                     slots: Optional[list] = None) -> dict:
    """Publie (ou RE-publie **la sienne**) dans la bibliothèque. Upsert par `slug` :
    re-publier le même slug incrémente `version` et remplace le corps. Sérialisé
    par slug via verrou advisory. Renvoie la row publiée (avec body).

    ⚠️ Écriture BORNÉE À L'AUTEUR : si le slug existe et que son couple
    `(author_kind, author_org_id)` diffère de celui de l'appelant, on lève
    `LibrarySlugTaken` — publier ne prend jamais possession de l'entrée d'autrui
    (le nom EST l'adresse : `UNIQUE (slug)`, toute l'API adresse par slug).
    L'appartenance est lue SOUS le verrou advisory, donc sans course. Une entrée
    orpheline (`author_kind='org'`, `author_org_id` NULL après suppression de
    l'org — ON DELETE SET NULL) n'est réclamable par personne : seul un admin
    plateforme peut la dépublier.

    Les `slots` sont VALIDÉS ici (`slots.validate_slots`, oto#34), sous le verrou :
    le schéma cible d'un slot se juge comme une pose de schéma, contre les slots de
    l'entrée RE-publiée — seule une clé que ce geste ajoute ou change est refusée
    (`LibrarySlotsInvalid`). Une entrée neuve n'a rien de stocké : tout y est neuf.
    La forme NORMALISÉE est celle qui est écrite."""
    slug = instructions.normalize_slug(slug)
    if not slug:
        raise ValueError("slug requis")
    if not (body_md or "").strip():
        raise ValueError("body_md requis")
    if author_kind not in ("otomata", "org"):
        raise ValueError("author_kind invalide (otomata|org)")
    if author_kind == "org" and author_org_id is None:
        # Sinon l'entrée naît sans propriétaire : indépublicable par son auteur et
        # hors de portée du contrôle d'appartenance ci-dessous.
        raise ValueError("author_org_id requis pour author_kind='org'")
    if visibility not in ("public", "unlisted"):
        raise ValueError("visibility invalide (public|unlisted)")
    tags = list(tags or [])
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"dl:{slug}",))
            cur = conn.execute(
                "SELECT version, author_kind, author_org_id, slots FROM guide_library "
                "WHERE slug = %s", (slug,)
            ).fetchone()
            if cur and (cur["author_kind"], cur["author_org_id"]) != (author_kind, author_org_id):
                raise LibrarySlugTaken(slug)
            # Paresseux : `slots` tire le registre des connecteurs, qu'une feuille
            # d'org_store n'importe pas au chargement.
            from .. import slots as slots_mod
            try:
                slots = slots_mod.validate_slots(slots or [],
                                                 cur["slots"] if cur else None)
            except ValueError as e:
                raise LibrarySlotsInvalid(str(e)) from None
            new_version = (cur["version"] + 1) if cur else 1
            row = conn.execute(
                f"""
                INSERT INTO guide_library
                    (slug, title, description, body_md, slots, author_kind, author_org_id,
                     author_display, category, tags, visibility, source_org_id,
                     source_slug, forked_from, version, published_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, slots = EXCLUDED.slots,
                    -- `author_kind`/`author_org_id` volontairement ABSENTS du SET :
                    -- l'appartenance d'une entrée ne change jamais par un upsert
                    -- (elle est vérifiée sous le verrou juste au-dessus). Les
                    -- remettre ici rouvrirait la prise de possession silencieuse.
                    author_display = EXCLUDED.author_display,
                    category = EXCLUDED.category, tags = EXCLUDED.tags,
                    visibility = EXCLUDED.visibility,
                    source_org_id = EXCLUDED.source_org_id,
                    source_slug = EXCLUDED.source_slug,
                    forked_from = EXCLUDED.forked_from, version = EXCLUDED.version,
                    published_by = EXCLUDED.published_by, updated_at = NOW()
                RETURNING {_LIBRARY_COLS}
                """,
                (slug, title, description, body_md, json.dumps(slots or []),
                 author_kind, author_org_id,
                 author_display, category, tags, visibility, source_org_id,
                 source_slug, forked_from, new_version, published_by),
            ).fetchone()
            return dict(row)


def list_library(*, query: Optional[str] = None, category: Optional[str] = None,
                 author_kind: Optional[str] = None, author_org_id: Optional[int] = None,
                 include_unlisted: bool = False, limit: int = 100,
                 sort: Optional[str] = None, removed_for_org: Optional[int] = None,
                 include_all_removed: bool = False) -> list[dict]:
    """Liste/recherche la bibliothèque (métadonnées + `snippet`, SANS body complet).
    Par défaut visibility='public' uniquement (surface anonyme/vitrine) ;
    `include_unlisted` élargit (surface authentifiée). `query` = substring sur
    title/description/body.

    Chaque entrée porte son état de modération et ses compteurs d'installations
    (`db/communaute.jointures`). Une entrée RETIRÉE par le personnel n'est pas rendue,
    sauf celles de l'org `removed_for_org` (l'appelant en est admin — décidé par la
    capacité) ou toutes avec `include_all_removed` (opérateur plateforme).
    `sort` ∈ `SORTS` ; `None` garde le tri historique (dernière publication d'abord)."""
    if sort not in communaute.ORDRES:
        raise ValueError(f"sort invalide ({'|'.join(SORTS)})")
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
        like = f"%{q}%"
        where.append("(e.title ILIKE %s OR e.description ILIKE %s OR e.body_md ILIKE %s)")
        params += [like, like, like]
    if category:
        where.append("e.category = %s")
        params.append(category)
    if author_kind:
        where.append("e.author_kind = %s")
        params.append(author_kind)
    if author_org_id is not None:
        where.append("e.author_org_id = %s")
        params.append(author_org_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    cols_c, joins = communaute.jointures(KIND, "e")
    sel = (_prefixe(_LIBRARY_META_COLS, "e") + ", e.slots, " + cols_c
           + (", e.body_md" if q else ""))
    ordre = communaute.ORDRES[sort].format(e="e")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {sel} FROM guide_library e{joins}{clause} "
            f"ORDER BY {ordre} LIMIT %s",
            tuple(params) + (int(limit),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if q:
            d["snippet"] = instructions._snippet(d.pop("body_md", "") or "", q)
        out.append(d)
    return out


def get_library_entry(*, entry_id: Optional[int] = None, slug: Optional[str] = None,
                      include_unlisted: bool = False,
                      include_removed: bool = False) -> Optional[dict]:
    """Une entrée complète (avec body_md) par id OU slug, avec son état de modération
    et ses compteurs d'installations. Respecte la visibilité : sans
    `include_unlisted`, ne renvoie que les entrées publiques ; sans `include_removed`,
    jamais une entrée retirée par le personnel (qui peut la voir quand même se décide
    dans la capacité, sur l'état rendu)."""
    if entry_id is None and not slug:
        raise ValueError("entry_id ou slug requis")
    vis = "" if include_unlisted else " AND e.visibility = 'public'"
    if not include_removed:
        vis += " AND cm.state IS DISTINCT FROM 'removed'"
    cols_c, joins = communaute.jointures(KIND, "e")
    sel = f"{_prefixe(_LIBRARY_COLS, 'e')}, {cols_c} FROM guide_library e{joins}"
    with _connect() as conn:
        if entry_id is not None:
            row = conn.execute(f"SELECT {sel} WHERE e.id = %s{vis}",
                               (entry_id,)).fetchone()
        else:
            row = conn.execute(f"SELECT {sel} WHERE e.slug = %s{vis}",
                               (instructions.normalize_slug(slug),)).fetchone()
    return dict(row) if row else None


def fork_library_entry(*, entry_id: int, owner_type: str, owner_id: int | str,
                       new_slug: Optional[str] = None,
                       set_by: Optional[str] = None,
                       install_org_id: Optional[int] = None) -> dict:
    """Copie une entrée de la bibliothèque en PROCÉDURE du palier `owner_type`/
    `owner_id` (`org` ou `user`, cf. `instructions.OWNER_TYPES`) sous un nouveau slug
    (défaut = slug source, suffixé -2/-3… si collision DANS CE PALIER). Réutilise
    set_instruction → la procédure forkée est versionnée (v1). Le palier est décidé
    par l'appelant (la capacité, sur le rôle réel), jamais ici — comme le droit de
    forker une entrée retirée par le personnel (lue ici sans filtre).

    `install_org_id` = l'org où le fork est fait : l'installation y est notée
    (`community_installs`) une fois la procédure écrite. Une note qui échoue ne défait
    pas le fork (la procédure existe déjà) : elle se journalise, le compteur perd un
    point."""
    entry = get_library_entry(entry_id=entry_id, include_unlisted=True,
                              include_removed=True)
    if not entry:
        raise ValueError("entrée de bibliothèque inconnue")
    otype, oid = instructions._owner(owner_type, owner_id)
    base_slug = instructions.normalize_slug(new_slug or entry["slug"])
    slug = base_slug
    n = 2
    # Libre dans les DEUX familles (oto#100) : un guide à charger du même slug dans
    # le palier ferait refuser la création.
    while (instructions.get_instruction(otype, oid, slug) is not None
           or guide_du_slug(otype, oid, slug) is not None):
        slug = f"{base_slug}-{n}"
        n += 1
    version = instructions.set_instruction(
        otype, oid, slug, entry["body_md"], title=entry.get("title") or "",
        description=entry.get("description") or "", set_by=set_by,
        slots=entry.get("slots") or [],
    )
    cree = instructions.get_instruction(otype, oid, slug) or {}
    if install_org_id is not None:
        try:
            communaute.noter_installation(KIND, entry["id"], org_id=install_org_id,
                                          sub=set_by)
        except Exception:
            logger.exception("installation non notée (entrée %s, org %s)",
                             entry["id"], install_org_id)
    return {
        "owner_type": otype, "owner_id": oid, "slug": slug, "version": version,
        "guide_id": cree.get("id"), "forked_from": entry["id"],
        "source_title": entry.get("title") or "",
    }


def unpublish_guide(entry_id: int) -> bool:
    """Retire une entrée publiée, avec son état de modération et ses installations
    (même transaction). False si elle n'existait pas."""
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute("DELETE FROM guide_library WHERE id = %s", (entry_id,))
            communaute.purger(conn, KIND, entry_id)
            return (cur.rowcount or 0) > 0
