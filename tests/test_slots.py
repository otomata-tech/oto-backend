"""Slots de procédure (ADR 0035, B1) — déclaration + convention <slot:name> + checks.

Couvre : validation de la déclaration (structure dure, messages actionnables),
le parsing des refs de prose, la vérification croisée non bloquante (`slots_check`),
et le câblage dans la capacité d'écriture (`oto_set_doctrine` : slots validés,
transmis au store, restaurés par from_version, check dans la réponse).
B1 = canari no-op : AUCUN test de résolution runtime (B3).
"""
import asyncio

import pytest

from oto_mcp import slots as slots_mod
from oto_mcp.capabilities import orgs_instructions as oi
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


# ── validate_slots : structure dure ──────────────────────────────────────────
def test_validate_ok_normalizes():
    out = slots_mod.validate_slots([
        {"name": "Sortie", "type": "tableau", "description": "leads enrichis"},
        {"name": "crm", "type": "connecteur"},
        {"name": "kb", "type": "doc"},
    ])
    assert out[0] == {"name": "sortie", "type": "tableau", "description": "leads enrichis"}
    # type=connecteur sans champ `connector` → le nom du slot désigne le connecteur
    assert out[1] == {"name": "crm", "type": "connecteur", "connector": "crm"}
    assert out[2] == {"name": "kb", "type": "doc"}


def test_validate_explicit_connector():
    out = slots_mod.validate_slots([{"name": "crm", "type": "connecteur", "connector": "folk"}])
    assert out[0]["connector"] == "folk"


def test_validate_none_and_empty():
    assert slots_mod.validate_slots(None) == []
    assert slots_mod.validate_slots([]) == []


@pytest.mark.parametrize("raw,fragment", [
    ({"name": "x", "type": "tableau"}, "liste"),                       # pas une liste
    ([{"type": "tableau"}], "name"),                                   # nom manquant
    ([{"name": "Bad Name", "type": "tableau"}], "name"),               # espace interdit
    ([{"name": "a", "type": "feuille"}], "type"),                      # type inconnu
    ([{"name": "a", "type": "tableau"}, {"name": "a", "type": "doc"}], "dupliqué"),
    ([{"name": "a", "type": "tableau", "connector": "folk"}], "connecteur"),  # connector hors type
    ([{"name": "a", "type": "tableau", "foo": 1}], "inconnus"),        # champ inconnu
])
def test_validate_rejects(raw, fragment):
    with pytest.raises(ValueError) as e:
        slots_mod.validate_slots(raw)
    assert fragment in str(e.value)


# ── slot_refs : convention <slot:name> dans la prose ─────────────────────────
def test_slot_refs_dedup_order():
    body = "Écrire dans <slot:sortie> puis relire <slot:kb> et encore <slot:sortie>."
    assert slots_mod.slot_refs(body) == ["sortie", "kb"]
    assert slots_mod.slot_refs("") == []
    assert slots_mod.slot_refs("<slot:Bad Name>") == []   # le marqueur est strict


# ── slots_check : vérification croisée non bloquante ─────────────────────────
def test_check_unresolved_and_unreferenced():
    body = "Écrire dans <slot:sortie> et <slot:fantome>."
    declared = [{"name": "sortie", "type": "tableau"}, {"name": "jamais", "type": "doc"}]
    r = slots_mod.slots_check(body, declared)
    assert r["unresolved_slots"] == ["fantome"]
    assert r["unreferenced_slots"] == ["jamais"]
    assert r["slots"] == declared


def test_check_connector_coherence(monkeypatch):
    # Registre minimal : `folk` existe et porte le namespace `folk` ; identités OK.
    class _Con:
        name = "folk"
    monkeypatch.setattr(slots_mod.providers, "REGISTRY", {"folk": _Con()})
    monkeypatch.setattr(slots_mod.providers, "connector_for_namespace",
                        lambda ns: _Con() if ns == "folk" else None)
    from oto_mcp import connector_identities
    monkeypatch.setattr(connector_identities, "_LISTERS", {"folk": lambda sub: []})

    # Connecteur déclaré + tool référencé → aucune alarme.
    body = "Créer via <tool:folk_create_person> vers <slot:crm>."
    r = slots_mod.slots_check(body, [{"name": "crm", "type": "connecteur", "connector": "folk"}])
    assert r["slot_warnings"] == [] and r["suggested_slots"] == []

    # Connecteur inconnu du registre → warning doux.
    r = slots_mod.slots_check(body, [{"name": "crm", "type": "connecteur", "connector": "nexiste"}])
    assert any("inconnu du registre" in w for w in r["slot_warnings"])

    # Connecteur déclaré mais aucun tool référencé → warning doux.
    r = slots_mod.slots_check("Prose sans refs <slot:crm>.",
                              [{"name": "crm", "type": "connecteur", "connector": "folk"}])
    assert any("aucun tool" in w for w in r["slot_warnings"])

    # L'inverse : tools d'un connecteur À IDENTITÉS sans slot déclaré → suggestion.
    r = slots_mod.slots_check("Créer via <tool:folk_create_person>.", [])
    assert [s["connector"] for s in r["suggested_slots"]] == ["folk"]


def test_check_never_raises(monkeypatch):
    # Le check de cohérence est best-effort : un registre cassé ne bloque pas l'écriture.
    monkeypatch.setattr(slots_mod, "_referenced_connectors",
                        lambda body: (_ for _ in ()).throw(RuntimeError("boom")))
    r = slots_mod.slots_check("<slot:x>", [])
    assert r["unresolved_slots"] == ["x"]


# ── Câblage capacité (oto_set_doctrine) ──────────────────────────────────────
def _wire_set(monkeypatch, existing=None):
    calls = {}

    def _set(org_id, slug, body_md, title=None, description=None, set_by=None, slots=None):
        calls["slots"] = slots
        return 2

    monkeypatch.setattr(oi.org_store, "set_instruction", _set)
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda org, slug, version=None: existing)

    async def _wc(body_md, mcp_instance=None):
        return {"referenced_tools": [], "unresolved_tools": []}
    monkeypatch.setattr(oi.tool_registry, "write_check", _wc)
    return calls


def test_set_passes_validated_slots(monkeypatch):
    calls = _wire_set(monkeypatch)
    out = asyncio.run(oi._set_instruction(
        ResolvedCtx(sub="u1", org_id=3),
        oi.InstrSetInput(slug="proc", body_md="Écrire dans <slot:sortie>.",
                         slots=[{"name": "Sortie", "type": "tableau"}])))
    assert calls["slots"] == [{"name": "sortie", "type": "tableau"}]
    assert out["unresolved_slots"] == [] and out["unreferenced_slots"] == []


def test_set_invalid_slots_actionable_400(monkeypatch):
    _wire_set(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(oi._set_instruction(
            ResolvedCtx(sub="u1", org_id=3),
            oi.InstrSetInput(slug="proc", body_md="x",
                             slots=[{"name": "a", "type": "feuille"}])))
    assert e.value.code == "invalid_slots"


def test_set_slots_none_preserves_and_checks_effective(monkeypatch):
    # slots omis = conservés → le check croisé relit la row effective.
    calls = _wire_set(monkeypatch,
                      existing={"slug": "proc", "title": "", "description": "", "version": 1,
                                "body_md": "x", "slots": [{"name": "sortie", "type": "tableau"}]})
    out = asyncio.run(oi._set_instruction(
        ResolvedCtx(sub="u1", org_id=3),
        oi.InstrSetInput(slug="proc", body_md="Sans référence.")))
    assert calls["slots"] is None                       # préservation au store
    assert out["unreferenced_slots"] == ["sortie"]      # check sur les slots effectifs


def test_from_version_restores_slots(monkeypatch):
    old = {"slug": "proc", "title": "T", "description": "d", "version": 1,
           "body_md": "Ancien <slot:sortie>.", "slots": [{"name": "sortie", "type": "tableau"}]}
    calls = _wire_set(monkeypatch, existing=old)
    out = asyncio.run(oi._set_instruction(
        ResolvedCtx(sub="u1", org_id=3),
        oi.InstrSetInput(slug="proc", from_version=1)))
    assert calls["slots"] == [{"name": "sortie", "type": "tableau"}]
    assert out["reverted_from"] == 1


def test_set_input_models_accept_slots():
    assert oi.InstrSetInput(slots=[{"name": "a", "type": "doc"}]).slots
    assert oi.AdminInstrSetInput(org_id=1, slots=None).slots is None


# ── B2 : binding nommé au link (oto_project op=link, slot = vocabulaire du projet) ──
def test_normalize_name():
    assert slots_mod.normalize_name("  Sortie ") == "sortie"
    with pytest.raises(ValueError):
        slots_mod.normalize_name("Bad Name")
    with pytest.raises(ValueError):
        slots_mod.normalize_name("")


def _wire_link(monkeypatch, add=None):
    from oto_mcp.capabilities import projects as P
    row = {"id": 7, "owner_type": "org", "owner_id": "3", "name": "Proj", "brief_md": "",
           "created_by": "u1", "archived_at": None, "created_at": "x", "updated_at": "x"}
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(row, id=pid))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: [])
    rec = {}

    def _add(pid, tt, tr, label=None, role=None, config=None, identity_ref=None, slot=None):
        if add:
            add(slot)
        rec["slot"] = slot

    monkeypatch.setattr(P.db, "add_project_link", _add)
    return P, rec


def test_link_binds_normalized_slot(monkeypatch):
    P, rec = _wire_link(monkeypatch)
    out = P._project(ResolvedCtx(sub="u1", org_id=3),
                     P.ProjectInput(op="link", project_id=7, target_type="tableau",
                                    target_ref="9", slot=" Sortie "))
    assert rec["slot"] == "sortie" and out["ok"] is True


def test_link_invalid_slot_400(monkeypatch):
    P, _ = _wire_link(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        P._project(ResolvedCtx(sub="u1", org_id=3),
                   P.ProjectInput(op="link", project_id=7, target_type="tableau",
                                  target_ref="9", slot="Bad Name"))
    assert e.value.code == "invalid_slot"


def test_link_slot_taken_409(monkeypatch):
    def _boom(slot):
        raise ValueError(f"slot_taken: le slot `{slot}` est déjà bindé…")
    P, _ = _wire_link(monkeypatch, add=_boom)
    with pytest.raises(AuthzDenied) as e:
        P._project(ResolvedCtx(sub="u1", org_id=3),
                   P.ProjectInput(op="link", project_id=7, target_type="tableau",
                                  target_ref="9", slot="sortie"))
    assert e.value.code == "slot_taken" and e.value.status == 409


# ── B3 : résolveur runtime `slot:<name>` (enforcement serveur, jamais de fallback) ──
from mcp.shared.exceptions import McpError  # noqa: E402

from oto_mcp import access  # noqa: E402
from oto_mcp.tools import datastore as ds  # noqa: E402

_LINKS = [
    {"target_type": "tableau", "target_ref": "9", "slot": "sortie", "namespace": "leads_q3"},
    {"target_type": "tableau", "target_ref": "12", "slot": "source", "namespace": "pool_pme"},
    {"target_type": "connecteur", "target_ref": "folk", "slot": None},
]


def _wire_resolve(monkeypatch, project=7, links=_LINKS):
    monkeypatch.setattr(access, "current_project", lambda: project)
    monkeypatch.setattr(access.db, "list_project_links", lambda pid: list(links))


def test_resolve_slot_ok(monkeypatch):
    _wire_resolve(monkeypatch)
    assert access.resolve_slot_tableau("sortie") == "leads_q3"
    assert access.resolve_slot_tableau(" Sortie ") == "leads_q3"   # normalisé


def test_resolve_slot_no_project_actionable(monkeypatch):
    _wire_resolve(monkeypatch, project=None)
    with pytest.raises(McpError) as e:
        access.resolve_slot_tableau("sortie")
    assert "project=<id>" in str(e.value)   # la marche à suivre, pas un refus sec


def test_resolve_slot_unbound_lists_bound(monkeypatch):
    _wire_resolve(monkeypatch)
    with pytest.raises(McpError) as e:
        access.resolve_slot_tableau("fantome")
    msg = str(e.value)
    assert "sortie" in msg and "source" in msg and "op=link" in msg


def test_resolve_slot_dangling(monkeypatch):
    _wire_resolve(monkeypatch, links=[{"target_type": "tableau", "target_ref": "99",
                                       "slot": "sortie"}])   # pas de namespace résolu
    with pytest.raises(McpError) as e:
        access.resolve_slot_tableau("sortie")
    assert "ne résout plus" in str(e.value)


def test_resolve_slot_invalid_name(monkeypatch):
    _wire_resolve(monkeypatch)
    with pytest.raises(McpError):
        access.resolve_slot_tableau("Bad Name")


def test_ns_helper_passthrough_and_resolution(monkeypatch):
    _wire_resolve(monkeypatch)
    assert ds._ns("timetrack") == "timetrack"          # nom nu : zéro magie
    assert ds._ns("slot:sortie") == "leads_q3"
    assert ds._ns("  SLOT:sortie ") == "leads_q3"      # préfixe insensible à la casse
    with pytest.raises(McpError):
        ds._ns("slot:fantome")


# ── B5 : liens vérifiés comme des refs (audit + complétude + suggestion) ────
def test_unbound_slots_for():
    from oto_mcp import project_audit
    instr = {"slug": "p", "slots": [{"name": "sortie", "type": "tableau"},
                                    {"name": "crm", "type": "connecteur", "connector": "folk"}]}
    links = [{"target_type": "tableau", "slot": "sortie"}]
    assert project_audit.unbound_slots_for(instr, links) == ["crm"]
    assert project_audit.unbound_slots_for({"slots": []}, links) == []


def test_audit_project(monkeypatch):
    from oto_mcp import project_audit
    import oto_mcp.org_store as org_store_mod
    import oto_mcp.providers as providers_mod
    import oto_mcp.db as db_mod

    links = [
        {"target_type": "tableau", "target_ref": "9", "slot": "sortie", "namespace": "leads"},
        {"target_type": "tableau", "target_ref": "99", "slot": None},              # mort
        {"target_type": "procedure", "target_ref": "42"},                          # ok, slots bindés sauf crm
        {"target_type": "procedure", "target_ref": "43"},                          # morte
        {"target_type": "connecteur", "target_ref": "nexiste"},                    # mort
    ]
    monkeypatch.setattr(org_store_mod, "get_instruction_by_id",
                        lambda iid: ({"slug": "prospection",
                                      "slots": [{"name": "sortie", "type": "tableau"},
                                                {"name": "crm", "type": "connecteur"}]}
                                     if iid == 42 else None))
    monkeypatch.setattr(providers_mod, "REGISTRY", {"folk": object()})
    monkeypatch.setattr(db_mod, "project_run_stats",
                        lambda pid: {"runs": 3, "doctrines": ["autre-doctrine"]})

    out = project_audit.audit_project(7, links)
    assert {(d["target_type"], str(d["target_ref"])) for d in out["dead_links"]} == {
        ("tableau", "99"), ("procedure", "43"), ("connecteur", "nexiste")}
    assert out["unbound_slots"] == [{"procedure": "prospection", "ref": "42", "slots": ["crm"]}]
    assert out["inert_procedures"] == ["prospection"]   # 3 runs, jamais déroulée

    # Projet sans run : rien d'inerte (jeune projet = bruit, pas signal).
    monkeypatch.setattr(db_mod, "project_run_stats", lambda pid: {"runs": 0, "doctrines": []})
    assert project_audit.audit_project(7, links)["inert_procedures"] == []


def test_link_procedure_unbound_warning(monkeypatch):
    P, _ = _wire_link(monkeypatch)
    import oto_mcp.org_store as org_store_mod
    monkeypatch.setattr(P, "_procedure_ref_to_id", lambda org, ref: "42")
    monkeypatch.setattr(org_store_mod, "get_instruction_by_id",
                        lambda iid: {"slug": "prospection",
                                     "slots": [{"name": "sortie", "type": "tableau"}]})
    out = P._project(ResolvedCtx(sub="u1", org_id=3),
                     P.ProjectInput(op="link", project_id=7, target_type="procedure",
                                    target_ref="prospection"))
    assert out["unbound_slots"] == ["sortie"]
    assert "slot='<name>'" in out["warning"] or "sortie" in out["warning"]


def test_project_hint_suggests_link(monkeypatch):
    monkeypatch.setattr(ds.access, "current_project", lambda: 7)
    monkeypatch.setattr(ds.db, "list_project_links",
                        lambda pid: [{"target_type": "tableau", "namespace": "leads"}])
    assert ds._project_hint("leads") is None                    # lié → pas de bruit
    hint = ds._project_hint("orphelin")
    assert hint and "op=link" in hint and "#7" in hint
    monkeypatch.setattr(ds.access, "current_project", lambda: None)
    assert ds._project_hint("orphelin") is None                 # hors projet → silence


# ── B4 : inventaire dérivé (oto_project op=inventory) ───────────────────────
def test_inventory_derives_union(monkeypatch):
    from oto_mcp.capabilities import projects as P

    row = {"id": 7, "owner_type": "org", "owner_id": "3", "name": "Proj", "brief_md": "",
           "created_by": "u1", "archived_at": None, "created_at": "x", "updated_at": "x"}
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(row, id=pid))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: [
        {"target_type": "procedure", "target_ref": "42"},
        {"target_type": "procedure", "target_ref": "morte"},        # slug legacy → non résolue
        {"target_type": "connecteur", "target_ref": "unipile"},
        {"target_type": "tableau", "target_ref": "9", "slot": "sortie", "namespace": "leads_q3"},
    ])
    monkeypatch.setattr(P.db, "project_run_tools",
                        lambda pid: ["fr_search", "oto_use_project", "folk_create_person"])
    monkeypatch.setattr(P.db, "project_run_stats", lambda pid: {"runs": 0, "doctrines": []})

    import oto_mcp.org_store as org_store_mod
    monkeypatch.setattr(org_store_mod, "get_instruction_by_id",
                        lambda iid: {"slug": "prospection", "body_md":
                                     "Chercher via <tool:fr_search> puis <tool:folk_create_person> "
                                     "vers <slot:sortie>.",
                                     "slots": [{"name": "crm", "type": "connecteur", "connector": "folk"},
                                               {"name": "sortie", "type": "tableau"}]})

    class _Con:
        def __init__(self, name):
            self.name = name
    import oto_mcp.providers as providers_mod
    monkeypatch.setattr(providers_mod, "connector_for_namespace",
                        lambda ns: {"fr": _Con("sirene"), "folk": _Con("folk")}.get(ns))

    out = P._project(ResolvedCtx(sub="u1", org_id=3), P.ProjectInput(op="inventory", project_id=7))
    # Union : refs des procédures d'abord, puis runs ; oto_use_project (spine) écarté.
    assert out["tools"] == ["fr_search", "folk_create_person"]
    # Connecteurs : slots connecteur ∪ liens ∪ dérivés des tools.
    assert out["connectors"] == ["folk", "sirene", "unipile"]
    procs = out["sources"]["procedures"]
    assert {p["ref"]: p["resolved"] for p in procs} == {"42": True, "morte": False}
    assert out["sources"]["tableaux"] == [{"slot": "sortie", "namespace": "leads_q3", "ref": "9"}]


# ── schéma CIBLE d'un slot tableau (ADR 0035 × 0046) ─────────────────────────

_TARGET = {"strict": True, "key": "fact_id",
           "fields": [{"key": "fact_id", "type": "text", "required": True},
                      {"key": "status", "role": "status",
                       "lifecycle": {"states": ["nouveau", "qualified"],
                                     "transitions": {"nouveau": ["qualified"]}}}]}


def test_slot_schema_accepted_on_tableau():
    out = slots_mod.validate_slots([{"name": "leads", "type": "tableau", "schema": _TARGET}])
    assert out[0]["schema"] == _TARGET


def test_slot_schema_rejected_on_non_tableau():
    with pytest.raises(ValueError, match="réservé au type `tableau`"):
        slots_mod.validate_slots([{"name": "crm", "type": "connecteur", "schema": _TARGET}])


def test_slot_schema_definition_validated():
    with pytest.raises(ValueError, match="type inconnu"):
        slots_mod.validate_slots([{"name": "leads", "type": "tableau",
                               "schema": {"fields": [{"key": "x", "type": "wat"}]}}])


def test_target_schema_for_resolves_from_linked_procedures(monkeypatch):
    links = [{"target_type": "tableau", "target_ref": "125", "slot": "leads"},
             {"target_type": "procedure", "target_ref": "111"}]
    monkeypatch.setattr("oto_mcp.org_store.get_instruction_by_id",
                        lambda i: {"slug": "qualifier-fact-pv",
                                   "slots": [{"name": "leads", "type": "tableau",
                                              "schema": _TARGET}]})
    assert slots_mod.target_schema_for("leads", links) == _TARGET
    assert slots_mod.target_schema_for("autre", links) is None
    assert slots_mod.target_schema_for("leads", [{"target_type": "tableau",
                                              "target_ref": "125"}]) is None


def test_provision_on_virgin_namespace(monkeypatch):
    import oto_mcp.slots as S
    calls = {"set": [], "index": []}
    monkeypatch.setattr("oto_mcp.db.get_datastore_namespace_by_id",
                        lambda i: {"id": i, "namespace": "leads-pv", "schema": None})
    monkeypatch.setattr("oto_mcp.db.datastore_key_dup_groups", lambda i, k: [])
    monkeypatch.setattr("oto_mcp.db.set_datastore_schema",
                        lambda i, sc: calls["set"].append((i, sc)))
    monkeypatch.setattr("oto_mcp.db.datastore_ensure_key_index",
                        lambda i, k: calls["index"].append((i, k)))
    res = S.provision_tableau_schema(125, _TARGET)
    assert res == {"status": "provisioned"}
    assert calls["set"] == [(125, _TARGET)] and calls["index"] == [(125, "fact_id")]


def test_provision_conform_and_mismatch(monkeypatch):
    import oto_mcp.slots as S
    monkeypatch.setattr("oto_mcp.db.get_datastore_namespace_by_id",
                        lambda i: {"id": i, "namespace": "leads-pv", "schema": _TARGET})
    assert S.provision_tableau_schema(125, _TARGET)["status"] == "conform"
    other = {"fields": [{"key": "autre"}]}
    res = S.provision_tableau_schema(125, other)
    assert res["status"] == "mismatch" and "DIFFÉRENT" in res["warning"]


def test_provision_refuses_key_on_dirty_data(monkeypatch):
    import oto_mcp.slots as S
    wrote = []
    monkeypatch.setattr("oto_mcp.db.get_datastore_namespace_by_id",
                        lambda i: {"id": i, "namespace": "leads-pv", "schema": None})
    monkeypatch.setattr("oto_mcp.db.datastore_key_dup_groups",
                        lambda i, k: [{"value": "f1", "n": 2}])
    monkeypatch.setattr("oto_mcp.db.set_datastore_schema",
                        lambda i, sc: wrote.append(sc))
    res = S.provision_tableau_schema(125, _TARGET)
    assert res["status"] == "dirty_key" and wrote == []
