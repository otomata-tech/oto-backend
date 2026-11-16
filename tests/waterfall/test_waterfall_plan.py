"""Le plan d'une cascade, pour un écran (`waterfall/plan.py`, capacité `me.waterfall_plan`)
— l'ordre, la clé de chaque étape et tout ce qui est offert ; des FAITS NEUTRES (les outils
appelés, ce qui leur est demandé, les bornes), jamais un prix : la correspondance avec une
grille vit dans le front d'une instance."""
import asyncio

import pytest

from oto_mcp.waterfall import email, gens, ordre, plan, societe, telephone

from _waterfall_faux import calls  # noqa: F401 — la fixture des fournisseurs simulés


# --- le plan, pour un écran (`me.waterfall_plan`) ---------------------------------

def test_le_plan_dit_l_ordre_les_cles_et_tout_ce_qui_est_offert(monkeypatch):
    monkeypatch.setattr(ordre, "_configured_steps", lambda ns: ["lusha", "nope", "hunter"])
    monkeypatch.setattr(ordre, "_key_modes", lambda sub, tools: {
        "lusha_search_and_enrich": "org", "hunter_email_finder": "forbidden"})
    out = plan.plan("waterfall_email", "sub-test")
    assert out["order_from"] == "setting" and out["ignored_steps"] == ["nope"]
    assert [(s["key"], s["key_mode"], s["usable"]) for s in out["steps"]] == [
        ("lusha", "org", True), ("hunter", "forbidden", False)]
    assert [s["key"] for s in out["available"]] == [s.key for s in email.EMAIL_STEPS]
    first = next(s for s in out["available"] if s["key"] == "hunter")
    assert first["connector"] == "hunter" and first["needs"] == "first_name+last_name+domain"
    assert first["requires"] == [["first_name", "last_name", "domain"]]
    labels = [s["label"] for s in plan.plan("waterfall_company", "sub-test")["available"]]
    assert len(labels) == len(set(labels)), labels
    assert "key_mode" not in first
    aiark = next(s for s in out["available"] if s["key"] == "aiark")
    assert aiark["connector"] == "aiark"


def test_le_plan_est_servi_par_une_capacite_rest_qui_refuse_un_autre_connecteur():
    import oto_mcp.capabilities  # noqa: F401 — les déclarations
    from oto_mcp.capabilities.connectors import waterfall_plan as cap
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities.registry import CAPABILITIES
    decl = next(c for c in CAPABILITIES if c.key == "me.waterfall_plan")
    assert decl.rest.path == "/api/me/waterfalls/{name}/plan" and decl.mcp is None
    ctx = type("Ctx", (), {"sub": "sub-test"})()
    with pytest.raises(AuthzDenied):
        asyncio.run(cap._plan(ctx, cap.WaterfallPlanInput(name="hunter")))


def test_le_plan_ne_porte_aucun_prix_mais_des_faits_neutres(calls):
    """Le tronc ne porte aucun prix (arbitrage du 20/10/2026) : chaque étape dit les outils
    qu'elle appelle, ce qu'elle leur demande et ses bornes — un front d'instance y lit sa
    propre grille."""
    for ns, steps in plan.STEPS_BY_NAMESPACE.items():
        for s in steps:
            row = plan._step_row(s)
            assert not {"price", "rate", "typical_usd"} & set(row), (ns, s.key)
            assert row["collect_tool"] == (s.collect.tool if s.collect else None)
    q = plan.plan("waterfall_people", "sub-test")["qualification"]
    assert "price" not in q


def test_les_outils_appeles_et_ce_qui_leur_est_demande():
    def row(steps, key):
        return plan._step_row(next(s for s in steps if s.key == key))
    fe = row(telephone.PHONE_STEPS, "fullenrich")
    assert (fe["tool"], fe["collect_tool"], fe["asks"]) == (
        "fullenrich_enrich_linkedin", "fullenrich_result", "phones")
    assert row(email.EMAIL_STEPS, "fullenrich")["asks"] == "work_emails"
    assert row(email.EMAIL_STEPS, "forager")["asks"] == "work_emails"
    apollo = row(gens.PEOPLE_STEPS, "apollo")
    assert (apollo["tool"], apollo["reveal"]) == ("apollo_search_people", "apollo_match_person")


def test_les_bornes_d_une_etape_sont_servies():
    def row(steps, key):
        return plan._step_row(next(s for s in steps if s.key == key))
    assert row(societe.COMPANY_STEPS, "aiark")["max_units"] == 3
    assert row(gens.PEOPLE_STEPS, "aiark")["per_item"] is True
    assert row(gens.PEOPLE_STEPS, "linkedin_employees")["per_item"] is True
    assert row(email.EMAIL_STEPS, "hunter")["max_units"] is None
    assert row(gens.PEOPLE_STEPS, "lemlist")["quota_only"] is True
    assert "quota_only" not in row(gens.PEOPLE_STEPS, "google")
    # Google : une recherche par rôle, bornée.
    assert row(gens.PEOPLE_STEPS, "google")["max_units"] == gens.GOOGLE_MAX_QUERIES


def test_le_plan_montre_la_qualification_jev_hors_des_etapes(calls):
    calls["modes"] = {"jev_items": "platform"}
    out = plan.plan("waterfall_people", "sub-test")
    q = out["qualification"]
    assert q["usable"] is True and q["connector"] == "jev"
    assert all(s["tool"] != "jev_items" for s in out["steps"])
    calls["modes"] = {"jev_items": "org"}
    q = plan.plan("waterfall_company", "sub-test")["qualification"]
    assert q["usable"] is False and q["shadowed"] is True
    assert "qualification" not in plan.plan("waterfall_email", "sub-test")


def test_chaque_etape_de_chaque_cascade_s_explique():
    """Un écran qui laisse choisir l'ordre dit ce que chaque étape apporte (`Step.explains`)."""
    sans = [(ns, s.key) for ns, steps in plan.STEPS_BY_NAMESPACE.items() for s in steps
            if not s.explains.strip()]
    assert not sans, sans
