"""Allègement des réponses LinkedIn lourdes + garde d'identifiant (feedback #281).

Une page de posts brute pèse 50-75 Ko même à `limit=10` (images en triple, urns,
tokens de partage) alors que le geste réel — « balayer les derniers posts de X et
voir si l'un correspond » — demande l'auteur, la date et 300-500 caractères. D'où
deux leviers serveur (`fields` projection + `text_max_chars` troncature) et le
refus explicite d'un `identifier` numérique, que LinkedIn rejette par un « Invalid
User ID » qui ne dit pas quoi passer à la place.
"""
from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp.tools.unipile import (
    _member_identifier_or_raise as ident,
    _project_items,
    _truncate_text,
)


def _page():
    return {"items": [
        {"text": "x" * 900, "date": "2026-07-01", "social_id": "urn:li:activity:1",
         "images": ["a", "b", "c"], "share_token": "tok"},
        {"text": "court", "date": "2026-07-02", "social_id": "urn:li:activity:2",
         "images": [], "share_token": "tok2"},
    ], "cursor": "next"}


# ── projection ──

def test_fields_keeps_only_asked_columns():
    out = _project_items(_page(), ["text", "date"])
    assert [sorted(i) for i in out["items"]] == [["date", "text"], ["date", "text"]]
    assert out["cursor"] == "next"          # l'enveloppe est préservée


def test_no_levers_is_a_noop():
    page = _page()
    assert _project_items(page) is page


def test_shape_without_items_is_untouched():
    for payload in ({"object": "Post", "text": "hi"}, [], None, "raw"):
        assert _project_items(payload, ["text"]) is payload


# ── troncature ──

def test_text_max_chars_cuts_and_marks_the_cut():
    out = _project_items(_page(), text_max_chars=300)
    first = out["items"][0]["text"]
    assert len(first) == 301 and first.endswith("…")   # 300 + la marque de coupe
    assert out["items"][1]["text"] == "court"          # sous le seuil → intact


def test_truncation_leaves_other_columns_intact():
    out = _project_items(_page(), text_max_chars=10)
    assert out["items"][0]["social_id"] == "urn:li:activity:1"
    assert out["items"][0]["images"] == ["a", "b", "c"]


def test_both_levers_compose():
    out = _project_items(_page(), ["text", "date"], text_max_chars=50)
    assert sorted(out["items"][0]) == ["date", "text"]
    assert out["items"][0]["text"].endswith("…")


def test_truncate_passes_through_non_strings_and_short_text():
    assert _truncate_text(42, 10) == 42
    assert _truncate_text(None, 10) is None
    assert _truncate_text("court", 10) == "court"


# ── garde d'identifiant ──

def test_numeric_member_id_is_refused_with_the_right_field_names():
    with pytest.raises(McpError) as e:
        ident("123456789")
    msg = str(e.value)
    assert "public_identifier" in msg and "provider_id" in msg


def test_empty_identifier_is_refused():
    for bad in ("", "   ", None):
        with pytest.raises(McpError):
            ident(bad)


def test_slug_and_provider_id_pass_through_trimmed():
    assert ident(" marie-durand ") == "marie-durand"
    assert ident("ACoAAB1234xyz") == "ACoAAB1234xyz"
