"""B5, B8 et B9 de l'inventaire des silences (27/08) : l'identité ne se dégrade pas en silence.

- **B5** `resolve_sub` — pendant la fenêtre de bascule de tenant, un hoquet DB rendait
  le sub NON canonicalisé : le porteur d'un vieux jeton était servi **sous son compte
  d'AVANT migration** (coffre, org, projets), sans une ligne de trace.
- **B8** `upsert_user` → `ensure_home_org` — le compte naissait **sans org maison**.
  Tout ce qui en dépend échouait plus tard, ailleurs, sans cause remontable.
- **B9** `upsert_user` → `reconcile_signup_with_invitation` — l'invité d'une org
  s'inscrivait et **ne rejoignait jamais l'org**, avec une invitation orpheline.

Les tests décrivent le SYSTÈME : ce que l'appelant reçoit, ce que le journal porte,
et l'ordre dans lequel les deux effets de première inscription sont tentés.
"""
from __future__ import annotations

import contextlib
import logging
import os

import pytest

from oto_mcp.auth import hooks as auth_hooks
from oto_mcp.db import sub_aliases as db_aliases
from oto_mcp.db import users as db_users


# ── le banc : `_connect` remplacé par un curseur scriptable ──────────────────

class _Row(dict):
    pass


class _Conn:
    def __init__(self, row=None, boum: BaseException = None):
        self._row, self._boum = row, boum
        self.vues = []
        self._servi = False

    def execute(self, sql, params=()):
        self.vues.append(sql)
        if self._boum is not None:
            raise self._boum
        return self

    def fetchone(self):
        """`row` répond à la PREMIÈRE requête ; les suivantes rendent « rien trouvé ».

        ⚠️ Servir la même ligne à toutes les requêtes fabrique des réponses que la
        base ne donnerait jamais, et le banc devient alors un piège pour le premier
        appelant qui en fait deux. Vécu le 2026-09-03 : `upsert_user` lit
        `sub_aliases` après son INSERT (garde anti-résurrection d'un compte en pause)
        et recevait ici la ligne de l'INSERT — donc un `KeyError` sur une colonne que
        cette requête-là ne rend pas."""
        if self._servi:
            return None
        self._servi = True
        return self._row


def _connect_factory(conn):
    @contextlib.contextmanager
    def _c():
        yield conn
    return _c


class _ConnScripte:
    """Un curseur qui rend une réponse DIFFÉRENTE par requête, dans l'ordre.

    ⚠️ **À préférer à `_Conn` dès que le code sous test fait plus d'UNE requête.**
    Une doublure qui sert la même ligne à tout le monde fabrique une réponse que la
    base ne donnerait jamais : elle rend vert un appelant qui lit deux fois, et elle
    masque précisément ce que ce lot exerce — une CHAÎNE d'alias, dont chaque maillon
    est une requête distincte.

    Lire plus que ce qui est scripté ne rend pas `None` en silence : ça DIT le geste.
    Un banc qui répondrait « rien trouvé » à la requête de trop laisserait le code
    sous test conclure — et c'est exactement le mode de panne qu'on ferme ici.
    """

    def __init__(self, reponses):
        self._reponses, self._i = list(reponses), 0
        self.vues = []

    def execute(self, sql, params=()):
        self.vues.append(sql)
        if self._i >= len(self._reponses):
            raise AssertionError(
                f"{self._i + 1}ᵉ requête alors que {len(self._reponses)} réponse(s) "
                f"ont été scriptées — ajoute la réponse attendue à la liste plutôt "
                f"que de laisser le banc en inventer une.\nRequêtes vues : {self.vues}")
        self._courant, self._i = self._reponses[self._i], self._i + 1
        return self

    def fetchone(self):
        return self._courant


# ── B5 : un sub non canonicalisé n'est jamais servi ──────────────────────────
#
# Le drain a quitté `db/users.py` pour `db/sub_aliases.py` le 2026-09-03 (il suit
# désormais la CHAÎNE d'alias, cf. `tests/test_resolve_sub_chaine_live.py`). Les
# tests ci-dessous continuent d'appeler `db_users.resolve_sub` — la surface servie
# n'a pas bougé — mais scriptent le curseur du module qui porte la requête.

def test_resolve_sub_rend_l_alias(monkeypatch):
    """Le drain lit DEUX fois quand un alias existe : le saut simple, puis la chaîne.
    Les deux réponses sont donc scriptées séparément — une doublure qui rendrait la
    même ligne aux deux décrirait une base qui n'existe pas."""
    conn = _ConnScripte([
        _Row(new_sub="sub-migre"),                      # 1. le saut simple : il y a un alias
        _Row(canonique="sub-migre", profondeur=1,       # 2. la chaîne, déroulée
             boucle=False, maillons=2, compte_vivant=True),
    ])
    monkeypatch.setattr(db_aliases, "_connect", _connect_factory(conn))
    assert db_users.resolve_sub("sub-vieux") == "sub-migre"
    assert len(conn.vues) == 2, conn.vues


def test_resolve_sub_sans_alias_rend_le_sub(monkeypatch):
    monkeypatch.setattr(db_aliases, "_connect", _connect_factory(_Conn(row=None)))
    assert db_users.resolve_sub("sub-1") == "sub-1"


def test_resolve_sub_leve_au_lieu_de_servir_l_ancien_compte(monkeypatch):
    """Le cœur de B5 : sur un hoquet DB, rendre le sub d'entrée = servir la requête
    sous le compte d'AVANT migration. Le refus doit être bruyant."""
    monkeypatch.setattr(db_aliases, "_connect",
                        _connect_factory(_Conn(boum=RuntimeError("pool épuisé"))))
    with pytest.raises(RuntimeError):
        db_users.resolve_sub("sub-vieux")


def test_un_alias_qui_disparait_entre_les_deux_lectures_leve(monkeypatch):
    """Le drain lit deux fois : le saut simple, puis la chaîne. Si la seconde lecture ne
    trouve plus rien, la tentation est de rendre le sub d'entrée — c'est-à-dire de servir
    la requête sous le compte d'AVANT bascule, exactement le silence B5. Aucun chemin
    n'écrit ce cas aujourd'hui (rien ne supprime de `sub_aliases`) : sans ce test, la
    branche ne serait jamais exécutée, donc jamais un refus prouvé."""
    monkeypatch.setattr(
        db_aliases, "_connect",
        _connect_factory(_ConnScripte([_Row(new_sub="sub-migre"), None])))

    with pytest.raises(db_aliases.AliasNonResolvable) as e:
        db_users.resolve_sub("sub-vieux")
    assert e.value.motif == "alias_evanoui"


def test_le_seam_mcp_ne_reclasse_pas_l_echec_en_absence_de_jeton(monkeypatch):
    """`current_user_sub_from_token` attrapait tout : l'échec de canonicalisation y
    devenait « pas de jeton » et la requête repartait anonyme, sans un mot. Le seam
    doit laisser passer l'échec d'IDENTITÉ (il garde sa tolérance à l'absence de
    contexte fastmcp, testée par ailleurs)."""
    monkeypatch.setenv("OTO_MCP_TENANT_MIGRATION_ISS", "https://ancien.logto.app")
    monkeypatch.setenv("OTO_MCP_DEV_SUB", "sub-dev-de-secours")

    class _Tok:
        claims = {"sub": "sub-vieux", "email": "a@b.c", "iss": "https://ancien.logto.app"}

    monkeypatch.setattr(auth_hooks, "_sub_override", type(auth_hooks._sub_override)("x", default=None))
    import fastmcp.server.dependencies as deps
    monkeypatch.setattr(deps, "get_access_token", lambda: _Tok())
    from oto_mcp import db
    monkeypatch.setattr(db, "resolve_sub", lambda s: (_ for _ in ()).throw(RuntimeError("pool épuisé")))

    with pytest.raises(RuntimeError):
        auth_hooks.current_user_sub_from_token()


# ── B8 / B9 : un compte ne naît pas à moitié ─────────────────────────────────

@pytest.fixture
def signup(monkeypatch):
    """Le VRAI premier insert : `RETURNING (xmax = 0)` rend `inserted=True`."""
    monkeypatch.setattr(db_users, "_connect",
                        _connect_factory(_Conn(row=_Row(inserted=True))))
    from oto_mcp import org_store
    faits = []
    monkeypatch.setattr(org_store, "reconcile_signup_with_invitation",
                        lambda sub, email: faits.append("invitation"))
    monkeypatch.setattr(org_store, "ensure_home_org",
                        lambda sub, email=None, name=None: faits.append("org_maison"))
    monkeypatch.setattr(org_store, "list_orgs_for_user", lambda sub: [])
    from oto_mcp import partage_en_attente
    monkeypatch.setattr(partage_en_attente, "honorer_au_signup",
                        lambda sub, email: faits.append("partages"))
    return faits


def test_signup_nominal_ne_leve_pas(signup):
    db_users.upsert_user("u-1", email="a@b.c", name="A")
    assert signup == ["invitation", "partages", "org_maison"]


def test_un_invite_deja_membre_ne_recoit_pas_d_org_perso(signup, monkeypatch):
    """La réconciliation l'a fait entrer dans l'org de son invitation : une org perso
    à côté n'était qu'un second espace qu'il n'a pas demandé."""
    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "list_orgs_for_user", lambda sub: [{"org_id": 7}])
    db_users.upsert_user("u-1", email="a@b.c", name="A")
    assert signup == ["invitation", "partages"]


def test_partages_en_attente_non_honores_est_une_erreur_nommee(signup, monkeypatch):
    """Les partages d'objets reçus avant l'inscription : un échec se NOMME, comme
    l'invitation d'org, et ne dispense pas du reste."""
    from oto_mcp import partage_en_attente
    monkeypatch.setattr(partage_en_attente, "honorer_au_signup",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(db_users.OnboardingIncomplet) as e:
        db_users.upsert_user("u-1", email="a@b.c", name="A")
    assert "partage_en_attente" in str(e.value)
    assert signup == ["invitation", "org_maison"]


def test_org_maison_manquante_est_une_erreur_nommee(signup, monkeypatch, caplog):
    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "ensure_home_org",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("UniqueViolation")))
    caplog.set_level(logging.DEBUG, logger=db_users.logger.name)

    with pytest.raises(db_users.OnboardingIncomplet) as e:
        db_users.upsert_user("u-1", email="a@b.c", name="A")
    assert "ensure_home_org" in str(e.value)

    erreurs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert erreurs, "aucune trace de l'échec"
    assert "u-1" in erreurs[0].getMessage() and "a@b.c" in erreurs[0].getMessage()


def test_invitation_non_honoree_est_une_erreur_nommee(signup, monkeypatch):
    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "reconcile_signup_with_invitation",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))

    with pytest.raises(db_users.OnboardingIncomplet) as e:
        db_users.upsert_user("u-1", email="a@b.c", name="A")
    assert "reconcile_signup_with_invitation" in str(e.value)
    # L'échec de l'un ne dispense pas de tenter les autres : tous sont tentés, et
    # l'erreur finale dit lesquels ont manqué.
    assert signup == ["partages", "org_maison"]


def test_un_login_ordinaire_ne_declenche_rien(monkeypatch):
    """`inserted=False` (UPDATE) : aucun effet de première inscription, aucun risque
    de lever sur le trajet chaud de CHAQUE requête."""
    monkeypatch.setattr(db_users, "_connect",
                        _connect_factory(_Conn(row=_Row(inserted=False))))
    from oto_mcp import org_store
    monkeypatch.setattr(org_store, "ensure_home_org",
                        lambda *a, **k: pytest.fail("ne doit pas être appelé"))
    monkeypatch.setattr(org_store, "reconcile_signup_with_invitation",
                        lambda *a, **k: pytest.fail("ne doit pas être appelé"))
    from oto_mcp import partage_en_attente
    monkeypatch.setattr(partage_en_attente, "honorer_au_signup",
                        lambda *a, **k: pytest.fail("ne doit pas être appelé"))
    db_users.upsert_user("u-1", email="a@b.c")
