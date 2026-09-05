"""Ce que la couche `origine` garde vraiment — otomata-tech/oto#45.

Elle est présentée comme « la plateforme garde la valeur précédente ». C'est vrai,
et incomplet d'une façon qui compte : la capture a lieu **une fois**, à la première
écriture qui CHANGE la valeur **après la déclaration du format**. Trois conséquences
que ces épreuves figent, parce qu'elles décident si on peut rendre une valeur à qui
l'a fournie :

 * la face d'appel n'y change rien — mesuré, une ligne créée par l'outil agent et
   une ligne créée par la face REST se comportent à l'identique. C'était l'hypothèse
   de départ du signalement, et elle est fausse ;
 * un format déclaré TARD capture ce que le dernier écrivain a laissé — donc
   possiblement la valeur d'un autre agent, nommée `origine` comme si elle venait
   du propriétaire de la donnée. C'est le défaut réel, et il est pire qu'une couche
   absente : une couche absente dit « je ne sais pas », une couche fausse invite à
   rétablir le chiffre de quelqu'un d'autre ;
 * réécrire la MÊME valeur ne capture rien, à dessein.
"""
from __future__ import annotations

import uuid

import pytest


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


SCHEMA = {"key": "ref", "fields": [
    {"key": "ref", "type": "text"},
    {"key": "priorite", "type": "text", "origine": "system"},
]}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _table(schema=SCHEMA):
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, schema)
    return st, ns, ns_id


class _Ctx:
    sub = "sub-test"
    org = None
    role = "member"


def test_la_face_de_creation_ne_change_RIEN_a_la_couche(live):
    """L'hypothèse du signalement — « une ligne créée par REST ne garde pas son
    origine » — est FAUSSE. Les deux faces appellent le même store ; l'épreuve le
    fige pour qu'on ne reparte pas sur cette piste."""
    st, ns_a, id_a = _table()
    ligne_a = st.append_row(ns_a, {"ref": "r1", "priorite": "1"})
    st.update_row(ns_a, ligne_a["_id"], {"priorite": "3"})

    from oto_mcp.capabilities.datastore import rows as R

    st_b, ns_b, id_b = _table()
    cree = R._append_row(_Ctx(), R.AppendRowInput(
        namespace=ns_b, row={"ref": "r1", "priorite": "1"}))
    rid_b = cree.get("_id") or (cree.get("row") or {}).get("_id")
    st_b.update_row(ns_b, rid_b, {"priorite": "3"})

    attendu = {"valeur": "3", "origine": "1"}
    assert _blob(id_a, ligne_a["_id"])["priorite"] == attendu
    assert _blob(id_b, rid_b)["priorite"] == attendu


def test_declarer_le_format_capture_la_valeur_courante(live):
    """LE geste d'oto#46. Avant, déclarer le format ne touchait aucune ligne : une
    ligne existante n'avait donc AUCUN filet jusqu'à sa prochaine écriture, et la
    valeur d'alors était perdue sans que rien ne le dise.

    Ce qui est gardé est la valeur telle qu'elle était À LA DÉCLARATION. Le nom dit
    QUAND, pas QUI : si des agents avaient déjà écrit, c'est leur valeur — et c'est
    exactement ce que la promesse annonce désormais."""
    from oto_mcp import db

    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "ref", "fields": [
        {"key": "ref", "type": "text"}, {"key": "priorite", "type": "text"}]})

    ligne = st.append_row(ns, {"ref": "r1", "priorite": "1"})
    st.update_row(ns, ligne["_id"], {"priorite": "2"})
    assert _blob(ns_id, ligne["_id"])["priorite"] == "2"

    out = st.patch_schema(ns, fields=[{"key": "priorite", "origine": "system"}])
    # La capture a lieu À LA DÉCLARATION, et elle se DIT : une écriture sur des
    # lignes existantes ne se fait pas en silence.
    assert out.get("origines_capturees") == 1, out
    assert _blob(ns_id, ligne["_id"])["priorite"] == {"valeur": "2", "origine": "2"}

    # Ensuite la couche ne bouge plus : la valeur change, l'origine tient.
    st.update_row(ns, ligne["_id"], {"priorite": "3"})
    assert _blob(ns_id, ligne["_id"])["priorite"] == {"valeur": "3", "origine": "2"}


def test_declarer_le_format_ne_touche_PAS_une_origine_deja_posee(live):
    """L'autre sens. Capturer par-dessus effacerait justement ce qu'on garde —
    et une re-déclaration du même schéma est un geste banal."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"ref": "r1", "priorite": "1"})
    st.update_row(ns, ligne["_id"], {"priorite": "2"})
    assert _blob(ns_id, ligne["_id"])["priorite"] == {"valeur": "2", "origine": "1"}

    out = st.set_schema(ns, SCHEMA)          # re-déclaration à l'identique
    assert "origines_capturees" not in out, (
        "re-déclarer un format déjà là ne doit RIEN recapturer : sinon chaque pose "
        "de schéma écraserait les origines gardées")
    assert _blob(ns_id, ligne["_id"])["priorite"] == {"valeur": "2", "origine": "1"}


def test_une_colonne_absente_de_la_ligne_ne_recoit_pas_d_origine(live):
    """« Pas de valeur » n'est pas « valeur vide » : poser `""` inventerait une
    origine qui n'a jamais existé."""
    from oto_mcp import db

    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "ref", "fields": [
        {"key": "ref", "type": "text"}, {"key": "priorite", "type": "text"}]})
    ligne = st.append_row(ns, {"ref": "r1"})          # pas de `priorite`
    st.patch_schema(ns, fields=[{"key": "priorite", "origine": "system"}])
    assert "priorite" not in _blob(ns_id, ligne["_id"])


def test_reecrire_la_meme_valeur_ne_capture_rien(live):
    """À dessein : relire puis repousser n'est pas une modification, et une colonne
    plate doit rester plate."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"ref": "r1", "priorite": "1"})
    st.update_row(ns, ligne["_id"], {"priorite": "1"})
    assert _blob(ns_id, ligne["_id"])["priorite"] == "1"


def test_une_colonne_SANS_le_format_ne_garde_rien(live):
    """Le cas le plus courant, et le plus silencieux : sans le format, écraser est
    définitif et rien ne le signale."""
    st, ns, ns_id = _table({"key": "ref", "fields": [
        {"key": "ref", "type": "text"}, {"key": "priorite", "type": "text"}]})
    ligne = st.append_row(ns, {"ref": "r1", "priorite": "1"})
    st.update_row(ns, ligne["_id"], {"priorite": "3"})
    assert _blob(ns_id, ligne["_id"])["priorite"] == "3", "aucune trace de « 1 »"


def test_la_couche_ne_se_reecrit_jamais(live):
    """Une fois posée, elle est fermée : c'est ce qui rend la valeur gardée fiable
    dans le seul cas où elle l'est."""
    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"ref": "r1", "priorite": "1"})
    st.update_row(ns, ligne["_id"], {"priorite": "2"})
    st.update_row(ns, ligne["_id"], {"priorite": "3"})
    assert _blob(ns_id, ligne["_id"])["priorite"] == {"valeur": "3", "origine": "1"}


def test_le_texte_servi_dit_ce_qu_une_ecriture_detruit():
    """L'agent lit la description à chaque appel : c'est le seul endroit où on peut
    l'arrêter avant qu'il n'écrase une valeur qu'il n'a pas fournie."""
    import asyncio
    import importlib

    from fastmcp import FastMCP

    m = FastMCP("t")
    importlib.import_module("oto_mcp.tools.datastore").register(m)
    doc = asyncio.run(m.get_tool("data_write")).description or ""
    assert "DESTROYS" in doc, doc[:400]
    assert "origine" in doc and "when the format was declared" in doc
    assert "says when, not who" in doc, (
        "la promesse doit dire ce qu'elle NE dit pas : le nom porte le moment, "
        "pas l'auteur de la valeur")


def test_la_capture_tient_sur_une_table_de_dix_mille_lignes(live):
    """Ce que la déclaration COÛTE, mesuré — pas supposé.

    Déclarer le format écrit sur chaque ligne existante. Sur une table réelle, ça
    doit rester un geste qu'on ose faire : une transaction, une requête par
    colonne, pas dix mille allers-retours. Le chiffre est imprimé pour qu'il soit
    lisible dans le journal du banc plutôt que déduit d'un seuil."""
    import time

    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "ref", "fields": [
        {"key": "ref", "type": "text"}, {"key": "priorite", "type": "text"}]})

    # Semées directement : on mesure la CAPTURE, pas le chemin d'écriture.
    with _connect() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                [(ns_id, f"r{i}", f'{{"ref": "r{i}", "priorite": "{i % 7}"}}')
                 for i in range(10_000)])

    debut = time.monotonic()
    out = st.patch_schema(ns, fields=[{"key": "priorite", "origine": "system"}])
    duree = time.monotonic() - debut

    print(f"\n  capture de 10 000 lignes en {duree:.2f}s")
    assert out.get("origines_capturees") == 10_000, out
    assert duree < 20, (
        f"{duree:.1f}s pour 10 000 lignes : trop lent pour un geste qu'on doit oser "
        "faire — vérifier qu'on est bien en UNE requête par colonne")

    # Et le contenu est juste, pas seulement le compte : un échantillon ouvert.
    blob = _blob(ns_id, "r3")
    assert blob["priorite"] == {"valeur": "3", "origine": "3"}, blob


def test_une_capture_interrompue_ne_laisse_pas_la_moitie_des_lignes(live):
    """Deux colonnes gagnent le format ensemble ; la seconde échoue.

    ⚠️ Ce que cette épreuve prouve et ce qu'elle ne prouve pas. Elle exerce la VRAIE
    fonction de capture, avec un champ valide et un champ dont le nom casse la
    requête : si les deux écritures ne partageaient pas une transaction, la
    première resterait posée. Elle ne prouve PAS l'atomicité face à une panne du
    serveur en cours de commit — ça, aucun banc en processus ne le montre, et le
    prétendre serait pire que de le taire."""
    from oto_mcp import db

    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "ref", "fields": [{"key": "ref", "type": "text"},
                                                {"key": "a", "type": "text"}]})
    ligne = st.append_row(ns, {"ref": "r1", "a": "1"})

    # `a` est capturable ; le second champ fait échouer la boucle APRÈS lui, dans
    # la même transaction. Un nom simplement inconnu ne suffirait pas — sa requête
    # réussit en ne touchant aucune ligne.
    class _NomIllisible:
        def __str__(self):
            raise RuntimeError("nom de champ illisible")

    with pytest.raises(RuntimeError, match="illisible"):
        db.datastore_capturer_origine(ns_id, ["a", _NomIllisible()])

    blob = _blob(ns_id, ligne["_id"])
    assert blob["a"] == "1", (
        f"la première colonne est restée capturée malgré l'échec de la seconde : "
        f"les deux écritures ne partagent pas de transaction — {blob}")
