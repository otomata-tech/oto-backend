"""L'isolement du processus du bac à sable : sous quelle identité, avec quelles bornes.

Deno refuse au code ce que ses permissions n'accordent pas (`executor.commande`). Ce
module ajoute une seconde barrière, posée par le SYSTÈME autour du processus : il ne
tourne jamais avec l'identité, l'environnement, le réseau ni la mémoire du serveur.

**Le mode se déclare** (`OTO_FUNCTIONS_ISOLATION`), il ne se devine pas :

- `systemd` (le défaut) — une unité transitoire `systemd-run`, sous un utilisateur dédié
  non privilégié (`OTO_FUNCTIONS_USER`), durcie : sans réseau, système de fichiers en
  lecture seule, aucune capacité, appels système filtrés, mémoire, tâches et durée
  bornées par le cgroup de l'unité. Exige un service qui peut lancer une unité sous un
  autre uid (un service privilégié ; pas par une règle polkit : elle ne saurait borner
  l'uid ni les propriétés de l'unité qu'elle autorise).
- `bwrap` — pour un service SANS privilège : `bwrap` dans un espace de noms
  utilisateur non privilégié (`--unshare-all` : ni réseau, ni processus, ni IPC de
  l'hôte), un système de fichiers qui ne montre que le système, le bac à sable et les
  scripts, en lecture seule ; mémoire bornée par `prlimit --data`, temps CPU par
  `prlimit --cpu` (le délai de l'appelant plus `MARGE_S`, en secondes CPU : un filet,
  le délai mural reste celui de l'appelant). Le processus garde l'uid du service, mais
  ne voit aucun de ses fichiers.
- `direct` — Deno lancé tel quel, seulement s'il est DEMANDÉ : poste de dev, CI.

Une valeur inconnue, ou un mode dont l'outil ou l'utilisateur manque, LÈVE
`IsolementIndisponible` (un `BacASableAbsent` : 503 `sandbox_unavailable`). Jamais de
repli vers un mode plus faible.

Combien d'exécutions à la fois : `places.py` (un plafond par processus, une par compte).
"""
from __future__ import annotations

import logging
import os
import pwd
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENV_MODE = "OTO_FUNCTIONS_ISOLATION"
ENV_UTILISATEUR = "OTO_FUNCTIONS_USER"
MODES = ("systemd", "bwrap", "direct")
MODE_DEFAUT = "systemd"
UTILISATEUR_DEFAUT = "oto-fonctions"
#: Le préfixe des unités transitoires : une règle d'hôte peut les nommer.
PREFIXE_UNITE = "oto-fn-"

#: La mémoire d'UNE exécution : le tas V8 (`executor.TAS_V8_MO`) plus la mémoire
#: WebAssembly de Pyodide, que V8 ne borne pas.
MEMOIRE_MO = 1024
#: Les tâches (fils d'exécution) d'une exécution : Deno en ouvre une quinzaine.
TACHES = 64
#: Ce qu'on laisse au système, au-delà du délai de l'appelant, avant de tuer l'unité.
MARGE_S = 5

_PATH = "/usr/bin:/bin"
#: Le durcissement de l'unité transitoire (`systemd.exec(5)`, `systemd.resource-control(5)`).
_PROPRIETES = (
    "NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
    "PrivateNetwork=yes", "IPAddressDeny=any", "RestrictAddressFamilies=AF_UNIX",
    "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
    "PrivateIPC=yes", "ProtectProc=invisible", "ProcSubset=pid",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes",
    "RestrictNamespaces=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",
    "LockPersonality=yes", "SystemCallArchitectures=native",
    # `@pkey` : V8 protège sa mémoire exécutable par clés de protection (`pkey_*`), que
    # `@system-service` ne comprend pas — sans lui, Deno est tué à son premier appel.
    "SystemCallFilter=@system-service @pkey", "UMask=0077", "LimitCORE=0",
    f"MemoryMax={MEMOIRE_MO}M", "MemorySwapMax=0", f"TasksMax={TACHES}",
    # Un cœur au plus par exécution : N exécutions simultanées (`places.py`) ne prennent
    # pas toute la machine.
    "CPUQuota=100%",
)


class IsolementIndisponible(RuntimeError):
    """Le mode d'isolement déclaré ne peut pas s'appliquer sur cette instance."""


@dataclass(frozen=True)
class Lancement:
    """Ce qu'il faut pour lancer UN processus du bac à sable, et l'arrêter."""
    argv: list[str]
    env: dict
    cwd: Optional[str]
    #: L'unité transitoire (mode `systemd`) : tuer son client ne la tue pas.
    unite: Optional[str] = None

    def arreter(self) -> None:
        """Tue l'unité transitoire, s'il y en a une. Une unité déjà finie (et collectée)
        n'est plus connue : ce n'est pas une panne."""
        if self.unite is None:
            return
        fini = subprocess.run(["systemctl", "kill", "--signal=SIGKILL", self.unite],
                              capture_output=True, timeout=10, env={"PATH": _PATH})
        if fini.returncode != 0 and b"not loaded" not in fini.stderr:
            logger.warning("bac à sable : arrêt de %s en échec : %s", self.unite,
                           fini.stderr.decode(errors="replace")[-300:])


def mode() -> str:
    """Le mode déclaré, lu à chaque exécution. Une valeur inconnue lève."""
    m = (os.environ.get(ENV_MODE) or MODE_DEFAUT).strip().lower()
    if m not in MODES:
        raise IsolementIndisponible(f"{ENV_MODE}={m!r} : attendu {', '.join(MODES)}.")
    return m


def preparer(commande: list[str], env: dict, racine: Path, lisibles: list[Path],
             duree_max_s: float) -> Lancement:
    """Enveloppe `commande` (la ligne de Deno) selon le mode déclaré. `lisibles` : ce
    que le processus doit pouvoir lire (le bac à sable, les scripts) ; `duree_max_s` :
    le délai de l'appelant, que l'unité borne aussi."""
    m = mode()
    if m == "direct":
        return Lancement(argv=commande, env=env, cwd=str(racine))
    if m == "systemd":
        return _systemd(commande, env, racine, duree_max_s)
    return _bwrap(commande, env, racine, lisibles, duree_max_s)


def _outil(nom: str) -> str:
    chemin = shutil.which(nom, path=_PATH)
    if chemin is None:
        raise IsolementIndisponible(f"{ENV_MODE}={mode()} exige `{nom}`, absent de {_PATH}.")
    return chemin


def _systemd(commande: list[str], env: dict, racine: Path, duree_max_s: float) -> Lancement:
    nom = os.environ.get(ENV_UTILISATEUR) or UTILISATEUR_DEFAUT
    try:
        compte = pwd.getpwnam(nom)
    except KeyError:
        raise IsolementIndisponible(
            f"{ENV_MODE}=systemd exige l'utilisateur `{nom}` ({ENV_UTILISATEUR}), absent "
            "de cette machine.") from None
    if compte.pw_uid == 0 or compte.pw_gid == 0:
        raise IsolementIndisponible(f"`{nom}` est privilégié : le bac à sable n'y tourne pas.")
    unite = f"{PREFIXE_UNITE}{secrets.token_hex(8)}.service"
    argv = [_outil("systemd-run"), f"--unit={unite}", f"--uid={compte.pw_uid}",
            f"--gid={compte.pw_gid}", "--pipe", "--wait", "--collect", "--quiet",
            f"--working-directory={racine}"]
    argv += [f"--setenv={k}={v}" for k, v in env.items()]
    argv += [f"--property={p}" for p in _PROPRIETES]
    argv += [f"--property=RuntimeMaxSec={int(duree_max_s) + MARGE_S}", "--", *commande]
    return Lancement(argv=argv, env={"PATH": _PATH}, cwd=None, unite=unite)


def _bwrap(commande: list[str], env: dict, racine: Path, lisibles: list[Path],
           duree_max_s: float) -> Lancement:
    # `--cpu` : des secondes de CPU (tous fils confondus), un filet sous le délai mural
    # de l'appelant — un code qui calcule sur plusieurs fils y arrive avant lui.
    argv = [_outil("prlimit"), f"--data={MEMOIRE_MO * 1024 * 1024}", "--core=0",
            f"--cpu={int(duree_max_s) + MARGE_S}", "--",
            _outil("bwrap"), "--unshare-all", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr"]
    # Les répertoires du système hors de /usr : liens symboliques sur un système à /usr
    # fusionné, répertoires sinon.
    for nom in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
        chemin = Path(nom)
        if chemin.is_symlink():
            argv += ["--symlink", os.readlink(chemin), nom]
        elif chemin.is_dir():
            argv += ["--ro-bind", nom, nom]
    # Les montages s'appliquent dans l'ordre : `/tmp` vide AVANT le bac à sable, qui peut
    # y vivre (un poste de dev) — posé après, il le masquerait.
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for chemin in dict.fromkeys(str(Path(p).resolve()) for p in [racine, *lisibles]):
        argv += ["--ro-bind", chemin, chemin]
    argv += ["--chdir", str(racine)]
    for k, v in env.items():
        argv += ["--setenv", k, v]
    return Lancement(argv=[*argv, "--", *commande], env={"PATH": _PATH}, cwd=None)
