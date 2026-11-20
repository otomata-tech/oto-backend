"""Upload out-of-bande de contenu volumineux (issue oto-backend#105).

Handler `oto_upload_url` (mint) + matérialisation côté réception. On monkeypatche
les seams (db/media_store/ownership) et le secret HMAC — pas de DB ni S3.
"""
import os

import pytest


from oto_mcp import upload_tokens as ut
from oto_mcp.capabilities import uploads as U
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture(autouse=True)
def _secret_d_instance(monkeypatch):
    """La clé qui signe, déclarée pour CE fichier.

    ⚠️ Elle était posée par `os.environ.setdefault` au niveau module — donc dès la
    collecte, et pour tout ce que pytest importait ensuite. Un banc qui signait sans
    déclarer de clé héritait de celle-ci et passait en suite complète tout en rougissant
    lancé seul (même motif que l'adresse publique, colmaté le 15/09/2026)."""
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")


CTX = ResolvedCtx(sub="u1", org_id=42)


@pytest.fixture
def seams(monkeypatch):
    monkeypatch.setattr(ut, "check_target_access", lambda sub, target: None)  # autz OK par défaut
    # L'URL signée n'a plus de repli : un jeton émis pour une adresse devinée serait
    # un téléversement envoyé chez quelqu'un d'autre.
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja")
    return monkeypatch


# --- mint (capacité) --------------------------------------------------------

def test_mint_doc_create_returns_signed_url(seams):
    out = U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create",
                                              project_id=5, title="Transcript"))
    assert out["method"] == "PUT" and "/api/upload/" in out["url"]
    tok = out["url"].rsplit("/", 1)[1]
    p = ut.verify(tok)
    assert p["sub"] == "u1" and p["org"] == 42
    assert p["target"] == {"kind": "doc", "op": "create", "project_id": 5,
                           "parent_id": None, "title": "Transcript", "doc_kind": "source"}


def test_mint_doc_create_requires_title(seams):
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create", project_id=5))
    assert e.value.code == "missing_title"


def test_mint_doc_update_seals_doc_id(seams):
    out = U._upload_url(CTX, U.UploadUrlInput(target="doc", op="update", doc_id=9))
    p = ut.verify(out["url"].rsplit("/", 1)[1])
    assert p["target"] == {"kind": "doc", "op": "update", "doc_id": 9}


def test_mint_project_file_requires_filename(seams):
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="project_file", project_id=5))
    assert e.value.code == "missing_filename"


def test_le_repli_d_un_fichier_n_est_jamais_en_ligne(seams):
    """Un DWG de 5 Mo ne passe pas dans un argument : sans PUT possible, le repli d'un
    fichier est `oto_import` depuis sa source ou un humain — jamais `data_write`. Seul
    un fichier PRODUIT et petit passe en ligne, borné (`file_source` `content`, #1149)."""
    out = U._upload_url(CTX, U.UploadUrlInput(target="project_file", project_id=5,
                                              filename="plan.dwg"))
    assert "oto_import" in out["hint"] and "base64" in out["hint"]
    assert "data_write" not in out["hint"]
    from oto_mcp import file_source
    assert f"up to {file_source.CONTENT_MAX_BYTES // 1024} KB" in out["hint"]


def test_le_repli_d_une_image_n_a_aucune_voie_en_ligne(seams, monkeypatch):
    """`oto_import` ne vise pas une image : son repli ne promet pas la voie en ligne."""
    from oto_mcp import media_store
    monkeypatch.setattr(media_store, "max_image_bytes", lambda: 2 * 1024 * 1024)
    out = U._upload_url(CTX, U.UploadUrlInput(target="image"))
    assert "no inline path" in out["hint"] and '{kind:"content"' not in out["hint"]


def test_le_repli_d_une_page_reste_en_ligne(seams):
    out = U._upload_url(CTX, U.UploadUrlInput(target="doc", op="update", doc_id=9))
    assert "oto_doc op=create|update|patch" in out["hint"]


def test_mint_fail_fast_on_no_write(monkeypatch):
    def deny(sub, target):
        raise ut.UploadError(403, "forbidden", "nope")
    monkeypatch.setattr(ut, "check_target_access", deny)
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create",
                                            project_id=5, title="T"))
    assert e.value.code == "forbidden" and e.value.status == 403


# --- verify (jeton) ---------------------------------------------------------

def test_verify_rejects_tamper():
    tok, _ = ut.sign("u1", 42, {"kind": "doc", "op": "update", "doc_id": 1})
    assert ut.verify(tok[:-4] + "AAAA") is None


def test_verify_rejects_expired():
    tok, _ = ut.sign("u1", 42, {"kind": "doc", "op": "update", "doc_id": 1}, ttl=-1)
    assert ut.verify(tok) is None


def test_verify_rejects_wrong_typ():
    # Un payload signé du bon secret mais typ != upload (ex. state OAuth) est refusé.
    import base64, hashlib, hmac, json
    bad = json.dumps({"typ": "oauth", "sub": "x"}).encode()
    sig = hmac.new(b"test-secret", bad, hashlib.sha256).digest()
    b64 = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    assert ut.verify(f"{b64(bad)}.{b64(sig)}") is None


# --- materialize (réception) ------------------------------------------------

def test_materialize_doc_create(monkeypatch):
    calls = {}
    import oto_mcp.db as db
    def fake_create(pid, title, **k):
        calls["create"] = (pid, title, k)
        return 77
    monkeypatch.setattr(db, "create_doc", fake_create)
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "doc", "op": "create", "project_id": 5, "parent_id": None,
              "title": "T", "doc_kind": "source"}
    res = ut.materialize("u1", target, b"# hello\nverbatim", None)
    # oto#86 : allow-list — l'accusé anonyme ne porte plus doc_id/project_id.
    assert res == {"ok": True, "kind": "doc", "op": "create", "bytes": 16, "chars": 16}
    assert calls["create"][2]["body_md"] == "# hello\nverbatim"


def test_materialize_doc_receipt_ne_fuite_aucun_identifiant_interne(monkeypatch):
    """oto#86 (suite) — trouvé par fleet : la branche `project_file` avait reçu
    l'allow-list, pas la branche `doc`, alors que c'est la MÊME route anonyme.
    `update` ET `create` doivent tenir le même verdict que `target_label`."""
    import oto_mcp.db as db
    monkeypatch.setattr(db, "update_doc", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_doc_by_id", lambda did: {"project_id": 5, "title": "T"})
    monkeypatch.setattr(db, "create_doc", lambda *a, **k: 77)
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)

    res_update = ut.materialize(
        "u1", {"kind": "doc", "op": "update", "doc_id": 991827}, b"x", None)
    assert res_update == {"ok": True, "kind": "doc", "op": "update", "bytes": 1, "chars": 1}, (
        f"l'accusé sert doc_id ou un champ de plus — oto#86 : {res_update!r}")

    res_create = ut.materialize(
        "u1", {"kind": "doc", "op": "create", "project_id": 553201,
              "title": "T", "doc_kind": "source"}, b"x", None)
    assert res_create == {"ok": True, "kind": "doc", "op": "create", "bytes": 1, "chars": 1}, (
        f"l'accusé sert doc_id/project_id ou un champ de plus — oto#86 : {res_create!r}")


def test_materialize_doc_rejects_non_utf8(monkeypatch):
    import oto_mcp.db as db
    monkeypatch.setattr(db, "update_doc", lambda *a, **k: None)
    with pytest.raises(ut.UploadError) as e:
        ut.materialize("u1", {"kind": "doc", "op": "update", "doc_id": 1},
                       b"\xff\xfe\x00binary", None)
    assert e.value.code == "not_utf8"


def test_materialize_project_file_ignores_curl_default_ct(monkeypatch):
    seen = {}
    import oto_mcp.db as db
    import oto_mcp.media_store as ms
    def fake_upload(prefix, owner, data, ctype, filename, *, max_bytes=None):
        seen["ctype"] = ctype
        return f"project-files/{owner}/deadbeef/{filename}"
    monkeypatch.setattr(ms, "upload_object", fake_upload)
    monkeypatch.setattr(db, "add_project_file",
                        lambda pid, key, filename, **k: {"id": 3, "s3_key": key, "filename": filename})
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "project_file", "project_id": 5, "filename": "r.pdf",
              "title": None, "description": None, "content_type": None}
    # curl --data-binary pose application/x-www-form-urlencoded → ne doit PAS coller au PDF.
    # Depuis #562 le type se décide sur le contenu : la signature `%PDF-` le nomme.
    body = b"%PDF-1.7 ..."
    res = ut.materialize("u1", target, body, "application/x-www-form-urlencoded")
    assert seen["ctype"] == "application/pdf"
    # oto#86 : allow-list, pas retrait — l'accusé anonyme ne porte QUE ce qui suit,
    # jamais la clé S3, jamais l'id de ligne, jamais `created_by`.
    assert res == {"ok": True, "kind": "project_file", "filename": "r.pdf", "bytes": len(body)}


def test_materialize_project_file_receipt_ne_fuite_aucun_identifiant_interne(monkeypatch):
    """oto#86 fuite 1 — l'accusé servait la ligne de base ENTIÈRE (moins la clé S3
    via un `pop()` ponctuel) : l'id de ligne, l'id de projet, `created_by` (le
    compte DÉPOSANT, forme `<tenant>:<sub>`), l'état de publication… à un TIERS
    anonyme porteur du lien — cas d'usage documenté (claude.ai sans shell transmet
    l'URL à un humain). Le tiers n'a besoin que de savoir que c'est passé."""
    import oto_mcp.db as db
    import oto_mcp.media_store as ms
    monkeypatch.setattr(ms, "upload_object", lambda *a, **k: "project-files/5/deadbeef/r.pdf")
    monkeypatch.setattr(db, "add_project_file", lambda *a, **k: {
        "id": 42, "project_id": 5, "s3_key": "project-files/5/deadbeef/r.pdf",
        "filename": "r.pdf", "mime": "application/pdf", "size_bytes": 4,
        "title": None, "description": None, "summary": None, "public": False,
        "public_url": None, "created_by": "tenant-x:sub-y",
        "created_at": "2026-09-15 00:00:00",
    })
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "project_file", "project_id": 5, "filename": "r.pdf",
              "title": None, "description": None, "content_type": "application/pdf"}
    res = ut.materialize("u1", target, b"%PDF", "application/pdf")
    assert res == {"ok": True, "kind": "project_file", "filename": "r.pdf", "bytes": 4}, (
        f"l'accusé sert un champ de plus que ok/kind/filename/bytes — oto#86 : {res!r}")


def test_parse_rows_ndjson_and_csv():
    # Depuis #684, `_parse_rows` rend AUSSI les en-têtes traduits : un en-tête de
    # tableur qui porte un point devient un nom de colonne, et le renommage doit
    # remonter jusqu'à l'appelant — une traduction silencieuse rendrait au client une
    # colonne qu'il ne peut plus retrouver par le nom qu'il lui a donné.
    rows, traduits = ut._parse_rows(b'{"a":1}\n\n{"a":2}\n', "ndjson")
    assert rows == [{"a": 1}, {"a": 2}]
    assert traduits == {}
    rows, traduits = ut._parse_rows(b"email,n\na@x,A\nb@y,B\n", "csv")
    assert rows == [{"email": "a@x", "n": "A"}, {"email": "b@y", "n": "B"}]
    assert traduits == {}, "aucun en-tête pointé ici, donc rien à signaler"


def test_parse_rows_rejects_bad_ndjson():
    with pytest.raises(ut.UploadError) as e:
        ut._parse_rows(b'{"a":1}\nnot json\n', "ndjson")
    assert e.value.code == "bad_ndjson"
    with pytest.raises(ut.UploadError) as e:
        ut._parse_rows(b'[1,2,3]\n', "ndjson")   # array, pas un objet
    assert e.value.code == "bad_ndjson"
    with pytest.raises(ut.UploadError) as e:
        ut._parse_rows(b'   \n', "ndjson")
    assert e.value.code == "empty_dataset"


def test_materialize_datastore_batch(monkeypatch):
    seen = {}
    class FakeStore:
        def _write_rows_to_ns(self, ns_id, rows, *, key, origine_override=False,
                              donnees_d_origine=False, **_):
            seen["ns_id"], seen["rows"], seen["key"] = ns_id, rows, key
            seen["donnees_d_origine"] = donnees_d_origine
            return {"inserted": 2, "updated": 0, "count": 2, "key": key, "ids": ["r1", "r2"]}

        def off_schema_report(self):
            return {"hors_schema": ["actualite_sociale"], "hors_schema_hint": "…"}
    from oto_mcp.datastore import core as ds
    monkeypatch.setattr(ds, "make_store", lambda sub: FakeStore())
    target = {"kind": "datastore", "ns_id": 7, "namespace": "boites",
              "format": "ndjson", "key": "siren"}
    res = ut.materialize("u1", target, b'{"siren":"1"}\n{"siren":"2"}', None)
    assert seen["ns_id"] == 7 and seen["key"] == "siren" and len(seen["rows"]) == 2
    # oto#140 : le drapeau vient du JETON, jamais du PUT — celui qui livre les octets
    # ne peut pas décider que son fichier est la donnée de la cliente.
    assert seen["donnees_d_origine"] is False, "absent du jeton ⟹ écriture ordinaire"

    ut.materialize("u1", {**target, "donnees_d_origine": True},
                   b'{"siren":"1"}', None)
    assert seen["donnees_d_origine"] is True, "scellé au mint ⟹ transmis au store"
    # Le récap remonte AUSSI le relevé « hors schéma » (#294) : le bulk load est le
    # geste où un format renommé passe le plus facilement inaperçu.
    assert res == {"ok": True, "kind": "datastore", "datastore": "boites",
                   "inserted": 2, "updated": 0, "count": 2, "bytes": 27,
                   "hors_schema": ["actualite_sociale"], "hors_schema_hint": "…"}


def test_mint_datastore_seals_resolved_ns_id(monkeypatch, seams):
    class FakeStore:
        def resolve_ns_id_for_write(self, ns): return 42
        def declared_key(self, ns): return "email"
    from oto_mcp.datastore import core as ds
    monkeypatch.setattr(ds, "make_store", lambda sub: FakeStore())
    monkeypatch.setattr(ut, "check_target_access", lambda sub, target: None)
    out = U._upload_url(CTX, U.UploadUrlInput(target="datastore", datastore="42"))
    p = ut.verify(out["url"].rsplit("/", 1)[1])
    # ⚠️ `origine_override` fait partie de ce que le jeton SCELLE (oto#70 lot 2) : la
    # déclaration se prend au mint, par celui qui prépare l'import, et ne peut donc pas
    # être ajoutée par qui livre les octets. L'égalité stricte est ce qui le grave — une
    # clé qui sortirait du sceau se lirait ici.
    assert p["target"] == {"kind": "datastore", "ns_id": 42, "namespace": "42",

                           "format": "ndjson", "key": "email",
                           "origine_override": False,
                           # oto#140 : scellé au mint pour la MÊME raison — le PUT ne
                           # porte aucun paramètre, donc « ce fichier EST la donnée de
                           # la cliente » se déclare par celui qui prépare l'import.
                           "donnees_d_origine": False,
                           # oto#141 : la fusion sur la clé se DEMANDE, au mint aussi.
                           "upsert": False,
                           # oto#141 : une clé NOMMÉE à la frappe désigne — absente ici.
                           "cle_passee": False}


def test_target_label():
    assert "contacts" in ut.target_label({"kind": "datastore", "namespace": "contacts", "format": "csv"})
    assert "Transcript" in ut.target_label({"kind": "doc", "op": "create", "title": "Transcript", "project_id": 5})
    assert "r.pdf" in ut.target_label({"kind": "project_file", "filename": "r.pdf", "project_id": 5})


def test_target_label_ne_porte_aucun_identifiant_interne():
    """oto#86 fuite 2 — `target_label` interpolait `doc_id`/`project_id` dans le
    libellé de la page GET, servie SANS authentification à quiconque a le lien ;
    sa propre docstring promettait pourtant « pas de secret, pas de contenu »."""
    label = ut.target_label({"kind": "doc", "op": "update", "doc_id": 991827})
    assert "991827" not in label, f"l'id interne du doc fuite — oto#86 ({label!r})"

    label = ut.target_label({"kind": "doc", "op": "create", "title": "Transcript",
                             "project_id": 553201})
    assert "553201" not in label, f"l'id de projet fuite (doc create) — oto#86 ({label!r})"

    label = ut.target_label({"kind": "project_file", "filename": "r.pdf",
                             "project_id": 553201})
    assert "553201" not in label, f"l'id de projet fuite (project_file) — oto#86 ({label!r})"


def test_materialize_project_file_prefers_declared_ct(monkeypatch):
    seen = {}
    import oto_mcp.db as db
    import oto_mcp.media_store as ms
    monkeypatch.setattr(ms, "upload_object",
                        lambda *a, **k: seen.setdefault("ctype", a[3]) or "k/x/f")
    monkeypatch.setattr(db, "add_project_file",
                        lambda *a, **k: {"id": 1, "s3_key": "k/x/f", "filename": "r.csv"})
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    target = {"kind": "project_file", "project_id": 5, "filename": "r.csv",
              "title": None, "description": None, "content_type": "text/csv"}
    res = ut.materialize("u1", target, b"a,b,c", "application/x-www-form-urlencoded")
    assert seen["ctype"] == "text/csv"
    assert res == {"ok": True, "kind": "project_file", "filename": "r.csv", "bytes": 5}


# --- le parent d'une page créée (même garde qu'`oto_doc op=create`) ------------

@pytest.fixture
def projet_5_ecrivable(monkeypatch):
    from oto_mcp import db, ownership
    pages = {7: {"project_id": 5}, 8: {"project_id": 6}}
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: {"id": pid})
    monkeypatch.setattr(db, "get_doc_by_id", lambda did: pages.get(did))
    monkeypatch.setattr(ownership, "can_access", lambda sub, t, rid, lvl: rid == "5")


def _cible_doc(parent_id):
    return {"kind": "doc", "op": "create", "project_id": 5, "parent_id": parent_id,
            "title": "T", "doc_kind": "source"}


@pytest.mark.parametrize("parent_id", [8, 404])
def test_un_parent_hors_du_projet_est_refuse(projet_5_ecrivable, parent_id):
    """Un parent d'un AUTRE projet (que l'appelant n'écrit pas), ou inexistant : refusé
    par `check_target_access` — donc au mint ET à la réception, qui l'appellent tous deux."""
    with pytest.raises(ut.UploadError) as e:
        ut.check_target_access("u1", _cible_doc(parent_id))
    assert (e.value.status, e.value.code) == (400, "bad_parent")


@pytest.mark.parametrize("parent_id", [7, None])
def test_un_parent_du_projet_ou_aucun_passe(projet_5_ecrivable, parent_id):
    assert ut.check_target_access("u1", _cible_doc(parent_id)) is None


def test_le_mint_refuse_un_parent_hors_du_projet(projet_5_ecrivable, monkeypatch):
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja")
    with pytest.raises(AuthzDenied) as e:
        U._upload_url(CTX, U.UploadUrlInput(target="doc", op="create", project_id=5,
                                            parent_id=8, title="T"))
    assert e.value.code == "bad_parent"
