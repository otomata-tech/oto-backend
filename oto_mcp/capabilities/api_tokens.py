"""Les JETONS API `oto_`, et les CLÉS PLATEFORME — deux surfaces qui émettent ou
détiennent un secret.

Neuf routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `GET|POST /api/me/tokens` + `DELETE …/{token_id}`              → MES jetons
- `GET|POST /api/admin/users/{sub}/tokens` + `DELETE …/{token_id}` → jetons émis POUR UN TIERS
- `GET|POST /api/admin/platform-keys` + `DELETE …/{provider}/{label}` → clés plateforme

⚠️ **Les six routes de jetons portent `allow_api_token=False`, et c'est LA raison pour
laquelle elles étaient restées écrites à la main** : `_rest_adapter` ne savait pas
exprimer ce cran. Un jeton `oto_` ne peut ni lister, ni créer, ni révoquer de jeton —
sinon une fuite s'auto-entretient : révoquer le jeton fuité ne suffit plus, l'attaquant
s'en est fait un second, non expirant. Le cran est un champ du BINDING
(`RestBinding.allow_api_token`), donc déclaré au même endroit que le chemin, et vérifié
par test sur les six.

**Une exception, le jeton ÉMETTEUR** (décision d'Alexis du 08/10/2026 : que les agents
n'aient plus besoin d'un humain à chaque clé, comme le jeton Cloudflare « API Tokens:
Edit »). Un jeton porté `{"issue": <plafond>}` atteint les trois routes de SES jetons
(`allow_issuer_token`), jamais celles du palier admin. Le plafond nomme des tableaux et
des projets, ou des orgs entières (`{"orgs": {"2": "write"}}`, même jour) : l'enfant
nomme alors ses tableaux et projets, qui doivent appartenir à l'org ou à l'une de ses
équipes. Ce qui tient le motif ci-dessus :
- il ne naît que d'une session humaine, et jamais sans échéance (`issuer_ttl_required`) ;
- il n'émet que des jetons À PORTÉE, inclus dans son plafond, ni émetteurs ni runner,
  échéancés au plus `_TTL_MAX_ENFANT` jours ;
- il ne liste et ne révoque que ses propres enfants ;
- un enfant meurt avec son parent, révoqué ou échu (`db.tokens`), et chaque enfant
  nomme son parent (`parent_id`), lisible dans la liste.

**Aucune face MCP** (`mcp=None`) sur les neuf. Pour les jetons : la garde ci-dessus
n'aurait aucun sens si un outil pouvait faire le même geste. Pour les clés plateforme :
`api_key` est un secret brut, il ne passe pas en argument d'outil.

⚠️ **Trois asymétries entre le palier membre et le palier admin, toutes conservées** —
elles sont servies telles quelles et « harmoniser » casserait un appelant :
- `POST /api/me/tokens` rend **201**, `POST /api/admin/users/{sub}/tokens` rend **200** ;
- le `DELETE` membre rend `{ok}`, l'admin rend `{ok, id}` ;
- seul le palier MEMBRE refuse un tableau que l'émetteur ne voit pas ; le palier admin
  n'a pas ce garde-fou (il émet pour quelqu'un d'autre, dont le catalogue n'est pas le
  sien).

⚠️ **Une QUATRIÈME asymétrie a été levée le 04/09/2026** (#514 lot d, décision d'Alexis
« expiration pour tout le monde) : `ttl_days` était accepté du seul palier admin et
ignoré du palier membre. Une personne qui voulait borner son propre jeton devait donc
passer par un opérateur — la précaution existait sans être à sa portée, ce qui est le
motif même de #514. Les deux paliers l'acceptent et le RENDENT désormais, avec la même
lecture tolérante (`_jours`, partagée) : c'est l'instant de la création qui est le seul
où une échéance est lisible, la liste ne rendant pas le secret et le porteur ne sachant
jamais de lui-même quand il cesse de fonctionner. Cette asymétrie-là ne « servait » rien
— contrairement aux trois ci-dessus, la lever ne casse aucun appelant : un champ jusque-là
ignoré devient un champ obéi, et personne ne s'appuyait sur le fait qu'il ne l'était pas.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from .. import (access, credentials_store, db, org_origin, org_store, ownership,
                providers, roles)
from ..auth import token_scopes
from ._authz import SUB_ONLY, SUPER_ADMIN
from ._refus_de_pose import REFUS_DE_VERIFICATION, refus_de_pose
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

# Le libellé d'un jeton n'existe que pour une chose : reconnaître ce jeton le jour où
# il faudra décider de le révoquer, des mois plus tard, sans se souvenir de l'avoir
# émis. La colonne est en `TEXT` — cette borne est un choix de surface, donc elle est
# PUBLIÉE dans le schéma servi et un dépassement est REFUSÉ, jamais raboté (oto#42,
# 4ᵉ règle : une coupe sur une écriture se refuse, la fin ne survit nulle part).
# ⚠️ 32 est court, et jusqu'au 03/09/2026 la coupe silencieuse EMPÊCHAIT de le
# découvrir : un libellé raboté n'a jamais produit de plainte, il a produit des listes
# de jetons qu'on ne sait plus distinguer. Un refus, lui, se signale — si la borne est
# mal calibrée, on l'apprendra maintenant.
_LABEL_MAX = 32
# Le motif d'une révocation (#523) : du texte libre, même régime que le libellé —
# une borne PUBLIÉE et un dépassement REFUSÉ, jamais raboté.
_REASON_MAX = 500

# L'échéance la plus longue qu'un jeton ÉMETTEUR peut donner à ce qu'il émet.
_TTL_MAX_ENFANT = 90

_ME = "/api/me/tokens"
_ADMIN = "/api/admin/users/{sub}/tokens"
_KEYS = "/api/admin/platform-keys"
_CIBLE = {"sub": "target_sub"}          # {sub} = le sub VISÉ, pas l'appelant


# --- Entrées ----------------------------------------------------------------

_INCLURE_REVOQUES = Field(
    False, description=("Rendre aussi les jetons RÉVOQUÉS (qui, quand, pourquoi). "
                        "Absent : seuls les jetons encore actifs."))
_MOTIF = Field(
    None, json_schema_extra={"maxLength": _REASON_MAX},
    description=("Pourquoi ce jeton est coupé — gardé avec la trace de la révocation "
                 "(qui, quand). Au plus 500 caractères, au-delà : 400 "
                 "`reason_too_long`, jamais une coupe."))


class TokenListInput(BaseModel):
    include_revoked: bool = _INCLURE_REVOQUES


class TokenCreateInput(BaseModel):
    label: Optional[str] = Field(
        None, json_schema_extra={"maxLength": _LABEL_MAX},
        description=("Comment reconnaître ce jeton plus tard — au plus 32 caractères, "
                     "au-delà : 400 `label_too_long`, jamais une coupe. Absent ⇒ 'cli'."))
    # Document de portée, validé par `token_scopes.parse` (jamais côté porteur). Absent
    # ⇒ jeton NON PORTÉ : il EST le sub, pleins pouvoirs. Sa forme est libre, elle est
    # décrite par `token_scopes` et refusée nommément si elle ne tient pas.
    scopes: Any = None
    # ADR/issue #514 lot (d), décision d'Alexis du 04/09/2026 « expiration pour tout le
    # monde » : le palier MEMBRE l'ignorait, seul l'admin l'acceptait. Un jeton émis
    # depuis le dashboard vivait donc éternellement, et la personne qui voulait le
    # borner devait passer par un opérateur — la précaution existait sans être à sa
    # portée, exactement le motif de cette issue.
    # ⚠️ MÊME tolérance que le palier admin, à dessein : nombre ou texte, et ce qui
    # n'est pas un entier positif écrit en chiffres vaut « pas d'échéance demandée »,
    # donc le plafond (`db.TTL_MAX_JOURS`). Durcir ici et pas là ferait deux contrats
    # pour un même geste.
    ttl_days: Union[int, str, None] = Field(
        None,
        description=("Nombre de jours avant échéance, au plus 90 (au-delà : 400 "
                     "`token_ttl_too_long`). Absent ou non numérique ⇒ 90 jours : "
                     "aucun jeton neuf n'est non-expirant. La réponse rend l'échéance "
                     "retenue."))


class TokenDeleteInput(BaseModel):
    # Texte, pas entier : la route rend `400 invalid_id`, pas le `invalid_input` de
    # pydantic. On convertit dans le handler pour garder le code servi.
    token_id: str = ""
    reason: Optional[str] = _MOTIF


class AdminTokenListInput(BaseModel):
    target_sub: str
    include_revoked: bool = _INCLURE_REVOQUES


class AdminTokenCreateInput(BaseModel):
    target_sub: str
    label: Optional[str] = Field(
        None, json_schema_extra={"maxLength": _LABEL_MAX},
        description=("Comment reconnaître ce jeton plus tard — au plus 32 caractères, "
                     "au-delà : 400 `label_too_long`, jamais une coupe. Absent ⇒ 'cli'."))
    # Accepté en nombre OU en texte, et IGNORÉ s'il n'est pas un entier positif écrit en
    # chiffres (`str(x).isdigit()`) : un `-1` ou un booléen donnent « pas d'échéance
    # demandée », donc le plafond de 90 jours (`db.TTL_MAX_JOURS`).
    ttl_days: Union[int, str, None] = None
    scopes: Any = None


class AdminTokenDeleteInput(BaseModel):
    target_sub: str
    token_id: str = ""
    reason: Optional[str] = _MOTIF


class PlatformKeyListInput(BaseModel):
    """Aucun paramètre."""


class PlatformKeyCreateInput(BaseModel):
    provider: str = ""
    label: str = ""
    # ⚠️ Les seuls secrets en CLAIR de ce module. Ils sont chiffrés au coffre et ne
    # ressortent jamais : la réponse ne porte que l'identité de la clé.
    api_key: str = ""                          # la clé seule (premier champ secret)
    fields: Optional[dict[str, str]] = None    # connecteurs multi-champs (forager…)


class PlatformKeyDeleteInput(BaseModel):
    provider: str
    label: str


# --- Sorties ----------------------------------------------------------------

class ApiToken(BaseModel):
    """Un jeton, SANS son secret : celui-ci n'est rendu qu'UNE FOIS, à la création.
    `scopes: null` = jeton non porté (pleins pouvoirs du sub). `expires_at: null` =
    jeton sans expiration. `revoked_at` non null = jeton RÉVOQUÉ (liste avec
    `include_revoked`) : `revoked_by` dit qui l'a coupé, `revoked_reason` pourquoi."""
    id: int
    label: Optional[str] = None
    created_at: Optional[Any] = None
    last_used_at: Optional[Any] = None
    expires_at: Optional[Any] = None
    scopes: Optional[dict] = None
    revoked_at: Optional[Any] = None
    revoked_by: Optional[str] = None
    revoked_reason: Optional[str] = None
    # Le jeton ÉMETTEUR qui a émis celui-ci ; `null` = émis par une session humaine.
    parent_id: Optional[int] = None


class ApiTokenList(BaseModel):
    tokens: list[ApiToken]


class ApiTokenCreated(BaseModel):
    """⚠️ **`token` est le secret en clair, rendu UNE SEULE FOIS.** Il n'est jamais
    relisible : il n'est stocké que haché. Un client qui ne le garde pas doit en émettre
    un autre. `scopes: null` = jeton non porté."""
    token: str
    label: Optional[str] = None
    scopes: Optional[dict] = None
    # L'échéance RETENUE, en jours (au plus `db.TTL_MAX_JOURS`, qui est aussi celle d'un
    # jeton demandé sans échéance). `null` ne sort plus d'une création depuis le
    # plafond ; le type reste pour ne pas changer le schéma servi. Rendu aux DEUX
    # paliers depuis #514 lot (d) : une
    # échéance qu'on pose sans la voir revenir ne se vérifie qu'en attendant qu'elle
    # tombe. C'est le seul instant où elle est lisible — la liste ne rend pas le secret,
    # et le porteur ne saura jamais de lui-même quand il cesse de fonctionner.
    ttl_days: Optional[int] = None


class AdminApiTokenCreated(ApiTokenCreated):
    """⚠️ Cette classe ne se distingue PLUS de sa mère depuis le 04/09/2026 (#514 lot
    d) : `ttl_days` est monté dans `ApiTokenCreated`, les deux paliers l'acceptant et
    le rendant. Elle est conservée telle quelle parce qu'elle NOMME la sortie admin
    dans le document servi — la fusionner renommerait un schéma que des intégrations
    lisent, pour aucun gain."""


class TokenDeleted(BaseModel):
    ok: bool


class AdminTokenDeleted(BaseModel):
    """Le palier admin rend l'id retiré, le palier membre non. Asymétrie historique."""
    ok: bool
    id: int


class PlatformKey(BaseModel):
    """L'IDENTITÉ d'une clé plateforme, jamais son secret : il n'est ni déchiffré ni
    rendu par cette surface."""
    provider: str
    label: Optional[str] = None
    set_at: Optional[Any] = None


class PlatformKeyList(BaseModel):
    platform_keys: list[PlatformKey]


class PlatformKeyCreated(BaseModel):
    provider: str
    label: str


class PlatformKeyDeleted(BaseModel):
    ok: bool
    provider: str
    label: str


# --- Helpers partagés -------------------------------------------------------

def _entier(brut: str) -> int:
    """`400 invalid_id` — le code servi, là où pydantic dirait `invalid_input`."""
    try:
        return int(brut)
    except (TypeError, ValueError):
        raise AuthzDenied(400, "invalid_id")


def _portee(brut: Any) -> Optional[dict]:
    try:
        return token_scopes.parse(brut)
    except token_scopes.ScopeError as e:
        raise AuthzDenied(400, "invalid_scopes", str(e))


def _jours(brut: Any) -> Optional[int]:
    """`ttl_days` tel que la surface l'accepte : nombre OU texte, et tout ce qui n'est
    pas un entier positif écrit en chiffres vaut « pas d'échéance demandée » (None) —
    que `_echeance_plafonnee` change ensuite en plafond.

    ⚠️ Extrait pour être PARTAGÉ par les deux paliers (#514 lot d) : la même règle
    vivait en une ligne dans le seul handler admin, et l'ouvrir au membre en la
    recopiant aurait fait deux lectures d'un même champ — elles auraient divergé au
    premier durcissement, et le contrat servi aurait dépendu du palier appelé.
    Cette tolérance est CONSERVÉE telle quelle, y compris pour `-1` ou un booléen qui
    donnent « pas d'expiration » : c'est le comportement déjà servi côté admin."""
    return int(brut) if isinstance(brut, (int, str)) and str(brut).isdigit() else None


def _echeance_plafonnee(ttl_days: Optional[int]) -> int:
    """L'échéance ÉCRITE d'un jeton neuf (`db.echeance_bornee`) : celle demandée, ou le
    plafond (`db.TTL_MAX_JOURS`) quand aucune ne l'est. Au-delà du plafond : 400
    `token_ttl_too_long`, jamais un rabotage — et c'est elle que la réponse rend, pour
    que le porteur sache quand son jeton cessera de fonctionner.

    Appelée APRÈS les gardes qui lisent l'absence d'échéance (`issuer_ttl_required`,
    `issued_token_ttl`) : un émetteur sans échéance reste refusé, il ne reçoit pas le
    plafond en silence."""
    try:
        return db.echeance_bornee(ttl_days)
    except db.EcheanceTropLointaine as e:
        raise AuthzDenied(400, e.code,
                          f"Un jeton vit au plus {db.TTL_MAX_JOURS} jours : demande "
                          f"`ttl_days` ≤ {db.TTL_MAX_JOURS}, ou omets-le pour "
                          f"{db.TTL_MAX_JOURS} jours.")


def _cible_connue(target_sub: str) -> str:
    if not db.get_user(target_sub):
        raise AuthzDenied(404, "unknown_user")
    return target_sub


def _libelle(brut: Optional[str]) -> str:
    """Le libellé tel qu'il sera ÉCRIT — c'est lui que les handlers rendent, pour que la
    réponse ne puisse pas décrire autre chose que la base. Jusqu'au 03/09/2026 elle
    rendait le brut de l'appelant tandis que la base recevait `strip()[:32]` : sur un
    libellé long, l'émetteur repartait avec la confirmation d'une valeur qui n'existait
    nulle part."""
    net = (brut or "cli").strip() or "cli"
    if len(net) > _LABEL_MAX:
        raise AuthzDenied(
            400, "label_too_long",
            f"`label` fait {len(net)} caractères pour {_LABEL_MAX} au plus. Il est "
            "refusé entier plutôt que raboté : une coupe emporterait sa fin, or c'est "
            "elle qui distingue ce jeton de ses voisins dans la liste où on décidera "
            "de le révoquer. Raccourcis-le en gardant ce qui le rend reconnaissable.")
    return net


def _motif(brut: Optional[str]) -> Optional[str]:
    """Le motif tel qu'il sera ÉCRIT : blanc ⇒ absent, trop long ⇒ refusé."""
    net = (brut or "").strip() or None
    if net is not None and len(net) > _REASON_MAX:
        raise AuthzDenied(
            400, "reason_too_long",
            f"`reason` fait {len(net)} caractères pour {_REASON_MAX} au plus. Il est "
            "refusé entier plutôt que raboté : c'est la trace de la révocation.")
    return net


# --- Handlers : MES jetons --------------------------------------------------

def _emetteur() -> Optional[tuple[int, dict]]:
    """`(id, plafond)` si la requête vient d'un jeton ÉMETTEUR, None si elle vient
    d'une session humaine. Tout autre jeton porté est refusé — fail-closed, même si
    l'authentification l'a déjà écarté : ici, le prendre pour un humain lui donnerait
    tous les jetons du compte."""
    em = token_scopes.emetteur()
    if em is None and token_scopes.current() is not None:
        raise AuthzDenied(403, "api_token_forbidden",
                          "Seul un jeton émetteur (portée `issue`) gère des jetons.")
    return em


def _echeance_d_emetteur(scopes: Optional[dict], ttl_days: Optional[int]) -> None:
    """Un jeton émetteur ne naît jamais sans échéance : c'est lui qui borne la durée
    de tous ses enfants."""
    if scopes and scopes.get(token_scopes.ISSUE) and not ttl_days:
        raise AuthzDenied(400, "issuer_ttl_required",
                          "Un jeton émetteur (portée `issue`) exige `ttl_days`.")


def _orgs_du_plafond(sub: str, scopes: Optional[dict]) -> None:
    """Un plafond d'org ne nomme qu'une org dont le porteur est membre : sinon il ne
    couvrirait rien, et l'émetteur paraîtrait branché sans pouvoir rien émettre."""
    orgs = ((scopes or {}).get(token_scopes.ISSUE) or {}).get(token_scopes.ORGS) or {}
    hors = sorted(o for o in orgs if not roles.is_org_member(sub, int(o)))
    if hors:
        raise AuthzDenied(400, "unknown_org",
                          f"Le porteur du jeton n'est pas membre de ces orgs : {hors}")


def _org_proprietaire(proprio: Optional[tuple], groupes: dict) -> Optional[int]:
    """L'org où vit un objet AU SENS DU PLAFOND D'ORG : celle qui le possède, ou l'org
    parente de l'équipe qui le possède. Un objet personnel n'en a aucune, même rangé
    dans une org (`context_org_id`) — décision d'Alexis du 08/10/2026 : un plafond
    d'org n'atteint jamais les objets personnels."""
    if proprio is None:
        return None
    if proprio[0] == "org":
        return int(proprio[1])
    if proprio[0] == "group":
        return groupes.get(int(proprio[1]))
    return None


def _orgs_des(scopes: dict) -> dict:
    """`{famille: {clé: org}}` pour `token_scopes.inclus`, lu sur le propriétaire de
    chaque tableau et projet que la portée nomme."""
    familles = {token_scopes.NAMESPACES: ownership.TYPE_RESSOURCE_DATASTORE,
                token_scopes.PROJECTS: "project"}
    proprios = {f: {cle: ownership.owner_of(rtype, cle) for cle in (scopes.get(f) or {})}
                for f, rtype in familles.items()}
    groupes = org_origin.group_orgs(
        [p for par_cle in proprios.values() for p in par_cle.values() if p])
    return {f: {cle: _org_proprietaire(p, groupes) for cle, p in par_cle.items()}
            for f, par_cle in proprios.items()}


def _borne_enfant(scopes: Optional[dict], ttl_days: Optional[int],
                  plafond: dict) -> None:
    """Ce qu'un jeton émetteur peut émettre : une portée incluse dans son plafond
    (donc ni pleins pouvoirs, ni émetteur, ni runner), échéancée au plus
    `_TTL_MAX_ENFANT` jours. L'org de chaque ressource ne se lit que si le plafond
    nomme des orgs."""
    orgs_des = (_orgs_des(scopes) if scopes and plafond.get(token_scopes.ORGS)
                else None)
    if not token_scopes.inclus(scopes, plafond, orgs_des):
        raise AuthzDenied(
            403, "scope_exceeds_issuer",
            f"Un jeton émetteur n'émet qu'une portée incluse dans son plafond "
            f"({plafond}) : tableaux et projets seulement, nommés par identifiant, "
            f"droit au plus égal. Sous un plafond d'org, chacun doit appartenir à "
            f"l'org ou à l'une de ses équipes — jamais un objet personnel.")
    if not ttl_days or ttl_days > _TTL_MAX_ENFANT:
        raise AuthzDenied(
            400, "issued_token_ttl",
            f"Un jeton émis par un jeton émetteur exige `ttl_days`, au plus "
            f"{_TTL_MAX_ENFANT}.")


def _my_list(ctx: ResolvedCtx, inp: TokenListInput) -> dict:
    em = _emetteur()
    return {"tokens": db.list_api_tokens(ctx.sub, include_revoked=inp.include_revoked,
                                         parent_id=em[0] if em else None)}


def _par_identifiant(sub: str, scopes: Optional[dict]) -> Optional[dict]:
    """La portée telle qu'elle est RANGÉE : chaque tableau sous son IDENTIFIANT
    (oto#158), résolu dans ce que `sub` — le porteur du jeton — voit dans son org
    active. Un nom est accepté à l'émission le temps du préavis
    (`deprecations.RETRAIT_NOM_DE_TABLEAU`) ; la réponse rend la portée rangée, donc
    l'identifiant, et c'est lui qu'un appelant doit envoyer désormais.

    ⚠️ La portée ne nomme pas forcément un tableau : `parse` rend légitimement
    `{"projects": …}` ou `{"runner": true}` seuls. Refuser un tableau que le porteur ne
    voit pas : le jeton ne peut de toute façon pas dépasser ses droits, mais une faute
    de frappe produirait un jeton muet qu'on croirait branché. Un nom que portent
    plusieurs tableaux visibles est refusé, jamais tranché."""
    if (scopes or {}).get(token_scopes.ISSUE):
        # Le plafond d'un émetteur se range comme une portée : par identifiant.
        scopes = {**scopes, token_scopes.ISSUE: _par_identifiant(
            sub, scopes[token_scopes.ISSUE])}
    vises = (scopes or {}).get("namespaces") or {}
    if not vises:
        return scopes
    from ..datastore.core import make_store
    visibles = make_store(sub).list_datastores()
    ids = {str(n["id"]) for n in visibles}
    par_nom: dict = {}
    for n in visibles:
        par_nom.setdefault(n["datastore"], set()).add(str(n["id"]))
    ranges: dict = {}
    inconnus, ambigus = [], []
    for cle, droit in vises.items():
        if cle.isdigit() and cle in ids:
            ranges[cle] = droit
        elif len(par_nom.get(cle, ())) == 1:
            ranges[next(iter(par_nom[cle]))] = droit
        elif par_nom.get(cle):
            ambigus.append(cle)
        else:
            inconnus.append(cle)
    if ambigus:
        raise AuthzDenied(400, "ambiguous_namespace",
                          f"Plusieurs tableaux visibles portent ces noms : {sorted(ambigus)}"
                          " — nomme-les par leur identifiant (`data_list_datastores`).")
    if inconnus:
        raise AuthzDenied(400, "unknown_namespace",
                          f"Tableaux inconnus dans l'org active : {sorted(inconnus)}")
    return {**scopes, "namespaces": ranges}


def _my_create(ctx: ResolvedCtx, inp: TokenCreateInput) -> dict:
    em = _emetteur()
    label = _libelle(inp.label)
    scopes = _par_identifiant(ctx.sub, _portee(inp.scopes))
    ttl_days = _jours(inp.ttl_days)
    if em:
        _borne_enfant(scopes, ttl_days, em[1])
    else:
        _echeance_d_emetteur(scopes, ttl_days)
        _orgs_du_plafond(ctx.sub, scopes)
    ttl_days = _echeance_plafonnee(ttl_days)
    # Ce qu'émet une clé d'org reste enfermé dans son org, comme elle (`org_api_keys`).
    org = org_store.org_du_compte_de_service(ctx.sub) if em else None
    token = db.create_api_token(ctx.sub, label=label, ttl_days=ttl_days, scopes=scopes,
                                parent_id=em[0] if em else None,
                                verrou_org=org is not None, verrou_org_id=org)
    return {"token": token, "label": label, "scopes": scopes, "ttl_days": ttl_days}


def _my_delete(ctx: ResolvedCtx, inp: TokenDeleteInput) -> dict:
    em = _emetteur()
    if not db.revoke_api_token(ctx.sub, _entier(inp.token_id), revoked_by=ctx.sub,
                               reason=_motif(inp.reason),
                               parent_id=em[0] if em else None):
        raise AuthzDenied(404, "unknown_token")
    return {"ok": True}


# --- Handlers : jetons émis POUR UN TIERS -----------------------------------

def _admin_list(ctx: ResolvedCtx, inp: AdminTokenListInput) -> dict:
    return {"tokens": db.list_api_tokens(_cible_connue(inp.target_sub),
                                         include_revoked=inp.include_revoked)}


def _admin_create(ctx: ResolvedCtx, inp: AdminTokenCreateInput) -> dict:
    cible = _cible_connue(inp.target_sub)
    label = _libelle(inp.label)
    ttl_days = _jours(inp.ttl_days)
    # Rangée dans ce que voit le PORTEUR du jeton, pas l'émetteur : c'est pour lui
    # que l'identifiant doit désigner le tableau.
    scopes = _par_identifiant(cible, _portee(inp.scopes))
    _echeance_d_emetteur(scopes, ttl_days)
    _orgs_du_plafond(cible, scopes)
    ttl_days = _echeance_plafonnee(ttl_days)
    token = db.create_api_token(cible, label=label, ttl_days=ttl_days,
                                scopes=scopes)
    return {"token": token, "label": label, "ttl_days": ttl_days, "scopes": scopes}


def _admin_delete(ctx: ResolvedCtx, inp: AdminTokenDeleteInput) -> dict:
    cible = inp.target_sub
    token_id = _entier(inp.token_id)
    if not db.revoke_api_token(cible, token_id, revoked_by=ctx.sub,
                               reason=_motif(inp.reason)):
        raise AuthzDenied(404, "unknown_token")
    return {"ok": True, "id": token_id}


# --- Handlers : clés plateforme (ADR 0044 §F) -------------------------------

def _keys_list(ctx: ResolvedCtx, inp: PlatformKeyListInput) -> dict:
    # Instances scope PLATFORM du coffre unifié (plus de table `platform_keys`). Le
    # secret n'est JAMAIS déchiffré/renvoyé — identité seulement.
    return {"platform_keys": credentials_store.list_platform_credentials()}


def _porte_une_cle_plateforme(provider: str) -> bool:
    """Un connecteur qui déclare le mode `platform` porte une clé plateforme même
    s'il n'est pas `keyed` (ex. `transcription`, multi-champs dont seul le secret
    est la clé) ; le coffre applique le même gate (`require_credential`)."""
    c = providers.REGISTRY.get(provider)
    return bool(c and c.credential_of is None and "platform" in c.auth_modes)


def _keys_create(ctx: ResolvedCtx, inp: PlatformKeyCreateInput) -> dict:
    provider = (inp.provider or "").strip()
    label = (inp.label or "").strip()
    api_key = (inp.api_key or "").strip()
    if provider not in db.KEY_PROVIDERS and not _porte_une_cle_plateforme(provider):
        raise AuthzDenied(400, "invalid_provider")
    if not label or not (api_key or inp.fields):
        raise AuthzDenied(400, "missing_fields")
    # La forme du coffre se décide À LA POSE, par la source unique des paliers partagés
    # (`preparer_pose`) : une clé seule d'un connecteur multi-champs y devient son premier
    # champ secret. Stockée brute, elle ne se relisait pas (`unpack_secret`).
    try:
        secret, meta_champs = credentials_store.preparer_pose(
            credentials_store.PLATFORM, label, provider, "", api_key or None, inp.fields)
    except ValueError as e:
        # Saisie refusée (400) ou non vérifiable chez le fournisseur (503).
        raise refus_de_pose(e)
    try:
        credentials_store.set_platform_key(label, provider, secret, set_by=ctx.sub,
                                           meta=meta_champs or None)
    except ValueError as e:
        raise AuthzDenied(400, "invalid_platform_provider", str(e))
    return {"provider": provider, "label": label}


def _keys_delete(ctx: ResolvedCtx, inp: PlatformKeyDeleteInput) -> dict:
    provider = (inp.provider or "").strip()
    label = (inp.label or "").strip()
    # Les grants de l'instance vivent sur SA ligne (`share_down`/`meta`) → ils partent
    # avec elle, pas d'orphelin.
    if not credentials_store.clear_credential(credentials_store.PLATFORM, label, provider):
        raise AuthzDenied(404, "unknown_key")
    return {"ok": True, "provider": provider, "label": label}


_D_LIST = ("Mes jetons API, sans leur secret — il n'est rendu qu'à la création et n'est "
           "stocké que haché. `scopes: null` = jeton non porté (pleins pouvoirs de mon "
           "compte). Les jetons révoqués n'y sont qu'avec `include_revoked`. Réservé à "
           "une session interactive, ou à un jeton émetteur (portée `issue`) qui n'y "
           "voit que ses propres enfants (`parent_id`).")
_D_CREATE = ("Émet un jeton API. ⚠️ Le secret n'est rendu QU'UNE FOIS. `scopes` le BORNE "
             "à des tableaux (par IDENTIFIANT) ou projets nommés — la forme à confier à "
             "une intégration tierce ; absent, le jeton a tous mes droits. Un nom de "
             "tableau est rangé sous son identifiant, que la réponse rend. Un tableau que "
             "je ne vois pas est refusé (`unknown_namespace`) : sinon le jeton serait "
             "muet et on le croirait branché. `{\"issue\": <plafond>}` fait un jeton "
             "ÉMETTEUR, qui émet à son tour sans session : `ttl_days` y est requis, et ce "
             "qu'il émet est une portée incluse dans le plafond (tableaux et projets), "
             "au plus 90 jours, révoquée avec lui. Tout jeton vit au plus 90 jours "
             "(`ttl_days`, `token_ttl_too_long` au-delà) ; sans `ttl_days`, 90 jours. "
             "Réservé à une session interactive, "
             "ou à un jeton émetteur.")
_D_DELETE = ("Révoque un de mes jetons : il cesse de fonctionner, et la révocation est "
             "GARDÉE (qui, quand, `reason` facultatif) — la liste la montre avec "
             "`include_revoked`. Un jeton déjà révoqué rend 404 `unknown_token`. Révoquer "
             "un jeton émetteur révoque ses enfants. Réservé à une session interactive, "
             "ou à un jeton émetteur pour ses seuls enfants — un autre jeton ne peut pas "
             "en révoquer, sinon un attaquant couperait les jetons légitimes.")
_D_A_LIST = ("Les jetons émis pour un compte TIERS ; les révoqués avec "
             "`include_revoked`. Réservé à une session interactive.")
_D_A_CREATE = ("Émet un jeton POUR UN COMPTE TIERS. ⚠️ Le secret n'est rendu qu'une "
               "fois. `ttl_days` borne sa durée de vie : au plus 90 jours "
               "(`token_ttl_too_long` au-delà), absent = 90 jours. Pas "
               "de contrôle de visibilité des tableaux ici : le catalogue visé n'est pas "
               "celui de l'émetteur. Réservé à une session interactive.")
_D_A_DELETE = ("Révoque un jeton d'un compte tiers, en gardant la trace (qui, quand, "
               "`reason` facultatif). Réservé à une session interactive.")
_D_K_LIST = ("Les clés PLATEFORME posées, sans leur secret : provider, libellé, date de "
             "pose. Le secret n'est ni déchiffré ni rendu par cette surface.")
_D_K_CREATE = ("Pose une clé plateforme. Elle est chiffrée au coffre et ne ressort "
               "jamais. `api_key` seule suffit : sur un connecteur multi-champs, elle "
               "remplit son premier champ secret ; les autres champs passent par "
               "`fields`. Refus nommés : `invalid_provider` (hors registre), "
               "`missing_fields`, `missing_credentials`, `invalid_field_value`, "
               "`invalid_platform_provider` ; une clé `anthropic` est essayée chez "
               "Anthropic avant l'écriture (`anthropic_workspace_required`, "
               "`anthropic_key_rejected` en 400, `anthropic_unreachable` en 503).")
_D_K_DELETE = ("Retire une clé plateforme. Ses grants vivent sur sa ligne : ils partent "
               "avec elle, sans orphelin.")

CAPABILITIES += [
    Capability(
        key="me.token.list", handler=_my_list, Input=TokenListInput, authz=SUB_ONLY,
        Output=ApiTokenList, description=_D_LIST, mcp=None,
        rest=RestBinding("GET", _ME, allow_api_token=False, allow_issuer_token=True),
    ),
    Capability(
        key="me.token.create", handler=_my_create, Input=TokenCreateInput,
        authz=SUB_ONLY, Output=ApiTokenCreated, description=_D_CREATE, mcp=None,
        # 201 : forme historique de CE palier — l'admin, lui, rend 200.
        rest=RestBinding("POST", _ME, status=201, allow_api_token=False,
                         allow_issuer_token=True),
    ),
    Capability(
        key="me.token.delete", handler=_my_delete, Input=TokenDeleteInput,
        authz=SUB_ONLY, Output=TokenDeleted, description=_D_DELETE, mcp=None,
        # `reads_body` : le motif (`{reason}`) voyage dans le corps du DELETE.
        rest=RestBinding("DELETE", _ME + "/{token_id}", allow_api_token=False,
                         allow_issuer_token=True, reads_body=True),
    ),
    Capability(
        key="platform.token.list", handler=_admin_list, Input=AdminTokenListInput,
        authz=SUPER_ADMIN, Output=ApiTokenList, description=_D_A_LIST, mcp=None,
        rest=RestBinding("GET", _ADMIN, path_map=_CIBLE, allow_api_token=False),
    ),
    Capability(
        key="platform.token.create", handler=_admin_create,
        Input=AdminTokenCreateInput, authz=SUPER_ADMIN, Output=AdminApiTokenCreated,
        description=_D_A_CREATE, mcp=None,
        rest=RestBinding("POST", _ADMIN, path_map=_CIBLE, allow_api_token=False),
    ),
    Capability(
        key="platform.token.delete", handler=_admin_delete,
        Input=AdminTokenDeleteInput, authz=SUPER_ADMIN, Output=AdminTokenDeleted,
        description=_D_A_DELETE, mcp=None,
        rest=RestBinding("DELETE", _ADMIN + "/{token_id}", path_map=_CIBLE,
                         allow_api_token=False, reads_body=True),
    ),
    Capability(
        key="platform.key.list", handler=_keys_list, Input=PlatformKeyListInput,
        authz=SUPER_ADMIN, Output=PlatformKeyList, description=_D_K_LIST, mcp=None,
        rest=RestBinding("GET", _KEYS),
    ),
    Capability(
        key="platform.key.create", handler=_keys_create, Input=PlatformKeyCreateInput,
        authz=SUPER_ADMIN, Output=PlatformKeyCreated, description=_D_K_CREATE,
        errors=REFUS_DE_VERIFICATION,
        mcp=None,   # `api_key` est un secret brut : jamais un argument d'outil
        rest=RestBinding("POST", _KEYS),
    ),
    Capability(
        key="platform.key.delete", handler=_keys_delete, Input=PlatformKeyDeleteInput,
        authz=SUPER_ADMIN, Output=PlatformKeyDeleted, description=_D_K_DELETE, mcp=None,
        rest=RestBinding("DELETE", _KEYS + "/{provider}/{label}"),
    ),
]
