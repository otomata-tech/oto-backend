"""La liste d'outils d'un travail hébergé, tenue par le SERVEUR sur chaque chemin d'appel
(`oto_mcp/outils_du_travail.py`).

Vécu le 11/10/2026 : un `oto_recipe op=test` lancé par un travail a appelé `fr_siret`, absent
de la liste du travail. La seule borne était celle du worker, qui filtre les outils servis au
modèle ; le jeton de délégation ouvrait le reste, et tout outil qui en appelle d'autres côté
serveur passait outre.

Sur une vraie base (`live`) : le travail est ENFILÉ, son jeton émis par le vrai chemin du
runner (`runner_jobs._delegue`), et le verrou posé depuis la ligne que rend la vraie
vérification du jeton. Chaque chemin est joué : appel direct (middleware de contexte),
`oto_call` (`executer_cible`), recette déclarative, recette `for_each`, échantillon, pont
d'une recette en code (lecture, tableaux).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-outils-du-travail"
LISTE = ["acme_people", "oto_call", "oto_recipe"]


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db
    from oto_mcp.org_store import members, orgs
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    o = orgs.create_org(f"Acme travail {uuid.uuid4().hex[:4]}", created_by=SUB)
    members.add_org_member(o, SUB, "org_admin")
    return o


def _travail(org: int, payload: dict) -> dict:
    from oto_mcp import db
    job = db.enqueue_job(org, "start", payload=payload, sub=SUB)
    return {"id": job["id"], "sub": SUB, "org_id": org}


@pytest.fixture
def verrou_de(org):
    """Pose, pour la durée du test, le verrou que porte le jeton d'un travail enfilé avec
    `payload` — émis et vérifié par les vrais chemins."""
    from oto_mcp import db, verrou_org
    from oto_mcp.capabilities import runner_jobs
    poses = []

    def poser(payload: dict) -> int:
        job = _travail(org, payload)
        jeton = runner_jobs._delegue(dict(job), bail_s=60, claimant="test")["delegated_token"]
        poses.append(verrou_org.poser(verrou_org.depuis_ligne(db.verify_api_token(jeton))))
        return job["id"]
    yield poser
    for t in reversed(poses):
        verrou_org.lever(t)


@pytest.fixture
def travail(verrou_de):
    """Un travail dont la liste est `LISTE`, et sa procédure (d'où `oto_procedure`)."""
    return verrou_de({"tools": list(LISTE), "procedure": "p", "input": "x"})


@pytest.fixture
def serveur(org, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, call_axes
    from oto_mcp.auth import hooks
    from oto_mcp.tools import datastore as outils_tableaux
    from oto_mcp.tools.lecture import LECTURE
    for mod in (access, hooks, call_axes):
        monkeypatch.setattr(mod, "current_user_sub_from_token", lambda: SUB, raising=False)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    appels: list[str] = []
    m = FastMCP("t-outils-du-travail")

    @m.tool(annotations=LECTURE)
    def acme_people(company: str = "") -> dict:
        appels.append("acme_people")
        return {"content": [{"name": f"{company} {i}"} for i in range(2)]}

    @m.tool(annotations=LECTURE)
    def acme_siret(siret: str = "") -> dict:
        appels.append("acme_siret")
        return {"content": [{"siret": siret}]}
    outils_tableaux.register(m)
    return m, appels


def _code(exc) -> str:
    data = getattr(getattr(exc, "error", None), "data", None) or {}
    return data.get("code")


# ── La liste elle-même ──────────────────────────────────────────────────────────

def test_la_liste_servie_est_celle_que_le_serveur_tient(travail):
    from oto_mcp import outils_du_travail as odt
    for permis in (*LISTE, "oto_procedure", "run_start", "run_finish"):
        assert odt.refus(permis) is None, permis
    assert "not in this hosted agent's tool list" in odt.refus("acme_siret")


def test_hors_travail_rien_ne_change(org):
    from oto_mcp import outils_du_travail as odt
    assert odt.refus("acme_siret") is None


def test_une_liste_qui_n_en_est_pas_une_ne_permet_que_l_hote(verrou_de):
    from oto_mcp import outils_du_travail as odt
    verrou_de({"tools": "acme_people", "input": "x"})
    assert odt.refus("acme_people") is not None
    assert odt.refus("run_start") is None


def test_un_travail_sans_liste_ne_permet_que_l_hote(verrou_de):
    from oto_mcp import outils_du_travail as odt
    verrou_de({"input": "x"})
    assert odt.refus("acme_people") is not None and odt.refus("oto_call") is not None
    assert odt.refus("run_finish") is None


def test_un_travail_introuvable_ne_permet_que_l_hote(org):
    from oto_mcp import outils_du_travail as odt
    from oto_mcp import verrou_org
    t = verrou_org.poser(verrou_org.Verrou(sub=SUB, org_id=org, job_id=10 ** 12))
    try:
        assert odt.refus("acme_people") is not None
        assert odt.refus("run_start") is None
    finally:
        verrou_org.lever(t)


def test_la_liste_tient_meme_quand_le_verrou_d_org_est_coupe(travail, monkeypatch):
    from oto_mcp import outils_du_travail as odt
    monkeypatch.setenv("OTO_VERROU_ORG_DELEGATION", "off")
    assert odt.refus("acme_siret") is not None


# ── Appel direct : le middleware de contexte ────────────────────────────────────

def _direct(name: str, appels: list):
    from oto_mcp.middleware.call_context import CallContextMiddleware

    class _Msg:
        pass

    class _Ctx:
        pass
    msg, ctx = _Msg(), _Ctx()
    msg.name, msg.arguments = name, {}
    ctx.message = msg

    async def _next(c):
        appels.append(c.message.name)
        return "servi"
    return asyncio.run(CallContextMiddleware().on_call_tool(ctx, _next))


def test_un_appel_direct_hors_liste_est_refuse_avant_le_handler(travail):
    from oto_mcp.mcp_errors import McpError
    appels: list = []
    with pytest.raises(McpError) as e:
        _direct("acme_siret", appels)
    assert _code(e.value) == "tool_not_in_job" and appels == []


def test_un_appel_direct_de_la_liste_passe(travail, monkeypatch):
    from oto_mcp.connectors import access_level_gate, activation_gate

    async def _rien(*a, **k):
        return None
    monkeypatch.setattr(activation_gate, "require_active", _rien)
    monkeypatch.setattr(access_level_gate, "require_covered", _rien)
    appels: list = []
    assert _direct("acme_people", appels) == "servi" and appels == ["acme_people"]


def test_une_cascade_appelee_en_direct_couvre_ses_etapes_le_temps_de_son_appel(verrou_de,
                                                                            monkeypatch):
    """Le bord pose l'outil APPELÉ avant le handler (`verrou_org.poser_outil`) : c'est ce
    que lit l'accord d'une cascade à ses étapes (`outils_du_travail.ouvrir_composite`),
    sur le vrai jeton du travail et sa vraie charge. Pendant l'appel de `waterfall_person`,
    son étape `lemlist_database` passe ; hors de lui, elle reste refusée."""
    from oto_mcp import outils_du_travail as odt
    from oto_mcp.connectors import access_level_gate, activation_gate
    from oto_mcp.middleware.call_context import CallContextMiddleware

    async def _rien(*a, **k):
        return None
    monkeypatch.setattr(activation_gate, "require_active", _rien)
    monkeypatch.setattr(access_level_gate, "require_covered", _rien)
    verrou_de({"tools": ["waterfall_person"], "input": "x"})

    class _Msg:
        name, arguments = "waterfall_person", {}

    class _Ctx:
        message = _Msg()
    vu: dict = {}

    async def _handler(c):
        with odt.ouvrir_composite(("waterfall_person",), {"lemlist_database"}):
            vu["etape"] = await asyncio.to_thread(odt.refus, "lemlist_database")
            vu["autre"] = await asyncio.to_thread(odt.refus, "folk_record")
        return "servi"
    assert asyncio.run(CallContextMiddleware().on_call_tool(_Ctx(), _handler)) \
        == "servi"
    assert vu["etape"] is None and vu["autre"] is not None
    assert odt.refus("lemlist_database") is not None


# ── `oto_call` : `executer_cible` ───────────────────────────────────────────────

def _cible(m, name: str, args: dict):
    from oto_mcp.tools import meta

    async def _go():
        tool = await meta.resoudre_outil(m, name)
        return await meta.executer_cible(tool, SUB, name, name, dict(args))
    return asyncio.run(_go())


def test_oto_call_vers_un_outil_hors_liste_est_refuse(travail, serveur):
    from oto_mcp.mcp_errors import McpError
    m, appels = serveur
    with pytest.raises(McpError) as e:
        _cible(m, "acme_siret", {"siret": "1"})
    assert _code(e.value) == "tool_not_in_job" and appels == []


def test_oto_call_vers_un_outil_de_la_liste_passe(travail, serveur):
    m, appels = serveur
    assert _cible(m, "acme_people", {"company": "acme"}).ok and appels == ["acme_people"]


def test_oto_call_hors_travail_passe_comme_avant(serveur):
    m, appels = serveur
    assert _cible(m, "acme_siret", {"siret": "1"}).ok and appels == ["acme_siret"]


# ── Recette déclarative, `for_each`, échantillon ───────────────────────────────

def _recette(m, corps: dict):
    from oto_mcp.recipes import moteur
    return asyncio.run(moteur.executer(corps, {}, fastmcp=m, sub=SUB, ecrire=False))


def test_une_recette_hors_liste_est_refusee_avant_tout_effet(travail, serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    with pytest.raises(moteur.RecetteRefusee) as e:
        _recette(m, {"tool": "acme_siret", "key": {"column": "siret"}, "map": {}})
    assert e.value.code == "tool_not_in_job" and appels == []


def test_une_recette_for_each_hors_liste_est_refusee_avant_tout_effet(travail, serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    corps = {"tool": "acme_siret", "key": {"column": "siret"}, "map": {},
             "arguments": {"siret": "{{row.siret}}"},
             "for_each": {"datastore": 10 ** 9, "status_column": "etat"}}
    with pytest.raises(moteur.RecetteRefusee) as e:
        _recette(m, corps)
    assert e.value.code == "tool_not_in_job" and appels == []


def test_l_echantillon_d_une_recette_hors_liste_est_refuse(travail, serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    with pytest.raises(moteur.RecetteRefusee) as e:
        asyncio.run(moteur.echantillon(m, SUB, "acme_siret", {"siret": "1"}))
    assert e.value.code == "tool_not_in_job" and appels == []


def test_l_echantillon_d_un_outil_de_la_liste_passe(travail, serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    forme = asyncio.run(moteur.echantillon(m, SUB, "acme_people", {"company": "a"},
                                           items="content"))
    assert forme["items_found"] and appels == ["acme_people"]


# ── Recette en code : le pont ───────────────────────────────────────────────────

def test_le_pont_en_lecture_refuse_un_outil_hors_liste(travail, serveur):
    from oto_mcp.recipes import pont
    m, appels = serveur
    reponse = asyncio.run(pont.appeleur_en_lecture(m, SUB)("acme_siret", {"siret": "1"}))
    assert reponse["ok"] is False and reponse["code"] == "tool_not_in_job"
    assert appels == []
    lu = asyncio.run(pont.appeleur_en_lecture(m, SUB)("acme_people", {"company": "a"}))
    assert lu["ok"] and appels == ["acme_people"]


def test_le_pont_d_une_recette_refuse_un_tableau_hors_liste(travail, serveur):
    """`data_rows` / `data_write` ne passent pas par le jugement d'une recette : c'est la
    garde de chaque appel (`executer_cible`) qui les arrête, et le pont garde le code."""
    from oto_mcp.recipes import pont
    m, _ = serveur
    appeler = pont.appeleur_de_recette(m, SUB, datastore="t", ecrire=True)
    lu = asyncio.run(appeler("data_rows", {"datastore": "t"}))
    assert lu["ok"] is False and lu["code"] == "tool_not_in_job"
    ecrit = asyncio.run(appeler("data_write", {"rows": [{"k": "a"}], "key": "k"}))
    assert ecrit["ok"] is False and ecrit["code"] == "tool_not_in_job"
