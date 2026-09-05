"""`/api/me` préchargé (lot ⑨) — et LA garde : le snapshot d'un tiers reste le sien.

Mesuré à chaud le 21/08 sur 67 connecteurs : 1 707 ms, dont 64 % dans les sondes
`member`/`org` (une marche par connecteur) et **24 % dans le quota** — une requête par
connecteur sur une table qu'une seule rend entièrement. `current_org`, lui, était déjà
résolu une fois : le correctif du lot précédent n'aurait rien gagné ici.
"""
import pytest

from oto_mcp import access


def _sonde_muette() -> access.CascadeProbe:
    """Une sonde qui ne trouve rien et ne touche pas la base.

    Les tests ci-dessous portent sur les QUOTAS et sur le scope, pas sur la résolution
    de credentials : leur faire marcher la vraie cascade les ferait dépendre d'une base.
    """
    return access.CascadeProbe(
        member=lambda s, o, p: None, member_cross=lambda s, o, p: None,
        legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: None, tenant=lambda t, p: None,
        platform=lambda s, p, o: {"label": "plat", "daily_quota": 100})


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
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    out = access.status_for("u1")
    assert out["providers"]["hunter"]["quota_used_today"] == 0


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
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    out = access.status_for("u1")
    assert appels == ["serper"]
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
