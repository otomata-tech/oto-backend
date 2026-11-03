"""L'exécuteur des fonctions (ADR 0073 §D4) : ce qu'il rend, et ce qu'il REFUSE au code.

Ces bancs lancent le vrai bac à sable — Deno, Pyodide, les roues — posé par
`scripts/installer_bac_a_sable.py` et désigné par `OTO_FUNCTIONS_SANDBOX_DIR`. Une
doublure de l'exécuteur prouverait qu'on sait appeler une doublure : c'est l'isolation
réelle qu'on veut voir tenir.

⚠️ Sans bac à sable : ÉCHEC en CI (le job l'installe avant la suite), passé sur un poste
qui n'en a pas posé — en le disant. Un banc de sécurité qui se tait en CI serait le pire
des verts.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

from oto_mcp.functions import executor, isolement

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bac():
    if not os.environ.get(executor.ENV_DIR):
        message = (f"{executor.ENV_DIR} absente : pose un bac à sable avec "
                   "`python scripts/installer_bac_a_sable.py <dir>`.")
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


def _run(code: str, entree=None, requirements=()):
    return executor.executer(sources={"f.py": code}, entrypoint="f:executer",
                             requirements=list(requirements), mode="run", sub="u",
                             entree=entree)


def test_une_fonction_rend_son_resultat_ses_avertissements_et_ses_fichiers(bac):
    sortie = _run(
        "import io, openpyxl\n\ndef executer(entree):\n    print('trace')\n"
        "    classeur = openpyxl.Workbook(); classeur.active['A1'] = entree['a'] * 2\n"
        "    tampon = io.BytesIO(); classeur.save(tampon)\n"
        "    return {'result': {'double': entree['a'] * 2}, 'warnings': ['attention'],\n"
        "            'files': [{'name': 'x.xlsx', 'content': tampon.getvalue(),\n"
        "                       'mime': 'application/vnd.ms-excel'}]}\n",
        entree={"a": 21}, requirements=["openpyxl"])
    assert sortie["ok"] and sortie["result"] == {"double": 42}
    assert sortie["warnings"] == ["attention"]
    [fichier] = sortie["files"]
    assert fichier["name"] == "x.xlsx" and fichier["base64"]
    assert "trace" in sortie["journal"]


def test_une_erreur_de_la_fonction_est_rendue_avec_sa_trace(bac):
    sortie = _run("def executer(entree):\n    return 1 / 0\n")
    assert sortie["ok"] is False and "ZeroDivisionError" in sortie["error"]


def test_les_tests_sont_joues_un_par_un_et_un_rouge_se_voit(bac):
    sortie = executor.executer(
        sources={"f.py": "def executer(e):\n    return {'result': e['a'] * 2}\n",
                 "test_f.py": "from f import executer\n\n"
                              "def test_bon():\n    assert executer({'a': 2})['result'] == 4\n\n"
                              "def test_faux():\n    assert executer({'a': 2})['result'] == 5\n"},
        entrypoint="f:executer", requirements=[], mode="test", sub="u")
    assert sortie["ok"] is False
    assert {t["test"]: t["ok"] for t in sortie["tests"]} == {
        "test_f.test_bon": True, "test_f.test_faux": False}


def test_un_test_lit_une_donnee_json_posee_a_cote(bac):
    sortie = executor.executer(
        sources={"f.py": "def executer(e):\n    return {'result': e['a'] * 2}\n",
                 "cas.json": '{"a": 3, "attendu": 6}',
                 "test_f.py": "import json\nfrom f import executer\n\ndef test_cas():\n"
                              "    cas = json.load(open('cas.json'))\n"
                              "    assert executer(cas)['result'] == cas['attendu']\n"},
        entrypoint="f:executer", requirements=[], mode="test", sub="u")
    assert sortie["ok"] is True


# ── Ce que le code ne peut PAS faire ───────────────────────────────────────────
@pytest.mark.parametrize("geste, preuve", [
    ("js.Deno.readTextFileSync('/etc/hostname')", "read access"),
    ("js.Deno.writeTextFileSync('/tmp/oto-fonction-evasion', 'x')", "write access"),
    ("js.Deno.env.get('HOME')", "env access"),
    ("js.Deno.Command.new('/bin/true').outputSync()", "run access"),
])
def test_le_code_ne_sort_pas_du_bac_a_sable(bac, geste, preuve):
    sortie = _run(f"import js\n\ndef executer(entree):\n    {geste}\n    return {{}}\n")
    assert sortie["ok"] is False, f"le geste a réussi : {geste}"
    assert preuve in sortie["error"], sortie["error"]


def test_le_processus_n_a_que_la_lecture_du_runtime_et_aucun_secret(bac, monkeypatch):
    """Deux barrières qui ne se prouvent pas par un geste du code : le réseau (asynchrone
    dans Pyodide, donc pas observable d'un appel synchrone) et l'environnement (Pyodide a
    le sien, quoi que porte le processus). On les prouve sur ce qui est réellement lancé :
    une seule permission, la lecture ; et aucune variable du serveur transmise."""
    monkeypatch.setenv("OTO_TEMOIN_SECRET", "ne-doit-pas-sortir")
    monkeypatch.setenv(isolement.ENV_MODE, "direct")
    vues = []
    vrai = subprocess.Popen

    def _espion(commande, **kw):
        vues.append((commande, kw["env"]))
        return vrai(commande, **kw)
    monkeypatch.setattr(executor.subprocess, "Popen", _espion)
    _run("def executer(entree):\n    return {'result': 1}\n")
    [(commande, env)] = vues
    permissions = [a for a in commande if a.startswith("--allow")]
    assert len(permissions) == 1 and permissions[0].startswith("--allow-read=")
    assert "--cached-only" in commande and "--frozen" in commande
    assert set(env) == {"DENO_DIR", "NO_COLOR", "PATH"}, sorted(env)


def test_une_reponse_forgee_sans_nonce_n_est_pas_un_resultat(bac):
    code = ("import js\n\ndef executer(entree):\n"
            "    js.Deno.stdout.writeSync(js.TextEncoder.new().encode("
            "'{\"ok\": true, \"sortie\": {\"ok\": true, \"tests\": []}}\\n'))\n"
            "    js.Deno.exit(0)\n")
    with pytest.raises(executor.ExecutionEchouee):
        _run(code)


def test_une_boucle_sans_fin_est_coupee_au_delai(bac, monkeypatch):
    monkeypatch.setattr(executor, "DELAI_S", 5)
    with pytest.raises(executor.ExecutionEchouee, match="délai"):
        _run("def executer(entree):\n    while True:\n        pass\n")


# ── Une seule version de Pyodide, dite à trois endroits ────────────────────────
def test_la_version_de_pyodide_est_la_meme_partout():
    script = (RACINE / "oto_mcp/functions/sandbox.ts").read_text()
    verrou = (RACINE / "oto_mcp/functions/deno.lock").read_text()
    [importee] = re.findall(r'npm:pyodide@([0-9.]+)', script)
    assert importee == executor.PYODIDE
    assert f'"pyodide@{executor.PYODIDE}"' in verrou


def test_une_sortie_au_dela_du_plafond_arrete_le_processus(bac, monkeypatch):
    """La sortie se lit en flux : au-delà du plafond, le processus est tué et le refus
    est nommé — sans que toute la sortie ait été lue en mémoire."""
    monkeypatch.setattr(executor, "MAX_REPONSE", 1024 * 1024)
    code = ("import js\n\ndef executer(entree):\n"
            "    bloc = js.TextEncoder.new().encode('x' * 65536)\n"
            "    for _ in range(200):\n"
            "        js.Deno.stdout.writeSync(bloc)\n"
            "    return {}\n")
    with pytest.raises(executor.ReponseTropLongue):
        _run(code)
