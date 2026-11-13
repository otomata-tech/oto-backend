"""Les ÉPISODES de panne d'un agent hébergé — relevé, ouverture, guérison, réparations.

Un épisode = les travaux morts d'un agent ALLUMÉ depuis son dernier travail réussi, donc
consécutifs — mort et réussi au sens de `issues_des_travaux` (un `done` que l'agent a
conclu `blocked` est mort, 09/11/2026) — la même définition que l'alerte au titulaire
(`db/alertes_agents.py`), mais dès le PREMIER travail mort : soigner commence avant
d'annoncer. La ligne `agent_episodes` est la source unique de la panne : la boucle
`medecin_agents` l'ouvre et la range, l'agent médecin (`oto_admin_agent_repair`) la lit
et la soigne, l'alerte au titulaire la consulte pour se taire quand la panne est la
nôtre.

⚠️ Une réparation s'écrit sur l'épisode (`reparations`, ajout seul) AVANT d'être
considérée comme faite par l'appelant : un travail relancé sans trace serait un second
mécanisme invisible sur le même objet — exactement ce que la décision du bail refuse.
Une personne qui DÉFAIT l'ajout d'un outil (`noter_annulation`) s'y écrit de même, en
ajout : l'historique dit le geste du médecin et celui qui l'a défait.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Optional

from ._conn import _connect
from .alertes_agents import FENETRE_JOURS, MOTIF_TETE, _DU_DECLENCHEUR
from . import issues_des_travaux

#: Les statuts d'un épisode qui n'est pas clos — lus par l'index unique partiel.
VIVANTS = ("ouvert", "en_soin", "escalade")

_COLONNES = ("id, org_id, trigger_id, classe, cote, statut, motif, dernier_job_id, "
             "travaux_morts, reparations, diagnostic, classe_manuelle, ouvert_at, vu_at, "
             "clos_at, resolution, empreinte")


def pannes_en_cours() -> list[dict[str, Any]]:
    """Les agents ALLUMÉS qui ont au moins un travail mort depuis leur dernier succès,
    dans la fenêtre de l'alerte. Une ligne par agent : org, libellé, modèle, le compte,
    le DERNIER travail mort (id, motif, genre, charge, identité) — de quoi le classer et
    le relancer sans relire. `motif_de_l_agent` : le motif est la NOTE que l'agent a
    écrite sur un travail qu'il a conclu bloqué (`issues_des_travaux.motif`), pas une
    erreur de la plateforme — un texte libre, qui ne s'empreinte pas."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            SELECT t.id AS trigger_id, t.org_id, t.model,
                   COALESCE(NULLIF(t.label, ''), t.procedure) AS libelle,
                   f.travaux, d.id AS job_id, LEFT(d.motif, %(tete)s) AS motif,
                   d.kind, d.payload, d.sub, d.finished_at AS dernier, d.motif_de_l_agent
              FROM runner_triggers t
              CROSS JOIN LATERAL (
                    SELECT MAX(j.finished_at) AS dernier_succes
                      FROM runner_jobs j
                     WHERE {_DU_DECLENCHEUR} AND {issues_des_travaux.reussi()}) s
              CROSS JOIN LATERAL (
                    SELECT COUNT(*)::int AS travaux
                      FROM runner_jobs j
                     WHERE {_DU_DECLENCHEUR} AND {issues_des_travaux.mort()}
                       AND j.finished_at > NOW() - make_interval(days => %(fenetre)s)
                       AND (s.dernier_succes IS NULL
                            OR j.finished_at > s.dernier_succes)) f
              CROSS JOIN LATERAL (
                    SELECT j.id, {issues_des_travaux.motif()} AS motif, j.kind, j.payload, j.sub, j.finished_at,
                           NULLIF(j.last_error, '') IS NULL AS motif_de_l_agent
                      FROM runner_jobs j
                     WHERE {_DU_DECLENCHEUR} AND {issues_des_travaux.mort()}
                       AND j.finished_at > NOW() - make_interval(days => %(fenetre)s)
                     ORDER BY j.finished_at DESC LIMIT 1) d
             WHERE t.enabled AND f.travaux >= 1
             ORDER BY t.org_id, t.id
            """,
            {"fenetre": FENETRE_JOURS, "tete": MOTIF_TETE},
        ).fetchall()]


#: Un épisode dont rien n'a changé n'est réécrit qu'une fois par battement : `vu_at`
#: est le POULS que lit le silence du courriel (`agents_soignes_par_nous`, borné par
#: `medecin_agents.FRAICHEUR`, 30 min) ; le battement doit en rester bien en deçà.
BATTEMENT = timedelta(minutes=10)


def ouvrir_ou_rafraichir(*, org_id: int, trigger_id: int, classe: str, cote: str,
                         motif: Optional[str], job_id: Optional[int],
                         travaux: int, empreinte: Optional[str] = None) -> dict[str, Any]:
    """L'épisode vivant de l'agent, ouvert s'il n'existe pas, rafraîchi sinon (dernier
    travail, motif, compte, `vu_at`). La classe suit le DERNIER motif : une panne qui
    change de cause change de remède — sauf une classe posée à la main (`poser`,
    `classe_manuelle`) : un diagnostic d'humain ou de l'agent médecin n'est pas
    défait au passage suivant par la regex qu'il corrigeait. `empreinte` = la clé de la
    cause du dernier motif (`causes_agents`), qui suit le motif. Rend la ligne.

    ⚠️ **Écrire seulement ce qui change** (13/11/2026) : l'épisode vivant n'est réécrit
    que si un champ a changé, ou si son pouls (`vu_at`) a plus d'un `BATTEMENT` ; sinon
    il est relu. Et l'INSERT ne part que s'il n'y a pas d'épisode vivant : l'ancien
    `INSERT … ON CONFLICT DO UPDATE` tirait un numéro de la séquence à CHAQUE passage
    pour CHAQUE agent en panne, et réécrivait la ligne cinq minutes après cinq minutes."""
    p = {"org": int(org_id), "trigger": int(trigger_id), "classe": classe,
         "cote": cote, "motif": motif, "job": job_id, "travaux": int(travaux),
         "empreinte": empreinte, "battement": BATTEMENT}
    with _connect() as conn:
        row = conn.execute(
            f"""
            UPDATE agent_episodes
               SET classe = CASE WHEN classe_manuelle THEN classe ELSE %(classe)s END,
                   cote = CASE WHEN classe_manuelle THEN cote ELSE %(cote)s END,
                   motif = %(motif)s, dernier_job_id = %(job)s,
                   travaux_morts = %(travaux)s, empreinte = %(empreinte)s, vu_at = NOW()
             WHERE trigger_id = %(trigger)s AND statut <> 'clos'
               AND (vu_at < NOW() - %(battement)s
                    OR (NOT classe_manuelle AND (classe IS DISTINCT FROM %(classe)s
                                                 OR cote IS DISTINCT FROM %(cote)s))
                    OR motif IS DISTINCT FROM %(motif)s
                    OR dernier_job_id IS DISTINCT FROM %(job)s::bigint
                    OR travaux_morts IS DISTINCT FROM %(travaux)s
                    OR empreinte IS DISTINCT FROM %(empreinte)s)
            RETURNING {_COLONNES}
            """, p).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_COLONNES} FROM agent_episodes "
                "WHERE trigger_id = %(trigger)s AND statut <> 'clos'", p).fetchone()
        if row is None:
            row = conn.execute(
                f"""
                INSERT INTO agent_episodes (org_id, trigger_id, classe, cote, motif,
                                            dernier_job_id, travaux_morts, empreinte)
                VALUES (%(org)s, %(trigger)s, %(classe)s, %(cote)s, %(motif)s, %(job)s,
                        %(travaux)s, %(empreinte)s)
                ON CONFLICT (trigger_id) WHERE statut <> 'clos' DO NOTHING
                RETURNING {_COLONNES}
                """, p).fetchone()
        if row is None:   # un autre processus l'a ouvert entre-temps : le sien vaut
            row = conn.execute(
                f"SELECT {_COLONNES} FROM agent_episodes "
                "WHERE trigger_id = %(trigger)s AND statut <> 'clos'", p).fetchone()
    return dict(row)


def clore_les_gueris() -> list[dict[str, Any]]:
    """Clôt les épisodes vivants dont l'agent a réussi un travail APRÈS l'ouverture
    (`gueri`), ou qui a été éteint ou supprimé (`agent_eteint`). Rend les épisodes clos —
    ceux qui avaient une réparation disent qu'elle a marché."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""
            UPDATE agent_episodes e
               SET statut = 'clos', clos_at = NOW(),
                   resolution = CASE WHEN t.id IS NULL OR NOT t.enabled
                                     THEN 'agent_eteint' ELSE 'gueri' END
              FROM agent_episodes e2
              LEFT JOIN runner_triggers t ON t.id = e2.trigger_id
             WHERE e.id = e2.id AND e.statut <> 'clos'
               AND (t.id IS NULL OR NOT t.enabled OR EXISTS (
                     SELECT 1 FROM runner_jobs j
                      WHERE j.org_id = t.org_id
                        AND j.payload->>'trigger_id' ~ '^[0-9]+$'
                        AND j.payload->>'trigger_id' = t.id::text
                        AND {issues_des_travaux.reussi()} AND j.finished_at > e.ouvert_at))
            RETURNING {", ".join("e." + c.strip() for c in _COLONNES.split(","))}
            """).fetchall()]


def noter_reparation(episode_id: int, *, action: str, acteur: str,
                     job_id: Optional[int] = None, detail: Optional[str] = None,
                     maximum: Optional[int] = None,
                     outil: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Ajoute un geste à l'épisode et le passe `en_soin` — sous garde : un épisode clos
    ne reçoit rien, `maximum` borne le nombre de gestes de cette `action`, et un même
    `job_id` ne reçoit qu'UN geste de chaque `action` (une relance par travail mort).
    La garde est dans le WHERE de l'UPDATE, qui relit `reparations` : deux processus
    sur le même épisode, le second attend le verrou de ligne, relit la ligne écrite par
    le premier, et refuse. Rend la ligne, ou None si la garde a refusé — l'appelant ne
    fait alors PAS le geste. `outil` = l'outil donné par un `ajout_outil`, rangé à part
    du `detail` (« + <outil> ») : c'est ce qu'une annulation retire."""
    geste = {"action": action, "acteur": acteur, "job_id": job_id,
             "detail": (detail or "")[:500] or None}
    if outil:
        geste["outil"] = outil[:120]
    with _connect() as conn:
        row = conn.execute(
            f"""
            UPDATE agent_episodes
               SET reparations = reparations || jsonb_build_array(
                       %(geste)s::jsonb || jsonb_build_object(
                           'at', to_char(NOW() AT TIME ZONE 'UTC',
                                         'YYYY-MM-DD"T"HH24:MI:SSZ'))),
                   statut = 'en_soin', vu_at = NOW()
             WHERE id = %(id)s AND statut <> 'clos'
               AND (%(max)s::int IS NULL OR (
                     SELECT COUNT(*) FROM jsonb_array_elements(reparations) r
                      WHERE r->>'action' = %(action)s) < %(max)s::int)
               AND (%(job)s::bigint IS NULL OR NOT EXISTS (
                     SELECT 1 FROM jsonb_array_elements(reparations) r
                      WHERE r->>'action' = %(action)s
                        AND r->>'job_id' = (%(job)s::bigint)::text))
            RETURNING {_COLONNES}
            """,
            {"geste": json.dumps(geste, ensure_ascii=False), "id": int(episode_id),
             "max": maximum, "action": action, "job": job_id},
        ).fetchone()
    return dict(row) if row else None


#: Le geste du médecin qui se défait, et celui qui le défait.
AJOUT_OUTIL = "ajout_outil"
ANNULATION = "annulation"


def outils_ajoutes(reparations: Any) -> list[str]:
    """Les outils que les `ajout_outil` de l'épisode ont donnés, dans l'ordre, sans
    doublon : la clé `outil`, sinon — un geste d'avant le 14/11/2026 — le `detail`
    « + <outil> »."""
    out: list[str] = []
    for r in reparations or []:
        if not isinstance(r, dict) or r.get("action") != AJOUT_OUTIL:
            continue
        outil = r.get("outil")
        if not outil:
            detail = str(r.get("detail") or "")
            outil = detail[2:].strip() if detail.startswith("+ ") else None
        if outil and outil not in out:
            out.append(str(outil))
    return out


def outils_a_defaire(reparations: Any) -> list[str]:
    """Les outils donnés par le médecin sur cet épisode qu'aucune annulation n'a encore
    retirés."""
    defaits = {str(o) for r in reparations or [] if isinstance(r, dict)
               and r.get("action") == ANNULATION for o in (r.get("outils") or [])}
    return [o for o in outils_ajoutes(reparations) if o not in defaits]


def noter_annulation(episode_id: int, *, acteur: str,
                     outils: list[str]) -> Optional[dict[str, Any]]:
    """Écrit sur l'épisode qu'une personne a défait l'ajout de ces outils — `{action:
    annulation, acteur, outils, detail: "- <outil>, …", at}`. ⚠️ Pas par
    `noter_reparation` : un épisode guéri est CLOS (c'est lui que l'onglet Santé sert),
    et son statut ne change pas — défaire n'est pas soigner. Sous garde, dans le WHERE :
    aucun de ces outils n'a déjà été défait sur cet épisode (deux clics, un seul geste).
    Rend la ligne, ou None si la garde a refusé."""
    outils = [str(o) for o in outils if o]
    if not outils:
        return None
    geste = {"action": ANNULATION, "acteur": acteur, "outils": outils,
             "detail": ", ".join(f"- {o}" for o in outils)[:500]}
    with _connect() as conn:
        row = conn.execute(
            f"""
            UPDATE agent_episodes
               SET reparations = reparations || jsonb_build_array(
                       %(geste)s::jsonb || jsonb_build_object(
                           'at', to_char(NOW() AT TIME ZONE 'UTC',
                                         'YYYY-MM-DD"T"HH24:MI:SSZ')))
             WHERE id = %(id)s
               AND NOT EXISTS (
                     SELECT 1 FROM jsonb_array_elements(reparations) r,
                                   jsonb_array_elements_text(
                                     COALESCE(r->'outils', '[]'::jsonb)) o
                      WHERE r->>'action' = %(action)s AND o = ANY(%(outils)s))
            RETURNING {_COLONNES}
            """,
            {"geste": json.dumps(geste, ensure_ascii=False), "id": int(episode_id),
             "action": ANNULATION, "outils": outils}).fetchone()
    return dict(row) if row else None


def poser(episode_id: int, *, statut: Optional[str] = None,
          diagnostic: Optional[str] = None, classe: Optional[str] = None,
          cote: Optional[str] = None,
          resolution: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Le geste d'un humain ou de l'agent médecin sur un épisode : son diagnostic, sa
    classe corrigée, l'escalade ou la clôture. Un champ absent ne change pas.

    Une `classe` ou un `cote` posé ici marque l'épisode `classe_manuelle` : le passage
    suivant de la boucle ne le réécrit plus (`ouvrir_ou_rafraichir`)."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            UPDATE agent_episodes
               SET statut = COALESCE(%(statut)s, statut),
                   diagnostic = COALESCE(%(diag)s, diagnostic),
                   classe = COALESCE(%(classe)s, classe),
                   cote = COALESCE(%(cote)s, cote),
                   classe_manuelle = classe_manuelle
                       OR %(classe)s::text IS NOT NULL OR %(cote)s::text IS NOT NULL,
                   resolution = COALESCE(%(resolution)s, resolution),
                   clos_at = CASE WHEN %(statut)s = 'clos' THEN NOW() ELSE clos_at END,
                   vu_at = NOW()
             WHERE id = %(id)s
            RETURNING {_COLONNES}
            """,
            {"statut": statut, "diag": diagnostic, "classe": classe, "cote": cote,
             "resolution": resolution, "id": int(episode_id)},
        ).fetchone()
    return dict(row) if row else None


def lister(*, statut: Optional[str] = None, org_id: Optional[int] = None,
           cote: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Les épisodes, les plus récemment vus d'abord. `statut='vivants'` = tout sauf
    `clos`."""
    filtres, params = [], {"limit": max(1, min(int(limit), 200))}
    if statut == "vivants":
        filtres.append("statut <> 'clos'")
    elif statut:
        filtres.append("statut = %(statut)s")
        params["statut"] = statut
    if org_id is not None:
        filtres.append("org_id = %(org)s")
        params["org"] = int(org_id)
    if cote:
        filtres.append("cote = %(cote)s")
        params["cote"] = cote
    where = f"WHERE {' AND '.join(filtres)}" if filtres else ""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {_COLONNES} FROM agent_episodes {where} "
            "ORDER BY vu_at DESC LIMIT %(limit)s", params).fetchall()]


def lire(episode_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {_COLONNES} FROM agent_episodes WHERE id = %s",
                           (int(episode_id),)).fetchone()
    return dict(row) if row else None


def agents_soignes_par_nous(trigger_ids: list, *, vus_depuis: timedelta) -> set[int]:
    """Parmi ces agents, ceux dont l'épisode vivant est du côté PLATEFORME, tant qu'il n'est pas
    clos — escaladé compris : une panne plateforme qui résiste s'escalade chez NOUS. Leur
    titulaire ne peut rien y faire, l'alerte se tait pour eux.

    ⚠️ Seulement un épisode VU depuis moins de `vus_depuis` (`medecin_agents.FRAICHEUR`,
    quelques passages de la boucle) : un épisode que plus personne ne rafraîchit — boucle
    éteinte, jamais armée, ou arrêtée — ne fait taire personne. Sans cette borne, une
    boucle éteinte après avoir tourné laissait ses agents muets pour toujours."""
    if not trigger_ids:
        return set()
    with _connect() as conn:
        return {int(r["trigger_id"]) for r in conn.execute(
            "SELECT trigger_id FROM agent_episodes "
            "WHERE trigger_id = ANY(%s) AND cote = 'plateforme' "
            "AND statut <> 'clos' AND vu_at > NOW() - %s",
            ([int(i) for i in trigger_ids], vus_depuis)).fetchall()}
