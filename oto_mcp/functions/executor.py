"""L'exécuteur : lance le bac à sable d'une fonction et lit sa réponse (ADR 0073 §D4).

Un processus Deno par exécution, hors du serveur MCP. Ce module est BLOQUANT (il attend
un sous-processus) : ses appelants le jouent hors de la boucle (`run_in_threadpool`).

**Où vit le bac à sable.** `OTO_FUNCTIONS_SANDBOX_DIR` désigne un répertoire posé par
`scripts/installer_bac_a_sable.py` : `deno` (le binaire), `deno_dir/` (le cache npm où
vit Pyodide) et `wheels/` (les roues de la liste blanche), tous épinglés et vérifiés par
empreinte. Absent, l'exécuteur LÈVE : une instance qui n'a pas de bac à sable ne sait
pas exécuter de fonction, et le dire vaut mieux que chercher un `deno` au hasard du PATH.

**Ce que le processus a le droit de faire** : lire le runtime de Pyodide et les roues.
Rien d'autre — ni réseau, ni écriture, ni environnement, ni sous-processus. Deno ne
résout ses paquets que dans le cache (`--cached-only`) : rien ne se télécharge au moment
d'exécuter. L'environnement du processus est vidé : il n'hérite d'aucun secret du
serveur. Le répertoire est en lecture seule pour le processus.

**Sous quelle identité, avec quelles bornes système** : `isolement.py` (le mode déclaré
par `OTO_FUNCTIONS_ISOLATION`). Deno refuse au code ce que ses permissions n'accordent
pas ; l'isolement borne le processus lui-même.

**La sortie se lit en flux** et s'arrête à `MAX_REPONSE` : au-delà, le processus est tué
et l'exécution lève `ReponseTropLongue`, sans avoir tout lu en mémoire.

**Combien à la fois** : `places.py`. Chaque exécution (aller simple ici, pont des
recettes-code dans `pont.py`) prend une place avant de lancer son processus ; plus de
place, `BacASableOccupe` (429 `sandbox_busy`), rien n'est lancé.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import contract, isolement, places

ENV_DIR = "OTO_FUNCTIONS_SANDBOX_DIR"
_ICI = Path(__file__).resolve().parent
_SCRIPT = _ICI / "sandbox.ts"
_HARNAIS = _ICI / "harness.pyodide"
_VERROU = _ICI / "deno.lock"
# La version de Pyodide importée par `sandbox.ts` et posée par l'installateur. Les trois
# doivent dire la même : c'est ce que vérifie `tests/test_functions_executeur_live.py`.
PYODIDE = "0.26.4"
_PYODIDE_REL = f"deno_dir/npm/registry.npmjs.org/pyodide/{PYODIDE}"

# Plafonds communs à toute exécution (0057 §D6 : la définition du lieu, pas un réglage
# par fonction). Chiffres PROVISOIRES, à remplacer par ceux mesurés sur le pilote.
DELAI_S = 60
MAX_REPONSE = 30 * 1024 * 1024
TAS_V8_MO = 512
#: Ce qu'on garde de la sortie d'erreur du processus (la fin), pour dire une panne.
QUEUE_ERREURS = 64 * 1024
_BLOC = 64 * 1024

# La roue qui accompagne chaque dépendance de la liste blanche. `et_xmlfile` suit
# `openpyxl` : c'est sa dépendance, jamais une demande d'auteur.
_ROUES = {"openpyxl": ("openpyxl", "et_xmlfile")}


class BacASableAbsent(RuntimeError):
    """Cette instance n'a pas de bac à sable : elle ne peut pas exécuter de fonction."""


#: Plus de place dans le bac à sable (`places.py`) : rien n'est lancé.
BacASableOccupe = places.BacASableOccupe


class ExecutionEchouee(RuntimeError):
    """Le bac à sable n'a pas rendu de réponse exploitable (délai, plantage, protocole)."""


class ReponseTropLongue(ExecutionEchouee):
    """Le processus a écrit plus que `MAX_REPONSE` : il a été tué avant la fin."""


def lancement(racine: Path, script: Path, duree_max_s: float) -> isolement.Lancement:
    """La ligne de Deno pour `script`, enveloppée par le mode d'isolement déclaré. Un
    mode qui ne peut pas s'appliquer lève `BacASableAbsent` : jamais de repli."""
    try:
        return isolement.preparer(commande(racine, script), environnement(racine), racine,
                                  [racine / _PYODIDE_REL, racine / "wheels", _ICI],
                                  duree_max_s)
    except isolement.IsolementIndisponible as e:
        raise BacASableAbsent(str(e)) from None


#: Où le DÉPLOIEMENT pose le bac à sable, dans l'arbre de la couleur qu'il installe
#: (`deploy/oto-mcp-bluegreen.sh`, `bg_install`) : il suit donc le tag, comme le code.
#: Non versionné (`.gitignore`), conservé d'une installation à l'autre de la couleur.
DEFAUT = _ICI.parents[1] / ".bac-a-sable"


def _repertoire() -> Path:
    brut = os.environ.get(ENV_DIR) or (str(DEFAUT) if DEFAUT.is_dir() else "")
    if not brut:
        raise BacASableAbsent(f"{ENV_DIR} n'est pas posée et {DEFAUT} n'existe pas : cette "
                              "instance n'exécute pas de fonction (cf. "
                              "scripts/installer_bac_a_sable.py).")
    racine = Path(brut)
    for requis in ("deno", f"{_PYODIDE_REL}/pyodide.mjs", "wheels"):
        if not (racine / requis).exists():
            raise BacASableAbsent(f"{racine / requis} manque : bac à sable incomplet.")
    return racine


def _roues(racine: Path, requirements: list[str]) -> list[str]:
    chemins = []
    for dependance in sorted(set(requirements)):
        if dependance not in contract.DEPENDANCES:
            raise ValueError(f"dépendance hors liste blanche : {dependance}")
        for projet in _ROUES[dependance]:
            trouvees = sorted((racine / "wheels").glob(f"{projet}-*.whl"))
            if len(trouvees) != 1:
                raise BacASableAbsent(f"roue `{projet}` : {len(trouvees)} trouvée(s) dans "
                                      f"{racine / 'wheels'}, il en faut exactement une.")
            chemins.append(str(trouvees[0]))
    return chemins


def commande(racine: Path, script: Path) -> list[str]:
    """La ligne de commande de Deno pour `script` : seule permission, la LECTURE de
    Pyodide et des roues. `--lock` + `--frozen` : la résolution des paquets est celle du
    verrou, jamais une résolution neuve — sans eux Deno réclame des métadonnées que le
    cache n'a pas. Partagée par l'exécution aller simple et par le pont (`pont.py`)."""
    lecture = ",".join([str(racine / _PYODIDE_REL), str(racine / "wheels")])
    return [str(racine / "deno"), "run", "--no-prompt", "--no-config", "--cached-only",
            "--no-remote", f"--lock={_VERROU}", "--frozen", f"--allow-read={lecture}",
            f"--v8-flags=--max-old-space-size={TAS_V8_MO}", str(script)]


def environnement(racine: Path) -> dict:
    """L'environnement VIDÉ du processus : il n'hérite d'aucun secret du serveur."""
    return {"DENO_DIR": str(racine / "deno_dir"), "NO_COLOR": "1", "PATH": "/usr/bin:/bin"}


def chemin_pyodide(racine: Path) -> Path:
    """Le runtime de Pyodide dans le bac à sable."""
    return racine / _PYODIDE_REL


def repertoire() -> Path:
    """Le répertoire du bac à sable, vérifié (`BacASableAbsent` sinon)."""
    return _repertoire()


def roues(racine: Path, requirements: list[str]) -> list[str]:
    """Les roues de la liste blanche pour `requirements` (`ValueError` hors liste)."""
    return _roues(racine, requirements)


def executer(*, sources: dict, entrypoint: str, requirements: list[str], mode: str,
             sub: Optional[str], entree: Optional[dict] = None) -> dict:
    """Joue la fonction (`mode='run'`) ou ses tests (`mode='test'`) dans le bac à sable.

    Rend la sortie du harnais — `{ok, result, warnings, files}` ou `{ok, error}` en
    `run`, `{ok, tests}` en `test` — plus `journal` (ce que le code a imprimé) et
    `duree_ms`. Lève `ExecutionEchouee` si le bac à sable n'a rien rendu d'exploitable,
    `BacASableOccupe` si `sub` ou le processus n'a plus de place (`places.py`).
    """
    if mode not in ("run", "test"):
        raise ValueError(f"mode inconnu : {mode}")
    racine = _repertoire()
    roues = _roues(racine, requirements)
    nonce = secrets.token_hex(16)
    pyodide = racine / _PYODIDE_REL
    tache = {
        "pyodide": str(pyodide), "wheels": roues, "nonce": nonce,
        "harness": _HARNAIS.read_text(encoding="utf-8"),
        "job": {"sources": sources, "entrypoint": entrypoint, "mode": mode,
                "input": entree if entree is not None else {}},
    }
    with places.place(sub):
        debut = time.monotonic()
        code, sortie, erreurs = _jouer(lancement(racine, _SCRIPT, DELAI_S),
                                       json.dumps(tache).encode())
    duree = int((time.monotonic() - debut) * 1000)
    reponse = _lire_reponse(sortie, nonce)
    if reponse is None:
        raise ExecutionEchouee(
            f"le bac à sable n'a rendu aucune réponse (code {code}) : "
            f"{erreurs.decode(errors='replace')[-1500:]}")
    if not reponse.get("ok"):
        raise ExecutionEchouee(reponse.get("erreur") or "échec sans message")
    rendu = dict(reponse["sortie"])
    rendu["journal"] = reponse.get("journal") or ""
    rendu["duree_ms"] = duree
    return rendu


def _jouer(lance: isolement.Lancement, entree: bytes) -> tuple[int, bytes, bytes]:
    """Lance le processus, lui donne `entree`, et lit ses deux sorties EN FLUX : la
    sortie standard jusqu'à `MAX_REPONSE` (au-delà : processus tué, `ReponseTropLongue`),
    la fin de la sortie d'erreur. Au-delà de `DELAI_S` : tué, `ExecutionEchouee`."""
    sortie, erreurs = bytearray(), bytearray()
    deborde = threading.Event()
    with subprocess.Popen(lance.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=lance.env, cwd=lance.cwd) as proc:
        def _tuer() -> None:
            proc.kill()
            lance.arreter()

        def _ecrire() -> None:
            try:
                proc.stdin.write(entree)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                # Le processus s'est arrêté avant d'avoir tout lu : sa sortie (absente)
                # le dira, en `ExecutionEchouee`.
                pass

        def _lire_sortie() -> None:
            for bloc in iter(lambda: proc.stdout.read1(_BLOC), b""):
                if len(sortie) + len(bloc) > MAX_REPONSE:
                    deborde.set()
                    _tuer()
                    return
                sortie.extend(bloc)

        def _lire_erreurs() -> None:
            for bloc in iter(lambda: proc.stderr.read1(_BLOC), b""):
                erreurs.extend(bloc)
                del erreurs[:-QUEUE_ERREURS]

        fils = [threading.Thread(target=f, daemon=True)
                for f in (_ecrire, _lire_sortie, _lire_erreurs)]
        for fil in fils:
            fil.start()
        try:
            proc.wait(timeout=DELAI_S)
        except subprocess.TimeoutExpired:
            _tuer()
            raise ExecutionEchouee(f"délai dépassé ({DELAI_S} s)") from None
        finally:
            for fil in fils:
                fil.join(timeout=5)
    if deborde.is_set():
        raise ReponseTropLongue(f"réponse de plus de {MAX_REPONSE} octets : processus "
                                "arrêté")
    return proc.returncode, bytes(sortie), bytes(erreurs)


def _lire_reponse(stdout: bytes, nonce: str) -> Optional[dict]:
    """La DERNIÈRE ligne qui porte le nonce — et aucune autre."""
    for ligne in reversed(stdout.decode(errors="replace").splitlines()):
        if ligne.startswith(nonce):
            return json.loads(ligne[len(nonce):])
    return None

