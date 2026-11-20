"""Trois crans du binding REST, posés pour migrer le datastore sans rien changer au fil.

Une migration en capacité doit être invisible du client. Trois chemins historiques du
datastore ne rentraient pas dans le moule tel qu'il était :

- `POST …/datastores` et `POST …/rows` rendent **201**, pas 200 (contrat servi au
  dashboard et à `oto-core`) → `RestBinding.status` ;
- `POST …/rows` et `PATCH …/rows/{id}` portent un corps **LIBRE** — les colonnes du
  tableau appartiennent à l'utilisateur, aucune n'est déclarable — et la garde de champ
  inconnu les aurait toutes refusées → `RestBinding.body_field` ;
- `DELETE …/share` lit un corps `{email}`, ce qu'un DELETE ne fait pas d'ordinaire
  (client hors dépôt : `oto-core`) → `RestBinding.reads_body`.

Chacun est **opt-in par binding** : rien ne change pour les ~200 routes déjà générées,
et la garde de champ inconnu continue de couvrir query string et params de chemin —
c'est ce que le dernier test vérifie, parce que c'est la seule chose qu'on risquait de
perdre en ouvrant le corps.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.requests import Request

from oto_mcp.capabilities import _rest_adapter
from oto_mcp.capabilities._types import Capability, RestBinding, ResolvedCtx


class _RowInput(BaseModel):
    datastore: str
    row: dict = {}


class _EmailInput(BaseModel):
    datastore: str
    email: str = ""


def _exercise(cap, binding, *, method, path_params, body=None, query=b""):
    """Fait tourner le VRAI handler de l'adaptateur (pas une reformulation)."""
    vus: list = []

    def _core(ctx, inp):
        vus.append(inp)
        return {"ok": True}

    cap = Capability(key=cap, handler=_core, Input=binding[0],
                     authz=lambda raw, inp: ResolvedCtx(sub=raw.sub),
                     rest=binding[1])
    b = cap.rest_bindings()[0]

    def _json_error(_req, status, code, message=None):
        return JSONResponse({"error": code, "detail": message}, status_code=status)

    def _json_response(_req, payload, status=200):
        return JSONResponse(payload, status_code=status)

    async def _auth(_req, _verifier):
        return "sub-test", None

    handler = _rest_adapter._make_handler(cap, b, None, _auth, _json_response, _json_error)
    brut = b"" if body is None else json.dumps(body).encode()

    async def _receive():
        return {"type": "http.request", "body": brut, "more_body": False}

    req = Request({"type": "http", "method": method, "path": b.path, "headers": [],
                   "query_string": query, "path_params": path_params}, _receive)
    import asyncio
    rep = asyncio.run(handler(req))
    return rep.status_code, json.loads(bytes(rep.body)), vus


# --- corps libre ---------------------------------------------------------------

def test_un_corps_libre_atterrit_entier_dans_le_champ_declare():
    """Les colonnes d'une ligne ne sont pas des champs d'API : elles sont la donnée."""
    code, _, vus = _exercise(
        "test.append",
        (_RowInput, RestBinding(verb="POST", path="/api/x/{datastore}/rows",
                                body_field="row")),
        method="POST", path_params={"datastore": "4217"},
        body={"societe": "ACME", "statut": "à traiter", "n'importe quoi": 1})
    assert code == 200
    assert vus[0].datastore == "4217"
    assert vus[0].row == {"societe": "ACME", "statut": "à traiter", "n'importe quoi": 1}


def test_la_garde_de_champ_inconnu_couvre_toujours_la_query_string():
    """Ouvrir le CORPS ne devait rien ouvrir d'autre — sinon on rouvre le trou du 05/08."""
    code, corps, vus = _exercise(
        "test.append",
        (_RowInput, RestBinding(verb="POST", path="/api/x/{datastore}/rows",
                                body_field="row")),
        method="POST", path_params={"datastore": "4217"},
        body={"societe": "ACME"}, query=b"scope=org")
    assert (code, corps["error"]) == (400, "unknown_fields")
    assert "scope" in corps["detail"]
    assert not vus, "le handler a tourné alors que la query string était mal formée"


def test_sans_body_field_le_corps_se_fusionne_comme_avant():
    """Le défaut est inchangé : un corps déclaré reste refusé s'il déborde."""
    code, corps, _ = _exercise(
        "test.plain",
        (_EmailInput, RestBinding(verb="POST", path="/api/x/{datastore}/share")),
        method="POST", path_params={"datastore": "4217"},
        body={"email": "a@b.c", "permission": "write"})
    assert (code, corps["error"]) == (400, "unknown_fields")


# --- code de retour -------------------------------------------------------------

def test_le_code_de_creation_est_celui_du_binding():
    code, _, _ = _exercise(
        "test.create",
        (_EmailInput, RestBinding(verb="POST", path="/api/x/{datastore}/share",
                                  status=201)),
        method="POST", path_params={"datastore": "4217"}, body={})
    assert code == 201


def test_le_defaut_reste_200():
    code, _, _ = _exercise(
        "test.create",
        (_EmailInput, RestBinding(verb="POST", path="/api/x/{datastore}/share")),
        method="POST", path_params={"datastore": "4217"}, body={})
    assert code == 200


# --- corps sur DELETE ------------------------------------------------------------

def test_un_delete_ne_lit_pas_de_corps_par_defaut():
    _, _, vus = _exercise(
        "test.unshare",
        (_EmailInput, RestBinding(verb="DELETE", path="/api/x/{datastore}/share")),
        method="DELETE", path_params={"datastore": "4217"}, body={"email": "a@b.c"})
    assert vus[0].email == "", "le corps d'un DELETE ne se lit que sur demande"


def test_un_delete_lit_son_corps_quand_le_binding_le_declare():
    _, _, vus = _exercise(
        "test.unshare",
        (_EmailInput, RestBinding(verb="DELETE", path="/api/x/{datastore}/share",
                                  reads_body=True)),
        method="DELETE", path_params={"datastore": "4217"}, body={"email": "a@b.c"})
    assert vus[0].email == "a@b.c"


@pytest.mark.parametrize("champ", ["status", "body_field", "reads_body"])
def test_les_trois_crans_sont_opt_in(champ):
    """Un binding qui ne dit rien se comporte exactement comme avant la migration."""
    b = RestBinding(verb="POST", path="/api/x")
    assert getattr(b, champ) in (200, None, False)
