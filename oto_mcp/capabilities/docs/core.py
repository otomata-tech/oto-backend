"""Le dispatcher `me.doc` et son descripteur — la seule surface du domaine « doc ».

Un Doc appartient à un projet et **hérite de son accès** (`ownership.can_access` sur le
projet — pas d'ownership propre). Le `brief_md` du projet reste la page d'entrée ; les
Docs sont les pages, en arbre via `parent_id`. kind ∈ {doc (humain), note (agent),
source (import)}. CRUD + move, co-déclaré MCP+REST.

⚠️ **L'ORDRE des branches de `_doc` est un contrat, pas une mise en page.** Les créations,
les deux lectures par projet (`list`, `search`) et la liste des pages reçues
(`shared_with_me`) se résolvent AVANT le gate `doc_id`. Les
remonter ou les descendre change ce qui est atteignable ; c'est la raison pour laquelle le dispatcher reste ici, entier et
lisible d'un coup, pendant que les corps vivent dans les modules du domaine.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import db
from .._authz import PROJECT_SHARED_READ
from .. import _portee, _publication
from .._types import Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES
from . import archivage, common, history, partage, patch, reads, view, writes
from .common import require


class DocInput(BaseModel):
    op: Literal["create", "bulk_create", "list", "search", "get", "update", "patch",
                "delete", "move", "revisions", "revert", "set_public", "backlinks",
                "shared_with_me", "outline", "archive", "unarchive", "verify"]
    project_id: Optional[int] = None   # create / list / search
    doc_id: Optional[int] = None       # get / update / delete / move
    query: Optional[str] = None        # search : termes recherchés dans titre + corps
    parent_id: Optional[int] = None    # create / move (None = 1er niveau sous le projet)
    title: Optional[str] = None
    body_md: Optional[str] = None
    kind: Optional[Literal["doc", "note", "source"]] = None
    description: Optional[str] = None  # chapô (Ship 2) — '' efface (fallback dérivé)
    position: Optional[int] = None     # move : INDEX cible (0-based) dans la fratrie
    public: Optional[bool] = None      # set_public : True = partager publiquement, False = retirer
    expected_rev: Optional[str] = None  # update/patch : rev (ETag) lue par le client → conflit optimiste
    # patch ET get (#1266) : titre (heading markdown) de la section ciblée — la MÊME
    # résolution pour lire et pour écrire (`docs/region`).
    section: Optional[str] = None
    # patch ET get : la RÉGION sans titre visée, sur un axe SÉPARÉ de `section`.
    region: Optional[Literal["preamble"]] = None
    mode: Optional[Literal["replace", "append", "prepend", "delete"]] = None  # patch : défaut replace
    to_project: Optional[int] = None    # move : projet cible (déplacer la page + son sous-arbre)
    pages: Optional[list[dict]] = None  # bulk_create : [{title, body_md?, kind?, description?, parent_index? | parent_id?}]
    # revert (#657) : la version à restaurer, adressée par le `id` que rend op=revisions.
    # PAS un numéro d'ordre : `doc_revisions` n'en porte aucun, et un rang calculé sur
    # une liste plafonnée (`limit`) désignerait une autre version au prochain appel.
    revision_id: Optional[int] = None
    # shared_with_me : la PORTÉE des pages reçues. Omis = la vue historique (tout ce que
    # l'appelant reçoit, toutes orgs confondues) — un contrat servi se double, il ne se
    # durcit pas en place.
    scope: Optional[Literal["me", "org"]] = Field(default=None, description=(
        "shared_with_me only: `me` = pages shared with YOU as a person; `org` = pages "
        "shared with the organization you are acting in (and your teams in it), plus "
        "those shared with you as a person whose project belongs to that organization "
        "(or, when you are not a member of the project's organization, to your active "
        "one). Omitted = both, "
        "across all your organizations. Served in any organization."))
    # list (#1087) : True = remontre aussi les pages ARCHIVÉES (retirées par défaut).
    include_archived: Optional[bool] = None
    # delete (#657) : True = ne supprime RIEN, rend seulement ce que la suppression
    # emporterait (de quoi annoncer « ceci supprimera N pages » avant de la faire).
    # patch (oto#171) : True = n'écrit rien, rend la région que le patch retirerait.
    dry_run: Optional[bool] = None
    # Projection de SORTIE, honorée par list/get/create/update/patch/move (`view.FIELDS_OPS`)
    # et REFUSÉE ailleurs. Omis : la liste rend son index, `get` la page entière, une
    # écriture son accusé. `["*"]` = la page entière partout.
    fields: Optional[list[str]] = Field(default=None, description=(
        "Output projection on list/get/create/update/patch/move (refused elsewhere). "
        "Omitted, a list returns its index and a write its receipt; `[\"*\"]` returns "
        "the whole page everywhere; a list of names picks columns."))


def _doc(ctx: ResolvedCtx, inp: DocInput) -> dict:
    sub = ctx.sub
    if sub is None:
        # Endpoint publié sans login : l'autz a déjà validé le contexte, on borne ici
        # les VERBES (lecture seule). `common.can(None, …)` borne le PÉRIMÈTRE au projet.
        require(inp.op in common.SHARED_READ_OPS, "forbidden",
                "Read-only on a shared project.", 403)

    # `fields` se valide UNE fois, pour toutes les ops — et APRÈS l'autz, pour qu'un
    # appelant anonyme se heurte au 403 plutôt qu'à un 400 qui lui décrirait la surface.
    if inp.fields is not None:
        require(inp.op in view.FIELDS_OPS, "unsupported_fields",
                f"`fields` only applies to ops {', '.join(sorted(view.FIELDS_OPS))} — "
                f"op={inp.op} returns a fixed shape. Remove it.")
        require(bool(inp.fields), "empty_fields",
                "`fields` is an empty list: omit it for the default view, pass "
                '`["*"]` for the whole page, or name the columns you want.')

    # `revision_id` et `dry_run` n'ont de sens que sur UNE op chacun. Un argument
    # accepté-et-ignoré coûte exactement ce qu'il prétendait économiser, et rien ne le
    # signale à l'appelant (leçon générale du signal #461) — ici le prix serait pire
    # qu'un surcoût : `dry_run=true` avalé par une op qui supprime pour de bon détruit
    # précisément ce que l'appelant croyait seulement simuler.
    if inp.revision_id is not None:
        require(inp.op == "revert", "unsupported_revision_id",
                "`revision_id` only applies to op=revert (the version to restore). "
                "To READ the history, use op=revisions, which takes only `doc_id`.")
    if inp.scope is not None:
        require(inp.op == "shared_with_me", "unsupported_scope_arg",
                "`scope` only applies to op=shared_with_me (the scope of the pages "
                "received). Remove it.")
    if inp.include_archived is not None:
        require(inp.op == "list", "unsupported_include_archived",
                "`include_archived` only applies to op=list. An archived page is read "
                "with op=get like any other. Remove it.")
    if inp.dry_run is not None:
        require(inp.op in ("delete", "patch"), "unsupported_dry_run",
                "`dry_run` only applies to op=delete and op=patch — the other ops "
                "have no simulation mode and would run for real. Remove it.")

    if inp.op == "create":
        return writes.create(sub, inp)

    if inp.op == "bulk_create":
        return writes.bulk_create(sub, inp)

    if inp.op == "list":
        return reads.liste(sub, inp)

    if inp.op == "search":
        return reads.search(ctx, inp)

    if inp.op == "shared_with_me":
        # Ce qu'on a REÇU, page par page (#1084) : le périmètre est l'appelant, jamais un
        # argument — un `project_id`/`doc_id` passé ici serait avalé sans effet.
        require(inp.project_id is None and inp.doc_id is None, "unsupported_scope",
                "op=shared_with_me takes no project_id or doc_id: it lists the pages "
                "shared with YOU, across projects. Read one with op=get doc_id.")
        if inp.scope == "org":
            require(ctx.org_id is not None, "no_active_org",
                    "scope=\"org\" lists the pages shared with the organization you act "
                    "in, and there is none: pass `_org=<id>`.")
        # Lentille « moi » (sans `scope`, ou `me`) : servie dans toute org (29/09/2026 :
        # une org perso est une org comme une autre).
        return partage.recus(sub, inp.scope, ctx.org_id)

    # ops par doc_id (résolvent le projet pour l'autz)
    require(inp.doc_id is not None, "missing_doc", "`doc_id` is required.")
    row = db.get_doc_by_id(int(inp.doc_id))
    # F-016 : une page qu'on ne peut pas LIRE (ni par son projet, ni par un partage de
    # la page) n'existe pas pour l'appelant — même 404 qu'un id absent. Les ids sont
    # des entiers séquentiels globaux : un 403 ici les rendait énumérables, toutes orgs
    # confondues. Le 403 des ops d'écriture reste à qui lit la page sans pouvoir
    # l'écrire (il sait déjà qu'elle existe). Écrire implique lire (`can_access`).
    require(row is not None and common.acces_a_la_page(sub, row) is not None,
            "unknown_doc", f"Unknown doc #{inp.doc_id}.", 404)
    pid = row["project_id"]

    if inp.op == "get":
        return reads.get(sub, inp, row, pid)

    if inp.op == "outline":
        return reads.outline(sub, inp, row, pid)

    if inp.op == "archive":
        return archivage.archive(sub, inp, row, pid)

    if inp.op == "unarchive":
        return archivage.unarchive(sub, inp, row, pid)

    if inp.op == "verify":
        return archivage.verify(sub, inp, row, pid)

    if inp.op == "revisions":
        return history.revisions(sub, inp, pid)

    if inp.op == "revert":
        return history.revert(sub, inp, row, pid)

    if inp.op == "backlinks":
        return reads.backlinks(sub, inp, row, pid)

    if inp.op == "set_public":
        # Retirer un partage public RÉDUIT la portée : jamais un risque, jamais gardé.
        # Seul `public=True` ouvre.
        if inp.public:
            _publication.refuser_si_agent(
                ctx, "this page",
                "It is shared from the dashboard, on the page itself.")
        return writes.set_public(sub, inp, row, pid)

    if inp.op == "update":
        return writes.update(sub, inp, row, pid)

    if inp.op == "patch":
        return patch.patch(sub, inp, row, pid)

    if inp.op == "delete":
        return writes.delete(sub, inp, row, pid)

    out = writes.move(sub, inp, row, pid)
    # ADR 0068 §4 — déplacer une page vers un AUTRE projet change qui peut la lire :
    # c'est le geste, pas un partage, mais l'effet est le même pour son auteur.
    # On observe le CHANGEMENT de projet, pas le rangement interne d'un même projet.
    if inp.to_project is not None and int(inp.to_project) != int(pid):
        dest = db.get_project_by_id(int(inp.to_project)) or {}
        _portee.observer(
            ctx, ressource_type="doc", ressource_id=inp.doc_id,
            ressource_nom=row.get("title"),
            vers={"org": "org", "group": "group"}.get(dest.get("owner_type"), "person"),
            geste=f"oto_doc op=move to_project={inp.to_project}",
            cible=str(inp.to_project))
    return out


CAPABILITIES += [
    Capability(
        key="me.doc", handler=_doc, Input=DocInput, authz=PROJECT_SHARED_READ,
        description=(
            "Docs (markdown pages tree inside a project; inherit the project's access — "
            "except ONE page shared on its own, see shared_with_me). "
            "A reference page is a DOC, in the PROJECT it belongs to (that project's "
            "« Documents » zone in the dashboard): CAPTURE the sourced facts of a piece of "
            "work there (kind=source/note) as you learn them, and search it before the "
            "web. How the org works (rules, conventions) belongs in its guide "
            "(`oto_guide`, read back by `oto_context`), and what concerns the person in "
            "their profile card (`oto_profile`) — not in a page. "
            "op=create (project_id, title; optional parent_id/body_md/kind) / bulk_create "
            "(project_id + `pages`=[{title, body_md?, kind?, description?, parent_index? | "
            "parent_id?}] → N pages in ONE call, build a tree via parent_index = an earlier "
            "page in the batch, or hang a page under an existing one via its parent_id; "
            "a top-level `parent_id` is the default parent of the batch; any other page "
            "key is refused) / list "
            "(project_id → the page INDEX, build the tree via parent_id: titles and "
            "`body_md_length`, NOT the bodies — pick a page here, then op=get it. "
            '`fields=["*"]` returns whole pages, `fields=[…]` picks columns) / search (project_id + '
            "query → full-text hits {id,title,kind,snippet}: LOCATE a page, then get its "
            "content) / get (the whole page, incl. `rev`, an ETag; pass `fields=[…]` to read "
            'ONLY those columns — `fields=["id","rev"]` gets the rev for an optimistic patch '
            "without paying for the body; `section`=a heading, or `region=\"preamble\"`, "
            "reads ONLY that region, addressed exactly as op=patch addresses it — heading "
            "and sub-sections included) / outline (doc_id → the headings: level, line, `length` of each "
            "section — pick the one to get or patch without reading the page) / update (title/body_md/kind, full body; "
            "snapshots the prior version; pass `expected_rev` from op=get for optimistic "
            "conflict detection → 409 if the page changed since) / patch (edit ONE region in "
            "place, WITHOUT re-emitting the page — this is how you edit a page too long to "
            "re-send: `mode` replace|append|prepend|delete, and ONE target, either "
            "`section`=its markdown heading + `body_md` = that section's BODY, WITHOUT "
            "repeating the heading (the server keeps it) — matched on the heading TEXT, "
            "level and case ignored: when several headings match, the patch is REFUSED "
            "with their list (never applied to the first), OR "
            '`region="preamble"` = everything ABOVE the first heading (provenance banner, '
            '"Last verified" line, front-matter) — it belongs to no section, so no '
            "`section` value can ever reach it; that is a SEPARATE axis, never a reserved "
            'heading name like "__preamble__" (a page may legitimately have such a '
            "heading, and it stays reachable via `section`). Passing both, or neither, is "
            "refused. `mode=delete` removes the target INCLUDING its heading (pass no "
            "`body_md`) — the only way to drop a heading without rewriting the page; to "
            "merely empty a section and keep its heading, use mode=replace with an empty "
            "`body_md`. Two authors on different regions don't clobber; every mode honours "
            "`expected_rev` and snapshots a revision. "
            "SCOPE: a section runs to the next heading of EQUAL-OR-HIGHER level, so its "
            "NESTED sub-sections are part of it — replacing OR deleting a `###` also takes "
            "its `####` children (the response then lists `removed_subsections`). To keep "
            "them, target the sub-heading itself or use mode=append. replace/delete "
            "responses say what went under `removed` (line bounds, `line_count`, "
            "`subsections`) — lines after a heading with no heading between belong to it; "
            "`dry_run: true` writes nothing and returns that same `removed` plus the `rev` "
            "to pass as `expected_rev`) / "
            "A SUCCESSFUL WRITE (create/update/patch/move) returns a RECEIPT, not the page: "
            "id, title, `url`, `rev`, `updated_at` and `body_md_length` — you just wrote the body, "
            'so it is not replayed back at you. Add `fields=["*"]` if you really want the '
            "stored page back, or `fields=[…]` to pick columns. / "
            "A page's `description` is a chapô you STORE: leave it out and the index "
            "DERIVES one from the first prose line of the body (marked `description_derived`), "
            "so it moves with every body edit — that is not an overwrite. Pass `description` "
            "explicitly to pin one that stops following the body. / "
            "EVERY page carries `url` — the web address to READ it, in the reader's own "
            "product. That is the answer to \"where is it?\": hand it over as-is, never "
            "rebuild an address from a pattern. `null` means that reader's product has no "
            "such view — then say where it lives (project + title) rather than invent a link. / "
            "revisions (doc_id → version history, newest first; each row's `id` is what "
            "op=revert takes) / revert (doc_id + `revision_id` from op=revisions → puts "
            "that past title+body back). A revert moves FORWARD: the current state is "
            "snapshotted first, so nothing is lost and a revert can itself be reverted; "
            "the response echoes `reverted_from`. It honours `expected_rev` too — pass it "
            "or you may silently overwrite a peer's edit. It restores a VERSION of a page "
            "that still exists; it does NOT undo a delete (a deleted page took its "
            "revisions with it) / backlinks (doc_id → the "
            "pages that CITE this one). LINK PAGES with `[[Exact page title]]` in body_md — "
            "that wiki-link is the ONLY thing that creates a backlink (prose mentions, "
            "[text](doc:88) and [text](/docs/88) create none). Resolved AT WRITE TIME against "
            "the page's own project first, then every project the ORGANIZATION owns — "
            "never a team project, a personal project or another organization's — "
            "case- and edge-space-insensitive. A title carried by pages of SEVERAL org "
            "projects (none in the page's own) is AMBIGUOUS: nothing is linked and the "
            "write lists the candidates under `citations_ambigues` — make the title "
            "unique rather than guessing. A title "
            "that doesn't exist yet is kept as a stub and links itself once the page is "
            "created or renamed. ⚠️ That is the reach of RESOLUTION, not of the graph, "
            "and they differ BOTH ways. (a) The graph is not symmetric: a page of a team "
            "or personal project resolves into the org's projects, but no page of an org "
            "project can ever link to it. A page "
            "can therefore be cited and still read as an orphan "
            "here: do not use backlinks as a completeness or orphan check without "
            "knowing that. (b) op=backlinks shows every STORED link whatever its project, "
            "including one left behind by a page MOVED between projects — no resolution "
            "would make it today, and it disappears, silently, the next time the citing "
            "page is written. So a cross-project backlink is not proof that the same "
            "`[[…]]`, written now, would resolve. (c) The list is filtered by YOUR "
            "access: citations living in projects you cannot read are removed. When "
            "that happens the response says `hidden_by_access: true` — « nobody cites "
            "this page » and « three pages cite it, you cannot see them » call for "
            "opposite moves, so the second is never reported as the first. The COUNT "
            "of hidden ones is deliberately not given: it would tell you how many "
            "pages exist in projects that are closed to you. Every write says which of its `[[…]]` "
            "found nothing, under `citations_sans_cible` "
            "/ shared_with_me (→ the pages shared WITH YOU one by one — to you, "
            "your org or your team, via oto_resource op=share resource_type=\"doc\": "
            "{id, title, updated_at, role, via, shared_by, url}; `scope` narrows it: "
            "`me` = shared with you as a person, `org` = shared with the organization "
            "you act in and your teams in it (in your personal org, to you as well); "
            "omitted = all of it, across all your organizations; served in any "
            "organization. Such a page is readable "
            "with op=get ALONE: its project, sibling pages, sub-pages, revisions and "
            "backlinks stay closed, and it is read-only) "
            "/ archive (doc_id → the page AND its live sub-pages leave oto_search and "
            "op=list — `include_archived: true` lists them — but stay readable with op=get; "
            "nothing is destroyed; unarchive undoes that same gesture) / verify (doc_id → "
            "stamps `verified_at`/`verified_by` = re-read and still true; body, "
            "`updated_at` and versions untouched) "
            "/ set_public (public: true → shareable public read-only link to THIS PAGE "
            "ALONE: the reader gets its title and body, and nothing else — not the "
            "project, not the sibling pages, not this page's own sub-pages, which each "
            "need their own link ; false → private ; returns public_url. Opening takes "
            "being a member of the org that owns the project — the parent org of an "
            "owning team, the owner themself for a personal project — or 403 "
            "`publish_requires_membership`; closing stays open to whoever writes) "
            "/ delete (removes the page AND its whole subtree, "
            "revisions included — irreversible, there is no trash and no undelete. The "
            "response says how many pages went with it (`descendants`); ask FIRST with "
            "`dry_run: true`, which deletes nothing and returns the same count, whenever "
            "a human has to confirm) / move (reparent/reorder "
            "in-project via parent_id [null=top-level] + position; OR cross-project via "
            "`to_project`=target project id → moves the page AND its subtree there, "
            "write required on both. ⚠️ A move is NOT free for links: the page's own "
            "`[[…]]` are re-resolved in the TARGET project (some become stubs), while "
            "the links pointing AT it are left stored though now out of reach — they "
            "still show in op=backlinks and die on the citing page's next write. After "
            "reorganising a tree, rewrite the citing pages and read their "
            "`citations_sans_cible`). kind ∈ doc|note|source. EMBED A LIVE DATASTORE in a "
            "page body with a fenced block ```oto-data<newline><namespace-name-or-id><newline>``` "
            "→ the viewer renders that datastore's table LIVE (always up to date). Prefer this "
            "over a hand-typed summary table when the data lives in a datastore (single source "
            "of truth, no drift)."
        ),
        errors=(_publication.REFUS_HORS_ORG,),
        mcp="oto_doc",
        rest=RestBinding("POST", "/api/me/docs"),
    ),
]
