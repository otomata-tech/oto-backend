"""Ce qui LIT une page ou l'arbre, sans rien écrire : `list`, `search`, `get` (la page,
ou UNE de ses régions), `outline` (son plan), `backlinks`.

`list` et `search` résolvent leur projet par `project_id` ; `get` et `backlinks`
reçoivent la ligne déjà résolue par le gate `doc_id` du dispatcher.
"""
from __future__ import annotations

from typing import Optional

from ... import db, output_projection
from .._types import ResolvedCtx
from ... import doc_patch
from . import common, region, view
from .common import require


def liste(sub: Optional[str], inp) -> dict:
    require(inp.project_id is not None, "missing_project", common.PROJET_REQUIS)
    require(common.can(sub, inp.project_id, "read"), "forbidden", "Access refused.", 403)
    # Une liste sert à choisir quoi ouvrir : elle rend l'INDEX de l'arbre, pas les
    # corps (37 pages = 201 K caractères, refusés par le client). `body_length`
    # remplace `body_md` ; `fields=["*"]` rend le brut. (`fields` est validé en tête
    # du dispatcher, pour toutes les ops d'un coup — voir `view.FIELDS_OPS`.)
    pages = db.list_docs_for_project(int(inp.project_id))
    # #1087 : une page ARCHIVÉE sort de la liste par défaut — et la liste DIT combien
    # elle en retire, sinon « 12 pages » se lirait comme le projet entier.
    archivees = sum(1 for d in pages if d.get("archived_at"))
    if not inp.include_archived:
        pages = [d for d in pages if not d.get("archived_at")]
    rows, notice = output_projection.summarize(
        [view.view(d, sub) for d in pages],
        body_fields=("body_md",), fields=inp.fields, always=view.ALWAYS)
    return {"project_id": inp.project_id, "docs": rows,
            **({"archived_hidden": archivees} if archivees and not inp.include_archived
               else {}),
            **({"projection": notice} if notice else {})}


def search(ctx: ResolvedCtx, inp) -> dict:
    # DÉPRÉCIÉ (lot 3 Ship 1) : rerouté sur le chemin UNIQUE de recherche
    # (`oto_search` scope=project kinds=page) — un seul verbe, un seul code.
    # Forme de sortie conservée-approchée (`results`), + le pointeur.
    sub = ctx.sub
    require(inp.project_id is not None, "missing_project", common.PROJET_REQUIS)
    require(inp.query and inp.query.strip(), "missing_query", "`query` is required.")
    require(common.can(sub, inp.project_id, "read"), "forbidden", "Access refused.", 403)
    from ... import search as search_mod
    out = search_mod.search(sub, ctx.org_id, inp.query.strip(),
                            scope="project", project_id=int(inp.project_id),
                            kinds=["page"])
    return {"project_id": inp.project_id, "query": inp.query.strip(),
            "deprecated": "use oto_search (scope=project) — same path, all sources",
            "results": [{"id": h["ref"], "project_id": h.get("project_id"),
                         "title": h["title"], "snippet": h.get("passage") or "",
                         "updated_at": h.get("updated_at")} for h in out["hits"]]}


def get(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    # La SEULE lecture qu'ouvre un partage de page (#1084) — cf. `common.acces_a_la_page`.
    acces = common.acces_a_la_page(sub, row)
    require(acces is not None, "forbidden", "Access refused.", 403)
    if inp.section is not None or inp.region is not None:
        out = _une_region(sub, inp, row)
    else:
        # Une lecture nue rend la page entière ; `fields` est HONORÉ quand il est là
        # (#461/#525 : lire le seul `rev` avant un patch ne doit plus coûter la page).
        out = view.projected(row, sub, inp.fields, brut_par_defaut=True)
    if acces == common.PAR_LA_PAGE:
        # L'adresse servie ouvre la page DANS son projet, que ce lecteur ne lit pas : un
        # lien mort se subit, un `None` se dit (`view.doc_url`).
        out["url"] = None
    return out


def _une_region(sub: Optional[str], inp, row: dict) -> dict:
    """`op=get section=…|region="preamble"` : UNE région de la page, adressée comme
    `op=patch` l'adresse (signal #1266 — une page de 170 000 caractères se lisait
    entière pour une section). La résolution et ses refus sont ceux du patch
    (`region.lire`) : une section introuvable rend `unknown_section` avec la liste
    des sections, plusieurs titres homonymes `ambiguous_section`.

    Forme FIXE (identité, `rev` de la page entière, la région) : `fields` y est
    refusé plutôt qu'avalé — il projette une PAGE, pas une région."""
    vise_section = bool(inp.section and inp.section.strip())
    require(not (vise_section and inp.region), "ambiguous_target",
            "`section` and `region` designate two different targets: pass only one.")
    require(vise_section or inp.region, "missing_target",
            "`section` is empty: pass a heading text, or "
            f"{region.POIGNEE_PREAMBULE}, or neither to read the whole page.")
    require(inp.fields is None, "unsupported_fields",
            "`fields` projects a whole page: a section read returns a fixed shape. "
            "Remove `fields`, or drop `section`/`region`.")
    corps = row.get("body_md") or ""
    lu = region.lire(corps, inp.section if vise_section else None, region.PAGE)
    return {"id": row["id"], "project_id": row["project_id"], "title": row.get("title"),
            "url": view.doc_url(sub, row),
            "rev": db.doc_rev(row.get("title"), corps),
            "body_md_length": len(corps), **lu}


def outline(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    """`op=outline` : le PLAN de la page — ses titres, leur niveau, leur ligne et ce que
    coûterait la lecture de chaque section (signal #1266). De quoi choisir la section à
    lire (`op=get section=`) ou à patcher, sans payer le corps. Même porte que `get`."""
    require(common.acces_a_la_page(sub, row) is not None, "forbidden",
            "Access refused.", 403)
    corps = row.get("body_md") or ""
    preambule = doc_patch.preamble(corps)
    return {"id": row["id"], "project_id": row["project_id"], "title": row.get("title"),
            "rev": db.doc_rev(row.get("title"), corps), "body_md_length": len(corps),
            "preamble_length": len(preambule) if preambule.strip() else 0,
            "sections": doc_patch.outline(corps)}


def backlinks(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    # « Cité par » (Ship 4) : les pages qui mentionnent celle-ci via [[…]],
    # FILTRÉES par accès (une page d'un projet non lisible ne fuite pas).
    require(common.can(sub, pid, "read"), "forbidden", "Access refused.", 403)
    seen: dict[int, bool] = {}

    def _readable(prj: int) -> bool:
        if prj not in seen:
            seen[prj] = common.can(sub, prj, "read")
        return seen[prj]

    tous = db.doc_backlinks(int(inp.doc_id))
    cites = [b for b in tous if _readable(b["project_id"])]
    # `url` (oto-backend#660) : dépend du tenant du lecteur, posée ici — comme
    # `docs/view.py::doc_url` la pose pour une page lue seule — pas dans `db`.
    for b in cites:
        b["url"] = view.doc_url(sub, b)
    out = {"doc_id": inp.doc_id, "backlinks": cites, "count": len(cites)}
    # oto#42, entrée 4 : le filtrage d'accès retirait des citations en silence, et le
    # hint affirmait alors « personne ne cite encore cette page » — une phrase FAUSSE
    # servie à un agent qui n'avait aucun moyen de le savoir. « Trois pages la citent,
    # tu n'y as pas accès » et « aucune page ne la cite » appellent deux gestes
    # opposés : demander un accès, ou écrire le lien qui manque.
    # ⚠️ Le NOMBRE n'est pas rendu : il révélerait combien de pages existent dans des
    # projets fermés à l'appelant. Le fait qu'il y en ait est déjà l'information utile
    # — le nombre demanderait un arbitrage de divulgation, pas une nuit de correctifs.
    masquees = len(tous) - len(cites)
    if masquees:
        out["hidden_by_access"] = True
        out["hidden_hint"] = (
            "Pages cite this one from projects you have no access to: this list is "
            "PARTIAL. Ask for access to the project concerned if you need the full "
            "graph.")
    if not cites and not masquees:
        # Un zéro muet se lit comme « la fonction ne marche pas » : personne ne
        # peut deviner que SEUL `[[Titre]]` compte (signal #244 — trois formats
        # de lien essayés, tous inertes, aucun indice nulle part).
        out["hint"] = (
            f"No page cites this one yet. A backlink is born from a wiki link "
            f"`[[{row.get('title') or 'Exact title'}]]` written in the body of another "
            "page (resolved at write time, case-insensitive) — prose, "
            "`[text](doc:ID)` and `[text](/docs/ID)` create none.")
    return out
