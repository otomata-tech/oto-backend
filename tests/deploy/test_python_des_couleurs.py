"""Le Python des couleurs de notre box (`BG_PYTHON`, 24/11/2026 : oto.cx en 3.12, plancher
`requires-python` inchangé) — docs/deploiement.md § Le Python des couleurs.

Les gestes complets de chaque scénario `prod-python-*` sont figés dans
`tests/deploy/gestes_bleu_vert/` et rejoués par `test_gestes_bleu_vert_967.py` ; ici, ce
que chacun doit prouver, dit en clair. Les scénarios d'avant tournent SANS `BG_PYTHON` et
gardent leurs références à l'octet : c'est la preuve qu'une bibliothèque qui ne le reçoit
pas fait les gestes d'avant.

Le dernier test rejoue la montée avec le VRAI uv (`uv venv`, `uv sync --python`) : CI
seulement (`OTO_BANC_UV_REEL_EXIGE=1`, job `plancher-python-uv-reel`), dans une racine
jetable où les interpréteurs sont COPIÉS — jamais sur un poste partagé.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

import _banc_bleu_vert as banc


def _rejouer(scenario: str) -> str:
    return banc.rejouer(banc.DEPLOY_DU_DEPOT, scenario)


def _commandes(trace: str) -> list[str]:
    return trace.split("### commandes\n")[1].split("### sortie\n")[0].splitlines()


def _venvs(trace: str) -> str:
    return trace.split("### venvs\n")[1]


def test_le_wrapper_declare_l_interpreteur_exact_au_patch():
    source = (banc.DEPLOY_DU_DEPOT / "oto-backend.sh").read_text(encoding="utf-8")
    declarations = re.findall(r"(?m)^BG_PYTHON=(.*)$", source)
    assert declarations == [banc.BG_PYTHON]
    # Au patch : le dossier de l'interpréteur, pas le lien de version mineure d'uv.
    assert re.search(r"/cpython-\d+\.\d+\.\d+-", declarations[0])
    # Déclaré AVANT le chargement de la bibliothèque, qui le lit.
    assert source.index("BG_PYTHON=") < source.index(". /opt/deploy/oto-mcp-bluegreen.sh")


def test_sans_bg_python_aucun_geste_ne_touche_au_python():
    """Les gestes d'avant : ni lecture de version, ni `--python`, ni `uv venv`."""
    for scenario in ("prod-bleu-vers-vert", "prod-retour-arriere", "prod-bac-a-sable-ko"):
        commandes = _commandes(_rejouer(scenario))
        assert not [c for c in commandes if "--python" in c or c.startswith(
            ("uv venv", "python3.12", "python -c"))], scenario


def test_venv_3_10_recree_sur_bg_python_puis_bascule():
    trace = _rejouer("prod-python-venv-recree")
    commandes = _commandes(trace)
    assert "### code de sortie : 0\n" in trace
    assert (f"uv venv --quiet --seed --python {banc.BG_PYTHON} /opt/oto-mcp-green/.venv"
            in commandes)
    assert f"uv sync --frozen --quiet --python {banc.BG_PYTHON}" in commandes
    assert "venv de green recréé : Python 3.10.12 → 3.12.14" in trace
    # La recréation précède l'installation, la garde précède le démarrage.
    assert (commandes.index("git reset --hard v1.2.3")
            > [i for i, c in enumerate(commandes) if c.startswith("uv venv")][0])
    assert "garde Python : /opt/oto-mcp-green/.venv rend Python 3.12.14" in trace
    assert _venvs(trace) == "blue : .venv 3.10.12\ngreen : .venv 3.12.14\n"
    assert "--- /etc/oto-mcp/active-prod\ngreen\n" in trace


def test_la_couleur_en_service_n_est_jamais_recreee():
    """La bleue sert pendant la montée : jamais lue, jamais recréée — c'est le retour."""
    trace = _rejouer("prod-python-venv-recree")
    assert not [c for c in _commandes(trace) if "oto-mcp-blue" in c and "uv" in c]


def test_venv_deja_au_patch_rien_n_est_recree():
    trace = _rejouer("prod-python-venv-garde")
    commandes = _commandes(trace)
    assert "### code de sortie : 0\n" in trace
    assert not [c for c in commandes if c.startswith("uv venv")]
    assert f"uv sync --frozen --quiet --python {banc.BG_PYTHON}" in commandes
    assert "venv de green déjà en Python 3.12.14 (BG_PYTHON) : gardé" in trace


def test_echec_du_sync_remet_code_et_venv_d_avant():
    trace = _rejouer("prod-python-sync-ko")
    commandes = _commandes(trace)
    assert "### code de sortie : 1\n" in trace
    assert ("git -C /opt/oto-mcp-green reset --quiet --hard "
            "fedcba9876543210fedcba9876543210fedcba98") in commandes
    assert "green remise à son état d'avant" in trace
    assert _venvs(trace) == "blue : .venv 3.10.12\ngreen : .venv 3.10.12\n"
    assert not [c for c in commandes if c.startswith(("systemctl start", "caddy"))]
    assert "--- /etc/oto-mcp/active-prod\nblue\n" in trace


def test_echec_d_uv_venv_remet_l_ancien_et_n_installe_rien():
    trace = _rejouer("prod-python-uv-venv-ko")
    commandes = _commandes(trace)
    assert "### code de sortie : 1\n" in trace
    assert not [c for c in commandes if c.startswith(("git reset", "uv sync", "systemctl start"))]
    assert _venvs(trace) == "blue : .venv 3.10.12\ngreen : .venv 3.10.12\n"


def test_la_garde_refuse_un_autre_patch_et_rien_ne_bascule():
    trace = _rejouer("prod-python-garde-refuse")
    commandes = _commandes(trace)
    assert "### code de sortie : 1\n" in trace
    assert "REFUS (garde Python) : /opt/oto-mcp-green/.venv rend Python 3.12.13" in trace
    assert not [c for c in commandes if c.startswith(("systemctl start", "caddy", "systemctl reload"))]
    assert _venvs(trace) == "blue : .venv 3.10.12\ngreen : .venv 3.10.12\n"
    assert "--- /etc/oto-mcp/active-prod\nblue\n" in trace


def test_un_bg_python_qui_ne_s_execute_pas_refuse_avant_tout():
    trace = _rejouer("prod-python-illisible")
    commandes = _commandes(trace)
    assert "### code de sortie : 1\n" in trace and "ne s'exécute pas" in trace
    assert not [c for c in commandes if c.startswith(("git", "uv", "systemctl start"))]


def test_le_retour_arriere_ne_lit_pas_bg_python():
    trace = _rejouer("prod-python-retour-arriere")
    assert "### code de sortie : 0\n" in trace
    assert not [c for c in _commandes(trace) if c.startswith(("uv", "python"))]
    assert _venvs(trace) == "blue : .venv 3.10.12\ngreen : .venv 3.10.12\n"


@pytest.mark.parametrize("cas", ["active", "tourne"])
def test_bg_venv_recreer_refuse_une_couleur_qui_sert(tmp_path, cas):
    """Appelée seule (comme une cible pourrait le faire) : la couleur en service, ou dont
    l'unité tourne, garde son venv ; `uv` n'est jamais appelé."""
    arbre = tmp_path / "arbre-blue"
    (arbre / ".venv").mkdir(parents=True)
    (arbre / ".venv/temoin").write_text("avant")
    (tmp_path / "active").write_text("blue\n" if cas == "active" else "green\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for nom, code in (("uv", 0), ("systemctl", 0), ("git", 0)):
        (bin_ / nom).write_text(f'#!/bin/bash\necho "{nom} $*" >> "{tmp_path}/trace"\nexit {code}\n')
        (bin_ / nom).chmod(0o755)
    declaration = {
        "BG_ENV": "t", "BG_UNIT": "u", "BG_TREE": str(tmp_path / "arbre"), "BG_PORT_blue": "1",
        "BG_PORT_green": "2", "BG_UPSTREAM": "/x/amont", "BG_SNIPPET": "s",
        "BG_DOCSHARE_HOST": "h", "BG_ASK": "", "BG_ACTIVE": str(tmp_path / "active"),
        "BG_PUBLIC": "https://h/", "BG_DRAIN_MAX": "1", "BG_LOCK": "/x/verrou",
        "BG_CADDYFILE": "/x/Caddyfile", "BG_DRAIN": "/x/vidange", "BG_DRAIN_UNIT": "v",
        "BG_LANCEUR": "versionne",
    }
    script = ("".join(f"{k}={v!r}\n" for k, v in declaration.items())
              + f". {banc.DEPLOY_DU_DEPOT}/oto-mcp-bluegreen.sh\n"
              + f"bg_venv_recreer blue {arbre} /x/python3.12 && echo recree || echo refuse\n")
    fini = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{bin_}:/usr/bin:/bin", "LANG": "C.UTF-8"})
    assert fini.stdout.strip().endswith("refuse"), fini.stdout + fini.stderr
    assert "REFUS" in fini.stdout
    assert (arbre / ".venv/temoin").read_text() == "avant"
    trace = (tmp_path / "trace").read_text() if (tmp_path / "trace").exists() else ""
    assert "uv " not in trace


# --- le vrai uv, CI seulement --------------------------------------------------------
UV = shutil.which("uv")  # un chemin, pas un travail
ANCIEN, NOUVEAU = "3.11", "3.12"


def _interpreteur_gere(version: str) -> pathlib.Path | None:
    if not UV:
        return None
    env = {k: v for k, v in os.environ.items() if k != "UV_PYTHON"}
    fini = subprocess.run([UV, "python", "find", "--managed-python", "--no-config", version],
                          capture_output=True, text=True,
                          env={**env, "UV_PYTHON_DOWNLOADS": "never"})
    return pathlib.Path(fini.stdout.strip()) if fini.returncode == 0 else None


@pytest.fixture
def uv_reel():
    """⚠️ CI SEULEMENT : sur un poste partagé, une fuite hors de la racine jetable touche le
    dossier uv de tout le monde (24/11/2026 : tous les venvs uv d'un poste cassés)."""
    if os.environ.get("OTO_BANC_UV_REEL_EXIGE") != "1":
        pytest.skip("vrai uv : CI seulement (OTO_BANC_UV_REEL_EXIGE=1), jamais sur un poste partagé")
    trouves = {v: _interpreteur_gere(v) for v in (ANCIEN, NOUVEAU)}
    if not all(trouves.values()):
        pytest.fail(f"uv et ses interpréteurs gérés {ANCIEN} et {NOUVEAU} requis (aucun téléchargement)")
    return trouves


def test_bg_python_sur_le_vrai_uv(tmp_path, uv_reel):
    """Venvs des deux couleurs en 3.11 (pour le 3.10 de la box), BG_PYTHON = un 3.12 COPIÉ
    dans la racine jetable, au chemin que déclare le wrapper. Trois montées : la verte puis
    la bleue passent sur BG_PYTHON, `uv sync --python` garde le venv recréé (pip, posé par
    `--seed`, y est encore), la couleur en service n'est jamais touchée ; la troisième, sur
    un venv déjà au patch, ne recrée rien."""
    racine = tmp_path / "racine"
    racine.mkdir()
    variantes = {"BANC_BG_PYTHON": "1", "BANC_SANS_BAC_A_SABLE": "1"}
    doublures = banc._preparer(racine, banc.DEPLOY_DU_DEPOT, "prod", "blue", variantes)
    (doublures / "uv").write_text(
        f'#!/bin/bash\necho "uv $*" >> "$BANC_TRACE"\nexec {UV} --no-config "$@"\n')
    # Les interpréteurs, COPIÉS : uv n'écrit jamais dans le dossier uv de la machine.
    bg_python = pathlib.Path(str(racine) + banc.BG_PYTHON)
    shutil.rmtree(bg_python.parents[1])
    shutil.copytree(uv_reel[NOUVEAU].parents[1], bg_python.parents[1], symlinks=True)
    ancien = racine / "pythons/ancien"
    shutil.copytree(uv_reel[ANCIEN].parents[1], ancien, symlinks=True)
    env = {
        "PATH": f"{doublures}:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(racine),
        "BANC_TRACE": str(racine / "trace"), "BANC_SHA": banc.SHA,
        "BANC_ETAT": str(doublures / "etat"),
        "UV_CACHE_DIR": str(racine / "cache-uv"),
        "UV_PYTHON_INSTALL_DIR": str(racine / "pythons-uv"),
        "UV_PYTHON_DOWNLOADS": "never",
    }

    def version(python: pathlib.Path) -> str:
        return subprocess.run([str(python), "-c",
                               'import sys; print("%d.%d.%d" % sys.version_info[:3])'],
                              capture_output=True, text=True, check=True).stdout.strip()

    attendue = version(bg_python)
    arbres = {c: pathlib.Path(f"{racine}/opt/oto-mcp-{c}") for c in ("blue", "green")}
    for arbre in arbres.values():
        (arbre / "pyproject.toml").write_text(
            '[project]\nname = "banc-bg-python"\nversion = "0"\nrequires-python = ">=3.10"\n'
            'dependencies = []\n\n[tool.uv]\npackage = false\n', encoding="utf-8")
        verrou = subprocess.run([UV, "lock", "--quiet", "--offline", "--no-config",
                                 "--python", str(bg_python)], cwd=arbre, env=env,
                                capture_output=True, text=True)
        assert verrou.returncode == 0, verrou.stderr
        shutil.rmtree(arbre / ".venv")
        venv = subprocess.run([UV, "venv", "--quiet", "--no-config", "--python",
                               str(next((ancien / "bin").glob("python3.1?"))),
                               str(arbre / ".venv")], env=env, capture_output=True, text=True)
        assert venv.returncode == 0, venv.stderr
        (arbre / ".venv/temoin").write_text("avant")
    assert version(arbres["blue"] / ".venv/bin/python").startswith(ANCIEN + ".")

    def monter() -> list[str]:
        (racine / "trace").write_text("")
        fini = subprocess.run(["/bin/bash", str(racine / "opt/deploy/oto-backend.sh"), "v1.2.3"],
                              env=env, cwd=str(racine), capture_output=True, text=True,
                              timeout=300)
        assert fini.returncode == 0, fini.stdout + fini.stderr
        return (racine / "trace").read_text().splitlines()

    def pip(couleur: str) -> bool:
        return bool(list((arbres[couleur] / ".venv/lib").glob("python3.*/site-packages/pip")))

    # 1. la bleue sert : la verte passe sur BG_PYTHON, `uv sync` garde le venv recréé
    monter()
    assert version(arbres["green"] / ".venv/bin/python") == attendue
    assert not (arbres["green"] / ".venv/temoin").exists() and pip("green")
    assert not (arbres["green"] / ".venv.avant-python").exists()
    assert (arbres["blue"] / ".venv/temoin").exists()                   # en service : intacte
    assert version(arbres["blue"] / ".venv/bin/python").startswith(ANCIEN + ".")
    # 2. la verte sert : la bleue passe à son tour
    monter()
    assert version(arbres["blue"] / ".venv/bin/python") == attendue and pip("blue")
    # 3. la bleue sert : la verte, déjà au patch, n'est pas recréée
    (arbres["green"] / ".venv/temoin").write_text("au patch")
    commandes = monter()
    assert (arbres["green"] / ".venv/temoin").read_text() == "au patch"
    assert not [c for c in commandes if c.startswith("uv venv")]
    assert pip("green")
