"""Le défaut « anonymisation candidat » sur la FORME RÉELLE d'un profil Unipile.

Garde-fou contre la régression du faux positif `name` (qui pseudonymisait
`skills[].name` / `languages[].name`) et vérifie que l'identité réelle est bien
traitée (clés imbriquées incluses). Calé sur la structure observée de `unipile_profile`.
"""
from oto.tools.common import FieldFilter
from oto_mcp.field_filter_defaults import _CANDIDATE_PII

# Mini-payload à la forme réelle Unipile (clés observées).
_PROFILE = {
    "first_name": "Alexis", "last_name": "Laporte",
    "headline": "Founder @ Otomata", "location": "Paris, Île-de-France",
    "public_identifier": "laportealexis", "provider_id": "ACoAAAHDNbk",
    "profile_picture_url": "https://media/x.jpg",
    "profile_picture_url_large": "https://media/x-large.jpg",
    "background_picture_url": "https://media/bg.jpg",
    "birthdate": {"month": 3, "day": 14},
    "contact_info": {"emails": ["alexis@otomata.tech"], "phones": ["+33612345678"]},
    "skills": [{"name": "Python", "endorsement_count": 5}, {"name": "LangChain"}],
    "languages": [{"name": "Français", "proficiency": "native"}],
    "recommendations": {"received": [
        {"text": "great", "actor": {"first_name": "Jean", "last_name": "Dupont",
                                    "profile_picture_url": "https://media/jd.jpg"}},
    ]},
}


def _redact():
    return FieldFilter(rules=_CANDIDATE_PII).apply(
        # deep copy via re-parse to not mutate the module constant
        __import__("json").loads(__import__("json").dumps(_PROFILE)))


def test_identity_is_anonymized():
    r = _redact()
    assert r["first_name"] != "Alexis" and r["first_name"]
    assert r["last_name"] != "Laporte" and r["last_name"]
    # contact masqué mais format préservé
    assert r["contact_info"]["emails"][0] != "alexis@otomata.tech"
    assert "@" in r["contact_info"]["emails"][0]
    assert r["contact_info"]["phones"][0] != "+33612345678"


def test_reidentifiers_dropped():
    r = _redact()
    for k in ("public_identifier", "provider_id", "profile_picture_url",
              "profile_picture_url_large", "birthdate"):
        assert k not in r, f"{k} aurait dû être retiré"


def test_non_pii_preserved():
    r = _redact()
    assert r["headline"] == "Founder @ Otomata"
    assert r["location"] == "Paris, Île-de-France"
    assert r["background_picture_url"] == "https://media/bg.jpg"


def test_skills_and_languages_not_corrupted():
    # LE bug : la règle `name` pseudonymisait skills[].name / languages[].name.
    r = _redact()
    assert [s["name"] for s in r["skills"]] == ["Python", "LangChain"]
    assert r["languages"][0]["name"] == "Français"


def test_nested_actor_identity_anonymized():
    r = _redact()
    actor = r["recommendations"]["received"][0]["actor"]
    assert actor["first_name"] != "Jean" and actor["first_name"]
    assert actor["last_name"] != "Dupont"
    assert "profile_picture_url" not in actor
