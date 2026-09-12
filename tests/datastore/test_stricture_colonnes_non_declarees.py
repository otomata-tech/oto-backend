"""Datastore — le troisième cran : REFUSER une colonne non déclarée au premier
niveau (#614 / #678).

Ce que ce banc décrit, et pourquoi il existe :

`strict` porte depuis toujours DEUX contrats sous un seul mot — rapporteur au
premier niveau (`hors_schema`, arbitrage #294), refus dans un sous-record déclaré
(#544). L'asymétrie est délibérée et le reste : au premier niveau, un nom inconnu
crée une VRAIE colonne qu'on peut déclarer après coup, et c'est ce qui permet
d'explorer un tableau avant de le typer.

Ce que le cran ajoute est un TROISIÈME état, opt-in table par table :
`unknown_fields: "reject"`. Le défaut (`report`) ne bouge pas — le fermer
retirerait un droit du contrat 0016.

⚠️ Deux propriétés sont plus importantes que le refus lui-même, parce que ce sont
elles qui décident si le cran est tenable sur un tableau vivant :

1. **il se juge sur ce que le geste POSE**, jamais sur la ligne mergée — un
   tableau qui porte déjà 162 colonnes hors schéma reste écrivable, et un patch
   sur une colonne sans rapport ne se fait pas refuser pour un défaut accumulé
   ailleurs (même borne que `max_length`, même raison : oto-backend#284) ;
2. **il ne peut pas se déclarer inerte** — `reject` sur un tableau qui n'est pas
   `strict`, ou qui ne déclare aucun champ, ne refuserait jamais rien : la
   déclaration est refusée à la POSE. Une option qui promet plus qu'elle ne fait
   est pire qu'une option absente.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg
from oto_mcp.datastore.errors import RowValidationError


FIELDS = [{"key": "siren", "type": "text"},
          {"key": "adresse", "type": "text"},
          {"key": "contacts", "type": "list",
           "of": {"fields": [{"key": "nom", "type": "text"}]}}]
REJECT = {"strict": True, "key": "siren", "unknown_fields": "reject",
          "fields": FIELDS}
REPORT = {"strict": True, "key": "siren", "fields": FIELDS}


# ── la décision, en fonction pure ────────────────────────────────────────────

def test_le_defaut_est_le_rapporteur():
    """Un schéma qui ne dit rien garde le comportement de #294 — le cran est
    opt-in, et un tableau se remplit souvent avant d'avoir son format."""
    assert dsv2.unknown_fields_mode(REPORT) == "report"
    assert dsv2.unknown_fields_mode(None) == "report"
    assert dsv2.unknown_fields_mode({}) == "report"


def test_le_mode_declare_se_lit():
    assert dsv2.unknown_fields_mode(REJECT) == "reject"


def test_le_rapporteur_ne_refuse_rien():
    errors, _ = dsv2.off_schema_refusal(REPORT, {"siren": "1", "_liberation": "x"})
    assert errors == []


def test_le_refus_nomme_la_colonne_et_le_referentiel():
    """Le modèle est le refus des sous-records (#544) : où, ce qui était attendu,
    et que RIEN n'a été écrit."""
    errors, details = dsv2.off_schema_refusal(
        REJECT, {"siren": "1", "_liberation": "x"})
    assert len(errors) == 1
    msg = errors[0]
    assert "`_liberation`" in msg
    assert "siren" in msg and "adresse" in msg     # le référentiel est dit
    assert "rien n'a été écrit" in msg.lower()
    assert details == {}      # #678 : aucune destination inventée


def test_le_refus_dit_qu_aucune_destination_n_existe():
    """#678 : « une destination inventée est pire qu'une destination absente ».
    Le refus doit DIRE qu'aucune colonne ne porte ce nom, pas pointer à côté."""
    errors, details = dsv2.off_schema_refusal(REJECT, {"_liberation": "x"})
    assert "expected_column" not in details
    assert "aucune colonne déclarée" in errors[0].lower()


def test_toutes_les_colonnes_inventees_d_un_coup():
    """Un agent qui renomme trois champs les apprend en un aller-retour, pas trois."""
    errors, _ = dsv2.off_schema_refusal(
        REJECT, {"siren": "1", "_action": "a", "_liberation": "b", "invente": "c"})
    assert len(errors) == 1
    for nom in ("_action", "_liberation", "invente"):
        assert f"`{nom}`" in errors[0]


def test_une_couche_d_une_colonne_declaree_n_est_pas_une_colonne():
    """`adresse.comment` est la couche de `adresse`, pas une colonne inventée —
    et c'est la forme que rend une relecture. La refuser casserait l'aller-retour."""
    errors, _ = dsv2.off_schema_refusal(REJECT, {"adresse.comment": "registre — …"})
    assert errors == []


# ── la déclaration : un cran qui ne peut pas s'appliquer se refuse ───────────

def test_reject_sur_un_tableau_non_strict_est_refuse_a_la_pose():
    """Sans `strict`, `off_schema_keys` ne relève rien : le cran serait INERTE.
    Accepté-inerte = la forme que #347 a fermée."""
    errs = dsv2.validate_schema_def(
        {"unknown_fields": "reject", "fields": FIELDS})
    assert any("strict" in e for e in errs), errs


def test_reject_sans_aucun_champ_declare_est_refuse_a_la_pose():
    """Sans référentiel, tout serait hors schéma — le tableau deviendrait
    inécrivable d'un coup."""
    errs = dsv2.validate_schema_def(
        {"strict": True, "unknown_fields": "reject", "fields": []})
    assert any("aucun champ" in e or "référentiel" in e for e in errs), errs


def test_une_valeur_hors_du_couple_ferme_est_refusee():
    errs = dsv2.validate_schema_def(
        {"strict": True, "unknown_fields": "refuse", "fields": FIELDS})
    assert any("report" in e and "reject" in e for e in errs), errs


def test_report_explicite_est_accepte():
    assert dsv2.validate_schema_def(
        {"strict": True, "unknown_fields": "report", "fields": FIELDS}) == []


def test_reject_bien_pose_est_accepte():
    assert dsv2.validate_schema_def(REJECT) == []


def test_le_cran_s_annonce_dans_enforced():
    """`enforced` est SONDÉ sur la fonction qui décide, jamais listé : le jour où
    le cran disparaît, l'annonce tombe avec lui."""
    dsv2.reset_enforced_keys()
    try:
        assert "unknown_fields" in dsv2.enforced_keys()
    finally:
        dsv2.reset_enforced_keys()


# ── le geste : les cinq portes d'écriture ────────────────────────────────────

def _fake_merge_locked(rows):
    def merge_locked(ns_id, row_id, apply_fn, updated_at, **k):
        if row_id not in rows:
            return None
        merged = apply_fn(dict(rows[row_id]))
        rows[row_id] = dict(merged)
        return ({"row_id": row_id, "created_at": "t0", "updated_at": updated_at,
                 "data": dict(merged)}, merged)
    return merge_locked


@pytest.fixture()
def banc(monkeypatch):
    """Un tableau `viviers` d'UNE ligne, schéma commutable — `etat["creees"]` et
    `etat["maj"]` distinguent « rien n'a été écrit » d'une erreur rendue après coup."""
    st = DatastorePg("u", acting_org=35)
    etat = {"schema": REJECT,
            "lignes": {"r1": {"siren": "552081317", "adresse": "1 rue A"}},
            "creees": [], "maj": []}
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "datastore": "viviers",
                                       "schema": etat["schema"]})

    def find(ns_id, key, kv):
        for rid, data in etat["lignes"].items():
            if key and str(data.get(key)) == str(kv):
                return rid
        return None

    def insert(ns_id, rid, data, *a, **k):
        etat["creees"].append(data)
        etat["lignes"][rid] = dict(data)
        return {"row_id": rid, "created_at": "t", "updated_at": "t", "data": data}

    def get_row(ns_id, rid):
        data = etat["lignes"].get(rid)
        return ({"row_id": rid, "created_at": "t", "updated_at": "t",
                 "data": dict(data)} if data is not None else None)

    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    monkeypatch.setattr(dsm.db, "datastore_get_row", get_row)
    monkeypatch.setattr(dsm.db, "datastore_insert_row", insert)
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    monkeypatch.setattr(dsm.db, "datastore_upsert_row",
                        lambda ns_id, rid, data: ({"row_id": rid, "created_at": "t",
                                                   "updated_at": "t",
                                                   "data": dict(data)}, False))
    fusion = _fake_merge_locked(etat["lignes"])

    def fusion_relevee(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : une
        # fusion ABOUTIE est une écriture, un refus levé dans `apply_fn` n'en est pas une.
        sortie = fusion(ns_id, rid, apply_fn, updated_at, **k)
        if sortie is not None:
            etat["maj"].append(rid)
        return sortie

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion_relevee)
    return st, etat


def test_creation_refusee(banc):
    store, etat = banc
    with pytest.raises(RowValidationError) as e:
        store.append_row("viviers", {"siren": "999", "_liberation": "x"})
    assert "`_liberation`" in str(e.value)
    assert etat["creees"] == []          # rien n'est parti


def test_patch_par_identifiant_refuse(banc):
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.update_row("viviers", "r1", {"_liberation": "x"})
    assert etat["maj"] == []
    assert "_liberation" not in etat["lignes"]["r1"]


def test_fusion_sur_cle_metier_refusee(banc):
    """La porte du terrain : une fiche réémise avec sa clé fusionne — et c'est là
    que les colonnes inventées entraient."""
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.append_row("viviers", {"siren": "552081317", "_liberation": "x"})
    assert "_liberation" not in etat["lignes"]["r1"]


def test_remplacement_refuse(banc):
    store, _ = banc
    with pytest.raises(RowValidationError):
        store.upsert_row("viviers", "r1", {"siren": "552081317", "_liberation": "x"})


def test_lot_refuse(banc):
    """Le lot est le chemin le plus volumineux — c'est par lui que passent les
    imports et l'upload signé."""
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.write_rows("viviers", [{"siren": "111", "_liberation": "x"}])
    assert etat["creees"] == []


# ── ce que le cran ne doit PAS casser ────────────────────────────────────────

def test_une_ecriture_dans_le_format_passe(banc):
    store, etat = banc
    store.append_row("viviers", {"siren": "552081317", "adresse": "2 rue B"})
    assert etat["lignes"]["r1"]["adresse"] == "2 rue B"


def test_un_patch_ne_paie_pas_les_colonnes_deja_en_base(banc):
    """LE cas qui rend le cran tenable : la ligne porte DÉJÀ une colonne hors
    schéma (162 accumulées en production). Un patch sur une colonne sans rapport
    ne doit pas s'en trouver refusé — le cran juge ce que le geste POSE."""
    store, etat = banc
    etat["lignes"]["r1"]["_vieille_colonne"] = "héritée"
    store.update_row("viviers", "r1", {"adresse": "3 rue C"})
    assert etat["lignes"]["r1"]["adresse"] == "3 rue C"
    assert etat["lignes"]["r1"]["_vieille_colonne"] == "héritée"


def test_le_mode_report_reste_le_rapporteur(banc):
    """Le défaut ne bouge pas : la valeur persiste et `hors_schema` la nomme."""
    store, etat = banc
    etat["schema"] = REPORT
    store.append_row("viviers", {"siren": "552081317", "_liberation": "x"})
    assert etat["lignes"]["r1"]["_liberation"] == "x"
    assert store.off_schema_report()["hors_schema"] == ["_liberation"]


# ── la POSE du cran ──────────────────────────────────────────────────────────

def test_le_cran_se_pose_sans_reecrire_le_schema(monkeypatch):
    """Un tableau se ferme quand il a FINI d'être exploré, donc quand son schéma
    est long : le poser par `set` obligerait à réécrire quatre-vingts champs pour
    une clé de tête — le geste exact que `patch` existe pour éviter (#388)."""
    store = DatastorePg("u", acting_org=35)
    vu: dict = {}
    monkeypatch.setattr(store, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(store, "_schema_of", lambda ns_id: dict(REPORT))
    monkeypatch.setattr(store, "set_schema",
                        lambda ns, sch, **k: vu.update(schema=sch) or
                        {"datastore": ns, "schema": sch, "enforced": []})
    store.patch_schema("viviers", unknown_fields="reject")
    assert vu["schema"]["unknown_fields"] == "reject"
    assert vu["schema"]["fields"] == FIELDS      # rien d'autre n'a bougé


def test_une_valeur_illisible_n_est_PAS_repliee_sur_le_defaut(monkeypatch):
    """Elle traverse telle quelle jusqu'à `validate_schema_def`, qui la refuse en
    nommant les deux modes. La replier ici rendrait un SUCCÈS à qui croit avoir
    fermé son tableau — le défaut même que ce cran corrige."""
    store = DatastorePg("u", acting_org=35)
    vu: dict = {}
    monkeypatch.setattr(store, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(store, "_schema_of", lambda ns_id: dict(REPORT))
    monkeypatch.setattr(store, "set_schema",
                        lambda ns, sch, **k: vu.update(schema=sch) or {})
    store.patch_schema("viviers", unknown_fields="refuse")
    assert vu["schema"]["unknown_fields"] == "refuse"


def test_patcher_seulement_le_cran_n_est_pas_un_appel_vide(monkeypatch):
    """La garde « rien à patcher » doit compter la clé neuve, sinon le seul geste
    qui pose le cran est refusé comme un appel sans objet."""
    store = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(store, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(store, "_schema_of", lambda ns_id: dict(REPORT))
    monkeypatch.setattr(store, "set_schema", lambda ns, sch, **k: {})
    store.patch_schema("viviers", unknown_fields="reject")   # ne lève pas
