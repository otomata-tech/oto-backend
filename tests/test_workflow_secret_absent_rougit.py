"""Un contrôle qui ne peut pas s'exécuter faute de secret doit être ROUGE (#823).

`contrat-front-code` sortait VERT sur toute PR de fork : GitHub ne lui transmet pas
`OTO_FRONTEND_CONTRACT_KEY`, le job avertissait dans son journal et concluait `exit 0`.
Seule la conclusion est lue — un vert « pas jugé » fabrique une preuve positive.

Cliquet : dans tout workflow, une variable d'environnement liée à un `secrets.*`, dont
l'absence est testée par `if [ -z "${VAR…` , doit faire échouer le job dans cette
branche (`exit 1`), jamais le faire réussir (`exit 0`). Même règle que le job `cla`
(`cla.yml`), qui échoue sans son jeton.
"""
from __future__ import annotations

import pathlib
import re

_WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
_LIE_A_UN_SECRET = re.compile(r"^\s*([A-Z_][A-Z0-9_]*):\s*\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}\s*$")


def _branches_secret_absent(texte: str):
    """(variable, bloc) pour chaque `if [ -z "${VAR…` sur une variable liée à un secret ;
    le bloc court jusqu'au `fi` de même indentation."""
    lignes = texte.splitlines()
    variables = {m.group(1) for l in lignes if (m := _LIE_A_UN_SECRET.match(l))}
    for i, ligne in enumerate(lignes):
        m = re.match(r'^(\s*)if \[ -z "\$\{([A-Z_][A-Z0-9_]*)', ligne)
        if not m or m.group(2) not in variables:
            continue
        retrait = m.group(1)
        fin = next(j for j in range(i + 1, len(lignes)) if lignes[j] == f"{retrait}fi")
        yield m.group(2), "\n".join(lignes[i:fin + 1])


def test_le_contrat_d_un_consommateur_sans_son_secret_rougit(tmp_path):
    """Depuis #966, la lecture du contrat d'un consommateur vit dans
    `scripts/lire-contrat-consommateur.sh`, appelé par les deux jobs de contrat : le
    cliquet ci-dessous ne la voit plus dans le YAML. On l'ÉPROUVE donc : un secret
    déclaré mais vide doit sortir en échec NOMMÉ, jamais en vert. (Une PR de fork ne
    l'appelle pas pour un consommateur à secret : voir le test suivant.)"""
    import os
    import subprocess
    script = _WORKFLOWS.parents[1] / "scripts" / "lire-contrat-consommateur.sh"
    sortie = tmp_path / "out"
    r = subprocess.run(
        [str(script), "oto-commerce", "otomata-tech/oto-commerce",
         "api/openapi-service.json", "OTO_COMMERCE_CONTRACT_KEY"],
        env={**os.environ, "CLE_LECTURE": "", "GITHUB_OUTPUT": str(sortie)},
        capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Contrat de oto-commerce NON JUGÉ" in r.stdout
    assert not sortie.exists() or "lu=oui" not in sortie.read_text()


def _etape_liste() -> str:
    import yaml
    wf = yaml.safe_load((_WORKFLOWS / "deploy-canari.yml").read_text())
    return next(s for s in wf["jobs"]["consommateurs"]["steps"] if s.get("id") == "liste")


def _liste_code(tmp_path, est_fork: str, liste: list) -> tuple[str, str]:
    """Joue l'étape `liste` du job `consommateurs` sur une liste donnée : (liste_code, stdout)."""
    import json
    import os
    import subprocess
    (tmp_path / ".github").mkdir(exist_ok=True)
    (tmp_path / ".github" / "contrat-consommateurs.json").write_text(json.dumps(liste))
    sortie = tmp_path / f"out-{est_fork}"
    r = subprocess.run(["bash", "-c", _etape_liste()["run"]], cwd=tmp_path,
                       env={**os.environ, "EST_FORK": est_fork, "GITHUB_OUTPUT": str(sortie)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    sorties = dict(l.split("=", 1) for l in sortie.read_text().splitlines())
    return sorties["liste_code"], r.stdout


def test_une_pr_de_fork_ne_juge_que_ce_qu_elle_peut_lire_et_le_dit(tmp_path):
    """Sur une PR de fork, un consommateur à secret quitte la matrice de la garde
    d'avant déploiement — ni vert (rien jugé) ni rouge (rien à voir avec le code) — et ce
    retrait est ANNONCÉ. Un consommateur public reste jugé ; hors fork, rien ne change."""
    import json
    prive = {"nom": "prive", "depot": "o/a", "chemin": "c.json", "secret": "CLE_A"}
    public = {"nom": "public", "depot": "o/b", "chemin": "c.json", "secret": None}

    code, sortie = _liste_code(tmp_path, "true", [prive, public])
    assert json.loads(code) == [public]
    assert "::warning title=Contrat NON JUGÉ sur cette PR de fork::" in sortie
    assert "prive" in sortie

    code, sortie = _liste_code(tmp_path, "true", [prive])
    assert code == "[]"  # le job est alors sauté (son `if`), pas vert

    code, sortie = _liste_code(tmp_path, "false", [prive, public])
    assert json.loads(code) == [prive, public]
    assert "::warning" not in sortie


def test_la_garde_d_avant_deploiement_lit_la_liste_filtree_et_saute_si_vide():
    import yaml
    wf = yaml.safe_load((_WORKFLOWS / "deploy-canari.yml").read_text())
    job = wf["jobs"]["contrat-front-code"]
    assert job["strategy"]["matrix"]["consommateur"] == \
        "${{ fromJSON(needs.consommateurs.outputs.liste_code) }}"
    assert job["if"] == "needs.consommateurs.outputs.liste_code != '[]'"
    # Le fork se reconnaît au dépôt de la tête de PR, pas à l'absence d'un secret : une
    # PR interne sans secret reste un échec nommé.
    assert "head.repo.full_name != github.repository" in _etape_liste()["env"]["EST_FORK"]
    # Le constat d'après préproduction garde la liste complète.
    assert wf["jobs"]["contrat-front"]["strategy"]["matrix"]["consommateur"] == \
        "${{ fromJSON(needs.consommateurs.outputs.liste) }}"


def test_les_jobs_de_contrat_passent_le_secret_de_chaque_consommateur():
    """Le secret d'un consommateur est lu par son NOM déclaré : un nom mal recopié dans
    le workflow retomberait sur une clé vide — que le script refuse (ci-dessus)."""
    texte = (_WORKFLOWS / "deploy-canari.yml").read_text()
    # Trois lectures : le contrat du constat (`contrat-front`), celui de la garde
    # (`contrat-front-code`) et, dans la même garde, le contrat ADMIN (#1196).
    assert texte.count(
        "CLE_LECTURE: ${{ matrix.consommateur.secret && "
        "secrets[matrix.consommateur.secret] || '' }}") == 3


def test_un_secret_absent_ne_sort_jamais_vert():
    fautifs = []
    for wf in sorted(_WORKFLOWS.glob("*.y*ml")):
        for variable, bloc in _branches_secret_absent(wf.read_text()):
            if re.search(r"\bexit 0\b", bloc) or not re.search(r"\bexit 1\b", bloc):
                fautifs.append(f"{wf.name} — {variable} absent :\n{bloc}")
    assert not fautifs, (
        "un job SORT VERT (ou sans échec explicite) quand son secret manque — il n'a "
        "rien jugé et le dit seulement dans son journal (#823) :\n\n" + "\n\n".join(fautifs))
