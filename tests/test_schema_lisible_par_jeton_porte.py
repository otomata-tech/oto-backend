"""Le schéma d'un tableau se LIT avec un jeton porté — et le refus ne ment plus.

⚠️ Mesuré en production sur un jeton neuf portant trois tableaux en écriture : la
route qui sert le schéma répondait « portée interdite », et le détail du refus
**listait ce tableau parmi ceux que le jeton ouvre**. Deux défauts en un.

Le premier est un oubli, et l'asymétrie le prouve : `PUT /schema` était ouvert,
`GET` non. On pouvait poser un schéma sans pouvoir le consulter — personne ne
décide ça, écrire est plus fort que lire.

Ce que ça coûtait : la doctrine servie aux agents leur dit de LIRE le schéma
avant d'écrire — c'est ce qui fait qu'une longueur maximale est un contrat et pas
une consigne. Un agent porté qui obéissait se prenait un refus sur le geste exact
qu'on lui demandait.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import token_scopes as ts

_SCHEMA = "/api/datastore/namespaces/t/schema"


# ── le schéma, dans les deux sens ────────────────────────────────────────────

def test_une_portee_en_LECTURE_lit_le_schema():
    assert ts.authorize(ts.parse({"namespaces": {"t": "read"}}), "GET", _SCHEMA) is True


def test_une_portee_en_LECTURE_ne_l_ecrit_pas():
    p = ts.parse({"namespaces": {"t": "read"}})
    assert ts.authorize(p, "PUT", _SCHEMA) is False
    assert ts.authorize(p, "PATCH", _SCHEMA) is False


def test_une_portee_en_ECRITURE_fait_les_deux():
    p = ts.parse({"namespaces": {"t": "write"}})
    assert all(ts.authorize(p, m, _SCHEMA) for m in ("GET", "PUT", "PATCH"))


def test_le_schema_d_un_AUTRE_tableau_reste_ferme():
    p = ts.parse({"namespaces": {"t": "write"}})
    assert ts.authorize(p, "GET", "/api/datastore/namespaces/autre/schema") is False


def test_la_gouvernance_reste_hors_de_portee():
    """L'ajout ne devait pas déborder : supprimer, renommer et partager un
    tableau restent fermés à tout jeton porté."""
    p = ts.parse({"namespaces": {"t": "write"}})
    for m, r in [("DELETE", "/api/datastore/namespaces/t"),
                 ("PATCH", "/api/datastore/namespaces/t"),
                 ("POST", "/api/datastore/namespaces/t/share"),
                 ("DELETE", "/api/datastore/namespaces/t/share")]:
        assert ts.authorize(p, m, r) is False, f"{m} {r} s'est ouvert par ricochet"


# ── le refus cesse de se contredire ──────────────────────────────────────────

def test_le_refus_distingue_un_GESTE_ferme_d_une_RESSOURCE_hors_portee():
    """Refuser un tableau EN LE NOMMANT comme autorisé fait conclure au lecteur
    que son jeton est cassé. C'est le pire des deux états."""
    p = ts.parse({"namespaces": {"t": "read"}})
    assert ts.motif_du_refus(p, "PUT", _SCHEMA) == ("ressource", "t")
    assert ts.motif_du_refus(p, "DELETE", "/api/datastore/namespaces/t") == ("geste", "")
    assert ts.motif_du_refus(p, "GET", "/api/me") == ("geste", "")


def test_le_motif_nomme_la_ressource_telle_qu_ELLE_est_adressee():
    """Le nom vient du CHEMIN, décodé — un tableau à espace ou accent doit se
    retrouver dans le message, pas sa forme encodée."""
    p = ts.parse({"namespaces": {"mon tableau": "read"}})
    cause, quoi = ts.motif_du_refus(p, "PUT", "/api/datastore/namespaces/mon%20tableau/schema")
    assert (cause, quoi) == ("ressource", "mon tableau")
