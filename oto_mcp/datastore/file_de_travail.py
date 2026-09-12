"""La FILE DE TRAVAIL (ADR 0046 D) : réserver une ligne, la rendre, et la protéger.

Extrait de `core.py` (déplacement pur, 07/09/2026) — un mixin que `DatastorePg`
compose, sur le modèle de `SchemaOpsMixin`. Les deux gardes d'écriture vivent ici
avec le bail qu'elles font respecter : `_lease_guard` sous le verrou de ligne,
`_assert_writable` pour les chemins qui n'en ouvrent pas.

⚠️ Ce module LIT des attributs de bail et de tableau (`claimed_by`, `claimed_until`,
`claimed_run`, `schema`…) : il est listé dans `vocabulaire._read_keys`.
"""
from __future__ import annotations

from typing import Optional

from .. import db
from . import claimable
from . import layers as dsl
from . import schema as dsv2
from .claimable import RowOutsideClaimable
from .errors import RowClaimed, RowLocked, RowNotFound
from .outils import _backquote, _current_run, _filter_clauses


class FileDeTravailMixin:
    """La file de travail du store. Composé par `DatastorePg`."""

    def _assert_writable(self, ns_id: int, row_id: str) -> None:
        """La même protection, pour les chemins qui n'ont PAS de verrou de ligne.

        Le remplacement et la suppression n'ouvrent pas de transaction `FOR UPDATE`
        (contrairement à la fusion) : la garde y est donc posée AVANT l'écriture, sur
        une lecture séparée. La MISE À JOUR par `id` n'est plus de ce nombre depuis le
        12/09/2026 : elle passe par le verrou de ligne et `_lease_guard`.

        ⚠️ **La fenêtre est assumée et bornée** : un claim qui s'intercalerait entre
        ce contrôle et l'écriture passerait. Elle est de l'ordre de la milliseconde,
        et infiniment plus étroite que ce qu'elle remplace — l'absence totale de
        protection sur ces chemins. La refermer demanderait de router ces deux
        gestes par le verrou de ligne, ce qui change leur sémantique (remplacer n'est
        pas fusionner) : c'est un lot, pas une rustine. Ce motif valait pour le
        remplacement, pas pour le patch, qui est une fusion : lui y est passé.

        Aucun contrôle sur une ligne NEUVE : elle ne peut pas être réservée."""
        lease = db.datastore_active_lease(ns_id, row_id)
        if not lease:
            return
        run = _current_run()
        if run and lease.get("claimed_run") == run:
            return
        raise RowLocked(row_id, lease.get("claimed_by"), lease.get("claimed_until"),
                        lease.get("claimed_run"))

    @staticmethod
    def _lease_guard(row_id: str):
        """La protection en écriture (#317) — appelée SOUS le verrou de la ligne.

        Le bail empêchait deux agents de PRENDRE la même ligne, pas d'ÉCRIRE dessus :
        il protégeait l'attribution, pas la donnée. Ici il protège les deux.

        **Le titulaire s'identifie par son RUN, et par rien d'autre** : écrire sous
        le run qui tient la ligne, c'est être le titulaire. Rien à déclarer, le cas
        nominal est transparent — et l'agent porte lui-même son `_run_id` d'appel en
        appel, donc reprendre sa ligne depuis une AUTRE session marche déjà : il
        repasse le même jeton.

        ⚠️ **Une seconde voie a existé ici jusqu'au 07/09/2026** — se réclamer du
        `worker` inscrit au bail — et elle a été retirée pour trois raisons qui se
        cumulent. Elle n'était **branchée sur rien** (aucune surface n'acceptait un
        worker à l'écriture, elle était donc inatteignable) ; elle était **redondante**
        avec le run, qui sert déjà la reprise hors session ; et elle aurait été
        **fausse** si on l'avait branchée, parce que `claimed_by` n'est pas une
        identité. Mesuré en base le 07/09 : 13 valeurs distinctes mêlant identifiants
        d'agent, étiquettes de lot datées et adresses e-mail de personnes — le schéma
        d'outil EXIGE un libellé, aucune procédure ne dit lequel, donc le modèle en
        invente un. On ne fonde pas une garde sur une identité que le contrôlé
        fabrique lui-même.

        Le prix de son retrait n'était pas ses vingt lignes : c'était que ce
        commentaire annonçait deux façons de prouver sa titularité, et qu'un lecteur
        venu diagnostiquer un refus cherchait la seconde sans jamais la trouver.

        ⚠️ **Seul un bail ACTIF protège.** Un bail expiré ne protège rien : son
        titulaire est mort, la ligne est libre. Sans cette nuance, le bail zombie
        mesuré en production (18 jours) serait devenu un mur de 18 jours.

        Pas d'échappatoire « forcer » : un bouton force devient un réflexe en trois
        clics et le verrou redevient une étiquette. La sortie est de LEVER le bail
        (`data_release`) puis d'écrire — deux gestes délibérés, chacun tracé."""
        def _guard(locked) -> None:
            until = locked.get("claimed_until")
            by = locked.get("claimed_by")
            if not by or until is None:
                return                       # libre
            # ⚠️ La date arrive en CHAÎNE, et c'est le cas NORMAL : le row factory du
            # dépôt (`db/_conn._str_dict_row`) normalise tout `datetime` en texte pour
            # les réponses JSON. Une première version retournait ici « comparaison
            # impossible ⇒ ne bloque pas » — un fail-open sur le cas courant, donc une
            # protection qui n'a JAMAIS protégé ce chemin. Constaté en production le
            # 15/08 : les écritures par lot passaient sur des lignes réservées sans un
            # mot, pendant que le chemin unitaire refusait tout le monde.
            # `run_status._as_aware` accepte les deux formes — la même fonction que le
            # reste du dépôt, plutôt qu'un second parseur qui divergerait.
            from datetime import datetime, timezone

            from ..run_status import _as_aware
            echeance = _as_aware(until)
            if echeance is None:
                # Illisible pour de bon : on REFUSE plutôt que d'ouvrir. Un bail dont
                # on ne sait pas s'il court protège encore quelqu'un ; l'ignorance ne
                # doit pas se résoudre en faveur de l'écrivain.
                raise RowLocked(row_id, by, until, locked.get("claimed_run"))
            if echeance <= datetime.now(timezone.utc):
                return                       # bail EXPIRÉ : ne protège rien
            run = _current_run()
            if run and locked.get("claimed_run") == run:
                return                       # le titulaire, par son run
            raise RowLocked(row_id, by, until, locked.get("claimed_run"))
        return _guard

    # --- file de travail (ADR 0046 D) -----------------------------------------

    def claim_next(self, datastore: str, *, worker: str,
                   filter: Optional[dict] = None, lease_s: int = 900,
                   max_claims: Optional[int] = None,
                   warnings: Optional[list] = None,
                   trace: Optional[dict] = None,
                   perimetre: Optional[dict] = None,
                   layers: str = dsl.DEFAUT,
                   filters: Optional[list] = None) -> Optional[dict]:
        """Pick + claim atomique de la prochaine row claimable (bail NULL ou
        expiré), `FOR UPDATE SKIP LOCKED` — N workers drainent sans collision.
        `filter` = `{col: val}`, ou `{col: {op: val}}` pour un opérateur (même
        grammaire que `data_rows`). Renvoie la row (avec `_claimed_by`/
        `_claimed_until`) ou None (file vide).

        `filters` = la forme en LISTE `[{field, op, value}]`, qui se cumule avec
        `filter` en ET (#356). Elle seule permet **deux bornes sur une même
        colonne** — `filter` refuse plus d'un opérateur par colonne, donc une
        plage `score >= 10 ET score <= 20` y était inexprimable, et le
        contournement (une seule borne) sert des lignes hors plage à un worker
        qui les traite quand même.

        ⚠️ Rien de neuf en dessous : `db.datastore_claim_next` prend `filters`
        depuis toujours, et `_filter_clauses` réunit déjà les deux formes. Ce qui
        manquait était le chemin — comme pour `layers`, la réservation ne portait
        pas ce que les lectures servaient depuis longtemps.

        Le périmètre déclaré au tableau (`lifecycle.claimable`, #517) passe DEVANT
        le filtre de l'appelant, en ET : celui-ci resserre, il n'élargit jamais.
        `perimetre` = dict OUT (patron `trace`) qui reçoit cette déclaration quand il
        y en a une — c'est ce qu'une réponse `row: null` doit NOMMER, sans quoi un
        filtre qui contredit le périmètre se lit comme une file vide.

        `warnings` = liste OUT (patron `trace`) où est déposé, le cas échéant, le
        défaut de configuration qui rend l'auto-release inopérante — le worker qui
        claim est celui que ça concerne, et il peut alors libérer explicitement.

        `max_claims` serre, pour cette passe, le plafond de reprises déclaré au
        schéma (#433) : la ligne réservée N fois sans écriture quitte la file. Sans
        déclaration ni paramètre, la garde ne s'arme pas."""
        worker = (worker or "").strip()
        if not worker:
            raise ValueError("worker requis (libellé stable rejoué sur release)")
        ns_id = self._resolve(datastore, write=True)
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        declare = dsv2.claimable_of(schema, ns_id)
        if perimetre is not None and declare:
            perimetre.update(declare)
        clauses = claimable.clauses(declare) + _filter_clauses(filter, filters)
        row = db.datastore_claim_next(ns_id, worker=worker,
                                      lease_seconds=int(lease_s), filters=clauses,
                                      run_id=_current_run(), max_claims=max_claims)
        if row is not None:
            self._after_claim(ns_id, warnings=warnings, trace=trace, ns=ns)
        # oto#63 : la RÉSERVATION est le seul chemin qu'un agent emprunte, et
        # c'était le seul à ne pas porter `layers`. Ce qu'il voit ici est le
        # modèle de ce qu'il réécrira — servir `champ.comment` à plat, c'est lui
        # montrer une forme qu'il transformera en `champ_comment` faute de savoir
        # qu'un point est adressable.
        return self._row_to_dict(row, schema, layers=layers) if row else None

    def claim_row(self, datastore: str, row_id: str, *, worker: str,
                  lease_s: int = 900, warnings: Optional[list] = None,
                  trace: Optional[dict] = None,
                  layers: str = dsl.DEFAUT) -> dict:
        """Réserve une row NOMMÉE — la file pilotée par un humain (il choisit qui
        appeler), là où `claim_next` sert un worker qui draine.

        Même bail, même garde au release. Renouvelable par le même `worker` (un
        rafraîchissement d'écran ne perd pas la ligne). Lève `RowNotFound` (row
        absente), `RowOutsideClaimable` (hors du périmètre déclaré au tableau, #517
        — jugé AVANT le bail : une ligne que le tableau ne sert pas n'est à personne)
        ou `RowClaimed` (bail actif d'un autre) — la distinction est ce que la
        surface doit dire à l'utilisateur, un `None` commun ne le peut pas."""
        worker = (worker or "").strip()
        if not worker:
            raise ValueError("worker requis (libellé stable rejoué sur release)")
        ns_id = self._resolve(datastore, write=True)
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        declare = dsv2.claimable_of(schema, ns_id)
        clauses = claimable.clauses(declare)
        row = db.datastore_claim_row(ns_id, row_id, worker=worker,
                                     lease_seconds=int(lease_s), run_id=_current_run(),
                                     filters=clauses)
        if row is None:
            existing = db.datastore_get_row(ns_id, row_id)
            if not existing:
                raise RowNotFound(row_id)
            if clauses and not db.datastore_row_within(ns_id, row_id, clauses):
                raise RowOutsideClaimable(row_id, declare)
            raise RowClaimed(row_id, existing.get("claimed_by"), existing.get("claimed_until"))
        self._after_claim(ns_id, warnings=warnings, trace=trace, ns=ns)
        return self._row_to_dict(row, schema, layers=layers)

    def _after_claim(self, ns_id: int, *, warnings: Optional[list],
                     trace: Optional[dict], ns: Optional[dict] = None) -> None:
        """Relevés communs aux deux claims, sur un ns_id DÉJÀ résolu : le défaut de
        configuration qui rend l'auto-release inopérante, et le contexte de journal.
        Une seule lecture de la ligne datastore pour les deux — et aucune quand
        l'appelant l'a déjà (`ns`), le périmètre l'ayant lue avant le pick."""
        if warnings is None and trace is None:
            return
        if ns is None:
            ns = self._ns_of(ns_id)
        if warnings is not None:
            w = dsv2.queue_release_warning(ns.get("schema"))
            if w:
                warnings.append(w)
        self._trace(trace, ns_id, ns)

    def release_claim(self, datastore: str, row_id: str, *, worker: str,
                      trace: Optional[dict] = None) -> dict:
        """Libère le bail (abandon sans verdict), et NOMME ce qu'elle a constaté.

        Gardé par `worker` — on ne libère pas le claim d'un autre.

        ⚠️ **Écrire un état terminal ne libère plus la ligne** : la libération
        automatique a été retirée (#317). Il faut appeler ceci après avoir écrit, ou
        encadrer le travail par `run_start` / `run_finish`, qui relâche ce qui reste.
        *Cette docstring affirmait le contraire jusqu'au 29/08/2026 — une promesse
        périmée écrite au plus près du geste, corrigée et datée ici.*

        Rend `{released, reason, lease}` et non un booléen, parce que le « non »
        couvrait DEUX situations opposées et qu'une flotte a branché sa borne d'arrêt
        dessus (#517, 29/08) :

        - `no_lease` — aucun bail sur la ligne : **bénin**, il n'y avait rien à rendre ;
        - `held_by_other` — bail tenu par un autre travail : **échec réel**, et `lease`
          dit qui le tient et jusqu'à quand.

        *Le serveur sait lequel des deux c'est : c'est dans la ligne qu'il vient de ne
        pas modifier. Un succès partiel qu'on ne peut pas distinguer d'un échec est
        pire qu'un refus — un refus, au moins, s'instruit.*"""
        ns_id = self._resolve(datastore, write=True)
        if trace is not None:
            self._trace(trace, ns_id, self._ns_of(ns_id))
        if db.datastore_release_claim(ns_id, row_id, str(worker)):
            return {"released": True, "reason": None, "lease": None}
        # Relu APRÈS coup : l'ordre est celui du geste, pas d'un diagnostic préalable.
        # Une course changerait le motif rendu, jamais le fait — la ligne n'a pas été
        # libérée dans les deux cas.
        bail = db.datastore_active_lease(ns_id, row_id)
        return {"released": False,
                "reason": "held_by_other" if bail else "no_lease",
                "lease": bail}

    def claimed_hint(self, datastore: str) -> Optional[str]:
        """Ce que le travail courant tient — dit au moment où une ADRESSE échoue (#517).

        Le refus « introuvable » tombe précisément quand l'agent s'est trompé
        d'identifiant ou de tableau, c'est-à-dire au seul instant où il peut encore
        corriger. À cet instant, le serveur sait ce que ce travail a réservé et
        l'agent, lui, l'a manifestement perdu. Le lui rendre coûte une requête.

        ⚠️ **Elle REND l'identifiant, elle ne renvoie plus vers un pronom** (07/09/2026).
        Tant que `@claimed` a vécu, cette piste finissait par « écris avec
        `id="@claimed"` plutôt que de recopier » : elle envoyait vers un raccourci qui se
        résolvait par le run, et qui a été retiré parce qu'un agent sans état n'en a pas
        un stable. Ce qui reste est ce qui servait déjà — les identifiants eux-mêmes,
        énoncés au seul moment où l'agent les cherche.

        None quand il n'y a rien d'utile à dire — hors run, ou aucune réservation :
        une piste vide vaut mieux qu'une phrase qui meuble."""
        ici, ailleurs = self._baux_du_run(datastore)
        if not ici and not ailleurs:
            return None
        if ici:
            return (f"ton travail tient {_backquote(ici)} dans `{datastore}` — "
                    "c'est l'identifiant à passer dans `id`, sans le recopier de mémoire")
        return (f"ton travail ne tient rien dans `{datastore}`, mais tient une ligne "
                f"dans {_backquote(ailleurs)} — c'est peut-être le tableau que tu visais")

    def _baux_du_run(self, datastore: str):
        """Ce que le travail courant tient : ici, et ailleurs — source unique de la piste
        rendue quand une adresse échoue (#517).

        `(None, [])` distingue « pas de travail sur cet appel » de « un travail qui ne
        tient rien » : le premier se répare en passant `_run_id`, le second en réservant
        une ligne. Les confondre dirait à l'agent de faire ce qu'il a déjà fait.

        ⚠️ **Plus de restriction par `worker`** (07/09/2026). Elle n'a jamais servi qu'à
        `@claimed` posé sur `data_release`, seul verbe qui passait le libellé du claim :
        un libellé qui ne retrouvait rien alors que le run tenait des lignes était nommé
        comme tel. Le pronom retiré, le seul appelant restant est la piste, qui ne passe
        aucun libellé — et un `data_release` au mauvais libellé reçoit déjà de
        `release_claim` le refus qui NOMME qui tient la ligne."""
        run = _current_run()
        if not run:
            return None, []
        baux = db.datastore_active_leases_of(run_id=run)
        if not baux:
            return [], []
        ns_id = self._resolve(datastore)
        ici = [str(b["row_id"]) for b in baux if b["ns_id"] == ns_id]
        ailleurs = sorted({str(self._ns_of(b["ns_id"]).get("datastore") or b["ns_id"])
                           for b in baux if b["ns_id"] != ns_id})
        return ici, ailleurs

    def queue(self, datastore: str) -> list[dict]:
        """Vue de SUPERVISION de la file (dashboard) : les rows sous bail —
        actif ou expiré, le consommateur tranche sur `_claimed_until`. Lecture
        seule (aucun droit d'écriture requis)."""
        ns_id = self._resolve(datastore)
        sch = self._schema_of(ns_id)
        return [self._row_to_dict(r, sch, bail_echu="servir")
                for r in db.datastore_claimed_rows(ns_id)]

    def force_release(self, datastore: str, row_id: str, *,
                      trace: Optional[dict] = None) -> bool:
        """Libère le bail SANS garde de worker — supervision humaine (dashboard),
        ≠ `release_claim` (agent, gardé). Exige le droit d'écriture. False = pas
        de bail à libérer."""
        ns_id = self._resolve(datastore, write=True)
        if trace is not None:
            self._trace(trace, ns_id, self._ns_of(ns_id))
        return db.datastore_release_claim(ns_id, row_id, None)
