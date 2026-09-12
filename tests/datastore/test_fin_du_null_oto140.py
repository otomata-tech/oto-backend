"""`null` cesse de vouloir dire « efface » — le préavis d'abord, le refus à la date.

Dans le datastore, `{"champ": null}` EFFACE. Partout ailleurs — un schéma, une réponse,
le JSON de n'importe qui — `null` veut dire « pas de valeur ». **Le même jeton disait
une chose et son contraire selon l'endroit.**

⚠️ Préavis et non retrait, parce que c'est mesuré : **1 862 `"valeur": null`** dans les
journaux du plus gros écrivain, et le sens que ses agents lui donnent est « cherché,
rien trouvé » — l'exact opposé d'« efface ». Un de leurs textes servis porte même
l'exemple. Retirer sec casserait six procédures le jour où elles tournent, et le refus
arriverait à un agent qui ne peut pas republier sa propre procédure.
"""
from __future__ import annotations

from datetime import date

import pytest

from oto_mcp.datastore import fin_du_null as fdn


# ── ce que le préavis VOIT ───────────────────────────────────────────────────

def test_le_null_NOMME_est_vu_sous_ses_deux_formes():
    """`{"a": null}` et `{"a": {"valeur": null}}` sont le même geste."""
    assert fdn.nulls_nommes({"a": None, "b": {"valeur": None}}) == ["a", "b"]


def test_ce_qui_n_est_PAS_un_null_nomme_ne_declenche_rien():
    """⚠️ Nommé, pas déduit d'un effacement. Une valeur peut tomber pour d'autres
    raisons — une liste remplacée, une chaîne vide. Ce préavis ne parle que du jeton
    qu'on retire ; confondre les deux ferait crier sur des gestes qui ne changeront
    pas, et un avertissement qu'on apprend à ignorer ne sert plus."""
    assert fdn.nulls_nommes({
        "vide": "", "zero": 0, "faux": False,
        "annote": {"comment": "x"}, "liste": [],
    }) == []


# ── ce que le texte DIT ──────────────────────────────────────────────────────

def test_l_avertissement_dit_la_DATE_et_les_deux_gestes():
    t = fdn.avertissement(["facebook"])
    assert "1er décembre 2026" in t
    assert "@empty" in t and "@keep" in t


def test_le_texte_nomme_l_usage_REEL_et_le_geste_qui_lui_correspond():
    """⚠️ La moitié qui évite le dégât. L'usage massif de `null` chez les agents est
    « cherché, rien trouvé » — pas « efface ». Leur proposer `@empty` sans le dire les
    ferait détruire en masse ce qu'ils voulaient seulement laisser vide. Le geste juste
    pour eux est l'OMISSION, et le texte doit le nommer."""
    t = fdn.avertissement(["a"])
    assert "cherché, rien trouvé" in t
    assert "OMISSION" in t or "omets" in t


def test_le_refus_PARTAGE_son_corps_avec_l_avertissement():
    """Partagé, pas recopié : celui qui s'est préparé pendant le préavis ne doit pas
    découvrir au moment du refus qu'on lui demandait autre chose."""
    commun = fdn._les_deux_gestes()
    assert commun in fdn.avertissement(["a"])
    assert commun in fdn.refus(["a"])


def test_le_silence_quand_aucun_null():
    assert fdn.avertissement([]) is None


# ── la bascule ───────────────────────────────────────────────────────────────

def test_le_refus_n_est_PAS_encore_arme():
    assert not fdn.refus_arme(date(2026, 11, 30))


def test_il_s_arme_le_jour_dit():
    assert fdn.refus_arme(date(2026, 12, 1))


def test_une_date_illisible_LEVE_au_lieu_de_retomber_sur_le_defaut(monkeypatch):
    """⚠️ Un préavis dont la date est muette annonce une échéance que rien n'applique.
    Le réglage décide à la fois de ce qui est ANNONCÉ et de ce qui est REFUSÉ : le lire
    de travers ferait promettre un jour et en couper un autre."""
    monkeypatch.setenv(fdn.ENV_NULL_REFUSE_LE, "bientôt")
    with pytest.raises(ValueError) as e:
        fdn.date_refus()
    assert fdn.ENV_NULL_REFUSE_LE in str(e.value)


def test_la_date_vit_dans_le_CODE_pas_dans_l_env(monkeypatch):
    """Ce que le tronc annonce doit être exactement ce qu'il refusera, sans dépendre
    d'un geste sur une machine."""
    monkeypatch.delenv(fdn.ENV_NULL_REFUSE_LE, raising=False)
    assert fdn.date_refus() == fdn.NULL_REFUSE_LE


# ── les DEUX chemins d'écriture le portent ───────────────────────────────────

def test_le_preavis_est_pose_sur_les_chemins_d_ecriture():
    """La création, le patch par `id` et le LOT — les trois par lesquels un `null`
    peut entrer. Le lot compte double : c'est lui qui porte les imports."""
    import inspect

    from oto_mcp.datastore import ecriture, ecriture_par_id, lots

    # La création dans `ecriture`, le patch par `id` dans son module depuis le 12/09/2026.
    assert inspect.getsource(ecriture).count("fdn.nulls_nommes(") == 1
    assert "fdn.nulls_nommes(" in inspect.getsource(ecriture_par_id)
    assert "fdn.nulls_nommes(" in inspect.getsource(lots)
