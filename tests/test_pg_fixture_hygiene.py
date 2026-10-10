"""La fixture PostgreSQL jetable ne fuit plus (#640) — et ça se prouve sur le VRAI docker.

Sans docker, tout ceci se skippe : le comportement de la suite est inchangé. Sans l'image
DÉJÀ présente localement aussi : la tirer ici ramènerait Docker Hub dans la CI, qui n'en
dépend plus depuis le 09/10/2026 (« toomanyrequests » sur l'adresse partagée des runners,
sept parts rouges) — la base de la CI vient du runner, par `OTO_TEST_PG_DSN`. Avec :
- le conteneur de la session porte ses deux étiquettes et n'a AUCUN volume (`Mounts`
  vide, `PGDATA` en tmpfs) — et le TÉMOIN prouve que l'assertion mord : le même
  conteneur sans tmpfs a bien un volume anonyme ;
- la sortie couvre `atexit`, SIGTERM et SIGINT : un processus enfant pose un factice,
  on le coupe, le factice disparaît AVEC son volume anonyme et le signal est RELAYÉ (un
  SIGTERM tue le processus DE SIGTERM ; une sortie propre masquerait la coupure) ;
- le balai retire un factice « lancé il y a 3 h » avec son volume, garde un factice
  frais, ne touche pas un factice étiqueté mais non daté — par la fonction, puis par le
  VRAI hook de début de session (un pytest enfant en `--collect-only`, qui le dit).

Le volume compte autant que le conteneur : `docker rm -f` sans `-v` laisse le volume
anonyme d'un conteneur `--rm` (prouvé le 30/08/2026 — il survit au `rm -f` nu, pas au
`rm -f -v`). L'ancienne fixture en fuyait donc un À CHAQUE RUN PROPRE : les ~260
volumes/jour de l'issue, pas seulement les dix orphelins.

Les factices sont l'image pgvector déjà requise par la fixture, avec `sleep` : aucun
pull réseau de plus. Ils ne s'appellent pas `oto-test-pg-*` — le balai va à l'étiquette,
pas au nom, et c'est aussi ce qu'on prouve.
"""
from __future__ import annotations

import json
import pathlib
import signal
import subprocess
import sys
import time
import uuid

import pytest

import _pg_hygiene as hygiene
from _pg_hygiene import IMAGE, LABEL, LABEL_STARTED, PGDATA, Guard, sweep_orphans

TESTS = pathlib.Path(__file__).resolve().parent
RACINE = TESTS.parent


def _raison_de_passer() -> str | None:
    """Pourquoi ces bancs ne tournent pas ici — dit, jamais un vert muet."""
    if not hygiene.docker_available():
        return "docker absent : rien à prouver ici"
    if subprocess.run(["docker", "image", "inspect", IMAGE],
                      capture_output=True).returncode != 0:
        return (f"image {IMAGE} absente localement : la tirer ici ramènerait Docker Hub "
                "(limite par IP des runners, 09/10/2026) — `docker pull` sur un poste "
                "pour rejouer ces bancs")
    return None


_PASSER = _raison_de_passer()
needs_docker = pytest.mark.skipif(_PASSER is not None, reason=_PASSER or "")

# Le balai (`sweep_orphans`) retire TOUT conteneur étiqueté « lancé il y a plus de deux
# heures » — pas seulement celui du test qui l'appelle. Deux tests de ce fichier posent un
# factice « vieux » puis exigent qu'il soit là (ou parti) : sur deux workers en même temps,
# le balai de l'un retire le factice de l'autre (`CalledProcessError: docker inspect`,
# « balayage : … retiré »). Ressource partagée = un seul worker pour tout le fichier (#963).
# La course est ANTÉRIEURE à `loadgroup` : fichier lancé seul en `-n 4 --dist load`, elle
# rougit 3 fois sur 4 ; sur la suite entière, le découpage en tronçons la masquait le plus
# souvent (le job CI de #963 l'a fait apparaître).
pytestmark = pytest.mark.xdist_group(name="balai-docker")


# ── outillage ────────────────────────────────────────────────────────────────

def _factice(started: float | None, *extra: str) -> str:
    name = f"oto-test-factice-{uuid.uuid4().hex[:8]}"
    labels = ["--label", f"{LABEL}=1"]
    if started is not None:
        labels += ["--label", f"{LABEL_STARTED}={int(started)}"]
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, *labels, *extra, IMAGE, "sleep", "600"],
        capture_output=True, check=True)
    return name


def _rm(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


def _present(name: str) -> bool:
    out = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"name=^{name}$"],
        capture_output=True, text=True, check=True).stdout
    return bool(out.strip())


def _gone(name: str, within: float = 10.0) -> bool:
    """`docker rm -f` sur un conteneur `--rm` peut croiser l'auto-suppression du démon :
    on attend la disparition, pas le code retour."""
    deadline = time.time() + within
    while time.time() < deadline:
        if not _present(name):
            return True
        time.sleep(0.2)
    return False


def _volume_of(name: str) -> str:
    """Le volume anonyme d'un factice SANS tmpfs — celui que `rm -f` nu laisserait."""
    return _inspect(name)["Mounts"][0]["Name"]


def _volume_gone(vol: str, within: float = 10.0) -> bool:
    deadline = time.time() + within
    while time.time() < deadline:
        if subprocess.run(["docker", "volume", "inspect", vol], capture_output=True).returncode != 0:
            return True
        time.sleep(0.2)
    return False


def _inspect(name: str) -> dict:
    out = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]


# ── 1. le conteneur de la session : étiqueté, daté, sans volume ──────────────

@needs_docker
def test_conteneur_de_session_etiquete_date_sans_volume(pg_box):
    if pg_box.container is None:
        pytest.skip("OTO_TEST_PG_DSN : la base n'est pas notre conteneur")
    info = _inspect(pg_box.container)
    labels = info["Config"]["Labels"]
    assert labels[LABEL] == "1"
    assert 0 <= time.time() - int(labels[LABEL_STARTED]) < 3600
    assert info["Mounts"] == [], info["Mounts"]
    assert PGDATA in info["HostConfig"]["Tmpfs"]


@needs_docker
def test_temoin_sans_tmpfs_aurait_un_volume_anonyme():
    """L'assertion « Mounts vide » ne vaut que si l'image, sans tmpfs, EN CRÉE un."""
    name = _factice(time.time())
    try:
        mounts = _inspect(name)["Mounts"]
        assert [m["Type"] for m in mounts] == ["volume"]
        assert mounts[0]["Destination"] == PGDATA
    finally:
        _rm(name)


def test_run_args_sans_volume_et_avec_labels():
    args = hygiene.run_args("x", started=1_700_000_000)
    assert "--label" in args and f"{LABEL}=1" in args and f"{LABEL_STARTED}=1700000000" in args
    assert any(a.startswith(f"{PGDATA}:rw,size=") for a in args)
    assert "-v" not in args and "--volume" not in args and "--mount" not in args


# ── 2. la sortie : atexit, SIGTERM relayé, SIGINT relayé ─────────────────────

_ENFANT = """
import subprocess, sys, time
sys.path.insert(0, {tests!r})
from _pg_hygiene import Guard, IMAGE, LABEL, LABEL_STARTED
name = sys.argv[1]
subprocess.run(["docker", "run", "-d", "--rm", "--name", name,
                "--label", LABEL + "=1", "--label", LABEL_STARTED + "=" + str(int(time.time())),
                IMAGE, "sleep", "600"], check=True, capture_output=True)
Guard(name).install()
print("ready", flush=True)
if sys.stdin.readline().strip() == "exit":
    sys.exit(3)
time.sleep(60)
"""


def _enfant(tmp_path: pathlib.Path, name: str) -> subprocess.Popen:
    script = tmp_path / "enfant.py"
    script.write_text(_ENFANT.format(tests=str(TESTS)), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script), name],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    return p


@needs_docker
@pytest.mark.parametrize("sortie, attendu", [
    ("exit", (3,)),                                        # atexit
    ("SIGTERM", (-signal.SIGTERM,)),                       # relayé : meurt DE SIGTERM
    ("SIGINT", (-signal.SIGINT, 130)),                     # relayé : KeyboardInterrupt
])
def test_sortie_retire_le_conteneur_et_relaie_le_signal(tmp_path, sortie, attendu):
    name = f"oto-test-factice-{uuid.uuid4().hex[:8]}"
    p = _enfant(tmp_path, name)
    try:
        assert _present(name)
        if sortie == "exit":
            p.stdin.write("exit\n")
            p.stdin.flush()
        else:
            p.send_signal(getattr(signal, sortie))
        vol = _volume_of(name)
        rc = p.wait(timeout=30)
        assert rc in attendu, rc
        assert _gone(name)
        assert _volume_gone(vol)
    finally:
        if p.poll() is None:
            p.kill()
        _rm(name)


def test_guard_remove_est_idempotent(monkeypatch):
    appels: list[tuple] = []
    monkeypatch.setattr(hygiene, "_docker", lambda *a: appels.append(a))
    g = Guard("x")
    g.remove()
    g.remove()
    assert appels == [("rm", "-f", "-v", "x")]


# ── 3. le balai : la fonction, puis le vrai hook ─────────────────────────────

@needs_docker
def test_balai_retire_le_vieux_garde_le_recent_ignore_le_non_date():
    now = time.time()
    vieux = _factice(now - 3 * 3600)
    recent = _factice(now)
    non_date = _factice(None)
    try:
        vol = _volume_of(vieux)
        lines = sweep_orphans(now)
        assert [l for l in lines if vieux in l and l.endswith("retiré")], lines
        assert not [l for l in lines if recent in l or non_date in l], lines
        assert _gone(vieux)
        assert _volume_gone(vol)
        assert _present(recent) and _present(non_date)
    finally:
        _rm(vieux)
        _rm(recent)
        _rm(non_date)


@needs_docker
def test_le_balai_tourne_au_debut_de_session_et_le_dit():
    vieux = _factice(time.time() - 3 * 3600)
    try:
        vol = _volume_of(vieux)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider",
             str(TESTS / "test_no_silent_except.py")],
            cwd=RACINE, capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        dites = [l for l in r.stdout.splitlines() if vieux in l]
        assert dites and dites[0].startswith("#640 balayage") and dites[0].endswith("retiré"), r.stdout
        assert _gone(vieux)
        assert _volume_gone(vol)
    finally:
        _rm(vieux)


def test_sans_docker_le_balai_est_inerte(monkeypatch):
    def absent(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(hygiene.subprocess, "run", absent)
    assert sweep_orphans(time.time()) == []
    assert hygiene.docker_available() is False
