"""Capacité « tester la connexion d'un connecteur » (framework de sondes, ADR 0009).

Résout le credential (cascade effective, ou clé d'org explicite) et exécute la sonde
enregistrée du connecteur (`connector_verify`). L'échec d'authentification EST le
résultat (`{ok:false, error}`), jamais un 500 — même esprit que le banc de test d'outil
(`my_tool_call`). Le message provider est déjà nettoyé par la sonde ; ici on extrait
juste celui d'une `McpError` (ex. data center Zoho manquant).
"""
from __future__ import annotations

import inspect
import time
from typing import Literal

from mcp.shared.exceptions import McpError
from pydantic import BaseModel

from .. import access, connector_verify, credentials_store
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding


class VerifyInput(BaseModel):
    provider: str                              # path {provider}
    level: Literal["auto", "org"] = "auto"     # auto = credential effectif ; org = clé de l'org


def _fields_config_scope(ctx: ResolvedCtx, inp: VerifyInput) -> tuple[dict, dict, "tuple | None"]:
    """(champs déchiffrés, config non-secrète, SCOPE-santé) à sonder selon le niveau.

    `config` = satellites NON-secrets appariés à la clé (meta public : dsn
    unipile…) — une sonde vers un endpoint dont l'hôte dépend de la clé DOIT en tenir compte.
    `scope-santé` = où persister le résultat (flag `meta.health_ko`, lu par `status_for`) :
    `('org', org_id)` en niveau org, `('member', member_id)` quand la clé EFFECTIVE est
    bien celle du membre (sinon None → on ne flague pas une clé partagée pour un seul user).

    - `auto` (carte user) : le credential EFFECTIF (cascade user > équipe > org >
      plateforme). `emit_on_failure=False` : une sonde ne doit pas polluer le monitoring.
    - `org` (carte org) : la clé DE L'ORG active/consultée spécifiquement (une clé perso
      la masquerait dans la cascade). `ctx.org_id` est injecté par l'authz (IDOR-safe)."""
    if inp.level == "org":
        row = credentials_store.get_credential_with_meta("org", str(ctx.org_id), inp.provider)
        if not row:
            raise AuthzDenied(400, "no_org_credential",
                              "aucune clé d'org posée pour ce connecteur.")
        return (credentials_store.unpack_secret(inp.provider, row["secret"]),
                credentials_store.public_meta(row.get("meta")),
                ("org", str(ctx.org_id)))
    rc = access.resolve_credential(
        inp.provider, want="auto", sub=ctx.sub, emit_on_failure=False,
    )
    scope = (("member", credentials_store.member_id(ctx.org_id, ctx.sub))
             if getattr(rc, "mode", None) == "user" and ctx.org_id is not None else None)
    return rc.fields, rc.config, scope


def _record_health(provider: str, scope: "tuple | None", ok: bool, error: "str | None") -> None:
    """Persiste l'état de santé du credential testé (flag `meta.health_ko` + raison) —
    lu par `status_for`, rendu terra au verdict (« connexion KO »). Merge (n'écrase rien),
    best-effort. `scope=None` (clé partagée sous un user) → on ne flague pas."""
    if scope is None:
        return
    try:
        credentials_store.update_meta(
            scope[0], scope[1], provider, "",
            {"health_ko": (not ok), "health_reason": (error if not ok else None)})
    except Exception:  # noqa: BLE001 — la santé est un bonus, jamais bloquant
        pass


async def _verify(ctx: ResolvedCtx, inp: VerifyInput) -> dict:
    probe = connector_verify.probe_for(inp.provider)
    if probe is None:
        raise AuthzDenied(400, "verify_unavailable",
                          f"pas de test de connexion pour « {inp.provider} ».")
    fields, config, scope = _fields_config_scope(ctx, inp)
    started = time.monotonic()
    ok, error = True, None
    try:
        res = probe(fields, config)
        if inspect.isawaitable(res):
            await res
    except Exception as e:  # noqa: BLE001 — l'erreur d'auth EST le résultat
        ok = False
        error = e.error.message if isinstance(e, McpError) else str(e)
    # La sonde EST le « health check » (read facile) → son verdict alimente le flag santé.
    _record_health(inp.provider, scope, ok, error)
    out = {"ok": ok, "provider": inp.provider,
           "elapsed_ms": int((time.monotonic() - started) * 1000)}
    if not ok:
        out["error"] = error
    return out


CAP_DOC = (
    "Test whether a connector's configured credential actually authenticates "
    "(side-effect-free probe), returning {ok, error}. Use it to diagnose a connector "
    "that is set but not working (wrong region, expired token…) before reporting a gap. "
    "'auto' tests the credential that resolves for you; 'org' tests the org shared key."
)

from .registry import CAPABILITIES  # noqa: E402

CAPABILITIES += [
    Capability(
        key="connectors.verify", handler=_verify, Input=VerifyInput, authz=ORG_MEMBER,
        description=CAP_DOC,
        rest=RestBinding("POST", "/api/me/connectors/{provider}/verify"),
    ),
]
