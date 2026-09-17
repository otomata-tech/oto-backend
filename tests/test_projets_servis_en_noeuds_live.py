"""Les projets et leurs pages servis au rail et à la fiche de nœud, LUS dans leurs tables.

Depuis l'arrêt de la recopie (01/09/2026), un front branché sur `/api/me/shell` et
`/api/me/nodes/{id}` ne voyait plus aucun projet. Ce banc tient le proxy sur une vraie
base : ce qu'on vérifie, c'est ce que le rail et la fiche rendent à partir de `projects`
et `docs`, et que le rail ne range plus une ancienne copie restée dans `nodes`.
"""
import json

import pytest

from oto_mcp.capabilities import node_view as V
from oto_mcp.capabilities import shell as S
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.db import project_nodes as P
from oto_mcp.db._conn import _connect

MOI = "sub-proxy-moi"
AUTRE = "sub-proxy-autre"
CORPS = "# Titre\n\nUn paragraphe.\n\n- une\n- deux\n"


@pytest.fixture(scope="module")
def base(live):
    with _connect() as conn:
        def projet(nom, *, owner=MOI, archive=False, brief=""):
            return conn.execute(
                "INSERT INTO projects (owner_type, owner_id, name, brief_md, created_by, "
                "archived_at) VALUES ('user', %s, %s, %s, %s, "
                f"{'NOW()' if archive else 'NULL'}) RETURNING id",
                (owner, nom, brief, owner)).fetchone()["id"]

        def page(pid, titre, parent=None, corps="", position=None):
            return conn.execute(
                "INSERT INTO docs (project_id, parent_id, title, body_md, position) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (pid, parent, titre, corps, position)).fetchone()["id"]

        ids = {"prj": projet("Alpha", brief="Le brief d'Alpha.\n")}
        ids["racine"] = page(ids["prj"], "Racine", position=16)
        ids["enfant"] = page(ids["prj"], "Enfant", parent=ids["racine"], corps=CORPS)
        ids["voisine"] = page(ids["prj"], "Voisine", position=32)
        ids["archive"] = projet("Zeta archivé", archive=True)
        ids["page_archivee"] = page(ids["archive"], "Page d'un projet archivé")
        ids["autre"] = projet("Chez un autre", owner=AUTRE)
        # Une ANCIENNE COPIE du projet, telle que la recopie d'avant le 01/09 la déposait.
        ids["copie"] = conn.execute(
            "INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            "VALUES ('nod_copie_figee_0000000000', 'page', 'user', %s, %s) RETURNING public_id",
            (MOI, json.dumps({"legacy": "prj", "legacy_id": ids["prj"],
                              "title": "Alpha figé au 01/09", "pinned": True}))
        ).fetchone()["public_id"]
        ids["natif"] = conn.execute(
            "INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
            "VALUES ('nod_natif_proxy_000000000', 'page', 'user', %s, %s) RETURNING public_id",
            (MOI, json.dumps({"title": "Page native"}))).fetchone()["public_id"]
    return ids


def _prive(corps: dict) -> list[dict]:
    return next(s for s in corps["sections"] if s["kind"] == "private")["nodes"]


def test_le_rail_range_le_projet_et_ses_pages_sans_la_copie(base):
    rail = _prive(S._compose(ResolvedCtx(sub=MOI)))
    par_nom = {n["name"]: n for n in rail}

    assert "Page native" in par_nom, "le proxy ne doit pas évincer un nœud natif"
    assert "Alpha figé au 01/09" not in par_nom, "une ancienne copie est servie"
    assert "Zeta archivé" not in par_nom
    assert "Chez un autre" not in par_nom

    alpha = par_nom["Alpha"]
    assert alpha["id"] == P.public_id("prj", base["prj"])
    assert [c["name"] for c in alpha["children"]] == ["Racine", "Voisine"]
    racine = alpha["children"][0]
    assert racine["doc_id"] == base["racine"]
    assert [c["name"] for c in racine["children"]] == ["Enfant"]


def test_la_fiche_d_une_page_rend_son_corps_son_fil_et_ses_poignees(base):
    fiche = V._compose(ResolvedCtx(sub=MOI), P.public_id("doc", base["enfant"]))

    assert fiche["edit_surface"] == "doc"
    assert fiche["doc_id"] == base["enfant"]
    assert fiche["project_id"] == base["prj"]
    assert fiche["name"] == "Enfant"
    assert "".join(b["md"] for b in fiche["body"]) == CORPS
    assert [b.get("role") for b in fiche["body"]] == ["heading", "paragraph", "list"]
    assert [m["name"] for m in fiche["trail"]] == ["Alpha", "Racine", "Enfant"]
    freres_racine = [s["name"] for s in fiche["trail"][1]["siblings"]]
    assert freres_racine == ["Racine", "Voisine"]


def test_la_fiche_d_un_projet_rend_son_brief(base):
    fiche = V._compose(ResolvedCtx(sub=MOI), P.public_id("prj", base["prj"]))

    assert fiche["edit_surface"] == "project"
    assert fiche["project_id"] == base["prj"]
    assert fiche["doc_id"] is None
    assert fiche["pinned"] is True
    assert "".join(b["md"] for b in fiche["body"]) == "Le brief d'Alpha.\n"


def test_la_fiche_se_relit_a_jour_quand_la_page_change(base):
    ident = P.public_id("doc", base["voisine"])
    avant = V._compose(ResolvedCtx(sub=MOI), ident)
    with _connect() as conn:
        conn.execute("UPDATE docs SET body_md = 'Écrit par oto_doc.\n' WHERE id = %s",
                     (base["voisine"],))
    apres = V._compose(ResolvedCtx(sub=MOI), ident)

    assert apres["body"][0]["md"] == "Écrit par oto_doc.\n"
    assert apres["rev"] != avant["rev"]


@pytest.mark.parametrize("cas", ["autre_personne", "inconnu", "projet_archive"])
def test_un_refus_est_le_meme_404_qu_un_inconnu(base, cas):
    ctx, ident = {
        "autre_personne": (ResolvedCtx(sub=AUTRE), P.public_id("doc", base["enfant"])),
        "inconnu": (ResolvedCtx(sub=MOI), P.public_id("doc", 987654321)),
        "projet_archive": (ResolvedCtx(sub=MOI), P.public_id("doc", base["page_archivee"])),
    }[cas]
    with pytest.raises(AuthzDenied) as refus:
        V._compose(ctx, ident)
    assert refus.value.status == 404


@pytest.mark.parametrize("ident", ["nod_prj_", "nod_doc_12a", "nod_abc_12", "", "prj_12"])
def test_un_identifiant_hors_forme_n_est_pas_une_cle(ident):
    assert P.cle_de(ident) is None


def test_la_cle_se_relit_depuis_l_identifiant():
    assert P.cle_de(P.public_id("doc", 42)) == ("doc", 42)
    assert P.cle_de(P.public_id("prj", 7)) == ("prj", 7)
