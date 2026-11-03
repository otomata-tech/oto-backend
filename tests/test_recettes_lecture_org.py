"""`GET /api/orgs/{id}/recipes` — les recettes d'une org, sans corps — et leur affichage
(`display`, posé par `oto_recipe op=describe`).

Ce que la page d'un processus lit pour savoir quelles recettes l'org possède et si
chacune est en service. Pur : le store est remplacé, seul le handler est éprouvé
(la porte « membre de l'org » est `ORG_MEMBER_OF`, éprouvée ailleurs).
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from oto_mcp.capabilities import recipes as cap
from oto_mcp.capabilities._authz import ORG_MEMBER_OF
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.recipes import affichage, contrat


DECLARATIVE = {"tool": "acme_people", "source": {"pagination": {"type": "none"}},
               "map": {"title": "profile.title"},
               "key": {"column": "contact_key", "template": "{{item.id}}"}}


def _fiche(slug: str, published: int | None, latest: int) -> dict:
    return {"id": 1, "owner_type": "org", "owner_id": "7", "slug": slug, "title": slug.title(),
            "description": None, "display": None, "published_version": published, "created_by": "u",
            "created_at": dt.datetime(2026, 10, 1), "updated_at": dt.datetime(2026, 10, 2),
            "latest_version": latest}


def test_liste_les_recettes_de_l_org_avec_leur_statut(monkeypatch):
    vus = []

    def list_recipes(owners):
        vus.append(list(owners))
        return [{**_fiche("find-people", 2, 3),
                 "display": {"steps": [{"label": "Find", "detail": "",
                                        "credits": 1.5, "per": "company"}]}},
                {**_fiche("draft-one", None, 1), "id": 2}]

    lus = []

    def get_bodies(versions):
        lus.append(dict(versions))
        return {rid: DECLARATIVE for rid in versions}

    monkeypatch.setattr(cap.db_recipes, "list_recipes", list_recipes)
    monkeypatch.setattr(cap.db_recipes, "get_bodies", get_bodies)
    out = asyncio.run(cap._org_recipes(ResolvedCtx(sub="u", org_id=7, role="member"),
                                       cap.OrgRecipesInput(org_id=7)))
    assert vus == [[("org", "7")]]
    # Un seul lot de corps, et seulement pour la fiche sans affichage écrit.
    assert lus == [{2: 1}]
    assert [r["status"] for r in out["recipes"]] == ["published", "proposed"]
    premiere = out["recipes"][0]
    assert premiere["published_version"] == 2 and premiere["latest_version"] == 3
    assert premiere["description"] == ""
    assert premiere["updated_at"] == "2026-10-02T00:00:00"
    assert premiere["display"]["steps"][0]["per"] == "company"
    assert premiere["display"]["source"] == "described"
    # Sans affichage écrit, une recette déclarative est LUE dans son corps.
    lue = out["recipes"][1]["display"]
    assert lue["source"] == "derived"
    assert [e["label"] for e in lue["steps"]] == ["Call acme_people", "Write rows"]
    # Jamais de corps : celui d'une recette-code est du code.
    assert "body" not in premiere
    cap.OrgRecipes.model_validate(out)


def test_les_dates_telles_que_la_base_les_rend(monkeypatch):
    """La base rend ses dates en TEXTE (`_str_dict_row`), pas en `datetime` : la liste
    répondait 500 sur tout org ayant une recette (prod, 25 octobre 2026)."""
    from oto_mcp.db._conn import _normalize_value

    texte = _normalize_value(dt.datetime(2026, 10, 2, 9, 30))
    assert isinstance(texte, str)
    fiche = {**_fiche("find-people", 2, 2), "created_at": _normalize_value(dt.datetime(2026, 10, 1)),
             "updated_at": texte}
    monkeypatch.setattr(cap.db_recipes, "list_recipes", lambda owners: [fiche])
    monkeypatch.setattr(cap.db_recipes, "get_bodies", lambda versions: {1: DECLARATIVE})
    out = asyncio.run(cap._org_recipes(ResolvedCtx(sub="u", org_id=7, role="member"),
                                       cap.OrgRecipesInput(org_id=7)))
    assert out["recipes"][0]["updated_at"] == "2026-10-02T09:30:00"
    cap.OrgRecipes.model_validate(out)


def test_une_recette_code_sans_affichage_ecrit_n_a_rien_a_dessiner(monkeypatch):
    monkeypatch.setattr(cap.db_recipes, "list_recipes", lambda owners: [_fiche("c", 1, 1)])
    monkeypatch.setattr(cap.db_recipes, "get_bodies", lambda versions: {
        rid: {"kind": "code", "sources": {}, "entrypoint": "r:run"} for rid in versions})
    out = asyncio.run(cap._org_recipes(ResolvedCtx(sub="u", org_id=7, role="member"),
                                       cap.OrgRecipesInput(org_id=7)))
    assert out["recipes"][0]["display"] is None


def test_les_corps_de_la_liste_se_lisent_en_un_lot(monkeypatch):
    """Pas de N+1 : un seul appel pour tous les corps, chacun à sa version en service,
    sinon à la dernière proposée ; rien à lire quand tout est écrit."""
    fiches = [{**_fiche(f"r{i}", 2 if i % 2 else None, 3), "id": i} for i in range(5)]
    lus = []
    monkeypatch.setattr(cap.db_recipes, "list_recipes", lambda owners: fiches)
    monkeypatch.setattr(cap.db_recipes, "get_bodies",
                        lambda versions: lus.append(dict(versions)) or {})
    monkeypatch.setattr(cap.db_recipes, "get_version",
                        lambda *a: pytest.fail("une lecture de corps par recette"))
    cap._lister(("org", "7"))
    assert lus == [{0: 3, 1: 2, 2: 3, 3: 2, 4: 3}]


def test_route_rest_membre_seulement_et_provisoire():
    capa = next(c for c in CAPABILITIES if c.key == "org.recipes")
    (liaison,) = capa.rest_bindings()
    assert (liaison.verb, liaison.path) == ("GET", "/api/orgs/{id}/recipes")
    assert liaison.provisoire is True
    assert capa.mcp is None
    assert capa.authz.__qualname__ == ORG_MEMBER_OF("org_id").__qualname__


# ── L'affichage : validé à l'écriture, posé par `describe` ──────────────────────


def test_affichage_valide_et_normalise():
    out = contrat.valider_affichage({
        "steps": [{"label": " Pick accounts ", "detail": "Tier A/B", "credits": 0.3,
                   "per": "company"}, {"label": "Write"}],
        "saves": "replaces ~300 tool calls a run with 9"})
    assert out == {"steps": [{"label": "Pick accounts", "detail": "Tier A/B",
                              "credits": 0.3, "per": "company"},
                             {"label": "Write", "detail": ""}],
                   "saves": "replaces ~300 tool calls a run with 9"}


@pytest.mark.parametrize("brut, attendu", [
    ("x", "must be an object"),
    ({"steps": []}, "1 to 10 steps"),
    ({"steps": [{"label": "a"}] * 11}, "1 to 10 steps"),
    ({"steps": [{"label": ""}]}, "label: 1 to 60"),
    ({"steps": [{"label": "a", "credits": 1}]}, "go together"),
    ({"steps": [{"label": "a", "credits": 1, "per": "seat"}]}, "per must be one of"),
    ({"steps": [{"label": "a", "credits": -1, "per": "row"}]}, "0 to 10000"),
    ({"steps": [{"label": "a", "credits": True, "per": "row"}]}, "0 to 10000"),
    ({"steps": [{"label": "a", "color": "red"}]}, "unknown keys"),
    ({"steps": [{"label": "a"}], "cost": 3}, "unknown keys"),
])
def test_affichage_refuse(brut, attendu):
    with pytest.raises(contrat.RecetteInvalide) as e:
        contrat.valider_affichage(brut)
    assert attendu in str(e.value)


def _ctx():
    return ResolvedCtx(sub="u", org_id=7, role="member")


def test_describe_pose_l_affichage_sans_toucher_aux_versions(monkeypatch):
    fiche = _fiche("find-people", 2, 3)
    poses = []
    monkeypatch.setattr(cap.db_recipes, "get_recipe", lambda *a: fiche)
    monkeypatch.setattr(cap.db_recipes, "describe_recipe",
                        lambda rid, **kw: poses.append((rid, kw)))
    monkeypatch.setattr(cap.db_recipes, "propose_version",
                        lambda *a, **k: pytest.fail("describe ne propose aucune version"))
    cap._gerer(_ctx(), cap.RecipeInput(op="describe", slug="find-people",
                                       display={"steps": [{"label": "Find"}]}))
    cap._gerer(_ctx(), cap.RecipeInput(op="describe", slug="find-people", display={}))
    assert poses == [(1, {"title": None, "description": None,
                          "display": {"steps": [{"label": "Find", "detail": ""}]}}),
                     (1, {"title": None, "description": None, "display": None})]


def test_create_ecrit_l_affichage_avec_la_recette(monkeypatch):
    """Une seule écriture, dans la transaction de la création : pas de second UPDATE."""
    crees = []
    monkeypatch.setattr(cap, "_corps", lambda ctx, brut: brut)
    # Aucune recette de la plateforme de ce slug (`slug_reserved_by_platform`).
    monkeypatch.setattr(cap.db_recipes, "get_recipe", lambda *a: None)
    monkeypatch.setattr(cap.db_recipes, "create_recipe",
                        lambda **kw: crees.append(kw) or _fiche(kw["slug"], None, 1))
    monkeypatch.setattr(cap.db_recipes, "describe_recipe",
                        lambda *a, **k: pytest.fail("l'affichage s'écrit à la création"))
    cap._gerer(_ctx(), cap.RecipeInput(op="create", slug="find-people", title="t",
                                       recipe=DECLARATIVE,
                                       display={"steps": [{"label": "Find"}]}))
    assert crees[0]["display"] == {"steps": [{"label": "Find", "detail": ""}]}


def test_describe_refuse_un_affichage_mal_forme_et_un_appel_vide(monkeypatch):
    monkeypatch.setattr(cap.db_recipes, "get_recipe", lambda *a: _fiche("x", 1, 1))
    monkeypatch.setattr(cap.db_recipes, "describe_recipe",
                        lambda *a, **k: pytest.fail("rien ne doit être écrit"))
    with pytest.raises(AuthzDenied) as e:
        cap._gerer(_ctx(), cap.RecipeInput(op="describe", slug="x",
                                           display={"steps": [{"label": "a", "credits": 1}]}))
    assert e.value.code == "invalid_display"
    with pytest.raises(AuthzDenied) as e:
        cap._gerer(_ctx(), cap.RecipeInput(op="describe", slug="x"))
    assert e.value.code == "nothing_to_describe"


# ── Les étapes LUES dans le corps d'une recette déclarative ─────────────────────


def test_etapes_lues_d_une_recette_complete():
    corps = {"tool": "serper_search",
             "for_each": {"datastore": 12, "status_column": "news_status",
                          "filter": {"tier": "A"}},
             "source": {"pagination": {"type": "page", "size": 10}},
             "where": [{"path": "date", "op": "not_empty"},
                       {"path": "link", "op": "not_in_table", "table": 12,
                        "column": "url"}],
             "map": {"title": "title", "url": "link"}, "values": {"src": "news"},
             "key": {"column": "url"}, "on_existing": "update",
             "limits": {"max_units": 100, "max_pages": 3}}
    assert affichage.etapes_lues(corps) == {"steps": [
        {"label": "For each row",
         "detail": "Rows of table #12 matching a filter whose news_status is empty"},
        {"label": "Call serper_search", "detail": "Page by page, 10 a page, at most 3 pages"},
        {"label": "Keep matching items",
         "detail": "1 condition on each item; skips what table #12 already holds"},
        {"label": "Write rows",
         "detail": "3 columns, one row per url; rows already there are updated"}]}
    # Même forme que `display`, et valide pour son contrat.
    contrat.valider_affichage(affichage.etapes_lues(corps))


def test_rien_a_lire_dans_une_recette_code_ou_un_corps_vide():
    assert affichage.etapes_lues({"kind": "code", "sources": {}}) is None
    assert affichage.etapes_lues({}) is None
    assert affichage.etapes_lues(None) is None
