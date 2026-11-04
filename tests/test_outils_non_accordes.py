"""Un outil qu'une recette ou une procédure appelle, absent de la liste de l'agent :
dit AVANT le run (`missing_tools` sur l'agent), et refusé UNE fois au run en les
nommant tous (`tool_not_in_job`, `missing_tools`, `fix`).

Né d'une soirée où deux agents d'une même chaîne se sont bloqués chacun sur un outil
qu'une recette de sa procédure appelait — l'un une lecture de CRM, l'autre une lecture
de tableau —, découverts un par un, au run.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from oto_mcp import outils_du_travail
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.recipes import code, outils_cites

# Ce banc ne parle pas de l'existence de la procédure d'un agent (#1499, conftest).
pytestmark = pytest.mark.usefixtures("procedures_connues")

_CORPS_CODE = {"kind": "code", "sources": {"recipe.py": (
    "async def run(oto, params):\n"
    "    rows = await oto.call('data_rows', {'datastore': 1})\n"
    "    res = await oto.call(\"folk_record\", {'op': 'search'})\n"
    "    again = await oto.call('data_rows', {'datastore': 2})\n"
    "    name = 'serper_' + 'search'\n"
    "    await oto.call(name, {})\n")}}


# ── ce qu'une recette cite ───────────────────────────────────────────────────

def test_une_recette_code_cite_ses_appels_litteraux_une_fois_chacun():
    assert outils_cites.outils_cites(_CORPS_CODE) == ["data_rows", "folk_record"]


def test_une_recette_declarative_cite_son_outil():
    """La forme que le moteur exécute : `tool` à la racine, `source` ne tient que la
    pagination (`moteur.py`)."""
    corps = {"tool": "lemlist_database", "arguments": {"op": "people"},
             "source": {"items": "results", "pagination": {"type": "page"}}}
    assert outils_cites.outils_cites(corps) == ["lemlist_database"]


def test_une_recette_declarative_ne_cite_pas_une_cle_qu_aucune_recette_ne_porte():
    assert outils_cites.outils_cites({"source": {"tool": "apollo_search_people"}}) == []


def test_un_corps_illisible_ne_cite_rien():
    assert outils_cites.outils_cites(None) == []
    assert outils_cites.outils_cites({"sources": {"a.py": 3}}) == []


def test_une_procedure_nomme_ses_recettes_seulement_si_elle_parle_d_oto_recipe():
    texte = ('<tool:oto_recipe> `op="run"`, `slug="recette-a"`, puis '
             '`slug="recette-b"` et encore `slug="recette-a"`')
    assert outils_cites.recettes_citees(texte) == ["recette-a", "recette-b"]
    assert outils_cites.recettes_citees('slug="x-y" sans recette') == []


# ── au run : un refus, tous les outils, le geste ─────────────────────────────

@pytest.fixture
def _travail(monkeypatch):
    verrou = types.SimpleNamespace(job_id=41)
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: verrou)
    monkeypatch.setattr(outils_du_travail.db, "charge_du_travail",
                        lambda job: {"tools": ["oto_recipe", "data_write"],
                                     "trigger_id": 7})


def test_manquants_nomme_chaque_outil_hors_liste_et_l_agent(_travail):
    m = outils_du_travail.manquants(["data_rows", "folk_record", "data_write", "run_finish",
                                     "data_rows"])
    assert m == {"job_id": 41, "trigger_id": 7, "tools": ["oto_recipe", "data_write"],
                 "missing": ["data_rows", "folk_record"]}


def test_manquants_lit_la_charge_du_travail_une_fois(monkeypatch):
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete",
                        lambda: types.SimpleNamespace(job_id=41))
    lectures = []
    monkeypatch.setattr(outils_du_travail.db, "charge_du_travail",
                        lambda job: lectures.append(job) or {"tools": [], "trigger_id": 7})
    assert outils_du_travail.manquants(["data_rows"])["missing"] == ["data_rows"]
    assert lectures == [41], "la liste permise et l'agent à compléter : une seule lecture"


def test_manquants_hors_travail_heberge_ne_dit_rien(monkeypatch):
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    assert outils_du_travail.manquants(["folk_record"]) is None


def test_la_recette_code_est_refusee_avant_tout_appel_en_nommant_tout(_travail, monkeypatch):
    async def jamais(**kw):
        raise AssertionError("le code ne doit pas tourner")
    monkeypatch.setattr(code.pont_bac, "executer_avec_pont", jamais)
    corps = {**_CORPS_CODE, "limits": {"max_calls": 5, "max_units": 1, "max_seconds": 10}}
    recu = asyncio.run(code.executer(corps, {}, fastmcp=None, sub="u", ecrire=True))
    assert recu["ok"] is False and recu["calls"] == 0 and recu["done"] is False
    assert recu["code"] == "tool_not_in_job"
    assert recu["missing_tools"] == ["data_rows", "folk_record"]
    assert recu["fix"] == {"tool": "oto_trigger", "op": "update", "trigger_id": 7,
                           "tools": ["oto_recipe", "data_write", "data_rows", "folk_record"]}
    assert "run_finish" in recu["error"] and "agent 7" in recu["error"]
    assert "fix" not in recu["error"], "l'agent s'arrête : élargir sa liste n'est pas son geste"


@pytest.fixture
def _agent_7(monkeypatch):
    """L'agent 7 de `porteur`, tel que le travail 41 l'a reçu. Rend ce que
    `update_trigger` a reçu."""
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
        "id": i, "org_id": o, "sub": "porteur", "tools": ["oto_recipe", "data_write"]})
    monkeypatch.setattr(RT.tool_alias, "canonical_names", lambda noms, sub: list(noms))
    vu: dict = {}
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs, **k: vu.update(champs) or {"id": i, **champs})
    return vu


def test_le_fix_joue_sur_la_capacite_donne_a_l_agent_la_liste_entiere(
        _travail, _agent_7, monkeypatch):
    """Le `fix` est un vrai geste : rejoué tel quel sur `oto_trigger`, il pose la liste
    de l'agent PLUS les manquants — par une personne. Sous le jeton du travail, le même
    geste est refusé (`tools_human_only`) : un agent n'élargit pas sa propre liste."""
    monkeypatch.setattr(code.pont_bac, "executer_avec_pont", None)
    corps = {**_CORPS_CODE, "limits": {"max_calls": 5, "max_units": 1, "max_seconds": 10}}
    fix = asyncio.run(code.executer(corps, {}, fastmcp=None, sub="u", ecrire=True))["fix"]
    arguments = {k: v for k, v in fix.items() if k != "tool"}
    ctx = ResolvedCtx(sub="porteur", org_id=2)

    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(ctx, RT.TriggerInput(**arguments)))
    assert (e.value.status, e.value.code) == (403, "tools_human_only")
    assert _agent_7 == {}, "rien n'est écrit sous le jeton du travail"

    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    asyncio.run(RT._triggers(ctx, RT.TriggerInput(**arguments)))
    assert _agent_7 == {"tools": ["oto_recipe", "data_write", "data_rows", "folk_record"]}


# ── avant le run : sur l'agent ───────────────────────────────────────────────

def test_l_agent_dit_ce_que_sa_procedure_et_ses_recettes_appellent_sans_l_avoir(monkeypatch):
    corps_proc = ('Lire <tool:data_aggregate>, puis <tool:oto_recipe> `op="run"`, '
                  '`slug="recette-a"`, et `slug="pas-une-recette"`.')
    monkeypatch.setattr(RT.db, "get_guide_db", lambda *a: {"body_md": corps_proc})
    fiches = {"recette-a": {"id": 3, "published_version": 2}}
    monkeypatch.setattr(RT.db_recipes, "get_recipe_or_platform",
                        lambda ot, oid, slug: fiches.get(slug))
    monkeypatch.setattr(RT.db_recipes, "get_version",
                        lambda rid, v: {"body": _CORPS_CODE} if (rid, v) == (3, 2) else None)
    t = {"procedure": "veille", "tools": ["oto_recipe", "data_rows", "run_finish"]}
    assert RT._outils_non_accordes(77, t) == [
        ("data_aggregate", "procedure `veille`"), ("folk_record", "recipe `recette-a`")]


def test_un_agent_sans_procedure_n_a_rien_a_ajouter():
    assert RT._outils_non_accordes(77, {"tools": ["x"]}) == []


def test_une_recette_non_publiee_n_est_pas_lue(monkeypatch):
    monkeypatch.setattr(RT.db, "get_guide_db",
                        lambda *a: {"body_md": 'oto_recipe slug="brouillon"'})
    monkeypatch.setattr(RT.db_recipes, "get_recipe_or_platform",
                        lambda *a: {"id": 1, "published_version": None})
    monkeypatch.setattr(RT.db_recipes, "get_version",
                        lambda *a: pytest.fail("une version non publiée ne se lit pas"))
    assert RT._outils_non_accordes(77, {"procedure": "p", "tools": []}) == []


def _procedure_et_recette(monkeypatch):
    """Une procédure qui cite un outil et lance une recette publiée ; rend les lectures."""
    lus: dict[str, list] = {"procedures": [], "recettes": []}
    monkeypatch.setattr(RT.db, "get_guide_db", lambda *a: lus["procedures"].append(a[2]) or {
        "body_md": 'Lire <tool:data_aggregate>, puis <tool:oto_recipe> `slug="recette-a"`.'})
    monkeypatch.setattr(RT.db_recipes, "get_recipe_or_platform",
                        lambda ot, oid, slug: lus["recettes"].append(slug) or {
                            "id": 3, "published_version": 2})
    monkeypatch.setattr(RT.db_recipes, "get_version", lambda rid, v: {"body": _CORPS_CODE})
    monkeypatch.setattr(RT.tool_registry, "bound_instance", lambda: None)
    return lus


def test_un_agent_sans_outils_dit_tout_ce_qui_lui_manque(monkeypatch):
    """`tools=[]` : c'est l'agent à qui il manque TOUT — le dire, pas se taire."""
    _procedure_et_recette(monkeypatch)
    t = asyncio.run(RT._avec_tool_warnings(
        ResolvedCtx(sub="porteur", org_id=77),
        {"id": 1, "org_id": 77, "procedure": "veille", "tools": []}))
    assert t["missing_tools"] == ["data_aggregate", "oto_recipe", "data_rows", "folk_record"]
    assert [(a["tool"], a["issue"], a["needed_by"]) for a in t["tool_warnings"]][-1] == (
        "folk_record", "not_granted", "recipe `recette-a`")


def test_une_liste_lit_chaque_procedure_et_ses_recettes_une_fois(monkeypatch):
    """Trois agents sur deux procédures qui lancent la même recette : une lecture par
    procédure et une par recette pour toute la réponse, pas une par agent."""
    lus = _procedure_et_recette(monkeypatch)
    rep = {"triggers": [
        {"id": 1, "org_id": 77, "procedure": "veille", "tools": ["oto_recipe"]},
        {"id": 2, "org_id": 77, "procedure": "veille", "tools": ["data_rows"],
         "kind": "webhook"},
        {"id": 3, "org_id": 77, "procedure": "relance", "tools": []}]}
    out = asyncio.run(RT._ajouter_tool_warnings(ResolvedCtx(sub="porteur", org_id=77), rep))
    assert lus == {"procedures": ["veille", "relance"], "recettes": ["recette-a"]}
    manque = {t["id"]: t["missing_tools"] for t in out["triggers"]}
    assert manque[1] == ["data_aggregate", "data_rows", "folk_record"]
    assert manque[2] == ["data_aggregate", "oto_recipe", "folk_record"], (
        "la lecture est partagée, le calcul reste celui de CHAQUE agent")


# ── les étapes d'une cascade que la procédure écrit ──────────────────────────

_PROC_CASCADE = (
    'Répéter `waterfall_email_act(op="update_rows", datastore="12", '
    'filter={"stage":"sourced"}, inputs={"full_name":"full_name","domain":"domain"}, '
    'output={"email":"email_verified"}, status_column="email_find_status", '
    'providers=["apollo","aiark"], batch_size=50)` une fois à la fois.')


def test_une_cascade_cite_les_outils_des_fournisseurs_qu_elle_nomme():
    from oto_mcp.waterfall import cites
    assert cites.outils_des_cascades(_PROC_CASCADE) == [
        ("apollo_match_person", "`waterfall_email_act` (providers)"),
        ("linkedin_aiark_person", "`waterfall_email_act` (providers)")]


def test_une_cascade_sans_fournisseurs_ecrits_ne_cite_rien():
    """Sans `providers=`, l'ordre se résout au run : toute la table serait une liste trop
    large, pas un outil qui manque."""
    from oto_mcp.waterfall import cites
    assert cites.outils_des_cascades('`waterfall_email_act(op="update_rows")`') == []
    assert cites.outils_des_cascades('providers=["apollo"] sans cascade') == []


def test_les_fournisseurs_appartiennent_a_la_cascade_qui_les_precede():
    """Deux cascades : chacune lit SES fournisseurs, et une clé qu'elle ne porte pas est
    ignorée (l'outil la refusera au run)."""
    from oto_mcp.waterfall import cites
    texte = ('`waterfall_phone(providers=["kaspr"])`, puis '
             '`waterfall_email(providers=["hunter", "inconnu"])`.')
    assert [n for n, _ in cites.outils_des_cascades(texte)] == [
        "kaspr_enrich_linkedin", "hunter_email_finder"]


def test_une_etape_asynchrone_cite_aussi_son_outil_de_collecte():
    from oto_mcp.waterfall import cites
    noms = [n for n, _ in cites.outils_des_cascades(
        'waterfall_email_act providers=["fullenrich"]')]
    assert noms[0] == "fullenrich_enrich_linkedin" and len(noms) == 2
    assert noms[1] != noms[0], "la collecte est un autre outil, appelé lui aussi"


def test_l_agent_dit_les_fournisseurs_de_sa_cascade_qu_il_n_a_pas(monkeypatch):
    """L'agent a la cascade, pas ses étapes : chaque étape serait refusée au travail et la
    ligne reviendrait sans réponse. Il le dit avant le run."""
    monkeypatch.setattr(RT.db, "get_guide_db", lambda *a: {"body_md": _PROC_CASCADE})
    t = {"procedure": "p", "tools": ["waterfall_email_act", "linkedin_aiark_person"]}
    assert RT._outils_non_accordes(77, t) == [
        ("apollo_match_person", "`waterfall_email_act` (providers)")]


# ── posé par une personne : ce que ses outils appellent pour lui ─────────────

_RECETTE_DECLARATIVE = {"tool": "lemlist_database", "source": {"items": "results"}}
_PROC_RECETTE_ET_CASCADE = (
    'Lire <tool:data_aggregate>. Sourcer : <tool:oto_recipe> `op="run"`, '
    '`slug="sourcing"`. Puis ' + _PROC_CASCADE)
#: Les effets tels que les outils les déclarent : `apollo_match_person` ne déclare rien
#: (comme la plupart des outils d'étape), `linkedin_aiark_person` et `lemlist_database`
#: se déclarent lectures.
_LECTURES = {"lemlist_database", "linkedin_aiark_person", "data_rows", "data_aggregate"}


@pytest.fixture
def _procedure_outillee(monkeypatch):
    """Une procédure qui cite un outil, lance une recette déclarative publiée et une
    cascade à deux fournisseurs ; seules les lectures connues sont déclarées `read`."""
    monkeypatch.setattr(RT.db, "get_guide_db",
                        lambda *a: {"body_md": _PROC_RECETTE_ET_CASCADE})
    monkeypatch.setattr(RT.db_recipes, "get_recipe_or_platform",
                        lambda ot, oid, slug: {"id": 3, "published_version": 1}
                        if slug == "sourcing" else None)
    monkeypatch.setattr(RT.db_recipes, "get_version",
                        lambda rid, v: {"body": _RECETTE_DECLARATIVE})
    monkeypatch.setattr(RT.tool_registry, "effet_de",
                        lambda n: "read" if n in _LECTURES else None)


def test_ses_outils_appellent_la_recette_et_les_etapes_qui_lui_manquent(_procedure_outillee):
    outils = ["oto_recipe", "waterfall_email_act", "linkedin_aiark_person"]
    assert RT._appeles_par_ses_outils(77, "p", outils) == [
        "lemlist_database", "apollo_match_person"]


def test_sans_l_outil_qui_appelle_rien_ne_lui_manque(_procedure_outillee):
    """Sans `oto_recipe`, la recette ne tournera pas ; sans la cascade, ses étapes non
    plus — et le marqueur `<tool:data_aggregate>` n'est jamais ajouté d'office."""
    assert RT._appeles_par_ses_outils(77, "p", ["data_rows"]) == []
    assert RT._appeles_par_ses_outils(77, "p", ["oto_recipe"]) == ["lemlist_database"]


def test_une_ecriture_qu_une_recette_code_appelle_n_est_jamais_ajoutee(monkeypatch):
    """`folk_record` agit : posé sur l'agent, il lui serait ouvert en direct."""
    monkeypatch.setattr(RT.db, "get_guide_db",
                        lambda *a: {"body_md": '<tool:oto_recipe> slug="ecrit"'})
    monkeypatch.setattr(RT.db_recipes, "get_recipe_or_platform",
                        lambda *a: {"id": 3, "published_version": 1})
    monkeypatch.setattr(RT.db_recipes, "get_version", lambda *a: {"body": _CORPS_CODE})
    monkeypatch.setattr(RT.tool_registry, "effet_de",
                        lambda n: "read" if n == "data_rows" else "act")
    assert RT._appeles_par_ses_outils(77, "p", ["oto_recipe"]) == ["data_rows"]
    monkeypatch.setattr(RT.tool_registry, "effet_de", lambda n: None)
    assert RT._appeles_par_ses_outils(77, "p", ["oto_recipe"]) == [], (
        "un effet inconnu n'est pas une lecture")


@pytest.fixture
def _agent_8(monkeypatch, _procedure_outillee):
    """L'agent 8 de `porteur`, sur la procédure outillée ; rend ce que `update_trigger`
    a reçu."""
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
        "id": i, "org_id": o, "sub": "porteur", "procedure": "p",
        "tools": ["oto_recipe", "waterfall_email_act"]})
    monkeypatch.setattr(RT.tool_alias, "canonical_names", lambda noms, sub: list(noms))
    vu: dict = {}
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs, **k: vu.update(champs) or {"id": i, **champs})
    return vu


def test_une_personne_qui_change_la_liste_voit_ce_qui_manque_sans_rien_d_ajoute(
        _agent_8, monkeypatch):
    """Correction (a) : la liste posée est celle demandée, ce qui manque est DIT."""
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    demandee = ["oto_recipe", "waterfall_email_act", "data_rows"]
    rep = RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2), RT.TriggerInput(
        op="update", trigger_id=8, tools=demandee))
    assert _agent_8["tools"] == demandee
    assert rep["tools_to_add"] == [
        "lemlist_database", "apollo_match_person", "linkedin_aiark_person"]


def test_une_retouche_qui_ne_touche_ni_liste_ni_procedure_ne_dit_rien(_agent_8, monkeypatch):
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    rep = RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2),
                            RT.TriggerInput(op="update", trigger_id=8, label="renommé"))
    assert "tools" not in _agent_8 and "tools_to_add" not in rep


def test_sous_le_jeton_d_un_travail_rien_n_est_propose(_agent_8, monkeypatch):
    """Un agent qui repose une liste (même ou plus courte) passe, et n'emporte rien de
    plus."""
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete",
                        lambda: types.SimpleNamespace(job_id=41))
    rep = RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2), RT.TriggerInput(
        op="update", trigger_id=8, tools=["oto_recipe", "waterfall_email_act"]))
    assert _agent_8["tools"] == ["oto_recipe", "waterfall_email_act"]
    assert "tools_to_add" not in rep


def test_une_prose_ecrite_par_un_agent_n_elargit_pas_la_liste_a_la_retouche_suivante(
        _agent_8, monkeypatch):
    """(b) Le détour : un run écrit dans la procédure une recette qui lit, et des
    fournisseurs de cascade ; une personne retouche ensuite l'agent. La liste écrite reste
    celle qu'elle a posée — un outil qu'elle a retiré ne revient pas —, ce que la prose
    appelle n'est que proposé."""
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    rep = RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2), RT.TriggerInput(
        op="update", trigger_id=8, tools=["oto_recipe", "waterfall_email_act"]))
    assert _agent_8["tools"] == ["oto_recipe", "waterfall_email_act"]
    assert "lemlist_database" in rep["tools_to_add"]
    _agent_8.clear()
    rep = RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2), RT.TriggerInput(
        op="update", trigger_id=8, procedure="p"))
    assert "tools" not in _agent_8, "changer la procédure n'écrit pas la liste"
    assert "lemlist_database" in rep["tools_to_add"]


def test_un_registre_d_effets_vide_n_ouvre_rien(_agent_8, monkeypatch):
    """Registre non réchauffé : aucune lecture de recette n'est proposée (fail-closed),
    seules les étapes de la liste blanche le sont, et la liste écrite ne bouge pas."""
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    monkeypatch.setattr(RT.tool_registry, "effet_de", lambda n: None)
    rep = RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2), RT.TriggerInput(
        op="update", trigger_id=8, tools=["oto_recipe", "waterfall_email_act"]))
    assert _agent_8["tools"] == ["oto_recipe", "waterfall_email_act"]
    assert rep["tools_to_add"] == ["apollo_match_person", "linkedin_aiark_person"]


def test_un_calcul_qui_echoue_leve_au_lieu_de_poser_la_liste_a_l_aveugle(
        _agent_8, monkeypatch):
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)

    def _panne(*a):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(RT.db_recipes, "get_recipe_or_platform", _panne)
    with pytest.raises(RuntimeError):
        RT._triggers_sync(ResolvedCtx(sub="porteur", org_id=2), RT.TriggerInput(
            op="update", trigger_id=8, tools=["oto_recipe"]))
    assert "tools" not in _agent_8


def test_a_la_pose_sous_le_jeton_d_un_travail_rien_n_est_propose(_procedure_outillee,
                                                                 monkeypatch):
    """(a) La pose (`op=create`), pas seulement la retouche : sous un jeton, rien."""
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete",
                        lambda: types.SimpleNamespace(job_id=41))
    assert RT._a_ajouter_par_une_personne(2, "p", ["oto_recipe", "waterfall_email_act"]) == []
    monkeypatch.setattr(outils_du_travail.verrou_org, "travail_de_la_requete", lambda: None)
    assert RT._a_ajouter_par_une_personne(2, "p", ["oto_recipe", "waterfall_email_act"]) == [
        "lemlist_database", "apollo_match_person", "linkedin_aiark_person"]


def test_chaque_outil_d_etape_est_range_proposable_ou_non():
    """La liste blanche est écrite à la main : une étape neuve doit y être rangée."""
    from oto_mcp.waterfall import cites
    from oto_mcp.waterfall.plan import STEPS_BY_NAMESPACE
    tous = {n for etapes in STEPS_BY_NAMESPACE.values() for s in etapes
            for n in cites.outils_de_l_etape(s)}
    assert not cites.PROPOSABLES & cites.NON_PROPOSES
    assert tous == cites.PROPOSABLES | cites.NON_PROPOSES, (
        tous ^ (cites.PROPOSABLES | cites.NON_PROPOSES))


def test_seule_la_liste_blanche_des_etapes_est_proposee(monkeypatch):
    """Ce qui décide, c'est `PROPOSABLES`, pas l'effet déclaré : `hunter_email_finder`
    (qui ne déclare rien) et `kaspr_enrich_linkedin` (même déclaré acteur) sont des
    étapes de la liste ; `apify_run_sync` (n'importe quel acteur) jamais."""
    monkeypatch.setattr(RT.db, "get_guide_db", lambda *a: {"body_md": (
        '`waterfall_email(providers=["hunter", "kaspr"])` puis '
        '`waterfall_company(providers=["linkedin"])`')})
    monkeypatch.setattr(RT.tool_registry, "effet_de",
                        lambda n: "act" if n == "kaspr_enrich_linkedin" else None)
    assert RT._appeles_par_ses_outils(
        77, "p", ["waterfall_email", "waterfall_company"]) == [
        "hunter_email_finder", "kaspr_enrich_linkedin"]
    assert ("apify_run_sync", "`waterfall_company` (providers)") in RT._outils_non_accordes(
        77, {"procedure": "p", "tools": ["waterfall_company"]}), (
        "jamais d'office, mais toujours dit : le geste reste à la personne")


def test_sans_recette_ni_cascade_la_procedure_n_est_pas_lue(monkeypatch):
    """La pose d'un agent ordinaire ne paie aucune lecture de plus."""
    def _interdit(*a):
        raise AssertionError("procédure lue pour rien")
    monkeypatch.setattr(RT.db, "get_guide_db", _interdit)
    assert RT._appeles_par_ses_outils(77, "p", ["data_rows", "gmail"]) == []
