"""Le dépôt d'un fichier dans une app, de bout en bout par les VRAIES routes, sur une
vraie base : consentement en JSON (`POST …/consent`), puis multipart
(`POST …/files`), puis `list_files` par l'appel. Ce que les tests unitaires ne voient
pas, c'est que l'empreinte se calcule par DEUX chemins — le corps JSON d'un côté, la
partie multipart (nom décodé, type de la partie) de l'autre — et doit coïncider.

Le stockage S3 est remplacé par un client qui garde les objets ; l'authentification
est la vraie (`_authenticate`), avec un vérificateur JWT qui rend un `sub` connu.
"""
from __future__ import annotations

import types

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp import db, media_store

MOI = "sub-apps-fichiers-rest"
AUTRE = "sub-apps-fichiers-rest-autre"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


class _S3:
    def __init__(self):
        self.objets = {}

    def put_object(self, *, Key, Body, **kw):
        self.objets[Key] = Body


class _Verifier:
    def __init__(self):
        self.sub = MOI

    async def verify_token(self, token):
        return types.SimpleNamespace(claims={"sub": self.sub})


@pytest.fixture
def client(live, monkeypatch):
    from oto_mcp.api import routes as api_routes
    s3 = _S3()
    monkeypatch.setattr(media_store, "_get_client", lambda: s3)
    monkeypatch.setattr(media_store, "_bucket", lambda: "media-test")
    for sub in (MOI, AUTRE):
        db.upsert_user(sub, f"{sub}@example.com") if hasattr(db, "upsert_user") else None
    verifier = _Verifier()
    app = Starlette(routes=list(api_routes.make_routes(verifier)))
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer jwt-de-test"})
    c.s3, c.verifier = s3, verifier
    return c


@pytest.fixture
def app_id(live):
    app = db.apps.create(("user", MOI), None, title="Inbox",
                         manifest={"tables": {}, "agents": {}, "files": True},
                         html="", thumbnail=None, by=MOI)
    return app["id"]


def _consent(client, app_id, name, size, media_type):
    r = client.post(f"/api/me/apps/{app_id}/consent", json={
        "action": "upload_file", "file": {"name": name, "size": size, "media_type": media_type}})
    assert r.status_code == 200, r.text
    return r.json()["consent"]


def test_consentement_json_puis_multipart_se_rejoignent(client, app_id):
    nom = "Devis été (v2).pdf"  # accents, espaces, parenthèses : le nom multipart se décode
    jeton = _consent(client, app_id, nom, len(PDF), "application/pdf")
    r = client.post(f"/api/me/apps/{app_id}/files", data={"consent": jeton},
                    files={"file": (nom, PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    f = r.json()["file"]
    assert f["name"] == nom and f["media_type"] == "application/pdf" and f["size"] == len(PDF)
    assert any(k.startswith(f"app-files/{app_id}/") for k in client.s3.objets)
    # Le même jeton ne sert pas deux fois.
    r = client.post(f"/api/me/apps/{app_id}/files", data={"consent": jeton},
                    files={"file": (nom, PDF, "application/pdf")})
    assert (r.status_code, r.json()["error"]) == (403, "consent_invalid")
    # L'app le liste à qui l'a déposé…
    r = client.post(f"/api/me/apps/{app_id}/call", json={"action": "list_files"})
    assert r.status_code == 200, r.text
    assert [x["name"] for x in r.json()["files"]] == [nom]


def test_un_fichier_autre_que_l_annonce_est_refuse(client, app_id):
    jeton = _consent(client, app_id, "a.pdf", len(PDF), "application/pdf")
    r = client.post(f"/api/me/apps/{app_id}/files", data={"consent": jeton},
                    files={"file": ("a.pdf", PDF + b"x", "application/pdf")})
    assert (r.status_code, r.json()["error"]) == (403, "consent_invalid")


def test_sans_consentement_ou_sans_fichier(client, app_id):
    r = client.post(f"/api/me/apps/{app_id}/files", files={"file": ("a.pdf", PDF, "application/pdf")})
    assert (r.status_code, r.json()["error"]) == (403, "consent_required")
    r = client.post(f"/api/me/apps/{app_id}/files", data={"consent": "x"})
    assert (r.status_code, r.json()["error"]) == (400, "missing_file")


def test_un_jeton_api_est_refuse(client, app_id):
    r = client.post(f"/api/me/apps/{app_id}/files", headers={"Authorization": "Bearer oto_deadbeef"},
                    files={"file": ("a.pdf", PDF, "application/pdf")})
    assert (r.status_code, r.json()["error"]) == (403, "api_token_forbidden")
