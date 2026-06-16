"""Types de la couche capacité (ADR 0009).

Une `Capability` co-déclare, au même endroit que son handler : la clé stable,
le handler core, le modèle d'entrée pydantic (seule source de validation), une
règle d'autz **obligatoire**, et les bindings de surface (MCP / REST). Les
adaptateurs bouclent sur le registre et appliquent autz → validation → handler.

Aucun import d'adaptateur ni de transport ici (sens unique ADR 0004). Le refus
d'autz est un `AuthzDenied` **neutre** ; chaque adaptateur le traduit dans son
transport (McpError côté MCP, json_error+CORS côté REST).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Callable, Optional

from pydantic import BaseModel


@dataclass
class RawCtx:
    """Identité brute résolue par l'adaptateur (deux chemins d'auth distincts :
    ContextVar de token côté MCP, `authenticate(request)` côté REST)."""
    sub: Optional[str]


@dataclass
class ResolvedCtx:
    """Contexte enrichi produit par la règle d'autz, passé au handler.
    `org_id`/`group_id` sont injectés par la règle (jamais acceptés d'un param
    client → verrou IDOR par construction)."""
    sub: str
    org_id: Optional[int] = None
    role: Optional[str] = None
    group_id: Optional[int] = None


class AuthzDenied(Exception):
    """Refus d'autz **neutre au transport**. `status` = code HTTP de référence
    (401/403/404/400) ; `code` = jeton machine stable ; `message` = détail."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message


# Une règle d'autz : (identité brute, input validé) -> contexte résolu, ou lève AuthzDenied.
AuthzRule = Callable[[RawCtx, Optional[BaseModel]], ResolvedCtx]


@dataclass(frozen=True)
class RestBinding:
    verb: str                                   # GET | POST | PUT | PATCH | DELETE
    path: str                                   # ex "/api/me/active-org"
    # placeholder de route -> champ Input, quand ils diffèrent (routes réelles en {id}).
    path_map: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    key: str                                    # clé stable, ≠ nom de surface (ex "org.use_org")
    handler: Callable                           # (ResolvedCtx, Input) -> dict (logique core)
    Input: type[BaseModel]                       # seule source de validation
    authz: AuthzRule                            # OBLIGATOIRE (pas de défaut → oubli = TypeError)
    description: str = ""                       # contrat LLM du tool MCP
    mcp: Optional[str] = None                   # nom du tool MCP, ou None (opt-out explicite)
    # un OU plusieurs bindings REST (ex. routes self-service + admin sur le même
    # métier+autz), ou None (opt-out explicite).
    rest: "Optional[RestBinding | tuple[RestBinding, ...]]" = None

    def __post_init__(self):
        if self.mcp is None and not self.rest:
            raise ValueError(
                f"Capability {self.key!r} sans surface : déclarer mcp= et/ou rest= "
                f"(un opt-out doit être explicite, pas un oubli)."
            )

    def rest_bindings(self) -> list[RestBinding]:
        if self.rest is None:
            return []
        if isinstance(self.rest, RestBinding):
            return [self.rest]
        return list(self.rest)


def apply_flat_signature(fn: Callable, model: type[BaseModel]) -> Callable:
    """Expose les champs de `model` en paramètres KEYWORD_ONLY plats sur `fn`.

    FastMCP (3.4.2) génère le schéma d'un tool depuis la signature : un unique
    param pydantic donnerait un schéma IMBRIQUÉ (`{"p": {"$ref": …}}`), cassant
    le contrat plat des tools existants. On injecte donc `__signature__` +
    `__annotations__` reconstruits depuis les champs du modèle → schéma plat.
    Validé empiriquement (ADR 0009 §6 ; test `test_with_signature_flat`).
    """
    params = []
    annotations: dict = {}
    for name, f in model.model_fields.items():
        default = inspect.Parameter.empty if f.is_required() else f.default
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                        annotation=f.annotation, default=default))
        annotations[name] = f.annotation
    fn.__signature__ = inspect.Signature(params)
    fn.__annotations__ = annotations
    return fn
