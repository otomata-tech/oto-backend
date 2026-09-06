"""La vitrine anonyme de la bibliothèque sert du contenu, jamais des identifiants.

`guide_library` porte, sur la même ligne, le guide publié (public par destination)
et ce qui le rattache à quelqu'un : `published_by` — un identifiant d'utilisateur
de la forme `<tenant>:<sub>`, dont le préfixe nomme le tenant qui a publié —,
`author_org_id` / `source_org_id`, `id` et `forked_from`. Les deux routes
anonymes servaient les deux, à un appelant sans jeton.

Ce qui est figé ici n'est pas la liste des champs retirés — elle serait muette sur
la colonne ajoutée demain, le seul cas qu'on ne relira pas. C'est le sens de la
garde : **rien ne sort qui ne soit nommé**. D'où le test central, celui du champ
interne inconnu : la doublure de store rend un champ que personne n'a prévu, et il
ne doit pas atteindre la réponse.

Les doublures rendent DÉLIBÉRÉMENT une ligne complète (tous les identifiants) :
sans ça, le banc passerait aussi bien sans la projection qu'avec.
"""
from __future__ import annotations

import asyncio
import json
import types

from oto_mcp.api import routes as api_routes
from oto_mcp.api import public as api_public


# Les identifiants qui ne doivent jamais atteindre un appelant anonyme.
IDENTIFIANTS = ("id", "author_org_id", "source_org_id", "forked_from", "published_by")

# Ce que le seul consommateur réel lit vraiment (oto.cx) — `DoctrineView.vue` pour
# le détail, `scripts/refresh-catalog.mjs` pour le cliché du build. La garde vaut
# dans les deux sens : retirer un de ces champs éteindrait une page publique.
LU_PAR_LA_VITRINE = ("slug", "title", "description", "author_kind", "author_display",
                     "category", "tags", "version", "updated_at")


def _ligne_complete(**extra) -> dict:
    """Une ligne `guide_library` telle que le store la rend : tout, identifiants compris."""
    ligne = {
        "id": 53, "slug": "un-guide", "title": "Un guide", "description": "d",
        "body_md": "# corps", "slots": [], "author_kind": "otomata",
        "author_org_id": 233, "author_display": "Otomata", "category": "GTM",
        "tags": ["gtm"], "visibility": "public", "source_org_id": 233,
        "source_slug": "un-guide", "forked_from": 7, "version": 4,
        "published_by": "un-tenant:zt57humow545",
        "created_at": "2026-08-29 16:03:06", "updated_at": "2026-08-30 07:49:34",
    }
    ligne.update(extra)
    return ligne


def _req(path: str, **params):
    from starlette.requests import Request
    return Request({
        "type": "http", "method": "GET", "path": path, "query_string": b"",
        "root_path": "", "scheme": "http", "server": ("test", 80),
        "http_version": "1.1", "headers": [],   # ANONYME : aucun en-tête d'auth
        "path_params": params,
    })


def _routes():
    verifier = types.SimpleNamespace()
    out = {}
    for r in api_routes.make_routes(verifier):
        if getattr(r, "path", None) in ("/api/guide-library", "/api/guide-library/{slug}"):
            if "GET" in getattr(r, "methods", set()):
                out[r.path] = r.endpoint
    return out


def _body(resp) -> dict:
    return json.loads(bytes(resp.body).decode())


def test_la_liste_ne_sert_aucun_identifiant(monkeypatch):
    monkeypatch.setattr(api_public.org_store, "list_library",
                        lambda **k: [_ligne_complete()])
    entree = _body(asyncio.run(
        _routes()["/api/guide-library"](_req("/api/guide-library"))))["guides"][0]
    fuites = [c for c in IDENTIFIANTS if c in entree]
    assert not fuites, f"servis à un appelant sans jeton : {fuites}"


def test_le_detail_ne_sert_aucun_identifiant(monkeypatch):
    monkeypatch.setattr(api_public.org_store, "get_library_entry",
                        lambda **k: _ligne_complete())
    entree = _body(asyncio.run(_routes()["/api/guide-library/{slug}"](
        _req("/api/guide-library/un-guide", slug="un-guide"))))
    fuites = [c for c in IDENTIFIANTS if c in entree]
    assert not fuites, f"servis à un appelant sans jeton : {fuites}"
    assert entree["body_md"] == "# corps", "le corps EST ce que la vitrine vient chercher"


def test_un_champ_interne_inconnu_ne_sort_pas(monkeypatch):
    """Le cœur de la garde : elle nomme ce qui SORT, pas ce qui reste.

    Une colonne ajoutée à `guide_library` — ici `owner_email` — n'a aucune raison
    d'atteindre la vitrine du seul fait qu'elle existe. Si ce test tombe, la
    projection est redevenue une liste de champs retirés.
    """
    monkeypatch.setattr(api_public.org_store, "list_library",
                        lambda **k: [_ligne_complete(owner_email="qui@exemple.test")])
    monkeypatch.setattr(api_public.org_store, "get_library_entry",
                        lambda **k: _ligne_complete(owner_email="qui@exemple.test"))
    liste = _body(asyncio.run(
        _routes()["/api/guide-library"](_req("/api/guide-library"))))["guides"][0]
    detail = _body(asyncio.run(_routes()["/api/guide-library/{slug}"](
        _req("/api/guide-library/un-guide", slug="un-guide"))))
    assert "owner_email" not in liste and "owner_email" not in detail


def test_la_vitrine_garde_ce_qu_elle_affiche(monkeypatch):
    """Le contrat ment dans les deux sens : trop servir expose, pas assez éteint
    une page publique. `oto.cx` lit ces champs — ils doivent rester."""
    monkeypatch.setattr(api_public.org_store, "list_library",
                        lambda **k: [_ligne_complete()])
    entree = _body(asyncio.run(
        _routes()["/api/guide-library"](_req("/api/guide-library"))))["guides"][0]
    manquants = [c for c in LU_PAR_LA_VITRINE if c not in entree]
    assert not manquants, f"lus par oto.cx, plus servis : {manquants}"


def test_la_copie_depreciee_est_projetee_elle_aussi(monkeypatch):
    """`avec_les_deux_noms` republie la liste sous `doctrines` (préavis #519). Une
    projection posée après le doublage aurait laissé la copie complète — et c'est
    justement le nom que le build de la vitrine appelle encore."""
    monkeypatch.setattr(api_public.org_store, "list_library",
                        lambda **k: [_ligne_complete()])
    payload = _body(asyncio.run(
        _routes()["/api/guide-library"](_req("/api/guide-library"))))
    assert "doctrines" in payload, "le préavis #519 sert encore l'ancien nom"
    fuites = [c for c in IDENTIFIANTS if c in payload["doctrines"][0]]
    assert not fuites, f"servis sous l'ancien nom : {fuites}"
