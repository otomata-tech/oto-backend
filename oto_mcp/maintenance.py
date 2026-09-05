"""Les travaux de maintenance du backend — hors du démarrage (ADR 0065, lot 0).

Quatre travaux tournaient à la fin d'`init_db`, donc **à chaque boot de chaque
environnement**, dans une fenêtre de healthcheck finie (120 s, sonde directe
`127.0.0.1:9103`). Ils avaient la forme d'un cron et le coût d'un cron : purger,
re-projeter, poser des index — le seul poste du démarrage dont la durée suit la
taille de la base, donc le seul qui transformera un jour un déploiement sain en
rollback sans que rien n'ait changé dans le lot.

Ils sont ici, chacun nommé, chacun jouable seul :

    oto-mcp maintenance retention     purge du fil des runs + des runs sans faits
    oto-mcp maintenance blocks        re-projection du corps des nœuds en blocs
    oto-mcp maintenance key-indexes   index d'unicité de clé métier par namespace
    oto-mcp maintenance check-boot    rejoue l'ordre du boot en transaction ANNULÉE
    oto-mcp maintenance all           les trois premiers, dans l'ordre

    oto-mcp maintenance key-index-rebuild   (#421 — voir plus bas, PAS dans `all`)
    oto-mcp maintenance journal-tokens      purge rétroactive des jetons écrits en
                                            clair dans le journal (#558) — À BLANC
                                            par défaut, `--apply` pour écrire

Trois propriétés qu'aucun de ces travaux ne perd en changeant de porte :
**idempotents** (les rejouer ne change rien), **fail-open par travail** (un échec
est journalisé, les autres continuent, le code de sortie reste 0 sauf `--strict`),
**journalisés** (une ligne par travail, avec sa durée et son compte).

⚠️ **La base est PARTAGÉE prod/preprod.** Le timer n'est posé que côté PROD
(`deploy/oto-backend.sh`) : deux exécutants sur la même base ne feraient que se
disputer les mêmes lignes.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Callable

logger = logging.getLogger("oto_mcp.maintenance")

# Fenêtre de rétention du JOURNAL d'appels, en jours — **une seule politique** (#426),
# le même nom d'environnement et le même défaut que `deploy/archive_tool_calls.py`.
# C'était le point du lot : il y en avait DEUX, 30 j au boot et 90 j au timer, et la
# plus courte gagnait en silence — le boot supprimait sans exporter, donc l'archive S3
# posée le 27/08 n'aurait jamais trouvé un mois complet à prendre (mesuré le 28/08 :
# 0 ligne au-delà de 30 j sur 969 314). Ce module ne purge PAS le journal (c'est
# l'archive qui le fait, après l'avoir exporté) ; il purge à la MÊME borne ce qui
# n'a pas d'archive : les étiquettes de runs devenues orphelines.
_JOURNAL_RETENTION_DAYS = int(os.environ.get("OTO_JOURNAL_RETENTION_DAYS", "90"))
# Le fil des runs hébergés a SA fenêtre, et ce n'est pas une seconde politique de
# rétention du journal : c'est un autre objet. Le fil est l'état d'exécution d'un
# run, pas sa vérité — celle-ci est au journal, et un run se reprend par le journal
# (ADR 0064-D3). On l'efface donc plus court, sans archive.
_RUN_THREAD_RETENTION_DAYS = int(
    os.environ.get("OTO_MCP_RUN_THREAD_RETENTION_DAYS", "30"))


# --------------------------------------------------------------------------- #
# Les travaux
# --------------------------------------------------------------------------- #

def retention(*, dry_run: bool = False) -> dict:
    """Purge bornée : les runs sans faits, puis le fil des runs hébergés.

    **Ne touche pas `tool_calls`.** La rétention du journal appartient à
    `deploy/archive_tool_calls.py` (timer `oto-journal-archive`), qui EXPORTE le mois
    au froid S3 avant de le supprimer. Le boot, lui, supprimait à 30 jours *sans rien
    exporter* — mesuré le 2026-08-28 : 0 ligne au-delà de 30 j dans une table de
    969 314 lignes, donc l'archive posée le 27/08 n'aurait jamais trouvé un seul mois
    à archiver. Ce n'était pas une politique en double, c'était une politique qui en
    annulait une autre.

    Un run dont les faits sont partis au froid devient une étiquette qui annonce
    « prospection Q3 → done » au-dessus d'une page vide (#289) : on l'efface, à la
    même borne que le journal.
    """
    from .db import run_thread, usage
    out: dict = {"retention_days": _JOURNAL_RETENTION_DAYS,
                 "run_thread_days": _RUN_THREAD_RETENTION_DAYS}
    if dry_run:
        out["orphan_runs"] = usage.count_orphan_runs(_JOURNAL_RETENTION_DAYS)
        out["run_messages"] = run_thread.count_prunable_run_messages(
            _RUN_THREAD_RETENTION_DAYS)
        return out
    out["orphan_runs"] = usage.prune_orphan_runs(_JOURNAL_RETENTION_DAYS)
    out["run_messages"] = run_thread.prune_run_messages(_RUN_THREAD_RETENTION_DAYS)
    return out


def blocks(*, dry_run: bool = False) -> dict:
    """Re-projette en blocs le corps des nœuds dont le marqueur ne correspond plus.

    Sorti du boot alors même que son régime stable y coûtait 130 ms : ce n'est pas le
    régime stable qui décide, c'est la ROTATION DE MARQUEUR. Le jour où le marqueur
    est passé de `blocks_md5` à `blocks_md5_v2`, les 1 526 nœuds ont été re-parsés au
    démarrage — 4 allers-retours chacun sur une base managée à 3,1 ms, soit ~19 s
    ajoutés à la fenêtre du healthcheck par un lot qui ne savait pas les ajouter.
    C'est exactement le déploiement sain que l'ADR 0065 veut cesser de rollbacker.

    Sortir est sûr parce que les blocs sont une PROJECTION : leur seul lecteur est la
    vue d'un nœud (`db/node_view.py`), et un retard d'un tir de timer y est visible
    comme un corps affiché depuis sa source, pas comme une erreur.
    """
    from .db import blocks as db_blocks
    if dry_run:
        return {"stale": db_blocks.count_stale_nodes()}
    return {"parsed": db_blocks.backfill_node_blocks()}


def key_indexes(*, dry_run: bool = False) -> dict:
    """Pose l'index UNIQUE de clé métier des namespaces qui en déclarent une (#109 ch.3).

    Sorti du boot parce que son coût est le NOMBRE DE NAMESPACES, pas leur contenu, et
    que ce nombre ne fait que croître : 204 namespaces à clé le 2026-08-28, un
    aller-retour chacun pour vérifier que l'index est là — 644 ms mesurées, payées
    trois fois par boot (`init_db` était appelé trois fois), pour zéro index manquant.

    Fail-open PAR namespace : un tableau récalcitrant est journalisé et n'empêche ni
    les autres, ni le reste de la maintenance. Son chemin d'écriture reste
    l'applicatif historique tant que son index n'est pas posé.
    """
    from .db import datastore as ds
    targets = ds.datastore_namespaces_with_key()
    manquants = [ns for ns in targets if not ds.datastore_has_key_index(ns["id"])]
    if dry_run:
        return {"namespaces": len(targets), "missing": len(manquants)}
    poses, resorbes, echecs = 0, 0, 0
    for ns in manquants:
        try:
            removed = ds.datastore_merge_key_duplicates(ns["id"], ns["key"])
            # `bornee=False` : travail de FOND. Les bornes du DDL à chaud existent
            # pour qu'un appel de requête n'attende pas ; ici, attendre son tour ne
            # dessert personne — et borner garantirait qu'un index sur une table très
            # occupée ne se pose jamais.
            ds.datastore_ensure_key_index(ns["id"], ns["key"], bornee=False)
            poses += 1
            resorbes += removed
            if removed:
                logger.info("key-index ns=%s key=%s : %d doublon(s) résorbé(s)",
                            ns["id"], ns["key"], removed)
        except Exception:
            echecs += 1
            logger.warning("key-index ns=%s : échec (fail-open)", ns.get("id"),
                           exc_info=True)
    return {"namespaces": len(targets), "posed": poses,
            "duplicates_merged": resorbes, "failed": echecs}


def key_index_rebuild(*, dry_run: bool = False) -> dict:
    """Reconstruit TOUS les index de clé métier sur l'expression polymorphe (#318).

    ⚠️ **Ce travail n'a jamais tourné en production** (oto-backend#421) : son appel
    était placé après une boucle dont chaque branche `return` ou `raise`, donc aucun
    chemin ne l'atteignait. Le lot 0 retire l'appel mort et met la fonction ici, où
    elle est appelable — **délibérément hors de `all` et sans timer** : la faire
    tourner pour la première fois est une décision qui change l'état de la production,
    pas un effet de bord de sortie de maintenance.
    """
    from .db import _init
    if dry_run:
        from .db import datastore as ds
        return {"namespaces": len(ds.datastore_namespaces_with_key()),
                "note": "aucun index reconstruit (dry-run) — cf. oto-backend#421"}
    return {"rebuilt": _init.migrate_business_key_indexes()}


def journal_tokens(*, dry_run: bool = True) -> dict:
    """Purge RÉTROACTIVE des jetons écrits en clair dans le journal (#558).

    Le masquage à l'écriture ne vaut que pour les lignes à venir : celles déjà
    posées portent, sur toute la fenêtre de rétention, des jetons d'upload, de
    partage et d'invitation en clair. Cette commande les ramène à la route réduite
    — une RÉPARATION, pas une suppression : la télémétrie de surface (qui, quand,
    quel code, quelle durée) reste, elle cesse seulement de nommer le secret.

    ⚠️ **À BLANC PAR DÉFAUT, et hors de `all`.** Elle réécrit des lignes servies
    aux lentilles de supervision sur une base PARTAGÉE prod/preprod : la lancer est
    une décision, pas un effet de bord de sortie de maintenance. Même règle que
    `key-index-rebuild` (#421). `--apply` pour écrire.

    Ce qu'elle purge est DÉRIVÉ de la table de routes servie et du registre de
    capacités, jamais d'une liste tenue à la main — c'est la même déclaration que
    le masquage à l'écriture, et deux listes divergeraient.
    """
    from . import journal_secrets
    from .api import routes as api_routes
    from .db import journal_purge
    # `make_routes` ne fait que capturer le verifier au montage (cf.
    # `tests/api/test_api_routes_table_frozen.py`) : un objet nu suffit à obtenir
    # la table, et c'est elle qui déclare `{token}` / `{code}`.
    api_routes.make_routes(object(), mcp_instance=None)
    plans = journal_secrets.journal_purge_plans()
    if not plans:
        raise RuntimeError(
            "aucune route à paramètre secret déclarée — la table de routes n'a pas "
            "été montée, la purge ne saurait pas quoi chercher")
    out: dict = {"applied": not dry_run,
                 "routes": journal_purge.purge_route_tokens(plans, dry_run=dry_run)}
    out["args"] = journal_purge.purge_arg_tokens(
        journal_secrets.secret_arg_names_by_tool(), journal_secrets.mask,
        dry_run=dry_run)
    return out


def check_boot(*, dry_run: bool = True) -> dict:
    """Rejoue l'ORDRE DU BOOT (DDL assemblé PUIS les ALTER) en transaction ANNULÉE.

    Le garde-fou qui manquait le 2026-08-27 (#450) : un index posé dans le DDL de base
    sur une colonne qui naît d'un ALTER. Le DDL seul passait, la migration seule
    passait — **c'est leur ordre qui échouait**, et rien ne jouait cet ordre ailleurs
    qu'au démarrage d'un vrai serveur. Ici, il se joue contre n'importe quelle base,
    y compris une base SERVIE, sans y laisser de trace : la transaction est annulée.

    `dry_run` n'est pas optionnel dans les faits — cette commande n'écrit jamais.
    """
    from .db._conn import _connect
    from .db._init import replay_boot_schema_dry
    with _connect() as conn:
        replay_boot_schema_dry(conn)
    return {"replayed": True, "committed": False}


# Ce que `all` enchaîne — l'ordre compte : la purge d'abord (elle réduit ce que les
# deux suivants ont à regarder), la re-projection ensuite, les index en dernier
# (seuls à poser du DDL). `key-index-rebuild` et `check-boot` n'y sont PAS : le
# premier change l'état de la prod pour la première fois (#421), le second est un
# diagnostic.
def residu_projete(*, dry_run: bool = True) -> dict:
    """Retire de `nodes` l'image que la recopie y déposait à chaque boot.

    Cinq conversions ont tourné au démarrage jusqu'au 2026-09-01 : elles copiaient
    projets, pages, procédures, tableaux et lignes dans `nodes`, chaque copie marquée
    `props.legacy`. Elles préparaient une bascule de lecture qui n'aura pas lieu — les
    deux univers vivent désormais côte à côte, chacun avec ses verbes. La copie n'a
    donc plus ni écrivain (l'arrêt est dans `db/_init.py`) ni lecteur, et ce travail
    la retirait : 75 668 nœuds sur 75 721 et 34 314 blocs à son plus haut, le
    2026-09-01. ⚠️ **Le stock n'est plus là** — relevé en lecture seule sur la base
    servie le 2026-09-01 à 21:27 UTC : 0 nœud recopié, 0 bloc orphelin. Le geste
    reste, son stock est fait. C'est le mode à blanc qui dit l'état du jour, et
    **jamais** un chiffre recopié d'ici.

    ⚠️ **Ce chiffre se DATE et se REFAIT avant de jouer.** Il valait 70 876 quatre
    heures plus tôt : la recopie a tourné jusqu'au déploiement de son arrêt. Et
    depuis cet arrêt, la PURGE ne tourne plus non plus (elle vivait dans la même
    fonction) — donc une page supprimée laisse désormais une copie entière que
    rien ne retire. Le décompte à blanc est là pour ça : on le relit, on ne le
    récite pas.

    **Ici et pas au boot** : le coût suit la taille de la base, la fenêtre du
    healthcheck est finie (ADR 0065). Et **à blanc par défaut** : c'est un ACTE, pas
    une routine — il n'est dans aucun timer, `--apply` l'exécute.

    ⚠️ **Le compte est un DIFFÉRENTIEL d'inventaire, jamais la réponse du geste.** Un
    `DELETE` qui ne trouve rien annonce « zéro ligne » exactement comme un `DELETE`
    qui vient de finir son travail : lire son retour, c'est confondre « fait » et
    « rien à faire ». On compte donc avant, on retire, on recompte — et `retires` est
    la soustraction.

    ⚠️ **Une passe peut ne pas tout prendre** et c'est voulu : `delete_projected_nodes`
    a un plafond de lots. Le retour porte `restants` — non nul, il dit de rappeler,
    pas que quelque chose a échoué.

    ⚠️ **Le mode à blanc annonce TOUTE la surface qu'`--apply` emporterait**, blocs
    attachés compris (#800). Il n'annonçait que les nœuds et les orphelins et taisait
    les blocs qui pendent aux nœuds recopiés — 34 314 au 2026-09-01, la plus grosse
    part de ce qui partait. Un inventaire incomplet est pire qu'aucun inventaire : il
    donne confiance à tort.

    ⚠️ **Ce travail ne touche plus RIEN qui ne soit marqué `props.legacy`** (#800,
    point ③). Il balayait aussi les blocs orphelins, sans prédicat de provenance —
    donc du contenu NATIF dont le nœud avait pu être supprimé par erreur, sous un nom
    qui promettait le contraire. `blocs_orphelins` reste dans l'inventaire, mais comme
    TÉMOIN : la contrainte `blocks_node_fk` rend l'orphelin impossible, ce compte doit
    valoir 0, et un non-zéro dit que la contrainte manque sur cette base — pas qu'il
    reste du ménage.
    """
    from .db import nodes as db_nodes
    avant = db_nodes.count_projected_nodes()
    blocs_avant = db_nodes.count_projected_blocks()
    orphelins = db_nodes.count_orphan_blocks()
    if dry_run:
        return {"projetes": avant, "blocs_attaches": blocs_avant,
                "blocs_orphelins": orphelins}
    db_nodes.delete_projected_nodes()
    apres = db_nodes.count_projected_nodes()
    return {
        "retires": avant - apres,
        "restants": apres,
        "blocs_attaches_retires": blocs_avant - db_nodes.count_projected_blocks(),
        "blocs_orphelins": db_nodes.count_orphan_blocks(),
    }


def portee_observation(*, dry_run: bool = True) -> dict:
    """Rend le volume d'alertes que l'ADR 0068 §4 aurait envoyées — sans rien envoyer.

    Période d'OBSERVATION (décision d'Alexis, 04/09/2026) : `portee_elargissements`
    enregistre chaque fois qu'un AGENT fait sortir un contenu du périmètre de son
    propriétaire, avec les destinataires qu'on aurait prévenus. Aucun message ne part
    tant que ce comptage n'a pas dit ce que le canal coûterait.

    ⚠️ **Ce travail ne fait que LIRE, et n'a pas de mode `--apply`.** Il n'écrit rien,
    ne notifie rien, n'efface rien — le mettre dans `_ACTES` suffirait à laisser croire
    qu'il existe une version qui agit.

    Ce qu'il faut regarder, et dans cet ordre : `sans_login` (ces alertes-là partiraient
    IMMÉDIATEMENT, et une seule de trop est un incident) puis `personnes` — trente
    élargissements vers une seule personne ne font pas le même produit que trente
    élargissements vers trente personnes. Le volume brut, lui, ne décide de rien."""
    from .db import portee as db_portee

    lignes = db_portee.compter_par_vers()
    total = sum(int(l["n"]) for l in lignes)
    sans_login = sum(int(l["n"]) for l in lignes if l["immediat"])
    personnes = max((int(l["proprietaires"]) for l in lignes), default=0)
    return {"observation": "aucun message envoyé (ADR 0068 §4)",
            "elargissements": total, "sans_login": sans_login,
            "personnes_concernees_max": personnes,
            "detail": [dict(l) for l in lignes]}


#: L'interrupteur de l'alerte. **OFF par défaut, et c'est le cœur du dispositif** : le
#: mécanisme part au tag, l'effet attend une décision (oto#59). Un canal qu'on ouvre en
#: devinant son volume est un canal qu'on referme au bout d'une semaine, après avoir
#: appris à ses destinataires à l'ignorer.
#:
#: ⚠️ Lu à CHAQUE tir, jamais mis en cache au boot : ouvrir le canal ne doit pas
#: demander de redémarrer le service.
_ENV_ALERTE = "OTO_ALERTE_CREDENTIAL"


def _alerte_activee() -> bool:
    return os.environ.get(_ENV_ALERTE, "").strip().lower() in ("1", "true", "yes", "on")


def alertes_credential(*, dry_run: bool = False) -> dict:
    """Prévient le titulaire d'une org qu'une clé est partie sous ses agents programmés.

    Le 03/09/2026, une clé a disparu et **une douzaine de passages programmés ont tourné
    à l'aveugle pendant 36 heures**. Le canal qui aurait annoncé la panne tournait sur le
    credential tombé : six fois par jour, un run découvrait qu'il était cassé, l'inscrivait
    sur une ligne que personne ne regardait, et se taisait — correctement, selon ses
    propres règles. La panne était silencieuse **par construction** (oto#59).

    ⚠️ **Le courriel part par le courrier de PLATEFORME**, jamais par un connecteur de
    l'org. C'est la seule propriété qui distingue cette alerte du registre qu'elle
    remplace : le canal qui prévient ne doit pas pouvoir mourir avec ce dont il annonce
    la mort.

    ⚠️ **UN courriel par org**, pas un par ligne : trois clés retirées le même jour font
    un message. Un destinataire qui en reçoit trois pour un incident apprend à les
    ignorer.

    ⚠️ **Rien ne part tant que `OTO_ALERTE_CREDENTIAL` n'est pas posé**, et le travail
    le DIT dans son retour plutôt que de rendre un zéro qui se lirait « rien à
    signaler ». `dry_run` fait la même chose en le disant autrement — les deux comptent
    ce qui partirait.

    ⚠️ Le marquage vient APRÈS l'envoi. Marquer d'abord transformerait un envoi raté en
    silence définitif, c'est-à-dire en la panne même que ce travail supprime.
    """
    # ⚠️ `import email as _email` puis `_email._send(...)`, jamais
    # `from .email import _send` : la seconde forme capture la référence à l'import et
    # rend le module intestable (un banc qui patche `email._send` ne toucherait rien).
    # C'est la convention d'`email_templates`, écrite pour cette raison exacte.
    from . import email as _email
    from .db import alertes_credential as db_alertes
    from .db import users as db_users
    from . import org_store

    groupes = db_alertes.a_notifier()
    actif = _alerte_activee()
    envoyes, marques, sans_destinataire = 0, 0, 0
    for g in groupes:
        org_id = int(g["org_id"])
        admins = [m["sub"] for m in org_store.list_org_members(org_id)
                  if m.get("org_role") == "org_admin"]
        adresses = [e for e in db_users.emails_by_subs(admins).values() if e]
        if not adresses:
            # On ne marque PAS : sans destinataire, la ligne reste à notifier. Le jour
            # où l'org gagne un admin, elle partira — la perdre ici serait la perdre
            # exactement quand elle devient délivrable.
            sans_destinataire += 1
            continue
        if not actif or dry_run:
            continue
        connecteurs = ", ".join(g["connectors"] or [])
        # Écrit pour un humain qui ne connaît pas le vocabulaire de la plateforme :
        # ce qui est arrivé, ce que ça casse, et les deux gestes possibles.
        corps = (
            f"<p>Une clé de connecteur a été retirée de votre organisation "
            f"({_email._esc(connecteurs)}), alors que "
            f"{int(g['agents_max'] or 0)} agent(s) programmé(s) actif(s) "
            "l'utilisaient.</p>"
            "<p>Ils continueront de partir à l'heure et échoueront en vol, sans que "
            "personne d'autre en soit averti : coupez-les, ou reposez une clé.</p>"
            "<p>Oto, pour Alexis</p>")
        if _email._send(to=adresses[0],
                        subject="Une clé retirée sous vos agents programmés",
                        html=corps):
            envoyes += 1
            marques += db_alertes.marquer_notifie(list(g["ids"] or []))
    return {"orgs_a_prevenir": len(groupes), "envoyes": envoyes, "marques": marques,
            "sans_destinataire": sans_destinataire,
            "actif": actif,
            "note": (None if actif else
                     f"aucun envoi : {_ENV_ALERTE} n'est pas posé — le mécanisme "
                     "tourne, l'effet attend une décision")}


_TRAVAUX: dict[str, Callable[..., dict]] = {
    "retention": retention,
    "blocks": blocks,
    "key-indexes": key_indexes,
    "key-index-rebuild": key_index_rebuild,
    "journal-tokens": journal_tokens,
    "check-boot": check_boot,
    "residu-projete": residu_projete,
    "portee-observation": portee_observation,
    "alertes-credential": alertes_credential,
}
# Travaux dont l'écriture est un ACTE, pas une routine : à blanc par défaut, et
# c'est `--apply` qui écrit. Ils ne sont dans aucun timer et jamais dans `all`.
_ACTES = ("journal-tokens", "residu-projete")
# ⚠️ `alertes-credential` est dans `_ALL` — donc dans le timer quotidien — ET son
# envoi est fermé par `OTO_ALERTE_CREDENTIAL`. Les deux ensemble sont le dispositif :
# le mécanisme tourne dès le tag (on voit ce qui partirait), l'effet attend une
# décision. Y mettre un travail qui écrit dehors ne se ferait pas autrement.
_ALL = ("retention", "blocks", "key-indexes", "alertes-credential")


def run(noms: list[str], *, dry_run: bool = False, strict: bool = False) -> int:
    """Joue les travaux nommés, chacun chronométré et journalisé. Rend un code de sortie.

    **Fail-open par défaut** : un travail qui casse est journalisé et n'empêche pas les
    suivants, et le code reste 0 — un timer de maintenance qui rougit pour une purge
    ratée réveille quelqu'un pour rien. `--strict` inverse ce choix, pour la CI.
    """
    echecs = 0
    for nom in noms:
        fn = _TRAVAUX[nom]
        debut = time.monotonic()
        try:
            out = fn(dry_run=dry_run)
            logger.info("maintenance %s%s : %.0f ms — %s", nom,
                        " (à blanc)" if dry_run else "",
                        (time.monotonic() - debut) * 1000, out)
        except Exception:
            echecs += 1
            logger.error("maintenance %s : ÉCHEC après %.0f ms", nom,
                         (time.monotonic() - debut) * 1000, exc_info=True)
    return 1 if (echecs and strict) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="oto-mcp maintenance",
        description="Les travaux de maintenance du backend (ADR 0065, lot 0).")
    p.add_argument("travail", choices=sorted(_TRAVAUX) + ["all"],
                   help="le travail à jouer, ou `all` pour " + ", ".join(_ALL))
    p.add_argument("--dry-run", action="store_true",
                   help="compte ce qu'il y aurait à faire, n'écrit rien")
    p.add_argument("--apply", action="store_true",
                   help=("écrit, pour les travaux qui sont à blanc par défaut ("
                         + ", ".join(_ACTES) + ")"))
    p.add_argument("--strict", action="store_true",
                   help="code de sortie 1 si un travail échoue (CI)")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    noms = list(_ALL) if args.travail == "all" else [args.travail]
    # Le sens du défaut s'INVERSE pour un acte : ailleurs `--dry-run` est l'opt-in
    # d'un travail qui écrit, ici `--apply` est l'opt-in d'un travail qui compte.
    a_blanc = (not args.apply) if args.travail in _ACTES else args.dry_run
    if args.apply and args.travail not in _ACTES:
        p.error("--apply ne vaut que pour : " + ", ".join(_ACTES))
    return run(noms, dry_run=a_blanc, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
