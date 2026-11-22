"""Le compte de chaque connecteur d'une cascade (`waterfall/comptes.py`) — verrouille :
avec deux comptes d'un même connecteur (deux espaces lemlist), `_accounts={"lemlist": …}`
fait passer CHAQUE appel des étapes de ce connecteur (recherche, enrichissement, relève)
sous le compte nommé, et celui-là seul ; rien de nommé = les appels d'avant, à l'octet ;
un connecteur qui n'est pas une étape, ou un compte qu'il n'a pas, est refusé AVANT tout
appel, `dry_run` compris ; un `_account` simple ne vaut que sur UN connecteur ; et les
deux axes d'une cascade ne posent jamais le compte de l'appel (`_CALL_ACCOUNT`), que
chaque étape lirait."""
import asyncio
from contextlib import contextmanager

import pytest
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp import access, call_axes, session_org
from oto_mcp.mcp_errors import McpError
from oto_mcp.middleware.call_context import CallContextMiddleware
from oto_mcp.tools.meta import CODE_COMPTE_A_NOMMER, IssueCible
from oto_mcp.waterfall import appel

from _waterfall_faux import _made, _run, calls  # noqa: F401 — la fixture des fournisseurs

LI = "https://www.linkedin.com/in/jane-doe/"


@contextmanager
def _nomme(accounts=None, account=None):
    """Les axes tels que leur pose les laisse (`call_axes`), rendus en sortie — posés
    ici, dans le contexte du test, que `_run` (asyncio.run) copie dans sa boucle."""
    undo = []
    if accounts is not None:
        undo.append((session_org.reset_call_step_accounts, session_org.set_call_step_accounts(
            call_axes.step_accounts_of(accounts))))
    if account is not None:
        undo.append((session_org.reset_call_cascade_account,
                     session_org.set_call_cascade_account(account)))
    try:
        yield
    finally:
        for reset, tok in reversed(undo):
            reset(tok)


@pytest.fixture
def coffre(monkeypatch):
    """Les comptes lemlist de l'appelant : deux espaces. Un autre nom n'existe pas."""
    vus = []

    def resoudre(provider, want="auto", sub=None, *, account=None, **kw):
        vus.append((provider, account, kw))
        assert session_org.current_call_trace() == {}, "vérifié sous un relevé jetable"
        if account not in ("espace-a", "espace-b"):
            raise access.CompteIntrouvable(ErrorData(
                code=INVALID_PARAMS, message=f"Workspace `{account}` not found"), account)
        return object()

    monkeypatch.setattr(access, "resolve_credential", resoudre)
    return vus


def _lemlist_sur_cle_perso(calls):
    calls["modes"] = {"lemlist_database": "user", "lemlist_enrich": "user"}


def _args_de(calls, tool):
    return [a for n, a in calls["made"] if n == tool]


# --- le compte nommé sert, et lui seul ---------------------------------------------

def test_le_compte_nomme_sert_chaque_etape_lemlist_et_elles_seules(calls, coffre):
    _lemlist_sur_cle_perso(calls)
    with _nomme(accounts={"lemlist": "espace-b"}):
        out = _run("waterfall_person", linkedin_url=LI)
    assert "lemlist_database" in _made(calls) and "lemlist_enrich" in _made(calls)
    for tool in ("lemlist_database", "lemlist_enrich"):
        assert all(a.get("_account") == "espace-b" for a in _args_de(calls, tool)), tool
    autres = [(n, a) for n, a in calls["made"] if not n.startswith("lemlist_")]
    assert autres and not any("_account" in a for _, a in autres)
    par_etape = {a["provider"]: a for a in out["attempts"]}
    assert par_etape["lemlist_db"]["account"] == "espace-b"
    assert par_etape["lemlist"]["account"] == "espace-b"
    assert "account" not in par_etape["aiark"]
    # Vérifié une fois, avant tout appel, sans toucher au règlement de l'appel.
    assert coffre == [("lemlist", "espace-b",
                       {"emit_on_failure": False, "check_usage": False})]


def test_le_nom_du_connecteur_vaut_pour_toutes_ses_etapes_quelle_que_soit_leur_cle(calls,
                                                                                   coffre):
    """`waterfall_company` : `lemlist` puis la complétion `lemlist_linkedin`, même compte."""
    calls["modes"] = {"lemlist_database": "user"}
    with _nomme(accounts={"Lemlist": "espace-a"}):
        _run("waterfall_company", domain="acme.com", providers=["lemlist", "lemlist_linkedin"])
    lemlist = _args_de(calls, "lemlist_database")
    assert lemlist and all(a["_account"] == "espace-a" for a in lemlist)


def test_la_releve_d_une_etape_en_attente_porte_le_compte(calls, coffre, monkeypatch):
    """Soumise sous un compte, relevée sous le même — maintenant et plus tard."""
    monkeypatch.setattr(appel, "POLL_BUDGET_S", 0)
    calls["modes"] = {"lemlist_enrich": "user"}
    calls["answers"]["lemlist_enrich"] = IssueCible(ok=True, result={"enrichment_id": "e1"})
    calls["answers"]["lemlist_enrich_result"] = IssueCible(
        ok=True, result={"results": [{"status": "in-progress", "done": False}]})
    with _nomme(accounts={"lemlist": "espace-b"}):
        out = _run("waterfall_phone", linkedin_url=LI, providers=["lemlist"])
    assert all(a["_account"] == "espace-b" for _, a in calls["made"])
    assert out["pending"] == [{"provider": "lemlist", "tool": "lemlist_enrich_result",
                               "arguments": {"enrichment_id": "e1",
                                             "_account": "espace-b"}}]


# --- rien de nommé : rien ne change ------------------------------------------------

def test_sans_compte_nomme_les_appels_sont_ceux_d_avant(calls, monkeypatch):
    def interdit(*a, **k):
        raise AssertionError("rien de nommé : le coffre n'est pas lu pour les comptes")

    monkeypatch.setattr(access, "resolve_credential", interdit)
    _lemlist_sur_cle_perso(calls)
    out = _run("waterfall_person", linkedin_url=LI)
    assert calls["made"] and not any("_account" in a for _, a in calls["made"])
    assert not any("account" in a for a in out["attempts"])


def test_deux_comptes_sans_nom_l_echec_de_l_etape_dit_comment_nommer(calls):
    """Le défaut d'avant (l'étape échoue, la cascade passe la main) — mais le refus
    nomme le jeton de la cascade, pas celui d'un appel direct."""
    calls["modes"] = {"lemlist_database": "user"}
    calls["answers"]["lemlist_database"] = IssueCible(
        ok=False, code=CODE_COMPTE_A_NOMMER,
        message="Multiple workspaces `lemlist` in this org (`espace-a`, `espace-b`), with "
                "no single default — pass `_account=\"<name>\"` on this call, or set a "
                "default with oto_identity(op='set', connector='lemlist', "
                "identity_id='<name>').")
    out = _run("waterfall_person", linkedin_url=LI, providers=["lemlist_db", "aiark"])
    lemlist = next(a for a in out["attempts"] if a["provider"] == "lemlist_db")
    assert lemlist["status"] == "failed" and lemlist["code"] == CODE_COMPTE_A_NOMMER
    assert '_accounts={"lemlist": "<name>"}' in lemlist["message"]
    assert "`_account=" not in lemlist["message"]
    assert "linkedin_aiark_person" in _made(calls)      # la cascade continue, comme avant


# --- refusé avant tout appel -------------------------------------------------------

@pytest.mark.parametrize("dry_run", [False, True])
def test_un_compte_que_le_connecteur_n_a_pas_est_refuse_avant_tout_appel(calls, coffre,
                                                                         dry_run):
    _lemlist_sur_cle_perso(calls)
    with _nomme(accounts={"lemlist": "espace-z"}), pytest.raises(McpError) as e:
        _run("waterfall_person", linkedin_url=LI, dry_run=dry_run)
    assert e.value.error.data["code"] == "account_not_found"
    assert "`espace-z`" in e.value.error.message and "`lemlist`" in e.value.error.message
    assert "oto_identity" in e.value.error.message
    assert calls["made"] == []


def test_un_connecteur_qui_n_est_pas_une_etape_est_refuse_en_nommant_les_bons(calls,
                                                                             coffre):
    with _nomme(accounts={"hubspot": "x"}), pytest.raises(McpError) as e:
        _run("waterfall_person", linkedin_url=LI)
    msg = e.value.error.message
    assert "hubspot" in msg and "lemlist" in msg and "apollo" in msg
    assert "sirene" not in msg      # l'open data ne prend aucun compte
    assert calls["made"] == [] and coffre == []


def test_la_forme_de_accounts_est_gardee_a_la_pose():
    for mauvais in (["lemlist"], {"lemlist": ""}, {"": "x"}, {"lemlist": 3}, "lemlist"):
        with pytest.raises(McpError):
            asyncio.run(call_axes.STEP_ACCOUNTS.pin_for(mauvais, "waterfall_person"))


def test_un_compte_disparu_entre_temps_arrete_la_cascade(calls, coffre):
    """Le filet : le connecteur dit « introuvable » à l'appel — rien d'autre n'est payé."""
    _lemlist_sur_cle_perso(calls)
    calls["answers"]["lemlist_database"] = IssueCible(
        ok=False, code=CODE_COMPTE_A_NOMMER, message="Workspace `espace-b` not found")
    with _nomme(accounts={"lemlist": "espace-b"}):
        out = _run("waterfall_person", linkedin_url=LI,
                   providers=["lemlist_db", "aiark", "apollo"])
    assert _made(calls) == ["lemlist_database"]
    assert out["found"] is False
    assert out["attempts"][-1]["code"] == CODE_COMPTE_A_NOMMER


# --- `_account` simple : sur UN connecteur seulement --------------------------------

def test_account_simple_vaut_quand_les_etapes_n_ont_qu_un_connecteur(calls, coffre):
    _lemlist_sur_cle_perso(calls)
    with _nomme(account="espace-a"):
        _run("waterfall_person", linkedin_url=LI, providers=["lemlist_db", "lemlist"])
    assert _made(calls)[:1] == ["lemlist_database"]
    assert all(a["_account"] == "espace-a" for _, a in calls["made"])


def test_account_simple_sur_plusieurs_connecteurs_est_refuse_en_nommant_accounts(calls,
                                                                                 coffre):
    with _nomme(account="espace-a"), pytest.raises(McpError) as e:
        _run("waterfall_person", linkedin_url=LI)
    assert "_accounts" in e.value.error.message and "ambiguous" in e.value.error.message
    assert calls["made"] == [] and coffre == []


def test_account_et_accounts_ensemble_sont_refuses(calls, coffre):
    with _nomme(accounts={"lemlist": "espace-a"}, account="espace-a"), \
            pytest.raises(McpError):
        _run("waterfall_person", linkedin_url=LI)
    assert calls["made"] == []


# --- les axes d'une cascade --------------------------------------------------------

def test_les_axes_de_cascade_ne_s_annoncent_que_sur_les_cascades():
    for name in ("waterfall_person", "waterfall_email_act", "waterfall_people"):
        params = [a.param for a in call_axes.axes_for_call(name)]
        assert "_accounts" in params and params.count("_account") == 1, name
        assert call_axes.CASCADE_ACCOUNT in call_axes.axes_for_call(name)
        assert call_axes.ACCOUNT not in call_axes.axes_for_call(name)
    for name in ("lemlist_database", "serper_search", "oto_whoami"):
        assert "_accounts" not in [a.param for a in call_axes.axes_for(name)], name
    # Une étape lemlist lit `_account` comme tout appel (`meta.executer_cible`).
    assert call_axes.ACCOUNT in call_axes.axes_for_call("lemlist_database")


class _Msg:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, msg):
        self.message = msg


@pytest.mark.asyncio
async def test_le_middleware_pose_les_comptes_de_cascade_jamais_le_compte_de_l_appel():
    mw = CallContextMiddleware()
    vu = {}

    async def _next(ctx):
        vu["args"] = dict(ctx.message.arguments)
        vu["compte_de_l_appel"] = session_org.current_call_account()
        vu["par_connecteur"] = session_org.current_call_step_accounts()
        vu["seul"] = session_org.current_call_cascade_account()
        return "ok"

    msg = _Msg("waterfall_person", {"linkedin_url": LI,
                                    "_accounts": {"lemlist": "espace-b"}})
    assert await mw.on_call_tool(_Ctx(msg), _next) == "ok"
    assert vu == {"args": {"linkedin_url": LI}, "compte_de_l_appel": None,
                  "par_connecteur": {"lemlist": "espace-b"}, "seul": None}
    msg = _Msg("waterfall_person", {"linkedin_url": LI, "_account": "espace-a"})
    assert await mw.on_call_tool(_Ctx(msg), _next) == "ok"
    assert vu["args"] == {"linkedin_url": LI}
    assert vu["compte_de_l_appel"] is None and vu["seul"] == "espace-a"
    assert session_org.current_call_step_accounts() is None
    assert session_org.current_call_cascade_account() is None


# --- la vérification, sur le VRAI résolveur (coffre simulé) --------------------------

class _Lemlist:
    """Connecteur multi-compte org-partageable, comme lemlist (clé d'API)."""
    auth_multi_account = True
    auth_modes = ("byo", "byo_org")
    personal_cross_org = False
    name = "lemlist"


@pytest.fixture
def org_a_deux_espaces(monkeypatch):
    """L'org tient deux espaces lemlist, la personne aucun (harnais de
    `tests/test_reprise_399_review.py`) — c'est `resolve_credential` qui tranche."""
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.providers, "connector_for_provider", lambda p: _Lemlist())
    monkeypatch.setattr(access.providers, "is_byo_user", lambda p: True)
    monkeypatch.setattr(access, "project_pinned_identity", lambda prov, project_id=None: None)
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", {"lemlist"})
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    rows = [{"account": n, "meta": {}} for n in ("espace-a", "espace-b")]
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, con: rows if et == "org" else [])
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda oid, prov, account="": {"espace-a": "K-A",
                                                       "espace-b": "K-B"}.get(account))


def test_le_vrai_resolveur_refuse_le_compte_absent_et_laisse_passer_l_existant(
        org_a_deux_espaces):
    from oto_mcp.waterfall import comptes
    # Le harnais sert bien l'espace nommé — le passage n'est pas un refus avalé.
    assert access.resolve_credential("lemlist", sub="u1", account="espace-b",
                                     emit_on_failure=False, check_usage=False).key == "K-B"
    comptes._verifier("u1", "lemlist", "espace-b")          # existe : rien à dire
    with pytest.raises(McpError) as e:
        comptes._verifier("u1", "lemlist", "espace-z")
    assert e.value.error.data["code"] == "account_not_found"
    assert "`espace-z`" in e.value.error.message
    assert session_org.current_call_trace() is None         # rien laissé sur l'appel
