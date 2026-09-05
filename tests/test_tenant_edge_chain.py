"""L-clés, PR 2 — l'arête tenant→org de la chaîne 0053, et ce qu'elle change (rien, sans arête).

L'arête `tenant:{slug}:{connecteur} —grant→ org:{id}` porte le BUDGET PAR ORG (R10,
tranché le 12/08 : budget partagé, la lettre de D7). Trois états, les mêmes que la
clé plateforme au lot L5 :

| état | condition | walker | chaîne 0053 |
|---|---|---|---|
| MUETTE | aucune arête n'a jamais visé cette org | la clé sert (PR 1) | `appartenance` |
| ACCORDE | ≥1 arête vivante | la clé sert, budget débité | `grant` |
| REFUSE | des arêtes, toutes révoquées | le barreau est SAUTÉ — l'org retombe sur la plateforme | idem |

Ce fichier tient : l'inertie sans arête (le différentiel de la PR), l'accord des deux
voies dans les trois états (aucun `inconnu` créé), l'anonyme servi PAR l'arête et
jamais par le rattachement d'org, et le budget qui refuse quand il est épuisé.
Sondes injectées, arêtes stubbées : aucune base.
"""
from __future__ import annotations

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, credentials_store, grants_chain, tenancy, tenant_vault
from oto_mcp.access import cascade, chain_resolution, chain_shadow, tenant_budget
from oto_mcp.db import grants as db_grants

PILOTE = "pilote"
SUB_T = f"{PILOTE}:u1"
ORG = 178
REF = f"tenant:{PILOTE}:serper"


def _edge(org=ORG, quota=50, revoked=None, edge_id=7):
    return {"id": edge_id, "resource_kind": "connector_instance", "resource_id": REF,
            "grantor_kind": "tenant", "grantor_id": PILOTE, "grantee_kind": "org",
            "grantee_id": str(org), "constraints": {"quota": quota} if quota else {},
            "parent_id": None, "source": "manual", "created_by": None,
            "created_at": None, "revoked_at": revoked}


@pytest.fixture
def registre(monkeypatch):
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": PILOTE, "issuer": "https://auth.pilote.test/oidc"}])),
        raising=False)


@pytest.fixture
def aretes(monkeypatch):
    """Les arêtes, en mémoire : `[]` = muette."""
    etat = {"edges": [], "lu": 0}
    def _edges_for(ref, grantees):
        etat["lu"] += 1
        return [e for e in etat["edges"] if e["resource_id"] == ref
                and (e["grantee_kind"], e["grantee_id"]) in [(k, str(i)) for k, i in grantees]]
    monkeypatch.setattr(db_grants, "edges_for", _edges_for)
    monkeypatch.setattr(db_grants, "live_edges_for_grantee",
                        lambda kind, ident, prefix=None: [
                            e for e in etat["edges"] if e["revoked_at"] is None
                            and (e["grantee_kind"], e["grantee_id"]) == (kind, str(ident))
                            and (not prefix or e["resource_id"].startswith(prefix))])
    return etat


def probe(*, tenant=True, platform=False):
    return access.CascadeProbe(
        member=lambda s, o, p: None, member_cross=lambda s, o, p: None,
        legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: None,
        tenant=lambda t, p: ("TK" if tenant else None),
        platform=lambda s, p, o: ({"label": "env", "secret": "PK", "daily_quota": None}
                                  if platform else None))


# ── 1. l'inertie sans arête ───────────────────────────────────────────────────

def test_sans_arete_la_cle_tenant_sert_comme_en_pr1(registre, aretes):
    win = access.cascade_winner(SUB_T, "serper", org=ORG, group=None,
                                probe=probe(platform=True))
    assert (win.mode, win.entity_id, win.via) == ("tenant", PILOTE, "local")


def test_sans_cle_tenant_aucune_arete_n_est_lue(registre, aretes):
    """La chaîne n'est consultée qu'APRÈS que la sonde a trouvé une clé : sans clé,
    zéro lecture d'arête — le coût du lot reste nul là où il ne peut rien servir."""
    access.cascade_winner(SUB_T, "serper", org=ORG, group=None, probe=probe(tenant=False))
    assert aretes["lu"] == 0


# ── 2. les trois états, des deux côtés ────────────────────────────────────────

def test_une_arete_vivante_accorde_et_le_dit(registre, aretes):
    aretes["edges"] = [_edge()]
    win = access.cascade_winner(SUB_T, "serper", org=ORG, group=None, probe=probe())
    assert (win.mode, win.via) == ("tenant", "grant")


def test_une_arete_revoquee_saute_le_barreau_et_l_org_retombe_sur_la_plateforme(registre,
                                                                                 aretes):
    aretes["edges"] = [_edge(revoked="2026-08-29")]
    win = access.cascade_winner(SUB_T, "serper", org=ORG, group=None,
                                probe=probe(platform=True))
    assert win.mode == "platform"
    assert access.cascade_winner(SUB_T, "serper", org=ORG, group=None, probe=probe()) is None


def test_l_arete_d_une_autre_org_ne_compte_pas(registre, aretes):
    aretes["edges"] = [_edge(org=999, revoked="2026-08-29")]
    win = access.cascade_winner(SUB_T, "serper", org=ORG, group=None, probe=probe())
    assert (win.mode, win.via) == ("tenant", "local")


@pytest.fixture
def vide(monkeypatch):
    from oto_mcp import group_store, org_store
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: et == credentials_store.TENANT)
    monkeypatch.setattr(credentials_store, "instance_suspended",
                        lambda et, eid, p, account="": False)
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [])
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda s, o=None: [])
    monkeypatch.setattr(group_store, "has_group_secret", lambda g, p: False)
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(access, "current_group", lambda sub: None)


@pytest.mark.parametrize("edges,attendu", [
    ([], "appartenance"), ([_edge()], "grant"),
])
def test_la_chaine_designe_le_meme_palier_dans_les_deux_etats_ouverts(registre, aretes, vide,
                                                                       edges, attendu):
    aretes["edges"] = edges
    pick = chain_resolution.chain_winner(SUB_T, "serper", org=ORG)
    win = access.cascade_winner(SUB_T, "serper", org=ORG, group=None, probe=probe())
    assert pick.mode == "tenant" and pick.via == attendu
    assert chain_shadow.classify(win, pick, acl_refus=False,
                                 hors_modele=None) == chain_shadow.ACCORD


def test_la_chaine_saute_aussi_le_barreau_revoque(registre, aretes, vide):
    """REFUSE des deux côtés : la chaîne passe au palier suivant comme le walker,
    et les deux rendent « rien » — un accord, pas un `inconnu`."""
    aretes["edges"] = [_edge(revoked="2026-08-29")]
    pick = chain_resolution.chain_winner(SUB_T, "serper", org=ORG)
    win = access.cascade_winner(SUB_T, "serper", org=ORG, group=None, probe=probe())
    assert pick is None and win is None
    assert chain_shadow.classify(win, pick, acl_refus=False,
                                 hors_modele=None) == chain_shadow.ACCORD


# ── 3. l'anonyme, par l'arête seule ───────────────────────────────────────────

def test_l_anonyme_obtient_l_etage_par_une_arete_vivante(registre, aretes):
    aretes["edges"] = [_edge()]
    win = access.cascade_winner(None, "serper", org=ORG, group=None, probe=probe())
    assert (win.mode, win.entity_id, win.via) == ("tenant", PILOTE, "grant")


def test_l_anonyme_sans_arete_n_a_pas_l_etage(registre, aretes):
    assert access.cascade_winner(None, "serper", org=ORG, group=None, probe=probe()) is None
    aretes["edges"] = [_edge(revoked="2026-08-29")]
    assert access.cascade_winner(None, "serper", org=ORG, group=None, probe=probe()) is None


def test_tenant_for_org_ne_lit_que_les_aretes_de_ce_connecteur(registre, aretes):
    aretes["edges"] = [dict(_edge(), resource_id=f"tenant:{PILOTE}:hunter")]
    assert grants_chain.tenant_for_org(ORG, "serper") is None
    assert grants_chain.tenant_for_org(ORG, "hunter") == PILOTE


# ── 4. le budget ──────────────────────────────────────────────────────────────

def test_le_budget_refuse_quand_il_est_epuise_et_debite_sinon(registre, aretes, monkeypatch):
    aretes["edges"] = [_edge(quota=2)]
    compteur = {"n": 1}
    monkeypatch.setattr(db_grants, "counter_sum_today", lambda ref, k, i: compteur["n"])
    monkeypatch.setattr(db_grants, "bump_counter",
                        lambda gid, calls=1: compteur.__setitem__("n", compteur["n"] + calls))
    tenant_budget.enforce(PILOTE, "serper", ORG)          # 1 < 2 : passe, débite
    assert compteur["n"] == 2
    with pytest.raises(McpError) as e:
        tenant_budget.enforce(PILOTE, "serper", ORG)      # 2 >= 2 : refuse
    assert "budget" in str(e.value).lower() and PILOTE in str(e.value)


def test_sans_arete_le_budget_ne_lit_rien(registre, aretes, monkeypatch):
    monkeypatch.setattr(db_grants, "counter_sum_today",
                        lambda *a: pytest.fail("compteur lu sans arête"))
    tenant_budget.enforce(PILOTE, "serper", ORG)


def test_une_arete_sans_quota_debite_sans_borner(registre, aretes, monkeypatch):
    aretes["edges"] = [_edge(quota=0)]
    vus = []
    monkeypatch.setattr(db_grants, "bump_counter", lambda gid, calls=1: vus.append(gid))
    monkeypatch.setattr(db_grants, "counter_sum_today",
                        lambda *a: pytest.fail("compteur lu sans quota"))
    tenant_budget.enforce(PILOTE, "serper", ORG)
    assert vus == [7]


# ── 5. l'écriture d'une arête ─────────────────────────────────────────────────

def test_poser_une_arete_archive_la_precedente(monkeypatch):
    """D6 : un grant re-posé REMPLACE le précédent — sinon deux arêtes vivantes
    coexisteraient et la plus favorable gagnerait, donc baisser un budget n'aurait
    aucun effet."""
    vus = []
    monkeypatch.setattr(db_grants, "revoke_edges",
                        lambda ref, k, i: vus.append(("revoke", ref, k, i)) or 0)
    monkeypatch.setattr(db_grants, "insert_grant",
                        lambda **kw: vus.append(("insert", kw["resource_id"], kw["grantor_kind"],
                                                 kw["grantor_id"], kw["grantee_kind"],
                                                 kw["grantee_id"], kw["constraints"])) or 42)
    assert grants_chain.tenant_grant(PILOTE, "serper", ORG, 50, created_by="x") == 42
    assert vus == [("revoke", REF, "org", str(ORG)),
                   ("insert", REF, "tenant", PILOTE, "org", str(ORG), {"quota": 50})]
