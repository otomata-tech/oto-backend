"""Portée d'un jeton API (`token_scopes.py`) — deny-by-default, sans DB.

Logique pure : la table `_ALLOWED` est la seule porte. Ces tests figent les trois
propriétés qui font la valeur d'un jeton confié à un tiers — il ne sort pas de ses
tableaux, il ne dépasse pas sa permission, il n'atteint rien d'autre de la plateforme.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import token_scopes as ts

READ_ONLY = {"namespaces": {"204": "read"}}
WRITABLE = {"namespaces": {"204": "write"}}


# ── Critère 1 du brief : un jeton porté lit SON tableau, pas les voisins ──────

def test_scoped_token_reads_its_own_table():
    assert ts.authorize(READ_ONLY, "GET",
                        "/api/datastores/204/rows")
    assert ts.authorize(READ_ONLY, "GET",
                        "/api/datastores/204/rows/42")


def test_scoped_token_is_forbidden_on_sibling_table_of_same_org():
    """Le cœur de la demande : quatre tableaux dans l'org, le jeton n'en ouvre qu'un."""
    for path in ("/api/datastores/317/rows",
                 "/api/datastores/317/rows/1",
                 "/api/datastores/317/queue"):
        assert not ts.authorize(READ_ONLY, "GET", path), path


# ── Critère 2 : lecture seule ⇒ les écritures sont refusées ───────────────────

def test_read_only_token_cannot_write():
    ns = "/api/datastores/204"
    assert not ts.authorize(READ_ONLY, "PATCH", f"{ns}/rows/42")
    assert not ts.authorize(READ_ONLY, "POST", f"{ns}/rows")
    assert not ts.authorize(READ_ONLY, "DELETE", f"{ns}/rows/42")
    assert not ts.authorize(READ_ONLY, "PUT", f"{ns}/schema")


def test_write_token_writes_and_reads():
    ns = "/api/datastores/204"
    assert ts.authorize(WRITABLE, "PATCH", f"{ns}/rows/42")
    assert ts.authorize(WRITABLE, "POST", f"{ns}/rows")
    assert ts.authorize(WRITABLE, "GET", f"{ns}/rows")      # write ⊃ read


def test_write_token_writes_a_batch_in_its_table_only():
    """`POST …/rows/batch` (upsert par clé) est une écriture de lignes, pas de la
    gouvernance : ouvert au jeton en écriture, refusé en lecture et hors du tableau."""
    assert ts.authorize(WRITABLE, "POST", "/api/datastores/204/rows/batch")
    assert not ts.authorize(READ_ONLY, "POST", "/api/datastores/204/rows/batch")
    assert not ts.authorize(WRITABLE, "POST", "/api/datastores/317/rows/batch")
    assert ts.motif_du_refus(READ_ONLY, "POST", "/api/datastores/204/rows/batch")[0] != "geste"


# ── Le reste de la plateforme est hors de portée, par défaut ─────────────────

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/me"),
    ("GET", "/api/me/tokens"),
    ("POST", "/api/me/tokens"),
    ("GET", "/api/connectors"),
    ("POST", "/api/me/projects"),
    ("GET", "/api/me/instructions"),
    ("POST", "/api/datastores"),                       # créer un tableau
    ("DELETE", "/api/datastores/204"),
    ("PATCH", "/api/datastores/204"),   # renommer
    ("POST", "/api/datastores/204/share"),
])
def test_everything_else_is_denied(method, path):
    assert not ts.authorize(WRITABLE, method, path)


# ── File de travail : réserver est une écriture (signal #362) ────────────────

def test_write_token_can_claim():
    ns = "/api/datastores/204"
    assert ts.authorize(WRITABLE, "POST", f"{ns}/claim_next")
    assert ts.authorize(WRITABLE, "POST", f"{ns}/rows/42/claim")
    assert ts.authorize(WRITABLE, "POST", f"{ns}/rows/42/release")


def test_read_only_token_cannot_claim():
    """Lire la file ne donne pas le droit d'en retirer une ligne aux autres."""
    ns = "/api/datastores/204"
    assert ts.authorize(READ_ONLY, "GET", f"{ns}/queue")      # la file se lit…
    assert not ts.authorize(READ_ONLY, "POST", f"{ns}/claim_next")
    assert not ts.authorize(READ_ONLY, "POST", f"{ns}/rows/42/claim")
    assert not ts.authorize(READ_ONLY, "POST", f"{ns}/rows/42/release")


def test_claim_stays_within_the_scoped_table():
    for path in ("/api/datastores/317/claim_next",
                 "/api/datastores/317/rows/42/claim"):
        assert not ts.authorize(WRITABLE, "POST", path), path


def test_unknown_future_route_is_denied_by_default():
    """Une route ajoutée demain est refusée sans qu'on ait rien à y penser."""
    assert not ts.authorize(WRITABLE, "GET", "/api/quelque-chose-de-neuf")


def test_unscoped_token_is_unchanged():
    """`scopes` NULL = jeton historique : le gate ne s'applique pas."""
    assert ts.authorize(None, "GET", "/api/me")
    assert ts.authorize(None, "POST", "/api/me/projects")


def test_datastore_with_url_encoded_name():
    """Un NOM dans le chemin est jugé sur l'identifiant qu'il résout (oto#158) — que
    l'appelant fournit, décodé du chemin (`tableau_du_chemin`)."""
    scopes = {"namespaces": {"204": "read"}}
    assert ts.tableau_du_chemin("GET", "/api/datastores/mon%20tableau/rows") == "mon tableau"
    assert ts.authorize(scopes, "GET", "/api/datastores/mon%20tableau/rows",
                        id_du_tableau=204)


def test_un_nom_qui_ne_resout_rien_n_ouvre_rien():
    assert not ts.authorize(READ_ONLY, "GET", "/api/datastores/leads-accords-dormants/rows")
    assert not ts.authorize(READ_ONLY, "GET", "/api/datastores/autre/rows",
                            id_du_tableau=317)


def test_une_portee_par_nom_se_voit():
    """Une portée émise avant oto#158 nomme encore ses tableaux : elle n'ouvre plus
    rien, et `noms_de_tableau` les liste pour que le refus les nomme."""
    ancienne = {"namespaces": {"leads-accords-dormants": "read", "204": "write"}}
    assert ts.noms_de_tableau(ancienne) == ["leads-accords-dormants"]
    assert ts.noms_de_tableau(READ_ONLY) == []
    assert ts.noms_de_tableau(None) == []


def test_trailing_slash_does_not_bypass():
    assert ts.authorize(READ_ONLY, "GET",
                        "/api/datastores/204/rows/")
    assert not ts.authorize(READ_ONLY, "POST",
                            "/api/datastores/autre/rows/")


def test_path_traversal_in_datastore_is_not_a_match():
    """Le segment de datastore ne franchit pas le `/` — pas d'évasion par chemin."""
    assert not ts.authorize(
        READ_ONLY, "GET",
        "/api/datastores/204/rows/../../autre/rows")


# ── Validation du document de portée (saisie de l'émetteur) ───────────────────

def test_parse_none_is_unscoped():
    assert ts.parse(None) is None


def test_parse_normalizes_and_validates():
    assert ts.parse({"namespaces": {" t1 ": "read"}}) == {"namespaces": {"t1": "read"}}


@pytest.mark.parametrize("raw", [
    "read",                                   # pas un objet
    {"namespaces": {}},                       # portée vide = jeton inerte
    {"namespaces": {"t": "admin"}},           # permission inconnue
    {"namespaces": {"t": "read"}, "x": 1},    # clé de portée inconnue
    {"tables": {"t": "read"}},                # nom de clé faux
])
def test_parse_rejects_malformed(raw):
    with pytest.raises(ts.ScopeError):
        ts.parse(raw)


# ── Catalogue : filtré, et rabattu sur les droits du JETON ────────────────────

def test_filter_datastores_keeps_only_scoped_and_downgrades_rights():
    rows = [
        {"id": 204, "datastore": "leads-accords-dormants", "permission": "write",
         "can_write": True, "can_govern": True, "schema": {"fields": []}},
        {"id": 317, "datastore": "autre-tableau", "permission": "write", "can_write": True},
    ]
    ts.set_current(READ_ONLY)
    try:
        out = ts.filter_datastores(rows)
    finally:
        ts.set_current(None)
    assert [r["datastore"] for r in out] == ["leads-accords-dormants"]
    assert out[0]["permission"] == "read"
    assert out[0]["can_write"] is False
    assert out[0]["can_govern"] is False
    assert out[0]["schema"] == {"fields": []}      # le schéma reste (colonnes du front)


def test_filter_datastores_is_noop_without_scope():
    rows = [{"datastore": "a"}, {"datastore": "b"}]
    ts.set_current(None)
    assert ts.filter_datastores(rows) == rows


# ── Portée « projet » : brancher une intégration sur un projet, et lui seul ───

PROJECT_ONLY = {"projects": {"12": "read"}}
BOTH = {"namespaces": {"204": "read"}, "projects": {"12": "read"}}


def test_project_scoped_token_reads_its_project():
    assert ts.authorize(PROJECT_ONLY, "GET", "/api/me/projects/12")


def test_project_scoped_token_is_forbidden_on_another_project():
    """Le pendant du critère « pas les voisins » : un projet nommé, pas les autres."""
    assert not ts.authorize(PROJECT_ONLY, "GET", "/api/me/projects/13")


def test_project_scope_does_not_open_the_post_form():
    """`POST /api/me/projects` porte sa cible dans le CORPS : impossible à borner,
    donc jamais ouvert à un jeton porté — quelle que soit sa portée."""
    for scopes in (PROJECT_ONLY, BOTH):
        assert not ts.authorize(scopes, "POST", "/api/me/projects")


def test_project_scope_does_not_open_the_datastore():
    assert not ts.authorize(
        PROJECT_ONLY, "GET", "/api/datastores/204/rows")


def test_table_scope_does_not_open_the_project():
    assert not ts.authorize(READ_ONLY, "GET", "/api/me/projects/12")


def test_both_scopes_coexist():
    assert ts.authorize(BOTH, "GET", "/api/me/projects/12")
    assert ts.authorize(BOTH, "GET",
                        "/api/datastores/204/rows")


def test_project_scope_leaves_the_rest_of_the_platform_shut():
    for path in ("/api/me", "/api/me/tokens", "/api/me/projects/12/files",
                 "/api/me/projects/12/export", "/api/connectors"):
        assert not ts.authorize(PROJECT_ONLY, "GET", path), path


@pytest.mark.parametrize("raw", [
    {"projects": {"12": "write"}},            # aucune écriture de projet n'est ouverte
    {"projects": {"douze": "read"}},          # un projet se nomme par son id
    {"projects": {}},                         # portée vide = jeton inerte
])
def test_parse_rejects_malformed_project_scope(raw):
    with pytest.raises(ts.ScopeError):
        ts.parse(raw)


def test_parse_normalises_project_ids():
    """L'id peut arriver en nombre (JSON relu côté Python) : la clé reste une string."""
    assert ts.parse({"projects": {12: "read"}}) == {"projects": {"12": "read"}}


def test_projects_lists_the_scoped_ids():
    assert ts.projects(BOTH) == frozenset({"12"})
    assert ts.projects(READ_ONLY) == frozenset()
    assert ts.projects(None) == frozenset()
