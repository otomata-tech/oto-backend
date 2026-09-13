"""Monid — la passerelle payante vers les endpoints de données de ~70 fournisseurs.

Wrappe `oto.tools.monid.MonidClient` (API `/v1`, Bearer, contrat OpenAPI 0.1.0). keyed
`api_key`, régime d'`apify` : BYO par défaut, clé plateforme sur grant explicite — chaque
appel est débité du portefeuille prépayé du workspace Monid dont la clé sert.

**Quatre outils, un par objet, le verbe en `op`, le défaut est une lecture** (ADR 0047) :
`monid_endpoint` (`discover` → `inspect`), `monid_run` (le seul qui dépense),
`monid_runs` (`list` / `get` / `stop`), `monid_wallet`. Un argument qui ne s'applique
pas à l'`op` choisie est REFUSÉ, jamais ignoré.

**Pas de dry_run sur `monid_run`**, comme les autres appels de données payants
(`apify_run*`, theirstack) : le dry-run par défaut reste réservé aux gestes qui sortent
de l'organisation. La protection d'un appel payant, c'est le prix lu à `inspect` et les
paramètres de volume posés au lancement.

⚠️ **La clé de la plateforme sert un workspace Monid PARTAGÉ** par toutes les orgs qui
ont un grant. Sa liste de runs est donc celle des autres aussi (entrées, sorties, et des
identifiants qui ouvriraient `get` et `stop`) : `monid_runs(op="list")` est refusé sous
cette clé, en le nommant. `get` et `stop` restent ouverts par identifiant de run — opaque,
rendu à qui l'a lancé (précédent apify, firecrawl). Le solde n'est pas servi non plus
(`monid_wallet` refusé sous cette clé, en le nommant) : c'est celui du portefeuille
partagé de la plateforme, pas celui de l'org qui a un grant — un lancement à court de
fonds le dit par son 402.

⚠️ **Le lancement a un budget de temps, et il est court à dessein.** Un client MCP
raccroche vers 60 s, et un worker du runner REJOUE un appel qui dépasse — donc paie deux
fois. Tout se compte depuis l'entrée de l'outil, résolution de la clé comprise : la
lecture du POST reçoit ce qui reste de `_RUN_BUDGET_S` une fois ôtés la connexion
(`_CONNECT_S`, posée par le client) et le temps déjà passé, au plus `_RUN_READ_S` ; s'il
reste moins de `_RUN_READ_MIN_S`, le lancement n'est PAS envoyé (rien n'est parti, rien
n'est facturé). Puis l'attente d'un run accepté jusqu'à la même échéance ; une relecture
partie juste avant la dépasse d'au plus ~10 s. Pire cas ≈ 55 s **en délais par socket**,
tant que la connexion s'établit du premier coup : chaque adresse injoignable ajoute
jusqu'à 10 s, et la résolution DNS n'est pas bornée. Un fournisseur synchrone plus lent
que la lecture accordée rend donc une issue INCONNUE : le run existe peut-être.

⚠️ **Une issue inconnue n'est jamais re-tentée ici, et le refus le dit.** Monid n'a pas de
clé d'idempotence : relancer un lancement perdu en vol peut payer deux fois. Le client
marque ces cas (`may_have_run`) ; l'outil refuse en l'interdisant d'abord, puis en
nommant `monid_runs(op="list")` — ou, sous la clé de la plateforme dont la liste est
fermée, un administrateur. Ce refus part en `INTERNAL_ERROR`, pas en `INVALID_PARAMS` (écart
VOULU au refus nommé des autres cas) : un « argument invalide » dit à l'agent de corriger
et de rappeler, soit le chemin exact du double paiement. Conséquence assumée : n'étant pas
une erreur « attendue » (`error_taxonomy._is_expected_error`), chaque issue inconnue
remonte à Sentry — un run peut-être payé deux fois mérite ce signal, et un fournisseur
synchrone plus lent que la lecture accordée en produira.

**Une relecture qui échoue n'est pas un échec de l'outil** : le run a été accepté, on le
rend avec la marche à suivre, et l'échec est journalisé. Une relecture qui rend autre
chose que CE run (corps vide, liste, autre identifiant) compte comme un échec : elle ne
remplace jamais le run accepté.

**Métrage** (patron `tools/theirstack.py`) : un lancement qui rend un run compte 1 —
`note_call_trace` toujours, `record_platform_usage` seulement sous la clé plateforme. Un
débit de quota qui échoue ne cache jamais un run accepté : il est journalisé avec
l'identifiant du run (pour rattraper le compte), et l'enveloppe est rendue.

Les appels au client sont écrits en clair (`client.run(…)`) : c'est ce qui les rend
vérifiables par la sonde version-skew (`test_tools_client_methods_exist`). Les bornes, la
traduction des refus, l'enveloppe d'un run et les gardes vivent dans `monid_socle.py`.
"""
from __future__ import annotations

import logging
import time
from typing import Literal, Optional

import requests
from fastmcp import FastMCP
from oto.tools.monid.client import (MonidClient, MonidHTTPError, MonidProtocolError,
                                    is_terminal)

from .. import access, session_org
from ..connectors import verify as connector_verify
from .monid_socle import (_CONNECT_S, _DISCOVER_LIMIT_MAX, _DROP_ENDPOINT, _DROP_RUN,
                          _RUN_BUDGET_S, _RUN_READ_MIN_S, _RUN_READ_S, _RUNS_LIMIT_MAX,
                          _WAIT_MAX_S, _appel, _bad, _borne, _cause, _enveloppe,
                          _est_un_run, _garde_liste, _garde_solde, _hors_op, _projeter,
                          _relecture_ratee)

logger = logging.getLogger(__name__)


# --- la sonde -----------------------------------------------------------------

def _verify(fields: dict, config: dict | None = None) -> Optional[dict]:  # noqa: ARG001
    """Sonde « tester la connexion » : `GET /v1/auth/whoami`, gratuit, sans effet de bord
    — jamais un run, jamais le portefeuille. Rend QUI la clé authentifie (workspace,
    utilisateur) quand Monid le nomme ; le préfixe de la clé n'est pas vérifié ici (le
    contrat et la CLI ne s'accordent pas dessus)."""
    try:
        moi = MonidClient(api_key=fields["key"]).whoami()
    except MonidHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(
                "Monid refuse cette clé (401) : absente, mal formée ou révoquée — crée-en "
                "une dans le tableau de bord Monid (API keys)." if e.status_code == 401 else
                "Monid reconnaît la clé mais ne la rattache à aucun workspace (403).") from None
        raise
    moi = moi if isinstance(moi, dict) else {}
    ws = moi.get("workspace") if isinstance(moi.get("workspace"), dict) else {}
    user = moi.get("user") if isinstance(moi.get("user"), dict) else {}
    identite = {k: v for k, v in (("workspace_id", ws.get("workspaceId")),
                                  ("workspace", ws.get("name") or ws.get("slug")),
                                  ("user_id", user.get("userId"))) if v}
    return {"identity": identite} if identite else None


def register(mcp: FastMCP) -> None:
    connector_verify.register("monid", _verify)

    def _client() -> tuple[MonidClient, bool]:
        key, is_platform = access.resolve_api_key("monid")
        return MonidClient(api_key=key), is_platform

    @mcp.tool()
    def monid_endpoint(
        op: Literal["discover", "inspect"] = "discover",
        q: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 10,
        min_score: Optional[float] = None,
        provider: Optional[str] = None,
        endpoint: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Find and read Monid endpoints — Monid is a paid gateway to ~2,000 data endpoints
        from ~70 providers (web search, scraping, contact enrichment, social media…)
        behind one key.

        - **"discover"** (default): describe the need in plain words (`q`). Returns —
          `{items: [{provider, providerDisplayName, endpoint, displayName,
          displayDescription, price, tags, categories}], total, projection}`: `total`
          counts matches before `limit`, there is no cursor; `providerDisplayDescription`
          and `supportedX402Networks` are dropped and named in `projection` (`full=True`
          keeps them).
        - **"inspect"**: one endpoint by `provider` + `endpoint`, exactly as discover
          returned them. Returns — Monid's card untouched: `input` (JSON Schemas for
          `pathParams`, `queryParams`, `body`, plus `bodyType`), `price` (may be absent),
          `notes`, `metrics`, `docUrl`.

        ALWAYS inspect before `monid_run`: the input schema and the live price are only
        there. Price `type` is open (PER_CALL, PER_RESULT with `flatFee`, PER_UNIT,
        TIERED, PER_UNIT_MATRIX…). For TIERED and PER_UNIT_MATRIX, `price.amount` is only
        a base and can be 0 on a paid endpoint: the actual rate is in `default`, `tiers`
        and `variants` — a tier or variant matched on your input applies, and a tier keyed
        on `output` is known only after the run, so read the cost as a floor. Volume
        inputs (`maxItems`, `limit`) often apply PER QUERY: 3 search terms × 10 items can
        bill 30 results.

        Args:
            op: "discover" | "inspect".
            q: op="discover" — the need in plain words, 1-1000 characters.
            category: op="discover" — a category id (lowercase slug, e.g. `lead-generation`).
            limit: op="discover" — cards returned, 1-40.
            min_score: op="discover" — relevance floor, 0-2.
            provider: op="inspect" — provider slug as discover returned it (may contain dots).
            endpoint: op="inspect" — endpoint path as discover returned it (starts with `/`).
            full: op="discover" — whole cards instead of projected ones.
        """
        if op == "inspect":
            _hors_op("inspect", q=q, category=category, min_score=min_score,
                     limit=limit != 10, full=full)
            if not provider or not endpoint:
                raise _bad("op='inspect' : `provider` et `endpoint` requis, tels que "
                           "discover les a rendus.")
            client, is_platform = _client()
            return _appel(lambda: client.inspect(provider, endpoint), is_platform=is_platform)
        if op != "discover":
            raise _bad(f"`op` invalide : {op!r} (attendu : discover, inspect).")
        _hors_op("discover", provider=provider, endpoint=endpoint)
        if not q:
            raise _bad("op='discover' : `q` requis — décris le besoin en clair.")
        _borne("limit", limit, 1, _DISCOVER_LIMIT_MAX)
        client, is_platform = _client()
        page = _appel(lambda: client.discover(q, limit=limit, category=category,
                                              min_score=min_score), is_platform=is_platform)
        return _projeter(page, _DROP_ENDPOINT, full)

    @mcp.tool()
    def monid_run(
        provider: str,
        endpoint: str,
        body: Optional[dict] = None,
        query_params: Optional[dict] = None,
        path_params: Optional[dict] = None,
        wait_seconds: int = 20,
    ) -> dict:
        """Run a Monid endpoint — SPENDS MONEY: the call is debited from the connected
        Monid wallet. Run only an endpoint you inspected (`monid_endpoint(op="inspect")`);
        on your own Monid key, check `monid_wallet` first when the run may be costly.

        Put each input where inspect's schema puts it — `body`, `query_params` or
        `path_params`, never flat — and keep volume inputs (`maxItems`, `limit`) small.

        Waits up to `wait_seconds` for an accepted run to finish. A provider that holds
        the launch longer than ~35 s yields an UNKNOWN-outcome refusal: the run may exist
        and be billed. NEVER retry a run whose outcome is unknown — retrying can pay
        twice; the refusal says where to look for it (`monid_runs(op="list")` on your own
        Monid key, an admin under the platform key).

        Returns — `{run, done, provider_ok, cost_usd, next_step}`: `run` = Monid's run as
        returned (`runId`, `status`, `output`, `providerResponse`…); `done` = terminal
        status; `provider_ok` = COMPLETED with a 2xx provider answer; null while running,
        or when Monid reports no provider HTTP status (then read `next_step`);
        `cost_usd` = the cost Monid reports (null until settled); `next_step` = what to do,
        null when the result is ready. Run status is not provider status: COMPLETED with
        a provider 404 means "not found", and is not billed.

        Args:
            provider: provider slug, exactly as discover/inspect returned it.
            endpoint: endpoint path, exactly as discover/inspect returned it.
            body: JSON body fields, per inspect's `input.body` schema.
            query_params: query-string fields, per inspect's `input.queryParams` schema.
            path_params: path fields, per inspect's `input.pathParams` schema.
            wait_seconds: 0-40 — seconds to poll an accepted run after the launch returns; 0 = no polling (a sync provider still holds the launch up to ~35 s).
        """
        _borne("wait_seconds", wait_seconds, 0, _WAIT_MAX_S)
        debut = time.monotonic()
        client, is_platform = _client()
        # La lecture reçoit ce qui RESTE du budget : la résolution de la clé en a peut-être
        # mangé. Trop peu → rien n'est envoyé (donc rien facturé), plutôt qu'un lancement
        # voué à finir en issue inconnue ou à dépasser le raccrochage du client.
        lecture = min(_RUN_READ_S, _RUN_BUDGET_S - _CONNECT_S - (time.monotonic() - debut))
        if lecture < _RUN_READ_MIN_S:
            raise _bad("Lancement non envoyé : la résolution de la clé a consommé le budget "
                       "de temps de l'appel. Rien n'est parti ni facturé, relance.")
        run = _appel(lambda: client.run(provider, endpoint, body=body,
                                        query_params=query_params, path_params=path_params,
                                        timeout=lecture), is_platform=is_platform)
        # Un run est revenu : il compte, quoi qu'il devienne ensuite.
        session_org.note_call_trace(quantity=1)
        if is_platform:
            try:
                access.record_platform_usage("monid", 1)
            except Exception:
                # Le run est lancé et facturé : taire son identifiant pour un compteur
                # pousserait l'agent à relancer, donc à payer deux fois.
                logger.warning("monid_run : débit du quota plateforme ÉCHOUÉ pour le run %s "
                               "(run lancé et rendu, le quota n'a PAS bougé)",
                               run.get("runId"), exc_info=True)
        rid = run["runId"]
        if is_terminal(run) or wait_seconds == 0:
            return _enveloppe(run, run_id=rid)
        reste = min(wait_seconds, _RUN_BUDGET_S - (time.monotonic() - debut))
        if reste <= 0:
            return _enveloppe(run, run_id=rid)
        try:
            relu = client.wait_for_run(rid, max_wait_s=reste)
        except (MonidHTTPError, MonidProtocolError, requests.exceptions.RequestException,
                ValueError) as e:
            logger.warning("monid_run : relecture du run %s échouée (%s)", rid, _cause(e))
            return _enveloppe(run, run_id=rid, relecture=_relecture_ratee(run, _cause(e)))
        if not _est_un_run(relu, rid):
            logger.warning("monid_run : relecture du run %s illisible (%s)", rid,
                           type(relu).__name__)
            return _enveloppe(run, run_id=rid,
                              relecture=_relecture_ratee(run, "réponse illisible"))
        return _enveloppe(relu, run_id=rid)

    @mcp.tool()
    def monid_runs(
        op: Literal["list", "get", "stop"] = "list",
        run_id: Optional[str] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
        wait_seconds: int = 0,
        full: bool = False,
    ) -> dict:
        """Monid runs of the connected workspace — list them, read one, or stop one.

        - **"list"** (default): newest first. Returns — `{items, cursor, projection}`:
          pass `cursor` back for the next page, null on the last one. Items carry `runId`,
          `provider`, `endpoint`, `status`, `cost`, `createdAt`… but not `input`/`output`
          (read one with "get"); `caller` is dropped and named in `projection`
          (`full=True` keeps it). Look here for a run whose outcome was unknown. Refused
          under the platform key: that shared workspace holds other organizations' runs.
        - **"get"**: one run by `run_id`, with its `output`; `wait_seconds` waits for it
          to finish. Returns — the `{run, done, provider_ok, cost_usd, next_step}`
          envelope of `monid_run`.
        - **"stop"**: stop a running run, and its spending. Asynchronous. Returns —
          `{run_id, status, message, next_step}`; read it again with "get": a metered
          run settles COMPLETED and bills what it used.

        Args:
            op: "list" | "get" | "stop".
            run_id: op="get"/"stop" — the Monid run id (`runId`), not the `_run_id` token from `run_start`.
            limit: op="list" — runs per page, 1-100.
            cursor: op="list" — the previous page's `cursor`.
            status: op="list" — READY, RUNNING, STOPPING, COMPLETED, FAILED, BLOCKED, STOPPED or TIMED_OUT.
            wait_seconds: op="get" — 0-40 s to wait for the run to finish.
            full: op="list" — whole items instead of projected ones.
        """
        if op not in ("list", "get", "stop"):
            raise _bad(f"`op` invalide : {op!r} (attendu : list, get, stop).")
        if op == "list":
            _hors_op("list", run_id=run_id, wait_seconds=wait_seconds != 0)
            _borne("limit", limit, 1, _RUNS_LIMIT_MAX)
            client, is_platform = _client()
            _garde_liste(is_platform)
            page = _appel(lambda: client.list_runs(limit=limit, cursor=cursor, status=status),
                          is_platform=is_platform)
            if isinstance(page, dict):
                page = {**page, "cursor": page.get("cursor") or None}
            return _projeter(page, _DROP_RUN, full)
        _hors_op(op, limit=limit != 20, cursor=cursor, status=status, full=full,
                 wait_seconds=op == "stop" and wait_seconds != 0)
        if not run_id:
            raise _bad(f"op='{op}' : `run_id` requis (le `runId` du run Monid).")
        _borne("wait_seconds", wait_seconds, 0, _WAIT_MAX_S)
        client, is_platform = _client()
        if op == "stop":
            out = _appel(lambda: client.stop_run(run_id), is_platform=is_platform)
            out = out if isinstance(out, dict) else {}
            rid = out.get("runId") or run_id
            return {"run_id": rid, "status": out.get("status"), "message": out.get("message"),
                    "next_step": (f"L'arrêt est asynchrone : relis le run avec monid_runs("
                                  f"op=\"get\", run_id=\"{rid}\") — il finit STOPPED, ou "
                                  "COMPLETED s'il est facturé à l'usage (il règle alors ce "
                                  "qu'il a consommé).")}
        if wait_seconds:
            run = _appel(lambda: client.wait_for_run(run_id, max_wait_s=wait_seconds),
                         is_platform=is_platform)
        else:
            run = _appel(lambda: client.get_run(run_id), is_platform=is_platform)
        return _enveloppe(run, run_id=run_id)

    @mcp.tool()
    def monid_wallet() -> dict:
        """The connected Monid wallet: spendable `balance` (can be negative) and `held`
        (reserved for runs in flight), in USD. Check it before a costly run. Refused
        under the platform key: that wallet is shared, and its balance is not served.

        Returns — `{balance: {value, currency}, held: {value, currency}}`, as Monid
        returns it.
        """
        client, is_platform = _client()
        _garde_solde(is_platform)
        return _appel(lambda: client.wallet_balance(), is_platform=is_platform)
