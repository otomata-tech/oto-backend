"""Le travail de fond qui fait avancer les effacements : `oto-mcp maintenance effacements`.

Il tourne HORS du processus MCP (timer de la box, prod seulement) : une purge efface
par lots des tables de journal, réécrit des archives de plusieurs centaines de Mo —
rien de cela n'a sa place dans la boucle unique du serveur (`docs/event-loop-perf.md`).

Pour chaque demande échue (`scheduled`, `scheduled_for` passé) ou en cours (`running`),
les étapes s'enchaînent dans cet ordre, chacune notée au reçu (`receipt`) quand elle
est faite — un passage coupé reprend à la première étape non notée :

0. `perimetre` — les partants revus (un compte entré dans une autre org reste) ;
   `repetition` — la purge jouée puis ANNULÉE, avant tout geste irréversible ;
1. `objets`   — les objets du stockage que l'org désigne (lus en base, donc AVANT elle) ;
2. `unipile`  — les sièges de messagerie de l'org sur la clé plateforme ;
3. `logto_org` — l'organisation Logto miroir de l'org ;
4. `comptes`  — le compte de connexion de chaque partant, et ses jetons `oto_` ;
   puis ATTENTE de `ATTENTE_APRES_COMPTES` : un JWT déjà émis vit encore une heure, et
   son premier appel recréerait la ligne `users` qu'on s'apprête à effacer ;
5. `base`     — la purge de la base (`purge.purger`), puis le recomptage : un résidu
   relance une passe, un résidu après deux passes est un échec nommé ;
6. `archives` — les archives froides du journal, réécrites sans l'org ;
7. `done`     — le reçu final, ce qui était personnel retiré de la demande, et un mail
   au titulaire.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from . import archives, demandes, externes, purge
from .perimetre import Instantane, rafraichir

_log = logging.getLogger("oto_mcp.effacement")

ATTENTE_APRES_COMPTES = timedelta(minutes=65)
# L'interrupteur : absent, le travail dit ce qu'il ferait et n'écrit RIEN — même règle
# que les autres travaux qui agissent dehors (`maintenance._ALL`).
ENV_ACTIF = "OTO_EFFACEMENTS"


def actif() -> bool:
    return os.environ.get(ENV_ACTIF, "").strip().lower() in ("1", "true", "yes", "on")


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def passer(*, dry_run: bool = False) -> dict:
    """Un passage : fait échoir les liens, fait avancer chaque demande due."""
    dues = demandes.dues()
    if dry_run or not actif():
        return {"actif": actif(), "a_faire": [d["id"] for d in dues]}
    expirees = demandes.expirer()
    out = {"expirees": expirees, "terminees": 0, "en_attente": 0, "echecs": 0}
    for d in dues:
        if not demandes.demarrer(d["id"]):
            continue
        try:
            fini = avancer(demandes.lire(d["id"]))
        except Exception as e:  # noqa: BLE001 — noté sur la demande, rejoué au passage suivant
            etat = demandes.echec(d["id"], f"{type(e).__name__}: {e}")
            _log.error("effacement %s (org %s) : échec, état %s", d["id"], d["org_id"], etat,
                       exc_info=True)
            out["echecs"] += 1
            continue
        out["terminees" if fini else "en_attente"] += 1
    return out


def avancer(d: dict) -> bool:
    """Fait avancer UNE demande `running`. Rend True quand elle est finie."""
    from .. import media_store
    from ..db._conn import _connect_autocommit
    inst = Instantane.depuis_json(d["snapshot"])
    recu = dict(d.get("receipt") or {})

    def noter(etape: str, valeur) -> None:
        recu[etape] = valeur
        demandes.noter(d["id"], {etape: valeur})

    with _connect_autocommit(bornee=False) as conn:
        r = conn.execute("SELECT front_brand FROM orgs WHERE id = %s", (inst.org_id,)).fetchone()
        marque = r["front_brand"] if r else None
        if "perimetre" not in recu:
            # Revu au début de la purge : un partant a pu rejoindre une autre org pendant
            # le délai de grâce — il garde alors son compte.
            inst = rafraichir(conn, inst)
            demandes.maj_snapshot(d["id"], inst.vers_json())
            noter("perimetre", {"partants": len(inst.partants), "restants": len(inst.restants)})
        if "repetition" not in recu:
            # La purge jouée puis annulée, AVANT tout geste irréversible hors base : une
            # clé étrangère qui la ferait échouer à mi-chemin lève ici.
            noter("repetition", purge.repeter(conn, inst))
        if "depuis_mois" not in recu:
            r = conn.execute(
                "SELECT to_char(LEAST(o.created_at, (SELECT min(created_at) FROM users "
                "  WHERE sub = ANY(%s))), 'YYYY-MM') AS m FROM orgs o WHERE o.id = %s",
                (list(inst.partants), inst.org_id)).fetchone()
            noter("depuis_mois", r["m"] if r else "0000-00")
        if "objets" not in recu:
            gardees: set[str] = set()
            cles = purge.cles_d_objet(conn, inst, media_store.public_base(), gardees)
            noter("objets", {"supprimes": externes.supprimer_objets(cles),
                             "cites_gardes": len(gardees - cles)})
        if "unipile" not in recu:
            noter("unipile", externes.liberer_sieges_unipile(inst.org_id))
        if "logto_org" not in recu:
            r = conn.execute("SELECT logto_org_id FROM orgs WHERE id = %s",
                             (inst.org_id,)).fetchone()
            noter("logto_org", {"supprimee": externes.supprimer_organisation_logto(
                r["logto_org_id"] if r else None)})
        if "comptes" not in recu:
            conn.execute("DELETE FROM user_api_tokens WHERE sub = ANY(%s)",
                         (list(inst.partants),))
            fait = externes.supprimer_comptes_de_connexion(inst.partants)
            noter("comptes", {**fait, "le": _maintenant().isoformat()})
        if "base" not in recu:
            le = datetime.fromisoformat(recu["comptes"]["le"])
            if inst.partants and _maintenant() < le + ATTENTE_APRES_COMPTES:
                return False
            ecrit = purge.purger(conn, inst)
            reste = purge.residu(conn, inst)
            if reste:
                # Un appel arrivé pendant la passe (journal) : une seconde passe le prend.
                for t, n in purge.purger(conn, inst).items():
                    ecrit[t] = ecrit.get(t, 0) + n
                reste = purge.residu(conn, inst)
            if reste:
                raise RuntimeError(f"résidu après deux passes : {reste}")
            noter("base", {"lignes": ecrit, "total": sum(ecrit.values())})
        if "archives" not in recu:
            s3, seau = media_store.client_et_seau()
            noter("archives", archives.reecrire(
                conn, s3, seau, id_effacement=d["id"], org_id=inst.org_id,
                partants=inst.partants, depuis_mois=recu["depuis_mois"]))
    _prevenir(d, marque)
    demandes.terminer(d["id"], {"termine_le": _maintenant().isoformat()})
    _log.warning("effacement %s : org %s effacée (%s ligne(s))", d["id"], inst.org_id,
                 recu["base"]["total"])
    return True


def _prevenir(d: dict, marque: str | None) -> None:
    """Le mail « c'est fait » au titulaire, tant qu'on a encore son adresse."""
    if not d.get("confirm_email"):
        return
    from .courriels import send_org_erasure_done_email
    try:
        send_org_erasure_done_email(d["confirm_email"], org_name=d.get("org_name"),
                                    brand=marque)
    except Exception:  # noqa: BLE001 — l'effacement est fait ; le mail manqué se journalise
        _log.warning("effacement %s : mail de fin non envoyé", d["id"], exc_info=True)
