"""`scripts/rotation_jetons_journal.py` sur une vraie base : à blanc, puis appliqué.

Le script fait tourner les jetons LONGS qui ont pu s'écrire en clair au journal
d'accès avant la v1.391.0 : le lien public d'une page, l'invitation en attente émise
avant la bascule. Ce banc vérifie ce que le STOCKAGE porte après coup — qu'à blanc
rien ne bouge, que l'ancien lien n'ouvre plus rien, qu'une invitation émise APRÈS la
bascule reste, et qu'aucun jeton n'atterrit dans la sortie ni dans la sauvegarde.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[1]
ALICE = "usr_rotation_journal_alice"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "rotation_jetons_journal", RACINE / "scripts/rotation_jetons_journal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def monde(live):
    from oto_mcp import db, org_store
    from oto_mcp.db._conn import _connect

    db.upsert_user(ALICE, email=f"{ALICE}@t.invalid", name=ALICE)
    org = org_store.create_org("Org rotation", created_by=ALICE)
    org_store.add_org_member(org, ALICE, "org_member")
    projet = db.create_project("org", str(org), "Partage", created_by=ALICE)
    doc = db.create_doc(projet, "Page partagée", body_md="contenu", created_by=ALICE)
    lien = db.set_doc_public(doc, True)
    avant_id, avant_jeton = org_store.create_invitation(
        org, "avant@t.invalid", "org_member", invited_by=ALICE)
    apres_id, _ = org_store.create_invitation(
        org, "apres@t.invalid", "org_member", invited_by=ALICE)
    with _connect() as conn:
        conn.execute("UPDATE org_invitations SET created_at = '2026-09-28T12:00:00Z', "
                     "expires_at = NOW() + interval '5 days' WHERE id = %s", (avant_id,))
        conn.execute("UPDATE org_invitations SET created_at = NOW() WHERE id = %s",
                     (apres_id,))
    yield {"org": org, "projet": projet, "doc": doc, "lien": lien,
           "avant": avant_id, "avant_jeton": avant_jeton, "apres": apres_id}
    with _connect() as conn:
        conn.execute("DELETE FROM org_invitations WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM projects WHERE id = %s", (projet,))


def _invitations(org) -> set:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return {r["id"] for r in conn.execute(
            "SELECT id FROM org_invitations WHERE org_id = %s", (org,)).fetchall()}


def test_a_blanc_rien_ne_bouge_et_rien_ne_fuit(monde, script, capsys, tmp_path):
    from oto_mcp import db
    assert script.main(["x", "--details", "--sauvegarde", str(tmp_path)]) == 0
    sortie = capsys.readouterr().out
    assert "À BLANC" in sortie
    assert monde["lien"] not in sortie and monde["avant_jeton"] not in sortie
    assert "avant@t.invalid" not in sortie, "à blanc, des comptes — jamais une adresse"
    assert db.get_doc_by_public_token(monde["lien"]) is not None
    assert _invitations(monde["org"]) == {monde["avant"], monde["apres"]}
    assert not list(tmp_path.iterdir()), "à blanc, aucune sauvegarde n'est écrite"


def test_applique_retire_le_lien_et_revoque_l_invitation_d_avant(monde, script,
                                                                  capsys, tmp_path):
    """Le lien est retiré sans réémission : rangé haché, un lien neuf ne serait montré
    à personne — c'est au propriétaire de rendre la page publique à nouveau."""
    from oto_mcp import db
    assert script.main(["x", "--appliquer", "--sauvegarde", str(tmp_path)]) == 0
    sortie = capsys.readouterr().out
    assert db.get_doc_by_public_token(monde["lien"]) is None, "l'ancien lien est mort"
    assert db.get_doc_by_id(monde["doc"])["public"] is False
    assert _invitations(monde["org"]) == {monde["apres"]}, \
        "seule l'invitation émise AVANT la bascule est révoquée"
    assert monde["lien"] not in sortie
    texte = next(tmp_path.iterdir()).read_text()
    assert monde["lien"] not in texte, \
        "la sauvegarde ne garde aucun lien : restaurer un lien exposé défait la rotation"
    assert {e["section"] for e in json.loads(texte)} == {"docs", "invitations"}


def test_seulement_docs_ne_touche_pas_aux_invitations(monde, script, tmp_path):
    from oto_mcp import db
    assert script.main(["x", "--appliquer", "--seulement", "docs",
                        "--sauvegarde", str(tmp_path)]) == 0
    assert db.get_doc_by_id(monde["doc"])["public"] is False
    assert _invitations(monde["org"]) == {monde["avant"], monde["apres"]}, \
        "--seulement docs ne touche pas aux invitations"
