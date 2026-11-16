"""`remplacements.appliquer` — éditer un long texte par remplacements exacts, tout ou rien."""
import pytest

from oto_mcp.capabilities.remplacements import MAX_EDITS, RemplacementRefuse, appliquer


def test_dans_l_ordre_chacun_sur_le_texte_deja_modifie():
    assert appliquer("a b c", [{"find": "b", "replace": "B"}, {"find": "a B", "replace": "x"}]) \
        == ("x c", 2)


def test_absent_ou_ambigu_refuse_et_nomme_l_edit():
    with pytest.raises(RemplacementRefuse, match=r"edits\[1\].*isn't in"):
        appliquer("a b", [{"find": "a", "replace": "z"}, {"find": "a", "replace": "y"}])
    with pytest.raises(RemplacementRefuse, match="appears 2 times"):
        appliquer("a a", [{"find": "a", "replace": "z"}])
    assert appliquer("a a", [{"find": "a", "replace": "z", "all": True}]) == ("z z", 2)


@pytest.mark.parametrize("edits", [None, [], "x", [{"find": "a"}], [{"find": "", "replace": "b"}],
                                   [{"find": 1, "replace": "b"}],
                                   [{"find": "a", "replace": "b"}] * (MAX_EDITS + 1)])
def test_forme_refusee(edits):
    with pytest.raises(RemplacementRefuse):
        appliquer("a", edits)


def test_chevauchement_ambigu():
    """`count` ne voit qu'une occurrence de « aa » dans « aaa » : il y en a deux."""
    with pytest.raises(RemplacementRefuse, match="overlapping"):
        appliquer("aaa", [{"find": "aa", "replace": "x"}])
    assert appliquer("aaa", [{"find": "aa", "replace": "x", "all": True}]) == ("xa", 1)


def test_longueur_jugee_avant_de_remplacer():
    with pytest.raises(RemplacementRefuse, match="longer than 1000"):
        appliquer("a" * 500, [{"find": "a", "replace": "b" * 100_000, "all": True}],
                  max_len=1000)
    assert appliquer("ab", [{"find": "a", "replace": "xyz"}], max_len=4) == ("xyzb", 1)
    with pytest.raises(RemplacementRefuse):
        appliquer("ab", [{"find": "a", "replace": "xyz"}], max_len=3)


def test_un_remplacement_qui_ne_change_rien_est_refuse():
    """Aucune version enregistrée identique à la précédente."""
    with pytest.raises(RemplacementRefuse, match=r"edits\[0\]\.replace is the same as its find"):
        appliquer("a b", [{"find": "a", "replace": "a"}])
    with pytest.raises(RemplacementRefuse, match=r"edits\[1\]"):
        appliquer("a b", [{"find": "b", "replace": "B"}, {"find": "a", "replace": "a"}])
    # Chaque edit change, la liste entière ne change rien : refusée aussi.
    with pytest.raises(RemplacementRefuse, match="exactly as it was"):
        appliquer("a b", [{"find": "a", "replace": "x"}, {"find": "x", "replace": "a"}])
