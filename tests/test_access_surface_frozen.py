"""La surface plate `access.<nom>` ne perd rien à la découpe en package (#427).

`oto_mcp/access.py` (2 000 lignes, quatre sujets, soixante-sept commits en soixante
jours) est devenu le package `oto_mcp/access/`. Le découpage est un **DÉPLACEMENT
PUR** : aucun appelant ne doit voir la différence, et ce fichier est le cliquet qui
le tient.

Trois choses y sont figées, et chacune correspond à une façon de casser un appelant
sans s'en apercevoir :

1. **les noms** — `access.<nom>` reste atteignable, privés compris (`_UNSET`,
   `_resolve_credential_impl`, `_platform_grant_meta`… sont consommés dehors) ;
2. **la traversée des écritures** — la suite patche `access.<nom>` à ~200 endroits.
   Depuis la découpe, un sous-module appelle son voisin par le MODULE
   (`scope.current_org(...)`) : sans la propagation de la façade, ces patchs
   laisseraient l'intérieur du package sur l'original, et les tests resteraient
   VERTS en exerçant un chemin qui n'est plus celui qu'ils croient ;
3. **l'acyclicité** — le package n'a d'intérêt que tant qu'on peut le lire par
   couches. Un import qui referme une boucle ramène le monolithe, en pire.

Comme `test_db_surface_frozen.py`, l'inventaire n'interdit pas d'AJOUTER (la surface
grandit à chaque lot) — il interdit de RETIRER. Retirer volontairement un nom reste
possible : on retire aussi sa ligne ici, et le diff dit alors ce qu'on a fait.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from oto_mcp import access

# Relevé le 2026-08-27 sur `oto_mcp/access.py` JUSTE AVANT la découpe (93 noms,
# dunder exclus) — `dir()` du monolithe, tel quel.
_SURFACE = """
    ADMIN BYO_MODES Callable CascadeProbe CascadeRung ErrorData FETCH_PROBE
    INVALID_PARAMS MEMBER McpError ORG_SHAREABLE_PROVIDERS Optional
    PRESENCE_PROBE ROLES ResolvedCredential SLOT_PREFIX SUPER_ADMIN
    _ACCOUNT_URL _PAID_OPTION_BY_CONNECTOR _QUOTA_DEFAULTS _UNSET
    _emit_connector_failure _instance_side_shares_safe _is_multi_account
    _legacy_platform_grant_meta _note_resolved_instance _org_unmetered
    _platform_grant_meta _platform_grantee_scope _platform_instance_usable
    _platform_quota _reachable_hint _resolve_credential_anon
    _resolve_credential_impl _resolve_pinned_instance _resolve_platform_grant
    _shared_auto_account _sub_matches_scopes annotations cascade_winner
    connector_link connector_resolvable_for_org credential_mode_for
    credentials_store current_group current_org current_project
    current_user_sub_from_token current_user_sub_or_raise dataclass db
    get_user_role grants_chain group_admin_hidden_tools
    group_rbac_denied_connectors group_store guard_instance_access has_option
    instance_refs is_platform_operator is_super_admin logger logging
    option_open org_admin_hidden_tools org_store os paid_option_for
    personal_instance_org preloaded_presence_probe project_declared_identities
    project_pinned_identity project_pinned_instance quota_for
    rbac_denied_connectors reachable_instances reachable_instances_map
    reachable_team_key record_platform_usage require_connector_access
    resolve_api_key resolve_credential resolve_credential_fields
    resolve_field_filter resolve_mount_token resolve_namespace_ref
    resolve_slot_tableau session_org status_for status_hints
    walk_cascade
""".split()

# `unipile_api_key_for` a quitté cette liste le 05/09/2026 : la route de connexion
# Unipile résout désormais clé, mode et point d'accès par un SEUL appel à
# `resolve_credential` (oto-backend#894 pour le motif). La fonction n'avait plus
# aucun appelant — un nom retiré parce qu'il ne sert plus, avec sa raison, n'est
# pas la même chose qu'une surface qu'on rabote pour faire passer un lot.

PKG = pathlib.Path(access.__file__).parent


def test_aucun_nom_ne_quitte_la_surface_plate():
    presents = {n for n in dir(access) if not n.startswith("__")}
    manquants = sorted(set(_SURFACE) - presents)
    assert not manquants, (
        f"{len(manquants)} nom(s) ont quitté `access.<nom>` : {', '.join(manquants)} "
        "— un déplacement doit rester invisible aux appelants. Ré-exporte-les depuis "
        "`access/__init__.py`, ou retire aussi leur ligne de cet inventaire.")


def test_l_inventaire_n_est_pas_vide():
    """Un inventaire vidé par accident rendrait le test vert et inutile.

    92 → 91 le 05/09/2026 : `unipile_api_key_for` retiré, un seul nom, parce
    qu'il n'a plus d'appelant (cf. l'entête). Ce compte n'est pas décoratif —
    c'est lui qui oblige à écrire POURQUOI la surface bouge. Une baisse qu'on
    ne peut pas justifier nom par nom est un rabotage, pas un nettoyage.
    """
    assert len(_SURFACE) == 91


def test_une_ecriture_sur_la_facade_traverse_les_sous_modules(monkeypatch):
    """`monkeypatch.setattr(access, …)` doit atteindre le VOISIN qui appelle.

    Ici : `require_connector_access` vit dans `access/rbac.py` et lit l'org par
    `scope.current_org`. Patcher la façade doit donc changer l'org QU'IL VOIT —
    sinon tous les tests qui posent une org de contexte de cette façon (et il y en
    a une centaine) exerceraient l'org réelle sans le dire.
    """
    vues = []
    monkeypatch.setattr(access, "current_org", lambda sub: 4242)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "rbac_denied_connectors",
                        lambda sub, org: (vues.append(org), set())[1])
    monkeypatch.setattr(access, "group_rbac_denied_connectors",
                        lambda sub, group: set())

    access.require_connector_access("serper", sub="u1")

    assert vues == [4242]


def test_l_ecriture_se_defait_aussi():
    """Le retour en arrière traverse comme l'aller — sinon un test en fuiterait
    la valeur sur le suivant, et l'ordre d'exécution deviendrait signifiant."""
    origine = access.scope.current_org
    mp = pytest.MonkeyPatch()
    mp.setattr(access, "current_org", lambda sub: 1)
    assert access.scope.current_org is not origine
    mp.undo()
    assert access.scope.current_org is origine
    assert access.current_org is origine


def _intra_package_imports() -> dict:
    """`module -> frères importés`, lu à l'AST (`from . import x`, niveau 1)."""
    modules = {p.stem for p in PKG.glob("*.py")} - {"__init__"}
    graphe = {}
    for path in sorted(PKG.glob("*.py")):
        if path.stem == "__init__":
            continue
        arbre = ast.parse(path.read_text(encoding="utf-8"), str(path))
        freres = set()
        for node in ast.walk(arbre):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                freres |= {a.name for a in node.names if a.name in modules}
        graphe[path.stem] = freres
    return graphe


def test_le_graphe_des_sous_modules_est_acyclique():
    graphe = _intra_package_imports()
    etat: dict = {}
    boucle: list = []

    def visite(n, pile):
        if etat.get(n) == "fait":
            return
        if n in pile:
            boucle.append(" -> ".join(pile[pile.index(n):] + [n]))
            return
        for suivant in sorted(graphe.get(n, ())):
            visite(suivant, pile + [n])
        etat[n] = "fait"

    for n in sorted(graphe):
        visite(n, [])
    assert not boucle, (
        "cycle d'imports dans le package `access` : " + " ; ".join(boucle) +
        " — le package se lit par couches (scope < quotas/cascade < rbac < "
        "resolve/status < views). Un besoin qui referme la boucle se règle en "
        "descendant le symbole partagé d'un étage, pas en important vers le haut.")


# Locales/paramètres qui portent LÉGITIMEMENT le nom d'un sous-module. `scope` est
# un mot du domaine (un scope de partage : `user:…`/`org:…`/`group:…`) autant qu'un
# module — les trois entrées ci-dessous sont antérieures au package.
_OMBRES_ADMISES = {
    ("cascade.py", "_shared_auto_account", "scope"),
    ("cascade.py", "_platform_quota", "scope"),
    ("resolve.py", "_pick_account", "scope"),
}


def test_aucune_locale_ne_masque_un_sous_module_sans_le_dire():
    """Une locale qui porte le nom d'un frère masque le MODULE dans cette fonction.

    Vécu à la découpe : `status_for` tenait la carte des compteurs du jour dans une
    locale `quotas`, et l'appel `quotas.quota_for(...)` qu'on venait d'y écrire est
    parti chercher la méthode sur un dict. Le piège est silencieux à la lecture (les
    deux `quotas` se ressemblent) et ne saute qu'à l'exécution de ce chemin-là.
    """
    import symtable
    modules = {p.stem for p in PKG.glob("*.py")} - {"__init__"}
    ombres = set()
    for path in sorted(PKG.glob("*.py")):
        if path.stem == "__init__":
            continue
        table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")

        def walk(t):
            for sym in t.get_symbols():
                if (sym.get_name() in modules and t.get_type() != "module"
                        and (sym.is_parameter() or sym.is_assigned())):
                    ombres.add((path.name, t.get_name(), sym.get_name()))
            for child in t.get_children():
                walk(child)

        walk(table)
    assert ombres - _OMBRES_ADMISES == set(), (
        f"locale(s) masquant un sous-module : {sorted(ombres - _OMBRES_ADMISES)} — "
        "renomme la locale, ou ajoute-la à `_OMBRES_ADMISES` en sachant que ce module "
        "n'est plus atteignable dans cette fonction.")


def test_aucun_sous_module_ne_redevient_un_monolithe():
    """La découpe n'a de valeur que tant qu'elle tient : le fichier est l'unité
    d'occupation d'une session sur un tree partagé, et c'est sa taille qui avait
    fait d'`access.py` le goulot de tous les chantiers de connecteurs."""
    trop_gros = {p.name: len(p.read_text(encoding="utf-8").splitlines())
                 for p in sorted(PKG.glob("*.py"))
                 if len(p.read_text(encoding="utf-8").splitlines()) > 500}
    assert not trop_gros, (
        f"sous-module(s) au-delà de 500 lignes : {trop_gros} — découpe selon la "
        "couture du sujet, ne rallonge pas.")
