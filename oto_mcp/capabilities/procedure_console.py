"""Console procédures MCP consolidée (ADR 0047, B2) — `oto_procedure`.

Réunit les 9 tools MCP du domaine guide/procédure membre en UN : lecture
(`get`/`list`), écriture
(`set` — org_admin au palier org, **membre de l'équipe** au palier `scope='group'` ;
`delete`, destructeur, reste au **chef d'équipe** — #681 ; épinglable par `org` /
`group`) et bibliothèque publique
(`library_list`/`library_get`/`publish`/`fork`/`unpublish`). Les handlers de
domaine (`orgs_instructions`, `guide_library`) sont réutilisés tels quels ;
leurs faces REST `/api/me/instructions*` ne bougent pas (palier org, org_admin).

⚠️ L'index des guides nommés (skills) est APPENDU à la description de CE
tool par `DynamicInstructionsMiddleware.on_list_tools` (via `_GUIDE_GET_TOOL`,
middleware/dynamic_instructions.py) — les skills ne sont pas des outils, c'est leur seul canal de
découverte. Le filtre d'usage (`org.instruction.usage`) compte les appels sur ce
nom de tool.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from . import guide_library
from .orgs import instructions as orgs_instructions
from ._authz import (BY_OP, GROUP_ADMIN_OPT, GROUP_MEMBER_OPT, ORG_ADMIN_OPT,
                     ORG_MEMBER, ORG_MEMBER_OPT, SUB_ONLY)
from . import _publication
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


# Le palier d'autz suit le SCOPE demandé (#681). Deux règles par geste, jamais une
# combinaison à énumérer op par op : `BY_OP(..., fields=("scope",))` est le même
# combinateur, appliqué à l'autre axe. Une valeur de `scope` hors map (« team »,
# « perso »…) est refusée net par le combinateur, avec la liste des valeurs attendues —
# plutôt que silencieusement traitée comme l'org, ce qui écrirait au mauvais endroit.
#
# ⚠️ `None` et `"org"` doivent MAPPER LA MÊME règle : `scope` est optionnel, et son
# absence ne veut pas dire « palier inconnu ».
# ⚠️ Le palier PERSONNEL (`scope='user'`) existe depuis le 04/09/2026 (ADR 0068) —
# mais `None` reste sur l'ORG, et c'est un choix à trancher, pas un oubli. Le basculer
# fait tomber une vingtaine de bancs et une garde (`…_reads_honor_explicit_org`) :
# l'absence de scope voyage jusqu'à des modèles d'entrée en aval qui l'exigent, et
# « privé par défaut » y demande un lot à part. Ouvrir la porte d'abord, déplacer le
# défaut ensuite — dans cet ordre, chaque pas se vérifie seul.
_LIRE = BY_OP({None: ORG_MEMBER_OPT("org"), "user": SUB_ONLY, "org": ORG_MEMBER_OPT("org"),
               "group": GROUP_MEMBER_OPT("group")}, fields=("scope",))
# ⚠️ `set` et `delete` ne partagent PAS une règle : ce n'est pas la surface qui décide du
# palier, c'est le VERBE.
#
# `set` au palier équipe = **membre** de l'équipe. C'est tout le lot #681 : celui qui
# DÉROULE la procédure est un membre, pas un chef — réserver l'écriture au chef réservait
# l'apprentissage à qui n'exécute pas, et la boucle que la procédure promet ne se fermait
# jamais. Le coût mesuré de la garde d'avant n'était pas théorique : pour laisser une
# opératrice annoter son mode d'emploi, il fallait la faire chef d'équipe — un rôle qui
# emporte les CLÉS PARTAGÉES de l'équipe. Une garde d'écriture trop grossière forçait donc
# une élévation de droits dans un domaine sans rapport.
#
# Ce qui rend l'ouverture tenable, c'est que le geste est RÉVERSIBLE : chaque écriture
# ajoute une version et `from_version` restaure la précédente. Le risque d'une procédure
# qui pilote un agent se traite là — par les versions et par le digest qui dit ce qui a
# changé — et non en fermant la porte à ceux qui s'en servent.
# ⚠️ Le refus d'écriture NOMME les autres chemins (04/09/2026). Mesuré sur un cas
# réel, au journal des appels : une membre d'org tente d'écrire une procédure le
# 31/08, réessaie le 02/09, et ne trouve le palier ÉQUIPE que le 04/09 — quatre jours,
# trois refus, pour un geste qui lui était ouvert depuis le début. « Réservé à un
# administrateur » dit qui a le droit ; il ne dit pas ce qu'elle, elle cherchait à
# faire. Un refus qui ne porte pas le geste n'arrête pas la demande, il la déplace.
# ⚠️ Il en nommait UN SEUL — l'équipe — le jour même où le palier PERSONNEL était
# ouvert (ADR 0068). Un refus qui n'énumère qu'une partie des issues envoie chez la
# mauvaise : la personne qui voulait sa propre procédure se serait retrouvée à écrire
# celle de son équipe, partagée avec elle, sans savoir que l'autre existait.
_AUTRES_PALIERS = (
    "Tu n'as pas besoin de l'être pour écrire une procédure — deux paliers te sont "
    "ouverts. `scope='user'` : une procédure qui n'appartient qu'à TOI, que personne "
    "d'autre ne voit, pas même les administrateurs de ton org. `scope='group'` "
    "(+ `group=<id>` si tu es dans plusieurs équipes) : celle de ton ÉQUIPE, partagée "
    "avec ses membres — y écrire demande d'en être MEMBRE, pas chef. "
    "`oto_procedure(op='list')` montre celles que tu vois déjà."
)
# ⚠️ `None` écrit à SOI (04/09/2026, question d'Alexis : « il ne peut pas y avoir un
# défaut à scope ? »). Il valait `org`, donc le défaut menait à un REFUS pour toute
# personne qui n'est pas administratrice — la majorité. Un défaut qui échoue pour le
# plus grand nombre n'est pas un défaut, c'est un piège : on l'a mesuré sur quatre
# jours et trois refus chez une org cliente.
# ⚠️ La LECTURE ne bascule PAS avec lui : `list` sans scope CUMULE déjà l'org et
# l'équipe active, ce qui est le bon geste pour retrouver. Restreindre la lecture au
# palier personnel cacherait ce qu'on cherche. C'est le verbe qui décide, pas la
# surface — la même règle que celle qui sépare `set` de `delete` ci-dessous.
_ECRIRE = BY_OP({None: SUB_ONLY, "user": SUB_ONLY,
                 "org": ORG_ADMIN_OPT("org", _AUTRES_PALIERS),
                 "group": GROUP_MEMBER_OPT("group")}, fields=("scope",))
# `delete` reste au **chef d'équipe** : il emporte la procédure ET tout son historique,
# sans corbeille — rien ne le défait. Un geste destructeur n'est pas un geste de travail.
# Au palier PERSONNEL, supprimer sa propre procédure ne demande personne d'autre :
# le geste destructeur ne l'est que pour son auteur, qui est le seul à la voir.
_SUPPRIMER = BY_OP({None: SUB_ONLY, "user": SUB_ONLY, "org": ORG_ADMIN_OPT("org"),
                    "group": GROUP_ADMIN_OPT("group")}, fields=("scope",))


def _need(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


class ProcedureInput(BaseModel):
    op: Literal["get", "list", "create", "set", "describe", "delete",
                "library_list", "library_get", "publish", "fork", "unpublish"]
    slug: Optional[str] = None
    guide_id: Optional[int] = None         # get : lecture par ID STABLE (ADR 0032)
    doctrine_id: Optional[int] = None      # ALIAS déprécié du précédent (retrait 29/10/2026, #519)
    scope: Optional[str] = None            # org (défaut) | group — LECTURE ET ÉCRITURE
    version: Optional[int] = None          # get
    with_history: bool = False             # get
    query: Optional[str] = None            # list / library_list
    body_md: Optional[str] = None          # set
    title: Optional[str] = None            # set / describe / publish
    description: Optional[str] = None      # set / describe / publish
    from_version: Optional[int] = None     # set (revert)
    # set : verrou OPTIMISTE (#662) — la version lue par l'appelant. Différente de la
    # courante ⟹ 409 `version_conflict`, l'écriture n'a pas lieu.
    expected_version: Optional[int] = None
    slots: Optional[list] = None           # set / create (ADR 0035)
    org: Optional[int] = None              # set/delete : org explicite (#69)
    group: Optional[int] = None            # scope=group : équipe explicite (#681)
    public_slug: Optional[str] = None      # publish
    category: Optional[str] = None         # publish / library_list
    tags: Optional[list] = None            # publish
    visibility: Optional[str] = None       # publish : public | unlisted
    new_slug: Optional[str] = None         # fork
    id: Optional[int] = None               # unpublish : id d'entrée bibliothèque
    author_kind: Optional[str] = None      # library_list : otomata | org
    limit: int = 100                       # library_list


def _ECRIT_SCOPE(inp) -> str:
    """Le palier d'ÉCRITURE effectif — `user` quand rien n'est demandé.

    ⚠️ Il doit dire EXACTEMENT ce que `_ECRIRE` a vérifié : l'autz mappe `None` sur
    `SUB_ONLY`, donc laisser passer `None` en aval ferait écrire à l'ORG une demande
    autorisée au palier PERSONNEL. Le trou serait invisible — l'écriture réussit, et
    au mauvais endroit."""
    return getattr(inp, "scope", None) or "user"


def _dispatch_procedure(ctx: ResolvedCtx, inp: ProcedureInput):
    oi, lib = orgs_instructions, guide_library
    if inp.op == "get":
        return oi._get_guide(ctx, oi.GuideGetInput(
            slug=inp.slug, guide_id=inp.guide_id, doctrine_id=inp.doctrine_id,
            # Pas de repli sur "org" : `None` déclenche la CASCADE de lecture
            # (chez soi d'abord, puis l'org). Écrire à soi par défaut et relire
            # ailleurs par défaut ferait « perdre » la procédure qu'on vient d'écrire.
            scope=inp.scope,
            version=inp.version, with_history=inp.with_history))
    if inp.op == "list":
        return oi._list_guides(ctx, oi.GuideListInput(query=inp.query, scope=inp.scope))
    if inp.op == "create":
        return oi._create_instruction(ctx, oi.ConsoleInstrCreateInput(
            slug=_need(inp.slug, "missing_slug", "`slug` requis pour create."),
            body_md=inp.body_md, title=inp.title, description=inp.description,
            slots=inp.slots, org=inp.org, scope=_ECRIT_SCOPE(inp), group=inp.group))
    if inp.op == "set":
        return oi._set_instruction(ctx, oi.ConsoleInstrSetInput(
            slug=inp.slug, body_md=inp.body_md, title=inp.title,
            description=inp.description, from_version=inp.from_version,
            expected_version=inp.expected_version,
            slots=inp.slots, org=inp.org, scope=_ECRIT_SCOPE(inp), group=inp.group))
    if inp.op == "describe":
        return oi._describe_instruction(ctx, oi.ConsoleInstrDescribeInput(
            slug=_need(inp.slug, "missing_slug", "`slug` requis pour describe."),
            title=inp.title, description=inp.description,
            expected_version=inp.expected_version,
            org=inp.org, scope=_ECRIT_SCOPE(inp), group=inp.group))
    if inp.op == "delete":
        return oi._delete_instruction(ctx, oi.ConsoleGuideDeleteInput(
            slug=_need(inp.slug, "missing_slug", "`slug` requis pour delete."),
            org=inp.org, scope=_ECRIT_SCOPE(inp), group=inp.group))
    if inp.op == "library_list":
        return lib._list(ctx, lib.LibraryListInput(
            query=inp.query, category=inp.category, author_kind=inp.author_kind,
            limit=inp.limit))
    if inp.op == "library_get":
        return lib._get(ctx, lib.LibraryGetInput(
            slug=_need(inp.slug, "missing_slug", "`slug` (public) requis pour library_get.")))
    if inp.op == "publish":
        # `public` ET `unlisted` sont servis SANS LOGIN (l'un est listé dans
        # l'annuaire, l'autre non) : les deux sortent la procédure de l'org.
        _publication.refuser_si_agent(
            ctx, "cette procédure",
            "Elle se publie depuis le dashboard, dans la bibliothèque de guides.")
        return lib._publish(ctx, lib.PublishInput(
            slug=_need(inp.slug, "missing_slug", "`slug` (skill d'org) requis pour publish."),
            public_slug=inp.public_slug, title=inp.title, description=inp.description,
            category=inp.category, tags=inp.tags,
            visibility=_need(inp.visibility, "missing_visibility",
                             "`visibility` requis : 'public' (listé dans la "
                             "bibliothèque publique) ou 'unlisted' (accessible par son "
                             "adresse, sans login, non listé). Le défaut était "
                             "'public' — le plus ouvert des deux.")))
    if inp.op == "fork":
        return lib._fork(ctx, lib.ForkInput(
            slug=_need(inp.slug, "missing_slug", "`slug` (public) requis pour fork."),
            new_slug=inp.new_slug))
    return lib._unpublish(ctx, lib.UnpublishInput(
        id=_need(inp.id, "missing_id", "`id` (entrée bibliothèque) requis pour unpublish.")))


async def _procedure(ctx: ResolvedCtx, inp: ProcedureInput) -> dict:
    from ._execution import execute
    _, result = await execute(_dispatch_procedure, lambda: (ctx, inp))
    return result


CAPABILITIES += [
    Capability(
        key="org.procedure.console", handler=_procedure, Input=ProcedureInput,
        authz=BY_OP({
            # `list` honore `_org=` comme `get` (signal #248 : `set org=Y` répondait
            # ok, puis `list org=Y` rendait toujours le catalogue de l'org MAISON —
            # l'agent croyait sa procédure perdue). Le fix cross-org du 27/07 n'avait
            # posé ORG_MEMBER_OPT que sur `get`, laissant la moitié du signal ouverte.
            "get": _LIRE, "list": _LIRE,
            # `create` partage la garde de `set` : créer n'est pas plus dangereux
            # qu'écrire — c'est le geste qui l'était trop peu, faute de savoir refuser.
            # `describe` partage la garde de `set` : corriger la vitrine est une
            # écriture, réversible de la même façon (la version monte, `from_version`
            # défait). Rien n'y justifierait un palier de droits différent.
            "create": _ECRIRE, "set": _ECRIRE, "describe": _ECRIRE,
            "delete": _SUPPRIMER,
            "library_list": SUB_ONLY, "library_get": SUB_ONLY,
            "publish": ORG_MEMBER, "fork": ORG_MEMBER, "unpublish": SUB_ONLY,
        }),
        description=(
            "Your org's procedures (named guides / skills) + the public library. The base "
            "guide is INJECTED at connect — op=get with `slug` loads ONE skill's full "
            "markdown (`scope=group` targets your active department; `guide_id` loads by "
            "STABLE id, incl. one SHARED to your org; `org` pins the read to an EXPLICIT org "
            "id you are a member of — cross-org load of a named skill by slug) / list (catalog: "
            "slug/title/description, "
            "no body) / create (NEW procedure: `slug` REQUIRED and free — a slug already "
            "taken is REFUSED, `slug_taken`, nothing overwritten. Use this whenever you mean "
            "to add a procedure; `set` on a taken slug silently replaces it) "
            "/ set (write: `slug` is REQUIRED — one named skill. It is an UPSERT: an "
            "existing slug is EDITED (new version, prior one kept in history). Pass "
            "`expected_version` (the version you read) so a procedure someone else changed "
            "meanwhile gives you `version_conflict` instead of losing their edit. "
            "`scope='group'` "
            "writes your TEAM's procedure and only needs you to be a MEMBER of it "
            "(`group` pins an explicit team id): whoever RUNS a procedure may improve it, "
            "and a bad edit is undone with `from_version`. The default `scope='org'` needs "
            "org_admin. ⚠️ The "
            "org README (the prose injected into every session, « socle de l'org ») is NOT a "
            "procedure: write it with `oto_guide(op='write', scope='org', delivery='init')`. "
            "⚠️ EVERY procedure OPENS with `> **Self-improvement digest** — …` as its first block (what the last run taught and what was fixed, dated; one sentence if it has never been run — never invent a run). "
            "⚠️ EVERY procedure must carry a FLOWCHART — one untagged fenced block drawn in "
            "box characters, placed right after the « At a glance » table (or the intro) and "
            "before the first phase heading. It is the DEFAULT view of the process page, and "
            "the grammar is a contract: read the `procedure-flowchart` guide before writing "
            "one. The response carries `diagram_warning` when the body has none. "
            "`from_version` "
            "restores; `org` pins an explicit org id. "
            "`slots` = the entities the prose cites as <slot:name>, and its SHAPE is "
            "`[{name, type}]` — a bare list of names is refused, so is a list of "
            "`{name}` alone. `type` is one of tableau | connecteur | doc. Optional per "
            "entry: `description`; `connector` on a `connecteur` slot ONLY (omit it and "
            "the slot NAME is used as the connector — it is not required, contrary to "
            "what the refusals may suggest); `schema` on a `tableau` slot only, to "
            "prescribe the target table's shape. The refusals name the faulty entry by "
            "index and say what was expected, so read them rather than guessing — but "
            "one procedure still reached version 3 discovering this shape by trial, "
            "which is why it is written here) "
            "/ describe (fix `title` and/or `description` ALONE — the body is carried "
            "over untouched. Use this for a stale catalog line instead of re-sending the "
            "whole body through `set`, which risks degrading prose you did not mean to "
            "edit. Same `scope`/`group`/`org` axes and same permission as `set`; still "
            "versioned, so `from_version` undoes it) "
            "/ delete (exact `slug`; same `scope`/`group`/`org` "
            "axes as set, but DESTRUCTIVE — it takes the whole version history, so "
            "`scope='group'` needs the team LEAD) — and the PUBLIC library: "
            "op=library_list (browse/search, filter category/author_kind) / library_get (full "
            "body by public slug) / publish (share one of your org's skills; visibility="
            "public|unlisted) / fork (copy a public entry into your org, optional `new_slug`) "
            "/ unpublish (`id`)."),
        mcp=orgs_instructions._GUIDE_GET_TOOL,
    ),
]
