"""`db.apps` et la révision `20261026_1256_apps`, joués sur une vraie base.

Ce qu'on vérifie, c'est ce que le STOCKAGE porte : l'ETag de contenu, la révision
écrite AVANT chaque mise à jour, le conflit qui n'écrit rien, la liste sans HTML,
la révision d'une autre app qui ne se lit pas, la rétention des révisions, et la
migration aller-retour.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from oto_mcp import db

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261024_1101_default_sharing"
_REVISION = "20261026_1256_apps"
MOI = "sub-apps-test"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


@pytest.fixture(scope="module")
def projet(live):
    return int(db.create_project("user", MOI, "Apps", created_by=MOI))


def test_creer_lire_mettre_a_jour_avec_revision(projet):
    app = db.apps.create(projet, title="Leads", manifest={"tables": {}}, html="<p>1</p>",
                         thumbnail=None, by=MOI)
    assert app["rev"] == db.apps.app_rev("Leads", {"tables": {}}, "<p>1</p>", None)
    lu = db.apps.get(app["id"])
    assert lu["rev"] == app["rev"] and lu["html"] == "<p>1</p>"

    v2 = db.apps.update(app["id"], by="autre", expected_rev=app["rev"], html="<p>2</p>",
                        thumbnail={"color": "#123", "emoji": "x"})
    assert v2["html"] == "<p>2</p>" and v2["updated_by"] == "autre"
    assert v2["thumbnail"] == {"color": "#123", "emoji": "x"}

    # L'état d'AVANT est dans les révisions, avec son auteur.
    (rev,) = db.apps.revisions(app["id"])
    old = db.apps.revision(app["id"], rev["id"])
    assert old["html"] == "<p>1</p>" and old["edited_by"] == MOI

    # Vignette omise : gardée ; None : effacée.
    v3 = db.apps.update(app["id"], by=MOI, title="Leads 2")
    assert v3["thumbnail"] == {"color": "#123", "emoji": "x"}
    v4 = db.apps.update(app["id"], by=MOI, thumbnail=None)
    assert v4["thumbnail"] is None


def test_conflit_n_ecrit_rien(projet):
    app = db.apps.create(projet, title="C", manifest={}, html="a", thumbnail=None, by=MOI)
    with pytest.raises(db.apps.AppConflict) as e:
        db.apps.update(app["id"], by=MOI, expected_rev="0" * 16, html="b")
    assert e.value.current_rev == app["rev"]
    assert db.apps.get(app["id"])["html"] == "a"
    assert db.apps.revisions(app["id"]) == []


def test_revision_d_une_autre_app_illisible(projet):
    a = db.apps.create(projet, title="A", manifest={}, html="a", thumbnail=None, by=MOI)
    b = db.apps.create(projet, title="B", manifest={}, html="b", thumbnail=None, by=MOI)
    db.apps.update(a["id"], by=MOI, html="a2")
    (rev,) = db.apps.revisions(a["id"])
    assert db.apps.revision(b["id"], rev["id"]) is None


def test_liste_cartes_sans_html_et_archives(projet):
    app = db.apps.create(projet, title="L", manifest={}, html="x" * 100, thumbnail=None, by=MOI)
    cartes = db.apps.list_for_projects([projet])
    assert app["id"] in {c["id"] for c in cartes}
    assert all("html" not in c and "manifest" not in c for c in cartes)
    db.apps.set_archived(app["id"], True)
    assert app["id"] not in {c["id"] for c in db.apps.list_for_projects([projet])}
    assert app["id"] in {c["id"] for c in db.apps.list_for_projects([projet], archived=True)}
    assert db.apps.project_owners([projet, 999999]) == {str(projet): ("user", MOI)}


def test_migration_aller_retour(live, pg_module_dsn):
    import psycopg
    from alembic import command

    def tables():
        with psycopg.connect(pg_module_dsn) as c:
            return {r[0] for r in c.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('apps', 'app_revisions')").fetchall()}

    # Depuis la TÊTE : des tables nées après (`app_consents`…) pointent `apps`, et le
    # démarrage les a posées — descendre la seule révision des apps ne pourrait pas
    # retirer une table dont d'autres dépendent.
    command.stamp(_alembic(), "head")
    command.downgrade(_alembic(), _PRECEDENTE)
    assert tables() == set()
    command.upgrade(_alembic(), "head")
    assert tables() == {"apps", "app_revisions"}
    # Rejouée sur une base que le démarrage a servie : rien à faire.
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), "head")
    assert tables() == {"apps", "app_revisions"}


def test_seules_les_dernieres_revisions_sont_gardees(projet, monkeypatch):
    """Un agent qui réécrit son app en boucle ne remplit pas la base : chaque mise à
    jour purge, dans sa transaction, ce qui dépasse `MAX_REVISIONS`."""
    monkeypatch.setattr(db.apps, "MAX_REVISIONS", 3)
    app = db.apps.create(projet, title="R", manifest={}, html="0", thumbnail=None, by=MOI)
    for i in range(1, 6):
        db.apps.update(app["id"], by=MOI, html=str(i))
    gardees = db.apps.revisions(app["id"], limit=100)
    assert [db.apps.revision(app["id"], r["id"])["html"] for r in gardees] == ["4", "3", "2"]
