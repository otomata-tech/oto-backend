"""Les favoris d'une org : des pages et des tableaux épinglés pour tous ses membres
(portée `org`), ou pour soi seul dans cette org (portée `me`).

Trois routes REST, pour la barre latérale du tableau de bord :

- `GET    /api/orgs/{id}/favorites`                            — membres : les favoris
  partagés de l'org ET les favoris personnels de l'appelant dans l'org ;
- `POST   /api/orgs/{id}/favorites`                            — membres : épingler
  `{kind, id, scope}` (`scope` = `org` par défaut) ;
- `DELETE /api/orgs/{id}/favorites/{kind}/{ref_id}?scope=me`   — membres : désépingler
  (`scope` = `org` par défaut, en query string).

Chacune rend la liste à jour, `{"favorites": [...]}`, dans l'ordre où ils ont été posés,
chaque favori portant sa `scope`. Le même objet peut être favori partagé ET personnel :
deux lignes, rendues toutes deux, et retirer l'une laisse l'autre.

**Un favori n'accorde rien.** Chaque membre n'en LIT que ce que ses droits lui ouvrent
déjà : la page par `docs.common.acces_a_la_page` (son projet, ou un partage de la page),
le tableau par `ownership.can_access` — les deux portes de lecture du produit, jamais
recopiées ici. Le tableau perso d'un collègue épinglé par lui pour l'org reste invisible
aux autres ; les favoris PERSONNELS d'un membre ne sont jamais rendus à un autre.

**Épingler exige de lire la cible.** Pour l'org, elle doit AUSSI être de l'org : elle y
VIT (`ownership.org_ou_vit`, ADR 0071 — l'org de son propriétaire, ou de rangement pour
un objet perso), ou elle est partagée à l'org ou à une de ses équipes. Pour soi, la
lecture suffit (son tableau perso, un tableau qu'on lui partage, une page d'un projet
privé). Sinon 404 `favorite_target_unknown`, la même réponse pour « n'existe pas » et
« ne se lit pas » — les ids sont des entiers séquentiels globaux (F-016).

**Épingler pour l'org exige que d'autres puissent la lire.** Une cible lisible par son
seul auteur — un objet PERSONNEL (propriétaire `user`, rangé dans l'org ou non) que rien
ne partage, ni lui ni, pour une page, son projet — est refusée en `org` : 409
`favorite_target_private`. En `me`, elle s'épingle. Un seul partage vivant (à l'org, à une
équipe, à une personne) suffit à la lever : la règle lit `resource_grants`, rien d'autre.

**Désépingler un favori partagé** est réservé à son auteur (`added_by`) et aux admins de
l'org (`roles.is_org_admin`), idempotent pour eux (200 si le favori n'est plus là). Un
autre membre reçoit 404 `favorite_target_unknown` s'il ne lit pas la cible (même réponse
qu'au POST, il n'apprend rien), 403 `favorite_not_yours` s'il la lit et que le favori
existe, et 200 sans rien toucher s'il la lit et que le favori est absent. Un favori
personnel ne se retire que par son propriétaire — la route ne touche que la ligne de
l'appelant, idempotent.

Une cible supprimée sort de la liste : ses lignes, toutes portées et toutes orgs, partent
dans la transaction de la suppression (`db.delete_doc`, `db.delete_datastore_by_id`) ;
une ligne morte qui aurait échappé à ces chemins est retirée à la lecture (filet). Une
page ARCHIVÉE, ou dont le projet l'est, sort de la liste sans être retirée. Quitter l'org
retire ses favoris personnels (`org_store.remove_org_member`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from ... import db, org_origin, ownership, roles
from ...db import favoris as store
from .._authz import ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..docs import common as docs_common
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}

DOC, TABLE = "doc", "table"
ORG, ME = "org", "me"
SCOPES = (ORG, ME)

#: Ce que rend `_resoudre` pour un objet qui n'existe plus (ses lignes se retirent).
DISPARU = "disparu"


class FavoritesInput(BaseModel):
    org_id: int


class FavoriteAddInput(BaseModel):
    org_id: int
    kind: str = Field(description="`doc` (a memory page) or `table` (a datastore).")
    id: int = Field(description="The page id or the table id.")
    scope: str = Field(default=ORG, description=(
        "`org` (default) = shared with every member of the organization; `me` = only "
        "for you, in this organization."))


class FavoriteRemoveInput(BaseModel):
    org_id: int
    kind: str
    ref_id: int
    scope: str = Field(default=ORG, description=(
        "`org` (default) removes the shared favorite; `me` removes only yours."))


class Favorite(BaseModel):
    kind: str
    id: int
    title: str
    # `org` = partagé avec l'org ; `me` = le favori personnel de l'appelant.
    scope: str
    # Le projet dont la mémoire porte la page ; None pour un tableau.
    project_id: Optional[int] = None
    added_by: Optional[str] = None
    added_at: str


class Favorites(BaseModel):
    favorites: list[Favorite]


def _kind(kind: str) -> str:
    if kind not in store.KINDS:
        raise AuthzDenied(400, "invalid_favorite_kind",
                          f"`kind` vaut {' | '.join(store.KINDS)} (reçu {kind!r}).")
    return kind


def _scope(scope: str) -> str:
    if scope not in SCOPES:
        raise AuthzDenied(400, "invalid_favorite_scope",
                          f"`scope` vaut {' | '.join(SCOPES)} (reçu {scope!r}).")
    return scope


def _proprietaire(ctx: ResolvedCtx, scope: str) -> Optional[str]:
    """La valeur de `owner_sub` d'une portée : None pour l'org, l'appelant pour soi."""
    return ctx.sub if scope == ME else None


def _partages_vivants(rtype: str, rid: str) -> list[dict]:
    return [g for g in ownership.list_grants(rtype, rid) if not g.get("expired")]


def _partage_a_l_org(org_id: int, rtype: str, rid: str) -> bool:
    """Un partage VIVANT de la ressource à l'org, ou à une de ses équipes."""
    vivants = _partages_vivants(rtype, rid)
    if any(g["principal_type"] == "org" and str(g["principal_id"]) == str(org_id)
           for g in vivants):
        return True
    equipes = [("group", g["principal_id"]) for g in vivants
               if g["principal_type"] == "group"]
    return any(int(o) == int(org_id) for o in org_origin.group_orgs(equipes).values())


def _de_l_org(org_id: int, rtype: str, rid: str) -> bool:
    org, connue = ownership.org_ou_vit(rtype, rid)
    if connue and org is not None and int(org) == int(org_id):
        return True
    return _partage_a_l_org(org_id, rtype, rid)


def _lisible_par_son_seul_auteur(kind: str, ref_id: int) -> bool:
    """Un objet PERSONNEL (propriétaire `user`) qu'aucun partage vivant n'ouvre : un
    tableau sans partage ; une page dont ni elle ni son projet ne sont partagés. Lu sur
    les deux portes existantes (`ownership.owner_of`, `resource_grants`), sans règle
    propre. Appelée sur une cible déjà résolue (elle existe)."""
    rid = str(ref_id)
    if kind == DOC:
        page = db.get_doc_by_id(int(ref_id))
        portes = [(docs_common.PROJECT_RTYPE, str(page["project_id"])),
                  (docs_common.DOC_RTYPE, rid)]
        owner = ownership.owner_of(docs_common.DOC_RTYPE, rid)
    else:
        portes = [(ownership.TYPE_RESSOURCE_DATASTORE, rid)]
        owner = ownership.owner_of(ownership.TYPE_RESSOURCE_DATASTORE, rid)
    return (owner is not None and owner[0] == "user"
            and not any(_partages_vivants(*p) for p in portes))


def _resoudre(sub: str, org_id: int, kind: str, ref_id: int, scope: str) -> Any:
    """Ce que `sub` voit du favori `(kind, ref_id)` de cette portée dans l'org :
    `{title, project_id}`, `None` s'il ne le lit pas (ou, pour l'org, si l'objet n'est
    pas de l'org), `DISPARU` si l'objet n'existe plus."""
    if kind == DOC:
        page = db.get_doc_by_id(int(ref_id))
        if page is None:
            return DISPARU
        projet = db.get_project_by_id(int(page["project_id"]))
        if projet is None:
            return DISPARU
        if page.get("archived_at") or projet.get("archived_at"):
            return None
        pid = str(page["project_id"])
        if scope == ORG and not (
                _de_l_org(org_id, "project", pid)
                or _partage_a_l_org(org_id, docs_common.DOC_RTYPE, str(ref_id))):
            return None
        if docs_common.acces_a_la_page(sub, page) is None:
            return None
        return {"title": page.get("title") or "", "project_id": int(page["project_id"])}
    tableau = db.get_datastore_by_id(int(ref_id))
    if tableau is None:
        return DISPARU
    tds, rid = ownership.TYPE_RESSOURCE_DATASTORE, str(ref_id)
    if scope == ORG and not _de_l_org(org_id, tds, rid):
        return None
    if not ownership.can_access(sub, tds, rid, "read"):
        return None
    return {"title": tableau.get("datastore") or "", "project_id": None}


def _iso(v) -> str:
    """La date de pose en ISO 8601 AVEC son fuseau : le store rend un texte sans fuseau,
    en UTC (`db._conn._str_dict_row`, session `TimeZone=UTC`) — même règle que
    `service_commerce._iso` (#1073)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()


def _vue(sub: str, org_id: int) -> dict:
    out: list[dict] = []
    morts: dict[str, set[int]] = {}
    for f in store.lister(org_id, sub):
        scope = ORG if f.get("owner_sub") is None else ME
        vu = _resoudre(sub, org_id, f["kind"], f["ref_id"], scope)
        if vu == DISPARU:
            morts.setdefault(f["kind"], set()).add(int(f["ref_id"]))
            continue
        if vu is None:
            continue
        out.append({"kind": f["kind"], "id": int(f["ref_id"]), **vu, "scope": scope,
                    "added_by": f.get("added_by"), "added_at": _iso(f["added_at"])})
    for kind, ids in morts.items():
        store.retirer_morts(kind, sorted(ids))
    return {"favorites": out}


def _lister(ctx: ResolvedCtx, inp: FavoritesInput) -> dict:
    return _vue(ctx.sub, inp.org_id)


def _inconnue(kind: str, ref_id: int, scope: str) -> AuthzDenied:
    return AuthzDenied(404, "favorite_target_unknown",
                       f"Aucun{'e page' if kind == DOC else ' tableau'} #{ref_id} "
                       + ("lisible dans cette org." if scope == ORG else "lisible."))


def _ajouter(ctx: ResolvedCtx, inp: FavoriteAddInput) -> dict:
    kind, scope = _kind(inp.kind), _scope(inp.scope)
    if not isinstance(_resoudre(ctx.sub, inp.org_id, kind, inp.id, scope), dict):
        raise _inconnue(kind, inp.id, scope)
    if scope == ORG and _lisible_par_son_seul_auteur(kind, inp.id):
        raise AuthzDenied(409, "favorite_target_private",
                          "Cette page est visible par toi seul : partage-la d'abord."
                          if kind == DOC else
                          "Ce tableau est visible par toi seul : partage-le d'abord.")
    store.poser(inp.org_id, _proprietaire(ctx, scope), kind, inp.id, ctx.sub)
    return _vue(ctx.sub, inp.org_id)


def _retirer_le_partage(sub: str, org_id: int, kind: str, ref_id: int) -> None:
    """Retire le favori PARTAGÉ : son auteur ou un admin de l'org. L'admin retire sans
    condition ; l'auteur retire SA ligne (le filtre `added_by` est dans le DELETE : pas
    de fenêtre entre la vérification et le retrait). Pour tout autre membre, rien n'est
    retiré : 404 s'il ne lit pas la cible, 403 si le favori existe, 200 sinon."""
    if roles.is_org_admin(sub, org_id):
        store.retirer(org_id, None, kind, ref_id)
        return
    if store.retirer(org_id, None, kind, ref_id, added_by=sub):
        return
    if not isinstance(_resoudre(sub, org_id, kind, ref_id, ORG), dict):
        raise _inconnue(kind, ref_id, ORG)
    if store.existe(org_id, None, kind, ref_id):
        raise AuthzDenied(403, "favorite_not_yours",
                          "Seuls son auteur et un admin de l'org retirent un favori "
                          "partagé.")


def _retirer(ctx: ResolvedCtx, inp: FavoriteRemoveInput) -> dict:
    kind, scope = _kind(inp.kind), _scope(inp.scope)
    if scope == ORG:
        _retirer_le_partage(ctx.sub, inp.org_id, kind, inp.ref_id)
    else:
        store.retirer(inp.org_id, ctx.sub, kind, inp.ref_id)
    return _vue(ctx.sub, inp.org_id)


_KIND_INVALIDE = DeclaredError(400, "invalid_favorite_kind", "`kind` n'est ni `doc` ni `table`")
_SCOPE_INVALIDE = DeclaredError(400, "invalid_favorite_scope",
                                "`scope` n'est ni `org` ni `me`")
_CIBLE_INCONNUE = DeclaredError(
    404, "favorite_target_unknown",
    "la page ou le tableau n'existe pas, ou l'appelant ne le lit pas ; en portée `org`, "
    "aussi quand la cible n'est pas de l'org (ni rangée dedans, ni partagée à l'org ou à "
    "une de ses équipes)")


CAPABILITIES += [
    Capability(key="org.favorites.list", handler=_lister, Input=FavoritesInput,
               Output=Favorites, authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("GET", "/api/orgs/{id}/favorites", _ID)),
    Capability(key="org.favorites.add", handler=_ajouter, Input=FavoriteAddInput,
               Output=Favorites, authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("POST", "/api/orgs/{id}/favorites", _ID),
               errors=(_KIND_INVALIDE, _SCOPE_INVALIDE, _CIBLE_INCONNUE,
                       DeclaredError(409, "favorite_target_private",
                                     "portée `org` sur une cible lisible par son seul "
                                     "auteur (objet personnel que rien ne partage) : la "
                                     "partager d'abord, ou l'épingler en `me`"))),
    Capability(key="org.favorites.remove", handler=_retirer, Input=FavoriteRemoveInput,
               Output=Favorites, authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("DELETE", "/api/orgs/{id}/favorites/{kind}/{ref_id}",
                                _ID),
               errors=(_KIND_INVALIDE, _SCOPE_INVALIDE, _CIBLE_INCONNUE,
                       DeclaredError(403, "favorite_not_yours",
                                     "portée `org` : l'appelant lit la cible, mais n'est "
                                     "ni l'auteur du favori partagé ni admin de l'org"))),
]
