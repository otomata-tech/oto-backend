"""Les recettes de la PLATEFORME de bout en bout sur un vrai store : un opérateur l'écrit
et la publie (`scope="platform"`), un membre d'une autre org la trouve et la lance sans
`scope` — avec SON identité, SON org et SES droits sur le tableau — et une recette de
l'org du même slug n'est jamais choisie en silence à sa place.

⚠️ Le propriétaire plateforme est partagé par tous les modules d'une même base : slugs
uniques, et jamais d'égalité sur une liste entière."""
from __future__ import annotations

import asyncio
import uuid

import pytest

OP = "sub-recettes-plateforme-op"
MEMBRE = "sub-recettes-plateforme-membre"
SCHEMA = {"key": "contact_key", "fields": [
    {"key": "contact_key", "type": "text"}, {"key": "title", "type": "text"}]}
PERSONNES = [{"link": {"linkedin": f"https://www.linkedin.com/in/p{i}"},
              "profile": {"title": f"Role {i}"}} for i in range(4)]
CORPS = {"tool": "acme_people", "arguments": {}, "source": {
    "items": "content", "pagination": {"type": "page", "param": "page", "size": 2,
                                       "size_param": "size", "last": "last"}},
         "map": {"linkedin_url": "link.linkedin", "title": "profile.title"},
         "key": {"column": "contact_key", "template": "{{item.link.linkedin}}"},
         "limits": {"max_units": 50}}


@pytest.fixture(scope="module")
def comptes(live):
    from oto_mcp import db
    for sub in (OP, MEMBRE):
        db.upsert_user(sub, email=f"{sub}@acme.test", name=sub)
    db.set_user_role(OP, "admin")
    return OP, MEMBRE


@pytest.fixture
def monde(comptes, monkeypatch):
    """Un outil de lecture factice, une org dont MEMBRE est membre (pas l'opérateur), et
    `appeler(sub, org, **kw)` : la capacité appelée sous cette identité, après sa garde
    DÉCLARÉE (la règle de `me.recipe`, pas un raccourci)."""
    from fastmcp import FastMCP

    from oto_mcp import access, org_store, tool_registry
    from oto_mcp.auth import hooks
    from oto_mcp.capabilities import recipes
    from oto_mcp.capabilities._types import RawCtx
    from oto_mcp.capabilities.registry import CAPABILITIES
    from oto_mcp.tools.lecture import LECTURE
    m = FastMCP("t-recettes-plateforme")

    @m.tool(annotations=LECTURE)
    def acme_people(page: int = 0, size: int = 2) -> dict:
        return {"content": PERSONNES[page * size:(page + 1) * size],
                "last": (page + 1) * size >= len(PERSONNES)}
    monkeypatch.setattr(tool_registry, "bound_instance", lambda: m)
    org = org_store.create_org(f"Acme members {uuid.uuid4().hex[:4]}", created_by=MEMBRE)
    org_store.add_org_member(org, MEMBRE)
    (capa,) = [c for c in CAPABILITIES if c.key == "me.recipe"]

    def appeler(sub: str, org_id, **kw):
        monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: sub)
        monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: sub)
        inp = recipes.RecipeInput(**({"org": org_id} if org_id else {}), **kw)
        ctx = capa.authz(RawCtx(sub=sub), inp)
        return asyncio.run(recipes._recipe(ctx, inp))
    return appeler, org


def _code(exc) -> str:
    return getattr(exc.value, "code", None) or exc.value.args[1]


def _publier_plateforme(appeler, slug: str, corps=CORPS, **kw) -> dict:
    appeler(OP, None, op="create", scope="platform", slug=slug, title="Acme people",
            recipe=corps, **kw)
    return appeler(OP, None, op="publish", scope="platform", slug=slug, version=1)


def _table_de_l_org(org: int) -> tuple[int, str]:
    from oto_mcp import db, session_org
    from oto_mcp.datastore.core import make_store
    nom = f"recettes-plateforme-{uuid.uuid4().hex[:6]}"
    ns_id = db.create_datastore("org", str(org), nom)
    jeton = session_org.set_call_org(org)
    try:
        make_store(MEMBRE).set_schema(nom, SCHEMA)
    finally:
        session_org.reset_call_org(jeton)
    return ns_id, nom


def test_un_membre_ne_peut_pas_ecrire_la_plateforme(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, org = monde
    slug = f"acme-plat-{uuid.uuid4().hex[:6]}"
    with pytest.raises(AuthzDenied) as e:
        appeler(MEMBRE, org, op="create", scope="platform", slug=slug, title="t",
                recipe=CORPS)
    assert (e.value.status, _code(e)) == (403, "platform_only")
    _publier_plateforme(appeler, slug)
    for op, extra in (("propose", {"expected_version": 1, "recipe": CORPS}),
                      ("describe", {"title": "Mine now"}), ("test", {}),
                      ("publish", {"version": 1})):
        with pytest.raises(AuthzDenied) as e:
            appeler(MEMBRE, org, op=op, scope="platform", slug=slug, **extra)
        assert _code(e) == "platform_only", op
    # Sans `scope`, une écriture ne vise que l'org : elle ne trouve pas la recette de
    # la plateforme, elle ne la modifie pas.
    for op, extra in (("propose", {"expected_version": 1, "recipe": CORPS}),
                      ("describe", {"title": "Mine now"})):
        with pytest.raises(AuthzDenied) as e:
            appeler(MEMBRE, org, op=op, slug=slug, **extra)
        assert _code(e) == "unknown_recipe", op
    fiche = appeler(MEMBRE, org, op="get", scope="platform", slug=slug)["recipe"]
    assert fiche["title"] == "Acme people" and fiche["latest_version"] == 1


def test_un_operateur_ecrit_et_publie_une_recette_de_la_plateforme(monde):
    appeler, _ = monde
    slug = f"acme-plat-{uuid.uuid4().hex[:6]}"
    pub = _publier_plateforme(appeler, slug)
    assert pub["version"]["status"] == "publiee"
    assert (pub["recipe"]["owner_type"], pub["recipe"]["owner_id"]) == ("platform", "platform")
    out = appeler(OP, None, op="describe", scope="platform", slug=slug,
                  display={"steps": [{"label": "Find people"}]})
    assert out["recipe"]["display"]["steps"][0]["label"] == "Find people"


def test_un_membre_lance_la_recette_de_la_plateforme_avec_ses_propres_droits(monde):
    """Sans `scope` : get, versions et run trouvent la recette de la plateforme. Le run
    écrit dans le tableau de l'org du MEMBRE, et ses appels sont journalisés sous SON
    identité et SON org — jamais celles de l'opérateur qui l'a écrite."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.db._conn import _connect
    appeler, org = monde
    slug = f"acme-plat-{uuid.uuid4().hex[:6]}"
    _publier_plateforme(appeler, slug)
    assert appeler(MEMBRE, org, op="get", slug=slug)["recipe"]["owner_type"] == "platform"
    assert [v["version"] for v in appeler(MEMBRE, org, op="versions",
                                          slug=slug)["versions"]] == [1]
    ns, nom = _table_de_l_org(org)
    run = appeler(MEMBRE, org, op="run", slug=slug, datastore=ns)
    assert run["recipe"]["owner_type"] == "platform"
    assert run["receipt"]["done"] and run["receipt"]["written"] == 4
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT DISTINCT sub, org_id FROM tool_calls WHERE tool = 'acme_people' "
            "AND sub IN (%s, %s) AND org_id = %s", (OP, MEMBRE, org)).fetchall()
    assert [(r["sub"], r["org_id"]) for r in lignes] == [(MEMBRE, org)]
    from oto_mcp import session_org
    jeton = session_org.set_call_org(org)
    try:
        assert len(make_store(MEMBRE).cursor_rows(nom, limit=10)["rows"]) == 4
    finally:
        session_org.reset_call_org(jeton)


def test_un_membre_ne_prend_pas_le_slug_d_une_recette_de_la_plateforme(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, org = monde
    slug = f"acme-plat-{uuid.uuid4().hex[:6]}"
    _publier_plateforme(appeler, slug)
    for scope in ({}, {"scope": "org"}, {"scope": "user"}):
        with pytest.raises(AuthzDenied) as e:
            appeler(MEMBRE, org, op="create", slug=slug, title="Ours", recipe=CORPS,
                    **scope)
        assert (e.value.status, _code(e)) == (409, "slug_reserved_by_platform"), scope
    libre = f"acme-org-{uuid.uuid4().hex[:6]}"
    cree = appeler(MEMBRE, org, op="create", slug=libre, title="Ours", recipe=CORPS)
    assert cree["recipe"]["owner_type"] == "org"


def _poser_en_base(org: int, slug: str, corps: dict, titre: str) -> None:
    """Une recette d'org PUBLIÉE, posée sans passer par `create` : une collision
    antérieure à la garde, ou une recette de la plateforme créée après celle de l'org."""
    from oto_mcp.db import recipes as db_recipes
    from oto_mcp.recipes import contrat
    fiche = db_recipes.create_recipe(owner_type="org", owner_id=str(org), slug=slug,
                                     title=titre, description="", created_by=MEMBRE,
                                     body=contrat.valider(corps), note=None)
    db_recipes.decide_version(fiche["id"], 1, status="publiee", decided_by=MEMBRE,
                              test_report=None)


def test_une_collision_existante_est_refusee_sans_scope(monde):
    """Sans `scope`, get, versions et run refusent `ambiguous_slug` : ni la recette du
    membre (son code s'exécuterait chez qui suit la procédure publique), ni celle de la
    plateforme en silence. Avec `scope`, chacune se lance."""
    from oto_mcp.capabilities._types import AuthzDenied
    appeler, org = monde
    slug = f"acme-plat-{uuid.uuid4().hex[:6]}"
    _publier_plateforme(appeler, slug)
    _poser_en_base(org, slug, {**CORPS, "limits": {"max_units": 7}}, "Ours")
    ns, _ = _table_de_l_org(org)
    for op, extra in (("get", {}), ("versions", {}), ("run", {"datastore": ns})):
        with pytest.raises(AuthzDenied) as e:
            appeler(MEMBRE, org, op=op, slug=slug, **extra)
        assert (e.value.status, _code(e)) == (409, "ambiguous_slug"), op
    plat = appeler(MEMBRE, org, op="run", slug=slug, scope="platform", datastore=ns)
    assert plat["recipe"]["owner_type"] == "platform"
    sienne = appeler(MEMBRE, org, op="run", slug=slug, scope="org", datastore=ns)
    assert (sienne["recipe"]["owner_type"], sienne["recipe"]["title"]) == ("org", "Ours")
    # Les deux restent nommées par `list`, chacune avec son propriétaire.
    listees = [(r["owner_type"], r["title"])
               for r in appeler(MEMBRE, org, op="list")["recipes"] if r["slug"] == slug]
    assert sorted(listees) == [("org", "Ours"), ("platform", "Acme people")]


def test_list_sans_scope_montre_les_recettes_de_la_plateforme(monde):
    appeler, org = monde
    slug = f"acme-plat-{uuid.uuid4().hex[:6]}"
    _publier_plateforme(appeler, slug)
    sans_scope = {r["slug"]: r for r in appeler(MEMBRE, org, op="list")["recipes"]}
    assert sans_scope[slug]["owner_type"] == "platform"
    assert slug not in {r["slug"] for r in appeler(MEMBRE, org, op="list",
                                                   scope="org")["recipes"]}
    assert slug in {r["slug"] for r in appeler(MEMBRE, org, op="list",
                                               scope="platform")["recipes"]}


def test_la_route_rest_rend_les_recettes_de_la_plateforme(monde):
    """`GET /api/platform/recipes` : la fiche publiée, son affichage dérivé du corps, ou
    écrit ; jamais une recette d'org."""
    from oto_mcp.capabilities import recipes
    from oto_mcp.capabilities._types import ResolvedCtx
    appeler, org = monde
    derivee = f"acme-plat-{uuid.uuid4().hex[:6]}"
    decrite = f"acme-plat-{uuid.uuid4().hex[:6]}"
    d_org = f"acme-org-{uuid.uuid4().hex[:6]}"
    _publier_plateforme(appeler, derivee)
    _publier_plateforme(appeler, decrite, display={"steps": [
        {"label": "Find people", "credits": 0.5, "per": "contact"}],
        "saves": "one call a page"})
    appeler(MEMBRE, org, op="create", slug=d_org, title="Org only", recipe=CORPS)
    out = asyncio.run(recipes._platform_recipes(ResolvedCtx(sub=MEMBRE, org_id=org),
                                                recipes.PlatformRecipesInput()))
    recipes.OrgRecipes.model_validate(out)
    par_slug = {r["slug"]: r for r in out["recipes"]}
    assert d_org not in par_slug
    assert par_slug[derivee]["status"] == "published"
    assert par_slug[derivee]["published_version"] == 1
    assert par_slug[derivee]["display"]["source"] == "derived"
    assert par_slug[decrite]["display"] == {
        "steps": [{"label": "Find people", "detail": "", "credits": 0.5, "per": "contact"}],
        "saves": "one call a page", "source": "described"}
    assert "body" not in par_slug[derivee]
