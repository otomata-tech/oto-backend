"""Publier un projet sans login exige d'être membre de l'org qui le possède (#1176).

Le trou : le rôle `manager` (gérant) est grantable à quiconque, membre ou non de l'org
propriétaire, et il suffisait (`can_govern`) pour publier. Or l'endpoint MCP publié
résout clés et quota SOUS L'ORG PROPRIÉTAIRE : un gérant extérieur ouvrait au public
l'usage des clés d'une org dont il ne fait pas partie.

Ce qui est prouvé ici, sur une VRAIE base et par le chemin servi (règle d'autz puis
handler), pour CHAQUE chemin de publication :

1. un gérant non membre est refusé (403 `publish_requires_membership`) — `oto_resource`
   audience secret/public (surfaces héritée et stricte), `oto_project op=publish_mcp`
   (tous modes), `oto_doc op=set_public`, fichier public — et rien n'est publié ;
2. un gérant membre de l'org propriétaire publie ;
3. la dépublication (`audience=private`, `unpublish_mcp`, page ou fichier refermés)
   reste ouverte au gérant extérieur ;
4. le gérant extérieur garde le reste : modifier, partager à une personne ;
5. projet d'équipe → l'org PARENTE suffit ; projet personnel → le propriétaire seul ;
   escalade plateforme → passe.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, db, group_store, org_store, ownership
from oto_mcp.capabilities import media_and_files as F
from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities import resources_v2 as R2
from oto_mcp.capabilities._authz import RESOURCE_GOVERN
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.docs import core as D

CODE = "publish_requires_membership"
PROPRIO, MEMBRE, EXTERNE = "u-1176-proprio", "u-1176-membre", "u-1176-externe"
COLLEGUE, AUTEUR, SUPER = "u-1176-collegue", "u-1176-auteur", "u-1176-super"
DEST = "u-1176-dest"
OUTILS = ["oto_doc"]


@pytest.fixture(scope="module")
def monde(live):
    for sub in (PROPRIO, MEMBRE, EXTERNE, COLLEGUE, AUTEUR, SUPER, DEST):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=sub)
    db.set_user_role(SUPER, "super_admin")
    x = org_store.create_org("Org propriétaire", created_by=PROPRIO)
    y = org_store.create_org("Org extérieure", created_by=EXTERNE)
    org_store.add_org_member(x, PROPRIO, "org_admin")
    org_store.add_org_member(x, MEMBRE)
    org_store.add_org_member(x, COLLEGUE)
    org_store.add_org_member(x, AUTEUR)
    org_store.add_org_member(y, EXTERNE)
    equipe = group_store.create_group(x, "Équipe", created_by=PROPRIO)

    def projet(owner_type, owner_id, nom, **kw) -> int:
        return int(db.create_project(owner_type, owner_id, nom, created_by=PROPRIO, **kw))

    p_org = projet("org", str(x), "Projet d'org")
    p_equipe = projet("group", str(equipe), "Projet d'équipe")
    p_perso = int(db.create_project("user", AUTEUR, "Projet perso", created_by=AUTEUR,
                                    context_org_id=x))
    # Le gérant extérieur et le gérant membre, sur le projet d'org.
    for sub in (EXTERNE, MEMBRE):
        ownership.grant("project", str(p_org), "user", sub, role="manager",
                        granted_by=PROPRIO)
    # Projet d'équipe : un membre de l'org HORS de l'équipe, et l'extérieur.
    for sub in (COLLEGUE, EXTERNE):
        ownership.grant("project", str(p_equipe), "user", sub, role="manager",
                        granted_by=PROPRIO)
    # Projet perso : un membre de l'org de rangement, gérant, n'en est pas propriétaire.
    ownership.grant("project", str(p_perso), "user", MEMBRE, role="manager",
                    granted_by=AUTEUR)
    page = db.create_doc(p_org, "Page", body_md="corps", created_by=PROPRIO)
    fichier = db.add_project_file(p_org, "projets/1176/f.pdf", "f.pdf", created_by=PROPRIO)
    return {"x": x, "y": y, "org": p_org, "equipe": p_equipe, "perso": p_perso,
            "page": page, "fichier": int(fichier["id"])}


def _perso(sub) -> int:
    return org_store.ensure_home_org(sub)


def _org_de(sub, m) -> int:
    """L'org d'où `sub` agit : l'org propriétaire s'il en est membre, sinon son org
    perso (où un projet partagé à la personne est servi)."""
    return m["x"] if sub in (PROPRIO, MEMBRE, COLLEGUE, AUTEUR) else _perso(sub)


def _essai(fn):
    try:
        return ("ok", fn())
    except AuthzDenied as e:
        return ("refus", e.status, e.code)


def _ressource(sub, surface=R, **args):
    """`oto_resource` (ou `_v2`) par le chemin servi : règle d'autz PUIS handler."""
    inp = (R.ResourceInput if surface is R else R2.ResourceInputV2)(**args)
    def _go():
        ctx = RESOURCE_GOVERN()(RawCtx(sub=sub), inp)
        return R._resources(ctx, inp)
    return _essai(_go)


def _projet(sub, m, **args):
    return _essai(lambda: P._project(ResolvedCtx(sub=sub, org_id=_org_de(sub, m)),
                                     P.ProjectInput(**args)))


def _doc(sub, m, **args):
    return _essai(lambda: D._doc(ResolvedCtx(sub=sub, org_id=_org_de(sub, m)),
                                 D.DocInput(**args)))


def _acces(pid) -> str:
    return db.get_project_by_id(pid)["mcp_access"]


@pytest.fixture
def depublie(monde):
    """Chaque cas part d'un projet non publié, et le laisse tel."""
    for k in ("org", "equipe", "perso"):
        db.set_project_mcp_publication(monde[k], slug=None, access="off", tools=[])
    yield monde
    for k in ("org", "equipe", "perso"):
        db.set_project_mcp_publication(monde[k], slug=None, access="off", tools=[])


# ── 1. Le gérant extérieur ne publie par AUCUN chemin ─────────────────────────

@pytest.mark.parametrize("audience", ["secret", "public"])
@pytest.mark.parametrize("surface", [R, R2], ids=["heritee", "v2"])
def test_gerant_exterieur_refuse_par_oto_resource(depublie, audience, surface):
    m = depublie
    r = _ressource(EXTERNE, surface, op="share", resource_type="project",
                   resource_id=str(m["org"]), audience=audience, mcp_slug="exterieur-1176",
                   mcp_tools=OUTILS)
    assert r == ("refus", 403, CODE)
    assert _acces(m["org"]) == "off"


@pytest.mark.parametrize("mode", ["secret", "anonymous", "org"])
def test_gerant_exterieur_refuse_par_publish_mcp(depublie, mode):
    m = depublie
    r = _projet(EXTERNE, m, op="publish_mcp", project_id=m["org"], mcp_access=mode,
                mcp_slug="exterieur-1176", mcp_tools=OUTILS)
    assert r == ("refus", 403, CODE)
    assert _acces(m["org"]) == "off"


def test_gerant_exterieur_ne_rend_pas_une_page_publique(depublie):
    m = depublie
    assert _doc(EXTERNE, m, op="set_public", doc_id=m["page"], public=True) == (
        "refus", 403, CODE)
    assert not db.get_doc_by_id(m["page"]).get("public_token")


def test_gerant_exterieur_ne_rend_pas_un_fichier_public(depublie, monkeypatch):
    m = depublie
    from oto_mcp import media_store
    ouverts = []
    monkeypatch.setattr(media_store, "make_public",
                        lambda key: ouverts.append(key) or "https://exemple.test/f.pdf")
    monkeypatch.setattr(media_store, "make_private", lambda key: None)
    monkeypatch.setattr(media_store, "presign_get", lambda key: "https://exemple.test/s")
    monkeypatch.setattr(access, "current_org", lambda sub: _org_de(sub, m))
    inp = F.ProjectFilePublicInput(project_id=m["org"], file_id=m["fichier"], public=True)
    r = _essai(lambda: F._file_public(ResolvedCtx(sub=EXTERNE), inp))
    assert r == ("refus", 403, CODE)
    assert ouverts == []        # l'ACL n'a pas bougé
    # Refermer reste ouvert ; le membre ouvre.
    ferme = F.ProjectFilePublicInput(project_id=m["org"], file_id=m["fichier"], public=False)
    assert _essai(lambda: F._file_public(ResolvedCtx(sub=EXTERNE), ferme))[0] == "ok"
    assert _essai(lambda: F._file_public(ResolvedCtx(sub=MEMBRE), inp))[0] == "ok"
    assert ouverts == ["projets/1176/f.pdf"]


# ── 2-3. Le membre publie ; l'extérieur dépublie ──────────────────────────────

def test_gerant_membre_publie_et_l_exterieur_depublie(depublie):
    m = depublie
    r = _ressource(MEMBRE, op="share", resource_type="project", resource_id=str(m["org"]),
                   audience="secret", mcp_tools=OUTILS)
    assert r[0] == "ok", r
    assert _acces(m["org"]) == "secret"
    r = _ressource(EXTERNE, op="share", resource_type="project", resource_id=str(m["org"]),
                   audience="private")
    assert r[0] == "ok", r
    assert _acces(m["org"]) == "off"
    # Même chose par oto_project.
    r = _projet(MEMBRE, m, op="publish_mcp", project_id=m["org"], mcp_access="secret",
                mcp_tools=OUTILS)
    assert r[0] == "ok", r
    r = _projet(EXTERNE, m, op="unpublish_mcp", project_id=m["org"])
    assert r[0] == "ok", r
    assert _acces(m["org"]) == "off"


def test_page_le_membre_ouvre_l_exterieur_referme(depublie):
    m = depublie
    assert _doc(MEMBRE, m, op="set_public", doc_id=m["page"], public=True)[0] == "ok"
    assert db.get_doc_by_id(m["page"]).get("public_token")
    assert _doc(EXTERNE, m, op="set_public", doc_id=m["page"], public=False)[0] == "ok"
    assert not db.get_doc_by_id(m["page"]).get("public_token")


# ── 4. Le gérant extérieur garde le reste ─────────────────────────────────────

def test_gerant_exterieur_modifie_et_partage_a_une_personne(depublie):
    m = depublie
    assert _projet(EXTERNE, m, op="update", project_id=m["org"], name="Renommé")[0] == "ok"
    r = _ressource(EXTERNE, op="share", resource_type="project", resource_id=str(m["org"]),
                   audience="person", email=f"{DEST}@exemple.test", role="viewer")
    assert r[0] == "ok", r
    assert ownership.can_access(DEST, "project", str(m["org"]), "read")


# ── 5. Équipe, perso, plateforme ──────────────────────────────────────────────

def test_projet_d_equipe_l_org_parente_suffit(depublie):
    m = depublie
    r = _ressource(EXTERNE, op="share", resource_type="project",
                   resource_id=str(m["equipe"]), audience="secret", mcp_tools=OUTILS)
    assert r == ("refus", 403, CODE)
    # Membre de l'org, hors de l'équipe : la publication ne se restreint pas à l'équipe.
    assert ownership.can_publish(COLLEGUE, "project", str(m["equipe"]))
    assert not ownership.can_publish(EXTERNE, "project", str(m["equipe"]))


def test_projet_perso_le_proprietaire_seul(depublie):
    m = depublie
    r = _ressource(MEMBRE, op="share", resource_type="project", resource_id=str(m["perso"]),
                   audience="secret", mcp_tools=OUTILS)
    assert r == ("refus", 403, CODE)      # membre de l'org de rangement, pas propriétaire
    assert _acces(m["perso"]) == "off"
    r = _ressource(AUTEUR, op="share", resource_type="project", resource_id=str(m["perso"]),
                   audience="secret", mcp_tools=OUTILS)
    assert r[0] == "ok", r
    assert _acces(m["perso"]) == "secret"


def test_escalade_plateforme_publie(depublie):
    m = depublie
    assert not _membre_direct(SUPER, m["x"])     # aucune ligne d'appartenance
    r = _ressource(SUPER, op="share", resource_type="project", resource_id=str(m["org"]),
                   audience="secret", mcp_tools=OUTILS)
    assert r[0] == "ok", r
    assert _acces(m["org"]) == "secret"


def _membre_direct(sub, org_id) -> bool:
    """Appartenance DIRECTE (ligne `org_members`), sans l'escalade plateforme."""
    return any(int(o["org_id"]) == int(org_id) for o in org_store.list_orgs_for_user(sub))
