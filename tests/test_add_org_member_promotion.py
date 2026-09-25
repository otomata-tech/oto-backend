"""Choix de l'org maison à l'adhésion + acceptation idempotente.

`add_org_member` : une org RÉELLE l'emporte sur l'org perso silencieuse comme
maison (`is_active`) — sinon un invité atterrit sur sa perso au lieu de sa boîte
(ADR 0030/0033). `accept_invitation*` : idempotent si déjà accepté par le même sub
(le reconcile au signup consomme l'invite avant l'accept explicite → faux 410).

Style test_ensure_home_org.py : on monkeypatche `_connect` avec un conn factice qui
sert les fetchone attendus et enregistre le SQL exécuté (pas de vrai PG).
"""
import oto_mcp.org_store as org_store


# ── add_org_member : auto-activation ─────────────────────────────────────────
class _MemberConn:
    """Sert les 3 SELECT de add_org_member et enregistre chaque exécution."""

    def __init__(self, existing=None, active=None, joining_personal=False):
        self._existing = existing
        self._active = active
        self._jp = {"p": joining_personal}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        conn = self

        class _T:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        return _T()

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.calls.append((s, params))
        if "SELECT org_role FROM org_members WHERE org_id" in s:
            row = self._existing
        elif "m.is_active" in s:
            row = self._active
        elif "personal_of IS NOT NULL) AS p FROM orgs" in s:
            row = self._jp
        else:
            row = None
        return type("R", (), {"fetchone": lambda self2: row})()


def _run_add(monkeypatch, **conn_kw):
    conn = _MemberConn(**conn_kw)
    monkeypatch.setattr(org_store, "_connect", lambda: conn)
    monkeypatch.setattr(org_store, "upsert_user", lambda sub: None)
    monkeypatch.setattr(org_store, "_sync_mfa_mirror", lambda org_id: None)
    org_store.add_org_member(100, "u1", "org_member")
    return conn.calls


def _inserted_active(calls):
    for s, p in calls:
        if s.startswith("INSERT INTO org_members"):
            return p[3]
    return "NO_INSERT"


def _demoted(calls):
    return any(s.startswith("UPDATE org_members SET is_active = FALSE") for s, _ in calls)


def _role_updated(calls):
    return any(s.startswith("UPDATE org_members SET org_role") for s, _ in calls)


def _personal_of_cleared(calls):
    return any(s.startswith("UPDATE orgs SET personal_of = NULL") for s, _ in calls)


def test_brand_new_user_activates_real_org(monkeypatch):
    calls = _run_add(monkeypatch, existing=None, active=None, joining_personal=False)
    assert _inserted_active(calls) is True
    assert not _demoted(calls)


def test_personal_active_yields_to_real_org(monkeypatch):
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 9, "personal": True}, joining_personal=False,
    )
    assert _inserted_active(calls) is True      # l'org réelle devient maison
    assert _demoted(calls)                       # la perso est démarquée


def test_real_home_unchanged_on_second_real_org(monkeypatch):
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 5, "personal": False}, joining_personal=False,
    )
    assert _inserted_active(calls) is False      # on ne débarque pas d'une maison réelle
    assert not _demoted(calls)


def test_readd_only_touches_role(monkeypatch):
    calls = _run_add(monkeypatch, existing={"org_role": "org_admin"})
    assert _inserted_active(calls) == "NO_INSERT"
    assert _role_updated(calls)
    # …et le journal (oto#145) nomme le changement, dans la même connexion.
    assert any(s.startswith("INSERT INTO org_member_events") and p[2] == "role_changed"
               for s, p in calls)


def test_personal_bootstrap_activates_when_no_membership(monkeypatch):
    # ensure_personal_org : 1er add sur la perso (encore vue non-perso) sans org active.
    calls = _run_add(monkeypatch, existing=None, active=None, joining_personal=True)
    assert _inserted_active(calls) is True


def test_personal_does_not_steal_real_home(monkeypatch):
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 5, "personal": False}, joining_personal=True,
    )
    assert _inserted_active(calls) is False


def test_second_member_joining_a_personal_org_clears_personal_of(monkeypatch):
    # Une org perso est mono-membre par définition (slot unique `personal_of`) :
    # dès qu'un 2e membre distinct la rejoint, ce n'en est plus une (deux tenants tiers,
    # 2026-08-04 — une org réelle multi-membre restait "personal" à vie faute de ce clear).
    calls = _run_add(
        monkeypatch, existing=None,
        active=None, joining_personal=True,
    )
    assert _personal_of_cleared(calls)


def test_first_member_bootstrap_does_not_clear_personal_of(monkeypatch):
    # ensure_personal_org : à la création, jp lit encore False (personal_of pas
    # encore posé sur l'org perso à ce stade — cf. docstring add_org_member).
    calls = _run_add(monkeypatch, existing=None, active=None, joining_personal=False)
    assert not _personal_of_cleared(calls)


def test_readd_does_not_clear_personal_of(monkeypatch):
    calls = _run_add(monkeypatch, existing={"org_role": "org_member"})
    assert not _personal_of_cleared(calls)


# ── accept_invitation* : idempotence ─────────────────────────────────────────
class _RowConn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return type("R", (), {"fetchone": lambda s: self._row})()


def test_accept_by_token_idempotent_same_sub(monkeypatch):
    monkeypatch.setattr(org_store, "get_invitation_by_token", lambda token: None)  # consommée
    monkeypatch.setattr(org_store, "_connect",
                        lambda: _RowConn({"org_id": 7, "org_role": "org_member",
                                          "group_id": None, "group_role": None,
                                          "accepted_sub": "u1"}))
    assert org_store.accept_invitation("tok", "u1") == {
        "org_id": 7, "org_role": "org_member", "group_id": None, "group_role": None}


def test_accept_by_token_not_idempotent_other_sub(monkeypatch):
    monkeypatch.setattr(org_store, "get_invitation_by_token", lambda token: None)
    monkeypatch.setattr(org_store, "_connect",
                        lambda: _RowConn({"org_id": 7, "org_role": "org_member", "accepted_sub": "someone_else"}))
    assert org_store.accept_invitation("tok", "u1") is None


def test_accept_by_token_none_when_absent(monkeypatch):
    monkeypatch.setattr(org_store, "get_invitation_by_token", lambda token: None)
    monkeypatch.setattr(org_store, "_connect", lambda: _RowConn(None))
    assert org_store.accept_invitation("tok", "u1") is None
