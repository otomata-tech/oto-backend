"""Les sections d'une app retirées du HTML servi, contre un PostgreSQL réel : un lecteur
borné par un rôle reçoit à `op=get` un HTML sans les éléments des sections que son rôle
n'ouvre pas ; qui modifie l'app reçoit tout ; un lecteur n'atteint le HTML entier par
aucun autre chemin (brouillon, révision, restauration) ; `op=validate` ne lui rend pas
les avertissements de sections."""
from __future__ import annotations

import uuid

import pytest

HTML = ('<main><h1>Desk</h1>'
        '<section data-oto-section="pipeline">PIPELINE</section>'
        '<section data-oto-section="admin">ADMIN-SECRET'
        '<script>const marge = "ADMIN-SCRIPT";</script></section>'
        '<script data-oto-section="admin">const cle = "ADMIN-INLINE";</script>'
        '</main>')


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.apps_manifeste import forme
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, chef, membre = f"auteur_{u}", f"chef_{u}", f"membre_{u}"
    for s in (auteur, chef, membre):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    ns_id = db.create_datastore("user", auteur, f"fiches_{u}", context_org_id=org)
    make_store(auteur).set_schema(str(ns_id), {"fields": [{"key": "name", "type": "text"}]})
    clean, problems = forme({
        "tables": {"fiches": {"table": str(ns_id), "fields": ["name"]}},
        "roles": {"admin": {"label": "Admin", "sections": ["pipeline", "admin"]},
                  "user": {"label": "User", "sections": ["pipeline"]}},
        "default_role": "user"})
    assert problems == []
    clean["tables"]["fiches"]["table"] = str(ns_id)
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=clean,
                         html=HTML, thumbnail=None, by=auteur)
    m = {"auteur": auteur, "chef": chef, "membre": membre, "org": org, "app": int(app["id"])}
    for s in (chef, membre):
        _partager(m, s)
    return m


def _ctx(m, sub):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=sub, org_id=m["org"], channel="rest")


def _partager(m, sub):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    R._resources(_ctx(m, m["auteur"]),
                 ResourceInputV2(op="share", resource_type="app", resource_id=str(m["app"]),
                                 audience="person", email=f"{sub}@example.test",
                                 role="contributor"))


def _apps(m, sub, **kw):
    from oto_mcp.capabilities import apps as A
    return A._app(_ctx(m, sub), A.AppInput(app_id=m["app"], **kw))


def test_le_lecteur_ne_recoit_pas_les_sections_de_son_role_absentes(monde):
    m = monde
    vue = _apps(m, m["membre"], op="get")["app"]
    assert vue["access"]["sections"] == ["pipeline"]
    assert "PIPELINE" in vue["html"] and "<h1>Desk</h1>" in vue["html"]
    for secret in ("ADMIN-SECRET", "ADMIN-SCRIPT", "ADMIN-INLINE", 'data-oto-section="admin"'):
        assert secret not in vue["html"]
    assert vue["html"] == ('<main><h1>Desk</h1><section data-oto-section="pipeline">'
                           "PIPELINE</section></main>")


def test_le_role_qui_ouvre_la_section_la_recoit(monde):
    m = monde
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["chef"],
          role="admin")
    assert _apps(m, m["chef"], op="get")["app"]["html"] == HTML


def test_qui_modifie_l_app_recoit_tout(monde):
    m = monde
    vue = _apps(m, m["auteur"], op="get")["app"]
    assert vue["html"] == HTML and vue["access"]["role"] is None


def test_aucun_autre_chemin_ne_rend_le_html_entier_au_lecteur(monde):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    # Une révision existe (l'app réécrite) : un lecteur ne la lit pas, ne la restaure pas.
    db.apps.update(m["app"], by=m["auteur"], html=HTML + "<p>v2</p>")
    rev = db.apps.revisions(m["app"])[0]
    assert "html" not in rev
    assert all("html" not in x for x in _apps(m, m["membre"], op="revisions")["revisions"])
    for kw in ({"op": "revision", "revision_id": rev["id"]},
               {"op": "restore", "revision_id": rev["id"]},
               {"op": "get", "draft": True}):
        with pytest.raises(AuthzDenied):
            _apps(m, m["membre"], **kw)
    assert "ADMIN-SECRET" not in _apps(m, m["membre"], op="get")["app"]["html"]


def test_validate_ne_rend_pas_les_avertissements_au_lecteur(monde):
    from oto_mcp import db
    m = monde
    db.apps.update(m["app"], by=m["auteur"],
                   html=HTML + '<div data-oto-section="billing">x</div>')
    lecteur = _apps(m, m["membre"], op="validate")
    assert lecteur["ok"] is True and "warnings" not in lecteur
    auteur = _apps(m, m["auteur"], op="validate")
    assert any('"billing"' in w for w in auteur["warnings"])


# ── Le mode strict, jugé à l'écriture et au service ──────────────────────────────

NON_CLASSE = HTML.replace("</main>", "<script>const tout = 1;</script></main>")


def test_ecrire_un_script_non_classe_est_refuse(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    vu = _apps(m, m["auteur"], op="get")["app"]
    with pytest.raises(AuthzDenied) as e:
        _apps(m, m["auteur"], op="update", html=NON_CLASSE, expected_rev=vu["rev"])
    assert e.value.code == "sections_not_classified" and "Line" in e.value.message
    # Rien n'a été écrit.
    assert _apps(m, m["auteur"], op="get")["app"]["html"] == HTML
    # Le commun passe.
    commun = HTML.replace("</main>", '<script data-oto-section="*">const t = 1;</script></main>')
    _apps(m, m["auteur"], op="update", html=commun, expected_rev=vu["rev"])


def test_validate_dit_le_probleme_sans_ecrire(monde):
    m = monde
    out = _apps(m, m["auteur"], op="validate", html=NON_CLASSE)
    assert out["ok"] is False and "Line" in out["problems"][0]


def test_strict_eteint_explicitement_laisse_ecrire(monde):
    m = monde
    vu = _apps(m, m["auteur"], op="get")["app"]
    man = {**vu["manifest"], "strict_sections": False}
    _apps(m, m["auteur"], op="update", manifest=man, html=NON_CLASSE, expected_rev=vu["rev"])


def test_au_service_une_app_non_classee_n_est_pas_servie_au_lecteur(monde):
    """Écrite avant le mode strict (directement en base) : le lecteur borné reçoit un
    refus neutre, sans nom de section ; qui modifie l'app l'ouvre et voit le problème."""
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    with db._connect() as conn:
        conn.execute("UPDATE apps SET html = %s WHERE id = %s", (NON_CLASSE, m["app"]))
    with pytest.raises(AuthzDenied) as e:
        _apps(m, m["membre"], op="get")
    assert e.value.code == "app_unavailable"
    assert "admin" not in e.value.message and "pipeline" not in e.value.message
    assert _apps(m, m["auteur"], op="get")["app"]["html"] == NON_CLASSE


def test_l_editeur_voit_l_app_comme_un_role(monde):
    """`op=get as_role` : exactement ce que ce rôle reçoit — manifeste resserré, HTML sans
    ses sections fermées, son `access`. Refusé à un lecteur, et sur le brouillon."""
    from oto_mcp.capabilities._types import AuthzDenied
    m = monde
    vu = _apps(m, m["auteur"], op="get", as_role="user")["app"]
    assert "ADMIN-SECRET" not in vu["html"] and "PIPELINE" in vu["html"]
    assert vu["access"]["role"] == "user" and "roles" not in vu["manifest"]
    assert "ADMIN-SECRET" in _apps(m, m["auteur"], op="get", as_role="admin")["app"]["html"]
    for kw in ({"as_role": "admin"}, {"as_role": "nope"}):
        who = m["membre"] if kw["as_role"] == "admin" else m["auteur"]
        with pytest.raises(AuthzDenied):
            _apps(m, who, op="get", **kw)
    with pytest.raises(AuthzDenied) as e:
        _apps(m, m["auteur"], op="get", as_role="user", draft=True)
    assert e.value.code == "as_role_live_only"
