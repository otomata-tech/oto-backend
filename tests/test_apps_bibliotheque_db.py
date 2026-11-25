"""La bibliothèque d'apps de la communauté, contre un PostgreSQL réel.

Ce qu'on vérifie, c'est ce que l'instantané EMPORTE et ce que l'installation CRÉE :
- publier prend le HTML, le manifeste réduit (alias, champs, gestes) et les SCHÉMAS ;
  jamais une ligne, jamais un agent, jamais un numéro de tableau ; un lien entre deux
  tableaux de l'app devient un alias ;
- le contrôle automatique refuse une app dont le HTML embarque une adresse ou une clé ;
- installer crée des tableaux VIDES à l'installeur, récrit le lien vers le tableau neuf,
  crée l'app PRIVÉE à l'installeur et note l'installation ; une app d'une autre org
  exige `reviewed=true` ; `map_tables` branche un tableau à soi ;
- la vitrine anonyme ne sert jamais le HTML ; modération `kind=app` comme les procédures.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

_HTML = ("<div id=app></div><script>oto.rows('deals', {fields: ['name', 'stage']})"
         ".then(r => document.title = String(r.total));</script>")


@pytest.fixture
def monde(live):
    from oto_mcp import db, org_store
    from oto_mcp.datastore.core import make_store
    u = uuid.uuid4().hex[:8]
    subs = {k: f"{k}_{u}" for k in ("admin_a", "membre_a", "membre_b", "staff")}
    for s in subs.values():
        db.upsert_user(s, email=f"{s}@northwind.example")
    org_a = org_store.create_org(f"Org A {u}", created_by=subs["admin_a"])
    org_b = org_store.create_org(f"Org B {u}", created_by=subs["membre_b"])
    org_store.add_org_member(org_a, subs["admin_a"], "org_admin")
    org_store.add_org_member(org_a, subs["membre_a"], "org_member")
    org_store.add_org_member(org_b, subs["membre_b"], "org_member")
    org_store.set_active_org(subs["admin_a"], org_a)
    org_store.set_active_org(subs["membre_b"], org_b)
    s = make_store(subs["admin_a"])
    deals = s.create_datastore(f"deals-{u}", schema={"fields": [
        {"key": "name", "type": "text", "label": "Name"},
        {"key": "stage", "type": "text", "label": "Stage"}]})["id"]
    notes = s.create_datastore(f"notes-{u}", schema={"fields": [
        {"key": "body", "type": "text"},
        {"key": "deal", "type": "ref", "target": int(deals)}]})["id"]
    with db._connect() as conn:
        conn.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                     (deals, f"r-{u}", '{"name": "Client secret row", "stage": "won"}'))
    app = db.apps.create(("user", subs["admin_a"]), org_a, title=f"Pipeline {u}",
                         manifest={"tables": {
                             "deals": {"table": str(deals), "fields": ["name", "stage"],
                                       "set": {"stage": ["won", "lost"]}},
                             "notes": {"table": str(notes)}},
                             "agents": {"relance": {"id": 999999, "run": True}}},
                         html=_HTML, thumbnail={"color": "#123456", "emoji": "📈"},
                         by=subs["admin_a"])
    return {"u": u, "subs": subs, "org_a": org_a, "org_b": org_b, "app": app["id"],
            "deals": int(deals), "notes": int(notes)}


def _ctx(m, qui, org):
    return ResolvedCtx(sub=m["subs"][qui], org_id=org, channel="rest")


def _B():
    from oto_mcp.capabilities import apps_bibliotheque
    return apps_bibliotheque


def _publier(m, qui="admin_a", **kw):
    B = _B()
    kw.setdefault("public_slug", f"pipeline-{m['u']}")
    return B._publish(_ctx(m, qui, m["org_a"]), B.AppLibraryPublishInput(app_id=m["app"], **kw))


def _installer(m, qui="membre_b", org=None, **kw):
    B = _B()
    return B._install(_ctx(m, qui, org or m["org_b"]),
                      B.AppLibraryInstallInput(slug=f"pipeline-{m['u']}", **kw))


def test_l_instantane_n_emporte_ni_ligne_ni_agent_ni_numero(monde):
    m = monde
    out = _publier(m, description="Suivre ses deals.")
    assert (out["author_kind"], out["agents_dropped"], out["version"]) == ("org", 1, 1)
    e = _B()._get(_ctx(m, "membre_b", m["org_b"]), _B().AppLibraryGetInput(slug=out["slug"]))
    assert e["html"] == _HTML
    assert e["manifest"]["tables"]["deals"] == {"fields": ["name", "stage"],
                                                "set": {"stage": ["won", "lost"]}, "add": []}
    assert "agents" not in e["manifest"]
    texte = str(e)
    assert "Client secret row" not in texte, "jamais une ligne"
    assert str(m["deals"]) not in str(e["manifest"]) and "999999" not in texte
    ref = next(f for f in e["schemas"]["notes"]["fields"] if f["key"] == "deal")
    assert ref == {"key": "deal", "type": "ref", "target": "@deals"}
    assert {t["alias"]: t["fields"] for t in e["tables"]} == {"deals": 2, "notes": 2}
    # Republier la sienne : version + 1 ; un membre non admin ne publie pas.
    assert _publier(m)["version"] == 2
    with pytest.raises(AuthzDenied) as err:
        from oto_mcp.capabilities.registry import CAPABILITIES
        cap = next(c for c in CAPABILITIES if c.key == "apps.library.publish")
        from oto_mcp import org_store
        org_store.set_active_org(m["subs"]["membre_a"], m["org_a"])
        cap.authz(RawCtx(sub=m["subs"]["membre_a"]), cap.Input(app_id=m["app"]))
    assert err.value.code == "publication_reservee_aux_admins"


def test_le_controle_automatique_juge_le_html(monde):
    from oto_mcp import db
    m = monde
    with db._connect() as conn:
        conn.execute("UPDATE apps SET html = %s WHERE id = %s",
                     (_HTML + "<p>Écrire à paul@vraie-boite.fr</p>", m["app"]))
    with pytest.raises(AuthzDenied) as e:
        _publier(m)
    assert e.value.code == "contenu_refuse"
    assert {"field": "html", "kind": "email"} in e.value.details["found"]
    assert "paul@" not in e.value.message


def test_installer_cree_des_tableaux_vides_et_une_app_privee(monde):
    from oto_mcp import db, ownership
    m = monde
    _publier(m)
    with pytest.raises(AuthzDenied) as e:
        _installer(m)
    assert e.value.code == "review_required", "le code d'une autre org se relit d'abord"
    out = _installer(m, reviewed=True)
    deals, notes = out["tables"]["deals"]["table"], out["tables"]["notes"]["table"]
    assert out["tables"]["deals"]["created"] and deals != m["deals"]
    with db._connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s",
                         (deals,)).fetchone()["n"]
    assert n == 0, "un tableau installé naît vide"
    owner = db.get_datastore_owner(deals)
    assert (owner["owner_type"], owner["owner_id"]) == ("user", m["subs"]["membre_b"])
    ref = next(f for f in db.get_datastore_by_id(notes)["schema"]["fields"]
               if f["key"] == "deal")
    assert int(ref["target"]) == deals, "le lien vise le tableau neuf"
    app = db.apps.get(out["app_id"])
    assert (app["owner_type"], app["owner_id"]) == ("user", m["subs"]["membre_b"])
    assert app["manifest"]["tables"]["deals"]["table"] == str(deals)
    assert app["manifest"]["agents"] == {} and app["html"] == _HTML
    assert ownership.list_grants("app", str(app["id"])) == [], "privée à l'installeur"
    e2 = _B()._get(_ctx(m, "membre_b", m["org_b"]),
                   _B().AppLibraryGetInput(slug=f"pipeline-{m['u']}"))
    assert e2["installs_total"] == 1


def test_map_tables_branche_un_tableau_a_soi_et_refuse_celui_d_autrui(monde):
    from oto_mcp.datastore.core import make_store
    m = monde
    _publier(m)
    mien = make_store(m["subs"]["membre_b"]).create_datastore(
        f"mes-deals-{m['u']}", schema={"fields": [{"key": "name", "type": "text"},
                                                  {"key": "stage", "type": "text"}]})["id"]
    out = _installer(m, reviewed=True, map_tables={"deals": int(mien)})
    assert out["tables"]["deals"] == {"table": int(mien), "created": False,
                                      "name": f"deals-{m['u']}"}
    with pytest.raises(AuthzDenied) as e:      # le tableau de l'auteur, pas le sien
        _installer(m, reviewed=True, map_tables={"deals": m["deals"]})
    assert e.value.code == "invalid_manifest"


def test_vitrine_et_moderation(monde, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from oto_mcp import access
    from oto_mcp.api import public
    from oto_mcp.capabilities import communaute as C
    m = monde
    out = _publier(m, category="sales")
    c = TestClient(Starlette(routes=[
        Route("/api/public/community/apps", public.community_apps),
        Route("/api/public/community/apps/{slug}", public.community_app)]))
    r = c.get("/api/public/community/apps")
    e = next(x for x in r.json()["apps"] if x["slug"] == out["slug"])
    assert set(e) == {"slug", "title", "description", "author_kind", "author_display",
                      "category", "tags", "installs_total", "installs_30d", "created_at",
                      "updated_at", "thumbnail", "tables"}
    assert e["tables"] == [{"alias": "deals", "name": f"deals-{m['u']}", "fields": 2},
                           {"alias": "notes", "name": f"notes-{m['u']}", "fields": 2}]
    for interdit in ("oto.rows", "<script", "author_org_id", "published_by", "manifest"):
        assert interdit not in r.text
    assert c.get(f"/api/public/community/apps/{out['slug']}").status_code == 200

    role_avant = access.get_user_role
    monkeypatch.setattr(access, "get_user_role", lambda sub: "admin"
                        if sub == m["subs"]["staff"] else role_avant(sub))
    C._moderate(_ctx(m, "staff", None), C.ModerateInput(id=out["id"], kind="app",
                                                         action="remove", reason="Spam"))
    assert c.get(f"/api/public/community/apps/{out['slug']}").status_code == 404
    with pytest.raises(AuthzDenied) as err:
        _installer(m, reviewed=True)
    assert err.value.status == 404
    vue = _B()._get(_ctx(m, "admin_a", m["org_a"]), _B().AppLibraryGetInput(slug=out["slug"]))
    assert vue["moderation"]["reason"] == "Spam"
    with pytest.raises(AuthzDenied) as err:          # l'auteur ne lève pas le retrait
        _B()._unpublish(_ctx(m, "admin_a", m["org_a"]), _B().AppLibraryUnpublishInput(id=out["id"]))
    assert err.value.code == "entree_retiree"


@pytest.mark.parametrize("slug", ["seo", "CRM", "dashboard"])
def test_les_slugs_des_modeles_sont_reserves(monde, slug):
    """Une app publiée sous l'id d'un modèle du front masquerait sa page."""
    with pytest.raises(AuthzDenied) as e:
        _publier(monde, public_slug=slug)
    assert (e.value.status, e.value.code) == (409, "slug_taken")
