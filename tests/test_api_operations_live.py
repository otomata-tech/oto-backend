"""Les opérations d'un « Custom API » dans `connector_instances.config`, contre un VRAI
PostgreSQL : la clé de config posée sans écraser les autres, ce que deviennent les
opérations quand la clé TOURNE, quand le compte est RENOMMÉ, quand la clé est RETIRÉE.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import base64
import os
import uuid

import pytest

ORG = 1
SUB = "usr_custom_api"
_KEY = base64.b64encode(b"\x22" * 32).decode()
OP = {"name": "list_teams", "description": "List the teams.", "method": "GET",
      "path": "/teams", "input": {"type": "object", "properties": {}}}


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_customapi_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    avant = {k: os.environ.get(k) for k in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    avant_pool = dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + name
    os.environ["OTO_MCP_MASTER_KEY"] = _KEY
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in avant.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _poser(account: str, token: str = "T1"):
    from oto_mcp import credentials_store as cs
    secret = cs.pack_secret("api", {"base_url": "https://api.acme.test",
                                     "auth_mode": "bearer", "token": token})
    cs.set_credential("org", str(ORG), "api", secret, set_by=SUB, account=account)


def test_poser_lire_et_les_autres_cles_de_config_survivent(live):
    from oto_mcp.connectors import api_operations as store
    from oto_mcp.db import connector_instances as ci
    from oto_mcp.db._conn import _connect
    _poser("paie")
    inst = ci.live_instance_config("org", str(ORG), "api", "paie")
    assert inst is not None and store.load("org", str(ORG), "paie") == []
    with _connect() as c:
        c.execute("UPDATE connector_instances SET config = '{\"autre\": 1}'::jsonb "
                  "WHERE id = %s", (inst["id"],))
    assert store.save("org", str(ORG), "paie", [OP]) == [OP]
    assert store.load("org", str(ORG), "paie") == [OP]
    assert ci.live_instance_config("org", str(ORG), "api", "paie")["config"]["autre"] == 1


def test_la_rotation_de_la_cle_garde_les_operations(live):
    from oto_mcp.connectors import api_operations as store
    _poser("rot")
    store.save("org", str(ORG), "rot", [OP])
    _poser("rot", token="T2")
    assert store.load("org", str(ORG), "rot") == [OP]


def test_le_renommage_du_compte_les_fait_suivre(live):
    from oto_mcp import credentials_store as cs
    from oto_mcp.connectors import api_operations as store
    _poser("avant")
    store.save("org", str(ORG), "avant", [OP])
    cs.rename_account("org", str(ORG), "api", "avant", "apres")
    assert store.load("org", str(ORG), "apres") == [OP]


def test_le_retrait_de_la_cle_archive_l_instance_et_ses_operations(live):
    from oto_mcp import credentials_store as cs
    from oto_mcp.connectors import api_operations as store
    _poser("retrait")
    store.save("org", str(ORG), "retrait", [OP])
    cs.clear_credential("org", str(ORG), "api", account="retrait")
    with pytest.raises(store.InstanceMissing):
        store.load("org", str(ORG), "retrait")
    with pytest.raises(store.InstanceMissing):
        store.save("org", str(ORG), "retrait", [OP])


def test_la_definition_et_la_liste_de_l_org_sans_dechiffrer(live):
    """Le nom affiché vit en clair dans `meta` (`in_meta`) : la liste des connecteurs
    personnalisés de l'org le lit sans déchiffrer ; une pose partielle de la définition
    garde le reste."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.connectors import api_operations as store
    from oto_mcp.db import connector_instances as ci
    secret, meta = cs.preparer_pose("org", str(ORG), "api", "malibou", fields={
        "base_url": "https://api.acme.test", "auth_mode": "bearer", "token": "T",
        "label": "Acme Payroll"})
    assert meta == {"label": "Acme Payroll"} and "Acme Payroll" not in secret
    cs.set_credential("org", str(ORG), "api", secret, set_by=SUB, account="malibou",
                      meta=meta)
    store.save_definition("org", str(ORG), "malibou", {"operations": [OP]})
    d = store.save_definition("org", str(ORG), "malibou",
                              {"description": "Payroll.", "headers": {"X-V": "2"}})
    assert d["operations"] == [OP] and d["headers"] == {"X-V": "2"}
    [c] = [c for c in store.org_connectors(ORG, SUB) if c["account"] == "malibou"]
    assert (c["label"], c["description"], c["actions_count"]) == \
        ("Acme Payroll", "Payroll.", 1)
    iid = ci.live_instance_config("org", str(ORG), "api", "malibou")["id"]
    assert ci.configs_for_instance_ids([iid])[iid]["description"] == "Payroll."


def test_le_coffre_de_l_ancien_nom_est_copie_avec_sa_definition(live):
    """Le connecteur s'appelait `http` : au démarrage, ses lignes de coffre sont COPIÉES
    sous `api` (re-chiffrées, l'AAD porte le nom), avec leur partage ; l'instance neuve
    reprend la définition de l'ancienne, et la ligne `http` reste. Rejouée, la copie ne
    refait rien."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.connectors import api_operations as store
    from oto_mcp.db import connector_instances as ci
    from oto_mcp.db._conn import _connect
    secret = cs.pack_secret("api", {"base_url": "https://api.acme.test",
                                     "auth_mode": "bearer", "token": "T-ancien"})
    with _connect() as c:
        cs._upsert(c, "org", str(ORG), "http", "ancienne", secret, SUB, {"label": "Paie"})
        c.execute("UPDATE connector_instances SET config = %s::jsonb, label = 'Paie' "
                  "WHERE owner_type = 'org' AND owner_id = %s AND connector = 'http' "
                  "AND account = 'ancienne' AND revoked_at IS NULL",
                  ('{"operations": [' + __import__("json").dumps(OP) + '], '
                   '"description": "la paie"}', str(ORG)))
        # Un prêt posé sur le compte `http` (le partage lu aujourd'hui).
        c.execute("UPDATE connector_credentials SET share_side = %s::jsonb "
                  "WHERE entity_type = 'org' AND entity_id = %s AND connector = 'http' "
                  "AND account = 'ancienne'", ('[{"sub": "usr_emprunteur"}]', str(ORG)))
    assert store.copier_depuis_http()["copied"] == 1
    assert cs.get_instance_sharing("org", str(ORG), "api", "ancienne") == (
        [], [{"sub": "usr_emprunteur"}]), "le partage suit la copie"
    assert store.load("org", str(ORG), "ancienne") == [OP]
    assert store.load_definition("org", str(ORG), "ancienne")["description"] == "la paie"
    assert ci.live_instance_config("org", str(ORG), "http", "ancienne") is not None
    assert cs.get_credential("org", str(ORG), "api", account="ancienne") == secret
    assert store.copier_depuis_http() == {"copied": 0, "present": 0, "unreadable": 0}
