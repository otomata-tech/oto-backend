"""Journal des appels — middleware MCP inliné (ex-lib `otomata-calllog`) + gestes REST.

Une ligne par `call_tool` reçu : qui (sub du JWT), quel tool, arguments tronqués,
durée, succès/erreur. L'écriture part en tâche de fond : zéro latence ajoutée, un
échec de journalisation n'échoue jamais le tool.

Le journal a **un seul domicile** : la table `tool_calls` (`db.insert_tool_call`),
et **un seul module** — ici. `log_rest_call` y ajoute les gestes faits depuis le
dashboard (`kind='rest'`), sous le MÊME vocabulaire de `tool` que la surface MCP
(`data_write`…), pour qu'une lecture de journal voie l'agent ET l'humain.

Historique : lib `otomata-calllog` (extraite d'ogic 2026-06-12), inlinée ici le
2026-07-23 (otomata-calllog#1) — le backend était son dernier consommateur, et le
**contrat canonique** (schéma de ligne `tool_calls`, dashboards comparables entre
serveurs MCP) vit désormais dans le socle `otomata-mcp` (`logging.py`). Le schéma
local étend ce contrat en OTO-LOCAL : `kind`, `session_id`, `run_id`, `org_id`,
`client_id` (cf. `db/_schema.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastmcp.server.middleware import Middleware

from . import journal_secrets

logger = logging.getLogger("oto_mcp.calllog")

MAX_ARG_CHARS = 300
MAX_ERROR_CHARS = 500

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
    stringifié et coupé — le journal montre l'intention, pas le payload.

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
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if k in caches and v is not None:
            out[k] = journal_secrets.mask(v)
            continue
        if caches:
            v = _masque_en_profondeur(v, caches, MAX_MASK_DEPTH)
        if not (v is None or isinstance(v, (int, float, bool))):
            v = str(v)
            if len(v) > max_chars:
                v = v[:max_chars] + "…"
        out[k] = v
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
                  fields: Any = None, forced: Any = None, ok: bool = True,
                  error: str | None = None,
                  org_id: int | None = None, duration_ms: int | None = None) -> None:
    """Journalise un GESTE fait depuis le dashboard (REST) dans le flux unifié.

    Même table, même fonction d'insertion et même discipline que le middleware MCP :
    **best-effort** (l'écriture part en tâche de fond, un échec se log en warning et
    n'échoue JAMAIS la requête métier) et arguments passés à `truncated_args` (le
    journal montre l'intention, jamais le payload).

    `tool` doit nommer le geste dans le vocabulaire de la surface MCP (`data_write`,
    `data_delete_row`, `data_release`) : les lectures du journal (parcours d'une
    ligne, activité d'un tableau) filtrent là-dessus, pas sur la route HTTP.
    `forced` (#658) = les colonnes verrouillées remplacées de force. Comme `fields`,
    il rejoint la ligne APRÈS `truncated_args` et reste un VRAI tableau JSON : passé
    dans `args`, il repartirait stringifié et coupé à 300 caractères, donc illisible
    colonne par colonne — exactement ce que `_fields_list` existe pour éviter. Absent
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
                 **({"readonly_forced": list(forced)} if forced else {})},
        "ok": bool(ok),
        "error": (str(error)[:MAX_ERROR_CHARS] if error else None),
        "org_id": org_id,
        "duration_ms": duration_ms,
    }
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


class ToolCallLogger(Middleware):
    """Middleware FastMCP : journalise chaque on_call_tool via le sink fourni.

    Sous-classe `fastmcp.server.middleware.Middleware` : son `__call__` dispatche
    vers nos hooks `on_*`. Un simple duck-typing (juste `on_call_tool`) ne suffit
    pas — fastmcp ≥3 appelle le middleware comme un callable et lèverait
    « 'ToolCallLogger' object is not callable », cassant le handshake MCP.

    `identity` = callable → {"sub": …, "email": …} (auth Logto custom d'oto :
    le `get_access_token` fastmcp par défaut ne la voit pas).
    """

    def __init__(self, sink: Sink, server: str, identity: Callable[[], dict] | None = None):
        self.sink = sink
        self.server = server
        self.identity = identity or (lambda: {})

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
        try:
            row.update({k: v for k, v in self.identity().items() if k in ("sub", "email")})
        # noqa: SILENT — dette déclarée : le journal ne casse pas le service, mais l'échec devrait se voir (#424)
        except Exception:
            pass
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
        try:
            row.update({k: v for k, v in self.identity().items() if k in ("sub", "email")})
        # noqa: SILENT — dette déclarée : le journal ne casse pas le service, mais l'échec devrait se voir (#424)
        except Exception:
            pass
        t0 = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            self._record({**row, "ok": False, "error": str(e)[:MAX_ERROR_CHARS],
                          "error_kind": _error_kind(e)}, t0)
            raise
        self._record({**row, "ok": True, "error": None,
                      "result_size": taille_servie(result)}, t0)
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
