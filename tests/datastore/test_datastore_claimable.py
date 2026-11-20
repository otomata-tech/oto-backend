"""Un filtre de réservation DÉCLARÉ sur le tableau — `lifecycle.claimable` (#517).

Le fait : sans `filter`, `data_claim_next` sert « la plus ancienne ligne dont le bail
est libre ou expiré » — TOUTE ligne du tableau. Sur un fichier de 8 910 lignes, un jalon
en cible 100 (`lot_test = jalon-100`) ; le harnais dicte le filtre dans la prose de
l'ordre et l'agent le recopie. À 5 % d'oubli, cinq fiches hors lot par jalon — servies,
traitées, écrites, payées. Une contrainte demandée par la prose n'est pas une contrainte.

Ce que ce banc fige, contre un PostgreSQL RÉEL (ce qui est en cause est la clause d'un
pick `FOR UPDATE SKIP LOCKED` et celle d'un UPDATE conditionnel — un magasin reconstitué
mesurerait la représentation qu'on s'en fait) :

- une ligne hors périmètre n'est JAMAIS servie, même sans filtre ;
- le filtre de l'appelant s'ajoute en ET : il resserre, il n'élargit pas — un filtre
  contradictoire ne sert rien, et la réponse NOMME le périmètre ;
- la réservation ciblée (REST `claim_row`) refuse une ligne hors périmètre, en le
  nommant, AVANT de regarder le bail — sinon la porte de côté ;
- `enforced` l'annonce ; `data_patch_schema` le pose et le lève par fusion du
  `lifecycle`, sans toucher `max_claims` ;
- la déclaration se valide à la pose, par le moteur de filtre qui la servira.
"""
from __future__ import annotations

import uuid

import pytest


PERIMETRE = {"lot_test": "jalon-100", "statut": "a_enrichir"}
ETATS = ["a_enrichir", "enrichi", "echec"]


def _schema(claimable=None, **lifecycle) -> dict:
    lc = {"states": list(ETATS),
          "transitions": {"a_enrichir": ["enrichi", "echec"], "echec": ["a_enrichir"]},
          "terminal": ["enrichi", "echec"]}
    lc.update(lifecycle)
    if claimable is not None:
        lc["claimable"] = claimable
    return {"fields": [
        {"key": "societe", "type": "text"},
        {"key": "lot_test", "type": "text"},
        {"key": "statut", "type": "enum", "role": "status",
         "options": list(ETATS), "lifecycle": lc},
    ]}


# Trois lignes, dans l'ordre de création (= l'ordre du pick) : la plus ANCIENNE est
# hors lot, exprès — sans périmètre, c'est elle que la file sert en premier.
LIGNES = (("HORS-LOT", "jalon-200", "a_enrichir"),
          ("DEJA-FAITE", "jalon-100", "enrichi"),
          ("CIBLE", "jalon-100", "a_enrichir"))


def _store(sub="sub-agent"):
    from oto_mcp.datastore.core import make_store
    return make_store(sub)


def _table(schema, lignes=LIGNES) -> tuple:
    """Un tableau neuf → `(store, datastore, ns_id, {societe: _id})`."""
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-agent", "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, schema)
    ids = {}
    for societe, lot, statut in lignes:
        row = st.append_row(ns, {"societe": societe, "lot_test": lot, "statut": statut})
        ids[societe] = row["_id"]
    return st, ns, ns_id, ids


def _bail(ns_id: int, row_id: str):
    """Ce que porte la BASE : le titulaire du bail, ou None."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT claimed_by FROM datastore_rows WHERE ns_id = %s "
                         "AND row_id = %s", (ns_id, row_id)).fetchone()
    return r["claimed_by"]


# ══ le fait, puis la garde ═════════════════════════════════════════════════════

def test_sans_declaration_la_file_sert_toute_ligne_libre(live):
    """Le témoin du défaut : sans périmètre ni filtre, la plus ancienne ligne libre
    part — et c'est celle hors lot. Comportement historique, inchangé."""
    st, ns, _, ids = _table(_schema())
    assert st.claim_next(ns, worker="w-1")["_id"] == ids["HORS-LOT"]


def test_une_ligne_hors_perimetre_n_est_jamais_servie_sans_filtre(live):
    st, ns, ns_id, ids = _table(_schema(PERIMETRE))

    servie = st.claim_next(ns, worker="w-1")
    assert servie["_id"] == ids["CIBLE"]
    assert st.claim_next(ns, worker="w-2") is None      # les deux autres : jamais
    assert _bail(ns_id, ids["HORS-LOT"]) is None
    assert _bail(ns_id, ids["DEJA-FAITE"]) is None


def test_le_filtre_de_l_appelant_s_ajoute_en_ET(live):
    """Le périmètre ne cible que le lot ; le filtre de l'appelant choisit dedans."""
    st, ns, _, ids = _table(_schema({"lot_test": "jalon-100"}))

    assert st.claim_next(ns, worker="w-1",
                         filter={"statut": "enrichi"})["_id"] == ids["DEJA-FAITE"]
    assert st.claim_next(ns, worker="w-2",
                         filter={"statut": "a_enrichir"})["_id"] == ids["CIBLE"]


def test_un_filtre_contradictoire_n_elargit_pas_et_le_perimetre_est_nomme(live):
    """L'ordre dit « jalon-200 » sur un tableau déclaré « jalon-100 » : rien n'est
    servi — et la réponse dit POURQUOI, sinon l'agent lit « file vide »."""
    from oto_mcp.tools import datastore as T
    from oto_mcp.capabilities.datastore import claim as C

    st, ns, ns_id, ids = _table(_schema(PERIMETRE))
    perimetre: dict = {}
    assert st.claim_next(ns, worker="w-1", filter={"lot_test": "jalon-200"},
                         perimetre=perimetre) is None
    assert perimetre == PERIMETRE
    assert _bail(ns_id, ids["HORS-LOT"]) is None

    mcp = T._hint_file_vide(perimetre, {"lot_test": "jalon-200"})
    rest = C._hint_vide(perimetre, {"lot_test": "jalon-200"})
    for phrase in (mcp, rest):
        assert "declared scope `{lot_test: jalon-100, statut: a_enrichir}`" in phrase
        assert "`{lot_test: jalon-200}`" in phrase and "ANDed" in phrase
    # La suite MCP ne change pas : l'agent ne tient rien, il n'écrit rien — et on ne
    # lui souffle aucun geste d'écriture, pas même le nom d'un raccourci (#517, 29/08 :
    # « ou écris avec un identifiant explicite » l'avait fait en inventer un).
    assert "run_finish" in mcp and "invent no identifier" in mcp
    # Sans périmètre, les deux faces gardent leur phrase historique.
    assert T._hint_file_vide({}, None) == T._HINT_FILE_VIDE
    assert "scope" not in C._hint_vide({}, None)


def test_le_perimetre_n_ouvre_pas_ce_que_l_abandon_a_ferme(live):
    """Les deux filets se cumulent : une ligne abandonnée par le plafond (#433) reste
    hors file même si elle est DANS le périmètre."""
    st, ns, _, ids = _table(_schema(PERIMETRE, max_claims=1, abandon_state="echec"))
    assert st.claim_next(ns, worker="w-1")["_id"] == ids["CIBLE"]
    assert st.release_claim(ns, ids["CIBLE"], worker="w-1")["released"] is True
    assert st.claim_next(ns, worker="w-2") is None   # abandonnée après 1 réservation


# ══ la réservation ciblée : pas de porte de côté ═════════════════════════════

def test_claim_row_hors_perimetre_est_refuse_en_nommant_le_perimetre(live):
    from oto_mcp.datastore.core import RowOutsideClaimable

    st, ns, ns_id, ids = _table(_schema(PERIMETRE))
    with pytest.raises(RowOutsideClaimable) as e:
        st.claim_row(ns, ids["HORS-LOT"], worker="sarah")
    assert "`{lot_test: jalon-100, statut: a_enrichir}`" in str(e.value)
    assert "data_patch_schema" in str(e.value)          # le geste qui élargit
    assert e.value.perimetre == PERIMETRE
    assert _bail(ns_id, ids["HORS-LOT"]) is None          # rien n'a été posé

    # Dans le périmètre : la réservation ciblée fonctionne comme avant.
    assert st.claim_row(ns, ids["CIBLE"], worker="sarah")["_claimed_by"] == "sarah"


def test_hors_perimetre_se_juge_AVANT_le_bail(live):
    """Une ligne hors périmètre tenue par un autre est refusée comme hors périmètre,
    pas comme prise : la première réponse s'instruit (le schéma), la seconde
    s'attend (un collègue) — et le titulaire lui-même ne la renouvelle plus."""
    from oto_mcp.datastore.core import RowClaimed, RowOutsideClaimable

    st, ns, _, ids = _table(_schema())
    assert st.claim_row(ns, ids["HORS-LOT"], worker="jules")["_claimed_by"] == "jules"
    st.patch_schema(ns, fields=[{"key": "statut", "lifecycle": {"claimable": PERIMETRE}}])

    with pytest.raises(RowOutsideClaimable):
        st.claim_row(ns, ids["HORS-LOT"], worker="sarah")
    with pytest.raises(RowOutsideClaimable):
        st.claim_row(ns, ids["HORS-LOT"], worker="jules")   # le titulaire aussi
    # Le bail, lui, reste ce qu'il est : une ligne dans le périmètre prise par un
    # autre se dit toujours « prise ».
    assert st.claim_row(ns, ids["CIBLE"], worker="jules")["_claimed_by"] == "jules"
    with pytest.raises(RowClaimed):
        st.claim_row(ns, ids["CIBLE"], worker="sarah")


def test_la_face_REST_rend_409_row_outside_claimable(monkeypatch):
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    from oto_mcp.capabilities.datastore import claim as dsc
    from oto_mcp.datastore.core import RowOutsideClaimable

    class _Store:
        def claim_row(self, datastore, row_id, **k):
            raise RowOutsideClaimable(row_id, PERIMETRE)

    monkeypatch.setattr(dsc, "make_store", lambda sub: _Store())
    with pytest.raises(AuthzDenied) as e:
        dsc._claim_row(ResolvedCtx(sub="u-1"),
                       dsc.ClaimRowInput(datastore="vivier", row_id="r1", worker="sarah"))
    assert (e.value.status, e.value.code) == (409, "row_outside_claimable")
    assert "jalon-100" in e.value.message
    assert e.value.details == {"claimable": PERIMETRE}


# ══ `enforced`, et le patch qui pose et lève ═════════════════════════════════

def test_enforced_annonce_claimable(live):
    from oto_mcp.datastore import schema as S

    S.reset_enforced_keys()
    try:
        assert "claimable" in S.enforced_keys()
        st, ns, _, _ = _table(_schema())
        assert "claimable" in st.set_schema(ns, _schema(PERIMETRE))["enforced"]
    finally:
        S.reset_enforced_keys()


def test_le_patch_pose_et_leve_le_perimetre_sans_toucher_max_claims(live):
    from oto_mcp.datastore import schema as S

    st, ns, _, ids = _table(_schema(max_claims=3, abandon_state="echec"))

    pose = st.patch_schema(ns, fields=[{"key": "statut",
                                       "lifecycle": {"claimable": PERIMETRE}}])
    lc = S.lifecycle_of(pose["schema"])
    assert lc["claimable"] == PERIMETRE
    assert (lc["max_claims"], lc["abandon_state"]) == (3, "echec")
    assert lc["states"] == ETATS and lc["terminal"] == ["enrichi", "echec"]
    assert "declarations_effacees" not in pose             # rien n'a disparu
    assert st.claim_next(ns, worker="w-1")["_id"] == ids["CIBLE"]

    leve = st.patch_schema(ns, fields=[{"key": "statut",
                                       "lifecycle": {"claimable": None}}])
    lc = S.lifecycle_of(leve["schema"])
    assert "claimable" not in lc
    assert (lc["max_claims"], lc["abandon_state"]) == (3, "echec")
    assert S.claimable_of(leve["schema"]) is None
    assert st.claim_next(ns, worker="w-2")["_id"] == ids["HORS-LOT"]   # rouvert


def test_la_fusion_du_lifecycle_est_une_fusion_par_cle():
    """Jusqu'au 29/08/2026, nommer `lifecycle` dans un patch le REMPLAÇAIT en bloc :
    poser un périmètre obligeait à recopier tout le cycle, et en oublier une clé la
    faisait disparaître — la promesse inverse du patch."""
    from oto_mcp.datastore import schema as S

    avant = [{"key": "s", "lifecycle": {"states": ["a", "b"], "terminal": ["b"],
                                        "max_claims": 2, "abandon_state": "b"}}]
    apres, _, _ = S.merge_fields(avant, [{"key": "s", "lifecycle": {"claimable": {"x": 1}}}])
    assert apres[0]["lifecycle"] == {"states": ["a", "b"], "terminal": ["b"],
                                     "max_claims": 2, "abandon_state": "b",
                                     "claimable": {"x": 1}}
    assert avant[0]["lifecycle"].get("claimable") is None       # la source est intacte
    # `null` lève une clé ; les autres propriétés du champ suivent l'ancien régime.
    apres, _, _ = S.merge_fields(apres, [{"key": "s", "label": "Statut",
                                          "lifecycle": {"max_claims": None,
                                                        "abandon_state": None}}])
    assert apres[0]["lifecycle"] == {"states": ["a", "b"], "terminal": ["b"],
                                     "claimable": {"x": 1}}
    assert apres[0]["label"] == "Statut"


# ══ la déclaration se valide à la pose, par le moteur qui la servira ═════════

def _erreurs(claimable, **kw) -> list:
    from oto_mcp.datastore.schema import validate_schema_def
    return validate_schema_def(_schema(claimable, **kw))


def test_une_declaration_valide_passe_dans_les_deux_formes():
    assert _erreurs(PERIMETRE) == []
    assert _erreurs({"lot_test": {"in": ["jalon-100", "jalon-101"]},
                     "_updated_at": {"lt": "2026-09-01"}}) == []


def test_un_operateur_inconnu_est_refuse_en_nommant_les_operateurs():
    erreurs = _erreurs({"lot_test": {"matches": "jalon-*"}})
    assert len(erreurs) == 1 and "lifecycle.claimable" in erreurs[0]
    assert "`matches`" in erreurs[0] and "eq" in erreurs[0] and "in" in erreurs[0]


def test_une_colonne_non_declaree_est_refusee_a_la_pose():
    """oto#124 : la garde de pose de la validation complète."""
    erreurs = _erreurs({"lot": "jalon-100"})
    assert any("`lot`" in e and "is binding" in e for e in erreurs)


@pytest.mark.parametrize("valeur", [{}, "jalon-100", ["jalon-100"], 3])
def test_une_forme_qui_n_est_pas_un_filtre_est_refusee(valeur):
    erreurs = _erreurs(valeur)
    assert len(erreurs) == 1 and "non-empty object" in erreurs[0]


def test_une_clause_inerte_est_refusee():
    """⚠️ Le refus vient d'ailleurs depuis oto-backend#353, et il dit plus.

    Cette clause était détectée ICI, en comptant les fragments rendus : une clause qui
    « s'évaporait » en laissait un de moins, et le message parlait d'une clause
    « INERTE » sans pouvoir dire laquelle. Depuis que le filtre `in` vide LÈVE au lieu
    de disparaître — c'est le lot #353, le même défaut qui avait moissonné un tableau
    entier côté lecture — le refus remonte de la couche de requête et **nomme la
    colonne** ainsi que la sortie pour qui visait les lignes sans valeur.

    Ce qui est protégé ici n'a pas changé : poser un périmètre de réservation avec une
    clause qui ne restreint rien est refusé, et l'appelant sait quoi corriger."""
    erreurs = _erreurs({"lot_test": {"in": []}})
    assert len(erreurs) == 1, erreurs
    assert "lot_test" in erreurs[0], "le refus doit NOMMER la clause fautive"
    assert "empty" in erreurs[0], "…et la sortie pour viser les lignes sans valeur"


def test_un_etat_que_le_cycle_ne_declare_pas_est_refuse():
    """La file serait vide pour toujours, sans un mot."""
    erreurs = _erreurs({"statut": "a_traiter"})
    assert any("'a_traiter'" in e and "a_enrichir" in e for e in erreurs)
    assert _erreurs({"statut": {"in": ["a_enrichir", "enrichi"]}}) == []


def test_un_perimetre_a_null_est_une_absence():
    from oto_mcp.datastore import schema as S
    assert _erreurs(None) == []
    assert S.claimable_of(_schema(None)) is None
    assert S.claimable_of(_schema(PERIMETRE)) == PERIMETRE


# ── relogés du banc du comptage réservable, supprimé le 13/09/2026 ────────────
# Trois garanties qui ne parlaient pas de compter : elles gardent la réservation
# elle-même, et elles EXÉCUTENT le code — aucune ne lit un source.

def test_le_perimetre_de_la_reservation_porte_ses_composantes():
    """Le tableau visé, le filet de PLATEFORME (`abandon_reason IS NULL`, indépendant du
    filtre du client), les baux actifs, et le filtre de l'appelant — chaque marqueur
    avec sa valeur."""
    from oto_mcp.db import rowlock
    where, params = rowlock._perimetre_reclamable(
        7, [{"field": "statut", "op": "eq", "value": "a_faire"}])
    assert "ns_id = %s" in where and params[0] == 7
    assert "abandon_reason IS NULL" in where
    assert "claimed_until IS NULL OR claimed_until < NOW()" in where
    assert "a_faire" in params
    assert where.count("%s") == len(params), "un marqueur sans valeur, ou l'inverse"


def test_un_perimetre_illisible_accuse_le_SCHEMA_et_dit_de_ne_pas_reessayer():
    """⚠️ Incident du 09/09/2026 : un tableau portait `claimable: ["a_traiter"]`, une
    liste là où la grammaire attend `{col: val}`. La réservation levait un
    `AttributeError` nu, et dix agents ont conclu que leur APPEL était fautif : 60 refus
    sur 81 appels, jusqu'au plafond de tours, sans une écriture.

    Un refus qui décrit la forme attendue d'un paramètre fait chercher la faute chez qui
    appelle. Ici elle est dans une déclaration qu'il ne contrôle pas : c'est la PHRASE
    qu'on éprouve — où est la faute, qui la corrige, et ne pas insister."""
    from oto_mcp.datastore import claimable
    with pytest.raises(ValueError) as exc:
        claimable.clauses(["a_traiter"])
    msg = str(exc.value)
    assert "SCHEMA" in msg, "le refus doit nommer le coupable, pas la forme attendue"
    assert "Your call has nothing to do with it" in msg, "sans ça, l'agent varie sa formulation"
    assert "data_patch_schema" in msg, "le refus doit dire par quel geste on corrige"


def test_un_perimetre_bien_forme_se_traduit_en_clause_et_son_absence_en_rien():
    """Traduit par le moteur de filtre que le pick emploie, jusqu'à la valeur liée."""
    from oto_mcp.datastore import claimable
    from oto_mcp.db.query import _ds_filter_clauses
    clauses = claimable.clauses({"statut": "a_traiter"})
    sql, params = _ds_filter_clauses(clauses)
    assert sql and "a_traiter" in params
    assert claimable.clauses(None) == []


def test_la_pose_refuse_sur_le_tableau_reel(live):
    st, ns, _, _ = _table(_schema())
    with pytest.raises(ValueError) as e:
        st.set_schema(ns, _schema({"statut": {"regex": "a_.*"}}))
    assert "lifecycle.claimable" in str(e.value) and "`regex`" in str(e.value)
    with pytest.raises(ValueError) as e:
        st.patch_schema(ns, fields=[{"key": "statut", "lifecycle": {"claimable": {}}}])
    assert "non-empty object" in str(e.value)
