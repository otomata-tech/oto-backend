"""Lot L6 pièce 2 — l'instance NAÎT à la pose, et meurt archivée. Vrai PostgreSQL.

La pièce 1 avait posé la table et nommé l'existant **au boot**. Le prix assumé était
une fenêtre : une clé posée depuis le dernier boot n'avait pas d'identifiant. Cette
pièce ferme la fenêtre en accrochant la naissance **au fond du coffre**, dans son
entonnoir unique d'écriture (`_upsert` / `_delete`), et pas surface par surface — le
relevé est net : il n'existe **qu'un** `INSERT INTO connector_credentials` et **qu'un**
`DELETE` dans tout le dépôt, et toutes les surfaces déclaratives (clé membre, org,
groupe, plateforme, session navigateur, OAuth) y aboutissent.

Ce que seul un vrai serveur peut dire, et pourquoi chaque test existe :

- **l'atomicité** — la ligne du coffre et son instance commitent ou rollbackent
  ENSEMBLE. Un stub ne peut pas mentir là-dessus : il n'a pas de transaction ;
- **l'index unique PARTIEL** — reposer une clé retirée doit créer une instance NEUVE
  (l'ancienne est archivée), et c'est la base qui doit l'accepter ;
- **le renommage** — le geste rechiffre (upsert au nouveau nom + delete de l'ancien) ;
  l'instance doit le TRAVERSER en gardant son id, sinon le lot n'a servi à rien ;
- **le filet de boot devient un no-op** — 0 instance nommée après ce lot, ce qui n'est
  démontrable qu'en rejouant le vrai `init_db()` sur une base déjà peuplée ;
- **l'invariant** — chaque ligne de coffre vivante a exactement une instance vivante,
  et réciproquement. C'est la requête qu'on ira jouer en preprod ; elle est ici pour
  qu'elle ne soit pas écrite pour la première fois là-bas.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import base64
import os
import uuid

import pytest

SUB = "usr_naissance"
ORG = 1
GROUP = 7
MEMBER = f"{ORG}:{SUB}"

# Master key JETABLE : ce lot ne déchiffre rien, mais `set_credential` CHIFFRE — sans
# clé il lève avant d'atteindre la moindre écriture, et le test ne prouverait rien.
_KEY = base64.b64encode(b"\x11" * 32).decode()


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Une base JETABLE, le VRAI `init_db()`, le vrai pool. Rend `init_db` pour
    pouvoir le REJOUER — c'est ce qui prouve que le filet est devenu un no-op."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_l6b_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = _KEY
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield init_db
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _rows(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


@pytest.fixture(autouse=True)
def table_rase(live):
    """Chaque test repart d'un coffre et d'un inventaire d'instances VIDES : les
    assertions portent sur des populations entières, pas sur des deltas."""
    _exec("DELETE FROM connector_instances")
    _exec("DELETE FROM connector_credentials")


def _instances(vivantes=None):
    ou = ""
    if vivantes is True:
        ou = " WHERE revoked_at IS NULL"
    elif vivantes is False:
        ou = " WHERE revoked_at IS NOT NULL"
    return _rows("SELECT id, owner_type, owner_id, connector, account, revoked_at, "
                 f"revoked_reason FROM connector_instances{ou} "
                 "ORDER BY owner_type, owner_id, connector, account, id")


def _quad(rows):
    return [(r["owner_type"], r["owner_id"], r["connector"], r["account"]) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# LA REQUÊTE D'INVARIANT — celle qu'on jouera en preprod après le merge.
#
# Elle dit les DEUX sens en un seul passage, et c'est voulu : une instance orpheline
# (« je désigne une clé qui n'existe pas ») est au moins aussi grave qu'une clé sans
# nom, et une requête qui n'en verrait qu'un côté laisserait croire que tout va bien.
# `FULL OUTER JOIN` sur le quadruplet : ce qui sort d'un côté seulement EST l'écart.
# ⚠️ Les instances ARCHIVÉES sont hors périmètre des deux côtés — c'est leur raison
# d'être (une consommation ou un partage passés doivent rester relisibles).
INVARIANT_SQL = """
SELECT COALESCE(c.entity_type, i.owner_type) AS owner_type,
       COALESCE(c.entity_id,   i.owner_id)   AS owner_id,
       COALESCE(c.connector,   i.connector)  AS connector,
       COALESCE(c.account,     i.account)    AS account,
       CASE WHEN i.id IS NULL THEN 'coffre sans instance'
            ELSE 'instance sans ligne de coffre' END AS ecart
  FROM connector_credentials c
  FULL OUTER JOIN (SELECT * FROM connector_instances WHERE revoked_at IS NULL
                     AND NOT (connector = 'unipile' AND starts_with(account, 'canal:'))) i
    ON  i.owner_type = c.entity_type AND i.owner_id = c.entity_id
    AND i.connector  = c.connector   AND i.account  = c.account
 WHERE c.entity_type IS NULL OR i.id IS NULL
 ORDER BY 1, 2, 3, 4
"""


def _ecarts():
    return _rows(INVARIANT_SQL)


# ─── 1. La naissance ─────────────────────────────────────────────────────────

def test_poser_une_cle_cree_son_instance_sans_attendre_un_boot(live):
    """LE test du lot. Avant lui, `id` manquait sur toute clé posée depuis le dernier
    redémarrage — une absence que la surface devait annoncer comme normale."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "hunter", "k1", set_by=SUB)
    insts = _instances(vivantes=True)
    assert _quad(insts) == [("member", MEMBER, "hunter", "")]
    assert not _ecarts()


def test_reposer_la_meme_cle_ne_cree_pas_une_seconde_instance(live):
    """Une rotation de secret n'est pas une naissance : c'est la même instance."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "hunter", "k1", set_by=SUB)
    premier = _instances(vivantes=True)[0]["id"]
    cs.set_credential("member", MEMBER, "hunter", "k2", set_by=SUB)
    insts = _instances()
    assert len(insts) == 1 and insts[0]["id"] == premier


def test_les_quatre_paliers_naissent_par_le_meme_entonnoir(live):
    """Membre, groupe, org, plateforme : quatre surfaces déclaratives, un seul
    `_upsert`. Le test vaut par ce qu'il n'a PAS besoin de faire — aucune de ces
    surfaces n'a de crochet à elle."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    cs.set_credential("group", str(GROUP), "hunter", "k", set_by=SUB)
    cs.set_credential("org", str(ORG), "hunter", "k", set_by=SUB)
    cs.set_credential("platform", "env", "hunter", "k", set_by="system")
    assert _quad(_instances(vivantes=True)) == [
        ("group", str(GROUP), "hunter", ""),
        ("member", MEMBER, "hunter", ""),
        ("org", str(ORG), "hunter", ""),
        ("platform", "env", "hunter", ""),
    ]
    assert not _ecarts()


def test_un_proprietaire_hors_vocabulaire_est_un_refus_NOMME_et_n_ecrit_rien(live):
    """Pas de repli, pas de log muet : la pose LÈVE, en nommant le type refusé — et
    la transaction emporte la ligne du coffre avec elle. Mesuré en prod avant la
    pièce 1 : zéro ligne hors vocabulaire ; ce test garde le zéro."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import connector_instances as ci

    with pytest.raises(ci.OwnerKindUnknown, match="tenant_draft"):
        cs.set_credential("tenant_draft", "x", "hunter", "k", set_by=SUB)
    assert _rows("SELECT 1 FROM connector_credentials") == []
    assert _instances() == []


def test_une_ecriture_versionnee_devancee_ne_laisse_pas_d_instance(live):
    """Le verrou optimiste refuse l'écriture (0 ligne touchée) : la naissance est
    placée APRÈS ce verdict, donc rien ne survit à un conflit."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "hunter", "k1", set_by=SUB)
    _exec("DELETE FROM connector_instances")
    with pytest.raises(cs.ConcurrencyConflict):
        cs.set_credential("member", MEMBER, "hunter", "k2", set_by=SUB,
                          expected_version=99)
    assert _instances() == []


def test_la_naissance_participe_a_la_transaction_de_l_appelant(live):
    """`conn` fourni ⟹ la ligne du coffre et son instance vivent la MÊME transaction.
    Un rollback doit emporter les deux — sinon on fabrique des instances orphelines
    au premier échec d'un flux OAuth."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db._conn import _connect

    class Boum(RuntimeError):
        pass

    with pytest.raises(Boum):
        with _connect() as conn:
            with conn.transaction():
                cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB,
                                  conn=conn)
                raise Boum()
    assert _rows("SELECT 1 FROM connector_credentials") == []
    assert _instances() == []


# ─── 2. Le retrait : on ARCHIVE, on ne supprime pas ──────────────────────────

def test_retirer_une_cle_archive_son_instance(live):
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    ident = _instances(vivantes=True)[0]["id"]
    assert cs.clear_credential("member", MEMBER, "hunter") is True
    archivees = _instances(vivantes=False)
    assert [r["id"] for r in archivees] == [ident]
    assert archivees[0]["revoked_reason"] == "credential_removed", (
        "un archivage muet est indistinguable six mois plus tard d'une réparation "
        "de maintenance — le motif est ce qui les sépare")
    assert _instances(vivantes=True) == []
    assert not _ecarts()


def test_reposer_apres_un_retrait_cree_une_instance_NEUVE(live):
    """Une instance archivée est une histoire close. La ressusciter rendrait vivant
    un partage ou une consommation qu'on avait coupés — d'où un id neuf, que l'index
    unique PARTIEL est précisément là pour permettre."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    ancienne = _instances(vivantes=True)[0]["id"]
    cs.clear_credential("member", MEMBER, "hunter")
    cs.set_credential("member", MEMBER, "hunter", "k2", set_by=SUB)

    vivantes, archivees = _instances(vivantes=True), _instances(vivantes=False)
    assert len(vivantes) == 1 and len(archivees) == 1
    assert vivantes[0]["id"] != ancienne and archivees[0]["id"] == ancienne
    assert not _ecarts()


def test_supprimer_un_groupe_archive_ses_instances(live):
    """Un des trois DELETE en masse qui contournaient l'entonnoir. Il ne le contourne
    plus : il passe par la primitive du coffre, qui archive."""
    from oto_mcp import credentials_store as cs, group_store

    _exec("INSERT INTO orgs (id, name) VALUES (%s, 'o') ON CONFLICT DO NOTHING", (ORG,))
    _exec("INSERT INTO org_groups (id, org_id, name) VALUES (%s, %s, 'g') "
          "ON CONFLICT DO NOTHING", (GROUP, ORG))
    cs.set_credential("group", str(GROUP), "hunter", "k", set_by=SUB)
    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)

    assert group_store.delete_group(GROUP) is True
    assert _quad(_instances(vivantes=True)) == [("member", MEMBER, "hunter", "")]
    assert _quad(_instances(vivantes=False)) == [("group", str(GROUP), "hunter", "")]
    assert not _ecarts()


def test_deconnecter_tous_les_comptes_google_archive_leurs_instances(live):
    """Deuxième DELETE en masse. `account=None` = « tous les comptes de l'entité »."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import google

    _exec("INSERT INTO orgs (id, name) VALUES (%s, 'o') ON CONFLICT DO NOTHING", (ORG,))
    _exec("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (SUB,))
    for compte in ("a@b.io", "c@d.io"):
        cs.set_credential("member", MEMBER, "google", "rt", set_by=SUB, account=compte,
                          meta={"granted_at": "2026-01-01"})
    google.delete_google_oauth(SUB, ORG, account=None)

    assert _instances(vivantes=True) == []
    assert len(_instances(vivantes=False)) == 2
    assert not _ecarts()


def test_retirer_une_app_d_editeur_archive_son_instance(live):
    """Troisième DELETE en masse — et le rappel que l'app OAuth d'un ÉDITEUR a bien
    une instance : le filet de boot lui en donnait déjà une (c'est une ligne de coffre
    au palier plateforme), la faire disparaître à la pose créerait deux populations
    différentes selon le chemin d'écriture."""
    from oto_mcp import credentials_store as cs

    cs.set_editor_app("zoho", "eu", {"client_id": "ci", "client_secret": "cs"})
    assert _quad(_instances(vivantes=True)) == [("platform", "editor:eu", "zoho", "")]
    assert cs.clear_editor_app("zoho", "eu") is True
    assert _instances(vivantes=True) == []
    assert len(_instances(vivantes=False)) == 1
    assert not _ecarts()


# ─── 3. Le renommage : l'instance SUIT, elle ne renaît pas ───────────────────

def test_renommer_un_compte_garde_l_id_de_l_instance(live):
    """La raison d'être du lot, exercée sur le seul geste qui DÉPLACE une ligne de
    coffre. Le renommage rechiffre (upsert au nouveau nom + delete de l'ancien) :
    naïvement crocheté, il tuerait l'instance et en ferait naître une autre — soit
    exactement le ref composé qu'on remplace."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "zoho", "k", set_by=SUB)
    avant = _instances(vivantes=True)[0]["id"]
    issue = cs.rename_account("member", MEMBER, "zoho", "", "principal")

    assert bool(issue) is True and issue.moved is True
    assert issue.archived_instance_id is None
    apres = _instances(vivantes=True)
    assert len(apres) == 1 and apres[0]["id"] == avant
    assert apres[0]["account"] == "principal"
    assert _instances(vivantes=False) == []
    assert not _ecarts()


def test_le_passage_au_multi_compte_fait_suivre_la_ligne_mono(live):
    """Le vrai chemin d'appel du renommage : poser un compte NOMMÉ là où existait la
    ligne mono `''` migre celle-ci vers « principal ». Deux instances au bout, dont
    une qui a traversé sans changer d'id."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "zoho", "k", set_by=SUB)
    mono = _instances(vivantes=True)[0]["id"]
    cs.ensure_named_coexistence("member", MEMBER, "zoho", "jane")
    cs.set_credential("member", MEMBER, "zoho", "k2", set_by=SUB, account="jane")

    vivantes = _instances(vivantes=True)
    assert _quad(vivantes) == [("member", MEMBER, "zoho", "jane"),
                               ("member", MEMBER, "zoho", "principal")]
    assert [v["id"] for v in vivantes if v["account"] == "principal"] == [mono]
    assert not _ecarts()


def test_un_renommage_vers_une_instance_deja_vivante_le_DIT(live):
    """Cas de réparation d'un écart (une instance vivante sans sa ligne de coffre) :
    l'arrivée gagne, le départ s'archive — mais jamais en silence. Le geste rend
    QUELLE instance il a archivée et au profit de laquelle."""
    from oto_mcp import credentials_store as cs

    cs.set_credential("member", MEMBER, "zoho", "k", set_by=SUB)
    depart = _instances(vivantes=True)[0]["id"]
    # L'écart, fabriqué à la main : une instance à l'arrivée, sans ligne de coffre.
    _exec("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
          "VALUES ('zoho', 'member', %s, 'principal')", (MEMBER,))
    arrivee = [i["id"] for i in _instances(vivantes=True) if i["account"] == "principal"][0]

    issue = cs.rename_account("member", MEMBER, "zoho", "", "principal")
    assert issue.moved is False
    assert issue.archived_instance_id == depart
    assert issue.kept_instance_id == arrivee
    assert [i["id"] for i in _instances(vivantes=True)] == [arrivee]
    assert [i["revoked_reason"] for i in _instances(vivantes=False)] == \
        ["renamed_onto_existing"]


# ─── 4. Le filet de boot, et l'invariant ─────────────────────────────────────

def test_le_backfill_de_boot_est_devenu_un_NO_OP(live):
    """La preuve que la fenêtre est fermée : après ce lot, un boot ne nomme plus rien
    parce qu'il n'y a plus rien à nommer. C'est la mesure qu'on ira refaire en
    preprod — ici on la fait sur un boot RÉEL, pas sur une intention."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import connector_instances as ci
    from oto_mcp.db._conn import _connect

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    cs.set_credential("org", str(ORG), "hunter", "k", set_by=SUB)
    cs.set_credential("platform", "env", "hunter", "k", set_by="system")

    with _connect() as conn:
        assert ci.name_vault_rows_as_instances(conn) == 0
    live()   # et le VRAI init_db(), pour de bon
    assert not _ecarts()


def test_le_filet_garde_sa_garde_anti_resurrection_et_l_invariant_le_VOIT(live):
    """L'unique angle mort, nommé plutôt que bouché. Le filet refuse de nommer une
    ligne de coffre qui porte déjà une instance ARCHIVÉE — garde posée délibérément
    en pièce 1, et qu'on ne retire pas ici. Après ce lot ce cas ne naît plus que d'un
    geste manuel en base (archiver une instance en laissant vivre sa clé) : ce n'est
    donc pas le filet qui doit le rattraper, c'est la requête d'invariant qui doit le
    MONTRER. Elle le montre."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import connector_instances as ci
    from oto_mcp.db._conn import _connect

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    _exec("UPDATE connector_instances SET revoked_at = NOW()")

    with _connect() as conn:
        assert ci.name_vault_rows_as_instances(conn) == 0     # la garde tient
    assert [e["ecart"] for e in _ecarts()] == ["coffre sans instance"]


def test_l_invariant_voit_aussi_l_instance_orpheline(live):
    """L'autre sens, celui qu'une requête à un seul côté laisserait passer : une
    instance qui désigne une clé qui n'existe pas est au moins aussi grave qu'une clé
    sans nom — c'est un objet qu'un binding ou une arête peuvent nommer."""
    _exec("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
          "VALUES ('hunter', 'org', '99', '')")
    assert [(e["owner_id"], e["ecart"]) for e in _ecarts()] == \
        [("99", "instance sans ligne de coffre")]


def test_une_bascule_de_compte_ne_detache_pas_l_instance_de_sa_ligne(live):
    """Migrer un compte ne repointe JAMAIS la ligne du coffre par un UPDATE nu
    (l'identité entre dans le sceau du chiffrement). Depuis #439 la clé personnelle
    SUIT pourtant la personne : rechiffrée en place sous la nouvelle entité — et son
    instance la suit AVEC SON ID. L'invariant tient des deux côtés : aucune orpheline,
    aucune ligne sans instance, et la clé s'ouvre sous son nouveau nom."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import users

    _exec("INSERT INTO orgs (id, name) VALUES (%s, 'o') ON CONFLICT DO NOTHING", (ORG,))
    for s in (SUB, "usr_neuf"):
        _exec("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))
    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    avant = _instances(vivantes=True)

    users.migrate_sub(SUB, "usr_neuf")
    nouveau = cs.member_id(ORG, "usr_neuf")
    apres = _instances(vivantes=True)
    assert _quad(apres) == [("member", nouveau, "hunter", "")]
    assert [i["id"] for i in apres] == [i["id"] for i in avant], "l'instance a changé d'id"
    assert cs.get_credential("member", nouveau, "hunter") == "k"
    assert not _ecarts()


# ─── 5. La maintenance : archiver les orphelines d'AVANT la pièce 2 ──────────

def test_l_orpheline_se_liste_et_s_archive_avec_son_motif(live):
    """Le geste de réparation, exercé dans le sens que la pièce 2 ne peut plus créer
    mais que la base servie porte encore (2 lignes mesurées le 28/08, sur 139).

    Il ARCHIVE, il ne supprime pas : si un binding ou une arête a nommé l'orpheline,
    « elle a été retirée » et « elle n'a jamais existé » ne sont pas le même verdict.
    Et il pose son MOTIF — sans lui, six mois plus tard, cet archivage-là serait
    indistinguable du retrait ordinaire d'une clé par son propriétaire."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import connector_instances as ci

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)   # celle-ci va bien
    _exec("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
          "VALUES ('hunter', 'org', '99', '')")                      # l'orpheline

    orphelines = ci.list_orphan_instances()
    assert _quad(orphelines) == [("org", "99", "hunter", "")]

    assert ci.archive_orphan_instances() == 1
    assert ci.list_orphan_instances() == []
    assert _quad(_instances(vivantes=True)) == [("member", MEMBER, "hunter", "")]
    assert [i["revoked_reason"] for i in _instances(vivantes=False)] == \
        ["vault_row_missing"]
    assert not _ecarts()


def test_archiver_les_orphelines_est_IDEMPOTENT(live):
    """Le prédicat est « vivante ET sans ligne de coffre » : une fois archivées, elles
    n'y répondent plus. C'est ce qui permet de relancer la commande sans réfléchir —
    et ce qui fait qu'elle ne mordra jamais sur une instance saine."""
    from oto_mcp.db import connector_instances as ci

    _exec("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
          "VALUES ('hunter', 'org', '99', '')")
    assert ci.archive_orphan_instances() == 1
    assert ci.archive_orphan_instances() == 0
    assert len(_instances(vivantes=False)) == 1


def test_la_maintenance_ne_touche_PAS_une_instance_saine(live):
    """La contre-épreuve, et la seule qui compte pour une commande qu'on lancera sur
    la base servie : ce qui a sa ligne de coffre reste vivant."""
    from oto_mcp import credentials_store as cs
    from oto_mcp.db import connector_instances as ci

    cs.set_credential("member", MEMBER, "hunter", "k", set_by=SUB)
    cs.set_credential("org", str(ORG), "hunter", "k", set_by=SUB)
    assert ci.archive_orphan_instances() == 0
    assert len(_instances(vivantes=True)) == 2
    assert _instances(vivantes=False) == []
