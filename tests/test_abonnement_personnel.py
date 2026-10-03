"""Un travail qui tourne sur l'ABONNEMENT d'une personne (OTO-130).

Ce que ces bancs tiennent, et qui n'est pas négociable :

1. **Aucune clé n'est cherchée** pour un travail d'abonnement — ni celle de
   l'org, ni celle de la plateforme. Le travail s'exécute dans le sandbox de
   son demandeur, où le programme officiel lit la session que cette personne y a
   ouverte elle-même. Un chemin qui irait au coffre serait la preuve que la
   plateforme paie, ou intermédie, ce qu'elle n'a pas le droit d'intermédier.
2. **Rien de ce qui sort d'ici ne ressemble à un secret** : le travail servi
   gagne un `sandbox_id`, et c'est tout.
3. **Un porteur sans connexion ARRÊTE le travail**, raison écrite — jamais une
   remise en file qui tournerait indéfiniment.
4. **Un abonnement ne sert que les agents de son propriétaire** : ni une flotte,
   ni l'agent d'un collègue.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import _abonnement, runner_jobs as RJ
from oto_mcp.db import user_subscriptions as US

_WORKER = "svc-runner-worker"
_PORTEUR = "personne-qui-paie"
_FAMILLE = "claude_subscription"
_BAC = "sandbox-a-sable-1"


def _travail(sub=_PORTEUR, famille=_FAMILLE, **extra):
    return {"id": 42, "org_id": 7, "sub": sub,
            "payload": {"procedure": "p", "model": "sub:sonnet",
                        "model_family": famille},
            **extra}


@pytest.fixture(autouse=True)
def _refus_sans_base(monkeypatch):
    """Arrêter un travail ÉCRIT sa raison en base (`arreter_definitivement`). Ces
    bancs jugent le verdict, pas l'écriture : elle a son propre banc."""
    arrets: list[tuple] = []
    monkeypatch.setattr(RJ.db, "arreter_definitivement",
                        lambda job_id, appelant, raison: arrets.append(
                            (job_id, appelant, raison)))
    return arrets


@pytest.fixture(autouse=True)
def _mode_personnel(monkeypatch):
    """L'org de ces bancs n'a réglé aucun mode : `personnel`, le défaut. Le mode `pool`
    a ses propres bancs (`test_pool_abonnements*.py`)."""
    from oto_mcp.db import org_subscription_pool
    monkeypatch.setattr(org_subscription_pool, "get_mode", lambda org_id, famille: None)


@pytest.fixture
def _abonnements(monkeypatch):
    """L'état des abonnements, en mémoire — et la TRACE des lectures du coffre.

    Le coffre est doublé par une levée : si un chemin d'abonnement allait y
    chercher une clé, le banc tomberait au lieu de passer en silence."""
    lignes: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(US, "get_subscription",
                        lambda sub, famille: lignes.get((sub, famille)))
    monkeypatch.setattr(
        "oto_mcp.capabilities.runner_jobs._cle_de_modele",
        lambda *a, **k: pytest.fail(
            "un travail d'abonnement est allé chercher une CLÉ : la plateforme ne "
            "paie pas ce chemin, et ne détient aucune session"))
    return lignes


def _servi(job, depot=_FAMILLE, appelant=_WORKER):
    return RJ._avec_cle(job, depot, appelant, worker=True, org_key_only=True)


def test_connecte_sert_le_bac_a_sable_et_aucune_cle(_abonnements):
    _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.CONNECTE, "sandbox_id": _BAC}
    servi = _servi(_travail())
    assert servi["sandbox_id"] == _BAC
    # Le contrat NÉGATIF, celui qui compte : rien qui puisse porter un secret.
    assert "model_key" not in servi and "delegation_refusee" not in servi


def test_jamais_connecte_arrete_le_travail_en_le_disant(_abonnements, _refus_sans_base):
    servi = _servi(_travail())
    raison = servi["delegation_refusee"]
    assert "abonnement" in raison and "Réglages" in raison
    assert "sandbox_id" not in servi
    # ARRÊTÉ, pas remis en file : sans cette écriture, le worker suivant le
    # reprendrait, indéfiniment, sans que personne n'apprenne pourquoi.
    assert [a[0] for a in _refus_sans_base] == [42]


def test_sans_bac_a_sable_il_n_y_a_RIEN_a_attendre_le_travail_s_arrete(
        _abonnements, _refus_sans_base):
    """Depuis le 21/09/2026, qui doit se RECONNECTER voit ses travaux attendre (banc
    plus bas). Ce qui s'arrête encore : l'état sans sandbox — personne ne
    reviendra « reconnecter » ce qui n'a jamais existé, et un travail en attente
    éternelle est un silence."""
    _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.A_RECONNECTER,
                                          "sandbox_id": None}
    assert "delegation_refusee" in _servi(_travail())
    assert [a[0] for a in _refus_sans_base] == [42]


def test_sans_porteur_rien_a_consommer(_abonnements):
    """Un travail sans demandeur n'a aucun abonnement à consommer — et surtout pas
    celui de quelqu'un d'autre."""
    assert "delegation_refusee" in _servi(_travail(sub=None))


def test_un_plafond_n_est_jamais_un_refus_a_la_garde(_abonnements):
    """L'attente de l'échéance vit dans la RÉSERVATION, pas ici : ce qui arrive à
    la garde a une échéance passée ou inconnue. Le refuser tuerait le travail pour
    un plafond expiré (la couture est jugée en base, `_db`)."""
    _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.PLAFOND, "sandbox_id": _BAC,
                                          "limit_reset_at": None}
    assert _servi(_travail())["sandbox_id"] == _BAC


def test_un_simple_membre_ne_recoit_ni_bac_a_sable_ni_pouvoir_d_arreter(
        _abonnements, _refus_sans_base, monkeypatch):
    """La file n'est pas réservée aux workers : un membre d'org peut réserver. Il ne
    doit ni recevoir le sandbox d'un collègue, ni — pire — pouvoir ARRÊTER
    DÉFINITIVEMENT son travail parce que ce collègue n'est pas connecté."""
    monkeypatch.setattr(RJ, "_depot_pose", lambda org, depot: False)
    servi = RJ._avec_cle(_travail(), _FAMILLE, "un-membre-ordinaire",
                         worker=False, org_key_only=True)
    assert "sandbox_id" not in servi and "delegation_refusee" not in servi
    assert _refus_sans_base == [], "un membre a fait arrêter le travail d'un autre"


def test_une_autre_famille_garde_le_chemin_des_cles(monkeypatch):
    """La garde d'abonnement ne doit rien changer pour un travail ordinaire : le
    chemin de la clé reste celui d'avant, refus compris."""
    monkeypatch.setattr("oto_mcp.capabilities.runner_jobs._cle_de_modele",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    servi = RJ._avec_cle(_travail(famille="anthropic"), "anthropic", _WORKER,
                         worker=True, org_key_only=True)
    assert "clé" in servi["delegation_refusee"]


@pytest.fixture(autouse=True)
def _option_ouverte(monkeypatch):
    """Ces bancs jugent les AUTRES refus : l'option est posée. Son absence a son banc."""
    ouverts: set = {_PORTEUR, "un-collegue", "quelqu-un-d-autre"}
    monkeypatch.setattr(_abonnement.access, "has_option",
                        lambda sub, option, **k: option == _abonnement.OPTION and sub in ouverts)
    return ouverts


class TestOption:
    """Le chemin n'est ouvert qu'à des personnes NOMMÉES (option `claude_subscription`)."""

    def test_sans_l_option_la_pose_est_refusee_avant_tout_le_reste(self, _abonnements,
                                                                   _option_ouverte):
        _option_ouverte.discard(_PORTEUR)
        _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.CONNECTE, "sandbox_id": _BAC}
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose(_PORTEUR, _PORTEUR, _FAMILLE)
        assert e.value.code == "subscription_not_enabled"

    def test_un_modele_ordinaire_ne_demande_aucune_option(self, _option_ouverte):
        _option_ouverte.clear()
        _abonnement.exiger_a_la_pose(_PORTEUR, _PORTEUR, "anthropic")


class TestEnfilage:
    """Le QUATRIÈME chemin de pose : un travail enfilé à la main (revue du 23/09/2026)."""

    def _ctx(self):
        from oto_mcp.capabilities._types import ResolvedCtx
        return ResolvedCtx(sub=_PORTEUR, org_id=2)

    def test_une_flotte_ne_s_enfile_pas_sur_un_abonnement(self, _abonnements):
        _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.CONNECTE, "sandbox_id": _BAC}
        with pytest.raises(Exception) as e:
            RJ._charge_et_modele(self._ctx(), RJ.JobsInput(
                op="enqueue", kind="start", fleet_id=9,
                payload={"input": "vas-y", "model": "sub:sonnet"}))
        assert e.value.code == "subscription_personal_only"

    def test_sans_l_option_rien_ne_s_enfile(self, _abonnements, _option_ouverte):
        _option_ouverte.clear()
        with pytest.raises(Exception) as e:
            RJ._charge_et_modele(self._ctx(), RJ.JobsInput(
                op="enqueue", kind="start", payload={"input": "vas-y", "model": "sub:sonnet"}))
        assert e.value.code == "subscription_not_enabled"

    def test_connecte_et_ouvert_s_enfile(self, _abonnements):
        _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.CONNECTE, "sandbox_id": _BAC}
        charge = RJ._charge_et_modele(self._ctx(), RJ.JobsInput(
            op="enqueue", kind="start", payload={"input": "vas-y", "model": "sub:sonnet"}))
        assert charge["model_family"] == _FAMILLE


class TestPose:
    """Le refus LISIBLE au moment de poser un agent sur un abonnement."""

    def test_flotte_refusee(self, _abonnements):
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose(_PORTEUR, _PORTEUR, _FAMILLE, flotte=True)
        assert e.value.code == "subscription_personal_only"

    def test_agent_d_un_collegue_refuse(self, _abonnements):
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose(_PORTEUR, "quelqu-un-d-autre", _FAMILLE)
        assert e.value.code == "subscription_personal_only"

    def test_sans_connexion_refuse(self, _abonnements):
        with pytest.raises(Exception) as e:
            _abonnement.exiger_a_la_pose(_PORTEUR, _PORTEUR, _FAMILLE)
        assert e.value.code == "subscription_not_connected"

    def test_connecte_passe(self, _abonnements):
        _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.CONNECTE,
                                              "sandbox_id": _BAC}
        _abonnement.exiger_a_la_pose(_PORTEUR, _PORTEUR, _FAMILLE)

    def test_un_modele_ordinaire_ne_passe_pas_par_cette_garde(self, _abonnements):
        """Aucune lecture, aucun refus : une famille qui n'est pas un abonnement
        sort immédiatement."""
        _abonnement.exiger_a_la_pose(_PORTEUR, "quelqu-un-d-autre", "anthropic",
                                     flotte=True)


class TestCablage:
    """La garde est-elle POSÉE là où un agent se déclare ? Le banc de `TestPose`
    juge la fonction ; celui-ci juge qu'elle est appelée — c'est l'oubli qui
    coûterait cher, pas la logique."""

    def _ctx(self, sub=_PORTEUR, org_id=2):
        from oto_mcp.capabilities._types import ResolvedCtx
        return ResolvedCtx(sub=sub, org_id=org_id)

    @pytest.fixture(autouse=True)
    def _plateforme(self, monkeypatch, _abonnements):
        """Un runner armé qui sert la famille, et aucune clé exigée : le seul refus
        qui reste possible est celui de l'abonnement."""
        from oto_mcp.capabilities import runner_fleets as RF
        from oto_mcp.capabilities import runner_triggers as RT
        for module in (RT, RF):
            monkeypatch.setattr(module.db, "runner_arme",
                                lambda org: {"armed": True, "workers": 1,
                                             "last_seen": "2026-09-19 07:00:00",
                                             "families": [_FAMILLE]})
        monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                            lambda *a, **k: None)
        monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
        # La population « bêta » et les outils de la procédure se lisent en base ;
        # ces bancs ne parlent ni de l'une ni des autres.
        # ⚠️ Par le NOM IMPORTÉ dans chaque module : `access` est une surface plate,
        # et doubler `access.quotas.has_option` ne change pas le nom déjà lié ici.
        monkeypatch.setattr(RT, "_outils_de_la_procedure", lambda ctx, p: ["oto_doc"])

    def test_un_declencheur_sur_un_abonnement_non_connecte_est_refuse(self, monkeypatch):
        import asyncio

        from oto_mcp.capabilities import runner_triggers as RT
        monkeypatch.setattr(RT.db, "create_trigger",
                            lambda *a, **k: pytest.fail(
                                "écrit malgré un abonnement non connecté"))
        with pytest.raises(Exception) as e:
            asyncio.run(RT._triggers(self._ctx(), RT.TriggerInput(
                op="create", procedure="p", cron="0 8 * * *", tz="Europe/Paris",
                model="sub:sonnet")))
        assert e.value.code == "subscription_not_connected"

    def test_une_flotte_ne_se_pose_JAMAIS_sur_un_abonnement(self, monkeypatch,
                                                            _abonnements):
        """Même connectée, une flotte est refusée : elle appartient à l'org."""
        import asyncio

        from oto_mcp.capabilities import runner_fleets as RF
        _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.CONNECTE,
                                              "sandbox_id": _BAC}
        monkeypatch.setattr(RF.db, "create_fleet",
                            lambda *a, **k: pytest.fail("flotte écrite sur un forfait"))
        with pytest.raises(Exception) as e:
            asyncio.run(RF._fleets(self._ctx(), RF.FleetInput(
                op="create", label="passage", namespace="n", procedure="p",
                tools=["oto_doc"], model="sub:sonnet")))
        assert e.value.code == "subscription_personal_only"

    def test_retoucher_le_modele_d_un_agent_ALLUME_passe_par_la_garde(self, monkeypatch):
        """Le troisième chemin de pose : ni création, ni rallumage. Un collègue ne
        pointe pas l'agent vivant d'un autre sur le forfait de celui-ci."""
        import asyncio

        from oto_mcp.capabilities import runner_triggers as RT
        monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {
            "id": 3, "org_id": 2, "sub": "le-proprietaire", "enabled": True,
            "kind": "schedule", "model": "claude-sonnet-5", "procedure": "p"})
        monkeypatch.setattr(RT.db, "update_trigger",
                            lambda *a, **k: pytest.fail("retouche écrite"))
        # Même PARTAGÉ en écriture avec lui : le forfait d'un autre ne se prête pas
        # par le partage d'un agent.
        from oto_mcp.capabilities import _acces_agent
        monkeypatch.setattr(_acces_agent, "niveaux",
                            lambda sub, org, agents: {3: "editor"})
        with pytest.raises(Exception) as e:
            asyncio.run(RT._triggers(self._ctx(sub="un-collegue"), RT.TriggerInput(
                op="update", trigger_id=3, model="sub:sonnet")))
        assert e.value.code == "subscription_personal_only"


class TestRapport:
    """Ce que le worker a vu du forfait, porté sur la connexion du demandeur.
    La forme du rapport est celle du `rate_limit_event` mesuré le 21/09/2026."""

    @pytest.fixture
    def _ecrits(self, monkeypatch):
        ecrits: list[tuple] = []
        monkeypatch.setattr(
            US, "marquer_statut",
            lambda sub, famille, statut, **k: ecrits.append((sub, statut, k)))
        return ecrits

    @pytest.fixture(autouse=True)
    def _plafonds(self, monkeypatch):
        """Les deux plafonds, sans base : `org` (None = rien réglé) et `perso`."""
        reglages = {"org": None, "perso": None}
        monkeypatch.setattr(
            _abonnement.org_subscription_limits, "get_limite",
            lambda org_id, famille: (None if reglages["org"] is None
                                     else {"limite_pct": reglages["org"]}))
        monkeypatch.setattr(
            US, "get_subscription",
            lambda sub, famille: {"statut": US.CONNECTE, "limite_pct": reglages["perso"]})
        return reglages

    def _conclu(self, famille=_FAMILLE, sub=_PORTEUR, org_id=7):
        return {"status": "done", "run_id": None, "sub": sub, "org_id": org_id,
                "model_family": famille}

    # ── le plafond (25/09/2026) ──────────────────────────────────────────────
    def test_le_defaut_est_80_quand_l_org_n_a_rien_regle(self, _plafonds):
        assert _abonnement.DEFAUT_LIMITE_PCT == 80
        assert _abonnement.seuil(_PORTEUR, 7, _FAMILLE) == 0.80

    def test_le_seuil_est_le_MIN_de_l_org_et_du_perso(self, _plafonds):
        _plafonds.update(org=90, perso=70)
        assert _abonnement.seuil(_PORTEUR, 7, _FAMILLE) == 0.70
        _plafonds.update(org=60, perso=70)
        assert _abonnement.seuil(_PORTEUR, 7, _FAMILLE) == 0.60

    def test_un_perso_plus_HAUT_ne_relache_rien(self, _plafonds):
        _plafonds.update(org=None, perso=100)
        assert _abonnement.seuil(_PORTEUR, 7, _FAMILLE) == 0.80
        _plafonds.update(org=50, perso=95)
        assert _abonnement.seuil(_PORTEUR, 7, _FAMILLE) == 0.50

    def test_le_perso_seul_resserre_le_defaut(self, _plafonds):
        _plafonds.update(org=None, perso=30)
        assert _abonnement.seuil(_PORTEUR, 7, _FAMILLE) == 0.30

    def _rapport(self, cinq_h):
        return {"abonnement": {"etat": "allowed",
                               "fenetres": self._fenetres(cinq_h=cinq_h)}}

    def test_081_MET_en_pause_avec_l_org_a_80(self, _ecrits, _plafonds):
        _plafonds.update(org=80)
        _abonnement.noter_rapport(self._conclu(), True, self._rapport(0.81))
        (_, statut, k), = _ecrits
        assert statut == US.PLAFOND
        assert int(k["limit_reset_at"].timestamp()) == 1790029800

    def test_081_ne_met_PAS_en_pause_avec_l_org_a_90_sans_perso(self, _ecrits, _plafonds):
        _plafonds.update(org=90, perso=None)
        _abonnement.noter_rapport(self._conclu(), True, self._rapport(0.81))
        assert [st for _, st, _ in _ecrits] == [US.CONNECTE]

    def test_081_MET_en_pause_avec_l_org_a_90_et_un_perso_a_75(self, _ecrits, _plafonds):
        _plafonds.update(org=90, perso=75)
        _abonnement.noter_rapport(self._conclu(), True, self._rapport(0.81))
        assert [st for _, st, _ in _ecrits] == [US.PLAFOND]

    def test_le_seuil_vaut_pour_la_fenetre_de_SEPT_jours_aussi(self, _ecrits, _plafonds):
        _plafonds.update(org=80)
        _abonnement.noter_rapport(self._conclu(), True, {"abonnement": {
            "etat": "allowed", "fenetres": self._fenetres(cinq_h=0.1, sept_j=0.85)}})
        (_, statut, k), = _ecrits
        assert statut == US.PLAFOND
        assert int(k["limit_reset_at"].timestamp()) == 1790053200

    def test_c_est_l_org_DU_TRAVAIL_qui_est_lue(self, _ecrits, monkeypatch):
        lues = []
        monkeypatch.setattr(_abonnement.org_subscription_limits, "get_limite",
                            lambda org_id, famille: lues.append(org_id) or None)
        _abonnement.noter_rapport(self._conclu(org_id=31), True, self._rapport(0.5))
        assert lues == [31]

    def test_un_plafond_illisible_ne_fait_JAMAIS_echouer_la_conclusion(
            self, _ecrits, monkeypatch):
        def _hoquet(org_id, famille):
            raise RuntimeError("connexion perdue")
        monkeypatch.setattr(_abonnement.org_subscription_limits, "get_limite", _hoquet)
        _abonnement.noter_rapport(self._conclu(), True, self._rapport(0.99))
        assert _ecrits == []

    def test_une_echeance_aberrante_est_BORNEE(self, _ecrits):
        """Un `resetsAt` en millisecondes suspendrait la personne pour des siècles."""
        from datetime import datetime, timezone
        _abonnement.noter_rapport(self._conclu(), True, {"abonnement": {
            "etat": "allowed",
            "fenetres": {"five_hour": {"utilization": 1.0, "resetsAt": 1790216400000}}}})
        ((_, statut, k),) = _ecrits
        assert statut == US.PLAFOND
        assert k["limit_reset_at"] <= datetime.now(timezone.utc) + _abonnement.ECHEANCE_MAX

    def _fenetres(self, cinq_h=0.07, sept_j=0.49):
        return {"five_hour": {"utilization": cinq_h, "resetsAt": 1790029800},
                "seven_day": {"utilization": sept_j, "resetsAt": 1790053200}}

    def test_un_usage_sain_confirme_la_connexion(self, _ecrits):
        _abonnement.noter_rapport(self._conclu(), True, {
            "abonnement": {"etat": "allowed", "fenetres": self._fenetres()}})
        assert [(s, st) for s, st, _ in _ecrits] == [(_PORTEUR, US.CONNECTE)]

    def test_le_seuil_met_en_attente_AVANT_le_refus(self, _ecrits):
        """`etat` dit encore `allowed` : c'est la jauge qui arrête, pas le refus."""
        _abonnement.noter_rapport(self._conclu(), True, {
            "abonnement": {"etat": "allowed",
                           "fenetres": self._fenetres(cinq_h=0.97)}})
        (_, statut, k), = _ecrits
        assert statut == US.PLAFOND
        assert int(k["limit_reset_at"].timestamp()) == 1790029800

    def test_deux_fenetres_saturees_attendent_la_plus_LOINTAINE(self, _ecrits):
        _abonnement.noter_rapport(self._conclu(), False, {
            "abonnement": {"etat": "rejected",
                           "fenetres": self._fenetres(cinq_h=1.0, sept_j=0.99)}})
        (_, statut, k), = _ecrits
        assert statut == US.PLAFOND
        assert int(k["limit_reset_at"].timestamp()) == 1790053200, (
            "repartir à l'échéance courte, c'est retomber sur la fenêtre longue")

    def test_une_session_perdue_demande_une_reconnexion(self, _ecrits):
        _abonnement.noter_rapport(self._conclu(), False,
                                  {"abonnement": {"deconnecte": True}})
        assert _ecrits[0][1] == US.A_RECONNECTER

    def test_un_rapport_mal_forme_ne_fait_JAMAIS_echouer_la_conclusion(self, _ecrits):
        """Un `complete` qui lèverait laisserait un travail TERMINÉ re-servi à
        l'expiration de son bail."""
        for tordu in ({"abonnement": {"fenetres": "pas un dict"}},
                      {"abonnement": {"fenetres": {"five_hour": {"utilization": "x"}}}},
                      {"abonnement": []}):
            _abonnement.noter_rapport(self._conclu(), True, tordu)

    def test_une_autre_famille_n_ecrit_rien(self, _ecrits):
        _abonnement.noter_rapport(self._conclu(famille="anthropic"), True, {
            "abonnement": {"deconnecte": True}})
        assert _ecrits == []


def test_un_worker_d_abonnement_ne_prend_QUE_sa_famille_meme_sans_le_demander(monkeypatch):
    """Le drapeau `org_key_only` oublié sur l'unité systemd ne doit pas suffire à
    lui faire voler — et casser — les agents historiques posés sans modèle."""
    import asyncio

    from oto_mcp.capabilities._types import ResolvedCtx
    vus = []
    monkeypatch.setattr(RJ.db, "claim_next_job",
                        lambda *a, **k: vus.append(k) or None)
    monkeypatch.setattr(RJ, "_produire_pour_une_campagne", lambda *a, **k: None)
    # Un worker de PLATEFORME : pas d'org, et c'est un fait (`WORKER_OR_ORG_MEMBER`).
    ctx = ResolvedCtx(sub="svc-worker", org_id=None, platform_worker=True)
    RJ._jobs(ctx, RJ.JobsInput(op="claim", provider=_FAMILLE))
    assert vus and all(k["famille_seule"] is True for k in vus)
    # Et rien ne change pour les autres : le drapeau reste leur choix.
    vus.clear()
    RJ._jobs(ctx, RJ.JobsInput(op="claim", provider="mistral"))
    assert vus and all(k["famille_seule"] is False for k in vus)


def test_une_base_qui_hoquette_ne_casse_PAS_la_conclusion(monkeypatch):
    """Le travail est déjà conclu quand le rapport se lit : une levée ici sortirait
    en 500, et la libération des lignes du run ne s'exécuterait jamais."""
    def _hoquet(job_id):
        raise RuntimeError("connexion perdue")
    monkeypatch.setattr(_abonnement.db_runner_jobs, "porteur_et_famille", _hoquet)
    _abonnement.noter_rapport_du_travail(42, True, {"abonnement": {"etat": "allowed"}})


class TestRetoucheParUnAutre:
    """Arbitré le 21/09/2026 : modifier l'agent d'un autre posé sur un abonnement
    n'est permis que si sa connexion est PARTAGÉE avec soi. Aucun partage n'existe
    encore — donc le propriétaire seul, et la couture est `peut_agir_pour`."""

    _AGENT = {"id": 3, "sub": "le-proprietaire", "model": "sub:sonnet"}

    def test_changer_la_consigne_de_l_agent_d_un_autre_est_refuse(self):
        with pytest.raises(Exception) as e:
            _abonnement.exiger_le_droit_de_modifier(
                "un-collegue", self._AGENT, _FAMILLE, {"input": "fais autre chose"})
        assert e.value.code == "subscription_personal_only"

    def test_l_ETEINDRE_reste_permis(self):
        """Personne ne doit avoir besoin du propriétaire pour arrêter un agent."""
        _abonnement.exiger_le_droit_de_modifier(
            "un-collegue", self._AGENT, _FAMILLE, {"enabled": False})

    def test_le_RALLUMER_ne_l_est_pas(self):
        with pytest.raises(Exception):
            _abonnement.exiger_le_droit_de_modifier(
                "un-collegue", self._AGENT, _FAMILLE, {"enabled": True})

    def test_le_proprietaire_modifie_librement(self):
        _abonnement.exiger_le_droit_de_modifier(
            "le-proprietaire", self._AGENT, _FAMILLE, {"input": "x", "tools": ["a"]})

    def test_un_agent_ordinaire_n_est_pas_concerne(self):
        _abonnement.exiger_le_droit_de_modifier(
            "un-collegue", {"sub": "le-proprietaire", "model": "claude-sonnet-5"},
            "anthropic", {"input": "x"})

    def test_la_couture_du_partage_est_UNIQUE(self, monkeypatch):
        """Le jour où une connexion se partage, UNE fonction change — et les trois
        chemins de pose comme la retouche suivent. Ce banc tient qu'ils la lisent."""
        monkeypatch.setattr(_abonnement, "peut_agir_pour", lambda *a: True)
        monkeypatch.setattr(US, "get_subscription",
                            lambda s, f: {"statut": US.CONNECTE, "sandbox_id": _BAC})
        _abonnement.exiger_le_droit_de_modifier(
            "un-collegue", self._AGENT, _FAMILLE, {"input": "x"})
        _abonnement.exiger_a_la_pose("un-collegue", "le-proprietaire", _FAMILLE)


def test_a_la_garde_un_etat_REPARABLE_rend_le_travail_au_lieu_de_l_arreter(
        _abonnements, _refus_sans_base, monkeypatch):
    rendus = []
    monkeypatch.setattr(RJ.db, "rendre_a_la_file",
                        lambda job_id, appelant, raison: rendus.append((job_id, raison)))
    _abonnements[(_PORTEUR, _FAMILLE)] = {"statut": US.A_RECONNECTER, "sandbox_id": _BAC}
    servi = _servi(_travail())
    assert [r[0] for r in rendus] == [42] and _refus_sans_base == [], (
        "rendu à la file — pas arrêté")
    assert "repartira" in servi["delegation_refusee"]
    assert servi.get("delegated_token") is None and "sandbox_id" not in servi
