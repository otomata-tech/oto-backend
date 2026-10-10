"""Les totaux du journal d'appels par jour UTC (oto-backend#1147) : consolider un jour,
et lire une fenêtre sur les jours consolidés plus le journal direct (`source`).

Le journal (`tool_calls`, ~12 M lignes) est la source de vérité des exécutions ; les
écrans de consommation et de monitoring n'ont pas à le relire en entier à chaque vue
d'une fenêtre de 30 ou 90 jours. Ils lisent ici les TOTAUX des jours clos, et le journal
direct pour ce qui n'est pas consolidé (le jour courant, la veille avant le passage de
la maintenance, le morceau d'un jour qu'une fenêtre glissante coupe).

**Un jour consolidé est COMPLET.** `consolider_jour` recalcule un jour clos en UNE
transaction : il retire le jour du registre (les totaux et les jobs partent avec lui,
`ON DELETE CASCADE`), le réinscrit, pose ses totaux et ses jobs. Rejouer un jour ne
change rien (idempotent) ; une lecture concurrente voit l'ancien jour entier ou le neuf
entier, jamais un mélange. Le jour courant est REFUSÉ : il n'est pas clos.

**Les dimensions, et pas plus** : celles que lisent les écrans (`DIMENSIONS`). Les
mesures s'additionnent d'un jour à l'autre (`MESURES`), sauf deux familles, servies
autrement et EXACTEMENT :

- les **distincts** (comptes actifs, membres) : `sub` et `org_id` sont des dimensions,
  un `count(DISTINCT sub)` sur l'union des jours est donc exact ;
- les **jobs distincts** du relevé facturable : une table de CLÉS par jour
  (`journal_jobs_jour`), comptées distinctes sur l'union ;
- les **percentiles** (p95 des durées et des tailles) : les valeurs elles-mêmes, en
  tableau (`durees`, `tailles`, 4 octets par valeur) — `percentile_cont` sur leur union
  rend la même valeur qu'au journal, là où un histogramme à seaux l'aurait approchée.

Natures agrégées : `mcp` (les appels d'outils) et `connector` (les échecs de
résolution de connecteur) — `KINDS`. Le REST, le protocole et le transport restent au
journal : leurs lecteurs sont des vues de la plateforme, appelées à partir vers Grafana
(épic #1147), et le REST est le flux le plus volumineux.

**Mémoire bornée.** Un jour chargé fait ~270 000 lignes : l'agrégation passe par un TRI
(`enable_hashagg = off`, local à la transaction), qui déborde sur disque au-delà de
`work_mem`, plutôt que par une table de hachage qui tiendrait un tableau par groupe en
mémoire — la base est une nano de 4 Go, partagée, qui a déjà manqué de mémoire (04/10).
"""
from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

from . import journal_calls
from ._conn import _connect

logger = logging.getLogger(__name__)

#: Les natures d'événement agrégées. Le reste du journal ne l'est pas (cf. docstring).
KINDS: tuple[str, ...] = ("mcp", "connector")

#: Les DIMENSIONS d'une ligne de totaux, et leur expression sur une ligne du journal
#: (alias `l`). Ordre = ordre des colonnes de `journal_totaux_jour`.
DIMENSIONS: dict[str, str] = {
    "jour": "(l.created_at AT TIME ZONE 'UTC')::date",
    "kind": "l.kind",
    "org_id": "l.org_id",
    "sub": "l.sub",
    "tool": "l.tool",
    "ok": "l.ok",
    "key_mode": "l.key_mode",
    # L'émetteur DÉCLARÉ (oto#187), tel que `tool_call_stats` le ventile.
    "client_name": f"l.args->'{journal_calls.ARGS_CLIENT_KEY}'->>'name'",
}

#: Les MESURES d'une ligne de totaux : leur agrégat sur les lignes du journal d'un même
#: groupe de dimensions. Les sommes ignorent NULL comme `SUM`/`AVG` ; `quantite` lit une
#: quantité absente comme 1, jamais 0 (commentaire DDL de `tool_calls.quantity`).
MESURES: dict[str, str] = {
    "appels": "count(*)::int",
    "quantite": "sum(COALESCE(l.quantity, 1))::bigint",
    "duree_n": "count(l.duration_ms)::int",
    "duree_somme": "COALESCE(sum(l.duration_ms), 0)::bigint",
    "durees": ("COALESCE(array_agg(l.duration_ms) FILTER "
               "(WHERE l.duration_ms IS NOT NULL), '{}')"),
    "taille_n": "count(l.result_size)::int",
    "taille_somme": "COALESCE(sum(l.result_size), 0)::bigint",
    "tailles": ("COALESCE(array_agg(l.result_size) FILTER "
                "(WHERE l.result_size IS NOT NULL), '{}')"),
    "dernier_at": "max(l.created_at)",
}

#: La borne d'un jour UTC `%s` (une date), en instant : son premier instant.
DEBUT_DU_JOUR = "(%s::date::timestamp AT TIME ZONE 'UTC')"

#: Durée maximale de la consolidation d'UN jour, par défaut (maintenance). Mesuré à
#: quelques secondes pour un jour chargé ; la borne coupe un jour pathologique sans
#: tenir la base, et le jour reste NON consolidé (la transaction est annulée).
DUREE_MAX_MS = 120_000

#: Au plus tant de jours rattrapés par un passage de la maintenance (une nuit manquée,
#: un week-end sans timer). Au-delà, c'est le script de rattrapage, lancé à la main.
MAINTENANCE_JOURS_MAX = 7

#: Verrou consultatif de la consolidation : deux consolidations du même jour (le timer et
#: un rattrapage à la main) se suivent au lieu de se croiser.
_VERROU = "hashtext('oto.journal_jour')"


class JourNonClos(ValueError):
    """Consolider le jour courant (ou un jour futur) : refusé, il n'est pas complet."""


def _projection(predicat: str, *, mesures: tuple[str, ...] = tuple(MESURES)) -> str:
    """Le SELECT qui agrège les lignes du journal satisfaisant `predicat` (alias `l`)
    par jour et par dimensions. UNE définition, partagée par la consolidation et par la
    lecture du journal direct (`source`) : les deux ne peuvent pas diverger."""
    dims = ",\n               ".join(f"{expr} AS {nom}" for nom, expr in DIMENSIONS.items())
    mes = ",\n               ".join(f"{MESURES[nom]} AS {nom}" for nom in mesures)
    rangs = ", ".join(str(i) for i in range(1, len(DIMENSIONS) + 1))
    return (f"SELECT {dims},\n               {mes}\n"
            f"          FROM tool_calls l\n"
            f"         WHERE {predicat}\n"
            f"         GROUP BY {rangs}")


def _expr_job() -> str:
    # Import différé : `usage` lit les totaux (et donc ce module) à son import.
    from .usage import _BILLABLE_JOB_ID_SQL
    return _BILLABLE_JOB_ID_SQL


def predicat_jobs(alias_ok: str = "l") -> str:
    """Ce qui fait d'une ligne du journal un job facturable relevé : la ligne de la
    lentille de facturation (`kind='mcp'`, `ok`, sous une org) qui nomme un job."""
    return (f"{alias_ok}.kind = 'mcp' AND {alias_ok}.ok AND {alias_ok}.org_id IS NOT NULL "
            f"AND {_expr_job()} IS NOT NULL")


def consolider_jour(jour: str, *, duree_max_ms: int = DUREE_MAX_MS) -> dict:
    """(Re)calcule les totaux et les jobs du jour UTC `jour` ('YYYY-MM-DD'), en UNE
    transaction bornée à `duree_max_ms`. Rend `{jour, lignes, totaux, jobs}`.

    Lève `JourNonClos` pour le jour courant ou un jour futur. Une borne dépassée lève
    l'annulation de PostgreSQL (`QueryCanceled`) : rien n'est écrit, le jour reste dans
    son état précédent (consolidé avant, ou absent du registre)."""
    debut = DEBUT_DU_JOUR
    predicat = (f"l.created_at >= {debut} AND l.created_at < "
                f"((%s::date + 1)::timestamp AT TIME ZONE 'UTC') "
                f"AND l.kind = ANY(%s)")
    params = (jour, jour, list(KINDS))
    with _connect() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {int(duree_max_ms)}")
        conn.execute("SET LOCAL enable_hashagg = off")
        conn.execute(f"SELECT pg_advisory_xact_lock({_VERROU})")
        clos = conn.execute(
            "SELECT %s::date < (now() AT TIME ZONE 'UTC')::date AS clos", (jour,)
        ).fetchone()["clos"]
        if not clos:
            raise JourNonClos(f"{jour} : le jour n'est pas clos (UTC), il ne se consolide pas.")
        conn.execute("DELETE FROM journal_jours_consolides WHERE jour = %s", (jour,))
        conn.execute("INSERT INTO journal_jours_consolides (jour, lignes) VALUES (%s, 0)",
                     (jour,))
        colonnes = ", ".join(list(DIMENSIONS) + list(MESURES))
        totaux = conn.execute(
            f"""
            WITH ins AS (
                INSERT INTO journal_totaux_jour ({colonnes})
                {_projection(predicat)}
                RETURNING appels
            )
            SELECT count(*) AS n, COALESCE(sum(appels), 0) AS lignes FROM ins
            """, params).fetchone()
        jobs = conn.execute(
            f"""
            WITH ins AS (
                INSERT INTO journal_jobs_jour (jour, org_id, tool, key_mode, job_id)
                SELECT DISTINCT {DIMENSIONS['jour']}, l.org_id, l.tool, l.key_mode,
                       {_expr_job()}
                  FROM tool_calls l
                 WHERE {predicat} AND {predicat_jobs()}
                RETURNING 1
            )
            SELECT count(*) AS n FROM ins
            """, params).fetchone()
        conn.execute(
            "UPDATE journal_jours_consolides SET lignes = %s, consolide_at = now() "
            "WHERE jour = %s", (int(totaux["lignes"]), jour))
    return {"jour": jour, "lignes": int(totaux["lignes"]), "totaux": int(totaux["n"]),
            "jobs": int(jobs["n"])}


def etat() -> dict:
    """Le registre en bref : `{premier, dernier, jours, hier, trous}` — `premier`/`dernier`
    les jours consolidés extrêmes (`None` si aucun), `jours` leur nombre, `hier` la veille
    UTC, `trous` le nombre de jours absents entre les deux extrêmes (0 attendu)."""
    with _connect() as conn:
        r = conn.execute(
            """
            SELECT min(jour) AS premier, max(jour) AS dernier, count(*) AS jours,
                   ((now() AT TIME ZONE 'UTC')::date - 1) AS hier
              FROM journal_jours_consolides
            """).fetchone()
    jours = int(r["jours"])
    trous = 0
    if jours:
        from datetime import date
        etendue = (date.fromisoformat(str(r["dernier"]))
                   - date.fromisoformat(str(r["premier"]))).days + 1
        trous = etendue - jours
    return {"premier": r["premier"], "dernier": r["dernier"], "jours": jours,
            "hier": r["hier"], "trous": trous}


def jours_a_consolider(*, du: Optional[str] = None, au: Optional[str] = None,
                       refaire: bool = False) -> list[str]:
    """Les jours à consolider sur `[du, au]` (défauts : le premier jour du journal, la
    veille UTC), dans l'ordre qui garde la couverture CONTIGUË : d'abord les jours
    après le dernier consolidé, en avançant ; puis ceux d'avant le premier, en
    reculant. Ainsi un rattrapage interrompu ne laisse jamais de trou que les lecteurs
    refuseraient. `refaire` reprend aussi les jours déjà consolidés (du plus récent au
    plus ancien)."""
    with _connect() as conn:
        r = conn.execute(
            """
            SELECT COALESCE(%s::date, (SELECT (min(created_at) AT TIME ZONE 'UTC')::date
                                         FROM tool_calls)) AS du,
                   LEAST(COALESCE(%s::date, (now() AT TIME ZONE 'UTC')::date - 1),
                         (now() AT TIME ZONE 'UTC')::date - 1) AS au
            """, (du, au)).fetchone()
        if r["du"] is None or str(r["du"]) > str(r["au"]):
            return []
        rows = conn.execute(
            """
            SELECT to_char(g.d, 'YYYY-MM-DD') AS jour,
                   EXISTS (SELECT 1 FROM journal_jours_consolides c
                            WHERE c.jour = g.d::date) AS consolide
              FROM generate_series(%s::date, %s::date, interval '1 day') AS g(d)
             ORDER BY g.d DESC
            """, (r["du"], r["au"])).fetchall()
        bornes = conn.execute(
            "SELECT to_char(min(jour), 'YYYY-MM-DD') AS premier, "
            "to_char(max(jour), 'YYYY-MM-DD') AS dernier FROM journal_jours_consolides"
        ).fetchone()
    if refaire:
        return [x["jour"] for x in rows]
    manquants = [x["jour"] for x in rows if not x["consolide"]]
    if bornes["dernier"] is None:
        return manquants                      # du plus récent au plus ancien
    apres = sorted(j for j in manquants if j > bornes["dernier"])
    avant = sorted((j for j in manquants if j < bornes["premier"]), reverse=True)
    # Un jour manquant ENTRE les deux extrêmes est un trou : on le comble d'abord.
    trous = sorted(j for j in manquants if bornes["premier"] < j < bornes["dernier"])
    return trous + apres + avant


def maintenance(*, dry_run: bool = False) -> dict:
    """Le passage quotidien (`oto-mcp maintenance journal-jour`) : consolide les jours
    clos qui suivent le dernier consolidé, la veille comprise, au plus
    `MAINTENANCE_JOURS_MAX`. Sur un registre VIDE, la veille seule : l'historique est
    l'affaire du rattrapage (`scripts/rattraper_journal_jour.py`), lancé à la main, pas
    d'une maintenance dans la fenêtre d'un timer.

    Un jour qui échoue arrête le passage (le suivant creuserait un trou) et lève : le
    travail est journalisé en échec, les lecteurs refuseront les fenêtres qui le
    couvrent dès qu'il aura deux jours, en nommant le geste qui le rattrape."""
    e = etat()
    if e["dernier"] is None:
        jours = [str(e["hier"])]
    else:
        jours = jours_a_consolider(du=_lendemain(str(e["dernier"])))
    plus = max(0, len(jours) - MAINTENANCE_JOURS_MAX)
    jours = jours[:MAINTENANCE_JOURS_MAX]
    if dry_run:
        return {"a_consolider": jours, "au_dela": plus}
    faits = []
    for jour in jours:
        faits.append(consolider_jour(jour))
    if plus:
        logger.error("journal-jour : %d jour(s) en retard au-delà de %d — lancer "
                     "scripts/rattraper_journal_jour.py", plus, MAINTENANCE_JOURS_MAX)
    return {"consolides": faits, "au_dela": plus}


def _lendemain(jour: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(jour) + timedelta(days=1)).isoformat()


# ── La LECTURE : les jours consolidés, plus le journal direct pour le reste ─────────
#
# Une fenêtre `[s, u]` se découpe en trois morceaux disjoints, sans trou ni double
# compte par construction :
#
#   [s, début(a0))          le journal direct (le morceau d'un jour qu'une fenêtre
#                           glissante coupe, ou rien) ;
#   [début(a0), début(a1+1)) les TOTAUX des jours consolidés a0..a1 — les jours ENTIERS
#                           de la fenêtre que le registre porte ;
#   [début(a1+1), u]        le journal direct (le jour courant, la veille tant que la
#                           maintenance ne l'a pas consolidée, le morceau final).
#
# Les deux morceaux directs passent par la MÊME projection que la consolidation
# (`_projection`) : un jour lu en direct et le même jour consolidé rendent les mêmes
# lignes. Chaque morceau est une plage simple de `created_at` (jamais un `OR`, que
# l'index ne sert pas).
#
# Ce qui est REFUSÉ, nommé (`AgregatIncomplet`) — jamais lu en direct à la place :
#   - un trou : un jour du registre manque entre deux jours consolidés de la fenêtre ;
#   - un retard : un jour entier de la fenêtre, clos depuis plus d'un jour (avant-hier
#     ou plus ancien), après le dernier consolidé — la maintenance n'est pas passée ;
#   - un historique non rattrapé : le journal a des lignes dans un jour entier de la
#     fenêtre ANTÉRIEUR au premier jour consolidé.
# La veille non encore consolidée, elle, est lue en direct : la maintenance passe la nuit.


class AgregatIncomplet(RuntimeError):
    """Une lecture couvre des jours clos que les totaux ne portent pas. Refus NOMMÉ :
    lire ces jours au journal à la place rendrait le chiffre juste en masquant que
    l'alimentation est cassée, et reporterait sur la base le coût que les totaux évitent."""

    def __init__(self, objet: str, motif: str, remede: str) -> None:
        super().__init__(f"{objet} : {motif} — {remede}")
        self.objet = objet
        self.motif = motif


_REMEDE_MAINTENANCE = "la maintenance `journal-jour` doit consolider ces jours"
_REMEDE_RATTRAPAGE = "rattraper l'historique (scripts/rattraper_journal_jour.py)"


class Decoupe(NamedTuple):
    """Le découpage d'une fenêtre : les jours servis par les totaux (`a0..a1`, `None`
    quand aucun) et les bornes de la fenêtre, en SQL (`s_sql`, `u_sql`, `None` = sans
    borne) avec leurs paramètres nommés."""
    a0: Optional[str]
    a1: Optional[str]
    s_sql: Optional[str]
    u_sql: Optional[str]
    haute_incluse: bool
    params: dict


def decouper(conn, objet: str, *, prefixe: str, jours: Optional[int] = None,
             depuis: Any = None, jusqu_a: Any = None,
             haute_incluse: bool = False) -> Decoupe:
    """Découpe la fenêtre d'une lecture, sur la connexion (et donc la transaction, et
    donc le `now()`) de la lecture. La borne basse est `now() - jours` (fenêtre
    glissante, celle des lentilles de monitoring) ou `depuis` ; aucune des deux = sans
    borne. La borne haute est `jusqu_a`, incluse ou non ; absente = sans borne.

    Lève `AgregatIncomplet` (cf. ci-dessus)."""
    if jours is not None and depuis is not None:
        raise ValueError("decouper : `jours` ou `depuis`, pas les deux")
    p = {f"{prefixe}_jours": jours, f"{prefixe}_depuis": depuis,
         f"{prefixe}_jusqu": jusqu_a}
    if jours is not None:
        s_sql = f"(now() - make_interval(days => %({prefixe}_jours)s))"
    elif depuis is not None:
        s_sql = f"%({prefixe}_depuis)s::timestamptz"
    else:
        s_sql = None
    u_sql = f"%({prefixe}_jusqu)s::timestamptz" if jusqu_a is not None else None
    r = conn.execute(
        f"""
        WITH b AS (SELECT {s_sql or 'NULL::timestamptz'} AS s,
                          {u_sql or 'NULL::timestamptz'} AS u,
                          (now() AT TIME ZONE 'UTC')::date AS auj),
             r AS (SELECT min(jour) AS c0, max(jour) AS c1 FROM journal_jours_consolides)
        SELECT to_char(CASE WHEN b.s IS NULL THEN NULL
                            WHEN b.s = ((b.s AT TIME ZONE 'UTC')::date::timestamp
                                        AT TIME ZONE 'UTC')
                            THEN (b.s AT TIME ZONE 'UTC')::date
                            ELSE (b.s AT TIME ZONE 'UTC')::date + 1 END,
                       'YYYY-MM-DD') AS pp,
               to_char(COALESCE((b.u AT TIME ZONE 'UTC')::date, b.auj) - 1,
                       'YYYY-MM-DD') AS dp,
               to_char(COALESCE(r.c0, b.auj - 1), 'YYYY-MM-DD') AS c0,
               to_char(COALESCE(r.c1, b.auj - 2), 'YYYY-MM-DD') AS c1,
               r.c0 IS NULL AS registre_vide,
               to_char(b.auj - 2, 'YYYY-MM-DD') AS clos_depuis
          FROM b, r
        """, p).fetchone()
    pp, dp, c0, c1 = r["pp"], r["dp"], r["c0"], r["c1"]

    # Historique non rattrapé : un jour entier de la fenêtre avant le premier consolidé
    # où le journal a encore des lignes.
    if pp is None or pp < c0:
        avant = conn.execute(
            f"""SELECT EXISTS (SELECT 1 FROM tool_calls
                                WHERE created_at < {DEBUT_DU_JOUR.replace('%s', '%(c0)s')}
                                  {'' if pp is None else
                                   'AND created_at >= ' + DEBUT_DU_JOUR.replace('%s', '%(pp)s')}
                              ) AS x""", {"c0": c0, "pp": pp}).fetchone()["x"]
        if avant:
            raise AgregatIncomplet(
                objet, "le journal a des lignes et aucun jour n'est consolidé"
                if r["registre_vide"] else
                f"le journal a des lignes avant le premier jour consolidé ({c0})",
                _REMEDE_RATTRAPAGE)
    # Retard : les jours entiers de la fenêtre après le dernier consolidé, clos depuis
    # plus d'un jour.
    lo = max(_lendemain(c1), pp) if pp is not None else _lendemain(c1)
    hi = min(dp, r["clos_depuis"])
    if lo <= hi:
        raise AgregatIncomplet(
            objet, f"jours non consolidés du {lo} au {hi}", _REMEDE_MAINTENANCE)

    a0 = max(pp, c0) if pp is not None else c0
    a1 = min(dp, c1)
    if a0 > a1:
        return Decoupe(None, None, s_sql, u_sql, haute_incluse, p)
    trous = [t["jour"] for t in conn.execute(
        """SELECT to_char(g.d, 'YYYY-MM-DD') AS jour
             FROM generate_series(%(a0)s::date, %(a1)s::date, interval '1 day') AS g(d)
            WHERE NOT EXISTS (SELECT 1 FROM journal_jours_consolides c
                               WHERE c.jour = g.d::date)
            ORDER BY g.d LIMIT 5""", {"a0": a0, "a1": a1}).fetchall()]
    if trous:
        raise AgregatIncomplet(objet, "jours absents du registre : " + ", ".join(trous),
                               _REMEDE_RATTRAPAGE)
    p.update({f"{prefixe}_a0": a0, f"{prefixe}_a1": a1})
    return Decoupe(a0, a1, s_sql, u_sql, haute_incluse, p)


def _segments(d: Decoupe, prefixe: str) -> list[str]:
    """Les plages de `created_at` lues au journal direct (alias `l`)."""
    haute = None
    if d.u_sql is not None:
        haute = f"l.created_at {'<=' if d.haute_incluse else '<'} {d.u_sql}"
    basse = f"l.created_at >= {d.s_sql}" if d.s_sql is not None else None
    if d.a0 is None:
        return [" AND ".join(c for c in (basse, haute) if c) or "TRUE"]
    debut_a0 = f"(%({prefixe}_a0)s::date::timestamp AT TIME ZONE 'UTC')"
    fin_a1 = f"((%({prefixe}_a1)s::date + 1)::timestamp AT TIME ZONE 'UTC')"
    avant = " AND ".join(c for c in (basse, f"l.created_at < {debut_a0}") if c)
    apres = " AND ".join(c for c in (f"l.created_at >= {fin_a1}", haute) if c)
    return [avant, apres]


#: Les filtres qu'une lecture pose sur les deux faces (totaux, alias `t`, et journal,
#: alias `l`) — les colonnes portent le même nom des deux côtés. Fermé : une clé
#: inconnue lève.
_FILTRES: dict[str, str] = {
    "org_id": "{a}.org_id = %({p}_org_id)s",
    "sub": "{a}.sub = %({p}_sub)s",
    "subs": "{a}.sub = ANY(%({p}_subs)s)",
    "ok": "{a}.ok = %({p}_ok)s",
    "tools": "{a}.tool = ANY(%({p}_tools)s)",
    "sub_non_nul": "{a}.sub IS NOT NULL",
}


def _clauses(alias: str, prefixe: str, filtres: dict) -> tuple[list[str], dict]:
    clauses, params = [], {}
    for cle, valeur in filtres.items():
        if cle not in _FILTRES:
            raise ValueError(f"filtre de totaux inconnu : {cle!r}")
        if valeur is None or valeur is False and cle == "sub_non_nul":
            continue
        clauses.append(_FILTRES[cle].format(a=alias, p=prefixe))
        if cle != "sub_non_nul":
            params[f"{prefixe}_{cle}"] = valeur
    return clauses, params


def source(conn, objet: str, *, prefixe: str = "jj", kinds: tuple[str, ...] = ("mcp",),
           mesures: tuple[str, ...] = ("appels",), filtres: Optional[dict] = None,
           **fenetre) -> tuple[str, dict]:
    """La SOURCE d'une lecture : une sous-requête qui rend, pour la fenêtre (`fenetre` :
    les arguments de `decouper`) et les filtres, des lignes `jour, kind, org_id, sub,
    tool, ok, key_mode, client_name` + `mesures` — les totaux des jours consolidés et
    les mêmes totaux calculés sur le journal direct pour le reste. Le lecteur agrège
    par-dessus (somme, max, distinct, percentile). Rend `(sql, params)`, paramètres
    NOMMÉS sous `prefixe` (deux sources dans une requête : deux préfixes)."""
    for k in kinds:
        if k not in KINDS:
            raise ValueError(f"nature non agrégée : {k!r} (agrégées : {KINDS})")
    inconnues = set(mesures) - set(MESURES)
    if inconnues:
        raise ValueError(f"mesures inconnues : {sorted(inconnues)}")
    d = decouper(conn, objet, prefixe=prefixe, **fenetre)
    params = dict(d.params)
    params[f"{prefixe}_kinds"] = list(kinds)
    filtres = filtres or {}
    morceaux = []
    if d.a0 is not None:
        cl, pa = _clauses("t", prefixe, filtres)
        params.update(pa)
        where = " AND ".join([f"t.jour BETWEEN %({prefixe}_a0)s::date AND %({prefixe}_a1)s::date",
                              f"t.kind = ANY(%({prefixe}_kinds)s)"] + cl)
        morceaux.append(f"SELECT {', '.join('t.' + c for c in list(DIMENSIONS) + list(mesures))} "
                        f"FROM journal_totaux_jour t WHERE {where}")
    cl, pa = _clauses("l", prefixe, filtres)
    params.update(pa)
    for seg in _segments(d, prefixe):
        predicat = " AND ".join([seg, f"l.kind = ANY(%({prefixe}_kinds)s)"] + cl)
        morceaux.append(_projection(predicat, mesures=mesures))
    return "\nUNION ALL\n".join(f"({m})" for m in morceaux), params


def source_jobs(conn, objet: str, *, prefixe: str = "jb", filtres: dict,
                **fenetre) -> tuple[str, dict]:
    """Les jobs facturables DISTINCTS de la fenêtre, `(tool, key_mode, job_id)` : les
    clés des jours consolidés, plus celles du journal direct pour le reste — en
    `UNION` (distinct), à compter par-dessus. Filtres : `org_id` (requis), `tools`."""
    if set(filtres) - {"org_id", "tools"} or filtres.get("org_id") is None:
        raise ValueError("source_jobs : filtres `org_id` (requis) et `tools` seulement")
    d = decouper(conn, objet, prefixe=prefixe, **fenetre)
    params = dict(d.params)
    morceaux = []
    if d.a0 is not None:
        cl, pa = _clauses("t", prefixe, filtres)
        params.update(pa)
        where = " AND ".join(
            [f"t.jour BETWEEN %({prefixe}_a0)s::date AND %({prefixe}_a1)s::date"] + cl)
        morceaux.append(f"SELECT t.tool, t.key_mode, t.job_id FROM journal_jobs_jour t "
                        f"WHERE {where}")
    cl, pa = _clauses("l", prefixe, filtres)
    params.update(pa)
    for seg in _segments(d, prefixe):
        where = " AND ".join([seg, predicat_jobs()] + cl)
        morceaux.append(f"SELECT l.tool, l.key_mode, {_expr_job()} AS job_id "
                        f"FROM tool_calls l WHERE {where}")
    return "\nUNION\n".join(f"({m})" for m in morceaux), params
