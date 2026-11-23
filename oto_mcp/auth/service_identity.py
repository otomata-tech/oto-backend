"""L'identité de SERVICE de la requête courante — un client machine de l'annuaire
(Logto, `client_credentials`), jamais un compte (oto-backend#1068, ADR 0070 §7.4(b)).

Un service consommateur de l'API (le commerce) parle au cœur sous son propre nom.
Il n'a ni ligne `users`, ni org, ni pause : le cœur ne possède pas l'annuaire, il
vérifie un jeton et garde un identifiant. Ce qu'il est, c'est l'authentification
REST qui le POSE après avoir vérifié le jeton, et la règle d'autorisation
(`_authz.SERVICE_ROLE`) qui le LIT — même patron que `platform_worker`.

**Comment un jeton est reconnu comme celui d'un service.** Son audience est la
ressource d'API des services de CETTE instance, `<OTO_MCP_PUBLIC_URL>/api/service`,
déclarée dans l'annuaire de l'instance. Aucune autre audience n'y mène, et
réciproquement le verifier des personnes (audience MCP) refuse ce jeton : un service
n'entre jamais par la face agent. Le jeton doit en plus être un jeton de MACHINE
(`sub == client_id`), et porter dans `scope` un rôle du catalogue (`ROLES`), accordé
par l'annuaire au client — un client sans rôle est refusé, pas rétrogradé.

**Refusé par défaut.** `_authenticate` ne l'accepte que sur une route qui le déclare
(`allow_service`), c'est-à-dire une capacité dont la règle le lit ; et toute règle
qui exige un compte le refuse (`_authz._require_sub`). Deux verrous : une route
écrite avant ce lot ne peut pas servir un service par oubli.
"""
from __future__ import annotations

import contextvars
import os
from functools import lru_cache
from typing import Optional

from fastmcp.server.auth.providers.jwt import JWTVerifier

from .. import tenancy
from ..config import public_base_url, require_env

# Le catalogue FERMÉ des rôles de service. Un rôle est une permission de la
# ressource des services, accordée au client machine par l'annuaire.
COMMERCE = "commerce"
# Le service des VISITES DE SITE : il identifie les entreprises qui visitent le site
# d'une org et les écrit dans le tableau que l'org lui a ouvert — rien d'autre
# (`capabilities/datastore/ecrivains_de_service.py`). Un rôle par service : la
# fuite du secret de l'un ne donne pas les gestes de l'autre.
VISITORS = "visitors"
ROLES: frozenset[str] = frozenset({COMMERCE, VISITORS})
# Les rôles qu'une org peut inscrire comme ÉCRIVAINS d'un de ses tableaux. Le
# commerce n'en est pas : il ne touche à aucune ligne.
ROLES_ECRIVAINS: frozenset[str] = frozenset({VISITORS})

# Préfixe du principal publié : le journal distingue un service d'un compte d'un coup
# d'œil. Le `client_id` qui suit reste OPAQUE — jamais relu pour décider.
SUB_PREFIX = "service:"

_CURRENT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "oto_service_identity", default=None)


class ServiceRefuse(Exception):
    """Un jeton adressé aux services, mais refusé — `code` et `status` nomment pourquoi."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def set_current(principal: Optional[dict]) -> None:
    _CURRENT.set(principal)


def current() -> Optional[dict]:
    return _CURRENT.get()


def audience() -> str:
    """La ressource d'API des services de cette instance — dérivée de son adresse
    publique, jamais un littéral à nous."""
    return f"{public_base_url()}/api/service"


def adresse_aux_services(token: str) -> bool:
    """Le jeton REVENDIQUE l'audience des services — lu SANS vérification, pour
    choisir le verifier ; `verifier()` revalide tout (signature, émetteur, audience)."""
    aud = tenancy.unverified_claims(token).get("aud")
    return audience() in (aud if isinstance(aud, list) else [aud])


@lru_cache(maxsize=4)
def _verifier_pour(issuer: str, aud: str) -> JWTVerifier:
    # Logto self-hosted signe en ES384 (même réglage que le verifier des personnes).
    return JWTVerifier(jwks_uri=f"{issuer}/jwks", issuer=issuer,
                       audience=aud, algorithm="ES384")


def emetteurs() -> tuple[str, ...]:
    """Les émetteurs dont un jeton de service est accepté : le PRIMAIRE de l'instance, et
    pendant la fenêtre de drain d'une bascule de domaine `LOGTO_ENDPOINT_ALT`, comme pour
    les personnes (`server._registry_and_issuers`) — sinon le service tombe entre la
    bascule de l'annuaire et celle du cœur. Un tenant tiers n'émet pas de jeton de
    service : son annuaire n'est pas le nôtre."""
    primaire = require_env("LOGTO_ENDPOINT").rstrip("/") + "/oidc"
    alt = os.environ.get("LOGTO_ENDPOINT_ALT", "").strip().rstrip("/")
    return (primaire, f"{alt}/oidc") if alt else (primaire,)


def verifier(token: str) -> Optional[JWTVerifier]:
    """Le verifier de l'émetteur que le jeton REVENDIQUE, s'il est l'un des nôtres
    (`emetteurs`), audience stricte ; None sinon. Choisi sur la revendication lue sans
    vérification : le verifier revalide tout (signature, émetteur, audience)."""
    iss = tenancy.unverified_claims(token).get("iss")
    return _verifier_pour(iss, audience()) if iss in emetteurs() else None


async def verifier_jeton(token: str) -> dict:
    """Vérifie un jeton adressé aux services et rend le principal, ou lève `ServiceRefuse`."""
    v = verifier(token)
    if v is None:
        raise ServiceRefuse(401, "invalid_token",
                            "Jeton de service d'un émetteur que cette instance ne reconnaît pas.")
    access = await v.verify_token(token)
    claims = getattr(access, "claims", None) or {}
    if not claims:
        raise ServiceRefuse(401, "invalid_token", "Jeton de service invalide ou expiré.")
    client_id, sub = claims.get("client_id"), claims.get("sub")
    if not client_id or sub != client_id:
        # Un jeton de PERSONNE émis pour la ressource des services : la ressource
        # n'ouvre que des clients machine.
        raise ServiceRefuse(403, "service_machine_required",
                            "La ressource des services n'accepte que le jeton d'un client "
                            "machine (client_credentials).")
    roles = ROLES & set(str(claims.get("scope") or "").split())
    if not roles:
        raise ServiceRefuse(403, "service_role_missing",
                            f"Ce client machine ne porte aucun rôle de service "
                            f"(attendu l'un de {sorted(ROLES)}), à lui accorder dans "
                            f"l'annuaire sur la ressource {audience()}.")
    return {"sub": SUB_PREFIX + client_id, "client_id": client_id,
            "roles": frozenset(roles)}
