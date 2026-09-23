"""L'org d'un run, pour l'appel qui s'y fait (#639 — décision du 30/08/2026).

Sans axe `_org=`, un appel fait DANS un run se résolvait dans l'org MAISON du sub.
Mesuré en production le 29/08 (#631/#638) : un `data_write` sans `_org`, dans un run
ouvert sur l'org 226, résolu dans l'org 2 et refusé « namespace inconnu » sur un tableau
que la réservation du même run venait de résoudre — 82 refus sur sept jours, puis 109 sur
les sept suivants, tous des `data_write` du runner. Et le journal stampait la maison
(#630) : la vue filtrée par org ne montrait pas l'appel.

Décidé le 30/08 : **l'org d'un appel qui porte un run et aucun `_org` est
`runs.org_id`**. Trois règles :

- `_org=`/`_project=` explicites gardent la priorité — l'agent multi-org ne change pas
  (1 873 appels sur 32 115 en run sur sept jours, tous portés par un axe) ;
- l'appartenance reste exigée : un sub qui n'est pas (plus) membre de l'org du run est
  REFUSÉ, nommément — jamais un repli silencieux sur la maison (tourner sous une autre
  org que celle demandée est pire que se faire rejeter, ADR 0038) ;
- un run inconnu de `runs`, ou hors org, ne pose rien : `_run_id` y reste ce qu'il était,
  un identifiant opaque de corrélation (`call_axes._pin_run`).

Une lecture par appel, pas une par seam : le middleware résout ici, une fois, APRÈS les
axes, et pose une ContextVar que `access.current_org` relit entre le jeton et la maison.
`runs.org_id` est immuable (seuls `outcome`/`note`/`finished_at` bougent après la pose) —
d'où un cache mémoire borné par run : le runner ouvre une session par appel, et sans lui
chaque appel d'un run relirait `runs`.
"""
from __future__ import annotations

import logging
from typing import Optional

from .mcp_errors import McpError
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ErrorData, INVALID_PARAMS
from starlette.concurrency import run_in_threadpool

from . import call_axes, db, session_org

logger = logging.getLogger(__name__)

# run_id -> org_id (None = run hors org). Insertion-ordered → éviction du plus ancien.
_ORG_DU_RUN: dict[str, Optional[int]] = {}
_CAP = 10_000


def org_of_run(run_id: str) -> Optional[int]:
    """`runs.org_id` du run — None si le run est inconnu ou hors org. Un run connu est
    mis en cache (son org ne change jamais) ; un run inconnu ne l'est pas (sa ligne
    peut naître après — `run_start` la pose en best-effort)."""
    if run_id in _ORG_DU_RUN:
        return _ORG_DU_RUN[run_id]
    head = db.get_run_head(run_id)
    if head is None:
        return None
    org = head.get("org_id")
    org = int(org) if org is not None else None
    _ORG_DU_RUN[run_id] = org
    while len(_ORG_DU_RUN) > _CAP:
        del _ORG_DU_RUN[next(iter(_ORG_DU_RUN))]
    return org


class PasMembre(ValueError):
    """Le sub n'est pas (plus) membre de l'org du run."""


def _resoudre(sub: str, run_id: str) -> Optional[int]:
    """Sync (threadpool) : l'org du run, gardée par l'appartenance du sub — la même
    appartenance que lit l'autz (`roles.is_org_member`). None = rien à poser."""
    from . import org_store, roles
    org = org_of_run(run_id)
    if org is None:
        return None
    if not roles.is_org_member(sub, org):
        nom = (org_store.get_org(org) or {}).get("name") or "sans nom"
        raise PasMembre(
            f"Le run `{run_id}` se déroule dans l'org {org} « {nom} », dont tu n'es pas "
            "membre : l'appel n'y est pas résolu, et il ne l'est pas non plus dans ton "
            "org maison à sa place. Passe `_org=<une de tes orgs>` sur l'appel (liste : "
            "`oto_list_orgs`), ou ouvre ton propre run avec `run_start`.")
    return org


async def pin_for_call() -> list:
    """Pose l'org du run pour l'appel courant, ou rien. À appeler APRÈS les axes : un
    `_org=`/`_project=` explicite a déjà posé l'org et garde la priorité — ni lecture,
    ni garde alors. Le sub se lit là où les gardes des axes le lisent (`call_axes`).
    Rend la liste d'annulation, même forme (LIFO) que les axes."""
    run_id = session_org.current_call_run()
    if not run_id or session_org.current_call_org() is not None:
        return []
    sub = None
    try:
        sub = call_axes.current_user_sub_from_token()
    # noqa: SILENT — sans identité (endpoint anonyme) il n'y a personne à garder : la pose est inerte
    except Exception:  # noqa: BLE001
        pass
    if not sub:
        return []
    try:
        org = await run_in_threadpool(_resoudre, sub, run_id)
    except PasMembre as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    except Exception:
        logger.exception("org du run illisible pour sub=%s run=%s", sub, run_id)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Impossible de résoudre l'org du run `{run_id}` (erreur interne) — "
                     "l'appel n'est pas résolu dans ton org maison à sa place. Réessaie, "
                     "ou passe `_org=` explicitement.")))
    if org is None:
        return []
    return [(session_org.reset_call_run_org, session_org.set_call_run_org(org))]


# ── Org du run pour la VISIBILITÉ d'une session (#1058, 23/09/2026) ────────────
# À l'`initialize`, aucun jeton d'appel n'existe encore (`_run_id=` n'arrive qu'avec
# le premier `call_tool`) : la visibilité (`session_visibility`) dérivait donc
# TOUJOURS de la maison, jamais de l'org du run. Mesuré le 22/09 : une bascule de la
# maison du compte porteur des workers de flotte (2 → 226, sans déploiement) a masqué
# tous les connecteurs de TOUTES les flottes de ce compte pendant ~2h, alors même que
# les APPELS de ces flottes continuaient de s'exécuter (et de se journaliser) sous
# l'org de la flotte — seule la boîte affichée suivait la maison.
#
# Le runner ouvrant une session PAR APPEL (cf. `pin_for_call`), il connaît déjà le
# run au moment d'ouvrir la connexion : il doit porter `X-Oto-Run` sur la requête HTTP
# d'`initialize`, comme la face REST le porte déjà sur chacune des siennes. Sans cet
# en-tête (session humaine, dashboard, claude.ai) : comportement inchangé, dérive de
# la maison — ce chemin ne concerne QUE les sessions qui déclarent un run.
def _resoudre_pour_visibilite(sub: str, run_id: str) -> Optional[int]:
    """Sync (threadpool) : même source de vérité que `_resoudre`, mais **fail-open** —
    jamais de refus au handshake. La visibilité est de la gouvernance, pas une
    barrière (ADR 0031) ; la vraie garde d'appartenance reste `pin_for_call`, jouée à
    CHAQUE appel. Un run inconnu, ou un sub qui n'en est pas membre, ne change rien :
    la visibilité retombe sur la dérivation historique (maison)."""
    from . import roles
    org = org_of_run(run_id)
    if org is None:
        return None
    if not roles.is_org_member(sub, org):
        return None
    return org


async def resolve_visibility_org(sub: str) -> Optional[int]:
    """Org à passer à `session_visibility.apply_session_visibility(..., org=…)` pour
    une session ouverte avec `X-Oto-Run` — ou `None` (dérive de la maison, comme
    avant). Lue au HANDSHAKE, donc avant tout `_run_id=` d'appel : l'en-tête HTTP est
    la seule voie possible à ce stade (`get_http_headers`, même patron que
    `handshake_log`)."""
    headers = get_http_headers(include={"x-oto-run"})
    run_id = (headers.get("x-oto-run") or "").strip()
    if not run_id:
        return None
    try:
        return await run_in_threadpool(_resoudre_pour_visibilite, sub, run_id)
    except Exception as e:  # noqa: BLE001 — fail-open : un hoquet ne casse pas le handshake
        logger.warning("org de run illisible pour la visibilité (sub=%s run=%s): %s",
                       sub, run_id, e)
        return None
