"""Les clés `tenant` du tenant devenu primaire, rangées en instances plateforme (import).

Une base née par `oto-mcp perimetre naitre` pour l'instance du tenant `SLUG`, où l'on
pose à la main ce que l'import y verse : des lignes `tenant` chiffrées sous l'AAD tenant,
leurs instances, des arêtes tenant→org. Chaque test joue dans une transaction annulée à
la sortie : la base reste celle de sa naissance.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp import credentials_store as cs  # noqa: E402
from oto_mcp import grants_chain, instance_refs  # noqa: E402
from oto_mcp.crypto import decrypt_with_key, encrypt_with_key  # noqa: E402
from oto_mcp.db import connector_instances  # noqa: E402
from oto_mcp.db import grants as db_grants  # noqa: E402
from oto_mcp.export_perimetre.cles_du_primaire import ConversionRefusee, convertir  # noqa: E402
from perimetre_banc import detruire, naitre  # noqa: E402

SLUG, NOM = "tprimaire", "tenant primaire"
CLE = os.urandom(32)


@pytest.fixture(scope="module")
def base(pg_dsn):
    dsn = naitre(pg_dsn, SLUG, NOM)
    try:
        yield dsn
    finally:
        detruire(pg_dsn, dsn)


@contextmanager
def _annulee(dsn: str):
    """Une connexion dont tout ce qu'elle écrit est annulé à la sortie, sous la clé `CLE`."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OTO_MCP_MASTER_KEY", CLE.hex())
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            try:
                yield c
            finally:
                c.rollback()


def _cle_tenant(c, connecteur: str, compte: str = "", secret: str = "s3cret",
                slug: str = SLUG) -> None:
    c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, account, "
              "secret_enc, meta, set_by) VALUES ('tenant', %s, %s, %s, %s, %s, 'admin')",
              (slug, connecteur, compte,
               encrypt_with_key(CLE, secret, cs._aad("tenant", slug, connecteur, compte)),
               '{"note": "garde"}'))
    connector_instances.name_vault_row(c, "tenant", slug, connecteur, compte)


def _arete(c, connecteur: str, org: int, contraintes: dict | None = None) -> int:
    return db_grants.insert_grant(
        resource_id=grants_chain.tenant_ref(SLUG, connecteur), grantor_kind="tenant",
        grantor_id=SLUG, grantee_kind="org", grantee_id=str(org),
        constraints=contraintes or {}, conn=c)


def _ligne(c, et: str, eid: str, connecteur: str, compte: str = ""):
    return c.execute("SELECT * FROM connector_credentials WHERE entity_type = %s AND "
                     "entity_id = %s AND connector = %s AND account = %s",
                     (et, eid, connecteur, compte)).fetchone()


def test_une_cle_sans_compte_devient_l_instance_plateforme_ouverte_du_slug(base):
    with _annulee(base) as c:
        _cle_tenant(c, "hunter")
        assert convertir(c, SLUG) == {"hunter": SLUG}
        assert _ligne(c, "tenant", SLUG, "hunter") is None
        p = _ligne(c, "platform", SLUG, "hunter")
        assert (p["share_mode"], p["share_down"]) == ("open", [])
        assert p["set_by"] == "admin" and p["meta"]["note"] == "garde"
        assert p["meta"]["_conversion"]["from"] == f"tenant:{SLUG}"
        assert decrypt_with_key(CLE, p["secret_enc"], cs._aad("platform", SLUG, "hunter")) \
            == "s3cret"
        with pytest.raises(RuntimeError):
            decrypt_with_key(CLE, p["secret_enc"], cs._aad("tenant", SLUG, "hunter"))
        assert connector_instances.instance_id_for_vault_row(
            "platform", SLUG, "hunter", conn=c) is not None
        assert connector_instances.instance_id_for_vault_row(
            "tenant", SLUG, "hunter", conn=c) is None


def test_une_cle_avec_compte_prend_son_compte_pour_label(base):
    with _annulee(base) as c:
        _cle_tenant(c, "serper", compte="Main")
        assert convertir(c, SLUG) == {"serper": "Main"}
        p = _ligne(c, "platform", "Main", "serper")
        assert decrypt_with_key(CLE, p["secret_enc"], cs._aad("platform", "Main", "serper")) \
            == "s3cret"
        assert _ligne(c, "tenant", SLUG, "serper", "Main") is None


def test_tout_le_monde_est_accorde_et_les_aretes_sans_contrainte_s_archivent(base):
    with _annulee(base) as c:
        _cle_tenant(c, "hunter")
        avant = _arete(c, "hunter", 12)
        convertir(c, SLUG)
        assert c.execute("SELECT revoked_at FROM grants WHERE id = %s",
                         (avant,)).fetchone()["revoked_at"] is not None
        tous = c.execute(
            "SELECT grantor_kind, grantor_id, revoked_at FROM grants WHERE resource_id = %s "
            "AND grantee_kind = %s AND grantee_id = %s",
            (grants_chain.instance_ref(SLUG, "hunter"), *grants_chain.EVERYONE)).fetchall()
        assert [(e["grantor_kind"], e["grantor_id"], e["revoked_at"]) for e in tous] == \
            [(*grants_chain.PLATFORM_SCOPE, None)]


@pytest.mark.parametrize("pose, motif", [
    (lambda c: _arete(c, "hunter", 12, {"quota": 100}), "AVEC contrainte"),
    (lambda c: c.execute("UPDATE connector_credentials SET share_down = '[\"org:3\"]' "
                         "WHERE entity_type = 'tenant'"), "porte un partage"),
    (lambda c: _cle_tenant(c, "attio"), "pas de mode `platform`"),
    (lambda c: _cle_tenant(c, "hunter", compte="Second"), "plusieurs clés tenant"),
    (lambda c: cs.set_credential("platform", "ancienne", "hunter", "x", conn=c),
     "instance plateforme déjà là"),
])
def test_un_refus_nomme_son_motif_et_n_ecrit_rien(base, pose, motif):
    with _annulee(base) as c:
        _cle_tenant(c, "hunter")
        pose(c)
        avant = c.execute("SELECT entity_type, entity_id, connector, account, version "
                          "FROM connector_credentials ORDER BY 1, 2, 3, 4").fetchall()
        with pytest.raises(ConversionRefusee, match=motif):
            convertir(c, SLUG)
        assert c.execute("SELECT entity_type, entity_id, connector, account, version "
                         "FROM connector_credentials ORDER BY 1, 2, 3, 4").fetchall() == avant


def test_rejouee_la_conversion_ne_change_rien(base):
    with _annulee(base) as c:
        _cle_tenant(c, "hunter")
        convertir(c, SLUG)
        etat = c.execute("SELECT entity_type, entity_id, connector, version "
                         "FROM connector_credentials ORDER BY 1, 2, 3").fetchall()
        aretes = c.execute("SELECT count(*) AS n FROM grants").fetchone()["n"]
        assert convertir(c, SLUG) == {}
        assert c.execute("SELECT entity_type, entity_id, connector, version "
                         "FROM connector_credentials ORDER BY 1, 2, 3").fetchall() == etat
        assert c.execute("SELECT count(*) AS n FROM grants").fetchone()["n"] == aretes


def test_un_tenant_qui_n_est_pas_le_primaire_refuse_sans_rien_toucher(base):
    with _annulee(base) as c:
        _cle_tenant(c, "hunter", slug="tiers")
        with pytest.raises(ConversionRefusee, match="pas le tenant primaire"):
            convertir(c, "tiers")
        assert _ligne(c, "tenant", "tiers", "hunter") is not None
        assert c.execute("SELECT count(*) AS n FROM connector_credentials "
                         "WHERE entity_type = 'platform'").fetchone()["n"] == 0


def test_la_ref_d_une_cle_a_compte_est_aussi_archivee(base):
    """L'arête peut désigner la ligne par son ref complet (avec le compte)."""
    with _annulee(base) as c:
        _cle_tenant(c, "serper", compte="Main")
        arete = db_grants.insert_grant(
            resource_id=instance_refs.make_tenant_ref(SLUG, "serper", "Main"),
            grantor_kind="tenant", grantor_id=SLUG, grantee_kind="org", grantee_id="7",
            conn=c)
        convertir(c, SLUG)
        assert c.execute("SELECT revoked_at FROM grants WHERE id = %s",
                         (arete,)).fetchone()["revoked_at"] is not None
