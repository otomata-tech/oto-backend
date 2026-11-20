"""La file qui tourne à vide : N réservations sans écriture (#433).

Depuis que le run est lié à la ligne réservée, la conclusion d'un job libère la
ligne — c'est le design. Effet de bord mesuré au rodage d'une campagne : un agent
qui réserve, enquête et conclut SANS écrire rend sa ligne dans la minute, et le
job suivant la reprend pour refaire le même faux départ. Deux lignes servies deux
fois en dix minutes, aucune écriture, et rien qui le dise — les jobs se terminent
en `done`. Un budget se vide sans rien produire.

Le plafond se déclare au cycle de vie (`lifecycle.max_claims` + `abandon_state`) :
seul le serveur sait, PAR LIGNE, combien de fois elle a été réservée sans être
écrite. Le compteur repart à zéro à la première écriture réussie — c'est ce qui
distingue « reprise après un vrai travail » de « faux départ répété ».

Le banc tient sur un PostgreSQL RÉEL : ce qui est en cause est un compteur de
colonne, un filtre de pick et une libération de bail. Un magasin reconstitué
mesurerait la représentation qu'on s'en fait, pas la file.
"""
from __future__ import annotations

import logging
import contextlib
import uuid

import pytest


# Le tableau minimal d'une file : un statut qui porte le cycle de vie, un état
# d'abandon qui est bien terminal, et le plafond posé sur ce cycle.
#
# ⚠️ `echec` est terminal ET rouvrable : `terminal` est déclaré EXPLICITEMENT, donc
# une transition de retour ne le déclasse pas. C'est ce qui rend une ligne abandonnée
# réparable en statut — sans elle, la réparation reste possible sur les autres champs
# mais le statut, lui, est gelé (le cycle de vie refuse de sortir d'un terminal).
def _schema(**lifecycle) -> dict:
    lc = {"states": ["a_faire", "traite", "echec"],
          "transitions": {"a_faire": ["traite", "echec"], "echec": ["a_faire"]},
          "terminal": ["traite", "echec"]}
    lc.update(lifecycle)
    return {"fields": [
        {"key": "societe", "type": "text"},
        {"key": "statut", "type": "enum", "role": "status",
         "options": ["a_faire", "traite", "echec"], "lifecycle": lc},
    ]}


PLAFONNE = _schema(max_claims=3, abandon_state="echec")


def _store(sub="sub-agent"):
    from oto_mcp.datastore.core import make_store
    return make_store(sub)


@contextlib.contextmanager
def sous_le_run(run_id: str):
    """Le titulaire écrit SOUS SON RUN — la seule preuve de titularité (#317).

    Remplace `writing_as` le 07/09/2026, retiré parce qu'aucune surface ne
    l'atteignait. Le run doit couvrir la RÉSERVATION autant que l'écriture : c'est
    au claim que le bail enregistre à quel run il appartient. Un banc qui ne
    l'envelopperait qu'autour de l'écriture verrait un refus, et ce refus serait
    juste."""
    from oto_mcp import session_org
    jeton = session_org.set_call_run(run_id)
    try:
        yield
    finally:
        session_org.reset_call_run(jeton)


def _table(schema) -> tuple:
    """Un tableau neuf portant UNE ligne à traiter."""
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-agent", "file-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, schema)
    st.append_row(ns, {"societe": "ENTREPRISE TEMOIN", "statut": "a_faire"})
    return st, ns, ns_id


@pytest.fixture
def plafonne(live):
    return _table(PLAFONNE)


def _brut(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data, claims, abandon_reason, claimed_by, claimed_until "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict(r or {})


def _nom(ns_id: int) -> str:
    """Le nom affiché du tableau, que la trace cite."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("SELECT namespace FROM user_datastores WHERE id = %s",
                            (ns_id,)).fetchone()["namespace"]


def _un_id(st, ns) -> str:
    """L'id de l'unique ligne du tableau, sans passer par la file — la réserver
    pour connaître son id fausserait le compteur qu'on mesure."""
    return st.list_rows(ns)[0]["_id"]


def _expirer_le_bail(ns_id: int, row_id: str) -> None:
    """Le titulaire disparaît sans relâcher : son bail cesse de protéger."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET claimed_until = NOW() - interval '1 hour' "
                     "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id))


def _tourner_a_vide(st, ns, tours: int) -> str:
    """`tours` réservations suivies d'un relâchement, sans une seule écriture."""
    dernier = ""
    for _ in range(tours):
        row = st.claim_next(ns, worker="agent-1")
        assert row is not None, "la ligne devait encore être servie"
        dernier = row["_id"]
        st.release_claim(ns, dernier, worker="agent-1")
    return dernier


# ══ le fait : la ligne quitte la file ═══════════════════════════════════════

def test_la_ligne_reservee_n_fois_sans_ecriture_quitte_la_file(plafonne):
    """LE test qui manquait. Trois faux départs, et la quatrième réservation
    servait encore la même ligne — indéfiniment."""
    st, ns, _ = plafonne

    _tourner_a_vide(st, ns, 3)

    assert st.claim_next(ns, worker="agent-1") is None


def test_le_filet_ne_depend_pas_du_filtre_du_client(plafonne):
    """Le retrait de la file est un acte de PLATEFORME : un `claim_next` qui ne
    filtre pas sur le statut ne doit pas la servir non plus. Sans ça, la garde
    tiendrait à la discipline de l'appelant."""
    st, ns, _ = plafonne

    _tourner_a_vide(st, ns, 3)

    assert st.claim_next(ns, worker="autre", filter=None) is None
    assert st.claim_next(ns, worker="autre", filter={"statut": "a_faire"}) is None


def test_l_abandon_pose_l_etat_declare_et_libere_le_bail(plafonne):
    st, ns, ns_id = plafonne

    rid = _tourner_a_vide(st, ns, 3)

    ligne = _brut(ns_id, rid)
    assert ligne["data"]["statut"] == "echec"
    assert ligne["claimed_by"] is None and ligne["claimed_until"] is None
    assert ligne["data"]["societe"] == "ENTREPRISE TEMOIN"   # rien n'est perdu


def test_le_motif_cite_ses_chiffres(plafonne):
    """Un motif sans ses nombres se lit comme un verdict ; avec eux, il se
    vérifie — et il dit quel plafond était en vigueur ce jour-là."""
    st, ns, ns_id = plafonne

    rid = _tourner_a_vide(st, ns, 3)

    # Réservée hors run : rien où lire la cause, et le motif le dit (#491).
    motif = ("abandoned after 3 claims without a write, ceiling 3 — last "
             "attempt: unknown cause: claimed outside a run")
    assert _brut(ns_id, rid)["abandon_reason"] == motif
    lue = st.get_row(ns, rid)
    assert lue["_abandon"] == motif


def test_l_abandon_est_bruyant(plafonne, caplog):
    """Une ligne qui sort de la file sans que personne ne l'ait demandé se dit :
    le tableau, la ligne, le compteur."""
    st, ns, ns_id = plafonne

    with caplog.at_level(logging.WARNING, logger="oto_mcp.db.rowabandon"):
        rid = _tourner_a_vide(st, ns, 3)

    trace = caplog.text
    assert _nom(ns_id) in trace and rid in trace and "3" in trace


# ══ le compteur ═════════════════════════════════════════════════════════════

def test_le_compteur_est_rendu_dans_la_ligne_servie(plafonne):
    """Voir qu'une ligne a déjà été tentée est ce qui permet à l'agent — et au
    relecteur — de ne pas refaire le même faux départ."""
    st, ns, _ = plafonne

    premiere = st.claim_next(ns, worker="agent-1")
    assert premiere["_claims"] == 1
    st.release_claim(ns, premiere["_id"], worker="agent-1")

    seconde = st.claim_next(ns, worker="agent-1")
    assert seconde["_claims"] == 2


def test_une_ecriture_reussie_remet_le_compteur_a_zero(plafonne):
    """La distinction que porte tout le mécanisme : une reprise APRÈS travail
    n'est pas un faux départ. Deux réservations écrites entre chaque tour ne
    doivent jamais atteindre le plafond."""
    st, ns, ns_id = plafonne

    for tour in range(5):
        with sous_le_run(f"run-{tour}"):          # réservation ET écriture, même run
            row = st.claim_next(ns, worker="agent-1")
            assert row is not None
            st.update_row(ns, row["_id"], {"societe": "ENQUÊTE FAITE"})
        st.release_claim(ns, row["_id"], worker="agent-1")

    encore = st.claim_next(ns, worker="agent-1")
    assert encore is not None
    assert encore["_claims"] == 1         # le compteur repart de la dernière écriture
    assert _brut(ns_id, encore["_id"])["abandon_reason"] is None


def test_une_ecriture_a_la_main_remet_la_ligne_dans_le_circuit(plafonne):
    """La ligne abandonnée reste lisible ET réparable : rien n'est perdu, la file
    cesse simplement de tourner à vide dessus."""
    st, ns, ns_id = plafonne

    rid = _tourner_a_vide(st, ns, 3)
    assert st.claim_next(ns, worker="agent-1") is None

    st.update_row(ns, rid, {"statut": "a_faire", "societe": "REPRISE MANUELLE"})

    assert _brut(ns_id, rid)["abandon_reason"] is None
    reprise = st.claim_next(ns, worker="agent-1")
    assert reprise is not None and reprise["_id"] == rid
    assert reprise["_claims"] == 1


def test_rouvrir_une_ligne_abandonnee_exige_une_transition_de_retour(live):
    """Le cycle de vie garde la main sur son propre vocabulaire : la plateforme
    verse la ligne dans l'état d'abandon, elle ne s'autorise pas à l'en sortir.
    Sur un tableau qui ne déclare aucun retour, le statut reste donc gelé — les
    autres champs, eux, restent réparables (et rouvrent la file)."""
    from oto_mcp.datastore.errors import RowValidationError
    sans_retour = _schema(max_claims=3, abandon_state="echec",
                          transitions={"a_faire": ["traite", "echec"]})
    st, ns, ns_id = _table(sans_retour)

    rid = _tourner_a_vide(st, ns, 3)

    with pytest.raises(RowValidationError):
        st.update_row(ns, rid, {"statut": "a_faire"})
    st.update_row(ns, rid, {"societe": "ENQUÊTE REPRISE"})
    assert _brut(ns_id, rid)["abandon_reason"] is None      # la ligne rouvre quand même


# ══ réserver ≠ renouveler ═══════════════════════════════════════════════════

def test_le_renouvellement_du_titulaire_ne_consomme_pas_le_plafond(plafonne):
    """Une réservation, c'est PRENDRE une ligne : un nouveau titulaire, ou une
    ligne dont le bail a lâché. Le titulaire qui rafraîchit son écran ne la prend
    pas — elle ne lui a jamais échappé. Compter ce geste ferait payer le plafond
    à une file pilotée à la main, où rafraîchir est le geste le plus banal."""
    st, ns, ns_id = plafonne
    rid = _un_id(st, ns)

    premiere = st.claim_row(ns, rid, worker="ecran-sarah")
    assert premiere["_claims"] == 1

    encore = premiere
    for _ in range(4):
        encore = st.claim_row(ns, rid, worker="ecran-sarah")

    assert encore["_claims"] == 1
    assert _brut(ns_id, rid)["abandon_reason"] is None      # le plafond de 3 est intact


def test_reprendre_une_ligne_dont_le_bail_a_lache_compte(plafonne):
    """L'autre moitié de la règle : dès que le bail ne protège plus, la ligne
    était reprenable par n'importe qui — la reprendre EST une réservation, que ce
    soit un collègue ou le même écran revenu plus tard."""
    st, ns, ns_id = plafonne
    rid = _un_id(st, ns)

    st.claim_row(ns, rid, worker="ecran-sarah")
    _expirer_le_bail(ns_id, rid)
    autre = st.claim_row(ns, rid, worker="ecran-jules")
    assert autre["_claims"] == 2

    _expirer_le_bail(ns_id, rid)
    revenu = st.claim_row(ns, rid, worker="ecran-jules")    # le MÊME, après expiration
    assert revenu["_claims"] == 3


def test_claim_next_ne_peut_pas_servir_une_ligne_sous_bail_actif(plafonne):
    """Le pick n'a pas besoin de la même nuance : sa clause d'éligibilité exclut
    déjà le bail actif, donc il ne renouvelle jamais rien. Gravé pour que ça
    reste vrai — c'est ce qui rend le compteur juste des deux côtés."""
    st, ns, ns_id = plafonne

    tenue = st.claim_next(ns, worker="agent-1")
    assert tenue["_claims"] == 1

    assert st.claim_next(ns, worker="agent-2") is None
    assert st.claim_next(ns, worker="agent-1") is None      # même le titulaire
    assert _brut(ns_id, tenue["_id"])["claims"] == 1        # rien n'a bougé


# ══ le filet des baux expirés ═══════════════════════════════════════════════

def test_un_bail_expire_sans_relachement_compte_aussi(plafonne):
    """L'agent qui MEURT ne relâche rien : son bail expire, et la ligne
    redevient servable. Sans évaluation au claim, ce chemin-là contournerait le
    plafond indéfiniment."""
    st, ns, ns_id = plafonne

    rid = ""
    for _ in range(3):
        row = st.claim_next(ns, worker="agent-mort")
        assert row is not None
        rid = row["_id"]
        _expirer_le_bail(ns_id, rid)      # le bail lâche, personne ne relâche

    assert st.claim_next(ns, worker="agent-mort") is None
    assert _brut(ns_id, rid)["data"]["statut"] == "echec"


def test_une_ligne_sous_bail_actif_n_est_pas_abandonnee(plafonne):
    """Le titulaire du bail courant n'a pas encore rendu son verdict : lui
    retirer la ligne pendant qu'il travaille serait précisément la course que le
    bail existe pour empêcher."""
    st, ns, ns_id = plafonne

    _tourner_a_vide(st, ns, 2)
    with sous_le_run("run-3"):
        tenue = st.claim_next(ns, worker="agent-1")   # 3e réservation, en cours

        assert tenue is not None and tenue["_claims"] == 3
        assert _brut(ns_id, tenue["_id"])["abandon_reason"] is None
        st.update_row(ns, tenue["_id"], {"statut": "traite"})  # il écrit : rien n'est perdu
    assert _brut(ns_id, tenue["_id"])["claims"] == 0


# ══ sans déclaration, le plafond de PLATEFORME (oto#101) ═════════════════════

def test_sans_declaration_le_plafond_par_defaut_met_la_ligne_de_cote(live):
    """La garde était OPT-IN : un tableau qui ne déclarait pas de plafond resservait
    la même ligne indéfiniment — le livelock d'oto#101. Le plafond de plateforme
    (3) la met de côté par la colonne de plateforme, sans toucher à son statut :
    aucun état ne lui est désigné."""
    st, ns, ns_id = _table(_schema())

    rid = _tourner_a_vide(st, ns, 3)

    assert st.claim_next(ns, worker="agent-1") is None
    brut = _brut(ns_id, rid)
    assert "ceiling 3 (platform default" in brut["abandon_reason"]
    assert brut["data"]["statut"] == "a_faire"


def test_le_parametre_ne_descend_pas_sous_le_plafond_par_defaut(live):
    """#132 vaut aussi pour le plafond de plateforme : le paramètre de l'appel
    l'assouplit, il ne le serre jamais — `max_claims=1` ne sort pas la ligne au
    premier faux départ."""
    st, ns, ns_id = _table(_schema())

    for tour in (1, 2):
        row = st.claim_next(ns, worker="agent-1", max_claims=1)
        assert row is not None, f"la ligne devait encore être servie au tour {tour}"
        st.release_claim(ns, row["_id"], worker="agent-1")
        assert _brut(ns_id, row["_id"])["abandon_reason"] is None

    # C'est le plafond en vigueur qui décide, et il finit par mordre.
    rid = _tourner_a_vide(st, ns, 1)
    assert st.claim_next(ns, worker="agent-1", max_claims=1) is None
    assert "ceiling 3" in _brut(ns_id, rid)["abandon_reason"]


# ══ la déclaration se valide à la pose ══════════════════════════════════════

def test_un_plafond_exige_un_etat_d_abandon():
    from oto_mcp.datastore.schema import validate_schema_def
    erreurs = validate_schema_def(_schema(max_claims=3))
    assert any("abandon_state" in e for e in erreurs)


def test_l_etat_d_abandon_doit_etre_terminal():
    """Verser une ligne dans un état d'où le cycle de vie repart, c'est la
    remettre dans la file qu'elle vient de quitter."""
    from oto_mcp.datastore.schema import validate_schema_def
    erreurs = validate_schema_def(_schema(max_claims=3, abandon_state="a_faire"))
    assert any("abandon_state" in e and "terminal" in e for e in erreurs)


@pytest.mark.parametrize("valeur", [0, -1, "3", 1.5, True])
def test_un_plafond_doit_etre_un_entier_positif(valeur):
    from oto_mcp.datastore.schema import validate_schema_def
    erreurs = validate_schema_def(_schema(max_claims=valeur, abandon_state="echec"))
    assert any("max_claims" in e for e in erreurs)


def test_une_declaration_complete_passe():
    from oto_mcp.datastore.schema import validate_schema_def
    assert validate_schema_def(PLAFONNE) == []
