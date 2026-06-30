"""`db.projects.duplicate_project` — copie profonde d'un projet (ADR 0032 §7 B5a).

On monkeypatche les sous-fonctions de domaine (pas de DB) pour vérifier la logique
de remap de l'arbre des docs, la préservation des liens, et la copie S3 des fichiers.
"""
import oto_mcp.db.projects as PJ
import oto_mcp.media_store as MS


def _wire(monkeypatch, *, src):
    """Câble les seams de domaine + un compteur d'id pour les créations."""
    created = {"projects": [], "docs": [], "links": [], "files": [], "activity": []}
    counter = {"pid": 100, "doc": 200}

    def create_project(ot, oid, name, brief_md="", created_by=None):
        counter["pid"] += 1
        created["projects"].append((counter["pid"], ot, oid, name, brief_md, created_by))
        return counter["pid"]

    def create_doc(pid, title, *, parent_id=None, body_md="", kind="doc", created_by=None):
        counter["doc"] += 1
        created["docs"].append({"new_id": counter["doc"], "pid": pid, "title": title,
                                "parent_id": parent_id, "body_md": body_md, "kind": kind})
        return counter["doc"]

    monkeypatch.setattr(PJ, "get_project_by_id", lambda pid: src["project"] if pid == 7 else None)
    monkeypatch.setattr(PJ, "create_project", create_project)
    monkeypatch.setattr(PJ, "list_docs_for_project", lambda pid: list(src["docs"]))
    monkeypatch.setattr(PJ, "create_doc", create_doc)
    monkeypatch.setattr(PJ, "list_project_links", lambda pid: list(src["links"]))
    monkeypatch.setattr(PJ, "add_project_link",
                        lambda pid, tt, tr, label=None, role=None, config=None:
                        created["links"].append((pid, tt, tr, label, role, config)))
    monkeypatch.setattr(PJ, "list_project_files", lambda pid: list(src["files"]))
    monkeypatch.setattr(PJ, "add_project_file",
                        lambda pid, key, fn, **kw: created["files"].append((pid, key, fn, kw)))
    monkeypatch.setattr(PJ, "log_project_activity",
                        lambda pid, sub, action, detail=None: created["activity"].append((pid, action, detail)))
    monkeypatch.setattr(MS, "copy_object", lambda src_key, prefix, owner_id: f"{prefix}/{owner_id}/copied/{src_key.split('/')[-1]}")
    return created


def test_duplicate_copies_brief_and_owner(monkeypatch):
    src = {"project": {"id": 7, "brief_md": "le brief"}, "docs": [], "links": [], "files": []}
    created = _wire(monkeypatch, src=src)
    new_id = PJ.duplicate_project(7, "Copie", "org", "42", copied_by="u1")
    assert new_id == 101
    assert created["projects"] == [(101, "org", "42", "Copie", "le brief", "u1")]
    assert created["activity"] == [(101, "project.copy", "from #7")]


def test_duplicate_remaps_doc_tree(monkeypatch):
    # Arbre : racine(1) → enfant(2) → petit-enfant(3) + une 2e racine(4).
    docs = [
        {"id": 1, "parent_id": None, "title": "R", "body_md": "r", "kind": "doc"},
        {"id": 2, "parent_id": 1, "title": "C", "body_md": "c", "kind": "note"},
        {"id": 3, "parent_id": 2, "title": "GC", "body_md": "gc", "kind": "doc"},
        {"id": 4, "parent_id": None, "title": "R2", "body_md": "r2", "kind": "doc"},
    ]
    src = {"project": {"id": 7, "brief_md": ""}, "docs": docs, "links": [], "files": []}
    created = _wire(monkeypatch, src=src)
    PJ.duplicate_project(7, "Copie", "org", "42")
    by_title = {d["title"]: d for d in created["docs"]}
    # Hiérarchie préservée : chaque parent pointe sur le NOUVEL id de son parent.
    assert by_title["R"]["parent_id"] is None
    assert by_title["C"]["parent_id"] == by_title["R"]["new_id"]
    assert by_title["GC"]["parent_id"] == by_title["C"]["new_id"]
    assert by_title["R2"]["parent_id"] is None
    assert by_title["C"]["kind"] == "note"   # kind préservé


def test_duplicate_preserves_links(monkeypatch):
    links = [{"target_type": "connecteur", "target_ref": "fr", "label": "Entreprises",
              "role": "données société", "config": {"identity_id": "acc_1"}}]
    src = {"project": {"id": 7, "brief_md": ""}, "docs": [], "links": links, "files": []}
    created = _wire(monkeypatch, src=src)
    PJ.duplicate_project(7, "Copie", "org", "42")
    assert created["links"] == [
        (101, "connecteur", "fr", "Entreprises", "données société", {"identity_id": "acc_1"})]


def test_duplicate_copies_files_via_s3(monkeypatch):
    files = [{"id": 9, "s3_key": "project-files/7/abc/doc.pdf", "filename": "doc.pdf",
              "mime": "application/pdf", "size_bytes": 123, "title": "Doc", "description": "d",
              "public": True, "public_url": "https://x"}]
    src = {"project": {"id": 7, "brief_md": ""}, "docs": [], "links": [], "files": files}
    created = _wire(monkeypatch, src=src)
    PJ.duplicate_project(7, "Copie", "org", "42")
    assert len(created["files"]) == 1
    pid, key, fn, kw = created["files"][0]
    assert pid == 101 and fn == "doc.pdf"
    assert key == "project-files/101/copied/doc.pdf"   # nouvelle clé S3
    # public ne se propage pas : add_project_file n'accepte pas `public` → la copie repart privée.
    assert "public" not in kw
