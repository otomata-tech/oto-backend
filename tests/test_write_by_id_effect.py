"""Une écriture du datastore a un EFFET, ou elle REFUSE en nommant (#349).

Trois défauts du même principe en une soirée (#329 les couches, #347 `required_when`,
#349 celui-ci) : tous trouvés en vérifiant l'effet, aucun en lisant le retour. Le
troisième comportement — accepter, rendre un succès, ne rien faire — est celui qui
coûte, parce que le refus est bruyant et que la divergence est muette.

Ce banc pose donc la règle sur les chemins d'écriture de LIGNES, un par un : ajout,
fusion sur clé métier, lot, patch par identifiant,
suppression, bail. Pour chacun, une seule question — **la base a-t-elle bougé ?** —
posée à la base, jamais au retour de l'appel, et jamais à un store stubé : un banc
qui reconstitue le magasin mesure la représentation qu'on s'en fait.

Le cas nommé de #349 est le patch par `id` dont le payload NE PORTE PAS la clé
métier, sur un tableau `key` + `strict` : l'identifiant technique est une désignation
COMPLÈTE de la ligne, donc l'écriture suffit et écrit. C'est
`test_le_patch_par_id_sans_la_cle_metier_ecrit` — la ligne de défense de l'incident,
et la garde à tenir le jour où le dispatch par clé métier sera retouché.
"""
from __future__ import annotations

import uuid

import pytest



# Le tableau de l'incident, réduit à ce qui porte la règle : une clé métier
# déclarée, un format strict, et une colonne-liste d'objets (les contacts qu'un
# script de reprise portait ligne à ligne).
SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison_sociale", "type": "text"},
        {"key": "contacts", "type": "list",
         "of": {"type": "object",
                "fields": [{"key": "nom"}, {"key": "fonction"}, {"key": "email"}]}},
    ],
}

CONTACTS = [{"nom": "Jo Mercier", "fonction": "Gérante", "email": "jo@x.fr"}]


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def table(live):
    """Un tableau `key` + `strict`, et UNE ligne témoin déjà posée."""
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)

    st = _store()
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "552032534", "raison_sociale": "TEMOIN"})
    return st, ns, ns_id, row["_id"]


# --- ce que porte la BASE, jamais ce que le store a bien voulu rendre ---------

def _donnees(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _cardinal(ns_id: int) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s",
            (ns_id,)).fetchone()["n"]


def _antidater(ns_id: int, row_id: str) -> None:
    """Recule `updated_at` d'un an.

    `_updated_at` inchangé était le SEUL indice de l'incident, et personne ne le
    compare — il mérite donc d'être vérifié. Le comparer à `NOW()` ne le peut pas :
    le dépôt normalise l'horodatage à la SECONDE (`db/_conn._str_dict_row`), et deux
    écritures d'un même test tombent dans la même. Reculer la ligne d'abord rend la
    mesure indépendante de la vitesse du test."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET updated_at = updated_at - interval "
                     "'1 year' WHERE ns_id = %s AND row_id = %s", (ns_id, row_id))


def _horodatage(ns_id: int, row_id: str) -> str:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT updated_at FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()["updated_at"]


# ══ le cas nommé : le patch par identifiant ═════════════════════════════════

def test_le_patch_par_id_sans_la_cle_metier_ecrit(table):
    """LE cas de #349 : la clé métier ABSENTE du payload.

    Un `_id` désigne complètement la ligne — exiger en plus la clé métier serait
    imposer à l'appelant un détail du dispatch d'upsert. L'écriture a lieu, et
    l'écho décrit ce qui est en base : pendant l'incident, le retour était
    parfaitement crédible, c'est la base qu'il fallait interroger."""
    st, ns, ns_id, rid = table

    echo = st.update_row(ns, rid, {"contacts": CONTACTS})

    assert _donnees(ns_id, rid).get("contacts") == CONTACTS, \
        "annoncé écrit, rien en base : le succès inerte est exclu (#349)"
    assert echo.get("contacts") == CONTACTS, "l'écho doit décrire ce qui est en base"
    assert _donnees(ns_id, rid).get("siren") == "552032534", \
        "un patch ne touche que ce qu'il nomme — la clé métier demeure"


def test_le_patch_par_id_avec_la_cle_metier_ecrit_pareil(table):
    """Le round-trip (relire une ligne entière, la modifier, la repousser) écrit
    par le MÊME chemin : porter la clé ne doit rien changer, dans un sens ni dans
    l'autre."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"siren": "552032534", "contacts": CONTACTS})

    assert _donnees(ns_id, rid).get("contacts") == CONTACTS
    assert _donnees(ns_id, rid).get("siren") == "552032534"


def test_le_patch_par_id_horodate_la_ligne(table):
    """L'horodatage est ce qui rend une écriture VÉRIFIABLE de l'extérieur."""
    st, ns, ns_id, rid = table
    _antidater(ns_id, rid)
    avant = _horodatage(ns_id, rid)

    st.update_row(ns, rid, {"contacts": CONTACTS})

    assert _horodatage(ns_id, rid) > avant


def test_le_patch_dun_id_inconnu_refuse_en_nommant(table):
    """L'autre issue honorable : un refus qui dit ce qui manque."""
    from oto_mcp.datastore.core import RowNotFound
    st, ns, ns_id, _rid = table

    with pytest.raises(RowNotFound):
        st.update_row(ns, "019f-inexistant", {"contacts": CONTACTS})
    assert _cardinal(ns_id) == 1, "un refus n'invente pas de ligne"


def test_le_patch_qui_duplique_la_cle_metier_refuse_en_nommant(table):
    """L'index d'unicité garde son mot à dire : viser par `id` ne permet pas de
    poser une valeur de clé déjà prise par une AUTRE ligne. Le refus nomme le
    champ — un 500 obligerait à deviner."""
    st, ns, ns_id, _rid = table
    autre = st.append_row(ns, {"siren": "999999999", "raison_sociale": "AUTRE"})

    with pytest.raises(ValueError) as exc:
        st.update_row(ns, autre["_id"], {"siren": "552032534"})
    assert "siren" in str(exc.value)
    assert _donnees(ns_id, autre["_id"]).get("siren") == "999999999", \
        "refusée ⟹ rien d'écrit, pas même partiellement"


# ══ les autres chemins d'écriture : effet, ou refus nommé ═══════════════════

def test_lajout_dune_ligne_neuve_a_un_effet(table):
    st, ns, ns_id, _rid = table

    cree = st.append_row(ns, {"siren": "111222333", "raison_sociale": "NEUVE"})

    assert _cardinal(ns_id) == 2
    assert _donnees(ns_id, cree["_id"]).get("raison_sociale") == "NEUVE"


def test_lajout_sur_une_cle_metier_connue_fusionne_sans_dupliquer(table):
    """La dédup par clé métier est un chemin d'écriture à part entière : elle doit
    ÉCRIRE sur la ligne existante, pas se contenter de ne pas dupliquer. Un ajout ne
    fusionne que sur demande (`upsert`, oto#141)."""
    st, ns, ns_id, rid = table

    st.append_row(ns, {"siren": "552032534", "contacts": CONTACTS}, upsert=True)

    assert _cardinal(ns_id) == 1, "même clé ⟹ pas de doublon"
    assert _donnees(ns_id, rid).get("contacts") == CONTACTS, "et la fusion écrit"
    assert _donnees(ns_id, rid).get("raison_sociale") == "TEMOIN", "sans rien perdre"


def test_le_lot_ecrit_ses_deux_regimes(table):
    """Un lot mêle les deux : ce qui porte une clé connue FUSIONNE, le reste est
    AJOUTÉ. Le récap doit décrire ce qui a eu lieu, pas ce qui a été soumis."""
    st, ns, ns_id, rid = table

    recap = st.write_rows(ns, [
        {"siren": "552032534", "contacts": CONTACTS},        # fusion
        {"siren": "444555666", "raison_sociale": "DU LOT"},  # ajout
    ], upsert=True)

    assert (recap["updated"], recap["inserted"]) == (1, 1)
    assert _cardinal(ns_id) == 2
    assert _donnees(ns_id, rid).get("contacts") == CONTACTS


def test_la_suppression_a_un_effet_et_refuse_ce_quelle_ne_trouve_pas(table):
    from oto_mcp.datastore.core import RowNotFound
    st, ns, ns_id, rid = table

    st.delete_row(ns, rid)
    assert _cardinal(ns_id) == 0

    with pytest.raises(RowNotFound):
        st.delete_row(ns, rid)


def test_le_bail_se_pose_et_se_leve_pour_de_vrai(table):
    """Réserver puis libérer sont des écritures : elles se prouvent sur la ligne,
    pas sur le booléen rendu."""
    from oto_mcp.db._conn import _connect
    st, ns, ns_id, rid = table

    def _titulaire():
        with _connect() as conn:
            return conn.execute(
                "SELECT claimed_by FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
                (ns_id, rid)).fetchone()["claimed_by"]

    pris = st.claim_next(ns, worker="w-1")
    assert pris and pris["_id"] == rid and _titulaire() == "w-1"

    assert st.release_claim(ns, rid, worker="w-1")["released"] is True
    assert _titulaire() is None

    # ⚠️ Et la SECONDE libération, sur une base réelle : c'est le cas que le booléen
    # seul rendait indiscernable d'un échec (#517, 29/08). Il n'y a plus de bail —
    # bénin — et la réponse doit le dire, pas laisser l'appelant le deviner.
    encore = st.release_claim(ns, rid, worker="w-1")
    assert (encore["released"], encore["reason"]) == (False, "no_lease")


def test_ecrire_sur_une_ligne_reservee_par_un_autre_refuse_en_disant_quoi_faire(table):
    """La protection du bail doit REFUSER, jamais avaler l'écriture : c'est le même
    principe, vu du côté du garde-fou."""
    from oto_mcp.datastore.core import RowLocked
    st, ns, ns_id, rid = table
    st.claim_row(ns, rid, worker="w-1")

    with pytest.raises(RowLocked) as exc:
        st.update_row(ns, rid, {"contacts": CONTACTS})

    assert "_run_id" in str(exc.value), "le refus mène par la faute la plus fréquente (#515)"
    assert "w-1" not in str(exc.value), "et ne nomme pas le titulaire"
    assert "contacts" not in _donnees(ns_id, rid), "refusée ⟹ rien d'écrit"
