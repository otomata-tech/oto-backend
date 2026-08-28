"""La résolution RÉELLE d'un credential — le chemin chaud (ADR 0024/0038).

`resolve_credential` est la vue publique ; `_resolve_credential_impl` marche la
cascade UNE fois avec la sonde de fetch (seul le gagnant est déchiffré) et rend
un `ResolvedCredential` (clé + origine + config non-secrète). Trois chemins
court-circuitent la marche, dans cet ordre de spécificité : l'instance épinglée
par l'appel (`_instance=`), celle bindée par le projet, puis la cascade.
`_resolve_credential_anon` en est le miroir org-only, pour l'endpoint MCP publié
(ADR 0032) où il n'y a personne dont on puisse prendre le compte par défaut.

Dépend de tout ce qui est en dessous : `scope` (contexte, épinglages du projet),
`rbac` (backstop RBAC, garde d'instance, hint d'erreur), `cascade` (le walker),
`quotas` (le plafond du palier plateforme). Les vues minces qui s'appuient
dessus (`resolve_api_key`, `resolve_credential_fields`…) vivent dans `views`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import (providers, credentials_store, db, group_store, instance_refs, org_store, session_org)
from . import cascade, quotas, rbac, scope

logger = logging.getLogger(__name__)


_ACCOUNT_URL = "https://manage.oto.cx/account"


@dataclass(frozen=True)
class ResolvedCredential:
    """Credential GAGNANT de la cascade (ADR 0024) — la clé, son origine, ET sa
    config non-secrète (endpoint/host) en un seul objet. Source unique : toute
    résolution (clé seule, multi-champs, ou endpoint) en dérive.

    - `secret` : la valeur stockée brute (la clé pour un keyed ; le pack JSON pour
      un multi-champs). `key` = alias (un keyed s'instancie avec).
    - `is_platform` / `mode` : origine (user|group|org|platform) — miroir de `status_for`.
    - `fields` (lazy) : champs unpackés (un client multi-secrets s'instancie avec).
    - `config` (lazy) : champs NON-secrets déclarés (data_center, base_url…) ∪ `meta`
      public du credential (ex. `dsn` unipile). La config voyage avec la clé.
    - `entity_type`/`entity_id` : niveau gagnant (None pour un grant plateforme — sa
      config est l'environnement, pas un credential du coffre)."""
    provider: str
    secret: str
    is_platform: bool
    mode: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    account: str = ""

    @property
    def key(self) -> str:
        return self.secret

    @property
    def fields(self) -> dict:
        return credentials_store.unpack_secret(self.provider, self.secret)

    @property
    def config(self) -> dict:
        """Config non-secrète appariée à la clé gagnante. Lazy : aucun coût pour
        les appelants qui ne lisent que `key` (chemin chaud resolve_api_key)."""
        _, cfg = credentials_store.split_secret_config(self.provider, self.fields)
        if self.entity_type is not None:
            try:
                row = credentials_store.get_credential_with_meta(
                    self.entity_type, self.entity_id, self.provider, self.account)
            # noqa: SILENT — config non-secrète absente ⇒ la clé gagnante reste utilisable
            except Exception:
                row = None
            if row:
                cfg = {**cfg, **credentials_store.public_meta(row.get("meta"))}
        return cfg


def resolve_credential(provider: str, want: str = "auto",
                       sub: Optional[str] = None, *,
                       account: Optional[str] = None,
                       emit_on_failure: bool = True) -> ResolvedCredential:
    """Vue publique de la résolution. Sur **échec** (McpError actionnable — credential
    absent / quota dépassé / accès RBAC refusé), émet un événement de monitoring
    `kind='connector'` dans le flux unifié (ADR 0017) AVANT de relever : c'est LE
    signal d'un connecteur qui ne résout pas pour un user/org, invisible jusqu'ici
    (un compte actif sans clé valide n'apparaissait nulle part). `emit_on_failure=False`
    pour les **sondes** qui avalent la McpError (ex. lookup de DSN), afin de ne pas
    fausser le signal. Cascade et sémantique : voir `_resolve_credential_impl`."""
    if sub is None:
        # Endpoint MCP ANONYME (ADR 0032) : pas de sub → résolution contre l'org
        # propriétaire du projet (org secret > grant org > clé plateforme ouverte),
        # sans quota per-sub (le rate-limit du sous-domaine borne l'abus).
        from .. import subdomain_project
        anon = subdomain_project.current_anon_context()
        if anon is not None:
            return _note_resolved_instance(
                _resolve_credential_anon(provider, want, anon.org_id))
    sub = sub or scope.current_user_sub_or_raise()
    try:
        resolved = _resolve_credential_impl(provider, want, sub, account=account)
    except McpError:
        if emit_on_failure:
            _emit_connector_failure(provider, sub)
        raise
    return _note_resolved_instance(resolved)


def _note_resolved_instance(rc: ResolvedCredential) -> ResolvedCredential:
    """Verse au relevé de l'appel le ref de la ligne du coffre qui a RÉELLEMENT servi
    (allowlist `server._TRACED_ARGS`) — l'empreinte que le journal ne portait pas.

    Le journal disait quel outil et quelle org ; jamais SOUS QUELLE CLÉ. Or c'est
    précisément la question d'une bascule d'accès (« l'appel est-il passé par
    l'arête ou par l'ancien chemin ? ») et celle d'un incident de credential
    (« quelle instance a été appelée ? »). Best-effort et no-op hors appel MCP : un
    relevé ne fait jamais échouer une résolution."""
    try:
        ref = instance_refs.ref_for_credential(
            rc.entity_type or "", rc.entity_id or "", rc.provider, rc.account)
        # `instance` = l'empreinte pour le JOURNAL. `resolved_*` = de quoi le dire à
        # l'AGENT au retour de l'appel (écho `_account`, `CallContextMiddleware`) :
        # sans ça il poste sur l'un de ses deux workspaces sans jamais savoir lequel.
        # Le connecteur est noté AVEC le compte : un outil composite peut résoudre un
        # credential auxiliaire, et l'écho ne doit annoncer que le connecteur appelé.
        session_org.note_call_trace(instance=ref, resolved_connector=rc.provider,
                                    resolved_account=rc.account)
    except Exception:  # noqa: BLE001
        logger.debug("relevé d'instance échoué", exc_info=True)
    return rc


def _emit_connector_failure(provider: str, sub: str) -> None:
    """Best-effort : une ligne `tool_calls(kind='connector', ok=False)` = « la
    résolution de credential a échoué pour ce provider/sub ». Jamais bloquant, jamais
    d'exception qui masquerait la McpError d'origine (le monitoring ne casse pas le service)."""
    try:
        org = scope.current_org(sub)
    # noqa: SILENT — le signal d'usage ne casse jamais la résolution qu'il observe
    except Exception:
        org = None
    try:
        db.insert_tool_call({
            "kind": "connector", "tool": provider, "sub": sub, "org_id": org,
            "ok": False, "error": "credential_resolution_failed",
        })
    except Exception:  # noqa: BLE001
        logger.debug("connector failure emit failed", exc_info=True)


def _resolve_credential_impl(provider: str, want: str, sub: str,
                             account: Optional[str] = None) -> ResolvedCredential:
    """Résolveur substrat unique (ADR 0024) : marche la cascade EXACTE
    user > groupe actif > org active [> grant plateforme] **une fois** et renvoie
    le credential gagnant (clé + origine + config). `want="byo"` court-circuite le
    palier plateforme (sémantique byo-only de `resolve_credential_fields`) ;
    `want="auto"` inclut le grant plateforme + quota (sémantique `resolve_api_key`).
    `sub` explicite = utilisable HORS contexte MCP (routes REST) ; None = sub courant.
    `account` sélectionne le compte au palier MEMBRE en multi-compte (« 2 Zoho ») —
    None ⇒ épinglage projet, sinon compte unique auto, sinon McpError (voir plus bas).
    Lève une McpError actionnable si rien ne résout."""
    sub = sub or scope.current_user_sub_or_raise()
    # RBAC connecteur interne à l'org (ADR 0025) — backstop DUR : un connecteur
    # restreint dans l'org du sub n'est résolu que pour les principals autorisés
    # (département/user). Avant toute résolution → couvre keyed/fields/BYO.
    rbac.require_connector_access(provider, sub)

    # Instance EXPLICITE de l'appel (`_instance=`, ADR 0038 §C/B6) : si le ref épinglé
    # vise CE provider, on résout EXACTEMENT cette ligne du coffre — jamais de
    # fallback (une instance demandée qui ne résout pas = erreur actionnable, pas
    # une autre identité). Un ref d'un AUTRE provider est ignoré ici (il ne visait
    # pas cette résolution — ex. résolution auxiliaire d'un tool composite).
    # ⚠️ La comparaison se fait sur le PORTEUR du credential (délégation) : un ref
    # d'instance nomme une ligne du COFFRE, et les six canaux unipile n'en ont pas —
    # leurs clés vivent sous `unipile`. Comparer au nom nu ferait silencieusement
    # ignorer le pin sur tout appel de canal (l'appel repartirait en cascade, donc
    # potentiellement sur une AUTRE clé que celle demandée).
    porteur = providers.credential_provider(provider)
    pinned = session_org.current_call_instance()
    if pinned is not None and getattr(pinned, "connector", None) == porteur:
        return _resolve_pinned_instance(provider, sub, pinned)

    # Binding de PROJET (ADR 0038 B5) : le projet de l'appel (`_project=`) binde une
    # instance pour ce provider → résolution EN DUR, RE-GARDÉE pour l'APPELANT (le
    # binding a été gardé pour celui qui l'a posé ; l'appelant d'un projet partagé
    # peut être un autre membre). `_instance=` explicite (ci-dessus) prime — le jeton
    # le plus spécifique de l'appel.
    bound = scope.project_pinned_instance(porteur)
    if bound is not None:
        rbac.guard_instance_access(sub, bound)
        return _resolve_pinned_instance(provider, sub, bound)

    # Scope MEMBRE (ADR 0033) : « ma clé » n'existe QUE dans l'org de contexte —
    # posée dans l'org A, elle ne résout pas depuis l'org B. L'org est résolue via
    # le seam `current_org` (session MCP ?? consultation ?? maison, ADR 0023) AVANT
    # le premier palier : plus aucun credential per-user org-agnostique.
    active_org = scope.current_org(sub)

    # Compte NOMMÉ par l'appelant — account explicite (param) > axe d'appel
    # `_account=` (#108) > épinglage projet — résolu UNE fois, avant la marche :
    # il sert à chaque palier (`_pick_account`) ET à la garde post-marche (un
    # compte nommé introuvable partout LÈVE, jamais un repli — review #399 F2).
    # None = rien de nommé (sélection automatique par palier).
    named_account = None
    if cascade._is_multi_account(provider):
        named_account = (account if account is not None
                         else session_org.current_call_account()
                         or scope.project_pinned_identity(provider))

    def _pick_account(entity_type: str, entity_id: str, mprov: str, where: str,
                      scope: Optional[str] = None) -> tuple:
        """Le compte EFFECTIF d'un palier multi-compte = compte nommé (cf.
        `named_account`) > compte unique auto > défaut posé (`oto_identity(op='set')`)
        > McpError — jamais de repli muet vers un AUTRE compte (anti-usurpation).
        '' = mono legacy. Renvoie `(compte, explicite)` : un compte NOMMÉ par
        l'appelant peut vivre à un palier plus bas (Phase 2 : l'org a « eu », le
        membre non) — le palier qui ne l'a pas passe la main, et c'est la garde
        POST-MARCHE qui lève s'il n'existe nulle part. Un compte choisi
        automatiquement n'est jamais cherché ailleurs."""
        if named_account is not None:
            return named_account, True
        return cascade._shared_auto_account(entity_type, entity_id, mprov, where, scope), False

    def _not_found(eff: str, mprov: str) -> McpError:
        noun = cascade.account_noun(mprov).capitalize()
        return McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"{noun} `{eff}` introuvable pour `{mprov}` — vérifie avec "
                f"oto_identity(op='list'), ou pose-le sur {_ACCOUNT_URL}."
            )))

    def _member_fetch(msub: str, morg: int, mprov: str) -> Optional[tuple]:
        """Sonde MEMBRE du fetch : sélection du compte en multi-compte (« 2 Zoho »),
        cf. `_pick_account`. Un compte explicite/épinglé introuvable LÈVE (on
        n'agit pas sous une autre identité).

        Une instance SUSPENDUE (lot 2 / ADR 0044 §KeyStack) est traitée comme
        absente : le barreau membre passe son tour et le niveau du dessous
        (groupe/org/plateforme) prend le relais — même verdict que les sondes
        PRESENCE/FETCH, sinon la résolution réelle contredit ce que le KeyStack
        annonce (#401). Suspendre est un acte du membre sur SA clé : le relais est
        le contrat, pas une usurpation. (Compte NOMMÉ suspendu : le relais ne va
        jamais jusqu'à la clé plateforme — la garde post-marche du compte nommé
        lève, review #399 F2.)"""
        if not cascade._is_multi_account(mprov):
            key = db.get_member_api_key(msub, morg, mprov)
            if key and db.member_instance_suspended(msub, morg, mprov):
                return None
            return (key, "") if key else None
        eff, explicit = _pick_account(credentials_store.MEMBER,
                                      credentials_store.member_id(morg, msub),
                                      mprov, "dans cette org")
        key = db.get_member_api_key(msub, morg, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        # Suspension PAR compte (le `account` de providers.instances.suspend) :
        # une clé existante mais mise de côté saute le barreau — après le check
        # « introuvable », qui garde sa sémantique (absent ≠ suspendu).
        if key and db.member_instance_suspended(msub, morg, mprov, eff):
            return None
        # Nommé mais absent ici : peut-être une clé partagée (équipe/org) — on
        # passe la main, sans jamais prendre un autre compte à ce palier.
        return (key, eff) if key else None

    def _group_fetch(gid: int, mprov: str):
        """Sonde ÉQUIPE du fetch : même sélection de compte que le membre (Phase 2).
        Mono-compte → la lecture historique (account='')."""
        if not cascade._is_multi_account(mprov):
            return group_store.get_group_secret(gid, mprov)
        eff, explicit = _pick_account("group", str(gid), mprov, "pour ton équipe",
                                      scope="group")
        key = group_store.get_group_secret(gid, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        return (key, eff) if key else None

    def _org_fetch(oid: int, mprov: str):
        """Sonde ORG du fetch : même sélection de compte que le membre (Phase 2).
        Un compte NOMMÉ absent ici passe la main comme les autres paliers — c'est
        la garde post-marche qui lève (le walker peut ne jamais atteindre ce
        barreau : org de contexte None, connecteur non org-partageable)."""
        if not cascade._is_multi_account(mprov):
            return org_store.get_org_secret(oid, mprov)
        eff, explicit = _pick_account("org", str(oid), mprov, "pour ton org",
                                      scope="org")
        key = org_store.get_org_secret(oid, mprov, eff)
        if eff and not key and not explicit:
            raise _not_found(eff, mprov)
        return (key, eff) if key else None

    # Marche unique de la cascade (walker) — la sonde fetch ne déchiffre que le
    # gagnant ; le palier membre porte la sélection multi-compte ci-dessus.
    # `group` passé en LAZY : l'équipe active (lookup DB) n'est résolue que si
    # aucun barreau plus proche n'a gagné.
    probe = cascade.CascadeProbe(member=_member_fetch, member_cross=cascade.FETCH_PROBE.member_cross,
                         group=_group_fetch, org=_org_fetch,
                         platform=cascade.FETCH_PROBE.platform)
    win = cascade.cascade_winner(sub, provider, org=active_org,
                         group=lambda: scope.current_group(sub),
                         probe=probe, want=want)

    # Garde post-marche (review #399 F2) : un compte NOMMÉ (param/axe/épinglage)
    # qui n'a gagné à AUCUN palier à clé lève « introuvable » — jamais une clé
    # PLATEFORME en silence (le palier plateforme n'a pas de comptes : y répondre
    # sous un autre credential que celui demandé serait une usurpation), jamais le
    # message générique « aucune clé ». Couvre les barreaux que le walker n'atteint
    # pas : org de contexte None, connecteur multi non org-partageable (google,
    # browser, planity…).
    if named_account and (win is None or win.mode == "platform"):
        raise _not_found(named_account, provider)

    if win is None:
        # byo-only : pas de palier plateforme (mounts basic_auth, multi-secrets).
        if want == "byo":
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"Aucun credential `{provider}` configuré pour toi. Renseigne-le "
                    f"sur {_ACCOUNT_URL} (section {provider.capitalize()})."
                    + rbac._reachable_hint(sub, active_org, provider)
                ),
            ))
        # Défense en profondeur : le palier plateforme n'existe que si le registre
        # AUTORISE `platform` (gate DANS le walker) — un provider byo-only n'est
        # JAMAIS résolu via une clé plateforme résiduelle (audité 2026-06-11).
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            # La clé se pose sur le PORTEUR (délégation) : renvoyer quelqu'un à
            # « la section Whatsapp » de sa page compte, où il n'y a pas de champ,
            # est un cul-de-sac. Le hint « une équipe a la clé » se cherche lui aussi
            # sous le porteur — c'est là que les secrets partagés existent.
            message=(
                f"Aucune clé `{porteur}` configurée pour toi. Soit pose "
                f"ta propre clé sur {_ACCOUNT_URL} (section {porteur.capitalize()}), "
                f"soit demande à un admin de te grant un accès à une clé plateforme."
                + rbac._reachable_hint(sub, active_org, porteur)
            ),
        ))

    if win.mode != "platform":
        return ResolvedCredential(provider, win.payload, False, win.mode,
                                  win.entity_type, win.entity_id, account=win.account)

    # ADR 0044 §F R3 : le palier plateforme lit les instances scope PLATFORM du
    # coffre unifié (share_mode/share_down = accès ; meta.rate_limit* = quota).
    # Le secret n'est déchiffré que pour l'instance gagnante.
    grant = win.payload
    used = quotas.usage_today(sub, provider)
    limit = grant.get("daily_quota") or quotas.quota_for(provider)
    # ADR 0043 : une org abonnée à un plan `unmetered` n'a PLUS de quota sur les
    # clés plateforme — fin du micro-management des « credits d'appel ». Le plan
    # est le seul cran ; hors abonnement, les quotas d'essai tiennent.
    if limit and active_org is not None and quotas._org_unmetered(active_org):
        limit = 0
    if limit and used >= limit:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Quota plateforme {provider} dépassé aujourd'hui ({used}/{limit}) "
                f"pour la clé `{grant['label']}`. Pose ta propre clé sur {_ACCOUNT_URL} "
                f"pour continuer sans limite."
            ),
        ))

    return ResolvedCredential(provider, grant["secret"], True, "platform",
                              credentials_store.PLATFORM, grant["label"])


def _resolve_pinned_instance(provider: str, sub: str, ref) -> ResolvedCredential:
    """Résolution EN DUR d'une instance explicite (`_instance=` OU binding de projet,
    ADR 0038 B6/B5) : lit exactement la ligne du coffre que le ref désigne. L'ACCÈS
    a été gardé par `guard_instance_access` (à la pose pour l'axe ; re-gardé pour
    l'APPELANT sur le chemin binding) ; le RBAC connecteur (ADR 0025) a été rejoué
    par l'appelant. Ligne absente = McpError actionnable, JAMAIS de fallback vers
    un autre palier (§C : agir sous une autre identité que celle demandée est
    interdit)."""
    from .. import instance_refs
    if ref.level == "member":
        etype, eid = credentials_store.MEMBER, credentials_store.member_id(ref.org_id, sub)
        mode = "user"
    elif ref.level == "group":
        etype, eid, mode = "group", str(ref.group_id), "group"
    elif ref.level == "org":
        etype, eid, mode = "org", str(ref.org_id), "org"
    else:  # platform — refusé dès la pose par l'axe ; défense en profondeur ici.
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Ref d'instance `platform:` non résoluble en `_instance=` (B6)."))
    secret = credentials_store.get_credential(etype, eid, provider, ref.account)
    if not secret:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'instance `{instance_refs.format_ref(ref)}` ne résout plus "
                     "(credential retiré ou compte renommé ?). Reliste avec "
                     "oto_instance(op='list') — pas de repli vers une autre identité.")))
    return ResolvedCredential(provider, secret, False, mode, etype, eid,
                              account=ref.account)


def _resolve_credential_anon(provider: str, want: str, org_id: Optional[int]) -> ResolvedCredential:
    """Résolution pour un endpoint MCP ANONYME (ADR 0032) : aucun `sub`, aucune session
    per-user → cascade réduite `org_secret > grant plateforme d'org > clé plateforme
    ouverte`, scopée sur l'org PROPRIÉTAIRE du projet. Pas de user_key/group (inexistants
    sans identité), pas de quota per-sub (le rate-limit du sous-domaine borne l'abus).
    Miroir org-only des paliers de `_resolve_credential_impl` — ce qui n'est pas résoluble
    au niveau org (oauth/cookie per-user) lève une McpError actionnable, fail-closed."""
    con = providers.connector_for_provider(provider)
    if con is None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Provider inconnu: {provider}"))
    if org_id is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme n'a pas d'org propriétaire pour résoudre "
                     f"`{provider}` (projet sans org).")))
    # Walker avec sub=None : les barreaux membre/groupe se sautent d'eux-mêmes →
    # cascade réduite org > plateforme (ADR 0044 §F R3 : anon → instance 'open'
    # free-tier, ou 'closed' dont le share_down vise `org:<org_id>`).
    # ⚠️ Le barreau org sélectionne son COMPTE comme le chemin réel (`_org_fetch` :
    # unique/`is_default`), jamais `''` en dur — `ensure_named_coexistence` migre la
    # ligne mono vers « principal » au premier compte nommé, et l'endpoint anonyme
    # cessait alors de résoudre pendant que `has_org_secret` disait « configuré »
    # (review #399 F3). Pas de compte nommable ici : aucun sub, aucun axe d'appel.
    def _anon_org_fetch(oid: int, mprov: str):
        # Mono D'ABORD : la ligne `''` historique répond sans lire la table des
        # comptes (zéro coût ajouté pour les orgs pré-migration, et le contrat des
        # tests qui stubbent `get_org_secret` seul reste entier). La sélection
        # nommée n'est tentée QUE si la ligne mono manque — le cas F3, où
        # `ensure_named_coexistence` l'a migrée vers « principal ».
        key = org_store.get_org_secret(oid, mprov)
        if key or not cascade._is_multi_account(mprov):
            return key
        eff = cascade._shared_auto_account("org", str(oid), mprov,
                                   "pour l'org de ce projet", scope="org")
        if not eff:
            return None
        key = org_store.get_org_secret(oid, mprov, eff)
        return (key, eff) if key else None

    probe = cascade.CascadeProbe(member=cascade.FETCH_PROBE.member,
                         member_cross=cascade.FETCH_PROBE.member_cross,
                         group=cascade.FETCH_PROBE.group, org=_anon_org_fetch,
                         platform=cascade.FETCH_PROBE.platform)
    win = cascade.cascade_winner(None, provider, org=org_id, group=None,
                         probe=probe, want=want)
    if win is None:
        if want == "byo":
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Aucun credential `{provider}` configuré pour l'org de ce projet."))
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme ne peut pas résoudre `{provider}` : configure "
                     f"une clé d'org, ou grant une clé plateforme à l'org du projet.")))
    if win.mode == "org":
        return ResolvedCredential(provider, win.payload, False, "org", "org",
                                  str(org_id), account=win.account)
    grant = win.payload
    return ResolvedCredential(provider, grant["secret"], True, "platform",
                              credentials_store.PLATFORM, grant["label"])
