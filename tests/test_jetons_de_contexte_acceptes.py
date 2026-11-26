"""Chaque jeton de contexte est accepté par chaque outil servi, sauf exception nommée.

La notice servie au handshake dit : « tout contexte se passe DANS l'appel :
`_project`/`_org`/`_group`/`_account`/`_accounts`/`_instance` (optionnels) et `_run_id` ».
Un outil qui refuse l'un de ces jetons (« Unexpected keyword argument ») fait perdre son
appel à l'agent qui obéit à la consigne. Vécu sur `oto_tool_schema` (`_org`), et avant
lui sur `oto_trigger`/`oto_procedure` (`_run_id`, #651/#664), `oto_doc` (`_project`) :
chaque fois corrigé outil par outil, chaque fois rouvert par l'outil suivant.

Ce test ferme la classe : il construit le serveur RÉEL (`_build_mcp`), liste les outils
À TRAVERS le middleware de contexte (le schéma que reçoit un client) et vérifie, pour
chaque outil et chaque jeton :

- qu'il est CONSOMMÉ à l'appel — posé puis retiré par le middleware (`call_axes.
  axes_for_call`), ou déclaré par l'outil lui-même (`_org` d'une capacité, les jetons
  d'`oto_call`), ou retiré sans être posé (`_run_id`, accepté partout) ;
- qu'un jeton consommé par un axe est ANNONCÉ dans le schéma servi (un client en
  `additionalProperties:false` refuse ce qu'il ne voit pas) — sauf `_account`, annoncé
  seulement à qui détient plusieurs comptes du connecteur (`account_axis_advertised_for`).

Un outil neuf qui oublie un jeton fait échouer le test. Les exceptions sont des CLASSES
d'outils où le jeton n'a pas d'objet, chacune avec sa raison (`EXCEPTIONS`).
"""
from __future__ import annotations

import asyncio

import pytest

from _mcp_app import static_mcp
from oto_mcp import call_axes, deprecations

JETONS = ("_org", "_project", "_group", "_run_id", "_instance", "_account", "_accounts")


def _capacites() -> frozenset[str]:
    from oto_mcp.capabilities import registry
    return frozenset(c.mcp for c in registry.CAPABILITIES if getattr(c, "mcp", None))


def _connecteur(nom: str) -> bool:
    return call_axes._is_instance_scopable_tool(nom)


# Les outils écrits à la main (ni capacité, ni outil de travail) qui ne lisent pas le
# projet ou l'équipe de l'appel. Liste NOMINATIVE : un outil écrit à la main neuf doit
# soit accepter le jeton, soit venir s'inscrire ici avec sa raison.
_MAIN_SANS_PROJET = frozenset({
    # `oto_call` reçoit `_project`/`_group`/`_instance`/`_account` DANS `arguments`, pour
    # sa cible (`tools/meta.executer_cible`) ; seuls `_org` et `_run_id` sont repliés.
    "oto_call",
    # Lecture du schéma d'un outil, de l'identité, d'une page d'aide d'installation :
    # aucun ne lit le projet de l'appel.
    "oto_tool_schema", "oto_whoami", "oto_connector_app", "oto_webhook_app",
    # Rendu d'une page de projet : le projet est son argument métier (`project_id`).
    "oto_doc_app",
    # Envoi depuis une adresse de l'org active : le projet n'y entre pas.
    "email_send",
    # Dessin d'une procédure de l'org (monté seulement quand son bundle est configuré).
    "oto_procedure_app",
})
_MAIN_SANS_EQUIPE = frozenset({"oto_call", "oto_tool_schema", "oto_doc_app"})

#: jeton → (prédicat « cet outil en est exempté », raison).
EXCEPTIONS: dict[str, tuple] = {
    "_project": (
        lambda nom, caps: (nom in caps and nom not in call_axes._PROJECT_SPINE_TOOLS)
        or nom in _MAIN_SANS_PROJET,
        "capacité qui ne lit pas le projet de l'appel (elle prend `project_id` en "
        "argument métier quand elle en a besoin) : l'ouvrir est une décision par "
        "capacité (`call_axes._PROJECT_SPINE_TOOLS`), la pose touchant `current_project`",
    ),
    "_group": (
        lambda nom, caps: nom in caps or nom in _MAIN_SANS_EQUIPE,
        "la pose d'une équipe co-pose son org parente ; sur une capacité, le contexte se "
        "résout par `ResolvedCtx`, qui n'a pas été revu pour lire l'équipe de l'appel",
    ),
    "_instance": (
        lambda nom, caps: not _connecteur(nom),
        "désigne une ligne du coffre d'un connecteur : hors connecteur, rien à désigner",
    ),
    "_account": (
        lambda nom, caps: not call_axes.accepts_account_axis(nom),
        "choisit un compte d'un connecteur multi-comptes : ailleurs, rien à choisir",
    ),
    "_accounts": (
        lambda nom, caps: not call_axes._is_cascade_tool(nom),
        "comptes par étape d'une cascade (`waterfall_*`) : n'a de sens que là",
    ),
}

# Annoncé à la demande seulement (plusieurs comptes chez l'appelant) : voir l'en-tête.
_ANNONCE_DYNAMIQUE = frozenset({"_account"})


@pytest.fixture(scope="module")
def surface():
    """(nom canonique, propriétés servies, propriétés déclarées par l'outil) pour chaque
    outil servi. Un ancien nom encore servi (`deprecations.TOOLS`) est jugé sur son outil
    canonique : c'est sous ce nom que le middleware de contexte le reçoit
    (`ToolAliasMiddleware`, plus externe)."""
    mcp = static_mcp()
    bruts = {t.name: set((t.parameters or {}).get("properties") or {})
             for t in asyncio.run(mcp.list_tools(run_middleware=False))}
    servis = asyncio.run(mcp.list_tools())
    out = []
    for t in servis:
        nom = deprecations.tool_canonique(t.name)
        out.append((nom, set((t.parameters or {}).get("properties") or {}), bruts[nom]))
    return out


def _consomme(nom: str, jeton: str, declares: set) -> bool:
    if jeton in {a.param for a in call_axes.axes_for_call(nom)}:
        return True
    if jeton in declares:
        return True
    # `_run_id` : retiré partout par le middleware, sauf chez qui le traite lui-même.
    return jeton == call_axes.RUN.param and nom not in call_axes.SELF_HANDLED


def test_la_surface_est_construite(surface):
    assert len(surface) > 500, len(surface)


@pytest.mark.parametrize("jeton", JETONS)
def test_chaque_outil_accepte_le_jeton(surface, jeton):
    caps = _capacites()
    exempte, _raison = EXCEPTIONS.get(jeton, (lambda nom, caps: False, ""))
    manquants = sorted(nom for nom, _servi, declares in surface
                       if not exempte(nom, caps) and not _consomme(nom, jeton, declares))
    assert not manquants, (
        f"{len(manquants)} outil(s) refusent `{jeton}` : {manquants[:30]}. Un jeton de "
        "contexte s'accepte par l'axe commun (`call_axes`), jamais par un paramètre "
        "ajouté à la main ; une exception s'inscrit dans EXCEPTIONS avec sa raison.")


@pytest.mark.parametrize("jeton", [j for j in JETONS if j not in _ANNONCE_DYNAMIQUE])
def test_un_jeton_consomme_est_annonce(surface, jeton):
    """Consommé mais pas annoncé = refusé par un client qui valide le schéma. Le jeton
    `_run_id` des capacités est la seule absence voulue (retiré sans être posé, non
    annoncé pour le budget du handshake, `test_call_axes_budget`)."""
    caps = _capacites()
    exempte, _raison = EXCEPTIONS.get(jeton, (lambda nom, caps: False, ""))
    muets = sorted(
        nom for nom, servi, declares in surface
        if jeton in {a.param for a in call_axes.axes_for_call(nom)} and jeton not in servi
        and not exempte(nom, caps))
    assert not muets, (jeton, muets[:30])


def test_les_exceptions_ont_une_raison():
    for jeton, (_predicat, raison) in EXCEPTIONS.items():
        assert jeton in JETONS and len(raison) > 20, jeton


def test_les_outils_exemptes_par_nom_existent(surface):
    """Une liste par nom rouille : un nom qui ne désigne plus rien s'en retire."""
    servis = {nom for nom, _s, _d in surface}
    conditionnels = {"oto_procedure_app"}          # monté selon son bundle
    absents = sorted((_MAIN_SANS_PROJET | _MAIN_SANS_EQUIPE) - servis - conditionnels)
    assert not absents, absents


# ── À l'appel, sur le chemin réel : `_org` posé, retiré, jamais refusé ──────────

class _Msg:
    pass


class _Ctx:                         # comme le vrai MiddlewareContext : PAS de get_state
    pass


def _appel(nom: str, arguments: dict, vu: dict):
    """L'appel tel qu'il arrive en production : le vrai `CallContextMiddleware` lit les
    jetons des arguments BRUTS, pose, retire, puis l'outil valide ce qui reste."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    msg = _Msg()
    msg.name = nom
    msg.arguments = dict(arguments)
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        from oto_mcp import session_org
        vu["org"] = session_org.current_call_org()
        vu["args"] = dict(c.message.arguments)
        outil = await static_mcp().get_tool(nom)
        return await outil.run(c.message.arguments)

    try:
        asyncio.run(CallContextMiddleware().on_call_tool(ctx, _next))
        return ""
    except Exception as e:  # noqa: BLE001 — c'est le motif qu'on inspecte
        return str(e)


@pytest.fixture
def org_resolue(monkeypatch):
    """La garde d'appartenance (`resolve_org_guarded`) lit la base : ici, elle rend l'id."""
    async def _resoudre(org):
        return int(org)
    monkeypatch.setattr(call_axes, "resolve_org_guarded", _resoudre)


@pytest.mark.parametrize("nom", ["oto_tool_schema", "oto_doc_app", "oto_trigger"])
def test_org_pose_et_retire_avant_la_validation(org_resolue, nom):
    """`oto_tool_schema` refusait `_org` (« Unexpected keyword argument »), `oto_doc_app`
    aussi ; `oto_trigger`, capacité, le recevait déjà par l'adaptateur et le reçoit
    désormais par le même axe que tous. L'appel bute là où il buterait SANS le jeton."""
    vu_sans: dict = {}
    vu: dict = {}
    motif_sans = _appel(nom, {}, vu_sans)
    motif = _appel(nom, {"_org": 7}, vu)
    assert "Unexpected keyword argument" not in motif, motif
    assert motif == motif_sans, (motif_sans, motif)
    assert vu["org"] == 7 and "_org" not in vu["args"], vu


def test_oto_call_lit_l_org_d_une_cible_capacite():
    """Via `oto_call`, l'`_org` d'une cible CAPACITÉ était avalé par
    `strip_unconsumed_axes` : aucun axe lu pour elle (`axes_for_call`, la boucle que
    rejoue `tools/meta.executer_cible`) ne le portait, et la cible tournait sous l'org
    maison, sans erreur. L'axe s'y lit désormais comme à un appel direct ; `oto_call`
    seul en reste hors, il déclare et replie le jeton lui-même."""
    assert call_axes.ORG in call_axes.axes_for_call("oto_trigger")
    assert call_axes.ORG not in call_axes.axes_for_call("oto_call")
