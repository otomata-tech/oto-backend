"""Archiver un espace rend sa place au quota de création — et l'espace personnel
n'en a jamais pris.

Le compte derrière le plafond de `org.create` était `COUNT(*) … WHERE created_by = %s`,
sans autre clause. Deux conséquences, qui se composent en cul-de-sac :

- **l'archivage ne libérait rien.** `archive_org` pose `archived_at` (soft-delete : il
  n'existe aucun hard-delete, les FK restent pour l'audit) et l'org quitte tous les
  listings — mais elle continuait de compter. Or c'est le SEUL geste par lequel un
  compte peut redescendre sous le plafond : arrivé au plafond, il y restait, et le
  refus portait sur un nombre que plus rien ne pouvait faire baisser ;
- **l'espace personnel occupait une place.** Il est posé d'office par
  `ensure_personal_org` (avec `created_by = sub`, donc compté) et son archivage est
  refusé — le plafond RÉEL valait donc un de moins que celui annoncé par le message.

Banc : un PostgreSQL **réel**. Le défaut est un prédicat SQL — deux clauses `WHERE`
absentes — et un prédicat ne s'exerce pas contre un stub, qui ne mesurerait que sa
propre fidélité. Les tables viennent du DDL réel (`db/_schema.py`) et les colonnes
`archived_at` / `personal_of`, qui portent tout le correctif, des migrations réelles
(`db/_init.py`) : elles n'existent QUE là, un banc qui les reconstituerait à la main
testerait la représentation qu'on s'en fait.
"""
from __future__ import annotations

import ast
import re
from contextlib import contextmanager

import pytest

from oto_mcp import org_store, session_org
from oto_mcp.capabilities.orgs import core as orgs, reads as orgs_reads, update as orgs_update
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.db import _conn as _conn_mod, _init, _schema, tenants as db_tenants, users as db_users

# Identités synthétiques : le défaut se reproduit avec n'importe quel compte.
SUB = "sub-test-1"
AUTRE = "sub-test-2"


# ── le banc : DDL réel + migrations réelles ──────────────────────────────────

# Les tables que le banc monte, dans l'ORDRE du DDL réel. `tenants` est en tête :
# `orgs.tenant_id` la référence, et PostgreSQL crée les tables dans l'ordre donné —
# c'est la contrainte que `test_tenant_l1_migration` garde sur le schéma servi.
# ⚠️ `sub_aliases` : `upsert_user` la consulte quand il INSÈRE, pour refuser de
# ressusciter un compte mis en pause (2026-09-03, `docs/comptes-en-pause.md`). Le banc
# porte les tables que le code servi LIT — c'est la règle de ce fichier, et elle vient
# d'en gagner une. `tenant_admins` : depuis le 2026-09-25 le plafond ne s'applique pas à
# l'admin de son tenant, et `org_quota` le LIT à chaque création (`db.is_tenant_admin`).
_TABLES = ("tenants", "tenant_admins", "users", "orgs", "org_members", "org_groups",
           "org_group_members", "option_comps", "org_subscriptions", "sub_aliases",
           # Le journal des membres (oto#145) : chaque ajout/retrait l'écrit.
           "org_member_events")


def _real_ddl(table: str) -> str:
    """Le `CREATE TABLE` de `table`, extrait du schéma RÉEL (`db/_schema.py`)."""
    m = re.search(rf"^CREATE TABLE IF NOT EXISTS {table} \(.*?^\);",
                  _schema._SCHEMA, re.S | re.M)
    assert m, f"DDL de `{table}` introuvable dans _schema.py"
    return m.group(0)


def _real_orgs_migrations() -> list[str]:
    """Les migrations RÉELLES de `orgs` (`db/_init.py`), lues à l'AST.

    `archived_at` et `personal_of` — les deux colonnes du correctif — sont ajoutées
    par migration, pas par `_SCHEMA` : les recopier ici ferait diverger le banc du
    système au premier changement de type ou de nom.

    On écarte les migrations qui référencent une table que le banc ne monte PAS
    (aujourd'hui `kb_project_id` → `projects`) : les inventer serait exactement le
    DDL de complaisance qu'on refuse. Le filtre se dérive de `_TABLES`, la liste des
    tables réellement montées — pas d'une liste de colonnes. Une colonne ajoutée
    demain entre donc dans le banc toute seule, et une table ajoutée à `_TABLES` y
    fait entrer ses migrations sans qu'on y pense.

    ⚠️ `tenant_id` était écarté par ce filtre jusqu'au 2026-09-03 ; depuis que
    `create_org` DÉCLARE le tenant de l'org à l'INSERT, le banc doit monter `tenants`
    — sans quoi il exercerait une création d'org que la production ne fait plus.
    """
    tree = ast.parse(open(_init.__file__, encoding="utf-8").read())
    out = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and (node.value.startswith("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS")
             or node.value.startswith("CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_"))
        and all(cible in _TABLES
                for cible in re.findall(r"REFERENCES\s+(\w+)", node.value))
    ]
    for col in ("archived_at", "personal_of"):
        assert any(col in s for s in out), f"migration de `orgs.{col}` introuvable"
    return out


@pytest.fixture()
def conn(pg_module_dsn):
    psycopg = pytest.importorskip("psycopg")
    # La row factory RÉELLE : les rows du serveur sont des dicts dont les dates sont
    # déjà normalisées en chaînes. Un `dict_row` nu ferait diverger le banc du système
    # sur le seul type que le code historique suppose.
    with psycopg.connect(pg_module_dsn, row_factory=_conn_mod._str_dict_row,
                         autocommit=True) as c:
        for t in reversed(_TABLES):
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        # `option_comps` + `org_subscriptions` : la liste de mes espaces dit
        # désormais, par org, si le compte est bêta (`access.has_option`) — le
        # banc porte les tables que le code servi LIT, toujours en DDL réel.
        for t in _TABLES:
            c.execute(_real_ddl(t))
        # Le tenant primaire AVANT la migration qui le référence : `orgs.tenant_id`
        # naît `NOT NULL DEFAULT 1 REFERENCES tenants(id)`, donc la FK est violée si
        # la ligne 1 n'est pas là. Même ordre que `db/_init.py`, et c'est exactement
        # la propriété que `test_tenant_l1_migration` tient sur le schéma servi.
        c.execute("INSERT INTO tenants (id, slug, name) VALUES (1, 'oto', 'Oto') "
                  "ON CONFLICT (id) DO NOTHING")
        for stmt in _real_orgs_migrations():
            c.execute(stmt)
        yield c


@pytest.fixture()
def store(conn, monkeypatch):
    """`org_store` (et l'`upsert_user` qu'il appelle, et les lectures de rôle du
    plafond : `get_user`, `is_tenant_admin`) branchés sur la connexion du banc. Les deux modules importent `_connect` dans LEUR namespace : patcher le pool
    seul laisserait `db.users` parler à la vraie base."""
    @contextmanager
    def _connect_test():
        yield conn

    monkeypatch.setattr(org_store, "_connect", _connect_test)
    monkeypatch.setattr(db_users, "_connect", _connect_test)
    monkeypatch.setattr(db_tenants, "_connect", _connect_test)
    # Comptes déjà inscrits : `upsert_user` ne doit PAS rejouer ici ses effets de
    # première inscription (réconciliation d'invitation, `ensure_personal_org`), qui
    # poseraient un espace personnel dans le dos du test — c'est ce test qui décide
    # quel compte en a un, et quand.
    for sub in (SUB, AUTRE):
        conn.execute("INSERT INTO users (sub) VALUES (%s)", (sub,))
    return org_store


def _espace(store, sub: str, name: str) -> int:
    """Un espace créé par `sub`, dont il est admin — la forme que produit `org.create`."""
    oid = store.create_org(name, created_by=sub)
    store.add_org_member(oid, sub, "org_admin")
    return oid


def _perso(store, sub: str, name: str = "Mon espace") -> int:
    """L'espace PERSONNEL de `sub` : `ensure_personal_org` le crée exactement ainsi
    (créé par lui, puis marqué `personal_of`)."""
    oid = _espace(store, sub, name)
    with store._connect() as c:
        c.execute("UPDATE orgs SET personal_of = %s WHERE id = %s", (sub, oid))
    return oid


# ── le compte lui-même ───────────────────────────────────────────────────────

def test_archiver_rend_la_place(store):
    """Le cœur du défaut : l'org quittait les listings sans rendre sa place."""
    _espace(store, SUB, "Espace A")
    b = _espace(store, SUB, "Espace B")
    assert store.count_orgs_created_by(SUB) == 2

    assert store.archive_org(b) is True
    assert store.count_orgs_created_by(SUB) == 1
    # L'org est bien sortie des listings — c'est la moitié qui marchait déjà, et ce
    # qui rendait le symptôme illisible : l'utilisateur ne la voyait plus.
    assert b not in [o["org_id"] for o in store.list_orgs_for_user(SUB)]


def test_l_espace_personnel_ne_prend_pas_de_place(store):
    """Il est posé d'office, sans que le compte l'ait demandé : le facturer rendait
    le plafond réel inférieur d'un au plafond annoncé."""
    _perso(store, SUB)
    assert store.count_orgs_created_by(SUB) == 0

    _espace(store, SUB, "Espace A")
    assert store.count_orgs_created_by(SUB) == 1


def test_le_compte_reste_celui_des_espaces_CREES(store):
    """Le message dit « créés » ; l'axe `created_by` n'a pas bougé. Rejoindre l'espace
    d'autrui n'a jamais consommé de place et ne doit pas commencer."""
    autrui = _espace(store, AUTRE, "Espace d'autrui")
    store.add_org_member(autrui, SUB, "org_member")

    assert store.count_orgs_created_by(SUB) == 0
    assert store.count_orgs_created_by(AUTRE) == 1


def test_archiver_deux_fois_ne_rend_pas_deux_places(store):
    """`archive_org` est idempotent (False au 2ᵉ appel) : le compte ne doit pas
    dériver sous zéro à coups de rejeux."""
    a = _espace(store, SUB, "Espace A")
    assert store.archive_org(a) is True
    assert store.archive_org(a) is False
    assert store.count_orgs_created_by(SUB) == 0


# ── la séquence complète, par la capacité ────────────────────────────────────

@pytest.fixture()
def plafond(monkeypatch):
    """Plafond ramené à 3 : le défaut ne dépend pas de sa valeur, et un banc à 10
    n'exercerait rien de plus."""
    monkeypatch.setattr(orgs, "_MAX_ORGS_PER_USER", 3)
    return 3


def _creer(sub: str, name: str) -> dict:
    return orgs._create_org(ResolvedCtx(sub=sub), orgs.CreateOrgInput(name=name))


def _archiver(sub: str, org_id: int) -> dict:
    return orgs_update._archive_org(ResolvedCtx(sub=sub, org_id=org_id),
                                    orgs_update.OrgIdInput(org_id=org_id))


def test_la_sequence_du_cul_de_sac(store, plafond, monkeypatch):
    """La reproduction, de bout en bout : plafond atteint → refus ; archiver l'espace
    personnel → refusé ; archiver un autre espace → la création repasse."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)

    _perso(store, SUB)
    ids = [_creer(SUB, f"Espace {i}")["org_id"] for i in range(plafond)]

    with pytest.raises(AuthzDenied) as refus:
        _creer(SUB, "Un de trop")
    assert refus.value.status == 429 and refus.value.code == "org_quota"

    # L'espace personnel d'AUTRUI ne peut pas être rendu : il n'est de toute façon
    # pas une de nos places (depuis 2026-08-25 le sien, lui, est supprimable — voir
    # `test_le_solo_supprime_son_espace_personnel`).
    with pytest.raises(AuthzDenied) as perso:
        _archiver(SUB, _perso(store, AUTRE, "L'espace d'autrui"))
    assert perso.value.code == "personal_org"

    assert _archiver(SUB, ids[0])["archived"] is True

    # C'est l'étape qui échouait : le même refus, mot pour mot, après l'archivage.
    assert _creer(SUB, "Le remplaçant")["org_id"] not in ids
    assert store.count_orgs_created_by(SUB) == plafond


def test_le_refus_dit_le_compte_et_le_remede(store, plafond, monkeypatch):
    """Un refus qui n'annonce que son plafond laisse l'appelant sans savoir ce qui
    l'occupe ni comment redescendre — l'archivage n'est deviné par personne."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    _perso(store, SUB)
    for i in range(plafond):
        _creer(SUB, f"Espace {i}")

    with pytest.raises(AuthzDenied) as refus:
        _creer(SUB, "Un de trop")
    msg = refus.value.message
    assert f"{plafond}/{plafond}" in msg      # le compte courant, pas que le plafond
    assert "rchive" in msg                    # le geste qui libère une place


# ── l'espace personnel d'un compte SOLO ──────────────────────────────────────
#
# Un user seul n'a QUE son espace personnel : le refuser en bloc le laissait sans
# aucun moyen de supprimer quoi que ce soit (front : le bouton était même caché).
# Ce qui reste refusé, c'est ce qui n'est pas à lui, ou ce qui strandrait autrui.


def test_le_solo_supprime_son_espace_personnel(store, monkeypatch):
    """Le geste que le compte solo n'avait pas : son unique espace s'archive, sort
    des listings, et relâche le slot `personal_of` (sinon `ensure_personal_org`
    boucle sur une UniqueViolation à chaque boot)."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    perso = _perso(store, SUB)

    assert _archiver(SUB, perso)["archived"] is True

    with store._connect() as c:
        row = c.execute("SELECT personal_of, archived_at FROM orgs WHERE id = %s",
                        (perso,)).fetchone()
    assert row["personal_of"] is None and row["archived_at"] is not None
    assert perso not in [o["org_id"] for o in store.list_orgs_for_user(SUB)]


def test_le_solo_ne_reste_pas_sans_aucune_org(store, monkeypatch):
    """« Tout user a TOUJOURS une org maison » (db/users.py) est un invariant que le
    reste du backend suppose : le geste qui vide le compte le rétablit dans la foulée,
    au lieu de le laisser org-less jusqu'au prochain boot. L'espace reposé est NEUF —
    l'ancien reste archivé, hors de tous les listings."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    perso = _perso(store, SUB)

    _archiver(SUB, perso)

    repose = store.get_personal_org(SUB)
    assert repose is not None and repose != perso
    assert store.get_active_org(SUB) == repose
    assert [o["org_id"] for o in store.list_orgs_for_user(SUB)] == [repose]


def test_l_espace_repose_ne_l_est_que_si_le_compte_est_VIDE(store, monkeypatch):
    """Le filet ne se déclenche que sur zéro org restante : archiver un espace parmi
    d'autres ne doit pas faire surgir un espace perso que le compte n'a pas demandé."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    a = _espace(store, SUB, "Espace A")
    _espace(store, SUB, "Espace B")

    _archiver(SUB, a)

    assert store.get_personal_org(SUB) is None
    assert len(store.list_orgs_for_user(SUB)) == 1


def test_l_espace_personnel_d_autrui_reste_refuse(store, monkeypatch):
    """`ORG_ADMIN_OF` s'obtient aussi par escalade platform_admin : sans cette garde,
    ce chemin self-service effacerait l'espace PRIVÉ d'un tiers."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    autrui = _perso(store, AUTRE)

    with pytest.raises(AuthzDenied) as refus:
        _archiver(SUB, autrui)
    assert refus.value.status == 400 and refus.value.code == "personal_org"
    assert store.get_personal_org(AUTRE) == autrui   # intact


def test_un_espace_perso_qui_gagne_un_membre_n_est_plus_perso(store, monkeypatch):
    """Pourquoi la capacité ne porte PAS de garde « et seulement s'il est seul » :
    elle serait morte. Le store tient déjà l'invariant — `add_org_member` efface
    `personal_of` au 2ᵉ membre (correctif 2026-08-04) — donc « perso » implique
    « solo », et l'espace qui a gagné un coéquipier s'archive par la voie ordinaire."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    perso = _perso(store, SUB)
    store.add_org_member(perso, AUTRE, "org_member")

    assert store.is_personal_org(perso) is False
    assert store.get_personal_org(SUB) is None
    assert _archiver(SUB, perso)["archived"] is True


# ── #467 : ce que l'archivage ferme, il doit le fermer PARTOUT ───────────────
#
# Quatrième signal du 15-16/08, et le seul qui ne parle pas du compte : sur l'org
# archivée #229, `oto_org op=update` a RÉUSSI et l'a renommée, pendant que tous les
# autres axes la déclaraient hors d'atteinte — `_org=229` sur `oto_project op=list`
# refusé (« Tu n'es membre d'aucune org #229 »), et l'org absente d'`oto_list_orgs`.
#
# La racine est une asymétrie de seam, pas un oubli de garde :
#
#   - les LECTURES passent par `list_orgs_for_user`, qui JOINT `orgs` et filtre
#     `archived_at IS NULL` → l'org archivée n'existe plus pour elles ;
#   - le RÔLE, lui, sort de `get_org_role`, qui lit `org_members` SEULE, sans
#     jointure sur `orgs` : `archived_at` ne l'atteint jamais. `roles.is_org_admin`
#     rend donc True sur une org archivée, `ORG_ADMIN_OF` laisse passer, et le
#     handler ne vérifie que l'EXISTENCE de la ligne (`get_org` ne filtre pas non
#     plus l'archivage).
#
# On ne corrige pas `get_org_role` : le rôle sur une org archivée reste VRAI (les
# membres sont conservés, c'est un soft-delete), et `org.archive` a besoin que le
# rôle survive à l'archivage pour rester idempotent. Ce qui doit changer, c'est ce
# que la CAPACITÉ en déduit — d'où une règle d'autz dédiée, déclarée au niveau
# capacité comme toutes les autres (ADR 0009 §7), jamais un `if` dans le handler.


def _authz_de(key: str):
    """La règle d'autz DÉCLARÉE par la capacité `key`, prise dans le registre.

    Le test vise la déclaration, pas le combinateur : brancher la bonne règle est
    précisément ce qui manquait, et une assertion sur `_authz.ORG_ADMIN_OF_LIVE`
    resterait verte si `org.update` continuait d'être déclarée avec l'ancienne."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    for c in CAPABILITIES:
        if c.key == key:
            return c.authz
    raise AssertionError(f"capacité `{key}` absente du registre")


@pytest.fixture()
def sans_escalade(monkeypatch):
    """Neutralise les DEUX chemins d'escalade que `ORG_ADMIN_OF` porte en plus de
    l'appartenance (platform_admin, opérateur en consultation) : ils ne sont pas le
    sujet, et les laisser vivants ferait passer le test par un chemin qui n'est pas
    celui du signal. Le rôle réel, lui, sort bien du banc PG."""
    from oto_mcp import access, roles, session_org as so
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: False)
    monkeypatch.setattr(so, "current_view_org", lambda: None)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "user")


def test_l_org_archivee_ne_se_renomme_plus(store, sans_escalade):
    """Le geste exact du signal : org archivée, puis renommage. Il réussissait."""
    from oto_mcp.capabilities._types import RawCtx

    oid = _espace(store, SUB, "Ciao Capital")
    autz = _authz_de("org.update")
    inp = orgs_update.UpdateOrgInput(org_id=oid, name="Renommée")

    # Tant qu'elle est vivante, rien ne change : l'admin la renomme.
    assert autz(RawCtx(sub=SUB), inp).org_id == oid

    store.archive_org(oid)

    with pytest.raises(AuthzDenied) as refus:
        autz(RawCtx(sub=SUB), inp)
    assert refus.value.status == 409 and refus.value.code == "org_archived"


def test_le_refus_de_l_org_archivee_dit_ce_qui_cloche_et_la_suite(store, sans_escalade):
    """Règle de la maison, née des trois autres signaux : un refus NOMME l'état qui
    bloque et ce qu'il reste à faire. « Réservé à un org_admin » aurait été faux (il
    l'EST) et muet sur l'archivage — l'appelant aurait cherché du côté des droits."""
    from oto_mcp.capabilities._types import RawCtx

    oid = _espace(store, SUB, "Ciao Capital")
    store.archive_org(oid)

    with pytest.raises(AuthzDenied) as refus:
        _authz_de("org.update")(RawCtx(sub=SUB),
                                orgs_update.UpdateOrgInput(org_id=oid, name="X"))
    msg = refus.value.message
    assert "rchiv" in msg                      # l'état qui bloque, nommé
    assert str(oid) in msg                      # DE QUEL espace on parle
    assert "restaur" in msg.lower()             # et que la restauration n'est pas self-service


def test_les_deux_axes_disent_desormais_la_meme_chose(store, sans_escalade):
    """Le fond de #467 : une org ne doit pas être MUTABLE par quelqu'un à qui la
    lecture répond qu'il n'en est pas membre. On fige l'accord des deux seams."""
    from oto_mcp.capabilities._types import RawCtx

    oid = _espace(store, SUB, "Ciao Capital")
    store.archive_org(oid)

    # axe lecture / entrée : inchangé, il refusait déjà
    with pytest.raises(ValueError):
        store.resolve_org_for_user(SUB, str(oid))
    # axe écriture : il refuse enfin aussi
    with pytest.raises(AuthzDenied):
        _authz_de("org.update")(RawCtx(sub=SUB),
                                orgs_update.UpdateOrgInput(org_id=oid, name="X"))


def test_archiver_reste_idempotent_sur_une_org_deja_archivee(store, sans_escalade):
    """La contre-épreuve, et la raison pour laquelle `org.archive` NE prend PAS la
    nouvelle règle : son contrat public (`OrgArchived`) promet qu'archiver deux fois
    répond `ok: true, archived: false` — « c'était déjà fait ». Lui interdire l'org
    archivée transformerait cet idempotent documenté en 409, et un client qui
    retente une suppression perdue casserait."""
    from oto_mcp.capabilities._types import RawCtx

    oid = _espace(store, SUB, "Ciao Capital")
    store.archive_org(oid)

    ctx = _authz_de("org.archive")(RawCtx(sub=SUB), orgs_update.OrgIdInput(org_id=oid))
    assert ctx.org_id == oid


def test_un_patch_partiel_n_efface_pas_les_champs_omis(store):
    """La question laissée ouverte par #467 : le renommage de l'org archivée avait
    rendu `description`/`industry`/`location` vides, et le rapporteur ne pouvait pas
    savoir de l'extérieur s'il les avait EFFACÉS. Réponse : non — `update_org` ne
    construit un `SET` que pour les champs non-None (None = conserver, chaîne vide =
    effacer). Ils étaient déjà vides. On l'épingle plutôt que de le laisser reposer
    sur une relecture."""
    oid = _espace(store, SUB, "Espace A")
    store.update_org(oid, description="une description", industry="SaaS",
                     location="Marseille")

    store.update_org(oid, name="Renommé")

    o = store.get_org(oid)
    assert o["name"] == "Renommé"
    assert (o["description"], o["industry"], o["location"]) == \
           ("une description", "SaaS", "Marseille")


# ── #464 (2ᵉ demande) : le compte se lit AVANT le mur ────────────────────────
#
# Le premier signal portait DEUX demandes ; ec976b0 n'a traité que la première (que
# le refus dise le compte et le remède). La seconde reste : « surface the count
# before the wall, so a procedure can warn at 9 of 10 rather than fail at 11 ».
# Un plafond qui ne se lit qu'en s'y cognant coûte un tour à chaque procédure qui
# crée des espaces — et celle qui l'a signalé (prospect KB) en crée un par run.


def test_la_liste_de_mes_espaces_porte_le_quota(store, plafond, monkeypatch):
    """`oto_list_orgs` est l'outil par lequel un agent regarde ses espaces : c'est là
    que le compte doit être lisible, sans avoir à tenter une création pour l'apprendre."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    _perso(store, SUB)
    _creer(SUB, "Espace 0")

    out = orgs_reads._list_my_orgs(ResolvedCtx(sub=SUB), orgs_reads.NoInput())

    assert out["quota"] == {"created": 1, "cap": plafond, "remaining": plafond - 1}


def test_le_quota_lu_est_CELUI_qui_refuse(store, plafond, monkeypatch):
    """Le piège que ce lot doit fermer définitivement : deux comptes qui divergent.
    Si la liste comptait autrement que le refus, on aurait remplacé un mur muet par
    un mur qui MENT — pire. On exerce donc la frontière : la liste annonce 0 place
    restante exactement quand la création refuse."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    _perso(store, SUB)
    ids = [_creer(SUB, f"Espace {i}")["org_id"] for i in range(plafond)]

    out = orgs_reads._list_my_orgs(ResolvedCtx(sub=SUB), orgs_reads.NoInput())
    assert out["quota"]["remaining"] == 0
    with pytest.raises(AuthzDenied):
        _creer(SUB, "Un de trop")

    # ... et l'archivage rend la place des DEUX côtés à la fois.
    _archiver(SUB, ids[0])
    out = orgs_reads._list_my_orgs(ResolvedCtx(sub=SUB), orgs_reads.NoInput())
    assert out["quota"]["remaining"] == 1
    assert _creer(SUB, "Le remplaçant")["org_id"] not in ids


def test_le_quota_ne_compte_ni_l_archive_ni_le_perso(store, plafond, monkeypatch):
    """Même règle de comptage que `count_orgs_created_by`, vue depuis la liste : elle
    ne peut pas redevenir un total historique sans que ce test tombe."""
    monkeypatch.setattr(session_org, "current_session_id", lambda: None)
    _perso(store, SUB)
    a = _creer(SUB, "Espace A")["org_id"]
    _creer(SUB, "Espace B")
    _archiver(SUB, a)

    out = orgs_reads._list_my_orgs(ResolvedCtx(sub=SUB), orgs_reads.NoInput())
    assert out["quota"]["created"] == 1
    # et la liste elle-même ne montre ni l'archivée ni... l'espace perso, qui lui EST
    # listé (il est bien à moi) : c'est le QUOTA qui l'exclut, pas la liste.
    assert a not in [o["org_id"] for o in out["orgs"]]
