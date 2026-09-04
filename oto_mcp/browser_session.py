"""Connexion par **session navigateur hébergée** (Browserbase) — seam partagé.

Pour un connecteur d'API privée cookie-bound (brevo, crunchbase…), la connexion = un
**login interactif** dans une Live View Browserbase : l'utilisateur se logue une fois
(SSO/captcha/2FA), sa session naît native dans un **Context** persistant qui devient le
credential du coffre. PAS de capture de cookie, PAS d'export, PAS de MCP requis.

Ce module factorise le flux pour qu'il soit servi par DEUX surfaces avec un seul corps
de logique (derive-don't-duplicate) :
- **REST** (dashboard) — bouton « Connecter » → Live View affichée en iframe (la voie
  produit) ;
- **MCP** (`<name>_connect_start`/`_connect_status`) — même flux depuis Claude.

`start()` est générique (aucune donnée par connecteur). Seule la **vérification du login**
diffère (cookie attendu vs sonde d'API) → chaque connecteur enregistre son `verify`.
Le substrat Browserbase lui-même vit dans `browserbase.py` (seam à sens unique, ADR 0004).

⚠️ **Ce flux est piloté par des AGENTS, pas par des humains qui liront les logs.** Un
agent n'a que le retour ; ce qui n'y est pas dit n'existe pas pour lui, et il comble par
la supposition la plus courante — « session expirée », donc « je recommence ». Trois
règles en découlent, portées par `Verdict` / `FinalizeResult` et à tenir dans TOUT
connecteur à session : (1) un refus dit son MOTIF (`reason`), jamais un booléen nu ;
(2) une panne de notre côté dit `retry=False` — recommencer ne peut pas aboutir ;
(3) une session capturée mais non vérifiable est persistée et ANNONCÉE (`warning`),
jamais présentée comme un échec de connexion. Vécu le 2026-09-03 : six reconnexions
d'affilée sur une session valide, une matinée perdue chez une cliente.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, NamedTuple

from . import browserbase, db

logger = logging.getLogger(__name__)

# verify(session_id) -> Verdict : la session est-elle loguée, et SINON POURQUOI (vérifié
# sur la session VIVANTE, jamais sur un export de cookie — cf. leçons ADR 0026). Un
# verify qui ne peut pas TRANCHER lève `ProbeUnavailable` — il ne rend pas « pas logué ».
# Variante account-aware (connecteur générique) : verify(session_id, account) — le
# site à vérifier vient de l'appel, cf. `register(..., account_aware=True)`.
Verify = Callable[..., Awaitable["Verdict"]]

# Motifs de refus, stables — c'est le CODE que lit l'agent appelant, le texte n'étant
# qu'une glose. Trois causes, trois conduites : finir de se loguer dans la fenêtre /
# recommencer le login parce qu'il a été rejeté / ne PAS recommencer, ça vient de nous.
LOGGED_IN = "logged_in"
NO_SESSION = "no_session"              # rien ne prouve un login : l'humain n'a pas fini
AUTH_REJECTED = "auth_rejected"        # 401/403, page de login : identifiants refusés
PROBE_UNAVAILABLE = "probe_unavailable"  # la SONDE est en panne, pas l'utilisateur
FORCED = "forced"                      # persisté sans vérification, à la demande

_REGISTRY: dict[str, Verify] = {}

# URL de login par connecteur : la page vers laquelle amener la session dès l'ouverture
# de la Live View (sinon `about:blank`, l'utilisateur ne sait pas où se loguer). Optionnel
# — un connecteur sans URL enregistrée ouvre une page vierge (comportement historique).
_LOGIN_URLS: dict[str, str] = {}

# Connecteurs GÉNÉRIQUES (N sites sous un même connecteur) : leur `verify` prend
# `(session_id, account)` et leur `login_url` vient de l'appel. Cf. `register`.
_ACCOUNT_AWARE: set[str] = set()

# Sessions ÉMISES par `start()`, liées au `sub` qui les a demandées : `finalize` n'accepte
# qu'un (context_id, session_id) qu'IL a émis pour CE user (anti-IDOR : empêche de
# persister le Context — donc la session loguée — d'un tiers). In-memory : le serveur est
# mono-worker (cf. CLAUDE.md) et start→finalize vivent dans le même process à quelques
# minutes d'intervalle ; un restart entre les deux = re-cliquer « Connecter » (rare).
_PENDING: dict[tuple[str, str, str], float] = {}
_PENDING_TTL = 1000.0  # > keep-alive de session Browserbase (900 s)


class SessionError(RuntimeError):
    """Erreur actionnable du flux de connexion (Browserbase indispo, vérif KO…).
    Son message est rendu au client → ne JAMAIS y interpoler une exception brute
    (peut contenir l'URL CDP avec `?apiKey=…`) : logguer le détail, message propre."""


class ProbeUnavailable(RuntimeError):
    """La sonde de login n'a rendu AUCUN verdict — endpoint sondé disparu (404),
    réponse d'une forme inattendue. Ce n'est pas « pas logué », c'est « je ne sais pas ».

    ⚠️ Les confondre casse TOUT le connecteur. Le 2026-09-03 : la sonde de
    `pennylaneged` tapait la route MÉTIER `/crm/flow_companies` ; Pennylane l'a
    déplacée sous `/portfolio/`, elle a répondu 404, `verify` a rendu False et
    `finalize` est sorti AVANT `_persist()` — plus aucune cliente ne pouvait connecter
    sa GED, et le message accusait l'authentification (une matinée perdue).

    Une sonde muette ne doit donc pas rendre un connecteur inconnectable. Mais elle ne
    doit pas non plus passer inaperçue : `finalize` persiste ET rend l'anomalie à
    l'appelant dans `FinalizeResult.warning`, en la logguant en ERROR. On ne masque
    rien — on distingue, et on le dit."""


class Verdict(NamedTuple):
    """Ce que rend une sonde de login — pas un booléen nu.

    ⚠️ **Un connecteur parle à des AGENTS.** Un `False` sans motif ne leur laisse
    qu'une conduite : recommencer. Le 2026-09-03 une cliente a ainsi relancé six fois
    la connexion de sa GED sur une session qui était valide depuis le début. Les trois
    causes d'un refus appellent trois conduites OPPOSÉES — finir de se loguer dans la
    fenêtre, refaire un login rejeté, ou surtout ne PAS recommencer — donc elles se
    distinguent ici, dans le retour, ou elles ne se distinguent nulle part.

    `retry=False` veut dire : inutile de repasser par la Live View, le problème n'est
    pas chez l'utilisateur."""
    connected: bool
    reason: str
    detail: str = ""
    retry: bool = True


class FinalizeResult(NamedTuple):
    """Issue de `finalize`. `connected=False` = rien n'a été écrit, et `reason`/`detail`
    disent POURQUOI (cf. `Verdict`). `warning` non vide = persisté SANS confirmation du
    login, la sonde étant hors service (cf. `ProbeUnavailable`) — à répercuter, pas à
    avaler : la session est probablement bonne, mais rien ne l'atteste."""
    connected: bool
    reason: str = LOGGED_IN
    detail: str = ""
    retry: bool = True
    warning: str = ""


async def sonder(connector: str, context_id: str, account: str = "") -> "Verdict":
    """Exécute la sonde de login d'un connecteur sur un contexte DONNÉ — y compris
    celui de quelqu'un d'autre (oto-backend#863).

    Le manque qu'elle comble : quand une personne signale que son connecteur à session
    ne marche plus, la première question du support est « sa session est-elle encore
    vivante ? », et de la réponse dépend tout le reste — soit elle n'a rien à faire,
    soit elle doit refaire son login. Jusqu'ici aucune surface n'y répondait pour un
    tiers : un outil résout toujours le credential de CELUI QUI L'APPELLE. La seule
    voie était de se connecter à la machine de production et de piloter le navigateur
    à la main, avec un script écrit à chaud. Vécu le 2026-09-03 : six relances d'une
    connexion dont la session avait toujours été valide.

    ⚠️ **Ce qu'elle ne fait PAS, et c'est le cœur de sa conception.** Elle n'exécute
    que la sonde DÉCLARÉE par le connecteur (`_REGISTRY`) — jamais une requête choisie
    par l'appelant. Ce serait un « agir en tant que » générique, c'est-à-dire convertir
    une porte qui exige aujourd'hui un accès à la machine de production en une porte
    qu'un jeton d'administration ouvre à distance. Le jour où ce jeton fuit, tous les
    accès utilisateurs deviendraient exploitables de n'importe où. La lourdeur du
    chemin serveur EST un garde-fou : elle fait qu'on n'y recourt pas à la légère, et
    les cas qui dépassent la sonde y restent, avec leur friction assumée.

    Elle ne rend donc qu'un `Verdict` — connecté ou non, et pourquoi. Aucune donnée
    métier ne remonte par ce chemin, parce qu'aucune n'est lue.

    ⚠️ La session ouverte est ÉPHÉMÈRE et refermée dans tous les cas : elle existe le
    temps de la question. Une session laissée ouverte sur le contexte d'un tiers serait
    exactement l'accès persistant que ce lot refuse de créer.

    `ProbeUnavailable` remonte telle quelle : « je ne sais pas » n'est pas « pas
    logué », et les confondre est ce qui a coûté une matinée le 2026-09-03."""
    verify = _REGISTRY.get(connector)
    if verify is None:
        raise SessionError(f"{connector} n'est pas un connecteur à session navigateur.")
    if not browserbase.is_configured():
        raise ProbeUnavailable("substrat navigateur non configuré sur cette instance")
    sess = browserbase.start_session(context_id, keep_alive=False, timeout=120)
    try:
        return (await verify(sess["id"], account) if connector in _ACCOUNT_AWARE
                else await verify(sess["id"]))
    finally:
        try:
            browserbase.release_session(sess["id"])
        except Exception as e:                                   # noqa: BLE001
            # Le verdict est déjà acquis ; une fermeture ratée ne doit pas l'effacer.
            # Mais elle se JOURNALISE : une session non refermée sur le contexte d'un
            # tiers est précisément ce qu'on s'interdit de laisser derrière soi.
            logger.error("sonde %s : session %s non refermée : %s",
                         connector, sess.get("id"), e)


def _prune(now: float) -> None:
    for k, exp in list(_PENDING.items()):
        if exp < now:
            _PENDING.pop(k, None)


def register(connector: str, verify: Verify, *, login_url: str | None = None,
             account_aware: bool = False) -> None:
    """Déclare un connecteur à session navigateur + sa vérification de login. `login_url`
    = page de login vers laquelle ouvrir la Live View (recommandé — évite l'`about:blank`).

    `account_aware=True` (connecteur GÉNÉRIQUE, ADR 0026 amendé) : le connecteur n'a pas
    UN site mais N — sa `login_url` est fournie à l'appel (`start(..., login_url=…)`) et
    son `verify` reçoit `(session_id, account)` au lieu de `(session_id)`, `account`
    identifiant le site (host). Les connecteurs à site unique (crunchbase, brevoauto,
    pennylaneged) restent inchangés."""
    _REGISTRY[connector] = verify
    if account_aware:
        _ACCOUNT_AWARE.add(connector)
    if login_url:
        _LOGIN_URLS[connector] = login_url


def is_session_connector(connector: str) -> bool:
    return connector in _REGISTRY


def start(sub: str, connector: str | None = None, *,
          login_url: str | None = None) -> dict:
    """Ouvre un Context + une session keep-alive pour `sub` et renvoie la Live View
    interactive. La session est amenée sur `login_url` si fourni (connecteur générique :
    le site vient de l'appel), sinon sur la `login_url` enregistrée du connecteur, sinon
    `about:blank`. La session émise est LIÉE à `sub` (consommée par `finalize`). BLOQUANT
    (HTTP Browserbase synchrone) → appeler via `asyncio.to_thread` depuis une route async.
    Lève `SessionError` si Browserbase n'est pas configuré côté plateforme."""
    if not browserbase.is_configured():
        raise SessionError("Browserbase non configuré côté plateforme "
                           "(BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID).")
    try:
        context_id = browserbase.create_context()
        sess = browserbase.start_session(context_id, keep_alive=True, timeout=900)
        login_url = login_url or _LOGIN_URLS.get(connector or "")
        if login_url:
            # Best-effort : on amène la session sur la page de login. Un échec (nav lente,
            # CDP indispo) ne doit pas rater l'ouverture — l'user peut taper l'URL.
            try:
                asyncio.run(browserbase.navigate(sess["id"], login_url))
            except Exception:  # noqa: BLE001 — détail loggué, jamais renvoyé
                logger.warning("browserbase navigate to login failed for %s", connector)
        live = browserbase.live_view_url(sess["id"])
    except browserbase.BrowserbaseError as e:
        logger.warning("browserbase start failed: %s", e)
        raise SessionError("connexion au navigateur distant impossible — réessaie.")
    now = time.monotonic()
    _prune(now)
    _PENDING[(sub, context_id, sess["id"])] = now + _PENDING_TTL
    return {"live_view_url": live, "context_id": context_id, "session_id": sess["id"]}


def _persist(sub: str, connector: str, context_id: str, session_id: str,
             scope: str, group_id: "int | None", account: str = "") -> None:
    browserbase.release_session(session_id)        # libère → persiste le Context
    # La session navigateur est un credential comme un autre : elle se pose au
    # niveau MEMBRE (ADR 0033, défaut), ÉQUIPE ou ORG (connecteur org-partageable —
    # ex. GED cabinet partagée par la team). L'org de contexte est résolue via le
    # seam `current_org`. Import lazy (access importe db comme ce module — pas de
    # cycle, mais on reste hors du top-level par symétrie avec les autres seams).
    from . import access, org_store, group_store
    org_id = access.current_org(sub)
    if org_id is None:
        raise SessionError("aucune org de contexte — reconnecte-toi et réessaie.")
    if scope == "org":
        org_store.set_org_secret(org_id, connector, context_id, set_by=sub)
    elif scope == "group":
        gid = group_id if group_id is not None else access.current_group(sub)
        if gid is None:
            raise SessionError("aucune équipe active — sélectionne une équipe puis réessaie.")
        group_store.set_group_secret(gid, connector, context_id, set_by=sub)
    else:
        # `account` = le compte du coffre (multi-compte ADR 0011/0024) : vide pour un
        # connecteur à site unique, le host du site pour le connecteur générique — une
        # ligne de coffre PAR SITE, jamais un Context fourre-tout.
        db.set_member_api_key(sub, org_id, connector, context_id, account=account)


async def finalize(sub: str, connector: str, context_id: str, session_id: str,
                   *, scope: str = "member", group_id: "int | None" = None,
                   account: str = "", force: bool = False) -> FinalizeResult:
    """Vérifie le login sur la session vivante ; si OK, persiste le Context (= credential)
    au niveau demandé (`scope` ∈ member|org|group). Rend un `FinalizeResult` :
    `connected=False` = pas encore logué (l'appelant invite à réessayer, rien n'est
    écrit) ; `warning` non vide = persisté SANS confirmation du login, parce que la
    sonde elle-même est hors service (`ProbeUnavailable`). ⚠️ Le contrôle des DROITS de
    pose à un niveau partagé (org_admin / group_admin) incombe à l'appelant (route REST
    / tool).

    `account` = compte du coffre visé (connecteur générique : le site). `force=True`
    persiste SANS passer par `verify` : l'échappatoire quand la sonde répond « pas
    logué » à tort — site dont le login vit en localStorage plutôt qu'en cookie, ou
    sonde d'API cassée.

    ⚠️ Cette échappatoire était RÉSERVÉE aux connecteurs account-aware, au motif que
    « sur un connecteur à site unique le verify est une vraie sonde d'API : la
    contourner n'aurait aucune justification ». Le 2026-09-03 a réfuté le motif : une
    vraie sonde d'API casse aussi (Pennylane a déplacé la route sondée), et il n'existait
    alors AUCUN moyen de débloquer une cliente. Elle est donc ouverte à tous les
    connecteurs à session. Le risque qu'elle couvrait demeure et il est assumé :
    persister un Context non logué pose au coffre un credential MORT, dont l'agent se
    croira pourvu — il ne l'apprendra qu'au premier appel métier (401 → « session
    expirée »). C'est récupérable, et c'est un geste explicite ; l'inverse — un client
    inconnectable sans recours — ne l'était pas."""
    verify = _REGISTRY.get(connector)
    if verify is None:
        raise SessionError(f"{connector} n'est pas un connecteur à session navigateur.")
    if scope not in ("member", "org", "group"):
        raise SessionError(f"scope inconnu : {scope!r}")
    # La session DOIT avoir été émise par `start()` pour CE sub (anti-IDOR) : on ne
    # persiste jamais un Context tiers passé à la main.
    key = (sub, context_id, session_id)
    if _PENDING.get(key, 0.0) < time.monotonic():
        _PENDING.pop(key, None)
        raise SessionError("session de connexion inconnue ou expirée — relance « Connecter ».")
    verdict = Verdict(True, FORCED, "Session persistée SANS vérification du login "
                      "(`force`) : si les appels échouent, c'est qu'elle n'était pas "
                      "loguée.", retry=False)
    warning = "" if not force else verdict.detail
    if not force:
        try:
            verdict = (await verify(session_id, account) if connector in _ACCOUNT_AWARE
                       else await verify(session_id))
        except ProbeUnavailable as e:
            # La sonde est aveugle — pas l'utilisateur délogué. Bloquer ici rendrait le
            # connecteur inconnectable pour tout le monde (2026-09-03). On persiste, et
            # on remonte l'anomalie au lieu de la taire.
            logger.error("session verify inconclusive for %s: %s", connector, e)
            verdict = Verdict(True, PROBE_UNAVAILABLE, str(e), retry=False)
            warning = str(e)
        except Exception:  # noqa: BLE001 — détail loggué, jamais renvoyé (peut porter l'apiKey)
            logger.exception("session verify failed for %s", connector)
            raise SessionError("vérification de la session impossible — réessaie.")
        if not verdict.connected:
            return FinalizeResult(False, verdict.reason, verdict.detail, verdict.retry)
    await asyncio.to_thread(_persist, sub, connector, context_id, session_id, scope,
                            group_id, account)
    _PENDING.pop(key, None)
    return FinalizeResult(True, verdict.reason, verdict.detail, verdict.retry, warning)
