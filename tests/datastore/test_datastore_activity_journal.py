"""Journal des gestes datastore : le dashboard laisse enfin une trace lisible.

Trois choses vérifiées, dans l'ordre du besoin vécu (« j'ai cliqué → ecarte sans
savoir, impossible de retrouver LAQUELLE ni d'annuler ») :

1. une transition faite en REST journalise `from_status`/`to_status` — l'état AVANT
   est lu avant l'écriture, sinon il est perdu ;
2. le parcours d'UNE ligne ne filtre plus `kind='mcp'` (le geste de dashboard y était
   invisible) et rend les champs enrichis, `null`/`[]` sur les lignes historiques ;
3. l'activité d'un TABLEAU retrouve le geste REST (matché par `ns_id`) **et** l'appel
   MCP, que l'agent ait nommé le tableau par son NOM ou par son ID.

Seams PG monkeypatchés (convention du repo : le chemin SQL est exercé au deploy).
"""
from __future__ import annotations

import asyncio

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import calllog
from oto_mcp.datastore import journal as datastore_journal
from oto_mcp.capabilities.datastore import activity as dsa
from oto_mcp.capabilities.datastore import rows as dsr
from oto_mcp.db import usage


# --- outillage --------------------------------------------------------------

class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, sink, rows):
        self._sink = sink
        self._rows = rows

    def execute(self, sql, params):
        self._sink["sql"] = sql
        self._sink["params"] = params
        return _FakeCur(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeReq:
    def __init__(self, path_params=None, body=None):
        self.path_params = path_params or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# #317 : la colonne qui nomme une ligne se déclare par sa PRÉSENTATION.
SCHEMA = {"fields": [{"key": "societe", "display": "title"},
                     {"key": "statut", "role": "status"}]}


class _FakeStore:
    """Store minimal : un tableau `leads-clients` (id 160) avec un lifecycle.

    ⚠️ Les mutations remplissent le RELEVÉ `trace` comme le vrai store — c'est le
    seul canal du journal : l'état d'avant vient de la mutation, jamais d'une
    relecture faite par la route (qui courrait avec un write concurrent)."""

    NS_ID = 160
    NAME = "leads-clients"

    def __init__(self):
        self.row = {"_id": "row-1", "societe": "DEXXON GROUPE", "statut": "enrichi"}
        self.written = None

    def _fill(self, trace, prev_status=None):
        if trace is not None:
            trace.update({"ns_id": self.NS_ID, "namespace": self.NAME,
                          "status_key": "statut", "title_key": "societe",
                          "prev_status": prev_status})

    def resolve_ns_id(self, namespace):
        return self.NS_ID

    def get_row(self, namespace, row_id):
        return dict(self.row)

    def update_row(self, namespace, row_id, patch, *, trace=None, readonly_override=False, origine_override=False):
        self._fill(trace, prev_status=self.row.get("statut"))
        self.row = {**self.row, **patch}
        self.written = patch
        return dict(self.row)

    def append_row(self, namespace, data, *, trace=None, readonly_override=False, origine_override=False):
        self._fill(trace)
        return {"_id": "row-2", **data}

    def delete_row(self, namespace, row_id, *, trace=None):
        self._fill(trace, prev_status=self.row.get("statut"))
        return None

    def declared_key(self, namespace):
        return "societe"

    # #658 : la surface REST relit ce relevé pour sa ligne de journal.
    off_forced: list = []

    def off_schema_report(self):
        return {}   # relevé « hors schéma » (#294) : rien de hors format ici


def _wire_journal(monkeypatch) -> list[dict]:
    """Capture les lignes `tool_calls` posées, sans DB ni event loop."""
    written: list[dict] = []
    monkeypatch.setattr(datastore_journal.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"namespace": _FakeStore.NAME, "schema": SCHEMA,
                                       "owner_type": "org", "owner_id": "35"})

    def _insert(row):
        written.append(row)

    monkeypatch.setattr(calllog, "_insert_rest", _insert)
    return written


def _mount(monkeypatch, store):
    """Écrire une ligne est une CAPACITÉ depuis le 2026-08-12 (#302) : le journal se
    vérifie donc sur `capabilities/datastore/rows.py`, par la vraie chaîne REST."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: store)


# --- 1. le geste REST écrit from_status / to_status -------------------------

def test_rest_transition_journals_from_and_to_status(monkeypatch):
    store = _FakeStore()
    written = _wire_journal(monkeypatch)
    _mount(monkeypatch, store)

    status, payload = call("me.datastore.update_row",
                           path_params={"namespace": "160", "row_id": "row-1"},
                           body={"statut": "ecarte"})

    assert status == 200
    assert payload["statut"] == "ecarte"
    assert len(written) == 1
    line = written[0]
    assert line["kind"] == "rest"
    assert line["tool"] == "data_write"        # vocabulaire de la surface MCP
    assert line["sub"] == "u-1"
    args = line["args"]
    assert args["from_status"] == "enrichi"    # lu AVANT l'écriture
    assert args["to_status"] == "ecarte"
    assert args["id"] == "row-1"
    assert args["fields"] == ["statut"]        # vrai tableau JSON, pas une chaîne
    assert args["namespace"] == "leads-clients"  # nom canonique, même appelé par id
    assert args["ns_id"] == 160


def test_rest_delete_journals_previous_status(monkeypatch):
    store = _FakeStore()
    written = _wire_journal(monkeypatch)
    _mount(monkeypatch, store)

    call("me.datastore.delete_row",
         path_params={"namespace": "leads-clients", "row_id": "row-1"})

    assert written[0]["tool"] == "data_delete_row"
    assert written[0]["args"]["from_status"] == "enrichi"
    assert written[0]["args"]["to_status"] is None


def test_from_status_comes_from_the_mutation_not_a_reread(monkeypatch):
    """Le champ qui porte toute la valeur (annuler) doit être l'état sur lequel la
    transition a été VALIDÉE. S'il venait d'une relecture faite avant l'appel, un
    write concurrent glissé entre les deux ferait proposer une annulation vers un
    état que la ligne n'a jamais eu. Ici `get_row` ment : seul le relevé compte."""
    store = _FakeStore()
    store.get_row = lambda ns, rid: {"_id": "row-1", "statut": "en_cours"}  # périmé
    written = _wire_journal(monkeypatch)
    _mount(monkeypatch, store)

    call("me.datastore.update_row",
         path_params={"namespace": "160", "row_id": "row-1"}, body={"statut": "ecarte"})

    assert written[0]["args"]["from_status"] == "enrichi"  # pas "en_cours"


def test_journal_failure_never_breaks_the_write(monkeypatch):
    """Best-effort strict : un journal en panne ne fait pas échouer le geste."""
    store = _FakeStore()
    _wire_journal(monkeypatch)
    monkeypatch.setattr(calllog, "_insert_rest",
                        lambda row: (_ for _ in ()).throw(RuntimeError("pg down")))
    _mount(monkeypatch, store)

    status, _ = call("me.datastore.update_row",
                     path_params={"namespace": "160", "row_id": "row-1"},
                     body={"statut": "ecarte"})
    assert status == 200


def test_namespace_lookup_failure_never_breaks_the_read(monkeypatch):
    """Best-effort de bout en bout (D2) : la résolution du contexte de journal ne
    doit pas pouvoir faire échouer l'appelant — un hoquet du pool PG sur la lecture
    du tableau rendait un 500 sur un geste qui aurait abouti."""
    def _boom(ns_id):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(datastore_journal.db, "get_datastore_namespace_by_id", _boom)
    ctx = datastore_journal.context(_FakeStore(), "160")
    assert ctx.ns_id is None and ctx.name == "160" and ctx.status_key is None


def test_illegal_transition_is_a_400_not_a_500(monkeypatch):
    """Chemin d'échec de l'annulation (D3) : le front calcule un retour légal, le
    schéma bouge entre-temps → le PATCH doit rendre un refus ACTIONNABLE, pas un 500
    (sans quoi l'UI ne peut pas dire « ce retour n'est plus possible »)."""
    from oto_mcp.datastore.core import RowValidationError

    store = _FakeStore()
    _wire_journal(monkeypatch)

    def _refuse(namespace, row_id, patch, *, trace=None,
                readonly_override=False, origine_override=False):
        raise RowValidationError(["statut: transition 'ecarte' → 'enrichi' interdite"])

    store.update_row = _refuse
    _mount(monkeypatch, store)

    status, corps = call("me.datastore.update_row",
                         path_params={"namespace": "160", "row_id": "row-1"},
                         body={"statut": "enrichi"})
    assert (status, corps["error"]) == (400, "row_invalid")


def test_fields_stay_a_bounded_json_array():
    assert calllog._fields_list(["a", "b"]) == ["a", "b"]
    assert calllog._fields_list(None) == []
    assert len(calllog._fields_list([f"c{i}" for i in range(200)])) == calllog.MAX_FIELDS


# --- 2. le parcours d'une ligne voit les deux surfaces ----------------------

def test_row_activity_covers_rest_and_mcp(monkeypatch):
    sink: dict = {}
    rows = [
        {"created_at": "2026-07-28 16:05:09", "kind": "rest", "tool": "data_write",
         "args": {"namespace": "leads-clients", "ns_id": 160, "id": "row-1",
                  "fields": ["statut"], "from_status": "enrichi", "to_status": "ecarte"},
         "ok": True, "error": None, "sub": "u-1", "email": "alexis@otomata.tech",
         "run_id": None, "run_label": None, "doctrine": None, "outcome": None},
        # ligne MCP historique : aucun des champs neufs → null / []
        {"created_at": "2026-07-27 09:00:00", "kind": "mcp", "tool": "data_rows",
         "args": {"namespace": "160"}, "ok": True, "error": None, "sub": "u-1",
         "email": None, "run_id": None, "run_label": None, "doctrine": None,
         "outcome": None},
    ]
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, rows))

    out = usage.datastore_row_activity("row-1", "DEXXON GROUPE",
                                       owner_type="org", owner_id="35")

    assert "l.kind IN ('mcp', 'rest')" in sink["sql"]   # le geste dashboard est visible
    assert out[0]["kind"] == "rest"
    assert out[0]["from_status"] == "enrichi" and out[0]["to_status"] == "ecarte"
    assert out[0]["fields"] == ["statut"] and out[0]["row_id"] == "row-1"
    assert out[1]["kind"] == "mcp"
    assert out[1]["fields"] == [] and out[1]["row_id"] is None
    assert out[1]["from_status"] is None and out[1]["to_status"] is None


def test_row_activity_surface_labels_entries_with_the_title(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(datastore_journal.db, "get_datastore_namespace_by_id",
                        lambda ns_id: {"namespace": _FakeStore.NAME, "schema": SCHEMA})
    monkeypatch.setattr(dsa, "make_store", lambda sub: store)
    monkeypatch.setattr(dsa.db, "datastore_row_activity",
                        lambda row_id, key_value=None, **kw: [{"row_id": "row-1",
                                                               "row_title": None}])
    monkeypatch.setattr(dsa.db, "emails_by_subs", lambda subs: {})

    from oto_mcp.capabilities._types import ResolvedCtx
    out = dsa._row_activity(ResolvedCtx(sub="u-1"),
                            dsa.RowActivityInput(namespace="160", row_id="row-1"))

    assert out["retention_days"] == 30 and out["key"] == "societe"
    assert out["activity"][0]["row_title"] == "DEXXON GROUPE"


# --- 3. l'activité du tableau matche les deux façons de le nommer -----------

def test_namespace_activity_matches_id_and_name(monkeypatch):
    """Le piège : MCP journalise le namespace TEL QUE tapé (nom OU id), REST son
    ns_id résolu. Ne matcher qu'une forme trouerait le journal en silence."""
    sink: dict = {}
    rows = [
        {"created_at": "c1", "kind": "rest", "tool": "data_write",
         "args": {"ns_id": 160, "namespace": "leads-clients", "id": "row-1",
                  "fields": ["statut"], "from_status": "enrichi", "to_status": "ecarte"},
         "ok": True, "error": None, "sub": "u-1", "email": None, "run_id": None,
         "run_label": None, "doctrine": None, "outcome": None},
        {"created_at": "c2", "kind": "mcp", "tool": "data_write",
         "args": {"namespace": "leads-clients"}, "ok": True, "error": None,
         "sub": "u-2", "email": None, "run_id": None, "run_label": None,
         "doctrine": None, "outcome": None},
        {"created_at": "c3", "kind": "mcp", "tool": "data_rows",
         "args": {"namespace": "160"}, "ok": True, "error": None, "sub": "u-2",
         "email": None, "run_id": None, "run_label": None, "doctrine": None,
         "outcome": None},
    ]
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, rows))

    out = usage.datastore_namespace_activity(160, "leads-clients",
                                             owner_type="org", owner_id="35", limit=50)

    assert "l.args->>'ns_id' = %s" in sink["sql"]
    assert "l.args->>'namespace' = ANY(%s)" in sink["sql"]
    # ns_id (rest) puis l'axe NOM : les deux formes tapables par l'agent, BORNÉES à l'org
    assert sink["params"][:3] == ("160", ["160", "leads-clients"], 35)
    assert [e["kind"] for e in out] == ["rest", "mcp", "mcp"]
    assert out[0]["from_status"] == "enrichi" and out[0]["to_status"] == "ecarte"


def test_row_activity_key_axis_is_bounded_to_the_owner(monkeypatch):
    """L'axe « clé métier » est une recherche de SOUS-CHAÎNE dans les args : non borné,
    une clé banale ferait remonter les gestes d'une autre org. L'axe `id` (uuid4, accès
    déjà prouvé) reste nu."""
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, []))

    usage.datastore_row_activity("row-1", "DEXXON", owner_type="org", owner_id="35")
    assert "l.args::text ILIKE %s AND l.org_id = %s" in sink["sql"]

    # propriétaire inconnu ⇒ l'axe flou disparaît, il ne reste que l'id
    usage.datastore_row_activity("row-1", "DEXXON")
    assert "ILIKE" not in sink["sql"]
    assert "l.args->>'id' = %s" in sink["sql"]


def test_namespace_activity_name_axis_is_bounded_to_the_owner(monkeypatch):
    """Non-régression fuite cross-org : un nom de tableau n'est unique QUE par
    propriétaire (`uq_user_datastores_owner_ns`) — deux orgs peuvent avoir `leads`.
    L'axe nom doit donc toujours porter une borne de tenant, jamais être matché nu."""
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, []))

    usage.datastore_namespace_activity(160, "leads", owner_type="org", owner_id="35")
    assert "l.org_id = %s" in sink["sql"]
    assert 35 in sink["params"]

    usage.datastore_namespace_activity(160, "leads", owner_type="user", owner_id="u-1")
    assert "l.sub = %s" in sink["sql"]
    assert "u-1" in sink["params"]

    # Propriétaire inconnu / tableau d'équipe : on SOUS-COUVRE (ns_id seul) plutôt que
    # de sur-matcher un homonyme d'un autre tenant.
    usage.datastore_namespace_activity(160, "leads")
    assert "l.args->>'namespace'" not in sink["sql"]
    assert sink["params"][:1] == ("160",)


def test_namespace_activity_limit_is_server_bounded(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, []))
    usage.datastore_namespace_activity(160, "leads-clients", limit=9999)
    assert sink["params"][-1] == 200
    usage.datastore_namespace_activity(160, "leads-clients", limit=0)
    assert sink["params"][-1] == 1


def test_namespace_activity_titles_resolved_in_one_batch(monkeypatch):
    """Libellés = UNE requête pour toutes les entrées, ligne supprimée ⇒ None."""
    calls: list = []

    def _by_ids(ns_id, row_ids):
        calls.append((ns_id, tuple(row_ids)))
        return {"row-1": {"societe": "DEXXON GROUPE"}}

    monkeypatch.setattr(datastore_journal.db, "datastore_rows_by_ids", _by_ids)
    entries = [{"row_id": "row-1", "row_title": None},
               {"row_id": "row-9", "row_title": None},   # supprimée depuis
               {"row_id": None, "row_title": None}]

    datastore_journal.attach_titles(160, "societe", entries)

    assert len(calls) == 1 and calls[0] == (160, ("row-1", "row-9"))
    assert entries[0]["row_title"] == "DEXXON GROUPE"
    assert entries[1]["row_title"] is None and entries[2]["row_title"] is None


def test_activity_surface_names_the_author(monkeypatch):
    """« Voir l'historique des actions » suppose de voir QUI : `tool_calls.email` est
    NULL en base (le sink ne connaît que le sub) → la surface le résout en UN lot,
    lignes déjà en base comprises."""
    seen: list = []

    def _emails(subs):
        seen.append(sorted(subs))
        return {"u-1": "alexis@otomata.tech"}

    monkeypatch.setattr(dsa.db, "emails_by_subs", _emails)
    entries = [{"sub": "u-1", "email": None}, {"sub": "u-1", "email": None},
               {"sub": "u-9", "email": None}, {"sub": None, "email": None}]

    dsa._attach_emails(entries)

    assert len(seen) == 1 and seen[0] == ["u-1", "u-1", "u-9"]
    assert entries[0]["email"] == entries[1]["email"] == "alexis@otomata.tech"
    assert entries[2]["email"] is None and entries[3]["email"] is None


def test_semantic_rest_lines_stay_out_of_the_route_lens(monkeypatch):
    """`kind='rest'` porte deux natures : la ROUTE (télémétrie de surface) et le
    GESTE métier. La lentille REST admin ne compte que la première — sinon chaque
    mutation du cockpit double-compte et `by_route` liste des pseudo-routes."""
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, []))
    usage.rest_call_stats(7)
    assert "position(' /' in tool) > 0" in sink["sql"]


def test_namespace_activity_is_a_capability_not_a_handwritten_route():
    """ADR 0042 §Convergence : le verbe naît capacité (une autz, une face dérivée)."""
    from oto_mcp.capabilities import registry
    paths = {b.path for c in registry.CAPABILITIES for b in c.rest_bindings()}
    assert "/api/datastore/namespaces/{namespace}/activity" in paths
    assert "/api/datastore/namespaces/{namespace}/rows/{row_id}/activity" in paths


# --- le journal cite l'ENTITÉ, pas la chaîne tapée --------------------------
# Corrélation par `ns_id` résolu sur les DEUX surfaces. Sans ça, la face MCP ne
# laissait que `args.namespace` = ce que l'agent avait tapé, et la lecture devait
# corréler par NOM — un nom n'étant unique que par propriétaire, il fallait le borner
# au tenant pour ne pas fuiter, il change au renommage, et `slot:<name>` échappait.

def test_resolve_notes_the_entity_whatever_the_agent_typed(monkeypatch):
    """`data_write("leads-clients")` et `data_write("160")` visent le même tableau :
    le relevé porte l'id résolu dans les deux cas."""
    from oto_mcp import ownership, session_org
    from oto_mcp.datastore.core import DatastorePg
    from oto_mcp.datastore import core as ds

    monkeypatch.setattr(ds.db, "resolve_datastore_ns",
                        lambda ns, **kw: {"id": 160, "namespace": "leads-clients"})
    monkeypatch.setattr(ownership, "org_can_access", lambda *a, **kw: True)
    st = DatastorePg("u-1", acting_org=35)
    monkeypatch.setattr(st, "_active_scope", lambda: ([35], []))

    for typed in ("leads-clients", "160", "slot:vivier"):
        holder: dict = {}
        tok = session_org.set_call_trace(holder)
        try:
            assert st._resolve(typed) == 160
        finally:
            session_org.reset_call_trace(tok)
        assert holder["ns_id"] == 160, f"tapé {typed!r}"
        assert holder["ns_name"] == "leads-clients"


def test_resolve_leaves_no_trace_when_access_is_refused(monkeypatch):
    """Un tableau hors périmètre ne doit pas laisser d'entité dans le relevé."""
    from oto_mcp import session_org
    from oto_mcp.datastore.core import DatastorePg, NamespaceNotFound
    from oto_mcp.datastore import core as ds

    monkeypatch.setattr(ds.db, "resolve_datastore_ns", lambda ns, **kw: None)
    st = DatastorePg("u-1", acting_org=35)
    monkeypatch.setattr(st, "_active_scope", lambda: ([35], []))
    holder: dict = {}
    tok = session_org.set_call_trace(holder)
    try:
        with pytest.raises(NamespaceNotFound):
            st._resolve("le-tableau-d-une-autre-org")
    finally:
        session_org.reset_call_trace(tok)
    assert holder == {}


def test_resolve_is_inert_outside_an_mcp_call(monkeypatch):
    """Hors appel MCP (REST, stdio, tests), aucun relevé n'est posé : la résolution
    ne doit pas en dépendre."""
    from oto_mcp import ownership, session_org
    from oto_mcp.datastore.core import DatastorePg
    from oto_mcp.datastore import core as ds

    monkeypatch.setattr(ds.db, "resolve_datastore_ns",
                        lambda ns, **kw: {"id": 160, "namespace": "leads-clients"})
    monkeypatch.setattr(ownership, "org_can_access", lambda *a, **kw: True)
    st = DatastorePg("u-1", acting_org=35)
    monkeypatch.setattr(st, "_active_scope", lambda: ([35], []))
    assert session_org.current_call_trace() is None
    assert st._resolve("leads-clients") == 160     # ne lève pas


def test_the_trace_survives_the_threadpool():
    """GARDE-FOU : le relevé est un HOLDER MUTABLE, pas une valeur rebindée. La
    plupart des handlers de tools sont des `def` sync dispatchés en threadpool, où
    `copy_context()` copie les BINDINGS : un `.set()` fait dans le thread ne remonte
    JAMAIS au contexte appelant. Quiconque remplacerait `note_call_trace` par un
    `set()` casserait la corrélation en silence — ce test le rattrape."""
    from starlette.concurrency import run_in_threadpool

    from oto_mcp import session_org

    def handler_sync():                       # le store, DANS le thread
        session_org.note_call_trace(ns_id=160)

    async def call():
        holder: dict = {}
        tok = session_org.set_call_trace(holder)
        try:
            await run_in_threadpool(handler_sync)
            return dict(holder)               # ce que le calllog relira
        finally:
            session_org.reset_call_trace(tok)

    assert asyncio.run(call()) == {"ns_id": 160}


def test_trace_only_yields_a_closed_set_of_keys():
    """Le relevé est un seam de service : il ne doit pas devenir une porte par
    laquelle n'importe quel état de résolution finit journalisé.

    QUATRE clés, quatre raisons nommées : l'entité datastore résolue (`ns_id`) ;
    l'EMPREINTE d'un run — la version de procédure exécutée + l'instance de connecteur
    résolue (chantier du run, lot J2) ; et le FORÇAGE d'une colonne verrouillée (#658,
    02/09/2026), dont c'est la seule trace — décidé comme telle, sans colonne de plus
    sur la ligne. Une clé s'ajoute ICI, à la main, dans le commit qui la provoque."""
    from oto_mcp import server
    assert server._TRACED_ARGS == ("ns_id", "doctrine_version", "instance",
                                   "readonly_forced")


def test_namespace_lens_correlates_on_the_id_without_a_tenant_bound(monkeypatch):
    """L'axe `ns_id` est de confiance (résolu serveur, l'appelant porte déjà le gate) :
    il se matche NU. Seul le repli par nom — l'historique d'avant — est borné au tenant."""
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink, []))

    usage.datastore_namespace_activity(160, "leads-clients",
                                       owner_type="org", owner_id="2", limit=50)

    sql = sink["sql"]
    assert "l.args->>'ns_id' = %s" in sql
    id_axis = [c for c in sql.split(" OR ") if "ns_id" in c][0]
    assert "org_id" not in id_axis                      # l'id ne se borne pas
    assert "l.args->>'namespace' = ANY(%s) AND l.org_id = %s" in sql   # le nom, si
