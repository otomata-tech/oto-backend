"""Old tool results leave the history when the conversation resumes cold."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from oto_mcp.capabilities.copilot import compactage, outils


def _sans_annotation(_target):
    return None


def _echange(i, taille, nom="data_rows", erreur=False, entree=None):
    return [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "t", "signature": "s"},
            {"type": "tool_use", "id": f"tu{i}", "name": nom, "input": entree or {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}", "content": "r" * taille,
             "is_error": erreur}]},
    ]


def _fil(*tailles):
    m = []
    for i, t in enumerate(tailles):
        m.append({"role": "user", "content": [{"type": "text", "text": f"question {i}"}]})
        m += _echange(i, t)
    return m


def test_les_resultats_des_questions_anciennes_partent_pas_ceux_du_dernier_echange():
    m = _fil(30_000, 5_000, 40_000)
    libres = compactage.compacter(m, _sans_annotation)
    assert 34_000 < libres < 35_000
    r0 = m[2]["content"][0]
    assert r0["tool_use_id"] == "tu0" and "removed from the history" in r0["content"]
    assert "`data_rows`" in r0["content"] and "30000" in r0["content"]
    assert "removed" in m[5]["content"][0]["content"]          # 5 000 >= the minimum
    assert m[8]["content"][0]["content"] == "r" * 40_000        # the last exchange stays


def test_sous_le_gain_minimum_rien_ne_bouge():
    m = _fil(3_000, 4_000, 10_000)
    avant = repr(m)
    assert compactage.compacter(m, _sans_annotation) == 0 and repr(m) == avant


def test_idempotent_et_garde_erreur_reflexion_et_ids():
    m = _fil(30_000, 1_000, 10_000)
    m[2]["content"][0]["is_error"] = True
    compactage.compacter(m, _sans_annotation)
    une_fois = repr(m)
    assert compactage.compacter(m, _sans_annotation) == 0 and repr(m) == une_fois
    assert m[2]["content"][0]["is_error"] is True
    assert m[1]["content"][0]["type"] == "thinking"             # blocks of the model untouched
    assert m[1]["content"][1]["id"] == "tu0"


def test_nom_de_la_cible_derriere_oto_call():
    m = [{"role": "user", "content": "q0"},
         {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "oto_call",
                                            "input": {"name": "oto_doc",
                                                      "arguments": {"op": "get"}}}]},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                       "content": "z" * 30_000, "is_error": False}]},
         {"role": "user", "content": "q1"}]
    compactage.compacter(m, _sans_annotation)
    assert "`oto_doc`" in m[2]["content"][0]["content"]


def test_le_resultat_d_une_ecriture_n_est_jamais_compacte():
    m = [{"role": "user", "content": "q0"}]
    m += _echange(0, 30_000)                                             # a read
    m += _echange(1, 50_000, nom="data_write", entree={"datastore": "t"})  # a write
    m += _echange(2, 50_000, nom="oto_call", entree={"name": "oto_trigger",
                                                     "arguments": {"op": "create"}})
    m.append({"role": "user", "content": "q1"})
    assert compactage.compacter(m, _sans_annotation) > 0
    assert "removed from the history" in m[2]["content"][0]["content"]
    assert m[4]["content"][0]["content"] == "r" * 50_000
    assert m[6]["content"][0]["content"] == "r" * 50_000


def test_les_ecritures_seules_ne_liberent_rien():
    m = [{"role": "user", "content": "q0"}]
    m += _echange(0, 60_000, nom="data_write", entree={"datastore": "t"})
    m.append({"role": "user", "content": "q1"})
    avant = repr(m)
    assert compactage.compacter(m, _sans_annotation) == 0 and repr(m) == avant


def test_les_annotations_declarees_classent_l_outil():
    """A connector tool is classed by what it declares, as in the loop."""
    lit = SimpleNamespace(readOnlyHint=True, destructiveHint=None)
    envoie = SimpleNamespace(readOnlyHint=None, destructiveHint=True)
    annotations = {"crm_lire": lit, "crm_envoyer": envoie}.get
    m = [{"role": "user", "content": "q0"}]
    m += _echange(0, 30_000, nom="crm_lire")
    m += _echange(1, 30_000, nom="crm_envoyer")
    m.append({"role": "user", "content": "q1"})
    compactage.compacter(m, annotations)
    assert "removed from the history" in m[2]["content"][0]["content"]
    assert m[4]["content"][0]["content"] == "r" * 30_000


def test_froid_seulement_apres_cinq_minutes():
    assert not compactage.est_froid(None) and not compactage.est_froid(120)
    assert compactage.est_froid(301)
    vu = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert 590 < compactage.inactif_depuis(vu) < 700
    assert compactage.inactif_depuis("x") is None


def test_champs_par_defaut_sans_ecraser_ceux_de_l_appelant():
    assert outils.champs_de("data_list_datastores", {}, "data_list_datastores")[0] == "id"
    assert "schema" not in outils.CHAMPS_PAR_DEFAUT["data_list_datastores"]
    assert outils.champs_de("oto_call", {"fields": ["*"]}, "data_list_datastores") == ["*"]
    assert outils.champs_de("oto_call", {"name": "data_list_datastores"},
                            "data_list_datastores")[0] == "id"
    assert outils.champs_de("data_rows", {}, "data_rows") is None
