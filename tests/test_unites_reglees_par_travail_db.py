"""`org.usage.tokens` : les unités que le grand livre a RÉGLÉES pour les tours de modèle de
chaque travail (`budget_units`), EN BASE et par les verbes du worker.

Ce que ces bancs tiennent :

1. un travail sur la clé PARTAGÉE (`reserve`, `meter`, `complete`) se lit, à sa case, avec
   EXACTEMENT les unités de ses mouvements `settle` — tours rapportés et tour jamais
   rapporté (réglé à la conclusion) compris ;
2. sans multiplier la case : un règlement par tour, mais un travail, ses jetons et son
   temps comptés une fois ;
3. un travail d'ABONNEMENT (ou sur la clé de l'org) se lit `0` : rien n'est réglé ;
4. ne comptent que les règlements de tours de modèle (`anthropic.messages:*`) du travail,
   quel que soit le payeur (`org:` ou `user:`), jamais ceux d'un travail d'une autre org.
"""
from __future__ import annotations

import json
import uuid

import pytest

PORTEUR = "worker:banc"
ORG, AUTRE_ORG = 4201, 4202
AGENT = 77
# Une unité de budget par paquet : 100 jetons d'entrée, 10 de sortie, 100 d'écriture de
# cache, 1 000 de lecture (`test_cle_partagee_anthropic_db.py`).
LIGNES = [("input_tokens", 1, 100), ("output_tokens", 1, 10),
          ("cache_creation_input_tokens", 1, 100), ("cache_read_input_tokens", 1, 1000)]
TOUR = {"model": "claude-sonnet-5", "usage_input": 100, "usage_output": 50,
        "usage_cache_write": 0, "usage_cache_read": 1000}


@pytest.fixture(scope="module", autouse=True)
def bareme(live):
    from oto_mcp.access import credits
    from oto_mcp.db import credits as db_credits
    db_credits.poser_bareme(1, [{"connector": "anthropic",
                                 "function": "messages:claude-sonnet-5",
                                 "unit": u, "budget": b, "per_n": n} for u, b, n in LIGNES])
    credits.oublier()
    yield
    credits.oublier()


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")


def _travail_pris(org: int, payload: dict) -> int:
    """Un travail RÉSERVÉ par `PORTEUR`, sur un run réel, dans `org`."""
    from oto_mcp.db._conn import _connect
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    with _connect() as c:
        c.execute("INSERT INTO runs (run_id, sub, org_id, label) VALUES (%s, %s, %s, %s)",
                  (run_id, "alexis", org, "banc"))
        return c.execute(
            "INSERT INTO runner_jobs (org_id, kind, payload, status, claimed_by, attempts, "
            "sub, run_id) VALUES (%s, 'start', %s::jsonb, 'claimed', %s, 1, 'alexis', %s) "
            "RETURNING id", (org, json.dumps(payload), PORTEUR, run_id)).fetchone()["id"]


def _jobs(op, jid, **champs):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    return RJ._jobs(ResolvedCtx(sub=PORTEUR, org_id=None, role="platform_worker",
                                platform_worker=True),
                    RJ.JobsInput(op=op, job_id=jid, **champs))


def _regle(jid) -> int:
    """Ce que le grand livre a réglé pour ce travail, lu SANS la lentille."""
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        return int(c.execute(
            "SELECT COALESCE(-SUM(amount), 0) AS u FROM credit_movements "
            "WHERE kind = 'settle' AND call_uid LIKE %s", (f"runner:{jid}:%",)
        ).fetchone()["u"])


def _cases(org):
    from oto_mcp.capabilities import org_monitoring as OM
    from oto_mcp.capabilities._types import ResolvedCtx
    lu = OM._tokens(ResolvedCtx(sub="admin", org_id=org), OM.OrgTokensInput(org_id=org, days=1))
    return {(r["trigger_id"], r["paid_by"]): r for r in lu["rows"]}


def test_PREUVE_les_unites_d_un_travail_sur_la_cle_partagee_sont_ses_reglements(live):
    from oto_mcp import db
    from oto_mcp.db import credits as db_credits
    payload = {"model": "claude-sonnet-5", "model_family": "anthropic",
               "trigger_id": str(AGENT)}
    jid = _travail_pris(ORG, payload)
    db_credits.verser(f"org:{ORG}", 10_000, "banc", f"banc-{jid}")
    assert db.estampiller_cle_partagee(jid, PORTEUR, "anthropic", "platform")

    _jobs("reserve", jid, turn=0, max_tokens=100)
    assert _jobs("meter", jid, turn=0, result=TOUR)["amount"] == 7
    _jobs("reserve", jid, turn=1, max_tokens=100)
    assert _jobs("meter", jid, turn=1, result={**TOUR, "usage_input": 5_000})["amount"] == 56
    _jobs("reserve", jid, turn=2, max_tokens=40)     # l'appel qui a coupé la boucle
    fin = _jobs("complete", jid, ok=True, result={
        "paye_par": "cle_plateforme", "model": "claude-sonnet-5", "usage_input": 5_100,
        "usage_output": 100, "usage_cache_read": 2_000, "usage_cache_write": 0})
    assert fin["status"] == "done"

    # Trois règlements (7 + 56 + le tour jamais rapporté, à ses 4 unités réservées).
    assert _regle(jid) == 67
    case = _cases(ORG)[(AGENT, "cle_plateforme")]
    assert case["budget_units"] == 67, "exactement ses règlements"
    # Un règlement par tour, mais la case compte le travail UNE fois.
    assert (case["jobs"], case["input"], case["output"], case["cache_read"]) == \
        (1, 5_100, 100, 2_000), case


def test_un_travail_d_abonnement_se_lit_ZERO_a_cote(live):
    """Même agent, payé par un abonnement : aucun tour réglé, donc `0` — ses jetons
    restent comptés, et la case du travail sur la clé partagée ne bouge pas."""
    from oto_mcp.db._conn import _connect
    # Lue avant : ce test ne dépend pas de l'ordre (la case de la clé partagée vient
    # du test d'avant quand il a tourné sur ce worker, sinon elle n'existe pas).
    avant = _cases(ORG).get((AGENT, "cle_plateforme"))
    with _connect() as c:
        c.execute(
            "INSERT INTO runner_jobs (org_id, kind, status, due_at, attempts, max_attempts, "
            "payload, result, finished_at) VALUES (%s, 'start', 'done', NOW(), 1, 3, "
            "%s::jsonb, %s::jsonb, NOW())",
            (ORG, json.dumps({"trigger_id": str(AGENT), "model": "sub:sonnet",
                              "model_family": "claude_subscription"}),
             json.dumps({"paye_par": "abonnement", "model": "claude-sonnet-5",
                         "usage_input": 10, "usage_output": 20})))
    cases = _cases(ORG)
    assert cases[(AGENT, "abonnement")]["budget_units"] == 0
    assert cases[(AGENT, "abonnement")]["output"] == 20
    assert cases.get((AGENT, "cle_plateforme")) == avant, "la case de la clé partagée ne bouge pas"


def test_seuls_les_reglements_de_tours_DU_travail_comptent(live):
    """Un règlement payé par une PERSONNE (org en mode personne) compte ; un règlement
    d'un autre connecteur qui citerait le travail, non ; un travail d'une AUTRE org ne
    prête rien à celle-ci."""
    from oto_mcp.db._conn import _connect

    def _fini(org, trigger, paye_par):
        with _connect() as c:
            return c.execute(
                "INSERT INTO runner_jobs (org_id, kind, status, due_at, attempts, "
                "max_attempts, payload, result, finished_at) VALUES (%s, 'start', 'done', "
                "NOW(), 1, 3, %s::jsonb, %s::jsonb, NOW()) RETURNING id",
                (org, json.dumps({"trigger_id": str(trigger)}),
                 json.dumps({"paye_par": paye_par, "usage_input": 1, "usage_output": 1}))
            ).fetchone()["id"]

    def _reglement(payer, jid, montant, connector="anthropic", function="messages:x"):
        with _connect() as c:
            c.execute(
                "INSERT INTO credit_movements (payer, kind, amount, held, call_uid, "
                "connector, function, meta) VALUES (%s, 'settle', %s, 0, %s, %s, %s, "
                "%s::jsonb)",
                (payer, -montant, f"runner:{jid}:1:{uuid.uuid4().hex[:6]}", connector,
                 function, json.dumps({"runner_job_id": jid, "attempt": 1, "turn": 0})))

    ici = _fini(ORG, 88, "cle_plateforme")
    _reglement(f"user:{uuid.uuid4().hex[:6]}", ici, 11)          # payé par une personne
    _reglement(f"org:{ORG}", ici, 1000, connector="apify",       # pas un tour de modèle
               function="run")
    ailleurs = _fini(AUTRE_ORG, 88, "cle_plateforme")
    _reglement(f"org:{AUTRE_ORG}", ailleurs, 500)

    assert _cases(ORG)[(88, "cle_plateforme")]["budget_units"] == 11
    assert _cases(AUTRE_ORG)[(88, "cle_plateforme")]["budget_units"] == 500


def test_le_demarrage_pose_l_index_des_reglements_d_un_travail_valide(live):
    from oto_mcp.db import credits as db_credits
    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        assert ic.scalaire_de(c)(db_credits.INDEX_REGLEMENTS_PAR_TRAVAIL.sql_validite) is True
