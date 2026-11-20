"""Rendre une ligne : le « non » disait deux choses opposées (#517, 29/08).

**Le cas qui l'impose, et il a coupé une campagne.** `data_release` rendait
`released: false` pour deux situations que rien ne distinguait dans la réponse :

- **aucun bail sur la ligne** — bénin, il n'y avait simplement rien à rendre ;
- **bail tenu par un autre travail** — échec réel, la ligne ne t'appartient pas.

Un seul texte les couvrait toutes les deux (« pas de bail sur cette row, ou bail posé
par un autre worker »). Une flotte qui a branché sa borne d'arrêt sur ce booléen a
compté le bénin avec le grave et **s'est coupée elle-même au sixième passage, à cinq
fiches sur cent**, le 29/08/2026 à 16:22.

⚠️ **Le serveur SAIT lequel des deux c'est.** Ce n'est pas une information qu'il faut
aller chercher : elle est dans la ligne qu'il vient de ne pas modifier. *Un succès
partiel qu'on ne peut pas distinguer d'un échec est pire qu'un refus — un refus, au
moins, s'instruit.*

C'est le même motif que les deux lots du jour, une couche plus loin : la réponse doit
NOMMER ce qu'elle constate, sans quoi l'appelant lui invente une cause.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as D

WORKER = "w-3"
AUTRE = {"claimed_by": "w-9", "claimed_until": "2026-08-29 18:00:00",
         "claimed_run": "run-autre"}


def _store(monkeypatch, *, libere: bool, bail=None):
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    # `_trace` lit le RECORD du tableau, pas son nom : la face REST passe
    # un relevé, la face agent non — le stub doit servir les deux.
    monkeypatch.setattr(s, "_ns_of",
                        lambda ns_id: {"datastore": "vivier", "schema": None})
    monkeypatch.setattr(D.db, "datastore_release_claim",
                        lambda ns_id, row_id, worker, **k: libere)
    monkeypatch.setattr(D.db, "datastore_active_lease",
                        lambda ns_id, row_id: bail)
    return s


# ── Le store rend la raison, pas seulement le verdict ────────────────────────

def test_liberation_reussie_ne_porte_aucune_raison(monkeypatch):
    s = _store(monkeypatch, libere=True)
    out = s.release_claim("vivier", "r1", worker=WORKER)
    assert out["released"] is True
    assert out["reason"] is None


def test_aucun_bail_est_nomme_comme_tel(monkeypatch):
    """Le cas BÉNIN — et c'est celui qu'une borne d'arrêt ne doit pas compter."""
    s = _store(monkeypatch, libere=False, bail=None)
    out = s.release_claim("vivier", "r1", worker=WORKER)
    assert (out["released"], out["reason"]) == (False, "no_lease")


def test_bail_d_un_autre_travail_est_nomme_et_porte_QUI(monkeypatch):
    """Le cas GRAVE — et il doit dire qui tient la ligne, et jusqu'à quand."""
    s = _store(monkeypatch, libere=False, bail=AUTRE)
    out = s.release_claim("vivier", "r1", worker=WORKER)
    assert (out["released"], out["reason"]) == (False, "held_by_other")
    assert out["lease"]["claimed_by"] == "w-9"


def test_les_deux_raisons_sont_un_vocabulaire_FERME(monkeypatch):
    """Une raison lue par une machine ne se lit pas dans une phrase."""
    for bail, attendu in ((None, "no_lease"), (AUTRE, "held_by_other")):
        s = _store(monkeypatch, libere=False, bail=bail)
        assert s.release_claim("vivier", "r1", worker=WORKER)["reason"] == attendu


# ── Sur la surface MCP : deux situations, deux phrases ───────────────────────

def _tool(name: str):
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as T
    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool(name)), T


def _appel(monkeypatch, *, libere, bail):
    import asyncio
    outil, T = _tool("data_release")
    s = _store(monkeypatch, libere=libere, bail=bail)
    monkeypatch.setattr(T, "_acting_store", lambda: s)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    return asyncio.run(outil.run({"datastore": "vivier", "id": "r1",
                                  "worker": WORKER})).structured_content


def test_MCP_la_reponse_porte_la_raison(monkeypatch):
    assert _appel(monkeypatch, libere=False, bail=None)["reason"] == "no_lease"
    assert _appel(monkeypatch, libere=False, bail=AUTRE)["reason"] == "held_by_other"


def test_MCP_les_deux_indices_ne_disent_PAS_la_meme_chose(monkeypatch):
    """⚠️ Le témoin du défaut : un seul texte couvrait les deux cas."""
    benin = _appel(monkeypatch, libere=False, bail=None)["hint"]
    grave = _appel(monkeypatch, libere=False, bail=AUTRE)["hint"]
    assert benin != grave, "deux situations opposées ne peuvent pas partager un indice"
    assert "nothing to release" in benin
    assert "w-9" in grave, "l'indice du cas grave doit dire QUI tient la ligne"


def test_MCP_une_liberation_reussie_ne_porte_pas_d_indice(monkeypatch):
    out = _appel(monkeypatch, libere=True, bail=None)
    assert out["released"] is True and out.get("hint") is None
    assert out.get("reason") is None


# ── Sur la face REST : la même raison, pas une seconde idée ──────────────────

def test_REST_porte_la_meme_raison(monkeypatch):
    import _datastore_rest as H

    from oto_mcp.capabilities.datastore import rows as dsr
    s = _store(monkeypatch, libere=False, bail=AUTRE)
    H.stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: s)
    monkeypatch.setattr(dsr.datastore_journal, "record", lambda *a, **k: None)
    monkeypatch.setattr(dsr.access, "resolve_datastore_ref", lambda ns: ns)
    _, corps = H.call("me.datastore.release_claim",
                      path_params={"datastore": "7", "row_id": "r1"},
                      body={"worker": WORKER})
    assert corps["released"] is False
    assert corps["reason"] == "held_by_other"
    assert "w-9" in (corps.get("hint") or "")


# ── L'affirmation périmée, au plus près du geste ─────────────────────────────

def test_la_docstring_ne_promet_plus_une_liberation_automatique_RETIREE():
    """⚠️ Même famille que la description de paramètre corrigée ce matin : une
    affirmation fausse écrite au plus près du geste. `release_claim` promettait que
    « l'entrée dans un état terminal libère déjà automatiquement » — c'est faux depuis
    que cette libération a été retirée (#317), et le code voisin dit explicitement
    qu'il ne libère plus rien."""
    doc = D.DatastorePg.release_claim.__doc__ or ""
    assert "libère déjà automatiquement" not in doc
    assert "#317" in doc, "la correction se DATE et cite le lot qui a retiré le geste"
