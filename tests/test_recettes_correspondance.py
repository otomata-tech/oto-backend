"""Recettes — la correspondance (chemins, gabarits, filtres, `where`) et le contrat."""
from __future__ import annotations

import pytest

from oto_mcp.recipes import contrat
from oto_mcp.recipes import correspondance as co

PERSONNE = {"id": "p-1",
            "profile": {"full_name": "Jane Doe", "title": "Head of Marketing",
                        "headline": "Marketing at Acme"},
            "link": {"linkedin": "https://www.linkedin.com/in/jane-doe"},
            "location": {"country": "Spain", "city": "Madrid"},
            "emails": [{"email": "jane@acme.test"}]}


def test_lire_suit_les_chemins_et_les_index():
    assert co.lire(PERSONNE, "profile.title") == "Head of Marketing"
    assert co.lire(PERSONNE, "emails[0].email") == "jane@acme.test"
    assert co.lire(PERSONNE, "emails[3].email") is None
    assert co.lire(PERSONNE, "nope.deeper") is None
    assert co.lire(PERSONNE, "") is PERSONNE


@pytest.mark.parametrize("brut,attendu", [
    ("Café Lumière", "cafe_lumiere"), ("4B Conseil", "4b_conseil"), ("Acme", "acme"),
    ("Acme Group (Ex : Old Acme)", "acme_group_ex_old_acme"),
    ("Nord - Sud Gestion", "nord_sud_gestion"), ("L’Atelier Hélène", "l_atelier_helene"),
])
def test_slug_reproduit_les_cles_deja_ecrites(brut, attendu):
    # La forme des clés que les procédures de sourcing écrivent déjà : la changer
    # dédoublerait chaque ligne au premier passage d'une recette.
    assert co.slug(brut) == attendu


def test_un_gabarit_seul_garde_son_type_mele_il_devient_texte():
    portees = {"params": {"ids": ["a", "b"], "company": "Acme Co"}, "item": PERSONNE}
    assert co.rendre("{{params.ids}}", portees) == ["a", "b"]
    assert co.rendre({"include": ["{{params.company}}"]}, portees) == {"include": ["Acme Co"]}
    assert co.rendre("{{params.company|slug}}::{{item.link.linkedin}}", portees) == \
        "acme_co::https://www.linkedin.com/in/jane-doe"


def test_une_portee_ou_un_filtre_inconnus_refusent():
    with pytest.raises(co.GabaritInvalide):
        co.rendre("{{row.x}}", {"params": {}})
    with pytest.raises(co.GabaritInvalide):
        co.rendre("{{params.x|reverse}}", {"params": {"x": "a"}})


def test_where_compare_sans_casse_ni_accents():
    p = {"country": "spain"}
    assert co.garde(PERSONNE, [{"path": "location.country", "op": "eq",
                                "value": "{{params.country}}"}], p)
    assert not co.garde(PERSONNE, [{"path": "location.country", "op": "ne",
                                    "value": "SPAIN"}], p)
    assert co.garde(PERSONNE, [{"path": "profile.title", "op": "contains_any",
                                "value": ["marketing", "digital"]}], p)
    assert not co.garde(PERSONNE, [{"path": "profile.title", "op": "in",
                                    "value": ["CEO"]}], p)
    assert co.garde(PERSONNE, [{"path": "profile.summary", "op": "empty"}], p)


def test_la_cle_d_un_element_incomplet_est_absente():
    spec = {"column": "contact_key", "template": "{{params.company|slug}}::{{item.link.linkedin}}"}
    sans_profil = {"profile": {"title": "CFO"}}
    assert co.cle(sans_profil, spec, {}, {"company": "Acme"}) is None
    assert co.cle(PERSONNE, spec, {}, {"company": "Acme"}) == \
        "acme::https://www.linkedin.com/in/jane-doe"


def test_la_ligne_prend_la_correspondance_puis_les_valeurs_fixes():
    corr = {"title": "profile.title", "headline": {"path": "profile.headline", "max": 9},
            "source": {"const": "aiark"}, "phone": {"path": "phone", "default": ""}}
    l = co.ligne(PERSONNE, corr, {"company": "{{params.company}}", "status": "sourced"},
                 {"company": "Acme"})
    assert l == {"title": "Head of Marketing", "headline": "Marketing", "source": "aiark",
                 "phone": "", "company": "Acme", "status": "sourced"}


def _recette(**surcharge):
    corps = {"tool": "linkedin_aiark_search",
             "arguments": {"op": "people", "account": {"id": {"any": {"include": [
                 "{{params.company_uuid}}"]}}}},
             "params": {"company_uuid": {"required": True}, "company": {"required": True}},
             "source": {"items": "content", "pagination": {
                 "type": "page", "param": "page", "start": 0, "size": 50,
                 "size_param": "size", "last": "last"}},
             "map": {"linkedin_url": "link.linkedin", "title": "profile.title"},
             "values": {"company": "{{params.company}}"},
             "key": {"column": "contact_key",
                     "template": "{{params.company|slug}}::{{item.link.linkedin}}"},
             "limits": {"max_units": 500}}
    corps.update(surcharge)
    return corps


def test_une_recette_complete_passe_et_recoit_ses_defauts():
    c = contrat.valider(_recette())
    assert c["mode"] == "pull" and c["on_existing"] == "skip" and c["units"] == "items"
    assert c["limits"]["max_pages"] == contrat.MAX_PAGES_DEFAUT and c["where"] == []


@pytest.mark.parametrize("surcharge,morceau", [
    ({"limits": {}}, "max_units"),
    ({"tool": "oto_call"}, "platform tool"),
    ({"tool": "data_write"}, "platform tool"),
    ({"key": {"column": "x"}}, "`map` columns"),
    ({"arguments": {"a": "{{item.x}}"}}, "unknown scope"),
    ({"map": {"_id": "id"}}, "not a valid column"),
    ({"source": {"pagination": {"type": "cursor", "param": "c"}}}, "pagination.next"),
    ({"mode": "per_row"}, "only `pull`"),
])
def test_une_recette_fautive_est_refusee_avec_tous_ses_defauts(surcharge, morceau):
    with pytest.raises(contrat.RecetteInvalide) as e:
        contrat.valider(_recette(**surcharge))
    assert any(morceau in p for p in e.value.problemes)


def test_les_params_requis_manquants_et_inconnus_refusent():
    c = contrat.valider(_recette())
    with pytest.raises(contrat.RecetteInvalide):
        contrat.params_resolus(c, {"company": "Acme"})
    with pytest.raises(contrat.RecetteInvalide):
        contrat.params_resolus(c, {"company": "Acme", "company_uuid": "u", "contry": "ES"})
    assert contrat.params_resolus(c, {"company": "Acme", "company_uuid": "u"}) == \
        {"company": "Acme", "company_uuid": "u"}
