"""Le journal des entrées et sorties d'org nomme TOUJOURS son acteur (otomata-tech/oto#145).

`add_org_member` et `remove_org_member` prennent `actor` avec une valeur par défaut
(`None` = le système) pour ne pas réécrire les centaines d'appels des bancs. Le défaut
est donc un piège pour le code de production : un chemin neuf qui l'oublie écrirait
« le système » à la place de la personne qui a agi, et rien ne rougirait. Ce banc
l'interdit : dans `oto_mcp/`, chaque appel à ces deux fonctions passe `actor=`
explicitement, `actor=None` compris (qui dit alors « c'est le système », à dessein).

Sans base : on lit le code, rien d'autre. Le comportement contre PostgreSQL vit dans
`tests/api/test_journal_membres_145_rest.py`.
"""
from __future__ import annotations

import ast
import pathlib

import oto_mcp

RACINE = pathlib.Path(oto_mcp.__file__).resolve().parent
GESTES = {"add_org_member", "remove_org_member"}
# Les modules par lesquels le code atteint le store d'appartenance. `tools/github.py`
# appelle un `remove_org_member` HOMONYME sur le client GitHub : il n'est pas visé.
PORTEURS = {"org_store", "members"}


def _appels_du_store() -> list[tuple[str, int, ast.Call]]:
    out = []
    for f in RACINE.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            if (isinstance(fn, ast.Attribute) and fn.attr in GESTES
                    and isinstance(fn.value, ast.Name) and fn.value.id in PORTEURS):
                out.append((str(f.relative_to(RACINE)), n.lineno, n))
    return out


def test_le_banc_voit_les_appels():
    """Le garde ne vaut que s'il voit quelque chose : les sept chemins connus au
    25/09/2026 (création d'org ×2, ajout, changement de rôle, retrait, départ,
    espace personnel, invitation)."""
    assert len(_appels_du_store()) >= 7


def test_chaque_appel_de_production_nomme_son_acteur():
    muets = [f"{f}:{ligne}" for f, ligne, n in _appels_du_store()
             if not any(k.arg == "actor" for k in n.keywords)]
    assert not muets, (
        "appel(s) au store d'appartenance sans `actor=` — le journal écrirait « le "
        f"système » à la place de qui agit : {muets}. Passer `actor=ctx.sub` (ou "
        "`actor=None` si c'est vraiment le système).")
