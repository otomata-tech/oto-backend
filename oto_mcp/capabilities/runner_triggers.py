"""Capacité « déclencheurs du runner » — la config qui fabrique des jobs (R3).

Deux faces, et c'est un choix de principe : un déclencheur est de la CONFIG
utilisateur, pas de la plomberie worker. « Tous les matins à 8h05, joue la
veille » doit pouvoir se poser EN CONVERSATION (`oto_trigger`) comme au
dashboard — c'est le `/schedule` du produit. La file de jobs, elle, reste
worker-only (`runner.jobs`, REST seul) : la frontière passe entre configurer
et exécuter.

Le FUSEAU se déclare, il ne se suppose pas : `tz` (défaut `Europe/Paris`,
écrit) — « 8h » doit dire quel 8h, sinon l'heure d'été décale toutes les
veilles d'une heure sans un mot. La validation (cron, fuseau, cadence
plancher) vit dans `runner_tick.validate_cron`, le même module qui calcule
les échéances : une seule vérité.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel

from . import _cle_exigee, _instruction, _modele
from .. import (access, db, runner_hook, runner_models, runner_tick, tool_alias,
                tool_registry, tool_visibility)
from ._authz import ORG_MEMBER
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                     RestBinding)
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_TZ_DEFAUT = "Europe/Paris"


class TriggerInput(BaseModel):
    op: Literal["create", "list", "get", "update", "delete",
                # Le webhook : (re)poser son secret — il n'est rendu QU'ICI, une
                # fois. Verbe séparé plutôt qu'un champ d'`update` : c'est une
                # rotation de credential, pas une retouche de configuration, et
                # elle CASSE la source en place tant qu'elle n'a pas le nouveau.
                "rotate_secret",
                # Ce que ce déclencheur a reçu — le journal que l'écran lit.
                "deliveries",
                # VIDER la file : périme ce qui attend et rend les créneaux.
                # Geste EXPLICITE, disponible à tout moment — en marche comme en
                # pause. C'est le seul moyen de se débarrasser d'un arriéré, et
                # c'est délibérément une décision de l'utilisateur : la pause,
                # elle, ne perd plus rien (13/09/2026).
                "clear_queue"]
    trigger_id: Optional[int] = None
    # create / update —
    procedure: Optional[str] = None
    cron: Optional[str] = None
    tz: Optional[str] = None
    tools: Optional[list[str]] = None
    project_id: Optional[int] = None
    input: Optional[str] = None
    label: Optional[str] = None
    max_steps: Optional[int] = None
    # Le modèle de l'agent, pris dans le catalogue (`runner.models`). Absent = on
    # ne touche à rien ; `""` sur `update` = revenir au modèle du worker.
    model: Optional[str] = None
    enabled: Optional[bool] = None
    # ── le WEBHOOK ────────────────────────────────────────────────────────────
    #: `schedule` (défaut) ou `webhook`. Posé à la CRÉATION et jamais après : un
    #: agent ne change pas de coup d'envoi en cours de route.
    kind: Optional[Literal["schedule", "webhook"]] = None
    #: Ce que l'agent fait du corps reçu. `ignore` (défaut) ne le transmet même
    #: pas ; `fields` n'en extrait que ce qui est nommé ; `inline` joint le tout.
    payload_mode: Optional[Literal["ignore", "fields", "inline"]] = None
    #: `{"lead_id": "$.data.id"}` — le mode `fields` et rien d'autre.
    payload_fields: Optional[dict[str, str]] = None
    #: Le débit de LISSAGE, par heure. Au-delà, une livraison est acceptée et son
    #: travail part plus tard — jamais refusée.
    max_per_hour: Optional[int] = None
    #: Au-delà de ce délai, un travail lissé ne part plus. Absent ou `0` = JAMAIS
    #: (le défaut) : un événement reçu part, même tard.
    freshness_seconds: Optional[int] = None
    #: `deliveries` : combien de livraisons rendre.
    limit: Optional[int] = None


class Trigger(BaseModel):
    """Un déclencheur tel que servi (les colonnes de `_COLS`, db/runner_triggers) :
    la procédure à jouer, quand (cron + tz), avec quels outils, et l'état de
    marche (enabled, next_due, last_enqueued_at)."""
    id: int
    org_id: Optional[int] = None
    sub: Optional[str] = None
    label: Optional[str] = None
    procedure: Optional[str] = None
    project_id: Optional[int] = None
    tools: Optional[list[str]] = None
    input: Optional[str] = None
    max_steps: Optional[int] = None
    #: Le modèle DÉCLARÉ. `null` = aucun : le worker qui prend le travail tourne
    #: sur le sien — c'est l'état de tout déclencheur posé avant le 12/09/2026.
    model: Optional[str] = None
    cron: Optional[str] = None
    tz: Optional[str] = None
    enabled: Optional[bool] = None
    next_due: Optional[str] = None
    last_enqueued_at: Optional[str] = None
    created_at: Optional[str] = None
    #: Ce que ce déclencheur a PERDU : des occurrences enfilées que personne n'est
    #: venu prendre dans leur cycle, et que le tick a périmées.
    #: ⚠️ Servi avec le déclencheur parce que c'est là qu'on le cherche. Compté à
    #: la main dans la file, il n'était visible de personne : quarante-et-une
    #: occurrences perdues sur treize jours n'ont été découvertes que le 02/09,
    #: en préparant autre chose.
    #: `0` est un vrai zéro (rien n'a été perdu), pas une absence de mesure.
    expired_count: Optional[int] = None
    #: La PREMIÈRE occurrence perdue et la DERNIÈRE : « depuis quand » et « est-ce
    #: encore en cours » sont deux questions différentes, et une seule date les
    #: confondrait. Une perte ancienne qui a cessé n'appelle pas le même geste
    #: qu'une perte qui continue ce matin.
    expired_since: Optional[str] = None
    expired_last: Optional[str] = None
    #: `schedule` ou `webhook` — ce qui donne le coup d'envoi.
    kind: Optional[str] = None
    payload_mode: Optional[str] = None
    payload_fields: Optional[dict] = None
    max_per_hour: Optional[int] = None
    fraicheur_s: Optional[int] = None
    #: L'URL à donner à la source. Servie sur un déclencheur webhook, jamais le
    #: secret — celui-ci n'existe en clair qu'au retour de `create`/`rotate_secret`.
    hook_url: Optional[str] = None
    #: Ce que ce déclencheur a reçu sur 24 h : reçues, refusées, la dernière.
    #: `0` est un vrai zéro, jamais une absence de mesure.
    deliveries_24h: Optional[int] = None
    deliveries_refused_24h: Optional[int] = None
    last_delivery: Optional[str] = None


class RunnerArme(BaseModel):
    """La présence d'un runner pour l'org — SERVIE avec les déclencheurs.

    ⚠️ Elle accompagne `list`/`get` parce qu'un déclencheur ne porte pas en
    lui-même de quoi savoir s'il sera joué : c'est une propriété de l'ORG, et
    elle manquait exactement là où on la cherche. Sans elle, la seule trace
    qu'un déclencheur ne s'exécute pas a été, le 26/08, une phrase tapée dans
    son propre LIBELLÉ."""
    armed: bool
    workers: int
    #: `None` = aucun worker n'est JAMAIS venu ; une date = il s'est tu depuis.
    #: Les deux n'appellent pas le même geste, et un seul booléen les confondrait.
    last_seen: Optional[str] = None
    #: Les familles de modèles qu'un worker vivant sert (`anthropic`, `mistral`).
    #: Vide ne veut pas dire « aucun runner » — `armed` le dit : un worker qui ne
    #: déclare pas de famille sert quand même les agents sans modèle.
    families: list[str] = []
    #: Le catalogue, chaque modèle marqué `served` — ce qu'un écran propose.
    models: list["RunnerModel"] = []


class RunnerModel(BaseModel):
    """Un modèle du catalogue (`runner_models`), et s'il est servi en ce moment."""
    id: str
    label: str
    family: str
    #: Le modèle à PROPOSER : le premier servi dans l'ordre du catalogue. Au plus un
    #: modèle le porte, jamais un modèle non servi — et aucun quand aucun worker ne
    #: déclare de famille (`families: []`) : il faut alors omettre `model`.
    default: bool = False
    served: bool = False


RunnerArme.model_rebuild()


class Delivery(BaseModel):
    """Une livraison reçue par un déclencheur webhook."""
    id: int
    received_at: Optional[str] = None
    #: `queued` | `delayed` (lissé) | `refused_paused` | `refused_secret` |
    #: `refused_too_large`. Un refus garde son MOTIF : c'est lui qui rend une
    #: source mal branchée réparable plutôt que mystérieuse.
    outcome: Optional[str] = None
    job_id: Optional[int] = None
    source: Optional[str] = None


class TriggerOut(BaseModel):
    trigger: Optional[Trigger] = None
    triggers: Optional[list[Trigger]] = None
    ok: Optional[bool] = None
    runner: Optional[RunnerArme] = None
    deliveries: Optional[list[Delivery]] = None
    #: Combien de travaux `clear_queue` a périmés. `0` est un vrai zéro (la file
    #: était vide), jamais une absence de mesure.
    cleared: Optional[int] = None
    #: Le secret en clair — rendu par `create` d'un webhook et par
    #: `rotate_secret`, et par RIEN D'AUTRE. Il n'est pas stocké : seul son haché
    #: l'est. Perdu, il se remplace ; il ne se relit jamais.
    hook_secret: Optional[str] = None


def _avec_pertes(org_id: int, t: dict) -> dict:
    """Le déclencheur, augmenté de ce qu'il a perdu.

    ⚠️ Un déclencheur ne porte pas en lui-même la trace de ses occurrences
    perdues — elles vivent dans la file, que personne ne lit. **Une perte que
    seule une requête manuelle révèle n'est pas une perte connue** : les
    quarante-et-une occurrences de treize jours ont été découvertes par hasard,
    en préparant autre chose. Servi ici, l'écart se voit là où on le cherche.
    """
    return {**t, **db.comptage_perime(org_id, t["id"])}


def _exige_un_runner(org_id: int) -> dict:
    """Refuse de PROMETTRE une exécution que personne n'assure — et rend l'état lu,
    pour que la garde du modèle (`_modele.exige_servi`) juge sur la même lecture.

    ⚠️ La garde suit le VERBE, pas l'objet — le motif que `runner_fleets` a
    établi pour `launch`/`stop`. Poser un déclencheur (ou en rallumer un) est le
    geste qui MENT : il rend un `next_due`, que l'agent rapporte comme une
    promesse tenue. Lire, corriger et supprimer restent ouverts, précisément
    parce que c'est ce dont a besoin quelqu'un qui découvre un déclencheur mort.

    Fermer `create` derrière la présence d'un worker n'ôte donc rien à personne :
    ce qui existe reste gérable, et ce qui n'aurait jamais tourné ne se crée
    plus en silence."""
    etat = db.runner_arme(org_id)
    if etat["armed"]:
        return etat
    if etat["last_seen"] is None:
        detail = ("aucun worker n'a jamais sondé la file de cette org : rien "
                  "n'exécuterait ce déclencheur")
    else:
        detail = (f"le dernier worker de cette org s'est tu le "
                  f"{etat['last_seen']} — au-delà de "
                  f"{db.ARME_FENETRE_S // 60} minutes on ne le tient plus pour "
                  f"présent")
    raise AuthzDenied(
        400, "no_runner_armed",
        f"aucun runner armé pour cette org ({detail}). Le tick ENFILE un job à "
        "chaque échéance ; l'exécution appartient au worker, et sans worker le "
        "job resterait `pending` pour toujours, sans erreur — le déclencheur "
        "aurait l'air de marcher. Arme un worker pour cette org "
        "(`OTO_RUNNER_ARMED=1` + un jeton de l'org, cf. otomata-tech/oto-runner), "
        "puis repose le déclencheur. Lecture, modification et suppression des "
        "déclencheurs existants restent ouvertes.")


def _outils_de_la_procedure(ctx: ResolvedCtx, slug: str) -> list[str]:
    """L'allowlist DÉDUITE de la procédure — les outils qu'elle cite.

    ⚠️ Tranché le 03/09 : *la liste d'outils se déduit de la procédure*. Sans ça,
    le bouton « en faire un agent programmé » demanderait une liste d'outils à
    l'utilisateur — **c'est-à-dire ne serait pas un bouton**.

    La procédure cite ses outils par marqueur (`<tool:nom>`), et c'est déjà ce
    que lit le compteur « référencé par N guides ». On ne devine donc rien : on
    lit ce que l'auteur a écrit.

    ⚠️ Rendue VIDE si la procédure n'en cite aucun — l'appelant décide quoi en
    faire. Rendre un défaut ici inventerait une allowlist que personne n'a
    déclarée, et une allowlist trop large est exactement ce qu'elle existe pour
    empêcher.
    """
    g = db.get_guide_db("org", str(ctx.org_id), slug)
    if not g:
        return []
    return tool_registry.ref_names(g.get("body_md") or "")


_REGLAGES_WEBHOOK = ("payload_mode", "payload_fields", "max_per_hour",
                     "freshness_seconds")


def _valide_le_webhook(inp: TriggerInput, actuel: Optional[dict] = None) -> None:
    """Les réglages du webhook, refusés à la POSE plutôt qu'ignorés en silence —
    à la création (`actuel` absent) comme à la retouche (`actuel` = l'état
    stocké, avec lequel l'entrée se FUSIONNE avant d'être jugée).

    ⚠️ Un réglage accepté puis inerte est le défaut que ce dépôt a payé plusieurs
    fois (`provider`/`model` d'une flotte, servis et ignorés pendant deux
    semaines) : on le croit posé, et le seul endroit où l'écart se voit est le
    comportement qu'on n'obtient pas. Ce lot l'a REFAIT une fois : les quatre
    réglages étaient acceptés par `update` et jamais écrits — relevé à la revue
    d'avant déploiement, pas par un banc.

    ⚠️ Jugé sur les valeurs EFFECTIVES, jamais sur la seule entrée : poser
    `payload_mode=fields` sur un agent qui a déjà ses `payload_fields` est valide,
    et poser `payload_fields` seul sur un agent déjà en `fields` aussi. Juger
    l'entrée isolée refuserait les deux.
    """
    genre = (actuel.get("kind") if actuel else inp.kind) or "schedule"
    if genre != "webhook":
        # Les réglages du webhook sur un déclencheur programmé ne s'appliqueraient
        # à rien. Les refuser NOMME l'erreur au lieu de la laisser dormir.
        poses = [c for c in _REGLAGES_WEBHOOK if getattr(inp, c) is not None]
        if poses:
            raise AuthzDenied(
                400, "not_a_webhook",
                f"{', '.join(poses)} ne s'applique qu'à un déclencheur `webhook` — "
                "un agent programmé n'a pas de corps reçu ni de source à lisser.")
        return
    stocke = actuel or {}
    mode = inp.payload_mode or stocke.get("payload_mode") or runner_hook.IGNORE
    champs = (inp.payload_fields if inp.payload_fields is not None
              else stocke.get("payload_fields"))
    if mode == runner_hook.FIELDS and not champs:
        raise AuthzDenied(
            400, "missing_fields",
            "`payload_mode=fields` sans `payload_fields` ne transmettrait rien : "
            "nomme ce qu'il faut extraire (`{\"lead_id\": \"$.data.id\"}`), ou "
            "choisis `inline` pour tout joindre.")
    if champs and mode != runner_hook.FIELDS:
        raise AuthzDenied(
            400, "not_a_webhook",
            f"`payload_fields` n'est lu qu'en `payload_mode=fields` (effectif : "
            f"`{mode}`) — posé ici, il serait inerte.")
    for champ, valeur in (("max_per_hour", inp.max_per_hour),
                          ("freshness_seconds", inp.freshness_seconds)):
        # `freshness_seconds=0` est une VALEUR (« ne périme jamais »), pas une
        # absence — d'où le test sur le signe et non sur la véracité.
        if valeur is not None and valeur < (1 if champ == "max_per_hour" else 0):
            raise AuthzDenied(400, "invalid_bound",
                              f"`{champ}`={valeur} : un débit se compte (≥ 1), et "
                              "une fraîcheur est une durée (≥ 0, `0` = jamais).")


def _avec_hook(org_id: int, t: dict) -> dict:
    """Le déclencheur, augmenté de ce qu'un écran de webhook doit lire : son URL,
    et ce qu'il a reçu. Jamais le secret, ni son haché.

    ⚠️ L'URL est composée ICI et pas côté client : elle se construit sur
    `OTO_MCP_PUBLIC_URL` — ce que CE process annonce de lui-même (`mcp.oto.cx` en
    prod, `mcp.oto.ninja` en preprod). Un front qui la fabriquerait la fabriquerait
    avec le domaine qu'il connaît, et une source branchée sur la preprod depuis un
    écran de prod n'échouerait qu'au premier événement.

    ⚠️ Sans la variable (dev, tests), l'URL est rendue RELATIVE plutôt qu'inventée :
    un domaine deviné serait une adresse qui ne répond pas, donnée avec l'assurance
    d'une adresse juste.
    """
    if (t.get("kind") or "schedule") != "webhook":
        return t
    import os
    base = (os.environ.get("OTO_MCP_PUBLIC_URL") or "").rstrip("/")
    compte = db.comptage_livraisons(t["id"], org_id)
    return {**t,
            "hook_url": f"{base}/api/hooks/{t['id']}",
            "deliveries_24h": compte["recues_24h"],
            "deliveries_refused_24h": compte["refusees_24h"],
            "last_delivery": str(compte["derniere"]) if compte["derniere"] else None}


def _noms_canoniques(ctx: ResolvedCtx, inp: TriggerInput) -> TriggerInput:
    """La déclaration aux noms d'outils CANONIQUES, avant tout le reste (`tool_alias`).

    L'agent d'un tenant passe `tools` et écrit `input` avec les noms qu'il voit
    (`acme_doc`) ; le worker est servi en canonique et confronte l'allowlist EXACTEMENT.
    Stockée telle quelle, elle ne désignait plus rien et l'agent programmé tournait sans
    outils. Une allowlist DÉDUITE de la procédure est déjà canonique : la procédure
    s'écrit ainsi (`orgs.instructions`)."""
    maj: dict[str, Any] = {}
    if inp.tools is not None:
        maj["tools"] = tool_alias.canonical_names(inp.tools, ctx.sub)
    if inp.input:
        maj["input"] = tool_alias.canonical_prose(inp.input, ctx.sub)
    return inp.model_copy(update=maj) if maj else inp


def _triggers(ctx: ResolvedCtx, inp: TriggerInput) -> dict:
    if not ctx.org_id:
        raise AuthzDenied(400, "org_required", "les déclencheurs sont org-scopés")
    inp = _noms_canoniques(ctx, inp)

    if inp.op == "create":
        webhook = (inp.kind or "schedule") == "webhook"
        if webhook and not access.has_option(ctx.sub, tool_visibility.BETA_OPTION):
            # ⚠️ **Le lot ATTERRIT FERMÉ** (13/09/2026), et c'est la condition de
            # son déploiement. `oto_trigger` est visible de tous (tranché le
            # 02/09) et la capacité est ouverte à tout membre d'org : sans cette
            # porte, le jour du déploiement, n'importe quel client pourrait
            # brancher une source bavarde sur un agent hébergé. Or **la file d'un
            # webhook n'a pas de plafond** (assumé) et **le plafond de DÉPENSE est
            # un autre chantier** : tant que `runner.org_key_required` n'est pas
            # posé, ces déroulés tournent sur NOTRE clé de modèle.
            #
            # L'option `beta` plutôt qu'un réglage neuf : c'est ce que ce dépôt
            # dit de faire (« une seconde surface bêta la rejoindra ici plutôt que
            # d'inventer sa propre option », `tool_visibility`), elle se pose déjà
            # par `oto_admin_set_option` sur un compte ou sur une org, et elle se
            # lit par le seam unique `access.has_option`.
            #
            # ⚠️ Seule la CRÉATION est gardée. Retirer l'option ne doit pas casser
            # un agent qui tourne : le geste d'arrêt d'un agent emballé est sa
            # PAUSE, pas la fermeture de la population.
            raise AuthzDenied(
                403, "webhook_beta_only",
                "les agents déclenchés par webhook sont en bêta fermée : cette "
                "organisation n'y est pas encore. Un agent PROGRAMMÉ (`cron`) "
                "reste disponible. Pour rejoindre la bêta, demande l'option "
                "`beta` sur l'organisation.")
        # ⚠️ Ce qu'on exige dépend du COUP D'ENVOI. Un déclencheur programmé exige
        # son cadencement ; un webhook n'en a pas — exiger `cron` de lui, ou
        # l'accepter en l'ignorant, seraient deux façons de mentir sur ce qu'il est.
        requis = ("procedure",) if webhook else ("procedure", "cron")
        manquants = [c for c in requis if not getattr(inp, c)]
        if manquants:
            raise AuthzDenied(400, "missing_fields",
                              f"create exige : {', '.join(manquants)} — la procédure à "
                              "jouer" + ("" if webhook else ", et quand"))
        if webhook and inp.cron:
            raise AuthzDenied(
                400, "invalid_schedule",
                "un déclencheur `webhook` n'a pas de cadencement : c'est la source "
                "qui décide quand. Retire `cron`, ou déclare un agent programmé.")
        _valide_le_webhook(inp)
        # ⚠️ `tools` n'est plus exigé : il se DÉDUIT de la procédure quand il
        # n'est pas fourni. C'est ce qui rend le geste possible depuis un bouton.
        outils = list(inp.tools or []) or _outils_de_la_procedure(ctx, inp.procedure)
        if not outils:
            raise AuthzDenied(
                400, "no_tools",
                f"la procédure `{inp.procedure}` ne cite aucun outil, et aucun n'a "
                "été fourni. Un agent sans outil n'exécute rien : cite les outils "
                "dans la procédure (marqueurs `<tool:nom>`), ou passe `tools` "
                "explicitement.")
        tz = inp.tz or _TZ_DEFAUT
        if not webhook:
            try:
                runner_tick.validate_cron(inp.cron, tz)
            except ValueError as e:
                raise AuthzDenied(400, "invalid_schedule", str(e))
        # Un modèle inconnu se corrige dans l'appel, comme un cron : il se juge
        # avec lui, avant la présence du runner.
        famille = _modele.famille_declaree(inp.model)
        # Après la validation du cadencement, avant l'écriture : un cron fautif
        # se corrige, une org sans runner appelle un autre geste — les deux
        # refus ne se remplacent pas, et celui qu'on lit d'abord est celui qu'on
        # peut réparer sans quitter l'appel.
        etat = _exige_un_runner(ctx.org_id)
        # Un runner armé ne sert pas forcément CE modèle : son travail attendrait
        # un worker de la bonne famille, puis périmerait.
        _modele.exige_servi(etat, famille)
        # Un runner armé ne suffit pas quand l'org doit tourner sur SA clé : sans
        # elle, le travail serait arrêté à la réservation. Le dire ICI, au moment où
        # l'on peut encore la déposer, plutôt qu'à la première occurrence. Seule la
        # famille DE CE MODÈLE compte (14/09/2026) — pas toutes celles exigées.
        _cle_exigee.exiger_a_la_pose(ctx.org_id, famille)
        # ⚠️ UN SEUL agent programmé par objet (tranché le 03/09). L'agent est une
        # PROPRIÉTÉ de la procédure, pas une collection : deux agents sur le même
        # objet, c'est deux réponses à « est-ce que ça tourne ? », et l'écran
        # devrait en choisir une. Le refus dit lequel existe, pour qu'on puisse
        # le modifier plutôt que d'en créer un second.
        # ⚠️ UN SEUL agent PAR COUP D'ENVOI et par objet. La règle du 03/09 tenait
        # « un objet ne porte qu'un agent » quand il n'existait qu'une façon de le
        # déclencher ; elle devient « un programmé ET un déclenché », parce qu'une
        # veille du matin et une réaction à un événement sont deux automatisations
        # différentes de la même procédure — pas deux réponses à la même question.
        # Deux du MÊME genre restent refusées, pour la raison d'origine.
        genre = "webhook" if webhook else "schedule"
        deja = [d for d in db.triggers_for_procedure(ctx.org_id, inp.procedure)
                if (d.get("kind") or "schedule") == genre]
        if deja:
            quoi = ("déjà un agent déclenché par webhook" if webhook else
                    f"déjà un agent programmé (cadencement `{deja[0]['cron']}`)")
            raise AuthzDenied(
                409, "already_scheduled",
                f"`{inp.procedure}` a {quoi} (#{deja[0]['id']}). Modifie-le plutôt "
                "que d'en créer un second — un objet ne porte qu'un agent de "
                "chaque genre.")
        secret = hache = None
        if webhook:
            secret, hache = runner_hook.nouveau_secret()
        t = db.create_trigger(
            ctx.org_id, ctx.sub, procedure=inp.procedure,
            cron=inp.cron or None, tz=tz,
            next_due=runner_tick.next_due(inp.cron, tz) if not webhook else None,
            tools=outils, project_id=inp.project_id,
            input=inp.input or _instruction.derivee(inp.procedure),
            label=inp.label, max_steps=inp.max_steps,
            # ⚠️ Sans modèle, on n'écrit PAS le défaut du catalogue : NULL veut
            # dire « n'importe quel worker, sur le sien ». Écrire le défaut
            # refuserait la création dans une org servie par une autre famille.
            model=inp.model or None,
            kind=genre,
            payload_mode=inp.payload_mode or runner_hook.IGNORE,
            payload_fields=inp.payload_fields,
            max_per_hour=inp.max_per_hour,
            fraicheur_s=inp.freshness_seconds)
        if webhook:
            db.poser_secret_de_hook(t["id"], ctx.org_id, hache)
            # Le secret en CLAIR, une seule fois. Il n'est pas stocké — seul son
            # haché l'est — donc ni une relecture ni un incident ne le rendront.
            return {"trigger": _avec_hook(ctx.org_id, t), "hook_secret": secret}
        return {"trigger": t}

    if inp.op == "list":
        # ⚠️ Filtré par OBJET quand `procedure` est fourni : l'écran d'une
        # procédure demande « celle-ci tourne-t-elle ? », pas la liste de l'org.
        # Filtrer côté client devient faux dès qu'il y a plus d'une page.
        lus = (db.triggers_for_procedure(ctx.org_id, inp.procedure) if inp.procedure
               else db.list_triggers(ctx.org_id))
        return {"triggers": [_avec_hook(ctx.org_id, _avec_pertes(ctx.org_id, t))
                             for t in lus],
                "runner": _modele.etat_servi(db.runner_arme(ctx.org_id), ctx.org_id)}

    if inp.trigger_id is None:
        raise AuthzDenied(400, "missing_fields", f"{inp.op} exige `trigger_id`")

    if inp.op == "get":
        t = db.get_trigger(inp.trigger_id, ctx.org_id)
        if not t:
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        return {"trigger": _avec_hook(ctx.org_id, _avec_pertes(ctx.org_id, t)),
                "runner": _modele.etat_servi(db.runner_arme(ctx.org_id), ctx.org_id)}

    if inp.op == "rotate_secret":
        t = db.get_trigger(inp.trigger_id, ctx.org_id)
        if not t or (t.get("kind") or "schedule") != "webhook":
            # Même 404 qu'un déclencheur inconnu : un agent programmé n'a pas de
            # secret, et le dire distinguerait « n'existe pas » de « pas le bon
            # genre » pour un appelant qui n'a pas à le savoir.
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        secret, hache = runner_hook.nouveau_secret()
        db.poser_secret_de_hook(inp.trigger_id, ctx.org_id, hache)
        logger.warning("secret de webhook RENOUVELÉ pour le déclencheur %s (org %s) "
                       "par %s — la source en place cessera d'être acceptée",
                       inp.trigger_id, ctx.org_id, ctx.sub)
        return {"trigger": _avec_hook(ctx.org_id,
                                      db.get_trigger(inp.trigger_id, ctx.org_id)),
                "hook_secret": secret}

    if inp.op == "deliveries":
        # Org-scopé par la requête : un déclencheur d'une autre org rend une liste
        # vide, jamais les livraisons d'autrui.
        return {"deliveries": db.livraisons(inp.trigger_id, ctx.org_id,
                                            limit=inp.limit or 50)}

    if inp.op == "clear_queue":
        # ⚠️ Disponible À TOUT MOMENT, en marche comme en pause — c'est tout
        # l'intérêt. En pause, c'est même le cas le plus courant : on arrête
        # l'agent qui s'emballe, puis on décide de jeter ce qu'il a accumulé.
        # Les travaux RETENUS par la pause sont donc périmés eux aussi
        # (`perimer_travaux_du_declencheur` couvre `pending` ET `held`).
        t = db.get_trigger(inp.trigger_id, ctx.org_id)
        if not t:
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        vides = db.perimer_travaux_du_declencheur(
            inp.trigger_id, ctx.org_id,
            raison="file vidée à la demande : ces occurrences n'ont jamais été "
                   "exécutées et ne le seront pas.")
        # Et les créneaux avec, sinon les livraisons suivantes attendraient
        # derrière une file qui n'existe plus.
        db.liberer_les_creneaux(inp.trigger_id)
        logger.info("déclencheur %s (org %s) : file VIDÉE à la demande de %s — "
                    "%d travaux périmés", inp.trigger_id, ctx.org_id, ctx.sub, vides)
        return {"ok": True, "cleared": vides,
                "trigger": _avec_hook(ctx.org_id,
                                      db.get_trigger(inp.trigger_id, ctx.org_id))}

    if inp.op == "delete":
        if not db.delete_trigger(inp.trigger_id, ctx.org_id):
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        return {"ok": True}

    # update — partiel ; toute retouche du cadencement (cron OU tz) revalide et
    # recalcule l'échéance avec les valeurs EFFECTIVES (jamais l'une sans l'autre).
    champs: dict[str, Any] = {}
    for c in ("procedure", "tools", "project_id", "input", "label",
              "max_steps", "enabled"):
        v = getattr(inp, c)
        if v is not None:
            champs[c] = v
    famille = None
    if inp.model is not None:
        famille = _modele.famille_declaree(inp.model)
        champs["model"] = inp.model or None

    # ⚠️ Le déclencheur se lit PARESSEUSEMENT, et seulement là où son GENRE ou son
    # état décident : l'ordre des refus est un contrat (« aucun runner » avant
    # « inconnu » sur un rallumage nu — voir plus bas), et un banc le tient en
    # ne doublant PAS `get_trigger` sur les gestes qui n'ont pas à lire.
    lu: dict[str, Any] = {}

    def _actuel() -> Optional[dict]:
        if "t" not in lu:
            lu["t"] = db.get_trigger(inp.trigger_id, ctx.org_id)
        return lu["t"]

    def _est_webhook() -> bool:
        return ((_actuel() or {}).get("kind") or "schedule") == "webhook"

    if any(getattr(inp, c) is not None for c in _REGLAGES_WEBHOOK):
        if not _actuel():
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        # Jugés FUSIONNÉS avec l'état stocké, puis ÉCRITS. Avant ce lot ils
        # étaient acceptés et jamais écrits — un réglage inerte de plus.
        _valide_le_webhook(inp, _actuel())
        for c, col in (("payload_mode", "payload_mode"),
                       ("payload_fields", "payload_fields"),
                       ("max_per_hour", "max_per_hour"),
                       ("freshness_seconds", "fraicheur_s")):
            v = getattr(inp, c)
            if v is not None:
                champs[col] = v
    if inp.cron is not None or inp.tz is not None:
        if not _actuel():
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        if _est_webhook():
            # Sans cette garde, un `cron` posé sur un webhook lui donnait une
            # échéance — et il partait à l'HORLOGE en plus de l'événement ; un
            # `tz` seul faisait valider un cron NULL et rendait 500.
            raise AuthzDenied(
                400, "invalid_schedule",
                "un déclencheur `webhook` n'a pas de cadencement : c'est la source "
                "qui décide quand. `cron` et `tz` ne s'y retouchent pas.")
        actuel = _actuel()
        cron = inp.cron if inp.cron is not None else actuel["cron"]
        tz = inp.tz if inp.tz is not None else actuel["tz"]
        try:
            runner_tick.validate_cron(cron, tz)
        except ValueError as e:
            raise AuthzDenied(400, "invalid_schedule", str(e))
        champs.update(cron=cron, tz=tz, next_due=runner_tick.next_due(cron, tz))
    # Rallumer, c'est promettre à nouveau : même geste, même garde. Éteindre,
    # renommer ou corriger un cron ne promet rien et passe toujours — sinon un
    # déclencheur mort deviendrait impossible à ranger.
    if champs.get("enabled") is True:
        etat = _exige_un_runner(ctx.org_id)
        # ⚠️ Lu APRÈS `_exige_un_runner`, jamais avant : l'ordre des refus est un
        # contrat. Lire le déclencheur d'abord ferait répondre « inconnu » (404)
        # là où le serveur répond aujourd'hui « aucun runner » — deux diagnostics
        # opposés pour la même org, et celui qu'on retirerait est le seul qui dit
        # quoi faire.
        actuel = _actuel()
        # La famille EFFECTIVE de ce rallumage : celle posée DANS cet appel s'il
        # y en a une, sinon celle déjà en base — c'est elle, et seulement elle,
        # que la clé exigée (14/09/2026) et `exige_servi` doivent juger, jamais
        # toutes les familles exigées à la fois.
        famille_pose = (famille if inp.model is not None
                        else runner_models.famille((actuel or {}).get("model")))
        _cle_exigee.exiger_a_la_pose(ctx.org_id, famille_pose)
        # ⚠️ **RALLUMER REPREND LE RYTHME, ça ne rembobine pas** (arbitré le
        # 02/09, #826). Une échéance figée pendant l'extinction est restée dans
        # le PASSÉ : sans ce recalcul, le tick voyait le déclencheur dû à la
        # seconde du rallumage et enfilait aussitôt — une exécution que personne
        # n'a demandée, déclenchée par le geste de quelqu'un qui répare.
        #
        # ⚠️ Et la cohérence l'impose, pas seulement le confort : éteindre PÉRIME
        # les occurrences en attente. *Un système qui dit « ce qui a attendu
        # pendant l'extinction est mort » ne peut pas dire « sauf l'échéance ».*
        # Une échéance manquée pendant une extinction VOULUE n'a pas été manquée.
        #
        # ⚠️ Seul le PASSAGE à allumé recalcule — même motif que la péremption,
        # qui ne mord qu'au passage à éteint. Recalculer sur un déclencheur déjà
        # allumé donnerait un moyen de repousser son échéance indéfiniment, en
        # répétant un geste qui n'est pas censé rien changer.
        #
        # ⚠️ Un WEBHOOK rallumé n'a pas d'échéance à reprendre : il repart à la
        # prochaine livraison. Recalculer ici sur un `cron` NULL rendait 500 sur
        # le geste le plus ordinaire qui soit — remettre en marche.
        if (actuel and not actuel["enabled"] and "next_due" not in champs
                and not _est_webhook()):
            champs["next_due"] = runner_tick.next_due(actuel["cron"], actuel["tz"])
        # Rallumer promet aussi un MODÈLE : celui qu'on pose dans cet appel, sinon
        # celui qui est stocké. Un déclencheur éteint pendant qu'une famille
        # disparaissait ne doit pas se rallumer sur une promesse morte.
        _modele.exige_servi(etat, famille_pose)
    elif famille and inp.enabled is None:
        # Changer le modèle d'un déclencheur ALLUMÉ, c'est promettre ce modèle dès
        # l'occurrence suivante. Éteint, rien n'est promis : la retouche passe.
        actuel = _actuel()
        if not actuel:
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        if actuel["enabled"]:
            _modele.exige_servi(db.runner_arme(ctx.org_id), famille)
    t = db.update_trigger(inp.trigger_id, ctx.org_id, champs)
    if not t:
        raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
    return {"trigger": _avec_hook(ctx.org_id, t)}


CAPABILITIES += [
    Capability(
        key="runner.triggers",
        handler=_triggers,
        Input=TriggerInput,
        Output=TriggerOut,
        authz=ORG_MEMBER,
        mcp="oto_trigger",
        # Les refus PUBLIÉS — un dashboard doit pouvoir GRISER « nouveau
        # déclencheur » et dire pourquoi, plutôt que laisser tenter un geste qui
        # sera refusé (le motif de `runner_fleets`).
        errors=(
            DeclaredError(400, "missing_fields",
                          "`create` sans `procedure`/`cron`/`tools`, ou une "
                          "opération sur un déclencheur sans `trigger_id`"),
            DeclaredError(403, "webhook_beta_only",
                          "`create` d'un agent déclenché par webhook, hors de la "
                          "population bêta — un écran peut griser l'option et "
                          "dire pourquoi, plutôt que laisser tenter le geste"),
            DeclaredError(400, "invalid_schedule",
                          "cron malformé, fuseau inconnu, ou deux occurrences "
                          "espacées de moins de 5 minutes"),
            DeclaredError(400, "no_runner_armed",
                          "aucun worker ne sonde la file de cette org : "
                          "`create`, et `update enabled=true`, sont refusés "
                          "plutôt que de promettre une exécution qui n'aurait "
                          "pas lieu"),
            DeclaredError(400, "invalid_model",
                          "`model` hors du catalogue servi (`runner.models` sur "
                          "`list`/`get`)"),
            DeclaredError(400, "model_not_served",
                          "`model` d'une famille qu'aucun worker vivant ne sert : "
                          "`create`, `update enabled=true` et le changement de "
                          "modèle d'un déclencheur allumé sont refusés"),
            DeclaredError(400, "model_key_required",
                          "l'org doit faire tourner ses agents sur SA clé de modèle "
                          "et ne l'a pas déposée : `create` et `update enabled=true` "
                          "sont refusés"),
            DeclaredError(404, "trigger_not_found",
                          "déclencheur inconnu dans l'org du porteur"),
        ),
        rest=RestBinding(verb="POST", path="/api/me/runner/triggers"),
        description=(
            "Scheduled triggers for hosted runs — the product's /schedule. op=create "
            "(procedure slug + `cron` + `tools` allowlist ; `tz` defaults to "
            "Europe/Paris and the cron evaluates IN that timezone — say WHICH 8am "
            "you mean) / list / get / update (editing cron or tz revalidates and "
            "recomputes the next due) / delete. The tick only ENQUEUES a job at "
            "each due time; execution belongs to the worker. Floor between two "
            "occurrences: 5 minutes — a run is not a ping. `create` (and "
            "`update enabled=true`) is REFUSED when no worker polls this org's "
            "queue — a trigger nothing executes would enqueue forever without an "
            "error; `list`/`get` carry `runner` (armed, workers, last_seen) so an "
            "existing trigger can be told apart from a live one. `model` "
            "(optional) is the model the agent runs on, one of `runner.models` — "
            "each flagged `served`; omitted, the worker that takes the job runs its "
            "own. The model flagged `default` is the one to propose: the first "
            "served model in catalogue order. No model is flagged when no live "
            "worker declares a family (`runner.families` is `[]`) — then omit "
            "`model`. A model no live worker serves is REFUSED (`model_not_served`) on "
            "create, on enable, and when changed on an enabled trigger: its job "
            "would wait for a worker of that family and expire. ⚠️ An occurrence "
            "nobody claimed BEFORE the next one is due is EXPIRED, not silently "
            "kept: a daily watch run thirteen days late does not return a late "
            "result, it returns a WRONG one — and a backlog released all at once "
            "would run with the procedure and context of its era. Expiry never "
            "deletes: `list`/`get` carry `expired_count` (a real 0, not a missing "
            "measure) plus `expired_since` and `expired_last` — since when, and "
            "whether it is STILL happening, are two different questions. A rising "
            "count on an enabled trigger means nobody is executing this org."
        ),
    ),
]
