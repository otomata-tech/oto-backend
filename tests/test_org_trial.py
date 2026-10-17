"""La fin d'essai d'une org, poussée par le commerce (`service.org.trial`, 16/10/2026).

L'essai n'est PAS un droit déclaré (docs/droits-declares.md) : il appartient au service
commerce, qui suspend l'org à sa fin (`service.org.suspension`). Il pousse aussi la date
(`orgs.trial_ends_at`), que la plateforme ne fait que SERVIR — dans la fiche d'org, que
lisent les admins et les agents hébergés (`oto_admin_org op=get`).

Même forme que `test_org_suspension.py` : qui passe (le commerce, un super admin), qui est
refusé, pose et effacement idempotents, org inconnue, puis la fiche — et, sur base réelle
quand un PostgreSQL est joignable, le store.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import org_store

FIN = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _env(monkeypatch, existe: bool = True):
    """Le handler sur un store en mémoire. La relecture passe par le vrai formatage
    (`trial_iso`) de la date telle que le store la rend : texte sans fuseau
    (`db._conn._str_dict_row`)."""
    from oto_mcp.capabilities import org_suspension as cap
    box = {"fin": None}

    def _set(i, fin):
        change, box["fin"] = box["fin"] != fin, fin
        return change

    def _lire(i):
        fin = box["fin"]
        return org_store.trial_iso(
            fin.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            if fin else None)

    monkeypatch.setattr(cap.org_store, "get_org", lambda i: {"id": i} if existe else None)
    monkeypatch.setattr(cap.org_store, "set_trial_ends_at", _set)
    monkeypatch.setattr(cap.org_store, "get_trial_ends_at", _lire)
    return cap


# ── ce que fait le geste ─────────────────────────────────────────────────────

def test_poser_puis_effacer_est_idempotent(monkeypatch):
    from oto_mcp.capabilities._types import ResolvedCtx
    cap = _env(monkeypatch)
    ctx = ResolvedCtx(sub="service:m2m")
    out = cap._org_trial(ctx, cap.OrgTrialInput(org_id=7, ends_at=FIN))
    assert out == {"org_id": 7, "trial_ends_at": "2026-11-01T12:00:00+00:00",
                   "changed": True}
    again = cap._org_trial(ctx, cap.OrgTrialInput(org_id=7, ends_at=FIN))
    assert again["trial_ends_at"] == out["trial_ends_at"] and not again["changed"]
    # `null` efface : l'org a payé.
    clear = cap._org_trial(ctx, cap.OrgTrialInput(org_id=7, ends_at=None))
    assert clear == {"org_id": 7, "trial_ends_at": None, "changed": True}
    assert not cap._org_trial(ctx, cap.OrgTrialInput(org_id=7, ends_at=None))["changed"]


def test_ends_at_absent_vaut_null():
    from oto_mcp.capabilities import org_suspension as cap
    assert cap.OrgTrialInput(org_id=7).ends_at is None
    assert cap.OrgTrialInput(org_id=7, ends_at="2026-11-01T12:00:00Z").ends_at == FIN


def test_une_org_inconnue_est_un_404(monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    cap = _env(monkeypatch, existe=False)
    with pytest.raises(AuthzDenied) as e:
        cap._org_trial(ResolvedCtx(sub="op"), cap.OrgTrialInput(org_id=7, ends_at=FIN))
    assert (e.value.status, e.value.code) == (404, "unknown_org")


# ── qui passe ────────────────────────────────────────────────────────────────

def test_le_commerce_pose_par_son_identite_de_service():
    from _datastore_rest import cap
    from oto_mcp.auth import service_identity
    from oto_mcp.capabilities import org_suspension as caps
    from oto_mcp.capabilities._authz import COMMERCE_SERVICE
    from oto_mcp.capabilities._types import RawCtx
    c = cap("service.org.trial")
    [b] = c.rest_bindings()
    assert (b.verb, b.path) == ("POST", "/api/service/orgs/{id}/trial")
    assert c.authz is COMMERCE_SERVICE and c.mcp is None
    assert c.handler is caps._org_trial
    service_identity.set_current({"sub": "service:m2m", "client_id": "m2m",
                                  "roles": frozenset({"commerce"})})
    try:
        assert c.authz(RawCtx(sub="service:m2m")).role == "service:commerce"
    finally:
        service_identity.set_current(None)


def test_la_face_super_admin_partage_le_handler():
    """Le repli du commerce tant que son client M2M n'existe pas."""
    from _datastore_rest import cap
    from oto_mcp.capabilities import org_suspension as caps
    from oto_mcp.capabilities._authz import SUPER_ADMIN
    c = cap("admin.org_trial")
    [b] = c.rest_bindings()
    assert (b.verb, b.path) == ("POST", "/api/admin/orgs/{id}/trial")
    assert c.authz is SUPER_ADMIN and c.mcp is None
    assert c.handler is caps._org_trial is cap("service.org.trial").handler


def test_un_membre_ordinaire_ne_pose_pas(monkeypatch):
    """Ni par la face du commerce (pas d'identité de service), ni par celle du super
    admin (pas super admin)."""
    from _datastore_rest import cap
    from oto_mcp import access
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    for cle, code in (("service.org.trial", "service_required"),
                      ("admin.org_trial", None)):
        with pytest.raises(AuthzDenied) as e:
            cap(cle).authz(RawCtx(sub="u-membre"))
        assert e.value.status == 403
        if code:
            assert e.value.code == code


def test_le_geste_reste_ouvert_dans_une_org_suspendue():
    """Le commerce efface la date quand l'org paie — elle est alors souvent suspendue.
    La face de service n'a pas d'org d'appel ; la face super admin est une opération."""
    from oto_mcp import org_suspension
    assert org_suspension.ouverte("admin.org_trial")


# ── la fiche d'org la sert ───────────────────────────────────────────────────

def _fiche(monkeypatch, fin):
    from oto_mcp import access, billing, db
    from oto_mcp.capabilities.orgs import reads
    org = {"id": 7, "name": "Acme", "description": "", "domain": None, "industry": "",
           "location": "", "logo_url": None}
    monkeypatch.setattr(org_store, "get_org", lambda i: org if i == 7 else None)
    monkeypatch.setattr(org_store, "get_trial_ends_at",
                        lambda i: org_store.trial_iso(fin) if i == 7 else None)
    monkeypatch.setattr(org_store, "get_org_role", lambda i, sub: None)
    monkeypatch.setattr(org_store, "is_personal_org", lambda i: False)
    monkeypatch.setattr(org_store, "list_org_members", lambda i: [])
    monkeypatch.setattr(org_store, "list_org_secrets", lambda i: [])
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(db, "list_option_comps", lambda *a: [])
    monkeypatch.setattr(billing, "status", lambda i: {"subscribed": False})
    return reads


@pytest.mark.parametrize("fin, servi", [
    ("2026-11-01 12:00:00", "2026-11-01T12:00:00+00:00"),
    (None, None),
])
def test_admin_org_get_sert_la_fin_d_essai(monkeypatch, fin, servi):
    from oto_mcp.capabilities import admin_console as ac
    from oto_mcp.capabilities._types import ResolvedCtx
    reads = _fiche(monkeypatch, fin)
    out = ac._org(ResolvedCtx(sub="op"), ac.OrgAdminInput(op="get", org_id=7))
    assert out["org"]["trial_ends_at"] == servi
    # La même forme sur toutes les faces (REST `GET /api/admin/orgs/{id}`, `org.get`).
    assert reads.OrgBrief(**out["org"]).trial_ends_at == servi


def test_la_description_de_oto_admin_org_le_dit():
    from _datastore_rest import cap
    assert "trial_ends_at" in cap("admin.org").description


# ── sur base réelle ──────────────────────────────────────────────────────────

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


def test_poser_et_effacer_sur_base_reelle(live):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        org = conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                           (f"t-{uuid.uuid4().hex[:6]}",)).fetchone()["id"]
    assert org_store.get_trial_ends_at(org) is None, "une org neuve n'a pas d'essai"
    assert org_store.set_trial_ends_at(org, FIN) is True
    assert org_store.set_trial_ends_at(org, FIN) is False, "rejouer ne change rien"
    # Le même instant à un autre fuseau, ou sans fuseau (lu en UTC) : rien ne change.
    paris = FIN.astimezone(timezone(timedelta(hours=1)))
    assert org_store.set_trial_ends_at(org, paris) is False
    assert org_store.set_trial_ends_at(org, FIN.replace(tzinfo=None)) is False
    assert org_store.get_trial_ends_at(org) == "2026-11-01T12:00:00+00:00"
    assert org_store.set_trial_ends_at(org, None) is True
    assert org_store.set_trial_ends_at(org, None) is False
    assert org_store.get_trial_ends_at(org) is None
    assert org_store.get_trial_ends_at(10 ** 12) is None
    assert org_store.set_trial_ends_at(10 ** 12, FIN) is False, "org inconnue"
