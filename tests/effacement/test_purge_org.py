"""L'effacement d'une org, sur une vraie base à plusieurs orgs (docs/effacement-d-org.md).

Le banc de l'export (`perimetre_banc`) sème deux propriétaires, chacun avec son
MARQUEUR dans une valeur texte de chaque ligne (`A7d1e` pour l'org effacée, `B9f3c`
pour le voisin). Deux propriétés se vérifient alors par un second chemin, sans passer
par les règles qu'on éprouve :

- **rien d'autrui** : pas une ligne du voisin ne bouge ;
- **rien d'oublié** : après la purge, le marqueur de l'org effacée ne survit QUE dans les
  tables où l'effacement le garde à dessein (pièces comptables), et dans ce qui
  appartient à un membre qui GARDE son compte (il est aussi membre d'une autre org).

Le banc de l'export fait d'`alice` un membre de deux orgs (l'org et son ex-org perso) et
de `bob` un membre de la seule org : `alice` reste, `bob` part avec l'org.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Le banc de l'export vit à côté de ses tests ; l'effacement le partage tel quel.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "export_perimetre"))

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.effacement import disposition as dp, purge  # noqa: E402
from oto_mcp.effacement.perimetre import EffacementRefuse, Instantane, figer  # noqa: E402
from oto_mcp.export_perimetre.decouverte import lire_schema  # noqa: E402
from perimetre_banc import A, B, membre, org, semer  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _tables(c) -> list[str]:
    return sorted(lire_schema(c).colonnes)


def _marquees(c, m: str) -> dict[str, int]:
    """Par table, les lignes dont le TEXTE porte le marqueur `m` (second chemin)."""
    out = {}
    for t in _tables(c):
        n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text LIKE %s",
                      (f"%{m}%",)).fetchone()["n"]
        if n:
            out[t] = n
    return out


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        a, b = semer(c, A), semer(c, B)
        # Une membre PARTAGÉE entre les deux orgs : elle garde son compte, et seule sa
        # ligne dans l'org effacée part.
        carol = f"partagee-{A}"
        membre(c, a["org"], carol)
        membre(c, b["org"], carol)
        # Un appel de bob hors de toute org, et une acceptation légale de bob.
        c.execute("INSERT INTO legal_acceptance_events (sub, doc_slug, version, ip, user_agent) "
                  "VALUES (%s, 'cgu', '1', '203.0.113.7', %s)", (a["bob"], f"navigateur {A}"))
        # carol travaille DANS l'org effacée : elle doit basculer sur l'autre.
        c.execute("UPDATE org_members SET is_active = (org_id = %s) WHERE sub = %s",
                  (a["org"], carol))
        # Un partage accordé par l'org, et un partage DÉRIVÉ accordé par le voisin : le
        # dérivé ne doit pas survivre à son parent (il élargirait un accès).
        racine = c.execute(
            "INSERT INTO grants (resource_id, grantor_kind, grantor_id, grantee_kind, "
            "grantee_id) VALUES ('1', 'org', %s, 'org', %s) RETURNING id",
            (str(a["org"]), str(b["org"]))).fetchone()["id"]
        derive = c.execute(
            "INSERT INTO grants (resource_id, grantor_kind, grantor_id, grantee_kind, "
            "grantee_id, parent_id) VALUES ('1', 'org', %s, 'user', %s, %s) RETURNING id",
            (str(b["org"]), b["alice"], racine)).fetchone()["id"]
        avant_b = _marquees(c, B)
        yield {"dsn": pg_module_dsn, A: a, B: b, "carol": carol, "avant_b": avant_b,
               "derive": derive}


def _conn(base, autocommit=True):
    return psycopg.connect(base["dsn"], autocommit=autocommit, row_factory=dict_row)


def test_la_disposition_couvre_le_schema_reel(base):
    with _conn(base) as c:
        schema, classement = purge.preparer(c)
    assert set(classement) == set(schema.colonnes)


def test_la_disposition_mord_sur_une_table_non_tranchee(base, monkeypatch):
    sans = dict(dp.NON_EXPORTEES)
    del sans["billing_invoices"]
    monkeypatch.setattr(dp, "NON_EXPORTEES", sans)
    with _conn(base) as c, pytest.raises(dp.DispositionIncomplete) as e:
        purge.preparer(c)
    assert "billing_invoices" in str(e.value)


def test_le_perimetre_separe_qui_part_de_qui_reste(base):
    with _conn(base) as c:
        inst = figer(c, base[A]["org"])
    assert inst.partants == (base[A]["bob"],)
    assert set(inst.restants) == {base[A]["alice"], base["carol"]}
    assert len(inst.groupes) == 1


def test_le_plan_compte_sans_rien_ecrire(base):
    with _conn(base) as c:
        inst = figer(c, base[A]["org"])
        avant = _marquees(c, A)
        p = purge.plan(c, inst)
        assert _marquees(c, A) == avant
    effacees = p["tables"][dp.EFFACER]
    assert effacees["org_members"] == 3          # alice, bob, carol
    assert effacees["users"] == 1                # bob seul
    assert effacees["docs"] == 2
    assert p["tables"][dp.CONSERVER]["billing_contracts"] == 1
    assert p["tables"][dp.ANONYMISER]["legal_acceptance_events"] == 1
    assert p["tables"][dp.PIERRE_TOMBALE] == {"orgs": 1}


def test_un_partant_qui_rejoint_une_autre_org_garde_son_compte(base):
    from oto_mcp.effacement.perimetre import rafraichir
    with _conn(base) as c:
        inst = figer(c, base[A]["org"])
        c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, "
                  "'org_member')", (base[B]["org"], base[A]["bob"]))
        try:
            revu = rafraichir(c, inst)
        finally:
            c.execute("DELETE FROM org_members WHERE org_id = %s AND sub = %s",
                      (base[B]["org"], base[A]["bob"]))
    assert revu.partants == () and base[A]["bob"] in revu.restants


def test_les_objets_de_l_org_sont_tous_trouves_et_rien_du_voisin(base):
    from perimetre_banc import BASE_SOURCE
    with _conn(base) as c:
        cles = purge.cles_d_objet(c, figer(c, base[A]["org"]), BASE_SOURCE)
    a, b = set(base[A]["objets"]), set(base[B]["objets"])
    # Les objets de l'org partent (logo, fichier de projet, audio) ; ce qui est à alice,
    # qui GARDE son compte, reste — son avatar, et ses images citées par URL (une ligne
    # qui reste pourrait les citer aussi).
    a_alice = {k for k in a if k.startswith(("avatars/", "images/"))}
    assert cles == a - a_alice
    assert not cles & b


def test_la_repetition_ne_laisse_aucune_trace(base):
    with _conn(base) as c:
        avant = _marquees(c, A)
        assert purge.repeter(c, figer(c, base[A]["org"]))["lignes"] > 0
        assert _marquees(c, A) == avant


def test_un_abonnement_qui_preleve_refuse(base):
    with _conn(base) as c:
        o = org(c, f"payante {A}", base[A]["tenant"])
        membre(c, o, f"payeur-{A}")
        c.execute("INSERT INTO org_subscriptions (org_id, plan, status) VALUES (%s, 'pro', "
                  "'active')", (o,))
        with pytest.raises(EffacementRefuse) as e:
            figer(c, o)
    assert e.value.code == "subscription_active"


def test_un_compte_d_operateur_qui_partirait_refuse(base):
    with _conn(base) as c:
        o = org(c, f"opérée {A}", base[A]["tenant"])
        membre(c, o, f"operateur-{A}")
        c.execute("UPDATE users SET role = 'super_admin' WHERE sub = %s", (f"operateur-{A}",))
        with pytest.raises(EffacementRefuse) as e:
            figer(c, o)
    assert e.value.code == "operator_account"


def test_la_purge_efface_l_org_et_rien_d_autre(base):
    a = base[A]
    with _conn(base) as c:
        inst = figer(c, a["org"])
        ecrit = purge.purger(c, inst)
        assert ecrit["tool_calls"] >= 2 and ecrit["users"] == 1
        assert purge.residu(c, inst) == {}
        # Rien d'autrui : le voisin est intact, ligne pour ligne — son partage DÉRIVÉ
        # d'un partage de l'org effacée reste, révoqué (vérifié plus bas).
        assert _marquees(c, B) == base["avant_b"]
        # Rien d'oublié : le marqueur ne survit que là où on le garde à dessein.
        reste = _marquees(c, A)
        # Le contrat du voisin porte le marqueur du voisin seulement ; celui de l'org reste.
        assert reste.pop("billing_contracts") == 1
        # La preuve d'acceptation de bob reste, sous son seul identifiant (qui porte le
        # marqueur), sans IP ni navigateur (`test_l_acceptation_legale_reste…`).
        assert reste.pop("legal_acceptance_events") == 1
        # Ce qui est à alice, qui garde son compte : sa fiche, son ex-org perso, son projet
        # personnel, son droit « partout », son statut d'admin de tenant — et le tenant.
        attendu = {"users", "orgs", "projects", "org_entitlements", "tenant_admins",
                   "tenants", "tenant_legal_docs", "connector_availability", "org_members"}
        assert set(reste) <= attendu, set(reste) - attendu
        assert c.execute("SELECT count(*) AS n FROM users WHERE sub = %s",
                         (a["bob"],)).fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM users WHERE sub = ANY(%s)",
                         ([a["alice"], base["carol"]],)).fetchone()["n"] == 2
        # carol reste membre de l'AUTRE org, qui devient son org active.
        assert c.execute("SELECT org_id, is_active FROM org_members WHERE sub = %s",
                         (base["carol"],)).fetchall() == [{"org_id": base[B]["org"],
                                                            "is_active": True}]
        # Le partage dérivé du voisin reste, RÉVOQUÉ et détaché : il n'ouvre plus rien,
        # et un partage s'archive, il ne se supprime pas.
        assert ecrit.get("grants.descendants") == 1
        r = c.execute("SELECT revoked_at, parent_id FROM grants WHERE id = %s",
                      (base["derive"],)).fetchone()
        assert r["revoked_at"] is not None and r["parent_id"] is None


def test_la_pierre_tombale_ne_garde_rien_de_personnel(base):
    with _conn(base) as c:
        r = c.execute("SELECT * FROM orgs WHERE id = %s", (base[A]["org"],)).fetchone()
    assert r["name"] == dp.PIERRE_TOMBALE_NOM
    assert r["logo_url"] is None and r["created_by"] is None
    assert r["archived_at"] is not None and r["suspended_at"] is not None
    assert A not in json.dumps(r, default=str)


def test_l_acceptation_legale_reste_sans_ip_ni_navigateur(base):
    with _conn(base) as c:
        r = c.execute("SELECT ip, user_agent FROM legal_acceptance_events WHERE sub = %s",
                      (base[A]["bob"],)).fetchone()
    assert r == {"ip": None, "user_agent": None}


def test_la_purge_se_rejoue_sans_rien_faire(base):
    with _conn(base) as c:
        inst = Instantane(base[A]["org"], (), (base[A]["bob"],), ())
        assert purge.purger(c, inst) == {"orgs": 1}       # la pierre tombale se repose
        assert purge.residu(c, inst) == {}
