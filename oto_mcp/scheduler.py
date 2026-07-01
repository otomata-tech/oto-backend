"""Envoi d'email différé — logique de planification (pure) + boucle de fond.

`compute_scheduled_at` est PURE (testable sans I/O) : décide, au moment de
`email_send`, quand un email doit partir (None = tout de suite). `run_scheduler_
loop` est la boucle de fond (démarrée au boot via le lifespan, cf. server.py) qui
réclame les emails dus et les envoie — dans un THREAD (`asyncio.to_thread`) pour ne
pas bloquer l'event loop MCP (httpx + DB sync).

Garde-fou « quiet hours » par org (ADR : bracelet serveur, pas discipline LLM) :
hors `send_at`/`force_now`, un email composé dans la fenêtre interdite est décalé
au prochain `end`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import credentials_store, db, email

log = logging.getLogger("oto_mcp.scheduler")

# Fenêtre d'envoi interdite par défaut (heures locales) si l'org n'a rien réglé.
DEFAULT_QUIET_HOURS = {"tz": "Europe/Paris", "start": 20, "end": 8}

_POLL_INTERVAL_S = 60


def _tz(quiet_hours: Optional[dict]) -> ZoneInfo:
    name = (quiet_hours or {}).get("tz") or DEFAULT_QUIET_HOURS["tz"]
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_QUIET_HOURS["tz"])


def _parse_send_at(raw: str, tz: ZoneInfo) -> datetime:
    """Parse un ISO8601 → datetime aware UTC. Naïf = interprété dans `tz`."""
    s = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)   # lève ValueError si invalide
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _in_quiet(hour: int, start: int, end: int) -> bool:
    """`hour` est-il dans la fenêtre [start, end) (avec wrap-around minuit) ?"""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # wrap (ex. 20→8)


def _next_end(local_now: datetime, end: int) -> datetime:
    """Prochain instant local à `end`h00 strictement après `local_now` (et qui sort
    de la fenêtre courante : si on est déjà avant `end` aujourd'hui, c'est aujourd'hui)."""
    candidate = local_now.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def compute_scheduled_at(now_utc: datetime, quiet_hours: Optional[dict],
                         send_at_raw: Optional[str], force_now: bool) -> Optional[datetime]:
    """Heure d'envoi effective (aware UTC), ou None = envoi immédiat.

    - `send_at_raw` fourni → cette heure (explicite, prime sur les quiet hours) ;
      naïf = TZ de l'org. Si déjà passée → None (immédiat).
    - `force_now` → None.
    - sinon : si l'instant courant tombe dans la fenêtre quiet de l'org → prochain
      `end` ; sinon None.
    """
    qh = quiet_hours or DEFAULT_QUIET_HOURS
    tz = _tz(qh)

    if send_at_raw:
        when = _parse_send_at(send_at_raw, tz)
        return None if when <= now_utc else when

    if force_now:
        return None

    start = int(qh.get("start", DEFAULT_QUIET_HOURS["start"]))
    end = int(qh.get("end", DEFAULT_QUIET_HOURS["end"]))
    local_now = now_utc.astimezone(tz)
    if not _in_quiet(local_now.hour, start, end):
        return None
    return _next_end(local_now, end).astimezone(timezone.utc)


def _send_one(row: dict) -> None:
    """Envoie un email réclamé. Marque sent/failed (failed = retry tant que
    attempts < max, cf. db.mark_scheduled_failed)."""
    transport = row["transport"]
    from_hdr = email.format_from(row.get("from_email"), row.get("from_name")) or email._MAIL_FROM
    reply_to = row.get("reply_to")
    try:
        if transport == "resend":
            key = credentials_store.get_credential("org", str(row["org_id"]), "resend")
            if not key:
                db.mark_scheduled_failed(row["id"], "clé Resend absente pour l'org")
                return
            ok = email.send_via_resend(row["to_email"], row["subject"], row["body_html"],
                                       api_key=key, from_email=from_hdr, reply_to=reply_to)
        elif transport == "scaleway":
            raw = credentials_store.get_credential("org", str(row["org_id"]), "scaleway")
            f = credentials_store.unpack_secret("scaleway", raw) if raw else {}
            if not f.get("secret_key") or not f.get("project_id"):
                db.mark_scheduled_failed(row["id"], "credential Scaleway TEM absent/incomplet pour l'org")
                return
            ok = email.send_via_scaleway_tem(
                row["to_email"], row["subject"], row["body_html"],
                secret_key=f["secret_key"], project_id=f["project_id"],
                region=f.get("region") or "fr-par",
                from_email=row.get("from_email"), from_name=row.get("from_name"), reply_to=reply_to)
        else:
            ok = email._send(row["to_email"], row["subject"], row["body_html"],
                             reply_to=reply_to, from_email=from_hdr)
    except Exception as e:  # déchiffrement, réseau… → échec de cette tentative
        db.mark_scheduled_failed(row["id"], f"{type(e).__name__}: {e}")
        return
    if ok:
        db.mark_scheduled_sent(row["id"])
    else:
        db.mark_scheduled_failed(row["id"], f"envoi {transport} échoué")


def _process_due_batch() -> int:
    """Réclame et envoie les emails dus. SYNC (DB + httpx) — appelé via to_thread.
    Retourne le nombre d'emails traités."""
    rows = db.claim_due_scheduled_emails()
    for row in rows:
        _send_one(row)
    return len(rows)


async def run_scheduler_loop(interval: int = _POLL_INTERVAL_S) -> None:
    """Boucle de fond : envoie les emails dus toutes `interval` secondes. Isolée en
    thread pour ne pas bloquer l'event loop. Ne meurt jamais sur une erreur de tick."""
    log.info("scheduler d'email démarré (intervalle %ss)", interval)
    while True:
        try:
            n = await asyncio.to_thread(_process_due_batch)
            if n:
                log.info("scheduler : %d email(s) traité(s)", n)
        except asyncio.CancelledError:
            log.info("scheduler arrêté")
            raise
        except Exception as e:  # un tick raté ne tue pas la boucle
            log.warning("scheduler tick échoué : %s", e)
        await asyncio.sleep(interval)
