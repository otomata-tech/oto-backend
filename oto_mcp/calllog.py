"""Journal des appels — middleware MCP inliné (ex-lib `otomata-calllog`) + gestes REST.

Une ligne par `call_tool` reçu : qui (sub du JWT), quel tool, arguments (bornés,
masqués, la coupe déclarée), durée, succès/erreur, taille et forme du résultat. L'écriture part en tâche de fond : zéro latence ajoutée, un
échec de journalisation n'échoue jamais le tool.

Le journal a **un seul domicile** : la table `tool_calls` (`db.insert_tool_call`),
et **un seul module** — ici. `log_rest_call` y ajoute les gestes faits depuis le
dashboard (`kind='rest'`), sous le MÊME vocabulaire de `tool` que la surface MCP
(`data_write`…), pour qu'une lecture de journal voie l'agent ET l'humain.

Historique : lib `otomata-calllog` (extraite d'un projet client 2026-06-12), inlinée ici le
2026-07-23 (otomata-calllog#1) — le backend était son dernier consommateur, et le
**contrat canonique** (schéma de ligne `tool_calls`, dashboards comparables entre
serveurs MCP) vit désormais dans le socle `otomata-mcp` (`logging.py`). Le schéma
local étend ce contrat en OTO-LOCAL : `kind`, `session_id`, `run_id`, `org_id`,
`client_id` (cf. `db/_schema.py`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastmcp.server.middleware import Middleware

from . import geste, journal_secrets
from .db._hors_boucle import HorsBoucle
from .db.journal_calls import ARGS_CLIENT_KEY, ARGS_TRUNCATED_KEY



def apply_call_trace(row: dict, trace: Optional[dict], traced_args: tuple) -> dict:
    """Verse le relevé d'un appel dans sa ligne de journal — UNE seule copie de la
    règle, pour les deux écrivains d'une ligne `tool_calls` : le sink du middleware
    (`server._calllog_sink`) et le traçage de la cible d'`oto_call`
    (`tools/meta._trace_target_call`). `traced_args` est la liste fermée
    `server._TRACED_ARGS`, passée par l'appelant plutôt que recopiée ici.

    - les clés de `traced_args` rejoignent `args` (entités résolues : l'intention de
      l'agent reste à côté, l'id devient la clé de corrélation) ;
    - `quantity` (items TRAITÉS — métrage/facturation) : sa propre colonne, posée si
      c'est un entier `>= 0`. ⚠️ `>= 0`, pas `> 0` : un zéro TRACÉ (recherche vide)
      n'est pas un appel non tracé, que le consommateur lit comme 1 ;
    - `provider_credits` (crédits déduits par le FOURNISSEUR, dans son unité) : sa
      propre colonne, même règle d'entier `>= 0` — à côté de `quantity`, jamais versé
      dedans : les deux grandeurs ne se confondent pas ;
    - `key_mode` (SOUS QUELLE clé : `user|group|org|tenant|platform`, posé au seul
      résolveur) : sa propre colonne si c'est une chaîne non vide. NULL = aucune clé
      résolue → rien à facturer.

    Le reste du relevé (`resolved_connector`, `resolved_account`…) sert l'ÉCHO rendu à
    l'agent, pas le journal : il n'est pas écrit."""
    if not trace:
        return row
    traced = {k: v for k, v in trace.items() if k in traced_args}
    if traced:
        row["args"] = {**(row.get("args") or {}), **traced}
    quantity = trace.get("quantity")
    if isinstance(quantity, int) and quantity >= 0:
        row["quantity"] = quantity
    credits = trace.get("provider_credits")
    if isinstance(credits, int) and credits >= 0:
        row["provider_credits"] = credits
    key_mode = trace.get("key_mode")
    if isinstance(key_mode, str) and key_mode:
        row["key_mode"] = key_mode
    return row

logger = logging.getLogger("oto_mcp.calllog")

def emetteur_declare() -> Optional[dict]:
    """Le logiciel client que la session MCP de l'appel en cours a DÉCLARÉ à son
    `initialize` (`clientInfo` : `{"name", "version"}`), ou `None` hors session MCP.

    Le serveur le reçoit à chaque handshake et la ligne `kind='protocol'` le garde ;
    aucun appel ne le portait (otomata-tech/oto#187). Lu ICI, sur la session servie —
    une lecture d'attribut, aucune base, aucun identifiant fourni par l'appel.

    ⚠️ **Déclaré, donc lisible, jamais opposable** : un client écrit ce qu'il veut dans
    `clientInfo`. Télémétrie par SURFACE (runner, CLI d'agent, client web, script),
    jamais une frontière d'autorisation — rien ne doit s'en servir pour refuser."""
    from fastmcp.server.dependencies import get_context
    try:
        # Un contexte sans session (appel interne, banc) n'a pas de client à nommer.
        session = getattr(get_context(), "session", None)
    except RuntimeError:
        # Hors requête MCP (REST, script), ou session pas encore établie.
        return None
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    nom = getattr(info, "name", None)
    if not nom:
        return None
    return {"name": str(nom)[:MAX_FIELD_CHARS],
            "version": (str(info.version)[:MAX_FIELD_CHARS]
                        if getattr(info, "version", None) else None)}


def poser_emetteur(row: dict) -> dict:
    """Verse l'ÉMETTEUR DÉCLARÉ d'un appel dans sa ligne de journal (otomata-tech/oto#187)
    — UNE règle pour les deux écrivains d'une ligne `kind='mcp'` : le middleware
    (`ToolCallLogger.on_call_tool`) et la cible d'`oto_call` (`tools/meta`).

    - le logiciel client (`emetteur_declare`) sous la clé réservée `args._client`
      (`ARGS_CLIENT_KEY`, exclue des `arg_keys`) : une clé du JSON existant plutôt
      qu'une colonne — le journal a des millions de lignes, et l'émetteur se LIT
      (fiche, liste, export, couverture), il ne se filtre pas sur un chemin chaud ;
    - le jeton NOMMÉ employé (`token_id`, `token_kind` : `user` | `delegation`), déjà
      colonnes, jusqu'ici jamais posées sur la face MCP. Absents = session OAuth
      (aucun jeton nommé : le client porte alors son `client_id`).

    Lectures de contexte seules (session servie, jeton de la requête) : appelable
    depuis la boucle."""
    emetteur = emetteur_declare()
    if emetteur:
        row["args"] = {**(row.get("args") or {}), ARGS_CLIENT_KEY: emetteur}
    from .auth.hooks import current_token_axes
    row.update(current_token_axes())
    return row


# Borne PAR VALEUR d'argument (oto-backend#413). Décision d'Alexis du 06/09/2026 : le
# journal garde les arguments, et c'est l'ACCÈS qui se restreint (#563) — la coupe à
# 300 caractères rendait un `data_write` refusé impossible à qualifier après coup.
# Ce qui reste de borne est un arbitrage de COÛT DE STOCKAGE, pas de confidentialité :
# `tool_calls` porte des millions de lignes. Mesure de 2026-09-01 sur la trace du
# dispatch universel (non bornée alors) : 111 lignes sur 40 159 avaient une valeur
# au-delà de 300, la plus longue à 4 383 — 4 000 couvre presque tout ce qui dépassait,
# sans ouvrir la porte à un corps de plusieurs centaines de kilo-octets par ligne.
# Toute coupe qui reste se DÉCLARE sur la ligne (`ARGS_TRUNCATED_KEY`).
MAX_ARG_CHARS = 4000
MAX_ERROR_CHARS = 500

# `ARGS_TRUNCATED_KEY` (importée de `db.journal_calls`, qui l'exclut des `arg_keys`) :
# la clé réservée d'`args` qui DÉCLARE une coupe — `{"at": <borne>, "sizes":
# {<argument>: <taille réelle>}}`. Absente quand rien n'a été coupé. Elle ne porte que
# des noms d'arguments et des tailles, jamais une valeur.

# Profondeur de traversée du masquage. Deux niveaux suffisent aujourd'hui (le dispatch
# universel en ajoute un, `smtp_imap` en ajoute un autre) ; la borne existe pour qu'une
# charge pathologique ne fasse pas tomber une journalisation qui est best-effort mais
# pas facultative. Au-delà, la valeur est stringifiée puis coupée comme le reste.
MAX_MASK_DEPTH = 6

Sink = Callable[[dict], Awaitable[None]]

# Références fortes sur les écritures en tâche de fond (anti-GC asyncio).
_PENDING: set = set()


def _masque_en_profondeur(valeur: Any, caches, profondeur: int) -> Any:
    """Remplace par une empreinte toute valeur portée par une clé DÉCLARÉE secrète,
    quelle que soit sa profondeur d'imbrication.

    Une déclaration porte sur un NOM de champ, pas sur une place : `smtp_password` est
    un secret qu'il arrive à la racine des arguments, dans le sous-dictionnaire d'un
    outil composite, ou sous l'enveloppe du dispatch universel (`oto_call` range les
    arguments de l'outil visé sous `arguments`, ce qui ajoute un niveau). Ne masquer
    qu'un niveau faisait tenir la déclaration sur le chemin direct et tomber sur le
    dispatch — c'est-à-dire, pour un outil hors du registre servi, sur le seul chemin
    par lequel il est appelé (mesuré le 2026-09-01).
    """
    if profondeur <= 0:
        return valeur
    if isinstance(valeur, dict):
        return {k: (journal_secrets.mask(v) if k in caches and v is not None
                    else _masque_en_profondeur(v, caches, profondeur - 1))
                for k, v in valeur.items()}
    if isinstance(valeur, list):
        return [_masque_en_profondeur(v, caches, profondeur - 1) for v in valeur]
    return valeur


def truncated_args(arguments: dict | None, max_chars: int = MAX_ARG_CHARS,
                   *, tool: str | None = None) -> dict | None:
    """Arguments journalisables : scalaires gardés tels quels, le reste
    stringifié, et coupé au-delà de `max_chars` seulement (#413).

    Une coupe n'est jamais silencieuse : la ligne porte alors
    `args[ARGS_TRUNCATED_KEY] = {"at": max_chars, "sizes": {<argument>: <taille>}}`,
    de quoi lire « tronqué à N, taille réelle M » sur la fiche d'appel.

    `tool` sert le MASQUAGE (#558) : un argument dont le nom est déclaré secret
    pour CET outil (`journal_secrets.secret_arg_names`) part en empreinte, jamais
    en clair. C'est la même propriété que pour les routes, sur l'autre face : le
    jeton d'invitation arrive aussi par `oto_org op=accept_invite`. Sans `tool`,
    rien n'est masqué — un appelant qui ne sait pas de quel outil il parle ne peut
    pas décider, et masquer par le NOM seul cacherait des `code` métier qui n'ont
    rien de secret.
    """
    if not arguments:
        return None
    caches = journal_secrets.secret_arg_names(tool)
    if tool == "oto_call":
        # Dispatch universel (ADR 0036) : les arguments de l'outil VISÉ voyagent
        # dans un sous-dictionnaire. Sans reprendre SA déclaration, `oto_call`
        # rouvrirait le canal qu'on vient de fermer.
        caches = caches | journal_secrets.secret_arg_names(arguments.get("name"))
    # Avant la mise en texte ci-dessous : l'adresse d'une source de fichier se
    # reconnaît à sa STRUCTURE (`{kind: "url", url}`), que `str(v)` effacerait.
    # `db.insert_tool_call` repasse le même masque (idempotent) pour les écrivains
    # qui n'empruntent pas ce chemin.
    arguments = journal_secrets.masquer_urls_des_valeurs(arguments)
    out: dict[str, Any] = {}
    tailles: dict[str, int] = {}
    for k, v in arguments.items():
        if k in caches and v is not None:
            out[k] = journal_secrets.mask(v)
            continue
        if caches:
            v = _masque_en_profondeur(v, caches, MAX_MASK_DEPTH)
        if not (v is None or isinstance(v, (int, float, bool))):
            v = str(v)
            if len(v) > max_chars:
                tailles[k] = len(v)
                v = v[:max_chars] + "…"
        out[k] = v
    if tailles:
        out[ARGS_TRUNCATED_KEY] = {"at": max_chars, "sizes": tailles}
    return out


MAX_FIELDS = 50
MAX_FIELD_CHARS = 64

# Références fortes sur les écritures REST en tâche de fond (anti-GC asyncio).
_REST_PENDING: set = set()


def _fields_list(fields: Any) -> list[str]:
    """Champs touchés par l'écriture, bornés — gardés comme un VRAI tableau JSON
    (`truncated_args` stringifierait la liste, et le journal ne serait plus
    exploitable colonne par colonne)."""
    if not fields:
        return []
    return [str(f)[:MAX_FIELD_CHARS] for f in list(fields)[:MAX_FIELDS]]


def log_rest_call(tool: str, *, sub: str | None, args: dict | None = None,
                  fields: Any = None, forced: Any = None, forced_bilan: Any = None,
                  ok: bool = True,
                  error: str | None = None,
                  org_id: int | None = None, duration_ms: int | None = None) -> None:
    """Journalise un GESTE fait depuis le dashboard (REST) dans le flux unifié.

    Même table, même fonction d'insertion et même discipline que le middleware MCP :
    **best-effort** (l'écriture part en tâche de fond, un échec se log en warning et
    n'échoue JAMAIS la requête métier) et arguments passés à `truncated_args` (bornés
    et masqués, la coupe déclarée).

    `tool` doit nommer le geste dans le vocabulaire de la surface MCP (`data_write`,
    `data_delete_row`, `data_release`) : les lectures du journal (parcours d'une
    ligne, activité d'un tableau) filtrent là-dessus, pas sur la route HTTP.
    `forced` (#658) = les colonnes verrouillées remplacées de force. Comme `fields`,
    il rejoint la ligne APRÈS `truncated_args` et reste un VRAI tableau JSON : passé
    dans `args`, il repartirait stringifié (et coupé au-delà de `MAX_ARG_CHARS`), donc
    illisible colonne par colonne — exactement ce que `_fields_list` existe pour éviter. Absent
    quand rien n'a été forcé : un forçage se cherche par la PRÉSENCE de la clé.

    ⚠️ Distinct de la ligne de route posée par `api.routes.RestCallLogger`
    (`tool='PATCH /api/…'`, dont les `args` ne portent QUE l'empreinte des jetons
    du chemin, #558) : celle-là est de la télémétrie de surface, celle-ci porte le
    SENS du geste (quelle ligne, quels champs, quel état avant/après).
    """
    row: dict[str, Any] = {
        "server": "oto",
        "kind": "rest",
        "sub": sub,
        "tool": tool,
        "args": {**(truncated_args(args, tool=tool) or {}),
                 "fields": _fields_list(fields),
                 **({"readonly_forced": list(forced)} if forced else {}),
                 # Le relevé écrêté se DIT : sans cette clé, le compte d'un lecteur du
                 # journal passerait pour un total (oto#139).
                 **({"readonly_forced_bilan": forced_bilan} if forced_bilan else {})},
        "ok": bool(ok),
        "error": (str(error)[:MAX_ERROR_CHARS] if error else None),
        "org_id": org_id,
        "duration_ms": duration_ms,
    }
    # Le geste de la requête (oto#273) : la ligne de route (`RestCallLogger`) le porte
    # déjà en `call_uid`, celle-ci le porte aussi. C'est elle que lit le parcours d'une
    # ligne, qui y rattache par lui les révisions que la requête a écrites.
    en_cours = geste.courant()
    if en_cours is not None:
        row["call_uid"] = en_cours.geste_id
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Appelé hors event loop : on insère EN DIRECT, même chemin d'insertion — le
        # journal reste best-effort, pas de file d'attente.
        #
        # ⚠️ Ce `try` n'est pas décoratif, et il manquait. Les deux branches promettent
        # la même chose (« un échec se log en warning et n'échoue JAMAIS la requête
        # métier ») mais une seule la tenait : la branche asynchrone est gardée par
        # `_emit_rest`, celle-ci ne l'était que par les gardes INTERNES d'`_insert_rest`
        # — donc pas du tout dès que le sink lui-même défaille. L'asymétrie était
        # invisible tant que ce chemin ne servait qu'en CLI et en test ; depuis que les
        # handlers de capacité tournent en threadpool (incident du 2026-09-01), c'est
        # le chemin de PRODUCTION de toute écriture datastore faite en REST.
        try:
            _insert_rest(row)
        except Exception:  # noqa: BLE001 — le journal ne casse jamais le service
            logger.warning("journalisation rest en échec (%s)", row.get("tool"),
                           exc_info=True)
        return
    task = loop.create_task(_emit_rest(row))
    _REST_PENDING.add(task)
    task.add_done_callback(_REST_PENDING.discard)


async def _emit_rest(row: dict) -> None:
    """Écrit hors event loop (`to_thread` : l'INSERT psycopg est sync)."""
    try:
        await asyncio.to_thread(_insert_rest, row)
    except Exception:  # noqa: BLE001 — le journal ne casse jamais le service
        logger.warning("journalisation rest en échec (%s)", row.get("tool"), exc_info=True)


def _insert_rest(row: dict) -> None:
    """Stampe l'org de l'acteur (scope d'audit, comme le sink MCP) puis insère."""
    try:
        if row.get("org_id") is None and row.get("sub"):
            from . import access
            row["org_id"] = access.current_org(row["sub"])
    except Exception:  # noqa: BLE001 — org indisponible ⇒ ligne non scopée, pas d'échec
        logger.debug("org de journalisation rest indisponible", exc_info=True)
    try:
        from . import db
        db.insert_tool_call(row)
    except Exception:  # noqa: BLE001
        logger.warning("insert tool_call rest en échec (%s)", row.get("tool"), exc_info=True)


def _error_kind(exc: BaseException) -> Optional[str]:
    """Résultat de la taxonomie (`error_taxonomy.classify(exc).code`) pour la colonne
    `tool_calls.error_kind` (oto#25 lot b1) : un refus d'authentification amont
    (ex. `not_authorized`) devient un FAIT structuré, en plus du texte tronqué déjà
    porté par `error`. Appelé sur l'exception CAPTURÉE, avant que
    `ErrorEnvelopeMiddleware` ne la normalise pour l'agent (calllog voit toujours le
    brut, comme Sentry).

    Import local : `error_taxonomy` importe `mcp_errors`/`mcp.types`, et ce module
    est chargé très tôt (identité/middleware) — éviter tout risque de cycle au
    chargement.
    """
    try:
        from . import error_taxonomy
        return error_taxonomy.classify(exc).code
    # noqa: SILENT — best-effort de journal, error_kind reste absent plutôt que de risquer l'appel
    except Exception:
        return None


def taille_servie(result) -> Optional[int]:
    """Le nombre de caractères de TEXTE servis à l'appelant (oto-backend#340).

    ⚠️ **Ce qu'elle mesure, et ce qu'elle ne mesure pas.** Elle additionne les blocs
    de `content` — ce que tout client reçoit, quel que soit son support des résultats
    structurés. Elle ne sérialise PAS `structured_content` : le faire coûterait un
    `json.dumps` complet sur le chemin de CHAQUE appel, pour une charge que `content`
    porte déjà sous forme de texte. Une mesure qui ralentit ce qu'elle observe finit
    par être retirée.

    ⚠️ Rend `None` — jamais 0 — dès qu'elle ne sait pas lire la forme. `0` doit rester
    la réponse réellement vide : les confondre ferait compter un outil muet comme un
    outil gratuit, l'inverse exact de ce qu'on cherche.

    Jamais bloquante : appelée sur le chemin de chaque appel, une mesure qui casse le
    service qu'elle observe n'a aucune valeur.
    """
    try:
        blocs = getattr(result, "content", None)
        if blocs is None:
            return None
        total = 0
        for b in blocs:
            texte = getattr(b, "text", None)
            if isinstance(texte, str):
                total += len(texte)
        return total
    except Exception:  # noqa: SILENT — une mesure ne casse jamais l'appel qu'elle observe ; forme illisible = `None`, lu « non mesurée »
        return None


# La FORME de ce qui a été servi (oto-backend#644) : un vocabulaire FERMÉ, jamais le
# contenu. Elle sépare ce que `result_size` laisse ambigu — un 0 ne dit pas « liste
# vide » plutôt que « refus rendu poliment », un non-zéro ne dit pas « résultat »
# plutôt que « `{"error": …}` rendu sous `ok=true` ».
RESULT_SHAPE_EMPTY = "empty"
RESULT_SHAPE_NON_EMPTY = "non_empty"
# `refused(<code>)` : le code est un identifiant (`not_found`, `identity_unavailable`),
# jamais un message. Lettres minuscules et `_` seulement, 40 au plus : un texte libre
# (« TypeError: … », « domaine invalide ») ou une valeur à chiffres — un identifiant
# technique, une empreinte — ne passe pas, et le refus s'écrit `refused(unnamed)`.
_CODE_DE_REFUS = re.compile(r"[a-z][a-z_]{0,39}")
_REFUS_SANS_CODE = "unnamed"
_TEXTES_VIDES = frozenset({"", "[]", "{}", "null"})


def _code_de_refus(sc: dict) -> str:
    erreur = sc.get("error")
    for candidat in (sc.get("code"), sc.get("error_code"),
                     erreur.get("code") if isinstance(erreur, dict) else erreur):
        if isinstance(candidat, str) and _CODE_DE_REFUS.fullmatch(candidat):
            return candidat
    return _REFUS_SANS_CODE


def forme_servie(result) -> Optional[str]:
    """`empty` | `non_empty` | `refused(<code>)` — la forme du résultat servi (#644).

    Lue sur `structured_content` quand il existe (la donnée de l'outil ; fastmcp range
    une sortie non-objet sous `{"result": …}`, qu'on déballe) :
    - `empty` : `None`, `[]`, `{}`, `""` ;
    - `refused(<code>)` : un objet qui porte `ok: false` ou une clé `error` non vide
      à la racine — le refus applicatif rendu sous un appel réussi ;
    - `non_empty` : tout le reste.
    Sans `structured_content`, sur les blocs de `content` : aucun bloc, ou des textes
    tous vides (`""`, `[]`, `{}`, `null`) → `empty` ; sinon `non_empty` (un refus rendu
    en texte seul n'est pas reconnu : ce serait parser le texte sur chaque appel).

    Même contrat que `taille_servie` : `None` — non mesurée — dès que la forme est
    illisible, et jamais bloquante. Un test de clés, pas une sérialisation : le coût
    est constant, quelle que soit la taille du résultat."""
    try:
        sc = getattr(result, "structured_content", None)
        if sc is None:
            blocs = getattr(result, "content", None)
            if blocs is None:
                return None
            textes = [getattr(b, "text", None) for b in blocs]
            if all(isinstance(t, str) and t.strip() in _TEXTES_VIDES for t in textes):
                return RESULT_SHAPE_EMPTY
            return RESULT_SHAPE_NON_EMPTY
        if isinstance(sc, dict) and sc.keys() == {"result"}:
            sc = sc["result"]
        if sc is None or (isinstance(sc, (dict, list, str)) and not sc):
            return RESULT_SHAPE_EMPTY
        if isinstance(sc, dict) and (sc.get("ok") is False or sc.get("error")):
            return f"refused({_code_de_refus(sc)})"
        return RESULT_SHAPE_NON_EMPTY
    except Exception:  # noqa: SILENT — une mesure ne casse jamais l'appel qu'elle observe ; forme illisible = `None`, lu « non mesurée »
        return None


def _oto_call_outcome(result) -> tuple[bool, Optional[str]]:
    """`(ok, error)` de la cible RELAYÉE par `oto_call` (oto-backend#784) — lu dans
    `structured_content`, la forme `{"tool": ..., "ok": bool, "error": str}` que
    `tools/meta.py` rend en donnée sur l'échec d'une cible dispatchée.

    Fail-open comme `taille_servie` : une forme illisible (résultat qui ne vient pas
    de `oto_call`, ou dont le contrat change) rend `(True, None)` — le comportement
    D'AVANT ce lot — plutôt que de faire mentir la mesure dans l'autre sens."""
    try:
        sc = getattr(result, "structured_content", None)
        if not isinstance(sc, dict) or "ok" not in sc:
            return True, None
        ok = bool(sc["ok"])
        error = None if ok else str(sc.get("error") or "")[:MAX_ERROR_CHARS]
        return ok, error
    except Exception:  # noqa: SILENT — une mesure ne casse jamais l'appel qu'elle observe ; forme illisible = comportement d'avant
        return True, None


async def _sans_identite() -> dict:
    return {}


def _outil_ecrivant(name: Optional[str], arguments: Any) -> Optional[str]:
    """L'outil que le journal des révisions nomme pour les lignes écrites pendant
    l'appel : `oto_call` déplié vers sa cible (c'est elle qui écrit), sinon l'outil."""
    if name == "oto_call" and isinstance(arguments, dict):
        cible = arguments.get("name")
        if isinstance(cible, str) and cible.strip():
            return cible.strip()[:128]
    return name or None


class ToolCallLogger(Middleware):
    """Middleware FastMCP : journalise chaque on_call_tool via le sink fourni.

    Sous-classe `fastmcp.server.middleware.Middleware` : son `__call__` dispatche
    vers nos hooks `on_*`. Un simple duck-typing (juste `on_call_tool`) ne suffit
    pas — fastmcp ≥3 appelle le middleware comme un callable et lèverait
    « 'ToolCallLogger' object is not callable », cassant le handshake MCP.

    `identity` = coroutine → {"sub": …, "email": …} (auth Logto custom d'oto :
    le `get_access_token` fastmcp par défaut ne la voit pas). ASYNCHRONE par contrat :
    résoudre l'identité peut lire la base (drain d'alias), et ce middleware tourne SUR la
    boucle — c'est à la coroutine fournie de faire son SQL hors d'elle
    (`server._calllog_identity`, `docs/event-loop-perf.md`).
    """

    def __init__(self, sink: Sink, server: str,
                 identity: Callable[[], Awaitable[dict]] | None = None):
        self.sink = sink
        self.server = server
        self.identity = identity or _sans_identite

    async def _poser_identite(self, row: dict) -> None:
        """Verse `sub`/`email` dans la ligne. Le journal ne casse pas le service : un échec
        d'identité laisse la ligne anonyme — sauf une violation de la garde d'exécution
        (tests), qui n'est pas un échec du journal mais un gel de la boucle à signaler."""
        try:
            row.update({k: v for k, v in (await self.identity()).items()
                        if k in ("sub", "email")})
        except HorsBoucle:
            raise
        # noqa: SILENT — dette déclarée : le journal ne casse pas le service, mais l'échec devrait se voir (#424)
        except Exception:
            pass

    async def on_initialize(self, context, call_next):
        """Journalise le HANDSHAKE lui-même (`kind='protocol'`, ADR 0017 « un seul flux »).

        Pourquoi : deux mécanismes centraux sont calculés à l'`initialize` — la règle
        de visibilité des tools (`SessionVisibilityMiddleware`) et l'injection des
        blocs A/C (`DynamicInstructionsMiddleware`) — donc « une fois par session ».
        Or la cadence RÉELLE de re-handshake des clients n'a jamais été mesurée :
        « Claude rehandshake par conversation » est une croyance de docstring, pas
        une observation. Le sink stampe déjà `session_id` et `client_id` (`azp` :
        claude.ai, Claude Code, ChatGPT…) → une ligne par handshake suffit à trancher,
        par client, sur du trafic réel.

        Isolé du monitoring d'outils : toutes ses lectures filtrent `kind='mcp'`.
        """
        # ⚠️ ASYMÉTRIE fastmcp : `on_initialize` reçoit un `InitializeRequest` ENTIER
        # (params sous `.params`), là où `on_call_tool` reçoit directement les params.
        # Le repli sur `msg` couvre une éventuelle normalisation amont.
        params = getattr(context.message, "params", None) or context.message
        info = getattr(params, "clientInfo", None)
        row: dict[str, Any] = {
            "server": self.server,
            "kind": "protocol",
            "sub": None,
            "email": None,
            "tool": "initialize",
            "args": {
                "client_name": getattr(info, "name", None),
                "client_version": getattr(info, "version", None),
                "protocol_version": getattr(params, "protocolVersion", None),
            },
        }
        await self._poser_identite(row)
        t0 = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            self._record({**row, "ok": False, "error": str(e)[:MAX_ERROR_CHARS],
                          "error_kind": _error_kind(e)}, t0)
            raise
        self._record({**row, "ok": True, "error": None}, t0)
        return result

    async def on_call_tool(self, context, call_next):
        row: dict[str, Any] = {
            "server": self.server,
            "sub": None,
            "email": None,
            "tool": context.message.name,
            "args": truncated_args(context.message.arguments,
                                   tool=context.message.name),
            # #117 — frappé ICI, au point le plus EXTERNE du chemin d'appel (ce
            # middleware est le premier ajouté après ceux du contexte), et avant
            # `call_next`. C'est ce qui en fait un discriminant : il naît dans la pile
            # de CETTE requête, sans dépendre d'un identifiant fourni par le client.
            "call_uid": uuid.uuid4().hex,
        }
        poser_emetteur(row)
        await self._poser_identite(row)
        # Le GESTE de l'appel (oto#273) : ce que le journal des révisions de ligne
        # estampille sur toute ligne écrite pendant l'appel. Son identifiant EST
        # `call_uid` — une révision se relie à SA ligne `tool_calls` sans heuristique.
        jeton_geste = geste.poser(geste.AGENT, row.get("sub"), row["call_uid"],
                                  outil=_outil_ecrivant(context.message.name,
                                                        context.message.arguments))
        t0 = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            self._record({**row, "ok": False, "error": str(e)[:MAX_ERROR_CHARS],
                          "error_kind": _error_kind(e)}, t0)
            raise
        finally:
            geste.retirer(jeton_geste)
        ok, error = True, None
        if context.message.name == "oto_call":
            # `oto_call` (ADR 0036) ne lève JAMAIS sur l'échec de sa cible — il le
            # rend en DONNÉE (`{"ok": False, "error": ...}`), donc sans ce rappel la
            # ligne d'ENVELOPPE marquait `ok: true` même quand la cible avait échoué.
            # Un filtre « erreurs » posé sur `oto_call` ne voyait alors rien de
            # l'échec relayé (oto-backend#784). `ok` suit désormais la cible : c'est
            # la lecture qu'un lecteur du journal suppose.
            ok, error = _oto_call_outcome(result)
        self._record({**row, "ok": ok, "error": error,
                      "result_size": taille_servie(result),
                      "result_shape": forme_servie(result)}, t0)
        return result

    def _record(self, row: dict, t0: float) -> None:
        row["duration_ms"] = int((time.monotonic() - t0) * 1000)
        task = asyncio.create_task(self._insert(row))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    async def _insert(self, row: dict) -> None:
        try:
            await self.sink(row)
        except Exception:
            logger.exception("journalisation tool_call en échec (%s)", row.get("tool"))
