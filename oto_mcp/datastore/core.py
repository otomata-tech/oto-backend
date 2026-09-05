"""Datastore — substrat natif PostgreSQL (ADR 0016).

Un namespace = une ligne `user_datastores` + ses rows dans `datastore_rows`
(une row = un dict JSONB). Schéma libre : aucune colonne à provisionner, les
champs apparaissent dans `data`. Trois champs auto-managés, exposés à plat dans
la row renvoyée :

- `_id` : identifiant uuid7-like (col `row_id`).
- `_created_at` / `_updated_at` : timestamps (colonnes dédiées).
- `_claims` / `_abandon` : ce que la file de travail sait de la ligne —
  réservations sans écriture, et motif si le plafond l'en a sortie (#433).

Plus de dépendance Google : la vérité est en base, types préservés nativement
par JSONB (fin de la sentinelle `__j:` de l'ère Sheets). La propriété et le partage
passent par la primitive générique `ownership` (ADR 0030) : un namespace est possédé
par `(owner_type, owner_id)` (user/org/group) et accessible via owner-match ∪ grants
(`resource_grants`). L'export vers un provider tiers (Sheets/Notion…) est une
projection optionnelle, déférée à otomata#29.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy

import base64
import binascii
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.errors import UniqueViolation

from . import ecartes as dsec
from . import layers as dsl
from . import schema as dsv2
from . import claimable, hors_org
# Les refus : extraits dans `errors` (#325), ré-importés ici pour que tout appelant
# (`from .datastore.core import RowNotFound`) reste inchangé.
from .claimable import RowOutsideClaimable  # noqa: F401
from .schema_ops import SchemaOpsMixin
from .forcage import Forcage
from .reserves import (
    iso_utc,
    poser_origine_systeme,
    poser_valeurs_systeme,
    refuser_champs_reserves,
    valeurs_systeme,
)
from .errors import (  # noqa: F401
    BusinessKeyRequired,
    ClaimedRefUnresolved,
    InvalidCursor,
    NamespaceExists,
    NamespaceForbidden,
    NamespaceNotFound,
    NamespaceReadOnly,
    RowClaimed,
    RowLocked,
    RowNotFound,
    RowValidationError,
)
from .. import db, ownership, session_org
from .. import config
from ..db.query import ds_filter_specs as _filter_specs

logger = logging.getLogger(__name__)


# La colonne et ses couches : extraites dans `columns` (#325), ré-importées
# ici pour que les appelants (et les tests qui les exercent) restent inchangés.
from .columns import (  # noqa: E402,F401
    _META_COLS,
    _existing_layers,
    _merge_column,
    _refuse_flat_writes,
    _refuse_mixed_layers,
    _resolve_filters,
    _resolve_group_by,
    _resolve_metrics,
    _to_path,
    _writes_layers,
    arbitrer_les_vides,
    refuser_geste_sans_effet,
    effacements_report,
    ignores_report,
)
# Les noms POINTÉS (#684/#687) : `ranger_les_couches` referme l'aller-retour —
# ce qu'on sert doit pouvoir être réécrit tel quel — et `_refuse_dotted_names`,
# rétréci, ne tranche plus que ce qui reste sans adresse.
from .points import (  # noqa: E402,F401
    _refuse_dotted_names,
    ranger_les_couches,
)


def _encode_cursor(row_id: str) -> str:
    """Curseur opaque = base64url du dernier `row_id` de la page (keyset)."""
    return base64.urlsafe_b64encode(row_id.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise InvalidCursor(cursor) from e


_OFFSET_CURSOR_PREFIX = "off:"


def _encode_offset_cursor(offset: int) -> str:
    """Curseur du chemin TRIÉ (`order_by`) : l'ordre n'étant plus celui du keyset
    `row_id`, la page suivante se repère par offset. Même forme opaque que le curseur
    keyset, préfixée pour ne jamais confondre les deux régimes."""
    return _encode_cursor(f"{_OFFSET_CURSOR_PREFIX}{offset}")


def _decode_offset_cursor(cursor: str) -> int:
    raw = _decode_cursor(cursor)
    if not raw.startswith(_OFFSET_CURSOR_PREFIX):
        raise InvalidCursor(cursor)  # curseur keyset repassé sur un appel trié
    try:
        return max(0, int(raw[len(_OFFSET_CURSOR_PREFIX):]))
    except ValueError as e:
        raise InvalidCursor(cursor) from e


def _filter_clauses(filter: Optional[dict], filters: Optional[list]) -> list[dict]:
    """Les DEUX formes de filtre d'un même appel, réunies en une liste de clauses.

    `filter` = le raccourci `{colonne: valeur}` (une colonne à la fois) ; `filters` =
    la forme complète `[{field|fields, op, value, match?}]`, seule capable de viser
    plusieurs colonnes déclarées (oto#22). Elles se cumulent en ET.

    Point unique, et il a coûté un défaut : `aggregate` et `page_rows` recopiaient la
    conversion en la simplifiant en égalité, si bien qu'un opérateur imbriqué —
    `{"posted_at": {"gte": "2026-06-01"}}`, la forme que la fiche de `data_rows`
    documente — y comparait la colonne au TEXTE d'un dictionnaire Python. Zéro ligne,
    aucune erreur : la même syntaxe répondait juste sur un verbe et faux sur l'autre.
    """
    return _filter_specs(filter) + list(filters or [])


def _now_iso() -> str:
    # Même forme que toute estampille posée par la plateforme (#859) : les deux
    # sources système d'une date en rendaient deux, et un tri les rangeait par
    # l'alphabet. La règle vit à UN endroit — `reserves.iso_utc` — pour qu'elles
    # ne puissent plus diverger.
    return iso_utc(datetime.now(timezone.utc))


def _new_id() -> str:
    # uuid7-ish : timestamp ms + random. Construit à la main pour compat 3.10+.
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = uuid.uuid4().int & ((1 << 74) - 1)
    raw = (ms << 80) | (0x7 << 76) | (rand << 2)
    return str(uuid.UUID(int=raw))


def _ns_url(ns_id: int, sub: Optional[str] = None) -> Optional[str]:
    """Deep-link vers la vue datastore du dashboard (surface d'édition canonique
    tant que l'export tiers — otomata#29 — n'existe pas). Par ID (`/data/<id>`,
    BIGSERIAL stable au renommage) — l'adressage `?ns=<nom>` est déprécié.

    ⚠️ **Peut valoir `None`** : le produit d'un partenaire n'a pas forcément de vue
    tableau (celui du 13/08 n'en a aucune). On ne rend alors AUCUN lien — un lien mort
    ne se diagnostique pas, il se subit."""
    from .. import links
    return links.link_for("table", sub=sub, id=int(ns_id))


# Le worker au nom duquel l'appel courant écrit (#317) — la SECONDE façon dont le
# titulaire d'un bail s'identifie, quand il écrit hors du run qui tient la ligne.
# Un contextvar plutôt qu'un paramètre porté de surface en surface : l'écriture
# traverse quatre couches, et un argument qu'on oublie de relayer une fois produit un
# refus incompréhensible. Posé par `writing_as`, jamais lu ailleurs qu'au garde-fou.
_WRITING_AS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "datastore_writing_as", default=None)


@contextlib.contextmanager
def writing_as(worker: Optional[str]):
    """Déclare le worker au nom duquel on écrit, le temps d'un appel.

    Sert le cas « le titulaire du bail écrit hors de son run » : sans lui, seul le
    run identifie le titulaire, et un agent qui reprend son travail dans une autre
    session se verrait refuser sa propre ligne.

    ⚠️ **AUCUNE surface ne l'appelle aujourd'hui** (vérifié le 05/09/2026 : zéro
    appelant dans `oto_mcp/`, trois fichiers de tests). Le cas qu'elle décrit est donc
    décrit, pas servi — un agent qui reprend sa ligne depuis une autre session **se
    verra bien refuser**, faute d'un chemin qui pose son worker.

    Ce n'est pas du code mort à retirer : c'est une capacité non branchée, et la
    brancher est une décision de produit (quelle surface déclare le worker, et sur
    quelle foi). Ce qui serait fautif, c'est qu'un texte servi la présente à un agent
    comme une issue disponible : elle ne l'est pas."""
    token = _WRITING_AS.set((worker or "").strip() or None)
    try:
        yield
    finally:
        _WRITING_AS.reset(token)


def indice_de_liberation(issue: dict) -> str:
    """Une phrase par situation — jamais une phrase pour les deux (#517, 29/08).

    Écrite ICI plutôt que sur chaque face : les deux avaient chacune sa formule, et
    toutes deux mêlaient « rien à rendre » et « la ligne est à un autre »."""
    if issue["reason"] == "no_lease":
        return ("aucun bail sur cette ligne — rien à rendre. Ce n'est pas un échec : "
                "la ligne est libre, ton travail peut continuer.")
    bail = issue.get("lease") or {}
    jusqu = str(bail.get("claimed_until") or "?")[:16]
    return (f"bail tenu par `{bail.get('claimed_by')}` jusqu'à {jusqu} — la ligne ne "
            "t'appartient pas. Réserve-en une autre avec `data_claim_next`.")


CLAIMED_REF = "@claimed"


def est_ref_reservation(id_: object) -> bool:
    """`@claimed` — « la ligne que je tiens », au lieu de ses trente-deux caractères.

    ⚠️ Égalité EXACTE, jamais un préfixe ni une tolérance. Un identifiant qui commence
    par « @ » sans être celui-là part tel quel et échoue comme avant : **deviner ce que
    l'agent voulait dire sur un nom d'adresse est exactement la classe de faute que cet
    alias supprime.** Un alias qui pardonne les approximations les encouragerait —
    `@claim`, `@claimed-2`, `@ma_ligne` — et on aurait remplacé une chaîne à recopier
    par une grammaire à deviner."""
    return id_ == CLAIMED_REF


def _backquote(noms) -> str:
    """`a`, `b` — une liste de noms rendue lisible au milieu d'un refus."""
    return ", ".join(f"`{n}`" for n in noms)


def _refus_run_clos(ou: str, quand: object) -> ClaimedRefUnresolved:
    """Le refus quand le travail est CLOS — un MOMENT, pas un état (#645).

    Huitième passage, 30/08 : 99 refus sur 200 écritures, tous « ton travail ne tient
    aucune ligne en ce moment ». Exact, et à côté de la question — les appels venaient
    d'un harnais qui écrivait APRÈS `run_finish`, dont la clôture avait justement
    libéré les baux. Le refus décrivait l'état constaté ; ce qu'il fallait dire est
    **quand** la porte s'est fermée, parce que rien dans le nom de l'alias ni dans sa
    description ne disait qu'il en avait une. Deux heures perdues, sur un mécanisme
    découvert deux fois à douze heures d'écart.

    > **Un refus juste qui n'est pas le bon refus coûte autant qu'un refus faux** : il
    > envoie chercher une réservation oubliée là où c'est l'ordre des gestes qui est en
    > cause.

    L'heure y est parce que c'est elle qui fait le lien avec le geste précédent :
    « depuis 21:08:53 » se reconnaît dans un journal, « clos » ne se reconnaît pas.
    Et la sortie ne prescrit **aucun outil de plus** : l'identifiant de la ligne est
    une valeur que l'appelant a déjà reçue (`docs/conventions.md`, la règle #613/#632)."""
    return ClaimedRefUnresolved(
        f"`{CLAIMED_REF}`{ou} : ton travail est CLOS depuis {str(quand)[:19]} "
        "(`run_finish`), et sa clôture a libéré toutes ses lignes — l'alias ne désigne "
        "une ligne que TANT QUE le travail est ouvert, jamais après. Il n'y a plus rien "
        "à écrire sous cette adresse : si une écriture restait due, vise la ligne par "
        "l'identifiant que `data_claim_next` t'avait rendu.")


def _refus_rien_tenu(ou: str = "", *, run: Optional[str] = None) -> ClaimedRefUnresolved:
    """Le refus « ton travail ne tient rien » — et ce qu'il ne dit PLUS.

    Le 29/08 à 15:24, il finissait par « ou écris avec un identifiant explicite ». Le
    travail venait de recevoir `row: null` (fin de file : la dernière ligne était sous
    le bail d'un pair, écrite 71 ms plus tard) ; l'agent a pris la phrase au mot et a
    fabriqué un identifiant sur le gabarit du refus suivant. Le cas NORMAL derrière ce
    refus est la fin de file, pas une réservation oubliée : on le nomme, et la conduite
    est de ne rien écrire — une invitation à fournir un identifiant, ici, est une
    invitation à l'inventer.

    ⚠️ **Sauf si le travail est CLOS** : la phrase ci-dessous serait alors vraie et
    inutile (#645). On paie une requête pour le savoir, ici et nulle part ailleurs —
    c'est un chemin d'échec, le nominal résout un bail sans y passer. `run=None` (hors
    run) garde le texte de fin de file : sans run, il n'y a pas de clôture à raconter,
    et c'est un autre refus, plus haut, qui parle.

    ⚠️ **Et cette requête ne peut pas emporter le refus.** On est ici pour RENDRE une
    conduite à l'agent (`_adresse_reservee` : « une erreur interne l'effacerait au
    moment précis où elle sert ») ; une lecture de journal qui casse doit coûter la
    précision du message, jamais le message. L'échec est journalisé — la dégradation
    se voit — et on retombe sur le texte de fin de file, qui reste un refus juste."""
    if run:
        try:
            clos = db.run_closed_at(run)
        except Exception:  # noqa: BLE001 — le refus prime sur sa propre précision
            logger.warning("clôture du run %s illisible : le refus `%s` retombe sur "
                           "le texte de fin de file", run, CLAIMED_REF, exc_info=True)
            clos = None
        if clos is not None:
            return _refus_run_clos(ou, clos)
    return ClaimedRefUnresolved(
        f"`{CLAIMED_REF}`{ou} : ton travail ne tient aucune ligne en ce moment (aucune "
        "réservation active). Si `data_claim_next` t'a rendu `row: null`, la file est "
        "vide pour ton filtre : il n'y a rien à écrire — n'invente pas d'identifiant, "
        "termine ton travail (`run_finish`). Sinon, réserve une ligne avec "
        "`data_claim_next` avant d'écrire.")


def _current_run() -> Optional[str]:
    """Le run de l'appel courant, ou None hors de tout run.

    Lu ICI et pas passé par les surfaces : le run est un CONTEXTE d'appel (ADR 0038),
    pas un argument métier — l'exiger des surfaces reviendrait à demander à chaque
    appelant de déclarer ce que le serveur sait déjà, et à l'oublier une fois sur
    deux."""
    from .. import session_org
    try:
        return session_org.current_call_run()
    # noqa: SILENT — hors run : pas de corrélation d'exécution à poser
    except Exception:      # noqa: BLE001 — hors contexte de requête (script, test)
        return None


def _refus_de_creation(namespace: str, key: str,
                       value: Any = None) -> BusinessKeyRequired:
    """Le refus d'une CRÉATION sur un tableau fermé (`key_required`, #516).

    Deux formes, parce que les deux gestes qui l'atteignent sont différents — et que
    dire « clé requise » à qui vient d'en fournir une le ferait chercher longtemps :

    - **la clé n'est pas renseignée** : le geste du 28/08, une ligne née sans `siren`
      sur un tableau qui en déclare un ;
    - **la clé ne désigne aucune ligne** : le geste du 29/08, un SIREN inconnu qui a
      fabriqué une entreprise fictive. La valeur refusée est DITE — sans elle, il
      reste à deviner si c'est la valeur ou le tableau qui est en cause.

    Les deux nomment la clé ET les DEUX gestes de sortie : viser la ligne par son
    identifiant, et — si la ligne doit vraiment naître — lever le cran. Un refus qui
    ne dit que « non » fait deviner (cf. `errors.py`).

    ⚠️ **La seconde sortie n'est pas un confort** (#668, 02/09/2026). « Vise-la par
    son identifiant » est vrai et IMPRATICABLE dans le cas qui déclenche le refus :
    la ligne n'existe pas, et sur un tableau fermé encore vide il n'y a aucun `_id`
    à viser — le tableau ne peut alors plus recevoir sa première ligne par aucune
    écriture. Le journal a daté les deux moitiés du coût, sur la MÊME procédure de
    journalisation : le 01/09 (run `493e624c…`) l'agent refusé relit le schéma,
    trouve seul la manœuvre — ouvrir, écrire, refermer — et pose les 47 lignes du
    tableau ; le 02/09 (run `a2da6c1e…`) un autre passage ne la retrouve pas, essaie
    les trois formes d'écriture, et s'arrête sur 19 lignes non journalisées. La
    sortie existait, documentée dans `docs/datastore.md` et dans la description de
    `data_patch_schema` — nulle part où la lit celui qui vient d'être refusé.

    Le cran ne bouge pas pour autant : la sortie passe par le SCHÉMA, jamais par un
    paramètre « forcer » sur l'écriture (#516 — un bouton force devient un réflexe,
    et le cran redevient une étiquette)."""
    ferme = ("ce tableau n'accepte que les écritures qui visent une ligne EXISTANTE "
             "(`key_required`) — rien n'a été créé.")
    sortie = ("Vise-la par son identifiant : data_write(id=…, row={…}) — son `_id` "
              "est rendu par data_rows et data_claim_next.")
    naissance = (
        f"Si cette ligne doit VRAIMENT naître, c'est une décision de SCHÉMA et pas "
        f"d'écriture — le cran a été posé sur ce tableau : "
        f"data_patch_schema(namespace='{namespace}', key_required=false), ton "
        f"écriture, puis data_patch_schema(namespace='{namespace}', "
        f"key_required=true) pour refermer.")
    if value is None or str(value) == "":
        return BusinessKeyRequired(
            f"`{key}`, la clé métier de `{namespace}`, n'est pas renseigné : {ferme} "
            f"{sortie} Sinon renseigne `{key}` avec la valeur que porte la ligne "
            f"visée. {naissance}",
            key=key, namespace=namespace)
    return BusinessKeyRequired(
        f"aucune ligne de `{namespace}` ne porte `{key}` = {str(value)!r} : {ferme} "
        f"Vérifie la valeur (une clé inventée créerait une ligne que rien ne "
        f"rapproche). {sortie} {naissance}",
        key=key, namespace=namespace, value=value)


class DatastorePg(SchemaOpsMixin):
    """Store tabulaire adossé à PostgreSQL.

    State-less, instancié par requête. Normalement à partir du `sub` (l'acteur user) ;
    ou, pour un endpoint MCP agissant sous une org (`acting_org`, secret opt-in), avec
    `sub=None` — l'autorité est alors l'org propriétaire. Résout chaque namespace en
    `ns_id` (possédé OU partagé) et opère sur `datastore_rows`.
    """

    def __init__(self, sub: Optional[str], *, acting_org: Optional[int] = None,
                 allowed_ns_ids: Optional[set] = None, read_only: bool = False):
        self.sub = sub
        self.acting_org = acting_org
        # Les colonnes dont CET appel a posé la couche `origine` (oto#70 lot 2) : le
        # store est instancié par requête, donc ce set est celui d'un seul appel — et
        # un lot qui écrit vingt lignes n'avertit qu'une fois par colonne.
        self._origine_posee: set = set()
        # Scope dur (endpoint partagé) : None = pas de restriction ; set = ces ns_ids seuls.
        self.allowed_ns_ids: Optional[set] = (None if allowed_ns_ids is None
                                              else {int(x) for x in allowed_ns_ids})
        self.read_only = bool(read_only)
        self._active_scope_cache: Optional[tuple[list[int], list[int]]] = None
        # Relevé des champs écrits HORS SCHÉMA par ce store (#294), union sur un lot :
        # rempli par `_check_row`, lu par les surfaces via `off_schema_report()`. Le
        # store est instancié par requête, donc la portée est celle du geste.
        self.off_schema: set = set()
        self.off_options: dict = {}
        self.off_notices: set = set()
        # Ce que ce geste a VIDÉ (#407/#408/#409) : les colonnes qu'il nomme avec un
        # `null` alors qu'elles portaient quelque chose, et la valeur perdue.
        # Même portée que les relevés ci-dessus (un store par requête), même union
        # sur un lot — mais une LISTE : la ligne fait partie de l'information.
        self.off_erased: list = []
        # #667 : les valeurs que le geste a posées et que le schéma REFUSE, écartées
        # pour que le reste de la ligne s'écrive. Sixième liste, distincte des cinq
        # autres — celles-ci ne sont NI en base NI perdues en silence.
        self.off_rejected: list = []
        # Et ce qu'il aurait vidé sans la règle de #608 : les colonnes qu'il nomme
        # avec un vide non-`null` par-dessus une valeur en place. Liste SÉPARÉE
        # d'`off_erased` — l'une nomme ce qui n'est plus, l'autre ce qui est resté.
        self.off_ignored: list = []
        # #658 : les colonnes VERROUILLÉES que ce geste a remplacées de force, avec
        # la ligne et la valeur d'avant. Même portée que les relevés ci-dessus (un
        # store par requête). Deux lecteurs, parce qu'il y a deux journaux : la face
        # MCP le verse au relevé d'appel (`note_call_trace`, no-op ailleurs), la face
        # REST le lit ici pour sa propre ligne (`datastore_journal.record`).
        self.off_forced: list = []

    # --- résolution namespace -> ns_id ---------------------------------------

    def _active_scope(self) -> tuple[list[int], list[int]]:
        """Contexte de l'ORG ACTIVE (ADR 0023) : `([org active], [mes groupes dans cette
        org])`. La résolution par NOM scope là-dessus — comme `list_namespaces` — de sorte
        qu'un namespace d'une AUTRE de mes orgs ne se résout plus hors de son org (fuite
        cross-org, symétrique au fix projets). L'ownership PERSO (`owner=user`) et les
        grants perso (`principal user`) suivent l'acteur : ils n'appartiennent à aucune
        org, donc ne sont pas une fuite d'org — `resolve_datastore_ns` les garde via `sub`."""
        if self._active_scope_cache is None:
            if self.acting_org is not None:
                # Endpoint agissant-org (sub-less) : contexte = l'org propriétaire seule,
                # aucun groupe (pas de membre → pas de scope de groupe).
                self._active_scope_cache = ([int(self.acting_org)], [])
                return self._active_scope_cache
            from .. import access, group_store
            oid = access.current_org(self.sub)
            if oid is None:
                self._active_scope_cache = ([], [])
            else:
                org = int(oid)
                # ADR 0049 (cadrage 10/07) : les groupes du contexte = mes équipes dans
                # l'org active — ou TOUS les groupes de l'org pour un org_admin (même
                # escalade que `roles.can_read_group`, alignée sur `oto_project op=list`).
                from .. import roles
                if roles.is_org_admin(self.sub, org):
                    groups = [int(g["id"]) for g in group_store.list_groups(org)]
                else:
                    groups = [int(g["group_id"])
                              for g in group_store.list_groups_for_user(self.sub, org)]
                self._active_scope_cache = ([org], groups)
        return self._active_scope_cache

    def _resolve(self, namespace: str, *, write: bool = False) -> int:
        """ns_id d'un namespace VISIBLE DANS L'ORG ACTIVE (possédé par elle, perso, ou
        accordé à son contexte). `write=True` exige le droit d'écriture via
        `ownership.can_access`."""
        org_ids, group_ids = self._active_scope()
        ns = db.resolve_datastore_ns(
            namespace, sub=self.sub, org_ids=org_ids, group_ids=group_ids)
        if not ns:
            # #631 : le run sait où il travaille — sa réservation porte le tableau.
            ns = hors_org.tenu_par_le_run(self.sub, namespace)
        if not ns:
            raise NamespaceNotFound(namespace, indice=hors_org.indice_autre_org(
                self.sub, namespace, org_ids[0] if org_ids else None))
        ns_id = int(ns["id"])
        # Scope dur d'endpoint partagé : hors des tableaux liés au projet ⇒ invisible
        # (anti-fuite #193 ; NamespaceNotFound plutôt que Forbidden — on ne divulgue pas
        # l'existence d'un namespace hors périmètre).
        if self.allowed_ns_ids is not None and ns_id not in self.allowed_ns_ids:
            raise NamespaceNotFound(namespace)
        if write and self.read_only:
            raise NamespaceReadOnly(namespace)
        if write:
            ok = (ownership.org_can_access(self.acting_org, "datastore_namespace",
                                           str(ns_id), "write")
                  if self.acting_org is not None
                  else ownership.can_access(self.sub, "datastore_namespace",
                                            str(ns_id), "write"))
            if not ok:
                raise NamespaceReadOnly(namespace)
        # Le journal cite l'ENTITÉ, pas la chaîne tapée : `data_write("leads-clients")`,
        # `data_write("160")` et `data_write("slot:vivier")` visent le même tableau.
        # Consigné APRÈS les gardes (un namespace refusé ne laisse pas de trace) ;
        # no-op hors appel MCP — la face REST tient déjà son propre relevé.
        session_org.note_call_trace(ns_id=ns_id, ns_name=ns.get("namespace"))
        return ns_id

    @staticmethod
    def _row_to_dict(row: dict, schema: Optional[dict] = None, *,
                     bail_echu: str = "taire", layers: str = dsl.DEFAUT) -> dict:
        """Ligne `datastore_rows` → row API (`_id`/`_created_at`/`_updated_at` à
        plat + champs user). Le bail de claim (ADR 0046 D) n'apparaît que s'il est
        posé (une ligne libre n'a aucune des trois clés `_claimed_*` → absentes,
        pas None).

        `layers` (oto#53) : la forme des cellules à couches — `flat` (défaut) les
        aplatit à côté du nom nu, `nested` les rend comme elles s'écrivent. Le défaut
        vit dans `layers.DEFAUT`, pas ici."""
        data = row.get("data") or {}
        out = {
            "_id": row["row_id"],
            "_created_at": row["created_at"],
            "_updated_at": row["updated_at"],
        }
        # Toute colonne a des sous-champs (#318) — c'est le contrat du datastore, pas
        # une forme que certaines valeurs adoptent. Une colonne « plate » est une
        # colonne dont les sous-champs sont VIDES, et on ne rend pas du vide.
        #
        # Le NOM NU rend donc toujours la valeur : un lecteur qui fait `row["email"]`
        # reçoit un e-mail, qu'il y ait une provenance ou non. Les sous-champs
        # renseignés s'ajoutent à plat sous `champ.couche` — visibles sans être
        # imposés, et projetables par `fields` comme n'importe quelle colonne.
        for k, v in data.items():
            if k in _META_COLS:
                continue
            # `layers="nested"` (oto#53) : la cellule revient comme elle s'écrit,
            # `{valeur, origine, comment, link}` — rien n'est aplati à côté.
            if layers == dsl.NESTED:
                out[k] = dsl.nested_value(v)
                continue
            # `served_value` descend dans une colonne-tableau : chaque attribut d'item
            # est une feuille, rendue comme telle (oto#22 §1).
            out[k] = dsv2.served_value(v)
            # Les couches s'exposent dès qu'il y en a — même sans `valeur` posée
            # (import de socle sur un champ pas encore renseigné).
            out.update(dsv2.flat_layers(k, v))
        # Double-service d'une migration (oto#22 §6) : les anciens noms plats sont
        # SERVIS, calculés depuis la colonne-tableau et jamais stockés — deux vérités
        # à réconcilier sinon. L'ordre des rangs est celui de la liste, qui est un
        # CONTRAT : un écran affiche « le premier contact » comme cible d'appel, et un
        # ordre instable ferait appeler quelqu'un d'autre entre deux ouvertures.
        #
        # Les couches suivent sans rien de plus : l'item est déjà servi, donc son
        # `email.origine` devient `contact1_email.origine`.
        for cle, gabarit in dsv2.flat_alias_of(schema).items():
            items = out.get(cle)
            if not isinstance(items, list):
                continue
            for rang, item in enumerate(items):
                if isinstance(item, dict):
                    for attr, val in item.items():
                        out[dsv2.flat_name(gabarit, rang, attr)] = val
        # ⚠️ Un bail EXPIRÉ n'est pas une réservation — mesuré le 01/09/2026 sur un
        # fichier de production : **495 lignes sur 8 910 portaient `_claimed_by`, et
        # les 495 étaient expirées**, la plus ancienne depuis dix-huit jours, au nom
        # de travailleurs d'une campagne close.
        #
        # La garde le savait déjà (`datastore_active_lease` : « expiré compte pour
        # libre ») ; la lecture, non — elle servait le nom d'un travailleur mort
        # comme une réservation en cours. **Deux lectures voisines de la même donnée,
        # une seule connaissait la règle.**
        #
        # **C'est POSTGRESQL qui tranche, et c'est le fond du correctif.** La
        # fraîcheur arrive en colonne calculée (`claim_active`), du même prédicat et
        # sur la même horloge que la garde : la lecture et la garde ne se ressemblent
        # pas, elles PARTAGENT la règle. Une comparaison refaite en Python serait une
        # SECONDE implémentation — et elle était fausse en germe : comparer les
        # horodatages en TEXTE n'est juste que tant que `_normalize_value` émet un
        # séparateur espace sans fuseau. Le jour où un chemin rendrait un `T`
        # (0x54 > 0x20), tout bail se serait lu ACTIF, en silence et sans rien rougir.
        #
        # Lu par CLÉ, comme `claimed_run` : un SELECT qui oublierait la fraîcheur doit
        # LEVER, jamais servir un bail mort comme s'il courait encore.
        if row.get("claimed_by") is not None:
            actif = row["claim_active"]
            # `bail_echu="servir"` — l'EXCEPTION, et elle est unique : la file de
            # supervision (`DatastorePg.queue`). Son contrat, écrit à trois endroits
            # et ANTÉRIEUR à ce lot, est de rendre le bail « actif OU expiré, le
            # consommateur tranche sur `_claimed_until` ». Neutraliser le bail ici
            # pour tout le monde lui retirerait ce sur quoi trancher : l'écran
            # compterait les lignes mortes « sous bail » avec un compteur d'échus à
            # zéro, et le bouton « Libérer » — gaté sur `_claimed_by` — disparaîtrait
            # sur les lignes qu'il faut justement libérer. Le défaut vaut mieux SÛR :
            # un chemin de lecture neuf tait le bail mort sans avoir à y penser.
            if bail_echu == "servir" or actif:
                out["_claimed_by"] = row["claimed_by"]
                out["_claimed_until"] = row.get("claimed_until")
                # LE RUN qui tient ce bail — ce qui lie un travail à la LIGNE qu'il
                # travaille. `null` = bail pris SANS run (une personne sur la file du
                # dashboard) : un fait, pas un trou.
                out["_claimed_run"] = row["claimed_run"]
        # Ce que la file sait de la ligne (#433). Rendus SEULEMENT s'ils portent
        # quelque chose : un `_claims: 0` sur chaque ligne de chaque tableau serait
        # du bruit dans toutes les lectures, pour une file que la plupart n'ouvrent
        # jamais. Voir « déjà tentée 2 fois » est ce qui change une décision.
        if row.get("claims"):
            out["_claims"] = row["claims"]
        if row.get("abandon_reason"):
            out["_abandon"] = row["abandon_reason"]
        return out

    # --- schéma v2 : validation d'écriture + cycle de vie (ADR 0046) ---------

    def _ns_of(self, ns_id: int) -> dict:
        """La ligne `user_datastores` (nom canonique + schéma + propriétaire)."""
        return db.get_datastore_namespace_by_id(ns_id) or {}

    def _schema_of(self, ns_id: int) -> Optional[dict]:
        return self._ns_of(ns_id).get("schema")

    @staticmethod
    def _order_health(ns_id: int, order_by, otype, oopts, q, filters) -> Optional[dict]:
        """Compteur d'écart d'un tri typé (#336) — présent SEULEMENT quand il y a
        un écart : prévenir là où tout est conforme ferait cesser de lire
        l'avertissement là où il compte (la leçon de l'avertissement scout)."""
        if not otype:
            return None
        health = db.datastore_order_health(
            ns_id, order_by=order_by, order_type=otype, order_options=oopts,
            q=q, filters=filters)
        return health if (health["off_type"] or health["empty"]) else None

    def _trace(self, trace: Optional[dict], ns_id: int, ns: dict,
               *, prev_status: Any = None) -> None:
        """RELEVÉ du geste pour le journal (seam ADR 0046 b4) : ce que la surface REST
        doit savoir, pris DANS la mutation qui l'a déjà calculé.

        ⚠️ `prev_status` **doit** venir d'ici et pas d'une relecture séparée : c'est
        l'état sur lequel la transition a été VALIDÉE. Une relecture faite avant
        l'appel court avec un write concurrent (un agent qui bouge la ligne entre
        les deux) et ferait proposer au cockpit une annulation vers un état que la
        ligne n'a jamais eu. Bénéfice second : zéro requête ajoutée (D2)."""
        if trace is None:
            return
        schema = ns.get("schema")
        trace.update({
            "ns_id": int(ns_id),
            "namespace": ns.get("namespace"),
            "status_key": (dsv2.status_field(schema) or {}).get("key"),
            "title_key": (dsv2.title_field(schema) or {}).get("key"),
            "prev_status": prev_status,
        })

    @staticmethod
    def _declared_key_of(schema: Optional[dict]) -> Optional[str]:
        k = (schema or {}).get("key")
        return k if isinstance(k, str) and k else None

    def _colonnes_de_la_ligne_visee(self, ns_id: int, schema: Optional[dict],
                                    user_data: dict,
                                    key: Optional[str] = None) -> set:
        """Les colonnes DÉJÀ en place sur la ligne que cette écriture vise, si elle en
        vise une par sa clé métier. Sinon l'ensemble vide.

        ⚠️ **Appelée PARESSEUSEMENT** (`ranger_les_couches(colonnes_en_place=…)`) : seul
        un nom pointé encore irrésolu la déclenche. Le chemin nominal — l'immense
        majorité des écritures, dont les lots de huit mille lignes — ne paie donc aucun
        aller-retour SQL de plus. Deux clés sont interrogées, la clé explicite du lot
        puis la clé DÉCLARÉE, parce qu'un lot peut dédoubler sur une autre que celle
        qui porte l'index."""
        vues = []
        for k in (key, self._declared_key_of(schema)):
            if not k or k in vues:
                continue
            vues.append(k)
            v = user_data.get(k)
            if v is None or str(v) == "":
                continue
            rid = db.datastore_find_row_id_by_key(ns_id, k, v)
            if rid is not None:
                return set((db.datastore_get_row(ns_id, rid) or {}).get("data") or {})
        return set()

    @staticmethod
    def _pose_systeme(schema: Optional[dict]) -> dict:
        """Ce que la plateforme pose sur CE geste (#607) — `{}` quand le tableau ne
        déclare aucune colonne `system:`, donc rien à payer pour qui ne s'en sert pas.

        Calculée UNE fois par geste et passée aux deux temps qui en ont besoin (le
        refus, puis la pose) : deux calculs encadrant la même écriture donneraient
        deux horodatages, et l'appelant se ferait refuser une valeur identique à
        celle qu'on venait de lui rendre — un refus impossible à comprendre et
        impossible à reproduire."""
        return valeurs_systeme(schema, run=_current_run(), maintenant=_now_iso())

    # --- forçage d'une colonne verrouillée (#658) -----------------------------

    def _forcage_readonly(self, ns_id: int, schema: Optional[dict],
                          demande: bool) -> Optional[Forcage]:
        """Le forçage de CET appel — `None` quand rien ne le demande.

        ⚠️ Le palier est tranché ICI, **une fois par appel et hors de toute
        transaction**. Il coûte une lecture d'ownership : l'évaluer dans le `_apply`
        du verrou de ligne prendrait une seconde connexion du pool pendant qu'on tient
        un `FOR UPDATE` — la forme exacte du gel de production du 02/09/2026.

        Deux courts-circuits avant la moindre requête, donc **zéro SQL de plus sur le
        chemin nominal** : personne ne demande le forçage, ou le tableau ne déclare
        aucune colonne verrouillée — il n'y a alors rien à forcer, et le demander ne
        doit rien coûter."""
        if not demande:
            return None
        if not dsv2.readonly_fields(schema):
            return Forcage(demande=True, autorise=False)
        return Forcage(demande=True, autorise=self._peut_forcer(ns_id))

    def _peut_forcer(self, ns_id: int) -> bool:
        """Le PALIER : propriétaire du tableau ∪ qui le gouverne. L'un des deux suffit.

        Les deux ensembles se croisent sans s'inclure — un membre de l'org
        propriétaire POSSÈDE sans gouverner, un gérant (`role='manager'`, ADR 0048)
        GOUVERNE sans posséder — d'où l'union, et non l'un des deux seul.

        ⚠️ Ce qui reste dehors est exactement ce qui doit rester dehors : le tiers à
        qui le tableau a été PARTAGÉ en écriture (`data_share`, permission `write`).
        Il écrit — c'est le droit qu'on lui a donné — et il ne force pas. Sinon le
        verrou ne protégerait de personne : quiconque peut écrire pourrait le lever,
        ce qui est la définition d'une colonne ouverte.

        Endpoint agissant-org (sub-less) : pas de gouvernance par cette porte —
        `_entry` pose déjà la même règle — reste l'owner-match de l'org elle-même."""
        if self.acting_org is not None:
            owner = ownership.owner_of("datastore_namespace", str(ns_id))
            return (owner is not None
                    and (str(owner[0]), str(owner[1])) == ("org", str(self.acting_org)))
        if not self.sub:
            return False
        return (ownership.owns(self.sub, "datastore_namespace", str(ns_id))
                or ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)))

    def _relever_forcage(self, forcage: Optional[Forcage],
                         row_id: Optional[str]) -> None:
        """Agrafe la ligne aux substitutions, puis les verse aux DEUX journaux.

        Appelée seulement quand l'écriture a ABOUTI : un geste refusé n'a rien forcé,
        et le journaliser ferait chercher une valeur qui n'a pas bougé.

        Deux dépôts parce qu'il y a deux faces et deux journaux : `note_call_trace`
        pour la face MCP (versé dans les `args` de la ligne `tool_calls` par
        `server._calllog_sink`, via l'allowlist `_TRACED_ARGS` ; no-op hors appel
        MCP), et `self.off_forced` que la face REST relit pour SA ligne. Le « qui »
        n'est répété ni dans l'un ni dans l'autre : les deux journaux stampent déjà
        le `sub` et l'org de l'appelant."""
        if forcage is None or not forcage.forcees:
            return
        forcage.rattacher(row_id)
        releve = forcage.releve()
        if not releve:
            return
        self.off_forced = list(releve)
        session_org.note_call_trace(readonly_forced=list(releve))

    def _ecarter(self, schema: Optional[dict], merged: dict, errors: list,
                 hors: list, *, prev_status=None,
                 written: Optional[set] = None) -> Optional[list]:
        """Écarte les valeurs hors options et rend leur relevé — ou None pour
        refuser tout, comme avant (#667).

        Trois conditions, et chacune ferme un trou :

        1. **Les refus sont TOUS des valeurs hors options.** Un requis manquant ou
           une transition interdite portent sur la COHÉRENCE de la ligne : les
           écarter écrirait une fiche fausse. Le partage est là, pas ailleurs.
        2. **Chaque champ fautif est posé par CE geste.** Un patch ne se fait pas
           amputer d'une valeur qu'il n'a pas écrite : ce serait un effacement
           silencieux de la base, exactement ce que `valeurs_effacees` existe pour
           empêcher. Le cran juge le geste, pas le passé qu'il hérite.
        3. **La ligne amputée REPASSE la validation entière.** Retirer une valeur
           peut en défaire une autre — une colonne-aiguillage écartée cesse de
           rendre requis ce qu'elle gardait. Sans ce second tour, on écrirait une
           ligne incomplète sur la foi d'un contrôle qui n'a pas vu sa forme
           finale. Elle échoue ⇒ on refuse tout, avec le message d'origine.

        ⚠️ L'amputation se joue d'abord sur une COPIE : `merged` n'est touché que
        si le second tour est propre. Un écartement à moitié appliqué sur un
        refus final laisserait l'appelant avec un dict trafiqué et une exception.
        """
        if not hors or len(hors) != len(errors):
            return None
        # Une valeur dont le schéma déclare la DESTINATION est mal rangée, pas
        # indésirable (#545/#667) : le refus dit où l'écrire, et 27 agents sur 27
        # se corrigent. L'écarter écrirait une fiche qui prétend ne pas avoir été
        # qualifiée, sous un `ok: true` — corrompre en silence, pas sauver.
        if any(h.get("destination") for h in hors):
            return None
        essai = copy.deepcopy(merged)
        releve: list = []
        for h in hors:
            champ = str(h.get("champ") or "")
            tete = dsec.tete(champ)
            if not tete or (written is not None and tete not in written):
                return None
            if not dsec.retirer(essai, champ):
                return None
            options = ", ".join(str(o) for o in (h.get("options") or []))
            releve.append({
                "champ": champ,
                "motif": f"valeur hors options ({options})",
                "valeur_rejetee": h.get("valeur"),
            })
        # Rien à sauver ⇒ rien à écarter. Quand la valeur fautive est TOUT ce que
        # le geste pose, l'amputer ne sauve pas une fiche : elle en crée une VIDE,
        # sous un `ok`. Le motif de ce lot est de préserver un travail déjà fait —
        # là où il n'y en a pas, le refus reste la bonne réponse, et c'est ce que
        # le banc du régime strict (#319) a rappelé.
        reste = (essai if written is None
                 else {k: v for k, v in essai.items() if k in written})
        if not any(not dsv2.est_vide(v) for v in reste.values()):
            return None
        if dsv2.validate_row(schema, essai, prev_status=prev_status,
                             written=written):
            return None
        for h in hors:
            dsec.retirer(merged, str(h.get("champ") or ""))
        return releve

    def _check_row(self, schema: Optional[dict], merged: dict, *,
                   prev_status=None, written: Optional[set] = None) -> None:
        """Valide la row TELLE QU'ÉCRITE (résultat mergé). No-op si le schéma ne
        déclare ni strict/required/max_length ni lifecycle (défaut 0016 soft).

        `written` = les clés que le geste réécrit (None sur un insert/remplacement,
        où tout est écrit) : borne `max_length` restreinte à celles-là, cf.
        `dsv2.validate_row`.

        C'est aussi LE seam d'écriture — tous les chemins (append, batch, merge de
        clé métier, upsert, patch) y passent — donc l'endroit unique où relever les
        champs HORS SCHÉMA du geste (#294), sur les seules clés posées. Un schéma
        `strict` active la validation, donc l'appel a bien lieu."""
        # #545 : le refus STRUCTURÉ se remplit PENDANT la validation — c'est le seul
        # endroit qui voit à la fois la colonne fautive et la colonne attendue. Le
        # récupérer après coup imposerait de reparser le message, ce que la face REST
        # ne doit jamais avoir à faire.
        details: dict = {}
        hors: list = []
        errors = dsv2.validate_row(schema, merged, prev_status=prev_status,
                                   written=written, details=details, hors=hors)
        if errors:
            # #667 : une valeur hors options s'ÉCARTE, la fiche s'écrit. Tout autre
            # refus — et toute combinaison avec un autre refus — retombe ici.
            ecartes = self._ecarter(schema, merged, errors, hors,
                                    prev_status=prev_status, written=written)
            if ecartes is None:
                raise RowValidationError(errors, details=details)  # rien à relever
            self.off_rejected.extend(ecartes)
        posed = merged if written is None else {k: merged[k] for k in written
                                                if k in merged}
        # #354 : un `id` NU posé par le geste, qu'aucun field ne déclare, est un
        # identifiant de ligne égaré — pas une donnée. Écrit comme donnée, il a
        # produit des lignes fantômes (4 en une nuit de flotte : l'agent recopie
        # l'`id` que le claim lui a servi, la fusion ne matche rien, une ligne
        # SANS clé métier naît avec tout l'enrichissement). Reconnaissance par
        # DÉCLARATION : une vraie colonne `id` (CSV importé) se déclare au schéma
        # et passe ; sans déclaration, refus nommé — jamais une devinette, jamais
        # un silence. Posé ICI parce que ce seam voit TOUS les chemins d'écriture.
        if "id" in posed and not dsv2.declares_field(schema, "id"):
            raise ValueError(
                f"`id` ({posed['id']!r}) posé dans `row` sans être une colonne "
                "déclarée du tableau : un identifiant de ligne ne s'écrit pas "
                "comme une donnée — l'écriture viserait à côté (ligne fantôme). "
                "Pour cibler une ligne : garde son `_id` tel que servi dans la "
                "ligne, ou passe le paramètre id=. Si `id` est une vraie colonne "
                "de TES données, déclare-la au schéma (data_set_schema) puis "
                "réécris.")
        # #614/#678 : le TROISIÈME état de `strict` au premier niveau — refuser la
        # colonne non déclarée, opt-in table par table (`unknown_fields: "reject"`).
        #
        # Posé ICI, contre `posed`, et les deux points comptent autant que le refus :
        #   • ici, parce que c'est le seam qui calcule DÉJÀ le relevé, deux lignes
        #     plus bas. Le rapporteur et le refuseur partagent donc le prédicat
        #     (`_unknown_subkeys`), pas seulement l'intention — la divergence entre
        #     un signal et un refus qui se veulent d'accord se paie chez l'appelant ;
        #   • contre `posed`, jamais contre la ligne mergée : un tableau de
        #     production porte 162 colonnes hors schéma accumulées avant la pose du
        #     cran. Les juger rendrait la ligne inécrivable pour un patch sans
        #     rapport — la faute de #284, sur une autre règle. Le cran juge le GESTE,
        #     pas le passé qu'il hérite.
        #
        # Après le refus de l'`id` nu, qui est plus spécifique et plus utile : sur un
        # tableau fermé, `id` serait sinon rendu comme une colonne inventée de plus.
        hs_errors, hs_details = dsv2.off_schema_refusal(schema, posed)
        if hs_errors:
            raise RowValidationError(hs_errors, details=hs_details)
        self.off_schema.update(dsv2.off_schema_keys(schema, posed))
        # Valeurs hors des options DÉCLARÉES quand rien ne les fait respecter (#319) :
        # écrites quand même — le tableau est en régime souple — mais plus en silence.
        # Vide dès que la validation est armée : là, `validate_row` ci-dessus a déjà
        # refusé, et le redire serait un doublon sur un chemin qui ne passe pas.
        self.off_options.update(dsv2.unenforced_options(schema, posed))

    @staticmethod
    def _reject_misplaced_id(data: dict, row_id: Optional[str], *,
                             batch: bool = False) -> None:
        """REFUSE un `_id` posé DANS le payload au lieu du paramètre `id` (#390).

        `_id` est géré par le datastore : il vit dans la colonne `row_id`, jamais
        dans le blob. Il était donc filtré des données écrites — en SILENCE, et c'est
        ce silence qui coûte : une écriture `row={"_id": "019f…", "statut": …}` sans
        `id=` a INSÉRÉ une ligne neuve portant tout le travail d'un enrichissement,
        la ligne visée restant vide, sans une erreur. 28 champs repris à la main.

        Refuser ne casse aucun appelant légitime : personne n'écrit `_id` comme
        DONNÉE, puisque le faire n'avait déjà aucun effet. Un `_id` cohérent avec le
        `id=` fourni passe en revanche — c'est le round-trip normal (relire une ligne
        entière, la modifier, la repousser), et le refuser n'apprendrait rien à
        personne. Les autres colonnes de plateforme (`_created_at`, `_claimed_by`…)
        restent ignorées sans bruit pour la même raison : leur présence dans un
        round-trip est bénigne, elles ne DÉSIGNENT pas la cible de l'écriture."""
        if not isinstance(data, dict) or "_id" not in data:
            return
        posed = data.get("_id")
        if row_id is not None and str(posed) == str(row_id):
            return   # round-trip cohérent : l'intention est claire
        if batch:
            raise ValueError(
                f"`_id` ({posed!r}) dans une row du LOT : un batch dédouble par clé "
                "métier (`key`), il ne cible pas une ligne par son `_id`. Pour "
                "modifier UNE ligne précise, appelle data_write(id=…, row={…}) ; "
                "pour un lot, déclare la clé métier et laisse-la dédoublonner.")
        if row_id is None:
            # Chemin normalement inatteignable depuis append_row depuis #354 (la
            # promotion capte `_id` en amont et route vers update) — conservé en
            # défense en profondeur pour tout futur appelant.
            raise ValueError(
                f"`_id` ({posed!r}) posé DANS `row` : il y serait ignoré et ton "
                "écriture INSÉRERAIT une nouvelle ligne au lieu de modifier "
                "celle-là. L'identifiant est un paramètre : data_write(id=" +
                f"{posed!r}, row={{…}}).")
        raise ValueError(
            f"`_id` ({posed!r}) dans `row` ne correspond pas au `id` visé "
            f"({row_id!r}) — deux cibles pour une écriture. Retire `_id` du corps : "
            "seul le paramètre `id` désigne la ligne.")

    def off_schema_report(self) -> dict:
        """Le relevé « hors schéma » du geste, prêt à fusionner dans une réponse
        d'écriture : `{}` quand tout est dans le format (le cas normal — pas de clé
        parasite dans la réponse), sinon la liste des champs + la phrase qui dit
        quoi en faire. Union sur un lot : un renommage fautif se voit une fois,
        pas une par row."""
        out: dict = {}
        # oto#70 lot 2, premier temps : l'avertissement part avec CHAQUE écriture qui
        # pose une origine, pas une seule fois. Un écrivain qui repasse sur une ligne
        # par semaine ne verrait jamais un message servi une fois — et c'est précisément
        # ce profil-là que la mesure a trouvé (52 lignes touchées sur une semaine, toutes
        # réécrites après leur création).
        if self._origine_posee:
            out["origine_warning"] = dsv2.avertissement_origine(
                sorted(self._origine_posee))
        keys = sorted(self.off_schema)
        if keys:
            out["hors_schema"] = keys
            out["hors_schema_hint"] = dsv2.off_schema_warning(keys)
        # #319 : les options déclarées mais inertes. Clé DISTINCTE de `hors_schema` —
        # ce n'est pas la même faute : là une colonne inconnue, ici une valeur hors
        # d'une liste que le schéma laissait croire fermée.
        if self.off_options:
            out["hors_options"] = dict(sorted(self.off_options.items()))
            out["hors_options_hint"] = dsv2.unenforced_options_warning(self.off_options)
        # #317 étape B : le changement de comportement, dit à l'instant où il joue.
        # Union sur un lot (comme `hors_schema`) — un batch de 500 lignes finies ne
        # répète pas 500 fois la même phrase.
        if self.off_notices:
            out["notices"] = sorted(self.off_notices)
        # Ce que le geste a VIDÉ (#407/#408/#409). Clé DISTINCTE des précédentes : ce
        # n'est ni une colonne inconnue ni une valeur hors d'une liste, c'est une
        # valeur qui N'EST PLUS — la seule des quatre qui ait détruit quelque chose.
        out.update(effacements_report(self.off_erased))
        # #608 : ce que le geste aurait détruit et qu'on a préservé. CINQUIÈME clé,
        # distincte des quatre autres — les valeurs qu'elle nomme sont ENCORE en
        # base, et c'est toute la différence avec `valeurs_effacees`.
        out.update(ignores_report(self.off_ignored))
        # #667 : SIXIÈME clé — ce que le schéma a refusé et que le geste a écarté
        # pour écrire le reste. Ni en base (à la différence de `hors_options`), ni
        # détruite (à la différence de `valeurs_effacees`) : jamais entrée.
        out.update(dsec.rapport(self.off_rejected))
        return out

    # Le message que voient les tableaux dont l'écriture d'un état final libérait la
    # ligne (#317 étape B). Rendu à l'INSTANT où l'ancien comportement aurait joué :
    # c'est le seul moment où l'information est actionnable, et son lecteur est le
    # seul qui puisse agir. Trois autres emplacements ont été écartés — à la pose du
    # schéma (les tableaux concernés ne le reposent pas, le message n'arriverait
    # jamais), au claim (trop tôt, et relu à chaque ligne), une annonce hors produit
    # (hors du geste, donc oubliée).
    #
    # ⚠️ La promesse finale est volontairement PLUS PETITE que la première rédaction :
    # elle disait « couvre le cas où votre agent s'arrête en route », ce qui est FAUX
    # — un agent qui meurt n'appelle pas `run_finish`. Ce que la fin de run couvre,
    # c'est l'OUBLI de relâcher. Une promesse doit suivre le code, pas l'inverse.
    _TERMINAL_RELEASE_RETIRED = (
        "La ligne reste réservée. Écrire un état final ne libère plus la ligne "
        "automatiquement — ce comportement a été retiré. Ce qui la libère "
        "maintenant : la fin du traitement en cours (`run_finish`, quelle que soit "
        "son issue) rend toutes les lignes qu'il avait prises ; ou un `data_release` "
        "explicite si vous travaillez hors traitement. Si vous dépendiez de la "
        "libération automatique : appelez `data_release` après avoir écrit l'état "
        "final, ou encadrez votre travail par `run_start` / `run_finish` — la "
        "libération devient alors automatique si vous oubliez de relâcher vos lignes."
    )

    def _terminal_write_notice(self, schema: Optional[dict], ns_id: int, row_id: str,
                               merged: dict) -> None:
        """Dit, UNE fois par écriture concernée, que la libération automatique est
        retirée — et ne libère plus rien.

        Ne parle qu'aux tableaux réellement touchés : un cycle de vie déclaré, un état
        final écrit, ET une ligne effectivement sous bail. Les autres ne voient rien.
        """
        sf = dsv2.status_field(schema)
        if not sf:
            return
        if not dsv2.is_terminal_status(schema, merged.get(sf.get("key"))):
            return
        if db.datastore_active_lease(ns_id, row_id):
            self.off_notices.add(self._TERMINAL_RELEASE_RETIRED)

    # --- namespace lifecycle -------------------------------------------------

    def _entry(self, n: dict, *, shared: bool, permission: Optional[str] = None) -> dict:
        ns_id = int(n["id"])
        perso = (self.sub is not None
                 and n.get("owner_type") == "user" and n.get("owner_id") == self.sub)
        # Agissant-org (sub-less) : pas de gouvernance via l'endpoint (create/delete/
        # rename/share restent réservés à un user identifié).
        can_govern = (False if self.acting_org is not None
                      else ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)))
        return {
            "id": ns_id,
            "namespace": n["namespace"],
            "created_at": n.get("created_at"),
            "url": _ns_url(ns_id, self.sub),
            "shared": shared,
            "owner_type": n.get("owner_type"),
            "owner_id": n.get("owner_id"),
            "permission": permission if shared else "write",
            "can_write": (permission == "write") if shared else True,
            "can_govern": can_govern,
            "is_personal": perso,
            "schema": n.get("schema"),   # mode typé optionnel (ADR 0032 §6 / 0029, B6) ; None = table libre
        }

    def list_namespaces(self) -> list[dict]:
        """Namespaces visibles DANS L'ORG ACTIVE (l'org est le contexte, ADR 0023) :
        possédés par l'org active + accordés à elle ou à MES équipes dans cette org
        (grants d'org/groupe — tous mes groupes de l'org active, pas seulement le
        groupe actif : un partage d'équipe doit se voir sans basculer). Un namespace
        possédé par une AUTRE org — ou partagé à l'acteur *en propre* (grant user,
        cross-org) — ne fuite PLUS dans la vue d'une org tierce (scope décidé le
        2026-07-01). Dédupliqués par id (priorité possédé). La résolution PAR NOM
        (`_resolve`) scope désormais SUR LE MÊME contexte d'org (2026-07-03) : un
        namespace d'une autre org ne se résout plus hors de son org non plus."""
        from .. import access
        if self.acting_org is not None:
            owner = ("org", str(self.acting_org))
            proprios: list = [owner]
        else:
            org = access.current_org(self.sub)
            if ownership.active_owner(org) is None:
                return []
            # ⚠️ `active_org_principals` et non `active_owner` (oto-backend#870,
            # 04/09/2026) : l'org active ET l'acteur. Depuis l'ADR 0068 un tableau créé
            # par un agent naît PERSONNEL — et cette liste ne montrait que l'org, donc
            # le créateur ne voyait pas ce qu'il venait de créer. Il concluait qu'il
            # n'existait pas ; il était pourtant résoluble par nom, et la recherche le
            # voyait. Une écriture sans lecteur, la classe oto#42 exactement.
            # Le jeu reste borné à l'org active : rien de cross-org n'entre par là.
            proprios = ownership.active_org_principals(self.sub, org)
        # ADR 0049 (cadrage 10/07) : les tableaux TEAM-OWNED de l'org active sont listés
        # comme les org-owned. `_active_scope` est la source unique du jeu de groupes
        # (mes équipes, ou TOUS les groupes de l'org pour un org_admin — même règle que
        # `oto_project op=list`) ; le scope reste borné à l'org active.
        org_ids, group_ids = self._active_scope()
        owned = proprios + [("group", str(g)) for g in group_ids
                            if ("group", str(g)) not in proprios]
        out: dict[int, dict] = {}
        for n in db.list_datastore_namespaces_for_owners(owned):
            out[int(n["id"])] = self._entry(n, shared=False)
        for n in db.list_datastore_namespaces_granted_to(self.sub, org_ids, group_ids):
            if int(n["id"]) in out:
                continue
            out[int(n["id"])] = self._entry(n, shared=True, permission=n.get("permission"))
        # Scope dur d'endpoint partagé : ne lister QUE les tableaux liés au projet.
        if self.allowed_ns_ids is not None:
            return [e for e in out.values() if int(e["id"]) in self.allowed_ns_ids]
        return list(out.values())

    def _default_owner(self) -> tuple[str, str]:
        """Owner d'un namespace créé sans précision = **la personne** (ADR 0068).

        ⚠️ C'était l'**org active** — « suppression du perso », un choix assumé du temps
        où l'appelant était un humain devant un écran, qui voit ce qu'il crée et où.
        L'appelant est aujourd'hui un agent qui ne lit que le nom du verbe : le geste le
        plus banal du produit posait du contenu lisible de toute l'org, sous une
        description qui annonçait « unique per user ».

        Le paramètre reste : `owner_type='org'` (ou `'group'`) donne un classeur
        partagé, et c'est désormais une phrase qu'on écrit plutôt qu'un défaut qu'on
        subit. Les tableaux existants ne bougent pas — la décision porte sur ce qui
        NAÎT."""
        return ("user", self.sub)

    def create_namespace(
        self, namespace: str, *, owner_type: Optional[str] = None, owner_id: Optional[str] = None,
    ) -> dict:
        """Crée un namespace. Défaut = **la personne** (`_default_owner`, ADR 0068).

        ⚠️ Cette phrase disait « défaut = org active », quinze lignes sous le code
        qui rend `("user", sub)` : elle datait du régime d'avant et personne ne
        l'avait suivie jusqu'ici. Un commentaire périmé sur un défaut de
        PROPRIÉTAIRE ne se contente pas d'être faux — il fait conclure à qui le lit
        que le tableau sera visible de l'org (otomata-tech/oto#45).

        Le contexte d'org de l'appel n'entre PAS dans ce choix : pour un classeur
        d'org ou d'équipe, passer `owner_type`/`owner_id`, dont l'autorisation
        (appartenance) est vérifiée par l'appelant (capacité/route)."""
        if owner_type is None:
            owner_type, owner_id = self._default_owner()
        oid = owner_id if owner_id is not None else self.sub
        try:
            ns_id = db.create_datastore_namespace(owner_type, oid, namespace)
        except ValueError as e:
            raise NamespaceExists(str(e))
        return {"namespace": namespace, "id": ns_id, "url": _ns_url(ns_id, self.sub)}

    def delete_namespace(self, namespace: str) -> None:
        ns_id = self._resolve(namespace)
        if not ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)):
            raise NamespaceForbidden(namespace)
        db.delete_datastore_namespace_by_id(ns_id)  # rows + grants partent avec

    def rename_namespace(self, namespace: str, new_name: str) -> dict:
        """Renomme un namespace (l'id/URL/grants restent stables, keyés par id — cf.
        `db.rename_datastore_namespace_by_id`). Exige le droit de GOUVERNANCE, comme la
        suppression. Le nouveau nom doit être libre chez le même propriétaire (sinon
        `NamespaceExists`) — c'est ce qui lève la collision cross-org du gap #71 avant
        un transfert/merge."""
        ns_id = self._resolve(namespace)
        if not ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)):
            raise NamespaceForbidden(namespace)
        new_name = (new_name or "").strip()
        try:
            db.rename_datastore_namespace_by_id(ns_id, new_name)
        except ValueError as e:
            raise NamespaceExists(str(e))
        return {"id": ns_id, "namespace": new_name, "url": _ns_url(ns_id, self.sub)}

    def resolve_ns_id(self, namespace: str) -> int:
        """ns_id d'un namespace visible par l'acteur (lève `NamespaceNotFound`).
        Surface publique pour les chemins de gouvernance (partage/transfert)."""
        return self._resolve(namespace)

    def resolve_ns_id_for_write(self, namespace: str) -> int:
        """ns_id d'un namespace où l'acteur peut ÉCRIRE (lève `NamespaceNotFound`/
        `NamespaceReadOnly`). Sert à sceller la cible d'un upload signé au mint (org
        active présente) ; l'autz est réappliquée au receive via `ownership.can_access`
        sur `datastore_namespace` (org-agnostique), sans contexte d'org."""
        return self._resolve(namespace, write=True)

    def get_url(self, namespace: str) -> str:
        return _ns_url(self._resolve(namespace), self.sub)  # 404 si inconnu

    # --- row ops -------------------------------------------------------------

    def append_row(self, namespace: str, data: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False) -> dict:
        """Écrit UNE row. Si le namespace déclare une clé métier (`schema.key`),
        applique la MÊME dédup upsert que le batch `write_rows` : une row de même
        valeur de clé est MERGÉE (pas de doublon, l'index `ds_bkey_<ns>` la refuse) ;
        sinon append. Renvoie la row (nouvelle ou mise à jour).

        ⚠️ Sur un tableau qui déclare `key_required` (#516), l'append n'existe plus :
        une écriture qui ne désigne aucune ligne existante est REFUSÉE
        (`BusinessKeyRequired`) au lieu d'en créer une.

        `trace` (dict mutable, optionnel) = relevé pour le journal, cf. `_trace`.
        `readonly_override` (#658) = forcer les colonnes verrouillées de CET appel,
        sous palier — cf. `_forcage_readonly`."""
        if isinstance(data, dict) and "_id" in data:
            # PROMOTION (#354, amende le refus #390) : `_id` dans `row` EST
            # l'adresse de la ligne — réécrire la ligne telle que
            # `data_claim_next`/`data_rows` l'a servie devient le geste juste,
            # symétrique du claim. Garde-fou indissociable : un `_id` qui ne
            # matche AUCUNE ligne rend une erreur nommée, jamais une création —
            # sinon la promotion re-fabrique le fantôme par une porte de côté.
            cible = str(data["_id"])
            reste = {k: v for k, v in data.items() if k != "_id"}
            try:
                return self.update_row(namespace, cible, reste, trace=trace,
                                       readonly_override=readonly_override)
            except RowNotFound:
                raise ValueError(
                    f"`_id` ({cible!r}) ne correspond à aucune ligne de "
                    f"`{namespace}` — rien n'est créé. L'identifiant est peut-être "
                    "tronqué ou la ligne purgée : relis-la (data_rows, "
                    "data_claim_next) et réécris avec son `_id` exact.")
        ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        # CAS 1 avant le refus : une fiche relue et réémise entière porte
        # `site_web` ET `site_web.comment`, et c'est notre propre lecture. On range
        # l'annotation à sa place AVANT de juger quoi que ce soit — sinon les gardes
        # qui suivent (champs réservés, schéma) jugeraient une adresse au lieu d'une
        # colonne, et le geste dominant d'un agent se ferait refuser.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: self._colonnes_de_la_ligne_visee(
                ns_id, schema, user_data))
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        # #586 : la couche d'origine d'un champ système ne s'écrit pas, création
        # comprise — jugée sur le payload seul (le readonly, lui, se juge contre la
        # ligne en place, donc dans la fusion). Refusé AVANT le lookup de clé.
        # #607 : la colonne posée par la plateforme se juge du même geste, contre ce
        # qu'on s'apprête à poser — sinon l'agent qui réémet l'estampille courante
        # (le geste dominant) se ferait refuser sa propre lecture.
        sys_pose = self._pose_systeme(schema)
        # #658 : tranché AVANT la fusion — c'est elle qui ouvre le verrou de ligne.
        forcage = self._forcage_readonly(ns_id, schema, readonly_override)
        refuser_champs_reserves(schema, user_data, pose_systeme=sys_pose)
        _relever_origine_module(self, ns_id, user_data, schema=schema,
                                declare=origine_override)
        self._trace(trace, ns_id, ns)
        # La clé métier sort du MÊME schéma que ci-dessus (`declared_key` re-résolvait
        # le namespace et relisait la ligne pour le même résultat).
        key = self._declared_key_of(schema)
        kv = user_data.get(key) if key else None
        if key and kv is not None and str(kv) != "":
            existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
            if existing_id is not None:
                return self._row_to_dict(
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                         origine_override=origine_override),
                    schema)
        # #516 : sur un tableau FERMÉ, on ne crée pas — on vise. Le geste est arrivé
        # jusqu'ici sans désigner de ligne : ni par son `_id` (promu plus haut, et
        # refusé s'il ne matche rien), ni par une valeur de clé que le tableau porte.
        # Refuser AVANT `_check_row` : la validation de schéma parlerait des champs
        # d'une ligne qui ne doit pas naître.
        if dsv2.key_required_of(schema):
            raise _refus_de_creation(ns.get("namespace") or namespace, key, kv)
        # #390 (3ᵉ demande) : une ligne CRÉÉE sans la clé métier déclarée est non
        # rapprochable — aucune écriture ultérieure ne la retrouvera par sa clé, et
        # le batch qui dédouble passera à côté. C'est la forme résiduelle de
        # l'incident : une 501ᵉ ligne sans SIREN née avec tout l'enrichissement,
        # sans une erreur. Les deux autres portes (adresse égarée dans `row`, `id`
        # nu) sont désormais fermées ; celle-ci n'a pas d'adresse du tout, donc rien
        # à refuser — on NOMME, comme `hors_schema`. Mesuré avant de la poser :
        # 197 tableaux à clé déclarée, 50 024 lignes, 3 sans clé. Elle ne parlera
        # quasiment jamais, et c'est ce qui la rendra lisible.
        if key and (kv is None or str(kv) == ""):
            self.off_notices.add(
                f"ligne créée SANS `{key}`, la clé métier de ce tableau : elle ne "
                f"sera rapprochée par personne — ni une réécriture, ni un lot qui "
                f"dédouble sur cette clé. Si elle visait une ligne existante, c'est "
                f"data_write(id=…) ; sinon renseigne `{key}`.")
        # #607 : la plateforme pose AVANT la validation — une colonne `system:`
        # déclarée `required` est satisfaite par ce qu'elle pose, jamais par ce que
        # l'appelant aurait dû deviner.
        poser_valeurs_systeme(schema, user_data, sys_pose)
        self._check_row(schema, user_data)
        try:
            row = db.datastore_insert_row(ns_id, _new_id(), user_data)
        except UniqueViolation:
            # Course perdue sous l'index UNIQUE de clé métier (#109 ch.3) : un write
            # concurrent a inséré la même clé entre le lookup et l'insert — le doublon
            # que la contrainte empêche. On converge en merge (même chemin que le batch).
            existing_id = (db.datastore_find_row_id_by_key(ns_id, key, kv)
                           if key and kv is not None else None)
            if existing_id is None:
                raise  # violation inexpliquée → erreur franche, pas de repli muet
            return self._row_to_dict(
                self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                     forcage=forcage,
                                     origine_override=origine_override),
                schema)
        return self._row_to_dict(row, schema)

    def _assert_writable(self, ns_id: int, row_id: str) -> None:
        """La même protection, pour les chemins qui n'ont PAS de verrou de ligne.

        Le remplacement, la mise à jour et la suppression n'ouvrent pas de
        transaction `FOR UPDATE` (contrairement à la fusion) : la garde y est donc
        posée AVANT l'écriture, sur une lecture séparée.

        ⚠️ **La fenêtre est assumée et bornée** : un claim qui s'intercalerait entre
        ce contrôle et l'écriture passerait. Elle est de l'ordre de la milliseconde,
        et infiniment plus étroite que ce qu'elle remplace — l'absence totale de
        protection sur ces chemins. La refermer demanderait de router ces trois
        gestes par le verrou de ligne, ce qui change leur sémantique (remplacer n'est
        pas fusionner) : c'est un lot, pas une rustine.

        Aucun contrôle sur une ligne NEUVE : elle ne peut pas être réservée."""
        lease = db.datastore_active_lease(ns_id, row_id)
        if not lease:
            return
        run = _current_run()
        if run and lease.get("claimed_run") == run:
            return
        if _WRITING_AS.get() and _WRITING_AS.get() == lease.get("claimed_by"):
            return
        raise RowLocked(row_id, lease.get("claimed_by"), lease.get("claimed_until"),
                        lease.get("claimed_run"))

    @staticmethod
    def _lease_guard(row_id: str):
        """La protection en écriture (#317) — appelée SOUS le verrou de la ligne.

        Le bail empêchait deux agents de PRENDRE la même ligne, pas d'ÉCRIRE dessus :
        il protégeait l'attribution, pas la donnée. Ici il protège les deux.

        **Le titulaire s'identifie de deux façons qui se recouvrent** — parce qu'une
        écriture ordinaire ne dit pas qui écrit, et que `claimed_by` est un libellé
        libre (`'campagne-s8'`), jamais un compte :

        - **par le RUN** : écrire sous le run qui tient la ligne, c'est être le
          titulaire — rien à déclarer, le cas nominal est transparent ;
        - **par le WORKER** rejoué (`_writing_as`) : la sortie explicite hors run,
          et c'est déjà LA garde du release, donc aucun concept nouveau.

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
            if _WRITING_AS.get() and _WRITING_AS.get() == by:
                return                       # le titulaire, par son worker
            raise RowLocked(row_id, by, until, locked.get("claimed_run"))
        return _guard

    def _merge_into_row(self, ns_id: int, row_id: str, user_data: dict,
                        *, schema: Optional[dict] = None,
                        forcage: Optional[Forcage] = None,
                        origine_override: bool = False) -> dict:
        """MERGE `user_data` dans la row existante (dernier écrit gagne par champ),
        en appliquant le schéma v2 (ADR 0046) au résultat mergé : validation avec
        `prev_status` (transition de lifecycle) puis release du claim si l'état
        devient terminal. Renvoie la row brute persistée. Corps commun à l'append
        unitaire et au batch.

        Le read-merge-write est ATOMIQUE (verrou de ligne, #197) : le get + le
        merge + l'update tournent dans une seule transaction `FOR UPDATE`, sinon
        deux writes concurrents de la même clé (même row_id) s'écrasaient
        mutuellement (last-writer-wins) et perdaient des champs silencieusement."""
        if schema is None:
            schema = self._schema_of(ns_id)
        _refuse_flat_writes(schema, user_data)
        # La ligne visée est connue ICI : ses colonnes comptent pour « colonne réelle »,
        # ce qui rend `{"site_web.comment": …}` seul écrivable sur un tableau souple.
        # Lue paresseusement — le chemin nominal ne la demande jamais.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: set(
                (db.datastore_get_row(ns_id, row_id) or {}).get("data") or {}))
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        sk = (dsv2.status_field(schema) or {}).get("key")
        # #607 : calculée HORS du `_apply`, qui peut être rejoué par le verrou — deux
        # tours donneraient deux horodatages pour une seule écriture.
        sys_pose = self._pose_systeme(schema)

        def _apply(current: dict) -> dict:
            merged = dict(current or {})
            prev_status = merged.get(sk) if sk else None
            # Arbitrage AVANT la fusion : après, l'ancienne valeur n'existe plus
            # nulle part. Il rend d'un coup ce que l'écriture pose VRAIMENT (les
            # vides non-`null` qui auraient déplacé une valeur en sont retirés,
            # #608) et les deux relevés. Posés sur le store seulement une fois la
            # validation passée — un refus n'a rien effacé, l'annoncer ferait
            # chercher un dégât imaginaire.
            pose, vidages, ecartes = arbitrer_les_vides(current, user_data, row_id)
            # #724 : préserver et le DIRE ne suffit pas quand l'écarté était TOUT ce
            # que l'écriture portait — l'appel n'a alors aucun effet et répond 200.
            # ⚠️ Par CE chemin le refus ne peut pas parler : on n'arrive ici (append
            # promu, lot) qu'avec une valeur de clé métier non vide, donc posée — ce
            # qui garantit qu'un LOT ne casse jamais dessus. Il y est quand même :
            # les deux chemins d'écriture ont déjà divergé une fois sur cette famille
            # de règles (#322), ils partagent la fonction, pas seulement l'intention.
            refuser_geste_sans_effet(pose, ecartes)
            # Colonne par colonne, pour que l'origine survive à une écriture
            # ordinaire. Un `update` en bloc l'emporterait avec le reste — et
            # silencieusement, puisque remplacer une valeur est le geste normal.
            for _k, _v in pose.items():
                merged[_k] = _merge_column(merged.get(_k), _v)
            # #586/#606 : ce que l'appelant n'écrit pas — jugé sur le geste ENTIER
            # (payload, ligne en place, résultat), sous le verrou, avant que quoi
            # que ce soit ne parte. Puis la plateforme pose l'origine qu'elle doit.
            refuser_champs_reserves(schema, pose, avant=current or {},
                                    pose_systeme=sys_pose, forcage=forcage)
            _relever_origine_module(self, ns_id, pose, current or {}, schema=schema,
                                    declare=origine_override)
            poser_origine_systeme(schema, current, merged, set(pose))
            # #607 : l'estampille est reposée sur CHAQUE écriture — c'est le point du
            # cran. Après l'origine : sur une colonne qui porterait les deux, la
            # capture doit voir la valeur d'avant, pas celle qu'on vient de poser.
            poser_valeurs_systeme(schema, merged, sys_pose)
            # ⚠️ `written` reste l'ensemble des clés que l'appelant a NOMMÉES, pas
            # celles qu'on a retenues : une borne de longueur ou un motif ne doit pas
            # se réarmer sur une colonne préservée, dont la valeur n'a pas bougé.
            self._check_row(schema, merged, prev_status=prev_status,
                            written=set(pose))
            self.off_erased.extend(vidages)
            self.off_ignored.extend(ecartes)
            return merged

        result = db.datastore_merge_row_locked(ns_id, row_id, _apply, _now_iso(),
                                               lease_guard=self._lease_guard(row_id))
        if result is None:
            raise RowNotFound(row_id)  # supprimée entre le lookup et le verrou (course)
        row, merged = result
        # #658 : après le verrou — un forçage n'est journalisé que s'il a ABOUTI.
        self._relever_forcage(forcage, row_id)
        self._terminal_write_notice(schema, ns_id, row_id, merged)
        return row

    def upsert_row(self, namespace: str, row_id: str, data: dict, *,
                   origine_override: bool = False) -> tuple[dict, bool]:
        """Écrit une row à une clé `row_id` EXPLICITE (≠ append_row qui génère un
        id), en remplaçant si elle existe. Crée le namespace au besoin. Sert le
        stockage dédupliqué par clé stable (ex. urn LinkedIn). Renvoie
        `(row, inserted)` — `inserted` False = la row existait déjà."""
        self._reject_misplaced_id(data, row_id)
        try:
            ns_id = self._resolve(namespace, write=True)
        except NamespaceNotFound:
            _ot, _oid = self._default_owner()
            db.create_datastore_namespace(_ot, _oid, namespace)
            self._active_scope_cache = None  # invalide le cache (le ns créé appartient à la PERSONNE (ADR 0068), pas à l'org active)
            ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        schema = self._schema_of(ns_id)
        # ⚠️ Pas de `colonnes_en_place` ici, et c'est délibéré : l'upsert REMPLACE la
        # ligne. Ranger une annotation sur une colonne qui n'est que dans l'ancienne
        # ligne poserait une couche sur une valeur qui tombe dans le même geste.
        user_data = ranger_les_couches(schema, user_data)
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        valide = dsv2.validation_active(schema) or dsv2.lifecycle_of(schema)
        sys_pose = self._pose_systeme(schema)
        reserves = bool(dsv2.readonly_fields(schema)
                        or dsv2.system_origin_fields(schema) or sys_pose)
        prev = db.datastore_get_row(ns_id, row_id) if (valide or reserves) else None
        prev_data = dict((prev or {}).get("data") or {}) if prev else None
        if reserves:
            # #586/#606 sur un REMPLACEMENT : une colonne readonly absente du corps
            # serait perdue par le remplacement — c'est une modification, jugée
            # comme telle (le payload est complété des colonnes qui tomberaient).
            complet = {**{k: None for k in (prev_data or {}) if k not in user_data},
                       **user_data}
            refuser_champs_reserves(schema, complet, avant=prev_data,
                                    pose_systeme=sys_pose)
            _relever_origine_module(self, ns_id, complet, prev_data, schema=schema,
                                    declare=origine_override)
            if prev_data is not None:
                poser_origine_systeme(schema, prev_data, user_data, set(complet))
        # #607 : hors du `if reserves` — un remplacement qui n'emporte QUE l'estampille
        # doit quand même la reposer, et `sys_pose` vide ne fait rien.
        poser_valeurs_systeme(schema, user_data, sys_pose)
        if valide:
            sk = (dsv2.status_field(schema) or {}).get("key")
            prev_status = (prev_data or {}).get(sk) if sk else None
            self._check_row(schema, user_data, prev_status=prev_status)
        self._assert_writable(ns_id, row_id)
        row, inserted = db.datastore_upsert_row(ns_id, row_id, user_data)
        if not inserted:
            self._terminal_write_notice(schema, ns_id, row_id, user_data)
        return self._row_to_dict(row, schema), inserted

    def declared_key(self, namespace: str) -> Optional[str]:
        """Clé métier déclarée au schéma (`schema.key`) — sert la dédup au batch
        write. None si aucune (table libre / schéma sans clé)."""
        return self._declared_key_of(self.get_schema(namespace))

    def write_rows(self, namespace: str, rows: list, *, key: Optional[str] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False) -> dict:
        """Écrit un LOT de rows en un appel. Si une clé métier est en vigueur (param
        `key` explicite, sinon `schema.key` déclarée), chaque row qui la porte fait un
        UPSERT (merge) sur la row existante de même valeur de clé — pas de doublon ;
        sinon append d'une nouvelle row. Renvoie un récap {inserted, updated, count,
        key, ids}. Résout le namespace UNE fois (write) pour tout le lot."""
        ns_id = self._resolve(namespace, write=True)
        return self._write_rows_to_ns(ns_id, rows, key=key or self.declared_key(namespace),
                                      readonly_override=readonly_override,
                                      origine_override=origine_override)

    @staticmethod
    def _designation_de_lot(rang: int, total: int, key: Optional[str],
                            data: Any, faites: int) -> str:
        """COMMENT retrouver la ligne fautive d'un lot, et OÙ le lot s'est arrêté (#412).

        Le refus nommait le champ et la valeur, jamais la ligne : sur un import de
        8 910 lignes par lots de 200, retrouver la fautive coûtait plus cher que les
        199 lignes perdues avec elle. Le store valide ligne par ligne — il SAIT
        laquelle échoue, l'information existait et ne sortait pas.

        ⚠️ On y ajoute ce que le signal croyait acquis et qui est FAUX : **le lot
        n'est pas atomique**. Les lignes qui précèdent la fautive sont écrites et le
        restent. C'est ce qui décide de la reprise — rejouer le lot entier
        re-fusionnerait les premières (ou les dupliquerait, sans clé métier)."""
        ref = ""
        if key and isinstance(data, dict) and data.get(key) is not None:
            ref = f" ({key}={data[key]})"
        etat = (f"{faites} ligne{'s' if faites > 1 else ''} déjà "
                f"écrite{'s' if faites > 1 else ''} avant l'arrêt, aucune après "
                f"— reprends le lot à la ligne {rang}" if faites
                else "aucune ligne écrite avant l'arrêt")
        return f"ligne {rang}/{total} du lot{ref} · {etat}"

    def _write_rows_to_ns(self, ns_id: int, rows: list, *, key: Optional[str],
                          readonly_override: bool = False,
                          origine_override: bool = False) -> dict:
        """Cœur du batch, keyé par `ns_id` déjà résolu (réutilisable hors contexte
        d'org — matérialisation d'un upload signé, où l'org de session est absente).
        Le schéma v2 (validation/lifecycle, ADR 0046) s'applique à CHAQUE row du
        lot, sur son résultat mergé — une row fautive fait échouer le lot en NOMMANT
        la ligne autant que le champ (#412), et en disant ce qui est déjà écrit."""
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        nom_ns = ns.get("namespace") or f"#{ns_id}"
        # #658 : UN palier pour le lot entier, lu une seule fois — pas une lecture
        # d'ownership par ligne sur un import de huit mille.
        forcage = self._forcage_readonly(ns_id, schema, readonly_override)
        inserted, updated, ids = 0, 0, []
        total = len(rows)
        for rang, data in enumerate(rows, 1):
            try:
                if not isinstance(data, dict):
                    raise ValueError("chaque row doit être un objet")
                self._reject_misplaced_id(data, None, batch=True)
                user_data = {k: v for k, v in data.items() if k not in _META_COLS}
                # ⚠️ #329 volet 2, appliqué au QUATRIÈME chemin — il y manquait.
                # `append_row`, `upsert_row` et la fusion refusent une clé littérale
                # pointée ; le LOT, non. Or c'est LUI qui porte les imports : la garde
                # était posée sur les trois chemins où l'on écrit une ligne, et absente
                # de celui où l'on en écrit huit mille.
                #
                # Ce que ça a produit, mesuré le 31/08 sur un fichier de production :
                # une fiche porte `contact2_nom.comment` et `contact2_email.comment`
                # comme COLONNES littérales de premier niveau, à côté d'une base
                # `contact2_nom` qui, elle, a été retirée depuis. Elles ont donc survécu
                # au retrait — *une couche imbriquée part avec sa colonne, une colonne
                # littérale du même nom ne part pas* — et se relisent ensuite comme des
                # « couches orphelines », un objet qui n'existe pas dans le modèle.
                # Deux sessions ont cherché le geste pendant une demi-journée.
                # MÊME ordre que les quatre autres portes : on range, puis on refuse.
                # C'est par ici que passent les imports — donc par ici que passe un
                # export du tableau de bord réimporté, qui porte `champ.comment` par
                # construction (#687).
                user_data = ranger_les_couches(
                    schema, user_data,
                    colonnes_en_place=lambda: self._colonnes_de_la_ligne_visee(
                        ns_id, schema, user_data, key))
                _refuse_dotted_names(user_data)
                kv = user_data.get(key) if key else None
                existing_id = None
                if key and kv is not None and str(kv) != "":
                    existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
                # #516 : le LOT est le second chemin de création, et le plus
                # volumineux — c'est par lui que passent les imports. La garde s'y
                # juge sur la clé DÉCLARÉE, celle qui porte l'index UNIQUE, même
                # quand le lot dédouble sur une AUTRE (`key=` explicite) : sinon un
                # tableau fermé refuserait une ligne qu'il porte déjà.
                if existing_id is None and dsv2.key_required_of(schema):
                    dk = self._declared_key_of(schema)
                    dkv = user_data.get(dk)
                    if dk != key and dkv is not None and str(dkv) != "":
                        existing_id = db.datastore_find_row_id_by_key(ns_id, dk, dkv)
                    if existing_id is None:
                        raise _refus_de_creation(nom_ns, dk, dkv)
                if existing_id is not None:
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                     origine_override=origine_override)
                    updated += 1
                    ids.append(existing_id)
                    continue
                # #586 : la création dans le LOT (même chemin que l'upload signé) —
                # la couche d'origine d'un champ système ne s'écrit pas.
                # #607 : et l'estampille s'y pose comme partout ailleurs — c'est le
                # chemin le plus volumineux, donc celui où un trou produirait le plus
                # de lignes sans trace.
                sys_pose = self._pose_systeme(schema)
                refuser_champs_reserves(schema, user_data, pose_systeme=sys_pose)
                _relever_origine_module(self, ns_id, user_data, schema=schema,
                                        declare=origine_override)
                poser_valeurs_systeme(schema, user_data, sys_pose)
                self._check_row(schema, user_data)
                try:
                    row = db.datastore_insert_row(ns_id, _new_id(), user_data)
                except UniqueViolation:
                    # Course perdue sous l'index UNIQUE de clé métier (#109 ch.3) : un
                    # write concurrent vient d'insérer la même clé entre le lookup et
                    # l'insert — c'est PRÉCISÉMENT le doublon que la contrainte empêche.
                    # On converge en update (même merge que le chemin nominal). La clé
                    # violée est la clé DÉCLARÉE du namespace (l'index ne porte qu'elle),
                    # qui peut différer d'un `key` explicite passé à l'appel.
                    dk = ((db.get_datastore_namespace_by_id(ns_id) or {}).get("schema")
                          or {}).get("key")
                    dkv = user_data.get(dk) if dk else None
                    existing_id = (db.datastore_find_row_id_by_key(ns_id, dk, dkv)
                                   if dk and dkv is not None else None)
                    if existing_id is None:
                        raise  # violation inexpliquée → erreur franche, pas de repli muet
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                     origine_override=origine_override)
                    updated += 1
                    ids.append(existing_id)
                    continue
            except RowLocked as e:
                # ⚠️ MÊME parti que les deux clauses suivantes, et pour la même
                # raison : le refus garde sa CLASSE, seule sa désignation change.
                # `RowLocked` dérive de `ValueError` depuis le 05/09/2026 ; sans
                # cette clause, elle tomberait dans le `except ValueError` du bas et
                # ressortirait en refus d'entrée invalide — perdant son code 409 et
                # le message du bail, exactement le défaut qu'on vient de fermer.
                raise RowLocked(
                    e.row_id, e.claimed_by, e.claimed_until, e.claimed_run,
                    row=self._designation_de_lot(rang, total, key, data,
                                                 inserted + updated)) from None
            except BusinessKeyRequired as e:
                # MÊME parti que ci-dessous : le refus garde sa classe (la face REST
                # en dérive son code `business_key_required`), seule sa désignation
                # change. Cette clause DOIT précéder `except ValueError` — dont
                # `BusinessKeyRequired` dérive, pour être actionnable côté MCP.
                raise BusinessKeyRequired(
                    e.motif, key=e.key, namespace=e.namespace, value=e.value,
                    row=self._designation_de_lot(rang, total, key, data,
                                                 inserted + updated)) from None
            except RowValidationError as e:
                # Le refus GARDE sa classe : les surfaces s'en servent pour choisir
                # leur code (`capabilities/datastore/rows`), et un refus de schéma
                # dans un lot reste un refus de schéma. Seule sa désignation change —
                # `details` suit, sinon le refus structuré (#545) se perdrait
                # exactement là où le lot rend la reprise la plus coûteuse.
                raise RowValidationError(
                    e.errors, details=e.details,
                    row=self._designation_de_lot(rang, total, key, data,
                                                 inserted + updated)) from None
            except ValueError as e:
                # Les autres refus de row (`id` égaré, `_id` dans un lot, row qui
                # n'est pas un objet) nomment déjà LEUR faute, jamais la ligne.
                raise ValueError(
                    f"{self._designation_de_lot(rang, total, key, data, inserted + updated)}"
                    f" : {e}") from None
            inserted += 1
            ids.append(row["row_id"])
        return {"inserted": inserted, "updated": updated, "count": inserted + updated,
                "key": key, "ids": ids}

    def get_row(self, namespace: str, row_id: str, *,
                layers: str = dsl.DEFAUT) -> dict:
        ns_id = self._resolve(namespace)
        row = db.datastore_get_row(ns_id, row_id)
        if not row:
            raise RowNotFound(row_id)
        return self._row_to_dict(row, self._schema_of(ns_id), layers=layers)

    def list_rows(
        self,
        namespace: str,
        filter: Optional[dict] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Filtre exact k:v en Python (chemin MCP `data_rows`). Ordre stable plus
        ancien d'abord (compat historique)."""
        ns_id = self._resolve(namespace)
        sch = self._schema_of(ns_id)
        out: list[dict] = []
        for row in db.datastore_list_rows(ns_id, order_by="_created_at", order_dir="asc"):
            record = self._row_to_dict(row, sch)
            if filter and not all(str(record.get(k)) == str(v) for k, v in filter.items()):
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    def cursor_rows(
        self,
        namespace: str,
        *,
        filter: Optional[dict] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        q: Optional[str] = None,
        order_by: Optional[str] = None,
        order_dir: str = "desc",
        filters: Optional[list] = None,
        layers: str = dsl.DEFAUT,
    ) -> dict:
        """Page pour l'agent (chemin MCP `data_rows`), filtre/recherche/tri poussés en
        SQL. Renvoie `{rows, next_cursor}` — `next_cursor` non nul ⇒ il reste des lignes
        (repasse-le pour la suite).

        `filter` et `filters` se cumulent (cf. `_filter_clauses`) : le premier vise une
        colonne, le second en vise plusieurs à la fois.

        Deux régimes de pagination, et le curseur porte lequel :
          - **sans `order_by`** (défaut) → keyset sur `row_id` = ordre de création,
            robuste aux écritures concurrentes (pas d'OFFSET qui dérive) ;
          - **avec `order_by`** → tri SQL demandé + pagination par offset, faute de clé
            keyset stable pour un tri arbitraire.

        Repasser le curseur d'un régime dans l'autre lève `InvalidCursor` plutôt que de
        rendre une page fausse — un curseur d'offset relu comme un `row_id` cadrerait
        silencieusement sur les mauvaises lignes."""
        ns_id = self._resolve(namespace)
        filters = _filter_clauses(filter, filters)
        # ⚠️ Le schéma se lit une fois par PAGE, et seulement quand il y a quelque chose
        # à résoudre ou à servir : le lire dès l'entrée ferait payer une requête à un
        # appel qui va refuser son curseur — un coût là où il n'y a même pas de résultat.
        sch = self._schema_of(ns_id) if (filters or order_by) else None
        filters = _resolve_filters(sch, filters)
        order_by = _to_path(sch, order_by)
        if order_by:
            offset = _decode_offset_cursor(cursor) if cursor else 0
            # Même résolution de type que `page_rows` : le tri d'un champ ne peut
            # pas répondre juste sur une face et faux sur l'autre (#336).
            otype, oopts = dsv2.order_spec(sch, order_by)
            rows = db.datastore_list_rows(
                ns_id, offset=offset, limit=limit, order_by=order_by,
                order_dir=order_dir, q=q, filters=filters,
                order_type=otype, order_options=oopts)
            next_cursor = (_encode_offset_cursor(offset + len(rows))
                           if len(rows) == limit else None)
            if sch is None and rows:
                sch = self._schema_of(ns_id)
            out = {"rows": [self._row_to_dict(r, sch, layers=layers) for r in rows],
                   "next_cursor": next_cursor}
            health = self._order_health(ns_id, order_by, otype, oopts, q, filters)
            if health:
                out["order_health"] = health
            return out
        after = _decode_cursor(cursor) if cursor else None
        if after and after.startswith(_OFFSET_CURSOR_PREFIX):
            raise InvalidCursor(cursor)  # curseur trié repassé sans `order_by`
        rows = db.datastore_list_rows_after(
            ns_id, after_row_id=after, limit=limit, q=q, filters=filters)
        if sch is None and rows:
            sch = self._schema_of(ns_id)
        out = [self._row_to_dict(r, sch, layers=layers) for r in rows]
        next_cursor = _encode_cursor(rows[-1]["row_id"]) if len(rows) == limit else None
        return {"rows": out, "next_cursor": next_cursor}

    def count_rows(self, namespace: str, *, filter: Optional[dict] = None,
                   q: Optional[str] = None, filters: Optional[list] = None) -> int:
        """Nombre de lignes (mêmes `filter`/`filters`/`q` que `cursor_rows`), poussé en
        SQL (`COUNT(*)`) — sans rapatrier les lignes (feedback #191 : stats d'un gros
        vivier sans charger 300+ lignes en contexte)."""
        ns_id = self._resolve(namespace)
        clauses = _filter_clauses(filter, filters)
        # Le compte doit décrire le MÊME jeu que la page : mêmes noms résolus.
        clauses = _resolve_filters(self._schema_of(ns_id), clauses) if clauses else clauses
        return db.datastore_count_rows(ns_id, q=q, filters=clauses)

    def aggregate(self, namespace: str, *, group_by=None,
                  metrics: Optional[list] = None, filter: Optional[dict] = None,
                  q: Optional[str] = None, filters: Optional[list] = None) -> list[dict]:
        """Agrégat serveur (feedback #191) : COUNT/SUM/AVG/MIN/MAX sur des champs JSONB,
        `group_by` optionnel — stats d'un vivier sans rapatrier les lignes. Délègue à
        `db.datastore_aggregate`. Deux formes de filtre cumulables : `filter` exact
        `{col: val}` (chemin MCP) et `q`/`filters` riches ({field|fields, op, value},
        mêmes clauses que `page_rows`) — le dashboard agrège ainsi le MÊME jeu que sa
        vue filtrée (tuiles metric).

        `group_by` accepte une LISTE de colonnes (oto#22) : leurs valeurs sont mises en
        commun, une ligne comptant une occurrence par colonne renseignée."""
        ns_id = self._resolve(namespace)
        clauses = _filter_clauses(filter, filters)
        demande = group_by
        if clauses or group_by or metrics:
            sch = self._schema_of(ns_id)
            clauses = _resolve_filters(sch, clauses)
            group_by = _resolve_group_by(sch, group_by)
            metrics = _resolve_metrics(sch, metrics)
        out = db.datastore_aggregate(
            ns_id, group_by=group_by, metrics=metrics, q=q, filters=clauses)
        # L'appelant retrouve la clé sous le nom QU'IL A DEMANDÉ. Rendre le nom résolu
        # obligerait chaque consommateur à connaître la traduction — donc à savoir
        # qu'une migration est en cours, ce que le double-service existe pour lui
        # épargner : sa facette doit revivre à l'identique, sans une ligne changée.
        avant, apres = db.group_key(demande), db.group_key(group_by)
        if avant != apres:
            out = [{(avant if k == apres else k): v for k, v in r.items()} for r in out]
        return out

    def page_rows(
        self,
        namespace: str,
        *,
        offset: int = 0,
        limit: int = 50,
        order_by: Optional[str] = None,
        order_dir: str = "desc",
        q: Optional[str] = None,
        filter: Optional[dict] = None,
        filters: Optional[list] = None,
        layers: str = dsl.DEFAUT,
    ) -> dict:
        """Page server-side (tri/recherche/filtres SQL) + total — pour le dashboard.
        Deux formes de filtre CUMULABLES, comme `aggregate` : `filter` exact
        `{col: val}` (chemin MCP, et la CLI `--filter`) et `filters` riches
        (liste `{field, op, value}`, combinées en ET). Renvoie
        `{rows, total, offset, limit}`.

        `filter` manquait ici alors que `cursor_rows`, `aggregate` et `claim_next`
        le portent : la face REST du même verbe ignorait donc **en silence** un
        paramètre que la face MCP honore (#303).

        Le tri honore le TYPE DÉCLARÉ de la colonne (#336) : `number` trié
        numériquement, `enum` dans l'ordre déclaré des options, `date` en
        chronologique. Les valeurs non conformes vont en QUEUE dans les deux
        sens (bloc alphabétique), les cases vides tout au bout — et quand il y
        en a, la réponse porte `order_health: {off_type, empty}` (compté sur le
        jeu filtré entier, absent quand tout est conforme)."""
        ns_id = self._resolve(namespace)
        clauses = _filter_clauses(filter, filters) or None
        sch = self._schema_of(ns_id)
        clauses = _resolve_filters(sch, clauses) or None
        order_by = _to_path(sch, order_by)
        # Le tri honore le TYPE déclaré (#336) — résolu ICI, où le schéma est connu :
        # la couche db reçoit un type générique, jamais le schéma.
        otype, oopts = dsv2.order_spec(sch, order_by)
        rows = db.datastore_list_rows(
            ns_id, offset=offset, limit=limit, order_by=order_by,
            order_dir=order_dir, q=q, filters=clauses,
            order_type=otype, order_options=oopts)
        out = {
            "rows": [self._row_to_dict(r, sch, layers=layers) for r in rows],
            # Le total doit décrire le MÊME jeu que la page : filtré aussi, sinon la
            # pagination du dashboard annonce des lignes qu'elle ne servira jamais.
            "total": db.datastore_count_rows(ns_id, q=q, filters=clauses),
            "offset": offset, "limit": limit,
        }
        health = self._order_health(ns_id, order_by, otype, oopts, q, clauses)
        if health:
            out["order_health"] = health
        return out

    def update_row(self, namespace: str, row_id: str, patch: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False) -> dict:
        """Patch partiel d'une row. `trace` (dict mutable, optionnel) = relevé pour
        le journal — dont l'état AVANT, celui-là même sur lequel la transition de
        cycle de vie est validée juste en dessous (cf. `_trace`).

        `readonly_override` (#658) = forcer les colonnes verrouillées de CET appel,
        sous palier — cf. `_forcage_readonly`."""
        self._reject_misplaced_id(patch, row_id)
        ns_id = self._resolve(namespace, write=True)
        existing = db.datastore_get_row(ns_id, row_id)
        if not existing:
            raise RowNotFound(row_id)
        data = dict(existing.get("data") or {})
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        _refuse_flat_writes(schema, patch)
        # La ligne est déjà lue : ses colonnes sont « réelles » sans un aller-retour de
        # plus. C'est la porte du round-trip #390 — relire une fiche et la repousser —
        # donc celle où l'aller-retour DOIT se refermer.
        patch = ranger_les_couches(schema, patch, colonnes_en_place=lambda: set(data))
        _refuse_dotted_names(patch)
        _refuse_mixed_layers(schema, patch)
        status_key = (dsv2.status_field(schema) or {}).get("key")
        prev_status = data.get(status_key) if status_key else None
        self._trace(trace, ns_id, ns, prev_status=prev_status)
        # MÊME arbitrage que la fusion : le patch par `id` est le geste qui a vidé
        # `moteur` en production le 13/08 — et il l'a fait en le NOMMANT (#407/#408/
        # #409). Fait avant la boucle, sur l'état lu en base. Les deux chemins
        # d'écriture ont déjà divergé une fois sur cette famille de règles (#322) :
        # ils partagent donc la fonction, pas seulement l'intention.
        pose, vidages, ecartes = arbitrer_les_vides(data, patch, row_id)
        # #724 : le patch par `id` est le chemin des dix retraits perdus du 01/09 —
        # un vide SEUL y était accepté sans effet, et le relevé qui nommait déjà la
        # porte n'a pas été lu. Refusé AVANT tout relevé : rien n'a été touché, il
        # n'y a donc rien à annoncer — le message, lui, écrit la porte en toutes
        # lettres, au moment où l'appelant peut encore corriger.
        refuser_geste_sans_effet(pose, ecartes)
        avant = dict(data)
        written = set()
        for k, v in pose.items():
            if k in _META_COLS:
                continue
            # MÊME fusion que le batch : l'origine survit ici aussi. Elle avait été
            # câblée dans `_merge_into_row` seulement — donc un patch par `id`, le
            # geste le plus courant d'un agent, l'effaçait quand même.
            data[k] = _merge_column(data.get(k), v)
            written.add(k)
        # #586/#606 : MÊME garde que la fusion — le patch par `id` est le geste le
        # plus courant d'un agent, et celui qui a écrasé les quatorze valeurs.
        sys_pose = self._pose_systeme(schema)
        forcage = self._forcage_readonly(ns_id, schema, readonly_override)
        refuser_champs_reserves(schema, pose, avant=avant, pose_systeme=sys_pose,
                                forcage=forcage)
        # ⚠️ CE chemin-ci a déjà été oublié une fois, six lignes plus haut : l'origine
        # n'avait été câblée que dans `_merge_into_row`, et le patch par `id` — le
        # geste le plus courant d'un agent — l'effaçait quand même. Le barreau 1 a
        # refait la MÊME omission sur le relevé : quatre chemins branchés, celui-ci
        # non. Un instrument qui ne voit pas le geste le plus courant sous-compte
        # exactement la population qu'il existe pour trouver.
        _relever_origine_module(self, ns_id, pose, avant, schema=schema,
                                declare=origine_override)
        poser_origine_systeme(schema, avant, data, written)
        # #607 : le patch par `id` est le geste le plus courant d'un agent — donc
        # celui où une estampille manquante se verrait le plus.
        poser_valeurs_systeme(schema, data, sys_pose)
        # Validation sur le RÉSULTAT mergé (un patch partiel ne doit pas échouer
        # sur un requis déjà présent) + transition de cycle de vie (ADR 0046 B/C).
        # Seule la borne de longueur se limite aux clés du patch (#383).
        self._check_row(schema, data, prev_status=prev_status, written=written)
        self.off_erased.extend(vidages)
        self.off_ignored.extend(ecartes)
        try:
            self._assert_writable(ns_id, row_id)
            row = db.datastore_update_row(ns_id, row_id, data, _now_iso())
        except UniqueViolation:
            # Un AUTRE enregistrement porte déjà cette valeur de clé métier (index
            # UNIQUE ds_bkey_<ns_id>). Contrairement au batch write (qui converge en
            # merge sur la row de même clé), un update ciblé sur `row_id` ne peut pas
            # basculer silencieusement sur une autre row → erreur actionnable
            # (ValueError → INVALID_PARAMS), jamais un 500 opaque.
            dk = (schema or {}).get("key")
            dkv = data.get(dk) if dk else None
            if dk and dkv is not None:
                raise ValueError(
                    f"un autre enregistrement porte déjà {dk}={dkv} "
                    "(clé métier unique) — impossible de dupliquer") from None
            raise  # violation inexpliquée → erreur franche, pas de repli muet
        # #658 : après l'UPDATE — un forçage n'est journalisé que s'il a ABOUTI.
        self._relever_forcage(forcage, row_id)
        self._terminal_write_notice(schema, ns_id, row_id, data)
        return self._row_to_dict(row, schema)

    # --- file de travail (ADR 0046 D) -----------------------------------------

    def claim_next(self, namespace: str, *, worker: str,
                   filter: Optional[dict] = None, lease_s: int = 900,
                   max_claims: Optional[int] = None,
                   warnings: Optional[list] = None,
                   trace: Optional[dict] = None,
                   perimetre: Optional[dict] = None,
                   layers: str = dsl.DEFAUT) -> Optional[dict]:
        """Pick + claim atomique de la prochaine row claimable (bail NULL ou
        expiré), `FOR UPDATE SKIP LOCKED` — N workers drainent sans collision.
        `filter` = `{col: val}`, ou `{col: {op: val}}` pour un opérateur (même
        grammaire que `data_rows`). Renvoie la row (avec `_claimed_by`/
        `_claimed_until`) ou None (file vide).

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
        ns_id = self._resolve(namespace, write=True)
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        declare = dsv2.claimable_of(schema)
        if perimetre is not None and declare:
            perimetre.update(declare)
        filters = claimable.clauses(declare) + _filter_clauses(filter, None)
        row = db.datastore_claim_next(ns_id, worker=worker,
                                      lease_seconds=int(lease_s), filters=filters,
                                      run_id=_current_run(), max_claims=max_claims)
        if row is not None:
            self._after_claim(ns_id, warnings=warnings, trace=trace, ns=ns)
        # oto#63 : la RÉSERVATION est le seul chemin qu'un agent emprunte, et
        # c'était le seul à ne pas porter `layers`. Ce qu'il voit ici est le
        # modèle de ce qu'il réécrira — servir `champ.comment` à plat, c'est lui
        # montrer une forme qu'il transformera en `champ_comment` faute de savoir
        # qu'un point est adressable.
        return self._row_to_dict(row, schema, layers=layers) if row else None

    def claim_row(self, namespace: str, row_id: str, *, worker: str,
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
        ns_id = self._resolve(namespace, write=True)
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        declare = dsv2.claimable_of(schema)
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
        Une seule lecture de la ligne namespace pour les deux — et aucune quand
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

    def release_claim(self, namespace: str, row_id: str, *, worker: str,
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
        ns_id = self._resolve(namespace, write=True)
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

    def resolve_claimed_ref(self, namespace: str, *,
                            worker: Optional[str] = None) -> str:
        """`@claimed` → l'identifiant de la ligne que cet appel tient (#517).

        La réservation porte déjà les deux choses que l'agent recopiait : la LIGNE et
        le TABLEAU. Les lui faire repasser lui demandait trente-deux caractères
        aléatoires, et il en altère un — ou en fabrique un dans une convention
        étrangère. Ici le serveur les relit ; il ne reste rien à transcrire.

        ⚠️ **L'appartenance se prouve par le jeton de run**, jamais par le seul
        identifiant (#546, refusée : ça viderait la notion de run). Sans jeton on
        refuse — mais le refus NOMME le paramètre absent, parce que c'est justement
        celui que les agents omettent quand la description leur dit qu'il est hérité
        (#547). `worker` ne prouve rien (c'est une étiquette choisie par l'appelant) :
        il ne fait que restreindre, comme au relâchement.

        Chaque refus dit quoi faire ensuite. Le silence coûte plus que le refus :
        l'agent qui ne comprend pas réessaie **sans identifiant**, et une écriture
        sans identifiant crée une ligne au lieu d'en corriger une."""
        ici, ailleurs = self._baux_du_run(namespace, worker=worker)
        if ici is None:
            raise ClaimedRefUnresolved(
                f"`{CLAIMED_REF}` désigne la ligne que TON travail tient, et cet appel "
                "n'est rattaché à aucun travail : passe `_run_id` (celui de ton "
                "`run_start`) sur cet appel — il n'est pas hérité —, ou écris avec "
                "l'identifiant que `data_claim_next` t'a rendu.")
        if not ici and not ailleurs:
            # `ici is None` a déjà écarté le hors-run : il y a donc bien un travail, et
            # la question qui reste est s'il est encore ouvert (#645).
            raise _refus_rien_tenu(run=_current_run())
        if len(ici) == 1:
            return ici[0]
        if not ici:
            # LE cas qui a mis des fiches d'essai dans le fichier d'une cliente : la
            # réservation savait quel tableau, l'agent visait l'autre. On le nomme.
            raise ClaimedRefUnresolved(
                f"`{CLAIMED_REF}` : ton travail ne tient rien dans `{namespace}` — sa "
                f"réservation porte sur {', '.join('`' + n + '`' for n in ailleurs)}. "
                "Écris dans le tableau que tu as réservé, ou réserve une ligne ici "
                "d'abord. ⚠️ Ne réessaie pas sans identifiant : sur un tableau que tu "
                "n'as pas réservé, une écriture sans identifiant CRÉE une ligne.")
        raise ClaimedRefUnresolved(
            f"`{CLAIMED_REF}` est ambigu : ton travail tient {len(ici)} lignes de "
            f"`{namespace}` ({', '.join(ici)}). Nomme celle que tu écris — en deviner "
            "une écrirait peut-être sur la mauvaise, ce qui ne se voit sur aucun écran.")

    def resolve_claimed_target(self, *, worker: Optional[str] = None) -> tuple[str, str]:
        """`@claimed` posé en TABLEAU → (tableau, ligne), tous deux lus dans la réservation.

        **Pourquoi cette forme existe** : à sa première rencontre avec des agents réels
        (29/08), `@claimed` est arrivé dans `namespace` et non dans `id` — deux écritures
        refusées sur cinq, en « namespace inconnu ». On leur retire un champ à recopier ;
        ils y mettent l'alias qu'on vient de leur apprendre. Et ils n'ont pas tort : on
        leur a dit « la réservation est l'adresse », et une adresse commence par le
        tableau.

        > **La réservation porte les deux. Refuser sur le champ voisin, c'est refuser une
        > demande qu'on sait satisfaire** — et envoyer chercher une faute de frappe là où
        > il n'y en a pas.

        L'ambiguïté se nomme ici sur DEUX dimensions : sans tableau donné, dire quelles
        lignes sont tenues sans dire où elles sont laisserait l'agent aussi démuni."""
        run = _current_run()
        if not run:
            raise ClaimedRefUnresolved(
                f"`{CLAIMED_REF}` en tableau désigne la réservation de TON travail, et cet "
                "appel n'est rattaché à aucun travail : passe `_run_id` (celui de ton "
                "`run_start`) — il n'est pas hérité —, ou nomme le tableau.")
        baux = self._baux_actifs(run, worker)
        if not baux:
            raise _refus_rien_tenu(" en tableau", run=run)
        if len(baux) == 1:
            b = baux[0]
            return str(self._ns_of(b["ns_id"]).get("namespace") or b["ns_id"]), str(b["row_id"])
        ou = ", ".join(f"`{b['row_id']}` dans "
                       f"`{self._ns_of(b['ns_id']).get('namespace') or b['ns_id']}`"
                       for b in baux)
        raise ClaimedRefUnresolved(
            f"`{CLAIMED_REF}` en tableau est ambigu : ton travail tient {len(baux)} "
            f"lignes — {ou}. Nomme le tableau, ou la ligne, ou les deux.")

    def claimed_hint(self, namespace: str) -> Optional[str]:
        """Ce que le travail courant tient — dit au moment où une ADRESSE échoue (#517).

        Le refus « introuvable » tombe précisément quand l'agent s'est trompé
        d'identifiant ou de tableau, c'est-à-dire au seul instant où il peut encore
        corriger. À cet instant, le serveur sait ce que ce travail a réservé et
        l'agent, lui, l'a manifestement perdu. Le lui rendre coûte une requête.

        None quand il n'y a rien d'utile à dire — hors run, ou aucune réservation :
        une piste vide vaut mieux qu'une phrase qui meuble."""
        ici, ailleurs = self._baux_du_run(namespace)
        if not ici and not ailleurs:
            return None
        if ici:
            return (f"ton travail tient {_backquote(ici)} dans `{namespace}` — "
                    f'écris avec `id="{CLAIMED_REF}"` plutôt que de recopier')
        return (f"ton travail ne tient rien dans `{namespace}`, mais tient une ligne "
                f"dans {_backquote(ailleurs)} — c'est peut-être le tableau que tu visais")

    @staticmethod
    def _baux_actifs(run: str, worker: Optional[str]) -> list[dict]:
        """Les baux actifs du run, restreints au libellé s'il est donné — et si le
        libellé ne retrouve rien alors que le run tient bien des lignes, on le DIT.

        Sans ça, un `data_release` rejoué avec un autre `worker` que celui du claim
        recevait « aucune réservation active » : faux — la ligne était tenue, sous un
        autre libellé — et un agent cru sans ligne en réserve une seconde pour
        retrouver la première. Une requête de plus, sur le seul chemin d'échec."""
        baux = db.datastore_active_leases_of(run_id=run, worker=worker)
        if baux or worker is None:
            return baux
        sans_libelle = db.datastore_active_leases_of(run_id=run)
        if not sans_libelle:
            return []
        tenus = sorted({str(b.get("claimed_by")) for b in sans_libelle})
        raise ClaimedRefUnresolved(
            f"`{CLAIMED_REF}` : ton travail tient bien une ligne, mais sous le libellé "
            f"{_backquote(tenus)} — pas `{worker}`. Rejoue le `worker` donné à "
            "`data_claim_next`, tel quel : c'est lui qui garde le relâchement.")

    def _baux_du_run(self, namespace: str, *, worker: Optional[str] = None):
        """Ce que le travail courant tient : ici, et ailleurs — source unique des deux
        lectures du bail-comme-adresse (#517).

        `(None, [])` distingue « pas de travail sur cet appel » de « un travail qui ne
        tient rien » : le premier se répare en passant `_run_id`, le second en réservant
        une ligne. Les confondre dirait à l'agent de faire ce qu'il a déjà fait."""
        run = _current_run()
        if not run:
            return None, []
        baux = self._baux_actifs(run, worker)
        if not baux:
            return [], []
        ns_id = self._resolve(namespace)
        ici = [str(b["row_id"]) for b in baux if b["ns_id"] == ns_id]
        ailleurs = sorted({str(self._ns_of(b["ns_id"]).get("namespace") or b["ns_id"])
                           for b in baux if b["ns_id"] != ns_id})
        return ici, ailleurs

    def queue(self, namespace: str) -> list[dict]:
        """Vue de SUPERVISION de la file (dashboard) : les rows sous bail —
        actif ou expiré, le consommateur tranche sur `_claimed_until`. Lecture
        seule (aucun droit d'écriture requis)."""
        ns_id = self._resolve(namespace)
        sch = self._schema_of(ns_id)
        return [self._row_to_dict(r, sch, bail_echu="servir")
                for r in db.datastore_claimed_rows(ns_id)]

    def force_release(self, namespace: str, row_id: str, *,
                      trace: Optional[dict] = None) -> bool:
        """Libère le bail SANS garde de worker — supervision humaine (dashboard),
        ≠ `release_claim` (agent, gardé). Exige le droit d'écriture. False = pas
        de bail à libérer."""
        ns_id = self._resolve(namespace, write=True)
        if trace is not None:
            self._trace(trace, ns_id, self._ns_of(ns_id))
        return db.datastore_release_claim(ns_id, row_id, None)

    def delete_row(self, namespace: str, row_id: str, *,
                   trace: Optional[dict] = None) -> None:
        ns_id = self._resolve(namespace, write=True)
        if trace is not None:
            # Relevé demandé : on lit l'état de la row DANS le chemin de suppression
            # (au plus près du delete), jamais par un `get_row` séparé côté route —
            # qui re-résoudrait le namespace et courrait avec un write concurrent.
            ns = self._ns_of(ns_id)
            sk = (dsv2.status_field(ns.get("schema")) or {}).get("key")
            prev = ((db.datastore_get_row(ns_id, row_id) or {}).get("data") or {}) if sk else {}
            self._trace(trace, ns_id, ns, prev_status=prev.get(sk) if sk else None)
        self._assert_writable(ns_id, row_id)
        if not db.datastore_delete_row(ns_id, row_id):
            raise RowNotFound(row_id)


def _relever_origine_module(store, ns_id, payload, avant=None,
                            schema=None, declare: bool = False) -> None:
    """Relève les colonnes dont CET appel pose la couche `origine`, et REFUSE l'appel
    qui ne les déclare pas une fois la date passée (oto#70 lot 2).

    ⚠️ **Ce qui est refusé n'est pas l'écriture, c'est le SILENCE.** `declare` porte le
    paramètre de l'appelant (`origine_override`) : avec, l'écriture passe et laisse une
    trace distincte ; sans, elle passe aussi tant que la date n'est pas atteinte, avec
    l'avertissement pour toute réponse. Après, elle est refusée par un message qui dit
    exactement ce que l'avertissement disait.

    ⚠️ **La date arme la garde toute seule**, sans déploiement (`dsv2.refus_arme()`).
    C'est ce qui a été annoncé aux écrivains à chaque écriture depuis le barreau 1 ; un
    refus qui attendrait qu'on y pense ne serait pas le préavis qu'on leur a promis.

    ⚠️ **La trace distingue les deux populations** (`declare`), et c'est elle qui dira,
    après la date, si un écrivain s'est ADAPTÉ ou a DISPARU. Deux faits que le même
    compteur confondrait : dans les deux cas les écritures non déclarées tombent à zéro.

    ⚠️ **`face` reste NULL** : le store ne connaît pas le canal d'appel — il voit un
    `sub` et une org. `ResolvedCtx.channel` vit à l'adaptateur, deux couches plus haut.
    Le DDL l'accepte, et une face inconnue vaut mieux qu'une face devinée.
    """
    from ..db import origine_ecritures as db_origine

    colonnes = dsv2.origine_posee(payload, avant)
    if not colonnes:
        return
    if not declare and dsv2.refus_arme():
        # Rien n'est relevé : rien n'a été écrit. Un refus n'est pas une écriture, et
        # le compter gonflerait la population de gens qui, précisément, n'ont pas
        # réussi à écrire.
        raise ValueError(dsv2.refus_origine(colonnes))
    store._origine_posee.update(colonnes)
    # ⚠️ Les deux populations sont relevées SÉPARÉMENT. Sur une colonne qui ne déclare
    # pas le format, la plateforme ne pose JAMAIS d'origine : celle-ci vient donc
    # forcément de l'écrivain — c'est le cas que la définition interdit, et le seul
    # qu'on cherche. Les fondre dans un total ferait disparaître la population visée
    # dans celle qui l'entoure (mesuré : 64 cellules contre 15 688).
    declarees = dsv2.system_origin_fields(schema)
    for format_declare, lot in ((False, [c for c in colonnes if c not in declarees]),
                                (True, [c for c in colonnes if c in declarees])):
        db_origine.relever(sub=getattr(store, "sub", None),
                          org_id=getattr(store, "acting_org", None),
                          ns_id=ns_id, colonnes=lot,
                          format_declare=format_declare, declare=declare)


def make_store(sub: str) -> "DatastorePg":
    """Construit un store PG pour `sub`. Plus aucune dépendance externe (ADR 0016)
    — datastore est une surface plateforme self-contained."""
    return DatastorePg(sub)


def make_org_store(org_id: int, *, allowed_ns_ids: Optional[set] = None,
                   read_only: bool = False) -> "DatastorePg":
    """Store agissant SOUS L'AUTORITÉ d'une ORG, sans user (`sub=None`). Sert un
    endpoint MCP `secret` opt-in datastore (ADR 0032) : la résolution de namespace et
    le droit d'écriture se décident sur le principal ORG (owner-match / grant d'org),
    jamais sur un membre. N'expose PAS la gouvernance (create/delete/rename/share) —
    ces actes restent réservés à un user identifié (tools sub-only).

    `allowed_ns_ids` (non None) = **scope dur** : seuls ces namespaces sont listables/
    résolvables — les tableaux LIÉS au projet partagé (anti-fuite #193 : sans ce scope
    l'endpoint exposerait TOUT le datastore de l'org). Set vide ⇒ rien d'exposé.
    `read_only=True` ⇒ l'écriture (`data_write`/`data_set_schema`) lève `NamespaceReadOnly`."""
    return DatastorePg(None, acting_org=int(org_id),
                       allowed_ns_ids=allowed_ns_ids, read_only=read_only)
