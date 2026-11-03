"""The `<next>` chips never reach the screen, however the stream cuts the tag; a long
result is cut AND the cut is said."""
from __future__ import annotations

import pytest

from oto_mcp.capabilities.copilot.texte import PLAFOND_RESULTAT, FiltreSuite, couper

TEXTE = "Done: 3 rows.\n<next>Show them | Undo it\n| Make it weekly</next>"


@pytest.mark.parametrize("taille", [1, 2, 3, 5, 7, len(TEXTE)])
def test_suggestions_hors_de_l_ecran_quel_que_soit_le_decoupage(taille):
    vus: list[str] = []
    f = FiltreSuite(vus.append)
    for i in range(0, len(TEXTE), taille):
        f.pousser(TEXTE[i:i + taille])
    items = f.finir()
    assert "".join(vus) == "Done: 3 rows.\n"
    assert items == ["Show them", "Undo it", "Make it weekly"]


def test_sans_bloc_tout_passe_meme_un_chevron():
    vus: list[str] = []
    f = FiltreSuite(vus.append)
    for morceau in ("a <ne", "xt-ish b", " <"):
        f.pousser(morceau)
    assert f.finir() == []
    assert "".join(vus) == "a <next-ish b <"


def test_au_plus_trois():
    vus: list[str] = []
    f = FiltreSuite(vus.append)
    f.pousser("<next>a|b|c|d</next>")
    assert f.finir() == ["a", "b", "c"]


def test_couper_dit_la_coupe():
    assert couper("") == "(empty)"
    court = "x" * 10
    assert couper(court) == court
    long = "y" * (PLAFOND_RESULTAT + 5)
    out = couper(long)
    assert out.startswith("y" * PLAFOND_RESULTAT)
    assert "5 more characters" in out


def test_plafond_par_outil_garde_les_lectures_en_prose_entieres():
    from oto_mcp.capabilities.copilot.texte import PLAFONDS_PAR_OUTIL, plafond_de
    assert plafond_de("data_rows") == PLAFOND_RESULTAT
    assert plafond_de("oto_procedure") == PLAFONDS_PAR_OUTIL["oto_procedure"] > PLAFOND_RESULTAT
    texte = "z" * (PLAFOND_RESULTAT + 1000)
    assert "more characters" in couper(texte, plafond_de("data_rows"))
    assert couper(texte, plafond_de("oto_procedure")) == texte


def test_le_guide_garde_le_plafond_de_la_procedure():
    from oto_mcp.capabilities.copilot.texte import plafond_de
    assert plafond_de("oto_guide") == plafond_de("oto_procedure") == 80_000
    guide = "g" * 60_000
    assert couper(guide, plafond_de("oto_guide")) == guide


def test_couper_reecrit_le_json_sans_espaces():
    import json
    lignes = [{"id": i, "note": "x" * 50} for i in range(200)]
    out = couper(json.dumps(lignes), 3_000)
    assert out.startswith('[{"id":0,"note":"x') and '"id": ' not in out
    assert json.loads(out)[-1]["_cut"] > 0


def test_couper_une_liste_json_par_elements():
    import json
    lignes = [{"id": i, "note": "x" * 200} for i in range(400)]
    texte = json.dumps(lignes)
    out = couper(texte, 5_000)
    assert len(out) <= 5_000
    lu = json.loads(out)                      # still valid JSON
    assert lu[-1]["_cut"] == 400 - (len(lu) - 1) and "limit" in lu[-1]["_hint"]
    assert lu[0] == lignes[0] and lu[len(lu) - 2] == lignes[len(lu) - 2]   # whole items


def test_couper_un_objet_coupe_sa_plus_longue_liste():
    import json
    obj = {"total": 300, "datastores": [{"id": i, "pad": "y" * 150} for i in range(300)]}
    out = json.loads(couper(json.dumps(obj), 4_000))
    assert out["total"] == 300
    assert out["_cut"] == {"datastores": 300 - len(out["datastores"])}


def test_couper_sans_liste_coupe_les_caracteres():
    out = couper("a" * 9_000, 1_000)
    assert out.startswith("a" * 1_000) and "more characters" in out
    # an item bigger than the whole budget cannot be kept whole either
    import json
    gros = json.dumps([{"t": "b" * 9_000}, {"t": "c"}])
    assert "more characters" in couper(gros, 1_000)


def test_alleger_compacte_et_retire_les_cles_vides_des_lignes():
    import json
    from oto_mcp.capabilities.copilot.texte import alleger
    lignes = [{"id": i, "name": f"n{i}", "note": "", "tags": [], "owner": None,
               "score": 0, "ok": False} for i in range(5)]
    brut = json.dumps({"rows": lignes, "total": 5}, indent=2)
    out = alleger(brut)
    assert len(out) < len(brut) * 0.5
    lu = json.loads(out)
    assert lu["total"] == 5
    assert lu["rows"][0] == {"id": 0, "name": "n0", "score": 0, "ok": False}   # 0/false stay
    assert ", " not in out and ": " not in out


def test_alleger_laisse_le_reste_tel_quel():
    import json
    from oto_mcp.capabilities.copilot.texte import alleger
    for texte in ("plain text", "", '"just a string"', "12", "{not json", "Result:\n[1,2]"):
        assert alleger(texte) == texte
    deux = json.dumps([{"a": None}, {"a": 1}])              # under three rows: keys kept
    assert json.loads(alleger(deux)) == [{"a": None}, {"a": 1}]
    assert alleger(json.dumps({"a": None, "b": []})) == '{"a":null,"b":[]}'
