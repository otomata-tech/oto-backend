"""Les 4 outils MCP `yousign_*` — cycle composer/envoyer/statut/document signé.

Exerce le VRAI chemin FastMCP (`mcp.call_tool`), le client Yousign remplacé
par un double en mémoire — même patron que les bancs `oto_call`/capacités de
cette session : le contrat exposé à un agent, pas l'implémentation interne.
"""
from __future__ import annotations

import asyncio
import base64

import pytest
from fastmcp import FastMCP

from oto_mcp import access
from oto_mcp.tools import yousign as Y


class _FauxYousignClient:
    """Double en mémoire du client oto-core — un état minimal, pas de réseau."""

    def __init__(self, **kw):
        self.demandes = {}
        self.documents = {}
        self.signataires = {}
        self._n = 0

    def _id(self, prefixe):
        self._n += 1
        return f"{prefixe}{self._n}"

    def create_signature_request(self, name, *, delivery_mode="email", **body):
        sr_id = self._id("sr")
        self.demandes[sr_id] = {"id": sr_id, "name": name, "status": "draft",
                                "delivery_mode": delivery_mode, "signers": [], "documents": []}
        return self.demandes[sr_id]

    def add_document(self, signature_request_id, file_bytes, filename, *,
                     nature="signable_document", **fields):
        doc_id = self._id("doc")
        self.documents[doc_id] = {"id": doc_id, "filename": filename, "bytes": file_bytes}
        self.demandes[signature_request_id]["documents"].append(doc_id)
        return self.documents[doc_id]

    def add_signer(self, signature_request_id, info, *,
                   signature_level="electronic_signature", **fields):
        sg_id = self._id("sg")
        self.signataires[sg_id] = {"id": sg_id, "info": info}
        self.demandes[signature_request_id]["signers"].append(sg_id)
        return self.signataires[sg_id]

    def activate_signature_request(self, signature_request_id):
        self.demandes[signature_request_id]["status"] = "ongoing"
        return {"id": signature_request_id, "status": "ongoing"}

    def get_signature_request(self, signature_request_id):
        return self.demandes[signature_request_id]

    def download_document(self, signature_request_id, document_id):
        return self.documents[document_id]["bytes"]


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setattr(access, "resolve_api_key", lambda provider, account=None: ("k-test", False))
    import oto.tools.yousign as pkg
    faux = _FauxYousignClient()
    monkeypatch.setattr(pkg, "YousignClient", lambda **kw: faux)

    m = FastMCP("banc-yousign")
    Y.register(m)
    m._faux = faux  # exposé pour les assertions
    return m


def _appeler(mcp, nom_outil, **arguments):
    resultat = asyncio.run(mcp.call_tool(nom_outil, arguments))
    return resultat.structured_content


def test_creer_compose_sans_envoyer(mcp):
    r = _appeler(mcp, "yousign_creer", nom="NDA — Partie A / Partie B",
                pdf_base64=base64.b64encode(b"%PDF-1.7 ...").decode(),
                nom_fichier="nda.pdf",
                signataires=[{"prenom": "Alice", "nom": "Dupont", "email": "alice@example.com"},
                            {"prenom": "Bob", "nom": "Martin", "email": "bob@example.com"}])
    assert r["demande_id"].startswith("sr")
    assert r["document_id"].startswith("doc")
    assert len(r["signataires"]) == 2
    assert r["signataires"][0]["email"] == "alice@example.com"

    demande = mcp._faux.demandes[r["demande_id"]]
    assert demande["status"] == "draft"  # composé, pas envoyé


def test_creer_transmet_prenom_nom_email_langue(mcp):
    r = _appeler(mcp, "yousign_creer", nom="Accord", pdf_base64=base64.b64encode(b"x").decode(),
                nom_fichier="a.pdf",
                signataires=[{"prenom": "Carla", "nom": "Rossi", "email": "carla@example.com",
                             "langue": "it"}])
    sg_id = r["signataires"][0]["id"]
    info = mcp._faux.signataires[sg_id]["info"]
    assert info == {"first_name": "Carla", "last_name": "Rossi",
                    "email": "carla@example.com", "locale": "it"}


def test_creer_langue_par_defaut_fr(mcp):
    r = _appeler(mcp, "yousign_creer", nom="Accord", pdf_base64=base64.b64encode(b"x").decode(),
                nom_fichier="a.pdf",
                signataires=[{"prenom": "D", "nom": "E", "email": "d@example.com"}])
    sg_id = r["signataires"][0]["id"]
    assert mcp._faux.signataires[sg_id]["info"]["locale"] == "fr"


def test_envoyer_active_la_demande(mcp):
    c = _appeler(mcp, "yousign_creer", nom="Accord", pdf_base64=base64.b64encode(b"x").decode(),
                nom_fichier="a.pdf", signataires=[])
    assert mcp._faux.demandes[c["demande_id"]]["status"] == "draft"
    _appeler(mcp, "yousign_envoyer", demande_id=c["demande_id"])
    assert mcp._faux.demandes[c["demande_id"]]["status"] == "ongoing"


def test_statut_rend_les_champs_utiles(mcp):
    c = _appeler(mcp, "yousign_creer", nom="Accord", pdf_base64=base64.b64encode(b"x").decode(),
                nom_fichier="a.pdf",
                signataires=[{"prenom": "A", "nom": "B", "email": "a@example.com"}])
    s = _appeler(mcp, "yousign_statut", demande_id=c["demande_id"])
    assert s["statut"] == "draft"
    assert s["nom"] == "Accord"
    assert len(s["signataires"]) == 1
    assert len(s["documents"]) == 1


def test_document_signe_rend_le_pdf_en_base64(mcp):
    contenu = b"%PDF-1.7 signed content..."
    c = _appeler(mcp, "yousign_creer", nom="Accord",
                pdf_base64=base64.b64encode(contenu).decode(),
                nom_fichier="a.pdf", signataires=[])
    r = _appeler(mcp, "yousign_document_signe",
                demande_id=c["demande_id"], document_id=c["document_id"])
    assert base64.b64decode(r["pdf_base64"]) == contenu
    assert r["nom_fichier"] == f"{c['document_id']}.pdf"


def test_cycle_complet_composer_envoyer_statut_document(mcp):
    """Le parcours entier qu'un agent suivrait : composer → envoyer → suivre →
    récupérer, sans jamais relire le PDF autrement qu'en base64."""
    contenu = b"%PDF-1.7 le vrai contenu"
    c = _appeler(mcp, "yousign_creer", nom="Contrat", pdf_base64=base64.b64encode(contenu).decode(),
                nom_fichier="contrat.pdf",
                signataires=[{"prenom": "Alice", "nom": "A", "email": "alice@example.com"}])
    _appeler(mcp, "yousign_envoyer", demande_id=c["demande_id"])
    s = _appeler(mcp, "yousign_statut", demande_id=c["demande_id"])
    assert s["statut"] == "ongoing"
    r = _appeler(mcp, "yousign_document_signe",
                demande_id=c["demande_id"], document_id=c["document_id"])
    assert base64.b64decode(r["pdf_base64"]) == contenu
