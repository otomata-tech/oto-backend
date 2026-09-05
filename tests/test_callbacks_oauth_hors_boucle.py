"""oto-backend#867, lot 2 — les retours OAuth ne figent plus le processus.

Trois callbacks (`async def`) échangeaient le code d'autorisation contre un jeton
par un appel HTTP **synchrone** de 15 à 30 s, plus une écriture en base également
synchrone. Le callback Zoho, lui, était déjà protégé : la discipline existait,
elle n'avait simplement pas été appliquée aux trois autres.

S'y ajoutent deux chemins d'enregistrement dynamique de client OAuth — traités au
point de passage commun des flux de connexion, pas connecteur par connecteur.

On OBSERVE le thread, et la mesure MORD sur la forme d'avant.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from oto_mcp.connectors import flow as connector_flow


def test_un_flux_de_connexion_synchrone_part_au_thread():
    """Deux flux enregistrent un client chez le fournisseur, en HTTP bloquant.
    Le point de passage les couvre tous les cinq — aucun n'a à le savoir, et le
    prochain non plus."""
    vu = {}

    class _Flux:
        @staticmethod
        def start(ctx, values):
            vu["thread"] = threading.current_thread()
            raise RuntimeError("stop après la mesure")

    connector_flow._FLOWS["_bidon_867"] = _Flux
    try:
        async def _scenario():
            vu["boucle"] = threading.current_thread()
            with pytest.raises(RuntimeError):
                await connector_flow.start("_bidon_867", object(), {})

        asyncio.run(_scenario())
    finally:
        connector_flow._FLOWS.pop("_bidon_867", None)

    assert vu["thread"] is not vu["boucle"], (
        "le flux s'est exécuté DANS la boucle : un fournisseur lent y fige tout "
        "le processus au premier consentement.")


def test_un_flux_deja_asynchrone_reste_dans_la_boucle():
    """Le router au thread coûterait un thread par consentement, sans rien
    protéger — il attend déjà proprement."""
    vu = {}

    class _Flux:
        @staticmethod
        async def start(ctx, values):
            vu["thread"] = threading.current_thread()
            raise RuntimeError("stop après la mesure")

    connector_flow._FLOWS["_bidon_867_async"] = _Flux
    try:
        async def _scenario():
            vu["boucle"] = threading.current_thread()
            with pytest.raises(RuntimeError):
                await connector_flow.start("_bidon_867_async", object(), {})

        asyncio.run(_scenario())
    finally:
        connector_flow._FLOWS.pop("_bidon_867_async", None)

    assert vu["thread"] is vu["boucle"]


def test_la_mesure_MORD_sur_la_forme_d_avant():
    vu = {}

    def _sync():
        vu["thread"] = threading.current_thread()

    async def _scenario_nu():
        vu["boucle"] = threading.current_thread()
        _sync()

    asyncio.run(_scenario_nu())
    assert vu["thread"] is vu["boucle"]


def _appels_directs(noeud, noms: set[str], differes: set[int]) -> list[int]:
    """Appels à l'une de `noms` dans le corps de `noeud`, SANS descendre dans une
    fonction imbriquée.

    La frontière compte : isoler le code bloquant dans une fonction interne qu'on
    passe au threadpool est précisément le motif de protection du dépôt. Un
    détecteur qui descend dedans crie sur du code protégé — et un garde-fou qui
    crie à tort finit ignoré, donc pire qu'absent.
    """
    import ast

    lignes = []
    pile = list(ast.iter_child_nodes(noeud))
    while pile:
        n = pile.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue                      # autre frontière : elle sera jugée seule
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in noms and id(n) not in differes):
            lignes.append(n.lineno)
        pile.extend(ast.iter_child_nodes(n))
    return lignes


def _echanges_sync_dans_une_async(source: str, noms: set[str]) -> list[int]:
    """Lignes où l'une de `noms` est appelée dans une fonction `async def` sans
    passer au thread. Un appel dans une fonction `def` ne compte pas : le seam
    des capacités le range déjà hors de la boucle."""
    import ast

    arbre = ast.parse(source)
    differes: set[int] = set()
    for n in ast.walk(arbre):
        # Passé au thread : tout ce qui est dedans est hors de la boucle.
        if (isinstance(n, ast.Call)
                and ((isinstance(n.func, ast.Attribute) and n.func.attr == "to_thread")
                     or (isinstance(n.func, ast.Name)
                         and n.func.id == "run_in_threadpool"))):
            for sous in ast.walk(n):
                if isinstance(sous, ast.Call):
                    differes.add(id(sous))
        # Déjà attendu : `await f()` désigne une fonction asynchrone, qui rend la
        # main à la boucle. La confondre avec un appel bloquant ferait crier le
        # contrôle sur la forme même qu'on cherche à obtenir.
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            differes.add(id(n.value))

    lignes = []
    for fn in ast.walk(arbre):
        if isinstance(fn, ast.AsyncFunctionDef):
            lignes.extend(_appels_directs(fn, noms, differes))
    return sorted(lignes)


@pytest.mark.parametrize("module", ["atlassian", "folk", "salesforce"])
def test_aucun_echange_de_code_ne_reste_dans_la_boucle(module):
    """⚠️ Contrôle STATIQUE, assumé : jouer un vrai callback demanderait un state
    signé, une base et un fournisseur. Il lit l'arbre, pas le texte — il tombera
    sur un retour à la forme d'avant, pas sur un reformatage."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "oto_mcp" / "api"
           / f"{module}.py").read_text()
    nus = _echanges_sync_dans_une_async(
        src, {"exchange_code", "persist_token", "read_saved_fields"})
    assert not nus, (
        f"ligne(s) {nus} : échange de code ou écriture synchrone dans un handler "
        "`async def` — le processus gèle le temps que le fournisseur réponde.")


def test_ce_controle_MORD():
    assert _echanges_sync_dans_une_async(
        "async def f(o):\n    return o.exchange_code('c')\n", {"exchange_code"}) == [2]


def test_ce_controle_voit_les_deux_enveloppes():
    """Les deux formes en usage dans le dépôt : le thread d'asyncio et celui de
    Starlette. N'en connaître qu'une ferait crier le contrôle sur du code sain."""
    assert _echanges_sync_dans_une_async(
        "import asyncio\nasync def f(o):\n"
        "    return await asyncio.to_thread(lambda: o.exchange_code('c'))\n",
        {"exchange_code"}) == []
    assert _echanges_sync_dans_une_async(
        "async def f(o):\n"
        "    return await run_in_threadpool(lambda: o.exchange_code('c'))\n",
        {"exchange_code"}) == []


def test_ce_controle_ne_crie_pas_sur_le_motif_de_protection_du_depot():
    """Le motif en usage ici : isoler le code bloquant dans une fonction interne
    passée au threadpool. Le détecteur ne doit pas descendre dedans — sinon il
    signale comme non protégé exactement ce qui l'est."""
    src = ("async def callback(o):\n"
           "    def _finir():\n"
           "        o.exchange_code('c')\n"
           "    await run_in_threadpool(_finir)\n")
    assert _echanges_sync_dans_une_async(src, {"exchange_code"}) == []


def test_ce_controle_ne_crie_pas_sur_un_appel_deja_attendu():
    """`await f()` désigne une fonction asynchrone : elle rend la main à la
    boucle, c'est la forme qu'on cherche à obtenir — pas celle qu'on traque."""
    assert _echanges_sync_dans_une_async(
        "async def f(o):\n    return await o.persist_token('t')\n",
        {"persist_token"}) == []


# --- l'observation, sur un callback réellement monté -----------------------
#
# Les contrôles statiques ci-dessus lisent l'arbre : ils ne montent pas le code,
# donc ils ne voient pas une panne d'exécution. Ils ont d'ailleurs laissé passer
# un import manquant — le handler levait un `NameError`, l'`except` du callback
# l'avalait, et TOUT retour OAuth partait en « échec » sans que le contrôle
# bronche. C'est la suite complète qui l'a attrapé.
#
# D'où cette épreuve : elle monte le vrai handler et observe le vrai thread.

def _requete(path: str, query: str):
    from starlette.requests import Request

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                    "query_string": query.encode(), "path_params": {}},
                   receive=_receive)


def _handler(module, path: str):
    def _json_response(_r, payload, status=200):
        return {"status": status, "body": payload}

    def _json_error(_r, status, code, message=None):
        return {"status": status, "error": code}

    async def _options(_r):
        return None

    routes = module.make_routes(None, None, _json_response, _json_error, _options)
    return next(r.endpoint for r in routes if r.path == path)


@pytest.mark.parametrize("module_nom,oauth_nom,path", [
    ("oto_mcp.api.atlassian", "atlassian_oauth", "/api/atlassian/oauth/callback"),
    ("oto_mcp.api.folk", "folk_oauth", "/api/folkmcp/oauth/callback"),
])
def test_le_callback_monte_echange_le_code_hors_de_la_boucle(
        monkeypatch, module_nom, oauth_nom, path):
    import importlib

    module = importlib.import_module(module_nom)
    oauth = getattr(module, oauth_nom)
    vu = {}

    monkeypatch.setattr(oauth, "verify_state", lambda s: ("sub-1", "verifier"))
    monkeypatch.setattr(oauth, "persist_token", lambda *a, **k: None)

    def _echange(*a, **k):
        vu["thread"] = threading.current_thread()
        return {"access_token": "t"}

    monkeypatch.setattr(oauth, "exchange_code", _echange)
    handler = _handler(module, path)

    async def _scenario():
        vu["boucle"] = threading.current_thread()
        return await handler(_requete(path, "code=c&state=s"))

    reponse = asyncio.run(_scenario())
    assert vu.get("thread") is not None, (
        "l'échange n'a jamais eu lieu : le handler a échoué avant, et son `except` "
        "l'a avalé — exactement la panne que les contrôles statiques ne voient pas.")
    assert vu["thread"] is not vu["boucle"]
    assert "connect=error" not in reponse.headers["location"], (
        "le callback rend une erreur alors que l'échange a réussi.")
