"""Un tour OpenRouter sur la clé partagée, EN BASE : ni réservé ni réglé au budget
d'unités, journalisé à son coût — une ligne `settlement` que la lentille de facturation
sert, écrite une seule fois quand le worker rejoue son rapport.

Une doublure ne prouve ni la relecture du run par la tentative en cours, ni la clé qui
garde le rejeu (même simultané), ni le débit du quota de l'accord, ni que la lentille
membre sert la ligne : c'est pourtant ce qui fait la facture.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

PORTEUR = "worker:banc"
FLASH = "deepseek/deepseek-v4.1-flash-1017"
TOUR = {"model": FLASH, "usage_input": 152, "usage_output": 81, "usage_cache_write": None,
        "usage_cache_read": 6912, "usage_cost_usd": 6.44952e-05}


@pytest.fixture
def travail(live, monkeypatch):
    from oto_mcp.db._conn import _connect
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    org = 43
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    with _connect() as c:
        c.execute("INSERT INTO runs (run_id, sub, org_id, label) VALUES (%s, %s, %s, %s)",
                  (run_id, "alexis", org, "banc"))
        jid = c.execute(
            """INSERT INTO runner_jobs (org_id, kind, payload, status, claimed_by, attempts,
                                        sub, run_id)
               VALUES (%s, 'start', %s::jsonb, 'claimed', %s, 1, 'alexis', %s)
               RETURNING id""",
            (org, '{"model": "%s", "model_family": "openrouter"}' % FLASH, PORTEUR,
             run_id)).fetchone()["id"]
    yield jid, run_id, org


def _jobs(op, jid, **champs):
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    return RJ._jobs(ResolvedCtx(sub=PORTEUR, org_id=None, role="platform_worker",
                                platform_worker=True),
                    RJ.JobsInput(op=op, job_id=jid, **champs))


def test_un_tour_au_cout_est_journalise_une_fois_et_servi_a_la_facturation(travail):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.db import usage
    from oto_mcp.db._conn import _connect
    jid, run_id, org = travail
    assert db.estampiller_cle_partagee(jid, PORTEUR, "openrouter", "platform")

    assert _jobs("reserve", jid, turn=0, max_tokens=16000) == {"ok": True, "held": False}
    assert _jobs("meter", jid, turn=0, result=TOUR) == {
        "ok": True, "metered": True, "microdollars": 65, "duplicate": False}
    assert _jobs("meter", jid, turn=0, result=TOUR)["duplicate"] is True
    with pytest.raises(AuthzDenied) as e:
        _jobs("meter", jid, turn=1, result={**TOUR, "usage_cost_usd": None})
    assert e.value.status == 422

    with _connect() as c:
        assert c.execute("SELECT count(*) AS n FROM credit_movements WHERE call_uid LIKE %s",
                         (f"runner:{jid}:%",)).fetchone()["n"] == 0, "rien au budget d'unités"
    page = usage.list_billable_calls_for_org(org, run_ids=[run_id])
    assert page["total"] == 1
    (ligne,) = page["calls"]
    assert (ligne["tool"], ligne["quantity"], ligne["key_mode"], ligne["run_id"],
            ligne["call_uid"]) == ("openrouter", 65, "platform", run_id, f"runner:{jid}:1:0")


def _en_meme_temps(n, geste):
    """`n` fils qui jouent `geste()` au même instant ; rend leurs résultats."""
    depart, sorties = threading.Barrier(n), []

    def fil():
        depart.wait()
        sorties.append(geste())
    fils = [threading.Thread(target=fil) for _ in range(n)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    return sorties


def _lignes(uid):
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        return c.execute("SELECT count(*) AS n FROM tool_calls WHERE call_uid = %s",
                         (uid,)).fetchone()["n"]


def test_l_insertion_d_un_releve_est_idempotente_sous_concurrence(live):
    from oto_mcp.db import usage
    uid = f"runner:{uuid.uuid4().hex[:6]}:1:0"
    ligne = {"kind": "settlement", "tool": "openrouter", "sub": "alexis", "org_id": 43,
             "ok": True, "duration_ms": 0, "call_uid": uid, "quantity": 65,
             "key_mode": "platform", "args": {"turn": 0}}
    assert sorted(_en_meme_temps(8, lambda: usage.inserer_tour_au_cout(dict(ligne)))) == (
        [False] * 7 + [True])
    assert _lignes(uid) == 1
    assert usage.inserer_tour_au_cout(dict(ligne)) is False
    with pytest.raises(ValueError):
        usage.inserer_tour_au_cout({**ligne, "tool": "apify"})


def test_un_tour_debite_une_fois_meme_rejoue_en_meme_temps(travail):
    from oto_mcp import access, db
    jid, run_id, org = travail
    sub = "alexis"
    avant = access.usage_today(sub, "openrouter")
    assert db.estampiller_cle_partagee(jid, PORTEUR, "openrouter", "platform")
    sorties = _en_meme_temps(6, lambda: _jobs("meter", jid, turn=0, result=TOUR))
    assert sorted(o["duplicate"] for o in sorties) == [False] + [True] * 5
    assert access.usage_today(sub, "openrouter") == avant + 65
    assert _lignes(f"runner:{jid}:1:0") == 1


def test_la_cle_du_tenant_journalise_sans_debiter(travail):
    from oto_mcp import access, db
    jid, run_id, org = travail
    avant = access.usage_today("alexis", "openrouter")
    assert db.estampiller_cle_partagee(jid, PORTEUR, "openrouter", "tenant")
    assert _jobs("meter", jid, turn=0, result=TOUR)["duplicate"] is False
    assert access.usage_today("alexis", "openrouter") == avant


@pytest.fixture
def accord(travail, monkeypatch):
    """Une vraie clé OpenRouter de plateforme, accordée à l'org du travail avec un quota
    du jour de 100 µ$ ; le demandeur, membre de cette org."""
    from oto_mcp import credentials_store, db, org_store
    from oto_mcp.db._conn import _connect
    jid, run_id, _ = travail
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
    sub = f"u-{uuid.uuid4().hex[:6]}"
    db.upsert_user(sub, f"{sub}@exemple.test")
    org = org_store.create_org(f"Org {sub}", created_by=sub)
    org_store.add_org_member(org, sub)
    with _connect() as c:
        c.execute("UPDATE runner_jobs SET sub = %s, org_id = %s WHERE id = %s",
                  (sub, org, jid))
    credentials_store.set_platform_key("plateforme", "openrouter", "or-banc", set_by="op")
    credentials_store.platform_grant("openrouter", f"org:{org}", daily_quota=100)
    assert db.estampiller_cle_partagee(jid, PORTEUR, "openrouter", "platform")
    return jid, sub


def test_le_plafond_atteint_refuse_le_tour_suivant_depassement_borne_a_un_tour(accord):
    from oto_mcp import access
    from oto_mcp.capabilities._types import AuthzDenied
    jid, sub = accord
    assert _jobs("reserve", jid, turn=0, max_tokens=16000) == {"ok": True, "held": False}
    _jobs("meter", jid, turn=0, result=TOUR)                       # 65 / 100
    assert _jobs("reserve", jid, turn=1, max_tokens=16000) == {"ok": True, "held": False}
    _jobs("meter", jid, turn=1, result=TOUR)                       # 130 / 100
    utilise = access.usage_today(sub, "openrouter")
    assert utilise == 130 and utilise - 100 < 65, "dépassement borné au coût d'UN tour"
    with pytest.raises(AuthzDenied) as e:
        _jobs("reserve", jid, turn=2, max_tokens=16000)
    assert (e.value.status, e.value.code) == (402, "platform_quota_exhausted")
    assert "130/100" in str(e.value)
