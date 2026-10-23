"""Les recettes EN CODE (#1202, lot 2) de bout en bout par `oto_recipe` : la plateforme
seule éprouve et publie, l'épreuve n'écrit rien, une exécution écrit par clé dans le seul
tableau qu'elle désigne, et le reçu reste des comptes.

Vrai store (`live`) et vrai bac à sable : sans bac à sable, ÉCHEC en CI, passé sur un
poste qui n'en a pas posé — en le disant."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

SUB = "sub-recettes-code"
SCHEMA = {"key": "slug", "fields": [{"key": "slug", "type": "text"},
                                    {"key": "name", "type": "text"}]}

# Lit les personnes d'Acme, en écrit une ligne par personne, rend des comptes.
CODE = (
    "async def run(oto, params):\n"
    "    lu = await oto.call('acme_people', {'company': params['company']})\n"
    "    lignes = [{'slug': p['id'], 'name': p['name']} for p in lu['content']]\n"
    "    try:\n"
    "        bilan = await oto.call('data_write', {'rows': lignes, 'key': 'slug'})\n"
    "    except oto.ToolError as e:\n"
    "        return {'lus': len(lignes), 'ecriture': e.code}\n"
    "    return {'lus': len(lignes), 'inserted': bilan['inserted'],\n"
    "            'updated': bilan['updated']}\n")


def _corps(source: str = CODE, **limites) -> dict:
    return {"kind": "code", "sources": {"recette.py": source}, "entrypoint": "recette:run",
            "params": {"company": {"required": True}},
            "limits": {"max_units": 50, **limites}}


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    return SUB


@pytest.fixture
def capa(compte, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, tool_registry
    from oto_mcp.auth import hooks
    from oto_mcp.capabilities import recipes
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.functions import executor
    from oto_mcp.tools import datastore as outils_tableaux
    from oto_mcp.tools.lecture import LECTURE
    if not os.environ.get(executor.ENV_DIR):
        if os.environ.get("CI"):
            pytest.fail(f"{executor.ENV_DIR} absente")
        pytest.skip(f"{executor.ENV_DIR} absente : pas de bac à sable sur ce poste")
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)
    # `data_rows` / `data_write` sont les VRAIS outils, appelés par `executer_cible` :
    # ils lisent l'acteur par `access`.
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    plateforme = {"oui": False}
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: plateforme["oui"])
    m = FastMCP("t-recettes-code")

    @m.tool(annotations=LECTURE)
    def acme_people(company: str = "") -> dict:
        return {"content": [{"id": f"{company}-{i}", "name": f"P {i}"} for i in range(3)]}

    @m.tool()
    def acme_post(text: str = "") -> dict:
        return {"posted": True}

    # Un faux CRM : la liste fermée des écritures ne le connaît que dans les bancs du
    # lot 3 (`crm`).
    @m.tool()
    def acme_crm(op: str = "search", name: str = "") -> dict:
        crees.append((op, name))
        return {"id": f"rec-{name}"} if op == "create" else {"items": []}
    crees: list = []
    outils_tableaux.register(m)
    monkeypatch.setattr(tool_registry, "bound_instance", lambda: m)
    ctx = ResolvedCtx(sub=SUB, org_id=None)

    def appeler(**kw):
        kw.setdefault("scope", "user")
        return asyncio.run(recipes._recipe(ctx, recipes.RecipeInput(**kw)))
    appeler.crees = crees
    return appeler, plateforme


def _table() -> str:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = f"recettes-code-{uuid.uuid4().hex[:6]}"
    db.create_datastore("user", SUB, ns)
    make_store(SUB).set_schema(ns, SCHEMA)
    return ns


def _lignes(ns: str) -> list:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).cursor_rows(ns, limit=100)["rows"]


def _code(exc) -> str:
    return getattr(exc.value, "code", None) or exc.value.args[1]


def test_le_membre_ecrit_la_plateforme_eprouve_et_publie(capa):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, plateforme = capa
    slug = f"code-{uuid.uuid4().hex[:6]}"
    assert appeler(op="create", slug=slug, title="t", recipe=_corps())["version"] == {
        "version": 1, "status": "proposee"}
    for op in ("test", "publish"):
        with pytest.raises(AuthzDenied) as e:
            appeler(op=op, slug=slug, version=1, params={"company": "acme"})
        assert _code(e) == "platform_only"
    with pytest.raises(AuthzDenied) as e:
        appeler(op="test", recipe=_corps(), params={"company": "acme"})
    assert _code(e) == "platform_only"
    plateforme["oui"] = True
    essai = appeler(op="test", slug=slug, params={"company": "acme"})["receipt"]
    # L'épreuve lit, mais n'écrit rien : `data_write` y est refusé.
    assert essai["ok"] and essai["result"] == {"lus": 3, "ecriture": "write_in_test"}
    assert "journal" in essai
    pub = appeler(op="publish", slug=slug, version=1, params={"company": "acme"})
    assert pub["version"]["status"] == "publiee"


def _publiee(appeler, plateforme, corps=None) -> str:
    slug = f"code-{uuid.uuid4().hex[:6]}"
    appeler(op="create", slug=slug, title="t", recipe=corps or _corps())
    plateforme["oui"] = True
    appeler(op="publish", slug=slug, version=1, params={"company": "acme"})
    plateforme["oui"] = False
    return slug


def test_une_execution_ecrit_par_cle_et_une_relance_ne_double_rien(capa):
    appeler, plateforme = capa
    slug, ns = _publiee(appeler, plateforme), _table()
    run = appeler(op="run", slug=slug, datastore=ns, params={"company": "acme"})["receipt"]
    assert run["ok"] and run["done"] and run["result"] == {"lus": 3, "inserted": 3,
                                                           "updated": 0}
    assert "journal" not in run
    encore = appeler(op="run", slug=slug, datastore=ns, params={"company": "acme"})["receipt"]
    assert encore["result"]["inserted"] == 0
    assert len(_lignes(ns)) == 3


def test_le_code_n_ecrit_que_dans_le_tableau_de_l_execution(capa):
    appeler, plateforme = capa
    autre = _table()
    source = ("async def run(oto, params):\n"
              "    try:\n"
              f"        await oto.call('data_write', {{'datastore': '{autre}',\n"
              "            'rows': [{'slug': 'x'}], 'key': 'slug'})\n"
              "        return 'ecrit'\n"
              "    except oto.ToolError as e:\n"
              "        return e.code\n")
    slug, ns = _publiee(appeler, plateforme, _corps(source)), _table()
    run = appeler(op="run", slug=slug, datastore=ns, params={"company": "acme"})["receipt"]
    assert run["result"] == "write_outside_target" and _lignes(autre) == []


def test_un_outil_de_connecteur_qui_ecrit_est_refuse(capa):
    appeler, plateforme = capa
    source = ("async def run(oto, params):\n"
              "    try:\n"
              "        await oto.call('acme_post', {'text': 'x'})\n"
              "        return 'poste'\n"
              "    except oto.ToolError as e:\n"
              "        return e.code\n")
    slug = _publiee(appeler, plateforme, _corps(source))
    run = appeler(op="run", slug=slug, params={"company": "acme"})["receipt"]
    assert run["result"] == "recipe_tool_not_read_only"


def test_le_code_lit_un_tableau_par_data_rows(capa):
    appeler, plateforme = capa
    ns = _table()
    from oto_mcp.datastore.core import make_store
    make_store(SUB).write_rows(ns, [{"slug": "a", "name": "A"}, {"slug": "b", "name": "B"}],
                               key="slug")
    source = ("async def run(oto, params):\n"
              f"    page = await oto.call('data_rows', {{'datastore': '{ns}', 'limit': 10}})\n"
              "    return sorted(r['slug'] for r in page['rows'])\n")
    slug = _publiee(appeler, plateforme, _corps(source))
    assert appeler(op="run", slug=slug, params={"company": "acme"})["receipt"]["result"] == [
        "a", "b"]


def test_le_recu_reste_des_comptes(capa):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, plateforme = capa
    # Toujours trop gros : la publication le refuse dès l'épreuve.
    slug = f"code-{uuid.uuid4().hex[:6]}"
    appeler(op="create", slug=slug, title="t",
            recipe=_corps("async def run(oto, params):\n    return 'x' * 10000\n"))
    plateforme["oui"] = True
    with pytest.raises(AuthzDenied) as e:
        appeler(op="publish", slug=slug, version=1, params={"company": "acme"})
    assert _code(e) == "test_failed" and "receipt_too_large" in str(e.value)
    plateforme["oui"] = False
    # Trop gros selon les paramètres : l'exécution rend un échec, pas les données.
    source = ("async def run(oto, params):\n"
              "    return 'x' * (10000 if params['company'] == 'big' else 1)\n")
    slug = _publiee(appeler, plateforme, _corps(source))
    run = appeler(op="run", slug=slug, params={"company": "big"})["receipt"]
    assert run["ok"] is False and "receipt_too_large" in run["error"]
    assert "result" not in run


def test_le_contrat_d_une_recette_code(capa):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, _ = capa
    for corps in ({**_corps(), "limits": {}},
                  {**_corps(), "entrypoint": "absent:run"},
                  {**_corps(), "tool": "acme_people"}):
        with pytest.raises(AuthzDenied) as e:
            appeler(op="create", slug=f"code-{uuid.uuid4().hex[:6]}", title="t", recipe=corps)
        assert _code(e) == "invalid_recipe"


def test_une_recette_code_n_a_pas_de_reprise(capa):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, plateforme = capa
    slug = _publiee(appeler, plateforme)
    with pytest.raises(AuthzDenied) as e:
        appeler(op="run", slug=slug, params={"company": "acme"}, resume="abc")
    assert _code(e) == "resume_not_supported"


# ── lot 3 : écrire chez un tiers ─────────────────────────────────────────────────────

CODE_CRM = (
    "async def run(oto, params):\n"
    "    lu = await oto.call('acme_people', {'company': params['company']})\n"
    "    crees, refus = 0, None\n"
    "    for p in lu['content']:\n"
    "        trouve = await oto.call('acme_crm', {'op': 'search', 'name': p['id']})\n"
    "        if trouve['items']:\n"
    "            continue\n"
    "        try:\n"
    "            await oto.call('acme_crm', {'op': 'create', 'name': p['id']})\n"
    "            crees += 1\n"
    "        except oto.ToolError as e:\n"
    "            refus = e.code\n"
    "            break\n"
    "    return {'crees': crees, 'refus': refus}\n")


@pytest.fixture
def crm(capa, monkeypatch):
    from oto_mcp.recipes import ecritures as ec
    monkeypatch.setitem(ec.ECRITURES, "acme_crm", ec.Ecriture(
        frozenset({"search"}), frozenset({"create"})))
    return capa


def _corps_crm(**autres) -> dict:
    return {**_corps(CODE_CRM), "writes": ["acme_crm:create"], **autres}


def test_le_contrat_des_ecritures(crm):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, _ = crm
    for corps in (_corps_crm(writes=["acme_crm:delete"]),
                  _corps_crm(writes=["acme_crm:search"]),
                  _corps_crm(writes=["acme_crm:create", "acme_crm:create"]),
                  _corps_crm(writes="acme_crm:create"),
                  {**_corps_crm(), "limits": {"max_units": 50, "max_writes": -1}},
                  {**_corps(), "writes": ["lemlist_create_lead"]},
                  {**_corps(), "writes": ["lemlist_create_lead"], "allow_sending": "yes"}):
        with pytest.raises(AuthzDenied) as e:
            appeler(op="create", slug=f"code-{uuid.uuid4().hex[:6]}", title="t", recipe=corps)
        assert _code(e) == "invalid_recipe", corps
    envoi = {**_corps(), "writes": ["lemlist_create_lead"], "allow_sending": True}
    assert appeler(op="create", slug=f"code-{uuid.uuid4().hex[:6]}", title="t",
                   recipe=envoi)["version"]["version"] == 1


def test_l_epreuve_lit_le_crm_mais_n_y_ecrit_rien(crm):
    appeler, plateforme = crm
    plateforme["oui"] = True
    essai = appeler(op="test", recipe=_corps_crm(), params={"company": "acme"})["receipt"]
    assert essai["ok"] and essai["result"] == {"crees": 0, "refus": "write_in_test"}
    assert essai["writes"] == {}
    assert [op for op, _ in appeler.crees] == ["search"]


def test_une_execution_ecrit_ce_qu_elle_declare_et_le_compte(crm):
    appeler, plateforme = crm
    slug = _publiee(appeler, plateforme, _corps_crm())
    appeler.crees.clear()
    run = appeler(op="run", slug=slug, params={"company": "acme"})["receipt"]
    assert run["ok"] and run["done"] and run["result"] == {"crees": 3, "refus": None}
    assert run["writes"] == {"acme_crm:create": 3}
    assert [n for op, n in appeler.crees if op == "create"] == ["acme-0", "acme-1", "acme-2"]


def test_le_plafond_d_ecritures_arrete_le_code_et_le_recu_le_dit(crm):
    appeler, plateforme = crm
    corps = {**_corps_crm(), "limits": {"max_units": 50, "max_writes": 2}}
    slug = _publiee(appeler, plateforme, corps)
    run = appeler(op="run", slug=slug, params={"company": "acme"})["receipt"]
    assert run["result"] == {"crees": 2, "refus": "budget_exhausted"}
    assert run["stopped"] == "max_writes" and run["done"] is False
    assert run["writes"] == {"acme_crm:create": 2}


def test_une_recette_sans_writes_ne_peut_pas_ecrire(crm):
    appeler, plateforme = crm
    slug = _publiee(appeler, plateforme, _corps(CODE_CRM))
    run = appeler(op="run", slug=slug, params={"company": "acme"})["receipt"]
    assert run["result"] == {"crees": 0, "refus": "write_not_declared"}
    assert not [op for op, _ in appeler.crees if op == "create"]
