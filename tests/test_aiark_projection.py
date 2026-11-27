"""AI Ark — la page de recherche resserrée par défaut, le brut sur demande.

Signal #364 : « every call with size=100 returns 2.8-3.2M characters and blows the token
limit, forcing a spill-to-file + jq parse for EVERY page. 11 pages fetched in this run =
11 forced file round-trips. » Les guides de sourcing paginent les grands viviers — le
surcoût est donc PAR PAGE, et un agent sans shell (client MCP nu, n8n) cale tout court.

L'enregistrement ci-dessous est une CAPTURE d'un retour réel du 14/08 (élagué à ses clés,
les valeurs longues remplacées par du remplissage de même ordre de grandeur) : un banc qui
reconstitue une forme qu'on IMAGINE mesure la représentation qu'on s'en fait, pas le
système.
"""
import json

from oto_mcp.tools import aiark

_LONG = "Description de la société, son marché, ses offres. " * 40

# Forme réelle d'un `content[]` de `op=people` (clés de premier niveau exhaustives).
PERSON = {
    "id": "03a4cc1e", "identifier": "laportealexis",
    "profile": {"first_name": "Alexis", "middle_name": None, "last_name": "Laporte",
                "full_name": "Alexis Laporte",
                "headline": "AI Engineer & Entrepreneur", "title": "AI Founding Engineer",
                "birth_date": None,
                "picture": {"source": "https://images.ai-ark.com/" + "x" * 120},
                "background": {"source": "https://media.licdn.com/" + "y" * 160},
                "summary": "Tech entrepreneur since 2010. " * 20},
    # Les clés à `None` et les sous-blocs sont ceux d'une capture du 10/09/2026 : la
    # capture du 14/08 avait été élaguée à ce que le sourcing lit, et un banc qui ne
    # porte pas une clé ne peut pas mesurer ce qu'elle coûte.
    "link": {"linkedin": "https://www.linkedin.com/in/laportealexis",
             "twitter": None, "github": None, "facebook": None},
    "location": {"default": "Greater Marseille Metropolitan Area, France",
                 "short": "Greater Marseille Metropolitan Area", "country": "France",
                 "state": "Provence-Alpes-Côte d'Azur", "city": "Marseille", "position": None},
    "languages": {"profile_languages": [{"name": "English"}, {"name": "French"}]},
    "industry": "Computer Software",
    "educations": [{"school": {"name": "ENSEEIHT"}, "degree_name": "Master"}] * 3,
    "awards": [{"title": "Prix INP Innov'"}],
    "position_groups": [{"company": {"name": "Otomata"},
                         "profile_positions": [{"description": _LONG}]}] * 10,
    "volunteer_experiences": [{"role": "Coach", "company": {"name": "French Tech"}}] * 5,
    "skills": ["Anthropic Claude", "Intelligence artificielle"] * 11,
    "member_badges": {"premium": True, "creator": True},
    "statistics": {"network": {"followers_count": 4191}},
    "company": {"id": "7e62bf93",
                "summary": {"name": "Otomata", "description": "Otomata builds AI",
                            "industry": "software development", "staff": {"total": 1}},
                "link": {"linkedin": "https://www.linkedin.com/company/otomata-tech"},
                "location": {"headquarter": {"city": "Marseille", "country": "France"},
                             "locations": [{"city": "Marseille"}] * 4},
                "industries": ["software development"], "languages": ["english", "french"],
                "technologies": ["hubspot", "aws"] * 30, "keywords": ["ai"] * 40,
                "naics": ["541511"], "last_updated": "2026-07-20"},
    "department": {"departments": ["engineering"], "sub_departments": ["software"],
                   "functions": ["engineering"], "seniority": "senior"},
    "last_updated": "2026-07-20",
}

PAGE = {"content": [PERSON] * 100, "size": 100, "totalElements": 6, "totalPages": 6,
        "pageable": {"pageNumber": 0, "pageSize": 100}, "trackId": "436d00b7",
        "number": 0, "empty": False}


def _size(p) -> int:
    return len(json.dumps(p, ensure_ascii=False))


def test_le_defaut_resserre_une_page_de_100_sous_le_plafond():
    brut = _size(PAGE)
    vue = _size(aiark._shape(PAGE, "people", full=False, fields=None))
    # Le banc reproduit l'ordre de grandeur signalé (millions de caractères).
    assert brut > 1_000_000
    # Et la vue de tri ramène la page dans ce qu'un agent lit en ligne.
    assert vue < brut / 10


def test_ce_que_le_sourcing_LIT_survit_a_la_projection():
    out = aiark._shape(PAGE, "people", full=False, fields=None)
    p = out["content"][0]
    assert p["id"] and p["identifier"]
    assert p["profile"]["full_name"] == "Alexis Laporte"
    assert p["profile"]["title"] and p["profile"]["headline"]
    assert p["link"]["linkedin"] and p["location"]["country"] == "France"
    assert p["department"]["seniority"] == "senior"
    # L'identité de la société reste — c'est elle qu'on qualifie.
    assert p["company"]["summary"]["name"] == "Otomata"
    assert p["company"]["location"]["headquarter"]["city"] == "Marseille"


def test_ce_qui_est_ecarte_est_ce_que_le_sourcing_ne_lit_jamais():
    p = aiark._shape(PAGE, "people", full=False, fields=None)["content"][0]
    for k in ("educations", "skills", "statistics", "member_badges", "languages",
              "position_groups", "volunteer_experiences", "awards"):
        assert k not in p, k
    assert "picture" not in p["profile"] and "background" not in p["profile"]
    # Élargi le 10/09 : le « À propos », les clés toujours nulles, les sous-blocs.
    for k in ("summary", "middle_name", "birth_date"):
        assert k not in p["profile"], k
    for k in ("twitter", "github", "facebook"):
        assert k not in p["link"], k
    for k in ("short", "state", "position"):
        assert k not in p["location"], k
    for k in ("sub_departments", "functions"):
        assert k not in p["department"], k
    # …et ce que le tri côté client LIT reste : `departments` est le remède documenté
    # au filtre mort `contact.department`, `city` et `country` situent le profil.
    assert p["department"]["departments"] == ["engineering"]
    assert p["location"]["city"] == "Marseille" and p["location"]["country"] == "France"
    # Les blocs répétés à l'identique sur les 100 personnes d'une même société.
    for k in ("technologies", "keywords", "naics"):
        assert k not in p["company"], k
    assert "locations" not in p["company"]["location"]


def test_l_enveloppe_de_pagination_est_INTACTE():
    # Sans elle, l'agent croit avoir tout vu et n'ira pas chercher la page suivante.
    out = aiark._shape(PAGE, "people", full=False, fields=None)
    for k in ("totalElements", "totalPages", "pageable", "trackId", "size", "number"):
        assert out[k] == PAGE[k], k


def test_full_rend_la_page_BRUTE_sans_copie_ni_perte():
    assert aiark._shape(PAGE, "people", full=True, fields=None) is PAGE


def test_fields_projette_les_enregistrements_sans_toucher_a_l_enveloppe():
    out = aiark._shape(PAGE, "people", full=False, fields=["id", "link"])
    assert set(out["content"][0]) == {"id", "link"}
    assert out["totalElements"] == 6 and out["trackId"] == "436d00b7"


def test_une_forme_inattendue_passe_sans_lever():
    # Une API tierce change de forme sans prévenir : une projection qui lève
    # transformerait une réponse utile en panne, et c'est le connecteur qu'on blâmerait.
    assert aiark._shape({"content": "pas une liste"}, "people", False, None) == {"content": "pas une liste"}
    assert aiark._shape([], "people", False, None) == []
    assert aiark._shape({"content": [None, 3]}, "people", False, None)["content"] == [None, 3]


def test_une_societe_est_delestee_de_ses_blocs_repetes():
    page = {"content": [PERSON["company"]], "totalElements": 1}
    c = aiark._shape(page, "companies", full=False, fields=None)["content"][0]
    assert c["summary"]["name"] == "Otomata"          # l'identité reste
    assert "technologies" not in c and "keywords" not in c and "naics" not in c


def test_la_description_de_fields_nomme_toutes_les_cles_de_la_vue():
    """Signal 717 (oto#91) : une projection dont les clés ne sont pas énumérées se
    règle à l'aveugle, et une clé inconnue est écartée EN SILENCE — les fiches
    ressortent vides. La description servie doit donc nommer CHAQUE clé de premier
    niveau que la vue par défaut rend, sur les deux points d'accès. Dérivé de la
    capture réelle ci-dessus : une clé que la vue gagne sans que le texte la nomme
    fait échouer ce banc."""
    import asyncio

    from fastmcp import FastMCP

    async def go():
        m = FastMCP("t")
        aiark.register(m)
        # La section `Args` part dans le SCHÉMA servi, en description du paramètre.
        t = await m.get_tool("linkedin_aiark_search")
        return t.parameters["properties"]["fields"]["description"]

    d = asyncio.run(go())
    personne = aiark._shape(PAGE, "people", full=False, fields=None)["content"][0]
    societe = aiark._shape({"content": [PERSON["company"]]}, "companies",
                           full=False, fields=None)["content"][0]
    for cle in set(personne) | set(societe):
        assert f"`{cle}`" in d, f"clé `{cle}` rendue par la vue, absente du texte servi"
    assert "silently discarded" in d


def test_la_photo_revient_sur_demande():
    """`with_photo=True` (26/11/2026) : `profile.picture` reste, la bannière non."""
    p = aiark._shape(PAGE, "people", full=False, fields=None, with_photo=True)["content"][0]
    assert p["profile"]["picture"]["source"].startswith("https://images.ai-ark.com/")
    assert "background" not in p["profile"] and "summary" not in p["profile"]
