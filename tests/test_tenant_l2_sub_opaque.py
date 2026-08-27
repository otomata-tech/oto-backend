"""Lot L2 (ADR 0052) — tripwire « le sub reste une chaîne opaque ».

**L'énoncé naïf est faux.** « Aucun call-site ne parse `:` » ne tient pas une
journée : au scope membre, `entity_id` vaut `{org}:{sub}` et trois endroits le
découpent LÉGITIMEMENT (`credentials_store.list_member_orgs_for`,
`connectors_instances._shared_ref` / `._shared_owner`). Un tripwire qui affirme une
chose fausse se fait désarmer — ou pire, s'écrit en exemptant nommément le call-site
qui compte.

**L'énoncé qui tient**, et que ce fichier vérifie :

    le sub n'est jamais découpé ; `entity_id` ne l'est qu'à son PREMIER `:`.

D'où les trois règles, sans allowlist (une exemption nommée rouvrirait le trou
qu'elle prétend garder) :

- découper une expression qui DÉSIGNE un sub : jamais, quelle que soit la méthode ;
- découper un `entity_id` : `partition(":")` ou `split(":", 1)`, jamais `rsplit` /
  `rpartition` (ils prennent le DERNIER `:` — avec un sub qualifié, ils coupent
  DANS le sub) ni `split(":")` nu (`[1]` devient le slug du tenant, pas le sub) ;
- un découpage d'`entity_id` doit être gardé par le scope MEMBRE : pour
  `entity_type='user'`, **`entity_id` EST le sub** — un sub qualifié y met un `:`
  là où il n'y en avait aucun, et le code croirait lire `{org}:{sub}`.

Deux découpages ne sont PAS des oublis et méritent d'être nommés ici :

- `instance_refs.parse_ref` fait un `split(":")` sur un ref qui CONTIENT un sub —
  mais le sub y est percent-encodé (`quote(sub, safe="")`), donc son `:` devient
  `%3A`. C'est vérifié empiriquement plus bas, pas par une exemption.
- `access._sub_matches_scopes` et `capabilities/platform_connectors` (ex-
  `api_routes_connectors`, migré le 2026-08-27) partitionnent des scopes
  `user:<sub>` au premier `:` et recomparent le reste EN ENTIER — même forme que
  `entity_id`, même sûreté. Testé empiriquement plus bas.
"""
from __future__ import annotations

import ast
import pathlib
import re

_PKG = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"

# Les méthodes qui coupent une chaîne sur un séparateur.
_SPLIT_METHODS = {"split", "rsplit", "partition", "rpartition"}
# Celles qui coupent au PREMIER séparateur (les seules sûres sur `{org}:{sub}`).
_FIRST_SEPARATOR = {"partition", "split"}


class _Site:
    __slots__ = ("path", "line", "method", "receiver", "words", "maxsplit",
                 "function", "body")

    def __repr__(self) -> str:  # pragma: no cover — confort de diagnostic
        return f"{self.path}:{self.line} {self.receiver}.{self.method}(':')"


def _words(source: str) -> set:
    """Mots d'une expression source — `str(r["entity_id"])` → {str, r, entity, id}.

    Découper sur tout ce qui n'est pas alphanumérique (le `_` compris) est ce qui
    permet de reconnaître `new_sub` comme un sub sans confondre `subdomain` ou
    `subscription` avec un."""
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", source or "") if w}


def _colon_split_sites() -> list:
    """Tous les découpages sur un séparateur contenant `:` dans `oto_mcp/`."""
    sites = []
    for path in sorted(_PKG.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        # Fonctions englobantes, pour retrouver la garde de scope autour d'un site.
        funcs = [(n.lineno, getattr(n, "end_lineno", n.lineno), n)
                 for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _SPLIT_METHODS
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and ":" in node.args[0].value):
                continue
            enclosing = min(
                (f for f in funcs if f[0] <= node.lineno <= f[1]),
                key=lambda f: f[1] - f[0], default=None)
            site = _Site()
            site.path = path.relative_to(_PKG.parent)
            site.line = node.lineno
            site.method = node.func.attr
            site.receiver = ast.get_source_segment(src, node.func.value) or "?"
            site.words = _words(site.receiver)
            site.maxsplit = (
                len(node.args) > 1
                or any(k.arg == "maxsplit" for k in node.keywords))
            site.function = enclosing[2].name if enclosing else "<module>"
            site.body = (ast.get_source_segment(src, enclosing[2]) or ""
                         if enclosing else "")
            sites.append(site)
    return sites


_SITES = _colon_split_sites()


def test_le_scan_trouve_bien_des_decoupages():
    """Un tripwire qui ne voit plus rien passe vert pour de mauvaises raisons."""
    assert len(_SITES) >= 8, (
        f"seulement {len(_SITES)} découpages détectés — le scan AST ne voit plus le "
        "code (fichiers déplacés ? autre forme d'appel ?), donc les deux règles "
        "ci-dessous ne gardent plus rien.")


def test_le_sub_nest_jamais_decoupe():
    """Règle 1. En aval du point de qualification, le sub est OPAQUE : le découper,
    c'est reconstruire l'étage tenant à la main, ailleurs, sans le registre."""
    coupables = [str(s) for s in _SITES if {"sub", "subs"} & s.words]
    assert not coupables, (
        "le sub est découpé sur `:` — il est une chaîne OPAQUE (ADR 0052 §2) :\n  "
        + "\n  ".join(coupables)
        + "\nUn sub de tenant tiers vaut `<slug>:<sub>` : le couper rend deux "
          "moitiés dont aucune n'est une identité. Pour savoir de quel tenant "
          "relève un sub, classer par préfixe — `tenancy.current().tenant_of(sub)`.")


def test_entity_id_ne_se_decoupe_quau_premier_deux_points():
    """Règle 2. `entity_id` membre vaut `{org}:{sub}` : le premier `:` sépare, tout
    le reste EST le sub — y compris quand il en contient un."""
    coupables = []
    for s in _SITES:
        if not ({"entity", "eid"} & s.words):
            continue
        if s.method not in _FIRST_SEPARATOR or (s.method == "split" and not s.maxsplit):
            coupables.append(f"{s} — utiliser `partition(':')`")
    assert not coupables, (
        "`entity_id` découpé ailleurs qu'à son PREMIER `:` :\n  "
        + "\n  ".join(coupables)
        + "\n`rsplit`/`rpartition` prennent le DERNIER `:` et `split(':')[1]` prend "
          "le deuxième segment : sur `12:tulina:abc123`, les deux coupent DANS le "
          "sub. `partition(':')` tient parce que le reste est recomparé en entier.")


def test_un_decoupage_dentity_id_est_garde_par_le_scope_membre():
    """Règle 3 — le piège nommé par l'issue. Pour `entity_type='user'`, `entity_id`
    EST le sub : un sub qualifié y met un `:` là où il n'y en avait AUCUN, et le
    code lirait `tulina` comme un id d'org et `abc123` comme le sub."""
    nus = [str(s) for s in _SITES
           if ({"entity", "eid"} & s.words)
           and not re.search(r"\bMEMBER\b|[\"']member[\"']", s.body)]
    assert not nus, (
        "`entity_id` découpé sans garde de scope membre :\n  "
        + "\n  ".join(nus)
        + "\nLa fonction doit contraindre `entity_type` à `member` (constante "
          "`credentials_store.MEMBER`, ou un test explicite) : au scope `user`, "
          "`entity_id` est le sub lui-même, et un sub qualifié le ferait passer "
          "pour un `{org}:{sub}`.")


# --- Ce que le statique ne peut pas dire : on l'exerce ----------------------

_QUALIFIE = "tulina:abc123"


def test_un_entity_id_membre_se_relit_juste_avec_un_sub_qualifie():
    """Le motif exact des trois call-sites légitimes, sur un sub qualifié."""
    from oto_mcp import credentials_store

    eid = credentials_store.member_id(12, _QUALIFIE)
    assert eid == f"12:{_QUALIFIE}"
    org, _, sub = eid.partition(":")
    assert org == "12" and sub == _QUALIFIE, "le sub doit ressortir ENTIER"
    # Le piège : la même expression avec les méthodes interdites.
    assert eid.rpartition(":")[2] != _QUALIFIE
    assert eid.split(":")[1] != _QUALIFIE


def test_un_ref_dinstance_survit_a_un_sub_qualifie():
    """`instance_refs` fait un `split(':')` sur un ref qui contient un sub — sûr
    parce que les segments libres sont percent-encodés. Vérifié, pas supposé."""
    from oto_mcp import instance_refs

    ref = instance_refs.make_member_ref(12, _QUALIFIE, "folk", account="a@b.test")
    assert "%3A" in ref, "le sub doit être percent-encodé dans le ref"
    parsed = instance_refs.parse_ref(ref)
    assert parsed.level == "member" and parsed.org_id == 12
    assert parsed.sub == _QUALIFIE
    assert parsed.connector == "folk" and parsed.account == "a@b.test"
    assert instance_refs.format_ref(parsed) == ref


def test_un_scope_nominatif_reconnait_un_sub_qualifie():
    """`user:<sub>` (allowlists `share_down`, prêts `share_side`, ADR 0044) : même
    forme que `entity_id`, même sûreté — le premier `:` sépare, le reste est
    recomparé en entier."""
    from oto_mcp import access

    assert access._sub_matches_scopes(_QUALIFIE, [f"user:{_QUALIFIE}"])
    assert not access._sub_matches_scopes(_QUALIFIE, ["user:abc123"])
    assert not access._sub_matches_scopes("abc123", [f"user:{_QUALIFIE}"])


def test_un_sub_qualifie_ne_ressemble_a_aucun_autre_entity_id():
    """La collision qu'on veut rendre impossible : un `entity_id` de scope `user`
    (= le sub) ne doit jamais pouvoir se lire comme un `{org}:{sub}` de scope
    membre. Le premier segment d'un sub qualifié est un slug, jamais un entier."""
    from oto_mcp import tenancy

    org_like, _, _reste = tenancy.qualify("tulina", "abc123").partition(":")
    assert not org_like.isdigit(), (
        "un slug de tenant numérique ferait passer un `entity_id` de scope user "
        "pour un `entity_id` de scope membre")
