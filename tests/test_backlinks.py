"""Backlinks [[…]] (lot 3 Ship 4) — extraction, résolution (précédence projet > KB,
N=0 souche, N>1 déterministe, pas d'auto-citation), hook db, op=backlinks filtré.

Extraction = pure. Résolution/hook = _connect factice (rows en mémoire).
"""
import pytest

from oto_mcp.db import backlinks as B


# ── extraction ───────────────────────────────────────────────────────────────

def test_extract_titles_dedup_and_normalize():
    body = "Voir [[Mūcho]] et [[ mūcho ]] puis [[Deal X]].\nEncore [[Deal X]]."
    # casse/espaces normalisés pour la clé → « Mūcho » une fois ; ordre d'apparition
    assert B.extract_titles(body) == ["Mūcho", "Deal X"]


def test_extract_ignores_empty_and_multiline():
    assert B.extract_titles("[[]] [[ \t ]] texte [[OK ici]]") == ["OK ici"]
    assert B.extract_titles("pas de lien") == []
    assert B.extract_titles("") == []


# ── résolution (conn factice) ────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows): self._rows = rows
    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return self._rows


class _Conn:
    """Renvoie des rows scénarisés par motif SQL ; enregistre les INSERT doc_links."""
    def __init__(self, *, project=None, kb=None, docs=None):
        self.project = project        # row projects (owner_type/owner_id/context_org_id)
        self.kb = kb                  # kb_project_id de l'org
        self.docs = docs or []        # docs candidats (id/project_id/title)
        self.inserted: list[tuple] = []
        self.deleted = False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("DELETE FROM doc_links"):
            self.deleted = True
            return _Cur([])
        if "FROM projects WHERE id" in s:
            return _Cur([self.project] if self.project else [])
        if "kb_project_id FROM orgs" in s:
            return _Cur([{"kb_project_id": self.kb}] if self.kb is not None else [{"kb_project_id": None}])
        if "FROM docs WHERE project_id = ANY" in s:
            scope = params[0]
            return _Cur([d for d in self.docs if d["project_id"] in scope])
        if s.startswith("INSERT INTO doc_links"):
            self.inserted.append((params[0], params[1]))
            return _Cur([])
        if "FROM doc_links l JOIN docs d" in s:
            return _Cur(self.docs)
        return _Cur([])


def _proj(owner_type="org", owner_id="7", ctx=None):
    return {"owner_type": owner_type, "owner_id": owner_id, "context_org_id": ctx}


def test_resolve_precedence_project_over_kb():
    # « Mūcho » existe dans le projet courant (1) ET la KB (9) → le projet gagne.
    c = _Conn(project=_proj(), kb=9, docs=[
        {"id": 100, "project_id": 1, "title": "Mūcho"},
        {"id": 200, "project_id": 9, "title": "Mūcho"},
    ])
    B.refresh_links(c, from_doc=5, project_id=1, body_md="cf [[Mūcho]]")
    assert c.deleted and c.inserted == [(5, 100)]


def test_resolve_falls_back_to_kb():
    c = _Conn(project=_proj(), kb=9, docs=[
        {"id": 200, "project_id": 9, "title": "Mūcho"},
    ])
    B.refresh_links(c, from_doc=5, project_id=1, body_md="[[mucho]]" )  # casse/accent ? -> non
    # 'mucho' (sans accent) ne matche pas 'Mūcho' → lien-souche, rien
    assert c.inserted == []
    c2 = _Conn(project=_proj(), kb=9, docs=[{"id": 200, "project_id": 9, "title": "Mūcho"}])
    B.refresh_links(c2, from_doc=5, project_id=1, body_md="[[Mūcho]]")
    assert c2.inserted == [(5, 200)]


def test_ambiguity_picks_lowest_id_same_tier():
    c = _Conn(project=_proj(), kb=None, docs=[
        {"id": 30, "project_id": 1, "title": "Note"},
        {"id": 12, "project_id": 1, "title": "Note"},
    ])
    B.refresh_links(c, from_doc=5, project_id=1, body_md="[[Note]]")
    assert c.inserted == [(5, 12)]           # N>1 même tier → plus petit id, JAMAIS création


def test_no_self_citation():
    c = _Conn(project=_proj(), kb=None, docs=[{"id": 5, "project_id": 1, "title": "Moi"}])
    B.refresh_links(c, from_doc=5, project_id=1, body_md="[[Moi]]")
    assert c.inserted == []


def test_stub_when_absent_still_clears_old():
    c = _Conn(project=_proj(), kb=None, docs=[])
    B.refresh_links(c, from_doc=5, project_id=1, body_md="[[Inconnu]]")
    assert c.deleted and c.inserted == []    # N=0 = souche (UI), rien stocké


def test_member_project_uses_context_org_kb():
    # projet perso (user) avec context_org_id → KB de cette org.
    c = _Conn(project=_proj(owner_type="user", owner_id="sub1", ctx=7), kb=9, docs=[
        {"id": 200, "project_id": 9, "title": "Charte"},
    ])
    B.refresh_links(c, from_doc=5, project_id=1, body_md="[[Charte]]")
    assert c.inserted == [(5, 200)]


# ── op=backlinks : filtrage d'accès ──────────────────────────────────────────

def test_op_backlinks_filters_unreadable_projects(monkeypatch):
    from oto_mcp.capabilities import docs as D
    from oto_mcp.capabilities._types import ResolvedCtx
    monkeypatch.setattr(D.db, "get_doc_by_id",
                        lambda did: {"id": did, "project_id": 1, "title": "Cible"})
    monkeypatch.setattr(D.db, "doc_backlinks", lambda did: [
        {"id": 10, "project_id": 1, "title": "Page lisible"},
        {"id": 11, "project_id": 99, "title": "Page d'un projet interdit"},
    ])
    # lisible : projet 1 ; interdit : projet 99
    monkeypatch.setattr(D.ownership, "can_access",
                        lambda sub, t, rid, want="read": str(rid) == "1")
    out = D._doc(ResolvedCtx(sub="u1", org_id=1), D.DocInput(op="backlinks", doc_id=5))
    assert out["count"] == 1 and out["backlinks"][0]["id"] == 10
