"""« C'est où ? » — l'adresse web d'une page et d'un projet (signal #599).

Le manque remonté le 27/08/2026, après la création d'une page qu'Alexis voulait ouvrir
et transmettre : `oto_doc op=create` rend l'id, le projet et le `rev`, **rien qui dise
où la lire**. Les contournements observés étaient tous mauvais — rendre la page publique
(inacceptable pour de l'interne), ou reconstruire l'adresse en lisant le routeur du
tableau de bord. Le signal le dit mieux que nous : « un patron d'URL deviné ou appris par
cœur dans une consigne est un moule à fabriquer des liens plausibles et faux ».

Ce fichier garde donc trois propriétés du SYSTÈME, pas trois valeurs :

1. **l'adresse est SERVIE, pas devinée** — elle sort du même seam que celle d'un tableau
   (`links.link_for`), donc elle suit le front du lecteur et se corrige en un endroit ;
2. **une projection ne l'emporte jamais** — l'accusé d'écriture est justement le moment
   où l'on demande « c'est où ? », et celui où la projection est la plus agressive ;
3. **pas de patron ⟹ pas de lien** — un lecteur dont le produit n'a pas cette vue
   reçoit `null`, jamais notre domaine. Un lien mort ne se diagnostique pas, il se subit.

⚠️ **Le piège, daté** : jusqu'au 2026-08-28 le patron par défaut d'une page disait
`/docs/{id}` — un chemin que notre propre tableau de bord ne route pas (il ne connaît
que la section `/documents`, sans id, et son attrape-tout renvoie l'inconnu sur
`/overview`). Le lien n'aurait donc pas affiché d'erreur : il aurait ouvert la page
d'accueil en se faisant passer pour la page demandée. Le patron n'avait aucun appelant,
ce qui l'a gardé invisible un mois. D'où le test n°1 ci-dessous : une page s'adresse
DANS son projet, comme le front lui-même l'écrit.
"""
from __future__ import annotations

import pytest

from oto_mcp import config, db, links, ownership, tenancy
from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.docs import core as D

# `_view` lit les partages vivants du projet (#1367) : aucun ici, sans base.
pytestmark = pytest.mark.usefixtures("projets_sans_partages")

CTX = ResolvedCtx(sub="u1", org_id=1)

PAGE = {"id": 662, "project_id": 153, "parent_id": None, "title": "Point commercial",
        "description": None, "position": 0, "body_md": "## Contexte\n\nBla.",
        "kind": "doc", "created_at": "2026-08-27", "updated_at": "2026-08-27"}

PROJET = {"id": 153, "name": "Développement commercial", "icon": None, "brief_md": "",
          "owner_type": "user", "owner_id": "u1", "context_org_id": 1,
          "is_template": False, "mcp_slug": None, "mcp_access": "off",
          "mcp_tools": [], "created_at": "2026-07-01", "updated_at": "2026-08-27"}


@pytest.fixture
def seams_page(monkeypatch):
    monkeypatch.setattr(ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(db, "get_doc_by_id", lambda i: dict(PAGE, id=i))
    monkeypatch.setattr(db, "doc_rev", lambda t, b: "9f2c41a")
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(db, "create_doc",
                        lambda pid, title, parent_id=None, body_md="", kind="doc",
                        created_by=None, description=None, trace=None: 662)
    monkeypatch.setattr(db, "update_doc", lambda *a, **k: None)


@pytest.fixture
def partenaire():
    """Un tenant tiers calqué sur le vrai : ses chemins ne ressemblent pas aux nôtres,
    et il n'a AUCUNE vue de page ni de projet — le cas qui a fabriqué le lien mort."""
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "issuer": "https://auth.acme.test/oidc",
                  "dashboard_url": "https://app.acme.test",
                  "link_paths": {"connectors": "/network/connectors"}}])))
    yield
    tenancy.install(avant)


# ── 1. Une page s'adresse DANS son projet ────────────────────────────────────────────

def test_l_adresse_d_une_page_nomme_son_projet_ET_la_page():
    """Le défaut daté : un chemin `/docs/<id>` n'existe pas côté front. Ce que le front
    écrit lui-même pour ouvrir une page (recherche, boîte de réception, vue projet),
    c'est le projet PUIS la page — une page n'a pas d'écran à elle.

    On vérifie la propriété, pas la chaîne : l'adresse doit porter les DEUX
    identifiants. Un patron qui n'en porterait qu'un ne peut pas ouvrir la page."""
    url = links.link_for("doc", sub=None, id=662, project_id=153)
    assert url is not None
    assert "153" in url and "662" in url
    # Et elle part bien de NOTRE tableau de bord, pas d'une adresse écrite en dur.
    assert url.startswith(config.dashboard_url())


def test_une_page_sans_projet_ne_rend_aucune_adresse():
    """`_render` annule un lien à trous plutôt que de servir `/projects//?doc=662`,
    qui mène à une page d'erreur en se faisant passer pour un lien valide."""
    assert links.link_for("doc", sub=None, id=662, project_id=None) is None


# ── 2. Les surfaces la servent ───────────────────────────────────────────────────────

def test_une_page_creee_rend_son_adresse(seams_page):
    """Le geste exact du signal : créer une page, puis pouvoir dire où elle se lit."""
    out = D._doc(CTX, D.DocInput(op="create", project_id=153, title="Point commercial"))
    assert out["url"] and "153" in out["url"] and "662" in out["url"]


def test_l_adresse_survit_a_la_projection_d_un_accuse(seams_page):
    """Une écriture rend un ACCUSÉ projeté (#530). Si l'adresse tombait avec le corps,
    elle manquerait très exactement au moment où on la demande."""
    out = D._doc(CTX, D.DocInput(op="update", doc_id=662, body_md="corps refondu",
                                 fields=["rev"]))
    assert "body_md" not in out          # la projection a bien mordu
    assert out["url"], "l'adresse doit résister à la projection la plus agressive"


def test_une_page_lue_rend_son_adresse(seams_page):
    out = D._doc(CTX, D.DocInput(op="get", doc_id=662))
    assert out["url"] and "662" in out["url"]


def test_un_projet_rend_son_adresse(monkeypatch):
    """Même question, même réponse, un cran au-dessus : `oto_project op=get`."""
    monkeypatch.setattr(P.db, "get_project_by_id", lambda i: dict(PROJET, id=i))
    monkeypatch.setattr(P.db, "list_project_links", lambda i: [])
    monkeypatch.setattr(P.ownership, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(P, "_require_active_org_visible", lambda ctx, row: None)
    from oto_mcp import project_audit
    monkeypatch.setattr(project_audit, "audit_project",
                        lambda pid, links, **_k: {
                            "dead_links": [], "unbound_slots": [], "inert_procedures": [],
                            "stale_docs": []})
    out = P._project(CTX, P.ProjectInput(op="get", project_id=153))
    assert out["url"] and out["url"].endswith("/projects/153")


# ── 3. Pas de patron ⟹ pas de lien (jamais notre domaine chez un partenaire) ─────────

def test_le_lecteur_d_un_partenaire_ne_recoit_aucune_adresse_plutot_que_la_notre(
        partenaire, seams_page, monkeypatch):
    """La règle de `links.py`, appliquée ici : le produit d'`acme` n'a ni vue de page ni
    vue de projet. Lui servir NOTRE adresse lui proposerait un service qu'il n'a pas ;
    coller notre chemin sous SON domaine fabriquerait un lien mort. Donc : rien.

    La page reste parfaitement lisible et la réponse parfaitement utile — c'est le
    champ `url` qui vaut `null`, pas l'opération qui échoue."""
    ctx = ResolvedCtx(sub="acme:u", org_id=1)
    out = D._doc(ctx, D.DocInput(op="get", doc_id=662))
    assert out["id"] == 662 and out["title"]        # la page est servie normalement
    assert out["url"] is None
    assert links.link_for("project", sub="acme:u", id=153) is None
