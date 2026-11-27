"""Les images CONNUES sur une vraie base : ce que les écritures posent au registre
`images_connues`, et ce que la boucle de copie en fait (`images_connues_worker.py`).

Ce qu'on vérifie, c'est ce que le STOCKAGE porte : une ligne écrite par un agent
apprend ses images, un geste d'app ou un travail de fond n'apprend rien, une valeur
déjà dans la ligne n'est pas apprise par l'écriture suivante, toute écriture d'app
(création, mise à jour, brouillon, publication) apprend son HTML, un registre en panne
ne défait pas l'écriture, et la copie conclut chaque adresse une fois — jamais à la
lecture.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import db, geste
from oto_mcp import images_connues as ic
from oto_mcp.db import images_connues as reg

MOI = "sub-images-connues"


def _url(nom: str) -> str:
    return f"https://img-{uuid.uuid4().hex[:8]}.example/{nom}.png"


@pytest.fixture
def table(live):
    return db.create_datastore("user", MOI, "images-" + uuid.uuid4().hex[:6])


@pytest.fixture
def projet(live):
    return int(db.create_project("user", MOI, "Images", created_by=MOI))


# ── Les lignes ─────────────────────────────────────────────────────────────

def test_agent_apprend_ce_qu_il_ecrit(table):
    logo, site = _url("logo"), "https://site.example"
    with geste.portee(geste.AGENT, MOI):
        db.datastore_insert_row(table, "r1", {"logo": logo, "site": site})
    r = reg.etat_de(logo)
    assert r and r["etat"] == reg.A_COPIER and r["origine"] == "ligne"
    assert r["empreinte"] == ic.empreinte(logo)
    assert reg.etat_de(site) is None


def test_geste_d_app_console_et_fond_n_apprennent_rien(table):
    page, console, fond, nu = _url("page"), _url("console"), _url("fond"), _url("nu")
    with geste.portee(geste.AGENT, MOI), ic.geste_d_app():
        db.datastore_insert_row(table, "r1", {"logo": page})
    with geste.portee(geste.CONSOLE, MOI):     # la session d'un lecteur, hôte de page
        db.datastore_insert_row(table, "r4", {"logo": console})
    with geste.interne("formules"):
        db.datastore_insert_row(table, "r2", {"logo": fond})
    db.datastore_insert_row(table, "r3", {"logo": nu})          # aucun geste posé
    for u in (page, console, fond, nu):
        assert reg.etat_de(u) is None, u


def test_ce_que_la_ligne_portait_n_est_pas_appris(table):
    """La page pose une adresse composée ; un agent réécrit la ligne autour d'une autre
    colonne et la recopie (ce que ferait une formule) : elle reste inconnue."""
    compose, neuf = "https://evil.example/?d=secret", _url("neuf")
    with geste.portee(geste.CONSOLE, MOI):
        db.datastore_insert_row(table, "r1", {"note": compose})
    with geste.portee(geste.AGENT, MOI):
        db.datastore_merge_row_locked(
            table, "r1", lambda d: {**d, "statut": "ok", "photo": d["note"], "logo": neuf},
            "2026-11-26T10:00:00Z")
    assert reg.etat_de(compose) is None
    assert reg.etat_de(neuf)["etat"] == reg.A_COPIER


def test_registre_en_panne_ne_defait_pas_l_ecriture(table):
    logo = _url("panne")
    with db._connect() as conn:
        conn.execute("ALTER TABLE images_connues RENAME TO images_connues_hors")
    try:
        with geste.portee(geste.AGENT, MOI):
            row = db.datastore_insert_row(table, "r1", {"logo": logo})
        assert row["data"]["logo"] == logo
    finally:
        with db._connect() as conn:
            conn.execute("ALTER TABLE images_connues_hors RENAME TO images_connues")
    assert reg.etat_de(logo) is None


# ── Les apps ───────────────────────────────────────────────────────────────

def test_toute_ecriture_d_app_apprend_son_html(projet):
    a, b, c = _url("creation"), _url("mise-a-jour"), _url("brouillon")
    app = db.apps.create(("user", MOI), None, project_id=projet, title="A", manifest={},
                         html=f'<img src="{a}">', thumbnail=None, by=MOI)
    db.apps.update(app["id"], by=MOI, html=f'<div style="background:url({b})"></div>')
    db.app_versions.set_enabled(app["id"], True)
    db.app_versions.write_draft(app["id"], by=MOI, html=f"<script>i.src='{c}'</script>")
    for u in (a, b, c):
        r = reg.etat_de(u)
        assert r and r["origine"] == "app", u


# ── La copie ───────────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def copie(live, monkeypatch):
    """Le téléchargement et le dépôt, remplacés : chaque adresse rend ce que le test
    lui donne, chaque dépôt est noté. Ce qu'on juge, c'est l'issue écrite au registre."""
    from oto_mcp import media_store
    from oto_mcp.tools import web
    reponses, deposes, demandes = {}, [], []

    def fetch(url, deadline_s=0):
        demandes.append(url)
        r = reponses[url]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(web, "_fetch_http", fetch)

    class Client:
        def put_object(self, **kw):
            deposes.append(kw)
    monkeypatch.setattr(media_store, "_get_client", lambda: Client())
    monkeypatch.setattr(media_store, "_bucket", lambda: "seau")
    monkeypatch.setattr(media_store, "public_base", lambda: "https://seau.example")
    with db._connect() as conn:
        conn.execute("DELETE FROM images_connues")
    return reponses, deposes, demandes


def _apprendre(*urls):
    with db._connect() as conn:
        reg.retenir(conn, urls, "app")


def test_copie_une_fois_et_conclut(copie):
    from oto_mcp import images_connues_worker as W
    from oto_mcp.mcp_errors import McpError
    from mcp.types import INVALID_REQUEST, ErrorData
    reponses, deposes, demandes = copie
    ok, html, interne, absent, lent = (_url(n) for n in ("ok", "html", "interne", "absent", "lent"))
    reponses.update({
        ok: {"ok": True, "body": PNG, "capped": False},
        html: {"ok": True, "body": b"<html><svg/>", "capped": False},
        interne: McpError(ErrorData(code=INVALID_REQUEST, message="internal address")),
        absent: {"ok": False, "verdict": "HTTP 404", "status": 404},
        lent: {"ok": False, "verdict": "timeout after 0 redirect(s), on x"},
    })
    _apprendre(ok, html, interne, absent, lent)
    assert W._tour() == (5, 1)

    r = reg.etat_de(ok)
    assert r["etat"] == reg.COPIEE and r["type_image"] == "image/png" and r["octets"] == len(PNG)
    (d,) = deposes
    assert d["Key"] == f"app-images/{ic.empreinte(ok)}" and d["ACL"] == "public-read"
    assert d["ContentType"] == "image/png"
    for u in (html, interne, absent):
        assert reg.etat_de(u)["etat"] == reg.REFUSEE, u
    # Panne passagère : reste à copier, plus tard.
    assert reg.etat_de(lent)["etat"] == reg.A_COPIER

    # Le tour suivant ne reprend rien : copiée et refusées sont closes, la lente attend.
    demandes.clear()
    assert W._tour() == (0, 0) and demandes == []


def test_panne_passagere_refusee_apres_trois_essais(copie):
    from oto_mcp import images_connues_worker as W
    reponses, _, _ = copie
    lent = _url("lent")
    reponses[lent] = {"ok": False, "verdict": "HTTP 503", "status": 503}
    _apprendre(lent)
    for essai in range(1, W.ESSAIS_MAX + 1):
        with db._connect() as conn:   # l'attente écoulée, sans dormir
            conn.execute("UPDATE images_connues SET prochain_essai = NOW()")
        W._tour()
        attendu = reg.REFUSEE if essai == W.ESSAIS_MAX else reg.A_COPIER
        assert reg.etat_de(lent)["etat"] == attendu, essai


def test_adresse_deja_connue_ne_bouge_pas(copie):
    from oto_mcp import images_connues_worker as W
    reponses, deposes, _ = copie
    u = _url("une")
    reponses[u] = {"ok": True, "body": PNG, "capped": False}
    _apprendre(u)
    W._tour()
    _apprendre(u)            # réapprise : reste copiée, rien ne repart
    assert reg.etat_de(u)["etat"] == reg.COPIEE
    assert W._tour() == (0, 0) and len(deposes) == 1


def test_echec_impreve_ecrit_puis_refuse(copie, monkeypatch):
    """Une copie qui lève toujours pareil ne revient pas à chaque bail pour toujours."""
    from oto_mcp import images_connues_worker as W
    reponses, _, _ = copie
    u = _url("casse")
    reponses[u] = RuntimeError("imprévu")
    _apprendre(u)
    for essai in range(1, W.ESSAIS_MAX + 1):
        with db._connect() as conn:
            conn.execute("UPDATE images_connues SET prochain_essai = NOW()")
        W._tour()
        r = reg.etat_de(u)
        assert r["etat"] == (reg.REFUSEE if essai == W.ESSAIS_MAX else reg.A_COPIER), essai
        assert "RuntimeError" in r["detail"]


def test_la_garde_d_egress_tient_depuis_la_boucle(live, monkeypatch):
    """Le VRAI `_fetch_http`, depuis le fil de la boucle (`ThreadPoolExecutor`), vers
    une image servie en local : une adresse interne n'est jamais copiée. Seul le
    stockage est remplacé."""
    import http.server
    import threading
    from oto_mcp import images_connues_worker as W
    from oto_mcp import media_store

    class Png(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(PNG)

        def log_message(self, *a):
            pass

    serveur = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Png)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    deposes = []

    class Client:
        def put_object(self, **kw):
            deposes.append(kw)
    monkeypatch.setattr(media_store, "_get_client", lambda: Client())
    monkeypatch.setattr(media_store, "_bucket", lambda: "seau")
    monkeypatch.setattr(media_store, "public_base", lambda: "https://seau.example")
    try:
        with db._connect() as conn:
            conn.execute("DELETE FROM images_connues")
        u = f"http://127.0.0.1:{serveur.server_port}/logo.png"
        _apprendre(u)
        assert W._tour() == (1, 0)
        r = reg.etat_de(u)
        assert r["etat"] == reg.REFUSEE and r["detail"].startswith("egress"), r["detail"]
        assert deposes == []
    finally:
        serveur.shutdown()
