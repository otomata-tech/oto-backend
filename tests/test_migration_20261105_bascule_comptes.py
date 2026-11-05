"""La révision `20261105_1000_bascule_comptes` : reprise rejouée, PUIS anciens partages retirés.

Joué sur une vraie base bootée (schéma de la tête), ramenée à l'état d'avant la bascule
par le retour arrière de la révision (structures recréées vides), garnie d'anciens
partages : la montée reprend chaque partage en règle AVANT de retirer tables et colonnes
(une règle reprise ne peut naître que de ce qu'elle a lu) ; la descente recrée les
structures vides ; la remontée redonne le même état, sans perdre une règle.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261105_0900_regles_de_comptes"
_REVISION = "20261105_1000_bascule_comptes"
_ANCIENNES_TABLES = ["connector_account_grants", "connector_account_group_grants",
                     "unipile_operated_accounts"]


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _un(dsn, sql, params=()):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, params).fetchall()


def _tables(dsn):
    return [r[0] for r in _un(dsn, "SELECT relname FROM pg_class WHERE relname = ANY(%s) "
                                   "AND relkind = 'r' ORDER BY 1", (_ANCIENNES_TABLES,))]


def _colonnes(dsn):
    return sorted(f"{r[0]}.{r[1]}" for r in _un(
        dsn, "SELECT table_name, column_name FROM information_schema.columns "
             "WHERE table_schema = current_schema() AND ((table_name = "
             "'connector_credentials' AND column_name IN ('share_side', 'share_services', "
             "'share_mode', 'share_down')) OR (table_name = 'connector_instances' "
             "AND column_name = 'visibility'))"))


def _regles(dsn):
    return _un(dsn, "SELECT i.connector, r.subject_user, r.subject_group, r.subject_org, "
                    "r.level, r.services, r.origine FROM connector_account_rules r "
                    "JOIN connector_instances i ON i.id = r.instance_id ORDER BY 1, 2, 3, 4")


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    """Une base bootée à la tête, ramenée avant la bascule, garnie d'anciens partages."""
    import psycopg
    from alembic import command
    from oto_mcp import db, org_store

    org = org_store.create_org("org_bascule")
    autre = org_store.create_org("org_tierce")
    for sub in ("proprio", "pair", "dehors"):
        db.upsert_user(sub)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        for o, sub in ((org, "proprio"), (org, "pair"), (autre, "dehors")):
            c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (o, sub))
        equipe = c.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, 'ventes') "
                           "RETURNING id", (org,)).fetchone()[0]
    command.stamp(_alembic(), _REVISION)
    command.downgrade(_alembic(), _PRECEDENTE)      # recrée les structures, vides
    db.set_unipile_account("proprio", "ACC", None, org_id=org, provider="LINKEDIN")
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        # Un prêt Google d'une personne à une autre, limité à Gmail, et un prêt entre orgs.
        c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                  "account, secret_enc, share_side, share_services) "
                  "VALUES ('member', %s, 'google', 'a@x.test', 'x', %s, %s)",
                  (f"{org}:proprio", json.dumps(["user:pair", "user:dehors"]),
                   json.dumps({"user:pair": ["gmail"]})))
        c.execute("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
                  "VALUES ('google', 'member', %s, 'a@x.test'), ('attio', 'org', %s, '')",
                  (f"{org}:proprio", str(org)))
        # Un prêt Unipile à une personne et à une équipe, et un pointeur.
        c.execute("INSERT INTO connector_account_grants (owner_sub, provider, account_id, "
                  "grantee_sub, granted_by) VALUES ('proprio', 'LINKEDIN', 'ACC', 'pair', "
                  "'proprio'), ('proprio', 'LINKEDIN', 'ACC', 'dehors', 'proprio')")
        # Une équipe d'une autre org : jamais une règle hors org (personnes seulement).
        tierce = c.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, 'dehors') "
                           "RETURNING id", (autre,)).fetchone()[0]
        c.execute("INSERT INTO connector_account_group_grants (owner_sub, provider, "
                  "account_id, grantee_group_id, granted_by) VALUES ('proprio', 'LINKEDIN', "
                  "'ACC', %s, 'proprio')", (tierce,))
        c.execute("INSERT INTO connector_account_group_grants (owner_sub, provider, "
                  "account_id, grantee_group_id, granted_by) VALUES ('proprio', 'LINKEDIN', "
                  "'ACC', %s, 'proprio')", (equipe,))
        c.execute("INSERT INTO unipile_operated_accounts (sub, provider, account_id, "
                  "owner_sub) VALUES ('pair', 'LINKEDIN', 'ACC', 'proprio')")
    return {"dsn": pg_module_dsn, "org": org, "equipe": equipe}


def test_montee_descente_montee(base):
    from alembic import command
    dsn, equipe = base["dsn"], base["equipe"]
    assert _tables(dsn) == sorted(_ANCIENNES_TABLES)
    assert "connector_credentials.share_side" in _colonnes(dsn)

    command.upgrade(_alembic(), _REVISION)
    # La reprise a tourné AVANT le retrait : chaque partage d'avant est une règle.
    attendues = [
        ("attio", None, None, True, "sensitive", None, "reprise"),         # compte d'org
        ("google", "dehors", None, False, "sensitive", [], "reprise"),     # hors org, D1
        ("google", "pair", None, False, "sensitive", ["gmail"], "reprise"),  # prêt limité
        ("unipile", "dehors", None, False, "sensitive", None, "reprise"),  # hors org, D1
        ("unipile", "pair", None, False, "sensitive", None, "reprise"),
        ("unipile", None, equipe, False, "sensitive", None, "reprise"),
    ]
    assert _regles(dsn) == attendues   # l'équipe d'une autre org n'est pas reprise
    # Le journal dit la règle hors org.
    assert sorted(r[0] for r in _un(dsn, "SELECT subject_id FROM "
                                         "connector_account_share_events WHERE hors_org")) \
        == ["dehors", "dehors"]
    assert _tables(dsn) == []
    assert _colonnes(dsn) == ["connector_credentials.share_down",
                              "connector_credentials.share_mode"]
    assert _un(dsn, "SELECT to_regclass('connector_loan_events') IS NOT NULL")[0][0]

    command.downgrade(_alembic(), _PRECEDENTE)
    assert _tables(dsn) == sorted(_ANCIENNES_TABLES)
    assert _un(dsn, "SELECT count(*) FROM connector_account_grants")[0][0] == 0
    assert len(_colonnes(dsn)) == 5
    assert _regles(dsn) == attendues   # la descente ne retire pas les règles

    command.upgrade(_alembic(), _REVISION)
    assert _tables(dsn) == [] and _regles(dsn) == attendues


def test_une_regle_manuelle_n_est_pas_touchee_par_la_reprise(base):
    from alembic import command
    from oto_mcp.db import regles_de_comptes
    dsn = base["dsn"]
    command.downgrade(_alembic(), _PRECEDENTE)
    iid = _un(dsn, "SELECT id FROM connector_instances WHERE connector = 'attio'")[0][0]
    regles_de_comptes.poser_regle(base["org"], iid, subject_org=True, level="read",
                                  acteur="proprio")
    command.upgrade(_alembic(), _REVISION)
    assert ("attio", None, None, True, "read", None, "manuelle") in _regles(dsn)


def _migration():
    import importlib.util
    chemin = (RACINE / "oto_mcp" / "db" / "migrations" / "versions"
              / f"{_REVISION}.py")
    spec = importlib.util.spec_from_file_location("bascule_comptes", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_l_heritage_d_un_projet_partage_devient_des_regles_lues_dans_l_etat(base):
    """D2 : l'héritage des clés d'un projet partagé (#480) retiré, la reprise pose une
    règle `sensitive` (`reprise`) pour chaque personne qu'une arête `project_credentials`
    VIVANTE servait — sur les comptes de l'équipe propriétaire pour qui ne lit pas
    l'équipe, sur les comptes de l'org pour qui n'en est pas membre —, lue dans l'état :
    pas à qui le partage a échu, ni à qui n'a qu'un partage sans héritage, ni à un membre
    de l'équipe, ni quand le partageur n'atteint plus l'équipe. Un principal équipe vaut
    pour ses membres. Rejouée, elle donne le même état."""
    import psycopg
    from alembic import command
    from oto_mcp import db
    dsn, org, equipe = base["dsn"], base["org"], base["equipe"]
    command.downgrade(_alembic(), _PRECEDENTE)
    for sub in ("partageur", "beneficiaire", "echu", "sans_heritage", "coequipier",
                "du_support", "exterieur"):
        db.upsert_user(sub)
    with psycopg.connect(dsn, autocommit=True) as c:
        for sub in ("partageur", "beneficiaire", "echu", "sans_heritage", "coequipier",
                    "du_support"):
            c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (org, sub))
        for sub in ("partageur", "coequipier"):
            c.execute("INSERT INTO org_group_members (group_id, sub) VALUES (%s, %s)",
                      (equipe, sub))
        support = c.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, 'support') "
                            "RETURNING id", (org,)).fetchone()[0]
        c.execute("INSERT INTO org_group_members (group_id, sub) VALUES (%s, 'du_support')",
                  (support,))
        iid = c.execute("INSERT INTO connector_instances (connector, owner_type, owner_id, "
                        "account) VALUES ('hubspot', 'group', %s, '') RETURNING id",
                        (str(equipe),)).fetchone()[0]
        compte_d_org = c.execute(
            "SELECT id FROM connector_instances WHERE owner_type = 'org' AND owner_id = %s "
            "AND connector = 'attio'", (str(org),)).fetchone()[0]
        projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                           "VALUES ('group', %s, 'p') RETURNING id",
                           (str(equipe),)).fetchone()[0]
        partages = [("user", "beneficiaire", None), ("user", "echu", "-1 day"),
                    ("user", "sans_heritage", None), ("user", "coequipier", None),
                    ("group", str(support), None), ("user", "exterieur", None)]
        for genre, ident, echeance in partages:
            c.execute("INSERT INTO resource_grants (resource_type, resource_id, "
                      "principal_type, principal_id, expires_at) VALUES ('project', %s, "
                      "%s, %s, NOW() + %s::interval)",
                      (str(projet), genre, ident, echeance or "1 year"))
            if ident != "sans_heritage":
                c.execute("INSERT INTO grants (resource_kind, resource_id, grantor_kind, "
                          "grantor_id, grantee_kind, grantee_id) VALUES "
                          "('project_credentials', %s, 'user', 'partageur', %s, %s)",
                          (f"project:{projet}", genre, ident))

    def _du_compte(i):
        return _un(dsn, "SELECT subject_user, level, origine FROM connector_account_rules "
                        "WHERE instance_id = %s AND subject_user IS NOT NULL ORDER BY 1",
                   (i,))
    migration = _migration()
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        premier = migration.reprendre_les_prets(c, retirer_perimees=False)
        second = migration.reprendre_les_prets(c, retirer_perimees=False)
        c.commit()
    sur_l_equipe = [(s, "sensitive", "reprise")
                    for s in ("beneficiaire", "du_support", "exterieur")]
    assert _du_compte(iid) == sur_l_equipe
    # Le compte d'org : prêté à qui n'est pas de l'org (le prêt Google de la base n'y
    # touche pas).
    assert ("exterieur", "sensitive", "reprise") in _du_compte(compte_d_org)
    assert premier["heritage_de_projet"]["aretes"] == 4
    assert second["posees"] == second["mises_a_jour"] == second["retirees"] == 0
    command.upgrade(_alembic(), _REVISION)
    assert _du_compte(iid) == sur_l_equipe
    assert _un(dsn, "SELECT count(*) FROM grants WHERE resource_kind = "
                    "'project_credentials' AND revoked_at IS NULL")[0][0] == 0


def test_le_partageur_qui_n_atteint_plus_l_equipe_ne_prete_rien(base):
    """La borne d'avant la bascule, relue à la reprise : le partageur ne prête que ce
    qu'il atteint lui-même AUJOURD'HUI — sorti de l'équipe, son arête n'ouvre plus rien."""
    import psycopg
    from oto_mcp import db
    dsn, org, equipe = base["dsn"], base["org"], base["equipe"]
    for sub in ("ex_partageur", "beneficiaire_2"):
        db.upsert_user(sub)
    with psycopg.connect(dsn, autocommit=True) as c:
        for sub in ("ex_partageur", "beneficiaire_2"):
            c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s) "
                      "ON CONFLICT DO NOTHING", (org, sub))
        projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                           "VALUES ('group', %s, 'p2') RETURNING id",
                           (str(equipe),)).fetchone()[0]
        c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                  "principal_id) VALUES ('project', %s, 'user', 'beneficiaire_2')",
                  (str(projet),))
        c.execute("INSERT INTO grants (resource_kind, resource_id, grantor_kind, grantor_id, "
                  "grantee_kind, grantee_id) VALUES ('project_credentials', %s, 'user', "
                  "'ex_partageur', 'user', 'beneficiaire_2')", (f"project:{projet}",))
    migration = _migration()
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        regles, aretes = migration.regles_d_heritage(c.cursor(), {}, {})
        c.rollback()
    assert aretes >= 1
    assert not any(cle[1] == "beneficiaire_2" for cle in regles)


def test_la_revision_ne_lit_aucune_grosse_table():
    """Ni le journal d'appels ni l'usage : la reprise lit l'état seul, la révision tient
    sa transaction le temps de lire des tables de configuration."""
    import re
    for nom in ("20261105_0900_regles_de_comptes", _REVISION):
        source = (RACINE / "oto_mcp" / "db" / "migrations" / "versions"
                  / f"{nom}.py").read_text()
        assert not re.search(r"\b(FROM|JOIN)\s+(tool_calls|usage\w*|credit_movements|"
                             r"connector_loan_events|runs)\b", source, re.I), nom


def test_la_reprise_pose_le_niveau_le_plus_haut_qu_un_appel_exerce(base):
    """C : après la reprise, aucun couple personne × compte n'a un niveau inférieur à
    celui qu'il exerçait — chaque règle reprise est au niveau le plus haut qu'une classe
    d'effet exige, et chaque compte d'org vivant porte sa règle d'org."""
    from oto_mcp.access.niveaux import NIVEAU_DE_LA_CLASSE, NIVEAUX
    from oto_mcp.tools import effets
    migration = _migration()
    plus_haut = max((NIVEAU_DE_LA_CLASSE[c] for c in effets.CLASSES), key=NIVEAUX.index)
    assert migration.NIVEAU_REPRIS == plus_haut == "sensitive"
    dsn = base["dsn"]
    assert {r[0] for r in _un(dsn, "SELECT DISTINCT level FROM connector_account_rules "
                                   "WHERE origine = 'reprise'")} == {"sensitive"}
    assert _un(dsn, "SELECT count(*) FROM connector_instances i WHERE i.owner_type = 'org' "
                    "AND i.revoked_at IS NULL AND NOT EXISTS (SELECT 1 FROM "
                    "connector_account_rules r WHERE r.instance_id = i.id "
                    "AND r.subject_org)")[0][0] == 0
