"""Les fichiers qu'un lecteur dépose dans une app pour ses agents.

Déposer est un GESTE (consentement), le type se juge sur les OCTETS, seul le lecteur
qui a déposé lit (lui, et les runs qu'il lance), et un run reçoit le texte des
fichiers qu'on lui remet, borné pour tenir dans son `handoff`. Stockage et base
remplacés par des dictionnaires : on vérifie le jugement.
"""
import pytest

from oto_mcp import client_trace, db, file_extract, geste, media_store, ownership
from oto_mcp.capabilities import _acces_agent
from oto_mcp.capabilities import apps as A
from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities import runner_triggers as T
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

LECTEUR, AUTRE, AUTEUR = "lecteur", "collegue", "auteur"
ORG = 42
CTX = ResolvedCtx(sub=LECTEUR, org_id=ORG, channel="rest")
AUTRE_CTX = ResolvedCtx(sub=AUTRE, org_id=ORG, channel="rest")
PDF = b"%PDF-1.7\n" + b"x" * 200
MANIFEST = {"tables": {}, "agents": {"enrich": {"id": 9, "run": True, "input": True}},
            "files": True}
APP = {"id": 5, "owner_type": "user", "owner_id": AUTEUR, "context_org_id": ORG,
       "title": "Inbox", "manifest": MANIFEST, "updated_by": AUTEUR, "archived_at": None,
       "thumbnail": None, "html": "", "created_by": AUTEUR, "created_at": "t",
       "updated_at": "t", "rev": "r"}


@pytest.fixture(autouse=True)
def hote():
    """L'écran hôte : session interactive, origine déclarée (`conftest.ORIGINES_CORS`)."""
    jeton = client_trace.set_current(ip=None, user_agent=None, origin="https://manage.oto.cx",
                                     fetch_dest="empty")
    with geste.portee(geste.CONSOLE, LECTEUR):
        yield
    client_trace.reset(jeton)


@pytest.fixture
def s(monkeypatch):
    rec = {"consents": {}, "files": {}, "objects": {}, "run_now": [], "manifest": MANIFEST}
    monkeypatch.setattr(ownership, "can_access", lambda sub, t, rid, want="read": sub in (LECTEUR, AUTRE))
    monkeypatch.setattr(db.apps, "get_for_call", lambda i: dict(APP, manifest=rec["manifest"]) if i == 5 else None)
    monkeypatch.setattr(db.apps, "get", lambda i: dict(APP, manifest=rec["manifest"]) if i == 5 else None)
    monkeypatch.setattr(C, "org_de_l_app", lambda app: ORG)
    monkeypatch.setattr(db, "get_trigger", lambda tid, org: {"id": tid})
    monkeypatch.setattr(_acces_agent, "niveau", lambda sub, org, t: "editor")
    monkeypatch.setattr(db, "file_du_declencheur", lambda tid, org: {"pending": 0})
    monkeypatch.setattr(T, "_triggers_sync", lambda ctx, inp: rec["run_now"].append(inp) or {"job": {"id": 77}})

    def issue(app_id, sub, action, fp, h, ttl):
        rec["consents"][h] = (app_id, sub, action, fp, False)

    def burn(app_id, sub, action, fp, h, geste_id):
        c = rec["consents"].get(h)
        if not c or c[4] or c[:4] != (app_id, sub, action, fp):
            return None
        rec["consents"][h] = (*c[:4], True)
        return 1

    monkeypatch.setattr(db.apps, "consent_issue", issue)
    monkeypatch.setattr(db.apps, "consent_burn", burn)
    monkeypatch.setattr(db.apps, "consent_job", lambda cid, jid: None)

    def upload_object(prefix, owner, data, ct, name, *, max_bytes=None, propre_au_depot=False):
        key = f"{prefix}/{owner}/{len(rec['objects'])}/{name}"
        rec["objects"][key] = data
        return key
    monkeypatch.setattr(media_store, "upload_object", upload_object)
    rec["deleted"] = []
    monkeypatch.setattr(media_store, "delete_by_key",
                        lambda key: rec["deleted"].append(key) or rec["objects"].pop(key, None))
    def fetch(key, max_bytes=None):
        rec["fetches"] += 1
        return rec["objects"][key]
    monkeypatch.setattr(media_store, "fetch_object", fetch)

    def file_add(app_id, sub, key, name, mt, size, text=None, text_status=None, origin="upload",
                 *, max_app_bytes, max_viewer_bytes):
        # Le contrat du SQL : compte et insertion ensemble, sous les deux plafonds.
        mien = [f for f in rec["files"].values() if f["sub"] == sub]
        if sum(f["size"] for f in mien if f["app_id"] == app_id) + size > max_app_bytes:
            raise db.apps.StorageFull("app")
        if sum(f["size"] for f in mien) + size + rec.get("ailleurs", 0) > max_viewer_bytes:
            raise db.apps.StorageFull("viewer")
        i = len(rec["files"]) + 1
        rec["files"][i] = {"id": i, "app_id": app_id, "sub": sub, "s3_key": key, "name": name,
                           "media_type": mt, "size": size, "created_at": "t", "text": text,
                           "text_status": text_status, "origin": origin}
        return rec["files"][i]
    monkeypatch.setattr(db.apps, "file_add", file_add)
    monkeypatch.setattr(db.apps, "files_text", lambda ids: {i: rec["files"][i]["text"] for i in ids})
    monkeypatch.setattr(db.apps, "bytes_of", lambda a, sub: sum(
        f["size"] for f in rec["files"].values() if f["app_id"] == a and f["sub"] == sub))
    monkeypatch.setattr(db.apps, "bytes_of_viewer", lambda sub: rec.get("ailleurs", 0) + sum(
        f["size"] for f in rec["files"].values() if f["sub"] == sub))
    # Lancer un run ne relit aucun objet : le texte a été extrait au dépôt.
    rec["fetches"] = 0
    monkeypatch.setattr(db.apps, "file_of", lambda a, sub, i: (
        f if (f := rec["files"].get(i)) and f["app_id"] == a and f["sub"] == sub else None))
    monkeypatch.setattr(db.apps, "files_of", lambda a, sub, limit: [
        f for f in reversed(list(rec["files"].values())) if f["app_id"] == a and f["sub"] == sub][:limit])
    return rec


def refus(fn, *a, **kw) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        fn(*a, **kw)
    return e.value


def deposer(data=PDF, name="devis.pdf", mt="application/pdf", ctx=CTX, consent=True):
    jeton = (C._consent(ctx, C.AppCallInput(app_id=5, action="upload_file",
                                            file={"name": name, "size": len(data), "media_type": mt}))["consent"]
             if consent else None)
    return C.deposer(ctx, 5, jeton, name, mt, data)


def test_deposer_un_pdf(s):
    out = deposer()
    assert out["file"]["name"] == "devis.pdf" and out["file"]["media_type"] == "application/pdf"
    assert list(s["objects"])[0].startswith("app-files/5/")


def test_deposer_exige_le_consentement_et_le_fichier_annonce(s):
    assert refus(deposer, consent=False).code == "consent_required"
    jeton = C._consent(CTX, C.AppCallInput(app_id=5, action="upload_file",
                                           file={"name": "a.pdf", "size": len(PDF), "media_type": "application/pdf"}))["consent"]
    # Un autre fichier que celui annoncé : refusé.
    assert refus(C.deposer, CTX, 5, jeton, "b.pdf", "application/pdf", PDF).code == "consent_invalid"
    assert s["files"] == {}


def test_le_type_se_juge_sur_les_octets(s):
    """Un exécutable renommé .pdf, ou du HTML déclaré texte : refusés."""
    e = refus(deposer, data=b"MZ\x90\x00" + b"x" * 50, name="x.pdf", mt="application/pdf")
    assert e.code == "file_type"
    e = refus(deposer, data=b"<html><script>x</script></html>", name="x.txt", mt="text/plain")
    assert e.code == "file_type"
    assert refus(C._consent, CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "a.exe", "size": 10, "media_type": "application/x-msdownload"})).code == "file_type"


def test_trop_gros_ou_app_sans_fichiers(s):
    assert refus(C._consent, CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "a.pdf", "size": C.MAX_FILE_BYTES + 1, "media_type": "application/pdf"})).code == "file_too_large"
    s["manifest"] = {**MANIFEST, "files": False}
    assert refus(deposer).code == "files_not_allowed"


def test_seul_qui_a_depose_le_voit(s):
    deposer()
    mine = C._call(CTX, C.AppCallInput(app_id=5, action="list_files"))["files"]
    assert [f["name"] for f in mine] == ["devis.pdf"]
    autre = C._call(ResolvedCtx(sub=AUTRE, org_id=ORG), C.AppCallInput(app_id=5, action="list_files"))
    assert autre["files"] == []


def test_l_appel_json_ne_depose_rien_et_ne_brule_pas_le_jeton(s):
    annonce = {"name": "a.pdf", "size": len(PDF), "media_type": "application/pdf"}
    jeton = C._consent(CTX, C.AppCallInput(app_id=5, action="upload_file", file=annonce))["consent"]
    e = refus(C._call, CTX, C.AppCallInput(app_id=5, action="upload_file", consent=jeton, file=annonce))
    assert e.code == "upload_route_required"
    # Le jeton sert encore à la route de dépôt.
    assert C.deposer(CTX, 5, jeton, "a.pdf", "application/pdf", PDF)["file"]["name"] == "a.pdf"


def test_le_type_annonce_et_le_type_recu_se_comparent_normalises(s):
    jeton = C._consent(CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "a.pdf", "size": len(PDF), "media_type": "Application/PDF"}))["consent"]
    assert C.deposer(CTX, 5, jeton, "a.pdf", "application/pdf; charset=binary", PDF)["file"]


def test_ni_images_ni_powerpoint(s):
    """Sans OCR ni extracteur, l'agent n'en lirait rien : refusés, avant tout octet."""
    for name, mt in (("photo.png", "image/png"), ("deck.pptx",
                     "application/vnd.openxmlformats-officedocument.presentationml.presentation")):
        assert refus(C._consent, CTX, C.AppCallInput(app_id=5, action="upload_file", file={
            "name": name, "size": 10, "media_type": mt})).code == "file_type"


def test_un_lecteur_garde_1_go_par_app(s, monkeypatch):
    monkeypatch.setattr(db.apps, "bytes_of", lambda a, sub: C.MAX_FILES_BYTES_PER_VIEWER - 10)
    e = refus(C._consent, CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "a.pdf", "size": 11, "media_type": "application/pdf"}))
    assert (e.status, e.code) == (413, "app_storage_full")


def test_le_jeton_d_un_depot_vaut_15_minutes(s):
    out = C._consent(CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "a.pdf", "size": 10, "media_type": "application/pdf"}))
    assert out["expires_in"] == 15 * 60


def test_le_run_recoit_le_texte_du_fichier(s, monkeypatch):
    monkeypatch.setattr(file_extract, "extract", lambda data, name, mt: file_extract.Extraction(
        file_extract.OK, "Total: 1 200 € HT"))
    fid = deposer()["file"]["id"]
    args = dict(action="start_agent", agent="enrich", input="Extract the total", files=[fid])
    jeton = C._consent(CTX, C.AppCallInput(app_id=5, **args))["consent"]
    assert C._call(CTX, C.AppCallInput(app_id=5, consent=jeton, **args)) == {"job_id": 77}
    (inp,), = [s["run_now"]]
    assert "Extract the total" in inp.handoff and "devis.pdf" in inp.handoff
    assert "Total: 1 200 € HT" in inp.handoff and f"file_id=<id>" in inp.handoff
    assert len(inp.handoff) <= 20_000
    assert s["fetches"] == 0


def test_le_texte_garde_est_borne(s, monkeypatch):
    monkeypatch.setattr(file_extract, "extract", lambda data, name, mt: file_extract.Extraction(
        file_extract.OK, "y" * 1_000_000))
    fid = deposer()["file"]["id"]
    assert len(s["files"][fid]["text"]) == C.STORED_TEXT_CHARS


def test_le_handoff_reste_borne(s, monkeypatch):
    monkeypatch.setattr(file_extract, "extract", lambda data, name, mt: file_extract.Extraction(
        file_extract.OK, "x" * 100_000))
    ids = [deposer(name=f"f{i}.pdf")["file"]["id"] for i in range(5)]
    args = dict(action="start_agent", agent="enrich", input="go", files=ids)
    jeton = C._consent(CTX, C.AppCallInput(app_id=5, **args))["consent"]
    C._call(CTX, C.AppCallInput(app_id=5, consent=jeton, **args))
    assert len(s["run_now"][0].handoff) <= 20_000 and "truncated" in s["run_now"][0].handoff


def test_un_run_ne_recoit_que_les_fichiers_du_lecteur(s):
    fid = deposer()["file"]["id"]
    e = refus(C._consent, AUTRE_CTX, C.AppCallInput(app_id=5, action="start_agent", agent="enrich", files=[fid]))
    assert e.code == "unknown_file"
    e = refus(C._consent, CTX, C.AppCallInput(app_id=5, action="start_agent", agent="enrich",
                                              files=list(range(1, 8))))
    assert e.code == "too_many_files"


def test_l_agent_lit_le_fichier_en_entier(s, monkeypatch):
    from oto_mcp import file_content
    fid = deposer()["file"]["id"]
    monkeypatch.setattr(file_content, "render_for_agent", lambda data, name, mt, *, sub, prefix: {
        "filename": name, "encoding": "text", "format": "pdf-text", "content": "…", "sub": sub})
    monkeypatch.setattr(A, "_cards", lambda sub, rows, writable=None: rows)
    out = A._app(CTX, A.AppInput(op="read_file", app_id=5, file_id=fid))
    assert out["file"]["id"] == fid and out["file"]["format"] == "pdf-text" and out["file"]["sub"] == LECTEUR
    e = refus(A._app, ResolvedCtx(sub=AUTRE, org_id=ORG), A.AppInput(op="read_file", app_id=5, file_id=fid))
    assert e.code == "unknown_file"



# ── Un agent rend un fichier à l'app ──────────────────────────────────────

def test_l_agent_rend_un_fichier_au_lecteur(s):
    out = A._app(CTX, A.AppInput(op="put_file", app_id=5, name="totaux.csv",
                                 content="client,total\nAcme,1200\n", media_type="text/csv"))
    f = out["file"]
    assert f["origin"] == "agent" and f["media_type"] == "text/csv" and f["name"] == "totaux.csv"
    vus = C._call(CTX, C.AppCallInput(app_id=5, action="list_files"))["files"]
    assert [(x["name"], x["origin"]) for x in vus] == [("totaux.csv", "agent")]
    # Un autre lecteur ne le voit pas.
    assert C._call(ResolvedCtx(sub=AUTRE, org_id=ORG), C.AppCallInput(app_id=5, action="list_files"))["files"] == []


def test_l_agent_rend_un_pdf_en_base64(s):
    import base64
    out = A._app(CTX, A.AppInput(op="put_file", app_id=5, name="rapport.pdf",
                                 content_base64=base64.b64encode(PDF).decode(), media_type="application/pdf"))
    assert out["file"]["media_type"] == "application/pdf"


@pytest.mark.parametrize("kw,code", [
    (dict(name="a.csv"), "invalid_file"),                                     # ni content ni base64
    (dict(name="a.csv", content="x", content_base64="eA=="), "invalid_file"),  # les deux
    (dict(name="a.pdf", content_base64="pas du base64!"), "invalid_file"),
    (dict(name="", content="x", media_type="text/plain"), "invalid_file"),
    (dict(name="x.html", content="<html><script>1</script></html>", media_type="text/html"), "file_type"),
])
def test_rendre_refuse(s, kw, code):
    assert refus(A._app, CTX, A.AppInput(op="put_file", app_id=5, **kw)).code == code


def test_rendre_dans_une_app_sans_fichiers_ou_illisible(s, monkeypatch):
    s["manifest"] = {**MANIFEST, "files": False}
    assert refus(A._app, CTX, A.AppInput(op="put_file", app_id=5, name="a.txt", content="x",
                                         media_type="text/plain")).code == "files_not_allowed"
    s["manifest"] = MANIFEST
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: False)
    assert refus(A._app, CTX, A.AppInput(op="put_file", app_id=5, name="a.txt", content="x",
                                         media_type="text/plain")).code == "unknown_app"


def test_rendre_respecte_le_plafond(s, monkeypatch):
    monkeypatch.setattr(db.apps, "bytes_of", lambda a, sub: C.MAX_FILES_BYTES_PER_VIEWER)
    assert refus(A._app, CTX, A.AppInput(op="put_file", app_id=5, name="a.txt", content="x",
                                         media_type="text/plain")).code == "app_storage_full"


def test_le_lien_de_telechargement_est_au_lecteur_seul(s, monkeypatch):
    vus = []
    monkeypatch.setattr(media_store, "presign_get",
                        lambda key, expiry=None, download_as=None: vus.append((key, expiry, download_as)) or "https://signed")
    fid = deposer()["file"]["id"]
    out = C._call(CTX, C.AppCallInput(app_id=5, action="file_url", file_id=fid))
    assert out == {"url": "https://signed", "name": "devis.pdf", "media_type": "application/pdf",
                   "expires_in": C.FILE_URL_TTL}
    assert vus[0][1] == C.FILE_URL_TTL and vus[0][2] == "devis.pdf"
    e = refus(C._call, ResolvedCtx(sub=AUTRE, org_id=ORG), C.AppCallInput(app_id=5, action="file_url", file_id=fid))
    assert e.code == "unknown_file"
    assert refus(C._call, CTX, C.AppCallInput(app_id=5, action="file_url")).code == "missing_file_id"


# ── L'accord d'un dépôt : l'hôte affiche nom, type et taille ─────────────────

def test_le_resume_d_un_depot_dit_nom_type_et_taille(s):
    out = C._consent(CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "devis.pdf", "size": len(PDF), "media_type": "Application/PDF"}))
    r = out["summary"]
    assert r["action"] == "upload_file" and r["target"] == {"app_id": 5}
    assert r["content"] == {"file": {"name": "devis.pdf", "size": len(PDF),
                                     "media_type": "application/pdf"}}
    assert "devis.pdf" in r["text"] and str(len(PDF)) in r["text"]


def test_un_depot_demande_depuis_l_app_n_obtient_pas_de_jeton(s):
    jeton = client_trace.set_current(ip=None, user_agent=None, origin="null", fetch_dest="empty")
    try:
        e = refus(C._consent, CTX, C.AppCallInput(app_id=5, action="upload_file", file={
            "name": "a.pdf", "size": 10, "media_type": "application/pdf"}))
    finally:
        client_trace.reset(jeton)
    assert (e.status, e.code) == (403, "consent_from_host_only")


def test_le_resume_d_un_lancement_nomme_les_fichiers_remis(s):
    fid = deposer()["file"]["id"]
    r = C._consent(CTX, C.AppCallInput(app_id=5, action="start_agent", agent="enrich",
                                       input="Extract", files=[fid]))["summary"]
    assert r["content"] == {"input": "Extract", "files": [
        {"id": fid, "name": "devis.pdf", "media_type": "application/pdf", "size": len(PDF)}]}
    assert "devis.pdf" in r["text"]


# ── Les plafonds : par app, toutes apps confondues, tenus à l'insertion ──────

def test_un_lecteur_garde_5_go_toutes_apps_confondues(s):
    s["ailleurs"] = C.MAX_FILES_BYTES_PER_VIEWER_TOTAL - 10
    e = refus(C._consent, CTX, C.AppCallInput(app_id=5, action="upload_file", file={
        "name": "a.pdf", "size": 11, "media_type": "application/pdf"}))
    assert (e.status, e.code) == (413, "viewer_storage_full")
    e = refus(A._app, CTX, A.AppInput(op="put_file", app_id=5, name="a.txt", content="x" * 11,
                                      media_type="text/plain"))
    assert e.code == "viewer_storage_full"


def test_le_plafond_tient_a_l_insertion_et_ne_laisse_pas_d_objet(s, monkeypatch):
    """L'avis précoce (`bytes_of`) est passé, mais un dépôt concurrent a rempli la place
    entre-temps : l'insertion refuse, et l'objet déjà posé est retiré du stockage."""
    monkeypatch.setattr(db.apps, "bytes_of", lambda a, sub: 0)
    monkeypatch.setattr(db.apps, "bytes_of_viewer", lambda sub: 0)
    s["ailleurs"] = C.MAX_FILES_BYTES_PER_VIEWER_TOTAL
    e = refus(deposer)
    assert (e.status, e.code) == (413, "viewer_storage_full")
    assert s["files"] == {} and s["objects"] == {} and len(s["deleted"]) == 1


# ── Un CSV rendu par un agent n'est pas une formule ─────────────────────────

def test_un_csv_rendu_par_un_agent_est_neutralise(s):
    csv_ = ('client,total,note,tel\n'
            'Acme,-12.5,"=HYPERLINK(""http://x.test"",""clic"")",+33 6 12 34 56 78\n'
            '@SUM(A1),+5,-2+3+cmd|calc,=1+1\n')
    f = A._app(CTX, A.AppInput(op="put_file", app_id=5, name="totaux.csv", content=csv_,
                               media_type="text/csv"))["file"]
    rendu = s["objects"][s["files"][f["id"]]["s3_key"]].decode()
    lignes = [l.split(",", 1)[0] for l in rendu.splitlines()]
    assert lignes == ["client", "Acme", "'@SUM(A1)"]
    assert "'=HYPERLINK" in rendu and "'-2+3+cmd|calc" in rendu and "'=1+1" in rendu
    # Un nombre signé reste un nombre.
    assert "Acme,-12.5," in rendu and ",+33 6 12 34 56 78" in rendu and ",+5," in rendu


def test_un_depot_du_lecteur_n_est_pas_reecrit(s):
    csv_ = b"a,b\n=1+1,x\n"
    deposer(data=csv_, name="mien.csv", mt="text/csv")
    assert list(s["objects"].values()) == [csv_]
