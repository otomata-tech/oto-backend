"""Les fichiers qu'un lecteur dépose dans une app pour ses agents.

Déposer est un GESTE (consentement), le type se juge sur les OCTETS, seul le lecteur
qui a déposé lit (lui, et les runs qu'il lance), et un run reçoit le texte des
fichiers qu'on lui remet, borné pour tenir dans son `handoff`. Stockage et base
remplacés par des dictionnaires : on vérifie le jugement.
"""
import pytest

from oto_mcp import db, file_extract, media_store, ownership
from oto_mcp.capabilities import _acces_agent
from oto_mcp.capabilities import apps as A
from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities import runner_triggers as T
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

LECTEUR, AUTRE, AUTEUR = "lecteur", "collegue", "auteur"
ORG = 42
CTX = ResolvedCtx(sub=LECTEUR, org_id=ORG)
PDF = b"%PDF-1.7\n" + b"x" * 200
MANIFEST = {"tables": {}, "agents": {"enrich": {"id": 9, "run": True, "input": True}},
            "files": True}
APP = {"id": 5, "owner_type": "user", "owner_id": AUTEUR, "context_org_id": ORG,
       "title": "Inbox", "manifest": MANIFEST, "updated_by": AUTEUR, "archived_at": None,
       "thumbnail": None, "html": "", "created_by": AUTEUR, "created_at": "t",
       "updated_at": "t", "rev": "r"}


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
    def fetch(key, max_bytes=None):
        rec["fetches"] += 1
        return rec["objects"][key]
    monkeypatch.setattr(media_store, "fetch_object", fetch)

    def file_add(app_id, sub, key, name, mt, size, text=None, text_status=None):
        i = len(rec["files"]) + 1
        rec["files"][i] = {"id": i, "app_id": app_id, "sub": sub, "s3_key": key, "name": name,
                           "media_type": mt, "size": size, "created_at": "t", "text": text,
                           "text_status": text_status}
        return rec["files"][i]
    monkeypatch.setattr(db.apps, "file_add", file_add)
    monkeypatch.setattr(db.apps, "files_text", lambda ids: {i: rec["files"][i]["text"] for i in ids})
    monkeypatch.setattr(db.apps, "bytes_of", lambda a, sub: sum(
        f["size"] for f in rec["files"].values() if f["app_id"] == a and f["sub"] == sub))
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
    autre = ResolvedCtx(sub=AUTRE, org_id=ORG)
    e = refus(C._consent, autre, C.AppCallInput(app_id=5, action="start_agent", agent="enrich", files=[fid]))
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
