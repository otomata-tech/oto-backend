"""oto-backend — lot `status_for` N+1 sur le barreau PLATEFORME (17/09/2026).

Mesuré par oto cd (prod, lecture seule) : `credentials_store.list_platform_instances`
était lue 31 fois par appel de `status_for` — une par connecteur qui atteint le
barreau plateforme (le cas `forbidden`, majorité d'un compte réel), doublée pour les
connecteurs basculés sur la chaîne de grants (`grants_chain.platform_rung` relit la
même table que `_legacy_platform_grant_meta`, lot 0053-L5). Correctif : une lecture
groupée par `status_for`, `credentials_store.list_all_platform_instances()`, quel
que soit le nombre de connecteurs interrogés ensuite — et non plus une durée, un
COMPTE de requêtes.

⚠️ Le calcul ne bouge pas : la chaîne de grants et le repli legacy restent deux
verdicts séparés — seule la LECTURE `list_platform_instances` leur est mutualisée.
(La comparaison des deux, `journal_resolution`, a été retirée avec R6 : là où la
chaîne a un avis, l'ancien chemin n'est plus lu.)
"""
from __future__ import annotations

from oto_mcp import access, credentials_store, db, group_store, grants_chain, org_store, tenant_vault


def _sonde_sans_base(monkeypatch, *, appels_unitaires: list, appels_groupes: list,
                     instances_par_provider: dict):
    """Neutralise toute lecture réelle de coffre/DB que `preloaded_presence_probe`
    ou `_platform_grant_meta` pourraient déclencher, pour isoler UNIQUEMENT le
    comptage des lectures d'instances plateforme."""
    monkeypatch.setattr(credentials_store, "list_platform_instances",
                        lambda provider: appels_unitaires.append(provider) or [])

    def _groupees():
        appels_groupes.append(True)
        return dict(instances_par_provider)
    monkeypatch.setattr(credentials_store, "list_all_platform_instances", _groupees)
    monkeypatch.setattr(credentials_store, "list_credentials", lambda et, eid: [])
    monkeypatch.setattr(access, "group_secret_map", lambda groups=None: {})
    monkeypatch.setattr(db, "has_member_api_key", lambda s, o, p, account=None: False)
    monkeypatch.setattr(group_store, "has_group_secret", lambda g, p: False)
    monkeypatch.setattr(org_store, "has_org_secret", lambda o, p: False)
    monkeypatch.setattr(tenant_vault, "has_tenant_secret", lambda t, p: False)
    monkeypatch.setattr(tenant_vault, "rung_tenant", lambda sub: None)
    # Reste sur le repli legacy — la chaîne de grants n'est pas le sujet de ce banc.
    monkeypatch.setattr(grants_chain, "is_chained", lambda provider: False)


_PROVIDERS_10 = ("serper", "hunter", "apollo", "clearbit", "explorium",
                 "kaspr", "dropcontact", "societeinfo", "pappers", "societe_com")


def test_le_nombre_de_lectures_ne_depend_pas_de_N(monkeypatch):
    """N=3 puis N=10 connecteurs qui atteignent le barreau plateforme : le nombre de
    lectures GROUPÉES reste 1 dans les deux cas — c'est la preuve que ça ne dépend
    pas de N, pas une comparaison de durée."""
    for n in (3, 10):
        appels_unitaires: list = []
        appels_groupes: list = []
        _sonde_sans_base(monkeypatch, appels_unitaires=appels_unitaires,
                         appels_groupes=appels_groupes, instances_par_provider={})
        sonde = access.preloaded_presence_probe("u1", org=2, groups=[])
        for provider in _PROVIDERS_10[:n]:
            sonde.platform("u1", provider, 2)
        assert appels_unitaires == [], (
            f"N={n} : le barreau plateforme relit encore list_platform_instances "
            f"par connecteur : {appels_unitaires!r}")
        assert len(appels_groupes) == 1, (
            f"N={n} : list_all_platform_instances doit être lue UNE fois par sonde, "
            f"pas {len(appels_groupes)}")


def test_avant_ce_lot_le_meme_scenario_aurait_rougi(monkeypatch):
    """Contrôle négatif : SANS le préchargement (le barreau plateforme d'origine,
    `PRESENCE_PROBE.platform`, qui relit `list_platform_instances` directement),
    le compte d'appels CROÎT avec N — la preuve que le banc ci-dessus teste bien
    quelque chose de réel, pas un artefact du mock."""
    appels_unitaires: list = []
    appels_groupes: list = []
    _sonde_sans_base(monkeypatch, appels_unitaires=appels_unitaires,
                     appels_groupes=appels_groupes, instances_par_provider={})
    for provider in _PROVIDERS_10:
        access.PRESENCE_PROBE.platform("u1", provider, 2)
    assert len(appels_unitaires) == len(_PROVIDERS_10), (
        "le chemin NON préchargé doit bien lire une fois par connecteur — sinon ce "
        "banc ne prouve rien sur le chemin préchargé")


def test_le_resultat_reste_identique_avec_ou_sans_prechargement(monkeypatch):
    """Les valeurs rendues par le barreau plateforme ne doivent PAS changer — seule
    la lecture est mutualisée, jamais le verdict."""
    instances = {
        "serper": [{"label": "plat-1", "share_mode": "all", "share_down": [],
                    "meta": {"rate_limit": 500}}],
        "hunter": [],
    }
    for provider in ("serper", "hunter", "jamais_installe"):
        appels_unitaires: list = []
        appels_groupes: list = []
        _sonde_sans_base(monkeypatch, appels_unitaires=appels_unitaires,
                         appels_groupes=appels_groupes,
                         instances_par_provider=instances)
        monkeypatch.setattr(credentials_store, "list_platform_instances",
                            lambda p, _i=instances: list(_i.get(p, [])))

        sans_prechargement = access.PRESENCE_PROBE.platform("u1", provider, None)
        sonde = access.preloaded_presence_probe("u1", org=2, groups=[])
        avec_prechargement = sonde.platform("u1", provider, None)

        assert avec_prechargement == sans_prechargement, (
            f"{provider} : divergence — sans préchargement {sans_prechargement!r}, "
            f"avec préchargement {avec_prechargement!r}")
