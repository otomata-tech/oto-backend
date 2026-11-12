"""Une liste rend son INDEX, jamais les corps — et un budget le prouve.

Deux signaux d'usage à deux jours d'écart : `oto_doc op=list` a rendu 201 170 caractères
pour 37 pages, `oto_project op=list` 73 K pour 26 projets. Les deux ont dépassé le plafond
d'un tool result ; le client a dû déverser en fichier puis reparser au `jq`. Un agent sans
shell — client MCP nu, n8n — n'a pas cette échappatoire : pour lui, l'appel échoue.

Une règle écrite ne tient pas ce genre d'invariant : c'est le genre de champ qu'on rajoute
sans y penser. D'où un **budget** — le retour d'une liste croît avec le NOMBRE d'éléments,
jamais avec la taille de leur contenu. Un corps réintroduit dans la vue par défaut fait
exploser l'écart entre les deux mesures ci-dessous, et le test tombe.
"""
import json

import pytest

from oto_mcp import db, output_projection, ownership
from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.docs import core as D, view as docs_view

CTX = ResolvedCtx(sub="u1", org_id=None)

# Un corps réaliste de page de KB (~4 000 caractères), la taille qui a fait déborder.
BODY = "Contexte et procédure interne. " * 130


def _pages(n: int) -> list[dict]:
    return [{"id": i, "project_id": 7, "parent_id": None, "title": f"Page {i}",
             "description": None, "position": i, "body_md": BODY, "kind": "doc",
             "created_at": "2026-08-14", "updated_at": "2026-08-14",
             "updated_by": "google-oauth2|104857312345678901234"} for i in range(n)]


@pytest.fixture
def docs_seam(monkeypatch):
    monkeypatch.setattr(ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(docs_view, "public_doc_url", lambda tok, sub: None)

    def _list(pid, rows=_pages(37)):
        return rows
    monkeypatch.setattr(db, "list_docs_for_project", _list)
    # La liste par défaut lit l'arbre de TRAVAIL et compte les sources laissées (12/11/2026).
    monkeypatch.setattr(db, "list_working_docs_for_project", _list)
    monkeypatch.setattr(db, "count_live_sources", lambda pid: 0)
    monkeypatch.setattr(db, "doc_rev", lambda t, b: "rev")


def _size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def test_l_index_des_pages_ne_porte_pas_les_corps(docs_seam):
    out = D._doc(CTX, D.DocInput(op="list", project_id=7))
    assert all("body_md" not in d for d in out["docs"])
    # La TAILLE remplace le corps : l'agent sait ce qu'il n'a pas (≠ un extrait tronqué,
    # qui le laisserait croire qu'il a lu).
    assert all(d["body_md_length"] == len(BODY) for d in out["docs"])
    assert out["projection"]["omitted"] == ["body_md"]


def test_le_budget_d_une_liste_suit_le_NOMBRE_de_pages_pas_leur_taille(docs_seam):
    out = D._doc(CTX, D.DocInput(op="list", project_id=7))
    brut = D._doc(CTX, D.DocInput(op="list", project_id=7, fields=["*"]))
    # 37 pages de 4 000 c. : le brut dépasse le plafond d'un tool result, l'index tient.
    assert _size(brut) > 140_000
    # 24/09/2026 (oto#274) : 12 000 → 14 000 et 320 → 380 par élément. La ligne porte
    # désormais `updated_by` (l'auteur de la dernière modification, que l'écran affiche
    # dans la liste), un identifiant de compte d'une quarantaine de caractères : mesuré
    # 13 238 (358 par page) avec un identifiant réaliste, contre ~11 300 avant. Un
    # champ par ligne, pas un corps : le budget suit toujours le NOMBRE de pages.
    assert _size(out) < 14_000
    # Et le budget par élément est BORNÉ, quelle que soit la page.
    assert _size(out) / 37 < 380


def test_le_brut_reste_atteignable_et_ne_ment_pas(docs_seam):
    brut = D._doc(CTX, D.DocInput(op="list", project_id=7, fields=["*"]))
    assert all(d["body_md"] == BODY for d in brut["docs"])
    # Rien n'a été écarté ⟹ pas de bloc `projection` : il ne s'affiche que quand il a
    # quelque chose à annoncer.
    assert "projection" not in brut


def test_une_projection_nommee_garde_de_quoi_ADRESSER_la_page(docs_seam):
    out = D._doc(CTX, D.DocInput(op="list", project_id=7, fields=["kind"]))
    d = out["docs"][0]
    # `id`/`project_id`/`parent_id`/`title` survivent même non demandés : une liste dont
    # les lignes ne sont pas adressables ne sert à rien.
    assert {"id", "project_id", "parent_id", "title", "kind"} <= set(d)
    assert "position" not in d and "created_at" not in d
    assert "position" in out["projection"]["omitted"]


def test_fields_vide_est_refuse_plutot_qu_avale(docs_seam):
    with pytest.raises(AuthzDenied) as e:
        D._doc(CTX, D.DocInput(op="list", project_id=7, fields=[]))
    assert e.value.code == "empty_fields"


def test_l_index_des_projets_ne_porte_ni_brief_ni_prose_d_endpoint():
    rows = [{"id": i, "name": f"Projet {i}", "brief_md": BODY, "owner_type": "user",
             "owner_id": "u1", "mcp_instructions_md": BODY} for i in range(26)]
    out = P._projected(list(rows), None)
    assert all("brief_md" not in p and "mcp_instructions_md" not in p for p in out["projects"])
    assert all(p["brief_md_length"] == len(BODY) for p in out["projects"])
    assert _size(out) < 6_000              # mesuré à 73 K avec les briefs
    assert out["projection"]["omitted"] == ["brief_md", "mcp_instructions_md"]

    brut = P._projected(list(rows), ["*"])
    assert all(p["brief_md"] == BODY for p in brut["projects"])


def test_une_colonne_corps_explicitement_demandee_est_servie_entiere():
    rows = [{"id": 1, "name": "P", "brief_md": BODY, "owner_type": "user", "owner_id": "u1",
             "mcp_instructions_md": BODY}]
    out = P._projected(rows, ["brief_md"])
    p = out["projects"][0]
    assert p["brief_md"] == BODY                     # demandé ⟹ entier, jamais coupé
    assert p["mcp_instructions_md_length"] == len(BODY)   # l'autre corps reste une taille
    assert "mcp_instructions_md" not in p


def test_la_taille_est_rendue_meme_quand_la_colonne_manque():
    # « corps vide » et « colonne absente de la ligne » doivent se lire pareil pour qui
    # trie — sinon l'absence de la clé passe pour un corps non mesuré.
    rows, notice = output_projection.summarize([{"id": 1}], body_fields=("body_md",))
    assert rows[0]["body_md_length"] == 0
    assert notice is None      # rien n'a été RETIRÉ : il n'y avait pas de corps à écarter
