"""Org SUSPENDUE : chaque garde, là où elle vit, et à l'envers (l'org active passe).

Quatre portes, qui ne partagent que le prédicat (`org_suspension.etat`) :
  1. les capacités — dans l'adaptateur, juste après la règle d'autz (ici : la vraie
     route REST, par `_datastore_rest.call`) ;
  2. les outils de connecteur — `activation_gate._refus`, seam commun de l'appel direct
     et d'`oto_call` ;
  3. la réservation d'un travail — `claim_next_job`, en SQL réel ;
  4. le webhook entrant et le cron — refus nommé / rien d'enfilé.
"""
from __future__ import annotations

import os
import uuid

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import org_store, org_suspension

SUSP = {"id": 35, "suspended_at": "2026-10-09T00:00:00+00:00",
        "suspended_by": "svc", "suspended_reason": "trial_ended"}


@pytest.fixture
def suspendue(monkeypatch):
    """L'org 35 est suspendue, toutes les autres sont actives."""
    monkeypatch.setattr(org_store, "suspended_org_ids", lambda: [35])
    monkeypatch.setattr(org_store, "get_org_suspension",
                        lambda org_id: SUSP if org_id == 35 else None)


# ── 1. les capacités ─────────────────────────────────────────────────────────

@pytest.fixture
def sans_handler(monkeypatch):
    """La garde vit dans `_amont`, AVANT le handler : on joue `_amont` pour de vrai et
    on remplace seulement le handler, qui toucherait la base."""
    from oto_mcp.capabilities import _rest_adapter

    async def _execute(handler, prepare):
        ctx, _inp = prepare()
        return ctx, {"ok": True}
    monkeypatch.setattr(_rest_adapter, "execute", _execute)


def test_une_capacite_dans_une_org_suspendue_est_refusee(monkeypatch, suspendue, sans_handler):
    stub_authz(monkeypatch, org_id=35)
    status, body = call("me.tools.list")
    assert status == 403 and body["error"] == "org_suspended"


def test_la_meme_capacite_dans_une_org_active_passe(monkeypatch, suspendue, sans_handler):
    """Le contrefactuel : sans lui, une garde qui refuse tout serait verte."""
    stub_authz(monkeypatch, org_id=36)
    status, body = call("me.tools.list")
    assert (status, body) == (200, {"ok": True})


def test_une_capacite_ouverte_passe_meme_suspendue(monkeypatch, suspendue, sans_handler):
    stub_authz(monkeypatch, org_id=35)
    status, _ = call("me.token.list")
    assert status == 200


@pytest.mark.parametrize("cle", ["me.get", "org.list", "org.set_home", "me.token.list"])
def test_se_reperer_et_changer_dorg_reste_ouvert(cle):
    assert org_suspension.ouverte(cle)


def test_ce_qui_agit_dans_lorg_nest_pas_ouvert():
    for cle in ("me.tools.call", "me.project_file.list", "me.unipile.connect",
                "me.credential.set"):
        assert not org_suspension.ouverte(cle), cle


def test_les_operations_de_plateforme_restent_ouvertes():
    """C'est par elles qu'on lève la suspension."""
    assert org_suspension.ouverte("admin.org_suspension")
    assert org_suspension.ouverte("platform.org.grant_key")


@pytest.mark.parametrize("cle", ["platform.org.grant_key", "platform.org.unipile_limit_set",
                                 "admin.tenant_org_grant", "admin.org_suspension"])
def test_les_leviers_du_service_dusage_passent_meme_dans_une_org_suspendue(
        monkeypatch, suspendue, sans_handler, cle):
    """Le pire cas : l'appelant RÉSOUT sur l'org suspendue. Les leviers qui rendent
    l'accès à l'abonnement ne doivent jamais être refusés par la suspension."""
    stub_authz(monkeypatch, org_id=35, role="super_admin")
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_platform_operator", lambda sub: True, raising=False)
    params = {"platform.org.grant_key": ({"id": "35", "provider": "aiark"}, {}),
              "platform.org.unipile_limit_set": ({"id": "35"}, {"limit": 1}),
              "admin.tenant_org_grant": ({"slug": "t", "provider": "aiark",
                                          "org_id": "35"}, {}),
              "admin.org_suspension": ({"id": "35"}, {"op": "resume"})}[cle]
    status, body = call(cle, path_params=params[0], body=params[1])
    assert (status, body) == (200, {"ok": True}), (cle, status, body)


def test_garde_capacite_ignore_un_worker_et_une_cap_sans_org(suspendue):
    from oto_mcp.capabilities._types import ResolvedCtx
    org_suspension.garde_capacite("runner.jobs", ResolvedCtx(sub="w", platform_worker=True))
    org_suspension.garde_capacite("x.y", ResolvedCtx(sub="u", org_id=None))


def test_un_hoquet_de_base_ne_laisse_pas_passer(monkeypatch):
    """Jamais lue + lecture en échec ⟹ refus, pas laisser-passer."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(org_store, "suspended_org_ids", boom)
    from oto_mcp.capabilities._types import ResolvedCtx
    with pytest.raises(RuntimeError):
        org_suspension.garde_capacite("me.tools.list", ResolvedCtx(sub="u", org_id=35))


# ── 2. les outils de connecteur ──────────────────────────────────────────────

def _gate_env(monkeypatch, org):
    from oto_mcp.connectors import activation_gate as g
    monkeypatch.setattr(g.call_axes, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(g.access, "current_org", lambda sub: org)
    monkeypatch.setattr(g.access, "current_group", lambda sub: None)
    monkeypatch.setattr(g.activation, "cran_qui_coupe", lambda c, o, gr: None)
    return g


def test_un_outil_de_connecteur_dans_une_org_suspendue_est_refuse(monkeypatch, suspendue):
    g = _gate_env(monkeypatch, 35)
    err = g._refus("serper")
    assert err is not None and err.data["code"] == "org_suspended"
    assert err.data["org_id"] == 35


def test_le_meme_outil_dans_une_org_active_passe(monkeypatch, suspendue):
    g = _gate_env(monkeypatch, 36)
    assert g._refus("serper") is None


# ── la face d'admin ──────────────────────────────────────────────────────────

def _admin_env(monkeypatch, existe=True):
    from oto_mcp.capabilities import org_suspension as cap
    box = {"etat": None}
    monkeypatch.setattr(cap.org_store, "get_org", lambda i: {"id": i} if existe else None)
    monkeypatch.setattr(cap.org_store, "get_org_suspension", lambda i: box["etat"])

    def _suspend(i, *, by, reason):
        box["etat"] = box["etat"] or {"id": i, "suspended_at": "t", "suspended_by": by,
                                      "suspended_reason": reason}
        return box["etat"]

    def _resume(i):
        changed, box["etat"] = box["etat"] is not None, None
        return changed

    monkeypatch.setattr(cap.org_store, "suspend_org", _suspend)
    monkeypatch.setattr(cap.org_store, "resume_org", _resume)
    return cap


def test_suspendre_exige_un_motif(monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    cap = _admin_env(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        cap._org_suspension(ResolvedCtx(sub="op"),
                            cap.OrgSuspensionInput(op="suspend", org_id=7))
    assert e.value.code == "missing_reason"


def test_suspendre_puis_lever_est_idempotent(monkeypatch):
    from oto_mcp.capabilities._types import ResolvedCtx
    cap = _admin_env(monkeypatch)
    ctx = ResolvedCtx(sub="op")
    out = cap._org_suspension(ctx, cap.OrgSuspensionInput(op="suspend", org_id=7,
                                                          reason="trial_ended"))
    assert out["suspended"] and out["changed"]
    again = cap._org_suspension(ctx, cap.OrgSuspensionInput(op="suspend", org_id=7,
                                                            reason="trial_ended"))
    assert again["suspended"] and not again["changed"]
    assert cap._org_suspension(ctx, cap.OrgSuspensionInput(op="resume", org_id=7))["changed"]
    assert not cap._org_suspension(ctx, cap.OrgSuspensionInput(op="resume",
                                                               org_id=7))["changed"]


def test_une_org_inconnue_est_un_404(monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    cap = _admin_env(monkeypatch, existe=False)
    with pytest.raises(AuthzDenied) as e:
        cap._org_suspension(ResolvedCtx(sub="op"),
                            cap.OrgSuspensionInput(op="resume", org_id=7))
    assert e.value.status == 404


def test_la_face_rest_est_un_post_reserve_au_super_admin():
    from _datastore_rest import cap
    c = cap("admin.org_suspension")
    [b] = c.rest_bindings()
    assert (b.verb, b.path) == ("POST", "/api/admin/orgs/{id}/suspension")
    from oto_mcp.capabilities._authz import SUPER_ADMIN
    assert c.authz is SUPER_ADMIN


# ── 3. la réservation, en SQL réel ───────────────────────────────────────────

@pytest.fixture(scope="module")
def live(pg_module_dsn):
    avant = {cle: os.environ.get(cle) for cle in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    os.environ["DATABASE_URL"] = pg_module_dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        for cle, valeur in avant.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur


def _org(nom: str) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (nom,)).fetchone()["id"]


def test_suspendre_et_lever_sur_base_reelle(live):
    org = _org(f"s-{uuid.uuid4().hex[:6]}")
    assert org_store.get_org_suspension(org) is None
    etat = org_store.suspend_org(org, by="svc", reason="trial_ended")
    assert etat["suspended_reason"] == "trial_ended"
    # Re-suspendre ne réécrit ni l'auteur ni le motif.
    assert org_store.suspend_org(org, by="autre", reason="x")["suspended_by"] == "svc"
    assert org_store.resume_org(org) is True and org_store.get_org_suspension(org) is None
    assert org_store.resume_org(org) is False


def test_le_travail_dune_org_suspendue_nest_pas_reserve(live):
    from oto_mcp import db
    org, autre = _org("susp"), _org("active")
    tenu = db.enqueue_job(org, "start", payload={"input": "go", "tools": []})["id"]
    libre = db.enqueue_job(autre, "start", payload={"input": "go", "tools": []})["id"]
    org_store.suspend_org(org, by="svc", reason="trial_ended")
    pris = db.claim_next_job(None, "w", lease_seconds=60, org_ids=[org, autre])
    assert pris and pris["id"] == libre
    assert db.claim_next_job(None, "w", lease_seconds=60, org_ids=[org, autre]) is None
    # Levée : le travail, resté en file intact, repart.
    org_store.resume_org(org)
    assert db.claim_next_job(None, "w", lease_seconds=60, org_ids=[org])["id"] == tenu


# ── 4. le cron ───────────────────────────────────────────────────────────────

def test_le_cron_nenfile_rien_pour_une_org_suspendue(monkeypatch, suspendue):
    from oto_mcp import runner_tick
    t = {"id": 1, "org_id": 35, "cron": "0 * * * *", "tz": "UTC", "next_due": "x",
         "procedure": "p", "project_id": None}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda: [t])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda *a: True)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne doit être enfilé"))
    assert runner_tick._tick() == 0


# ── le premier appel d'une org (horloge de l'essai d'un tenant) ──────────────

def test_le_premier_appel_de_chaque_org_sur_base_reelle(live):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    org, muette = _org("appelante"), _org("muette")
    for quand in ("2026-09-01 10:00:00+00", "2026-08-15 09:00:00+00", "2026-09-20 08:00:00+00"):
        db.insert_tool_call({"tool": "oto_whoami", "org_id": org, "sub": "u", "ok": True,
                             "kind": "rest"})
        with _connect() as conn:
            conn.execute("UPDATE tool_calls SET created_at = %s WHERE id = "
                         "(SELECT MAX(id) FROM tool_calls WHERE org_id = %s)", (quand, org))
    got = db.premiers_appels([org, muette])
    assert str(got[org]).startswith("2026-08-15") and got[muette] is None
    assert db.premiers_appels([]) == {}


def test_la_lecture_est_une_operation_de_plateforme():
    from _datastore_rest import cap
    from oto_mcp.capabilities._authz import PLATFORM_ADMIN
    c = cap("platform.usage.first_calls")
    assert c.authz is PLATFORM_ADMIN and org_suspension.ouverte(c.key)
    [b] = c.rest_bindings()
    assert (b.verb, b.path) == ("GET", "/api/admin/usage/first-calls")


# ── la liste en mémoire : aucune lecture par appel ───────────────────────────

def _compteur(monkeypatch, ids):
    lus = {"n": 0}

    def _lire():
        lus["n"] += 1
        return list(ids)
    monkeypatch.setattr(org_store, "suspended_org_ids", _lire)
    return lus


def test_une_org_active_ne_lit_pas_la_base_a_chaque_appel(monkeypatch):
    lus = _compteur(monkeypatch, [])
    monkeypatch.setattr(org_store, "get_org_suspension",
                        lambda o: pytest.fail("aucun détail lu pour une org active"))
    for _ in range(100):
        assert org_suspension.etat(36) is None
    assert lus["n"] == 1


def test_la_liste_est_relue_apres_le_ttl(monkeypatch):
    lus = _compteur(monkeypatch, [])
    horloge = {"t": 1000.0}
    monkeypatch.setattr(org_suspension.time, "monotonic", lambda: horloge["t"])
    org_suspension.etat(36)
    horloge["t"] += org_suspension.TTL_S - 1
    org_suspension.etat(36)
    assert lus["n"] == 1
    horloge["t"] += 2
    org_suspension.etat(36)
    assert lus["n"] == 2


def test_le_geste_dadmin_est_vu_tout_de_suite_dans_ce_processus(monkeypatch):
    ids = []
    _compteur_ids = {"ids": ids}
    monkeypatch.setattr(org_store, "suspended_org_ids", lambda: list(_compteur_ids["ids"]))
    monkeypatch.setattr(org_store, "get_org_suspension", lambda o: SUSP)
    assert org_suspension.etat(35) is None
    _compteur_ids["ids"] = [35]
    org_suspension.invalider()
    assert org_suspension.etat(35) == SUSP


def test_une_relecture_en_echec_garde_la_derniere_liste(monkeypatch):
    """Une suspension posée n'est pas oubliée parce que la base hoquette."""
    horloge = {"t": 1000.0}
    monkeypatch.setattr(org_suspension.time, "monotonic", lambda: horloge["t"])
    monkeypatch.setattr(org_store, "suspended_org_ids", lambda: [35])
    monkeypatch.setattr(org_store, "get_org_suspension", lambda o: SUSP)
    assert org_suspension.etat(35) == SUSP
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(org_store, "suspended_org_ids", boom)
    horloge["t"] += org_suspension.TTL_S + 1
    assert org_suspension.etat(35) == SUSP


def test_invalider_avant_toute_lecture_ne_vaut_pas_une_liste_vide(monkeypatch):
    """Sinon un premier échec de lecture servirait une liste vide : laisser-passer."""
    org_suspension.invalider()
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(org_store, "suspended_org_ids", boom)
    with pytest.raises(RuntimeError):
        org_suspension.etat(35)
