"""Le relevé serveur des reveals de téléphone Apollo de la clé PLATEFORME
(`apollo_releve.py`), sur une VRAIE base (`live`).

Ce que ce banc fige :

1. **Une commande que personne n'a relue est facturée** — au coût qu'Apollo a déclaré
   dans ce qu'il nous a livré, sous l'org et la personne qui l'ont commandée, en ligne
   de relevé (`kind='settlement'`) que la lentille de facturation lit comme la relève.
2. **Un coût illisible n'est jamais zéro** : livrée sans coût lisible, ou jamais livrée
   passé le délai, la commande est facturée au pire cas.
3. **Une commande relevée n'est pas refacturée**, une commande récente attend son
   lecteur, une clé propre n'est jamais relevée.
4. **Le plafond par org compte les commandes du jour de l'org**, pas celles d'une autre.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

_SUB, _ORG, _AUTRE_ORG = "sub-releve", 7201, 7202


def _cle(plateforme: bool):
    from oto_mcp.access.resolved_credential import ResolvedCredential
    if plateforme:
        return ResolvedCredential(provider="apollo", secret="k", is_platform=True,
                                  mode="platform", entity_type=None, entity_id=None)
    return ResolvedCredential(provider="apollo", secret="k", is_platform=False,
                              mode="org", entity_type="org", entity_id=str(_ORG))


@pytest.fixture
def commanditaire(live, monkeypatch):
    from oto_mcp import access
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.acme.test")
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: _SUB)
    monkeypatch.setattr(access, "current_org", lambda sub: _ORG)
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM apollo_phone_reveals WHERE sub = %s", (_SUB,))
        conn.execute("DELETE FROM tool_calls WHERE sub = %s", (_SUB,))


def _commande(rid: str | None, *, plateforme: bool = True, payload=None,
              heures: float = 3.0) -> str:
    """Une commande passée il y a `heures` heures, livrée ou pas."""
    import hashlib

    from oto_mcp import apollo_receiver
    from oto_mcp.db._conn import _connect
    jeton, _ = apollo_receiver.commander(_cle(plateforme))
    if rid is not None:
        apollo_receiver.lier(jeton, rid)
    empreinte = hashlib.sha256(jeton.encode()).hexdigest()
    if payload is not None:
        apollo_receiver.recevoir(jeton, payload)
    with _connect() as conn:
        conn.execute("UPDATE apollo_phone_reveals SET created_at = NOW() - "
                     "make_interval(secs => %s) WHERE token_hash = %s",
                     (heures * 3600, empreinte))
    return empreinte


def _lignes_facturees() -> list[dict]:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT kind, tool, sub, org_id, quantity, key_mode, args FROM tool_calls "
            "WHERE tool = 'apollo_reveal_phone_result' AND kind = 'settlement' "
            "ORDER BY id").fetchall()


def _client(sonde: dict | None = None):
    c = MagicMock()
    c.poll_webhook_result.return_value = sonde or {"done": False}
    return c


def _relever(client=None, dry_run=False):
    from oto_mcp import apollo_releve
    with pytest.MonkeyPatch.context() as mp:
        import oto.tools.apollo as apollo_lib
        mp.setattr(apollo_lib, "ApolloClient", lambda **kw: client or _client())
        return apollo_releve.relever("cle-plateforme", dry_run=dry_run)


def test_une_commande_livree_non_relue_est_facturee_a_son_cout_declare(commanditaire):
    _commande("6001", payload={"request_id": 6001, "credits_consumed": 8,
                               "webhook_result": {"people": []}})
    compte = _relever()
    assert compte["factures"] == 1
    (ligne,) = _lignes_facturees()
    assert (ligne["sub"], ligne["org_id"], ligne["quantity"], ligne["key_mode"]) == (
        _SUB, _ORG, 8, "platform")
    assert ligne["args"]["apollo_reveal_request_id"] == "6001"
    assert _relever()["factures"] == 0, "relevée une fois, jamais refacturée"
    assert len(_lignes_facturees()) == 1


def test_un_cout_declare_nul_ne_trace_rien_mais_clot_la_commande(commanditaire):
    _commande("6002", payload={"credits_consumed": 0, "webhook_result": {"people": []}})
    assert _relever()["gratuits"] == 1
    assert _lignes_facturees() == []
    assert _relever()["gratuits"] == 0


def test_livree_sans_cout_lisible_elle_est_facturee_au_pire_cas(commanditaire):
    _commande("6003", payload={"webhook_result": {"people": []}})
    _relever()
    (ligne,) = _lignes_facturees()
    assert ligne["quantity"] == 8


def test_pas_encore_livree_le_releve_sonde_apollo(commanditaire):
    _commande("6004")
    client = _client({"done": True, "result": {"credits_consumed": 8}})
    assert _relever(client)["factures"] == 1
    client.poll_webhook_result.assert_called_once_with("6004")
    assert _lignes_facturees()[0]["quantity"] == 8


def test_jamais_livree_passe_le_delai_elle_est_facturee_au_pire_cas(commanditaire):
    _commande("6005", heures=72)
    assert _relever()["pire_cas"] == 1
    assert _lignes_facturees()[0]["quantity"] == 8


def test_jamais_livree_dans_le_delai_elle_attend(commanditaire):
    _commande("6006", heures=5)
    assert _relever()["en_cours"] == 1
    assert _lignes_facturees() == []


def test_sans_request_id_passe_le_delai_elle_est_facturee_sous_son_jeton(commanditaire):
    empreinte = _commande(None, heures=72)
    _relever()
    (ligne,) = _lignes_facturees()
    assert ligne["args"]["apollo_reveal_request_id"] == "jeton:" + empreinte[:16]


def test_une_commande_recente_est_laissee_a_son_lecteur(commanditaire):
    _commande("6007", payload={"credits_consumed": 8}, heures=0.1)
    assert _relever() == {"factures": 0, "pire_cas": 0, "gratuits": 0,
                          "en_cours": 0, "echecs": 0}


def test_une_commande_relue_n_est_pas_refacturee(commanditaire):
    from oto_mcp import apollo_receiver
    _commande("6008", payload={"credits_consumed": 8})
    apollo_receiver.marquer_releve("6008", _cle(True))
    assert _relever()["factures"] == 0
    assert _lignes_facturees() == []


def test_une_cle_propre_n_est_jamais_relevee(commanditaire):
    _commande("6009", plateforme=False, payload={"credits_consumed": 8})
    assert _relever()["factures"] == 0
    assert _lignes_facturees() == []


def test_a_blanc_rien_n_est_ecrit(commanditaire):
    _commande("6010", payload={"credits_consumed": 8})
    assert _relever(dry_run=True)["factures"] == 1
    assert _lignes_facturees() == []
    assert _relever()["factures"] == 1


def test_le_plafond_compte_les_commandes_du_jour_de_l_org(commanditaire, monkeypatch):
    from oto_mcp import access, apollo_receiver
    cle = _cle(True)
    _commande("6011", heures=0.5)
    _commande("6012", heures=0.5)
    assert apollo_receiver.commandes_du_jour(cle) == 2
    monkeypatch.setattr(access, "current_org", lambda sub: _AUTRE_ORG)
    assert apollo_receiver.commandes_du_jour(cle) == 0, "une autre org a son propre compte"


def test_la_maintenance_releve_avant_de_purger(commanditaire, monkeypatch):
    from oto_mcp import apollo_releve, maintenance
    monkeypatch.setattr(apollo_releve, "_cle_plateforme", lambda: None)
    _commande("6013", payload={"credits_consumed": 8})
    out = maintenance.apollo_phones()
    assert out["releve"]["factures"] == 1 and "purged" in out
