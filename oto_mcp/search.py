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

Deux familles de hits : **passages** (prose — page/brief/procedure/guide + ligne de
datastore, avec fragment surligné) et **conteneurs** (tableau/fichier/connecteur —
nom+description, pas d'aperçu). La **ligne** (#67 V2.1) est hybride : un enregistrement
de datastore (`ref={ns_id, row_id}`) matché sur son contenu, rendu avec un fragment
comme la prose. Forme : `{kind, ref, title, description?, passage?, project_id?,
project_name?, updated_at?, matched_by:'lexical'}`.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from . import db, ownership

logger = logging.getLogger(__name__)

_RRF_K = 60
KINDS = ("page", "brief", "procedure", "guide", "tableau", "ligne", "fichier", "connecteur")

# Repli d'accents côté Python (miroir de db.projects._fold) — pour les sources
# matchées en mémoire (tableaux, connecteurs).
_ACCENTS = "àâäáãéèêëïîíôöóòõùûüúçñýÿÀÂÄÁÃÉÈÊËÏÎÍÔÖÓÒÕÙÛÜÚÇÑÝŸ"
_PLAIN = "aaaaaeeeeiiiooooouuuucnyyAAAAAEEEEIIIOOOOOUUUUCNYY"
_FOLD = str.maketrans(_ACCENTS, _PLAIN)


def fold(s: str) -> str:
    """Repli accents+casse — LA règle du produit pour tout matching en mémoire.
    Publique parce que le catalogue d'outils (`tool_registry.match`) s'en sert aussi :
    deux replis divergents feraient répondre différemment à « déclaration » selon
    qu'on cherche un tableau ou un outil."""
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
           connectors_catalog: Optional[list[dict]] = None,
           query_embedding: Optional[list[float]] = None) -> dict:
    """Le verbe « retrouver ». `scope='project'` restreint à UN projet (déjà validé
    accessible par le caller) ; défaut = tous les projets accessibles de l'org.
    `query_embedding` (fourni par le handler async, hors event loop) active la
    fusion LEXICAL + SÉMANTIQUE des pages par RRF (sinon lexical seul, dégradation
    gracieuse : le sémantique n'est jamais un prérequis)."""
    wanted = set(kinds) if kinds else set(KINDS)
    per_source = min(max(limit, 10), 50)

    if scope == "project" and project_id is not None:
        pids = [int(project_id)]
        granted_pids: set[int] = set()
    else:
        by_prov = ownership.accessible_project_ids_by_provenance(sub, org_id, want="read")
        pids = by_prov["all"]
        granted_pids = set(by_prov["granted"])
    # Même provenance, pour les tableaux/lignes (scopés par NAMESPACE, pas par
    # projet) — calculée UNE fois, réutilisée par `_match_tableaux`/`_match_rows`/
    # `_match_rows_semantic` (qui appellent `_accessible_namespaces` elles-mêmes,
    # cache non nécessaire ici, l'appel est déjà borné aux namespaces du contexte)
    # et par le tour de tag des hits plus bas.
    ns_ids_partages = _granted_namespace_ids(sub, org_id) if "tableau" in wanted or "ligne" in wanted else set()

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
            "project_id": r["project_id"], "description": r.get("description") or None,
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})
        # Sémantique (lot 3) : les pages proches du sens de la requête, fusionnées au
        # lexical par RRF (une page trouvée par les DEUX cumule ses deux rangs → remonte).
        if query_embedding is not None:
            from .embeddings import to_pg
            _add(db.search_docs_semantic(to_pg(query_embedding), pids, limit=per_source),
                 lambda r: {"kind": "page", "ref": r["id"], "title": r["title"],
                            "project_id": r["project_id"], "description": r.get("description") or None,
                            # Pas de surlignage lexical sur un hit sémantique pur → passage
                            # de repli = début du corps (à défaut, le chapô), pour juger la
                            # pertinence sans ouvrir la page (oto-backend#6).
                            "passage": _snippet(r.get("body_excerpt") or r.get("description") or "") or None,
                            "updated_at": r.get("updated_at"), "matched_by": "semantic"})
    if "brief" in wanted:
        _add(db.search_project_briefs(q, pids, limit=per_source), lambda r: {
            "kind": "brief", "ref": r["id"], "title": r["name"],
            "project_id": r["id"],
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})
        # Sémantique sur les briefs (#6 C) — fusionnée au lexical par RRF, comme les pages.
        if query_embedding is not None:
            from .embeddings import to_pg
            _add(db.search_briefs_semantic(to_pg(query_embedding), pids, limit=per_source),
                 lambda r: {"kind": "brief", "ref": r["id"], "title": r["name"],
                            "project_id": r["id"],
                            "passage": _snippet(r.get("body_excerpt") or "") or None,
                            "updated_at": r.get("updated_at"), "matched_by": "semantic"})
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
        # Sémantique sur les guides on-demand (#6 C).
        if query_embedding is not None:
            from .embeddings import to_pg
            _add(db.search_guides_semantic(to_pg(query_embedding), org_id, sub, limit=per_source),
                 lambda r: {"kind": "guide", "ref": {"scope": r["scope"], "slug": r["slug"]},
                            "title": r["title"] or r["slug"],
                            "description": r.get("description") or None,
                            "passage": _snippet(r.get("body_excerpt") or "") or None,
                            "updated_at": r.get("updated_at"), "matched_by": "semantic"})

    # ── conteneurs ──────────────────────────────────────────────────────────
    if "tableau" in wanted:
        _add(_match_tableaux(q, sub, org_id), lambda r: r)
    # ── lignes de datastore (#67 V2.1/V2.2) — le contenu DANS les tableaux ────
    if "ligne" in wanted:
        _add(_match_rows(q, sub, org_id), lambda r: r)
        # Sémantique (V2.2) : lignes des namespaces OPT-IN proches du sens de la
        # requête, fusionnées au lexical par RRF (comme les pages).
        if query_embedding is not None:
            _add(_match_rows_semantic(q, sub, org_id, query_embedding), lambda r: r)
    if "fichier" in wanted:
        _add(db.search_files_meta(q, pids, limit=per_source), lambda r: {
            "kind": "fichier", "ref": r["id"],
            "title": r.get("title") or r["filename"],
            "description": r.get("description") or None,
            "project_id": r["project_id"],
            "updated_at": r.get("created_at"), "matched_by": "lexical"})
        # Le CONTENU du fichier (#298) — jusqu'ici un PDF de trente pages était, pour
        # la recherche, un nom de fichier : mal nommé, introuvable. Source SÉPARÉE
        # parce que le texte vit dans une autre table et qu'un index d'expression ne
        # couvre pas une jointure — mais même `kind` et même `ref`, donc le RRF réunit
        # les deux : un fichier trouvé par son nom ET par son contenu cumule ses rangs
        # et remonte. `matched_by='content'` dit lequel des deux a répondu, et le
        # `passage` porte l'extrait — c'est lui qui montre POURQUOI le fichier sort.
        _add(db.search_file_contents(q, pids, limit=per_source), lambda r: {
            "kind": "fichier", "ref": r["id"],
            "title": r.get("title") or r["filename"],
            "passage": _snippet(r.get("headline") or "") or None,
            "project_id": r["project_id"],
            "updated_at": r.get("created_at"), "matched_by": "content"})
    if "connecteur" in wanted and connectors_catalog:
        _add(_match_connectors(q, connectors_catalog), lambda r: r)

    # ── fusion RRF : dédup par (kind, ref) en SOMMANT les rangs réciproques ──
    # Une page trouvée par lexical ET sémantique cumule ses deux contributions →
    # remonte (le vrai gain du RRF). On garde le passage lexical s'il existe.
    fused: dict[tuple, tuple[float, dict]] = {}
    for score, hit in ranked:
        key = (hit["kind"], str(hit["ref"]))
        if key in fused:
            prev_s, prev_h = fused[key]
            merged = prev_h if prev_h.get("passage") else hit
            if not merged.get("passage") and hit.get("passage"):
                merged = hit
            fused[key] = (prev_s + score, merged)
        else:
            fused[key] = (score, hit)
    ordered = sorted(fused.values(), key=lambda t: t[0], reverse=True)
    hits = [h for _, h in ordered[:limit]]
    names = db.project_names(sorted({h["project_id"] for h in hits if h.get("project_id")}))
    for h in hits:
        if h.get("project_id") in names:
            h["project_name"] = names[h["project_id"]]
        # oto-backend, feedback #1005/#1006 : dit si CE hit vient d'un partage
        # cross-org plutôt que d'une ressource propre du contexte — jamais deviné,
        # jamais omis, pour qu'un agent ne restitue pas un résultat partagé comme
        # s'il appartenait au client audité. `ns_ids_partages` (tableau/ligne) est
        # peuplé plus bas, avant ce tour — voir `_accessible_namespaces`.
        if h.get("project_id") is not None:
            h["cross_org_share"] = h["project_id"] in granted_pids
        elif h.get("kind") == "tableau":
            h["cross_org_share"] = h["ref"] in ns_ids_partages
        elif h.get("kind") == "ligne":
            h["cross_org_share"] = h["ref"].get("ns_id") in ns_ids_partages

    # Télémétrie (lot 3 Ship 1 §5) : le calllog ne trace que le MCP — log applicatif
    # anonymisé (hash de q, jamais la saisie) pour rendre les conditions V2 décidables.
    logger.info("search q_hash=%s scope=%s kinds=%s n=%d",
                hashlib.sha256(q.encode()).hexdigest()[:12], scope,
                ",".join(sorted(wanted)) if kinds else "*", len(hits))

    # `matched_by` racine DÉRIVÉ des hits (était codé « lexical » en dur → mentait
    # quand tout venait du sémantique, oto-backend#6). semantic si tous les hits le
    # sont, mixed si les deux familles cohabitent, lexical sinon.
    tones = {h.get("matched_by") for h in hits}
    root_matched = ("semantic" if tones == {"semantic"}
                    else "mixed" if "semantic" in tones and len(tones) > 1
                    else "lexical")
    out: dict = {"hits": hits, "count": len(hits), "matched_by": root_matched}
    # oto#42, entrée 2 du lot 1 : `len(ordered)` est connu ICI, trois lignes après la
    # coupe, et ne sortait pas. `count` dit honnêtement ce que la réponse contient —
    # mais rien ne disait qu'il y avait plus, et une recherche qui rend 20 sur 300 se
    # lit « voilà tout ce qui existe ». Une absence ne se rappelle jamais : l'agent
    # cherche, ne trouve pas ce qu'il voulait, et conclut que ça n'existe pas.
    # Ne se dit QUE sur troncature réelle — pas d'écart, pas de bruit.
    if len(ordered) > len(hits):
        out["total"] = len(ordered)
        out["truncated"] = True
        out["hint_truncated"] = (
            f"{len(ordered)} résultats trouvés, {len(hits)} rendus (les mieux classés). "
            "Relance avec `limit` plus haut, ou resserre la requête — ce qui manque "
            "n'est pas absent, il est plus bas dans le classement.")
    if not hits:
        out["hint"] = ("Aucun résultat — reformule (la V1 est lexicale : essaie les "
                       "mots exacts du contenu), ou navigue : `oto_project op=list` "
                       "puis l'épine du projet.")
    return out


def _accessible_namespaces(sub: str, org_id: int) -> list[dict]:
    """Namespaces datastore du CONTEXTE : owners (org + moi + mes groupes) ∪ grants
    org/groupe — parité EXACTE du listing datastore (mêmes fonctions db, sujet du
    tripwire d'étanchéité).
    ⚠️ Cette parité était FAUSSE du 04/09 (ADR 0068) au 04/09 (#870) : la liste, elle,
    ne prenait que l'org — la recherche voyait un tableau personnel que la liste
    cachait. La phrase affirmait donc un invariant que le code ne tenait pas, et
    personne ne pouvait le savoir en la lisant. Elle est vraie depuis, et
    `test_parite_recherche_liste` la tient au lieu de la répéter. Source unique du scoping des sources `tableau` ET `ligne`
    → l'invariant « cherchable ⇔ lisible » tient au grain ligne par héritage du ns."""
    principals = ownership.active_org_principals(sub, org_id)
    gids = [int(p[1]) for p in principals if p[0] == "group"]
    rows = db.list_datastores_for_owners(principals)
    seen = {r["id"] for r in rows}
    rows += [r for r in db.list_datastores_granted_to(sub, [org_id], gids)
             if r["id"] not in seen]
    return rows


def _granted_namespace_ids(sub: str, org_id: int) -> set[int]:
    """Ids de namespace visibles UNIQUEMENT via un partage cross-org (grant
    org/groupe) — jamais un namespace possédé par le contexte, même s'il est
    AUSSI partagé explicitement. Même distinction que
    `ownership.accessible_project_ids_by_provenance`, pour les tableaux/lignes."""
    principals = ownership.active_org_principals(sub, org_id)
    gids = [int(p[1]) for p in principals if p[0] == "group"]
    owned = {r["id"] for r in db.list_datastores_for_owners(principals)}
    return {r["id"] for r in db.list_datastores_granted_to(sub, [org_id], gids)
            if r["id"] not in owned}


def _match_tableaux(q: str, sub: str, org_id: int) -> list[dict]:
    """Tableaux du CONTEXTE matchés en mémoire sur nom + labels de colonnes du schéma.
    Rang : nom exact > nom partiel > label."""
    rows = _accessible_namespaces(sub, org_id)
    fq = fold(q)
    scored: list[tuple[int, dict]] = []
    for r in rows:
        name = fold(r["datastore"])
        labels = " ".join(
            fold(str(f.get("label") or f.get("key") or ""))
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
            "kind": "tableau", "ref": r["id"], "title": r["datastore"],
            "matched_by": "lexical"}))
    scored.sort(key=lambda t: t[0])
    return [h for _, h in scored]


def _row_hits(rows: list[dict], names: dict[int, str], matched_by: str) -> list[dict]:
    """Forme commune d'un hit `ligne` (lexical ou sémantique). Titre = nom du namespace ;
    `ref = {ns_id, row_id}` pour deep-linker la ligne ; passage = fragment JSON surligné
    (lexical), à défaut un début de la ligne."""
    out: list[dict] = []
    for r in rows:
        passage = (r["headline"] if _headline_ok(r.get("headline"))
                   else _snippet(r.get("excerpt") or "") or None)
        out.append({
            "kind": "ligne", "ref": {"ns_id": r["ns_id"], "row_id": r["row_id"]},
            "title": names.get(r["ns_id"]) or "ligne", "passage": passage,
            "updated_at": r.get("updated_at"), "matched_by": matched_by})
    return out


def _match_rows(q: str, sub: str, org_id: int) -> list[dict]:
    """Lignes de datastore des namespaces accessibles, en LEXICAL (kind=ligne, #67 V2.1) —
    rend le CONTENU des tableaux trouvable (ex. « la ligne où figure Sylvie »), pas
    seulement leur nom. Même FTS que la prose (index d'expression), déjà classé par le SQL."""
    ns = _accessible_namespaces(sub, org_id)
    if not ns:
        return []
    names = {r["id"]: r["datastore"] for r in ns}
    return _row_hits(db.search_datastore_rows_fts(q, list(names.keys())), names, "lexical")


def _match_rows_semantic(q: str, sub: str, org_id: int,
                         query_embedding: list[float]) -> list[dict]:
    """Lignes SÉMANTIQUEMENT proches (#67 V2.2) — seuls les namespaces opt-in ont des
    embeddings (les autres sont naturellement absents). Scope IDENTIQUE au lexical
    (mêmes namespaces accessibles → invariant « cherchable ⇔ lisible »)."""
    ns = _accessible_namespaces(sub, org_id)
    if not ns:
        return []
    names = {r["id"]: r["datastore"] for r in ns}
    from .embeddings import to_pg
    rows = db.search_datastore_rows_semantic(to_pg(query_embedding), list(names.keys()))
    return _row_hits(rows, names, "semantic")


def _match_connectors(q: str, catalog: list[dict]) -> list[dict]:
    """Connecteurs du catalogue VISIBLE (injecté par la capacité — activation × RBAC
    déjà appliqués), matchés en mémoire sur name/label/description."""
    fq = fold(q)
    if not fq:
        return []
    scored: list[tuple[int, dict]] = []
    for c in catalog:
        name, label = fold(c.get("name", "")), fold(c.get("label", ""))
        blurb = fold(f"{c.get('help', '')} {c.get('description', '')}")
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
