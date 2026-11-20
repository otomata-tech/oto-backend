"""Les travaux planifiés de notre box passent par le lanceur générique (#967, lot 5c).

La maintenance quotidienne et l'archive du journal lisent la base et le stockage : une
fois les secrets sortis du `.env`, ils n'y ont plus accès qu'en passant par
`deploy/lanceur_secrets.py`. La maintenance et l'archive sont posées par le
déploiement prod (`install_timers`), sur l'arbre de la couleur en service (#1807).
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import _banc_bleu_vert as banc

DEPLOY = banc.DEPLOY_DU_DEPOT


def _unite(nom: str) -> dict[str, list[str]]:
    lignes = (DEPLOY / nom).read_text(encoding="utf-8").splitlines()
    unite: dict[str, list[str]] = {}
    for ligne in lignes:
        m = re.match(r"([A-Za-z]+)=(.*)", ligne)
        if m:
            unite.setdefault(m.group(1), []).append(m.group(2))
    return unite


def test_la_maintenance_demarre_par_le_lanceur_avec_la_cle_d_api_en_credential():
    u = _unite("oto-mcp-maintenance.service")
    assert u["ExecStart"] == [
        "@ARBRE@/.venv/bin/python @ARBRE@/deploy/lanceur_secrets.py maintenance all"]
    assert u["WorkingDirectory"] == ["@ARBRE@"]
    assert u["LoadCredential"] == ["scw:/etc/oto-mcp/scw.key"]
    assert u["EnvironmentFile"] == ["/opt/oto-mcp/.env", "/etc/oto-mcp/lanceur-prod.env"]


def test_l_archive_du_journal_demarre_le_script_versionne_par_le_lanceur():
    u = _unite("oto-journal-archive.service")
    assert u["ExecStart"] == [
        "@ARBRE@/.venv/bin/python @ARBRE@/deploy/lanceur_secrets.py"
        " --script deploy/archive_tool_calls.py"]
    assert u["WorkingDirectory"] == ["@ARBRE@"]
    assert u["LoadCredential"] == ["scw:/etc/oto-mcp/scw.key"]
    assert (DEPLOY / "archive_tool_calls.py").is_file()


def test_install_timers_ecrit_l_arbre_de_la_couleur_en_service(tmp_path):
    """Le déploiement pose l'unité avec l'arbre qui vient d'être activé, pas un chemin
    figé qui pourrait porter un tag sans lanceur."""
    racine = tmp_path
    arbre = racine / "opt/oto-mcp-green"
    (arbre / "deploy").mkdir(parents=True)
    (racine / "etc/systemd/system").mkdir(parents=True)
    for u in ("oto-mcp-maintenance.service", "oto-mcp-maintenance.timer",
              "oto-journal-archive.service", "oto-journal-archive.timer"):
        (arbre / "deploy" / u).write_text((DEPLOY / u).read_text(encoding="utf-8"))
    source = (DEPLOY / "oto-backend.sh").read_text(encoding="utf-8")
    fonction = re.search(r"^install_timers\(\) \{.*?^\}", source, re.S | re.M).group(0)
    (racine / "bin").mkdir()
    for nom in ("systemctl",):
        (racine / "bin" / nom).write_text("#!/bin/bash\nexit 0\n")
        (racine / "bin" / nom).chmod(0o755)
    script = banc._reecrire(fonction, str(racine)) + f'\ninstall_timers "{arbre}"\n'
    fini = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": f"{racine}/bin:/usr/bin:/bin"}, timeout=30)
    assert fini.returncode == 0, fini.stderr
    pose = (racine / "etc/systemd/system/oto-mcp-maintenance.service").read_text()
    assert "@ARBRE@" not in pose
    assert f"ExecStart={arbre}/.venv/bin/python {arbre}/deploy/lanceur_secrets.py maintenance all" in pose
    assert f"WorkingDirectory={arbre}\n" in pose
    # L'archive du journal suit la même couleur (#1807), plus /opt/oto-mcp figé.
    archive = (racine / "etc/systemd/system/oto-journal-archive.service").read_text()
    assert "@ARBRE@" not in archive and "/opt/oto-mcp/.venv" not in archive
    assert (f"ExecStart={arbre}/.venv/bin/python {arbre}/deploy/lanceur_secrets.py "
            "--script deploy/archive_tool_calls.py") in archive
    assert (racine / "etc/systemd/system/oto-journal-archive.timer").is_file()


def test_la_migration_documentee_passe_par_le_lanceur_avec_l_environnement_des_unites():
    """La ligne `systemd-run` de docs/migrations-versionnees.md (oto-backend#1105) porte les
    fichiers d'environnement et la clé d'API des unités de prod : une ligne qui en
    perdrait un tirerait les secrets d'ailleurs, ou pas du tout."""
    doc = (DEPLOY.parent / "docs" / "migrations-versionnees.md").read_text(encoding="utf-8")
    [ligne] = [l for l in doc.splitlines()
               if "systemd-run" in l and "lanceur_secrets.py migrer upgrade head" in l]
    proprietes = re.findall(r"-p (\w+)=(\S+)", ligne)
    u = _unite("oto-mcp-maintenance.service")
    assert [v for k, v in proprietes if k == "EnvironmentFile"] == u["EnvironmentFile"]
    assert [v for k, v in proprietes if k == "LoadCredential"] == u["LoadCredential"]
    assert ligne.endswith("$A/.venv/bin/python $A/deploy/lanceur_secrets.py migrer upgrade head")
