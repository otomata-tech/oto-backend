"""Rédaction des champs à la frontière des tools (middleware unique, ADR 0009/0015).

Vérifie que `FieldRedactionMiddleware` redacte les DEUX canaux d'un `ToolResult`
(structured_content + content texte) sans laisser fuiter de brut, applique le défaut
candidat (anonymisation profil), respecte le passe-through (no-op / is_error) et reste
**fail-closed** si la rédaction plante.
"""
import asyncio
import json

import pytest
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from oto.tools.common import FieldFilter

from oto_mcp import middleware
from oto_mcp.field_filter_defaults import _CANDIDATE_PII


class _Msg:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, name):
        self.message = _Msg(name)


def _result(payload: dict, *, is_error=False, structured=True) -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload if structured else None,
        is_error=is_error,
    )


def _run(name, result, *, ff=None, raises=False):
    """Exécute le middleware en patchant la résolution de FieldFilter."""
    mw = middleware.FieldRedactionMiddleware()

    if raises:
        def _resolve(_service):
            raise RuntimeError("DB down")
    else:
        def _resolve(_service):
            return ff if ff is not None else FieldFilter()

    orig = middleware._resolve_field_filter
    middleware._resolve_field_filter = _resolve
    try:
        async def call_next(_ctx):
            return result
        return asyncio.run(mw.on_call_tool(_Ctx(name), call_next))
    finally:
        middleware._resolve_field_filter = orig


_PROFILE = {
    "first_name": "Jean-Baptiste",
    "last_name": "Fleury",
    "email": "jb.fleury@example.com",
    "phone": "+33 6 12 34 56 78",
    "photo_url": "https://media.licdn.com/jb.jpg",
    "public_profile_url": "https://linkedin.com/in/jbfleury",
    "headline": "Head of Talent",
    "location": "Paris, Île-de-France",
}


def test_candidate_default_redacts_both_channels():
    ff = FieldFilter(rules=_CANDIDATE_PII)
    out = _run("unipile_profile", _result(_PROFILE), ff=ff)

    sc = out.structured_content
    text = json.loads(out.content[0].text)
    for view in (sc, text):
        # identité pseudonymisée (≠ original, non vide)
        assert view["first_name"] != "Jean-Baptiste" and view["first_name"]
        assert view["last_name"] != "Fleury" and view["last_name"]
        # email masqué mais format préservé
        assert view["email"] != _PROFILE["email"] and "@" in view["email"]
        # téléphone masqué
        assert view["phone"] != _PROFILE["phone"]
        # ré-identifiants directs supprimés
        assert "photo_url" not in view
        assert "public_profile_url" not in view
        # non sensibles conservés
        assert view["headline"] == "Head of Talent"
        assert view["location"] == "Paris, Île-de-France"
    # cohérence inter-canaux : aucun brut résiduel
    assert sc["first_name"] == text["first_name"]
    assert "Fleury" not in out.content[0].text


def test_pseudonym_is_stable():
    ff = FieldFilter(rules=_CANDIDATE_PII)
    a = _run("unipile_profile", _result(dict(_PROFILE)), ff=ff).structured_content
    b = _run("unipile_profile", _result(dict(_PROFILE)), ff=ff).structured_content
    assert a["last_name"] == b["last_name"]  # même source → même pseudonyme


def test_empty_filter_passthrough():
    out = _run("fr_search", _result({"first_name": "Jean"}), ff=FieldFilter())
    assert out.structured_content == {"first_name": "Jean"}


def test_error_result_untouched():
    err = _result({"first_name": "Jean"}, is_error=True)
    out = _run("unipile_profile", err, ff=FieldFilter(rules=_CANDIDATE_PII))
    assert out is err


def test_fail_closed_when_apply_raises():
    class _Boom(FieldFilter):
        @property
        def is_empty(self):
            return False

        def apply(self, _data):
            raise RuntimeError("faker missing")

    out = _run("unipile_profile", _result(dict(_PROFILE)), ff=_Boom())
    assert out.is_error
    assert "Fleury" not in out.content[0].text  # aucun brut ne fuit


def test_resolve_failure_passthrough():
    # Rien par défaut (SERVER_DEFAULTS vide) → aucun service n'est « sensible connu » :
    # sur un échec de résolution (aléa DB) on passe le résultat tel quel plutôt que de
    # casser le tool. (Le fail-closed reste sur l'échec d'APPLICATION, cf. test ci-dessus.)
    res = _result({"q": "x"})
    assert _run("fr_search", res, raises=True) is res
    res2 = _result(dict(_PROFILE))
    assert _run("unipile_profile", res2, raises=True) is res2
