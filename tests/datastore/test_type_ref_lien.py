"""Le type `ref` : une cellule de lien porte l'`_id` d'une ligne du tableau `target`.

Le cas qui l'a fait naître : une colonne de lien n'était qu'un `text`, et une écriture qui
y posait le NOM de la ligne cible (« Acme ») passait sans un mot. Ce banc tient les trois
moitiés : la déclaration (`ref` exige `target`, `target` ne vit que sur un `ref`), la
pose (la cible est un tableau que l'auteur lit), et l'écriture (un nom est refusé avec
l'endroit où lire l'`_id`, un `_id` passe — en colonne comme en liste).
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import liens
from oto_mcp.datastore import schema as S
from oto_mcp.datastore import schema_keys as K
from oto_mcp.datastore.outils import _new_id
from oto_mcp.datastore.phrases_de_refus import _forme_attendue, gabarit

_ID = "01a17e59-1234-7abc-8def-0123456789ab"


def _contacts(cible=42):
    return {"fields": [{"key": "nom", "type": "text"},
                       {"key": "societe", "type": "ref", "target": cible},
                       {"key": "liees", "type": "list",
                        "of": {"type": "ref", "target": cible}}]}


# ── la forme d'un `_id` et d'une cible ────────────────────────────────────────

def test_un_id_neuf_a_la_forme_d_un_id():
    assert liens.est_un_id_de_ligne(_new_id())
    assert liens.est_un_id_de_ligne(_ID)


@pytest.mark.parametrize("valeur", [
    "Acme Robotics", "acme.example", "42", 42, "", None, {"valeur": _ID},
    "01a17e59-1234-7abc-8def-0123456789", _ID + " (voir fiche)",
    # Entouré d'espaces, il serait stocké tel quel et la puce ne trouverait rien.
    " " + _ID, _ID + "\n", _ID.upper(),
])
def test_ce_qui_n_est_pas_un_id_n_en_est_pas_un(valeur):
    assert not liens.est_un_id_de_ligne(valeur)


@pytest.mark.parametrize("target,attendu", [
    (42, 42), ("42", 42), (" 7 ", 7), (0, None), (-3, None), (True, None),
    ("acme", None), (None, None), (4.2, None),
    # « ² » passe `str.isdigit` et casse `int` ; au-delà d'un BIGSERIAL, la lecture de
    # la cible lèverait une erreur de base au lieu d'un refus.
    ("²", None), (2 ** 63, None), (str(2 ** 64), None), (2 ** 63 - 1, 2 ** 63 - 1),
])
def test_une_cible_est_un_numero_de_tableau(target, attendu):
    assert liens.cible_valide(target) == attendu


def test_les_cibles_se_lisent_a_toute_profondeur():
    schema = {"fields": [
        {"key": "a", "type": "ref", "target": 1},
        {"key": "b", "type": "list", "of": {"type": "ref", "target": "2"}},
        {"key": "c", "type": "object", "fields": [
            {"key": "d", "type": "ref", "target": 3}]},
        {"key": "e", "type": "list", "of": {"type": "object", "fields": [
            {"key": "f", "type": "ref", "target": 4}]}},
        {"key": "g", "type": "text"}]}
    assert liens.cibles(schema["fields"]) == {1, 2, 3, 4}


# ── la déclaration ────────────────────────────────────────────────────────────

def test_ref_et_sa_cible_se_declarent():
    assert S.validate_schema_def(_contacts()) == []
    assert "ref" in next(c.quoi for c in K.CLES if c.nom == "type")
    assert "target" in K.ADMISES["champ"] and "target" in K.ADMISES["element"]
    # La lecture compacte la garde : c'est une contrainte, lue par le validateur.
    assert "target" in K.CONTRAINTES["champ"]


@pytest.mark.parametrize("champ,morceau", [
    ({"key": "c", "type": "ref"}, "type=ref exige target"),
    ({"key": "c", "type": "ref", "target": "acme"}, "target doit être le NUMÉRO"),
    ({"key": "c", "type": "ref", "target": 0}, "target doit être le NUMÉRO"),
    ({"key": "c", "type": "text", "target": 42}, "target ne se lit que sur"),
    ({"key": "c", "type": "list", "of": {"type": "ref"}}, "c.of: type=ref exige target"),
    ({"key": "c", "type": "list", "of": {"type": "text", "target": 4}},
     "target ne se lit que sur"),
])
def test_une_cible_absente_mal_formee_ou_deplacee_est_refusee(champ, morceau):
    erreurs = S.validate_schema_def({"fields": [champ]})
    assert any(morceau in e for e in erreurs), erreurs


# ── un schéma DÉJÀ stocké : ce qu'on ne touche pas ne se rejuge pas ─────────────

@pytest.mark.parametrize("stocke", [
    {"key": "c", "type": "text", "target": 42},       # `target` d'avant `ref`, tolérée
    {"key": "c", "type": "ref"},                       # un lien sans cible
    {"key": "c", "type": "ref", "target": "acme"},     # une cible par son nom
    {"key": "c", "type": "list", "of": {"type": "text", "target": 4}},
])
def test_une_declaration_stockee_inchangee_ne_gele_pas_le_patch_d_une_autre(stocke):
    """Même parti que les clés inconnues : patcher `autre` repasse le schéma fusionné,
    et `c` — que ce geste ne touche pas — ne doit pas le faire refuser."""
    ancien = {"fields": [stocke]}
    patche = {"fields": [stocke, {"key": "autre", "type": "text"}]}
    assert not any("target" in e for e in S.validate_schema_def(patche, ancien)), \
        S.validate_schema_def(patche, ancien)


def test_une_declaration_stockee_qu_on_MODIFIE_se_juge():
    ancien = {"fields": [{"key": "c", "type": "text", "target": 42}]}
    erreurs = S.validate_schema_def(
        {"fields": [{"key": "c", "type": "text", "target": 43}]}, ancien)
    assert any("target ne se lit que sur" in e for e in erreurs), erreurs
    erreurs = S.validate_schema_def(
        {"fields": [{"key": "c", "type": "ref", "target": 42},
                    {"key": "d", "type": "ref"}]}, ancien)
    assert any("fields.d: type=ref exige target" in e for e in erreurs), erreurs


def test_une_cible_absente_dans_un_sous_champ_est_refusee():
    erreurs = S.validate_schema_def({"fields": [{"key": "o", "type": "object", "fields": [
        {"key": "d", "type": "ref"}]}]})
    assert any("fields.o.fields.d: type=ref exige target" in e for e in erreurs), erreurs


# ── l'écriture ────────────────────────────────────────────────────────────────

def test_un_id_passe_en_colonne_comme_en_liste():
    assert S.validate_row(_contacts(), {"nom": "x", "societe": _ID, "liees": [_ID]},
                          written={"societe", "liees"}) == []


def test_un_nom_est_refuse_avec_l_endroit_ou_lire_l_id():
    erreurs = S.validate_row(_contacts(), {"nom": "x", "societe": "Acme Robotics"},
                             written={"societe"})
    arme = [e for e in erreurs if "`societe` is declared `ref`" in e]
    assert arme, erreurs
    assert "data_rows(datastore=42" in arme[0]
    assert "@empty" in arme[0]                  # « cherché, rien » a son mot (oto#140)


def test_un_nom_dans_une_liste_de_liens_est_refuse_a_son_rang():
    erreurs = S.validate_row(_contacts(), {"nom": "x", "liees": [_ID, "Globex"]},
                             written={"liees"})
    assert any(e.startswith("liees[1]:") and "data_rows(datastore=42" in e
               for e in erreurs), erreurs
    assert not any(e.startswith("liees[0]") for e in erreurs)


def test_un_lien_deja_faux_en_base_ne_gele_pas_une_autre_colonne():
    """Même parti que tout type armé : on juge ce que le geste ÉCRIT."""
    gelees: list = []
    erreurs = S.validate_row(_contacts(), {"nom": "y", "societe": "Acme"},
                             written={"nom"}, gelees=gelees)
    assert erreurs == []
    assert any(g["champ"] == "societe" for g in gelees)


def test_le_refus_dit_la_forme_et_le_gabarit_nomme_la_cible():
    champ = _contacts()["fields"][1]
    assert "table 42" in _forme_attendue(champ)
    assert gabarit(champ) == "<row _id of table 42>"


# ── bout en bout : la pose vérifie la cible, l'écriture le lien ─────────────────

SUB = "sub-ref-lien"


def _table(nom: str, schema=None):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = f"{nom}-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    if schema is not None:
        make_store(SUB).set_schema(str(ns_id), schema)
    return str(ns_id), ns_id


def test_pose_et_ecriture_sur_une_vraie_base(live):
    from oto_mcp.datastore.core import make_store
    from oto_mcp.datastore.errors import SchemaDefinitionError

    store = make_store(SUB)
    cible_ns, cible_id = _table("societes", {"fields": [{"key": "nom", "type": "text"}]})
    ligne = store.append_row(cible_ns, {"nom": "Acme"})
    contacts_ns, _ = _table("contacts")

    # Une cible que personne ne lit sous ce numéro : refusée, sans dire si elle existe.
    with pytest.raises(SchemaDefinitionError) as e:
        store.set_schema(contacts_ns, _contacts(cible=999_999_999))
    assert "target 999999999" in str(e.value)

    store.set_schema(contacts_ns, _contacts(cible=cible_id))
    servi = store.get_schema(contacts_ns)
    assert servi["fields"][1] == {"key": "societe", "type": "ref", "target": cible_id}
    assert servi["fields"][2]["of"] == {"type": "ref", "target": cible_id}

    store.append_row(contacts_ns, {"nom": "a", "societe": ligne["_id"],
                              "liees": [ligne["_id"]]})
    with pytest.raises(Exception) as e:
        store.append_row(contacts_ns, {"nom": "b", "societe": "Acme"})
    assert f"data_rows(datastore={cible_id}" in str(e.value)

    # Une cible déjà posée, inchangée, ne se revérifie pas : patcher une autre colonne
    # passe même si on ne relit pas la cible.
    store.patch_schema(contacts_ns, fields=[{"key": "note", "type": "text"}])


def test_une_cible_qui_existe_chez_un_autre_a_la_meme_phrase(live):
    """« n'existe pas » et « ne t'est pas visible » ont la même phrase (#193)."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from oto_mcp.datastore.errors import SchemaDefinitionError

    autre_id = db.create_datastore("user", "sub-ref-lien-autre",
                                   "secret-" + uuid.uuid4().hex[:6])
    contacts_ns, _ = _table("contacts")
    phrases = []
    for cible in (autre_id, 999_999_999):
        with pytest.raises(SchemaDefinitionError) as e:
            make_store(SUB).set_schema(contacts_ns, _contacts(cible=cible))
        phrases.append(str(e.value).replace(str(cible), "<n>"))
    assert phrases[0] == phrases[1]


def test_un_tableau_NOMME_par_les_chiffres_de_la_cible_ne_la_rend_pas_lisible(live):
    """La cible est un NUMÉRO : un tableau à moi qui porte ces chiffres pour nom n'ouvre
    pas le tableau d'un autre."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from oto_mcp.datastore.errors import SchemaDefinitionError

    autre_id = db.create_datastore("user", "sub-ref-lien-autre",
                                   "secret-" + uuid.uuid4().hex[:6])
    leurre = db.create_datastore("user", SUB, str(autre_id))
    contacts_ns, _ = _table("contacts")
    try:
        with pytest.raises(SchemaDefinitionError):
            make_store(SUB).set_schema(contacts_ns, _contacts(cible=autre_id))
    finally:
        db.delete_datastore_by_id(leurre)


def test_lire_la_cible_ne_change_pas_le_tableau_de_l_appel(live):
    """La remise (`dernier_tableau`) et le journal nomment le tableau dont on pose le
    schéma, jamais la cible que la pose a regardée."""
    from oto_mcp.datastore.core import make_store

    cible_ns, cible_id = _table("societes", {"fields": [{"key": "nom", "type": "text"}]})
    contacts_ns, contacts_id = _table("contacts")
    store = make_store(SUB)
    store.set_schema(contacts_ns, _contacts(cible=cible_id))
    assert store.dernier_tableau["ns_id"] == contacts_id
