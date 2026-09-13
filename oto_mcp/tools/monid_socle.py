"""Socle du connecteur `monid` : ce que ses quatre outils partagent.

Les bornes et le budget de temps du lancement, la traduction d'un refus de Monid en
consigne (choisie sur le CODE, l'issue inconnue à part), l'enveloppe d'un run et sa
marche à suivre, les deux gardes de la clé de la plateforme (liste des runs, solde), la
projection d'une page. Le POURQUOI de ces choix vit dans la docstring de `tools/monid.py`,
qui garde la sonde, `register()` et les outils.

Découpé du module des outils pour tenir sous le plafond de 500 lignes par fichier
(`docs/conventions.md`). Il n'a pas de `register()` : ce n'est pas un connecteur, c'est
un helper. Aucun appel au client n'y vit — ils restent écrits en clair dans les outils,
là où la sonde version-skew les lit.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData
from oto.tools.monid.client import (MonidHTTPError, MonidProtocolError, is_terminal,
                                    run_cost_usd)

from .. import output_projection
from ..mcp_errors import McpError

_WAIT_MAX_S = 40      # borne de `wait_seconds` (lancement et relecture)
_RUN_READ_S = 35      # lecture du POST /v1/run, au plus
_RUN_READ_MIN_S = 10  # en deçà, un run synchrone finirait presque sûrement en issue inconnue
_CONNECT_S = 10       # délai de connexion que le client pose en dur sur `run()`
_RUN_BUDGET_S = 45    # échéance lancement + attente, depuis l'entrée de l'outil
_DISCOVER_LIMIT_MAX = 40
_RUNS_LIMIT_MAX = 100

_DROP_ENDPOINT = ("providerDisplayDescription", "supportedX402Networks")
_DROP_RUN = ("caller",)
_HINT_FULL = "full=True rend les enregistrements entiers"


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _hors_op(op: str, **donnes: Any) -> None:
    """Refuse un argument qui ne s'applique pas à l'`op` choisie, plutôt que de l'ignorer :
    un filtre passé à `op="get"` laisserait croire qu'il a filtré. Comparé par IDENTITÉ :
    `0 == False`, et un `min_score=0` passé à inspect serait sinon avalé en silence."""
    en_trop = sorted(k for k, v in donnes.items() if v is not None and v is not False)
    if en_trop:
        raise _bad(f"op='{op}' ne prend pas {en_trop}.")


def _borne(nom: str, valeur: Any, bas: int, haut: int) -> None:
    if isinstance(valeur, bool) or not isinstance(valeur, int) or not bas <= valeur <= haut:
        raise _bad(f"`{nom}` doit être un entier de {bas} à {haut} (reçu {valeur!r}).")


def _extrait(valeur: Any, n: int = 200) -> str:
    texte = valeur if isinstance(valeur, str) else json.dumps(valeur, ensure_ascii=False,
                                                              default=str)
    texte = texte.strip()
    return texte if len(texte) <= n else texte[:n] + "…"


def _rid(e: Any) -> str:
    rid = getattr(e, "request_id", None)
    return f" [request_id {rid}]" if rid else ""


def _cause(e: BaseException) -> str:
    """Ce qui a manqué, en deux mots : le code HTTP, ou l'incident de transport d'origine."""
    if isinstance(e, MonidHTTPError):
        return f"HTTP {e.status_code}"
    if isinstance(e, MonidProtocolError):
        return type(e.__cause__).__name__ if e.__cause__ else "réponse sans run lisible"
    return type(e).__name__


# --- traduction des refus -----------------------------------------------------

def _issue_inconnue(e: Any, *, is_platform: bool = False) -> McpError:
    """`INTERNAL_ERROR` et non `INVALID_PARAMS` (écart voulu à `_bad`) : « argument
    invalide » pousse l'agent à corriger puis rappeler — le double paiement. L'interdit
    vient en tête. Sous la clé de la plateforme, la liste des runs est fermée : le refus
    ne l'indique pas, il renvoie à un administrateur. Remonte à Sentry (non « attendue »)."""
    ou_chercher = ("Il est passé par la clé de la plateforme : demande à un administrateur "
                   "de vérifier le workspace Monid de la plateforme." if is_platform else
                   "Cherche-le d'abord dans monid_runs(op=\"list\").")
    return McpError(ErrorData(code=INTERNAL_ERROR, message=(
        "NE RELANCE PAS monid_run, ni à l'identique ni modifié : issue INCONNUE du "
        f"lancement ({_cause(e)}), le run peut exister et être facturé. "
        f"{ou_chercher}{_rid(e)}")))


def _traduire(e: Exception, *, is_platform: bool = False) -> Exception:
    """L'exception à lever pour un refus de Monid — choisie sur le CODE, jamais sur le texte.

    4xx → refus nommé (l'appel ou la clé est à changer). 429 et 5xx restent ce qu'ils
    sont : la taxonomie d'erreurs les classe réessayables — sauf un lancement dont l'issue
    est inconnue, qu'on ne laisse jamais passer pour réessayable ni pour une erreur
    d'argument (`_issue_inconnue` : `INTERNAL_ERROR`, non réessayable)."""
    if getattr(e, "may_have_run", False):
        return _issue_inconnue(e, is_platform=is_platform)
    if isinstance(e, MonidProtocolError):
        return _bad(f"Réponse inexploitable de Monid : {e}{_rid(e)}")
    status, rid = e.status_code, _rid(e)
    corps = e.body if isinstance(e.body, dict) else {}
    if status == 503 and "walletStatus" in corps and e.retry_after is None:
        etat = corps.get("walletStatus") or "pas encore créé"
        return _bad(f"Portefeuille Monid indisponible (503, statut {etat}) : Monid "
                    f"n'annonce aucun délai de reprise — vois son tableau de bord.{rid}")
    if status == 429 or status >= 500:
        return e
    detail = e.upstream_message or _extrait(e.body)
    if status == 400:
        msg = (f"Monid a refusé l'entrée (400) : {detail}. Vérifie-la contre le schéma "
               "rendu par monid_endpoint(op=\"inspect\").")
    elif status == 401:
        msg = ("Monid refuse la clé (401) : absente, mal formée ou révoquée. "
               + ("C'est la clé de la plateforme : préviens un administrateur."
                  if is_platform else
                  "Crée une nouvelle clé dans le tableau de bord Monid (API keys) et "
                  "remplace-la sur la carte du connecteur."))
    elif status == 402:
        msg = ("Solde du portefeuille Monid insuffisant (402) : "
               + ("il est servi par la clé de la plateforme, c'est à un administrateur "
                  "de le recharger." if is_platform else "recharge-le sur monid.ai."))
    elif status == 403:
        msg = ("Monid refuse l'accès (403) : la clé n'est rattachée à aucun workspace, "
               "ou ce run appartient à un autre workspace.")
    elif status == 404:
        msg = ("Inconnu de Monid (404) : endpoint ou run introuvable. Relance "
               "monid_endpoint(q=…) et passe provider + endpoint EXACTEMENT comme rendus"
               + ("." if is_platform else
                  " ; un run se retrouve dans monid_runs(op=\"list\")."))
    elif status == 409:
        msg = ("Run déjà terminé ou non arrêtable (409) : rien à arrêter — relis-le avec "
               "monid_runs(op=\"get\").")
    else:
        msg = f"Monid a refusé la requête (HTTP {status}) : {detail}."
    return _bad(msg + rid)


def _appel(fn: Callable[[], Any], *, is_platform: bool = False) -> Any:
    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e)) from None
    except (MonidHTTPError, MonidProtocolError) as e:
        raise _traduire(e, is_platform=is_platform) from None


# --- l'enveloppe d'un run -----------------------------------------------------

def _http_fournisseur(run: dict) -> Optional[int]:
    reponse = run.get("providerResponse")
    code = reponse.get("httpStatus") if isinstance(reponse, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _est_un_run(corps: Any, run_id: Optional[str] = None) -> bool:
    """Le test du client (`run()`) : un dict qui porte `runId` et `status`, chaînes non
    vides — et, quand `run_id` est donné, CE run-là."""
    return (isinstance(corps, dict)
            and all(isinstance(corps.get(k), str) and corps[k] for k in ("runId", "status"))
            and (run_id is None or corps["runId"] == run_id))


def _relecture_ratee(run: dict, cause: str) -> str:
    rid = run.get("runId")
    return (f"Run accepté ({run.get('status')}), mais sa relecture a échoué ({cause}) : il "
            f"tourne peut-être encore. Relis-le avec monid_runs(op=\"get\", run_id=\"{rid}\") "
            "— ne le relance pas.")


def _suite(run: dict, done: bool, run_id: Optional[str] = None) -> Optional[str]:
    """La marche à suivre, par statut ; `None` quand le résultat est prêt à lire.
    `run_id` = l'identifiant connu de l'appelant, si le corps n'en porte pas."""
    rid, status = run.get("runId") or run_id, run.get("status")
    if not done:
        return (f"Run {status} : pas encore fini. Relis-le avec monid_runs(op=\"get\", "
                f"run_id=\"{rid}\", wait_seconds=30), ou arrête-le (et sa dépense) avec "
                f"monid_runs(op=\"stop\", run_id=\"{rid}\").")
    if status == "BLOCKED":
        motif = _extrait(run.get("reason") or "sans motif")
        return ("Bloqué avant exécution par un plafond du workspace Monid (budget ou nombre "
                f"de runs) : « {motif} ». Rien n'est facturé ; relancer bloquera de nouveau "
                "tant que ce plafond n'est pas changé chez Monid.")
    if status == "FAILED":
        return "Échec côté Monid, pas chez le fournisseur : non facturé."
    if status == "TIMED_OUT":
        return ("Délai du run dépassé : non facturé. Relance avec un volume plus petit, "
                "ou plus tard.")
    if status == "STOPPED":
        return "Run arrêté."
    http = _http_fournisseur(run)
    if http is None:
        return ("Le fournisseur n'a rendu aucun statut HTTP : lis `run.output` et "
                "`run.providerResponse` avant de te fier au résultat.")
    if 200 <= http < 300:
        return None
    erreur = (run.get("providerResponse") or {}).get("error")
    return (f"Le fournisseur a répondu HTTP {http}"
            + (f" : {_extrait(erreur)}" if erreur else "")
            + " — Monid ne facture pas une réponse non 2xx (un 404 veut souvent dire "
              "« rien trouvé »).")


def _provider_ok(run: dict) -> Optional[bool]:
    """COMPLETED avec une réponse 2xx du fournisseur ; `None` si ce statut manque."""
    if run.get("status") != "COMPLETED":
        return False
    http = _http_fournisseur(run)
    return None if http is None else 200 <= http < 300


def _enveloppe(run: Any, *, relecture: Optional[str] = None,
               run_id: Optional[str] = None) -> dict:
    """`run_id` = l'identifiant que l'appelant connaît (accepté, ou demandé) : la marche à
    suivre ne dit jamais « run None », et un corps qui n'est pas un run le dit."""
    r = run if isinstance(run, dict) else {}
    lisible = _est_un_run(run)
    done = lisible and is_terminal(r)
    if relecture is None and not lisible:
        relecture = (f"Réponse illisible de Monid pour le run {run_id} (le corps n'est pas "
                     f"un run) : relis-le avec monid_runs(op=\"get\", run_id=\"{run_id}\") "
                     "— ne le relance pas.")
    return {"run": run, "done": done, "provider_ok": _provider_ok(r) if done else None,
            "cost_usd": run_cost_usd(r), "next_step": relecture or _suite(r, done, run_id)}


# --- les gardes de la clé de la plateforme ------------------------------------

def _garde_liste(is_platform: bool) -> None:
    """La liste des runs est celle du WORKSPACE de la clé. Sous la clé de la plateforme,
    ce workspace est partagé par toutes les orgs qui ont un grant : la lister exposerait
    leurs runs (et des identifiants qui ouvrent `get` et `stop`). Refusé, et nommé."""
    if is_platform:
        raise _bad("La liste des runs n'est pas servie sous la clé de la plateforme : son "
                   "workspace Monid est partagé, son historique contient les runs d'autres "
                   "organisations. Relis un run par son identifiant (monid_runs(op=\"get\", "
                   "run_id=…)) ; pour l'historique, pose ta propre clé Monid.")


def _garde_solde(is_platform: bool) -> None:
    """Le solde est celui du portefeuille de la clé. Sous la clé de la plateforme, c'est
    le portefeuille PARTAGÉ de la plateforme : son solde n'est pas celui de l'org qui a un
    grant, et ne lui est pas servi. Un lancement à court de fonds le dit par son 402.
    Refusé avant tout envoi, et nommé."""
    if is_platform:
        raise _bad("Le solde n'est pas servi sous la clé de la plateforme : son portefeuille "
                   "Monid est partagé entre les organisations qui y ont accès, et son solde "
                   "ne leur est pas communiqué. Un lancement qui manque de fonds le dit "
                   "(402) ; pour voir un solde, pose ta propre clé Monid.")


def _projeter(page: Any, drop: tuple, full: bool) -> Any:
    """Retire des COLONNES entières des éléments, et le dit dans `projection`."""
    if full or not isinstance(page, dict):
        return page
    out = output_projection.project(page, items_path="items", item_drop=drop)
    out["projection"] = {"omitted": list(drop), "hint": _HINT_FULL}
    return out
