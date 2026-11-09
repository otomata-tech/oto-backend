"""Portée d'un jeton API `oto_…` — le confier sans confier l'organisation.

Un jeton API **est** le sub : le porteur peut tout ce que la personne peut. C'est
pourtant lui qu'on confie à une intégration tierce (un front client qui affiche UN
tableau) — elle reçoit l'organisation entière, plus l'identité, plus la liste des
connecteurs, plus les jetons du compte. La restriction était alors portée par le
code de l'intégration : la mauvaise couche.

Un jeton **porté** (`user_api_tokens.scopes` non NULL) inverse la posture :
**rien n'est permis sauf ce que la portée nomme**. Deux portées sont exprimables,
ensemble ou séparément — le datastore et le projet :

    {"namespaces": {"204": "read", "317": "write"},
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
porté, et accepter le mot donnerait une permission qui n'existe pas.

Un jeton **sans** portée (`scopes` NULL) garde le comportement historique (pleins
pouvoirs du sub) : aucune migration, aucun jeton existant cassé.

**Tableau et projet se nomment par leur IDENTIFIANT** (oto#158, 29/09/2026). La
portée nommait le tableau par son nom, comparé littéralement au chemin : un
renommage — ou un tableau qui reprenait un nom libéré — déplaçait ce que le jeton
atteignait. L'identifiant ne bouge pas. L'émission reçoit encore un nom et le range
sous son identifiant (`capabilities/api_tokens`) ; une URL qui adresse le tableau par
son nom se juge sur l'identifiant que ce nom résout (`api.base`), le temps du
préavis (`deprecations.RETRAIT_NOM_DE_TABLEAU`). ⚠️ Une portée déjà émise qui nomme
encore un tableau par son NOM n'ouvre rien et le DIT (`noms_de_tableau`) : elle se
migre (`scripts/tableaux_par_numero.py`), elle ne se devine pas.
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
# L'id du jeton de la requête courante, posé avec sa portée : un jeton ÉMETTEUR se
# nomme parent de ce qu'il émet, et ne voit que ses propres enfants.
_JETON: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "oto_token_id", default=None)

# ⚠️ **Ces deux valeurs sont PERSISTÉES dans les jetons déjà émis.** Ce ne sont pas
# des mots de vocabulaire : `"namespaces"` a survécu au renommage de `namespace` en
# `datastore` (08/09/2026) parce qu'un jeton émis hier porte cette chaîne, et qu'un
# scope qui ne correspond plus ne lève pas d'erreur — il n'autorise simplement plus
# rien. Le mode d'échec est un ensemble vide, et un ensemble vide ressemble à un
# succès partout où on le regarde. Les renommer exige de réémettre les jetons.
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

# La portée d'ÉMISSION — le jeton qui fabrique des jetons, sans humain à chaque clé
# (décision d'Alexis, 08/10/2026, qui renverse « émettre un jeton reste un acte
# humain »). Sa valeur est un PLAFOND : une portée `namespaces`/`projects`, ou des
# orgs entières (`ORGS`), et rien d'autre, que chaque jeton émis doit tenir (`inclus`).
#
#     {"issue": {"namespaces": {"204": "write"}}}
#
# Le motif de l'ancienne règle — une fuite qui s'auto-entretient — est tenu par ce
# que l'émetteur NE PEUT PAS faire : émettre un jeton non porté, un jeton émetteur, un
# jeton runner, un jeton hors de son plafond ou sans échéance ; et par ce que sa
# révocation emporte : tous ses enfants (`db.tokens`). Lui-même ne naît que d'une
# session humaine, et jamais sans échéance (`capabilities/api_tokens`).
ISSUE = "issue"

# Le plafond « TOUTE UNE ORG » (décision d'Alexis, 08/10/2026 : choisir les tableaux un
# par un pour un émetteur était le geste même qu'il voulait supprimer). Il ne vaut QUE
# dans un plafond, jamais dans une portée : l'enfant nomme toujours ses tableaux et ses
# projets, et chacun doit VIVRE dans l'org — appartenir à l'org ou à l'une de ses
# équipes. Un objet personnel n'y est jamais, même rangé dans l'org : l'émetteur d'org
# n'atteint pas les objets personnels de son propriétaire. L'org où vit chaque objet se
# lit en base, par l'appelant (`inclus(…, orgs_des=…)`) ; ce module reste pur.
#
#     {"issue": {"orgs": {"2": "write"}}}
ORGS = "orgs"

# Ce qu'ouvre `issue` : SES jetons — émettre, lister ses enfants, révoquer un enfant.
# Le handler borne la liste et la révocation aux enfants de l'émetteur (`emetteur`).
_ALLOWED_EMISSION: tuple[tuple[re.Pattern, frozenset], ...] = (
    (re.compile(r"^/api/me/tokens$"), frozenset({"GET", "POST"})),
    (re.compile(r"^/api/me/tokens/[^/]+$"), frozenset({"DELETE"})),
)

# Les trois routes sans lesquelles un worker cesse de fonctionner — relevées dans
# son client HTTP, pas supposées : la file (réserver, prolonger, conclure, lier),
# les flottes (déclarer, armer, prendre, battre) et le fil (le rejeu d'un travail
# interrompu). Compter des lignes se demande EN PLUS, par `namespaces`.
_ALLOWED_RUNNER: tuple[tuple[re.Pattern, frozenset], ...] = (
    (re.compile(r"^/api/me/runner/jobs$"), frozenset({"POST"})),
    (re.compile(r"^/api/me/runner/fleets$"), frozenset({"POST"})),
    (re.compile(r"^/api/me/runs/thread$"), frozenset({"POST"})),
)

# Le CYCLE du run en REST (oto#227) : l'ouvrir, le clore. Ces deux routes ne désignent
# aucun tableau dans leur chemin, mais elles n'ont de raison d'être que pour réserver
# puis écrire des lignes — la même famille que `claim_next` et `release`. Elles
# s'ouvrent donc au jeton qui ÉCRIT au moins un tableau, et à lui seul : ni famille
# nouvelle, ni identité nouvelle (décision du 13/09/2026 : pas de second jeton de bail).
# ⚠️ Un run ne donne AUCUN droit sur une ligne : la portée par tableau et la garde du
# bail restent seules juges de chaque réservation et de chaque écriture.
_ALLOWED_CYCLE_DU_RUN: tuple[tuple[re.Pattern, frozenset], ...] = (
    (re.compile(r"^/api/me/runs$"), frozenset({"POST"})),
    (re.compile(r"^/api/me/runs/[^/]+$"), frozenset({"PATCH"})),
)


def _ecrit_un_tableau(scopes: dict) -> bool:
    """Le jeton porte-t-il le droit d'écrire sur au moins un tableau ?"""
    return any(WRITE in _IMPLIES[droit]
               for droit in (scopes.get(NAMESPACES) or {}).values())

# La ressource nommée par la portée, capturée dans l'URL : un nom de tableau, ou
# l'id d'un projet. C'est ce que la requête ADRESSE — d'où la règle : ce qu'un
# jeton porté peut atteindre doit se lire dans le chemin, jamais dans le corps.
_RES = r"(?P<res>[^/]+)"
_ID = r"(?P<res>\d+)"

# (chemin, méthodes, permission requise, famille de portée). Disjointes.
_ALLOWED: tuple[tuple[re.Pattern, frozenset, str, str], ...] = (
    (re.compile(rf"^/api/datastores/{_RES}/rows$"), frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/rows$"), frozenset({"POST"}), WRITE, NAMESPACES),
    # Le lot (`POST …/rows/batch`, upsert par `key`) écrit les MÊMES lignes du MÊME
    # tableau que `POST …/rows` — il manquait : un jeton en écriture se prenait le
    # refus « gouvernance » sur le geste que le guide lui indique pour écrire en lot.
    (re.compile(rf"^/api/datastores/{_RES}/rows/batch$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/rows/[^/]+$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/rows/[^/]+$"),
     frozenset({"PATCH", "DELETE"}), WRITE, NAMESPACES),
    # File de travail : réserver EST une écriture (le bail change la ligne), et un
    # jeton en lecture ne doit pas pouvoir retirer une ligne à ses collègues.
    (re.compile(rf"^/api/datastores/{_RES}/claim_next$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/rows/[^/]+/claim$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/rows/[^/]+/release$"),
     frozenset({"POST"}), WRITE, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/rows/[^/]+/activity$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/activity$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/queue$"), frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/aggregate$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/url$"), frozenset({"GET"}), READ, NAMESPACES),
    # ⚠️ Le schéma se LIT avant de s'écrire, et la lecture manquait : `PUT` était
    # ouvert, `GET` non. On pouvait donc poser un schéma sans pouvoir le
    # consulter — personne ne décide ça, c'était un oubli, et l'asymétrie le
    # prouve (écrire est plus fort que lire).
    #
    # Ce que ça coûtait : le guide servi aux agents leur dit de lire le
    # schéma AVANT d'écrire — c'est ce qui fait qu'une longueur maximale est un
    # contrat et pas une consigne. Un agent porté qui obéissait se prenait un
    # refus sur le geste exact qu'on lui demandait.
    (re.compile(rf"^/api/datastores/{_RES}/schema$"),
     frozenset({"GET"}), READ, NAMESPACES),
    (re.compile(rf"^/api/datastores/{_RES}/schema$"),
     frozenset({"PUT", "PATCH"}), WRITE, NAMESPACES),
    # Le projet nommé : son brief et ses liens. Lecture seule, et par id — la
    # capacité `oto_project` (POST /api/me/projects) reste, elle, hors de portée
    # d'un jeton porté : sa cible vit dans le corps, on ne saurait pas la borner.
    (re.compile(rf"^/api/me/projects/{_ID}$"), frozenset({"GET"}), READ, PROJECTS),
)

# Le catalogue des tableaux est LISIBLE par un jeton porté, mais FILTRÉ à sa portée
# par le handler (`ds_list_ns`) : sans lui, une intégration ne peut pas découvrir le
# schéma de son tableau (les colonnes) — `page_rows` ne le rend pas.
_FILTERED = ("GET", "/api/datastores")


class ScopeError(ValueError):
    """Document de portée invalide (saisie de l'émetteur, jamais du porteur)."""


def _parse_namespaces(ns: object) -> dict[str, str]:
    """Les clés sont encore des noms OU des identifiants : l'émission range un nom
    sous son identifiant, et c'est elle qui sait lequel (`api_tokens`)."""
    if not isinstance(ns, dict) or not ns:
        raise ScopeError(
            "scopes.namespaces doit être un objet non vide {identifiant: read|write}")
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
    unknown = set(raw) - {NAMESPACES, PROJECTS, RUNNER, ISSUE}
    if unknown:
        raise ScopeError(f"clé(s) de portée inconnue(s) : {sorted(unknown)}")
    if not (raw.get(NAMESPACES) or raw.get(PROJECTS) or raw.get(RUNNER)
            or raw.get(ISSUE)):
        raise ScopeError(
            "scopes doit nommer au moins un tableau, un projet, le runner, ou un "
            "plafond d'émission")

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
    if raw.get(ISSUE) is not None:
        out[ISSUE] = _parse_plafond(raw.get(ISSUE))
    return out


def _parse_orgs(brut: object) -> dict[str, str]:
    if not isinstance(brut, dict) or not brut:
        raise ScopeError("scopes.issue.orgs doit être un objet non vide {id: read|write}")
    out: dict[str, str] = {}
    for oid, perm in brut.items():
        try:
            cle = str(int(str(oid).strip()))
        except (TypeError, ValueError):
            raise ScopeError(f"« {oid} » n'est pas un id d'org") from None
        if perm not in (READ, WRITE):
            raise ScopeError(f"permission « {perm} » sur l'org {cle} : attendu read|write")
        out[cle] = perm
    return out


def _parse_plafond(brut: object) -> dict:
    """Le plafond d'un jeton émetteur : des tableaux, des projets, des orgs entières
    (`ORGS`), rien d'autre — ni `runner`, ni `issue` (un émetteur n'émet pas
    d'émetteur)."""
    if not isinstance(brut, dict):
        raise ScopeError("scopes.issue doit être un objet {\"orgs\": {…}, "
                         "\"namespaces\": {…}, \"projects\": {…}}")
    hors = set(brut) - {NAMESPACES, PROJECTS, ORGS}
    if hors:
        raise ScopeError(f"scopes.issue ne porte que orgs, namespaces et projects, pas "
                         f"{sorted(hors)}")
    reste = {k: v for k, v in brut.items() if k != ORGS}
    plafond = parse(reste) if reste else {}
    if brut.get(ORGS) is not None:
        plafond[ORGS] = _parse_orgs(brut[ORGS])
    if not plafond:
        raise ScopeError("scopes.issue doit nommer au moins une org, un tableau ou un "
                         "projet")
    return plafond


def inclus(portee: Optional[dict], plafond: dict,
           orgs_des: Optional[dict] = None) -> bool:
    """`portee` tient-elle dans `plafond` ? Chaque tableau et projet qu'elle nomme y
    figure — nommément, ou par l'org où il vit (`ORGS`) — avec un droit que celui du
    plafond contient (`write` contient `read`). Une portée nulle (pleins pouvoirs) ou
    qui porte autre chose que des tableaux et des projets n'y tient jamais.

    `orgs_des` = `{famille: {clé: id d'org | None}}`, l'org où vit chaque ressource
    nommée, lue par l'appelant. Absente, ou `None` pour une ressource : celle-ci ne
    tient que par le plafond nommé."""
    if not portee or set(portee) - {NAMESPACES, PROJECTS}:
        return False
    par_org = plafond.get(ORGS) or {}
    for famille in (NAMESPACES, PROJECTS):
        accorde = plafond.get(famille) or {}
        lieux = (orgs_des or {}).get(famille) or {}
        for cle, droit in (portee.get(famille) or {}).items():
            org = lieux.get(cle)
            plafonds = (accorde.get(cle),
                        par_org.get(str(org)) if org is not None else None)
            if not any(p and droit in _IMPLIES[p] for p in plafonds):
                return False
    return True


def namespaces(scopes: Optional[dict]) -> frozenset:
    """Identifiants des tableaux nommés par la portée (vide si le jeton n'est pas porté)."""
    if not scopes:
        return frozenset()
    return frozenset((scopes.get(NAMESPACES) or {}).keys())


def projects(scopes: Optional[dict]) -> frozenset:
    """Ids de projets nommés par la portée (vide si le jeton n'est pas porté)."""
    if not scopes:
        return frozenset()
    return frozenset((scopes.get(PROJECTS) or {}).keys())


def authorize(scopes: Optional[dict], method: str, path: str,
              id_du_tableau: Optional[int] = None) -> bool:
    """La requête `(method, path)` est-elle dans la portée ? Fail-closed.

    `scopes` None ⇒ jeton non porté ⇒ True (le gate ne s'applique qu'aux jetons
    portés ; les droits du sub restent seuls juges en aval). `id_du_tableau` : ce que
    RÉSOUT le nom de tableau écrit dans le chemin (`tableau_du_chemin`), résolu par
    l'appelant — un nom qui ne résout rien n'ouvre rien.
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
    if scopes.get(ISSUE):
        for motif, methodes in _ALLOWED_EMISSION:
            if method in methodes and motif.match(path):
                # Fail-closed : sans l'id du jeton, l'émetteur ne se distinguerait plus
                # d'une session humaine dans les handlers (`emetteur()`).
                return _JETON.get() is not None
    for motif, methodes in _ALLOWED_CYCLE_DU_RUN:
        if method in methodes and motif.match(path):
            return _ecrit_un_tableau(scopes)
    for pattern, methods, needed, family in _ALLOWED:
        if method not in methods:
            continue
        m = pattern.match(path)
        if not m:
            continue
        res = unquote(m.group("res"))
        if family == NAMESPACES and not res.isdigit():
            # Un NOM dans le chemin : c'est l'identifiant qu'il résout qui est jugé.
            res = str(id_du_tableau) if id_du_tableau is not None else ""
        granted = ((scopes or {}).get(family) or {}).get(res)
        return granted is not None and needed in _IMPLIES[granted]
    return False


def tableau_du_chemin(method: str, path: str) -> Optional[str]:
    """Le tableau qu'adresse une route ouverte aux jetons portés — tel qu'il est écrit
    dans le chemin (identifiant ou nom), `None` si la route n'en adresse aucun."""
    method = (method or "").upper()
    path = (path or "").rstrip("/") or "/"
    for pattern, methods, _needed, family in _ALLOWED:
        if family == NAMESPACES and method in methods and (m := pattern.match(path)):
            return unquote(m.group("res"))
    return None


def noms_de_tableau(scopes: Optional[dict]) -> list:
    """Les tableaux qu'une portée déjà émise nomme encore par leur NOM — vide si elle
    n'en nomme que par identifiant. Une telle portée n'ouvre plus ces tableaux."""
    return sorted(k for k in ((scopes or {}).get(NAMESPACES) or {})
                  if not str(k).isdigit())


def motif_du_refus(scopes: Optional[dict], method: str, path: str) -> tuple[str, str]:
    """POURQUOI la requête est hors portée — le refus doit cesser de se contredire.

    ⚠️ Deux situations très différentes finissaient dans le même message, qui
    listait les tableaux ouverts. Refuser une requête SUR un tableau ouvert en le
    nommant comme autorisé fait conclure au lecteur que son jeton est cassé —
    c'est le pire des deux états, quel que soit le correctif.

    Rend `("ressource", <nom>)` quand le GESTE est ouvert aux jetons portés mais
    que cette ressource-là n'est pas dans la portée (ou pas avec ce droit), et
    `("geste", "")` quand aucune entrée n'ouvre ce couple méthode+chemin — la
    portée n'y peut rien, c'est le geste qui est fermé.
    """
    method = (method or "").upper()
    path = (path or "").rstrip("/") or "/"
    for motif, methodes in _ALLOWED_CYCLE_DU_RUN:
        if method in methodes and motif.match(path):
            return "ecriture", ""
    for pattern, methods, _needed, _family in _ALLOWED:
        if method in methods and (m := pattern.match(path)):
            return "ressource", unquote(m.group("res"))
    return "geste", ""


# ── Portée de la requête courante ────────────────────────────────────────────

def set_current(scopes: Optional[dict], token_id: Optional[int] = None) -> None:
    """Posée à chaque authentification REST — y compris à None (JWT, jeton non
    porté), pour qu'aucune portée ne survive à sa requête.

    ⚠️ Une portée `issue` exige `token_id` : sans lui, `emetteur()` rendrait None et
    le handler prendrait le jeton émetteur pour une session humaine."""
    if scopes and scopes.get(ISSUE) and token_id is None:
        _CURRENT.set(None)
        _JETON.set(None)
        raise ValueError("portée `issue` posée sans l'id de son jeton")
    _CURRENT.set(scopes)
    _JETON.set(token_id)


def current() -> Optional[dict]:
    return _CURRENT.get()


def emetteur() -> Optional[tuple[int, dict]]:
    """`(id, plafond)` quand la requête vient d'un jeton ÉMETTEUR, None sinon (session
    humaine, jeton sans portée d'émission)."""
    scopes, jeton = _CURRENT.get(), _JETON.get()
    if not scopes or not scopes.get(ISSUE) or jeton is None:
        return None
    return jeton, scopes[ISSUE]


def filter_datastores(rows: list) -> list:
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
        # ⚠️ La CLÉ DE SCOPE reste `namespaces` — elle vit dans les jetons déjà émis —
        # et elle porte l'IDENTIFIANT du tableau, pas son nom (oto#158).
        perm = grants.get(str((r or {}).get("id")))
        if perm is None:
            continue
        e = dict(r)
        e["permission"] = perm
        e["can_write"] = perm == WRITE
        e["can_govern"] = False           # un jeton porté ne gouverne jamais
        out.append(e)
    return out
