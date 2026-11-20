"""Retrait de la désignation d'un tableau par son NOM (08/11/2026) : un nom est REFUSÉ.

Un tableau s'adresse par son numéro (`ns_id`). Le préavis (lot 0, 29/09/2026) a servi
le nom avec un avis daté jusqu'au 08/11/2026 ; depuis, un nom est refusé sur toutes les
faces, sans repli et sans rien résoudre, avec un code nommé (`datastore_by_name`) et une
phrase qui dit de passer le numéro. Ce banc fige le refus au stockage, sur la face REST,
dans les capacités et dans les outils, et les textes servis — sans base.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
from collections import Counter

import pytest

from oto_mcp.datastore import errors

RACINE = pathlib.Path(__file__).resolve().parents[1]


# ── La forme : un nom, pas un numéro ni un `slot:` ───────────────────────────

@pytest.mark.parametrize("adresse,nom", [
    ("edition-vivier", True),
    ("2024-leads", True),
    ("204", False),
    (" 204 ", False),
    (204, False),
    ("slot:vivier", False),
    ("SLOT:vivier", False),
    ("", False),
    (None, False),
])
def test_est_un_nom_de_tableau(adresse, nom):
    assert errors.est_un_nom_de_tableau(adresse) is nom


def test_le_refus_dit_le_nom_et_le_geste():
    texte = errors.refus_tableau_par_nom("edition-vivier")
    assert "`edition-vivier` is a table NAME" in texte
    assert "`datastore=<number>`" in texte and "data_list_datastores" in texte
    assert "nothing was read or written" in texte
    assert errors.CODE_TABLEAU_PAR_NOM == "datastore_by_name"


# ── Le stockage : un nom est refusé AVANT toute lecture ──────────────────────

@pytest.fixture()
def store(monkeypatch):
    from oto_mcp import access, group_store, roles
    from oto_mcp.datastore import core as D

    vus: list = []
    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: False)
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org_id=None: [])
    monkeypatch.setattr(D.ownership, "vue_bornee", lambda: None)
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda adresse, **k: vus.append(adresse)
                        or {"id": 204, "datastore": "vivier"})
    return D.make_store("u-1"), vus


def test_un_nom_est_refuse_sans_rien_resoudre(store):
    st, vus = store
    with pytest.raises(errors.TableauParNom) as e:
        st._resolve("vivier")
    assert isinstance(e.value, errors.DatastoreNotFound), \
        "refusé partout où « introuvable » l'est déjà"
    assert e.value.indice == errors.refus_tableau_par_nom("vivier")
    assert vus == [], "aucune résolution sous un nom"


def test_un_numero_resout(store):
    st, vus = store
    assert st._resolve("204") == 204
    assert vus == ["204"]


# ── Les faces : le même code, la même phrase ─────────────────────────────────

def test_capacite_le_refus_est_nomme():
    from oto_mcp.capabilities.datastore.common import ns_not_found
    d = ns_not_found("u-1", "vivier")
    assert (d.status, d.code) == (400, "datastore_by_name")
    assert d.message == errors.refus_tableau_par_nom("vivier")


def test_outil_mcp_le_refus_est_nomme():
    from oto_mcp.tools.datastore import _inconnu
    assert _inconnu("vivier", errors.TableauParNom("vivier")) == (
        "datastore_by_name: " + errors.refus_tableau_par_nom("vivier"))
    assert _inconnu("204", errors.DatastoreNotFound("204")) == "datastore `204` unknown"


@pytest.mark.parametrize("adresse,attendu", [("vivier", 400), ("204", 200)])
def test_la_route_rest_adressee_par_nom_est_refusee(adresse, attendu):
    from pydantic import BaseModel
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    from oto_mcp.capabilities import _rest_adapter
    from oto_mcp.capabilities._types import Capability, ResolvedCtx, RestBinding

    appels: list = []

    class Entree(BaseModel):
        datastore: str

    class Sortie(BaseModel):
        ok: bool

    cap = Capability(
        key="t.tableau", handler=lambda ctx, inp: appels.append(inp) or {"ok": True},
        Input=Entree, Output=Sortie,
        authz=lambda raw, inp: ResolvedCtx(sub=raw.sub, org_id=None),
        rest=RestBinding(verb="GET", path="/api/datastores/{datastore}/t"))

    async def authenticate(request, verifier, **kw):
        return "u-1", None

    async def options(request):
        return JSONResponse({})

    routes = _rest_adapter.make_routes(
        None, authenticate,
        lambda request, data, status=200: JSONResponse(data, status),
        lambda request, status, code, detail=None, **kw: JSONResponse(
            {"error": code, "detail": detail}, status),
        options, [cap])
    r = TestClient(Starlette(routes=routes)).get(f"/api/datastores/{adresse}/t")
    assert r.status_code == attendu, r.text
    assert "Sunset" not in r.headers and "Deprecation" not in r.headers
    if attendu == 400:
        assert r.json() == {"error": "datastore_by_name",
                            "detail": errors.refus_tableau_par_nom("vivier")}
        assert appels == [], "le handler n'est jamais appelé"


# ── Les textes servis ne promettent plus que le nom résout ───────────────────

def _textes_servis() -> dict:
    from oto_mcp.capabilities.registry import CAPABILITIES
    from oto_mcp.datastore import identite

    out = {"identite.DESCRIPTION": identite.DESCRIPTION}
    for c in CAPABILITIES:
        if c.key in ("me.datastore.get_schema", "me.project", "runner.fleets"):
            out[c.key] = c.description
    assert len(out) == 4, "un texte servi a disparu du relevé"
    for chemin in ("oto_mcp/tools/datastore.py", "oto_mcp/capabilities/_instruction.py",
                   "oto_mcp/guides/datastore-semantics.md"):
        out[chemin] = (RACINE / chemin).read_text()
    return out


@pytest.mark.parametrize("motif", [r"still resolves", r"being (retired|phased out)",
                                   r"name still works", r"by name or number"])
def test_aucun_texte_servi_ne_dit_que_le_nom_resout(motif):
    encore = [n for n, t in _textes_servis().items() if re.search(motif, t)]
    assert not encore, f"« {motif} » encore servi par : {encore}"


def test_le_guide_dit_le_refus():
    guide = (RACINE / "oto_mcp/guides/datastore-semantics.md").read_text()
    assert "datastore_by_name" in guide


# ── Le script de migration : ne devine jamais ────────────────────────────────

@pytest.fixture()
def script():
    spec = importlib.util.spec_from_file_location(
        "tableaux_par_numero", RACINE / "scripts/tableaux_par_numero.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_le_motif_reecrit_un_nom_resolu_et_laisse_le_reste(script, monkeypatch):
    monkeypatch.setattr(script, "_resoudre",
                        lambda noms, portee: ({"vivier": 204, "homonyme": 9},
                                              {"homonyme"}))
    c = Counter()
    texte = ('data_claim_next(datastore="vivier") puis '
             "data_write(datastore='inconnu') puis data_rows(datastore=\"homonyme\") "
             'et data_rows(datastore="317") et slot : datastore="slot:file". '
             "La prose qui cite vivier reste.")
    neuf = script._reecrire(texte, {"sub": "u"}, c)
    assert "data_claim_next(datastore=204)" in neuf
    assert "datastore='inconnu'" in neuf
    assert 'datastore="homonyme"' in neuf
    assert 'datastore="317"' in neuf and 'datastore="slot:file"' in neuf
    assert "La prose qui cite vivier reste." in neuf
    assert c == Counter({"occurrences réécrites": 1,
                         "occurrences INTROUVABLES (laissées)": 1,
                         "occurrences AMBIGUËS (laissées)": 1})


def test_les_props_d_un_bloc_se_reecrivent_dans_leurs_chaines(script, monkeypatch):
    """Sur le JSON sérialisé les guillemets sont échappés : le motif ne se verrait
    pas. La réécriture descend dans les chaînes, et le JSON reste un JSON."""
    monkeypatch.setattr(script, "_resoudre", lambda noms, portee: ({"vivier": 204}, set()))
    props = {"text": 'appelle data_rows(datastore="vivier")', "n": 3,
             "liste": ['datastore="vivier"']}
    assert script._cite_un_nom(props)
    neuf = script._reecrire_json(props, {"sub": "u"}, Counter())
    assert neuf == {"text": "appelle data_rows(datastore=204)", "n": 3,
                    "liste": ["datastore=204"]}


def test_un_proprietaire_sans_portee_n_est_jamais_reecrit(script):
    """Plateforme, tenant : pas de portée de tableaux où résoudre — listé, pas deviné."""
    assert script._portee_du_proprietaire(None, "platform", "0") is None
    assert script._portee_du_proprietaire(None, "tenant", "t") is None
