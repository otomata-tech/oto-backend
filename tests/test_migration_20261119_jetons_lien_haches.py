"""La révision `20261119_0630_jetons_lien_haches` : les jetons de lien sont stockés
hachés, comme les invitations.

Jouée sur une vraie base remise à la forme d'AVANT — jetons en clair dans
`docs.public_token` et `process_shares.token`, sans colonne de haché (une instance
cible migre avant le démarrage du code neuf), une fiche de compte et un nœud qui portent
une copie du jeton : après, chaque lien existant répond encore, par son haché, et le
jeton n'est plus rangé nulle part ; rejouée, elle ne fait rien ; le retour arrière lève.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_FICHIER = (RACINE / "oto_mcp" / "db" / "migrations" / "versions"
            / "20261119_0630_jetons_lien_haches.py")

LIEN_PAGE = "lien-page-davant-0000000"
LIEN_PROC = "lien-proc-davant-00000"


def _revision():
    spec = importlib.util.spec_from_file_location("rev_jetons_lien_haches", _FICHIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sql(dsn, sql, params=None):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql, params)


def _un(dsn, sql, params=None):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return c.execute(sql, params).fetchone()


def _colonnes(dsn, table) -> set:
    import psycopg
    with psycopg.connect(dsn) as c:
        return {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,))}


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """La forme d'avant : jetons en clair, aucune colonne de haché, et des liens émis."""
    from oto_mcp import db, org_store
    u = uuid.uuid4().hex[:8]
    sub, lecteur = f"u_{u}", f"lecteur_{u}"
    db.upsert_user(sub)
    db.upsert_user(lecteur)
    org = org_store.create_org(f"o_{u}", created_by=sub)
    org_store.add_org_member(org, sub, "org_admin")
    org_store.set_instruction("org", org, "procedure-partagee", "le corps", set_by=sub)
    instr = org_store.get_instruction("org", org, "procedure-partagee")
    projet = db.create_project("org", str(org), "Partage", created_by=sub)
    page = db.create_doc(projet, "Page publique", body_md="contenu", created_by=sub)
    _sql(pg_module_dsn, """
        DROP INDEX IF EXISTS idx_docs_public_token_hash;
        ALTER TABLE docs DROP COLUMN public_token_hash;
        ALTER TABLE docs ADD COLUMN public_token TEXT;
        CREATE UNIQUE INDEX idx_docs_public_token ON docs(public_token)
            WHERE public_token IS NOT NULL;
        DROP INDEX IF EXISTS uq_process_shares_token_hash;
        ALTER TABLE process_shares DROP COLUMN token_hash;
        ALTER TABLE process_shares ADD COLUMN token TEXT NOT NULL UNIQUE;
    """)
    _sql(pg_module_dsn, "UPDATE docs SET public_token = %s WHERE id = %s", (LIEN_PAGE, page))
    _sql(pg_module_dsn, "INSERT INTO process_shares (instruction_id, org_id, token, "
         "created_by) VALUES (%s, %s, %s, %s)", (instr["id"], org, LIEN_PROC, sub))
    profil = {"acquired_via": {"kind": "process_share", "token": LIEN_PROC, "at": "t"}}
    _sql(pg_module_dsn, "INSERT INTO user_account_profile (sub, profile) VALUES (%s, %s) "
         "ON CONFLICT (sub) DO UPDATE SET profile = EXCLUDED.profile",
         (lecteur, json.dumps(profil)))
    noeud = f"n_{u}"
    _sql(pg_module_dsn, "INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
         "VALUES (%s, 'page', 'org', %s, %s)",
         (noeud, str(org), json.dumps({"title": "x", "public_token": LIEN_PAGE})))
    return {"dsn": pg_module_dsn, "page": page, "lecteur": lecteur, "noeud": noeud,
            "instr": instr["id"]}


def test_apres_la_revision_les_liens_d_avant_repondent_par_leur_hache(base_d_avant):
    from oto_mcp import db
    from oto_mcp.db import partages_procedure as db_partages
    m, dsn = base_d_avant, base_d_avant["dsn"]
    rev = _revision()
    _sql(dsn, rev.UPGRADE)
    _sql(dsn, rev.UPGRADE)          # rejouée : rien à faire, aucune erreur

    assert "public_token" not in _colonnes(dsn, "docs")
    assert "token" not in _colonnes(dsn, "process_shares")
    assert _un(dsn, "SELECT public_token_hash FROM docs WHERE id = %s", (m["page"],)) == {
        "public_token_hash": hashlib.sha256(LIEN_PAGE.encode()).hexdigest()}

    # Le même lien, servi par le code de ce lot : il résout par le haché.
    assert db.get_doc_by_public_token(LIEN_PAGE)["title"] == "Page publique"
    assert db.get_doc_by_id(m["page"])["public"] is True
    partage = db_partages.par_jeton(LIEN_PROC)
    assert partage is not None and partage["instruction_id"] == m["instr"]

    via = _un(dsn, "SELECT profile FROM user_account_profile WHERE sub = %s",
              (m["lecteur"],))["profile"]["acquired_via"]
    assert via == {"kind": "process_share", "share_id": partage["id"], "at": "t"}
    props = _un(dsn, "SELECT props FROM nodes WHERE public_id = %s", (m["noeud"],))["props"]
    assert props == {"title": "x"}


def test_le_retour_arriere_leve():
    rev = _revision()
    with pytest.raises(RuntimeError):
        rev.downgrade()
