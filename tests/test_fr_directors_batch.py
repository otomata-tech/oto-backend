"""`fr_directors` en lot, et la fin des trois silences (#612).

Une liste vide de dirigeants avait trois causes et une seule forme. Ces tests
décrivent la réponse SERVIE — un SIREN inconnu se nomme, une forme juridique qui
ne déclare personne se distingue d'une société sans dirigeant, et la qualité
absente de l'entrepreneur individuel est posée en se disant déduite.

Proxies FOD (`fod_fr`) stubés : pas de réseau. Les catégories juridiques des
stubs sont celles MESURÉES le 2026-09-01 sur la surface servie (cf. la docstring
de `oto_mcp.fr_registre`).
"""
from __future__ import annotations

import pytest

from oto_mcp import fr_registre
from oto_mcp.mcp_errors import McpError


class _Reg:
    """FastMCP minimal : capture les fonctions décorées par @mcp.tool()."""

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        if a and callable(a[0]):  # @mcp.tool sans parenthèses
            return deco(a[0])
        return deco


# Un échantillon par cas de bord, calqué sur le réel.
_FICHES = {
    # SASU : le cas nominal — une personne physique, une qualité déclarée.
    "106974637": {
        "siren": "106974637", "nom_complet": "OTOMATA", "nature_juridique": "5710",
        "dirigeants": [{"nom": "LAPORTE", "prenoms": "ALEXIS", "qualite": "Président de SAS",
                        "type_dirigeant": "personne physique"}],
    },
    # Société immatriculée SANS dirigeant au registre : la vide qui EST un signal.
    "500000005": {
        "siren": "500000005", "nom_complet": "SOCIETE SANS DIRIGEANT",
        "nature_juridique": "5499", "dirigeants": [],
    },
    # Association : la vide qui NE dit RIEN (mesuré sur LE SOUVENIR FRANCAIS).
    "775676182": {
        "siren": "775676182", "nom_complet": "LE SOUVENIR FRANCAIS",
        "nature_juridique": "9220", "dirigeants": [],
    },
    # Commune : hors registre aussi (mesuré sur COMMUNE DE MARSEILLE).
    "211300553": {
        "siren": "211300553", "nom_complet": "COMMUNE DE MARSEILLE",
        "nature_juridique": "7210", "dirigeants": [],
    },
    # Entrepreneur individuel : la personne EST là, sa qualité est nulle.
    "478464803": {
        "siren": "478464803", "nom_complet": "LAURENT DAURE", "nature_juridique": "1000",
        "dirigeants": [{"nom": "DAURE", "prenoms": "LAURENT", "qualite": None,
                        "type_dirigeant": "personne physique"}],
    },
    # Société dont le SEUL dirigeant est une société : aucune personne NOMMÉE.
    "420751455": {
        "siren": "420751455", "nom_complet": "HOLDEE", "nature_juridique": "5710",
        "dirigeants": [{"denomination": "GROUPE OLYMPE", "siren": "479274789",
                        "qualite": "Autre", "type_dirigeant": "personne morale"}],
    },
    # Famille 4 (droit public à activité commerciale) : mixte, donc indéterminée.
    "400000004": {
        "siren": "400000004", "nom_complet": "REGIE MUNICIPALE",
        "nature_juridique": "4110", "dirigeants": [],
    },
    # Société dont la qualité est nulle SANS être un EI : on n'y touche pas.
    "510000001": {
        "siren": "510000001", "nom_complet": "SOCIETE QUALITE NULLE",
        "nature_juridique": "5710",
        "dirigeants": [{"nom": "X", "prenoms": "Y", "qualite": None,
                        "type_dirigeant": "personne physique"}],
    },
}


class _Entreprises:
    """Double du proxy FOD — les DEUX méthodes de sa surface réelle.

    `get_directors` n'est plus appelée par le tool, mais elle reste ici : sans
    elle, ces tests joués contre l'ancien code échouent en `AttributeError` (le
    double est incomplet) au lieu d'échouer sur le SILENCE qu'ils visent. Un
    rouge qui vient de l'instrument ne prouve rien du vérifié.
    """

    def get_by_siren(self, siren):
        if siren == "999999999":
            raise RuntimeError("identity down")
        return _FICHES.get(siren)  # None = inconnu du répertoire

    def get_directors(self, siren):
        # Le corps EXACT de `france_opendata` : c'est ce `[]` sur `None` qui
        # rendait un SIREN inconnu indistinguable d'une entreprise sans dirigeant.
        fiche_amont = self.get_by_siren(siren)
        return (fiche_amont or {}).get("dirigeants", [])


class _Noop:
    def __init__(self, *a, **k): ...


@pytest.fixture()
def fr_directors(monkeypatch):
    monkeypatch.setattr("oto_mcp.fod.fr.entreprises", _Entreprises())
    monkeypatch.setattr("oto_mcp.fod.fr.inpi", _Noop())
    monkeypatch.setattr("oto_mcp.fod.fr.bodacc", _Noop())
    monkeypatch.setattr("oto_mcp.fod.fr.egapro", _Noop())
    monkeypatch.setattr("oto.tools.sirene.SireneClient", _Noop)
    from oto_mcp.tools import fr
    reg = _Reg()
    fr.register(reg)
    return reg.tools["fr_directors"]


# --- Le silence n°1 : le SIREN inconnu ---------------------------------------

def test_siren_inconnu_se_nomme_au_lieu_de_rendre_une_liste_vide(fr_directors):
    out = fr_directors(siren="000000000")
    assert out["error"] == "not_found"
    assert out["siren"] == "000000000"


def test_le_lot_nomme_ses_introuvables_deux_fois(fr_directors):
    out = fr_directors(sirens=["106974637", "000000000", "775676182"])
    # `count` = les fiches OBTENUES, pas les lignes rendues (otomata-tech/oto#44) :
    # un introuvable n'est pas une fiche. Les trois lignes sont bien là.
    assert out["count"] == 2
    assert len(out["entreprises"]) == 3
    assert out["entreprises"][1] == {"error": "not_found", "siren": "000000000"}
    assert out["not_found"] == ["000000000"]


# --- Le silence n°2 : la forme juridique qui ne déclare personne -------------

def test_association_et_societe_sans_dirigeant_ne_se_lisent_plus_pareil(fr_directors):
    asso = fr_directors(siren="775676182")
    societe = fr_directors(siren="500000005")
    assert asso["dirigeants"] == societe["dirigeants"] == []
    # C'est TOUT l'objet du lot : même liste vide, deux verdicts opposés.
    assert asso["registre"] == fr_registre.REGISTRE_HORS
    assert societe["registre"] == fr_registre.REGISTRE_ATTENDU
    assert asso["note"] != societe["note"]
    assert "ne dit rien de l'entreprise" in asso["note"]


def test_la_commune_est_hors_registre_comme_l_association(fr_directors):
    assert fr_directors(siren="211300553")["registre"] == fr_registre.REGISTRE_HORS


def test_une_famille_mixte_reste_indeterminee(fr_directors):
    # Familles 3 et 4 : une partie seulement est immatriculée. On ne tranche pas
    # dans le sens rassurant — la troisième catégorie existe pour ça.
    out = fr_directors(siren="400000004")
    assert out["registre"] == fr_registre.REGISTRE_INDETERMINE
    assert "pas concluante" in out["note"]


def test_la_forme_est_nommee_en_clair(fr_directors):
    assert fr_directors(siren="775676182")["forme"] == "Groupement de droit privé"
    assert fr_directors(siren="106974637")["forme"] == "Société commerciale"


# --- Le silence n°3 : la qualité vide de l'entrepreneur individuel -----------

def test_l_entrepreneur_individuel_recoit_une_qualite_dite_deduite(fr_directors):
    d = fr_directors(siren="478464803")["dirigeants"][0]
    assert d["qualite"] == fr_registre.QUALITE_EI
    # Posée par nous, pas par le registre : la réponse le DIT.
    assert "déduite" in d["qualite_deduite"]


def test_une_qualite_nulle_hors_famille_1_reste_nulle(fr_directors):
    d = fr_directors(siren="510000001")["dirigeants"][0]
    assert d["qualite"] is None
    assert "qualite_deduite" not in d


# --- « Aucune personne physique nommée » : la question du terrain ------------

def test_un_dirigeant_personne_morale_ne_compte_pas_comme_personne_nommee(fr_directors):
    out = fr_directors(siren="420751455")
    assert out["dirigeants"] != []
    assert out["personnes_physiques"] == 0
    # Et il n'y a pas de `note` : la liste n'est pas vide, elle ne nomme personne.
    assert "note" not in out


def test_la_synthese_partitionne_le_lot(fr_directors):
    sirens = ["106974637", "500000005", "775676182", "478464803",
              "420751455", "400000004", "000000000", "999999999"]
    out = fr_directors(sirens=sirens)
    assert out["synthese"] == {
        "avec_personne_physique": 2,            # 106974637 (SASU) + 478464803 (EI)
        "dirigeant_personne_morale_seulement": 1,
        "aucun_dirigeant_declare": 1,           # la société immatriculée, vide
        "forme_sans_dirigeant_au_registre": 1,  # l'association
        "registre_indetermine": 1,
        "not_found": 1,
        "erreur": 1,
    }
    # Exhaustive et exclusive : rien ne se perd entre les catégories. La synthèse
    # partitionne les LIGNES RENDUES — `count` ne compte que les fiches obtenues
    # depuis oto#44, les deux totaux ne se confondent donc plus.
    assert sum(out["synthese"].values()) == len(out["entreprises"]) == len(sirens)
    assert out["obtenues"] + out["en_echec"] + len(out["not_found"]) == len(sirens), (
        "obtenues / en échec / introuvables doivent partitionner le lot")


def test_toutes_les_categories_sont_rendues_meme_a_zero(fr_directors):
    # Une clé absente se lit « pas mesuré » ; un 0 se lit « mesuré, aucun ».
    out = fr_directors(sirens=["106974637"])
    assert out["synthese"]["not_found"] == 0
    assert out["synthese"]["forme_sans_dirigeant_au_registre"] == 0


# --- Le lot : ordre, dégradation par SIREN, bornes d'entrée -----------------

def test_l_ordre_d_entree_est_conserve(fr_directors):
    ordre = ["775676182", "106974637", "478464803"]
    out = fr_directors(sirens=ordre)
    assert [e["siren"] for e in out["entreprises"]] == ordre


def test_un_siren_en_echec_ne_fait_pas_tomber_le_lot_ni_disparaitre_de_la_reponse(fr_directors):
    out = fr_directors(sirens=["106974637", "999999999"])
    # LE point d'oto#44 : `count` valait 2 alors qu'une seule fiche avait été
    # obtenue. Un agent qui lit `count` et `not_found` concluait « 2 fiches, aucune
    # introuvable » — l'entreprise en échec passait pour « sans dirigeant » ou
    # disparaissait du livrable.
    assert out["count"] == 1 and out["obtenues"] == 1
    assert out["en_echec"] == 1
    assert out["erreurs"] == ["999999999"], (
        "les SIREN en échec doivent être NOMMÉS au premier niveau, comme les "
        "introuvables — une liste de cent fiches ne se relit pas pour les retrouver")
    assert out["entreprises"][1]["siren"] == "999999999"
    assert out["entreprises"][1]["error"].startswith("RuntimeError")
    # Un échec amont n'est PAS un « not_found » : le numéro peut être bon.
    assert out["not_found"] == []


def test_bornes_d_entree(fr_directors):
    with pytest.raises(McpError, match="pas les deux"):
        fr_directors(siren="1", sirens=["2"])
    with pytest.raises(McpError, match="pas les deux"):
        fr_directors()
    with pytest.raises(McpError, match="vide"):
        fr_directors(sirens=["  "])
    with pytest.raises(McpError, match="limité à 100"):
        fr_directors(sirens=[str(i).zfill(9) for i in range(101)])
