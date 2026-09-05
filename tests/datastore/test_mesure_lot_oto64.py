"""Ce qu'un lot fait vraiment quand une de ses lignes est refusée (oto#64).

Le signal 726 dit « *would* fail the whole batched write » — l'agent n'a pas tenté, il
a préféré ne rien écrire et confier la reprise à un humain. Le comportement était donc
**rapporté, pas mesuré**, et l'issue demandait de le mesurer avant de décider.

**Mesuré le 05/09/2026 : la prémisse est fausse.** Le lot n'est ni atomique ni
par-ligne — il s'ARRÊTE à la première ligne refusée :

 - les lignes qui précèdent sont **écrites** et le restent ;
 - la ligne fautive est refusée ;
 - celles qui suivent ne sont **pas tentées** ;
 - et le refus dit déjà les trois : quelle ligne, combien sont écrites, où reprendre.

⚠️ **Ce qui reste vrai du signal**, et que ce fichier grave aussi : quand la ligne
fautive est la PREMIÈRE, rien n'est écrit — l'appelant perd bien tout son lot. C'est ce
cas-là, et non « le lot entier tombe toujours », qui coûte en production.

**Deux des trois sont corrigés le même jour** : le refus nomme désormais la sortie, et
la fusion de `transitions` descend par état — dans cet ordre, parce qu'un message qui
apprend un geste doit attendre que le geste soit sûr. Le troisième, le contrat du lot,
ne bouge pas : ce qui coûte est la fautive en TÊTE, pas la chute du lot, et un refus
partiel changerait le contrat de `data_write` pour un cas qui n'est pas celui-là.

Les bancs qui restent DATENT le comportement du lot. Le changer devra les faire tomber
délibérément, pas par surprise.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore.errors import RowValidationError


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_org_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


#: Un cycle de vie avec des états TERMINAUX : `gagne` et `perdu` ne mènent nulle part.
#: ⚠️ Le cycle de vie vit SUR LE CHAMP `role="status"`, pas à la racine du schéma.
SCHEMA = {
    "key": "ref",
    "fields": [
        {"key": "ref", "type": "text"},
        {"key": "statut", "type": "text", "role": "status",
         "values": ["neuf", "en_cours", "gagne", "perdu"],
         "lifecycle": {"states": ["neuf", "en_cours", "gagne", "perdu"],
                       "transitions": {"neuf": ["en_cours"],
                                       "en_cours": ["gagne", "perdu"]},
                       "terminal": ["gagne", "perdu"]}},
    ],
}


def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-lot", ns)
    st = make_store("sub-lot")
    st.set_schema(ns, SCHEMA)
    return st, ns


def _etats(st, ns) -> dict:
    return {r["ref"]: r["statut"] for r in st.list_rows(ns)}


def _id(st, ns, ref):
    for r in st.list_rows(ns):
        if r["ref"] == ref:
            return r["_id"]
    raise AssertionError(f"ligne {ref} introuvable")


def _six_lignes(st, ns, *, enfermee: str):
    """Six lignes `neuf` ; celle nommée est menée jusqu'à un état terminal."""
    st.write_rows(ns, [{"ref": f"r{i}", "statut": "neuf"} for i in range(1, 7)])
    rid = _id(st, ns, enfermee)
    st.update_row(ns, rid, {"statut": "en_cours"})
    st.update_row(ns, rid, {"statut": "perdu"})


def test_le_lot_s_ARRETE_a_la_ligne_refusee_il_ne_tombe_pas_en_entier(live):
    """La mesure qui contredit le signal : trois lignes passent, la quatrième est
    refusée, les deux dernières ne sont pas tentées."""
    st, ns = _table()
    _six_lignes(st, ns, enfermee="r4")

    with pytest.raises(RowValidationError) as e:
        st.write_rows(ns, [{"ref": f"r{i}", "statut": "en_cours"} for i in range(1, 7)])

    etats = _etats(st, ns)
    assert [etats[f"r{i}"] for i in range(1, 7)] == [
        "en_cours", "en_cours", "en_cours",   # écrites, et elles le restent
        "perdu",                              # refusée : rien n'a bougé
        "neuf", "neuf",                       # jamais tentées
    ]
    assert "transition 'perdu' → 'en_cours' interdite" in str(e.value)


def test_le_refus_dit_OU_reprendre_pas_seulement_ce_qui_a_echoue(live):
    """⚠️ C'est ce qui rend l'arrêt supportable : sans la position et le décompte,
    l'appelant ne peut pas rejouer la queue du lot sans rejouer aussi ce qui est déjà
    écrit. Le message porte les trois — quelle ligne, combien avant, où reprendre."""
    st, ns = _table()
    _six_lignes(st, ns, enfermee="r4")

    with pytest.raises(RowValidationError) as e:
        st.write_rows(ns, [{"ref": f"r{i}", "statut": "en_cours"} for i in range(1, 7)])

    message = str(e.value)
    assert "ligne 4/6 du lot" in message
    assert "ref=r4" in message
    assert "3 lignes déjà écrites avant l'arrêt, aucune après" in message
    assert "reprends le lot à la ligne 4" in message


def test_la_ligne_fautive_EN_TETE_fait_tout_perdre(live):
    """⚠️ **Le cas qui coûte, et le seul où le signal a raison.** Quand la première
    ligne est refusée, rien n'est écrit : l'appelant a bien tout perdu, et il n'a aucune
    ligne « déjà écrite » sur laquelle s'appuyer pour reprendre.

    Le message le dit — « aucune ligne écrite avant l'arrêt » — mais le dire ne rend pas
    le lot. C'est ce cas-là que le journal de production montre le plus souvent."""
    st, ns = _table()
    _six_lignes(st, ns, enfermee="r1")

    with pytest.raises(RowValidationError) as e:
        st.write_rows(ns, [{"ref": f"r{i}", "statut": "en_cours"} for i in range(1, 7)])

    assert _etats(st, ns) == {"r1": "perdu", "r2": "neuf", "r3": "neuf",
                              "r4": "neuf", "r5": "neuf", "r6": "neuf"}
    assert "aucune ligne écrite avant l'arrêt" in str(e.value)


def test_la_SORTIE_d_un_etat_terminal_est_DEJA_declarable(live):
    """⚠️ **Mesuré, et ça renverse le point (1) de l'issue.** « Permettre une transition
    de sortie d'un état terminal » n'est pas à construire : il suffit de la DÉCLARER
    dans `lifecycle.transitions`, et elle passe — avec `terminal` explicite comme sans.

    Ce qui manque n'est donc pas la capacité, c'est de le SAVOIR. Le refus, lui, dit
    « (état terminal) » sans dire qu'une sortie se déclare : il ferme sans montrer la
    porte, et l'appelant en déduit qu'il n'y en a pas. C'est exactement ce qui s'est
    passé — l'agent a préféré ne rien écrire et rendre la main à un humain."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-lot", ns)
    st = make_store("sub-lot")
    st.set_schema(ns, {"key": "ref", "fields": [
        {"key": "ref", "type": "text"},
        {"key": "statut", "type": "text", "role": "status",
         "values": ["neuf", "en_cours", "perdu"],
         # `perdu` reste déclaré terminal, ET porte une transition de retour.
         "lifecycle": {"states": ["neuf", "en_cours", "perdu"],
                       "transitions": {"neuf": ["en_cours"],
                                       "en_cours": ["perdu"],
                                       "perdu": ["en_cours"]},
                       "terminal": ["perdu"]}}]})
    st.write_rows(ns, [{"ref": "a", "statut": "neuf"}])
    rid = _id(st, ns, "a")
    st.update_row(ns, rid, {"statut": "en_cours"})
    st.update_row(ns, rid, {"statut": "perdu"})

    st.update_row(ns, rid, {"statut": "en_cours"})       # la sortie déclarée passe
    assert _etats(st, ns) == {"a": "en_cours"}


def test_declarer_UNE_transition_laisse_les_AUTRES_en_place(live):
    """⚠️ **Le défaut le plus grave des trois, corrigé — et c'était le geste de
    RÉPARATION qui le déclenchait.** Jusqu'au 05/09/2026, celui qui déclarait la sortie
    de la façon naturelle — ne nommer que ce qu'il ajoute — effaçait toutes les autres
    transitions, silencieusement : `merge_lifecycle` remplaçait `transitions` en bloc.

    C'est le défaut que cette fusion avait corrigé un cran plus haut le 29/08 (« en
    oublier un les faisait disparaître sans un mot, la promesse inverse de
    `data_patch_schema` ») : elle était descendue d'un cran, pas de deux.

    Ce banc tombe si la fusion REMONTE."""
    from oto_mcp import db
    from oto_mcp.datastore import schema as dsv2
    from oto_mcp.datastore.core import make_store

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-lot", ns)
    st = make_store("sub-lot")
    st.set_schema(ns, SCHEMA)

    st.patch_schema(ns, fields=[{"key": "statut",
                                 "lifecycle": {"transitions": {"perdu": ["en_cours"]}}}])

    lc = dsv2.lifecycle_of(st._schema_of(st.resolve_ns_id(ns)))
    assert lc["transitions"] == {"neuf": ["en_cours"],
                                 "en_cours": ["gagne", "perdu"],
                                 "perdu": ["en_cours"]}
    assert lc["states"] == ["neuf", "en_cours", "gagne", "perdu"]
    assert lc["terminal"] == ["gagne", "perdu"]

    # …et la ligne enfermée repart pour de bon, sans autre geste.
    _six_lignes(st, ns, enfermee="r1")
    st.update_row(ns, _id(st, ns, "r1"), {"statut": "en_cours"})
    assert _etats(st, ns)["r1"] == "en_cours"


def test_RETIRER_une_transition_est_explicite(live):
    """Le pendant obligé de la fusion : sans retrait nommé, plus de nettoyage. C'est ce
    que le changement coûte à l'appelant, et il est dit dans la description servie —
    on ne préavise pas la fin d'une destruction silencieuse, mais on la nomme."""
    from oto_mcp import db
    from oto_mcp.datastore import schema as dsv2
    from oto_mcp.datastore.core import make_store

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-lot", ns)
    st = make_store("sub-lot")
    st.set_schema(ns, SCHEMA)

    st.patch_schema(ns, fields=[{"key": "statut",
                                 "lifecycle": {"transitions": {"neuf": None}}}])
    lc = dsv2.lifecycle_of(st._schema_of(st.resolve_ns_id(ns)))
    assert lc["transitions"] == {"en_cours": ["gagne", "perdu"]}

    # Et la table entière se retire toujours d'un coup.
    st.patch_schema(ns, fields=[{"key": "statut", "lifecycle": {"transitions": None}}])
    lc = dsv2.lifecycle_of(st._schema_of(st.resolve_ns_id(ns)))
    assert "transitions" not in lc


def test_le_refus_NOMME_la_porte_et_le_patch_qu_il_conseille_ne_DETRUIT_rien(live):
    """⚠️ Deux exigences dans un seul banc, parce qu'elles ne valent qu'ensemble.

    Le refus doit nommer la sortie — sans quoi l'appelant conclut qu'il n'y en a pas,
    ce qui a produit le signal 726. Et le patch qu'il conseille doit porter les
    destinations DÉJÀ autorisées en plus de la nouvelle : la fusion descend par état,
    mais la LISTE d'un état se remplace. Conseiller la nouvelle seule reformerait, dans
    le message écrit pour l'éviter, le défaut qu'on vient de fermer."""
    from oto_mcp.datastore import schema as dsv2

    terminal = dsv2.refus_de_transition("statut", "perdu", "en_cours", [])
    assert "data_patch_schema" in terminal
    assert '{"perdu": ["en_cours"]}' in terminal

    saut = dsv2.refus_de_transition("status", "recorded", "pushed",
                                    ["drafted", "excluded"])
    assert '{"recorded": ["drafted", "excluded", "pushed"]}' in saut, (
        "le patch conseillé perdrait les sorties existantes de cet état")


def test_un_etat_terminal_ENFERME_la_ligne_meme_seule(live):
    """L'autre moitié d'oto#64, indépendante du lot : une ligne parvenue à un état
    terminal ne peut plus repartir, même écrite seule. Juste pour le cas nominal, faux
    dès qu'une entité redevient active.

    ⚠️ L'enfermement vient du SCHÉMA POSÉ, pas du produit : la sortie se déclare (banc
    précédent). Ce qui manque est que le refus ne l'enseigne pas."""
    st, ns = _table()
    _six_lignes(st, ns, enfermee="r1")

    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, _id(st, ns, "r1"), {"statut": "en_cours"})
    assert "état terminal" in str(e.value)
