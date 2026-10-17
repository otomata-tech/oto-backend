"""La fin du droit `unipile` : cesser de payer les comptes de qui ne l'a plus.

Décision du 23/09/2026 (oto-backend#806) : dès la fin du droit (fin d'essai, résiliation,
don échu), les canaux cessent de marcher — c'est la relecture du droit à l'usage, pas ce
module. Ici, la DÉPENSE : un compte branché sur la clé de la plateforme se paie tant
qu'il existe chez unipile, qu'on s'en serve ou non. Le propriétaire est prévenu ; au
bout du délai (7 jours par défaut, `OTO_UNIPILE_FIN_DE_DROIT_DELAI_JOURS`), si le droit
n'est pas revenu, le compte est supprimé chez unipile. Un compte branché sur la propre
clé du client (`platform_seat = false`) n'est jamais regardé.

Ce module n'agit que sur la perte d'un droit `unipile` DÉCLARÉ, jamais sur la fin
d'un essai : l'essai n'est pas un droit déclaré, sa fin (suspension de l'org,
libération des comptes Unipile) appartient au service commerce
(docs/droits-declares.md).

Un travail de maintenance, pas un événement : `oto-mcp maintenance unipile-fin-de-droit`,
tiré par le timer quotidien (PROD seulement — la base est partagée). Il relit l'état à
chaque passage et ne dépend d'aucun signal de fin de droit : un droit qui s'éteint par
son échéance ne prévient personne, et un événement perdu ne se rejoue pas. Chaque étape
est idempotente — le rejouer ne marque, ne prévient ni ne supprime deux fois :

Le droit est lu PAR BINDING, pour son titulaire (`sub` de la ligne) dans l'org du
binding (`access.has_right`) : celui de l'org, ou une ligne posée sur la personne, dans
l'org ou partout (ADR 0070 §7). Une personne servie par un droit personnel garde ses
comptes même si son org n'a pas le droit.

1. **retour du droit** : les marques des bindings dont le titulaire a (de nouveau) le
   droit s'effacent, binding par binding ;
2. **perte** : un binding en service dont le titulaire n'a pas le droit reçoit
   `entitlement_lost_at` (jamais repoussée : le délai part du premier constat) ;
3. **préavis** : un e-mail par (propriétaire, org), `entitlement_notice_at` posé APRÈS
   l'envoi ;
4. **suppression** : un compte dont AUCUNE org qui le tient en service n'a le droit,
   dont le propriétaire a été prévenu et dont le délai est échu, est supprimé chez
   unipile par le même geste que `oto_admin_unipile_seat release`
   (`capabilities/unipile_seats.liberer`) — et le journal le dit, compte par compte.
   Seuls les comptes que l'instance unipile LISTE sont candidats : une suppression déjà
   faite ne se rejoue pas, une suppression ratée se reprend au passage suivant.

⚠️ **Fermé par défaut** (`OTO_UNIPILE_FIN_DE_DROIT`, même dispositif que
`OTO_ALERTE_CREDENTIAL`) : le travail tourne et DIT ce qu'il ferait, sans rien écrire,
tant que le drapeau n'est pas posé. Raison : le droit est lu dans `org_entitlements`
(`access.has_right`), que la reprise des abonnements remplit ; avant elle, il rend
faux pour tout abonné, et le travail marquerait, préviendrait puis supprimerait les
comptes de clients qui paient. Une garde « table vide » ne suffisait pas : la table
porte déjà des droits posés à la main, donc elle n'est pas vide alors qu'elle est
incomplète. Seule une décision humaine, prise après lecture du mode à blanc, sait que
la reprise est faite. Et une fois ouvert, le travail refuse encore de tourner si AUCUN
droit `unipile` vivant n'existe alors que des sièges sont en service — la table n'est
alors pas remplie, et la supposer juste supprimerait tout.

⚠️ **Les orgs d'un tenant tiers sont écartées** (`db.org_tenant_slug`, fermé sur erreur) :
le préavis s'adresserait aux clients d'un partenaire, dans notre produit et avec notre
offre, et supprimer sans préavis n'est pas la décision. Le travail les compte
(`tenant_tiers_ecartes`) sans y toucher.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

log = logging.getLogger("oto_mcp.unipile_fin_de_droit")

DROIT = "unipile"

#: Interrupteur du travail. Lu à CHAQUE passage : l'ouvrir ne demande pas de redémarrer.
ENV_ACTIF = "OTO_UNIPILE_FIN_DE_DROIT"
#: Délai, en jours, entre le premier constat de la perte et la suppression.
ENV_DELAI = "OTO_UNIPILE_FIN_DE_DROIT_DELAI_JOURS"
_DELAI_DEFAUT = "7"


def actif() -> bool:
    return os.environ.get(ENV_ACTIF, "").strip().lower() in ("1", "true", "yes", "on")


def delai_jours() -> int:
    """Le délai réglé pour cette instance. Une valeur illisible ou nulle LÈVE : un délai
    faux ne se remplace pas par un défaut en silence, il supprimerait trop tôt."""
    brut = os.environ.get(ENV_DELAI, _DELAI_DEFAUT).strip()
    try:
        jours = int(brut)
    except ValueError:
        raise ValueError(f"{ENV_DELAI}={brut!r} : un nombre entier de jours est attendu")
    if jours < 1:
        raise ValueError(f"{ENV_DELAI}={jours} : au moins un jour, sinon le préavis "
                         "annoncerait une suppression déjà faite")
    return jours


def _org_est_a_nous(org_id: int, cache: dict) -> bool:
    """L'org relève-t-elle du tenant primaire, sur réponse franche ? Fermé sur erreur :
    sans réponse, on ne s'adresse pas à elle et on ne supprime rien chez elle."""
    if org_id not in cache:
        from . import db, tenancy
        try:
            cache[org_id] = db.org_tenant_slug(int(org_id)) == tenancy.primary_slug()
        # noqa: SILENT — fermé par défaut, et dit : l'org est écartée et comptée.
        except Exception:  # noqa: BLE001
            log.warning("unipile fin de droit : tenant de l'org %s illisible — org "
                        "écartée", org_id, exc_info=True)
            cache[org_id] = False
    return cache[org_id]


def _prevenir(groupe: list[dict], supprime_le) -> bool:
    """Un préavis pour les comptes d'UN propriétaire dans UNE org."""
    from . import config, email_templates
    tete = groupe[0]
    if not tete.get("email"):
        return False
    return email_templates.send_unipile_fin_de_droit_email(
        tete["email"], org_name=tete.get("org_name"),
        canaux=[r["provider"] for r in groupe], supprime_le=supprime_le,
        app_url=config.dashboard_url_for(tete["sub"]), locale=tete.get("locale"))


def balayer(*, dry_run: bool = False) -> dict:
    """Un passage complet. `dry_run`, ou le drapeau fermé : compte, n'écrit rien."""
    from . import access
    from .capabilities import unipile_seats
    from .db import unipile_fin_de_droit as db_fdd

    client = unipile_seats._platform_client()
    if client is None:
        return {"configured": False,
                "note": "aucune clé plateforme unipile : aucun siège à payer"}
    ouvert = actif()
    ecrit = ouvert and not dry_run
    delai = delai_jours()

    droits: dict = {}
    a_nous: dict = {}
    ecartes: set = set()

    def lire() -> list[dict]:
        """Les bindings à regarder, org d'un tenant tiers écartée, droit du TITULAIRE
        relu une fois par (titulaire, org) et par passage."""
        lignes = []
        for r in db_fdd.sieges_plateforme(delai):
            org = int(r["org_id"])
            if not _org_est_a_nous(org, a_nous):
                ecartes.add((r["sub"], org, r["provider"]))
                continue
            cle = (r["sub"], org)
            if cle not in droits:
                droits[cle] = access.has_right(r["sub"], org, DROIT)
            r["entitled"] = droits[cle]
            lignes.append(r)
        return lignes

    lignes = lire()
    vivantes = [r for r in lignes if r["disconnected_at"] is None]

    if ecrit and vivantes and not db_fdd.droit_unipile_declare_quelque_part():
        raise RuntimeError(
            "aucun droit `unipile` vivant dans org_entitlements alors que "
            f"{len(vivantes)} binding(s) sont en service sur la clé plateforme : la table "
            "n'est pas remplie. Rien n'a été marqué, prévenu ni supprimé.")

    # 1. Le droit est revenu : effacer, binding par binding (le droit est celui de son
    # titulaire, pas de l'org : un voisin sans droit dans la même org garde sa marque).
    revenus = [r for r in lignes if r["entitled"]
               and (r["entitlement_lost_at"] or r["entitlement_notice_at"])]
    # 2. La perte : marquer les bindings en service qui ne le sont pas encore.
    a_marquer = [r for r in vivantes if not r["entitled"] and r["entitlement_lost_at"] is None]
    efface = marque = 0
    if ecrit:
        efface = db_fdd.effacer_perte(revenus)
        marque = db_fdd.marquer_perte(a_marquer)
        lignes = lire()  # relire la donnée écrite, dates de la base comprises

    # Par compte : un compte adopté dans deux orgs reste dû tant qu'un de ses bindings a
    # le droit. Seul un compte que PERSONNE ayant droit ne tient en service est concerné.
    comptes: dict = {}
    for r in lignes:
        comptes.setdefault(r["account_id"], []).append(r)
    condamnes = {aid: rs for aid, rs in comptes.items()
                 if not any(r["entitled"] and r["disconnected_at"] is None for r in rs)
                 and any(r["entitlement_lost_at"] for r in rs)}

    # 3. Le préavis : un par (propriétaire, org), à la date la plus tardive du groupe.
    a_prevenir: dict = {}
    for aid, rs in condamnes.items():
        for r in rs:
            if r["entitlement_lost_at"] and r["entitlement_notice_at"] is None:
                a_prevenir.setdefault((r["sub"], r["org_id"]), []).append(r)
    envoyes = sans_adresse = 0
    for groupe in a_prevenir.values():
        if not groupe[0].get("email"):
            sans_adresse += 1
            continue
        if not ecrit:
            continue
        # Les dates de la base arrivent en chaînes « AAAA-MM-JJ hh:mm:ss » (`db/_conn`) :
        # leur ordre est celui du texte, le gabarit reçoit une date.
        date = max(max(x["supprime_le"] for x in condamnes[r["account_id"]]
                       if x["supprime_le"] is not None) for r in groupe)
        if _prevenir(groupe, datetime.fromisoformat(date)):
            envoyes += 1
            db_fdd.marquer_preavis(groupe)
        else:
            log.warning("unipile fin de droit : préavis NON envoyé à sub=%s org=%s — "
                        "repris au passage suivant", groupe[0]["sub"], groupe[0]["org_id"])
    if ecrit and envoyes:
        lignes = lire()
        condamnes = {aid: [r for r in lignes if r["account_id"] == aid] for aid in condamnes}

    # 4. La suppression : délai échu, préavis fait, compte encore listé par l'instance.
    echus = [aid for aid, rs in condamnes.items()
             if all(r["echu"] for r in rs if r["entitlement_lost_at"])
             and any(r["entitlement_notice_at"] for r in rs)]
    supprimes: list = []
    echecs = deja_absents = 0
    if echus:
        instance = {a.get("id") for a in client.list_accounts()}
        deja_absents = sum(1 for aid in echus if aid not in instance)
        for aid in (a for a in echus if a in instance):
            if not ecrit:
                supprimes.append(aid)
                continue
            try:
                delies = unipile_seats.liberer(client, aid, unipile_seats._rows_for(aid))
            except Exception:  # noqa: BLE001 — un compte récalcitrant n'arrête pas les autres
                echecs += 1
                log.error("unipile fin de droit : suppression de %s ÉCHOUÉE — reprise au "
                          "passage suivant", aid, exc_info=True)
                continue
            supprimes.append(aid)
            rs = condamnes[aid]
            log.info("unipile fin de droit : compte %s SUPPRIMÉ chez unipile — org(s) %s "
                     "sans droit depuis %s, préavis du %s, %d binding(s) délié(s)", aid,
                     sorted({r["org_id"] for r in rs}),
                     min(r["entitlement_lost_at"] for r in rs if r["entitlement_lost_at"]),
                     min(r["entitlement_notice_at"] for r in rs
                         if r["entitlement_notice_at"]), delies)

    return {
        "configured": True,
        "actif": ouvert,
        "a_blanc": not ecrit,
        "delai_jours": delai,
        "en_service": len(vivantes),
        "tenant_tiers_ecartes": len(ecartes),
        "droit_revenu": efface if ecrit else len(revenus),
        "marques": marque if ecrit else len(a_marquer),
        "a_prevenir": len(a_prevenir),
        "preavis_envoyes": envoyes,
        "sans_adresse": sans_adresse,
        "supprimes": supprimes,
        "echecs": echecs,
        "deja_absents": deja_absents,
        "note": (None if ecrit else
                 "à blanc : rien n'a été marqué, prévenu ni supprimé — les compteurs "
                 "disent ce qu'un passage ouvert ferait"
                 + ("" if ouvert else f" ({ENV_ACTIF} n'est pas posé)")),
    }
