"""Ce que l'ordonnanceur compte d'une file, exercé en SQL RÉEL contre la réservation.

Le compte des lignes réservables décide quelles campagnes sont servies, et avec quel
poids. Un comptage de ce genre a déjà existé (09/09 → 13/09/2026) : il recomposait le
périmètre de `claim_next` à sa façon et levait sur tout tableau qui déclare un plafond
de reprises. D'où la mesure qui compte ici, et qui ne dépend d'aucune intention :

    **le compte d'une campagne est EXACTEMENT le nombre de lignes que `claim_next`
    lui sert, sous le même filtre, avant de rendre None** —

éprouvée sur des états de table tirés au hasard (baux actifs et échus, reprises au
plafond, abandons, périmètre déclaré, filtre du passage), et par le vrai store — celui
que l'agent appelle —, pas par la requête qu'on croit équivalente.

Patron de base éphémère repris de `test_campagne_a_servir_db.py`.
"""
from __future__ import annotations

import logging
import os
import random
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_reservables_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(autouse=True)
def _compteur_neuf(monkeypatch):
    """Le compteur garde ses comptes et ses signalements PAR PROCESSUS, indexés par
    identifiant de campagne. Deux bases de test en réutilisent les numéros : sans cette
    remise à neuf, un banc lirait le compte ou le silence laissé par un autre module du
    même processus (série comme xdist)."""
    from oto_mcp.capabilities import _lignes_reservables
    monkeypatch.setattr(_lignes_reservables, "_CACHE", {})
    monkeypatch.setattr(_lignes_reservables, "_SIGNALE", {})


ETATS = ["a_enrichir", "enrichi", "echec"]


def _schema(**lifecycle) -> dict:
    lc = {"states": list(ETATS),
          "transitions": {"a_enrichir": ["enrichi", "echec"], "echec": ["a_enrichir"]},
          "terminal": ["enrichi", "echec"], "claimable": {"statut": "a_enrichir"}}
    lc.update(lifecycle)
    return {"fields": [
        {"key": "societe", "type": "text"},
        {"key": "lot", "type": "text"},
        {"key": "passe", "type": "text"},
        {"key": "statut", "type": "enum", "role": "status", "options": list(ETATS),
         "lifecycle": lc},
    ]}


@pytest.fixture
def declarant(live):
    """Qui déclare les campagnes : un compte et son org, uniques (base module-scope)."""
    from oto_mcp import db, org_store
    sub = "declarant_" + uuid.uuid4().hex[:8]
    db.upsert_user(sub)
    return {"sub": sub, "org": org_store.create_org("org_" + uuid.uuid4().hex[:8],
                                                     created_by=sub)}


def _table(declarant, schema, lignes) -> tuple:
    """Un tableau neuf du déclarant → `(store, adresse, ns_id, [_id])`."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", declarant["sub"], "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store(declarant["sub"])
    st.set_schema(ns, schema)
    ids = [st.append_row(ns, dict(ligne))["_id"] for ligne in lignes]
    return st, ns, ns_id, ids


def _campagne(declarant, ns, row_filter) -> dict:
    from oto_mcp import db
    return db.create_fleet(declarant["org"], declarant["sub"], label="essai",
                           procedure="p", tools=["data_claim_next"], namespace=ns,
                           row_filter=row_filter)


def _sql(requete: str, params: tuple) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(requete, params)


def _compter(campagnes, instant: float = 1000.0) -> dict:
    from oto_mcp.capabilities._lignes_reservables import lignes_reservables
    return lignes_reservables(campagnes, horloge=lambda: instant)


def _servies(st, ns, row_filter) -> int:
    """Ce que la file sert VRAIMENT : réserver jusqu'à ce qu'elle rende None."""
    n = 0
    while st.claim_next(ns, worker=f"w-{n}", filter=row_filter) is not None:
        n += 1
    return n


def _ligne(lot="L", passe="2", statut="a_enrichir", societe="s") -> dict:
    return {"societe": societe, "lot": lot, "passe": passe, "statut": statut}


# ══ le compte suit la file ═════════════════════════════════════════════════════

def test_une_file_vide_puis_remplie_se_compte_zero_puis_N_apres_la_fenetre(declarant):
    """0 → N : sautée tant que la file est vide, comptée dès que la fenêtre expire."""
    from oto_mcp.capabilities._lignes_reservables import TTL_S
    st, ns, _, _ = _table(declarant, _schema(), [_ligne(passe="1")])
    f = _campagne(declarant, ns, {"lot": "L", "passe": "2"})

    assert _compter([f], 1000.0) == {f["id"]: 0}

    for i in range(3):
        st.append_row(ns, _ligne(societe=f"neuve-{i}"))
    assert _compter([f], 1000.0 + TTL_S / 2) == {f["id"]: 0}, (
        "dans la fenêtre, le compte gardé sert — c'est la péremption assumée")
    assert _compter([f], 1000.0 + TTL_S + 1) == {f["id"]: 3}


def test_bail_actif_abandon_et_plafond_atteint_sortent_du_compte(declarant):
    """Les trois façons de ne plus être servie, dont celle que la réservation produit
    elle-même : une ligne libre à `claims ≥ plafond` est abandonnée AVANT le pick."""
    st, ns, ns_id, ids = _table(declarant, _schema(max_claims=3, abandon_state="echec"),
                                [_ligne(societe=s) for s in ("bail", "abandon", "plafond", "libre")])
    _sql("UPDATE datastore_rows SET claimed_by = 'autre', claimed_until = NOW() + "
         "interval '10 minutes' WHERE ns_id = %s AND row_id = %s", (ns_id, ids[0]))
    _sql("UPDATE datastore_rows SET abandon_reason = 'essai' WHERE ns_id = %s AND "
         "row_id = %s", (ns_id, ids[1]))
    _sql("UPDATE datastore_rows SET claims = 3 WHERE ns_id = %s AND row_id = %s",
         (ns_id, ids[2]))
    rf = {"lot": "L", "passe": "2"}
    f = _campagne(declarant, ns, rf)

    assert _compter([f]) == {f["id"]: 1}
    assert _servies(st, ns, rf) == 1


def test_le_perimetre_du_tableau_et_le_filtre_du_passage_se_croisent(declarant):
    lignes = [_ligne(statut="enrichi"), _ligne(lot="M"), _ligne(passe="3"),
              _ligne(societe="a"), _ligne(societe="b")]
    st, ns, _, _ = _table(declarant, _schema(), lignes)
    rf = {"lot": "L", "passe": "2"}
    f = _campagne(declarant, ns, rf)

    assert _compter([f]) == {f["id"]: 2}
    assert _servies(st, ns, rf) == 2


@pytest.mark.parametrize("graine", range(8))
def test_le_compte_egale_ce_que_la_file_sert_sur_des_etats_tires_au_hasard(declarant, graine):
    """L'invariant, sur des états que personne n'a choisis."""
    for rf in ({"lot": "L", "passe": "2"}, {"lot": "M"}, None):
        rnd = random.Random(graine)           # la MÊME table pour chaque filtre
        plafonne = rnd.random() < 0.5
        schema = _schema(max_claims=2, abandon_state="echec") if plafonne else _schema()
        lignes = [_ligne(lot=rnd.choice("LM"), passe=rnd.choice("12"),
                         statut=rnd.choice(ETATS), societe=f"s{i}") for i in range(25)]
        st, ns, ns_id, ids = _table(declarant, schema, lignes)
        for row_id in ids:
            tirage = rnd.random()
            if tirage < 0.15:
                bail = "NOW() + interval '10 minutes'"
            elif tirage < 0.3:
                bail = "NOW() - interval '1 minute'"
            else:
                bail = "NULL"
            _sql(f"UPDATE datastore_rows SET claimed_by = CASE WHEN {bail} IS NULL THEN "
                 f"NULL ELSE 'autre' END, claimed_until = {bail}, claims = %s, "
                 "abandon_reason = %s WHERE ns_id = %s AND row_id = %s",
                 (rnd.choice([0, 0, 1, 2, 3]),
                  "essai" if rnd.random() < 0.08 else None, ns_id, row_id))
        f = _campagne(declarant, ns, rf)

        compte = _compter([f])[f["id"]]

        assert compte == _servies(st, ns, rf), (
            f"graine {graine}, filtre {rf}, plafond {plafonne} : le compte doit être "
            "exactement ce que la file sert")


# ══ de bout en bout : la campagne servie ou non ════════════════════════════════

def test_une_campagne_a_file_vide_n_est_pas_servie_et_l_est_des_qu_une_ligne_arrive(declarant):
    """Le défaut de départ : une campagne à file vide fabriquait des travaux à vide."""
    import functools

    from oto_mcp import db
    from oto_mcp.capabilities import _lignes_reservables, _ordre_de_service
    st, ns, _, _ = _table(declarant, _schema(), [_ligne(passe="1")])
    f = _campagne(declarant, ns, {"lot": "L", "passe": "2"})
    db.armer(f["id"], declarant["org"])

    def ordonner_a(instant):
        compter = functools.partial(_lignes_reservables.lignes_reservables,
                                    horloge=lambda: instant)
        return functools.partial(_ordre_de_service.ordonner, compter=compter)

    assert db.campagne_a_servir(declarant["org"], ordonner_a(9000.0)) is None

    st.append_row(ns, _ligne())
    servie = db.campagne_a_servir(declarant["org"],
                                  ordonner_a(9000.0 + _lignes_reservables.TTL_S + 1))

    assert servie is not None and servie["id"] == f["id"]


def test_le_superviseur_lit_la_vue_de_l_ordonnanceur_et_pourquoi_elle_manque(declarant):
    """Décision du 14/09/2026 : `oto_fleet op=state` rend ce compte à qui supervise."""
    from oto_mcp.capabilities._lignes_reservables import pour_le_superviseur
    _, ns, _, _ = _table(declarant, _schema(), [_ligne(), _ligne(), _ligne(passe="1")])
    avec = _campagne(declarant, ns, {"lot": "L", "passe": "2"})
    sans = _campagne(declarant, None, None)
    introuvable = _campagne(declarant, "aucun-" + uuid.uuid4().hex[:6], None)

    assert pour_le_superviseur(avec) == {"reservable_rows": 2,
                                         "reservable_rows_unavailable": None}
    assert pour_le_superviseur(sans) == {"reservable_rows": None,
                                         "reservable_rows_unavailable": "no_table"}
    assert pour_le_superviseur(introuvable) == {"reservable_rows": None,
                                                "reservable_rows_unavailable": "count_failed"}


# ══ un scan par tableau, et ce qui ne se compte pas ════════════════════════════

def test_deux_campagnes_du_meme_tableau_partagent_un_seul_scan(declarant, monkeypatch):
    from oto_mcp import db
    st, ns, _, _ = _table(declarant, _schema(),
                          [_ligne(), _ligne(passe="1"), _ligne(passe="1")])
    deux, un = _campagne(declarant, ns, {"passe": "2"}), _campagne(declarant, ns, {"passe": "1"})
    appels = []
    vrai = db.datastore_compter_reservables

    def espion(ns_id, perimetres):
        appels.append(sorted(perimetres))
        return vrai(ns_id, perimetres)

    monkeypatch.setattr(db, "datastore_compter_reservables", espion)

    assert _compter([deux, un]) == {deux["id"]: 1, un["id"]: 2}
    assert appels == [sorted([deux["id"], un["id"]])]


def test_une_campagne_sans_tableau_se_dit_sans_objet(declarant):
    f = _campagne(declarant, None, None)
    assert _compter([f]) == {f["id"]: None}


def test_un_tableau_introuvable_ecarte_la_campagne_et_le_journalise_une_fois(declarant, caplog):
    from oto_mcp.capabilities._lignes_reservables import TTL_S
    f = _campagne(declarant, "aucun-tableau-" + uuid.uuid4().hex[:6], {"passe": "2"})
    nom = "oto_mcp.capabilities._lignes_reservables"

    with caplog.at_level(logging.WARNING, logger=nom):
        assert f["id"] not in _compter([f], 5000.0)
        assert f["id"] not in _compter([f], 5000.0 + TTL_S + 1)

    signales = [r for r in caplog.records if r.name == nom and str(f["id"]) in r.getMessage()]
    assert len(signales) == 1, "une panne qui dure se dit une fois, pas à chaque sondage"


def test_un_plafond_mal_declare_leve_au_compte_comme_a_la_reservation(declarant):
    """Le défaut du comptage de 09/09 : un tableau au plafond sans état d'abandon.
    Écrit HORS de la pose (qui le refuse), comme un schéma antérieur à la garde."""
    from oto_mcp import db
    st, ns, ns_id, _ = _table(declarant, _schema(), [_ligne()])
    bancal = _schema(max_claims=3)
    db.set_datastore_schema(ns_id, bancal)
    rf = {"lot": "L"}
    f = _campagne(declarant, ns, rf)

    assert f["id"] not in _compter([f]), "un compte impossible ne doit pas servir la campagne"
    with pytest.raises(ValueError):
        st.claim_next(ns, worker="w", filter=rf)
