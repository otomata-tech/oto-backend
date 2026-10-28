"""Le manifeste d'une app : sa forme, et ce que son AUTEUR peut réellement toucher.

Une app s'exécute chez son lecteur, avec les droits du lecteur ; son manifeste dit
quels tableaux, champs et agents elle peut toucher. Le serveur le juge À L'ÉCRITURE
(`create`, `update`, `restore`) :
- **la forme** — mêmes règles que le lecteur de manifeste du front, pour qu'un agent
  apprenne son erreur en écrivant, pas à l'ouverture de l'app ; taille bornée
  (`MAX_MANIFEST`) ;
- **l'auteur** — chaque tableau déclaré doit se résoudre POUR L'AUTEUR, chaque champ
  déclaré exister dans son schéma ;
- **l'org du PROJET** — chaque tableau et chaque agent déclarés vivent dans l'org du
  projet de l'app (`org_du_projet`), jamais dans l'org active de l'auteur ni du
  lecteur : une app rangée dans un projet de l'org A ne lit pas un tableau de B.

**Chaque tableau est RÉSOLU à l'écriture et rangé par son numéro** (`table` devient
le `ns_id`) : un nom résout dans l'org active de qui le lit, et à l'APPEL l'app sert
un lecteur qui n'est pas l'auteur — le même nom y désignerait un autre tableau. À
l'appel (`apps_appel.py`), la règle « droits = auteur ∩ lecteur » juge ce numéro.

**`set`, `add` et `agents` bornent les gestes** de l'appel d'une app (`write_cell`,
`add_row`, `start_agent`, `agent_runs`, `run_result`), chacun servi avec le
consentement du lecteur (`apps_appel.py`, oto-backend#1386).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
MAX_HEIGHT = 4000
DEFAULT_HEIGHT = 480
MAX_TABLES = 20
MAX_AGENTS = 20
# Le manifeste sérialisé, en caractères : il est relu à CHAQUE appel d'une app ouverte.
MAX_MANIFEST = 32_000


def _liste_de_noms(v: Any) -> Optional[list[str]]:
    if not isinstance(v, list) or any(not isinstance(x, str) or not x for x in v):
        return None
    return v


def forme(raw: Any) -> tuple[dict, list[str]]:
    """Le manifeste normalisé et la liste de ce qui ne va pas (vide = valide).
    Ne lit aucune base."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["The manifest must be a JSON object."]
    if len(json.dumps(raw, ensure_ascii=False)) > MAX_MANIFEST:
        return {}, [f"The manifest is at most {MAX_MANIFEST} characters of JSON."]
    tables: dict[str, dict] = {}
    raw_tables = raw.get("tables") or {}
    if not isinstance(raw_tables, dict):
        problems.append("`tables` must map a name to a table.")
        raw_tables = {}
    if len(raw_tables) > MAX_TABLES:
        problems.append(f"At most {MAX_TABLES} tables.")
    for alias, t in raw_tables.items():
        where = f"tables.{alias}"
        if not ALIAS.match(alias):
            problems.append(f"`{alias}` isn't a usable table name.")
            continue
        if not isinstance(t, dict):
            problems.append(f"{where} must be an object with a `table`.")
            continue
        ref = str(t["table"]) if isinstance(t.get("table"), (int, str)) and str(t.get("table")).strip() else None
        if ref is None:
            problems.append(f"{where}.table is missing.")
            continue
        fields = None
        if t.get("fields") is not None:
            fields = _liste_de_noms(t["fields"])
            if fields is None:
                problems.append(f"{where}.fields must be a list of names.")
        sets: dict[str, list[str]] = {}
        if t.get("set") is not None:
            if not isinstance(t["set"], dict):
                problems.append(f"{where}.set must map a field to its allowed values.")
            else:
                for f, vals in t["set"].items():
                    ok = _liste_de_noms(vals)
                    if ok is None:
                        problems.append(f"{where}.set.{f} must be a list of values.")
                    else:
                        sets[f] = ok
        add: list[str] = []
        if t.get("add") is not None:
            add = _liste_de_noms(t["add"]) or []
            if not add and t["add"]:
                problems.append(f"{where}.add must be a list of names.")
        tables[alias] = {"table": ref.strip(), "fields": fields, "set": sets, "add": add}
    agents: dict[str, dict] = {}
    raw_agents = raw.get("agents") or {}
    if not isinstance(raw_agents, dict):
        problems.append("`agents` must map a name to an agent id.")
        raw_agents = {}
    if len(raw_agents) > MAX_AGENTS:
        problems.append(f"At most {MAX_AGENTS} agents.")
    for alias, a in raw_agents.items():
        if not ALIAS.match(alias):
            problems.append(f"`{alias}` isn't a usable agent name.")
            continue
        entry = a if isinstance(a, dict) else {"id": a}
        try:
            aid = int(entry.get("id"))
        except (TypeError, ValueError):
            aid = 0
        if aid <= 0:
            problems.append(f"agents.{alias} must be an agent id.")
            continue
        run = entry.get("run") is True
        agents[alias] = {"id": aid, "run": run, "input": run and entry.get("input") is True}
    height = raw.get("height")
    height = min(int(height), MAX_HEIGHT) if isinstance(height, (int, float)) and height > 0 else DEFAULT_HEIGHT
    clean = {
        "title": raw.get("title") if isinstance(raw.get("title"), str) else None,
        "height": height,
        "tables": tables,
        "agents": agents,
        "files": raw.get("files") is True,
    }
    return clean, problems


def org_du_projet(project_id: int) -> Optional[int]:
    """L'org où vit le projet d'une app — celle de ses tableaux et de ses agents.
    Un projet perso sans org de rangement vit dans l'org perso de son propriétaire,
    comme la liste des projets le range (`db/projects.py`). None = aucune (projet
    inconnu, ou de la plateforme) : l'app n'y déclare alors rien."""
    from .. import org_store, ownership

    org, connue = ownership.org_ou_vit("project", str(int(project_id)))
    if connue:
        return org
    owner = ownership.owner_of("project", str(int(project_id)))
    if owner is not None and owner[0] == "user":
        return org_store.get_personal_org(str(owner[1]))
    return None


def org_de_l_app(app: dict) -> Optional[int]:
    """L'org où vit une app — celle de ses tableaux et de ses agents, celle où elle se
    liste. Celle de son PROPRIÉTAIRE : l'org, l'org de l'équipe ; pour une app perso,
    l'org où elle a été créée (`context_org_id`), sinon l'org perso de la personne —
    comme un tableau perso. None = aucune : l'app n'y déclare alors rien."""
    from .. import group_store, org_store

    t, i = app.get("owner_type"), app.get("owner_id")
    if t == "org" and i is not None:
        return int(i)
    if t == "group" and i is not None:
        g = group_store.get_group(int(i))
        return int(g["org_id"]) if g else None
    if t == "user" and i is not None:
        return (int(app["context_org_id"]) if app.get("context_org_id")
                else org_store.get_personal_org(str(i)))
    return None


def tableau_de_l_org(ns_id: str, org_id: Optional[int]) -> bool:
    """Le tableau `ns_id` vit-il dans l'org `org_id` ? Org inconnue = non."""
    from .. import ownership

    if org_id is None:
        return False
    org, connue = ownership.org_ou_vit(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
    return connue and org is not None and int(org) == int(org_id)


def auteur(sub: str, project_org: Optional[int], clean: dict) -> list[str]:
    """Ce que l'AUTEUR ne peut pas toucher parmi ce que le manifeste déclare, et ce qui
    ne vit pas dans l'org de l'app (`project_org`, `org_de_l_app`). Range au passage le numéro de
    chaque tableau résolu dans `clean` (`table`)."""
    from .. import db
    from ..datastore.core import DatastoreAmbigu, DatastoreNotFound, make_store

    problems: list[str] = []
    store = make_store(sub)
    for alias, t in clean["tables"].items():
        try:
            ns_id = store._resolve(t["table"])
            schema = store.get_schema(t["table"])
        except (DatastoreNotFound, DatastoreAmbigu):
            # Même phrase qu'un tableau absent ou invisible : rien ne dit lequel.
            problems.append(f"tables.{alias}: table `{t['table']}` isn't one you can read here.")
            continue
        if not tableau_de_l_org(str(ns_id), project_org):
            problems.append(f"tables.{alias}: table `{t['table']}` isn't in this app's "
                            "organization.")
            continue
        t["table"] = str(ns_id)
        declared = {f.get("key") for f in (schema or {}).get("fields") or [] if isinstance(f, dict)}
        if not declared:
            continue
        named = set(t["fields"] or []) | set(t["set"]) | set(t["add"])
        unknown = sorted(n for n in named if n not in declared)
        if unknown:
            problems.append(f"tables.{alias}: no such field: {', '.join(unknown)}.")
    for alias, a in clean["agents"].items():
        if project_org is None or db.get_trigger(a["id"], int(project_org)) is None:
            problems.append(f"agents.{alias}: agent {a['id']} isn't one of this app's "
                            "organization.")
    return problems
