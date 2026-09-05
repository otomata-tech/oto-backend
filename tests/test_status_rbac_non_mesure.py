"""Une lecture de droits qui échoue ne dit plus « aucune restriction » tout court.

oto#42, entrée 8 du lot 1 — **règle 1**, la pire des trois : une valeur qu'on n'a pas
pu établir n'est jamais rendue par son défaut, et ici le défaut était le plus permissif
possible. `status_for` lit le RBAC connecteur en deux paliers (org, équipe), chacun sous
son propre `try/except` fail-open ; quand la base hoquette, `denied` reste vide et chaque
entrée sort `rbac_restricted: false`. Un écran — et un agent — lisent alors « rien ne te
restreint » là où **personne n'a pu vérifier**. Les deux phrases sortaient du même
booléen, indistinguables.

**La valeur servie ne bouge pas** : le front la lit, et le fail-open est le bon choix
(un mur affiché à tort arrête quelqu'un que rien ne bloque, alors qu'une restriction
vraie est de toute façon appliquée au call-time par le même seam). Ce qui s'ajoute est
le fait qu'elle n'a pas été MESURÉE.

⚠️ **Ce fichier lit la surface, pas le producteur.** `access.status_for` n'est pas ce
que le client reçoit : `GET /api/me` (capacité `me.get`) l'est. On passe donc par la
vraie chaîne de l'adaptateur REST — un banc qui asserte sur le dict intermédiaire
certifierait une forme que la surface pourrait ne pas servir.

⚠️ **La neutralisation est ASSERTÉE avant d'être lue** : une substitution qui rate rend
du VERT, c'est-à-dire le signal rassurant. Chaque test appelle donc le seam saboté et
exige qu'il lève, avant de regarder la réponse.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import access
from oto_mcp.capabilities import me_account as ma


def _boom(*a, **k):
    raise RuntimeError("DB indisponible")


@pytest.fixture()
def socle(monkeypatch):
    """Le snapshot complet sans base — cascade muette + compte en mémoire.

    Même montage que `test_status_rbac_restricted.py` pour la partie `status_for`
    (sonde MUETTE : ce qui est en jeu ici est le drapeau, pas la résolution), plus
    ce dont `_me` a besoin pour que la réponse parte réellement sur le fil.
    """
    muette = access.CascadeProbe(
        member=lambda s, o, p: None, member_cross=lambda s, o, p: None,
        legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: None, tenant=lambda t, p: None,
        platform=lambda s, p, o: None)
    monkeypatch.setattr(access, "preloaded_presence_probe",
                        lambda sub, *, org, groups=None: muette)
    monkeypatch.setattr(access, "current_org", lambda s: 35)
    monkeypatch.setattr(access, "current_group", lambda s: None)
    monkeypatch.setattr(access, "is_platform_operator", lambda s: False)
    monkeypatch.setattr(access.db, "usage_today_map", lambda sub: {})
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(access.db, "KEY_PROVIDERS", ("pennylane", "serper"))
    monkeypatch.setattr(access.providers, "REGISTRY", {})
    monkeypatch.setattr(access.credentials_store, "list_credentials", lambda *a, **k: [])
    # Ce que `_me` demande en plus du bloc `providers`.
    monkeypatch.setattr(ma.db, "get_user", lambda sub: {"email": "a@b.c", "name": "A"})
    monkeypatch.setattr(ma.org_store, "get_org", lambda oid: {"id": oid, "name": "Otomata"})
    monkeypatch.setattr(ma.org_store, "effective_logo_url", lambda o: None)
    monkeypatch.setattr(ma.org_store, "get_org_role", lambda oid, sub: "member")
    monkeypatch.setattr(ma.org_store, "get_org_mfa", lambda oid: {"require_mfa": False})
    monkeypatch.setattr(ma.org_store, "is_personal_org", lambda oid: False)
    monkeypatch.setattr(ma.org_store, "get_active_org", lambda sub: 35)
    monkeypatch.setattr(ma.group_store, "get_active_group", lambda sub: None)
    monkeypatch.setattr(ma.billing, "is_enabled", lambda: False)
    stub_authz(monkeypatch, org_id=35, role="member")
    return monkeypatch


def _servis(monkeypatch, *, org=None, group=None):
    """Le bloc `providers` TEL QU'IL PART sur le fil, après sabotage des paliers.

    `org`/`group` : un `set` (le palier répond) ou `_boom` (il tombe). La
    neutralisation est vérifiée AVANT la lecture, palier par palier.
    """
    org_seam = org if callable(org) else (lambda sub, o: set(org or ()))
    group_seam = group if callable(group) else (lambda sub, g: set(group or ()))
    monkeypatch.setattr(access.rbac, "rbac_denied_connectors", org_seam)
    monkeypatch.setattr(access.rbac, "group_rbac_denied_connectors", group_seam)
    # ⚠️ Assertion de la neutralisation, avant tout résultat : une substitution qui
    # n'aurait pas pris (nom déplacé, palier appelé par un autre chemin) laisserait
    # le vrai seam répondre `set()` — et le banc virerait au vert sans rien prouver.
    if org is _boom:
        with pytest.raises(RuntimeError):
            access.rbac.rbac_denied_connectors("u-1", 35)
    if group is _boom:
        with pytest.raises(RuntimeError):
            access.rbac.group_rbac_denied_connectors("u-1", None)
    code, corps = call("me.get")
    assert code == 200, corps
    return corps["providers"]


def test_le_palier_dorg_tombe_et_la_fiche_lavoue(socle):
    """Le cas de l'inventaire : la lecture échoue, la réponse ne peut plus être lue
    comme un constat d'ouverture."""
    p = _servis(socle, org=_boom)
    # Le contrat ne bouge pas — c'est la condition posée au correctif.
    assert p["pennylane"]["rbac_restricted"] is False
    # …mais le `false` avoue désormais d'où il vient.
    assert p["pennylane"]["rbac_restricted_measured"] is False
    hint = p["pennylane"]["rbac_restricted_hint"]
    assert "org" in hint                      # QUEL palier n'a pas répondu
    assert "refusé" in hint                   # et que l'accès n'est pas ouvert pour autant


def test_tous_les_connecteurs_portent_laveu_pas_seulement_le_premier(socle):
    """Le drapeau se pose par entrée : un écran qui n'en verrait qu'une retomberait
    sur la lecture qu'on vient de retirer pour les autres."""
    p = _servis(socle, org=_boom)
    assert {n for n, e in p.items() if e.get("rbac_restricted_measured") is False} \
        == set(p)


def test_rien_ne_tombe_rien_ne_se_dit(socle):
    """Sur écart SEULEMENT : un champ toujours présent devient du bruit qu'on cesse
    de lire, et son absence est alors l'information — la règle a bien été LUE."""
    p = _servis(socle, org=("pennylane",))
    assert p["pennylane"]["rbac_restricted"] is True
    assert p["serper"]["rbac_restricted"] is False
    for nom, e in p.items():
        assert "rbac_restricted_measured" not in e, nom
        assert "rbac_restricted_hint" not in e, nom


def test_un_refus_etabli_ne_savoue_pas(socle):
    """Un palier tombe, l'autre a répondu ET refuse : `true` reste ÉTABLI (l'union
    des refus ne peut que croître) — c'est le `false` du voisin qui est une
    non-réponse. Confondre les deux ferait douter d'un mur qui, lui, est réel."""
    p = _servis(socle, org=("pennylane",), group=_boom)
    assert p["pennylane"]["rbac_restricted"] is True
    assert "rbac_restricted_measured" not in p["pennylane"]
    assert p["serper"]["rbac_restricted_measured"] is False
    assert "équipe" in p["serper"]["rbac_restricted_hint"]


def test_les_deux_paliers_tombes_se_nomment_tous_les_deux(socle):
    """Nommer un seul palier quand les deux sont muets enverrait chercher la panne
    du mauvais côté."""
    p = _servis(socle, org=_boom, group=_boom)
    hint = p["serper"]["rbac_restricted_hint"]
    assert "org" in hint and "équipe" in hint
