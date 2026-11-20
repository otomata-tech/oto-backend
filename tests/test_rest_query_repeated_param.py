"""Un paramètre de requête RÉPÉTÉ (`?filter=a:1&filter=b:2`) ne perd plus `a` (#418).

L'adaptateur REST versait la query string par `dict(request.query_params)` : sur une
clé répétée, `dict()` ne garde que la DERNIÈRE valeur. Toute capacité dont un champ
est une LISTE (`filter` de `GET /api/me/nodes/{id}/rows`) ne recevait donc qu'une
entrée par la face REST, **sans erreur**, quand la face MCP recevait la liste entière.
Deux faces, deux résultats pour la même demande — la famille « la divergence est
muette ».

Et le document OpenAPI servi promettait précisément la forme perdue : un champ
`Optional[list[str] | str]` y est décrit `anyOf [array, string]`, et un paramètre de
requête de type `array` se sérialise par défaut (`style: form`, `explode: true`) en
`?filter=a&filter=b`. Un client généré depuis notre propre descriptif envoyait donc la
forme que l'adaptateur aplatissait.

Le contrat, tenu au seam (toutes les routes générées, pas connecteur par connecteur) :

- champ déclaré LISTE (`list[...]` quelque part dans l'annotation, `Optional`/`Union`
  traversés) et clé répétée → une **liste**, dans l'ordre de l'URL ;
- champ déclaré liste et clé UNIQUE → **inchangé** : la valeur arrive en chaîne, et
  c'est au champ de la normaliser (virgule, cf. #367) — la face REST et la face MCP
  passent par la même validation ;
- champ SCALAIRE et clé répétée → **refus `400 repeated_scalar` qui nomme la clé**,
  jamais la dernière valeur en silence. Même principe que `unknown_fields` et que le
  corps illisible : refuser plutôt qu'ignorer ;
- clé INCONNUE répétée → `unknown_fields`, comme une clé inconnue simple.

Les tests exercent le VRAI handler de l'adaptateur, jamais une reformulation.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp.capabilities import _authz, _rest_adapter
from oto_mcp.capabilities._types import Capability, ResolvedCtx, RestBinding
from oto_mcp.capabilities.registry import CAPABILITIES


class _Input(BaseModel):
    items: Optional[list[str] | str] = None
    pur: Optional[list[str]] = None
    n: Optional[int] = None
    mode: Optional[Literal["a", "b"]] = None


def _handler_de(cap, binding):
    def _json_error(_req, status, code, message=None, **kw):
        return JSONResponse({"error": code, "detail": message}, status_code=status)

    def _json_response(_req, payload, status=200):
        return JSONResponse(payload, status_code=status)

    async def _auth(_req, _verifier, **_kw):
        return "sub-test", None

    return _rest_adapter._make_handler(cap, binding, None, _auth, _json_response,
                                       _json_error)


async def _get(handler, chemin: str, qs: str, path_params: Optional[dict] = None):
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "method": "GET", "path": chemin, "headers": [],
                   "query_string": qs.encode(), "path_params": path_params or {}},
                  _receive)
    rep = await handler(req)
    return rep.status_code, json.loads(bytes(rep.body))


async def _appeler(qs: str):
    """Une capacité factice dont le handler rend ce qu'il a REÇU."""
    vus: list = []

    def _core(ctx, inp):
        vus.append(inp)
        return {"ok": True}

    cap = Capability(key="test.repete", handler=_core, Input=_Input,
                     authz=lambda raw, inp: ResolvedCtx(sub=raw.sub), rest=RestBinding("GET", "/api/x"))
    code, corps = await _get(_handler_de(cap, cap.rest), "/api/x", qs)
    return code, corps, vus


# ── Le contrat, sur une capacité factice ───────────────────────────────────────

@pytest.mark.asyncio
async def test_une_cle_repetee_sur_un_champ_LISTE_arrive_en_liste_dans_l_ordre():
    code, _, vus = await _appeler("items=a&items=b&items=c")
    assert code == 200
    # `a` ne disparaît plus, et l'ordre est celui de l'URL.
    assert vus[0].items == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_une_cle_repetee_sur_une_liste_PURE_arrive_aussi_en_liste():
    # `Optional[list[str]]` sans `| str` : la forme répétée est la SEULE qui
    # l'atteigne par une URL (une valeur unique reste une chaîne, refusée par
    # pydantic — cf. #367, qui impose `| str` pour ce cas).
    code, _, vus = await _appeler("pur=a&pur=b")
    assert code == 200
    assert vus[0].pur == ["a", "b"]


@pytest.mark.asyncio
async def test_une_cle_UNIQUE_reste_une_chaine_comme_avant():
    """Le contrat #367 ne bouge pas : une valeur seule arrive en chaîne et c'est au
    champ de la normaliser (virgule). Sinon `?items=a,b` deviendrait `["a,b"]` et
    tous les champs à virgule changeraient de sens le même jour."""
    code, _, vus = await _appeler("items=a,b")
    assert code == 200
    assert vus[0].items == "a,b"


@pytest.mark.asyncio
async def test_une_cle_repetee_sur_un_champ_SCALAIRE_est_refusee_et_nommee():
    code, corps, vus = await _appeler("n=1&n=2")
    assert code == 400 and corps["error"] == "repeated_scalar", corps
    assert "n" in corps["detail"]
    assert vus == []            # rien n'est passé au handler


@pytest.mark.asyncio
async def test_un_Literal_est_un_scalaire():
    # `Literal["a","b"]` porte des CHAÎNES en arguments d'annotation : traverser ses
    # arguments comme ceux d'une `Union` en ferait une liste. Il n'en est pas une.
    code, corps, _ = await _appeler("mode=a&mode=b")
    assert code == 400 and corps["error"] == "repeated_scalar"


@pytest.mark.asyncio
async def test_une_cle_INCONNUE_repetee_est_refusee_comme_inconnue():
    code, corps, _ = await _appeler("zz=1&zz=2")
    assert code == 400 and corps["error"] == "unknown_fields"
    assert "zz" in corps["detail"]


@pytest.mark.asyncio
async def test_le_champ_inconnu_prime_sur_le_scalaire_repete():
    # Deux refus possibles, un seul rendu : celui qui liste les champs attendus est
    # le plus utile à un client qui se trompe de forme.
    code, corps, _ = await _appeler("n=1&n=2&zz=3")
    assert code == 400 and corps["error"] == "unknown_fields"


# ── Sur les VRAIES capacités : `filter` de node_rows, `filters` de data_rows ──

@pytest.fixture
def autz_sans_base(monkeypatch):
    monkeypatch.setattr(_authz.access, "current_org", lambda sub: 2)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")


class _Store:
    def __init__(self):
        self.vu: dict = {}

    def _resolve(self, datastore):
        return 12

    def cursor_rows(self, datastore, **kw):
        self.vu = dict(kw, datastore=datastore)
        return {"rows": [{"_id": "r1", "nom": "x"}], "next_cursor": None}

    def count_rows(self, datastore, *, filter=None, q=None, q_scope=None, filters=None):
        # Le compte passe par le STORE depuis #621 (il y résout les noms plats comme
        # la page, ce que `db.datastore_count_rows` ne fait pas).
        return len(filters or [])


_TABLE = {"id": 5, "public_id": "nod_tbl", "parent_id": None, "kind": "tableau",
          "owner_type": "org", "owner_id": "2", "position": 0,
          "props": {"title": "vivier", "legacy": "tbl", "legacy_id": 12,
                    "child_schema": {"fields": [{"key": "nom", "label": "Nom"}]}},
          "created_at": "2026-08-01", "updated_at": "2026-08-01"}


@pytest.mark.asyncio
async def test_node_rows_MEME_resultat_sur_les_deux_faces(monkeypatch, autz_sans_base):
    """LA requête de l'issue : `?filter=a:1&filter=b:2`. La face MCP reçoit la liste ;
    la face REST doit produire le MÊME appel au store et la MÊME page."""
    from oto_mcp.capabilities import node_rows as R

    cap = next(c for c in CAPABILITIES if c.key == "me.node.rows")
    binding = cap.rest_bindings()[0]
    monkeypatch.setattr(R.db_node, "node_by_public_id", lambda pid: _TABLE)

    # Face MCP : la liste, telle que le protocole la transporte.
    store_mcp = _Store()
    monkeypatch.setattr(R.ds, "make_store", lambda sub: store_mcp)
    ctx = ResolvedCtx(sub="sub-test", org_id=2, role="member")
    page_mcp = await cap.handler(ctx, cap.Input(node_id="nod_tbl",
                                                filter=["statut:actif", "ville:Paris"]))

    # Face REST : la même demande, écrite dans une URL.
    store_rest = _Store()
    monkeypatch.setattr(R.ds, "make_store", lambda sub: store_rest)
    code, page_rest = await _get(_handler_de(cap, binding), "/api/me/nodes/nod_tbl/rows",
                                 "filter=statut:actif&filter=ville:Paris",
                                 {"node_id": "nod_tbl"})
    assert code == 200, page_rest
    assert store_rest.vu["filters"] == store_mcp.vu["filters"] == [
        {"field": "statut", "op": "eq", "value": "actif"},
        {"field": "ville", "op": "eq", "value": "Paris"}]
    assert page_rest == page_mcp


@pytest.mark.asyncio
async def test_data_rows_un_filters_repete_est_refuse_pas_tronque(autz_sans_base):
    """`filters` de `GET …/data/{datastore}/rows` est un JSON dans UNE chaîne : le
    répéter est une erreur de forme, et la réponse le dit au lieu de garder le
    dernier et de présenter une page comme filtrée par les deux."""
    cap = next(c for c in CAPABILITIES if c.key == "me.datastore.list_rows")
    binding = cap.rest_bindings()[0]
    code, corps = await _get(
        _handler_de(cap, binding), "/api/me/data/12/rows",
        'filters=[{"field":"a","op":"eq","value":1}]&filters=[{"field":"b","op":"eq","value":2}]',
        {"datastore": "12"})

    assert code == 400 and corps["error"] == "repeated_scalar", corps
    assert "filters" in corps["detail"]
