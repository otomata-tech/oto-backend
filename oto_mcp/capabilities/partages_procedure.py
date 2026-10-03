"""Partager UNE procédure par lien — « See who's reading » (Tulina).

Un propriétaire (org_admin de l'org de la procédure) publie un lien `/p/<token>`.
Sans compte, on lit la VITRINE : titre, description, auteur, connecteurs, et la
FORME du graphe — jamais le corps. Connecté, on lit le corps et on est noté LECTEUR
(sauf si l'on est membre de l'org propriétaire, ou si le propriétaire a coupé
« voir qui lit »). Le lecteur peut COPIER la procédure dans son espace.

Trois régimes, trois faces :

- **propriétaire** (`/api/me/instructions/{slug}/share*`, et `oto_procedure`
  op=share_*) : même garde que l'écriture de la procédure (`ORG_ADMIN_OPT`) ;
- **vitrine anonyme** (`GET /api/public/process-shares/{token}`) : écrite à la main
  dans `api/public.py` (l'adaptateur des capacités authentifie toujours), elle appelle
  `vitrine()` d'ici — le même constructeur que la lecture connectée, pour que les deux
  ne puissent pas diverger sur ce qui reste CACHÉ ;
- **lecteur connecté** (`/api/me/process-shares/{token}`) : `SUB_ONLY`.

⚠️ **Ce que la vitrine ne sert jamais : le corps, ni aucun texte d'étape.** C'est la
raison du retrait des vitrines anonymes de la bibliothèque (oto#84 : elles servaient le
corps sans jeton). La forme du graphe est validée à l'écriture par un schéma FERMÉ
(`valider_forme`) — énumérations et slugs seulement — donc aucun texte ne peut y être
glissé pour sortir par là.

⚠️ **Publier ne sort pas d'une conversation** (`_publication.refuser_si_agent`) : le
corps devient lisible par quiconque se crée un compte, ce qui équivaut à une ouverture
au web. `op=share_publish` est refusé à un agent ; retirer, régler et lire les lecteurs
lui restent ouverts.

La bibliothèque publique (`guide_library`) n'est PAS touchée : elle reste une vitrine
éditée par la plateforme (30a1ada5). Un partage n'y entre jamais et n'est listé nulle
part — il ne se trouve que par son lien.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel

from .. import access, config, db, org_store, roles, slots as slots_mod
from ..db import partages_procedure as db_partages
from . import _publication
from ._authz import ORG_ADMIN_OPT, SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

# ── La forme du graphe (schéma FERMÉ) ──────────────────────────────────────────

TRIGGER_KINDS = ("schedule", "calendar", "chat", "event")
STEP_KINDS = ("ai", "human", "check")
MAX_STEPS = 30
MAX_CONNECTORS = 4
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
#: Lecteurs visibles à partir desquels la vitrine affiche leur NOMBRE (jamais de nom).
SEUIL_NOMBRE_LECTEURS = 5

# Messageries grand public : un domaine d'ici ne dit rien de l'entreprise du lecteur,
# il n'est donc pas compté comme une « entreprise » dans les totaux.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr", "hotmail.com",
    "hotmail.fr", "live.com", "live.fr", "msn.com", "yahoo.com", "yahoo.fr",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.fr", "gmx.de", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr",
    "laposte.net", "yandex.com", "mail.com",
})


def _forme_refusee(message: str):
    return AuthzDenied(422, "invalid_preview_shape", message)


def valider_forme(raw) -> dict:
    """Rend la forme NORMALISÉE, ou lève `invalid_preview_shape`. Toute clé inconnue,
    tout type inattendu, toute chaîne hors énumération ou hors motif de slug est
    refusée : c'est ce qui garantit qu'aucun texte libre n'entre dans la vitrine."""
    if not isinstance(raw, dict):
        raise _forme_refusee("`preview_shape` doit être un objet.")
    inconnues = set(raw) - {"v", "trigger", "steps"}
    if inconnues:
        raise _forme_refusee(f"clé(s) inconnue(s) : {sorted(inconnues)}.")
    if raw.get("v") != 1 or isinstance(raw.get("v"), bool):
        raise _forme_refusee("`v` doit valoir 1.")
    trig = raw.get("trigger")
    if trig is not None:
        if not isinstance(trig, dict) or set(trig) != {"kind"} \
                or trig["kind"] not in TRIGGER_KINDS:
            raise _forme_refusee(f"`trigger` = null ou {{kind: {'|'.join(TRIGGER_KINDS)}}}.")
        trig = {"kind": trig["kind"]}
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise _forme_refusee(f"`steps` = une liste de 1 à {MAX_STEPS} étapes.")
    propres = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict) or not set(st) <= {"kind", "connectors"} \
                or st.get("kind") not in STEP_KINDS:
            raise _forme_refusee(
                f"étape {i} : {{kind: {'|'.join(STEP_KINDS)}, connectors: [slug]}}.")
        cons = st.get("connectors", [])
        if not isinstance(cons, list) or len(cons) > MAX_CONNECTORS or not all(
                isinstance(c, str) and _SLUG.match(c) for c in cons):
            raise _forme_refusee(
                f"étape {i} : `connectors` = au plus {MAX_CONNECTORS} slugs [a-z0-9_-].")
        propres.append({"kind": st["kind"], "connectors": list(dict.fromkeys(cons))})
    return {"v": 1, "trigger": trig, "steps": propres}


# ── Sorties ────────────────────────────────────────────────────────────────────

class ShareView(BaseModel):
    """Le partage actif d'une procédure, vu par son propriétaire."""
    token: str
    url: str
    show_readers: bool
    created_at: str
    shape_version: Optional[int] = None
    shape_fresh: bool


class ShareEnvelope(BaseModel):
    """`share` = None quand la procédure n'est pas (ou plus) publiée."""
    share: Optional[ShareView] = None


class ReaderRow(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    domain: Optional[str] = None
    first_read_at: str
    last_read_at: str
    reads: int
    copied_at: Optional[str] = None


class ReaderTotals(BaseModel):
    readers: int
    companies: int
    copies: int
    preview_views_30d: int


class ReadersView(BaseModel):
    """Les lecteurs VISIBLES (lus sous « voir qui lit »), tous liens de la procédure
    confondus, hors membres de l'org propriétaire."""
    totals: ReaderTotals
    readers: list[ReaderRow]


class Author(BaseModel):
    name: Optional[str] = None
    org_name: Optional[str] = None


class PublicPreview(BaseModel):
    """La VITRINE — ce qu'un visiteur sans compte voit. Jamais de corps."""
    title: str
    description: Optional[str] = None
    author: Author
    connectors: list[str]
    step_count: Optional[int] = None
    show_readers: bool
    preview_shape: Optional[dict] = None
    reader_count: Optional[int] = None


class CopiedRef(BaseModel):
    org_id: int
    slug: str


class SharedProcessView(PublicPreview):
    """La vitrine + le corps, pour un lecteur connecté."""
    body_md: str
    version: int
    is_member: bool
    copied: Optional[CopiedRef] = None


class CopyResult(BaseModel):
    org_id: int
    slug: str
    created: bool
    is_new_account: bool


# ── Entrées ────────────────────────────────────────────────────────────────────

class ShareSlugInput(BaseModel):
    slug: str
    org: Optional[int] = None


class ShareWriteInput(BaseModel):
    slug: str
    op: Literal["publish", "unpublish", "set"]
    show_readers: Optional[bool] = None
    preview_shape: Optional[dict] = None
    shape_version: Optional[int] = None
    org: Optional[int] = None


class TokenInput(BaseModel):
    token: str


# ── Briques communes ───────────────────────────────────────────────────────────

def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    return str(v)


def domaine(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def _url(org_id: Optional[int], created_by: str, token: str) -> str:
    """Le lien servi au propriétaire : sur le front qui héberge l'org (`orgs.front_*`),
    sinon celui du compte — jamais une adresse écrite en dur."""
    base = org_store.org_front(org_id)[0] or config.dashboard_url_for(created_by)
    return f"{base.rstrip('/')}/p/{token}"


def _share_view(share: dict, instr: dict) -> dict:
    return {
        "token": share["token"],
        "url": _url(share.get("org_id"), share["created_by"], share["token"]),
        "show_readers": bool(share["show_readers"]),
        "created_at": _iso(share["created_at"]),
        "shape_version": share.get("shape_version"),
        "shape_fresh": _forme_fraiche(share, instr),
    }


def _forme_fraiche(share: dict, instr: dict) -> bool:
    return (share.get("preview_shape") is not None
            and share.get("shape_version") == instr.get("version"))


def _procedure_de_l_org(ctx: ResolvedCtx, slug: str) -> dict:
    instr = org_store.get_instruction("org", ctx.org_id, slug)
    if not instr or instr.get("id") is None:
        raise AuthzDenied(404, "not_found",
                          f"Procédure `{org_store.normalize_slug(slug)}` absente.")
    return instr


def _procedure_partagee(token: str) -> tuple[dict, dict]:
    """(partage actif, procédure vivante) pour ce jeton — 404 si l'un manque, si la
    procédure est archivée, ou si elle n'est pas (ou plus) une procédure d'org."""
    share = db_partages.par_jeton(token)
    instr = org_store.get_instruction_by_id(share["instruction_id"]) if share else None
    if not share or not instr or instr.get("archived_at") is not None \
            or instr.get("owner_type") != "org":
        raise AuthzDenied(404, "not_found", "Ce lien ne mène à aucune procédure.")
    return share, instr


def _connecteurs(instr: dict, forme: Optional[dict]) -> list[str]:
    out: list[str] = []
    if forme:
        for st in forme["steps"]:
            out.extend(st["connectors"])
    out.extend(sorted(slots_mod._referenced_connectors(instr.get("body_md") or "")))
    for s in instr.get("slots") or []:
        if isinstance(s, dict) and s.get("type") == "connecteur":
            nom = s.get("connector") or s.get("name")
            if isinstance(nom, str) and nom:
                out.append(nom)
    return list(dict.fromkeys(out))


def vitrine(share: dict, instr: dict) -> dict:
    """La vitrine d'un partage. ⚠️ Seul constructeur de ce que voit un visiteur sans
    compte : il ne lit JAMAIS `body_md` pour le rendre, seulement pour en déduire les
    connecteurs cités."""
    forme = share["preview_shape"] if _forme_fraiche(share, instr) else None
    auteur = db.get_user(share["created_by"]) or {}
    org = org_store.get_org(share["org_id"]) if share.get("org_id") else None
    n = db_partages.nombre_lecteurs(instr["id"], share.get("org_id"))
    return {
        "title": instr.get("title") or instr["slug"],
        "description": instr.get("description") or None,
        "author": {"name": auteur.get("name") or None,
                   "org_name": (org or {}).get("name") or None},
        "connectors": _connecteurs(instr, forme),
        "step_count": len(forme["steps"]) if forme else None,
        "show_readers": bool(share["show_readers"]),
        "preview_shape": forme,
        "reader_count": n if n >= SEUIL_NOMBRE_LECTEURS else None,
    }


def _copie_vivante(instruction_id: int, sub: str) -> Optional[dict]:
    cid = db_partages.copie_existante(instruction_id, sub)
    copie = org_store.get_instruction_by_id(cid) if cid else None
    if not copie or copie.get("archived_at") is not None or copie.get("org_id") is None:
        return None
    return copie


# ── Propriétaire ───────────────────────────────────────────────────────────────

def _share_get(ctx: ResolvedCtx, inp: ShareSlugInput) -> dict:
    instr = _procedure_de_l_org(ctx, inp.slug)
    share = db_partages.actif(instr["id"])
    return {"share": _share_view(share, instr) if share else None}


def _share_write(ctx: ResolvedCtx, inp: ShareWriteInput) -> dict:
    instr = _procedure_de_l_org(ctx, inp.slug)
    if inp.op == "unpublish":
        db_partages.retirer(instr["id"])
        return {"share": None}
    forme = None
    if inp.preview_shape is not None:
        forme = valider_forme(inp.preview_shape)
        if inp.shape_version is None:
            raise _forme_refusee("`shape_version` requis avec `preview_shape` : la "
                                 "version de procédure dont la forme est tirée.")
    if inp.op == "publish":
        _publication.refuser_si_agent(
            ctx, "cette procédure",
            "Elle se publie depuis l'application, bouton « Share » de la procédure.")
        share, cree = db_partages.publier(
            instr["id"], ctx.org_id, ctx.sub,
            show_readers=True if inp.show_readers is None else inp.show_readers,
            preview_shape=forme, shape_version=inp.shape_version if forme else None)
        if not cree and (inp.show_readers is not None or forme is not None):
            share = _regler(instr, inp, forme) or share
        return {"share": _share_view(share, instr)}
    share = _regler(instr, inp, forme)
    if share is None:
        raise AuthzDenied(404, "no_share", "Cette procédure n'est pas publiée.")
    return {"share": _share_view(share, instr)}


def _regler(instr: dict, inp: ShareWriteInput, forme: Optional[dict]) -> Optional[dict]:
    kw = {}
    if forme is not None:
        kw = {"preview_shape": forme, "shape_version": inp.shape_version}
    return db_partages.regler(instr["id"], show_readers=inp.show_readers, **kw)


def _share_readers(ctx: ResolvedCtx, inp: ShareSlugInput) -> dict:
    instr = _procedure_de_l_org(ctx, inp.slug)
    rows = db_partages.lecteurs(instr["id"], ctx.org_id)
    lecteurs = [{
        "name": r.get("name") or None, "email": r.get("email") or None,
        "domain": domaine(r.get("email")),
        "first_read_at": _iso(r["first_read_at"]), "last_read_at": _iso(r["last_read_at"]),
        "reads": int(r["reads"] or 0), "copied_at": _iso(r.get("copied_at")),
    } for r in rows]
    entreprises = {x["domain"] for x in lecteurs
                   if x["domain"] and x["domain"] not in FREE_MAIL}
    return {"totals": {"readers": len(lecteurs), "companies": len(entreprises),
                       "copies": sum(1 for x in lecteurs if x["copied_at"]),
                       "preview_views_30d": db_partages.vues(instr["id"], 30)},
            "readers": lecteurs}


# ── Lecteur connecté ───────────────────────────────────────────────────────────

def _read(ctx: ResolvedCtx, inp: TokenInput) -> dict:
    share, instr = _procedure_partagee(inp.token)
    membre = bool(share.get("org_id")) and roles.is_org_member(ctx.sub, share["org_id"])
    if not membre:
        db_partages.noter_lecture(share["id"], ctx.sub, bool(share["show_readers"]))
        profil = (db.get_account_profile(ctx.sub) or {}).get("profile") or {}
        if not profil.get("acquired_via"):
            db.update_account_profile(ctx.sub, {"acquired_via": {
                "kind": "process_share", "token": share["token"],
                "at": datetime.now(timezone.utc).isoformat()}})
    copie = _copie_vivante(instr["id"], ctx.sub)
    return {**vitrine(share, instr), "body_md": instr["body_md"],
            "version": int(instr["version"]), "is_member": membre,
            "copied": ({"org_id": int(copie["org_id"]), "slug": copie["slug"]}
                       if copie else None)}


def _org_cible(sub: str) -> int:
    """L'org active si l'appelant peut y écrire une procédure, sinon son org perso."""
    active = access.current_org(sub)
    if active and roles.is_org_admin(sub, active):
        return int(active)
    u = db.get_user(sub) or {}
    return int(org_store.ensure_personal_org(sub, u.get("email"), u.get("name")))


def _nouveau_compte(sub: str, org_id: int) -> bool:
    if not org_store.is_personal_org(org_id):
        return False
    profil = (db.get_account_profile(sub) or {}).get("profile") or {}
    return not (profil.get("onboarded_at") or profil.get("onboarding_skipped_at"))


def _copy(ctx: ResolvedCtx, inp: TokenInput) -> dict:
    share, instr = _procedure_partagee(inp.token)
    membre = bool(share.get("org_id")) and roles.is_org_member(ctx.sub, share["org_id"])
    deja = None if membre else _copie_vivante(instr["id"], ctx.sub)
    if deja:
        org_id = int(deja["org_id"])
        return {"org_id": org_id, "slug": deja["slug"], "created": False,
                "is_new_account": _nouveau_compte(ctx.sub, org_id)}
    cible = _org_cible(ctx.sub)
    copie = org_store.copy_instruction_to_owner(instr["id"], "org", cible, set_by=ctx.sub)
    if not membre:
        db_partages.noter_copie(share["id"], ctx.sub, bool(share["show_readers"]),
                                int(copie["id"]))
    return {"org_id": cible, "slug": copie["slug"], "created": True,
            "is_new_account": _nouveau_compte(ctx.sub, cible)}


# ── Déclarations ───────────────────────────────────────────────────────────────

_NOT_FOUND = DeclaredError(404, "not_found", "procédure absente, lien inconnu, retiré, "
                           "ou procédure archivée")
_FORME = DeclaredError(422, "invalid_preview_shape", "`preview_shape` hors du schéma "
                       "fermé, ou sans `shape_version`")

CAPABILITIES += [
    Capability(
        key="org.instruction.share.get", handler=_share_get, Input=ShareSlugInput,
        authz=ORG_ADMIN_OPT("org"), Output=ShareEnvelope,
        description=("The web link of one of your org's procedures (org_admin): token, "
                     "url, whether readers are recorded, and whether the stored graph "
                     "shape matches the current version. `share` is null when the "
                     "procedure is not published."),
        errors=(_NOT_FOUND,),
        rest=RestBinding("GET", "/api/me/instructions/{slug}/share"),
    ),
    Capability(
        key="org.instruction.share.write", handler=_share_write, Input=ShareWriteInput,
        authz=ORG_ADMIN_OPT("org"), Output=ShareEnvelope,
        description=("Publish (`op=publish`, idempotent), revoke (`op=unpublish`: the "
                     "link stops working; publishing again issues a NEW token) or adjust "
                     "(`op=set`) the web link of a procedure (org_admin). Anyone with the "
                     "link sees its overview without an account; signed-in readers see "
                     "the body. `show_readers` records who reads it. `preview_shape` + "
                     "`shape_version` = the graph SHAPE shown behind the sign-up gate "
                     "(closed schema: trigger kind, then per step its kind and "
                     "connectors — no text). Publishing is refused to an agent."),
        errors=(_NOT_FOUND, _FORME,
                DeclaredError(404, "no_share", "`op=set` sur une procédure non publiée"),
                DeclaredError(403, "publication_reservee_a_l_humain",
                              "`op=publish` demandé depuis une conversation d'agent")),
        rest=RestBinding("POST", "/api/me/instructions/{slug}/share"),
    ),
    Capability(
        key="org.instruction.share.readers", handler=_share_readers,
        Input=ShareSlugInput, authz=ORG_ADMIN_OPT("org"), Output=ReadersView,
        description=("Who read a published procedure (org_admin): signed-in readers "
                     "recorded while `show_readers` was on, across every link of the "
                     "procedure, members of your org excluded — name, email, email "
                     "domain, reads, copy date — plus totals and 30-day overview views."),
        errors=(_NOT_FOUND,),
        rest=RestBinding("GET", "/api/me/instructions/{slug}/share/readers"),
    ),
    Capability(
        key="me.process_share.read", handler=_read, Input=TokenInput, authz=SUB_ONLY,
        Output=SharedProcessView,
        description=("Read a procedure shared by link, signed in: its overview plus the "
                     "body. Records you as a reader unless you belong to the owning org."),
        errors=(_NOT_FOUND,),
        rest=RestBinding("GET", "/api/me/process-shares/{token}"),
    ),
    Capability(
        key="me.process_share.copy", handler=_copy, Input=TokenInput, authz=SUB_ONLY,
        Output=CopyResult,
        description=("Copy a procedure shared by link into your workspace: your active "
                     "org if you administer it, otherwise your personal workspace. "
                     "Idempotent — a second call returns the same copy."),
        errors=(_NOT_FOUND,),
        rest=RestBinding("POST", "/api/me/process-shares/{token}/copy"),
    ),
]
