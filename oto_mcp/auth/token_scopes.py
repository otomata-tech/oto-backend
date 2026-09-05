"""Portée d'un jeton API `oto_…` — le confier sans confier l'organisation.

Un jeton API **est** le sub : le porteur peut tout ce que la personne peut. C'est
pourtant lui qu'on confie à une intégration tierce (un front client qui affiche UN
tableau) — elle reçoit l'organisation entière, plus l'identité, plus la liste des
connecteurs, plus les jetons du compte. La restriction était alors portée par le
code de l'intégration : la mauvaise couche.

Un jeton **porté** (`user_api_tokens.scopes` non NULL) inverse la posture :
**rien n'est permis sauf ce que la portée nomme**. Deux portées sont exprimables,
ensemble ou séparément — le datastore et le projet :

    {"namespaces": {"leads-accords-dormants": "read", "sorties": "write"},
     "projects": {"12": "read"}}

`read` = lire le tableau ; `write` = lire **et** écrire ses LIGNES. Ni l'un ni
l'autre n'ouvre la gouvernance (créer / supprimer / renommer / partager un tableau),
ni quoi que ce soit hors datastore (`/api/me`, `/api/me/tokens`, `/api/connectors`,
les capacités…). La table `_ALLOWED` ci-dessous est la **seule** porte : tout ce qui
n'y figure pas est refusé, y compris une route ajoutée demain — deny-by-default, pas
une denylist à tenir à jour.

`projects` ouvre **la lecture d'un projet nommé** : son brief et ses liens, de quoi
qu'une intégration parte du projet plutôt que d'un nom de tableau appris par cœur.
`write` y est refusé — aucune route de projet en écriture n'est ouverte à un jeton
porté, et accepter le mot donnerait une permission qui n'existe pas. Un projet se
nomme par son **id**, lui stable, là où un tableau se nomme par son nom.

Un jeton **sans** portée (`scopes` NULL) garde le comportement historique (pleins
pouvoirs du sub) : aucune migration, aucun jeton existant cassé.

⚠️ La portée nomme le tableau par son **nom** — ce que l'URL adresse — pas par son
id. Renommer un tableau (ou en créer un qui reprend un nom libéré) déplace donc ce
que le jeton atteint. Les deux actes sont hors de portée d'un jeton porté (ils
demandent une session interactive du propriétaire), mais après un renommage :
ré-émettre le jeton.
"""
from __future__ import annotations

import contextvars
import re
from typing import Optional
from urllib.parse import unquote

READ, WRITE = "read", "write"

# `write` contient `read` : un jeton en écriture lit aussi.
_IMPLIES = {READ: frozenset({READ}), WRITE: frozenset({READ, WRITE})}

# Portée du jeton de la requête courante — posée par `api.routes._authenticate` à
# CHAQUE requête (None comprise : jamais de valeur rémanente d'une requête voisine).
# ContextVar = par tâche asyncio, donc par requête. Lue par les handlers qui doivent
# FILTRER leur réponse (la liste des tableaux) plutôt que la refuser en bloc.
_CURRENT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "oto_token_scope", default=None)

NAMESPACES, PROJECTS = "namespaces", "projects"

# La portée du RUNNER — booléenne, et c'est ce qui la distingue des deux autres.
#
# ⚠️ Les familles ci-dessus nomment une ressource LUE DANS LE CHEMIN (un tableau,
# un projet). Les routes du runner n'en portent aucune : le même chemin sert à
# réserver, prolonger, conclure, enfiler — l'opération vit dans le CORPS. On ne
# peut donc pas les borner plus finement sans faire monter l'opération dans l'URL,
# ce qui est un changement de contrat public. `runner: true` ouvre la file, les
# flottes et le fil, ET RIEN D'AUTRE.
#
# Ce que ça vaut, mesuré le 05/09/2026 : le jeton qui faisait tourner les workers
# n'était pas porté, donc il ouvrait `/api/admin/users`, `/api/admin/platform-keys`
# et la console de supervision — non parce qu'on l'avait voulu, mais parce que
# RIEN NE LES FERMAIT. Un jeton porté `{"runner": true}` ne les atteint plus.
RUNNER = "runner"

# Les trois routes sans lesquelles un worker cesse de fonctionner — relevées dans
# son client HTTP, pas supposées : la file (réserver, prolonger, conclure, lier),
# les flottes (déclarer, armer, prendre, battre) et le fil (le rejeu d'un travail
# interrompu). Compter des lignes se demande EN PLUS, par `namespaces`.
_ALLOWED_RUNNER: tuple[tuple[re.Pattern, frozenset], ...] = (
    (re.compile(r"^/api/me/runner/jobs$"), frozenset({"POST"})),
    (re.compile(r"^/api/me/runner/fleets$"), frozenset({"POST"})),
    (re.compile(r"^/api/me/runs/thread$"), frozenset({"POST"})),
)

# La ressource nommée par la portée, capturée dans l'URL : un nom de tableau, ou
# l'id d'un projet. C'est ce que la requête ADRESSE — d'où la règle : ce qu'un
# jeton porté peut atteindre doit se lire dans le chemin, jamais dans le corps.
_RES = r"(?P<res>[^/]+)"
_ID = r"(?P<res>\d+)"

# (chemin, méthodes, permission requise, famille de portée). Disjointes.
_ALLOWED: tuple[tuple[re.Pattern, frozenset, str, str], ...] = (
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows$"), frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows$"), frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows/[^/]+$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows/[^/]+$"),
     frozenset({"PATCH", "DELETE"}), WRITE, NAMESPACES),
    # File de travail : réserver EST une écriture (le bail change la ligne), et un
    # jeton en lecture ne doit pas pouvoir retirer une ligne à ses collègues.
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/claim_next$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows/[^/]+/claim$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows/[^/]+/release$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/rows/[^/]+/activity$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/activity$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/queue$"), frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/aggregate$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/url$"), frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastore/namespaces/{_RES}/schema$"),
     frozenset({"PUT"}), WRITE, NAMESPACES),
    # Le projet nommé : son brief et ses liens. Lecture seule, et par id — la
    # capacité `oto_project` (POST /api/me/projects) reste, elle, hors de portée
    # d'un jeton porté : sa cible vit dans le corps, on ne saurait pas la borner.
    (re.compile(rf"^/api/me/projects/{_ID}$"), frozenset({"GET"}), READ, PROJECTS),
)

# Le catalogue des tableaux est LISIBLE par un jeton porté, mais FILTRÉ à sa portée
# par le handler (`ds_list_ns`) : sans lui, une intégration ne peut pas découvrir le
# schéma de son tableau (les colonnes) — `page_rows` ne le rend pas.
_FILTERED = ("GET", "/api/datastore/namespaces")


class ScopeError(ValueError):
    """Document de portée invalide (saisie de l'émetteur, jamais du porteur)."""


def _parse_namespaces(ns: object) -> dict[str, str]:
    if not isinstance(ns, dict) or not ns:
        raise ScopeError("scopes.namespaces doit être un objet non vide {nom: read|write}")
    out: dict[str, str] = {}
    for name, perm in ns.items():
        if not isinstance(name, str) or not name.strip():
            raise ScopeError("nom de tableau vide dans scopes.namespaces")
        if perm not in (READ, WRITE):
            raise ScopeError(f"permission « {perm} » sur « {name} » : attendu read|write")
        out[name.strip()] = perm
    return out


def _parse_projects(pr: object) -> dict[str, str]:
    """Un projet se nomme par son id, et ne s'ouvre qu'en lecture.

    `write` est refusé plutôt qu'ignoré : aucune route de projet en écriture n'est
    ouverte à un jeton porté, et l'accepter promettrait une permission inexistante.
    """
    if not isinstance(pr, dict) or not pr:
        raise ScopeError("scopes.projects doit être un objet non vide {id: read}")
    out: dict[str, str] = {}
    for pid, perm in pr.items():
        try:
            key = str(int(str(pid).strip()))
        except (TypeError, ValueError):
            raise ScopeError(f"« {pid} » n'est pas un id de projet") from None
        if perm != READ:
            raise ScopeError(
                f"permission « {perm} » sur le projet {key} : seul `read` existe "
                "(aucune écriture de projet n'est ouverte à un jeton porté)")
        out[key] = perm
    return out


def parse(raw: object) -> Optional[dict]:
    """Valide et normalise un document de portée à la CRÉATION du jeton.

    `None`/absent ⇒ jeton non porté (pleins pouvoirs du sub, comportement
    historique). Sinon `{"namespaces": {nom: "read"|"write"}}` et/ou
    `{"projects": {id: "read"}}`, au moins une entrée — une portée vide serait un
    jeton inerte, presque sûrement une erreur de saisie.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScopeError("scopes doit être un objet {\"namespaces\": {…}, \"projects\": {…}}")
    unknown = set(raw) - {NAMESPACES, PROJECTS, RUNNER}
    if unknown:
        raise ScopeError(f"clé(s) de portée inconnue(s) : {sorted(unknown)}")
    if not (raw.get(NAMESPACES) or raw.get(PROJECTS) or raw.get(RUNNER)):
        raise ScopeError(
            "scopes doit nommer au moins un tableau, un projet, ou le runner")

    out: dict[str, dict[str, str]] = {}
    if raw.get(NAMESPACES) is not None:
        out[NAMESPACES] = _parse_namespaces(raw.get(NAMESPACES))
    if raw.get(PROJECTS) is not None:
        out[PROJECTS] = _parse_projects(raw.get(PROJECTS))
    if raw.get(RUNNER) is not None:
        # Booléen STRICT : `"true"`, `1` ou `{}` seraient acceptés par un `if`
        # complaisant et donneraient une portée que l'émetteur n'a pas écrite.
        if raw.get(RUNNER) is not True:
            raise ScopeError("scopes.runner vaut `true`, ou rien — pas de degré")
        out[RUNNER] = True
    return out


def namespaces(scopes: Optional[dict]) -> frozenset:
    """Noms de tableaux nommés par la portée (vide si le jeton n'est pas porté)."""
    if not scopes:
        return frozenset()
    return frozenset((scopes.get(NAMESPACES) or {}).keys())


def projects(scopes: Optional[dict]) -> frozenset:
    """Ids de projets nommés par la portée (vide si le jeton n'est pas porté)."""
    if not scopes:
        return frozenset()
    return frozenset((scopes.get(PROJECTS) or {}).keys())


def authorize(scopes: Optional[dict], method: str, path: str) -> bool:
    """La requête `(method, path)` est-elle dans la portée ? Fail-closed.

    `scopes` None ⇒ jeton non porté ⇒ True (le gate ne s'applique qu'aux jetons
    portés ; les droits du sub restent seuls juges en aval).
    """
    if scopes is None:
        return True
    method = (method or "").upper()
    path = path.rstrip("/") or "/"
    if (method, path) == _FILTERED:
        return True                       # lecture filtrée par le handler
    if scopes.get(RUNNER) is True:
        for motif, methodes in _ALLOWED_RUNNER:
            if method in methodes and motif.match(path):
                return True
        # Pas de `return False` ici : un jeton peut porter `runner` ET des
        # tableaux (un ordonnanceur compte des lignes). La boucle ci-dessous
        # tranche le reste, et ce qui n'y figure pas reste refusé.
    for pattern, methods, needed, family in _ALLOWED:
        if method not in methods:
            continue
        m = pattern.match(path)
        if not m:
            continue
        granted = ((scopes or {}).get(family) or {}).get(unquote(m.group("res")))
        return granted is not None and needed in _IMPLIES[granted]
    return False


# ── Portée de la requête courante ────────────────────────────────────────────

def set_current(scopes: Optional[dict]) -> None:
    """Posée à chaque authentification REST — y compris à None (JWT, jeton non
    porté), pour qu'aucune portée ne survive à sa requête."""
    _CURRENT.set(scopes)


def current() -> Optional[dict]:
    return _CURRENT.get()


def filter_namespaces(rows: list) -> list:
    """Restreint une liste de tableaux à la portée du jeton courant (no-op hors
    jeton porté). Le catalogue est la seule réponse FILTRÉE plutôt que refusée.

    Les droits annoncés sont **rabattus** sur ceux du jeton : une entrée dit
    `permission='write'` parce que le SUB peut écrire, or c'est le jeton qui appelle.
    Un front qui peint ses boutons sur ces champs afficherait sinon une écriture que
    le serveur refusera.
    """
    scopes = current()
    if scopes is None:
        return rows
    grants = (scopes or {}).get(NAMESPACES) or {}
    out = []
    for r in rows:
        perm = grants.get((r or {}).get("namespace"))
        if perm is None:
            continue
        e = dict(r)
        e["permission"] = perm
        e["can_write"] = perm == WRITE
        e["can_govern"] = False           # un jeton porté ne gouverne jamais
        out.append(e)
    return out
