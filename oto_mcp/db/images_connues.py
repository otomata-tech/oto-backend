"""Le registre des images CONNUES (`images_connues`) : écrire ce qu'une écriture a
appris, réserver ce qui reste à copier, conclure (`oto_mcp/images_connues.py` dit la
règle, `images_connues_worker.py` copie).

Une ligne par adresse EXACTE, clé = son empreinte SHA-256. États :
`a_copier` → `copiee` (la copie est sous `app-images/<empreinte>`) ou `refusee`
(terminal : pas une image, trop grosse, adresse interne, 4xx, essais épuisés). Une
adresse copiée ne se recopie jamais — ni à la lecture, ni plus tard.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from ._conn import _connect

log = logging.getLogger(__name__)

A_COPIER = "a_copier"
COPIEE = "copiee"
REFUSEE = "refusee"

#: La réservation d'une adresse par un tour de la boucle : au-delà, un tour mort (le
#: processus coupé en plein téléchargement) la rend au suivant.
BAIL_S = 300


def retenir(conn, urls: Iterable[str], origine: str) -> None:
    """Pose les adresses au registre, dans la transaction de `conn`, sous un point de
    sauvegarde : une adresse déjà connue ne bouge pas (`ON CONFLICT DO NOTHING`), et un
    échec ici est JOURNALISÉ, jamais propagé — l'écriture de ligne ou d'app qui a
    appris l'adresse reste faite. Le pire est une image qui ne s'affiche pas."""
    from .. import images_connues as ic
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO images_connues (empreinte, url, origine) "
                "SELECT e, u, %s FROM unnest(%s::text[], %s::text[]) AS t(e, u) "
                "ON CONFLICT (empreinte) DO NOTHING",
                (origine, [ic.empreinte(u) for u in urls], urls))
    except Exception as e:  # noqa: BLE001 — journalisé : l'écriture qui apprend reste faite
        log.warning("images_connues: %d adresse(s) %s non retenue(s) : %s",
                    len(urls), origine, e)


def reserver(limite: int) -> list[dict]:
    """Les adresses à copier, réservées pour `BAIL_S` secondes (`SKIP LOCKED` : deux
    tours concurrents ne prennent pas la même). Rend `[{empreinte, url, essais}]`."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "UPDATE images_connues SET reservee_jusqu_a = NOW() + make_interval(secs => %s), "
            "       essais = essais + 1 "
            " WHERE empreinte IN ("
            "   SELECT empreinte FROM images_connues "
            "    WHERE etat = %s AND prochain_essai <= NOW() "
            "      AND (reservee_jusqu_a IS NULL OR reservee_jusqu_a < NOW()) "
            "    ORDER BY prochain_essai LIMIT %s FOR UPDATE SKIP LOCKED) "
            "RETURNING empreinte, url, essais",
            (BAIL_S, A_COPIER, limite)).fetchall()]


def conclure(empreinte: str, etat: str, detail: Optional[str] = None, *,
             octets: Optional[int] = None, type_image: Optional[str] = None,
             reessayer_dans_s: Optional[int] = None) -> None:
    """Écrit l'issue d'une copie. `reessayer_dans_s` : l'adresse reste `a_copier` et
    revient après ce délai (panne passagère) ; sinon `etat` est posé."""
    with _connect() as conn:
        if reessayer_dans_s is not None:
            conn.execute(
                "UPDATE images_connues SET reservee_jusqu_a = NULL, detail = %s, "
                "       prochain_essai = NOW() + make_interval(secs => %s) "
                " WHERE empreinte = %s",
                ((detail or "")[:300], int(reessayer_dans_s), empreinte))
            return
        conn.execute(
            "UPDATE images_connues SET etat = %s, detail = %s, octets = %s, type_image = %s, "
            "       reservee_jusqu_a = NULL, "
            "       copiee_le = CASE WHEN %s = 'copiee' THEN NOW() ELSE copiee_le END "
            " WHERE empreinte = %s",
            (etat, (detail or "")[:300] or None, octets, type_image, etat, empreinte))


def etat_de(url: str) -> Optional[dict]:
    """La ligne d'une adresse (ou None) — pour les tests et l'enquête."""
    from .. import images_connues as ic
    with _connect() as conn:
        r = conn.execute("SELECT * FROM images_connues WHERE empreinte = %s",
                         (ic.empreinte(url),)).fetchone()
    return dict(r) if r else None
