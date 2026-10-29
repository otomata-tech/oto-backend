"""L'avantage OFFERT : le rendre visible, le borner, et ne PAS le servir hors périmètre.

Trois gardes, chacune posée sur une bévue réelle mesurée le 2026-09-02 :

1. **Le bénéficiaire ne voyait rien.** 32 dons d'option vivants, un seul abonnement
   payant sur la plateforme — et l'écran d'abonnement, qui lit l'abonnement, ne
   montrait aucun des 32. Le titulaire voyait un catalogue lui vendre, prix affichés
   et bouton armé, exactement ce qu'il possédait déjà.
2. **Le périmètre.** 11 des 20 orgs gratifiées appartiennent à un tenant tiers : ce
   sont les clients d'un partenaire, sur ses données, dans son produit. Aucun badge,
   aucune échéance, aucune relance ne doit les atteindre. C'est une limite de
   périmètre, donc elle est MÉCANIQUE — ce fichier rougit si elle cède.
3. **L'échéance.** Une date posée sur un don doit FERMER le droit le jour venu ; une
   échéance sans effet serait pire que pas d'échéance.

⚠️ **Depuis la coupure du cœur (#1097)**, un avantage qui est un droit du catalogue
(`unipile`) n'est plus annoncé d'après `option_comps` : oto-commerce le pose et sait ce
qu'il offre. La mise en forme d'un avantage se prouve donc sur un avantage VENDU hors
catalogue (`addon_test`, ajouté à un palier le temps du test).

⚠️ Le dépôt est public : aucun nom de partenaire ici. Le tenant tiers de test
s'appelle `acme`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import access, billing, billing_grants


UTC = timezone.utc
LOIN = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)


def _wire(monkeypatch, *, tenant="oto", org_rows=(), user_rows=()):
    """Câble les deux seams que lit `granted_benefits` : le tenant de l'org, et les
    lignes de don. Aucune base — la convention du dépôt (cf. conftest)."""
    monkeypatch.setattr(billing_grants.db, "org_tenant_slug", lambda oid: tenant)
    monkeypatch.setattr(
        billing_grants.db, "list_option_comp_rows",
        lambda et, eid: list(org_rows if et == "org" else user_rows))


ADDON = "addon_test"


@pytest.fixture(autouse=True)
def _avantage_hors_catalogue(monkeypatch):
    """Un avantage vendu qui n'est PAS un droit du catalogue : le seul genre que
    `granted_benefits` annonce encore."""
    standard = dict(billing.PLANS["standard"])
    standard["options"] = tuple(standard["options"]) + (ADDON,)
    monkeypatch.setitem(billing.PLANS, "standard", standard)


def _don(option=ADDON, expires_at=None):
    return {"option": option, "granted_by": "admin",
            "granted_at": datetime(2026, 7, 16, tzinfo=UTC), "expires_at": expires_at}


# ── 1. Le don devient visible, et il se NOMME ────────────────────────────────

def test_un_don_dorg_est_rendu_avec_son_nom_et_sa_valeur(monkeypatch):
    _wire(monkeypatch, org_rows=[_don()])
    (b,) = billing_grants.granted_benefits(7)
    assert b["option"] == ADDON
    assert b["scope"] == "org"
    # Ce que ça vaut = le palier le MOINS cher qui l'inclut, en centimes HT.
    assert b["value_amount"] == 1900 and b["currency"] == "eur"


@pytest.mark.parametrize("option", ["unipile", "platform_unmetered"])
def test_un_droit_du_catalogue_offert_n_est_plus_annonce_d_ici(monkeypatch, option):
    """Coupure du cœur (#1097) : oto-commerce pose ces droits et sait ce qu'il offre ;
    la marque héritée d'un don ne dit plus ce qui est ouvert."""
    _wire(monkeypatch, org_rows=[_don(option="unipile"), _don(option=option)])
    assert billing_grants.granted_benefits(7) == []


def test_un_don_fait_a_une_personne_n_est_pas_annonce(monkeypatch):
    # Il n'ouvre plus d'option payante (ADR 0070 §7) : l'annoncer comme un avantage
    # promettrait ce que le seam refuse.
    _wire(monkeypatch, user_rows=[_don()])
    assert billing_grants.granted_benefits(7) == []


def test_un_drapeau_de_population_nest_pas_un_cadeau(monkeypatch):
    # `beta` n'est vendu dans aucun palier : l'afficher comme un avantage offert
    # promettrait une valeur à ce qui n'en a pas, et le jour où on le retire on
    # aurait l'air de reprendre un cadeau.
    _wire(monkeypatch, org_rows=[_don(option="beta")])
    assert billing_grants.granted_benefits(7) == []
    assert billing_grants.is_benefit("beta") is False


def test_status_sans_abonnement_porte_le_don_ET_garde_le_catalogue(monkeypatch):
    # La branche où le défaut vivait. Le catalogue RESTE servi : un don n'est pas un
    # abonnement, et la voie pour en prendre un ne doit pas se refermer.
    _wire(monkeypatch, org_rows=[_don(expires_at=LOIN)])
    monkeypatch.setattr(billing.db_billing, "get_org_subscription", lambda oid: None)
    st = billing.status(7)
    assert st["subscribed"] is False
    assert [b["option"] for b in st["granted"]] == [ADDON]
    assert st["plans"], "le catalogue doit rester joint — c'est la voie de conversion"


# ── 2. Le périmètre : les clients d'un partenaire ne sont pas les nôtres ──────

def test_une_org_de_tenant_tiers_ne_recoit_aucun_dispositif(monkeypatch):
    _wire(monkeypatch, tenant="acme", org_rows=[_don(expires_at=LOIN)])
    assert billing_grants.granted_benefits(7) == []
    assert billing_grants.org_is_ours(7) is False


def test_le_dispositif_se_referme_si_le_tenant_est_illisible(monkeypatch):
    # Sans réponse franche sur le rattachement, on se TAIT : le coût d'un faux
    # négatif (parler aux clients d'un partenaire) n'est pas symétrique de celui
    # d'un écran incomplet.
    def _boom(oid):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(billing_grants.db, "org_tenant_slug", _boom)
    assert billing_grants.org_is_ours(7) is False
    assert billing_grants.granted_benefits(7) == []


def test_status_dune_org_de_tenant_tiers_ne_porte_aucun_don(monkeypatch):
    # La garde tient sur la surface SERVIE, pas seulement sur la fonction interne.
    _wire(monkeypatch, tenant="acme", org_rows=[_don(expires_at=LOIN)])
    monkeypatch.setattr(billing.db_billing, "get_org_subscription", lambda oid: None)
    assert billing.status(7)["granted"] == []


def test_org_absente_hors_dispositif(monkeypatch):
    assert billing_grants.org_is_ours(None) is False


# ── 3. L'échéance : elle DIT avant, et elle FERME le jour venu ───────────────

def test_lecheance_est_annoncee_avec_le_temps_qui_reste(monkeypatch):
    fin = datetime.now(UTC) + timedelta(days=59)
    _wire(monkeypatch, org_rows=[_don(expires_at=fin)])
    (b,) = billing_grants.granted_benefits(7)
    assert b["expires_at"] is not None
    assert b["days_left"] == 58  # 58 jours pleins + un reliquat

def test_un_don_echu_se_lit_echu_et_non_expire_aujourdhui(monkeypatch):
    fin = datetime.now(UTC) - timedelta(days=3)
    _wire(monkeypatch, org_rows=[_don(expires_at=fin)])
    (b,) = billing_grants.granted_benefits(7)
    assert b["days_left"] < 0, "borner à zéro effacerait la différence"


def test_sans_echeance_le_don_est_perpetuel(monkeypatch):
    # L'état des 32 dons posés avant le 2026-09-02 : la colonne les laisse intacts.
    _wire(monkeypatch, org_rows=[_don()])
    (b,) = billing_grants.granted_benefits(7)
    assert b["expires_at"] is None and b["days_left"] is None


# ── 4. Le seam : une seule règle pour « cette org a-t-elle l'option » ────────

def test_le_cockpit_dorg_sert_le_meme_verdict_que_le_seam(monkeypatch):
    # LE défaut du 2026-09-02 : le cockpit d'activation lisait le don en direct, donc
    # une org qui PAYAIT s'y affichait « non souscrite ». Il lit le droit déclaré.
    from oto_mcp.capabilities.connectors import activation as cap

    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None: access.db_entitlements.Posees(
                            1 if (oid, droit) == (9, "unipile") else None, None))
    assert cap._org_subscribed(9, "unipile") is True
    monkeypatch.setattr(access, "current_org", lambda sub: 9)
    assert access.has_option("moi", "unipile") is True


def test_le_don_personnel_du_requerant_n_ouvre_rien(monkeypatch):
    # Ni pour l'org, ni pour lui : sa marque de compte n'est pas une ligne de droit.
    monkeypatch.setattr(access.db, "has_option_comp",
                        lambda et, eid, opt: et == "user")
    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None:
                        access.db_entitlements.Posees(None, None))
    monkeypatch.setattr(access, "current_org", lambda sub: 9)
    assert access.org_has(9, "unipile") is False
    assert access.has_option("moi", "unipile") is False


# ── 5. Poser l'échéance : l'ÉCRITURE aussi refuse hors périmètre ────────────

def _admin(monkeypatch, *, tenant="oto"):
    from oto_mcp.capabilities import users_admin as ua

    monkeypatch.setattr(ua.billing_grants.db, "org_tenant_slug", lambda oid: tenant)
    return ua


def test_poser_une_echeance_sur_une_org_de_partenaire_est_refuse(monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied

    ua = _admin(monkeypatch, tenant="acme")
    inp = ua.OptionInput(entity_type="org", entity_id="200", option="beta",
                         on=True, expires_at="2026-10-31")
    with pytest.raises(AuthzDenied) as e:
        ua._parse_expiry(inp, "200")
    assert e.value.code == "partner_org_out_of_scope"


def test_une_echeance_au_jour_couvre_la_JOURNEE(monkeypatch):
    # « offert jusqu'au 31 octobre » doit couvrir le 31 : minuit couperait un jour trop tôt.
    ua = _admin(monkeypatch)
    inp = ua.OptionInput(entity_type="org", entity_id="7", option="beta",
                         on=True, expires_at="2026-10-31")
    d = ua._parse_expiry(inp, "7")
    assert (d.year, d.month, d.day, d.hour) == (2026, 10, 31, 23)


def test_omettre_lecheance_ne_leffacce_pas(monkeypatch):
    # Deux surfaces re-posent un don sans rien savoir des dates : leur geste anodin
    # ne doit pas retirer la borne posée ailleurs.
    from oto_mcp import db

    ua = _admin(monkeypatch)
    inp = ua.OptionInput(entity_type="org", entity_id="7", option="beta", on=True)
    assert ua._parse_expiry(inp, "7") is db.KEEP_EXPIRY


def test_une_chaine_vide_rouvre_le_don(monkeypatch):
    ua = _admin(monkeypatch)
    inp = ua.OptionInput(entity_type="org", entity_id="7", option="beta",
                         on=True, expires_at="")
    assert ua._parse_expiry(inp, "7") is None


def test_le_don_lui_meme_reste_posable_sur_une_org_de_partenaire(monkeypatch):
    # Le refus porte sur l'ÉCHÉANCE, jamais sur le droit : retirer un accès à l'org
    # d'un partenaire serait le même débordement, dans l'autre sens.
    from oto_mcp import db

    ua = _admin(monkeypatch, tenant="acme")
    inp = ua.OptionInput(entity_type="org", entity_id="200", option="beta", on=True)
    assert ua._parse_expiry(inp, "200") is db.KEEP_EXPIRY


# ── 6. Le compteur d'usage : il MONTRE, il ne refuse jamais ─────────────────

def _wire_usage(monkeypatch, *, tenant="oto", calls=25):
    monkeypatch.setattr(billing_grants.db, "org_tenant_slug", lambda oid: tenant)
    monkeypatch.setattr(billing_grants.db, "count_org_mcp_calls",
                        lambda oid, *, since: calls)


def test_lusage_est_rendu_sans_ratio(monkeypatch):
    # À 25 sur 1000, un pourcentage ou une barre dirait « gratuit et sans fin » —
    # l'inverse de ce que le compteur existe pour faire comprendre. On rend le
    # NOMBRE et le plafond ; on ne les divise pas.
    _wire_usage(monkeypatch, calls=25)
    u = billing_grants.monthly_usage(7)
    assert u["calls"] == 25 and u["included"] == 1000 and u["over"] is False
    interdits = {"percent", "ratio", "pct", "progress", "fraction"}
    assert not (interdits & set(u)), "aucun ratio ne doit être servi"


def test_un_depassement_saffiche_et_ne_refuse_rien(monkeypatch):
    _wire_usage(monkeypatch, calls=1200)
    assert billing_grants.monthly_usage(7)["over"] is True
    # Et surtout : aucun chemin d'entitlement ne consulte ce compteur. Un refus
    # bâti sur un journal best-effort couperait un service sur une donnée qui a le
    # droit de manquer.
    import inspect

    from oto_mcp.access import quotas

    for fn in (quotas.has_option, quotas.user_has_option):
        assert "monthly_usage" not in inspect.getsource(fn)
        assert "INCLUDED_CALLS" not in inspect.getsource(fn)


def test_lusage_dune_org_de_tenant_tiers_nest_pas_compte(monkeypatch):
    _wire_usage(monkeypatch, tenant="acme", calls=900)
    assert billing_grants.monthly_usage(7) is None


def test_un_journal_illisible_se_tait_au_lieu_dafficher_zero(monkeypatch):
    monkeypatch.setattr(billing_grants.db, "org_tenant_slug", lambda oid: "oto")

    def _boom(oid, *, since):
        raise RuntimeError("journal indisponible")

    monkeypatch.setattr(billing_grants.db, "count_org_mcp_calls", _boom)
    assert billing_grants.monthly_usage(7) is None


def test_la_fenetre_est_le_mois_en_cours(monkeypatch):
    # Le journal ne garde qu'environ 35 jours : le mois en cours est toujours
    # calculable, le mois précédent ne l'est pas. Le `since` doit être le 1er.
    vus = {}
    monkeypatch.setattr(billing_grants.db, "org_tenant_slug", lambda oid: "oto")
    monkeypatch.setattr(billing_grants.db, "count_org_mcp_calls",
                        lambda oid, *, since: vus.update(since=since) or 3)
    billing_grants.monthly_usage(7)
    d = vus["since"]
    assert (d.day, d.hour, d.minute, d.second) == (1, 0, 0, 0)


def test_le_compteur_compte_les_appels_dagent_pas_la_navigation():
    """Le rattachement RÉEL (`tool_calls.org_id`) et le seul genre 'mcp'. Un préfixe
    de nom d'outil a déjà produit un faux résultat : il n'en reste aucune trace ici.
    Lu sur les totaux par jour : la nature et l'org passent par `journal_jour.source`,
    dont l'exactitude se juge dans `tests/db/test_journal_jour_lecteurs.py`."""
    import inspect

    from oto_mcp.db import journal_calls

    src = inspect.getsource(journal_calls.count_org_mcp_calls)
    assert 'kinds=("mcp",)' in src
    assert 'filtres={"org_id": int(org_id)}' in src
    assert "LIKE" not in src.upper().replace("UNLIKE", ""), \
        "aucun filtre par motif de nom d'outil"


def test_status_porte_lusage_pour_tout_le_monde(monkeypatch):
    _wire(monkeypatch, org_rows=[])
    monkeypatch.setattr(billing_grants.db, "count_org_mcp_calls",
                        lambda oid, *, since: 42)
    monkeypatch.setattr(billing.db_billing, "get_org_subscription", lambda oid: None)
    st = billing.status(7)
    # Aucun don, aucun abonnement : le compteur est là quand même.
    assert st["granted"] == [] and st["usage"]["calls"] == 42


@pytest.mark.parametrize("sql_attendu", ["expires_at IS NULL OR expires_at > NOW()"])
def test_lecheance_mord_dans_le_seam_pas_dans_les_appelants(sql_attendu):
    """`has_option_comp` est le seul lecteur des dons pour l'entitlement : c'est LÀ
    que l'échéance doit filtrer, sinon chaque surface devrait connaître la règle —
    et une échéance qu'aucun chemin n'applique serait pire que pas d'échéance."""
    import inspect

    from oto_mcp.db import unipile as db_unipile

    src = inspect.getsource(db_unipile.has_option_comp)
    assert sql_attendu in src
    # Et le pendant : la console admin doit VOIR le don échu pour pouvoir le rouvrir.
    assert sql_attendu not in inspect.getsource(db_unipile.list_option_comps)
