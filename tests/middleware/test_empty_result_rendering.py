"""Un résultat d'outil VIDE se sert en PHRASE, jamais en structure nue (oto#32).

Le 2026-08-27, une flotte d'agents a perdu la moitié de ses départs sur un
`{"total_count": 0, "rows": []}` rendu tel quel dans le canal texte : le décodage
du modèle dégénère dessus — il recopie la structure, boucle sur des centaines de
`]}`, reprend en prose, et le fournisseur encadre toute la sortie comme un appel
d'outil dont le nom est la narration. 16 des 26 faux départs d'une campagne, 10 des
11 d'une vague de production.

Le banc fait traverser à un résultat la chaîne RÉELLE montée sur `_test_mcp()` (les
instances de middleware du vrai serveur, dans leur vrai ordre) et lit ce qui sort
côté client : l'ordre des middlewares est ici la moitié du correctif, un banc qui
n'appellerait que la fonction de rendu ne prouverait donc rien. Aucune base n'est
requise — le journal d'appels est best-effort et se contente de râler.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import asyncio

import pytest
from fastmcp import Client, FastMCP
from oto.tools.common import FieldFilter

from oto_mcp import redaction, server, session_org

# La forme EXACTE capturée en production, octet pour octet.
VIDE_CAPTURE = {"total_count": 0, "rows": []}


def _banc(fn, *, nom: str = "recherche"):
    """Un serveur d'un seul outil, sous la chaîne de middlewares du VRAI serveur."""
    m = FastMCP("banc")
    for mw in _test_mcp().middleware:
        m.add_middleware(mw)
    m.tool(name=nom)(fn)
    return m


def _servir_brut(m: FastMCP, nom: str = "recherche"):
    """Le résultat tel que le client le reçoit, blocs de contenu compris."""
    async def appel():
        async with Client(m) as c:
            return await c.call_tool(nom, {})
    return asyncio.run(appel())


def _servir(m: FastMCP, nom: str = "recherche"):
    """Ce que le CLIENT reçoit : (texte servi, canal structuré)."""
    r = _servir_brut(m, nom)
    return "".join(getattr(b, "text", "") for b in r.content), r.structured_content


def _outil_vide(payload):
    def recherche() -> dict:
        return payload
    return recherche


# --- Le canal texte : la phrase, et rien d'autre ----------------------------

def test_le_dict_vide_capture_ne_part_jamais_en_structure():
    """RED avant le correctif : le texte servi était `{"total_count":0,"rows":[]}`."""
    texte, structure = _servir(_banc(_outil_vide(VIDE_CAPTURE)))
    assert texte == redaction.EMPTY_MESSAGE_DEFAULT
    assert "[]" not in texte and "{}" not in texte
    assert "[" not in texte and "{" not in texte
    # Le canal structuré, lui, porte toujours la structure vide : c'est la structure
    # DANS LE TEXTE qui déclenche la dégénérescence, pas son existence.
    assert structure == VIDE_CAPTURE


def test_la_liste_vide_ne_part_jamais_en_structure(monkeypatch):
    """La liste vide n'atteint le canal texte qu'en repassant par `rebuild_result`
    (rédaction, écho de compte) — sans quoi fastmcp ne sérialise rien. On l'y met
    donc : une policy de rédaction active, et le texte servi valait `[]`."""
    monkeypatch.setattr(redaction, "_resolve_field_filter",
                        lambda _s: FieldFilter(rules={"secret": "drop"}))

    def recherche() -> list:
        return []

    texte, _ = _servir(_banc(recherche))
    assert texte == redaction.EMPTY_MESSAGE_DEFAULT
    assert "[]" not in texte


def test_un_resultat_non_vide_est_rendu_tel_quel():
    plein = {"total_count": 1, "rows": [{"id": 1}]}
    texte, structure = _servir(_banc(_outil_vide(plein)))
    assert '"id":1' in texte.replace(" ", "")
    assert structure == plein


def test_le_gabarit_declare_par_l_outil_est_servi():
    texte, _ = _servir(_banc(_outil_vide({"results": [], "total_count": 0}),
                             nom="fr_accords_search"), nom="fr_accords_search")
    assert texte == "Aucun accord déposé pour ce SIREN."
    assert texte == redaction.EMPTY_MESSAGES["fr_accords_search"]


def test_l_echo_de_compte_ne_retablit_pas_la_structure():
    """L'écho de compte réémet le payload en JSON dans le canal texte. Il est plus
    interne que le rendu du vide — s'il tournait après, il rétablirait très
    exactement la structure qu'on vient d'en retirer."""
    def recherche() -> dict:
        # Ce que fait un vrai connecteur quand il a résolu un compte NOMMÉ.
        session_org.note_call_trace(resolved_connector="fr", resolved_account="client-x")
        return dict(VIDE_CAPTURE)

    texte, structure = _servir(_banc(recherche, nom="fr_search"), nom="fr_search")
    assert texte == redaction.EMPTY_MESSAGE_DEFAULT
    assert "[]" not in texte
    # L'écho reste lisible là où il ne nuit pas : le canal structuré.
    assert structure.get("_account") == "client-x"


def test_une_erreur_n_est_pas_reecrite():
    def recherche() -> dict:
        raise ValueError("boum")

    m = _banc(recherche)

    async def appel():
        async with Client(m) as c:
            return await c.call_tool("recherche", {}, raise_on_error=False)

    r = asyncio.run(appel())
    assert r.is_error
    assert redaction.EMPTY_MESSAGE_DEFAULT not in "".join(
        getattr(b, "text", "") for b in r.content)


# --- Le vide MUET : zéro bloc de contenu ------------------------------------
# `_convert_to_content([])` ne rend AUCUN bloc (idem `None`), là où `[1]` rend un bloc
# texte. Un tour sans contenu est le pire cas : le modèle ne reçoit littéralement rien.

def test_une_liste_vide_produit_exactement_un_bloc_texte():
    """Le cas fondateur : forme de `fr_directors` sur un SIREN sans dirigeant.

    Cet outil rend un dict depuis le 2026-09-01 (#612) — la forme testée ici
    reste celle de tout outil qui rend une liste.
    """
    def recherche() -> list[dict]:
        return []

    r = _servir_brut(_banc(recherche))
    assert len(r.content) == 1
    assert type(r.content[0]).__name__ == "TextContent"
    assert r.content[0].text == redaction.EMPTY_MESSAGE_DEFAULT


def test_une_liste_vide_non_annotee_produit_exactement_un_bloc_texte():
    """Sans annotation, FastMCP ne pose même pas d'enveloppe `{"result": …}` : le
    résultat est muet sur les DEUX canaux."""
    def recherche():
        return []

    r = _servir_brut(_banc(recherche))
    assert len(r.content) == 1
    assert r.content[0].text == redaction.EMPTY_MESSAGE_DEFAULT


def test_un_retour_none_produit_exactement_un_bloc_texte():
    def recherche():
        return None

    r = _servir_brut(_banc(recherche))
    assert len(r.content) == 1
    assert r.content[0].text == redaction.EMPTY_MESSAGE_DEFAULT


def test_une_liste_non_vide_est_servie_a_l_identique():
    """Le témoin : hors du vide, rien ne bouge."""
    def recherche() -> list[int]:
        return [1]

    r = _servir_brut(_banc(recherche))
    assert len(r.content) == 1
    assert r.content[0].text == "[1]"
    assert r.structured_content == {"result": [1]}


def test_le_gabarit_par_outil_vaut_aussi_pour_le_vide_muet():
    def fr_accords_search() -> list[dict]:
        return []

    r = _servir_brut(_banc(fr_accords_search, nom="fr_accords_search"),
                     nom="fr_accords_search")
    assert len(r.content) == 1
    assert r.content[0].text == "Aucun accord déposé pour ce SIREN."


# --- Ce qui ressemble à un vide sans en être un ------------------------------

def test_un_accuse_d_ecriture_garde_sa_structure():
    """« Opération réussie, 0 supprimé » n'est PAS « aucun résultat ». Répondre
    « Aucun résultat pour cette recherche. » à qui vient de supprimer zéro ligne
    effacerait le seul fait utile de la réponse : que l'opération a abouti."""
    accuse = {"ok": True, "deleted": []}
    texte, structure = _servir(_banc(_outil_vide(accuse), nom="data_write"),
                               nom="data_write")
    assert redaction.is_empty_payload(accuse) is False
    assert '"ok":true' in texte.replace(" ", "").lower()
    assert "deleted" in texte
    assert structure == accuse


def test_une_notice_de_troncature_garde_sa_structure():
    """Une réponse coupée par un plafond n'est pas une absence de résultat : la
    rendre en phrase ferait conclure « il n'y a rien » là où le plafond a coupé
    avant d'avoir cherché. Forme réelle d'un client oto-core (sellsy) :
    `{"data": [...], "pages": …, "truncated": …}`."""
    tronque = {"data": [], "pages": 3, "truncated": True}
    texte, structure = _servir(_banc(_outil_vide(tronque)))
    assert redaction.is_empty_payload(tronque) is False
    assert "truncated" in texte
    assert structure == tronque


def test_une_notice_de_troncature_imbriquee_garde_sa_structure():
    """`fr_accords_search` — l'outil même de l'incident — porte sa notice un cran
    plus bas : `effectifs_filter.truncated` dit que le vivier est PLUS GRAND que le
    plafond examiné, donc que la réponse n'est pas exhaustive."""
    tronque = {"results": [], "total_count": 0, "effectifs_filter": {"truncated": True}}
    texte, _ = _servir(_banc(_outil_vide(tronque), nom="fr_accords_search"),
                       nom="fr_accords_search")
    assert redaction.is_empty_payload(tronque) is False
    assert "truncated" in texte
    assert redaction.EMPTY_MESSAGES["fr_accords_search"] not in texte


# --- La règle de détection, isolée -----------------------------------------

@pytest.mark.parametrize("payload", [
    [],
    VIDE_CAPTURE,
    {"rows": []},
    {"results": [], "total_count": 0},
    {"result": []},                        # l'enveloppe fastmcp d'un retour `list`
    {"items": [], "hits": [], "count": 0},
    {"total_count": 0},                    # le compteur seul suffit à affirmer le vide
    {"rows": [], "_account": "client-x"},   # un scalaire à côté ne réveille rien
    {"rows": [], "next_cursor": None},      # un curseur nul ne dit rien de plus
    {"data": [], "pages": 0, "truncated": False},  # notice PRÉSENTE mais négative
    {"rows": [], "warnings": []},          # une notice VIDE ne dit rien
])
def test_est_vide(payload):
    assert redaction.is_empty_payload(payload) is True


@pytest.mark.parametrize("payload", [
    None,
    "",
    0,
    {},                                     # aucun signal reconnu : rien à affirmer
    {"ok": True},
    {"rows": [{"id": 1}]},
    {"rows": [], "total_count": 3},         # un compteur non nul CONTREDIT la collection
    {"rows": [], "items": [{"id": 1}]},     # une seule collection peuplée suffit
    {"data": {"rows": []}},                 # le signal ne se cherche qu'à la racine
    {"ok": True, "deleted": []},            # accusé d'écriture : `deleted` n'est pas
                                            # une collection reconnue
    {"created": [], "skipped": []},         # idem, un bilan d'écriture n'est pas un vide
    {"total": 0, "succeeded": 0, "failed": []},   # webflow : le COMPTEUR d'un accusé
    {"total": 0, "imported": 0, "items": []},     # waalaxy : « succès, 0 élément »
    {"dry_run": True, "would_create": []},        # une simulation n'a rien cherché
    {"rows": [], "truncated": True},        # coupé par un plafond ≠ rien trouvé
    {"results": [], "warning": "quota"},    # un avertissement doit rester lisible
    {"count": 0, "results": [], "note": "No company found."},   # gr : notice à côté
    {"rows": [], "_etablissements_truncated": 25},   # famille `*_truncated`
    {"items": [], "texte_tronque": True},            # famille `*_tronqu*`
    {"results": [], "filtre_ca_avertissement": "…"},  # famille `*avertissement*`
    {"data": "une chaîne"},                 # clé reconnue mais valeur non-liste : ignorée
])
def test_n_est_pas_vide(payload):
    assert redaction.is_empty_payload(payload) is False


def test_le_compteur_booleen_n_est_pas_un_compteur():
    """`True` est un `int` en Python — un drapeau nommé `count` ne doit pas se lire
    comme un volume."""
    assert redaction.is_empty_payload({"rows": [], "count": True}) is True


# --- La garde générique : AUCUN outil monté n'échappe à la règle ------------

def _outils_montes() -> list[str]:
    """Les outils du VRAI montage (connecteurs + capacités), pas une liste écrite
    à la main : c'est ce qui fait qu'un outil ajouté demain est couvert d'office."""
    return [t.name for t in asyncio.run(_test_mcp().list_tools(run_middleware=False))]


def _vides_reconnus() -> list[dict]:
    """Un payload vide par SIGNAL reconnu — c'est la forme, et elle seule, qui décide.

    La garde ne porte que sur les outils « rendant une collection reconnue ou un
    compteur », et ce périmètre se prend par la FORME plutôt que par le schéma de
    sortie : sur les 610 outils montés, 514 déclarent un `output_schema`, mais la
    seule propriété qu'on y trouve est le `result` de l'enveloppe fastmcp, et aucun
    n'est typé `array`. Un schéma déclaré ne dit donc pas quel outil rend une
    collection ; l'exercer le dit.
    """
    return ([{cle: []} for cle in redaction._COLLECTION_KEYS]
            + [{cle: 0} for cle in redaction._COUNTER_KEYS])


def test_chaque_signal_de_vide_est_bien_reconnu():
    """La liste FERMÉE est le contrat : chaque clé qui y entre doit valoir signal."""
    for payload in _vides_reconnus():
        assert redaction.is_empty_payload(payload) is True, payload


def test_aucun_gabarit_declare_ne_porte_de_structure():
    noms = _outils_montes()
    # Garde-fou de l'instrument : un registre vide ferait passer ce test à vide.
    assert len(noms) > 300, f"registre d'outils suspect ({len(noms)}) — banc invalide"
    fautifs = [n for n in noms
               if any(c in redaction.empty_message(n) for c in "[]{}")]
    assert not fautifs, (
        f"Gabarit de vide porteur d'une structure : {fautifs}. La phrase servie pour "
        "un résultat vide ne doit contenir ni crochet ni accolade — c'est très "
        "exactement le déclencheur qu'on retire.")


def test_aucun_outil_ne_sert_de_structure_sur_un_vide_reconnu():
    """La règle est GÉNÉRIQUE : elle se juge sur la forme du résultat, jamais sur le
    nom de l'outil. On le prouve sur tout le montage — chaque outil monté croisé avec
    chaque signal de vide reconnu — plutôt que sur un échantillon écrit à la main."""
    noms = _outils_montes()
    assert len(noms) > 300, f"registre d'outils suspect ({len(noms)}) — banc invalide"
    for nom in noms:
        phrase = redaction.empty_message(nom)
        for payload in _vides_reconnus():
            rendu = redaction.render_empty(_ResultatFactice(payload), nom)
            texte = "".join(getattr(b, "text", "") for b in rendu.content)
            assert texte == phrase, (nom, payload)
            assert not any(c in texte for c in "[]{}"), (nom, payload)
            assert rendu.structured_content == payload, (nom, payload)


class _ResultatFactice:
    """Ce que rend un tool FastMCP, réduit aux deux canaux qui portent la donnée."""

    def __init__(self, payload):
        self.structured_content = payload
        self.content = []
        self.is_error = False
