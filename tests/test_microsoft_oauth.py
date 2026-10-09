"""Le porteur Microsoft 365 (`microsoft`) et ses services — OAuth délégué, par
personne, plusieurs comptes par personne (oto-backend#23), un consentement par carte.

⚠️ Le cœur est MOQUÉ à sa frontière (`oto.tools.microsoft` posé dans
`sys.modules`, avec un faux `scopes` qui suit le contrat de la lib) et le coffre est un
faux en mémoire : aucun appel réel à Microsoft ni à la base n'est joué ici. La
RÉSOLUTION du compte, elle, est la vraie (`access.resolve_credential`, celle de tout
connecteur multi-compte, sous le service appelé). Vérifié : le registre (porteur +
service), les scopes par carte, le flux hébergé (state, carte de retour, annuaire du
client), le retour de connexion (un second compte s'AJOUTE, le même se remplace, les
scopes s'unissent), le refus d'un service non autorisé, le choix du compte à l'appel,
le renouvellement (`.default` sur l'annuaire du compte, scopes relus, cache, rotation)
et l'autorisation morte isolée à son compte, le lien d'approbation d'administrateur et
son retour (aucune écriture).
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

PORTEUR = "microsoft"
SERVICE = "sharepoint"
ORG = 42
SUB = "user-de-test"
MEMBRE = f"{ORG}:{SUB}"
_COORDONNEES = {"client_id": "app-id-fictif", "client_secret": "secret-fictif"}
_RETOUR = "https://mcp.exemple.test/api/microsoft/oauth/callback"
JANE = {"id": "id-jane", "displayName": "Jane Doe", "mail": "Jane@Contoso.example",
        "userPrincipalName": "jane@contoso.example"}
JOHN = {"id": "id-john", "displayName": "John Roe", "mail": None,
        "userPrincipalName": "john@fabrikam.example"}
G = "https://graph.microsoft.com/"
# Ce qu'Entra rendait aux connexions SharePoint d'avant le porteur (`meta.scopes` en prod).
SCOPE_FICHIERS = ("Files.ReadWrite.All Sites.ReadWrite.All User.Read profile openid "
                  "email offline_access")
SCOPE_COURRIER = f"{G}Mail.ReadWrite {G}Mail.Send User.Read offline_access"


def _grant(access="AT1", refresh="RT1", expires_in=3600, scope=SCOPE_FICHIERS):
    return types.SimpleNamespace(access_token=access, refresh_token=refresh,
                                 expires_in=expires_in, scope=scope)


def _faux_scopes():
    """Le module `scopes` de la lib, au contrat : tuples d'URI complètes, `normalize`
    et `short` en noms COURTS (préfixe Graph retiré, casse canonique)."""
    s = types.ModuleType("oto.tools.microsoft.scopes")
    s.GRAPH = G
    s.IDENTITY = ("offline_access", "User.Read")
    s.FILES = (G + "Files.ReadWrite.All", G + "Sites.ReadWrite.All")
    s.MAIL = (G + "Mail.ReadWrite", G + "Mail.Send")
    s.CALENDAR = (G + "Calendars.ReadWrite",)
    s.TEAMS = (G + "Team.ReadBasic.All", G + "Chat.ReadWrite")
    s.TEAMS_ADMIN = (G + "ChannelMessage.Read.All",)
    s.REFRESH = (G + ".default", "offline_access")
    canon = {n.removeprefix(G).lower(): n.removeprefix(G)
             for t in (s.IDENTITY, s.FILES, s.MAIL, s.CALENDAR, s.TEAMS, s.TEAMS_ADMIN)
             for n in t}

    def _court(n):
        n = n[len(G):] if n.lower().startswith(G) else n
        return canon.get(n.lower(), n)

    s.short = lambda t: frozenset(_court(n) for n in t)
    s.normalize = lambda chaine: frozenset(_court(n) for n in (chaine or "").split())
    return s


def _faux_coeur():
    mod = types.ModuleType("oto.tools.microsoft")
    auth = types.ModuleType("oto.tools.microsoft.auth")

    class MicrosoftAuthError(ValueError):
        status_code = 401

        def __init__(self, message="", code=None):
            super().__init__(message)
            self.code = code

    class MicrosoftGrantExpired(MicrosoftAuthError):
        pass

    auth.authorize_url = MagicMock(return_value="https://login.example/authorize?x=1")
    auth.admin_consent_url = MagicMock(return_value="https://login.example/adminconsent?x=1")
    auth.exchange_code = MagicMock(return_value=_grant())
    auth.refresh = MagicMock(return_value=_grant("AT2", "RT2"))
    mod.auth = auth
    mod.scopes = _faux_scopes()
    mod.MicrosoftAuthError = MicrosoftAuthError
    mod.MicrosoftGrantExpired = MicrosoftGrantExpired
    client = MagicMock(name="FilesClient")
    client.return_value.get_me.return_value = dict(JANE)
    mod.FilesClient = client
    return mod


class _Coffre:
    """Le coffre, en mémoire : une ligne par (entité, compte), le secret à part du
    meta — mêmes signatures que `credentials_store` pour ce que le connecteur lit.
    Tout passe sous le PORTEUR : une écriture sous le nom d'un service est un défaut."""

    def __init__(self):
        self.lignes: dict[tuple, dict] = {}
        self.prets: dict[tuple, list] = {}     # (entité, compte) → share_side (ADR 0044)

    def poser_chez(self, membre, account, secret, meta=None, prete_a=()):
        """Une ligne d'un AUTRE membre (`org:sub`), prêtée à `prete_a` (subs)."""
        self.lignes[("member", membre, account)] = {
            "secret": secret, "meta": {"scopes": SCOPE_FICHIERS, **(meta or {})},
            "set_by": membre.partition(":")[2], "set_at": "2026-10-05T00:00:00Z"}
        self.prets[(membre, account)] = [f"user:{s}" for s in prete_a]

    def list_shared_with(self, scopes):
        return [{"entity_type": et, "entity_id": eid, "connector": PORTEUR, "account": a,
                 "meta": dict(l["meta"]), "secret_kind": "oauth", "set_by": l["set_by"],
                 "set_at": l["set_at"]}
                for (et, eid, a), l in sorted(self.lignes.items())
                if set(self.prets.get((eid, a), ())) & set(scopes)]

    def get_instance_sharing(self, entity_type, entity_id, connector, account=""):
        assert connector == PORTEUR
        return [], list(self.prets.get((entity_id, account), []))

    def set_instance_sharing(self, entity_type, entity_id, connector, account="", *,
                             share_down=None, share_side=None):
        assert connector == PORTEUR
        if (entity_type, entity_id, account) not in self.lignes:
            return False
        self.prets[(entity_id, account)] = list(share_side or [])
        return True

    def poser(self, account, secret, meta=None):
        self.lignes[("member", MEMBRE, account)] = {
            "secret": secret, "meta": {"scopes": SCOPE_FICHIERS, **(meta or {})},
            "set_by": SUB, "set_at": "2026-10-05T00:00:00Z"}

    def meta(self, account):
        return self.lignes[("member", MEMBRE, account)]["meta"]

    def get_with_meta(self, entity_type, entity_id, connector, account=""):
        assert connector == PORTEUR
        ligne = self.lignes.get((entity_type, entity_id, account))
        return {**ligne, "meta": dict(ligne["meta"])} if ligne else None

    def get(self, entity_type, entity_id, connector, account=""):
        ligne = self.get_with_meta(entity_type, entity_id, connector, account)
        return ligne["secret"] if ligne else None

    def set(self, entity_type, entity_id, connector, secret, set_by=None,
            meta=None, conn=None, account="", expected_version=None):
        assert connector == PORTEUR
        self.lignes[(entity_type, entity_id, account)] = {
            "secret": secret, "meta": dict(meta or {}), "set_by": set_by,
            "set_at": "2026-10-05T01:00:00Z"}

    def list_accounts(self, entity_type, entity_id, connector):
        assert connector == PORTEUR
        return [{"account": a, "meta": dict(l["meta"]), "set_at": l["set_at"]}
                for (et, eid, a), l in sorted(self.lignes.items())
                if (et, eid) == (entity_type, entity_id)]

    def update_meta(self, entity_type, entity_id, connector, account, patch, conn=None):
        assert connector == PORTEUR
        ligne = self.lignes.get((entity_type, entity_id, account))
        if ligne is None:
            return False
        ligne["meta"].update(patch)
        return True


@pytest.fixture
def env(monkeypatch):
    from oto_mcp import access, credentials_store, db, session_org
    from oto_mcp.auth import microsoft as ms_auth
    from oto_mcp.connectors import cardinality
    from oto_mcp.db import connector_settings as store

    mod = _faux_coeur()
    monkeypatch.setitem(sys.modules, "oto.tools.microsoft", mod)
    monkeypatch.setitem(sys.modules, "oto.tools.microsoft.auth", mod.auth)
    monkeypatch.setitem(sys.modules, "oto.tools.microsoft.scopes", mod.scopes)
    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: [
                            {"scope_type": "platform", "scope_id": "platform",
                             "connector": PORTEUR, "key": k, "value": v}
                            for k, v in _COORDONNEES.items()])
    coffre = _Coffre()
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", coffre.get_with_meta)
    monkeypatch.setattr(credentials_store, "get_credential", coffre.get)
    monkeypatch.setattr(credentials_store, "set_credential", coffre.set)
    monkeypatch.setattr(credentials_store, "list_accounts", coffre.list_accounts)
    monkeypatch.setattr(credentials_store, "update_meta", coffre.update_meta)
    monkeypatch.setattr(credentials_store, "list_shared_with", coffre.list_shared_with)
    monkeypatch.setattr(credentials_store, "get_instance_sharing", coffre.get_instance_sharing)
    monkeypatch.setattr(credentials_store, "set_instance_sharing", coffre.set_instance_sharing)
    monkeypatch.setattr(db, "member_instance_suspended", lambda *a, **k: False)
    monkeypatch.setattr(db, "insert_tool_call", lambda *a, **k: None)
    # La mesure à côté de la résolution (L7) lit la base : hors sujet ici.
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "project_pinned_identity", lambda p, project_id=None: None)
    # La cardinalité vient du REGISTRE (aucune surcharge en base dans ce banc).
    monkeypatch.setattr(cardinality, "_OVERRIDES", {})
    monkeypatch.setattr(cardinality, "_LOADED", True)
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.exemple.test")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-signature-de-test")
    monkeypatch.setattr(ms_auth, "_JETONS", {})

    def sous_compte(account):
        """Exécute un appel comme le ferait l'axe `_account=` posé par le middleware."""
        def appel(fn, *a):
            jeton = session_org.set_call_account(account)
            try:
                return fn(*a)
            finally:
                session_org.reset_call_account(jeton)
        return appel

    return types.SimpleNamespace(coeur=mod, coffre=coffre, auth=ms_auth,
                                 sous_compte=sous_compte)


# ── Registre ────────────────────────────────────────────────────────────────

def test_registre_porteur_et_service():
    porteur, service = providers.REGISTRY[PORTEUR], providers.REGISTRY[SERVICE]
    for c in (porteur, service):
        assert c.secret_kind == "oauth" and c.auth_modes == frozenset({"byo_user"})
        # Multi-compte DÉCLARÉ (OAuth ⟹ la dérivation dirait mono) : c'est ce qui branche
        # la mécanique commune — axe `_account=`, `oto_identity`, refus d'ambiguïté.
        assert c.cardinality == "multi" and c.auth_multi_account
        assert c.publisher_name == "Microsoft"
        assert not c.credential_fields
        assert c.doc_sections, "la fiche doit être servie depuis son markdown"
    assert porteur.credential_of is None and porteur.label == "Microsoft 365 account"
    # Le service n'a AUCUN credential à lui : coffre et comptes sont ceux du porteur.
    assert service.credential_of == PORTEUR
    assert providers.credential_provider(SERVICE) == PORTEUR


# ── Scopes par carte ────────────────────────────────────────────────────────

def test_scopes_par_carte(env):
    s = env.coeur.scopes
    assert env.auth.scopes_for(PORTEUR) == s.IDENTITY, "le compte : l'identité seule"
    assert env.auth.scopes_for("sharepoint") == s.IDENTITY + s.FILES
    assert env.auth.scopes_for("outlook") == s.IDENTITY + s.MAIL
    assert env.auth.scopes_for("outlook_calendar") == s.IDENTITY + s.CALENDAR
    assert env.auth.scopes_for("teams") == s.IDENTITY + s.TEAMS
    with pytest.raises(RuntimeError, match="not a known Microsoft service"):
        env.auth.scopes_for("onenote")


def test_les_scopes_d_avant_le_porteur_donnent_sharepoint_sans_reconnexion(env):
    assert env.auth.services_granted(SCOPE_FICHIERS) == ["sharepoint"]
    assert env.auth.services_granted("") == []
    assert env.auth.services_granted(SCOPE_COURRIER) == ["outlook"]


def test_teams_admin_se_lit_a_l_usage(env):
    s = env.coeur.scopes
    assert not env.auth.has_scopes({"scopes": "Team.ReadBasic.All"}, s.TEAMS_ADMIN)
    assert env.auth.has_scopes({"scopes": "ChannelMessage.Read.All User.Read"},
                               s.TEAMS_ADMIN)


# ── Flux hébergé ────────────────────────────────────────────────────────────

def test_url_de_retour_derivee_de_l_environnement(env):
    from oto_mcp.connectors import flow as connector_flow

    for carte in (PORTEUR, SERVICE):
        assert connector_flow.supports(carte)
        assert connector_flow.callback_url(carte) == _RETOUR
    # Chaque service du porteur est déclaré : chacun a SON flux, même retour.
    assert env.auth.declared_services() == list(env.auth.SERVICES)
    for carte in env.auth.SERVICES:
        assert connector_flow.callback_url(carte) == _RETOUR


def test_state_ne_vaut_que_pour_ce_flux(env):
    from oto_mcp.auth import flow as oauth_flow

    etat = env.auth.make_state(SUB, ORG, "", SERVICE, "contoso.onmicrosoft.com")
    assert env.auth.verify_state(etat) == (SUB, ORG, "", SERVICE, "contoso.onmicrosoft.com")
    assert env.auth.verify_state(oauth_flow.sign_state(
        "meta_ads", {"sub": SUB, "org": ORG, "c": SERVICE})) is None
    # Une carte inconnue n'est pas une carte de retour.
    assert env.auth.verify_state(oauth_flow.sign_state(
        "microsoft", {"sub": SUB, "org": ORG, "c": "gmail"})) is None
    # Un state de personne ne vaut pas approbation, ni l'inverse.
    assert env.auth.verify_admin_state(etat) is None


def test_sans_reglages_le_refus_nomme_la_cle(env, monkeypatch):
    from oto_mcp.db import connector_settings as store

    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: [])
    assert env.auth.coordonnees_manquantes() == ["client_id", "client_secret"]
    assert env.auth.app_disponible(SUB) is False
    with pytest.raises(RuntimeError) as e:
        env.auth.app()
    assert "client_secret" in str(e.value) and 'connector="microsoft"' in str(e.value)


def test_le_dialogue_d_un_service_demande_ses_scopes(env):
    env.auth.build_auth_url(SUB, "", SERVICE)
    appel = env.coeur.auth.authorize_url.call_args
    client_id, retour, etat = appel.args
    assert client_id == "app-id-fictif" and retour == _RETOUR
    assert appel.kwargs == {"scopes": env.coeur.scopes.IDENTITY + env.coeur.scopes.FILES,
                            "tenant": "organizations"}
    assert env.auth.verify_state(etat) == (SUB, ORG, "", SERVICE, None)


def test_le_flux_porte_l_annuaire_du_client(env):
    from oto_mcp.connectors import flow as connector_flow

    ctx = types.SimpleNamespace(sub=SUB)
    start = connector_flow.entries()[SERVICE].start
    start(ctx, {"tenant": "https://Contoso.sharepoint.com/sites/Ventes/Documents"})
    appel = env.coeur.auth.authorize_url.call_args
    assert appel.kwargs["tenant"] == "contoso.onmicrosoft.com"
    assert env.auth.verify_state(appel.args[2]).tenant == "contoso.onmicrosoft.com"
    # Le champ est déclaré sur la fiche, facultatif.
    assert [p.name for p in connector_flow.entries()[SERVICE].params] == ["tenant"]
    assert connector_flow.entries()[SERVICE].params[0].required is False


def test_un_annuaire_illisible_est_refuse_avant_le_dialogue(env):
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.connectors import flow as connector_flow

    start = connector_flow.entries()[SERVICE].start
    with pytest.raises(AuthzDenied) as e:
        start(types.SimpleNamespace(sub=SUB), {"tenant": "consumers"})
    assert e.value.status == 400 and e.value.code == "invalid_tenant"
    env.coeur.auth.authorize_url.assert_not_called()


@pytest.mark.parametrize("saisi,annuaire", [
    ("", None), (None, None), ("organizations", None),
    ("Contoso.onmicrosoft.com", "contoso.onmicrosoft.com"),
    ("contoso.com", "contoso.com"),
    ("72F988BF-86F1-41AF-91AB-2D7CD011DB47", "72f988bf-86f1-41af-91ab-2d7cd011db47"),
    ("https://contoso.sharepoint.com/sites/Marketing", "contoso.onmicrosoft.com"),
    ("https://contoso-my.sharepoint.com/personal/jane", "contoso.onmicrosoft.com"),
    ("my-company.sharepoint.com", "my-company.onmicrosoft.com"),
])
def test_annuaire_saisi(saisi, annuaire):
    from oto_mcp.auth import microsoft as ms_auth

    assert ms_auth.normalize_tenant(saisi) == annuaire


@pytest.mark.parametrize("saisi", ["common", "consumers", "https://exemple.test/x",
                                   "pas un domaine", "contoso"])
def test_annuaire_refuse(saisi):
    from oto_mcp.auth import microsoft as ms_auth

    with pytest.raises(ValueError, match="Client directory"):
        ms_auth.normalize_tenant(saisi)


def test_seul_client_secret_est_secret_et_suit_la_convention():
    from oto_mcp.auth import microsoft as ms_auth
    from oto_mcp.capabilities import platform_connectors as pc

    assert [k for k in ms_auth._REGLAGES if "secret" in k] == ["client_secret"]
    lignes = pc._sans_les_secrets([
        {"connector": PORTEUR, "key": k, "value": v} for k, v in _COORDONNEES.items()])
    assert "secret-fictif" not in str(lignes)


# ── Retour de connexion ─────────────────────────────────────────────────────

def _callback(query: dict):
    from urllib.parse import urlencode

    from starlette.requests import Request

    from oto_mcp.api import microsoft as api_ms

    route = api_ms.make_routes(None, None, None, None, None)[0]
    req = Request({"type": "http", "method": "GET", "path": route.path,
                   "query_string": urlencode(query).encode(), "headers": []})
    return asyncio.run(route.endpoint(req))


def test_retour_echange_le_code_et_range_le_refresh_token(env):
    etat = env.auth.make_state(SUB, ORG, "", SERVICE)
    resp = _callback({"code": "le-code", "state": etat})
    lieu = resp.headers["location"]
    assert resp.status_code == 302 and "connect=connected" in lieu
    assert "connector=sharepoint" in lieu, "le retour revient sur la carte qui a demandé"
    appel = env.coeur.auth.exchange_code.call_args
    assert appel.args == ("app-id-fictif", "secret-fictif", "le-code", _RETOUR)
    assert appel.kwargs == {"scopes": env.coeur.scopes.IDENTITY + env.coeur.scopes.FILES,
                            "tenant": "organizations"}
    ligne = env.coffre.lignes[("member", MEMBRE, "jane@contoso.example")]
    assert ligne["secret"] == "RT1"
    assert ligne["meta"]["email"] == "Jane@Contoso.example"
    assert ligne["meta"]["microsoft_id"] == "id-jane"
    assert ligne["meta"]["is_default"] is True, "le premier compte lié est le défaut"
    assert "tenant" not in ligne["meta"]
    assert env.auth.services_granted(ligne["meta"]["scopes"]) == ["sharepoint"]
    assert "AT1" not in str(ligne), "le jeton d'accès ne va jamais en base"


def test_retour_sur_l_annuaire_d_un_client(env):
    etat = env.auth.make_state(SUB, ORG, "", SERVICE, "contoso.onmicrosoft.com")
    _callback({"code": "le-code", "state": etat})
    assert env.coeur.auth.exchange_code.call_args.kwargs["tenant"] == "contoso.onmicrosoft.com"
    compte = "jane@contoso.example (contoso.onmicrosoft.com)"
    assert env.coffre.meta(compte)["tenant"] == "contoso.onmicrosoft.com"


def test_retour_refuse_sans_state_ou_sur_refus(env):
    assert "connect=error" in _callback({"code": "c", "state": "faux"}).headers["location"]
    etat = env.auth.make_state(SUB, ORG, "", SERVICE)
    resp = _callback({"error": "access_denied", "state": etat})
    assert "connect=forbidden" in resp.headers["location"]
    env.coeur.auth.exchange_code.assert_not_called()
    assert not env.coffre.lignes


def test_retour_quand_l_organisation_exige_un_administrateur(env):
    etat = env.auth.make_state(SUB, ORG, "", SERVICE)
    resp = _callback({"error": "access_denied", "state": etat,
                      "error_description": "AADSTS90094: The grant requires admin "
                                           "permission. Trace ID: x"})
    assert resp.headers["location"].endswith("?connector=sharepoint&connect=admin_required")
    assert not env.coffre.lignes
    resp = _callback({"error": "access_denied", "state": etat,
                      "error_description": "AADSTS65001: The user or administrator has "
                                           "not consented to use the application."})
    assert resp.headers["location"].endswith("connect=admin_required")


def test_un_refus_d_entra_a_l_echange_qui_exige_un_admin(env):
    env.coeur.auth.exchange_code.side_effect = env.coeur.MicrosoftAuthError(
        "consent", code="AADSTS65001")
    etat = env.auth.make_state(SUB, ORG, "", SERVICE)
    resp = _callback({"code": "le-code", "state": etat})
    assert resp.headers["location"].endswith("connect=admin_required")
    assert not env.coffre.lignes


def test_retour_quand_entra_refuse_l_annuaire(env):
    etat = env.auth.make_state(SUB, ORG, "", SERVICE, "contoso.onmicrosoft.com")
    resp = _callback({"error": "invalid_request", "state": etat,
                      "error_description": "AADSTS90002: Tenant not found."})
    assert resp.headers["location"].endswith("?connector=sharepoint&connect=error")


# ── Plusieurs comptes, plusieurs services : se connecter AJOUTE ─────────────

def _connecter(env, me, refresh, scope=SCOPE_FICHIERS, tenant=None):
    env.coeur.FilesClient.return_value.get_me.return_value = dict(me)
    return env.auth.persist_grant(SUB, ORG, _grant("AT-" + refresh, refresh, scope=scope),
                                  tenant=tenant)


def test_un_second_compte_s_ajoute_sans_toucher_au_premier(env):
    _connecter(env, JANE, "RT-JANE")
    out = _connecter(env, JOHN, "RT-JOHN")
    assert out["account"] == "john@fabrikam.example", "sans `mail`, l'UPN nomme le compte"
    assert set(a for (_, _, a) in env.coffre.lignes) == {
        "jane@contoso.example", "john@fabrikam.example"}
    assert env.coffre.lignes[("member", MEMBRE, "jane@contoso.example")]["secret"] == "RT-JANE"
    assert env.coffre.meta("jane@contoso.example")["is_default"] is True
    assert env.coffre.meta("john@fabrikam.example")["is_default"] is False


def test_un_second_service_s_unit_au_premier_sur_le_meme_compte(env):
    _connecter(env, JANE, "RT-1")
    _connecter(env, JANE, "RT-2", scope=SCOPE_COURRIER)
    assert len(env.coffre.lignes) == 1, "un compte, une ligne"
    meta = env.coffre.meta("jane@contoso.example")
    assert env.auth.services_granted(meta["scopes"]) == ["sharepoint", "outlook"]
    assert env.coffre.lignes[("member", MEMBRE, "jane@contoso.example")]["secret"] == "RT-2"


def test_reconnecter_le_meme_compte_remplace_sa_ligne_meme_renommee(env):
    _connecter(env, JANE, "RT-JANE")
    _connecter(env, JOHN, "RT-JOHN")
    # Renommé depuis (`oto_identity op='rename'`) : le compte se reconnaît à son id.
    env.coffre.lignes[("member", MEMBRE, "client-a")] = env.coffre.lignes.pop(
        ("member", MEMBRE, "john@fabrikam.example"))
    env.coffre.meta("client-a").update(health_ko=True, health_reason="expiré")
    _connecter(env, JOHN, "RT-JOHN-2")
    assert set(a for (_, _, a) in env.coffre.lignes) == {"jane@contoso.example", "client-a"}
    assert env.coffre.lignes[("member", MEMBRE, "client-a")]["secret"] == "RT-JOHN-2"
    assert "health_ko" not in env.coffre.meta("client-a"), "la reconnexion démarque"
    assert env.coffre.meta("client-a")["is_default"] is False, "le défaut ne bouge pas"


def test_un_nom_pris_par_un_autre_compte_n_est_pas_ecrase(env):
    env.coffre.poser("jane@contoso.example", "RT-AUTRE", {"microsoft_id": "id-autre"})
    with pytest.raises(RuntimeError, match="rename"):
        _connecter(env, JANE, "RT-JANE")
    assert env.coffre.lignes[("member", MEMBRE, "jane@contoso.example")]["secret"] == "RT-AUTRE"


def test_me_sans_identite_rien_n_est_range(env):
    with pytest.raises(RuntimeError, match="nothing was saved"):
        _connecter(env, {"displayName": "x"}, "RT")
    assert not env.coffre.lignes


# ── Choix du compte à l'appel ───────────────────────────────────────────────

def _rafraichir(cid, cs, rt, **kw):
    return _grant("AT:" + rt, rt)


def _deux_comptes(env, defaut="jane@contoso.example"):
    for compte, rt in (("jane@contoso.example", "RT-JANE"),
                       ("john@fabrikam.example", "RT-JOHN")):
        env.coffre.poser(compte, rt, {"is_default": compte == defaut})
    env.coeur.auth.refresh.side_effect = _rafraichir


def test_account_choisit_le_compte(env):
    _deux_comptes(env)
    jeton = env.sous_compte("john@fabrikam.example")(env.auth.access_token_for, SUB, SERVICE)
    assert jeton == "AT:RT-JOHN"


def test_sans_account_le_compte_par_defaut(env):
    _deux_comptes(env)
    assert env.auth.access_token_for(SUB, SERVICE) == "AT:RT-JANE"


def test_un_seul_compte_sert_sans_defaut(env):
    env.coffre.poser("john@fabrikam.example", "RT-JOHN")
    env.coeur.auth.refresh.side_effect = _rafraichir
    assert env.auth.access_token_for(SUB, SERVICE) == "AT:RT-JOHN"


def test_plusieurs_comptes_sans_defaut_refus_qui_les_nomme(env):
    _deux_comptes(env, defaut=None)
    with pytest.raises(McpError) as e:
        env.auth.access_token_for(SUB, SERVICE)
    message = str(e.value)
    assert "jane@contoso.example" in message and "john@fabrikam.example" in message
    assert "_account" in message
    env.coeur.auth.refresh.assert_not_called()


def test_compte_inconnu_refuse_jamais_un_autre(env):
    _deux_comptes(env)
    with pytest.raises(McpError, match="not found"):
        env.sous_compte("inconnu@exemple.test")(env.auth.access_token_for, SUB, SERVICE)
    env.coeur.auth.refresh.assert_not_called()


def test_sans_compte_le_refus_dit_le_geste(env, monkeypatch):
    from oto_mcp import access

    # Les indices du refus générique lisent la base (révocations, instances à
    # portée) : hors sujet ici, c'est le refus lui-même qu'on vérifie.
    for indice in ("_revoked_hint", "_reachable_hint"):
        monkeypatch.setattr(access, indice, lambda *a, **k: "")
    with pytest.raises(McpError, match="sharepoint"):
        env.auth.access_token_for(SUB, SERVICE)
    env.coeur.auth.refresh.assert_not_called()


def test_un_compte_qui_n_a_pas_autorise_le_service_est_refuse_en_nommant_la_carte(env):
    env.coffre.poser("jane@contoso.example", "RT-JANE", {"scopes": SCOPE_COURRIER})
    with pytest.raises(RuntimeError) as e:
        env.auth.access_token_for(SUB, SERVICE)
    assert "has not yet authorized SharePoint & OneDrive" in str(e.value)
    assert "jane@contoso.example" in str(e.value)
    with pytest.raises(RuntimeError, match="has not yet authorized Outlook Calendar"):
        env.auth.access_token_for(SUB, "outlook_calendar")
    env.coeur.auth.refresh.assert_not_called()
    # Le même compte, pour le service qu'il a autorisé : servi.
    assert env.auth.access_token_for(SUB, "outlook") == "AT2"


# ── Renouvellement ──────────────────────────────────────────────────────────

def test_le_renouvellement_demande_default_sur_l_annuaire_du_compte(env):
    env.coffre.poser("jane@contoso.example", "RT1", {"tenant": "contoso.onmicrosoft.com"})
    env.coffre.poser("john@fabrikam.example", "RT2", {"is_default": True})
    env.auth.access_token_for(SUB, SERVICE)
    env.sous_compte("jane@contoso.example")(env.auth.access_token_for, SUB, SERVICE)
    vus = [c.kwargs for c in env.coeur.auth.refresh.call_args_list]
    assert vus == [{"scopes": env.coeur.scopes.REFRESH, "tenant": "organizations"},
                   {"scopes": env.coeur.scopes.REFRESH, "tenant": "contoso.onmicrosoft.com"}]


def test_le_renouvellement_relit_les_scopes_consentis(env):
    env.coffre.poser("jane@contoso.example", "RT1")
    env.coeur.auth.refresh.return_value = _grant(
        "AT2", "RT1", scope=SCOPE_FICHIERS + " ChannelMessage.Read.All")
    env.auth.access_token_for(SUB, SERVICE)
    meta = env.coffre.meta("jane@contoso.example")
    assert env.auth.has_scopes(meta, env.coeur.scopes.TEAMS_ADMIN), \
        "une approbation donnée depuis la connexion est vue sans reconnexion"


def test_rotation_rangee_sur_le_bon_compte_puis_cache(env):
    _deux_comptes(env)
    env.coffre.meta("john@fabrikam.example")["email"] = "john@fabrikam.example"
    env.coeur.auth.refresh.side_effect = None
    env.coeur.auth.refresh.return_value = _grant("AT2", "RT-JOHN-TOURNE")
    appel = env.sous_compte("john@fabrikam.example")
    assert appel(env.auth.access_token_for, SUB, SERVICE) == "AT2"
    assert env.coffre.lignes[("member", MEMBRE, "john@fabrikam.example")]["secret"] \
        == "RT-JOHN-TOURNE"
    assert env.coffre.meta("john@fabrikam.example")["email"] == "john@fabrikam.example", \
        "l'identité du compte reste"
    assert env.coffre.lignes[("member", MEMBRE, "jane@contoso.example")]["secret"] \
        == "RT-JANE", "l'autre compte ne bouge pas"
    assert appel(env.auth.access_token_for, SUB, SERVICE) == "AT2"
    assert env.coeur.auth.refresh.call_count == 1, "le second appel sert du cache"


def test_renew_renouvelle_malgre_le_cache_et_relit_les_scopes(env):
    """Une approbation d'administrateur donnée après l'émission du jeton en cache n'est
    portée que par un jeton NEUF — et `meta.scopes` ne l'apprend qu'au renouvellement."""
    env.coffre.poser("jane@contoso.example", "RT1",
                     {"scopes": "Team.ReadBasic.All Chat.ReadWrite User.Read"})
    env.coeur.auth.refresh.return_value = _grant("AT2", "RT1",
                                                 scope="Team.ReadBasic.All Chat.ReadWrite")
    assert env.auth.access_token_for(SUB, "teams") == "AT2"
    env.coeur.auth.refresh.return_value = _grant(
        "AT3", "RT1", scope="Team.ReadBasic.All Chat.ReadWrite ChannelMessage.Read.All")
    assert env.auth.access_token_for(SUB, "teams") == "AT2", "le cache sert"
    assert env.auth.access_token_for(SUB, "teams", renew=True) == "AT3"
    assert env.coeur.auth.refresh.call_count == 2
    assert env.auth.has_scopes(env.coffre.meta("jane@contoso.example"),
                               env.coeur.scopes.TEAMS_ADMIN)
    assert env.auth.access_token_for(SUB, "teams") == "AT3", "le jeton neuf est en cache"


def test_le_cache_d_un_compte_ne_sert_pas_l_autre(env):
    _deux_comptes(env)
    assert env.auth.access_token_for(SUB, SERVICE) == "AT:RT-JANE"
    jeton = env.sous_compte("john@fabrikam.example")(env.auth.access_token_for, SUB, SERVICE)
    assert jeton == "AT:RT-JOHN"


def test_un_renouvellement_reussi_demarque_le_compte(env):
    env.coffre.poser("jane@contoso.example", "RT1",
                     {"is_default": True, "health_ko": True, "health_reason": "expiré"})
    assert env.auth.access_token_for(SUB, SERVICE) == "AT2"
    assert env.coffre.meta("jane@contoso.example")["health_ko"] is False


def test_autorisation_morte_marque_ce_compte_seulement(env):
    _deux_comptes(env)
    expire = env.coeur.MicrosoftGrantExpired("expiré")

    def refresh(cid, cs, rt, **kw):
        if rt == "RT-JOHN":
            raise expire
        return _grant("AT:" + rt, rt)

    env.coeur.auth.refresh.side_effect = refresh
    with pytest.raises(env.auth.MicrosoftReauthRequired, match="john@fabrikam.example"):
        env.sous_compte("john@fabrikam.example")(env.auth.access_token_for, SUB, SERVICE)
    assert env.coffre.meta("john@fabrikam.example")["health_ko"] is True
    assert not env.coffre.meta("jane@contoso.example").get("health_ko")
    assert env.coffre.lignes[("member", MEMBRE, "john@fabrikam.example")]["secret"] \
        == "RT-JOHN", "marquer n'efface rien"
    # L'autre compte continue de servir.
    assert env.auth.access_token_for(SUB, SERVICE) == "AT:RT-JANE"
    indice = env.auth._etape_manquante_for(SERVICE)(SUB, None, None, {})
    assert "john@fabrikam.example" in indice
    for carte in (None, SERVICE):
        etat = env.auth._link_state_for(carte)(SUB)
        assert etat.linked and etat.accounts == 2 and etat.health_ko
        assert "john@fabrikam.example" in etat.health_reason
        assert "jane@contoso.example" not in etat.health_reason


def test_secret_d_application_faux_ne_marque_pas_la_personne(env):
    env.coffre.poser("jane@contoso.example", "RT1")
    env.coeur.auth.refresh.side_effect = env.coeur.MicrosoftAuthError("invalid_client")
    with pytest.raises(env.coeur.MicrosoftAuthError):
        env.auth.access_token_for(SUB, SERVICE)
    assert not env.coffre.meta("jane@contoso.example").get("health_ko")


def test_statut_des_fiches(env):
    hint_service = env.auth._etape_manquante_for(SERVICE)
    hint_porteur = env.auth._etape_manquante_for(None)
    assert hint_service(SUB, None, None, {}) == "Sign in with Microsoft"
    assert env.auth._link_state_for(SERVICE)(SUB).linked is False
    # Un compte lié pour un AUTRE service : le compte est lié, pas SharePoint.
    env.coffre.poser("jane@contoso.example", "RT1", {"scopes": SCOPE_COURRIER})
    assert hint_porteur(SUB, None, None, {}) is None
    assert env.auth._link_state_for(None)(SUB).linked is True
    assert hint_service(SUB, None, None, {}) == "Authorize SharePoint & OneDrive"
    assert env.auth._link_state_for(SERVICE)(SUB).linked is False
    env.coffre.meta("jane@contoso.example")["scopes"] = SCOPE_FICHIERS
    assert hint_service(SUB, None, None, {}) is None
    env.coffre.meta("jane@contoso.example")["health_ko"] = True
    assert "reconnect" in hint_service(SUB, None, None, {})


def test_la_fiche_teams_dit_le_palier_admin_qui_manque(env):
    hint = env.auth._etape_manquante_for("teams")
    sans = "Team.ReadBasic.All Chat.ReadWrite User.Read offline_access"
    env.coffre.poser("jane@contoso.example", "RT1", {"scopes": sans})
    assert hint(SUB, None, None, {}) == (
        "Reading channel messages requires your Microsoft admin's approval")
    env.coffre.poser("john@fabrikam.example", "RT2",
                     {"scopes": sans + " ChannelMessage.Read.All"})
    assert hint(SUB, None, None, {}) == (
        "Reading channel messages requires your Microsoft admin's approval for "
        "jane@contoso.example")
    env.coffre.meta("jane@contoso.example")["scopes"] += " ChannelMessage.Read.All"
    assert hint(SUB, None, None, {}) is None
    # Un service sans palier admin n'en dit rien.
    assert env.auth.admin_scopes("outlook") == ()


# ── Approbation d'un administrateur ─────────────────────────────────────────

def test_lien_d_approbation_demande_l_union_des_services(env):
    s = env.coeur.scopes
    env.auth.admin_consent_url(SUB, ("sharepoint", "teams"), "contoso.onmicrosoft.com")
    appel = env.coeur.auth.admin_consent_url.call_args
    client_id, retour, etat = appel.args
    assert client_id == "app-id-fictif" and retour == _RETOUR
    assert appel.kwargs["tenant"] == "contoso.onmicrosoft.com"
    assert set(appel.kwargs["scopes"]) == set(s.IDENTITY + s.FILES + s.TEAMS + s.TEAMS_ADMIN)
    approbation = env.auth.verify_admin_state(etat)
    assert approbation.services == ("sharepoint", "teams") and approbation.connector == SERVICE
    assert env.auth.verify_state(etat) is None, "une approbation ne vaut pas connexion"
    env.auth.admin_consent_url(SUB, ("outlook",))
    appel = env.coeur.auth.admin_consent_url.call_args
    assert appel.kwargs["tenant"] == "organizations"
    assert not set(s.TEAMS_ADMIN) & set(appel.kwargs["scopes"]), "TEAMS_ADMIN pour Teams seul"
    with pytest.raises(ValueError, match="Unknown Microsoft service"):
        env.auth.admin_consent_url(SUB, ("onenote",))


def test_le_lien_d_approbation_vit_sept_jours(env, monkeypatch):
    env.auth.admin_consent_url(SUB, ("sharepoint",))
    etat = env.coeur.auth.admin_consent_url.call_args.args[2]
    maintenant = time.time()
    monkeypatch.setattr(time, "time", lambda: maintenant + 6 * 24 * 3600)
    assert env.auth.verify_admin_state(etat) is not None
    monkeypatch.setattr(time, "time", lambda: maintenant + 8 * 24 * 3600)
    assert env.auth.verify_admin_state(etat) is None


def _etat_approbation(env):
    env.auth.admin_consent_url(SUB, ("sharepoint",))
    return env.coeur.auth.admin_consent_url.call_args.args[2]


def test_retour_d_approbation_donnee_n_ecrit_rien(env):
    resp = _callback({"admin_consent": "True", "tenant": "un-guid",
                      "state": _etat_approbation(env)})
    assert resp.headers["location"].endswith(
        "/connectors?connector=sharepoint&connect=admin_approved")
    env.coeur.auth.exchange_code.assert_not_called()
    assert not env.coffre.lignes


def test_retour_d_approbation_refusee_n_ecrit_rien(env):
    resp = _callback({"error": "access_denied", "error_description": "AADSTS65004: declined",
                      "state": _etat_approbation(env)})
    assert resp.headers["location"].endswith("connect=admin_refused")
    env.coeur.auth.exchange_code.assert_not_called()
    assert not env.coffre.lignes


def test_le_lien_d_approbation_servi_par_la_carte(env):
    from oto_mcp.capabilities.registry import CAPABILITIES

    cap = next(c for c in CAPABILITIES if c.key == "me.microsoft_admin_consent")
    assert cap.mcp == "microsoft_admin_consent"
    assert (cap.rest.verb, cap.rest.path) == ("POST",
                                              "/api/me/connectors/microsoft/admin-consent")
    ctx = types.SimpleNamespace(sub=SUB)
    out = cap.handler(ctx, cap.Input())
    assert out["url"] == "https://login.example/adminconsent?x=1"
    assert out["services"] == ["sharepoint", "outlook", "outlook_calendar", "teams"]
    assert out["tenant"] == "organizations"
    assert out["expires_at"].endswith("Z")
    etat = env.auth.verify_admin_state(env.coeur.auth.admin_consent_url.call_args.args[2])
    assert etat.connector == "sharepoint", "la carte de retour par défaut"
    out = cap.handler(ctx, cap.Input(services=["outlook"], connector="microsoft",
                                     tenant="https://contoso.sharepoint.com/sites/x"))
    assert out["services"] == ["outlook"] and out["tenant"] == "contoso.onmicrosoft.com"
    etat = env.auth.verify_admin_state(env.coeur.auth.admin_consent_url.call_args.args[2])
    assert etat.connector == "microsoft"


@pytest.mark.parametrize("entree,motif", [
    ({"services": ["onenote"]}, "Unknown Microsoft service"),
    ({"connector": "gmail"}, "not a Microsoft card"),
    ({"tenant": "consumers"}, "Client directory"),
])
def test_le_lien_d_approbation_refuse_en_nommant(env, entree, motif):
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities.registry import CAPABILITIES

    cap = next(c for c in CAPABILITIES if c.key == "me.microsoft_admin_consent")
    with pytest.raises(AuthzDenied) as e:
        cap.handler(types.SimpleNamespace(sub=SUB), cap.Input(**entree))
    assert e.value.status == 400 and e.value.code == "invalid_admin_consent"
    assert motif in e.value.message


def test_le_lien_d_approbation_sans_application(env, monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities.registry import CAPABILITIES
    from oto_mcp.db import connector_settings as store

    monkeypatch.setattr(store, "list_connector_settings", lambda key=None, conn=None: [])
    cap = next(c for c in CAPABILITIES if c.key == "me.microsoft_admin_consent")
    with pytest.raises(AuthzDenied) as e:
        cap.handler(types.SimpleNamespace(sub=SUB), cap.Input())
    assert e.value.status == 503 and e.value.code == "oauth_misconfigured"


# ── La mécanique commune, branchée ──────────────────────────────────────────

def test_oto_identity_liste_par_service_et_fixe_le_defaut(env):
    from oto_mcp.connectors import identities

    _deux_comptes(env)
    env.coffre.meta("john@fabrikam.example")["scopes"] = SCOPE_FICHIERS + " Mail.ReadWrite"
    env.coffre.poser("marc@exemple.test", "RT-MARC", {"scopes": SCOPE_COURRIER})
    for carte in (PORTEUR, SERVICE):
        assert identities.supports(carte)
    tous = identities.list_identities(SUB, PORTEUR)
    assert [i["id"] for i in tous] == [
        "jane@contoso.example", "john@fabrikam.example", "marc@exemple.test"]
    liste = identities.list_identities(SUB, SERVICE)
    assert [(i["id"], i["is_default"]) for i in liste] == [
        ("jane@contoso.example", True), ("john@fabrikam.example", False)], \
        "un compte qui n'a pas autorisé SharePoint n'est pas proposé sur sa carte"
    identities.select_identity(SUB, SERVICE, "john@fabrikam.example")
    assert env.auth.access_token_for(SUB, SERVICE) == "AT:RT-JOHN"


def test_microsoft_accounts_dit_les_services_de_chaque_compte(env, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import microsoft as outils

    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    env.coffre.poser("jane@contoso.example", "RT1",
                     {"is_default": True, "email": "Jane@Contoso.example"})
    env.coffre.poser("jane@contoso.example (contoso.onmicrosoft.com)", "RT2",
                     {"scopes": SCOPE_COURRIER, "tenant": "contoso.onmicrosoft.com"})
    m = FastMCP("t")
    outils.register(m)
    out = asyncio.run(m.get_tool("microsoft_accounts")).fn()
    assert out == {"accounts": [
        {"account": "jane@contoso.example", "email": "Jane@Contoso.example",
         "is_default": True, "services": ["sharepoint"], "directory": None},
        {"account": "jane@contoso.example (contoso.onmicrosoft.com)", "email": None,
         "is_default": False, "services": ["outlook"],
         "directory": "contoso.onmicrosoft.com"}]}
    assert "RT1" not in str(out)


def test_l_axe_account_est_accepte_sur_les_outils(env):
    from oto_mcp import call_axes

    for outil in ("sharepoint_file", "sharepoint_site", "outlook_message",
                  "outlook_compose", "outlook_calendar_calendars", "outlook_calendar_event",
                  "teams_spaces", "teams_message"):
        assert "_account" in {a.param for a in call_axes.axes_for_call(outil)}, outil


def test_la_fiche_dit_la_regle_des_comptes_par_connexion():
    for carte in (PORTEUR, SERVICE):
        sections = providers.REGISTRY[carte].doc_sections
        multi = next(s for s in sections if "multiple" in s.title)
        assert "_account" in multi.body_md and "principal" not in multi.body_md



# ── Le compte PRÊTÉ par une collègue (`oto_instance op=lend`, ADR 0044) ─────────────
#
# Mesuré en prod : le prêt était enregistré (share_side du porteur `microsoft`), mais
# rien côté emprunteuse ne le voyait — ni ses identités, ni `microsoft_accounts`, ni la
# résolution d'un outil sans `_instance=` explicite.

PRETEUSE = "preteuse"
EMPRUNTEUSE = "emprunteuse"
CHEZ_PRETEUSE = f"{ORG}:{PRETEUSE}"


@pytest.fixture
def pret(env, monkeypatch):
    from oto_mcp import access, account_suspension, db

    for indice in ("_revoked_hint", "_reachable_hint"):
        monkeypatch.setattr(access, indice, lambda *a, **k: "")
    monkeypatch.setattr(account_suspension, "refus_preteur", lambda *a, **k: None)
    monkeypatch.setattr(db, "emails_by_subs",
                        lambda subs: {PRETEUSE: "preteuse@contoso.example"})
    # Entra rend au renouvellement ce que le compte a consenti : le courrier seulement.
    env.coeur.auth.refresh.side_effect = (
        lambda cid, cs, rt, **kw: _grant("AT:" + rt, rt, scope=SCOPE_COURRIER))
    return env


def test_un_pret_deja_enregistre_sert_l_emprunteuse_sur_outlook(pret):
    """La forme exacte du prêt en base (share_side `user:<sub>` sur la ligne du porteur,
    posé avant ce correctif) : visible et utilisable sans le refaire."""
    pret.coffre.poser_chez(CHEZ_PRETEUSE, "jane@contoso.example", "RT-JANE",
                           {"scopes": SCOPE_COURRIER}, prete_a=[EMPRUNTEUSE])
    assert pret.auth.access_token_for(EMPRUNTEUSE, "outlook") == "AT:RT-JANE"
    # Le service que le compte n'a pas autorisé reste refusé en nommant la carte.
    with pytest.raises(McpError):
        pret.auth.access_token_for(EMPRUNTEUSE, "sharepoint")
    # Personne d'autre : le prêt est nominatif.
    with pytest.raises(McpError):
        pret.auth.access_token_for("quelqu-un-d-autre", "outlook")


def test_le_compte_prete_apparait_marque_et_relie_la_carte(pret, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, connectors
    from oto_mcp.connectors import identities
    from oto_mcp.tools import microsoft as outils

    pret.coffre.poser_chez(CHEZ_PRETEUSE, "jane@contoso.example", "RT-JANE",
                           {"scopes": SCOPE_COURRIER, "is_default": True},
                           prete_a=[EMPRUNTEUSE])
    (ident,) = identities.list_identities(EMPRUNTEUSE, "outlook")
    assert ident["id"] == "jane@contoso.example"
    assert ident["granted"] is True and ident["is_default"] is False
    assert ident["owner"] == {"sub": PRETEUSE, "email": "preteuse@contoso.example",
                              "org": ORG}
    assert connectors.link.state("outlook", EMPRUNTEUSE).linked is True
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: EMPRUNTEUSE)
    m = FastMCP("t")
    outils.register(m)
    (compte,) = asyncio.run(m.get_tool("microsoft_accounts")).fn()["accounts"]
    assert compte["account"] == "jane@contoso.example" and compte["lent_by"] == PRETEUSE
    assert compte["is_default"] is False and compte["services"] == ["outlook"]


def test_ses_propres_comptes_d_abord_le_pret_par_son_nom(pret):
    pret.coffre.lignes[("member", f"{ORG}:{EMPRUNTEUSE}", "moi@fabrikam.example")] = {
        "secret": "RT-MOI", "meta": {"scopes": SCOPE_COURRIER}, "set_by": EMPRUNTEUSE,
        "set_at": "2026-10-05T00:00:00Z"}
    pret.coffre.poser_chez(CHEZ_PRETEUSE, "jane@contoso.example", "RT-JANE",
                           {"scopes": SCOPE_COURRIER}, prete_a=[EMPRUNTEUSE])
    assert pret.auth.access_token_for(EMPRUNTEUSE, "outlook") == "AT:RT-MOI"
    assert pret.sous_compte("jane@contoso.example")(
        pret.auth.access_token_for, EMPRUNTEUSE, "outlook") == "AT:RT-JANE"


def test_deux_comptes_pretes_sans_nom_refus_qui_les_nomme(pret):
    for compte in ("jane@contoso.example", "john@fabrikam.example"):
        pret.coffre.poser_chez(CHEZ_PRETEUSE, compte, f"RT-{compte}",
                               {"scopes": SCOPE_COURRIER}, prete_a=[EMPRUNTEUSE])
    with pytest.raises(McpError) as e:
        pret.auth.access_token_for(EMPRUNTEUSE, "outlook")
    assert "jane@contoso.example" in str(e.value) and "john@fabrikam.example" in str(e.value)
    pret.coeur.auth.refresh.assert_not_called()


def test_un_pret_repris_ne_sert_plus(pret):
    pret.coffre.poser_chez(CHEZ_PRETEUSE, "jane@contoso.example", "RT-JANE",
                           {"scopes": SCOPE_COURRIER}, prete_a=[EMPRUNTEUSE])
    pret.coffre.prets[(CHEZ_PRETEUSE, "jane@contoso.example")] = []
    with pytest.raises(McpError):
        pret.auth.access_token_for(EMPRUNTEUSE, "outlook")
    assert pret.auth.accounts_for(EMPRUNTEUSE) == []


def test_bout_en_bout_preter_outlook_puis_l_emprunteuse_l_emploie(pret, monkeypatch):
    """`oto_instance op=lend connector=outlook to=<sub>` SANS `account` (le geste mesuré
    en prod) prête le compte du porteur ; l'outil Outlook de l'emprunteuse le trouve."""
    import types as _t

    from oto_mcp import db
    from oto_mcp.capabilities.connectors import sharing

    pret.coffre.poser_chez(CHEZ_PRETEUSE, "jane@contoso.example", "RT-JANE",
                           {"scopes": SCOPE_COURRIER})
    monkeypatch.setattr(db, "get_user", lambda sub: {"sub": sub})
    out = sharing._lend_instance(_t.SimpleNamespace(sub=PRETEUSE),
                                 sharing.LendInstanceInput(connector="outlook", to=EMPRUNTEUSE))
    assert (out["connector"], out["account"], out["lent_to"]) == (
        "microsoft", "jane@contoso.example", [EMPRUNTEUSE])
    assert pret.auth.access_token_for(EMPRUNTEUSE, "outlook") == "AT:RT-JANE"
