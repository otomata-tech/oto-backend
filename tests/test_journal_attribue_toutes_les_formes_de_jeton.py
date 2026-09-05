"""Le journal attribue un compte QUELLE QUE SOIT la forme du bearer.

⚠️ Le défaut, trouvé le 05/09/2026 en cherchant sous quel compte tournaient les
workers : `_claimed_sub` décode un JWT, et rend `None` devant tout le reste. Un
jeton API (`oto_…`) ou un jeton de délégation produisait donc une ligne
**anonyme** — dans le seul journal où l'on va chercher qui a fait quoi.

Ce n'était pas une limite d'information : l'authentification, elle, résout le
porteur pour de vrai (`verify_api_token`). Elle le jetait, faute de le publier, et
le middleware le re-déduisait de l'en-tête — la seule source qui ne peut pas
répondre.

Conséquence pratique de l'aveuglement, tant qu'il durait : `op=rest sub=<qqn>` ne
rendait que ses gestes faits depuis le dashboard, et l'écart entre le total d'une
route et la somme par compte passait pour normal.
"""
from __future__ import annotations

import base64
import json

import pytest

from oto_mcp.api import base as ab
from oto_mcp.api import routes as ar


def _jwt(sub: str) -> str:
    charge = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    return f"h.{charge}.sig"


def _mw_avec_principal(monkeypatch, principal, headers=None):
    """Joue le middleware sur une requête dont l'auth a publié `principal`."""
    captured = {}
    monkeypatch.setattr(ar.db, "insert_tool_call", lambda row: captured.update(row))

    async def downstream(scope, receive, send):
        if principal is not None:
            scope[ab.CLE_PRINCIPAL] = principal      # ce que fait `_authenticate`
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    import asyncio
    mw = ar.RestCallLogger(downstream)
    scope = {"type": "http", "path": "/api/me/runner/jobs", "method": "POST",
             "headers": [(b"authorization", h.encode()) for h in ([headers] if headers else [])],
             "query_string": b""}

    async def drive():
        await mw(scope, lambda: None, lambda m: asyncio.sleep(0))
        await asyncio.sleep(0)
        await asyncio.gather(*list(ar._REST_LOG_TASKS), return_exceptions=True)

    asyncio.run(drive())
    return captured


# ── une forme de bearer par banc ──────────────────────────────────────────────

def test_une_session_interactive_est_attribuee(monkeypatch):
    row = _mw_avec_principal(monkeypatch, {"sub": "u-42", "token_id": None,
                                           "token_kind": None},
                             headers=f"Bearer {_jwt('u-42')}")
    assert row["sub"] == "u-42"
    assert row["token_id"] is None, "une session n'a pas de jeton nommé"


def test_un_jeton_API_est_attribue_ET_nomme(monkeypatch):
    """LE défaut réparé. L'en-tête ne porte qu'un opaque `oto_…` : seule
    l'authentification sait à qui il appartient."""
    row = _mw_avec_principal(monkeypatch, {"sub": "alexis", "token_id": 77,
                                           "token_kind": "user"},
                             headers="Bearer oto_opaque")
    assert row["sub"] == "alexis", "un jeton API n'écrit plus une ligne anonyme"
    assert (row["token_id"], row["token_kind"]) == (77, "user")


def test_une_delegation_du_runner_ne_ressemble_plus_a_une_session(monkeypatch):
    """Le compte seul ne suffisait pas : un travail exécuté au nom de quelqu'un
    et un geste fait par lui portaient le même `sub`, sans rien pour les
    distinguer."""
    row = _mw_avec_principal(monkeypatch, {"sub": "alexis", "token_id": 91,
                                           "token_kind": "delegation"},
                             headers="Bearer oto_opaque")
    assert row["sub"] == "alexis" and row["token_kind"] == "delegation"


def test_le_jeton_lui_meme_n_entre_jamais_dans_la_ligne(monkeypatch):
    row = _mw_avec_principal(monkeypatch, {"sub": "alexis", "token_id": 77,
                                           "token_kind": "user"},
                             headers="Bearer oto_le-secret-en-clair")
    assert "oto_le-secret-en-clair" not in json.dumps(row, default=str)


# ── l'épreuve inverse : une ligne sans principal DOIT se voir ────────────────

def test_sans_principal_publie_la_ligne_retombe_sur_l_en_tete_et_perd_le_compte(monkeypatch):
    """C'est l'état d'AVANT, reproduit exprès : si personne ne publie, le
    middleware n'a que l'en-tête, et un bearer opaque n'y dit rien. Ce banc
    échoue le jour où on croirait le réparer sans publier — il montre ce que
    coûte l'oubli, au lieu de le laisser passer en silence."""
    row = _mw_avec_principal(monkeypatch, None, headers="Bearer oto_opaque")
    assert row["sub"] is None, (
        "sans publication, l'attribution est impossible — si ce banc devient vert "
        "en rendant un sub, c'est qu'une autre source le devine, et il faut savoir "
        "laquelle")


def test_les_deux_chemins_d_authentification_PUBLIENT(monkeypatch):
    """La classe, pas le cas : chaque branche de `_authenticate` qui rend un
    compte doit le publier. Une branche qui l'oublierait rendrait des lignes
    anonymes sans qu'aucun autre banc ne le voie."""
    import ast
    import inspect
    arbre = ast.parse(inspect.getsource(ab._authenticate))
    retours = [n for n in ast.walk(arbre) if isinstance(n, ast.Return)]
    # Les retours de SUCCÈS sont ceux dont le premier élément n'est pas None.
    succes = [r for r in retours
              if isinstance(r.value, ast.Tuple)
              and not (isinstance(r.value.elts[0], ast.Constant)
                       and r.value.elts[0].value is None)]
    assert len(succes) == 2, f"{len(succes)} chemins de succès — le banc doit suivre"
    src = inspect.getsource(ab._authenticate)
    assert src.count("_publier_principal(") == 2
