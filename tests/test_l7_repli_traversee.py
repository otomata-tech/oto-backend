"""Sous le nouveau régime, la traversée retrouve son repli.

Le chemin historique (`walk_cascade`) est un **générateur** : un compte nommé absent
au palier membre passait la main au palier org — « l'org a eu, le membre non », contrat
écrit dans son code. La chaîne l'a remplacé par un `return` au premier palier qui
DÉTIENT une clé, pendant que la lecture, elle, résout au **fetch**. Or les deux ne
répondent pas la même chose : la désignation se fait sur la PRÉSENCE (`has_credential`,
n'importe quel compte), la lecture sur le compte NOMMÉ. Un miss devenait donc un refus
sec au lieu d'un repli (#673) — bloquant pour la pose du drapeau.

Le remède n'est pas d'ajouter une forme, c'est de **rendre à la traversée celle qu'elle
avait perdue**. Deux propriétés en découlent, et ces tests les tiennent séparément :

- la LECTURE parcourt jusqu'au premier palier qui répond (le repli) ;
- la DÉSIGNATION, elle, ne bouge pas — le relevé de fenêtre compare des désignations,
  et lui donner une liste ferait bouger ce qu'il mesure pendant qu'on corrige la lecture.
"""
from __future__ import annotations

import inspect

import pytest

from oto_mcp.access import chain_resolution


class _Sonde:
    """Une sonde de lecture qui ne répond qu'au palier nommé — le cas du compte
    nommé présent à l'org et absent au membre."""

    def __init__(self, repond: str):
        self.repond = repond
        self.vus: list[str] = []

    def _rep(self, mode: str, charge):
        self.vus.append(mode)
        return (charge, "") if mode == self.repond else None

    def member(self, sub, org, provider):
        return self._rep("user", "clé-membre")

    def group(self, gid, provider):
        return self._rep("group", "clé-équipe")

    def org(self, oid, provider):
        return self._rep("org", "clé-org")

    def tenant(self, slug, provider):
        return self._rep("tenant", "clé-tenant")

    def platform(self, sub, provider, org):
        self.vus.append("platform")
        return {"label": "plateforme"} if self.repond == "platform" else None


def _paliers(*modes):
    """Des paliers atteignables, dans l'ordre — ce que la chaîne cède."""
    for m in modes:
        yield chain_resolution.ChainPick(
            m, {"user": "member", "group": "group", "org": "org",
                "tenant": "tenant", "platform": "platform"}[m],
            "7" if m in ("group", "org") else "x")


# --- le repli, la propriété que ce lot rend -----------------------------------

def test_un_miss_au_premier_palier_passe_la_main_au_suivant():
    """LE défaut de #673 : le membre DÉTIENT une clé (donc il est désigné) mais le
    compte nommé n'y est pas (donc le fetch rate). Avant, refus sec."""
    sonde = _Sonde(repond="org")
    rung = chain_resolution.rung_for_picks(
        _paliers("user", "org"), sonde, "u-1", "serper", 7)
    assert rung is not None and rung.mode == "org", "le repli n'a pas eu lieu"
    assert sonde.vus == ["user", "org"], "les paliers doivent être essayés dans l'ordre"


def test_le_premier_palier_qui_repond_gagne_et_arrete_le_parcours():
    """Le coût nominal ne change pas : on ne sonde pas ce qui suit."""
    sonde = _Sonde(repond="user")
    rung = chain_resolution.rung_for_picks(
        _paliers("user", "group", "org"), sonde, "u-1", "serper", 7)
    assert rung.mode == "user"
    assert sonde.vus == ["user"], "un palier suivant a été sondé pour rien"


def test_aucun_palier_ne_repond_rend_None():
    """Ne rien trouver reste « rien » — le repli ne fabrique pas un accès."""
    sonde = _Sonde(repond="jamais")
    assert chain_resolution.rung_for_picks(
        _paliers("user", "group", "org"), sonde, "u-1", "serper", 7) is None
    assert sonde.vus == ["user", "group", "org"]


def test_sans_palier_atteignable_on_ne_sonde_rien():
    sonde = _Sonde(repond="user")
    assert chain_resolution.rung_for_picks(iter([]), sonde, "u-1", "serper", 7) is None
    assert sonde.vus == []


# --- la forme : un générateur, pas une liste ----------------------------------

def test_la_traversee_est_un_GENERATEUR():
    """⚠️ Pas un détail de style : une liste sonderait TOUS les paliers à chaque appel
    — membre, chaque équipe, org, tenant, instances plateforme — sur le chemin le plus
    chaud du produit, pour un résultat dont on ne consomme presque toujours que le
    premier élément."""
    assert inspect.isgeneratorfunction(chain_resolution._paliers), (
        "`_paliers` n'est plus un générateur : le coût de la traversée redevient "
        "celui de tous les paliers, à chaque appel")


def test_la_lecture_consomme_paresseusement():
    """Corollaire du générateur : si le premier palier répond, ce qui suit n'est
    même pas CALCULÉ — pas seulement pas sondé."""
    calcules: list[str] = []

    def paliers():
        calcules.append("user")
        yield chain_resolution.ChainPick("user", "member", "x")
        calcules.append("org")            # ne doit jamais s'exécuter
        yield chain_resolution.ChainPick("org", "org", "7")

    chain_resolution.rung_for_picks(paliers(), _Sonde(repond="user"), "u", "serper", 7)
    assert calcules == ["user"], "le palier suivant a été calculé alors qu'il ne sert pas"


# --- la désignation, elle, ne bouge pas ---------------------------------------

def test_la_designation_reste_le_PREMIER_palier(monkeypatch):
    """Le relevé de fenêtre compare des DÉSIGNATIONS. Lui donner une liste ferait
    bouger ce qu'il mesure au moment même où on corrige la lecture — on refermerait
    un trou en en ouvrant un dans la mesure."""
    monkeypatch.setattr(chain_resolution, "_paliers",
                        lambda *a, **k: _paliers("user", "org"))
    pick, nuance = chain_resolution.chain_verdict("u-1", "serper", org=7)
    assert pick is not None and pick.mode == "user"
    assert nuance is None, "la nuance ne se calcule QUE si la chaîne se tait"


def test_la_nuance_du_trou_survit_au_generateur(monkeypatch):
    """⚠️ La contrainte qui se perd en général. La nuance distingue les deux classes
    hors-modèle suivies depuis trois jours ; elle est la valeur de RETOUR du générateur
    (PEP 380), donc elle n'existe que lorsqu'il s'épuise sans rien céder."""
    def muette(*a, **k):
        return
        yield                                   # noqa: unreachable — générateur vide
    monkeypatch.setattr(chain_resolution, "_paliers",
                        lambda *a, **k: (lambda: (yield from ()) or None)())

    def vide_avec_nuance(*a, **k):
        if False:
            yield
        return "free_tier_hors_modele"
    monkeypatch.setattr(chain_resolution, "_paliers", vide_avec_nuance)
    pick, nuance = chain_resolution.chain_verdict("u-1", "serper", org=7)
    assert pick is None
    assert nuance == "free_tier_hors_modele", (
        "la nuance est perdue : le relevé deviendrait aveugle sur la classe du trou")


# --- le consommateur RÉEL, pas seulement la fonction ---------------------------

def test_le_chemin_servi_PARCOURT_les_paliers(monkeypatch):
    """⚠️ Le cliquet qui manquait, et son absence a failli me tromper : les tests
    ci-dessus exercent `rung_for_picks` **directement**. En réintroduisant le défaut
    dans son appelant — relire le palier désigné au lieu de parcourir — ils restaient
    tous VERTS. Un test qui vérifie la fonction ne dit rien de qui l'appelle : c'est
    la même faute que celle qui a fait casser la découverte en production le 13/08.

    On lit donc le chemin réellement servi.
    """
    from oto_mcp.access import chain_shadow
    monkeypatch.setenv("OTO_L7_DECIDE", "chain")
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    monkeypatch.setattr(chain_resolution, "chain_paliers",
                        lambda *a, **k: _paliers("user", "org"))
    win = chain_shadow.decide("serper", "u-1", 7, probe=_Sonde(repond="org"))
    assert win is not None and win.mode == "org"
