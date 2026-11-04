"""Lot L7, PR 2 (blueprint ADR 0053) — l'arête « tout le monde », et l'autorité qui bascule.

Deux choses, et chacune a un mode de panne qui ne se verrait pas tout seul :

1. **L'arête « tout le monde »** comble le seul vrai trou du modèle (une clé plateforme
   ouverte n'avait aucun bénéficiaire). Le risque n'est pas qu'elle manque — c'est
   qu'elle **ressuscite** : si elle primait sur une arête nominative révoquée, retirer
   l'accès de quelqu'un sur une clé ouverte ne couperait plus rien, et l'acquis de L5
   (« la révocation est vraie ») redeviendrait faux **en silence**. D'où la règle
   testée ici : *nommer l'appelant prime, révoqué compris*.
2. **L'inversion sous drapeau.** `legacy` (le défaut) doit rendre le comportement
   d'aujourd'hui à l'octet près ; `chain` doit servir la MÊME clé par la MÊME sonde —
   seule la traversée change. Un drapeau mal orthographié vaut `legacy`.

Convention du repo : logique pure et gardes par stub ici ; le SQL s'exerce contre un
vrai PostgreSQL ailleurs.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

from oto_mcp import access, credentials_store, grants_chain, group_store, org_store
from oto_mcp.access import chain_resolution, chain_shadow
from oto_mcp.db import access_shadow as db_shadow
from oto_mcp.db import grants as db_grants

# La racine du dépôt n'est pas dans `sys.path` en CI (pas de `__init__.py`) : `scripts`
# ne s'importait que parce qu'un banc collecté avant l'y avait mise (oto#116).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Sans base : le palier plateforme lit les droits de la personne (#1090).
pytestmark = pytest.mark.usefixtures("sans_droit_declare")

REF = "platform:serper:env"
# La clé serper telle qu'elle est en prod : ouverte à tous, quota par défaut 200.
OUVERTE = [{"label": "env", "share_mode": "open", "share_down": [], "meta": {"rate_limit": 200}}]
# Une clé FERMÉE sur une allowlist : là, chaque bénéficiaire a son arête.
FERMEE = [{"label": "env", "share_mode": "closed", "share_down": ["org:42"],
           "meta": {"rate_limit": 200}}]


def _edge(grantee, quota=200, revoked=None, edge_id=1):
    return {"id": edge_id, "resource_id": REF, "grantor_kind": "platform",
            "grantor_id": "platform", "grantee_kind": grantee[0],
            "grantee_id": grantee[1], "constraints": {"quota": quota} if quota else {},
            "parent_id": None, "source": "manual", "created_by": None,
            "created_at": None, "revoked_at": revoked}


@pytest.fixture
def socle(monkeypatch):
    """Aucune clé BYO, une clé plateforme ouverte, aucune arête — l'état d'avant."""
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: False)
    monkeypatch.setattr(credentials_store, "instance_suspended",
                        lambda et, eid, p, account="": False)
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [dict(i) for i in OUVERTE] if p == "serper" else [])
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda s, o=None: [])
    monkeypatch.setattr(group_store, "has_group_secret", lambda g, p: False)
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr(db_shadow, "bump_shadow",
                        lambda c, o, k, n=1, sample=None: None)
    chain_shadow._accords.clear()
    chain_shadow._dernier_versement.clear()
    yield
    chain_shadow._accords.clear()
    chain_shadow._dernier_versement.clear()


def _aretes(monkeypatch, rows):
    """`edges_for` filtre sur les grantees demandés, comme le vrai SQL — sinon un test
    passerait en rendant une arête que la requête réelle n'aurait pas ramenée."""
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [
        r for r in rows if (r["grantee_kind"], r["grantee_id"]) in set(grantees)])


# ── 1. L'arête « tout le monde » ──────────────────────────────────────────────

def test_sans_arete_le_trou_est_toujours_signale(socle):
    pick, hors_modele = chain_resolution.chain_verdict("u", "serper", org=None)
    assert pick is None
    assert hors_modele == chain_resolution.FREE_TIER_HORS_MODELE, (
        "une clé OUVERTE que rien n'exprime a sa propre nuance — celle que l'arête "
        "« tout le monde » éteint (la nuance sœur, `partage_hors_modele`, est celle "
        "d'une clé fermée sur allowlist)")


def test_avec_l_arete_la_chaine_accorde_et_le_trou_s_eteint(socle, monkeypatch):
    """C'est la mesure qui ouvre la porte du retrait : la classe `free_tier_hors_modele`
    ne compte plus « la clé est ouverte » mais « la clé est ouverte ET rien ne le
    dit » — elle tombe donc à zéro exactement quand la commande est passée."""
    _aretes(monkeypatch, [_edge(grants_chain.EVERYONE, quota=200)])
    pick, hors_modele = chain_resolution.chain_verdict("u", "serper", org=None)
    assert pick is not None and pick.via == "tout_le_monde"
    assert hors_modele is None, "l'arête posée, il n'y a plus de trou à signaler"


def test_une_arete_nominative_REVOQUEE_prime_sur_tout_le_monde(socle, monkeypatch):
    """Le mode de panne qui compte. Sans cette priorité, révoquer l'accès d'une
    personne sur une clé ouverte ne couperait rien — l'arête « tout le monde » la
    re-accorderait aussitôt, et « la révocation est vraie » (acquis de L5) serait faux
    sans qu'aucun test ne rougisse."""
    _aretes(monkeypatch, [_edge(grants_chain.EVERYONE, quota=200, edge_id=1),
                          _edge(("user", "u"), quota=50, revoked="2026-08-29", edge_id=2)])
    pick, _ = chain_resolution.chain_verdict("u", "serper", org=None)
    assert pick is None, "un accès révoqué nominativement doit rester coupé"


def test_une_arete_nominative_VIVANTE_prime_aussi(socle, monkeypatch):
    _aretes(monkeypatch, [_edge(grants_chain.EVERYONE, quota=200, edge_id=1),
                          _edge(("user", "u"), quota=5000, edge_id=2)])
    pick, _ = chain_resolution.chain_verdict("u", "serper", org=None)
    assert pick is not None and pick.via == "grant"


def test_le_chemin_servi_d_aujourd_hui_ne_voit_PAS_l_arete_tout_le_monde(socle, monkeypatch):
    """`platform_rung` est la fenêtre L5, et elle DÉCIDE pour les connecteurs basculés.
    Lui faire voir l'arête « tout le monde » ferait ressusciter, sur le chemin servi
    d'aujourd'hui, un accès individuel révoqué. Elle reste donc aveugle : l'arête ne se
    lit que sous l'autorité de la chaîne."""
    _aretes(monkeypatch, [_edge(grants_chain.EVERYONE, quota=200)])
    assert grants_chain.platform_rung("u", "serper", None) is None  # MUET ⟹ ancien chemin
    assert grants_chain.EVERYONE not in grants_chain.grantee_scopes("u", 42)


# ── 2. La commande de migration ───────────────────────────────────────────────

@pytest.fixture
def cmd(monkeypatch):
    from scripts import seed_everyone_edges as s
    monkeypatch.setattr(credentials_store, "list_platform_credentials",
                        lambda c=None: [{"connector": "serper", "label": "env"}])
    return s


def test_la_commande_propose_l_arete_tout_le_monde_sur_une_cle_ouverte(socle, cmd, monkeypatch):
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, k, i, conn=None: False)
    m = cmd._manquantes()
    assert [(x["genre"], x["grantee"]) for x in m] == [
        ("tout_le_monde", grants_chain.EVERYONE)]
    assert m[0]["constraints"] == {"quota": 200}


def test_la_commande_ne_ressuscite_pas_une_arete_deja_posee(socle, cmd, monkeypatch):
    """`edge_exists` compte les RÉVOQUÉES : éteindre un free-tier se fera en révoquant
    l'arête « tout le monde », et un second passage de la commande ne doit pas la
    rallumer."""
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, k, i, conn=None: True)
    assert cmd._manquantes() == []


def test_la_commande_rattrape_les_nominatives_restees_derriere(socle, cmd, monkeypatch):
    """Le semis de L5 ne couvrait que les connecteurs basculés. Relevé prod du 29/08 :
    9 orgs sur une clé fermée, 9 personnes sur des quotas nominatifs — sans elles, la
    bascule d'autorité perdrait des accès et des plafonds en silence."""
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [
        {"label": "env", "share_mode": "closed", "share_down": ["org:42"],
         "meta": {"rate_limit": 200,
                                    "rate_limit_by": {"user:alice": 50}}}])
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, k, i, conn=None: False)
    m = cmd._manquantes()
    # Clé FERMÉE ⟹ pas d'arête « tout le monde ». Deux nominatives, quotas repris.
    assert {x["genre"] for x in m} == {"nominative"}
    assert {(x["grantee"], x["constraints"].get("quota")) for x in m} == {
        (("org", "42"), 200), (("user", "alice"), 50)}


def test_un_scope_hors_vocabulaire_est_NOMME_et_pas_pose(socle, cmd, monkeypatch):
    monkeypatch.setattr(credentials_store, "list_platform_instances", lambda p: [
        {"label": "env", "share_mode": "closed", "share_down": ["group:7"],
         "meta": {}}])
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, k, i, conn=None: False)
    m = cmd._manquantes()
    assert [x["genre"] for x in m] == ["hors_vocabulaire"]


def test_le_dry_run_n_ecrit_rien(socle, cmd, monkeypatch):
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, k, i, conn=None: False)
    monkeypatch.setattr(db_grants, "insert_grant",
                        lambda **k: pytest.fail("le dry-run a écrit"))
    assert cmd.main(apply=False, connector=None) == 0


def test_apply_pose_les_aretes_sans_toucher_au_coffre(socle, cmd, monkeypatch):
    """Le geste du 31/07 n'existe plus : accorder n'écrit QUE dans `grants`."""
    poses: list = []
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, k, i, conn=None: False)
    monkeypatch.setattr(db_grants, "insert_grant", lambda **k: poses.append(k) or 1)
    for interdit in ("platform_grant", "platform_revoke", "set_credential"):
        if hasattr(credentials_store, interdit):
            monkeypatch.setattr(credentials_store, interdit,
                                lambda *a, **k: pytest.fail(f"{interdit} sur ce chemin"))
    cmd.main(apply=True, connector=None)
    assert [p["grantee_kind"] for p in poses] == ["platform"]
    assert poses[0]["source"] == "manual" and poses[0]["created_by"] == "migration:l7"


# ── 3. L'inversion sous drapeau ───────────────────────────────────────────────

def test_le_drapeau_par_defaut_et_mal_orthographie_valent_legacy(monkeypatch):
    monkeypatch.delenv("OTO_L7_DECIDE", raising=False)
    assert chain_shadow.decide_mode() == chain_shadow.DECIDE_LEGACY
    monkeypatch.setenv("OTO_L7_DECIDE", "chaine")   # faute de frappe
    assert chain_shadow.decide_mode() == chain_shadow.DECIDE_LEGACY
    monkeypatch.setenv("OTO_L7_DECIDE", "CHAIN")
    assert chain_shadow.chain_decides() is True


def _harnais(monkeypatch):
    """La forme prod : aucune clé BYO, la clé plateforme ouverte, quota 200."""
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.credentials_store, "list_platform_instances",
                        lambda p: [dict(i) for i in OUVERTE])
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLAT")
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 0)
    monkeypatch.setattr(credentials_store, "has_credential",
                        lambda et, eid, p, account=None: False)
    monkeypatch.setattr(credentials_store, "instance_suspended",
                        lambda et, eid, p, account="": False)
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda s, o=None: [])
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(db_shadow, "bump_shadow", lambda c, o, k, n=1, sample=None: None)


def test_sous_chain_la_meme_cle_est_servie_que_sous_legacy(monkeypatch):
    """L'inversion ne réécrit pas le FETCH : elle réutilise la sonde. La clé servie est
    donc la même des deux côtés du drapeau — c'est ce qui rend la bascule réversible
    sans revert."""
    _harnais(monkeypatch)
    _aretes(monkeypatch, [_edge(grants_chain.EVERYONE, quota=200)])
    monkeypatch.setenv("OTO_L7_DECIDE", "legacy")
    avant = access.resolve_credential("serper", sub="u")
    monkeypatch.setenv("OTO_L7_DECIDE", "chain")
    apres = access.resolve_credential("serper", sub="u")
    assert (apres.key, apres.is_platform, apres.mode, apres.entity_type,
            apres.entity_id) == (avant.key, avant.is_platform, avant.mode,
                                 avant.entity_type, avant.entity_id)


def test_sous_chain_un_releve_inverse_qui_explose_ne_casse_pas_la_resolution(monkeypatch):
    """Sous l'autorité de la chaîne, c'est l'ANCIEN chemin qu'on relève pour comparer.
    S'il casse, la résolution servie ne doit pas en souffrir : l'observation est
    enveloppée chez elle, jamais au site d'appel."""
    _harnais(monkeypatch)
    _aretes(monkeypatch, [_edge(grants_chain.EVERYONE, quota=200)])
    monkeypatch.setenv("OTO_L7_DECIDE", "chain")

    def _boum(*a, **k):
        raise RuntimeError("l'ancien chemin est cassé")
    monkeypatch.setattr(access, "cascade_winner", _boum)
    assert access.resolve_credential("serper", sub="u").key == "PLAT"


def test_la_lecture_reste_celle_de_la_sonde(socle, monkeypatch):
    """La preuve que le fetch n'est pas réécrit : une sonde explosive fait remonter SON
    erreur. Si `rung_for_pick` relisait le coffre par lui-même, ce test passerait au
    vert en silence — et la sélection de compte multi-identités aurait deux copies."""
    class _Sonde:
        member = staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("sonde")))
        group = org = platform = staticmethod(lambda *a: None)
    pick = chain_resolution.ChainPick("user", credentials_store.MEMBER, "7:u")
    with pytest.raises(RuntimeError, match="sonde"):
        chain_resolution.rung_for_pick(pick, _Sonde, "u", "serper", 7)


# ── Le barreau TENANT, servi et pas seulement désigné ────────────────────────

def test_sous_chain_une_cle_de_TENANT_est_vraiment_SERVIE(socle, monkeypatch):
    """Le trou qu'une revue a trouvé, et que le shadow ne pouvait PAS voir.

    `rung_for_pick` traitait `user`, `group`, `org`, puis tombait dans la branche
    plateforme pour tout le reste — `tenant` compris. Conséquences en chaîne : la
    sonde `probe.tenant` n'était jamais appelée, la clé SERVIE devenait celle de la
    plateforme, et `tenant_budget.enforce` — que l'appelant conditionne à
    `win.mode == "tenant"` — était sauté. Le shadow comparait deux DÉSIGNATIONS et
    voyait un `accord` : le drapeau aurait annulé en silence la pièce 1 des L-clés,
    qui est en prod.

    Le test porte donc sur la CLÉ RENDUE, pas sur le `ChainPick` : c'est la seule
    assertion que le bug ne pouvait pas satisfaire."""
    from oto_mcp.access import chain_resolution

    class _Sonde:
        member = staticmethod(lambda *a: None)
        group = staticmethod(lambda *a: None)
        org = staticmethod(lambda *a: None)
        tenant = staticmethod(lambda slug, p: ("CLE-DU-TENANT", ""))
        platform = staticmethod(
            lambda *a: pytest.fail("le palier plateforme a été servi à la place du tenant"))

    pick = chain_resolution.ChainPick("tenant", credentials_store.TENANT, "acme")
    rung = chain_resolution.rung_for_pick(pick, _Sonde, "acme:u", "serper", 7)

    assert rung is not None
    assert rung.mode == "tenant", "le barreau servi doit être celui que la chaîne désigne"
    assert rung.payload == "CLE-DU-TENANT", "c'est la clé du TENANT qui doit être servie"
    assert rung.entity_id == "acme"
    # `via` est TRADUIT dans le vocabulaire du walker : `status.py` lit `via == 'local'`.
    assert rung.via == "local"


def test_le_via_d_une_arete_de_tenant_reste_un_grant(socle):
    from oto_mcp.access import chain_resolution

    class _Sonde:
        member = group = org = staticmethod(lambda *a: None)
        tenant = staticmethod(lambda slug, p: ("K", ""))
        platform = staticmethod(lambda *a: None)

    pick = chain_resolution.ChainPick("tenant", credentials_store.TENANT, "acme",
                                      via="grant")
    assert chain_resolution.rung_for_pick(pick, _Sonde, "acme:u", "serper", 7).via == "grant"


def test_une_cle_de_tenant_absente_ne_retombe_pas_sur_la_plateforme(socle):
    """Si la sonde du tenant ne rend rien, le barreau n'existe pas — il ne se
    remplace pas par la clé plateforme sans le dire."""
    from oto_mcp.access import chain_resolution

    class _Sonde:
        member = group = org = staticmethod(lambda *a: None)
        tenant = staticmethod(lambda slug, p: None)
        platform = staticmethod(lambda *a: pytest.fail("repli muet sur la plateforme"))

    pick = chain_resolution.ChainPick("tenant", credentials_store.TENANT, "acme")
    assert chain_resolution.rung_for_pick(pick, _Sonde, "acme:u", "serper", 7) is None


# ── La commande borne vraiment quand on la borne ─────────────────────────────

@pytest.mark.parametrize("argv", [["--connector"], ["--connector", "--apply"],
                                  ["--connector", "--apply", "serper"]])
def test_un_connecteur_sans_valeur_est_REFUSE(argv, monkeypatch):
    """`--connector` sans nom valait `None` — c'est-à-dire TOUS les connecteurs. Avec
    `--apply`, la vague devenait le semis complet. Un drapeau qui borne ne doit jamais
    élargir quand il est mal tapé."""
    from scripts import seed_everyone_edges as s

    monkeypatch.setattr(s, "_manquantes", lambda c=None: pytest.fail(
        "la commande a énuméré malgré un --connector sans valeur"))
    assert s.run(argv) == 2


def test_un_connecteur_NOMME_borne_bien_l_enumeration(monkeypatch):
    """Le pendant positif : bien nommé, l'argument arrive jusqu'à l'énumération."""
    from scripts import seed_everyone_edges as s

    vus: list = []
    monkeypatch.setattr(s, "_manquantes", lambda c=None: vus.append(c) or [])
    assert s.run(["--connector", "serper"]) == 0
    assert vus == ["serper"]
