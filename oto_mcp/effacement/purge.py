"""La purge : compter, effacer par lots, vérifier — sur le schéma RÉEL, jamais deviné.

Trois temps, chacun rejouable :

1. `plan` compte, table par table, ce que l'effacement ferait (effacer, anonymiser,
   conserver…). C'est ce que voit le titulaire avant de confirmer, et ce que garde le
   reçu.
2. `purger` efface les ENFANTS avant leurs PARENTS (clés étrangères et liens `Via`
   du classement) : la règle d'un enfant se lit par son parent (`Via`), donc effacer
   le parent d'abord rendrait ses enfants introuvables — des orphelins que personne ne
   reverrait. Chaque lot est sa propre transaction (`LIMIT`, par `ctid`) : une table de
   journal de plusieurs millions de lignes ne tient ni verrou long ni transaction
   géante, et une purge coupée se REPREND en la rejouant (ce qui est effacé ne compte
   plus, ce qui reste est retrouvé par la même règle).
3. `residu` recompte : toute ligne encore visée est un résidu, et un effacement à
   résidu n'est pas fini.

Les règles sont celles de l'export (`export_perimetre.regles`), compilées par le même
mécanisme, à une différence près : un PARTAGE (`destinataire`) part si sa ressource
OU son destinataire est de l'org — l'export, lui, exige les deux.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from ..export_perimetre.classement import CLASSEMENT, JOURNAL, Table
from ..export_perimetre.decouverte import Schema, lire_schema, verifier_classement
from ..export_perimetre.objets import COLONNES_D_URL, COLONNES_DE_CLES, cle_du_chemin, cles_dans
from ..export_perimetre.regles import vias
from . import disposition as dp
from .perimetre import Instantane

_log = logging.getLogger("oto_mcp.effacement")

LOT = 5_000
# Une table de journal se purge par lots ; la pause laisse respirer le WAL et
# l'autovacuum (même raisonnement que l'archivage du journal).
PAUSE_ENTRE_LOTS_S = 0.05


@dataclass(frozen=True)
class Cible:
    table: str
    entree: Table
    decision: dp.Decision


def preparer(conn) -> tuple[Schema, dict[str, Table]]:
    """Le schéma réel et le classement indexé par table physique ; refuse un classement
    ou une disposition incomplets AVANT de toucher quoi que ce soit."""
    schema = lire_schema(conn)
    classement = verifier_classement(schema, CLASSEMENT)
    dp.verifier(classement, schema.colonnes,
                {k.table for k in schema.cles if k.table == k.cible})
    return schema, classement


def cibles(classement: dict[str, Table]) -> dict[str, Cible]:
    return {t: Cible(t, e, dp.decision(t, e)) for t, e in classement.items()}


def compilateur(tout: dict[str, Cible]) -> Callable[[str], str]:
    """`pred(table)` : « cette ligne relève de l'org effacée »."""
    @lru_cache(maxsize=None)
    def pred(table: str) -> str:
        c = tout[table]
        regle = dp.regle_de(c.entree, c.decision)
        if regle is None:
            return "FALSE"
        p = regle.predicat(pred)
        if c.entree.destinataire is not None:
            p = f"(({p}) OR ({c.entree.destinataire.predicat(pred)}))"
        return p
    return pred


def ordre_d_effacement(schema: Schema, tout: dict[str, Cible]) -> list[str]:
    """Les tables qui s'écrivent, chaque ENFANT avant son parent ; la pierre tombale de
    l'org en premier."""
    ecrites = {t for t, c in tout.items() if c.decision.action in dp.ECRIVENT}
    deps = {t: ({k.cible for k in schema.cles_de(t) if k.cible in ecrites}
                | {v.parent for v in vias(dp.regle_de(tout[t].entree, tout[t].decision))
                   if v.parent in ecrites}) - {t}
            for t in ecrites}
    ordre: list[str] = []
    while deps:
        prets = sorted(t for t, d in deps.items() if not d - set(ordre))
        if not prets:
            raise RuntimeError(f"cycle de dépendances entre tables effacées : {sorted(deps)}")
        ordre.extend(prets)
        for t in prets:
            del deps[t]
    ordre.reverse()
    # La pierre tombale EN PREMIER : elle ne supprime rien, elle vide la ligne `orgs`
    # — dont des colonnes désignent des lignes qu'on s'apprête à effacer
    # (`kb_project_id → projects`). Les règles d'appartenance ne lisent pas `orgs`.
    tombes = [t for t in ordre if tout[t].decision.action == dp.PIERRE_TOMBALE]
    return tombes + [t for t in ordre if t not in tombes]


def _encore_nommees(c: Cible) -> str:
    """Pour `anonymiser` : la ligne porte encore une des colonnes à vider."""
    return "(" + " OR ".join(f"{col} IS NOT NULL" for col in c.decision.colonnes) + ")"


def _compte(conn, sql: str, params: dict) -> int:
    return int(conn.execute(sql, params).fetchone()["n"])


def plan(conn, inst: Instantane, *, plafond: int | None = None) -> dict:
    """Ce que l'effacement ferait, par action puis par table (lignes), sans rien écrire.
    `plafond` borne chaque compte (une table de journal de plusieurs millions de lignes
    ne se compte pas sous une requête d'API) : un compte qui l'atteint vaut « au moins »."""
    schema, classement = preparer(conn)
    tout = cibles(classement)
    pred, params = compilateur(tout), inst.parametres()
    out: dict[str, dict[str, int]] = {}
    for t in sorted(tout):
        c = tout[t]
        if dp.regle_de(c.entree, c.decision) is None:
            continue
        cond = pred(t)
        if c.decision.action == dp.ANONYMISER:
            cond = f"({cond}) AND {_encore_nommees(c)}"
        borne = f" LIMIT {int(plafond)}" if plafond else ""
        n = _compte(conn, f"SELECT count(*) AS n FROM (SELECT 1 FROM {t} WHERE {cond}{borne}) x",
                    params)
        if n:
            out.setdefault(c.decision.action, {})[t] = n
    return {"comptes": {"partants": len(inst.partants), "restants": len(inst.restants),
                        "equipes": len(inst.groupes)},
            "plafond": plafond, "tables": out}


def cles_d_objet(conn, inst: Instantane, base_publique: str | None,
                 gardees: set[str] | None = None) -> set[str]:
    """Les objets du stockage que l'org désigne : colonnes de clé, URL de NOTRE stockage
    public dans les colonnes d'URL (avatar d'un compte qui part, logo), et images citées
    dans le texte des lignes effacées.

    ⚠️ Une image citée par URL peut l'être AUSSI par une ligne qui reste (une procédure
    copiée, une entrée de bibliothèque) et le seau n'est pas versionné : seule part une
    image déposée par un compte qui PART (`images/<sub>/…`). Les autres restent — `gardees`
    les reçoit, le reçu les compte."""
    schema, classement = preparer(conn)
    tout = cibles(classement)
    pred, params = compilateur(tout), inst.parametres()
    cles: set[str] = set()
    for nom in sorted(COLONNES_DE_CLES | dp.CLES_D_OBJET_EN_PLUS):
        t, col = nom.split(".")
        if t in tout and col in schema.colonnes.get(t, ()):
            cles |= {r["v"] for r in conn.execute(
                f"SELECT DISTINCT {col} AS v FROM {t} WHERE ({pred(t)}) AND {col} IS NOT NULL",
                params)}
    if base_publique:
        prefixe = base_publique.rstrip("/") + "/"
        a_eux = tuple(f"images/{quote(sub, safe='')}/" for sub in inst.partants)
        # Une image déposée par un agent (`images/<sub>/…`) n'a d'autre trace que son
        # URL collée dans une page, une ligne de tableau, un JSON : comme l'export, on la
        # cherche dans le TEXTE de chaque ligne effacée — sauf le journal, que personne
        # ne cite et qui pèse l'essentiel des lignes.
        for t in sorted(tout):
            if tout[t].decision.action != dp.EFFACER or t in JOURNAL:
                continue
            cur = conn.cursor()
            for r in cur.stream(f"SELECT row_to_json(x)::text AS j FROM {t} x "
                                f"WHERE {pred(t)}", params):
                if prefixe in r["j"]:
                    for cle in cles_dans(r["j"], base_publique):
                        if any(cle.startswith(p) for p in a_eux):
                            cles.add(cle)
                        elif gardees is not None:
                            gardees.add(cle)
        for nom in sorted(COLONNES_D_URL):
            t, col = nom.split(".")
            for r in conn.execute(
                    f"SELECT DISTINCT {col} AS v FROM {t} WHERE ({pred(t)}) "
                    f"AND {col} LIKE %(prefixe)s", {**params, "prefixe": prefixe + "%"}):
                cles.add(cle_du_chemin(r["v"][len(prefixe):]))
    return cles


def _par_lots(conn, sql: str, params: dict, *, arret: Callable[[], bool]) -> int:
    total = 0
    while True:
        n = conn.execute(sql, {**params, "lot": LOT}).rowcount
        total += n
        if n < LOT or arret():
            return total
        time.sleep(PAUSE_ENTRE_LOTS_S)


def _pierre_tombale(conn, schema: Schema, org_id: int) -> int:
    vider = [c for c in schema.colonnes["orgs"]
             if c not in dp.PIERRE_TOMBALE_GARDE and c != "name"
             and c not in schema.generees.get("orgs", frozenset())]
    sets = ", ".join(["name = %(nom)s", "archived_at = COALESCE(archived_at, NOW())",
                      "suspended_at = COALESCE(suspended_at, NOW())"]
                     + [f"{c} = DEFAULT" for c in vider])
    return conn.execute(f"UPDATE orgs SET {sets} WHERE id = %(org)s",
                        {"nom": dp.PIERRE_TOMBALE_NOM, "org": org_id}).rowcount


def _descendants(conn, schema: Schema, t: str, cond: str, params: dict) -> int:
    """Les lignes HORS périmètre qui descendent (clé vers la même table) d'une ligne
    effacée : révoquées ou détachées selon `AUTO_REFERENCES` — jamais supprimées, ce
    sont celles d'un autre."""
    cle = next(k for k in schema.cles_de(t) if k.cible == t)
    col, = cle.colonnes
    enfants = (f"{col} IN (SELECT id FROM {t} WHERE {cond}) AND NOT ({cond})")
    if dp.AUTO_REFERENCES[t] == "detacher":
        return conn.execute(f"UPDATE {t} SET {col} = NULL WHERE {enfants}", params).rowcount
    # `revoquer` : toute la descendance hors périmètre, puis le lien vers le parent effacé.
    ids = [r["id"] for r in conn.execute(
        f"WITH RECURSIVE d AS (SELECT id FROM {t} WHERE {enfants} "
        f"UNION SELECT x.id FROM {t} x JOIN d ON x.{col} = d.id) SELECT id FROM d", params)]
    if not ids:
        return 0
    conn.execute(f"UPDATE {t} SET revoked_at = COALESCE(revoked_at, NOW()) "
                 f"WHERE id = ANY(%s)", (ids,))
    conn.execute(f"UPDATE {t} SET {col} = NULL WHERE {enfants}", params)
    return len(ids)


def _basculer_les_restants(conn, inst: Instantane) -> int:
    """Un membre qui GARDE son compte et avait l'org effacée pour org active bascule sur
    sa plus ancienne org non archivée — le geste d'`org_store.archive_org` : sans lui,
    son appartenance effacée le laisserait sans org active."""
    n = 0
    for r in conn.execute("SELECT sub FROM org_members WHERE org_id = %s AND is_active "
                          "AND sub = ANY(%s)", (inst.org_id, list(inst.restants))).fetchall():
        conn.execute("UPDATE org_members SET is_active = FALSE WHERE sub = %s AND org_id = %s",
                     (r["sub"], inst.org_id))
        n += conn.execute(
            "UPDATE org_members SET is_active = TRUE WHERE sub = %(s)s AND org_id = ("
            "  SELECT m.org_id FROM org_members m JOIN orgs o ON o.id = m.org_id "
            "   WHERE m.sub = %(s)s AND m.org_id <> %(o)s AND o.archived_at IS NULL "
            "   ORDER BY m.joined_at ASC LIMIT 1)", {"s": r["sub"], "o": inst.org_id}).rowcount
    return n


def purger(conn, inst: Instantane, *, arret: Callable[[], bool] = lambda: False,
           sauf: frozenset[str] = frozenset()) -> dict:
    """Efface, anonymise, pose la pierre tombale. `conn` est en AUTOCOMMIT : chaque lot
    est une transaction. Rend les lignes écrites par table. `arret()` vrai interrompt
    proprement entre deux lots (la purge se reprend en la rejouant). `sauf` : tables
    laissées de côté (la répétition saute le journal)."""
    schema, classement = preparer(conn)
    tout = cibles(classement)
    pred, params = compilateur(tout), inst.parametres()
    fait: dict[str, int] = {}
    bascules = _basculer_les_restants(conn, inst)
    if bascules:
        fait["org_active_basculee"] = bascules
    for t in ordre_d_effacement(schema, tout):
        c = tout[t]
        if arret():
            break
        if t in sauf:
            continue
        if c.decision.action == dp.EFFACER and dp.AUTO_REFERENCES.get(t) in ("revoquer",
                                                                            "detacher"):
            n_desc = _descendants(conn, schema, t, pred(t), params)
            if n_desc:
                fait[f"{t}.descendants"] = n_desc
        if c.decision.action == dp.EFFACER:
            n = _par_lots(conn, f"DELETE FROM {t} WHERE ctid = ANY(ARRAY("
                                f"SELECT ctid FROM {t} WHERE {pred(t)} LIMIT %(lot)s))",
                          params, arret=arret)
        elif c.decision.action == dp.ANONYMISER:
            sets = ", ".join(f"{col} = NULL" for col in c.decision.colonnes)
            n = _par_lots(conn, f"UPDATE {t} SET {sets} WHERE ctid = ANY(ARRAY("
                                f"SELECT ctid FROM {t} WHERE ({pred(t)}) AND "
                                f"{_encore_nommees(c)} LIMIT %(lot)s))",
                          params, arret=arret)
        else:
            n = _pierre_tombale(conn, schema, inst.org_id)
        if n:
            fait[t] = n
            _log.info("effacement org=%s : %s %s ligne(s) de %s", inst.org_id,
                      c.decision.action, n, t)
    return fait


def residu(conn, inst: Instantane) -> dict[str, int]:
    """Ce qui relève encore de l'org après la purge : vide = effacement complet."""
    schema, classement = preparer(conn)
    tout = cibles(classement)
    pred, params = compilateur(tout), inst.parametres()
    reste: dict[str, int] = {}
    for t, c in sorted(tout.items()):
        if c.decision.action == dp.EFFACER:
            cond = pred(t)
        elif c.decision.action == dp.ANONYMISER:
            cond = f"({pred(t)}) AND {_encore_nommees(c)}"
        elif c.decision.action == dp.PIERRE_TOMBALE:
            cond = f"({pred(t)}) AND name <> %(nom)s"
        else:
            continue
        n = _compte(conn, f"SELECT count(*) AS n FROM {t} WHERE {cond}",
                    {**params, "nom": dp.PIERRE_TOMBALE_NOM})
        if n:
            reste[t] = n
    return reste


def repeter(conn, inst: Instantane) -> dict:
    """La purge entière, JOUÉE puis ANNULÉE dans une seule transaction — avant tout geste
    irréversible hors base. Une clé étrangère qu'une ligne restante pose sur une ligne
    effacée (une page d'une autre org qui pointe vers une page de celle-ci) y lève ici,
    nommée, pendant qu'il est encore temps : rien n'a été supprimé, ni dans la base ni
    dehors. Le journal est sauté (rien ne le désigne, et il pèse l'essentiel)."""
    with conn.transaction(force_rollback=True):
        fait = purger(conn, inst, sauf=frozenset(JOURNAL))
        reste = {t: n for t, n in residu(conn, inst).items() if t not in JOURNAL}
    if reste:
        raise RuntimeError(f"répétition de la purge : résidu {reste}")
    return {"tables": len(fait), "lignes": sum(fait.values())}
