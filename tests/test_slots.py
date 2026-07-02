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
        {"name": "kb", "type": "base"},
    ])
    assert out[0] == {"name": "sortie", "type": "tableau", "description": "leads enrichis"}
    # type=connecteur sans champ `connector` → le nom du slot désigne le connecteur
    assert out[1] == {"name": "crm", "type": "connecteur", "connector": "crm"}
    assert out[2] == {"name": "kb", "type": "base"}


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
    ([{"name": "a", "type": "tableau"}, {"name": "a", "type": "base"}], "dupliqué"),
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
    declared = [{"name": "sortie", "type": "tableau"}, {"name": "jamais", "type": "base"}]
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
    assert oi.InstrSetInput(slots=[{"name": "a", "type": "base"}]).slots
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
