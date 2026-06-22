"""Extraction du schéma observé (squelette clés+types, sans valeurs) — forme réelle."""
from oto_mcp import connector_schema_store as css

_PROFILE = {
    "first_name": "Alexis", "last_name": "Laporte", "headline": "Founder",
    "is_premium": True, "follower_count": 1200,
    "contact_info": {"emails": ["a@b.com"], "phones": ["+33..."]},
    "skills": [{"name": "Python", "endorsement_count": 5}, {"name": "LangChain"}],
    "languages": [{"name": "Français"}],
    "recommendations": {"received": [{"actor": {"first_name": "Jean", "last_name": "Dupont"}}]},
}


def test_leaves_captures_paths_and_types_no_values():
    lv = css.leaves(_PROFILE)
    # scalaires de 1er niveau (+ first_name apparaît aussi sous recommendations…actor)
    assert lv["first_name"]["type"] == "string" and "first_name" in lv["first_name"]["paths"]
    assert lv["is_premium"]["type"] == "boolean"
    assert lv["follower_count"]["type"] == "number"
    # listes de scalaires (contact_info) → chemin avec [] , type de l'élément
    assert lv["emails"]["paths"] == {"contact_info.emails[]"}
    assert lv["phones"]["paths"] == {"contact_info.phones[]"}
    # clé `name` ambiguë : multi-chemins capturés (skills + languages)
    assert lv["name"]["paths"] == {"skills[].name", "languages[].name"}
    # imbriqué profond (recommendations[].actor.*)
    assert "recommendations.received[].actor.first_name" in lv["first_name"]["paths"]
    # aucune VALEUR ne fuit dans le squelette
    blob = repr(lv)
    for secret in ("Alexis", "Laporte", "a@b.com", "Python", "Jean"):
        assert secret not in blob


def test_as_fields_exposes_paths_as_label():
    raw = css._serialize(css.leaves(_PROFILE))   # forme persistée {name:{type,paths:[...]}}
    fields = css.as_fields(raw)
    by = {f["name"]: f for f in fields}
    # `name` montre où il apparaît (rend l'ambiguïté visible dans l'UI)
    assert "skills[].name" in by["name"]["label"] and "languages[].name" in by["name"]["label"]
    # une clé nichée montre son chemin
    assert by["emails"]["label"] == "contact_info.emails[]"
    # headline n'apparaît qu'à la racine (homonyme du nom) → pas de label redondant
    assert by["headline"]["label"] is None


def test_merge_is_incremental():
    cur = css._load.__wrapped__ if hasattr(css._load, "__wrapped__") else None  # noqa: F841
    a = css.leaves({"x": 1})
    b = css.leaves({"x": 2, "y": "z"})
    merged = {n: {"type": i["type"], "paths": set(i["paths"])} for n, i in a.items()}
    changed = css._merge(merged, b)
    assert changed and set(merged) == {"x", "y"}
    # re-merge identique → pas de changement
    assert css._merge(merged, b) is False
