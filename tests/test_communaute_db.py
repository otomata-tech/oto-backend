"""La communauté contre un PostgreSQL réel : retraits, installations, tris, vitrine.

Ce qu'on vérifie, c'est ce que la base porte et ce que chaque face SERT :
- une installation (un fork réussi) se note dans l'org où elle est faite, et les
  compteurs comptent des orgs DISTINCTES (`installs_total`, `installs_30d`) ;
- `sort` trie par ces compteurs ou par date de création, et l'absence de `sort` garde
  le tri d'avant ;
- une entrée retirée par le personnel disparaît de la liste, de la lecture et du fork
  pour tous, sauf les admins de l'org autrice et les opérateurs plateforme ; la
  republier ne lève pas le retrait ; la restaurer la rend à tous ;
- la vitrine anonyme ne sert QUE sa liste fermée de champs : jamais le corps, jamais
  un identifiant d'org ou de compte, jamais une entrée `unlisted` ou retirée ;
- dépublier emporte les lignes de la communauté.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

_CORPS = "# Relance\n\nÉtape confidentielle : relancer avec <tool:serper_search>."


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import access, db, org_store
    u = uuid.uuid4().hex[:8]
    subs = {k: f"{k}_{u}" for k in ("admin_a", "membre_a", "admin_b", "membre_b",
                                     "admin_c", "staff")}
    for k, sub in subs.items():
        db.upsert_user(sub, email=f"{sub}@northwind.example", name=k)
    orgs = {}
    for o in ("a", "b", "c"):
        orgs[o] = org_store.create_org(f"Org {o.upper()} {u}", created_by=subs[f"admin_{o}"])
        org_store.add_org_member(orgs[o], subs[f"admin_{o}"], "org_admin")
    org_store.add_org_member(orgs["a"], subs["membre_a"], "org_member")
    org_store.add_org_member(orgs["b"], subs["membre_b"], "org_member")
    for o, slug in (("a", "relance"), ("b", "veille")):
        org_store.set_instruction("org", orgs[o], slug, _CORPS, title=f"T {slug}",
                                  description="Une description.", set_by=subs[f"admin_{o}"])
    role_avant = access.get_user_role
    monkeypatch.setattr(access, "get_user_role",
                        lambda sub: "admin" if sub == subs["staff"] else role_avant(sub))
    return {"u": u, "subs": subs, "orgs": orgs}


def _ctx(m, qui, org=None):
    return ResolvedCtx(sub=m["subs"][qui], org_id=m["orgs"][org] if org else None,
                       channel="rest")


def _lib():
    from oto_mcp.capabilities import guide_library
    return guide_library


def _publier(m, qui, org, slug, public_slug, **kw):
    L = _lib()
    return L._publish(_ctx(m, qui, org),
                      L.PublishInput(slug=slug, public_slug=public_slug, **kw))


def _forker(m, qui, org, slug):
    L = _lib()
    return L._fork(_ctx(m, qui, org), L.ForkInput(slug=slug))


def _liste(m, qui, org, **kw):
    L = _lib()
    return L._list(_ctx(m, qui, org), L.LibraryListInput(**kw))["guides"]


def _get(m, qui, org, slug):
    L = _lib()
    return L._get(_ctx(m, qui, org), L.LibraryGetInput(slug=slug))


def _moderer(m, qui, entry_id, action, reason="Contenu trompeur"):
    from oto_mcp.capabilities import communaute as C
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "library.moderate")
    inp = C.ModerateInput(id=entry_id, action=action, reason=reason)
    ctx = cap.authz(RawCtx(sub=m["subs"][qui]), inp)
    return cap.handler(ctx, inp)


def _client():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from oto_mcp.api import public
    return TestClient(Starlette(routes=[
        Route("/api/public/community/processes", public.community_processes),
        Route("/api/public/community/processes/{slug}", public.community_process)]))


def _slugs(rows):
    return [r["slug"] for r in rows]


def test_un_org_admin_publie_au_nom_de_son_org_et_le_membre_non(monde):
    m = monde
    out = _publier(m, "admin_a", "a", "relance", f"relance-{m['u']}")
    assert (out["author_kind"], out["moderation_state"]) == ("org", "listed")
    assert out["author_display"].startswith("Org A")
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "library.publish")
    from oto_mcp import org_store
    # Le membre de A, dans A : refusé par la règle déclarée.
    org_store.set_active_org(m["subs"]["membre_a"], m["orgs"]["a"])
    with pytest.raises(AuthzDenied) as e:
        cap.authz(RawCtx(sub=m["subs"]["membre_a"]), cap.Input(slug="relance"))
    assert e.value.code == "publication_reservee_aux_admins"


def test_installations_compteurs_et_tris(monde):
    from oto_mcp.db._conn import _connect
    m, u = monde, monde["u"]
    r = _publier(m, "admin_a", "a", "relance", f"relance-{u}")
    v = _publier(m, "admin_b", "b", "veille", f"veille-{u}")
    # relance : installée par B (deux fois, deux comptes) et par C → 2 orgs distinctes.
    _forker(m, "membre_b", "b", r["slug"])
    _forker(m, "admin_b", "b", r["slug"])
    _forker(m, "admin_c", "c", r["slug"])
    # veille : installée par A une fois.
    _forker(m, "admin_a", "a", v["slug"])
    with _connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM community_installs WHERE kind = "
                         "'procedure' AND entry_id = %s", (r["id"],)).fetchone()["n"]
    assert n == 3, "chaque fork réussi est noté"
    e = _get(m, "membre_b", "b", r["slug"])
    assert (e["installs_total"], e["installs_30d"]) == (2, 2), "des orgs DISTINCTES"
    assert "body_md" in e and e["moderation_state"] == "listed" and "moderation" not in e

    ici = {r["slug"], v["slug"]}
    def ordre(**kw):
        return [s for s in _slugs(_liste(m, "membre_b", "b", **kw)) if s in ici]
    assert ordre(sort="most_added") == [r["slug"], v["slug"]]
    assert ordre(sort="new") == [v["slug"], r["slug"]], "veille est publiée après"
    assert ordre() == [v["slug"], r["slug"]], "sans sort : dernière publication d'abord"
    # Les installations de relance vieillissent : hors des 30 jours, elle ne « tend » plus.
    with _connect() as conn:
        conn.execute("UPDATE community_installs SET created_at = NOW() - INTERVAL "
                     "'40 days' WHERE entry_id = %s AND kind = 'procedure'", (r["id"],))
    assert ordre(sort="trending") == [v["slug"], r["slug"]]
    assert ordre(sort="most_added") == [r["slug"], v["slug"]]
    rows = {x["slug"]: x for x in _liste(m, "membre_b", "b")}
    assert (rows[r["slug"]]["installs_total"], rows[r["slug"]]["installs_30d"]) == (2, 0)


def test_une_entree_retiree_ne_se_voit_plus_que_de_son_org_et_du_personnel(monde):
    m, u = monde, monde["u"]
    r = _publier(m, "admin_a", "a", "relance", f"relance-{u}")
    with pytest.raises(AuthzDenied) as e:
        _moderer(m, "admin_a", r["id"], "remove")
    assert e.value.status == 403, "un org_admin ne modère pas"
    out = _moderer(m, "staff", r["id"], "remove", "Contenu trompeur")
    assert (out["state"], out["reason"], out["removed_by"]) == \
        ("removed", "Contenu trompeur", m["subs"]["staff"])

    # Les autres : ni liste, ni lecture, ni fork — le 404 d'une entrée inconnue.
    assert r["slug"] not in _slugs(_liste(m, "membre_b", "b", include_removed=True))
    for geste in (_get, _forker):
        with pytest.raises(AuthzDenied) as e:
            geste(m, "membre_b", "b", r["slug"])
        assert (e.value.status, e.value.code) == (404, "unknown_entry")
    with pytest.raises(AuthzDenied):            # un simple membre de l'org autrice non plus
        _get(m, "membre_a", "a", r["slug"])

    # L'org autrice (ses admins) et le personnel la voient, avec le motif.
    vue = _get(m, "admin_a", "a", r["slug"])
    assert vue["moderation"]["reason"] == "Contenu trompeur"
    assert r["slug"] not in _slugs(_liste(m, "admin_a", "a")), "pas sans le demander"
    assert r["slug"] in _slugs(_liste(m, "admin_a", "a", include_removed=True))
    assert r["slug"] in _slugs(_liste(m, "staff", None, include_removed=True))
    assert _get(m, "staff", None, r["slug"])["moderation_state"] == "removed"

    # Republier ne lève pas le retrait.
    again = _publier(m, "admin_a", "a", "relance", f"relance-{u}")
    assert (again["version"], again["moderation_state"]) == (2, "removed")
    assert _client().get(f"/api/public/community/processes/{r['slug']}").status_code == 404

    # Restaurer la rend à tous.
    assert _moderer(m, "staff", r["id"], "restore", "Corrigé")["state"] == "listed"
    assert _get(m, "membre_b", "b", r["slug"])["moderation_state"] == "listed"
    assert _client().get(f"/api/public/community/processes/{r['slug']}").status_code == 200


def test_la_vitrine_anonyme_ne_sert_que_ses_champs(monde):
    m, u = monde, monde["u"]
    pub = _publier(m, "admin_a", "a", "relance", f"relance-{u}", category="sales",
                   tags=["relance"])
    cache = _publier(m, "admin_b", "b", "veille", f"veille-{u}", visibility="unlisted")
    _forker(m, "membre_b", "b", pub["slug"])
    c = _client()
    r = c.get("/api/public/community/processes", params={"limit": 1000})
    assert r.status_code == 200 and r.headers["cache-control"].startswith("public, max-age=")
    rows = {x["slug"]: x for x in r.json()["processes"]}
    assert pub["slug"] in rows and cache["slug"] not in rows, "jamais une `unlisted`"
    champs = {"slug", "title", "description", "author_kind", "author_display",
              "category", "tags", "installs_total", "installs_30d", "created_at",
              "updated_at", "slots"}
    e = rows[pub["slug"]]
    assert set(e) == champs
    assert (e["installs_total"], e["category"], e["tags"]) == (1, "sales", ["relance"])
    texte = r.text
    for interdit in ("body_md", "confidentielle", "author_org_id", "source_org_id",
                     "published_by", m["subs"]["admin_a"]):
        assert interdit not in texte
    d = c.get(f"/api/public/community/processes/{pub['slug']}")
    assert d.status_code == 200 and set(d.json()) == champs
    assert "confidentielle" not in d.text
    for absent in (cache["slug"], f"inconnue-{u}"):
        r404 = c.get(f"/api/public/community/processes/{absent}")
        assert r404.status_code == 404 and r404.json()["error"] == "not_found"
    assert c.get("/api/public/community/processes", params={"sort": "x"}).status_code == 400
    assert c.get("/api/public/community/processes",
                 params={"sort": "trending", "category": "sales"}).status_code == 200


def test_l_auteur_ne_leve_pas_un_retrait_en_depubliant(monde):
    """Dépublier puis republier sous le même slug ferait une entrée NEUVE, sans retrait :
    une entrée retirée ne se dépublie que par le personnel."""
    L = _lib()
    m, u = monde, monde["u"]
    r = _publier(m, "admin_a", "a", "relance", f"relance-{u}")
    _moderer(m, "staff", r["id"], "remove")
    with pytest.raises(AuthzDenied) as e:
        L._unpublish(_ctx(m, "admin_a", "a"), L.UnpublishInput(id=r["id"]))
    assert (e.value.status, e.value.code) == (403, "entree_retiree")
    with pytest.raises(AuthzDenied) as e:
        L._unpublish(_ctx(m, "admin_b", "b"), L.UnpublishInput(id=r["id"]))
    assert e.value.status == 404, "un tiers ne sait même pas qu'elle existe"
    assert L._unpublish(_ctx(m, "staff", None), L.UnpublishInput(id=r["id"]))["unpublished"]


def test_depublier_emporte_les_lignes_de_la_communaute(monde):
    from oto_mcp import org_store
    from oto_mcp.db._conn import _connect
    m, u = monde, monde["u"]
    r = _publier(m, "admin_a", "a", "relance", f"relance-{u}")
    _forker(m, "admin_b", "b", r["slug"])
    _moderer(m, "staff", r["id"], "remove")
    assert org_store.unpublish_guide(r["id"]) is True     # le geste du personnel
    with _connect() as conn:
        for t in ("community_installs", "community_moderation"):
            n = conn.execute(f"SELECT count(*) AS n FROM {t} WHERE kind = 'procedure' "
                             "AND entry_id = %s", (r["id"],)).fetchone()["n"]
            assert n == 0, t
