"""Résolveur « fichier côté oto » (oto-backend#60).

Teste le dispatch par `kind` + les gardes (kind inconnu, dépassement de taille)
sans I/O réseau : les clients Drive/Gmail et la résolution de credentials sont
monkeypatchés.
"""
from __future__ import annotations

import sys
import types

import pytest

from oto_mcp import file_source as fs


def _inject(monkeypatch, module_path: str, attr: str, value):
    """Injecte un faux module dans sys.modules pour que le `from … import` lazy de
    file_source résolve un stub — sans importer le vrai client (qui tire l'extra
    `google`, absent du venv de test mais présent en prod)."""
    mod = types.ModuleType(module_path)
    setattr(mod, attr, value)
    monkeypatch.setitem(sys.modules, module_path, mod)


@pytest.fixture(autouse=True)
def _no_google(monkeypatch):
    # Pas de credentials réels : les résolveurs drive/gmail instancient un client
    # stub, on n'atteint jamais Google.
    monkeypatch.setattr(fs, "_google_creds", lambda account, service: object())


def test_unknown_kind_raises():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "ftp", "path": "x"})


def test_non_dict_raises():
    with pytest.raises(fs.FileSourceError):
        fs.resolve("drive:123")


def test_drive_dispatch(monkeypatch):
    class FakeDrive:
        def __init__(self, **kw): pass
        def get_file_bytes(self, file_id):
            return {"filename": "Contrat.pdf", "mimeType": "application/pdf",
                    "size": 4, "data": b"%PDF"}
    _inject(monkeypatch, "oto.tools.google.drive.lib.drive_client", "DriveClient", FakeDrive)
    rf = fs.resolve({"kind": "drive", "file_id": "1AbC"})
    assert rf.data == b"%PDF" and rf.filename == "Contrat.pdf" and rf.mime == "application/pdf"


def test_drive_missing_file_id():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "drive"})


def test_gmail_dispatch(monkeypatch):
    class FakeGmail:
        def __init__(self, **kw): pass
        def get_attachment(self, mid, filename, index):
            assert (mid, filename, index) == ("m1", "facture.pdf", 0)
            return {"filename": "facture.pdf", "mimeType": "application/pdf",
                    "size": 3, "data": b"abc"}
    _inject(monkeypatch, "oto.tools.google.gmail.lib.gmail_client", "GmailClient", FakeGmail)
    rf = fs.resolve({"kind": "gmail", "message_id": "m1", "filename": "facture.pdf"})
    assert rf.data == b"abc"


def test_gmail_missing_args():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "gmail", "message_id": "m1"})


def test_size_cap_enforced(monkeypatch):
    class FakeDrive:
        def __init__(self, **kw): pass
        def get_file_bytes(self, file_id):
            return {"filename": "big.bin", "mimeType": "application/octet-stream",
                    "size": 10, "data": b"x" * 10}
    _inject(monkeypatch, "oto.tools.google.drive.lib.drive_client", "DriveClient", FakeDrive)
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "drive", "file_id": "1"}, max_bytes=5)


def test_url_requires_http():
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": "file:///etc/passwd"})


@pytest.mark.parametrize("host", [
    "http://127.0.0.1/x",            # loopback
    "http://localhost/x",            # loopback (résolu)
    "http://169.254.169.254/latest", # IMDS cloud
    "http://10.0.0.5/x",             # privé
    "http://192.168.1.1/x",          # privé
])
def test_url_ssrf_blocked(host):
    # Anti-SSRF : une cible interne/réservée est refusée AVANT toute requête.
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": host})


def test_assert_public_host_passes_for_global(monkeypatch):
    # Un host public ne lève pas. Hermétique (#463) : la résolution est remplacée au seam
    # de la garde d'egress (`egress.resolved_addresses`) — un test unitaire affirme le
    # système, pas la connectivité du poste. Le test contraire (une adresse non publique
    # est refusée) est `test_url_ssrf_blocked` juste au-dessus.
    from oto_mcp import egress
    appels = []

    def _resolu(hote, port):
        appels.append(hote)
        return {"93.184.216.34"}  # adresse globale (plage documentaire d'example.com)

    monkeypatch.setattr(egress, "resolved_addresses", _resolu)
    fs._assert_public_host("example.com")
    assert appels == ["example.com"], "la garde doit résoudre l'hôte demandé, une fois"


# ── kind="project_file" : un fichier déposé sur un projet (ADR 0074) ──────────

class _S3:
    """Faux client S3 : le VRAI `media_store.fetch_object` s'exerce dessus."""

    def __init__(self, objets):
        self.objets = objets

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objets[Key])}

    def get_object(self, *, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objets[Key])}


TROIS_MO = b"ID3" + b"a" * (3 * 1024 * 1024)
FICHIER = {"id": 7, "project_id": 42, "s3_key": "project-files/42/abc/visite.m4a",
           "filename": "visite.m4a", "mime": "audio/mp4"}


@pytest.fixture
def projet(monkeypatch):
    """Le monde par défaut : le projet 42 est visible dans l'org active et lisible,
    il porte le fichier 7. Chaque test ne débranche que ce qu'il éprouve."""
    from oto_mcp import db, media_store, ownership

    etat = {"visible": [], "acces": []}
    monkeypatch.setattr(fs.access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(fs.access, "current_org", lambda sub: 3)

    def _visible(sub, org_id, rtype, rid):
        etat["visible"].append((sub, org_id, rtype, rid))
        return True

    def _acces(sub, rtype, rid, want="read"):
        etat["acces"].append((sub, rtype, rid, want))
        return True

    monkeypatch.setattr(ownership, "visible_in_org", _visible)
    monkeypatch.setattr(ownership, "can_access", _acces)
    monkeypatch.setattr(db, "get_project_file",
                        lambda fid: dict(FICHIER) if fid == FICHIER["id"] else None)
    monkeypatch.setattr(media_store, "_get_client",
                        lambda: _S3({FICHIER["s3_key"]: TROIS_MO}))
    monkeypatch.setattr(media_store, "_bucket", lambda: "media-test")
    monkeypatch.delenv("OTO_MCP_S3_MAX_IMAGE_BYTES", raising=False)
    return etat


def _pf(pid=42, fid=7):
    return {"kind": "project_file", "project_id": pid, "file_id": fid}


def test_project_file_rend_les_octets_nom_et_type(projet):
    """Un fichier de 3 Mo passe : la lecture est bornée par le plafond de
    `file_source` (25 Mo), pas par celui d'une image (2 Mo)."""
    rf = fs.resolve(_pf())
    assert rf.data == TROIS_MO
    assert (rf.filename, rf.mime) == ("visite.m4a", "audio/mp4")


def test_project_file_garde_d_une_lecture_par_id_dans_l_org_active(projet):
    fs.resolve(_pf())
    assert projet["visible"] == [("u1", 3, "project", "42")]
    assert projet["acces"] == [("u1", "project", "42", "read")]


def _message_introuvable(**source):
    with pytest.raises(fs.FileSourceError) as e:
        fs.resolve(_pf(**source))
    return str(e.value)


def test_un_refus_repond_comme_un_fichier_inconnu(projet, monkeypatch):
    """Distinguer « refusé » de « inconnu » dirait à qui n'a pas le droit que le
    fichier existe. Hors de l'org active, sans lecture, fichier absent ou rangé sur
    un AUTRE projet : un seul et même message."""
    from oto_mcp import db, ownership

    attendu = _message_introuvable(fid=999)                    # fichier inconnu
    assert "introuvable" in attendu

    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: False)
    assert _message_introuvable(fid=999) == attendu            # hors de l'org active
    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: True)

    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: False)
    assert _message_introuvable(fid=999) == attendu            # sans droit de lecture
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: True)

    monkeypatch.setattr(db, "get_project_file",
                        lambda fid: dict(FICHIER, id=999, project_id=41))
    assert _message_introuvable(fid=999) == attendu            # fichier d'un autre projet


def test_un_refus_ne_lit_rien_dans_le_stockage(projet, monkeypatch):
    from oto_mcp import media_store, ownership

    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: False)
    monkeypatch.setattr(media_store, "_get_client",
                        lambda: pytest.fail("le stockage ne doit pas être lu sur un refus"))
    with pytest.raises(fs.FileSourceError):
        fs.resolve(_pf())


def test_project_file_au_dela_du_plafond_est_refuse_avant_lecture(projet):
    with pytest.raises(fs.FileSourceError, match="object_too_large"):
        fs.resolve(_pf(), max_bytes=1024 * 1024)


@pytest.mark.parametrize("source", [
    {"kind": "project_file", "file_id": 7},
    {"kind": "project_file", "project_id": 42},
    {"kind": "project_file", "project_id": "abc", "file_id": 7},
])
def test_project_file_exige_deux_ids_entiers(projet, source):
    with pytest.raises(fs.FileSourceError, match="entiers requis"):
        fs.resolve(source)


def test_le_kind_inconnu_nomme_project_file():
    with pytest.raises(fs.FileSourceError, match="project_file"):
        fs.resolve({"kind": "ftp"})


def test_le_type_http_est_annonce():
    with pytest.raises(fs.FileSourceError, match="api"):
        fs.resolve({"kind": "ftp", "path": "x"})


def test_api_exige_un_chemin_relatif():
    with pytest.raises(fs.FileSourceError, match="path"):
        fs.resolve({"kind": "api", "path": "x.csv"})
    with pytest.raises(fs.FileSourceError, match="params"):
        fs.resolve({"kind": "api", "path": "/x.csv", "params": "a=1"})


# --- un fichier PRODUIT par l'agent, passé en ligne (oto-backend#1149) -----------

def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def test_content_base64_rend_les_octets_le_nom_et_le_type():
    rf = fs.resolve({"kind": "content", "filename": "rapport.xlsx",
                     "base64": _b64(b"PK\x03\x04xlsx")})
    assert rf.data == b"PK\x03\x04xlsx" and rf.filename == "rapport.xlsx"
    assert rf.mime == ("application/vnd.openxmlformats-officedocument."
                       "spreadsheetml.sheet")


def test_content_text_est_de_l_utf8_et_le_type_vient_du_nom():
    rf = fs.resolve({"kind": "content", "filename": "leads.csv", "text": "nom\nÉlise\n"})
    assert rf.data == "nom\nÉlise\n".encode() and rf.mime == "text/csv"


def test_content_le_type_declare_l_emporte_et_le_chemin_ne_passe_pas():
    rf = fs.resolve({"kind": "content", "filename": "../../x/notes", "text": "a",
                     "mime": "text/markdown"})
    assert rf.filename == "notes" and rf.mime == "text/markdown"


@pytest.mark.parametrize("source, motif", [
    ({"kind": "content", "base64": "YQ=="}, "filename"),
    ({"kind": "content", "filename": "a.bin"}, "exactement un"),
    ({"kind": "content", "filename": "a.bin", "base64": "YQ==", "text": "a"}, "exactement un"),
    ({"kind": "content", "filename": "a.bin", "base64": "pas du base64 !"}, "invalide"),
    ({"kind": "content", "filename": "a.bin", "base64": "data:x;base64,YQ=="}, "invalide"),
    ({"kind": "content", "filename": "a.txt", "text": 3}, "chaîne"),
])
def test_content_mal_forme_est_refuse_par_son_nom(source, motif):
    with pytest.raises(fs.FileSourceError, match=motif):
        fs.resolve(source)


def test_content_borne_avant_de_decoder(monkeypatch):
    """Au-delà du plafond en ligne, refusé sur la longueur ENCODÉE : rien n'est décodé."""
    import base64 as b64mod
    trop = _b64(b"x" * (fs.CONTENT_MAX_BYTES + 3))
    monkeypatch.setattr(b64mod, "b64decode",
                        lambda *a, **k: pytest.fail("décodé malgré le plafond"))
    with pytest.raises(fs.FileSourceError, match="oto_upload_url"):
        fs.resolve({"kind": "content", "filename": "a.bin", "base64": trop})


def test_content_juste_au_plafond_passe_et_un_texte_au_dela_est_refuse():
    pile = b"x" * fs.CONTENT_MAX_BYTES
    assert len(fs.resolve({"kind": "content", "filename": "a.bin",
                           "base64": _b64(pile)}).data) == fs.CONTENT_MAX_BYTES
    with pytest.raises(fs.FileSourceError, match="en ligne"):
        fs.resolve({"kind": "content", "filename": "a.txt",
                    "text": "x" * (fs.CONTENT_MAX_BYTES + 1)})


def test_content_respecte_un_plafond_d_appelant_plus_bas():
    with pytest.raises(fs.FileSourceError, match="10 octets"):
        fs.resolve({"kind": "content", "filename": "a.txt", "text": "x" * 11},
                   max_bytes=10)


def test_le_type_content_est_annonce():
    with pytest.raises(fs.FileSourceError, match="content"):
        fs.resolve({"kind": "ftp"})


# --- une pièce jointe d'une messagerie hébergée (oto-backend#1149) ---------------
# Formes seulement : ni vrai compte, ni vraie pièce.

COMPTE = "acc_forme01"
DSN = "api.unipile.example"
PIECE = f"https://{DSN}/v2/{COMPTE}/messages/msg_1/attachments/att_1"


class _Reponse:
    def __init__(self, status=200, corps=b"OggS-audio", entetes=None):
        self.status_code, self._corps = status, corps
        self.headers = {"content-type": "audio/ogg; codecs=opus",
                        "content-length": str(len(corps)), **(entetes or {})}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, taille):
        for i in range(0, len(self._corps), 4):
            yield self._corps[i:i + 4]


class _Session:
    def __init__(self, reponse):
        self.reponse, self.appels = reponse, []

    def get(self, url, **kw):
        self.appels.append((url, kw))
        if isinstance(self.reponse, Exception):
            raise self.reponse
        return self.reponse


class _Client:
    def __init__(self, reponse):
        self.dsn, self.session = DSN, _Session(reponse)

    def account_id(self):
        return COMPTE


@pytest.fixture
def unipile(monkeypatch):
    """Le client du compte opéré, stubé ; `etat["canaux"]` dit pour quel canal il a été
    résolu (la même résolution que la lecture)."""
    from oto_mcp.tools import unipile as U
    etat = {"canaux": [], "client": _Client(_Reponse())}

    def _resoudre(provider="LINKEDIN"):
        etat["canaux"].append(provider)
        return etat["client"]
    monkeypatch.setattr(U, "unipile_client", _resoudre)
    return etat


def _src(**kw):
    return {"kind": "unipile", "channel": "whatsapp", "url": PIECE, **kw}


def test_unipile_rend_la_piece_du_compte_opere(unipile):
    unipile["client"] = _Client(_Reponse(entetes={
        "content-disposition": "attachment; filename*=UTF-8''note%20vocale.ogg"}))
    rf = fs.resolve(_src())
    assert (rf.data, rf.filename, rf.mime) == (b"OggS-audio", "note vocale.ogg", "audio/ogg")
    assert unipile["canaux"] == ["WHATSAPP"]
    url, kw = unipile["client"].session.appels[0]
    assert url == PIECE and kw["allow_redirects"] is False and kw["stream"] is True


@pytest.mark.parametrize("disposition", [
    'attachment; filename="ACF0.ogg; codecs=opus"',
    "attachment; filename*=UTF-8''ACF0.ogg%3B%20codecs%3Dopus",
])
def test_unipile_nom_servi_sans_les_parametres_du_type(unipile, disposition):
    """Unipile nomme la pièce d'après le type MIME complet (`….ogg; codecs=opus`) :
    les paramètres du type ne font pas partie du nom de fichier."""
    unipile["client"] = _Client(_Reponse(entetes={"content-disposition": disposition}))
    assert fs.resolve(_src()).filename == "ACF0.ogg"


def test_unipile_sans_nom_servi_nomme_par_le_canal_et_le_type(unipile):
    assert fs.resolve(_src()).filename.startswith("whatsapp-att_1")


@pytest.mark.parametrize("url", [
    f"https://{DSN}/v2/acc_autre/messages/msg_1/attachments/att_1",   # un autre compte
    f"https://{DSN}/v2/{COMPTE}x/messages/msg_1/attachments/att_1",   # préfixe trompeur
    f"https://ailleurs.example/v2/{COMPTE}/messages/msg_1/attachments/att_1",
    f"http://{DSN}/v2/{COMPTE}/messages/msg_1/attachments/att_1",
    f"https://{DSN}/{COMPTE}/messages/msg_1",
])
def test_unipile_refuse_une_url_hors_du_compte_opere_sans_rien_appeler(unipile, url):
    """La clé est partagée : sans cette garde, elle lirait la pièce d'un autre compte,
    ou partirait vers un autre hôte."""
    with pytest.raises(fs.FileSourceError, match="compte whatsapp") as e:
        fs.resolve(_src(url=url))
    assert unipile["client"].session.appels == []
    assert COMPTE not in str(e.value)


@pytest.mark.parametrize("source, motif", [
    ({"kind": "unipile", "channel": "fax", "url": PIECE}, "channel"),
    ({"kind": "unipile", "url": PIECE}, "channel"),
    ({"kind": "unipile", "channel": "telegram"}, "url"),
])
def test_unipile_mal_formee_est_refusee_avant_toute_resolution(unipile, source, motif):
    with pytest.raises(fs.FileSourceError, match=motif):
        fs.resolve(source)
    assert unipile["canaux"] == []


def test_unipile_sans_compte_connecte_dit_le_refus_de_la_lecture(monkeypatch):
    from mcp.types import ErrorData, INVALID_PARAMS
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import unipile as U

    def _refus(provider):
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message="Connect your Whatsapp account first."))
    monkeypatch.setattr(U, "unipile_client", _refus)
    with pytest.raises(fs.FileSourceError, match="Connect your Whatsapp account"):
        fs.resolve(_src())


@pytest.mark.parametrize("reponse, motif", [
    (_Reponse(status=302, entetes={"location": "https://ailleurs.example/x"}),
     "redirection refusée"),
    (_Reponse(status=404), "404"),
    (_Reponse(corps=b"x" * 64), "trop volumineuse"),
    (_Reponse(corps=b"x" * 64, entetes={"content-length": ""}), "> 32 octets"),
])
def test_unipile_refus_au_telechargement(unipile, reponse, motif):
    unipile["client"] = _Client(reponse)
    with pytest.raises(fs.FileSourceError, match=motif):
        fs.resolve(_src(), max_bytes=32)


def test_unipile_panne_reseau_ne_rend_que_son_type(unipile):
    import requests
    unipile["client"] = _Client(requests.ConnectionError(f"boom {PIECE}"))
    with pytest.raises(fs.FileSourceError, match="ConnectionError") as e:
        fs.resolve(_src())
    assert COMPTE not in str(e.value)


def test_le_type_unipile_est_annonce():
    with pytest.raises(fs.FileSourceError, match="unipile"):
        fs.resolve({"kind": "ftp"})
