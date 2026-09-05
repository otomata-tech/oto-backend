"""Capacités « partager un tableau » : lister, accorder, retirer un accès nominatif (#302).

Trois verbes sur un même chemin (`…/namespaces/{ns}/share`), qui vivaient en routes
écrites à la main. Le dashboard passe aujourd'hui par la surface générique
`oto_resource` (ADR 0048), mais ces chemins restent le contrat du client HTTP
d'`oto-core` (`DatastoreClient.share`/`unshare`) : ils ne bougent pas, ils gagnent
seulement un schéma d'entrée et de sortie.

`DELETE` avec un corps `{email}` est une forme historique — `RestBinding.reads_body`
la déclare explicitement plutôt que de la deviner (cf. `_types.py`). Sans ce cran, le
corps du retrait aurait été ignoré et chaque appel serait devenu `email_required` : le
genre de régression qu'une migration « invisible » produit sans bruit.

Autz `SUB_ONLY` au seuil, puis la VRAIE garde dans le handler : `govern_ns`, c'est-à-
dire `ownership.can_govern` (propriétaire ∪ gérant ∪ escalade `roles.py`, ADR 0030/
0048). Partager est un acte de gouvernance — jamais un rôle d'org.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ... import db, ownership
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .common import HORODATAGE, govern_ns
from ..registry import CAPABILITIES


# ⚠️ Une adresse ne DÉSIGNE PAS un compte : dix en portent deux (mesuré le
# 05/09/2026), dont une paire sans aucun tenant. Partager sur une adresse ambiguë
# revenait à donner l'accès à l'un des deux, en silence — le seul endroit de cette
# classe où une erreur donne accès à des DONNÉES. D'où `sub`, qui lève l'ambiguïté,
# et le refus qui la nomme.
_SUB = ("Identifiant du compte destinataire, quand une adresse en désigne "
        "plusieurs. À passer SEUL (jamais avec `email` : deux façons de nommer "
        "la même chose, dont une pourrait mentir sur l'autre).")


class ShareInput(BaseModel):
    namespace: str
    email: str = ""
    sub: str = Field(default="", description=_SUB)
    # ADR 0068 : partager sans préciser donnait l'ÉCRITURE. « Partager », dans la tête
    # de qui le demande, veut dire « qu'il puisse le lire ».
    permission: str = "read"


class UnshareInput(BaseModel):
    namespace: str
    email: str = ""
    sub: str = Field(default="", description=_SUB)


class NamespaceRefInput(BaseModel):
    namespace: str


class Share(BaseModel):
    """Un partage nominatif tel que le rend cette surface (vue APLATIE d'un grant)."""
    email: Optional[str] = None
    # `read` | `write` — la projection CONTENU du rôle (ADR 0048).
    permission: Optional[str] = None
    principal_type: Optional[str] = None
    principal_id: Optional[str] = None
    # Date d'octroi (`granted_at` côté `resource_grants`).
    created_at: Optional[str] = Field(default=None, description=HORODATAGE)


class ShareList(BaseModel):
    shares: list[Share]


class Shared(BaseModel):
    ok: bool
    namespace: str
    shared_with: str
    permission: str


class Unshared(BaseModel):
    ok: bool
    namespace: str
    removed: str


def _recipient(email: str) -> dict:
    """Le destinataire, ou le refus tel quel.

    ⚠️ L'ordre des gardes est celui de la route d'avant : le destinataire est résolu
    AVANT la vérification de gouvernance. Un appelant sans droit sur le tableau peut
    donc distinguer « cet email a un compte oto » de « il n'en a pas ». Conservé à
    l'identique — une migration de plomberie ne change pas le comportement observable ;
    l'inversion est un correctif à part, à trancher pour ses propres raisons.
    """
    porteurs = db.get_users_by_email(email)
    if not porteurs:
        raise AuthzDenied(404, f"no oto user with email {email}")
    if len(porteurs) > 1:
        # ⚠️ On ne DEVINE pas : partager avec « un des deux » donnerait l'accès à
        # un compte que le propriétaire ne visait peut-être pas, et rien ne le
        # lui dirait. Le refus nomme les candidats ET la sortie.
        subs = ", ".join(f"`{u['sub']}`" for u in porteurs)
        raise AuthzDenied(
            400, "ambiguous_email",
            f"L'adresse `{email}` désigne {len(porteurs)} comptes : {subs}. "
            "Reprends avec `sub` (sans `email`) pour dire lequel tu vises.")
    return porteurs[0]


def _destinataire(email: str, sub: str) -> dict:
    """Le compte visé — par son adresse, ou par son identifiant s'il est ambigu.

    Les deux ensemble sont REFUSÉS : ils peuvent se contredire, et rien ne dirait
    lequel a servi. Un seul chemin, choisi par l'appelant.
    """
    email, sub = (email or "").strip(), (sub or "").strip()
    if email and sub:
        raise AuthzDenied(
            400, "email_and_sub",
            "Passe `email` OU `sub`, pas les deux : ils peuvent désigner des "
            "comptes différents, et rien ne dirait lequel a servi.")
    if sub:
        row = db.get_user(sub)
        if not row:
            raise AuthzDenied(404, "unknown_user", f"Aucun compte `{sub}`.")
        return row
    if not email:
        raise AuthzDenied(400, "email_required")
    return _recipient(email)


def _share(ctx: ResolvedCtx, inp: ShareInput) -> dict:
    permission = (inp.permission or "read").strip()
    if permission not in ("read", "write"):
        # Le message EST le code ici — forme héritée de la route, gardée telle quelle.
        raise AuthzDenied(400, "permission must be 'read' or 'write'")
    recipient = _destinataire(inp.email, inp.sub)
    email = (inp.email or "").strip() or recipient["sub"]
    ns_id = govern_ns(ctx.sub, inp.namespace)
    ownership.grant("datastore_namespace", str(ns_id), "user", recipient["sub"],
                    permission, granted_by=ctx.sub)
    return {"ok": True, "namespace": inp.namespace, "shared_with": email,
            "permission": permission}


def _unshare(ctx: ResolvedCtx, inp: UnshareInput) -> dict:
    recipient = _destinataire(inp.email, inp.sub)
    email = (inp.email or "").strip() or recipient["sub"]
    ns_id = govern_ns(ctx.sub, inp.namespace)
    if not ownership.revoke("datastore_namespace", str(ns_id), "user", recipient["sub"]):
        raise AuthzDenied(404, f"no active share for {email} on {inp.namespace}")
    return {"ok": True, "namespace": inp.namespace, "removed": email}


def _list_shares(ctx: ResolvedCtx, inp: NamespaceRefInput) -> dict:
    ns_id = govern_ns(ctx.sub, inp.namespace)
    return {"shares": [
        {"email": s.get("email"), "permission": s.get("permission"),
         "principal_type": s.get("principal_type"), "principal_id": s.get("principal_id"),
         "created_at": s.get("granted_at")}
        for s in ownership.list_grants("datastore_namespace", str(ns_id))
    ]}


_SHARE = "/api/datastore/namespaces/{namespace}/share"

CAPABILITIES += [
    Capability(
        key="me.datastore.list_shares",
        handler=_list_shares,
        Input=NamespaceRefInput,
        Output=ShareList,
        authz=SUB_ONLY,
        mcp=None,  # la face agent du partage est `oto_resource op=share` (ADR 0048)
        rest=RestBinding(verb="GET", path=_SHARE),
        description="Liste les partages nominatifs d'un tableau (droit de gouvernance).",
    ),
    Capability(
        key="me.datastore.share",
        handler=_share,
        Input=ShareInput,
        Output=Shared,
        authz=SUB_ONLY,
        mcp=None,
        rest=RestBinding(verb="POST", path=_SHARE),
        description=("Partage un tableau avec un utilisateur oto, en lecture ou écriture. "
                     "Le destinataire se nomme par `email` — ou par `sub` quand une "
                     "adresse désigne plusieurs comptes."),
    ),
    Capability(
        key="me.datastore.unshare",
        handler=_unshare,
        Input=UnshareInput,
        Output=Unshared,
        authz=SUB_ONLY,
        mcp=None,
        # Corps sur un DELETE : forme historique du client `oto-core`, déclarée.
        rest=RestBinding(verb="DELETE", path=_SHARE, reads_body=True),
        description=("Retire le partage d'un tableau pour un utilisateur, nommé par "
                     "`email` ou par `sub` si son adresse est ambiguë."),
    ),
]
