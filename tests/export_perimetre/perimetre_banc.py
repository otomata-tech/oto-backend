"""Le banc partagé des tests d'export et d'import de périmètre (oto-backend#1088).

Un propriétaire = un TENANT tiers, une org, deux comptes qualifiés par le tenant
(`<slug>:<m>-alice`), un espace perso, une équipe, et une ligne de chaque famille de
contenu. Chaque ligne porte le MARQUEUR `m` du propriétaire dans une valeur texte :
c'est ce qui permet de vérifier « rien d'autrui, rien d'oublié » par un second chemin,
sans passer par les règles du classement.

Ses objets de stockage (avatar, logo, fichier de projet, images citées par URL dans une
page et dans une ligne de tableau, audio) sont rendus par `semer` pour qu'un `FauxS3`
les porte : aucun vrai seau.
"""
from __future__ import annotations

import io
import json
import uuid
from contextlib import contextmanager
from urllib.parse import quote

import psycopg
import pytest

from oto_mcp import credentials_store, runner_hook, transcription_worker
from oto_mcp.crypto import encrypt_with_key

A, B = "A7d1e", "B9f3c"
SECRET = "clair-{}-{}"
BASE_SOURCE = "https://stockage-source.exemple.test"
BASE_CIBLE = "https://stockage-cible.exemple.test"
PNG = b"\x89PNG\r\n\x1a\n"  # la signature qu'`upload_image` exige d'une image publique


def url_source(cle: str) -> str:
    """L'URL qui cite `cle` sous notre base, telle que les URL stockées la portent : la
    clé encodée d'un niveau (`images/<slug>%3A<id>/…` devient `…/images/<slug>%253A<id>/…`,
    constaté sur une vraie copie)."""
    return f"{BASE_SOURCE}/{quote(cle, safe='/')}"


def url_cible(cle: str) -> str:
    """L'URL que l'import doit avoir écrite pour `cle` : seule la base a changé."""
    return f"{BASE_CIBLE}/{quote(cle, safe='/')}"


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _NoSuchKey(_ClientError):
    def __init__(self):
        super().__init__("NoSuchKey")


class FauxS3:
    """Un seau en mémoire, à l'API de boto3 (`get_object`, `put_object`, `head_object`,
    `get_object_acl`, `exceptions`) — `alterer` simule une cible qui abîme ce qu'on lui
    écrit, `entetes` pose ceux d'objets de départ (`ContentType`, `ACL`…)."""

    class exceptions:  # noqa: N801 — la forme de `client.exceptions` chez boto3
        ClientError = _ClientError
        NoSuchKey = _NoSuchKey

    def __init__(self, objets: dict[str, bytes] | None = None, alterer: bool = False,
                 entetes: dict[str, dict] | None = None):
        self.objets = {k: (v, {}, dict((entetes or {}).get(k, {})))
                       for k, v in (objets or {}).items()}
        self.alterer = alterer
        self.ecritures = 0

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objets:
            raise _NoSuchKey()
        return {"Body": io.BytesIO(self.objets[Key][0])}

    def put_object(self, Bucket, Key, Body, Metadata=None, **entetes):  # noqa: N803
        self.ecritures += 1
        self.objets[Key] = (Body[:-1] if self.alterer else Body, dict(Metadata or {}),
                            entetes)

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objets:
            raise _ClientError("404")
        donnees, meta, entetes = self.objets[Key]
        return {"ContentLength": len(donnees), "Metadata": meta,
                **{k: v for k, v in entetes.items() if k != "ACL"}}

    def get_object_acl(self, Bucket, Key):  # noqa: N803
        if Key not in self.objets:
            raise _NoSuchKey()
        grants = [{"Grantee": {"Type": "CanonicalUser", "ID": "proprietaire"},
                   "Permission": "FULL_CONTROL"}]
        if self.objets[Key][2].get("ACL") == "public-read":
            grants.append({"Grantee": {"Type": "Group",
                                       "URI": "http://acs.amazonaws.com/groups/global/AllUsers"},
                           "Permission": "READ"})
        return {"Grants": grants}


@contextmanager
def _sur(dsn: str, slug: str, nom: str):
    """Le code de l'instance du tenant `slug` (nom `nom`) branché sur la base `dsn`, le
    temps du bloc : son environnement et un pool à elle, rendu à la sortie."""
    from oto_mcp.db import _conn
    pool_avant = _conn._pool
    _conn._pool = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", dsn)
        mp.setenv("OTO_TENANT_PRIMAIRE_SLUG", slug)
        mp.setenv("OTO_BRAND_NAME", nom)
        try:
            yield mp
        finally:
            if _conn._pool is not None:
                _conn._pool.close()
            _conn._pool = pool_avant


def base_vide(pg_dsn: str) -> str:
    """Une base VIDE (aucune table) sur le serveur `pg_dsn` ; rend son DSN. À `detruire`."""
    base = "oto_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(pg_dsn, autocommit=True) as root:
        root.execute(f'CREATE DATABASE "{base}"')
    return pg_dsn.rsplit("/", 1)[0] + "/" + base


def naitre(pg_dsn: str, slug: str, nom: str) -> str:
    """Une base NEUVE née par le geste de l'opérateur (`oto-mcp perimetre naitre`), pour
    l'instance du tenant `slug` (nom `nom`), où l'app n'a jamais démarré ; rend son DSN.
    À `detruire`."""
    from psycopg.rows import dict_row

    from oto_mcp.export_perimetre.naissance import naitre as naitre_schema
    dsn = base_vide(pg_dsn)
    with _sur(dsn, slug, nom), psycopg.connect(dsn, row_factory=dict_row) as c:
        naitre_schema(c)
    return dsn


def demarrer(dsn: str, slug: str, nom: str) -> None:
    """Le PREMIER démarrage de l'app sur la base `dsn` : la préparation de la base du
    boot (`server._prepare_database`), telle quelle — elle y sème les lignes de l'app."""
    from oto_mcp import server
    with _sur(dsn, slug, nom) as mp:
        mp.setattr(server, "_PREPARED", False)
        server._prepare_database()


def detruire(pg_dsn: str, dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as root:
        root.execute(f'DROP DATABASE IF EXISTS "{dsn.rsplit("/", 1)[1]}" WITH (FORCE)')


def slug_de(m: str) -> str:
    return f"t{m.lower()}"


def tenant(c, slug: str, nom: str) -> int:
    return c.execute("INSERT INTO tenants (slug, name) VALUES (%s, %s) RETURNING id",
                     (slug, nom)).fetchone()["id"]


def org(c, nom: str, tenant_id: int, auto_created: bool = False) -> int:
    """`auto_created` : l'org que l'inscription pose (l'ex-org perso, plus d'étiquette
    `personal_of` depuis le 05/11/2026) — personnelle tant que son compte y est seul."""
    return c.execute("INSERT INTO orgs (name, auto_created, tenant_id) VALUES (%s, %s, %s) "
                     "RETURNING id", (nom, auto_created, tenant_id)).fetchone()["id"]


def membre(c, org_id: int, sub: str) -> None:
    c.execute("INSERT INTO users (sub, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
              (sub, f"{sub.replace(':', '.')}@exemple.test"))
    c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'admin')",
              (org_id, sub))


def _credential(c, cle: bytes, entity_type: str, entity_id: str, connector: str,
                m: str) -> None:
    aad = credentials_store._aad(entity_type, entity_id, connector, "")
    c.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
              "account, secret_enc) VALUES (%s, %s, %s, '', %s)",
              (entity_type, entity_id, connector,
               encrypt_with_key(cle, SECRET.format(m, connector), aad)))


def semer(c, m: str, *, cle: bytes | None = None) -> dict:
    """Sème le propriétaire `m` ; avec `cle`, ses trois sortes de secrets en plus."""
    slug = slug_de(m)
    tid = tenant(c, slug, f"tenant {m}")
    o = org(c, f"org {m}", tid)
    alice, bob = f"{slug}:{m}-alice", f"{slug}:{m}-bob"
    membre(c, o, alice)
    membre(c, o, bob)
    # Des clés et des octets d'écriture native : l'import repose les en-têtes d'après le
    # préfixe et, pour une image publique, d'après sa signature (`entetes_d_objet`).
    objets = {f"avatars/{quote(alice, safe='')}/{m}.png": PNG + f"avatar {m}".encode(),
              f"org-logos/{o}/{m}.png": PNG + f"logo {m}".encode(),
              f"project-files/{m}/abc/f.txt": f"fichier {m}".encode(),
              f"images/{quote(alice, safe='')}/{m}-page.png": PNG + f"image page {m}".encode(),
              f"images/{quote(alice, safe='')}/{m}-ligne.png":
                  PNG + f"image ligne {m}".encode()}
    url = {cle: url_source(cle) for cle in objets}
    cles = list(objets)
    c.execute("UPDATE users SET avatar_url = %s WHERE sub = %s", (url[cles[0]], alice))
    c.execute("UPDATE orgs SET logo_url = %s WHERE id = %s", (url[cles[1]], o))
    membre(c, org(c, f"perso {m}", tid, auto_created=True), alice)
    c.execute("INSERT INTO tenant_admins (slug, sub, granted_by) VALUES (%s, %s, %s)",
              (slug, alice, f"admin {m}"))
    c.execute("INSERT INTO tenant_legal_docs (tenant_slug, doc_slug, version, label, url) "
              "VALUES (%s, 'cgu', '1', %s, 'https://exemple.test/cgu')", (slug, f"CGU {m}"))
    c.execute("INSERT INTO connector_availability (scope_type, scope_id, connector, enabled, "
              "set_by) VALUES ('tenant', %s, 'serper', false, %s)", (slug, f"coupure {m}"))
    groupe = c.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, %s) RETURNING id",
                       (o, f"équipe {m}")).fetchone()["id"]
    c.execute("INSERT INTO org_group_members (group_id, sub, group_role) "
              "VALUES (%s, %s, 'member')", (groupe, bob))
    projet = c.execute("INSERT INTO projects (owner_type, owner_id, name) "
                       "VALUES ('org', %s, %s) RETURNING id", (str(o), f"projet {m}")
                       ).fetchone()["id"]
    c.execute("INSERT INTO projects (owner_type, owner_id, name) VALUES ('user', %s, %s)",
              (alice, f"projet perso {m}"))
    page = c.execute("INSERT INTO docs (project_id, title, body_md) VALUES (%s, %s, %s) "
                     "RETURNING id",
                     (projet, f"page {m}", f"corps {m}\n\n![schéma]({url[cles[3]]})")
                     ).fetchone()["id"]
    c.execute("INSERT INTO docs (project_id, parent_id, title, body_md) "
              "VALUES (%s, %s, %s, '')", (projet, page, f"sous-page {m}"))
    c.execute("INSERT INTO doc_revisions (doc_id, title, body_md) VALUES (%s, %s, %s)",
              (page, f"page {m}", f"v1 {m}"))
    c.execute("INSERT INTO project_files (project_id, s3_key, filename, mime, size_bytes, "
              "public, public_url) VALUES (%s, %s, %s, 'text/plain', 3, true, %s)",
              (projet, cles[2], f"f {m}", url[cles[2]]))
    c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
              "principal_id, granted_by) VALUES ('project', %s, 'group', %s, %s)",
              (str(projet), str(groupe), alice))
    tableau = c.execute("INSERT INTO user_datastores (owner_type, owner_id, namespace) "
                        "VALUES ('org', %s, %s) RETURNING id", (str(o), f"tableau_{m}")
                        ).fetchone()["id"]
    for i in (1, 2):
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                  (tableau, f"{m}-{i}", json.dumps({"nom": f"ligne {m}", "par": bob,
                                                     "image": url[cles[4]]})))
    noeud = c.execute("INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
                      "VALUES (%s, 'page', 'org', %s, %s) RETURNING id",
                      (f"n-{m}", str(o), json.dumps({"titre": m}))).fetchone()["id"]
    c.execute("INSERT INTO blocks (public_id, node_id, position, type, props) "
              "VALUES (%s, %s, 1, 'paragraph', %s)", (f"b-{m}", noeud, json.dumps({"t": m})))
    c.execute("INSERT INTO org_instructions (org_id, owner_type, owner_id, slug, body_md) "
              "VALUES (%s, 'org', %s, %s, %s)", (o, str(o), f"proc-{m}", f"fais {m}"))
    c.execute("INSERT INTO runs (run_id, sub, org_id, label) VALUES (%s, %s, %s, %s)",
              (f"run-{m}", alice, o, f"run {m}"))
    c.execute("INSERT INTO run_messages (run_id, seq, role, content) "
              "VALUES (%s, 1, 'user', %s)", (f"run-{m}", json.dumps({"texte": m})))
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id, args) "
              "VALUES ('oto', 'tool', %s, 'oto_doc', %s, %s)",
              (alice, o, json.dumps({"_m": m, "pour": alice})))
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool) "
              "VALUES ('oto', 'tool', %s, 'oto_whoami')", (bob,))
    c.execute("INSERT INTO usage (sub, tool, day, count) VALUES (%s, 'oto_doc', "
              "CURRENT_DATE, 3)", (bob,))
    c.execute("INSERT INTO billing_contracts (org_id, seats, unit_amount, reference, "
              "starts_at) VALUES (%s, 1, 100, %s, NOW())", (o, f"contrat {m}"))
    c.execute("INSERT INTO org_entitlements (sub, right_key, value, source) "
              "VALUES (%s, 'essai', 1, %s)", (alice, f"commerce {m}"))
    if cle is not None:
        _credential(c, cle, "org", str(o), "serper", m)
        _credential(c, cle, "member", f"{o}:{alice}", "apollo", m)
        _credential(c, cle, "user", alice, "tavily", m)
        # Une clé TENANT d'un connecteur qui sert le barreau plateforme : le tenant
        # devient le primaire de la cible, elle y devient une instance plateforme.
        _credential(c, cle, "tenant", slug, "hunter", m)
        declencheur = c.execute(
            "INSERT INTO runner_triggers (org_id, sub, label, procedure, kind, tools) "
            "VALUES (%s, %s, %s, 'p', 'webhook', '[]') RETURNING id", (o, alice, f"hook {m}")
        ).fetchone()["id"]
        c.execute("UPDATE runner_triggers SET hook_signing_secret_enc = %s WHERE id = %s",
                  (encrypt_with_key(cle, SECRET.format(m, "hook"),
                                    runner_hook._aad_du_secret(declencheur)), declencheur))
        audio = f"transcription-jobs/{m}/abc/a.mp3"
        objets[audio] = f"audio {m}".encode()
        c.execute("INSERT INTO transcription_jobs (project_id, sub, status, audio_key, "
                  "filename, mime, api_key_enc) VALUES (%s, %s, 'queued', %s, %s, "
                  "'audio/mpeg', %s)",
                  (projet, alice, audio, f"a {m}.mp3",
                   encrypt_with_key(cle, SECRET.format(m, "transcription"),
                                    transcription_worker._aad(audio))))
    return {"org": o, "tenant": tid, "slug": slug, "projet": projet, "page": page,
            "alice": alice, "bob": bob, "objets": objets}
