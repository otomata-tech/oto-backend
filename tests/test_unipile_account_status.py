"""`linkedin_account` : le nom promettait l'état du compte, l'outil servait
l'ardoise premium (signal #452, org 2, 14/08/2026).

Vécu en prod : un agent qui veut vérifier « mon LinkedIn est-il connecté ? » tombe
sur ce nom, invente `op='status'`, se prend un `invalid_arguments` et conclut « pas
connecté » — alors que le canal l'était (appel **248959**, args `{op:'status'}`, un
utilisateur qui signale « ça ne marche pas »). Relevé le 28/08 : cet outil n'a que
**trois** appels dans tout `tool_calls`, et les trois ont échoué.

Le signal offrait deux sorties : renommer, ou faire exister `op='status'`. On fait
exister l'op — c'est ce que le nom promet, et un renommage casserait les appels qui
marchent (ainsi que les procédures qui citent le nom, cf. `docs/doctrines.md`).

Contrainte NON négociable : `op='status'` doit répondre `connected:false` quand rien
n'est lié. S'il LEVAIT dans ce cas (ce que fait `unipile_client()`, par refus de
fallback anti-usurpation), on aurait remplacé un faux négatif par une erreur —
c'est-à-dire rien changé.
"""
import asyncio

import pytest
from mcp.shared.exceptions import McpError


def _tool(name: str):
    from fastmcp import FastMCP

    from oto_mcp.tools import unipile as U
    m = FastMCP("t")
    U.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def wired(monkeypatch):
    """Compte lié, clé résolue, sonde de liveness contrôlable. Aucun accès DB."""
    from oto_mcp import access, db
    from oto_mcp.access import ResolvedCredential
    from oto_mcp.connectors import identities as connector_identities
    from oto_mcp.connectors import readiness as connector_readiness

    state = {"accounts": [{"provider": "LINKEDIN", "account_id": "acc_1",
                           "account_name": "Julien Frèche", "org_id": 196,
                           "platform_seat": True,
                           "connected_at": "2026-08-22 16:56:33"}],
             "alive": True}

    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(access, "current_org", lambda sub: 196)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "project_pinned_identity", lambda name: None)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda sub: list(state["accounts"]))
    monkeypatch.setattr(db, "get_unipile_account_id",
                        lambda sub, org, prov: next(
                            (a["account_id"] for a in state["accounts"]
                             if a["provider"] == prov and a["org_id"] == org), None))
    monkeypatch.setattr(db, "get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr(db, "granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr(access, "personal_instance_org",
                        lambda sub, name, exclude_org=None: None)
    monkeypatch.setattr(access, "resolve_credential",
                        lambda prov, want="auto", sub=None, **k: ResolvedCredential(
                            "unipile", "KEY", True, "platform", None, None))
    # Le geste manquant vient du seam PARTAGÉ, pas d'une prose locale à ce tool.
    monkeypatch.setattr(connector_readiness, "diagnose",
                        lambda sub, name, *, org, group: None)
    assert connector_identities.supports("unipile")   # le backend d'identités existe

    class _Cli:
        def __init__(self, **kw):
            pass

        def account_alive(self, account_id):
            return state["alive"]

    import oto.tools.unipile as core
    monkeypatch.setattr(core, "make_unipile_client", lambda **kw: _Cli())
    return state


def test_op_status_existe_et_dit_le_compte_lie(wired):
    """L'op que l'agent inventait existe, et rend ce que le NOM promet."""
    out = _tool("linkedin_account")(op="status")
    assert out["connected"] is True
    assert out["account_id"] == "acc_1"
    assert out["account_name"] == "Julien Frèche"
    assert out["channel"] == "LINKEDIN"


def test_op_status_sans_compte_repond_faux_au_lieu_de_lever(wired):
    """Le mode de panne de #452, retourné : sans compte lié, `op='status'` doit
    RÉPONDRE. Lever renverrait l'agent à la même conclusion fausse qu'avant."""
    wired["accounts"] = []
    out = _tool("linkedin_account")(op="status")
    assert out["connected"] is False
    assert out["account_id"] is None
    assert out["next_step"]                  # le geste, nommé


def test_op_status_rapporte_une_session_morte(wired):
    """Le compte est lié ET la session peut être morte (checkpoint, cookie tourné —
    #236). « Lié » n'est pas « vivant » : la carte doit distinguer les deux, sinon
    elle rassure exactement comme l'état qu'on répare."""
    wired["alive"] = False
    out = _tool("linkedin_account")(op="status")
    assert out["connected"] is True and out["alive"] is False
    assert out["next_step"]


def test_sonde_indisponible_nest_pas_une_session_morte(wired, monkeypatch):
    """`alive=None` = « je n'ai pas pu regarder », distinct de `False` = « c'est
    mort ». Les confondre ferait annoncer une panne jamais constatée."""
    import oto.tools.unipile as core

    class _Boom:
        def __init__(self, **kw):
            pass

        def account_alive(self, account_id):
            raise RuntimeError("amont indisponible")

    monkeypatch.setattr(core, "make_unipile_client", lambda **kw: _Boom())
    out = _tool("linkedin_account")(op="status")
    assert out["connected"] is True and out["alive"] is None
    assert "next_step" not in out


def test_op_inconnue_leve_toujours(wired):
    with pytest.raises(McpError):
        _tool("linkedin_account")(op="wat")
