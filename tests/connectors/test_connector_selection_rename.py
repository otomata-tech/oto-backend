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
    sel.rename_selection(conn, "aiark", "linkedin")
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
    """Le renommage `linkedin` → `aiark` (posé le 2026-08-10, leçon de #295) a été
    RETIRÉ du boot le 2026-08-28, et son retrait était OBLIGATOIRE.

    Une migration de boot n'est sûre QUE tant que son nom SOURCE reste mort. Celle-ci
    l'était : `linkedin` avait été déposé, plus rien ne le créait. Depuis le split,
    `linkedin` est la session hébergée et le fan-out en CRÉE des lignes à chaque
    boot — le renommage devenait donc une bombe à retardement d'un boot de décalage
    (boot N crée les sélections LinkedIn, boot N+1 les déménage vers `aiark`, et
    ainsi à chaque redémarrage).

    Le tripwire vise le geste, pas le symptôme : reprendre un nom déposé oblige à
    relire TOUTES les migrations qui le nomment, et c'est cette relecture-là qu'on
    ne pense pas à faire."""
    init_src = (pathlib.Path(__file__).resolve().parents[2]
                / "oto_mcp" / "db" / "_init.py").read_text(encoding="utf-8")
    # Sur les lignes de CODE seulement : le commentaire qui explique le retrait cite
    # forcément le geste retiré, et une sonde textuelle naïve se déclencherait dessus
    # — un test qui interdit d'expliquer sa propre raison d'être.
    code = [l for l in init_src.splitlines() if not l.lstrip().startswith("#")]
    assert not [l for l in code if 'rename_selection(conn, "linkedin"' in l], (
        "`linkedin` n'est plus un nom mort : le fan-out du split en crée des lignes "
        "à chaque boot, un renommage depuis ce nom les déménagerait en boucle.")
    assert [l for l in code if 'fanout_selection(conn, "unipile"' in l]
