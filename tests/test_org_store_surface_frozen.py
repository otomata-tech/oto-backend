"""Cliquet de la surface `org_store` — la découpe en package n'a rien changé dehors.

Le palier org était un fichier de 1 773 lignes ; il est devenu le package
`oto_mcp/org_store/` (8 modules par couture + une façade). C'était un
**déplacement pur** : ce test fige ce qui ne doit pas bouger, pour qu'un futur lot
qui touche au package le prouve au lieu de l'affirmer.

Quatre choses figées :

1. **La surface** — les 118 noms que `org_store` exposait avant la découpe sont
   TOUS encore là, avec la même nature et la même signature. Comme
   `test_db_surface_frozen.py`, ce cliquet **n'interdit pas d'ajouter** : la
   surface grandit à chaque lot, et l'y contraindre ferait un test qu'on met à
   jour sans le lire. Il interdit de RETIRER et de DÉFORMER. En revanche il exige
   que **tout module du package soit ré-exporté** — un module neuf oublié dans la
   boucle de `__init__` ne retire aucun nom, donc rien ne rougirait, et ses
   fonctions seraient introuvables en `org_store.<fn>` jusqu'au premier appel en
   prod.
2. **Le report d'écriture** — `monkeypatch.setattr(org_store, "X", stub)` doit
   atteindre le module qui appelle `X`. Sans ce report, une dizaine de tests
   poseraient un stub MORT et passeraient au vert en tapant la vraie fonction :
   c'est le faux vert que la découpe ne doit pas introduire.
3. **L'absence de cycle** — le graphe interne reste un DAG à deux étages, et
   **aucun module n'importe `group_store`** au niveau module (il dépend
   d'org_store : l'importer ici ferait le cycle que le SQL direct de `members.py`
   existe précisément pour éviter — cf. CLAUDE.md §Groupes).
4. **La taille** — aucun module du package ne repasse au-dessus de 500 lignes,
   sinon la découpe se rebouche toute seule.

Retirer volontairement un nom reste possible : on retire aussi sa ligne de
`FROZEN`, et le diff dit alors ce qu'on a fait.

⚠️ **Mise à jour du 31/08/2026 (oto-backend#681)** — la première depuis la découpe,
et elle était PRÉVUE : les procédures étaient servies par DEUX jeux de fonctions
concurrents sur la même table (`org_store.instructions` filtrait `owner_type='org'`
en dur, `group_store` filtrait `owner_type='group'` en dur), déjà divergents. Les
faire converger sur la clé que la table porte — `(owner_type, owner_id)` — DÉFORME
huit signatures et retire trois noms devenus faux :

  · `reparent_instruction(id, new_org_id)` → `move_instruction(id, type, id)` — le
    déplacement n'est plus « vers une org » ;
  · `copy_instruction_to_org` → `copy_instruction_to_owner` — idem ;
  · `list_instructions_for_orgs(org_ids)` → `list_instructions_for_owners(pairs)` —
    prenait une liste d'orgs, donc ne pouvait pas rendre une procédure d'équipe.

Ce cliquet a donc fait exactement son travail : il a rendu le changement VISIBLE et
obligé à l'expliquer ici. Ce qui ne serait PAS acceptable, c'est de le mettre à jour
sans cette explication — la ligne retirée deviendrait indistinguable d'un oubli.

⚠️ **Mise à jour du 01/09/2026 (oto-backend#662)** — `set_instruction` gagne deux
paramètres nommés, `must_create` et `expected_version`, tous deux à valeur par défaut :
la surface s'ÉLARGIT, aucun appelant existant ne bouge. Ils portent les deux refus
anti-écrasement (`InstructionExists`, `InstructionVersionConflict`) qu'un front tiers
a payés en découvrant qu'une création sur un slug pris remplaçait la procédure en
place sans un mot. Ils vivent dans le store, sous le verrou advisory, parce que
n'importe quel pré-check posé dehors laisserait passer deux créations simultanées.

⚠️ **Mise à jour du 03/09/2026 (bascule de tenant)** — `upsert_user` PERD son
paramètre `iss`. Il n'a jamais servi qu'à une chose : décider si le login devait
déclencher le rapprochement d'identités (fusionner l'ancien compte de même email dans
le nouveau). Ce déclenchement automatique est retiré — zéro rapprochement sur les 20
jours précédents, une commande réglée sur NOTRE émetteur donc armée en permanence sur
tous nos comptes, et une résurrection de compte supprimé à son actif (884 appels sous
une identité morte). L'émetteur n'étant plus lu, le garder en signature ferait calculer
aux deux portes une valeur que personne ne regarde.

Deux appelants seulement le passaient, tous deux nommément (`api/base._authenticate`,
`auth/hooks.current_user_sub_from_token`) ; les seize autres sites ne le passaient pas.
La fusion reste joignable en acte d'OPÉRATEUR (`migrate_sub`, ADR 0052 §6) — c'est le
déclenchement sans décideur qui disparaît, pas le mécanisme. Le DRAIN d'alias, lui,
reste armé : c'est lui qui empêche la résurrection, et les couper ensemble aurait fait
renaître les comptes fusionnés.

⚠️ **Mise à jour du 01/09/2026 (issue `oto`#27)** — une couture de PLUS dans le
package : `instructions.py` touchait 499 lignes pour un plafond de 500 (le lot
précédent avait raboté sa prose pour y tenir), donc plus rien ne pouvait s'y
ajouter. Le fichier est coupé en deux sur la frontière qu'il portait déjà en
bannière de section, et qui est celle d'ADR 0030 : `instructions` sert le plan
CONTENU (lire / écrire / versionner / archiver, à la clé
`(owner_type, owner_id, slug)`), `instruction_ownership` sert le plan GOUVERNANCE
(identité par `id` surrogate, copie, déplacement, inventaires). Ce sont deux
DROITS distincts depuis ADR 0030 (`can_access` vs `can_govern`) ; ce sont
maintenant deux fichiers.

**Déplacement pur, et c'est vérifiable** : `FROZEN` ne perd pas une ligne, aucune
signature ne bouge, et les six noms déplacés (`get_instruction_by_id`,
`_free_instruction_slug`, `copy_instruction_to_owner`, `move_instruction`,
`list_instructions_for_owners`, `list_all_instructions`) restent servis en
`org_store.<fn>` par le ré-export — c'est exactement ce que
`test_aucun_nom_perdu` prouve ici. Le seul changement de forme est interne : les
références croisées passent désormais par `instructions.<nom>`, la seule forme
que `test_aucun_frere_importe_a_plat` admet.

⚠️ **Mise à jour du 14/09/2026 (oto#191)** — `list_pending_invitations_for_email` est
RETIRÉ : son seul appelant était l'inbox d'accueil, retirée avec la boucle de
propositions de docs. Il portait le seul usage de `orgs` dans `invitations` : l'import
part avec lui, et l'arête `invitations → orgs` quitte `EXPECTED_EDGES`.

⚠️ **Mise à jour du 15/09/2026 (oto-backend#560)** — le CODE COURT d'invitation (7
caractères, ~34 bits, brute-forçable) est ENTIÈREMENT retiré au profit du seul token
long (256 bits). Cinq noms quittent la surface : `_CODE_ALPHABET` et `_gen_code`
(génération du code), `accept_invitation_by_code`, `get_invitation_by_code` et
`preview_invitation_by_code` (lecture/acceptation par code). Retrait DÉLIBÉRÉ, pas
un oubli : la ligne quitte `FROZEN` dans le même commit que les
call-sites (`org_store/invitations.py`, `api/public.py`, `api/routes.py`,
`capabilities/orgs/invites.py`). `create_invitation` perd un élément de tuple
(`(id, token, code)` → `(id, token)`) et `peek_invitation` perd son paramètre `code`
— les deux sont donc aussi mis à jour dans `FROZEN_SIGNATURES`.

⚠️ **Mise à jour du 25/09/2026 (otomata-tech/oto#145)** — le journal des entrées et
sorties de membres. `add_org_member` et `remove_org_member` gagnent un paramètre NOMMÉ
`actor` (sub de qui agit, None = le système), à valeur par défaut : la surface
s'ÉLARGIT, aucun appelant existant ne casse — mais tout appel de PRODUCTION le passe
explicitement (garde `tests/test_journal_membres_145.py`). `_accept_invitation_row`
le reçoit sans défaut : ses deux seuls appelants distinguent le clic (la personne) de
la réconciliation au signup (le système).
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import oto_mcp.org_store as org_store

PKG = pathlib.Path(org_store.__file__).parent

# Les 9 coutures, dans l'ordre du graphe (feuilles d'abord).
MODULES = ("orgs", "members", "vault", "settings", "instructions",
           "instructions_cycle", "instruction_ownership", "personal",
           "invitations", "library")

# Arêtes ATTENDUES du graphe interne : module -> modules frères importés.
EXPECTED_EDGES = {
    "orgs": set(), "members": set(), "vault": set(), "settings": set(),
    "instructions": set(),
    # Le cycle de vie lit les outils du module qu'il prolonge — couture du
    # 10/09/2026, quand le désarchivage a fait déborder `instructions.py`.
    "instructions_cycle": {"instructions"},
    "instruction_ownership": {"instructions"},
    "personal": {"orgs", "members"},
    "invitations": {"members"},
    "library": {"instructions"},
}

# ── la surface d'avant la découpe, relevée sur `main` (commit 3a40ee0) ───────
FROZEN = (
    'BASE_SLUG', 'LibrarySlugTaken', 'ORG_ROLES', 'Optional',
    '_DOMAIN_RE', '_EMAIL_CONNECTORS_ORDER', '_INV_LIST_SELECT', '_LIBRARY_COLS',
    '_LIBRARY_META_COLS', '_PREVIEW_SELECT', '_PRIMARY_TENANT', '_SLUG_RE',
    '_accept_invitation_row', '_connect', '_email_connectors_in_order',
    '_free_instruction_slug', '_get_invitation', '_hash_token',
    '_idempotent_accept', '_list_invitations', '_log', '_mark_invitation_accepted',
    '_personal_label', '_preview_from_row', '_reclaim_or_create_personal',
    '_scope_of', '_snippet', '_sync_mfa_mirror', 'accept_invitation',
    'add_org_member', 'annotations', 'archive_org',
    'backfill_org_front', 'backfill_personal_orgs', 'cancel_scheduled_email',
    'claim_kb_project', 'clear_kb_project', 'config',
    'count_orgs_created_by', 'create_invitation',
    'create_org', 'credentials_store', 'db', 'delete_instruction',
    'delete_org_secret', 'effective_logo_url', 'ensure_personal_org',
    'fork_into_org', 'get_active_org', 'get_instruction', 'get_instruction_by_id',
    'get_invitation_by_token', 'get_kb_project_id',
    'get_library_entry', 'get_org', 'get_org_default_connectors',
    'get_org_email_settings', 'get_org_field_filters', 'get_org_mfa',
    'get_org_role', 'get_org_secret', 'get_personal_org', 'has_org_secret',
    'is_personal_org', 'json', 'list_all_instructions', 'list_all_orgs',
    'list_group_invitations', 'list_instruction_bodies',
    'list_instruction_versions', 'list_instructions',
    'list_invitations', 'list_library', 'list_org_members', 'list_org_secrets',
    'list_orgs_for_user',
    'list_platform_invitations', 'list_scheduled_emails', 'logging', 'logodev',
    'normalize_domain', 'normalize_slug', 'org_email_quiet_hours', 'org_front',
    'preview_invitation', 'publish_guide', 're',
    'reconcile_signup_with_invitation', 'remove_org_member',
    'resolve_org_for_user', 'resolve_sender', 'revoke_group_invitation',
    'revoke_invitation', 'revoke_platform_invitation', 'search_instructions',
    'secrets', 'set_active_org', 'set_instruction', 'set_org_default_connectors',
    'set_org_email_settings', 'set_org_field_filters', 'set_org_logo',
    'set_org_logto_org_id', 'set_org_require_mfa', 'set_org_secret',
    'unpublish_guide', 'update_org', 'upsert_user'
)

# nom -> str(inspect.signature(...)) relevé sur le même commit.
FROZEN_SIGNATURES = {
    'LibrarySlugTaken': '?',
    '_accept_invitation_row': "(inv: 'dict', sub: 'str', *, actor: 'Optional[str]') -> 'dict'",
    '_connect': "() -> 'Iterator[psycopg.Connection]'",
    '_email_connectors_in_order': "(settings: 'dict') -> 'list[str]'",
    # #681 : la clé est la paire propriétaire, plus l'org — cf. l'en-tête.
    '_free_instruction_slug': "(conn, owner_type: 'str', owner_id: 'int | str', slug: 'str') -> 'str'",
    '_get_invitation': "(pred: 'str', val) -> 'Optional[dict]'",
    '_hash_token': "(token: 'str') -> 'str'",
    '_idempotent_accept': "(pred: 'str', val, sub: 'str') -> 'Optional[dict]'",
    '_list_invitations': "(pred: 'str', *args) -> 'list[dict]'",
    '_mark_invitation_accepted': "(inv_id: 'int', sub: 'str') -> 'None'",
    '_personal_label': "(email: 'Optional[str]', name: 'Optional[str]') -> 'str'",
    '_preview_from_row': "(r: 'dict') -> 'dict'",
    '_reclaim_or_create_personal': "(sub: 'str', email: 'Optional[str]', name: 'Optional[str]') -> 'int'",
    '_scope_of': "(r: 'dict') -> 'str'",
    '_snippet': "(body: 'str', query: 'str', width: 'int' = 200) -> 'str'",
    '_sync_mfa_mirror': "(org_id: 'int') -> 'None'",
    'accept_invitation': "(token: 'str', sub: 'str') -> 'Optional[dict]'",
    'add_org_member': "(org_id: 'int', sub: 'str', org_role: 'str' = 'org_member', *, actor: 'Optional[str]' = None) -> 'None'",
    'archive_org': "(org_id: 'int') -> 'bool'",
    'backfill_org_front': "() -> 'dict'",
    'backfill_personal_orgs': "() -> 'dict'",
    'cancel_scheduled_email': "(org_id: 'int', email_id: 'int') -> 'bool'",
    'claim_kb_project': "(org_id: 'int', project_id: 'int') -> 'bool'",
    'clear_kb_project': "(org_id: 'int', expected_project_id: 'int') -> 'None'",
    'count_orgs_created_by': "(sub: 'str') -> 'int'",
    'create_invitation': "(org_id: 'Optional[int]', email: 'Optional[str]', org_role: 'str', invited_by: 'str', ttl_days: 'int' = 7, source: 'Optional[str]' = None, group_id: 'Optional[int]' = None, group_role: 'Optional[str]' = None) -> 'tuple[int, str]'",
    'create_org': "(name: 'str', created_by: 'Optional[str]' = None, front_base_url: 'Optional[str]' = None, front_brand: 'Optional[str]' = None, front_of: 'Optional[str]' = None) -> 'int'",
    'delete_instruction': "(owner_type: 'str', owner_id: 'int | str', slug: 'str') -> 'bool'",
    'delete_org_secret': "(org_id: 'int', provider: 'str', account: 'str' = '') -> 'bool'",
    'effective_logo_url': "(org: 'dict') -> 'Optional[str]'",
    'ensure_personal_org': "(sub: 'str', email: 'Optional[str]' = None, name: 'Optional[str]' = None) -> 'int'",
    'fork_into_org': "(*, entry_id: 'int', org_id: 'int', new_slug: 'Optional[str]' = None, set_by: 'Optional[str]' = None) -> 'dict'",
    'get_active_org': "(sub: 'str') -> 'Optional[int]'",
    'get_instruction': "(owner_type: 'str', owner_id: 'int | str', slug: 'str', version: 'Optional[int]' = None) -> 'Optional[dict]'",
    'get_instruction_by_id': "(instruction_id: 'int') -> 'Optional[dict]'",
    'get_invitation_by_token': "(token: 'str') -> 'Optional[dict]'",
    'get_kb_project_id': "(org_id: 'int') -> 'Optional[int]'",
    'get_library_entry': "(*, entry_id: 'Optional[int]' = None, slug: 'Optional[str]' = None, include_unlisted: 'bool' = False) -> 'Optional[dict]'",
    'get_org': "(org_id: 'int') -> 'Optional[dict]'",
    'get_org_default_connectors': "(org_id: 'int') -> 'Optional[list[str]]'",
    'get_org_email_settings': "(org_id: 'int') -> 'dict'",
    'get_org_field_filters': "(org_id: 'int') -> 'dict'",
    'get_org_mfa': "(org_id: 'int') -> 'dict'",
    'get_org_role': "(org_id: 'int', sub: 'str') -> 'Optional[str]'",
    'get_org_secret': "(org_id: 'int', provider: 'str', account: 'str' = '') -> 'Optional[str]'",
    'get_personal_org': "(sub: 'str') -> 'Optional[int]'",
    'has_org_secret': "(org_id: 'int', provider: 'str') -> 'bool'",
    'is_personal_org': "(org_id: 'int') -> 'bool'",
    'list_all_instructions': "() -> 'list[dict]'",
    'list_all_orgs': "() -> 'list[dict]'",
    'list_group_invitations': "(group_id: 'int') -> 'list[dict]'",
    'list_instruction_bodies': "(owner_type: 'str', owner_id: 'int | str') -> 'list[dict]'",
    'list_instruction_versions': "(owner_type: 'str', owner_id: 'int | str', slug: 'str') -> 'list[dict]'",
    'list_instructions': "(owner_type: 'str', owner_id: 'int | str', include_base: 'bool' = False) -> 'list[dict]'",
    'list_invitations': "(org_id: 'int') -> 'list[dict]'",
    'list_library': "(*, query: 'Optional[str]' = None, category: 'Optional[str]' = None, author_kind: 'Optional[str]' = None, author_org_id: 'Optional[int]' = None, include_unlisted: 'bool' = False, limit: 'int' = 100) -> 'list[dict]'",
    'list_org_members': "(org_id: 'int') -> 'list[dict]'",
    'list_org_secrets': "(org_id: 'int') -> 'list[dict]'",
    'list_orgs_for_user': "(sub: 'str') -> 'list[dict]'",
    'list_platform_invitations': "() -> 'list[dict]'",
    'list_scheduled_emails': "(org_id: 'int', status: 'str' = 'pending') -> 'list[dict]'",
    'normalize_domain': "(raw: 'str') -> 'Optional[str]'",
    'normalize_slug': "(slug: 'str') -> 'str'",
    'org_email_quiet_hours': "(org_id: 'int', connector: 'str') -> 'Optional[dict]'",
    'org_front': "(org_id: 'Optional[int]') -> 'tuple[Optional[str], Optional[str]]'",
    'preview_invitation': "(token: 'str') -> 'Optional[dict]'",
    'publish_guide': "(*, slug: 'str', title: 'str' = '', description: 'str' = '', body_md: 'str', author_kind: 'str', author_org_id: 'Optional[int]' = None, author_display: 'str' = '', category: 'str' = '', tags: 'Optional[list]' = None, visibility: 'str' = 'public', source_org_id: 'Optional[int]' = None, source_slug: 'Optional[str]' = None, forked_from: 'Optional[int]' = None, published_by: 'Optional[str]' = None, slots: 'Optional[list]' = None) -> 'dict'",
    'reconcile_signup_with_invitation': "(sub: 'str', email: 'str') -> 'Optional[dict]'",
    'remove_org_member': "(org_id: 'int', sub: 'str', *, actor: 'Optional[str]' = None) -> 'bool'",
    'resolve_org_for_user': "(sub: 'str', org: 'str') -> 'int'",
    'resolve_sender': "(org_id: 'int', from_email: 'Optional[str]' = None) -> 'Optional[tuple[dict, str]]'",
    'revoke_group_invitation': "(group_id: 'int', inv_id: 'int') -> 'bool'",
    'revoke_invitation': "(org_id: 'int', inv_id: 'int') -> 'bool'",
    'revoke_platform_invitation': "(inv_id: 'int') -> 'bool'",
    'search_instructions': "(owner_type: 'str', owner_id: 'int | str', query: 'str', include_base: 'bool' = False) -> 'list[dict]'",
    'set_active_org': "(sub: 'str', org_id: 'int') -> 'bool'",
    'set_instruction': "(owner_type: 'str', owner_id: 'int | str', slug: 'str', body_md: 'str', title: 'Optional[str]' = None, description: 'Optional[str]' = None, set_by: 'Optional[str]' = None, slots: 'Optional[list]' = None, must_create: 'bool' = False, expected_version: 'Optional[int]' = None) -> 'int'",
    'set_org_default_connectors': "(org_id: 'int', connectors: 'Optional[list[str]]') -> 'bool'",
    'set_org_email_settings': "(org_id: 'int', connector: 'str', *, senders: 'Optional[list[dict]]' = None, quiet_hours: 'Optional[dict]' = None, clear_quiet_hours: 'bool' = False) -> 'bool'",
    'set_org_field_filters': "(org_id: 'int', service: 'str', block: 'Optional[dict]') -> 'bool'",
    'set_org_logo': "(org_id: 'int', url: 'Optional[str]') -> 'None'",
    'set_org_logto_org_id': "(org_id: 'int', logto_org_id: 'Optional[str]') -> 'bool'",
    'set_org_require_mfa': "(org_id: 'int', require: 'bool') -> 'bool'",
    'set_org_secret': "(org_id: 'int', provider: 'str', api_key: 'str', set_by: 'Optional[str]' = None, meta: 'Optional[dict]' = None, account: 'str' = '') -> 'None'",
    'unpublish_guide': "(entry_id: 'int') -> 'bool'",
    'update_org': "(org_id: 'int', name: 'Optional[str]' = None, description: 'Optional[str]' = None, domain: 'Optional[str]' = None, industry: 'Optional[str]' = None, location: 'Optional[str]' = None) -> 'bool'",
    'upsert_user': "(sub: 'str', email: 'Optional[str]' = None, name: 'Optional[str]' = None, locale: 'Optional[str]' = None) -> 'None'",
}


def _surface() -> set[str]:
    return {n for n in dir(org_store) if not n.startswith("__")}


def test_aucun_nom_perdu():
    manquants = sorted(set(FROZEN) - _surface())
    assert not manquants, (
        f"`org_store` n'expose plus : {manquants}. La découpe en package devait "
        "être un déplacement pur — un nom qui disparaît casse des appelants qui "
        "n'ont pas bougé (`from .org_store import …` dans group_store, ~70 modules "
        "en `org_store.<fn>`). Si le retrait est voulu, retire-le de FROZEN dans le "
        "même commit que les call-sites."
    )


def test_tout_module_du_package_est_re_exporte():
    """Un module neuf oublié dans la boucle de `__init__` ne rougit nulle part.

    Il ne RETIRE aucun nom (donc `test_aucun_nom_perdu` reste vert) : il ajoute
    simplement un fichier dont aucune fonction n'est atteignable en
    `org_store.<fn>`. Le trou se découvre au premier appel en prod. D'où ce
    contrôle, qui compare le disque à ce que la façade a effectivement ratissé.
    """
    sur_disque = {p.stem for p in PKG.glob("*.py")} - {"__init__"}
    re_exportes = {m.__name__.rsplit(".", 1)[-1]
                   for mods in org_store._OWNERS.values() for m in mods}
    oublies = sorted(sur_disque - re_exportes)
    assert not oublies, (
        f"modules présents mais jamais ré-exportés : {oublies}. Ajoute-les à la boucle "
        "de `org_store/__init__.py` (et à MODULES/EXPECTED_EDGES ici), sinon leurs "
        "fonctions sont introuvables en `org_store.<fn>`."
    )
    assert sur_disque == set(MODULES), (
        f"le package contient {sorted(sur_disque)} mais ce test connaît {sorted(MODULES)} — "
        "MODULES et EXPECTED_EDGES doivent suivre."
    )


def test_signatures_inchangees():
    ecarts = []
    for name, sig in FROZEN_SIGNATURES.items():
        obj = getattr(org_store, name, None)
        if obj is None:
            continue                      # couvert par test_aucun_nom_perdu
        try:
            now = str(inspect.signature(obj))
        except (ValueError, TypeError):
            now = "?"
        if now != sig:
            ecarts.append(f"{name} : {sig} -> {now}")
    assert not ecarts, (
        "signatures changées par rapport à l'avant-découpe :\n  " + "\n  ".join(ecarts)
    )


def test_ecriture_sur_la_facade_atteint_le_module_appelant(monkeypatch):
    """La propriété qui rend « surface figée » vrai jusque sous les tests.

    Trois cas réels, un par arête du graphe : `personal` appelle `orgs.create_org`,
    `invitations` appelle `members.get_org_role`, `library` appelle
    `instructions.get_instruction`. Un stub posé sur la façade doit être vu des trois.
    """
    sentinelle = object()
    for facade_name, module, appele in (
        ("create_org", org_store.personal, "orgs"),
        ("get_org_role", org_store.invitations, "members"),
        ("get_instruction", org_store.library, "instructions"),
    ):
        monkeypatch.setattr(org_store, facade_name, sentinelle)
        vu = getattr(getattr(module, appele), facade_name)
        assert vu is sentinelle, (
            f"`monkeypatch.setattr(org_store, {facade_name!r}, …)` n'atteint pas "
            f"`{appele}.{facade_name}`, que `{module.__name__}` appelle. Le stub serait "
            "MORT SILENCIEUSEMENT et le test taperait la vraie fonction (donc la vraie "
            "base). Le report d'écriture vit dans `org_store/__init__.py` (_Facade)."
        )
        monkeypatch.undo()


def _sibling_imports(mod: str) -> set[str]:
    """Frères importés en MODULE (`from . import orgs`) — la seule forme admise."""
    tree = ast.parse((PKG / f"{mod}.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            out |= {a.name for a in node.names}
    return out


def test_aucun_frere_importe_a_plat():
    """`from .orgs import create_org` est interdit : il fige le nom à l'import.

    Le report d'écriture de la façade pose le stub sur `orgs.create_org` ; un
    module qui a copié la fonction dans son propre namespace ne le verra jamais.
    Toute référence croisée passe donc par `<frère>.<nom>`, résolu à l'appel.
    """
    fautifs = []
    for mod in MODULES:
        tree = ast.parse((PKG / f"{mod}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.level == 1
                    and node.module in MODULES):
                fautifs.append(f"{mod}.py:{node.lineno} from .{node.module} import "
                               + ", ".join(a.name for a in node.names))
    assert not fautifs, (
        "import à plat d'un frère : " + " | ".join(fautifs) + ". Écris "
        "`from . import <frère>` puis `<frère>.<nom>()` — sinon un "
        "`monkeypatch.setattr(org_store, …)` ne t'atteindra plus (cf. _Facade)."
    )


def test_graphe_interne_sans_cycle():
    for mod in MODULES:
        assert _sibling_imports(mod) == EXPECTED_EDGES[mod], (
            f"`{mod}.py` n'importe plus les mêmes frères : "
            f"{sorted(_sibling_imports(mod))} au lieu de {sorted(EXPECTED_EDGES[mod])}. "
            "Le graphe doit rester un DAG à deux étages (feuilles : orgs, members, "
            "vault, settings, instructions)."
        )
    # vérification indépendante de la table : un tri topologique doit aboutir.
    restants = {m: set(_sibling_imports(m)) for m in MODULES}
    while restants:
        libres = [m for m, deps in restants.items() if not (deps & set(restants))]
        assert libres, f"cycle d'import dans le package : {sorted(restants)}"
        for m in libres:
            del restants[m]


def test_aucun_import_group_store_au_niveau_module():
    """CLAUDE.md §Groupes : `org_store` n'importe PAS `group_store` (cycle).

    L'invariant « l'équipe est subordonnée à l'org » est tenu en SQL direct
    (`members.remove_org_member`, `members.set_active_org`). Les rares besoins
    réels de `group_store` restent des imports PARESSEUX au point d'appel.
    """
    fautifs = []
    for p in sorted(PKG.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:                      # niveau module SEULEMENT
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("group_store" in n for n in names):
                fautifs.append(f"{p.name}:{node.lineno}")
    assert not fautifs, (
        f"import `group_store` au niveau module dans {fautifs} — `group_store` dépend "
        "d'`org_store`, l'importer ici crée le cycle. Import paresseux au point d'appel."
    )


def test_modules_sous_500_lignes():
    trop = {p.name: len(p.read_text(encoding="utf-8").splitlines())
            for p in PKG.glob("*.py")
            if len(p.read_text(encoding="utf-8").splitlines()) >= 500}
    assert not trop, (
        f"modules du package au-dessus de 500 lignes : {trop} — c'est la limite que "
        "la découpe existe pour tenir (CLAUDE.md). Recoupe par couture."
    )
