"""Le règlement d'un appel (L8, `access/reglement.py`) : un seul point débite, au bord.

Ce que ces tests tiennent, dans l'ordre de ce qui coûterait cher :

1. **Un seul débiteur.** Plus aucun outil ne débite : l'ancien `record_platform_usage`
   n'existe plus, `debiter_plateforme` n'a qu'un appelant par relevé (le règlement d'un
   appel ; le tour de modèle au coût, qui n'est pas un appel d'outil), et le règlement
   n'est appelé que par les trois bords qui posent un relevé d'appel. Un débit ajouté
   ailleurs compterait deux fois.
2. **Rien en silence.** Chaque outil d'un connecteur qui peut servir une clé plateforme
   pose `units` ou est déclaré dans `SANS_DEBIT` — et la déclaration ne ment pas.
3. **Le quota au succès seulement, le budget d'unités au succès comme à l'échec, et
   sans défaire un appel servi.**
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib

import pytest

from oto_mcp import access, providers, session_org
from oto_mcp.access import reglement
from oto_mcp.tool_visibility import namespace_of

RACINE = pathlib.Path(access.__file__).parent.parent
BORDS = {"middleware/call_context.py", "tools/meta.py", "jev_jobs_worker.py"}


def _appels(nom: str) -> dict:
    """`fichier relatif -> nombre d'appels à <nom>(...)`, lu à l'AST dans `oto_mcp/`."""
    vus = {}
    for p in RACINE.rglob("*.py"):
        arbre = ast.parse(p.read_text(encoding="utf-8"))
        n = 0
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.Call):
                continue
            f = noeud.func
            cible = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            # `run_in_threadpool(access.regler_appel, …)` ATTEINT la fonction sans l'appeler.
            atteint = any(isinstance(a, ast.Attribute) and a.attr == nom for a in noeud.args)
            n += (cible == nom) + atteint
        if n:
            vus[str(p.relative_to(RACINE))] = n
    return vus


# ── 1. Un seul débiteur ───────────────────────────────────────────────────────

def test_plus_aucun_outil_ne_debite_lui_meme():
    assert not hasattr(access, "record_platform_usage")
    assert _appels("record_platform_usage") == {}


def test_le_debit_plateforme_n_a_que_le_reglement_pour_appelant():
    """Deux débiteurs, chacun pour un relevé DISTINCT : le règlement d'un appel d'outil, et
    le tour de modèle journalisé à son coût (`tours_au_cout.journaliser_tour`), qui n'est
    pas un appel d'outil — aucun bord ne le règle — et ne débite qu'à la première écriture
    de sa ligne. Un troisième compterait deux fois."""
    assert _appels("debiter_plateforme") == {"access/reglement.py": 1,
                                             "tours_au_cout.py": 1}


def test_le_reglement_n_est_appele_que_par_les_trois_bords():
    assert set(_appels("regler_appel")) == BORDS


def test_l_echec_se_regle_aux_memes_trois_bords_et_nulle_part_ailleurs():
    """Le budget d'unités se règle aussi à l'échec (`regler_echec`) : chaque bord qui
    règle une réussite règle l'échec, et ouvre l'appel avant (`ouvrir_appel`)."""
    assert set(_appels("regler_echec")) == BORDS
    assert set(_appels("ouvrir_appel")) == BORDS


# ── 2. Rien en silence ────────────────────────────────────────────────────────

def _outils_a_cle_plateforme() -> dict:
    """`nom -> fonction` des outils dont le connecteur peut servir une clé plateforme."""
    from _mcp_app import static_mcp
    outils = asyncio.run(static_mcp().list_tools(run_middleware=False))
    out = {}
    for t in outils:
        connecteur = providers.connector_for_namespace(namespace_of(t.name))
        if connecteur is None:
            continue
        porteur = providers.REGISTRY.get(providers.credential_provider(connecteur.name))
        if porteur is not None and "platform" in porteur.auth_modes:
            # Un outil du lecteur générique (`tools/decrits.py`) n'a pas de `fn` : il ne
            # pose jamais `units`, seulement `quantity`.
            out[t.name] = getattr(t, "fn", None)
    return out


def _pose_units(fn) -> bool:
    """La fonction de l'outil, ou une fonction de son module qu'elle atteint, pose-t-elle
    `units` au relevé ? Fermeture par NOM dans le module, à l'AST : un `_run` partagé
    par les outils d'un connecteur compte pour chacun d'eux."""
    import inspect
    if fn is None:
        return False   # le lecteur générique : `quantity`, jamais `units`
    fn = inspect.unwrap(fn)
    module = inspect.getmodule(fn)
    arbre = ast.parse(inspect.getsource(module))
    defs: dict = {}
    for n in ast.walk(arbre):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(n.name, []).append(n)
    # `co_firstlineno` d'une fonction décorée = la ligne de son premier décorateur.
    depart = [n for n in defs.get(fn.__name__, [])
              if min([d.lineno for d in n.decorator_list] + [n.lineno])
              <= fn.__code__.co_firstlineno <= n.end_lineno]
    assert depart, f"{fn.__name__} : définition introuvable dans {module.__name__}"
    vus, a_voir = set(), list(depart)
    while a_voir:
        n = a_voir.pop()
        if id(n) in vus:
            continue
        vus.add(id(n))
        for c in ast.walk(n):
            # `units=` AU RELEVÉ seulement : celui de `resolve_api_key(…, units=)` est
            # la taille du lot que le quota doit couvrir, pas un débit.
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "note_call_trace"
                    and any(k.arg == "units" for k in c.keywords)):
                return True
            if isinstance(c, ast.Name) and c.id in defs:
                a_voir.extend(defs[c.id])
    return False


def test_chaque_outil_a_cle_plateforme_pose_units_ou_est_declare_sans_debit():
    outils = _outils_a_cle_plateforme()
    assert len(outils) > 100, "le registre ne porte presque aucun outil : le test ne prouve rien"
    muets = sorted(n for n, fn in outils.items()
                   if n not in reglement.SANS_DEBIT and not _pose_units(fn))
    assert not muets, (
        f"{len(muets)} outil(s) peuvent servir une clé plateforme sans dire ce qu'ils "
        f"consomment : {', '.join(muets)} — poser `units` (le quota de la clé) ou les "
        "déclarer dans `reglement.SANS_DEBIT`.")


def test_la_declaration_sans_debit_ne_ment_pas():
    outils = _outils_a_cle_plateforme()
    inconnus = sorted(reglement.SANS_DEBIT - set(outils))
    assert not inconnus, f"déclarés sans débit mais introuvables : {inconnus}"
    contradictoires = sorted(n for n in reglement.SANS_DEBIT if _pose_units(outils[n]))
    assert not contradictoires, f"déclarés sans débit mais qui posent `units` : {contradictoires}"


# ── 3. Le règlement lui-même ──────────────────────────────────────────────────

@pytest.fixture
def debits(monkeypatch):
    vus = {"plateforme": [], "tenant": []}
    monkeypatch.setattr(access, "debiter_plateforme",
                        lambda sub, p, n: vus["plateforme"].append((sub, p, n)))
    monkeypatch.setattr(access, "regler_aretes_tenant",
                        lambda ids: vus["tenant"].append(list(ids)))
    return vus


def test_un_appel_sous_cle_plateforme_debite_ses_units(debits):
    access.regler_appel("u", "serper_search", {"platform_carrier": "serper", "units": 3})
    assert debits["plateforme"] == [("u", "serper", 3)]


def test_zero_unite_ne_debite_rien(debits):
    access.regler_appel("u", "apollo_bulk_match", {"platform_carrier": "apollo", "units": 0})
    assert debits["plateforme"] == []


def test_sans_cle_plateforme_les_units_ne_debitent_rien(debits):
    """Sous la clé propre du client, l'outil note ses `units` sans que rien ne bouge."""
    access.regler_appel("u", "serper_search", {"units": 3, "quantity": 3})
    assert debits["plateforme"] == []


def test_units_absentes_se_journalise_sans_debiter(debits, caplog):
    with caplog.at_level(logging.WARNING, logger="oto_mcp.access.reglement"):
        access.regler_appel("u", "un_outil_neuf", {"platform_carrier": "serper"})
    assert debits["plateforme"] == []
    assert "units_absentes" in caplog.text and "un_outil_neuf" in caplog.text
    assert "serper" in caplog.text


def test_un_outil_declare_sans_debit_se_tait(debits, caplog):
    with caplog.at_level(logging.WARNING, logger="oto_mcp.access.reglement"):
        access.regler_appel("u", "tavily_search", {"platform_carrier": "tavily"})
    assert debits["plateforme"] == [] and not caplog.records


def test_les_aretes_de_tenant_se_debitent_au_reglement(debits):
    access.regler_appel(None, "serper_search", {"tenant_grant_ids": [7, 7]})
    assert debits["tenant"] == [[7, 7]]


def test_un_debit_en_panne_ne_defait_pas_l_appel(monkeypatch, caplog):
    def _panne(*a):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(access, "debiter_plateforme", _panne)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.access.reglement"):
        access.regler_appel("u", "serper_search", {"platform_carrier": "serper", "units": 2})
    assert "règlement ÉCHOUÉ" in caplog.text and "serper_search" in caplog.text


def test_le_budget_tenant_se_note_au_releve_et_se_debite_au_reglement(monkeypatch):
    from oto_mcp import grants_chain
    from oto_mcp.access import tenant_budget
    from oto_mcp.db import grants as db_grants
    verdict = grants_chain.ChainVerdict(True, "env", quota=0, grant_id=7,
                                        resource_id="tenant:t:serper", grantee=("org", "1"))
    monkeypatch.setattr(grants_chain, "tenant_rung", lambda *a: verdict)
    bumps = []
    monkeypatch.setattr(db_grants, "bump_counter", lambda gid, calls=1: bumps.append(gid))
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    try:
        tenant_budget.enforce("t", "serper", 1)
    finally:
        session_org.reset_call_trace(jeton)
    assert bumps == [] and releve == {"tenant_grant_ids": [7]}
    tenant_budget.regler_aretes_tenant(releve["tenant_grant_ids"])
    assert bumps == [7]
    # Hors relevé d'appel, il n'y a pas de bord : débit à la résolution, comme avant.
    tenant_budget.enforce("t", "serper", 1)
    assert bumps == [7, 7]


# ── 4. Les bords : au succès, jamais sur un échec ─────────────────────────────

class _Msg:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Ctx:
    def __init__(self, msg):
        self.message = msg


@pytest.mark.asyncio
async def test_le_middleware_regle_un_appel_reussi_et_pas_un_appel_en_echec(monkeypatch,
                                                                           debits):
    from oto_mcp.middleware import call_context as mwmod
    mw = mwmod.CallContextMiddleware(reserved_org_tools=set())
    monkeypatch.setattr(mwmod, "current_user_sub_from_token", lambda: "u")

    async def _reussi(ctx):
        session_org.note_call_trace(platform_carrier="serper", units=2)
        return "ok"

    async def _echec(ctx):
        session_org.note_call_trace(platform_carrier="serper", units=2)
        raise RuntimeError("le fournisseur a refusé")

    assert await mw.on_call_tool(_Ctx(_Msg("serper_search", {"q": "x"})), _reussi) == "ok"
    assert debits["plateforme"] == [("u", "serper", 2)]
    with pytest.raises(RuntimeError):
        await mw.on_call_tool(_Ctx(_Msg("serper_search", {"q": "x"})), _echec)
    assert debits["plateforme"] == [("u", "serper", 2)]


def test_la_cible_d_oto_call_garde_les_cles_du_reglement():
    """L'enveloppe d'un `oto_call` ne reçoit pas les clés que la cible vient de régler :
    sans quoi le middleware les réglerait une seconde fois sur la ligne `oto_call`."""
    src = (RACINE / "tools/meta.py").read_text(encoding="utf-8")
    assert "k not in access.CLES_REGLEMENT" in src
    assert set(reglement.CLES_REGLEMENT) == {"units", "platform_carrier", "tenant_grant_ids",
                                             "credits_appel"}
