"""Le guide — primitif UNIQUE d'instruction (ADR 0042), sur ses deux axes.

Un guide = une unité de prose portée par **`scope`** (platform | org | group | user —
qui l'écrit, à qui elle s'applique) × **`delivery`** (`init` = concaténé dans l'artefact
injecté au handshake · `on-demand` = listé/chargé quand la tâche le demande). Ces deux
axes sont des PARAMÈTRES, pas des concepts distincts : « agent readme », « secret sauce »
et « instructions » ne sont plus que des libellés d'UI (§Convergence des surfaces,
Décision 5).

**Une capacité, deux faces** (ADR 0009) : `oto_guide` op-aware côté MCP + les routes
`/api/me/guides…` côté dashboard, **mêmes handlers, une seule autz par scope**
(`_owner_for_write`). Cœur = `guide_store` (table `guides`, tout-DB depuis le
2026-07-16 ; les fichiers `guides/*.md` ne sont que des seeds de boot).

`op=list` reste le catalogue **on-demand** : un readme `init` est DÉJÀ injecté, l'ajouter
à l'index que l'agent consulte pour charger de la prose serait un doublon de contexte.
On lit/écrit un init explicitement (`delivery='init'`).

Reste distinct : la **PROCÉDURE** (`org_instructions`, slots/versions — prose avec
sémantique d'exécution, ADR 0042 Décision 3) et la **fiche profil** (structurée,
`capabilities/profile.py`, Décision 6).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .. import guide_store, tool_alias
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_MAX_BODY_BYTES = 64 * 1024


class _NoInput(BaseModel):
    pass


Delivery = Literal["init", "on-demand"]


class GuideRefInput(BaseModel):
    scope: str
    slug: str = ""                       # omis quand delivery='init' (slug canonique)
    delivery: Delivery = "on-demand"
    owner_id: Optional[str] = None       # org/équipe CIBLÉE (défaut : l'active)


class GuideSetInput(BaseModel):
    scope: str
    slug: str = ""
    delivery: Delivery = "on-demand"
    owner_id: Optional[str] = None
    # La borne est en OCTETS (`_set` compte `body.encode()`), et elle est PUBLIÉE :
    # `maxLength` dans le schéma servi, sans validation pydantic — la valider en
    # caractères changerait le code du refus (`invalid_input` au lieu de
    # `body_too_large`) pour le même dépassement. Un front tiers dérive sa garde de
    # saisie du contrat ; jusqu'au 29/08/2026 le contrat ne disait rien.
    body_md: str = Field(
        "", json_schema_extra={"maxLength": _MAX_BODY_BYTES},
        description=("Corps markdown, au plus 65 536 OCTETS UTF-8 — au-delà : 400 "
                     "`body_too_large`. `maxLength` compte des caractères : nécessaire, "
                     "pas suffisant (un caractère accentué pèse deux octets)."))
    title: str = ""
    description: str = ""


class GuideOpInput(BaseModel):
    """Face MCP op-aware (`oto_guide`). `scope` défaut 'user' à l'écriture — un agent
    qui rédige sans préciser écrit POUR SON utilisateur, jamais pour l'org/la plateforme."""
    op: Literal["list", "read", "write", "delete"] = "list"
    slug: Optional[str] = None
    scope: Optional[str] = None
    delivery: Delivery = "on-demand"
    owner_id: Optional[str] = None
    body_md: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None


def _target_org(ctx: ResolvedCtx, owner_id: Optional[str]) -> int:
    """L'org visée : celle passée explicitement (dashboard, route par-id), sinon l'active."""
    if owner_id:
        return int(owner_id)
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org", "Aucune org active — vois `oto_use_org`.")
    return int(ctx.org_id)


def _target_group(ctx: ResolvedCtx, owner_id: Optional[str]) -> int:
    """L'équipe visée : celle passée explicitement, sinon l'active. ⚠️ Une vue qui gère
    UNE équipe donnée doit passer son id — l'équipe « active » est un contexte de session,
    pas ce que l'écran a sous les yeux."""
    if owner_id:
        return int(owner_id)
    from .. import access
    gid = ctx.group_id if ctx.group_id is not None else access.current_group(ctx.sub)
    if gid is None:
        raise AuthzDenied(400, "no_active_group",
                          "Aucune équipe active — vois `oto_use_group`.")
    return int(gid)


def _owner_for_write(ctx: ResolvedCtx, scope: str, owner_id: Optional[str] = None) -> str:
    """Le owner_id d'écriture pour `scope`, avec autz sur la cible RÉELLE. platform →
    platform_admin ; org → org_admin de l'org visée ; group → chef de l'équipe visée
    (escalade roles.py) ; user → self. Lève AuthzDenied."""
    from .. import roles
    if scope == "user":
        return ctx.sub                    # jamais d'écriture pour un AUTRE user
    if scope == "org":
        oid = _target_org(ctx, owner_id)
        if not roles.is_org_admin(ctx.sub, oid):
            raise AuthzDenied(403, "forbidden", "Réservé à un admin de l'org (guide d'org).")
        return str(oid)
    if scope == "group":
        gid = _target_group(ctx, owner_id)
        if not roles.can_admin_group(ctx.sub, gid):
            raise AuthzDenied(403, "forbidden",
                              "Réservé au chef de l'équipe (guide d'équipe).")
        return str(gid)
    if scope == "platform":
        if not roles.is_platform_admin(ctx.sub):
            raise AuthzDenied(403, "forbidden", "Réservé à l'admin plateforme (guide plateforme).")
        return guide_store.PLATFORM_OWNER
    if scope == "tenant":
        # Le socle d'un tenant remplace le nôtre pour TOUS ses comptes : c'est la
        # marque et le mode d'emploi d'un produit tiers. L'écrire est un acte
        # d'exploitation, jamais une préférence — même palier que le socle plateforme.
        if not roles.is_platform_admin(ctx.sub):
            raise AuthzDenied(403, "forbidden",
                              "Réservé à l'admin plateforme (socle de tenant).")
        return _target_tenant(ctx, owner_id)
    raise AuthzDenied(400, "bad_scope",
                      "scope éditable = platform | tenant | org | group | user.")


def _target_tenant(ctx: ResolvedCtx, owner_id: Optional[str]) -> str:
    """Le slug du tenant visé — explicite, ou celui de l'appelant.

    Refuse un tenant inconnu du registre plutôt que d'écrire une ligne que personne ne
    lira : le registre est construit au boot, donc une faute de frappe resterait muette
    jusqu'à ce qu'on cherche pourquoi la marque ne change pas."""
    from .. import tenancy
    slug = (owner_id or "").strip() or tenancy.current().tenant_of(ctx.sub)
    if not slug or slug == tenancy.PRIMARY_SLUG:
        raise AuthzDenied(400, "bad_owner",
                          "Préciser le tenant visé (son slug) — `platform` a son "
                          "propre scope.")
    if not any(e.slug == slug for e in tenancy.current().entries()):
        raise AuthzDenied(404, "unknown_tenant",
                          f"Aucun tenant `{slug}` au registre. Un tenant se déclare "
                          "en base et le registre se construit au démarrage.")
    return slug


def _owner_for_read(ctx: ResolvedCtx, scope: str, owner_id: Optional[str] = None) -> str:
    """Le owner_id de LECTURE d'un readme init. Sans cible explicite : l'identité
    courante du scope (ce que le handshake injecte déjà, aucun droit supplémentaire).
    Avec une cible : appartenance requise — un readme d'org/équipe n'est pas public."""
    from .. import roles
    if scope == "user":
        return ctx.sub
    if scope == "org":
        oid = _target_org(ctx, owner_id)
        if owner_id and not roles.is_org_member(ctx.sub, oid):
            raise AuthzDenied(403, "forbidden", "Réservé aux membres de l'org.")
        return str(oid)
    if scope == "group":
        gid = _target_group(ctx, owner_id)
        if owner_id and not roles.can_read_group(ctx.sub, gid):
            raise AuthzDenied(403, "forbidden", "Réservé aux membres de l'équipe.")
        return str(gid)
    if scope == "platform":
        return guide_store.PLATFORM_OWNER
    if scope == "tenant":
        # Lecture : son propre tenant sans rien demander (c'est ce que le handshake
        # injecte déjà) ; un autre tenant demande le palier plateforme.
        from .. import roles, tenancy
        sien = tenancy.current().tenant_of(ctx.sub)
        if owner_id and owner_id != sien and not roles.is_platform_admin(ctx.sub):
            raise AuthzDenied(403, "forbidden", "Réservé à l'admin plateforme.")
        return _target_tenant(ctx, owner_id)
    raise AuthzDenied(400, "bad_scope", "scope = platform | org | group | user.")


def _init_ref(ctx: ResolvedCtx, scope: str, slug: str, *, write: bool,
              owner_id: Optional[str] = None) -> tuple[str, str]:
    """`(ident passé au store, slug affichable)` d'un readme init.

    ⚠️ Asymétrie de `guide_store` : pour la PLATEFORME l'ident EST le slug du bloc
    (il en existe plusieurs — défaut `secret_sauce`), l'owner étant constant ; pour
    org/group/user l'ident est l'OWNER et le slug est canonique (`readme`) → l'appelant
    n'a pas à le connaître. L'autz passe toujours par le résolveur d'owner du mode."""
    owner = (_owner_for_write(ctx, scope, owner_id) if write
             else _owner_for_read(ctx, scope, owner_id))
    if scope == "platform":
        s = slug or guide_store.PLATFORM_SLUG
        return s, s
    return owner, guide_store.INIT_SLUG


class GuideRef(BaseModel):
    """Entrée de catalogue : de quoi CHOISIR un guide, sans en charger le corps."""
    slug: str
    scope: str                                   # platform | org | user
    title: str = ""
    description: str = ""


class GuideList(BaseModel):
    guides: list[GuideRef]


class GuideView(BaseModel):
    """Enveloppe COMMUNE de get et set, pour les deux livraisons — d'où les champs
    optionnels, qui ne coexistent pas tous :
    - un readme `init` porte `delivery` et `updated_at`, jamais de titre ;
    - un guide `on-demand` porte titre/description ;
    - `set` d'un on-demand ne renvoie PAS le corps (l'appelant vient de l'écrire).
    """
    scope: str
    slug: str
    delivery: Optional[str] = None
    body_md: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    updated_at: Optional[str] = None


class GuideDeleted(BaseModel):
    """Un readme `init` ne se supprime pas, il se VIDE — d'où `delivery` ici et un
    `deleted` qui vaut « la couche ne s'affiche plus », pas « la ligne est partie »."""
    scope: str
    slug: str
    deleted: bool
    delivery: Optional[str] = None


def _list(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    """Catalogue des guides on-demand visibles : plateforme ∪ org active ∪ perso (DB)."""
    return {"guides": guide_store.list_guides_for(ctx.sub, ctx.org_id)}


def _init_view(scope: str, slug: str, state: dict) -> dict:
    """Forme d'un readme init, alignée sur celle d'un guide on-demand."""
    return {"scope": scope, "slug": slug, "delivery": "init",
            "body_md": state.get("body_md") or "", "updated_at": state.get("updated_at")}


def _get(ctx: ResolvedCtx, inp: GuideRefInput) -> dict:
    if inp.delivery == "init":
        # Lecture de SON contexte (org/équipe actives) — ce que le handshake injecte.
        ident, slug = _init_ref(ctx, inp.scope, inp.slug, write=False,
                                owner_id=inp.owner_id)
        return _init_view(inp.scope, slug, guide_store.get_init_guide(inp.scope, ident))
    if not inp.slug:
        raise AuthzDenied(400, "missing_slug", "`slug` requis pour un guide on-demand.")
    # `scope` vide = pas de filtre : le store cherche plateforme → org → user (1er match).
    g = guide_store.read_guide_scoped(inp.slug, scope=inp.scope or None,
                                      org_id=ctx.org_id, sub=ctx.sub)
    if g is None:
        where = f" (scope {inp.scope})" if inp.scope else ""
        raise AuthzDenied(404, "not_found",
                          f"Guide `{inp.slug}`{where} introuvable — liste-les avec op=list.")
    # Les outils CITÉS dans le corps, au nom du produit de ce compte. Le socle le fait
    # depuis toujours (`instructions.session_layers`), les descriptions d'outils et les
    # messages d'erreur aussi — pas le texte d'un guide, alors que c'est le texte que le
    # socle prescrit d'aller lire. Sans ça le renommage se retourne contre lui-même : la
    # notice prescrit `oto_doc`, l'agent l'appelle (le serveur l'accepte), et le client
    # réaffiche `Oto doc` — le nom qu'on voulait faire disparaître, à l'endroit exact où
    # il se voit.
    #
    # ⚠️ Portée : les NOMS D'OUTILS, pas le nom du produit. Un guide qui écrit « la
    # boîte à outils oto » en toutes lettres continuera de le dire — la prose d'un guide
    # est du contenu, et la réécrire par substitution serait réécrire le texte de
    # quelqu'un. Le remède de ce cas-là est l'étage tenant ci-dessus : un partenaire que
    # notre prose gêne écrit la sienne.
    prefix = tool_alias.prefix_for(ctx.sub)
    if not prefix:
        return g
    return {**g, "body_md": tool_alias.rewrite_prose(g["body_md"], prefix)}


def _set(ctx: ResolvedCtx, inp: GuideSetInput) -> dict:
    # Le symétrique de `_get` : le corps est LU aux noms du produit, il s'ÉCRIT au
    # canonique. Sans ça, relire un guide puis le réenregistrer stockait `acme_doc`.
    body = tool_alias.canonical_prose(inp.body_md or "", ctx.sub)
    poids = len(body.encode())
    if poids > _MAX_BODY_BYTES:
        # La borne était publiée depuis le 29/08 (`maxLength` ci-dessus) ; il manquait la
        # MESURE. Sans elle l'auteur raccourcit à l'aveugle, et il ne peut même pas la
        # calculer : `maxLength` compte des caractères, la garde compte des OCTETS, et
        # deux textes de même longueur ne pèsent pas pareil (leçon du datastore, signal
        # #383 — « pas de combien on dépasse fait deviner »).
        raise AuthzDenied(
            400, "body_too_large",
            f"`body_md` pèse {poids} octets UTF-8 pour {_MAX_BODY_BYTES} au plus "
            f"({poids - _MAX_BODY_BYTES} de trop). La borne est en OCTETS : un caractère "
            "accentué en pèse deux, donc compter les caractères la sous-estime.")
    if inp.delivery == "init":
        # Un corps vide EFFACE la couche (la note se retire comme elle s'écrit) — là où
        # un guide on-demand vide n'aurait aucun sens (rien à charger).
        ident, slug = _init_ref(ctx, inp.scope, inp.slug, write=True,
                                owner_id=inp.owner_id)
        try:
            state = guide_store.set_init_guide(inp.scope, ident, body)
        except guide_store.GuideDeliveryConflict as e:
            raise AuthzDenied(409, "delivery_conflict", str(e))
        return _init_view(inp.scope, slug, state)
    owner_id = _owner_for_write(ctx, inp.scope, inp.owner_id)
    if not inp.slug:
        raise AuthzDenied(400, "missing_slug", "`slug` requis pour un guide on-demand.")
    if not body.strip():
        raise AuthzDenied(400, "missing_body", "`body_md` requis.")
    try:
        return guide_store.set_guide(inp.scope, owner_id, inp.slug, body,
                                     inp.title or "", inp.description or "")
    # ⚠️ AVANT `GuideError`, dont c'est une sous-classe : sans cet ordre le conflit de
    # livraison sortirait en 400 `invalid_guide` — « ta saisie est mal formée » — alors
    # que la saisie est valide et que c'est l'ÉTAT en place qui refuse.
    except guide_store.GuideDeliveryConflict as e:
        raise AuthzDenied(409, "delivery_conflict", str(e))
    except guide_store.GuideError as e:
        raise AuthzDenied(400, "invalid_guide", str(e))


def _delete(ctx: ResolvedCtx, inp: GuideRefInput) -> dict:
    if inp.delivery == "init":
        # Retirer une couche injectée = la vider (pas de ligne à supprimer : le rendu
        # omet déjà les couches vides).
        ident, slug = _init_ref(ctx, inp.scope, inp.slug, write=True,
                                owner_id=inp.owner_id)
        # ⚠️ Vider un readme, c'est l'ÉCRIRE vide : ce chemin passe par le même
        # upsert que `_set`, donc il rencontre le même conflit de livraison. Sans
        # cette branche il sortirait en 500 « erreur interne » — un refus prévu
        # rendu comme une panne, et l'appelant n'a alors rien à faire de la réponse.
        try:
            guide_store.set_init_guide(inp.scope, ident, "")
        except guide_store.GuideDeliveryConflict as e:
            raise AuthzDenied(409, "delivery_conflict", str(e))
        return {"scope": inp.scope, "slug": slug, "delivery": "init", "deleted": True}
    owner_id = _owner_for_write(ctx, inp.scope, inp.owner_id)
    if not inp.slug:
        raise AuthzDenied(400, "missing_slug", "`slug` requis pour un guide on-demand.")
    deleted = guide_store.delete_guide(inp.scope, owner_id, inp.slug)
    if not deleted:
        raise AuthzDenied(404, "not_found", f"Guide `{inp.slug}` (scope {inp.scope}) absent.")
    return {"scope": inp.scope, "slug": inp.slug, "deleted": True}


def _guide_op(ctx: ResolvedCtx, inp: GuideOpInput) -> dict:
    """Dispatch de la face MCP sur les MÊMES handlers que les faces REST."""
    if inp.op == "list":
        return _list(ctx, _NoInput())
    init = inp.delivery == "init"
    if not inp.slug and not init:
        # Un init a un slug canonique par scope — seul un on-demand doit être nommé.
        raise AuthzDenied(400, "missing_slug", "`slug` requis (cf. op=list).")
    if inp.op == "read":
        # Lecture on-demand : `scope` est un FILTRE optionnel (le store cherche dans
        # l'ordre de visibilité plateforme → org → user quand il est omis). Un init,
        # lui, se lit toujours à un scope donné (défaut : le sien).
        return _get(ctx, GuideRefInput(scope=inp.scope or ("user" if init else ""),
                                       slug=inp.slug or "", delivery=inp.delivery,
                                       owner_id=inp.owner_id))
    scope = inp.scope or "user"
    if inp.op == "delete":
        return _delete(ctx, GuideRefInput(scope=scope, slug=inp.slug or "",
                                          delivery=inp.delivery, owner_id=inp.owner_id))
    return _set(ctx, GuideSetInput(scope=scope, slug=inp.slug or "", delivery=inp.delivery,
                                   owner_id=inp.owner_id, body_md=inp.body_md or "",
                                   title=inp.title or "", description=inp.description or ""))


# Autz = SUB_ONLY (authentifié) + garde par SCOPE inline (platform_admin / org_admin / self) :
# le combinateur ne peut pas dériver l'org d'un champ `scope` libre.
# UNE capacité, deux faces (ADR 0042 §Convergence des surfaces) : `oto_guide` op-aware côté
# MCP + les routes `/api/me/guides…` côté dashboard, mêmes handlers. Jusqu'au 2026-07-28 la
# face MCP était un tool écrit à la main (`tools/guide.py`) qui redéclarait sa propre autz.
CAPABILITIES += [
    Capability(
        key="me.guide", handler=_guide_op, Input=GuideOpInput, authz=SUB_ONLY,
        description=(
            "Load or author oto INSTRUCTION PROSE — a how-to loaded when needed, or a readme "
            "injected into every session. Two axes: `scope` = platform | org | group | user "
            "(who writes it, whom it applies to) and `delivery` = 'on-demand' (default: a "
            "guide you load for a task) | 'init' (a readme concatenated into what you receive "
            "at handshake). op=list → the on-demand catalog you can see (platform ∪ your org ∪ "
            "your own) ; op=read (slug, optional scope) → its markdown body ; op=write / delete "
            "(slug, scope, body_md, title?, description?) → author for the PLATFORM (platform "
            "admin), your ORG (org admin), your TEAM (team lead) or YOURSELF (scope=user, the "
            "default). With delivery='init' the slug is canonical per scope — omit it (the "
            "user's own readme is `scope='user', delivery='init'`; an empty body clears that "
            "layer). Read the relevant guide BEFORE a non-trivial task (e.g. bulk-load). "
            "This is PROSE: a repeatable process with slots is a procedure (`oto_procedure`), "
            "and what oto knows about the user is their profile card (`oto_profile`)."),
        mcp="oto_guide",
    ),
    Capability(
        key="me.guides.list", handler=_list, Input=_NoInput, authz=SUB_ONLY, mcp=None,
        Output=GuideList,
        description="List the on-demand guides you can see (platform ∪ your org ∪ your own).",
        rest=RestBinding("GET", "/api/me/guides"),
    ),
    # Multi-binding : `/api/me/…` = le scope COURANT (org/équipe actives) ; `/api/orgs/{id}/…`
    # et `/api/groups/{id}/…` = une cible EXPLICITE — un écran qui gère une équipe donnée ne
    # doit pas dépendre de l'équipe « active » de la session (même métier, même autz dessous).
    Capability(
        key="me.guides.get", handler=_get, Input=GuideRefInput, authz=SUB_ONLY, mcp=None,
        Output=GuideView,
        description="Read one guide body by scope+slug (`?delivery=init` for a readme).",
        rest=(RestBinding("GET", "/api/me/guides/{scope}/{slug}"),
              RestBinding("GET", "/api/orgs/{id}/guides/{scope}/{slug}", {"id": "owner_id"}),
              RestBinding("GET", "/api/groups/{id}/guides/{scope}/{slug}", {"id": "owner_id"})),
    ),
    Capability(
        key="me.guides.set", handler=_set, Input=GuideSetInput, authz=SUB_ONLY, mcp=None,
        Output=GuideView,
        errors=(DeclaredError(400, "body_too_large",
                              "`body_md` dépasse 65 536 octets UTF-8"),
                DeclaredError(409, "delivery_conflict",
                              "ce `(scope, slug)` porte déjà une couche de l'AUTRE "
                              "livraison — un guide à charger ne remplace pas un "
                              "readme injecté, ni l'inverse ; rien n'est écrit"),),
        description=(
            "Create/update a guide (scope=platform|org|group|user|tenant). "
            "`delivery='init'` writes that scope's injected readme (empty body clears it). "
            "WHO MAY WRITE, per scope — checked on the REAL target, not the active one: "
            "`user` = yourself only; `org` = an admin of that org; `group` = that team's "
            "lead (org admins escalate); `platform` and `tenant` = a platform admin. "
            "⚠️ The flag to read before showing an editor is `can_edit` on the org or "
            "team view — NEVER `can_write_instructions`, which governs PROCEDURES and "
            "is true for any team MEMBER: reading it here shows an editor that the "
            "server refuses."),
        rest=(RestBinding("PUT", "/api/me/guides/{scope}/{slug}"),
              RestBinding("PUT", "/api/orgs/{id}/guides/{scope}/{slug}", {"id": "owner_id"}),
              RestBinding("PUT", "/api/groups/{id}/guides/{scope}/{slug}", {"id": "owner_id"})),
    ),
    Capability(
        key="me.guides.delete", handler=_delete, Input=GuideRefInput, authz=SUB_ONLY, mcp=None,
        Output=GuideDeleted,
        errors=(DeclaredError(409, "delivery_conflict",
                              "ce `(scope, slug)` porte une couche de l'AUTRE "
                              "livraison — vider un readme injecté ne retire pas un "
                              "guide à charger ; rien n'est écrit"),),
        description="Delete a guide (scope=platform|org|group|user).",
        rest=RestBinding("DELETE", "/api/me/guides/{scope}/{slug}"),
    ),
]
