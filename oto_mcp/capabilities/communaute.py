"""La COMMUNAUTÉ : ce qui est commun à tout ce qui s'y publie.

La bibliothèque publique de guides (`guide_library.py`) en est le premier genre
(`kind='procedure'`). Ce module porte ce qui ne dépend pas du genre :

- **le contrôle automatique** de ce qui part (`refuser_si_contenu`, sur
  `contenu_publiable`) : 400 `contenu_refuse`, qui dit CE QUI a été trouvé et OÙ,
  jamais la valeur ;
- **la modération après coup** : une entrée est listée dès sa publication ; le
  personnel de la plateforme la RETIRE (motif obligatoire) ou la RESTAURE
  (`library.moderate`, opérateur plateforme). Une entrée retirée disparaît de la liste,
  de la lecture et du fork pour tous — sauf pour les admins de l'org autrice et les
  opérateurs plateforme (`peut_voir_retiree`), qui la voient avec son motif ;
- **les compteurs servis** (`installs_total`, `installs_30d` : orgs DISTINCTES) et les
  champs de modération, décrits une fois (`ChampsCommunaute`) ;
- **la vitrine anonyme** (`vitrine`) : la liste FERMÉE des champs qu'une surface sans
  login peut rendre — jamais le corps, jamais un identifiant d'org ou de compte.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import access, contenu_publiable, org_store, roles
from ..db import communaute as store
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

ModerationState = Literal["listed", "removed"]


def refuser_si_contenu(champs: dict[str, str]) -> None:
    """Le contrôle automatique, AVANT toute écriture. Lève 400 `contenu_refuse`."""
    constats = contenu_publiable.inspecter(champs)
    if constats:
        raise AuthzDenied(400, "contenu_refuse",
                          contenu_publiable.message_de_refus(constats),
                          {"found": constats})


def peut_voir_retiree(sub: str, entry: dict) -> bool:
    """Une entrée retirée reste visible à l'org qui l'a publiée (ses admins, pour
    savoir pourquoi) et au personnel de la plateforme. Personne d'autre."""
    if access.is_platform_operator(sub):
        return True
    org = entry.get("author_org_id")
    return (entry.get("author_kind") == "org" and org is not None
            and roles.is_org_admin(sub, int(org)))


def refuser_si_retiree(sub: str, entry: dict) -> None:
    """Une entrée RETIRÉE ne se dépublie que par le personnel de la plateforme : son
    auteur la dépublierait puis la republierait sous le même slug — une entrée neuve,
    sans retrait. 403 `entree_retiree`."""
    if entry.get("moderation_state") == store.REMOVED and not access.is_platform_operator(sub):
        raise AuthzDenied(403, "entree_retiree",
                          "Cette entrée a été retirée par l'équipe de la plateforme : elle ne "
                          "se dépublie pas de ton côté. Écris-nous pour la faire revoir.")


def projeter(sub: str, entry: dict, *, privilegie: Optional[bool] = None) -> dict:
    """Range les champs de modération d'une ligne lue : `moderation` (motif, auteur,
    date du retrait) n'est rendu qu'à qui peut voir une entrée retirée ; les autres ne
    reçoivent que `moderation_state` (qui vaut alors toujours 'listed')."""
    d = dict(entry)
    raison = d.pop("moderation_reason", "")
    par, quand = d.pop("removed_by", None), d.pop("removed_at", None)
    if d.get("moderation_state") != store.REMOVED:
        return d
    if privilegie is None:
        privilegie = peut_voir_retiree(sub, d)
    if privilegie:
        d["moderation"] = {"state": store.REMOVED, "reason": raison,
                           "removed_by": par, "removed_at": quand}
    return d


class ChampsCommunaute(BaseModel):
    """Champs communs à toute entrée de la communauté (liste et lecture)."""
    installs_total: int = Field(
        default=0, description="Nombre d'orgs DISTINCTES qui ont installé (forké) "
                               "l'entrée, depuis toujours. Réinstaller dans la même org "
                               "ne compte pas deux fois.")
    installs_30d: int = Field(
        default=0, description="Nombre d'orgs distinctes qui l'ont installée dans les 30 "
                               "derniers jours — la clé du tri `trending`.")
    moderation_state: ModerationState = Field(
        default="listed",
        description="'listed' (visible de tous) | 'removed' (retirée par le personnel de "
                    "la plateforme). Une entrée retirée n'est servie qu'aux admins de "
                    "l'org autrice et aux opérateurs plateforme ; pour tout autre "
                    "appelant la valeur est donc toujours 'listed'.")
    moderation: Optional[dict] = Field(
        default=None,
        description="Présent seulement sur une entrée RETIRÉE, et seulement pour qui peut "
                    "la voir : `{state, reason, removed_by, removed_at}`.")


# ── la vitrine anonyme ─────────────────────────────────────────────────────────

#: Les SEULS champs qu'une surface sans login rend d'une entrée. Liste fermée : un
#: champ ajouté à la ligne lue n'en sort pas tant qu'il n'est pas nommé ici.
CHAMPS_VITRINE = ("slug", "title", "description", "author_kind", "author_display",
                  "category", "tags", "installs_total", "installs_30d", "created_at",
                  "updated_at")


def _date(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def vitrine(entry: dict) -> dict:
    """La vitrine d'une entrée, pour une surface ANONYME. Jamais `body_md`, jamais un
    identifiant d'org ou de compte (`author_org_id`, `source_org_id`, `published_by`).
    `slots` = ce qu'il faudra brancher après l'installation, réduit à son nom, son type
    et son connecteur (ni description libre, ni schéma)."""
    out = {k: _date(entry.get(k)) for k in CHAMPS_VITRINE}
    out["tags"] = list(out.get("tags") or [])
    out["slots"] = [
        {k: s[k] for k in ("name", "type", "connector") if s.get(k) is not None}
        for s in (entry.get("slots") or []) if isinstance(s, dict) and s.get("name")]
    return out


# ── library.moderate ────────────────────────────────────────────────────────────

class ModerateInput(BaseModel):
    id: int                               # l'entrée de bibliothèque (≠ slug)
    action: Literal["remove", "restore"]
    reason: str                           # obligatoire : il est montré à l'org autrice

    @field_validator("reason")
    @classmethod
    def _raison(cls, v):
        v = (v or "").strip()
        if len(v) < 3:
            raise ValueError("`reason` requis (3 caractères au moins) : il est montré "
                             "aux admins de l'org autrice")
        return v[:500]


class ModerationResult(BaseModel):
    """L'état de modération d'une entrée après le geste."""
    id: int = Field(description="Identifiant de l'entrée.")
    slug: str = Field(description="Slug public de l'entrée.")
    state: ModerationState = Field(description="'removed' après un retrait, 'listed' "
                                               "après une restauration.")
    reason: str = Field(description="Motif du dernier geste (retrait ou restauration).")
    removed_by: Optional[str] = Field(default=None, description="Compte (sub) du dernier "
                                                                "retrait.")
    removed_at: Optional[str] = Field(default=None, description="Date du dernier retrait.")
    restored_by: Optional[str] = Field(default=None, description="Compte (sub) de la "
                                                                 "dernière restauration.")
    restored_at: Optional[str] = Field(default=None, description="Date de la dernière "
                                                                 "restauration.")


def _moderate(ctx: ResolvedCtx, inp: ModerateInput) -> dict:
    entry = org_store.get_library_entry(entry_id=inp.id, include_unlisted=True,
                                        include_removed=True)
    if not entry:
        raise AuthzDenied(404, "unknown_entry", "Entrée inconnue.")
    geste = store.retirer if inp.action == "remove" else store.restaurer
    etat = geste(org_store.KIND, entry["id"], by=ctx.sub, reason=inp.reason)
    return {"id": entry["id"], "slug": entry["slug"],
            **{k: _date(v) for k, v in etat.items()}}


CAPABILITIES += [
    Capability(
        key="library.moderate", handler=_moderate, Input=ModerateInput,
        authz=PLATFORM_ADMIN, mcp=None,
        description="Platform staff only: remove a community (public library) entry, or "
                    "restore one, with a reason shown to the author org's admins. A "
                    "removed entry disappears from list/get/fork for everyone except the "
                    "author org's admins and platform operators, and from the anonymous "
                    "community pages. Re-publishing does not lift a removal.",
        Output=ModerationResult,
        rest=RestBinding("POST", "/api/admin/guide-library/{id}/moderation"),
    ),
]
