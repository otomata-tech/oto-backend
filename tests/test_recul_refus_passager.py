"""Un refus PASSAGER du fournisseur recule plus loin qu'un échec ordinaire.

Le 20/10/2026, trois travaux d'un agent webhook sont morts en quatre minutes : leurs
trois tentatives, à +30 s puis +60 s, toutes tombées dans la même fenêtre de
refus de six minutes sur la clé de son org. Un 429 / 529 / 5xx recule désormais de
2 min, puis 10 min ; tout autre échec garde le linéaire (+30 s × tentatives).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from oto_mcp.db.runner_jobs import refus_passager


@pytest.mark.parametrize("motif", [
    "fin_anormale (rate_limit, HTTP 429 : Rate limit reached for requests)",
    "RateLimitError : HTTP 429 rate_limit_error — Number of request tokens has exceeded",
    "InternalServerError : HTTP 529 overloaded_error — Overloaded",
    "fin_anormale (server_error, HTTP 500 : API Error)",
    "APIStatusError : HTTP 503 — Service Unavailable",
])
def test_un_refus_passager_se_reconnait(motif):
    assert refus_passager(motif)


@pytest.mark.parametrize("motif", [
    None,
    "",
    "boom",
    "fin_anormale (authentication_failed, HTTP 401 : invalid x-api-key)",
    "fin_anormale (billing_error, HTTP 400 : Your credit balance is too low)",
    "fin_anormale (cle_sans_workspace, HTTP 400 : clé d'organisation Anthropic : …)",
    "fin_anormale (max_tokens)",
    "/api/me/runs/thread → 401 : invalid_api_token",
    # un nombre qui ressemble à un statut, sans être un statut HTTP
    "appel_outil_mal_encode (salesforce_query) après 5290 jetons",
])
def test_une_cle_refusee_ou_un_echec_ordinaire_n_en_est_pas_un(motif):
    """Attendre plus longtemps ne répare ni une clé refusée, ni un crédit épuisé."""
    assert not refus_passager(motif)


def _recul(d, job_id, org_id):
    """Le recul posé par la conclusion : `due_at` − NOW() au moment de la lecture."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT due_at - NOW() AS recul FROM runner_jobs WHERE id = %s AND org_id = %s",
            (job_id, org_id)).fetchone()
    return row["recul"]


def _echouer(d, org_id, worker, motif):
    """Rend le travail réservable maintenant, le réserve, le conclut en échec."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = NOW() WHERE org_id = %s "
                     "AND status = 'pending'", (org_id,))
    job = d.claim_next_job(org_id, worker, lease_seconds=60)
    assert job, "le travail doit être réservable"
    d.complete_job(job["id"], worker, False, error=motif)
    return job["id"]


def test_un_refus_passager_recule_de_2_puis_10_minutes(live):
    from oto_mcp import db as d

    org = 9301
    d.enqueue_job(org, "start", payload={"procedure": "p"})
    motif = "fin_anormale (rate_limit, HTTP 429 : Rate limit reached)"

    jid = _echouer(d, org, "w-passager", motif)
    assert timedelta(seconds=110) < _recul(d, jid, org) <= timedelta(seconds=120)

    _echouer(d, org, "w-passager", motif)
    assert timedelta(seconds=590) < _recul(d, jid, org) <= timedelta(seconds=600)

    _echouer(d, org, "w-passager", motif)
    assert d.get_job(jid, org)["status"] == "failed", "le plafond de tentatives reste le même"


def test_un_echec_ordinaire_garde_le_recul_lineaire(live):
    from oto_mcp import db as d

    org = 9302
    d.enqueue_job(org, "start", payload={"procedure": "p"})

    jid = _echouer(d, org, "w-ordinaire", "boom")
    assert timedelta(seconds=20) < _recul(d, jid, org) <= timedelta(seconds=30)

    _echouer(d, org, "w-ordinaire", "fin_anormale (authentication_failed, HTTP 401 : x)")
    assert timedelta(seconds=50) < _recul(d, jid, org) <= timedelta(seconds=60)
