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
    app = db.apps.create(("user", MOI), None, project_id=projet, title="Leads", manifest={"tables": {}}, html="<p>1</p>",
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
    app = db.apps.create(("user", MOI), None, project_id=projet, title="C", manifest={}, html="a", thumbnail=None, by=MOI)
    with pytest.raises(db.apps.AppConflict) as e:
        db.apps.update(app["id"], by=MOI, expected_rev="0" * 16, html="b")
    assert e.value.current_rev == app["rev"]
    assert db.apps.get(app["id"])["html"] == "a"
    assert db.apps.revisions(app["id"]) == []


def test_revision_d_une_autre_app_illisible(projet):
    a = db.apps.create(("user", MOI), None, project_id=projet, title="A", manifest={}, html="a", thumbnail=None, by=MOI)
    b = db.apps.create(("user", MOI), None, project_id=projet, title="B", manifest={}, html="b", thumbnail=None, by=MOI)
    db.apps.update(a["id"], by=MOI, html="a2")
    (rev,) = db.apps.revisions(a["id"])
    assert db.apps.revision(b["id"], rev["id"]) is None


def test_liste_cartes_sans_html_et_archives(projet):
    app = db.apps.create(("user", MOI), None, project_id=projet, title="L", manifest={}, html="x" * 100, thumbnail=None, by=MOI)
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
    app = db.apps.create(("user", MOI), None, project_id=projet, title="R", manifest={}, html="0", thumbnail=None, by=MOI)
    for i in range(1, 6):
        db.apps.update(app["id"], by=MOI, html=str(i))
    gardees = db.apps.revisions(app["id"], limit=100)
    assert [db.apps.revision(app["id"], r["id"])["html"] for r in gardees] == ["4", "3", "2"]


def test_proprietaire_partages_et_rangement(projet):
    """Une app à elle : listée par son propriétaire, partagée avec la meilleure
    permission, rangée dans un projet sans que cela donne quoi que ce soit."""
    from oto_mcp import ownership

    autre = "sub-apps-test-autre"
    a = db.apps.create(("user", MOI), None, title="Mine", manifest={}, html="", thumbnail=None, by=MOI)
    b = db.apps.create(("user", autre), None, title="Theirs", manifest={}, html="",
                       thumbnail=None, by=autre)
    assert a["id"] in {r["id"] for r in db.apps.list_for_owners([("user", MOI)])}
    ownership.grant("app", str(b["id"]), "user", MOI, role="viewer")
    ownership.grant("app", str(b["id"]), "group", "999999", role="editor")
    vues = {r["id"]: r["permission"] for r in
            db.apps.list_granted_to([("user", MOI), ("group", "999999")])}
    assert vues[b["id"]] == "write"
    assert db.apps.owner(b["id"])["owner_id"] == autre
    db.apps.file(b["id"], projet, MOI)
    assert db.apps.projects_of([b["id"]]) == {b["id"]: [projet]}
    assert b["id"] in {r["id"] for r in db.apps.list_for_projects([projet])}
    assert db.apps.unfile(b["id"], projet) is True
    assert db.apps.projects_of([b["id"]]) == {b["id"]: []}


_PLAFONDS = {"max_app_bytes": 10 ** 9, "max_viewer_bytes": 5 * 10 ** 9}


def test_les_fichiers_de_plus_de_30_jours_sont_purges(projet, monkeypatch):
    from oto_mcp import maintenance, media_store
    from oto_mcp.db._conn import _connect

    app = db.apps.create(("user", MOI), None, title="F", manifest={"files": True}, html="",
                         thumbnail=None, by=MOI)
    vieux = db.apps.file_add(app["id"], MOI, "app-files/x/vieux.pdf", "vieux.pdf", "application/pdf", 3,
                             **_PLAFONDS)
    neuf = db.apps.file_add(app["id"], MOI, "app-files/x/neuf.pdf", "neuf.pdf", "application/pdf", 3,
                            **_PLAFONDS)
    with _connect() as conn:
        conn.execute("UPDATE app_files SET created_at = now() - interval '31 days' WHERE id = %s",
                     (vieux["id"],))
    supprimes = []
    monkeypatch.setattr(media_store, "delete_by_key", supprimes.append)
    assert maintenance.app_files(dry_run=True)["purgeables"] == 1
    assert maintenance.app_files()["purged"] == 1
    assert supprimes == ["app-files/x/vieux.pdf"]
    assert db.apps.file_of(app["id"], MOI, vieux["id"]) is None
    assert db.apps.file_of(app["id"], MOI, neuf["id"]) is not None
    assert db.apps.bytes_of(app["id"], MOI) == 3


def test_le_plafond_se_juge_a_l_insertion(projet):
    """Compte et insertion dans la même transaction : au-delà du plafond, rien n'est
    inséré — par app, et toutes apps confondues."""
    sub = "sub-apps-plafond"
    a = db.apps.create(("user", sub), None, title="A", manifest={"files": True}, html="",
                       thumbnail=None, by=sub)
    b = db.apps.create(("user", sub), None, title="B", manifest={"files": True}, html="",
                       thumbnail=None, by=sub)
    db.apps.file_add(a["id"], sub, "app-files/p/1", "1.pdf", "application/pdf", 6,
                     max_app_bytes=10, max_viewer_bytes=15)
    with pytest.raises(db.apps.StorageFull) as e:
        db.apps.file_add(a["id"], sub, "app-files/p/2", "2.pdf", "application/pdf", 5,
                         max_app_bytes=10, max_viewer_bytes=15)
    assert e.value.scope == "app"
    db.apps.file_add(b["id"], sub, "app-files/p/3", "3.pdf", "application/pdf", 8,
                     max_app_bytes=10, max_viewer_bytes=15)
    with pytest.raises(db.apps.StorageFull) as e:
        db.apps.file_add(b["id"], sub, "app-files/p/4", "4.pdf", "application/pdf", 2,
                         max_app_bytes=10, max_viewer_bytes=15)
    assert e.value.scope == "viewer"
    assert db.apps.bytes_of(a["id"], sub) == 6 and db.apps.bytes_of_viewer(sub) == 14


def test_supprimer_une_app_ne_laisse_pas_d_objet_orphelin(projet, monkeypatch):
    """`ON DELETE SET NULL` : la ligne survit à son app, invisible, et la purge suivante
    retire son objet puis la ligne — sans attendre 30 jours."""
    from oto_mcp import maintenance, media_store
    from oto_mcp.db._conn import _connect

    app = db.apps.create(("user", MOI), None, title="G", manifest={"files": True}, html="",
                         thumbnail=None, by=MOI)
    f = db.apps.file_add(app["id"], MOI, "app-files/y/recent.pdf", "recent.pdf",
                         "application/pdf", 3, **_PLAFONDS)
    with _connect() as conn:
        conn.execute("DELETE FROM apps WHERE id = %s", (app["id"],))
    assert db.apps.file_of(app["id"], MOI, f["id"]) is None
    supprimes = []
    monkeypatch.setattr(media_store, "delete_by_key", supprimes.append)
    assert maintenance.app_files()["purged"] >= 1
    assert "app-files/y/recent.pdf" in supprimes
    with _connect() as conn:
        assert conn.execute("SELECT 1 FROM app_files WHERE id = %s", (f["id"],)).fetchone() is None


def test_la_chaine_des_revisions_d_apps_suit_les_pieces_du_copilot():
    """Les révisions de la série d'apps se suivent, à partir de la tête d'avant
    (`20261029_1700_copilot_pieces`) — une seule tête (`test_migrations_registre`)."""
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(_alembic())
    attendu = {"20261029_1800_app_consents": "20261029_1700_copilot_pieces",
               "20261029_1900_apps_owned": "20261029_1800_app_consents",
               "20261029_2000_app_files": "20261029_1900_apps_owned",
               "20261029_2100_app_files_origin": "20261029_2000_app_files"}
    assert {r: script.get_revision(r).down_revision for r in attendu} == attendu
    assert len(script.get_heads()) == 1


def test_l_origine_d_un_fichier_s_ajoute_sans_reecrire_le_fragment_joue():
    """`origin` arrive par une colonne AJOUTÉE (révision et démarrage), pas en réécrivant
    le `CREATE TABLE` de `FICHIERS` — déjà joué là où elle manque."""
    from alembic.script import ScriptDirectory

    from oto_mcp.db import _schema
    from oto_mcp.db.schema import apps as schema_apps
    assert "origin" not in schema_apps.FICHIERS
    assert "ADD COLUMN IF NOT EXISTS origin" in schema_apps.FICHIERS_ORIGINE
    assert _schema.ASSEMBLAGE.index(schema_apps.FICHIERS_ORIGINE) == \
        _schema.ASSEMBLAGE.index(schema_apps.FICHIERS) + 1
    rev = ScriptDirectory.from_config(_alembic()).get_revision(
        "20261029_2100_app_files_origin").module
    assert rev.FICHIERS_ORIGINE is schema_apps.FICHIERS_ORIGINE
    # Une app supprimée laisse ses fichiers à la purge, jamais d'orphelin.
    assert "ON DELETE SET NULL" in schema_apps.FICHIERS
