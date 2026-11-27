"""Ce que la réponse de `linkedin_unipile_search` dit — et n'a pas dit — d'elle-même.

Allègement (feedback #335) : dé-dup data/items + strip images.
Aveux (#536) : trois amputations muettes mesurées sur UNE même cible (~3 300 salariés),
sans erreur ni indicateur — 25 items servis sur 86 annoncés et `cursor=null` ; 0 résultat
sur une facette employeur que le même compte trouve en `classic` ; page 2 obtenue par
curseur qui rend des profils étrangers au filtre. Dans les trois cas un agent honnête
conclut « vivier vide » ou « population balayée ». La page ne peut pas réparer l'amont,
mais elle doit CESSER de se présenter comme complète.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.tools import unipile as U


def test_slim_dedup_and_strip_images():
    lst = [{"id": "1", "name": "X", "headline": "H",
            "public_picture_url": "a", "public_picture_url_large": "b",
            "background_picture_url": "c", "profile_picture_url": "d"}]
    res = {"data": lst, "items": lst, "next_cursor": "N", "cursor": "N", "total_count": 1}
    out = U._slim_search(res)
    # dé-duplication : plus que items + cursor + total_count
    assert set(out) == {"items", "cursor", "total_count"}
    assert out["cursor"] == "N" and out["total_count"] == 1
    it = out["items"][0]
    # toutes les URLs d'image retirées
    assert not any("picture_url" in k for k in it)
    # champs métier conservés
    assert it["name"] == "X" and it["headline"] == "H" and it["id"] == "1"


def test_slim_reads_data_when_items_absent():
    res = {"data": [{"id": "9", "public_picture_url": "x"}], "next_cursor": "C"}
    out = U._slim_search(res)
    assert out["items"][0]["id"] == "9" and "public_picture_url" not in out["items"][0]
    assert out["cursor"] == "C"


def test_slim_passthrough_non_dict():
    assert U._slim_search([1, 2]) == [1, 2]
    assert U._slim_search(None) is None


def test_une_page_complete_ne_s_alourdit_pas():
    """L'aveu ne se paie qu'en cas d'amputation : quand la page rend toute la
    population annoncée, l'enveloppe reste celle de #335 (le coût token de la
    recherche en bulk était la raison d'être de `_slim_search`)."""
    res = {"items": [{"id": "1"}, {"id": "2"}], "total_count": 2, "cursor": None}
    assert set(U._slim_search(res)) == {"items", "cursor", "total_count"}


def test_le_plafond_sans_curseur_est_nomme_et_chiffre():
    """Cas mesuré : `total_count=86`, 25 items, `cursor=null`. 71 % de la population
    est inatteignable et RIEN ne le signalait — la réponse se lisait comme un
    balayage complet."""
    res = {"items": [{"id": str(i)} for i in range(25)],
           "total_count": 86, "next_cursor": None}
    out = U._slim_search(res, facettes=("company",))

    assert out["returned"] == 25 and out["truncated"] is True
    alerte = " ".join(out["warnings"])
    assert "61" in alerte and "86" in alerte      # ce qui manque, sur quoi
    assert "UNREACHABLE" in alerte             # pas « il reste des pages »


def test_une_page_partielle_avec_curseur_dit_de_continuer():
    """Même écart items/total, mais un curseur existe : la suite est atteignable —
    l'agent doit rappeler, pas resserrer sa recherche. Les deux cas ne se disent
    donc PAS pareil."""
    out = U._slim_search({"items": [{"id": "1"}], "total_count": 40, "cursor": "C"})
    assert out["truncated"] is True
    alerte = " ".join(out["warnings"])
    assert "cursor" in alerte and "UNREACHABLE" not in alerte


def test_zero_sur_facette_ne_se_lit_pas_comme_un_vivier_vide():
    """Cas mesuré : même entreprise, même facette employeur → 0 en
    `sales_navigator`, 10 en `classic`. L'amont n'applique pas toujours la facette
    et ne le dit pas ; un zéro nu faisait conclure à un vivier vide."""
    out = U._slim_search({"items": [], "total_count": 0}, facettes=("company",))
    alerte = " ".join(out["warnings"])
    assert "company" in alerte and "cross-check" in alerte

    # Sans facette posée, un zéro est un zéro : rien à avouer.
    assert "warnings" not in U._slim_search({"items": [], "total_count": 0})


def test_sans_total_ni_curseur_la_page_ne_se_dit_pas_complete():
    """#91 (retour 753) : le palier ordinaire ne porte aucun `total_count`, donc ni
    `returned` ni `truncated` — et l'aveu chiffré, gardé par le total, se taisait
    exactement là où la page peut être un plafond sans curseur."""
    out = U._slim_search({"items": [{"id": "1"}, {"id": "2"}], "cursor": None})
    assert "total_count" not in out and "truncated" not in out
    alerte = " ".join(out["warnings"])
    assert "NO total" in alerte and "swept" in alerte

    # Avec un curseur, la suite est atteignable : rien de plus à avouer.
    assert "warnings" not in U._slim_search({"items": [{"id": "1"}], "cursor": "C"})


def test_une_page_paginee_avertit_que_le_filtre_n_est_pas_re_applique():
    """Cas mesuré en `classic` : page 1 filtrée sur l'employeur, page 2 obtenue par
    curseur rendant des dirigeants d'entreprises étrangères au sujet. La pagination
    amont est cursor-only (oto-core ne renvoie AUCUN corps avec le curseur), donc
    les filtres repassés ici ne sont pas ré-appliqués."""
    out = U._slim_search({"items": [{"id": "1"}], "cursor": "C2"},
                         facettes=("company",), page_suivante=True)
    alerte = " ".join(out["warnings"])
    assert "CURSOR-ONLY" in alerte and "employer" in alerte


def _search_tool():
    from fastmcp import FastMCP

    m = FastMCP("t")
    U.register(m)
    return asyncio.run(m.get_tool("linkedin_unipile_search")).fn


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr(U, "_rate_limit_guard", lambda sub: None)
    return inst


def test_le_tool_transmet_le_contexte_de_la_requete_a_l_aveu(client):
    """Le garde-fou vit dans un helper : encore faut-il que la SURFACE lui dise
    quelles facettes ont été posées et si la page vient d'un curseur. Sans ce
    câblage, l'helper est vert et l'outil servi reste muet."""
    client.search.return_value = {"items": [], "total_count": 0}
    out = _search_tool()(company=["Acme"], api="sales_navigator")
    assert "warnings" in out and "company" in out["warnings"][0]

    client.search.return_value = {"items": [{"id": "1"}], "cursor": "C2"}
    out = _search_tool()(company=["Acme"], cursor="C1")
    assert any("CURSOR-ONLY" in w for w in out["warnings"])


def test_with_photo_garde_la_petite_photo_seulement():
    """`with_photo=True` (26/11/2026) : une seule URL d'image, `profile_picture_url`."""
    lst = [{"id": "1", "public_picture_url": "a", "public_picture_url_large": "b",
            "background_picture_url": "c", "profile_picture_url": "d"}]
    it = U._slim_search({"items": lst}, with_photo=True)["items"][0]
    assert [k for k in it if "picture_url" in k] == ["profile_picture_url"]
    assert it["profile_picture_url"] == "d"


def test_with_photo_se_rabat_sur_public_picture_url():
    it = U._slim_search({"items": [{"id": "9", "public_picture_url": "x"}]},
                        with_photo=True)["items"][0]
    assert it == {"id": "9", "profile_picture_url": "x"}
