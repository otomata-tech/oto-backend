"""`linkedin_unipile_account` : le nom promettait l'état du compte, l'outil servait
l'ardoise premium (signal #452, org 2, 14/08/2026).

Vécu en prod : un agent qui veut vérifier « mon LinkedIn est-il connecté ? » tombe
sur ce nom, invente `op='status'`, se prend un `invalid_arguments` et conclut « pas
connecté » — alors que le canal l'était (appel **248959**, args `{op:'status'}`, un
utilisateur qui signale « ça ne marche pas »). Relevé le 28/08 : cet outil n'a que
**trois** appels dans tout `tool_calls`, et les trois ont échoué.

Le signal offrait deux sorties : renommer, ou faire exister `op='status'`. On fait
exister l'op — c'est ce que le nom promet, et un renommage casserait les appels qui
marchent (ainsi que les procédures qui citent le nom, cf. `docs/guides.md`).

Contrainte NON négociable : `op='status'` doit répondre `connected:false` quand rien
n'est lié. S'il LEVAIT dans ce cas (ce que fait `unipile_client()`, par refus de
fallback anti-usurpation), on aurait remplacé un faux négatif par une erreur —
c'est-à-dire rien changé.
"""
import asyncio

import pytest
from oto_mcp.mcp_errors import McpError
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
                           "account_name": "Jane Doe", "org_id": 196,
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
    # La réconciliation (poll-and-bind) est stubée : rien à lier par défaut. Un test
    # la remplace par `state["reconcile"]` pour simuler un compte fraîchement connecté.
    from oto_mcp import unipile_connect
    state["reconciled"] = []

    def _reconcile(sub, account_id=None):
        state["reconciled"].append(sub)
        state.setdefault("hints", []).append(account_id)
        return state.get("reconcile", lambda s: {"bound": False, "accounts": [],
                                                  "reason": "no_pending",
                                                  "detail": "rien en attente"})(sub)
    monkeypatch.setattr(unipile_connect, "reconcile_pending", _reconcile)
    # #1342 : les comptes propres liés dans une AUTRE org, adoptables ici (aucun par défaut).
    from oto_mcp.tools import unipile as U
    state["elsewhere"] = {}
    monkeypatch.setattr(U, "status_for",
                        lambda sub, **k: {"elsewhere": dict(state["elsewhere"])})
    return state


def test_op_status_existe_et_dit_le_compte_lie(wired):
    """L'op que l'agent inventait existe, et rend ce que le NOM promet."""
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is True
    assert out["account_id"] == "acc_1"
    assert out["account_name"] == "Jane Doe"
    assert out["channel"] == "LINKEDIN"


def test_op_status_sans_compte_repond_faux_au_lieu_de_lever(wired):
    """Le mode de panne de #452, retourné : sans compte lié, `op='status'` doit
    RÉPONDRE. Lever renverrait l'agent à la même conclusion fausse qu'avant."""
    wired["accounts"] = []
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is False
    assert out["account_id"] is None
    assert out["next_step"]                  # le geste, nommé


def test_op_status_nomme_le_compte_lie_dans_une_autre_org_et_sa_reprise(wired):
    """#1342 : le compte existe, dans une autre org de la personne — le statut le
    nomme avec le geste qui l'adopte ici, jamais « connecte ton compte »."""
    wired["accounts"] = []
    wired["elsewhere"] = {"linkedin": {"account_id": "acc_9", "account_name": "Jane Doe",
                                       "org_id": 12}}
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is False
    assert out["elsewhere"]["account_id"] == "acc_9"
    assert "unipile_connect_start(channel='linkedin')" in out["next_step"]
    assert "org 12" in out["next_step"]


def test_op_status_rapporte_une_session_morte(wired):
    """Le compte est lié ET la session peut être morte (checkpoint, cookie tourné —
    #236). « Lié » n'est pas « vivant » : la carte doit distinguer les deux, sinon
    elle rassure exactement comme l'état qu'on répare."""
    wired["alive"] = False
    out = _tool("linkedin_unipile_account")(op="status")
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
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is True and out["alive"] is None
    assert "next_step" not in out


def test_op_inconnue_leve_toujours(wired):
    with pytest.raises(McpError):
        _tool("linkedin_unipile_account")(op="wat")


# ── La face agent LIE (plus de webhook depuis #581) ─────────────────────────
# Vécu org 270 (2026-09-03/14) : un onboarding lancé par `unipile_connect_start` ne
# pouvait aboutir que si la personne rouvrait sa carte dans un tableau de bord — la
# face agent ne réconciliait nulle part, et le tool promettait un webhook disparu.

def test_op_status_lie_le_compte_fraichement_connecte(wired):
    """`op='status'` réconcilie AVANT de résoudre : le compte que la personne vient de
    connecter est lié, et le statut le dit dans le même appel."""
    wired["accounts"] = []

    def _bind(sub):
        wired["accounts"].append({"provider": "LINKEDIN", "account_id": "acc_new",
                                  "account_name": "Tristan", "org_id": 196,
                                  "platform_seat": True, "connected_at": None})
        return {"bound": True, "accounts": [{"account_id": "acc_new"}]}
    wired["reconcile"] = _bind
    out = _tool("linkedin_unipile_account")(op="status")
    assert wired["reconciled"] == ["u1"]
    assert out["connected"] is True and out["account_id"] == "acc_new"
    assert "binding" not in out


def test_op_status_dit_pourquoi_rien_na_ete_lie(wired):
    """`no_candidate` était muet partout (log absent, motif jeté par la face REST) :
    quand rien n'est lié, le motif établi remonte à l'agent."""
    wired["accounts"] = []
    wired["reconcile"] = lambda sub: {
        "bound": False, "accounts": [], "reason": "no_candidate", "detail": "aucun éligible",
        "pendings": [{"nonce": "N", "provider": "LINKEDIN", "reason": "no_candidate",
                      "detail": "aucun éligible"}]}
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is False
    assert out["binding"] == {"reason": "no_candidate", "detail": "aucun éligible"}


def test_op_status_ne_raconte_pas_lechec_dun_autre_canal(wired):
    wired["accounts"] = []
    wired["reconcile"] = lambda sub: {
        "bound": False, "accounts": [], "reason": "no_candidate", "detail": "whatsapp",
        "pendings": [{"nonce": "W", "provider": "WHATSAPP", "reason": "no_candidate",
                      "detail": "whatsapp"}]}
    out = _tool("linkedin_unipile_account")(op="status")
    assert "binding" not in out


def test_op_status_dit_une_panne_sans_canal(wired):
    wired["accounts"] = []
    wired["reconcile"] = lambda sub: {"bound": False, "accounts": [],
                                      "reason": "provider_unreachable", "detail": "réessaie"}
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["binding"]["reason"] == "provider_unreachable"


def test_op_status_sans_pending_ne_bavarde_pas(wired):
    """Aucun lien demandé : pas de `binding` — ce n'est pas une panne à signaler."""
    wired["accounts"] = []
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is False and "binding" not in out


def test_une_reconciliation_qui_casse_ne_casse_pas_le_statut(wired):
    wired["reconcile"] = lambda sub: (_ for _ in ()).throw(RuntimeError("amont"))
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is True


# ── oto#247 : la face agent porte la preuve jusqu'à la réconciliation ────────
# Sans l'`account_id` du retour, la réconciliation refuse de choisir entre comptes
# connectés dans la même fenêtre sur la clé partagée. L'agent n'a pas de retour
# navigateur : il relaie ce que la personne lit dans l'adresse de sa page de retour.

def test_op_status_transmet_l_account_id_a_la_reconciliation(wired):
    _tool("linkedin_unipile_account")(op="status", account_id=" acc_du_retour ")
    assert wired["hints"] == ["acc_du_retour"]


def test_op_status_sans_account_id_ne_fabrique_pas_d_indice(wired):
    _tool("linkedin_unipile_account")(op="status")
    assert wired["hints"] == [None]


def test_op_status_rend_le_refus_ambigu_a_l_agent(wired):
    wired["accounts"] = []
    wired["reconcile"] = lambda sub: {
        "bound": False, "accounts": [], "reason": "ambiguous_candidates",
        "detail": "repasse account_id=",
        "pendings": [{"nonce": "N", "provider": "LINKEDIN",
                      "reason": "ambiguous_candidates", "detail": "repasse account_id="}]}
    out = _tool("linkedin_unipile_account")(op="status")
    assert out["connected"] is False
    assert out["binding"]["reason"] == "ambiguous_candidates"


def test_account_id_hors_status_est_refuse(wired):
    """Pas d'argument avalé en silence : hors `status`, il ne veut rien dire."""
    with pytest.raises(McpError, match="account_id"):
        _tool("linkedin_unipile_account")(op="contracts", account_id="acc_x")
