"""`oto_credits` : les lectures que le commerce tient, relayées en lecture seule.

Ce qui est tenu : l'outil n'existe pas sans config ; l'appartenance se vérifie ICI avant
tout appel ; le commerce reçoit la personne et son rôle DANS l'org (pas le rôle de
plateforme) ; une org d'un tenant hébergé n'est jamais relayée ; et un commerce muet ou
en erreur rend un refus nommé, pas une exception brute."""
import pytest
import requests

from oto_mcp import roles
from oto_mcp.capabilities import commerce_relay as relay
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

BASE = "https://commerce.example/service/oto"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OTO_COMMERCE_RELAY_URL", BASE + "/")
    monkeypatch.setenv("OTO_COMMERCE_RELAY_TOKEN", "s3cret")
    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", "main")
    monkeypatch.setattr(relay.tenants, "org_tenant_slug", lambda org_id: "main")
    monkeypatch.setattr(relay.roles, "effective_org_role",
                        lambda sub, org_id: roles.ORG_MEMBER)


class _R:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {"ok": True} if body is None else body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _spy(monkeypatch, reply=None):
    calls = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if isinstance(reply, Exception):
            raise reply
        return reply or _R()

    monkeypatch.setattr(relay.requests, "get", get)
    return calls


def _cap():
    return next(c for c in CAPABILITIES if c.key == "org.commerce.credits")


def _run(op, org_id=7, sub="u-1", **kw):
    return relay._relayer(ResolvedCtx(sub=sub, org_id=org_id, role="user"),
                          relay.CreditsInput(op=op, org_id=org_id, **kw))


# ── montage ──────────────────────────────────────────────────────────────────

def test_l_outil_n_est_monte_que_si_l_instance_declare_son_commerce(monkeypatch):
    assert _cap().mcp == "oto_credits" and _cap().rest is None
    assert _cap().is_exposed()
    monkeypatch.delenv("OTO_COMMERCE_RELAY_TOKEN")
    assert not _cap().is_exposed()
    monkeypatch.setenv("OTO_COMMERCE_RELAY_TOKEN", "s3cret")
    monkeypatch.setenv("OTO_COMMERCE_RELAY_URL", " ")
    assert not _cap().is_exposed()


def test_sans_config_le_handler_refuse_sans_rien_envoyer(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.delenv("OTO_COMMERCE_RELAY_URL")
    with pytest.raises(AuthzDenied) as e:
        _run("usage")
    assert e.value.code == "commerce_not_configured" and calls == []


# ── autorisation ─────────────────────────────────────────────────────────────

@pytest.fixture
def _sans_base(monkeypatch):
    """Les deux lectures de compte que font les règles (org active, rôle de plateforme)."""
    from oto_mcp import access
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "user")


def test_une_org_se_lit_par_ses_membres_seulement(monkeypatch, _sans_base):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org_id: False)
    for op in ("usage", "daily", "agents", "spend_limits"):
        with pytest.raises(AuthzDenied) as e:
            _cap().authz(RawCtx(sub="u-1"), relay.CreditsInput(op=op, org_id=7))
        assert e.value.status == 403
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org_id: org_id == 7)
    ctx = _cap().authz(RawCtx(sub="u-1"), relay.CreditsInput(op="usage", org_id=7))
    assert ctx.org_id == 7 and ctx.sub == "u-1"


def test_une_lecture_d_org_exige_l_org(monkeypatch, _sans_base):
    with pytest.raises(AuthzDenied):
        _cap().authz(RawCtx(sub="u-1"), relay.CreditsInput(op="usage"))


def test_les_tarifs_se_lisent_sans_org(_sans_base):
    ctx = _cap().authz(RawCtx(sub="u-1"), relay.CreditsInput(op="pricing"))
    assert ctx.sub == "u-1"


# ── ce qui part chez le commerce ─────────────────────────────────────────────

@pytest.mark.parametrize("op, chemin", [
    ("usage", "/orgs/7/credits"), ("daily", "/orgs/7/credits/daily"),
    ("agents", "/orgs/7/credits/agents"), ("spend_limits", "/orgs/7/spend-limits"),
])
def test_chaque_op_appelle_son_chemin_avec_le_bearer_et_la_personne(monkeypatch, op, chemin):
    calls = _spy(monkeypatch, _R(body={"used_credits": 12.5}))
    out = _run(op)
    assert out == {"op": op, "org_id": 7, "result": {"used_credits": 12.5}}
    c = calls[0]
    assert c["url"] == BASE + chemin          # le `/` final de la base est retiré
    assert c["headers"]["Authorization"] == "Bearer s3cret"
    assert c["headers"]["X-Oto-Sub"] == "u-1"
    assert c["headers"]["X-Oto-Org-Role"] == "org_member"
    assert c["timeout"] == relay._TIMEOUT_S


def test_le_role_envoye_est_celui_dans_l_org_pas_celui_de_plateforme(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(relay.roles, "effective_org_role",
                        lambda sub, org_id: roles.ORG_ADMIN)
    _run("usage")
    assert calls[0]["headers"]["X-Oto-Org-Role"] == "org_admin"


def test_la_fenetre_par_defaut_est_de_trente_jours(monkeypatch):
    calls = _spy(monkeypatch)
    _run("daily")
    _run("agents", days=7)
    _run("usage")
    assert [c["params"] for c in calls] == [{"days": 30}, {"days": 7}, {}]


def test_une_fenetre_hors_contrat_est_refusee_a_l_entree():
    with pytest.raises(ValueError):
        relay.CreditsInput(op="daily", org_id=7, days=12)


def test_les_tarifs_partent_sans_org(monkeypatch):
    calls = _spy(monkeypatch, _R(body={"pricing": []}))
    out = relay._relayer(ResolvedCtx(sub="u-1"), relay.CreditsInput(op="pricing"))
    assert out["org_id"] is None and calls[0]["url"] == BASE + "/pricing"


def test_une_org_d_un_tenant_heberge_n_est_jamais_relayee(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(relay.tenants, "org_tenant_slug", lambda org_id: "partenaire")
    with pytest.raises(AuthzDenied) as e:
        _run("usage")
    assert e.value.code == "no_commerce" and calls == []


def test_sans_tenant_primaire_declare_on_ne_devine_pas(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.delenv("OTO_TENANT_PRIMAIRE_SLUG")
    with pytest.raises(AuthzDenied) as e:
        _run("usage")
    assert e.value.code == "commerce_not_configured" and calls == []


# ── ce que rend un commerce qui ne répond pas comme prévu ───────────────────

def test_un_commerce_injoignable_est_un_502_nomme(monkeypatch):
    _spy(monkeypatch, requests.ConnectionError("down"))
    with pytest.raises(AuthzDenied) as e:
        _run("usage")
    assert (e.value.status, e.value.code) == (502, "commerce_unreachable")


def test_une_lecture_reservee_a_l_admin_le_dit(monkeypatch):
    _spy(monkeypatch, _R(403, {"error": "admin_required"}))
    with pytest.raises(AuthzDenied) as e:
        _run("daily")
    assert (e.value.status, e.value.code) == (403, "admin_required")


@pytest.mark.parametrize("reply", [_R(500, {"error": "boom"}), _R(401, {"error": "unauthorized"}),
                                   _R(200, ValueError("not json")), _R(200, ["a", "list"])])
def test_une_reponse_inattendue_est_un_502_nomme(monkeypatch, reply):
    _spy(monkeypatch, reply)
    with pytest.raises(AuthzDenied) as e:
        _run("usage")
    assert (e.value.status, e.value.code) == (502, "commerce_error")


def test_le_secret_ne_part_jamais_dans_un_refus(monkeypatch):
    _spy(monkeypatch, _R(500, {"error": "boom"}))
    with pytest.raises(AuthzDenied) as e:
        _run("usage")
    assert "s3cret" not in str(e.value)
