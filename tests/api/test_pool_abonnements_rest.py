"""Le POOL d'org des abonnements, SUR LES ROUTES et sur une vraie base (25/09/2026).

1. **le mode de l'org** — `PUT /api/orgs/{id}/model-subscriptions/{family}/mode`
   (admin d'org), lu par le `GET` de la ressource avec `pool_size` (membre) ;
2. **le prêt** — `PATCH /api/me/model-subscriptions/{family}` `{"lent_to": [...]}` :
   opt-in, par org, aux seules orgs dont on est membre, rendu par la liste.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

_F = "claude_subscription"
_ME = "/api/me/model-subscriptions"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@pool.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_pool_rest_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(),
                                                              mcp_instance=None)))


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    admin, membre, dehors = "usr_pool_admin", "usr_pool_membre", "usr_pool_dehors"
    for sub in (admin, membre, dehors):
        db.upsert_user(sub, email=f"{sub}@pool.invalid", name=sub)
    oid = org_store.create_org("Org du pool", created_by=admin)
    autre = org_store.create_org("Autre org", created_by=dehors)
    org_store.add_org_member(oid, admin, "org_admin")
    org_store.add_org_member(oid, membre, "org_member")
    org_store.add_org_member(autre, dehors, "org_admin")
    return {"id": oid, "autre": autre, "admin": admin, "membre": membre,
            "dehors": dehors,
            "route": f"/api/orgs/{oid}/model-subscriptions/{_F}",
            "mode": f"/api/orgs/{oid}/model-subscriptions/{_F}/mode"}


@pytest.fixture
def option(monkeypatch):
    """Prêter exige l'option `claude_subscription`, comme se connecter."""
    from oto_mcp.capabilities import _abonnement
    ouverts = set()
    monkeypatch.setattr(_abonnement.access, "has_option",
                        lambda sub, opt, **k: opt == _abonnement.OPTION and sub in ouverts)
    return ouverts


@pytest.fixture
def abonne(org):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    moi = org["membre"]
    US.upsert_sandbox(moi, _F, f"sandbox-{moi}")
    US.marquer_statut(moi, _F, US.CONNECTE, ok=True)
    P.poser_prets(moi, _F, [])
    return org


# ── le mode de l'org ─────────────────────────────────────────────────────────
def test_le_mode_par_defaut_est_personnel(client, org):
    from oto_mcp.db import org_subscription_pool as P
    P.poser_mode(org["id"], _F, "personnel", org["admin"])
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert (lu["mode"], lu["pool_size"]) == ("personnel", 0)


def test_un_admin_passe_l_org_en_pool_et_un_membre_le_lit(client, org, abonne, option):
    o = org
    r = client.put(o["mode"], json={"mode": "pool"}, headers=_h(o["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "pool" and r.json()["pool_size"] == 0
    option.add(o["membre"])
    client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))
    lu = client.get(o["route"], headers=_h(o["membre"])).json()
    assert (lu["mode"], lu["pool_size"]) == ("pool", 1)
    assert lu["limit_pct"] == 80, "le plafond n'a pas bougé : un geste par réglage"


def test_la_lecture_dit_QUI_prete_et_dans_quel_etat(client, org, abonne, option):
    """Une ligne par prêteur, en tout mode : qui partage, et s'il est utilisable
    maintenant — jamais son palier ni son usage (décision du 08/10/2026). L'org_admin
    seul lit en plus le plafond effectif et POURQUOI le forfait ne sert pas (`state`,
    22/11/2026, `test_forfaits_de_l_org_db.py`) ; un membre, rien de plus qu'avant."""
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    o = org
    P.poser_mode(o["id"], _F, "personnel", o["admin"])
    option.add(o["membre"])
    client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))

    lu = client.get(o["route"], headers=_h(o["admin"])).json()
    # l'org_admin lit le plafond effectif de chaque prêteur (un réglage, pas l'usage)
    # et son état (`ok` ⇔ `usable_now`) — jamais son palier ni son usage mesuré
    (preteur,) = lu["lenders"]
    assert {k: preteur[k] for k in ("sub", "usable_now", "limit_pct", "state",
                                    "reset_at")} == {
        "sub": o["membre"], "usable_now": True, "limit_pct": lu["limit_pct"],
        "state": "ok", "reset_at": None}
    assert set(preteur) == {"sub", "usable_now", "limit_pct", "state", "last_ok_at",
                            "reset_at"}
    assert lu["pool_size"] == 1
    # un membre ne lit que qui prête et s'il est utilisable — jamais le plafond d'autrui
    assert client.get(o["route"], headers=_h(o["membre"])).json()["lenders"] == \
        [{"sub": o["membre"], "usable_now": True}]

    US.marquer_statut(o["membre"], _F, US.DECONNECTE)
    lu = client.get(o["route"], headers=_h(o["membre"])).json()
    assert lu["lenders"] == [{"sub": o["membre"], "usable_now": False}], \
        "un prêteur inutilisable reste une ligne, sans dire pourquoi"
    assert lu["pool_size"] == 0, "pool_size = le compte des utilisables"

    client.patch(f"{_ME}/{_F}", json={"lent_to": []}, headers=_h(o["membre"]))
    assert client.get(o["route"], headers=_h(o["admin"])).json()["lenders"] == []


def test_l_admin_lit_le_plafond_perso_du_preteur_le_membre_non(client, abonne, option):
    """Le plafond PERSO remplace celui de l'org, plus haut OU plus bas
    (`_abonnement.seuil`) : l'admin lit l'effectif ; retiré, celui de l'org revient."""
    o = abonne
    option.add(o["membre"])
    client.put(o["route"], json={"limit_pct": 60}, headers=_h(o["admin"]))
    client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))
    try:
        lu = client.get(o["route"], headers=_h(o["admin"])).json()
        assert lu["lenders"][0]["limit_pct"] == 60, "sans réglage perso : celui de l'org"

        client.patch(f"{_ME}/{_F}", json={"limit_pct": 90}, headers=_h(o["membre"]))
        lu = client.get(o["route"], headers=_h(o["admin"])).json()
        assert lu["lenders"][0]["limit_pct"] == 90, "le perso remplace, même plus haut"

        assert "limit_pct" not in \
            client.get(o["route"], headers=_h(o["membre"])).json()["lenders"][0]
    finally:
        client.patch(f"{_ME}/{_F}", json={"lent_to": [], "limit_pct": None},
                     headers=_h(o["membre"]))
        client.put(o["route"], json={"limit_pct": None}, headers=_h(o["admin"]))


def test_au_plafond_l_echeance_decide_sans_etre_servie(client, org, abonne, option):
    """« Au plafond jusqu'à … » dirait à toute l'org l'usage TOTAL d'un compte : le
    plafond décide de `usable_now`, son échéance n'est jamais servie."""
    from datetime import datetime, timedelta, timezone

    from oto_mcp.db import user_subscriptions as US
    o = org
    option.add(o["membre"])
    client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))
    try:
        demain = datetime.now(timezone.utc) + timedelta(days=1)
        US.marquer_statut(o["membre"], _F, US.PLAFOND, limit_reset_at=demain)
        lu = client.get(o["route"], headers=_h(o["membre"])).json()
        assert lu["lenders"] == [{"sub": o["membre"], "usable_now": False}]
        assert lu["pool_size"] == 0
        assert str(demain.year) not in str(lu["lenders"]), "l'échéance ne sort pas"

        hier = datetime.now(timezone.utc) - timedelta(hours=1)
        US.marquer_statut(o["membre"], _F, US.PLAFOND, limit_reset_at=hier)
        lu = client.get(o["route"], headers=_h(o["membre"])).json()
        assert lu["lenders"] == [{"sub": o["membre"], "usable_now": True}]
        assert lu["pool_size"] == 1
    finally:
        client.patch(f"{_ME}/{_F}", json={"lent_to": []}, headers=_h(o["membre"]))


def test_un_MEMBRE_ne_change_pas_le_mode(client, org):
    from oto_mcp.db import org_subscription_pool as P
    P.poser_mode(org["id"], _F, "personnel", org["admin"])
    r = client.put(org["mode"], json={"mode": "pool"}, headers=_h(org["membre"]))
    assert r.status_code == 403, r.text
    assert P.get_mode(org["id"], _F)["mode"] == "personnel"


def test_un_mode_hors_contrat_400(client, org):
    r = client.put(org["mode"], json={"mode": "tous"}, headers=_h(org["admin"]))
    assert r.status_code == 400, r.text


def test_mode_famille_inconnue_400(client, org):
    r = client.put(f"/api/orgs/{org['id']}/model-subscriptions/anthropic/mode",
                   json={"mode": "pool"}, headers=_h(org["admin"]))
    assert r.status_code == 400 and r.json()["error"] == "unknown_family"


# ── le prêt ──────────────────────────────────────────────────────────────────
def test_je_prete_a_MON_org_et_la_liste_le_rend(client, abonne, option):
    o = abonne
    option.add(o["membre"])
    r = client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))
    assert r.status_code == 200, r.text
    assert r.json()["lent_to"] == [o["id"]]
    (mien,) = client.get(_ME, headers=_h(o["membre"])).json()["subscriptions"]
    assert mien["lent_to"] == [o["id"]]


def test_j_ouvre_mon_abonnement_dans_MON_org_seulement(client, abonne, option):
    """08/10/2026 : `used_in` dit où l'abonnement sert mes agents. Une org dont je
    ne suis pas membre est refusée ; la liste se remplace, et se vide."""
    o = abonne
    option.add(o["membre"])
    r = client.patch(f"{_ME}/{_F}", json={"used_in": [o["id"]]}, headers=_h(o["membre"]))
    assert r.status_code == 200 and r.json()["used_in"] == [o["id"]], r.text
    (mien,) = client.get(_ME, headers=_h(o["membre"])).json()["subscriptions"]
    assert mien["used_in"] == [o["id"]]

    r = client.patch(f"{_ME}/{_F}", json={"used_in": [o["autre"]]}, headers=_h(o["membre"]))
    assert r.status_code == 403 and r.json()["error"] == "not_org_member", r.text

    r = client.patch(f"{_ME}/{_F}", json={"used_in": []}, headers=_h(o["membre"]))
    assert r.status_code == 200 and r.json()["used_in"] == [], r.text


def test_une_org_quittee_sort_de_used_in_et_ne_bloque_pas_le_patch(client, abonne, option):
    """Revue du 08/10/2026 : ouvert dans A et B, puis sorti de B — `used_in` ne rend
    plus B (elle ne sert plus), et le front qui renvoie l'ensemble plus une org C
    dont la personne est membre n'est pas refusé à cause de B."""
    from oto_mcp import org_store
    o = abonne
    option.add(o["membre"])
    b = org_store.create_org("Org B quittée", created_by=o["admin"])
    c = org_store.create_org("Org C", created_by=o["admin"])
    org_store.add_org_member(b, o["membre"], "org_member")
    org_store.add_org_member(c, o["membre"], "org_member")
    r = client.patch(f"{_ME}/{_F}", json={"used_in": [o["id"], b]}, headers=_h(o["membre"]))
    assert r.status_code == 200 and r.json()["used_in"] == sorted([o["id"], b]), r.text

    org_store.remove_org_member(b, o["membre"])
    (mien,) = client.get(_ME, headers=_h(o["membre"])).json()["subscriptions"]
    assert mien["used_in"] == [o["id"]], "une org quittée ne sert plus, ne se rend plus"

    envoye = mien["used_in"] + [c]
    r = client.patch(f"{_ME}/{_F}", json={"used_in": envoye}, headers=_h(o["membre"]))
    assert r.status_code == 200 and r.json()["used_in"] == sorted(envoye), r.text
    client.patch(f"{_ME}/{_F}", json={"used_in": []}, headers=_h(o["membre"]))


def test_rien_n_est_prete_par_defaut(client, abonne):
    (mien,) = client.get(_ME, headers=_h(abonne["membre"])).json()["subscriptions"]
    assert mien["lent_to"] == []


def test_je_retire_mon_pret(client, abonne, option):
    o = abonne
    option.add(o["membre"])
    client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))
    r = client.patch(f"{_ME}/{_F}", json={"lent_to": []}, headers=_h(o["membre"]))
    assert r.status_code == 200 and r.json()["lent_to"] == [], r.text


def test_retirer_ne_demande_pas_l_option(client, abonne, option):
    """Prêter est réservé aux personnes nommées ; se retirer, jamais."""
    r = client.patch(f"{_ME}/{_F}", json={"lent_to": []}, headers=_h(abonne["membre"]))
    assert r.status_code == 200, r.text


def test_preter_sans_l_option_403(client, abonne, option):
    r = client.patch(f"{_ME}/{_F}", json={"lent_to": [abonne["id"]]},
                     headers=_h(abonne["membre"]))
    assert r.status_code == 403 and r.json()["error"] == "subscription_not_enabled"


def test_on_ne_prete_pas_a_une_org_dont_on_n_est_pas_membre(client, abonne, option):
    from oto_mcp.db import org_subscription_pool as P
    o = abonne
    option.add(o["membre"])
    r = client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"], o["autre"]]},
                     headers=_h(o["membre"]))
    assert r.status_code == 403 and r.json()["error"] == "not_org_member", r.text
    assert P.orgs_pretees(o["membre"], _F) == [], "rien d'écrit sur un refus"


def test_le_plafond_seul_ne_touche_pas_aux_prets(client, abonne, option):
    o = abonne
    option.add(o["membre"])
    client.patch(f"{_ME}/{_F}", json={"lent_to": [o["id"]]}, headers=_h(o["membre"]))
    r = client.patch(f"{_ME}/{_F}", json={"limit_pct": 40}, headers=_h(o["membre"]))
    assert r.status_code == 200, r.text
    assert (r.json()["limit_pct"], r.json()["lent_to"]) == (40, [o["id"]])


def test_preter_sans_abonnement_404(client, org, option):
    option.add(org["admin"])
    r = client.patch(f"{_ME}/{_F}", json={"lent_to": [org["id"]]}, headers=_h(org["admin"]))
    assert r.status_code == 404 and r.json()["error"] == "not_connected", r.text


def test_corps_vide_400_nothing_to_change(client, abonne):
    r = client.patch(f"{_ME}/{_F}", json={}, headers=_h(abonne["membre"]))
    assert r.status_code == 400 and r.json()["error"] == "nothing_to_change", r.text


def test_la_console_regle_le_mode_seul(live, org):
    """Face MCP (`oto_org_settings`) : le mode se règle seul, un `limit_pct` à côté
    est refusé — sur cette face, omis et `null` arrivent identiques."""
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    ctx = ResolvedCtx(sub=org["admin"], org_id=org["id"])
    S = oc.OrgSettingsInput
    out = oc._org_settings(ctx, S(op="set", domain="model_subscriptions",
                                  org_id=org["id"], family=_F, mode="pool"))
    assert out["mode"] == "pool"
    for inp, code in ((S(op="set", domain="model_subscriptions", org_id=org["id"],
                         family=_F, mode="pool", limit_pct=50), "one_setting_per_call"),
                      (S(op="set", domain="model_subscriptions", org_id=org["id"],
                         family=_F, mode="tous"), "invalid_mode")):
        with pytest.raises(AuthzDenied) as e:
            oc._org_settings(ctx, inp)
        assert e.value.code == code
