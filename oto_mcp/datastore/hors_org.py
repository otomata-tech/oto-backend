"""Un nom de tableau qui ne résout pas dans l'org de l'appel (#631).

Vécu en production le 29/08/2026, dans un même travail : `data_claim_next(<nom>)` ok
à 21:10:05, `data_write(<nom>, id=<ligne>)` refusé « datastore inconnu » à 21:11:23,
`data_write("@claimed")` ok à 21:11:35 (ce pronom a été retiré le 07/09/2026, sans
rien changer au diagnostic ci-dessous) — 103 refus de cette famille sur la soirée,
82 sur sept jours. Les deux appels ok portaient l'axe `_org=` (journal : `org_id` de
l'org du tableau) ; le refusé ne le portait pas (`org_id` = l'org MAISON de l'appelant),
et c'est là que le nom a été cherché. Le tableau n'y est pas ; il n'y a jamais été. Le
refus disait « inconnu » : faux, et sans issue — l'agent qui le lit réessaie ou invente,
il ne pose pas `_org=`. Le journal ne montre pas l'axe (le middleware le retire des
arguments avant le sink) : la colonne `org_id` stampée est la seule preuve.

Deux gestes, sur le seul chemin qui échouait (une résolution qui réussit n'y passe pas) :

- **le run sait où il travaille** (`tenu_par_le_run`) : la réservation active du run
  porte le tableau ET la ligne ; quand le nom demandé est celui d'un tableau réservé, on
  le résout par elle. Le bail LOCALISE, il ne donne aucun droit : `ownership.can_access`
  reste exigé, org-agnostique — un jeton de run n'est pas un axe de droits (#546).
- **sinon le refus le dit** (`indice_autre_org`) : dans quelle org de l'appelant le
  tableau existe, sous quelle org l'appel a été résolu, et l'axe à passer. La face REST
  le disait déjà (`capabilities/datastore/common.ns_not_found`, signal #316) ; la face
  MCP répondait « inconnu » nu — une divergence entre deux faces, pas un manque
  d'information. `ou_existe` est la recherche commune ; chaque face phrase son remède
  (`X-Oto-Org` là, `_org=` ici) parce que le geste diffère vraiment.

On ne nomme que des orgs dont l'appelant est MEMBRE : l'indice ne révèle rien qu'il ne
puisse déjà lister. Et il est fail-open : au moindre pépin, le refus nu d'avant — un
indice ne remplace jamais un refus actionnable par une erreur interne.
"""
from __future__ import annotations

from typing import Optional

from .. import db, org_store, ownership, session_org


def run_courant() -> Optional[str]:
    """Le run de l'appel (explicite, sinon celui de la session, #830), ou None hors
    de tout run / hors contexte de requête."""
    try:
        return session_org.current_call_or_session_run()
    # noqa: SILENT — hors contexte de requête (script, test) : pas de run à lire
    except Exception:  # noqa: BLE001
        return None


def tenu_par_le_run(sub: Optional[str], datastore: str) -> Optional[dict]:
    """La ligne `user_datastores` du tableau que la réservation active du run porte
    sous ce nom (ou cet id) — None si le run ne tient rien de tel.

    Le droit de LIRE ce tableau reste celui du sub, jugé sans contexte d'org : un
    tiers qui connaît le jeton d'un run n'y gagne rien. Le droit d'ÉCRIRE est jugé
    ensuite par l'appelant, comme pour toute résolution."""
    run = run_courant()
    if not run or not sub:
        return None
    vus: set[int] = set()
    for bail in db.datastore_active_leases_of(run_id=run):
        ns_id = int(bail["ns_id"])
        if ns_id in vus:
            continue
        vus.add(ns_id)
        ns = db.get_datastore_by_id(ns_id) or {}
        if datastore not in (ns.get("datastore"), str(ns_id)):
            continue
        if not ownership.can_access(sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id), "read"):
            return None
        return ns
    return None


def ou_existe(sub: str, datastore: str) -> list[tuple[int, Optional[str]]]:
    """`(org_id, nom)` des orgs DU sub qui possèdent le tableau de ce NUMÉRO — la
    recherche des deux faces (REST : `X-Oto-Org` ; MCP : `_org=`). Un tableau ne se
    désigne plus par son nom (08/11/2026) : c'est le numéro qui est cherché."""
    orgs = {int(o["org_id"]): o.get("name") for o in org_store.list_orgs_for_user(sub)}
    owners = [("org", str(i)) for i in orgs]
    return [(int(n["owner_id"]), orgs.get(int(n["owner_id"])))
            for n in db.list_datastores_for_owners(owners)
            if str(n["id"]) == str(datastore).strip()]


def _nom(org_id: int) -> str:
    return str((org_store.get_org(org_id) or {}).get("name") or "sans nom")


def indice_autre_org(sub: Optional[str], datastore: str,
                     org_courante: Optional[int]) -> Optional[str]:
    """La phrase de la face MCP : où le tableau existe, où l'appel a été résolu, quoi
    passer. None quand le nom n'existe dans aucune org de l'appelant — on ne suggère que
    le tableau DEMANDÉ, jamais une org au hasard."""
    if not sub:
        return None
    try:
        ailleurs = [(o, n) for o, n in ou_existe(sub, datastore) if o != org_courante]
        if not ailleurs:
            return None
        ou = ", ".join(f"org {o} \"{n or 'unnamed'}\"" for o, n in ailleurs)
        ici = (f"org {org_courante} \"{_nom(org_courante)}\""
               if org_courante is not None else "your personal space (no org)")
        return (f"it exists in another of your organizations: {ou}. This call was "
                f"resolved in {ici} — pass `_org={ailleurs[0][0]}` on the call.")
    # noqa: SILENT — un indice est un bonus : absent plutôt que faux, jamais une erreur interne à la place du refus
    except Exception:  # noqa: BLE001
        return None
