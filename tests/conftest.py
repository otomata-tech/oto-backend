"""Fixtures partagées.

`pg_dsn` — un PostgreSQL RÉEL, pour les rares tests qui n'ont de valeur que là :
une contrainte (la PK que viole un renommage naïf, #295) ou un opérateur JSONB
(`data - key`, qui efface là où `null` conserve, #296) ne s'exerce pas contre un
stub. Le reste de la suite reste sans base — la convention du repo est de tester
la logique pure et les gardes par stub, le chemin SQL étant vérifié au déploiement.

Source, dans l'ordre : `OTO_TEST_PG_DSN`, sinon un conteneur jetable si `docker`
répond, sinon `skip`. Session-scopé : un seul conteneur pour toute la suite.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest

# ⚠️ **pgvector, et pas `postgres:17-alpine`** : `init_db()` fait
# `CREATE EXTENSION vector` avant `_SCHEMA` (des tables de `_SCHEMA` déclarent des
# `halfvec`), donc une image sans l'extension rend le VRAI boot intestable — et un
# test de migration qui ne peut pas jouer `init_db` ne prouve rien de la migration.
# Image officielle pgvector = PostgreSQL 17 standard + l'extension, `pg_trgm` inclus.
_IMAGE = "pgvector/pgvector:pg17"


@pytest.fixture(scope="session")
def pg_dsn():
    dsn = os.environ.get("OTO_TEST_PG_DSN")
    if dsn:
        yield dsn
        return
    try:
        joignable = subprocess.run(["docker", "info"],
                                   capture_output=True).returncode == 0
    except (FileNotFoundError, OSError):
        # `docker` PAS INSTALLÉ : `subprocess.run` lève au lieu de rendre un code.
        # Sans ce cas, la fixture explose et les tests PG remontent en ERROR au lieu
        # de SKIP — indiscernables d'une vraie casse dans un diff de suite, ce qui
        # est précisément ce qu'un test « qui se saute proprement » promettait.
        joignable = False
    if not joignable:
        pytest.skip("aucun PostgreSQL joignable (ni OTO_TEST_PG_DSN, ni docker)")
    name = f"oto-test-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-e", "POSTGRES_PASSWORD=test", "-P", _IMAGE],
        capture_output=True, check=True)
    try:
        port = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            capture_output=True, text=True, check=True).stdout.strip().rsplit(":", 1)[1]
        dsn = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
        # L'attente se fait avec L'INSTRUMENT DU TEST — une vraie connexion depuis
        # l'hôte. `pg_isready` dans le conteneur répond OK pendant la phase d'INIT
        # de l'image postgres (serveur temporaire, socket locale), puis le serveur
        # redémarre : les premiers tests tombaient alors sur « server closed the
        # connection unexpectedly ». Un sondage qui n'emprunte pas le chemin du test
        # ne prouve pas que le chemin du test est prêt.
        psycopg = pytest.importorskip("psycopg")
        deadline = time.time() + 60
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3) as c:
                    c.execute("SELECT 1")
                break
            except Exception:
                if time.time() > deadline:
                    pytest.skip("le PostgreSQL jetable n'est pas devenu prêt")
                time.sleep(1)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
