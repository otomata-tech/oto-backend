"""La file de travail avance : ordre de service et plafond par défaut (oto#101).

`data_claim_next` servait `ORDER BY row_id` — la plus ancienne ligne éligible —, sans
mémoire de ce qu'il venait de servir. Une ligne relâchée qui correspondait encore au
filtre revenait EN TÊTE : trois workers filtrant sur une colonne que leur traitement
n'écrivait pas ont tourné sur les deux ou trois mêmes lignes, sans jamais atteindre les
834 lignes fraîches. Livelock reproduit trois fois sur trois, chaque appel réussissant.

Deux corrections, jugées ici sur un PostgreSQL RÉEL — l'ordre d'un `SELECT … FOR UPDATE
SKIP LOCKED` et un index partiel ne se simulent pas :

- **l'ordre** : la ligne servie le moins récemment (`claimed_at NULLS FIRST`, puis
  `row_id`) — une ligne relâchée ou expirée passe derrière toute ligne jamais servie ;
- **le plafond par défaut** : sans `lifecycle.max_claims`, une ligne réservée
  `OTO_MCP_CLAIM_DEFAULT_MAX_CLAIMS` fois (3) sans écriture est mise de côté par la
  colonne de plateforme, motif compris, données intactes.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

# Un tableau SANS cycle de vie : le cas où aucune garde n'existait.
LIBRE = {"fields": [
    {"key": "segment", "type": "text"},
    {"key": "resultat", "type": "text"},
    {"key": "rang"},
]}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-agent")


@contextlib.contextmanager
def sous_le_run(run_id: str):
    """Le titulaire écrit sous son run (#317) — réservation ET écriture."""
    from oto_mcp import session_org
    jeton = session_org.set_call_run(run_id)
    try:
        yield
    finally:
        session_org.reset_call_run(jeton)


def _tableau(n: int, schema: dict = LIBRE) -> tuple:
    """Un tableau neuf de `n` lignes, toutes du segment « a », dans l'ordre de
    création : rend `(store, nom, ns_id, [ids dans l'ordre de création])`."""
    from oto_mcp import db
    ns = "file-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-agent", ns)
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, schema)
    for i in range(n):
        st.append_row(ns, {"segment": "a", "resultat": None, "rang": i})
    ids = [r["_id"] for r in sorted(st.list_rows(ns), key=lambda r: r["rang"])]
    return st, ns, ns_id, ids


def _brut(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data, claims, abandon_reason, claimed_at, claimed_until "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict(r or {})


def _prendre_et_rendre(st, ns, *, ecrire: bool = False, run: str = "") -> str:
    """Une réservation, éventuellement une écriture, puis le relâchement."""
    with sous_le_run(run or "run-" + uuid.uuid4().hex[:6]):
        row = st.claim_next(ns, worker="w", filter={"segment": "a"})
        assert row is not None, "la file devait encore servir une ligne"
        if ecrire:
            st.update_row(ns, row["_id"], {"resultat": "vu"})
    st.release_claim(ns, row["_id"], worker="w")
    return row["_id"]


# ══ le livelock : une ligne relâchée ne revient plus en tête ═════════════════

def test_la_ligne_relachee_sans_ecriture_passe_derriere_les_fraiches(live):
    """LE défaut : la première ligne, relâchée à chaque tour, était resservie
    indéfiniment. Elle doit laisser passer toutes les autres."""
    st, ns, _, ids = _tableau(5)

    servies = [_prendre_et_rendre(st, ns) for _ in range(5)]

    assert servies == ids, "chaque ligne fraîche doit être servie une fois, dans l'ordre"


def test_la_ligne_ecrite_mais_toujours_eligible_ne_boucle_plus(live):
    """Le cas mesuré en production : filtre sur une colonne A, écriture dans une
    colonne B. L'écriture remet le compteur à zéro — le plafond ne peut donc rien
    ici —, et seule la mémoire de service fait avancer la file."""
    st, ns, _, ids = _tableau(4)

    servies = [_prendre_et_rendre(st, ns, ecrire=True) for _ in range(4)]

    assert servies == ids
    # Toutes servies une fois : la file repart par la moins récemment servie.
    assert _prendre_et_rendre(st, ns, ecrire=True) == ids[0]


def test_une_ligne_toujours_relachee_ne_bloque_pas_les_autres(live):
    """Un worker qui échoue toujours sur UNE ligne, d'autres qui traitent : les
    autres lignes sont toutes servies, et la ligne fautive finit mise de côté."""
    st, ns, ns_id, ids = _tableau(4)
    fautive = ids[0]
    traitees = set()

    for _ in range(8):
        with sous_le_run("run-" + uuid.uuid4().hex[:6]):
            row = st.claim_next(ns, worker="w", filter={"segment": "a",
                                                          "resultat": {"empty": True}})
            if row is None:
                break
            if row["_id"] != fautive:
                st.update_row(ns, row["_id"], {"resultat": "fait"})
                traitees.add(row["_id"])
        st.release_claim(ns, row["_id"], worker="w")

    assert traitees == set(ids[1:])
    assert _brut(ns_id, fautive)["abandon_reason"] is not None


def test_le_bail_expire_passe_aussi_derriere(live):
    """L'agent mort ne relâche rien : son bail expire. La ligne n'en revient pas
    plus en tête qu'une ligne relâchée."""
    from oto_mcp.db._conn import _connect
    st, ns, ns_id, ids = _tableau(3)
    with sous_le_run("run-mort"):
        tenue = st.claim_next(ns, worker="mort")
    assert tenue["_id"] == ids[0]
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET claimed_until = NOW() - interval '1 hour' "
                     "WHERE ns_id = %s AND row_id = %s", (ns_id, ids[0]))

    assert _prendre_et_rendre(st, ns) == ids[1]
    assert _prendre_et_rendre(st, ns) == ids[2]
    assert _prendre_et_rendre(st, ns) == ids[0]


def test_la_date_de_prise_survit_au_relachement_et_a_l_ecriture(live):
    """`claimed_at` est une mémoire de service, pas un bail : ni le relâchement ni
    l'écriture ne l'effacent — sinon la ligne redeviendrait « jamais servie »."""
    st, ns, ns_id, ids = _tableau(1)
    assert _brut(ns_id, ids[0])["claimed_at"] is None

    _prendre_et_rendre(st, ns, ecrire=True)

    assert _brut(ns_id, ids[0])["claimed_at"] is not None


def test_claim_row_date_la_prise_pas_le_renouvellement(live):
    """La réservation nommée (file pilotée à la main) date une PRISE ; le titulaire
    qui rafraîchit son écran ne déplace pas la ligne dans la file."""
    st, ns, ns_id, ids = _tableau(1)
    with sous_le_run("run-humain"):
        st.claim_row(ns, ids[0], worker="poste-1")
        prise = _brut(ns_id, ids[0])["claimed_at"]
        st.claim_row(ns, ids[0], worker="poste-1")
    assert prise is not None
    assert _brut(ns_id, ids[0])["claimed_at"] == prise


# ══ le plafond par défaut : mise de côté sans lifecycle ═══════════════════════

def test_sans_lifecycle_la_ligne_est_mise_de_cote_au_plafond_par_defaut(live):
    st, ns, ns_id, ids = _tableau(1)

    for _ in range(3):
        _prendre_et_rendre(st, ns)

    assert st.claim_next(ns, worker="w") is None
    brut = _brut(ns_id, ids[0])
    assert brut["abandon_reason"].startswith(
        "abandoned after 3 claims without a write, ceiling 3 (platform default")
    # Mise de côté par la colonne de plateforme : les données du métier sont intactes.
    assert brut["data"].get("segment") == "a" and brut["data"].get("resultat") is None


def test_la_mise_de_cote_se_dit_dans_la_ligne_servie(live):
    st, ns, _, ids = _tableau(1)
    for _ in range(3):
        _prendre_et_rendre(st, ns)

    ligne = next(r for r in st.list_rows(ns) if r["_id"] == ids[0])

    assert "ceiling 3" in ligne["_abandon"]


def test_une_ecriture_remet_la_ligne_mise_de_cote_dans_la_file(live):
    st, ns, ns_id, ids = _tableau(1)
    for _ in range(3):
        _prendre_et_rendre(st, ns)

    st.update_row(ns, ids[0], {"resultat": "repris à la main"})

    assert _brut(ns_id, ids[0])["abandon_reason"] is None
    with sous_le_run("run-reprise"):
        assert st.claim_next(ns, worker="w")["_id"] == ids[0]


def test_le_plafond_par_defaut_est_un_reglage(live, monkeypatch):
    monkeypatch.setenv("OTO_MCP_CLAIM_DEFAULT_MAX_CLAIMS", "2")
    st, ns, ns_id, ids = _tableau(1)

    for _ in range(2):
        _prendre_et_rendre(st, ns)

    assert st.claim_next(ns, worker="w") is None
    assert "ceiling 2" in _brut(ns_id, ids[0])["abandon_reason"]


@pytest.mark.parametrize("valeur", ["0", "-1", "trois", ""])
def test_un_reglage_illisible_leve(live, monkeypatch, valeur):
    """Pas de repli silencieux : une garde qu'on croit armée et qui ne l'est pas
    est pire que pas de garde."""
    monkeypatch.setenv("OTO_MCP_CLAIM_DEFAULT_MAX_CLAIMS", valeur)
    st, ns, _, _ = _tableau(1)

    with sous_le_run("run-x"), pytest.raises(ValueError,
                                             match="OTO_MCP_CLAIM_DEFAULT_MAX_CLAIMS"):
        st.claim_next(ns, worker="w")


# ══ l'index sert le pick ══════════════════════════════════════════════════════

def test_le_pick_passe_par_l_index_de_file(live):
    """L'expression d'ordre est celle de l'index, au caractère près : sinon chaque
    réservation trierait tout le tableau. Jugé au plan, sur un tableau assez gros
    pour que le planificateur ait le choix."""
    from oto_mcp.db import rowlock
    from oto_mcp.db._conn import _connect
    st, ns, ns_id, _ = _tableau(1)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data) "
            "SELECT %s, 'r' || lpad(g::text, 6, '0'), "
            "       jsonb_build_object('segment', 'a') "
            "FROM generate_series(1, 20000) g", (ns_id,))
        conn.execute("UPDATE datastore_rows SET claimed_at = NOW() - (random() * interval '1 day') "
                     "WHERE ns_id = %s AND row_id < 'r010000'", (ns_id,))
        conn.execute("ANALYZE datastore_rows")
        where, params = rowlock._perimetre_reclamable(ns_id, None)
        plan = "\n".join(r["QUERY PLAN"] for r in conn.execute(
            f"EXPLAIN SELECT row_id FROM datastore_rows {where} "
            f"ORDER BY {rowlock._ORDRE_DE_SERVICE} LIMIT 1 FOR UPDATE SKIP LOCKED",
            tuple(params)).fetchall())

    assert rowlock.INDEX_FILE in plan, plan
    assert "Sort" not in plan, plan
