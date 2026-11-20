"""L'avancement d'une ligne à travers plusieurs passes ne repose plus sur l'agent (oto#95).

Une campagne en passes (`a_traiter → societe → dirigeant → email`) : l'agent lisait le
marqueur de passe à la réservation et ne l'écrivait jamais — zéro écriture sur deux
passages. La plateforme savait faire RECULER une ligne (au plafond, `abandon_state`) ;
elle la fait désormais AVANCER : relâchée après une écriture, une ligne passe à l'état
que `lifecycle.advance` déclare pour son état courant.

Le banc tient sur un PostgreSQL RÉEL : ce qui est en cause est un compteur de colonne,
un verrou de ligne, un déclencheur de journal. Un magasin reconstitué mesurerait la
représentation qu'on s'en fait.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

from oto_mcp.datastore import schema as S

ETATS = ["a_traiter", "societe", "dirigeant", "email", "fait", "echec"]


def _schema(**lifecycle) -> dict:
    lc = {"states": ETATS,
          "transitions": {"a_traiter": ["societe"], "societe": ["dirigeant", "echec"],
                          "dirigeant": ["email", "echec"], "email": ["fait", "echec"],
                          "echec": ["a_traiter"]},
          "terminal": ["fait", "echec"],
          "max_claims": 3, "abandon_state": "echec",
          "advance": {"societe": "dirigeant", "dirigeant": "email", "email": "fait"}}
    lc.update(lifecycle)
    lc = {k: v for k, v in lc.items() if v is not None}
    return {"fields": [
        {"key": "nom", "type": "text"},
        {"key": "trouve", "type": "text"},
        {"key": "statut", "type": "enum", "options": ETATS, "lifecycle": lc},
    ]}


@contextlib.contextmanager
def sous_le_run(run_id: str):
    """Réservation ET écriture sous le même run : c'est la preuve de titularité."""
    from oto_mcp import session_org
    jeton = session_org.set_call_run(run_id)
    try:
        yield
    finally:
        session_org.reset_call_run(jeton)


def _table(schema=None, n: int = 1) -> tuple:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "passes-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-o95", ns)
    ns = str(ns_id)
    st = make_store("sub-o95")
    st.set_schema(ns, schema or _schema())
    for i in range(n):
        st.append_row(ns, {"nom": f"ENTREPRISE {i}", "statut": "societe"})
    return st, ns, ns_id


def _brut(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT data, claims, abandon_reason, claimed_by, claimed_run "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone())


def _passe(st, ns, run: str, etat: str, ecrire: dict | None) -> str:
    """Une passe : réserver PAR ÉTAT, écrire (ou pas), sous le run ; rend l'id."""
    with sous_le_run(run):
        row = st.claim_next(ns, worker="agent", filter={"statut": etat})
        assert row is not None, f"une ligne `{etat}` devait être servie"
        if ecrire is not None:
            st.update_row(ns, row["_id"], ecrire)
    return row["_id"]


# ══ 1. l'avance au relâchement ═══════════════════════════════════════════════

def test_relachee_apres_une_ecriture_la_ligne_passe_a_l_etat_suivant(live):
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-1", "societe", {"trouve": "SIREN 123"})
    issue = st.release_claim(ns, rid, worker="agent")
    assert issue["released"] is True
    assert issue["advanced"] == {"field": "statut", "from": "societe", "to": "dirigeant"}
    brut = _brut(ns_id, rid)
    assert brut["data"]["statut"] == "dirigeant"
    assert brut["data"]["trouve"] == "SIREN 123", "l'écriture de la passe est intacte"
    assert brut["claimed_by"] is None and brut["claimed_run"] is None


def test_les_passes_s_enchainent_jusqu_au_terminal(live):
    """Chaque passe réserve par SON état, écrit son travail, relâche : la ligne
    traverse la suite sans que personne n'écrive l'état. ⚠️ Les reprises sont
    immédiates — la révision de l'avance précédente ne doit pas passer pour une
    écriture de l'état par la passe suivante (même seconde)."""
    st, ns, ns_id = _table()
    for etat, suivant in (("societe", "dirigeant"), ("dirigeant", "email"),
                          ("email", "fait")):
        rid = _passe(st, ns, f"run-{etat}", etat, {"trouve": f"passe {etat}"})
        assert st.release_claim(ns, rid, worker="agent")["advanced"]["to"] == suivant
    assert _brut(ns_id, rid)["data"]["statut"] == "fait"
    assert st.claim_next(ns, worker="agent", filter={"statut": "email"}) is None


def test_reecrire_le_meme_etat_n_empeche_pas_l_avance(live):
    """Renvoyer la fiche entière, état compris et inchangé, est un geste légitime : ce
    n'est pas « l'agent a écrit l'état », le journal n'en garde aucune révision."""
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-1", "societe", {"statut": "societe", "trouve": "x"})
    assert st.release_claim(ns, rid, worker="agent")["advanced"]["to"] == "dirigeant"


# ══ 2. ce qui n'avance pas ═══════════════════════════════════════════════════

def test_sans_ecriture_la_ligne_n_avance_pas_et_le_plafond_s_applique(live):
    """Une passe en échec ne fait pas avancer : la ligne revient dans son état, et au
    plafond la plateforme la fait reculer comme avant (`abandon_state`)."""
    st, ns, ns_id = _table()
    for tour in range(3):
        rid = _passe(st, ns, f"run-{tour}", "societe", None)
        issue = st.release_claim(ns, rid, worker="agent")
        assert issue["released"] is True and issue["advanced"] is None
    brut = _brut(ns_id, rid)
    assert brut["data"]["statut"] == "echec", "le plafond verse dans abandon_state"
    assert brut["abandon_reason"]


def test_l_agent_qui_ecrit_l_etat_fait_foi(live):
    """Son écriture fait foi : la plateforme n'avance pas une seconde fois."""
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-1", "societe", {"statut": "dirigeant", "trouve": "x"})
    issue = st.release_claim(ns, rid, worker="agent")
    assert issue["advanced"] is None
    assert _brut(ns_id, rid)["data"]["statut"] == "dirigeant", "pas `email`"


def test_l_agent_qui_conclut_en_echec_n_est_pas_avance(live):
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-1", "societe", {"statut": "echec"})
    assert st.release_claim(ns, rid, worker="agent")["advanced"] is None
    assert _brut(ns_id, rid)["data"]["statut"] == "echec"


def test_un_bail_qui_expire_n_est_pas_un_relachement(live):
    """L'agent qui écrit puis meurt n'a pas rendu son verdict : la passe se refait."""
    from oto_mcp.db._conn import _connect
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-1", "societe", {"trouve": "à moitié"})
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET claimed_until = NOW() - interval '1 hour' "
                     "WHERE ns_id = %s AND row_id = %s", (ns_id, rid))
    reprise = st.claim_next(ns, worker="autre", filter={"statut": "societe"})
    assert reprise is not None and reprise["_id"] == rid


def test_sans_advance_declare_rien_ne_bouge(live):
    st, ns, ns_id = _table(_schema(advance=None))
    rid = _passe(st, ns, "run-1", "societe", {"trouve": "x"})
    assert st.release_claim(ns, rid, worker="agent")["advanced"] is None
    assert _brut(ns_id, rid)["data"]["statut"] == "societe"


# ══ 3. les autres chemins de relâchement ═════════════════════════════════════

def test_run_finish_avance_ce_que_le_run_a_ecrit_et_seulement_ca(live):
    from oto_mcp import db
    st, ns, ns_id = _table(n=2)
    with sous_le_run("run-fin"):
        ecrite = st.claim_next(ns, worker="agent", filter={"statut": "societe"})
        vue = st.claim_next(ns, worker="agent", filter={"statut": "societe"})
        st.update_row(ns, ecrite["_id"], {"trouve": "SIREN 1"})
    assert db.datastore_release_by_run("run-fin") == 2
    assert _brut(ns_id, ecrite["_id"])["data"]["statut"] == "dirigeant"
    assert _brut(ns_id, vue["_id"])["data"]["statut"] == "societe"
    assert _brut(ns_id, vue["_id"])["claimed_by"] is None


def test_la_liberation_forcee_d_un_superviseur_avance_aussi(live):
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-1", "societe", {"trouve": "x"})
    avance: dict = {}
    assert st.force_release(ns, rid, avance=avance) is True
    assert avance == {"field": "statut", "from": "societe", "to": "dirigeant"}


# ══ 4. le journal ════════════════════════════════════════════════════════════

def test_l_avance_est_une_revision_du_journal_rattachee_au_run(live):
    """Lisible comme le reste (`data_row_history`), et réversible comme le reste : la
    révision porte l'avant et l'après, donc l'état d'où revenir."""
    from oto_mcp.db.historique import revisions_de_ligne
    st, ns, ns_id = _table()
    rid = _passe(st, ns, "run-journal", "societe", {"trouve": "x"})
    with sous_le_run("run-de-l-appel"):        # le run de l'APPEL n'est pas celui de la passe
        st.release_claim(ns, rid, worker="agent")
    revs = revisions_de_ligne(ns_id, rid)
    avance = [r for r in revs if "statut" in (r.get("diff") or {})
              and r["diff"]["statut"].get("apres") == "dirigeant"]
    assert len(avance) == 1, revs
    r = avance[0]
    assert r["diff"]["statut"] == {"avant": "societe", "apres": "dirigeant"}
    assert r["diff"].keys() == {"statut"}, "l'avance ne touche que l'état"
    assert r["source"] == "system" and r["acteur"] == "service:file-de-travail"
    assert r["run_id"] == "run-journal"
    assert r["geste_id"]


# ══ 5. la déclaration ════════════════════════════════════════════════════════

def test_une_avance_qui_n_est_pas_une_transition_declaree_est_refusee_a_la_pose():
    erreurs = S.validate_schema_def(_schema(advance={"societe": "email"}))
    assert len(erreurs) == 1, erreurs
    assert "'societe' → 'email'" in erreurs[0] and "transition" in erreurs[0]
    assert '"societe": ["dirigeant", "echec", "email"]' in erreurs[0], \
        "le patch proposé garde les destinations existantes"


def test_la_bonne_declaration_passe():
    assert S.validate_schema_def(_schema()) == []
    assert S.validate_schema_def(_schema(transitions=None)) == [], \
        "sans table de transitions, toute transition est permise — l'avance aussi"


@pytest.mark.parametrize("advance,attendu", [
    ({"societe": ["dirigeant"]}, "must be ONE state"),
    ({"societe": None}, "must be ONE state"),
    ("dirigeant", "must be an object"),
    ({"societe": "inconnu"}, "état inconnu 'inconnu'"),
    ({"fait": "echec"}, "est un état terminal"),
    ({"echec": "a_traiter"}, "est un état terminal"),
])
def test_une_avance_mal_declaree_est_refusee_et_nommee(advance, attendu):
    erreurs = S.validate_schema_def(_schema(advance=advance))
    assert any(attendu in e for e in erreurs), erreurs


def test_advance_fait_de_sa_colonne_la_file_deux_files_sont_refusees():
    sch = _schema(advance=None)
    sch["fields"].append({"key": "suivi", "type": "text", "lifecycle": {
        "states": ["x", "y"], "transitions": {"x": ["y"]}, "terminal": ["y"],
        "advance": {"x": "y"}}})
    erreurs = S.validate_schema_def(sch)
    assert any("deux colonnes déclarent une FILE" in e and "`advance`" in e
               for e in erreurs), erreurs


def test_le_patch_fusionne_l_avance_etat_par_etat_et_garde_la_coherence(live):
    st, ns, ns_id = _table(_schema(advance={"societe": "dirigeant"}))
    st.patch_schema(ns, fields=[{"key": "statut",
                                 "lifecycle": {"advance": {"dirigeant": "email"}}}])
    assert S.avance_of(st.get_schema(ns)) == {"societe": "dirigeant",
                                              "dirigeant": "email"}
    st.patch_schema(ns, fields=[{"key": "statut",
                                 "lifecycle": {"advance": {"societe": None}}}])
    assert S.avance_of(st.get_schema(ns)) == {"dirigeant": "email"}
    # Retirer la transition qu'une avance emprunte est refusé : c'est le schéma
    # FUSIONNÉ qui est jugé.
    with pytest.raises(ValueError, match="is not a declared transition"):
        st.patch_schema(ns, fields=[{"key": "statut", "lifecycle": {
            "transitions": {"dirigeant": ["echec"]}}}])


# ══ 6. le texte servi ════════════════════════════════════════════════════════

def test_le_texte_servi_dit_le_mecanisme():
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import guide_store
    from oto_mcp.tools import datastore as D
    m = FastMCP("t")
    D.register(m)
    fiche = lambda nom: asyncio.run(m.get_tool(nom)).description or ""  # noqa: E731
    assert "lifecycle.advance" in fiche("data_set_schema")
    assert "advance" in fiche("data_claim_next")
    assert "advanced" in fiche("data_release")
    guide = guide_store.file_guide("datastore-semantics")["body_md"]
    assert "`advance`" in guide and "is two QUEUES" in guide
