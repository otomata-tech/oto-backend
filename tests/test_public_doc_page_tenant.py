"""La page publique d'un doc partagé porte la marque de son produit, pas la nôtre.

Dernière des trois surfaces du chantier, et la plus exposée : **n'importe qui avec le
jeton**, sans authentification, sans compte, sans même un sous-domaine qui dirait de qui
il s'agit. Le handler ne recevait QUE le jeton — la lecture faisait
`SELECT title, body_md, updated_at`, et rien d'autre ne remontait. Aucune information de
tenant n'était disponible ; c'est ce que ce lot change, en joignant le projet et son org.

Le lien vers cette page était déjà tenant-isé (`links.link_for` rend `None` chez un
tenant sans patron) : un compte tiers n'obtient donc même pas d'adresse. Mais la PAGE
restait servie à notre marque pour qui a le jeton — et un jeton se transfère.

Même patron que les deux autres surfaces : la donnée décide, nos couleurs ne partent
jamais, et l'absence de tenant rend notre marque explicitement.
"""
from __future__ import annotations

import pytest

from oto_mcp import public_doc_page

NOS_MARQUES = ("oto.cx", "Oto", "otomata")


@pytest.fixture
def acme():
    from oto_mcp import email_brand, tenancy
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme", "issuer": "https://auth.acme.test/oidc"}])))
    yield email_brand.marque("acme")
    tenancy.install(avant)


def test_le_rendu_porte_la_marque_qu_on_lui_donne(acme):
    from oto_mcp import brand
    m = brand.nom_affiche(acme)
    page = public_doc_page.render(title="Notes", body_md="Du texte.", marque=m)
    assert "Acme" in page
    for mot in NOS_MARQUES:
        assert mot not in page, f"« {mot} » servi au lecteur d'un partenaire"


def test_la_page_INTROUVABLE_aussi(acme):
    """Elle se rend AVANT toute lecture réussie — c'est celle qu'un jeton périmé sert,
    donc celle qu'on voit le plus souvent depuis l'extérieur."""
    from oto_mcp import brand
    page = public_doc_page.render_missing(marque=brand.nom_affiche(acme))
    for mot in NOS_MARQUES:
        assert mot not in page


def test_nos_couleurs_ne_partent_pas(acme):
    from oto_mcp import brand
    page = public_doc_page.render(title="Notes", body_md="", marque=brand.nom_affiche(acme))
    assert "#f0b41e" not in page and "#fefcf5" not in page


def test_sans_marque_la_page_est_INCHANGEE_a_l_octet():
    """Le garde-fou d'inertie : le défaut est notre marque, explicitement."""
    page = public_doc_page.render(title="Notes", body_md="Du texte.")
    assert "Partagé via" in page and "oto.cx" in page and "#f0b41e" in page


def test_la_lecture_par_jeton_remonte_le_PROPRIETAIRE(live):
    """Sans ça, aucune marque n'est résolvable : le jeton ne dit rien de qui partage.

    Éprouvé sur une vraie base parce que c'est une JOINTURE : une colonne absente ou
    renommée rend `None` au lieu de lever, et la page retomberait sur notre marque en
    silence — le défaut exact que ce lot ferme."""
    from oto_mcp import db
    from oto_mcp.db import _conn

    with _conn._connect() as c:
        c.execute("INSERT INTO orgs (id, name) VALUES (777, 'Acme Inc') "
                  "ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO projects (id, name, owner_type, owner_id) "
                  "VALUES (888, 'P', 'org', '777') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO docs (id, project_id, title, body_md, public_token_hash) "
                  "VALUES (999, 888, 'Notes', 'x', encode(sha256('jeton-test'::bytea), 'hex')) "
                  "ON CONFLICT (id) DO NOTHING")
        c.commit()

    doc = db.get_doc_by_public_token("jeton-test")
    assert doc is not None
    assert doc["title"] == "Notes"
    assert doc.get("owner_type") == "org" and str(doc.get("owner_id")) == "777"


def test_le_HANDLER_sert_la_marque_du_proprietaire(acme, monkeypatch):
    """Le rendu sait porter une marque ; encore faut-il que quelqu'un la lui donne.
    Une fonction prête que personne n'appelle est le défaut classique d'un lot pareil :
    tout est vert, et la page servie n'a pas changé d'un octet."""
    import asyncio

    from starlette.requests import Request
    from oto_mcp import db
    from oto_mcp.api import public as P

    monkeypatch.setattr(db, "get_doc_by_public_token",
                        lambda t: {"title": "Notes", "body_md": "x", "updated_at": None,
                                   "owner_type": "org", "owner_id": "42"})
    monkeypatch.setattr(db, "org_tenant_slug", lambda org_id: "acme")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "method": "GET", "path": "/p/d/jeton",
                   "query_string": b"", "root_path": "", "scheme": "https",
                   "server": ("test", 443), "http_version": "1.1",
                   "headers": [(b"accept", b"text/html")],
                   "path_params": {"token": "jeton"}}, receive)
    resp = asyncio.run(P.public_doc_view(req))
    page = bytes(resp.body).decode()
    assert "Acme" in page
    for mot in NOS_MARQUES:
        assert mot not in page, f"« {mot} » servi au lecteur d'un partenaire"
