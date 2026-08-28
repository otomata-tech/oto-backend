"""Capacités « instances de connecteur » (ADR 0038 §B, barreau 4) — PROJECTION
LECTURE du coffre existant (cas dégénéré) : chaque credential que la cascade de
résolution sait trouver (membre (sub, org) > groupes > org > clé plateforme
grantée/ouverte) est exposé comme une **instance possédée nommée**. Métadonnées
seulement — le secret n'est NI déchiffré NI renvoyé (lecteurs non-déchiffrants
uniquement : `list_credentials`, grants, `list_platform_keys_meta`). Zéro table
nouvelle, zéro chemin d'écriture, `resolve_credential`/`status_for` intouchés.

LOT L6 (blueprint ADR 0053-D9, R1 tranché le 27/08) : chaque instance projetée porte
désormais, en PLUS de son `ref`, l'identifiant STABLE de la table
`connector_instances` (`id = "inst:{n}"`). La projection reste ce qu'elle était — une
lecture du coffre, zéro déchiffrement, zéro écriture : le seul ajout est **une** requête
qui traduit les quadruplets de coffre en identifiants, fail-open. Rien d'autre ne lit
encore ces instances (ni la cascade, ni la résolution) ; `ref` reste la référence qu'on
repasse (les gardes de pose refusent `inst:` nommément, cf. `access.rbac`).

EXCLUSIONS (assumées, documentées) :
- résidus `entity_type='user'` (mounts oauth fédérés atlassian/folkmcp)
  — hors cascade de travail by design (ADR 0033) ;
- grants de compte #55 (`connector_account_grants` = pointeurs d'identité
  satellites, déjà servis par `oto_account_access`) — repliés en
  « instances partagées » au B5 ;
- identités distantes Unipile (`connector_identities` : les énumérer déchiffre
  la clé et appelle l'API distante) — la clé BYO elle-même EST listée comme
  instance membre.

LIMITES (documentées) :
- les `config_fields` packés DANS `secret_enc` (ex. `data_center` zoho posé via
  POST api-keys) ne sortent PAS (les lire = `unpack_secret` = déchiffrement) ;
  seule la part `meta` (publique) est projetée en `config`. La vraie table B5
  dépackera à l'écriture.
- pas de filtre activation/exposition (ADR 0031) : la résolution ne le fait pas
  non plus — l'instance existe même si le connecteur n'est pas exposé
  (divergence assumée avec `oto_connector op=list`). Au passage la projection liste
  ce que `status_for` ignore (grant d'org, free-tier) : elle est le miroir
  honnête de ce que la résolution trouverait.
- PAS de `wins`/`mode` (le gagnant reste dit par `status_for` — une seule
  vérité) : la préférence §C est portée par le TRI membre < groupe < org <
  plateforme. B6 rendra cet ordre littéral.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict

# Lecteurs NON-déchiffrants uniquement — jamais get_credential*, jamais
# list_platform_keys (qui déchiffre), jamais les formes appauvries
# list_org_secrets/list_group_secrets (elles écrasent account/meta/secret_kind).
from ... import access, credentials_store, db, group_store, instance_refs, providers
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

logger = logging.getLogger(__name__)

# Rang de proximité (§C) : la cascade relue comme niveaux, portée par le tri.
_LEVEL_RANK = {"member": 0, "group": 1, "org": 2, "platform": 3}


class ListInstancesInput(BaseModel):
    connector: Optional[str] = None      # filtre par type de connecteur
    level: Optional[Literal["member", "group", "org", "platform"]] = None


class InstanceOwner(BaseModel):
    """Propriétaire d'une instance. `type='user'` porte un sub, `group`/`org` un
    entier, `platform` **aucun id** (une clé plateforme est identifiée par son
    label, ADR 0044 §F) — d'où trois champs optionnels plutôt qu'un couple figé."""
    type: Literal["user", "group", "org", "platform"]
    # sub (user) ou id de groupe/org — ENTIER quand il vient du contexte, CHAÎNE
    # quand il est reconstruit depuis une ligne partagée (`entity_id`). Absent en
    # platform.
    id: Optional[Union[int, str]] = None
    label: Optional[str] = None             # nom du groupe, ou label de la clé plateforme


class ConnectorInstance(BaseModel):
    """Une instance = un connecteur × un paramétrage d'auth, projeté depuis le
    coffre (lecture seule). Métadonnées uniquement : le secret n'est NI déchiffré
    NI renvoyé.

    Deux familles derrière la même forme, et leurs clés diffèrent : une instance
    de COFFRE (member/group/org) porte `account`/`secret_kind`/`config`/`set_by` ;
    une instance PLATEFORME n'a aucune ligne de coffre, donc rien de tout ça —
    seulement `daily_quota` (grant) ou `set_at` (free-tier)."""
    model_config = ConfigDict(extra="allow")

    # L'IDENTIFIANT STABLE de l'instance (`inst:{id}`, lot L6) — la forme CIBLE,
    # servie EN PLUS de `ref` et destinée à le remplacer (lui et le `connectionId`
    # du shell). Ce que `ref` ne sait pas faire : survivre au renommage d'un compte
    # ou du label d'une clé plateforme (le ref composé projette la clé du coffre, qui
    # DÉPLACE la ligne au renommage), désigner une sous-instance, désigner une
    # instance sans secret.
    # ⚠️ **Peut être absent**, et le client doit le supporter : l'instance est nommée
    # au BOOT (backfill de `_init`), donc une clé posée depuis le dernier boot n'a pas
    # encore d'identifiant. Tant que la bascule n'est pas faite, `ref` reste la
    # référence à repasser (`_instance=`, bindings) — `id` est là pour être stocké et
    # préparé, pas encore pour être épinglé (les gardes de pose le refusent nommément).
    # Opaque comme `ref` : à repasser tel quel, jamais à parser.
    id: Optional[str] = None
    # Handle opaque et STABLE, cible d'un pin `_instance=`. Ne pas le parser.
    ref: str
    connector: str
    # Rang de PROXIMITÉ dans la cascade, qui porte le tri (membre < groupe < org <
    # plateforme). Ce n'est PAS le gagnant : la liste ne dit jamais qui résout —
    # une seule vérité pour ça, `status_for`.
    level: Literal["member", "group", "org", "platform"]
    owner: InstanceOwner
    # DÉRIVÉ, jamais stocké : `meta.label` > « Connecteur · compte » > « Connecteur ·
    # label de clé » > label du connecteur. Deux instances peuvent donc porter le
    # même nom ; `ref` est l'identité.
    name: str
    # Discrimine plusieurs instances du même connecteur au même niveau (multi-compte).
    # `""` = l'instance par défaut. Absent des instances plateforme.
    account: Optional[str] = None
    secret_kind: Optional[str] = None
    # Part PUBLIQUE du meta seulement. ⚠️ Les `config_fields` packés DANS le secret
    # (ex. `data_center` zoho) n'y sont PAS — les lire supposerait de déchiffrer.
    # Un `config` vide ne signifie donc pas « aucune configuration ».
    config: Optional[dict] = None
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    # D'OÙ vient l'instance : `credential` (le coffre, à ma portée), `user_grant` /
    # `org_grant` / `free_tier` (paliers plateforme), `shared_with_me` (prêt
    # nominatif d'un pair, ADR 0044 share_side — cross-org possible),
    # `personal_cross_org` (ma propre clé posée dans une AUTRE org, #172).
    via: str
    is_default: Optional[bool] = None       # présent (true) seulement si marqué défaut
    # Présent (true) seulement si l'instance est mise de côté : la cascade la SAUTE,
    # mais elle reste listée et réactivable — un `suspended` n'est pas une absence.
    suspended: Optional[bool] = None
    daily_quota: Optional[int] = None       # paliers plateforme grantés seulement


class ConnectorInstances(BaseModel):
    """Les instances visibles depuis l'org active, par proximité.

    Deux angles morts assumés, qui font qu'une absence ne prouve rien : la liste
    n'applique AUCUN filtre d'activation/exposition (une instance existe même si
    le connecteur n'est pas exposé — divergence voulue avec `connectors.me`), et
    ses sections « partagé avec moi » / « perso cross-org » sont fail-open loggé
    (un incident les rend vides sans erreur)."""
    instances: list[ConnectorInstance]
    count: int                              # = len(instances), APRÈS filtres


class SuspendInstanceResult(BaseModel):
    """Écho de la mise de côté (ou réactivation) de MA clé membre."""
    connector: str
    # `null` quand l'instance visée est celle sans compte nommé (le `""` de
    # l'entrée ressort en `null`) — pas « compte inconnu ».
    account: Optional[str] = None
    suspended: bool


def _connector_label(connector: str) -> str:
    c = providers.REGISTRY.get(connector)
    return c.label if (c is not None and c.label) else connector


def _instance_name(connector: str, meta_label, account: str = "",
                   key_label: str = "") -> str:
    """Nom d'instance DÉRIVÉ, déterministe (rien de stocké) : `meta.label` non
    vide > « Connecteur · compte » > « Connecteur · label de clé » (platform)
    > label du connecteur."""
    clabel = _connector_label(connector)
    if meta_label:
        return str(meta_label)
    if account:
        return f"{clabel} · {account}"
    if key_label:
        return f"{clabel} · {key_label}"
    return clabel


def _vault_key(entity_type: str, entity_id, connector: str, account: str) -> tuple:
    """La clé à quatre colonnes d'une ligne de coffre — le LIEN vers son instance
    (`connector_instances`), qui porte le même quadruplet. Convention du coffre :
    `account` vaut `''` en mono-compte, jamais None."""
    return (entity_type, str(entity_id), connector, account or "")


def _cred_instance(level: str, owner: dict, ref: str, row: dict,
                   vault_key: Optional[tuple] = None) -> dict:
    """Projette une ligne du coffre (forme `list_credentials`) en instance.
    `meta` sort déjà filtré `_public_meta` à la source ; on ré-applique
    `public_meta` en défense en profondeur (jamais un bearer vers le client).
    `config` = meta public MOINS les clés extraites en top-level."""
    meta = credentials_store.public_meta(row.get("meta"))
    account = row.get("account") or ""
    inst = {
        "ref": ref,
        "connector": row["connector"],
        "level": level,
        "owner": owner,
        "name": _instance_name(row["connector"], meta.get("label"), account),
        "account": account,
        "secret_kind": row.get("secret_kind"),
        "config": {k: v for k, v in meta.items() if k not in ("label", "is_default", "suspended")},
        "set_by": row.get("set_by"),
        "set_at": row.get("set_at"),
        "via": "credential",
        # Clé PRIVÉE, retirée avant sérialisation (`_stamp_instance_ids`) : elle ne
        # sert qu'à résoudre l'identifiant stable en UNE requête pour toute la liste.
        "_vault_key": vault_key,
    }
    if meta.get("is_default"):
        inst["is_default"] = True
    # État SUSPENDU (lot 2) : instance mise de côté → sautée par la cascade, mais
    # listée pour le KeyStack (« suspendue · Réactiver »).
    if meta.get("suspended"):
        inst["suspended"] = True
    return inst


def _platform_instance(provider: str, label: str, via: str, extra: dict) -> dict:
    """Instance « clé plateforme » (ADR 0044 §F : identifiée par (connector, label),
    plus de surrogate platform_key_id)."""
    return {
        "ref": instance_refs.make_platform_ref(provider, label),
        "connector": provider,
        "level": "platform",
        "owner": {"type": "platform", "label": label},
        "name": _instance_name(provider, None, key_label=label),
        "via": via,
        # ADR 0044 §F : une clé plateforme EST une ligne du coffre — `entity_type`
        # 'platform', `entity_id` = son label. Elle a donc une instance comme les
        # autres, y compris quand elle est atteinte par un grant ou le free-tier.
        "_vault_key": _vault_key(credentials_store.PLATFORM, label, provider, ""),
        **extra,
    }


def _hidden_connectors(sub: str, org: Optional[int]) -> set:
    """Filtre RBAC ADR 0025 — MIROIR de `access.require_connector_access` en mode
    filtre, via le seam unique `rbac_denied_connectors` (escalade super_admin +
    org_admin incluse). Même doctrine fail-open loggé que le pré-gate (gate de
    confort sur un listing ; la résolution réelle re-gate en dur à l'appel).
    Sans org active : pas de filtre."""
    try:
        return access.rbac_denied_connectors(sub, org)
    except Exception:
        logger.warning("instances: filtre RBAC indisponible (fail-open)",
                       exc_info=True)
        return set()


def _platform_eligible(provider: str) -> bool:
    """Le chemin plateforme de la cascade est gaté sur `auth_modes` (cf.
    `resolve_credential`) : un provider byo-only ne résout JAMAIS une clé
    plateforme — la projeter serait un mensonge (revue B4, finding major)."""
    con = providers.REGISTRY.get(provider)
    return con is not None and "platform" in con.auth_modes


def _shared_ref(entity_type: str, entity_id: str, connector: str,
                account: str) -> Optional[str]:
    """Ref PINNABLE d'une instance partagée avec moi (share_side), reconstruit depuis
    (entity_type, entity_id). None si non-pinnable (résidu oauth `user`, id malformé)."""
    if entity_type == "member":
        oid, _, osub = entity_id.partition(":")
        if not (oid.isdigit() and osub):
            return None
        return instance_refs.make_member_ref(int(oid), osub, connector, account)
    if entity_type == "group" and entity_id.isdigit():
        return instance_refs.make_group_ref(int(entity_id), connector, account)
    if entity_type == "org" and entity_id.isdigit():
        return instance_refs.make_org_ref(int(entity_id), connector, account)
    return None


def _shared_owner(entity_type: str, entity_id: str) -> dict:
    """Propriétaire (le PRÊTEUR) d'une instance partagée — pas moi."""
    if entity_type == "member":
        _, _, osub = entity_id.partition(":")
        return {"type": "user", "id": osub}
    return {"type": entity_type, "id": entity_id}


def _stamp_instance_ids(out: list[dict]) -> None:
    """Pose `id = inst:{id}` sur chaque instance qui en a un, et RETIRE la clé privée.

    **Une seule requête pour toute la liste** (`instance_ids_for_vault_rows`) : la
    projection tourne inline sur un serveur mono-loop, contre une base managée
    distante — un lookup par instance ferait N allers-retours sur une surface qui en
    rend des dizaines.

    **Fail-open loggé**, comme les sections « partagé avec moi » et « perso cross-org »
    de cette même liste : `id` est un identifiant d'affichage que rien ne consomme
    encore, `ref` reste servi et suffit. Faire tomber le listing des clés d'un
    utilisateur parce que la table des instances n'a pas répondu serait hors de
    proportion. La contrepartie est explicite dans le modèle de sortie : **`id` peut
    être absent** — d'une indisponibilité comme d'une instance pas encore nommée par
    un boot.

    ⚠️ La clé privée `_vault_key` est retirée dans TOUS les cas (le modèle de sortie
    est `extra="allow"` : ce qui reste dans le dict part sur le fil).
    """
    cles = [i.get("_vault_key") for i in out]
    for i in out:
        i.pop("_vault_key", None)
    try:
        ids = db.instance_ids_for_vault_rows([k for k in cles if k])
    except Exception:
        logger.warning("instances: identifiants stables indisponibles (fail-open)",
                       exc_info=True)
        return
    for inst, cle in zip(out, cles):
        iid = ids.get(cle) if cle else None
        if iid is not None:
            inst["id"] = instance_refs.make_instance_ref(iid)


def _list_instances(ctx: ResolvedCtx, inp: ListInstancesInput) -> dict:
    # Handler SYNC exécuté INLINE par les adaptateurs de capacité (pattern des
    # capacités existantes — pas de threadpool ici) : requêtes courtes indexées.
    sub, org = ctx.sub, ctx.org_id
    out: list[dict] = []

    if org is not None:
        # 1. MEMBRE — mes credentials dans CETTE org (ADR 0033 : jamais de repli
        # org-agnostique). Une ligne (connector, account) = une instance.
        member_eid = credentials_store.member_id(org, sub)
        for row in credentials_store.list_credentials(credentials_store.MEMBER,
                                                      member_eid):
            out.append(_cred_instance(
                "member", {"type": "user", "id": sub},
                instance_refs.make_member_ref(org, sub, row["connector"],
                                              row.get("account") or ""),
                row,
                _vault_key(credentials_store.MEMBER, member_eid, row["connector"],
                           row.get("account") or "")))

        # 2. GROUPES — les groupes que je peux LIRE (miroir de `can_read_group`,
        # la garde de résolution) : mes groupes, et TOUS les groupes de l'org pour
        # un org_admin (escalade roles.py — « un connecteur par département, vu au
        # niveau org » : l'admin voit et administre chaque instance départementale).
        # Depuis B3, `_group=` est un jeton d'appel → toute instance listée ici est
        # atteignable = visible au sens §C.
        from ... import roles as _roles
        try:
            org_admin = _roles.is_org_admin(sub, org)
        # noqa: SILENT — dette déclarée : énumération partielle rendue comme complète (#424, verdict C)
        except Exception:
            org_admin = False
        groups = (group_store.list_groups(org) if org_admin
                  else group_store.list_groups_for_user(sub, org))
        for g in groups:
            gid = g.get("group_id") or g.get("id")
            owner = {"type": "group", "id": gid, "label": g.get("name")}
            for row in credentials_store.list_credentials("group", str(gid)):
                out.append(_cred_instance(
                    "group", owner,
                    instance_refs.make_group_ref(gid, row["connector"],
                                                 row.get("account") or ""),
                    row,
                    _vault_key("group", gid, row["connector"],
                               row.get("account") or "")))

        # 3. ORG — secrets de l'org active, visibles de tout membre (précédent :
        # la fiche org les liste déjà aux membres).
        for row in credentials_store.list_credentials("org", str(org)):
            out.append(_cred_instance(
                "org", {"type": "org", "id": org},
                instance_refs.make_org_ref(org, row["connector"],
                                           row.get("account") or ""),
                row,
                _vault_key("org", org, row["connector"],
                           row.get("account") or "")))

    # 4. PLATEFORME — grants user + org + free-tier. La cascade ne résout qu'UNE
    # clé plateforme par provider (user_grant > org_grant > free_tier) → dédup
    # par PROVIDER (ordre d'insertion = priorité ; dédup par clé listait un
    # free-tier fantôme après rotation, revue B4). Gate `auth_modes` miroir de
    # la résolution : un provider byo-only ne projette aucun palier plateforme.
    seen_providers: set = set()
    for gr in db.list_grants_for_user(sub):
        if gr["provider"] in seen_providers or not _platform_eligible(gr["provider"]):
            continue
        seen_providers.add(gr["provider"])
        out.append(_platform_instance(
            gr["provider"], gr.get("label") or "", "user_grant",
            {"daily_quota": gr.get("daily_quota")}))
    if org is not None:
        for gr in db.list_org_grants(org):
            if (gr["provider"] in seen_providers
                    or not _platform_eligible(gr["provider"])):
                continue
            seen_providers.add(gr["provider"])
            out.append(_platform_instance(
                gr["provider"], gr.get("label") or "", "org_grant",
                {"daily_quota": gr.get("daily_quota")}))
    # Free-tier ADR 0031 : clé la plus récente de chaque provider `platform_key_open`,
    # utilisable sans grant (ADR 0044 §F : instances scope PLATFORM du coffre unifié,
    # triées set_at DESC → 1re rencontrée par provider = la plus récente).
    last_open: dict[str, dict] = {}
    for k in credentials_store.list_platform_credentials():
        con = providers.REGISTRY.get(k["provider"])
        if con is not None and con.platform_key_open and k["provider"] not in last_open:
            last_open[k["provider"]] = k
    for k in last_open.values():
        if k["provider"] in seen_providers or not _platform_eligible(k["provider"]):
            continue
        seen_providers.add(k["provider"])
        out.append(_platform_instance(
            k["provider"], k.get("label") or "", "free_tier",
            {"set_at": k.get("set_at")}))

    # 5. PARTAGÉ AVEC MOI (ADR 0044 share_side) : instances d'AUTRES dont le
    # share_side me vise (nominatif `user:` ou via un de mes groupes). Cross-org
    # possible (le prêt nominatif = consentement). Le pin résout la clé de l'owner ;
    # `require_connector_access` re-gate MON org à l'appel (le filtre RBAC ci-dessous
    # le reflète déjà). Dédup par ref (une instance de groupe déjà listée en §2 ne
    # réapparaît pas).
    my_scopes = [f"user:{sub}"]
    if org is not None:
        for g in group_store.list_groups_for_user(sub, org):
            my_scopes.append(f"group:{g.get('group_id') or g.get('id')}")
    my_eids = {credentials_store.member_id(org, sub)} if org is not None else set()
    existing_refs = {i["ref"] for i in out}
    try:
        shared = credentials_store.list_shared_with(my_scopes)
    except Exception:
        logger.warning("instances: 'partagé avec moi' indisponible (fail-open)", exc_info=True)
        shared = []
    for row in shared:
        et, eid = row["entity_type"], row["entity_id"]
        if et == "member" and eid in my_eids:
            continue  # défensif : jamais ma propre ligne
        ref = _shared_ref(et, eid, row["connector"], row.get("account") or "")
        if ref is None or ref in existing_refs:
            continue
        existing_refs.add(ref)
        inst = _cred_instance(et, _shared_owner(et, eid), ref, row,
                              _vault_key(et, eid, row["connector"],
                                         row.get("account") or ""))
        inst["via"] = "shared_with_me"
        out.append(inst)

    # 6. PERSONNELLES CROSS-ORG (issue #172, piste A) : mes instances membre d'un
    # connecteur PAR-PERSONNE (unipile) posées dans une AUTRE org me suivent — la
    # résolution de proximité les trouve depuis n'importe quelle org (cf.
    # `access.personal_instance_org`), donc la liste DOIT les montrer, sinon le manque
    # #1 (« rien ne signale que j'ai déjà une instance perso ailleurs » → on reconnecte
    # → doublon). Pinnable (`guard_instance_access` : ma propre ligne, org où je suis
    # membre). Dédup par ref (déjà listée si l'org de contexte EST l'org porteuse).
    try:
        for provider in providers.PERSONAL_CROSS_ORG_PROVIDERS:
            # Un connecteur qui DÉLÈGUE son credential n'a aucune ligne au coffre :
            # son instance perso est celle de son porteur, déjà listée sous lui (et
            # pinnable pour ses appels — le pin `_instance=` se compare au porteur,
            # cf. access/resolve.py). Le sonder coûterait une requête par canal pour
            # une liste toujours vide.
            if providers.delegates_credential(provider):
                continue
            for other_org in credentials_store.list_member_orgs_for(sub, provider):
                if org is not None and other_org == org:
                    continue
                for row in credentials_store.list_credentials(
                        credentials_store.MEMBER,
                        credentials_store.member_id(other_org, sub)):
                    if row["connector"] != provider:
                        continue
                    ref = instance_refs.make_member_ref(other_org, sub, provider,
                                                        row.get("account") or "")
                    if ref in existing_refs:
                        continue
                    existing_refs.add(ref)
                    inst = _cred_instance(
                        "member", {"type": "user", "id": sub}, ref, row,
                        _vault_key(credentials_store.MEMBER,
                                   credentials_store.member_id(other_org, sub),
                                   provider, row.get("account") or ""))
                    inst["via"] = "personal_cross_org"
                    out.append(inst)
    except Exception:
        logger.warning("instances: 'perso cross-org' indisponible (fail-open)",
                       exc_info=True)

    # Filtre RBAC (ADR 0025, fail-open loggé) puis filtres d'input.
    hidden = _hidden_connectors(sub, org)
    if hidden:
        out = [i for i in out if i["connector"] not in hidden]
    if inp.connector:
        out = [i for i in out if i["connector"] == inp.connector]
    if inp.level:
        out = [i for i in out if i["level"] == inp.level]

    # Préférence §C portée par le TRI : membre < groupe < org < plateforme.
    out.sort(key=lambda i: (i["connector"], _LEVEL_RANK[i["level"]],
                            i.get("account") or ""))
    # L'identifiant stable, EN DERNIER : après les filtres, pour ne résoudre que ce
    # qui sort, et parce que la clé privée doit disparaître de TOUT ce qui sort.
    _stamp_instance_ids(out)
    return {"instances": out, "count": len(out)}


CAPABILITIES += [
    Capability(
        key="connectors.instances.list",
        handler=_list_instances,
        Input=ListInstancesInput,
        Output=ConnectorInstances,
        authz=SUB_ONLY,   # org_id injecté du seam acteur, jamais d'un param client
        description=(
            "List the connector INSTANCES (connector x auth/config) visible to you in the active "
            "org, by proximity: yours (member), your groups', the org's, then platform grants. "
            "Metadata only — the secret is never returned. `id` (`inst:<n>`) is the stable "
            "identifier of the instance and will replace `ref`; it may be missing on a key set "
            "since the last boot, so keep using `ref` as the pin handle for now. Both are "
            "opaque: pass them back as-is. Contrast with oto_identity (operable accounts of ONE "
            "connector) and oto_connector op=list (catalog of TYPES)."),
        rest=RestBinding("GET", "/api/me/connector-instances"),
    ),
]


class SuspendInstanceInput(BaseModel):
    connector: str                       # type de connecteur (ex. "unipile")
    account: str = ""                    # discrimine si plusieurs instances membre
    suspended: bool = True               # False = réactiver


def _suspend_instance(ctx: ResolvedCtx, inp: SuspendInstanceInput) -> dict:
    """Suspend / réactive TA clé membre du connecteur dans l'org active (lot 2).
    Une instance suspendue est SAUTÉE par la résolution de credential (le barreau
    du dessous — groupe/org/plateforme — prend le relais), mais reste listée et
    réactivable (`suspended=False`). N'écrit QUE `meta.suspended` — le secret n'est
    jamais touché ni lu. Réservé à TA propre clé (SUB_ONLY, org du seam acteur)."""
    sub, org = ctx.sub, ctx.org_id
    if org is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active pour résoudre l'instance.")
    ok = credentials_store.update_meta(
        credentials_store.MEMBER, credentials_store.member_id(org, sub),
        inp.connector, inp.account, {"suspended": bool(inp.suspended)})
    if not ok:
        raise AuthzDenied(404, "no_instance",
                          f"Aucune clé membre '{inp.connector}' à suspendre.")
    return {"connector": inp.connector, "account": inp.account or None,
            "suspended": bool(inp.suspended)}


CAPABILITIES += [
    Capability(
        key="connectors.instances.suspend",
        handler=_suspend_instance,
        Input=SuspendInstanceInput,
        Output=SuspendInstanceResult,
        authz=SUB_ONLY,   # ta propre clé, org du seam acteur — jamais un tiers
        description=(
            "Suspend or reactivate YOUR OWN member key for a connector in the active "
            "org. A suspended instance is SKIPPED by credential resolution (the next "
            "level down — group/org/platform — takes over) but stays listed and "
            "reactivable (`suspended=false`). Only meta.suspended is written; the "
            "secret is never touched."),
        rest=RestBinding("POST", "/api/me/connector-instances/suspend"),
    ),
]
