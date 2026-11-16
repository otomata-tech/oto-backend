"""`api_doc` : route de documentation configurable sur la carte du connecteur
`api`, pas un endpoint fixe côté oto — chaque org/bridge choisit la sienne
(ex. `/openapi.json` pour un bridge, derrière le même auth que le reste,
aucune route publique à part). Cf. `_require_doc_path`, testable sans contexte
MCP comme `_excerpt`/`_upstream_error` (test_api_upstream_error.py).
"""
import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools.api import _require_doc_path


def test_doc_path_absent_leve_une_erreur_actionnable():
    with pytest.raises(McpError, match="doc_path"):
        _require_doc_path({})


def test_doc_path_vide_ou_blanc_leve_la_meme_erreur():
    with pytest.raises(McpError, match="doc_path"):
        _require_doc_path({"doc_path": "   "})


def test_doc_path_present_est_retourne_tel_quel():
    assert _require_doc_path({"doc_path": "/openapi.json"}) == "/openapi.json"


def test_doc_path_est_independant_des_autres_champs():
    """Sa présence ne dépend ni de base_url ni de auth_mode — la config peut être
    incomplète par ailleurs, seul doc_path compte ici."""
    fields = {"doc_path": "/docs.json", "auth_mode": "bearer", "token": "x"}
    assert _require_doc_path(fields) == "/docs.json"
