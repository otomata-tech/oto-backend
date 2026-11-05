"""R6 — le compte que lit le refus du quota plateforme, contre une vraie base.

Sous une arête `user` d'un connecteur chaîné, le refus lit le compteur d'arête ; sous une
arête d'org (ou de groupe), il lit `usage`, par personne, comme avant
(`docs/credits-au-moment-de-l-appel.md`, R6). Ce que ce fichier tient :

1. **aucun plafond ne bouge pour un bénéficiaire d'une arête d'org** : chacun a le sien,
   même quand le compteur de l'arête, lui, compte pour toute l'org ;
2. sous une arête `user`, c'est bien le compteur d'arête qui décide ;
3. ce que coûtent, en requêtes, la résolution et le règlement d'un appel chaîné.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from oto_mcp.mcp_errors import McpError

ORG = 42
CONTEXTE = {"alice": None, "bob": ORG, "carol": ORG}   # sub → org active
QUOTA_USER, QUOTA_ORG = 3, 2

# Relevé du 19/10/2026 sur ce jeu, requêtes par appel (résolution, règlement). Avant R6 :
# arête `user` (9, 6), arête d'org (17, 4) — l'ancien chemin relu à chaque résolution où
# la chaîne a un avis, et les deux compteurs comparés à chaque règlement sous arête
# `user`. Un écart est une régression du chemin chaud, ou une économie à graver ici.
# #1499 (bascule des comptes) : arête d'org 16 → 15, la lecture des prêts reçus
# (`share_side`) a disparu avec eux ; puis 15 → 16 : l'étape 5 (05/11/2026) lit une fois
# les comptes d'autres orgs ouverts à l'appelant avant de laisser servir la clé
# plateforme (alice, sans org, ne la paie pas).
_REQUETES = {"alice": (8, 4), "bob": (16, 4)}


@pytest.fixture(scope="module")
def jeu(live):
    # Le registre du SERVEUR (comme `test_connecteurs_me_rapide_1148`) : sans lui, le
    # compte de requêtes dépend de ce que les tests joués avant dans le worker ont importé.
    import _mcp_app
    _mcp_app.static_mcp()
    from oto_mcp import credentials_store, grants_chain
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
    try:
        credentials_store.set_platform_key("env", "fullenrich", "k-1", set_by="op")
        aretes = {
            "user": grants_chain.grant("fullenrich", "user:alice", daily_quota=QUOTA_USER),
            "org": grants_chain.grant("fullenrich", f"org:{ORG}", daily_quota=QUOTA_ORG),
        }
        yield aretes
    finally:
        if avant is None:
            os.environ.pop("OTO_MCP_MASTER_KEY", None)
        else:
            os.environ["OTO_MCP_MASTER_KEY"] = avant


@pytest.fixture
def en_tant_que(monkeypatch):
    """`en_tant_que(sub)` : les appels suivants viennent de `sub`, dans son org active."""
    from oto_mcp import access
    actuel = {"sub": None}
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: actuel["sub"])
    monkeypatch.setattr(access, "current_org", lambda sub: CONTEXTE.get(sub))
    monkeypatch.setattr(access, "current_group", lambda sub: None)

    def _poser(sub):
        actuel["sub"] = sub
    return _poser


def _appel(sub: str) -> None:
    """Un appel servi sous la clé plateforme : la résolution, puis le règlement du bord."""
    from oto_mcp import access
    access.resolve_api_key("fullenrich")
    access.regler_appel(sub, "fullenrich_enrich_linkedin",
                        {"platform_carrier": "fullenrich", "units": 1})


def _compter(f) -> int:
    from unittest import mock
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        f()
    return len(vues)


def _compteur(arete: int) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(calls), 0) AS n FROM grant_counters "
                           "WHERE grant_id = %s", (arete,)).fetchone()
    return int(row["n"])


def _vider_les_compteurs() -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM grant_counters")
        conn.execute("DELETE FROM usage")


def test_aucun_plafond_ne_bouge_pour_un_beneficiaire_d_une_arete_d_org(jeu, en_tant_que):
    from oto_mcp import access
    _vider_les_compteurs()
    en_tant_que("bob")
    for _ in range(QUOTA_ORG):
        _appel("bob")
    with pytest.raises(McpError) as refus:
        access.resolve_api_key("fullenrich")
    assert f"({QUOTA_ORG}/{QUOTA_ORG})" in str(refus.value)
    # Le compteur de l'arête compte pour TOUTE l'org : il est plein…
    assert _compteur(jeu["org"]) == QUOTA_ORG
    # … et pourtant Carol, servie par la même arête, a tout son plafond : par personne.
    en_tant_que("carol")
    assert access.platform_quota_hint("fullenrich", "carol")["used"] == 0
    assert access.platform_quota_hint("fullenrich", "carol")["limit"] == QUOTA_ORG
    _appel("carol")
    assert access.platform_quota_hint("fullenrich", "carol")["used"] == 1
    assert _compteur(jeu["org"]) == QUOTA_ORG + 1


def test_sous_une_arete_user_le_compteur_d_arete_decide(jeu, en_tant_que):
    from oto_mcp import access
    from oto_mcp.db import grants as db_grants
    _vider_les_compteurs()
    en_tant_que("alice")
    _appel("alice")
    assert access.platform_quota_hint("fullenrich", "alice")["used"] == 1
    # Le compteur d'arête avance seul (l'historique `usage` n'a vu qu'un appel) : c'est
    # lui que lit le refus.
    db_grants.bump_counter(jeu["user"], QUOTA_USER - 1)
    assert access.platform_quota_hint("fullenrich", "alice")["used"] == QUOTA_USER
    with pytest.raises(McpError) as refus:
        access.resolve_api_key("fullenrich")
    assert f"({QUOTA_USER}/{QUOTA_USER})" in str(refus.value)


@pytest.mark.parametrize("sub", ["alice", "bob"])
def test_ce_que_coute_un_appel_chaine(jeu, en_tant_que, sub):
    from oto_mcp import access
    _vider_les_compteurs()
    en_tant_que(sub)
    # Un premier appel, non compté : le compte est celui du régime établi, pas celui des
    # mémoires de processus encore froides (il dépendrait de l'ordre de la suite).
    _appel(sub)
    resolution = _compter(lambda: access.resolve_api_key("fullenrich"))
    reglement = _compter(lambda: access.regler_appel(
        sub, "fullenrich_enrich_linkedin", {"platform_carrier": "fullenrich", "units": 1}))
    assert (resolution, reglement) == _REQUETES[sub]
