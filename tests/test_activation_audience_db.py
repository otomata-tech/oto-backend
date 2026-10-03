"""L'audience de l'email d'activation, sur le SQL réel : qui le reçoit, et surtout qui
ne le reçoit PAS.

Une seule personne doit entrer : un compte du tenant configuré, jamais branché, inscrit
entre `delay_hours` et `window_days`. Chaque autre compte de la population est un
témoin d'une exclusion, nommé par ce qu'il prouve.

Patron de base éphémère : `test_outreach_audience_db.py::live`.
"""
from __future__ import annotations

import os
import uuid

import pytest

CAMPAGNE = "activation-connect:acme"
CRIT = dict(tenant="acme", campaign=CAMPAGNE, delay_hours=24, window_days=30,
            exclude_domains=["equipe.test"])


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_activation_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        _peupler()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url), ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _peupler() -> None:
    from oto_mcp.db._conn import _connect

    # (sub, email, inscrit il y a N heures, rôle)
    comptes = [
        ("acme:du", "du@client.test", 48, "member"),                # ← le seul dû
        ("acme:trop-tot", "tot@client.test", 2, "member"),          # < 24 h
        ("acme:trop-vieux", "vieux@client.test", 24 * 45, "member"),  # > 30 jours
        ("acme:actif", "actif@client.test", 48, "member"),          # a appelé un outil
        ("acme:branche", "branche@client.test", 48, "member"),      # initialize seul
        ("acme:rest", "rest@client.test", 48, "member"),            # dashboard seul ⟹ DÛ
        ("acme:refus", "refus@client.test", 48, "member"),          # désinscrit
        ("acme:deja", "deja@client.test", 48, "member"),            # déjà envoyé
        ("acme:suspendu", "suspendu@client.test", 48, "member"),
        ("acme:equipe", "quelquun@equipe.test", 48, "member"),      # domaine déclaré
        ("acme:op-essai", "operateur+essai@exemple.test", 48, "member"),  # +suffixe d'un opérateur
        ("operateur", "operateur@exemple.test", 48, "super_admin"),  # l'opérateur (autre tenant)
        ("acme:op-lui-meme", "admin@client.test", 48, "admin"),      # opérateur du tenant
        ("sub-nu", "nu@client.test", 48, "member"),                  # tenant primaire
        ("autre:x", "x@client.test", 48, "member"),                  # un AUTRE tenant
        # une boîte, deux comptes du tenant ; l'un a appelé ⟹ la boîte a appelé
        ("acme:mixte-a", "mixte@client.test", 72, "member"),
        ("acme:mixte-b", "Mixte@Client.test", 48, "member"),
        ("acme:sans-email", None, 48, "member"),
    ]
    with _connect() as conn:
        for slug, iss in (("oto", None), ("acme", "https://a.exemple.test"),
                          ("autre", "https://b.exemple.test")):
            conn.execute("INSERT INTO tenants (slug, name, issuer) VALUES (%s, %s, %s) "
                         "ON CONFLICT (slug) DO NOTHING", (slug, slug, iss))
        for sub, email, heures, role in comptes:
            conn.execute(
                "INSERT INTO users (sub, email, role, created_at) VALUES "
                "(%s, %s, %s, NOW() - make_interval(hours => %s))",
                (sub, email, role, heures))
        conn.execute("UPDATE users SET suspended_at = NOW(), suspended_reason = 'test' "
                     "WHERE sub = 'acme:suspendu'")
        for sub, kind in (("acme:actif", "mcp"), ("acme:branche", "protocol"),
                          ("acme:rest", "rest"), ("acme:mixte-a", "mcp")):
            conn.execute("INSERT INTO tool_calls (sub, tool, kind) VALUES (%s, 'x', %s)",
                         (sub, kind))
        conn.execute("INSERT INTO outreach_optouts (sub) VALUES ('acme:refus')")
        conn.execute(
            "INSERT INTO outreach_sends (campaign, sub, to_email, locale, kind, fingerprint) "
            "VALUES (%s, 'acme:deja', 'deja@client.test', 'en', 'send', 'abc')",
            (CAMPAGNE,))


def _subs() -> set:
    from oto_mcp.db import activation
    return {r["sub"] for r in activation.audience(**CRIT, cap=100)}


def test_seuls_les_comptes_dus_entrent(live):
    assert _subs() == {"acme:du", "acme:rest"}


def test_ouvrir_le_dashboard_n_est_pas_se_brancher(live):
    """Une ligne `rest` dit que la personne a ouvert l'application, pas qu'elle a
    branché son agent : elle reste due."""
    assert "acme:rest" in _subs()


def test_un_initialize_suffit_a_sortir(live):
    """Branché sans rien demander : le message « ajoutez le connecteur » serait faux."""
    assert "acme:branche" not in _subs()


def test_les_operateurs_et_leurs_adresses_d_essai_sortent(live):
    s = _subs()
    assert "acme:op-essai" not in s, "une adresse +suffixe d'un opérateur est un essai"
    assert "acme:op-lui-meme" not in s


def test_seul_le_tenant_configure_est_lu(live):
    s = _subs()
    assert "sub-nu" not in s and "autre:x" not in s


def test_la_boite_compte_pour_tous_ses_comptes(live):
    """Un des deux comptes de la boîte a appelé : la personne s'est branchée."""
    s = _subs()
    assert not {"acme:mixte-a", "acme:mixte-b"} & s


def test_la_taille_dit_l_audience_entiere(live):
    from oto_mcp.db import activation
    assert activation.taille(**CRIT) == 2
    assert len(activation.audience(**CRIT, cap=1)) == 1


def test_un_travail_hors_serveur_lit_la_marque_declaree(live, monkeypatch):
    """Le registre des tenants n'est posé qu'au boot du serveur : le travail de
    maintenance doit le poser lui-même, ou il signerait du slug."""
    import json as _json
    from oto_mcp import activation, tenancy
    from oto_mcp.db._conn import _connect
    marque = {"nom": "Acme", "site": "acme.test", "fond": "#ffffff", "surface": "#ffffff",
              "encre": "#111111", "discret": "#666666", "filet": "#dddddd",
              "bouton_fond": "#111111", "bouton_encre": "#ffffff"}
    with _connect() as conn:
        conn.execute("UPDATE tenants SET brand = %s WHERE slug = 'acme'",
                     (_json.dumps(marque),))
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry())
    # Le process de maintenance lit le même fichier d'environnement que le serveur.
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.exemple.test")
    monkeypatch.setenv("OTO_ACTIVATION", _json.dumps({"acme": {
        "sender": "Acme <hello@acme.test>", "reply_to": "hello@acme.test",
        "app_url": "https://app.acme.test", "mcp_url": "https://mcp.acme.test/mcp"}}))
    r = activation.reglages()[0]
    assert activation._nom_produit(r) == "Acme"
