"""Le renommage `http` → `api` passe dans les deux ordres du code et de la base.

La révision `20261112_1200_connecteur_api` repointe ce qui désigne une instance `http` (règle
d'accès d'un compte, arête `grants`, lien de projet) vers sa copie `api`, que la copie du
coffre fait naître — une copie re-chiffrée, qui exige la clé maîtresse, donc une étape du
code (`oto-mcp maintenance copies-du-coffre`, jouée aussi au démarrage). Sans copie, la
révision lève et n'écrit rien.

- Notre instance DÉMARRE puis MIGRE : la copie du démarrage précède la révision.
- Une instance cible MIGRE AVANT DE DÉMARRER (`deploy/cible/migrations_a_jour.py`) : la
  porte joue la copie avant `migrer upgrade head`. Sans elle, la montée refusait — la
  révision levait sur la première règle d'un compte `http`.

Joué sur une vraie base (une par cas), estampillée à la révision d'avant, la clé maîtresse
posée. La porte est la vraie (`migrer_la_base`) ; seules ses commandes `oto-mcp` sont jouées
dans ce processus plutôt que dans un sous-processus, la migration s'arrêtant à la révision
étudiée (le schéma de la base est déjà celui de la tête).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261111_1930_dataforseo_decrit"
_REVISION = "20261112_1200_connecteur_api"
_CLE = base64.b64encode(b"\x31" * 32).decode()
ORG, PROJET, SUB = 97001, 97001, "usr_renommage"

_spec = importlib.util.spec_from_file_location(
    "migrations_a_jour_renommage", RACINE / "deploy" / "cible" / "migrations_a_jour.py")
garde = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(garde)


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


@pytest.fixture
def base(pg_dsn):
    """Une base neuve à la révision d'avant, la clé maîtresse posée, un compte `http` de
    l'org dans le coffre — et ce qui désigne son instance : une règle d'accès, une arête
    `grants`, un lien de projet épinglé. Aucune copie `api` : rien n'a encore démarré."""
    psycopg = pytest.importorskip("psycopg")
    from alembic import command

    from oto_mcp.db import _conn as dbconn
    nom = "oto_renommage_" + uuid.uuid4().hex[:8]
    racine = psycopg.connect(pg_dsn, autocommit=True)
    racine.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    avant = {k: os.environ.get(k) for k in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    avant_pool = dbconn._pool
    os.environ["DATABASE_URL"], os.environ["OTO_MCP_MASTER_KEY"] = dsn, _CLE
    dbconn._pool = None
    try:
        from oto_mcp import credentials_store as cs
        from oto_mcp.db import init_db
        from oto_mcp.db._conn import _connect
        init_db()
        command.stamp(_alembic(), _PRECEDENTE)
        with _connect() as c:
            c.execute("INSERT INTO orgs (id, name) VALUES (%s, 'o')", (ORG,))
            c.execute("INSERT INTO projects (id, owner_id, name) VALUES (%s, %s, 'p')",
                      (PROJET, SUB))
            cs._upsert(c, "org", str(ORG), "http", "paie", "jeton", SUB, {"label": "Paie"})
            iid = c.execute("SELECT id FROM connector_instances WHERE connector = 'http' "
                            "AND owner_id = %s AND account = 'paie'", (str(ORG),)
                            ).fetchone()["id"]
            c.execute("INSERT INTO connector_account_rules (org_id, instance_id, subject_org, "
                      "level) VALUES (%s, %s, true, 'write')", (ORG, iid))
            c.execute("INSERT INTO grants (resource_id, grantor_kind, grantor_id, "
                      "grantee_kind, grantee_id) VALUES (%s, 'org', %s, 'group', '7')",
                      (f"inst:{iid}", str(ORG)))
            c.execute("INSERT INTO project_links (project_id, target_type, target_ref, "
                      "config) VALUES (%s, 'connecteur', 'http', %s::jsonb)",
                      (PROJET, json.dumps({"instance_ref": f"inst:{iid}"})))
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in avant.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        racine.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        racine.close()


def _lire(sql, *args):
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        return [tuple(r.values()) for r in c.execute(sql, args).fetchall()]


def _instance(connector):
    [(iid,)] = _lire("SELECT id FROM connector_instances WHERE connector = %s "
                     "AND owner_id = %s AND account = 'paie' AND revoked_at IS NULL",
                     connector, str(ORG))
    return iid


def _tout_designe_la_copie():
    """Chaque désignation est repointée vers la copie `api`, aucune n'est perdue ; la
    ligne `http` du coffre reste (copie additive)."""
    api = _instance("api")
    assert _lire("SELECT instance_id, level FROM connector_account_rules "
                 "WHERE org_id = %s", ORG) == [(api, "write")]
    assert _lire("SELECT resource_id FROM grants WHERE grantor_id = %s",
                 str(ORG)) == [(f"inst:{api}",)]
    assert _lire("SELECT target_ref, config FROM project_links WHERE project_id = %s",
                 PROJET) == [("api", {"instance_ref": f"inst:{api}"})]
    assert _instance("http") != api


def _commandes_dans_ce_processus(monkeypatch):
    """Les commandes `oto-mcp` de la porte, jouées ici : `maintenance` par son point
    d'entrée, `migrer upgrade head` jusqu'à la révision étudiée."""
    import subprocess

    from alembic import command

    from oto_mcp import maintenance
    jouees = []

    def lancer(commande, timeout, env):
        arguments = commande[1:]
        jouees.append(arguments[0])
        if arguments[0] == "maintenance":
            return subprocess.CompletedProcess(commande, maintenance.main(arguments[1:]))
        assert arguments == ["migrer", "upgrade", "head"]
        command.upgrade(_alembic(), _REVISION)
        return subprocess.CompletedProcess(commande, 0)
    monkeypatch.setattr(garde.subprocess, "run", lancer)
    return jouees


def test_migrer_avant_de_demarrer_par_la_porte_d_une_cible(base, monkeypatch):
    """Le cas de la cible : la porte migre AVANT que le code neuf ait jamais démarré.
    Elle copie le coffre d'abord : la révision trouve la copie, et repointe."""
    jouees = _commandes_dans_ce_processus(monkeypatch)
    garde.migrer_la_base()
    assert jouees == ["maintenance", "migrer"]
    _tout_designe_la_copie()


def test_demarrer_puis_migrer(base):
    """Le cas de notre instance : le démarrage copie le coffre (la liste que la porte
    joue aussi), la révision passe ensuite."""
    from alembic import command

    from oto_mcp import maintenance
    for _, copier in maintenance.copies_declarees():
        copier()
    command.upgrade(_alembic(), _REVISION)
    _tout_designe_la_copie()


def test_sans_copie_la_revision_leve_en_nommant_le_geste_et_n_ecrit_rien(base):
    """Une désignation orpheline ne se saute pas : la révision lève, dit quoi jouer, et
    la transaction annulée laisse la règle sur l'instance `http`."""
    from alembic import command
    http = _instance("http")
    with pytest.raises(Exception, match="oto-mcp maintenance copies-du-coffre"):
        command.upgrade(_alembic(), _REVISION)
    assert _lire("SELECT instance_id FROM connector_account_rules WHERE org_id = %s",
                 ORG) == [(http,)]
    assert _lire("SELECT target_ref FROM project_links WHERE project_id = %s",
                 PROJET) == [("http",)]


def test_les_copies_refusent_sans_cle_maitresse(monkeypatch, capsys):
    """Sans clé maîtresse, rien ne se déchiffre : la commande refuse, au lieu de compter
    chaque ligne « indéchiffrable » et de rendre la main à la migration."""
    from oto_mcp import maintenance
    monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    assert maintenance.main(["copies-du-coffre"]) == maintenance.COPIES_ERREUR
    assert "OTO_MCP_MASTER_KEY absente" in capsys.readouterr().err
