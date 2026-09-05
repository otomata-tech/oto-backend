"""Les attributs de colonne : une déclaration, deux clients, et un garde-fou (oto#56).

Le validateur acceptait n'importe quelle clé sur une colonne. `editable` passe,
`zorglub` passe, et surtout `read_only` passe — une faute de frappe sur un attribut de
garde est silencieuse **et** elle désarme la garde.

⚠️ **Ce banc existe surtout pour empêcher l'avertissement de MENTIR.** Sa première forme
dérivait la liste des clés vivantes en observant le validateur ; c'était faux, parce que
le schéma est aussi un contrat SERVI — cinq attributs (`label`, `help`, `placeholder`,
`hint`, `description`) n'existent que pour le front. L'avertissement aurait crié sur
presque tous les tableaux existants, et on aurait appris à l'ignorer.

L'observation n'a pas disparu : elle a changé de rôle. Elle ne fabrique plus la liste,
elle la GARDE — tout ce que le validateur lit doit être déclaré.
"""
from __future__ import annotations

from oto_mcp.datastore import cles_inconnues as C
from oto_mcp.datastore import schema as S
from oto_mcp.datastore import schema_keys as K


class _Espion(dict):
    """Un champ qui note ce qu'on lui demande — `get`, `[]` et `in` sont relevés."""

    def __init__(self, source, vues):
        super().__init__(source)
        self._vues = vues

    def get(self, cle, defaut=None):
        self._vues.add(cle)
        return super().get(cle, defaut)

    def __getitem__(self, cle):
        self._vues.add(cle)
        return super().__getitem__(cle)

    def __contains__(self, cle):
        self._vues.add(cle)
        return super().__contains__(cle)


_SONDES = (
    {}, {"type": "text"}, {"type": "text", "readonly": True},
    {"type": "text", "system": "now"}, {"type": "object", "fields": []},
    {"type": "text", "role": "status"},
)


def _cles_lues_par_le_validateur() -> set[str]:
    vues: set = set()
    for sonde in _SONDES:
        for nom in ("colonne", "colonne.comment"):
            try:
                S.validate_schema_def({"fields": [_Espion({"key": nom, **sonde}, vues)]})
            except Exception:  # noqa: BLE001 — une sonde qui casse n'apprend rien
                continue
    return {v for v in vues if isinstance(v, str)}


def test_tout_ce_que_le_validateur_LIT_est_declare():
    """LE garde-fou. Un `f.get("nouveau")` ajouté sans déclaration ferait dire à
    l'avertissement qu'une clé vivante n'est lue par personne — et personne ne s'en
    apercevrait avant qu'un utilisateur ne le signale."""
    non_declarees = _cles_lues_par_le_validateur() - K.RECONNUES
    assert not non_declarees, (
        f"le validateur lit des clés qui ne sont pas déclarées : {sorted(non_declarees)}. "
        "Déclare-les dans `datastore/schema_keys.py` avec leur lecteur, sinon "
        "`unknown_keys_warning` les dénoncera comme mortes.")


def test_le_validateur_DERIVE_ses_crans_de_la_declaration():
    """Premier client, pas documentation : si les deux divergeaient, la déclaration
    ne serait qu'un commentaire."""
    assert S._COLUMN_ONLY_KEYS == K.COLONNE_SEULEMENT


def test_une_faute_de_frappe_sur_un_cran_est_nommee():
    """`read_only` désarme le verrou en silence : c'est le cas le plus coûteux, parce
    que l'auteur croit avoir protégé sa colonne."""
    w = C.check({"fields": [{"key": "k", "type": "text", "read_only": True}]})
    assert w["unknown_keys_warning"] and "read_only" in w["unknown_keys_warning"]


def test_le_cran_correctement_ecrit_ne_dit_rien():
    """L'autre moitié : un avertissement qui parle toujours ne se lit plus."""
    assert C.check({"fields": [{"key": "k", "type": "text", "readonly": True}]})[
        "unknown_keys_warning"] is None


def test_le_cas_fondateur_est_nomme():
    """`editable` n'existe nulle part — l'agent l'a découvert en comparant deux refus
    strictement identiques, mot pour mot."""
    w = C.check({"fields": [{"key": "k", "type": "text",
                             "readonly": True, "editable": True}]})
    assert w["unknown_keys_warning"] and "editable" in w["unknown_keys_warning"]


def test_les_attributs_du_FRONT_ne_sont_jamais_denonces():
    """La moitié qui a failli couler le lot : ces cinq-là sont invisibles au
    validateur et parfaitement vivants. `label` est lu 40 fois côté dashboard."""
    champs = [{"key": "k", "type": "text", "label": "Nom", "description": "d",
               "help": "h", "hint": "i", "placeholder": "p"}]
    assert C.check({"fields": champs})["unknown_keys_warning"] is None


def test_chaque_cle_declaree_nomme_au_moins_un_lecteur():
    """Une clé sans lecteur n'aurait aucune raison d'être reconnue — et c'est
    précisément ce que le lot cherche à rendre impossible."""
    for c in K.CLES:
        assert c.lecteurs, c.nom
        assert set(c.lecteurs) <= {"validateur", "front"}, c.nom
        assert c.quoi.strip(), c.nom


def test_la_declaration_est_SERVIE():
    """Elle est déclarée à la main pour la moitié `front` : le seul chemin qui rendra
    cette moitié sûre est un contrôle côté dashboard, et il lui faut cette route."""
    servie = K.servie()
    assert {e["key"] for e in servie} == K.RECONNUES
    assert all(e["readers"] and "what" in e for e in servie)
