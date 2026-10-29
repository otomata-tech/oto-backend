"""La déclaration « en lecture » (`tools/lecture.LECTURE`) ne ment pas.

Une recette n'appelle QUE des outils déclarés en lecture : une déclaration posée par
erreur sur un outil qui écrit chez le tiers ouvrirait à une recette l'envoi d'un mail ou
la création d'un contact, sans modèle pour s'arrêter. Cette garde lit le catalogue
RÉELLEMENT monté et refuse toute déclaration portée par un outil qui a la forme d'un
écrivain : un verbe d'écriture dans son nom, une op d'écriture dans son `op`, ou un
connecteur à modèle.
"""
from __future__ import annotations

import re

import pytest

from _mcp_app import static_mcp
from oto_mcp.recipes import contrat
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools.lecture import en_lecture

#: Les verbes d'un outil qui change quelque chose chez son fournisseur.
_ECRIT = re.compile(r"(^|_)(send|post|push|compose|create|update|delete|write|set|"
                    r"add|remove|upload|fire|reply|invite|publish|archive|move|"
                    r"cancel|pause|resume|connect|disconnect|reveal)(_|$)")


#: Noms où le mot d'écriture est un NOM, pas un verbe : `linkedin_unipile_post` désigne
#: LA publication (publier vit dans `linkedin_unipile_act` depuis le 21/10/2026 ; l'outil
#: est `LECTURE`, et ses ops restent jugées une à une ci-dessous). Liste fermée.
_NOM_PAS_VERBE = frozenset({"linkedin_unipile_post"})


async def _outils():
    return await static_mcp().list_tools(run_middleware=False)


def _ops(tool) -> list:
    props = (getattr(tool, "parameters", None) or {}).get("properties") or {}
    op = props.get("op") or {}
    enum = op.get("enum") or []
    for alt in op.get("anyOf") or []:
        enum += alt.get("enum") or []
    return [str(o) for o in enum]


@pytest.mark.asyncio
async def test_un_outil_declare_en_lecture_n_a_pas_la_forme_d_un_ecrivain():
    fautifs = []
    for t in await _outils():
        if not en_lecture(t):
            continue
        if namespace_of(t.name) in contrat.NAMESPACES_A_MODELE:
            fautifs.append(f"{t.name} : connecteur à modèle")
        if _ECRIT.search(t.name) and t.name not in _NOM_PAS_VERBE:
            fautifs.append(f"{t.name} : verbe d'écriture dans le nom")
        fautifs += [f"{t.name} : op d'écriture `{o}`" for o in _ops(t) if _ECRIT.search(o)]
    assert not fautifs, fautifs


@pytest.mark.asyncio
async def test_les_outils_des_recettes_documentees_sont_declares_en_lecture():
    """L'exemple de `docs/recettes.md` doit pouvoir tourner."""
    servis = {t.name: t for t in await _outils()}
    for nom in ("linkedin_aiark_search", "apollo_search_people", "serper_search",
                "fr_search", "fr_stock_search"):
        assert en_lecture(servis[nom]), nom


@pytest.mark.asyncio
async def test_les_lecteurs_de_messagerie_sont_declares_en_lecture():
    """Depuis le 21/10/2026, un lecteur de messagerie n'envoie plus rien (paire `<ns>` /
    `<ns>_act`) : il se déclare `LECTURE`, et une recette « pull » peut le lire."""
    servis = {t.name: t for t in await _outils()}
    for nom in ("whatsapp", "telegram", "instagram", "linkedin_unipile", "teams", "chat",
                # et, depuis le lot des envois, les lecteurs LinkedIn qui envoyaient
                "linkedin_unipile_post", "linkedin_unipile_network",
                "linkedin_unipile_profile"):
        assert en_lecture(servis[nom]), nom


@pytest.mark.asyncio
async def test_les_lectures_stripe_et_http_get_sont_declarees():
    """Un rapport financier (Stripe + une banque branchée en API personnalisée) se
    synchronise par recette : ces outils ne font que lire chez leur fournisseur.
    `stripe_invoice` et `stripe_customer` restent non déclarés (brouillon, création)."""
    servis = {t.name: t for t in await _outils()}
    for nom in ("http_get", "stripe_search", "stripe_subscription", "stripe_payment",
                "stripe_balance", "stripe_event"):
        assert en_lecture(servis[nom]), nom
    for nom in ("stripe_invoice", "stripe_customer", "stripe_catalog", "stripe_checkout"):
        assert not en_lecture(servis[nom]), nom


@pytest.mark.asyncio
async def test_les_lecteurs_lemlist_sont_declares_en_lecture():
    """Une recette compare les leads d'un tableau aux campagnes avant de pousser :
    lister, lire une campagne, ses leads, ses activités, un résultat d'enrichissement."""
    servis = {t.name: t for t in await _outils()}
    for nom in ("lemlist_status", "lemlist_list_campaigns", "lemlist_get_campaign",
                "lemlist_get_campaign_stats", "lemlist_get_activities", "lemlist_get_leads",
                "lemlist_enrich_result"):
        assert en_lecture(servis[nom]), nom
    for nom in ("lemlist_campaign", "lemlist_lead", "lemlist_create_lead",
                "lemlist_enrich", "lemlist_enrich_bulk", "lemlist_launch_lead"):
        assert not en_lecture(servis[nom]), nom


@pytest.mark.asyncio
async def test_les_lectures_de_prospection_sont_declarees_en_lecture():
    """Ce qu'un process de prospection lit dans une recette, qui garde côté serveur des
    réponses de centaines de milliers de caractères : la base partagée lemlist (en lecture
    depuis que ses personas sont passés à `lemlist_act`, 23/10/2026), TheirStack, les
    technologies d'un site, une page web publique. `lemlist_enrich`, `lemlist_enrich_bulk`
    (une `webhook_url` pousse le résultat chez un tiers) et `apify_run_sync` (un acteur
    arbitraire) ne le sont pas."""
    servis = {t.name: t for t in await _outils()}
    for nom in ("lemlist_database", "theirstack_jobs_search", "theirstack_companies_search",
                "dataforseo_technologies", "web_read"):
        assert en_lecture(servis[nom]), nom
    for nom in ("lemlist_enrich", "lemlist_enrich_bulk", "apify_run_sync"):
        if nom in servis:
            assert not en_lecture(servis[nom]), nom


@pytest.mark.asyncio
async def test_aucun_outil_de_plateforme_ni_ecrivain_connu_n_est_declare():
    servis = {t.name: t for t in await _outils()}
    for nom in ("email_send", "slack_post_message", "gmail", "gmail_act", "outlook",
                "outlook_act", "http_post",
                "lemlist_push_rows", "hubspot_push_rows", "routine_fire", "oto_call",
                "data_write", "whatsapp_act", "telegram_act", "instagram_act",
                "linkedin_unipile_act", "chat_act", "teams_act", "apollo_act",
                "lightfield_act", "luma_act", "pennylane_act", "signwell_act",
                "productlane_act", "ahrefs_act", "lemlist_act", "github_act"):
        if nom in servis:
            assert not en_lecture(servis[nom]), nom
