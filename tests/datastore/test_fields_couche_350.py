"""`fields` ne doit plus accuser une adresse de couche valide (oto-backend#350).

Demander `fields=["effectif.origine"]` est une projection légitime — l'adresse d'une
annotation. Elle n'apparaît dans les clés PRÉSENTES que si la couche est renseignée sur
au moins une ligne de la page, et **jamais** dans les clés déclarées : le schéma déclare
des colonnes, pas leurs couches, qui sont natives et universelles.

⚠️ Conséquence sur une page où la couche est vide partout : l'avertissement disait
« colonne inconnue — vérifie l'orthographe » sur une adresse juste. L'appelant relit un
appel correct et conclut que l'annotation n'existe pas sur ce tableau. C'est la
troisième cause fausse désignée de la journée, après le refus de transition et celui de
la couche soulignée — **un message qui accuse à tort coûte plus qu'un silence.**

⚠️ Reconnaissance exacte, jamais rapprochement : la couche doit être l'un des trois noms
connus, et la colonne être présente ou déclarée. `effectif.bidule` reste inconnu,
`inconnue.origine` aussi.
"""
from __future__ import annotations

import pytest

from oto_mcp.tools.datastore import _adresse_de_couche_valide

PRESENT = {"effectif", "ref"}
DECLARE = {"effectif", "ref", "statut"}


@pytest.mark.parametrize("champ", [
    "effectif.origine",   # colonne présente sur la page
    "effectif.comment",
    "effectif.link",
    "statut.origine",     # colonne déclarée mais absente de la page
])
def test_une_adresse_de_couche_sur_colonne_connue_est_VALIDE(champ):
    assert _adresse_de_couche_valide(champ, PRESENT, DECLARE)


@pytest.mark.parametrize("champ", [
    "effectif.bidule",    # `bidule` n'est pas une couche connue
    "inconnue.origine",   # la colonne n'existe nulle part
    "effectif",           # pas une adresse de couche
    "origine",            # la couche seule
    ".origine",           # aucune colonne devant
    "effectif.",          # aucune couche derrière
])
def test_rien_d_AUTRE_n_est_reconnu(champ):
    """La garde ne doit pas devenir un silence général : ce qui n'est pas adressable
    continue d'être signalé, sinon on remplace une accusation fausse par un mutisme."""
    assert not _adresse_de_couche_valide(champ, PRESENT, DECLARE)


# ── le CHEMIN, pas la fonction — la leçon du jour appliquée à moi-même ───────

class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco(a[0]) if a and callable(a[0]) else deco


class _Store:
    """Une page où la couche `origine` n'est renseignée NULLE PART — le seul cas où
    l'avertissement se déclenchait à tort."""

    def get_schema(self, namespace):
        return {"fields": [{"key": "ref", "type": "text"},
                           {"key": "effectif", "type": "text"}]}

    def cursor_rows(self, namespace, **kw):
        return {"rows": [{"_id": "1", "ref": "r1", "effectif": "12"},
                         {"_id": "2", "ref": "r2", "effectif": "8"}],
                "next_cursor": None}

    def count_rows(self, *a, **k):
        return 2

    def _resolve(self, namespace):
        raise RuntimeError("le relevé des clés ne doit pas être atteint ici")


def _rows(monkeypatch, **kw):
    from oto_mcp import access
    from oto_mcp.tools import datastore as D

    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(D, "make_store", lambda sub: _Store())
    reg = _Reg()
    D.register(reg)
    return reg.tools["data_rows"](namespace="t", **kw)


def test_le_CHEMIN_ne_signale_plus_une_adresse_de_couche_valide(monkeypatch):
    """⚠️ Les bancs ci-dessus appellent la fonction : ils prouvent qu'elle reconnaît
    l'adresse, pas que l'appelant cesse d'être accusé. Celui-ci suit `data_rows`, le
    chemin où l'avertissement s'écrit."""
    out = _rows(monkeypatch, fields=["ref", "effectif.origine"])
    assert "warning" not in out, out.get("warning")


def test_le_CHEMIN_signale_toujours_une_VRAIE_colonne_inconnue(monkeypatch):
    """L'autre moitié : la garde ne doit pas être devenue un silence général."""
    out = _rows(monkeypatch, fields=["ref", "effectif.bidule"])
    assert "warning" in out and "effectif.bidule" in out["warning"]


def test_les_trois_couches_connues_et_elles_seules():
    """La liste vient du serveur, elle n'est pas recopiée ici : ajouter une couche un
    jour ne doit pas demander de retoucher ce banc, et en retirer une doit le faire
    tomber."""
    from oto_mcp.datastore import schema as dsv2

    for couche in dsv2.LAYER_KEYS:
        assert _adresse_de_couche_valide(f"effectif.{couche}", PRESENT, DECLARE)
    assert not _adresse_de_couche_valide("effectif.valeur", PRESENT, DECLARE), (
        "`valeur` n'est pas une couche adressable à plat : c'est la colonne elle-même")
