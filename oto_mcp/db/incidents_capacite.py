"""Les INCIDENTS DE CAPACITÉ d'une org — l'état, et ce qui en est parti (19/11/2026).

Une ligne de `org_incidents_capacite` (`db/schema/runs.py`) par incident et par
(org, famille d'abonnement) : des travaux dus qu'aucun forfait ne peut servir
(`runner_jobs.travaux_sans_forfait`). La boucle `alertes_capacite` en est le seul
écrivain ; ce module porte ses écritures, chacune idempotente d'une passe à l'autre
et d'un processus à l'autre (le verrou de boucle, puis l'index partiel
`uq_org_incidents_capacite_vivant` : un seul incident vivant par org et famille).

Le cycle, une passe après l'autre (`constater`) :

1. **vu** — une passe le voit : la ligne naît CANDIDATE (`ouvert_at` NULL), ou se
   rafraîchit (cause, compte, échéance) ;
2. **ouvert** — encore vu `delai_min_s` après sa première passe : `ouvert_at` ;
3. **libre** — une passe ne le voit plus : `libre_at`, l'heure où les agents ont repris.
   Revu ensuite, il continue, comme s'il n'avait pas cédé ;
4. **clos** — toujours libre `delai_cloture_s` plus tard : `clos_at` = `libre_at`, et les
   occurrences périmées pendant l'arrêt sont comptées (`perimees`). Un candidat qui cède
   ainsi n'a jamais été un incident : sa ligne s'efface.

⚠️ « Plus rien n'attend » n'est pas « les agents ont repris » : un travail de webhook
périme à sa fraîcheur, une file se vide à la main, un agent s'éteint — le forfait, lui,
peut être encore au plafond. L'appelant dit lesquels cèdent sans que leur cause ait cédé
(`sans_reprise`) : ils se closent `repris = false`, et aucune ligne de reprise ne part.

⚠️ Pourquoi un délai à la clôture : une passe qui tombe pendant la SONDE d'un forfait
(le travail est pris, sondé, rendu à la file en quelques secondes) ne voit plus rien
attendre, sans que rien soit reparti. Clore sur une seule passe rouvrirait l'incident un
quart d'heure plus tard, avec un second message pour la même panne.

Ce qui est parti s'écrit APRÈS l'envoi (`marquer_*`) : un envoi raté se rejoue au passage
suivant, il ne devient pas un silence (même règle que `alertes_slack.marquer`).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ._conn import _connect

_CHAMPS = ("id, org_id, famille, cause, par_le_pool, vu_at, revu_at, libre_at, ouvert_at, "
           "clos_at, travaux, agents, reprise_at, titulaires, perimees, repris, alerte_at, "
           "alerte_par, titulaires_prevenus, titulaires_at, retour_at")

#: Le délai au-delà duquel une ligne de reprise qui n'est pas partie ne part plus : la
#: dire le lendemain ne dirait plus rien d'utile (même borne que `alertes_slack`).
REPRISE_DITE_SOUS_S = 86400


def _paires(cles: Iterable[tuple[int, str]]) -> tuple[list[int], list[str]]:
    cles = list(cles)
    return [int(o) for o, _f in cles], [str(f) for _o, f in cles]


#: « (org_id, famille) de `i` est dans les paires passées » — deux tableaux parallèles.
_DANS = ("EXISTS (SELECT 1 FROM unnest(%s::bigint[], %s::text[]) AS v(o, f) "
         "WHERE v.o = i.org_id AND v.f = i.famille)")


def constater(vus: Iterable[dict], *, delai_min_s: int, delai_cloture_s: int,
              tenus: Iterable[tuple[int, str]] = (),
              sans_reprise: Iterable[tuple[int, str]] = ()) -> dict[str, int]:
    """Une passe : `vus` = les incidents que la passe voit, un par (org, famille) —
    `{org_id, famille, cause, par_le_pool, travaux, agents, reprise_at, titulaires}`.
    Ceux qui ne sont pas vus cèdent, puis se closent (voir l'en-tête).

    `tenus` : les (org, famille) dont la passe ne peut RIEN conclure (une org
    suspendue : ses travaux ne sont pas lus) — leur incident reste tel quel, ni libre,
    ni clos, ni effacé. `sans_reprise` : ceux dont plus rien n'attend mais dont la
    cause tient encore — clos `repris = false`. Rend les comptes de la passe : `vus`,
    `ouverts`, `clos`, `effaces`. Une seule transaction."""
    vus = list(vus)
    orgs, familles = _paires((v["org_id"], v["famille"]) for v in vus)
    t_orgs, t_familles = _paires(tenus)
    s_orgs, s_familles = _paires(sans_reprise)
    ouverts = 0
    with _connect() as conn:
        for v in vus:
            r = conn.execute(
                """
                INSERT INTO org_incidents_capacite
                       (org_id, famille, cause, par_le_pool, travaux, agents, reprise_at,
                        titulaires)
                VALUES (%(org)s, %(famille)s, %(cause)s, %(pool)s, %(travaux)s,
                        %(agents)s, %(reprise)s, %(titulaires)s)
                ON CONFLICT (org_id, famille) WHERE clos_at IS NULL DO UPDATE
                   SET cause = EXCLUDED.cause, par_le_pool = EXCLUDED.par_le_pool,
                       travaux = EXCLUDED.travaux, agents = EXCLUDED.agents,
                       reprise_at = EXCLUDED.reprise_at,
                       titulaires = EXCLUDED.titulaires, revu_at = NOW(),
                       libre_at = NULL,
                       ouvert_at = COALESCE(
                           org_incidents_capacite.ouvert_at,
                           CASE WHEN org_incidents_capacite.vu_at
                                     <= NOW() - make_interval(secs => %(delai)s)
                                THEN NOW() END)
                RETURNING ouvert_at IS NOT NULL AND ouvert_at = NOW() AS ouvert_ici
                """,
                {"org": int(v["org_id"]), "famille": v["famille"], "cause": v["cause"],
                 "pool": bool(v.get("par_le_pool")), "travaux": int(v.get("travaux") or 0),
                 "agents": int(v.get("agents") or 0), "reprise": v.get("reprise_at"),
                 "titulaires": list(v.get("titulaires") or ()),
                 "delai": int(delai_min_s)}).fetchone()
            ouverts += bool(r and r["ouvert_ici"])
        conn.execute(
            f"""
            UPDATE org_incidents_capacite i SET libre_at = NOW()
             WHERE i.clos_at IS NULL AND i.libre_at IS NULL
               AND NOT {_DANS} AND NOT {_DANS}
            """,
            (orgs, familles, t_orgs, t_familles))
        effaces = conn.execute(
            f"DELETE FROM org_incidents_capacite i WHERE i.clos_at IS NULL "
            f"AND i.ouvert_at IS NULL "
            f"AND i.libre_at <= NOW() - make_interval(secs => %s) AND NOT {_DANS}",
            (int(delai_cloture_s), t_orgs, t_familles)).rowcount or 0
        clos = conn.execute(
            f"""
            UPDATE org_incidents_capacite i
               SET clos_at = i.libre_at, repris = NOT {_DANS},
                   perimees = (SELECT COUNT(*) FROM runner_jobs j
                                WHERE j.org_id = i.org_id AND j.status = 'expired'
                                  AND j.payload->>'model_family' = i.famille
                                  AND j.finished_at >= i.vu_at
                                  AND j.finished_at <= i.libre_at)
             WHERE i.clos_at IS NULL AND i.ouvert_at IS NOT NULL
               AND i.libre_at <= NOW() - make_interval(secs => %s) AND NOT {_DANS}
            """,
            (s_orgs, s_familles, int(delai_cloture_s), t_orgs, t_familles)).rowcount or 0
    return {"vus": len(vus), "ouverts": ouverts, "clos": clos, "effaces": effaces}


def _lire(where: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_CHAMPS} FROM org_incidents_capacite WHERE {where} "
            "ORDER BY org_id, id", params).fetchall()
    return [dict(r) for r in rows]


def vivants() -> list[dict[str, Any]]:
    """Les incidents vivants, candidats compris (pas clos)."""
    return _lire("clos_at IS NULL")


def ouverts() -> list[dict[str, Any]]:
    """Les incidents ouverts (passé le délai, pas clos) et qui tiennent encore (la
    dernière passe les a vus) — ceux qui ont une alerte ou un courriel de titulaire à
    envoyer. Un incident qui cède (`libre_at`) n'annonce plus une panne : il attend sa
    clôture, ou la passe qui le revoit."""
    return _lire("ouvert_at IS NOT NULL AND clos_at IS NULL AND libre_at IS NULL")


def a_dire_la_reprise() -> list[dict[str, Any]]:
    """Les incidents clos parce que la capacité est revenue (`repris`), après une alerte
    partie, dont la ligne de reprise n'est pas partie — depuis moins de
    `REPRISE_DITE_SOUS_S`."""
    return _lire("clos_at IS NOT NULL AND repris AND alerte_at IS NOT NULL "
                 "AND retour_at IS NULL "
                 "AND clos_at > NOW() - make_interval(secs => %s)",
                 (REPRISE_DITE_SOUS_S,))


def vivant(org_id: int, famille: str) -> Optional[dict[str, Any]]:
    """L'incident vivant (candidat ou ouvert) de cette org et famille, ou None."""
    lignes = _lire("org_id = %s AND famille = %s AND clos_at IS NULL",
                   (int(org_id), famille))
    return lignes[0] if lignes else None


def dernier(org_id: int, famille: str) -> Optional[dict[str, Any]]:
    """Le dernier incident de cette org et famille, vivant ou clos, ou None."""
    with _connect() as conn:
        r = conn.execute(
            f"SELECT {_CHAMPS} FROM org_incidents_capacite "
            "WHERE org_id = %s AND famille = %s ORDER BY id DESC LIMIT 1",
            (int(org_id), famille)).fetchone()
    return dict(r) if r else None


def marquer_alerte(incident_id: int, par: str) -> None:
    """L'alerte est partie, par `slack` ou `courriel` — APRÈS l'envoi."""
    with _connect() as conn:
        conn.execute("UPDATE org_incidents_capacite SET alerte_at = NOW(), alerte_par = %s "
                     "WHERE id = %s AND alerte_at IS NULL", (par, int(incident_id)))


def marquer_titulaire(incident_id: int, sub: str) -> None:
    """Le courriel de ce titulaire est parti — APRÈS l'envoi, titulaire par titulaire :
    un envoi raté chez l'un ne fait pas réécrire aux autres."""
    with _connect() as conn:
        conn.execute(
            "UPDATE org_incidents_capacite "
            "SET titulaires_prevenus = array_append(titulaires_prevenus, %s), "
            "    titulaires_at = NOW() "
            "WHERE id = %s AND NOT (%s = ANY(titulaires_prevenus))",
            (sub, int(incident_id), sub))


def marquer_reprise(incident_id: int) -> None:
    """La ligne de reprise est partie — APRÈS l'envoi."""
    with _connect() as conn:
        conn.execute("UPDATE org_incidents_capacite SET retour_at = NOW() "
                     "WHERE id = %s AND retour_at IS NULL", (int(incident_id),))
