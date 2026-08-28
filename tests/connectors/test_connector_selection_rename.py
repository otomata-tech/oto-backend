"""Renommage des sélections d'un connecteur déposé (oto-backend#295).

Déposer un connecteur renomme le registre ; la toolbox d'un membre, elle, est la
liste de ses connecteurs INSTALLÉS. `linkedin` → `aiark` (#279) a laissé 119 lignes
sur un nom qui ne résout plus rien : aucun outil monté, et sous le régime strict
« non-sélectionné = masqué » la surface entière disparaît sans un mot.

Le seul cas qui CASSE est la paire `(sub, org)` portant DÉJÀ les deux connecteurs :
la PK est `(sub, org_id, connector)`, donc un `UPDATE … SET connector` brut y viole
l'unicité. Un test naïf (une seule ligne à renommer) passe et ne prouve rien — d'où
un exercice contre un **vrai PostgreSQL**, la seule instance qui applique la PK.

Le test se saute proprement si aucun PostgreSQL n'est joignable (fixture `pg_dsn`,
`tests/conftest.py`) : le garde-fou d'ORDRE en fin de fichier, lui, reste actif
partout — c'est l'ordre des trois gestes qui porte le correctif.
"""
from __future__ import annotations

import pathlib

import pytest

from oto_mcp.connectors import selection as sel


@pytest.fixture()
def conn(pg_dsn):
    """Connexion sur une table `user_selected_connectors` fraîche (schéma réel)."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    with psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS user_selected_connectors")
        c.execute("DROP TABLE IF EXISTS connector_selection_seeded")
        sel.init_schema(c)          # le VRAI schéma, PK comprise
        yield c


def _seed(conn, rows):
    for sub, org_id, connector, state in rows:
        conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
            "VALUES (%s, %s, %s, %s)", (sub, org_id, connector, state))


def _selection(conn) -> set:
    return {(r["sub"], r["org_id"], r["connector"], r["state"]) for r in
            conn.execute("SELECT sub, org_id, connector, state "
                         "FROM user_selected_connectors").fetchall()}


def test_simple_rename_keeps_its_state(conn):
    _seed(conn, [("s1", 2, "linkedin", "active"),
                 ("s2", 2, "linkedin", "paused")])
    assert sel.rename_selection(conn, "linkedin", "aiark") == 2
    # une pause reste une pause : renommer n'est pas installer
    assert _selection(conn) == {("s1", 2, "aiark", "active"),
                               ("s2", 2, "aiark", "paused")}


def test_pair_holding_both_connectors_does_not_violate_the_primary_key(conn):
    """LE cas du bug : sans le dédoublonnage, cet UPDATE lève UniqueViolation."""
    _seed(conn, [("s1", 2, "linkedin", "active"),
                 ("s1", 2, "aiark", "paused")])
    sel.rename_selection(conn, "linkedin", "aiark")
    # une seule ligne survit, et elle est ACTIVE — le plus permissif gagne, car le
    # membre avait bien l'outil par sa sélection `linkedin`.
    assert _selection(conn) == {("s1", 2, "aiark", "active")}


def test_both_paused_stays_paused(conn):
    """Le « plus permissif gagne » ne fabrique pas un actif à partir de deux pauses."""
    _seed(conn, [("s1", 2, "linkedin", "paused"),
                 ("s1", 2, "aiark", "paused")])
    sel.rename_selection(conn, "linkedin", "aiark")
    assert _selection(conn) == {("s1", 2, "aiark", "paused")}


def test_already_active_target_is_untouched(conn):
    _seed(conn, [("s1", 2, "linkedin", "paused"),
                 ("s1", 2, "aiark", "active")])
    sel.rename_selection(conn, "linkedin", "aiark")
    assert _selection(conn) == {("s1", 2, "aiark", "active")}


def test_scope_is_the_pair_not_the_sub(conn):
    """Le dédoublonnage se juge par (sub, org_id) : la même personne dans deux orgs
    n'est pas la même sélection."""
    _seed(conn, [("s1", 2, "linkedin", "active"),
                 ("s1", 3, "aiark", "paused"),
                 ("s1", 3, "linkedin", "active")])
    sel.rename_selection(conn, "linkedin", "aiark")
    assert _selection(conn) == {("s1", 2, "aiark", "active"),
                               ("s1", 3, "aiark", "active")}


def test_replay_is_a_noop(conn):
    """La migration tourne à CHAQUE boot (base partagée preprod/prod) : le second
    passage ne doit rien trouver, ni rien abîmer."""
    _seed(conn, [("s1", 2, "linkedin", "active"), ("s1", 2, "aiark", "paused"),
                 ("s2", 2, "linkedin", "paused"), ("s3", 0, "aiark", "active")])
    sel.rename_selection(conn, "linkedin", "aiark")
    after = _selection(conn)
    assert sel.rename_selection(conn, "linkedin", "aiark") == 0
    assert _selection(conn) == after


def test_other_connectors_are_never_touched(conn):
    _seed(conn, [("s1", 2, "linkedin", "active"), ("s1", 2, "unipile", "paused"),
                 ("s2", 2, "folk", "active")])
    sel.rename_selection(conn, "linkedin", "aiark")
    assert ("s1", 2, "unipile", "paused") in _selection(conn)
    assert ("s2", 2, "folk", "active") in _selection(conn)


# ── garde-fou d'ORDRE (actif sans PostgreSQL) ────────────────────────────────

_SRC = (pathlib.Path(__file__).resolve().parents[2]
        / "oto_mcp" / "connectors" / "selection.py").read_text(encoding="utf-8")


def test_the_three_statements_stay_in_this_order():
    """Promouvoir → dédoublonner → renommer. Inverser 2 et 3 fait échouer le
    renommage sur la PK ; passer 1 après 2 perd l'information « était active » (la
    ligne source est déjà supprimée) — et un membre garderait une sélection en pause
    alors qu'il avait l'outil."""
    body = _SRC[_SRC.index("def rename_selection"):_SRC.index("# --- migration ADR 0050")]
    promote = body.index("UPDATE user_selected_connectors a SET state")
    dedupe = body.index("DELETE FROM user_selected_connectors a")
    rename = body.index("UPDATE user_selected_connectors SET connector")
    assert promote < dedupe < rename


def test_le_boot_ne_renomme_plus_linkedin_vers_aiark():
    """Le renommage `linkedin` → `aiark` a été RETIRÉ du boot, et il doit le rester.

    Il était juste tant que `linkedin` était un nom MORT : rien n'en créait, la
    migration ne pouvait donc que rattraper des lignes orphelines (leçon #295). Le
    lot suivant rend ce nom à une surface VIVANTE, dont le fan-out créera des lignes
    à chaque boot — la migration cesserait alors de migrer pour déménager en boucle,
    un boot sur deux.

    **La règle générale, qui est ce que ce test garde vraiment** : une migration de
    boot A → B n'est sûre que tant que RIEN ne crée de A. Reprendre un nom déposé
    oblige donc à relire toutes les migrations qui le nomment — et c'est cette
    relecture-là qu'on ne pense pas à faire, parce que le nom qu'on reprend paraît
    libre justement parce qu'il est mort.

    Le tripwire ne lit que les lignes de CODE : le commentaire qui explique le
    retrait cite forcément le geste retiré, et une sonde textuelle naïve interdirait
    d'expliquer sa propre raison d'être."""
    init_src = (pathlib.Path(__file__).resolve().parents[2]
                / "oto_mcp" / "db" / "_init.py").read_text(encoding="utf-8")
    code = [l for l in init_src.splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if 'rename_selection(conn, "linkedin"' in l], (
        "le renommage depuis `linkedin` est de retour : s'il existe une surface qui "
        "CRÉE des lignes `linkedin`, cette migration les déménagera en boucle.")
