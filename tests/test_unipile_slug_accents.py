"""Un slug accentué : tel quel d'abord, sans accents en repli (#180).

Un slug LinkedIn peut porter une lettre non ASCII (`…-née-…`) — retirer les
accents avant l'appel envoyait un slug qui n'existe pas (404). #180 : un agent qui
accentue un slug ASCII (`chloé-dupré` pour `chloe-dupre`) se voyait refuser en 403.
`_read_li_slug` envoie le slug tel quel, puis, sur un 403/404 d'un slug non ASCII
SEULEMENT, une seule fois sa forme sans accents ; la réponse dit laquelle a servi
(`identifier_form`), jamais le slug lui-même (un profil anonymisé ne le récupère pas).
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto.tools.unipile.client import UnipileError, UnipileRateLimited


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def client(monkeypatch):
    from oto_mcp.tools import unipile as U

    inst = MagicMock()
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr(U, "_rate_limit_guard", lambda sub: None)
    U._COMPANY_CACHE.clear()
    return inst


def _answers(*outcomes):
    """Réponses successives du client : un dict rendu, une exception levée."""
    it = iter(outcomes)

    def call(ident, **kw):
        out = next(it)
        if isinstance(out, Exception):
            raise out
        return dict(out, asked=ident)
    return call


def _person(identifier):
    return _tool("linkedin_unipile_profile")(op="person", identifier=identifier)


@pytest.mark.parametrize("identifier", [
    "marie-dupont-n%c3%a9e-martin-123456789",
    "marie-dupont-née-martin-123456789",
    "https://www.linkedin.com/in/marie-dupont-n%C3%A9e-martin-123456789/",
])
def test_un_slug_accentue_part_tel_quel_et_dit_la_forme(client, identifier):
    client.get_profile.side_effect = _answers({"public_identifier": "x"})
    out = _person(identifier)
    assert [c.args[0] for c in client.get_profile.call_args_list] == [
        "marie-dupont-née-martin-123456789"]
    assert out["identifier_form"] == "as_given"
    assert "warnings" not in out


@pytest.mark.parametrize("status", [403, 404])
def test_refuse_tel_quel_il_est_relu_une_fois_sans_accents(client, status):
    client.get_profile.side_effect = _answers(
        UnipileError(f"Unipile {status}: x", status_code=status), {"public_identifier": "x"})
    out = _person("chloé-dupré")
    assert [c.args[0] for c in client.get_profile.call_args_list] == ["chloé-dupré", "chloe-dupre"]
    assert out["asked"] == "chloe-dupre"
    assert out["identifier_form"] == "without_accents"
    assert "accent-free form" in out["warnings"][0]
    assert "chloe" not in " ".join(out["warnings"])  # la forme, jamais le slug


def test_introuvable_des_deux_facons_l_erreur_nomme_le_slug_demande(client):
    client.get_profile.side_effect = _answers(
        UnipileError("Unipile 404: chloé-dupré", status_code=404),
        UnipileError("Unipile 404: chloe-dupre", status_code=404))
    with pytest.raises(UnipileError) as e:
        _person("chloé-dupré")
    assert "chloé-dupré" in str(e.value)
    assert client.get_profile.call_count == 2


@pytest.mark.parametrize("error", [
    UnipileError("Unipile 500: x", status_code=500),
    UnipileError("Unipile identity_mismatch: x"),
])
def test_une_autre_erreur_n_est_pas_relue(client, error):
    client.get_profile.side_effect = _answers(error)
    with pytest.raises(UnipileError):
        _person("chloé-dupré")
    assert client.get_profile.call_count == 1


def test_un_429_n_est_pas_relu_et_arme_la_discipline(client):
    from oto_mcp.mcp_errors import McpError

    client.get_profile.side_effect = _answers(UnipileRateLimited("Unipile 429: x", retry_after=5))
    with pytest.raises(McpError):
        _person("chloé-dupré")
    assert client.get_profile.call_count == 1


@pytest.mark.parametrize("identifier", ["jean-dupont", "ACoAAB1234xyz_-"])
def test_un_slug_ascii_ou_un_id_est_lu_une_fois_sans_annotation(client, identifier):
    client.get_profile.side_effect = _answers(
        UnipileError("Unipile 404: x", status_code=404))
    with pytest.raises(UnipileError):
        _person(identifier)
    assert client.get_profile.call_count == 1
    client.get_profile.side_effect = _answers({"public_identifier": "x"})
    assert "identifier_form" not in _person(identifier)


def test_une_societe_accentuee_a_le_meme_repli(client):
    client.get_company.side_effect = _answers(
        UnipileError("Unipile 404: x", status_code=404), {"name": "Société"})
    out = _tool("linkedin_unipile_profile")(op="company", identifier="société-x")
    assert [c.args[0] for c in client.get_company.call_args_list] == ["société-x", "societe-x"]
    assert out["identifier_form"] == "without_accents"
