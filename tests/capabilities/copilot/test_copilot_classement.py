"""The in-app agent's gate, on declarations: what runs, what asks, what always asks —
and the axes the server pins on every call, `oto_call`'s target included."""
from __future__ import annotations

import pytest
from mcp.types import ToolAnnotations

from oto_mcp.capabilities.copilot import classement as c
from oto_mcp.tools.lecture import ACTE, LECTURE, SIMULATION


@pytest.mark.parametrize("target,args,ann,attendu", [
    # Declared readers run.
    ("fr_search", {"q": "x"}, LECTURE, c.LECTURE),
    ("data_rows", {"datastore": 1}, None, c.LECTURE),
    ("oto_doc", {"op": "get", "doc_id": 3}, None, c.LECTURE),
    ("oto_trigger", {"op": "list"}, None, c.LECTURE),
    # A declared reader that reaches an outside URL the agent picks: always asks.
    ("web_read", {"url": "https://example.test"}, LECTURE, c.DESTRUCTIF),
    ("serper_scrape", {"url": "https://example.test"}, None, c.DESTRUCTIF),
    # A declared act (send, delete, change at a third party): always asks.
    ("gmail_act", {"op": "send"}, ACTE, c.DESTRUCTIF),
    # A draft (SIMULATION) is not a read, and not one of the person's own objects.
    ("gmail", {"op": "draft"}, SIMULATION, c.DESTRUCTIF),
    # A plain write to the person's own workspace: asks, rememberable.
    ("data_write", {"datastore": 1, "row": {"a": "b"}}, None, c.ECRITURE),
    ("oto_doc", {"op": "create", "title": "t"}, None, c.ECRITURE),
    # ... unless it carries an outside URL.
    ("data_write", {"datastore": 1, "row": {"a": "https://x.test/p"}}, None, c.DESTRUCTIF),
    # Deletes, launches, steering writes, access changes: always ask.
    ("data_delete_row", {"datastore": 1, "id": "r"}, None, c.DESTRUCTIF),
    ("oto_doc", {"op": "delete", "doc_id": 3}, None, c.DESTRUCTIF),
    ("oto_doc", {"op": "set_public", "doc_id": 3}, None, c.DESTRUCTIF),
    ("oto_trigger", {"op": "update", "trigger_id": 1, "cron": "0 9 * * 1"}, None, c.DESTRUCTIF),
    ("oto_trigger", {"op": "run_now", "trigger_id": 1}, None, c.DESTRUCTIF),
    ("oto_recipe", {"op": "run"}, None, c.DESTRUCTIF),
    ("oto_guide", {"op": "write"}, None, c.DESTRUCTIF),
    ("oto_procedure", {"op": "set"}, None, c.DESTRUCTIF),
    ("oto_org_settings", {"op": "set"}, None, c.DESTRUCTIF),
    ("data_share", {"datastore": 1}, None, c.DESTRUCTIF),
    ("oto_project", {"op": "publish_mcp"}, None, c.DESTRUCTIF),
    # A question the agent could not answer, flagged to the team: no card...
    ("feedback", {"signal": "gap", "kind": "missing_doctrine", "target": "group a table"},
     None, c.LECTURE),
    # ... unless it carries a link, and a review of a tool is not a gap.
    ("feedback", {"signal": "gap", "kind": "other", "target": "x", "text": "https://x.test"},
     None, c.DESTRUCTIF),
    ("feedback", {"signal": "tool_feedback", "kind": "bug", "target": "fr_search"},
     None, c.DESTRUCTIF),
    # An undeclared tool is never presumed a reader.
    ("someconnector_search", {"q": "x"}, None, c.DESTRUCTIF),
    ("oto_doc", {"op": "some_new_op"}, None, c.ECRITURE),
])
def test_classement(target, args, ann, attendu):
    assert c.classer(target, args, ann) == attendu


def test_un_outil_declare_destructeur_demande_meme_s_il_se_dit_lecteur():
    ann = ToolAnnotations(readOnlyHint=True, destructiveHint=True)
    assert c.classer("x_tool", {}, ann) == c.DESTRUCTIF


@pytest.mark.parametrize("nom", [
    "oto_admin_org", "oto_clear_org", "oto_use_org", "oto_shell",
    "oto_enable_tool", "oto_disable_tool", "run_start", "run_finish", "oto_call",
])
def test_interdits(nom):
    assert c.interdit(nom)


def test_permis():
    for nom in ("oto_search", "data_write", "oto_doc", "fr_search"):
        assert not c.interdit(nom)


def test_cible_deballe_oto_call():
    assert c.cible("oto_call", {"name": "fr_get", "arguments": {"siren": "1"}}) == \
        ("fr_get", {"siren": "1"})
    assert c.cible("oto_call", {"name": "fr_get", "arguments": "nope"}) == ("fr_get", {})
    assert c.cible("data_rows", {"datastore": 2}) == ("data_rows", {"datastore": 2})


def test_epingler_force_les_axes_du_serveur_partout():
    planted = {"q": "x", "_org": 999, "_group": 7, "_project": 5, "_run_id": "autre",
               "_account": "boite-2",
               "arguments": {"_org": 999, "_group": 7, "_run_id": "autre", "k": 1}}
    out = c.epingler(planted, org=1, projet=None, run_id="run-a")
    assert out["_org"] == 1 and out["_run_id"] == "run-a"
    assert "_group" not in out and "_project" not in out
    # The model's own choice of account survives: it shows on the card.
    assert out["_account"] == "boite-2"
    inner = out["arguments"]
    assert inner == {"k": 1, "_org": 1, "_run_id": "run-a"}


def test_epingler_pose_le_projet_de_la_page():
    out = c.epingler({}, org=4, projet=12, run_id=None)
    assert out == {"_org": 4, "_project": 12}


def test_sorte_retenue_par_outil_et_op():
    assert c.cle_de_sorte("oto_doc", {"op": "create"}) == "oto_doc:create"
    assert c.cle_de_sorte("oto_call", {"name": "data_write", "arguments": {}}) == "data_write:"


def test_rouge_seulement_pour_une_suppression():
    assert c.rouge("data_delete_row", {})
    assert c.rouge("oto_doc", {"op": "archive"})
    assert not c.rouge("oto_trigger", {"op": "run_now"})


def test_le_signal_de_manque_est_a_portee_de_main():
    # Seen directly, not through `oto_call`: the prompt asks for it by name.
    assert "feedback" in c.CORE_TOOLS
    assert not c.interdit("feedback")
