"""« Aucune clé ne résout » ≠ « l'accès t'est refusé » — le snapshot dit les deux
séparément (oto-dashboard#126).

Le snapshot ne portait que `mode`, et `forbidden` y veut dire « aucune clé ne
résout » — l'état par défaut de tout connecteur BYO pas encore connecté. L'écran en
tirait « Réservé à certaines équipes — demande à un admin », c'est-à-dire un mur
affiché à quelqu'un que rien ne bloque. Vécu jusqu'à un org_admin devant le
connecteur de SA propre org, qu'il gouverne. Le faux diagnostic avait été repéré le
2026-07-16 et est resté sans signal pour le corriger.
"""
import pytest

from oto_mcp import access


@pytest.fixture()
def snapshot(monkeypatch):
    """`status_for` sans base : on ne teste ici que le drapeau de restriction."""
    # Sonde MUETTE : ce test porte sur le drapeau de restriction, pas sur la
    # résolution — faire marcher la vraie cascade le ferait dépendre d'une base.
    muette = access.CascadeProbe(
        member=lambda s, o, p: None, member_cross=lambda s, o, p: None,
        legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: None, tenant=lambda t, p: None,
        platform=lambda s, p, o: None)
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda sub, *, org, groups=None: muette)
    monkeypatch.setattr(access, "current_org", lambda s: 35)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access.db, "usage_today_map", lambda sub: {})
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ("pennylane", "serper"))
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    monkeypatch.setattr(access.credentials_store, "list_credentials", lambda *a, **k: [])
    return monkeypatch


def _providers(monkeypatch, *, org_denied=(), group_denied=()):
    monkeypatch.setattr(access.rbac, "rbac_denied_connectors",
                        lambda sub, org: set(org_denied))
    monkeypatch.setattr(access.rbac, "group_rbac_denied_connectors",
                        lambda sub, group: set(group_denied))
    return access.status_for("u-1")["providers"]


def test_un_connecteur_sans_cle_nest_pas_reserve(snapshot):
    """Le cas qui produisait le mur : rien de posé, mais rien de refusé non plus."""
    p = _providers(snapshot)
    assert p["pennylane"]["mode"] == "forbidden"
    assert p["pennylane"]["rbac_restricted"] is False


def test_une_restriction_dorg_se_dit(snapshot):
    p = _providers(snapshot, org_denied=("pennylane",))
    assert p["pennylane"]["rbac_restricted"] is True
    assert p["serper"]["rbac_restricted"] is False


def test_une_restriction_dequipe_se_dit_aussi(snapshot):
    """L'équipe RESTREINT davantage que l'org — le verdict est un OU des deux."""
    p = _providers(snapshot, group_denied=("serper",))
    assert p["serper"]["rbac_restricted"] is True


def test_chaque_connecteur_porte_le_drapeau(snapshot):
    """Sans quoi l'écran devrait deviner sur les entrées qui ne l'ont pas, et
    retomberait sur le raisonnement qu'on vient de retirer."""
    p = _providers(snapshot)
    assert all("rbac_restricted" in e for e in p.values())


def test_un_hoquet_de_base_ninvente_jamais_une_restriction(snapshot):
    """Fail-open assumé : mieux vaut ne rien annoncer que refuser à tort. Un mur
    affiché par erreur arrête l'utilisateur ; une restriction tue, elle, est de
    toute façon appliquée au call-time par le même seam."""
    def _boom(*a, **k):
        raise RuntimeError("DB indisponible")
    snapshot.setattr(access.rbac, "rbac_denied_connectors", _boom)
    snapshot.setattr(access.rbac, "group_rbac_denied_connectors", _boom)
    p = access.status_for("u-1")["providers"]
    assert p["pennylane"]["rbac_restricted"] is False
