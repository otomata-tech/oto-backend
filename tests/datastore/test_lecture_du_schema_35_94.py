"""Lire un schéma : la forme COMPACTE (oto#35) et le schéma tel qu'il est STOCKÉ (oto#94).

Ce banc fige :

1. **la forme compacte** garde les clés que le validateur lit, à chaque niveau, et rien
   d'autre — ni prose, ni libellé, ni `meta`, ni clé nulle ;
2. **les gardes** sont lues des prédicats qui décident, et `sans_effet` nomme ce qui est
   déclaré sans être appliqué — avec son contrôle négatif (un tableau strict n'a pas
   d'`options` inertes) ;
3. **sur le vrai chemin** : la face outil dit combien de colonnes elle retire, la
   lecture stockée rend la colonne masquée au titre et la refuse sans titre, la face
   REST rend `tel_que` et la forme compacte par la query string.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import session_org
from oto_mcp.datastore import acces_agent as aga
from oto_mcp.datastore import lecture_du_schema as lds
from oto_mcp.datastore import schema_keys as sk


SCHEMA = {
    "key": "ref",
    "description": "Le vivier — un long texte écrit pour l'agent.",
    "meta": {"a_moi": 1},
    "fields": [
        {"key": "ref", "type": "text", "label": "Référence", "required": True,
         "description": "L'identifiant.", "role": None},
        {"key": "etat", "type": "enum", "options": ["a_faire", "fait"],
         "label": "État", "hidden": True, "width": "half",
         "lifecycle": {"states": ["a_faire", "fait"],
                       "transitions": {"a_faire": ["fait"]},
                       "labels": {"a_faire": "À faire"}, "meta": {"x": 1}}},
        {"key": "contacts", "type": "list", "label": "Contacts",
         "of": {"type": "object", "label": "Contact", "description": "Un contact.",
                "fields": [{"key": "email", "type": "text", "max_length": 120,
                            "description": "Son courriel.", "meta": {"y": 2}}]}},
        {"key": "source", "type": "text", "readonly": True, "label": "Source"},
        {"key": "calcul", "type": "formula", "formula": "=1"},
        {"key": "suivi", "type": "text", "agent_access": "none",
         "description": "Où en est VOTRE démarche."},
        {"key": "score", "type": "text", "agent_access": "read"},
    ],
}


# ── 1. La forme compacte ─────────────────────────────────────────────────────


def test_la_forme_compacte_garde_les_contraintes_et_rien_d_autre():
    c = lds.compacte(SCHEMA)
    assert c["key"] == "ref" and "description" not in c and "meta" not in c
    champs = {f["key"]: f for f in c["fields"]}
    # Clé nulle retirée, prose et libellé aussi ; la contrainte reste.
    assert champs["ref"] == {"key": "ref", "type": "text", "required": True}
    # Présentation (`hidden`, `width`, `label`) dehors ; `options` et le cycle dedans,
    # sans ses `labels` (présentation) ni sa `meta`.
    assert champs["etat"] == {
        "key": "etat", "type": "enum", "options": ["a_faire", "fait"],
        "lifecycle": {"states": ["a_faire", "fait"],
                      "transitions": {"a_faire": ["fait"]}}}
    # La descente traverse `of` et ses sous-champs.
    assert champs["contacts"] == {
        "key": "contacts", "type": "list",
        "of": {"type": "object",
               "fields": [{"key": "email", "type": "text", "max_length": 120}]}}
    # Les crans de garde sont des contraintes : ils restent.
    assert champs["suivi"] == {"key": "suivi", "type": "text", "agent_access": "none"}
    assert champs["calcul"]["formula"] == "=1"


def test_la_forme_compacte_ne_mute_pas_le_schema_lu():
    avant = repr(SCHEMA)
    lds.compacte(SCHEMA)
    assert repr(SCHEMA) == avant


def test_la_forme_compacte_d_un_tableau_sans_schema_est_none():
    assert lds.compacte(None) is None


def test_les_contraintes_sont_derivees_des_lecteurs_declares():
    """Aucune liste tenue à part : une clé « front » seule n'entre jamais, une clé
    lue par le validateur entre toujours."""
    for niveau, cles in sk.NIVEAUX.items():
        for c in cles:
            assert (c.nom in sk.CONTRAINTES[niveau]) is ("validateur" in c.lecteurs), \
                (niveau, c.nom)
    assert all("description" not in v and sk.META not in v
               for v in sk.CONTRAINTES.values())


# ── 2. Les gardes ────────────────────────────────────────────────────────────


def test_les_gardes_disent_ce_que_les_predicats_appliquent():
    g = lds.gardes(SCHEMA)
    assert g["verrouillees"] == ["calcul", "source"]       # formule = readonly implicite
    assert g["masquees_a_l_agent"] == ["suivi"]
    assert g["lecture_seule_agent"] == ["score"]


def test_sans_effet_nomme_ce_qui_est_declare_et_que_rien_n_applique():
    schema = {
        "key": "ref",
        "fields": [
            {"key": "ref", "type": "text", "agent_access": "none"},  # clé métier
            {"key": "x", "type": "text", "agent_access": "personne"},  # valeur inconnue
            {"key": "choix", "type": "text", "options": ["a", "b"]},  # appliquée (oto#124)
            {"key": "statut", "type": "text",
             "lifecycle": {"states": ["a", "b"], "claimable": {"statut": "a"}}},
            {"key": "autre", "type": "text", "lifecycle": {"states": ["c"]}},
            {"key": "vieux", "type": "text", "enum": ["a"]},  # résidu d'avant le 01/10
        ],
    }
    vus = {(e["chemin"], e["cle"]) for e in lds.gardes(schema)["sans_effet"]}
    assert vus == {("ref", "agent_access"), ("x", "agent_access"),
                   ("autre", "lifecycle"), ("vieux", "enum")}
    raisons = {e["chemin"]: e["raison"] for e in lds.gardes(schema)["sans_effet"]
               if e["cle"] == "agent_access"}
    assert "business key" in raisons["ref"] and "not recognized" in raisons["x"]


def test_contre_epreuve_un_tableau_strict_n_a_pas_d_options_inertes():
    schema = {"fields": [{"key": "choix", "type": "text", "options": ["a", "b"]},
                         {"key": "suivi", "type": "text", "agent_access": "none"}]}
    assert lds.gardes(schema) == {"masquees_a_l_agent": ["suivi"]}
    assert aga.sans_effet(schema) == []


def test_rien_de_declare_rien_d_inerte():
    assert lds.gardes({"fields": [{"key": "a", "type": "text"}]}) == {}
    assert lds.gardes(None) == {}


# ── 3. Sur le vrai chemin ────────────────────────────────────────────────────


def _table(sub: str):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", sub, "t-" + uuid.uuid4().hex[:6]))
    make_store(sub).set_schema(ns, SCHEMA)
    return ns


def _lire(sub: str, ns: str, **params) -> dict:
    """Le HANDLER de la capacité — celui que montent les deux faces."""
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.datastore.schema import GetSchemaInput, _get_schema
    return _get_schema(ResolvedCtx(sub=sub), GetSchemaInput(datastore=ns, **params))


def _cles(out: dict) -> set:
    return {f["key"] for f in out["schema"]["fields"]}


def test_bout_en_bout_la_face_outil_dit_combien_elle_retire(live):
    ns = _table("u-3594a")
    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        servi = _lire("u-3594a", ns)
        stocke = _lire("u-3594a", ns, tel_que="stocke")
    finally:
        session_org.reset_call_face(jeton)
    # Le servi : amputé, et il le DIT — le compte, pas le nom.
    assert servi["tel_que"] == "servi" and "suivi" not in _cles(servi)
    assert servi["colonnes_masquees"] == 1 and "gardes" not in servi
    assert "suivi" not in repr({k: v for k, v in servi.items() if k != "enforced"})
    # Le stocké, lu par le PROPRIÉTAIRE sur la même face : entier, gardes comprises.
    assert stocke["tel_que"] == "stocke" and "suivi" in _cles(stocke)
    assert "colonnes_masquees" not in stocke
    assert stocke["gardes"]["masquees_a_l_agent"] == ["suivi"]


def test_bout_en_bout_sans_titre_le_stocke_est_refuse(live, monkeypatch):
    """Le palier réel (possède ∪ gouverne) est éprouvé par `test_forcage_readonly_658` ;
    ici se mesure le CÂBLAGE — et que le refus renvoie au défaut, qui passe."""
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.datastore.core import DatastorePg

    ns = _table("u-3594b")
    monkeypatch.setattr(DatastorePg, "_peut_forcer", lambda self, ns_id: False)
    with pytest.raises(AuthzDenied) as refus:
        _lire("u-3594b", ns, tel_que="stocke")
    assert refus.value.status == 403 and "tel_que=servi" in refus.value.message
    assert _lire("u-3594b", ns)["tel_que"] == "servi"


def test_bout_en_bout_rest_rend_tel_que_et_la_forme_compacte(live, monkeypatch):
    import _datastore_rest as R

    ns = _table("u-1")
    R.stub_authz(monkeypatch, org_id=None)

    code, corps = R.call("me.datastore.get_schema", path_params={"datastore": ns})
    assert code == 200, corps
    # REST ne masque pas : rien de retiré, donc rien à compter.
    assert corps["tel_que"] == "servi" and "suivi" in _cles(corps)
    assert "colonnes_masquees" not in corps and "forme" not in corps

    code, corps = R.call("me.datastore.get_schema", path_params={"datastore": ns},
                         query=b"forme=compacte&tel_que=stocke")
    assert code == 200, corps
    assert corps["forme"] == "compacte" and corps["tel_que"] == "stocke"
    assert "description" not in repr(corps["schema"])
    assert corps["gardes"]["verrouillees"] == ["calcul", "source"]

    code, corps = R.call("me.datastore.get_schema", path_params={"datastore": ns},
                         query=b"forme=courte")
    assert code == 400, corps
