"""Patcher un schéma par clé au lieu de le remplacer (oto-backend#388, signal).

`data_set_schema` REMPLACE. C'est le bon geste pour poser un format, un piège pour
le retoucher : deux appels indiscernables — même méthode, même succès, même réponse
— n'ont pas le même effet selon que l'appelant a patché en mémoire ou reconstruit la
liste. Mesuré dans une seule journée sur un même tableau : une migration qui patchait
a préservé 78 notes de champ, un remappage qui reconstruisait a détruit un `pattern`
et un `max_length`, et 52 notes ont disparu entre deux sessions par ce mécanisme.

Un avertissement n'aurait pas suffi — personne ne lit un avertissement sur un appel
qui réussit. D'où un geste qui ne PEUT pas détruire, et son pendant obligé : le
retrait explicite, sans quoi on troquerait la destruction accidentelle contre
l'impossibilité de nettoyer (les deux servent à une heure d'intervalle).
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.capabilities.datastore import columns as dcc
from oto_mcp.datastore.core import DatastorePg

from _datastore_rest import call, stub_authz


CURRENT = {
    "unknown_columns": "report",
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text", "label": "SIREN",
         "help": "L'identifiant légal.", "max_length": 9},
        {"key": "contact1_fonction", "type": "text", "label": "Contact 1 — fonction",
         "help": "Jamais tronquée : elle porte le niveau.", "width": "half"},
        {"key": "occupant", "type": "object",
         "fields": [{"key": "nom", "type": "text", "label": "Nom"},
                    {"key": "naf", "type": "text", "label": "NAF"}]},
        {"key": "contacts", "type": "list",
         "of": {"fields": [{"key": "nom", "type": "text", "label": "Nom"},
                           {"key": "tel", "type": "text", "label": "Téléphone"}]}},
    ],
}


# ── fusion pure ──────────────────────────────────────────────────────────────

def test_merge_preserves_what_the_caller_did_not_restate():
    """LE point du signal : 78 notes de champ ne doivent pas tomber parce qu'on
    change un type."""
    fields, added, updated = dsv2.merge_fields(
        CURRENT["fields"], [{"key": "contact1_fonction", "type": "email"}])
    f = next(x for x in fields if x["key"] == "contact1_fonction")
    assert f["type"] == "email"                       # la propriété donnée écrase
    assert f["help"] == "Jamais tronquée : elle porte le niveau."  # le reste survit
    assert f["label"] == "Contact 1 — fonction" and f["width"] == "half"
    assert (added, updated) == ([], ["contact1_fonction"])


def test_unknown_key_is_appended_at_the_end():
    fields, added, _ = dsv2.merge_fields(
        CURRENT["fields"], [{"key": "analyse5", "type": "text"}])
    assert added == ["analyse5"] and fields[-1]["key"] == "analyse5"


def test_existing_order_is_never_reshuffled():
    """L'ordre pilote le rendu : le bouger serait un effet de bord invisible."""
    before = [f["key"] for f in CURRENT["fields"]]
    fields, _, _ = dsv2.merge_fields(
        CURRENT["fields"], [{"key": "siren", "label": "Siren"}])
    assert [f["key"] for f in fields] == before


def test_merge_descends_into_declared_sub_records():
    """Patcher un sous-record ne doit pas détruire ses sous-champs — le même trou,
    un cran plus bas."""
    fields, _, _ = dsv2.merge_fields(CURRENT["fields"], [
        {"key": "occupant", "fields": [{"key": "nom", "max_length": 60}]},
        {"key": "contacts", "of": {"fields": [{"key": "tel", "label": "Tél."}]}},
    ])
    occ = next(f for f in fields if f["key"] == "occupant")
    assert [s["key"] for s in occ["fields"]] == ["nom", "naf"]     # `naf` survit
    assert occ["fields"][0]["max_length"] == 60
    assert occ["fields"][0]["label"] == "Nom"
    contacts = next(f for f in fields if f["key"] == "contacts")
    subs = {s["key"]: s for s in contacts["of"]["fields"]}
    assert set(subs) == {"nom", "tel"} and subs["tel"]["label"] == "Tél."
    assert subs["tel"]["type"] == "text"                            # préservé


def test_the_source_is_never_mutated():
    dsv2.merge_fields(CURRENT["fields"], [{"key": "siren", "type": "number"}])
    assert CURRENT["fields"][0]["type"] == "text"


def test_remove_takes_fields_out_and_reports_unknown_keys():
    kept, unknown = dsv2.remove_fields(CURRENT["fields"], ["occupant"])
    assert [f["key"] for f in kept] == ["siren", "contact1_fonction", "contacts"]
    assert unknown == []
    _, unknown = dsv2.remove_fields(CURRENT["fields"], ["ocupant"])
    assert unknown == ["ocupant"]     # une faute de frappe se DIT


# ── le geste, côté store ─────────────────────────────────────────────────────

@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    posed = {}
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": CURRENT})
    # `set_schema` est réutilisé tel quel : on stubbe ce qu'IL appelle, pas lui —
    # c'est ainsi que le patch hérite de ses gardes et de ses avertissements.
    monkeypatch.setattr(dsm.db, "set_datastore_schema",
                        lambda ns_id, schema: posed.update(schema=schema))
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda ns_id, key: [])
    monkeypatch.setattr(dsm.db, "datastore_ensure_key_index", lambda ns_id, key: None)
    # oto#82 : ce db stubbé ne porte aucun index — la pose reste donc faite.
    monkeypatch.setattr(dsm.db, "datastore_has_key_index", lambda ns_id: False)
    monkeypatch.setattr(dsm.db, "datastore_drop_key_index", lambda ns_id: None)
    monkeypatch.setattr(dsm.db, "datastore_overlong_fields", lambda ns_id, bounds: [])
    monkeypatch.setattr(dsm.db, "datastore_row_keys", lambda ns_id, sample=1000: [])
    # oto-backend#479 : le relevé des lignes en place compte d'abord le tableau — vide ici.
    monkeypatch.setattr(dsm.db, "datastore_count_rows", lambda *a, **k: 0)
    return st, posed


def test_patch_keeps_every_other_field_intact(store):
    st, posed = store
    out = st.patch_schema("v", fields=[{"key": "siren", "label": "Siren"}])
    keys = [f["key"] for f in posed["schema"]["fields"]]
    assert keys == ["siren", "contact1_fonction", "occupant", "contacts"]
    assert posed["schema"]["fields"][0]["help"] == "L'identifiant légal."
    assert posed["schema"]["fields"][0]["max_length"] == 9
    assert out["updated"] == ["siren"] and out["added"] == []


def test_patch_leaves_head_keys_alone_unless_asked(store):
    st, posed = store
    st.patch_schema("v", fields=[{"key": "siren", "label": "Siren"}])
    assert posed["schema"]["unknown_columns"] == "report", \
        "un réglage retiré mais STOCKÉ est reconduit tel quel, jusqu'à son retrait"
    assert posed["schema"]["key"] == "siren"
    assert "new_rows" not in posed["schema"]      # absent avant, absent après
    st.patch_schema("v", new_rows="reject")
    assert posed["schema"]["new_rows"] == "reject"


def test_remove_is_explicit_and_a_typo_touches_nothing(store):
    st, posed = store
    st.patch_schema("v", remove=["occupant"])
    assert [f["key"] for f in posed["schema"]["fields"]] == [
        "siren", "contact1_fonction", "contacts"]
    posed.clear()
    with pytest.raises(ValueError, match="que le schéma ne déclare pas"):
        st.patch_schema("v", remove=["ocupant"])
    assert posed == {}          # rien posé sur une faute de frappe


def test_an_empty_patch_is_refused(store):
    st, posed = store
    with pytest.raises(ValueError, match="rien à patcher.*new_rows"):
        st.patch_schema("v")
    assert posed == {}


def test_patch_inherits_the_schema_guards(store, monkeypatch):
    """Le résultat repasse par `set_schema` : ses refus s'appliquent au patch."""
    st, posed = store
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups",
                        lambda ns_id, key: [{"value": "1", "n": 2}])
    with pytest.raises(ValueError, match="DOUBLON"):
        st.patch_schema("v", key="siren")
    assert posed == {}


# ── la face REST (cockpit — text → select, #388 suite) ──────────────────────

class _RestStore:
    """Un store REEL (patch_schema tel qu'écrit), pour que ce test exerce la
    fusion elle-même — pas une reformulation qui serait d'accord avec elle-même."""

    def __init__(self, current):
        self.current = current
        self.calls: list = []

    def patch_schema(self, datastore, *, fields=None, remove=None, remove_attrs=None,
                     key=None, unknown_columns=None, new_rows=None):
        self.calls.append((datastore, fields, remove, key, unknown_columns,
                           new_rows))
        merged, added, updated = dsv2.merge_fields(
            [f for f in self.current.get("fields") or [] if isinstance(f, dict)],
            fields or [])
        merged, _ = dsv2.remove_field_attrs(merged, remove_attrs or {})
        return {"datastore": datastore, "schema": {**self.current, "fields": merged},
               "added": added, "updated": updated, "removed": list(remove or [])}


@pytest.fixture(autouse=True)
def _sans_db(monkeypatch):
    stub_authz(monkeypatch)


def test_rest_patch_turns_a_text_column_into_a_select(monkeypatch):
    """Le geste que le cockpit doit pouvoir faire : convertir `statut` (text) en
    `enum` par PATCH, sans réécrire tout le schéma (ce que `PUT` exigerait)."""
    store = _RestStore({"fields": [{"key": "statut", "type": "text", "label": "Statut"}]})
    monkeypatch.setattr(dcc, "make_store", lambda sub: store)
    status, corps = call(
        "me.datastore.patch_schema",
        path_params={"datastore": "160"},
        body={"fields": [{"key": "statut", "type": "enum",
                          "options": ["ouvert", "gagné", "perdu"]}]},
    )
    assert status == 200
    assert corps["updated"] == ["statut"]
    f = next(x for x in corps["schema"]["fields"] if x["key"] == "statut")
    assert f["type"] == "enum" and f["options"] == ["ouvert", "gagné", "perdu"]
    # le path param DÉSIGNE le tableau — jamais le corps (voir call() ci-dessus,
    # qui ne poste pas `datastore`) : le champ ne s'y confond pas avec la clé
    # métier `key` de `PatchSchemaInput`, prise ici pour ce qu'elle est.
    assert store.calls[0][0] == "160"


def test_rest_patch_refuses_an_unknown_field_rather_than_dropping_it(monkeypatch):
    """La garde de champ inconnu (#302) couvre aussi cette route neuve — un
    appelant qui se trompe de forme reçoit un 400 nommé, jamais un 200 muet."""
    store = _RestStore({"fields": []})
    monkeypatch.setattr(dcc, "make_store", lambda sub: store)
    status, corps = call(
        "me.datastore.patch_schema",
        path_params={"datastore": "160"},
        body={"type": "enum"},  # forme fautive : `type` n'est pas un champ d'Input
    )
    assert status == 400
    assert corps["error"] == "unknown_fields"
