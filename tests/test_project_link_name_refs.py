"""Fix #117 : un lien projet `tableau` créé par NOM (target_ref = nom, pas id) doit
résoudre son `namespace` — sinon `resolve_slot_tableau` le voyait « ne résout plus ».
`_apply_tableau_name_refs` attache le nom quand le namespace existe, sans toucher aux
liens déjà résolus (chemin id) ni aux non-tableaux."""
from __future__ import annotations

from oto_mcp.db import projects as P


def test_name_ref_existing_gets_namespace():
    links = [{"target_type": "tableau", "target_ref": "vivier-pmi"}]
    P._apply_tableau_name_refs(links, {"vivier-pmi", "autre"})
    assert links[0]["namespace"] == "vivier-pmi"


def test_name_ref_missing_stays_unresolved():
    links = [{"target_type": "tableau", "target_ref": "disparu"}]
    P._apply_tableau_name_refs(links, {"vivier-pmi"})
    assert "namespace" not in links[0]          # dead-link préservé (signalé à l'usage)


def test_id_resolved_link_untouched():
    # déjà résolu par le chemin id → ne pas écraser
    links = [{"target_type": "tableau", "target_ref": "109", "namespace": "vivier-pmi"}]
    P._apply_tableau_name_refs(links, {"109"})   # même si "109" existait comme nom
    assert links[0]["namespace"] == "vivier-pmi"


def test_non_tableau_untouched():
    links = [{"target_type": "connecteur", "target_ref": "folk"}]
    P._apply_tableau_name_refs(links, {"folk"})
    assert "namespace" not in links[0]
