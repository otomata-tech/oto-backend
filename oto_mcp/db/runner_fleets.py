"""Les flottes du runner — la configuration déclarée d'un passage (chantier R4).

Le module stocke la CONFIG et rend l'ÉTAT ; il ne lance rien et ne juge rien.
L'état est un agrégat sur `runner_jobs` rattachés par `fleet_id` : c'est ce qui
rend un passage lisible d'un bout à l'autre sans corréler des horodatages à la
main, et sans qu'une session ait à pousser des messages à une autre.

⚠️ **Un zéro se distingue d'un « personne n'a regardé ».** Un passage sans aucun
travail rattaché ne rend pas des compteurs à zéro — il rend `jobs_total: 0`, et
l'appelant sait que le vide est constaté et non déduit. Un zéro qui peut vouloir
dire « rien trouvé » ou « rien de mesurable » est le défaut le plus coûteux qu'on
ait payé sur ce chantier.

⚠️ **Tout compteur servi est CASTÉ en entier.** Une agrégation PostgreSQL rend
volontiers un `numeric` là où on attend un entier (`SUM` sur un bigint), donc un
`Decimal` que JSON refuse — et la réponse entière part en 500. Le défaut ne se
voit pas en lisant un modèle : il faut sérialiser une vraie réponse
(`tests/api/test_runner_fleets_rest.py`).

⚠️ **Le coût est rendu en JETONS, jamais en monnaie.** Les tarifs changent, ils
diffèrent par fournisseur, et une valeur monétaire figée en base devient fausse
sans que rien ne le dise. Ce qui est mesuré ici est ce que le worker a déclaré ;
la conversion appartient à qui lit, avec un tarif daté.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ._conn import _connect

_COLS = ("id, org_id, sub, label, procedure, project_id, tools, input, max_steps, "
         "namespace, row_filter, provider, model, workers, rows_at_launch, max_rows, "
         "max_tokens, max_consecutive_failures, max_tokens_per_row, status, stop_reason, "
         "armed_at, started_at, stopping_at, heartbeat_at, stopped_at, created_at")

# Ce qu'un passage a le droit de changer une fois déclaré. La CIBLE n'en est pas :
# rediriger un passage en vol vers un autre tableau est exactement le geste que la
# configuration déclarée existe pour empêcher — on en déclare un autre.
# ⚠️ `provider`/`model` n'en sont PAS, pour la raison exacte qui gèle la cible :
# changer le modèle en vol rend FAUSSE l'attribution des lignes déjà écrites sous
# le passage. Le contexte d'exécution est aussi peu mutable que ce qu'il vise.
# `status` non plus : il se change par les gestes d'état, jamais par une retouche
# de configuration — un `update` qui l'accepterait rendrait 200 sans rien faire.
CHAMPS_MODIFIABLES = ("label", "tools", "input", "max_steps", "workers", "max_rows",
                      "max_tokens", "max_consecutive_failures", "max_tokens_per_row")


def create_fleet(org_id: int, sub: str, *, label: str, procedure: str,
                 tools: list, namespace: Optional[str] = None,
                 row_filter: Optional[dict] = None, project_id: Optional[int] = None,
                 input: Optional[str] = None, max_steps: Optional[int] = None,
                 provider: Optional[str] = None, model: Optional[str] = None,
                 workers: int = 1, max_rows: Optional[int] = None,
                 max_tokens: Optional[int] = None,
                 max_consecutive_failures: Optional[int] = None,
                 max_tokens_per_row: Optional[int] = None) -> dict:
    with _connect() as conn:
        row = conn.execute(
            f"""
            INSERT INTO runner_fleets
                   (org_id, sub, label, procedure, project_id, tools, input,
                    max_steps, namespace, row_filter, provider, model, workers,
                    max_rows, max_tokens, max_consecutive_failures,
                    max_tokens_per_row)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s,
                    %s, %s, %s, %s, %s)
            RETURNING {_COLS}
            """,
            (org_id, sub, label, procedure, project_id,
             json.dumps(list(tools), ensure_ascii=False), input, max_steps,
             namespace,
             json.dumps(row_filter, ensure_ascii=False) if row_filter is not None else None,
             provider, model, workers, max_rows, max_tokens,
             max_consecutive_failures, max_tokens_per_row),
        ).fetchone()
    return dict(row)


def list_fleets(org_id: int, statut: Optional[str] = None) -> list[dict]:
    ou, args = "WHERE org_id = %s", [org_id]
    if statut:
        ou += " AND status = %s"
        args.append(statut)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM runner_fleets {ou} ORDER BY id DESC", tuple(args),
        ).fetchall()
    return [dict(r) for r in rows]


def get_fleet(fleet_id: int, org_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM runner_fleets WHERE id = %s AND org_id = %s",
            (fleet_id, org_id),
        ).fetchone()
    return dict(row) if row else None


def update_fleet(fleet_id: int, org_id: int, champs: dict[str, Any]) -> Optional[dict]:
    champs = {c: v for c, v in champs.items() if c in CHAMPS_MODIFIABLES}
    if not champs:
        return get_fleet(fleet_id, org_id)
    sets, args = [], []
    for c, v in champs.items():
        if c == "tools":
            sets.append(f"{c} = %s::jsonb")
            args.append(json.dumps(list(v), ensure_ascii=False))
        else:
            sets.append(f"{c} = %s")
            args.append(v)
    args += [fleet_id, org_id]
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE runner_fleets SET {', '.join(sets)} "
            f"WHERE id = %s AND org_id = %s RETURNING {_COLS}",
            tuple(args),
        ).fetchone()
    return dict(row) if row else None


def set_status(fleet_id: int, org_id: int, statut: str,
               raison: Optional[str] = None) -> Optional[dict]:
    """Change l'état, et ÉCRIT la raison quand le passage s'arrête.

    ⚠️ `stop_reason` n'est jamais déduit d'un statut : « arrêtée » sans raison
    oblige l'opérateur à rouvrir les journaux pour savoir si le budget a coupé, si
    la file s'est vidée, ou si un outil est tombé.
    """
    horodatage = {"running": "started_at = NOW(), heartbeat_at = NOW()",
                  "stopped": "stopped_at = NOW()",
                  "done": "stopped_at = NOW()",
                  "failed": "stopped_at = NOW()"}.get(statut)
    sets = ["status = %s", "stop_reason = %s"]
    args: list[Any] = [statut, raison]
    if horodatage:
        sets.append(horodatage)
    args += [fleet_id, org_id]
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE runner_fleets SET {', '.join(sets)} "
            f"WHERE id = %s AND org_id = %s RETURNING {_COLS}",
            tuple(args),
        ).fetchone()
    return dict(row) if row else None


# ── Les gestes d'ÉTAT, nommés — et la transition qu'ils exigent ──────────────
# ⚠️ Chacun est CONDITIONNEL sur l'état de départ, dans le même UPDATE. Sans ça,
# deux appels concurrents (un opérateur et un ordonnanceur) écriraient l'un sur
# l'autre, et le dernier gagnerait — y compris pour ressusciter un passage arrêté.
# Rendre `False` quand la transition n'était pas permise laisse l'appelant DIRE
# qu'il n'a rien changé, au lieu de croire qu'il a agi.

def armer(fleet_id: int, org_id: int,
          rows_at_launch: Optional[int] = None) -> Optional[dict]:
    """`draft`/`stopped`/`done`/`failed` → `armed` : on DEMANDE que ça tourne.

    ⚠️ Ce n'est PAS `running`. Une intention déclarée et un fait constaté ne
    partagent jamais une colonne : `running` veut dire qu'un ordonnanceur l'a
    PRISE et donne signe. Une flotte armée que personne n'a réclamée doit se lire
    « armée, personne ne l'a prise » — pas « en cours ».

    `rows_at_launch` est le compte des lignes visées À CET INSTANT (l'appelant le
    lit sur la table ; cf. la capacité). Il est réécrit à chaque armement — un
    passage relancé vise une table qui a bougé — et `None` l'efface plutôt que de
    laisser en place le dénominateur d'un armement précédent, qui serait faux.
    """
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE runner_fleets SET status = 'armed', armed_at = NOW(), "
            f"    rows_at_launch = %s, stop_reason = NULL, stopping_at = NULL "
            f"WHERE id = %s AND org_id = %s "
            f"  AND status IN ('draft', 'stopped', 'done', 'failed') "
            f"RETURNING {_COLS}",
            (None if rows_at_launch is None else int(rows_at_launch), fleet_id, org_id),
        ).fetchone()
    return dict(row) if row else None


def prendre(fleet_id: int, org_id: int) -> Optional[dict]:
    """`armed` → `running` : un ordonnanceur l'a prise. C'est le FAIT."""
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE runner_fleets SET status = 'running', started_at = NOW(), "
            f"    heartbeat_at = NOW() "
            f"WHERE id = %s AND org_id = %s AND status = 'armed' "
            f"RETURNING {_COLS}",
            (fleet_id, org_id),
        ).fetchone()
    return dict(row) if row else None


def demander_arret(fleet_id: int, org_id: int, raison: str) -> Optional[dict]:
    """`armed`/`running` → `stopping` : l'arrêt est DEMANDÉ, pas encore effectif.

    ⚠️ Entre cet appel et la lecture par la boucle, le passage CONTINUE — il
    réserve, il appelle, il dépense. Écrire `stopped` ici annoncerait un arrêt qui
    n'a pas eu lieu, et **croire qu'on a coupé une dépense qui continue est pire
    que croire qu'on a lancé un passage qui ne tourne pas** : dans un cas on
    attend, dans l'autre on part tranquille pendant que ça brûle.
    """
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE runner_fleets SET status = 'stopping', stopping_at = NOW(), "
            f"    stop_reason = %s "
            f"WHERE id = %s AND org_id = %s AND status IN ('armed', 'running') "
            f"RETURNING {_COLS}",
            (raison, fleet_id, org_id),
        ).fetchone()
    return dict(row) if row else None


def accuser_arret(fleet_id: int, org_id: int, raison: Optional[str] = None) -> bool:
    """`stopping`/`running` → `stopped` : l'ordonnanceur a accusé réception.

    ⚠️ C'est LUI qui pose ce statut, jamais l'opérateur — sans quoi l'écart entre
    « demandé » et « effectif » disparaîtrait, et avec lui le seul diagnostic d'un
    ordonnanceur mort : *un arrêt demandé qui ne devient jamais un arrêt effectif*.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE runner_fleets SET status = 'stopped', stopped_at = NOW(), "
            "    stop_reason = COALESCE(%s, stop_reason) "
            "WHERE id = %s AND org_id = %s AND status IN ('stopping', 'running') "
            "RETURNING id",
            (raison, fleet_id, org_id),
        ).fetchone()
    return row is not None


def arret_demande(fleet_id: int, org_id: int) -> bool:
    """L'ordonnanceur demande : « dois-je m'arrêter ? » — une lecture, pas un état
    local. C'est ce qui rend `op=stop` RÉEL au lieu d'être une écriture que
    personne ne lit."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM runner_fleets WHERE id = %s AND org_id = %s",
            (fleet_id, org_id),
        ).fetchone()
    return bool(row) and dict(row)["status"] in ("stopping", "stopped")


def run_appartient_a_flotte(run_id: str, fleet_id: int) -> bool:
    """Ce déroulé tourne-t-il POUR cette flotte ?

    ⚠️ La question n'est pas « ce run existe-t-il » mais « est-ce CELUI qu'on
    voudrait couper ». Un agent doit pouvoir arrêter une AUTRE flotte de son org —
    c'est même le cas utile : un opérateur qui pilote par la conversation. Ce
    qu'on interdit, c'est qu'il coupe celle qui l'exécute.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM runner_jobs WHERE run_id = %s AND fleet_id = %s LIMIT 1",
            (run_id, fleet_id),
        ).fetchone()
    return row is not None


def battre(fleet_id: int, org_id: int) -> bool:
    """Le battement de l'ordonnanceur — ce qui distingue le VIVANT du RÉSIDU.

    Une flotte `running` qui ne bat plus n'est pas une concurrence à attendre :
    c'est un reste de passage mort. Sans cette distinction, un second passage se
    heurte à un refus que rien ne justifie, quelqu'un désarme à la main — et
    désarmer devient le geste normal.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE runner_fleets SET heartbeat_at = NOW() "
            "WHERE id = %s AND org_id = %s AND status = 'running' RETURNING id",
            (fleet_id, org_id),
        ).fetchone()
    return row is not None


def fleet_state(fleet_id: int, org_id: int) -> Optional[dict]:
    """L'ÉTAT d'un passage : sa config, l'avancement de ses travaux, ce qu'il a
    consommé, et ce qui est mort en route.

    Rend `None` si la flotte n'existe pas dans cette org — un état vide et un état
    inexistant ne se ressemblent pas et ne doivent pas se répondre pareil.
    """
    fleet = get_fleet(fleet_id, org_id)
    if not fleet:
        return None
    with _connect() as conn:
        agg = conn.execute(
            """
            SELECT COUNT(*)                                            AS jobs_total,
                   COUNT(*) FILTER (WHERE status = 'pending')          AS pending,
                   COUNT(*) FILTER (WHERE status = 'claimed')          AS claimed,
                   COUNT(*) FILTER (WHERE status = 'done')             AS done,
                   COUNT(*) FILTER (WHERE status = 'failed')           AS failed,
                   COUNT(*) FILTER (WHERE status = 'failed'
                                      AND attempts >= max_attempts)    AS abandoned,
                   -- ⚠️ `SUM` sur un bigint rend un NUMERIC en PostgreSQL, donc un
                   -- Decimal côté client — que rien ne normalise et que JSON refuse.
                   -- Sans ce cast, `state` rendait 500 sur TOUTE flotte, y compris
                   -- vierge (COALESCE rend `Decimal('0')`).
                   -- Celui sur `MAX` est une SYMÉTRIE DÉFENSIVE, pas une garde :
                   -- le cast interne type déjà la valeur, donc aucun test ne peut
                   -- le faire tomber. Il est là pour qu'un futur passage de MAX à
                   -- SUM ne réintroduise pas la panne — et il est nommé pour ce
                   -- qu'il est, parce qu'une protection qu'on n'a jamais vue mordre
                   -- ne doit pas se faire passer pour une garde éprouvée.
                   COALESCE(SUM((result->>'usage_tokens')::bigint), 0)::bigint
                       AS usage_tokens,
                   MAX((result->>'usage_tokens')::bigint)::bigint
                       AS heaviest_row_tokens,
                   MAX(finished_at)                                    AS last_finished
              FROM runner_jobs
             WHERE fleet_id = %s AND org_id = %s
            """,
            (fleet_id, org_id),
        ).fetchone()
    etat = dict(agg)
    # Un passage sans aucun travail rattaché le DIT, au lieu de rendre des
    # compteurs à zéro qu'on lirait comme « rien ne s'est passé ».
    etat["no_jobs_attached"] = etat["jobs_total"] == 0
    return {"fleet": fleet, "state": etat}
