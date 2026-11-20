"""Poser et retirer `key_required` PAR PATCH, sans réécrire le schéma (#516, suite).

Le cran « écrire, jamais créer » (#516/#552) ne se posait et ne se retirait que par
`data_set_schema`, qui REMPLACE le schéma entier : `data_patch_schema` ne connaissait
que `strict` et `key` en tête. Or `set` est le piège documenté pour retoucher (#388),
et une équipe refuse à raison de réécrire un schéma de 80 champs pour poser une clé de
tête — c'est exactement le geste qu'un patch existe pour éviter.

Ce banc prouve la propriété qui justifie le lot : la pose par patch préserve CHAQUE
déclaration de champ (labels, notes, options, `required_when`, `max_length`,
sous-records), et le retrait ne fait pas crier le relevé d'effacement.
"""
from __future__ import annotations

import copy

import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.capabilities.datastore import columns as dcc
from oto_mcp.datastore.core import DatastorePg

from _datastore_rest import call, stub_authz


# Un schéma « de terrain » : tout ce qu'une reconstruction par `set` perdrait.
_FIELDS = [
    {"key": "siren", "type": "text", "label": "SIREN",
     "help": "L'identifiant légal.", "max_length": 9, "pattern": r"^\d{9}$"},
    {"key": "statut", "type": "enum", "label": "Statut",
     "options": ["a_faire", "en_cours", "fait"], "role": "status"},
    {"key": "compte_rendu", "type": "text", "label": "Compte rendu",
     "required_when": {"statut": "fait"}, "max_length": 2000, "width": "full"},
    {"key": "occupant", "type": "object",
     "fields": [{"key": "nom", "type": "text", "label": "Nom"},
                {"key": "naf", "type": "text", "label": "NAF", "max_length": 6}]},
    {"key": "contacts", "type": "list",
     "of": {"fields": [{"key": "nom", "type": "text", "label": "Nom"},
                       {"key": "email", "type": "email", "label": "E-mail"}]}},
]
OUVERT = {"unknown_columns": "report", "key": "siren", "fields": _FIELDS}
FERME = {**OUVERT, "new_rows": "reject"}
SANS_CLE = {"unknown_columns": "report", "fields": _FIELDS}


def _banc(monkeypatch, current: dict):
    """Un store réel dont `set_schema` tourne tel quel — on stubbe ce qu'IL appelle,
    jamais lui : c'est ainsi que le patch hérite de ses gardes (`validate_schema_def`)
    et de ses relevés (`declarations_effacees`, `enforced`)."""
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    posed: dict = {}
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": copy.deepcopy(current)})
    monkeypatch.setattr(dsm.db, "set_datastore_schema",
                        lambda ns_id, schema: posed.update(schema=schema))
    monkeypatch.setattr(dsm.db, "datastore_key_dup_groups", lambda ns_id, key: [])
    monkeypatch.setattr(dsm.db, "datastore_ensure_key_index", lambda ns_id, key: None)
    # oto#82 : ce db stubbé ne porte aucun index — la pose reste donc faite.
    monkeypatch.setattr(dsm.db, "datastore_has_key_index", lambda ns_id: False)
    monkeypatch.setattr(dsm.db, "datastore_drop_key_index", lambda ns_id: None)
    monkeypatch.setattr(dsm.db, "datastore_overlong_fields", lambda ns_id, bounds: [])
    monkeypatch.setattr(dsm.db, "datastore_field_values", lambda *a, **k: {})
    monkeypatch.setattr(dsm.db, "datastore_offending_enum_values", lambda *a, **k: [])
    monkeypatch.setattr(dsm.db, "datastore_row_keys", lambda ns_id, sample=1000: [])
    # oto-backend#479 : le relevé des lignes en place compte d'abord le tableau — vide ici.
    monkeypatch.setattr(dsm.db, "datastore_count_rows", lambda *a, **k: 0)
    return st, posed


# ── la pose ──────────────────────────────────────────────────────────────────

def test_la_pose_par_patch_preserve_chaque_declaration(monkeypatch):
    """LA propriété du lot : poser le cran ne touche à rien d'autre — pas une note,
    pas une borne, pas une option, pas un sous-champ."""
    st, posed = _banc(monkeypatch, OUVERT)
    out = st.patch_schema("v", new_rows="reject")
    assert posed["schema"]["fields"] == _FIELDS            # égalité PROFONDE
    assert posed["schema"]["unknown_columns"] == "report" and posed["schema"]["key"] == "siren"
    assert posed["schema"]["new_rows"] == "reject"
    assert dsv2.creation_refusee(posed["schema"]) is True    # ce que l'écriture LIRA
    assert out["schema"] == posed["schema"]
    # rien n'a disparu : le relevé d'effacement n'a rien à dire
    assert "declarations_effacees" not in out
    assert out["added"] == [] and out["updated"] == [] and out["removed"] == []


def test_la_pose_annonce_le_cran_dans_enforced(monkeypatch):
    """`enforced` (#389) dit ce que CETTE version fait respecter : un client qui vient
    de fermer un tableau doit lire que le serveur qui lui répond sait le fermer."""
    st, _ = _banc(monkeypatch, OUVERT)
    out = st.patch_schema("v", new_rows="reject")
    assert "new_rows" in out["enforced"]


def test_poser_le_cran_sans_cle_est_refuse_au_patch(monkeypatch):
    """Le patch repasse par `set_schema`, donc par `validate_schema_def` : le refus
    existant s'applique, avec son message — et rien n'est posé."""
    st, posed = _banc(monkeypatch, SANS_CLE)
    with pytest.raises(ValueError, match="new_rows: \"reject\" exige une clé métier"):
        st.patch_schema("v", new_rows="reject")
    assert posed == {}


def test_poser_la_cle_et_le_cran_dans_le_meme_appel_passe(monkeypatch):
    """Un tableau sans clé se ferme en UN geste : `key` et `key_required` ensemble —
    le refus juge le schéma résultant, pas le schéma courant."""
    st, posed = _banc(monkeypatch, SANS_CLE)
    out = st.patch_schema("v", key="siren", new_rows="reject")
    assert posed["schema"]["key"] == "siren" and posed["schema"]["new_rows"] == "reject"
    assert dsv2.creation_refusee(posed["schema"]) is True
    assert posed["schema"]["fields"] == _FIELDS
    assert "new_rows" in out["enforced"]


# ── le retrait ───────────────────────────────────────────────────────────────

def test_le_retrait_par_patch_desarme_sans_crier(monkeypatch):
    """`key_required=false` rouvre le tableau. Il ÉCRIT `false` plutôt que de retirer
    la clé : le relevé d'effacement compte les disparitions de tête sans exception,
    et un geste explicite ne doit pas crier sur lui-même (l'avertissement qu'on
    apprend à ignorer). Ce que l'écriture lit — `creation_refusee` — est désarmé."""
    st, posed = _banc(monkeypatch, FERME)
    out = st.patch_schema("v", new_rows="create")
    assert dsv2.creation_refusee(posed["schema"]) is False
    assert posed["schema"]["new_rows"] == "create"
    assert posed["schema"]["fields"] == _FIELDS
    assert posed["schema"]["key"] == "siren"                 # la clé métier reste
    assert "declarations_effacees" not in out


def test_omis_le_cran_ne_bouge_pas(monkeypatch):
    """Même sémantique que `strict` : absent du patch = inchangé, dans les deux
    régimes."""
    st, posed = _banc(monkeypatch, FERME)
    st.patch_schema("v", fields=[{"key": "statut", "label": "État"}])
    assert posed["schema"]["new_rows"] == "reject"
    st, posed = _banc(monkeypatch, OUVERT)
    st.patch_schema("v", key="siren")
    assert "new_rows" not in posed["schema"]


def test_le_cran_seul_est_un_patch_non_vide(monkeypatch):
    """La garde « rien à patcher » connaît la clé neuve : `key_required` seul est
    un geste complet, pas un appel vide."""
    st, posed = _banc(monkeypatch, OUVERT)
    st.patch_schema("v", new_rows="reject")
    assert posed["schema"]["new_rows"] == "reject"


# ── la face REST (même `PatchSchemaInput`, verbe PATCH) ──────────────────────

class _Store:
    def __init__(self):
        self.calls: list = []

    def patch_schema(self, datastore, **kw):
        self.calls.append((datastore, kw))
        return {"datastore": datastore, "schema": {"key": "siren", "new_rows": "reject",
                                                   "fields": []},
                "added": [], "updated": [], "removed": [], "enforced": ["new_rows"]}


def test_la_face_REST_passe_key_required_au_store(monkeypatch):
    """`PATCH /api/datastores/{ns}/schema` sert le même Input : la clé
    neuve traverse le corps jusqu'au store (avant ce lot : `400 unknown_fields`)."""
    stub_authz(monkeypatch)
    store = _Store()
    monkeypatch.setattr(dcc, "make_store", lambda sub: store)
    status, corps = call("me.datastore.patch_schema",
                         path_params={"datastore": "160"},
                         body={"new_rows": "reject"})
    assert status == 200, corps
    assert store.calls == [("160", {"fields": None, "remove": None, "remove_attrs": None,
                                       "key": None, "new_rows": "reject"})]
    assert corps["schema"]["new_rows"] == "reject"
    assert "new_rows" in corps["enforced"]
