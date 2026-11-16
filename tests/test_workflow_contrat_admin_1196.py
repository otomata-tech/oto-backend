"""Le contrat ADMIN d'un consommateur est jugé avant déploiement, et seulement là (#1196).

`chemin_admin` est FACULTATIF dans `.github/contrat-consommateurs.json` : le contrat qu'un
consommateur épingle sur la console `/api/admin/*`, absente du document public. Ce banc
tient :
1. la forme : le job `consommateurs` accepte `chemin_admin` absent, `null` ou relatif, et
   refuse une forme invalide — le filtre `jq` est EXÉCUTÉ, pas relu ; il tient aussi la
   référence indirecte (`depot_ref`/`chemin_ref`, jamais avec `depot`) ;
2. la garde : dans `contrat-front-code`, les quatre étapes admin ne tournent que si
   `chemin_admin` est déclaré — et tournent MÊME si le contrat public a rougi (`!cancelled()`), lisent CE chemin, dérivent le document admin
   (`build(admin=True)`) et le confrontent avec l'outil commun ;
3. le périmètre : le constat d'après préproduction (`contrat-front`) ne juge PAS de contrat
   admin — décision du 09/10/2026, pas de clé admin en CI.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

_RACINE = pathlib.Path(__file__).resolve().parents[1]
_WF = _RACINE / ".github" / "workflows" / "deploy-canari.yml"


def _jobs() -> dict:
    return yaml.safe_load(_WF.read_text(encoding="utf-8"))["jobs"]


def _filtre_jq() -> str:
    """Le programme `jq -e '…'` de l'étape `liste` du job `consommateurs`, tel qu'écrit."""
    run = next(s for s in _jobs()["consommateurs"]["steps"] if s.get("id") == "liste")["run"]
    m = re.search(r"jq -e '(.*?)' \\\n", run, re.S)
    assert m, run
    return m.group(1)


def _valide(liste: list[dict]) -> bool:
    r = subprocess.run(["jq", "-e", _filtre_jq()], input=json.dumps(liste),
                       capture_output=True, text=True)
    return r.returncode == 0


_UN = {"nom": "front", "depot": "org/front", "chemin": "api/openapi.json", "secret": None}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq absent")
@pytest.mark.parametrize("chemin_admin, accepte", [
    ("absent", True),
    (None, True),
    ("api/openapi-admin.json", True),
    ("openapi-admin.json", True),
    ("/api/openapi-admin.json", False),      # absolu
    ("api//openapi-admin.json", False),       # segment vide
    ("", False),                              # vide : déclarer, c'est nommer
    (3, False),                               # pas une chaîne
])
def test_la_liste_valide_chemin_admin_facultatif(chemin_admin, accepte):
    entree = dict(_UN)
    if chemin_admin != "absent":
        entree["chemin_admin"] = chemin_admin
    assert _valide([entree]) is accepte


_RETIRE = object()
_INDIRECT = {"nom": "prive-1", "depot_ref": "PRIVE_1_DEPOT", "chemin_ref": "PRIVE_1_CHEMIN",
             "secret": "PRIVE_1_CLE"}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq absent")
@pytest.mark.parametrize("retouche, accepte", [
    ({}, True),
    ({"depot": "org/prive"}, False),                  # depot ET depot_ref : jamais les deux
    ({"chemin": "api/openapi.json"}, False),          # chemin en clair à côté d'une référence
    ({"chemin_admin": "api/admin.json"}, False),      # pas de second contrat en clair
    ({"depot_ref": _RETIRE}, False),
    ({"chemin_ref": _RETIRE}, False),
    ({"secret": None}, False),                        # une référence = un dépôt privé
    ({"depot_ref": "prive_1_depot"}, False),          # un nom de secret, en MAJUSCULES
    ({"chemin_ref": "PRIVE-1"}, False),
    ({"inconnu": 1}, False),
])
def test_la_liste_valide_une_reference_indirecte(retouche, accepte):
    entree = {k: v for k, v in {**_INDIRECT, **retouche}.items() if v is not _RETIRE}
    assert _valide([_UN, entree]) is accepte


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq absent")
def test_une_entree_en_clair_ne_porte_pas_de_reference():
    assert _valide([{**_UN, "depot_ref": "X_DEPOT"}]) is False
    assert _valide([{k: v for k, v in _UN.items() if k != "depot"}]) is False


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq absent")
def test_la_liste_declaree_passe_le_filtre():
    liste = json.loads((_RACINE / ".github" / "contrat-consommateurs.json").read_text())
    assert _valide(liste)


def _etapes_admin() -> list[dict]:
    etapes = _jobs()["contrat-front-code"]["steps"]
    return [s for s in etapes if "ADMIN" in s.get("name", "") or "(admin)" in s.get("name", "")]


def test_les_etapes_admin_ne_tournent_que_si_chemin_admin_est_declare():
    etapes = _etapes_admin()
    assert len(etapes) == 4, [s.get("name") for s in etapes]
    deriver, lire, confronter, effacer = etapes
    # `!cancelled()` : un rouge du contrat public ne doit pas sauter le verdict admin.
    assert deriver["if"] == "${{ !cancelled() && matrix.consommateur.chemin_admin }}"
    assert "admin=True" in deriver["run"]
    assert lire["id"] == "contrat_admin"
    assert lire["if"] == "${{ !cancelled() && matrix.consommateur.chemin_admin }}"
    assert lire["env"]["CHEMIN"] == "${{ matrix.consommateur.chemin_admin }}"
    assert lire["env"]["SECRET"] == "${{ matrix.consommateur.secret || '' }}"
    assert lire["run"].startswith("scripts/lire-contrat-consommateur.sh ")
    assert confronter["if"] == "${{ !cancelled() && steps.contrat_admin.outputs.lu == 'oui' }}"
    assert "scripts/contrat-front.py" in confronter["run"]
    assert "openapi-admin-de-la-pr.json" in confronter["run"]
    assert "la console chez $NOM" in confronter["run"]
    # Le verdict se nomme comme le consommateur se nomme lui-même (`api:admin-check`).
    assert '--consommateur "$NOM (admin)"' in confronter["run"]
    assert effacer["if"] == "always()"
    assert effacer["env"]["TRAVAIL"] == "${{ steps.contrat_admin.outputs.travail }}"


def test_le_contrat_public_est_toujours_lu_sur_chemin_pas_chemin_admin():
    lire = next(s for s in _jobs()["contrat-front-code"]["steps"] if s.get("id") == "contrat")
    assert lire["env"]["CHEMIN"] == ("${{ matrix.consommateur.chemin_ref && "
                                     "secrets[matrix.consommateur.chemin_ref] || "
                                     "matrix.consommateur.chemin }}")
    assert "if" not in lire


def test_le_constat_d_apres_preproduction_ne_juge_pas_de_contrat_admin():
    texte = yaml.safe_dump(_jobs()["contrat-front"])
    assert "chemin_admin" not in texte and "admin=True" not in texte
