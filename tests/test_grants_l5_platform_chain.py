"""Lot L5 (blueprint ADR 0053) — la clé plateforme entre dans le modèle d'accès.

Ce que ces tests garantissent, dans l'ordre de ce qui coûterait cher :

1. **Le repli est identique.** Un bénéficiaire que la chaîne ne connaît pas passe par
   l'ancien chemin, au caractère près. C'est ce qui rend la fenêtre de double lecture
   déployable sans qu'une seule personne perde un accès le jour même.
2. **La révocation coupe.** Une arête archivée fait dire NON à la chaîne, qui ne
   retombe donc PAS sur l'ancien chemin — sans quoi révoquer serait un no-op sur une
   clé free-tier, et « l'accès se retire » serait faux. Aucun cache à invalider :
   la lecture suivante voit `revoked_at`.
3. **Les neuf autres ne bougent pas d'un octet.** Pas une branche, pas une requête —
   vérifié en rendant toute lecture de chaîne EXPLOSIVE et en constatant que leur
   résolution ne la touche jamais.
4. **Accorder n'enlève plus rien à personne.** L'écriture d'un grant ne touche plus la
   ligne du coffre : c'est l'incident du 31/07 rendu structurellement impossible, et
   c'est vérifié en rendant l'accès au coffre explosif sur le chemin d'écriture.

Convention du repo : logique pure et gardes par stub ici ; le SQL s'exerce contre un
vrai PostgreSQL dans `test_grants_l5_migration_live.py`.
"""
from __future__ import annotations

import contextlib

import pytest

from oto_mcp import access, credentials_store, grants_chain, providers
from oto_mcp.db import grants as db_grants

# Sans base : le palier plateforme lit les droits de la personne (#1090).
pytestmark = pytest.mark.usefixtures("sans_droit_declare")

# L'instance fullenrich TELLE QU'ELLE EST EN PROD (relevé du 12/08/2026) : free-tier
# ouverte (`share_mode='open'`, `share_down=[]`), quota par défaut 5, deux grants
# nominatifs dans `rate_limit_by`. Les tests jouent contre CETTE forme, pas contre une
# forme idéale — c'est elle que la migration doit reproduire.
PROD_INSTANCE = {
    "label": "env", "share_mode": "open", "share_down": [], "meta": {"rate_limit": 5, "rate_limit_by": {"user:granted-sub": 200,
                                                "user:other-sub": 5}},
}
REF = "platform:fullenrich:env"


def _edge(grantee_kind="user", grantee_id="granted-sub", quota=200, revoked=None,
          edge_id=1):
    return {"id": edge_id, "resource_kind": "connector_instance", "resource_id": REF,
            "grantor_kind": "platform", "grantor_id": "platform",
            "grantee_kind": grantee_kind, "grantee_id": grantee_id,
            "constraints": {"quota": quota} if quota else {}, "parent_id": None,
            "source": "manual", "created_by": "migration:l5",
            "created_at": "2026-08-12 00:00:00", "revoked_at": revoked}


@pytest.fixture
def vault(monkeypatch):
    """La ligne du coffre, sans base."""
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [dict(PROD_INSTANCE)] if p == "fullenrich" else [])


def _edges(monkeypatch, rows):
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: list(rows))


# ── 1. Les trois états de la chaîne ────────────────────────────────────────────

def test_granted_beneficiary_resolves_exactly_as_before(vault, monkeypatch):
    """Arête vivante ⟹ la chaîne résout, avec LA MÊME clé et LE MÊME quota que
    l'ancien chemin. C'est la condition de la bascule : migrer ne change rien de ce
    que voit celui qui était déjà servi."""
    monkeypatch.setattr(access, "_platform_instance_usable", lambda *a: True)
    legacy = access._legacy_platform_grant_meta("granted-sub", "fullenrich", None)
    _edges(monkeypatch, [_edge()])
    chained = access._platform_grant_meta("granted-sub", "fullenrich", None)
    assert legacy == {"label": "env", "daily_quota": 200}
    # Une arête `user` désigne en plus le compteur que lit le refus (R6).
    assert chained == {**legacy, "edge_counter": REF}


def test_unknown_beneficiary_falls_back_identically(vault, monkeypatch):
    """Aucune arête n'a JAMAIS visé cet appelant ⟹ la chaîne est muette et l'ancien
    chemin répond seul. Le repli n'est pas « équivalent », il est le MÊME appel : on
    compare au résultat de `_legacy_platform_grant_meta` lui-même."""
    _edges(monkeypatch, [])
    assert (access._platform_grant_meta("nobody", "fullenrich", None)
            == access._legacy_platform_grant_meta("nobody", "fullenrich", None)
            == {"label": "env", "daily_quota": 5})


def test_revoked_edge_cuts_access_without_falling_back(vault, monkeypatch):
    """⚠️ LE test du lot. Des arêtes existent mais toutes révoquées ⟹ la chaîne dit
    NON, et ne retombe PAS sur l'ancien chemin. Sans cette règle, révoquer un grant
    sur une clé free-tier (ouverte à tous) ne couperait rien du tout.

    Et rien n'est à invalider : la révocation est vue à la lecture SUIVANTE — c'est
    l'argument du banc L0 contre le cache, ici sous forme de test."""
    assert access._legacy_platform_grant_meta("granted-sub", "fullenrich", None), (
        "prémisse du test : l'ancien chemin accorde (clé free-tier ouverte)")
    _edges(monkeypatch, [_edge(revoked="2026-08-12 10:00:00")])
    assert access._platform_grant_meta("granted-sub", "fullenrich", None) is None


def test_where_the_chain_has_an_opinion_the_old_path_is_not_read(vault, monkeypatch):
    """R6 : la fenêtre de double lecture est close. Là où la chaîne ACCORDE ou REFUSE,
    l'ancien chemin n'est plus lu — ni pour décider, ni pour journaliser un écart."""
    def boom(*a, **k):  # pragma: no cover - doit ne jamais être appelé
        raise AssertionError("l'ancien chemin a été lu alors que la chaîne a un avis")
    monkeypatch.setattr(access, "_legacy_platform_grant_meta", boom)
    _edges(monkeypatch, [_edge()])
    assert access._platform_grant_meta("granted-sub", "fullenrich", None)["label"] == "env"
    _edges(monkeypatch, [_edge(revoked="2026-08-12 10:00:00")])
    assert access._platform_grant_meta("granted-sub", "fullenrich", None) is None


# ── 2. Le quota : la règle vient de l'arête, le refus reste le même ────────────

def test_edge_quota_produces_the_same_refusal_as_before(vault, monkeypatch):
    """Quota épuisé ⟹ le MÊME refus qu'avant, mot pour mot : c'est le plafond porté
    par l'arête qui alimente le message historique, pas un nouveau chemin d'erreur."""
    from oto_mcp.mcp_errors import McpError
    monkeypatch.setattr(access.session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(access, "project_pinned_instance", lambda p, *a: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    # Sous une arête `user`, le compte du refus est celui de l'arête (R6) ; l'historique
    # `usage` ne décide plus — 0 ici, pour qu'un refus ne puisse venir que de l'arête.
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: 0)
    monkeypatch.setattr(db_grants, "counter_sum_today",
                        lambda ref, kind, gid: 7 if (ref, kind, gid) == (REF, "user",
                                                                         "granted-sub") else 0)
    monkeypatch.setattr(credentials_store, "get_credential", lambda *a, **k: "SECRET")
    _edges(monkeypatch, [_edge(quota=7)])
    with pytest.raises(McpError) as e:
        access._resolve_credential_impl("fullenrich", "auto", "granted-sub")
    assert "Platform quota fullenrich exceeded today (7/7)" in str(e.value)
    assert "key `env`" in str(e.value)


def test_user_edge_beats_org_edge(vault, monkeypatch):
    """Plusieurs voies vers la même instance ⟹ la plus FAVORABLE (0053-D5), et
    l'ordre de spécificité est celui de l'ancien chemin : `user:` prime sur l'org
    active — miroir exact d'`_platform_grantee_scope`."""
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    _edges(monkeypatch, [_edge(quota=10, edge_id=2),
                         _edge("org", "42", quota=1000, edge_id=3)])
    assert access._platform_grant_meta("granted-sub", "fullenrich", 42)["daily_quota"] == 10


def test_org_scope_is_the_ACTIVE_org_never_membership(monkeypatch):
    """Un grant d'org est métré per-contexte-d'org : un membre de X actif dans Y n'en
    profite pas. La migration doit reproduire ça, donc la liste des scopes aussi."""
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    assert grants_chain.grantee_scopes("s", 7) == [("user", "s"), ("org", "7")]
    assert grants_chain.grantee_scopes("s", None) == [("user", "s")]


def test_group_scope_is_the_ACTIVE_team_and_only_one_of_his(monkeypatch):
    """#1239 : l'équipe ACTIVE entre `user` et `org`, si l'appelant en est membre dans
    l'org active — jamais une équipe qu'il ne fait que nommer."""
    from oto_mcp import group_store
    monkeypatch.setattr(access, "current_group", lambda sub: 5)
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": 5}] if sub == "s" else [])
    assert grants_chain.grantee_scopes("s", 7) == [("user", "s"), ("group", "5"),
                                                    ("org", "7")]
    assert grants_chain.grantee_scopes("x", 7) == [("user", "x"), ("org", "7")]
    assert grants_chain.grantee_scopes("s", None) == [("user", "s")]


# ── 3. Le périmètre : qui est basculé, qui ne l'est pas ────────────────────────

# Les connecteurs à `platform_key_open` — fait de PRODUIT, inchangé par la vague 2 :
# basculer un connecteur sur la chaîne n'éteint pas son free-tier (celle décision-là
# se prend connecteur par connecteur, mesure de rayon en main — fullenrich seul l'a eue).
FREE_TIER_OTHERS = ("serper", "hunter", "reddit", "sirene", "kaspr",
                    "unipile", "apollo", "serpapi", "searchapi",
                    "tavily")  # 26/08 : socle recherche web, ouvert sans quota (GO du partenaire)

# Vague 2 (23/08) : chaînés SANS toucher leur flag — arêtes semées au boot, révocation
# vraie, metering d'arête ; un appelant sans arête retombe sur le chemin ouvert.
WAVE2 = ("serper", "hunter", "apollo", "serpapi", "kaspr", "reddit")

# Jamais chaînés, et pourquoi : unipile (mode plateforme gouverné par option comp +
# comptes opérés, pas par share_down), searchapi/sirene (pas de clé plateforme posée).
NEVER_CHAINED = ("unipile", "searchapi", "sirene", "tavily")


def test_unchained_connectors_never_touch_the_chain(monkeypatch):
    """Test DIFFÉRENTIEL : toute lecture de chaîne explose. Les connecteurs hors
    périmètre résolvent quand même — donc aucun d'eux ne passe par une ligne de ce
    lot."""
    def boom(*a, **k):  # pragma: no cover - doit ne jamais être appelé
        raise AssertionError("un connecteur non basculé a consulté la chaîne")

    monkeypatch.setattr(db_grants, "edges_for", boom)
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"label": "env", "share_mode": "open",
                                    "share_down": [], "meta": {"rate_limit": 42}}])
    for provider in NEVER_CHAINED:
        assert access._platform_grant_meta("s", provider, None) == {
            "label": "env", "daily_quota": 42}, provider


def test_wave2_open_key_without_edges_resolves_like_legacy(monkeypatch):
    """Le rayon de la vague 2 est NUL pour qui n'a pas d'arête : un connecteur
    chaîné mais à clé OUVERTE rend, pour un appelant que la chaîne ne connaît pas,
    exactement ce que rendait l'ancien chemin (état MUET → repli identique)."""
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"label": "env", "share_mode": "open",
                                    "share_down": [], "meta": {"rate_limit": 42}}])
    for provider in WAVE2:
        assert access._platform_grant_meta("s", provider, None) == \
            access._legacy_platform_grant_meta("s", provider, None), provider
        assert access._platform_grant_meta("s", provider, None) == {
            "label": "env", "daily_quota": 42}, provider


def test_wave2_revocation_cuts_access_even_on_open_key(monkeypatch):
    """Ce que la vague 2 CHANGE : une arête révoquée coupe l'accès plateforme d'un
    connecteur chaîné, même à clé ouverte — sans elle, révoquer un grant serait un
    no-op (le free-tier re-accorderait aussitôt)."""
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{"label": "env", "share_mode": "open",
                                    "share_down": [], "meta": {"rate_limit": 42}}])
    for provider in WAVE2:
        ref = f"platform:{provider}:env"
        monkeypatch.setattr(db_grants, "edges_for", lambda r, g, _ref=ref: [
            {"id": 1, "resource_kind": "connector_instance", "resource_id": _ref,
             "grantor_kind": "platform", "grantor_id": "platform",
             "grantee_kind": "user", "grantee_id": "s",
             "constraints": {}, "parent_id": None, "source": "manual",
             "created_by": "t", "created_at": "2026-08-23 00:00:00",
             "revoked_at": "2026-08-23 01:00:00"}])
        assert access._platform_grant_meta("s", provider, None) is None, provider


def test_only_fullenrich_lost_its_free_tier_flag():
    """`platform_key_open` s'éteint pour fullenrich seulement — vague 2 comprise :
    chaîner un connecteur n'éteint PAS son free-tier. Le flag survivant des neuf
    autres est un fait de produit — l'éteindre en passant serait couper un free-tier
    sans le décider (le rayon de fullenrich était mesuré nul, pas les leurs)."""
    assert providers.REGISTRY["fullenrich"].platform_key_open is False
    still_open = {n for n, c in providers.REGISTRY.items() if c.platform_key_open}
    assert still_open == set(FREE_TIER_OTHERS), sorted(still_open)


def test_the_lot_covers_pilot_plus_wave2():
    """« Un connecteur à quota d'abord, les 9 autres ensuite » : vague 2 du 23/08
    (GO Alexis). Élargir encore reste une décision, pas un ajout de nom."""
    assert grants_chain.CHAIN_CONNECTORS == frozenset({"fullenrich", *WAVE2})


# ── 4. L'écriture : accorder n'enlève plus rien ────────────────────────────────

def test_granting_never_touches_the_vault_row(monkeypatch):
    """L'incident du 31/07 : poser un grant individuel avait basculé la clé partagée
    `open`→`closed` avec une allowlist d'une personne, la fermant POUR TOUS. Ici,
    l'accès au coffre est rendu explosif sur le chemin d'écriture : accorder passe
    quand même. Il n'y a plus de geste qui, en accordant à l'un, retire à l'autre."""
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("le chemin de grant a touché la ligne du coffre")

    posed = []
    monkeypatch.setattr(credentials_store, "_connect", boom)
    monkeypatch.setattr(grants_chain, "grant",
                        lambda *a, **k: posed.append((a, k)))
    credentials_store.platform_grant("fullenrich", "user:x", daily_quota=200)
    assert posed == [(("fullenrich", "user:x"), {"daily_quota": 200, "label": None})]


def test_granting_a_non_migrated_connector_still_uses_the_old_write_path(monkeypatch):
    """Le pendant du test précédent : pour un connecteur HORS chaîne, l'écriture
    reste exactement celle d'avant (elle passe bien par le coffre)."""
    touched = []
    monkeypatch.setattr(credentials_store, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("coffre touché")))
    monkeypatch.setattr(grants_chain, "grant",
                        lambda *a, **k: touched.append(a))
    with pytest.raises(RuntimeError, match="coffre touché"):
        credentials_store.platform_grant("unipile", "user:x", daily_quota=200)
    assert touched == []


def test_revoking_archives_the_edge_for_a_migrated_connector(monkeypatch):
    revoked = []
    monkeypatch.setattr(credentials_store, "_connect",
                        lambda: (_ for _ in ()).throw(AssertionError("coffre touché")))
    monkeypatch.setattr(grants_chain, "revoke", lambda *a, **k: revoked.append(a))
    credentials_store.platform_revoke("fullenrich", "user:x")
    assert revoked == [("fullenrich", "user:x")]


def test_regrant_archives_the_previous_edge(monkeypatch):
    """Un grant re-posé REMPLACE le précédent (0053-D6, le remplacé est archivé).
    Sans ça, deux arêtes vivantes coexisteraient et la plus favorable gagnerait —
    donc BAISSER un quota n'aurait aucun effet."""
    calls = []
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [dict(PROD_INSTANCE)])
    monkeypatch.setattr(db_grants, "revoke_edges",
                        lambda *a: calls.append(("revoke",) + a) or 1)
    monkeypatch.setattr(db_grants, "insert_grant",
                        lambda **k: calls.append(("insert", k["constraints"])) or 9)
    grants_chain.grant("fullenrich", "user:x", daily_quota=50)
    assert calls == [("revoke", REF, "user", "x"), ("insert", {"quota": 50})]



def test_revoking_without_edge_never_opens_a_live_edge(monkeypatch):
    """F-046 pose une arête pour qu'une révocation sans arête ait un avis. Posée VIVANTE
    puis archivée, elle accordait la clé SANS quota entre les deux écritures — à un
    appelant jamais accordé, sur une clé fermée — et pour toujours si l'archivage
    échouait. Un lecteur concurrent, juste après la pose, doit déjà lire « révoqué »."""
    edges: list[dict] = []

    def insert_grant(**k):
        edges.append({**_edge(k["grantee_kind"], k["grantee_id"], quota=None,
                              edge_id=len(edges) + 1),
                      "revoked_at": "now" if k.get("revoked") else None})
        seen.append(grants_chain.platform_rung("x", "fullenrich", None))
        return len(edges)

    def revoke_edges(ref, kind, ident, conn=None):
        live = [e for e in edges if (e["grantee_kind"], e["grantee_id"]) == (kind, ident)
                and e["revoked_at"] is None]
        for e in live:
            e["revoked_at"] = "now"
        return len(live)

    seen: list = []
    monkeypatch.setattr(grants_chain, "_connect", contextlib.nullcontext)
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda p: [{**PROD_INSTANCE, "share_mode": "closed"}])
    monkeypatch.setattr(db_grants, "edges_for", lambda ref, scopes: [
        e for e in edges if (e["grantee_kind"], e["grantee_id"]) in scopes])
    monkeypatch.setattr(db_grants, "edge_exists", lambda ref, kind, ident, conn=None: any(
        (e["grantee_kind"], e["grantee_id"]) == (kind, ident) for e in edges))
    monkeypatch.setattr(db_grants, "insert_grant", insert_grant)
    monkeypatch.setattr(db_grants, "revoke_edges", revoke_edges)
    grants_chain.revoke("fullenrich", "user:x")
    assert [v.granted for v in seen] == [False]
    assert grants_chain.platform_rung("x", "fullenrich", None).granted is False


# ── 5. La désignation de l'instance ────────────────────────────────────────────

def test_resource_id_is_the_canonical_instance_ref():
    """`grants.resource_id` n'invente pas une désignation : c'est le ref canonique
    d'`instance_refs` (domicile unique du format, ADR 0038 §B). Deux gains : un label
    contenant `:` (`zoho` porte `editor:eu` en prod) est percent-encodé au lieu de
    rendre le découpage ambigu, et le jour de L6 le ref `inst:{id}` sera accepté par
    le même parseur — pas de migration de forme."""
    from oto_mcp import instance_refs

    ref = grants_chain.instance_ref("editor:eu", "zoho")
    assert ref == instance_refs.make_platform_ref("zoho", "editor:eu")
    assert grants_chain.parse_instance_ref(ref) == ("editor:eu", "zoho")
    # Un ref d'un AUTRE niveau, ou malformé, n'est jamais pris pour une clé plateforme.
    assert grants_chain.parse_instance_ref("org:2:salesforce") is None
    assert grants_chain.parse_instance_ref("platform") is None


def test_quota_zero_means_unlimited_like_before():
    """Convention conservée de l'ancien chemin : 0 (ou absent) = illimité."""
    assert grants_chain.quota_of(_edge(quota=None)) is None
    assert grants_chain.quota_of({"constraints": {"quota": 0}}) == 0
    assert grants_chain.quota_of({"constraints": {}}) is None


# ── 6. L'empreinte de résolution : SOUS QUELLE CLÉ l'appel est passé ───────────

def _resolved_ref(monkeypatch, rung):
    """Fait résoudre un credential et rend ce que le relevé de l'appel a retenu."""
    from oto_mcp import session_org

    monkeypatch.setattr(access, "_resolve_credential_impl", lambda *a, **k: rung)
    holder: dict = {}
    token = session_org.set_call_trace(holder)
    try:
        access.resolve_credential("fullenrich", sub="s")
    finally:
        session_org.reset_call_trace(token)
    return holder.get("instance")


def test_the_trace_says_which_key_actually_served(monkeypatch):
    """Le journal disait quel outil et quelle org ; jamais SOUS QUELLE CLÉ. C'est
    pourtant la question d'une bascule d'accès — « l'appel est-il passé par l'arête
    ou par l'ancien chemin ? » — et celle de tout incident de credential."""
    from oto_mcp import instance_refs

    platform = access.ResolvedCredential(
        "fullenrich", "SECRET", True, "platform", "platform", "env")
    assert _resolved_ref(monkeypatch, platform) == "platform:fullenrich:env"

    member = access.ResolvedCredential(
        "fullenrich", "SECRET", False, "user", "member", "42:sub-x", account="a@b.c")
    assert _resolved_ref(monkeypatch, member) == instance_refs.make_member_ref(
        42, "sub-x", "fullenrich", "a@b.c")


def test_a_row_without_a_projectable_ref_still_resolves(monkeypatch):
    """Une ligne dont l'id ne se projette pas en ref résout quand même : le relevé
    n'y pose simplement pas d'`instance`."""
    weird = access.ResolvedCredential("fullenrich", "SECRET", False, "user",
                                      "member", "pas-un-id")
    monkeypatch.setattr(access, "_resolve_credential_impl", lambda *a, **k: weird)
    assert access.resolve_credential("fullenrich", sub="s") is weird


def test_a_failing_trace_fails_the_resolution(monkeypatch):
    """Le relevé n'est PLUS best-effort : il décide (`key_mode` facture,
    `credential_row` désigne qui paie un run Apify). Avalé, son échec rendait ces
    verdicts aléatoires ; il lève."""
    import pytest
    monkeypatch.setattr(access.session_org, "note_call_trace",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("journal HS")))
    rc = access.ResolvedCredential("fullenrich", "SECRET", False, "org", "org", "7")
    monkeypatch.setattr(access, "_resolve_credential_impl", lambda *a, **k: rc)
    with pytest.raises(RuntimeError, match="journal HS"):
        access.resolve_credential("fullenrich", sub="s")


def test_instance_is_in_the_journal_allowlist():
    """Le relevé n'atteint la ligne de journal que si l'allowlist le laisse passer —
    une valeur consignée hors allowlist serait un travail invisible."""
    from oto_mcp import server

    assert "instance" in server._TRACED_ARGS


# ── 7. Le chemin chaud : un bulk ne se compte pas en N requêtes ────────────────

def test_a_bulk_debits_both_counters_once(monkeypatch):
    """`fullenrich` facture au contact et métrait donc en boucle : jusqu'à 100 incréments
    par job. Les DEUX compteurs débitent maintenant en une seule écriture, du montant
    exact — sinon métrer un recensement Maps en crédits Serper (81 par défaut, 2 000 sur
    une grille dense) prendrait autant de connexions du pool et de transactions, pour un
    seul appel d'outil, sur le chemin chaud d'un serveur mono-loop.

    Ce banc gardait auparavant la cadence inverse (`len(legacy) == 100`), au motif que la
    signature de l'historique « n'accepte pas de pas ». Elle en accepte un depuis que le
    métrage se compte en unités consommées : ce n'était pas une propriété à tenir, c'était
    un manque figé."""
    legacy, edge = [], []
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.db, "increment_usage",
                        lambda sub, p, amount=1: legacy.append((p, amount)))
    monkeypatch.setattr(grants_chain, "record_usage",
                        lambda sub, p, org, calls: edge.append(calls))
    access.debiter_plateforme("s", "fullenrich", 100)
    assert legacy == [("fullenrich", 100)], (
        "l'historique est débité UNE fois, du montant exact — pas 100 fois de 1")
    assert edge == [100], "l'arête est débitée une fois, du bon montant"


def test_a_non_migrated_connector_never_reaches_the_chain_counter(monkeypatch):
    monkeypatch.setattr(access.db, "increment_usage", lambda sub, p, amount=1: None)
    monkeypatch.setattr(grants_chain, "record_usage",
                        lambda *a, **k: pytest.fail("connecteur non basculé compté"))
    access.debiter_plateforme("s", "unipile", 3)
