"""oto#166, barreau 4 — le retrait du kit (Q1) et la poussée à un membre (Q2).

Décisions d'Alexis du 11/09/2026 :
- Q1 — « Retirer un connecteur du kit le désinstalle chez les membres, mais seulement
  là où c'est le kit qui l'a installé. Un membre qui l'avait installé lui-même le garde. »
- Q2 — « "Pousser à un membre" installe vraiment le connecteur chez ce membre — sans
  jamais passer outre un retrait qu'il aurait fait lui-même. »

Au tag v1.264.0, la poussée posait une préférence par outil que le régime de sélection
ignore (18 poussées en production, 11 sans aucun effet, réponse `ok` à chaque fois), et
un retrait du kit ne retirait rien. Comportement SERVI : chaîne REST, console MCP, et
ce que voit l'agent d'un membre par le vrai `compute_hidden_tools`, sur un vrai
PostgreSQL.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from _datastore_rest import call, stub_authz

CONNECTEURS = ("hunter", "kaspr", "folk", "osm")


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        from oto_mcp.connectors import activation
        init_db()
        for nom in CONNECTEURS:
            activation.set_activation(nom, True)       # master plateforme ON
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _org(nom: str, admin: str, *membres: str) -> int:
    from oto_mcp import org_store
    oid = org_store.create_org(nom, created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, m, "org_member")
    return oid


def _ligne(sub: str, org: int, connecteur: str):
    from oto_mcp import db
    with db._connect() as c:
        r = c.execute("SELECT state, origin FROM user_selected_connectors "
                      "WHERE sub = %s AND org_id = %s AND connector = %s",
                      (sub, org, connecteur)).fetchone()
    return dict(r) if r else None


class _Ctx:
    class _FastMCP:
        async def list_tools(self, run_middleware=False):
            return [type("T", (), {"name": f"{c}_x"})() for c in CONNECTEURS]

    fastmcp = _FastMCP()


def _vus_par_l_agent(sub: str, org: int) -> set[str]:
    from oto_mcp import session_visibility as SV
    caches = asyncio.run(SV.compute_hidden_tools(_Ctx(), sub, org=org))
    return {c for c in CONNECTEURS if f"{c}_x" not in caches}


def _rest(monkeypatch, cle: str, sub: str, org: int, *, path: dict, body=None):
    stub_authz(monkeypatch, org_id=org)
    return call(cle, sub=sub, path_params=path, body=body)


def _membre(monkeypatch, cle: str, sub: str, org: int, connecteur: str) -> None:
    code, corps = _rest(monkeypatch, cle, sub, org, path={"name": connecteur})
    assert code == 200, corps


def _pousse(monkeypatch, admin: str, org: int, connecteur: str, membre: str):
    return _rest(monkeypatch, "connectors.force.member", admin, org,
                 path={"id": str(org), "connector": connecteur}, body={"member": membre})


def _surcharges(sub: str, org: int) -> int:
    from oto_mcp import db
    with db._connect() as c:
        return c.execute("SELECT count(*) AS n FROM user_enabled_tools "
                         "WHERE sub = %s AND org_id = %s", (sub, org)).fetchone()["n"]


# ── Q1 : un retrait du kit ne retire que ce que le kit a posé ─────────────────────

def test_retirer_du_kit_desinstalle_seulement_ce_que_le_kit_a_pose(live, monkeypatch):
    # `kaspr` et non l'open data : depuis « actif = utilisable » (22/10/2026), la pause
    # d'un connecteur utilisable est refusée (409 `connector_usable`) — ce banc a besoin
    # d'un connecteur qu'on PEUT mettre en pause, donc qui demande une clé.
    from oto_mcp import db
    admin, pause, lui, pousse, ancien = (f"k4-q1-{x}" for x in
                                         ("admin", "pause", "lui", "pousse", "ancien"))
    org = _org("Kit4 Q1", admin, pause, lui, pousse, ancien)
    _membre(monkeypatch, "connectors.select", lui, org, "kaspr")        # l'a installé lui-même
    code, _ = _pousse(monkeypatch, admin, org, "kaspr", pousse)          # poussé par un admin
    assert code == 200
    with db._connect() as c:                                           # antérieur à la trace
        c.execute("INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                  "VALUES (%s, %s, 'kaspr', 'active')", (ancien, org))
    code, corps = _rest(monkeypatch, "connectors.recommend", admin, org,
                        path={"id": str(org)}, body={"connectors": ["kaspr"]})
    assert code == 200
    _membre(monkeypatch, "connectors.pause", pause, org, "kaspr")       # posé par le kit, en pause
    assert _ligne(pause, org, "kaspr") == {"state": "paused", "origin": "kit"}
    code, corps = _rest(monkeypatch, "connectors.unset_default", admin, org,
                        path={"id": str(org), "name": "kaspr"})
    assert code == 200, corps
    (ch,) = corps["changes"]
    assert ch["change"] == "removed" and ch["uninstalled"] == 2       # admin + pause
    assert ch["kept"] == {"membre": 1, "admin": 1, "inconnue": 1}
    for s in (admin, pause):
        assert _ligne(s, org, "kaspr") is None
        assert "kaspr" not in _vus_par_l_agent(s, org)
    for s, origine in ((lui, "membre"), (pousse, "admin"), (ancien, "inconnue")):
        assert _ligne(s, org, "kaspr") == {"state": "active", "origin": origine}
        assert "kaspr" in _vus_par_l_agent(s, org)


def test_poser_un_kit_plus_court_retire_pareil(live, monkeypatch):
    admin, m = "k4-put-admin", "k4-put-m"
    org = _org("Kit4 PUT", admin, m)
    _rest(monkeypatch, "connectors.recommend", admin, org, path={"id": str(org)},
          body={"connectors": ["folk", "kaspr"]})
    code, corps = _rest(monkeypatch, "connectors.recommend", admin, org, path={"id": str(org)},
                        body={"connectors": ["kaspr"]})
    assert code == 200 and corps["kit"] == ["kaspr"]
    (ch,) = corps["changes"]
    assert (ch["connector"], ch["change"], ch["uninstalled"]) == ("folk", "removed", 2)
    assert _ligne(m, org, "folk") is None and _ligne(m, org, "kaspr")["origin"] == "kit"


# ── Q2 : la poussée INSTALLE, chez ce membre seul, sans passer outre ─────────────

def test_la_poussee_installe_chez_le_membre_et_son_agent_le_voit(live, monkeypatch):
    from oto_mcp import org_store
    admin, m, autre = "k4-q2-admin", "k4-q2-m", "k4-q2-autre"
    org = _org("Kit4 Q2", admin, m, autre)
    for s in (m, autre):
        assert _vus_par_l_agent(s, org) == set()
    code, corps = _pousse(monkeypatch, admin, org, "hunter", m)
    assert code == 200, corps
    assert corps["ok"] is True and corps["result"] == "installed" and corps["member"] == m
    assert _ligne(m, org, "hunter") == {"state": "active", "origin": "admin"}
    assert _vus_par_l_agent(m, org) == {"hunter"}                     # sa prochaine conversation
    assert _ligne(autre, org, "hunter") is None                        # lui seul
    assert not org_store.get_org_default_connectors(org)              # le kit n'est pas touché
    assert _surcharges(m, org) == 0                                    # plus de préférence par outil


def test_la_poussee_ne_defait_pas_un_retrait_et_dit_la_date(live, monkeypatch):
    from oto_mcp import db
    admin, m = "k4-ret-admin", "k4-ret-m"
    org = _org("Kit4 retrait", admin, m)
    _membre(monkeypatch, "connectors.select", m, org, "folk")
    _membre(monkeypatch, "connectors.unselect", m, org, "folk")
    with db._connect() as c:
        date = c.execute("SELECT removed_at FROM connector_selection_removed "
                         "WHERE sub = %s AND org_id = %s AND connector = 'folk'",
                         (m, org)).fetchone()["removed_at"]
    code, corps = _pousse(monkeypatch, admin, org, "folk", m)
    assert code == 409 and corps["error"] == "removed_by_member"
    assert f"on {date} (UTC)" in corps["detail"] and "nothing was written" in corps["detail"]
    assert corps["details"] == {"removed_at": date}
    assert _ligne(m, org, "folk") is None and _surcharges(m, org) == 0


def test_la_poussee_ne_reprend_pas_une_pause(live, monkeypatch):
    admin, m = "k4-pause-admin", "k4-pause-m"
    org = _org("Kit4 pause", admin, m)
    _membre(monkeypatch, "connectors.pause", m, org, "kaspr")
    code, corps = _pousse(monkeypatch, admin, org, "kaspr", m)
    assert code == 409 and corps["error"] == "paused_by_member"
    assert _ligne(m, org, "kaspr") == {"state": "paused", "origin": "membre"}


def test_la_poussee_sur_un_connecteur_deja_actif_ne_reecrit_rien(live, monkeypatch):
    admin, m = "k4-actif-admin", "k4-actif-m"
    org = _org("Kit4 actif", admin, m)
    _membre(monkeypatch, "connectors.select", m, org, "osm")
    code, corps = _pousse(monkeypatch, admin, org, "osm", m)
    assert code == 200 and corps["result"] == "already_active"
    assert _ligne(m, org, "osm") == {"state": "active", "origin": "membre"}


def test_la_poussee_passe_la_garde_d_ecriture(live, monkeypatch):
    admin, m = "k4-garde-admin", "k4-garde-m"
    org = _org("Kit4 garde", admin, m)
    code, corps = _pousse(monkeypatch, admin, org, "pas-un-connecteur", m)
    assert code == 404 and corps["error"] == "unknown_connector"
    _rest(monkeypatch, "connectors.activation.set_org", admin, org,
          path={"id": str(org), "name": "hunter"}, body={"enabled": False})
    code, corps = _pousse(monkeypatch, admin, org, "hunter", m)
    assert code == 409 and corps["error"] == "org_disabled"
    assert _ligne(m, org, "hunter") is None


def test_la_face_mcp_op_force_passe_par_la_meme_fonction(live):
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import console
    admin, m = "k4-mcp-admin", "k4-mcp-m"
    org = _org("Kit4 mcp", admin, m)
    out = asyncio.run(console._connector(
        ResolvedCtx(sub=admin, org_id=org),
        console.ConnectorInput(op="force", org_id=org, name="folk", member=m)))
    assert out["result"] == "installed"
    assert _ligne(m, org, "folk") == {"state": "active", "origin": "admin"}


def test_la_poussee_vise_un_membre_de_l_org(live, monkeypatch):
    admin = "k4-hors-admin"
    org = _org("Kit4 hors org", admin)
    code, corps = _pousse(monkeypatch, admin, org, "folk", "k4-pas-membre")
    assert code == 400 and corps["error"] == "user_not_in_org"
