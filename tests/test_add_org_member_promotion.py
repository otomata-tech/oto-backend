"""Choix de l'org maison à l'adhésion + acceptation idempotente.

`add_org_member` : une org d'équipe l'emporte comme maison (`is_active`) sur l'org
que l'inscription a posée et où le compte est encore SEUL (`db.org_d_inscription.
seule_d_inscription`, l'ex-org perso) — sinon un invité atterrit sur l'org de son
inscription au lieu de sa boîte. Le critère sur une vraie base :
`tests/test_org_d_inscription_seule_cede.py`. `accept_invitation*` : idempotent si déjà accepté par le même sub
(le reconcile au signup consomme l'invite avant l'accept explicite → faux 410).

Style test_ensure_home_org.py : on monkeypatche `_connect` avec un conn factice qui
sert les fetchone attendus et enregistre le SQL exécuté (pas de vrai PG).
"""
import oto_mcp.org_store as org_store


# ── add_org_member : auto-activation ─────────────────────────────────────────
class _MemberConn:
    """Sert les 3 SELECT de add_org_member et enregistre chaque exécution."""

    def __init__(self, existing=None, active=None, joining_personal=False,
                 joining_suspended=False):
        self._existing = existing
        self._active = active
        self._jp = {"p": joining_personal, "s": joining_suspended}
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
        elif " AS p," in s and "FROM orgs o WHERE o.id" in s:
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
        active={"org_id": 9, "seule": True}, joining_personal=False,
    )
    assert _inserted_active(calls) is True      # l'org réelle devient maison
    assert _demoted(calls)                       # la perso est démarquée


def test_real_home_unchanged_on_second_real_org(monkeypatch):
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 5, "seule": False}, joining_personal=False,
    )
    assert _inserted_active(calls) is False      # on ne débarque pas d'une maison réelle
    assert not _demoted(calls)


def test_suspended_real_home_yields_to_active_org(monkeypatch):
    # Une maison réelle SUSPENDUE (essai fini) cède à l'org active qu'on rejoint :
    # sinon chaque connexion atterrit sur le mur au lieu de l'org qui paie.
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 5, "seule": False, "suspended": True}, joining_personal=False,
    )
    assert _inserted_active(calls) is True
    assert _demoted(calls)


def test_suspended_home_does_not_yield_to_a_suspended_org(monkeypatch):
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 5, "seule": False, "suspended": True},
        joining_personal=False, joining_suspended=True,
    )
    assert _inserted_active(calls) is False
    assert not _demoted(calls)


def test_les_deux_lectures_portent_le_critere_unique(monkeypatch):
    """L'active courante et l'org rejointe se jugent par LE même prédicat
    (`seule_d_inscription`), lu dans la requête qui lit déjà la ligne : pas de
    comptage de membres à part, pas de lecture par org."""
    from oto_mcp.db.org_d_inscription import seule_d_inscription
    critere = " ".join(seule_d_inscription("o", "%(sub)s").split())
    calls = _run_add(monkeypatch, existing=None,
                     active={"org_id": 9, "seule": True}, joining_personal=False)
    lectures = [s for s, _ in calls if s.startswith("SELECT") and critere in s]
    assert len(lectures) == 2, calls


def test_readd_only_touches_role(monkeypatch):
    calls = _run_add(monkeypatch, existing={"org_role": "org_admin"})
    assert _inserted_active(calls) == "NO_INSERT"
    assert _role_updated(calls)
    # …et le journal (oto#145) nomme le changement, dans la même connexion.
    assert any(s.startswith("INSERT INTO org_member_events") and p[2] == "role_changed"
               for s, p in calls)


def test_personal_bootstrap_activates_when_no_membership(monkeypatch):
    # ensure_home_org : 1er add sur la perso (encore vue non-perso) sans org active.
    calls = _run_add(monkeypatch, existing=None, active=None, joining_personal=True)
    assert _inserted_active(calls) is True


def test_personal_does_not_steal_real_home(monkeypatch):
    calls = _run_add(
        monkeypatch, existing=None,
        active={"org_id": 5, "seule": False}, joining_personal=True,
    )
    assert _inserted_active(calls) is False


def test_second_member_joining_a_personal_org_keeps_personal_of(monkeypatch):
    # Décision d'Alexis du 29/09/2026 : `personal_of` est une ÉTIQUETTE (l'org créée à
    # l'inscription), pas un état. Un 2ᵉ membre qui la rejoint ne la retire plus — le
    # retrait laissait le propriétaire sans org perso, et ses projets hors des listes.
    calls = _run_add(
        monkeypatch, existing=None,
        active=None, joining_personal=True,
    )
    assert not _personal_of_cleared(calls)


def test_first_member_bootstrap_does_not_clear_personal_of(monkeypatch):
    # ensure_home_org : à la création, jp lit encore False (personal_of pas
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
