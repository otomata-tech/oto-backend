"""`sheets_spreadsheet op=write source=…` : un CSV qu'oto va chercher lui-même —
typiquement servi par le connecteur `api` de l'appelant — écrit dans la feuille
sans que le modèle recopie une seule valeur.

Ce qui est figé ici :
- la source `api` passe par la MÊME fabrique que `api_get` (résolution de la
  clé, y compris une instance d'équipe épinglée sur le projet), en GET, sans suivre
  de redirection, et la clé ne sort jamais ;
- `values` et `source` s'excluent ;
- les cellules partent typées (nombre strict → nombre, `'` gardée, formule
  neutralisée sauf `allow_formulas`) ;
- `append` et le plafond de taille.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from oto_mcp.mcp_errors import McpError

CLE = "CLE-SECRETE-DU-GROUPE-123"
CSV = ('id,"Solde\nrestant",Date,Note\n'
       "'1234567890123456,-150.5,01/03/2024,=IMPORTXML(\"https://x/\")\n"
       "'2234567890123456,42,02/03/2024,ok\n").encode("utf-8")


class _Rep:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": "text/csv; charset=utf-8", **(headers or {})}
        self.text = body.decode("utf-8", "replace")
        self.content = body

    def iter_content(self, n):
        yield self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def close(self):
        pass


@pytest.fixture
def connecteur_http(monkeypatch):
    """Le VRAI client http d'oto-core, derrière la VRAIE fabrique de `api_get` :
    seules la résolution de la clé (une instance d'équipe épinglée sur le projet) et
    la sortie réseau sont simulées."""
    from oto_mcp import access, egress
    from oto_mcp.tools import api as H

    resolutions = []

    def resolve(provider, want="auto", **k):
        resolutions.append(provider)
        return SimpleNamespace(
            fields={"base_url": "https://api.exemple.test", "auth_mode": "bearer",
                    "token": CLE}, entity_type="group", entity_id="7", account="")

    monkeypatch.setattr(access, "resolve_credential", resolve)
    monkeypatch.setattr(H, "current_user_sub_from_token", lambda: "sub-1")
    monkeypatch.setattr(egress, "check_url", lambda *a, **k: None)
    appels = []
    reponses = [_Rep(body=CSV)]

    def get(self, url, **kw):
        appels.append((url, kw, dict(self.headers)))
        return reponses.pop(0)

    def interdit(self, *a, **k):
        raise AssertionError("une source http ne fait QUE des GET")

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(requests.Session, "post", interdit)
    monkeypatch.setattr(requests.Session, "request", interdit)
    return {"resolutions": resolutions, "appels": appels, "reponses": reponses}


@pytest.fixture
def feuille(monkeypatch):
    from oto_mcp.tools import sheets as S

    inst = MagicMock()
    inst.write.return_value = {"updated_range": "A1:D3", "updated_rows": 3}
    inst.append.return_value = {"updated_range": "A4:D5", "updated_rows": 2}
    monkeypatch.setattr(S, "_client_for_user", lambda account=None: inst)
    return inst


def _ecrire(**kwargs):
    from fastmcp import FastMCP
    from oto_mcp.tools import sheets as S

    m = FastMCP("t")
    S.register(m)
    fn = asyncio.run(m.get_tool("sheets_spreadsheet")).fn
    return asyncio.run(fn(spreadsheet_id="s1", op="write", **kwargs))


SOURCE = {"kind": "api", "path": "/ops/export.csv", "params": {"ids": "1,2"}}


def test_un_csv_du_connecteur_http_part_type_sans_passer_par_le_modele(connecteur_http, feuille):
    out = _ecrire(range="A1", source=SOURCE)
    (_, rng, values), _ = feuille.write.call_args
    assert rng == "A1"
    assert values == [
        ["id", "Solde\nrestant", "Date", "Note"],
        ["'1234567890123456", -150.5, "01/03/2024", "'=IMPORTXML(\"https://x/\")"],
        ["'2234567890123456", 42, "02/03/2024", "ok"],
    ]
    assert out == {"updated_range": "A1:D3", "updated_rows": 3}
    url, kw, headers = connecteur_http["appels"][0]
    assert url == "https://api.exemple.test/ops/export.csv" and kw["params"] == {"ids": "1,2"}
    assert kw["allow_redirects"] is False
    assert headers["Authorization"] == f"Bearer {CLE}"
    assert CLE not in repr(out)


def test_la_cle_est_celle_que_resout_http_get(connecteur_http, feuille, monkeypatch):
    """Même fabrique (`tools.api._client`) : la source ne résout pas la clé à sa
    façon. `resolve_credential("api")` est ce qui sert l'instance épinglée
    par `_project` — y compris une instance d'équipe — à `api_get` comme ici."""
    from oto_mcp.tools import api as H
    fabriques = []
    vraie = H._client
    monkeypatch.setattr(H, "_client", lambda: fabriques.append(1) or vraie())
    _ecrire(range="A1", source=SOURCE)
    assert fabriques == [1] and connecteur_http["resolutions"] == ["api"]


def test_une_formule_ne_passe_que_sur_option(connecteur_http, feuille):
    _ecrire(range="A1", source=SOURCE, allow_formulas=True)
    (_, _, values), _ = feuille.write.call_args
    assert values[1][3] == "=IMPORTXML(\"https://x/\")"


def test_append_depuis_une_source(connecteur_http, feuille):
    _ecrire(range="'Feuille'!A:D", source=SOURCE, append=True)
    (_, rng, values), _ = feuille.append.call_args
    assert rng == "'Feuille'!A:D" and values[2][1] == 42
    feuille.write.assert_not_called()


@pytest.mark.parametrize("kwargs,motif", [
    ({"values": [["a"]], "source": SOURCE}, "exactly one"),
    ({}, "exactly one"),
    ({"values": [["a"]], "allow_formulas": True}, "allow_formulas"),
])
def test_values_et_source_s_excluent(feuille, kwargs, motif):
    with pytest.raises(McpError, match=motif):
        _ecrire(range="A1", **kwargs)
    feuille.write.assert_not_called()


def test_le_plafond_de_taille(connecteur_http, feuille, monkeypatch):
    from oto_mcp.tools import sheets as S
    monkeypatch.setattr(S, "_CSV_MAX_BYTES", 10)
    with pytest.raises(McpError, match="limit"):
        _ecrire(range="A1", source=SOURCE)
    feuille.write.assert_not_called()


def test_une_redirection_est_refusee(connecteur_http, feuille):
    connecteur_http["reponses"][:] = [_Rep(302, headers={"Location": "https://ailleurs.test/"})]
    with pytest.raises(McpError, match="redirected"):
        _ecrire(range="A1", source=SOURCE)
    feuille.write.assert_not_called()


def test_une_erreur_amont_dit_le_statut_sans_la_cle(connecteur_http, feuille):
    connecteur_http["reponses"][:] = [_Rep(500, body=b"panne du pont")]
    with pytest.raises(McpError) as e:
        _ecrire(range="A1", source=SOURCE)
    msg = str(e.value)
    assert "HTTP 500" in msg and CLE not in msg
    feuille.write.assert_not_called()


def test_un_csv_pas_en_utf8_est_refuse(connecteur_http, feuille):
    connecteur_http["reponses"][:] = [_Rep(body="é".encode("latin-1"))]
    with pytest.raises(McpError, match="UTF-8"):
        _ecrire(range="A1", source=SOURCE)


def test_un_chemin_absolu_est_refuse(feuille):
    with pytest.raises(McpError, match="path"):
        _ecrire(range="A1", source={"kind": "api", "path": "https://ailleurs.test/x"})


def test_une_redirection_ne_rend_pas_la_cle_de_sa_requete(connecteur_http, feuille):
    """Un 3xx garde souvent la requête d'origine : la Location rendue ne doit porter
    ni la clé injectée en paramètre ni une userinfo."""
    connecteur_http["reponses"][:] = [_Rep(302, headers={
        "Location": "https://u:pw@ailleurs.test/x?api_key=SECRET"})]
    with pytest.raises(McpError) as e:
        _ecrire(range="A1", source=SOURCE)
    msg = str(e.value)
    assert "SECRET" not in msg and "pw" not in msg and "ailleurs.test/x" in msg
    feuille.write.assert_not_called()
