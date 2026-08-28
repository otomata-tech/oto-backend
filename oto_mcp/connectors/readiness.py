"""Aptitude EFFECTIVE d'un connecteur : « est-ce que ça MARCHE ? », couche par couche.

Seam UNIQUE de diagnostic, né du signal **#476** (org 196, 16/08/2026). La carte
connecteur y rendait `state:"active"` + `recommended:true`, la sonde
`oto_instance(op="verify")` répondait `ok:true` — et rien ne pouvait partir : aucun
canal hébergé n'était lié, `free_tier.daily_quota` valait 0. **Trois lectures vertes,
capacité absente.** L'opérateur a lu « active » comme « connecté », ce qui est la
lecture naturelle, et a cherché cinq jours au mauvais endroit.

La cause n'est pas un calcul faux : c'est que chaque surface publiait UNE couche en
laissant croire qu'elle répondait pour les trois. `state` ne dit QUE « le membre l'a
installé dans sa boîte à outils » ; `verify` ne teste QUE la clé résolue ; `identities`
ne compte QUE les comptes liés. `docs/connector-model.md` nomme les trois couches
depuis longtemps — il manquait l'endroit qui les lit ENSEMBLE, et le fait dire.

Deux surfaces le consomment, et c'est la raison pour laquelle il vit ici plutôt que
dans l'une d'elles : la carte connecteur (`capabilities/connectors/selection`, verdict
`ready`) et la liste d'identités (`capabilities/connectors/identities`, le POURQUOI
d'une liste vide — signal **#504**). Une seconde formulation du même verdict rouvrirait
exactement la divergence qu'on répare — c'est déjà arrivé entre `option_ok` et
`status_for.subscribed` (corrigé le 07/07/2026, cf. `access.option_open`).

⚠️ **Le diagnostic n'inclut PAS l'état de sélection** (`not_selected` / `paused`), et
c'est délibéré : un connecteur non sélectionné reste appelable par `oto_call` (dispatch
universel, ADR 0036). Le signal **#577** l'a prouvé sur la prod — les sept outils
« invisibles » ont tous répondu du premier coup contre le credential d'org. La sélection
gouverne la VISIBILITÉ des outils, pas l'aptitude. Mélanger les deux recréerait la
confusion de #476 sous un autre nom.

⚠️ **Le coût est réel, et c'est lui qui dicte l'usage.** Mesuré sur la prod le
28/08/2026 (compte réel, catalogue de 90 connecteurs, org 196) : `credential_mode_for`
coûte **1 993 ms pour les 90** — une marche de cascade par connecteur, ≈22 ms l'unité —
et le serveur est MONO-LOOP. Un seul connecteur : **~244 ms**. D'où la règle des
appelants : on diagnostique sur DEMANDE (lecture ciblée), et on DIT qu'on n'a pas
calculé le reste plutôt que de laisser une absence passer pour un blanc-seing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import status_hints

# Jetons machine stables (l'ordre est celui de l'évaluation, cf. `diagnose`).
PAID_OPTION_OFF = "paid_option_off"      # couche 3 — l'option n'est pas levée
NO_CREDENTIAL = "no_credential"          # couche 2 — aucune clé ne résout
OVER_QUOTA = "over_quota"                # couche 2 — la clé résout, la journée est finie
PENDING_STEP = "pending_step"            # couches OK, il reste un geste (lier un canal…)


@dataclass(frozen=True)
class Diagnosis:
    """La PREMIÈRE couche qui manque, et le geste qui la lève.

    `reason` est un jeton machine stable ; `next_step` est la phrase que TOUTES les
    surfaces rendent telle quelle (agent, front, message d'erreur) — jamais reformulée
    en aval, sinon deux surfaces racontent deux versions du même fait."""
    reason: str
    next_step: str


def _account_url(sub: Optional[str]) -> str:
    """Où l'on POSE une clé, pour CE compte. `dashboard_url_for` et pas une adresse
    en dur : org 196 (celle de #476) est un tenant tiers, et lui servir notre marque
    est précisément l'incident du 13/08/2026 — un client d'un partenaire renvoyé vers
    un produit qui n'est pas le sien."""
    from .. import config
    return f"{config.dashboard_url_for(sub)}/account"


def _connections_url(sub: Optional[str]) -> str:
    """Où l'on CONNECTE un compte hébergé (hosted-auth) — même règle de tenant."""
    from .. import config
    return f"{config.dashboard_url_for(sub)}/console/connections"


def diagnose(sub: str, connector: str, *, org, group) -> Optional[Diagnosis]:
    """`None` = les trois couches sont levées ET rien n'est en attente : un appel
    partirait. Sinon la PREMIÈRE couche qui manque, dans l'ordre de
    `docs/connector-model.md` — c'est cet ordre qui décide ce que le refus NOMME
    quand plusieurs manquent à la fois, et il va du plus englobant au plus fin.

    ⚠️ `org` / `group` sont EXIGÉS explicites (pas de défaut) : ce diagnostic se
    calcule aussi POUR UN TIERS, et `current_org` est scopé sur l'ACTEUR courant.
    Le laisser dériver ferait lire l'org du requérant dans l'état de quelqu'un
    d'autre — le bug du 24/06/2026 (`has_option(cible)` lisant l'org du requérant),
    et la raison d'être de `access._UNSET`."""
    from .. import access

    # Couche 3 — l'option payante. En tête parce qu'elle englobe : option fermée ⟹
    # ni clé plateforme utilisable ni connexion possible. `paid_option_for` d'abord,
    # pour ne parler d'option qu'aux connecteurs qui en ont une (la plupart n'ont
    # que les couches 1+2, et `option_open` leur répond `True` par construction).
    opt = access.paid_option_for(connector)
    if opt is not None and not access.option_open(sub, connector, org=org):
        return Diagnosis(PAID_OPTION_OFF, (
            f"L'option `{opt}` n'est pas ouverte pour toi ici : il faut l'abonnement "
            f"d'org qui l'inclut, un accès accordé par un admin, ou ta PROPRE clé "
            f"`{connector}` — une clé à toi lève l'option par construction (il n'y a "
            f"plus de siège plateforme à protéger). Pose-la sur {_account_url(sub)}."))

    # Couche 2 — la clé. `credential_mode_for` est le MIROIR de la cascade réelle
    # (`resolve_credential`) : le verdict de la carte ne peut donc pas diverger de ce
    # que l'appel ferait. `forbidden` = rien ne résout ; ce n'est PAS un refus RBAC.
    # ⚠️ La clé peut appartenir à un AUTRE connecteur (`Connector.credential_of` —
    # les canaux de messagerie hébergée empruntent celle du compte fournisseur). Le
    # message doit alors nommer la carte où elle se POSE, pas celle qu'on diagnostique :
    # envoyer quelqu'un « section Whatsapp » de sa page compte est un cul-de-sac, il
    # n'y a pas de champ. La couche 1 (au-dessus) reste, elle, sur le connecteur
    # DEMANDÉ — c'est bien son activation et son ACL à lui qui le gouvernent.
    from .. import providers
    porteur = providers.credential_provider(connector)
    mode = access.credential_mode_for(sub, connector, org=org, group=group)
    if mode == "forbidden":
        return Diagnosis(NO_CREDENTIAL, (
            f"Aucune clé `{porteur}` ne résout pour toi dans cette org : pose la "
            f"tienne sur {_account_url(sub)} (section {porteur.capitalize()}), ou "
            f"demande à un admin de te prêter la clé plateforme."))
    if mode == OVER_QUOTA:
        # Distinct de `no_credential` À DESSEIN : la clé va très bien, c'est la
        # journée qui est finie. Les confondre envoie reconfigurer un credential sain.
        return Diagnosis(OVER_QUOTA, (
            f"Quota de la clé plateforme `{porteur}` épuisé pour aujourd'hui — la "
            f"clé résout, elle est à bout de course. Pose ta propre clé sur "
            f"{_account_url(sub)} pour continuer sans limite, ou reprends demain."))

    # Le geste qui reste. Seam générique `status_hints` : la spécificité (unipile =
    # « lier un canal », zoho/salesforce = « autoriser oto ») vit dans le module du
    # connecteur, jamais ici. On RELAIE son libellé, on ne le reformule pas.
    step = status_hints.pending_action(connector, sub, org, group, {"mode": mode})
    if step:
        return Diagnosis(PENDING_STEP, step)
    return None


def no_identity_step(sub: Optional[str], connector: str, noun: str = "compte") -> str:
    """Le geste par défaut quand la liste d'identités est vide sans qu'aucune couche
    ne manque (#504) : le connecteur ne déclare pas de `status_hints`, mais le silence
    reste le défaut à réparer — on nomme l'état plutôt que de rendre `[]` tout court.
    `noun` = le MOT du fournisseur (`access.account_noun`) : parler de « compte » pour
    un espace Slack oblige le lecteur à traduire."""
    return (f"Aucun {noun} n'est encore lié à `{connector}` : la clé résout, il reste à "
            f"en connecter un depuis {_connections_url(sub)}.")
