"""Le verrou de ligne devient natif (#317) — libération par run + protection en écriture.

Deux manques mesurés sur le terrain, que ce lot comble :

**① L'agent qui meurt garde sa ligne.** La pile de run est session-scopée (aucune
table) : elle ne survit ni au redémarrage ni à l'agent disparu — or c'est précisément
lui qu'il faut ramasser. Le lien run→ligne est donc durable, porté par le bail
lui-même. Mesure qui l'a rendu nécessaire : **une** ligne portait un bail sur toute la
production, tenue depuis **18 jours** par un worker disparu, invisible de tous.

**② Le bail protégeait l'attribution, pas la donnée.** Deux agents ne prenaient pas la
même ligne, mais rien n'empêchait le second d'écrire dessus.

⚠️ Le titulaire s'identifie de DEUX façons qui se recouvrent, parce qu'une écriture
ordinaire ne dit pas qui écrit et que `claimed_by` est un libellé libre, jamais un
compte : par le RUN (transparent, cas nominal) ou par le WORKER rejoué (la sortie
explicite hors run, déjà la garde du release).
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def table(live):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "q-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    # `societe` déclarée : une colonne non déclarée est refusée (oto#124).
    _store().set_schema(ns, {"fields": [{"key": "societe", "type": "text"}]})
    for i in range(3):
        db.datastore_insert_row(ns_id, f"r{i}", {"societe": f"Boîte {i}"})
    return ns, ns_id


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _bail(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id)).fetchone()
    return dict(r or {})


# ── ① la libération par run ──────────────────────────────────────────────────

def test_a_claim_records_the_run_that_holds_it(table):
    """Le lien est DURABLE, porté par le bail — pas par la pile de session, qui ne
    survivrait pas à l'agent mort qu'on cherche justement à ramasser."""
    from oto_mcp import db, session_org
    ns, ns_id = table

    token = session_org.set_call_run("run-abc")
    try:
        row = _store().claim_next(ns, worker="w1")
    finally:
        session_org.reset_call_run(token) if hasattr(session_org, "reset_call_run") else None

    assert row is not None
    assert _bail(ns_id, row["_id"])["claimed_run"] == "run-abc"
    assert db.datastore_release_by_run("run-abc") == 1
    assert _bail(ns_id, row["_id"])["claimed_by"] is None


def test_closing_a_run_frees_everything_it_held(table):
    """⚠️ Le cas mesuré : l'agent meurt sans relâcher. La fermeture du run rend TOUT
    ce qu'il tenait, quel que soit son issue — c'est ce qui manquait pendant 18 jours
    sur la seule ligne réservée qu'ait portée la production."""
    from oto_mcp import db, session_org
    ns, ns_id = table

    token = session_org.set_call_run("run-mort")
    try:
        a = _store().claim_next(ns, worker="w1")
        b = _store().claim_next(ns, worker="w1")
    finally:
        session_org.reset_call_run(token) if hasattr(session_org, "reset_call_run") else None
    assert a and b

    assert db.datastore_release_by_run("run-mort") == 2
    for r in (a, b):
        assert _bail(ns_id, r["_id"])["claimed_by"] is None


def test_a_run_only_frees_what_it_held(table):
    """Un run ne libère que SES lignes — sinon fermer un run relâcherait le travail
    d'un collègue, ce qui serait pire que le défaut d'origine."""
    from oto_mcp import db, session_org
    ns, ns_id = table

    t1 = session_org.set_call_run("run-1")
    a = _store().claim_next(ns, worker="w1")
    session_org.reset_call_run(t1) if hasattr(session_org, "reset_call_run") else None
    t2 = session_org.set_call_run("run-2")
    b = _store().claim_next(ns, worker="w2")
    session_org.reset_call_run(t2) if hasattr(session_org, "reset_call_run") else None

    assert db.datastore_release_by_run("run-1") == 1
    assert _bail(ns_id, a["_id"])["claimed_by"] is None
    assert _bail(ns_id, b["_id"])["claimed_by"] == "w2", "l'autre run garde la sienne"


def test_releasing_an_unknown_run_is_a_cheap_no_op(live):
    # `live` (#963) : ce test parle à la base mais ne demandait aucune fixture — il ne passait
    # que s'il tombait sur un worker où un autre test du fichier avait déjà branché
    # DATABASE_URL. Sous `--dist loadgroup` il est distribué seul : rouge (RuntimeError).
    from oto_mcp import db
    assert db.datastore_release_by_run("run-jamais-vu") == 0
    assert db.datastore_release_by_run("") == 0


# ── ② la protection en écriture ──────────────────────────────────────────────

def test_writing_on_a_row_held_by_another_is_refused(table):
    """Le bail protège désormais la DONNÉE, pas seulement l'attribution."""
    from oto_mcp import session_org
    from oto_mcp.datastore.core import RowLocked
    ns, ns_id = table

    t = session_org.set_call_run("run-titulaire")
    row = _store().claim_next(ns, worker="w1")
    session_org.reset_call_run(t) if hasattr(session_org, "reset_call_run") else None

    with pytest.raises(RowLocked) as e:
        _store().update_row(ns, row["_id"], {"societe": "Par un autre"})

    # L'erreur donne la SORTIE, pas seulement le constat — et, depuis #515, ne nomme
    # jamais le titulaire (l'échéance suffit) : elle mène par le `_run_id` manquant.
    msg = str(e.value)
    assert "_run_id" in msg and "data_release" in msg
    assert "w1" not in msg


def test_the_holder_writes_freely_through_its_run(table):
    """La première des deux identifications : écrire sous le run qui tient la ligne,
    c'est être le titulaire — rien à déclarer, le cas nominal est transparent."""
    from oto_mcp import session_org
    ns, ns_id = table

    t = session_org.set_call_run("run-x")
    try:
        row = _store().claim_next(ns, worker="w1")
        _store().update_row(ns, row["_id"], {"societe": "Par son titulaire"})
    finally:
        session_org.reset_call_run(t) if hasattr(session_org, "reset_call_run") else None

    from oto_mcp import db
    assert db.datastore_get_row(ns_id, row["_id"])["data"]["societe"] == "Par son titulaire"


def test_the_holder_writes_freely_from_ANOTHER_session(table):
    """La reprise hors session — et elle passe par le RUN, pas par un second concept.

    Ce banc gardait jusqu'au 07/09/2026 une seconde voie (`writing_as`, se réclamer du
    `worker` du bail). Elle a été retirée : aucune surface ne l'atteignait, et elle
    était **redondante** — c'est ce que ce banc démontre désormais.

    Le point : `_run_id` n'est pas un état que le serveur détient, c'est un jeton que
    l'AGENT porte d'appel en appel. Rien ne le lie à une session. Un agent qui a gardé
    son jeton le rejoue d'où il veut et retrouve sa ligne — ici, la réservation et
    l'écriture sont posées dans deux contextes séparés, comme deux sessions.
    """
    from oto_mcp import db, session_org
    ns, ns_id = table

    # session 1 : il réserve, puis son contexte disparaît entièrement
    t = session_org.set_call_run("run-y")
    row = _store().claim_next(ns, worker="w1")
    session_org.reset_call_run(t)
    assert session_org.current_call_run() is None, "le contexte doit être vraiment parti"

    # session 2 : un autre store, un autre contexte — le même jeton
    t2 = session_org.set_call_run("run-y")
    try:
        _store().update_row(ns, row["_id"], {"societe": "Reprise"})
    finally:
        session_org.reset_call_run(t2)

    assert db.datastore_get_row(ns_id, row["_id"])["data"]["societe"] == "Reprise"


def test_a_DIFFERENT_run_is_refused(table):
    """La contre-épreuve, sans laquelle le banc du dessus ne prouverait rien : ce qui
    passe est le jeton, pas le simple fait d'en avoir un."""
    import pytest
    from oto_mcp import session_org
    from oto_mcp.datastore.errors import RowLocked
    ns, _ = table

    t = session_org.set_call_run("run-y")
    row = _store().claim_next(ns, worker="w1")
    session_org.reset_call_run(t)

    t2 = session_org.set_call_run("run-z")
    try:
        with pytest.raises(RowLocked):
            _store().update_row(ns, row["_id"], {"societe": "Un autre"})
    finally:
        session_org.reset_call_run(t2)


def test_an_expired_lease_protects_nothing(table):
    """⚠️ La nuance qui empêche la protection de devenir un mur : seul un bail ACTIF
    protège. Sans elle, le zombie de 18 jours mesuré en production aurait bloqué sa
    ligne pendant 18 jours."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns, ns_id = table

    row = _store().claim_next(ns, worker="w1")
    with _connect() as conn:                    # on fait expirer le bail
        conn.execute("UPDATE datastore_rows SET claimed_until = NOW() - interval '1 day' "
                     "WHERE ns_id = %s AND row_id = %s", (ns_id, row["_id"]))

    _store().update_row(ns, row["_id"], {"societe": "Le zombie ne bloque rien"})

    assert db.datastore_get_row(
        ns_id, row["_id"])["data"]["societe"] == "Le zombie ne bloque rien"


def test_a_free_row_is_written_as_before(table):
    """Hors bail actif, tout le monde écrit comme avant — le lot ne durcit que ce
    qui est réservé."""
    from oto_mcp import db
    ns, ns_id = table
    _store().update_row(ns, "r2", {"societe": "Libre"})
    assert db.datastore_get_row(ns_id, "r2")["data"]["societe"] == "Libre"


def test_releasing_then_writing_is_the_documented_way_out(table):
    """La sortie officielle, celle que l'erreur indique : lever le bail, puis écrire.
    Deux gestes délibérés — il n'y a pas de « forcer » en un clic, un bouton force
    devenant un réflexe qui rendrait le verrou décoratif."""
    from oto_mcp import db, session_org
    from oto_mcp.datastore.core import RowLocked
    ns, ns_id = table

    t = session_org.set_call_run("run-z")
    row = _store().claim_next(ns, worker="w1")
    session_org.reset_call_run(t) if hasattr(session_org, "reset_call_run") else None

    with pytest.raises(RowLocked):
        _store().update_row(ns, row["_id"], {"societe": "Non"})

    _store().force_release(ns, row["_id"])      # le geste d'un humain qui a le droit
    _store().update_row(ns, row["_id"], {"societe": "Oui"})

    assert db.datastore_get_row(ns_id, row["_id"])["data"]["societe"] == "Oui"


def test_every_write_path_is_covered_not_just_the_merge(table):
    """⚠️ **Le trou que les tests ont trouvé.** Le seam de FUSION n'est pas le seul
    chemin d'écriture : la mise à jour par `id` et la suppression ont leur propre
    porte. Une protection posée sur le seul merge aurait été un verrou troué — et le
    trou aurait été invisible, puisque le cas le plus courant, lui, était protégé."""
    from oto_mcp import session_org
    from oto_mcp.datastore.core import RowLocked
    ns, ns_id = table

    t = session_org.set_call_run("run-couverture")
    row = _store().claim_next(ns, worker="w1")
    session_org.reset_call_run(t)
    rid = row["_id"]

    # mise à jour par `id`
    with pytest.raises(RowLocked):
        _store().update_row(ns, rid, {"societe": "non"})
    # suppression — plus destructrice qu'une écriture, donc gardée aussi
    with pytest.raises(RowLocked):
        _store().delete_row(ns, rid)


# ── étape B : la libération sur état final est retirée ───────────────────────

def _schema_lifecycle(ns_id: int) -> None:
    """Un tableau comme les 19 vivants : un statut, un cycle de vie, un état final."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "UPDATE user_datastores SET schema = %s::jsonb WHERE id = %s",
            ('{"fields": [{"key": "statut", "type": "enum", "role": "status",'
             ' "options": ["a_faire", "fait"],'
             ' "lifecycle": {"states": ["a_faire", "fait"], "terminal": ["fait"]}}]}',
             ns_id))


def test_writing_a_final_state_no_longer_frees_the_row(table):
    """⚠️ **Le retrait.** Le verrou cesse de dépendre de ce que le client appelle
    « terminé » : pour savoir qu'un travail est fini, la plateforme devait connaître
    les états du client et lesquels sont des fins. Une mission a payé ce couplage —
    le verrou écoutait un champ que personne ne remplissait."""
    ns, ns_id = table
    _schema_lifecycle(ns_id)
    from oto_mcp import session_org
    jeton = session_org.set_call_run("run-verdict")
    try:
        row = _store().claim_next(ns, worker="w1")
        _store().update_row(ns, row["_id"], {"statut": "fait"})   # le titulaire écrit
    finally:
        session_org.reset_call_run(jeton)

    assert _bail(ns_id, row["_id"])["claimed_by"] == "w1", \
        "l'état final ne libère plus — c'est le geste de fin de travail qui libère"


def test_the_change_is_announced_where_it_happens(table):
    """Le message est rendu à l'INSTANT où l'ancien comportement aurait joué — le seul
    moment actionnable, et son lecteur est le seul qui puisse agir."""
    ns, ns_id = table
    _schema_lifecycle(ns_id)
    from oto_mcp import session_org
    st = _store()
    jeton = session_org.set_call_run("run-annonce")
    try:
        row = _store().claim_next(ns, worker="w1")
        st.update_row(ns, row["_id"], {"statut": "fait"})
    finally:
        session_org.reset_call_run(jeton)
    notices = st.off_schema_report().get("notices") or []

    assert notices, "le changement doit être dit"
    texte = notices[0]
    assert texte.startswith("The row stays claimed"), "la conséquence d'abord"
    assert "data_release" in texte and "run_finish" in texte, "les deux remplaçants"
    # ⚠️ La promesse est réduite à ce qui est VRAI : la fin de run couvre l'oubli de
    # relâcher, PAS l'agent qui meurt (il n'appelle pas `run_finish`).
    assert "forget to release" in texte
    assert "stops along the way" not in texte, "promesse retirée : elle était fausse"


def test_a_table_without_a_lifecycle_hears_nothing(table):
    """Les 38 tableaux sans cycle de vie ne sont pas concernés : pas de message."""
    ns, ns_id = table
    st = _store()
    st.update_row(ns, "r1", {"societe": "Rien à dire"})
    assert not (st.off_schema_report().get("notices") or [])


def test_a_free_row_hears_nothing_either(table):
    """Écrire un état final sur une ligne qui n'est PAS réservée ne concerne
    personne — le message ne parle qu'à qui perd quelque chose."""
    ns, ns_id = table
    _schema_lifecycle(ns_id)
    st = _store()
    st.update_row(ns, "r2", {"statut": "fait"})
    assert not (st.off_schema_report().get("notices") or [])


# ── ④ le chemin de FUSION : la garde la plus fréquentée, et la moins gardée ──
#
# ⚠️ **Ces deux bancs ferment un trou mesuré le 07/09/2026.** Le magasin a DEUX gardes
# de bail, sur deux chemins différents : `_assert_writable` pour le remplacement, la
# mise à jour et la suppression ; `_lease_guard` pour la FUSION — l'écriture par clé
# métier, c'est-à-dire le geste le plus fréquent d'une campagne (une fiche par SIREN).
# (Depuis, tout passe par `_lease_guard` : la mise à jour le 12/09/2026, et le
# remplacement `upsert_row` — dernier client d'`_assert_writable` — est retiré, oto#156.)
#
# Sonde : en désarmant la branche « le titulaire écrit, reconnu par son run » de
# `_lease_guard`, **1 238 bancs restent verts**. Personne n'écrivait sur une ligne
# réservée par ce chemin-là. La conséquence d'une casse aurait été l'inverse de celle
# qu'on redoute d'habitude : non pas une écriture qui passe sans droit, mais TOUS les
# titulaires légitimes refusés sur le chemin principal, sans un banc pour le dire.

def _table_a_cle(ns_id: int) -> None:
    _store().set_schema(str(ns_id), {"key": "siren",
                                     "fields": [{"key": "siren", "type": "text"},
                                                {"key": "societe", "type": "text"}]})



def test_the_holder_MERGES_freely_through_its_run(table):
    """Le titulaire écrit sur SA ligne par clé métier — le chemin de la fusion."""
    from oto_mcp import db, session_org
    ns, ns_id = table
    _table_a_cle(ns_id)
    db.datastore_insert_row(ns_id, "rk", {"siren": "123456789", "societe": "Avant"})

    jeton = session_org.set_call_run("run-fusion")
    try:
        _store().claim_row(ns, "rk", worker="w1")     # SA ligne, désignée
        _store().write_rows(ns, [{"siren": "123456789", "societe": "Après"}],
                            key="siren")
    finally:
        session_org.reset_call_run(jeton)

    assert db.datastore_get_row(ns_id, "rk")["data"]["societe"] == "Après"


def test_ANOTHER_run_is_refused_on_the_merge_path(table):
    """La contre-épreuve : sans elle, le banc du dessus passerait aussi si la garde
    ne gardait plus rien."""
    from oto_mcp import db, session_org
    from oto_mcp.datastore.errors import RowLocked
    ns, ns_id = table
    _table_a_cle(ns_id)
    db.datastore_insert_row(ns_id, "rk", {"siren": "123456789", "societe": "Avant"})

    jeton = session_org.set_call_run("run-titulaire")
    try:
        _store().claim_row(ns, "rk", worker="w1")
    finally:
        session_org.reset_call_run(jeton)

    autre = session_org.set_call_run("run-intrus")
    try:
        with pytest.raises(RowLocked):
            _store().write_rows(ns, [{"siren": "123456789", "societe": "Volée"}],
                                key="siren")
    finally:
        session_org.reset_call_run(autre)
