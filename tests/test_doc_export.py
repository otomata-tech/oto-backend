"""Export arborescent markdown (oto/#6 B2)."""
import io
import zipfile

from oto_mcp import doc_export as X


def _names(zbytes):
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        return set(z.namelist())


def _read(zbytes, path):
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        return z.read(path).decode()


DOCS = [
    {"id": 1, "parent_id": None, "title": "Panorama des marchés", "body_md": "intro.", "position": 16},
    {"id": 2, "parent_id": 1, "title": "Marché A", "body_md": "détail A.", "position": 16},
    {"id": 3, "parent_id": None, "title": "Contacts", "body_md": "alice", "position": 32},
]


def test_parent_becomes_folder_with_index():
    z = X.build_export(DOCS, root_name="kb")
    names = _names(z)
    assert "kb/panorama-des-marches/_index.md" in names   # parent → dossier + _index
    assert "kb/panorama-des-marches/marche-a.md" in names  # enfant → fichier dedans
    assert "kb/contacts.md" in names                       # feuille → fichier


def test_index_carries_title_and_body():
    z = X.build_export(DOCS)
    txt = _read(z, "kb/panorama-des-marches/_index.md")
    assert txt.startswith("# Panorama des marchés") and "intro." in txt


def test_sibling_slug_dedup():
    docs = [{"id": 1, "parent_id": None, "title": "Note", "body_md": "a", "position": 1},
            {"id": 2, "parent_id": None, "title": "Note", "body_md": "b", "position": 2}]
    names = _names(X.build_export(docs))
    assert "kb/note.md" in names and "kb/note-2.md" in names


def test_empty_project_has_readme():
    assert "kb/README.md" in _names(X.build_export([]))


def test_path_traversal_is_neutralised():
    docs = [{"id": 1, "parent_id": None, "title": "../../etc/passwd", "body_md": "x"}]
    names = _names(X.build_export(docs))
    assert all(".." not in n for n in names)   # pas d'échappée de chemin
