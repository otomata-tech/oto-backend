"""L'état d'abonnement d'une org, poussé par le commerce (`service.org.subscription_state`).

Le commerce facture et garde l'abonnement `active` quand il ne peut pas débiter une
échéance (`block_code`). La fiche d'admin d'org (`oto_admin_org op=get`, bloc `billing`)
lisait la table de facturation du CŒUR, figée depuis la bascule : elle disait « actif,
rien de bloqué » d'une org dont plus rien ne se prélevait. Elle lit désormais l'état
POUSSÉ (`subscription_states`) ; tant que rien n'a été poussé, elle reste servie et
NOMME l'absence (`state: missing`) — `status = none` (aucun abonnement au commerce) est un
état, pas une absence. Aucun drapeau de facturation n'y touche : la route est toujours
montée, la fiche lit toujours l'état poussé.
"""
from __future__ import annotations

import contextlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import org_store
from oto_mcp.db import etat_abonnement

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
BLOQUE = {"status": "active", "block_code": "no_valid_mandate",
          "block_since": T0 - timedelta(days=33), "current_period_end": T0,
          "grace_until": None, "canceled_at": None, "emitted_at": T0}


def _iso(etat: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in etat.items()}


# ── le geste du commerce ─────────────────────────────────────────────────────

def test_la_route_est_celle_du_commerce_rest_seule():
    from _datastore_rest import cap
    from oto_mcp.capabilities import org_suspension as caps
    from oto_mcp.capabilities._authz import COMMERCE_SERVICE
    c = cap("service.org.subscription_state")
    [b] = c.rest_bindings()
    assert (b.verb, b.path) == ("PUT", "/api/service/orgs/{id}/subscription-state")
    assert c.authz is COMMERCE_SERVICE and c.mcp is None
    assert c.handler is caps._subscription_state


def _geste(monkeypatch, existe=True):
    from oto_mcp.capabilities import org_suspension as cap
    ecrit = []

    def _poser(org_id, etat):
        ecrit.append((org_id, etat))
        return {"org_id": org_id, **_iso(etat), "received_at": T0.isoformat()}, True
    monkeypatch.setattr(cap.org_store, "get_org", lambda i: {"id": i} if existe else None)
    monkeypatch.setattr(etat_abonnement, "poser", _poser)
    return cap, ecrit


def _refus(cap, **champs):
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    with pytest.raises(AuthzDenied) as e:
        cap._subscription_state(ResolvedCtx(sub="service:m2m"),
                                cap.SubscriptionStateInput(org_id=7, **champs))
    return e.value.status, e.value.code


def test_le_commerce_pose_un_etat_bloque(monkeypatch):
    from oto_mcp.capabilities._types import ResolvedCtx
    cap, ecrit = _geste(monkeypatch)
    out = cap._subscription_state(ResolvedCtx(sub="service:m2m"),
                                  cap.SubscriptionStateInput(org_id=7, **BLOQUE))
    assert (out["status"], out["block_code"], out["changed"]) == (
        "active", "no_valid_mandate", True)
    assert ecrit == [(7, BLOQUE)]
    cap.SubscriptionStateOut.model_validate(out)


def test_aucun_abonnement_se_pousse_explicitement(monkeypatch):
    from oto_mcp.capabilities._types import ResolvedCtx
    cap, ecrit = _geste(monkeypatch)
    cap._subscription_state(ResolvedCtx(sub="service:m2m"),
                            cap.SubscriptionStateInput(org_id=7, status="none", emitted_at=T0))
    assert ecrit[0][1]["status"] == "none"


def test_refus_nommes_et_rien_n_est_ecrit(monkeypatch):
    cap, ecrit = _geste(monkeypatch)
    assert _refus(cap, **{**BLOQUE, "emitted_at": "2026-11-16T12:00:00"}) == (
        422, "datetime_without_timezone")
    assert _refus(cap, **{**BLOQUE, "block_since": None}) == (422, "block_since_mismatch")
    assert _refus(cap, status="active", block_since=T0, emitted_at=T0) == (
        422, "block_since_mismatch")
    assert ecrit == []
    cap, ecrit = _geste(monkeypatch, existe=False)
    assert _refus(cap, **BLOQUE) == (404, "unknown_org")
    assert ecrit == []


def test_un_envoi_plus_ancien_est_un_409(monkeypatch):
    cap, _ = _geste(monkeypatch)

    def _perime(org_id, etat):
        raise etat_abonnement.EtatPerime(T0, etat["emitted_at"])
    monkeypatch.setattr(etat_abonnement, "poser", _perime)
    assert _refus(cap, **{**BLOQUE, "emitted_at": T0 - timedelta(seconds=1)}) == (
        409, "stale_subscription_state")


# ── la fiche d'admin lit l'état poussé, jamais la table figée ────────────────

def _fiche(monkeypatch, etat, *, drapeau="0"):
    from oto_mcp import access, billing, db
    from oto_mcp.capabilities.orgs import reads
    org = {"id": 7, "name": "Acme", "description": "", "domain": None, "industry": "",
           "location": "", "logo_url": None}
    # Le drapeau de l'ancienne facturation du cœur ne décide de rien ici.
    monkeypatch.setenv("OTO_BILLING_ENABLED", drapeau)
    monkeypatch.setattr(org_store, "get_org", lambda i: org if i == 7 else None)
    monkeypatch.setattr(org_store, "get_trial_ends_at", lambda i: None)
    monkeypatch.setattr(org_store, "get_org_role", lambda i, sub: None)
    monkeypatch.setattr(org_store, "is_personal_org", lambda i: False)
    monkeypatch.setattr(org_store, "list_org_members", lambda i: [])
    monkeypatch.setattr(org_store, "list_org_members_with_users", lambda i: [])
    monkeypatch.setattr(org_store, "list_org_secrets", lambda i: [])
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    monkeypatch.setattr(db, "list_option_comps", lambda *a: [])
    monkeypatch.setattr(db, "_connect", lambda: contextlib.nullcontext("conn"))

    def _figee(i):
        raise AssertionError("la fiche d'admin a lu la facturation figée du cœur")
    monkeypatch.setattr(billing, "status", _figee)
    lus = []

    def _lire(conn, i):
        lus.append(i)
        if etat is None:
            raise etat_abonnement.EtatJamaisPousse(i)
        return {"org_id": i, **_iso(etat), "received_at": T0.isoformat()}
    monkeypatch.setattr(etat_abonnement, "lire_etat", _lire)
    from oto_mcp.db import entitlements as db_entitlements
    monkeypatch.setattr(db_entitlements, "lignes_filtrees", lambda **_kw: [])
    return reads, lus


def _get(monkeypatch, etat, **kw):
    from oto_mcp.capabilities import admin_console as ac
    from oto_mcp.capabilities._types import ResolvedCtx
    reads, lus = _fiche(monkeypatch, etat, **kw)
    out = ac._org(ResolvedCtx(sub="op"), ac.OrgAdminInput(op="get", org_id=7))
    reads.OrgAdminDetail.model_validate(out)
    # La fiche est ENTIÈRE dans les deux formes.
    assert out["org"]["id"] == 7 and "members" in out and "entitlements" in out
    return out["billing"], lus


def test_etat_pousse_une_org_bloquee_ne_se_lit_plus_active_sans_blocage(monkeypatch):
    b, _ = _get(monkeypatch, BLOQUE)
    assert b == {"source": "commerce", "state": "pushed", "subscribed": True,
                 "status": "active", "block_code": "no_valid_mandate",
                 "block_since": (T0 - timedelta(days=33)).isoformat(),
                 "current_period_end": T0.isoformat(), "grace_until": None,
                 "canceled_at": None, "emitted_at": T0.isoformat(),
                 "received_at": T0.isoformat()}


def test_etat_pousse_aucun_abonnement_au_commerce(monkeypatch):
    etat = {k: None for k in BLOQUE} | {"status": "none", "emitted_at": T0}
    b, _ = _get(monkeypatch, etat)
    assert (b["state"], b["status"], b["subscribed"], b["block_code"]) == (
        "pushed", "none", False, None)


def test_rien_pousse_la_fiche_est_rendue_et_nomme_l_absence_sans_rien_deviner(monkeypatch):
    b, lus = _get(monkeypatch, None)
    assert lus == [7]
    # `subscribed: false` pour la console d'avant ; `state` nomme l'absence, rien d'autre.
    assert b == {"source": "commerce", "state": "missing",
                 "code": "subscription_state_missing", "subscribed": False}


def test_le_bloc_d_admin_garde_la_forme_lue_par_la_console_d_avant():
    """Le contrat épinglé de la console lit `OrgBilling` sous `billing` : `subscribed`
    booléen toujours présent, et les champs d'avant déclarés (jamais servis)."""
    from oto_mcp.capabilities.orgs.reads import OrgAdminBilling, OrgBilling

    admin = OrgAdminBilling.model_json_schema()
    avant = OrgBilling.model_json_schema()
    assert "subscribed" in admin["required"]
    assert admin["properties"]["subscribed"] == avant["properties"]["subscribed"]
    for champ, forme in avant["properties"].items():
        assert admin["properties"].get(champ) == forme, champ


@pytest.mark.parametrize("drapeau", ["0", "1"])
def test_le_drapeau_de_facturation_ne_change_rien_a_la_fiche(monkeypatch, drapeau):
    b, lus = _get(monkeypatch, BLOQUE, drapeau=drapeau)
    assert lus == [7] and b["state"] == "pushed"
    b, lus = _get(monkeypatch, None, drapeau=drapeau)
    assert lus == [7] and b["state"] == "missing"


@pytest.mark.parametrize("drapeau", ["0", "1"])
def test_la_route_est_toujours_montee(monkeypatch, drapeau):
    from _datastore_rest import cap
    c = cap("service.org.subscription_state")
    monkeypatch.setenv("OTO_BILLING_ENABLED", drapeau)
    assert c.gate is None and c.is_exposed()


def test_aucun_refus_sur_la_fiche_et_le_texte_servi_dit_les_deux_formes():
    from _datastore_rest import cap
    for cle in ("admin.org", "org.admin.get"):
        assert "subscription_state_missing" not in {d.code for d in cap(cle).errors}, cle
    d = cap("admin.org").description
    for mot in ("PUSHED by commerce", "state: pushed", "state: missing",
                "subscription_state_missing", "block_code"):
        assert mot in d, mot
    assert "state: disabled" not in d


@pytest.mark.parametrize("etat, preleve", [
    ({"status": "active", "canceled_at": None}, True),
    ({"status": "past_due", "canceled_at": None}, True),
    ({"status": "active", "canceled_at": "2026-11-01T00:00:00+00:00"}, False),
    ({"status": "none", "canceled_at": None}, False),
    ({"status": "incomplete", "canceled_at": None}, False),
    ({"status": "canceled", "canceled_at": "2026-11-01T00:00:00+00:00"}, False),
])
def test_preleve_encore(etat, preleve):
    assert etat_abonnement.preleve_encore(etat) is preleve


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


def _org():
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (f"t-{uuid.uuid4().hex[:6]}",)).fetchone()["id"]


def test_poser_relire_perime_et_rejeu_sur_base_reelle(live):
    from oto_mcp.db._conn import _connect
    org = _org()
    with _connect() as conn, pytest.raises(etat_abonnement.EtatJamaisPousse):
        etat_abonnement.lire_etat(conn, org)
    etat, change = etat_abonnement.poser(org, BLOQUE)
    assert change and etat["status"] == "active" and etat["block_code"] == "no_valid_mandate"
    assert etat["block_since"] == (T0 - timedelta(days=33)).isoformat()
    # Le même envoi rejoué : accepté, rien n'a bougé.
    assert etat_abonnement.poser(org, BLOQUE)[1] is False
    # Plus ancien : refusé, rien n'est écrit.
    with pytest.raises(etat_abonnement.EtatPerime):
        etat_abonnement.poser(org, {**BLOQUE, "block_code": None, "block_since": None,
                                    "emitted_at": T0 - timedelta(minutes=1)})
    with _connect() as conn:
        assert etat_abonnement.lire_etat(conn, org)["block_code"] == "no_valid_mandate"
    # Plus récent : le blocage est levé.
    leve = {**BLOQUE, "block_code": None, "block_since": None,
            "emitted_at": T0 + timedelta(minutes=1)}
    etat, change = etat_abonnement.poser(org, leve)
    assert change and etat["block_code"] is None
    # « Aucun abonnement » est un état.
    aucun = {k: None for k in BLOQUE} | {"status": "none", "emitted_at": T0 + timedelta(hours=1)}
    etat_abonnement.poser(org, aucun)
    with _connect() as conn:
        lu = etat_abonnement.lire_etat(conn, org)
    assert lu["status"] == "none" and not etat_abonnement.preleve_encore(lu)


def test_la_fiche_d_admin_ne_lit_plus_org_subscriptions(live, monkeypatch):
    import psycopg
    from unittest import mock
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import reads
    org = _org()
    etat_abonnement.poser(org, BLOQUE)
    vues: list[str] = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(str(query))
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        fiche = reads._org_admin_detail(ResolvedCtx(sub="op"), reads.OrgIdInput(org_id=org))
    assert any("subscription_states" in q for q in vues), "l'instrument regarde à côté"
    assert not [q for q in vues if "org_subscriptions" in q]
    assert (fiche["billing"]["status"], fiche["billing"]["block_code"]) == (
        "active", "no_valid_mandate")


# ── la fiche et la liste, par la vraie route REST, pour un service consommateur ─
# Un service consommateur lit ces champs de `GET /api/admin/orgs/{id}` (`org.name`,
# `members[].sub|email|suspended`) et de `GET /api/admin/orgs` (`orgs[].id|created_by|
# created_at`). Ces deux routes ne sont pas couvertes par la garde de contrat : ce test
# tient ces champs, quelle que soit la forme du bloc `billing`.

OPERATEUR = "op-fiche-service"


def _org_peuplee() -> tuple[int, str, str]:
    """Une org avec un membre actif et un membre suspendu, créée par l'opérateur."""
    from oto_mcp.db import users
    from oto_mcp.db._conn import _connect
    actif, suspendu = (f"u-{uuid.uuid4().hex[:8]}" for _ in range(2))
    for sub in (OPERATEUR, actif, suspendu):
        users.upsert_user(sub, email=f"{sub}@example.test")
    nom = f"t-{uuid.uuid4().hex[:6]}"
    with _connect() as conn:
        org = conn.execute("INSERT INTO orgs (name, created_by) VALUES (%s, %s) "
                           "RETURNING id", (nom, OPERATEUR)).fetchone()["id"]
        conn.execute("UPDATE users SET suspended_at = NOW() WHERE sub = %s", (suspendu,))
    org_store.add_org_member(org, actif, "org_admin", actor=OPERATEUR)
    org_store.add_org_member(org, suspendu, "org_member", actor=OPERATEUR)
    return org, actif, suspendu


def _ce_que_lit_le_service(corps: dict) -> tuple:
    return (corps["org"]["name"],
            sorted((m["sub"], m["email"], m["suspended"]) for m in corps["members"]))


def test_la_fiche_rest_garde_ce_que_lit_un_service_sous_les_deux_formes(live, monkeypatch):
    from _datastore_rest import call
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", OPERATEUR)
    org, actif, suspendu = _org_peuplee()

    def _fiche_rest() -> dict:
        statut, corps = call("org.admin.get", path_params={"id": str(org)}, sub=OPERATEUR)
        assert statut == 200, corps
        return corps

    rien_pousse = _fiche_rest()
    etat_abonnement.poser(org, BLOQUE)
    pousse = _fiche_rest()

    assert rien_pousse["billing"] == {"source": "commerce", "state": "missing",
                                      "code": "subscription_state_missing",
                                      "subscribed": False}
    assert (pousse["billing"]["state"], pousse["billing"]["block_code"]) == (
        "pushed", "no_valid_mandate")
    attendu = (rien_pousse["org"]["name"], sorted([
        (actif, f"{actif}@example.test", False),
        (suspendu, f"{suspendu}@example.test", True)]))
    for corps in (rien_pousse, pousse):
        assert _ce_que_lit_le_service(corps) == attendu


def test_la_liste_rest_garde_id_created_by_created_at(live, monkeypatch):
    from _datastore_rest import call
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", OPERATEUR)
    org, _, _ = _org_peuplee()
    statut, corps = call("org.admin.list", sub=OPERATEUR)
    assert statut == 200, corps
    [ligne] = [o for o in corps["orgs"] if o["id"] == org]
    assert ligne["created_by"] == OPERATEUR
    # Forme servie aujourd'hui : `AAAA-MM-JJ HH:MM:SS`, sans fuseau. On tient sa
    # présence et sa lecture, pas un fuseau qu'elle ne porte pas.
    assert datetime.fromisoformat(ligne["created_at"])
