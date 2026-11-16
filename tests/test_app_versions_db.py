"""`db.app_versions` — brouillon et publication, joués sur une vraie base.

Ce que le STOCKAGE porte : le brouillon né de l'état vivant, son ETag à lui, le refus
d'une écriture qui nomme la version publiée quand un brouillon existe, la publication
qui garde l'état remplacé et vide le brouillon en une transaction, les versions
publiées que la purge des révisions ordinaires ne touche pas, et l'état vivant qui perd
sa marque de version publiée quand on l'écrit en direct.
"""
from __future__ import annotations

import pytest

from oto_mcp import db

MOI = "sub-app-versions-test"


@pytest.fixture(scope="module")
def projet(live):
    return int(db.create_project("user", MOI, "Versions", created_by=MOI))


def _app(projet, html="<p>1</p>"):
    """Une app neuve, passée en mode brouillon."""
    app = db.apps.create(("user", MOI), None, project_id=projet, title="V",
                         manifest={"tables": {}}, html=html, thumbnail=None, by=MOI)
    return db.app_versions.set_enabled(app["id"], True)


def test_pas_de_brouillon_dans_une_app_qui_n_en_a_pas(projet):
    """Une app sans brouillon écrit en direct : un brouillon posé à côté vieillirait
    sous ses écritures, et le publier les déferait. Refusé, rien n'est écrit."""
    app = db.apps.create(("user", MOI), None, project_id=projet, title="V",
                         manifest={"tables": {}}, html="<p>1</p>", thumbnail=None, by=MOI)
    assert app["drafts_enabled"] is False
    with pytest.raises(db.app_versions.DraftsOff):
        db.app_versions.write_draft(app["id"], by=MOI, html="<p>2</p>")
    assert db.app_versions.draft(app["id"]) is None


def test_le_brouillon_nait_de_l_etat_vivant_sans_le_toucher(projet):
    app = _app(projet)
    assert db.app_versions.draft(app["id"]) is None
    d = db.app_versions.write_draft(app["id"], by="autre", expected_rev=app["rev"],
                                    html="<p>2</p>")
    assert (d["html"], d["manifest"], d["by"]) == ("<p>2</p>", {"tables": {}}, "autre")
    assert d["rev"] != app["rev"] and d["rev"].startswith("d")
    vivant = db.apps.get(app["id"])
    assert vivant["html"] == "<p>1</p>" and vivant["rev"] == app["rev"]
    assert db.apps.revisions(app["id"]) == []


def test_ecrire_contre_la_version_publiee_quand_un_brouillon_existe(projet):
    app = _app(projet)
    d = db.app_versions.write_draft(app["id"], by=MOI, html="<p>2</p>")
    with pytest.raises(db.app_versions.DraftExists) as e:
        db.app_versions.write_draft(app["id"], by=MOI, expected_rev=app["rev"], html="<p>3</p>")
    assert e.value.current_rev == d["rev"]
    with pytest.raises(db.app_versions.DraftConflict):
        db.app_versions.write_draft(app["id"], by=MOI, expected_rev="dautre", html="<p>3</p>")
    assert db.app_versions.draft(app["id"])["html"] == "<p>2</p>"
    d2 = db.app_versions.write_draft(app["id"], by=MOI, expected_rev=d["rev"], html="<p>3</p>")
    assert d2["html"] == "<p>3</p>"


def test_publier_garde_l_etat_remplace_et_vide_le_brouillon(projet):
    app = _app(projet)
    d = db.app_versions.write_draft(app["id"], by=MOI, html="<p>2</p>")
    with pytest.raises(db.app_versions.DraftConflict):
        db.app_versions.publish(app["id"], by="pub", expected_rev="d0", manifest={"tables": {}})
    out = db.app_versions.publish(app["id"], by="pub", expected_rev=d["rev"],
                                  manifest={"tables": {}}, note="v2")
    assert (out["html"], out["updated_by"], out["version_note"]) == ("<p>2</p>", "pub", "v2")
    assert out["published_at"] is not None
    assert db.app_versions.draft(app["id"]) is None
    (r,) = db.apps.revisions(app["id"])
    assert db.apps.revision(app["id"], r["id"])["html"] == "<p>1</p>"
    with pytest.raises(db.app_versions.NoDraft):
        db.app_versions.publish(app["id"], by="pub", expected_rev=None, manifest={})


def test_les_versions_publiees_echappent_a_la_purge(projet, monkeypatch):
    monkeypatch.setattr(db.apps, "MAX_REVISIONS", 2)
    app = _app(projet)
    d = db.app_versions.write_draft(app["id"], by=MOI, html="<p>pub</p>")
    db.app_versions.publish(app["id"], by=MOI, expected_rev=d["rev"], manifest={}, note="kept")
    # Une écriture directe du HTML : l'état vivant n'est plus la version publiée…
    v = db.apps.update(app["id"], by=MOI, html="<p>a</p>")
    assert v["published_at"] is None and v["version_note"] is None
    # …mais la version publiée remplacée reste dans l'historique, marquée, malgré la purge.
    for i in range(5):
        db.apps.update(app["id"], by=MOI, html=f"<p>{i}</p>")
    revs = db.apps.revisions(app["id"])
    publiees = [r for r in revs if r["published_at"] is not None]
    assert [r["note"] for r in publiees] == ["kept"]
    assert len([r for r in revs if r["published_at"] is None]) == 2


def test_changer_la_carte_garde_la_marque_de_version_publiee(projet):
    app = _app(projet)
    d = db.app_versions.write_draft(app["id"], by=MOI, html="<p>2</p>")
    db.app_versions.publish(app["id"], by=MOI, expected_rev=d["rev"], manifest={}, note="n")
    v = db.apps.update(app["id"], by=MOI, title="Renamed")
    assert v["published_at"] is not None and v["version_note"] == "n"
    # La version publiée reste VIVANTE : l'état gardé par ce changement de carte n'est
    # pas une deuxième copie épinglée (elle userait la borne des versions publiées).
    dernier = db.apps.revisions(app["id"])[0]
    assert dernier["published_at"] is None and dernier["note"] is None


def test_une_revision_dit_quand_son_etat_a_ete_ecrit(projet):
    app = _app(projet)
    db.apps.update(app["id"], by=MOI, html="<p>2</p>")
    (r,) = db.apps.revisions(app["id"])
    assert r["saved_at"] == app["updated_at"]
    assert r["created_at"] >= r["saved_at"]


def test_couper_le_brouillon_avec_un_brouillon_en_cours(projet):
    app = _app(projet)
    assert app["drafts_enabled"] is True
    d = db.app_versions.write_draft(app["id"], by=MOI, html="<p>2</p>")
    with pytest.raises(db.app_versions.DraftPending):
        db.app_versions.set_enabled(app["id"], False)
    assert db.app_versions.delete_draft(app["id"], expected_rev=d["rev"]) is True
    assert db.app_versions.delete_draft(app["id"]) is False
    assert db.app_versions.set_enabled(app["id"], False)["drafts_enabled"] is False


def test_la_carte_de_liste_ne_porte_jamais_le_brouillon(projet):
    app = _app(projet)
    db.app_versions.write_draft(app["id"], by=MOI, html="<p>secret draft</p>")
    cartes = db.apps.list_for_projects([projet])
    carte = next(c for c in cartes if c["id"] == app["id"])
    contenu = {"draft_manifest", "draft_html", "draft_by", "draft_at"}
    assert not contenu & set(carte)
    # `get` sert la version publiée à tout lecteur : jamais le brouillon.
    assert not contenu & set(db.apps.get(app["id"]))
    assert "secret draft" not in str(db.apps.get(app["id"]))


def test_deux_publications_du_meme_brouillon_une_seule_passe(projet):
    """Deux éditeurs publient le même brouillon : la seconde, servie après la première
    sous le verrou de la ligne, trouve le brouillon vidé et est refusée — l'état publié
    par la première n'est ni écrasé ni gardé deux fois."""
    app = _app(projet)
    d = db.app_versions.write_draft(app["id"], by=MOI, html="<p>2</p>")
    db.app_versions.publish(app["id"], by="a", expected_rev=d["rev"], manifest={}, note="a")
    with pytest.raises(db.app_versions.NoDraft):
        db.app_versions.publish(app["id"], by="b", expected_rev=d["rev"], manifest={}, note="b")
    vivant = db.apps.get(app["id"])
    assert (vivant["html"], vivant["updated_by"], vivant["version_note"]) == ("<p>2</p>", "a", "a")
    assert len(db.apps.revisions(app["id"])) == 1
