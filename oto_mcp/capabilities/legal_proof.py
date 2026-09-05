"""Sortir la PREUVE d'une acceptation légale (oto#42 lot 2).

Le journal `legal_acceptance_events` enregistre, pour chaque acceptation, l'adresse
réseau, l'agent, le contexte et l'org payeuse. **Aucune surface ne les rendait.** En cas
de contestation — la seule situation où ces colonnes servent — la preuve était en base
et il fallait un accès à la production pour la lire.

C'est le cas que l'inventaire d'oto#42 isole comme « le seul qui se paie ailleurs qu'en
confusion » : les autres font écrire une phrase fausse à un agent, celui-ci fait perdre
un litige.

## Ce que cette surface rend, et ce qu'elle refuse de deviner

**Chaque acceptation, jamais la dernière.** `me.legal.get` répond « a-t-il accepté la
version courante ? » et n'a donc qu'une ligne par document. Une preuve, c'est
l'historique : « à telle date, depuis telle adresse, il a accepté telle version ».

**Le total du jeu entier** (oto#42 règle 2). Un historique coupé à `limit` sans dire
combien il en reste ferait écrire « il a accepté deux fois » à qui en compte deux sur
trente.

**⚠️ `ip`/`user_agent`/`context`/`org_id` à `null` ne veulent PAS dire « recopié ».**
Le DDL du journal pose cette équivalence ; elle ne tient que dans un sens. La recopie de
l'ancienne projection les laisse à NULL — mais une acceptation ordinaire arrivée sans
trace de transport aussi. On ne peut donc pas déduire l'origine d'une ligne, et cette
surface ne s'y risque pas : `null` s'y lit « aucune trace enregistrée », jamais « aucune
trace n'existait ».

**⚠️ Le texte d'une version passée n'est pas récupérable, et c'est dit.** `legal_docs.
CURRENT_DOCS` ne porte que la version COURANTE de chaque document. Une acceptation des
CGU 2.0 ne peut donc pas être reliée au texte qu'elle a accepté — `url` reste `null` et
`version_courante` vaut `false`. C'est une **limite de la preuve**, pas de cette
lecture : l'opposer suppose de retrouver le texte d'époque ailleurs (le dépôt du site).
La rendre visible ici est le seul moyen que quelqu'un s'en aperçoive avant d'en avoir
besoin.

## Le palier, et pourquoi celui-là

`PLATFORM_ADMIN`. Une preuve d'acceptation contient une **adresse réseau** et un agent
utilisateur : c'est de la donnée personnelle, lue au sujet d'un tiers. Le geste est le
même que celui d'`admin.instance_health` — un acte de support ou de litige, pas une
consultation ordinaire — et il est journalisé comme tel par `tool_calls`.

⚠️ **Lecture seule, et le sujet ne la voit pas.** Rien ici ne permet à quelqu'un de
sortir SON propre historique : ce serait une autre surface, avec un autre palier, et
elle n'a pas été demandée. Dire ce qui manque à côté de ce qui existe vaut mieux que
l'ajouter sans qu'on l'ait décidé.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .. import db, legal_docs, tenancy
from ._authz import PLATFORM_ADMIN
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class LegalProofInput(BaseModel):
    sub: str = Field(description=("Le compte dont on sort la preuve (`sub` Logto). "
                                  "C'est un TIERS : c'est tout l'objet de l'outil."))
    doc_slug: Optional[str] = Field(
        default=None,
        description="Restreindre à un document (`terms`, `cgv`, `dpa`). Omis = tous.")
    limit: int = Field(default=200, ge=1, le=1000,
                       description="Acceptations rendues, les plus récentes d'abord.")


class Acceptance(BaseModel):
    """Une acceptation, telle qu'on l'oppose."""
    id: int
    doc_slug: str
    version: str
    accepted_at: str
    #: `null` = **aucune trace enregistrée** — jamais « aucune trace n'existait ».
    #: Voir l'avertissement du module : l'origine d'une ligne ne se déduit pas.
    context: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    org_id: Optional[int] = None
    #: La version acceptée est-elle celle qui est servie aujourd'hui ?
    version_courante: bool = False
    #: L'URL du document — servie UNIQUEMENT si la version acceptée est la courante.
    #: Sur une version passée elle reste `null` : le registre ne garde pas les textes
    #: d'époque, et pointer l'URL courante ferait passer le texte d'aujourd'hui pour
    #: celui qui a été accepté.
    url: Optional[str] = None


class LegalProof(BaseModel):
    sub: str
    events: list[Acceptance]
    #: Le total du jeu ENTIER, pas de la page rendue (oto#42 règle 2).
    total: int
    #: `true` = il en reste au-delà de `limit`. Le drapeau ET le total : l'un se voit,
    #: l'autre se compte.
    truncated: bool


def _legal_proof(ctx: ResolvedCtx, inp: LegalProofInput) -> dict:
    rows, total = db.list_acceptance_events(
        inp.sub, doc_slug=(inp.doc_slug.strip() if inp.doc_slug else None),
        limit=int(inp.limit))
    # Les documents EFFECTIFS du sujet, pas ceux de la plateforme : un compte d'un
    # tenant tiers a ses propres CGU (`tenant_legal_docs`), et comparer sa version
    # acceptée au catalogue d'oto dirait « périmée » sur un document jamais servi.
    docs = legal_docs.docs_for(tenancy.current().tenant_of(inp.sub))
    events = []
    for r in rows:
        meta = docs.get(r["doc_slug"]) or {}
        courante = bool(meta.get("version")) and meta["version"] == r["version"]
        events.append({
            "id": int(r["id"]), "doc_slug": r["doc_slug"], "version": r["version"],
            "accepted_at": r["accepted_at"].isoformat() if r["accepted_at"] else "",
            "context": r["context"], "ip": r["ip"], "user_agent": r["user_agent"],
            "org_id": int(r["org_id"]) if r["org_id"] is not None else None,
            "version_courante": courante,
            "url": meta.get("url") if courante else None,
        })
    return {"sub": inp.sub, "events": events, "total": total,
            "truncated": total > len(events)}


CAPABILITIES += [
    Capability(
        key="admin.legal_proof", handler=_legal_proof,
        Input=LegalProofInput, Output=LegalProof,
        authz=PLATFORM_ADMIN,
        description=(
            "[platform admin] The PROOF that someone accepted a legal document: every "
            "acceptance in the journal, newest first, with what situates it — date, IP, "
            "user agent, context (access|purchase) and the org that was paying. Use it "
            "when an acceptance is disputed; `me.legal.get` answers 'is he up to date?' "
            "and keeps only the latest row per document, which is not a proof. "
            "⚠️ `total` counts the WHOLE set, not the page returned. ⚠️ A null ip / "
            "user_agent / context means NO TRACE WAS RECORDED — never 'no trace "
            "existed'; the origin of a row cannot be deduced. ⚠️ `url` is served only "
            "when the accepted version is the current one: the registry keeps no past "
            "texts, so an old acceptance cannot be tied to what it accepted."),
        mcp="oto_admin_legal_proof",
        rest=RestBinding("GET", "/api/admin/users/{sub}/legal/acceptances"),
    ),
]
