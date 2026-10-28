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
_CARTE = ("id, owner_type, owner_id, context_org_id, title, thumbnail, created_by, "
          "created_at, updated_by, updated_at, archived_at")
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


def create(owner: tuple[str, str], context_org_id: Optional[int], *, title: str,
           manifest: dict, html: str, thumbnail: Optional[dict], by: str,
           project_id: Optional[int] = None) -> dict:
    """Une app neuve, à `owner`, créée dans l'org `context_org_id` — et rangée dans
    `project_id` s'il est donné, dans la même transaction."""
    with _connect() as conn:
        row = conn.execute(
            f"INSERT INTO apps (owner_type, owner_id, context_org_id, title, manifest, html, "
            f"thumbnail, created_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_TOUT}",
            (owner[0], str(owner[1]), context_org_id, title, Jsonb(manifest), html,
             Jsonb(thumbnail) if thumbnail is not None else None, by, by),
        ).fetchone()
        if project_id is not None:
            conn.execute("INSERT INTO app_projects (app_id, project_id, filed_by) "
                         "VALUES (%s, %s, %s)", (row["id"], project_id, by))
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


def _archives(archived: bool) -> str:
    return "a.archived_at IS NOT NULL" if archived else "a.archived_at IS NULL"


def list_for_projects(project_ids: Iterable[int], *, archived: bool = False) -> list[dict]:
    """Les apps RANGÉES dans les projets donnés, cartes seules, les plus récemment
    changées d'abord. Ranger ne donne aucun accès : l'appelant filtre ce qu'il lit."""
    ids = sorted({int(p) for p in project_ids})
    if not ids:
        return []
    with _connect() as conn:
        return conn.execute(
            f"SELECT DISTINCT {', '.join('a.' + c.strip() for c in _CARTE.split(','))} "
            f"FROM apps a JOIN app_projects f ON f.app_id = a.id "
            f"WHERE f.project_id = ANY(%s) AND {_archives(archived)} "
            "ORDER BY a.updated_at DESC, a.id DESC",
            (ids,),
        ).fetchall()


def list_for_owners(owners: list[tuple[str, str]], *, archived: Optional[bool] = False,
                    createur: Optional[tuple[str, int, bool]] = None) -> list[dict]:
    """Les apps POSSÉDÉES par `owners` (paires collectives : org, équipes), plus — avec
    `createur` (`ownership.mes_objets_ici`) — les apps PERSO de `sub` créées dans cette
    org (et, dans son org perso, celles sans org de création). `archived=None` : toutes."""
    clauses, params = [], []
    if owners:
        clauses.append("(a.owner_type, a.owner_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))")
        params += [[o[0] for o in owners], [str(o[1]) for o in owners]]
    if createur is not None:
        csub, corg, sans_org = createur
        clauses.append("(a.owner_type = 'user' AND a.owner_id = %s AND (a.context_org_id = %s"
                       + (" OR a.context_org_id IS NULL" if sans_org else "") + "))")
        params += [csub, corg]
    if not clauses:
        return []
    arch = "" if archived is None else f" AND {_archives(archived)}"
    with _connect() as conn:
        return conn.execute(
            f"SELECT {', '.join('a.' + c.strip() for c in _CARTE.split(','))} FROM apps a "
            f"WHERE ({' OR '.join(clauses)}){arch} ORDER BY a.updated_at DESC, a.id DESC",
            tuple(params),
        ).fetchall()


def list_granted_to(principals: list[tuple[str, str]], *, archived: bool = False) -> list[dict]:
    """Les apps PARTAGÉES à l'un des `principals` (partage vivant), cartes seules, avec
    la meilleure permission (`permission` : `write` > `read`)."""
    from ._partage_vivant import PARTAGE_VIVANT_G

    if not principals:
        return []
    with _connect() as conn:
        cols = ', '.join('a.' + c.strip() for c in _CARTE.split(','))
        return conn.execute(
            f"SELECT {cols}, max(g.permission) AS permission "
            "FROM resource_grants g JOIN apps a ON a.id::text = g.resource_id "
            f"WHERE g.resource_type = 'app' AND {PARTAGE_VIVANT_G} "
            "AND (g.principal_type, g.principal_id) IN "
            "(SELECT * FROM unnest(%s::text[], %s::text[])) "
            f"AND {_archives(archived)} GROUP BY a.id ORDER BY a.updated_at DESC, a.id DESC",
            ([p[0] for p in principals], [str(p[1]) for p in principals]),
        ).fetchall()


def list_all() -> list[dict]:
    with _connect() as conn:
        return conn.execute(f"SELECT {_CARTE} FROM apps ORDER BY id").fetchall()


def owner(app_id: int) -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT owner_type, owner_id, context_org_id FROM apps WHERE id = %s",
            (app_id,)).fetchone()


def reparent(app_id: int, owner_type: str, owner_id: str,
             context_org_id: Optional[int] = None) -> None:
    """Change le propriétaire ; `context_org_id` (donné pour un propriétaire personne)
    dit dans quelle org l'app vit désormais."""
    with _connect() as conn:
        conn.execute("UPDATE apps SET owner_type = %s, owner_id = %s, "
                     "context_org_id = COALESCE(%s, context_org_id) WHERE id = %s",
                     (owner_type, str(owner_id), context_org_id, app_id))


def projects_of(app_ids: Iterable[int]) -> dict[int, list[int]]:
    """`{app_id: [project_id, …]}` — où chaque app est rangée."""
    ids = sorted({int(a) for a in app_ids})
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT app_id, project_id FROM app_projects WHERE app_id = ANY(%s) "
            "ORDER BY filed_at", (ids,)).fetchall()
    out: dict[int, list[int]] = {i: [] for i in ids}
    for r in rows:
        out[int(r["app_id"])].append(int(r["project_id"]))
    return out


def file(app_id: int, project_id: int, by: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO app_projects (app_id, project_id, filed_by) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (app_id, project_id, by))


def unfile(app_id: int, project_id: int) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM app_projects WHERE app_id = %s AND project_id = %s",
                            (app_id, project_id)).rowcount > 0


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


# ── Fichiers déposés dans une app ────────────────────────────────────────────

_FICHIER = "id, app_id, sub, s3_key, name, media_type, size, text_status, created_at"


def file_add(app_id: int, sub: str, s3_key: str, name: str, media_type: str,
             size: int, text: Optional[str] = None, text_status: Optional[str] = None) -> dict:
    with _connect() as conn:
        return conn.execute(
            f"INSERT INTO app_files (app_id, sub, s3_key, name, media_type, size, text, "
            f"text_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_FICHIER}",
            (app_id, sub, s3_key, name, media_type, size, text, text_status),
        ).fetchone()


def files_text(file_ids: Iterable[int]) -> dict[int, Optional[str]]:
    """`{id: texte extrait au dépôt}` — lu à part : il peut peser des centaines de Ko."""
    ids = sorted({int(i) for i in file_ids})
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute("SELECT id, text FROM app_files WHERE id = ANY(%s)", (ids,)).fetchall()
    return {int(r["id"]): r["text"] for r in rows}


def bytes_of(app_id: int, sub: str) -> int:
    """Ce que `sub` garde dans l'app, en octets (la purge de 30 jours borne la fenêtre)."""
    with _connect() as conn:
        return int(conn.execute(
            "SELECT COALESCE(sum(size), 0) AS n FROM app_files WHERE app_id = %s AND sub = %s",
            (app_id, sub)).fetchone()["n"])


def files_of(app_id: int, sub: str, limit: int) -> list[dict]:
    """Les fichiers que `sub` a déposés dans l'app, du plus récent au plus ancien."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_FICHIER} FROM app_files WHERE app_id = %s AND sub = %s "
            "ORDER BY id DESC LIMIT %s",
            (app_id, sub, limit),
        ).fetchall()


def files_older_than(days: int, limit: int) -> list[dict]:
    """Les fichiers déposés il y a plus de `days` jours — les plus anciens d'abord,
    `limit` au plus : de quoi purger par tranches sans charger la table."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, s3_key FROM app_files WHERE created_at < now() - make_interval(days => %s) "
            "ORDER BY id LIMIT %s",
            (days, limit),
        ).fetchall()


def count_files_older_than(days: int) -> int:
    with _connect() as conn:
        return int(conn.execute(
            "SELECT count(*) AS n FROM app_files WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        ).fetchone()["n"])


def delete_files(file_ids: Iterable[int]) -> int:
    ids = sorted({int(i) for i in file_ids})
    if not ids:
        return 0
    with _connect() as conn:
        return conn.execute("DELETE FROM app_files WHERE id = ANY(%s)", (ids,)).rowcount


def file_of(app_id: int, sub: str, file_id: int) -> Optional[dict]:
    """UN fichier de l'app, déposé par `sub` — None sinon (inconnu, d'une autre app ou
    d'un autre lecteur : la même réponse)."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_FICHIER} FROM app_files WHERE id = %s AND app_id = %s AND sub = %s",
            (file_id, app_id, sub),
        ).fetchone()
