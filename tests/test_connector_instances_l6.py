"""Lot L6 (blueprint ADR 0053-D9) — l'instance est un OBJET, et rien ne la lit encore.

Le lot ne livre qu'une table et un identifiant, et c'est sa réussite : l'existant est
**nommé**, pas déplacé. La désignation (D2) et les sous-instances (D9-3) en dépendent
toutes deux, donc l'objet arrive avant son premier usage — comme `tenants` au lot L1
et `grants` au lot L4.

Quatre choses portent son risque, et aucune n'est vérifiable par de la vigilance :

1. **L'INTENTION.** Aucun chemin de résolution ne connaît la table. Le jour où
   `walk_cascade` ou `resolve_credential` la lit, ce n'est plus L6 — c'est L7, avec sa
   revue. Le garde-fou est une **allowlist nommée** (patron
   `test_tenant_l1_migration.py`), pas une interdiction totale : la projection de
   lecture, elle, est admise et c'est la surface du lot.
2. **LA PAIRE D'INDEX.** L'unique est PARTIEL (une instance *vivante* par ligne de
   coffre) ; son jumeau est NON PARTIEL, parce que le backfill demande « existe-t-il
   une instance, **archivée comprise** ? » — c'est ce qui l'empêche de RESSUSCITER une
   instance retirée à la main entre deux boots. Les « harmoniser » casserait la garde
   en silence : exactement le geste que le lot L4 avait déjà dû interdire par écrit.
3. **LA GRAMMAIRE.** `inst:{id}` s'ajoute aux refs composés sans les remplacer : ceux-ci
   sont déjà distribués (bindings de slot B5, axe `_instance=`, `resource_id` des arêtes
   de `grants`) et rien ne les réécrit dans ce lot.
4. **LES GARDES DE POSE.** `parse_ref` accepte désormais `inst:` — mais rien ne le
   résout. Un `inst:` passé en entrée doit se voir refuser NOMMÉMENT : sans ça il
   tombe dans le refus final de `guard_instance_access` et s'entend dire que « les refs
   platform: ne s'épinglent pas ». Un message faux est pire qu'un refus.

Ces tests sont STATIQUES : ils portent sur des formes et des lieux, là où un test SQL
exigerait un PostgreSQL sans rien dire de plus. Le SQL lui-même est exercé contre un
vrai PostgreSQL par `test_connector_instances_l6_live.py`.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from oto_mcp import instance_refs
from oto_mcp.db import _schema

_OTO = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"
_SCHEMA_SRC = _schema._SCHEMA
_INIT_SRC = (_OTO / "db" / "_init.py").read_text(encoding="utf-8")


# ─── 1. L'intention : personne ne lit encore l'instance ──────────────────────

# Les SEULS fichiers admis à connaître la table ou son module. Quatre familles, et
# aucune n'est un chemin de RÉSOLUTION : le DDL, le filet de boot, la PROJECTION de
# lecture (`GET /api/me/connector-instances`), et — depuis la pièce 2 — le COFFRE,
# qui nomme ses propres lignes à la pose.
#
# ⚠️ `credentials_store.py` a rejoint la liste le 2026-08-28, **délibérément**, et
# c'est le seul ajout de la pièce 2. Ce n'est pas un assouplissement du garde-fou :
# ce qu'il protège n'a jamais été « personne ne touche la table » mais « aucune
# RÉSOLUTION n'en dépend », et écrire l'instance au moment où naît la ligne du coffre
# n'est pas résoudre. La contrepartie est mécanique et vit juste en dessous
# (`test_dans_le_coffre_seules_les_primitives_d_ECRITURE_nomment_l_instance`) : dans ce
# fichier, seules les primitives d'ÉCRITURE connaissent les instances — aucun lecteur
# de credential ne les touche, sans quoi le coffre commencerait à désigner ses clés
# autrement que par son quadruplet.
#
# ⚠️ Ce qui n'est toujours PAS ici, et qui reste tout l'objet du garde-fou :
# `access/cascade.py`, `access/resolve.py`, `access/rbac.py`, `grants_chain.py`. Le
# jour où l'un d'eux lit une instance, la résolution cesse de passer par la clé du
# coffre — c'est le lot L7, il a sa revue, et il retire cette ligne dans son propre
# commit.
_LECTEURS_ADMIS = {
    "db/connector_instances.py",        # le module lui-même
    "db/__init__.py",                   # la façade plate `db.<fn>`
    "db/_init.py",                      # le filet de boot
    "db/schema/connectors.py",          # le DDL
    "capabilities/connectors/instances.py",  # la projection de lecture
    "credentials_store.py",             # le coffre : la naissance à la pose (pièce 2)
    # #863 (04/09/2026) — sonder la session d'un tiers. Inscrit DÉLIBÉRÉMENT, comme
    # cette table l'exige, et voici pourquoi ce n'est pas le lot L7 que le garde-fou
    # retient : ce module ne RÉSOUT rien. Il traduit un `instance_id` en quadruplet de
    # coffre, puis lit le credential par ce quadruplet — exactement comme la projection
    # de lecture au-dessus. Ce que L7 vise, c'est le jour où `access/cascade.py`,
    # `access/resolve.py`, `access/rbac.py` ou `grants_chain.py` liront une instance :
    # là, la résolution cesserait de passer par la clé du coffre. Aucun d'eux n'est
    # ici, et cette ligne ne les y fait pas entrer.
    # ⚠️ Il n'existe pas de résolveur partagé à emprunter : `instance_refs.py` reste
    # PUR et dit lui-même que résoudre un `inst:{id}` demande la base. Passer par lui
    # aurait été mieux ; il ne le permet pas.
    "capabilities/instance_health.py",
}

# Dans le coffre, les SEULES fonctions admises à nommer une instance. Ce sont les
# primitives d'ÉCRITURE, et elles seules : l'entonnoir (`_upsert`/`_delete`), les deux
# purges en masse qui l'empruntent désormais, et le renommage — le seul geste qui
# DÉPLACE une ligne de coffre, donc le seul qui doive faire suivre l'instance.
_ECRIVAINS_DU_COFFRE = {
    "_upsert", "_delete", "clear_entity_credentials", "clear_connector_credentials",
    "rename_account",
}

# La table, et les symboles du module — chercher le CONCEPT, pas seulement le nom de
# la table : un appelant passe par `db.instance_ids_for_vault_rows`, jamais par du SQL.
_MARQUEURS = re.compile(
    r"connector_instances|instance_ids_for_vault_rows|instance_id_for_vault_row"
    r"|instance_by_id|name_vault_rows_as_instances|connector_instance_counts")


def _references(src: str) -> list[str]:
    """Les références RÉELLES aux instances dans du code — sonde AST, pas un `grep`.

    Un `grep` de ligne prend la prose pour un lecteur : les modules voisins ont le
    droit de DIRE où vit désormais l'identité d'une instance (`instance_refs` le fait,
    et c'est même la première chose qu'on veut y lire). Ce qu'on cherche est un
    ACCÈS : un nom, un attribut, un import — ou une table nommée dans une chaîne qui
    n'est pas une docstring. Les commentaires disparaissent avec l'AST, gratuitement.
    """
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        corps = getattr(node, "body", None)
        if not (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)) and corps):
            continue
        tete = corps[0]
        if (isinstance(tete, ast.Expr) and isinstance(tete.value, ast.Constant)
                and isinstance(tete.value.value, str)):
            docstrings.add(id(tete.value))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and _MARQUEURS.search(node.id):
            hits.append(node.id)
        elif isinstance(node, ast.Attribute) and _MARQUEURS.search(node.attr):
            hits.append(f".{node.attr}")
        elif isinstance(node, ast.alias) and _MARQUEURS.search(node.name):
            hits.append(f"import {node.name}")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and _MARQUEURS.search(node.value)):
            hits.append("chaîne : " + " ".join(node.value.split())[:70])
    return hits


def test_seule_la_projection_de_lecture_connait_les_instances():
    """L6 NOMME l'existant, il ne le déplace pas.

    Ce que le garde-fou protège n'est pas « personne ne touche la table » mais
    « aucune RÉSOLUTION n'en dépend » : la cascade, le coffre et l'autorisation
    continuent de désigner une clé par le quadruplet du coffre. Un lecteur hors
    allowlist tombe — c'est le cas qu'on veut voir en revue.
    """
    lecteurs = []
    for path in _OTO.rglob("*.py"):
        rel = path.relative_to(_OTO).as_posix()
        if rel in _LECTEURS_ADMIS:
            continue
        for hit in _references(path.read_text(encoding="utf-8")):
            lecteurs.append(f"{rel}: {hit}")
    assert not lecteurs, (
        "Quelqu'un lit les instances hors de la projection, ce qui déborde du lot L6 "
        f"(« l'existant est nommé, pas déplacé ») : {lecteurs}. Faire dépendre une "
        "résolution de cette table est un LOT (L7), avec sa revue : l'ajouter à "
        "_LECTEURS_ADMIS doit être un acte délibéré.")


def test_la_cascade_et_la_resolution_sont_litteralement_intactes():
    """Contre-épreuve nommée du test précédent — il vaut par ce qu'il EXCLUT, et un
    jour quelqu'un déplacera ces fichiers. Si l'un des deux disparaît, on veut
    l'apprendre ici et pas par un garde-fou devenu muet."""
    for rel in ("access/cascade.py", "access/resolve.py"):
        src = (_OTO / rel)
        assert src.exists(), f"{rel} a été déplacé : le garde-fou ci-dessus est à recalibrer"
        assert not _MARQUEURS.search(src.read_text(encoding="utf-8")), (
            f"{rel} lit les instances — c'est le lot L7, pas celui-ci")


def test_dans_le_coffre_seules_les_primitives_d_ECRITURE_nomment_l_instance():
    """La contrepartie de l'entrée de `credentials_store.py` dans l'allowlist.

    L'allowlist raisonne par FICHIER ; ce que le lot promet est plus fin — le coffre
    ÉCRIT l'instance, il ne la LIT jamais pour décider. Le jour où `get_credential`,
    `resolve_*` ou `list_credentials` iraient chercher une instance, la désignation
    d'une clé cesserait de passer par le quadruplet, et personne ne le verrait : le
    fichier est déjà admis. Ce test rend la promesse mécanique en descendant d'un
    étage — le grain n'est plus le fichier mais la FONCTION.
    """
    src = (_OTO / "credentials_store.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Fonctions de PREMIER niveau seulement : `ast.unparse` d'une fonction embarque ses
    # définitions internes, donc un helper imbriqué est compté chez son parent — sinon
    # deux `_do` homonymes s'écraseraient dans le relevé.
    coupables = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hits = _references(ast.unparse(node))
        if hits:
            coupables[node.name] = sorted(set(hits))
    assert set(coupables) == _ECRIVAINS_DU_COFFRE, (
        f"les fonctions du coffre qui nomment une instance ont changé : "
        f"{sorted(coupables)} (attendu {sorted(_ECRIVAINS_DU_COFFRE)}). Un LECTEUR de "
        "credential qui se met à lire les instances fait dépendre la désignation d'une "
        "clé d'autre chose que du quadruplet du coffre — c'est le lot L7, avec sa revue.")


def test_le_backfill_tourne_apres_le_schema_et_dans_la_transaction():
    """Il LIT `connector_credentials`, que le boot vient de faire évoluer (colonnes,
    PK recomposée) — même raison qui met les conversions M2/M3 et le semis L5 en fin
    de transaction. Posé avant `_SCHEMA`, il travaillerait sur l'ancienne forme.
    """
    schema = _INIT_SRC.index("conn.execute(_SCHEMA)")
    semis_l5 = _INIT_SRC.index("_seed_platform_grants_as_edges(conn)")
    backfill = _INIT_SRC.index("connector_instances.name_vault_rows_as_instances(conn)")
    assert schema < semis_l5 < backfill, (
        "le backfill L6 doit suivre `conn.execute(_SCHEMA)` (il lit le coffre que ce "
        "boot vient de faire évoluer) et rester dans la transaction de schéma.")


# ─── 2. La forme du socle ────────────────────────────────────────────────────

def _index_stmt(nom: str) -> str:
    i = _SCHEMA_SRC.index(f"IF NOT EXISTS {nom}\n")
    return _SCHEMA_SRC[i:i + _SCHEMA_SRC[i:].index(";")]


def test_l_unicite_porte_sur_le_quadruplet_du_coffre():
    """« Une instance ↔ une ligne de coffre » est tenu par la BASE, pas par le
    backfill : l'unicité porte sur les quatre colonnes qui SONT la clé du coffre."""
    stmt = _index_stmt("idx_connector_instances_vault")
    assert "ON connector_instances(owner_type, owner_id, connector, account)" in stmt, stmt
    assert "WHERE revoked_at IS NULL" in stmt, (
        "l'unique doit rester PARTIEL : une instance ARCHIVÉE ne doit pas interdire "
        "à jamais d'en poser une neuve sur la même ligne de coffre.")


def test_le_jumeau_du_backfill_reste_non_partiel():
    """⚠️ LE test de ce lot, et le jumeau exact de celui du lot L4 sur l'index de
    comptage. Le backfill demande « existe-t-il une instance pour cette ligne,
    **archivée comprise** ? » — c'est ce qui l'empêche de RESSUSCITER une instance
    retirée à la main entre deux boots. Un index partiel ne peut pas servir une
    requête qui n'a pas son prédicat : PostgreSQL retomberait sur un parcours complet,
    une fois par ligne du coffre, et personne ne le verrait passer.
    """
    stmt = _index_stmt("idx_connector_instances_vault_all")
    assert "ON connector_instances(owner_type, owner_id, connector, account)" in stmt, stmt
    assert "WHERE" not in stmt.upper(), (
        f"le jumeau du backfill est devenu PARTIEL : {stmt.strip()!r}. Ne pas "
        "l'« harmoniser » avec l'unique — leurs deux lectures sont opposées (l'unique "
        "protège les vivantes, le backfill compte les archivées).")


def test_le_vocabulaire_de_proprietaire_est_ferme_et_prevoit_le_tenant():
    m = re.search(r"CHECK \(owner_type IN \(([^)]*)\)\)", _SCHEMA_SRC)
    assert m, "le CHECK de fermeture du vocabulaire de propriétaire a disparu"
    kinds = {k.strip().strip("'") for k in m.group(1).split(",")}
    assert kinds == {"platform", "tenant", "org", "group", "member", "user"}, (
        f"vocabulaire de propriétaire modifié : {sorted(kinds)}. `tenant` y est "
        "PRÉVU et INERTE (l'entité tenant du coffre est le lot L-clés) ; `user` ne "
        "survit que pour les mounts OAuth (ADR 0033).")


def test_la_visibilite_est_une_propriete_de_l_instance_r9():
    """R9, tranché le 27/08 : la visibilité est une propriété de l'INSTANCE, dérivée
    de la chaîne, avec surcharge explicite par le propriétaire. La colonne porte la
    SURCHARGE ; `inherited` dit « laisse la dérivation décider ». **La dérivation
    n'est pas écrite** — c'est un lot, et ce test ne prétend pas le contraire."""
    m = re.search(r"CHECK \(visibility IN \(([^)]*)\)\)", _SCHEMA_SRC)
    assert m, "le CHECK de fermeture de la visibilité a disparu"
    assert {v.strip().strip("'") for v in m.group(1).split(",")} == \
        {"inherited", "hidden", "org"}
    assert "visibility TEXT NOT NULL DEFAULT 'inherited'" in _SCHEMA_SRC, (
        "le défaut doit rester `inherited` : une instance existante ne change pas de "
        "visibilité parce qu'on lui a posé une colonne.")


def test_le_compte_suit_la_convention_du_coffre_et_n_est_pas_nullable():
    """`''` est le marqueur mono-compte DU COFFRE. Une colonne nullable rendrait
    l'index unique AVEUGLE (`NULL` n'entre en conflit avec rien) — donc muet
    exactement sur les lignes qu'il existe pour protéger."""
    assert "account TEXT NOT NULL DEFAULT ''" in _SCHEMA_SRC


def test_l_instance_s_archive_elle_ne_se_supprime_pas():
    """Même parti pris que `grants` (0053-D7) : un binding, un grant ou une consommation
    qui désignent une instance doivent pouvoir la relire après son retrait."""
    debut = _SCHEMA_SRC.index("CREATE TABLE IF NOT EXISTS connector_instances")
    corps = _SCHEMA_SRC[debut:debut + _SCHEMA_SRC[debut:].index(");")]
    assert "revoked_at TIMESTAMPTZ" in corps and "created_at TIMESTAMPTZ NOT NULL" in corps
    assert "parent_id BIGINT REFERENCES connector_instances(id)" in corps, (
        "les sous-instances (0053-D9-3) exigent un parent DÉSIGNABLE — c'est la "
        "moitié de la raison d'être de l'identifiant stable.")


# ─── 3. La grammaire : `inst:{id}` s'AJOUTE, il ne remplace pas ──────────────

def test_inst_roundtrip():
    ref = instance_refs.make_instance_ref(412)
    assert ref == "inst:412"
    p = instance_refs.parse_ref(ref)
    assert (p.level, p.instance_id, p.connector) == ("inst", 412, None)
    assert instance_refs.format_ref(p) == ref


def test_les_refs_composes_restent_lisibles_a_l_identique():
    """Ils sont DÉJÀ distribués — bindings de slot, axe `_instance=`, `resource_id`
    des arêtes de `grants` — et ce lot n'en réécrit aucun."""
    for ref in ("member:8:usr_x:zoho:jane", "group:3:hunter", "org:8:bridge:prod",
                "platform:zoho:main"):
        assert instance_refs.format_ref(instance_refs.parse_ref(ref)) == ref


@pytest.mark.parametrize("bad", ["inst:", "inst:abc", "inst:1:2", "inst:-1", "inst:١٢"])
def test_inst_malforme_est_refuse(bad):
    """Même sévérité que les refs composés : l'entier est ASCII strict (`isdigit`
    seul accepte les chiffres Unicode → refs alias non-canoniques)."""
    with pytest.raises(ValueError, match="invalid_instance_ref"):
        instance_refs.parse_ref(bad)


def test_un_inst_ne_porte_ni_connecteur_ni_proprietaire():
    """C'est sa VALEUR, pas un manque : l'identifiant survit aux renommages qui
    cassent un ref composé, et le résoudre demande la base — pas ce module, qui
    reste pur (aucun import d'oto_mcp, aucune requête)."""
    p = instance_refs.parse_ref("inst:1")
    assert (p.connector, p.org_id, p.sub, p.group_id, p.label, p.account) == \
        (None, None, None, None, None, "")
    src = (_OTO / "instance_refs.py").read_text(encoding="utf-8")
    assert "from .db" not in src and "from . import" not in src, (
        "`instance_refs` doit rester PUR : c'est ce qui lui permet d'être le domicile "
        "du format sans tirer la base derrière lui.")


# ─── 4. Les gardes de pose refusent `inst:` NOMMÉMENT ────────────────────────

def test_les_gardes_de_pose_refusent_inst_avec_leur_propre_message():
    """`parse_ref` accepte `inst:` — rien ne le résout. Sans branche explicite, un
    `inst:` tombe dans le refus final de `guard_instance_access` (« les refs
    platform: ne s'épinglent pas ») et dans la garde de connecteur du binding de
    projet (« une instance `None` ») : deux messages FAUX, qui envoient chercher au
    mauvais endroit."""
    rbac = (_OTO / "access" / "rbac.py").read_text(encoding="utf-8")
    i = rbac.index("def guard_instance_access")
    corps = rbac[i:i + rbac[i:].index("\ndef ", 1)]
    assert 'ref.level == "inst"' in corps, (
        "`guard_instance_access` doit refuser `inst:` nommément tant que rien ne le "
        "résout (lot L7).")
    assert corps.index('ref.level == "inst"') < corps.index('ref.level == "member"')

    projets = (_OTO / "capabilities" / "projects.py").read_text(encoding="utf-8")
    assert 'iref.level != "inst"' in projets, (
        "le binding de projet doit refuser `inst:` AVANT sa garde de connecteur, "
        "sinon il rend « une instance `None` ».")
