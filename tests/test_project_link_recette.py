"""Un projet lie une RECETTE de son org, par son slug (`target_type="recette"`).

Sur le montage réel (PostgreSQL) : la pose vérifie que la recette est dans l'org du
projet et que le lieur en est membre ; la lecture pose `title` ; l'audit rend mort un
lien dont la recette a disparu ; la copie dans une autre org n'emporte pas un pointeur
qu'elle ne résoudrait pas ; l'unlink retire le lien.
"""
from __future__ import annotations

import uuid

import pytest

_CORPS = {"kind": "code", "sources": {"recipe.py": "async def run(oto, params):\n    return {}\n"},
          "entrypoint": "recipe:run", "params": {}, "limits": {"max_units": 1}}


@pytest.fixture(scope="module")
def monde(live):
    """Moi, dans l'org A, qui a une recette ; un tiers, dans l'org V, qui a la sienne."""
    from oto_mcp import db, org_store
    from oto_mcp.db import recipes as db_recipes

    u = uuid.uuid4().hex[:8]
    moi, tiers = f"moi_{u}", f"tiers_{u}"
    for s in (moi, tiers):
        db.upsert_user(s, email=f"{s}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=moi)
    org_store.add_org_member(org_a, moi)
    org_store.set_active_org(moi, org_a)
    org_v = org_store.create_org(f"v_{u}", created_by=tiers)
    org_store.add_org_member(org_v, tiers)
    for org, slug, titre, qui in ((org_a, f"rec-a-{u}", "Recette A", moi),
                                  (org_v, f"rec-v-{u}", "Recette secrète de V", tiers)):
        db_recipes.create_recipe(owner_type="org", owner_id=str(org), slug=slug, title=titre,
                                 description="", created_by=qui, body=_CORPS, note=None)
    return {"moi": moi, "tiers": tiers, "org_a": org_a, "org_v": org_v,
            "rec_a": f"rec-a-{u}", "rec_v": f"rec-v-{u}",
            "projet": db.create_project("org", str(org_a), f"p_{u}", created_by=moi)}


def _op(m, op: str, ref: str, projet=None) -> dict:
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    return P._project(ResolvedCtx(sub=m["moi"], org_id=m["org_a"]),
                      P.ProjectInput(op=op, project_id=projet or m["projet"],
                                     target_type="recette", target_ref=ref))


def test_une_recette_de_l_org_se_lie_et_se_lit_avec_son_titre(monde):
    out = _op(monde, "link", monde["rec_a"])
    lien = next(l for l in out["links"] if l["target_type"] == "recette")
    assert lien["target_ref"] == monde["rec_a"]
    assert lien["title"] == "Recette A"
    assert out["link_status"] == "created"


@pytest.mark.parametrize("cle", ["rec_v", None])
def test_une_recette_d_ailleurs_ou_absente_est_refusee_pareil(monde, cle):
    from oto_mcp.capabilities._types import AuthzDenied
    ref = monde[cle] if cle else "nexiste-pas"
    with pytest.raises(AuthzDenied) as e:
        _op(monde, "link", ref)
    assert (e.value.status, e.value.code) == (404, "unknown_recipe")
    # Le message ne dit rien de la recette d'ailleurs.
    assert "secrète" not in str(e.value)


def test_un_lien_dont_la_recette_a_disparu_est_mort_a_l_audit(monde):
    from oto_mcp import db, project_audit
    db.add_project_link(monde["projet"], "recette", "disparue", None)   # posé à la main
    liens = db.list_project_links(monde["projet"])
    assert "title" not in next(l for l in liens if l["target_ref"] == "disparue")
    morts = {d["target_ref"]: d["why"]
             for d in project_audit.audit_project(monde["projet"], liens)["dead_links"]}
    assert morts["disparue"] == "la recette pointée n'existe plus dans l'org du projet"
    assert monde["rec_a"] not in morts
    # L'audit léger (liste de projets) le voit aussi : aucune requête de plus.
    legers = project_audit.audit_project(monde["projet"], liens, light=True)["dead_links"]
    assert "disparue" in {d["target_ref"] for d in legers}
    db.remove_project_link(monde["projet"], "recette", "disparue")


def test_la_copie_dans_une_autre_org_n_emporte_pas_la_recette(monde):
    from oto_mcp import db, org_store
    org_c = org_store.create_org(f"c_{uuid.uuid4().hex[:6]}", created_by=monde["moi"])
    org_store.add_org_member(org_c, monde["moi"])
    _op(monde, "link", monde["rec_a"])
    ailleurs, warnings = db.duplicate_project(monde["projet"], "copie", "org", str(org_c),
                                              copied_by=monde["moi"], lisible=lambda t, r: True)
    assert not any(l["target_type"] == "recette" for l in db.list_project_links(ailleurs))
    assert any("recette" in w for w in warnings)
    # Dans la même org, le pointeur suit.
    ici, _ = db.duplicate_project(monde["projet"], "copie2", "org", str(monde["org_a"]),
                                  copied_by=monde["moi"], lisible=lambda t, r: True)
    assert any(l["target_type"] == "recette" and l.get("title") == "Recette A"
               for l in db.list_project_links(ici))


def test_l_unlink_retire_le_lien(monde):
    _op(monde, "link", monde["rec_a"])
    out = _op(monde, "unlink", monde["rec_a"])
    assert out["removed"] == 1
    assert not any(l["target_type"] == "recette" for l in out["links"])


def test_make_private_ne_vaut_pas_pour_une_recette(monde):
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    with pytest.raises(AuthzDenied) as e:
        P._project(ResolvedCtx(sub=monde["moi"], org_id=monde["org_a"]),
                   P.ProjectInput(op="link", project_id=monde["projet"], target_type="recette",
                                  target_ref=monde["rec_a"], make_private=True))
    assert e.value.code == "make_private_not_applicable"
