"""Un compte Microsoft connecté au nom de l'org ou d'une équipe (lot 5 du partage de
compte, `docs/partage-de-compte.md`, décision du 15/10/2026).

Le concept 1 du modèle : posé par l'admin d'org ou le chef de l'équipe active, le compte
appartient à l'org ou à l'équipe et sert ses membres SANS désignation, après leur compte
propre (propre → équipe active → org ; le compte d'une collègue seulement nommé, au niveau
de ses règles). Ce n'est pas le compte d'une autre personne : pas de `lent_by`. Même banc que `test_microsoft_oauth.py` (cœur moqué, coffre en mémoire,
vraie résolution).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from oto_mcp.mcp_errors import McpError
from test_microsoft_oauth import (  # noqa: F401 — fixtures et doublures du banc Microsoft
    COLLEGUE, ORG, PORTEUR, SCOPE_COURRIER, SCOPE_FICHIERS, _callback, _grant, _regle,
    _repondre, autrui, env, trace)

EQUIPE = 49
ADMIN = "admin-de-l-org"
CHEF = "chef-d-equipe"
JANE = "jane@contoso.example"
BOITE_ORG = "accueil@contoso.example"
BOITE_EQUIPE = "ventes@contoso.example"
SCOPE_TOUT = f"{SCOPE_COURRIER} {SCOPE_FICHIERS}"


@pytest.fixture
def org(autrui, monkeypatch):
    """L'org ORG (admin ADMIN) et son équipe EQUIPE (chef CHEF), active pour tous."""
    from oto_mcp import access, group_store, org_store, roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: (sub, oid) == (ADMIN, ORG))
    monkeypatch.setattr(roles, "can_admin_group",
                        lambda sub, gid: (sub, gid) in ((CHEF, EQUIPE), (ADMIN, EQUIPE)))
    monkeypatch.setattr(access, "current_group", lambda sub: EQUIPE)
    monkeypatch.setattr(group_store, "get_group", lambda gid: (
        {"id": gid, "org_id": ORG, "name": "Ventes"} if gid == EQUIPE else None))
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid, "name": "Contoso"})
    monkeypatch.setattr(org_store, "nom_d_org", lambda oid: "Contoso")
    return autrui


def _poser(env, palier, cible, compte, secret, scopes=SCOPE_COURRIER, **meta):
    env.coffre.lignes[(palier, str(cible), compte)] = {
        "secret": secret, "meta": {"scopes": scopes, **meta}, "set_by": ADMIN,
        "set_at": "2026-10-05T00:00:00Z"}


def _demarrer(env, sub, carte="outlook", **valeurs):
    ctx = types.SimpleNamespace(sub=sub)
    env.auth._start_flow_for(carte)(ctx, valeurs)
    return env.coeur.auth.authorize_url.call_args.args[2]


# ── La connexion au nom de l'org ou de l'équipe ─────────────────────────────

def test_l_admin_connecte_outlook_au_nom_de_l_org(org):
    etat = _demarrer(org, ADMIN, scope="org")
    assert (org.auth.verify_state(etat).scope, org.auth.verify_state(etat).group) == (
        "org", None)
    # Entra rend l'union des consentements du compte : le courrier ET les fichiers
    # que l'admin avait autorisés pour lui-même.
    org.coeur.auth.exchange_code.return_value = _grant("AT", "RT-ORG", scope=SCOPE_TOUT)
    resp = _callback({"code": "code-1", "state": etat})
    assert resp.headers["location"].endswith("connect=connected")
    ligne = org.coffre.lignes[("org", str(ORG), JANE)]
    assert ligne["secret"] == "RT-ORG" and ligne["set_by"] == ADMIN
    assert org.auth.services_granted(ligne["meta"]["scopes"]) == ["outlook"], \
        "la ligne de l'org ne garde que ce que SON consentement demandait"
    assert ligne["meta"]["is_default"] is False, "à l'org, l'admin choisit le défaut"
    assert not [k for k in org.coffre.lignes
                if k[0] == "member" and k[1] == f"{ORG}:{ADMIN}"], "rien chez l'admin"


def test_le_chef_connecte_au_nom_de_son_equipe_active(org):
    etat = _demarrer(org, CHEF, scope="group")
    assert org.auth.verify_state(etat).group == EQUIPE
    _callback({"code": "code-1", "state": etat})
    assert ("group", str(EQUIPE), JANE) in org.coffre.lignes


@pytest.mark.parametrize("sub,valeurs,statut,code", [
    (COLLEGUE, {"scope": "org"}, 403, "scope_forbidden"),
    (COLLEGUE, {"scope": "group"}, 403, "scope_forbidden"),
    (ADMIN, {"scope": "tenant"}, 400, "invalid_scope"),
])
def test_un_palier_que_l_appelant_n_administre_pas_est_refuse(org, sub, valeurs, statut,
                                                              code):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _demarrer(org, sub, **valeurs)
    assert (e.value.status, e.value.code) == (statut, code)
    org.coeur.auth.authorize_url.assert_not_called()


def test_l_equipe_sans_equipe_active_est_refusee(org, monkeypatch):
    from oto_mcp import access
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    with pytest.raises(AuthzDenied) as e:
        _demarrer(org, CHEF, scope="group")
    assert e.value.code == "scope_forbidden" and "active team" in e.value.message


def test_un_state_d_equipe_sans_equipe_ne_vaut_rien(org):
    from oto_mcp.auth import flow as oauth_flow
    assert org.auth.verify_state(oauth_flow.sign_state("microsoft", {
        "sub": CHEF, "org": ORG, "c": "outlook", "s": "group"})) is None


def test_un_droit_perdu_avant_le_retour_n_ecrit_rien(org, monkeypatch):
    from oto_mcp import roles
    etat = _demarrer(org, ADMIN, scope="org")
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: False)
    resp = _callback({"code": "code-1", "state": etat})
    assert resp.headers["location"].endswith("connect=error")
    assert ("org", str(ORG), JANE) not in org.coffre.lignes
    org.coeur.auth.exchange_code.assert_not_called()


# ── Le service : sans désignation, propre → équipe → org ; autrui seulement nommé ─

def test_le_compte_de_l_org_sert_le_membre_sans_designation_et_n_est_pas_un_pret(org,
                                                                                trace):
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG")
    assert org.auth.access_token_for(COLLEGUE, "outlook") == "AT:RT-ORG"
    assert _repondre("outlook") == {"ok": True, "_account": BOITE_ORG}


def test_l_ordre_propre_puis_equipe_puis_org(org):
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG")
    _poser(org, "group", EQUIPE, BOITE_EQUIPE, "RT-EQUIPE")
    _poser(org, "member", f"{ORG}:{COLLEGUE}", "moi@contoso.example", "RT-MOI")
    jeton = lambda: org.auth.access_token_for(COLLEGUE, "outlook")  # noqa: E731
    assert jeton() == "AT:RT-MOI"
    del org.coffre.lignes[("member", f"{ORG}:{COLLEGUE}", "moi@contoso.example")]
    assert jeton() == "AT:RT-EQUIPE"
    del org.coffre.lignes[("group", str(EQUIPE), BOITE_EQUIPE)]
    assert jeton() == "AT:RT-ORG"
    # Nommé, le compte de l'org sert même quand l'équipe en a un.
    _poser(org, "group", EQUIPE, BOITE_EQUIPE, "RT-EQUIPE")
    assert org.sous_compte(BOITE_ORG)(
        org.auth.access_token_for, COLLEGUE, "outlook") == "AT:RT-ORG"


def test_deux_comptes_de_l_org_sans_nom_refus_d_ambiguite_nomme(org):
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG")
    _poser(org, "org", ORG, "compta@contoso.example", "RT-COMPTA")
    with pytest.raises(McpError) as e:
        org.auth.access_token_for(COLLEGUE, "outlook")
    assert BOITE_ORG in str(e.value) and "compta@contoso.example" in str(e.value)
    org.coeur.auth.refresh.assert_not_called()
    assert org.sous_compte("compta@contoso.example")(
        org.auth.access_token_for, COLLEGUE, "outlook") == "AT:RT-COMPTA"


def test_le_compte_de_l_org_avant_celui_d_une_collegue_seulement_nomme(org, trace):
    org.regles.append(_regle("sensitive"))
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG")
    assert org.auth.access_token_for(COLLEGUE, "outlook") == "AT:RT-ORG"
    assert org.sous_compte(JANE)(
        org.auth.access_token_for, COLLEGUE, "outlook") == "AT:RT-JANE"
    assert _repondre("outlook")["lent_by"] == "titulaire@contoso.example"


def test_le_renouvellement_n_elargit_pas_la_ligne_partagee(org):
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG",
           scopes="Team.ReadBasic.All Chat.ReadWrite User.Read offline_access")
    org.coeur.auth.refresh.side_effect = None
    org.coeur.auth.refresh.return_value = _grant("AT2", "RT-ORG", scope=(
        "Team.ReadBasic.All Chat.ReadWrite ChannelMessage.Read.All Mail.ReadWrite "
        "Mail.Send User.Read offline_access"))
    assert org.auth.access_token_for(COLLEGUE, "teams") == "AT2"
    meta = org.coffre.lignes[("org", str(ORG), BOITE_ORG)]["meta"]
    assert org.auth.services_granted(meta["scopes"]) == ["teams"], \
        "le courrier personnel du titulaire n'entre pas dans la ligne de l'org"
    assert org.auth.has_scopes(meta, org.coeur.scopes.TEAMS_ADMIN), \
        "l'approbation de l'administrateur du tenant, elle, est vue"


# ── Ce que la personne voit, et qui retire ──────────────────────────────────

def test_microsoft_accounts_et_identites_disent_le_niveau(org, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.connectors import identities
    from oto_mcp.tools import microsoft as outils

    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG", is_default=True)
    _poser(org, "group", EQUIPE, BOITE_EQUIPE, "RT-EQUIPE")
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: COLLEGUE)
    m = FastMCP("t")
    outils.register(m)
    comptes = asyncio.run(m.get_tool("microsoft_accounts")).fn()["accounts"]
    assert [(c["account"], c["shared"], c["shared_owner"], c["is_default"])
            for c in comptes] == [
        (BOITE_EQUIPE, "group", {"id": EQUIPE, "name": "Ventes"}, False),
        (BOITE_ORG, "org", {"id": ORG, "name": "Contoso"}, False)]
    assert not any("lent_by" in c for c in comptes)
    for carte in ("outlook", PORTEUR):
        idents = identities.list_identities(COLLEGUE, carte)
        # #1499 : plus de marques `shared`/`granted` — le `level` que donnent les règles
        # (sans règle : membre de l'équipe `sensitive`, membre de l'org `sensitive`, écart (a)).
        assert [(i["id"], i["level"], i["owner"]["type"], i["is_default"])
                for i in idents] == [(BOITE_EQUIPE, "sensitive", "group", False),
                                     (BOITE_ORG, "sensitive", "org", False)], carte
        assert not any("shared" in i or "granted" in i for i in idents)
    # `owner` traverse le modèle de sortie REST : l'écran nomme l'équipe ou l'org qui
    # possède le compte.
    from oto_mcp.capabilities.connectors.identities import ConnectorIdentities
    servi = ConnectorIdentities(connector="outlook", supported=True,
                                identities=identities.list_identities(COLLEGUE, "outlook")
                                ).model_dump(mode="json")
    assert [(i["owner"]["id"], i["owner"]["name"]) for i in servi["identities"]] == [
        (str(EQUIPE), "Ventes"), (str(ORG), "Contoso")]
    assert identities.list_identities(COLLEGUE, "sharepoint") == []
    # Le palier org de la carte du porteur : les comptes de l'org (admin, générique).
    assert [i["id"] for i in identities.list_identities(ADMIN, PORTEUR, "org")] == [
        BOITE_ORG]
    assert org.auth._link_state_for("outlook")(COLLEGUE).linked is True


def test_seul_l_admin_retire_le_compte_de_l_org(org, monkeypatch):
    from oto_mcp import credentials_store
    from oto_mcp.capabilities import me_credentials as mc
    from oto_mcp.capabilities._types import AuthzDenied

    def effacer(et, eid, connector, conn=None, account=""):
        return org.coffre.lignes.pop((et, eid, account), None) is not None

    monkeypatch.setattr(credentials_store, "clear_credential", effacer)
    monkeypatch.setattr(mc, "_refus_org_admin",
                        lambda oid, autres=None, sub=None: AuthzDenied(403, "org_admin_required"))
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG")
    entree = mc.CredentialClearInput(provider=PORTEUR, scope="org", account=BOITE_ORG)
    with pytest.raises(AuthzDenied) as e:
        mc._clear(types.SimpleNamespace(sub=COLLEGUE), entree)
    assert e.value.status == 403
    assert ("org", str(ORG), BOITE_ORG) in org.coffre.lignes
    out = mc._clear(types.SimpleNamespace(sub=ADMIN), entree)
    assert (out["scope"], out["account"]) == ("org", BOITE_ORG)
    assert ("org", str(ORG), BOITE_ORG) not in org.coffre.lignes


# ── Les niveaux d'accès : le compte de l'org ne passe pas outre une équipe en lecture ─

def test_une_equipe_en_lecture_ne_se_fait_pas_ecrire_par_le_compte_de_l_org(org,
                                                                            monkeypatch):
    from oto_mcp import call_axes
    from oto_mcp.connectors import access_level_gate as gate
    from oto_mcp.connectors import access_levels as al

    decl = al._valider("outlook", {
        "levels": {"full": ("Mail.ReadWrite",), "read": ("Mail.Read",)},
        "scope_implies": {"Mail.ReadWrite": ("Mail.Read",)},
        "tools": {"outlook": {"get": ("Mail.Read",), "delete": ("Mail.ReadWrite",)}}})
    monkeypatch.setitem(al.DECLARED, "outlook", decl)
    monkeypatch.setattr(al, "settings", lambda o, g: (
        {PORTEUR: {"org": None, "group": "read"}} if g == EQUIPE else {}))
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: COLLEGUE)
    _poser(org, "org", ORG, BOITE_ORG, "RT-ORG")
    assert org.auth.access_token_for(COLLEGUE, "outlook") == "AT:RT-ORG"

    async def schema():
        return {}

    with pytest.raises(McpError) as e:
        asyncio.run(gate.require_covered("outlook", {"op": "delete"}, schema))
    assert (e.value.error.data["scope"], e.value.error.data["group_id"]) == (
        "group", EQUIPE)
    asyncio.run(gate.require_covered("outlook", {"op": "get"}, schema))
