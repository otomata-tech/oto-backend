"""Qui agit, et dans quel contexte (ADR 0023/0038) — la couche BASSE du package.

Trois questions, un seul endroit pour chacune :

- **le rôle plateforme** du sub (`member` < `admin` < `super_admin`) ;
- **le contexte de l'appel** — org, équipe et projet EFFECTIFS, résolus
  `jeton d'appel ?? consultation ?? maison` ; `current_org` est le seam unique
  par lequel passe tout ce qui scope une action (credentials, visibilité,
  entitlements, redaction) ;
- **ce que le projet actif ÉPINGLE** — identité, instance, slot de tableau.

Ce module ne dépend d'aucun autre sous-module d'`access` : tous les autres
partent de lui. Une fonction qui a besoin de savoir « pour qui, sous quelle
org » l'appelle — elle ne relit jamais la maison (`org_store.get_active_org`)
en direct, cf. `tests/test_org_seam_tripwire.py`.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import db, group_store, org_store, session_org
from ..auth.hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)


# Rôles plateforme, du plus faible au plus fort : `member` (défaut non-admin) <
# `admin` (opérateur : supervision sans escalade en masse) < `super_admin`
# (tout-puissant : escalade org/groupe, rôles, keys, tokens, orgs tierces).
# `guest` retiré (2026-06-15) — c'était un alias sans effet, migré en `member`.
MEMBER = "member"
ADMIN = "admin"
SUPER_ADMIN = "super_admin"
ROLES = (MEMBER, ADMIN, SUPER_ADMIN)


def get_user_role(sub: str) -> str:
    """Rôle effectif du user — env override > DB > défaut member.

    Le bootstrap `OTO_MCP_ADMIN_SUB` force le **super_admin** (le tout-puissant)
    — c'est le sub propriétaire de la plateforme."""
    admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
    if admin_sub and sub == admin_sub:
        return SUPER_ADMIN
    user = db.get_user(sub)
    role = (user or {}).get("role") or MEMBER
    return role if role in ROLES else MEMBER


def is_super_admin(sub: str) -> bool:
    """Tout-puissant : escalade org/groupe, rôles plateforme, keys, tokens,
    écriture sur orgs tierces."""
    return get_user_role(sub) == SUPER_ADMIN


def is_platform_operator(sub: str) -> bool:
    """Opérateur plateforme = `admin` (supervision) OU `super_admin`. Cran de
    visibilité/supervision, SANS l'escalade en masse réservée au super_admin."""
    return get_user_role(sub) in (ADMIN, SUPER_ADMIN)


def is_operator_role(sub: str, role: Optional[str]) -> bool:
    """MÊME prédicat qu'`is_platform_operator`, sur un `role` DÉJÀ EN MAIN (colonne
    `users.role`) — pour un appelant qui a déjà lu la ligne (ex. la liste des membres
    d'une org, `capabilities/orgs/reads.py::_members`, oto#270 suite) et ne doit pas
    refaire un `db.get_user` par membre pour ce seul verdict. Même override
    `OTO_MCP_ADMIN_SUB`, même repli sur `role` (pas de validation contre `ROLES` :
    un rôle hors énumération ne matche ni `ADMIN` ni `SUPER_ADMIN`, donc `False`,
    comme `get_user_role` qui le ramènerait à `MEMBER`)."""
    admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
    if admin_sub and sub == admin_sub:
        return True
    return role in (ADMIN, SUPER_ADMIN)


def current_org(sub: str | None) -> Optional[int]:
    """Org sous laquelle Claude AGIT pour le `sub` courant — **seam unique** de
    résolution d'org (ADR 0023, amende 0015).

    Point de passage de TOUT ce qui scope une action sur l'org (credentials,
    visibilité, entitlements, redaction). Aujourd'hui (barreau R0) =
    l'org persistée (`org_store.get_active_org`, qui devient l'« org maison »).

    Résout `jeton d'appel ?? org du run ?? consultation ?? maison` (ADR 0038, amende
    0023 ; étage « org du run » ajouté le 30/08/2026, #639) :
    - **jeton d'appel** (MCP) — `_org=`/`_project=`/`_group=` posés déjà gardés par
      les axes/adaptateurs (contextvar per-requête) ; AUCUN état de session ;
    - **org du run** (MCP) — sans jeton, un appel qui porte `_run_id=` se résout dans
      `runs.org_id`, posée déjà gardée (appartenance) par le middleware
      (`run_org.pin_for_call`) ; un run inconnu ne pose rien ;
    - **org de consultation** (REST) — view-as du dashboard, contextvar per-requête
      posé APRÈS validation d'appartenance par l'adaptateur REST ;
    - sinon → repli sur la **maison** persistante (`org_store.get_active_org`).

    Jeton et consultation ne coexistent jamais (jeton = MCP only, consultation =
    REST only). Garder ce seam étroit : candidat broker de credentials (ADR 0004)."""
    if sub is None:
        # Endpoint MCP ANONYME (`<slug>.mcp.oto.cx`, ADR 0032) : pas de sub, mais l'org
        # PROPRIÉTAIRE du projet est le contexte de résolution (credentials/redaction).
        from .. import subdomain_project
        return subdomain_project.current_anon_org()
    # Jeton de DÉLÉGATION (`verrou_org.py`) : l'org de son travail, avant tout le
    # reste — ni un jeton d'appel, ni l'org d'un run, ni la maison du porteur. Pour le
    # porteur seul : l'org d'un TIERS se résout par son chemin ordinaire.
    from .. import verrou_org
    if (verrou := verrou_org.borne(sub, route="current_org", ecart=True)) is not None:
        return verrou.org_id
    # Endpoint scopé par sous-domaine (« 1 oto par org ») : épingle l'org de la
    # connexion AVANT tout. Garde d'appartenance ici (sub connu) → un non-membre
    # est ignoré (repli maison, zéro fuite). Précédence ⇒ hard-lock : `oto_use_org`
    # (override de session) ne peut pas sortir de l'org du sous-domaine.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from .. import roles
        if roles.is_org_member(sub, cand):
            return cand
    # Jeton explicite de l'appel (`_org=`, modèle sans état de session) : posé par
    # l'adaptateur capacité APRÈS validation d'appartenance → rendu tel quel. Prime
    # sur l'override de session (qui, lui, ne survit pas au stateless claude.ai).
    call = session_org.current_call_org()
    if call is not None:
        return call
    # L'org du RUN (#639, 30/08/2026) : sans `_org=`, un appel fait DANS un run se
    # résout dans l'org du run (`runs.org_id`), pas dans la maison du sub — c'est ce
    # qui refusait « namespace inconnu » à 82 `data_write` sur sept jours et stampait
    # le journal hors de l'org du travail (#630/#631). Posée par le middleware (une
    # lecture par run, appartenance gardée, refus nommé sinon) — jamais relue ici :
    # le seam reste sans requête.
    run_org = session_org.current_call_run_org()
    if run_org is not None:
        return run_org
    # Le BRACELET de session (`oto_use_org`, dict keyé Mcp-Session-Id) n'est PLUS lu
    # (ADR 0038 B3) : claude.ai renouvelle le session_id à chaque appel (jamais relu)
    # et un session_id recyclé cross-compte faisait fuiter le scope (#108). Le scope
    # est porté par l'appel (`_org=`/`_project=`/`_group=`, ci-dessus) ou retombe maison.
    view = session_org.current_view_org()
    if view is not None:
        return None if view == 0 else view
    return org_store.get_active_org(sub)


# Sentinelle « param non fourni » — distingue « org=None » (perso, valeur légitime)
# de « pas d'org explicite → résous via current_org ». Sert à calculer l'état d'un
# TIERS (fiche admin) contre SON org persistée, sans laisser fuiter le contexte
# view-as/session du REQUÉRANT (bug 2026-06-24 : has_option(cible) lisait l'org du
# requérant). Le chemin self (/api/me) ne passe rien → comportement inchangé.
_UNSET: object = object()


def current_group(sub: str | None) -> Optional[int]:
    """Équipe (groupe) EFFECTIVE — mirror de `current_org` pour l'axe groupe
    (ADR 0038). Résout `jeton d'appel ?? consultation ?? maison` en TENANT
    l'invariant « groupe ⊂ org » : un jeton/consultation d'ORG **sans** groupe
    explicite ⇒ niveau org (None), jamais le home_group d'une autre org."""
    if sub is None:
        return None
    # Sous lock d'org par sous-domaine : le groupe n'est rendu QUE s'il ⊂ l'org
    # épinglée (sinon None = niveau org) — hard-lock cohérent avec current_org.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from .. import roles
        if not roles.is_org_member(sub, cand):
            return None
        ag = group_store.get_active_group(sub)
        if ag is not None and (group_store.get_group(ag) or {}).get("org_id") == cand:
            return ag
        return None
    # Jeton d'appel `_group=` : déjà gardé à la pose (can_read_group + org co-posée
    # par l'axe, invariant par construction) → rendu tel quel. Le BRACELET de session
    # (`oto_use_group`) n'est plus lu (ADR 0038 B3, même raison que current_org).
    call_g = session_org.current_call_group()
    if call_g is not None:
        return call_g
    vg = session_org.current_view_group()
    if vg is not None:
        return None if vg == 0 else vg
    if session_org.current_view_org() is not None:
        return None  # consultation d'org sans groupe → niveau org
    ag = group_store.get_active_group(sub)  # maison
    if ag is None:
        return None
    # Jeton d'org (`_org=`/`_project=`) — ou org du RUN (#639) — SANS groupe : le
    # home_group n'est rendu que s'il appartient à l'org épinglée (invariant groupe ⊂
    # org — jamais le home_group d'une AUTRE org sous une org de jeton ou de run).
    call_org = session_org.current_call_org()
    if call_org is None:
        call_org = session_org.current_call_run_org()
    if call_org is not None:
        g = group_store.get_group(ag)
        if not g or g.get("org_id") != call_org:
            return None
    return ag


def current_project() -> Optional[int]:
    """Projet de l'APPEL courant (ADR 0038) = jeton `_project=` — posé déjà gardé
    (`can_access` + org dérivée co-posée) par l'axe d'appel. Le BRACELET de session
    (`oto_use_project`) n'est plus lu (B3b — même raison que org/groupe : claude.ai
    renouvelle le session_id à chaque appel, et un session_id recyclé cross-compte
    faisait hériter le contexte, #108). Pas de projet « maison » : pas de jeton ⇒
    None (hors projet). Sert la surcharge connecteur PRÉFAITE du projet, les slots
    (ADR 0035) et le gel `runs.project_id`."""
    return session_org.current_call_project()


def current_user_sub_or_raise() -> str:
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Unauthenticated — no user identity on the request.",
        ))
    return sub


def _sub_matches_scopes(sub: str, scopes) -> bool:
    """Vrai si `sub` appartient à l'un des scopes listés — vocabulaire COMMUN aux
    allowlists `share_down` et aux prêts `share_side` (ADR 0044) : `user:<sub>` | `group:<gid>` | `org:<id>` (appartenance
    réelle) | `org` (tout le monde du sous-arbre). `org:<id>` (ADR 0044 §F) porte
    l'ancien grant org-level d'une clé plateforme. Fail-closed par entrée (une ref
    malformée est ignorée, jamais d'exception qui casserait la résolution)."""
    from .. import group_store, roles
    for s in scopes or []:
        if s == "org":
            return True
        kind, _, ident = str(s).partition(":")
        if kind == "user" and ident == sub:
            return True
        if kind == "group":
            try:
                if group_store.is_group_member(sub, int(ident)):
                    return True
            except (ValueError, TypeError):
                continue
        if kind == "org":
            try:
                if roles.is_org_member(sub, int(ident)):
                    return True
            except (ValueError, TypeError):
                continue
    return False


def project_pinned_identity(connector: str, project_id: Optional[int] = None) -> Optional[str]:
    """Identité (account) ÉPINGLÉE par le projet actif pour `connector`, ou None ⇒ la
    résolution retombe sur le défaut user. Lit la clé de BINDING `project_links.identity_ref`
    (ADR 0032 §4 amendé, #57). Multiplicité : **un seul** binding avec identité ⇒ on l'épingle ;
    **plusieurs** ⇒ None (ambigu → l'agent doit préciser `_account=` à l'appel). `project_id`
    omis ⇒ projet de session (`current_project`). **Fail-soft** : toute erreur ⇒ None
    (jamais de plantage de la résolution d'un tool sur ce chemin)."""
    pid = current_project() if project_id is None else project_id
    if pid is None:
        return None
    try:
        pinned = [link.get("identity_ref")
                  for link in db.list_project_links(int(pid))
                  if link.get("target_type") == "connecteur"
                  and link.get("target_ref") == connector and link.get("identity_ref")]
        return pinned[0] if len(pinned) == 1 else None
    except Exception as e:
        logger.warning("project_pinned_identity fail-soft %s/%s: %s", pid, connector, e)
    return None


def project_declared_identities(connector: str, project_id: int) -> list[str]:
    """TOUTES les identités déclarées par un projet pour `connector` (ADR 0032 §4).

    Pendant de `project_pinned_identity`, pour le chemin **sans `sub`** (endpoint MCP
    publié, ADR 0032) : là, il n'y a personne dont on puisse prendre le compte par
    défaut, et « ambigu ⇒ None » n'est pas jouable — un projet qui déclare LinkedIn ET
    WhatsApp sous le même connecteur `unipile` n'est pas ambigu, il déclare deux canaux.
    On rend donc la liste, et c'est au module du connecteur de choisir sur un critère
    qu'il est seul à connaître (le canal, ici) — la spécificité reste dans son module.

    ⚠️ Le résultat n'est PAS une autorisation : ces refs viennent d'un lien de projet,
    donc de ce qu'un membre a écrit. Sur une clé PARTAGÉE (l'abonnement Unipile de la
    plateforme adresse tous les comptes de tous les tenants), les servir tels quels
    laisserait un lien nommer le compte d'autrui. L'appelant DOIT recouper contre les
    comptes réellement rattachés à l'org propriétaire. Fail-soft : erreur ⇒ []."""
    try:
        return [str(link["identity_ref"])
                for link in db.list_project_links(int(project_id))
                if link.get("target_type") == "connecteur"
                and link.get("target_ref") == connector and link.get("identity_ref")]
    except Exception as e:  # noqa: BLE001
        logger.warning("project_declared_identities fail-soft %s/%s: %s",
                       project_id, connector, e)
        return []


def project_pinned_instance(provider: str, project_id: Optional[int] = None):
    """Instance de connecteur BINDÉE par le projet de l'appel pour `provider`
    (`project_links.config.instance_ref`, ADR 0038 B5), ou None ⇒ cascade normale.
    **Un seul** binding à instance ⇒ son ref (parsé) ; **plusieurs** ⇒ McpError
    actionnable (identité d'action en jeu — jamais de choix silencieux : l'agent
    précise `_instance=`). Lecture des liens fail-soft (DB en hoquet ⇒ None, comme
    `project_pinned_identity`) ; un ref STOCKÉ inparsable lève (validé au link —
    corruption = erreur, pas un repli muet)."""
    pid = current_project() if project_id is None else project_id
    if pid is None:
        return None
    try:
        refs = [(link.get("config") or {}).get("instance_ref")
                for link in db.list_project_links(int(pid))
                if link.get("target_type") == "connecteur"
                and link.get("target_ref") == provider
                and (link.get("config") or {}).get("instance_ref")]
    except Exception as e:
        logger.warning("project_pinned_instance fail-soft %s/%s: %s", pid, provider, e)
        return None
    if not refs:
        return None
    if len(refs) > 1:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Le projet #{pid} binde PLUSIEURS instances `{provider}` — "
                     f"précise laquelle avec `_instance=` ({', '.join(refs)}).")))
    from .. import instance_refs
    return instance_refs.parse_ref(refs[0])


# Préfixe d'adressage par slot (ADR 0035 B3) : `slot:<name>` dans un argument
# `namespace` des tools data_* = « le tableau bindé sous ce nom par le projet actif ».
SLOT_PREFIX = "slot:"


def resolve_datastore_ref(namespace: str) -> str:
    """Résout une référence de tableau : `slot:<name>` → l'IDENTIFIANT du tableau
    bindé par le projet actif ; un nom nu passe inchangé (zéro magie sur les noms
    littéraux).

    Source UNIQUE de cette résolution, appelée par les tools `data_*` comme par les
    capacités du datastore. Elle a d'abord vécu dans `tools/datastore.py` seulement,
    et c'est ce qui a fait le trou : une capacité datastore recevait `slot:vivier`
    comme un nom littéral et répondait « tableau introuvable ». Sur un verbe destructif
    (`data_drop_column`), l'échec est heureux — mais un agent qui travaille en slots
    voit seize refus sans comprendre pourquoi, et croirait à un tableau déjà propre
    si le refus n'était pas là."""
    if (isinstance(namespace, str)
            and namespace.strip().lower().startswith(SLOT_PREFIX)):
        return resolve_slot_tableau(namespace.strip()[len(SLOT_PREFIX):])
    return namespace


def resolve_slot_tableau(name: str) -> str:
    """Résout un slot `tableau` contre les bindings du projet ACTIF (ADR 0035 B3) →
    l'IDENTIFIANT du tableau bindé (#365 — jamais son nom). **Enforcement serveur, jamais
    de fallback** : pas de projet actif, slot non bindé, ou binding pendouillant ⇒
    `McpError` ACTIONNABLE — on n'interprète jamais `slot:x` comme un nom littéral
    et on ne « prend jamais le premier tableau venu »."""
    from .. import slots as slots_mod
    try:
        name = slots_mod.normalize_name(name)
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"slot invalide : {e}"))
    pid = current_project()
    if pid is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"`slot:{name}` exige un PROJET (le binding nom→instance vit "
                     "dans le projet, ADR 0035). Passe `project=<id>` sur CET appel "
                     "(liste : `oto_project op=list`) — ou crée un projet et binde le slot "
                     f"(`oto_project op=link target_type=tableau … slot='{name}'`), ou "
                     "passe un `namespace` explicite.")))
    links = db.list_project_links(int(pid))
    match = [l for l in links
             if l.get("target_type") == "tableau" and l.get("slot") == name]
    if not match:
        bound = sorted(l["slot"] for l in links
                       if l.get("target_type") == "tableau" and l.get("slot"))
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"le projet actif (#{pid}) ne binde aucun slot tableau `{name}`. "
                     + (f"Slots bindés : {', '.join(bound)}. " if bound else
                        "Aucun slot tableau bindé dans ce projet. ")
                     + f"Binde-le : `oto_project op=link project_id={pid} "
                       f"target_type=tableau target_ref=<id> slot='{name}'`.")))
    # ⚠️ **L'IDENTIFIANT, jamais le nom (#365).** `slot:` est la façon dont les
    # procédures adressent « le tableau de ce projet » sans nom en dur : tout passe par
    # ici. On rendait le NOM du tableau bindé, que le store résolvait ensuite dans la
    # portée de l'APPELANT — son « vivier » perso passait donc devant le « vivier » que
    # le projet a bindé, et la procédure écrivait chez l'homonyme, sans erreur. Le
    # binding désigne un tableau précis : on rend son identifiant (`datastore_id`,
    # résolu dans la portée du PROPRIÉTAIRE du projet, `db.list_project_links`).
    lien = match[0]
    if lien.get("datastore_ambigu"):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"le slot `{name}` du projet #{pid} désigne son tableau par le NOM "
                     f"« {lien.get('target_ref')} », que plusieurs tableaux portent dans "
                     "la portée du projet — rien n'est servi. Re-binde-le par identifiant "
                     f"(`oto_project op=link project_id={pid} target_type=tableau "
                     f"target_ref=<id> slot='{name}'`).")))
    ns_id = lien.get("datastore_id")
    if ns_id is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"le slot `{name}` du projet #{pid} pointe un tableau qui ne résout "
                     f"plus (ref `{lien.get('target_ref')}`) — re-binde-le sur un "
                     "tableau existant (`oto_project op=link`).")))
    return str(int(ns_id))
