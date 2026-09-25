"""Dispatch `op=` de la surface Google Tasks consolidée (ADR 0047 §Amendement,
appliqué au produit tasks : 6 tools → 2).

Le module n'avait AUCUN test, et c'est le premier consolidé qui ÉCRIT sur les données
personnelles de l'utilisateur : `upsert` crée/modifie, `set_status` modifie, **`rm`
supprime**. Avant, chaque verbe était un tool distinct — le mauvais nom ne pouvait pas
être appelé par accident. Depuis, une op mal câblée appelle silencieusement une AUTRE
méthode du client, et rien ne casse au boot : un `op="get"` qui atteindrait
`delete_task` est un incident, pas un bug d'affichage.

D'où, pour chaque op : la méthode client appelée **et** l'absence d'appel aux méthodes
voisines dangereuses (`delete_task`/`update_task`/`create_task`/`complete_task`), le
refus explicite d'une op inconnue (message qui NOMME les ops valides), les arguments
obligatoires manquants, et le fait qu'un appel SANS `op` liste (lecture) — jamais une
écriture atteignable par défaut.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError
# Méthodes d'écriture/suppression du TasksClient : sur CHAQUE op, celles qui ne sont
# pas la cible attendue doivent rester muettes.
_DANGEROUS = ("create_task", "update_task", "complete_task", "delete_task",
              "create_tasklist")


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import tasks as T

    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _call(name: str, **kwargs):
    """Les tools tasks_* sont `async` (le client Google est sync, poussé en threadpool
    par `asyncio.to_thread`) → on déroule la coroutine ici."""
    return asyncio.run(_tool(name)(**kwargs))


@pytest.fixture
def client(monkeypatch):
    """Faux TasksClient + identité résolue.

    On patche la CLASSE (import local dans `_client_for_user`) plutôt que le helper :
    le plomberie `sub` → credentials → `account` reste ainsi exercée."""
    import oto.tools.google.tasks.lib.tasks_client as tc

    inst = MagicMock()
    inst.list_tasks.return_value = [{"id": "t1"}]
    inst.list_tasklists.return_value = [{"id": "@default", "title": "Mes tâches"}]
    factory = MagicMock(return_value=inst)
    monkeypatch.setattr(tc, "TasksClient", factory)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr("oto_mcp.auth.google.credentials_for",
                        lambda sub, account=None, service=None: MagicMock(name=f"creds:{account}"))
    inst._factory = factory
    return inst


def _assert_only(client, method: str):
    """La méthode attendue a été appelée, et AUCUNE autre méthode dangereuse."""
    getattr(client, method).assert_called_once()
    for other in _DANGEROUS:
        if other != method:
            getattr(client, other).assert_not_called()


# --- surface -------------------------------------------------------------------

def test_the_product_exposes_exactly_two_tools():
    """6 → 2. La liste est le contrat : un verbe re-sorti en tool nommé doit être un
    choix, pas un accident."""
    from fastmcp import FastMCP
    from oto_mcp.tools import tasks as T

    m = FastMCP("t")
    T.register(m)
    names = {t.name for t in asyncio.run(m._list_tools())}
    assert names == {"tasks_lists", "tasks_task"}


# --- lectures -------------------------------------------------------------------

def test_list_is_the_default_op_and_never_writes(client):
    """Un appel SANS `op` doit LIRE. C'est l'invariant qui protège des données
    personnelles : aucune écriture/suppression atteignable par défaut."""
    out = _call("tasks_task")
    assert out == {"tasks": [{"id": "t1"}], "count": 1}
    _assert_only(client, "list_tasks")


def test_list_passes_its_filters(client):
    _call("tasks_task", op="list", tasklist="L1", completed=True, max_results=7)
    assert client.list_tasks.call_args.args == ("L1", True, 7)


def test_get_reads_one_task(client):
    _call("tasks_task", op="get", task_id="t1", tasklist="L1")
    _assert_only(client, "get_task")
    assert client.get_task.call_args.args == ("t1", "L1")


# --- écritures : création / mise à jour ------------------------------------------

def test_upsert_without_task_id_creates(client):
    _call("tasks_task", op="upsert", title="Rappeler Marie", notes="perso",
          due="2026-08-20", tasklist="L1", parent="p1")
    _assert_only(client, "create_task")
    assert client.create_task.call_args.args == (
        "Rappeler Marie", "perso", "2026-08-20T00:00:00.000Z", "L1", "p1")


def test_upsert_with_task_id_updates_and_never_creates(client):
    """Le pivot create↔update est `task_id` : s'il est fourni, la création doit rester
    muette (sinon un doublon apparaît à chaque « modification »)."""
    _call("tasks_task", op="upsert", task_id="t1", title="Nouveau titre")
    _assert_only(client, "update_task")
    assert client.update_task.call_args.args == ("t1", "@default", "Nouveau titre",
                                                 None, None)


def test_upsert_expands_a_plain_date_to_rfc_3339(client):
    """L'API Tasks veut du RFC 3339 ; une date nue est acceptée côté surface et
    normalisée ici (create ET update)."""
    _call("tasks_task", op="upsert", task_id="t1", due="2026-12-01")
    assert client.update_task.call_args.args[4] == "2026-12-01T00:00:00.000Z"
    client.reset_mock()
    _call("tasks_task", op="upsert", title="x", due="2026-12-01T09:30:00Z")
    assert client.create_task.call_args.args[2] == "2026-12-01T09:30:00Z"  # déjà RFC


def test_upsert_refuses_an_update_with_nothing_to_change(client):
    with pytest.raises(McpError, match="title, notes ou due"):
        _call("tasks_task", op="upsert", task_id="t1")
    client.update_task.assert_not_called()


def test_upsert_refuses_a_creation_without_title(client):
    with pytest.raises(McpError, match="title"):
        _call("tasks_task", op="upsert", notes="orphelin")
    client.create_task.assert_not_called()


# --- écritures : statut ----------------------------------------------------------

def test_set_status_completes_by_default(client):
    _call("tasks_task", op="set_status", task_id="t1", tasklist="L1")
    _assert_only(client, "complete_task")
    assert client.complete_task.call_args.args == ("t1", "L1", True)


def test_set_status_reopens_with_done_false(client):
    _call("tasks_task", op="set_status", task_id="t1", done=False)
    assert client.complete_task.call_args.args == ("t1", "@default", False)


def test_set_status_requires_a_task_id(client):
    with pytest.raises(McpError, match="task_id"):
        _call("tasks_task", op="set_status")
    client.complete_task.assert_not_called()


# --- suppression -----------------------------------------------------------------

def test_rm_deletes_that_task_and_nothing_else(client):
    _call("tasks_task", op="rm", task_id="t1", tasklist="L1")
    _assert_only(client, "delete_task")
    assert client.delete_task.call_args.args == ("t1", "L1")


def test_rm_requires_a_task_id(client):
    with pytest.raises(McpError, match="task_id"):
        _call("tasks_task", op="rm")
    client.delete_task.assert_not_called()


def test_rm_refuses_an_empty_task_id(client):
    """Un id vide n'est pas « la tâche par défaut » : refus ici, pas un 404 amont."""
    with pytest.raises(McpError, match="task_id"):
        _call("tasks_task", op="rm", task_id="")
    client.delete_task.assert_not_called()


def test_get_requires_a_task_id(client):
    with pytest.raises(McpError, match="task_id"):
        _call("tasks_task", op="get")
    client.get_task.assert_not_called()


# --- refus d'une op inconnue -----------------------------------------------------

def test_unknown_op_is_refused_with_the_allowed_list(client):
    """Jamais de repli silencieux sur le défaut : l'agent croirait sa demande honorée.
    Et le refus tombe AVANT d'appeler quoi que ce soit."""
    with pytest.raises(McpError, match="op doit être"):
        _call("tasks_task", op="delete", task_id="t1")
    for m in _DANGEROUS:
        getattr(client, m).assert_not_called()
    client._factory.assert_not_called()      # même pas de credential résolu


@pytest.mark.parametrize("op", ["list", "get", "upsert", "set_status", "rm"])
def test_the_refusal_message_names_every_valid_op(client, op):
    with pytest.raises(McpError) as e:
        _call("tasks_task", op="nope")
    assert f"'{op}'" in e.value.error.message


# --- les listes de tâches (tool resté seul) --------------------------------------

def test_tasklists_lists_by_default(client):
    out = _call("tasks_lists")
    assert out == {"tasklists": [{"id": "@default", "title": "Mes tâches"}], "count": 1}
    _assert_only(client, "list_tasklists")


def test_tasklists_creates_only_when_a_title_is_given(client):
    """`create` est la seule ÉCRITURE de ce tool : sans lui, rien ne doit être créé."""
    _call("tasks_lists", create="Projet X")
    _assert_only(client, "create_tasklist")
    assert client.create_tasklist.call_args.args == ("Projet X",)


# --- multi-compte -----------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs", [
    ("tasks_task", {"op": "list"}),
    ("tasks_lists", {}),
])
def test_account_selects_the_google_account(monkeypatch, client, tool, kwargs):
    """`account` = l'email du compte Google ciblé (multi-compte, ADR 0033) — il doit
    atteindre la résolution de credential, pas être avalé."""
    seen = {}
    monkeypatch.setattr("oto_mcp.auth.google.credentials_for",
                        lambda sub, account=None, service=None: seen.update(sub=sub, account=account))
    _call(tool, account="alexis@otomata.tech", **kwargs)
    assert seen == {"sub": "sub-1", "account": "alexis@otomata.tech"}


def test_a_missing_google_account_is_an_actionable_error(monkeypatch, client):
    """`google_oauth` lève une RuntimeError quand aucun compte n'est connecté : elle
    doit ressortir en McpError lisible, pas en 500."""
    def _boom(sub, account=None, service=None):
        raise RuntimeError("Aucun compte Google connecté")
    monkeypatch.setattr("oto_mcp.auth.google.credentials_for", _boom)
    with pytest.raises(McpError, match="Aucun compte Google"):
        _call("tasks_task", op="list")
