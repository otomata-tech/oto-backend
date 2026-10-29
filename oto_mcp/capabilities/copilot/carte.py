"""What the person READS about a call: its label in the trace, the approval card's
sentence and fields, the one-line summary of a result.

The card's sentence is written by the SERVER from the call itself — never by the model,
so it cannot word a change differently from what runs — and it is never LESS than what
runs: every argument it did not name is listed after it.
"""
from __future__ import annotations

import json
from typing import Optional

from .classement import AXES_DU_SERVEUR, cible

_LIBELLES = {
    "data_create_datastore": "Create a table",
    "data_patch_schema": "Change a table's fields",
    "data_write": "Write rows",
    "data_update_where": "Update rows",
    "data_delete_row": "Delete a row",
    "data_drop_column": "Delete a column",
    "data_rows": "Read rows",
    "data_get_schema": "Read a table's fields",
    "data_list_datastores": "List tables",
    "data_aggregate": "Count rows",
    "oto_search": "Search",
    "oto_context": "Read the workspace context",
    "oto_tool_schema": "Look up a tool",
    "oto_whoami": "Check who you are",
}
_VERBES = {"create": "Create", "update": "Update", "patch": "Edit", "delete": "Delete",
           "archive": "Archive", "get": "Read", "list": "List", "search": "Search",
           "run_now": "Run now", "enable": "Turn on", "disable": "Turn off",
           "share": "Share", "publish_mcp": "Publish", "move": "Move", "link": "Link",
           "unlink": "Unlink"}
_NOMS = {"oto_doc": "doc", "oto_procedure": "process", "oto_trigger": "agent",
         "oto_fleet": "batch", "oto_project": "project", "oto_guide": "guide",
         "oto_apps": "app"}


def libelle(name: str, args: Optional[dict]) -> str:
    """Plain words for the trace and the card's step; an unknown tool keeps its name."""
    target, a = cible(name, args)
    op = str(a.get("op") or "")
    quoi = a.get("title") or a.get("name") or a.get("datastore") or a.get("label")
    texte = _LIBELLES.get(target)
    if not texte and op and target in _NOMS:
        texte = f"{_VERBES.get(op, op)} {_NOMS[target]}"
    if not texte:
        texte = f"{target} · {op}" if op else target
    if quoi is not None and not isinstance(quoi, (dict, list)):
        return f"{texte} · {str(quoi)[:60]}"
    return texte


def _q(v) -> str:
    return f"“{str(v)[:60]}”"


def _champs(row) -> str:
    return ", ".join([k for k in (row or {}) if not str(k).startswith("_")][:4])


def _valeur(v):
    return v.get("valeur") if isinstance(v, dict) else v


def _phrase(target: str, a: dict) -> tuple[str, list, bool]:
    """(sentence, arguments it accounts for, generic?)."""
    base = ["op", "datastore", "trigger_id", "doc_id", "id"]

    def t(texte: str, couverts=()):
        return texte, base + list(couverts), False

    op = a.get("op")
    ds = a.get("datastore")
    if target == "data_create_datastore":
        sch = a.get("schema") if isinstance(a.get("schema"), dict) else {}
        cols = [f.get("key") or f.get("name") for f in (sch.get("fields") or a.get("fields") or [])
                if isinstance(f, dict)]
        cols = [c for c in cols if c]
        suite = (f" with {len(cols)} column{'s' if len(cols) > 1 else ''}: {', '.join(cols)}"
                 if cols else "")
        return t(f"Create the table {_q(a.get('name') or ds or 'new table')}{suite}.",
                 ["name", "schema", "fields", "owner", "description", "label"])
    if target == "data_write":
        if a.get("id"):
            return t(f"Change {_champs(a.get('row'))} on one row of table {ds}.", ["row"])
        if isinstance(a.get("rows"), list):
            n = len(a["rows"])
            return t(f"Add {n} row{'s' if n > 1 else ''} to table {ds}.", ["rows"])
        suite = f" ({_champs(a.get('row'))})" if a.get("row") else ""
        return t(f"Add a row to table {ds}{suite}.", ["row"])
    if target == "data_update_where":
        sets = ", ".join(f"{k} = {_q(_valeur(v))}" for k, v in (a.get("set") or {}).items())
        filtre = a.get("filter") or a.get("filters")
        return t(f"Set {sets} on every {'matching ' if filtre else ''}row of table {ds}"
                 f"{'' if filtre else ' (no filter: ALL rows)'}.", ["set", "filter", "filters"])
    if target == "data_patch_schema":
        champs = a.get("fields") if isinstance(a.get("fields"), list) else []
        cles = ", ".join(str(f.get("key")) for f in champs if isinstance(f, dict))
        retire = a.get("remove") if isinstance(a.get("remove"), list) else []
        return t(f"Change the fields of table {ds}{': ' + cles if cles else ''}"
                 f"{' (remove ' + ', '.join(map(str, retire)) + ')' if retire else ''}.",
                 ["fields", "remove"])
    if target == "data_delete_row":
        return t(f"Delete one row of table {ds}.")
    if target == "data_drop_column":
        return t(f"Delete the column {_q(a.get('column') or a.get('field'))} and its data "
                 f"in table {ds}.", ["column", "field", "confirm"])
    if target == "oto_trigger":
        qui = f"agent #{a['trigger_id']}" if a.get("trigger_id") is not None else "the agent"
        if op == "run_now":
            return t(f"Run {qui} now. It acts as you.", ["handoff"])
        if op == "update":
            parts = []
            if a.get("enabled") is False:
                parts.append("pause it (it won't run until turned back on)")
            if a.get("enabled") is True:
                parts.append("turn it on")
            if a.get("cron"):
                parts.append(f"set its schedule to {a['cron']}"
                             f"{' (' + str(a['tz']) + ')' if a.get('tz') else ''}")
            if a.get("label"):
                parts.append(f"rename it {_q(a['label'])}")
            if parts:
                return t(f"{qui[0].upper()}{qui[1:]}: {', '.join(parts)}.",
                         ["enabled", "cron", "tz", "label"])
        if op == "create":
            return t(f"Create a new agent{' ' + _q(a['label']) if a.get('label') else ''} "
                     f"running {a.get('procedure') or 'a process'}"
                     f"{' on ' + str(a['cron']) if a.get('cron') else ''}.",
                     ["label", "procedure", "cron", "tz", "kind"])
        if op == "delete":
            return t(f"Delete {qui}.")
        return t(f"{str(op or 'Change')} {qui}.")
    if target == "oto_doc":
        if op == "create":
            return t(f"Create the doc {_q(a.get('title') or 'untitled')}.",
                     ["title", "body_md", "project_id", "parent_id", "kind"])
        if op in ("update", "patch"):
            return t(f"Edit the doc #{a.get('doc_id')}"
                     f"{', section ' + _q(a['section']) if a.get('section') else ''}.",
                     ["body_md", "section", "mode", "expected_rev", "title"])
        if op == "delete":
            return t(f"Delete the doc #{a.get('doc_id')} and its sub-pages.")
    if target == "oto_procedure":
        return t(f"{'Create' if op == 'create' else 'Change'} the process "
                 f"{_q(a.get('slug') or a.get('title') or '')}. Agents that follow it will "
                 "use the new version.",
                 ["slug", "title", "body_md", "scope", "description", "expected_version"])
    if target in ("web_read", "serper_scrape"):
        return t(f"Open an outside page: {str(a.get('url') or '')[:200]}", ["url"])
    return libelle(target, a), base, True


def phrase(name: str, args: Optional[dict]) -> str:
    """The card's title: one plain sentence, then every argument it did not name."""
    target, a = cible(name, args)
    texte, couverts, generique = _phrase(target, a)
    reste = [k for k in a if k not in AXES_DU_SERVEUR and k not in couverts]
    if generique:
        return f"Run {texte} with {', '.join(reste)}." if reste else f"Run {texte}."
    return f"{texte} Also changes: {', '.join(reste)}." if reste else texte


def detail(name: str, args: Optional[dict]) -> str:
    """The card's step detail: the TARGET's arguments, whole (the panel folds long ones)
    — never a prefix of what runs. Only the server's axes are left out."""
    _, a = cible(name, args)
    return json.dumps({k: v for k, v in a.items() if k not in AXES_DU_SERVEUR},
                      ensure_ascii=False, indent=1, default=str)


def champs_modifiables(name: str, args: Optional[dict]) -> Optional[list[dict]]:
    """The fields the card lets the person change before Apply, as text."""
    target, a = cible(name, args)
    out: list[dict] = []

    def ajouter(cle: str, label: str, valeur) -> None:
        if valeur is not None and not isinstance(valeur, (dict, list)):
            out.append({"key": cle, "label": label, "value": str(valeur)})

    if target == "oto_trigger" and a.get("op") in ("update", "create"):
        ajouter("cron", "Schedule (cron)", a.get("cron"))
        ajouter("tz", "Time zone", a.get("tz"))
        ajouter("label", "Name", a.get("label"))
    if target == "data_create_datastore":
        ajouter("name", "Table name", a.get("name"))
    if target == "data_update_where" and isinstance(a.get("set"), dict):
        for k, v in a["set"].items():
            ajouter(f"set.{k}", str(k), _valeur(v))
    if target == "oto_doc" and a.get("op") == "create":
        ajouter("title", "Title", a.get("title"))
    if target == "data_write" and isinstance(a.get("row"), dict):
        for k, v in list(a["row"].items())[:6]:
            ajouter(f"row.{k}", str(k), v)
    return out or None


def appliquer_modifications(name: str, args: dict, edits: dict) -> dict:
    """The call with the card's edits written in — ONLY on the keys the card offered,
    as text (a number stays a number). Nothing else of the call can be rewritten."""
    offerts = {f["key"] for f in (champs_modifiables(name, args) or [])}
    out = json.loads(json.dumps(args, default=str))
    cibles = out.setdefault("arguments", {}) if name == "oto_call" else out
    for cle, valeur in (edits or {}).items():
        if cle not in offerts or not isinstance(valeur, str):
            continue
        tete, _, sous = cle.partition(".")
        if sous:
            porteur = cibles.setdefault(tete, {})
            actuel = porteur.get(sous)
            nu = _valeur(actuel)
            typee = _comme(nu, valeur)
            porteur[sous] = ({**actuel, "valeur": typee} if isinstance(actuel, dict)
                             and "valeur" in actuel else typee)
        else:
            cibles[tete] = _comme(cibles.get(tete), valeur)
    return out


def _comme(ancien, texte: str):
    if isinstance(ancien, (int, float)) and not isinstance(ancien, bool) and texte.strip():
        try:
            return int(texte) if isinstance(ancien, int) else float(texte)
        except ValueError:
            return texte
    return texte


def portee(target: str) -> str:
    """Which of the app's data a write touched (the front's `ChangeScope`)."""
    if target.startswith("data_") or target == "oto_import":
        return "data"
    if target == "oto_procedure":
        return "processes"
    if target in ("oto_doc", "oto_guide"):
        return "docs"
    if target in ("oto_trigger", "oto_fleet"):
        return "agents"
    if target == "oto_project":
        return "projects"
    if target.startswith("oto_connector"):
        return "connectors"
    return "all"


def resume(texte: str) -> str:
    """One short line for the step, never a JSON dump: counts for lists, the error, the
    first characters otherwise. The model still gets the whole result."""
    try:
        data = json.loads(texte)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        listes = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if listes:
            return " · ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in listes[:2])
        if data.get("error") or data.get("detail"):
            return str(data.get("detail") or data.get("error"))[:100]
        if isinstance(data.get("total"), int):
            return f"{data['total']} total"
        return "done"
    return " ".join(str(texte or "").split())[:100]
