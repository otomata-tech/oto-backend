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
  existants (owners du contexte + grants org/groupe, et le personnel dans l'org perso
  seulement — décision du 28/09/2026) ; procédures = org active ; guides = platform ∪
  org active ∪ user (ce dernier dans l'org perso seulement).
- **jamais de LLM au read** ; V1 lexicale (FTS `french` + repli d'accents).
- la source connecteurs (registre en mémoire) est INJECTÉE par la capacité
  (`connectors_catalog`) — ce module ne remonte pas dans la couche adaptateur ;
- la source apps aussi (`apps_catalog`) : les apps que la LISTE des apps de l'org rend
  (`capabilities/apps._org_list` — mêmes seams que la liste des projets), matchées
  en mémoire sur leur titre.

Deux familles de hits : **passages** (prose — page/brief/procedure/guide + ligne de
datastore, avec fragment surligné) et **conteneurs** (tableau/fichier/connecteur —
nom+description, pas d'aperçu). La **ligne** (#67 V2.1) est hybride : un enregistrement
de datastore (`ref={ns_id, row_id}`) matché sur son contenu, rendu avec un fragment
comme la prose. Forme : `{kind, ref, title, description?, passage?, project_id?,
project_name?, updated_at?, matched_by:'lexical', origin_org_id, other_org}` — les deux
derniers sur TOUT hit : l'org où vit l'objet, et s'il vit dans une autre org que l'org
active (`None` = inconnue), cf. `_stamp_origins` et ADR 0071.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from . import db, org_origin, ownership

logger = logging.getLogger(__name__)

_RRF_K = 60
KINDS = ("page", "brief", "procedure", "guide", "tableau", "ligne", "fichier", "connecteur",
         "app")

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
           query_embedding: Optional[list[float]] = None,
           apps_catalog: Optional[list[dict]] = None) -> dict:
    """Le verbe « retrouver ». `scope='project'` restreint à UN projet (déjà validé
    accessible par le caller) ; défaut = tous les projets accessibles de l'org.
    `query_embedding` (fourni par le handler async, hors event loop) active la
    fusion LEXICAL + SÉMANTIQUE des pages par RRF (sinon lexical seul, dégradation
    gracieuse : le sémantique n'est jamais un prérequis)."""
    wanted = set(kinds) if kinds else set(KINDS)
    per_source = min(max(limit, 10), 50)

    if scope == "project" and project_id is not None:
        pids = [int(project_id)]
    else:
        pids = ownership.accessible_project_ids(sub, org_id, want="read")
    # Namespaces du contexte, lus UNE fois par recherche : ils scopent `tableau` ET
    # `ligne` (lexical et sémantique), et leurs propriétaires disent l'org de chaque hit.
    namespaces = (_accessible_namespaces(sub, org_id)
                  if wanted & {"tableau", "ligne"} else [])

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
        # Guides PERSONNELS (`owner_type='user'`) : dans l'org perso seulement, comme
        # tout objet personnel d'une liste (décision du 28/09/2026). `None` : aucun.
        guide_sub = sub if ownership.perso_de_la_liste(sub, org_id) else None
        _add(db.search_guides_fts(q, org_id, guide_sub, limit=per_source), lambda r: {
            "kind": "guide", "ref": {"scope": r["scope"], "slug": r["slug"]},
            "title": r["title"] or r["slug"],
            "description": r.get("description") or None,
            "passage": r["headline"] if _headline_ok(r.get("headline")) else None,
            "updated_at": r.get("updated_at"), "matched_by": "lexical"})
        # Sémantique sur les guides on-demand (#6 C).
        if query_embedding is not None:
            from .embeddings import to_pg
            _add(db.search_guides_semantic(to_pg(query_embedding), org_id, guide_sub,
                                           limit=per_source),
                 lambda r: {"kind": "guide", "ref": {"scope": r["scope"], "slug": r["slug"]},
                            "title": r["title"] or r["slug"],
                            "description": r.get("description") or None,
                            "passage": _snippet(r.get("body_excerpt") or "") or None,
                            "updated_at": r.get("updated_at"), "matched_by": "semantic"})

    # ── conteneurs ──────────────────────────────────────────────────────────
    if "tableau" in wanted:
        _add(_match_tableaux(q, namespaces), lambda r: r)
    # ── lignes de datastore (#67 V2.1/V2.2) — le contenu DANS les tableaux ────
    if "ligne" in wanted:
        _add(_match_rows(q, namespaces), lambda r: r)
        # Sémantique (V2.2) : lignes des namespaces OPT-IN proches du sens de la
        # requête, fusionnées au lexical par RRF (comme les pages).
        if query_embedding is not None:
            _add(_match_rows_semantic(namespaces, query_embedding), lambda r: r)
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
    if "app" in wanted and apps_catalog:
        _add(_match_apps(q, apps_catalog), lambda r: r)

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
    projects = db.project_labels(sorted({h["project_id"] for h in hits if h.get("project_id")}))
    for h in hits:
        if h.get("project_id") in projects:
            h["project_name"] = projects[h["project_id"]]["name"]
    _stamp_origins(hits, sub, org_id, projects, namespaces, apps_catalog or [])

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
    """Namespaces datastore du CONTEXTE — la liste des tableaux de l'org
    (`DatastorePg.list_datastores`), en parité EXACTE : possédés par
    `ownership.proprietaires_collectifs_de_liste` (l'org, mes équipes) ∪ mes tableaux
    perso créés dans cette org (`ownership.mes_tableaux_ici`, 29/09/2026 ; dans mon org
    perso, ceux sans org de création — 07/10/2026)
    ∪ accordés à l'org et à mes équipes ∪, dans mon org perso, partagés à moi en
    personne (décision du 28/09/2026). `test_parite_recherche_liste` tient la
    parité au lieu de la répéter. Source unique du scoping des sources `tableau` ET
    `ligne` → l'invariant « cherchable ⇔ lisible » tient au grain ligne par héritage
    du ns.

    ⚠️ Les équipes sont celles du jeu de principals (mes équipes), pas l'escalade
    d'org_admin de `_active_scope` : c'est l'écart que la parité de liste tolère
    aujourd'hui pour un org_admin, inchangé par ce lot."""
    principals = ownership.principaux_de_liste(sub, org_id)
    gids = [int(p[1]) for p in principals if p[0] == "group"]
    rows = db.list_datastores_for_owners(
        ownership.proprietaires_collectifs_de_liste(sub, org_id))
    seen = {r["id"] for r in rows}
    for r in ownership.mes_tableaux_ici(ownership.mes_objets_ici(sub, org_id)):
        if r["id"] not in seen:
            rows.append(r)
            seen.add(r["id"])
    # Les orgs du jeu de principals, pas `org_id` nu : un bénéficiaire HORS de l'org
    # (projet partagé) n'en reçoit pas les partages (`ownership.owner_de_portee`).
    oids = [int(p[1]) for p in principals if p[0] == "org"]
    recus = db.list_datastores_granted_to(sub, oids, gids)
    if ("user", sub) in principals:
        recus += db.list_datastores_shared_to_user(sub)
    for r in recus:
        if r["id"] not in seen:
            rows.append(r)
            seen.add(r["id"])
    # Vue bornée (oto#270) : même règle que la liste des tableaux.
    return ownership.borner_a_la_vue(sub, ownership.TYPE_RESSOURCE_DATASTORE, rows,
                                     rid=lambda r: r["id"])


def _stamp_origins(hits: list[dict], sub: str, org_id: int,
                   projects: dict[int, dict], namespaces: list[dict],
                   apps: Optional[list[dict]] = None) -> None:
    """Pose sur CHAQUE hit l'org où vit l'objet (`origin_org_id`) et s'il vit dans une
    AUTRE org que l'org active (`other_org`, `None` = on ne sait pas) — ADR 0071 : l'org
    d'un objet, jamais le chemin par lequel on l'atteint (`org_origin`).

    Le champ n'est jamais omis. Par grain :
    - page / brief / fichier → le PROJET (une page partagée seule, #1084, vit dans
      celui de son projet — la recherche ne la rend d'ailleurs pas, elle reste gardée
      par le projet) ;
    - tableau / ligne → le propriétaire du namespace ; personnel → inconnue (§3) ;
    - procédure → l'org active (la source ne lit que les procédures qu'elle possède) ;
    - guide → son palier : org active, plateforme (aucune org), note personnelle
      (inconnue — un nœud personnel ne porte pas son org) ;
    - connecteur → une entrée du catalogue, pas un contenu rangé dans une org ;
    - app → son propriétaire (org, équipe, ou personne et son org de création).

    Coût : aucune requête au-delà de `project_labels` (déjà lue pour les noms), sauf
    UNE, groupée, si un propriétaire est une équipe."""
    ns_by_id = {r["id"]: r for r in namespaces}
    app_by_id = {int(a["id"]): a for a in (apps or [])}
    owners = ([(p["owner_type"], p["owner_id"]) for p in projects.values()]
              + [(ns_by_id[i]["owner_type"], ns_by_id[i]["owner_id"])
                 for i in {_ns_id(h) for h in hits} if i in ns_by_id]
              + [(app_by_id[h["ref"]]["owner_type"], app_by_id[h["ref"]]["owner_id"])
                 for h in hits if h["kind"] == "app" and h["ref"] in app_by_id])
    groups = org_origin.group_orgs(owners)

    def origine(h: dict) -> org_origin.Origine:
        if h.get("project_id") is not None:
            p = projects.get(h["project_id"])
            if p is None:   # effacé entre la recherche et l'étiquette
                return None, False
            return org_origin.org_of(p["owner_type"], p["owner_id"],
                                     context_org_id=p.get("context_org_id"), groups=groups)
        if h["kind"] in ("tableau", "ligne"):
            ns = ns_by_id[_ns_id(h)]
            return org_origin.org_of(ns["owner_type"], ns["owner_id"], groups=groups)
        if h["kind"] == "procedure":
            return org_origin.org_of("org", str(org_id), groups=groups)
        if h["kind"] == "guide":
            scope = h["ref"]["scope"]
            owner_id = {"org": str(org_id), "user": sub}.get(scope, "")
            return org_origin.org_of(scope, owner_id, groups=groups)
        if h["kind"] == "connecteur":
            return None, True
        if h["kind"] == "app":
            a = app_by_id.get(h["ref"])
            if a is None:
                return None, False
            return org_origin.org_of(a.get("owner_type"), a.get("owner_id"),
                                     context_org_id=a.get("context_org_id"), groups=groups)
        raise ValueError(f"grain de recherche sans org d'origine : {h['kind']!r}")

    for h in hits:
        o = origine(h)
        h["origin_org_id"] = o[0]
        h["other_org"] = org_origin.other_org(o, org_id)


def _match_apps(q: str, apps: list[dict]) -> list[dict]:
    """Apps du CONTEXTE (`apps` = ce que la liste des apps de l'org rend) matchées en
    mémoire sur leur titre. Rang : titre exact > titre partiel."""
    fq = fold(q)
    scored: list[tuple[int, dict]] = []
    for a in apps:
        title = a.get("title") or ""
        ft = fold(title)
        if ft == fq:
            rank = 0
        elif fq and fq in ft:
            rank = 1
        else:
            continue
        scored.append((rank, {
            "kind": "app", "ref": int(a["id"]), "title": title,
            "updated_at": a.get("updated_at"), "matched_by": "lexical"}))
    scored.sort(key=lambda t: t[0])
    return [h for _, h in scored]


def _ns_id(hit: dict) -> Optional[int]:
    """Namespace d'un hit `tableau` (sa ref) ou `ligne` (`ref.ns_id`) ; None sinon."""
    if hit["kind"] == "tableau":
        return hit["ref"]
    if hit["kind"] == "ligne":
        return hit["ref"]["ns_id"]
    return None


def _match_tableaux(q: str, rows: list[dict]) -> list[dict]:
    """Tableaux du CONTEXTE (`rows` = `_accessible_namespaces`) matchés en mémoire sur
    nom + labels de colonnes du schéma. Rang : nom exact > nom partiel > label."""
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


def _match_rows(q: str, ns: list[dict]) -> list[dict]:
    """Lignes de datastore des namespaces accessibles (`ns` = `_accessible_namespaces`),
    en LEXICAL (kind=ligne, #67 V2.1) — rend le CONTENU des tableaux trouvable (ex. « la
    ligne où figure Sylvie »), pas seulement leur nom. Même FTS que la prose (index
    d'expression), déjà classé par le SQL."""
    if not ns:
        return []
    names = {r["id"]: r["datastore"] for r in ns}
    return _row_hits(db.search_datastore_rows_fts(q, list(names.keys())), names, "lexical")


def _match_rows_semantic(ns: list[dict], query_embedding: list[float]) -> list[dict]:
    """Lignes SÉMANTIQUEMENT proches (#67 V2.2) — seuls les namespaces opt-in ont des
    embeddings (les autres sont naturellement absents). Scope IDENTIQUE au lexical
    (mêmes namespaces accessibles → invariant « cherchable ⇔ lisible »)."""
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
