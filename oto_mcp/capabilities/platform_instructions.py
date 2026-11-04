"""Édition du bloc d'instructions PLATEFORME (#50) — secret sauce (bloc A). Surface
admin plateforme (PLATFORM_ADMIN) : ce bloc est injecté à TOUS les comptes au handshake
et **inviolable par l'org**. (L'onboarding n'est plus un bloc : c'est un projet, ADR 0032 §7.)

Pattern ADR 0009 : capacités par-verbe (avec REST `/api/admin/platform-instructions`)
+ un outil MCP op-aware consolidé `oto_admin_platform_instructions` qui les réutilise.
Prose (pas un credential) → l'édition est permise aussi côté MCP."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import guide_store, instructions
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_KEYS = (instructions.KEY_SECRET_SAUCE,)


class _NoInput(BaseModel):
    pass


class KeyInput(BaseModel):
    key: str


class SetInput(BaseModel):
    key: str
    body_md: str


class PlatformInstrInput(BaseModel):
    op: Literal["list", "get", "set", "drift"]
    key: Optional[str] = None
    body_md: Optional[str] = None


class ProseEtat(BaseModel):
    """Une prose de plateforme, et l'écart entre ce qui est SERVI et ce que le code porte."""
    famille: Literal["bloc", "guide"]
    slug: str
    etat: Literal["seed", "aligné", "divergent", "hors_code", "vide"]
    servi_len: int          # longueur de ce que la base sert (0 = pas d'override)
    code_len: int           # longueur du seed porté par le code (0 = aucun)
    updated_at: Optional[str] = None


class DriftOutput(BaseModel):
    proses: list[ProseEtat]
    a_traiter: list[ProseEtat]      # les seuls sur lesquels il y a un geste à faire
    resume: dict[str, int]
    lecture: str


def _require_key(key: Optional[str]) -> str:
    if key not in _KEYS:
        raise AuthzDenied(400, "unknown_key",
                          f"`key` doit être l'un de {', '.join(_KEYS)}.")
    return key


def _view(key: str) -> dict:
    """L'état effectif d'un bloc : la ligne `guides` (delivery='init'), ou le seed
    (is_seed=True, corps = constante) si jamais éditée. `default_md` accompagne toujours
    (bouton « rétablir le défaut »). `updated_by` retiré (guides ne le porte pas).

    ⚠️ **« Édité » se juge sur le CORPS, pas sur `updated_at`** — la même règle que ce que
    le handshake SERT (`instructions._platform_block` : « override DB s'il existe *et non
    vide*, sinon le seed »). Les deux divergeaient : vider un bloc pose une ligne datée à
    corps vide, donc l'agent recevait le seed pendant que cette vue annonçait « édité »
    avec un corps vide. Un écran qui décrit autre chose que ce qui est servi est pire
    qu'un écran absent — on le lit avec confiance. Et c'est ce qui rend le geste
    « rétablir le défaut » lisible : vider l'override EST le retour au seed."""
    st = guide_store.get_init_guide("platform", key)
    default_md = instructions.default_block(key)
    if (st["body_md"] or "").strip():
        return {"key": key, "body_md": st["body_md"], "updated_at": st["updated_at"],
                "updated_by": None, "is_seed": False, "default_md": default_md}
    return {"key": key, "body_md": default_md, "updated_at": None,
            "updated_by": None, "is_seed": True, "default_md": default_md}


def _list(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return {"blocks": [_view(k) for k in _KEYS], "keys": list(_KEYS)}


def _get(ctx: ResolvedCtx, inp: KeyInput) -> dict:
    return _view(_require_key(inp.key))


def _etat(servi: Optional[str], seed: Optional[str]) -> str:
    """Ce que la base SERT, face à ce que le code PORTE.

    `divergent` est le seul état dangereux, et c'est le silencieux : la base gagne, donc
    le code a beau évoluer, personne ne le reçoit — et rien ne le signale."""
    servi = (servi or "").strip()
    seed = (seed or "").strip()
    if not servi:
        return "seed" if seed else "vide"
    if not seed:
        return "hors_code"          # né en base : un environnement NEUF naîtra sans lui
    return "aligné" if servi == seed else "divergent"


def _drift(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    """Toute la prose de plateforme : ce que la base sert vs ce que le code porte.

    ⚠️ **Un override qui recopie le seed est une mine.** Il fige la prose au jour où il a
    été écrit ; toute évolution du code cesse alors de se propager, SANS que rien ne le
    signale. Vécu le 14/08 sur le bloc A : `abandoned` est resté deux jours dans le texte
    le plus lu de la plateforme après son retrait du code (#311), et `run_finish` le
    refusait — une consigne que le serveur ne savait pas honorer, servie à chaque session
    de chaque compte. Trouvé par comparaison manuelle ; d'où cette sonde, pour que la
    prochaine se voie sans qu'on la cherche.

    Deux familles, même trappe : le bloc A (`guides` delivery='init') et les guides
    plateforme (semés des fichiers `guides/*.md`, JAMAIS réécrits ensuite)."""
    from .. import db

    lignes = []
    for key in _KEYS:
        st = guide_store.get_init_guide("platform", key)
        seed = instructions.default_block(key)
        lignes.append({"famille": "bloc", "slug": key,
                       "etat": _etat(st["body_md"], seed),
                       "servi_len": len((st["body_md"] or "").strip()),
                       "code_len": len((seed or "").strip()),
                       "updated_at": st["updated_at"]})

    fichiers = {g["slug"]: g for g in guide_store.list_file_guides()}
    en_db = {g["slug"]: g for g in db.list_guides_db("platform", guide_store.PLATFORM_OWNER)}
    for slug in sorted(set(fichiers) | set(en_db)):
        servi = (en_db.get(slug) or {}).get("body_md")
        seed = (fichiers.get(slug) or {}).get("body_md")
        lignes.append({"famille": "guide", "slug": slug, "etat": _etat(servi, seed),
                       "servi_len": len((servi or "").strip()),
                       "code_len": len((seed or "").strip()),
                       "updated_at": (en_db.get(slug) or {}).get("updated_at")})

    a_traiter = [ligne for ligne in lignes if ligne["etat"] in ("divergent", "hors_code")]
    return {
        "proses": lignes,
        "a_traiter": a_traiter,
        "resume": {e: sum(1 for ligne in lignes if ligne["etat"] == e)
                   for e in ("seed", "aligné", "divergent", "hors_code", "vide")},
        "lecture": (
            "`divergent` = la base sert autre chose que le code, et le code n'atteint "
            "plus personne — comparer, puis vider l'override (le service retombe sur le "
            "code) ou reporter la différence dans le fichier. "
            "`hors_code` = né en base : un environnement NEUF naîtra sans cette prose. "
            "`seed`/`aligné` = rien à faire."),
    }


def _set(ctx: ResolvedCtx, inp: SetInput) -> dict:
    key = _require_key(inp.key)
    # ⚠️ Un bloc plateforme est un readme `delivery='init'` de scope `platform` : il
    # partage sa clé dérivée avec une couche À CHARGER du même slug (09/09/2026).
    # Aucune ne porte `secret_sauce` aujourd'hui, mais un refus prévu qui sort en
    # 500 n'apprend rien à qui le rencontre — et c'est un platform_admin.
    try:
        guide_store.set_init_guide("platform", key, inp.body_md or "",
                                   edited_by=ctx.sub)
    except guide_store.GuideDeliveryConflict as e:
        raise AuthzDenied(409, "delivery_conflict", str(e))
    return _view(key)


def _platform_instructions(ctx: ResolvedCtx, inp: PlatformInstrInput) -> dict:
    if inp.op == "list":
        return _list(ctx, _NoInput())
    if inp.op == "get":
        return _get(ctx, KeyInput(key=inp.key or ""))
    if inp.op == "drift":
        return _drift(ctx, _NoInput())
    if inp.body_md is None:
        raise AuthzDenied(400, "missing_body", "`body_md` requis pour set.")
    return _set(ctx, SetInput(key=inp.key or "", body_md=inp.body_md))


CAPABILITIES += [
    # MCP op-aware consolidé.
    Capability(
        key="platform.instructions", handler=_platform_instructions,
        Input=PlatformInstrInput, authz=PLATFORM_ADMIN,
        description=(
            "Platform-level injected instruction block (#50), shown to EVERY account at "
            "handshake, editable only by platform admins, immutable by orgs. op=list / "
            "get (`key`) / set (`key`, `body_md`) / **drift** (no arg: for EVERY platform "
            "prose — block A and the platform guides — what the DB SERVES vs what the "
            "CODE carries; `divergent` means the DB overrides the code and code changes "
            "reach nobody, silently). key = 'secret_sauce' (block A: posture "
            "+ usage loop + derived namespace catalog, always injected)."),
        mcp="oto_admin_platform_instructions",
    ),
    # Faces REST par-verbe (dashboard éditeur).
    Capability(
        key="platform.instructions.list", handler=_list, Input=_NoInput,
        authz=PLATFORM_ADMIN,
        description="List platform instruction blocks (A/B) with their effective content.",
        rest=RestBinding("GET", "/api/admin/platform-instructions"),
    ),
    Capability(
        key="platform.instructions.get", handler=_get, Input=KeyInput,
        authz=PLATFORM_ADMIN,
        description="Get one platform instruction block by `key`.",
        rest=RestBinding("GET", "/api/admin/platform-instructions/{key}"),
    ),
    Capability(
        key="platform.instructions.drift", handler=_drift, Input=_NoInput,
        Output=DriftOutput, authz=PLATFORM_ADMIN,
        description=("Where the DB overrides the code for platform prose (block A + "
                     "platform guides) — and where the two have silently diverged."),
        rest=RestBinding("GET", "/api/admin/platform-instructions/drift"),
    ),
    Capability(
        key="platform.instructions.set", handler=_set, Input=SetInput,
        authz=PLATFORM_ADMIN,
        description="Edit one platform instruction block (`key`, `body_md`).",
        rest=RestBinding("PUT", "/api/admin/platform-instructions/{key}"),
    ),
]
