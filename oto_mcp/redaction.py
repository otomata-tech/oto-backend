"""Rédaction des champs sensibles du RÉSULTAT d'un tool — logique PARTAGÉE.

Extraite de `middleware.field_redaction.FieldRedactionMiddleware` (chemin protocole `tools/call`)
pour être réutilisée par `oto_call` (dispatch universel, ADR 0036) : le dispatch
exécute la cible via `Tool.run` **hors chaîne de middleware**, donc il doit
ré-appliquer la rédaction lui-même — sinon un connecteur à PII
(folk/pennylane/unipile) fuiterait par ce canal. « Derive don't duplicate ».

Politique (ADR 0009/0015) : la policy de l'org active gouverne l'exposition.
**Fail-closed** : une policy qui EXISTE mais échoue RETIENT la sortie (lève
`RedactionWithheld`) plutôt que de laisser fuiter le brut. Absence de policy
(`is_empty`), échec de résolution sur un service sans défaut serveur, ou payload
non-structuré = passe-through (sentinelle `PASSTHROUGH`).

Ce module porte aussi le **rendu du VIDE** (`is_empty_payload`/`render_empty`) :
un résultat sans aucun résultat se sert au modèle **en phrase**, jamais en
structure nue. Cf. `EMPTY_MESSAGES` pour la règle et son incident fondateur.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Sentinelle « rien à rédiger » — distincte de None (un payload peut légitimement
# valoir None). L'appelant renvoie alors le résultat d'ORIGINE inchangé (pas de
# re-sérialisation sur le chemin chaud du cas commun « aucune policy »).
PASSTHROUGH = object()


class RedactionWithheld(Exception):
    """La rédaction d'une policy EXISTANTE a échoué → sortie retenue (fail-closed)."""


def _resolve_field_filter(service: str):
    # Import tardif : `access` importe des stores → éviter un cycle au chargement.
    from . import access
    return access.resolve_field_filter(service)


def _service_has_server_default(service: str) -> bool:
    from . import field_filter_defaults
    return service in field_filter_defaults.SERVER_DEFAULTS


def extract_payload(result) -> dict | list | None:
    """Forme brute renvoyée par un tool à partir de son `ToolResult` :
    `structured_content` si dict, sinon le JSON du 1er bloc `content`. None si
    rien de structuré (texte libre / binaire)."""
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    content = getattr(result, "content", None) or []
    block = content[0] if content else None
    text = getattr(block, "text", None)
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if isinstance(data, (dict, list)):
            return data
    return None


def redact_payload(service: str, payload):
    """Applique la policy de rédaction de l'org active pour `service` (namespace)
    au `payload` brut (dict | list). Retourne le payload rédacté, ou `PASSTHROUGH`
    si rien ne s'applique. Lève `RedactionWithheld` si une policy existe mais lève."""
    if not isinstance(payload, (dict, list)):
        return PASSTHROUGH
    try:
        ff = _resolve_field_filter(service)
    except Exception:
        # Résolution de policy en échec (ex. DB) : policy inconnue. Service à PII
        # connu (défaut serveur déclaré) → fail-closed ; sinon passe-through pour
        # ne pas casser tous les tools sur un aléa DB.
        logger.exception("resolve_field_filter a échoué pour %s", service)
        if _service_has_server_default(service):
            raise RedactionWithheld(service)
        return PASSTHROUGH
    if ff.is_empty:
        return PASSTHROUGH
    # Une policy EXISTE pour ce service → fail-closed à partir d'ici.
    try:
        return ff.apply(payload)
    except Exception:
        logger.exception("rédaction de %s en échec — sortie retenue", service)
        raise RedactionWithheld(service)


def rebuild_result(result, redacted):
    """Réémet un `ToolResult` avec `redacted` sur les DEUX canaux : texte JSON +
    `structured_content` (seulement si l'original en portait un dict — sinon la
    donnée vivait dans le canal texte, le structuré reste vide)."""
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent
    sc = getattr(result, "structured_content", None)
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(redacted, default=str))],
        structured_content=redacted if isinstance(sc, dict) else None,
        meta=getattr(result, "meta", None),
        is_error=False,
    )


def withheld_result(name: str):
    """`ToolResult` d'erreur « sortie retenue » (fail-closed) pour l'outil `name`."""
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent
    return ToolResult(
        content=[TextContent(
            type="text",
            text=f"[oto] rédaction de « {name} » impossible — sortie retenue par sécurité.")],
        is_error=True,
    )


# --- Rendu du VIDE : une phrase, jamais une structure nue ---------------------

# Phrase servie quand l'outil ne déclare pas la sienne.
EMPTY_MESSAGE_DEFAULT = "Aucun résultat pour cette recherche."

# Gabarit par outil — table de DÉCLARATION, pas de logique : le rendu du vide est
# générique, seul le mot juste appartient à l'outil. Clé = nom CANONIQUE (le
# préfixe de tenant est rétabli plus haut dans la chaîne). Déclarée ici, dans la
# couche de rendu, plutôt qu'au registre des connecteurs : la table est lue sur le
# chemin chaud de CHAQUE appel, et un outil n'a pas à savoir comment on le rend.
EMPTY_MESSAGES: dict[str, str] = {
    "fr_accords_search": "Aucun accord déposé pour ce SIREN.",
}

# Clés de COLLECTION reconnues — liste FERMÉE, et c'est tout l'intérêt : « toute clé
# dont la valeur est une liste » faisait lire un accusé d'écriture (`{"ok": true,
# "deleted": []}`) comme un résultat vide, et répondre « aucun résultat » à qui venait
# de supprimer zéro ligne. Relevé du 2026-08-27 sur `tools/` + `capabilities/` :
#   rows/results/items/data  — le gros des connecteurs (`sheets`, `datastore`, `fr_stock`,
#                              `unipile`, `foncier`, `lemlist` ; `data` aussi chez sellsy,
#                              `{"data": rows, "pages": …, "truncated": …}`)
#   result                   — l'enveloppe fastmcp d'un retour `list` (98 outils)
#   records                  — airtable (clé DYNAMIQUE `{key: items}`), salesforce
#   files/messages/events    — `drive_file`, `gmail_message`/`messenger_chat`, `calendar_event`
#   calls/jobs/documents/hits — `oto_admin_monitoring`, `runner_jobs`, `me_legal`, `oto_search`
#   matches/entries          — AUCUNE occurrence relevée ; conservées du contrat arbitré.
# ⚠️ Ce backend ne nomme PAS ses collections de façon uniforme : les capacités à elles
# seules en exposent ~90 (`instances`, `seats`, `guides`, `signals`, `namespaces`…), et
# airtable calcule la sienne à l'exécution. La liste fermée sous-détecte donc beaucoup —
# c'est le sens du compromis : mieux vaut servir une structure de trop qu'affirmer un
# vide à tort. Le second signal rattrape l'essentiel, la convention maison étant de
# poser `count: len(...)` à côté de la collection (34 sites sur 41).
_COLLECTION_KEYS = (
    "rows", "results", "items", "matches", "hits", "data", "entries",
    "calls", "jobs", "documents", "records", "files", "messages", "events",
    "result",
)

# Clés reconnues comme compteur de volume à côté d'une collection.
_COUNTER_KEYS = ("total_count", "total", "count")

# Clés de NOTICE : un avertissement que l'agent doit voir MÊME quand la collection est
# vide — une réponse tronquée n'est pas une absence de résultat, et la rendre en phrase
# ferait conclure « il n'y a rien » là où le plafond a coupé avant d'avoir cherché.
_NOTICE_KEYS = ("note", "hint", "warning", "warnings", "notices",
                "error", "errors", "partial", "partial_errors", "hors_schema")

# Les mêmes, en FAMILLES : le suffixe est trop productif pour une liste fermée —
# `_etablissements_truncated` (fr), `text_truncated`/`{champ}_truncated` (unipile, clé
# dynamique), `texte_tronque` (fr), `truncated_results`/`truncated_companies`
# (theirstack), `filtre_ca_avertissement`/`finances_avertissement` (fr), `slot_warnings`.
_NOTICE_FRAGMENTS = ("truncat", "tronqu", "warning", "avertissement")

# Accusés d'ÉCRITURE : « opération réussie, 0 élément » n'est pas « rien trouvé ». Le
# nom de la collection ne suffit pas à les écarter, parce qu'ils portent AUSSI les
# signaux reconnus — `{"total": len(items), "succeeded": …, "failed": []}` (webflow) et
# `{"total": total, "imported": 0, "items": [], …}` (waalaxy) seraient lus comme vides
# par le compteur. On les écarte donc explicitement.
_WRITE_ACK_KEYS = ("ok", "dry_run", "created", "updated", "deleted", "removed",
                   "added", "skipped", "failed", "succeeded", "imported",
                   "submitted", "sent", "revoked", "cleared", "dropped",
                   "released", "trashed", "archived")


def empty_message(tool_name: str) -> str:
    """Phrase à servir pour un résultat vide de `tool_name` : son gabarit déclaré,
    sinon la phrase générique."""
    return EMPTY_MESSAGES.get(tool_name) or EMPTY_MESSAGE_DEFAULT


def _est_compteur(valeur) -> bool:
    # `True` est un `int` en Python : un drapeau nommé `count` n'est pas un volume.
    return isinstance(valeur, int) and not isinstance(valeur, bool)


def _cle_parle(cle: str, valeur) -> bool:
    """Une clé qui porte une information, donc TRUTHY — un `truncated: false` ou un
    `warnings: []` ne disent rien et ne doivent rien disqualifier."""
    if not valeur:
        return False
    bas = cle.lower()
    return bas in _NOTICE_KEYS or any(f in bas for f in _NOTICE_FRAGMENTS)


def _porte_une_notice(payload: dict) -> bool:
    """Un avertissement, à la racine ou dans un sous-dict immédiat (`fr_accords_search`
    porte le sien sous `effectifs_filter.truncated`, l'outil même de l'incident)."""
    for niveau in (payload, *(v for v in payload.values() if isinstance(v, dict))):
        if any(_cle_parle(c, v) for c, v in niveau.items()):
            return True
    return False


def _est_un_accuse_d_ecriture(payload: dict) -> bool:
    return any(c in _WRITE_ACK_KEYS or c.startswith("would_") for c in payload)


def is_empty_payload(payload) -> bool:
    """Vrai quand le payload dit « je n'ai rien trouvé » — et RIEN d'autre.

    Une **liste** est vide si elle n'a pas d'élément. Un **dict** est vide s'il
    l'AFFIRME, par l'un des deux signaux reconnus :
    - un **compteur** (`total_count`, `total`, `count`) qui vaut 0 ;
    - une **clé de collection reconnue** (`_COLLECTION_KEYS`) dont la valeur est une
      liste sans élément.

    Quatre contradictions le disqualifient, parce qu'elles portent une information que
    la phrase effacerait :
    - une collection reconnue NON vide, ou un compteur non nul — la structure est
      alors rendue telle quelle plutôt que d'affirmer un vide qu'elle dément ;
    - une **notice** (`truncated`, `warning`, `note`, `hint`…) : une réponse coupée par
      un plafond, ou assortie d'un conseil, n'est pas une absence de résultat ;
    - un **accusé d'écriture** (`ok`, `deleted`, `failed`, `imported`, `would_*`…) :
      « opération réussie, 0 élément » n'est pas « rien trouvé ».

    Une clé de collection reconnue dont la valeur n'est PAS une liste (`data` porte
    souvent un objet) n'est ni un signal ni une contradiction : elle est ignorée. Le
    signal ne se cherche qu'à la RACINE — une collection vide imbriquée sous un
    résultat par ailleurs peuplé n'est pas un résultat vide.
    """
    if isinstance(payload, list):
        return not payload
    if not isinstance(payload, dict):
        return False

    vide = False
    for cle in _COLLECTION_KEYS:
        valeur = payload.get(cle)
        if not isinstance(valeur, list):
            continue
        if valeur:
            return False
        vide = True
    for cle in _COUNTER_KEYS:
        valeur = payload.get(cle)
        if not _est_compteur(valeur):
            continue
        if valeur != 0:
            return False
        vide = True
    if not vide:
        return False
    return not _porte_une_notice(payload) and not _est_un_accuse_d_ecriture(payload)


def _enveloppe_vide(payload) -> bool:
    """`{"result": …}` est l'enveloppe que FastMCP pose sur un retour NON-dict : la
    reconnaître évite de confondre « l'outil n'a rien rendu » avec « l'outil a rendu
    la valeur 0 »."""
    return (isinstance(payload, dict) and set(payload) == {"result"}
            and payload["result"] in (None, [], {}, ""))


def sert_du_vide(result) -> bool:
    """Vrai quand ce `ToolResult` ne porte RIEN pour le modèle. Seule porte d'entrée
    du rendu du vide — le middleware ne juge pas lui-même.

    Deux chemins, mesurés sur le FastMCP servi (3.4.2) :

    - **zéro bloc de contenu.** `_convert_to_content([])` — et `None` — ne rend
      AUCUN bloc, là où `[1]` rend un bloc texte et un dict rend son JSON. Le canal
      texte est alors littéralement muet : le modèle reçoit un tour sans contenu, et
      c'est cette absence-là qui le fait dérailler en production (`fr_directors` sur
      un SIREN sans dirigeant). Une phrase vaut mieux que rien — et il n'est pas
      question de fabriquer un JSON pour combler le trou.
    - **un bloc, mais une collection vide dedans** : `is_empty_payload` tranche.

    Le zéro-bloc ne suffit pas seul : on vérifie que le canal structuré ne porte pas
    davantage, sans quoi un résultat riche servi hors canal texte serait effacé.
    """
    if getattr(result, "content", None):
        return is_empty_payload(extract_payload(result))
    payload = extract_payload(result)
    return payload is None or _enveloppe_vide(payload) or is_empty_payload(payload)


def render_empty(result, tool_name: str):
    """Réémet un `ToolResult` dont le canal TEXTE ne porte que la phrase, le canal
    structuré restant INTACT — le client qui parse garde sa structure vide.

    C'est la structure DANS LE TEXTE qui fait dégénérer le décodage du modèle, pas
    son existence : un `{"total_count": 0, "rows": []}` servi en texte a fait boucler
    une flotte d'agents sur des centaines de `]}` avant qu'ils n'encadrent leur propre
    narration en appel d'outil — 16 des 26 faux départs d'une campagne et 10 des 11
    d'une vague de production (2026-08-27, otomata-tech/oto#32).

    Phrase SEULE : y rajouter la structure « pour information » rétablirait très
    exactement le déclencheur qu'on retire.
    """
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent
    return ToolResult(
        content=[TextContent(type="text", text=empty_message(tool_name))],
        structured_content=getattr(result, "structured_content", None),
        meta=getattr(result, "meta", None),
        is_error=False,
    )
