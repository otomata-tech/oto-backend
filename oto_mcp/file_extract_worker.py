"""Worker d'extraction du texte des fichiers déposés (#298) — boucle de fond dédiée.

Draine la file du barreau 2 : les fichiers sans texte extrait, plus les échecs
imprévus pas encore épuisés. Chaque tour lit un petit lot, télécharge, extrait,
enregistre le résultat — succès **comme refus**.

## Pourquoi une boucle à part plutôt qu'un pas de plus dans `embed_worker`

`embed_worker.run_embed_loop` commence par `if not embeddings.enabled(): return` — ce
qui est juste pour ce qu'elle fait : sans clé Mistral, il n'y a pas d'embedding à
calculer. Mais **l'extraction de texte ne dépend d'aucun service tiers**. L'y greffer
la rendrait muette sur tout déploiement sans `MISTRAL_API_KEY`, et le symptôme serait
« la recherche de fichiers ne trouve rien », sans erreur nulle part. Un silence
structurel, exactement la classe de panne que ce dépôt traque.

S'y ajoutent deux raisons de fond : les domaines de panne restent **disjoints** (un
échec d'embedding n'arrête pas l'extraction, ni l'inverse), et les deux travaux n'ont
ni le même rythme ni le même coût — 130 ms de CPU local par fichier ici, un
aller-retour réseau facturé là-bas.

## Deux contraintes du terrain, tenues ici

**Hors de la boucle d'événements.** Le serveur est mono-loop : 130 ms de CPU par
fichier dans la boucle est un gel (`docs/event-loop-perf.md`). Le tour de travail est
donc SYNC et passe par `run_in_threadpool`, comme `embed_worker`.

**Idempotent par le statut.** Un redémarrage en plein lot ne double rien : le travail
se réclame par l'ABSENCE de ligne, et chaque fichier traité en pose une — refus
compris. Au pire un fichier est ré-extrait une fois (le résultat écrase le précédent),
jamais deux fois compté.
"""
from __future__ import annotations

import asyncio
import logging

import psycopg
from starlette.concurrency import run_in_threadpool

from . import db, file_extract

logger = logging.getLogger(__name__)

# Rythme volontairement lent : l'indexation d'un fichier n'est pas interactive, et un
# dépôt attend quelques secondes sans que personne ne le remarque. Un poll court ne
# gagnerait rien et réveillerait la base pour rien.
_POLL_S = 30
# Petit lot : chaque fichier est un téléchargement + du CPU. Un gros lot tiendrait un
# thread du pool longtemps, au détriment des requêtes qui en ont besoin.
_BATCH = 5


def _extract_one(f: dict) -> str:
    """Un fichier : télécharger, extraire, enregistrer. Rend le statut obtenu.

    Une erreur de TÉLÉCHARGEMENT (stockage indisponible, clé absente) est un `failed`
    reprenable : contrairement à un format non supporté, elle peut disparaître
    d'elle-même. L'ÉCRITURE du résultat peut lever (la base refuse le texte) : c'est
    `_traiter` qui enregistre alors l'échec, sans quoi le fichier ne sortirait jamais
    de la file.
    """
    from . import media_store, upload_tokens

    fid = int(f["id"])
    # Un gros fichier (une vidéo déposée par parties, `large_uploads`) dépasse ce que la
    # lecture accepte : terminal, sans le télécharger — un `failed` le retenterait.
    if int(f.get("size_bytes") or 0) > upload_tokens.max_bytes():
        db.save_extracted_text(fid, status=file_extract.UNSUPPORTED,
                               detail="too large to read")
        return file_extract.UNSUPPORTED
    try:
        # Le plafond d'un FICHIER DE PROJET (celui du dépôt) : sans lui, la lecture
        # retombe sur celui d'une image (2 Mo) et tout fichier plus gros échoue ici.
        data = media_store.fetch_object(f["s3_key"], max_bytes=upload_tokens.max_bytes())
    # noqa: SILENT — l'échec est PERSISTÉ sur le fichier (status FAILED + detail)
    except Exception as e:  # noqa: BLE001 — stockage : reprenable, borné par `attempts`
        detail = getattr(e, "code", None) or type(e).__name__
        db.save_extracted_text(fid, status=file_extract.FAILED, detail=str(detail))
        return file_extract.FAILED

    out = file_extract.extract(data, f.get("filename") or "", f.get("mime") or "")
    db.save_extracted_text(fid, status=out.status, text=out.text,
                           pages=out.pages, detail=out.detail)
    return out.status


def _statut_d_echec(e: Exception) -> str:
    """Le statut d'un fichier dont le traitement a LEVÉ.

    Une valeur que la base refuse (`psycopg.DataError`, texte non encodable) le sera à
    l'identique au tour suivant : `unstorable`, terminal — la retenter, c'est
    retélécharger et réextraire le même fichier toutes les 30 s pour toujours (vécu le
    30/09/2026 : cinq fichiers en tête de file, retraités à chaque tour, et plus rien
    derrière eux). Le reste (base injoignable, imprévu) est `failed`, reprenable et
    borné par `attempts`."""
    if isinstance(e, (psycopg.DataError, UnicodeError)):
        return file_extract.UNSTORABLE
    return file_extract.FAILED


def _enregistrer_echec(f: dict, e: Exception) -> str:
    """Écrit l'échec sur le fichier, avec sa raison — c'est ce qui le sort de la file.
    Si l'écriture elle-même lève (base injoignable), l'exception remonte : le fichier
    reste dans la file, ce qui est juste pour une panne qui passera."""
    statut = _statut_d_echec(e)
    raison = (str(e).splitlines() or [""])[0]
    db.save_extracted_text(int(f["id"]), status=statut,
                           detail=f"{type(e).__name__}: {raison}"[:200])
    return statut


def _traiter(f: dict) -> "str | None":
    """Un fichier, ceinture comprise : quoi qu'il lève, les suivants passent, et
    l'échec s'ÉCRIT sur le fichier — sans ligne, il reviendrait à chaque tour. Rend le
    statut écrit, ou None si même l'échec n'a pas pu s'écrire (il reste en file)."""
    try:
        return _extract_one(f)
    except Exception as e:  # noqa: BLE001 — ceinture : la file avance quoi qu'il arrive
        try:
            statut = _enregistrer_echec(f, e)
        except Exception as e2:  # noqa: BLE001 — l'échec n'a pas pu s'écrire : reste en file
            logger.warning("file_extract_worker: fichier #%s en échec (%s), non "
                           "enregistré (%s) : reste en file", f.get("id"), e, e2)
            return None
        logger.warning("file_extract_worker: fichier #%s marqué %s : %s",
                       f.get("id"), statut, e)
        return statut


def _extract_batch() -> tuple:
    """Un tour SYNC (exécuté en threadpool). Rend `(traités, extraits)`.

    `traités` compte tout ce qui a reçu une réponse — refus inclus, puisque c'est ce
    qui les fait sortir de la file. `extraits` ne compte que les succès : c'est le
    chiffre qui dit si la recherche s'enrichit, et les distinguer évite de lire « 40
    fichiers traités » comme « 40 fichiers devenus cherchables »."""
    lot = db.files_pending_extraction(limit=_BATCH)
    if not lot:
        return 0, 0
    extraits = sum(1 for f in lot if _traiter(f) == file_extract.OK)
    return len(lot), extraits


async def run_extract_loop(interval: int = _POLL_S) -> None:
    """La boucle, composée au lifespan (`server._bg_loops`).

    Pas de gate d'activation : contrairement à l'embedding, l'extraction ne dépend
    d'aucune clé. Si le stockage objet n'est pas configuré, chaque fichier échoue
    proprement et la file se borne d'elle-même (`attempts`), ce qui est plus honnête
    qu'un worker qui se tait."""
    logger.info("file_extract_worker: démarré (poll %ss, lot %s).", interval, _BATCH)
    while True:
        try:
            traites, extraits = await run_in_threadpool(_extract_batch)
            if traites:
                logger.info("file_extract_worker: %d fichier(s) traité(s), "
                            "%d extrait(s).", traites, extraits)
        except Exception as e:  # noqa: BLE001 — un tour raté ne tue pas la boucle
            logger.warning("file_extract_worker: tour en échec : %s", e)
        await asyncio.sleep(interval)
