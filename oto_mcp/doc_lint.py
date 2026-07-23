"""Lint natif de KB (oto/#6 B1) : santé des pages d'un projet, sans requête par-page.

Trois signaux ACTIONNABLES, dérivés d'un seul `list_docs_for_project` :
- **stale** : page pas retouchée depuis N jours (`updated_at < cutoff`) ;
- **empty** : page au corps trivial (à rédiger ou à supprimer) ;
- **duplicate_titles** : même titre normalisé sur ≥2 pages (fusion probable).

Fonction PURE (le caller calcule le `stale_before` avec l'horloge) → testable, zéro
N+1 (leçon A7)."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

_MIN_BODY = 10   # sous ce seuil de caractères (corps strippé), la page est « vide ».


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip()).lower()


def lint_docs(docs: list[dict], *, stale_before: Optional[str] = None) -> dict:
    """`docs` = sortie de `list_docs_for_project` (title, body_md, updated_at).
    `stale_before` = borne 'YYYY-MM-DD HH:MM:SS' (comparaison lexicale = chronologique
    sur ce format) ; None → pas de check de fraîcheur."""
    stale: list[dict] = []
    empty: list[dict] = []
    by_title: dict[str, list[int]] = defaultdict(list)
    for d in docs:
        title = (d.get("title") or "").strip()
        if len((d.get("body_md") or "").strip()) < _MIN_BODY:
            empty.append({"id": d.get("id"), "title": title})
        upd = d.get("updated_at")
        if stale_before and upd and str(upd) < stale_before:
            stale.append({"id": d.get("id"), "title": title, "updated_at": str(upd)})
        if title:
            by_title[_norm_title(title)].append(d.get("id"))
    duplicate_titles = [{"title": k, "ids": ids}
                        for k, ids in by_title.items() if len(ids) > 1]
    return {"stale": stale, "empty": empty, "duplicate_titles": duplicate_titles,
            "count": len(stale) + len(empty) + len(duplicate_titles)}
