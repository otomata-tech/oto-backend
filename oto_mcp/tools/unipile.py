"""Unipile — LinkedIn & WhatsApp hébergés (recherche / scrape / messagerie).

⚠️ **Un module, SEPT connecteurs** depuis le split du 2026-08-28 : `unipile` (le
compte, qui porte la clé) et ses six canaux, dont `linkedin_unipile` sert ici. Les
cinq autres ont leur propre `tools/<canal>.py`, qui appelle la factory de messagerie
commune d'ici. Cf. `docs/unipile.md` §Le split.

La clé est résolue par appel **sous le connecteur du CANAL**
(`unipile_client` → `access.resolve_credential(<canal>)`), pas sous `unipile` : c'est
ce qui fait mordre l'ACL et l'activation DE CE CANAL, le gate lisant le nom qu'on lui
passe. La clé, elle, reste celle du compte — la délégation
(`Connector.credential_of`) normalise dans la cascade. Le dsn (API v2 : gateway
`api.unipile.com`) et l'account_id sont résolus côté client (env `UNIPILE_DSN`,
défaut api.unipile.com).

Pourquoi à côté du connecteur browser `linkedin` : la session vit chez Unipile
(vrai Chrome + proxy résidentiel), ce qui contourne l'empreinte TLS et
l'isolation de session du browser local (issue #5) — au prix d'un SaaS payant.
"""
from __future__ import annotations

import logging
import os
import time
import unicodedata
from datetime import datetime, timezone
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, providers, session_org, status_hints
from ..connectors import flow as connector_flow
from ..connectors import verify as connector_verify

logger = logging.getLogger(__name__)

# Miroir autogéré du feed (home LinkedIn) dans le datastore spine (ADR 0016).
_FEED_NS = "linkedin-feed"          # namespace datastore per-user
_FEED_SYNC_CAP_PAGES = 5            # garde-fou anti-martelage LinkedIn par sync
_FEED_PAGE_COUNT = 40              # items par page Voyager pendant le sync
_FEED_SORT_ORDER = "MEMBER_SETTING"  # honore le tri choisi sur la home LinkedIn

# Vue de TRI du feed — le défaut d'`op="feed"` (signal #384). Mesuré sur 40 posts réels
# du miroir : la ligne brute coûte ~1 650 caractères (66 Ko la page de 40, au-delà du
# plafond d'un résultat MCP — le harnais bascule en fichier et l'agent doit re-trier au jq
# avant de commencer son travail). Le texte pèse 60 % à lui seul, le reste est de la
# redondance (`urn` == `_id` == la queue de `post_url`) et de la comptabilité de miroir.
# Rien ne SORT du catalogue : le miroir garde toutes ses colonnes (`data_rows`), et
# `fields=["*"]` / `text_max_chars=None` rendent le brut. Ce qui change est la LECTURE
# par défaut — ADR 0047 §Amendement du 11/08 : le chemin paresseux doit être le juste.
_FEED_DEFAULT_FIELDS = (
    "urn", "post_url",                              # adresser le post + le citer
    "author_name", "author_headline",               # qui parle (le guide trie dessus)
    "posted_at",                                    # fraîcheur
    "text",                                         # de quoi ça parle (tronqué)
    "content_type", "content_title",                # …et de quoi le post est FAIT
    "reactions_count", "comments_count",            # traction
    "is_repost", "original_author_name",            # repost ⟹ réagir sur l'original
    "original_text", "original_content_type",       # …et le propos EST dans l'original
    "feed_reason",                                  # pourquoi c'est dans ton feed
)
# Écartées du défaut : `_created_at`/`_updated_at` (dates du MIROIR, pas du post),
# `posted_relative` (dérivable de `posted_at`, et figée à l'heure du sync donc trompeuse
# relue plus tard), `surfaced_by`/`comment_authors` (vides sur 40/40 des lignes mesurées).
# `_id` AUSSI : le sync écrit `upsert_row(_FEED_NS, urn, item)`, donc l'`urn` EST l'id de
# la ligne — les deux colonnes portent la même chaîne, par construction et pas par hasard
# (vérifié 40/40). Rendre les deux coûtait 2,7 % de la page pour zéro information. Qui
# veut relire la ligne passe l'`urn` en `id` à `data_rows`, c'est le même identifiant.
_FEED_ADDRESSING = ("urn",)         # jamais projeté hors du résultat : sans lui on ne
                                    # peut plus ouvrir le post ni le dédupliquer

# Longueur d'extrait par DÉFAUT de tout texte long rendu en LISTE par ce connecteur —
# feed (#384) comme posts/commentaires d'un membre (#281). Un seul chiffre pour toute la
# famille `linkedin_*` : l'agent l'apprend une fois. L'entête d'un post suffit à
# le trier (c'est ce que l'agent de #384 avait retenu à la main au jq) ; la coupe est
# MARQUÉE (`text_truncated`) et `text_max_chars=None` rend le texte entier.
_TEXT_EXCERPT_CHARS = 600
# TOUS les champs de texte libre d'un item, pas seulement `text` : depuis oto-core
# v1.80.0 un repost porte aussi `original_text` (le propos réel, quand `text` ne
# contient que le mot du re-partageur). Ne borner que `text` laisserait le second
# passer entier et annulerait le plafond sur précisément les posts où il y a le plus
# à lire. Chaque coupe est marquée à son propre nom (`original_text_truncated`).
_TEXTUAL_FIELDS = ("text", "original_text")

# --- Discipline du rate-limit amont LinkedIn (Unipile). EMPIRIQUE : le 429 Unipile est un
# rate-limit EN COUCHES (« We only allow 1 / 10 / 100 requests ») dont le `Retry in N`
# SUIT LA CADENCE RÉCENTE du compte — ce n'est pas une constante. Deux mesures, à ne pas
# confondre : 2026-07-21, rafales modérées → 3-38s (455 appels/h dont 187 OK, 429 récupéré
# en ~40s) ; 2026-08-07, APRÈS un pilote qui a enchaîné → ~55 min puis ~53 min sur un seul
# appel isolé (#361). Donc pas de cap dur 100/12h, mais pas non plus de « quelques secondes »
# promises : le délai à annoncer est CELUI qu'Unipile renvoie, jamais une moyenne. On
# SUIT le signal d'Unipile : sur un 429 on arme un cooldown = SON PROPRE `retry_after`
# (parsé oto-core, secondes incluses), plafonné, et on refuse les scrapes du sub d'ici là
# — micro-backoff qui auto-pace la rafale sans marteler (le martèlement dégrade en timeouts
# puis fait checkpoint/déconnecte le compte). + cache fiches société (route la plus
# contrainte, ~100/fenêtre) = 0 appel amont, 0 quota. Garde-fous PROCESS-LOCAL (mono-loop).
_RATE_LIMIT_UNTIL: dict[str, float] = {}   # sub -> epoch de fin de cooldown
_COMPANY_CACHE: dict[tuple, tuple] = {}     # (sub, ident_lower) -> (epoch, résultat)
_COMPANY_TTL = 6 * 3600                      # fiches société ~statiques → 6h
_COMPANY_CACHE_MAX = 3000                    # borne mémoire (purge grossière au-delà)
_RL_DEFAULT_SECS = 30    # 429 sans délai lisible → backoff court (les rafales = quelques s)
_RL_MAX_SECS = 3600      # plafond : un « Retry in 12 hours » (rare/trompeur) ne verrouille
                         #  pas la journée — au pire on re-sonde après 1h (auto-correcteur)


def _fmt_wait(secs: float) -> str:
    s = max(1, int(secs) + 1)
    return f"~{s}s" if s < 90 else f"~{s // 60 + 1} min"


def _rate_limit_guard(sub: str) -> None:
    """Refuse un scrape pendant le cooldown 429 en cours (sans taper Unipile) — la durée
    est CELLE qu'Unipile a demandée. Évite de marteler pendant le backoff."""
    until = _RATE_LIMIT_UNTIL.get(sub, 0.0)
    now = time.time()
    if until > now:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            f"⏳ Unipile rate-limite ce compte LinkedIn — réessaie dans {_fmt_wait(until - now)} "
            "(délai demandé par Unipile : de quelques secondes après une rafale légère à "
            "~1h quand la cadence récente a été soutenue — c'est le délai affiché qui fait "
            "foi, pas une moyenne). RALENTIS la cadence des appels linkedin_* plutôt que de "
            "les enchaîner en rafale ; si l'attente est longue, passe à autre chose et "
            "reviens, plutôt que de sonder en boucle.")))


def _note_rate_limited(sub: str, err) -> None:
    """Arme le cooldown = le `retry_after` renvoyé par Unipile — quelques secondes après
    une rafale légère, jusqu'à ~1h quand la cadence récente a été soutenue (#361) — plafonné
    à `_RL_MAX_SECS` (un « 12 hours » rare/trompeur ne bloque pas la journée) ; défaut court
    si le corps n'a pas de délai lisible."""
    secs = min(getattr(err, "retry_after", None) or _RL_DEFAULT_SECS, _RL_MAX_SECS)
    _RATE_LIMIT_UNTIL[sub] = time.time() + secs


def _actor_key() -> str:
    """Clé d'ACTEUR pour la comptabilité LOCALE — cooldown de rate-limit, cache société.
    Jamais une autorisation : ce qu'elle indexe, c'est une cadence, pas un droit.

    Le `sub` quand il y en a un. Sur un endpoint MCP de projet publié (ADR 0032) il n'y
    en a pas, et exiger un `sub` ici refusait des lectures que le projet autorise
    pourtant — c'est le fond de #276. Le projet est le porteur légitime : il n'opère
    qu'un compte LinkedIn, donc une seule cadence à tenir."""
    from .. import subdomain_project
    anon = subdomain_project.current_anon_context()
    if anon is not None:
        return f"project:{anon.project_id}"
    return access.current_user_sub_or_raise()


def _scrape(sub: str, fn):
    """Scrape LinkedIn sous discipline de rate-limit : refus pendant un cooldown en cours,
    et sur un 429 amont on arme le cooldown (= délai Unipile) + erreur actionnable « ralentis »."""
    _rate_limit_guard(sub)
    from oto.tools.unipile.client import UnipileRateLimited
    try:
        return fn()
    except UnipileRateLimited as e:
        _note_rate_limited(sub, e)
        wait = _fmt_wait(_RATE_LIMIT_UNTIL[sub] - time.time())
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            f"⏳ Unipile rate-limite ce compte LinkedIn ({e}). Réessaie dans {wait} et "
            "RALENTIS : n'enchaîne pas des dizaines d'appels linkedin_* en rafale (c'est ce qui "
            "déclenche le throttle, puis dégrade et déconnecte le compte). Les fiches société "
            "déjà vues sont servies du cache — inutile de les relire.")))


# Filtres STRUCTURÉS de `linkedin_unipile_search` (≠ mots-clés) : ce sont eux que
# l'amont peut ne pas appliquer sans le dire (#536).
_FACETTES_RECHERCHE = ("company", "location", "industry", "skills",
                       "network_distance", "advanced_keywords")


def _alertes_recherche(items, total, cursor, facettes, page_suivante):
    """Ce que la page NE dit pas d'elle-même (#536 : trois amputations muettes sur une
    même cible, aucune erreur, aucun indicateur — un agent honnête en conclut « vivier
    vide » ou « population balayée »)."""
    out = []
    if isinstance(total, int) and len(items) < total:
        if cursor:
            out.append(
                f"page PARTIELLE : {len(items)} résultats servis sur {total} — il reste "
                "des pages, rappelle avec `cursor` avant de conclure quoi que ce soit "
                "sur la population.")
        else:
            out.append(
                f"⚠️ {total - len(items)} résultats sur {total} sont INATTEIGNABLES : "
                f"l'amont n'en rend que {len(items)} et ne donne AUCUN curseur "
                "(plafond du produit LinkedIn — mesuré à 25 sur 86 en "
                "api='sales_navigator'). Ce n'est PAS un balayage complet : ne conclus "
                "rien sur les profils non vus ; resserre la recherche (facette plus "
                "fine, découpage par localisation/intitulé, autre `api=`) pour faire "
                "tenir la population sous le plafond.")
    if facettes and not items:
        out.append(
            f"0 résultat AVEC facette(s) {', '.join(facettes)} : une facette peut ne PAS "
            "être appliquée par l'amont, sans erreur ni indicateur (mesuré : même cible "
            "et même facette employeur → 0 en api='sales_navigator', 10 en "
            "api='classic'). Un zéro ici ne prouve pas un vivier vide — recoupe avec un "
            "autre `api=` ou en mots-clés avant de le rapporter.")
    if page_suivante and facettes:
        out.append(
            f"pagination CURSOR-ONLY : les filtres repassés avec `cursor` "
            f"({', '.join(facettes)}) ne sont PAS ré-appliqués — seul le curseur porte la "
            "requête amont, et la page 2 PERD parfois le filtre employeur (mesuré en "
            "api='classic' : profils sans rapport). Vérifie l'employeur de chaque item "
            "de cette page avant de l'exploiter.")
    return out


def _slim_search(res, *, facettes=(), page_suivante=False):
    """Allège une réponse de recherche (feedback #335, coût token ÷~2 en bulk) :
    - dé-duplique `data`/`items` et `next_cursor`/`cursor` — oto-core `_norm` renvoie les
      DEUX (même liste) pour la stabilité de l'aval ; l'agent n'a besoin QUE de `items`/`cursor` ;
    - retire de chaque résultat les URLs d'image (`*picture_url*` : photo + large + fond) =
      poids mort en recherche (l'agent ne rend pas d'images ; un profil précis → linkedin_unipile_profile).
    Ne touche à RIEN d'autre (tous les champs métier restent).

    ...et DIT ce que la page ampute (#536) : `returned`/`truncated` dès que
    `items < total_count`, plus des `warnings` en clair quand le résultat ne se
    lit pas au premier degré (plafond sans curseur, zéro sur facette, page
    obtenue par curseur). L'enveloppe ne grossit QUE s'il y a quelque chose à
    avouer — une recherche complète reste aussi légère qu'avant."""
    if not isinstance(res, dict):
        return res
    items = res.get("items")
    if items is None:
        items = res.get("data") or []
    for it in items:
        if isinstance(it, dict):
            for k in [k for k in it if "picture_url" in k.lower()]:
                it.pop(k, None)
    cursor = res.get("cursor") or res.get("next_cursor")
    total = res.get("total_count")
    out = {"items": items, "cursor": cursor}
    if total is not None:
        out["total_count"] = total
    if isinstance(total, int) and len(items) < total:
        out["returned"] = len(items)
        out["truncated"] = True
    alertes = _alertes_recherche(items, total, cursor, tuple(facettes), page_suivante)
    if alertes:
        out["warnings"] = alertes
    return out


def _canonical_li_identifier(identifier: str) -> str:
    """Canonicalise un `public_identifier` LinkedIn (vanity slug) : LinkedIn le
    génère TOUJOURS en ASCII (translittère les accents à la création, p. ex.
    `renée-lefèvre` → `renee-lefevre`). Un slug accentué saisi par l'agent
    fait renvoyer à l'API Unipile un 403 « Insufficient permissions » TROMPEUR
    (#180) → on retire les diacritiques avant l'appel. No-op sur un slug déjà ASCII
    ou un provider_id opaque (`ACoAA…`, sans accent) — idempotent."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", identifier)
        if not unicodedata.combining(c)
    )


def _slim(payload, fields: Optional[list[str]] = None,
          text_max_chars: Optional[int] = None,
          *, keep_always: tuple[str, ...] = ("id", "social_id")):
    """Allège une enveloppe de liste Unipile — projection de champs + troncature du texte.

    Pourquoi (signal #281) : `limit=10` sur les posts d'un membre rend 55 à 75 Ko — URLs
    d'images en triple, urns, jetons de partage — pour un besoin qui est presque toujours
    « balayer les derniers posts de X et voir si l'un colle ». Le payload basculait en
    fichier à chaque appel, donc il fallait un second outil (jq) pour trier. Avec une
    projection et un extrait de texte, le triage tient en UN appel léger.

    Ne touche QUE les items : l'enveloppe (`cursor`, `total_count`) est préservée, sinon
    la pagination casserait. `fields` garde toujours de quoi ADRESSER l'item ensuite
    (`keep_always` — `id`/`social_id` pour un item Unipile brut, `_id`/`urn` pour une
    ligne du miroir de feed) : projeter jusqu'à rendre le résultat inutilisable serait
    pire que de tout renvoyer."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return payload
    if not fields and not text_max_chars:
        return payload
    keep = set(fields or ()) | set(keep_always) if fields else None

    def _one(it):
        if not isinstance(it, dict):
            return it
        out = {k: v for k, v in it.items() if k in keep} if keep else dict(it)
        for champ in _TEXTUAL_FIELDS:
            v = out.get(champ)
            if text_max_chars and isinstance(v, str) and len(v) > text_max_chars:
                out[champ] = v[:text_max_chars] + "…"
                out[f"{champ}_truncated"] = True
        return out

    payload = dict(payload)
    payload["items"] = [_one(i) for i in payload["items"]]
    if "data" in payload and isinstance(payload.get("data"), list):
        payload["data"] = payload["items"]   # `_norm` aliase les deux : garder cohérent
    return payload


def _shape_feed(payload: dict, fields: Optional[list[str]],
                text_max_chars: Optional[int]) -> dict:
    """Met la page de feed à la taille d'un résultat d'outil (signal #384).

    Trois régimes, et le résultat DIT toujours lequel s'applique :
    - `fields` omis → la vue de tri `_FEED_DEFAULT_FIELDS` ;
    - `fields=["*"]` → toutes les colonnes du miroir (chemin vers le brut) ;
    - `fields=[…]` → exactement ces colonnes — **même sémantique que `data_rows`**
      (les colonnes d'adressage restent, une colonne inconnue est signalée sans bloquer).

    Le bloc `projection` n'est posé que si quelque chose a été rogné : il nomme ce qui
    manque et comment l'obtenir, pour qu'un défaut qui résume ne devienne jamais un
    défaut qui cache."""
    items = payload["items"]
    present = {k for it in items if isinstance(it, dict) for k in it}

    if fields is not None and not fields:
        # Ni « tout », ni « rien », ni la vue de tri : demande ambiguë. On refuse au
        # lieu de choisir à la place de l'appelant (un `fields=[]` avalé rendrait
        # SILENCIEUSEMENT plus que le défaut, l'inverse de l'intention).
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "`fields` est une liste vide : omets-le pour la vue de tri, passe les "
            "colonnes voulues, ou `['*']` pour toutes les colonnes du miroir.")))
    if text_max_chars is not None and text_max_chars <= 0:
        # Même piège : 0 est faux en Python, donc « aucune limite » — soit l'inverse
        # de ce que demande qui écrit `text_max_chars=0`.
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "`text_max_chars` doit être > 0 (ou `None` pour le texte intégral) — "
            "`0` ne veut pas dire « pas de texte ».")))

    if fields is None:
        keep: Optional[list[str]] = list(_FEED_DEFAULT_FIELDS)
    elif "*" in fields:
        keep = None
    else:
        keep = list(fields)

    out = _slim(payload, keep, text_max_chars, keep_always=_FEED_ADDRESSING)

    rendered = {k for it in out["items"] if isinstance(it, dict) for k in it}
    omitted = sorted(present - rendered)
    if omitted or text_max_chars:
        out["projection"] = {
            "omitted_fields": omitted,
            "text_max_chars": text_max_chars,
            "hint": "vue de tri. Toutes les colonnes : fields=['*'] — texte intégral : "
                    "text_max_chars=None — un post entier : op='get' (post_id=<urn>) ou "
                    "data_rows('linkedin-feed', id=<urn>).",
        }
    if fields and keep is not None:
        unknown = [f for f in fields if f not in present]
        if unknown and items:
            out["warning"] = (
                "colonne(s) de `fields` inconnue(s) dans le miroir du feed : "
                f"{', '.join(unknown)} — vérifie l'orthographe (absentes du résultat)")
    return out


def _feed_ttl_seconds() -> int:
    try:
        return int(os.environ.get("OTO_UNIPILE_FEED_TTL_SECONDS", "600"))
    except ValueError:
        return 600


def _feed_is_stale(sub: str, provider: str = "LINKEDIN") -> bool:
    """True si le cache du feed mérite un refresh (jamais sync, ou plus vieux que
    le TTL). Tolérant au format d'horodatage (string row-factory)."""
    ts = db.get_unipile_feed_synced_at(sub, access.current_org(sub), provider)
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= _feed_ttl_seconds()


def _sync_feed(client, store, sub: str, provider: str = "LINKEDIN") -> int:
    """Pagine le feed live et upsert chaque post dans le datastore (dédup par
    `urn`). S'arrête dès qu'une page entière n'apporte AUCUN urn nouveau (condition
    robuste à l'ordre de tri) ou au cap de pages. Renvoie le nombre de posts neufs.
    Marque le sync à la fin. Best-effort : un item sans urn est ignoré."""
    new_count = 0
    cursor = None
    for _ in range(_FEED_SYNC_CAP_PAGES):
        page = client.get_feed(count=_FEED_PAGE_COUNT, cursor=cursor,
                               sort_order=_FEED_SORT_ORDER)
        items = page.get("items") or []
        if not items:
            break
        page_new = 0
        for item in items:
            urn = item.get("urn")
            if not urn:
                continue
            _row, inserted = store.upsert_row(_FEED_NS, urn, item)
            if inserted:
                page_new += 1
        new_count += page_new
        cursor = page.get("cursor")
        if page_new == 0 or not cursor:
            break  # rattrapé (page déjà connue) ou fin de flux
    db.touch_unipile_feed_synced(sub, access.current_org(sub), provider)
    return new_count


# Canaux Unipile : clé front → provider DB. Source unique de la liste de canaux
# (consommée par status_for ; calquée côté front dans ConnectorHostedWidget).
UNIPILE_CHANNELS = {
    "linkedin": "LINKEDIN", "whatsapp": "WHATSAPP", "telegram": "TELEGRAM",
    "instagram": "INSTAGRAM", "messenger": "MESSENGER", "twitter": "TWITTER",
}


def _channels_from(accts_by_provider: dict) -> dict:
    """Construit le dict des 6 canaux à partir des comptes indexés par provider DB."""
    def _ch(provider: str) -> dict:
        a = accts_by_provider.get(provider)
        return {
            "connected": a is not None,
            "account_id": a["account_id"] if a else None,
            "account_name": a.get("account_name") if a else None,
            "connected_at": str(a["connected_at"]) if a else None,
        }
    return {front: _ch(prov) for front, prov in UNIPILE_CHANNELS.items()}


def status_for(sub: str, *, org=access._UNSET, group=access._UNSET) -> dict:
    """État Unipile per-user : canaux connectés + option débloquée + mode de clé.
    SOURCE UNIQUE consommée par `/api/me/unipile` (face user). BYO (clé propre
    user/groupe/org) ⇒ option ouverte (l'user gère sa propre instance). Sinon l'option
    de messagerie hébergée doit avoir été accordée à l'org par un admin (comp).
    `org`/`group` explicites = état d'un TIERS contre son propre contexte, sans le
    contexte view-as/session du requérant (anti-fuite, cf. access._UNSET).
    Scope membre (ADR 0033 B4) : `channels` = les canaux liés à CETTE org (le binding
    est un acte par org — modèle explicite, fin du fallback silencieux #221).
    `elsewhere` = la PROPOSITION : canaux non liés ici dont le sub a un siège
    plateforme vivant dans une autre org (même clé partagée ⟹ adoptable au connect)."""
    o = access.current_org(sub) if org is access._UNSET else org
    mode = access.credential_mode_for(sub, "unipile", org=org, group=group)
    byo = mode in access.BYO_MODES
    subscribed = access.option_open(sub, "unipile", org=org, group=group)  # source unique (byo OU option)
    all_accts = db.list_unipile_accounts(sub)
    accts = {a["provider"]: a for a in all_accts if a.get("org_id") == o}
    # Adoption possible ? (mode platform = même clé partout ; l'option gate le connect)
    elsewhere: dict = {}
    if mode == "platform" and subscribed:
        for a in sorted((x for x in all_accts
                         if x.get("platform_seat") and x.get("org_id") != o),
                        key=lambda x: str(x.get("connected_at") or ""), reverse=True):
            if a["provider"] not in accts:
                elsewhere.setdefault(a["provider"], {
                    "account_id": a["account_id"],
                    "account_name": a.get("account_name"),
                    "org_id": a.get("org_id"),
                })
    front_by_provider = {prov: front for front, prov in UNIPILE_CHANNELS.items()}
    return {
        "subscribed": subscribed,   # option débloquée (BYO ou comp admin) — gate « connecter »
        "mode": mode,  # user|group|org|platform|over_quota|forbidden (origine de la clé)
        "byo": byo,
        "channels": _channels_from(accts),
        # par canal front : compte du sub connecté AILLEURS, adoptable ici en un clic
        # (le bouton Connect adopte côté backend — l'UI peut l'annoncer).
        "elsewhere": {front_by_provider[p]: v for p, v in elsewhere.items()
                      if p in front_by_provider},
    }


def account_status(provider: str = "LINKEDIN") -> dict:
    """« Mon compte {provider} est-il connecté, et sa session est-elle vivante ? »

    Né du signal **#452** (org 2, 14/08/2026). Le NOM `linkedin_unipile_account`
    promet l'état du compte ; l'outil ne servait que l'ardoise premium (contrats
    Recruiter / Sales Navigator). Un agent venu vérifier « mon LinkedIn est-il
    connecté ? » a inventé `op='status'`, s'est pris un `invalid_arguments` (appel
    248959, args `{op:'status'}`) et en a conclu « pas connecté » — alors que le canal
    l'était, et un utilisateur a signalé « ça ne marche pas ».

    Deux contraintes, toutes deux tirées de ce mode de panne :

    - **Ça RÉPOND, ça ne lève pas.** Sans compte lié, `unipile_client()` lève une
      McpError : bâtir le statut dessus aurait remplacé un faux négatif par une
      erreur, c'est-à-dire rien changé. Ici, « pas connecté » est une RÉPONSE.
    - **Ça résout comme un vrai appel.** `resolve_operated_account_id` est
      exactement ce que `unipile_client()` emprunte (pin `_account=`, compte
      accordé #55, compte propre de l'org) — donc « status dit connecté » implique
      « un appel trouvera un compte ». Une autre lecture recréerait la carte qui
      rassure pendant que les appels échouent.

    `connected` ≠ `alive` : un compte reste LIÉ en base alors que sa session est
    morte (checkpoint, cookie tourné — #236), et c'est précisément l'état où une
    carte verte trompe le plus. `alive=None` = sonde indisponible, pas « morte ».
    """
    from ..connectors import identities as connector_identities
    from ..connectors import readiness as connector_readiness

    sub = access.current_user_sub_or_raise()
    org = access.current_org(sub)
    front = {p: f for f, p in UNIPILE_CHANNELS.items()}.get(provider, provider.lower())

    try:
        account_id = connector_identities.resolve_operated_account_id(sub, provider)
        pointer_error = None
    except ValueError as e:
        # Pointeur « identité opérée » orphelin (grant révoqué, compte déconnecté par
        # son propriétaire) : un vrai appel LÈVE ici. Le statut, lui, le RAPPORTE —
        # c'est le genre d'état qu'on vient justement lui demander.
        account_id, pointer_error = None, str(e)

    label = None
    if account_id:
        label = next((a.get("account_name") for a in db.list_unipile_accounts(sub)
                      if a.get("account_id") == account_id), None)
        if label is None:
            granted = db.granted_accounts_for(sub, provider) or {}
            g = granted.get(account_id)
            label = (g or {}).get("account_name") if isinstance(g, dict) else None

    alive = None
    if account_id:
        try:
            alive = bool(unipile_client(provider).account_alive(account_id))
        except Exception:
            # Sonde indisponible ≠ session morte : on rend `alive=None` et on le dit,
            # plutôt que d'annoncer une panne qu'on n'a pas constatée.
            logger.warning("sonde de liveness unipile indisponible (%s)", provider,
                           exc_info=True)

    out = {"connected": account_id is not None, "account_id": account_id,
           "account_name": label, "channel": provider, "alive": alive}

    if account_id is None:
        # Le geste manquant vient du seam PARTAGÉ (option fermée ? aucune clé ? juste
        # un canal à lier ?) — la même réponse que la carte connecteur, pas une
        # seconde version qui divergerait.
        # Diagnostiquer le CANAL, pas le compte : depuis le split, l'activation, l'ACL
        # et la sélection qui peuvent bloquer CE canal sont les SIENNES. La couche
        # « clé », elle, remonte au compte porteur — `readiness.diagnose` le fait
        # lui-même (`credential_provider`), le message nomme donc la bonne carte.
        canal_con = providers.connector_for_hosted_channel(provider)
        nom = canal_con.name if canal_con else "unipile"
        diag = connector_readiness.diagnose(
            sub, nom, org=org, group=access.current_group(sub))
        out["next_step"] = pointer_error or (
            diag.next_step if diag is not None
            else connector_readiness.no_identity_step(sub, nom, "compte"))
    elif alive is False:
        out["next_step"] = (
            f"Le compte {front} est bien lié, mais sa session est MORTE côté "
            f"fournisseur (checkpoint, mot de passe changé, cookie révoqué) : tout "
            f"appel échouera. Reconnecte-le via `unipile_connect_start`.")
    return out


def _status_pending_action(sub: str, org, group, entry: dict):
    """Hook `status_hints` (seam générique, lot 2) : la clé résout et l'option est
    ouverte, mais AUCUN canal n'est lié → l'étape manquante est « Connecte un
    canal ». La spécificité hosted-account reste ICI, pas dans le modèle commun."""
    if entry.get("mode") == "forbidden":
        return None   # pas de clé → les verdicts « à connecter »/« option » suffisent
    st = status_for(sub, org=org, group=group)
    if not st["subscribed"]:
        return None   # option fermée → le front rend déjà « option requise »
    if any(ch["connected"] for ch in st["channels"].values()):
        return None
    return "Connecte un canal"


status_hints.register("unipile", _status_pending_action)


def _channel_pending_action(canal: str, libelle: str):
    """Hook `status_hints` d'UNE carte de canal (split du 2026-08-28).

    La clé du compte résout et l'option est ouverte, mais CE canal n'est pas lié →
    l'étape manquante est « connecte ton compte X ». Avant le split, le hook était
    posé sur `unipile` et disait « Connecte un canal » tant qu'AUCUN des six ne
    l'était : il se taisait dès le premier connecté, donc quelqu'un qui avait
    LinkedIn ne s'entendait jamais dire qu'il lui restait WhatsApp à brancher. Une
    carte par canal rend la question posable canal par canal — et la réponse utile.

    (Fermé sur `canal` par une fabrique plutôt que par une closure de boucle : six
    hooks qui fermeraient sur la variable d'itération diraient tous le dernier.)"""

    def hook(sub: str, org, group, entry: dict):
        if entry.get("mode") == "forbidden":
            return None   # pas de clé → « à connecter »/« option » suffisent déjà
        st = status_for(sub, org=org, group=group)
        if not st["subscribed"]:
            return None   # option fermée → le front rend déjà « option requise »
        if st["channels"].get(canal, {}).get("connected"):
            return None
        return f"Connecte ton compte {libelle}"

    return hook


# Un hook par carte de canal. `unipile` n'en a plus : sa carte pose une CLÉ, elle
# n'a aucun bouton pour connecter quoi que ce soit — un « Connecte un canal » y
# serait une consigne sans geste.
for _con in providers.REGISTRY.values():
    if _con.hosted_channel:
        status_hints.register(_con.name,
                              _channel_pending_action(_con.hosted_channel.lower(),
                                                      _con.label))
del _con


def admin_status_by_org(sub: str, orgs: list) -> list:
    """État messagerie **par org** pour la fiche admin (un user peut être dans N orgs ;
    l'option est PAR ORG). `orgs` = `org_store.list_orgs_for_user(sub)`.
    Pour chaque org : option/mode calculés CONTRE CETTE org + canaux rattachés à elle
    (`unipile_accounts.org_id`). Les comptes rattachés à une org hors de sa liste tombent
    dans un bloc « (hors de ses orgs) »."""
    accts = db.list_unipile_accounts(sub)
    out = []
    for o in orgs:
        oid = o["org_id"]
        mode = access.credential_mode_for(sub, "unipile", org=oid)
        byo = mode in access.BYO_MODES
        by = {a["provider"]: a for a in accts if a.get("org_id") == oid}
        out.append({
            "org_id": oid, "org_name": o.get("name"), "is_active": bool(o.get("is_active")),
            "subscribed": access.option_open(sub, "unipile", org=oid),  # source unique
            "mode": mode, "byo": byo,
            "channels": _channels_from(by),
            "option_source": {
                "user_comp": db.has_option_comp("user", sub, "unipile"),
                "org_comp": db.has_option_comp("org", str(oid), "unipile"),
            },
        })
    member = {o["org_id"] for o in orgs}
    orphans = {a["provider"]: a for a in accts if a.get("org_id") not in member}
    if orphans:
        out.append({
            "org_id": None, "org_name": "(hors de ses orgs)", "is_active": False,
            "subscribed": None, "mode": None, "byo": None,
            "channels": _channels_from(orphans), "option_source": None,
        })
    return out


def _project_operated_account(anon, provider: str) -> str:
    """Compte à opérer sur un endpoint MCP PUBLIÉ (ADR 0032) — aucun `sub`.

    Le secret d'un endpoint publié authentifie le PROJET, pas une personne : la
    résolution per-membre (`resolve_operated_account_id`) n'a rien à quoi s'accrocher et
    levait « Unauthenticated ». Résultat, un projet partagé avec un tiers perdait
    LinkedIn — la moitié des contacts d'une mission d'enrichissement — et la seule
    alternative était de confier un jeton `oto_` NOMINAL, qui porte l'organisation
    entière (356 outils, `email_send`, `data_delete_namespace`) : indéfendable devant la
    conformité d'un client sous contrat de traitement.

    L'information manquante existait déjà : le projet DÉCLARE ses identités de connecteur
    (`project_links.identity_ref`). On l'utilise — c'est ce qui fait du secret de projet un
    vrai jeton restreint : le périmètre d'un projet, sous les identités qu'il déclare.

    Deux gardes, toutes deux nécessaires :
    - **appartenance** — l'identité doit être un compte VIVANT de l'org propriétaire du
      projet. La clé Unipile partagée adresse tout l'abonnement de la plateforme : sans ce
      recoupement, un lien de projet nommant un `acc_…` quelconque ferait agir un endpoint
      public sous le LinkedIn d'un autre tenant.
    - **canal** — le filtre par `provider` remplace ici la règle « plusieurs bindings ⇒
      ambigu, on abandonne » : un projet qui déclare LinkedIn ET WhatsApp sous le même
      connecteur `unipile` ne dit rien d'ambigu, il déclare deux canaux.

    Jamais de repli : ni sur un autre compte de l'org, ni sur le premier de l'abonnement.
    Un message parti sous la mauvaise identité est irréversible, et le destinataire du
    partage n'a aucun moyen de s'en apercevoir."""
    # ⚠️ DEUX noms de lien à lire depuis le split du 2026-08-28. Un lien écrit
    # AVANT nomme le connecteur `unipile` (il n'y en avait qu'un, et le filtre par
    # canal ci-dessous suffisait à lever l'ambiguïté) ; un lien écrit DEPUIS, depuis
    # la carte du canal, nomme le canal. Ne lire que l'un des deux casse la moitié
    # des projets — les anciens ou les nouveaux selon le nom retenu — et le casse en
    # silence, puisque l'absence de lien se rend comme « ce projet ne déclare aucun
    # compte ». Les liens ne sont volontairement PAS migrés : ils portent une
    # identité choisie par une personne, et les deux noms restent vrais.
    canal_con = providers.connector_for_hosted_channel(provider)
    noms_de_lien = ["unipile"] + ([canal_con.name] if canal_con else [])
    declared = [a for nom in noms_de_lien
                for a in access.project_declared_identities(nom, anon.project_id)]
    if not declared:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Ce projet partagé ne déclare aucun compte {provider.title()}. Son "
                     "propriétaire doit lier le connecteur AVEC une identité "
                     f"(`oto_project op=link target_type=connecteur "
                     f"target_ref={noms_de_lien[-1]} identity_ref=<account_id>`) pour "
                     "que l'endpoint puisse agir.")))
    # Dédupliqué (ordre stable) : un projet qui déclare la MÊME identité sous les
    # deux noms de lien déclare UN compte, pas deux — sans ça le garde-fou
    # « plusieurs comptes ⟹ je ne devine pas » se déclencherait sur un projet
    # parfaitement univoque, simplement parce qu'il a été relié après le split.
    joignables = db.org_unipile_account_ids(anon.org_id, provider)
    usable = list(dict.fromkeys(a for a in declared if a in joignables))
    if not usable:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Le compte {provider.title()} déclaré par ce projet n'est pas (ou "
                     "plus) un compte connecté de l'organisation propriétaire — "
                     "déconnecté, ou lié à une autre organisation. Pas de repli sur un "
                     "autre compte : le propriétaire doit rétablir le lien.")))
    if len(usable) > 1:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Ce projet déclare {len(usable)} comptes {provider.title()} "
                     f"({', '.join(sorted(usable))}). Un endpoint publié n'a personne à "
                     "qui demander lequel : le propriétaire doit n'en garder qu'un pour "
                     "ce canal.")))
    # `_account=` reste lisible sur cette surface (l'axe ne demande pas de `sub`). On ne
    # l'ignore PAS en silence — avaler un jeton de contexte fait agir sous une autre
    # identité que celle demandée, le mode de panne que le préfixe `_` corrigeait
    # (#250) : on l'accepte s'il redit l'identité du projet, on refuse sinon. Le
    # destinataire d'un partage ne choisit pas sous quel compte il opère.
    pin = session_org.current_call_account()
    if pin and pin != usable[0]:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("`_account=` n'est pas recevable sur un endpoint de projet publié : "
                     "l'identité vient du projet, pas de l'appelant. Retire le jeton.")))
    return usable[0]


def unipile_client(provider: str = "LINKEDIN"):
    """Client Unipile du user pour un canal (LINKEDIN, WHATSAPP, …).

    Clé partagée (org) + account_id per-user PAR CANAL : chacun agit comme
    LUI-MÊME sous l'abonnement Unipile commun. PAS de fallback : sans account_id
    connecté pour ce canal, le client oto-core retomberait sur le 1er compte de
    l'abonnement → **usurpation cross-user** (audit sécu 2026-06-18). On exige le
    credential per-user, sinon McpError actionnable. Réutilisé par tools/whatsapp.py.

    SEULE exception (#55) : un compte ACCORDÉ par son propriétaire
    (`connector_account_grants`, revalidé à CHAQUE appel — révocation immédiate),
    résolu par `connector_identities.resolve_operated_account_id`. Limitation : si
    le owner est sur une AUTRE clé Unipile que le grantee (BYO perso ≠ clé
    partagée), l'API Unipile répondra 404 sur l'account_id — erreur surfacée telle
    quelle (la clé résolue est indépendante du compte).
    """
    from oto.tools.unipile import make_unipile_client
    from .. import subdomain_project
    from ..connectors import identities as connector_identities
    # Résolution sous le connecteur du CANAL (split du 2026-08-28), pas sous
    # `unipile` : c'est ce qui fait passer l'appel par les gates DE CE CANAL —
    # `require_connector_access` (ACL d'org, backstop dur) est appliqué sur le nom
    # que la résolution reçoit. Résoudre sous `unipile` gaterait les six canaux
    # ensemble et une org qui a réservé WhatsApp à un département verrait le gate
    # muet. La CLÉ, elle, reste celle du compte : la délégation
    # (`Connector.credential_of`) la ramène sur `unipile` dans la cascade.
    # Canal hors registre ⟹ on retombe sur le porteur (comportement d'avant).
    canal_con = providers.connector_for_hosted_channel(provider)
    rc = access.resolve_credential(canal_con.name if canal_con else "unipile",
                                   want="auto")
    anon = subdomain_project.current_anon_context()
    if anon is not None:
        return make_unipile_client(
            api_key=rc.key, dsn=(None if rc.is_platform else rc.config.get("dsn")),
            account_id=_project_operated_account(anon, provider), provider=provider)
    sub = access.current_user_sub_or_raise()
    try:
        account_id = connector_identities.resolve_operated_account_id(sub, provider)
    except ValueError as e:  # pointeur opéré révoqué/déconnecté → erreur explicite
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    # Pin projet (#57) : si le projet actif épingle un compte unipile, il prime sur le
    # défaut per-canal — MAIS seulement s'il appartient à CE user DANS CETTE org
    # (anti-usurpation + scope membre ADR 0033) OU lui est accordé par son propriétaire
    # (#55, grant vivant re-checké à cet appel), ET au canal demandé. Sinon défaut (fail-soft).
    org = access.current_org(sub)
    # Même dualité de nom qu'au chemin anonyme (cf. `_project_operated_account`) :
    # le canal d'abord — un lien posé depuis SA carte est le plus spécifique —, le
    # compte ensuite pour les liens d'avant le split.
    canal_con = providers.connector_for_hosted_channel(provider)
    pinned = ((access.project_pinned_identity(canal_con.name) if canal_con else None)
              or access.project_pinned_identity("unipile"))
    if pinned and (
        any(a.get("account_id") == pinned and a.get("provider") == provider
            and a.get("org_id") == org
            for a in db.list_unipile_accounts(sub))
        or pinned in db.granted_accounts_for(sub, provider)
    ):
        account_id = pinned
    if not account_id:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Connecte ton compte {provider.title()} sur "
                    "https://manage.oto.cx/console/connections "
                    "avant d'utiliser ces outils."))
    # DSN tiré de la config du credential résolu (défaut api.unipile.com côté
    # oto-core). Clé plateforme → DSN env/défaut (instance Otomata).
    dsn = None if rc.is_platform else rc.config.get("dsn")
    # `provider` = le CANAL du compte opéré. Il ne sert pas qu'à documenter : la
    # messagerie Unipile v2 a deux formes d'endpoint (par inbox pour LinkedIn, à plat
    # pour les cinq autres canaux) et l'amont répond 501 à la mauvaise. Sans lui,
    # oto-core suppose LinkedIn et paie un aller-retour 501 avant de se rattraper.
    return make_unipile_client(api_key=rc.key, account_id=account_id, dsn=dsn,
                               provider=provider)


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde de connexion Unipile (#133) : `list_accounts()` sur la clé résolue.

    Teste l'auth ET le contenu d'un coup — un endpoint compte-agnostique donc pas
    besoin d'account_id. On distingue trois cas :
    - clé absente → message actionnable (ne devrait pas arriver : `_fields_for`
      résout le credential en amont, mais on garde le garde-fou) ;
    - clé morte / refusée → `UnipileError` (401/4xx) laissée remonter telle quelle
      (son message = le retour d'erreur de la sonde) ;
    - clé valide mais AUCUN compte connecté → distinct d'un listing cassé, on lève
      un message qui oriente vers le hosted-auth du dashboard."""
    from oto.tools.unipile import make_unipile_client

    # ⚠️ Le champ dérivé de `secret_kind="api_key"` se nomme `key` (cf.
    # providers.secret_fields) — lire `api_key` ici rendait la sonde AVEUGLE
    # (« clé absente » systémique quel que soit le coffre, vécu 2026-07-08 :
    # diagnostiqué à tort comme clé plateforme manquante).
    api_key = fields.get("key")
    if not api_key:
        raise ValueError("clé API Unipile absente.")
    cfg = config or {}
    # dsn apparié à la clé (défaut api.unipile.com côté oto-core).
    client = make_unipile_client(api_key=api_key, dsn=cfg.get("dsn"))
    accounts = client.list_accounts()
    if not accounts:
        raise ValueError(
            "clé Unipile valide mais aucun compte connecté — connecte un compte "
            "via le hosted-auth du dashboard (unipile_connect_start).")


def register_messaging_tools(mcp: FastMCP, channel: str) -> None:
    """Enregistre L'outil de messagerie Unipile d'un canal : `{c}_chat(op=…)`,
    résolu sur le compte <channel> de l'user (no-fallback). La messagerie Unipile
    (`/chats`) est channel-agnostic → un seul code pour tous les canaux. Appelé par
    tools/{whatsapp,telegram,instagram,messenger,twitter}.py.

    Le canal reste dans le NOM (c'est ce qui le rend trouvable par l'agent) ; le
    verbe passe en `op` — même forme que `linkedin_unipile_chat`, qui est la même
    capacité sur le même connecteur (ADR 0047 §Amendement). 3 tools × 5 canaux
    (15) → 5."""
    cl = channel.lower()
    prov = channel.upper()

    @mcp.tool(
        name=f"{cl}_chat",
        description=(
            f"Messagerie {channel} (DM) via Unipile.\n\n"
            "`op` :\n"
            "- **\"list\"** (défaut) : les conversations, paginé (`limit` + `cursor`). "
            "Chaque fil 1-à-1 est enrichi du nom de l'interlocuteur (`attendee_name`) ; "
            "`with_names=False` coupe cet enrichissement (payload brut, un appel API en moins).\n"
            "- **\"read\"** : les messages d'un fil (`chat_id` d'op=\"list\").\n"
            "- **\"send\"** : envoie un message. `chat_id` → répond dans un fil existant ; "
            "sinon `recipient_id` → ouvre un nouveau fil."),
    )
    def _chat(op: Literal["list", "read", "send"] = "list",
              chat_id: Optional[str] = None,
              text: Optional[str] = None,
              recipient_id: Optional[str] = None,
              limit: Optional[int] = None,
              cursor: Optional[str] = None,
              with_names: bool = True) -> dict:
        client = unipile_client(prov)

        def _bad(msg: str) -> McpError:
            return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

        if op == "list":
            return client.list_chats(limit=limit if limit is not None else 20,
                                     cursor=cursor, with_attendee_names=with_names)
        if op == "read":
            if chat_id is None:
                raise _bad("op='read' requiert chat_id")
            return client.list_messages(chat_id,
                                        limit=limit if limit is not None else 30)
        if op == "send":
            if text is None:
                raise _bad("op='send' requiert text")
            if chat_id is None and recipient_id is None:
                raise _bad("op='send' requiert chat_id (répondre) ou recipient_id "
                           "(nouveau fil)")
            return client.send_message(text, chat_id=chat_id, attendee_id=recipient_id)
        raise _bad("op doit être 'list', 'read' ou 'send'")


def register(mcp: FastMCP) -> None:

    connector_verify.register("unipile", _verify)

    @mcp.tool()
    async def unipile_connect_start(channel: str = "linkedin",
                                    force: bool = False,
                                    premium: Optional[str] = None) -> dict:
        """Démarre la connexion d'un compte de messagerie hébergé (LinkedIn par
        défaut) et renvoie une **`url`** d'auth Unipile à transmettre à l'utilisateur.

        L'utilisateur ouvre l'URL, se connecte à son compte (login/2FA/captcha —
        tout se passe dans cette page hébergée) ; la liaison se **finalise
        automatiquement** côté serveur (webhook), rien d'autre à appeler ensuite.
        Vérifie l'état avec `oto_instance(op='verify', connector='unipile')`. C'est LE
        point d'entrée d'onboarding messagerie depuis l'agent (feedback #131).

        Un compte de messagerie est PAR-PERSONNE : s'il est déjà connecté dans une
        autre de tes orgs, il te suit ici (inutile de reconnecter) et cet appel
        refuse par défaut pour éviter un doublon. Ne passe `force=True` que pour
        connecter un compte RÉELLEMENT différent.

        ⚠️ **LinkedIn premium** : par défaut seul le produit `classic` est connecté.
        Si la personne a un siège **Recruiter** ou **Sales Navigator** et veut s'en
        servir (`linkedin_unipile_search(api="recruiter"/"sales_navigator")`,
        `linkedin_unipile_account(op="contracts")`…),
        il FAUT le demander ICI via `premium` — sinon ces APIs répondent 403 « out of
        your scope ». Les deux sont **exclusifs**. Pour AJOUTER un produit à un compte
        DÉJÀ connecté (classic seul aujourd'hui), relance avec `premium=` — et
        `force=True` si le garde anti-doublon bloque : le siège existant est
        **reconnecté** (produit rattaché, PAS de doublon). Si Recruiter répond quand
        même 403 après ça, c'est côté abonnement Unipile plateforme (API Recruiter à
        activer), pas la connexion.

        Args:
            channel: canal à connecter — linkedin (défaut), whatsapp, telegram,
                instagram, messenger, twitter.
            force: connecter malgré un compte déjà lié à ce canal ailleurs (#172).
            premium: produit LinkedIn premium à activer — "recruiter" ou
                "sales_navigator" (exclusifs, un seul par compte). À ne demander que
                si la personne a bien le siège LinkedIn correspondant. Ajoute aussi
                la connexion par cookies au wizard (recommandé pour ces produits).
        """
        from .. import unipile_connect

        sub = access.current_user_sub_or_raise()
        try:
            out = await unipile_connect.hosted_auth_url(sub, channel, force=force,
                                                        premium=premium)
        except unipile_connect.ConnectRefused as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=e.message))
        if out.get("adopted"):
            # Binding-par-org : le compte déjà connecté ailleurs (même clé partagée)
            # vient d'être lié à l'org courante — aucun lien à ouvrir.
            out["instructions"] = (
                f"Compte {out.get('channel', channel)} déjà connecté ailleurs : il vient "
                "d'être activé pour cette org — rien d'autre à faire, les outils sont "
                "utilisables immédiatement.")
            return out
        out["instructions"] = (
            f"Transmets `url` à l'utilisateur : il ouvre le lien, connecte son compte "
            f"{out.get('channel', channel)}, et la liaison se finalise seule "
            "(webhook). Vérifie ensuite avec oto_instance(op='verify', connector='unipile').")
        return out

    # ---- helpers de dispatch (patron `op=`, ADR 0047) --------------------

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _need(value, name: str, op: str):
        """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback."""
        if value is None:
            raise _bad(f"op='{op}' requiert {name}")
        return value

    # ---- recherche -------------------------------------------------------

    @mcp.tool()
    def linkedin_unipile_search(
        keywords: Optional[str] = None,
        category: str = "people",
        company: Optional[list[str]] = None,
        location: Optional[list[str]] = None,
        industry: Optional[dict] = None,
        network_distance: Optional[list[int]] = None,
        advanced_keywords: Optional[dict] = None,
        skills: Optional[list] = None,
        url: Optional[str] = None,
        api: str = "classic",
        cursor: Optional[str] = None,
    ) -> dict:
        """Recherche LinkedIn via Unipile.

        ⚠️ **Recherche Recruiter / Sales Navigator par facettes** (compétences,
        secteur, localisation, employeur) : lis d'abord le guide
        `oto_guide(op=read, slug="linkedin-search")`. Quatre pièges qui FAUSSENT en
        silence : (1) une facette exige un **ID résolu** — passe le terme par
        `linkedin_unipile_facets` et donne l'`id` choisi ; un terme brut NE filtre PAS ;
        (2) le mode `url=` est **plafonné à 25 sans pagination** — préfère le structuré ;
        (3) une facette peut n'être **PAS appliquée** par le produit choisi, sans erreur
        (mesuré : même employeur → 0 en `sales_navigator`, 10 en `classic`) — un **0 sur
        recherche à facettes ne prouve pas un vivier vide**, recoupe ;
        (4) la pagination est **CURSOR-ONLY** : les filtres repassés à côté du `cursor`
        ne sont pas ré-appliqués, et la page 2 **perd parfois le filtre employeur** —
        contrôle l'employeur des items d'une page paginée.

        **Le retour DIT ce qu'il ampute** (#536) : `total_count` (population annoncée)
        vs `returned` + `truncated: true` dès que la page en rend moins, et des
        `warnings` en clair quand le résultat ne se lit pas au premier degré. `truncated`
        **sans** `cursor` = le reste est INATTEIGNABLE (plafond produit, ex. 25 sur 86) :
        ne conclus rien sur les non-vus, resserre la recherche. Lis ces champs avant de
        rapporter « vivier vide » ou « population balayée ».

        ⚠️ **Cadence** : LinkedIn rate-limite par compte. Enchaîner des dizaines
        d'appels en rafale déclenche un `429`, puis DÉGRADE et finit par DÉCONNECTER
        le compte. Le backoff demandé SUIT ta cadence récente : quelques secondes
        après une rafale légère, jusqu'à ~1h derrière un enchaînement soutenu — lis
        le délai renvoyé, ne suppose pas qu'il est court. Espace tes appels ; sur un
        `429`, respecte CE délai et RALENTIS — n'insiste pas.
        Pour du volume, délègue la pagination à un sous-agent (guide `bulk-load`).

        `company`/`location`/`industry` acceptent des NOMS (résolus automatiquement
        en facettes LinkedIn) ou des ids de facette numériques. ⚠️ La page company
        LinkedIn n'est PAS un id de facette employeur valide pour la recherche
        people — passer le nom et laisser le client résoudre.

        Args:
            keywords: Mots-clés (nom, intitulé de poste…).
            category: "people" ou "companies".
            company: Employeur(s) — noms ou ids de facette.
            location: Localisation(s) — noms ou ids de facette.
            industry: filtre secteur — dict `{include?: [...], exclude?: [...]}` (noms ou ids).
                ⚠️ `exclude` n'est PAS supporté par `api="classic"` (lève une erreur) :
                LinkedIn classic n'accepte qu'une liste de secteurs à INCLURE. Pour
                exclure un secteur, utilise `api="sales_navigator"` ou `"recruiter"`.
            network_distance: degré de relation — `[1]`=1er degré (tes relations N1),
                `[2]`=2e, `[3]`=3e+. Combinable (`[1, 2]`) → cible « mes N1 sur [ville] ».
            advanced_keywords: ciblage people — dict `{first_name?, last_name?, title?,
                company?, school?}`.
            skills: filtre compétences (Recruiter / Sales Nav) — liste de noms OU
                d'ids de facette (résous d'abord via `linkedin_unipile_facets(
                facet_type="SKILL", …)` et passe l'`id` choisi). Accepte aussi un dict
                `{include?, exclude?}` (exclusion = `priority DOESNT_HAVE`).
            url: URL de recherche LinkedIn collée du navigateur (classic / Sales
                Navigator). Si fournie, les autres filtres structurés sont ignorés ;
                passe `api=` du produit de l'URL. ⚠️ **Recruiter-from-URL est
                actuellement peu fiable côté Unipile** (l'endpoint pend → timeout,
                même avec un searchContextId neuf) : pour Recruiter, préfère la
                recherche STRUCTURÉE ci-dessous (`api="recruiter"` + keywords/facettes),
                pas l'URL.
            api: "classic" | "sales_navigator" | "recruiter" (filtres avancés selon
                l'abonnement LinkedIn du compte connecté). Recruiter/Sales Nav exigent
                le siège premium activé au connect (sinon 403 « out of scope »).
            cursor: Curseur de pagination renvoyé par un appel précédent.
        """
        sub = _actor_key()
        poses = dict(company=company, location=location, industry=industry,
                     skills=skills, network_distance=network_distance,
                     advanced_keywords=advanced_keywords)
        facettes = tuple(n for n in _FACETTES_RECHERCHE if poses.get(n))
        return _slim_search(_scrape(sub, lambda: unipile_client().search(
            keywords=keywords, category=category, company=company, location=location,
            industry=industry, network_distance=network_distance,
            advanced_keywords=advanced_keywords, skills=skills, url=url, api=api,
            cursor=cursor,
        )), facettes=facettes, page_suivante=bool(cursor))

    @mcp.tool()
    def linkedin_unipile_facets(facet_type: str, keywords: str, limit: int = 25) -> dict:
        """Résout un NOM de filtre LinkedIn en candidats `{id, name}` à passer à
        `linkedin_unipile_search`. À utiliser AVANT une recherche structurée dès qu'un
        critère n'est pas un simple mot-clé (compétence, secteur, localisation,
        employeur…).

        Le choix du bon candidat est TON travail : une même saisie renvoie souvent
        plusieurs facettes (« Microsoft Excel » → Excel, Microsoft Office, …) —
        lis les `name` et retiens l'`id` pertinent. Puis passe-le à
        `linkedin_unipile_search` (`location`/`company`/`industry` acceptent déjà les
        ids ; les autres facettes arrivent — cf. guide `linkedin-search`).

        Renvoie `{facet_type, candidates: [{id, name}]}`. Résolution INDÉPENDANTE du
        produit/contrat (marche même hors Recruiter/Sales Nav).

        Args:
            facet_type: type de facette, MAJUSCULES. Confirmés : `SKILL`, `LOCATION`,
                `INDUSTRY`, `COMPANY`. D'autres existent (essaie `TITLE`, `SCHOOL`,
                `FUNCTION`, `SENIORITY`, `LANGUAGE`…) — un type invalide lève une
                erreur `Expected kind 'StringEnum'`.
            keywords: le libellé à résoudre (ex. « Microsoft Excel », « Paris »).
            limit: nb max de candidats (défaut 25).
        """
        cands = unipile_client().resolve_facet(str(facet_type).upper(), keywords, limit=limit)
        return {"facet_type": str(facet_type).upper(), "candidates": cands}

    # ---- membres & sociétés : lire un profil, son activité, agir dessus ---

    @mcp.tool()
    def linkedin_unipile_profile(
        op: Literal["person", "company", "me", "posts", "comments", "reactions",
                    "followers", "following", "endorse", "action"] = "person",
        identifier: Optional[str] = None,
        sections: str = "*",
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        fields: Optional[list[str]] = None,
        text_max_chars: Optional[int] = _TEXT_EXCERPT_CHARS,
        skill_endorsement_id: Optional[int] = None,
        api: Optional[str] = None,
        action: Optional[str] = None,
        hiring_project_id: Optional[str] = None,
        stage: Optional[str] = None,
        list_id: Optional[str] = None,
    ) -> dict:
        """Un membre ou une société LinkedIn : lire son profil, son activité, agir dessus.

        `identifier` = **slug public** (`marie-dupont`) ou **URN** (`ACoAA…`). PAS le
        `member_id` numérique de `linkedin_unipile_search` : l'API v2 le rejette
        (`400 Invalid User ID`) — passe par le slug, que ces ops résolvent pour toi.

        `op` :
        - **"person"** (défaut) : profil complet (carrière datée, écoles, réseau).
          ⚠️ LinkedIn peut throttler une section (souvent `experience`) : la réponse
          porte alors `throttled_sections=[…]` avec la section vide malgré un
          `*_total_count` > 0. C'est un rate-limit AMONT, pas une absence de donnée :
          réessaie plus tard (minutes), réduis la concurrence (≤8 en parallèle), et
          sur un batch traite ces cibles dans une passe de rattrapage différée.
          VÉRIFIE aussi que le `public_identifier`/id renvoyé == demandé avant
          d'écrire (rejette + retry sinon).
        - **"company"** : fiche société. Mise en cache 6h par compte (fiches
          ~statiques) — une même société relookée ne consomme pas le quota amont
          (~100 fiches/12h par compte).
        - **"me"** : profil du compte connecté lui-même (le « moi » sous lequel les
          autres ops agissent). Aucun `identifier`.
        - **"posts"** / **"comments"** : ce qu'un membre publie / commente — pour
          repérer un post à commenter/liker, ou ce qu'un prospect engage.
          Le texte de chaque item est servi en EXTRAIT (600 caractères, coupe marquée
          `text_truncated: true`) : le brut fait 55-75 Ko pour 10 posts, et on trie sur
          l'entête. `text_max_chars=None` rend le texte entier, `op="get"` de
          `linkedin_unipile_post` un post précis. Pour alléger encore, `fields`
          (ex. `["text","posted_at","social_id"]`) ne garde que ces champs — `id`/
          `social_id` restent toujours là pour enchaîner.
        - **"reactions"** : posts qu'un membre a likés/aimés.
        - **"followers"** / **"following"** : followers du compte connecté, ou d'un
          membre via `identifier`. Paginé.
        - **"endorse"** : recommande une compétence (`skill_endorsement_id` =
          `endorsement_id` d'une compétence renvoyée par op="person").
        - **"action"** : action premium sur un membre (sauvegarde lead Sales Navigator
          / pipeline Recruiter). Exige `api` + `action`.

        Args:
            op: person (défaut) | company | me | posts | comments | reactions |
                followers | following | endorse | action.
            identifier: slug public ou URN du membre / de la société. Obligatoire
                sauf op="me" ; optionnel pour followers/following (défaut = toi).
            sections: op="person" — sections à inclure ("*" = tout).
            cursor: pagination (posts, comments, reactions, followers, following).
            limit: taille de page.
            fields: op="posts"/"comments" — projection de champs (allège fortement).
            text_max_chars: op="posts"/"comments" — longueur du texte de chaque item
                (défaut 600 ; `None` = texte intégral).
            skill_endorsement_id: op="endorse" — id de la compétence à recommander.
            api: op="action" — 'sales_navigator' ou 'recruiter'.
            action: op="action" — sales_navigator → 'saveLead' ; recruiter →
                'addCandidateToPipeline' | 'addApplicantToPipeline' |
                'changeCandidatePipeline' | 'rejectApplicant'.
            hiring_project_id: op="action" — requis pour les actions pipeline recruiter.
            stage: op="action" — pipeline recruiter : 'UNCONTACTED' | 'CONTACTED' | 'REPLIED'.
            list_id: op="action" — liste Sales Navigator cible (optionnel pour saveLead).
        """
        sub = _actor_key()

        if op == "me":
            return unipile_client().get_own_profile()

        if op == "person":
            ident = _canonical_li_identifier(_need(identifier, "identifier", op))
            return _scrape(sub, lambda: unipile_client().get_profile(ident, sections=sections))

        if op == "company":
            ident = _canonical_li_identifier(_need(identifier, "identifier", op))
            key = (sub, ident.lower())
            hit = _COMPANY_CACHE.get(key)
            if hit and time.time() - hit[0] < _COMPANY_TTL:
                return hit[1]
            res = _scrape(sub, lambda: unipile_client().get_company(ident))
            if len(_COMPANY_CACHE) >= _COMPANY_CACHE_MAX:
                _COMPANY_CACHE.clear()
            _COMPANY_CACHE[key] = (time.time(), res)
            return res

        if op == "posts":
            return _slim(unipile_client().list_member_posts(
                _need(identifier, "identifier", op), cursor=cursor, limit=limit),
                fields, text_max_chars)

        if op == "comments":
            return _slim(unipile_client().list_member_comments(
                _need(identifier, "identifier", op), cursor=cursor, limit=limit),
                fields, text_max_chars)

        if op == "reactions":
            return unipile_client().list_member_reactions(
                _need(identifier, "identifier", op), cursor=cursor, limit=limit)

        if op == "followers":
            return unipile_client().list_followers(user_id=identifier, cursor=cursor,
                                                   limit=limit)

        if op == "following":
            return unipile_client().list_following(user_id=identifier, cursor=cursor,
                                                    limit=limit)

        if op == "endorse":
            return unipile_client().endorse_profile(
                _need(identifier, "identifier", op),
                _need(skill_endorsement_id, "skill_endorsement_id", op))

        if op == "action":
            return unipile_client().member_action(
                _need(identifier, "identifier", op),
                _need(api, "api", op), _need(action, "action", op),
                hiring_project_id=hiring_project_id, stage=stage, list_id=list_id)

        raise _bad("op doit être 'person', 'company', 'me', 'posts', 'comments', "
                   "'reactions', 'followers', 'following', 'endorse' ou 'action'")

    # ---- messagerie ------------------------------------------------------

    @mcp.tool()
    def linkedin_unipile_chat(
        op: Literal["list", "read", "send", "attendees", "contacts", "update",
                    "react"] = "list",
        chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
        text: Optional[str] = None,
        recipient_id: Optional[str] = None,
        action: Optional[str] = None,
        value: Optional[bool | str] = None,
        reaction: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        with_names: bool = True,
    ) -> dict:
        """Messagerie LinkedIn (DM) via Unipile.

        `op` :
        - **"list"** (défaut) : les conversations, paginé (`limit` + `cursor`).
          Chaque fil 1-à-1 est enrichi de `attendee_name`/`attendee_headline`/
          `attendee_profile_url` (résolus en batch — le `name` brut des fils 1-à-1
          est null et `attendee_provider_id` est opaque). `with_names=False` coupe
          cet enrichissement (payload brut, un appel API en moins).
          ⚠️ **L'enrichissement peut ne pas avoir lieu, et la réponse le DIT** : un
          champ `attendee_names` apparaît alors, avec son `status` et sa raison. Une
          absence de `attendee_name` sur un fil ne veut donc PAS dire « pas
          d'interlocuteur » — lis `attendee_names` avant de conclure, et replie-toi
          sur `name` ou `last_message.sender`.
          ⚠️ **Ne te sers PAS de `last_message.is_sender` pour savoir si TU as écrit
          en dernier.** Observé à `false` sur la totalité des fils d'un compte le
          03/09/2026 — y compris les 22 dont le dernier message venait du compte
          lui-même, exactement comme les fils où l'interlocuteur avait répondu. Le
          champ ne distingue rien : s'y fier fait conclure à une réponse sur chaque
          fil, ou l'inverse. Compare `last_message.sender_id` (ou
          `last_message.sender.display_name`) à l'identité du compte.
        - **"read"** : les messages d'un fil (`chat_id`).
        - **"send"** : envoie un message. `chat_id` → répond dans un fil existant ;
          sinon `recipient_id` (provider id du destinataire) → ouvre un nouveau fil.
        - **"attendees"** : participants d'un fil (`chat_id`).
        - **"contacts"** : ton carnet de contacts de messagerie (interlocuteurs). Paginé.
        - **"update"** : modifie l'état d'un fil — `action` ∈ setReadStatus |
          setMuteStatus | setArchiveStatus | setPinnedStatus | setLabel | getInviteLink ;
          `value` = booléen pour les statuts, string pour setLabel, omis pour getInviteLink.
        - **"react"** : réagit à un message avec un emoji natif (ex. '👍').
          `message_id` = id d'un message d'op="read" ; `chat_id` est **requis sur
          l'API v2**, ignoré en v1.

        Args:
            op: list (défaut) | read | send | attendees | contacts | update | react.
            chat_id: id du fil (read, attendees, update, send-dans-un-fil, react v2).
            message_id: op="react" — id du message ciblé.
            text: op="send" — contenu du message.
            recipient_id: op="send" — provider id du destinataire (nouveau fil).
            action: op="update" — l'action de fil (cf. liste ci-dessus).
            value: op="update" — valeur associée à l'action.
            reaction: op="react" — l'emoji.
            limit: taille de page (list, read, contacts).
            cursor: pagination (list, contacts).
            with_names: op="list" — enrichissement des noms d'interlocuteurs.
        """
        client = unipile_client()

        if op == "list":
            return client.list_chats(limit=limit if limit is not None else 20,
                                     cursor=cursor, with_attendee_names=with_names)

        if op == "read":
            return client.list_messages(_need(chat_id, "chat_id", op),
                                        limit=limit if limit is not None else 30)

        if op == "send":
            if chat_id is None and recipient_id is None:
                raise _bad("op='send' requiert chat_id (répondre) ou recipient_id "
                           "(nouveau fil)")
            return client.send_message(_need(text, "text", op), chat_id=chat_id,
                                       attendee_id=recipient_id)

        if op == "attendees":
            return client.list_chat_attendees(_need(chat_id, "chat_id", op))

        if op == "contacts":
            return client.list_attendees(cursor=cursor, limit=limit)

        if op == "update":
            return client.patch_chat(_need(chat_id, "chat_id", op),
                                     _need(action, "action", op), value=value)

        if op == "react":
            mid = _need(message_id, "message_id", op)
            rea = _need(reaction, "reaction", op)
            # Ne passe `chat_id` que s'il est fourni : garde la compat si oto-core est
            # encore à une version dont `react_message` n'a pas ce kwarg (v2-only).
            if chat_id is not None:
                return client.react_message(mid, rea, chat_id=chat_id)
            return client.react_message(mid, rea)

        raise _bad("op doit être 'list', 'read', 'send', 'attendees', 'contacts', "
                   "'update' ou 'react'")

    # ---- publications ----------------------------------------------------

    @mcp.tool()
    def linkedin_unipile_post(
        op: Literal["feed", "get", "engagement", "create", "comment",
                    "react"] = "feed",
        post_id: Optional[str] = None,
        text: Optional[str] = None,
        kind: Literal["comments", "reactions"] = "comments",
        value: str = "LIKE",
        limit: int = 20,
        page: int = 0,
        refresh: bool = False,
        cursor: Optional[str] = None,
        fields: Optional[list[str]] = None,
        text_max_chars: Optional[int] = _TEXT_EXCERPT_CHARS,
    ) -> dict:
        """Publications LinkedIn : ton fil d'accueil, un post, l'engagement, publier.

        `op` :
        - **"feed"** (défaut) : miroir autogéré de ta home LinkedIn. Tu n'as RIEN à
          gérer (ni curseur, ni sync) : l'outil persiste les posts de ta page
          d'accueil dans ta base (datastore `linkedin-feed`, dédupliqués par leur
          identifiant), rafraîchit tout seul quand le cache est périmé, et te sert le
          miroir le plus récent en tête. Les encarts sponsorisés/promo sont exclus.
          Sous le capot : à `page=0`, refresh si le cache a dépassé son TTL — on pagine
          le feed live et on n'ajoute que les posts neufs (arrêt dès qu'une page est
          déjà connue). Les pages suivantes (`page>0`) lisent le miroir stocké sans
          retaper LinkedIn. Le tri suit ton réglage de home LinkedIn ; quoi qu'il
          arrive le miroir est re-trié par date de publication. Le miroir complet
          reste requêtable via `data_rows('linkedin-feed')` (filtrage par date côté
          nous, impossible sur le feed Voyager brut). Renvoie
          `{items, total, page, limit, synced}`.
          **Servi en VUE DE TRI** : chaque post rend de quoi le classer (auteur +
          headline, date, traction, lien, `urn`) et son texte coupé à 600 caractères
          (`text_truncated: true` marque la coupe) — une page de 40 posts bruts dépasse
          la taille d'un résultat d'outil, et le tri d'un feed se joue sur l'entête.
          Rien n'est perdu : `fields=["*"]` rend toutes les colonnes du miroir,
          `text_max_chars=None` le texte intégral, et un post entier se lit par
          `op="get"` ou `data_rows('linkedin-feed', id=<urn>)`.
        - **"get"** : un post — `post_id` = social_id (`urn:li:…`) d'un résultat
          `linkedin_unipile_profile(op="posts")`.
        - **"engagement"** : qui a réagi/commenté — `kind`='comments' ou 'reactions'.
        - **"create"** : publie un post depuis le compte connecté.
        - **"comment"** : commente un post (social-selling).
        - **"react"** : réagit à un post — `value`: LIKE | PRAISE | EMPATHY |
          INTEREST | APPRECIATION | ENTERTAINMENT.

        Args:
            op: feed (défaut) | get | engagement | create | comment | react.
            post_id: social_id du post (get, engagement, comment, react).
            text: op="create"/"comment" — le contenu.
            kind: op="engagement" — 'comments' (défaut) ou 'reactions'.
            value: op="react" — le type de réaction.
            limit: op="feed" — posts renvoyés pour cette page (défaut 20).
            page: op="feed" — page du miroir (0 = la plus récente ; >0 ne rafraîchit pas).
            refresh: op="feed" — force un rafraîchissement live.
            cursor: op="engagement" — pagination.
            fields: op="feed" — projection de colonnes, même sémantique que `data_rows`
                (les colonnes demandées, plus `_id`/`urn` toujours gardés pour adresser
                le post). Omis = la vue de tri ; `["*"]` = toutes les colonnes du miroir.
            text_max_chars: op="feed" — longueur du texte de chaque post (défaut 600 ;
                `None` = texte intégral).
        """
        if op == "feed":
            from ..datastore.core import make_store, NamespaceNotFound

            sub = access.current_user_sub_or_raise()
            client = unipile_client()
            store = make_store(sub)

            synced = False
            if page <= 0 and (refresh or _feed_is_stale(sub)):
                _sync_feed(client, store, sub)
                synced = True

            try:
                rows = store.list_rows(_FEED_NS, limit=10_000)
            except NamespaceNotFound:
                rows = []
            rows.sort(key=lambda r: r.get("posted_at") or "", reverse=True)

            offset = max(0, page) * limit
            window = rows[offset:offset + limit]
            return _shape_feed({"items": window, "total": len(rows), "page": page,
                                "limit": limit, "synced": synced},
                               fields, text_max_chars)

        client = unipile_client()

        if op == "get":
            return client.get_post(_need(post_id, "post_id", op))

        if op == "engagement":
            pid = _need(post_id, "post_id", op)
            return client.list_reactions(pid, cursor=cursor) if kind == "reactions" \
                else client.list_comments(pid, cursor=cursor)

        if op == "create":
            return client.create_post(_need(text, "text", op))

        if op == "comment":
            return client.comment_post(_need(post_id, "post_id", op),
                                       _need(text, "text", op))

        if op == "react":
            return client.react_post(_need(post_id, "post_id", op), value=value)

        raise _bad("op doit être 'feed', 'get', 'engagement', 'create', 'comment' "
                   "ou 'react'")

    # ---- réseau : relations & invitations ---------------------------------

    @mcp.tool()
    def linkedin_unipile_network(
        op: Literal["relations", "invitations", "invite", "handle",
                    "cancel"] = "relations",
        direction: Literal["received", "sent"] = "received",
        provider_id: Optional[str] = None,
        invitation_id: Optional[str] = None,
        shared_secret: Optional[str] = None,
        message: Optional[str] = None,
        action: str = "accept",
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Ton réseau LinkedIn : relations de 1er degré et invitations.

        `op` :
        - **"relations"** (défaut) : tes relations N1 — pour cibler/exporter ton
          réseau direct. Paginé (`cursor`). `fields` = PROJECTION : ne garde que ces
          champs sur chaque item (ex. `["name","headline","public_identifier",
          "member_id","created_at"]`) — réduit fortement le payload d'un export.
          ⚠️ Pagination NON fiable pour un export EXHAUSTIF : le `cursor` encode un
          offset volatil (doublons dans l'espace d'offset, total surestimé) et une
          page `limit=100` rend 90-100 items, pas 100. Pour charger tout un réseau :
          dédupliquer par `member_id` (JAMAIS l'offset), garder ≤8 pages en parallèle
          (au-delà : 502 en cascade), prouver le tarissement par 2 passes décalées.
          ⚠️ Ces six précautions sont TOUT ce qu'il y a à savoir : elles vivent ici,
          il n'existe pas de page à aller lire. Le guide `bulk-load` traite d'autre
          chose — déléguer un gros chargement à un sous-agent — et ne dit rien des
          pièges de pagination ci-dessus. ⚠️ Un chargement incomplet ne LÈVE PAS,
          il rend moins de monde : un traitement qui lit l'absence comme « pas une
          relation » agira ensuite sur une réponse fausse.
        - **"invitations"** : les invitations de connexion. `direction`='received'
          (reçues, à accepter) ou 'sent' (envoyées, en attente). Paginé — `limit`
          (défaut 50 : sans borne le backlog entier dépasse la limite de tokens).
        - **"invite"** : envoie une demande de connexion (outreach 2e/3e degré).
          `provider_id` = champ `provider_id` d'un résultat `linkedin_unipile_search`
          / `linkedin_unipile_profile` ; `message` = note ≤300 caractères.
        - **"handle"** : accepte ou refuse une invitation REÇUE. `invitation_id` ET
          `shared_secret` proviennent du MÊME item d'op="invitations"
          (direction='received') ; `action` = 'accept' ou 'decline'.
        - **"cancel"** : annule une invitation ENVOYÉE (en attente) — `invitation_id`
          d'un item direction='sent'.

        Args:
            op: relations (défaut) | invitations | invite | handle | cancel.
            direction: op="invitations" — 'received' (défaut) ou 'sent'.
            provider_id: op="invite" — provider id LinkedIn du destinataire.
            invitation_id: op="handle"/"cancel" — id de l'invitation.
            shared_secret: op="handle" — token LinkedIn du même item (obligatoire).
            message: op="invite" — note d'accompagnement (≤300 caractères).
            action: op="handle" — 'accept' (défaut) ou 'decline'.
            cursor: pagination (relations, invitations).
            limit: taille de page.
            fields: op="relations" — projection de champs.
        """
        client = unipile_client()

        if op == "relations":
            out = client.list_relations(cursor=cursor, limit=limit)
            if fields and isinstance(out, dict) and isinstance(out.get("items"), list):
                keep = set(fields)
                out["items"] = [{k: v for k, v in it.items() if k in keep}
                                for it in out["items"] if isinstance(it, dict)]
            return out

        if op == "invitations":
            return client.list_invitations(direction,
                                           limit=limit if limit is not None else 50,
                                           cursor=cursor)

        if op == "invite":
            return client.send_invitation(_need(provider_id, "provider_id", op),
                                          message=message)

        if op == "handle":
            return client.handle_invitation(_need(invitation_id, "invitation_id", op),
                                            _need(shared_secret, "shared_secret", op),
                                            action)

        if op == "cancel":
            return client.cancel_invitation(_need(invitation_id, "invitation_id", op))

        raise _bad("op doit être 'relations', 'invitations', 'invite', 'handle' "
                   "ou 'cancel'")

    # ---- compte : ardoise premium (Recruiter / Sales Navigator) -----------

    @mcp.tool()
    def linkedin_unipile_account(
        op: Literal["status", "contracts", "select", "inmail_balance"] = "contracts",
        contract_id: Optional[str] = None,
    ) -> dict:
        """Le compte LinkedIn connecté : son ÉTAT (op="status"), et son ardoise
        premium Recruiter / Sales Navigator (les trois autres op).

        - **"status"** — « mon LinkedIn est-il connecté ? » : `connected`,
          `account_id`, `account_name`, et `alive` (la session peut être MORTE alors
          que le compte reste lié — checkpoint, cookie tourné). Répond toujours ;
          `connected:false` porte `next_step`, le geste qui manque. **C'est l'op à
          prendre pour vérifier un onboarding messagerie** (#452 : un agent l'avait
          inventée, s'était pris un `invalid_arguments` et en avait conclu, à tort,
          que le canal n'était pas connecté).
        - **"contracts"** (défaut) : les contrats premium disponibles — l'`id` à
          passer à op="select".
        - **"select"** : active un contrat pour les appels premium qui suivent.
        - **"inmail_balance"** : solde de crédits InMail (messages premium).

        Les trois ops premium exigent l'abonnement correspondant SUR le compte
        connecté et le siège premium activé au connect
        (`unipile_connect_start(premium=…)`) — sinon les APIs premium répondent
        403 « out of your scope ». `op="status"`, lui, n'exige rien.

        Args:
            op: status | contracts (défaut) | select | inmail_balance.
            contract_id: op="select" — id renvoyé par op="contracts".
        """
        # AVANT `unipile_client()` : celui-ci LÈVE quand aucun compte n'est lié, ce
        # qui est exactement l'état que `status` doit pouvoir rapporter (#452).
        if op == "status":
            return account_status("LINKEDIN")

        client = unipile_client()

        if op == "contracts":
            return client.list_contracts()
        if op == "select":
            return client.select_contract(_need(contract_id, "contract_id", op))
        if op == "inmail_balance":
            return client.inmail_balance()

        raise _bad("op doit être 'status', 'contracts', 'select' ou 'inmail_balance'")

    # ---- Recruiter : offres d'emploi & candidats (lectures) ---------------

    @mcp.tool()
    def linkedin_unipile_job(
        op: Literal["postings", "posting", "applicants", "applicant",
                    "projects"] = "postings",
        job_id: Optional[str] = None,
        applicant_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Offres d'emploi et candidats du compte Recruiter LinkedIn (lectures).

        `op` :
        - **"postings"** (défaut) : les offres d'emploi du compte recruteur. Paginé.
        - **"posting"** : détail d'une offre (`job_id` d'op="postings").
        - **"applicants"** : candidats d'une offre. Paginé.
        - **"applicant"** : détail d'un candidat (`applicant_id` d'op="applicants").
        - **"projects"** : projets de recrutement (hiring projects). Le
          `hiring_project_id` alimente `linkedin_unipile_profile(op="action")`
          (pipeline). Paginé.

        Args:
            op: postings (défaut) | posting | applicants | applicant | projects.
            job_id: id de l'offre (posting, applicants, applicant).
            applicant_id: op="applicant" — id du candidat.
            cursor: pagination.
            limit: taille de page.
        """
        client = unipile_client()

        if op == "postings":
            return client.list_job_postings(cursor=cursor, limit=limit)
        if op == "posting":
            return client.get_job_posting(_need(job_id, "job_id", op))
        if op == "applicants":
            return client.list_job_applicants(_need(job_id, "job_id", op),
                                              cursor=cursor, limit=limit)
        if op == "applicant":
            return client.get_job_applicant(_need(job_id, "job_id", op),
                                            _need(applicant_id, "applicant_id", op))
        if op == "projects":
            return client.list_hiring_projects(cursor=cursor, limit=limit)

        raise _bad("op doit être 'postings', 'posting', 'applicants', 'applicant' "
                   "ou 'projects'")


# --- Le geste « connecter », déclaré ICI et pas dans le module d'auth ---------
#
# ⚠️ Un flux se déclare à l'IMPORT de son module. `unipile_connect` n'est importé
# que DANS les handlers (import paresseux) : le déclarer là-bas revenait à ne jamais
# le déclarer au boot — le catalogue de production ne le voyait pas, alors que les
# tests le voyaient parce que leur fixture importait le module de complaisance.
# Troisième fois cette semaine qu'un banc de test diverge du montage réel ; ici la
# règle qui en sort est simple : **une déclaration vit dans un module que le boot
# charge**, et `tools/unipile.py` en est un (le connecteur est au registre).
connector_flow.declare(
    "unipile",
    start=lambda ctx, values: _start_hosted_flow(ctx, values),
    label="Connecter un compte de messagerie",
    params=(connector_flow.FlowParam(
        name="channel", label="Canal à connecter", default="linkedin",
        options=(("linkedin", "LinkedIn"), ("whatsapp", "WhatsApp"),
                 ("telegram", "Telegram"), ("instagram", "Instagram"),
                 ("messenger", "Messenger"), ("twitter", "X (Twitter)"))),),
)

# ⚠️ **Un flux par CANAL, sans paramètre de canal** (split du 2026-08-28). Avant, un
# seul flux `unipile` portait un `channel` à choisir dans une liste : la carte
# demandait « lequel ? » parce qu'elle représentait les six. Maintenant chaque canal
# a sa carte, donc son flux, et le canal est DÉRIVÉ du connecteur
# (`Connector.hosted_channel`) au lieu d'être saisi. Le geste ne perd rien et gagne
# une garde : on ne peut plus démarrer une connexion WhatsApp depuis la carte
# Telegram. Le front n'a rien à changer — il rend `connect.params`, qui est
# simplement vide ici.
#
# `unipile` lui-même n'a PLUS de flux : c'est le compte fournisseur, sa carte pose
# une clé. Le tool `unipile_connect_start(channel=…)` reste, lui, multi-canal (il
# n'appartient à aucune capacité — cf. le namespace `unipile`).
#
# ⚠️ Cette phrase est FAUSSE de la déclaration ci-dessus, et le rester est délibéré
# (2026-08-29). Le flux multi-canal de `unipile` est du code de PRODUCTION que le
# split devait laisser intact — `test_le_compte_garde_son_code_de_production` le
# tient. Ce qui devait changer était côté ÉCRAN : le front rendait cette liste des
# six sur les SEPT cartes, donc la carte WhatsApp proposait de connecter LinkedIn.
# Corrigé là-bas (front tiers v1.17.0, `hostedChannelOf`), en lisant
# `auth.hosted_channel` — la carte du compte garde ses six, chaque carte de canal
# n'a plus que le sien.
for _con in providers.REGISTRY.values():
    if not _con.hosted_channel:
        continue
    connector_flow.declare(
        _con.name,
        # `_ch` capturé par valeur (défaut d'argument) : une closure sur `_con`
        # rendrait les six flux identiques, tous sur le dernier canal de la boucle.
        start=(lambda ctx, values, _ch=_con.hosted_channel.lower():
               _start_hosted_flow(ctx, {**values, "channel": _ch})),
        label=f"Connecter mon compte {_con.label}",
    )
del _con


async def _start_hosted_flow(ctx, values: dict):
    """Délègue au corps partagé REST+MCP, importé paresseusement (il tire le client
    du fournisseur). Les deux issues du flux — lien à ouvrir, ou compte adopté — sont
    traitées là-bas : l'adoption devient un refus typé, pas un contrat mutilé."""
    from .. import unipile_connect
    return await unipile_connect._start_flow(ctx, values)
