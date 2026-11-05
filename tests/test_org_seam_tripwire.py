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
    # `access.py` jusqu'au 2026-08-27 : le seam a suivi la découpe d'`access` en
    # package. Même call-site, même raison, autre fichier — et le fichier est
    # PLUS étroit qu'avant (le seam et rien d'autre), donc la garde se resserre.
    "access/scope.py": "le seam current_org lui-même : la maison est son REPLI terminal.",
    # `org_store.py` jusqu'au 2026-08-27 : le store d'org a été découpé en package.
    # L'entrée est REMPLACÉE par le seul module qui appelle vraiment la maison —
    # la découpe rétrécit donc l'allowlist au lieu de la reconduire en bloc
    # (`members.py` la DÉFINIT sans jamais l'appeler ; les 6 autres l'ignorent).
    "org_store/personal.py": "le store qui la POSE : `ensure_home_org` fait "
                             "de l'org perso la maison quand le user n'en a aucune.",
    # `api/routes.py` → `api_routes_account.py` (découpe du 2026-08-27) →
    # `capabilities/me_account.py` (migration en capacité, le même jour). Même
    # call-site, même raison, troisième fichier : c'est un AFFICHAGE, pas une résolution.
    "capabilities/me_account.py": "exposition `home_org` de /api/me (« ton défaut », "
                                  "distinct d'active_org — affichage, pas résolution).",
    "credentials_store.py": "backfill_member_scope (migration ADR 0033) : la "
                            "destination du re-chiffrement est la maison, by design.",
    "db/unipile.py": "backfill_unipile_member_scope (migration ADR 0033 B4), idem.",
    "capabilities/users_admin.py": "fiche admin d'un TIERS : son état se calcule "
                                   "contre SA maison persistée, jamais le contexte "
                                   "du requérant (feedback actor-scoped seam).",
    "capabilities/usage.py": "fallback de télémétrie (tag d'un signal si ctx.org_id "
                             "absent) — métadonnée best-effort, pas un accès.",
    "capabilities/connectors/selection.py": "signal #577 : la carte connecteur NOMME "
                                            "l'écart entre l'org pour laquelle la "
                                            "session a été montée (la maison, lue au "
                                            "handshake où aucun jeton `_org=` n'existe "
                                            "encore) et celle que l'appel épingle. Les "
                                            "DEUX valeurs sont l'information — passer "
                                            "par `current_org` rendrait deux fois la "
                                            "même et effacerait précisément le fait à "
                                            "signaler. Affichage, jamais une résolution.",
    "capabilities/projects.py": "listing des projets (#5.1) : un partage PERSONNEL "
                                "(grant ('user',sub), org-agnostique) ne s'AFFICHE que "
                                "dans l'org de RATTACHEMENT (la maison) — comparaison "
                                "current(ctx.org_id) vs maison pour dé-dupliquer "
                                "l'affichage, JAMAIS une résolution d'accès (l'accès "
                                "reste cross-org via can_access/recherche).",
    # oto#161 (10/09/2026) — les deux entrées suivantes ÉCRIVENT la maison ou la
    # RENDENT ; aucune ne résout un accès. `current_org` y serait un contresens : il
    # rend le contexte de l'appel (jeton `_org=`, `X-Oto-Org`), or ce qui se décide et
    # ce qui s'annonce ici est le DÉFAUT PERSISTANT, pas le contexte du moment.
    "org_store/invitations.py": "accepter une invitation d'ÉQUIPE ne pose le groupe "
                                "actif que si la maison est déjà l'org du groupe — "
                                "`set_active_group` écrit lui aussi `is_active` "
                                "(invariant ADR 0012) et déplacerait la maison par la "
                                "bande. Lecture du défaut persistant pour décider d'une "
                                "ÉCRITURE sur ce même défaut, jamais une résolution.",
    "capabilities/orgs/invites.py": "l'écho de `org.invite.accept` rend `active_org` "
                                    "LU après l'écriture : depuis oto#161 accepter ne "
                                    "déplace plus une maison établie, et recopier "
                                    "`org_id` serait un accusé de réception faux. "
                                    "Affichage de « ton défaut », comme `me_account`.",
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
