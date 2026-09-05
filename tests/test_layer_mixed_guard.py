"""#329 — une couche mal orthographiée se REFUSE, elle n'écrase jamais.

Le trou : `{"origine": "x", "sourse": "y"}` — une couche connue + une faute de
frappe, SANS `valeur` — n'était pas reconnu comme couches (`_writes_layers`
strict) ni refusé (`unknown_layers` court-circuitait sans `valeur`) : le dict
entier devenait une valeur json ordinaire et ÉCRASAIT la valeur existante,
sans erreur. Le geste exact du rattrapage de socle (#326), une faute de frappe
plus loin. Une faute systématique dans une procédure d'enrichissement
effacerait un champ sur ~9 000 lignes — la campagne qui démarre écrit
exactement des couches, ce lot la précède.

La règle (arbitrage superviseur, 13-14/08) : un dict qui MÊLE ≥1 clé de couche
connue et ≥1 clé inconnue est REFUSÉ en nommant l'intruse — à TOUTE
profondeur, attributs d'items de colonne-liste compris (c'est là que passent
les écritures réelles). Un dict sans AUCUNE clé de couche reste une donnée
json libre ; une colonne déclarée `json` est exempte ; l'ambiguïté ne s'écrit
pas, elle se refuse.

Volet 2, même famille : un nom de COLONNE contenant un point fabriquait une
colonne littérale fantôme — acceptée, persistée, invisible au filtre du même
nom (l'adresse lit la couche, jamais la colonne littérale). Refus nommant la
forme imbriquée.

⚠️ **Volet 2 rétréci le 01/09/2026 (#684/#687)** : le refus portait aussi sur
ce que la LECTURE produit — `flat_layers` sert `naf` et `naf.comment` côte à
côte — donc une fiche relue et réémise entière se faisait refuser. Une adresse
d'annotation dont la colonne est réelle est désormais RANGÉE ; seul ce qui ne
désigne aucune colonne reste refusé.

Chemin réel (`DatastorePg.update_row`/`append_row`, banc de
test_datastore_layer_directions) — pas la fonction de garde seule.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg
from oto_mcp.datastore.errors import RowValidationError


class _Db:
    def __init__(self):
        self.rows: dict = {}

    def datastore_get_row(self, ns_id, row_id):
        return self.rows.get(row_id)

    def datastore_merge_row_locked(self, ns_id, row_id, apply, now, **kw):
        cur = self.rows.get(row_id)
        if cur is None:
            return None
        merged = apply(dict(cur["data"]))
        cur["data"] = merged
        return cur, merged

    def datastore_update_row(self, ns_id, row_id, data, now, **kw):
        self.rows[row_id]["data"] = data
        return self.rows[row_id]

    def datastore_insert_row(self, ns_id, row_id, data, *a, **kw):
        self.rows[row_id] = {"row_id": row_id, "created_at": "t",
                             "updated_at": "t", "data": data}
        return self.rows[row_id]


def _monte(monkeypatch, schema=None):
    from oto_mcp.datastore import core as ds
    db = _Db()
    s = DatastorePg("u-1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 1)
    monkeypatch.setattr(s, "_ns_of", lambda ns_id: {"schema": schema, "namespace": "t"})
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: schema)
    monkeypatch.setattr(s, "_assert_writable", lambda *a, **k: None)
    monkeypatch.setattr(s, "_trace", lambda *a, **k: None)
    for name in ("datastore_get_row", "datastore_merge_row_locked",
                 "datastore_update_row", "datastore_insert_row"):
        if hasattr(ds.db, name):
            monkeypatch.setattr(ds.db, name, getattr(db, name))
    db.rows["r1"] = {"row_id": "r1", "created_at": "t", "updated_at": "t",
                     "data": {"naf": "58.11Z", "email": "a@b.c"}}
    return s, db


# ── le trou principal : mixte sans `valeur`, premier niveau ──────────────────

def test_une_couche_mal_orthographiee_ne_detruit_jamais_la_valeur(monkeypatch):
    s, db = _monte(monkeypatch)
    with pytest.raises(RowValidationError) as e:
        s.update_row("t", "r1", {"naf": {"origine": "x", "sourse": "hunter"}}, origine_override=True)
    msg = str(e.value)
    assert "sourse" in msg and "origine" in msg, "le refus NOMME l'intruse et le vocabulaire"
    assert db.rows["r1"]["data"]["naf"] == "58.11Z", \
        "la valeur existante est INTACTE — c'est tout l'enjeu (#329)"


def test_le_cas_reel_de_la_campagne_les_attributs_ditems(monkeypatch):
    """Le grain FEUILLE : la garde mord À L'INTÉRIEUR d'un item de colonne-liste
    — c'est là que passent les milliers d'écritures réelles (contacts)."""
    s, db = _monte(monkeypatch)
    with pytest.raises(RowValidationError) as e:
        s.update_row("t", "r1", {"contacts": [
            {"email": {"valeur": "a@b.c", "sourse": "site"}}]})
    assert "sourse" in str(e.value)
    assert "contacts" not in db.rows["r1"]["data"], "rien n'est écrit"


def test_item_mixte_sans_valeur_meme_regle(monkeypatch):
    s, db = _monte(monkeypatch)
    with pytest.raises(RowValidationError):
        s.update_row("t", "r1", {"contacts": [
            {"email": {"origine": "x", "sourse": "y"}}]})


def test_un_attribut_libre_sans_couche_reste_accepte(monkeypatch):
    s, db = _monte(monkeypatch)
    s.update_row("t", "r1", {"contacts": [{"notes": {"perso": 1, "tags": ["a"]}}]})
    assert db.rows["r1"]["data"]["contacts"] == [{"notes": {"perso": 1, "tags": ["a"]}}]


def test_une_colonne_declaree_json_est_exempte(monkeypatch):
    """Un objet métier peut porter une clé nommée `origine` : la déclaration
    `json` est LA sortie légitime de l'ambiguïté — le refus la nomme."""
    schema = {"fields": [{"key": "meta", "type": "json"}]}
    s, db = _monte(monkeypatch, schema=schema)
    s.update_row("t", "r1", {"meta": {"origine": "import", "batch": 7}}, origine_override=True)
    assert db.rows["r1"]["data"]["meta"] == {"origine": "import", "batch": 7}


def test_le_refus_vaut_aussi_a_la_creation(monkeypatch):
    s, db = _monte(monkeypatch)
    with pytest.raises(RowValidationError):
        s.append_row("t", {"naf": {"origine": "x", "sourse": "y"}}, origine_override=True)


# ── volet 2 : le point dans un nom de colonne ────────────────────────────────

def test_un_nom_a_point_SANS_COLONNE_REELLE_est_refuse(monkeypatch):
    """⚠️ **Rétréci le 01/09/2026 (#684/#687).** Le volet 2 refusait TOUT nom pointé —
    y compris ce que notre propre lecture produit, puisque `flat_layers` sert
    `naf.comment` à côté de `naf`. Ce qui reste refusé, et c'est le défaut de
    production, c'est l'adresse dont **la colonne n'existe nulle part** : elle ne
    désigne rien, donc elle fabriquerait la colonne littérale fantôme."""
    s, db = _monte(monkeypatch)
    with pytest.raises(RowValidationError) as e:
        s.update_row("t", "r1", {"fantome.comment": "vérifié"})
    assert "fantome" in str(e.value) and "imbriqu" in str(e.value)
    assert "fantome.comment" not in db.rows["r1"]["data"], \
        "la colonne fantôme — invisible au filtre du même nom — ne naît plus"


def test_un_nom_a_point_SUR_UNE_COLONNE_REELLE_est_range(monkeypatch):
    """Le pendant, et c'est l'aller-retour : `naf` est sur la ligne, donc
    `naf.comment` est l'adresse de son annotation — on la range, on ne la refuse pas.
    Sinon la fiche relue et repoussée (#390) se ferait rejeter."""
    s, db = _monte(monkeypatch)
    s.update_row("t", "r1", {"naf.comment": "vérifié"})
    assert db.rows["r1"]["data"]["naf"] == {"valeur": "58.11Z", "comment": "vérifié"}
    assert "naf.comment" not in db.rows["r1"]["data"], "rangée, pas littérale"


def test_une_adresse_ditem_en_nom_de_colonne_est_refusee(monkeypatch):
    s, db = _monte(monkeypatch)
    with pytest.raises(RowValidationError):
        s.update_row("t", "r1", {"contacts[0].email": "x@y.z"})


# ── non-régression : tout ce qui était légitime le reste ─────────────────────

def test_le_rattrapage_origine_seule_marche_toujours(monkeypatch):
    """#326 : poser l'origine par-dessus une valeur existante — le geste nominal
    du rattrapage de socle. La garde ne doit PAS le toucher."""
    s, db = _monte(monkeypatch)
    s.update_row("t", "r1", {"naf": {"origine": "INSEE"}}, origine_override=True)
    assert db.rows["r1"]["data"]["naf"] == {"valeur": "58.11Z", "origine": "INSEE"}


def test_un_json_libre_sans_aucune_couche_reste_accepte(monkeypatch):
    s, db = _monte(monkeypatch)
    s.update_row("t", "r1", {"naf": {"a": 1, "sourse": 2}})
    assert db.rows["r1"]["data"]["naf"] == {"a": 1, "sourse": 2}, \
        "aucune clé de couche présente = donnée json ordinaire, on ne devine pas"


# ── unknown_layers : la même validation dans les deux cas ────────────────────

def test_unknown_layers_mord_aussi_sans_valeur():
    """Le court-circuit `valeur absente` était l'oubli : la même validation
    s'applique qu'une écriture de couches porte `valeur` ou non."""
    assert dsv2.unknown_layers({"origine": "x", "sourse": "y"}) == ["sourse"]
    assert dsv2.unknown_layers({"valeur": 1, "sourse": "y"}) == ["sourse"]
    assert dsv2.unknown_layers({"a": 1, "sourse": 2}) == [], \
        "sans AUCUNE couche connue, rien n'est jugé"
