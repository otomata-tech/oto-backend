"""The budget's settings PER SCALE FUNCTION (#1376): a function that runs in its own mode,
and the floor a hosted agent's job must have before it starts on a re-billed key.

**A mode per function** (`OTO_CREDITS_MODE_FUNCTIONS`): a comma list of
`<connector>.<function>=<mode>`; a function ending in `*` names every function that starts
with what precedes it. An EXACT name always wins over a pattern, and among patterns the
longest prefix wins; any other function keeps the instance's mode (`credits.mode`). The
case it exists for: `anthropic.messages:*=enforce` — model calls refuse while the tools
stay in `report`.

**A mode per surface** (same variable): an entry prefixed by a surface,
`<surface>/anthropic.<function>=<mode>`, holds only for the model turns of that surface —
`runner` (a hosted agent's job, turn key `runner:…`) or `copilot` (the in-app agent, turn
key `copilot:…`). The surface is the turn key's own prefix (`credits_tours.surface_du_tour`),
and only model turns have one: a surface on another connector refuses to start. Order,
first match wins: the surface's exact name, the surface's longest pattern, the exact name,
the longest pattern, the instance's mode. Without a surface entry, both surfaces read
exactly what they read before. The case it exists for: `runner/anthropic.messages:*=enforce`
— the runner refuses below the floor while the copilot keeps the mode it had.

Each entry must name a function the scale can price (`credits_tours.fonctions_connues`): a connector
function with a declared cost (`oto.connectors.declared`), or a model call
`anthropic.messages:<model>` of the hosted agents' catalogue (`runner_models`). A name
that matches none refuses to start: a typo would otherwise leave that function in the
instance's mode, in silence.

**The floor** (`OTO_CREDITS_JOB_FLOOR`, budget units): the first turn's input is never
reserved, so a job whose payer cannot cover it does not start to die on its first call
(`credits_tours.sous_le_plancher`). Absent or 0: none.

Both are read on each call; an unreadable value raises, and refuses to start
(`credits.verifier_reglages`, through `verifier`).
"""
from __future__ import annotations

import os
from typing import Optional

_ENV_MODE_FONCTIONS = "OTO_CREDITS_MODE_FUNCTIONS"
_ENV_PLANCHER = "OTO_CREDITS_JOB_FLOOR"
_MOTIF = "*"
#: The surfaces a model turn is called from: the prefix of its hold key.
SURFACES = ("runner", "copilot")


def entrees() -> dict[tuple[Optional[str], str, str], str]:
    """`{(surface, connector, function): mode}`, surface None for an entry without one.
    Absent or empty: none. Unreadable (syntax, mode, surface, a surface on a connector
    without turns) or named twice: raises, naming the item."""
    from .credits import _MODES
    from .credits_tours import MODELE_CONNECTOR
    # Names LITERAL: the inventory (`env_inventory.py`) finds its reads by the AST.
    lu = os.environ.get("OTO_CREDITS_MODE_FUNCTIONS", "")
    out: dict[tuple[Optional[str], str, str], str] = {}
    for item in (x.strip() for x in lu.split(",")):
        if not item:
            continue
        cle, _, m = item.partition("=")
        connector, _, function = cle.strip().partition(".")
        # The surface prefixes the connector: a function's own `/` stays its own.
        surface, barre, connector = connector.rpartition("/")
        surface = surface.strip() if barre else None
        connector, function, m = connector.strip(), function.strip(), m.strip().lower()
        if not connector or not function or m not in _MODES:
            raise ValueError(f"{_ENV_MODE_FONCTIONS}: {item!r} is not "
                             f"`[<surface>/]<connector>.<function>=<mode>`, mode one of "
                             f"{', '.join(_MODES)}")
        if surface is not None and surface not in SURFACES:
            raise ValueError(f"{_ENV_MODE_FONCTIONS}: {item!r} — unknown surface "
                             f"`{surface}`, one of {', '.join(SURFACES)}")
        if surface is not None and connector != MODELE_CONNECTOR:
            raise ValueError(f"{_ENV_MODE_FONCTIONS}: {item!r} — a surface names model "
                             f"turns only (`{MODELE_CONNECTOR}.<function>`)")
        if (surface, connector, function) in out:
            raise ValueError(f"{_ENV_MODE_FONCTIONS}: `{nom(surface, connector, function)}` "
                             "is named twice")
        out[(surface, connector, function)] = m
    return out


def modes_par_fonction() -> dict[tuple[str, str], str]:
    """`{(connector, function): mode}` of the entries WITHOUT a surface (`entrees`)."""
    return {(c, f): m for (s, c, f), m in entrees().items() if s is None}


def nom(surface: Optional[str], connector: str, function: str) -> str:
    """An entry as the setting writes it."""
    return f"{surface}/{connector}.{function}" if surface else f"{connector}.{function}"


def _prefixe(function: str) -> Optional[str]:
    """What a pattern names the start of (`messages:*` → `messages:`); None: an exact
    name."""
    return function[:-len(_MOTIF)] if function.endswith(_MOTIF) else None


def _choisir(lus: dict[str, str], function: str) -> Optional[str]:
    """Among `{function: mode}`: the exact entry, else the pattern with the longest
    prefix, else None."""
    if function in lus and _prefixe(function) is None:
        return lus[function]
    retenu, longueur = None, -1
    for f, m in lus.items():
        p = _prefixe(f)
        if p is not None and function.startswith(p) and len(p) > longueur:
            retenu, longueur = m, len(p)
    return retenu


def mode_de(connector: str, function: Optional[str],
            surface: Optional[str] = None) -> str:
    """The mode of ONE scale function, called from `surface` (None: a tool call): the
    surface's exact entry, else its longest pattern, else the exact entry, else the
    longest pattern, else the instance's (`credits.mode`)."""
    from .credits import mode
    if surface is not None and surface not in SURFACES:
        raise ValueError(f"unknown surface `{surface}`, one of {', '.join(SURFACES)}")
    if function is None:
        return mode()
    lus = entrees()
    for s in ((surface, None) if surface is not None else (None,)):
        m = _choisir({f: m for (s_, c, f), m in lus.items() if s_ == s and c == connector},
                     function)
        if m is not None:
            return m
    return mode()


def un_mode_arme() -> bool:
    """Does any function reserve — the instance's mode, or one of its own, not `off`?"""
    from .credits import mode
    return mode() != "off" or any(m != "off" for m in entrees().values())


def plancher_travail() -> int:
    """The floor, in budget units. Absent or empty: 0. Unreadable or negative: raises."""
    lu = os.environ.get("OTO_CREDITS_JOB_FLOOR", "")
    if not lu.strip():
        return 0
    try:
        n = int(lu.strip())
    except ValueError:
        n = -1
    if n < 0:
        raise ValueError(f"{_ENV_PLANCHER}={lu!r}: unreadable value, expected a whole "
                         "number of budget units (0 or more)")
    return n


def verifier() -> None:
    """The startup guard of the instance's mode and of these settings: each read once,
    and each per-function entry checked against what the scale can price."""
    from .credits import mode
    mode()
    entrees()
    plancher_travail()
