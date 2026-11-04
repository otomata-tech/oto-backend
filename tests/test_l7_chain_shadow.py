"""Lot L7, PR 1 (blueprint ADR 0053) — la double lecture, et ce qu'elle doit prouver.

La fenêtre ouvre la porte d'un lot IRRÉVERSIBLE. Ce fichier garde les quatre choses
qui, si elles cédaient, rendraient la mesure inutile ou dangereuse :

1. **L'empreinte servie est identique.** La résolution rend le même credential, que
   le shadow soit allumé ou éteint — et une observation qui explose ne se voit nulle
   part ailleurs que dans son propre journal. Un shadow qui casserait une résolution
   serait pire que pas de shadow du tout.
2. **Les verdicts sont IDENTIQUES là où ils doivent l'être.** Sur les formes relevées
   en prod (clé membre, clé d'équipe, clé d'org, arête plateforme), la chaîne désigne
   le même palier que la cascade : la classe est `accord`.
3. **Chaque divergence attendue a sa classe, et `inconnu` reste atteignable.** Un
   garde-fou dont le rouge est inatteignable ne garde rien : le test qui prouve la
   classe `inconnu` est ce qui rend crédible la porte « zéro inconnu ».
4. **L'accord n'écrit pas par appel.** C'est ce qui garde le compteur hors du chemin
   chaud d'un serveur mono-loop — et hors de la contention de ligne mesurée pour R8.

Convention du repo : logique pure et gardes par stub ici ; le SQL de
`access_shadow_l7` s'exerce contre un vrai PostgreSQL dans
`test_grants_l5_migration_live.py`'s voisin de lot, pas ici.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, credentials_store, group_store, org_store
from oto_mcp.access import cascade, chain_resolution, chain_shadow
from oto_mcp.db import access_shadow as db_shadow
from oto_mcp.db import grants as db_grants

# Sans base : le palier plateforme lit les droits de la personne (#1090).
pytestmark = pytest.mark.usefixtures("sans_droit_declare")

# L'instance free-tier serper TELLE QU'ELLE EST EN PROD : ouverte à tous
# (`share_mode='open'`, aucune allowlist). C'est la forme que 0053 ne sait PAS dire —
# elle n'a pas de bénéficiaire, et « tout le monde » n'est pas un scope de grant.
FREE_SERPER = [{"label": "env", "share_mode": "open", "share_down": [],
                "meta": {"rate_limit": 200}}]
# La même instance, refermée sur une allowlist : là, un grant EXISTE à reproduire.
CLOSED_SERPER = [{"label": "env", "share_mode": "closed", "share_down": ["user:u"],
                  "meta": {"rate_limit": 200}}]


def _edge(grantee=("user", "u"), revoked=None):
    return {"id": 1, "resource_id": "platform:serper:env", "grantor_kind": "platform",
            "grantor_id": "platform", "grantee_kind": grantee[0],
            "grantee_id": grantee[1], "constraints": {"quota": 200}, "parent_id": None,
            "source": "manual", "created_by": None, "created_at": None,
            "revoked_at": revoked}


@pytest.fixture
def ecritures(monkeypatch):
    """Le compteur, en mémoire — et l'accumulateur d'accords remis à neuf : il est
    au niveau du MODULE, donc un test qui le laisse chargé décale le suivant."""
    vues: list = []
    monkeypatch.setattr(db_shadow, "bump_shadow",
                        lambda c, o, k, n=1, sample=None: vues.append((c, o, k, n)))
    chain_shadow._accords.clear()
    chain_shadow._dernier_versement.clear()
    yield vues
    chain_shadow._accords.clear()
    chain_shadow._dernier_versement.clear()


@pytest.fixture
def vide(monkeypatch):
    """Aucune clé nulle part, aucune arête : le socle sur lequel chaque test repose
    exactement ce qu'il veut voir."""
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: False)
    monkeypatch.setattr(credentials_store, "instance_suspended",
                        lambda et, eid, p, account="": False)
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda s, o=None: [])
    monkeypatch.setattr(group_store, "has_group_secret", lambda g, p: False)
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    yield


# ── 2. Les verdicts sont identiques là où ils doivent l'être ──────────────────

def test_la_chaine_designe_le_meme_palier_que_la_cascade_sur_une_cle_membre(vide, monkeypatch):
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: et == credentials_store.MEMBER)
    pick = chain_resolution.chain_winner("u", "serper", org=7)
    legacy = cascade.CascadeRung("user", credentials_store.MEMBER, "7:u", "K")
    assert pick is not None and pick.mode == "user"
    assert chain_shadow.classify(legacy, pick, hors_modele=None) == chain_shadow.ACCORD


def test_la_chaine_designe_le_meme_palier_sur_une_cle_d_equipe(vide, monkeypatch):
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda s, o=None: [{"group_id": 3, "name": "finance"}])
    monkeypatch.setattr(group_store, "has_group_secret", lambda g, p: g == 3)
    monkeypatch.setattr(access, "current_group", lambda sub: 3)   # équipe ACTIVE
    pick = chain_resolution.chain_winner("u", "serper", org=35)
    legacy = cascade.CascadeRung("group", "group", "3", "K")
    assert chain_shadow.classify(legacy, pick, hors_modele=None) == chain_shadow.ACCORD


def test_la_chaine_designe_le_meme_palier_sur_une_cle_d_org(vide, monkeypatch):
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: True)
    pick = chain_resolution.chain_winner("u", "serper", org=178)
    legacy = cascade.CascadeRung("org", "org", "178", "K")
    assert chain_shadow.classify(legacy, pick, hors_modele=None) == chain_shadow.ACCORD


def test_la_chaine_designe_le_meme_palier_sur_une_arete_plateforme(vide, monkeypatch):
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: CLOSED_SERPER)
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [_edge()])
    pick = chain_resolution.chain_winner("u", "serper", org=None)
    legacy = cascade.CascadeRung("platform", credentials_store.PLATFORM, "env", {})
    assert pick is not None and pick.via == "grant"
    assert chain_shadow.classify(legacy, pick, hors_modele=None) == chain_shadow.ACCORD


def test_la_chaine_designe_le_meme_palier_sur_une_cle_tenant(vide, monkeypatch):
    """L-clés PR 1 : l'étage tenant existe des DEUX côtés de la fenêtre — sinon le
    shadow compterait une divergence `inconnu` que le lot aurait créée."""
    from oto_mcp import tenancy
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "pilote", "issuer": "https://auth.pilote.test/oidc"}])),
        raising=False)
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: et == credentials_store.TENANT)
    pick = chain_resolution.chain_winner("pilote:u", "serper", org=7)
    legacy = cascade.CascadeRung("tenant", credentials_store.TENANT, "pilote", "K")
    assert pick is not None and pick.mode == "tenant"
    assert chain_shadow.classify(legacy, pick, hors_modele=None) == chain_shadow.ACCORD


def test_les_deux_refusent_ensemble_est_un_accord(vide):
    """Rien ne résout d'un côté, rien de l'autre : c'est un accord, pas un trou. Sans
    cette ligne, tout appel d'un connecteur non configuré compterait comme divergence
    et noierait la mesure."""
    assert chain_resolution.chain_winner("u", "serper", org=7) is None
    assert chain_shadow.classify(None, None, hors_modele=None) == chain_shadow.ACCORD


# ── 3. Les quatre divergences attendues, et l'inconnu qui doit rester joignable ──

def test_l_elargissement_d_equipe_est_classe_a_part(vide, monkeypatch):
    """La forme d'un client : la clé est sur « finance », le sujet y appartient mais
    son équipe ACTIVE est « sales ». La cascade ne lit que l'active et ne résout rien ;
    l'ensemble atteignable de D2 lit toutes les équipes et résout. Le comportement
    servi change chez un client nommé — d'où une classe à part, comptée par org."""
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda s, o=None: [{"group_id": 2, "name": "sales"},
                                           {"group_id": 3, "name": "finance"}])
    monkeypatch.setattr(group_store, "has_group_secret", lambda g, p: g == 3)
    monkeypatch.setattr(access, "current_group", lambda sub: 2)
    pick = chain_resolution.chain_winner("u", "serper", org=35)
    assert pick is not None and pick.group_id == 3
    assert chain_shadow.classify(None, pick, hors_modele=None) \
        == chain_shadow.ELARGISSEMENT_EQUIPE


def test_le_free_tier_ouvert_est_nomme_comme_un_trou_du_modele(vide, monkeypatch):
    """La clé plateforme ouverte à tous n'a aucun bénéficiaire dans 0053. L'ancien
    chemin l'accorde, la chaîne se tait. La classe le NOMME au lieu de le subir :
    elle doit tomber à zéro avant le retrait (PR 3), une fois l'arête « tout le
    monde » posée (arbitrage rendu le 29/08)."""
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: FREE_SERPER)
    pick, hors_modele = chain_resolution._platform_pick("u", "serper", None)
    assert pick is None and hors_modele == chain_shadow.FREE_TIER_HORS_MODELE
    legacy = cascade.CascadeRung("platform", credentials_store.PLATFORM, "env", {})
    assert chain_shadow.classify(legacy, None, hors_modele=chain_shadow.FREE_TIER_HORS_MODELE) \
        == chain_shadow.FREE_TIER_HORS_MODELE


def test_la_classe_inconnue_est_ATTEIGNABLE(vide):
    """Le rouge de la porte doit pouvoir s'allumer. Ici : les deux voies désignent une
    instance, mais pas la même, et aucune des explications nommées ne s'applique. Sans
    ce test, « zéro inconnu » pourrait n'être vrai que parce que rien ne le produit."""
    legacy = cascade.CascadeRung("org", "org", "178", "K")
    autre = chain_resolution.ChainPick("user", credentials_store.MEMBER, "178:u")
    assert chain_shadow.classify(legacy, autre, hors_modele=None) == chain_shadow.INCONNU


def test_le_vocabulaire_des_classes_est_ferme():
    """Toute classe rendue par `classify` est déclarée. Le compteur, la lentille admin
    et la porte se lisent sur cette liste : une sixième valeur inventée à l'exécution
    passerait sous les trois."""
    assert chain_shadow.INCONNU in chain_shadow.CLASSES
    assert len(set(chain_shadow.CLASSES)) == len(chain_shadow.CLASSES)


# ── 4. L'accord n'écrit pas par appel ─────────────────────────────────────────

def test_l_accord_ne_produit_pas_une_ecriture_par_appel(vide, ecritures):
    for _ in range(50):
        chain_shadow.observe("serper", "u", 7, None)
    assert len(ecritures) <= 1, (
        "l'accord — le cas nominal, donc le volume — doit être accumulé et versé au "
        "battement, jamais écrit par appel : c'est ce qui garde le compteur hors du "
        "chemin chaud et hors de la contention de ligne.")
    # Et les cinquante accords sont COMPTÉS, versés ou en attente. Sans cette ligne,
    # une observation qui échoue (avalée par `observe`, best-effort) n'écrit rien non
    # plus, et `<= 1` passe sans qu'aucun accord n'ait été constaté (#896).
    comptes = sum(n for *_, n in ecritures) + sum(chain_shadow._accords.values())
    assert comptes == 50


def test_une_divergence_s_ecrit_a_l_occurrence(vide, ecritures, monkeypatch):
    pick = chain_resolution.ChainPick("group", "group", "3", group_id=3)
    monkeypatch.setattr(chain_resolution, "chain_verdict", lambda *a, **k: (pick, None))
    chain_shadow.observe("serper", "u", 178, None)
    assert [(k, n) for _, _, k, n in ecritures] == [(chain_shadow.ELARGISSEMENT_EQUIPE, 1)]


def test_l_echantillon_ne_porte_aucun_sub_en_clair():
    ech = chain_shadow._sample("alexis@example.test", None,
                               chain_resolution.ChainPick("group", "group", "3", group_id=3))
    assert "alexis@example.test" not in repr(ech)
    assert ech["equipe"] == 3 and len(ech["sub_h"]) == 8


# ── 1. L'empreinte servie est identique ──────────────────────────────────────

def test_l_observation_ne_leve_jamais(vide, ecritures, monkeypatch):
    def _boum(*a, **k):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(chain_resolution, "chain_verdict", _boum)
    monkeypatch.setattr(db_shadow, "bump_shadow", _boum)
    chain_shadow.observe("serper", "u", 7, None)          # ne lève pas


def test_l_interrupteur_eteint_toute_lecture(vide, ecritures, monkeypatch):
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    monkeypatch.setattr(chain_resolution, "chain_verdict",
                        lambda *a, **k: pytest.fail("le shadow éteint a lu"))
    chain_shadow.observe("serper", "u", 7, None)
    assert ecritures == []


def test_la_resolution_servie_est_identique_shadow_allume_ou_eteint(monkeypatch, ecritures):
    """L'empreinte du lot : le même credential est rendu des deux côtés. La forme
    exercée est celle du free-tier serper relevé en prod — c'est-à-dire précisément
    celle où les deux voies DIVERGENT, donc le cas où une fuite de verdict se
    verrait."""
    def _socle():
        monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
        monkeypatch.setattr(access, "current_group", lambda sub: None)
        monkeypatch.setattr(access, "current_org", lambda sub: None)
        monkeypatch.setattr(access.credentials_store, "list_platform_instances",
                            lambda p: FREE_SERPER)
        monkeypatch.setattr(access.credentials_store, "get_credential",
                            lambda et, eid, p, account="": "PLAT")
        monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 0)
        monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [])
        monkeypatch.setattr(group_store, "list_groups_for_user", lambda s, o=None: [])
        monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
        monkeypatch.setattr(credentials_store, "has_credential",
                            lambda et, eid, p, account=None: False)

    _socle()
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    eteint = access.resolve_credential("serper", sub="u")
    monkeypatch.setenv("OTO_L7_SHADOW", "1")
    allume = access.resolve_credential("serper", sub="u")

    assert (allume.key, allume.is_platform, allume.mode, allume.entity_type,
            allume.entity_id, allume.account) == (
        eteint.key, eteint.is_platform, eteint.mode, eteint.entity_type,
        eteint.entity_id, eteint.account)
    # …et le shadow a bien vu passer la divergence qu'il est là pour compter.
    assert [k for _, _, k, _ in ecritures] == [chain_shadow.FREE_TIER_HORS_MODELE]
