"""Un connecteur qui a besoin d'un flux de connexion doit en avoir un d'ATTEIGNABLE.

Deux impasses ont atteint la prod en deux jours, pour la même raison : rien ne vérifiait
qu'un connecteur dont le credential ne s'obtient PAS au formulaire ait un point d'entrée.

- **28/07, salesforce** : `secret_then_oauth` ajouté au jeu fermé d'`auth_method` →
  la valeur tombait dans le `default` du switch du dashboard, la carte ne rendait rien.
  Ni formulaire ni bouton. Personne ne pouvait poser sa Connected App.
- **29/07, salesforce (bis)** : `register_state` déclaré sans `register` — la pose
  fonctionnait, mais la fiche ne disait jamais qu'il restait à consentir. Un credential
  à moitié posé qui a l'air complet.

Les deux sont des OMISSIONS. Un test qui vérifie ce qui existe ne les voit pas ; il faut
un test de TOTALITÉ : pour l'ensemble des connecteurs qui en ont besoin, exiger la
déclaration. C'est ce fichier.

Écrit en RATCHET : `_DETTE` ne peut que rétrécir. Y ajouter un nom demande de nommer le
manque — ce qui rend la dette visible au lieu de silencieuse.
"""
from __future__ import annotations

import pytest

from oto_mcp import providers, status_hints
from oto_mcp.tools import register_all  # noqa: F401 — importe les modules déclarants


# Le chargement des modules de connecteurs est ce qui remplit les registres de seams :
# sans lui, tous les `has_*` répondent False et le test passerait à vide.
def _load_declarations() -> None:
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all as _reg
    _reg(FastMCP("totality-probe"))


@pytest.fixture(scope="module", autouse=True)
def _declarations():
    _load_declarations()


# Connecteurs dont on SAIT que la connexion n'est pas encore déclarée dans les seams.
# Ratchet : cette liste ne doit que rétrécir. Chaque entrée nomme ce qui manque.
_DETTE_DECLARATION: dict[str, str] = {
    # Les 4 flux OAuth fédérés ne déclarent NI état NI étape manquante : ils réexposent
    # leur propre trio `status_for`/`disconnect`/`access_token_for`. Et `access.status_for`
    # est structurellement aveugle à eux (keyed=False, secret_fields=0) — donc même
    # déclaré, un `pending_action` serait aujourd'hui inatteignable. Levée par le barreau
    # « connector_token + 4e boucle de status_for ».
    "google": "status_for maison ; status_for() aveugle aux auth_method=oauth",
    "atlassian": "status_for maison ; status_for() aveugle aux auth_method=oauth",
    "folkmcp": "status_for maison ; status_for() aveugle aux auth_method=oauth",
    # Les 4 connecteurs à session navigateur passent par browser_session : leur état
    # vit dans le coffre (session_set_at), pas dans un hook déclaré.
    "brevoauto": "état porté par browser_session, pas par status_hints",
    "crunchbase": "état porté par browser_session, pas par status_hints",
    "pennylaneged": "état porté par browser_session, pas par status_hints",
    "browser": "état porté par browser_session, pas par status_hints",
}


def _needs_a_flow() -> set[str]:
    """Connecteurs dont le credential NE s'obtient PAS en collant des champs.

    `oauth`/`cookie`/`hosted` = par construction, il faut un geste hors formulaire.
    On ajoute ceux qui DÉCLARENT un état incomplet attendu : dire « ce credential se
    complète ailleurs » implique qu'il existe un ailleurs."""
    out = set()
    for name, c in providers.REGISTRY.items():
        if c.auth_method in ("oauth", "cookie", "hosted"):
            out.add(name)
        elif status_hints.has_state(name):
            out.add(name)
    return out


def test_le_perimetre_nest_pas_vide():
    """Garde-fou du garde-fou : si la dérivation casse, le test doit échouer, pas
    passer à vide (le mode de panne de tout test de totalité)."""
    besoin = _needs_a_flow()
    assert len(besoin) >= 10, f"périmètre suspect : {sorted(besoin)}"
    # `unipile` n'y est plus depuis le split du 2026-08-28 : sa clé se colle au
    # formulaire. Ce qui se connecte hors formulaire, ce sont ses canaux.
    assert {"salesforce", "zoho", "google", "whatsapp",
            "linkedin"} <= besoin


def test_un_etat_declare_implique_une_etape_declaree():
    """LE bug du 29/07, mécanisé. `register_state` répond « ce credential est-il
    complet ? » à la POSE ; `register` répond « que reste-t-il à faire ? » à la
    LECTURE. Déclarer le premier sans le second, c'est une carte qui a l'air
    configurée et qui échoue au premier appel d'outil."""
    manquants = sorted(n for n in providers.REGISTRY
                       if status_hints.has_state(n) and not status_hints.has_hook(n))
    assert not manquants, (
        f"{manquants} déclarent l'état de leur credential mais pas l'étape qui manque — "
        "la fiche restera muette sur ce qu'il reste à faire (cf. salesforce, 29/07).")


def test_tout_connecteur_a_flux_dit_quelque_chose_de_son_etat():
    """TOTALITÉ. Un connecteur dont le credential ne s'obtient pas au formulaire doit
    déclarer AU MOINS un des deux hooks — sinon aucune surface ne peut guider
    l'utilisateur, et c'est le front qui compense en devinant par le nom du connecteur.

    « Au moins un », et pas « l'état » : les deux hooks ne répondent pas à la même
    question et tous les connecteurs n'ont pas les deux. Unipile en est la preuve —
    sa clé est complète dès qu'elle est posée (rien à dire sur l'état du credential),
    ce qui manque est un CANAL connecté, et c'est exactement ce que dit son
    `pending_action`. Exiger `register_state` de lui serait exiger une réponse à une
    question qui ne se pose pas."""
    besoin = _needs_a_flow()
    muets = {n for n in besoin
             if not status_hints.has_state(n) and not status_hints.has_hook(n)}
    non_declares = sorted(muets - set(_DETTE_DECLARATION))
    assert not non_declares, (
        f"{non_declares} : connexion hors formulaire, et AUCUN des deux hooks "
        "`status_hints` — la fiche ne pourra rien dire de leur état. Déclare-en un "
        "dans le module du connecteur, ou inscris-le dans _DETTE_DECLARATION en "
        "nommant ce qui manque.")


def test_la_dette_ne_contient_pas_dentrees_perimees():
    """RATCHET. Une entrée de dette qui n'a plus lieu d'être doit sortir de la liste —
    sinon le plancher ne remonte jamais et le ratchet ne cliquette pas."""
    perimees = sorted(n for n in _DETTE_DECLARATION
                      if status_hints.has_state(n) or status_hints.has_hook(n))
    assert not perimees, (
        f"{perimees} déclarent maintenant leur état : retire-les de _DETTE_DECLARATION.")
    inconnues = sorted(n for n in _DETTE_DECLARATION if n not in providers.REGISTRY)
    assert not inconnues, f"{inconnues} ne sont plus au registre : retire-les."
