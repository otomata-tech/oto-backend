"""Rejoindre une org installe d'office ce qui marche déjà pour le membre : un
connecteur dont une clé d'org, d'équipe, de tenant ou de plateforme résout — sous
l'exposition, comme le socle. Un connecteur sans clé n'en est pas : le départ vide
du 16/07 vaut pour lui."""
from __future__ import annotations

import pytest

from oto_mcp import access, org_store, providers, session_visibility as sv
from oto_mcp.connectors import activation, selection

SANS_CLE = next(c.name for c in providers.REGISTRY.values()
                if c.secret_kind == "none" and c.credential_of is None
                and not (c.keyed or c.credential_fields))


@pytest.fixture
def semis(monkeypatch):
    semes: dict[str, str] = {}
    monkeypatch.setattr(access, "org_admin_hidden_tools", lambda *_a: set())
    monkeypatch.setattr(access, "group_admin_hidden_tools", lambda *_a: set())
    monkeypatch.setattr(access, "current_group", lambda *_a: None)
    monkeypatch.setattr(access, "has_option", lambda *_a, **_k: False)
    monkeypatch.setattr(activation, "exposed_for", lambda *_a: set())
    monkeypatch.setattr(org_store, "get_org_default_connectors", lambda _o: ["kit_x"])
    monkeypatch.setattr(selection, "is_seeded", lambda *_a: False)
    monkeypatch.setattr(selection, "seed_active",
                        lambda _s, origins, _o: semes.update(origins))
    monkeypatch.setattr(selection, "list_selection", lambda *_a: {})
    return semes


def _status(monkeypatch, providers_):
    monkeypatch.setattr(access, "status_for",
                        lambda *_a, **_k: {"providers": providers_})


def test_les_cles_partagees_s_installent_pas_le_sans_cle(monkeypatch, semis):
    _status(monkeypatch, {
        "cle_org": {"mode": "org"}, "cle_equipe": {"mode": "group"},
        "cle_tenant": {"mode": "tenant"}, "cle_plateforme": {"mode": "platform"},
        "plateforme_epuisee": {"mode": "over_quota"},
        "autre_equipe": {"mode": "forbidden", "team_key_group": 3},
        "cle_perso": {"mode": "user"}, "rien": {"mode": "forbidden"},
        "non_expose": {"mode": "org"},
    })
    expose = {"cle_org", "cle_equipe", "cle_tenant", "cle_plateforme",
              "plateforme_epuisee", "autre_equipe", "cle_perso", "rien", SANS_CLE}
    monkeypatch.setattr(activation, "exposed_connectors", lambda *_a: expose)
    sv._compute_couches("sub-x", 7, 7, "member", set(), set(), set())
    assert semis == {
        "kit_x": selection.KIT,
        **{n: selection.SOCLE for n in ("cle_org", "cle_equipe", "cle_tenant",
                                        "cle_plateforme", "plateforme_epuisee",
                                        "autre_equipe")},
    }


def test_un_hoquet_de_lecture_retombe_sur_le_semis_d_avant(monkeypatch, semis):
    def _casse(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(access, "status_for", _casse)
    monkeypatch.setattr(activation, "exposed_connectors", lambda *_a: {SANS_CLE})
    sv._compute_couches("sub-x", 7, 7, "member", set(), set(), set())
    assert semis == {"kit_x": selection.KIT}
