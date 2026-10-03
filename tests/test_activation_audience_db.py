"""L'audience des emails d'activation, sur le SQL réel : à quelle étape est chacun, et
à qui chaque email est dû — surtout à qui il ne l'est PAS.

Chaque compte de la population est un témoin, nommé par ce qu'il prouve. Les étapes
sont disjointes : un compte n'apparaît jamais dans deux audiences.

Patron de base éphémère : `test_outreach_audience_db.py::live`.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from oto_mcp.db.usage import _ARG_PROCEDURE

BASE = dict(tenant="acme", delay_hours=24, window_days=30, sequence_days=14,
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
        # ── connect ──
        ("acme:du", "du@client.test", 48, "member"),                # ← dû (connect)
        ("acme:trop-tot", "tot@client.test", 2, "member"),          # < 24 h
        ("acme:trop-vieux", "vieux@client.test", 24 * 45, "member"),  # > 30 jours
        ("acme:branche", "branche@client.test", 48, "member"),      # initialize seul
        ("acme:rest", "rest@client.test", 48, "member"),            # dashboard ⟹ dû (connect)
        ("acme:refus", "refus@client.test", 48, "member"),
        ("acme:deja", "deja@client.test", 48, "member"),            # connect déjà envoyé
        ("acme:suspendu", "suspendu@client.test", 48, "member"),
        ("acme:equipe", "quelquun@equipe.test", 48, "member"),
        ("acme:op-essai", "operateur+essai@exemple.test", 48, "member"),
        ("operateur", "operateur@exemple.test", 48, "super_admin"),
        ("acme:op-lui-meme", "admin@client.test", 48, "admin"),
        ("sub-nu", "nu@client.test", 48, "member"),                  # tenant primaire
        ("autre:x", "x@client.test", 48, "member"),                  # un autre tenant
        ("acme:mixte-a", "mixte@client.test", 24 * 5, "member"),     # boîte branchée, active
        ("acme:mixte-b", "Mixte@Client.test", 48, "member"),
        # ── first-process ──
        ("acme:p1-du", "p1@client.test", 24 * 5, "member"),          # ← dû (first-process)
        ("acme:p1-actif", "p1actif@client.test", 24 * 5, "member"),  # appelle encore
        ("acme:p1-recent", "p1recent@client.test", 24 * 5, "member"),  # branché il y a 10 h
        ("acme:p1-vieux", "p1vieux@client.test", 24 * 20, "member"),  # > 14 jours
        ("acme:p1-espace", "p1espace@client.test", 24 * 5, "member"),  # email il y a 10 h
        # ── recurring ──
        ("acme:r-du", "r@client.test", 24 * 6, "member"),             # ← dû (recurring)
        ("acme:r-programme", "rprog@client.test", 24 * 6, "member"),  # agent programmé
        ("acme:r-deux-jours", "r2j@client.test", 24 * 6, "member"),   # 2 jours de runs
        ("acme:r-echoue", "rko@client.test", 24 * 6, "member"),       # run « failed » ⟹ first-process
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

        def appel(sub, kind="mcp", il_y_a_h=72, tool="x"):
            conn.execute(
                "INSERT INTO tool_calls (sub, tool, kind, created_at) VALUES "
                "(%s, %s, %s, NOW() - make_interval(hours => %s))", (sub, tool, kind, il_y_a_h))

        def run(sub, run_id, il_y_a_h, outcome="done"):
            conn.execute(
                "INSERT INTO tool_calls (sub, tool, kind, run_id, args, created_at) VALUES "
                "(%s, 'run_start', 'mcp', %s, %s, NOW() - make_interval(hours => %s))",
                (sub, run_id, json.dumps({"label": "p", _ARG_PROCEDURE: "p"}), il_y_a_h + 1))
            conn.execute(
                "INSERT INTO tool_calls (sub, tool, kind, args, created_at) VALUES "
                "(%s, 'run_finish', 'mcp', %s, NOW() - make_interval(hours => %s))",
                (sub, json.dumps({"run_id": run_id, "outcome": outcome}), il_y_a_h))

        appel("acme:branche", "protocol")
        appel("acme:rest", "rest")
        appel("acme:mixte-a", "mcp", 2)
        appel("acme:p1-du", "mcp", 72)
        appel("acme:p1-actif", "mcp", 72)
        appel("acme:p1-actif", "mcp", 3)
        appel("acme:p1-recent", "protocol", 10)
        appel("acme:p1-vieux", "mcp", 24 * 19)
        appel("acme:p1-espace", "mcp", 72)
        for sub in ("acme:r-du", "acme:r-programme", "acme:r-deux-jours", "acme:r-echoue"):
            appel(sub, "mcp", 24 * 5)
        run("acme:r-du", "run-1", 72)
        run("acme:r-programme", "run-2", 72)
        run("acme:r-deux-jours", "run-3", 72)
        run("acme:r-deux-jours", "run-4", 120)
        run("acme:r-echoue", "run-5", 72, outcome="failed")
        conn.execute(
            "INSERT INTO runner_triggers (org_id, sub, procedure, tools, cron, enabled) "
            "VALUES (1, 'acme:r-programme', 'p', '[]', '0 8 * * 1', TRUE)")

        conn.execute("INSERT INTO outreach_optouts (sub) VALUES ('acme:refus')")
        conn.execute(
            "INSERT INTO outreach_sends (campaign, sub, to_email, locale, kind, fingerprint) "
            "VALUES ('activation-connect:acme', 'acme:deja', 'deja@client.test', 'en', "
            "'send', 'abc')")
        conn.execute(
            "INSERT INTO outreach_sends (campaign, sub, to_email, locale, kind, fingerprint, "
            "sent_at) VALUES ('activation-connect:acme', 'acme:p1-espace', "
            "'p1espace@client.test', 'en', 'send', 'abc', NOW() - INTERVAL '10 hours')")


def _subs(etape: str) -> set:
    from oto_mcp.db import activation
    return {r["sub"] for r in activation.audience(etape=etape, **BASE, cap=100)}


def test_connect_ne_retient_que_les_jamais_branches(live):
    assert _subs("connect") == {"acme:du", "acme:rest"}


def test_first_process_ne_retient_que_les_branches_silencieux_sans_run(live):
    """Actif depuis moins de 48 h, branché depuis moins de 48 h, inscrit depuis plus de
    14 jours, servi il y a 10 h : tous dehors. Un run `failed` n'est pas un résultat.
    Branchée sans jamais rien demander (`initialize` seul, il y a 72 h) : c'est
    exactement la personne à qui écrire « lancez un premier processus »."""
    assert _subs("first-process") == {"acme:p1-du", "acme:r-echoue", "acme:branche"}


def test_recurring_ne_retient_que_les_runs_uniques_sans_programme(live):
    assert _subs("recurring") == {"acme:r-du"}


def test_les_etapes_sont_disjointes(live):
    from oto_mcp.db import activation
    vus: dict = {}
    for etape in activation.ETAPES:
        for sub in _subs(etape):
            assert sub not in vus, f"{sub} est à la fois {vus[sub]} et {etape}"
            vus[sub] = etape


def test_les_exclusions_valent_pour_toutes_les_etapes(live):
    from oto_mcp.db import activation
    tous = set().union(*(_subs(e) for e in activation.ETAPES))
    for sub in ("acme:refus", "acme:suspendu", "acme:equipe", "acme:op-essai",
                "acme:op-lui-meme", "operateur", "sub-nu", "autre:x",
                "acme:mixte-a", "acme:mixte-b", "acme:deja", "acme:p1-espace"):
        assert sub not in tous, sub


def test_la_taille_dit_l_audience_entiere(live):
    from oto_mcp.db import activation
    assert activation.taille(etape="connect", **BASE) == 2
    assert len(activation.audience(etape="connect", **BASE, cap=1)) == 1


def test_un_travail_hors_serveur_lit_la_marque_declaree(live, monkeypatch):
    """Le registre des tenants n'est posé qu'au boot du serveur : le travail de
    maintenance doit le poser lui-même, ou il signerait du slug."""
    from oto_mcp import activation, tenancy
    from oto_mcp.db._conn import _connect
    marque = {"nom": "Acme", "site": "acme.test", "fond": "#ffffff", "surface": "#ffffff",
              "encre": "#111111", "discret": "#666666", "filet": "#dddddd",
              "bouton_fond": "#111111", "bouton_encre": "#ffffff"}
    with _connect() as conn:
        conn.execute("UPDATE tenants SET brand = %s WHERE slug = 'acme'",
                     (json.dumps(marque),))
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry())
    # Le process de maintenance lit le même fichier d'environnement que le serveur.
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.exemple.test")
    monkeypatch.setenv("OTO_ACTIVATION", json.dumps({"acme": {
        "sender": "Acme <hello@acme.test>", "reply_to": "hello@acme.test",
        "app_url": "https://app.acme.test", "mcp_url": "https://mcp.acme.test/mcp"}}))
    r = activation.reglages()[0]
    assert activation._nom_produit(r) == "Acme"
