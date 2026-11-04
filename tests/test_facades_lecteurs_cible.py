"""Où les lecteurs lisent `access` et `org_store` : la cible de #896, gardée à zéro.

Les deux paquets sont des FAÇADES : `access.<nom>` et `org_store.<nom>` ré-exportent
à plat leurs sous-modules, et une écriture sur la façade se PROPAGE à chaque
sous-module qui détient le nom. La propagation descend, elle ne remonte jamais :

| on patche                         | lecteur interne (`scope.X`) | lecteur externe (`access.X`) |
|-----------------------------------|-----------------------------|------------------------------|
| la façade `access.X`              | atteint (propagation)       | atteint                      |
| le sous-module `access.scope.X`   | atteint                     | **RATÉ, en silence**         |

D'où la cible, la seule où toute doublure atteint tous ses lecteurs :

- un lecteur HORS du paquet lit la façade — jamais un sous-module qu'elle ré-exporte ;
- un lecteur interne lit son voisin par le module (`scope.current_org(...)`) ;
- un test patche la façade — jamais un sous-module qu'elle ré-exporte ;
- la propagation reste le seul pont entre les deux.

Deux trous ferment ici, chacun à zéro :

1. **Côté production** : aucun module hors du paquet n'importe un sous-module
   façadé ni ne le lit (`access.quotas.paid_option_for`), et aucun n'importe une
   FONCTION par son nom (`from oto_mcp.access import current_org`) — ce lien est figé
   à l'import, aucune doublure ne l'atteint jamais. Types et constantes restent
   importables depuis la façade.
2. **Côté tests** : aucun test ne patche un sous-module façadé
   (`monkeypatch.setattr(access.scope, "current_org", …)`) — la doublure n'atteindrait
   pas les ~680 appels qui lisent la façade.

Les sous-modules qu'une façade ne ré-exporte PAS (`access.chain_shadow`,
`access.chain_resolution`, …) n'ont pas d'autre adresse : ils se lisent et se patchent
en direct. Ce qui est façadé, c'est ce que la façade elle-même déclare (`_OWNERS`),
pas une liste recopiée ici.

Ce que cette garde ne voit pas : une assertion que tient une capture trop large
(`pytest.raises(Exception)`, un chemin best-effort) quand la doublure n'est pas
atteinte. Le premier barreau de #896 en a resserré treize ; la classe se rejoue en
remplaçant la règle d'écriture de la façade, pas par lecture du code.
"""
from __future__ import annotations

import ast
import functools
import inspect
import pathlib
import pkgutil
import types

from oto_mcp import access, org_store

RACINE = pathlib.Path(access.__file__).resolve().parents[2]
PROD = RACINE / "oto_mcp"
TESTS = RACINE / "tests"
FACADES = {"access": access, "org_store": org_store}


def _sous_modules(facade) -> dict:
    """Les sous-modules que la façade ré-exporte — lus dans SA carte de propagation,
    pas recopiés ici. Rend {objet module: nom pointé}."""
    pre = facade.__name__ + "."
    return {m: m.__name__ for mods in facade._OWNERS.values() for m in mods
            if m.__name__.startswith(pre)}


SOUS = {m: n for f in FACADES.values() for m, n in _sous_modules(f).items()}


def _module_de(chemin: pathlib.Path) -> tuple[str, bool]:
    rel = chemin.resolve().relative_to(RACINE).with_suffix("")
    parts = list(rel.parts)
    paquet = parts[-1] == "__init__"
    if paquet:
        parts = parts[:-1]
    return ".".join(parts), paquet


def _absolu(module: str, paquet: bool, noeud: ast.ImportFrom) -> str:
    if noeud.level == 0:
        return noeud.module or ""
    base = module.split(".")
    if not paquet:
        base = base[:-1]
    base = base[: len(base) - (noeud.level - 1)]
    return ".".join(base + ([noeud.module] if noeud.module else []))


@functools.lru_cache(maxsize=None)
def _objet(chemin: str):
    """L'objet que désigne un chemin pointé, ou None s'il ne se résout pas."""
    try:
        return pkgutil.resolve_name(chemin)
    except Exception:  # noqa: BLE001 — un chemin qui ne se résout pas n'est pas un module
        return None


def _sous(chemin) -> str | None:
    """Le nom du sous-module FAÇADÉ que désigne ce chemin, sinon None."""
    if chemin is None:
        return None
    objet = _objet(chemin)
    return SOUS.get(objet) if isinstance(objet, types.ModuleType) else None


def _noms(arbre: ast.AST, module: str, paquet: bool) -> dict:
    """Nom local -> chemin pointé, pour tout import du fichier (y compris paresseux,
    dans le corps d'une fonction)."""
    noms: dict = {}
    for n in ast.walk(arbre):
        if isinstance(n, ast.Import):
            for a in n.names:
                noms[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom):
            source = _absolu(module, paquet, n)
            for a in n.names:
                noms[a.asname or a.name] = f"{source}.{a.name}"
    return noms


def _chemin(expr: ast.AST, noms: dict):
    if isinstance(expr, ast.Name):
        return noms.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = _chemin(expr.value, noms)
        return f"{base}.{expr.attr}" if base else None
    return None


def lectures_fautives(source: str, chemin: pathlib.Path) -> list[str]:
    """Les lectures d'un module de PRODUCTION hors paquet qui contournent la façade."""
    module, paquet = _module_de(chemin)
    arbre = ast.parse(source)
    noms = _noms(arbre, module, paquet)
    fautes = []
    for n in ast.walk(arbre):
        if isinstance(n, ast.Import):
            for a in n.names:
                if _sous(a.name):
                    fautes.append(f"{n.lineno}: import {a.name} (sous-module façadé)")
        elif isinstance(n, ast.ImportFrom):
            source_mod = _absolu(module, paquet, n)
            if _sous(source_mod):
                fautes.append(f"{n.lineno}: from {source_mod} import … (sous-module façadé)")
            elif _objet(source_mod) in FACADES.values():
                for a in n.names:
                    if _sous(f"{source_mod}.{a.name}"):
                        fautes.append(f"{n.lineno}: from {source_mod} import {a.name} "
                                      "(sous-module façadé)")
                    elif inspect.isfunction(_objet(f"{source_mod}.{a.name}")):
                        fautes.append(f"{n.lineno}: from {source_mod} import {a.name} "
                                      "(fonction liée à l'import : aucune doublure ne l'atteint)")
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            sous = _sous(_chemin(n.value, noms))
            if sous:
                fautes.append(f"{n.lineno}: {sous}.{n.attr}")
    return fautes


def patchs_fautifs(source: str, chemin: pathlib.Path) -> list[str]:
    """Les doublures d'un TEST posées sur un sous-module façadé."""
    module, paquet = _module_de(chemin)
    arbre = ast.parse(source)
    noms = _noms(arbre, module, paquet)
    fautes = []
    for n in ast.walk(arbre):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        f = n.func
        verbe = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if verbe in ("setattr", "object", "delattr") and len(n.args) >= 2 \
                and isinstance(n.args[1], ast.Constant):
            sous = _sous(_chemin(n.args[0], noms))
            if sous:
                fautes.append(f"{n.lineno}: {sous}.{n.args[1].value}")
        if verbe in ("setattr", "patch", "delattr") and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str) and "." in n.args[0].value:
            cible = n.args[0].value.rsplit(".", 1)
            sous = _sous(cible[0])
            if sous:
                fautes.append(f"{n.lineno}: {sous}.{cible[1]}")
    return fautes


def _hors_paquets(racine: pathlib.Path):
    for f in sorted(racine.rglob("*.py")):
        rel = f.relative_to(RACINE).parts
        if rel[:2] in (("oto_mcp", "access"), ("oto_mcp", "org_store")):
            continue
        yield f


def test_les_facades_declarent_des_sous_modules():
    """Une carte vide rendrait les deux gardes vertes et inutiles."""
    noms = set(SOUS.values())
    assert {"oto_mcp.access.scope", "oto_mcp.access.quotas", "oto_mcp.access.resolve",
            "oto_mcp.org_store.orgs", "oto_mcp.org_store.vault"} <= noms
    assert "oto_mcp.access.chain_shadow" not in noms


def test_aucun_lecteur_de_production_ne_contourne_la_facade():
    fautes = {str(f.relative_to(RACINE)): x for f in _hors_paquets(PROD)
              if (x := lectures_fautives(f.read_text(encoding="utf-8"), f))}
    assert not fautes, (
        "lecture d'un sous-module façadé hors du paquet : passer par la façade "
        "(`access.<nom>` / `org_store.<nom>`), sinon une doublure posée sur elle peut "
        f"rater ce lecteur — {fautes}")


def test_aucun_test_ne_patche_un_sous_module_facade():
    fautes = {str(f.relative_to(RACINE)): x for f in sorted(TESTS.rglob("*.py"))
              if (x := patchs_fautifs(f.read_text(encoding="utf-8"), f))}
    assert not fautes, (
        "doublure posée sur un sous-module façadé : la poser sur la façade "
        "(`monkeypatch.setattr(access, \"<nom>\", …)`), qui la propage aux lecteurs "
        f"internes ET atteint les externes — {fautes}")


# ── La garde mord : chaque forme fautive, plantée, est vue ─────────────────────

_PLANTE_PROD = '''
from . import access, org_store
from .access import quotas
from .access.scope import current_org
from .org_store.members import ORG_ROLES
from .access import current_group
from .access import ResolvedCredential, chain_shadow
import oto_mcp.access.rbac as r

def f():
    from .access import entitlements as e
    return access.scope.current_org(), access.chain_shadow.ACCORD, e.verifier_defauts()
'''

_PLANTE_TEST = '''
from unittest import mock
from oto_mcp import access, org_store
from oto_mcp.access import scope, chain_shadow
import oto_mcp.org_store.vault as V

def test_x(monkeypatch):
    monkeypatch.setattr(scope, "current_org", lambda s: 1)
    monkeypatch.setattr(access.cascade, "walk_cascade", lambda *a: None)
    monkeypatch.setattr(V, "get_org_secret", lambda *a: None)
    monkeypatch.setattr("oto_mcp.org_store.orgs.get_org", lambda *a: None)
    mock.patch.object(access.rbac, "current_user_sub_from_token")
    monkeypatch.setattr(access, "current_org", lambda s: 1)
    monkeypatch.setattr(chain_shadow, "barreau_gagnant", lambda *a: None)
'''


def test_la_garde_de_production_mord_sur_un_cas_plante():
    fautes = lectures_fautives(_PLANTE_PROD, PROD / "plante.py")
    attendues = ["from oto_mcp.access import quotas", "oto_mcp.access.scope",
                 "oto_mcp.org_store.members", "import current_group", "access.rbac",
                 "import entitlements", "access.scope.current_org",
                 "access.entitlements.verifier_defauts"]
    for a in attendues:
        assert any(a in x for x in fautes), (a, fautes)
    # Ce qui est permis ne crie pas : un type depuis la façade, un sous-module
    # que la façade ne ré-exporte pas.
    assert not any("ResolvedCredential" in x or "chain_shadow" in x for x in fautes), fautes
    assert len(fautes) == 8, fautes


def test_la_garde_des_tests_mord_sur_un_cas_plante():
    fautes = patchs_fautifs(_PLANTE_TEST, TESTS / "test_plante.py")
    attendues = ["access.scope.current_org", "access.cascade.walk_cascade",
                 "org_store.vault.get_org_secret", "oto_mcp.org_store.orgs.get_org",
                 "access.rbac.current_user_sub_from_token"]
    for a in attendues:
        assert any(a in x for x in fautes), (a, fautes)
    # La façade et un sous-module hors façade restent les bonnes adresses.
    assert len(fautes) == 5, fautes
