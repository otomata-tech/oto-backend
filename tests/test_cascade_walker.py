"""Walker de cascade (access.walk_cascade) — le contrat DORÉ de la cascade.

La cascade `perso > équipe active > org > tenant > plateforme` vit à UN seul
endroit (walker paramétré par sonde) ; ce fichier fige l'ordre des barreaux et
les gates (byo_user, ORG_SHAREABLE, éligibilité plateforme, want='byo', org dont
l'appelant est membre). Depuis #1499 (D1), aucun barreau ne lit une autre org. Les sondes sont injectées : aucun stub de DB nécessaire.

Providers réels du registre utilisés comme fixtures de gates :
- serper        : byo_user + byo_org + platform (tous barreaux)
- zoho          : byo_user + byo_org, PAS de plateforme (want='byo' vivant)
- silae         : byo_user seul (non partageable → barreaux groupe/org inertes)
- unipile       : clé personnelle (aucune clé d'une autre org ne sert)
- http          : byo_org seul (pas de barreau membre par construction)
"""
import pytest

from oto_mcp import access


def probe(*, member=False, group=False, org=False, platform=False):
    return access.CascadeProbe(
        member=lambda s, o, p: (("MK", "") if member else None),
        legacy_user=lambda s, p: None,
        group=lambda g, p: ("GK" if group else None),
        org=lambda o, p: ("OK" if org else None),
        tenant=lambda t, p: None,
        platform=lambda s, p, o: ({"label": "env", "secret": "PK",
                                   "daily_quota": None} if platform else None),
    )


ALL = dict(member=True, group=True, org=True, platform=True)


@pytest.mark.parametrize("flags,expected", [
    (dict(ALL), "user"),
    (dict(group=True, org=True, platform=True), "group"),
    (dict(org=True, platform=True), "org"),
    (dict(platform=True), "platform"),
    (dict(), None),
])
def test_winner_order(flags, expected):
    win = access.cascade_winner("u1", "serper", org=1, group=2, probe=probe(**flags))
    assert (win.mode if win else None) == expected


def test_want_byo_skips_platform():
    win = access.cascade_winner("u1", "serper", org=1, group=2,
                                probe=probe(platform=True), want="byo")
    assert win is None


def test_byo_only_provider_never_reaches_platform():
    # zoho n'a pas 'platform' dans auth_modes : même une sonde plateforme
    # positive (clé résiduelle en base) ne doit JAMAIS gagner (audité 2026-06-11).
    win = access.cascade_winner("u1", "zoho", org=1, group=2,
                                probe=probe(platform=True))
    assert win is None


def test_non_shareable_skips_group_and_org():
    win = access.cascade_winner("u1", "silae", org=1, group=2,
                                probe=probe(group=True, org=True))
    assert win is None


def test_byo_org_only_provider_has_no_member_rung():
    # http : byo_org seul → pas de credential membre par construction (la lecture
    # du coffre lèverait) — trou « un http par département » (2026-07-05).
    win = access.cascade_winner("u1", "http", org=1, group=None,
                                probe=probe(member=True, org=True))
    assert win is not None and win.mode == "org"


def test_anon_sub_none_reduces_to_org_platform():
    win = access.cascade_winner(None, "serper", org=1, group=None,
                                probe=probe(member=True, org=True))
    assert win.mode == "org"


def test_no_group_skips_group_rung():
    win = access.cascade_winner("u1", "serper", org=1, group=None,
                                probe=probe(group=True, org=True))
    assert win.mode == "org"


def test_personal_provider_reads_only_the_call_org():
    """#172 retiré : la sonde membre est interrogée pour l'org de l'APPEL, jamais une
    autre ; sans clé dans cette org, rien ne résout (aucun barreau cross-org)."""
    vues = []
    pr = access.CascadeProbe(
        member=lambda s, o, p: vues.append(o),
        legacy_user=lambda s, p: None, group=lambda g, p: None,
        org=lambda o, p: None, tenant=lambda t, p: None, platform=lambda s, p, o: None)
    assert access.cascade_winner("u1", "unipile", org=1, group=None, probe=pr) is None
    assert set(vues) <= {1}


def test_shared_project_of_another_org_skips_org_rung():
    """#1499, D1 : `_project=` d'une org dont l'appelant n'est pas membre — l'org du
    projet est l'org de l'appel, mais ni son compte, ni son accès plateforme ne lui
    servent."""
    from oto_mcp import session_org
    vues = []
    pr = access.CascadeProbe(
        member=lambda s, o, p: None, legacy_user=lambda s, p: None,
        group=lambda g, p: None, org=lambda o, p: ("OK", ""),
        tenant=lambda t, p: None,
        platform=lambda s, p, o: vues.append(o))
    jeton = session_org.set_call_hors_org("u1", 7)
    try:
        assert access.cascade_winner("u1", "serper", org=7, group=None, probe=pr) is None
        assert vues == [None]   # l'accès plateforme est lu SANS l'org du projet
        # Un autre appelant, ou une autre org : la marque ne s'applique pas.
        assert access.cascade_winner("u2", "serper", org=7, group=None,
                                     probe=pr).mode == "org"
    finally:
        session_org.reset_call_hors_org(jeton)
    assert access.cascade_winner("u1", "serper", org=7, group=None, probe=pr).mode == "org"


def test_walk_all_exposes_losing_rungs():
    hits = list(access.walk_cascade("u1", "serper", org=1, group=2,
                                    probe=probe(**ALL)))
    assert [r.mode for r in hits] == ["user", "group", "org", "platform"]


def test_presence_and_fetch_probes_agree(monkeypatch):
    """Les 2 sondes réelles (PRESENCE/FETCH) donnent le MÊME gagnant sur toute la
    matrice — le contrat anti-« l'UI ment » (résolution vs statut)."""
    matrix = [
        (dict(member=True, group=True, org=True), "user"),
        (dict(group=True, org=True), "group"),
        (dict(org=True), "org"),
        (dict(), None),
    ]
    for flags, expected in matrix:
        monkeypatch.setattr(access.db, "has_member_api_key",
                            lambda s, o, p, *a, **k: flags.get("member", False))
        monkeypatch.setattr(access.db, "get_member_api_key",
                            lambda s, o, p, *a, **k: ("MK" if flags.get("member") else None))
        monkeypatch.setattr(access.db, "member_instance_suspended",
                            lambda s, o, p, *a, **k: False)
        monkeypatch.setattr(access.group_store, "has_group_secret",
                            lambda g, p: flags.get("group", False))
        monkeypatch.setattr(access.group_store, "get_group_secret",
                            lambda g, p: ("GK" if flags.get("group") else None))
        monkeypatch.setattr(access.org_store, "has_org_secret",
                            lambda o, p: flags.get("org", False))
        monkeypatch.setattr(access.org_store, "get_org_secret",
                            lambda o, p: ("OK" if flags.get("org") else None))
        got = []
        for pr in (access.PRESENCE_PROBE, access.FETCH_PROBE):
            win = access.cascade_winner("u1", "zoho", org=1, group=2,
                                        probe=pr, want="byo")
            got.append(win.mode if win else None)
        assert got[0] == got[1] == expected, (flags, got)
