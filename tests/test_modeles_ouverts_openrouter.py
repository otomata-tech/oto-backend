"""Les modèles OUVERTS servis par OpenRouter (famille `openrouter`).

Ce que ces bancs tiennent :

1. **le catalogue** porte les modèles dans la famille `openrouter`, après les familles
   d'avant (le défaut proposé ne bouge pas), chacun avec son effort, son plafond de
   complétion et ses hôtes épinglés — et le travail les emporte (`charge`) ;
2. **le dépôt de clé** `openrouter` est un porteur de clé (aucun outil), à clé de
   plateforme FERMÉE : c'est ce qui permet à la réservation de le remettre au worker ;
3. **un tour sur la clé partagée** n'est ni réservé ni réglé au budget d'unités : il est
   JOURNALISÉ au coût que le fournisseur a rendu (`tours_au_cout`), une ligne par tour,
   sans aucun tarif côté oto ; sans coût déclaré il est refusé, rejoué il n'est écrit
   qu'une fois ;
4. **le quota de l'accord**, en µ$ : une clé de plateforme ne s'accorde pas sans lui ;
   chaque tour sur elle est refusé une fois le plafond atteint, et son coût débité une
   fois, jamais au rejeu.
"""
from __future__ import annotations

import contextlib

import pytest

from oto_mcp import access, credentials_store, db, providers, runner_models, tours_au_cout
from oto_mcp.access import credits_tours as tours
from oto_mcp.capabilities import _cle_exigee, _quota_d_octroi, runner_jobs
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.db import usage as db_usage

FLASH = "deepseek/deepseek-v4.1-flash-1017"


@pytest.fixture(autouse=True)
def _propre(monkeypatch):
    for v in ("OTO_CREDITS_MODE", "OTO_CREDITS_MODE_FUNCTIONS", "OTO_CREDITS_JOB_FLOOR"):
        monkeypatch.delenv(v, raising=False)


# ── 1. le catalogue ─────────────────────────────────────────────────────────

def test_les_modeles_ouverts_sont_de_la_famille_openrouter_et_apres_les_autres():
    ouverts = [m for m in runner_models.MODELES if m.family == "openrouter"]
    assert [m.id for m in ouverts] == [FLASH, "deepseek/deepseek-v4-pro",
                                       "minimax/minimax-m3"]
    ids = [m.id for m in runner_models.MODELES]
    assert ids.index(FLASH) > ids.index("mistral-small-2603")
    for m in ouverts:
        assert m.providers and m.max_output_tokens and m.effort, m.id
        # jamais fp4 ni un alias `~` : la liste borne le prix réglé et la quantification
        assert not any("fp4" in p or p.startswith("~") for p in m.providers), m.id


def test_le_defaut_propose_ne_bouge_pas_quand_openrouter_est_servi():
    cat = runner_models.catalogue({"anthropic", "openrouter"})
    assert [m["id"] for m in cat if m["default"]] == ["claude-sonnet-5"]
    seul = runner_models.catalogue({"openrouter"})
    assert [m["id"] for m in seul if m["default"]] == [FLASH]


def test_le_travail_emporte_les_hotes_epingles():
    c = runner_models.charge(FLASH)
    assert c["model_family"] == "openrouter" and c["effort"] == "medium"
    assert c["max_output_tokens"] == 16000
    assert c["providers"] == ["stepfun/fp8", "streamlake/fp8", "deepinfra/fp8"]
    assert "providers" not in runner_models.charge("claude-sonnet-5")


# ── 2. le dépôt de clé ──────────────────────────────────────────────────────

def test_openrouter_est_un_porteur_de_cle_a_cle_de_plateforme_fermee():
    from oto_mcp.capabilities import _cle_exigee
    c = providers.connector_for_provider("openrouter")
    assert c.kind == "credential" and not c.namespaces
    assert {"byo_org", "platform"} <= set(c.auth_modes) and c.platform_key_open is False
    assert "openrouter" in _cle_exigee.fournisseurs_de_modele()


# ── 3. journalisé au coût, jamais tarifé ici ─────────────────────────────────

def test_seule_la_famille_openrouter_est_journalisee_au_cout():
    assert tours_au_cout.au_cout(FLASH)
    assert not tours_au_cout.au_cout("claude-sonnet-5")
    assert not tours_au_cout.au_cout("mistral-large-2512")
    assert not tours_au_cout.au_cout(None)


def test_le_cout_en_microdollars_arrondi_au_dessus():
    assert tours_au_cout.microdollars(0.00113355722) == 1134  # un tour V4 Pro, 12/11
    assert tours_au_cout.microdollars(3 * 1e-05) == 30         # pas 31
    assert tours_au_cout.microdollars(0) == 0
    for brut in (None, True, "0.1", -1e-6, float("nan"), float("inf")):
        assert tours_au_cout.cout_declare(brut) is None, brut
    assert tours_au_cout.cout_declare(0) == 0.0


class _Ctx:
    sub = "worker-1"


@pytest.fixture
def tour_partage(monkeypatch):
    vu = {"lignes": [], "existe": False, "debits": [], "mode": "platform",
          "atteint": None, "quotas_lus": []}
    monkeypatch.setattr(runner_jobs.db, "tentative_en_cours", lambda job, sub: {
        "org_id": 42, "sub": "u-1", "attempts": 2, "run_id": "run-9", "model": FLASH,
        "cle_partagee": {"tentative": 2, "mode": vu["mode"], "depot": "openrouter"}})

    def inserer(row):
        if vu["existe"]:
            return False
        vu["lignes"].append(row)
        return True
    monkeypatch.setattr(db_usage, "inserer_tour_au_cout", inserer)
    monkeypatch.setattr(db, "transaction_d_ecriture", contextlib.nullcontext)
    monkeypatch.setattr(access, "debiter_plateforme",
                        lambda sub, provider, unites: vu["debits"].append(
                            (sub, provider, unites)))

    def quota(org, fournisseur, sub):
        vu["quotas_lus"].append((org, fournisseur, sub))
        if isinstance(vu["atteint"], Exception):
            raise vu["atteint"]
        return vu["atteint"]
    monkeypatch.setattr(_cle_exigee, "quota_partage_atteint", quota)

    def interdit(*a, **k):
        raise AssertionError("un tour au coût ne touche pas le budget d'unités")
    monkeypatch.setattr(tours, "reserver_tour", interdit)
    monkeypatch.setattr(tours, "regler_tour", interdit)
    return vu


_USAGE = {"model": FLASH, "usage_input": 152, "usage_output": 81,
          "usage_cache_read": 6912, "usage_cost_usd": 6.44952e-05}


def test_un_tour_au_cout_ne_se_reserve_pas(tour_partage):
    assert runner_jobs._reserver_un_tour(_Ctx(), 7, 0, 16000) == {"ok": True, "held": False}


def test_un_tour_au_cout_est_journalise_a_son_cout(tour_partage):
    r = runner_jobs._regler_un_tour(_Ctx(), 7, 3, dict(_USAGE))
    assert r == {"ok": True, "metered": True, "microdollars": 65, "duplicate": False}
    (ligne,) = tour_partage["lignes"]
    assert ligne["kind"] == "settlement" and ligne["tool"] == "openrouter"
    assert ligne["quantity"] == 65 and ligne["key_mode"] == "platform"
    assert (ligne["org_id"], ligne["sub"], ligne["run_id"]) == (42, "u-1", "run-9")
    assert ligne["call_uid"] == tours.cle_du_tour(7, 2, 3)
    assert {k: ligne["args"].get(k) for k in ("input_tokens", "output_tokens",
                                               "cache_read_input_tokens", "turn")} == {
        "input_tokens": 152, "output_tokens": 81, "cache_read_input_tokens": 6912, "turn": 3}
    assert ligne["args"]["cost_usd"] == 6.44952e-05


def test_un_tour_rejoue_n_est_ecrit_qu_une_fois(tour_partage):
    tour_partage["existe"] = True
    r = runner_jobs._regler_un_tour(_Ctx(), 7, 3, dict(_USAGE))
    assert r["duplicate"] is True and tour_partage["lignes"] == []


# ── 3 bis. le quota de l'accord, en µ$ ──────────────────────────────────────

def test_une_cle_de_plateforme_openrouter_ne_s_accorde_pas_sans_quota():
    assert providers.connector_for_provider("openrouter").grant_quota_unit
    for sans in (None, 0):
        with pytest.raises(credentials_store.QuotaDOctroiRequis):
            credentials_store.exiger_quota_d_octroi("openrouter", sans)
        with pytest.raises(AuthzDenied) as e:
            _quota_d_octroi.exiger("openrouter", sans)
        assert (e.value.status, e.value.code) == (400, "grant_quota_required")
    credentials_store.exiger_quota_d_octroi("openrouter", 5_000_000)


def test_un_tour_sur_la_cle_de_plateforme_debite_son_cout_une_fois(tour_partage):
    runner_jobs._regler_un_tour(_Ctx(), 7, 3, dict(_USAGE))
    assert tour_partage["debits"] == [("u-1", "openrouter", 65)]
    tour_partage["existe"] = True
    assert runner_jobs._regler_un_tour(_Ctx(), 7, 3, dict(_USAGE))["duplicate"] is True
    assert tour_partage["debits"] == [("u-1", "openrouter", 65)], "un rejeu ne débite pas"


def test_la_cle_du_tenant_n_a_pas_de_quota_d_accord(tour_partage):
    tour_partage["mode"] = "tenant"
    tour_partage["atteint"] = {"used": 9, "limit": 1}
    assert runner_jobs._reserver_un_tour(_Ctx(), 7, 0, 16000) == {"ok": True, "held": False}
    runner_jobs._regler_un_tour(_Ctx(), 7, 0, dict(_USAGE))
    assert tour_partage["quotas_lus"] == [] and tour_partage["debits"] == []
    assert len(tour_partage["lignes"]) == 1


def test_le_plafond_atteint_refuse_le_tour(tour_partage):
    assert runner_jobs._reserver_un_tour(_Ctx(), 7, 0, 16000) == {"ok": True, "held": False}
    assert tour_partage["quotas_lus"] == [(42, "openrouter", "u-1")]
    tour_partage["atteint"] = {"used": 130, "limit": 100}
    with pytest.raises(AuthzDenied) as e:
        runner_jobs._reserver_un_tour(_Ctx(), 7, 1, 16000)
    assert (e.value.status, e.value.code) == (402, "platform_quota_exhausted")
    assert "130/100" in str(e.value)


def test_un_quota_illisible_ne_laisse_rien_partir(tour_partage):
    tour_partage["atteint"] = RuntimeError("base injoignable")
    with pytest.raises(AuthzDenied) as e:
        runner_jobs._reserver_un_tour(_Ctx(), 7, 0, 16000)
    assert e.value.status == 503


class _RC:
    def __init__(self, mode):
        self.mode, self.key, self.config, self.entity_id = mode, "or-partagee", {}, None


@pytest.fixture
def lancement(monkeypatch):
    """Au lancement d'un travail au coût : rien de déposé par l'org, la clé de plateforme
    accordée. Chaque banc dit où en est le quota."""
    from oto_mcp.capabilities import _modele
    vu = {"estampilles": [], "atteint": None}
    monkeypatch.setattr(runner_jobs, "_cle_de_modele", lambda org, depot: (None, None))
    monkeypatch.setattr(runner_jobs, "_depot_pose", lambda org, depot: False)
    monkeypatch.setattr(_modele, "region_bloquee", lambda org, model: False)
    monkeypatch.setattr(_cle_exigee, "cle_exigee", lambda org, f: False)
    monkeypatch.setattr(_cle_exigee, "cle_partagee",
                        lambda org, f, sub=None, strict=False: _RC("platform"))

    def quota(org, fournisseur, sub):
        if isinstance(vu["atteint"], Exception):
            raise vu["atteint"]
        return vu["atteint"]
    monkeypatch.setattr(_cle_exigee, "quota_partage_atteint", quota)
    monkeypatch.setattr(tours, "sous_le_plancher",
                        lambda *a: pytest.fail("pas de plancher d'unités au coût"))
    monkeypatch.setattr(runner_jobs.db, "estampiller_cle_partagee",
                        lambda job_id, sub, depot, mode:
                        vu["estampilles"].append((depot, mode)) or True)
    monkeypatch.setattr(runner_jobs.db, "arreter_definitivement",
                        lambda job_id, sub, raison: vu.update(arret=raison) or True)
    monkeypatch.setattr(runner_jobs.db, "rendre_a_la_file",
                        lambda job_id, sub, raison, delai_s=60: vu.update(rendu=raison) or True)
    return vu


def _lancer():
    return runner_jobs._avec_cle(
        {"id": 9, "org_id": 42, "sub": "u-1", "delegated_token": "otd_x",
         "payload": {"model_family": "openrouter", "model": FLASH}},
        "openrouter", "worker:banc", worker=True, metre=True)


def test_sous_le_plafond_le_travail_part_sur_la_cle_de_plateforme(lancement):
    rendu = _lancer()
    assert rendu["model_key"] == "or-partagee" and rendu["meter_usage"] is True
    assert lancement["estampilles"] == [("openrouter", "platform")]


def test_plafond_atteint_le_travail_ne_part_pas(lancement):
    lancement["atteint"] = {"used": 100, "limit": 100}
    rendu = _lancer()
    assert "model_key" not in rendu and "100/100" in rendu["delegation_refusee"]
    assert lancement["estampilles"] == [] and "rendu" not in lancement


def test_un_quota_illisible_au_lancement_rend_le_travail_a_la_file(lancement):
    lancement["atteint"] = RuntimeError("base injoignable")
    assert _lancer() is None
    assert lancement["estampilles"] == [] and "arret" not in lancement
    assert "quota" in lancement["rendu"]


def test_un_tour_sans_cout_declare_arrete_le_travail(tour_partage):
    with pytest.raises(AuthzDenied) as e:
        runner_jobs._regler_un_tour(_Ctx(), 7, 3, {**_USAGE, "usage_cost_usd": None})
    assert e.value.status == 422 and tour_partage["lignes"] == []


def test_l_hote_qui_a_servi_le_tour_part_au_journal(tour_partage):
    runner_jobs._regler_un_tour(_Ctx(), 7, 3, {**_USAGE, "usage_host": "StepFun"})
    (ligne,) = tour_partage["lignes"]
    assert ligne["args"]["host"] == "StepFun"


# ── 4. où tourne un modèle ───────────────────────────────────────────────────

def test_chaque_hote_epingle_dit_ou_il_tourne():
    """Un hôte ajouté à une liste sans sa région ferait servir un modèle sans dire où il
    tourne : `hebergement` lève, et ce banc le voit."""
    for m in runner_models.MODELES:
        h = runner_models.hebergement(m.id)
        assert h and h["region"] in ("us", "eu", "cn") and h["hosts"], m.id


def test_la_region_d_un_modele_est_la_pire_de_ses_hotes():
    assert runner_models.hebergement("claude-sonnet-5")["region"] == "us"
    assert runner_models.hebergement("mistral-large-2512")["region"] == "eu"
    flash = runner_models.hebergement(FLASH)
    assert flash["region"] == "cn" and flash["router"] == {"name": "OpenRouter", "region": "us"}
    assert [(h["name"], h["region"], h["role"]) for h in flash["hosts"]] == [
        ("StepFun", "cn", "first"), ("StreamLake", "cn", "backup"),
        ("DeepInfra", "us", "last_backup")]
    # Premier hôte aux US, un secours en Chine : la Chine, dite.
    minimax = runner_models.hebergement("minimax/minimax-m3")
    assert minimax["hosts"][0]["region"] == "us" and minimax["region"] == "cn"
    assert runner_models.hebergement("inconnu") is None


def test_un_hote_inconnu_leve(monkeypatch):
    m = runner_models._PAR_ID[FLASH]
    monkeypatch.setitem(runner_models._PAR_ID, FLASH, m._replace(providers=("nouveau/fp8",)))
    with pytest.raises(KeyError):
        runner_models.hebergement(FLASH)


# ── 5. l'org qui écarte les modèles qui tournent en Chine ────────────────────

@pytest.fixture
def chine(monkeypatch):
    from oto_mcp import db
    vu = {"permise": True}
    from oto_mcp.capabilities import _cle_exigee
    monkeypatch.setattr(db.regions_de_modeles, "chine_permise", lambda org: vu["permise"])
    monkeypatch.setattr(_cle_exigee, "manquantes", lambda *a, **k: [])
    return vu


def test_ecartee_la_chine_n_est_plus_proposee_ni_posable(chine):
    from oto_mcp.capabilities import _modele
    chine["permise"] = False
    etat = _modele.etat_servi({"families": ["openrouter", "anthropic"]}, 42)
    par_id = {m["id"]: m for m in etat["models"]}
    assert par_id[FLASH]["allowed"] is False and par_id[FLASH]["default"] is False
    assert "allowed" not in par_id["claude-sonnet-5"]  # rien d'écarté hors de Chine
    with pytest.raises(AuthzDenied) as e:
        _modele.exige_region_permise(42, FLASH)
    assert (e.value.status, e.value.code) == (403, "model_region_blocked")
    _modele.exige_region_permise(42, "claude-sonnet-5")
    _modele.exige_region_permise(42, "mistral-large-2512")


def test_permise_par_defaut_rien_ne_change(chine):
    from oto_mcp.capabilities import _modele
    etat = _modele.etat_servi({"families": ["openrouter"]}, 42)
    assert all(m.get("allowed", True) for m in etat["models"])
    _modele.exige_region_permise(42, FLASH)


def test_un_travail_deja_en_file_est_arrete_a_la_reservation(chine, monkeypatch):
    chine["permise"] = False
    arrete = {}
    monkeypatch.setattr(runner_jobs.db, "arreter_definitivement",
                        lambda jid, qui, raison: arrete.update(jid=jid, raison=raison))
    job = {"id": 5, "org_id": 42, "payload": {"model": FLASH, "model_family": "openrouter"}}
    out = runner_jobs._avec_cle(job, "openrouter", "worker:x", worker=True, metre=True)
    assert arrete["jid"] == 5 and "Chine" in arrete["raison"]
    assert out["delegation_refusee"] == arrete["raison"]
