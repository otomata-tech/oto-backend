"""Les lectures d'agrégat sont BORNÉES, et le dépassement sort NOMMÉ (#1145).

Le 04/10/2026, quelques lectures d'agrégat sur `tool_calls` ont tenu chacune une
connexion de 25 s à plus de 7 min, et une rafale a pris toute la réserve du pool. Le
pool applicatif ne pose aucun `statement_timeout` ; `db.lecture_bornee` en pose un,
court et LOCAL, sur ces lectures-là, et la face en fait un `503 aggregate_timeout`.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import _lecture_bornee, me_account, org_monitoring
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.db import lecture_bornee

#: Les capacités dont le chemin passe par une lecture d'agrégat bornée. Une capacité
#: qui s'y branche sans l'enveloppe rendrait le dépassement en 500 anonyme.
BORNEES = {
    "org.usage.calls", "org.usage.tools", "org.instruction.usage", "me.activity_summary",
    "org.monitoring.summary", "org.monitoring.console", "monitoring.summary",
    "admin.monitoring",
    # La supervision plateforme et la fiche des tenants (#1145, lot E).
    "monitoring.rest", "monitoring.connectors", "monitoring.funnel",
    "org.monitoring.connectors", "admin.tenants", "admin.tenant", "admin.tenant_console",
    # Les lectures du journal qui n'avaient pas de borne de durée (infra#9) : listes de
    # runs, journaux ligne à ligne, adoption, refus du transport, export d'audit.
    "usage.runs", "org.monitoring.runs", "monitoring.calls", "org.monitoring.calls",
    "me.calls", "monitoring.rest_calls", "monitoring.transport",
    "org.monitoring.adoption", "org.monitoring.view_as_writes", "org.audit_log.export",
    # Les jetons déclarés par les travaux d'une org.
    "org.usage.tokens",
}


@pytest.mark.parametrize("cle", sorted(BORNEES))
def test_la_capacite_est_enveloppee(cle):
    cap = next(c for c in CAPABILITIES if c.key == cle)
    assert getattr(cap.handler, "__wrapped__", None) is not None, (
        f"{cle} lit un agrégat borné sans `bornee(...)` : un dépassement y sortirait en 500.")


def test_le_depassement_sort_en_503_nomme(monkeypatch):
    def trop_long(*_a, **_kw):
        raise lecture_bornee.LectureTropLongue("relevé par outil d'une org", 10_000)

    monkeypatch.setattr(org_monitoring.db, "billable_usage_by_tool_for_org", trop_long)
    cap = next(c for c in CAPABILITIES if c.key == "org.usage.tools")
    with pytest.raises(AuthzDenied) as e:
        cap.handler(ResolvedCtx(sub="membre", org_id=7),
                    org_monitoring.OrgBillableToolsInput(org_id=7))
    assert (e.value.status, e.value.code) == (503, "aggregate_timeout")
    assert "relevé par outil" in e.value.message and "10 s" in e.value.message


def test_le_reste_passe_tel_quel(monkeypatch):
    """L'enveloppe ne traduit QUE le dépassement : un autre refus garde son code."""
    cap = next(c for c in CAPABILITIES if c.key == "me.activity_summary")
    monkeypatch.setattr(me_account.access, "current_org", lambda sub: None)
    with pytest.raises(AuthzDenied) as e:
        cap.handler(ResolvedCtx(sub="u-1", org_id=None), me_account.ActivitySummaryInput())
    assert e.value.code == "no_active_org"


def test_un_handler_asynchrone_est_refuse():
    async def lire(ctx, inp):
        return {}

    with pytest.raises(TypeError):
        _lecture_bornee.bornee(lire)


def test_la_borne_coupe_la_lecture_et_ne_reste_pas_sur_la_connexion(live, monkeypatch):
    """Contre la base : la lecture qui dépasse est annulée par PostgreSQL et sort
    NOMMÉE ; la connexion rendue au pool n'emporte pas la borne (`SET LOCAL`)."""
    from oto_mcp.db._conn import _connect

    monkeypatch.setattr(lecture_bornee, "DUREE_MAX_MS", 100)
    with pytest.raises(lecture_bornee.LectureTropLongue) as e:
        with lecture_bornee.lecture_d_agregat("essai") as conn:
            conn.execute("SELECT pg_sleep(2)")
    assert e.value.objet == "essai" and e.value.duree_ms == 100

    with lecture_bornee.lecture_d_agregat("essai") as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "100ms"
    with _connect() as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"


def test_l_isolation_se_pose_en_tete(live):
    with lecture_bornee.lecture_d_agregat("essai", isolation="REPEATABLE READ") as conn:
        assert conn.execute(
            "SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
    with pytest.raises(ValueError):
        with lecture_bornee.lecture_d_agregat("essai", isolation="READ UNCOMMITTED"):
            pass
