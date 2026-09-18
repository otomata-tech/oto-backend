"""Colonnes calculées par formule OpenFormula (oto-backend#1008).

Sous-ensemble FERMÉ (IFS/IF/SWITCH/AND/OR/NOT/LEFT/MID/LEN/TRUE/FALSE + comparaisons),
analyseur écrit pour cette grammaire précise — jamais `eval()`, jamais une lib de
formules généraliste. Une formule est une fonction PURE de sa propre ligne : pas
d'agrégat cross-lignes, pas de chaînage formule→formule (refusé à la pose).

⚠️ Données 100% fictives (zones A/B/C plutôt que des départements réels, noms
génériques) — aucune donnée métier réelle, aucun nom de client/personne."""
from __future__ import annotations

import pytest

from oto_mcp.datastore import formule as F
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.controles import ControlesMixin


# ── tokenizer / parseur / évaluateur ────────────────────────────────────────

BAREME = """IFS(
  AND(OR(LEFT(code;4)="AAAA"; LEFT(code;4)="BBBB"); effectif<>""; effectif<>"inconnu"); "1";
  AND(OR(LEFT(code;4)="AAAA"; LEFT(code;4)="BBBB"); rattache="oui"); "2";
  AND(effectif<>""; effectif<>"inconnu"); "3";
  rattache="oui"; "4";
  TRUE(); "5")"""


def test_bareme_cascade_ordonnee_rang_1():
    noeud = F.parse(BAREME)
    assert F.champs_references(noeud) == {"code", "effectif", "rattache"}
    v, p = F.evaluer_avec_provenance(
        noeud, {"code": "AAAA1", "effectif": "12", "rattache": "non"})
    assert v == "1"
    assert p == ('AND(OR(LEFT(code;4)="AAAA";LEFT(code;4)="BBBB");'
                 'effectif<>"";effectif<>"inconnu")')


def test_bareme_rang_3_effectif_seul():
    v, _ = F.evaluer_avec_provenance(
        F.parse(BAREME), {"code": "ZZZZ", "effectif": "5", "rattache": "non"})
    assert v == "3"


def test_bareme_sentinelle_inconnu_nest_pas_effectif_connu():
    """`effectif="inconnu"` NE DOIT PAS compter comme « effectif connu » — c'est
    exactement le bug qu'un simple `<>\"\"` commettrait."""
    v, _ = F.evaluer_avec_provenance(
        F.parse(BAREME), {"code": "ZZZZ", "effectif": "inconnu", "rattache": "non"})
    assert v == "5"


def test_bareme_defaut_true_rang_5():
    v, p = F.evaluer_avec_provenance(
        F.parse(BAREME), {"code": "ZZZZ", "effectif": "", "rattache": "non"})
    assert v == "5"
    assert p == "TRUE()"


# ── SWITCH, provenance, et le bug préfixe 2 vs 3 chiffres (celui d'Audiens) ──

ZONES = """IFS(
  AND(LEN(code)=5; LEFT(code;2)="AA"); SWITCH(code; "AA001";"Alice"; "AA002";"Bob"; "");
  LEN(code)=5; SWITCH(IF(OR(LEFT(code;3)="BB1"; LEFT(code;3)="BB2"); LEFT(code;3); LEFT(code;2)); "BB1";"Carla"; "CC";"Dan"; "");
  TRUE(); "")"""


def test_switch_zone_courte_deux_chiffres():
    v, p = F.evaluer_avec_provenance(F.parse(ZONES), {"code": "CC123"})
    assert v == "Dan"
    assert "'CC'" in p and "'Dan'" in p


def test_switch_zone_prefixe_trois_chiffres_seulement_les_bons():
    """Le patron du bug réel (Monaco/98) : un préfixe à 3 chiffres ne doit
    s'appliquer QU'AUX codes qui le déclarent explicitement — pas à tout ce qui
    partage les 2 premiers chiffres."""
    v, _ = F.evaluer_avec_provenance(F.parse(ZONES), {"code": "BB1XX"})
    assert v == "Carla"
    # "BB9XX" partage le préfixe 2 chiffres "BB" avec "BB1"/"BB2" mais n'a pas de
    # correspondance 3-chiffres déclarée : il doit retomber sur le SWITCH 2-chiffres,
    # ce qui ne matche rien ("BB" seul n'est pas une clé) -> vide.
    v2, _ = F.evaluer_avec_provenance(F.parse(ZONES), {"code": "BB9XX"})
    assert v2 == ""


def test_hors_correspondance_reste_vide_pas_invente():
    v, _ = F.evaluer_avec_provenance(F.parse(ZONES), {"code": "ZZ999"})
    assert v == ""


def test_code_incomplet_tombe_sur_le_defaut():
    v, _ = F.evaluer_avec_provenance(F.parse(ZONES), {"code": "AB1"})
    assert v == ""


# ── refus à l'analyse ────────────────────────────────────────────────────────

def test_fonction_hors_sous_ensemble_refusee_et_nommee():
    with pytest.raises(F.FormulaError, match="VLOOKUP"):
        F.parse("VLOOKUP(a;b;1)")


def test_caractere_inattendu_refuse():
    with pytest.raises(F.FormulaError):
        F.parse("a & b")


def test_arite_ifs_impaire_refusee_a_l_evaluation():
    with pytest.raises(F.FormulaError, match="IFS"):
        F.evaluer_avec_provenance(F.parse('IFS(TRUE();"1";TRUE())'), {})


def test_if_arite_fixe_a_trois():
    with pytest.raises(F.FormulaError, match="3 argument"):
        F.parse('IF(TRUE();"1")')


# ── validation contre un schéma (colonne inconnue, chaînage) ─────────────────

def test_valider_colonne_inconnue_refusee_et_nommee():
    with pytest.raises(F.FormulaError, match="fantome"):
        F.valider('IFS(fantome="x";"1";TRUE();"2")', {"autre"}, set())


def test_valider_chainage_refuse():
    with pytest.raises(F.FormulaError, match="chaînage"):
        F.valider('IFS(autre_calc="x";"1";TRUE();"2")',
                  {"autre_calc"}, {"autre_calc"})


# ── plage sur colonne-liste : contacts[].telephone + COUNTA (oto-backend#1008 v2)

CHAMPS_HAS_TEL = [
    {"key": "entreprise_telephone", "type": "text"},
    {"key": "contacts", "type": "list", "of": {"fields": [
        {"key": "telephone", "type": "text"}, {"key": "nom", "type": "text"}]}},
]

# Encadrée par IFS (pas un simple OR) : c'est la forme réelle — c'est aussi elle
# qui produit une provenance (`evaluer_avec_provenance` n'en dérive une que pour
# un IFS de tête, cf. sa docstring).
HAS_TEL = """IFS(
  entreprise_telephone<>""; TRUE();
  COUNTA(contacts[].telephone)>0; TRUE();
  TRUE(); FALSE())"""


def test_plage_parse_et_champs_references():
    noeud = F.parse(HAS_TEL)
    # `contacts` (la colonne-liste) est référencée comme une colonne normale —
    # le sous-champ `telephone`, lui, n'est PAS un nom de colonne de la ligne.
    assert F.champs_references(noeud) == {"entreprise_telephone", "contacts"}


def test_counta_compte_les_telephones_non_vides_sur_plusieurs_contacts():
    row = {"entreprise_telephone": "", "contacts": [
        {"telephone": "0600000000", "nom": "A"},
        {"telephone": "", "nom": "B"},
        {"telephone": "0700000000", "nom": "C"},
    ]}
    v, p = F.evaluer_avec_provenance(F.parse(HAS_TEL), row)
    assert v is True
    assert "2 valeur(s) trouvée(s)" in p


def test_counta_zero_sur_contacts_vide_ou_sans_telephone():
    for contacts in ([], [{"nom": "A"}], [{"telephone": "", "nom": "A"}]):
        row = {"entreprise_telephone": "", "contacts": contacts}
        v, _ = F.evaluer_avec_provenance(F.parse(HAS_TEL), row)
        assert v is False, contacts


def test_colonne_plate_gagne_sans_regarder_les_contacts():
    row = {"entreprise_telephone": "0600000000", "contacts": []}
    v, p = F.evaluer_avec_provenance(F.parse(HAS_TEL), row)
    assert v is True
    assert "valeur(s) trouvée(s)" not in p  # la branche gagnante ne teste pas COUNTA


def test_recalcul_declenche_par_une_ecriture_qui_ne_touche_que_contacts():
    """`_appliquer_formules` recalcule TOUJOURS toutes les formules sur `merged`
    (cf. `controles.py::_check_row`) — donc un geste qui n'écrit QUE `contacts`
    recalcule déjà `has_telephone` correctement, sans câblage supplémentaire."""
    schema = {"fields": CHAMPS_HAS_TEL + [
        {"key": "has_telephone", "type": "formula", "formula": HAS_TEL}]}
    merged = {"entreprise_telephone": "", "contacts": [{"telephone": "0600000000"}]}
    mixin = ControlesMixin()
    mixin._appliquer_formules(schema, merged)
    assert merged["has_telephone"][dsv2.VALUE_LAYER] is True


def test_valider_plage_ok_contre_le_schema():
    F.valider(HAS_TEL, {"entreprise_telephone", "contacts"}, set(),
              champs_def=CHAMPS_HAS_TEL)


def test_valider_plage_colonne_pas_liste_refusee():
    with pytest.raises(F.FormulaError, match="n'est pas de type `list`"):
        F.valider('COUNTA(entreprise_telephone[].x)>0',
                  {"entreprise_telephone"}, set(), champs_def=CHAMPS_HAS_TEL)


def test_valider_plage_sous_champ_inconnu_refuse():
    with pytest.raises(F.FormulaError, match="sous-champ déclaré"):
        F.valider('COUNTA(contacts[].fax)>0', {"contacts"}, set(),
                  champs_def=CHAMPS_HAS_TEL)


def test_plage_hors_counta_refusee():
    with pytest.raises(F.FormulaError, match="COUNTA"):
        F.valider('contacts[].telephone<>""', {"contacts"}, set(),
                  champs_def=CHAMPS_HAS_TEL)


def test_counta_sur_autre_chose_qu_une_plage_refuse():
    with pytest.raises(F.FormulaError, match="COUNTA"):
        F.valider('COUNTA(entreprise_telephone)>0', {"entreprise_telephone"}, set())


def test_valider_formule_saine_rend_last_node():
    noeud = F.valider('IFS(code="x";"1";TRUE();"2")', {"code"}, set())
    assert isinstance(noeud, F.Appel) and noeud.fonction == "IFS"


# ── re-sérialisation canonique (déterministe) ─────────────────────────────────

def test_serialiser_est_pur_et_deterministe():
    noeud = F.parse(BAREME)
    a = F.serialiser(noeud)
    b = F.serialiser(F.parse(BAREME))
    assert a == b


# ── déclaration de schéma : refus à la pose, readonly implicite ──────────────

def _schema_zones():
    return {"fields": [
        {"key": "code", "type": "text"},
        {"key": "zone", "type": "formula", "formula": ZONES},
    ]}


def test_schema_avec_formule_saine_valide():
    assert dsv2.validate_schema_def(_schema_zones()) == []


def test_schema_formule_refusee_a_la_pose_fonction_inconnue():
    bad = {"fields": [
        {"key": "code", "type": "text"},
        {"key": "zone", "type": "formula", "formula": "VLOOKUP(code;1;2)"},
    ]}
    errs = dsv2.validate_schema_def(bad)
    assert errs and "VLOOKUP" in errs[0]


def test_schema_formule_refusee_a_la_pose_colonne_inconnue():
    bad = {"fields": [
        {"key": "code", "type": "text"},
        {"key": "zone", "type": "formula", "formula": 'IFS(fantome="x";"1";TRUE();"2")'},
    ]}
    errs = dsv2.validate_schema_def(bad)
    assert errs and "fantome" in errs[0]


def test_schema_formule_refusee_a_la_pose_chainage():
    bad = {"fields": [
        {"key": "code", "type": "text"},
        {"key": "a", "type": "formula", "formula": 'IFS(code="x";"1";TRUE();"2")'},
        {"key": "b", "type": "formula", "formula": 'IFS(a="1";"oui";TRUE();"non")'},
    ]}
    errs = dsv2.validate_schema_def(bad)
    assert errs and any("chaînage" in e for e in errs)


def test_schema_formule_sans_texte_refusee():
    bad = {"fields": [{"key": "zone", "type": "formula"}]}
    errs = dsv2.validate_schema_def(bad)
    assert errs and "formula" in errs[0]


def test_colonne_formule_est_readonly_implicite():
    assert dsv2.readonly_fields(_schema_zones()) == {"zone"}


def test_colonne_formule_apparait_dans_les_cles_reconnues():
    from oto_mcp.datastore import schema_keys
    assert "formula" in schema_keys.RECONNUES


# ── recalcul à l'écriture (seam _check_row, via le mixin réel) ───────────────

class _Store(ControlesMixin):
    off_geles: dict = {}
    off_rejected: list = []


def test_appliquer_formules_calcule_et_pose_le_comment():
    store = _Store()
    merged = {"code": "AA001"}
    store._appliquer_formules(_schema_zones(), merged)
    assert merged["zone"]["valeur"] == "Alice"
    assert "comment" in merged["zone"]


def test_appliquer_formules_ne_touche_jamais_la_couche_origine():
    store = _Store()
    merged = {"code": "AA001",
             "zone": {"origine": {"valeur": "VALEUR HISTORIQUE DE LA CLIENTE"}}}
    store._appliquer_formules(_schema_zones(), merged)
    assert merged["zone"]["origine"] == {"valeur": "VALEUR HISTORIQUE DE LA CLIENTE"}
    assert merged["zone"]["valeur"] == "Alice"


def test_appliquer_formules_noop_sans_colonne_formule():
    store = _Store()
    schema_sans_formule = {"fields": [{"key": "code", "type": "text"}]}
    merged = {"code": "AA001"}
    store._appliquer_formules(schema_sans_formule, merged)
    assert merged == {"code": "AA001"}


def test_appliquer_formules_recalcule_quand_une_entree_change():
    """La propriété demandée : changer une colonne d'entrée fait recalculer la
    formule qui en dépend, sur la MÊME écriture."""
    store = _Store()
    merged = {"code": "AA001"}
    store._appliquer_formules(_schema_zones(), merged)
    assert merged["zone"]["valeur"] == "Alice"
    merged["code"] = "AA002"
    store._appliquer_formules(_schema_zones(), merged)
    assert merged["zone"]["valeur"] == "Bob"


# ── backfill à la pose/modification de la formule, sur vrai PostgreSQL ───────
# Complète #1010 : poser ou modifier une formule doit recalculer TOUTES les
# lignes existantes — pas seulement les écritures qui suivent. Vrai Postgres,
# jamais un double : ce qu'on vérifie ici, c'est ce que le STORE persiste.

import uuid


def _store_1008():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test-1008")


def _donnees_1008(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _poser_origine_brute(ns_id: int, row_id: str, champ: str, valeur: str) -> None:
    """Plante une couche `origine` directement en base, AVANT que la colonne ne
    devienne une formule (une fois `type: formula`, la colonne est readonly —
    on ne peut plus y écrire par le chemin normal).

    ⚠️ `jsonb_set` ne crée QUE le dernier élément manquant d'un chemin, jamais
    les intermédiaires (doc Postgres) : `jsonb_set(data, '{zone,origine}', …)`
    est un NO-OP SILENCIEUX quand `zone` n'existe pas encore. On fusionne donc
    au niveau du champ (`||`), en partant de son objet existant ou de `{}`."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "UPDATE datastore_rows SET data = data || jsonb_build_object("
            "  %s::text, COALESCE(data->%s::text, '{}'::jsonb) "
            "    || jsonb_build_object('origine', %s::jsonb)"
            ") WHERE ns_id = %s::bigint AND row_id = %s::text",
            (champ, champ, f'{{"valeur": "{valeur}"}}', ns_id, row_id))


def test_poser_une_formule_recalcule_les_lignes_existantes(live):
    ns = "t-" + uuid.uuid4().hex[:6]
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test-1008", ns)
    st = _store_1008()
    st.set_schema(ns, {"fields": [{"key": "code", "type": "text"}]})
    r1 = st.append_row(ns, {"code": "AA001"})
    r2 = st.append_row(ns, {"code": "AA002"})

    pose = st.set_schema(ns, _schema_zones())

    assert pose.get("formules_recalculees") == 2
    d1 = _donnees_1008(ns_id, r1["_id"])
    d2 = _donnees_1008(ns_id, r2["_id"])
    assert d1["zone"]["valeur"] == "Alice"
    assert "comment" in d1["zone"]
    assert d2["zone"]["valeur"] == "Bob"


def test_backfill_ne_touche_jamais_la_couche_origine(live):
    ns = "t-" + uuid.uuid4().hex[:6]
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test-1008", ns)
    st = _store_1008()
    st.set_schema(ns, {"fields": [
        {"key": "code", "type": "text"}, {"key": "zone", "type": "text"}]})
    row = st.append_row(ns, {"code": "AA001"})
    _poser_origine_brute(ns_id, row["_id"], "zone", "VALEUR HISTORIQUE DE LA CLIENTE")

    st.set_schema(ns, _schema_zones())

    d = _donnees_1008(ns_id, row["_id"])
    assert d["zone"]["origine"] == {"valeur": "VALEUR HISTORIQUE DE LA CLIENTE"}
    assert d["zone"]["valeur"] == "Alice"


def test_modifier_le_texte_de_la_formule_recalcule_a_nouveau(live):
    ns = "t-" + uuid.uuid4().hex[:6]
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test-1008", ns)
    st = _store_1008()
    st.set_schema(ns, _schema_zones())
    row = st.append_row(ns, {"code": "AA001"})
    assert _donnees_1008(ns_id, row["_id"])["zone"]["valeur"] == "Alice"

    autre = {"fields": [
        {"key": "code", "type": "text"},
        {"key": "zone", "type": "formula",
         "formula": 'IFS(TRUE(); "toujours-pareil")'}]}
    pose = st.set_schema(ns, autre)

    assert pose.get("formules_recalculees") == 1
    assert _donnees_1008(ns_id, row["_id"])["zone"]["valeur"] == "toujours-pareil"


def test_reposer_la_meme_formule_ne_recalcule_rien(live):
    ns = "t-" + uuid.uuid4().hex[:6]
    from oto_mcp import db
    db.create_datastore("user", "sub-test-1008", ns)
    st = _store_1008()
    st.set_schema(ns, _schema_zones())
    st.append_row(ns, {"code": "AA001"})

    pose = st.set_schema(ns, _schema_zones())

    assert "formules_recalculees" not in pose
