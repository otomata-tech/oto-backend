"""Brouillon, publication, lecture d'une version (`capabilities/apps_versions.py`).

Une app à brouillon garde ses lecteurs sur sa version PUBLIÉE : le manifeste et le HTML
écrits vont au brouillon, que ses éditeurs seuls ouvrent ; publier le rend vivant, sous
le nom de qui publie, après avoir rejugé son manifeste. Ce qu'on vérifie : où chaque
écriture va, qui voit quoi, et ce qui est refusé — par le CODE du refus. Les seams de
`test_apps.py` (le store, l'ownership, l'org) ; le brouillon est un double en mémoire.
"""
import pytest

from oto_mcp import db
from oto_mcp.capabilities import apps as A
from oto_mcp.capabilities.copilot import classement
from oto_mcp.tools import effets
from test_apps import CTX, GOOD, ROW, _refus, seams  # noqa: F401 — la fixture `seams`


@pytest.fixture
def brouillons(seams, monkeypatch):  # noqa: F811
    """Un brouillon en mémoire pour l'app 5, et l'app passée en mode brouillon."""
    etat = {"draft": None, "writes": [], "published": [], "deleted": 0, "enabled": []}
    monkeypatch.setattr(db.apps, "get", lambda i: dict(ROW, id=i, drafts_enabled=etat.get(
        "mode", True)) if i == 5 else None)
    monkeypatch.setattr(db.app_versions, "draft", lambda i: etat["draft"])

    def write_draft(app_id, *, by, expected_rev=None, manifest=None, html=None):
        if etat.get("raise"):
            raise etat["raise"]
        base = etat["draft"] or {"manifest": ROW["manifest"], "html": ROW["html"]}
        etat["writes"].append({"by": by, "expected_rev": expected_rev, "manifest": manifest,
                               "html": html})
        etat["draft"] = {"manifest": manifest if manifest is not None else base["manifest"],
                         "html": html if html is not None else base["html"], "by": by,
                         "at": "t", "rev": "d-new"}
        return etat["draft"]

    def publish(app_id, *, by, expected_rev, manifest, note=None):
        if expected_rev != etat["draft"]["rev"]:
            raise db.app_versions.DraftConflict(etat["draft"]["rev"])
        etat["published"].append({"by": by, "manifest": manifest, "note": note})
        return dict(ROW, html=etat["draft"]["html"], manifest=manifest, updated_by=by)

    def set_enabled(app_id, enabled):
        if not enabled and etat["draft"] is not None:
            raise db.app_versions.DraftPending()
        etat["enabled"].append(enabled)
        return dict(ROW, drafts_enabled=enabled)

    monkeypatch.setattr(db.app_versions, "write_draft", write_draft)
    monkeypatch.setattr(db.app_versions, "publish", publish)
    monkeypatch.setattr(db.app_versions, "set_enabled", set_enabled)
    monkeypatch.setattr(db.app_versions, "delete_draft",
                        lambda i, expected_rev=None: etat.__setitem__("deleted", 1) or True)
    etat["seams"] = seams
    return etat


def _app(**kw):
    return A._app(CTX, A.AppInput(app_id=5, **kw))


def _brouillon(**kw):
    return {"manifest": {}, "html": "<p>draft</p>", "by": "u1", "at": "t", "rev": "d1", **kw}


# ── Qui voit le brouillon ───────────────────────────────────────────────────

def test_get_nomme_le_brouillon_a_l_editeur_seul(brouillons):
    brouillons["draft"] = _brouillon()
    out = _app(op="get")
    assert out["draft"] == {"rev": "d1", "updated_by": "u1", "updated_at": "t"}
    # L'app servie reste la version publiée.
    assert out["app"]["html"] == ROW["html"]
    brouillons["seams"]["rights"]["write"] = False
    assert "draft" not in _app(op="get")


@pytest.mark.parametrize("existe", [True, False])
def test_un_lecteur_ne_sait_pas_qu_un_brouillon_existe(brouillons, existe):
    brouillons["draft"] = _brouillon() if existe else None
    brouillons["seams"]["rights"]["write"] = False
    e = _refus(lambda: _app(op="get", draft=True))
    assert (e.status, e.code) == (403, "draft_editors_only")


def test_get_draft_sert_le_brouillon_et_son_rev(brouillons):
    e = _refus(lambda: _app(op="get", draft=True))
    assert (e.status, e.code) == (404, "no_draft")
    brouillons["draft"] = _brouillon()
    out = _app(op="get", draft=True)
    assert (out["app"]["html"], out["app"]["rev"], out["target"]) == ("<p>draft</p>", "d1", "draft")


# ── Où vont les écritures ───────────────────────────────────────────────────

def test_update_ecrit_le_brouillon_et_la_carte_en_direct(brouillons):
    out = _app(op="update", html="<p>new</p>", title="Renamed")
    assert out["target"] == "draft"
    (w,) = brouillons["writes"]
    assert (w["html"], w["by"]) == ("<p>new</p>", "u1")
    # Le HTML n'est PAS écrit en direct ; seul le titre l'est.
    (_, _, fields), = brouillons["seams"]["update"]
    assert fields.get("html") is None and fields["title"] == "Renamed"


def test_update_sans_brouillon_reste_vivant(brouillons):
    brouillons["mode"] = False
    out = _app(op="update", html="<p>new</p>")
    assert out["target"] == "live" and brouillons["writes"] == []
    (_, _, fields), = brouillons["seams"]["update"]
    assert fields["html"] == "<p>new</p>"


def test_update_du_manifeste_le_juge_avant_le_brouillon(brouillons):
    e = _refus(lambda: _app(op="update", manifest={"tables": {"t": {"table": "99"}}}))
    assert (e.status, e.code) == (400, "invalid_manifest")
    assert brouillons["writes"] == []


def test_patch_s_applique_au_brouillon_contre_son_rev(brouillons):
    brouillons["draft"] = _brouillon(html="<p>a</p><p>b</p>")
    out = _app(op="patch", edits=[{"find": "<p>b</p>", "replace": "<p>c</p>"}])
    assert (out["target"], out["replaced"]) == ("draft", 1)
    (w,) = brouillons["writes"]
    assert w["html"] == "<p>a</p><p>c</p>" and w["expected_rev"] == "d1"
    assert brouillons["seams"]["update"] == []


def test_patch_sans_brouillon_part_de_la_version_publiee(brouillons):
    _app(op="patch", edits=[{"find": "<p>x</p>", "replace": "<p>y</p>"}])
    (w,) = brouillons["writes"]
    assert w["html"] == "<p>y</p>" and w["expected_rev"] == ROW["rev"]


def test_ecrire_contre_la_version_publiee_quand_un_brouillon_existe(brouillons):
    brouillons["raise"] = db.app_versions.DraftExists("d1")
    e = _refus(lambda: _app(op="update", html="<p>z</p>", expected_rev="abc"))
    assert (e.status, e.code) == (409, "draft_exists")


def test_restaurer_vers_le_brouillon_d_une_app_sans_brouillon(brouillons):
    """La base refuse un brouillon dans une app sans brouillon (jugé sous verrou) : la
    capacité le dit d'un code, sans rien écrire en direct à la place."""
    brouillons["raise"] = db.app_versions.DraftsOff()
    e = _refus(lambda: _app(op="restore", revision_id=3, to_draft=True))
    assert (e.status, e.code) == (409, "drafts_off")
    assert brouillons["seams"]["update"] == []


def test_qui_ecrit_le_brouillon_doit_atteindre_son_manifeste(brouillons):
    """L'auteur du brouillon borne son aperçu : écrire sans lire ce qu'il déclare est
    refusé, comme pour l'app publiée."""
    brouillons["draft"] = _brouillon(manifest={"tables": {"t": {"table": "99"}}}, by="autre")
    e = _refus(lambda: _app(op="patch", edits=[{"find": "draft", "replace": "x"}]))
    assert (e.status, e.code) == (403, "would_break_app")
    assert brouillons["writes"] == []


# ── Publier ─────────────────────────────────────────────────────────────────

def test_publier_exige_le_rev_du_brouillon_vu(brouillons):
    brouillons["draft"] = _brouillon()
    e = _refus(lambda: _app(op="publish"))
    assert (e.status, e.code) == (400, "missing_rev")
    e = _refus(lambda: _app(op="publish", expected_rev="d0"))
    assert (e.status, e.code) == (409, "draft_conflict")
    assert brouillons["published"] == []


def test_publier_sans_brouillon(brouillons):
    e = _refus(lambda: _app(op="publish", expected_rev="d1"))
    assert (e.status, e.code) == (409, "no_draft")


def test_publier_rejuge_le_manifeste_contre_qui_publie(brouillons):
    brouillons["draft"] = _brouillon(manifest={"tables": {"t": {"table": "99"}}})
    e = _refus(lambda: _app(op="publish", expected_rev="d1"))
    assert (e.status, e.code) == (400, "invalid_manifest")
    brouillons["draft"] = _brouillon(manifest=GOOD)
    out = _app(op="publish", expected_rev="d1", note="  New layout  ")
    assert out["target"] == "live"
    (p,) = brouillons["published"]
    assert (p["by"], p["note"]) == ("u1", "New layout")
    assert p["manifest"]["tables"]["leads"]["table"] == "12"


def test_publier_demande_d_ecrire_l_app(brouillons):
    brouillons["draft"] = _brouillon()
    brouillons["seams"]["rights"]["write"] = False
    e = _refus(lambda: _app(op="publish", expected_rev="d1"))
    assert (e.status, e.code) == (403, "forbidden")


def test_couper_le_brouillon_avec_un_brouillon_en_cours(brouillons):
    brouillons["draft"] = _brouillon()
    e = _refus(lambda: _app(op="set_drafts", enabled=False))
    assert (e.status, e.code) == (409, "draft_pending")
    assert _app(op="delete_draft")["ok"] is True
    brouillons["draft"] = None
    assert _app(op="set_drafts", enabled=False)["app"]["drafts_enabled"] is False


# ── Lire et restaurer une version ───────────────────────────────────────────

def test_lire_une_version_est_reserve_aux_editeurs(brouillons):
    out = _app(op="revision", revision_id=3)
    assert out["revision"]["html"] == "<p>0</p>"
    brouillons["seams"]["rights"]["write"] = False
    e = _refus(lambda: _app(op="revision", revision_id=3))
    assert (e.status, e.code) == (403, "forbidden")


def test_restaurer_vers_le_brouillon_ne_touche_pas_la_version_publiee(brouillons):
    out = _app(op="restore", revision_id=3, to_draft=True)
    assert out["target"] == "draft"
    (w,) = brouillons["writes"]
    assert w["html"] == "<p>0</p>"
    assert brouillons["seams"]["update"] == []


def test_restaurer_en_direct_est_une_version_publiee(brouillons):
    out = _app(op="restore", revision_id=3, note="Back to last week")
    assert out["target"] == "live"
    (_, _, fields), = brouillons["seams"]["update"]
    assert fields["published"] is True and fields["note"] == "Back to last week"


# ── Les déclarations ────────────────────────────────────────────────────────

def test_les_ops_neuves_sont_des_ecritures():
    assert {"publish", "delete_draft", "set_drafts"} <= A._ECRITURES


def test_publier_et_jeter_demandent_l_accord_de_la_personne():
    """Publier rend du code vivant pour tous : l'agent intégré ne le fait pas sans
    demander, ni jeter un brouillon ; lire une version est une lecture."""
    assert classement.classer("oto_apps", {"op": "publish"}) == classement.DESTRUCTIF
    assert classement.classer("oto_apps", {"op": "delete_draft"}) == classement.DESTRUCTIF
    assert classement.classer("oto_apps", {"op": "revision"}) == classement.LECTURE
    assert effets.effet("oto_apps", {"op": "publish"})[0] == "detruire"
    assert effets.effet("oto_apps", {"op": "revision"})[0] == "lire"
