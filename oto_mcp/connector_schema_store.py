"""Schéma OBSERVÉ des connecteurs — dérivé des vraies réponses (squelette clés+types,
JAMAIS de valeurs/PII).

Pourquoi observer plutôt que déclarer : les sorties connecteurs sont des **passthrough**
d'API tierces qu'on ne possède pas (Unipile, ATS, Apollo…) ; un schéma écrit à la main
dérive. Le schéma juste = ce qui transite réellement. On en extrait les **feuilles
redactables** (scalaires + listes de scalaires) avec leur(s) chemin(s) et un type — et
on persiste par service (namespace), en fusion incrémentale. Cache process pour ne pas
écrire en base à chaque appel. Best-effort : ne JAMAIS casser un appel d'outil.

`name` peut apparaître à plusieurs chemins (ex. `skills[].name`, `languages[].name`) —
on garde l'ensemble des chemins pour rendre l'ambiguïté VISIBLE dans l'UI (un toggle sur
la clé `name` touche tous ces chemins).
"""
from __future__ import annotations

import threading
from typing import Any

from . import db

_SCALAR_TYPE = {bool: "boolean", int: "number", float: "number", str: "string", type(None): "null"}

# service -> {name: {"type": str, "paths": set[str]}}
_cache: dict[str, dict[str, dict]] = {}
_lock = threading.Lock()


def _type_of(v: Any) -> str:
    return _SCALAR_TYPE.get(type(v), "string")


def _scalar(v: Any) -> bool:
    return not isinstance(v, (dict, list))


def leaves(payload: Any, path: str = "", out: dict[str, dict] | None = None) -> dict[str, dict]:
    """Feuilles redactables observées : `{name: {"type", "paths": set}}`.

    Une feuille = une clé dont la valeur est un scalaire OU une liste de scalaires
    (`emails: [...]`). Les dicts / listes de dicts sont parcourus en profondeur sans
    être listés (structure, pas feuille)."""
    if out is None:
        out = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            if not isinstance(k, str):
                continue
            p = f"{path}.{k}" if path else k
            if _scalar(v):
                e = out.setdefault(k, {"type": _type_of(v), "paths": set()})
                e["paths"].add(p)
            elif isinstance(v, list):
                if all(_scalar(x) for x in v):
                    e = out.setdefault(k, {"type": _type_of(v[0]) if v else "string", "paths": set()})
                    e["paths"].add(p + "[]")
                else:
                    leaves(v, p + "[]", out)
            elif isinstance(v, dict):
                leaves(v, p, out)
    elif isinstance(payload, list):
        for x in payload:
            leaves(x, path, out)
    return out


def observe(service: str, payload: Any) -> None:
    """Fusionne le squelette de `payload` dans le schéma persisté du service.
    Best-effort, jamais bloquant : toute erreur est avalée."""
    try:
        found = leaves(payload)
        if not found:
            return
        with _lock:
            cur = _cache.get(service)
            if cur is None:
                cur = _load(service)
                _cache[service] = cur
            if _merge(cur, found):
                db.upsert_connector_schema(service, _serialize(cur))
    except Exception:
        pass


def _load(service: str) -> dict[str, dict]:
    raw = db.get_connector_schema(service) or {}
    return {n: {"type": i.get("type", "string"), "paths": set(i.get("paths", []))} for n, i in raw.items()}


def _merge(cur: dict[str, dict], found: dict[str, dict]) -> bool:
    changed = False
    for name, info in found.items():
        e = cur.get(name)
        if e is None:
            cur[name] = {"type": info["type"], "paths": set(info["paths"])}
            changed = True
        else:
            n = len(e["paths"])
            e["paths"] |= info["paths"]
            if len(e["paths"]) != n:
                changed = True
    return changed


def _serialize(cur: dict[str, dict]) -> dict:
    return {n: {"type": i["type"], "paths": sorted(i["paths"])} for n, i in cur.items()}


def as_fields(raw: dict) -> list[dict]:
    """Convertit un schéma observé (`{name: {type, paths}}`) en champs pour l'UI :
    `[{name, label, type}]`. `label` = les chemins (montre où la clé apparaît, ex.
    `skills[].name · languages[].name`)."""
    out = []
    for name in sorted(raw):
        info = raw[name] or {}
        paths = info.get("paths") or []
        label = " · ".join(paths) if paths and paths != [name] else None
        out.append({"name": name, "label": label, "type": info.get("type", "string")})
    return out
