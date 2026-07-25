"""Client LLM « chat + tool calling » — substrat du mode agent SERVER-SIDE.

Seam UNIQUE entre la boucle d'agent (`agent_runtime`) et le fournisseur de modèle.
Implémenté avec le **SDK officiel Anthropic** (`anthropic`, Messages API), import
**GUARDÉ** (même patron que `prefab_ui` des MCP Apps) : sans la lib installée ou
sans `ANTHROPIC_API_KEY`, `enabled()` est False et TOUTE la surface agent se
désactive proprement — jamais un prérequis de boot (le serveur MCP/REST démarre
identique).

Pourquoi un module dédié plutôt qu'un appel inline : la boucle d'agent
(`agent_runtime`) ne doit connaître QUE trois choses — « donne-moi un tour »,
« voici les outils », « voici l'historique ». Le format de fil Anthropic (blocs
`tool_use` / `tool_result`, blocs de pensée à réémettre tels quels) reste
CONFINÉ ici et dans le fil que la boucle transporte sans l'inspecter.

Contrat (cf. la doc Messages API) :
- `complete()` rend un `Turn` : texte visible + `tool_calls` demandés + `raw_content`
  (les blocs BRUTS de la réponse, à réinjecter **inchangés** dans le fil comme tour
  assistant — les blocs de pensée doivent être rejoués verbatim).
- `stop_reason` est lu AVANT le contenu : un `refusal` (classifieurs de sûreté)
  revient en HTTP 200 avec un contenu vide ou partiel → on le traduit en tour
  terminal, jamais en crash d'indexation.
- Pas de `temperature`/`top_p` (retirés sur les modèles courants), pas de
  `budget_tokens` : la profondeur se règle par `output_config.effort`.
- `max_tokens` plafonne pensée + texte ensemble → on laisse de la marge (défaut
  8192) sinon la réponse se coupe au milieu.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Modèle par défaut : le plus capable de la gamme, surchargeable par env pour
# arbitrer coût/latence par déploiement (preprod vs prod, endpoint public vs interne).
DEFAULT_MODEL = "claude-opus-5"

# Effort = LE curseur profondeur/coût (`output_config.effort`, low→max). Défaut
# `medium` : un agent de projet partagé répond à des questions cadrées sur un
# périmètre d'outils étroit — `high` (le défaut d'API) y dépenserait sans gain.
DEFAULT_EFFORT = "medium"

DEFAULT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class ToolCall:
    """Un appel d'outil demandé par le modèle (bloc `tool_use`)."""
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Turn:
    """Un tour de modèle. `raw_content` = les blocs bruts à réémettre tels quels."""
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    raw_content: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LlmUnavailable(RuntimeError):
    """Le substrat LLM n'est pas configuré (lib absente / clé absente)."""


def model() -> str:
    return os.environ.get("OTO_AGENT_MODEL") or DEFAULT_MODEL


def effort() -> str:
    return os.environ.get("OTO_AGENT_EFFORT") or DEFAULT_EFFORT


def max_tokens() -> int:
    try:
        return int(os.environ.get("OTO_AGENT_MAX_TOKENS", "") or DEFAULT_MAX_TOKENS)
    except ValueError:
        return DEFAULT_MAX_TOKENS


def _sdk():
    """Module `anthropic` ou None (extra optionnel — import local, jamais au boot)."""
    try:
        import anthropic  # noqa: PLC0415 — import guardé, cf. docstring
        return anthropic
    except Exception:  # noqa: BLE001 — lib absente = capacité absente, pas une panne
        return None


def enabled() -> bool:
    """Le mode agent est-il servable sur ce déploiement ? (lib + clé présentes)"""
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and _sdk() is not None


_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        anthropic = _sdk()
        if anthropic is None:
            raise LlmUnavailable("le paquet `anthropic` n'est pas installé")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LlmUnavailable("ANTHROPIC_API_KEY absente")
        _CLIENT = anthropic.Anthropic()
    return _CLIENT


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return getattr(block, "type", "") or ""


def _block_to_dict(block: Any) -> dict:
    """Bloc de réponse → dict JSON-sérialisable **réinjectable tel quel**.

    Le fil transite par HTTP (le client rejoue l'historique — zéro état serveur) :
    les objets pydantic du SDK doivent donc être aplatis, mais SANS déformer la
    forme de fil attendue au tour suivant. `mode="json"` rend des scalaires purs ;
    on écarte les clés nulles (champs optionnels absents = accepté, `null` explicite
    pas toujours). Un bloc de pensée rejoué modifié serait rejeté : on ne reconstruit
    jamais un bloc, on le transporte."""
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if v is not None}
    dump = getattr(block, "model_dump", None)
    if dump is not None:
        try:
            return {k: v for k, v in dump(mode="json").items() if v is not None}
        except Exception:  # noqa: BLE001 — repli sur la forme minimale utilisable
            pass
    return {"type": _block_type(block) or "text",
            "text": str(getattr(block, "text", "") or "")}


def complete(*, system: str, messages: list, tools: list[dict]) -> Turn:
    """UN tour de modèle. **SYNCHRONE** — le serveur est mono-loop : l'appeler
    depuis un threadpool (`run_in_threadpool`), jamais dans la boucle (cf. CLAUDE.md
    §PERF, même règle que `embeddings.embed_texts`).

    `messages` = le fil au format Anthropic (tours user/assistant, blocs
    `tool_result` côté user) ; `tools` = `[{name, description, input_schema}]`.
    Lève `LlmUnavailable` si le substrat n'est pas configuré ; toute autre erreur
    (réseau, quota, 4xx) remonte telle quelle à l'appelant, qui décide.
    """
    client = _client()
    kwargs: dict = {
        "model": model(),
        "max_tokens": max_tokens(),
        "system": system,
        "messages": messages,
        "output_config": {"effort": effort()},
    }
    if tools:
        kwargs["tools"] = tools
    resp = client.messages.create(**kwargs)

    stop = getattr(resp, "stop_reason", "") or "end_turn"
    usage = {}
    try:
        usage = {"input_tokens": resp.usage.input_tokens,
                 "output_tokens": resp.usage.output_tokens}
    except Exception:  # noqa: BLE001 — la télémétrie n'est jamais bloquante
        pass

    # Refus des classifieurs : HTTP 200, contenu vide ou partiel → tour terminal
    # explicite. On lit `stop_reason` AVANT le contenu (jamais `content[0]` nu).
    if stop == "refusal":
        return Turn(text="", tool_calls=(), stop_reason="refusal",
                    raw_content=[_block_to_dict(b) for b in getattr(resp, "content", []) or []],
                    usage=usage)

    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in getattr(resp, "content", []) or []:
        kind = _block_type(block)
        if kind == "text":
            texts.append(getattr(block, "text", "") or "")
        elif kind == "tool_use":
            raw_args = getattr(block, "input", None)
            calls.append(ToolCall(
                id=getattr(block, "id", "") or "",
                name=getattr(block, "name", "") or "",
                arguments=raw_args if isinstance(raw_args, dict) else {}))
    return Turn(text="\n".join(t for t in texts if t.strip()).strip(),
                tool_calls=tuple(calls), stop_reason=stop,
                raw_content=[_block_to_dict(b) for b in getattr(resp, "content", []) or []],
                usage=usage)
