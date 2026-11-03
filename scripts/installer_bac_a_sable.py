"""Pose le bac à sable des fonctions (ADR 0073) dans un répertoire, versions ÉPINGLÉES.

    python scripts/installer_bac_a_sable.py <répertoire>

Trois pièces, chacune vérifiée par empreinte avant d'être posée :
- `deno` — le binaire officiel, somme SHA-256 publiée par Deno ;
- `deno_dir/` — le cache npm où vit Pyodide, rempli par un PREMIER lancement du bac à
  sable, `--frozen` contre `oto_mcp/functions/deno.lock` (intégrité SHA-512 de chaque
  paquet). Pas par `deno cache` : il ne rapatrie pas les métadonnées des dépendances
  optionnelles de `ws`, que Deno résout pourtant au lancement — un cache rempli ainsi
  échoue ensuite en `--cached-only` (mesuré le 18/09/2026) ;
- `wheels/` — les roues Python pures de la liste blanche, SHA-256 publiées par PyPI.

Le réseau n'est utilisé qu'ICI, à l'installation. Ensuite l'exécuteur lance Deno en
`--cached-only` : une exécution ne télécharge rien. Le répertoire peut ensuite passer
en lecture seule pour l'utilisateur du service.

Rejouable : une pièce déjà présente et conforme n'est pas retéléchargée. Une pièce
présente et NON conforme fait échouer l'installation plutôt que d'être gardée.

Termine par une exécution de contrôle : une fonction jouet qui produit un `.xlsx`. Un
bac à sable posé mais qui ne sait pas exécuter ne sort pas d'ici en succès.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

DENO_VERSION = "2.9.7"
DENO_URL = (f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/"
            "deno-x86_64-unknown-linux-gnu.zip")
DENO_SHA256 = "c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490"

ROUES = {
    "openpyxl-3.1.5-py2.py3-none-any.whl": (
        "https://files.pythonhosted.org/packages/c0/da/"
        "977ded879c29cbd04de313843e76868e6e13408a94ed6b987245dc7c8506/"
        "openpyxl-3.1.5-py2.py3-none-any.whl",
        "5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2"),
    "et_xmlfile-2.0.0-py3-none-any.whl": (
        "https://files.pythonhosted.org/packages/c1/8b/"
        "5fe2cc11fee489817272089c4203e679c63b570a5aaeb18d852ae3cbba6a/"
        "et_xmlfile-2.0.0-py3-none-any.whl",
        "7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa"),
}

_DEPOT = Path(__file__).resolve().parents[1]
_FONCTIONS = _DEPOT / "oto_mcp" / "functions"
_DELAI_S = 120


def _telecharger(url: str, sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_DELAI_S) as reponse:
        contenu = reponse.read()
    obtenu = hashlib.sha256(contenu).hexdigest()
    if obtenu != sha256:
        raise SystemExit(f"empreinte fausse pour {url} : {obtenu}, attendue {sha256}")
    return contenu


def _conforme(chemin: Path, sha256: str) -> bool:
    if not chemin.exists():
        return False
    if hashlib.sha256(chemin.read_bytes()).hexdigest() != sha256:
        raise SystemExit(f"{chemin} existe mais n'est pas la version épinglée : "
                         "retire-le, l'installation le reposera.")
    return True


def _poser_deno(cible: Path) -> Path:
    binaire = cible / "deno"
    archive_sha = cible / ".deno.zip.sha256"
    if binaire.exists() and archive_sha.exists() and archive_sha.read_text() == DENO_SHA256:
        return binaire
    archive = _telecharger(DENO_URL, DENO_SHA256)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        binaire.write_bytes(z.read("deno"))
    binaire.chmod(0o755)
    archive_sha.write_text(DENO_SHA256)
    return binaire


def _poser_pyodide(cible: Path, deno: Path) -> None:
    """Un lancement à vide du bac à sable, réseau permis au CHARGEUR de Deno seulement :
    le script, lui, n'a toujours que la lecture. La réponse doit porter le nonce."""
    env = {"DENO_DIR": str(cible / "deno_dir"), "NO_COLOR": "1",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", str(cible))}
    pyodide = cible / "deno_dir/npm/registry.npmjs.org/pyodide"
    tache = ('{"nonce": "INSTALLATION", "pyodide": "%s", "wheels": [], '
             '"harness": "import json; json.dumps({\\"ok\\": True})", '
             '"job": {"sources": {}, "entrypoint": "a:b", "mode": "test", "input": {}}}'
             % (pyodide / _pyodide_version()))
    fini = subprocess.run(
        [str(deno), "run", "--no-prompt", "--no-config",
         f"--lock={_FONCTIONS / 'deno.lock'}", "--frozen", f"--allow-read={pyodide}",
         str(_FONCTIONS / "sandbox.ts")],
        input=tache.encode(), env=env, capture_output=True, timeout=_DELAI_S)
    if b"INSTALLATION" not in fini.stdout:
        raise SystemExit("premier lancement du bac à sable en échec : "
                         f"{fini.stderr.decode(errors='replace')[-2000:]}")


def _pyodide_version() -> str:
    sys.path.insert(0, str(_DEPOT))
    from oto_mcp.functions import executor
    return executor.PYODIDE


def _poser_roues(cible: Path) -> None:
    dossier = cible / "wheels"
    dossier.mkdir(exist_ok=True)
    for nom, (url, sha256) in ROUES.items():
        if not _conforme(dossier / nom, sha256):
            (dossier / nom).write_bytes(_telecharger(url, sha256))


def _controler(cible: Path) -> None:
    sys.path.insert(0, str(_DEPOT))
    os.environ["OTO_FUNCTIONS_SANDBOX_DIR"] = str(cible)
    from oto_mcp.functions import executor
    sortie = executor.executer(
        sources={"jouet.py": "import io, openpyxl\n\ndef executer(entree):\n"
                             "    classeur = openpyxl.Workbook()\n"
                             "    classeur.active['A1'] = entree['a'] * 2\n"
                             "    tampon = io.BytesIO(); classeur.save(tampon)\n"
                             "    return {'result': entree['a'] * 2, 'files': "
                             "[{'name': 'x.xlsx', 'content': tampon.getvalue()}]}\n"},
        entrypoint="jouet:executer", requirements=["openpyxl"], mode="run", sub=None,
        entree={"a": 21})
    if not (sortie.get("ok") and sortie.get("result") == 42 and sortie.get("files")):
        raise SystemExit(f"contrôle en échec : {sortie}")
    print(f"bac à sable prêt dans {cible} (contrôle : {sortie['duree_ms']} ms)")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    cible = Path(sys.argv[1]).resolve()
    cible.mkdir(parents=True, exist_ok=True)
    deno = _poser_deno(cible)
    _poser_pyodide(cible, deno)
    _poser_roues(cible)
    _controler(cible)


if __name__ == "__main__":
    main()
