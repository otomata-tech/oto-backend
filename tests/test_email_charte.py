"""Le DESSIN d'un email suit la marque du DESTINATAIRE — comme le texte depuis
7d10a798, et par le même paramètre `brand`.

La faute réparée n'était pas une faute de goût : le client d'un partenaire lisait
« sur son produit » écrit dans NOTRE brun, sur un gabarit qui ne ressemblait à rien de ce
qu'il voyait en cliquant. Le mot suivait le tenant, la couleur non — donc il y avait
un endroit du système où les deux pouvaient diverger, et c'est ça qu'on ferme ici.

Ces tests décrivent des PROPRIÉTÉS du HTML servi, jamais un golden : figer un octet
gèlerait la charte et rendrait rouge tout changement de gabarit, y compris juste.
Deux familles :

- **la marque** — la palette servie est celle du destinataire, et jamais celle d'un
  autre produit (le cas qui blesse : le nom d'oto chez un partenaire) ;
- **le client mail** — ce qu'un `<div>` avec un `max-width` ne survivait pas :
  tables, styles en ligne uniquement, `color-scheme` déclaré, ligne d'aperçu.
"""
from __future__ import annotations

import re

import pytest

from oto_mcp import email as E
from oto_mcp import email_brand as B

_URL = "https://exemple.test/x"

# La marque d'un partenaire fictif, posée pour ces tests seuls. Elle a remplacé celle
# d'un client réel (03/09/2026), qui vivait dans le code d'`email_brand` : la palette
# d'un partenaire se DÉCLARE désormais en base, elle n'est plus dans notre code — et
# une propriété générale ne doit de toute façon pas s'éprouver sur un client nommé,
# dans un dépôt public. ⚠️ Ses sept teintes sont TOUTES distinctes de celles d'oto :
# c'est ce qui rend l'assertion par l'absence, plus bas, capable de rougir.
_PARTENAIRE = B.Marque(
    slug="pilote", nom="Pilote", site="pilote.test",
    fond="#0b0d12", surface="#141821", encre="#e8ecf4", discret="#8b93a7",
    filet="#232a38", bouton_fond="#e8ecf4", bouton_encre="#0b0d12",
)


@pytest.fixture(autouse=True)
def _marque_de_test(monkeypatch):
    """Déclare `_PARTENAIRE` le temps du test, par le seul chemin qu'a une marque de
    partenaire : sa déclaration (`_declaree`, lue du registre de tenants)."""
    declaree = B._declaree
    monkeypatch.setattr(B, "_declaree",
                        lambda slug: _PARTENAIRE if slug.lower() == "pilote" else declaree(slug))


@pytest.fixture
def envoye(monkeypatch):
    """Capture le `html` du dernier `_send` sans jamais sortir sur le réseau."""
    boite: dict = {}
    monkeypatch.setattr(E, "_send",
                        lambda to, subject, html, **k: boite.update(
                            to=to, subject=subject, html=html) or True)
    return boite


def _tous_les_envois(brand: str, locale: str | None = None) -> list:
    """Un appel par gabarit transactionnel, tous paramètres nominaux — la liste que
    parcourt chaque propriété ci-dessous. Elle est écrite ici plutôt que dans un
    `parametrize` de haut niveau pour qu'un gabarit ajouté sans être ajouté ICI se
    voie : `test_les_cinq_gabarits_sont_couverts` compte les fonctions du module."""
    return [
        lambda: E.send_invite_email("q@e.test", "Acme", _URL, "alex",
                                    brand=brand, locale=locale),
        lambda: E.send_resource_shared_email("q@e.test", type_label="projet", name="Plan",
                                             permission="read", app_url=_URL,
                                             sharer="alex", brand=brand, locale=locale),
        lambda: E.send_resource_transferred_email("q@e.test", type_label="projet",
                                                  name="Plan", app_url=_URL, sharer="alex",
                                                  brand=brand, locale=locale),
        lambda: E.send_signal_digest_email(
            "q@e.test", items=[{"status": "resolved", "target": "oto_call",
                                "created_at": "2026-08-30 10:00:00", "body": "ça coince",
                                "resolution": "corrigé"}],
            brand=brand, locale=locale),
        lambda: E.send_composed_email("q@e.test", "objet", "bonjour,\n\nvoilà.",
                                      cta_text="ouvrir", cta_url=_URL, brand=brand),
    ]


# --- la marque --------------------------------------------------------------

def test_un_email_de_partenaire_ne_porte_aucune_couleur_d_oto(envoye):
    """La propriété qui compte : chez le destinataire d'un partenaire, RIEN du dessin d'oto ne
    doit ressortir. Assertion par l'absence, parce que c'est l'absence qui manquait."""
    oto, partenaire = B.marque_instance(), _PARTENAIRE
    couleurs_oto = {oto.fond, oto.surface, oto.encre, oto.discret, oto.filet,
                    oto.bouton_fond, oto.bouton_encre}
    couleurs_part = {partenaire.fond, partenaire.surface, partenaire.encre,
                     partenaire.discret, partenaire.filet,
                     partenaire.bouton_fond, partenaire.bouton_encre}
    # Sans ça, l'assertion par l'absence ci-dessous serait plus faible qu'elle n'en a
    # l'air : une couleur commune aux deux palettes la rendrait vraie pour rien.
    assert not (couleurs_oto & couleurs_part)
    for envoi in _tous_les_envois("pilote"):
        envoi()
        html = envoye["html"]
        assert partenaire.encre in html and partenaire.filet in html
        for couleur in couleurs_oto:
            assert couleur not in html, f"{couleur} (oto) dans un email de partenaire"


def test_la_marque_ecrite_est_le_nom_du_produit_pas_le_slug(envoye):
    for envoi in _tous_les_envois("pilote"):
        envoi()
        assert "Pilote" in envoye["html"]
    for envoi in _tous_les_envois("oto"):
        envoi()
        assert "oto" in envoye["html"]


def test_un_slug_inconnu_prend_le_gabarit_neutre_et_SON_nom(envoye):
    """`front_brand` est une colonne, pas une énum : un tenant déclaré demain y écrira
    son slug avant de déclarer sa marque. Le repli n'est PAS oto — écrire le nom
    d'oto chez un partenaire est exactement le faux qu'on répare."""
    for envoi in _tous_les_envois("partenaire"):
        envoi()
        html = envoye["html"]
        assert "partenaire" in html
        assert "oto.cx" not in html
        assert B.marque_instance().encre not in html


def test_sans_marque_c_est_oto_le_defaut():
    """`orgs.front_brand IS NULL` veut dire « la plateforme », pas « inconnu »."""
    assert B.marque(None) == B.marque_instance()
    assert B.marque("") == B.marque_instance()
    assert B.marque("PILOTE") is _PARTENAIRE


def test_une_marque_sans_site_ne_signe_pas_un_domaine_invente():
    """« nom · site » suppose un site. Sans lui, la ligne disparaît — on n'invente pas
    le domaine d'un partenaire, et un pied qui n'aurait plus rien ne se rend pas."""
    assert "·" not in B._pied(B.marque("partenaire"), "une raison")
    assert "·" in B._pied(_PARTENAIRE, "une raison")
    assert B._pied(B.marque("partenaire"), "") == ""
    assert B._pied(_PARTENAIRE, None) == ""


# --- ce qu'un client mail sait rendre ---------------------------------------

@pytest.mark.parametrize("brand", ["oto", "pilote", "inconnue"])
def test_chaque_email_est_un_document_complet_en_tables(envoye, brand):
    for envoi in _tous_les_envois(brand):
        envoi()
        html = envoye["html"]
        assert html.startswith("<!DOCTYPE html>") and html.endswith("</html>")
        assert '<meta name="color-scheme" content="light">' in html, (
            "sans elle, Outlook mobile et Apple Mail repeignent le fond en sombre "
            "et laissent le texte en place")
        # La mise en page est en TABLES : Outlook (moteur Word) ne met en page que ça,
        # et ignorait le `max-width` du `<div>` d'avant.
        assert html.count("<table") >= 2 and 'role="presentation"' in html
        assert f'width="{B.LARGEUR}"' in html


@pytest.mark.parametrize("brand", ["oto", "pilote"])
def test_aucun_style_qui_ne_survit_pas_a_un_client_mail(envoye, brand):
    """Styles EN LIGNE uniquement. Un `<style>` est retiré par plusieurs webmails, une
    variable CSS n'est jamais résolue, `flex`/`grid` ne sont pas implémentés par le
    moteur Word — chacun rend une mise en page qui s'effondre sans erreur."""
    interdits = ("<style", "var(--", "display:flex", "display:grid", "@media")
    for envoi in _tous_les_envois(brand):
        envoi()
        for motif in interdits:
            assert motif not in envoye["html"], motif


@pytest.mark.parametrize("brand", ["oto", "pilote"])
def test_chaque_email_declare_sa_ligne_d_apercu(envoye, brand):
    """La ligne d'aperçu est la deuxième chose qu'on lit d'un email — avant de
    l'ouvrir. Sans bloc dédié, la boîte y mettait le premier texte venu, c'est-à-dire
    « ou collez ce lien : https://… »."""
    for envoi in _tous_les_envois(brand):
        envoi()
        html = envoye["html"]
        debut = html.index('<div style="display:none')
        apercu = html[debut:html.index("</div>", debut)]
        assert "mso-hide:all" in apercu
        texte = re.sub(r"<[^>]+>|&#8203;|&#847;", "", apercu).strip()
        assert texte and "collez ce lien" not in texte
        assert texte not in ("", B.marque(brand).nom)


def test_la_langue_du_document_suit_celle_du_destinataire(envoye):
    E.send_invite_email("q@e.test", "Acme", _URL, brand="pilote", locale="en")
    assert '<html lang="en">' in envoye["html"]
    E.send_invite_email("q@e.test", "Acme", _URL, brand="pilote", locale=None)
    assert '<html lang="fr">' in envoye["html"]


def test_le_bouton_est_une_table_et_disparait_sans_url():
    """Outlook n'applique ni `padding` ni `border-radius` à un `<a>` en `inline-block` :
    le fond et l'arrondi vivent sur le `<td>`. Et sans URL, RIEN — le produit d'un
    partenaire n'a pas forcément la vue visée, et un lien mort ne se diagnostique pas,
    il se subit (`links.py`)."""
    m = _PARTENAIRE
    bouton = B.bouton(m, _URL, "ouvrir")
    assert bouton.count("<a ") == 1 and f"background:{m.bouton_fond}" in bouton
    assert re.search(r"<td[^>]*border-radius:999px", bouton)
    assert B.bouton(m, None, "ouvrir") == "" and B.bouton(m, "", "ouvrir") == ""


def test_le_bouton_n_offre_aucun_guillemet_nu_a_une_url_d_agent():
    """`cta_url` vient d'un agent, et l'adresse est rendue DEUX fois : dans `href` et
    en clair dessous. Les deux sont échappées en attribut — une donnée d'entrée ne
    ressort jamais avec un guillemet nu, où qu'elle atterrisse."""
    bouton = B.bouton(B.marque_instance(), 'https://e.test/?a="b" onmouseover="y', "ouvrir")
    assert 'onmouseover="' not in bouton and bouton.count("<a ") == 1
    assert bouton.count("&quot;") == 6, "3 guillemets de l'URL × 2 rendus"


def test_les_cinq_gabarits_sont_couverts():
    """Un gabarit ajouté sans être ajouté à `_tous_les_envois` échapperait à TOUTES les
    propriétés ci-dessus, en silence. Le compte est donc vérifié, pas supposé. Cinq
    depuis le retrait des deux e-mails de proposition (oto#191)."""
    assert len(_tous_les_envois("oto")) == 5


# --- l'email libre d'un agent part à la marque de CELUI QUI ENVOIE --------------

def test_email_send_signe_la_marque_de_l_expediteur_pas_la_notre(monkeypatch):
    """Le client d'un partenaire dont l'agent écrit à un prospect signait « oto, par otomata
    · oto.cx » — le pied d'un produit que son destinataire n'a jamais vu, sous son
    propre domaine d'envoi. La marque vient du tenant de l'expéditeur, et elle est
    dérivée du `sub` que la route a authentifié : un appel d'auth de plus lèverait
    AVANT les refus de paramètre et inverserait l'ordre des erreurs de l'outil."""
    import asyncio

    from fastmcp import FastMCP
    from oto_mcp.tools import email as T

    route = {"org_id": None, "connector": None, "from_email": None, "from_name": None,
             "transport": "mailer", "reply_to": None, "quiet_hours": None}
    monkeypatch.setattr(T, "_resolve_route", lambda from_email: ("pilote:u1", route))
    monkeypatch.setattr(T.config, "front_for",
                        lambda sub: ("https://app.pilote.test", "pilote")
                        if sub == "pilote:u1" else (None, None))
    m = FastMCP("t")
    T.register(m)
    outil = asyncio.run(m.get_tool("email_send"))

    out = outil.fn(ctx=None, to="q@e.test", subject="objet", body="bonjour,",
                   dry_run=True)
    assert "Pilote · pilote.test" in out["html"]
    assert "oto.cx" not in out["html"]
    assert B.marque_instance().encre not in out["html"]


# --- La charte DÉCLARÉE du tenant primaire (`OTO_BRAND_CHARTE`, 15/11/2026) -----------

_CHARTE = ('{"fond": "#f9f9fb", "surface": "#ffffff", "encre": "#1c2024", '
           '"discret": "#60646c", "filet": "#e8e8ec", "bouton_fond": "#1c2024", '
           '"bouton_encre": "#ffffff", "bouton_rayon": 6, '
           '"logo": {"url": "https://media.exemple.test/logo.png", "largeur": 104, '
           '"hauteur": 26}}')


def test_une_charte_declaree_habille_le_primaire(monkeypatch):
    """Les teintes du code sont celles de toute instance qui ne dit rien ; une
    instance dont le produit ne leur ressemble pas les DÉCLARE, comme son nom."""
    monkeypatch.setenv("OTO_BRAND_CHARTE", _CHARTE)
    m = B.marque_instance()
    assert (m.fond, m.encre, m.bouton_fond, m.bouton_rayon) == (
        "#f9f9fb", "#1c2024", "#1c2024", 6)
    html = B.page(m, "<p>corps</p>", preheader="aperçu", mention="")
    assert "background:#f9f9fb" in html and "#faf6ec" not in html
    assert re.search(r'<img src="https://media\.exemple\.test/logo\.png" width="104" '
                     r'height="26" alt="oto"', html), "le logo, avec le nom en alt"
    assert re.search(r"<td[^>]*border-radius:6px", B.bouton(m, _URL, "ouvrir"))


def test_le_logo_remplace_le_nom_en_tete_et_son_absence_le_garde(monkeypatch):
    monkeypatch.setenv("OTO_BRAND_CHARTE", _CHARTE)
    avec = B.page(B.marque_instance(), "<p>corps</p>", preheader="p", mention=None)
    monkeypatch.delenv("OTO_BRAND_CHARTE")
    sans = B.page(B.marque_instance(), "<p>corps</p>", preheader="p", mention=None)
    assert "<img" not in sans and "letter-spacing:-0.01em" in sans
    assert "letter-spacing:-0.01em" not in avec, "le nom texte ne double pas le logo"


@pytest.mark.parametrize("valeur", [
    "pas du json",
    "[]",
    '{"fond": "#f9f9fb"}',
    _CHARTE.replace('"#1c2024", "discret"', '"rouge", "discret"'),
    _CHARTE.replace('"bouton_rayon": 6', '"bouton_rayon": "6"'),
    _CHARTE.replace("https://media", "http://media"),
    _CHARTE.replace("logo.png", 'logo.png\\" onerror=\\"x'),
    _CHARTE.replace('"largeur": 104', '"largeur": 0'),
])
def test_une_charte_invalide_ou_partielle_refuse_le_demarrage(monkeypatch, valeur):
    """Jamais servie à moitié, jamais remplacée en silence par celle du code : posée
    mais illisible, elle refuse le démarrage et lève à l'envoi."""
    monkeypatch.setenv("OTO_BRAND_CHARTE", valeur)
    with pytest.raises(B.CharteInvalide, match="OTO_BRAND_CHARTE"):
        B.verifier_reglages()
    with pytest.raises(B.CharteInvalide):
        B.marque_instance()


def test_sans_charte_declaree_le_rendu_est_celui_du_code(monkeypatch):
    """Une instance qui ne pose rien : teintes du code, bouton en pilule, nom en tête."""
    monkeypatch.delenv("OTO_BRAND_CHARTE", raising=False)
    B.verifier_reglages()
    m = B.marque_instance()
    assert (m.fond, m.bouton_rayon, m.logo) == ("#faf6ec", 999, ())
    assert "<img" not in B.page(m, "<p>x</p>", preheader="p", mention="")


def test_une_marque_de_partenaire_ignore_la_charte_de_l_instance(monkeypatch):
    """La charte déclarée est celle du PRIMAIRE : un partenaire garde la sienne."""
    monkeypatch.setenv("OTO_BRAND_CHARTE", _CHARTE)
    m = B.marque("pilote")
    assert m.fond == _PARTENAIRE.fond and m.logo == () and m.bouton_rayon == 999
