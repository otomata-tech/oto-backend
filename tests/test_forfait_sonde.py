"""`runner.jobs op=forfait` : la SONDE du worker, portée sur le forfait, et le verdict.
Et le prédicat unique de la pause qui freine la réservation, et qui est sondé."""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp.capabilities import _abonnement
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.db import runner_jobs as DBRJ
from oto_mcp.db import user_subscriptions as US

_FAMILLE = "claude_subscription"
_RAPPORT = {"abonnement": {"etat": "rejected", "fenetre": "five_hour", "echeance": 1790029800,
                           "fenetres": {"five_hour": {"utilization": 1.0,
                                                      "resetsAt": 1790029800}}}}


def _en_base(quand: datetime) -> str:
    """Une échéance telle que la base la rend (`_conn._str_dict_row`) : un TEXTE."""
    return quand.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def _dans(**k) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(**k)).replace(microsecond=0)


class _Ctx:
    def __init__(self, platform_worker=True, sub="worker-1"):
        self.platform_worker, self.sub = platform_worker, sub


@pytest.fixture
def _monde(monkeypatch):
    """`ligne` = la ligne AVANT le rapport ; `apres` = celle qu'il laisse (None : inchangée)."""
    etat = {"tenu": True, "ligne": {"statut": US.CONNECTE, "sandbox_id": "sb"},
            "apres": None, "pool": False, "rendus": [], "rapports": []}

    def _noter(conclu, ok, res):
        etat["rapports"].append(res)
        if etat["apres"] is not None:
            etat["ligne"] = etat["apres"]
    monkeypatch.setattr(RJ.db, "porteur_et_famille", lambda job_id: {
        "sub": "demandeur", "org_id": 7, "model_family": _FAMILLE, "abonnement": "demandeur",
        "pool": etat["pool"]})
    monkeypatch.setattr(RJ.db, "tenu_par", lambda job_id, sub: etat["tenu"])
    monkeypatch.setattr(RJ._abonnement, "noter_rapport", _noter)
    monkeypatch.setattr(RJ.user_subscriptions, "get_subscription",
                        lambda sub, famille: etat["ligne"])
    monkeypatch.setattr(RJ.db, "rendre_a_la_file",
                        lambda job_id, sub, raison, delai_s=60:
                        etat["rendus"].append((job_id, sub, delai_s, raison)) or True)
    return etat


def _forfait(ctx=None, result=_RAPPORT):
    return RJ._trancher_le_forfait(ctx or _Ctx(), RJ.JobsInput(op="forfait", job_id=11,
                                                                result=result))


def _au_plafond(reprise, fenetre=None):
    return {"statut": US.PLAFOND, "sandbox_id": "sb", "limit_reset_at": _en_base(reprise),
            "limit_fenetre": fenetre}


def test_un_forfait_en_pause_rend_le_travail_SANS_tentative(_monde):
    """L'échéance est lue telle que la base la rend (un texte) : la comparer à un
    `datetime` sans la convertir levait à chaque verdict."""
    reprise = _dans(hours=2)
    _monde["apres"] = _au_plafond(reprise, "five_hour")
    out = _forfait()
    assert out["servable"] is False and out["limit_reset_at"] == reprise.isoformat()
    assert _monde["rapports"] == [_RAPPORT]
    ((job, worker, delai, _),) = _monde["rendus"]
    assert (job, worker, delai) == (11, "worker-1", US.VERIFICATION_S)


def test_un_forfait_connecte_laisse_partir(_monde):
    assert _forfait() == {"ok": True, "servable": True}
    assert _monde["rendus"] == []


def test_une_echeance_passee_n_est_pas_une_pause(_monde):
    _monde["ligne"] = _au_plafond(_dans(minutes=-1))
    assert _forfait()["servable"] is True


def test_un_membre_ne_met_personne_en_pause(_monde):
    with pytest.raises(AuthzDenied) as e:
        _forfait(_Ctx(platform_worker=False))
    assert e.value.status == 403 and _monde["rapports"] == []


def test_on_ne_parle_que_d_un_travail_qu_on_tient(_monde):
    _monde["tenu"] = False
    with pytest.raises(AuthzDenied) as e:
        _forfait()
    assert e.value.status == 404 and _monde["rapports"] == []


# ── 1. servable seulement `connected` ou en pause échue ─────────────────────────
@pytest.mark.parametrize("ligne", [
    {"statut": US.A_RECONNECTER, "sandbox_id": "sb"},
    {"statut": US.DECONNECTE, "sandbox_id": "sb"},
    {"statut": US.CONNECTE, "sandbox_id": None},
    {},
], ids=["needs_login", "disconnected", "sans_sandbox", "sans_ligne"])
def test_seuls_connected_ou_une_pause_echue_sont_servables(_monde, ligne):
    """La ligne `needs_login` répondait « servable » : le run partait sur une session
    perdue. Tout ce qui n'est ni connecté ni en pause échue retourne à la file."""
    _monde["ligne"] = ligne
    out = _forfait()
    assert out == {"ok": True, "servable": False, "limit_reset_at": None}
    assert [r[:3] for r in _monde["rendus"]] == [(11, "worker-1", US.VERIFICATION_S)]


# ── 2. un rapport illisible est une 400 nommée et déclarée ──────────────────────
@pytest.mark.parametrize("result", [
    {"autre": 1},
    {"abonnement": "rejected"},
    {"abonnement": {}},
    {"abonnement": {"etat": None, "fenetres": None}},
    {"abonnement": {"etat": 3}},
    {"abonnement": {"etat": "allowed", "deconnecte": "oui"}},
    {"abonnement": {"etat": "rejected", "echeance": "demain"}},
    {"abonnement": {"etat": "rejected", "echeance": True}},
    {"abonnement": {"etat": "allowed", "fenetres": ["five_hour"]}},
    {"abonnement": {"etat": "allowed", "fenetres": {"five_hour": 0.5}}},
    {"abonnement": {"etat": "allowed",
                    "fenetres": {"five_hour": {"utilization": "50 %"}}}},
], ids=lambda r: str(r)[:40])
def test_un_rapport_illisible_est_REFUSE_pas_avale(_monde, result):
    with pytest.raises(AuthzDenied) as e:
        _forfait(result=result)
    assert (e.value.status, e.value.code) == (400, "forfait_report_unreadable")
    assert _monde["rapports"] == [] and _monde["rendus"] == []


def test_le_refus_du_rapport_illisible_est_declare():
    (cap,) = [c for c in registry.CAPABILITIES if c.key == "runner.jobs"]
    assert (400, "forfait_report_unreadable") in {(e.status, e.code) for e in cap.errors}


def test_un_rapport_lisible_passe_la_garde():
    for rapport in ({"etat": "allowed", "fenetres": None},
                    {"etat": None, "fenetres": {"five_hour": {"utilization": 0.2,
                                                              "resetsAt": 1790029800}}},
                    {"deconnecte": True}, _RAPPORT["abonnement"]):
        assert _abonnement.exiger_rapport_lisible({"abonnement": rapport}) is rapport


# ── 3. une pause propre à un modèle n'est pas levée par la sonde ────────────────
def test_une_pause_PROPRE_A_UN_MODELE_n_est_pas_levee_par_la_sonde(_monde):
    """oto-runner sonde sur son plus petit modèle : un « allowed » n'y dit rien de la
    fenêtre `seven_day_opus`. Le rapport n'est pas porté, la pause attend."""
    reprise = _dans(days=3)
    _monde["ligne"] = _au_plafond(reprise, "seven_day_opus")
    _monde["apres"] = {"statut": US.CONNECTE, "sandbox_id": "sb"}
    out = _forfait(result={"abonnement": {"etat": "allowed", "fenetres": {}}})
    assert out == {"ok": True, "servable": False, "limit_reset_at": reprise.isoformat()}
    assert _monde["rapports"] == [] and len(_monde["rendus"]) == 1


def test_une_pause_du_COMPTE_est_levee_par_la_sonde(_monde):
    _monde["ligne"] = _au_plafond(_dans(hours=3), "five_hour")
    _monde["apres"] = {"statut": US.CONNECTE, "sandbox_id": "sb"}
    assert _forfait(result={"abonnement": {"etat": "allowed"}})["servable"] is True
    assert len(_monde["rapports"]) == 1


def test_une_fenetre_inconnue_est_propre_a_un_modele():
    assert not US.propre_a_un_modele(None)
    assert not US.propre_a_un_modele("five_hour") and not US.propre_a_un_modele("seven_day")
    assert US.propre_a_un_modele("seven_day_opus") and US.propre_a_un_modele("seven_day_sonnet")
    assert US.propre_a_un_modele("fenetre_de_demain")


# ── 4. coût : ne sonder que les pauses anciennes, se fier à un refus nommé ──────
def test_une_pause_ANCIENNE_cede_a_la_sonde_SAUF_fenetre_propre_a_un_modele():
    """Fenêtre connue ou non (28/10/2026) : seule une fenêtre propre à un modèle tient."""
    assert "updated_at" not in US.pause_en_vigueur("ab")
    assert "limit_fenetre" not in US.pause_en_vigueur("ab")
    sonde = US.pause_en_vigueur("ab", sonde=True)
    assert f"make_interval(secs => {US.VERIFICATION_S})" in sonde
    assert ("ab.limit_fenetre NOT IN ('five_hour', 'seven_day') "
            "OR (ab.limit_epuise AND ab.limit_fenetre IS NOT NULL) OR ab.updated_at"
            in sonde)
    assert sonde.startswith("COALESCE(") and sonde.endswith(", false)")


@pytest.fixture
def _remise(monkeypatch):
    etat = {"ligne": {"statut": US.CONNECTE, "sandbox_id": "sb"}}
    monkeypatch.setattr(RJ.db, "porteur_du_forfait", lambda job: "demandeur")
    monkeypatch.setattr(RJ.user_subscriptions, "get_subscription",
                        lambda sub, famille: etat["ligne"])
    return etat


def _remettre():
    return RJ._avec_abonnement({"id": 11, "org_id": 7, "sub": "demandeur",
                                "payload": {"model_family": _FAMILLE,
                                            "_plateforme": {"pool": False}}},
                                _FAMILLE, "worker-1")


def test_un_forfait_connecte_ne_se_sonde_pas(_remise):
    servi = _remettre()
    assert servi["sandbox_id"] == "sb" and "forfait_a_sonder" not in servi


def test_un_travail_servi_EN_PAUSE_demande_la_sonde(_remise):
    _remise["ligne"] = _au_plafond(_dans(hours=4))
    assert _remettre()["forfait_a_sonder"] is True


def test_une_pause_echue_ne_se_sonde_pas(_remise):
    _remise["ligne"] = _au_plafond(_dans(minutes=-5))
    assert "forfait_a_sonder" not in _remettre()


# ── 5. délai court en mode pool ─────────────────────────────────────────────────
def test_en_POOL_le_travail_revient_vite_pour_un_autre_preteur(_monde):
    _monde["pool"] = True
    _monde["apres"] = _au_plafond(_dans(hours=2), "five_hour")
    assert _forfait()["servable"] is False
    ((_, _, delai, raison),) = _monde["rendus"]
    assert delai == US.RENDU_EN_POOL_S < US.VERIFICATION_S
    assert "prochain abonnement prêté" in raison


# ── 6. le repli sur clé ne dépend pas de `forfait_sonde` ────────────────────────
def test_le_repli_sur_cle_ignore_la_sonde():
    for f in (DBRJ.repli_disponible, DBRJ.claim_fallback_job,
              DBRJ.candidats_repli_abonnement, DBRJ._jointures_de_repli,
              DBRJ._pause_pour_l_org):
        assert "sonde" not in inspect.signature(f).parameters, f.__name__
    sql = DBRJ._jointures_de_repli(80)
    # La fraîcheur de la SONDE (`updated_at >` une fenêtre, `limit_fenetre`) n'y est
    # pas. `updated_at <=` y est, depuis le 22/11/2026 : l'ANCIENNETÉ d'une déconnexion
    # (`_deconnecte`, `DECONNEXION_TENUE_S`), qui ne lit jamais un `paused_limit`.
    assert "updated_at >" not in sql and "limit_fenetre" not in sql
    assert US.pause_en_vigueur("ab", True) not in sql
    assert DBRJ._deconnecte("ab") in sql


# ── 7. le texte servi dit « au plafond jusqu'au … », pas « reconnecter » ────────
@pytest.mark.parametrize("pool", [False, True])
def test_le_texte_d_un_forfait_au_plafond_dit_jusqu_a_quand(_monde, pool):
    _monde["pool"] = pool
    reprise = _dans(hours=2)
    _monde["apres"] = _au_plafond(reprise, "five_hour")
    _forfait()
    ((_, _, _, raison),) = _monde["rendus"]
    assert f"au plafond jusqu'au {reprise:%d/%m/%Y %H:%M} UTC" in raison
    assert "reconnect" not in raison


def test_une_session_perdue_dit_toujours_de_se_reconnecter(_monde):
    _monde["ligne"] = {"statut": US.A_RECONNECTER, "sandbox_id": "sb"}
    _forfait()
    ((_, _, _, raison),) = _monde["rendus"]
    assert "reconnecter" in raison
