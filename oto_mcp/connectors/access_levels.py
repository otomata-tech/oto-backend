"""Access levels of an OAuth consent connector — the declaration, the coverage, the setting.

Design validated on 14/10/2026 (connectors repo, `docs/conception/niveaux-d-acces.md`).
A connector declares NAMED levels, `full` mandatory, each with the scopes its consent
asks for; a `scope_implies` (a provider fact, never a guessed rule: `ALL` covers `READ`
only if the declaration says so); and, for every tool — or every `op` of a multiplexed
tool — the scopes it REQUIRES. A tool or an op is **covered** by a level when each of its
scopes is in the closure of the level's scopes by `scope_implies`.

Three things this module holds:

- **The declaration is DATA** (`ACCESS_LEVELS` in `providers/<name>.py`, aggregated by
  `providers._ACCESS_LEVELS_BY_CONNECTOR`; for a connector served from its description, its
  `auth.access_levels`, read by `descriptions.niveaux_d_acces`), validated here once at
  import: a malformed declaration fails the boot, it is never half-read.
- **`full` filters nothing.** Every reader of a level asks `restricts(level)` first: at
  `full` the behaviour is exactly the one of a connector without levels — visibility,
  served schemas, call guard and consent alike.
- **The setting** lives in `connector_settings` (key `access_level`, scope `org` or
  `group`, connector = the credential CARRIER) and is read AT THE CALL, not from the
  cardinality snapshot: a restriction set on one color must hold on all of them at the
  next call. One query per call for every leveled connector (`lecture_d_appel`).
  Resolution: the active team's value if it has one, otherwise the org's, otherwise
  the connector's DEFAULT level — `full`, unless its declaration names another (a service
  account whose description says `default_level: read`: the keys installed before it
  gained a write level keep reading only, without anyone's gesture). Monotone like activation (ADR 0012): a team can narrow its org's level, never
  widen it — refused when set, and if the org narrows later, the org's level is the
  ceiling (a team value wider than it never applies).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .. import providers

#: The key of the setting in `connector_settings`.
KEY = "access_level"
#: The mandatory level — the consent of before levels, and no filtering.
FULL = "full"
#: The parameter that carries the verb of a multiplexed tool.
OP_PARAM = "op"


class AccessLevelError(ValueError):
    """A level gesture that cannot be honoured. `code` is the named refusal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AccessLevels:
    """The validated declaration of ONE connector."""
    connector: str
    levels: Mapping[str, tuple[str, ...]]
    scope_implies: Mapping[str, tuple[str, ...]]
    # tool → required scopes, or tool → {op → required scopes}
    tools: Mapping[str, object]
    #: The level of an org and a team that set none.
    default: str = FULL
    #: Widening the level needs a new consent (OAuth: a token obtained for a narrower
    #: level lacks its scopes). False for a service account: its next assertion claims them.
    widening_needs_consent: bool = True

    def closure(self, level: str) -> frozenset[str]:
        """The level's scopes and everything they imply, transitively."""
        vus: set[str] = set()
        pile = list(self.levels[level])
        while pile:
            s = pile.pop()
            if s not in vus:
                vus.add(s)
                pile.extend(self.scope_implies.get(s, ()))
        return frozenset(vus)

    def is_op_tool(self, tool: str) -> bool:
        return isinstance(self.tools.get(tool), Mapping)

    def required(self, tool: str, op: Optional[str] = None) -> Optional[tuple[str, ...]]:
        """The scopes `tool` (or its `op`) requires; None when nothing is declared for
        it — which the guard reads as NOT covered (fail-closed)."""
        decl = self.tools.get(tool)
        if isinstance(decl, Mapping):
            return decl.get(op) if op is not None else None
        return decl

    def covers(self, level: str, tool: str, op: Optional[str] = None) -> bool:
        exige = self.required(tool, op)
        return exige is not None and set(exige) <= self.closure(level)

    def covered_ops(self, level: str, tool: str) -> frozenset[str]:
        decl = self.tools.get(tool)
        if not isinstance(decl, Mapping):
            raise ValueError(f"`{tool}` is not declared op by op")
        return frozenset(op for op in decl if self.covers(level, tool, op))

    def narrower_or_equal(self, level: str, than: str) -> bool:
        """`level` grants nothing `than` does not: the monotony test of a team value."""
        return self.closure(level) <= self.closure(than)


def _scopes(valeur, ou: str) -> tuple[str, ...]:
    if not isinstance(valeur, tuple) or not all(isinstance(s, str) and s for s in valeur):
        raise ValueError(f"{ou}: a tuple of scope names is expected, got {valeur!r}")
    return valeur


def _valider(connector: str, data: Mapping) -> AccessLevels:
    """Builds the declaration of `connector`, or RAISES at import (never half-read)."""
    connues = {"levels", "scope_implies", "tools", "default", "widening_needs_consent"}
    if set(data) - connues:
        raise ValueError(f"{connector}: unknown keys {sorted(set(data) - connues)}")
    levels = {nom: _scopes(s, f"{connector}.levels.{nom}")
              for nom, s in (data.get("levels") or {}).items()}
    if FULL not in levels:
        raise ValueError(f"{connector}: the `{FULL}` level is mandatory")
    if any(not s for s in levels.values()):
        raise ValueError(f"{connector}: a level without scopes would ask for nothing")
    implies = {s: _scopes(v, f"{connector}.scope_implies.{s}")
               for s, v in (data.get("scope_implies") or {}).items()}
    tools: dict[str, object] = {}
    for tool, decl in (data.get("tools") or {}).items():
        if isinstance(decl, Mapping):
            if not decl:
                raise ValueError(f"{connector}.tools.{tool}: no op declared")
            tools[tool] = {op: (_scopes(s, f"{connector}.tools.{tool}.{op}") if s else ())
                           for op, s in decl.items()}
        else:
            tools[tool] = _scopes(decl, f"{connector}.tools.{tool}") if decl else ()
    defaut = data.get("default", FULL)
    if defaut not in levels:
        raise ValueError(f"{connector}: the default level `{defaut}` is not declared")
    consentement = data.get("widening_needs_consent", True)
    if not isinstance(consentement, bool):
        raise ValueError(f"{connector}: widening_needs_consent is a bool, got {consentement!r}")
    return AccessLevels(connector, levels, implies, tools, defaut, consentement)


def _declarations() -> dict:
    """The raw declarations: the hand-written connectors' `ACCESS_LEVELS`, then those the
    descriptions of the inscribed connectors declare. One connector, one source."""
    from . import descriptions
    out = dict(providers._ACCESS_LEVELS_BY_CONNECTOR)
    for c in descriptions.inscrits():
        data = descriptions.niveaux_d_acces(descriptions.charger(c.name))
        if data is None:
            continue
        if c.name in out:
            raise ValueError(f"{c.name}: access levels declared by hand AND by its description")
        out[c.name] = data
    return out


#: The validated declarations, by connector name.
DECLARED: dict[str, AccessLevels] = {
    nom: _valider(nom, data) for nom, data in _declarations().items()}


def declared(connector: str) -> Optional[AccessLevels]:
    return DECLARED.get(connector)


def for_tool(tool: str) -> Optional[AccessLevels]:
    """The declaration of the connector `tool` belongs to (longest declared namespace,
    `tool_visibility.namespace_of`), or None."""
    from ..tool_visibility import namespace_of
    con = providers.connector_for_namespace(namespace_of(tool))
    return DECLARED.get(con.name) if con is not None else None


def require_declared(connector: str) -> AccessLevels:
    decl = DECLARED.get(connector)
    if decl is None:
        raise AccessLevelError(
            "no_access_levels",
            f"`{connector}` declares no access levels: its consent always asks for the "
            f"same scopes, there is nothing to choose.")
    return decl


def require_level(connector: str, level: str) -> AccessLevels:
    decl = require_declared(connector)
    if level not in decl.levels:
        raise AccessLevelError(
            "unknown_access_level",
            f"`{level}` is not an access level of `{connector}` "
            f"(declared: {', '.join(sorted(decl.levels))}).")
    return decl


def scopes_for(connector: str, level: str) -> tuple[str, ...]:
    """The scopes the consent asks for at `level`."""
    return require_level(connector, level).levels[level]


def restricts(level: str) -> bool:
    return level != FULL


# --- the setting ---------------------------------------------------------------

def _carrier(connector: str) -> str:
    return providers.credential_provider(connector)


def _stored(connector: str, scope: str, valeur: Optional[str]) -> Optional[str]:
    """A stored value that is not a declared level RAISES: interpreting it would be
    deciding in place of whoever set it (same rule as `cle_perso.interdite`)."""
    if valeur is None:
        return None
    decl = DECLARED.get(connector)
    if decl is None or valeur not in decl.levels:
        raise ValueError(f"connector_settings {scope} {connector} {KEY}={valeur!r}: "
                         f"not a declared access level")
    return valeur


def settings(org_id: Optional[int], group_id: Optional[int]) -> dict[str, dict]:
    """`{connector: {"org": level|None, "group": level|None}}` for (org, team) — one
    query for every connector, remembered for the call."""
    if org_id is None and group_id is None:
        return {}
    from ..db import connector_settings
    brut = connector_settings.scoped_settings_for_key(
        KEY, None if org_id is None else str(org_id),
        None if group_id is None else str(group_id))
    return {c: {s: _stored(c, s, v) for s, v in par_scope.items()}
            for c, par_scope in brut.items()}


def resolve(connector: str, org_id: Optional[int], group_id: Optional[int],
            valeurs: Optional[dict] = None) -> tuple[str, str]:
    """`(level, source)` in force for `connector` under (org, active team). `source` ∈
    `group` | `org` | `default` (the connector's default level, `AccessLevels.default`). A
    connector without levels is `(full, default)`.
    `valeurs` = an already read `settings(org, group)` (the visibility reads it once for
    every connector)."""
    decl = DECLARED.get(connector)
    if decl is None:
        return FULL, "default"
    lu = (valeurs if valeurs is not None else settings(org_id, group_id)).get(
        _carrier(connector), {})
    org_level = lu.get("org") or decl.default
    group_level = lu.get("group")
    if group_level is not None and decl.narrower_or_equal(group_level, org_level):
        return group_level, "group"
    return org_level, ("org" if lu.get("org") else "default")


def effective_level(connector: str, org_id: Optional[int],
                    group_id: Optional[int]) -> str:
    return resolve(connector, org_id, group_id)[0]


def set_level(scope_type: str, scope_id: int, connector: str, level: str,
              set_by: Optional[str], *, org_id: Optional[int] = None) -> None:
    """Sets the level of an org (`scope_type='org'`) or a team (`'group'`, `org_id` =
    its org). A team level wider than its org's is REFUSED — never stored to be ignored."""
    from ..db import connector_settings
    decl = require_level(connector, level)
    if scope_type == "group":
        org_level = resolve(connector, org_id, None)[0]
        if not decl.narrower_or_equal(level, org_level):
            raise AccessLevelError(
                "access_level_wider_than_org",
                f"a team can narrow the access level of its organization, never widen "
                f"it: `{level}` grants more than `{org_level}`, the level of "
                f"organization #{org_id} for `{connector}`.")
    elif scope_type != "org":
        raise ValueError(f"scope_type {scope_type!r}: org or group")
    connector_settings.set_connector_setting(
        scope_type, str(scope_id), _carrier(connector), KEY, level, set_by)


def clear_level(scope_type: str, scope_id: int, connector: str) -> bool:
    """Removes the setting: the scope falls back on the level above (org, then the
    connector's default level)."""
    from ..db import connector_settings
    require_declared(connector)
    return connector_settings.clear_connector_setting(
        scope_type, str(scope_id), _carrier(connector), KEY)
