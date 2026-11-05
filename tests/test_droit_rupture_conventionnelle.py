"""`ccn_rupture_conventionnelle` passe le dossier tel quel au contrôle FOD, sans les
arguments laissés vides, et ne calcule rien lui-même : le verdict est celui de FOD."""
import asyncio

import oto_mcp.fod.rupture  # noqa: F401 — cible du monkeypatch (import à l'appel)


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import droit as D

    m = FastMCP("t")
    D.register(m)
    return asyncio.run(m.get_tool("ccn_rupture_conventionnelle")).fn


def test_le_dossier_part_tel_quel_sans_les_vides(monkeypatch):
    recu = {}

    def controler(dossier):
        recu.update(dossier)
        return {"verdict": "non_conforme", "controles": []}

    monkeypatch.setattr("oto_mcp.fod.rupture.controler", controler)
    mois = [{"mois": "2026-09", "brut": 2400.0}]
    r = _tool()(date_entree="2022-03-01", date_signature="2026-10-05",
                date_rupture_envisagee="2026-11-20", indemnite_convenue=125.0, mois=mois)
    assert r["verdict"] == "non_conforme"
    assert recu == {"date_entree": "2022-03-01", "date_signature": "2026-10-05",
                    "date_rupture_envisagee": "2026-11-20", "indemnite_convenue": 125.0,
                    "mois": mois, "jours_non_comptes_anciennete": 0, "alsace_moselle": False}


def test_la_route_appelee(monkeypatch):
    vu = {}
    monkeypatch.setattr("oto_mcp.fod.rupture._post",
                        lambda path, body: vu.setdefault("path", path) and {"verdict": "conforme"})
    from oto_mcp.fod import rupture
    rupture.controler({"x": 1})
    assert vu["path"] == "/api/rupture-conventionnelle/controle"
