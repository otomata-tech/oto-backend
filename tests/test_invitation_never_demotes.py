"""Accepter une invitation n'enlève JAMAIS un droit — les trois chemins (#297).

`org_store._accept_invitation_row` écrit les deux paliers par **upsert**
(`add_org_member`, `add_group_member`) : le rôle de l'invitation écrasait donc celui
déjà détenu, y compris vers le bas. Deux formes du même défaut :

- **org** — un `org_admin` invité en `org_member` était rétrogradé en cliquant
  « accepter ». Ce n'est pas théorique : mesuré en prod le 11/08, l'invitation #94
  (org 81, posée le 08/07) est armée sur quelqu'un qui est déjà `org_admin`.
- **équipe** — pire, parce que le défaut suffit : `inv.get("group_role") or
  "group_member"` rétrograde un chef même quand l'invitation ne parle pas d'équipe.

Arbitrage (Alexis) : accepter est un **ajout**, on garde le **maximum des deux rôles**
aux DEUX paliers ; rétrograder reste possible par la route dédiée, elle-même gardée
(#273/#280). L'ordre des rôles est dérivé de `roles` (`max_org_role`/`max_group_role`),
jamais recopié dans le store — un rang recopié diverge au premier rôle ajouté.

**Chaque cas est joué sur les deux entrées** (lien mail, réconciliation de signup — le
code court partageable, troisième entrée d'origine, a été RETIRÉ le 15/09/2026,
oto-backend#560). Elles convergent vers le même corps, mais c'est précisément la
leçon de #280 : un test qui n'exerce qu'un chemin passe au vert en laissant le trou
ouvert. On monkeypatche les stores (pas de PG).
"""
import pytest

from oto_mcp import group_store, org_store

ORG_ID = 81
GROUP_ID = 7
SUB = "invitee"


class _RowConn:
    """Connexion factice : `execute(...).fetchone()` rend toujours la même ligne."""

    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return type("R", (), {"fetchone": lambda s: self._row})()


def _inv(*, org_role="org_member", group_id=None, group_role=None):
    return {"id": 94, "org_id": ORG_ID, "org_role": org_role,
            "group_id": group_id, "group_role": group_role}


def _patch(monkeypatch, *, org_role=None, group_role=None):
    """`org_role`/`group_role` = rôles ACTUELS de l'invité (None = pas encore membre).
    Rend le journal des écritures réellement tentées sur les deux stores."""
    written = {"org": [], "group": []}
    monkeypatch.setattr(org_store, "get_org_role", lambda oid, sub: org_role)
    monkeypatch.setattr(org_store, "add_org_member",
                        lambda oid, sub, role, *, actor: written["org"].append((oid, sub, role)))
    monkeypatch.setattr(org_store, "set_active_org", lambda sub, oid: None)
    # La maison est LUE depuis oto#161 (le palier équipe ne pose le groupe actif que
    # si la maison est bien l'org du groupe) : sans ce stub, ce banc sans PG tomberait
    # sur `DATABASE_URL not set` au lieu de mesurer les rôles.
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: ORG_ID)
    monkeypatch.setattr(org_store, "_mark_invitation_accepted", lambda i, sub: None)
    monkeypatch.setattr(group_store, "get_group_role", lambda gid, sub: group_role)
    monkeypatch.setattr(group_store, "add_group_member",
                        lambda gid, sub, role: written["group"].append((gid, sub, role)))
    monkeypatch.setattr(group_store, "set_active_group", lambda sub, gid: None)
    return written


# Les deux entrées d'acceptation, appelées avec la MÊME intention métier :
# « <sub> accepte cette invitation ».
def _via_token(monkeypatch, inv, sub):
    monkeypatch.setattr(org_store, "get_invitation_by_token", lambda t: inv)
    return org_store.accept_invitation("tok", sub)


def _via_signup(monkeypatch, inv, sub):
    monkeypatch.setattr(org_store, "_connect", lambda: _RowConn(inv))
    return org_store.reconcile_signup_with_invitation(sub, "invitee@x.tld")


ALL_PATHS = pytest.mark.parametrize("accept", [_via_token, _via_signup],
                                    ids=["lien mail", "signup"])


# ── Palier org : le cas armé en prod ─────────────────────────────────────────

@ALL_PATHS
def test_an_org_admin_stays_admin_on_a_member_invitation(monkeypatch, accept):
    """LE cas : org_admin + invitation `org_member` → il reste org_admin.
    Sans le correctif, l'upsert écrivait `org_member`."""
    written = _patch(monkeypatch, org_role="org_admin")
    res = accept(monkeypatch, _inv(org_role="org_member"), SUB)
    assert written["org"] == [(ORG_ID, SUB, "org_admin")]
    # L'écho annonce le rôle ÉCRIT, pas celui de l'invitation.
    assert res["org_role"] == "org_admin"


@ALL_PATHS
def test_a_newcomer_joins_with_the_invited_role(monkeypatch, accept):
    """Le nominal n'est pas cassé : un non-membre invité en org_member le devient."""
    written = _patch(monkeypatch, org_role=None)
    res = accept(monkeypatch, _inv(org_role="org_member"), SUB)
    assert written["org"] == [(ORG_ID, SUB, "org_member")]
    assert res["org_role"] == "org_member"


@ALL_PATHS
def test_an_invitation_still_promotes(monkeypatch, accept):
    """L'élévation reste le sens utile d'une invitation : org_member → org_admin."""
    written = _patch(monkeypatch, org_role="org_member")
    res = accept(monkeypatch, _inv(org_role="org_admin"), SUB)
    assert written["org"] == [(ORG_ID, SUB, "org_admin")]
    assert res["org_role"] == "org_admin"


# ── Palier équipe : le défaut par défaut est le pire ─────────────────────────

@ALL_PATHS
def test_a_chief_stays_chief_when_the_invitation_says_nothing(monkeypatch, accept):
    """Le pire cas : l'invitation ne parle PAS d'équipe (`group_role` absent), le défaut
    `group_member` s'applique — et rétrogradait le chef. Il reste chef."""
    written = _patch(monkeypatch, org_role="org_member", group_role="group_admin")
    res = accept(monkeypatch, _inv(group_id=GROUP_ID, group_role=None), SUB)
    assert written["group"] == [(GROUP_ID, SUB, "group_admin")]
    assert res["group_role"] == "group_admin"


@ALL_PATHS
def test_a_chief_stays_chief_on_an_explicit_member_invitation(monkeypatch, accept):
    """Même verdict quand l'invitation nomme explicitement `group_member` : accepter
    n'est pas une administration de rôle, quelle que soit l'intention de l'émetteur."""
    written = _patch(monkeypatch, org_role="org_admin", group_role="group_admin")
    accept(monkeypatch, _inv(group_id=GROUP_ID, group_role="group_member"), SUB)
    assert written["group"] == [(GROUP_ID, SUB, "group_admin")]


@ALL_PATHS
def test_a_newcomer_joins_the_team_as_invited(monkeypatch, accept):
    """Nominal équipe : non-membre + invitation sans `group_role` → group_member."""
    written = _patch(monkeypatch, org_role=None, group_role=None)
    res = accept(monkeypatch, _inv(group_id=GROUP_ID, group_role=None), SUB)
    assert written["group"] == [(GROUP_ID, SUB, "group_member")]
    assert res["group_role"] == "group_member"


@ALL_PATHS
def test_a_team_invitation_still_promotes_to_chief(monkeypatch, accept):
    written = _patch(monkeypatch, org_role="org_member", group_role="group_member")
    res = accept(monkeypatch, _inv(group_id=GROUP_ID, group_role="group_admin"), SUB)
    assert written["group"] == [(GROUP_ID, SUB, "group_admin")]
    assert res["group_role"] == "group_admin"


# ── L'ordre vient de `roles`, il n'est pas recopié dans le store ─────────────

def test_the_ranking_is_derived_from_roles():
    """Tripwire : si la hiérarchie bouge dans `roles`, l'acceptation la suit — c'est
    la raison d'être de `max_org_role`/`max_group_role`. Un ordre en dur dans
    `org_store` passerait ce fichier tout en divergeant au premier rôle ajouté."""
    from oto_mcp import roles

    assert roles.max_org_role("org_admin", "org_member") == "org_admin"
    assert roles.max_org_role("org_member", "org_admin") == "org_admin"
    assert roles.max_org_role(None, "org_member") == "org_member"
    assert roles.max_group_role("group_admin", "group_member") == "group_admin"
    assert roles.max_group_role(None, "group_member") == "group_member"
    # Rôle hors hiérarchie : pas de rang deviné, le demandé passe et c'est l'enum du
    # store qui le refuse (sinon une écriture illégale serait avalée en silence).
    assert roles.max_org_role("org_admin", "sorcier") == "sorcier"
