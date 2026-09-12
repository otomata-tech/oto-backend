"""Le LOT refuse la clé littérale pointée, comme ses trois voisins (#329 volet 2).

**Le cas qui l'impose, trouvé le 31/08/2026 sur un fichier de production.** Une fiche
porte `contact2_nom.comment` et `contact2_email.comment` comme **colonnes littérales de
premier niveau**. La base `contact2_nom` a été retirée le 29/08 ; elles, non — *une
couche imbriquée part avec sa colonne, une colonne littérale du même nom ne part pas.*

Elles se relisent alors comme des « couches orphelines » : un objet qui n'existe pas
dans le modèle. **Deux sessions ont cherché le geste pendant une demi-journée**, en
éprouvant d'abord l'écriture à `null`, puis le retrait de colonne — deux hypothèses
mesurées et réfutées, parce que le geste n'était sur aucun des chemins qu'on regardait.

⚠️ **Le refus existait depuis le 14/08 — sur trois chemins d'écriture, et pas sur le
quatrième.** `append_row`, `upsert_row` et la fusion le posaient ; `write_rows` non.
*La garde était sur les trois chemins où l'on écrit UNE ligne, et absente de celui où
l'on en écrit huit mille.* Une garde partiellement posée ne protège pas moins : elle
protège pendant qu'on écrit à côté d'elle, ce qui est pire, parce qu'on la croit tenue.

⚠️ **Amendé le 01/09/2026 (#684/#687).** Ce refus, posé sur TOUTE clé pointée, refusait
aussi ce que notre propre lecture PRODUIT : `flat_layers` sert `site_web` et
`site_web.comment` côte à côte, donc une fiche relue et réémise entière — le geste
dominant — se faisait refuser, et l'export CSV du tableau de bord n'était plus
réimportable. Le refus s'est donc **rétréci** : une adresse d'annotation valide est
d'abord RANGÉE (`ranger_les_couches`), et seul ce qui reste sans colonne réelle est
refusé. La sonde ci-dessous exige désormais LE COUPLE aux portes, pas le refus seul.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_lot_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + name
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


SCHEMA = {"key": "siren", "strict": True,
          "fields": [{"key": "siren", "type": "text"},
                     {"key": "contact2_nom", "type": "text"}]}


@pytest.fixture
def table(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-test", ns)
    st = make_store("sub-test")
    st.set_schema(ns, SCHEMA)
    return st, ns, ns_id


def _colonnes(ns_id: int) -> set:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s", (ns_id,)).fetchall()
    return {k for l in lignes for k in (l["data"] or {})}


def test_le_LOT_refuse_la_cle_pointee(table):
    """⚠️ LE témoin. Sans lui, l'import fabrique une colonne fantôme en silence.

    ⚠️ Depuis #684 le refus s'est RÉTRÉCI : `contact2_nom` étant déclarée au schéma,
    `contact2_nom.comment` est désormais une ADRESSE d'annotation valide, et elle est
    rangée. Ce qui reste refusé — et qui est le vrai défaut de production — c'est
    l'adresse dont la colonne n'existe nulle part : rien n'atteste `contact9_nom`, donc
    la clé ne désigne rien et fabriquerait la colonne littérale."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table

    with pytest.raises(RowValidationError) as e:
        st.write_rows(ns, [{"siren": "552032534",
                            "contact9_nom.comment": "site — probable"}])

    assert "n'est aucune colonne" in str(e.value)
    assert "contact9_nom.comment" not in _colonnes(ns_id), "rien n'est écrit"

    # Et le pendant : la colonne DÉCLARÉE, elle, se range — c'est l'aller-retour.
    st.write_rows(ns, [{"siren": "552032534", "contact2_nom.comment": "au registre"}])
    assert "contact2_nom.comment" not in _colonnes(ns_id), "rangée, pas littérale"


def test_les_QUATRE_chemins_repondent_pareil(table):
    """La règle est la cohérence, pas le cas : trois chemins qui refusent et un qui
    accepte, c'est une garde qu'on croit tenue."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    row = st.append_row(ns, {"siren": "1"})

    gestes = (
        ("append_row", lambda: st.append_row(ns, {"siren": "2", "a.comment": "x"})),
        ("update_row", lambda: st.update_row(ns, row["_id"], {"a.comment": "x"})),
        ("upsert_row", lambda: st.upsert_row(ns, row["_id"],
                                             {"siren": "1", "a.comment": "x"})),
        ("write_rows", lambda: st.write_rows(ns, [{"siren": "3", "a.comment": "x"}])),
    )
    for nom, geste in gestes:
        with pytest.raises(RowValidationError):
            geste()
    assert not any("." in c for c in _colonnes(ns_id))


def test_une_ecriture_EN_COUCHES_par_le_lot_passe_toujours(table):
    """Le témoin négatif : on ferme la forme littérale, pas la primitive de couches.
    Sans ce cas, « refuser les points » pourrait se durcir en « refuser les couches »
    et rendre le lot inutilisable là où il sert le plus."""
    st, ns, ns_id = table
    st.write_rows(ns, [{"siren": "552032534",
                        "contact2_nom": {"valeur": "Jo", "comment": "site"}}])
    cols = _colonnes(ns_id)
    assert "contact2_nom" in cols
    assert not any("." in c for c in cols), "la couche est imbriquée, pas pointée"


# ── La sonde : que le PROCHAIN chemin ne l'oublie pas non plus ────────────────

def test_tout_chemin_qui_ECRIT_en_base_refuse_les_cles_pointees():
    """⚠️ Le garde-fou mécanique, et c'est lui qui vaut le plus.

    Le trou n'était pas une erreur de raisonnement : c'était un appel manquant dans
    une méthode ajoutée après les trois autres. *Une garde appelée par ses appelants
    n'est pas une garde, c'est une convention* — et une convention se perd au
    quatrième appelant, puis au cinquième.

    On ne peut pas la centraliser dans `_check_row` : ce seam est **no-op sur un
    tableau souple** (ni `strict`, ni `required`, ni `lifecycle`), et la garde, elle,
    doit valoir partout — une colonne fantôme est aussi nuisible dans un tableau
    libre. Alors on sonde : toute méthode du store qui atteint une porte d'écriture
    doit, dans son propre corps, poser le refus.

    Si ce test rougit après l'ajout d'un chemin, ce n'est pas lui qu'il faut corriger.
    """
    import ast
    import inspect

    from oto_mcp.datastore import core, ecriture, ecriture_par_id, lots

    # `datastore_merge_row_locked` : depuis le 12/09/2026 le patch par `id` écrit par la
    # fusion sous verrou (`ecriture_par_id`), comme la fusion par clé — il n'a plus de
    # porte à lui (`datastore_update_row` est retiré).
    PORTES = {"datastore_insert_row", "datastore_upsert_row",
              "datastore_merge_row_locked"}
    # ⚠️ Les modules qui EXÉCUTENT, jamais la porte d'entrée. Depuis la coupe du
    # 07/09/2026 le store est un noyau qui COMPOSE des greffons : les chemins
    # d'écriture vivent dans `ecriture` et `lots`, et sonder `core` seul ne voyait
    # plus rien — c'est la garde `len(vues) >= 4` ci-dessous qui l'a dit.
    methodes = []
    for module in (core, ecriture, ecriture_par_id, lots):
        arbre = ast.parse(inspect.getsource(module))
        for classe in [n for n in ast.walk(arbre) if isinstance(n, ast.ClassDef)]:
            methodes += [n for n in classe.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    fautives, vues = [], []
    for methode in methodes:
        appels = {n.func.attr for n in ast.walk(methode)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        appels |= {n.func.id for n in ast.walk(methode)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if not appels & PORTES:
            continue
        vues.append(methode.name)
        # DEUX gestes, pas un : ranger l'adresse d'annotation (#684), PUIS refuser ce
        # qui reste. Une porte qui ne poserait que le refus rejetterait la lecture
        # réémise ; une porte qui ne poserait que le rangement laisserait passer la
        # colonne fantôme. Le trou de #685 était l'absence du second ; l'absence du
        # premier serait le trou symétrique, et il casse le geste dominant d'un agent.
        manquants = {"ranger_les_couches", "_refuse_dotted_names"} - appels
        if manquants:
            fautives.append(f"{methode.name} (manque {', '.join(sorted(manquants))})")

    # ⚠️ Le cliquet porte SA PROPRE garde. Sans elle, renommer une porte ou le seam
    # rendrait cette sonde inerte EN SILENCE — son seul mode de panne qu'elle ne peut
    # pas signaler toute seule.
    assert len(vues) >= 4, (
        f"la sonde ne voit plus que {vues} : les portes d'écriture ont été renommées "
        f"ou déplacées, et elle ne surveille plus rien. Corrige {sorted(PORTES)}.")
    assert not fautives, (
        f"{fautives} atteint une porte d'écriture sans le couple complet. C'est "
        "exactement le trou par lequel `write_rows` a fabriqué deux colonnes "
        "littérales sur un fichier de production le 27/08/2026.")
