"""L-clés, PR 1 (blueprint ADR 0052) — l'étage TENANT de la cascade, et sa preuve d'inertie.

Le lot ajoute un barreau entre l'org et la plateforme : une clé posée sur un tenant
sert à toutes les orgs de ce tenant qui n'en ont pas de plus proche. Ce fichier garde
les quatre choses qui, si elles cédaient, feraient mentir une surface :

1. **L'ordre.** `user > cross-org > groupe > org > TENANT > plateforme`, dans le walker
   unique et nulle part ailleurs — sondes injectées, aucun stub de base.
2. **L'inertie sans clé tenant.** Le tenant d'un appelant se lit sur son sub qualifié
   (jamais sur le rattachement d'org — lot L1) ; un sub NU relève du tenant primaire,
   dont les clés partagées SONT les instances plateforme : son barreau n'est jamais
   sondé. Toute la matrice historique du walker rend donc les mêmes verdicts qu'avant
   — c'est la moitié « dans l'arbre » du différentiel de la PR.
3. **Les deux voies de la fenêtre L7 s'accordent.** La chaîne 0053 calcule le même
   étage, sinon le shadow compterait une divergence `inconnu` que ce lot aurait créée.
4. **La grammaire de ref** porte le niveau `tenant` (relevé d'appel : quelle clé a servi).
"""
from __future__ import annotations

import pytest

from oto_mcp import access, credentials_store, instance_refs, tenancy, tenant_vault
from oto_mcp.access import cascade, chain_resolution, chain_shadow

PILOTE = "pilote"
SUB_TENANT = f"{PILOTE}:u1"      # un compte du tenant pilote (sub qualifié, lot L2)
SUB_NU = "u1"                     # un compte du tenant primaire (sub nu)


@pytest.fixture(autouse=True)
def sans_arete(monkeypatch):
    """PR 2 : une clé tenant trouvée fait lire l'arête tenant→org. Ici, aucune —
    l'état MUET, celui de la PR 1."""
    from oto_mcp.db import grants as db_grants
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr(db_grants, "live_edges_for_grantee",
                        lambda kind, ident, prefix=None: [])


@pytest.fixture
def registre(monkeypatch):
    """Un tenant tiers chargé dans le registre du process — sans lui, tout sub est
    `oto` et le barreau n'existe pas."""
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": PILOTE, "issuer": "https://auth.pilote.test/oidc"}])),
        raising=False)


def probe(*, member=False, cross=False, group=False, org=False, tenant=False,
          platform=False, vu: list | None = None):
    """Sonde à six barreaux ; `vu` note les barreaux effectivement SONDÉS (un barreau
    sauté ne coûte rien — c'est ce que la mesure mono-loop demande)."""
    def _note(nom, val):
        if vu is not None:
            vu.append(nom)
        return val
    return access.CascadeProbe(
        member=lambda s, o, p: _note("member", ("MK", "") if member else None),
        member_cross=lambda s, o, p: _note("cross", "XK" if cross else None),
        legacy_user=lambda s, p: None,
        group=lambda g, p: _note("group", "GK" if group else None),
        org=lambda o, p: _note("org", "OK" if org else None),
        tenant=lambda t, p: _note("tenant", "TK" if tenant else None),
        platform=lambda s, p, o: _note("platform", {"label": "env", "secret": "PK",
                                                    "daily_quota": None}
                                       if platform else None),
    )


# ── 1. l'ordre ────────────────────────────────────────────────────────────────

def test_le_tenant_est_le_barreau_entre_l_org_et_la_plateforme(registre):
    hits = list(access.walk_cascade(SUB_TENANT, "serper", org=1, group=2,
                                    probe=probe(member=True, group=True, org=True,
                                                tenant=True, platform=True)))
    assert [r.mode for r in hits] == ["user", "group", "org", "tenant", "platform"]
    tenant = hits[3]
    assert (tenant.entity_type, tenant.entity_id, tenant.payload) == (
        credentials_store.TENANT, PILOTE, "TK")


def test_la_cle_tenant_gagne_quand_rien_de_plus_proche_ne_resout(registre):
    win = access.cascade_winner(SUB_TENANT, "serper", org=1, group=2,
                                probe=probe(tenant=True, platform=True))
    assert win is not None and win.mode == "tenant" and win.entity_id == PILOTE


def test_une_cle_d_org_prime_sur_la_cle_tenant(registre):
    win = access.cascade_winner(SUB_TENANT, "serper", org=1, group=None,
                                probe=probe(org=True, tenant=True))
    assert win.mode == "org"


def test_le_barreau_tenant_suit_le_gate_org_partageable(registre):
    """`silae` n'est pas org-partageable : ni équipe, ni org, ni tenant — une clé
    partagée sur un connecteur par-personne n'est lue par personne."""
    vu: list = []
    win = access.cascade_winner(SUB_TENANT, "silae", org=1, group=2,
                                probe=probe(tenant=True, vu=vu))
    assert win is None and "tenant" not in vu


def test_want_byo_garde_le_barreau_tenant(registre):
    """La clé de tenant est le BYO du tenant (il gère sa propre instance chez le
    fournisseur) : `want='byo'` coupe la plateforme, pas le tenant."""
    win = access.cascade_winner(SUB_TENANT, "serper", org=1, group=None,
                                probe=probe(tenant=True, platform=True), want="byo")
    assert win is not None and win.mode == "tenant"


# ── 2. l'inertie sans clé tenant ──────────────────────────────────────────────

def test_un_sub_du_tenant_primaire_ne_sonde_jamais_le_barreau_tenant(registre):
    """Le tenant `oto` n'a pas de clé de tenant : ses clés partagées sont des instances
    PLATEFORME (avec leurs grants). Sonder le barreau coûterait une lecture par appel
    à 99 % du trafic pour ne jamais rien trouver."""
    vu: list = []
    win = access.cascade_winner(SUB_NU, "serper", org=1, group=None,
                                probe=probe(tenant=True, platform=True, vu=vu))
    assert win.mode == "platform"
    assert "tenant" not in vu


def test_l_anonyme_n_a_pas_de_tenant(registre):
    """Sans sub, aucun tenant ne se lit (le rattachement d'org n'est pas un chemin de
    résolution, L1) : l'endpoint anonyme garde sa cascade `org > plateforme`."""
    vu: list = []
    win = access.cascade_winner(None, "serper", org=1, group=None,
                                probe=probe(org=True, tenant=True, vu=vu))
    assert win.mode == "org" and "tenant" not in vu


def test_rung_tenant_ne_rend_un_slug_que_pour_un_tenant_tiers(registre):
    assert tenant_vault.rung_tenant(SUB_TENANT) == PILOTE
    assert tenant_vault.rung_tenant(SUB_NU) is None
    assert tenant_vault.rung_tenant(None) is None


@pytest.mark.parametrize("flags,expected", [
    (dict(member=True, group=True, org=True, platform=True), "user"),
    (dict(group=True, org=True, platform=True), "group"),
    (dict(org=True, platform=True), "org"),
    (dict(platform=True), "platform"),
    (dict(), None),
])
def test_sans_cle_tenant_la_matrice_historique_rend_les_memes_verdicts(registre, flags,
                                                                       expected):
    """Même sub d'un tenant tiers, même matrice que `test_cascade_walker` : tant
    qu'aucune clé tenant n'existe, le gagnant est celui d'avant le lot."""
    win = access.cascade_winner(SUB_TENANT, "serper", org=1, group=2,
                                probe=probe(**flags))
    assert (win.mode if win else None) == expected


def test_les_sondes_reelles_portent_le_barreau_tenant(monkeypatch, registre):
    """PRESENCE et FETCH répondent pareil sur le barreau — le contrat anti-« l'UI
    ment » (résolution vs statut), étendu au nouvel étage."""
    monkeypatch.setattr(tenant_vault, "has_tenant_secret", lambda t, p: t == PILOTE)
    monkeypatch.setattr(tenant_vault, "get_tenant_secret",
                        lambda t, p, account="": "TK" if t == PILOTE else None)
    assert access.PRESENCE_PROBE.tenant(PILOTE, "serper") is True
    assert access.FETCH_PROBE.tenant(PILOTE, "serper") == "TK"
    assert access.PRESENCE_PROBE.tenant("autre", "serper") is None
    assert access.FETCH_PROBE.tenant("autre", "serper") is None


# ── 3. la fenêtre L7 : les deux voies s'accordent ─────────────────────────────

@pytest.fixture
def vide(monkeypatch):
    from oto_mcp import group_store, org_store
    from oto_mcp.db import grants as db_grants
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


def test_la_chaine_designe_le_meme_palier_sur_une_cle_tenant(vide, monkeypatch, registre):
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: (et, eid) == ("tenant", PILOTE))
    pick = chain_resolution.chain_winner(SUB_TENANT, "serper", org=7)
    legacy = cascade.CascadeRung("tenant", credentials_store.TENANT, PILOTE, "TK")
    assert pick is not None and pick.mode == "tenant" and pick.entity_id == PILOTE
    assert chain_shadow.classify(legacy, pick, acl_refus=False,
                                 hors_modele=None) == chain_shadow.ACCORD


def test_la_chaine_ignore_la_cle_tenant_pour_un_sub_du_tenant_primaire(vide, monkeypatch,
                                                                       registre):
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: et == "tenant")
    assert chain_resolution.chain_winner(SUB_NU, "serper", org=7) is None


# ── 4. la grammaire de ref ────────────────────────────────────────────────────

def test_le_ref_d_une_cle_tenant_fait_l_aller_retour():
    ref = instance_refs.ref_for_credential("tenant", PILOTE, "serper", "")
    assert ref == f"tenant:{PILOTE}:serper"
    parsed = instance_refs.parse_ref(ref)
    assert (parsed.level, parsed.tenant, parsed.connector, parsed.account) == (
        "tenant", PILOTE, "serper", "")
    assert instance_refs.format_ref(parsed) == ref
    nomme = instance_refs.ref_for_credential("tenant", PILOTE, "zoho", "editor:eu")
    assert instance_refs.parse_ref(nomme).account == "editor:eu"
    assert instance_refs.format_ref(instance_refs.parse_ref(nomme)) == nomme
