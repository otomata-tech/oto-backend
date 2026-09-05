"""La sonde PRÉCHARGÉE doit répondre comme `PRESENCE_PROBE`, connecteur par connecteur.

C'est le seul endroit où une divergence peut naître : le walker n'est pas touché, donc
son contrat ne bouge pas — mais deux sondes qui ne s'accordent pas produiraient deux
vérités sur « quelle clé résout », et l'une des deux ferait mentir une surface.

Le test est un DIFFÉRENTIEL, pas une assertion sur des valeurs choisies : on compare les
deux sondes sur TOUT le registre, dans le même contexte. Un barreau ajouté demain à la
cascade cassera ce test si la préchargée ne sait pas y répondre — au lieu de diverger en
silence, ce qui est le mode d'échec qu'on ferme.
"""
import pytest

from oto_mcp import access


@pytest.fixture
def coffre(monkeypatch):
    """Un coffre en mémoire, lu par les DEUX sondes — donc comparables.

    Les sondes unitaires passent par `db`/`group_store`/`org_store`, la préchargée par
    `credentials_store.list_credentials` : on bouchonne les deux faces du MÊME état,
    sinon le différentiel comparerait deux mondes au lieu de deux lectures.
    """
    etat = {
        "membre": {"folk": {"suspended": False}, "pennylane": {"suspended": True}},
        "groupe": {9: {"serper"}},
        "org": {"hunter", "folk"},
        "tenant": {"serper", "folk"},
        # Scope legacy (#876) : atlassian EST dans `LEGACY_USER_SCOPE_PROVIDERS`,
        # folkmcp n'a rien posé — les deux cas du barreau, sur le registre réel.
        "legacy": {"atlassian"},
    }

    from oto_mcp import credentials_store as cs

    def _list_credentials(entity_type, entity_id):
        if entity_type == cs.MEMBER:
            return [{"connector": c, "account": "",
                     "meta": {"suspended": "true"} if v["suspended"] else {}}
                    for c, v in etat["membre"].items()]
        if entity_type == "group":
            return [{"connector": c, "account": "", "meta": {}}
                    for c in etat["groupe"].get(int(entity_id), ())]
        if entity_type == "org":
            return [{"connector": c, "account": "", "meta": {}} for c in etat["org"]]
        if entity_type == cs.TENANT:
            return [{"connector": c, "account": "", "meta": {}} for c in etat["tenant"]]
        return []

    monkeypatch.setattr(cs, "list_credentials", _list_credentials)
    monkeypatch.setattr(access.db, "has_member_api_key",
                        lambda s, o, p, account=None: p in etat["membre"])
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda s, o, p, account="": bool(
                            etat["membre"].get(p, {}).get("suspended")))
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda g, p: p in etat["groupe"].get(int(g), ()))
    monkeypatch.setattr(access.org_store, "has_org_secret",
                        lambda o, p: p in etat["org"])
    monkeypatch.setattr(cs, "has_credential",
                        lambda et, eid, p, account=None: (
                            et == cs.USER and p in etat["legacy"]))
    from oto_mcp import tenancy, tenant_vault
    monkeypatch.setattr(tenant_vault, "has_tenant_secret",
                        lambda t, p: p in etat["tenant"])
    # Un tenant tiers dans le registre : sans lui, le sujet est `oto` et le barreau
    # tenant n'existe pas — le différentiel ne le comparerait jamais.
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "pilote", "issuer": "https://auth.pilote.test/oidc"}])),
        raising=False)
    # Le walker appelle `personal_instance_org` LUI-MÊME (pas via la sonde) pour les
    # connecteurs personal_cross_org : il touche la base, on le neutralise ici. C'est
    # aussi ce qui documente la limite de la sonde préchargée — ce barreau-là n'est pas
    # préchargeable sans toucher au walker, et on refuse d'y toucher.
    monkeypatch.setattr(access, "personal_instance_org",
                        lambda sub, provider, exclude_org=None: None)
    return etat


def _providers() -> list[str]:
    from oto_mcp import providers
    return sorted(providers.REGISTRY)


def test_les_deux_sondes_rendent_le_MEME_verdict_sur_TOUT_le_registre(coffre):
    sonde = access.preloaded_presence_probe("pilote:u1", org=2, groups=[{"group_id": 9}])
    ecarts = []
    for p in _providers():
        for nom, a, b in (
            ("member", access.PRESENCE_PROBE.member("u1", 2, p), sonde.member("u1", 2, p)),
            ("group", access.PRESENCE_PROBE.group(9, p), sonde.group(9, p)),
            ("org", access.PRESENCE_PROBE.org(2, p), sonde.org(2, p)),
            ("tenant", access.PRESENCE_PROBE.tenant("pilote", p), sonde.tenant("pilote", p)),
            ("legacy", access.PRESENCE_PROBE.legacy_user("u1", p), sonde.legacy_user("u1", p)),
        ):
            if bool(a) != bool(b):
                ecarts.append(f"{p}/{nom}: unitaire={a!r} préchargée={b!r}")
    assert not ecarts, "divergence entre les deux sondes :\n  " + "\n  ".join(ecarts)


def test_une_instance_SUSPENDUE_est_sautee_par_les_deux(coffre):
    sonde = access.preloaded_presence_probe("u1", org=2, groups=[])
    # `pennylane` est au coffre mais suspendue : la cascade doit la sauter des DEUX
    # côtés, sinon la préchargée résoudrait une clé que la vraie ignore.
    assert access.PRESENCE_PROBE.member("u1", 2, "pennylane") is None
    assert sonde.member("u1", 2, "pennylane") is None
    assert sonde.member("u1", 2, "folk") is not None


def test_le_walker_recoit_la_sonde_prechargee_SANS_etre_modifie(coffre):
    # La preuve que c'est une sonde, pas un chemin : on marche la MÊME cascade avec
    # l'une puis l'autre, et le barreau gagnant est le même.
    sonde = access.preloaded_presence_probe("u1", org=2, groups=[{"group_id": 9}])
    for p in _providers():
        a = access.cascade_winner("u1", p, org=2, group=9,
                                  probe=access.PRESENCE_PROBE, want="byo")
        b = access.cascade_winner("u1", p, org=2, group=9, probe=sonde, want="byo")
        assert (a.mode if a else None) == (b.mode if b else None), p


def test_l_inventaire_est_lu_UNE_fois_par_entite(coffre, monkeypatch):
    from oto_mcp import credentials_store as cs
    appels = []
    vrai = cs.list_credentials
    monkeypatch.setattr(cs, "list_credentials",
                        lambda t, i: appels.append((t, i)) or vrai(t, i))
    access.preloaded_presence_probe("u1", org=2, groups=[{"group_id": 9}])
    # membre + une équipe + org = 3 lectures, quel que soit le nombre de connecteurs.
    assert len(appels) == 3
    assert len(_providers()) > 50, "le registre doit être gros pour que ça prouve quelque chose"


def test_sans_org_la_sonde_ne_pretend_rien(coffre):
    sonde = access.preloaded_presence_probe("u1", org=None, groups=[])
    for p in _providers():
        assert sonde.member("u1", None, p) is None
        assert sonde.org(None, p) is None


def test_le_quota_reste_traduit_par_le_SEAM_pas_par_l_appelant(coffre, monkeypatch):
    """`credential_mode_for` rend `over_quota`, pas `platform`, quand le grant est épuisé.

    C'est le contrôle qu'un appelant pressé aurait recopié en court-circuitant la
    fonction : passer la sonde EN PARAMÈTRE le garde, et c'est pour ça que le paramètre
    existe plutôt qu'un accès direct au walker.
    """
    monkeypatch.setattr(access, "current_org", lambda s: 2)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access.db, "get_usage_today", lambda s, p: 999)
    sonde = access.preloaded_presence_probe("u1", org=2, groups=[])
    monkeypatch.setattr(access, "cascade_winner",
                        lambda *a, **k: access.CascadeRung(
                            "platform", "platform", "lbl", {"daily_quota": 10}))
    assert access.credential_mode_for("u1", "serper", org=2, group=None,
                                      probe=sonde) == "over_quota"
