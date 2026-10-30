"""La liste fermée des écritures chez un tiers (`recipes/ecritures.py`, #1202 lot 3) ne
ment pas : chaque outil est servi par le catalogue monté, chaque op qu'elle nomme est
une op que l'outil connaît, aucune ne détruit, et un outil sans `op` n'en a vraiment
pas."""
from __future__ import annotations

import pytest

from _mcp_app import static_mcp
from oto_mcp.recipes import ecritures as ec


def _ops(tool) -> set:
    props = (getattr(tool, "parameters", None) or {}).get("properties") or {}
    op = props.get("op") or {}
    enum = list(op.get("enum") or [])
    for alt in op.get("anyOf") or []:
        enum += alt.get("enum") or []
    return {str(o) for o in enum}


@pytest.mark.asyncio
async def test_chaque_outil_est_servi_et_ses_ops_existent():
    servis = {t.name: t for t in await static_mcp().list_tools(run_middleware=False)}
    assert not set(ec.ECRITURES) - set(servis), sorted(set(ec.ECRITURES) - set(servis))
    for nom, e in ec.ECRITURES.items():
        connues = _ops(servis[nom])
        if e.ecrire is None:
            assert not connues, f"{nom} a une `op` : nommer ses ops"
        else:
            assert (e.lire or set()) | e.ecrire <= connues, (nom, connues)


def test_aucune_op_ne_detruit_et_lire_n_ecrit_pas():
    for nom, e in ec.ECRITURES.items():
        assert not (e.lire or set()) & ec.OPS_DESTRUCTRICES, nom
        assert not (e.ecrire or set()) & ec.OPS_DESTRUCTRICES, nom
        assert not (e.lire or set()) & (e.ecrire or set()), nom


def test_juger_exige_une_op_litterale():
    assert ec.juger("folk_record", {})[0] == "refus"
    assert ec.juger("folk_record", {"op": "delete"})[0] == "refus"
    assert ec.juger("folk_record", {"op": "search"}) == ("lire", None)
    assert ec.juger("folk_record", {"op": "create"}) == ("ecrire", "folk_record:create")
    assert ec.juger("lemlist_create_lead", {"campaign_id": "c"}) == (
        "ecrire", "lemlist_create_lead")


def test_les_declarables_et_les_envois():
    assert "folk_record:create" in ec.declarables()
    assert "folk_record:search" not in ec.declarables()
    assert "folk_record:delete" not in ec.declarables()
    assert ec.envoient(["folk_record:create", "lemlist_create_lead"]) == [
        "lemlist_create_lead"]


def test_allo_act_ecrit_la_fiche_et_la_file_jamais_un_envoi():
    assert ec.juger("allo_act", {"op": "create_person"}) == ("ecrire", "allo_act:create_person")
    assert ec.juger("allo_act", {"op": "add_queue_numbers"}) == (
        "ecrire", "allo_act:add_queue_numbers")
    for op in ("send_sms", "create_queue", "remove_queue_numbers"):
        assert ec.juger("allo_act", {"op": op})[0] == "refus", op
    assert ec.envoient(["allo_act:create_person"]) == []


def test_les_numeros_d_une_file_se_comptent_un_par_un():
    from oto_mcp.recipes.pont import _fiches
    assert _fiches({"op": "add_queue_numbers", "entries": [{"number": "+33"}] * 3}) == 3
    assert _fiches({"op": "add_queue_numbers", "entries": "x"}) is None
    assert _fiches({"op": "create_person"}) == 1


def test_une_recette_lit_les_comptes_et_les_exports_d_une_campagne_lemlist_sans_rien_ecrire():
    """Comparer les leads d'un tableau à ceux des campagnes avant de pousser : des
    lectures, sans déclaration. Créer, mettre en pause, envoyer un export par mail :
    refusé — rien de `lemlist_campaign` ne se déclare. `export_status` aussi : sans
    `export_start`, une recette n'a jamais l'id d'un export."""
    for op in ("reports", "batch_stats", "export_leads", "statutes"):
        assert ec.juger("lemlist_campaign", {"op": op}) == ("lire", None), op
    for op in ("create", "update", "pause", "duplicate", "export_email", "export_start",
               "export_status"):
        assert ec.juger("lemlist_campaign", {"op": op})[0] == "refus", op
    assert not any(d.startswith("lemlist_campaign") for d in ec.declarables())


def test_une_recette_lit_les_watch_lists_les_desinscriptions_et_les_fiches_lemlist():
    """Lire les prises d'une watch list, la liste de désinscription et les fiches du CRM
    lemlist avant de pousser : des lectures, sans déclaration. Tout ce qui crée, modifie,
    désinscrit ou réinscrit est refusé — rien de ces trois outils ne se déclare."""
    lectures = {
        "lemlist_watchlist": ("list", "filters", "filter_values", "library", "history",
                              "signals"),
        "lemlist_unsubscribe": ("list", "get", "export", "var_list", "var_get",
                                "var_export", "contact_status", "contact_export"),
        "lemlist_contact": ("list", "get", "lists", "list_export"),
    }
    ecritures = {
        "lemlist_watchlist": ("create", "update", "delete", "push_signals"),
        "lemlist_unsubscribe": ("add", "delete", "var_add", "var_bulk", "var_remove",
                                "contact_add", "contact_remove"),
        "lemlist_contact": ("upsert", "delete", "list_create", "list_manage"),
    }
    for outil, ops in lectures.items():
        for op in ops:
            assert ec.juger(outil, {"op": op}) == ("lire", None), (outil, op)
    for outil, ops in ecritures.items():
        for op in ops:
            assert ec.juger(outil, {"op": op})[0] == "refus", (outil, op)
        assert not any(d.startswith(outil) for d in ec.declarables()), outil


# --- La règle générale (28/10/2026) : tout outil de connecteur, classé par son verbe ---

from oto_mcp.recipes import effets  # noqa: E402


@pytest.mark.parametrize("outil,op,classe", [
    ("slack_act", "send", "envoyer"),
    ("slack_post_message", None, "envoyer"),
    ("slack_delete_message", None, "detruire"),
    ("hubspot_object", "create", "ecrire"),
    ("acme_act", "upsert_contact", "ecrire"),
    ("acme_act", "list", "lire"),
    ("acme_act", "bulk_create", "detruire"),
    ("acme_act", "merge", "detruire"),
    # Un verbe inconnu n'est JAMAIS présumé lecteur.
    ("acme_act", "reveal_phone", "ecrire"),
    ("acme_act", "export_start", "ecrire"),
])
def test_effets_classe_par_le_verbe(outil, op, classe):
    assert effets.classer(outil, op) == classe


@pytest.mark.parametrize("nom", ["slack_act:send", "hubspot_object:create",
                                 "slack_post_message", "notion_act:create_page"])
def test_une_ecriture_de_connecteur_se_declare(nom):
    assert ec.probleme_de_declaration(nom) is None, ec.probleme_de_declaration(nom)


@pytest.mark.parametrize("nom,motif", [
    ("slack_delete_message", "deletes"),
    ("hubspot_object:delete", "not open"),        # outil forcé : ses seules ops
    ("slack_act:list", "reads"),
    ("jev_rows", "model"),
    ("data_write", "platform"),
    ("acme_thing:create", "not a connector"),
    ("Slack Act:send", "is named"),
])
def test_ce_qui_ne_se_declare_pas(nom, motif):
    assert motif in (ec.probleme_de_declaration(nom) or "")


def test_les_envois_de_la_regle_generale():
    assert ec.envoient(["slack_act:send", "hubspot_object:create",
                        "lemlist_create_lead", "notion_act:create_page"]) == \
        ["lemlist_create_lead", "slack_act:send"]


def test_une_recette_code_declare_un_envoi_hors_liste_avec_allow_sending():
    from oto_mcp.recipes import code
    corps = {"kind": "code", "sources": {"r.py": "async def run(oto, params):\n    return {}\n"},
             "entrypoint": "r:run", "writes": ["slack_act:send"],
             "limits": {"max_units": 1, "max_sends": 5}}
    with pytest.raises(Exception) as e:
        code.valider(corps)
    assert "allow_sending" in str(e.value.problemes)
    valide = code.valider({**corps, "allow_sending": True})
    assert valide["limits"]["max_sends"] == 5 and valide["limits"]["max_writes"] == 50


def test_max_sends_est_un_entier_positif_ou_absent():
    from oto_mcp.recipes import code
    corps = {"kind": "code", "sources": {"r.py": "async def run(oto, params):\n    return {}\n"},
             "entrypoint": "r:run", "limits": {"max_units": 1, "max_sends": -1}}
    with pytest.raises(Exception) as e:
        code.valider(corps)
    assert "max_sends" in str(e.value.problemes)
