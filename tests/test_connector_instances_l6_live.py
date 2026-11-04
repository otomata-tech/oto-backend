"""Lot L6 — la table, le backfill et l'identifiant stable, contre un VRAI PostgreSQL.

Ce que seul un vrai serveur peut dire, et que la suite statique ne prouverait pas :

- la table **naît au boot** — dans le vrai `init_db()`, sur le vrai schéma assemblé ;
- le **backfill nomme chaque ligne de coffre exactement une fois**, et rejoué il est
  un no-op (l'idempotence n'est pas une intention, c'est un deuxième `init_db()`) ;
- il ne **RESSUSCITE pas** une instance archivée entre deux boots — la garde est un
  `NOT EXISTS` sans filtre sur `revoked_at`, servie par l'index NON PARTIEL ;
- une ligne de coffre au `entity_type` inconnu **ne fait pas tomber le boot** : le
  CHECK de la table la refuserait, donc la transaction de schéma entière — sur une
  base PARTAGÉE avec la production ;
- l'unicité « une instance vivante par ligne de coffre » est tenue **par la base** ;
- `inst:{id}` **résout vers la bonne ligne de coffre** ;
- et le tripwire du lot : **le coffre, le registre et la cascade rendent exactement la
  même chose avant et après** que la table soit peuplée. Le lot est ADDITIF ou il
  n'est rien — et « additif » se prouve en jouant la résolution des deux côtés du
  backfill, pas en le disant dans un commit.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import uuid

import pytest

SUB = "usr_l6"
ORG = 1
GROUP = 3

# Le coffre TEL QU'IL EST : les cinq `entity_type` qui y vivent réellement — le
# régime courant `member` (ADR 0033), les paliers partagés `group`/`org`, la clé
# `platform` (ADR 0044 §F) et le résidu `user` des mounts OAuth (google & co), qui
# EST un credential et doit donc être nommé lui aussi.
_COFFRE = [
    ("member", f"{ORG}:{SUB}", "hunter", ""),
    ("member", f"{ORG}:{SUB}", "zoho", "jane"),   # multi-compte : 2 instances
    ("member", f"{ORG}:{SUB}", "zoho", "bureau"),
    ("group", str(GROUP), "hunter", ""),
    ("org", str(ORG), "zoho", ""),
    ("platform", "env", "hunter", ""),
    ("user", SUB, "google", "a@b.io"),                 # résidu OAuth
]


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Une base JETABLE, le VRAI `init_db()`, le vrai pool. Rend la fonction `init_db`
    pour pouvoir la REJOUER — c'est la moitié des tests d'ici."""
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_l6_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield init_db
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
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


def _seed_coffre(lignes=None):
    """Repart d'un coffre et d'un inventaire d'instances CONNUS. Le secret est un
    littéral : ce lot ne déchiffre rien, et exiger une master key ici ne prouverait
    que la crypto."""
    _exec("DELETE FROM connector_instances")
    _exec("DELETE FROM connector_credentials")
    for et, eid, connector, account in (lignes if lignes is not None else _COFFRE):
        _exec("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
              "account, secret_enc, meta) VALUES (%s, %s, %s, %s, 'x', '{}'::jsonb)",
              (et, eid, connector, account))


def _instances():
    return _rows("SELECT id, owner_type, owner_id, connector, account, label, config, "
                 "parent_id, revoked_at, created_at FROM connector_instances "
                 "ORDER BY owner_type, owner_id, connector, account")


def _quadruplets(insts):
    return [(i["owner_type"], i["owner_id"], i["connector"], i["account"]) for i in insts]


# ─── 1. La table naît au boot ────────────────────────────────────────────────

def test_la_table_nait_au_boot_avec_sa_forme(live):
    cols = {r["column_name"]: r for r in _rows(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'connector_instances'")}
    assert set(cols) == {
        "id", "connector", "owner_type", "owner_id", "account", "label", "config",
        # (`visibility` retirée par la bascule des comptes, #1499 : ce sont les règles.)
        "parent_id", "created_at", "revoked_at",
        # Le MOTIF de l'archivage (pièce 2) : sans lui, « l'utilisateur a retiré sa
        # clé » et « on a réparé une orpheline » sont le même événement.
        "revoked_reason"}, sorted(cols)
    assert cols["account"]["is_nullable"] == "NO", (
        "`account` suit la convention du coffre (`''` = mono-compte) : nullable, il "
        "rendrait l'index unique aveugle.")
    assert cols["revoked_at"]["is_nullable"] == "YES"
    assert cols["revoked_reason"]["is_nullable"] == "YES", (
        "le motif est NULLABLE et sans CHECK : son vocabulaire est fermé par le code "
        "qui écrit, pas par la base — sinon le prochain motif est une migration.")


def test_les_deux_index_sont_la_paire_partiel_et_non_partiel(live):
    defs = {r["indexname"]: r["indexdef"] for r in _rows(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'connector_instances'")}
    unique = defs["idx_connector_instances_vault"]
    assert "UNIQUE" in unique and "WHERE (revoked_at IS NULL)" in unique, unique
    jumeau = defs["idx_connector_instances_vault_all"]
    assert "UNIQUE" not in jumeau and "WHERE" not in jumeau, (
        f"le jumeau du backfill doit rester NON PARTIEL : {jumeau}. Il sert la "
        "question « existe-t-il une instance, archivée comprise ? » — celle qui "
        "empêche de ressusciter une instance retirée à la main.")
    assert "idx_connector_instances_parent" in defs


def test_l_unicite_est_tenue_par_la_base_pas_par_le_backfill(live):
    import psycopg
    _seed_coffre([("org", "1", "zoho", "")])
    live()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _exec("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
              "VALUES ('zoho', 'org', '1', '')")


def test_le_vocabulaire_de_proprietaire_refuse_vraiment_l_inconnu(live):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        _exec("INSERT INTO connector_instances (connector, owner_type, owner_id) "
              "VALUES ('zoho', 'banane', '1')")


# ─── 2. Le backfill ──────────────────────────────────────────────────────────

def test_chaque_ligne_de_coffre_recoit_exactement_une_instance(live):
    _seed_coffre()
    live()
    insts = _instances()
    assert len(insts) == len(_COFFRE)
    assert sorted(_quadruplets(insts)) == sorted(
        (et, eid, c, a) for et, eid, c, a in _COFFRE), (
        "le quadruplet de l'instance EST la clé du coffre, à l'octet près — c'est "
        "tout le lien, il n'y en a pas d'autre.")
    # Le multi-compte donne bien DEUX instances distinctes du même connecteur.
    zoho = [i for i in insts if i["connector"] == "zoho" and i["owner_type"] == "member"]
    assert sorted(i["account"] for i in zoho) == ["bureau", "jane"]
    assert len({i["id"] for i in zoho}) == 2


def test_le_backfill_ne_deplace_ni_le_nom_ni_la_config(live):
    """Il NOMME l'existant, il ne le déplace pas : `label` et `config` restent vides,
    leur domicile est encore `connector_credentials.meta` (et, pour les
    `config_fields` packés, le ciphertext lui-même). Les recopier ici ferait un
    second domicile pour une donnée que rien ne lit."""
    _seed_coffre()
    _exec("UPDATE connector_credentials SET meta = '{\"label\": \"Ma clé\"}'::jsonb "
          "WHERE connector = 'hunter' AND entity_type = 'member'")
    _exec("DELETE FROM connector_instances")
    live()
    for i in _instances():
        assert i["label"] is None and i["config"] == {}
        assert i["parent_id"] is None
        assert i["revoked_at"] is None


def test_le_backfill_ne_touche_aucune_ligne_du_coffre(live):
    """Additif au sens strict : la prod tourne l'ANCIEN code sur CETTE MÊME base."""
    _seed_coffre()
    avant = _rows("SELECT * FROM connector_credentials "
                  "ORDER BY entity_type, entity_id, connector, account")
    live()
    assert _rows("SELECT * FROM connector_credentials "
                 "ORDER BY entity_type, entity_id, connector, account") == avant


def test_rejouer_le_backfill_est_un_no_op(live):
    _seed_coffre()
    live()
    premier = _instances()
    live()
    live()
    assert _instances() == premier, (
        "rejouer doit être un no-op EXACT — mêmes lignes, mêmes ids : ce sont des "
        "ids déjà distribuables, ils ne se renumérotent pas à chaque boot.")


def test_une_instance_archivee_n_est_pas_ressuscitee(live):
    """La leçon de `db.grants.edge_exists` au lot L5 : un boot ne doit jamais rendre
    un objet qu'un humain a retiré entre deux boots. La garde est le `NOT EXISTS`
    SANS filtre sur `revoked_at` — d'où l'index non partiel."""
    _seed_coffre([("org", str(ORG), "zoho", "")])
    live()
    [inst] = _instances()
    _exec("UPDATE connector_instances SET revoked_at = NOW() WHERE id = %s", (inst["id"],))
    live()
    apres = _instances()
    assert len(apres) == 1 and apres[0]["id"] == inst["id"], (
        f"le backfill a ressuscité une instance archivée : {apres}")
    assert apres[0]["revoked_at"] is not None


def test_une_ligne_de_coffre_hors_vocabulaire_ne_fait_pas_tomber_le_boot(live, caplog):
    """Le CHECK de la table refuserait un `owner_type` inconnu — donc la transaction
    de schéma ENTIÈRE, donc le boot, sur une base partagée avec la production. Ce qui
    sort du vocabulaire est compté et journalisé, jamais inventé (même règle qu'au
    lot L5 pour les scopes hors `user:`/`org:`)."""
    import logging
    _seed_coffre([("org", str(ORG), "zoho", ""), ("licorne", "42", "zoho", "")])
    with caplog.at_level(logging.WARNING):
        live()                                   # ne lève pas
    assert _quadruplets(_instances()) == [("org", str(ORG), "zoho", "")]
    assert any("licorne" in r.getMessage() for r in caplog.records), (
        [r.getMessage() for r in caplog.records])


def test_l_inventaire_journalise_compte_les_vivantes(live):
    from oto_mcp.db import connector_instances as ci_db
    _seed_coffre()
    live()
    assert ci_db.connector_instance_counts() == {
        "group": 1, "member": 3, "org": 1, "platform": 1, "user": 1}


# ─── 3. `inst:{id}` résout vers la bonne ligne de coffre ─────────────────────

def test_inst_id_resout_vers_la_bonne_ligne_de_coffre(live):
    from oto_mcp import instance_refs
    from oto_mcp.db import connector_instances as ci_db
    _seed_coffre()
    live()
    for et, eid, connector, account in _COFFRE:
        iid = ci_db.instance_id_for_vault_row(et, eid, connector, account)
        assert iid is not None, (et, eid, connector, account)
        ref = instance_refs.make_instance_ref(iid)
        assert instance_refs.parse_ref(ref).instance_id == iid
        row = ci_db.instance_by_id(iid)
        assert (row["owner_type"], row["owner_id"], row["connector"], row["account"]) \
            == (et, eid, connector, account)


def test_la_resolution_en_lot_rend_le_meme_resultat_qu_une_par_une(live):
    """La projection en tient des dizaines : une requête, pas N allers-retours vers
    une base managée distante. Le lot doit donc rendre EXACTEMENT ce que rendraient
    les lookups unitaires — dont il est l'optimisation."""
    from oto_mcp.db import connector_instances as ci_db
    _seed_coffre()
    live()
    cles = [(et, eid, c, a) for et, eid, c, a in _COFFRE]
    en_lot = ci_db.instance_ids_for_vault_rows(cles)
    un_par_un = {k: ci_db.instance_id_for_vault_row(*k) for k in cles}
    assert en_lot == un_par_un
    assert ci_db.instance_ids_for_vault_rows([]) == {}
    assert ci_db.instance_ids_for_vault_rows([("org", "999", "zoho", "")]) == {}


def test_une_instance_archivee_ne_se_resout_plus_en_lot(live):
    from oto_mcp.db import connector_instances as ci_db
    _seed_coffre([("org", str(ORG), "zoho", "")])
    live()
    _exec("UPDATE connector_instances SET revoked_at = NOW()")
    assert ci_db.instance_ids_for_vault_rows([("org", str(ORG), "zoho", "")]) == {}
    assert ci_db.instance_id_for_vault_row("org", str(ORG), "zoho", "") is None


# ─── 4. La surface porte l'identifiant ───────────────────────────────────────

def test_la_projection_sert_l_identifiant_a_cote_du_ref(live):
    """Bout en bout, contre la vraie base : le handler de
    `GET /api/me/connector-instances` lit le coffre, résout les identifiants et sert
    les deux. `ref` n'est pas remplacé — il est déjà distribué."""
    from oto_mcp.capabilities.connectors import instances as ci
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.db import connector_instances as ci_db
    _seed_coffre()
    live()
    out = ci.ConnectorInstances.model_validate(
        ci._list_instances(ResolvedCtx(sub=SUB, org_id=ORG), ci.ListInstancesInput()))
    par_ref = {i.ref: i for i in out.instances}
    attendu = ci_db.instance_id_for_vault_row(
        "member", f"{ORG}:{SUB}", "zoho", "jane")
    inst = par_ref[f"member:{ORG}:{SUB}:zoho:jane"]
    assert inst.id == f"inst:{attendu}"
    assert all(not k.startswith("_") for i in out.instances for k in i.model_dump()), (
        "le quadruplet de coffre est un détail d'implémentation : il ne part pas sur "
        "le fil.")


def test_une_cle_posee_depuis_le_dernier_boot_n_a_pas_encore_d_identifiant(live):
    """L'instance naît au BOOT, pas à la pose : c'est le prix assumé de ne toucher
    AUCUN chemin d'écriture du coffre dans ce lot. Le client doit donc supporter
    l'absence — d'où `id` optionnel, et `ref` maintenu."""
    from oto_mcp.capabilities.connectors import instances as ci
    from oto_mcp.capabilities._types import ResolvedCtx
    _seed_coffre()
    live()
    _exec("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
          "account, secret_enc, meta) VALUES ('member', %s, 'serper', '', 'x', '{}'::jsonb)",
          (f"{ORG}:{SUB}",))
    out = ci._list_instances(ResolvedCtx(sub=SUB, org_id=ORG), ci.ListInstancesInput())
    serper = [i for i in out["instances"] if i["connector"] == "serper"]
    assert len(serper) == 1 and "id" not in serper[0] and serper[0]["ref"]


# ─── 5. LE tripwire : additif, prouvé des deux côtés du backfill ─────────────

def _empreinte_de_resolution():
    """Ce que le coffre, le registre et la cascade rendent — la seule chose que ce
    lot n'a le droit de PAS changer.

    La cascade est jouée avec la sonde de PRÉSENCE (celle du statut), sur les quatre
    barreaux et sur des connecteurs de formes différentes : `hunter` (byo + org +
    plateforme, clé ouverte), `zoho` (byo only, multi-compte), `google` (byo_user
    strict, pas org-partageable). Le `payload` est capturé, pas seulement le barreau :
    un lot qui changerait ce qu'une sonde rapporte se verrait ici."""
    from oto_mcp import access, credentials_store, providers

    empreinte = {"registre": sorted(
        (n, tuple(sorted(c.auth_modes)), bool(c.platform_key_open))
        for n, c in providers.REGISTRY.items())}
    for et, eid in (("member", f"{ORG}:{SUB}"), ("group", str(GROUP)),
                    ("org", str(ORG)), ("user", SUB), ("platform", "env")):
        empreinte[f"coffre:{et}:{eid}"] = repr(
            credentials_store.list_credentials(et, eid))
    for provider in ("hunter", "zoho", "google"):
        for org in (ORG, None):
            empreinte[f"cascade:{provider}:{org}"] = repr([
                (r.mode, r.entity_type, r.entity_id, r.account, r.via, repr(r.payload))
                for r in access.walk_cascade(SUB, provider, org=org, group=GROUP,
                                             probe=access.PRESENCE_PROBE)])
    return empreinte


def test_le_coffre_le_registre_et_la_cascade_rendent_la_meme_chose_avant_et_apres(live):
    """LE test du lot. « Additif » ne se déclare pas dans un message de commit : il se
    prouve en jouant la résolution des DEUX côtés du backfill, sur la même base.

    Avant : la table est vide, personne n'est nommé. Après : chaque ligne de coffre a
    son instance. Entre les deux, la cascade, le coffre et le registre doivent rendre
    l'octet près la même chose — sans quoi le lot aurait déplacé quelque chose au
    lieu de le nommer.
    """
    _seed_coffre()
    assert _instances() == [], "l'état AVANT doit vraiment être « rien n'est nommé »"
    avant = _empreinte_de_resolution()

    live()
    assert len(_instances()) == len(_COFFRE), "l'état APRÈS doit vraiment être peuplé"
    apres = _empreinte_de_resolution()

    ecarts = {k: (avant[k], apres[k]) for k in avant if avant[k] != apres[k]}
    assert not ecarts, (
        f"la résolution a changé en nommant les instances : {ecarts}. Le lot L6 est "
        "ADDITIF — il donne un identifiant, il ne déplace pas la résolution (c'est L7).")
    assert set(avant) == set(apres)
