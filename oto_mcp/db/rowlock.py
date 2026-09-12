"""Le bail d'une ligne — qui la tient, jusqu'à quand, et par quoi elle se libère.

Extrait de `db/datastore.py` sans un changement de comportement (#325). La file de
travail (ADR 0046 D) est une préoccupation entière : réserver, prolonger, libérer, et
savoir qui détient quoi.

Trois défauts constatés en PRODUCTION au premier essai réel valent d'être rappelés là
où ils se sont produits :

- le lien entre une ligne et le traitement en cours n'était pas enregistré, donc rien
  ne se libérait à la fin — et le TITULAIRE lui-même se voyait refuser l'écriture ;
- ce refus, non traduit en surface, ressortait en « erreur interne » ;
- la protection du chemin par LOT n'avait jamais rien protégé : un fail-open sur les
  horodatages « rendus en texte », alors que le row factory du dépôt les rend TOUS en
  texte — le cas cru marginal était le cas normal.

D'où la règle qui gouverne ce module : **une date de bail illisible REFUSE l'écriture
au lieu de l'ouvrir.** Un bail dont on ne sait pas s'il court protège peut-être encore
quelqu'un ; l'ignorance ne se résout pas en faveur de l'écrivain.

Le bail dit qui tient la ligne. Il ne dit pas combien de fois on l'a tenue pour rien :
c'est le plafond de reprises (`rowabandon`, #433), armé aux deux réservations et jugé
à chaque relâchement.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect
from .query import _ds_filter_clauses
from .rowabandon import abandonner_les_lignes_a_bout, plafond_de

# Les colonnes rendues avec une ligne réservée : son bail — À QUI, JUSQU'À QUAND et
# POUR QUEL RUN —, et ce que la file sait d'elle : combien de fois elle a été prise
# sans être écrite, et le motif si elle en est sortie (#433).
#
# ⚠️ `claimed_run` est dans TOUTE projection qui porte `claimed_by`, et pas seulement
# ici : `_row_to_dict` le lit par CLÉ (pas par `.get`) précisément pour qu'un chemin
# qui l'oublierait lève au lieu de servir un `null` — « ce bail n'a pas de run » est
# un fait, pas un défaut de SELECT. La garde mécanique est
# `tests/datastore/test_claimed_run_projection.py::test_toute_projection_de_ligne_porte_claimed_run`.
#
# `rev` (12/09/2026) : la révision de la ligne, servie `_revision` — une réservation,
# un renouvellement ou une libération la font avancer (déclencheur, `db/revision.py`).
_RENDU = ("RETURNING row_id, created_at, updated_at, data, rev, claimed_by, "
          "claimed_until, claimed_run, claims, abandon_reason, "
          "(claimed_until IS NOT NULL AND claimed_until > NOW())"
          "    AS claim_active")


def _perimetre_reclamable(ns_id: int, filters: Optional[list],
                          *, hors_plafond: Optional[int] = None) -> tuple:
    """LA définition du périmètre réclamable — une seule, partagée.

    `abandon_reason IS NULL` est un filet de PLATEFORME, indépendant du filtre du
    client : une ligne sortie de la file ne se sert plus, même à un appelant qui ne
    filtre sur rien. Le filtre dit ce que l'appelant VEUT ; ceci dit ce que le tableau
    a le DROIT de servir.

    ⚠️ **Extraite le 09/09/2026 pour que `claim_next` et `count_claimable` ne puissent
    pas diverger.** La demande venait de l'ordonnanceur, et son motif est le bon : une
    clause recopiée ailleurs *divergera* — c'est certain, pas probable — et une
    divergence ferait arrêter une campagne qui a encore du travail. Une garde qui coupe
    à tort est pire que pas de garde.

    `hors_plafond` — voir `datastore_count_claimable` : c'est le seul écart entre les
    deux appelants, et il est là parce que l'un écrit avant de lire et l'autre non.
    """
    fclauses, fparams = _ds_filter_clauses(filters)
    where = ("WHERE ns_id = %s AND abandon_reason IS NULL "
             "AND (claimed_until IS NULL OR claimed_until < NOW())")
    params: list = [ns_id, *fparams]
    for c in fclauses:
        where += f" AND {c}"
    if hors_plafond is not None:
        where += " AND claims < %s"
        params.append(int(hors_plafond))
    return where, params


def datastore_count_claimable(ns_id: int, *, filters: Optional[list] = None,
                              max_claims: Optional[int] = None) -> int:
    """Combien de lignes `claim_next` pourrait encore servir. Ne réserve rien, n'écrit
    rien.

    **Pourquoi il existe.** Une campagne dont la file est vide continuait de fabriquer
    des travaux jusqu'à son plafond : la production ne regardait pas s'il restait
    quelque chose. Mesuré le 09/09/2026 — 52 campagnes armées produisant en continu des
    déroulés qui concluaient « file vide » en quelques secondes, jusqu'à ~50 refus
    toutes les quatre minutes. Compter avant de fabriquer coûte un `COUNT` là où on
    s'apprêtait à ouvrir un déroulé d'agent de plusieurs milliers de jetons.

    ⚠️ **Ce comptage N'APPELLE PAS la passe d'abandon, et c'est délibéré : un comptage
    qui écrit serait un piège.** Mais `claim_next`, lui, l'appelle avant de piocher —
    elle marque `abandon_reason` sur les lignes à bout, qui sortent alors du périmètre.
    Compter sans reproduire cet effet SUR-ESTIMERAIT, et sur-estimer est précisément le
    défaut qu'on ferme : la campagne continuerait de produire pour des lignes que
    `claim_next` refusera. D'où `claims < plafond`, qui exclut exactement ce que la
    passe retirerait — sans rien écrire.

    ⚠️ Le chiffre reste une PHOTO : entre le comptage et la réservation, un autre
    worker peut avoir pris la dernière ligne. Il répond « reste-t-il du travail ? »,
    jamais « cette réservation aboutira ».
    """
    where, params = _perimetre_reclamable(
        ns_id, filters, hors_plafond=plafond_de(ns_id, max_claims))
    with _connect() as conn:
        row = conn.execute(
            f"SELECT count(*) AS n FROM datastore_rows {where}", tuple(params),
        ).fetchone()
    return int((row or {}).get("n") or 0)


def datastore_claim_next(ns_id: int, *, worker: str, lease_seconds: int = 900,
                         filters: Optional[list] = None,
                         run_id: Optional[str] = None,
                         max_claims: Optional[int] = None) -> Optional[dict]:
    """Claim atomique de la prochaine row claimable du namespace (ordre de
    création — row_id uuid7 monotone). `filters` = mêmes filtres whitelistés que
    la lecture (`_ds_filter_clauses`), typiquement `[{field:'status',op:'eq',…}]`.
    Renvoie la row (avec bail posé) ou None si plus rien à traiter.

    `max_claims` surcharge le plafond de reprises déclaré au schéma (#433) pour
    cette passe. La réservation INCRÉMENTE le compteur de la ligne : c'est
    l'écriture qui le remet à zéro, jamais le fait de la reprendre.

    ⚠️ La passe d'abandon tourne AVANT le pick, hors de sa transaction : elle
    ramasse les lignes à bout que personne n'a relâchées (agent mort, bail
    expiré) — le relâchement, lui, s'occupe du cas nominal."""
    abandonner_les_lignes_a_bout(ns_id, max_claims=max_claims)
    where, params = _perimetre_reclamable(ns_id, filters)
    with _connect() as conn:
        picked = conn.execute(
            f"SELECT row_id FROM datastore_rows {where} "
            "ORDER BY row_id ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
            tuple(params),
        ).fetchone()
        if not picked:
            return None
        row = conn.execute(
            "UPDATE datastore_rows SET claimed_by = %s, "
            "claimed_until = NOW() + (%s || ' seconds')::interval, claimed_run = %s, "
            "claims = claims + 1 "
            "WHERE ns_id = %s AND row_id = %s "
            + _RENDU,
            (str(worker), int(lease_seconds), run_id, ns_id, picked["row_id"]),
        ).fetchone()
        return dict(row) if row else None


def datastore_claim_row(ns_id: int, row_id: str, *, worker: str,
                        lease_seconds: int = 900,
                        run_id: Optional[str] = None,
                        filters: Optional[list] = None) -> Optional[dict]:
    """Claim d'une row **nommée** (≠ pick de la suivante) — la file pilotée par un
    humain, qui choisit la ligne qu'il traite et à qui le serveur la réserve.

    Même condition d'éligibilité que `datastore_claim_next` (bail NULL ou expiré),
    plus le RENOUVELLEMENT par le même worker : rafraîchir son écran ne doit pas
    coûter sa propre ligne. L'UPDATE conditionnel EST l'atomicité — deux appels
    concurrents sur la même row, un seul repart avec le bail.

    `filters` = le périmètre déclaré au tableau (#517), dans la clause de l'UPDATE :
    une ligne hors périmètre n'est pas réservable, même nommée, même par son
    titulaire — sinon la réservation ciblée serait la porte de côté du périmètre.

    None = row absente, hors périmètre, OU sous bail actif d'un AUTRE worker ; les
    distinguer coûte une relecture, laissée à l'appelant (chemin d'échec seulement).

    ⚠️ Le compteur de reprises monte ici aussi (#433) — mais sur une PRISE, pas sur
    un renouvellement : reprendre une ligne dont le bail a lâché compte, la garder
    non. `claim_next`, lui, n'a pas la nuance à porter : sa clause d'éligibilité
    exclut déjà le bail actif, donc il ne renouvelle jamais rien."""
    fclauses, fparams = _ds_filter_clauses(filters)
    perimetre = "".join(f" AND {c}" for c in fclauses)
    with _connect() as conn:
        row = conn.execute(
            "UPDATE datastore_rows SET claimed_by = %s, "
            "claimed_until = NOW() + (%s || ' seconds')::interval, claimed_run = %s, "
            # RÉSERVER, c'est PRENDRE une ligne : un nouveau titulaire, ou une ligne
            # dont le bail a lâché. Le titulaire qui renouvelle ne la prend pas — elle
            # ne lui a jamais échappé — donc son geste ne consomme pas le plafond
            # (#433) : sur une file pilotée à la main, rafraîchir son écran est le
            # geste le plus banal, et le compter la viderait de ses lignes.
            # ⚠️ Les colonnes lues dans le SET sont celles d'AVANT l'UPDATE (PG) :
            # `claimed_until` désigne bien le bail que cet appel remplace.
            "claims = claims + CASE WHEN claimed_until IS NULL "
            "                       OR claimed_until < NOW() THEN 1 ELSE 0 END "
            "WHERE ns_id = %s AND row_id = %s AND (claimed_until IS NULL "
            "OR claimed_until < NOW() OR claimed_by = %s)"
            + perimetre + " " + _RENDU,
            (str(worker), int(lease_seconds), run_id, ns_id, row_id, str(worker),
             *fparams),
        ).fetchone()
        return dict(row) if row else None


def datastore_row_within(ns_id: int, row_id: str, filters: list) -> bool:
    """Cette ligne est-elle DANS le périmètre (#517) ? Le chemin d'échec de
    `datastore_claim_row` : un None y couvre trois situations, et « hors périmètre »
    se dit autrement que « prise par un autre » — l'une s'attend, l'autre s'instruit.
    Jugé par le MÊME moteur que le pick, jamais par une évaluation Python du filtre
    qui divergerait de lui."""
    fclauses, fparams = _ds_filter_clauses(filters)
    where = "WHERE ns_id = %s AND row_id = %s" + "".join(f" AND {c}" for c in fclauses)
    with _connect() as conn:
        hit = conn.execute(
            f"SELECT 1 AS ok FROM datastore_rows {where}",
            (ns_id, row_id, *fparams)).fetchone()
        return hit is not None


def datastore_release_by_run(run_id: str) -> int:
    """Libère toutes les lignes réservées sous ce run — la TROISIÈME voie du verrou.

    Appelée à la fermeture d'un run, quel que soit son issue (`done`, `failed`,
    `blocked`) : un run qui se termine ne travaille plus, donc ne tient plus rien.

    ⚠️ **Ce qu'elle NE couvre PAS, contrairement à ce que ce commentaire affirmait :
    l'agent qui MEURT.** Un agent mort n'appelle pas `run_finish` — c'est la
    définition. `stale` est par ailleurs DÉRIVÉ (`run_status.is_stale`), jamais posé :
    rien ne ferme un run abandonné, donc rien ne libère ses lignes. Le seul filet pour
    ce cas reste l'expiration du bail, celui qui a mis **18 jours** à jouer sur la
    seule ligne réservée qu'ait portée la production. Le ramassage des runs abandonnés
    est une décision à part (#324).

    Ce qu'elle couvre réellement : l'agent qui TERMINE son run en oubliant de relâcher
    ses lignes. Plus petit que promis, et probablement le cas fréquent.

    Depuis #633 (29/08/2026), un second appelant : la conclusion d'un job du runner
    (`runner.jobs` op=complete) — le WORKER survit à l'agent mort et libère le run
    que le job connaît. L'agent conversationnel (hors runner) qui meurt reste au bail.

    ⚠️ Aucune garde de worker ici, et c'est voulu : la garde du release protège d'un
    agent qui libérerait la ligne d'un AUTRE. Ici c'est le run lui-même qui se ferme —
    il ne peut libérer que ce qu'il tenait, la clause `claimed_run = %s` s'en charge.

    Rend le nombre de lignes libérées (0 = le cas normal, un run qui n'a rien
    réservé)."""
    if not run_id:
        return 0
    with _connect() as conn:
        liberees = conn.execute(
            "UPDATE datastore_rows SET claimed_by = NULL, claimed_until = NULL, "
            "claimed_run = NULL WHERE claimed_run = %s "
            "RETURNING ns_id, row_id", (str(run_id),)).fetchall()
    # Un run qui se ferme sans avoir écrit est LE geste que le plafond mesure : le
    # traitement s'est conclu, la ligne revient intacte. L'évaluation suit la
    # libération, jamais l'inverse — une ligne encore sous bail n'est pas à bout.
    par_tableau: dict = {}
    for r in liberees:
        par_tableau.setdefault(int(r["ns_id"]), []).append(r["row_id"])
    for ns_id, ids in par_tableau.items():
        abandonner_les_lignes_a_bout(ns_id, row_ids=ids)
    return len(liberees)


def datastore_active_lease(ns_id: int, row_id: str) -> Optional[dict]:
    """Le bail ACTIF d'une ligne, ou None — expiré compte pour libre.

    ⚠️ « Actif » est la nuance qui empêche la protection en écriture de devenir un
    mur : un bail expiré ne protège rien (son titulaire est mort), sinon le zombie de
    18 jours mesuré en production aurait bloqué cette ligne pendant 18 jours."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT claimed_by, claimed_until, claimed_run FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s AND claimed_by IS NOT NULL "
            "  AND claimed_until IS NOT NULL AND claimed_until > NOW()",
            (ns_id, row_id)).fetchone()
        return dict(row) if row else None


def datastore_claimed_rows(ns_id: int) -> list[dict]:
    """Rows sous bail de file de travail (ADR 0046 D) — la vue « en cours » du
    dashboard. Bail actif OU expiré confondus (le consommateur tranche sur
    `claimed_until`), plus ancien bail d'abord."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data, rev, claimed_by, claimed_until, "
            "       claimed_run, claims, abandon_reason, "
            "       (claimed_until IS NOT NULL AND claimed_until > NOW())"
            "           AS claim_active "
            "FROM datastore_rows WHERE ns_id = %s AND claimed_by IS NOT NULL "
            "ORDER BY claimed_until ASC",
            (ns_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_active_leases_of(*, run_id: Optional[str] = None,
                               worker: Optional[str] = None) -> list[dict]:
    """Les lignes qu'un run (et, si donné, un worker) tient EN CE MOMENT — la
    réservation lue comme une adresse (#517).

    ⚠️ Baux ACTIFS seulement, contrairement à `datastore_claimed_rows` qui rend aussi
    les baux échus pour la vue du dashboard : ici la réponse sert à DÉSIGNER une ligne
    où écrire. Un bail échu ne désigne plus rien — la ligne est peut-être déjà repartie
    à quelqu'un d'autre, et écrire dessus au nom d'un bail mort est exactement ce que
    le verrou natif interdit.

    Sans `run_id`, aucune ligne : l'appartenance se prouve par le jeton de run, jamais
    par le seul nom de worker — celui-ci est une étiquette que l'appelant choisit, donc
    qu'un autre peut porter. Il ne sert qu'à RESTREINDRE.

    Rendu volontairement étroit (`ns_id`, `row_id`) : l'appelant nomme les tableaux
    lui-même, et rapatrier ici la jointure obligerait ce module — qui ne connaît que
    le bail — à connaître le catalogue."""
    if not run_id:
        return []
    garde = "" if worker is None else " AND claimed_by = %s"
    params: tuple = (run_id,) if worker is None else (run_id, str(worker))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ns_id, row_id, claimed_by, claimed_until FROM datastore_rows "
            "WHERE claimed_run = %s AND claimed_until IS NOT NULL "
            f"AND claimed_until > NOW(){garde} ORDER BY claimed_until ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_release_claim(ns_id: int, row_id: str, worker: Optional[str]) -> bool:
    """Libère le bail d'une row. `worker` non-None = gardé (on ne libère pas le
    claim d'un autre) ; None = libération inconditionnelle (chemin interne : entrée
    en état terminal). Renvoie False si rien n'a été libéré (pas de bail, ou bail
    d'un autre worker)."""
    guard = "" if worker is None else " AND claimed_by = %s"
    params: tuple = (ns_id, row_id) if worker is None else (ns_id, row_id, str(worker))
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE datastore_rows SET claimed_by = NULL, claimed_until = NULL "
            f"WHERE ns_id = %s AND row_id = %s AND claimed_by IS NOT NULL{guard}",
            params,
        )
        libere = (cur.rowcount or 0) > 0
    if libere:
        # Rendre la ligne sans l'avoir écrite est le cas NOMINAL du faux départ :
        # c'est ici, à la ligne qu'on vient de relâcher, que le plafond se juge.
        abandonner_les_lignes_a_bout(ns_id, row_ids=[row_id])
    return libere
