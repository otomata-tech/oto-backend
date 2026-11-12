"""Le contrat de l'API garde PLUSIEURS consommateurs, et nomme celui qui casse (#966).

Jusqu'au 25/09/2026 le mécanisme était écrit au singulier : un dépôt, un chemin, un
secret en dur. Un second consommateur aurait été invisible — jamais lu, jamais jugé,
jamais rouge. Ce banc tient la liste déclarée et le verdict nommé ; la matrice elle-même
(une jambe par consommateur, `fail-fast: false`) se lit dans `deploy-canari.yml`.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import yaml

_RACINE = pathlib.Path(__file__).resolve().parents[1]
_LISTE = _RACINE / ".github" / "contrat-consommateurs.json"
_SCRIPT = _RACINE / "scripts" / "contrat-front.py"


def _consommateurs() -> list[dict]:
    return json.loads(_LISTE.read_text())


def test_la_liste_declaree_est_bien_formee():
    liste = _consommateurs()
    assert len(liste) >= 2, "un seul consommateur : le mécanisme n'est éprouvé par rien"
    noms = [c["nom"] for c in liste]
    assert len(set(noms)) == len(noms), "deux jambes au même nom seraient indiscernables"
    for c in liste:
        # `chemin_admin` (#1196) : facultatif — le contrat qu'il épingle sur la console
        # `/api/admin/*`, absente du document public.
        assert {"nom", "depot", "chemin", "secret"} <= set(c) <= \
            {"nom", "depot", "chemin", "secret", "chemin_admin"}, c
        if "chemin_admin" in c and c["chemin_admin"] is not None:
            assert not c["chemin_admin"].startswith("/"), c
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", c["depot"]), c
        assert not c["chemin"].startswith("/"), c
        # `null` = dépôt public, lu sans clé ; sinon le NOM d'un secret du dépôt.
        assert c["secret"] is None or re.fullmatch(r"[A-Z][A-Z0-9_]*", c["secret"]), c


def test_les_deux_jobs_de_contrat_tournent_sur_toute_la_liste():
    wf = yaml.safe_load((_RACINE / ".github" / "workflows" / "deploy-canari.yml").read_text())
    jobs = wf["jobs"]
    # `liste_code` : la liste entière, sauf sur une PR de fork où les consommateurs à
    # secret en sont retirés et annoncés (test_workflow_secret_absent_rougit.py).
    for nom, sortie in (("contrat-front", "liste"), ("contrat-front-code", "liste_code")):
        strat = jobs[nom]["strategy"]
        assert strat["fail-fast"] is False, f"{nom} : un rouge masquerait les suivants"
        assert f"fromJSON(needs.consommateurs.outputs.{sortie})" in strat["matrix"]["consommateur"]
        assert "consommateurs" in jobs[nom]["needs"]
    assert "contrat-front-code" in jobs["deploy-preprod"]["needs"], \
        "la garde d'avant déploiement doit bloquer la préproduction"


def _contrat(chemins: dict) -> dict:
    return {"openapi": "3.1.0", "paths": chemins}


def _op(op_id: str) -> dict:
    return {"get": {"operationId": op_id, "responses": {"200": {"description": "ok"}}}}


def _juger(tmp_path, nom, epingle, servi):
    e, s = tmp_path / f"{nom}.json", tmp_path / "servi.json"
    e.write_text(json.dumps(epingle))
    s.write_text(json.dumps(servi))
    return subprocess.run([sys.executable, str(_SCRIPT), "--consommateur", nom, str(e), str(s)],
                          capture_output=True, text=True)


def test_un_appel_casse_chez_le_second_rougit_en_le_nommant_et_le_premier_reste_juge(tmp_path):
    """Le critère de fin de #966 : deux consommateurs, un appel du SECOND cassé
    volontairement. Son verdict est rouge et le nomme ; celui du premier est rendu
    quand même, vert."""
    servi = _contrat({"/api/a": _op("a")})
    premier = _juger(tmp_path, "oto-commerce", _contrat({"/api/a": _op("a")}), servi)
    second = _juger(tmp_path, "oto-dashboard",
                    _contrat({"/api/a": _op("a"), "/api/b": _op("b")}), servi)
    assert premier.returncode == 0 and "« oto-commerce »" in premier.stdout
    assert second.returncode == 1
    assert "épinglée(s) par « oto-dashboard »" in second.stdout
    assert "CASSENT les appels de « oto-dashboard »" in second.stdout
    assert "GET /api/b" in second.stdout


def test_sans_nom_le_script_garde_son_ancienne_forme(tmp_path):
    e = tmp_path / "e.json"
    e.write_text(json.dumps(_contrat({"/api/a": _op("a")})))
    r = subprocess.run([sys.executable, str(_SCRIPT), str(e), str(e)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "par le front" in r.stdout


def test_le_document_derive_sert_les_memes_routes_que_le_serveur(monkeypatch):
    """La garde d'avant déploiement dérive le document de `server.routes_rest` — la
    source du montage. Une route écrite à la main (hors capacité) et une route de
    sous-domaine de projet y figurent : sans elles, un consommateur qui les appelle
    rougirait pour une route pourtant servie (mesuré le 25/09 : 43 « disparues »)."""
    monkeypatch.setenv("OTO_BILLING_ENABLED", "1")
    from oto_mcp import openapi, server
    chemins = openapi.build(server.routes_rest(object()))["paths"]
    for attendu in ("/api/public/mcp-projects", "/api/billing/webhook", "/api/me/billing"):
        assert attendu in chemins, attendu
