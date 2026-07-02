"""Slots de procédure (ADR 0035, B1) — déclaration d'entités requises + convention
de référence par nom dans la prose.

Une procédure (`org_instructions`) déclare ses **entités à instance** (quel tableau,
quel compte de connecteur, quelle base) sous forme de **slots typés nommés** dans la
colonne JSONB `slots` : `{name, type, description?, connector?}`. La prose les
référence **par nom** via le marqueur `<slot:name>` (même famille que `<tool:slug>`
d'ADR 0014) — l'agent sait toujours de quelle entité on parle, jamais un nom
d'instance en dur (le binding nom→instance vit dans le projet, `project_links`).

B1 = canari no-op : déclaration + vérification croisée à l'écriture (`slots_check`),
AUCUN effet runtime (pas de résolution ni d'enforcement — B3). Les types réutilisent
la taxonomie `target_type` de `project_links` (sous-ensemble à instance).

« derive don't duplicate » : la logique marqueur→slot ne vit qu'ici.
"""
from __future__ import annotations

import re
from typing import Optional

from . import providers, tool_registry
from .tool_visibility import namespace_of

# Sous-ensemble À INSTANCE de la taxonomie project_links.target_type (ADR 0035
# arbitrages) : `procedure`/`page` ne se bindent pas via un slot.
SLOT_TYPES = ("tableau", "connecteur", "base")

# Nom de slot = clé du binding côté projet → même hygiène qu'un slug.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Convention d'écriture dans la prose (forme fixée en B1, cf. ADR 0035 §questions).
MARKER = re.compile(r"<slot:([a-z0-9_-]+)>")

_MAX_SLOTS = 32
_MAX_DESC = 500


def validate_slots(raw: object) -> list[dict]:
    """Valide et normalise une déclaration de slots. Lève `ValueError` avec un
    message ACTIONNABLE (structure, type inconnu, nom invalide/dupliqué) — les
    incohérences DOUCES (connecteur inconnu du registre, slot jamais référencé)
    sont des warnings de `slots_check`, jamais un refus (soft-binding 0014)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("`slots` doit être une liste d'objets {name, type, description?}.")
    if len(raw) > _MAX_SLOTS:
        raise ValueError(f"`slots` : {_MAX_SLOTS} entrées max.")
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"`slots[{i}]` doit être un objet {{name, type, description?}}.")
        name = str(item.get("name") or "").strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError(
                f"`slots[{i}].name` invalide ({name!r}) — attendu [a-z0-9][a-z0-9_-]*, 64 car. max.")
        if name in seen:
            raise ValueError(f"`slots` : nom `{name}` dupliqué (le nom est la clé du binding).")
        seen.add(name)
        stype = str(item.get("type") or "").strip().lower()
        if stype not in SLOT_TYPES:
            raise ValueError(
                f"`slots[{i}].type` invalide ({stype!r}) — attendu {'|'.join(SLOT_TYPES)}.")
        slot: dict = {"name": name, "type": stype}
        desc = item.get("description")
        if desc:
            slot["description"] = str(desc).strip()[:_MAX_DESC]
        connector = item.get("connector")
        if connector is not None and stype != "connecteur":
            raise ValueError(f"`slots[{i}].connector` réservé au type `connecteur`.")
        if stype == "connecteur":
            # Le connecteur visé : champ `connector` explicite, sinon le nom du slot.
            slot["connector"] = str(connector or name).strip().lower()
        unknown = set(item) - {"name", "type", "description", "connector"}
        if unknown:
            raise ValueError(f"`slots[{i}]` : champs inconnus {sorted(unknown)}.")
        out.append(slot)
    return out


def slot_refs(text: str) -> list[str]:
    """Noms de slots cités via `<slot:name>` dans la prose, dédupliqués, dans l'ordre."""
    out: list[str] = []
    seen: set[str] = set()
    for m in MARKER.finditer(text or ""):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _referenced_connectors(body_md: str) -> set[str]:
    """Connecteurs (noms du registre) des tools cités `<tool:slug>` dans la prose."""
    out: set[str] = set()
    for name in tool_registry.ref_names(body_md):
        con = providers.connector_for_namespace(namespace_of(name))
        if con is not None:
            out.add(con.name)
    return out


def slots_check(body_md: str, slots: Optional[list]) -> dict:
    """Vérification croisée à l'écriture (ADR 0035, pendant du `write_check` 0014).
    **Non bloquant** — l'écriture a lieu, l'auteur (IA ou UI) reçoit les signaux :
    - `unresolved_slots` : `<slot:name>` dans la prose sans déclaration (ref morte) ;
    - `unreferenced_slots` : déclaré, jamais cité dans la prose ;
    - `slot_warnings` : incohérences douces (connecteur inconnu du registre,
      connecteur déclaré dont aucun tool n'est référencé) ;
    - `suggested_slots` : connecteurs à identités référencés par `<tool:>` mais non
      déclarés (la prose trahit un besoin de binding — grandfathering, suggestion).
    Best-effort : jamais d'exception (un check ne casse pas une écriture)."""
    slots = slots or []
    declared = {s["name"] for s in slots}
    refs = slot_refs(body_md)
    result = {
        "slots": slots,
        "unresolved_slots": [n for n in refs if n not in declared],
        "unreferenced_slots": sorted(declared - set(refs)),
        "slot_warnings": [],
        "suggested_slots": [],
    }
    try:
        referenced = _referenced_connectors(body_md)
        declared_connectors: set[str] = set()
        for s in slots:
            if s["type"] != "connecteur":
                continue
            con = s.get("connector") or s["name"]
            declared_connectors.add(con)
            if con not in providers.REGISTRY:
                result["slot_warnings"].append(
                    f"slot `{s['name']}` : connecteur `{con}` inconnu du registre.")
            elif con not in referenced:
                result["slot_warnings"].append(
                    f"slot `{s['name']}` : aucun tool `<tool:{con}_…>` référencé dans la prose.")
        # L'inverse (suggestion, jamais un warning) : un tool d'un connecteur à
        # IDENTITÉS est cité sans slot connecteur déclaré → binding probablement requis.
        from . import connector_identities
        for con in sorted(referenced - declared_connectors):
            if connector_identities.supports(con):
                result["suggested_slots"].append(
                    {"name": con, "type": "connecteur", "connector": con,
                     "reason": f"la prose référence des tools `{con}` (connecteur à identités) "
                               "sans slot déclaré — le projet ne saura pas quel compte binder."})
    except Exception:  # noqa: BLE001 — check best-effort, jamais bloquant
        pass
    return result
