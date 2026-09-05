"""Un patch partiel de `customFieldValues` ne détruit plus les champs voisins (#714).

L'API Folk est en *replace-all* sur cet objet. Passer un seul champ personnalisé
effaçait donc tous les autres de ce groupe — silencieusement, avec `succeeded: 1` en
retour. Mesuré le 04/09/2026 en `dry_run` : **quatre champs perdus en un appel**, dont
une consigne opérationnelle.

Ce qui rend le piège particulièrement mauvais, et ce que ces bancs verrouillent : la
documentation de l'outil DIT que `groups` est un remplacement et offre de quoi
l'éviter. Cette précaution visible sur le champ voisin fait conclure que la fusion est
le défaut ailleurs.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp.mcp_errors import McpError

FICHE = {
    "id": "per_1",
    "customFieldValues": {
        "grp_a": {"Cercle": [], "Statut": "en cours", "Suite": "rappeler",
                  "Relation": "partenaire"},
        "grp_b": {"Source": "salon"},
    },
}


def _appel(**kwargs):
    from fastmcp import FastMCP

    from oto_mcp.tools import folk as folk_tool
    m = FastMCP("t")
    folk_tool.register(m)
    return asyncio.run(m.get_tool("folk_record")).fn(**kwargs)


@pytest.fixture(autouse=True)
def _cle(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("k", False))


@pytest.fixture
def client_cls():
    with patch("oto.tools.folk.client.FolkClient") as cls:
        yield cls


def test_un_seul_champ_ecrit_laisse_les_autres_en_place(client_cls):
    """LE banc du lot : deux champs déjà posés, on en écrit un troisième, on retrouve
    les trois. C'est l'incident du 04/09, joué à l'endroit exact où il s'est produit."""
    inst = client_cls.return_value
    inst.get_person.return_value = dict(FICHE)
    inst.update_person.return_value = {"id": "per_1"}

    _appel(op="update", entity="person", id="per_1",
           fields={"customFieldValues": {"grp_a": {"Cercle": ["agentique"]}}})

    envoye = inst.update_person.call_args.kwargs["customFieldValues"]
    assert envoye["grp_a"] == {
        "Cercle": ["agentique"],          # fourni → écrit
        "Statut": "en cours",             # absent de l'appel → survit
        "Suite": "rappeler",
        "Relation": "partenaire",
    }, "les champs non cités doivent survivre — c'est tout l'objet du lot"


def test_les_autres_groupes_de_la_fiche_survivent(client_cls):
    """La granularité est le CHAMP, pas le groupe : écrire dans un groupe ne doit pas
    emporter les champs personnalisés portés par les autres."""
    inst = client_cls.return_value
    inst.get_person.return_value = dict(FICHE)
    inst.update_person.return_value = {"id": "per_1"}

    _appel(op="update", entity="person", id="per_1",
           fields={"customFieldValues": {"grp_a": {"Cercle": ["x"]}}})

    envoye = inst.update_person.call_args.kwargs["customFieldValues"]
    assert envoye["grp_b"] == {"Source": "salon"}


def test_vider_un_champ_reste_possible_en_le_fournissant(client_cls):
    """⚠️ La fusion ne doit pas confisquer l'effacement : l'incident fondateur s'est
    RÉPARÉ par un second appel qui remettait le champ à vide. Une valeur fournie est
    écrite telle quelle, `None` compris."""
    inst = client_cls.return_value
    inst.get_person.return_value = dict(FICHE)
    inst.update_person.return_value = {"id": "per_1"}

    _appel(op="update", entity="person", id="per_1",
           fields={"customFieldValues": {"grp_a": {"Statut": None}}})

    envoye = inst.update_person.call_args.kwargs["customFieldValues"]
    assert envoye["grp_a"]["Statut"] is None
    assert envoye["grp_a"]["Suite"] == "rappeler"


def test_un_groupe_absent_de_la_fiche_s_ecrit_tel_quel(client_cls):
    """Rien à préserver, donc rien à relire : on écrit ce que l'appelant a fourni."""
    inst = client_cls.return_value
    inst.get_person.return_value = dict(FICHE)
    inst.update_person.return_value = {"id": "per_1"}

    _appel(op="update", entity="person", id="per_1",
           fields={"customFieldValues": {"grp_neuf": {"Note": "ok"}}})

    envoye = inst.update_person.call_args.kwargs["customFieldValues"]
    assert envoye["grp_neuf"] == {"Note": "ok"}
    assert envoye["grp_a"]["Statut"] == "en cours"


def test_fiche_illisible_REFUSE_au_lieu_d_ecrire_a_l_aveugle(client_cls):
    """Sans l'état actuel on ne peut pas fusionner, et écrire le patch tel quel
    effacerait ce qu'on n'a pas pu lire — le dégât même que ce lot corrige. Un refus
    nommé laisse l'appelant choisir ; un succès muet ne laisse rien."""
    inst = client_cls.return_value
    inst.get_person.return_value = None

    with pytest.raises(McpError):
        _appel(op="update", entity="person", id="per_1",
               fields={"customFieldValues": {"grp_a": {"Cercle": ["x"]}}})

    inst.update_person.assert_not_called()


def test_les_champs_ordinaires_ne_declenchent_aucune_relecture(client_cls):
    """La relecture a un coût (un appel réseau de plus) : elle ne doit se produire que
    lorsqu'elle est nécessaire, sinon on ralentit tous les autres patchs."""
    inst = client_cls.return_value
    inst.update_person.return_value = {"id": "per_1"}

    _appel(op="update", entity="person", id="per_1", fields={"jobTitle": "CTO"})

    inst.get_person.assert_not_called()


def test_le_dry_run_montre_l_etat_FUSIONNE(client_cls):
    """C'est le `dry_run` qui a évité la perte le 04/09 : il doit maintenant montrer ce
    que l'appel fera VRAIMENT. S'il continuait d'afficher le patch nu, il annoncerait
    une destruction qui n'a plus lieu — et personne n'oserait plus écrire."""
    inst = client_cls.return_value
    inst.get_person.return_value = dict(FICHE)

    r = _appel(op="update", entity="person", id="per_1", dry_run=True,
               fields={"customFieldValues": {"grp_a": {"Cercle": ["x"]}}})

    apres = r["changes"]["customFieldValues"]["to"]
    assert apres["grp_a"]["Statut"] == "en cours"
    assert apres["grp_a"]["Cercle"] == ["x"]
    inst.update_person.assert_not_called()
