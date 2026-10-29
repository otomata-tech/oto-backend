"""Le PORT du budget d'unités — sous l'identité de service du commerce.

Le commerce ÉCRIT (le barème, qui paie dans une org, les versements), le cœur RELIT et
règle à chaque appel payant (`access/credits.py`, `docs/credits-au-moment-de-l-appel.md`).
Rien ici ne connaît de monnaie : une unité est un entier, que seul le commerce convertit.

Ce que le commerce y trouve :

- le BARÈME, versionné, posé d'un bloc (`PUT …/scales/{version}`) : une version ne se
  réécrit pas, la plus haute fait foi ;
- QUI PAIE dans une org (`org`, le défaut, ou `person`) ;
- les VERSEMENTS (`POST …/movements`), idempotents par `(payeur, clé)` : la même clé avec
  un autre contenu est un 409 `idempotency_conflict` ;
- le SOLDE d'un payeur ;
- le CURSEUR des mouvements (`GET …/movements?after=`), dans l'ordre où leurs
  transactions sont devenues visibles : relu jusqu'à la fin, il n'en manque aucun ; et
  sur chaque page `as_of`, l'horizon sous lequel aucun mouvement ne peut plus
  apparaître (une période qui s'achève avant est complète une fois le flux lu), et
  `settled_as_of`, celui sous lequel tout appel est réglé ou libéré.

Un payeur est `org:<id>` ou `user:<sub>`, et doit exister. Mêmes règles que
`service_commerce` : REST seules (`mcp=None`), gardées par `COMMERCE_SERVICE`.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from ..db import credits as db_credits
from ._authz import COMMERCE_SERVICE
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding, cap_limit
from .registry import CAPABILITIES
from .service_commerce import ServiceNoInput, _compte_ou_404, _iso, _org_ou_404

_ID = {"id": "org_id"}
_PAYEUR = re.compile(r"^(org:(?P<org>[0-9]+)|user:(?P<sub>.+))$")

_ORG_INCONNUE = DeclaredError(404, "unknown_org", "l'org n'existe pas ou est archivée")
_COMPTE_INCONNU = DeclaredError(404, "unknown_user", "aucun compte ne porte ce sub")
_PAYEUR_ILLISIBLE = DeclaredError(400, "invalid_payer",
                                  "le payeur n'est ni `org:<id>` ni `user:<sub>`")
_ERREURS_PAYEUR = (_PAYEUR_ILLISIBLE, _ORG_INCONNUE, _COMPTE_INCONNU)
_CONFLIT = DeclaredError(409, "idempotency_conflict",
                         "la clé a déjà servi, pour ce payeur, avec un autre contenu")
_CURSEUR_ILLISIBLE = DeclaredError(400, "invalid_cursor",
                                   "`after` n'est pas un curseur rendu par cette route")


def _payeur_existant(payer: str) -> str:
    m = _PAYEUR.match(payer)
    if not m:
        raise AuthzDenied(400, "invalid_payer",
                          f"Payeur {payer!r} illisible : `org:<id>` ou `user:<sub>`.")
    if m["org"] is not None:
        _org_ou_404(int(m["org"]))
    else:
        _compte_ou_404(m["sub"])
    return payer


# ── le barème ────────────────────────────────────────────────────────────────

class CreditScaleLine(BaseModel):
    connector: str = Field(min_length=1)
    function: str = Field(min_length=1)
    unit: str = Field(min_length=1, description="The unit of the function's declared "
                                                "cost (`requests`, `pages`, `credits`…).")
    budget: int = Field(ge=0, description="Budget units per `per_n` units of the function.")
    per_n: int = Field(1, ge=1, description="Package size; reservation and settlement "
                                            "round up to whole packages.")


class CreditScalePutInput(BaseModel):
    version: int = Field(ge=1)
    lines: list[CreditScaleLine]

    @field_validator("lines")
    @classmethod
    def _uniques(cls, v):
        cles = [(l.connector, l.function, l.unit) for l in v]
        if len(set(cles)) != len(cles):
            raise ValueError("une ligne par (connector, function, unit)")
        return v


class CreditScale(BaseModel):
    """`version` = la version qui fait foi (`null` sans barème) ; `created` = cette
    pose l'a créée (False : la même version portait déjà exactement ces lignes)."""
    version: Optional[int] = None
    created: Optional[bool] = None
    lines: list[CreditScaleLine]


def _scale_put(ctx: ResolvedCtx, inp: CreditScalePutInput) -> dict:
    try:
        neuve = db_credits.poser_bareme(inp.version, [l.model_dump() for l in inp.lines])
    except db_credits.BaremeEnConflit as e:
        raise AuthzDenied(409, "scale_version_conflict", str(e))
    return {"version": inp.version, "created": neuve,
            "lines": [l.model_dump() for l in inp.lines]}


def _scale_current(ctx: ResolvedCtx, inp: ServiceNoInput) -> dict:
    version = db_credits.version_du_bareme()
    return {"version": version,
            "lines": db_credits.lignes_du_bareme(version) if version is not None else []}


# ── qui paie ─────────────────────────────────────────────────────────────────

class CreditPayerModeInput(BaseModel):
    org_id: int
    mode: Literal["org", "person"]


class CreditPayerMode(BaseModel):
    org_id: int
    mode: Literal["org", "person"]


def _payer_mode(ctx: ResolvedCtx, inp: CreditPayerModeInput) -> dict:
    _org_ou_404(inp.org_id)
    db_credits.poser_mode(inp.org_id, inp.mode)
    return {"org_id": inp.org_id, "mode": inp.mode}


# ── versements, solde, curseur ───────────────────────────────────────────────

class CreditMovementInput(BaseModel):
    payer: str
    amount: int = Field(description="Signed budget units added to the balance.")
    reason: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


class CreditMovement(BaseModel):
    id: int
    cursor: str = Field(description="Opaque; pass it as `after` to read what follows.")
    payer: str
    kind: str = Field(description="deposit | settle | transfer")
    amount: int = Field(description="Effect on the balance.")
    held: int = Field(0, description="Effect on the open holds.")
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    call_uid: Optional[str] = None
    connector: Optional[str] = None
    function: Optional[str] = None
    quantity: Optional[int] = None
    meta: dict = Field(default_factory=dict, description="Settlement facts: `from`, "
                                                         "`exceeded`, `hold_expired`…")
    created_at: Optional[str] = None


class CreditMovementPosted(BaseModel):
    """`created` = False quand la clé avait déjà servi avec le même contenu : c'est le
    mouvement d'origine qui est rendu, rien n'a bougé."""
    created: bool
    movement: CreditMovement


class CreditBalanceInput(BaseModel):
    payer: str


class CreditBalance(BaseModel):
    """`available` = `balance` − `holds_open`."""
    payer: str
    balance: int
    holds_open: int
    available: int
    as_of: str


class CreditMovementsInput(BaseModel):
    after: Optional[str] = None
    limit: int = 500
    payer: Optional[str] = Field(
        None, description="`org:<id>` or `user:<sub>`: `settled_as_of` then counts only "
                          "this payer's reservations, so another payer's pending "
                          "asynchronous call does not hold it. The page and `as_of` stay "
                          "those of the whole feed. Omitted: every payer.")

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        return cap_limit(v, 1000)


class CreditMovements(BaseModel):
    """Les mouvements d'après `after`, dans l'ordre où leurs transactions sont devenues
    visibles. `next_cursor` = le curseur à repasser ; égal à `after` quand rien de neuf
    n'est encore visible."""
    movements: list[CreditMovement]
    next_cursor: Optional[str] = None
    as_of: Optional[str] = Field(
        description="ISO 8601 instant, on every page (empty ones included). Every "
                    "movement served after this read, beyond the movements already "
                    "visible, has `created_at` >= `as_of`: once a page shorter than "
                    "`limit` has been read (the end of the feed), no movement dated "
                    "before `as_of` can still appear, so a period ending at or before "
                    "`as_of` is complete. Held at the start of the oldest open "
                    "transaction that can still write a movement, or at the oldest "
                    "committed movement the cursor still holds back, otherwise close "
                    "to the read time; it never goes backwards. `null` when the core "
                    "cannot see every open transaction of its database (its database "
                    "role lacks `pg_read_all_stats`, or a prepared transaction is "
                    "pending): completeness cannot be judged on that read.")
    settled_as_of: Optional[str] = Field(
        description="ISO 8601 instant, on every page, read in the same snapshot as "
                    "`as_of`: the earliest of `as_of` and the start (`created_at`) of "
                    "the oldest reservation (every payer, or only `payer` when given), "
                    "still OPEN or "
                    "settled by a movement the cursor still holds back. Every call that "
                    "reserved before it is settled, released or expired, and its "
                    "movement, if any, is served by the feed (on this page or the next "
                    "ones). A reservation waiting for an asynchronous "
                    "result or for its deadline holds it at its start, without the "
                    "reader knowing the deadline. To judge the calls of a period "
                    "settled, compare its end with `settled_as_of`, not `as_of`. It "
                    "never goes backwards. `null` whenever `as_of` is.")


def _curseur(m: dict) -> str:
    return f"{m['xid']}.{m['id']}"


def _lire_curseur(after: Optional[str]) -> tuple[int, int]:
    if after is None:
        return 0, 0
    xid, point, id_ = after.partition(".")
    if not point or not xid.isdigit() or not id_.isdigit():
        raise AuthzDenied(400, "invalid_cursor", f"Curseur {after!r} illisible.")
    return int(xid), int(id_)


def _mouvement(m: dict) -> dict:
    return {**m, "cursor": _curseur(m), "created_at": _iso(m.get("created_at")),
            "meta": m.get("meta") or {}}


def _movement_post(ctx: ResolvedCtx, inp: CreditMovementInput) -> dict:
    _payeur_existant(inp.payer)
    try:
        m, neuf = db_credits.verser(inp.payer, inp.amount, inp.reason, inp.idempotency_key)
    except db_credits.CleEnConflit as e:
        raise AuthzDenied(409, "idempotency_conflict", str(e))
    return {"created": neuf, "movement": _mouvement(m)}


def _balance(ctx: ResolvedCtx, inp: CreditBalanceInput) -> dict:
    _payeur_existant(inp.payer)
    s = db_credits.solde(inp.payer)
    return {"payer": inp.payer, "balance": s["balance"], "holds_open": s["holds_open"],
            "available": s["balance"] - s["holds_open"], "as_of": _iso(s["as_of"])}


def _movements(ctx: ResolvedCtx, inp: CreditMovementsInput) -> dict:
    xid, id_ = _lire_curseur(inp.after)
    if inp.payer is not None:
        _payeur_existant(inp.payer)
    lu = db_credits.page_de_mouvements(xid, id_, inp.limit, payeur=inp.payer)
    page = [_mouvement(m) for m in lu["mouvements"]]
    return {"movements": page,
            "next_cursor": page[-1]["cursor"] if page else inp.after,
            "as_of": _iso(lu["as_of"]), "settled_as_of": _iso(lu["settled_as_of"])}


CAPABILITIES += [
    Capability(key="service.credits.scale.put", handler=_scale_put,
               Input=CreditScalePutInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=CreditScale,
               errors=(DeclaredError(409, "scale_version_conflict",
                                     "la version est déjà posée, avec d'autres lignes"),),
               description="[service commerce] Set one version of the credit scale, all "
                           "lines at once (budget units per `per_n` units of a declared "
                           "function cost). A version is never rewritten; the highest "
                           "one applies. Re-posting the same lines is a no-op.",
               rest=RestBinding("PUT", "/api/service/credits/scales/{version}")),
    Capability(key="service.credits.scale.current", handler=_scale_current,
               Input=ServiceNoInput, authz=COMMERCE_SERVICE, mcp=None, Output=CreditScale,
               description="[service commerce] The credit scale version that applies, "
                           "with its lines (`version` null without any scale).",
               rest=RestBinding("GET", "/api/service/credits/scales/current")),
    Capability(key="service.org.credits.payer_mode", handler=_payer_mode,
               Input=CreditPayerModeInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=CreditPayerMode, errors=(_ORG_INCONNUE,),
               description="[service commerce] Who pays for calls made in an org: the "
                           "org (`org`, default) or each person (`person`).",
               rest=RestBinding("PUT", "/api/service/orgs/{id}/credits/payer-mode", _ID)),
    Capability(key="service.credits.movement.post", handler=_movement_post,
               Input=CreditMovementInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=CreditMovementPosted, errors=_ERREURS_PAYEUR + (_CONFLIT,),
               description="[service commerce] Add a signed amount of budget units to a "
                           "payer (`org:<id>` or `user:<sub>`), once per idempotency key "
                           "and payer; the same key with another content is a 409.",
               rest=RestBinding("POST", "/api/service/credits/movements")),
    Capability(key="service.credits.balance", handler=_balance, Input=CreditBalanceInput,
               authz=COMMERCE_SERVICE, mcp=None, Output=CreditBalance,
               errors=_ERREURS_PAYEUR,
               description="[service commerce] The balance of a payer: units credited "
                           "minus settled, units held by calls in flight, and what is "
                           "available.",
               rest=RestBinding("GET", "/api/service/credits/balance")),
    Capability(key="service.credits.movements", handler=_movements,
               Input=CreditMovementsInput, authz=COMMERCE_SERVICE, mcp=None,
               Output=CreditMovements, errors=(_CURSEUR_ILLISIBLE, *_ERREURS_PAYEUR),
               description="[service commerce] Movements of every payer after the opaque "
                           "cursor `after`, in the order their transactions became "
                           "visible (default 500, at most 1000). Read up to the end, it "
                           "misses none; `as_of` tells up to when a period is "
                           "complete, `settled_as_of` up to when its calls are settled "
                           "(those of `payer` only, when given).",
               rest=RestBinding("GET", "/api/service/credits/movements")),
]
