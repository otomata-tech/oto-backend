"""Les images CONNUES d'une app (`oto_mcp/images_connues.py`) — la règle, sans base.

Ce qu'on vérifie : quelles adresses un HTML enregistré et une écriture de ligne font
connaître, l'empreinte que le front recalcule (mêmes vecteurs des deux côtés), quelles sources
apprennent,
(l'appel d'une app ouverte, qui n'apprend jamais rien, est éprouvé dans
`test_apps_gestes.py`).
"""
from __future__ import annotations

import pytest

from oto_mcp import geste
from oto_mcp import images_connues as ic

# Vecteurs PARTAGÉS avec le front (vérifiés contre la fonction du cadre, dans un vrai
# navigateur) : même chaîne, même empreinte, des deux côtés.
VECTEURS = {
    "https://example.com/logo.png":
        "adc2cf2dbcde0df0a108066c5f6f973453801b181e7fa92d392540a099f1fd8d",
    "https://media.licdn.com/dms/image/v2/C4E03AQ/profile?e=1767225600&v=beta&t=AbC":
        "4392b67a0fe71f07e380852665bfbd24fae547fd4150fa0561c91addba3468eb",
    "https://x.example/é.png":
        "08d896e67219ccdac53137184a70e0ece367e347a046444df210de9d82a1ebc2",
}


def test_empreinte_sha256_de_la_chaine_exacte():
    import hashlib
    for url in ("https://example.com/logo.png", "https://media.licdn.com/dms/image/x?e=1&t=a"):
        assert ic.empreinte(url) == hashlib.sha256(url.encode()).hexdigest()
        assert ic.cle(url) == f"app-images/{ic.empreinte(url)}"
    # Aucune normalisation : deux formes d'une adresse sont deux adresses.
    assert ic.empreinte("https://example.com/a.png") != ic.empreinte("https://EXAMPLE.com/a.png")


def test_vecteurs_partages_avec_le_front():
    for url, attendu in VECTEURS.items():
        assert ic.empreinte(url) == attendu, url


# ── Le HTML enregistré ─────────────────────────────────────────────────────

HTML = """<img src="https://cdn.example.com/a.png?w=1&amp;h=2">
<img src=https://nu.example/sans-extension>
<img srcset="https://x.example/1x 1x, https://x.example/2x 2x">
<div style="background-image:url(&quot;https://y.example/fond&quot;)"></div>
<style>.c{background:url(https://z.example/c)}</style>
<svg><image width="10" href="https://s.example/i"></image></svg>
<a href="https://site.example/page">un lien</a>
<a href="https://shop.example/confirm?token=1">une action</a>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">
<script>img.src = "https://logo.example/" + row.domain; t.src = 'https://k.example/k.png';
const g = `https://tpl.example/${id}.png`;</script>"""


def test_html_les_adresses_d_image_decodees():
    urls = ic.urls_du_texte(HTML)
    # L'attribut est décodé comme le décode le navigateur, la forme brute est gardée.
    assert "https://cdn.example.com/a.png?w=1&h=2" in urls
    assert "https://cdn.example.com/a.png?w=1&amp;h=2" in urls
    # Là où le navigateur charge une image : prise telle quelle, extension ou pas.
    for u in ("https://nu.example/sans-extension", "https://x.example/1x", "https://x.example/2x",
              "https://y.example/fond", "https://z.example/c", "https://s.example/i",
              "https://k.example/k.png"):
        assert u in urls, u
    # Ni lien, ni action, ni script, ni police ; ni préfixe composé, ni gabarit.
    assert not [u for u in urls if "site.example" in u or "shop.example" in u
                or u.endswith(".js") or "fonts.googleapis" in u
                or "logo.example" in u or "tpl.example" in u]


def test_html_borne_et_sans_doublon():
    html = "".join(f'<img src="https://h.example/{i}.png">' for i in range(500))
    html += '<img src="https://h.example/0.png">'
    urls = ic.urls_du_texte(html)
    assert len(urls) == ic.MAX_PAR_APP and len(set(urls)) == len(urls)


def test_manifeste_lu_aussi():
    import json
    m = {"tables": {"t": {"table": "1", "set": {"logo": ["https://m.example/l.png"]}}}}
    assert "https://m.example/l.png" in ic.urls_du_texte(None, json.dumps(m))


# ── Une écriture de ligne ──────────────────────────────────────────────────

def test_ligne_ce_qui_a_l_air_d_une_image():
    apres = {"name": "Acme", "website": "https://acme.example", "linkedin":
             "https://www.linkedin.com/in/x", "logo": "https://acme.example/l",
             "photo_profil": "https://media.licdn.com/dms/image/v2/x?e=1&t=a",
             "notes": "voir https://acme.example/a.png", "fichier": "https://f.example/doc.jpg",
             "contacts": [{"avatar": "https://g.example/u/1"}],
             "logo_long": "https://acme.example/" + "x" * 3000}
    urls = ic.urls_des_lignes(None, apres)
    assert set(urls) == {"https://acme.example/l",
                         "https://media.licdn.com/dms/image/v2/x?e=1&t=a",
                         "https://f.example/doc.jpg", "https://g.example/u/1"}


def test_ligne_ce_qui_y_etait_deja_n_est_pas_appris():
    """Une valeur posée par un geste d'app, puis recopiée (formule) ou réécrite telle
    quelle par un agent qui change une autre colonne, ne devient pas connue."""
    avant = {"note": "https://evil.example/?d=secret", "logo": "https://a.example/l.png"}
    apres = {**avant, "statut": "ok", "photo": "https://evil.example/?d=secret",
             "logo2": "https://a.example/neuf.png"}
    assert ic.urls_des_lignes(avant, apres) == ["https://a.example/neuf.png"]


def test_ligne_cellule_en_couches():
    """Une cellule en couches rend sa valeur sous le nom de sa colonne ; sa provenance
    n'est pas une image."""
    apres = {"logo": {"valeur": "https://a.example/l", "origine": "https://src.example/p.png",
                      "comment": "trouvé sur https://src.example/c.png"}}
    assert ic.urls_des_lignes(None, apres) == ["https://a.example/l"]


def test_ligne_bornee():
    apres = {f"logo{i}": f"https://h.example/{i}.png" for i in range(50)}
    assert len(ic.urls_des_lignes(None, apres)) == ic.MAX_PAR_LIGNE


# ── Qui apprend ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source,outil,apprend", [
    (geste.AGENT, None, True), (geste.API, None, True),
    # La session du lecteur : l'écran hôte d'une page écrit par elle (page dans un
    # document, annulation d'un geste d'app) — elle n'apprend jamais.
    (geste.CONSOLE, None, False),
    (geste.UPLOAD, None, True), (geste.IMPORT, None, True),
    (geste.SYSTEM, None, False),           # file de travail, formules, maintenance
    (geste.SYSTEM, "jev_rows", True),      # un outil rejoué en tâche de fond
])
def test_sources_qui_apprennent(source, outil, apprend):
    with geste.portee(source, "x", outil=outil):
        assert ic.ecriture_apprend() is apprend
        with ic.geste_d_app():
            assert ic.ecriture_apprend() is False


def test_sans_geste_rien_n_est_appris():
    assert geste.courant() is None
    assert ic.ecriture_apprend() is False
