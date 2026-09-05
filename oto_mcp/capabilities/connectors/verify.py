"""Capacité « tester la connexion d'un connecteur » (framework de sondes, ADR 0009).

Résout le credential (cascade effective, ou clé d'org explicite) et exécute la sonde
enregistrée du connecteur (`connector_verify`). L'échec d'authentification EST le
résultat (`{ok:false, error}`), jamais un 500 — même esprit que le banc de test d'outil
(`my_tool_call`). Le message provider est déjà nettoyé par la sonde ; ici on extrait
juste celui d'une `McpError` (ex. data center Zoho manquant).
"""
from __future__ import annotations

import time
from typing import Literal, Optional

from ...mcp_errors import McpError
from pydantic import BaseModel, ConfigDict

from ... import access, credentials_store, status_hints
from ...connectors import health as connector_health
from ...connectors import verify as connector_verify
from .._authz import ORG_ADMIN, ORG_MEMBER
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)


class VerifyInput(BaseModel):
    provider: str                              # path {provider}
    level: Literal["auto", "org"] = "auto"     # auto = credential effectif ; org = clé de l'org


class VerifyResult(BaseModel):
    """Verdict d'une sonde de connexion. L'échec d'authentification EST le
    résultat (200 + `ok:false`), jamais un 500 — un client qui ne regarde que le
    code HTTP conclura toujours au succès."""
    ok: bool
    provider: str
    # Durée de la sonde. Vaut **0** sans avoir rien sondé quand `pending` est vrai.
    elapsed_ms: int
    # QUELLE instance a répondu, DÉRIVÉE de la même entité que `ref` pour qu'ils ne
    # puissent pas se contredire. Sous `level="auto"` la cascade a pu retomber d'un
    # cran : `ok:true` seul ne distingue pas « ma clé perso marche » de « ma clé
    # perso a échoué, c'est celle de l'org qui répond ». `platform` = grant
    # plateforme, qui n'a aucune ligne de coffre.
    level: Literal["member", "group", "org", "tenant", "platform"]
    # `<level>:<entity_id>:<provider>` — ex. `org:2:salesforce`. Au palier
    # plateforme l'`entity_id` est le LABEL de la clé (ADR 0044 §F : plus de
    # surrogate id), pas un entier : `platform:serper-shared:serper`.
    ref: str
    # ⚠️ Porte DEUX sens selon `pending`. Sous `pending:true` ce n'est PAS une
    # erreur mais l'ÉTAPE SUIVANTE à faire (`status_hints.next_action`) ; sinon
    # c'est le message d'échec de la sonde. ABSENT quand `ok:true`.
    error: Optional[str] = None
    # Présent (et vrai) UNIQUEMENT sur une connexion en DEUX temps volontairement
    # incomplète (application posée, consentement à venir) : rien n'a été sondé.
    # `ok:false` + `pending:true` = « enregistré, la suite est ailleurs », pas un
    # credential invalide — le confondre rouvre le formulaire sur une correction
    # impossible.
    pending: Optional[bool] = None

    # CE QUE LA SONDE A MESURÉ (oto#57) — `auth` : la clé authentifie, et rien de
    # plus ; `auth+quota` : elle authentifie ET il reste de quoi travailler.
    #
    # ⚠️ Un `ok:true` sous `coverage:"auth"` ne dit RIEN du solde. C'est ce que le
    # 04/09/2026 a coûté : un préflight tout vert, puis 402 après quatre espaces,
    # quatre tables et 28 lignes créés. **La sonde n'avait pas menti** — elle avait
    # rapporté un vert qui ne voulait pas dire ce qu'on croyait. Le champ existe pour
    # qu'un appelant sache ce qu'il ne sait pas.
    #
    # ⚠️ `null` = aucune sonde n'est déclarée pour ce connecteur. Ce n'est pas
    # « ne couvre rien » : dans un cas on n'a pas pu mesurer, dans l'autre on a
    # mesuré l'authentification. Les deux appellent des conduites différentes.
    coverage: Optional[str] = None


class MemberProviderStatus(BaseModel):
    """Entrée `ProviderStatus` d'un membre pour un connecteur — la MÊME forme que
    lit sa propre carte, rejouée par un org_admin.

    Le jeu de clés dépend de la FAMILLE du connecteur (keyed à quota, BYO à champs
    déclarés, session navigateur, OAuth fédéré) : `quota_*` n'existe qu'avec un
    palier plateforme, `session_set_at`/`identity_*` seulement pour une session
    navigateur, etc. D'où l'ouverture aux champs additionnels — seuls `mode` et
    les quatre booléens de présence sont servis par toutes les familles."""
    model_config = ConfigDict(extra="allow")

    # `forbidden` = rien ne résout POUR CE MEMBRE ; il ne dit pas pourquoi (option
    # manquante, activation coupée, RBAC) — c'est `connectors.me` qui désambiguïse.
    mode: str
    user_key_configured: bool
    group_secret_configured: bool
    org_secret_configured: bool
    platform_key_label: Optional[str] = None
    quota_used_today: Optional[int] = None
    # `null` = pas de palier plateforme OU quota illimité (convention : 0 illimité
    # est traduit en `null` pour que l'UI affiche « ∞ », pas « /0 »).
    quota_daily: Optional[int] = None
    # Clé d'équipe « à portée » sans être active — renseignée SEULEMENT quand
    # `mode == "forbidden"` : sa présence dit « une clé existe, il faut l'épingler ».
    team_key_group: Optional[dict] = None


class ConnectorEffectForMember(BaseModel):
    """Verdict d'un connecteur REJOUÉ pour un membre nommé (M4) : ce que LUI voit,
    calculé contre SON org (jamais le contexte du requérant, ADR 0023)."""
    provider: str
    member: str                               # sub du membre visé
    # ⚠️ `null` = ce connecteur n'a AUCUNE entrée de statut (nom hors catalogue, ou
    # famille sans credential) — pas « accès refusé ». Un front qui rend le null
    # comme un blocage invente un verdict.
    status: Optional[MemberProviderStatus] = None


def _ref(entity_type: "str | None", entity_id: "str | None", provider: str) -> str:
    """Identifiant lisible de l'instance sondée. `None` = grant plateforme : il n'a pas
    de ligne de coffre, mais il faut quand même pouvoir le NOMMER dans le résultat."""
    if entity_type is None:
        return f"platform:{provider}"
    return f"{entity_type}:{entity_id}:{provider}"


def _fields_config_scope(ctx: ResolvedCtx, inp: VerifyInput) -> tuple[dict, dict, "tuple | None", dict, "tuple | None"]:
    """(champs, config, SCOPE-santé, INSTANCE sondée, CIBLE d'écriture) selon le niveau.

    `instance` = quelle clé a RÉELLEMENT été testée (`level` + `ref`). Sans elle, un
    `ok:true` en niveau `auto` est ambigu : la cascade a pu retomber d'un cran, et on ne
    peut pas distinguer « ma clé perso marche » de « ma clé perso a échoué, c'est celle
    de l'org qui répond ». C'est précisément le cas où la confirmation compte.

    `config` = satellites NON-secrets appariés à la clé (meta public : dsn
    unipile…) — une sonde vers un endpoint dont l'hôte dépend de la clé DOIT en tenir compte.
    `scope-santé` = où persister le résultat (`meta.health_ko` + `meta.health_reason`) :
    `(entity_type, entity_id, account)` de la ligne RÉELLEMENT testée, tant qu'elle
    n'est pas partagée au-delà de l'org de l'appelant — sinon None.
    ⚠️ Elle ne valait que pour le palier MEMBRE jusqu'au 2026-09-03 : un `level="auto"`
    qui résolvait une clé d'ORG — le seul palier possible d'un connecteur `byo_org` only
    comme `linear` — n'écrivait RIEN. L'utilisateur lisait `ok:false` sur la sonde et
    retrouvait sa carte verte derrière lui (#541). Restent exclus le palier TENANT et la
    clé PLATEFORME, partagés par des orgs entières : le hoquet réseau d'un seul membre
    n'a pas à les peindre en rouge pour tout le monde.

    - `auto` (carte user) : le credential EFFECTIF (cascade user > équipe > org >
      plateforme). `emit_on_failure=False` : une sonde ne doit pas polluer le monitoring.
    - `org` (carte org) : la clé DE L'ORG active/consultée spécifiquement (une clé perso
      la masquerait dans la cascade). `ctx.org_id` est injecté par l'authz (IDOR-safe)."""
    if inp.level == "org":
        row = credentials_store.get_credential_with_meta("org", str(ctx.org_id), inp.provider)
        if not row:
            raise AuthzDenied(400, "no_org_credential",
                              "aucune clé d'org posée pour ce connecteur.")
        return (credentials_store.unpack_secret(inp.provider, row["secret"]),
                credentials_store.public_meta(row.get("meta")),
                ("org", str(ctx.org_id), ""),
                {"level": "org", "ref": _ref("org", str(ctx.org_id), inp.provider)},
                ("org", str(ctx.org_id), ""))
    rc = access.resolve_credential(
        inp.provider, want="auto", sub=ctx.sub, emit_on_failure=False,
    )
    etype, eid = getattr(rc, "entity_type", None), getattr(rc, "entity_id", None)
    scope = ((etype, eid, getattr(rc, "account", "") or "")
             if etype in connector_health.FLAGGABLE_SCOPES and eid else None)
    # `level` et `ref` sont DÉRIVÉS de la même source — l'entité — pour qu'ils ne
    # puissent pas se contredire. La version précédente exposait `rc.mode`, dont le
    # vocabulaire diffère (`user` là où l'entité, le `ref` et `oto_instance op=list`
    # disent `member`) : deux mots pour le même objet dans la même réponse, et tout
    # code comparant ce `level` à celui de la liste cassait. Signalé le 03/08.
    instance = {"level": etype or "platform", "ref": _ref(etype, eid, inp.provider)}
    # Cible d'ÉCRITURE pour une sonde à effet de bord (rotation) — None pour un grant
    # plateforme, qui n'a pas de ligne de coffre à réécrire.
    cible = (etype, eid, getattr(rc, "account", "") or "") if etype else None
    return rc.fields, rc.config, scope, instance, cible


async def _verify(ctx: ResolvedCtx, inp: VerifyInput) -> dict:
    probe = connector_verify.probe_for(inp.provider)
    if probe is None:
        raise AuthzDenied(400, "verify_unavailable",
                          f"pas de test de connexion pour « {inp.provider} ».")
    fields, config, scope, instance, cible = _fields_config_scope(ctx, inp)
    # Connexion en DEUX temps : un credential VOLONTAIREMENT incomplet (app posée,
    # consentement à venir) n'est pas une erreur de saisie — sonder le renverrait un
    # échec, et le formulaire du dashboard resterait ouvert sur une correction
    # impossible (« connecter ne fait rien », vécu 28/07). On rend l'ÉTAT, pas un
    # verdict : `pending=True` dit au front « c'est enregistré, l'étape suivante est
    # ailleurs ». Même source que le verdict de la fiche (`status_hints`).
    st = status_hints.credential_state(inp.provider, fields)
    if st is not None and not st.complete:
        return {"ok": False, "pending": True, "provider": inp.provider,
                "error": st.next_action, "elapsed_ms": 0,
                "coverage": connector_verify.couverture(inp.provider), **instance}
    started = time.monotonic()
    ok, error = True, None
    try:
        # Un seul endroit exécute les sondes (`connectors.verify.executer`) : hors
        # de la boucle d'événements, sous une borne de temps, et il porte la
        # résolution de `instance` — sous rotation, sonder CONSOMME le jeton, et le
        # remplaçant doit être réécrit sur la ligne testée, pas sur celle que la
        # cascade aurait choisie. Ce bloc dupliquait ce geste : corriger l'un
        # laissait l'autre (oto-backend#867, lot 2).
        await connector_verify.executer(probe, fields, config, cible)
    # noqa: SILENT — l'erreur d'auth EST le résultat de la sonde, rendue à l'appelant
    except Exception as e:  # noqa: BLE001 — l'erreur d'auth EST le résultat
        ok = False
        error = e.error.message if isinstance(e, McpError) else str(e)
    # La sonde EST le « health check » (read facile) → son verdict alimente le flag santé.
    # `record_health` = aide partagée (`connectors/health.py`, oto#25 lot b2) : mêmes
    # deux lignes qu'avant sous `_record_health`, extraites pour que d'autres modules
    # (atlassian, folk, salesforce, zoho) réutilisent la MÊME garde de portée sans la
    # redéfinir chacun de leur côté.
    connector_health.record_health(inp.provider, scope, ok, error)
    out = {"ok": ok, "provider": inp.provider,
           "elapsed_ms": int((time.monotonic() - started) * 1000),
           # Ce que ce verdict VAUT : servi avec lui, jamais à côté. Un client qui
           # lit `ok` sans lire ceci croit en savoir plus qu'il n'en sait.
           "coverage": connector_verify.couverture(inp.provider),
           # QUELLE instance a répondu — cf. `_fields_config_scope`.
           **instance}
    if not ok:
        out["error"] = error
    return out


CAP_DOC = (
    "Test whether a connector's configured credential actually authenticates "
    "(side-effect-free probe), returning {ok, error}. Use it to diagnose a connector "
    "that is set but not working (wrong region, expired token…) before reporting a gap. "
    "'auto' tests the credential that resolves for you; 'org' tests the org shared key. "
    "The reply names the instance actually probed (`level` + `ref`) — under 'auto' the "
    "cascade may have fallen through to a shared key, and `ok` alone would not say so. "
    "⚠️ READ `coverage` WITH `ok`: it says what the probe actually measured. "
    "`auth` = the key authenticates, and NOTHING about credit or quota — an `ok:true` "
    "there does not mean the account can still work. `auth+quota` = it also checked "
    "there is something left to spend. `null` = this connector declares no probe at "
    "all, which is not the same as 'nothing to check'. A preflight built on `ok` alone "
    "reports green on an exhausted account and the work fails mid-flight, after side "
    "effects."
)

class EffectForMemberInput(BaseModel):
    provider: str                              # path {provider}
    member: str                                # query ?member=<sub> : le membre cible


def _effect_for_member(ctx: ResolvedCtx, inp: EffectForMemberInput) -> dict:
    """M4 (CDC connecteurs) : rejoue le verdict d'un connecteur POUR un membre de l'org
    (org admin) → « Effet pour : [membre] ». L'org est passée EXPLICITEMENT à `status_for`
    (ADR 0023 : jamais `current_org` d'un tiers). Anti-IDOR : la cible doit appartenir à
    l'org active. Retourne l'entrée `ProviderStatus` du membre pour ce connecteur (le front
    la passe à `connectorVerdict` pour afficher la même phrase que le membre verrait)."""
    org = ctx.org_id
    if org is None:
        raise AuthzDenied(400, "no_active_org", "Aucune org active.")
    from ... import roles
    if not roles.is_org_member(inp.member, org):
        raise AuthzDenied(404, "not_a_member", "Ce membre n'appartient pas à cette org.")
    st = access.status_for(inp.member, org=org)
    return {"provider": inp.provider, "member": inp.member,
            "status": (st.get("providers") or {}).get(inp.provider)}


from ..registry import CAPABILITIES  # noqa: E402

CAPABILITIES += [
    Capability(
        key="connectors.verify", handler=_verify, Input=VerifyInput, authz=ORG_MEMBER,
        Output=VerifyResult,
        description=CAP_DOC,
        errors=(DeclaredError(400, "no_org_credential",
                              "aucune clé d'org posée pour ce connecteur : il "
                              "n'y a rien à vérifier"),
                DeclaredError(400, "verify_unavailable",
                              "ce connecteur ne déclare aucune sonde de "
                              "vérification"),),
        rest=RestBinding("POST", "/api/me/connectors/{provider}/verify"),
    ),
    Capability(
        key="connectors.effect_for_member", handler=_effect_for_member,
        Input=EffectForMemberInput, authz=ORG_ADMIN, Output=ConnectorEffectForMember,
        description="Org admin: replay a connector's verdict AS a given org member (M4). "
                    "Returns that member's ProviderStatus entry for {provider}, org scoped.",
        errors=(DeclaredError(400, "no_active_org",
                              "aucune org de contexte pour juger l'effet"),
                DeclaredError(404, "not_a_member",
                              "la personne visée n'est pas membre de cette org"),),
        rest=RestBinding("GET", "/api/me/connectors/{provider}/effect"),
    ),
]
