"""Comptes Google PARTAGÉS par l'org ou l'équipe (2026-09-27).

Un admin d'org (ou un chef d'équipe) connecte UN compte Google au nom de tous — une
boîte partagée, un agenda d'équipe. Ce banc tient :

1. le CONSENTEMENT — seul un admin du scope le démarre ; le state signé porte le scope
   (et l'équipe), le callback range le jeton sous l'org ou l'équipe, jamais sous le
   membre qui a cliqué ;
2. la RÉSOLUTION — le compte du membre d'abord, puis l'équipe active, puis l'org ; un
   compte nommé se trouve où il vit ; le refresh et la santé s'écrivent sur l'entité
   qui porte la ligne ;
3. la GESTION — retirer ou choisir le défaut d'un compte partagé est un geste d'admin ;
4. la LECTURE — statut, lien de carte et identités voient les comptes partagés.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from oto_mcp import access, credentials_store, providers, roles  # noqa: E402
from oto_mcp.auth import google as G  # noqa: E402
from oto_mcp.capabilities import federated_oauth as fo  # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx  # noqa: E402
from oto_mcp.connectors import flow as connector_flow  # noqa: E402
from oto_mcp.connectors import identities  # noqa: E402
from oto_mcp.connectors import link as connector_link  # noqa: E402

pytestmark = pytest.mark.usefixtures("connecteurs_tous_disponibles")

ORG, GROUP = 7, 9
ALL = " ".join(G.SCOPES)


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "cid-env")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "secret-env")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "state-secret-test")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: GROUP)
    monkeypatch.setattr(credentials_store, "get_editor_app", lambda c, k: None)


def _admins(monkeypatch, org=False, group=False):
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org_id: org)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: group)


def _q(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ─── 1. le consentement ───────────────────────────────────────────────────────

def test_le_state_porte_le_scope_et_lequipe():
    etat = G.make_state("u", ORG, "", "gmail", "group", GROUP)
    assert G.verify_state(etat) == ("u", ORG, "", "gmail", "group", GROUP)
    assert G.verify_state(G.make_state("u", ORG, "", "drive", "org"))[4:] == ("org", None)
    # Un state d'avant les comptes partagés : le membre, comme alors.
    assert G.verify_state(G.make_state("u", ORG))[4:] == ("member", None)
    # Une équipe sans identifiant ne passe pas, même bien signée.
    assert G.verify_state(G.make_state("u", ORG, "", "gmail", "group", None)) is None


def test_seul_un_admin_de_lorg_connecte_un_compte_de_lorg(monkeypatch):
    _admins(monkeypatch, org=False)
    with pytest.raises(PermissionError):
        G.build_auth_url("u", connector="gmail", scope="org")
    _admins(monkeypatch, org=True)
    etat = _q(G.build_auth_url("u", connector="gmail", scope="org"))["state"]
    assert G.verify_state(etat)[4:] == ("org", None)


def test_seul_un_chef_dequipe_connecte_un_compte_dequipe(monkeypatch):
    _admins(monkeypatch, group=False)
    with pytest.raises(PermissionError):
        G.build_auth_url("u", connector="calendar", scope="group")
    _admins(monkeypatch, group=True)
    etat = _q(G.build_auth_url("u", connector="calendar", scope="group"))["state"]
    assert G.verify_state(etat)[4:] == ("group", GROUP)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    with pytest.raises(PermissionError):
        G.build_auth_url("u", connector="calendar", scope="group")


@pytest.mark.asyncio
async def test_le_flux_refuse_un_non_admin_en_nommant_le_refus(monkeypatch):
    _admins(monkeypatch, org=False)

    class _C:
        sub = "u"
    with pytest.raises(AuthzDenied) as e:
        await connector_flow.start("drive", _C(), {"scope": "org"})
    assert e.value.status == 403 and e.value.code == "scope_forbidden"
    with pytest.raises(AuthzDenied) as e:
        await connector_flow.start("drive", _C(), {"scope": "planete"})
    assert e.value.code == "invalid_scope"


def test_le_jeton_partage_se_range_sous_lorg_ou_lequipe(monkeypatch):
    vus = []
    monkeypatch.setattr(G, "_fetch_email", lambda tok, scopes=(): "hello@acme.test")
    monkeypatch.setattr(G.db, "set_shared_google_oauth",
                        lambda scope, target, **k: vus.append((scope, target, k["set_by"])))
    monkeypatch.setattr(G.db, "list_shared_google_accounts", lambda scope, target: [])
    monkeypatch.setattr(G.db, "set_google_oauth",
                        lambda *a, **k: pytest.fail("jamais sous le membre qui a cliqué"))
    jeton = {"refresh_token": "rt", "access_token": "at", "expires_in": 3600, "scope": ALL}
    G.persist_token("u", ORG, jeton, scope="org", connector="google")
    G.persist_token("u", ORG, jeton, scope="group", group_id=GROUP, connector="google")
    assert vus == [("org", ORG, "u"), ("group", GROUP, "u")]
    # La carte qui a demandé le consentement borne le partage : sans elle, refus.
    with pytest.raises(ValueError):
        G.persist_token("u", ORG, jeton, scope="org")


def _partage_capture(monkeypatch, deja=None):
    ecrits = []
    monkeypatch.setattr(G, "_fetch_email", lambda tok, scopes=(): "hello@acme.test")
    monkeypatch.setattr(G.db, "set_shared_google_oauth",
                        lambda scope, target, **k: ecrits.append(k))
    monkeypatch.setattr(G.db, "list_shared_google_accounts", lambda scope, target: (
        [{"google_email": "hello@acme.test", "scopes": deja}] if deja else []))
    return ecrits


def test_partager_drive_ne_livre_pas_le_gmail_perso_du_titulaire(monkeypatch):
    """Revue de #1081 (B2) : le titulaire a déjà autorisé Gmail POUR LUI ; le jeton du
    consentement Drive porte l'union (`include_granted_scopes`). La ligne partagée ne
    garde que Drive — ni Gmail, ni l'access token d'échange qui porte l'union."""
    ecrits = _partage_capture(monkeypatch)
    gmail, drive = G.SERVICE_SCOPES["gmail"][0], G.SERVICE_SCOPES["drive"][0]
    union = " ".join([*G.IDENTITY_SCOPES, gmail, drive])
    G.persist_token("u", ORG, {"refresh_token": "rt", "access_token": "at-union",
                               "expires_in": 3600, "scope": union},
                    scope="org", connector="drive")
    (k,) = ecrits
    assert gmail not in k["scopes"].split()
    assert G.services_granted(k["scopes"]) == ["drive"]
    assert k["access_token"] is None and k["expires_at"] is None


def test_un_partage_incremental_garde_ce_quil_partageait_deja(monkeypatch):
    drive, cal = G.SERVICE_SCOPES["drive"][0], G.SERVICE_SCOPES["calendar"][0]
    ecrits = _partage_capture(monkeypatch, deja=" ".join([*G.IDENTITY_SCOPES, drive]))
    union = " ".join([*G.IDENTITY_SCOPES, drive, cal, G.SERVICE_SCOPES["gmail"][0]])
    G.persist_token("u", ORG, {"refresh_token": "rt", "access_token": "at",
                               "expires_in": 3600, "scope": union},
                    scope="org", connector="calendar")
    assert G.services_granted(ecrits[0]["scopes"]) == ["drive", "calendar"]


# ─── 2. la résolution ─────────────────────────────────────────────────────────

def _row(email, scopes=ALL, token="AT"):
    return {"google_email": email, "refresh_token": "RT", "access_token": token,
            "expires_at": "2999-01-01T00:00:00+00:00", "scopes": scopes, "client_id": None}


def _coffre(monkeypatch, membre=None, groupe=None, org=None, token="AT"):
    """Le faux coffre sous la VRAIE résolution commune (oto-backend#1160) : chaque
    palier porte au plus un compte, défaut de son palier. Rend le coffre."""
    from _coffre_google import installer

    env = installer(monkeypatch, org=ORG, sub="u",
                    group=access.current_group("u"))
    for entite, email in ((None, membre), (("group", str(GROUP)), groupe),
                          (("org", str(ORG)), org)):
        if email:
            env.coffre.poser(email, f"RT-{email}", entite=entite, defaut=True,
                             access_token=token)
    return env.coffre


def _sert(**k) -> str:
    """Le compte qui sert un appel Gmail (son refresh token le désigne)."""
    return G.credentials_for("u", service="gmail", **k).refresh_token.removeprefix("RT-")


def test_le_membre_dabord_puis_lequipe_puis_lorg(monkeypatch):
    _coffre(monkeypatch, membre="moi@x.test", groupe="team@x.test", org="hello@x.test")
    assert G.credentials_for("u").token == "AT"
    assert _sert() == "moi@x.test"
    _coffre(monkeypatch, groupe="team@x.test", org="hello@x.test")
    assert _sert() == "team@x.test"
    _coffre(monkeypatch, org="hello@x.test")
    assert _sert() == "hello@x.test"
    # Un compte NOMMÉ se trouve où il vit.
    _coffre(monkeypatch, membre="moi@x.test", org="hello@x.test")
    assert _sert(account="hello@x.test") == "hello@x.test"


def test_les_comptes_partages_sont_ceux_de_l_equipe_active_puis_de_l_org(monkeypatch):
    """Les paliers de la cascade (#1499 retire l'héritage d'un projet partagé) : l'équipe
    ACTIVE, puis l'org ; sans équipe active, l'org seule."""
    monkeypatch.setattr(access, "current_group", lambda sub: GROUP)
    assert G._shared_targets("u", ORG) == [("group", GROUP), ("org", ORG)]
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    assert G._shared_targets("u", ORG) == [("org", ORG)]


def test_le_refresh_et_la_sante_secrivent_sur_lentite_partagee(monkeypatch):
    coffre = _coffre(monkeypatch, org="hello@x.test", token=None)
    sante = []
    from oto_mcp.connectors import health
    monkeypatch.setattr(health, "record_health", lambda prov, scope, ok, err: sante.append(scope))

    class _OK:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "AT-NEUF", "expires_in": 3600}

        def raise_for_status(self):
            pass
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _OK())
    creds = G.credentials_for("u", service="gmail")
    assert creds.token == "AT-NEUF"
    assert coffre.meta("hello@x.test", ("org", str(ORG)))["access_token"] == "AT-NEUF"
    assert sante == [("org", str(ORG), "hello@x.test")]


# ─── 3. la gestion ────────────────────────────────────────────────────────────

def test_retirer_un_compte_partage_est_un_geste_dadmin(monkeypatch):
    retraits = []
    monkeypatch.setattr(G.db, "list_shared_google_accounts",
                        lambda scope, target: [{"google_email": "hello@x.test"}])
    monkeypatch.setattr(G.db, "get_shared_google_oauth", lambda *a, **k: None)
    monkeypatch.setattr(G.db, "delete_shared_google_oauth",
                        lambda scope, target, account=None, *, par: retraits.append((scope, target, account)))
    _admins(monkeypatch, org=False)
    with pytest.raises(AuthzDenied) as e:
        fo._google_revoke(ResolvedCtx(sub="u"), fo.GoogleRevokeInput(account="hello@x.test", scope="org"))
    assert e.value.code == "scope_forbidden" and retraits == []
    _admins(monkeypatch, org=True)
    fo._google_revoke(ResolvedCtx(sub="u"), fo.GoogleRevokeInput(account="hello@x.test", scope="org"))
    assert retraits == [("org", ORG, "hello@x.test")]


def test_le_defaut_partage_se_choisit_par_un_admin(monkeypatch):
    monkeypatch.setattr(G.db, "set_default_shared_google_account",
                        lambda scope, target, account, *, par: (scope, target) == ("group", GROUP))
    _admins(monkeypatch, group=True)
    out = fo._google_set_default(ResolvedCtx(sub="u"),
                                 fo.GoogleDefaultInput(account="team@x.test", scope="group"))
    assert out == {"ok": True, "default": "team@x.test"}
    _admins(monkeypatch, group=False)
    with pytest.raises(AuthzDenied):
        fo._google_set_default(ResolvedCtx(sub="u"),
                               fo.GoogleDefaultInput(account="team@x.test", scope="group"))


# ─── 4. la lecture ────────────────────────────────────────────────────────────

def _partages(monkeypatch):
    from oto_mcp import roles
    # Ni chef de l'équipe ni admin de l'org : le niveau lu est celui des défauts.
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: False)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(G.db, "list_shared_google_accounts", lambda scope, target: [
        {"google_email": f"{scope}@x.test", "is_default": True, "scopes": ALL,
         "granted_at": None, "scope": scope, "target_id": target,
         "connected_by": f"admin-{scope}"}])
    monkeypatch.setattr(G.db, "list_google_accounts", lambda sub, org: [])


def test_le_statut_montre_les_comptes_partages(monkeypatch):
    _partages(monkeypatch)
    out = fo._google_status(ResolvedCtx(sub="u"), fo.OAuthStatusInput())
    assert out["accounts"] == []
    assert [(a["email"], a["scope"]) for a in out["shared"]] == [
        ("group@x.test", "group"), ("org@x.test", "org")]
    # `ALL` = le consentement du COMPTE : les six du split, pas bigquery (2026-10-02)
    # ni google_ads (2026-10-08), qui ne s'autorisent que depuis leur carte.
    assert out["shared"][0]["services"] == [s for s in G.SERVICES
                                            if s not in ("bigquery", "google_ads")]
    # Qui l'a connecté : lu dans le statut, validé par le modèle servi.
    assert [a["connected_by"] for a in out["shared"]] == ["admin-group", "admin-org"]
    assert fo.GoogleStatus(**out).shared[1].connected_by == "admin-org"


def test_le_retrait_dun_compte_partage_se_journalise(monkeypatch, caplog):
    monkeypatch.setattr(G.db, "list_shared_google_accounts", lambda scope, target: [
        {"google_email": "hello@x.test", "connected_by": "admin-1"}])
    monkeypatch.setattr(G.db, "get_shared_google_oauth", lambda *a, **k: None)
    monkeypatch.setattr(G.db, "delete_shared_google_oauth", lambda *a, **k: None)
    _admins(monkeypatch, org=True)
    with caplog.at_level("INFO", logger=G.__name__):
        G.revoke("admin-2", account="hello@x.test", scope="org")
    (ligne,) = [r.getMessage() for r in caplog.records if "removed" in r.getMessage()]
    assert "hello@x.test" in ligne and "by=admin-2" in ligne and "admin-1" in ligne


def test_la_carte_dun_service_est_reliee_par_un_compte_partage(monkeypatch):
    _partages(monkeypatch)
    assert connector_link.state("drive", "u").linked is True
    assert connector_link.state("drive", "u").accounts == 2


def test_les_identites_proposent_les_comptes_partages_avec_leur_niveau(monkeypatch):
    """#1499 : plus de marque `shared` ; un compte qui n'est pas le sien porte le
    `level` que ses règles donnent (sans règle : les défauts d'oto-core — membre de
    l'équipe `sensitive`, membre de l'org `read`) et son `owner`."""
    _partages(monkeypatch)
    ids = identities.list_identities("u", "gmail")
    assert [i["id"] for i in ids] == ["group@x.test", "org@x.test"]
    assert all(i["is_default"] is False and "shared" not in i for i in ids)
    assert [(i["label"], i["level"], i["owner"]["type"]) for i in ids] == [
        ("group@x.test", "sensitive", "group"), ("org@x.test", "read", "org")]


def test_le_contrat_sert_level_et_owner_jamais_shared(monkeypatch):
    """`level` et `owner` traversent le modèle de sortie ; absents sur un compte du
    membre (il le gère)."""
    from oto_mcp.capabilities.connectors.identities import ConnectorIdentities

    _partages(monkeypatch)
    monkeypatch.setattr(G.db, "list_google_accounts", lambda sub, org: [
        {"google_email": "moi@x.test", "is_default": True, "scopes": ALL}])
    servi = ConnectorIdentities(connector="gmail", supported=True,
                                identities=identities.list_identities("u", "gmail")
                                ).model_dump(mode="json")
    assert {i["id"]: (i["level"], (i["owner"] or {}).get("type"))
            for i in servi["identities"]} == {
        "moi@x.test": (None, None), "group@x.test": ("sensitive", "group"),
        "org@x.test": ("read", "org")}
    assert not any("shared" in i or "granted" in i for i in servi["identities"])


def test_le_compte_google_accepte_le_palier_org_et_ses_services_non():
    assert "byo_org" in providers.REGISTRY["google"].auth_modes
    providers.require_credential("org", "google")          # ne lève pas
    providers.require_credential("group", "google")
    with pytest.raises(ValueError):
        providers.require_credential("org", "drive")       # délégué : pas de clé à lui


# ─── 5. les outils nomment les comptes partagés ───────────────────────────────
#
# Un compte partagé se résout par son adresse (`credentials_for`) — mais les listes que
# l'agent lit pour choisir l'adresse (`gmail_list_accounts`, `google_accounts`, le
# message « aucun compte ») ne montraient que les comptes du membre. Un agent ne
# pouvait donc pas découvrir la boîte qu'il avait le droit d'employer.

def _deux_niveaux(monkeypatch, membre=(), partages=()):
    monkeypatch.setattr(G.db, "list_google_accounts", lambda sub, org: [
        {"google_email": e, "is_default": i == 0, "scopes": ALL, "granted_at": None}
        for i, e in enumerate(membre)])
    gmail_seul = " ".join([*G.IDENTITY_SCOPES, G.SERVICE_SCOPES["gmail"][0]])
    drive_seul = " ".join([*G.IDENTITY_SCOPES, G.SERVICE_SCOPES["drive"][0]])
    monkeypatch.setattr(G.db, "list_shared_google_accounts", lambda scope, target: [
        {"google_email": e, "is_default": i == 0, "granted_at": None, "scope": scope,
         "scopes": drive_seul if e.startswith("drive") else gmail_seul}
        for i, (s, e) in enumerate(p for p in partages if p[0] == scope)])


def test_les_comptes_atteignables_suivent_lordre_de_resolution(monkeypatch):
    _deux_niveaux(monkeypatch, membre=["moi@x.test"],
                  partages=[("group", "team@x.test"), ("org", "boss@x.test"),
                            ("org", "moi@x.test")])
    out = G.reachable_accounts("u")
    assert [(a["google_email"], a["shared"]) for a in out] == [
        ("moi@x.test", None), ("team@x.test", "group"), ("boss@x.test", "org")]
    # Le défaut reste celui du membre : un partagé ne l'est jamais à sa place.
    assert [a["google_email"] for a in out if a["is_default"]] == ["moi@x.test"]


def test_sans_compte_a_lui_le_defaut_est_le_partage_le_plus_proche(monkeypatch):
    _deux_niveaux(monkeypatch, partages=[("org", "hello@x.test"), ("org", "sales@x.test")])
    out = G.reachable_accounts("u")
    assert [(a["google_email"], a["is_default"]) for a in out] == [
        ("hello@x.test", True), ("sales@x.test", False)]


def test_gmail_ne_liste_pas_un_compte_partage_pour_drive_seulement(monkeypatch):
    _deux_niveaux(monkeypatch, partages=[("org", "drive@x.test"), ("org", "boite@x.test")])
    assert [a["google_email"] for a in G.reachable_accounts("u", service="gmail")] == [
        "boite@x.test"]
    assert len(G.reachable_accounts("u")) == 2


def test_aucun_compte_nomme_aussi_les_comptes_partages(monkeypatch):
    _deux_niveaux(monkeypatch, membre=["moi@x.test"], partages=[("org", "boss@x.test")])
    msg = G._no_account_message("u", ORG, "inconnu@x.test")
    assert "moi@x.test" in msg and "boss@x.test" in msg


# ─── 6. retirer une copie ne tue pas l'autre ──────────────────────────────────
#
# Google traite un `/revoke` comme la fin de l'accès de l'app au compte entier. Une
# adresse connectée pour soi ET partagée à l'org, c'est deux lignes et UN accès chez
# Google : révoquer en retirant l'une coupait l'autre.

def _revocations(monkeypatch):
    vus = []

    class _R:
        status_code = 200
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **k: vus.append(url) or _R())
    return vus


def _detenteurs(monkeypatch, *lignes):
    monkeypatch.setattr(G.db, "google_grant_holders", lambda email: [
        {"entity_type": et, "entity_id": eid, "client_id": cid} for et, eid, cid in lignes])


def test_retirer_la_copie_partagee_garde_lacces_du_titulaire(monkeypatch):
    revoques = _revocations(monkeypatch)
    _admins(monkeypatch, org=True)
    monkeypatch.setattr(G.db, "list_shared_google_accounts",
                        lambda scope, target: [{"google_email": "moi@x.test"}])
    monkeypatch.setattr(G.db, "get_shared_google_oauth",
                        lambda *a, **k: {**_row("moi@x.test"), "client_id": "cid-env"})
    monkeypatch.setattr(G.db, "delete_shared_google_oauth", lambda *a, **k: None)
    _detenteurs(monkeypatch, ("org", str(ORG), "cid-env"), ("member", f"{ORG}:u", "cid-env"))
    G.revoke("u", account="moi@x.test", scope="org")
    assert revoques == []
    # Seule copie : là, on révoque chez Google, comme avant.
    _detenteurs(monkeypatch, ("org", str(ORG), "cid-env"))
    G.revoke("u", account="moi@x.test", scope="org")
    assert revoques == ["https://oauth2.googleapis.com/revoke"]


def test_retirer_sa_copie_garde_la_boite_partagee(monkeypatch):
    revoques = _revocations(monkeypatch)
    monkeypatch.setattr(G.db, "get_google_oauth",
                        lambda sub, org, account=None: {**_row("moi@x.test"), "client_id": "cid-env"})
    monkeypatch.setattr(G.db, "delete_google_oauth", lambda *a, **k: None)
    _detenteurs(monkeypatch, ("member", f"{ORG}:u", "cid-env"), ("org", str(ORG), "cid-env"))
    G.revoke("u", account="moi@x.test")
    assert revoques == []


def test_un_autre_client_oauth_nest_pas_le_meme_acces(monkeypatch):
    """Le même compte connecté sous l'app d'un tenant et sous la nôtre : deux accès
    distincts chez Google — révoquer l'un ne touche pas l'autre, donc on révoque."""
    _detenteurs(monkeypatch, ("member", f"{ORG}:u", "cid-env"), ("org", str(ORG), "cid-tenant"))
    assert G._grant_held_elsewhere("moi@x.test", "cid-env", ("member", f"{ORG}:u")) is False
    # Émetteur inconnu sur une ligne d'avant qu'on le note : dans le doute, on garde.
    _detenteurs(monkeypatch, ("member", f"{ORG}:u", "cid-env"), ("org", str(ORG), None))
    assert G._grant_held_elsewhere("moi@x.test", "cid-env", ("member", f"{ORG}:u")) is True


def test_un_coffre_illisible_ne_fait_pas_revoquer(monkeypatch):
    def boum(email):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(G.db, "google_grant_holders", boum)
    assert G._grant_held_elsewhere("moi@x.test", "cid-env", ("org", str(ORG))) is True


def test_le_descriptif_openapi_decrit_level():
    from oto_mcp import openapi

    proprietes = openapi.build()["components"]["schemas"]["Identity"]["properties"]
    assert "shared" not in proprietes and "granted" not in proprietes
    assert {"type": "string", "enum": ["read", "write", "sensitive", "manage"]} in \
        proprietes["level"]["anyOf"]
