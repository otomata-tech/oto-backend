"""Gros fichiers de projet déposés par parties, directement au stockage (`large_uploads`).

Sur le VRAI `media_store` (seul le client S3 est faux, il tient l'état multipart) :
- l'ouverture scelle la clé et l'`UploadId` dans le jeton, le client n'en fournit aucun ;
- une partie se demande par son numéro, bornée à 1 000 et au plafond ;
- la clôture LISTE les parties côté serveur : trou, taille de partie, plafond refusés
  et réparables avec le même lien ; puis usage unique ;
- un type passif déclaré dont le début est une page web : objet retiré, refus nommé ;
- l'identité et l'écriture du projet sont rejugées à chaque étape ;
- la capacité rend le mode d'emploi, la page porte la taille des parties, la CSP
  autorise son script et l'origine du stockage ;
- l'extraction de texte ne télécharge pas un fichier trop gros pour elle.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from oto_mcp import large_uploads, media_store, upload_tokens

MIB = 1024 * 1024


class _S3:
    """Faux client S3 multipart : objets posés, dépôts ouverts et leurs parties."""

    def __init__(self):
        self.objets: dict[str, dict] = {}
        self.depots: dict[str, dict] = {}
        self.supprimes: list[str] = []

    def create_multipart_upload(self, *, Bucket, Key, **entetes):
        uid = f"up-{len(self.depots) + 1}"
        self.depots[uid] = {"key": Key, "parts": {}, "entetes": entetes}
        return {"UploadId": uid}

    def generate_presigned_url(self, op, *, Params, ExpiresIn):
        return (f"https://s3.exemple/{Params['Key']}?op={op}"
                f"&n={Params.get('PartNumber')}&X-Amz-Expires={ExpiresIn}")

    def poser_partie(self, uid, n, data):
        self.depots[uid]["parts"][n] = data

    def list_parts(self, *, Bucket, Key, UploadId, PartNumberMarker, MaxParts):
        if UploadId not in self.depots:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")
        parts = sorted(self.depots[UploadId]["parts"].items())
        parts = [(n, d) for n, d in parts if n > PartNumberMarker]
        page, reste = parts[:MaxParts], parts[MaxParts:]
        return {"Parts": [{"PartNumber": n, "ETag": f'"e{n}"', "Size": len(d)}
                          for n, d in page],
                "IsTruncated": bool(reste),
                "NextPartNumberMarker": page[-1][0] if page else 0}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        d = self.depots.pop(UploadId)
        corps = b"".join(d["parts"][p["PartNumber"]] for p in MultipartUpload["Parts"])
        self.objets[Key] = {"Body": corps, **d["entetes"]}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.depots.pop(UploadId, None)

    def get_object(self, *, Bucket, Key, Range=None):
        corps = self.objets[Key]["Body"]
        if Range:
            debut, fin = Range.removeprefix("bytes=").split("-")
            corps = corps[int(debut):int(fin) + 1]
        return {"Body": io.BytesIO(corps)}

    def delete_object(self, *, Bucket, Key):
        self.supprimes.append(Key)
        self.objets.pop(Key, None)


@pytest.fixture
def s3(monkeypatch):
    faux = _S3()
    monkeypatch.setattr(media_store, "_get_client", lambda: faux)
    monkeypatch.setattr(media_store, "_bucket", lambda: "media-test")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-test")
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_PART_BYTES", str(5 * MIB))
    return faux


@pytest.fixture
def monde(monkeypatch, s3):
    """La base et les gardes, faites à la main : on observe ce que la clôture écrit."""
    etat = {"consommes": set(), "fichiers": [], "refus": None, "acces": True}

    def consommer(jti):
        if jti in etat["consommes"]:
            return False
        etat["consommes"].add(jti)
        return True

    def ajouter(pid, key, filename, **kw):
        row = {"id": 41, "project_id": pid, "s3_key": key, "filename": filename, **kw}
        etat["fichiers"].append(row)
        return row

    def acces(sub, target):
        if not etat["acces"]:
            raise upload_tokens.UploadError(403, "forbidden", "Écriture refusée.")

    monkeypatch.setattr("oto_mcp.db.consume_upload_token", consommer)
    monkeypatch.setattr("oto_mcp.db.add_project_file", ajouter)
    monkeypatch.setattr("oto_mcp.db.log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr("oto_mcp.garde_identite.refus", lambda sub: etat["refus"])
    monkeypatch.setattr(upload_tokens, "check_target_access", acces)
    return etat


def _cible(**kw):
    return {"kind": "project_file", "project_id": 7, "filename": "film.mp4",
            "title": None, "description": None, "content_type": None, **kw}


def _ouvrir(**kw):
    jeton, _exp, scelle = large_uploads.ouvrir("sub-a", 3, _cible(**kw))
    return large_uploads.verifier(jeton), scelle


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100


def test_ouverture_scelle_la_cle_et_le_depot(s3, monde):
    payload, scelle = _ouvrir()
    t = payload["target"]
    assert t["key"].startswith("project-files/7/") and t["key"].endswith("/film.mp4")
    assert t["upload_id"] in s3.depots and t["kind"] == "project_file_large"
    assert t["served_type"] == "video/mp4"           # déduit du nom
    assert s3.depots[t["upload_id"]]["entetes"] == {"ContentType": "video/mp4"}
    assert t["part_size"] == 5 * MIB
    # un jeton d'upload ordinaire ne s'ouvre pas ici, ni l'inverse
    assert upload_tokens.verify(large_uploads.upload_tokens.sign(
        "sub-a", 3, t, typ="upload_large")[0]) is None


def test_type_actif_declare_est_neutre_et_telecharge(s3, monde):
    payload, _ = _ouvrir(filename="page.html", content_type="text/html")
    t = payload["target"]
    assert t["served_type"] == "application/octet-stream"
    entetes = s3.depots[t["upload_id"]]["entetes"]
    assert entetes["ContentDisposition"].startswith("attachment;")


def test_parcours_complet_deux_parties(s3, monde):
    payload, _ = _ouvrir()
    t = payload["target"]
    url = large_uploads.url_de_partie(payload, 2)
    assert "op=upload_part" in url and "n=2" in url
    s3.poser_partie(t["upload_id"], 1, MP4 + b"\x00" * (5 * MIB - len(MP4)))
    s3.poser_partie(t["upload_id"], 2, b"fin")
    avant = datetime.now(timezone.utc)
    accuse = large_uploads.terminer(payload)
    echeance = accuse.pop("expires_at")
    assert "14 days" in accuse.pop("notice")
    assert accuse == {"ok": True, "kind": "project_file", "filename": "film.mp4",
                      "bytes": 5 * MIB + 3, "parts": 2}
    assert s3.objets[t["key"]]["Body"][:len(MP4)] == MP4
    (row,) = monde["fichiers"]
    assert row["size_bytes"] == 5 * MIB + 3 and row["mime"] == "video/mp4"
    # Rangé avec son échéance : 14 jours après le dépôt, dite dans l'accusé.
    assert timedelta(days=14) - timedelta(minutes=1) < row["expires_at"] - avant \
        < timedelta(days=14, minutes=1)
    assert datetime.fromisoformat(echeance) == row["expires_at"].replace(microsecond=0)
    # usage unique : une seconde clôture est refusée
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "token_already_used"


def test_partie_manquante_se_reprend_avec_le_meme_lien(s3, monde):
    payload, _ = _ouvrir()
    uid = payload["target"]["upload_id"]
    s3.poser_partie(uid, 1, MP4 + b"\x00" * (5 * MIB - len(MP4)))
    s3.poser_partie(uid, 3, b"x")
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "missing_parts" and "[2]" in e.value.message
    assert not monde["consommes"]                    # le lien sert encore
    s3.poser_partie(uid, 2, b"\x00" * (5 * MIB))
    assert large_uploads.terminer(payload)["parts"] == 3


def test_partie_de_mauvaise_taille_refusee(s3, monde):
    payload, _ = _ouvrir()
    uid = payload["target"]["upload_id"]
    s3.poser_partie(uid, 1, MP4)
    s3.poser_partie(uid, 2, b"x")
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "bad_part_size"


def test_rien_recu(s3, monde):
    payload, _ = _ouvrir()
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "empty_body"


def test_plafond_tenu_aux_parties_et_a_la_cloture(s3, monde, monkeypatch):
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_MAX_BYTES", str(8 * MIB))
    payload, _ = _ouvrir()
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.url_de_partie(payload, 3)       # commencerait à 10 Mio
    assert e.value.code == "content_too_large"
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.url_de_partie(payload, 0)
    assert e.value.code == "bad_part_number"
    uid = payload["target"]["upload_id"]
    s3.poser_partie(uid, 1, MP4 + b"\x00" * (5 * MIB - len(MP4)))
    s3.poser_partie(uid, 2, b"\x00" * (4 * MIB))
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "content_too_large"


def test_debut_actif_sous_un_type_passif_est_retire(s3, monde):
    payload, _ = _ouvrir()
    t = payload["target"]
    s3.poser_partie(t["upload_id"], 1, b"<!doctype html><script>alert(1)</script>")
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "active_content"
    assert t["key"] in s3.supprimes and not monde["fichiers"]


def test_acces_rejuge_a_chaque_etape(s3, monde):
    payload, _ = _ouvrir()
    monde["acces"] = False
    for geste in (lambda: large_uploads.url_de_partie(payload, 1),
                  lambda: large_uploads.terminer(payload),
                  lambda: large_uploads.abandonner(payload)):
        with pytest.raises(upload_tokens.UploadError) as e:
            geste()
        assert e.value.code == "forbidden"
    monde["acces"], monde["refus"] = True, ("account_paused", "Compte en pause.")
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.url_de_partie(payload, 1)
    assert e.value.status == 403 and e.value.code == "account_paused"


def test_abandon_libere_le_depot(s3, monde):
    payload, _ = _ouvrir()
    uid = payload["target"]["upload_id"]
    assert large_uploads.abandonner(payload)["aborted"] is True
    assert uid not in s3.depots
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "token_already_used"


def test_liste_des_parties_paginee(s3):
    uid = media_store.ouvrir_multipart("project-files/7/x/f.bin", "video/mp4", None)
    for n in range(1, 1001):
        s3.poser_partie(uid, n, b"a")
    s3_list = s3.list_parts
    s3.list_parts = lambda **kw: s3_list(**{**kw, "MaxParts": 300})
    parties = media_store.parties_recues("project-files/7/x/f.bin", uid)
    assert [p["n"] for p in parties] == list(range(1, 1001))


def test_bornes_des_reglages(monkeypatch):
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_PART_BYTES", str(1024))
    assert large_uploads.part_size() == 5 * MIB          # minimum du stockage
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_PART_BYTES", str(100 * MIB + 7))
    assert large_uploads.part_size() == 100 * MIB         # en Mio entiers
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_MAX_BYTES", str(10 ** 15))
    assert large_uploads.max_bytes() == 1000 * 100 * MIB  # 1 000 parties au plus
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_TTL_S", str(10 ** 6))
    assert large_uploads.ttl() == 86400


def test_capacite_rend_le_mode_d_emploi(s3, monde, monkeypatch):
    from oto_mcp.capabilities import uploads as cap
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setattr("oto_mcp.config.public_base_url", lambda: "https://mcp.exemple")

    class Ctx:
        sub, org_id = "sub-a", 3

    out = cap._upload_url(Ctx(), cap.UploadUrlInput(
        target="project_file", project_id=7, filename="film.mp4", large=True))
    assert out["url"].startswith("https://mcp.exemple/api/upload/large/")
    assert out["part_url"] == out["url"] + "/part/{n}"
    assert out["complete_url"] == out["url"] + "/complete"
    assert out["part_size"] == 5 * MIB and out["max_parts"] == 1000
    assert "key" not in out["target"] and "upload_id" not in out["target"]
    assert "dd if=" in out["hint"] and "count=5" in out["hint"]
    cap.UploadUrlOutput(**out)                            # le contrat publié tient
    with pytest.raises(AuthzDenied) as e:
        cap._upload_url(Ctx(), cap.UploadUrlInput(
            target="doc", project_id=7, title="x", large=True))
    assert e.value.code == "large_needs_project_file"


def test_page_et_csp(monkeypatch):
    from oto_mcp.api.upload_page import SCRIPT_UPLOAD_LARGE, upload_page_html
    from oto_mcp.entetes_securite import csp_upload_large, empreinte_script
    page = upload_page_html("file “film.mp4”", "en", part_size=5 * MIB)
    assert f'data-ps="{5 * MIB}"' in page and f"<script>{SCRIPT_UPLOAD_LARGE}</script>" in page
    csp = csp_upload_large(SCRIPT_UPLOAD_LARGE, ["https://s3.fr-par.scw.cloud"])
    assert empreinte_script(SCRIPT_UPLOAD_LARGE) in csp
    assert "connect-src 'self' https://s3.fr-par.scw.cloud" in csp
    # la page ordinaire n'a pas changé
    assert "data-ps" not in upload_page_html("x", "en")


def test_origines_de_depot(monkeypatch):
    monkeypatch.setenv("OTO_MCP_S3_ENDPOINT", "https://s3.fr-par.scw.cloud")
    monkeypatch.setenv("OTO_MCP_S3_BUCKET", "media-test")
    monkeypatch.delenv("OTO_MCP_S3_PUBLIC_BASE_URL", raising=False)
    assert media_store.origines_de_depot() == [
        "https://media-test.s3.fr-par.scw.cloud", "https://s3.fr-par.scw.cloud"]


def test_extraction_ne_telecharge_pas_un_gros_fichier(monkeypatch):
    from oto_mcp import file_extract, file_extract_worker
    ecrits = []
    monkeypatch.setattr("oto_mcp.db.save_extracted_text",
                        lambda fid, **kw: ecrits.append((fid, kw)))
    monkeypatch.setattr(media_store, "fetch_object",
                        lambda *a, **k: pytest.fail("ne doit pas télécharger"))
    statut = file_extract_worker._extract_one({"id": 5, "s3_key": "k", "filename": "f.mp4",
                                            "mime": "video/mp4",
                                            "size_bytes": 3 * 1024 ** 3})
    assert statut == file_extract.UNSUPPORTED
    assert ecrits == [(5, {"status": file_extract.UNSUPPORTED,
                           "detail": "too large to read"})]


def test_cloture_reprise_si_la_base_tombe_juste_apres_la_fermeture(s3, monde, monkeypatch):
    payload, _ = _ouvrir()
    t = payload["target"]
    s3.poser_partie(t["upload_id"], 1, MP4)
    consommer = __import__("oto_mcp.db", fromlist=["x"]).consume_upload_token

    def base_tombe(jti):
        raise RuntimeError("base injoignable")

    monkeypatch.setattr("oto_mcp.db.consume_upload_token", base_tombe)
    with pytest.raises(RuntimeError):
        large_uploads.terminer(payload)
    assert t["key"] in s3.objets and t["upload_id"] not in s3.depots   # déjà fermé
    # la base revient : le MÊME lien range le fichier, sans dépôt à fermer
    monkeypatch.setattr("oto_mcp.db.consume_upload_token", consommer)
    s3.head_object = lambda *, Bucket, Key: {"ContentLength": len(s3.objets[Key]["Body"])}
    accuse = large_uploads.terminer(payload)
    assert accuse["bytes"] == len(MP4) and "parts" not in accuse
    assert len(monde["fichiers"]) == 1


def test_cloture_apres_abandon_refusee(s3, monde):
    payload, _ = _ouvrir()
    s3.abort_multipart_upload(Bucket="b", Key=payload["target"]["key"],
                              UploadId=payload["target"]["upload_id"])
    s3.head_object = lambda *, Bucket, Key: (_ for _ in ()).throw(
        ClientError({"Error": {"Code": "404"}}, "HeadObject"))
    with pytest.raises(upload_tokens.UploadError) as e:
        large_uploads.terminer(payload)
    assert e.value.code == "token_already_used" and not monde["fichiers"]


def test_une_signature_reconnue_fait_foi_avant_le_balisage(s3, monde):
    payload, _ = _ouvrir(filename="livre.epub", content_type="application/epub+zip")
    t = payload["target"]
    s3.poser_partie(t["upload_id"], 1, b"PK\x03\x04" + b"<?xml version='1.0'?>" * 10)
    assert large_uploads.terminer(payload)["ok"] is True
    assert t["key"] not in s3.supprimes


def test_tar_sans_signature_servi_neutre():
    assert media_store.type_servi_gros("application/x-tar").attachment is True


def test_le_mode_d_emploi_shell_se_lit(s3, monde, monkeypatch):
    from oto_mcp.capabilities import uploads as cap
    monkeypatch.setattr("oto_mcp.config.public_base_url", lambda: "https://mcp.exemple")

    class Ctx:
        sub, org_id = "sub-a", 3

    hint = cap._upload_url(Ctx(), cap.UploadUrlInput(
        target="project_file", project_id=7, filename="film.mp4", large=True))["hint"]
    assert "-H 'Content-Type:'" in hint and '"$(curl -sf "$B/part/$n")"' in hint
    assert 'dd if="$F" bs=1M skip=$(( (n-1)*5 )) count=5' in hint


def test_duree_de_garde_reglable_et_bornee(monkeypatch):
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_KEEP_DAYS", "3")
    assert large_uploads.keep_days() == 3
    assert "3 days" in large_uploads.retention_notice()
    assert "3 jours" in large_uploads.retention_notice("fr")
    assert "kept 3 days" in large_uploads.libelle({"filename": "f.mp4"})
    monkeypatch.setenv("OTO_MCP_LARGE_UPLOAD_KEEP_DAYS", "0")
    assert large_uploads.keep_days() == 1
    monkeypatch.delenv("OTO_MCP_LARGE_UPLOAD_KEEP_DAYS")
    assert large_uploads.keep_days() == 14


def test_page_d_upload_dit_la_duree_de_garde():
    from oto_mcp.api.upload_page import upload_page_html
    html = upload_page_html("fichier", "fr", part_size=5 * MIB,
                            notice=large_uploads.retention_notice("fr"))
    # Avant le dépôt ET dans l'accusé affiché après.
    assert html.count("gardés 14 jours") == 2
    assert 'class="avis"' not in upload_page_html("fichier", "fr")


def test_fichiers_echus_retire_objet_puis_ligne(monkeypatch):
    from oto_mcp import fichiers_echus
    ordre = []
    monkeypatch.setattr("oto_mcp.db.project_files_expired", lambda n: [
        {"id": 5, "project_id": 7, "s3_key": "project-files/7/x/film.mp4",
         "filename": "film.mp4", "title": None}])
    monkeypatch.setattr("oto_mcp.media_store.delete_by_key",
                        lambda k: ordre.append(("objet", k)))
    monkeypatch.setattr("oto_mcp.db.delete_project_file",
                        lambda i: ordre.append(("ligne", i)) or {"project_id": 7})
    monkeypatch.setattr("oto_mcp.db.log_project_activity",
                        lambda pid, sub, action, detail: ordre.append((action, detail)))
    assert fichiers_echus.purger() == 1
    assert ordre == [("objet", "project-files/7/x/film.mp4"), ("ligne", 5),
                     ("project.file_expired", "film.mp4")]
