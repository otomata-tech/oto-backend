"""La garde des travaux qui tournent sur l'ABONNEMENT d'une personne (OTO-130).

Un travail de famille `claude_subscription` ne consomme NI la clé de son org NI
celle de la plateforme : il s'exécute dans le sandbox de son demandeur, sur le
programme officiel du fournisseur, où cette personne s'est connectée elle-même.

Trois refus nommés, tous à l'écriture ou à la réservation, jamais silencieux :

1. `subscription_personal_only` — un abonnement ne sert que les agents de son
   propriétaire. Une flotte, ou l'agent d'un collègue, ferait payer le forfait
   d'une personne pour le travail d'une autre.
2. `subscription_not_connected` — la personne n'a pas (ou plus) de session
   ouverte dans son sandbox.
3. Au claim, un travail dont le porteur n'est plus connecté est ARRÊTÉ avec sa
   raison, comme un travail sans clé déposée : le remettre en file le ferait
   reprendre indéfiniment par le worker suivant.

**Le POOL d'org** (25/09/2026). Une org peut passer une famille en mode `pool`
(`org_subscription_pool`) : ses travaux tournent alors sur l'abonnement d'un membre
qui l'a PRÊTÉ à cette org (opt-in, par org), choisi à la réservation — le moins
récemment servi, libre, servable. Le demandeur n'a plus besoin d'une connexion à
lui (le chemin doit toujours lui être ouvert : `ouvert`), une flotte y passe, et le forfait rapporté est
celui du PRÊTEUR, sous SON seuil (min du plafond de l'org du travail et du sien).
Le mode personnel reste le défaut, à l'octet près.

⚠️ **Ce module ne lit jamais de session.** Il lit un ÉTAT (`user_model_
subscriptions.statut`), écrit par la sonde du sandbox. La session elle-même
ne traverse pas le backend — c'est la condition qui rend ce chemin licite.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import access, interrupteurs, runner_models
from ..db import org_subscription_limits, org_subscription_pool
from ..db import runner_jobs as db_runner_jobs
from ..db import user_subscriptions
from ._types import AuthzDenied

logger = logging.getLogger(__name__)

#: Le PLAFOND de consommation par défaut, en % de l'usage TOTAL du compte du
#: fournisseur (fenêtres cinq heures et sept jours, usage perso compris) — celui d'une
#: org qui n'a rien réglé. Au-delà, la personne est mise en attente AVANT qu'un
#: travail ne soit refusé : le fournisseur annonce l'usage à chaque exécution
#: (`rate_limit_event`, mesuré le 21/09/2026), et attendre le refus, c'est brûler une
#: tentative pour apprendre ce qu'on savait déjà — et ne rien laisser à la personne
#: pour son propre usage (décision du 25/09/2026). Le seuil effectif : `seuil`.
DEFAUT_LIMITE_PCT = 80

#: L'OPTION qui ouvre ce chemin à une personne (`oto_admin_set_option`, entité
#: `user`). Ouvert nominativement, jamais par défaut : un abonnement personnel
#: sur une plateforme partagée reste un usage que le fournisseur n'a pas
#: confirmé par écrit (24/09/2026) — on l'ouvre à des personnes nommées.
OPTION = "claude_subscription"

#: Ouvre le chemin à TOUT compte de l'instance, option ou pas (`1`/`true`/`on`).
#: Décision de l'instance, pas d'une personne : absente = nommément, comme avant.
ENV_OUVERT_A_TOUS = "OTO_ABONNEMENT_OUVERT_A_TOUS"

#: La borne d'une échéance de plafond rapportée par un worker. Les fenêtres du
#: fournisseur durent cinq heures ou sept jours : au-delà, le rapport est faux
#: (une unité, un worker fautif), et le croire suspendrait la personne sans fin.
ECHEANCE_MAX = timedelta(days=8)

#: Les familles servies par un abonnement personnel. Dérivée du catalogue, jamais
#: recopiée : le jour où Codex suit le même chemin, il suffit de l'y déclarer.
FAMILLES = runner_models.FAMILLES_PERSONNELLES


def est_abonnement(famille: Optional[str]) -> bool:
    return bool(famille) and famille in FAMILLES


def famille_du_travail(job: dict) -> Optional[str]:
    """La famille portée par la charge d'un travail — la même lecture que la
    réservation (`claim_next_job` route sur `payload->>'model_family'`)."""
    return (job.get("payload") or {}).get("model_family")


_PAS_CONNECTE = (
    "ce travail demande un modèle `{famille}`, qui tourne sur l'abonnement de la "
    "personne qui l'a demandé — et cette personne n'a pas de connexion ouverte "
    "({statut}). Travail non exécuté. Ouvre la connexion (Réglages › Fournisseurs de "
    "modèles), puis rallume l'agent.")


def raison_du_refus(famille: str, statut: Optional[str]) -> str:
    return _PAS_CONNECTE.format(famille=famille, statut=statut or "jamais connectée")


def reparable(statut: Optional[str], sandbox: Optional[str]) -> bool:
    """Cet état se répare-t-il SANS toucher au travail ? Oui dès que la personne a
    un sandbox et n'a qu'à s'y reconnecter. Non quand il n'y a rien à attendre :
    ni sandbox, ni ligne — personne ne reviendra « reconnecter » ce qui n'a
    jamais existé, et un travail en attente éternelle est un silence."""
    return bool(sandbox) and statut in (user_subscriptions.A_RECONNECTER,
                                    user_subscriptions.DECONNECTE)


def raison_de_l_attente(famille: str, statut: Optional[str], *, pool: bool = False,
                        reprise: Optional[datetime] = None) -> str:
    """Pourquoi un travail rendu à la file attend. ⚠️ Un forfait AU PLAFOND n'a rien à
    reconnecter : le texte dit jusqu'à quand il attend (`reprise`), pas un geste que
    personne n'a à faire."""
    if statut == user_subscriptions.PLAFOND:
        jusqu = f" jusqu'au {reprise:%d/%m/%Y %H:%M} UTC" if reprise else ""
        if pool:
            return (f"en attente : ce travail tourne sur le pool `{famille}` de "
                    f"l'organisation, et l'abonnement prêté qui devait le servir est au "
                    f"plafond{jusqu}. Il repartira sur le prochain abonnement prêté "
                    "disponible.")
        return (f"en attente : ce travail tourne sur l'abonnement `{famille}` de son "
                f"demandeur, au plafond{jusqu}. Il repartira tout seul à la reprise.")
    if pool:
        return (f"en attente : ce travail tourne sur le pool `{famille}` de "
                f"l'organisation, et l'abonnement prêté qui devait le servir ne peut "
                f"plus ({statut or 'retiré'}). Il repartira sur le prochain abonnement "
                "prêté disponible.")
    return (f"en attente : ce travail tourne sur l'abonnement `{famille}` de son "
            f"demandeur, qui doit s'y reconnecter ({statut}). Il repartira tout seul "
            "à la reconnexion (Réglages › Fournisseurs de modèles).")


def en_pool(org_id: Optional[int], famille: Optional[str]) -> bool:
    """L'org sert-elle cette famille par son POOL (abonnements prêtés par ses
    membres) plutôt que par l'abonnement de chaque demandeur ? Réglé par l'org
    (`org.model_subscriptions`), `personnel` par défaut."""
    return est_abonnement(famille) and org_subscription_pool.en_pool(org_id, famille)


_FLOTTE_HORS_POOL = (
    "les modèles `{famille}` tournent ici sur l'abonnement d'UNE personne : une flotte "
    "appartient à l'organisation et ferait payer son forfait pour le travail de tous. "
    "Choisis un modèle servi par une clé d'organisation, ou demande à un admin de "
    "passer l'organisation en mode pool (des membres y prêtent leur abonnement).")


def peut_agir_pour(sub: str, proprietaire: Optional[str], famille: str) -> bool:
    """`sub` a-t-il le droit de faire tourner QUELQUE CHOSE sur l'abonnement de
    `proprietaire` ? **LA couture du partage** (arbitré le 21/09/2026).

    Le propriétaire seul, au niveau de la PERSONNE. Le prêt existe aussi, au niveau
    d'un AGENT : qui le tient en écriture par un partage le modifie sur le forfait
    de son propriétaire (`_acces_agent.forfaits_pretes`, 23/10/2026). Il ne passe pas par ici —
    il ne vaut que pour cet agent-là — mais par `prete` sur la retouche, et par la
    garde d'écriture (`db.update_trigger`). Demain, une connexion d'abonnement
    s'administrera peut-être comme les autres connecteurs, partagée à des personnes
    nommées : ce jour-là, la règle de la PERSONNE change ICI, et les trois chemins de
    pose comme la retouche d'un agent suivent."""
    return not proprietaire or proprietaire == sub


def exiger_le_droit_de_modifier(sub: str, agent: dict, famille: Optional[str],
                                champs: dict, *, prete: bool = False) -> None:
    """Retoucher l'agent d'un AUTRE posé sur un abonnement : refusé, sauf l'éteindre
    — ou si `sub` le tient en écriture par un partage (`prete`).

    Changer sa procédure, sa consigne ou ses outils, c'est faire exécuter SES
    instructions sur le forfait d'un autre — la même faute que changer son modèle,
    par une autre porte. Le propriétaire qui nomme un éditeur y consent pour cet
    agent ; le prêt vaut pour la famille où l'agent tourne DÉJÀ — le poser sur une
    autre famille d'abonnement reste à lui seul. ÉTEINDRE reste ouvert à qui
    administre l'org : personne ne doit avoir besoin du propriétaire pour arrêter
    un agent qui dérape (la suppression, elle, ne passe pas par ici)."""
    if not est_abonnement(famille):
        return
    if peut_agir_pour(sub, agent.get("sub"), famille):
        return
    if prete and famille == runner_models.famille(agent.get("model")):
        return
    if set(champs) <= {"enabled"} and champs.get("enabled") is False:
        return
    raise AuthzDenied(
        400, "subscription_personal_only",
        f"cet agent tourne sur l'abonnement `{famille}` de quelqu'un d'autre : ce que "
        "tu y changerais s'exécuterait sur SON forfait. Tu peux l'éteindre ; pour le "
        "modifier, il faut qu'il te soit partagé en éditeur.")


def servable(sub: Optional[str], famille: str) -> tuple[bool, Optional[str], Optional[str]]:
    """La connexion de `sub` peut-elle servir un travail `famille` ?

    Rend `(servable, statut, sandbox_id)`. Un porteur absent n'est jamais servable :
    sans personne, il n'y a pas d'abonnement à consommer.

    ⚠️ `paused_limit` EST servable ici, et ce n'est pas une largesse : l'attente de
    l'échéance vit dans la réservation (`claim_next_job`), pas dans cette garde.
    Deux juges du même plafond finiraient par se contredire — voir plus bas."""
    if not sub:
        return False, None, None
    return servable_de_la_ligne(user_subscriptions.get_subscription(sub, famille) or {})


def servable_de_la_ligne(ligne: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """`servable`, sur une ligne déjà lue (`{}` = aucune)."""
    statut, sandbox = ligne.get("statut"), ligne.get("sandbox_id")
    if not sandbox:
        return False, statut, None
    if statut == user_subscriptions.CONNECTE:
        return True, statut, sandbox
    if statut == user_subscriptions.PLAFOND:
        # ⚠️ Un plafond n'est JAMAIS un refus ici (corrigé le 21/09/2026). Tant que
        # son échéance est future, la réservation saute la personne — ce travail
        # n'arrive donc ici qu'une fois l'échéance PASSÉE, ou inconnue. Le refuser
        # l'ARRÊTERAIT DÉFINITIVEMENT pour un plafond qui n'existe plus : la file
        # rendait le travail et la garde le tuait (mesuré en base). On sert ; si
        # le forfait est encore épuisé, le fournisseur le dira, et le worker
        # rapportera une nouvelle échéance.
        return True, statut, sandbox
    return False, statut, sandbox


def ouvert_a_toute_l_instance() -> bool:
    """L'instance ouvre-t-elle le chemin à TOUT compte (`ENV_OUVERT_A_TOUS`) ? Lu à
    chaque appel : l'ouvrir ne demande pas de redémarrer. Une valeur qui n'est ni un oui
    ni un non LÈVE : `yes` mal orthographié fermerait le chemin en silence, et chacun
    recevrait un 403 sans que l'exploitation en sache rien."""
    return interrupteurs.oui_non(ENV_OUVERT_A_TOUS, os.environ.get(ENV_OUVERT_A_TOUS))


def ouvert(sub: str) -> bool:
    """Le chemin est-il ouvert à `sub` ? L'instance l'ouvre à tous
    (`ouvert_a_toute_l_instance`), sinon la personne porte l'option `OPTION`. Seule
    source, lue par la garde ET par `/api/me` (le front n'affiche que ce qui passera)."""
    if ouvert_a_toute_l_instance():
        return True
    return access.has_option(sub, OPTION)


def exiger_ouvert(sub: str, famille: str) -> None:
    """Ce chemin n'est ouvert qu'aux personnes pour qui `ouvert` répond vrai."""
    if not ouvert(sub):
        raise AuthzDenied(
            403, "subscription_not_enabled",
            f"les modèles `{famille}` tournent sur l'abonnement personnel de qui les "
            "pose, et ce chemin n'est ouvert qu'à des personnes nommées. Demande-le à "
            "l'équipe oto.")


def exiger_a_la_pose(sub: str, proprietaire: Optional[str], famille: Optional[str],
                     *, flotte: bool = False, org_id: Optional[int] = None) -> None:
    """Le refus LISIBLE au moment de poser un agent sur un abonnement.

    `proprietaire` = le `sub` de l'agent existant (None à la création : c'est
    l'appelant qui le devient). `org_id` = l'org où l'agent tournera : c'est son MODE
    qui dit quel abonnement paiera.

    **En mode pool**, ce n'est plus la connexion du demandeur qui se juge (il ne paie
    pas) mais le pool : au moins un membre de l'org doit lui prêter un abonnement
    servable, sinon l'agent resterait programmé sans jamais tourner. Une flotte y
    passe. Le chemin, lui, doit toujours être ouvert au demandeur (`ouvert`) : par
    son option, ou à toute l'instance quand elle le déclare.

    ⚠️ La propriété de l'agent se juge dans les DEUX modes (`peut_agir_pour`) :
    l'org peut repasser en personnel, et l'agent d'un autre retouché pendant le pool
    tournerait alors sur SON forfait avec les consignes d'un autre."""
    if not est_abonnement(famille):
        return
    exiger_ouvert(sub, famille)
    pool = en_pool(org_id, famille)
    if flotte and not pool:
        raise AuthzDenied(400, "subscription_personal_only",
                          _FLOTTE_HORS_POOL.format(famille=famille))
    if not peut_agir_pour(sub, proprietaire, famille):
        raise AuthzDenied(
            400, "subscription_personal_only",
            f"cet agent appartient à quelqu'un d'autre, et les modèles `{famille}` "
            "tournent sur l'abonnement de leur propriétaire. Seule la personne qui "
            "possède l'agent peut le poser sur le sien.")
    if pool:
        # ⚠️ `taille_du_pool_a_la_pose`, PAS `taille_du_pool` : un prêteur au plafond
        # pourra servir dès son échéance, et la pose juge « ça tournera un jour ? »,
        # pas « ça tournerait à la seconde près ? ». Le mode personnel dit déjà oui
        # dans ce cas (`servable()` rend True sur un `paused_limit`) ; refuser ici
        # rendait le pool STRICTEMENT pire que le personnel pour la même org.
        if not org_subscription_pool.taille_du_pool_a_la_pose(org_id, famille):
            raise AuthzDenied(
                400, "subscription_pool_empty",
                f"l'organisation fait tourner les modèles `{famille}` sur son pool, et "
                "aucun membre n'y prête d'abonnement utilisable. Un membre doit prêter "
                "le sien, connecté (Réglages › Fournisseurs de modèles), puis pose "
                "l'agent : posé sur un pool vide, il resterait programmé sans jamais "
                "tourner. Un prêteur au plafond ne compte PAS comme un pool vide — son "
                "forfait se réinitialise, et l'agent partira ce jour-là.")
        return
    servable_, statut, _ = servable(sub, famille)
    if not servable_:
        etat = statut or "jamais connectée"
        raise AuthzDenied(
            400, "subscription_not_connected",
            f"aucune connexion `{famille}` ouverte pour toi ({etat}). Ouvre-la dans "
            "Réglages › Fournisseurs de modèles, puis pose l'agent : un agent posé "
            "sans connexion resterait programmé sans jamais tourner.")
    # Connectée, mais pas OUVERTE dans cette org (08/10/2026) : un abonnement sert
    # les orgs où la personne l'a ouvert, une à une, jamais toutes par défaut.
    if not user_subscriptions.sert_dans(sub, famille, org_id):
        raise AuthzDenied(
            400, "subscription_not_used_here",
            f"ton abonnement `{famille}` est connecté, mais pas ouvert dans cette "
            "organisation : il ne sert que les orgs où tu l'as ouvert. Ouvre-le pour "
            "cette organisation dans Réglages › Fournisseurs de modèles, puis pose "
            "l'agent.")


def exiger_limite_perso_valide(limite_pct: Optional[int]) -> None:
    """Un plafond PERSO est un pourcentage entier de 1 à
    `user_subscriptions.LIMITE_PERSO_MAX`, ou `None` (aucun : celui de l'org)."""
    plus_haut = user_subscriptions.LIMITE_PERSO_MAX
    if limite_pct is not None and not 1 <= limite_pct <= plus_haut:
        raise AuthzDenied(
            400, "invalid_limit",
            f"`limit_pct` doit être un entier de 1 à {plus_haut} (reçu {limite_pct}), ou null.")


def exiger_limite_valide(limite_pct: Optional[int]) -> None:
    """Un plafond (org ou perso) est un pourcentage entier de 1 à 100, ou `None`
    (org : revenir au défaut ; perso : aucun). 0 n'est pas un plafond : c'est ne plus
    rien servir, et le geste pour ça est de se déconnecter."""
    if limite_pct is not None and not 1 <= limite_pct <= 100:
        raise AuthzDenied(
            400, "invalid_limit",
            f"`limit_pct` doit être un entier de 1 à 100 (reçu {limite_pct}), ou null.")


def seuil(sub: str, org_id: Optional[int], famille: str) -> float:
    """La part d'une fenêtre du forfait (0..1) au-delà de laquelle les travaux de
    `sub` sur `famille`, lancés dans `org_id`, ATTENDENT la réinitialisation.

    **LA règle du plafond**, et elle n'est écrite qu'ici (le SQL de
    `db.runner_jobs._pause_pour_l_org` la relit) : le plafond PERSO de la personne
    s'il est posé — plus bas OU plus haut que celui de l'org, c'est son forfait —,
    borné à `user_subscriptions.LIMITE_PERSO_MAX` ; sinon le plafond de l'org
    (`DEFAUT_LIMITE_PCT` si elle n'a rien réglé)."""
    perso = (user_subscriptions.get_subscription(sub, famille) or {}).get("limite_pct")
    if perso is not None:
        return min(perso, user_subscriptions.LIMITE_PERSO_MAX) / 100
    reglee = org_subscription_limits.get_limite(org_id, famille) if org_id else None
    pct = reglee["limite_pct"] if reglee else DEFAUT_LIMITE_PCT
    return pct / 100


def raison_de_peremption(sub: Optional[str], org_id: int,
                         famille: str, etat_runner: dict,
                         modele: Optional[str] = None) -> str:
    """La raison SERVIE quand le tick périme une occurrence non prise DANS SON
    CYCLE (27/09/2026) — nommant la VRAIE cause plutôt que le texte générique
    d'avant, qui disait toujours « aucun agent ne dessert cette organisation »
    même quand un worker de la famille existait et que la seule chose qui
    manquait était la connexion PERSONNELLE du propriétaire, ou un pool momentanément
    en pause. Les deux diagnostics n'envoient pas au même geste : le premier dit
    « préviens l'exploitant », le second dit « reconnecte-toi, ça repartira tout
    seul ». Confondre les deux a fait tourner en rond un propriétaire qui n'avait
    qu'à se reconnecter (mesuré sur un déclencheur réel, le 25/09/2026).

    ⚠️ **Best-effort, jamais une garantie** : lu au moment de la péremption, cet
    état a pu changer une seconde avant ou après (une reconnexion qui gagne de
    justesse contre le tick, par exemple) — la RÈGLE d'expiration (occurrence
    superседée) ne change pas d'un mot, seul le TEXTE qui l'accompagne s'affine.

    `modele` = le modèle déclaré : il nomme le dépôt d'un repli sur la clé d'org
    (`runner_models.repli_api`), que le diagnostic d'un pool au plafond lit.

    `famille` est toujours déclarée : un déclencheur sans modèle n'enfile rien, sa
    livraison est refusée avant (`runner_models.motif_sans_famille`, 21/10/2026).
    """
    base = "occurrence non prise dans son cycle : le déclencheur a enfilé la suivante."
    aucun_agent = f"{base} Aucun agent ne dessert cette organisation."
    if famille not in (etat_runner.get("families") or []):
        # Aucun worker vivant ne sert cette famille pour cette org (21/10/2026) : le
        # dire, et depuis quand. « Aucun agent ne dessert cette organisation »
        # laissait croire à une org sans runner, quand c'est une FAMILLE qui manque.
        return _famille_non_servie(base, org_id, famille)
    if not est_abonnement(famille):
        # Famille API servie : le détail d'une clé manquante appartient à
        # `_cle_exigee`, pas à ce module.
        return base
    if not sub:
        return aucun_agent
    if en_pool(org_id, famille):
        # ⚠️ `taille_du_pool` compte les prêteurs SERVABLES MAINTENANT — 0 y est
        # ambigu (personne ne prête ? ou tout le monde est au plafond ?) pour un
        # diagnostic. `taille_totale_du_pool` compte les prêts VIVANTS sans
        # regarder l'état : lui seul distingue les deux.
        if not org_subscription_pool.taille_totale_du_pool(org_id, famille):
            return (f"{base} Le pool `{famille}` de l'organisation est vide : "
                    "personne n'y prête d'abonnement connecté.")
        if not org_subscription_pool.taille_du_pool(org_id, famille):
            # ⚠️ Aucun prêteur servable : au plafond, ou à reconnecter ? Seul le
            # premier est une PAUSE. `taille_du_pool_a_la_pose` compte les prêteurs
            # connectés ou au plafond : 0, c'est que tous doivent se reconnecter
            # — l'annoncer « en pause » renverrait attendre qui doit agir.
            if not org_subscription_pool.taille_du_pool_a_la_pose(org_id, famille):
                return (f"{base} Les membres qui prêtent leur abonnement `{famille}` "
                        "au pool de l'organisation doivent le reconnecter (Réglages › "
                        "Fournisseurs de modèles) — il repartira tout seul.")
            return _pool_au_plafond(base, org_id, famille, modele)
        # Un prêteur EST servable maintenant : la vraie cause a déjà cédé.
        return aucun_agent
    # ⚠️ `servable()` répond FAIT EXPRÈS `True` pour un `paused_limit`, quelle que
    # soit son échéance (l'attente vit dans la réservation, pas dans cette
    # garde) : la réutiliser ici confondrait TOUT plafond avec une cause déjà
    # résolue. Le diagnostic lit donc la ligne lui-même.
    ligne = user_subscriptions.get_subscription(sub, famille) or {}
    statut = ligne.get("statut")
    if statut in (user_subscriptions.A_RECONNECTER, user_subscriptions.DECONNECTE):
        return (f"{base} L'abonnement `{famille}` de son propriétaire doit se "
                "reconnecter (Réglages › Fournisseurs de modèles) — il repartira "
                "tout seul.")
    if statut == user_subscriptions.PLAFOND:
        reset = echeance_utc(ligne.get("limit_reset_at"))
        if reset and reset > datetime.now(timezone.utc):
            return (f"{base} L'abonnement `{famille}` de son propriétaire avait "
                    "atteint son plafond de consommation.")
        # Échéance passée (ou inconnue) : la réservation aurait déjà servi ce
        # travail — la vraie cause a cédé, le texte générique reste le plus
        # honnête qu'on puisse écrire.
        return aucun_agent
    return aucun_agent


def echeance_utc(v) -> Optional[datetime]:
    """Une échéance lue en base — texte « AAAA-MM-JJ HH:MM:SS » (le `row_factory` du
    pool), ou `datetime` — en `datetime` UTC ; None si absente."""
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if v and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v or None


def _famille_non_servie(base: str, org_id: int, famille: str) -> str:
    """Aucun worker vivant ne sert `famille` pour cette org : quand en a-t-on vu un
    pour la dernière fois (`dernier_vu_de_famille`) ? Jamais = « aucun worker vu »."""
    vu = echeance_utc(db_runner_jobs.dernier_vu_de_famille(org_id, famille))
    quand = f"dernier vu le {vu:%d/%m/%Y %H:%M} UTC" if vu else "aucun worker vu"
    return f"{base} Aucun worker ne sert la famille `{famille}` ({quand})."


def _pool_au_plafond(base: str, org_id: int, famille: str, modele: Optional[str]) -> str:
    """Un pool AU PLAFOND, et pourquoi le repli sur la clé d'org ne l'a pas pris
    (21/10/2026) : fermé, ou ouvert sans clé déposée. Sans ce détail, l'expiration ne
    disait ni jusqu'à quand attendre, ni quel réglage aurait servi le travail. Les
    autres cas gardent le texte d'avant. UNE lecture (`pool_au_plafond`)."""
    depot = runner_models.famille(runner_models.repli_api(modele))
    pause = org_subscription_pool.pool_au_plafond(org_id, famille, depot)
    repli = (org_subscription_pool.repli_api_par_defaut() if pause["repli_api"] is None
             else bool(pause["repli_api"]))
    reprise = echeance_utc(pause["reprise"])
    au_plafond = (f"{base} Le pool `{famille}` de l'organisation est en pause : "
                  "abonnements au plafond"
                  + (f" jusqu'au {reprise:%d/%m/%Y %H:%M} UTC" if reprise else ""))
    if not repli:
        return f"{au_plafond}, repli sur la clé d'org fermé."
    if depot and not pause["cle_org"]:
        return (f"{au_plafond}, repli sur la clé d'org ouvert mais aucune clé `{depot}` "
                "déposée par l'organisation.")
    return (f"{base} Le pool `{famille}` de l'organisation était en pause "
            "(plafond de consommation atteint) au moment où la suivante est arrivée.")


#: Les `rate_limit_info.status` qui SERVENT encore. `allowed_warning` n'est pas un
#: refus : le fournisseur prévient seulement qu'une fenêtre approche de sa limite. Lu
#: comme un refus, il mettait la personne en pause jusqu'à l'échéance la plus
#: lointaine (sept jours) avec quelques % d'usage — seul le seuil juge la jauge. Un
#: état inconnu reste un refus : dans le doute, on attend.
ETATS_SERVABLES = (None, "allowed", "allowed_warning")


def noter_rapport(conclu: dict, ok: bool, resultat: Optional[dict]) -> None:
    """Ce que le worker a VU du forfait en exécutant ce travail, porté sur la
    connexion qui l'a SERVI (`conclu["abonnement"]` : le demandeur en mode personnel,
    le prêteur en mode pool — le seuil est alors le SIEN dans l'org du travail). Appelé à la conclusion, pour un worker de
    plateforme seulement (l'appelant le garantit).

    Le rapport voyage dans le résultat déclaré, clé `abonnement` :

        {"etat": "allowed" | "allowed_warning" | <autre>,   # `rate_limit_info.status`
         "fenetre": "five_hour", "echeance": 1790029800,  # `rateLimitType` / `resetsAt`
         "deconnecte": true,                     # le programme n'a plus de session
         "fenetres": {"five_hour": {"utilization": 0.07, "resetsAt": 1790029800},
                      "seven_day": {"utilization": 0.49, "resetsAt": 1790053200}}}

    ⚠️ DEUX fenêtres, pas une : un forfait s'épuise sur cinq heures OU sur sept
    jours, et l'échéance à attendre est celle de la fenêtre saturée — la plus
    LOINTAINE s'il y en a deux, sinon la personne repartirait pour retomber.
    « Saturée » = au-delà du plafond de consommation (`seuil`), le même pour les deux
    fenêtres. Le travail qui rapporte est déjà FINI : un plafond ne coupe jamais un
    run, il fait attendre les suivants.

    ⚠️ Jamais une levée : ce rapport est un à-côté de la conclusion. Un rapport mal
    formé se journalise et s'ignore — faire échouer `complete` pour lui laisserait
    un travail TERMINÉ re-servi à l'expiration de son bail.
    """
    # Le porteur du FORFAIT, pas forcément le demandeur : en mode pool, c'est le
    # membre qui a prêté son abonnement — sa connexion, son plafond perso.
    famille = conclu.get("model_family")
    porteur = conclu.get("abonnement") or conclu.get("sub")
    if not est_abonnement(famille) or not porteur:
        return
    try:
        rapport = (resultat or {}).get("abonnement")
        if not isinstance(rapport, dict):
            # Aucun rapport : un succès prouve au moins que la session tient.
            if ok:
                user_subscriptions.marquer_statut(
                    porteur, famille, user_subscriptions.CONNECTE, ok=True,
                    observe=True)
            return
        if rapport.get("deconnecte") is True:
            user_subscriptions.marquer_statut(
                porteur, famille, user_subscriptions.A_RECONNECTER, observe=True)
            return
        fenetres = rapport.get("fenetres") or {}
        plafond = seuil(porteur, conclu.get("org_id"), famille) if fenetres else None
        # (échéance, fenêtre) : la fenêtre part avec la pause (`limit_fenetre`) — une
        # pause dont on connaît la fenêtre n'est pas revérifiée par la sonde, et une
        # fenêtre propre à un modèle n'est pas levée par la sonde d'un autre.
        echeances = [
            (f["resetsAt"], nom) for nom, f in fenetres.items()
            if isinstance(f, dict)
            and isinstance(f.get("resetsAt"), (int, float))
            and isinstance(f.get("utilization"), (int, float))
            and f["utilization"] >= plafond]
        refuse = rapport.get("etat") not in ETATS_SERVABLES
        if refuse and isinstance(rapport.get("echeance"), (int, float)):
            # Le fournisseur NOMME la fenêtre qui a refusé (`rateLimitType`, rapportée
            # en `fenetre`/`echeance` par oto-runner) : c'est son échéance qu'on attend,
            # pas la plus lointaine — un refus sur cinq heures ne fige plus une semaine.
            nommee = rapport.get("fenetre")
            echeances.append((rapport["echeance"],
                              nommee if isinstance(nommee, str) and nommee else None))
        elif refuse and not echeances:
            # Refusé sans fenêtre nommée ni saturée lisible (un runner d'avant
            # `fenetre`) : toutes les échéances connues comptent, faute de savoir
            # laquelle a mordu — et la fenêtre reste INCONNUE.
            echeances = [(f["resetsAt"], None) for f in fenetres.values()
                         if isinstance(f, dict)
                         and isinstance(f.get("resetsAt"), (int, float))]
        if echeances or refuse:
            # Bornée AVANT la conversion : une échéance en millisecondes déborde `fromtimestamp`,
            # et la levée faisait ignorer le rapport entier.
            borne = (datetime.now(timezone.utc) + ECHEANCE_MAX).timestamp()
            retenue, fenetre = (max(echeances, key=lambda e: e[0]) if echeances
                                else (None, None))
            quand = (datetime.fromtimestamp(min(retenue, borne), tz=timezone.utc)
                     if echeances else None)
            # La CAUSE part avec la pause (décision du 28/09/2026) : le repli vers la
            # clé d'une org n'en part que si le forfait est épuisé, ou si le seuil de
            # l'org du TRAVAIL est dépassé — la pause, elle, est portée par la
            # personne, et une autre org peut en tolérer davantage.
            charges = [f["utilization"] for f in fenetres.values()
                       if isinstance(f, dict)
                       and isinstance(f.get("utilization"), (int, float))]
            # Le rapport BRUT au journal : la ligne ne garde que la cause résumée
            # (`limit_epuise`, `limit_utilisation`), qui ne dit ni l'état exact du
            # fournisseur ni la fenêtre qui a mordu — sans lui, une pause douteuse ne
            # se prouve pas.
            logger.info("forfait en pause (famille %s, porteur %s) : etat=%r fenetre=%r "
                        "seuil=%s fenetres=%s → jusqu'à %s", famille, porteur,
                        rapport.get("etat"), rapport.get("fenetre"), plafond, fenetres,
                        quand)
            user_subscriptions.marquer_statut(
                porteur, famille, user_subscriptions.PLAFOND, limit_reset_at=quand,
                epuise=refuse, utilisation=max(charges) if charges else None,
                fenetre=fenetre, observe=True)
            return
        user_subscriptions.marquer_statut(
            porteur, famille, user_subscriptions.CONNECTE, ok=bool(ok),
            observe=True)
    except Exception:
        logger.warning("rapport d'abonnement illisible pour le travail conclu "
                       "(famille %s) — ignoré, la conclusion tient", famille,
                       exc_info=True)


def _nombre(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pourquoi_illisible(rapport) -> Optional[str]:
    """Ce qui rend un rapport de sonde illisible, ou None s'il se lit."""
    if not isinstance(rapport, dict):
        return "`result.abonnement` absent ou pas un objet"
    for champ, type_, nom in (("etat", str, "un texte"), ("fenetre", str, "un texte"),
                              ("deconnecte", bool, "un booléen")):
        if rapport.get(champ) is not None and not isinstance(rapport[champ], type_):
            return f"`{champ}` n'est pas {nom}"
    if rapport.get("echeance") is not None and not _nombre(rapport["echeance"]):
        return "`echeance` n'est pas un nombre"
    fenetres = rapport.get("fenetres")
    if fenetres is not None and not isinstance(fenetres, dict):
        return "`fenetres` n'est pas un objet"
    for nom, f in (fenetres or {}).items():
        if not isinstance(f, dict):
            return f"la fenêtre `{nom}` n'est pas un objet"
        for champ in ("utilization", "resetsAt"):
            if f.get(champ) is not None and not _nombre(f[champ]):
                return f"`{nom}.{champ}` n'est pas un nombre"
    if not (isinstance(rapport.get("etat"), str) or fenetres
            or rapport.get("deconnecte") is True):
        return "ni état, ni fenêtre, ni session perdue : il ne dit rien"
    return None


def exiger_rapport_lisible(resultat) -> dict:
    """Le rapport d'une SONDE (`op=forfait`), relu AVANT d'être porté — ou la 400
    `forfait_report_unreadable`. Rend le rapport (`resultat["abonnement"]`).

    ⚠️ Pas la tolérance de `noter_rapport` : là, le rapport est l'à-côté d'une conclusion
    qui doit tenir quoi qu'il arrive, et un rapport mal formé s'y avale. Ici, il est
    l'OBJET de l'appel : l'avaler rendrait un verdict sur une ligne que rien n'a lue, et
    le worker lancerait un run sur un forfait qu'il croirait vérifié.

    Lisible = la forme qu'oto-runner envoie (`rapport_du_forfait`), et qui dit quelque
    chose : un état, une fenêtre, ou une session perdue."""
    rapport = resultat.get("abonnement") if isinstance(resultat, dict) else None
    pourquoi = _pourquoi_illisible(rapport)
    if pourquoi:
        raise AuthzDenied(400, "forfait_report_unreadable",
                          f"`op=forfait` : rapport de la sonde illisible ({pourquoi}). "
                          "Attendu `result.abonnement` = {etat, fenetres, fenetre?, "
                          "echeance?, deconnecte?}.")
    return rapport


def noter_rapport_du_travail(job_id: int, ok: bool, resultat: Optional[dict]) -> None:
    """`noter_rapport`, en lisant soi-même à qui le travail appartenait.

    ⚠️ La LECTURE aussi est sous la garde (revue du 21/09/2026). Faite chez
    l'appelant, un hoquet de base à cet instant sortait en 500 d'un `complete`
    dont le travail était DÉJÀ conclu — et la libération des lignes du run, qui
    suit, ne s'exécutait jamais. Un à-côté ne doit pas pouvoir casser le geste
    qu'il accompagne, ni par son écriture, ni par sa lecture."""
    try:
        conclu = db_runner_jobs.porteur_et_famille(job_id) or {}
    except Exception:
        logger.warning("porteur du travail %s illisible — rapport d'abonnement "
                       "ignoré, la conclusion tient", job_id, exc_info=True)
        return
    noter_rapport(conclu, ok, resultat)
