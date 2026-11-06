"""Ce qui est partagé à une personne se liste dans l'org où c'est rangé, pas dans sa maison.

Projets, tableaux, pages reçues (`scope="org"`) et apps (06/11/2026).

Plus d'org perso depuis le 05/11/2026 : la maison d'un compte est son org ACTIVE, une
org ordinaire. Tout partage nominatif de projet s'y listait. Un membre de deux orgs A
et B, actif dans B, à qui l'on partage EN PERSONNE un projet de A, le voyait donc dans
la liste de B : le projet d'un espace affiché dans un autre.

La règle (`ownership.projets_partages_a_moi_ici`) : un projet partagé nominativement se
liste dans son org de rangement (org propriétaire, org de l'équipe propriétaire, org
de création d'un projet perso) quand le bénéficiaire en est membre, et là seulement.
Sinon (collaborateur extérieur, projet sans org de rangement), dans sa maison, comme
avant. La recherche (`accessible_project_ids`) suit : « cherchable ⇔ lisible ».

Base réelle, vraie face servie (`POST /api/me/projects` op=list sous `X-Oto-Org`).
"""
from __future__ import annotations

import uuid

import pytest


def _nom() -> str:
    return "pr-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, group_store, org_store, ownership
    u = uuid.uuid4().hex[:6]
    moi, tiers = f"usr_rang_moi_{u}", f"usr_rang_tiers_{u}"
    for s in (moi, tiers):
        db.upsert_user(s, email=f"{s}@t.invalid", name=s)
    a = org_store.create_org(f"A rang {u}", created_by=tiers)
    b = org_store.create_org(f"B rang {u}", created_by=moi)
    c = org_store.create_org(f"C rang {u}", created_by=tiers)
    org_store.add_org_member(a, tiers, "org_admin")
    org_store.add_org_member(a, moi)
    org_store.add_org_member(b, moi, "org_admin")
    org_store.add_org_member(c, tiers, "org_admin")        # MOI n'est pas membre de C
    equipe = group_store.create_group(a, f"E rang {u}", created_by=tiers)  # MOI hors équipe

    def projet(owner_type, owner_id, ctx=None):
        pid = int(db.create_project(owner_type, str(owner_id), _nom(), created_by=tiers,
                                    context_org_id=ctx))
        ownership.grant("project", str(pid), "user", moi, role="viewer", granted_by=tiers)
        return pid

    p = {
        "org_a": projet("org", a),                 # rangé dans A, j'en suis membre
        "equipe_a": projet("group", equipe),       # rangé dans A par son équipe
        "perso_a": projet("user", tiers, ctx=a),   # projet perso d'un tiers créé dans A
        "org_c": projet("org", c),                 # rangé dans C, je n'en suis pas membre
    }
    tds = ownership.TYPE_RESSOURCE_DATASTORE

    def tableau(owner_type, owner_id, ctx=None):
        tid = db.create_datastore(owner_type, str(owner_id), _nom(), context_org_id=ctx)
        ownership.grant(tds, str(tid), "user", moi, "read", granted_by=tiers)
        return int(tid)

    t = {"org_a": tableau("org", a), "perso_a": tableau("user", tiers, ctx=a),
         "org_c": tableau("org", c)}

    def page(pid):
        did = int(db.create_doc(pid, _nom(), body_md="x", created_by=tiers))
        ownership.grant("doc", str(did), "user", moi, role="viewer", granted_by=tiers)
        return did

    from oto_mcp.capabilities import apps as _apps  # noqa: F401 — enregistre `app`
    from oto_mcp.capabilities.docs import common as _docs  # noqa: F401 — enregistre `doc`
    # Des projets NON partagés : seule la page l'est.
    d = {"dans_a": page(int(db.create_project("org", str(a), _nom(), created_by=tiers))),
         "dans_c": page(int(db.create_project("org", str(c), _nom(), created_by=tiers)))}

    def app(owner_type, owner_id, ctx):
        r = db.apps.create((owner_type, str(owner_id)), ctx, title=_nom(), manifest={},
                           html="<p>x</p>", thumbnail=None, by=tiers)
        ownership.grant("app", str(r["id"]), "user", moi, role="viewer", granted_by=tiers)
        return int(r["id"])

    ap = {"org_a": app("org", a, a), "org_c": app("org", c, c)}
    org_store.set_active_org(moi, b)                # ma maison = B
    assert org_store.home_org(moi) == b
    return {"moi": moi, "a": a, "b": b, "c": c, "p": p, "t": t, "d": d, "ap": ap}


@pytest.fixture(scope="module")
def client(monde):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes
    moi = monde["moi"]

    class _Claims:
        claims = {"sub": moi, "email": f"{moi}@t.invalid", "name": moi}

    class _Verifier:
        async def verify_token(self, token):
            return _Claims()

    # `X-Oto-Org` est lu par `ViewAsMiddleware`, comme en production.
    return TestClient(api_routes.ViewAsMiddleware(
        Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)),
        verifier=_Verifier()))


def _servis(client, monde, org: int) -> set[int]:
    r = client.post("/api/me/projects", json={"op": "list"},
                    headers={"Authorization": f"Bearer {monde['moi']}",
                             "X-Oto-Org": str(org)})
    assert r.status_code == 200, r.text
    return {int(x["id"]) for x in r.json()["projects"]}


def _ids_liste(sub: str, org: int) -> set[int]:
    """Les ids que rend `op=list` : possédés ici + reçus ici — les deux mêmes seams
    que la capacité."""
    from oto_mcp import ownership
    own = {int(r["id"]) for r in ownership.projets_possedes_ici(sub, org)}
    return own | {int(r["id"]) for r in ownership.projets_recus_ici(sub, org)}


def test_le_partage_nominatif_se_liste_dans_son_org_de_rangement(monde):
    vus = _ids_liste(monde["moi"], monde["a"])
    for k in ("org_a", "equipe_a", "perso_a"):
        assert monde["p"][k] in vus, f"{k} absent de la liste de son org"


def test_il_ne_se_liste_plus_dans_la_maison(monde):
    """Rouge sur le code d'avant : la maison (B) listait les projets de A."""
    vus = _ids_liste(monde["moi"], monde["b"])
    fuite = vus & {monde["p"][k] for k in ("org_a", "equipe_a", "perso_a")}
    assert not fuite, f"projets de A listés dans B : {sorted(fuite)}"


def test_collaborateur_exterieur_il_reste_dans_la_maison(monde):
    """MOI n'est pas membre de C : son projet partagé n'a pas d'autre place que la
    maison — la règle d'avant, inchangée."""
    assert monde["p"]["org_c"] in _ids_liste(monde["moi"], monde["b"])
    assert monde["p"]["org_c"] not in _ids_liste(monde["moi"], monde["a"])


def test_la_recherche_suit_la_liste(monde):
    from oto_mcp import ownership
    for org in (monde["a"], monde["b"]):
        cherche = set(ownership.accessible_project_ids(monde["moi"], org))
        assert cherche == _ids_liste(monde["moi"], org), org


def test_la_face_servie_rend_la_meme_chose(monde, client):
    """`POST /api/me/projects` op=list sous `X-Oto-Org` lit les mêmes seams."""
    a, b, p = monde["a"], monde["b"], monde["p"]
    dans_a, dans_b = _servis(client, monde, a), _servis(client, monde, b)
    assert {p["org_a"], p["equipe_a"], p["perso_a"]} <= dans_a
    assert p["org_c"] not in dans_a
    assert not dans_b & {p["org_a"], p["equipe_a"], p["perso_a"]}
    assert p["org_c"] in dans_b


def test_l_acces_par_id_ne_change_pas(monde):
    """Un filtre de liste, jamais un droit : depuis B, le projet de A s'ouvre toujours."""
    from oto_mcp import ownership
    assert ownership.visible_in_org(monde["moi"], monde["b"], "project",
                                    str(monde["p"]["org_a"]))


# ── tableaux, pages, apps : la même règle ──────────────────────────────────────

def _tableaux(client, monde, org: int) -> set[int]:
    r = client.get("/api/datastores", headers={"Authorization": f"Bearer {monde['moi']}",
                                               "X-Oto-Org": str(org)})
    assert r.status_code == 200, r.text
    return {int(x["id"]) for x in r.json()["datastores"]}


def test_tableaux_partages_ranges_dans_leur_org(monde, client):
    from oto_mcp import search
    t = monde["t"]
    dans_a, dans_b = _tableaux(client, monde, monde["a"]), _tableaux(client, monde, monde["b"])
    assert {t["org_a"], t["perso_a"]} <= dans_a and t["org_c"] not in dans_a
    assert not dans_b & {t["org_a"], t["perso_a"]}, "tableaux de A listés dans B"
    assert t["org_c"] in dans_b                     # collaborateur extérieur : maison
    for org, vus in ((monde["a"], dans_a), (monde["b"], dans_b)):
        cherche = {int(r["id"]) for r in search._accessible_namespaces(monde["moi"], org)}
        assert cherche & set(t.values()) == vus & set(t.values()), org


def test_pages_recues_rangees_par_leur_projet(monde):
    from oto_mcp.capabilities.docs import partage
    d = monde["d"]

    def recues(org):
        return {int(x["id"]) for x in partage.recus(monde["moi"], "org", org)["docs"]}
    assert d["dans_a"] in recues(monde["a"]) and d["dans_c"] not in recues(monde["a"])
    assert d["dans_a"] not in recues(monde["b"]) and d["dans_c"] in recues(monde["b"])
    # `scope="me"` et la vue historique ne changent pas : tout ce qui m'est partagé.
    for portee in ("me", None):
        tout = {int(x["id"]) for x in partage.recus(monde["moi"], portee, monde["b"])["docs"]}
        assert set(d.values()) <= tout, portee


def test_apps_partagees_rangees_dans_leur_org(monde):
    from oto_mcp.capabilities import apps
    from oto_mcp.capabilities._types import ResolvedCtx
    ap = monde["ap"]

    def listees(org):
        rows, _ = apps._org_list(ResolvedCtx(sub=monde["moi"], org_id=org), False)
        return {int(r["id"]) for r in rows}
    assert ap["org_a"] in listees(monde["a"]) and ap["org_c"] not in listees(monde["a"])
    assert ap["org_a"] not in listees(monde["b"]) and ap["org_c"] in listees(monde["b"])
