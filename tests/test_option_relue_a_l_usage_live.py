"""Le droit d'une option payante est RELU à chaque usage de la clé plateforme
(ADR 0070 §7, lot 3 de #806), sur une vraie base pour les droits.

Avant : l'option ne gardait que l'ENTRÉE (brancher un compte). Un siège déjà branché
continuait de consommer la clé plateforme après la fin de l'essai ou de l'abonnement.

Ce que ces bancs tiennent, au palier plateforme de la résolution identifiée ET de
l'endpoint anonyme :
- droit échu → refus qui nomme la cause ;
- droit vivant → servi ;
- clé propre → servie, sans consulter le droit ;
- configurer une connexion (`check_usage=False`) → inchangé ;
- un connecteur sans option payante → inchangé ;
- une ligne de droit posée sur la PERSONNE (dans l'org ou partout) → servie, à elle
  seule ; hors org (projet partagé sans prêt), seule la personne partout compte.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from oto_mcp import access, db, session_org
from oto_mcp.access import resolve_anon
from oto_mcp.db import entitlements as E
from oto_mcp.db._conn import _connect
from oto_mcp.mcp_errors import McpError

HIER = datetime.now(timezone.utc) - timedelta(days=1)
DEMAIN = datetime.now(timezone.utc) + timedelta(days=1)


def _org() -> int:
    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (f"org-{uuid.uuid4().hex[:8]}",)).fetchone()["id"]


def _barreau(mode: str):
    if mode == "platform":
        return SimpleNamespace(mode="platform", entity_type="platform", entity_id="",
                               account=None,
                               payload={"secret": "CLE-PLATEFORME", "label": "env",
                                        "daily_quota": None})
    return SimpleNamespace(mode=mode, entity_type=mode, entity_id="x", account=None,
                           payload="CLE-PROPRE")


@pytest.fixture
def gagnant(monkeypatch):
    """La cascade est court-circuitée : seul le barreau gagnant compte ici."""
    etat = {"mode": "platform", "org": None}
    monkeypatch.setattr(access.chain_shadow, "barreau_gagnant",
                        lambda *a, **k: _barreau(etat["mode"]))
    monkeypatch.setattr(access, "cascade_winner",
                        lambda *a, **k: _barreau(etat["mode"]))
    monkeypatch.setattr(access, "_is_multi_account", lambda p, o: False)
    monkeypatch.setattr(session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p, *a: None)
    monkeypatch.setattr(access, "current_org", lambda sub: etat["org"])
    monkeypatch.setattr(db, "get_usage_today", lambda sub, p: 0)
    return etat


def _resoudre(provider="unipile", check_usage=True, sub="u-test"):
    return access.resolve._resolve_credential_impl(provider, "auto", sub,
                                                   check_usage=check_usage)


def _sub() -> str:
    """Une personne PROPRE au test : ses lignes de personne partout valent dans toute
    org de la base du module, donc aucun test ne les partage."""
    return f"u-{uuid.uuid4().hex[:8]}"


def test_droit_echu_refus_qui_nomme_la_cause(live, gagnant):
    gagnant["org"] = org = _org()
    E.grant(org, "unipile", "offered", value=1, expires_at=HIER)
    with pytest.raises(McpError) as e:
        _resoudre()
    msg = str(e.value)
    assert "is active neither for this org nor for you" in msg
    assert "trial ended or subscription required" in msg


def test_un_canal_suit_le_droit_de_son_porteur(live, gagnant):
    gagnant["org"] = _org()
    with pytest.raises(McpError):
        _resoudre("linkedin_unipile")


def test_droit_vivant_servi(live, gagnant):
    gagnant["org"] = org = _org()
    E.grant(org, "unipile", "subscription", value=1, expires_at=DEMAIN)
    rc = _resoudre()
    assert rc.is_platform is True and rc.key == "CLE-PLATEFORME"


@pytest.mark.parametrize("mode", ["user", "org"])
def test_cle_propre_servie_sans_droit(live, gagnant, mode):
    gagnant["org"] = _org()
    gagnant["mode"] = mode
    rc = _resoudre()
    assert rc.is_platform is False and rc.key == "CLE-PROPRE"


def test_configurer_une_connexion_ne_change_pas(live, gagnant):
    gagnant["org"] = _org()
    assert _resoudre(check_usage=False).is_platform is True


def test_sans_org_de_contexte_refus_nomme(live, gagnant):
    with pytest.raises(McpError) as e:
        _resoudre()
    assert "no org that holds it covers this call" in str(e.value)


def test_un_connecteur_sans_option_payante_n_est_pas_concerne(live, gagnant):
    gagnant["org"] = _org()
    assert _resoudre("apollo").is_platform is True


def test_endpoint_anonyme_droit_echu_refuse_vivant_servi(live, gagnant):
    org = _org()
    E.grant(org, "unipile", "offered", value=1, expires_at=HIER)
    with pytest.raises(McpError) as e:
        resolve_anon._resolve_credential_anon("unipile", "auto", org)
    assert "trial ended or subscription required" in str(e.value)
    E.grant(org, "unipile", "subscription", value=1, expires_at=DEMAIN)
    assert resolve_anon._resolve_credential_anon("unipile", "auto", org).is_platform


def test_endpoint_anonyme_cle_d_org_servie_sans_droit(live, gagnant):
    gagnant["mode"] = "org"
    rc = resolve_anon._resolve_credential_anon("unipile", "auto", _org())
    assert rc.is_platform is False


def test_le_beneficiaire_d_un_projet_partage_d_une_autre_org_n_a_pas_son_droit(
        live, gagnant):
    """#1499, D1 : l'org d'un projet partagé dont le bénéficiaire n'est pas membre ne
    le couvre pas — son droit payant non plus, et rien ne le lui prête."""
    gagnant["org"] = org = _org()
    E.grant(org, "unipile", "subscription", value=1, expires_at=DEMAIN)
    assert _resoudre().is_platform is True          # membre : le droit de l'org sert
    jeton = session_org.set_call_hors_org("u-test", org)
    try:
        with pytest.raises(McpError):
            _resoudre()
    finally:
        session_org.reset_call_hors_org(jeton)


# ── le droit posé sur la personne (ADR 0070 §7, « ne fait qu'ajouter ») ─────────

@pytest.mark.parametrize("partout", [False, True])
def test_un_droit_personnel_sert_sa_personne_seule(live, gagnant, partout):
    gagnant["org"] = org = _org()
    sub = _sub()
    E.grant(None if partout else org, "unipile", "offered", value=1, sub=sub)
    assert _resoudre(sub=sub).is_platform is True
    with pytest.raises(McpError) as e:
        _resoudre(sub=_sub())
    assert "is active neither for this org nor for you" in str(e.value)


def test_sans_org_servie_seule_la_personne_partout_ouvre(live, gagnant):
    """Bénéficiaire d'un projet partagé d'une autre org : l'org ne le couvre pas, sa
    ligne dans cette org non plus — sa ligne de personne partout, si."""
    gagnant["org"] = org = _org()
    sub = _sub()
    E.grant(org, "unipile", "offered", value=1, sub=sub)
    jeton = session_org.set_call_hors_org(sub, org)
    try:
        with pytest.raises(McpError) as e:
            _resoudre(sub=sub)
        assert "is not open to you personally" in str(e.value)
        E.grant(None, "unipile", "offered", value=1, sub=sub)
        assert _resoudre(sub=sub).is_platform is True
    finally:
        session_org.reset_call_hors_org(jeton)


def test_l_endpoint_anonyme_ne_lit_que_l_org(live, gagnant):
    """`sub` None : une ligne de personne n'ouvre rien à l'anonyme, et le refus reste
    celui d'avant, mot pour mot."""
    org = _org()
    E.grant(org, "unipile", "offered", value=1, sub=_sub())
    with pytest.raises(McpError) as e:
        resolve_anon._resolve_credential_anon("unipile", "auto", org)
    assert ("is not active for this org: trial ended or subscription required; "
            "your own `unipile` key is still served.") in str(e.value)
