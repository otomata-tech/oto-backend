"""Le compte, en capacité : mêmes chemins, mêmes replis, MÊME fil.

Les trois routes `/api/me`, `/api/me/calls` et `/api/me/activity-summary` ont quitté
`api_routes_account.py` (module supprimé) pour `capabilities/me_account.py` (27/08).
`GET /api/me` est la PREMIÈRE requête de tout front qui se branche — dashboard,
extension, front partenaire : une migration qui « promet d'être invisible » ne se
prouve qu'en lisant ce qui part sur le fil, via la vraie chaîne de l'adaptateur REST.

Deux points qui ne survivent PAS à une migration naïve, et qui sont l'objet de la
moitié de ce fichier :

1. **Le repli de saisie.** Les handlers d'origine faisaient `int(...)` sous
   `try/except ValueError` : `?days=abc` rendait 200 avec la fenêtre par défaut.
   Pydantic refuserait — un lien mal formé déjà en circulation deviendrait un 400
   visible. Les validateurs `mode="before"` rejouent l'ancien repli.
2. **La garde de champ inconnu s'applique désormais à ces chemins.** Un paramètre de
   query non déclaré était IGNORÉ en silence ; il est maintenant REFUSÉ (400
   `unknown_fields`). C'est le changement visible de ce lot, et il est voulu — mais il
   doit être exercé, pas supposé.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities import me_account as ma

_OUVERT = ma._abonnement.ouvert


@pytest.fixture()
def socle(monkeypatch):
    """Un compte complet en mémoire — on lit ce qui part sur le fil, sans DB."""
    vus: list = []

    monkeypatch.setattr(ma.db, "get_user", lambda sub: {
        "email": "a@b.c", "name": "A", "avatar_url": "https://x/a.png", "locale": "fr"})
    monkeypatch.setattr(ma.access, "status_for", lambda sub: {
        "role": "super_admin", "providers": {"serper": {"mode": "platform"}}})
    monkeypatch.setattr(ma.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(ma.access, "current_group", lambda sub: 7)
    monkeypatch.setattr(ma.access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(ma.org_store, "get_org",
                        lambda oid: {"id": oid, "name": "Otomata"})
    monkeypatch.setattr(ma.org_store, "effective_logo_url", lambda o: "https://cdn/l.png")
    monkeypatch.setattr(ma.org_store, "get_org_role", lambda oid, sub: "org_admin")
    monkeypatch.setattr(ma.org_store, "get_org_mfa", lambda oid: {"require_mfa": True})
    monkeypatch.setattr(ma.org_store, "is_personal_org", lambda oid: False)
    monkeypatch.setattr(ma.org_store, "get_active_org", lambda sub: 35)
    monkeypatch.setattr(ma.group_store, "get_group",
                        lambda gid: {"id": gid, "name": "Growth", "org_id": 35})
    monkeypatch.setattr(ma.group_store, "get_active_group", lambda sub: 7)
    monkeypatch.setattr(ma.billing, "is_enabled", lambda: True)
    monkeypatch.setattr(ma._abonnement, "ouvert", lambda sub: False)
    from oto_mcp import roles
    monkeypatch.setattr(roles, "effective_group_role", lambda sub, gid: "group_admin")

    def _list(**kw):
        vus.append(kw)
        return []

    def _stats(**kw):
        vus.append(kw)
        return {"since_days": kw["since_days"], "total_calls": 0, "error_count": 0,
                "active_users": 0, "by_tool": [], "by_user": [], "by_day": []}

    monkeypatch.setattr(ma.db, "list_tool_calls", _list)
    monkeypatch.setattr(ma.db, "tool_call_stats", _stats)
    return vus


# --- Le contrat du fil ------------------------------------------------------

def test_les_cles_de_api_me_sont_exactement_celles_servies(monkeypatch, socle):
    """La liste EXACTE, pas « au moins ». Un front lit `active_org_readonly` pour
    passer un écran en lecture et `features.billing` pour masquer une entrée de nav :
    une clé qui disparaît ne casse rien de visible côté serveur, elle casse le front."""
    stub_authz(monkeypatch)
    code, out = call("me.get")
    assert code == 200, out
    assert sorted(out) == sorted([
        "sub", "email", "email_verified", "name", "avatar_url", "locale", "role",
        "claude_subscription",
        "active_org", "active_org_name", "active_org_logo_url", "org_role",
        "active_org_readonly", "view_as_read_only",
        "view_as_bound_org", "view_as_refused_prefixes",
        "active_org_is_personal", "active_org_require_mfa",
        "home_org", "home_org_name",
        "active_group", "active_group_name", "group_role",
        "home_group", "home_group_name",
        "features", "providers",
    ])
    assert out["features"] == {"billing": True}
    assert out["providers"] == {"serper": {"mode": "platform"}}


def test_api_me_dit_l_instance_ouverte_a_tous(monkeypatch, socle):
    """Même source que la garde : une instance qui ouvre le chemin à tous le dit à
    chaque compte, option ou pas — le front affiche la connexion Claude."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(ma._abonnement, "ouvert", _OUVERT)
    monkeypatch.setattr(ma._abonnement.access, "has_option", lambda sub, option, **k: False)
    monkeypatch.setenv(ma._abonnement.ENV_OUVERT_A_TOUS, "1")
    code, out = call("me.get")
    assert code == 200, out
    assert out["claude_subscription"] is True
    monkeypatch.delenv(ma._abonnement.ENV_OUVERT_A_TOUS)
    code, out = call("me.get")
    assert code == 200, out
    assert out["claude_subscription"] is False


def test_api_me_declare_tout_ce_qu_il_sert(monkeypatch, socle):
    """`Output` DÉCRIT la réponse : s'il oubliait une clé, l'intégrateur générerait un
    type amputé de ce que le serveur envoie vraiment. On confronte les deux."""
    stub_authz(monkeypatch)
    _, out = call("me.get")
    assert set(out) == set(ma.MeView.model_fields)


def test_org_active_sans_role_et_operateur_vaut_lecture_seule(monkeypatch, socle):
    """Le flag qui fait afficher le bandeau « consultation » : org active posée, aucun
    rôle réel dedans, opérateur plateforme."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(ma.org_store, "get_org_role", lambda oid, sub: None)
    _, out = call("me.get")
    assert out["active_org_readonly"] is True
    assert out["org_role"] is None


def test_un_membre_n_est_jamais_en_lecture_seule(monkeypatch, socle):
    stub_authz(monkeypatch)
    monkeypatch.setattr(ma.access, "is_platform_operator", lambda sub: False)
    _, out = call("me.get")
    assert out["active_org_readonly"] is False


# --- `email_verified` : le claim du jeton Logto, `null` s'il est inconnu ----------

class _Verifieur:
    """Le jeton Logto vérifié, aux claims choisis par le banc."""

    def __init__(self, claims: dict):
        self.claims = claims

    async def verify_token(self, token):
        from types import SimpleNamespace
        return SimpleNamespace(claims=self.claims)


def _requete(scope_partage: dict | None = None):
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/api/me", "query_string": b"",
             "root_path": "", "scheme": "http", "server": ("test", 80),
             "http_version": "1.1", "headers": [(b"authorization", b"Bearer eyJ-un-jwt")]}
    return Request(scope if scope_partage is None else scope_partage)


def _me_apres_authentification(monkeypatch, claims: dict, *, sub_servi=None,
                               deux_fois=False):
    """`_authenticate` (chemin JWT) puis `_me`, dans le MÊME contexte : ce que le
    handler lit est ce que l'authentification de la requête a posé."""
    import asyncio
    from oto_mcp.api import base as api_base
    monkeypatch.setattr(api_base.db, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(api_base.db, "get_suspension", lambda sub: None)

    async def scenario():
        requete = _requete()
        sub, err = await api_base._authenticate(requete, verifier=_Verifieur(claims))
        assert err is None, err
        if deux_fois:
            # Le second appel de la même requête (middleware puis route) : le porteur
            # mémorisé, pas une nouvelle vérification.
            api_base._EMAIL_VERIFIE.set((None, None))
            sub, err = await api_base._authenticate(
                _requete(requete.scope), verifier=_Verifieur({}))
            assert err is None, err
        from types import SimpleNamespace
        return ma._me(SimpleNamespace(sub=sub_servi or sub), ma.MeInput())

    return asyncio.run(scenario())


@pytest.mark.parametrize("claims,attendu", [
    ({"email_verified": True}, True),
    ({"email_verified": False}, False),
    ({}, None),
    ({"email_verified": None}, None),
    ({"email_verified": "true"}, None),
], ids=["vrai", "faux", "absent", "nul", "illisible"])
def test_api_me_sert_email_verified_du_jeton(monkeypatch, socle, claims, attendu):
    out = _me_apres_authentification(
        monkeypatch, {"sub": "u-jwt", "email": "a@b.c", **claims})
    assert out["email_verified"] is attendu


def test_email_verified_survit_au_second_appel_de_la_requete(monkeypatch, socle):
    out = _me_apres_authentification(
        monkeypatch, {"sub": "u-jwt", "email_verified": True}, deux_fois=True)
    assert out["email_verified"] is True


def test_en_voir_en_tant_que_email_verified_est_inconnu(monkeypatch, socle):
    """Le jeton est celui de l'opérateur : il ne dit rien de l'adresse de la cible."""
    out = _me_apres_authentification(
        monkeypatch, {"sub": "u-operateur", "email_verified": True}, sub_servi="u-cible")
    assert out["email_verified"] is None


def test_sans_authentification_jwt_email_verified_est_inconnu(monkeypatch, socle):
    """Jeton API, worker, service : rien n'est posé — `null`, jamais un faux."""
    stub_authz(monkeypatch)
    _, out = call("me.get")
    assert out["email_verified"] is None


# --- « Voir en tant que » : la lecture seule que la cible ne porte pas (oto#212) ---

def _me_par_le_middleware(monkeypatch, entetes: dict) -> dict:
    """`GET /api/me` à travers le VRAI `ViewAsMiddleware` : c'est lui qui applique la
    vue (gardes comprises) ; le handler ne fait que la lire."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from types import SimpleNamespace

    from oto_mcp.api import routes as api_routes

    async def authentifie(request, verifier, **kw):
        return "u-operateur", None
    monkeypatch.setattr(api_routes, "_authenticate", authentifie)

    def me(request):  # synchrone → threadpool, comme les handlers de capacité
        return JSONResponse(ma._me(SimpleNamespace(sub="u-cible"), ma.MeInput()))

    app = Starlette(routes=[Route("/api/me", me)])
    c = TestClient(api_routes.ViewAsMiddleware(app, verifier=None))
    r = c.get("/api/me", headers={"Authorization": "Bearer x", **entetes})
    assert r.status_code == 200, r.text
    return r.json()


def test_voir_en_tant_que_annonce_la_lecture_seule(monkeypatch, socle):
    """La cible a un rôle réel : `active_org_readonly` est faux, et le serveur refuse
    pourtant toute écriture. Sans ce champ, l'écran propose des gestes voués au 403."""
    out = _me_par_le_middleware(monkeypatch, {"X-Oto-View-As": "u-cible"})
    assert out["view_as_read_only"] is True
    assert out["active_org_readonly"] is False


@pytest.mark.parametrize("entetes", [{}, {"X-Oto-View-As": "u-operateur"}],
                         ids=["sans-vue", "vue-sur-soi"])
def test_sans_vue_appliquee_pas_de_lecture_seule(monkeypatch, socle, entetes):
    """Une vue sur soi est un no-op du middleware : rien n'est refusé, rien ne
    s'annonce."""
    assert _me_par_le_middleware(monkeypatch, entetes)["view_as_read_only"] is False


# L'écriture ACCEPTÉE (super_admin + `X-Oto-View-As-Write: 1`, 24/09) lève la lecture
# seule : le serveur accepte alors les écritures, l'écran doit proposer les gestes. Le
# jugement est celui que `ViewAsMiddleware` applique déjà à l'en-tête, jamais refait.

def test_ecriture_acceptee_leve_la_lecture_seule(monkeypatch, socle):
    monkeypatch.setattr(ma.access, "is_super_admin", lambda sub: True)
    out = _me_par_le_middleware(
        monkeypatch, {"X-Oto-View-As": "u-cible", "X-Oto-View-As-Write": "1"})
    assert out["view_as_read_only"] is False


@pytest.mark.parametrize("super_admin, entetes", [
    (True, {"X-Oto-View-As": "u-cible"}),
    (True, {"X-Oto-View-As": "u-cible", "X-Oto-View-As-Write": "0"}),
    (False, {"X-Oto-View-As": "u-cible", "X-Oto-View-As-Write": "1"}),
], ids=["en-tete-absent", "en-tete-invalide", "operateur-non-super-admin"])
def test_ecriture_non_acceptee_reste_en_lecture_seule(monkeypatch, socle, super_admin,
                                                      entetes):
    """En-tête absent ou refusé par le middleware (un `admin` de supervision n'écrit
    pas, 403 `view_as_write_forbidden`) : la consultation reste en lecture seule."""
    monkeypatch.setattr(ma.access, "is_super_admin", lambda sub: super_admin)
    assert _me_par_le_middleware(monkeypatch, entetes)["view_as_read_only"] is True


def test_hors_org_les_champs_d_org_sont_nuls_pas_absents(monkeypatch, socle):
    """Un compte sans org rend les mêmes clés, à `null` — un front qui teste la
    PRÉSENCE d'une clé ne doit pas basculer de branche selon l'état du compte."""
    stub_authz(monkeypatch, org_id=None)
    monkeypatch.setattr(ma.access, "current_org", lambda sub: None)
    monkeypatch.setattr(ma.access, "current_group", lambda sub: None)
    monkeypatch.setattr(ma.org_store, "get_active_org", lambda sub: None)
    monkeypatch.setattr(ma.group_store, "get_active_group", lambda sub: None)
    code, out = call("me.get")
    assert code == 200, out
    assert set(out) == set(ma.MeView.model_fields)
    assert out["active_org"] is None and out["active_org_name"] is None
    assert out["active_org_require_mfa"] is False
    assert out["home_group"] is None


# --- Les filtres du journal atteignent bien le store ------------------------

def test_les_filtres_du_journal_arrivent_au_store(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.calls", query=b"limit=50&tool=oto_kb&errors=1&days=14")
    assert code == 200 and out == {"calls": []}
    assert socle[0] == {"limit": 50, "sub": "u-1", "org_id": 35,
                        "tool_name": "oto_kb", "errors_only": True, "since_days": 14}


@pytest.mark.parametrize("valeur,attendu", [(b"errors=1", True), (b"errors=true", True),
                                            (b"errors=0", False), (b"errors=oui", False),
                                            (b"", False)])
def test_errors_ne_filtre_que_sur_1_ou_true(monkeypatch, socle, valeur, attendu):
    """Comparaison littérale, telle qu'écrite depuis toujours : `?errors=yes` ne filtre
    PAS. Le figer évite qu'un « nettoyage » en fasse un booléen permissif."""
    stub_authz(monkeypatch)
    call("me.calls", query=valeur)
    assert socle[0]["errors_only"] is attendu


def test_le_journal_est_scope_au_demandeur_et_a_son_org(monkeypatch, socle):
    """L'anti-fuite de cette lentille : jamais le sub d'un autre, jamais une autre org
    que celle chargée — les deux viennent du serveur, aucun ne peut être passé."""
    stub_authz(monkeypatch)
    call("me.calls")
    assert socle[0]["sub"] == "u-1" and socle[0]["org_id"] == 35


# --- Le repli de saisie, préservé mot pour mot ------------------------------

@pytest.mark.parametrize("query,limit,days", [
    (b"", 200, None),
    (b"limit=abc", 200, None),          # illisible → défaut, PAS un 400
    (b"days=zz", 200, None),
    (b"days=", 200, None),              # vide → absent
    (b"limit=&days=3", 200, 3),
])
def test_une_saisie_illisible_retombe_sur_le_defaut(monkeypatch, socle, query, limit, days):
    stub_authz(monkeypatch)
    code, out = call("me.calls", query=query)
    assert code == 200, out
    assert socle[0]["limit"] == limit
    assert socle[0]["since_days"] == days


@pytest.mark.parametrize("query,attendu", [(b"", 7), (b"days=30", 30), (b"days=oups", 7),
                                           (b"days=", 7)])
def test_la_fenetre_des_agregats_garde_son_repli(monkeypatch, socle, query, attendu):
    stub_authz(monkeypatch)
    code, out = call("me.activity_summary", query=query)
    assert code == 200, out
    assert out["since_days"] == attendu
    assert socle[0] == {"since_days": attendu, "org_id": 35, "sub": "u-1"}


def test_la_fenetre_d_un_an_reste_servie(monkeypatch, socle):
    """#1145 : un front tiers en circulation demande `days=365` (« ce compte a-t-il
    déjà appelé un outil ? »). La fenêtre n'est pas bornée plus court qu'avant : le
    coût est tenu par la passe unique et la borne de durée, pas par un refus."""
    stub_authz(monkeypatch)
    code, out = call("me.activity_summary", query=b"days=365")
    assert code == 200, out
    assert socle[0]["since_days"] == 365


def test_sans_org_active_les_agregats_refusent_au_lieu_de_tout_lire(monkeypatch, socle):
    """Sans org, la lecture n'était bornée que par le compte : tout son historique."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(ma.access, "current_org", lambda sub: None)
    code, out = call("me.activity_summary")
    assert code == 400 and out["error"] == "no_active_org", out
    assert socle == []


def test_les_agregats_declarent_toutes_leurs_ventilations(monkeypatch, socle):
    stub_authz(monkeypatch)
    _, out = call("me.activity_summary")
    assert set(out) == set(ma.ActivitySummaryView.model_fields)


# --- Ce qui CHANGE pour un appelant -----------------------------------------

def test_un_parametre_inconnu_est_desormais_refuse(monkeypatch, socle):
    """**Changement visible.** Avant, un query param non lu par le handler était jeté en
    silence ; l'adaptateur le refuse, en NOMMANT le champ. Aucun consommateur connu
    (dashboard, extension, CLI) n'en envoie — vérifié au moment de la migration — mais
    le comportement doit être exercé, pas supposé."""
    stub_authz(monkeypatch)
    code, out = call("me.calls", query=b"limite=50")
    assert code == 400
    assert out["error"] == "unknown_fields"
    assert "limite" in out["detail"]
