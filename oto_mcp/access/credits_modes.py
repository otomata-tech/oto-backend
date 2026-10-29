"""The budget's settings PER SCALE FUNCTION (#1376): a function that runs in its own mode,
and the floor a hosted agent's job must have before it starts on a re-billed key.

**A mode per function** (`OTO_CREDITS_MODE_FUNCTIONS`): a comma list of
`<connector>.<function>=<mode>`; a function ending in `*` names every function that starts
with what precedes it. An EXACT name always wins over a pattern, and among patterns the
longest prefix wins; any other function keeps the instance's mode (`credits.mode`). The
case it exists for: `anthropic.messages:*=enforce` — model calls refuse while the tools
stay in `report`.

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


def modes_par_fonction() -> dict[tuple[str, str], str]:
    """`{(connector, function): mode}`. Absent or empty: none. Unreadable or named twice:
    raises, naming the item."""
    from .credits import _MODES
    # Names LITERAL: the inventory (`env_inventory.py`) finds its reads by the AST.
    lu = os.environ.get("OTO_CREDITS_MODE_FUNCTIONS", "")
    out: dict[tuple[str, str], str] = {}
    for item in (x.strip() for x in lu.split(",")):
        if not item:
            continue
        cle, _, m = item.partition("=")
        connector, _, function = cle.strip().partition(".")
        connector, function, m = connector.strip(), function.strip(), m.strip().lower()
        if not connector or not function or m not in _MODES:
            raise ValueError(f"{_ENV_MODE_FONCTIONS}: {item!r} is not "
                             f"`<connector>.<function>=<mode>`, mode one of "
                             f"{', '.join(_MODES)}")
        if (connector, function) in out:
            raise ValueError(f"{_ENV_MODE_FONCTIONS}: `{connector}.{function}` is named "
                             "twice")
        out[(connector, function)] = m
    return out


def _prefixe(function: str) -> Optional[str]:
    """What a pattern names the start of (`messages:*` → `messages:`); None: an exact
    name."""
    return function[:-len(_MOTIF)] if function.endswith(_MOTIF) else None


def mode_de(connector: str, function: Optional[str]) -> str:
    """The mode of ONE scale function: its exact entry, else the pattern with the longest
    prefix, else the instance's (`credits.mode`)."""
    from .credits import mode
    if function is None:
        return mode()
    retenu, longueur = None, -1
    for (c, f), m in modes_par_fonction().items():
        if c != connector:
            continue
        p = _prefixe(f)
        if p is None:
            if f == function:
                return m
        elif function.startswith(p) and len(p) > longueur:
            retenu, longueur = m, len(p)
    return retenu if retenu is not None else mode()


def un_mode_arme() -> bool:
    """Does any function reserve — the instance's mode, or one of its own, not `off`?"""
    from .credits import mode
    return mode() != "off" or any(m != "off" for m in modes_par_fonction().values())


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
    modes_par_fonction()
    plancher_travail()
