"""Datastore — stockage de données structurées légères par user (PG natif, ADR 0016).

Chaque user a son propre set de "namespaces". Schéma libre : chaque row = un
dict JSON (stocké en JSONB, types préservés), les champs apparaissent au fur et
à mesure. Trois champs auto-managés exposés à plat : `_id`, `_created_at`,
`_updated_at`. Aucune dépendance externe — surface plateforme self-contained.

Surface (« moins d'outils, plus d'args ») : `data_write`/`data_rows`/`data_share`
fondent append↔update / get↔list / share↔unshare via un arg de mode. Les
destructifs (delete_namespace, delete_row) et la création restent séparés.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, ownership
from ..datastore import claimable, jetons
from ..datastore import layers as dsl
from ..datastore import schema as dsv2
from ..datastore.core import (
    ClaimedRefUnresolved,
    indice_de_liberation,
    InvalidCursor,
    NamespaceExists,
    NamespaceForbidden,
    NamespaceNotFound,
    NamespaceReadOnly,
    RowLocked,
    RowNotFound,
    est_ref_reservation,
    make_org_store,
    make_store,
)


def _store_for(sub: str):
    return make_store(sub)


def _acting_store():
    """Store du datastore pour l'acteur courant, pour les tools NON-gouvernance
    (list/read/write/schema).

    - User authentifié (`sub`) → son store, contexte = son org active (inchangé).
    - Endpoint MCP `secret` avec opt-in datastore (ADR 0032) → store agissant SOUS
      L'ORG propriétaire du projet (sub-less), **scopé aux tableaux LIÉS au projet**
      (anti-fuite #193) et en **lecture seule** sauf opt-in write séparé.
    - Sinon (endpoint sans login SANS opt-in) → McpError « Unauthenticated ».

    Les tools de GOUVERNANCE/destructifs (create/delete/rename/share) n'utilisent PAS
    ce seam : ils gardent `current_user_sub_or_raise()` → jamais exposés sur un endpoint
    sans user identifié."""
    sub = access.current_user_sub_from_token()
    if sub:
        return make_store(sub)
    from .. import subdomain_project
    if subdomain_project.current_anon_datastore_exposed():
        return make_org_store(
            int(subdomain_project.current_anon_org()),
            allowed_ns_ids=_anon_project_tableau_ns_ids(
                subdomain_project.current_anon_project_id()),
            read_only=not subdomain_project.current_anon_datastore_writable())
    access.current_user_sub_or_raise()  # pas d'opt-in → lève « Unauthenticated »


def _anon_project_tableau_ns_ids(project_id: Optional[int]) -> frozenset:
    """Ids des namespaces LIÉS au projet (`project_links` type tableau) — le datastore
    exposé sur un endpoint partagé est scopé à CES tableaux, jamais tout le datastore de
    l'org (anti-fuite #193). Un lien tableau porte soit l'id numérique du namespace, soit
    son NOM (liens legacy d'avant la normalisation nom→id) → on résout LES DEUX formes
    contre le datastore de l'org propriétaire (`current_anon_org`). project_id None /
    erreur / aucun lien ⇒ frozenset() (rien d'exposé, jamais de fallback ouvert)."""
    from .. import subdomain_project
    if project_id is None:
        return frozenset()
    try:
        org = subdomain_project.current_anon_org()
        ids: set[int] = set()
        for l in db.list_project_links(int(project_id)):
            if l.get("target_type") != "tableau":
                continue
            ref = str(l.get("target_ref") or "").strip()
            if not ref:
                continue
            if ref.isdigit():
                ids.add(int(ref))
            elif org is not None:
                ns = db.get_datastore_namespace("org", str(org), ref)
                if ns:
                    ids.add(int(ns["id"]))
        return frozenset(ids)
    # noqa: SILENT — hint anonyme : ensemble vide plutôt qu'une liste fausse
    except Exception:  # noqa: BLE001
        return frozenset()


def _project_hint(namespace: str) -> Optional[str]:
    """Suggestion inverse run→lien (ADR 0035 B5) : écrire sous PROJET ACTIF dans un
    namespace NON lié au projet ⇒ suggérer le lien — aujourd'hui c'est de la
    discipline LLM (« pense à linker »), ici le substrat le rappelle au moment de
    l'acte. Jamais bloquant, jamais d'auto-link (le lien est une décision).
    Best-effort : toute erreur ⇒ None."""
    try:
        pid = access.current_project()
        if pid is None:
            return None
        links = db.list_project_links(int(pid))
        linked = {l.get("namespace") for l in links if l.get("target_type") == "tableau"}
        if namespace in linked:
            return None
        return (f"ce tableau `{namespace}` n'est pas lié au projet actif (#{pid}) — "
                f"si c'est une sortie du projet, lie-le : `oto_project op=link "
                f"project_id={pid} target_type=tableau target_ref=<id du namespace> "
                "(+ slot='<name>' s'il réalise un slot de procédure)`.")
    # noqa: SILENT — dette déclarée : le hint de projet disparaît en silence (#424, verdict C)
    except Exception:  # noqa: BLE001
        return None


def _omitted_run_hint(e: RowLocked) -> Optional[str]:
    """Le refus ENSEIGNE la faute la plus fréquente : `_run_id` omis (#547).

    Même seam que #515 — reformuler le refus du point de vue de l'APPELANT. Mesuré le
    29/08/2026 sur une campagne : 31 écritures refusées sur 100, **toutes** sur une
    ligne que l'appelant tenait lui-même, le jeton passé à la réservation (140/140)
    puis omis à l'écriture. Un refus qui décrit l'état du monde (« réservée par w8 »)
    laisse déduire la faute ; celui-ci la NOMME, mais seulement quand il peut la
    prouver.

    Trois conditions, toutes nécessaires :
    - l'appel ne porte AUCUN run — c'est précisément la faute ;
    - le bail est tenu par un run identifié ;
    - ce run appartient au MÊME sub que l'appelant. ⚠️ Sans ce dernier test on
      révélerait à un tiers le jeton qui lève le verrou : `_run_id` n'autorise rien,
      il NOMME (cf. `call_axes._pin_run`) — l'imprimer dans un refus adressé à
      quelqu'un d'autre ferait du verrou une étiquette.

    Best-effort : toute erreur ⇒ None. Un refus ne tombe pas parce qu'un indice manque.
    """
    try:
        from .. import session_org
        if session_org.current_call_run():
            return None                      # l'appel porte un run : autre cause
        run = getattr(e, "claimed_run", None)
        if not run:
            return None                      # bail sans run (worker seul) : rien à dire
        sub = access.current_user_sub_from_token()
        head = db.get_run_head(str(run))
        if not sub or not head or head.get("sub") != sub:
            return None                      # pas le tien : on ne nomme pas le run
        return (f"Tu n'as passé aucun `_run_id` sur cet appel, et cette ligne est tenue "
                f"par TON run `{run}` — tu l'as probablement omis : repasse "
                f"`_run_id={run}` sur CHAQUE appel jusqu'à `run_finish`, il n'est pas "
                f"hérité d'un appel au suivant.")
    # noqa: SILENT — un indice absent ne doit jamais masquer le refus lui-même
    except Exception:  # noqa: BLE001
        return None


def _row_locked_message(e: RowLocked) -> str:
    """Le texte servi pour un refus de ligne réservée : le message du refus, plus
    l'indice d'omission de `_run_id` quand il est prouvé (#547)."""
    hint = _omitted_run_hint(e)
    return f"{e} {hint}" if hint else str(e)


def _namespace_keys(store, namespace: str) -> set[str]:
    """Clés réellement présentes dans les DONNÉES du namespace (relevé borné).

    Troisième juge, après le schéma et la page : une colonne ORPHELINE — présente
    en base, sortie du schéma par un renommage — n'est ni déclarée ni forcément sur
    la page tirée. L'annoncer « inconnue, vérifie l'orthographe » désignerait encore
    une cause fausse ; elle existe, elle n'est simplement plus au format.
    Indisponible ⇒ set() (on ne se tait pas sur un doute, on garde l'accusation la
    moins coûteuse : signaler)."""
    try:
        ns_id = store._resolve(namespace)
        return set(db.datastore_row_keys(ns_id))
    # noqa: SILENT — clés de namespace illisibles ⇒ pas d'avertissement de frappe
    except Exception:  # noqa: BLE001
        return set()


def _targeted_columns(filter: Optional[dict], filters: Optional[list]) -> set[str]:
    """Les colonnes qu'un appel VISE, quelle que soit la forme employée.

    Les deux formes doivent nourrir l'avertissement anti-faute de frappe : une colonne
    mal orthographiée dans `fields` rendrait moins de lignes sans rien dire, ce qui est
    exactement le piège que cet avertissement existe pour fermer — et le rouvrir sur la
    forme NEUVE serait le rouvrir là où l'agent a le plus besoin d'aide.

    Le suffixe de couche est retiré (`email.comment` vise la colonne `email`), et les
    colonnes système sont écartées : elles ne figurent jamais dans `data`, les annoncer
    inconnues désignerait une cause fausse."""
    vise = set(filter or {})
    for f in (filters or []):
        if not isinstance(f, dict):
            continue
        cibles = f.get("fields") if f.get("fields") is not None else [f.get("field")]
        for c in (cibles if isinstance(cibles, (list, tuple)) else []):
            if isinstance(c, str) and c:
                vise.add(db.split_layer(c)[0])
    return {c for c in vise if not c.startswith("_")}


def _unknown_filter_keys(store, namespace: str, filter, filters=None) -> set[str]:
    """Clés de `filter`/`filters` absentes de TOUTES les lignes d'un échantillon du namespace
    (feedback #163 : filtre sur colonne inexistante = 0 résultat silencieux,
    indiscernable d'un « aucune ligne ne matche »). Chemin résultat-vide seulement.
    Namespace vide ou erreur ⇒ set() (rien d'affirmable, pas de faux warning).

    ⚠️ Le schéma prime sur l'échantillon, pour la même raison que la projection :
    une colonne déclarée mais peu renseignée peut manquer aux 50 lignes tirées, et
    l'annoncer inconnue enverrait chercher une faute d'orthographe qui n'existe
    pas. Un filtre légitime sur une colonne rare rend 0 ligne — c'est une réponse,
    pas un symptôme."""
    # Le schéma est lu à part : s'il est indisponible, on RETOMBE sur l'échantillon
    # au lieu d'éteindre l'avertissement. Le mettre dans le try commun ferait
    # disparaître un signal utile à la première anicroche de lecture de schéma —
    # exactement le genre de silence que ce warning existe pour combattre.
    try:
        known = set(dsv2.top_level_keys(store.get_schema(namespace)))
    # noqa: SILENT — schéma illisible ⇒ repli sur l'échantillon, l'avertissement survit
    except Exception:  # noqa: BLE001
        known = set()
    try:
        sample = store.cursor_rows(namespace, limit=50)["rows"]
        if not sample and not known:
            return set()
        for r in sample:
            known |= set(r.keys())
        unknown = {k for k in _targeted_columns(filter, filters) if k not in known}
        # Même dernier recours que la projection : une orpheline existe en base
        # sans être ni déclarée ni forcément dans l'échantillon.
        return {k for k in unknown
                if k not in _namespace_keys(store, namespace)} if unknown else set()
    # noqa: SILENT — dernier recours : colonne orpheline non déclarée, pas d'avertissement
    except Exception:  # noqa: BLE001
        return set()


def _inconnu(namespace: str, e: NamespaceNotFound) -> str:
    """« inconnu » — et, quand le tableau existe dans une autre org de l'appelant, OÙ et
    QUOI passer (#631). L'indice vient du store (`datastore/hors_org`), la même recherche
    que la face REST ; sans indice, le refus nu d'avant."""
    indice = getattr(e, "indice", None)
    return f"namespace `{namespace}` inconnu" + (f" — {indice}" if indice else "")


def _introuvable(row_id: object, piste: Optional[str]) -> str:
    """Le refus « introuvable » du chemin d'écriture (#517) — la forme se DÉCRIT, elle
    ne se montre pas.

    La première version montrait un identifiant en exemple, « cinq groupes
    hexadécimaux ». Le 29/08 à 15:24, un agent y a lu un modèle à remplir et a rendu
    `6738f4c2-57c0-43b9-9d78-XXXXXXXXXXXX` — douze X à la place du groupe qu'il ne
    connaissait pas. Un exemple dans un refus est un gabarit : on n'en met plus. Et
    quand ce qui est reçu n'a même pas la forme d'un identifiant, on le dit — c'est la
    preuve qu'il a été inventé, pas altéré."""
    try:
        uuid.UUID(str(row_id))
        forme = ""
    except (ValueError, AttributeError, TypeError):
        forme = " (et ce n'est pas la forme d'un identifiant de ligne)"
    return (f"row `{row_id}` introuvable{forme} — un identifiant de ligne est un UUID de "
            "36 caractères rendu par `data_write`/`data_claim_next` : on ne l'invente "
            "pas, on le relit dans la réponse qui l'a rendu. Pour écrire sur la ligne "
            'que tu tiens, passe `id="@claimed"`'
            + (f" ; {piste}" if piste else ""))


# Le rendu d'un claim À VIDE dit aussi ce qu'on ne fait PAS ensuite. Le 29/08 à 15:24,
# un travail a reçu `row: null` puis a écrit quand même — `@claimed`, puis un identifiant
# fabriqué. Rien n'est passé, mais le rendu du claim ne l'avait pas averti.
_HINT_RIEN_TENU = (" — tu ne tiens AUCUNE ligne : n'écris rien (ni `@claimed`, ni "
                   "un identifiant), termine ton travail (`run_finish`)")
_HINT_FILE_VIDE = ("plus rien à claim (file vide pour ce filtre, ou tout est sous bail "
                   "actif)" + _HINT_RIEN_TENU)


def _hint_file_vide(perimetre: dict, filter: Optional[dict]) -> str:
    """Rien servi : la file est vide POUR CE PÉRIMÈTRE, et il se nomme (#517) — un
    filtre qui contredit la déclaration du tableau ne doit pas se lire « file
    vide ». La suite ne change pas : l'agent ne tient rien, il n'écrit rien."""
    if not perimetre:
        return _HINT_FILE_VIDE
    return claimable.phrase_vide(perimetre, filter) + _HINT_RIEN_TENU


def _row_not_found_hint(store, namespace: str, row_id: object) -> str:
    """Message actionnable d'un lookup `id` raté (feedback #161 : le param `id`
    cherche par `_id` UUID technique ; quand le schéma déclare une clé métier —
    souvent nommée `id` — l'agent passe naturellement SA valeur et tombe sur
    « introuvable » sans piste). Si une ligne matche la clé métier, on le dit."""
    msg = f"row `{row_id}` introuvable (le param `id` cherche par `_id` technique)"
    try:
        key = store.declared_key(namespace)
        if key:
            hit = store.cursor_rows(namespace, filter={key: row_id}, limit=1)["rows"]
            if hit:
                return (f"{msg} ; une ligne a bien `{key}={row_id}` (clé métier) — "
                        f"utilise `filter={{\"{key}\": \"{row_id}\"}}`, son `_id` est "
                        f"`{hit[0].get('_id')}`")
            return f"{msg} ; pour la clé métier `{key}`, utilise `filter={{\"{key}\": …}}`"
    # noqa: SILENT — dette déclarée : le hint « ligne introuvable » disparaît (#424, verdict C)
    except Exception:  # noqa: BLE001
        pass
    return msg


def _project_row(row: dict, fields: list[str]) -> dict:
    """Projette une row sur `fields` (sous-ensemble de colonnes, feedback #191) en
    gardant TOUJOURS `_id` — sans lui l'agent ne pourrait plus adresser/mettre à jour
    la ligne. Les champs demandés absents de la row sont simplement omis."""
    if TOUT in fields:
        # `["*"]` demande TOUT — pas une colonne nommée `*`. Le jeton est légitime sur
        # `oto_doc` et sur le feed depuis toujours ; le refuser ici rendait `_id` seul à
        # un agent qui croyait demander la ligne entière (inventaire du 29/08).
        return row
    keep = set(fields)
    keep.add("_id")
    return {k: v for k, v in row.items() if k in keep}


TOUT = "*"  # `fields=["*"]` — « toutes les colonnes », le même jeton que sur oto_doc


def _adresse_reservee(store, namespace: str, id=None, *, worker=None, ligne: bool = True):
    """`@claimed` posé en tableau et/ou en ligne — le MÊME geste sur tous les verbes (#517).

    Écrit une fois plutôt que six : l'alias a été enseigné comme « la réservation est
    l'adresse », et un agent qui l'a compris l'emploie partout où il donne une adresse —
    y compris pour LIRE. Le déclarer inconnu sur le verbe voisin de celui qui l'accepte
    n'est pas une garde, c'est une incohérence.

    `ligne=False` pour les verbes qui n'adressent qu'un TABLEAU (`data_url`,
    `data_aggregate`) : y résoudre une ligne n'aurait aucun sens.

    Le refus traverse la surface en `INVALID_PARAMS` — il PORTE la conduite à tenir
    (« pose `_run_id` », « ta réservation est dans tel tableau »), et une erreur interne
    l'effacerait au moment précis où elle sert."""
    try:
        return jetons.resoudre(store, namespace, id, worker=worker, ligne=ligne,
                               resoudre_slot=_ns)
    except (ClaimedRefUnresolved, jetons.JetonMalPlace) as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))


def _ns(namespace: str) -> str:
    """Adressage par SLOT (ADR 0035 B3) : `slot:<name>` = le tableau bindé sous ce
    nom par le PROJET ACTIF (`access.resolve_slot_tableau` — erreur actionnable si
    pas de projet actif / slot non bindé / binding pendouillant, JAMAIS de fallback).
    Un nom nu passe inchangé (zéro magie sur les noms littéraux).

    Corps déplacé dans `access.resolve_namespace_ref` (source unique) : les capacités
    du datastore en ont besoin aussi, et l'avoir gardé ici a laissé `slot:` non résolu
    sur leur face MCP."""
    return access.resolve_namespace_ref(namespace)


def _destinataire(email: str, recipient_sub: str) -> dict:
    """Le compte qui va recevoir l'accès — jamais deviné.

    Face MCP du même geste que `capabilities/datastore/sharing._destinataire`.
    Les deux existent (dette assumée : `data_*` en MCP, `/api/datastore/*` en
    REST) et c'est CELLE-CI que les agents empruntent. Corriger l'autre seule
    aurait fermé la porte de derrière en laissant la principale ouverte.

    ⚠️ Une adresse ne désigne pas un compte : dix en portent deux (mesuré le
    05/09/2026). Partager sur l'une d'elles ouvrait le tableau à celui que
    `fetchone()` rendait en premier, sans que le propriétaire l'apprenne.
    """
    email = (email or "").strip()
    recipient_sub = (recipient_sub or "").strip()
    if email and recipient_sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="donne `email` OU `recipient_sub`, pas les deux : ils peuvent "
                    "désigner des comptes différents, et le partage réussirait vers "
                    "une cible que rien ne nommerait."))
    if recipient_sub:
        row = db.get_user(recipient_sub)
        if not row:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"aucun compte oto avec le sub `{recipient_sub}`"))
        return row
    if not email:
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message="`email` (ou `recipient_sub`) est requis."))
    porteurs = db.get_users_by_email(email)
    if not porteurs:
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message=f"aucun utilisateur oto avec l'email {email}"))
    if len(porteurs) > 1:
        subs = ", ".join(f"`{u['sub']}`" for u in porteurs)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"L'adresse `{email}` désigne {len(porteurs)} comptes : {subs}. "
                    "Reprends avec `recipient_sub` (sans `email`) pour dire lequel tu vises."))
    return porteurs[0]


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def data_list_namespaces() -> dict:
        """List the user's datastore namespaces (owned + shared)."""
        store = _acting_store()
        return {"namespaces": store.list_namespaces()}

    # ⚠️ Cette description a dit « unique per user » jusqu'au 04/09/2026, quand le code
    # créait des tableaux d'ORG depuis toujours. Le mensonge est réparé DEUX FOIS ce
    # jour-là : d'abord le texte (`ab6d0eff`), puis le comportement lui-même (ADR 0068,
    # le tableau naît personnel) — et c'est le second qui rend le premier obsolète.
    # Le récit vit ICI et pas dans le docstring : une description est une instruction
    # relue à chaque appel, et y CITER la formule fautive, même pour la démentir, c'est
    # la re-servir au modèle.
    # `tests/test_description_dit_le_proprietaire.py` lit le défaut réel dans le code
    # puis exige que le texte servi nomme ce défaut-là — jamais l'inverse. C'est lui qui
    # a refusé de virer au vert quand le défaut a changé, avant que ce texte ne bouge.
    @mcp.tool()
    def data_create_namespace(namespace: str) -> dict:
        """Create a new datastore namespace (PG-backed, schema-free).

        The table is PRIVATE: it belongs to you, and no one else can read it — not
        the other members of your org, not its admins. That is the default and it is
        never implicit (ADR 0068).

        ⚠️ It used to be owned by your ACTIVE ORG, readable by every member. To share
        a table with your org or a team, say so: the REST route takes
        `owner: {type: "org"|"group", id: N}`. Tables created before 2026-09-04 keep
        the owner they have.

        ⚠️ **`_org=` does NOT change the owner.** It decides which org you read and
        write under — never who owns what you create. Create a table under `_org=N`
        without an owner and it is still YOURS: every later call of yours keeps
        working, so nothing looks wrong. It shows up at the second agent, or at the
        colleague who cannot find the table and concludes it does not exist. The
        reply tells you the owner, and warns you in exactly that case.

        Args:
            namespace: kebab-case identifier, unique per owner (e.g. `timetrack`).
        """
        sub = access.current_user_sub_or_raise()
        if not namespace or not namespace.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="namespace requis"))
        if namespace.strip().lower().startswith(access.SLOT_PREFIX):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=("un slot binde un tableau EXISTANT — crée le namespace avec son "
                         "nom réel, puis binde-le au projet "
                         "(`oto_project op=link target_type=tableau … slot='<name>'`).")))
        store = _store_for(sub)
        try:
            return store.create_namespace(namespace.strip())
        except NamespaceExists:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"namespace `{namespace}` existe déjà",
            ))

    @mcp.tool()
    def data_delete_namespace(namespace: str) -> dict:
        """Delete a namespace and all its rows (irreversible). Owner (or org/platform
        admin governing it) only."""
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        store = _store_for(sub)
        try:
            store.delete_namespace(namespace)
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceForbidden:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"tu n'as pas le droit de supprimer `{namespace}`"))
        return {"ok": True, "namespace": namespace}

    @mcp.tool()
    def data_rename_namespace(namespace: str, new_name: str) -> dict:
        """Rename a namespace. Only the name changes — the id, URL/deeplink and shares
        stay stable (grants are keyed by id). Governance right required (owner, or the
        org/platform admin governing it). The new name must be free for the same owner.

        Use this to lift a name collision (e.g. two `reconcile_log` across orgs) before
        transferring/consolidating: rename one side, then transfer with `oto_resource`.

        Args:
            namespace: current namespace (or `slot:<name>` under the active project).
            new_name: the new kebab-case name (must be unique for the owner).
        """
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        if not new_name or not new_name.strip():
            raise McpError(ErrorData(code=INVALID_PARAMS, message="new_name requis"))
        if new_name.strip().lower().startswith(access.SLOT_PREFIX):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`slot:` est réservé à l'adressage — choisis un nom réel."))
        store = _store_for(sub)
        try:
            return store.rename_namespace(namespace, new_name)
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceForbidden:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"tu n'as pas le droit de renommer `{namespace}`"))
        except NamespaceExists as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    def data_set_schema(namespace: str, schema: Optional[dict] = None,
                        semantic_search: Optional[bool] = None) -> dict:
        """Declare (or clear with schema=null) a namespace's TYPED schema (ADR 0032 §6).

        A typed namespace renders as readable cards/records instead of a flat table.
        `schema` = {"fields": [{"key": str, "label"?: str, "type"?: "text|number|date|
        datetime|bool|json|object|list|url|email|enum",
        "display"?: "title", "role"?: "status|metric|note|qualif"}],
        "key"?: str, "strict"?: bool}.
        ⚠️ **An attribute nobody reads is accepted in SILENCE**, so a typo disarms the
        guard you thought you set: `read_only` instead of `readonly` locks nothing, and
        nothing says so. The reply now carries `unknown_keys_warning` naming those keys
        column by column — a warning, never a refusal, so existing schemas keep
        working. The declared attributes, and who reads each one (validator /
        front-end), are served at `GET /api/datastore/schema/keys`.
        ⚠️ This REPLACES the schema — it does not merge. Any setting absent from the
        body you send DISAPPEARS (a field note, a bound, options, the business key and
        its UNIQUE index). The response now names what it just removed
        (`declarations_effacees`, with the lost VALUES — it is the only copy left) and
        `enforced` (the validation keys THIS version applies). To EDIT a schema without
        risking part of it, use `data_patch_schema`: it merges by key and cannot
        destroy what it does not name.

        The optional top-level `"key"` names the field that is the row's BUSINESS KEY
        (e.g. "email", "siren"): EVERY write carrying that key value then UPSERTs on it
        — a single `data_write(row=…)` as much as a batch (`rows=…`) or
        `oto_upload_url` — the same key value updates the existing row instead of
        duplicating. Default is SOFT (rendering/dedup only, no write validation).
        Add `"key_required": true` to CLOSE the table: a write that designates NO
        existing row (no `id`, and no key value the table already carries) is then
        REFUSED instead of creating one. Off by default — a table often fills up
        before it has its key. Pass schema=null to switch back to free-table mode.

        PRESENTATION — the schema also DRIVES THE UI (there is no visual editor:
        this tool IS the way to configure how a table looks):
        - field ORDER in the record view = the order of `fields` here. Reorder the
          list to reorder the form.
        - `type` picks the WIDGET: `date`/`datetime` → date picker (readable, not a
          raw ISO string), `url` → compact field + open link (never a giant text
          box), `email` → mail field, `enum` (+ `options: [...]`) → dropdown,
          `bool` → true/false, `number` → numeric. Untyped fields fall back to a
          plain text box, so DECLARE the type when the rendering matters.
        - `width: "half"|"full"` = the field's width in the record form. Without it
          the width is derived from the widget — declare it to keep a stable layout.
        - `hidden: true` = keep the field OUT of the table columns by default (still
          editable in the record). Use it for opaque ids and technical fields.
        - `display: "title"` NAMES the row: that field titles the record everywhere
          the server names a line (work queue, undo, cards) instead of a raw `_id`.
          One per table.
        - `role: "status"` is what `lifecycle` attaches to — declaring a lifecycle on
          any other field is refused. `metric`/`note`/`qualif` only steer the
          dashboard's rendering (metric tiles, notes placed last).
          ⚠️ The dashboard still titles rows from `role: "title"`, which the server
          no longer reads. On a table meant to look right in BOTH, declare both until
          they converge.

        STRUCTURED RECORDS (ADR 0046 — every layer opt-in):
        - nested types: `type:"object"` + `fields:[…]` (sub-record, e.g. occupant);
          `type:"list"` + `of:<field-def>` (list of scalars or sub-records, e.g.
          contacts = list of {nom, titre, email}).
        - write validation: `field.required: true`, type conformity,
          `field.required_when: {"<field>": "<value>"}` (e.g. deliverables required
          when status="qualified"), `field.max_length: <int>` on a SCALAR field, and
          `field.pattern: "<regex>"` for its SHAPE when the size does not separate
          anything (a code, a snake_case identifier) — `re.search`, so anchor it
          yourself (`^…$`). `pattern` REQUIRES `max_length` on the same field (≤1000):
          the cost of a regex is bounded against what it reads, and oto refuses what
          it cannot price — an ambiguous repeated group, a backreference, a lookaround
          are rejected AT DECLARATION TIME, each naming why.
          Validation is active when `strict: true` or any field has required/
          required_when/max_length. A non-conforming write FAILS naming the culprit
          (max_length reports the actual length AND the bound; pattern reports the
          value it saw AND the motif).
          ⚠️ `strict` does NOT close the top level: a key no field declares still
          CREATES a free column and the value persists — it is only REPORTED, in
          `hors_schema`. That is how you explore a table before typing it, and it is
          why `strict` refuses an undeclared attribute INSIDE a declared sub-record
          but not a column beside it. Head key `"unknown_fields": "reject"` closes
          the top level too (default `"report"` = the above): the write is REFUSED
          and nothing is stored.
          Fields the caller does NOT write — one question ("whose column is this?"),
          and each refusal names the field, the reason and where the thing goes:
          `field.readonly: true` refuses a write that CHANGES the value in place
          (layers stay open — what another source says goes in `<field>.comment`);
          `field.origine: "system"` has the platform keep the previous value in
          `<field>.origine` — the value AS IT STOOD when the format was declared
          (`data_write` says what that does and does not mean); `field.system: "run.id"|"run.started_at"|"write.at"` has
          the PLATFORM write the VALUE on every write — do not send that column, it
          is stamped for you (a value you retype is what you believe, not what
          happened). Re-sending the SAME value is never a write, so re-emitting a
          record you just read always passes.
          Bound the fields meant to hold ONE short value (a job title, a city): a
          column that collects reasoning stops being groupable/filterable. The
          bound applies to the keys a write actually SETS, so rows already over it
          keep working until that field is rewritten — and setting a bound on a
          table that already overflows answers with a `warning` saying how many.
        - lifecycle: on the `role:"status"` field, `lifecycle: {states:[…],
          transitions:{from:[to…]}, terminal?:[…]}` — unknown state or undeclared
          transition is refused. ⚠️ It no longer releases the work-queue claim:
          writing a "final" state does NOT free the row (#317). Release is a gesture
          of the LOCK — data_release, or closing your run — never an inference from
          a business value.
        - work-queue ceiling: `lifecycle.max_claims: <int >= 1>` +
          `lifecycle.abandon_state: "<terminal state>"` — a row claimed that many
          times WITHOUT a successful write leaves the queue in that state, with a
          platform reason in `_abandon`. Both go together: a ceiling without an
          abandon state, or an abandon state that is not terminal, is REFUSED here.
          Counter (`_claims`) resets on the first successful write to the row.
          `lifecycle.claimable: {col: val | {op: val}}` (`filter` grammar) = the
          rows the queue SERVES: no claim hands out a row outside it, whatever
          `filter` says.

        SEMANTIC SEARCH (#67 V2.2 — opt-in per namespace): pass `semantic_search=true`
        to make this namespace's ROWS findable by MEANING via oto_search (not just exact
        words), embedding each row (has a per-row cost → off by default; enable it on the
        tables you actually search by concept). `false` turns it off and purges the
        embeddings. Passing ONLY `semantic_search` leaves the schema untouched.

        Args:
            namespace: target namespace (must exist; you must have write access).
            schema: the schema object, or null to clear it. Head key
                `unknown_fields: "report"|"reject"` decides an undeclared column's
                fate; a field may carry `readonly: true` (value locked, layers open),
                `origine: "system"` (platform-kept `<field>.origine` — the value as
                it stood WHEN THIS FORMAT WAS DECLARED, written onto every existing
                row at that moment; the name says when, not who) or
                `system: "<source>"` (platform-written value).
            semantic_search: true/false to toggle semantic row search; null = leave as is.
        """
        store = _acting_store()
        namespace = _ns(namespace)
        try:
            out: dict = {}
            # Schéma posé/effacé — sauf si l'appel ne vise QUE le toggle sémantique
            # (schema omis + semantic fourni) : on ne veut pas effacer le schéma alors.
            if schema is not None or semantic_search is None:
                out = store.set_schema(namespace, schema)
            if semantic_search is not None:
                out.update(store.set_semantic(namespace, semantic_search))
            return out or {"namespace": namespace}
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"namespace `{namespace}` partagé en lecture seule"))
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    # `data_drop_column` (purge d'une colonne morte, #296) n'est PAS ici : c'est une
    # capacité (`capabilities/datastore/columns.py`) — un verbe de plateforme naît
    # capacité, ADR 0042 §Convergence des surfaces.

    @mcp.tool()
    def data_write(namespace: str, row: dict | None = None, id: str | None = None,
                   rows: list | None = None, key: str | None = None,
                   readonly_override: bool = False,
                   origine_override: bool = False) -> dict:
        """Write one row, or a BATCH of rows in a single call.

        Layers (`valeur`/`comment`/`link`/`origine`), what a write destroys, what
        `readonly` and the business key protect, and where the REST face differs:
        guide `datastore-semantics` (`oto_guide op=read slug=datastore-semantics`).

        ⚠️ **A write DESTROYS what is in the column.** On an open column there is no
        undo and no history: the previous value is gone the moment yours lands. If
        the value was supplied by the table's owner and you overwrite it, they get
        nothing back — announce what you are about to change on a column you did not
        fill yourself.

        The safety net is `origine: "system"`, and what it keeps is precise: **the
        value as it stood when the format was declared**. Declaring the format
        writes that value into `<field>.origine` on every existing row, once, in one
        transaction — the reply says how many rows it touched. From then on, the
        layer never moves again: later writes overwrite the value, never the origin.

        Two things it does NOT mean:

        - it is not "the value the data owner supplied". If agents had already
          written before the format was declared, what is kept is what stood at
          declaration time. The name says when, not who ;
        - a column WITHOUT that format keeps nothing at all — overwriting is final,
          and nothing will tell you afterwards.

        Re-writing the SAME value changes nothing and captures nothing, by design.
        And it does not depend on how the row was created: a row appended through
        the MCP tool and one created through the REST face behave identically
        (measured 2026-09-04, both faces call the same store).

        SINGLE (`row`): WITHOUT `id` = append a NEW row (new JSON keys auto-create
        columns, unless the table is CLOSED — see below) — UNLESS the table declares
        a business `key` and your row carries a value that already exists: it then
        MERGES onto that row, exactly like a batch, and returns its `_id`. WITH `id`
        = PARTIAL update of that row (only provided fields change). Returns the row
        (with `_id`/`_created_at`/`_updated_at`).

        On a row you CLAIMED, pass `id="@claimed"` instead of retyping its `_id` —
        `namespace="@claimed"` works too (the reservation carries the table).

        BATCH (`rows` = list of dicts): write them all at once — for importing a
        dataset without round-tripping each row through your context. If a business
        KEY is in effect (the `key` arg, else the namespace's declared `schema.key`),
        every row carrying that key value UPSERTS (merges) onto the existing row of
        the same key instead of duplicating; rows without a key are appended. Returns
        a summary {inserted, updated, count, key, ids}. Use `data_set_schema` to
        declare a persistent `key`. For LARGE batches, prefer `oto_upload_url` to push
        the data out-of-band (never through your context).

        ⚠️ A table can be CLOSED by its schema (`key_required: true`, next to its
        business `key`) — `data_get_schema` says whether it is. On such a table there
        is NO append at all: a write designating no existing row (no `id`, and no key
        value the table already carries) is REFUSED, single row and batch alike, and
        nothing is created — including a key value that is simply NEW. That is a
        deliberate setting of that table, not a platform rule. To make a row EXIST
        there, it is a schema move and not a write:
        `data_patch_schema(namespace=…, key_required=false)`, your write, then
        `data_patch_schema(namespace=…, key_required=true)` to close it back.

        ⚠️ The `origine` layer is the value at the START, at IMPORT time — not
        yours. Setting it silently is refused from 2026-10-01 on: write the value
        alone (`{"col": …}`) and the origin is kept, set by the platform when it
        is missing. If your import really must set it, pass
        `origine_override=true` ON THIS CALL — there is nobody to ask, the
        parameter is the whole of it, and it applies to this call only.

        ⚠️ A COLUMN can be LOCKED by the schema (`readonly: true`) — it holds a
        value someone put there, and an ordinary write that CHANGES it is refused by
        name (writing the same value again is fine, and `<column>.comment` always
        stays open for what another source says). To REPLACE it anyway, pass
        `readonly_override=true` ON THIS CALL. It is open to the OWNER of the table
        (you, your org or your team) or to whoever GOVERNS it — a table merely SHARED
        with you in write is refused, by design. It applies to this one call and
        nothing else: there is no schema setting to reopen and therefore none to
        close back. Every forced replacement is written to the call journal (row,
        column, replaced value) next to who called.

        On a namespace with a STRICT schema, any key you write that the schema does
        NOT declare comes back in `hors_schema` (with `hors_schema_hint`): the write
        IS accepted and the value persists, but it lands in a free column that the
        interface and everything schema-driven ignore. CHECK that field after a
        write — it is how you catch a renamed field you kept writing under its old
        name. Absent = everything you wrote is in the declared format.

        ⚠️ The namespace must EXIST first (create it with `data_create_namespace`);
        writing to an unknown namespace raises "namespace inconnu" — it is NOT
        auto-created. New JSON KEYS within an existing namespace, however, do
        auto-create their columns.

        `namespace` also accepts `slot:<name>` = the table BOUND under that slot
        name by the ACTIVE project (procedures reference tables as <slot:name>;
        the project maps the name via its links). Requires an active project +
        the binding — otherwise an actionable error, never a fallback.

        Args:
            namespace: target namespace (must already exist), `slot:<name>`, or
                `@claimed` = the table your reservation is in (open run only).
            row: single-row content as a dict (JSON-encoded automatically).
            id: omit = append a new row ; provided = partial update of that `_id` ;
                `"@claimed"` = the row your run holds — it resolves only while that
                run is OPEN, `run_finish` releases what it held.
            rows: BATCH mode — a list of row dicts written in one call.
            key: business key field for batch upsert/dedup (else `schema.key`).
            readonly_override: `true` = overwrite the `readonly` columns THIS CALL
                writes, instead of being refused. Owner or governor of the table
                only ; valid for this call alone ; journaled.
            origine_override: `true` = states that this call sets the `origine`
                layer (the value at the START, at import time) knowingly. Without
                it, writing an origin is refused from 2026-10-01 on. Nothing to
                ask anyone for: the parameter is enough, and it applies to this
                call only.
        """
        store = _acting_store()
        try:
            # #517 : « la ligne que je tiens » plutôt que ses trente-deux caractères.
            # Résolu ICI, avant tout le reste, pour que le refus éventuel sorte par le
            # même chemin actionnable que les autres (ValueError → INVALID_PARAMS).
            #
            # `@claimed` en TABLEAU (29/08) : à leur première rencontre avec l'alias, les
            # agents l'ont mis là — la réservation porte les deux, refuser ici serait
            # refuser une demande qu'on sait satisfaire.
            namespace, id = _adresse_reservee(store, namespace, id)
            jetons.verifier_contenu(row)
            jetons.verifier_contenu(rows)
            if rows is not None:
                if row is not None or id is not None:
                    raise McpError(ErrorData(code=INVALID_PARAMS,
                                             message="passer `rows` (batch) OU `row`/`id`, pas les deux"))
                if not isinstance(rows, list):
                    raise McpError(ErrorData(code=INVALID_PARAMS, message="rows doit être une liste de dicts"))
                out = {"namespace": namespace,
                       **store.write_rows(namespace, rows, key=key,
                                          readonly_override=readonly_override,
                                          origine_override=origine_override)}
            else:
                if row is None:
                    raise McpError(ErrorData(code=INVALID_PARAMS,
                                             message="fournir `row` (objet) ou `rows` (liste d'objets, mode batch)"))
                if not isinstance(row, dict):
                    raise McpError(ErrorData(code=INVALID_PARAMS, message="row doit être un dict"))
                out = store.append_row(namespace, row,
                                       readonly_override=readonly_override,
                                       origine_override=origine_override) \
                    if id is None \
                    else store.update_row(namespace, id, row,
                                          readonly_override=readonly_override,
                                          origine_override=origine_override)
            # Champs posés hors du format déclaré (#294) : l'écriture est acceptée (un
            # champ libre reste un droit du contrat), mais elle n'est plus silencieuse.
            out = {**out, **store.off_schema_report()}
            hint = _project_hint(namespace)
            return {**out, "project_hint": hint} if hint else out
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` partagé en lecture seule"))
        except RowNotFound:
            # #517 : ce refus arrive au SEUL moment où l'agent peut encore corriger.
            # « Introuvable » tout court le laisse réessayer SANS identifiant — et une
            # écriture sans identifiant CRÉE une ligne au lieu d'en corriger une. On lui
            # rend donc les deux choses qui manquent : à quoi ressemble un identifiant,
            # et ce que son propre travail tient déjà.
            try:
                piste = store.claimed_hint(namespace)
            # noqa: SILENT — une piste est un bonus : échouer à la calculer ne doit jamais remplacer un refus actionnable par une erreur interne
            except Exception:  # noqa: BLE001
                piste = None
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_introuvable(id, piste)))
        except RowLocked as e:
            # #317 : un refus, pas un 500. Sans cette traduction l'agent voit « Erreur
            # interne du serveur » là où il lui faut QUI tient la ligne, JUSQU'À QUAND,
            # et COMMENT lever — vécu en production le 15/08, sur une campagne bloquée.
            # Le message de l'exception porte déjà les trois ; `_row_locked_message` y
            # ajoute la CAUSE quand elle est prouvée (`_run_id` omis, #547).
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_row_locked_message(e)))

    @mcp.tool()
    def data_claim_next(namespace: str, worker: str, filter: Optional[dict] = None,
                        lease_s: int = 900, max_claims: Optional[int] = None,
                        layers: str = "flat") -> dict:
        """Atomically claim the NEXT unprocessed row of a namespace (work queue).

        The primitive for draining a table with N parallel (sub-)agents without
        collisions: picks the oldest row whose claim lease is free or expired,
        stamps `_claimed_by`/`_claimed_until` and returns it — two concurrent
        workers never get the same row. Returns `{row: null}` when nothing is
        left to claim.

        To write or release it, pass `id="@claimed"` rather than retyping the
        returned `_id`.

        `worker` is a label YOU choose and REUSE verbatim on data_release — the
        guard so one agent cannot release another's claim.
        `filter` (exact {col: val}, e.g. {"status": "nouveau"}) selects what counts
        as claimable; it narrows the table's declared `lifecycle.claimable`, never
        widens it. The claim does NOT change the row: write your progress via
        data_write (id=…), then release it — writing a "final" status does not
        free it (#317). Release the row with `data_release` if you have it;
        otherwise finishing your run (`run_finish`) releases it. Never write your
        intent into the row (no `_action`/`_liberation` columns). The lease
        (`lease_s`, default 900s) only covers a worker that died. While you hold a
        row, nobody else can write it.

        The row carries `_claims` = how many times it has been claimed since the
        last successful write. A row claimed over and over WITHOUT a write is a
        queue running empty: past `lifecycle.max_claims` (declared on the table, or
        `max_claims` here for this pass) the server moves it to
        `lifecycle.abandon_state`, stamps `_abandon` with the reason, and STOPS
        serving it — whatever your filter says. It stays readable and repairable:
        an explicit data_write puts it back in the queue and resets the counter.
        Neither declared nor passed = no ceiling.

        ⚠️ `layers="nested"` gives the row back in the SHAPE YOU WRITE — a cell
        that carries layers comes as `{"valeur": …, "comment": …}` instead of the
        flat pair `champ` + `champ.comment`. Use it whenever you mean to write
        layers back. The flat form shows a key with a DOT, and a dot does not look
        like a field name: agents turn `effectif.comment` into `effectif_comment`,
        which creates a ghost column — or, on a table that refuses unknown columns,
        loses the whole row. This is the only read that feeds a WRITE loop, so it is
        the one where the shape matters.

        `namespace` also accepts `slot:<name>` (table bound by the active project).
        """
        store = _acting_store()
        namespace = _ns(namespace)
        warnings: list = []
        perimetre: dict = {}
        try:
            row = store.claim_next(namespace, worker=worker, filter=filter,
                                   lease_s=lease_s, max_claims=max_claims,
                                   warnings=warnings, perimetre=perimetre,
                                   layers=dsl.check(layers))
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"namespace `{namespace}` partagé en lecture seule"))
        return {"namespace": namespace, "row": row,
                **({"warning": warnings[0]} if warnings else {}),
                **({} if row else {"hint": _hint_file_vide(perimetre, filter)})}

    @mcp.tool()
    def data_release(namespace: str, id: str, worker: str) -> dict:
        """Release a claimed row — the NORMAL end of processing one row, and the
        counterpart of data_claim_next. Guarded by `worker` (same label as at claim
        time). `id="@claimed"` (or `namespace="@claimed"`) releases the row your run
        holds, without copying it — only while that run is OPEN.

        ⚠️ Call it after EVERY row you finish, not only when abandoning: writing a
        "final" status no longer frees the row (#317). If you wrap your work in
        run_start / run_finish, closing the run frees everything it held — that is
        the safety net when you forget. `namespace` also accepts `slot:<name>`."""
        store = _acting_store()
        try:
            namespace, id = _adresse_reservee(store, namespace, id, worker=worker)
            issue = store.release_claim(namespace, id, worker=worker)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"namespace `{namespace}` partagé en lecture seule"))
        # ⚠️ DEUX situations opposées partageaient ce `false` et cet indice (#517) :
        # « il n'y avait rien à rendre » (bénin) et « la ligne est à un autre travail »
        # (échec). Une flotte a branché sa borne d'arrêt dessus et s'est coupée à cinq
        # fiches sur cent, le 29/08. La réponse porte donc la RAISON — vocabulaire
        # fermé, lisible par une machine — et l'indice dit LAQUELLE des deux.
        return {"namespace": namespace, "id": id, "released": issue["released"],
                "reason": issue["reason"],
                **({} if issue["released"] else {"hint": indice_de_liberation(issue)})}

    @mcp.tool()
    def data_rows(
        namespace: str, id: str | None = None,
        filter: Optional[dict] = None, limit: int = 100,
        cursor: str | None = None, fields: Optional[list[str]] = None,
        count_only: bool = False, q: str | None = None,
        order_by: str | None = None, order_dir: str = "desc",
        filters: Optional[list[dict]] = None, layers: str = dsl.DEFAUT,
    ) -> dict:
        """Read rows. WITH `id` = the single row (by `_id`). WITHOUT `id` = one PAGE
        of rows (`filter`/`q` narrow it, `order_by` sorts it) with a stable cursor.

        Layers come back FLAT by default (`champ.origine` beside the bare name);
        `layers="nested"` returns the shape you write — guide `datastore-semantics`.
        The REST face `GET …/rows` pages by `offset` with a `total`, no cursor.

        List mode returns `{rows, count, next_cursor}`. When `next_cursor` is not null
        there are MORE rows: call again with `cursor=<next_cursor>` (same namespace/
        filter/order) to get the next page — repeat until `next_cursor` is null.

        Without `order_by` the cursor is keyset-stable (rows created meanwhile don't
        shift the paging). With `order_by` it pages by offset instead, since an
        arbitrary sort has no stable keyset — so a row inserted mid-walk can shift the
        remaining pages. Keep the SAME `order_by` across a walk: passing a cursor from
        one regime into the other is rejected rather than silently mispaged.

        Use `count_only=True` to get just the TOTAL number of (optionally filtered)
        rows — computed server-side, no rows returned — when you only need the count
        (e.g. how many leads match a filter) without pulling the data into context.

        Use `fields` to PROJECT a subset of columns when the full row is heavy and you
        only need a few (e.g. name + email + score over a large vivier): each row is
        trimmed to those columns (plus `_id`, always kept so you can still update the
        row), drastically shrinking the payload. Bump `limit` when projecting — narrow
        rows let you pull far more per page.

        Args:
            namespace: target namespace, `slot:<name>` = the table bound under
                that slot name by the ACTIVE project (actionable error if unbound),
                or `@claimed` = the table your reservation is in.
            id: `_id` of one row, or `@claimed` = the row your run holds ; omit =
                list rows.
            filter: dict `{column: value}` — exact match. A column may instead take
                ONE operator: `{"posted_at": {"gte": "2026-06-01"}}`,
                `{"author": {"contains": "sylvie"}}`, `{"status": {"ne": "traité"}}`,
                `{"idcc": {"in": ["573", "86"]}}`, `{"email": {"not_empty": true}}`.
                Ops: eq, ne, contains, in, gt, gte, lt, lte, empty, not_empty.
                The system columns are filterable too — `_updated_at`/`_created_at`
                (ops eq/ne/gt/gte/lt/lte; a plain `YYYY-MM-DD` means that WHOLE day,
                so `{"_updated_at": {"gte": "2026-08-01"}}` = touched since the 1st)
                and `_id`. Filtering happens in SQL — never pull the whole table to
                filter it yourself. (list mode only)
            filters: list of clauses, for what `filter` cannot express — ONE clause
                may target SEVERAL columns at once. A single notion often lives on
                numbered columns (`contact1_fonction`, `contact2_fonction`…): ask
                about all of them in one go by NAMING them.
                `[{"fields": ["contact1_fonction", "contact2_fonction",
                "contact3_fonction"], "op": "in", "value": ["DRH", "DAF"]}]`
                = rows where ANY of those three holds an HR/finance role, whichever
                rank carries it. `match` picks the sense: `any` (default, one column
                is enough) or `all` (every listed column) — `all` + `empty` is how you
                get "rows with NO contact at all", which is NOT the negation of the
                first. A clause may also name a single column (`{"field": …}`), and
                a `champ.origine`/`.comment`/`.link` suffix targets that layer.
                Clauses combine with AND, and with `filter`. (list mode only)
            q: free-text search across the whole row (accent-insensitive substring)
                — the way to find a row when you don't know WHICH column holds the
                word. Combines with `filter` (AND). (list mode only)
            limit: page size (default 100, list mode only).
            cursor: opaque `next_cursor` from a previous call = fetch the NEXT page.
            fields: list of column names to keep (projection) — the returned rows
                carry only these plus `_id`. Omit = full rows.
            count_only: return only `{total}` (filtered row count), no rows.
            order_by: sort column — a user field, or a system one (`_created_at`,
                `_updated_at`, `_id`). Omit = creation order. Sorting in SQL is how
                you get "the 10 most recent" or "the top scores" without pulling the
                table and sorting it yourself. (list mode only)
                Sorting honors the DECLARED type of the column: a `number` sorts
                numerically (never "10 < 2"), an `enum` sorts in its declared
                option order, a `date` chronologically. Values that don't fit the
                type (junk in a number column, a value outside the enum's options)
                go to the TAIL in both directions, alphabetically; empty cells go
                last of all. When that happens the response carries
                `order_health: {off_type, empty}` — counts over the whole filtered
                set, absent when everything conforms.
            order_dir: `desc` (default) or `asc`. Only meaningful with `order_by`.
            layers: shape of a cell that carries layers (`origine`/`comment`/`link`).
                You WRITE nested (`{"valeur": …, "origine": …}`) and, by default,
                read back FLAT — this parameter lifts that asymmetry. `flat`
                (default): `row["email"]` is the value, and each filled layer sits
                BESIDE it as `row["email.origine"]`. `nested`: `row["email"]` is
                `{"valeur": …, "origine": …, "comment": …, "link": …}` — `valeur`
                always, the other keys only when filled — i.e. the shape you WRITE
                with `data_write`. A cell without layers is the same plain value in
                both shapes. Any other value is refused. With `nested`, `fields`
                names columns (a nested cell keeps its layers); `email.origine` as
                a field name only exists in `flat`. The default WILL switch to
                `nested`, with dated notice: pass `layers` explicitly if you depend
                on one shape.
        """
        store = _acting_store()
        namespace, id = _adresse_reservee(store, namespace, id)
        try:
            jetons.verifier_champs(fields=fields, filter=filter, filters=filters)
            layers = dsl.check(layers)
            if count_only:
                return {"total": store.count_rows(namespace, filter=filter, q=q,
                                                  filters=filters)}
            if id is not None:
                row = store.get_row(namespace, id, layers=layers)
                return _project_row(row, fields) if fields else row
            page = store.cursor_rows(namespace, filter=filter, limit=limit,
                                     cursor=cursor, q=q, filters=filters,
                                     order_by=order_by, order_dir=order_dir,
                                     layers=layers)
            rows = [_project_row(r, fields) for r in page["rows"]] if fields else page["rows"]
            out = {"rows": rows, "count": len(rows),
                   "next_cursor": page["next_cursor"]}
            # Tri typé (#336) : l'écart (valeurs hors type/options, cases vides —
            # rangées en queue) se DIT, sinon le tri a l'air délibéré et ment.
            if page.get("order_health"):
                out["order_health"] = page["order_health"]
            # Projection sur des colonnes absentes de TOUTES les lignes = même piège
            # silencieux que le filter (#163) : on le signale sans bloquer.
            # ⚠️ Le SCHÉMA d'abord, l'échantillon seulement à défaut : une colonne
            # déclarée mais renseignée sur 12 lignes de 500 est absente d'une page
            # où aucune des 12 ne figure (dans une row JSONB, une colonne vide
            # n'existe pas). L'annoncer « inconnue — vérifie l'orthographe » ne
            # rate pas seulement sa cible, ça DÉSIGNE UNE CAUSE FAUSSE : l'appelant
            # relit son appel, qui est juste, et conclut que le champ n'existe pas.
            if fields and page["rows"]:
                present = {k for r in page["rows"] for k in r}
                declared = dsv2.top_level_keys(store.get_schema(namespace))
                unknown = [f for f in fields
                           if f != TOUT and f not in present and f not in declared]
                # Dernier recours AVANT d'accuser : une colonne peut n'être ni
                # déclarée ni sur cette page, et exister quand même ailleurs dans le
                # tableau (colonne orpheline d'un renommage). L'appeler « faute
                # d'orthographe » serait encore désigner une cause fausse. Le relevé
                # des clés du namespace tranche — et il ne coûte que sur ce chemin-là,
                # celui où on s'apprête à écrire un avertissement.
                if unknown:
                    unknown = [f for f in unknown
                               if f not in _namespace_keys(store, namespace)]
                if unknown:
                    out["warning"] = (
                        f"colonne(s) de `fields` inconnue(s) dans ce namespace : "
                        f"{', '.join(unknown)} — vérifie l'orthographe (absentes du résultat)")
            # 0 résultat filtré ≠ « la donnée n'existe pas » : si une clé du filter
            # n'apparaît dans AUCUNE ligne échantillonnée, c'est probablement une
            # colonne mal orthographiée — on le SIGNALE (non bloquant, feedback #163).
            if (filter or filters) and not out["rows"]:
                unknown = _unknown_filter_keys(store, namespace, filter, filters)
                if unknown:
                    out["warning"] = (
                        f"colonne(s) de filter inconnue(s) dans ce namespace : "
                        f"{', '.join(sorted(unknown))} — vérifie l'orthographe "
                        "(0 résultat peut venir de là)")
            return out
        except InvalidCursor:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`cursor` invalide (repartir sans cursor)"))
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=_row_not_found_hint(store, namespace, id)))
        except ValueError as e:  # filtre malformé / opérateur inconnu → actionnable
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool()
    def data_aggregate(
        namespace: str,
        metrics: Optional[list[dict]] = None,
        group_by: str | list[str] | None = None,
        filter: Optional[dict] = None,
        filters: Optional[list[dict]] = None,
        q: str | None = None,
    ) -> dict:
        """Aggregate rows SERVER-SIDE — stats over a whole (optionally filtered) table
        WITHOUT pulling the rows into context (feedback #191). Use this for totals and
        distributions over a large vivier (e.g. total kWc, average score, count per
        department) instead of reading 300+ rows and summing them yourself.

        `metrics` = list of `{op, field?}`; `op` ∈ count|count_rows|sum|avg|min|max
        (default `[{"op":"count"}]`). `count` without `field` = total rows;
        sum/avg/min/max require a numeric `field` and ignore non-numeric values.
        `group_by` = a column to group on (omit = one global row). Results are sorted
        by the first metric descending when grouped (so `group_by` gives you the TOP
        groups first).

        A metric may carry its OWN condition — `where`, same clauses as `filters` —
        so the total and a subset are counted in the SAME query. That is how you get a
        RATE without crossing two calls whose scopes can silently differ. Give such a
        metric a `label`, and it comes back under that name.

        `group_by` also accepts a LIST of columns: their values are POOLED, one row
        contributing one occurrence per filled column ("all ranks together"). Under a
        pooled group, `count` counts OCCURRENCES and `count_rows` counts ROWS — two
        different questions, so ask for the one you mean.

        ⚠️ Pooling is NOT a two-dimensional group-by, and `group_by: "a,b"` is not one
        either — it is REFUSED (oto#50). A comma-separated string used to be read as a
        single column name containing a comma: no row carries it, so you got 200 with
        ONE group of key `null` holding every row, and no way to tell that from empty
        data. Group on one column per call, or pass the list if you meant to pool.
        Crossing two dimensions is not served yet.

        Returns `{results: [...]}` — each entry carries the `group_by` value (when set)
        plus one key per metric (`count`, `sum_<field>`, `avg_<field>`, or `label`).

        Examples —
            - total rows matching a filter: metrics omitted, filter={"statut":"qualified"}
            - MWc by department: group_by="departement",
              metrics=[{"op":"sum","field":"kwc_estime"}, {"op":"count"}]
            - share of companies with an HR contact on ANY rank, by headcount band —
              one call, no rows pulled:
              group_by="tranche_effectif",
              metrics=[{"op":"count","label":"fiches"},
                       {"op":"count","label":"avec_rh","where":[
                          {"fields":["contact1_fonction","contact2_fonction",
                                     "contact3_fonction"],
                           "op":"in","value":["DRH","DAF"]}]}]
            - which roles appear across all contact ranks:
              group_by=["contact1_fonction","contact2_fonction","contact3_fonction"]

        Args:
            namespace: target namespace, `slot:<name>` (active project), or
                `@claimed` = the table your reservation is in.
            metrics: list of `{op, field?, where?, label?}` aggregations
                (default = count of rows).
            group_by: column to group by, or a LIST of columns whose values are
                pooled (omit = global aggregate, single row).
            filter: dict `{column: value}` exact match to scope the aggregate.
            filters: list of clauses, incl. multi-column ones — same grammar as
                `data_rows.filters`. Combines with `filter` (AND).
            q: free-text search across the whole row, to aggregate the same set a
                search shows.
        """
        store = _acting_store()
        namespace, _ = _adresse_reservee(store, namespace, ligne=False)
        try:
            jetons.verifier_champs(filter=filter, filters=filters)
            results = store.aggregate(
                namespace, group_by=group_by, metrics=metrics, filter=filter,
                filters=filters, q=q)
            return {"results": results}
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))

    @mcp.tool()
    def data_delete_row(namespace: str, id: str) -> dict:
        """Delete a row by `_id`. `namespace` accepts `slot:<name>` (active project)
        or `@claimed`; `id="@claimed"` deletes the row your run holds."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        namespace, id = _adresse_reservee(store, namespace, id)
        try:
            store.delete_row(namespace, id)
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        except NamespaceReadOnly:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"namespace `{namespace}` partagé en lecture seule"))
        except RowNotFound:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"row `{id}` introuvable"))
        except RowLocked as e:
            # #317 : un refus, pas un 500. Sans cette traduction l'agent voit « Erreur
            # interne du serveur » là où il lui faut QUI tient la ligne, JUSQU'À QUAND,
            # et COMMENT lever — vécu en production le 15/08, sur une campagne bloquée.
            # Le message de l'exception porte déjà les trois ; `_row_locked_message` y
            # ajoute la CAUSE quand elle est prouvée (`_run_id` omis, #547).
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_row_locked_message(e)))
        return {"ok": True, "id": id}

    @mcp.tool()
    def data_url(namespace: str) -> dict:
        """Return the dashboard URL of a namespace (for the user to open/edit in
        browser). `namespace` accepts `slot:<name>` (active project) or `@claimed`
        = the table your reservation is in."""
        sub = access.current_user_sub_or_raise()
        store = _store_for(sub)
        namespace, _ = _adresse_reservee(store, namespace, ligne=False)
        try:
            return {"url": store.get_url(namespace)}
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))

    @mcp.tool()
    def data_share(
        namespace: str, email: str = "", permission: str = "read", remove: bool = False,
        recipient_sub: str = "",
    ) -> dict:
        """Share (or with `remove=True`, unshare) a namespace with another oto user.
        The recipient accesses it with their own oto account.

        Identify the recipient by `email`, or by `recipient_sub` when one address
        carries several accounts — an ambiguous address is REFUSED, never guessed.

        Args:
            namespace: namespace to (un)share (must be owned by you).
            email: email of the recipient oto user.
            permission: 'read' or 'write' (default write) — when sharing.
            remove: True = revoke access instead of granting it.
            recipient_sub: the recipient's `sub`, when `email` is ambiguous.
        """
        sub = access.current_user_sub_or_raise()
        namespace = _ns(namespace)
        recipient = _destinataire(email, recipient_sub)

        # Le partage est une action de GOUVERNANCE (owner ∪ escalade roles.py).
        try:
            ns_id = _store_for(sub).resolve_ns_id(namespace)
        except NamespaceNotFound as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=_inconnu(namespace, e)))
        if not ownership.can_govern(sub, "datastore_namespace", str(ns_id)):
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message=f"tu n'as pas le droit de gérer le partage de `{namespace}`"))

        # Ce qu'on rend nomme le compte SERVI, pas l'argument reçu : appelé par
        # `recipient_sub`, `email` est vide, et « partagé avec ␣ » serait faux.
        # `..._sub` est le seul identifiant qui désigne un compte et un seul.
        cible = recipient.get("email") or recipient["sub"]

        if remove:
            removed = ownership.revoke("datastore_namespace", str(ns_id), "user", recipient["sub"])
            if not removed:
                raise McpError(ErrorData(code=INVALID_PARAMS,
                                         message=f"pas de partage actif pour {cible} sur {namespace}"))
            return {"ok": True, "namespace": namespace, "unshared_with": cible,
                    "unshared_with_sub": recipient["sub"]}

        if permission not in ("read", "write"):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="permission must be 'read' or 'write'"))
        ownership.grant("datastore_namespace", str(ns_id), "user", recipient["sub"],
                        permission, granted_by=sub)
        return {"ok": True, "namespace": namespace, "shared_with": cible,
                "shared_with_sub": recipient["sub"], "permission": permission}

    # --- MCP App : variante à interface rendue du datastore (SEP-1865) --------
    # `data_app` rend le contenu d'un namespace INLINE (carte + table triable /
    # cherchable) au lieu de seulement renvoyer un lien dashboard (`data_url`).
    # Import OPTIONNEL de prefab_ui (extra `fastmcp[apps]`) : absent → on
    # n'enregistre pas l'app, les tools JSON ci-dessus suffisent (dégradation
    # gracieuse, même pattern que foncier.py).
    try:
        from prefab_ui.components import (  # type: ignore
            Card, Column, DataTable, DataTableColumn, Heading, Text,
        )
    # noqa: SILENT — extra `apps` absent ⇒ pas d'app, les tools JSON suffisent
    except Exception:  # pragma: no cover - extra `apps` absent
        return

    _META = ("_id", "_created_at", "_updated_at")

    def _label(k: str) -> str:
        return str(k).lstrip("_").replace("_", " ").capitalize()

    def _is_scalar(v: object) -> bool:
        return isinstance(v, (str, int, float, bool)) or v is None

    def _compact(v: object, limit: int = 90) -> str:
        """Résumé 1-ligne d'une valeur imbriquée pour une cellule DataTable :
        liste → `n × {aperçu du 1er item}` ; dict → JSON compact. Tronqué."""
        try:
            if isinstance(v, list):
                head = json.dumps(v[0], ensure_ascii=False, default=str) if v else ""
                s = f"{len(v)} × {head}" if head else "0 item"
            else:
                s = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(v)
        return s if len(s) <= limit else s[: limit - 1] + "…"

    def _message_card(title: str, message: str) -> "Card":
        with Card() as card:
            with Column(gap=4):
                Heading(title)
                Text(message)
        return card

    # ── conscience du schéma v2 (ADR 0046) ───────────────────────────────────
    # Un namespace typé porte des fields imbriqués (`object`/`list` → occupant{},
    # contacts[], signaux[]) + des rôles `title`/`status` (+ lifecycle). La table
    # plate collapsait tout ça en `n × {...}` : une fiche perdait sa structure. On
    # rend donc (1) la liste avec les colonnes DANS L'ORDRE du schéma, (2) une
    # fiche seule en détail — sous-records dépliés en sous-tables.
    def _fdefs(schema: Optional[dict]) -> list:
        return [f for f in (schema or {}).get("fields") or [] if isinstance(f, dict)]

    def _role_key(schema: Optional[dict], role: str) -> Optional[str]:
        """La clé du champ qui joue ce rôle à l'écran.

        ⚠️ `title` passe par `dsv2.title_field` (#317) : la désignation vit désormais
        dans `display`, et cette fonction ne doit pas garder une seconde lecture de
        `role` — deux chemins pour la même question finissent par diverger."""
        if role == "title":
            return (dsv2.title_field(schema) or {}).get("key")
        for f in _fdefs(schema):
            if f.get("role") == role and f.get("key"):
                return f["key"]
        return None

    def _ordered_keys(schema: Optional[dict], present: list) -> list:
        """Clés présentes réordonnées selon l'ordre de déclaration du schéma ;
        les clés hors-schéma (dont les méta) sont appendues en fin. Sans schéma =
        ordre d'apparition inchangé (comportement 0016)."""
        decl = [f["key"] for f in _fdefs(schema) if f.get("key")]
        if not decl:
            return list(present)
        seen, out = set(), []
        for k in decl:
            if k in present and k not in seen:
                out.append(k); seen.add(k)
        for k in present:
            if k not in seen:
                out.append(k); seen.add(k)
        return out

    def _rows_table(records: list, *, show_meta: bool,
                    schema: Optional[dict] = None) -> None:
        """Rend une liste de dicts en DataTable triable/cherchable (cellules
        scalaires uniquement). Les colonnes méta (`_id`/`_created_at`/
        `_updated_at`) sont masquées par défaut pour une vue épurée — `data_rows`
        les expose en JSON quand il faut agir (ex. `_id` pour un update). Avec un
        `schema` v2, les colonnes suivent l'ORDRE de déclaration des fields."""
        rows, keys = [], []
        for r in records:
            row = {}
            for k, v in r.items():
                if k in _META and not show_meta:
                    continue
                if not _is_scalar(v):
                    # Sous-record / liste (schéma v2, ADR 0046) : résumé compact au
                    # lieu de dropper la colonne (une fiche sans ses contacts[] mentait).
                    v = _compact(v)
                row[k] = v
                if k not in keys:
                    keys.append(k)
            rows.append(row)
        if schema is not None:
            keys = _ordered_keys(schema, keys)
        cols = [DataTableColumn(key=k, header=_label(k), sortable=True) for k in keys]
        DataTable(columns=cols, rows=rows, search=True, paginated=len(rows) > 20, pageSize=20)

    def _status_line(schema: Optional[dict], value: object) -> None:
        """Ligne « Statut : X » enrichie du cycle de vie : (terminal) ou les
        suites autorisées, pour que l'agent sache quoi faire ensuite."""
        txt = f"Statut : {value}"
        lc = dsv2.lifecycle_of(schema)
        if lc:
            if dsv2.is_terminal_status(schema, value):
                txt += " (terminal)"
            else:
                nxt = (lc.get("transitions") or {}).get(str(value))
                nxt = nxt if isinstance(nxt, list) else ([nxt] if nxt else [])
                if nxt:
                    txt += f" — suites : {', '.join(str(s) for s in nxt)}"
        Text(txt)

    def _render_composite(key: str, value: object, fdef: Optional[dict]) -> None:
        """Déplie un field imbriqué : `list` de sous-records → sous-DataTable ;
        `list` de scalaires → puces ; `object` → paires clé/valeur. C'est le cœur
        de l'adaptation v2 (avant, un `contacts[]` finissait en `3 × {...}`)."""
        ftype = (fdef or {}).get("type")
        Heading(_label(key))
        if ftype == "list" or isinstance(value, list):
            items = value if isinstance(value, list) else []
            if not items:
                Text("(vide)")
            elif all(isinstance(it, dict) for it in items):
                _rows_table(items, show_meta=True,
                            schema=(fdef or {}).get("of"))
            else:
                for it in items:
                    Text(f"· {it if _is_scalar(it) else _compact(it, 200)}")
        elif ftype == "object" or isinstance(value, dict):
            d = value if isinstance(value, dict) else {}
            if not d:
                Text("(vide)")
            else:
                for k, v in d.items():
                    Text(f"{_label(k)} : {v if _is_scalar(v) else _compact(v, 200)}")

    def _fiche_card(record: dict, schema: Optional[dict], url: str,
                    *, show_meta: bool) -> "Card":
        """Vue DÉTAIL d'UNE fiche : titre (`display="title"`), statut+lifecycle,
        scalaires en clé/valeur, puis chaque sous-record déplié. La valeur de v2."""
        by_key = {f["key"]: f for f in _fdefs(schema) if f.get("key")}
        title_key = _role_key(schema, "title")
        status_key = _role_key(schema, "status")
        biz_key = (schema or {}).get("key")
        title = (record.get(title_key) if title_key else None) \
            or (record.get(biz_key) if biz_key else None) \
            or record.get("_id") or "Fiche"
        scalars, composites = [], []
        for k in _ordered_keys(schema, list(record.keys())):
            if k in (title_key, status_key):
                continue
            if k in _META and not show_meta:
                continue
            v = record.get(k)
            fdef = by_key.get(k)
            ftype = (fdef or {}).get("type")
            if ftype in ("object", "list") or (fdef is None and not _is_scalar(v)):
                composites.append((k, v, fdef))
            else:
                scalars.append((k, v))
        with Card() as card:
            with Column(gap=4):
                Heading(str(title))
                if status_key and record.get(status_key) is not None:
                    _status_line(schema, record.get(status_key))
                for k, v in scalars:
                    Text(f"{_label(k)} : {'' if v is None else v}")
                Text(f"éditer : {url}")
                for k, v, fdef in composites:
                    _render_composite(k, v, fdef)
        return card

    def _pick_fiche(rows: list, schema: Optional[dict], row: str) -> Optional[dict]:
        """Retrouve UNE fiche par `row` : match sur `_id`, la clé métier déclarée
        (`schema.key`), ou la valeur du field titre — le repère naturel pour l'agent."""
        biz_key = (schema or {}).get("key")
        title_key = _role_key(schema, "title")
        target = str(row)
        for r in rows:
            for probe in ("_id", biz_key, title_key):
                if probe and str(r.get(probe)) == target:
                    return r
        return None

    @mcp.tool(app=True)
    def data_app(
        namespace: str | None = None,
        filter: Optional[dict] = None,
        row: str | None = None,
        limit: int = 100,
        show_meta: bool = False,
    ):  # pas d'annotation de retour `-> Card` : avec `from __future__ import
        # annotations`, fastmcp résout les hints contre les globals du module au
        # build du schéma, or `Card` (prefab_ui) est importé LOCAL à register() →
        # NameError fatal au démarrage (data_app hors try/except de register_all,
        # crash-loop prod vécu 2026-06-28). Le corps marche par closure. Cf. #69.
        """Rendered datastore browser (MCP App / interactive card).

        Visual variant of `data_url` that renders the data INLINE instead of just
        returning a dashboard link. WITHOUT `namespace` = a table of your
        namespaces. WITH `namespace` = a sortable/searchable table of its rows,
        with an optional exact-match `filter` (same shape as `data_rows`).

        Schema-aware (datastore v2, ADR 0046): a typed namespace renders its
        columns in the declared field order, and a SINGLE fiche is shown in a
        detail view — nested `object`/`list` fields (e.g. `contacts[]`, `signaux[]`)
        are expanded as sub-tables instead of a `"3 × {...}"` blob, and the
        `status` field shows its lifecycle (terminal / next allowed states). The
        detail view opens automatically when `filter` narrows to one row, or on
        demand with `row`.

        Use when the user wants to *see* and explore datastore content (e.g. a
        watch-list or a lead fiche) without leaving the chat. For raw JSON use
        `data_rows`; to edit a row, follow the dashboard link shown on the card.

        Args:
            namespace: target namespace ; omit = list all your namespaces.
            filter: dict `{column: value}` exact match to pre-filter rows,
                e.g. `{"priorite": "P1"}`.
            row: open ONE fiche in detail view — matched against `_id`, the
                declared business key (`schema.key`), or the title field value.
            limit: max rows rendered (default 100).
            show_meta: also show the `_id`/`_created_at`/`_updated_at` columns
                (hidden by default).
        """
        sub = access.current_user_sub_or_raise()
        if namespace:
            namespace = _ns(namespace)
        store = _store_for(sub)

        if not namespace:
            spaces = store.list_namespaces()
            if not spaces:
                return _message_card(
                    "Aucun namespace",
                    "Crée-en un avec data_create_namespace, puis écris avec data_write.",
                )
            index = [
                {"namespace": s["namespace"],
                 "structure": "typée" if _fdefs(s.get("schema")) else "libre",
                 "partage": "oui" if s.get("shared") else "non",
                 "lien": s.get("url", "")}
                for s in spaces
            ]
            with Card() as card:
                with Column(gap=4):
                    Heading("Datastore")
                    Text(f"{len(spaces)} namespace(s)")
                    _rows_table(index, show_meta=True)
            return card

        try:
            rows = store.list_rows(namespace, filter=filter, limit=limit)
            url = store.get_url(namespace)
            schema = store.get_schema(namespace)
        except NamespaceNotFound:
            return _message_card(
                "Namespace introuvable",
                f"Aucun namespace « {namespace} » sur ton compte.",
            )

        # Vue DÉTAIL d'une fiche : `row` explicite, ou `filter` qui isole 1 ligne.
        fiche = None
        if row is not None:
            fiche = _pick_fiche(rows, schema, row)
            if fiche is None:
                return _message_card(
                    "Fiche introuvable",
                    f"Aucune fiche « {row} » dans « {namespace} ».",
                )
        elif filter and len(rows) == 1:
            fiche = rows[0]
        if fiche is not None:
            return _fiche_card(fiche, schema, url, show_meta=show_meta)

        suffix = f" (filtre {filter})" if filter else ""
        with Card() as card:
            with Column(gap=4):
                Heading(namespace)
                Text(f"{len(rows)} ligne(s){suffix} · éditer : {url}")
                if rows:
                    _rows_table(rows, show_meta=show_meta, schema=schema)
                elif filter:
                    Text("Aucune ligne pour ce filtre.")
                else:
                    Text("Aucune ligne.")
        return card
