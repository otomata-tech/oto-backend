"""La péremption d'une occurrence programmée — et le fait qu'elle se DISE.

Le trou du 02/09 : 41 travaux programmés attendaient depuis treize jours, sur
quatre organisations, `attempts = 0`. Les déclencheurs enfilaient, les agents
prenaient ce qu'ils pouvaient voir, le périmètre par organisation protégeait —
**chaque pièce faisait exactement son travail, et leur composition fabriquait le
trou.** Rien ne le disait, parce qu'un travail « en attente » ressemble à un
travail qui va partir.

⚠️ Deux erreurs symétriques sont possibles ici, et ces tests gardent les deux :
purger en silence (on remplace un trou par un pire, en effaçant la preuve du
premier) et laisser s'empiler (le jour où des agents arrivent, treize jours
partent d'un coup avec le contexte de leur époque).
"""
from __future__ import annotations

import pytest

from oto_mcp import runner_tick
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)



def _declencheur(**kw):
    base = {"id": 5, "org_id": 2, "cron": "5 6 * * *", "tz": "Europe/Paris",
            "next_due": "2026-08-14 04:05:00", "procedure": "veille",
            "project_id": None, "tools": ["data_write"], "input": None,
            "label": None, "max_steps": None}
    base.update(kw)
    return base


# ── le tick périme AVANT d'enfiler ────────────────────────────────────────────

def test_l_occurrence_precedente_est_perimee_avant_la_suivante(monkeypatch):
    """C'est le cœur : ce qui n'a pas été pris dans son cycle ne le sera plus
    utilement. Une veille quotidienne jouée treize jours plus tard ne rend pas un
    résultat en retard, elle rend un résultat FAUX."""
    ordre = []
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [_declencheur()])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, p: True)
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur",
                        lambda t, o: ordre.append(("perime", t, o)) or 3)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None, **_: ordre.append(("enfile", org))
                        or {"id": 9})
    assert runner_tick._tick() == 1
    assert [g for g, *_ in ordre] == ["perime", "enfile"], (
        "périmer APRÈS aurait périmé l'occurrence qu'on vient d'enfiler")
    assert ordre[0][1:] == (5, 2), "périmé pour CE déclencheur, dans SON org"


def test_le_cas_perdu_ne_perime_rien(monkeypatch):
    """L'autre environnement a consommé l'échéance : ce tick-ci n'enfile pas, donc
    il n'a pas à périmer non plus. Périmer sans enfiler ferait disparaître une
    occurrence que personne ne remplace."""
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [_declencheur()])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, p: False)
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur",
                        lambda t, o: pytest.fail("CAS perdu ⟹ aucune péremption"))
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("CAS perdu ⟹ aucun job"))
    assert runner_tick._tick() == 0


def test_une_peremption_qui_echoue_n_empeche_pas_le_reste(monkeypatch):
    """⚠️ La péremption est un geste d'HYGIÈNE : si elle casse, l'enfilage doit
    continuer. L'inverse ferait qu'un défaut d'entretien arrête le service."""
    enfile = []
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [
        _declencheur(id=1), _declencheur(id=2)])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, p: True)

    def _perime(t, o):
        if t == 1:
            raise RuntimeError("base indisponible")
        return 0

    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur", _perime)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None, **_: enfile.append(
                            payload["trigger_id"]) or {"id": 9})
    assert runner_tick._tick() == 2
    assert enfile == [1, 2], "les DEUX déclencheurs ont enfilé malgré l'échec"


# ── la perte se DIT, sur le déclencheur, là où on la cherche ──────────────────

def _ctx(org_id=2):
    return ResolvedCtx(sub="alexis", org_id=org_id)


def test_le_declencheur_servi_porte_ce_qu_il_a_perdu(monkeypatch):
    """Une perte que seule une requête manuelle révèle n'est pas une perte connue :
    les 41 occurrences ont été découvertes par hasard, en préparant autre chose."""
    monkeypatch.setattr(RT.db, "list_triggers",
                        lambda org: [{"id": 5, "org_id": org, "enabled": True}])
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": False, "workers": 0, "last_seen": None})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda org, tid: {"expired_count": 20,
                                          "expired_since": "2026-08-20 18:00:00",
                                          "expired_last": "2026-09-02 07:00:00"})
    out = RT._triggers(_ctx(), RT.TriggerInput(op="list"))
    t = out["triggers"][0]
    assert t["expired_count"] == 20
    # ⚠️ DEUX dates, pas une : « depuis quand » et « est-ce encore en cours » sont
    # deux questions, et une perte ancienne qui a cessé n'appelle pas le même
    # geste qu'une perte qui continue ce matin.
    assert t["expired_since"] != t["expired_last"]


def test_zero_perdu_est_un_vrai_zero_pas_une_absence(monkeypatch):
    """⚠️ Le champ est SERVI même à zéro. Absent, il se lirait « je n'ai pas
    regardé » — et un lecteur prudent irait vérifier à la main, ce qui est
    exactement le geste qu'on veut supprimer."""
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {"id": i, "enabled": True})
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 2,
                                     "last_seen": "2026-09-02 15:00:00"})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda org, tid: {"expired_count": 0, "expired_since": None,
                                          "expired_last": None})
    out = RT._triggers(_ctx(), RT.TriggerInput(op="get", trigger_id=5))
    assert out["trigger"]["expired_count"] == 0
    assert "expired_count" in out["trigger"]


def test_le_modele_servi_declare_les_trois_champs():
    """Le contrat, pas seulement le dictionnaire : un champ rendu par le handler
    mais absent du modèle est silencieusement RETIRÉ à la sérialisation — le
    dashboard ne le verrait jamais, et rien n'échouerait."""
    champs = RT.Trigger.model_fields
    for c in ("expired_count", "expired_since", "expired_last"):
        assert c in champs, f"`{c}` n'est pas déclaré : il ne sera pas servi"


def test_la_description_servie_annonce_la_peremption():
    """Une description d'outil est relue à chaque appel et vaut instruction : si
    elle ne dit pas qu'une occurrence peut périmer, un agent lit `expired_count`
    sans savoir ce qu'il regarde."""
    cap = next(c for c in RT.CAPABILITIES if c.key == "runner.triggers")
    assert "expired_count" in cap.description
    assert "EXPIRED" in cap.description or "expired" in cap.description


# ── rallumer reprend le RYTHME, ça ne rembobine pas (#826, arbitré le 02/09) ──

def _trigger_db(monkeypatch, **etat):
    """Le déclencheur tel qu'il est EN BASE avant la retouche."""
    base = {"id": 6, "cron": "0 18 * * *", "tz": "Europe/Paris", "enabled": False}
    base.update(etat)
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: dict(base, id=i))
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": "2026-09-02 20:00:00"})
    vu = {}
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs: vu.update(champs) or {"id": i, **champs})
    monkeypatch.setattr(RT.db, "comptage_perime", lambda org, tid: {})
    return vu


def test_rallumer_repousse_l_echeance_dans_le_futur(monkeypatch):
    """⚠️ Sans ce recalcul, l'échéance figée pendant l'extinction est restée dans
    le PASSÉ : le tick voyait le déclencheur dû à la seconde du rallumage et
    enfilait aussitôt. Une exécution que personne n'a demandée, déclenchée par le
    geste de quelqu'un qui répare."""
    import datetime

    vu = _trigger_db(monkeypatch, enabled=False)
    RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=6, enabled=True))
    assert "next_due" in vu, "rallumer sans recalculer laisse une échéance périmée"
    assert vu["next_due"] > datetime.datetime.now(datetime.timezone.utc)


def test_rallumer_un_declencheur_deja_allume_ne_repousse_rien(monkeypatch):
    """⚠️ Seul le PASSAGE à allumé recalcule — même motif que la péremption, qui
    ne mord qu'au passage à éteint. Sinon répéter un geste qui n'est censé rien
    changer donnerait un moyen de repousser l'échéance indéfiniment."""
    vu = _trigger_db(monkeypatch, enabled=True)
    RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=6, enabled=True))
    assert "next_due" not in vu


def test_eteindre_ne_touche_pas_l_echeance(monkeypatch):
    """Éteindre ne promet rien et ne recalcule rien : c'est le rallumage qui
    reprend le rythme, et lui seul."""
    vu = _trigger_db(monkeypatch, enabled=True)
    RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=6, enabled=False))
    assert "next_due" not in vu and vu["enabled"] is False


def test_rallumer_avec_un_nouveau_cron_ne_calcule_qu_une_fois(monkeypatch):
    """Le cas combiné : le cadencement fourni fait autorité, et l'échéance vient
    de LUI — pas de l'ancien cron recalculé par-dessus."""
    vu = _trigger_db(monkeypatch, enabled=False, cron="0 18 * * *")
    RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=6, enabled=True,
                                         cron="30 7 * * *", tz="UTC"))
    assert vu["cron"] == "30 7 * * *"
    assert vu["next_due"].astimezone(__import__("datetime").timezone.utc).hour == 7
