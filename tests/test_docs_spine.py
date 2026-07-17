"""Ship 2 (lot 3) — chapôs + ordre curé + épine.

- `derive_description` : fallback chapô à la LECTURE (jamais stocké) — première
  ligne de prose, markdown strippé, « vide plutôt que déchet ».
- `project_spine` : arbre ordonné borné (profondeur + plafond nœuds + compteurs
  `more`), enracinable (`from_doc` = drill). Rows stubées via _connect factice.
- `oto_doc(op=move)` : `position` = index de fratrie (réordonner sans reparenter).
"""
from oto_mcp.db import projects as P


# ── derive_description ───────────────────────────────────────────────────────

def test_derive_skips_headings_and_noise():
    body = "# Titre\n\n```python\ncode\n```\n\n| a | b |\n\n**La** _vraie_ ligne de `prose` utile."
    assert P.derive_description(body) == "La vraie ligne de prose utile."


def test_derive_strips_links_and_lists():
    body = "- [Guide complet](https://x.y) du processus d'onboarding"
    assert P.derive_description(body) == "Guide complet du processus d'onboarding"


def test_derive_empty_rather_than_junk():
    assert P.derive_description("# Juste un titre\n\n---\n") == ""
    assert P.derive_description("") == ""
    assert P.derive_description("ok") == ""          # < 8 chars = pas probant


def test_derive_caps_long_lines():
    out = P.derive_description("Une très longue ligne. " * 20)
    assert len(out) <= 141 and out.endswith("…")


# ── project_spine (rows stubées) ─────────────────────────────────────────────

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        rows = self._rows

        class _Cur:
            def fetchall(self):
                return rows
        return _Cur()


def _doc(id, parent, title, desc=None, body=""):
    return {"id": id, "parent_id": parent, "title": title,
            "description": desc, "body_md": body}


def _wire(monkeypatch, rows):
    monkeypatch.setattr(P, "_connect", lambda: _FakeConn(rows))


def test_spine_ordered_tree_with_fallback_descriptions(monkeypatch):
    _wire(monkeypatch, [
        _doc(1, None, "Racine A", desc="Chapô curé"),
        _doc(2, None, "Racine B", body="Premier paragraphe de la page B."),
        _doc(3, 1, "Enfant A1"),
    ])
    out = P.project_spine(9)
    assert out["pages"] == 3
    assert [n["title"] for n in out["tree"]] == ["Racine A", "Racine B"]
    assert out["tree"][0]["description"] == "Chapô curé"
    assert out["tree"][1]["description"] == "Premier paragraphe de la page B."
    assert out["tree"][0]["children"][0]["title"] == "Enfant A1"


def test_spine_depth_bound_with_more_counter(monkeypatch):
    # chaîne 1←2←3←4 : depth=2 = racine + 2 niveaux (N+2) → N3 rendu SANS enfants,
    # `more` compte le sous-arbre coupé (N4).
    _wire(monkeypatch, [
        _doc(1, None, "N1"), _doc(2, 1, "N2"), _doc(3, 2, "N3"), _doc(4, 3, "N4"),
    ])
    out = P.project_spine(9, depth=2)
    n3 = out["tree"][0]["children"][0]["children"][0]
    assert n3["title"] == "N3" and "children" not in n3 and n3["more"] == 1


def test_spine_drill_from_doc(monkeypatch):
    _wire(monkeypatch, [
        _doc(1, None, "N1"), _doc(2, 1, "N2"), _doc(3, 2, "N3"),
    ])
    out = P.project_spine(9, from_doc=2, depth=3)
    assert [n["title"] for n in out["tree"]] == ["N2"]
    assert out["tree"][0]["children"][0]["title"] == "N3"
    assert out["root_doc"] == 2


def test_spine_max_nodes_truncates(monkeypatch):
    rows = [_doc(i, None, f"Page {i:03d}") for i in range(1, 40)]
    _wire(monkeypatch, rows)
    out = P.project_spine(9, max_nodes=10)
    assert len(out["tree"]) == 10 and out["pages"] == 39


# ── op=move : position = index de fratrie ────────────────────────────────────

def test_move_reorder_without_reparent(monkeypatch):
    from oto_mcp.capabilities import docs as D
    from oto_mcp.capabilities._types import ResolvedCtx
    calls = {}
    monkeypatch.setattr(D, "_can", lambda sub, pid, want: True)
    monkeypatch.setattr(D.db, "get_doc_by_id",
                        lambda did: {"id": did, "project_id": 7, "parent_id": 5,
                                     "title": "T", "kind": "doc"})
    monkeypatch.setattr(D.db, "move_doc",
                        lambda did, parent, position=None: calls.update(
                            did=did, parent=parent, position=position))
    D._doc(ResolvedCtx(sub="u1", org_id=1),
           D.DocInput(op="move", doc_id=3, position=0))
    # position sans parent_id ⇒ réordonner DANS la fratrie courante (parent 5 conservé)
    assert calls == {"did": 3, "parent": 5, "position": 0}
