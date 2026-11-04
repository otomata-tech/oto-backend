"""Les versions d'un guide sont gardées, relisibles et restaurables (`db/guide_revisions.py`).

Sur base RÉELLE et par la capacité servie (`_guide_op`, droits réels) :

- un remplacement (write, patch, readme) et une suppression gardent l'état PRÉCÉDENT ;
  une première écriture et une réécriture au même contenu n'en gardent pas ;
- `op=revisions` liste (guide supprimé compris), `op=read rev=` rend une version,
  `op=restore rev=` la réécrit comme une écriture — gardée à son tour, gardée par
  `expected_rev`, exigée par la surface stricte ;
- la rétention : 30 jours OU les 50 plus récentes, purgée au fil de l'écriture ;
- les droits : lire l'historique = lire le guide, restaurer = l'écrire ;
- un `rev` inconnu : 404 `unknown_revision`, rien d'autre à sa place.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import db, org_store
from oto_mcp.capabilities import guides as G
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ADMIN, MEMBRE, DEHORS = "u-vers-admin", "u-vers-membre", "u-vers-dehors"
_S = uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def org(live):
    for sub in (ADMIN, MEMBRE, DEHORS):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=sub)
    oid = org_store.create_org(f"Org versions {_S}", created_by=ADMIN)
    org_store.add_org_member(oid, ADMIN, "org_admin")
    org_store.add_org_member(oid, MEMBRE, "org_member")
    return oid


def _op(sub, oid, strict=False, **kw):
    h = G._guide_op_strict if strict else G._guide_op
    return h(ResolvedCtx(sub=sub, org_id=oid), G.GuideOpInput(**kw))


def _versions(sub, oid, **kw):
    return _op(sub, oid, op="revisions", **kw)


def test_un_remplacement_garde_l_etat_precedent(org):
    slug = f"guide-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="v1", title="T1")
    assert _versions(ADMIN, org, scope="user", slug=slug)["revisions"] == [], \
        "une première écriture ne garde rien"
    v1 = _op(ADMIN, org, op="read", scope="user", slug=slug)["rev"]
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="v2", title="T1")
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="v2", title="T1")
    liste = _versions(ADMIN, org, scope="user", slug=slug)
    assert [(v["rev"], v["reason"], v["replaced_by"]) for v in liste["revisions"]] == [
        (v1, "write", ADMIN)], "même contenu réécrit : aucune version de plus"
    assert liste["revisions"][0]["body_md_bytes"] == 2 and liste["retention"]
    lu = _op(ADMIN, org, op="read", scope="user", slug=slug, rev=v1)
    assert (lu["body_md"], lu["title"], lu["reason"]) == ("v1", "T1", "write")


def test_un_patch_garde_le_corps_d_avant(org):
    slug = f"journal-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="# Jour\n\nun")
    avant = _op(ADMIN, org, op="read", scope="user", slug=slug)["rev"]
    _op(ADMIN, org, op="patch", scope="user", slug=slug, section="Jour", body_md="deux")
    (v,) = _versions(ADMIN, org, scope="user", slug=slug)["revisions"]
    assert (v["rev"], v["reason"]) == (avant, "write")


def test_un_guide_supprime_se_relit_et_se_restaure(org):
    slug = f"supprime-{_S}"
    _op(ADMIN, org, op="write", scope="org", slug=slug, body_md="consigne", title="C")
    rev = _op(ADMIN, org, op="read", scope="org", slug=slug)["rev"]
    _op(ADMIN, org, op="delete", scope="org", slug=slug)
    liste = _versions(MEMBRE, org, scope="org", slug=slug)
    assert liste["current_rev"] == db.REV_ABSENT
    assert [(v["rev"], v["reason"]) for v in liste["revisions"]] == [(rev, "delete")]
    with pytest.raises(AuthzDenied) as e:          # supprimé : le scope ne se devine plus
        _versions(MEMBRE, org, slug=slug)
    assert e.value.code == "missing_scope"

    with pytest.raises(AuthzDenied) as e:          # la garde d'écriture vaut ici aussi
        _op(ADMIN, org, op="restore", scope="org", slug=slug, rev=rev, expected_rev=rev)
    assert e.value.code == "conflict"
    out = _op(ADMIN, org, op="restore", scope="org", slug=slug, rev=rev,
              expected_rev=db.REV_ABSENT)
    assert out["rev"] == rev and out["previous_rev"] == db.REV_ABSENT
    lu = _op(MEMBRE, org, op="read", scope="org", slug=slug)
    assert (lu["body_md"], lu["title"]) == ("consigne", "C")


def test_une_restauration_est_une_ecriture_versionnee(org):
    slug = f"restaure-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="bon")
    bon = _op(ADMIN, org, op="read", scope="user", slug=slug)["rev"]
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="abîmé")
    abime = _op(ADMIN, org, op="read", scope="user", slug=slug)["rev"]
    _op(ADMIN, org, op="restore", scope="user", slug=slug, rev=bon)
    assert _op(ADMIN, org, op="read", scope="user", slug=slug)["body_md"] == "bon"
    assert [(v["rev"], v["reason"]) for v in
            _versions(ADMIN, org, scope="user", slug=slug)["revisions"]] == [
        (abime, "restore"), (bon, "write")]


def test_le_readme_garde_ses_versions(org):
    def readme(sub=ADMIN, **kw):
        return _op(sub, org, scope="org", delivery="init", **kw)
    readme(op="write", body_md="Règles v1.")
    v1 = readme(op="read")["rev"]
    readme(op="write", body_md="Règles v2.")
    v2 = readme(op="read")["rev"]
    readme(op="delete")
    assert [(v["rev"], v["reason"]) for v in readme(op="revisions")["revisions"]] == [
        (v2, "delete"), (v1, "init")]
    assert readme(MEMBRE, op="read", rev=v1)["body_md"] == "Règles v1."
    readme(op="restore", rev=v1)
    assert readme(op="read")["body_md"] == "Règles v1."


def test_un_rev_inconnu_est_refuse_par_son_nom(org):
    slug = f"inconnu-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="seul")
    for kw in ({"op": "read"}, {"op": "restore"}):
        with pytest.raises(AuthzDenied) as e:
            _op(ADMIN, org, scope="user", slug=slug, rev="0" * 16, **kw)
        assert (e.value.status, e.value.code) == (404, "unknown_revision")
    assert _op(ADMIN, org, op="read", scope="user", slug=slug)["body_md"] == "seul"


def test_les_droits_sont_ceux_de_la_lecture_et_de_l_ecriture(org):
    slug = f"droits-{_S}"
    _op(ADMIN, org, op="write", scope="org", slug=slug, body_md="a")
    a = _op(ADMIN, org, op="read", scope="org", slug=slug)["rev"]
    _op(ADMIN, org, op="write", scope="org", slug=slug, body_md="b")
    assert _versions(MEMBRE, org, scope="org", slug=slug)["revisions"]
    with pytest.raises(AuthzDenied) as e:          # hors de l'org : pas de lecture
        _versions(DEHORS, None, scope="org", slug=slug, owner_id=str(org))
    assert e.value.status == 403
    with pytest.raises(AuthzDenied) as e:          # membre : lit, ne restaure pas
        _op(MEMBRE, org, op="restore", scope="org", slug=slug, rev=a)
    assert e.value.status == 403
    assert _op(ADMIN, org, op="read", scope="org", slug=slug)["body_md"] == "b"


def test_la_surface_stricte_exige_expected_rev_pour_restaurer(org):
    with pytest.raises(AuthzDenied) as e:
        _op(ADMIN, org, strict=True, op="restore", scope="user", slug="x", rev="r")
    assert e.value.code == "expected_rev_required"


def test_rev_et_corps_refuses_la_ou_ils_seraient_ignores(org):
    with pytest.raises(AuthzDenied) as e:
        _op(ADMIN, org, op="write", scope="user", slug="x", body_md="b", rev="r")
    assert e.value.code == "unsupported_revision_arg"
    with pytest.raises(AuthzDenied) as e:
        _op(ADMIN, org, op="restore", scope="user", slug="x", rev="r", body_md="b")
    assert e.value.code == "unsupported_revision_arg"


def _poser_des_versions(slug: str, n: int, age_jours: int) -> None:
    with db._connect() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO guide_revisions (owner_id, owner_type, slug, delivery, "
                "body_md, rev, reason, created_at) VALUES (%s, 'user', %s, 'on-demand', "
                "%s, %s, 'write', NOW() - make_interval(days => %s))",
                (ADMIN, slug, f"vieux {i}", f"r{i:04d}", age_jours))


def _compte(slug: str) -> int:
    return len(db.guide_revisions.lister("user", ADMIN, slug, "on-demand"))


def test_retention_les_50_plus_recentes_au_dela_de_30_jours(org):
    slug = f"vieux-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="courant")
    _poser_des_versions(slug, 60, age_jours=40)
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="suivant")
    assert _compte(slug) == db.guide_revisions.GARDER_DERNIERES
    gardees = [v["rev"] for v in db.guide_revisions.lister("user", ADMIN, slug, "on-demand")]
    assert "r0000" not in gardees and "r0059" in gardees, "les plus anciennes partent"


def test_retention_tout_ce_qui_a_moins_de_30_jours_reste(org):
    slug = f"recent-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="courant")
    _poser_des_versions(slug, 60, age_jours=1)
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="suivant")
    assert _compte(slug) == 61
