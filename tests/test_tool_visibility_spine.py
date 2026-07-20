"""Anti-lockout SPINE de la visibilité d'outils (signal d’usage #213).

Les tools spine (gestion projet/docs, datastore, boucle d'usage) ne doivent JAMAIS
être masqués — ni par un toggle perso, ni par le gating connecteur/sélection. On
teste la source unique `is_protected` + son respect par `is_tool_visible`. Le garde
final de `compute_hidden_tools` (`to_hide -= {n if is_protected(n)}`) applique la
même fonction sur le chemin DB (vérifié au déploiement).
"""
from __future__ import annotations

from oto_mcp.tool_visibility import is_protected, is_tool_visible


def test_spine_tools_are_protected():
    # Famille projet + docs (le cœur du bug #213)
    for n in ("oto_project", "oto_use_project", "oto_doc", "oto_doc_app"):
        assert is_protected(n), n
    # Datastore : namespace `data` entier (trop de noms pour lister)
    for n in ("data_write", "data_rows", "data_create_namespace", "data_claim_next",
              "data_set_schema", "data_delete_row"):
        assert is_protected(n), n
    # Boucle d'usage + identité + échappatoires + dispatch
    for n in ("feedback", "run_start", "run_finish", "oto_whoami", "oto_profile",
              "oto_use_org", "oto_clear_org", "oto_call", "oto_tool_schema", "oto_guide"):
        assert is_protected(n), n


def test_admin_and_connector_tools_are_NOT_protected():
    # oto_admin_* reste gaté par rôle (namespace `oto` non protégé en bloc)
    for n in ("oto_admin_org", "oto_admin_signal", "oto_admin_user"):
        assert not is_protected(n), n
    # outils de connecteur = jamais spine
    for n in ("apollo_search_people", "serper_web_search", "zoho_records",
              "unipile_search", "folk_list_notes"):
        assert not is_protected(n), n
    # autres oto_* NON spine (gérés par nom, pas protégés)
    for n in ("oto_org", "oto_resource", "oto_connector"):
        assert not is_protected(n), n


def test_protected_tool_ignores_disable_toggle():
    # oto_doc / data_write protégés → visibles MÊME si l'user les a désactivés
    assert is_tool_visible("oto_doc", disabled={"oto_doc"}, enabled_override=set())
    assert is_tool_visible("data_write", disabled={"data_write"}, enabled_override=set())
    assert is_tool_visible("feedback", disabled={"feedback"}, enabled_override=set())


def test_non_protected_tool_respects_disable():
    # un outil de connecteur désactivé reste masqué
    assert not is_tool_visible("apollo_search_people",
                               disabled={"apollo_search_people"}, enabled_override=set())
