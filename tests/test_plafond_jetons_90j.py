"""Le plafond de 90 jours des jetons `oto_` NEUFS, au store (`db.tokens`).

Un jeton sans échéance survivait à tout, mot de passe changé compris. Le store est la
dernière porte — `scripts/issue_token.py` l'appelle sans passer par une capacité — donc
la règle y vit (`echeance_bornee`), et les capacités la lisent au même endroit.
On relève le SQL émis, sans base.
"""
from __future__ import annotations

import contextlib

import pytest

from oto_mcp.db import tokens


@pytest.fixture
def ecrit(monkeypatch):
    vus: list = []
    monkeypatch.setattr(tokens, "upsert_user", lambda sub: vus.append(("upsert", sub)))

    class _Conn:
        def execute(self, sql, params=()):
            vus.append(("sql", sql))

    @contextlib.contextmanager
    def _connect():
        yield _Conn()

    monkeypatch.setattr(tokens, "_connect", _connect)
    return vus


def _expires(vus) -> str:
    (sql,) = [v[1] for v in vus if v[0] == "sql"]
    return sql.split("VALUES (%s, %s, %s, ")[1].split(",")[0]


def test_la_constante_nommee_vaut_90():
    assert tokens.TTL_MAX_JOURS == 90


@pytest.mark.parametrize("ttl", [None, 0, -5])
def test_sans_echeance_un_jeton_recoit_90_jours(ecrit, ttl):
    tokens.create_api_token("u-1", ttl_days=ttl)
    assert _expires(ecrit) == "NOW() + INTERVAL '90 days'"


def test_une_echeance_sous_le_plafond_est_gardee(ecrit):
    tokens.create_api_token("u-1", ttl_days=30)
    assert _expires(ecrit) == "NOW() + INTERVAL '30 days'"


def test_au_dela_du_plafond_le_store_refuse_avant_toute_ecriture(ecrit):
    with pytest.raises(tokens.EcheanceTropLointaine) as e:
        tokens.create_api_token("u-1", ttl_days=91)
    assert e.value.code == "token_ttl_too_long"
    assert ecrit == [], "ni compte créé (`upsert_user`), ni ligne écrite"


def test_une_delegation_en_secondes_reste_bornee(ecrit):
    tokens.create_api_token("u-1", ttl_seconds=720, kind="delegation")
    assert _expires(ecrit) == "NOW() + INTERVAL '720 seconds'"
    with pytest.raises(tokens.EcheanceTropLointaine):
        tokens.create_api_token("u-1", ttl_seconds=91 * 86_400)


def test_la_capacite_lit_la_regle_du_store():
    """La capacité lit la règle du store, elle ne la recopie pas."""
    from oto_mcp.capabilities import api_tokens
    assert api_tokens._echeance_plafonnee(None) == tokens.TTL_MAX_JOURS
    assert api_tokens._echeance_plafonnee(7) == 7
