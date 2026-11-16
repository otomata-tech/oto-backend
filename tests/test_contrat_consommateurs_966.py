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
    secret = re.compile(r"[A-Z][A-Z0-9_]*")
    for c in liste:
        if "depot_ref" in c:
            # Référence indirecte : dépôt et chemin portés par des SECRETS, nommés ici ;
            # dépôt privé, donc clé de lecture requise. Jamais avec `depot` en clair.
            assert set(c) == {"nom", "depot_ref", "chemin_ref", "secret"}, c
            assert all(secret.fullmatch(c[k]) for k in ("depot_ref", "chemin_ref", "secret")), c
            continue
        # `chemin_admin` (#1196) : facultatif — le contrat qu'il épingle sur la console
        # `/api/admin/*`, absente du document public.
        assert {"nom", "depot", "chemin", "secret"} <= set(c) <= \
            {"nom", "depot", "chemin", "secret", "chemin_admin"}, c
        if "chemin_admin" in c and c["chemin_admin"] is not None:
            assert not c["chemin_admin"].startswith("/"), c
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", c["depot"]), c
        assert not c["chemin"].startswith("/"), c
        # `null` = dépôt public, lu sans clé ; sinon le NOM d'un secret du dépôt.
        assert c["secret"] is None or secret.fullmatch(c["secret"]), c


def test_un_partenaire_ne_se_declare_jamais_en_clair():
    """Garde de neutralité : ce dépôt est public. Un consommateur partenaire (clé de
    lecture `PARTENAIRE_*`) se déclare par référence, sans dépôt ni chemin en clair."""
    for c in _consommateurs():
        if (c.get("secret") or "").startswith("PARTENAIRE_"):
            assert not {"depot", "chemin", "chemin_admin"} & set(c), c["nom"]
            assert c["nom"].startswith("partenaire-"), c["nom"]


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


_LECTEUR = _RACINE / "scripts" / "lire-contrat-consommateur.sh"
_DEPOT_CACHE, _CHEMIN_CACHE = "org-cachee/depot-cache", "dossier-cache/contrat-cache.json"


def _faux_distant(tmp_path) -> pathlib.Path:
    """Un dépôt « distant » local, servi à la place de github.com par `insteadOf` (passé
    par l'environnement) : le script clone sa vraie URL SSH, git la réécrit en `file://`.
    Identité git passée en ligne : la CI n'en a pas."""
    racine = tmp_path / "distants"
    depot = racine / f"{_DEPOT_CACHE}.git"
    travail = tmp_path / "source"
    (travail / "dossier-cache").mkdir(parents=True)
    (travail / _CHEMIN_CACHE).write_text(json.dumps(_contrat({"/api/a": _op("a")})))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@exemple.invalid",
           "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", str(travail)], check=True)
    subprocess.run([*git, "-C", str(travail), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(travail), "commit", "-qm", "c"], check=True)
    subprocess.run([*git, "clone", "-q", "--bare", str(travail), str(depot)], check=True)
    return racine


def _lire_par_reference(tmp_path, racine, depot=_DEPOT_CACHE, chemin=_CHEMIN_CACHE):
    import os
    sortie = tmp_path / "sortie"
    sortie.write_text("")
    env = {**os.environ, "CLE_LECTURE": "cle-factice", "GITHUB_OUTPUT": str(sortie),
           "DEPOT_REF": "PRIVE_1_DEPOT", "CHEMIN_REF": "PRIVE_1_CHEMIN",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{racine}/.insteadOf",
           "GIT_CONFIG_VALUE_0": "git@github.com:"}
    r = subprocess.run([str(_LECTEUR), "prive-1", depot, chemin, "PRIVE_1_CLE"],
                       env=env, capture_output=True, text=True, timeout=60)
    sorties = dict(l.split("=", 1) for l in sortie.read_text().splitlines())
    return r, sorties


def _sans_nom_cache(r, sorties):
    """Hors des lignes `::add-mask::` (qui ne s'impriment pas), rien ne nomme le dépôt,
    son nom seul ni le chemin — ni le journal, ni les sorties réimprimées ensuite."""
    journal = [l for l in (r.stdout + r.stderr).splitlines() if not l.startswith("::add-mask::")]
    for cache in (_DEPOT_CACHE, "depot-cache", "org-cachee", _CHEMIN_CACHE, "dossier-cache"):
        assert not any(cache in l for l in journal), (cache, journal)
        assert not any(cache in v for v in sorties.values()), (cache, sorties)


def test_une_reference_indirecte_lit_le_contrat_sans_jamais_nommer_le_depot(tmp_path):
    r, sorties = _lire_par_reference(tmp_path, _faux_distant(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorties["lu"] == "oui"
    assert json.loads(pathlib.Path(sorties["contrat"]).read_text())["paths"] == {"/api/a": _op("a")}
    # Masqués AVANT tout autre geste : les premières lignes du journal.
    assert r.stdout.splitlines()[:3] == [f"::add-mask::{_DEPOT_CACHE}", "::add-mask::depot-cache",
                                         f"::add-mask::{_CHEMIN_CACHE}"]
    _sans_nom_cache(r, sorties)


def test_une_reference_indirecte_introuvable_avertit_sans_nommer(tmp_path):
    racine = _faux_distant(tmp_path)
    r, sorties = _lire_par_reference(tmp_path, racine, chemin="dossier-cache/ailleurs.json")
    assert r.returncode == 0 and sorties["lu"] == "non"
    assert "::warning title=Contrat de prive-1 introuvable::" in r.stdout
    _sans_nom_cache(r, {k: v for k, v in sorties.items() if k != "travail"})
    # Un dépôt illisible : le détail de git, qui cite l'URL, est expurgé.
    r, sorties = _lire_par_reference(tmp_path, racine, depot="org-cachee/depot-absent")
    assert r.returncode == 0 and sorties["lu"] == "non"
    assert "::warning title=Contrat de prive-1 inaccessible::le dépôt de prive-1" in r.stdout
    assert "<dépôt>" in r.stdout
    assert not any("depot-absent" in l for l in r.stdout.splitlines() if not l.startswith("::add-mask::"))


def test_une_reference_indirecte_mal_formee_est_refusee_sans_la_citer(tmp_path):
    r, _ = _lire_par_reference(tmp_path, tmp_path, depot="pas un depot")
    assert r.returncode == 1
    assert "référence mal formée" in r.stdout and "PRIVE_1_DEPOT" in r.stdout
    assert not any("pas un depot" in l for l in r.stdout.splitlines() if not l.startswith("::add-mask::"))


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
