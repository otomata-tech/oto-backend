"""Les colonnes `date`/`datetime` lisent large et stockent UNE forme (oto-backend#859).

Mesuré le 30/09/2026 sur 225 tableaux : `Z`, offsets, fractions, sans fuseau, date nue
dans une colonne d'instants, heure et fuseau dans une colonne de jours, nombres — et
le tri comme les filtres comparaient du texte. Quatre moitiés, quatre bancs :

1. **la lecture** (`dates.lire`), pure : chaque forme d'entrée et ce qui est stocké ;
2. **l'écriture** contre une vraie base, par les trois portes (création, lot, patch
   par `id` ; le remplacement `upsert_row` est retiré, oto#156) : la forme stockée, la notice « supposé UTC », les couches
   qui survivent, le refus de l'illisible, au premier niveau comme en sous-champ ;
3. **les filtres** : `eq`/`lte`/`gte`/`gt`/`lt`/`ne`/`in` comparent des instants, et
   la borne d'une date imprécise couvre sa période ;
4. **le tri** lit une date imprécise à son début de période.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import dates


# ── 1. la lecture ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entree, datetime_, date_", [
    # ISO, toutes variantes
    ("2026-09-04T10:00:00Z", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T10:00:00.000Z", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T10:00:00.123456z", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T10:00Z", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T12:00:00+02:00", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T12:00:00+0200", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T12:00+02", "2026-09-04T10:00:00Z", "2026-09-04"),
    # une heure avec fuseau dans une colonne `date` : la date DANS ce fuseau
    ("2026-09-04T00:30:00+02:00", "2026-09-03T22:30:00Z", "2026-09-04"),
    ("2026-09-03T23:30:00-05:00", "2026-09-04T04:30:00Z", "2026-09-03"),
    # sans fuseau, séparateur espace : supposé UTC
    ("2026-09-04 10:00", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("2026-09-04T10:00:00", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("  2026-09-04 10:00:00  ", "2026-09-04T10:00:00Z", "2026-09-04"),
    # usage français, jour d'abord
    ("04/09/2026", "2026-09-04", "2026-09-04"),
    ("4/9/2026", "2026-09-04", "2026-09-04"),
    ("04/09/2026 10:30", "2026-09-04T10:30:00Z", "2026-09-04"),
    ("04/09/2026 10:30:15", "2026-09-04T10:30:15Z", "2026-09-04"),
    # imprécise : à sa précision, sans minuit fabriqué
    ("2026-09-04", "2026-09-04", "2026-09-04"),
    ("2026-09", "2026-09", "2026-09"),
    ("2026", "2026", "2026"),
    # horodatages : secondes, millisecondes (nombre ou chaîne de chiffres)
    (1788516000, "2026-09-04T10:00:00Z", "2026-09-04"),
    (1788516000.75, "2026-09-04T10:00:00Z", "2026-09-04"),
    (1788516000000, "2026-09-04T10:00:00Z", "2026-09-04"),
    ("1788516000", "2026-09-04T10:00:00Z", "2026-09-04"),
    ("1788516000000", "2026-09-04T10:00:00Z", "2026-09-04"),
])
def test_chaque_forme_se_lit_et_se_stocke_en_une_seule(entree, datetime_, date_):
    assert dates.lire(entree, "datetime").forme == datetime_
    assert dates.lire(entree, "date").forme == date_


@pytest.mark.parametrize("entree", [
    "bientôt", "2026-02-31", "2026-13", "2026-09-04T25:00:00Z", "32/01/2026",
    "09-04-2026", "20260904",       # 8 chiffres : ni ISO étendu, ni horodatage
    2026, 45900, "45900",            # sous 10⁸ : une année, un numéro de tableur
    True, None, "", {"a": 1}, [1], float("nan"),
])
def test_ce_qui_ne_se_lit_pas_rend_None(entree):
    assert dates.lire(entree, "datetime") is None
    assert dates.lire(entree, "date") is None


def test_la_precision_et_le_debut_de_periode():
    assert dates.lire("2026", "datetime").precision == "annee"
    m = dates.lire("2026-12", "datetime")
    assert (m.precision, m.debut.isoformat(), m.fin.isoformat()) == (
        "mois", "2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
    i = dates.lire("2026-09-04T10:00:00Z", "datetime")
    assert i.precision == "instant" and i.debut == i.fin and not i.suppose_utc
    assert dates.lire("2026-09-04 10:00", "datetime").suppose_utc
    assert not dates.lire(1788516000, "datetime").suppose_utc, \
        "un horodatage Unix est en UTC par définition"


def test_la_forme_stockee_se_relit_a_l_identique():
    """Idempotence : deux portes successives (append promu en fusion) ne réécrivent
    rien et ne disent rien de plus."""
    for v in ("2026-09-04T10:00:00Z", "2026-09-04", "2026-09", "2026"):
        assert dates.lire(v, "datetime").forme == v


SCHEMA = {"fields": [
    {"key": "d", "type": "datetime"},
    {"key": "j", "type": "date"},
    {"key": "o", "type": "object", "fields": [{"key": "s", "type": "date"}]},
    {"key": "l", "type": "list", "of": {"fields": [{"key": "q", "type": "datetime"}]}},
    {"key": "ld", "type": "list", "of": {"type": "date"}},
    {"key": "t", "type": "text"},
]}


def test_normaliser_ligne_a_tous_les_niveaux_sans_toucher_les_couches():
    entree = {
        "d": {"valeur": "2026-09-04 10:00", "comment": "relevé", "origine": "brut"},
        "j": "2026-09-04T00:30:00+02:00",
        "o": {"s": "04/09/2026", "x": "libre"},
        "l": [{"q": 1788516000}, {"q": "2026-09-04T10:00:00Z"}],
        "ld": ["2026-09-04T10:00:00Z", "@empty"],
        "t": "2026-09-04 10:00",      # un texte n'est pas une date
        "hors": "2026-09-04 10:00",   # ni une colonne non déclarée
    }
    sortie, notices = dates.normaliser_ligne(SCHEMA, entree)
    assert sortie == {
        "d": {"valeur": "2026-09-04T10:00:00Z", "comment": "relevé", "origine": "brut"},
        "j": "2026-09-04",
        "o": {"s": "2026-09-04", "x": "libre"},
        "l": [{"q": "2026-09-04T10:00:00Z"}, {"q": "2026-09-04T10:00:00Z"}],
        "ld": ["2026-09-04", "@empty"],
        "t": "2026-09-04 10:00",
        "hors": "2026-09-04 10:00",
    }
    assert entree["d"]["valeur"] == "2026-09-04 10:00", "l'entrée n'est pas mutée"
    assert len(notices) == 1 and "`d`" in next(iter(notices))
    assert "UTC" in next(iter(notices))
    # idempotente, et muette au second passage
    assert dates.normaliser_ligne(SCHEMA, sortie) == (sortie, set())


def test_un_sous_champ_illisible_n_est_pas_dit_c_est_le_refus_qui_parle():
    """Le format fait contrat sur tout tableau à colonnes (oto#124) : pas de notice."""
    _, notices = dates.normaliser_ligne(SCHEMA, {"o": {"s": "bientôt"}})
    assert notices == set()


def test_les_bornes_d_un_filtre_couvrent_la_periode():
    assert dates.bornes_du_filtre("2026-09-04", "d") == [
        "2026-09-04T00:00:00Z", "2026-09-05T00:00:00Z"]
    assert dates.bornes_du_filtre("2026", "d") == [
        "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"]
    assert dates.bornes_du_filtre("2026-09-04T12:00+02:00", "d") == [
        "2026-09-04T10:00:00Z", "2026-09-04T10:00:00Z"]
    with pytest.raises(ValueError, match="not a readable"):
        dates.bornes_du_filtre("demain", "d")


def test_la_validation_juge_par_la_meme_lecture():
    from oto_mcp.datastore.validation import _conformite_scalaire
    assert _conformite_scalaire("04/09/2026 10:30", "datetime", "d") == []
    assert _conformite_scalaire(1788516000, "date", "j") == []
    refus = _conformite_scalaire("bientôt", "datetime", "d")
    assert refus and "2026-09-04T10:00:00Z" in refus[0], "le refus donne la forme"


# ── 2. l'écriture, par les trois portes ───────────────────────────────────────

SUB = "sub-dates-859"


def _monte(schema=SCHEMA, lignes=()):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    db.upsert_user(SUB, email=f"{SUB}@exemple.invalid", name=SUB)
    st = make_store(SUB)
    ns_id = db.create_datastore("user", SUB, "d859-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    # L'existant d'avant la normalisation, posé en base tel quel : une colonne non
    # déclarée est refusée à l'écriture (oto#124).
    for r in lignes:
        db.datastore_insert_row(ns_id, uuid.uuid4().hex, r)
    if schema:
        st.set_schema(ns, schema)
    return st, ns


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def test_la_creation_stocke_la_forme_et_dit_l_utc_suppose(live):
    st, ns = _monte()
    st = _store()
    row = st.append_row(ns, {"d": "2026-09-04 10:00", "j": "2026-09-04T12:00:00Z",
                             "o": {"s": "4/9/2026"}})
    assert (row["d"], row["j"], row["o"]) == (
        "2026-09-04T10:00:00Z", "2026-09-04", {"s": "2026-09-04"})
    assert any("`d`" in n and "UTC" in n for n in st.off_schema_report()["notices"])
    relue = st.get_row(ns, row["_id"])
    assert relue["d"] == "2026-09-04T10:00:00Z"


def test_une_date_imprecise_reste_a_sa_precision(live):
    st, ns = _monte()
    row = st.append_row(ns, {"d": "2026-09", "j": "2026"})
    assert (row["d"], row["j"]) == ("2026-09", "2026")


def test_le_lot_et_le_patch_normalisent_aussi(live):
    st, ns = _monte()
    recap = st.write_rows(ns, [{"d": 1788516000000}, {"d": "2026-09-04T12:00+02:00"}])
    lues = [st.get_row(ns, i)["d"] for i in recap["ids"]]
    assert lues == ["2026-09-04T10:00:00Z", "2026-09-04T10:00:00Z"]
    patchee = st.update_row(ns, recap["ids"][0], {"j": "04/09/2026"})
    assert patchee["j"] == "2026-09-04"


def test_reecrire_la_meme_date_sous_une_autre_forme_garde_le_commentaire(live):
    """La normalisation se fait AVANT la fusion : la même date réécrite autrement est
    la même valeur, et `comment` ne tombe pas (`_merge_column`)."""
    st, ns = _monte()
    row = st.append_row(ns, {"d": {"valeur": "2026-09-04T10:00:00Z",
                                   "comment": "relevé sur l'acte"}})
    st.update_row(ns, row["_id"], {"d": "2026-09-04T12:00:00+02:00"})
    brute = _store().get_row(ns, row["_id"], layers="nested")
    assert brute["d"] == {"valeur": "2026-09-04T10:00:00Z",
                          "comment": "relevé sur l'acte"}


def test_l_illisible_est_refuse_au_premier_niveau_meme_sans_validation(live):
    from oto_mcp.datastore.errors import RowValidationError
    st, ns = _monte()
    with pytest.raises(RowValidationError, match="bientôt"):
        st.append_row(ns, {"d": "bientôt"})


def test_un_sous_champ_illisible_est_refuse(live):
    from oto_mcp.datastore.errors import RowValidationError
    st, ns = _monte()
    with pytest.raises(RowValidationError, match="o.s"):
        st.append_row(ns, {"o": {"s": "fin du mois"}})


# ── 3. les filtres comparent des instants ─────────────────────────────────────

LIGNES = {
    "matin": "2026-09-04T10:00:00Z",
    "jour": "2026-09-04",
    "mois": "2026-09",
    "annee": "2026",
    "veille_tard": "2026-09-03T23:59:59Z",
    "lendemain": "2026-09-05T00:00:00Z",
    "decale": "2026-09-05T01:00:00+02:00",   # forme d'avant : le 4 à 23 h UTC
    "texte": "bientôt",                       # d'avant l'armement du type
}


@pytest.fixture(scope="module")
def table(live):
    st, ns = _monte({"fields": [{"key": "d", "type": "datetime"},
                                {"key": "nom", "type": "text"}]},
                    [{"nom": k, "d": v} for k, v in LIGNES.items()] + [{"nom": "vide"}])
    return ns


def _noms(ns, op, valeur):
    page = _store().page_rows(ns, filter={"d": {op: valeur}}, limit=50)
    return sorted(r["nom"] for r in page["rows"])


def test_lte_sur_un_jour_couvre_toute_la_journee(table):
    assert _noms(table, "lte", "2026-09-04") == sorted(
        ["matin", "jour", "mois", "annee", "veille_tard", "decale"])


def test_gte_sur_un_jour_part_de_son_debut(table):
    assert _noms(table, "gte", "2026-09-04") == sorted(
        ["matin", "jour", "lendemain", "decale"])


def test_gt_et_lt_sur_un_jour(table):
    assert _noms(table, "gt", "2026-09-04") == ["lendemain"]
    assert _noms(table, "lt", "2026-09-04") == sorted(["mois", "annee", "veille_tard"])


def test_eq_sur_une_date_imprecise(table):
    assert _noms(table, "eq", "2026-09-04") == sorted(["matin", "jour", "decale"])
    assert _noms(table, "eq", "2026-09") == sorted(
        ["matin", "jour", "mois", "veille_tard", "lendemain", "decale"])
    assert _noms(table, "eq", "2026") == sorted(set(LIGNES) - {"texte"})


def test_eq_sur_un_instant_est_un_point_quelle_que_soit_la_forme(table):
    assert _noms(table, "eq", "2026-09-04T12:00:00+02:00") == ["matin"]
    assert _noms(table, "lte", "2026-09-04T10:00:00Z") == sorted(
        ["matin", "jour", "mois", "annee", "veille_tard"])


def test_ne_et_in(table):
    assert _noms(table, "ne", "2026-09-04") == sorted(
        ["mois", "annee", "veille_tard", "lendemain", "texte", "vide"])
    assert _noms(table, "in", ["2026-09-03", "2026-09-05"]) == sorted(
        ["veille_tard", "lendemain"])


def test_une_borne_illisible_est_refusee(table):
    with pytest.raises(ValueError, match="not a readable"):
        _noms(table, "lte", "demain")


def test_contains_reste_textuel(table):
    assert "mois" in _noms(table, "contains", "2026-09")


# ── 4. le tri lit une date imprécise à son début de période ──────────────────

def test_le_tri_range_les_precisions_par_leur_debut(table):
    page = _store().page_rows(table, order_by="d", order_dir="asc", limit=50)
    noms = [r["nom"] for r in page["rows"]]
    assert noms[:6] == ["annee", "mois", "veille_tard", "jour", "matin", "decale"]
    assert noms[6] == "lendemain" and noms[-2:] == ["texte", "vide"]
    assert page["order_health"] == {"off_type": 1, "empty": 1}
