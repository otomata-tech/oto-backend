#!/usr/bin/env python3
"""Amorce la déclaration des effets d'outil (`oto_mcp/tools/effets.py`) : pour chaque outil
monté (connecteur ou plateforme) qui n'est pas déclaré `LECTURE`, une classe PROPOSÉE par op, et le lot
proposé de chaque op qui écrit ou envoie et prend une liste.

⚠️ Hors du chemin d'exécution, et sa sortie n'est PAS la vérité : elle se relit op par op
avant d'entrer dans `EFFETS`. Elle classe par le verbe (`recipes/effets.py`, élargi
ci-dessous), suit la classe forcée d'`recipes/ecritures.ECRITURES`, et ne sait rien de ce
qu'un verbe cache (un `add` qui lance une séquence, un `set_*` qui remplace) : c'est
l'objet de la relecture.

    # ce qui n'est pas encore déclaré (un connecteur neuf, une op ajoutée)
    PYTHONPATH=. .venv/bin/python scripts/amorcer_effets.py
    # tout le catalogue, déclaré compris
    PYTHONPATH=. .venv/bin/python scripts/amorcer_effets.py --tout

Il lit le catalogue RÉELLEMENT monté (`_build_mcp`), donc les outils du paquet de
connecteurs installé : avec une version plus ancienne que l'épingle, des connecteurs
manquent.
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Optional

from oto_mcp.recipes import ecritures
from oto_mcp.recipes import effets as effets_recette
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import effets
from oto_mcp.tools.lecture import en_lecture

DETRUIRE = effets_recette.DESTRUCTION | {
    "refund", "void", "deactivate", "overwrite", "set", "unpublish", "terminate", "kill",
    "unenroll", "unfollow", "uninvite", "dismiss", "reject", "decline", "close"}
ENVOYER = effets_recette.ENVOI | {
    "fire", "launch", "enroll", "sms", "invite", "follow", "like", "repost",
    "activate", "resume", "sign", "transfer", "pay"}
LIRE = effets_recette.LIRE | {
    "query", "view", "views", "fields", "options", "statuses", "relationships", "threads",
    "thread", "recordings", "recording", "transcript", "transcripts", "verify", "check",
    "balance", "info", "me", "whoami", "reports", "report", "analytics", "metrics",
    "insights", "autocomplete", "suggest", "resolve", "lookup", "poll", "result",
    "results", "usage", "quota", "schema", "metadata", "overview", "summary", "timeline",
    "activity", "entries", "members", "users", "accounts", "export", "batch", "filters",
    "filter", "library", "signals", "contact", "lists", "var", "statutes", "folders",
    "files", "properties", "pipelines", "stages", "owners", "attachments", "comments",
    "messages", "events", "tags", "templates", "teams", "channels", "inbox", "audit"}
#: Les verbes qui écrivent sans envoyer ni détruire : une op qui commence par l'un d'eux
#: n'est jamais proposée en lecture, quel que soit son dernier mot (`create_thread`).
ECRIRE = frozenset({"create", "update", "add", "upsert", "upload", "import", "tag", "mark",
                    "configure", "edit", "write", "save", "move", "rename", "copy",
                    "assign", "enable", "pause", "stop", "complete", "confirm", "draft",
                    "generate", "run", "patch", "put", "insert", "append", "attach",
                    "duplicate", "restore", "submit", "approve", "review", "lend",
                    "accept", "start", "continue", "register", "reveal", "redeem",
                    "enrich", "unarchive", "convert", "link", "label", "subscribe"})
#: Les clés d'argument qui portent un LOT de fiches (proposées ; la relecture tranche).
CLES_DE_LOT = ("ids", "items", "entries", "rows", "records", "contact_ids", "contacts",
               "leads", "lead_ids", "people", "persons", "person_ids", "emails", "to",
               "recipients", "numbers", "phone_numbers", "user_ids", "member_ids",
               "members", "profile_ids", "company_ids", "companies", "organization_ids",
               "organizations", "values", "domains", "invitees", "attendees", "deal_ids",
               "account_ids", "record_ids", "prospects", "subscribers", "profiles", "data")
_ORDRE = {c: i for i, c in enumerate(effets.CLASSES)}


def classer_mot(texte: str, nom_d_outil: bool = False) -> str:
    """La classe proposée d'une op, ou du nom d'un outil sans op : le mot le plus
    contraignant qu'il porte, jamais `lire` sur un mot inconnu."""
    mots = [m for m in re.split(r"[_\W]+", texte.lower()) if m]
    if not mots:
        return "detruire"
    portee = mots if nom_d_outil else mots[:2]
    if mots[0] == "bulk" or any(m in DETRUIRE for m in portee):
        return "detruire"
    if any(m in ENVOYER for m in portee):
        return "envoyer"
    if mots[0] in ECRIRE:
        return "ecrire"
    if mots[0] in LIRE or mots[-1] in LIRE:
        return "lire"
    if nom_d_outil and not any(m in ECRIRE | DETRUIRE | ENVOYER for m in mots):
        # Un outil sans op nommé d'un NOM (`foncier_dpe`) : proposé en lecture, à relire.
        return "lire"
    return "ecrire"


def proposer(outil: str, op: Optional[str]) -> str:
    forcee = ecritures.ECRITURES.get(outil)
    if forcee is not None and op is not None:
        if op in (forcee.lire or ()):
            return "lire"
        if op in (forcee.ecrire or ()):
            return "envoyer" if forcee.envoie else "ecrire"
    if forcee is not None and op is None and forcee.ecrire is None:
        return "envoyer" if forcee.envoie else "ecrire"
    if op is None:
        ns = namespace_of(outil)
        return classer_mot(outil[len(ns) + 1:] if outil.startswith(ns + "_") else outil,
                           nom_d_outil=True)
    return classer_mot(op)


def ops_servies(tool) -> list[str]:
    op = (tool.parameters.get("properties") or {}).get("op") or {}
    if "enum" in op:
        return list(op["enum"])
    if "const" in op:
        return [op["const"]]
    return [v for b in op.get("anyOf", ()) for v in b.get("enum", ())]


def listes(tool) -> list[str]:
    def est_liste(s: dict) -> bool:
        return s.get("type") == "array" or any(est_liste(b) for b in s.get("anyOf", ()))
    return [k for k, s in (tool.parameters.get("properties") or {}).items()
            if k != "op" and est_liste(s)]


def catalogue():
    from oto_mcp.server import _build_mcp
    outils = asyncio.run(_build_mcp("noauth").list_tools(run_middleware=False))
    # Tout outil monté qui n'est pas `LECTURE` : connecteurs ET plateforme.
    return [t for t in outils if not en_lecture(t)]


def main(argv: list[str]) -> int:
    tout = "--tout" in argv
    lignes_effets, lignes_lots, lignes_pas_lot = [], [], []
    for t in sorted(catalogue(), key=lambda t: t.name):
        ops = ops_servies(t)
        if not ops and t.name in effets.EFFETS and not tout:
            continue  # sans op, ou une op libre (`notion_view`) : déjà déclaré
        declarees = effets.classes_par_op(t.name) if t.name in effets.EFFETS else {}
        a_faire = [o for o in (ops or [None]) if tout or o not in declarees]
        if not a_faire:
            continue
        if not ops:
            classe = proposer(t.name, None)
            lignes_effets.append(f'    "{t.name}": "{classe}",')
            par_classe = {classe: [None]}
        else:
            par_classe: dict[str, list] = {}
            for o in a_faire:
                par_classe.setdefault(proposer(t.name, o), []).append(o)
            corps = ", ".join(f'"{c}": "{" ".join(par_classe[c])}"'
                              for c in sorted(par_classe, key=_ORDRE.get))
            lignes_effets.append(f'    "{t.name}": {{{corps}}},')
        lst = listes(t)
        if not lst:
            continue
        lots = [k for k in lst if k in CLES_DE_LOT]
        autres = [k for k in lst if k not in lots]
        ecrivent = par_classe.get("ecrire", []) + par_classe.get("envoyer", [])
        if lots:
            for o in ecrivent:
                cle = f"{t.name}:{o}" if o else t.name
                lignes_lots.append(f'    "{cle}": Lot("{lots[0]}"),')
        if autres and ecrivent:
            lignes_pas_lot.append(f'    "{t.name}": "{" ".join(autres)}",')
    print("EFFETS = {\n" + "\n".join(lignes_effets) + "\n}")
    print("LOTS = {\n" + "\n".join(lignes_lots) + "\n}")
    print("PAS_UN_LOT = {\n" + "\n".join(lignes_pas_lot) + "\n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
