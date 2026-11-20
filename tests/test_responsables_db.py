"""Le responsable d'une procédure et d'une page, sur une VRAIE base (`db/responsables`).

- lu par défaut : l'auteur (première version d'une procédure, `created_by` d'une page) ;
- posé par un geste, relu ensuite, sans toucher à la version, au corps ni à `updated_at` ;
- un NOM, pas un droit : la garde est celle de l'écriture (org_admin pour une procédure,
  `write` sur le projet pour une page) ;
- on ne nomme pas quelqu'un qui ne voit pas la chose (422) ; un slug inconnu est un 404 ;
- une version GARDÉE n'a pas de responsable, la procédure en a un.
"""
from __future__ import annotations

import uuid

import pytest

ADMIN = "sub-resp-admin"
MEMBRE = "sub-resp-membre"
DEHORS = "sub-resp-dehors"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@resp.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    for sub in (ADMIN, MEMBRE, DEHORS):
        db.upsert_user(sub, email=f"{sub}@resp.invalid", name=sub)
    oid = org_store.create_org(f"Resp {uuid.uuid4().hex[:6]}", created_by=ADMIN)
    org_store.add_org_member(oid, ADMIN, "org_admin")
    org_store.add_org_member(oid, MEMBRE, "org_member")
    return int(oid)


@pytest.fixture(scope="module")
def rest(org):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def _h(sub: str = ADMIN) -> dict:
    return {"Authorization": f"Bearer {sub}"}


# ── procédures ─────────────────────────────────────────────────────────────────

def _procedure(org: int) -> str:
    """Une procédure de l'org : v1 par l'admin, v2 par le membre."""
    from oto_mcp import org_store
    slug = f"resp-{uuid.uuid4().hex[:6]}"
    org_store.set_instruction("org", str(org), slug, "1. lire", title="Essai", set_by=ADMIN)
    org_store.set_instruction("org", str(org), slug, "1. lire\n2. écrire", title="Essai",
                              set_by=MEMBRE)
    return slug


def _lire(rest, slug, sub=ADMIN, **params):
    r = rest.get(f"/api/me/instructions/{slug}", headers=_h(sub), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _confier(rest, slug, owner, sub=ADMIN):
    return rest.post(f"/api/me/instructions/{slug}/owner", headers=_h(sub),
                     json={"owner_sub": owner})


def test_procedure_par_defaut_l_auteur_de_la_premiere_version(rest, org):
    slug = _procedure(org)
    lue = _lire(rest, slug)
    assert lue["version"] == 2
    assert lue["owner_sub"] == ADMIN, "l'auteur de la v1, pas celui de la dernière"


def test_procedure_confier_se_relit_sans_monter_la_version(rest, org):
    slug = _procedure(org)
    r = _confier(rest, slug, MEMBRE)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "slug": slug, "owner_sub": MEMBRE,
                        "previous_owner_sub": ADMIN}
    lue = _lire(rest, slug)
    assert (lue["owner_sub"], lue["version"]) == (MEMBRE, 2)
    assert lue["body_md"].endswith("2. écrire")


def test_procedure_une_version_gardee_n_a_pas_de_responsable(rest, org):
    slug = _procedure(org)
    assert not _lire(rest, slug, version=1).get("owner_sub")


def test_procedure_confier_est_un_geste_d_org_admin(rest, org):
    slug = _procedure(org)
    r = _confier(rest, slug, MEMBRE, sub=MEMBRE)
    assert r.status_code == 403, r.text
    assert _lire(rest, slug)["owner_sub"] == ADMIN


def test_procedure_on_ne_nomme_pas_quelqu_un_hors_de_l_org(rest, org):
    slug = _procedure(org)
    r = _confier(rest, slug, DEHORS)
    assert (r.status_code, r.json().get("error")) == (422, "owner_not_member"), r.text
    assert _lire(rest, slug)["owner_sub"] == ADMIN


def test_procedure_inconnue_est_un_404(rest, org):
    r = _confier(rest, f"absente-{uuid.uuid4().hex[:6]}", MEMBRE)
    assert r.status_code == 404, r.text


# ── pages ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def projet(org):
    from oto_mcp import db
    return int(db.create_project("org", str(org), f"Mémoire {uuid.uuid4().hex[:6]}",
                                 created_by=ADMIN))


def _doc(sub, **kw):
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.docs import core as D
    return D._doc(ResolvedCtx(sub=sub, org_id=None), D.DocInput(**kw))


def _page(pid) -> int:
    from oto_mcp import db
    return int(db.create_doc(pid, f"Page {uuid.uuid4().hex[:6]}", body_md="Contenu",
                             created_by=ADMIN))


def test_page_par_defaut_son_createur(projet):
    p = _page(projet)
    assert _doc(ADMIN, op="get", doc_id=p)["owner_sub"] == ADMIN


def test_page_confier_se_relit_sans_toucher_au_contenu(projet):
    p = _page(projet)
    avant = _doc(ADMIN, op="get", doc_id=p)
    out = _doc(MEMBRE, op="set_owner", doc_id=p, owner_sub=MEMBRE)
    assert (out["owner_sub"], out["previous_owner_sub"]) == (MEMBRE, ADMIN)
    apres = _doc(ADMIN, op="get", doc_id=p)
    assert apres["owner_sub"] == MEMBRE
    assert (apres["rev"], apres["updated_at"]) == (avant["rev"], avant["updated_at"])
    assert _doc(ADMIN, op="revisions", doc_id=p)["revisions"] == []


def test_page_sans_droit_d_ecriture_refuse(projet):
    from oto_mcp.capabilities._types import AuthzDenied
    p = _page(projet)
    with pytest.raises(AuthzDenied) as e:
        _doc(DEHORS, op="set_owner", doc_id=p, owner_sub=DEHORS)
    assert e.value.status in (403, 404)
    assert _doc(ADMIN, op="get", doc_id=p)["owner_sub"] == ADMIN


def test_page_on_ne_nomme_pas_quelqu_un_qui_ne_la_lit_pas(projet):
    from oto_mcp.capabilities._types import AuthzDenied
    p = _page(projet)
    with pytest.raises(AuthzDenied) as e:
        _doc(ADMIN, op="set_owner", doc_id=p, owner_sub=DEHORS)
    assert (e.value.status, e.value.code) == (422, "owner_cannot_read")
