"""Le résumé quotidien des LECTEURS d'une procédure partagée par lien.

Un email par propriétaire (celui qui a publié le lien) et par passage, seulement s'il a
de nouveaux lecteurs visibles : qui a lu, de quelle entreprise, et qui a copié. Il n'y a
pas de notification dans l'application — ce mail est ce qui ramène le propriétaire vers
l'onglet Readers, et ce qui lui donne une raison de relancer un lecteur.

Un travail de maintenance, pas un événement : `oto-mcp maintenance digest-lecteurs`,
tiré par le timer quotidien (PROD seulement — la base est partagée). Chaque passage
relit l'état : un (partage, lecteur) part UNE fois (`digested_at` posé APRÈS l'envoi ;
un envoi refusé par le mailer reste dû au passage suivant).

⚠️ **Fermé par défaut** (`OTO_DIGEST_LECTEURS`, même dispositif que
`OTO_ALERTE_CREDENTIAL`) : le travail tourne et DIT ce qu'il enverrait, sans rien
envoyer ni marquer, tant que le drapeau n'est pas posé. La décision d'écrire à des
propriétaires est un acte, pas l'effet de bord d'un déploiement.

Écartés par la requête : les partages retirés ou dont « voir qui lit » est coupé, les
lecteurs membres de l'org propriétaire, les propriétaires qui se sont désinscrits
(`process_readers_digest_optouts`, lien `/o/r/<token>`). Écarté ici : un propriétaire qui
n'administre plus l'org de la procédure — il n'a plus accès à l'onglet qu'on lui
montrerait.
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict

log = logging.getLogger("oto_mcp.digest_lecteurs")


def _ouvert() -> bool:
    return os.environ.get("OTO_DIGEST_LECTEURS", "").strip() in ("1", "true", "on")


def _lien_readers(org_id, slug: str, sub: str) -> str | None:
    from . import config, org_store
    base = org_store.org_front(org_id)[0] or config.dashboard_url_for(sub)
    if not base or not slug:
        return None
    return f"{base.rstrip('/')}/org/{org_id}/processes/{slug}/readers"


def balayer(*, dry_run: bool = False) -> dict:
    """Envoie les résumés dus. À blanc si `dry_run` OU si le drapeau est fermé."""
    from . import db, email as mailer, org_store, outreach_optout, roles
    from .capabilities.partages_procedure import FREE_MAIL, domaine
    from .db import partages_procedure as db_partages

    a_blanc = dry_run or not _ouvert()
    lignes = db_partages.a_resumer()
    par_proprio: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in lignes:
        par_proprio.setdefault(r["created_by"], []).append(r)

    envoyes = refuses = ecartes = 0
    for sub, rows in par_proprio.items():
        rows = [r for r in rows if r.get("org_id") and roles.is_org_admin(sub, r["org_id"])]
        if not rows:
            ecartes += 1
            continue
        proprio = db.get_user(sub) or {}
        if not proprio.get("email"):
            ecartes += 1
            continue
        procedures: "OrderedDict[int, dict]" = OrderedDict()
        for r in rows:
            p = procedures.get(r["instruction_id"])
            if p is None:
                instr = org_store.get_instruction_by_id(r["instruction_id"]) or {}
                p = procedures[r["instruction_id"]] = {
                    "title": instr.get("title") or instr.get("slug") or "",
                    "url": _lien_readers(r["org_id"], instr.get("slug") or "", sub),
                    "readers": []}
            dom = domaine(r.get("reader_email"))
            p["readers"].append({
                "name": r.get("reader_name"),
                "company": dom if dom and dom not in FREE_MAIL else None,
                "copied": r.get("copied_at") is not None})
        if a_blanc:
            continue
        _base, marque = org_store.org_front(rows[0]["org_id"])
        ok = mailer.send_process_readers_digest_email(
            proprio["email"], processes=list(procedures.values()), brand=marque or "oto",
            locale=proprio.get("locale"), unsubscribe_url=outreach_optout.lien_lecteurs(sub))
        if ok:
            db_partages.marquer_resumes([(r["share_id"], r["reader_sub"]) for r in rows])
            envoyes += 1
        else:
            refuses += 1
            log.warning("résumé des lecteurs refusé par le mailer — reste dû au passage "
                        "suivant (%d lecteur(s))", len(rows))
    return {"a_blanc": a_blanc, "proprietaires": len(par_proprio), "lecteurs": len(lignes),
            "envoyes": envoyes, "refuses": refuses, "ecartes": ecartes}
