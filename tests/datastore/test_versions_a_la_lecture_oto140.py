"""`versions` — quelles versions d'une case la lecture rend (oto#140).

Une case existe en deux versions : `current`, ce qu'on a établi, et `origine`, ce que
la cliente a remis. Une écriture vise toujours la courante ; une lecture doit pouvoir
dire ce qu'elle veut recevoir.

Le défaut sert `current` seul (décision du 02/10/2026) ; l'origine se demande.

⚠️ Cette bascule n'est pas une économie de données : elle **supprime la surface d'un
incident**. Un écran avait comparé la valeur de départ à la valeur courante et
s'apprêtait à annoncer à une cliente « nous avons corrigé votre valeur » en lui
montrant du texte écrit par la plateforme. Ce genre d'accident demande que la donnée
soit là sans qu'on l'ait demandée.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore import versions as dsver
# `live` vient de `conftest.py` — pytest la découvre, rien à importer.

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



def _table():
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "raison_sociale", "type": "text"}]})
    st.append_row(ns, {"siren": "1", "raison_sociale":
                       {"valeur": "DUPONT", "comment": "fichier cliente 05/08"}},
                  donnees_d_origine=True)
    st.update_row(ns, st.list_rows(ns)[0]["_id"],
                  {"raison_sociale": {"valeur": "Dupont SAS", "comment": "INSEE"}})
    return st, ns


# ── le paramètre : ce qu'il admet et ce qu'il refuse ──────────────────────────

def test_le_defaut_sert_la_valeur_actuelle_SEULE():
    """Décision du 02/10/2026 : sans `versions`, `current` seul ; l'origine se demande."""
    assert dsver.check(None) == (dsver.CURRENT,)
    assert not dsver.sert_l_origine(dsver.check(None))


def test_l_ordre_demande_ne_change_PAS_ce_qui_est_declare():
    """Deux appelants qui demandent la même chose dans un ordre différent doivent lire
    la même déclaration — sinon `versions_servies` devient incomparable."""
    assert dsver.check(["origine", "current"]) == dsver.check(["current", "origine"])


def test_une_liste_VIDE_est_refusee_et_non_traitee_comme_un_defaut():
    """⚠️ `versions=[]` est un geste délibéré qui ne veut rien dire. Le lire comme un
    défaut servirait exactement l'INVERSE de ce qu'il demande, et sans un mot."""
    with pytest.raises(ValueError) as e:
        dsver.check([])
    assert "non-empty" in str(e.value)


def test_le_refus_NOMME_ce_qui_est_admis():
    """Un `invalid_input` nu oblige à deviner. Ici le refus rend les deux versions et
    ce que chacune veut dire — y compris pour qui essaierait l'ancien mot français."""
    with pytest.raises(ValueError) as e:
        dsver.check(["actuel"])
    msg = str(e.value)
    assert "actuel" in msg and "current" in msg and "origine" in msg


# ── ce que la lecture sert vraiment ───────────────────────────────────────────

def test_sans_origine_demandee_la_couche_DISPARAIT(live):
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.CURRENT,))[0]

    assert ligne["raison_sociale"] == "Dupont SAS"
    assert not [k for k in ligne if k.startswith("raison_sociale.origine")]


def test_avec_origine_demandee_la_couche_ET_ses_sous_champs_reviennent(live):
    """Le cas de l'écran d'écart : la valeur de départ ET sa provenance, pour dire
    « vous nous aviez donné X, source Y » — ce qu'on ne pouvait pas dire avant."""
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.CURRENT, dsver.ORIGINE))[0]

    assert ligne["raison_sociale"] == "Dupont SAS"
    assert ligne["raison_sociale.origine"] == "DUPONT"
    assert ligne["raison_sociale.origine.comment"] == "fichier cliente 05/08"


def test_le_NOM_NU_porte_toujours_la_version_COURANTE(live):
    """⚠️ L'axe qui compte le plus, et le piège qu'on refuse d'installer : faire porter
    deux sens à `champ` selon un paramètre serait exactement ce qu'on retire ailleurs
    du produit — un mot, deux choses. Même en ne demandant QUE l'origine, `champ` reste
    la valeur courante ; `versions` décide seulement de ce qui S'AJOUTE."""
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.ORIGINE,))[0]

    assert ligne["raison_sociale"] == "Dupont SAS", (
        "`champ` doit rester la version courante, quelle que soit la demande")
    assert ligne["raison_sociale.origine"] == "DUPONT"


def test_les_deux_versions_arrivent_dans_le_MEME_appel(live):
    """⚠️ Demandé par le consommateur avec un argument qui tranche : deux appels ne
    sont pas ATOMIQUES. Une écriture entre les deux ferait comparer l'avant d'un état
    à l'après d'un autre, et l'écran dirait « corrigé » sur une ligne que personne n'a
    touchée — la famille d'accident que ce contrat existe pour fermer."""
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.CURRENT, dsver.ORIGINE))[0]

    assert ligne["raison_sociale"] != ligne["raison_sociale.origine"], (
        "l'écart doit être lisible sur la MÊME ligne, sans jointure ni second appel")


# ── la forme imbriquée honore `versions` comme la forme plate (oto#273) ───────

def _contient_origine(v) -> bool:
    if isinstance(v, dict):
        return dsv2.ORIGIN_LAYER in v or any(_contient_origine(x) for x in v.values())
    if isinstance(v, list):
        return any(_contient_origine(x) for x in v)
    return False


def test_en_NESTED_sans_origine_demandee_la_couche_DISPARAIT(live):
    """⚠️ Le défaut d'oto#273 : en `layers=nested`, l'origine était servie quoi qu'on
    demande, sous une réponse qui déclarait `versions_servies: ["current"]` — la
    déclaration mentait exactement là où elle existe pour ne pas mentir."""
    st, ns = _table()
    page = st.cursor_rows(ns, layers="nested")
    ligne = page["rows"][0]

    assert page["versions_servies"] == ["current"]
    assert ligne["raison_sociale"] == {"valeur": "Dupont SAS", "comment": "INSEE"}
    assert not _contient_origine(ligne), ligne
    assert not _contient_origine(st.get_row(ns, ligne["_id"], layers="nested"))


def test_en_NESTED_l_origine_demandee_revient_dans_la_cellule(live):
    st, ns = _table()
    page = st.cursor_rows(ns, layers="nested", versions=(dsver.CURRENT, dsver.ORIGINE))
    ligne = page["rows"][0]

    assert page["versions_servies"] == ["current", "origine"]
    assert ligne["raison_sociale"]["valeur"] == "Dupont SAS"
    assert ligne["raison_sociale"]["origine"] == {
        "valeur": "DUPONT", "comment": "fichier cliente 05/08"}


def test_en_NESTED_une_case_qui_ne_portait_que_son_origine_redevient_nue(live):
    """Une cellule dont la seule couche est l'origine n'a plus de couche servie : elle
    revient comme une cellule sans couche, sa valeur nue — comme à plat."""
    from oto_mcp.datastore import layers as dsl
    cellule = {"valeur": "Dupont SAS", "origine": {"valeur": "DUPONT"}}
    assert dsl.nested_value(cellule, origine=False) == "Dupont SAS"
    assert dsl.nested_value(cellule, origine=True) == cellule
    liste = [{"email": {"valeur": "a@b.c", "origine": "x@b.c", "comment": "vu"}}]
    assert dsl.nested_value(liste, origine=False) == [
        {"email": {"valeur": "a@b.c", "comment": "vu"}}]


def test_le_GET_REST_nested_n_a_l_origine_que_demandee(live, monkeypatch):
    """Le chemin du tiroir de fiche du tableau de bord (`?empties=sentinel&layers=nested`) :
    il doit demander `versions=…origine` pour l'afficher, et la recevoir alors."""
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    ns, rid = _table_de("u-1")
    status, ligne = call("me.datastore.get_row",
                         path_params={"datastore": ns, "row_id": rid},
                         query=b"empties=sentinel&layers=nested")
    assert status == 200, ligne
    assert ligne["raison_sociale"] == "DUPONT"
    assert not _contient_origine(ligne), ligne
    status, ligne = call("me.datastore.get_row",
                         path_params={"datastore": ns, "row_id": rid},
                         query=b"empties=sentinel&layers=nested"
                               b"&versions=current&versions=origine")
    assert status == 200, ligne
    assert ligne["raison_sociale"] == {"valeur": "DUPONT", "origine": {"valeur": "DUPONT"}}


# ── ce que la réponse DÉCLARE ─────────────────────────────────────────────────

def test_la_reponse_declare_ce_qu_elle_sert(live):
    """Sans elle, « je ne l'ai pas demandée » et « elle n'existe pas sur cette case »
    se lisent pareil — et le lecteur réinventerait un marqueur, en pire, puisque cette
    fois il l'aurait deviné."""
    st, ns = _table()

    page = st.cursor_rows(ns, versions=(dsver.CURRENT,))
    assert page["versions_servies"] == ["current"]

    page = st.cursor_rows(ns, versions=(dsver.CURRENT, dsver.ORIGINE))
    assert page["versions_servies"] == ["current", "origine"]


def test_la_page_REST_la_declare_aussi(live):
    """Les deux faces lisent le même stockage : une déclaration servie d'un seul côté
    ferait diverger ce que chacune promet."""
    st, ns = _table()
    assert st.page_rows(ns, versions=(dsver.ORIGINE,))["versions_servies"] == ["origine"]


def test_le_defaut_se_lit_a_UN_seul_endroit():
    """Même promesse que `layers.DEFAUT`, et pour la même raison : un défaut recopié
    diverge le jour où on le change — et il divergerait sur la surface qu'un agent
    répète le plus."""
    import inspect

    from oto_mcp.tools import datastore as face_mcp
    src = inspect.getsource(face_mcp)
    assert "dsver.check(versions)" in src
    assert '"current", "origine"]' not in src.replace(
        '`versions=["current","origine"]`', "")


def test_les_DEUX_faces_portent_le_parametre():
    """⚠️ Une face branchée et l'autre non fait diverger ce que chacune promet — et
    c'est la face REST qui sert les écrans. Le champ vit dans `_forme.py`, le tiers
    neutre que `rows` et `claim` importent tous les deux : deux définitions
    divergeraient le jour où le défaut bascule."""
    import inspect

    from oto_mcp.capabilities.datastore import _forme, rows as face_rest
    from oto_mcp.tools import datastore as face_mcp

    assert "_VERSIONS" in inspect.getsource(_forme)
    assert "_versions(inp.versions)" in inspect.getsource(face_rest)
    assert "dsver.check(versions)" in inspect.getsource(face_mcp)


def test_le_refus_REST_NOMME_le_parametre():
    """Un `invalid_input` nu obligerait l'appelant à deviner lequel de ses paramètres
    est en cause. Le code d'erreur nomme celui-ci, comme `invalid_layers`."""
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities.datastore._forme import _versions

    with pytest.raises(AuthzDenied) as e:
        _versions(["actuel"])
    assert e.value.code == "invalid_versions"
    assert "current" in str(e.value.message if hasattr(e.value, "message") else e.value)


# ── l'écriture rend sa ligne COMME une lecture ────────────────────────────────

def _table_de(sub: str):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", sub, "t-" + uuid.uuid4().hex[:6]))
    st = make_store(sub)
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "raison_sociale", "type": "text"}]})
    st.append_row(ns, {"siren": "1", "raison_sociale": "DUPONT"}, donnees_d_origine=True)
    return ns, st.list_rows(ns)[0]["_id"]


def test_le_PATCH_REST_ne_rend_PAS_l_origine_par_defaut(live, monkeypatch):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    ns, rid = _table_de("u-1")
    status, ligne = call("me.datastore.update_row",
                         path_params={"datastore": ns, "row_id": rid},
                         body={"raison_sociale": "Dupont SAS"})
    assert status == 200, ligne
    assert ligne["raison_sociale"] == "Dupont SAS"
    assert not [k for k in ligne if k.startswith("raison_sociale.origine")]
    assert ligne["versions_servies"] == ["current"]


def test_le_PATCH_REST_rend_l_origine_quand_on_la_demande(live, monkeypatch):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    ns, rid = _table_de("u-1")
    status, ligne = call("me.datastore.update_row",
                         path_params={"datastore": ns, "row_id": rid},
                         body={"raison_sociale": "Dupont SAS"},
                         query=b"versions=current&versions=origine")
    assert status == 200, ligne
    assert ligne["raison_sociale"] == "Dupont SAS"
    assert ligne["raison_sociale.origine"] == "DUPONT"
    assert ligne["versions_servies"] == ["current", "origine"]
    # la forme virgule vaut la forme répétée (#367)
    status, ligne = call("me.datastore.update_row",
                         path_params={"datastore": ns, "row_id": rid},
                         body={"raison_sociale": "Dupont SA"},
                         query=b"versions=current,origine&layers=nested")
    assert status == 200, ligne
    assert ligne["raison_sociale"]["origine"] == {"valeur": "DUPONT"}


def test_l_ajout_REST_se_lit_comme_une_lecture(live, monkeypatch):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    ns, _ = _table_de("u-1")
    status, ligne = call("me.datastore.append_row", path_params={"datastore": ns},
                         body={"siren": "1", "raison_sociale": "Dupont SAS"},
                         query=b"versions=current,origine&upsert=true&key=siren")
    assert status in (200, 201), ligne
    assert ligne["raison_sociale.origine"] == "DUPONT"
    assert ligne["versions_servies"] == ["current", "origine"]
    status, ligne = call("me.datastore.append_row", path_params={"datastore": ns},
                         body={"siren": "2", "raison_sociale": "Autre"})
    assert status in (200, 201), ligne
    assert ligne["versions_servies"] == ["current"]


def test_un_refus_de_forme_REST_n_ecrit_RIEN(live, monkeypatch):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    ns, rid = _table_de("u-1")
    status, corps = call("me.datastore.update_row",
                         path_params={"datastore": ns, "row_id": rid},
                         body={"raison_sociale": "Jamais"}, query=b"versions=actuel")
    assert (status, corps["error"]) == (400, "invalid_versions")
    from oto_mcp.datastore.core import make_store
    assert make_store("u-1").get_row(ns, rid)["raison_sociale"] == "DUPONT"


def test_l_ecriture_du_store_se_lit_comme_une_lecture(live):
    ns, rid = _table_de("sub-test")
    from oto_mcp.datastore.core import make_store
    st = make_store("sub-test")
    ligne = st.update_row(ns, rid, {"raison_sociale": "Dupont SAS"})
    assert "raison_sociale.origine" not in ligne
    ligne = st.update_row(ns, rid, {"raison_sociale": "Dupont SA"},
                          versions=(dsver.CURRENT, dsver.ORIGINE))
    assert ligne["raison_sociale.origine"] == "DUPONT"


# ── à plat, l'origine d'un sous-champ de LISTE se demande aussi (oto#273) ─────
#
# ⚠️ La fuite que la forme imbriquée avait déjà fermée (a3b32bd2) restait ouverte à
# plat : `_row_to_dict` retirait `champ.origine` au premier niveau, mais les items
# d'une colonne-liste passaient par `served_value` → `_served_item` → `flat_layers`,
# qui les servait sans regarder `versions`. `contacts[0]["email.origine"]` sortait
# donc sous une réponse qui déclarait `versions_servies: ["current"]`.

_LISTE = {
    "siren": "1",
    "contacts": [
        {"nom": "Dupont",
         "email": {"valeur": "d@x.fr", "comment": "hunter",
                   "origine": {"valeur": "dupont@x.fr", "comment": "fichier 05/08"}},
         # Un cran plus bas : une liste DANS un item, qui porte elle aussi une origine.
         "postes": [{"titre": {"valeur": "DRH", "origine": "RH"}}]},
        {"nom": {"valeur": "Martin", "origine": "MARTIN"}},
    ],
}


def _origines_a_plat(v) -> list:
    """Toute clé servie à plat qui porte la couche `origine`, à toute profondeur."""
    if isinstance(v, dict):
        return ([k for k in v if isinstance(k, str)
                 and f".{dsv2.ORIGIN_LAYER}" in k]
                + [x for val in v.values() for x in _origines_a_plat(val)])
    if isinstance(v, list):
        return [x for item in v for x in _origines_a_plat(item)]
    return []


def _lu_a_plat(versions: tuple = dsver.DEFAUT) -> dict:
    from oto_mcp.datastore.core import DatastorePg
    return DatastorePg._row_to_dict(
        {"row_id": "r1", "created_at": "t", "updated_at": "t", "data": _LISTE},
        versions=versions)


def test_a_PLAT_l_origine_d_un_item_de_liste_ne_sort_pas_au_defaut():
    """⚠️ Le défaut : `l[0]["e.origine"]` servi sans qu'on l'ait demandé."""
    ligne = _lu_a_plat()
    assert not _origines_a_plat(ligne), _origines_a_plat(ligne)
    contact = ligne["contacts"][0]
    # Le reste de l'item ne bouge pas : la valeur au nom nu, `comment` à côté.
    assert contact["email"] == "d@x.fr" and contact["email.comment"] == "hunter"
    assert contact["postes"] == [{"titre": "DRH"}]
    # Une case qui ne portait que son origine redevient nue — comme au premier niveau.
    assert ligne["contacts"][1] == {"nom": "Martin"}


def test_a_PLAT_l_origine_d_un_item_demandee_revient_a_toute_profondeur():
    contact = _lu_a_plat((dsver.CURRENT, dsver.ORIGINE))["contacts"][0]
    assert contact["email.origine"] == "dupont@x.fr"
    assert contact["email.origine.comment"] == "fichier 05/08"
    assert contact["postes"][0]["titre.origine"] == "RH"


def _table_liste(sub: str) -> tuple:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", sub, "t-" + uuid.uuid4().hex[:6]))
    st = make_store(sub)
    # Les colonnes déclarées : une colonne non déclarée est refusée (oto#124).
    st.set_schema(ns, {"fields": [{"key": "siren", "type": "text"},
                                  {"key": "contacts", "type": "list",
                                   "of": {"type": "object"}}]})
    row = st.append_row(ns, dict(_LISTE), origine_override=True)
    return ns, row["_id"]


def test_a_PLAT_tous_les_chemins_du_store_taisent_l_origine_d_un_item(live):
    """Le correctif vit au point unique qui aplatit (`flat_layers`) : chaque lecture
    du store, et la ligne que rend une écriture, le prennent de là."""
    ns, rid = _table_liste("sub-test")
    st = _store()
    assert not _origines_a_plat(st.list_rows(ns))
    page = st.cursor_rows(ns)
    assert page["versions_servies"] == ["current"] and not _origines_a_plat(page["rows"])
    assert not _origines_a_plat(st.page_rows(ns)["rows"])
    assert not _origines_a_plat(st.get_row(ns, rid))
    assert not _origines_a_plat(st.update_row(ns, rid, {"siren": "1"}))
    avec = st.get_row(ns, rid, versions=(dsver.CURRENT, dsver.ORIGINE))
    assert avec["contacts"][0]["email.origine"] == "dupont@x.fr"


def test_a_PLAT_le_REST_tait_l_origine_d_un_item_sauf_demandee(live, monkeypatch):
    from _datastore_rest import call, stub_authz
    stub_authz(monkeypatch)
    ns, rid = _table_liste("u-1")
    status, ligne = call("me.datastore.get_row",
                         path_params={"datastore": ns, "row_id": rid})
    assert status == 200, ligne
    assert not _origines_a_plat(ligne), ligne
    status, ligne = call("me.datastore.get_row",
                         path_params={"datastore": ns, "row_id": rid},
                         query=b"versions=current,origine")
    assert status == 200, ligne
    assert ligne["contacts"][0]["postes"][0]["titre.origine"] == "RH"
    status, ligne = call("me.datastore.update_row",
                         path_params={"datastore": ns, "row_id": rid},
                         body={"siren": "1"})
    assert status == 200, ligne
    assert not _origines_a_plat(ligne), ligne
