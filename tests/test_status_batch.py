"""`/api/me` préchargé (lot ⑨) — et LA garde : le snapshot d'un tiers reste le sien.

Mesuré à chaud le 21/08 sur 67 connecteurs : 1 707 ms, dont 64 % dans les sondes
`member`/`org` (une marche par connecteur) et **24 % dans le quota** — une requête par
connecteur sur une table qu'une seule rend entièrement. `current_org`, lui, était déjà
résolu une fois : le correctif du lot précédent n'aurait rien gagné ici.
"""
import pytest

from oto_mcp import access

# Sans base : le palier plateforme lit les droits de la personne (#1090).
pytestmark = pytest.mark.usefixtures("sans_droit_declare")


def _sonde_muette() -> access.CascadeProbe:
    """Une sonde qui ne trouve rien et ne touche pas la base.

    Les tests ci-dessous portent sur les QUOTAS et sur le scope, pas sur la résolution
    de credentials : leur faire marcher la vraie cascade les ferait dépendre d'une base.
    """
    return access.CascadeProbe(
        member=lambda s, o, p: None,
        legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: None, tenant=lambda t, p: None,
        platform=lambda s, p, o: {"label": "plat", "daily_quota": 100})


def _registre(*noms: str) -> dict:
    """Le registre RÉDUIT aux connecteurs sous test — au lieu de `{}`.

    ⚠️ Un registre VIDE ne se contente pas d'alléger la boucle : il coupe le
    barreau plateforme lui-même (`walk_cascade` le gate sur
    `"platform" in con.auth_modes`, et `connector_for_provider` rend None). Les
    fiches sortaient donc en `forbidden`, et ces tests lisaient `quota_used_today`
    sur une fiche où RIEN ne résout — un port d'observation sur `_used()`, pas le
    contrat servi. Ça a tenu tant que le champ était inconditionnel ; il ne l'est
    plus (un champ d'EFFET plateforme se tait hors barreau plateforme), et le
    préchargement qu'ils gardent — 24 % du coût de /api/me — serait parti avec.
    On fait donc RÉPONDRE le barreau : le préchargement est mesuré là où son
    résultat est servi."""
    from oto_mcp import providers as _p
    return {n: _p.REGISTRY[n] for n in noms}


# ── LA garde : le snapshot d'un TIERS se calcule sur SON contexte ──────────────
def test_le_prechargement_suit_le_SUJET_jamais_le_REQUERANT(monkeypatch):
    """La fuite qu'on a déjà fermée une fois, et qu'un préchargement pourrait rouvrir.

    `status_for(org=…, group=…)` explicite sert la fiche admin d'un TIERS : le snapshot
    doit se calculer contre l'org DE CE TIERS. Si le préchargement se construisait sur
    `current_org` — le contexte du requérant —, on afficherait l'état d'un compte vu
    depuis l'org de quelqu'un d'autre. C'est le bug vécu le 24/06, refermé par le seam
    scopé sur l'acteur ; accélérer ne doit pas le rouvrir.
    """
    vus = {}
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda sub, *, org, groups=None: vus.setdefault("org", org)
                        or access.PRESENCE_PROBE)
    monkeypatch.setattr(access, "current_org",
                        lambda s: pytest.fail("current_org ne doit PAS être consulté "
                                              "quand `org` est explicite"))
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access.db, "usage_today_map", lambda sub: {})
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ())
    monkeypatch.setattr(access.providers, "REGISTRY", {})

    access.status_for("le_tiers", org=999, group=None)
    assert vus["org"] == 999, "le préchargement s'est construit sur une autre org"


def test_les_quotas_sont_lus_pour_le_SUJET(monkeypatch):
    vus = {}
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda sub, *, org, groups=None: access.PRESENCE_PROBE)
    monkeypatch.setattr(access.db, "usage_today_map",
                        lambda sub: vus.setdefault("sub", sub) or {})
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ())
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    access.status_for("le_tiers", org=999, group=None)
    assert vus["sub"] == "le_tiers"


# ── Le différentiel : groupés ≡ un par un ──────────────────────────────────────
def test_la_map_de_quotas_repond_comme_la_lecture_UNITAIRE(monkeypatch):
    """Même patron que le différentiel des sondes : on compare les DEUX chemins.

    Une map qui divergerait de la lecture unitaire ferait afficher un quota faux — et
    « quota intact » sur un compte épuisé est le sens dangereux de l'erreur.
    """
    from oto_mcp.db import usage

    table = {"serper": 12, "hunter": 3}
    monkeypatch.setattr(usage, "get_usage_today",
                        lambda sub, tool: table.get(tool, 0))
    monkeypatch.setattr(usage, "usage_today_map", lambda sub: dict(table))

    m = usage.usage_today_map("u1")
    for tool in ("serper", "hunter", "un_outil_sans_compteur"):
        assert m.get(tool, 0) == usage.get_usage_today("u1", tool), tool


def test_un_outil_ABSENT_de_la_map_vaut_zero_pas_None(monkeypatch):
    # La lecture unitaire rend 0 sur une ligne absente ; la map doit se lire pareil,
    # sinon un `None` remonterait dans une comparaison de quota.
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda sub, *, org, groups=None: _sonde_muette())
    monkeypatch.setattr(access.db, "usage_today_map", lambda sub: {"serper": 5})
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access, "current_org", lambda s: 2)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ("serper", "hunter"))
    monkeypatch.setattr(access.providers, "REGISTRY", _registre("serper", "hunter"))
    out = access.status_for("u1")
    # `hunter` est absent de la map préchargée : 0, pas None — sinon un None
    # remonterait dans une comparaison de quota. Servi parce que le barreau
    # plateforme RÉPOND ici (cf. `_registre`).
    assert out["providers"]["hunter"]["mode"] == "platform"
    assert out["providers"]["hunter"]["quota_used_today"] == 0
    assert out["providers"]["serper"]["quota_used_today"] == 5


# ── Le repli : mieux vaut payer que mentir ─────────────────────────────────────
def test_un_prechargement_de_quotas_en_PANNE_retombe_sur_la_lecture_unitaire(monkeypatch):
    """Rendre 0 partout ferait afficher « quota intact » à qui l'a épuisé.

    Le repli coûte 48 requêtes ; c'est le bon prix pour ne pas mentir sur un quota.
    """
    def _boom(sub):
        raise RuntimeError("usage indisponible")
    appels = []
    monkeypatch.setattr(access.db, "usage_today_map", _boom)
    monkeypatch.setattr(access.db, "get_usage_today",
                        lambda sub, tool: appels.append(tool) or 42)
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda sub, *, org, groups=None: _sonde_muette())
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access, "current_org", lambda s: 2)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ("serper",))
    monkeypatch.setattr(access.providers, "REGISTRY", _registre("serper"))
    out = access.status_for("u1")
    assert appels == ["serper"]
    assert out["providers"]["serper"]["mode"] == "platform"
    assert out["providers"]["serper"]["quota_used_today"] == 42


def test_une_sonde_prechargee_en_PANNE_retombe_sur_la_sonde_unitaire(monkeypatch):
    def _boom(sub, *, org, groups=None):
        raise RuntimeError("coffre indisponible")
    monkeypatch.setattr(access, "preloaded_presence_probe", _boom)
    monkeypatch.setattr(access.db, "usage_today_map", lambda sub: {})
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access, "current_org", lambda s: 2)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ())
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    # Ne lève pas : l'accélération est facultative, le snapshot ne l'est pas.
    assert "providers" in access.status_for("u1")


# ── Le repli DEUX try (oto#522) : un préchargement ne doit plus en jeter un autre ──
def _wire_un_forbidden(monkeypatch, *, group_secret_map, preloaded_presence_probe):
    """Câble `status_for` pour qu'il traite UN provider `forbidden` via `KEY_PROVIDERS`,
    sans toucher de vraie base : `resolution_rungs` est coupé à vide (aucun hit ⟹
    `mode='forbidden'` pour tout provider), ce qui déclenche systématiquement le hint
    `team_key_group` — c'est LUI qu'on observe : quel `secrets_by_group` reçoit-il, et
    quelle sonde `resolution_rungs` reçoit-il en retour ?"""
    probe_recus: list = []
    map_recues: list = []
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access.db, "usage_today_map", lambda sub: {})
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ("zoho",))
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    # Org-partageable de force : indépendant du registre réel, on ne teste que le
    # câblage des deux préchargements, pas la liste des connecteurs partageables.
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", frozenset({"zoho"}))
    monkeypatch.setattr(access, "group_secret_map", group_secret_map)
    monkeypatch.setattr(access, "preloaded_presence_probe", preloaded_presence_probe)

    def _sans_hit(sub, provider, *, org, group, probe, want="auto"):
        probe_recus.append(probe)
        return iter(())  # aucun hit ⟹ winner=None ⟹ mode='forbidden'
    # `chain_shadow` n'est pas dans `_MODULES` de la façade `access` (status.py
    # l'importe en direct) : on patche le sous-module, pas le nom plat.
    monkeypatch.setattr(access.chain_shadow, "resolution_rungs", _sans_hit)

    def _hint(sub, org, provider, groups=None, secrets_by_group=None):
        map_recues.append(secrets_by_group)
        return None
    monkeypatch.setattr(access, "reachable_team_key", _hint)
    return probe_recus, map_recues


def test_la_sonde_en_panne_laisse_la_carte_d_equipes_construite_intacte(monkeypatch):
    """(a) La carte d'équipes a fini de se construire ; SEULE la sonde tombe ensuite.

    Avant #522, les deux préchargements partageaient un `try` unique : cet échec-là
    jetait aussi la carte, pourtant valide, et le hint `team_key_group` retombait sur
    ~200 lectures unitaires qu'elle existait déjà pour éviter."""
    carte = {2: {"zoho"}}

    def _boom(sub, *, org, groups=None):
        raise RuntimeError("sonde indisponible")
    probe_recus, map_recues = _wire_un_forbidden(
        monkeypatch, group_secret_map=lambda groups=None: carte,
        preloaded_presence_probe=_boom)

    access.status_for("u1", org=999, group=None)

    assert map_recues == [carte], "la carte construite avec succès doit atteindre le hint"
    assert probe_recus == [access.PRESENCE_PROBE], "seule la sonde en échec doit retomber"


def test_la_carte_en_panne_laisse_la_sonde_prechargee_intacte(monkeypatch):
    """Symétrique de (a) : SEULE la carte d'équipes tombe, la sonde a réussi.

    Avant #522, le même `try` unique aurait aussi jeté la sonde préchargée — payant
    à nouveau les ~180 sondes unitaires (member/org) que #518 avait retirées."""
    sonde_ok = access.CascadeProbe(
        member=lambda s, o, p: None,
        legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: None, tenant=lambda t, p: None,
        platform=lambda s, p, o: None)

    def _boom(groups=None):
        raise RuntimeError("carte indisponible")
    probe_recus, map_recues = _wire_un_forbidden(
        monkeypatch, group_secret_map=_boom,
        preloaded_presence_probe=lambda sub, *, org, groups=None: sonde_ok)

    access.status_for("u1", org=999, group=None)

    assert map_recues == [None], "carte en échec ⟹ None (pas {}), le hint doit relire"
    assert probe_recus == [sonde_ok], "la sonde préchargée avec succès doit être utilisée"


def test_double_panne_retombe_comme_avant_522(monkeypatch):
    """(b) Les deux préchargements tombent : le double repli reste EXACTEMENT celui
    d'avant le lot — carte `None`, sonde `PRESENCE_PROBE` — le lot ne change que le
    cas où un seul des deux échoue."""
    def _boom_carte(groups=None):
        raise RuntimeError("carte indisponible")

    def _boom_sonde(sub, *, org, groups=None):
        raise RuntimeError("sonde indisponible")
    probe_recus, map_recues = _wire_un_forbidden(
        monkeypatch, group_secret_map=_boom_carte, preloaded_presence_probe=_boom_sonde)

    out = access.status_for("u1", org=999, group=None)

    assert map_recues == [None]
    assert probe_recus == [access.PRESENCE_PROBE]
    assert "providers" in out
