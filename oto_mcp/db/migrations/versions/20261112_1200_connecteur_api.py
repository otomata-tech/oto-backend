"""Le connecteur Custom API s'appelle `api` (ex-`http`) : ses références enregistrées suivent.

Le 12/11/2026, le connecteur `http` est renommé `api`, et ses outils `http`, `http_act`,
`http_get`, `http_post`, `http_doc` deviennent `api`, `api_act`, `api_get`, `api_post`,
`api_doc` — mêmes ops, mêmes arguments, même effet. Les anciens noms d'outils restent
servis en alias dépréciés (`deprecations.OUTILS_CONNECTEUR_API`) : une liste enregistrée
qui nomme encore `http_get` continue de fonctionner. Cette révision fige sa propre
correspondance (`_OUTILS`) : une révision est une histoire, elle ne suit pas une table
vivante appelée à disparaître au retrait des alias.

**Ce que la révision réécrit**, le nom seul, le reste de chaque ligne tel quel :

- les lignes qui NOMMENT le connecteur : `user_selected_connectors`,
  `connector_selection_removed`, `connector_availability`, `connector_settings`, et le kit
  des orgs (`orgs.default_connectors`, à sa place dans la liste) ;
- les listes d'outils enregistrées, par la mécanique de `20261021_1219_outils_act_msg`
  (`projects.mcp_tools`, `runner_fleets.tools`, `runner_triggers.tools`, la liste
  `payload.tools` des travaux du runner pas encore terminés, `user_enabled_tools`,
  `user_disabled_tools`, `org_disabled_tools`, `group_disabled_tools`) : le même outil des
  deux côtés, qu'elle autorise ou masque. `runner_triggers.outils_plateforme` ne nomme que
  des outils `oto_admin_*` (`verrou_org.PREFIXE_ADMIN`) : rien à y réécrire.

- ce qui DÉSIGNE une instance `http` ou son compte, repointé vers la copie `api` (même
  propriétaire, même compte) : les règles d'accès des comptes
  (`connector_account_rules.instance_id`), les arêtes `grants` (`resource_id`, forme
  `inst:{id}` ou composée), et les liens de projet vers le connecteur
  (`project_links` : `target_ref`, l'instance épinglée `config.instance_ref`, l'identité
  `identity_ref`). Ici et non au démarrage : l'id de l'instance neuve se lit en base
  (la copie du coffre l'a fait naître), et déplacer ces désignations tant que la prod
  sert encore l'ancien code lui retirerait ses épingles. Une instance `http` désignée
  sans copie `api` (copie jamais jouée, ligne indéchiffrable) arrête la révision.

**Ce qu'elle ne touche pas.** Le coffre (`connector_credentials`) et ses instances : l'AAD
du chiffrement contient le nom du connecteur, la copie exige la clé applicative — c'est
une étape du code, `api_operations.copier_depuis_http`, COPIE re-chiffrée qui laisse
les lignes `http` en place (base partagée par la prod et la préprod) et recopie la
définition de chaque instance (opérations, en-têtes, variables). Ni l'histoire (journal des
appels, travaux terminés, journaux de partage), ni le texte libre des tenants (procédures,
guides, recettes, consignes d'agent) : l'ancien nom y répond en alias déprécié, et
chaque réponse dit le nom d'aujourd'hui.

**Conflit.** Une ligne `api` peut déjà exister sous la même clé (la préprod sert le code
neuf avant le tag de prod). Identique en sens (même état, même activation, même valeur) :
la ligne `http` part, la ligne `api` reste. Différente : la révision LÈVE, en nommant la
table et le nombre de clés en conflit — jamais une ligne écrasée.

**Verrous.** `UPDATE`/`DELETE` ciblés sur les lignes qui nomment l'ancien nom ;
`lock_timeout` 5 s.

**Ordre avec le code : APRÈS la copie du coffre.** Le démarrage du code neuf la joue ; une
base qui migre AVANT ce démarrage (la porte d'une instance cible) la reçoit de
`oto-mcp maintenance copies-du-coffre`, que la porte joue avant `migrer upgrade head`
(`deploy/cible/migrations_a_jour.py`). Notre base, partagée par la prod et la préprod : la
jouer au tag de prod (`migrer upgrade head` après le tag).

Retour arrière : exact pour ce que la révision a réécrit (`api` → `http`, mêmes règles de
conflit) ; les copies du coffre sous `api` restent, inertes pour l'ancien code.

Révision : 20261112_1200_connecteur_api
Précédente : 20261111_1930_dataforseo_decrit
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
from pathlib import Path

from alembic import context, op

revision = "20261112_1200_connecteur_api"
down_revision = "20261111_1930_dataforseo_decrit"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")

ANCIEN, NOUVEAU = "http", "api"

#: Les tables qui nomment un connecteur : (table, colonnes de la clé hors `connector`,
#: colonnes qui portent le SENS de la ligne — comparées en cas de conflit).
TABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("user_selected_connectors", ("sub", "org_id"), ("state",)),
    ("connector_selection_removed", ("sub", "org_id"), ()),
    ("connector_availability", ("scope_type", "scope_id"), ("enabled",)),
    ("connector_settings", ("scope_type", "scope_id", "key"), ("value",)),
)


class ConflitDeRenommage(RuntimeError):
    """Une ligne existe sous les deux noms, avec un sens différent."""


#: Ce que dit une désignation sans copie : le geste qui la fait naître.
_GESTE = ("jouer `oto-mcp maintenance copies-du-coffre` (sous le lanceur de l'instance) "
          "puis rejouer la révision ; si la copie compte la ligne `unreadable`, la ligne "
          "du coffre est indéchiffrable : retirer la désignation à la main. La transaction "
          "de la révision est annulée : rien n'est écrit.")


def _revision_des_messageries():
    """La révision des messageries, pour sa mécanique (`reecrire_les_listes`)."""
    chemin = Path(__file__).with_name("20261021_1219_outils_act_msg.py")
    spec = importlib.util.spec_from_file_location("revision_outils_act_msg", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Les outils renommés avec le connecteur, figés au jour du renommage.
_OUTILS = {f"http{suffixe}": f"api{suffixe}"
           for suffixe in ("", "_act", "_get", "_post", "_doc")}


def correspondances(sens: str = "montee") -> tuple[dict, dict]:
    """`(autorise, masque)` des outils renommés — le même outil des deux côtés.
    `sens="descente"` : la table inversée, pour le retour arrière."""
    paires = list(_OUTILS.items())
    if sens == "descente":
        paires = [(n, a) for a, n in paires]
    table = {de: (vers,) for de, vers in paires}
    return table, dict(table)


def renommer_les_lignes(conn, de: str, vers: str) -> None:
    """Chaque ligne qui nomme `de` passe à `vers`. Conflit de clé au sens identique : la
    ligne `de` part ; au sens différent : `ConflitDeRenommage`, rien n'est écrit."""
    for table, cle, sens in TABLES:
        jointure = " AND ".join(f"n.{c} = o.{c}" for c in cle)
        differe = " OR ".join(f"n.{c} IS DISTINCT FROM o.{c}" for c in sens) or "FALSE"
        conflits = conn.exec_driver_sql(
            f"SELECT count(*) FILTER (WHERE {differe}) AS differents, count(*) AS tous "
            f"FROM {table} o JOIN {table} n ON {jointure} AND n.connector = %(vers)s "
            f"WHERE o.connector = %(de)s", {"de": de, "vers": vers}).one()
        if conflits[0]:
            raise ConflitDeRenommage(
                f"{table} : {conflits[0]} clé(s) portent à la fois `{de}` et `{vers}` avec "
                f"un sens différent ({', '.join(sens)}) — à trancher à la main (garder "
                f"l'une des deux lignes), puis rejouer. La transaction de la révision est "
                f"annulée : rien n'est écrit.")
        if conflits[1]:
            conn.exec_driver_sql(
                f"DELETE FROM {table} o USING {table} n WHERE o.connector = %(de)s "
                f"AND n.connector = %(vers)s AND {jointure}", {"de": de, "vers": vers})
        n = conn.exec_driver_sql(
            f"UPDATE {table} SET connector = %(vers)s WHERE connector = %(de)s",
            {"de": de, "vers": vers}).rowcount
        logger.info("%s : %s — %d ligne(s) renommée(s), %d doublon(s) identique(s) retiré(s)",
                    revision, table, n, conflits[1])
    kits = conn.exec_driver_sql(
        "SELECT id, default_connectors FROM orgs WHERE %(de)s = ANY(default_connectors)",
        {"de": de}).all()
    for pk, kit in kits:
        # À sa place ; un kit qui portait déjà les deux noms ne le garde qu'une fois, à sa
        # première occurrence. Les autres noms ne bougent pas.
        nouveau, vu = [], False
        for k in kit:
            if k in (de, vers):
                if vu:
                    continue
                k, vu = vers, True
            nouveau.append(k)
        conn.exec_driver_sql("UPDATE orgs SET default_connectors = %(k)s::text[] "
                             "WHERE id = %(pk)s", {"k": nouveau, "pk": pk})
    n = len(kits)
    logger.info("%s : orgs.default_connectors — %d kit(s) réécrit(s)", revision, n)


def _instances(conn, de: str, vers: str) -> tuple[dict[int, int], set[int]]:
    """`({id de l'instance « de » : id de sa copie « vers »}, {ids « de » sans copie})`,
    instances VIVANTES, appariées par propriétaire et compte."""
    lignes = conn.exec_driver_sql(
        "SELECT o.id AS de, n.id AS vers FROM connector_instances o "
        "LEFT JOIN connector_instances n ON n.owner_type = o.owner_type "
        "AND n.owner_id = o.owner_id AND n.account = o.account "
        "AND n.connector = %(vers)s AND n.revoked_at IS NULL "
        "WHERE o.connector = %(de)s AND o.revoked_at IS NULL",
        {"de": de, "vers": vers}).all()
    return ({a: b for a, b in lignes if b is not None},
            {a for a, b in lignes if b is None})


def _ref(ref, de: str, vers: str, apparies: dict[int, int], orphelines: set[int]):
    """Le ref réécrit (`inst:{id}` apparié, ou composé qui nomme `de`), ou tel quel."""
    from oto_mcp import instance_refs
    if not ref:
        return ref
    try:
        r = instance_refs.parse_ref(ref)
    except ValueError:
        return ref
    if r.level == "inst":
        if r.instance_id in orphelines:
            raise ConflitDeRenommage(
                f"`{ref}` désigne une instance `{de}` sans copie `{vers}` — {_GESTE}")
        return instance_refs.make_instance_ref(apparies.get(r.instance_id, r.instance_id))
    if r.connector != de:
        return ref
    return instance_refs.format_ref(dataclasses.replace(r, connector=vers))


def repointer_les_references(conn, de: str, vers: str) -> None:
    """Ce qui désigne une instance `de` (par son id ou son ref composé) désigne sa copie
    `vers`. Conflit au sens identique : la désignation `de` part ; au sens différent :
    `ConflitDeRenommage`, la révision est annulée."""
    apparies, orphelines = _instances(conn, de, vers)
    # Règles d'accès des comptes : une par (compte, sujet).
    regles = conn.exec_driver_sql(
        "SELECT id, instance_id, subject_user, subject_group, subject_org, level, services "
        "FROM connector_account_rules WHERE instance_id = ANY(%(ids)s)",
        {"ids": sorted(set(apparies) | orphelines)}).all()
    for rid, iid, su, sg, so, niveau, services in regles:
        if iid in orphelines:
            raise ConflitDeRenommage(
                f"connector_account_rules : la règle {rid} vise l'instance `{de}` {iid}, sans "
                f"copie `{vers}` — {_GESTE}")
        deja = conn.exec_driver_sql(
            "SELECT level, services FROM connector_account_rules WHERE instance_id = %(n)s "
            "AND subject_user IS NOT DISTINCT FROM %(su)s "
            "AND subject_group IS NOT DISTINCT FROM %(sg)s AND subject_org = %(so)s",
            {"n": apparies[iid], "su": su, "sg": sg, "so": so}).first()
        if deja is None:
            conn.exec_driver_sql("UPDATE connector_account_rules SET instance_id = %(n)s "
                                 "WHERE id = %(id)s", {"n": apparies[iid], "id": rid})
        elif tuple(deja) == (niveau, services):
            conn.exec_driver_sql("DELETE FROM connector_account_rules WHERE id = %(id)s",
                                 {"id": rid})
        else:
            raise ConflitDeRenommage(
                f"connector_account_rules : la règle {rid} et une règle de l'instance "
                f"`{vers}` {apparies[iid]} visent le même sujet avec un sens différent "
                "(level, services) — à trancher à la main, puis rejouer. La transaction "
                "de la révision est annulée : rien n'est écrit.")
    # Arêtes `grants` (aucune unicité sur la ressource : réécriture sur place).
    for gid, rid in conn.exec_driver_sql(
            "SELECT id, resource_id FROM grants WHERE resource_kind = 'connector_instance'"
            ).all():
        nouveau = _ref(rid, de, vers, apparies, orphelines)
        if nouveau != rid:
            conn.exec_driver_sql("UPDATE grants SET resource_id = %(r)s WHERE id = %(id)s",
                                 {"r": nouveau, "id": gid})
    # Liens de projet vers le connecteur : nom, instance épinglée, identité.
    liens = conn.exec_driver_sql(
        "SELECT id, project_id, target_ref, identity_ref, config, slot FROM project_links "
        "WHERE target_type = 'connecteur' AND (target_ref = %(de)s "
        "OR config ? 'instance_ref' OR identity_ref IS NOT NULL)", {"de": de}).all()
    for lid, projet, cible, identite, avant, slot in liens:
        config = dict(avant or {})
        n_cible = vers if cible == de else cible
        n_identite = _ref(identite, de, vers, apparies, orphelines)
        if config.get("instance_ref"):
            config["instance_ref"] = _ref(config["instance_ref"], de, vers, apparies,
                                          orphelines)
        if (n_cible, n_identite, config) == (cible, identite, dict(avant or {})):
            continue
        deja = conn.exec_driver_sql(
            "SELECT config, slot FROM project_links WHERE id <> %(id)s "
            "AND project_id = %(p)s AND target_type = 'connecteur' AND target_ref = %(t)s "
            "AND identity_ref IS NOT DISTINCT FROM %(i)s",
            {"id": lid, "p": projet, "t": n_cible, "i": n_identite}).first()
        if deja is not None:
            if (dict(deja[0] or {}), deja[1]) != (config, slot):
                raise ConflitDeRenommage(
                    f"project_links : le lien {lid} du projet {projet} et un lien existant "
                    f"vers `{n_cible}` ont un sens différent (config, slot) — à trancher à "
                    "la main, puis rejouer. La transaction de la révision est annulée : "
                    "rien n'est écrit.")
            conn.exec_driver_sql("DELETE FROM project_links WHERE id = %(id)s", {"id": lid})
            continue
        conn.exec_driver_sql(
            "UPDATE project_links SET target_ref = %(t)s, identity_ref = %(i)s, "
            "config = %(c)s::jsonb WHERE id = %(id)s",
            {"t": n_cible, "i": n_identite, "c": json.dumps(config), "id": lid})
    logger.info("%s : %d règle(s) de compte, liens de projet et arêtes repointés vers `%s`",
                revision, len(regles), vers)


def _jouer(de: str, vers: str, sens: str) -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire (cf. 0062).
        return
    op.execute(_ATTENTE_MAX)
    conn = op.get_bind()
    renommer_les_lignes(conn, de, vers)
    repointer_les_references(conn, de, vers)
    _revision_des_messageries().reecrire_les_listes(conn, *correspondances(sens), revision)


def upgrade() -> None:
    _jouer(ANCIEN, NOUVEAU, "montee")


def downgrade() -> None:
    _jouer(NOUVEAU, ANCIEN, "descente")
