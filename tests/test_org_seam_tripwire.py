"""Garde-fou du seam d'org (ADR 0023).

`org_store.get_active_org(sub)` rend l'org **MAISON** (défaut persistant) — PAS le
contexte courant. Tout chemin de lecture/écriture scopé org doit passer par le seam
`access.current_org(sub)` (= sous-domaine ?? session MCP ?? consultation X-Oto-Org
?? maison) : lire la maison en direct **ignore le switch d'org** du dashboard (et
l'`oto_use_org` MCP) → mélange d'orgs. Vécu 2026-07-02 : le catalogue authentifié +
8 sites toggles/presets REST scopaient la maison — en consultant l'org B au
dashboard on voyait le catalogue activé de l'org A (fixé 25e9f22).

Ce test **fige les call-sites légitimes** de `get_active_org`. Un nouvel usage
casse le test → revue consciente : soit c'est un des cas légitimes (repli du seam,
exposition « ton défaut », cible TIERCE d'un écran admin, backfill de migration,
fallback télémétrie) → ajout justifié à ALLOWED ; soit c'est un chemin de
résolution → bascule sur `access.current_org`.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"

# fichier_relatif -> raison pour laquelle lire la MAISON en direct y est correct.
ALLOWED = {
    "access.py": "le seam current_org lui-même : la maison est son REPLI terminal.",
    "org_store.py": "le store qui la définit (set-path, invariants internes).",
    "api_routes.py": "exposition `home_org` de /api/me (« ton défaut », distinct "
                     "d'active_org — affichage, pas résolution).",
    "api_routes_connectors.py": "filet webhook unipile : pending émis pré-B4 sans "
                                "org_id → maison du sub (binding legacy, documenté).",
    "credentials_store.py": "backfill_member_scope (migration ADR 0033) : la "
                            "destination du re-chiffrement est la maison, by design.",
    "db/unipile.py": "backfill_unipile_member_scope (migration ADR 0033 B4), idem.",
    "capabilities/users_admin.py": "fiche admin d'un TIERS : son état se calcule "
                                   "contre SA maison persistée, jamais le contexte "
                                   "du requérant (feedback actor-scoped seam).",
    "capabilities/usage.py": "fallback de télémétrie (tag d'un signal si ctx.org_id "
                             "absent) — métadonnée best-effort, pas un accès.",
}


def _callsites() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for p in ROOT.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else None)
            if name == "get_active_org":
                found.setdefault(str(p.relative_to(ROOT)), []).append(node.lineno)
    return found


def test_get_active_org_only_in_allowed_sites():
    files = set(_callsites())
    unexpected = files - set(ALLOWED)
    assert not unexpected, (
        f"`get_active_org` (org MAISON) hors allowlist : {sorted(unexpected)}.\n"
        "Un chemin scopé org doit passer par `access.current_org(sub)` (seam ADR "
        "0023 : session/consultation/maison) — lire la maison en direct ignore le "
        "switch d'org du dashboard (X-Oto-Org) et d'`oto_use_org`. Si ce call-site "
        "est un cas légitime (repli/affichage/tiers/migration), ajoute-le à ALLOWED "
        "avec sa raison."
    )
