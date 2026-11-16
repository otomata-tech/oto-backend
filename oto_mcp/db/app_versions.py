"""Apps : le BROUILLON et la PUBLICATION (`docs/apps.md` § Brouillon et publication).

Une app dont `drafts_enabled` est vrai ne change plus pour ses lecteurs à chaque
écriture : le manifeste et le HTML écrits vont au brouillon (`apps.draft_*`, sur la
ligne de l'app), que seuls ses éditeurs ouvrent ; `publish` le rend vivant pour tous,
en une transaction, sous le verrou de la ligne — l'état remplacé gardé dans
`app_revisions` comme toute écriture (`apps.ecrire_sous_verrou`).

**Le brouillon a son ETag** (`draft_rev`), distinct de celui de l'état vivant : une
écriture qui le nomme et trouve un autre brouillon est refusée (`DraftConflict`), et
une écriture qui nomme l'état VIVANT alors qu'un brouillon existe aussi — l'agent a lu
l'app publiée, pas le brouillon sur lequel il écrirait (`DraftExists`).

NON aplati dans `db.*` : `db.app_versions.<fn>`.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from ._conn import _connect
from .apps import _TOUT, app_rev, ecrire_sous_verrou, with_rev

_BROUILLON = "draft_manifest, draft_html, draft_by, draft_at"


class DraftConflict(Exception):
    """Le brouillon a changé depuis la lecture de l'appelant : rien n'a été écrit."""

    def __init__(self, current_rev: Optional[str]):
        super().__init__(current_rev)
        self.current_rev = current_rev


class DraftExists(DraftConflict):
    """L'appelant nomme l'état VIVANT, mais un brouillon existe : il ne l'a pas lu."""


class NoDraft(Exception):
    """Rien à publier : l'app n'a pas de brouillon."""


class DraftPending(Exception):
    """Couper le brouillon d'une app qui en a un en cours perdrait ce travail."""


class DraftsOff(Exception):
    """L'app ne passe pas par un brouillon : ses écritures sont vivantes. Un brouillon
    posé à côté vieillirait sous elles, et le publier les déferait sans un mot."""


def draft_rev(manifest: Any, html: Optional[str]) -> str:
    """L'ETag d'un brouillon — préfixé, il ne se confond jamais avec celui d'un état
    vivant de même contenu."""
    return "d" + app_rev("draft", manifest, html or "", None)[:15]


def _vue(row: Optional[dict]) -> Optional[dict]:
    if row is None or row.get("draft_html") is None:
        return None
    return {"manifest": row["draft_manifest"] or {}, "html": row["draft_html"],
            "by": row["draft_by"], "at": row["draft_at"],
            "rev": draft_rev(row["draft_manifest"], row["draft_html"])}


def draft(app_id: int) -> Optional[dict]:
    """Le brouillon de l'app (`manifest`, `html`, `by`, `at`, `rev`), ou `None`."""
    with _connect() as conn:
        return _vue(conn.execute(
            f"SELECT {_BROUILLON} FROM apps WHERE id = %s", (app_id,)).fetchone())


def _verrou(conn, app_id: int) -> Optional[dict]:
    return conn.execute(f"SELECT {_TOUT}, {_BROUILLON} FROM apps WHERE id = %s FOR UPDATE",
                        (app_id,)).fetchone()


def _juger(prior: dict, expected_rev: Optional[str]) -> Optional[dict]:
    """Le brouillon courant, après avoir jugé `expected_rev` contre lui — ou contre
    l'état vivant quand il n'y en a pas (le brouillon naîtra de cet état)."""
    courant = _vue(prior)
    if expected_rev is None:
        return courant
    vivant = with_rev(prior)["rev"]
    if courant is None:
        if expected_rev != vivant:
            raise DraftConflict(vivant)
    elif expected_rev != courant["rev"]:
        raise (DraftExists if expected_rev == vivant else DraftConflict)(courant["rev"])
    return courant


def write_draft(app_id: int, *, by: str, expected_rev: Optional[str] = None,
                manifest: Optional[dict] = None, html: Optional[str] = None) -> Optional[dict]:
    """Écrit le brouillon : `manifest` et/ou `html`, le reste repris du brouillon en
    cours ou, sans lui, de l'état vivant. L'auteur du brouillon devient `by`. `None` =
    app inconnue ; `DraftsOff` si l'app ne passe pas par un brouillon."""
    with _connect() as conn:
        prior = _verrou(conn, app_id)
        if prior is None:
            return None
        # Jugé SOUS le verrou : un `set_drafts enabled=false` passé entre la lecture de
        # l'appelant et cette écriture ne laisse pas un brouillon orphelin.
        if not prior["drafts_enabled"]:
            raise DraftsOff()
        courant = _juger(prior, expected_rev)
        base = courant or {"manifest": prior["manifest"], "html": prior["html"]}
        m = manifest if manifest is not None else base["manifest"]
        h = html if html is not None else base["html"]
        row = conn.execute(
            "UPDATE apps SET draft_manifest = %s, draft_html = %s, draft_by = %s, "
            f"draft_at = NOW() WHERE id = %s RETURNING {_BROUILLON}",
            (Jsonb(m), h, by, app_id)).fetchone()
    return _vue(row)


def delete_draft(app_id: int, *, expected_rev: Optional[str] = None) -> bool:
    """Abandonne le brouillon. Faux s'il n'y en avait pas."""
    with _connect() as conn:
        prior = _verrou(conn, app_id)
        if prior is None or _vue(prior) is None:
            return False
        _juger(prior, expected_rev)
        conn.execute("UPDATE apps SET draft_manifest = NULL, draft_html = NULL, "
                     "draft_by = NULL, draft_at = NULL WHERE id = %s", (app_id,))
    return True


def set_enabled(app_id: int, enabled: bool) -> Optional[dict]:
    """Règle le passage par un brouillon. Le couper avec un brouillon en cours est
    refusé (`DraftPending`) : publier ou abandonner d'abord."""
    with _connect() as conn:
        prior = _verrou(conn, app_id)
        if prior is None:
            return None
        if not enabled and _vue(prior) is not None:
            raise DraftPending()
        row = conn.execute(f"UPDATE apps SET drafts_enabled = %s WHERE id = %s "
                           f"RETURNING {_TOUT}", (enabled, app_id)).fetchone()
    return with_rev(row)


def publish(app_id: int, *, by: str, expected_rev: Optional[str], manifest: dict,
            note: Optional[str] = None) -> Optional[dict]:
    """Rend le brouillon vivant : `manifest` est celui du brouillon, déjà REJUGÉ par
    l'appelant contre `by` (qui devient l'auteur de l'app) ; le HTML est celui du
    brouillon nommé par `expected_rev` — refusé s'il a changé. L'état remplacé est
    gardé, le brouillon vidé, dans la même transaction. `None` = app inconnue."""
    with _connect() as conn:
        prior = _verrou(conn, app_id)
        if prior is None:
            return None
        courant = _vue(prior)
        if courant is None:
            raise NoDraft()
        if expected_rev is not None and expected_rev != courant["rev"]:
            raise DraftConflict(courant["rev"])
        row = ecrire_sous_verrou(
            conn, prior, by=by, manifest=manifest, html=courant["html"], published=True,
            note=note, extra_sets=("draft_manifest = NULL", "draft_html = NULL",
                                   "draft_by = NULL", "draft_at = NULL"))
    return with_rev(row)
