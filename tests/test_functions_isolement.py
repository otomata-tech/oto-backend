"""L'isolement du processus du bac à sable (`functions/isolement.py`) : le mode se
DÉCLARE, un mode qui ne peut pas s'appliquer lève, et jamais de repli vers un plus
faible. Ces bancs lisent la commande construite ; les bancs `*_live` la lancent."""
from __future__ import annotations

import pwd
from pathlib import Path

import pytest

from oto_mcp.functions import executor, isolement

RACINE = Path("/bac")
COMMANDE = ["/bac/deno", "run", "script.ts"]
ENV = {"DENO_DIR": "/bac/deno_dir", "NO_COLOR": "1", "PATH": "/usr/bin:/bin"}


def _preparer(duree=60):
    return isolement.preparer(COMMANDE, ENV, RACINE, [Path("/scripts")], duree)


def _compte(uid=990, gid=990):
    return pwd.struct_passwd(("oto-fonctions", "x", uid, gid, "", "/nonexistent",
                              "/usr/sbin/nologin"))


@pytest.fixture
def outils(monkeypatch):
    monkeypatch.setattr(isolement.shutil, "which", lambda nom, path=None: f"/usr/bin/{nom}")


def test_sans_variable_le_mode_est_systemd(monkeypatch):
    monkeypatch.delenv(isolement.ENV_MODE, raising=False)
    assert isolement.mode() == "systemd"


def test_un_mode_inconnu_leve(monkeypatch):
    monkeypatch.setenv(isolement.ENV_MODE, "aucun")
    with pytest.raises(isolement.IsolementIndisponible):
        _preparer()


def test_direct_ne_s_obtient_que_demande(monkeypatch):
    monkeypatch.setenv(isolement.ENV_MODE, "direct")
    lance = _preparer()
    assert lance.argv == COMMANDE and lance.env == ENV and lance.unite is None


def test_systemd_sans_utilisateur_dedie_leve(monkeypatch, outils):
    monkeypatch.setenv(isolement.ENV_MODE, "systemd")

    def _absent(nom):
        raise KeyError(nom)
    monkeypatch.setattr(isolement.pwd, "getpwnam", _absent)
    with pytest.raises(isolement.IsolementIndisponible, match="oto-fonctions"):
        _preparer()


def test_systemd_refuse_un_utilisateur_privilegie(monkeypatch, outils):
    monkeypatch.setenv(isolement.ENV_MODE, "systemd")
    monkeypatch.setattr(isolement.pwd, "getpwnam", lambda nom: _compte(uid=0))
    with pytest.raises(isolement.IsolementIndisponible, match="privilégié"):
        _preparer()


def test_un_outil_absent_leve_sans_repli(monkeypatch):
    monkeypatch.setenv(isolement.ENV_MODE, "bwrap")
    monkeypatch.setattr(isolement.shutil, "which", lambda nom, path=None: None)
    with pytest.raises(isolement.IsolementIndisponible, match="prlimit"):
        _preparer()


def test_systemd_pose_une_unite_durcie_sous_l_utilisateur_dedie(monkeypatch, outils):
    monkeypatch.setenv(isolement.ENV_MODE, "systemd")
    monkeypatch.setattr(isolement.pwd, "getpwnam", lambda nom: _compte())
    lance = _preparer(duree=125)
    argv = lance.argv
    assert argv[0] == "/usr/bin/systemd-run" and argv[-len(COMMANDE):] == COMMANDE
    assert {"--uid=990", "--gid=990", "--pipe", "--wait", "--collect"} <= set(argv)
    assert lance.unite.startswith(isolement.PREFIXE_UNITE)
    assert f"--unit={lance.unite}" in argv
    proprietes = {a.split("=", 1)[1] for a in argv[:-len(COMMANDE)]
                  if a.startswith("--property=")}
    for attendue in ("PrivateNetwork=yes", "ProtectSystem=strict", "ProtectHome=yes",
                     "PrivateTmp=yes", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                     "RestrictAddressFamilies=AF_UNIX", "SystemCallFilter=@system-service @pkey",
                     f"MemoryMax={isolement.MEMOIRE_MO}M", f"TasksMax={isolement.TACHES}",
                     f"RuntimeMaxSec={125 + isolement.MARGE_S}",
                     # Un cœur au plus par exécution (03/11/2026).
                     "CPUQuota=100%"):
        assert attendue in proprietes, attendue
    # L'environnement du processus est celui, vidé, de l'exécuteur — rien du serveur.
    assert {a for a in argv if a.startswith("--setenv=")} == {
        f"--setenv={k}={v}" for k, v in ENV.items()}
    assert lance.env == {"PATH": "/usr/bin:/bin"}


def test_bwrap_ne_montre_que_le_systeme_et_le_bac_a_sable(monkeypatch, outils):
    monkeypatch.setenv(isolement.ENV_MODE, "bwrap")
    lance = _preparer(duree=125)
    argv = lance.argv
    assert argv[:2] == ["/usr/bin/prlimit", f"--data={isolement.MEMOIRE_MO * 1024 * 1024}"]
    # Le filet CPU (03/11/2026) : des secondes de CPU, délai de l'appelant plus la marge,
    # posé par prlimit AVANT bwrap (donc hérité par Deno).
    assert f"--cpu={125 + isolement.MARGE_S}" in argv[:argv.index("--")]
    assert {"--unshare-all", "--die-with-parent", "--new-session", "--clearenv"} <= set(argv)
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    montes = [argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--bind")]
    assert "--bind" not in argv, "rien n'est monté en écriture"
    assert "/usr" in montes and str(RACINE) in montes and "/scripts" in montes
    assert not any(m.startswith(("/home", "/etc", "/opt", "/var")) for m in montes)
    assert argv[-len(COMMANDE):] == COMMANDE and lance.unite is None


def test_l_executeur_traduit_un_isolement_indisponible_en_bac_absent(monkeypatch, tmp_path):
    monkeypatch.setenv(isolement.ENV_MODE, "aucun")
    with pytest.raises(executor.BacASableAbsent):
        executor.lancement(tmp_path, tmp_path / "script.ts", 60)
