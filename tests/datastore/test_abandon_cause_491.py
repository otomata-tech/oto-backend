"""Le motif d'abandon dit la CAUSE, pas seulement le symptôme (oto-backend#491).

« Abandonnée après 3 réservations sans écriture, plafond 3 » réunissait quatre causes à
quatre gestes différents — un redémarrage qui a coupé les sessions, un modèle qui
dégénère, un outil en panne, une ligne impossible. Sur une file de plusieurs milliers
de lignes, c'est un livrable qui rétrécit en silence si personne n'examine chaque cas.

Ce que ces tests tiennent, contre un PostgreSQL réel :

1. **la cause** que la dernière tentative a rencontrée finit le motif, dans l'ordre :
   erreur du travail, échec déclaré du run, écriture refusée, appels en erreur, bail
   expiré, rien tenté ;
2. **le minimum garanti** : dès qu'un run est connu, `_abandon_run` le nomme — y compris
   quand la cause est illisible, qui le dit au lieu de faire échouer l'abandon ;
3. **la fenêtre** : ce que le même run a fait AVANT de prendre la ligne ne la concerne
   pas ;
4. **la symétrie** : une écriture efface le run avec le motif, et toute projection de
   ligne qui sert le motif sert aussi le run.

Le journal des appels est écrit ici à la main, dans la forme que le middleware d'appel
lui donne (`calllog.ToolCallLogger` : `tool`, `args`, `ok`, `error`, `run_id`, `kind`) ;
le refus d'écriture, lui, est le texte d'un VRAI refus du store.
"""
from __future__ import annotations

import ast
import contextlib
import json
import logging
import re
import pathlib
import uuid

import pytest

# Le tableau minimal d'une file plafonnée — même forme que le banc du plafond
# (`test_datastore_claim_ceiling.py`), recopiée : un banc n'importe pas d'un autre.
PLAFONNE = {"fields": [
    {"key": "societe", "type": "text"},
    {"key": "statut", "type": "enum", "role": "status",
     "options": ["a_faire", "traite", "echec"],
     "lifecycle": {"states": ["a_faire", "traite", "echec"],
                   "transitions": {"a_faire": ["traite", "echec"], "echec": ["a_faire"]},
                   "terminal": ["traite", "echec"],
                   "max_claims": 3, "abandon_state": "echec"}},
]}


@contextlib.contextmanager
def sous_le_run(run_id: str):
    """Réserver et écrire SOUS un run — ce que pose `_run_id=` en production."""
    from oto_mcp import session_org
    jeton = session_org.set_call_run(run_id)
    try:
        yield
    finally:
        session_org.reset_call_run(jeton)


def _table(schema, ligne: dict) -> tuple:
    """Un tableau neuf portant UNE ligne à traiter."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "cause-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-agent", ns)
    ns = str(ns_id)
    st = make_store("sub-agent")
    st.set_schema(ns, schema)
    st.append_row(ns, ligne)
    return st, ns, ns_id


def _colonnes(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT abandon_reason, abandon_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone())


@pytest.fixture
def plafonne(live):
    return _table(PLAFONNE, {"societe": "ENTREPRISE TEMOIN", "statut": "a_faire"})


def _run() -> str:
    return "run-" + uuid.uuid4().hex[:10]


def _appel(run_id: str, tool: str, *, ok: bool, error: str | None = None,
           args: dict | None = None, il_y_a: str | None = None) -> None:
    """Une ligne du journal des appels, telle que le middleware la pose."""
    from oto_mcp.db._conn import _connect
    quand = "NOW()" if il_y_a is None else f"NOW() - interval '{il_y_a}'"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tool_calls (tool, args, ok, error, run_id, kind, created_at) "
            f"VALUES (%s, %s::jsonb, %s, %s, %s, 'mcp', {quand})",
            (tool, json.dumps(args) if args is not None else None, ok, error, run_id))


def _tentative(st, ns, run_id: str) -> str:
    """Une réservation sous `run_id` ; rend l'id de la ligne."""
    with sous_le_run(run_id):
        row = st.claim_next(ns, worker="agent-1")
    assert row is not None, "la ligne devait encore être servie"
    return row["_id"]


def _fin_du_travail(run_id: str, erreur: str | None = None) -> int:
    """La conclusion d'un travail : le run rend ce qu'il tenait (#633)."""
    from oto_mcp import db
    return db.datastore_release_by_run(run_id, erreur=erreur)


def _deux_faux_departs(st, ns) -> None:
    for _ in range(2):
        run = _run()
        _tentative(st, ns, run)
        assert _fin_du_travail(run) == 1


def _lue(st, ns, rid) -> dict:
    return next(r for r in st.list_rows(ns) if r["_id"] == rid)


# ══ le test d'acceptation ═══════════════════════════════════════════════════

def test_le_dernier_travail_echoue_sur_un_outil_le_motif_le_nomme(plafonne):
    """Trois travaux, le dernier a échoué sur un outil : la ligne abandonnée porte le
    NOM de cet outil, et le run de cette tentative."""
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)
    _appel(dernier, "fr_search", ok=True)
    _appel(dernier, "serper_search", ok=False, error="upstream 503")

    _fin_du_travail(dernier)

    ligne = _lue(st, ns, rid)
    assert ligne["statut"] == "echec"
    assert ligne["_abandon"] == (
        "abandoned after 3 claims without a write, ceiling 3 — last "
        "attempt: calls in error: serper_search")
    assert ligne["_abandon_run"] == dernier


def test_oto_call_se_nomme_par_sa_cible(plafonne):
    """`oto_call` relaie : c'est sa CIBLE qui a échoué, et c'est elle qu'on répare."""
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)
    _appel(dernier, "oto_call", ok=False, error="introuvable",
           args={"name": "pappers_company"})

    _fin_du_travail(dernier)

    assert _lue(st, ns, rid)["_abandon"].endswith(
        "last attempt: calls in error: pappers_company")


# ══ les autres causes ═══════════════════════════════════════════════════════

def test_la_derniere_ecriture_refusee_par_le_schema_donne_son_motif(plafonne):
    """Le refus du schéma n'est écrit nulle part ailleurs que dans le journal des
    appels — le journal des révisions n'enregistre que ce qui a été écrit."""
    from oto_mcp.datastore.errors import RowValidationError
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)
    with sous_le_run(dernier), pytest.raises(RowValidationError) as refus:
        st.update_row(ns, rid, {"statut": "inconnu"})
    _appel(dernier, "data_write", ok=False, error=str(refus.value),
           args={"datastore": ns, "id": rid, "row": "{'statut': 'inconnu'}"})
    # Un autre outil en panne PLUS TÔT : l'écriture refusée passe devant.
    _appel(dernier, "serper_search", ok=False, error="upstream 503")

    with sous_le_run(dernier):
        st.release_claim(ns, rid, worker="agent-1")

    ligne = _lue(st, ns, rid)
    texte = " ".join(str(refus.value).split())
    # Le refus du schéma se nomme lui-même : il n'est pas préfixé une seconde fois.
    assert texte.startswith("write refused by the schema"), texte
    assert ligne["_abandon"].endswith(
        "last attempt: " + texte[:300] + ("…" if len(texte) > 300 else "")), \
        ligne["_abandon"]
    assert ligne["_abandon_run"] == dernier


def test_une_ecriture_refusee_sur_une_autre_ligne_ne_lui_est_pas_imputee(plafonne):
    """Un run qui tient plusieurs lignes : le refus d'écrire la voisine n'est pas la
    cause de celle-ci — mais l'outil reste nommé parmi les appels en erreur."""
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)
    _appel(dernier, "data_write", ok=False, error="refus sur la voisine",
           args={"datastore": ns, "id": "une-autre-ligne"})

    _fin_du_travail(dernier)

    assert _lue(st, ns, rid)["_abandon"].endswith(
        "last attempt: calls in error: data_write")


def test_rien_tente_se_dit(plafonne):
    """Le faux départ pur : un run, aucune écriture, aucun appel en erreur DANS la
    fenêtre de la tentative — une erreur du même run avant la prise ne compte pas."""
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    _appel(dernier, "serper_search", ok=False, error="avant la prise", il_y_a="1 hour")
    rid = _tentative(st, ns, dernier)

    _fin_du_travail(dernier)

    ligne = _lue(st, ns, rid)
    assert ligne["_abandon"] == (
        "abandoned after 3 claims without a write, ceiling 3 — last "
        "attempt: no write attempted")
    assert ligne["_abandon_run"] == dernier


def test_l_erreur_du_travail_passe_devant_tout(plafonne):
    """Ce que le worker déclare en concluant le travail en échec est la cause la plus
    proche : le relâchement qu'il déclenche la porte."""
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)
    _appel(dernier, "serper_search", ok=False, error="upstream 503")

    _fin_du_travail(dernier, erreur="délai mural dépassé\n(600 s)")

    assert _lue(st, ns, rid)["_abandon"].endswith(
        "last attempt: job error: délai mural dépassé (600 s)")


def test_un_run_clos_en_echec_donne_sa_note(plafonne):
    """`run_finish(failed|blocked, note)` écrit l'issue avant de relâcher : la note
    de l'agent est la cause qu'il a lui-même constatée."""
    from oto_mcp import db
    st, ns, _ = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    db.insert_run(dernier, sub="sub-agent", org_id=None, label="passe")
    rid = _tentative(st, ns, dernier)
    db.finish_run(dernier, "blocked", "site de la société introuvable", sub="sub-agent")

    _fin_du_travail(dernier)

    assert _lue(st, ns, rid)["_abandon"].endswith(
        "last attempt: run closed `blocked`: site de la société introuvable")


def test_le_bail_expire_sans_relachement_se_dit_et_garde_son_run(plafonne):
    """L'agent qui disparaît (session coupée, redémarrage) ne relâche rien : c'est le
    filet du claim qui abandonne, et le run est encore sur la ligne."""
    from oto_mcp.db._conn import _connect
    st, ns, ns_id = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET claimed_until = NOW() - interval '1 hour' "
                     "WHERE ns_id = %s AND row_id = %s", (ns_id, rid))

    assert st.claim_next(ns, worker="agent-2") is None

    ligne = _lue(st, ns, rid)
    assert ligne["_abandon"].endswith("last attempt: lease expired without a release")
    assert ligne["_abandon_run"] == dernier


def test_sans_run_la_cause_est_inconnue_et_le_dit(plafonne):
    st, ns, _ = plafonne
    for _ in range(3):
        row = st.claim_next(ns, worker="agent-1")
        st.release_claim(ns, row["_id"], worker="agent-1")

    ligne = _lue(st, ns, row["_id"])
    assert ligne["_abandon"].endswith("last attempt: unknown cause: claimed outside a run")
    assert "_abandon_run" not in ligne


# ══ le minimum garanti ══════════════════════════════════════════════════════

def test_journal_illisible_l_abandon_se_fait_quand_meme_avec_son_run(plafonne, monkeypatch,
                                                                     caplog):
    """La lecture du journal ne doit jamais faire échouer l'abandon — ni la réservation
    qui le porte. Elle échoue : « cause inconnue », le run quand même, et une erreur
    au journal applicatif, pas un silence."""
    from oto_mcp.db import rowabandon
    st, ns, ns_id = plafonne
    _deux_faux_departs(st, ns)
    dernier = _run()
    rid = _tentative(st, ns, dernier)

    def _illisible(conn, *a, **k):
        conn.execute("SELECT 1/0")
    monkeypatch.setattr(rowabandon, "_lu_au_journal", _illisible)

    with caplog.at_level(logging.ERROR, logger="oto_mcp.db.rowabandon"):
        _fin_du_travail(dernier)

    ligne = _lue(st, ns, rid)
    assert ligne["statut"] == "echec"
    assert ligne["_abandon"].endswith("last attempt: unknown cause: log unreadable")
    assert ligne["_abandon_run"] == dernier
    assert any("cause d'abandon illisible" in r.getMessage() for r in caplog.records)


def test_plafond_par_defaut_le_motif_porte_aussi_la_cause(live):
    st, ns, _ = _table({"fields": [{"key": "societe", "type": "text"}]},
                       {"societe": "ENTREPRISE TEMOIN"})
    for _ in range(3):
        run = _run()
        rid = _tentative(st, ns, run)
        _fin_du_travail(run)

    ligne = _lue(st, ns, rid)
    assert "ceiling 3 (platform default" in ligne["_abandon"]
    assert ligne["_abandon"].endswith("last attempt: no write attempted")
    assert ligne["_abandon_run"] == run


# ══ la symétrie ═════════════════════════════════════════════════════════════

def test_une_ecriture_efface_le_run_avec_le_motif(plafonne):
    st, ns, ns_id = plafonne
    for _ in range(3):
        run = _run()
        rid = _tentative(st, ns, run)
        _fin_du_travail(run)
    assert _lue(st, ns, rid)["_abandon_run"] == run

    st.update_row(ns, rid, {"societe": "CORRIGÉE"})

    assert _colonnes(ns_id, rid) == {"abandon_reason": None, "abandon_run": None}
    assert "_abandon_run" not in _lue(st, ns, rid)


def _projections_du_motif(racine: pathlib.Path) -> list[tuple[str, str]]:
    """(fichier, SQL) des requêtes de `db/` qui RENDENT `abandon_reason` avec une
    ligne — colonne rendue (`abandon_reason,`), pas filtre (`IS NULL`) ni écriture
    (`= %s`). Par l'AST : un littéral SQL s'écrit en morceaux concaténés."""
    trouvees = []
    for f in sorted(racine.glob("*.py")):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                sql = " ".join(n.value.split())
                if re.search(r"abandon_reason'?\s*,", sql) and "claims" in sql:
                    trouvees.append((f.name, sql))
    return trouvees


def test_toute_projection_qui_sert_le_motif_sert_le_run():
    """Le run de la tentative ne vaut que s'il part AVEC le motif : une projection de
    ligne qui l'oublierait servirait un abandon sans son rapprochement, en silence."""
    racine = pathlib.Path(__file__).resolve().parents[2] / "oto_mcp" / "db"
    projections = _projections_du_motif(racine)
    assert len(projections) >= 8, projections   # la garde voit bien les projections
    oublis = [(f, sql[:90]) for f, sql in projections if "abandon_run" not in sql]
    assert not oublis, oublis
