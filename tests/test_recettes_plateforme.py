"""Les recettes de la PLATEFORME — `scope="platform"`, la résolution org → plateforme et
`GET /api/platform/recipes`.

Une procédure de la bibliothèque publique nomme sa recette (`oto_recipe <slug>`) : toute
org qui la copie doit pouvoir la lancer. Pur : le store est remplacé ; la garde est la
règle DÉCLARÉE par la capacité, éprouvée telle quelle. Le parcours sur une vraie base
est dans `test_recettes_plateforme_live.py`.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from oto_mcp import access
from oto_mcp.capabilities import recipes as cap
from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

DECLARATIVE = {"tool": "acme_people", "source": {"pagination": {"type": "none"}},
               "map": {"title": "profile.title"},
               "key": {"column": "contact_key", "template": "{{item.id}}"}}


def _fiche(slug: str, owner=("platform", "platform"), published: int | None = 1,
           latest: int = 1, rid: int = 1) -> dict:
    return {"id": rid, "owner_type": owner[0], "owner_id": owner[1], "slug": slug,
            "title": slug.title(), "description": None, "display": None,
            "published_version": published, "created_by": "op",
            "created_at": dt.datetime(2026, 10, 1), "updated_at": dt.datetime(2026, 10, 2),
            "latest_version": latest}


def _capacite(cle: str):
    (c,) = [c for c in CAPABILITIES if c.key == cle]
    return c


# ── `GET /api/platform/recipes` ─────────────────────────────────────────────────


def test_route_rest_tout_compte_et_provisoire():
    capa = _capacite("platform.recipes")
    (liaison,) = capa.rest_bindings()
    assert (liaison.verb, liaison.path) == ("GET", "/api/platform/recipes")
    assert liaison.provisoire is True
    assert capa.mcp is None
    assert capa.authz is SUB_ONLY
    # La MÊME forme que les recettes d'une org : la page lit l'une et l'autre pareil.
    assert capa.Output is _capacite("org.recipes").Output


def test_liste_les_recettes_de_la_plateforme_avec_leur_affichage(monkeypatch):
    vus = []

    def list_recipes(owners):
        vus.append(list(owners))
        return [{**_fiche("find-people", published=2, latest=3),
                 "display": {"steps": [{"label": "Find", "detail": "", "credits": 1.5,
                                        "per": "company"}], "saves": "one call a page"}},
                _fiche("draft-one", published=None, rid=2)]

    lus = []
    monkeypatch.setattr(cap.db_recipes, "list_recipes", list_recipes)
    monkeypatch.setattr(cap.db_recipes, "get_bodies",
                        lambda versions: lus.append(dict(versions)) or {
                            rid: DECLARATIVE for rid in versions})
    out = asyncio.run(cap._platform_recipes(ResolvedCtx(sub="u", org_id=7),
                                            cap.PlatformRecipesInput()))
    assert vus == [[("platform", "platform")]]
    assert lus == [{2: 1}]
    premiere, seconde = out["recipes"]
    assert set(premiere) == {"slug", "title", "description", "status", "published_version",
                             "latest_version", "updated_at", "display"}
    assert (premiere["status"], seconde["status"]) == ("published", "proposed")
    assert premiere["updated_at"] == "2026-10-02T00:00:00"
    assert premiere["display"]["source"] == "described"
    assert premiere["display"]["saves"] == "one call a page"
    assert premiere["display"]["steps"][0] == {"label": "Find", "detail": "",
                                               "credits": 1.5, "per": "company"}
    assert seconde["display"]["source"] == "derived"
    assert [e["label"] for e in seconde["display"]["steps"]] == ["Call acme_people",
                                                                "Write rows"]
    cap.OrgRecipes.model_validate(out)


def test_les_deux_routes_partagent_la_meme_lecture(monkeypatch):
    """Une seule lecture, paramétrée par le propriétaire : pas de copie qui dérive."""
    vus = []
    monkeypatch.setattr(cap.db_recipes, "list_recipes",
                        lambda owners: vus.append(list(owners)) or [])
    monkeypatch.setattr(cap.db_recipes, "get_bodies", lambda versions: {})
    asyncio.run(cap._org_recipes(ResolvedCtx(sub="u", org_id=7), cap.OrgRecipesInput(org_id=7)))
    asyncio.run(cap._platform_recipes(ResolvedCtx(sub="u"), cap.PlatformRecipesInput()))
    assert vus == [[("org", "7")], [("platform", "platform")]]


# ── La garde : écrire la plateforme est aux opérateurs ───────────────────────────


@pytest.fixture
def garde(monkeypatch):
    """La règle DÉCLARÉE par `me.recipe`, avec un rôle plateforme réglable."""
    role = {"operateur": False}
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: role["operateur"])
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "member")

    def juger(operateur: bool, **kw):
        role["operateur"] = operateur
        return _capacite("me.recipe").authz(RawCtx(sub="u"), cap.RecipeInput(**kw))
    return juger


@pytest.mark.parametrize("op", ["create", "propose", "describe", "test", "publish"])
def test_ecrire_la_plateforme_est_refuse_a_un_membre(garde, op):
    with pytest.raises(AuthzDenied) as e:
        garde(False, op=op, scope="platform", slug="x")
    assert (e.value.status, e.value.code) == (403, "platform_only")


@pytest.mark.parametrize("op", ["create", "propose", "describe", "test", "publish"])
def test_ecrire_la_plateforme_est_ouvert_a_un_operateur(garde, op):
    ctx = garde(True, op=op, scope="platform", slug="x")
    assert ctx.sub == "u" and ctx.org_id == 7


@pytest.mark.parametrize("op", ["list", "get", "versions", "run"])
def test_lire_et_lancer_la_plateforme_est_ouvert_a_tous(garde, op):
    ctx = garde(False, op=op, scope="platform", slug="x")
    assert ctx.sub == "u" and ctx.org_id == 7


@pytest.mark.parametrize("op", ["create", "propose", "describe", "test", "publish"])
def test_sans_scope_rien_ne_change_pour_un_membre(garde, op):
    assert garde(False, op=op, slug="x").org_id == 7


def test_le_refus_est_declare():
    assert (403, "platform_only") in {(e.status, e.code)
                                      for e in _capacite("me.recipe").errors}


# ── La résolution : l'org, sinon la plateforme ─────────────────────────────────


@pytest.fixture
def store(monkeypatch):
    """Un store à deux propriétaires : l'org 7 et la plateforme."""
    fiches = {("org", "7", "mine"): _fiche("mine", ("org", "7")),
              ("org", "7", "shared"): _fiche("shared", ("org", "7"), rid=2),
              ("platform", "platform", "shared"): _fiche("shared", rid=3),
              ("platform", "platform", "public"): _fiche("public", rid=4)}

    def get_recipe(ot, oid, slug):
        return fiches.get((ot, str(oid), slug))

    def get_recipe_or_platform(ot, oid, slug):
        return get_recipe(ot, oid, slug) or get_recipe("platform", "platform", slug)
    monkeypatch.setattr(cap.db_recipes, "get_recipe", get_recipe)
    monkeypatch.setattr(cap.db_recipes, "get_recipe_or_platform", get_recipe_or_platform)
    return fiches


def _fiche_de(op: str, slug: str, org_id=7, **kw) -> dict:
    return cap._fiche(ResolvedCtx(sub="u", org_id=org_id), cap.RecipeInput(op=op, slug=slug,
                                                                            **kw))


@pytest.mark.parametrize("op", ["get", "versions", "run"])
def test_sans_scope_une_lecture_ou_un_lancement_retombe_sur_la_plateforme(store, op):
    assert _fiche_de(op, "public")["owner_type"] == "platform"
    # L'org gagne toujours sur une recette de la plateforme du même slug.
    assert _fiche_de(op, "shared")["owner_type"] == "org"


@pytest.mark.parametrize("op", ["propose", "describe", "test", "publish"])
def test_sans_scope_une_ecriture_ne_vise_jamais_la_plateforme(store, op):
    """Sinon un membre écrirait une recette de la plateforme sans passer la garde — ou en
    éprouverait une version non publiée, qui n'est montrée qu'à ses opérateurs."""
    with pytest.raises(AuthzDenied) as e:
        _fiche_de(op, "public")
    assert e.value.code == "unknown_recipe"


def test_scope_explicite_ne_retombe_pas(store):
    with pytest.raises(AuthzDenied) as e:
        _fiche_de("get", "public", scope="org")
    assert e.value.code == "unknown_recipe"
    assert _fiche_de("get", "shared", scope="platform")["id"] == 3


def test_sans_org_active_la_plateforme_reste_lisible(store):
    assert _fiche_de("run", "public", org_id=None)["owner_type"] == "platform"
    with pytest.raises(AuthzDenied) as e:
        _fiche_de("get", "mine", org_id=None)
    assert e.value.code == "no_active_org"


@pytest.mark.parametrize("scope, attendus", [
    (None, [("org", "7"), ("user", "u"), ("platform", "platform")]),
    ("org", [("org", "7")]),
    ("user", [("user", "u")]),
    ("platform", [("platform", "platform")]),
])
def test_list_sans_scope_montre_aussi_la_plateforme(monkeypatch, scope, attendus):
    vus = []
    monkeypatch.setattr(cap.db_recipes, "list_recipes",
                        lambda owners: vus.append(list(owners)) or [])
    cap._gerer(ResolvedCtx(sub="u", org_id=7), cap.RecipeInput(op="list", scope=scope))
    assert vus == [attendus]


@pytest.mark.parametrize("op", ["test", "publish"])
def test_un_agent_heberge_n_eprouve_ni_ne_publie_le_code(op):
    """`code_human_only` tient : sous le jeton d'un agent hébergé, personne n'éprouve ni
    ne publie une recette-code — opérateur ou non."""
    from oto_mcp import verrou_org
    jeton = verrou_org.poser(verrou_org.Verrou(sub="op", org_id=7, job_id=1))
    try:
        with pytest.raises(AuthzDenied) as e:
            asyncio.run(cap._personne_pour_le_code({"kind": "code"}, op))
    finally:
        verrou_org.lever(jeton)
    assert (e.value.status, e.value.code) == (403, "code_human_only")


@pytest.mark.parametrize("op", ["test", "publish"])
def test_un_membre_eprouve_et_publie_son_code(monkeypatch, op):
    """#1202, 28/10/2026 : le code tourne avec les droits de qui l'EXÉCUTE, son auteur
    l'éprouve et le publie dans sa portée — sans être opérateur de la plateforme."""
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    asyncio.run(cap._personne_pour_le_code({"kind": "code"}, op))


def test_oto_recipe_declare_ses_deux_refus_de_porte():
    """Les deux portes sur la même capacité : opérateurs pour la plateforme, personne
    pour le code. Un rebase qui garderait l'une sans l'autre rougit ici."""
    codes = {e.code for e in _capacite("me.recipe").errors}
    assert {cap.CODE_PLATEFORME_SEULE, "code_human_only"} <= codes


# ── Borne de dernier recours d'une recette-code (`recipe_timeout`) ──────────────


def test_une_recette_code_qui_ne_rend_pas_la_main_est_refusee_sans_attendre_son_fil(
        monkeypatch):
    """Le pont ne rend pas la main (un fil d'outil qui bloque) : la capacité cesse
    d'attendre à sa borne et refuse en 504 `recipe_timeout` — sans attendre le fil, que
    le banc libère après sa mesure (sinon `asyncio.run` l'attendrait en fermant)."""
    import threading
    import time

    import anyio

    from oto_mcp.recipes import code
    libere = threading.Event()

    async def executer_qui_bloque(*a, **kw):
        await anyio.to_thread.run_sync(libere.wait, 30)
        return {"ok": True}

    monkeypatch.setattr(cap, "_instance", lambda: object())
    monkeypatch.setattr(cap, "_borne_s", lambda corps: 0.5)
    monkeypatch.setattr(code, "executer", executer_qui_bloque)

    async def scenario():
        debut = time.monotonic()
        try:
            with pytest.raises(AuthzDenied) as e:
                await cap._executer(ResolvedCtx(sub="u"), cap.RecipeInput(op="test"),
                                    {"kind": "code", "limits": {"max_seconds": 10}},
                                    ecrire=False)
            return e.value, time.monotonic() - debut
        finally:
            libere.set()

    refus, duree = asyncio.run(scenario())
    assert (refus.status, refus.code) == (504, "recipe_timeout")
    assert duree < 2
