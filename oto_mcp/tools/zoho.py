"""Zoho CRM — generic CRUD over modules (Contacts, Leads, Deals, Accounts…).

Credential = OAuth2 (self-client) à 3 secrets : client_id + client_secret +
refresh_token → modèle générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential("zoho", want="byo")` (l'ENTITÉ gagnante, pas seulement
les champs — oto#25 lot b2 : elle sert à marquer une ligne rejetée sur un grant mort,
`ZohoAuthError` au refresh). byo_user (pas de quota plateforme : le credential EST
le grant). Le token d'accès est dérivé/caché en mémoire côté client.

**Surface consolidée (ADR 0047 §Amendement, appliqué au connecteur zoho)** : un tool
par OBJET métier, le verbe en paramètre `op` — `zoho_record` (list/get/search/create/
update/delete, tous scopés par `module`) et `zoho_note` (list/create sur un record).
`zoho_modules` reste seul : il ne prend AUCUN paramètre (il énumère les `module` que
les deux autres consomment) et dépend d'un scope OAuth distinct (settings vs data).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Literal, Optional

import requests
from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, providers, status_hints
from ..auth import zoho as zoho_oauth
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import verify as connector_verify

# Modules CRM standard sondés pour prouver un scope de LECTURE réel (au moins un
# `ZohoCRM.modules.<m>.READ`). On passe au 1er lisible ; tous en scope-mismatch =
# le token authentifie mais n'a aucun accès CRM (souvent une clé d'un AUTRE produit
# Zoho — Analytics/Desk). Ordre = du plus universellement présent au moins.
_CRM_PROBE_MODULES = ("Contacts", "Deals", "Accounts", "Leads")


# Zoho héberge par data center régional ; le self-client (client_id/secret) ET le
# refresh token sont liés à leur région d'émission — un self-client `.eu` tapant
# `accounts.zoho.com` est rejeté par Zoho avec un `invalid_client` opaque. Le champ
# `data_center` du credential sélectionne les domaines API/OAuth. Régions reconnues :
_DC_DOMAINS = {
    "com": ("https://www.zohoapis.com", "https://accounts.zoho.com"),
    "eu": ("https://www.zohoapis.eu", "https://accounts.zoho.eu"),
    "in": ("https://www.zohoapis.in", "https://accounts.zoho.in"),
    "au": ("https://www.zohoapis.com.au", "https://accounts.zoho.com.au"),
    "jp": ("https://www.zohoapis.jp", "https://accounts.zoho.jp"),
    "ca": ("https://www.zohoapis.ca", "https://accounts.zohocloud.ca"),
}


def _resolve_dc_domains(data_center: Optional[str]) -> tuple[str, str]:
    """`(api_domain, accounts_url)` pour la région Zoho déclarée. Région manquante ou
    non reconnue → `McpError` actionnable, **jamais** de repli silencieux sur `com` (ce
    repli masquait la vraie cause d'un `invalid_client` : self-client posé sur une autre
    région). `com` reste pleinement valide — on ne force aucune région, on exige juste un
    choix reconnu."""
    dc = (data_center or "").strip().lower()
    if dc not in _DC_DOMAINS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            (f"Data center Zoho non reconnu : {data_center!r}." if dc
             else "Data center Zoho manquant.")
            + " Renseigne ta région dans le champ « Data center » du connecteur Zoho —"
            " l'une de : com, eu, in, au, jp, ca. Elle est visible dans l'URL quand tu es"
            " connecté·e à Zoho (ex. crm.zoho.eu → « eu », crm.zoho.com → « com »)."
        )))
    return _DC_DOMAINS[dc]


def _credential_state_for(connector: str):
    """SOURCE UNIQUE de « ce credential Zoho est-il utilisable ? », par connecteur.

    Connexion en DEUX temps : on pose l'app (client_id + client_secret), puis on
    consent — et c'est le consentement qui produit le refresh_token. L'état
    intermédiaire est donc NORMAL, pas une panne. Un seul libellé, rendu tel quel
    par toutes les surfaces (verdict de la fiche, sonde « tester la connexion »).

    ⚠️ Le consentement ne remplit QUE ce qu'OAuth produit (`zoho_oauth.PERSISTED_FIELDS`) :
    les AUTRES champs requis du connecteur restent à saisir. Analytics exige ainsi un
    `org_id` qu'aucun flux ne peut deviner — c'est l'identifiant de l'org Analytics de
    l'utilisateur. On dérive donc les manques du REGISTRE plutôt que de nommer un champ
    en dur : coder `refresh_token` seul laissait passer pour « complet » un credential
    Analytics inutilisable, et chaque nouveau champ requis rouvrirait le même trou.

    Les champs produits par le flux sont EXCLUS de ce contrôle : ils ont déjà leur
    diagnostic, plus précis (`_resolve_dc_domains` sur la région). Deux messages pour un
    seul problème valent moins qu'un bon."""
    def _state(fields: dict) -> status_hints.CredentialState:
        if fields.get("client_id") and fields.get("client_secret") \
                and not fields.get("refresh_token"):
            return status_hints.CredentialState(
                complete=False, missing=("refresh_token",),
                next_action=("app Zoho enregistrée, mais l'autorisation n'a pas encore "
                             "été donnée — clique « Autoriser oto chez Zoho » sur la "
                             "fiche du connecteur. (Ou colle un refresh token si tu "
                             "utilises un self client.)"))
        con = providers.REGISTRY.get(connector)
        manquants = tuple(f for f in (con.secret_fields if con else ())
                          if f.required and not fields.get(f.name)
                          and f.name not in zoho_oauth.PERSISTED_FIELDS)
        if manquants:
            libelles = ", ".join(f"« {f.label} »" for f in manquants)
            return status_hints.CredentialState(
                complete=False, missing=tuple(f.name for f in manquants),
                next_action=(f"il manque {libelles} sur la fiche du connecteur — "
                             "l'autorisation Zoho ne peut pas le deviner."))
        return status_hints.CredentialState(complete=True)
    return _state


def _zoho_error_hint(exc: Exception) -> str:
    """Traduit l'erreur OAuth Zoho brute en message actionnable pour la sonde."""
    low = str(exc).lower()
    if "invalid_client" in low or "invalid_client_secret" in low:
        return ("client_id / client_secret ou data center incorrect — le self-client "
                "Zoho est lié à sa région, vérifie le champ « data center ».")
    if "invalid_code" in low or "invalid_grant" in low or "invalid_oauthtoken" in low:
        return "refresh token périmé ou révoqué — régénère-le dans la console Zoho."
    return f"échec de connexion Zoho : {exc}"


def _pending_action_for(connector: str):
    """Fabrique le hook `status_hints` d'un connecteur Zoho — le seam passe
    `(sub, org, group, entry)` sans le nom du connecteur, on le capture ici.

    Connexion en DEUX temps (mode server-based) : l'app est posée (client_id +
    client_secret) mais le consentement n'a pas encore été donné → pas de
    refresh_token. Sans ce hook la carte paraîtrait configurée et échouerait au
    premier appel ; avec lui, le front affiche l'étape qui manque."""
    def _hook(sub: str, org, group, entry: dict):  # noqa: ARG001
        if entry.get("mode") == "forbidden":
            return None   # rien de posé → le verdict « à connecter » suffit
        try:
            # `resolve_credential(sub=…)` : le hook tourne depuis /api/me (REST),
            # hors contexte MCP → le sub doit être EXPLICITE. `emit_on_failure=False`
            # (sonde d'affichage, ne fausse pas le signal d'usage).
            f = access.resolve_credential(
                connector, want="byo", sub=sub, emit_on_failure=False).fields
        # noqa: SILENT — sonde d'affichage : sans credential, pas d'action en attente à proposer
        except Exception:  # noqa: BLE001 — fail-open, jamais /api/me en erreur
            return None
        st = _credential_state_for(connector)(f)
        # Le libellé du CTA suit l'étape qui manque : proposer « Autorise oto chez
        # Zoho » à qui a déjà consenti mais à qui il manque un champ enverrait
        # refaire le geste qui vient de réussir.
        if st.complete:
            return None
        return ("Autorise oto chez Zoho" if "refresh_token" in (st.missing or ())
                else st.next_action)
    return _hook


# Les 3 connecteurs Zoho partagent ce mode de connexion — enregistrés ici, ce
# module étant chargé inconditionnellement par `register_all`.
def _start_flow(ctx, connector: str, values: dict) -> dict:
    """Point d'entrée du flux générique — même corps que la capacité `me.zoho_connect`,
    dont il partage le handler pour qu'il n'existe qu'UNE façon de démarrer."""
    from ..auth import flow as oauth_flow
    from ..capabilities import zoho_connect
    # `app` est une clé CACHÉE, pas un `FlowParam` déclaré : le front la passe hors
    # formulaire (le client sait qui il est), elle ne doit jamais devenir un champ
    # visible. Résolue ICI, une seule fois, contre la liste fermée — jamais signée brute.
    return zoho_connect.start_for(
        ctx, connector, (values.get("data_center") or "").lower(),
        oauth_flow.resolve_return_app(values.get("app")))


for _c in ("zoho", "zohodesk", "zohoanalytics"):
    status_hints.register(_c, _pending_action_for(_c))
    status_hints.register_state(_c, _credential_state_for(_c))
    # Le flux de consentement, déclaré comme le reste : le front en dérive un select de
    # région + un bouton, sans savoir que « zoho » existe. Les régions vivent ICI et
    # nulle part ailleurs — elles étaient recopiées jusque dans un libellé de registre
    # qui annonçait un data center que le code rejette.
    connector_flow.declare(
        _c,
        start=lambda ctx, values, _c=_c: _start_flow(ctx, _c, values),
        label="Autoriser oto chez Zoho",
        callback_path="/api/zoho/oauth/callback",
        # Sans région : « une app existe quelque part » (la sienne, celle de son org,
        # ou celle d'oto pour au moins une région). La région n'est choisie qu'au clic,
        # donc la promesse affichée avant le clic ne peut pas en dépendre.
        app_ready=lambda sub, _c=_c: zoho_oauth.has_app(_c, sub),
        params=(connector_flow.FlowParam(
            name="data_center", label="Région de ton compte Zoho", default="eu",
            help="l'app OAuth et le jeton sont liés à leur data center",
            options=tuple((dc, lbl) for dc, lbl in (
                ("eu", "Europe (zoho.eu)"), ("com", "International (zoho.com)"),
                ("in", "Inde (zoho.in)"), ("au", "Australie (zoho.com.au)"),
                ("jp", "Japon (zoho.jp)"), ("ca", "Canada (zohocloud.ca)"),
            ) if dc in _DC_DOMAINS)),
        ),
    )


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001 (config: contrat de sonde, non utilisé ici)
    """Sonde SANS effet de bord, en DEUX temps (auth PUIS scope) :

    1. **refresh du token OAuth** : valide client_id + client_secret + refresh_token +
       data_center d'un coup (échec → message actionnable via `_zoho_error_hint`) ;
    2. **lecture réelle d'un module CRM** (`GET /crm/v7/<module>?per_page=1`) : un token
       peut authentifier mais n'avoir AUCUN scope CRM (ex. une clé Zoho **Analytics** posée
       par erreur sur le connecteur CRM — vécu 2026-07-04, la sonde auth-seule donnait un
       faux « ok »). Si TOUS les modules renvoient `OAUTH_SCOPE_MISMATCH`, on lève en
       incluant le **scope réellement accordé** (renvoyé par le refresh) → immédiatement
       diagnostiquable. Aucun effet de bord (lectures `per_page=1`).

    `_resolve_dc_domains` lève déjà une `McpError` claire si la région manque/est inconnue.
    """
    from oto.tools.zoho.client import ZohoClient

    status_hints.require_complete("zoho", fields)
    api_domain, accounts_url = _resolve_dc_domains(fields.get("data_center"))

    # 1) auth — refresh brut : valide les 4 champs ET capte le `scope` accordé (le
    # refresh Zoho le renvoie), pour un message de scope actionnable si besoin.
    try:
        # ⚠️ `data=` et JAMAIS `params=` : en query string, client_id/client_secret/
        # refresh_token atterrissent dans l'URL — donc dans le message de toute
        # exception requests (ConnectionError, HTTPError…), qui est ici renvoyé à
        # l'agent et journalisé. Fuite vécue (#284) ; cf. `oto.tools.zoho.auth`.
        tok = requests.post(f"{accounts_url}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "client_id": fields.get("client_id"),
            "client_secret": fields.get("client_secret"),
            "refresh_token": fields.get("refresh_token"),
        }, timeout=20).json()
    except Exception as e:  # noqa: BLE001 — réseau / réponse illisible
        raise ValueError(f"échec de connexion Zoho : {type(e).__name__}") from e
    if "access_token" not in tok:
        raise ValueError(_zoho_error_hint(tok.get("error") or tok))
    granted = tok.get("scope", "")

    # 2) scope — LECTURE réelle par le MÊME chemin que le tool `zoho_record`
    # (`list_records` ajoute les `fields` par défaut, requis en API v7) : au moins un
    # module CRM lisible = credential utilisable. `per_page=1`, sans effet de bord.
    client = ZohoClient(
        client_id=fields.get("client_id"), client_secret=fields.get("client_secret"),
        refresh_token=fields.get("refresh_token"),
        api_domain=api_domain, accounts_url=accounts_url,
    )
    scope_missing = False
    for module in _CRM_PROBE_MODULES:
        try:
            client.list_records(module, page=1, per_page=1)
            return  # lecture réelle OK → credential utilisable
        except McpError:
            raise
        # noqa: SILENT — scope absent pour CE module ⇒ essayer le suivant, verdict rendu à la fin
        except Exception as e:  # noqa: BLE001 — l'erreur provider EST le retour de la sonde
            if "OAUTH_SCOPE_MISMATCH" in str(e):
                scope_missing = True   # scope absent pour CE module — essayer le suivant
            # autre erreur (module désactivé, INVALID_MODULE…) → tenter le suivant
    if scope_missing:
        extra = f" (scope accordé : {granted})" if granted else ""
        raise ValueError(
            "le token authentifie mais n'a aucun scope de lecture CRM" + extra
            + " — c'est peut-être une clé d'un autre produit Zoho (Analytics/Desk). "
            "Régénère un self-client Zoho CRM avec ZohoCRM.modules.ALL "
            "(ou leads/contacts/deals/accounts.READ).")
    raise ValueError("connexion Zoho établie mais aucun module CRM lisible "
                     "(modules désactivés ou inaccessibles).")


def _demarque_apres_refresh(rc):
    """Démarque la ligne de coffre quand le refresh Zoho RÉUSSIT (oto#25 lot b3).

    Symétrique du câblage Salesforce, qui n'existait pas ici : le démarquage
    excluait Zoho, faute de savoir quand un credential recommence à marcher. Une
    ligne marquée rejetée restait rouge jusqu'à une re-pose manuelle, même après
    que son propriétaire ait réparé son application.

    ⚠️ Le rappel n'est invoqué qu'après un refresh RÉUSSI, et jamais sur un succès
    de cache (garanti côté oto-core) : c'est ce qui en fait une preuve de vie et
    non une information périmée. Le cache Zoho dure une heure — un jeton valide
    prouve un refresh d'il y a une heure, pas un credential sain maintenant.

    Au niveau MODULE, hors de `register()` : une fabrique enfermée dans la closure
    ne s'éprouve qu'en montant tout le connecteur, et c'est précisément le genre de
    pièce qu'on veut pouvoir attaquer seule.
    """
    if rc.entity_type is None:
        return None              # grant plateforme : aucune ligne de coffre à marquer

    def _demarque(_token_data: dict) -> None:
        connector_health.record_health(
            "zoho", (rc.entity_type, rc.entity_id, rc.account), True, None)

    return _demarque


def register(mcp: FastMCP) -> None:
    connector_verify.register("zoho", _verify)
    from oto.tools.zoho import ZohoAuthError
    from oto.tools.zoho.client import ZohoClient

    def _client() -> tuple[ZohoClient, "access.ResolvedCredential"]:
        # `resolve_credential(want="byo")` — pas `resolve_credential_fields`, qui n'en
        # est qu'une vue mince (mêmes champs, cascade user > groupe > org identique,
        # cf. `access/views.py`) — parce qu'on a besoin de l'ENTITÉ gagnante pour
        # marquer une ligne rejetée (oto#25 lot b2, `rc` rendu à l'appelant).
        rc = access.resolve_credential("zoho", want="byo")
        creds = rc.fields
        # Connexion en DEUX temps : l'app peut être posée sans que le consentement ait
        # été donné (pas de refresh_token). Partir quand même produisait un échec OAuth
        # opaque au premier appel ; on rend l'ÉTAPE MANQUANTE, avec le libellé unique de
        # `_zoho_credential_state` — le même que celui de la fiche et de la sonde.
        state = _credential_state_for("zoho")(creds)
        if not state.complete:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=state.next_action))
        api_domain, accounts_url = _resolve_dc_domains(creds.get("data_center"))
        client = ZohoClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            api_domain=api_domain,
            accounts_url=accounts_url,
            on_refresh=_demarque_apres_refresh(rc),
        )
        return client, rc

    @contextmanager
    def _marks_rejection(rc):
        """Sur `ZohoAuthError` (refus du REFRESH — grant mort ; jamais un 401 nu d'un
        geste applicatif ordinaire, avec une clé par ailleurs saine — cf.
        `oto.tools.zoho.auth`, seul point qui la lève), marque la ligne DE COFFRE
        réellement servie rejetée (`connectors.health.mark_rejected`, même garde de
        portée que `verify`), PUIS RE-LÈVE — marquer n'est jamais un fallback qui
        avale l'erreur réelle (oto#25 lot b2)."""
        try:
            yield
        except ZohoAuthError as e:
            connector_health.mark_rejected(
                rc.entity_type, rc.entity_id, "zoho", rc.account, str(e))
            raise

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback."""
        if value is None:
            raise _bad(f"op='{op}' requiert {name}")
        return value

    @mcp.tool()
    def zoho_modules() -> dict:
        """List the available CRM modules (Contacts, Leads, Deals, Accounts…).

        Reads Zoho's module metadata, which needs the **settings** scope on the
        self-client — `ZohoCRM.settings.modules.READ` (or `ZohoCRM.settings.ALL`).
        A token minted with only data scopes (`ZohoCRM.modules.ALL`) reads records
        fine (`zoho_record`) but is rejected here; regenerate the self-client with
        the settings scope added.
        """
        from oto.tools.common.errors import UpstreamHTTPError
        client, rc = _client()
        try:
            with _marks_rejection(rc):
                return {"modules": client.list_modules()}
        except UpstreamHTTPError as e:
            if "OAUTH_SCOPE_MISMATCH" in str(e.body):
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    "le token Zoho n'a pas le scope métadonnées `ZohoCRM.settings.modules.READ` "
                    "(ou `ZohoCRM.settings.ALL`) requis pour lister les modules — les données "
                    "(`zoho_record`) restent lisibles. Régénère le self-client Zoho CRM en "
                    "ajoutant ce scope settings à côté des scopes data.")))
            raise

    @mcp.tool()
    def zoho_record(
        module: str,
        op: Literal["list", "get", "search", "create", "update", "delete"] = "list",
        record_id: Optional[str] = None,
        data: Optional[dict] = None,
        criteria: Optional[str] = None,
        page: int = 1,
        per_page: int = 200,
        fields: Optional[str] = None,
    ) -> dict:
        """A CRM record inside a module — list, read, search, create, update, delete.

        `op`:
        - **"list"** (default): list records from a module. Paginated
          (`page` / `per_page`).
        - **"get"**: get one record by id (`record_id`). {} if not found.
        - **"search"**: search records. `criteria` = Zoho criteria, e.g.
          "(Email:equals:a@b.com)" or "(Last_Name:starts_with:Dup)". Paginated.
        - **"create"**: create a record in a module (`data` = field → value).
        - **"update"**: update a record's fields (`record_id` + `data`).
        - **"delete"**: delete a record. Irreversible.

        Args:
            module: e.g. "Contacts", "Leads", "Deals", "Accounts".
            op: list (default) | get | search | create | update | delete.
            record_id: op="get"/"update"/"delete" — the record id.
            data: op="create"/"update" — field → value.
            criteria: op="search" — Zoho criteria, e.g. "(Email:equals:a@b.com)"
                or "(Last_Name:starts_with:Dup)".
            page: pagination (list, search).
            per_page: page size (list, search).
            fields: op="list" — comma-separated field names. Optional — a sensible
                default set is used per known module if omitted.
        """
        client, rc = _client()

        with _marks_rejection(rc):
            if op == "list":
                return client.list_records(module, page=page, per_page=per_page,
                                           fields=fields)
            if op == "get":
                return client.get_record(module, _need(record_id, "record_id", op))
            if op == "search":
                return client.search_records(module, _need(criteria, "criteria", op),
                                             page=page, per_page=per_page)
            if op == "create":
                return client.create_record(module, _need(data, "data", op))
            if op == "update":
                return client.update_record(module, _need(record_id, "record_id", op),
                                            _need(data, "data", op))
            if op == "delete":
                return client.delete_record(module, _need(record_id, "record_id", op))
            raise _bad("op doit être 'list', 'get', 'search', 'create', 'update' "
                       "ou 'delete'")

    @mcp.tool()
    def zoho_note(
        module: str,
        record_id: str,
        op: Literal["list", "create"] = "list",
        title: Optional[str] = None,
        content: Optional[str] = None,
    ) -> dict:
        """The notes attached to a CRM record.

        `op`:
        - **"list"** (default): list the notes attached to a record.
        - **"create"**: add a note to a record (`title` + `content`).

        Args:
            module: e.g. "Contacts", "Leads", "Deals", "Accounts".
            record_id: the record the notes are attached to.
            op: list (default) | create.
            title: op="create" — the note title.
            content: op="create" — the note body.
        """
        client, rc = _client()

        with _marks_rejection(rc):
            if op == "list":
                return {"notes": client.list_notes(module, record_id)}
            if op == "create":
                return client.create_note(module, record_id,
                                          _need(title, "title", op),
                                          _need(content, "content", op))
            raise _bad("op doit être 'list' ou 'create'")
