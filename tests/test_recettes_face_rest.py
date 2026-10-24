"""`POST /api/me/recipes` — la capacité `me.recipe` sur la face REST.

Ce qui est éprouvé : le BINDING tel que le registre le sert (verbe, chemin,
provisoire), que la garde reste celle de la face MCP (la même règle par op, pas une
seconde), et que la route est déclarée lourde (un `run` appelle un outil page après
page). Le comportement des ops est celui de la capacité, éprouvé par les autres
`test_recettes_*` : la face REST n'en ajoute aucun.
"""
from __future__ import annotations

from oto_mcp.api import routes_lourdes as rl
from oto_mcp.capabilities import recipes as cap  # noqa: F401 — enregistre la capacité
from oto_mcp.capabilities.registry import CAPABILITIES


def _capacite():
    (c,) = [c for c in CAPABILITIES if c.key == "me.recipe"]
    return c


def test_la_capacite_est_servie_en_rest_sous_post_api_me_recipes():
    c = _capacite()
    assert c.rest is not None
    assert (c.rest.verb, c.rest.path) == ("POST", "/api/me/recipes")
    # Provisoire comme la capacité : publié `x-oto-provisoire` dans l'OpenAPI.
    assert c.rest.provisoire is True
    # Le corps est l'Input entier (`{op, slug, params, datastore…}`), pas un champ.
    assert c.rest.body_field is None


def test_les_deux_faces_partagent_la_meme_garde():
    c = _capacite()
    # Une seule capacité, une seule règle : la face REST ne porte pas de garde à elle.
    assert c.mcp == "oto_recipe"
    assert c.authz is not None


def test_la_route_est_declaree_lourde():
    lourdes = [r for r in rl.ROUTES_LOURDES if (r.methode, r.gabarit) == ("POST", "/api/me/recipes")]
    assert len(lourdes) == 1
    assert lourdes[0].concurrence > 0 and lourdes[0].par_minute > 0
