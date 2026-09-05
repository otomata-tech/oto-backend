"""Qui a le DROIT — la gouvernance d'accès, hors résolution (ADR 0025/0012/0044).

Quatre familles, toutes des GARDES ou des ÉNUMÉRATIONS, jamais une résolution :

- **RBAC connecteur** par org (0025) et par équipe (0012 B2) + le backstop
  call-time `require_connector_access` ;
- **visibilité de tools** masqués par l'org_admin ou le chef d'équipe (0031) ;
- **partage d'instance** : la garde de niveau d'une instance épinglée (0038 B6)
  et les prêts nominatifs `share_side` (0044) ;
- **instances à portée** : ce qu'un jeton d'appel atteindrait légitimement, pour
  que l'erreur « rien ne résout » remonte les choix au lieu d'un refus sec.

S'y ajoute `resolve_field_filter` : la politique de REDACTION de l'org active —
même nature (ce que l'org gouverne s'applique à l'acteur), autre surface (les
champs de la réponse plutôt que l'accès au connecteur).

Dépend de `scope` (rôle, contexte, appartenance) et de `cascade` (la liste des
connecteurs org-partageables). Ne dépend PAS de la résolution : c'est elle qui
appelle ces gardes.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import credentials_store, db, group_store, org_store, providers
from ..auth.hooks import current_user_sub_from_token
from . import cascade, scope

logger = logging.getLogger(__name__)


class ConnectorAccessDenied(McpError):
    """Le connecteur est réservé par une ACL, indépendamment de la clé disponible."""


def rbac_denied_connectors(sub: str, org: Optional[int]) -> set:
    """Connecteurs REFUSÉS à `sub` dans `org` par le RBAC interne (ADR 0025) — seam
    UNIQUE des 4 surfaces (call-time `require_connector_access`, visibilité session,
    listing d'instances, marketplace). Escalade descendante alignée sur `roles.py` :
    super_admin ET **org_admin de l'org** transcendent la restriction — l'admin
    gouverne l'ACL (`org_connector_access`), lui en interdire l'USAGE était une
    incohérence (un connecteur réservé à une équipe restait inaccessible — et même
    invisible — à l'admin de l'org). LÈVE sur hoquet DB : chaque surface garde sa
    propre règle fail-open (le call-time logue, les listings best-effort)."""
    if org is None:
        return set()
    if scope.is_super_admin(sub):
        return set()
    from .. import roles
    if roles.is_org_admin(sub, org):
        return set()
    restricted = db.org_restricted_connectors(org)
    if not restricted:
        return set()
    return set(restricted) - set(db.member_allowed_connectors(sub, org))


def group_rbac_denied_connectors(sub: str, group: Optional[int]) -> set:
    """Connecteurs REFUSÉS à `sub` par le RBAC d'ÉQUIPE (ADR 0012 B2) — mirror de
    `rbac_denied_connectors` au grain équipe, NARROWING de l'org (une équipe réserve
    un connecteur à un sous-ensemble de SES membres ; elle ne peut que restreindre
    davantage). Bypass descendant (roles.py) : super_admin, org_admin de l'org parente
    ET group_admin (chef) de l'équipe transcendent — celui qui gouverne l'ACL n'en est
    jamais victime. LÈVE sur hoquet DB (le call-time logue, fail-open par palier)."""
    if group is None:
        return set()
    if scope.is_super_admin(sub):
        return set()
    from .. import roles
    if roles.can_admin_group(sub, group):   # chef d'équipe OU org_admin parent (escalade)
        return set()
    restricted = db.group_restricted_connectors(group)
    if not restricted:
        return set()
    return set(restricted) - set(db.group_member_allowed_connectors(sub, group))


def org_admin_hidden_tools(org: Optional[int]) -> set:
    """Tools masqués PAR DÉFAUT pour `org` (denylist posé par l'org_admin) —
    gouvernance de visibilité, PAS une barrière de sécurité (ADR 0031, même esprit
    que `tool_visibility.DEFAULT_HIDDEN_TOOLS`) : un override perso positif
    (`user_enabled_tools`) le lève toujours. Pas d'escalade à exempter — même un
    org_admin qui a masqué le tool le voit masqué, et se le réactive lui-même
    comme n'importe qui (cohérent avec DEFAULT_HIDDEN_TOOLS aujourd'hui). LÈVE sur
    hoquet DB : chaque surface (session_visibility, oto_list_my_tools) garde sa
    propre règle fail-open, indépendante du palier équipe."""
    if org is None:
        return set()
    return set(db.list_org_disabled_tools(org))


def group_admin_hidden_tools(group: Optional[int]) -> set:
    """Mirror au grain ÉQUIPE — un chef d'équipe masque un tool pour SON équipe.
    Additif pur (l'appelant UNIT ce résultat avec `org_admin_hidden_tools`) : ce
    seam n'exprime jamais une levée, une équipe ne peut donc jamais révéler un tool
    que l'org a masqué. LÈVE sur hoquet DB (fail-open par palier, à la charge de
    l'appelant)."""
    if group is None:
        return set()
    return set(db.list_group_disabled_tools(group))


def require_connector_access(provider: str, sub: Optional[str] = None) -> None:
    """Backstop call-time du RBAC connecteur (ADR 0025 org + 0012 B2 équipe) : si
    `provider` est RESTREINT dans l'org active OU dans l'ÉQUIPE active du `sub` et qu'il
    n'y est pas autorisé, lève. **DUR** — appelé dans `resolve_credential` (couvre keyed
    + fields + BYO : pas de clé perso qui contourne). Bypass par palier (super_admin /
    org_admin / group_admin — escalade des deux seams) ; pas d'org/équipe active →
    restriction non applicable ; stdio local (sub=None) = accès complet. L'équipe ne peut
    que RESTREINDRE davantage (le verdict est un OR org|équipe — monotone). Fail-open
    INDÉPENDANT par palier : un hoquet de la DB d'équipe ne désactive pas l'org."""
    sub = sub or current_user_sub_from_token()
    if sub is None:
        return
    denied = False
    try:
        denied = provider in rbac_denied_connectors(sub, scope.current_org(sub))
    except Exception as e:
        logger.warning("require_connector_access org fail-open %s/%s: %s", sub, provider, e)
    try:
        denied = denied or provider in group_rbac_denied_connectors(sub, scope.current_group(sub))
    except Exception as e:
        logger.warning("require_connector_access group fail-open %s/%s: %s", sub, provider, e)
    if denied:
        raise ConnectorAccessDenied(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Le connecteur `{provider}` est réservé à certaines équipes/personnes "
                f"de ton organisation. Demande l'accès à un admin de ton org (ou de ton équipe)."
            ),
        ))


def _instance_side_shares_safe(entity_type: str, entity_id: str, provider: str,
                               account: str = "") -> list:
    """`share_side` (prêts nominatifs) d'une instance, RÉSILIENT : sur hoquet DB →
    `[]` + warning = **fail-CLOSED** (aucun prêt accordé sans preuve). En prod ce
    chemin n'est atteint qu'après une lecture de clé réussie (même DB) — le fail-safe
    ne mord donc qu'aux tests unitaires sans DB. (Le cran `share_down` BYO a été
    retiré : une instance BYO est utilisable par tout le sous-arbre de son owner,
    restreindre = la poser au bon niveau. `share_down` ne vit plus que sur les
    instances PLATFORM, comme liste de grantees — `_platform_instance_usable`.)"""
    try:
        _, side = credentials_store.get_instance_sharing(entity_type, entity_id, provider, account)
        return side
    except Exception as e:
        logger.warning("instance_sharing fail-safe %s:%s/%s: %s", entity_type, entity_id, provider, e)
        return []


def guard_instance_access(sub: str, ref) -> Optional[int]:
    """Garde d'accès à une instance de connecteur par NIVEAU (ADR 0038 B6) — même
    sémantique que la projection B4 : member = MA ligne dans une org où je suis
    membre ; group = groupe dont je suis lecteur ; org = org dont je suis membre ;
    platform = refusé (le grant se résout déjà en dernier palier). Renvoie l'org de
    l'instance (à co-poser). McpError actionnable sinon. Chemin DB sync — appelants
    inbound chauds : threadpool. Partagée par l'axe `_instance=` (pose) et la
    résolution d'un binding de projet (re-garde pour l'APPELANT, qui n'est pas
    forcément celui qui a bindé)."""
    from .. import group_store, roles
    # Lot L6 : `parse_ref` accepte désormais l'identifiant stable `inst:{id}` — mais
    # RIEN ne le résout encore (la résolution par identifiant est L7). Sans cette
    # branche, un `inst:` tomberait dans le refus final et s'entendrait dire que
    # « les refs platform: ne s'épinglent pas » : un message faux est pire qu'un
    # refus, il envoie chercher au mauvais endroit.
    if ref.level == "inst":
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("L'identifiant d'instance `inst:` n'est pas encore épinglable : "
                     "repasse le `ref` rendu par oto_instance(op='list').")))
    if ref.level == "member":
        if ref.sub == sub:                       # owner : ma propre instance
            if not roles.is_org_member(sub, ref.org_id):
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=f"Instance refusée : tu n'es plus membre de l'org #{ref.org_id}."))
            return ref.org_id
        # Prêt à un pair (share_side, ADR 0044) : instance d'un AUTRE membre, autorisée
        # ssi `sub` est nommé dans son share_side. On EMPRUNTE la clé mais on garde le
        # contexte de l'APPELANT → co-pose SON org (pas celle de l'owner ; cross-org OK,
        # le prêt nominatif EST le consentement). Pin explicite → refus DUR si non prêté.
        side = _instance_side_shares_safe(
            credentials_store.MEMBER, credentials_store.member_id(ref.org_id, ref.sub),
            ref.connector, ref.account)
        if scope._sub_matches_scopes(sub, side):
            return scope.current_org(sub)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("Instance refusée : elle appartient à un autre membre et ne t'est "
                     "pas prêtée (share_side).")))
    if ref.level == "group":
        # Lecteur du groupe = membre OU admin de l'org (escalade `can_read_group`,
        # roles.py) — c'est le chemin par lequel un org_admin utilise l'instance
        # d'une équipe de son org (pin `_instance=` / binding projet).
        if not roles.can_read_group(sub, ref.group_id):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Instance refusée : tu n'es pas membre du groupe #{ref.group_id}."))
        g = group_store.get_group(ref.group_id)
        return g.get("org_id") if g else None
    if ref.level == "org":
        if not roles.is_org_member(sub, ref.org_id):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Instance refusée : tu n'es pas membre de l'org #{ref.org_id}."))
        return ref.org_id
    if ref.level == "tenant":
        # L-clés PR 1 : la clé d'un tenant s'épingle par ses comptes et eux seuls — le
        # tenant se lit sur le sub qualifié (`rung_tenant`), jamais sur l'org. Le
        # contexte reste celui de l'APPELANT (la clé ne porte pas d'org).
        from .. import tenant_vault
        if tenant_vault.rung_tenant(sub) != ref.tenant:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(f"Instance refusée : cette clé appartient au tenant "
                         f"`{ref.tenant}`, et ton compte n'en relève pas.")))
        return scope.current_org(sub)
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message="Les refs `platform:` ne s'épinglent pas (le grant plateforme se "
                "résout déjà tout seul en dernier palier)."))


def reachable_instances(sub: str, org: Optional[int], provider: str) -> list[dict]:
    """Instances `provider` utilisables dans un AUTRE contexte que l'ambiant :
    équipes de `org` dont `sub` est MEMBRE (secret présent, équipe pas forcément
    active) + ses AUTRES orgs (clé d'org partagée, ou sa clé membre là-bas). La
    cascade ne les lit pas — mais un jeton d'appel (`_group=`/`_org=`/`_instance=`)
    les atteint légitimement (mêmes gardes d'appartenance). Nourrit l'erreur
    « rien ne résout » : on REMONTE les choix pour que l'agent pinne explicitement,
    jamais de choix silencieux entre identités. Best-effort : ne lève jamais,
    renvoie ce qui a pu être énuméré (vécu Zoho/un client 2026-07-16 : clé sur
    l'équipe sales, 3 membres, 0 actif → « pas de clé » sec et session perdue).

    `provider` est normalisé vers le PORTEUR du credential (délégation) : les
    instances d'un canal unipile SONT celles du compte, il n'en existe pas d'autres.
    Sans ça, la carte d'un canal perdait le signal « une équipe a la clé » — et pour
    un connecteur par-personne, c'est le signal qui évite de reconnecter un compte
    déjà lié ailleurs (le doublon d'`account_id` de #172)."""
    provider = providers.credential_provider(provider)
    out: list[dict] = []
    shareable = provider in cascade.ORG_SHAREABLE_PROVIDERS
    try:
        if org is not None and shareable:
            seen_gids: set = set()
            for g in group_store.list_groups_for_user(sub, org):
                if group_store.has_group_secret(g["group_id"], provider):
                    out.append({"kind": "group", "id": g["group_id"],
                                "name": g["name"]})
                    seen_gids.add(g["group_id"])
            # #218 : l'org_admin GOUVERNE ses équipes sans en être MEMBRE — sa clé de
            # groupe lui est accessible par escalade (group=/instance= re-gardés par
            # can_read_group). Le hint était aveugle là (list_groups_for_user = membre
            # STRICT) → « pas de clé » sec alors que la clé existe sur une équipe qu'il
            # gouverne. On complète par les équipes de l'org visibles par escalade.
            from .. import roles
            try:  # escalade best-effort ISOLÉE : ne doit pas abîmer l'énumération
                if roles.is_org_admin(sub, org):  # ci-dessous (orgs) si elle hoquette.
                    for g in group_store.list_groups(org):
                        gid = g["id"]
                        if gid not in seen_gids and group_store.has_group_secret(gid, provider):
                            out.append({"kind": "group", "id": gid, "name": g["name"]})
                            seen_gids.add(gid)
            # noqa: SILENT — fail-open de visibilité, backstop dur au call-time
            except Exception:
                pass
        for o in org_store.list_orgs_for_user(sub):
            oid = o["org_id"]
            if oid == org:
                continue
            if ((shareable and org_store.has_org_secret(oid, provider))
                    or db.has_member_api_key(sub, oid, provider)):
                out.append({"kind": "org", "id": oid,
                            "name": o.get("name") or f"org {oid}"})
    # noqa: SILENT — fail-open de visibilité, backstop dur au call-time
    except Exception:
        return out
    return out


def reachable_team_key(sub: str, org: Optional[int], provider: str,
                       groups: "Optional[list[dict]]" = None,
                       secrets_by_group: "Optional[dict]" = None) -> Optional[dict]:
    """Première équipe de `org` dont `sub` est membre et qui détient un secret
    `provider` — le hint `team_key_group` de `status_for` (drawer). `groups` =
    liste pré-chargée de `list_groups_for_user` (hissée par l'appelant batch,
    /api/me boucle sur ~50 providers). Best-effort, ne lève jamais.

    `secrets_by_group` = la carte de `cascade.group_secret_map` quand l'appelant
    l'a déjà construite. Sans elle le comportement est INCHANGÉ (une lecture par
    équipe et par connecteur) : c'est ce que font les autres appelants et les
    tests. Avec elle, `/api/me` cesse de payer un aller-retour par connecteur
    `forbidden` — soit la majorité d'un compte réel, 67 lectures mesurées sur une
    seule équipe, et autant de plus par équipe supplémentaire.

    L'ORDRE est le contrat : la première équipe de `groups` qui détient le secret
    gagne, carte ou pas. Répondre depuis la carte ne change que l'endroit où la
    réponse est lue, jamais laquelle."""
    if org is None or provider not in cascade.ORG_SHAREABLE_PROVIDERS:
        return None
    try:
        if groups is None:
            groups = group_store.list_groups_for_user(sub, org)
        for g in groups:
            detient = (provider in secrets_by_group.get(int(g["group_id"]), ())
                       if secrets_by_group is not None
                       else group_store.has_group_secret(g["group_id"], provider))
            if detient:
                return {"id": g["group_id"], "name": g["name"]}
    # noqa: SILENT — fail-open de visibilité, backstop dur au call-time
    except Exception:
        return None
    return None


def reachable_instances_map(sub: str, org: Optional[int]) -> dict[str, list[dict]]:
    """`{provider: [instances à portée]}` en UNE passe — version BATCHÉE de
    `reachable_instances`, pour annoter un catalogue entier (≈40 connecteurs).

    `reachable_instances` interroge la DB **par provider** (has_group_secret /
    has_org_secret) : l'appeler en boucle sur le catalogue ferait N×M
    allers-retours sur un serveur mono-loop. Ici on liste les secrets de chaque
    entité UNE fois (`list_group_secrets` / `list_org_secrets`) et on inverse en
    mémoire → coût borné par le nombre d'équipes + d'orgs, pas de providers.

    Limite assumée : ne couvre pas « ma clé MEMBRE dans une autre org » (pas de
    listing groupé côté `db.has_member_api_key`, qui est per-provider). Le hint
    d'erreur, lui, la couvre — le catalogue est une surface de découverte, pas
    l'autorité. Best-effort : ne lève jamais."""
    out: dict[str, list[dict]] = {}

    def _add(provider: str, item: dict) -> None:
        out.setdefault(provider, []).append(item)

    try:
        if org is not None:
            seen_gids: set = set()
            groups = list(group_store.list_groups_for_user(sub, org))
            # #218 : l'org_admin gouverne ses équipes sans en être membre — ses clés
            # d'équipe lui sont atteignables par escalade (re-gardées à l'appel).
            from .. import roles
            try:
                if roles.is_org_admin(sub, org):
                    known = {g["group_id"] for g in groups}
                    groups += [{"group_id": g["id"], "name": g["name"]}
                               for g in group_store.list_groups(org)
                               if g["id"] not in known]
            # noqa: SILENT — fail-open de visibilité, backstop dur au call-time
            except Exception:
                pass
            for g in groups:
                gid = g["group_id"]
                if gid in seen_gids:
                    continue
                seen_gids.add(gid)
                for s in group_store.list_group_secrets(gid):
                    p = s.get("provider")
                    if p and p in cascade.ORG_SHAREABLE_PROVIDERS:
                        _add(p, {"kind": "group", "id": gid, "name": g["name"]})
        for o in org_store.list_orgs_for_user(sub):
            oid = o["org_id"]
            if oid == org:
                continue
            for s in org_store.list_org_secrets(oid):
                p = s.get("provider")
                if p and p in cascade.ORG_SHAREABLE_PROVIDERS:
                    _add(p, {"kind": "org", "id": oid,
                             "name": o.get("name") or f"org {oid}"})
    # noqa: SILENT — fail-open de visibilité, backstop dur au call-time
    except Exception:
        return out
    # Délégation : la carte d'un canal doit montrer les instances de SON compte —
    # ce sont les seules qui existent, et c'est la même liste, pas une approximation.
    # Le catalogue annote par NOM de connecteur (`connectors_selection`), donc sans
    # cet alias la ligne `whatsapp` reste muette pendant que `unipile` affiche la clé
    # d'équipe qui la ferait marcher.
    for c in providers._REGISTRY_LIST:
        if c.credential_of and c.credential_of in out:
            out[c.name] = list(out[c.credential_of])
    return out


_REVOKED_REASON_LABELS = {
    db.REVOKED_CREDENTIAL_REMOVED: "clé retirée",
    db.REVOKED_RENAMED_ONTO_EXISTING: "compte renommé vers un autre déjà posé",
    db.REVOKED_VAULT_ROW_MISSING: "ligne de coffre disparue (maintenance)",
}


def _revoked_hint(sub: str, org: Optional[int], provider: str) -> str:
    """Suffixe actionnable des erreurs « aucun credential configuré » : dit si CE
    connecteur a existé ici puis a été RETIRÉ, plutôt que de laisser croire qu'il n'a
    jamais été posé. Chaîne vide si rien n'a jamais existé, ou sans org (pas de scope
    membre à interroger).

    oto#42, entrée 11 du lot 1 — quatre signalements le même jour (03/09) pour cette
    seule cause : chacun a mené sa propre enquête pour retrouver une info déjà en
    base. Lecture seule (`connector_instances.most_recent_revocation`), jamais un
    critère d'aiguillage — voir son docstring.

    Fail-soft PAR CONSTRUCTION (comme `chain_shadow.observe`) : c'est un hint EN
    PLUS d'un refus déjà levé, jamais un chemin dont la résolution dépend — un
    hoquet DB ici doit rendre le refus normal (sans second indice), pas remplacer
    le refus par une 500."""
    if org is None:
        return ""
    try:
        rev = db.most_recent_revocation("member", credentials_store.member_id(org, sub), provider)
    # noqa: SILENT — hint best-effort : un hoquet DB laisse le refus SANS second indice
    except Exception:
        logger.warning("hint de révocation indisponible pour %s (fail-soft)", provider,
                       exc_info=True)
        return ""
    if not rev:
        return ""
    motif = _REVOKED_REASON_LABELS.get(rev["revoked_reason"], rev["revoked_reason"])
    quand = str(rev["revoked_at"])[:10]
    return f"\n(un `{provider}` a existé ici et a été retiré le {quand} — {motif})"


def _reachable_hint(sub: str, org: Optional[int], provider: str) -> str:
    """Suffixe actionnable des erreurs « rien ne résout » : remonte les instances
    à portée avec le GESTE de pin pour chacune — jeton d'appel d'abord (`_group=`/
    `_org=`, per-call, sans état), `_instance=` pour le grain fin. Chaîne vide si
    rien à portée."""
    items = reachable_instances(sub, org, provider)
    if not items:
        return ""
    lines = []
    for it in items[:4]:
        if it["kind"] == "group":
            lines.append(
                f"· équipe « {it['name']} » → passe group={it['id']} sur l'appel "
                f"(ou instance=group:{it['id']}:{provider})")
        else:
            lines.append(
                f"· org « {it['name']} » → passe org={it['id']} sur l'appel")
    more = len(items) - 4
    if more > 0:
        lines.append(f"· … +{more} (oto_instance op=list pour tout voir)")
    return (
        # Le hint nomme des clés qui appartiennent à l'entité citée, et un prestataire
        # est membre des orgs de SES clients : sans cette réserve, il se lit comme un
        # libre-service et l'agent bascule l'appel sur les crédits d'un client pour un
        # travail qui n'est pas le sien (signalé le 12/08 sur une prospection maison
        # renvoyée vers la clé d'une org cliente).
        f"\nNB — des clés `{provider}` existent à portée, aux frais de l'entité citée "
        f"— n'y bascule un appel QUE s'il est fait pour elle :\n" + "\n".join(lines)
        + "\nDurable : lie l'instance à ton projet (oto_project op=link)."
    )


def resolve_field_filter(service: str):
    """Construit le `FieldFilter` à appliquer aux réponses d'un connecteur pour
    le sub courant, selon la politique de redaction de son **org active**.

    Cascade (décision « contrôle total org ») :
      1. l'org active a une politique pour ce service → elle est **autoritaire**
         (peut lever le masquage baseline, ou ne rien masquer) ;
      2. sinon → repli sur le **défaut serveur** (`field_filter_defaults`, plancher
         PII explicite, ex. IBAN Silae) ;
      3. sinon → filtre vide (no-op, aucune redaction).

    Best-effort : sans org active ou sur erreur DB, on retombe sur le défaut
    serveur (jamais moins protecteur que l'état pré-UI)."""
    from oto.tools.common import FieldFilter

    from .. import field_filter_defaults

    block: Optional[dict] = None
    sub = current_user_sub_from_token()
    if sub:
        active_org = scope.current_org(sub)
        if active_org is not None:
            configured = org_store.get_org_field_filters(active_org)
            if service in configured:
                block = configured[service]
    if block is None:
        block = field_filter_defaults.SERVER_DEFAULTS.get(service)
    if not block:
        return FieldFilter()
    return FieldFilter(rules=block.get("rules", []), salt=block.get("salt"))
