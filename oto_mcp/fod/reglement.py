"""Client HTTP mince vers le service FOD — capacité « règlement » (ADR 0028).

Les règlements écrits de PLU/PLUi sont des PDF lourds (souvent >50 Mo, >1000 pages,
plusieurs zones). Le download + parse pdftotext + indexation est fait **une fois par
`idurba`** par le service FOD dédié (box fod-0, PG + texte en S3) — JAMAIS dans le
chemin d'une requête MCP. Ce module sert les **extraits**
ciblés (par zone / mot-clé) depuis ce cache partagé.

Cache-miss = `{cached: False, statut}`. Le service sait ingérer un règlement **à la
demande**, en tâche de fond (`POST|GET /api/reglements/{idurba}/ingestion`) : une seule
ingestion à la fois sur la box, une pièce de 80 Mo au plus, 600 s au plus. `ingestion`
lance ou relit cet état et rend le code HTTP avec le corps — c'est l'outil qui nomme
le refus. On NE refait PAS le parsing en local : le backend ne porte ni le PDF ni
poppler. Pas de fallback (ADR 0028) : service indisponible/mal configuré ⟹ erreur
actionnable.

La plomberie (client, jeton S2S, erreurs sans adresse interne) est celle de
`fod.http`, partagée par tous les clients FOD.
"""
from __future__ import annotations

from typing import Any, Optional

from . import http as fod_http

#: Les réponses de la route d'ingestion qui portent un ÉTAT, pas une panne : déjà prêt
#: (200), lancé ou en cours (202), document que le GPU ne publie pas (404), service
#: occupé par une autre ingestion (503 + `Retry-After`). Les autres codes lèvent par
#: `fod.http.etat`.
_ETATS_HTTP = (200, 202, 404, 503)


def extraits(idurba: str, *, zone: Optional[str] = None, query: Optional[str] = None,
             max_extraits: int = 8, context_lignes: int = 30) -> dict[str, Any]:
    """Extraits du règlement écrit d'un PLU/PLUi (cache partagé par `idurba`).

    Renvoie `{idurba, cached, ...}` ; `cached=False` (+ `statut`) si le règlement
    n'est pas encore ingéré dans le service.
    """
    params: dict[str, Any] = {
        "idurba": idurba,
        "max_extraits": max_extraits,
        "context_lignes": context_lignes,
    }
    if zone:
        params["zone"] = zone
    if query:
        params["query"] = query
    return fod_http.get("/api/reglement", params=params)


def ingestion(idurba: str, *, lancer: bool) -> tuple[int, dict[str, Any], Optional[int]]:
    """Lance (`lancer=True`, POST — idempotent côté service) ou relit (GET) l'ingestion
    à la demande d'un règlement. Rend `(code HTTP, corps, Retry-After en secondes)`.

    Pas de reprise sur 503 ici, contrairement à `fod.http.get` : ce 503 dit « une autre
    ingestion occupe la box », un état que l'appelant doit nommer, pas absorber."""
    from urllib.parse import quote

    chemin = f"/api/reglements/{quote(idurba, safe='')}/ingestion"
    r = fod_http.etat("POST" if lancer else "GET", chemin, _ETATS_HTTP)
    try:
        apres = int(r.headers.get("Retry-After") or 0) or None
    except ValueError:
        apres = None
    return r.status_code, r.json(), apres
