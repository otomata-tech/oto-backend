"""La surface d'édition d'un nœud (oto#198), dérivée en UN seul point.

La fiche annonce OÙ un nœud s'écrit (`edit_surface`), la garde d'écriture refuse ce qui
ne s'écrit pas ici, et les deux lisent `node_keys.edit_surface_de`. Ce banc tient la
dérivation elle-même, sans base : chaque forme de stockage, les incohérences qui LÈVENT
au lieu de retomber sur `node`, la poignée que chaque surface exige, et le texte du refus
qui nomme la destination d'un guide. Le rejeu sur la route servie vit dans
`tests/api/test_node_page_native.py`.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from oto_mcp.capabilities import node_keys as K

_SURFACES = {"node", "doc", "project", "procedure", "datastore", "guide"}


# ── chaque forme de stockage a sa surface ─────────────────────────────────────

@pytest.mark.parametrize("props, surface", [
    ({}, "node"),
    (None, "node"),
    ({"legacy": "doc", "legacy_id": 12, "project_id": 7}, "doc"),
    # Une racine de projet n'a pas de `doc_id` : c'est exactement le cas où « doc_id
    # absent » aurait fait conclure à un nœud natif.
    ({"legacy": "prj", "legacy_id": 7, "pinned": True}, "project"),
    ({"legacy": "prc", "legacy_id": 3, "role": "procedure"}, "procedure"),
    ({"legacy": "tbl", "legacy_id": 5}, "datastore"),
    ({"legacy": "row", "legacy_ns": 5, "legacy_row": "r1"}, "datastore"),
    ({"delivery": "on-demand", "slug": "prospection"}, "guide"),
    ({"delivery": "init", "slug": "readme"}, "guide"),
])
def test_chaque_forme_de_stockage_a_sa_surface(props, surface):
    assert K.edit_surface_de(props) == surface


@pytest.mark.parametrize("valeur", ["init", "on-demand", "inattendue", None, ""])
def test_un_guide_se_reconnait_a_la_PRESENCE_de_delivery(valeur):
    """L'ancienne table `guides` n'avait aucune contrainte CHECK sur `delivery` : exiger
    une valeur connue ferait tomber des fiches sans rien protéger. La règle de stockage
    écrite est « un nœud natif ne porte JAMAIS `delivery` » — la présence suffit."""
    assert K.edit_surface_de({"delivery": valeur}) == "guide"


@pytest.mark.parametrize("props", [
    {"legacy": "xyz", "legacy_id": 1},
    {"legacy": ""},
    {"legacy": None},
    {"legacy": ["doc"]},
    {"legacy": "doc", "legacy_id": 1, "delivery": "init"},
], ids=["famille_inconnue", "legacy_vide", "legacy_null", "legacy_pas_un_texte",
        "legacy_et_delivery"])
def test_un_stockage_incoherent_LEVE_au_lieu_de_retomber_sur_node(props):
    """Un repli sur `node` enverrait le client écrire par une route qui refuse (409), ou
    pire, par une route qui accepte ce qui ne lui appartient pas."""
    with pytest.raises(K.NoeudIncoherent):
        K.edit_surface_de(props)


def test_l_enumere_est_ferme_et_chaque_valeur_a_sa_poignee():
    assert set(K.EditSurface.__args__) == _SURFACES
    assert set(K.POIGNEE_PAR_SURFACE) == _SURFACES
    assert set(K._SURFACE_PAR_FAMILLE.values()) <= _SURFACES


def test_les_familles_sont_EXACTEMENT_celles_que_posent_les_conversions():
    """Miroir comparé par test plutôt qu'importé : une famille ajoutée à la conversion
    sans surface ferait lever chaque fiche de cette famille — ce test le dit avant."""
    from oto_mcp.db import nodes as db_nodes
    poses = {db_nodes._FAMILY_PROJECT, db_nodes._FAMILY_DOC, db_nodes._FAMILY_GUIDE,
             db_nodes._FAMILY_TABLE, db_nodes._FAMILY_ROW}
    assert set(K._SURFACE_PAR_FAMILLE) == poses


# ── la poignée que chaque surface exige ───────────────────────────────────────

@pytest.mark.parametrize("surface, corps", [
    ("node", {"id": "nod_x"}),
    ("doc", {"doc_id": 12}),
    ("project", {"project_id": 7}),
    ("procedure", {"procedure": {"id": 3, "slug": "relance", "scope": "org"}}),
    ("datastore", {"datastore": "vivier"}),
    ("guide", {}),
])
def test_une_poignee_servie_passe(surface, corps):
    K.exiger_poignee(surface, corps)


@pytest.mark.parametrize("surface, corps", [
    ("node", {}),
    ("doc", {"doc_id": None}),
    ("project", {"project_id": None}),
    ("procedure", {"procedure": None}),
    ("datastore", {"datastore": None}),
    ("datastore", {"datastore": ""}),
], ids=["node", "doc", "project", "procedure", "datastore_null",
        "tableau_au_nom_vide"])
def test_une_poignee_absente_LEVE(surface, corps):
    """Le tableau au nom vide est POSSIBLE dans le schéma (`namespace` NOT NULL, pas non
    vide) et n'a pas été mesuré en production : il lève comme les autres, plutôt que de
    servir `datastore: null` sur un tableau."""
    with pytest.raises(K.NoeudIncoherent):
        K.exiger_poignee(surface, corps)


# ── le refus d'un guide nomme sa destination ──────────────────────────────────

def test_le_refus_d_un_guide_a_la_demande_est_ce_texte_la():
    message, details = K.refus_guide("user", "usr_moi",
                                     {"slug": "prospection", "delivery": "on-demand"})
    assert message == (
        "Ce nœud est un guide : il s'édite par `oto_guide` (op=write pour le corps, "
        "op=delete pour le retirer ; scope=user, slug=prospection), pas par "
        "`oto_node_edit`.")
    assert details == {"edit_surface": "guide", "scope": "user", "slug": "prospection",
                       "delivery": "on-demand"}


def test_le_readme_d_une_org_s_adresse_par_son_scope_et_son_org_sans_slug():
    message, details = K.refus_guide("org", "42", {"slug": "readme", "delivery": "init"})
    assert "delivery=init" in message and "owner_id=42" in message
    assert "sans slug" in message and "slug=readme" not in message
    assert details["owner_id"] == "42"


def test_le_readme_de_la_plateforme_s_adresse_par_son_slug():
    """À ce palier le slug désigne le BLOC (`guides._init_ref`) : l'omettre enverrait
    écrire le bloc par défaut."""
    message, details = K.refus_guide("platform", "platform",
                                     {"slug": "secret_sauce", "delivery": "init"})
    assert "slug=secret_sauce" in message and "sans slug" not in message
    assert "owner_id" not in details


# ── le module reste pur ───────────────────────────────────────────────────────

def test_node_keys_n_importe_ni_base_ni_registre():
    """Importé par la fiche, le rail ET la garde d'écriture : s'il déclarait une
    capacité ou ouvrait la base, il réordonnerait la table des routes ou lierait trois
    surfaces à un accès qu'aucune n'a demandé."""
    arbre = ast.parse(inspect.getsource(K))
    modules = {n.module or "" for n in ast.walk(arbre) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(arbre) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in modules if "db" in m or "registry" in m or "psycopg" in m}, modules
