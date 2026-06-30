"""Garde-fou de scoping cross-org (ADR 0023/0030).

`accessor_scope(sub).owner_pairs()` rend l'**union de TOUTES les orgs** de l'acteur.
Légitime pour le plan GOUVERNANCE / découverte cross-org (ex. bibliothèque de
modèles). **Interdit pour une LISTE DE CONTENU** : celle-ci doit scoper sur
`ownership.active_owner(org)` (l'org active) — sinon charger une org expose le
contenu des AUTRES (fuite *fail-open* : le superset montre plus que le contexte).

Ce test **fige les call-sites légitimes** de `.owner_pairs()`. Un nouvel usage casse
le test → revue consciente (ajout justifié à `ALLOWED`, ou bascule sur `active_owner`).
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"

# Call-sites `.owner_pairs()` ASSUMÉS cross-org. fichier_relatif -> raison.
ALLOWED = {
    "capabilities/projects.py": "op=list_templates : bibliothèque de MODÈLES copiables "
                                "(découverte cross-org voulue, ≠ liste de contenu d'org).",
}


def _owner_pairs_callsites() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for p in ROOT.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "owner_pairs"):
                found.setdefault(str(p.relative_to(ROOT)), []).append(node.lineno)
    return found


def test_owner_pairs_only_in_allowed_sites():
    files = set(_owner_pairs_callsites())
    unexpected = files - set(ALLOWED)
    assert not unexpected, (
        f"`owner_pairs()` (union TOUTES orgs) hors allowlist : {sorted(unexpected)}.\n"
        "Une LISTE DE CONTENU possédé doit scoper sur `ownership.active_owner(org)` "
        "(org active) — sinon fuite cross-org. Si ce call-site est un cas gouvernance/"
        "découverte cross-org légitime, ajoute-le à ALLOWED avec sa raison."
    )
