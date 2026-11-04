"""Le DDL assemblé est GELÉ, fragment par fragment, et son ORDRE à part.

Le DDL vit dans `db/schema/<domaine>.py`, que `_schema.ASSEMBLAGE` concatène dans un
ordre figé. Ce que le gel protège :

1. **Une base PARTAGÉE prod/preprod** (`docs/live-migrations.md`) : le DDL exécuté
   au boot preprod s'applique instantanément à la production, qui tourne encore
   l'ancien code. Une altération accidentelle du DDL n'a pas de fenêtre de rattrapage.
2. **L'ORDRE est une contrainte d'exécution**, pas une mise en page : PostgreSQL
   crée les tables dans l'ordre du DDL, et une FK vers une table pas encore créée
   échoue sur une base VIERGE (#151 sur `orgs`, `tenants` avant `orgs` en L1,
   `grants` avant `grant_counters` en L4). Un simple réordonnancement de la liste
   d'assemblage — le genre de geste qu'un tri alphabétique « propre » produit —
   casserait tout premier boot, et rien d'autre ne le verrait.
3. **Un fragment orphelin est silencieux** : une constante déclarée dans un module
   de domaine mais absente de `ASSEMBLAGE` ne lève aucune erreur ; ses tables
   n'existent simplement jamais.

**La forme du gel** (oto-backend#789, absorbe oto#87) : un fichier par fragment,
`tests/schema_gele/<module>.<CONSTANTE>.sql`, qui porte son DDL NORMALISÉ — sans
commentaires SQL, blancs normalisés (`scripts/schema_gele.normaliser`) — et un fichier
`tests/schema_gele/ORDRE`, la liste ordonnée des fragments. Deux lots sur deux
fragments différents ne touchent plus la même ligne, et réécrire un commentaire SQL ne
casse plus rien. L'empreinte unique qui précédait, et son journal daté : `git log`.

⚠️ **Changer le DDL est un acte délibéré** : le test échoue, avec le diff, tant que le
fichier figé n'a pas suivi. On le réécrit EN LOCAL (`python -m scripts.schema_gele
--regen`) et le diff part dans le MÊME commit que le DDL, pour être lu en revue. La CI
ne régénère jamais : elle compare. Deux lots qui touchent le même fragment se
fusionnent sur le fichier figé comme sur du code : on régénère sur le résultat FUSIONNÉ,
jamais en recopiant un côté du conflit.
"""
from __future__ import annotations

import pathlib
import re
import sys

from oto_mcp.db import _schema, schema

# La racine du dépôt n'est pas dans `sys.path` en CI (pas de `__init__.py`) : `scripts`
# ne s'importait que parce qu'un banc collecté avant l'y avait mise (oto#116).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import schema_gele  # noqa: E402


def test_le_ddl_servi_est_le_ddl_fige():
    """Chaque fragment, sa forme normalisée, et l'ordre d'assemblage — contre le figé."""
    ecarts = schema_gele.ecarts()
    assert not ecarts, (
        "le DDL assemblé a changé :\n\n" + "\n\n".join(ecarts) + "\n\nSi c'est "
        f"délibéré (vraie évolution du schéma) : `{schema_gele.COMMANDE}` en local, et "
        "commite le diff de tests/schema_gele/ dans CE commit. Sinon, un déplacement de "
        "fragment a modifié le SQL — ce qui touche la base PARTAGÉE prod/preprod au "
        "premier boot.")


def test_un_commentaire_sql_ne_change_pas_le_fige():
    """Ce que oto#87 a payé trois fois : réécrire un commentaire, ré-indenter, sauter une
    ligne ne change rien d'exécutable, donc rien du figé — un ordre changé, si."""
    base = "CREATE TABLE IF NOT EXISTS t (\n    a TEXT NOT NULL DEFAULT 'x--y', -- note\n    b INT\n);\n"
    assert schema_gele.normaliser(base) == schema_gele.normaliser(
        "-- en-tête réécrit\nCREATE TABLE IF NOT EXISTS t (\n  a TEXT NOT NULL DEFAULT 'x--y',\n"
        "\n  b   INT /* autre */\n);")
    assert "'x--y'" in schema_gele.normaliser(base), "un `--` dans un littéral n'est pas un commentaire"
    assert schema_gele.normaliser(base) != schema_gele.normaliser(base.replace("b INT", "b BIGINT"))


_CREATE_TABLE = re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+)", re.M)


def test_chaque_table_a_exactement_un_domicile():
    """Un `CREATE TABLE` par domaine, et un seul — sinon le DDL en crée deux, ou
    l'un des deux dérive sans que personne ne le voie."""
    domiciles: dict[str, list[str]] = {}
    for nom_module in schema.__all__:
        module = getattr(schema, nom_module)
        for const in dir(module):
            if const.startswith("_") or not isinstance(getattr(module, const), str):
                continue
            for table in _CREATE_TABLE.findall(getattr(module, const)):
                domiciles.setdefault(table, []).append(f"{nom_module}.{const}")

    doublons = {t: d for t, d in domiciles.items() if len(d) > 1}
    assert not doublons, f"tables déclarées à plusieurs endroits : {doublons}"

    assemblees = _CREATE_TABLE.findall(_schema._SCHEMA)
    assert len(assemblees) == len(set(assemblees)), "table créée deux fois dans l'assemblage"
    assert set(assemblees) == set(domiciles), (
        "écart entre les tables des fragments et celles de l'assemblage : "
        f"orphelines={sorted(set(domiciles) - set(assemblees))}, "
        f"inconnues={sorted(set(assemblees) - set(domiciles))}")


def test_aucun_fragment_ne_reste_hors_de_l_assemblage():
    """Un fragment déclaré mais jamais assemblé ne lève rien : ses tables n'existent
    simplement pas. Le seul endroit où ça se voit est ici."""
    declares = set()
    for nom_module in schema.__all__:
        module = getattr(schema, nom_module)
        for const in dir(module):
            valeur = getattr(module, const)
            if not const.startswith("_") and isinstance(valeur, str) and "CREATE " in valeur:
                declares.add((nom_module, const))

    assembles = set()
    for fragment in _schema.ASSEMBLAGE:
        for nom_module, const in declares:
            if getattr(getattr(schema, nom_module), const) is fragment:
                assembles.add((nom_module, const))

    orphelins = sorted(declares - assembles)
    assert not orphelins, (
        f"fragments de DDL jamais assemblés : {orphelins}. Ils ne créent aucune "
        "table et personne ne s'en apercevrait — ajoute-les à `_schema.ASSEMBLAGE` "
        "à la bonne place (les FK imposent l'ordre) ou supprime-les.")


def test_l_ordre_impose_par_les_fk_est_tenu():
    """Les trois ordres déjà payés en incident, vérifiés sur la chaîne ASSEMBLÉE —
    c'est-à-dire à travers la frontière des modules, là où un déplacement les casse.

    Les tests de lot (`test_tenant_l1_migration`, `test_grants_l4_migration`) les
    gardent aussi ; ils sont répétés ici parce qu'ils sont désormais une propriété
    de l'ORDRE D'ASSEMBLAGE, pas d'un fichier."""
    ddl = _schema._SCHEMA
    for avant, apres, pourquoi in (
        ("tenants", "orgs", "orgs.tenant_id → tenants(id)"),
        ("orgs", "org_members", "org_members.org_id → orgs(id)"),
        ("grants", "grant_counters", "grant_counters → grants(id)"),
        ("docs", "doc_embeddings", "doc_embeddings.doc_id → docs(id)"),
        ("runner_fleets", "runner_jobs", "runner_jobs.fleet_id → runner_fleets(id)"),
        ("datastore_rows", "datastore_row_embeddings", "FK composite sur la PK"),
        ("org_groups", "connector_account_rules",
         "connector_account_rules.subject_group → org_groups(id)"),
        ("connector_instances", "connector_account_rules",
         "connector_account_rules.instance_id → connector_instances(id)"),
    ):
        i = ddl.index(f"CREATE TABLE IF NOT EXISTS {avant}")
        j = ddl.index(f"CREATE TABLE IF NOT EXISTS {apres}")
        assert i < j, (
            f"`{avant}` doit être créée avant `{apres}` ({pourquoi}) : sur une base "
            "VIERGE, PostgreSQL crée les tables dans l'ordre du DDL et la FK échoue.")
