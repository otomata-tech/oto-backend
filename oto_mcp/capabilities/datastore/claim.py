"""Capacités « réserver une ligne » — la file de travail, côté application (signal #362).

`claim_next` et le bail (ADR 0046 D) n'existaient que côté MCP : une application web
pouvait LIRE la file (`…/queue`) et libérer, jamais RÉSERVER. Les fronts compensaient
en écrivant un verrou dans les données de la ligne — ça tient tant que l'équipe est
coopérative, mais ce n'est pas atomique (deux personnes qui cliquent à la même seconde
obtiennent la même ligne) et ça oblige chaque tableau à prévoir deux colonnes pour une
mécanique déjà en base.

Deux gestes, REST-only :

- `me.datastore.claim_next` — POST …/claim_next          : la prochaine ligne libre
  (`max_claims` optionnel : plafond de reprises pour cette passe, #433) ;
- `me.datastore.claim_row`  — POST …/rows/{row_id}/claim : CETTE ligne-là.

`mcp=None` sur les deux, opt-out explicite : la face agent de `claim_next` existe déjà
(tool `data_claim_next`), en ajouter une seconde serait le doublon que la convergence
combat ; et réserver une ligne NOMMÉE est le geste d'un humain qui choisit qui il
appelle, là où un agent qui draine prend la suivante. Le jour où l'agent en a besoin,
c'est une ligne `mcp=` à poser ici — pas un tool à réécrire.

`worker` est le libellé rejoué au release : c'est LA garde du bail (on ne libère pas
la réservation d'un autre), d'où son exigence des deux côtés. Autz `SUB_ONLY` au
seuil, le vrai gate est le droit d'ÉCRITURE sur le tableau — résolu par le store
(org active + ownership), jamais par le nom passé en path ; un tableau hors périmètre
répond 404, comme partout ailleurs dans le datastore.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ...datastore import claimable
from ...datastore import journal as datastore_journal
from ...datastore.core import (
    NamespaceNotFound,
    NamespaceReadOnly,
    RowClaimed,
    RowNotFound,
    RowOutsideClaimable,
    make_store,
)
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES
from ._forme import _LAYERS, _layers


class ClaimNextInput(BaseModel):
    namespace: str
    # oto#63 : la RÉSERVATION est le seul chemin qui alimente une boucle d'écriture,
    # et c'était le seul à ne pas porter la forme. Réutilise le champ des lectures —
    # même nom, même défaut, même refus nommé sur une valeur inconnue.
    layers: str = _LAYERS
    # Défaut vide plutôt que champ requis : un `worker` manquant mérite un refus qui
    # DIT ce qu'est un worker (le `invalid_input` de pydantic ne le dirait pas).
    worker: str = ""
    filter: Optional[dict] = None
    # #356 : la forme en LISTE, seule capable de porter deux bornes sur une même
    # colonne — `filter` refuse plus d'un opérateur par colonne. Cumulée en ET.
    filters: Optional[list] = None
    lease_s: Optional[int] = None
    # Plafond de reprises pour CETTE passe (#433) — il serre le `lifecycle.max_claims`
    # du tableau sans le modifier. Absent = la déclaration du tableau fait foi.
    max_claims: Optional[int] = None


class ClaimRowInput(BaseModel):
    namespace: str
    row_id: str
    layers: str = _LAYERS
    worker: str = ""
    lease_s: Optional[int] = None


class ClaimResult(BaseModel):
    namespace: str
    # La ligne réservée, colonnes libres du tableau + son bail (`_claimed_by`,
    # `_claimed_until`, `_claimed_run`) + ce que la file sait d'elle (`_claims`, et
    # `_abandon` si le plafond de reprises l'en a sortie). `null` sur `claim_next`
    # quand il n'y a plus rien à réserver.
    row: Optional[dict] = None
    # Défaut de configuration relevé au claim (statut sans état
    # terminal) : le worker qui réserve est celui que ça concerne.
    warning: Optional[str] = None
    hint: Optional[str] = None


def _worker(raw: str) -> str:
    w = (raw or "").strip()
    if not w:
        raise AuthzDenied(
            400, "worker_required",
            "worker = libellé stable de celui qui réserve (une personne, un poste, "
            "un agent), rejoué tel quel au release : c'est la garde du bail.")
    return w


def _lease(inp) -> dict:
    """Le défaut du bail appartient au store, pas à l'appelant : `lease_s` absent
    ne se relaie pas."""
    return {} if inp.lease_s is None else {"lease_s": int(inp.lease_s)}


def _claim_next(ctx: ResolvedCtx, inp: ClaimNextInput) -> dict:
    worker = _worker(inp.worker)
    trace: dict = {}
    warnings: list = []
    perimetre: dict = {}
    try:
        row = make_store(ctx.sub).claim_next(
            inp.namespace, worker=worker, filter=inp.filter,
            max_claims=inp.max_claims, warnings=warnings, trace=trace,
            perimetre=perimetre, layers=_layers(inp.layers), filters=inp.filters,
            **_lease(inp))
    except NamespaceNotFound:
        raise AuthzDenied(404, "namespace_not_found")
    except NamespaceReadOnly:
        raise AuthzDenied(403, "namespace_read_only")
    except ValueError as e:
        raise AuthzDenied(400, "invalid_claim", str(e))
    if row:
        datastore_journal.record(
            datastore_journal.TOOL_CLAIM_NEXT, sub=ctx.sub,
            ctx=datastore_journal.from_trace(trace, inp.namespace), row_id=row.get("_id"))
    return {
        "namespace": inp.namespace, "row": row,
        **({"warning": warnings[0]} if warnings else {}),
        # File vide ≠ erreur — mais ça se dit, sinon un `row: null` se lit comme un bug.
        # Et quand le tableau déclare un périmètre, c'est LUI qui se nomme (#517).
        **({} if row else {"hint": _hint_vide(perimetre, inp.filter)}),
    }


def _hint_vide(perimetre: dict, filtre: Optional[dict]) -> str:
    if perimetre:
        return claimable.phrase_vide(perimetre, filtre)
    return "plus rien à réserver (file vide pour ce filtre, ou tout est sous bail actif)"


def _claim_row(ctx: ResolvedCtx, inp: ClaimRowInput) -> dict:
    worker = _worker(inp.worker)
    trace: dict = {}
    warnings: list = []
    try:
        row = make_store(ctx.sub).claim_row(
            inp.namespace, inp.row_id, worker=worker,
            warnings=warnings, trace=trace, layers=_layers(inp.layers),
            **_lease(inp))
    except NamespaceNotFound:
        raise AuthzDenied(404, "namespace_not_found")
    except NamespaceReadOnly:
        raise AuthzDenied(403, "namespace_read_only")
    except RowNotFound:
        raise AuthzDenied(404, "row_not_found")
    except RowOutsideClaimable as e:
        # 409 comme `row_claimed` : ce n'est pas un droit qui manque ni un corps mal
        # formé, c'est l'ÉTAT de la ligne contre ce que le tableau sert. Le périmètre
        # part en `details` — un front l'affiche sans reparser une phrase française.
        raise AuthzDenied(409, "row_outside_claimable", str(e),
                          details={"claimable": e.perimetre})
    except RowClaimed as e:
        # 409 et non 403 : le refus n'est pas un droit qui manque, c'est un collègue
        # plus rapide — et l'utilisateur a besoin de savoir QUI et jusqu'à QUAND.
        raise AuthzDenied(
            409, "row_claimed",
            f"ligne déjà réservée par « {e.claimed_by} » jusqu'à {e.claimed_until}")
    except ValueError as e:
        raise AuthzDenied(400, "invalid_claim", str(e))
    datastore_journal.record(
        datastore_journal.TOOL_CLAIM, sub=ctx.sub,
        ctx=datastore_journal.from_trace(trace, inp.namespace), row_id=inp.row_id)
    return {"namespace": inp.namespace, "row": row,
            **({"warning": warnings[0]} if warnings else {})}


CAPABILITIES += [
    Capability(
        key="me.datastore.claim_next",
        handler=_claim_next,
        Input=ClaimNextInput,
        Output=ClaimResult,
        authz=SUB_ONLY,
        mcp=None,  # `data_claim_next` tient déjà la face agent
        rest=RestBinding(verb="POST", path="/api/datastore/namespaces/{namespace}/claim_next"),
        description="Réserve atomiquement la prochaine ligne libre d'un tableau (file de travail).",
    ),
    Capability(
        key="me.datastore.claim_row",
        handler=_claim_row,
        Input=ClaimRowInput,
        Output=ClaimResult,
        authz=SUB_ONLY,
        mcp=None,  # geste d'un humain qui choisit sa ligne ; l'agent draine
        rest=RestBinding(verb="POST",
                         path="/api/datastore/namespaces/{namespace}/rows/{row_id}/claim"),
        description="Réserve une ligne nommée d'un tableau (409 si déjà sous bail d'un autre).",
    ),
]
