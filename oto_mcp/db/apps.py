"""Apps : lecture et écriture des tables `apps`, `app_revisions`, `app_projects`,
`app_consents`, `app_files`, `app_table_access` et `app_reads` (le journal des
lectures, en compteurs par jour).

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
un HTML pèse jusqu'à 1 000 000 de caractères, et un agent qui réécrit son app en
boucle remplirait la base sans borne. **Une version PUBLIÉE (`published_at`) échappe à
cette purge** : elle a sa propre borne, `MAX_PUBLISHED` — publier est un geste, pas une
sauvegarde (`db/app_versions.py`, brouillon et publication).

**Puis un budget en octets, par app** (`BUDGET_REVISIONS`, HTML brut, versions
publiées COMPRISES) : compter les versions ne borne pas le poids d'une app qui grandit
(250 copies d'un million de caractères). Au-delà du budget, les plus anciennes tombent,
ordinaires comme publiées — sauf les `PLANCHER_ORDINAIRES` et `PLANCHER_PUBLIEES` plus
récentes de chaque sorte, gardées quel que soit leur poids. Une app à brouillon n'écrit
de révision qu'en publiant : sans ce plancher-là, son historique serait tout entier
hors budget ou tout entier gardé. La taille se lit par `octet_length`, qui ne
décompresse pas le HTML stocké (mesuré : 3 blocs lus contre 1 753 pour `length`).
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
# L'état vivant est-il une version publiée, sa note, et l'app passe-t-elle par un
# brouillon : de quoi dire au lecteur quelle version il ouvre. Jamais le brouillon
# lui-même (`db/app_versions.py`), réservé à ses éditeurs.
_VERSION = "drafts_enabled, published_at, version_note"
_TOUT = _CARTE + ", manifest, html, " + _VERSION
# Les révisions ORDINAIRES gardées par app ; les versions publiées, à part.
MAX_REVISIONS = 50
MAX_PUBLISHED = 200
# Le poids des révisions d'une app (HTML brut, toutes sortes), au plus ; et ce que le
# budget ne touche jamais : les plus récentes de chaque sorte.
BUDGET_REVISIONS = 100 * 1024 * 1024
PLANCHER_ORDINAIRES = 10
PLANCHER_PUBLIEES = 20


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
    (jusqu'à 1 000 000 de caractères, `MAX_HTML`) n'est servi qu'à l'ouverture
    (`op=get`)."""
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


#: Le rang d'un rôle de partage d'app, pour dire le plus fort d'une org (`visibilites`) :
#: View < Use < Edit/Manage. `manager` se dit `editor` à l'écran des apps.
_RANG_DU_ROLE_SQL = ("CASE role WHEN 'viewer' THEN 1 WHEN 'contributor' THEN 2 "
                     "WHEN 'editor' THEN 3 WHEN 'manager' THEN 3 END")
_ROLE_DU_RANG = {1: "viewer", 2: "contributor", 3: "editor"}


def visibilites(orgs: dict[int, Optional[int]]) -> dict[int, dict]:
    """Qui VOIT chacune des apps, au-delà de son propriétaire : `{"org":
    "editor"|"contributor"|"viewer"|None, "people": n}` — le rôle du partage VIVANT à l'org OÙ VIT
    l'app (`orgs` : id de l'app → son org, `apps_manifeste.org_de_l_app`), et le nombre
    de personnes ou d'équipes nommées. Un partage à une AUTRE org ne compte pas : il
    n'est pas « toute l'org », et le dire en révélerait l'existence. La forme des agents
    (`runner_triggers.visibilites_d_agents`) : l'écran des apps dit « Vous seul » d'une
    app que personne d'autre ne voit, comme les tableaux et les agents. UNE requête pour
    toute une liste ; une app sans partage vaut `{"org": None, "people": 0}`. Le rôle
    se lit par son RANG (`_RANG_DU_ROLE`), pas par la projection `permission` : un
    partage Use (`contributor`) projette `read` comme un `viewer`, et l'écran doit les
    distinguer. `manager` se dit `editor`, comme avant."""
    from ._partage_vivant import PARTAGE_VIVANT

    out = {int(i): {"org": None, "people": 0} for i in orgs}
    if not out:
        return out
    ids = [str(i) for i in out]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT resource_id, "
            f"  max({_RANG_DU_ROLE_SQL}) FILTER (WHERE principal_type = 'org' "
            "                            AND principal_id = o.org) AS org, "
            "  count(*) FILTER (WHERE principal_type <> 'org') AS people "
            "FROM resource_grants JOIN unnest(%s::text[], %s::text[]) AS o(id, org) "
            "  ON o.id = resource_id "
            f"WHERE resource_type = 'app' AND {PARTAGE_VIVANT} GROUP BY resource_id",
            (ids, [None if orgs[int(i)] is None else str(orgs[int(i)]) for i in ids]),
        ).fetchall()
    for r in rows:
        out[int(r["resource_id"])] = {
            "org": _ROLE_DU_RANG.get(r["org"]),
            "people": int(r["people"] or 0)}
    return out


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
           html: Optional[str] = None, thumbnail: Any = ...,
           published: bool = False, note: Optional[str] = None) -> Optional[dict]:
    """Change les champs donnés (`thumbnail=None` l'efface ; omis, il reste). Écrit
    l'état précédent dans `app_revisions` d'abord. `None` = app inconnue.
    `published` : l'état écrit est une version publiée (`op=restore`), avec `note`."""
    with _connect() as conn:
        prior = conn.execute(
            f"SELECT {_TOUT} FROM apps WHERE id = %s FOR UPDATE", (app_id,)).fetchone()
        if prior is None:
            return None
        current = app_rev(prior["title"], prior["manifest"], prior["html"], prior["thumbnail"])
        if expected_rev is not None and expected_rev != current:
            raise AppConflict(current)
        row = ecrire_sous_verrou(conn, prior, by=by, title=title, manifest=manifest,
                                 html=html, thumbnail=thumbnail, published=published,
                                 note=note)
    return with_rev(row)


def ecrire_sous_verrou(conn, prior: dict, *, by: str, title: Optional[str] = None,
                       manifest: Optional[dict] = None, html: Optional[str] = None,
                       thumbnail: Any = ..., published: bool = False,
                       note: Optional[str] = None, extra_sets: Iterable[str] = ()) -> dict:
    """Le cœur d'une écriture de l'état vivant, sur la ligne `prior` déjà verrouillée
    (`FOR UPDATE`) par l'appelant : garde l'état remplacé dans `app_revisions` (avec sa
    marque de version publiée et sa note), purge, écrit. Partagé par `update` et la
    publication d'un brouillon (`db/app_versions.publish`), qui le fait dans SA
    transaction. Changer le manifeste ou le HTML sans publier fait de l'état vivant
    une écriture ordinaire : il perd sa marque de version publiée.

    La révision ne garde la marque de version publiée que si cette écriture REMPLACE
    la version (manifeste, HTML, ou une publication) : changer la carte d'une version
    publiée la garde vivante, et en ferait sinon une deuxième copie épinglée."""
    app_id = prior["id"]
    remplace = published or manifest is not None or html is not None
    conn.execute(
        "INSERT INTO app_revisions (app_id, title, manifest, html, thumbnail, edited_by, "
        "published_at, note, saved_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (app_id, prior["title"], Jsonb(prior["manifest"]), prior["html"],
         Jsonb(prior["thumbnail"]) if prior["thumbnail"] is not None else None,
         prior["updated_by"], prior.get("published_at") if remplace else None,
         prior.get("version_note") if remplace else None, prior.get("updated_at")),
    )
    # Deux bornes : les révisions ordinaires, puis les versions publiées — jamais l'une
    # ne pousse l'autre dehors.
    for publiees, garde in (("IS NULL", MAX_REVISIONS), ("IS NOT NULL", MAX_PUBLISHED)):
        conn.execute(
            f"DELETE FROM app_revisions WHERE app_id = %s AND published_at {publiees} "
            "AND id NOT IN (SELECT id FROM app_revisions WHERE app_id = %s "
            f"AND published_at {publiees} ORDER BY id DESC LIMIT %s)",
            (app_id, app_id, garde),
        )
    _purger_au_budget(conn, app_id)
    sets, params = ["updated_at = NOW()", "updated_by = %s", *extra_sets], [by]
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
    if published:
        sets += ["published_at = NOW()", "version_note = %s"]
        params.append(note)
    elif manifest is not None or html is not None:
        sets += ["published_at = NULL", "version_note = NULL"]
    return conn.execute(
        f"UPDATE apps SET {', '.join(sets)} WHERE id = %s RETURNING {_TOUT}",
        (*params, app_id),
    ).fetchone()


# Le poids cumulé du plus récent au plus ancien, et le rang de chaque révision dans sa
# sorte : tombe ce qui dépasse le budget hors des planchers. `octet_length` : la taille
# brute sans décompresser (un `length` lirait chaque HTML stocké).
_AU_DELA_DU_BUDGET = (
    "SELECT id FROM (SELECT id, published_at IS NULL AS ordinaire, "
    "SUM(octet_length(html)) OVER (ORDER BY id DESC ROWS UNBOUNDED PRECEDING) AS cumul, "
    "ROW_NUMBER() OVER (PARTITION BY published_at IS NULL ORDER BY id DESC) AS rang "
    "FROM app_revisions WHERE app_id = %s) r "
    "WHERE cumul > %s AND rang > CASE WHEN ordinaire THEN %s ELSE %s END")


def _purger_au_budget(conn, app_id: int) -> int:
    """Retire les révisions de `app_id` au-delà de `BUDGET_REVISIONS`, les plus anciennes
    d'abord, planchers gardés. Dans la transaction de l'appelant ; rend le nombre retiré."""
    return conn.execute(
        f"DELETE FROM app_revisions WHERE id IN ({_AU_DELA_DU_BUDGET})",
        (app_id, BUDGET_REVISIONS, PLANCHER_ORDINAIRES, PLANCHER_PUBLIEES),
    ).rowcount


def set_archived(app_id: int, archived: bool) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "UPDATE apps SET archived_at = CASE WHEN %s THEN COALESCE(archived_at, NOW()) "
            f"ELSE NULL END WHERE id = %s RETURNING {_TOUT}",
            (archived, app_id),
        ).fetchone()
    return with_rev(row)


def revisions(app_id: int, limit: int = MAX_REVISIONS + MAX_PUBLISHED) -> list[dict]:
    """Les états antérieurs, du plus récent au plus ancien — sans leur HTML : les
    révisions ordinaires gardées ET les versions publiées, que la purge garde à part."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, app_id, title, edited_by, created_at, published_at, note, saved_at "
            "FROM app_revisions WHERE app_id = %s ORDER BY id DESC LIMIT %s",
            (app_id, limit),
        ).fetchall()


def revision(app_id: int, revision_id: int) -> Optional[dict]:
    """Un état antérieur, entier. `app_id` est dans la clause : la révision d'une
    autre app ne se restaure pas ici."""
    with _connect() as conn:
        return conn.execute(
            "SELECT id, app_id, title, manifest, html, thumbnail, edited_by, created_at, "
            "published_at, note, saved_at FROM app_revisions WHERE id = %s AND app_id = %s",
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

_FICHIER = "id, app_id, sub, s3_key, name, media_type, size, text_status, origin, created_at"


class StorageFull(Exception):
    """Le fichier ferait dépasser un plafond du lecteur : `scope` = `app` (dans cette
    app) ou `viewer` (toutes apps confondues). Rien n'a été inséré."""

    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


def file_add(app_id: int, sub: str, s3_key: str, name: str, media_type: str,
             size: int, text: Optional[str] = None, text_status: Optional[str] = None,
             origin: str = "upload", *, max_app_bytes: int,
             max_viewer_bytes: int) -> dict:
    """Range un fichier, SOUS les plafonds de `sub` — compte et insertion dans la même
    transaction, sous un verrou consultatif propre au lecteur : deux dépôts concurrents
    ne passent pas tous deux sous la borne (le compte d'avant, dans la capacité, n'est
    qu'un avis précoce). Lève `StorageFull` sans rien insérer."""
    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"app_files:{sub}",))
        tot = conn.execute(
            "SELECT COALESCE(sum(size) FILTER (WHERE app_id = %s), 0) AS app, "
            "COALESCE(sum(size), 0) AS tout FROM app_files "
            "WHERE sub = %s AND app_id IS NOT NULL", (app_id, sub)).fetchone()
        if int(tot["app"]) + size > max_app_bytes:
            raise StorageFull("app")
        if int(tot["tout"]) + size > max_viewer_bytes:
            raise StorageFull("viewer")
        return conn.execute(
            f"INSERT INTO app_files (app_id, sub, s3_key, name, media_type, size, text, "
            f"text_status, origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_FICHIER}",
            (app_id, sub, s3_key, name, media_type, size, text, text_status, origin),
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


def bytes_of_viewer(sub: str) -> int:
    """Ce que `sub` garde dans TOUTES les apps, en octets (`app_files_sub_idx`)."""
    with _connect() as conn:
        return int(conn.execute(
            "SELECT COALESCE(sum(size), 0) AS n FROM app_files "
            "WHERE sub = %s AND app_id IS NOT NULL", (sub,)).fetchone()["n"])


def files_of(app_id: int, sub: str, limit: int) -> list[dict]:
    """Les fichiers que `sub` a déposés dans l'app, du plus récent au plus ancien."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_FICHIER} FROM app_files WHERE app_id = %s AND sub = %s "
            "ORDER BY id DESC LIMIT %s",
            (app_id, sub, limit),
        ).fetchall()


# Purgeable : plus vieux que la rétention, OU sans app (app supprimée : `ON DELETE SET
# NULL` laisse la ligne pour que son objet soit retiré, jamais orphelin).
_PURGEABLE = "(app_id IS NULL OR created_at < now() - make_interval(days => %s))"


def files_older_than(days: int, limit: int) -> list[dict]:
    """Les fichiers à purger — déposés il y a plus de `days` jours, ou dont l'app a été
    supprimée —, les plus anciens d'abord, `limit` au plus : de quoi purger par tranches
    sans charger la table."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT id, s3_key FROM app_files WHERE {_PURGEABLE} ORDER BY id LIMIT %s",
            (days, limit),
        ).fetchall()


def count_files_older_than(days: int) -> int:
    with _connect() as conn:
        return int(conn.execute(
            f"SELECT count(*) AS n FROM app_files WHERE {_PURGEABLE}", (days,),
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


def app_shares_of(app_id: str, principals: list[tuple[str, str]]) -> list[dict]:
    """Chaque partage VIVANT de l'app que tiennent ces principals, rôle posé ou non :
    `[{principal_type, principal_id, app_role, app_scope}]`
    (`capabilities/apps_roles.choix_du_lecteur` : le rôle ET le périmètre de lignes du
    partage retenu). UNE requête pour tous les principals."""
    from ._partage_vivant import PARTAGE_VIVANT_G

    if not principals:
        return []
    with _connect() as conn:
        return conn.execute(
            "SELECT g.principal_type, g.principal_id, g.app_role, g.app_scope "
            "FROM resource_grants g "
            "JOIN unnest(%s::text[], %s::text[]) AS pr(t, i) "
            "  ON g.principal_type = pr.t AND g.principal_id = pr.i "
            "WHERE g.resource_type = 'app' AND g.resource_id = %s "
            f"AND {PARTAGE_VIVANT_G} ORDER BY g.principal_type, g.principal_id",
            ([p[0] for p in principals], [str(p[1]) for p in principals], str(app_id)),
        ).fetchall()


def set_app_scope(app_id: str, principal_type: str, principal_id: str,
                  scope: Optional[dict]) -> bool:
    """Pose (ou retire, `scope` None) le périmètre de lignes du partage de ce principal
    (`{numéro de tableau: {champ: valeur}}`). Faux = aucun partage de l'app."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE resource_grants SET app_scope = %s WHERE resource_type = 'app' "
            "AND resource_id = %s AND principal_type = %s AND principal_id = %s RETURNING 1",
            (Jsonb(scope) if scope else None, str(app_id), principal_type, str(principal_id)),
        ).fetchone()
    return row is not None


def set_app_role(app_id: str, principal_type: str, principal_id: str,
                 role: Optional[str]) -> bool:
    """Pose (ou retire, `role` None) le rôle d'app du partage VIVANT de ce principal.
    Faux = ce principal n'en a aucun : un rôle ne se pose que sur un partage vivant (un
    partage échu repris repart du rôle par défaut, `db.grant_resource`)."""
    from ._partage_vivant import PARTAGE_VIVANT

    with _connect() as conn:
        row = conn.execute(
            "UPDATE resource_grants SET app_role = %s WHERE resource_type = 'app' "
            "AND resource_id = %s AND principal_type = %s AND principal_id = %s "
            f"AND {PARTAGE_VIVANT} RETURNING 1",
            (role, str(app_id), principal_type, str(principal_id)),
        ).fetchone()
    return row is not None


def app_role_grants(app_id: str) -> list[dict]:
    """Les partages VIVANTS de l'app avec leur rôle d'app (NULL = le rôle par défaut)."""
    from ._partage_vivant import PARTAGE_VIVANT

    with _connect() as conn:
        return conn.execute(
            "SELECT principal_type, principal_id, role, app_role, app_scope, expires_at "
            "FROM resource_grants WHERE resource_type = 'app' AND resource_id = %s "
            f"AND {PARTAGE_VIVANT} ORDER BY principal_type, principal_id", (str(app_id),),
        ).fetchall()


def _sous_son_partage() -> str:
    """La jointure qui fait VALOIR une ligne `app_table_access a` : le même principal
    tient, vivant, le partage de l'app sous lequel elle a été ouverte (`share_id`). Un
    partage reposé — après retrait, échéance, transfert — porte une autre identité :
    ce qui avait été ouvert sous l'ancien ne revit pas (`docs/apps.md` § Accès par l'app)."""
    from ._partage_vivant import PARTAGE_VIVANT_G

    return ("JOIN resource_grants g ON g.resource_type = 'app' "
            "  AND g.resource_id = a.app_id::text AND g.principal_type = a.principal_type "
            "  AND g.principal_id = a.principal_id AND g.share_id = a.share_id "
            f"  AND {PARTAGE_VIVANT_G} ")


def through_app_access(app_id: int, ns_id: int,
                       principals: list[tuple[str, str]]) -> Optional[str]:
    """Le droit que l'un de ces principals tient sur le tableau À TRAVERS cette app
    (`app_table_access`) : `write` > `read`, None = aucun. Ne compte que sous le partage
    VIVANT de l'app qui l'a vu ouvrir (`_sous_son_partage`)."""
    if not principals:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT max(a.permission) AS p FROM app_table_access a "
            "JOIN unnest(%s::text[], %s::text[]) AS pr(t, i) "
            "  ON a.principal_type = pr.t AND a.principal_id = pr.i "
            + _sous_son_partage() +
            "WHERE a.app_id = %s AND a.ns_id = %s",
            ([p[0] for p in principals], [str(p[1]) for p in principals], int(app_id),
             int(ns_id)),
        ).fetchone()
    return row["p"] if row else None


def grant_through_app(app_id: int, ns_id: int, principal_type: str, principal_id: str,
                      permission: str, by: str) -> bool:
    """Ouvre le tableau à ce principal à travers l'app, SOUS son partage vivant de l'app
    (son `share_id`, numéroté ici s'il date d'avant la colonne). Faux = aucun partage
    vivant : rien n'est écrit. Rouvrir sous un partage reposé lie la ligne au neuf."""
    from ._partage_vivant import PARTAGE_VIVANT

    cle = (str(app_id), principal_type, str(principal_id))
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE resource_grants SET share_id = nextval('resource_grants_share_id_seq') "
                "WHERE resource_type = 'app' AND resource_id = %s AND principal_type = %s "
                f"AND principal_id = %s AND share_id IS NULL AND {PARTAGE_VIVANT}", cle)
            cur = conn.execute(
                "INSERT INTO app_table_access (app_id, ns_id, principal_type, principal_id, "
                "permission, share_id, granted_by) "
                "SELECT %s, %s, principal_type, principal_id, %s, share_id, %s "
                "FROM resource_grants WHERE resource_type = 'app' AND resource_id = %s "
                f"AND principal_type = %s AND principal_id = %s AND {PARTAGE_VIVANT} "
                "ON CONFLICT (app_id, ns_id, principal_type, principal_id) DO UPDATE SET "
                "permission = EXCLUDED.permission, share_id = EXCLUDED.share_id, "
                "granted_by = EXCLUDED.granted_by, granted_at = NOW()",
                (int(app_id), int(ns_id), permission, by, *cle))
            return cur.rowcount > 0


def revoke_through_app(app_id: int, principal_type: str, principal_id: str) -> int:
    with _connect() as conn:
        return conn.execute(
            "DELETE FROM app_table_access WHERE app_id = %s AND principal_type = %s "
            "AND principal_id = %s", (int(app_id), principal_type, str(principal_id)),
        ).rowcount


def through_app_of_table(ns_id: int) -> list[dict]:
    """Chaque accès par une app qui VAUT sur ce tableau, avec l'app (pour son gérant)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT a.app_id, ap.title AS app_title, a.principal_type, a.principal_id, "
            "a.permission FROM app_table_access a JOIN apps ap ON ap.id = a.app_id "
            + _sous_son_partage() +
            "WHERE a.ns_id = %s ORDER BY a.app_id, a.principal_type, a.principal_id",
            (int(ns_id),),
        ).fetchall()


def revoke_through_app_tables(app_id: int, principal_type: str, principal_id: str,
                              ns_ids: list[int]) -> int:
    with _connect() as conn:
        return conn.execute(
            "DELETE FROM app_table_access WHERE app_id = %s AND principal_type = %s "
            "AND principal_id = %s AND ns_id = ANY(%s)",
            (int(app_id), principal_type, str(principal_id), [int(n) for n in ns_ids]),
        ).rowcount


def through_app_of(app_id: int) -> list[dict]:
    """Chaque accès par l'app qui VAUT sur cette app (pour `op=roles`) : une ligne restée
    d'un partage disparu ne s'y montre pas — elle ne rend rien."""
    with _connect() as conn:
        return conn.execute(
            "SELECT a.ns_id, a.principal_type, a.principal_id, a.permission "
            "FROM app_table_access a " + _sous_son_partage() +
            "WHERE a.app_id = %s ORDER BY a.principal_type, a.principal_id, a.ns_id",
            (int(app_id),),
        ).fetchall()


def app_of_run(job_id: int, sub: str) -> Optional[int]:
    """L'app depuis laquelle `sub` a lancé ce travail (consentement `start_agent` brûlé
    sur lui), None sinon."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT app_id FROM app_consents WHERE job_id = %s AND sub = %s "
            "AND action = 'start_agent' AND used_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (int(job_id), sub),
        ).fetchone()
    return int(row["app_id"]) if row else None


# --- Le journal des lectures d'app (`app_reads`, `docs/apps.md` § Journal des lectures).

#: Ce que le journal garde : la purge (`maintenance app-reads`) et la borne de lecture.
READS_RETENTION_DAYS = 180
#: Au plus, les lignes d'une lecture du journal (une par lecteur, tableau, accès, jour).
READS_MAX = 2000

_JOUR_UTC = "(NOW() AT TIME ZONE 'UTC')::date"


def note_reads(app_id: int, sub: str, lues: Iterable[tuple]) -> int:
    """Ajoute aux compteurs du jour (UTC) ce qu'un appel a servi : `lues` = des
    `(ns_id, access, row_reads, aggregate_reads, rows_served)`, déjà sommés par
    `(ns_id, access)` par l'appelant — un `ON CONFLICT DO UPDATE` refuse de toucher deux
    fois la même ligne dans une requête. UNE requête, en autocommit de son emprunt ;
    les clés triées : deux appels concurrents verrouillent dans le même ordre. Un
    tableau supprimé entre la lecture et l'écriture est sauté (jointure)."""
    lignes = sorted((int(ns), str(acc), int(r), int(a), int(n)) for ns, acc, r, a, n in lues)
    if not lignes:
        return 0
    cols = list(zip(*lignes))
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO app_reads (app_id, day, ns_id, sub, access, row_reads, "
            "aggregate_reads, rows_served) "
            f"SELECT %s, {_JOUR_UTC}, x.ns, %s, x.acc, x.r, x.a, x.n "
            "FROM unnest(%s::bigint[], %s::text[], %s::int[], %s::int[], %s::bigint[]) "
            "AS x(ns, acc, r, a, n) JOIN user_datastores d ON d.id = x.ns "
            "ORDER BY x.ns, x.acc "
            "ON CONFLICT (app_id, day, ns_id, sub, access) DO UPDATE SET "
            "row_reads = app_reads.row_reads + EXCLUDED.row_reads, "
            "aggregate_reads = app_reads.aggregate_reads + EXCLUDED.aggregate_reads, "
            "rows_served = app_reads.rows_served + EXCLUDED.rows_served, "
            "last_at = NOW()",
            (int(app_id), sub, list(cols[0]), list(cols[1]), list(cols[2]), list(cols[3]),
             list(cols[4])),
        ).rowcount


_COLONNES_LUES = ("r.app_id, ap.title AS app_title, r.ns_id, d.namespace AS table_name, r.sub, "
                  "u.email, r.access, r.day, r.row_reads, r.aggregate_reads, r.rows_served, "
                  "r.first_at, r.last_at")
_JOINTURES_LUES = ("FROM app_reads r JOIN apps ap ON ap.id = r.app_id "
                   "JOIN user_datastores d ON d.id = r.ns_id "
                   "LEFT JOIN users u ON u.sub = r.sub")


def reads_of_app(app_id: int, days: int, ns_ids: Optional[list[int]] = None) -> list[dict]:
    """Le journal d'UNE app sur les `days` derniers jours (UTC, aujourd'hui compris),
    les plus récents d'abord, `READS_MAX` lignes au plus ; `ns_ids` : ces tableaux
    seulement (None = tous)."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_COLONNES_LUES} {_JOINTURES_LUES} WHERE r.app_id = %s "
            f"AND r.day > {_JOUR_UTC} - %s "
            "AND (%s::bigint[] IS NULL OR r.ns_id = ANY(%s::bigint[])) "
            "ORDER BY r.day DESC, r.last_at DESC LIMIT %s",
            (int(app_id), int(days), ns_ids, ns_ids, READS_MAX),
        ).fetchall()


def reads_of_tables(ns_ids: list[int], days: int) -> list[dict]:
    """Le journal de ces tableaux, par toutes les apps, sur les `days` derniers jours."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_COLONNES_LUES} {_JOINTURES_LUES} WHERE r.ns_id = ANY(%s::bigint[]) "
            f"AND r.day > {_JOUR_UTC} - %s "
            "ORDER BY r.day DESC, r.last_at DESC LIMIT %s",
            ([int(n) for n in ns_ids], int(days), READS_MAX),
        ).fetchall()


_READS_PURGEABLES = f"day <= {_JOUR_UTC} - %s"


def count_purgeable_reads(days: int = READS_RETENTION_DAYS) -> int:
    with _connect() as conn:
        return int(conn.execute(
            f"SELECT count(*) AS n FROM app_reads WHERE {_READS_PURGEABLES}", (int(days),),
        ).fetchone()["n"])


def purge_reads(days: int = READS_RETENTION_DAYS) -> int:
    """Retire les jours du journal des lectures au-delà de `days`. Rend combien."""
    with _connect() as conn:
        return conn.execute(
            f"DELETE FROM app_reads WHERE {_READS_PURGEABLES}", (int(days),),
        ).rowcount or 0
