"""Apps : lecture et écriture des tables `apps` et `app_revisions`.

NON aplati dans `db.*` : `create`/`get`/`update` sont trop communs pour la surface
plate. Les appelants écrivent `db.apps.<fn>`.

**La révision (`rev`) est une ETag de contenu**, comme celle des pages
(`projects.doc_rev`) : les 16 premiers caractères d'un md5 du titre, du manifeste, du
HTML et de la vignette. Aucune colonne ne la porte ; une écriture qui la reçoit
(`expected_rev`) et trouve un autre contenu est refusée (`AppConflict`) au lieu
d'écraser l'édition d'un autre.

**Chaque mise à jour écrit d'abord l'état précédent dans `app_revisions`**, sous le
verrou de la ligne : de quoi restaurer, et de quoi dire qui a changé quoi. **Seules
les `MAX_REVISIONS` plus récentes sont gardées**, purgées dans la même transaction :
un HTML pèse jusqu'à 512 Ko, et un agent qui réécrit son app en boucle remplirait la
base sans borne.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from ._conn import _connect

# Ce qu'une LISTE rend : de quoi dessiner une carte, jamais le HTML (une app pèse
# des dizaines de Ko ; la liste d'une org en servait sinon des centaines).
_CARTE = ("id, project_id, title, thumbnail, created_by, created_at, updated_by, "
          "updated_at, archived_at")
_TOUT = _CARTE + ", manifest, html"
# Les révisions gardées par app — autant que `revisions()` en liste.
MAX_REVISIONS = 50


class AppConflict(Exception):
    """L'app a changé depuis la lecture de l'appelant : rien n'a été écrit."""

    def __init__(self, current_rev: str):
        super().__init__(current_rev)
        self.current_rev = current_rev


def app_rev(title: str, manifest: Any, html: str, thumbnail: Any) -> str:
    """L'ETag d'un état d'app — stable pour un même contenu, quel que soit l'ordre
    des clés JSON."""
    h = hashlib.md5()
    for part in (title, json.dumps(manifest, sort_keys=True), html,
                 json.dumps(thumbnail, sort_keys=True)):
        h.update((part or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def with_rev(row: Optional[dict]) -> Optional[dict]:
    if row is None or "html" not in row:
        return row
    return {**row, "rev": app_rev(row["title"], row["manifest"], row["html"], row["thumbnail"])}


def create(project_id: int, *, title: str, manifest: dict, html: str,
           thumbnail: Optional[dict], by: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO apps (project_id, title, manifest, html, thumbnail, created_by, "
            f"updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_TOUT}",
            (project_id, title, Jsonb(manifest), html,
             Jsonb(thumbnail) if thumbnail is not None else None, by, by),
        ).fetchone()
    return with_rev(row)


def get(app_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {_TOUT} FROM apps WHERE id = %s", (app_id,)).fetchone()
    return with_rev(row)


def get_for_call(app_id: int) -> Optional[dict]:
    """L'app SANS son HTML — ce qu'un appel d'app ouverte relit à chaque fois
    (`capabilities/apps_appel.py`) : le manifeste, le projet, l'auteur. Le HTML
    (jusqu'à 512 Ko) n'est servi qu'à l'ouverture (`op=get`)."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_CARTE}, manifest FROM apps WHERE id = %s", (app_id,)).fetchone()


def list_for_projects(project_ids: Iterable[int], *, archived: bool = False) -> list[dict]:
    """Les apps des projets donnés, cartes seules (sans manifeste ni HTML), les plus
    récemment changées d'abord. L'appelant a déjà vérifié l'accès à chaque projet."""
    ids = sorted({int(p) for p in project_ids})
    if not ids:
        return []
    clause = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_CARTE} FROM apps WHERE project_id = ANY(%s) AND {clause} "
            "ORDER BY updated_at DESC, id DESC",
            (ids,),
        ).fetchall()


def update(app_id: int, *, by: str, expected_rev: Optional[str] = None,
           title: Optional[str] = None, manifest: Optional[dict] = None,
           html: Optional[str] = None, thumbnail: Any = ..., ) -> Optional[dict]:
    """Change les champs donnés (`thumbnail=None` l'efface ; omis, il reste). Écrit
    l'état précédent dans `app_revisions` d'abord. `None` = app inconnue."""
    with _connect() as conn:
        prior = conn.execute(
            f"SELECT {_TOUT} FROM apps WHERE id = %s FOR UPDATE", (app_id,)).fetchone()
        if prior is None:
            return None
        current = app_rev(prior["title"], prior["manifest"], prior["html"], prior["thumbnail"])
        if expected_rev is not None and expected_rev != current:
            raise AppConflict(current)
        conn.execute(
            "INSERT INTO app_revisions (app_id, title, manifest, html, thumbnail, edited_by) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (app_id, prior["title"], Jsonb(prior["manifest"]), prior["html"],
             Jsonb(prior["thumbnail"]) if prior["thumbnail"] is not None else None,
             prior["updated_by"]),
        )
        conn.execute(
            "DELETE FROM app_revisions WHERE app_id = %s AND id NOT IN ("
            "SELECT id FROM app_revisions WHERE app_id = %s ORDER BY id DESC LIMIT %s)",
            (app_id, app_id, MAX_REVISIONS),
        )
        sets, params = ["updated_at = NOW()", "updated_by = %s"], [by]
        if title is not None:
            sets.append("title = %s")
            params.append(title)
        if manifest is not None:
            sets.append("manifest = %s")
            params.append(Jsonb(manifest))
        if html is not None:
            sets.append("html = %s")
            params.append(html)
        if thumbnail is not ...:
            sets.append("thumbnail = %s")
            params.append(Jsonb(thumbnail) if thumbnail is not None else None)
        row = conn.execute(
            f"UPDATE apps SET {', '.join(sets)} WHERE id = %s RETURNING {_TOUT}",
            (*params, app_id),
        ).fetchone()
    return with_rev(row)


def set_archived(app_id: int, archived: bool) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "UPDATE apps SET archived_at = CASE WHEN %s THEN COALESCE(archived_at, NOW()) "
            f"ELSE NULL END WHERE id = %s RETURNING {_TOUT}",
            (archived, app_id),
        ).fetchone()
    return with_rev(row)


def revisions(app_id: int, limit: int = MAX_REVISIONS) -> list[dict]:
    """Les états antérieurs, du plus récent au plus ancien — sans leur HTML."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, app_id, title, edited_by, created_at FROM app_revisions "
            "WHERE app_id = %s ORDER BY id DESC LIMIT %s",
            (app_id, limit),
        ).fetchall()


def revision(app_id: int, revision_id: int) -> Optional[dict]:
    """Un état antérieur, entier. `app_id` est dans la clause : la révision d'une
    autre app ne se restaure pas ici."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, app_id, title, manifest, html, thumbnail, edited_by, created_at "
            "FROM app_revisions WHERE id = %s AND app_id = %s",
            (revision_id, app_id),
        ).fetchone()


def project_owners(project_ids: Iterable[int]) -> dict[str, tuple[str, str]]:
    """`{project_id: (owner_type, owner_id)}` des projets VIVANTS donnés, en une
    requête — de quoi juger l'accès à tous d'un coup (`ownership.can_access_many`)."""
    ids = sorted({int(p) for p in project_ids})
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, owner_type, owner_id FROM projects WHERE id = ANY(%s) "
            "AND archived_at IS NULL",
            (ids,),
        ).fetchall()
    return {str(r["id"]): (r["owner_type"], str(r["owner_id"])) for r in rows}


# ── Gestes d'app : consentement du lecteur, et ce qu'une app lit des runs ────────

def consent_issue(app_id: int, sub: str, action: str, fingerprint: str,
                  token_hash: str, ttl_seconds: int) -> None:
    """Pose un consentement NEUF, non brûlé, valable `ttl_seconds`."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_consents (token_hash, app_id, sub, action, fingerprint, "
            "expires_at) VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
            (token_hash, app_id, sub, action, fingerprint, ttl_seconds),
        )


def consent_burn(app_id: int, sub: str, action: str, fingerprint: str,
                 token_hash: str, geste_id: Optional[str]) -> Optional[int]:
    """Brûle le consentement s'il désigne EXACTEMENT ce geste (même app, lecteur,
    action, arguments), n'a pas servi et n'a pas expiré — en une écriture, de sorte que
    deux appels concurrents ne le brûlent pas deux fois. Rend son id, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE app_consents SET used_at = now(), geste_id = %s "
            "WHERE token_hash = %s AND app_id = %s AND sub = %s AND action = %s "
            "AND fingerprint = %s AND used_at IS NULL AND expires_at > now() "
            "RETURNING id",
            (geste_id, token_hash, app_id, sub, action, fingerprint),
        ).fetchone()
    return int(row["id"]) if row else None


def consent_job(consent_id: int, job_id: int) -> None:
    """Le travail qu'un consentement a lancé — le journal dit quel run vient de l'app."""
    with _connect() as conn:
        conn.execute("UPDATE app_consents SET job_id = %s WHERE id = %s",
                     (job_id, consent_id))


def agent_jobs(org_id: int, trigger_id: int, limit: int) -> list[dict]:
    """Les derniers travaux d'un agent, du plus récent au plus ancien : statut, dates,
    erreur, et QUI les a lancés (`sub`) — l'appelant décide ce qu'il en montre."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, status, run_id, sub, created_at, finished_at, last_error "
            "FROM runner_jobs WHERE org_id = %s "
            "AND payload->>'trigger_id' = %s ORDER BY id DESC LIMIT %s",
            (org_id, str(int(trigger_id)), limit),
        ).fetchall()


def job_of_org(org_id: int, job_id: int) -> Optional[dict]:
    """Un travail de l'org, avec l'agent dont il est un run (`trigger_id`, texte tel
    que posé par le serveur dans sa charge ; NULL hors agent) et qui l'a lancé."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, status, run_id, sub, created_at, finished_at, last_error, "
            "payload->>'trigger_id' AS trigger_id "
            "FROM runner_jobs WHERE id = %s AND org_id = %s",
            (job_id, org_id),
        ).fetchone()


def run_results(run_ids: Iterable[str]) -> dict[str, dict]:
    """`{run_id: {outcome, note}}` — ce que l'agent a conclu (`run_finish`), en une
    requête sur la clé primaire."""
    ids = sorted({r for r in run_ids if r})
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, outcome, note FROM runs WHERE run_id = ANY(%s)", (ids,)
        ).fetchall()
    return {r["run_id"]: {"outcome": r["outcome"], "note": r["note"]} for r in rows}
