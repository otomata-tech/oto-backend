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

from typing import Any, Literal, Optional

from pydantic import BaseModel

from . import _cle_exigee, _instruction, _modele
from .. import db, runner_models, runner_tick, tool_registry
from ._authz import ORG_MEMBER
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                     RestBinding)
from .registry import CAPABILITIES

_TZ_DEFAUT = "Europe/Paris"


class TriggerInput(BaseModel):
    op: Literal["create", "list", "get", "update", "delete"]
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


class TriggerOut(BaseModel):
    trigger: Optional[Trigger] = None
    triggers: Optional[list[Trigger]] = None
    ok: Optional[bool] = None
    runner: Optional[RunnerArme] = None


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


def _triggers(ctx: ResolvedCtx, inp: TriggerInput) -> dict:
    if not ctx.org_id:
        raise AuthzDenied(400, "org_required", "les déclencheurs sont org-scopés")

    if inp.op == "create":
        manquants = [c for c in ("procedure", "cron") if not getattr(inp, c)]
        if manquants:
            raise AuthzDenied(400, "missing_fields",
                              f"create exige : {', '.join(manquants)} — la procédure à "
                              "jouer, et quand")
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
        # l'on peut encore la déposer, plutôt qu'à la première occurrence.
        _cle_exigee.exiger_a_la_pose(ctx.org_id)
        # ⚠️ UN SEUL agent programmé par objet (tranché le 03/09). L'agent est une
        # PROPRIÉTÉ de la procédure, pas une collection : deux agents sur le même
        # objet, c'est deux réponses à « est-ce que ça tourne ? », et l'écran
        # devrait en choisir une. Le refus dit lequel existe, pour qu'on puisse
        # le modifier plutôt que d'en créer un second.
        deja = db.triggers_for_procedure(ctx.org_id, inp.procedure)
        if deja:
            raise AuthzDenied(
                409, "already_scheduled",
                f"`{inp.procedure}` a déjà un agent programmé (#{deja[0]['id']}, "
                f"cadencement `{deja[0]['cron']}`). Modifie-le plutôt que d'en "
                "créer un second — un objet ne porte qu'un agent.")
        t = db.create_trigger(
            ctx.org_id, ctx.sub, procedure=inp.procedure, cron=inp.cron, tz=tz,
            next_due=runner_tick.next_due(inp.cron, tz), tools=outils,
            project_id=inp.project_id,
            input=inp.input or _instruction.derivee(inp.procedure),
            label=inp.label, max_steps=inp.max_steps,
            # ⚠️ Sans modèle, on n'écrit PAS le défaut du catalogue : NULL veut
            # dire « n'importe quel worker, sur le sien ». Écrire le défaut
            # refuserait la création dans une org servie par une autre famille.
            model=inp.model or None)
        return {"trigger": t}

    if inp.op == "list":
        # ⚠️ Filtré par OBJET quand `procedure` est fourni : l'écran d'une
        # procédure demande « celle-ci tourne-t-elle ? », pas la liste de l'org.
        # Filtrer côté client devient faux dès qu'il y a plus d'une page.
        lus = (db.triggers_for_procedure(ctx.org_id, inp.procedure) if inp.procedure
               else db.list_triggers(ctx.org_id))
        return {"triggers": [_avec_pertes(ctx.org_id, t) for t in lus],
                "runner": _modele.etat_servi(db.runner_arme(ctx.org_id))}

    if inp.trigger_id is None:
        raise AuthzDenied(400, "missing_fields", f"{inp.op} exige `trigger_id`")

    if inp.op == "get":
        t = db.get_trigger(inp.trigger_id, ctx.org_id)
        if not t:
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        return {"trigger": _avec_pertes(ctx.org_id, t),
                "runner": _modele.etat_servi(db.runner_arme(ctx.org_id))}

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
    actuel = None
    if inp.cron is not None or inp.tz is not None:
        actuel = db.get_trigger(inp.trigger_id, ctx.org_id)
        if not actuel:
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
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
        _cle_exigee.exiger_a_la_pose(ctx.org_id)
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
        # ⚠️ Lu APRÈS `_exige_un_runner`, jamais avant : l'ordre des refus est un
        # contrat. Lire le déclencheur d'abord ferait répondre « inconnu » (404)
        # là où le serveur répond aujourd'hui « aucun runner » — deux diagnostics
        # opposés pour la même org, et celui qu'on retirerait est le seul qui dit
        # quoi faire.
        if actuel is None:
            actuel = db.get_trigger(inp.trigger_id, ctx.org_id)
        if actuel and not actuel["enabled"] and "next_due" not in champs:
            champs["next_due"] = runner_tick.next_due(actuel["cron"], actuel["tz"])
        # Rallumer promet aussi un MODÈLE : celui qu'on pose dans cet appel, sinon
        # celui qui est stocké. Un déclencheur éteint pendant qu'une famille
        # disparaissait ne doit pas se rallumer sur une promesse morte.
        _modele.exige_servi(etat, famille if inp.model is not None
                            else runner_models.famille((actuel or {}).get("model")))
    elif famille and inp.enabled is None:
        # Changer le modèle d'un déclencheur ALLUMÉ, c'est promettre ce modèle dès
        # l'occurrence suivante. Éteint, rien n'est promis : la retouche passe.
        if actuel is None:
            actuel = db.get_trigger(inp.trigger_id, ctx.org_id)
        if not actuel:
            raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
        if actuel["enabled"]:
            _modele.exige_servi(db.runner_arme(ctx.org_id), famille)
    t = db.update_trigger(inp.trigger_id, ctx.org_id, champs)
    if not t:
        raise AuthzDenied(404, "trigger_not_found", "déclencheur inconnu")
    return {"trigger": t}


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
