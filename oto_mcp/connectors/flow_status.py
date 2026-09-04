"""Les verbes « lire l'état / déconnecter » d'un connecteur OAuth fédéré — déclarés
par son module, dérivés partout. Décalque symétrique de `connector_flow` (`flow.py`,
verbe « connecter ») pour le couple statut/déconnexion (oto-dashboard#125).

**Le problème que ça ferme.** `me.connector_connect` a un registre (`declare`) alimenté
au niveau MODULE par chaque connecteur à flux : un seul chemin fixe, dérivé partout.
Les deux autres moitiés du même geste — « suis-je connecté ? », « déconnecte-moi » —
n'avaient pas d'équivalent : le dashboard construisait encore son URL à partir du NOM
du connecteur (`/api/${name}/oauth/status`, `DELETE /api/${name}/oauth`), pour les
trois connecteurs OAuth fédérés (atlassian, folkmcp, google). Ce module ferme la moitié
`disconnect` ; `status` reste déclarable ICI (même forme que `declare`), mais rien ne
l'appelle dans ce lot.

**Pourquoi `status` existe sans être câblé.** La contrainte 1 d'oto-dashboard#125
(arbitrage du 04/09/2026) interdit à `me.connector_status` d'interroger un module
`auth.*` en parallèle de `/api/me` : son état DOIT venir d'`access.status_for`, la
MÊME source, jamais d'un second appel qui pourrait diverger. Le verbe `status` de ce
registre est donc de l'infrastructure symétrique (même forme que `disconnect`, pour
qu'un futur connecteur qui voudrait un lecteur dédié n'ait pas à inventer un second
patron) — `capabilities/connectors/oauth_status.py` ne branche QUE `disconnect`
dessus, jamais `status`.

**Ce que le seam garantit.** Une déclaration pure au niveau MODULE (comme
`connector_flow.declare`), lisible dès l'import, sans effet de bord. Les callables
eux-mêmes peuvent importer paresseusement leur module `auth.*` (celui-ci monte des
clients HTTP et lit sa config au chargement) — la déclaration ne les force pas à
charger avant l'appel réel, exactement comme `federated_oauth._federation()._module()`.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusFlow:
    connector: str
    # (ctx) -> dict, jamais appelé par ce lot (cf. docstring du module) — `None` =
    # non déclaré, ce qui est le cas des trois connecteurs câblés aujourd'hui.
    status: Optional[Callable[..., dict]] = None
    # (ctx) -> dict — le seul verbe réellement branché par ce lot.
    disconnect: Optional[Callable[..., dict]] = None


_FLOWS: dict[str, StatusFlow] = {}


def declare_status(connector: str, *, status: Optional[Callable[..., dict]] = None,
                    disconnect: Optional[Callable[..., dict]] = None) -> None:
    """Déclare les verbes statut/déconnexion de ce connecteur OAuth fédéré. Appelé au
    niveau MODULE (comme `connector_flow.declare`) : une déclaration pure, lisible dès
    l'import, sans attendre le montage FastMCP."""
    _FLOWS[connector] = StatusFlow(connector=connector, status=status, disconnect=disconnect)


def supports(connector: str) -> bool:
    return connector in _FLOWS


def entries() -> dict[str, StatusFlow]:
    return dict(_FLOWS)


async def _run(fabrique: Callable[..., dict], ctx) -> dict:
    """Exécute le callable déclaré et rend sa forme brute — même discipline que
    `connector_flow.start` : un flux peut être asynchrone (fournisseur hébergé) ou
    synchrone-mais-bloquant (révocation HTTP, cf. `google_oauth.revoke`), et dans les
    deux cas le serveur mono-loop ne doit jamais l'exécuter en bloquant."""
    if inspect.iscoroutinefunction(fabrique):
        out = await fabrique(ctx)
    else:
        out = await asyncio.to_thread(fabrique, ctx)
        if inspect.isawaitable(out):
            out = await out
    if not isinstance(out, dict):
        raise TypeError(
            f"le verbe déclaré doit rendre un dict (reçu {type(out).__name__})")
    return out


async def read_status(connector: str, ctx) -> dict:
    f = _FLOWS[connector].status
    if f is None:
        raise KeyError(f"« {connector} » n'a pas de lecteur de statut déclaré ici")
    return await _run(f, ctx)


async def disconnect(connector: str, ctx) -> dict:
    f = _FLOWS[connector].disconnect
    if f is None:
        raise KeyError(f"« {connector} » n'a pas de verbe disconnect déclaré ici")
    return await _run(f, ctx)
