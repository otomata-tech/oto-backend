"""Toute valeur chiffrée sous la clé de l'instance est dans `rechiffrement.COLONNES_CHIFFREES`.

Cette table est la liste UNIQUE de ce que `crypto` chiffre : `vault_health` compte ses
lignes par `key_ref`, et changer de clé (rechiffrer ligne à ligne) parcourt exactement
elle. Une valeur chiffrée qui n'y serait pas ne serait ni comptée ni rechiffrée : elle
deviendrait illisible le jour où la clé qui l'a écrite quitte le trousseau.

Deux portes d'entrée, deux gardes :

- le SCHÉMA : toute colonne `*_enc` de la base que monte `init_db` (tables de
  `db/schema/*` ET colonnes posées par le DDL de démarrage) y figure ;
- le CODE : tout appel qui chiffre sous la clé de l'instance (`crypto.encrypt`) vit dans
  un module de `_ECRITURES`, qui dit quelles entrées il écrit ; tout appel qui chiffre
  sous une clé DONNÉE (`encrypt_with_key`, `seal`) vit dans un module de `_HORS_TABLE`,
  qui dit pourquoi il n'est pas dans la table.
"""
from __future__ import annotations

import ast
import base64
import json
import pathlib

import pytest

from oto_mcp.export_perimetre.rechiffrement import AAD, COLONNES_CHIFFREES, compter_key_refs

RACINE = pathlib.Path(__file__).resolve().parents[2]
NOMS = {c.nom for c in COLONNES_CHIFFREES}

#: module → les entrées de `COLONNES_CHIFFREES` qu'il chiffre par `crypto.encrypt`.
_ECRITURES: dict[str, set[str]] = {
    "oto_mcp/credentials_store.py": {"connector_credentials.secret_enc",
                                     "connector_credentials.meta.session_jar_enc"},
    # reformatage des clés d'instance (palier PLATFORM), rangées dans le coffre
    "oto_mcp/cles_plateforme_format.py": {"connector_credentials.secret_enc"},
    "oto_mcp/runner_hook.py": {"runner_triggers.hook_signing_secret_enc"},
    "oto_mcp/capabilities/transcription.py": {"transcription_jobs.api_key_enc"},
    "oto_mcp/db/jetons_de_lien.py": {"docs.public_token_enc", "process_shares.token_enc"},
    "oto_mcp/youtube_upload_worker.py": {"youtube_upload_jobs.session_url_enc"},
}

#: module → pourquoi il chiffre HORS de la table : sous une clé donnée, pas la nôtre.
_HORS_TABLE: dict[str, str] = {
    "oto_mcp/export_perimetre/rechiffrement.py":
        "rechiffre les secrets d'un export sous la clé de l'instance CIBLE (le fichier "
        "d'export), jamais sous la nôtre",
    "oto_mcp/export_perimetre/objets.py":
        "scelle les objets de l'archive d'un export sous la clé de l'instance CIBLE",
    "oto_mcp/auth/flow.py":
        "scelle pour la durée d'un state le credential qu'un fournisseur renvoie au "
        "callback (WordPress), remis au front comme `code` opaque : jamais rangé, sous "
        "une clé dérivée du secret de state",
}

#: colonne `table.colonne` du schéma nommée `*_enc` sans être chiffrée par `crypto` —
#: aucune aujourd'hui ; chacune dirait pourquoi.
_COLONNES_HORS_TABLE: dict[str, str] = {}

_ECRIT_SOUS_L_INSTANCE = {"encrypt"}
_ECRIT_SOUS_UNE_CLE_DONNEE = {"encrypt_with_key", "seal"}


def _appels_qui_chiffrent(source: str) -> set[str]:
    """Les fonctions de `crypto` qui chiffrent, appelées dans ce module (par leur nom
    d'origine), quel que soit l'alias d'import (`from ..crypto import encrypt as _x`,
    `from . import crypto`)."""
    arbre = ast.parse(source)
    modules, fonctions = set(), {}
    for n in ast.walk(arbre):
        if isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[-1] == "crypto":
                fonctions.update({a.asname or a.name: a.name for a in n.names})
            else:
                modules.update(a.asname or a.name for a in n.names if a.name == "crypto")
    vus = set()
    for n in ast.walk(arbre):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in fonctions:
            vus.add(fonctions[f.id])
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in modules:
            vus.add(f.attr)
    return vus & (_ECRIT_SOUS_L_INSTANCE | _ECRIT_SOUS_UNE_CLE_DONNEE)


def _ecrivains() -> dict[str, set[str]]:
    trouves = {}
    for p in sorted((RACINE / "oto_mcp").rglob("*.py")):
        rel = p.relative_to(RACINE).as_posix()
        if rel == "oto_mcp/crypto.py":
            continue
        appels = _appels_qui_chiffrent(p.read_text(encoding="utf-8"))
        if appels:
            trouves[rel] = appels
    return trouves


# ── le code ─────────────────────────────────────────────────────────────────

def test_tout_chiffrement_sous_la_cle_de_l_instance_est_dans_la_table():
    trouves = _ecrivains()
    sous_l_instance = {m for m, a in trouves.items() if a & _ECRIT_SOUS_L_INSTANCE}
    assert sous_l_instance == set(_ECRITURES), (
        f"chiffrent sous la clé de l'instance sans être déclarés : "
        f"{sorted(sous_l_instance - set(_ECRITURES))} — ranger leur valeur dans "
        "`rechiffrement.COLONNES_CHIFFREES` et le module dans `_ECRITURES` ; "
        f"déclarés sans plus chiffrer : {sorted(set(_ECRITURES) - sous_l_instance)}")
    sous_une_cle_donnee = {m for m, a in trouves.items() if a & _ECRIT_SOUS_UNE_CLE_DONNEE}
    assert sous_une_cle_donnee == set(_HORS_TABLE), (
        f"chiffrent sous une clé donnée sans exception nommée : "
        f"{sorted(sous_une_cle_donnee - set(_HORS_TABLE))} ; exceptions périmées : "
        f"{sorted(set(_HORS_TABLE) - sous_une_cle_donnee)}")


def test_chaque_entree_de_la_table_a_son_ecrivain():
    ecrites = set().union(*_ECRITURES.values())
    assert ecrites <= NOMS, f"inconnues de la table : {sorted(ecrites - NOMS)}"
    assert NOMS <= ecrites, f"entrées que personne n'écrit : {sorted(NOMS - ecrites)}"


def test_la_garde_voit_tous_les_alias_d_import():
    """La garde est prouvée sur les formes qu'elle prétend attraper."""
    assert _appels_qui_chiffrent("from .. import crypto\ncrypto.encrypt('a', 'b')") \
        == {"encrypt"}
    assert _appels_qui_chiffrent("from ..crypto import encrypt as _e\n_e('a', 'b')") \
        == {"encrypt"}
    assert _appels_qui_chiffrent("from oto_mcp.crypto import seal\nseal(k, b'', 'a')") \
        == {"seal"}
    assert _appels_qui_chiffrent("from .. import crypto\ncrypto.decrypt('a', 'b')") == set()
    assert _appels_qui_chiffrent("def encrypt(a): ...\nencrypt(1)") == set()


def test_les_secrets_de_l_export_sont_dans_la_table():
    assert {f"{t}.{c}" for t, (c, _) in AAD.items()} <= NOMS


# ── le schéma ───────────────────────────────────────────────────────────────

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402


@pytest.fixture(scope="module")
def conn(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        yield c


def test_toute_colonne_enc_du_schema_est_dans_la_table(conn):
    from oto_mcp.export_perimetre.decouverte import lire_schema
    schema = lire_schema(conn)
    assert len(schema.colonnes) > 90, "le schéma lu n'est pas celui du démarrage"
    enc = {f"{t}.{c}" for t, cols in schema.colonnes.items() for c in cols
           if c.endswith("_enc")}
    colonnes = {f"{c.table}.{c.colonne}" for c in COLONNES_CHIFFREES if c.cle_meta is None}
    assert enc - set(_COLONNES_HORS_TABLE) == colonnes, (
        f"colonnes `*_enc` hors de la table : {sorted(enc - colonnes)} ; entrées sans "
        f"colonne : {sorted(colonnes - enc)}")
    for c in COLONNES_CHIFFREES:
        assert c.colonne in schema.colonnes[c.table], c.nom


def test_vault_health_compte_par_key_ref_sans_lire_la_valeur(conn, monkeypatch):
    from oto_mcp import credentials_store as cs, crypto
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "5" * 64)
    jar = crypto.encrypt("jar", cs.aad_satellite("user", "u1", "c", "", "session_jar"))
    autre = base64.b64encode(b"\x02" + b"\x00" * 40).decode()
    lignes = [("u1", crypto.encrypt("a", cs._aad("user", "u1", "c", "")),
               {"session_jar_enc": jar}),
              ("u2", crypto.encrypt("b", cs._aad("user", "u2", "c", "")), {}),
              ("u3", autre, {}),
              ("u4", "!pas du base64", {})]
    for eid, enc, meta in lignes:
        conn.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                     "secret_enc, meta) VALUES ('user', %s, 'c', %s, %s)",
                     (eid, enc, json.dumps(meta)))
    try:
        compte = compter_key_refs(conn)
    finally:
        conn.execute("DELETE FROM connector_credentials WHERE connector = 'c'")
    assert set(compte) == NOMS
    assert compte["connector_credentials.secret_enc"] == {"0x01": 2, "0x02": 1,
                                                          "illisible": 1}
    assert compte["connector_credentials.meta.session_jar_enc"] == {"0x01": 1}
    assert compte["runner_triggers.hook_signing_secret_enc"] == {}
