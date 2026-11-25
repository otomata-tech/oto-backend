"""Le contrôle automatique de ce qui part dans la communauté (`contenu_publiable`).

Fonction pure : ce qu'on fige, ce sont les CATÉGORIES rendues — ce qui doit être refusé
l'est, ce qu'un modèle de procédure porte légitimement (exemples réservés, dates,
SIRET, slugs, chemins, `<slot:…>`) passe, et la valeur trouvée n'est jamais recopiée.
Les valeurs « secrètes » de ces tests sont fabriquées : aucune n'est réelle.
"""
from __future__ import annotations

import pytest

from oto_mcp import contenu_publiable as cp


@pytest.mark.parametrize("texte, attendu", [
    ("Écrire à jeanne.dupont@acme-industrie.fr", ["email"]),
    ("Contact : sales@sub.domaine.co.uk", ["email"]),
    ("Appeler le 06 12 34 56 78", ["phone"]),
    ("Appeler le 06.12.34.56.78", ["phone"]),
    ("Appeler le 0612345678", ["phone"]),
    ("Standard : +33 1 23 45 67 89", ["phone"]),
    ("US: +1-415-555-0100", ["phone"]),
    ("clé sk-proj-AbCdEfGhIjKlMnOpQrStUv12", ["secret"]),
    ("clé sk_live_51HxxxxxxxxxxxxABCDEF", ["secret"]),
    ("slack xoxb-1234567890-abcdefghijkl", ["secret"]),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", ["secret"]),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", ["secret"]),
    ("AWS AKIAABCDEFGHIJKLMNOP", ["secret"]),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk_lmnop", ["secret"]),
    ("hex 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", ["secret"]),
    ("b64 Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFiQ2RFZkdoSWpLbE1uT3A=", ["secret"]),
    ("voir https://api.tiers.example.net/v1/export?api_key=abcdef123456", ["url_with_secret"]),
    ("voir https://hooks.tiers.io/run?token=0123456789abcdef", ["url_with_secret"]),
    ("voir https://admin:motdepasse@intranet.tiers.io/", ["url_with_secret"]),
])
def test_ce_qui_doit_etre_refuse_l_est(texte, attendu):
    assert cp.categories(texte) == attendu


@pytest.mark.parametrize("texte", [
    "Écrire à prenom@example.com, ou à <slot:contact_email>.",
    "Écrire à contact@acme.test (domaine réservé).",
    "Le 12/03/2026 à 14:30, relancer sous 48 h.",
    "SIRET 12345678901234, SIREN 123 456 789.",
    "Montant : 1 234 567 €, 25 % de remise.",
    "Utilise <tool:serper_search> puis `data_write` sur le tableau 1234.",
    "Lire https://docs.example.org/guide?page=2&lang=fr pour la méthode.",
    "Chemin /api/me/guide-library/veille-concurrence-hebdomadaire-des-prix",
    "Nom de colonne tres_long_nom_de_colonne_sans_aucun_chiffre_dedans_vraiment",
    "",
])
def test_ce_qu_un_modele_porte_legitimement_passe(texte):
    assert cp.categories(texte) == []


def test_les_constats_nomment_le_champ_et_jamais_la_valeur():
    constats = cp.inspecter({"title": "Relance", "body_md": "à paul@vraie-boite.fr",
                             "description": "+33 6 11 22 33 44"})
    assert constats == [{"field": "body_md", "kind": "email"},
                        {"field": "description", "kind": "phone"}]
    msg = cp.message_de_refus(constats)
    assert "body_md" in msg and "adresse email" in msg and "téléphone" in msg
    for valeur in ("paul", "vraie-boite", "11 22"):
        assert valeur not in msg


def test_le_html_d_une_app_ordinaire_passe():
    """Une app charge une bibliothèque de CDN avec son empreinte d'intégrité, embarque une
    image en `data:`, dessine en SVG, prend une police : rien de tout cela n'est un
    secret. Une vraie clé posée à côté l'est toujours."""
    html = (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" '
        'integrity="sha384-9nhczxUqK87bcKHh20fSQcTGD4qq5GhayNYSYWqwBkINBhOfQLg/P5HG5lF1urn4" '
        'crossorigin="anonymous"></script>'
        '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUl'
        'EQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==">'
        '<svg><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/>'
        '</svg><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600" '
        'rel="stylesheet"><script>oto.rows("deals", {limit: 50})</script>')
    assert cp.categories(html) == []
    assert cp.categories(html + "<script>const k = 'sk-proj-AbCdEfGhIjKlMnOpQrStUv12'</script>") \
        == ["secret"]
