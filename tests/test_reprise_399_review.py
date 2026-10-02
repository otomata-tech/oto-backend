"""Reprise de la PR #399 — les trois correctifs de la review REQUEST_CHANGES (25/08).

F1 (tools/meta.py, `oto_call`) : la boucle de rejeu des axes lit `axes_for_call`
(les axes LUS à l'appel), plus `axes_for` (les seuls ANNONCÉS statiquement) — sinon
`_account=` sur un connecteur à clé d'API « simple » (serper, hunter…) n'est pas
consommé, `strip_unconsumed_axes` l'avale SANS erreur et l'appel part sur le compte
par défaut.

F2 (access/resolve.py) : « seul le dernier palier à clé lève » avait deux trous quand un
compte est NOMMÉ (param / axe `_account=` / épinglage projet) :
  (a) org de contexte None → barreaux membre ET org sautés → `_account=x` explicite
      résolvait la clé PLATEFORME en silence (usurpation d'identité de credential) ;
  (b) connecteur multi non org-partageable (google, browser, planity…) → compte
      introuvable → message générique « Aucune clé configurée » (régression vs main,
      qui levait « Compte x introuvable »).
La garde vit désormais APRÈS la marche : compte nommé + gagnant None/platform ⇒
« Compte introuvable », jamais un repli.

F3 (access/resolve.py) : l'endpoint MCP anonyme (`<slug>.mcp.oto.cx`) lisait le barreau org
en `account=''` EN DUR ; or `ensure_named_coexistence` migre la ligne `''` vers
« principal » au premier compte nommé → l'endpoint cessait de résoudre le provider
pendant que `has_org_secret` (account-blind) disait « configuré ». La sonde anonyme
sélectionne désormais le compte comme le barreau org du chemin réel
(unique / `is_default`).

Harnais : celui de la review — appels directs, seams DB stubbés (patron
`test_multi_account_shared_scopes`).
"""
import ast
import asyncio
import pathlib

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, call_axes, session_org

# Sans base : le palier plateforme lit les droits de la personne (#1090).
pytestmark = pytest.mark.usefixtures("sans_droit_declare")

META_PY = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp" / "tools" / "meta.py"


# ── F1 — oto_call rejoue les axes LUS, pas les seuls annoncés ─────────────────

def test_account_axis_is_read_beyond_the_static_advert():
    """La forme du bug : sur un connecteur à clé d'API simple, `_account` n'est PAS
    dans les axes statiques (annonce curée) mais DOIT être dans les axes lus à
    l'appel. Une boucle de rejeu bâtie sur `axes_for` ne le consommerait donc
    jamais — et le balayage le retirerait sans erreur (assert final)."""
    static = {a.param for a in call_axes.axes_for("serper_search")}
    at_call = {a.param for a in call_axes.axes_for_call("serper_search")}
    assert "_account" not in static
    assert "_account" in at_call
    # Ce que faisait le bug : axe non consommé → avalé, aucun signal.
    args = {"q": "acme", "_account": "eu"}
    call_axes.strip_unconsumed_axes(args)
    assert args == {"q": "acme"}


def test_replay_loop_semantics_consume_and_pin_the_account():
    """Rejoue la boucle d'`oto_call` (celle du correctif) : `_account` est POPpé des
    args et POSÉ dans le contexte d'appel — le compte atteint la résolution.
    Tout vit DANS la coroutine (un token de ContextVar ne se reset pas depuis un
    autre contexte que celui d'`asyncio.run`)."""

    async def _replay():
        args = {"q": "acme", "_account": "eu"}
        undo = []
        for axis in call_axes.axes_for_call("serper_search"):
            if axis.param in args:
                undo.extend(await axis.pin_for(args.pop(axis.param), "serper_search"))
        pinned = session_org.current_call_account()
        call_axes.strip_unconsumed_axes(args)
        for reset, tok in reversed(undo):
            reset(tok)
        return pinned, args, session_org.current_call_account()

    pinned, args, after = asyncio.run(_replay())
    assert pinned == "eu"          # le compte a atteint le contexte de résolution
    assert args == {"q": "acme"}   # l'axe a été consommé, pas avalé
    assert after is None           # l'undo restaure le contexte


def test_oto_call_source_uses_axes_for_call():
    """TRIPWIRE (AST) — la boucle de rejeu des axes appelle `axes_for_call`, jamais
    `axes_for` nu : revenir en arrière ferait avaler `_account=` par
    `strip_unconsumed_axes` (review #399 F1). Dérivé des sources, comme les
    tripwires de `test_call_axes_business_param_collision`.

    La boucle vit dans `executer_cible`, le corps d'`oto_call` partagé avec les
    recettes ; elle y est lancée hors boucle d'événements, donc `axes_for_call` est
    ATTEINT (passé à `run_in_threadpool`) plutôt qu'appelé directement."""
    tree = ast.parse(META_PY.read_text(encoding="utf-8"))
    fn = next(node for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name == "executer_cible")
    called = {node.attr for node in ast.walk(fn)
              if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
              and node.value.id == "call_axes"}
    assert "axes_for_call" in called
    assert "axes_for" not in called


# ── Harnais F2/F3 — seams stubbés ─────────────────────────────────────────────

class _Con:
    """Connecteur multi-compte org-partageable (le cas serper)."""
    auth_multi_account = True
    auth_modes = ("byo", "byo_org")
    personal_cross_org = False
    name = "serper"


class _PlatCon(_Con):
    """Variante avec palier plateforme (le trou (a))."""
    auth_modes = ("byo", "byo_org", "platform")


class _MemberOnlyCon(_Con):
    """Connecteur multi NON org-partageable (planity, browser, google…)."""
    auth_modes = ("byo",)
    name = "planity"


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.providers, "connector_for_provider", lambda p: _Con())
    monkeypatch.setattr(access.providers, "is_byo_user", lambda p: True)
    monkeypatch.setattr(access, "project_pinned_identity",
                        lambda prov, project_id=None: None)
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", {"serper"})
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)
    # Aucune clé membre par défaut.
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    yield


def _org_accounts(monkeypatch, names, default=None):
    rows = [{"account": n, "meta": {"is_default": n == default}} for n in names]
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, con: rows if et == "org" else [])


def _org_vault(monkeypatch, mapping):
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda oid, prov, account="": mapping.get(account))


# ── F2 (a) — org de contexte None : un compte nommé ne résout JAMAIS platform ──

def test_named_account_without_context_org_never_resolves_platform(monkeypatch):
    """Le trou (a) : sans org de contexte, les barreaux membre et org sont sautés ;
    le gagnant serait la clé PLATEFORME — répondre avec elle à un `_account=x`
    explicite est une usurpation silencieuse. On refuse « introuvable »."""
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.providers, "connector_for_provider",
                        lambda p: _PlatCon())
    monkeypatch.setattr(access, "_resolve_platform_grant",
                        lambda s, p, o: {"label": "oto", "secret": "PK",
                                         "daily_quota": 999999})
    monkeypatch.setattr(access.db, "get_usage_today", lambda s, p: 0)
    with pytest.raises(McpError) as e:
        access.resolve_credential("serper", sub="u1", account="x",
                                  emit_on_failure=False)
    assert "introuvable" in str(e.value)


def test_unnamed_account_without_context_org_still_reaches_platform(monkeypatch):
    """Contre-épreuve : SANS compte nommé, le repli plateforme reste le comportement
    normal — la garde ne vise que le compte nommé."""
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.providers, "connector_for_provider",
                        lambda p: _PlatCon())
    monkeypatch.setattr(access, "_resolve_platform_grant",
                        lambda s, p, o: {"label": "oto", "secret": "PK",
                                         "daily_quota": 999999})
    monkeypatch.setattr(access.db, "get_usage_today", lambda s, p: 0)
    rc = access.resolve_credential("serper", sub="u1", emit_on_failure=False)
    assert rc.key == "PK" and rc.is_platform


# ── F2 (b) — connecteur multi non org-partageable : « introuvable », pas
#             « aucune clé » ─────────────────────────────────────────────────────

def test_named_account_on_member_only_connector_says_not_found(monkeypatch):
    """Le trou (b) : le compte nommé n'existe pas au palier membre, aucun palier
    partagé n'existe (connecteur non org-shareable) → le message doit rester
    « Compte `x` introuvable » (comportement de main pour google/browser), pas le
    générique « Aucune clé configurée pour toi »."""
    monkeypatch.setattr(access.providers, "connector_for_provider",
                        lambda p: _MemberOnlyCon())
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", set())
    monkeypatch.setattr(access, "_reachable_hint", lambda *a, **k: "")
    with pytest.raises(McpError) as e:
        access.resolve_credential("planity", sub="u1", account="x",
                                  emit_on_failure=False)
    assert "Compte `x` introuvable" in str(e.value)
    assert "Aucune clé" not in str(e.value)


# ── Mineur — l'ambiguïté d'un palier partagé nomme son scope ──────────────────

def test_shared_rung_ambiguity_names_its_scope(monkeypatch):
    """Deux comptes d'org sans défaut : le message renvoie vers
    `oto_identity(op='list', scope='org')` — sans le scope, l'agent listerait ses
    comptes MEMBRE et ne verrait rien."""
    _org_accounts(monkeypatch, ["eu", "us"])
    _org_vault(monkeypatch, {"eu": "K-EU", "us": "K-US"})
    with pytest.raises(McpError) as e:
        access.resolve_credential("serper", sub="u1", emit_on_failure=False)
    assert "scope='org'" in str(e.value)


def test_member_rung_ambiguity_message_is_unchanged(monkeypatch):
    """Le palier MEMBRE garde son message historique (scope member = défaut du
    tool, rien à préciser)."""
    monkeypatch.setattr(access.db, "get_member_api_key", lambda *a, **k: None)
    rows = [{"account": "a", "meta": {}}, {"account": "b", "meta": {}}]
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, con: rows if et == access.credentials_store.MEMBER else [])
    with pytest.raises(McpError) as e:
        access.resolve_credential("serper", sub="u1", emit_on_failure=False)
    assert "Plusieurs comptes" in str(e.value)
    assert "scope=" not in str(e.value)


# ── F3 — l'endpoint anonyme sélectionne le compte d'org comme le chemin réel ──

def test_anon_resolves_after_legacy_row_migrated_to_principal(monkeypatch):
    """Rejoue la séquence du bug : l'org avait une ligne `''`, un compte nommé a
    été posé → `ensure_named_coexistence` a migré `''` vers « principal », puis le
    compte nommé a été retiré. L'endpoint anonyme doit résoudre « principal »
    (compte unique), plus jamais chercher `''` en dur."""
    _org_accounts(monkeypatch, ["principal"])
    _org_vault(monkeypatch, {"principal": "K-ORG"})
    rc = access._resolve_credential_anon("serper", "auto", 7)
    assert rc.key == "K-ORG" and rc.mode == "org" and rc.account == "principal"


def test_anon_picks_the_org_default_among_named_accounts(monkeypatch):
    """Plusieurs comptes d'org : l'anonyme suit le défaut posé (`is_default`),
    comme le barreau org du chemin réel."""
    _org_accounts(monkeypatch, ["eu", "us"], default="us")
    _org_vault(monkeypatch, {"eu": "K-EU", "us": "K-US"})
    rc = access._resolve_credential_anon("serper", "auto", 7)
    assert rc.key == "K-US" and rc.account == "us"


def test_anon_legacy_single_row_resolves_as_before(monkeypatch):
    """Iso-comportement : une org restée mono (`''` seule) résout comme avant."""
    _org_accounts(monkeypatch, [""])
    _org_vault(monkeypatch, {"": "K-ORG"})
    rc = access._resolve_credential_anon("serper", "auto", 7)
    assert rc.key == "K-ORG" and rc.mode == "org" and rc.account == ""
