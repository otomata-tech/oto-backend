"""Worker d'indexation sémantique (lot 3) — draine l'outbox `docs.embed_dirty`.

Boucle de fond démarrée au boot (composée au lifespan, comme le scheduler email).
Chaque tour : lit un batch de pages `dirty`, embed HORS event loop
(`run_in_threadpool` → l'appel réseau Mistral ne bloque jamais la boucle mono-loop),
upsert `doc_embeddings` + baisse le drapeau. Idempotent (`content_sha` : on saute
une page dont le texte n'a pas bougé). No-op complet sans `MISTRAL_API_KEY`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from starlette.concurrency import run_in_threadpool

from . import db, embeddings

logger = logging.getLogger(__name__)

_POLL_S = 20
_BATCH = 16


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _index_batch() -> int:
    """Un tour SYNC (exécuté en threadpool) : embed les pages dirty, upsert. Renvoie
    le nombre traité. Best-effort par page — une erreur laisse la page dirty (re-tentée)."""
    rows = db.list_dirty_docs(_BATCH)
    if not rows:
        return 0
    # Idempotence : ne ré-embed que les pages dont le texte a changé ; les autres
    # (dirty mais sha identique) sont juste dé-marquées.
    to_embed = []
    skipped = 0
    for r in rows:
        sha = _sha(r["text"])
        if db.get_doc_embedding_sha(r["id"]) == sha:
            db.clear_embed_dirty(r["id"])       # dirty mais texte inchangé → dé-marqué
            skipped += 1
        else:
            to_embed.append((r["id"], r["text"], sha))
    if not to_embed:
        return skipped
    try:
        vectors = embeddings.embed_texts([t for _, t, _ in to_embed])
    except Exception as e:  # noqa: BLE001 — réseau/API : on laisse dirty, re-tour suivant
        logger.warning("embed_worker: batch échoué (re-tenté) : %s", e)
        return 0
    done = 0
    for (doc_id, _text, sha), vec in zip(to_embed, vectors):
        try:
            db.upsert_doc_embedding(doc_id, sha, embeddings.to_pg(vec), embeddings.MODEL)
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("embed_worker: upsert doc #%s échoué : %s", doc_id, e)
    return skipped + done


def _index_aux_batch() -> int:
    """Miroir de `_index_batch` pour les sources non-page (briefs + guides on-demand,
    #6 C) : draine `list_dirty_aux`, ré-embed sur changement de sha, upsert dans
    `aux_embeddings`. Best-effort par source."""
    rows = db.list_dirty_aux(_BATCH)
    if not rows:
        return 0
    to_embed = []
    skipped = 0
    for r in rows:
        sha = _sha(r["text"])
        if db.get_aux_embedding_sha(r["kind"], r["ref"]) == sha:
            db.clear_aux_dirty(r["kind"], r["ref"])
            skipped += 1
        else:
            to_embed.append((r["kind"], r["ref"], r["text"], sha))
    if not to_embed:
        return skipped
    try:
        vectors = embeddings.embed_texts([t for _, _, t, _ in to_embed])
    except Exception as e:  # noqa: BLE001
        logger.warning("embed_worker: batch aux échoué (re-tenté) : %s", e)
        return 0
    done = 0
    for (kind, ref, _text, sha), vec in zip(to_embed, vectors):
        try:
            db.upsert_aux_embedding(kind, ref, sha, embeddings.to_pg(vec), embeddings.MODEL)
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("embed_worker: upsert %s #%s échoué : %s", kind, ref, e)
    return skipped + done


async def run_embed_loop(interval: int = _POLL_S) -> None:
    if not embeddings.enabled():
        logger.info("embed_worker: MISTRAL_API_KEY absent → sémantique inerte, worker off.")
        return
    logger.info("embed_worker: démarré (poll %ss).", interval)
    while True:
        try:
            n = await run_in_threadpool(_index_batch)
            na = await run_in_threadpool(_index_aux_batch)   # briefs + guides (#6 C)
            if n or na:
                logger.info("embed_worker: %d page(s) + %d brief/guide indexé(s).", n, na)
        except Exception as e:  # noqa: BLE001
            logger.warning("embed_worker: tour en échec : %s", e)
        await asyncio.sleep(interval)
