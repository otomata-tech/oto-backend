"""La boucle qui COPIE les images connues d'une app dans le stockage média
(`oto_mcp/images_connues.py` dit la règle, `db/images_connues.py` le registre).

Chaque tour réserve un petit lot d'adresses `a_copier`, télécharge chacune UNE fois,
et la dépose publique sous `app-images/<empreinte>` — ou la refuse, définitivement.

## Ce qui garde le téléchargement

Le même chemin que `web_image` (`tools/web._fetch_http`) : chaque saut de redirection
repasse la garde d'egress de la plateforme (adresse interne refusée, ensemble résolu
jugé en entier), budget global, lecture en flux bornée à 3 Mo et arrêtée PENDANT. Le
format est lu sur les octets (png, jpeg, gif, webp ; jamais SVG), jamais sur le
`Content-Type`. Ce qui manque ici par rapport à `web_image` : les
`excluded_url_prefixes` d'un projet — un tour de boucle n'a pas d'appel, donc pas de
projet ; une adresse ne vient de toute façon que d'un HTML ou d'une ligne que leur
auteur a écrits.

## Ce que la boucle ne fait jamais

Télécharger à cause d'une LECTURE d'app : elle ne lit que le registre, qu'une lecture
n'écrit pas. Retélécharger une adresse copiée. Une panne passagère (réseau, délai, 5xx,
429) revient au plus `ESSAIS_MAX` fois, espacée ; le reste est refusé tout de suite.

`tiers=True` (`boucles_de_fond.py`) : elle appelle des hôtes extérieurs, seule la
production la fait tourner, et seulement si elle pose `OTO_IMAGES_CONNUES_ENABLED=1`.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from starlette.concurrency import run_in_threadpool

from .db import images_connues as reg

logger = logging.getLogger(__name__)

_POLL_S = 15
_LOT = 16
_PARALLELE = 4
#: Le budget global d'un téléchargement (`_fetch_http`), redirections comprises.
_DELAI_S = 20
ESSAIS_MAX = 3
_ATTENTES_S = (60, 600, 3600)


def _passagere(verdict: str, status: Optional[int]) -> bool:
    """Une panne qui peut passer d'elle-même : réseau, délai, 5xx, 429."""
    if status is not None:
        return status >= 500 or status == 429
    v = verdict.lower()
    return v.startswith(("network", "timeout", "global timeout"))


def copier(img: dict) -> str:
    """Une adresse : télécharger, juger, déposer, conclure. Rend l'état écrit
    (`copiee`, `refusee`, ou `a_copier` quand elle reviendra)."""
    from . import media_store
    from .mcp_errors import McpError
    from .tools.web import _MAX_FETCH_BYTES, _fetch_http

    empreinte, url, essais = img["empreinte"], img["url"], int(img.get("essais") or 1)

    def _encore(detail: str) -> str:
        if essais >= ESSAIS_MAX:
            reg.conclure(empreinte, reg.REFUSEE, f"{detail} (après {essais} essais)")
            return reg.REFUSEE
        reg.conclure(empreinte, reg.A_COPIER, detail,
                     reessayer_dans_s=_ATTENTES_S[min(essais, len(_ATTENTES_S)) - 1])
        return reg.A_COPIER

    try:
        res = _fetch_http(url, deadline_s=_DELAI_S)
    except McpError as e:
        # La garde d'egress : une adresse interne ne devient jamais publique.
        reg.conclure(empreinte, reg.REFUSEE, f"egress: {e}")
        return reg.REFUSEE
    if not res.get("ok"):
        verdict = str(res.get("verdict") or "unreadable")
        if _passagere(verdict, res.get("status")):
            return _encore(verdict)
        reg.conclure(empreinte, reg.REFUSEE, verdict)
        return reg.REFUSEE
    if res.get("capped"):
        reg.conclure(empreinte, reg.REFUSEE, f"over {_MAX_FETCH_BYTES} bytes")
        return reg.REFUSEE
    octets = res.get("body") or b""
    try:
        _url, type_image = media_store.upload_image_connue(
            empreinte, octets, max_bytes=_MAX_FETCH_BYTES)
    except media_store.MediaError as e:
        if e.status >= 500:
            return _encore(f"storage: {e.code}")
        reg.conclure(empreinte, reg.REFUSEE, f"{e.code}: {e}")
        return reg.REFUSEE
    reg.conclure(empreinte, reg.COPIEE, None, octets=len(octets), type_image=type_image)
    return reg.COPIEE


def _traiter(img: dict) -> Optional[str]:
    """`copier`, ceinture comprise : quoi qu'il lève, le lot avance et l'échec est
    JOURNALISÉ et ÉCRIT — repris plus tard, refusé au dernier essai : une adresse qui
    casse toujours de la même façon ne revient pas à chaque bail pour toujours. Si même
    l'écriture échoue (base injoignable), l'adresse revient au bout de son bail."""
    try:
        return copier(img)
    except Exception as e:  # noqa: BLE001 — ceinture : le lot avance, l'échec est écrit
        logger.warning("images_connues: copie de %s en échec imprévu : %s",
                       img.get("empreinte"), e)
        detail = f"{type(e).__name__}: {(str(e).splitlines() or [''])[0]}"[:200]
        try:
            if int(img.get("essais") or 1) >= ESSAIS_MAX:
                reg.conclure(img["empreinte"], reg.REFUSEE, detail)
                return reg.REFUSEE
            reg.conclure(img["empreinte"], reg.A_COPIER, detail,
                         reessayer_dans_s=_ATTENTES_S[-1])
            return reg.A_COPIER
        except Exception as e2:  # noqa: BLE001 — journalisé : l'adresse revient au bail
            logger.warning("images_connues: échec de %s non écrit (%s) : revient au bail",
                           img.get("empreinte"), e2)
            return None


def _tour() -> tuple[int, int]:
    """Un tour SYNC (en threadpool) : `(traitées, copiées)`."""
    lot = reg.reserver(_LOT)
    if not lot:
        return 0, 0
    with ThreadPoolExecutor(max_workers=_PARALLELE) as pool:
        etats = list(pool.map(_traiter, lot))
    return len(lot), sum(1 for e in etats if e == reg.COPIEE)


async def run_images_connues_loop(interval: int = _POLL_S) -> None:
    """La boucle, composée au lifespan (`boucles_de_fond.BOUCLES`)."""
    logger.info("images_connues: démarrée (poll %ss, lot %s).", interval, _LOT)
    while True:
        try:
            traitees, copiees = await run_in_threadpool(_tour)
            if traitees:
                logger.info("images_connues: %d adresse(s) traitée(s), %d copiée(s).",
                            traitees, copiees)
                continue  # du travail en attente : le tour suivant tout de suite
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — la boucle survit à un tour cassé
            logger.warning("images_connues: tour en échec : %s", e)
        await asyncio.sleep(interval)
