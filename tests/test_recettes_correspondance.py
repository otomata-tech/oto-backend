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
    # Pas de borne de pages imposée : absente, `max_pages` reste absente.
    assert "max_pages" not in c["limits"] and c["where"] == []


@pytest.mark.parametrize("surcharge,morceau", [
    ({"limits": {}}, "max_units"),
    ({"tool": "oto_call"}, "platform tool"),
    ({"tool": "data_write"}, "platform tool"),
    ({"key": {"column": "x"}}, "`map` columns"),
    ({"arguments": {"a": "{{item.x}}"}}, "unknown scope"),
    ({"map": {"_id": "id"}}, "not a valid column"),
    ({"source": {"pagination": {"type": "cursor", "param": "c"}}}, "pagination.next"),
    ({"mode": "per_row"}, "only `pull`"),
    ({"tool": "jev_ask"}, "runs a model"),
    ({"tool": "lighton_ask"}, "runs a model"),
    ({"limits": {"max_units": 0}}, "at least 1"),
    ({"limits": {"max_units": 1000, "max_pages": 0}}, "`limits.max_pages` is at least 1"),
    ({"limits": {"max_units": 10}}, "at least `source.pagination.size`"),
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


@pytest.mark.parametrize("filtre,brut,attendu", [
    ("domain", "https://www.Acme.test/about?x=1", "acme.test"),
    ("domain", "jane@acme.test", "acme.test"),
    ("domain", "acme", None),
    ("email", " Jane@Acme.test ", "jane@acme.test"),
    ("email", "not found", None),
    ("email_domain", "jane@acme.test", "acme.test"),
    ("linkedin_slug", "https://fr.linkedin.com/in/Jane-Doe/?trk=x", "jane-doe"),
    ("linkedin_slug", "https://www.linkedin.com/company/acme-co/", "acme-co"),
    ("linkedin_slug", "jane-doe", "jane-doe"),
    ("url", "acme.test/", "https://acme.test"),
    ("url", "HTTP://Acme.test/a/", "http://acme.test/a"),
    ("digits", "+33 6 12 34", "3361234"),
])
def test_les_normaliseurs(filtre, brut, attendu):
    assert co.rendre(f"{{{{params.v|{filtre}}}}}", {"params": {"v": brut}}) == attendu


def test_une_clause_normalisee_compare_les_formes():
    site = {"site": "https://www.acme.test/team"}
    assert co.garde(site, [{"path": "site", "op": "eq", "value": "{{row.domain}}",
                            "normalize": "domain"}], {}, row={"domain": "ACME.test"})
    assert not co.garde(site, [{"path": "site", "op": "not_in_table", "normalize": "domain"}],
                        {}, ensembles={0: {"acme.test"}})
    # Un élément sans valeur n'est pas dans la liste d'exclusion : il passe.
    assert co.garde({}, [{"path": "site", "op": "not_in_table", "normalize": "domain"}],
                    {}, ensembles={0: {"acme.test"}})


@pytest.mark.parametrize("filtre,brut,attendu", [
    ("email", "mailto:Jane@Acme.test", "jane@acme.test"),
    ("linkedin_slug", "https://evil.test/in/x", None),
    ("linkedin_slug", "acme.test", None),
    ("linkedin_slug", "linkedin.com/in/Jane-Doe", "jane-doe"),
])
def test_les_normaliseurs_refusent_ce_qui_n_a_pas_la_forme(filtre, brut, attendu):
    assert co.normaliser(brut, filtre) == attendu


def test_url_comparee_sans_schema_ni_www_ecrite_avec():
    assert co.normaliser("http://acme.test", "url") == co.normaliser(
        "https://www.acme.test/", "url") == "acme.test"
    # Écrite dans une colonne, elle garde son schéma.
    assert co.rendre("{{params.v|url}}", {"params": {"v": "acme.test"}}) == "https://acme.test"


@pytest.mark.parametrize("filtre", ["email", "domain", "url", "linkedin_slug", None])
def test_un_normaliseur_refuse_une_liste(filtre):
    with pytest.raises(co.ValeurNonScalaire):
        co.normaliser(["jane@acme.test"], filtre)
    with pytest.raises(co.ValeurNonScalaire):
        co.garde({"e": ["jane@acme.test"]},
                 [{"path": "e", "op": "not_in_table", "normalize": filtre}], {},
                 ensembles={0: set()})


def test_une_recette_code_n_a_pas_de_plafond_de_plateforme():
    """Les bornes d'une recette-code sont celles de qui l'écrit : aucune borne haute imposée,
    `max_calls` absent = pas de borne en nombre d'appels ; l'absurde reste refusé."""
    from oto_mcp.recipes import code
    corps = {"kind": "code", "sources": {"recette.py": "async def run(oto, params):\n"
                                                      "    return {}\n"},
             "entrypoint": "recette:run", "params": {}}
    grand = code.valider({**corps, "limits": {"max_units": 10_000_000, "max_calls": 50_000}})
    assert grand["limits"]["max_calls"] == 50_000
    assert "max_calls" not in code.valider({**corps, "limits": {"max_units": 5}})["limits"]
    for limites, morceau in (({"max_units": 0}, "max_units"),
                             ({"max_units": 5, "max_calls": 0}, "max_calls"),
                             ({"max_units": 5, "max_writes": -1}, "max_writes")):
        with pytest.raises(contrat.RecetteInvalide) as e:
            code.valider({**corps, "limits": limites})
        assert any(morceau in p for p in e.value.problemes), e.value.problemes
