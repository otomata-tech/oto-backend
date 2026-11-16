"""Le connecteur Custom API s'appelle `api` (ex-`http`) — et ses anciens noms d'outils
répondent encore, en alias dépréciés.

Une surface ne se renomme pas, elle se double (`docs/alias-deprecies.md`) : `http_get`
est servi par `api_get`, mêmes arguments, et l'avis le dit. Le NOM du connecteur et le
`kind` de source de fichier, eux, n'ont pas d'alias : ils refusent en nommant `api`.
"""
from __future__ import annotations

import datetime

import pytest

from oto_mcp import deprecations, file_source, outils_retires, providers

ANCIENS = {"http": "api", "http_act": "api_act", "http_get": "api_get",
           "http_post": "api_post", "http_doc": "api_doc"}


def test_le_registre_sert_api_et_plus_http():
    assert "api" in providers.REGISTRY and "http" not in providers.REGISTRY
    assert providers.REGISTRY["api"].label == "Custom API"
    assert providers.CONNECTEURS_RENOMMES == {"http": "api"}


@pytest.mark.parametrize("ancien,nouveau", sorted(ANCIENS.items()))
def test_chaque_ancien_outil_est_un_alias_deprecie_du_nouveau(ancien, nouveau):
    assert deprecations.OUTILS_CONNECTEUR_API[ancien] == nouveau
    assert deprecations.tool_canonique(ancien) == nouveau
    assert ancien in deprecations.tools_deprecies_de(nouveau)
    assert outils_retires.retrait(ancien) is None, (
        f"`{ancien}` est REFUSÉ : un alias déprécié répond, il ne refuse pas.")


def test_aucun_nouveau_nom_n_est_retire_ni_deprecie():
    assert all(outils_retires.retrait(n) is None for n in ANCIENS.values())
    assert all(deprecations.tool_canonique(n) == n for n in ANCIENS.values())


def test_l_annonce_est_servie_un_mois_et_dit_que_l_ancien_nom_repond():
    jour = deprecations.ANNONCE_CONNECTEUR_API
    annonce = deprecations.annonce_connecteur_api_du_jour(jour)
    assert all(f"`{a}` → `{n}`" in annonce for a, n in ANCIENS.items())
    assert "still answer" in annonce
    assert deprecations.RETRAIT_CONNECTEUR_API.isoformat() in annonce
    fin = deprecations.ANNONCE_CONNECTEUR_API_JUSQU_AU
    assert fin == datetime.date(2026, 12, 12)
    assert deprecations.annonce_connecteur_api_du_jour(fin) is not None
    assert deprecations.annonce_connecteur_api_du_jour(
        fin + datetime.timedelta(days=1)) is None


def test_le_refus_d_un_connecteur_renomme_nomme_le_nouveau():
    assert "`api`" in providers.refus_renomme("http")
    assert providers.refus_renomme("serper") == ""


def test_la_source_de_fichier_http_refuse_en_nommant_api():
    with pytest.raises(file_source.FileSourceError, match='kind="api"'):
        file_source.resolve({"kind": "http", "path": "/x.csv"})


def test_un_agent_dont_la_liste_nomme_l_ancien_outil_garde_son_droit(monkeypatch):
    """La révision réécrit les listes enregistrées ; une liste qui nommerait ENCORE
    `http_get` (travail en vol, liste reposée par un client d'hier) permet `api_get` —
    l'outil s'y lit par son nom d'aujourd'hui — et rien d'autre."""
    from oto_mcp import outils_du_travail
    monkeypatch.setattr(outils_du_travail, "_charge",
                        lambda job_id: {"tools": ["http_get", "serper_search"]})
    permis = outils_du_travail.permis_du_travail(7)
    assert {"api_get", "serper_search"} <= permis
    assert "api_post" not in permis and "api_act" not in permis
