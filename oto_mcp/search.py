"""Recherche transverse « retrouver » (lot 3, Ship 1) — l'orchestrateur.

UNE capacité, un seul chemin de code (MCP `oto_search`, REST `/api/me/search`,
et la popup/page du dashboard par-dessus) : chaque source (pages, briefs,
procédures, guides, tableaux, fichiers, connecteurs) est interrogée avec SON
prédicat d'accès, puis fusion **RRF par rang (k=60)** — pas de comparaison de
scores hétérogènes entre sources.

Invariants (plan lot 3 §4.2) :
- **cherchable ⇔ lisible** : docs/briefs/fichiers scopés `ownership.accessible_
  project_ids` (la factorisation du scoping d'`op=list` — JAMAIS `can_access`,
  cross-org par construction) ; tableaux scopés par les listings datastore
  existants (owners du contexte + grants org/groupe) ; procédures = org active ;
  guides = platform ∪ org active ∪ user.
- **jamais de LLM au read** ; V1 lexicale (FTS `french` + repli d'accents).
- la source connecteurs (registre en mémoire) est INJECTÉE par la capacité
  (`connectors_catalog`) — ce module ne remonte pas dans la couche adaptateur.

Deux familles de hits : **passages** (prose — page/brief/procedure/guide, avec
fragment surligné) et **conteneurs** (tableau/fichier/connecteur — nom+description,
pas d'aperçu). Forme : `{kind, ref, title, description?, passage?, project_id?,
project_name?, updated_at?, matched_by:'lexical'}`.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from . import db, ownership

logger = logging.getLogger(__name__)

_RRF_K = 60
KINDS = ("page", "brief", "procedure", "guide", "tableau", "fichier", "connecteur")

# Repli d'accents côté Python (miroir de db.projects._fold) — pour les sources
# matchées en mémoire (tableaux, connecteurs).
_ACCENTS = "àâäáãéèêëïîíôöóòõùûüúçñýÿÀÂÄÁÃÉÈÊËÏÎÍÔÖÓÒÕÙÛÜÚÇÑÝŸ"
_PLAIN = "aaaaaeeeeiiiooooouuuucnyyAAAAAEEEEIIIOOOOOUUUUCNYY"
_FOLD = str.maketrans(_ACCENTS, _PLAIN)


def _fold_py(s: str) -> str:
    return (s or "").translate(_FOLD).lower()


def _headline_ok(h: Optional[str]) -> bool:
    """Le fragment ts_headline porte-t-il un vrai surlignage ? (La 2e tsquery est
    construite de la saisie BRUTE : si le match ne venait que du folding, pas de
    <b> → on n'affiche pas un fragment non pertinent.)"""
    return bool(h) and "<b>" in h


def _snippet(text: str, cap: int = 160) -> str:
    t = " ".join((text or "").split())
    return t[:cap] + ("…" if len(t) > cap else "")


def search(sub: str, org_id: int, q: str, *,
           scope: str = "org", project_id: Optional[int] = None,
           kinds: Optional[list[str]] = None, limit: int = 20,
           connectors_catalog: Optional[list[dict]] = None) -> dict:
    """Le verbe « retrouver ». `scope='project'` restreint à UN projet (déjà validé
    accessible par le caller) ; défaut = tous les projets accessibles de l'org."""
    wanted = set(kinds) if kinds else set(KINDS)
    per_source = min(max(limit, 10), 50)

    if scope == "project" and project_id is not None:
        pids = [int(project_id)]
    else:
        pids = ownership.accessible_project_ids(sub, org_id, want="read")

    ranked: list[tuple[float, dict]] = []

    def _add(rows: list[dict], to_hit) -> None:
        for i, r in enumerate(rows):
            hit = to_hit(r)
            if hit is not None:
                ranked.append((1.0 / (_RRF_K + i + 1), hit))

    # ── passages ────────────────────────────────────────────────────────────
    if "page" in wanted:
        _add(db.search_docs_fts(q, pids, limit=per_source), lambda r: {
            "kind": "page", "ref": r["id"], "title": r["title"],
            "project_id": r["project_id"],
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})
    if "brief" in wanted:
        _add(db.search_project_briefs(q, pids, limit=per_source), lambda r: {
            "kind": "brief", "ref": r["id"], "title": r["name"],
            "project_id": r["id"],
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})
    if "procedure" in wanted:
        _add(db.search_procedures_fts(q, org_id, limit=per_source), lambda r: {
            "kind": "procedure", "ref": r["slug"], "title": r["title"] or r["slug"],
            "description": r.get("description") or None,
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})
    if "guide" in wanted:
        _add(db.search_guides_fts(q, org_id, sub, limit=per_source), lambda r: {
            "kind": "guide", "ref": {"scope": r["scope"], "slug": r["slug"]},
            "title": r["title"] or r["slug"],
            "description": r.get("description") or None,
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})

    # ── conteneurs ──────────────────────────────────────────────────────────
    if "tableau" in wanted:
        _add(_match_tableaux(q, sub, org_id), lambda r: r)
    if "fichier" in wanted:
        _add(db.search_files_meta(q, pids, limit=per_source), lambda r: {
            "kind": "fichier", "ref": r["id"],
            "title": r.get("title") or r["filename"],
            "description": r.get("description") or None,
            "project_id": r["project_id"],
            "updated_at": r.get("created_at"), "matched_by": "lexical"})
    if "connecteur" in wanted and connectors_catalog:
        _add(_match_connectors(q, connectors_catalog), lambda r: r)

    # ── fusion RRF + noms de projet (une requête) ───────────────────────────
    ranked.sort(key=lambda t: t[0], reverse=True)
    hits = [h for _, h in ranked[:limit]]
    names = db.project_names(sorted({h["project_id"] for h in hits if h.get("project_id")}))
    for h in hits:
        if h.get("project_id") in names:
            h["project_name"] = names[h["project_id"]]

    # Télémétrie (lot 3 Ship 1 §5) : le calllog ne trace que le MCP — log applicatif
    # anonymisé (hash de q, jamais la saisie) pour rendre les conditions V2 décidables.
    logger.info("search q_hash=%s scope=%s kinds=%s n=%d",
                hashlib.sha256(q.encode()).hexdigest()[:12], scope,
                ",".join(sorted(wanted)) if kinds else "*", len(hits))

    out: dict = {"hits": hits, "count": len(hits), "matched_by": "lexical"}
    if not hits:
        out["hint"] = ("Aucun résultat — reformule (la V1 est lexicale : essaie les "
                       "mots exacts du contenu), ou navigue : `oto_project op=list` "
                       "puis l'épine du projet.")
    return out


def _match_tableaux(q: str, sub: str, org_id: int) -> list[dict]:
    """Tableaux du CONTEXTE (owners org+moi+mes groupes ∪ grants org/groupe) matchés
    en mémoire sur nom + labels de colonnes du schéma — parité du listing datastore
    (mêmes fonctions db, sujet du tripwire). Rang : nom exact > nom partiel > label."""
    principals = ownership.active_org_principals(sub, org_id)
    gids = [int(p[1]) for p in principals if p[0] == "group"]
    rows = db.list_datastore_namespaces_for_owners(principals)
    seen = {r["id"] for r in rows}
    rows += [r for r in db.list_datastore_namespaces_granted_to(sub, [org_id], gids)
             if r["id"] not in seen]
    fq = _fold_py(q)
    scored: list[tuple[int, dict]] = []
    for r in rows:
        name = _fold_py(r["namespace"])
        labels = " ".join(
            _fold_py(str(f.get("label") or f.get("key") or ""))
            for f in ((r.get("schema") or {}).get("fields") or []))
        if name == fq:
            rank = 0
        elif fq in name:
            rank = 1
        elif fq and fq in labels:
            rank = 2
        else:
            continue
        scored.append((rank, {
            "kind": "tableau", "ref": r["id"], "title": r["namespace"],
            "matched_by": "lexical"}))
    scored.sort(key=lambda t: t[0])
    return [h for _, h in scored]


def _match_connectors(q: str, catalog: list[dict]) -> list[dict]:
    """Connecteurs du catalogue VISIBLE (injecté par la capacité — activation × RBAC
    déjà appliqués), matchés en mémoire sur name/label/description."""
    fq = _fold_py(q)
    if not fq:
        return []
    scored: list[tuple[int, dict]] = []
    for c in catalog:
        name, label = _fold_py(c.get("name", "")), _fold_py(c.get("label", ""))
        blurb = _fold_py(f"{c.get('help', '')} {c.get('description', '')}")
        if fq == name or fq == label:
            rank = 0
        elif fq in name or fq in label:
            rank = 1
        elif fq in blurb:
            rank = 2
        else:
            continue
        scored.append((rank, {
            "kind": "connecteur", "ref": c["name"], "title": c.get("label") or c["name"],
            "description": _snippet(c.get("help") or c.get("description") or "") or None,
            "matched_by": "lexical"}))
    scored.sort(key=lambda t: t[0])
    return [h for _, h in scored]
