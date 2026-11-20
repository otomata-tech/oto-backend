"""Capacité « commentaires d'une page » : ce que les personnes disent d'une page de
mémoire (doc), à côté de son texte (`docs/commentaires-de-procedure.md` §Pages de
mémoire).

`GET|POST /api/me/docs/{doc_id}/comments`, `DELETE …/comments/{comment_id}` — le pendant
exact des commentaires de procédure (`commentaires_procedure.py`), même forme servie, sans
`version` (une page n'a pas de numéro : `version` est toujours `null`). La source est
`doc_comments` (`db/doc_commentaires.py`) — PAS le corps de la page : un commentaire ne
crée aucune révision et ne change pas `updated_at`.

**REST seule.** Aucun outil agent : un agent lit et écrit la page (`oto_doc`) ; les
commentaires sont la conversation des personnes sur la page de l'application.

**Jamais lus pour EXÉCUTER.** Sous un jeton de délégation (un agent hébergé qui
travaille), les lire ET les écrire est refusé (`comments_outside_job`, 403), même règle
que les commentaires de procédure. Le lien public d'une page n'en sert aucun.

**Accès.** Lire ET écrire un commentaire, c'est l'accès de LECTURE de la page
(`docs.common.acces_a_la_page` : par son projet, ou par un partage de CETTE page) : qui
lit une page peut dire ce qu'il en pense. Une page illisible est un 404, comme une
absente. **Retirer** : son auteur, ou qui gouverne la page (= son projet) ; retrait doux.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .. import db, ownership
from ._authz import SUB_ONLY
from ._procedure_du_travail import procedure_du_travail
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .commentaires_procedure import (CITATION_MAX, HORODATAGE, LIMITE_DEFAUT, LIMITE_MAX,
                                     LONGUEUR_MAX, PREFIXE_MAX, Deleted)
from .docs import common as docs_common
from .registry import CAPABILITIES


class _Page(BaseModel):
    doc_id: int = Field(description="The page's id.")


class DocCommentsInput(_Page):
    limit: int = Field(default=LIMITE_DEFAUT, description=(
        f"Comments per page, the most recent page first ({LIMITE_MAX} at most)."))
    before_id: Optional[int] = Field(default=None, description=(
        "Read the next (older) page: the previous answer's `next_before_id`."))


class DocCommentInput(_Page):
    body: str = Field(description=f"The comment, plain text, 1 to {LONGUEUR_MAX} characters.")
    quote: Optional[str] = Field(default=None, description=(
        "The exact passage of the page the comment is about, as it reads (at most "
        f"{CITATION_MAX} characters). Omit to comment on the whole page. Not with "
        "`reply_to`."))
    quote_prefix: Optional[str] = Field(default=None, description=(
        f"Up to {PREFIXE_MAX} characters right before `quote`, to tell two identical "
        "passages apart."))
    reply_to: Optional[int] = Field(default=None, description=(
        "Reply to a thread: the `id` of its first comment."))

    @field_validator("quote", "quote_prefix", mode="after")
    @classmethod
    def _passage(cls, v: Optional[str]) -> Optional[str]:
        v = " ".join((v or "").split())
        return v or None

    @model_validator(mode="after")
    def _ancre(self):
        if self.quote and len(self.quote) > CITATION_MAX:
            raise ValueError(f"`quote` is longer than {CITATION_MAX} characters")
        if self.quote_prefix:
            self.quote_prefix = self.quote_prefix[-PREFIXE_MAX:]
        if self.reply_to is not None and self.quote:
            raise ValueError("a reply has no `quote`: it belongs to its thread's passage")
        if not self.quote:
            self.quote_prefix = None
        return self

    @field_validator("body", mode="after")
    @classmethod
    def _corps(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("`body` is empty")
        if len(v) > LONGUEUR_MAX:
            raise ValueError(f"`body` is longer than {LONGUEUR_MAX} characters")
        return v


class DeleteDocCommentInput(_Page):
    comment_id: int


class DocComment(BaseModel):
    id: int
    reply_to: Optional[int] = Field(default=None, description=(
        "The thread's first comment, for a reply; null for a thread's first comment."))
    quote: Optional[str] = Field(default=None, description=(
        "The passage of the page the thread is about (null = the whole page). A passage "
        "the current text no longer contains was on an earlier version."))
    quote_prefix: Optional[str] = None
    version: Optional[int] = Field(default=None, description=(
        "Always null: a page has no version number (kept for the same shape as process "
        "comments)."))
    author: Optional[str] = Field(default=None, description="The author's sub.")
    author_email: Optional[str] = Field(default=None, description=(
        "Resolved at read time; absent for an unknown account."))
    body: str
    at: Optional[str] = Field(default=None, description=HORODATAGE)
    mine: bool = Field(description="Written by the caller.")
    can_delete: bool = Field(description="The caller may remove it (author or governor).")
    deleted: bool = Field(default=False, description=(
        "A thread's first comment that was removed while replies remain: served without "
        "its text or author, so the thread keeps its passage."))


class DocComments(BaseModel):
    doc_id: int
    total: int = Field(description="Live comments on the page, all pages.")
    comments: list[DocComment] = Field(description="Oldest first within the page.")
    next_before_id: Optional[int] = Field(default=None, description=(
        "Pass as `before_id` to read the older page. `null` = nothing older."))


def _hors_travail() -> None:
    """Un agent hébergé qui travaille ne lit ni n'écrit de commentaire."""
    sous_delegation, _ = procedure_du_travail()
    if sous_delegation:
        raise AuthzDenied(403, "comments_outside_job", (
            "Page comments are not served to a hosted agent's job: comments are for the "
            "people who read or edit the page."))


def _page(ctx: ResolvedCtx, inp: _Page) -> tuple[dict, bool]:
    """La page visée, sous l'accès de LECTURE de l'appelant ; rend `(page, gouverne)`."""
    _hors_travail()
    row = db.get_doc_by_id(int(inp.doc_id))
    if row is None or ctx.sub is None or docs_common.acces_a_la_page(ctx.sub, row) is None:
        raise AuthzDenied(404, "doc_not_found", f"No page #{inp.doc_id} readable from here.")
    return row, ownership.can_govern(ctx.sub, docs_common.DOC_RTYPE, str(row["id"]))


def _servi(c: dict, sub: Optional[str], gouverne: bool, emails: dict) -> dict:
    if c.get("deleted_at") is not None:
        return {"id": c["id"], "reply_to": None, "quote": c.get("quote"),
                "quote_prefix": c.get("quote_prefix"), "version": None,
                "author": None, "author_email": None, "body": "", "at": c["created_at"],
                "mine": False, "can_delete": False, "deleted": True}
    auteur = c["auteur_sub"]
    a_moi = auteur is not None and auteur == sub
    return {
        "id": c["id"],
        "reply_to": c.get("parent_id"),
        "quote": c.get("quote"),
        "quote_prefix": c.get("quote_prefix"),
        "version": None,
        "author": auteur,
        "author_email": emails.get(auteur),
        "body": c["body"],
        "at": c["created_at"],
        "mine": a_moi,
        "can_delete": a_moi or gouverne,
        "deleted": False,
    }


def _doc_comments(ctx: ResolvedCtx, inp: DocCommentsInput) -> dict:
    page_, gouverne = _page(ctx, inp)
    ident = int(page_["id"])
    limit = max(1, min(LIMITE_MAX, inp.limit))
    lus = db.doc_commentaires.lister(ident, avant_id=inp.before_id, limit=limit)
    suite = lus[limit - 1]["id"] if len(lus) > limit else None
    page = list(reversed(lus[:limit]))
    emails = db.emails_by_subs([c["auteur_sub"] for c in page if c.get("deleted_at") is None])
    total = (sum(1 for c in lus if c.get("deleted_at") is None)
             if inp.before_id is None and len(lus) <= limit
             else db.doc_commentaires.compter(ident))
    return {
        "doc_id": ident,
        "total": total,
        "comments": [_servi(c, ctx.sub, gouverne, emails) for c in page],
        "next_before_id": suite,
    }


def _doc_comment(ctx: ResolvedCtx, inp: DocCommentInput) -> dict:
    page_, gouverne = _page(ctx, inp)
    if inp.reply_to is not None:
        racine = db.doc_commentaires.lire(int(page_["id"]), inp.reply_to, servi=True)
        if racine is None or racine.get("parent_id") is not None:
            raise AuthzDenied(404, "comment_not_found", (
                "`reply_to` names no live thread of this page (reply to the thread's "
                "FIRST comment)."))
    c = db.doc_commentaires.ajouter(
        int(page_["id"]), auteur_sub=ctx.sub, body=inp.body, parent_id=inp.reply_to,
        quote=inp.quote, quote_prefix=inp.quote_prefix)
    return _servi(c, ctx.sub, gouverne, db.emails_by_subs([ctx.sub]))


def _delete_doc_comment(ctx: ResolvedCtx, inp: DeleteDocCommentInput) -> dict:
    page_, gouverne = _page(ctx, inp)
    c = db.doc_commentaires.lire(int(page_["id"]), inp.comment_id)
    if c is None:
        raise AuthzDenied(404, "comment_not_found")
    if not gouverne and (ctx.sub is None or c["auteur_sub"] != ctx.sub):
        raise AuthzDenied(403, "comment_not_yours")
    db.doc_commentaires.retirer(c["id"])
    return {"deleted": True}


_CHEMIN = "/api/me/docs/{doc_id}/comments"
_INTROUVABLE = DeclaredError(404, "doc_not_found", "aucune page de cet id lisible par l'appelant")
_HORS_TRAVAIL = DeclaredError(403, "comments_outside_job",
                              "un agent hébergé qui travaille ne lit ni n'écrit les "
                              "commentaires d'une page")

CAPABILITIES += [
    Capability(
        key="me.doc.comments",
        handler=_doc_comments,
        Input=DocCommentsInput,
        Output=DocComments,
        authz=SUB_ONLY,
        rest=RestBinding(verb="GET", path=_CHEMIN, provisoire=True),
        errors=(_INTROUVABLE, _HORS_TRAVAIL),
        description=("Read the comments people left on ONE memory page (doc), oldest first "
                     "within a page; `before_id` reads older ones."),
    ),
    Capability(
        key="me.doc.comment",
        handler=_doc_comment,
        Input=DocCommentInput,
        Output=DocComment,
        authz=SUB_ONLY,
        rest=RestBinding(verb="POST", path=_CHEMIN, status=201, provisoire=True),
        errors=(_INTROUVABLE, _HORS_TRAVAIL,
                DeclaredError(404, "comment_not_found",
                              "`reply_to` ne désigne aucun fil vivant de cette page")),
        description=("Add a comment to ONE memory page (doc), as the caller: on a passage "
                     "(`quote`), on the whole page, or as a reply (`reply_to`). Anyone who "
                     "can read the page can comment. It does not change the page."),
    ),
    Capability(
        key="me.doc.delete_comment",
        handler=_delete_doc_comment,
        Input=DeleteDocCommentInput,
        Output=Deleted,
        authz=SUB_ONLY,
        rest=RestBinding(verb="DELETE", path=_CHEMIN + "/{comment_id}", provisoire=True),
        errors=(
            _INTROUVABLE, _HORS_TRAVAIL,
            DeclaredError(404, "comment_not_found",
                          "aucun commentaire vivant de cet `id` sur cette page"),
            DeclaredError(403, "comment_not_yours",
                          "seuls son auteur et qui gouverne la page retirent un commentaire"),
        ),
        description=("Remove one comment from a memory page (its author or the page's "
                     "governor); a thread whose first comment is removed keeps its replies."),
    ),
]
