"""L'API d'administration du commerce, sous l'identité de service (#1069).

Deux moitiés :
- **qui passe** : chaque capacité `service.*` ouvre l'authentification au service et
  passe sa règle ; un compte, même super admin, est refusé ;
- **ce qu'elles font**, sur une vraie base : membres par ancienneté, usage sur une
  fenêtre, pose idempotente, retrait d'une seule ligne, refus nommés.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import access, db, org_store
from oto_mcp.auth import service_identity
from oto_mcp.capabilities import _authz, registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.db._conn import _connect

_SERVICE = {"sub": "service:m2m-commerce", "client_id": "m2m-commerce",
            "roles": frozenset({"commerce"})}
_CLES = ("service.orgs.list", "service.org.members", "service.org.usage",
         "service.org.entitlements.list", "service.org.entitlement.put",
         "service.org.entitlement.delete", "service.billing.export", "service.users.get",
         "service.user.entitlements.list", "service.user.entitlement.put",
         "service.user.entitlement.delete", "service.user.entitlement.effective",
         "service.org.suspension")


def _cap(cle):
    return next(c for c in registry.CAPABILITIES if c.key == cle)


def _appel(cle, **champs):
    cap = _cap(cle)
    ctx = ResolvedCtx(sub=_SERVICE["sub"], org_id=None, role="service:commerce")
    return cap.handler(ctx, cap.Input(**champs))


@pytest.fixture(autouse=True)
def _nettoie():
    yield
    service_identity.set_current(None)


# ── qui passe ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cle", _CLES)
def test_chaque_capacite_est_ouverte_au_service_et_a_lui_seul(cle, monkeypatch):
    cap = _cap(cle)
    assert cap.mcp is None, "un tuyau de service, pas un outil d'agent"
    assert _authz.accepts_service(cap.authz)
    service_identity.set_current(dict(_SERVICE))
    assert cap.authz(RawCtx(sub=_SERVICE["sub"])).role == "service:commerce"
    service_identity.set_current(None)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: True)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    with pytest.raises(AuthzDenied) as refus:
        cap.authz(RawCtx(sub="u-super"))
    assert refus.value.code == "service_required"


def test_une_date_sans_fuseau_sort_en_utc_explicite():
    """#1073 : une colonne de la base servie rendait `created_at` sans fuseau, et le
    commerce ne pouvait pas la comparer à une date réelle."""
    from oto_mcp.capabilities import service_commerce as sc
    assert sc._iso("2026-06-10 23:18:58") == "2026-06-10T23:18:58+00:00", \
        "la forme texte du store du cœur"
    assert sc._iso(datetime(2026, 9, 25, 11, 0)) == "2026-09-25T11:00:00+00:00"
    paris = timezone(timedelta(hours=2))
    assert sc._iso(datetime(2026, 9, 25, 13, 0, tzinfo=paris)) == "2026-09-25T13:00:00+02:00"
    assert sc._iso(None) is None


# ── sur une vraie base ───────────────────────────────────────────────────────

def _org(archivee: bool = False) -> int:
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO orgs (name, archived_at) VALUES (%s, %s) RETURNING id",
            (f"org-{uuid.uuid4().hex[:8]}",
             datetime.now(timezone.utc) if archivee else None)).fetchone()["id"]


def _membre(org: int, joined_at: datetime) -> str:
    sub = f"u-{uuid.uuid4().hex[:8]}"
    db.upsert_user(sub, email=f"{sub}@exemple.test")
    org_store.add_org_member(org, sub, "org_member")
    with _connect() as conn:
        conn.execute("UPDATE org_members SET joined_at = %s WHERE org_id = %s AND sub = %s",
                     (joined_at, org, sub))
    return sub


def _appel_journal(org: int, sub: str, quand: datetime, *, ok=True, key_mode=None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tool_calls (created_at, kind, sub, tool, ok, org_id, key_mode) "
            "VALUES (%s, 'mcp', %s, 'outil_x', %s, %s, %s)",
            (quand, sub, ok, org, key_mode))


def _refus(cle, **champs) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as refus:
        _appel(cle, **champs)
    return refus.value


T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_les_membres_sortent_par_anciennete_avec_leur_derniere_activite(live):
    org = _org()
    cadet = _membre(org, T0 + timedelta(days=5))
    aine = _membre(org, T0)
    _appel_journal(org, cadet, T0 + timedelta(days=6))
    out = _appel("service.org.members", org_id=org)
    assert [m["sub"] for m in out["members"]] == [aine, cadet]
    assert out["members"][0]["last_activity_at"] is None
    assert out["members"][1]["last_activity_at"].startswith("2026-09-07")
    assert out["members"][0]["joined_at"] == T0.isoformat(), "servie avec son fuseau"
    assert out["members"][0]["email"] == f"{aine}@exemple.test"


def test_une_org_archivee_ou_inconnue_est_un_404_nomme(live):
    assert _refus("service.org.members", org_id=_org(archivee=True)).code == "unknown_org"
    assert _refus("service.org.entitlements.list", org_id=10**12).code == "unknown_org"


def test_la_liste_des_orgs_se_pagine_par_curseur_sans_les_archivees(live):
    a, b, archivee = _org(), _org(), _org(archivee=True)
    page = _appel("service.orgs.list", after_id=a - 1, limit=1)
    assert [o["id"] for o in page["orgs"]] == [a] and page["next_after_id"] == a
    assert datetime.fromisoformat(page["orgs"][0]["created_at"]).tzinfo is not None
    suite = _appel("service.orgs.list", after_id=a, limit=1000)
    ids = [o["id"] for o in suite["orgs"]]
    assert b in ids and archivee not in ids and suite["next_after_id"] is None


def test_chaque_org_porte_son_tenant(live):
    """#1072 : l'org d'un tenant tiers se reconnaît dans la liste — le commerce ne doit
    rien lui adresser."""
    from oto_mcp import tenancy
    a_nous, chez_un_tiers = _org(), _org()
    slug = f"t{uuid.uuid4().hex[:8]}"
    with _connect() as conn:
        tid = conn.execute(
            "INSERT INTO tenants (slug, name, issuer, jwks_uri) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (slug, slug, f"https://{slug}.exemple.test/oidc",
             f"https://{slug}.exemple.test/oidc/jwks")).fetchone()["id"]
        conn.execute("UPDATE orgs SET tenant_id = %s WHERE id = %s", (tid, chez_un_tiers))
    page = _appel("service.orgs.list", after_id=a_nous - 1, limit=2)
    assert {o["id"]: o["tenant"] for o in page["orgs"]} == {
        a_nous: tenancy.primary_slug(), chez_un_tiers: slug}


def test_l_usage_compte_les_reussites_de_la_fenetre_et_les_cles_de_plateforme(live):
    org = _org()
    sub = _membre(org, T0)
    dans = T0 + timedelta(days=2)
    _appel_journal(org, sub, dans)
    _appel_journal(org, sub, dans, key_mode="platform")
    _appel_journal(org, sub, dans, ok=False)                   # un échec ne compte pas
    _appel_journal(org, sub, T0 + timedelta(days=40))          # hors fenêtre
    _consolider_le_journal()
    out = _appel("service.org.usage", org_id=org, since=T0, until=T0 + timedelta(days=30))
    assert out["by_person"] == [{"sub": sub, "calls": 2, "platform_calls": 1}]
    assert _refus("service.org.usage", org_id=org, since=T0, until=T0).code == "invalid_window"


def test_la_pose_est_idempotente_et_nomme_le_service(live):
    org = _org()
    sub = _membre(org, T0)
    for valeur in (1, 1, 0):
        ligne = _appel("service.org.entitlement.put", org_id=org, right_key="unipile",
                       source="trial", value=valeur, sub=sub)
    assert (ligne["value"], ligne["sub"], ligne["granted_by"]) == (0, sub, _SERVICE["sub"])
    lignes = _appel("service.org.entitlements.list", org_id=org)["entitlements"]
    assert [(r["right_key"], r["source"], r["sub"]) for r in lignes] == [("unipile", "trial", sub)]


def test_le_retrait_ne_touche_que_sa_ligne(live):
    org = _org()
    sub = _membre(org, T0)
    for portee in (None, sub):
        _appel("service.org.entitlement.put", org_id=org, right_key="unipile_seats",
               source="trial", value=3, sub=portee)
    assert _appel("service.org.entitlement.delete", org_id=org, right_key="unipile_seats",
                  source="trial") == {"ok": True}
    restant = _appel("service.org.entitlements.list", org_id=org)["entitlements"]
    assert [r["sub"] for r in restant] == [sub]
    assert _refus("service.org.entitlement.delete", org_id=org, right_key="unipile_seats",
                  source="trial").code == "unknown_entitlement"


@pytest.mark.parametrize("champs, code", [
    (dict(right_key="unipile", source="gratuit", value=1), "unknown_source"),
    (dict(right_key="inconnu", source="trial", value=1), "entitlement_unknown_key"),
    (dict(right_key="unipile", source="trial", value=7), "entitlement_value_invalid"),
    (dict(right_key="unipile", source="trial", value=1, sub="u-etranger"), "not_a_member"),
    (dict(right_key="unipile", source="trial", value=1,
          starts_at=T0, expires_at=T0), "invalid_window"),
])
def test_une_pose_refusee_dit_pourquoi_et_n_ecrit_rien(live, champs, code):
    org = _org()
    assert _refus("service.org.entitlement.put", org_id=org, **champs).code == code
    assert _appel("service.org.entitlements.list", org_id=org)["entitlements"] == []


# ── la portée « personne, toutes orgs » ──────────────────────────────────────

def _personne() -> str:
    sub = f"u-{uuid.uuid4().hex[:8]}"
    db.upsert_user(sub, email=f"{sub}@exemple.test")
    return sub


def test_la_personne_partout_se_pose_se_relit_et_se_retire_seule(live):
    """Pose idempotente sous le service, relue par sa route, retirée sans toucher la
    ligne d'org ni la ligne de la personne dans l'org."""
    org = _org()
    sub = _membre(org, T0)
    _appel("service.org.entitlement.put", org_id=org, right_key="unipile_seats",
           source="subscription", value=2)
    _appel("service.org.entitlement.put", org_id=org, right_key="unipile_seats",
           source="subscription", value=3, sub=sub)
    for valeur in (4, 4, 6):
        ligne = _appel("service.user.entitlement.put", sub=sub, right_key="unipile_seats",
                       source="subscription", value=valeur, expires_at=T0 + timedelta(days=30))
    assert (ligne["sub"], ligne["value"], ligne["granted_by"]) == (sub, 6, _SERVICE["sub"])
    assert ligne["expires_at"] == (T0 + timedelta(days=30)).isoformat()
    lues = _appel("service.user.entitlements.list", sub=sub)
    assert lues["sub"] == sub and lues["entitlements"] == [ligne], "relue telle que posée"
    assert [r["sub"] for r in _appel("service.org.entitlements.list", org_id=org)
            ["entitlements"]] == [None, sub], "la route de l'org ne la rend pas"

    assert _appel("service.user.entitlement.delete", sub=sub, right_key="unipile_seats",
                  source="subscription") == {"ok": True}
    assert _appel("service.user.entitlements.list", sub=sub)["entitlements"] == []
    assert [(r["sub"], r["value"]) for r in _appel(
        "service.org.entitlements.list", org_id=org)["entitlements"]] == [(None, 2), (sub, 3)]
    assert _refus("service.user.entitlement.delete", sub=sub, right_key="unipile_seats",
                  source="subscription").code == "unknown_entitlement"


def test_la_personne_partout_n_a_pas_besoin_d_etre_membre(live):
    sub = _personne()
    ligne = _appel("service.user.entitlement.put", sub=sub, right_key="unipile",
                   source="trial", value=1)
    assert ligne["sub"] == sub
    assert access.value_for(sub, _org(), "unipile") == 1


@pytest.mark.parametrize("champs, code", [
    (dict(right_key="unipile", source="gratuit", value=1), "unknown_source"),
    (dict(right_key="inconnu", source="trial", value=1), "entitlement_unknown_key"),
    (dict(right_key="unipile", source="trial", value=7), "entitlement_value_invalid"),
    (dict(right_key="unipile", source="trial", value=1,
          starts_at=T0, expires_at=T0), "invalid_window"),
])
def test_une_pose_de_personne_refusee_dit_pourquoi_et_n_ecrit_rien(live, champs, code):
    sub = _personne()
    assert _refus("service.user.entitlement.put", sub=sub, **champs).code == code
    assert _appel("service.user.entitlements.list", sub=sub)["entitlements"] == []


@pytest.mark.parametrize("cle, champs", [
    ("service.user.entitlements.list", {}),
    ("service.user.entitlement.effective", dict(right_key="unipile")),
    ("service.user.entitlement.put", dict(right_key="unipile", source="trial", value=1)),
    ("service.user.entitlement.delete", dict(right_key="unipile", source="trial")),
])
def test_une_personne_inconnue_est_un_404_nomme(live, cle, champs):
    assert _refus(cle, sub=f"u-inconnu-{uuid.uuid4().hex[:8]}", **champs).code == \
        "unknown_user"


def test_l_export_rend_l_etat_de_facturation_en_un_instantane_date(live):
    """#1085 : ce que oto-commerce reprendra, bloc par bloc, dates avec leur fuseau."""
    org = _org()
    sub = _membre(org, T0)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO org_subscriptions (org_id, provider, plan, status, customer_id, "
            "mandate_id, current_period_end, next_billing_at) VALUES "
            "(%s, 'mollie', 'standard', 'active', 'cst_x', 'mdt_x', %s, %s)",
            (org, T0 + timedelta(days=30), T0 + timedelta(days=30)))
        conn.execute("INSERT INTO option_comps (entity_type, entity_id, option, granted_by) "
                     "VALUES ('user', %s, 'unipile', 'admin')", (sub,))
        conn.execute(
            "INSERT INTO billing_identities (org_id, legal_name, country_code, vat_number, "
            "address_line, postal_code, city, billing_email) VALUES "
            "(%s, 'Acme', 'FR', NULL, '1 rue X', '75001', 'Paris', 'c@acme.test')", (org,))
        conn.execute(
            "INSERT INTO legal_acceptance_events (sub, org_id, doc_slug, version, context) "
            "VALUES (%s, %s, 'cgv', 'v1', 'purchase')", (sub, org))
        pid = conn.execute(
            "INSERT INTO billing_payments (org_id, kind, amount, status) "
            "VALUES (%s, 'initial', 2280, 'paid') RETURNING id", (org,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO billing_invoices (org_id, payment_row_id, kind, status, number, "
            "amount_ttc, pdf, pdf_filename) VALUES (%s, %s, 'invoice', 'issued', 'F-1', 2280, "
            "%s, 'F-1.pdf')", (org, pid, b"%PDF-1.4 x"))
    out = _appel("service.billing.export")
    assert out["plans"]["standard"]["amount_ht"] == 1900
    assert set(out["plans"]["standard"]["rights"]) == {"unipile", "platform_unmetered"}
    abo = next(s for s in out["subscriptions"] if s["org_id"] == org)
    assert (abo["customer_id"], abo["mandate_id"], abo["plan"]) == ("cst_x", "mdt_x", "standard")
    assert abo["current_period_end"] == (T0 + timedelta(days=30)).isoformat().replace(
        "+00:00", ".000000+00:00")
    assert datetime.fromisoformat(abo["current_period_end"]).tzinfo is not None
    assert abo["grace_until"] is None
    assert any(c["entity_id"] == sub and c["option"] == "unipile" for c in out["option_comps"])
    assert any(i["org_id"] == org and i["billing_email"] == "c@acme.test"
               for i in out["identities"])
    assert any(a["sub"] == sub and a["doc_slug"] == "cgv" for a in out["purchase_acceptances"])
    import base64
    facture = next(f for f in out["invoices"] if f["org_id"] == org)
    assert (facture["number"], facture["pdf_filename"]) == ("F-1", "F-1.pdf")
    assert base64.b64decode(facture["pdf_base64"]) == b"%PDF-1.4 x"


def test_le_role_plateforme_d_un_compte(live):
    org = _org()
    membre, admin = _membre(org, T0), _membre(org, T0)
    with _connect() as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE sub = %s", (admin,))
    assert _appel("service.users.get", sub=membre)["role"] == "member"
    assert _appel("service.users.get", sub=admin)["role"] == "admin"
    assert _refus("service.users.get", sub="u-inconnu").code == "unknown_user"


# ── la valeur effective d'un droit (#1096) ───────────────────────────────────
#
# L'égalité avec la lecture des points d'usage est prouvée sur la grille de monotonie
# (`test_org_entitlements_live.py`) ; ici : les refus, la lecture héritée, et
# qu'aucune écriture n'a lieu.

def _effectif(sub, right_key, org_id=None):
    return _appel("service.user.entitlement.effective", sub=sub, right_key=right_key,
                  org_id=org_id)


def test_la_valeur_effective_refuse_nommement(live):
    sub = _personne()
    assert _refus("service.user.entitlement.effective", sub=sub,
                  right_key="inconnu").code == "entitlement_unknown_key"
    assert _refus("service.user.entitlement.effective", sub=sub,
                  right_key="platform_key:inconnu").code == "entitlement_unknown_key"
    assert _refus("service.user.entitlement.effective", sub=sub, right_key="unipile",
                  org_id=10**12).code == "unknown_org"
    assert _refus("service.user.entitlement.effective", sub=sub, right_key="unipile",
                  org_id=_org(archivee=True)).code == "unknown_org"


def test_la_lecture_directe_des_dons_d_option_part_a_cote_de_la_valeur(live):
    """Un don d'option n'ouvre pas l'option payante (la valeur reste le défaut), ni sur
    le compte ni, depuis la coupure du cœur (#1097), sur l'org : plus rien ne le traduit
    en droit. La lecture héritée le montre, la valeur non ; seule la ligne posée par le
    service l'ouvre."""
    org, sub = _org(), _personne()
    db.set_option_comp("user", sub, "unipile", granted_by="admin-test")
    out = _effectif(sub, "unipile", org)
    assert (out["valeur"], out["defaut"], out["par"]) == (0, True, [])
    assert out["lecture_directe"] == {"option_comps": {"personne": True, "org": False}}
    db.set_option_comp("org", str(org), "unipile", granted_by="admin-test")
    out = _effectif(sub, "unipile", org)
    assert (out["valeur"], out["defaut"], out["par"]) == (0, True, []), \
        "le don d'org n'ouvre plus rien"
    _appel("service.org.entitlement.put", org_id=org, right_key="unipile",
           source="offered", value=1)
    out = _effectif(sub, "unipile", org)
    assert (out["valeur"], out["defaut"]) == (1, False)
    assert out["par"] == [{"portee": "org", "source": "offered", "valeur": 1}]
    assert out["lecture_directe"]["option_comps"] == {"personne": True, "org": True}
    hors = _effectif(sub, "unipile")
    assert hors["valeur"] == 0 and hors["lecture_directe"]["option_comps"] == \
        {"personne": True, "org": None}


def test_la_lecture_directe_suit_la_cle(live):
    from oto_mcp import providers, unipile_connect
    org, sub = _org(), _personne()
    sieges = _effectif(sub, "unipile_seats", org)
    assert sieges["lecture_directe"]["plafond_messagerie"] == \
        {"plafond": unipile_connect.plafond_de_comptes(org)}
    assert _effectif(sub, "unipile_seats")["lecture_directe"] is None, \
        "hors org, aucun plafond de messagerie ne s'applique"
    cle = _effectif(sub, "platform_key:kaspr", org)["lecture_directe"]["registre"]
    assert cle == {"cle_ouverte": providers.REGISTRY["kaspr"].platform_key_open,
                   "quota_du_jour": access.quota_for("kaspr")}
    assert _effectif(sub, "members_max", org)["lecture_directe"] is None, \
        "aucune lecture héritée"


def test_la_valeur_effective_n_ecrit_rien(live):
    """Toute la lecture tient dans une session en LECTURE SEULE : une écriture lèverait
    `ReadOnlySqlTransaction`."""
    from oto_mcp.db._conn import reuse_connection
    org, sub = _org(), _personne()
    _appel("service.user.entitlement.put", sub=sub, right_key="unipile_seats",
           source="trial", value=9)
    with reuse_connection():
        with _connect() as conn:
            conn.execute("SET default_transaction_read_only = on")
        try:
            for cle in ("unipile", "unipile_seats", "platform_key:kaspr", "members_max"):
                for o in (org, None):
                    _effectif(sub, cle, o)
            assert _effectif(sub, "unipile_seats", org)["valeur"] == 9
        finally:
            with _connect() as conn:
                conn.execute("RESET default_transaction_read_only")


def test_la_route_effective_n_est_pas_avalee_par_celle_de_la_source():
    """`GET …/{right_key}/effective` côtoie `PUT|DELETE …/{right_key}/{source}` : la
    route servie pour un GET est bien la lecture effective."""
    from starlette.routing import Match
    from oto_mcp.api import routes as api_routes
    scope = {"type": "http", "method": "GET", "path_params": {},
             "path": "/api/service/users/u-x/entitlements/platform_key:kaspr/effective"}
    pleines = [r for r in api_routes.make_routes(object())
               if r.matches(scope)[0] is Match.FULL]
    assert [r.path for r in pleines] == [
        "/api/service/users/{sub}/entitlements/{right_key}/effective"]


def _consolider_le_journal():
    """La consommation se lit sur les totaux par jour (#1147) : comme en production après le
    rattrapage, chaque jour clos du journal du banc est (re)consolidé avant la lecture."""
    from oto_mcp.db import journal_jour
    for jour in journal_jour.jours_a_consolider(refaire=True):
        journal_jour.consolider_jour(jour)
