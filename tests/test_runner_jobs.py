"""La file d'exécutions du runner — les gardes que le worker ne doit jamais contourner.

Ce que ces tests verrouillent : le scope org du claim (un worker ne voit que SA
file), les exigences par kind (un `continue` sans run est inexécutable, autant le
refuser à l'entrée), le refus-sans-oracle sur les verbes de prise (conclure le job
d'un autre = job inconnu), et la borne du bail. Le comportement SQL (backoff,
`failed` au plafond, SKIP LOCKED) est porté par `db/runner_jobs.py` et se vérifie
au déploiement — ici on stubbe, on teste la capacité.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture(autouse=True)
def _aucune_cle_partagee(monkeypatch):
    """Aucune clé partagée (plateforme ou tenant) n'est posée ni accordée ici : sans cette
    doublure, la garde d'argent irait la résoudre dans le vrai coffre. Le cas où elle
    l'est a son banc (`test_cle_partagee_anthropic.py`)."""
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.cle_partagee",
                        lambda org_id, fournisseur, sub=None, strict=False: None)
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.cle_de_plateforme_posee",
                        lambda fournisseur: False)
    # Le dépôt de l'org se lit ABSENT, sans aller au coffre : un coffre injoignable
    # se lit désormais « doute » et rend le travail à la file (`_doute_du_coffre`).
    monkeypatch.setattr("oto_mcp.capabilities.runner_jobs._depot_pose",
                        lambda org_id, depot: False)



@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)



def _ctx(sub="worker-campagne", org_id=226):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _appel(ctx, **kw):
    return RJ._jobs(ctx, RJ.JobsInput(**kw))


@pytest.fixture
def espion(monkeypatch):
    vu = {}
    # ⚠️ La doublure suit la SIGNATURE SERVIE : `fleet_id` est entré avec le
    # rattachement d'un travail à sa flotte (#791). Une doublure figée sur une
    # ancienne signature ne protège plus rien — elle éclate en `TypeError`, ce qui
    # est le bon comportement : c'est le contrat qui a bougé, pas le test.
    monkeypatch.setattr(RJ.db, "enqueue_job",
                        # ⚠️ `**_` : une doublure qui fige la signature de son
                        # original casse au premier champ ajouté — et l'échec
                        # accuse le test, pas le manque.
                        lambda org_id, kind, payload=None, run_id=None,
                        max_attempts=3, fleet_id=None, sub=None, **_:
                        vu.update(org=org_id, kind=kind, fleet=fleet_id,
                                  sub=sub, payload=payload) or
                        {"id": 7, "status": "pending", "due_at": "2026-08-13",
                         "fleet_id": fleet_id})
    monkeypatch.setattr(RJ.db, "claim_next_job",
                        lambda org_id, sub, lease_seconds=600, depot=None, **_:
                        vu.update(claim=(org_id, sub, lease_seconds)) or
                        vu.setdefault("depots", []).append(depot))
    monkeypatch.setattr(RJ.db, "complete_job",
                        lambda job_id, sub, ok, error=None, run_id=None, result=None:
                        vu.update(result=result) or
                        ({"status": "done"} if sub == "worker-campagne" else None))
    monkeypatch.setattr(RJ.db, "bind_job_run", lambda j, s, r: s == "worker-campagne")
    # Un `continue` relit le modèle de son run : par défaut, un run démarré sans.
    monkeypatch.setattr(RJ.db, "modele_du_run", lambda run_id, org_id: {})
    monkeypatch.setattr(RJ.db, "extend_job_lease", lambda j, s, lease_seconds=600: False)
    monkeypatch.setattr(RJ.db, "get_job", lambda j, org: None)
    # ⚠️ Doublure OBLIGATOIRE : sans elle, l'arrêt des campagnes épuisées tape la
    # vraie base, lève, et le fail-open de la production avale l'échec — les
    # bancs de campagne passeraient alors sans rien exercer. Vérifié le
    # 07/09/2026 : lancés seuls ils tombaient, en groupe ils passaient.
    monkeypatch.setattr(RJ.db, "une_campagne_vit", lambda org_id: True)
    monkeypatch.setattr(RJ.db, "arreter_campagnes_epuisees", lambda org_id: [])
    # ⚠️ MÊME raison, MÊME piège, repayé le 10/09/2026 : `accuser_arrets_effectifs`
    # est entré dans le même chemin (#« un arrêt demandé redevient un arrêt
    # constaté ») sans doublure ici. Joués seuls, les sept bancs de campagne
    # tombaient en `KeyError: 'sub'` — un message qui accuse l'espion ; joués avec
    # leur fichier ils passaient, parce que la fixture `live` d'un test VOISIN
    # laisse `DATABASE_URL` posée pour tout le module. Un banc vert par voisinage
    # ne garde rien : ici il garde SOUS QUELLE IDENTITÉ une campagne agit.
    # Toute lecture de base ajoutée à `_produire_pour_une_campagne` doit être
    # doublée ici — sinon le fail-open de la production avale l'échec.
    monkeypatch.setattr(RJ.db, "accuser_arrets_effectifs", lambda org_id: [])
    # Et la troisième du même chemin (22/10/2026), doublée pour la même raison.
    monkeypatch.setattr(RJ.db, "clore_campagnes_terminees", lambda org_id: [])
    return vu


# ── le scope, sans lequel tout le reste est faux ──────────────────────────────

def test_le_claim_porte_lorg_et_le_sub_de_lappelant(espion):
    _appel(_ctx(), op="claim")
    org, sub, _ = espion["claim"]
    assert (org, sub) == (226, "worker-campagne"), \
        "le claim ne peut servir QUE la file de l'org du jeton, au nom du worker"


def test_sans_org_la_file_refuse_et_dit_comment_la_nommer():
    """⚠️ Renversé DEUX FOIS, et la seconde annule la première. J'avais ouvert
    `claim` aux workers sans organisation, armé par une marque posée sur un
    compte. Alexis l'a refusé — « je ne veux pas que les workers aient des
    droits de ce genre, ça doit être applicatif » — et il avait raison : la voie
    applicative existait déjà. Un worker NOMME l'organisation pour laquelle il
    sonde, et son appartenance est vérifiée à chaque requête. Ce qui borne est
    l'appartenance, pas un privilège, et elle se révoque sans toucher au code.

    Le refus dit COMMENT nommer, sinon il fait relire le même appel."""
    with pytest.raises(AuthzDenied) as e:
        _appel(ResolvedCtx(sub="w", org_id=None), op="claim")
    assert e.value.code == "org_required"
    assert "_org" in e.value.message or "X-Oto-Org" in e.value.message


def test_le_bail_est_borne(espion):
    _appel(_ctx(), op="claim", lease_seconds=999_999)
    assert espion["claim"][2] == 3600, "un bail d'une journée n'est pas un heartbeat"
    _appel(_ctx(), op="claim", lease_seconds=1)
    assert espion["claim"][2] == 30, "un bail d'une seconde est un claim jetable"


# ── les exigences par kind, refusées à l'entrée ───────────────────────────────

def test_un_continue_sans_run_est_refuse(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="enqueue", kind="continue")
    assert e.value.code == "missing_fields" and "run_id" in str(e.value.message)


def test_un_start_sans_payload_est_refuse(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="enqueue", kind="start")
    assert e.value.code == "missing_fields"


def test_enfiler_un_continue_sur_le_run_dautrui_rend_run_inconnu(espion, monkeypatch):
    """Le gate propriétaire tient CÔTÉ SERVEUR, pas dans le séquencement de l'UI :
    un enqueue direct (sans append préalable) sur le run d'un autre ferait
    continuer son fil par le worker, avec les droits du run. Même 404 sans
    oracle que l'append du fil (R1)."""
    monkeypatch.setattr(RJ.db, "get_run_head",
                        lambda run_id: {"sub": "proprietaire", "org_id": 226}
                        if run_id == "run-X" else None)
    monkeypatch.setattr(RJ.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne s'enfile sans propriété"))
    with pytest.raises(AuthzDenied) as a:
        _appel(_ctx(sub="intrus"), op="enqueue", kind="continue", run_id="run-X")
    with pytest.raises(AuthzDenied) as b:
        _appel(_ctx(sub="intrus"), op="enqueue", kind="continue", run_id="run-INEXISTANT")
    assert (a.value.status, a.value.code) == (b.value.status, b.value.code) == \
        (404, "run_not_found")


def test_le_proprietaire_enfile_son_continue(espion, monkeypatch):
    monkeypatch.setattr(RJ.db, "get_run_head",
                        lambda run_id: {"sub": "worker-campagne", "org_id": 226})
    out = _appel(_ctx(), op="enqueue", kind="continue", run_id="run-X")
    assert out["id"] == 7


def test_enqueue_scope_lorg_de_lappel(espion):
    out = _appel(_ctx(org_id=42), op="enqueue", kind="start",
                 payload={"procedure": "veille-linkedin"})
    assert espion["org"] == 42 and out["id"] == 7


def test_enqueue_refuse_un_trigger_id_fourni(espion):
    """Le `trigger_id` d'une charge dit de quel agent un travail est un run : le serveur
    seul le pose. Fourni à `enqueue`, il rangerait un travail quelconque — et le passage
    que ce travail déclarerait (`oto_fleet op=create`) — sous l'agent d'un autre."""
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="enqueue", kind="start",
               payload={"procedure": "veille", "trigger_id": 42})
    assert (e.value.status, e.value.code) == (400, "trigger_id_server_set")
    assert "payload" not in espion, "rien n'est enfilé"


# ── conclure ce qui ne nous appartient pas = job inconnu (pas d'oracle) ───────

def test_conclure_le_job_dun_autre_rend_job_inconnu(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(sub="autre-worker"), op="complete", job_id=7, ok=True)
    assert (e.value.status, e.value.code) == (404, "job_not_found")


def test_le_claimant_conclut(espion):
    out = _appel(_ctx(), op="complete", job_id=7, ok=True)
    # Sans run connu, la libération des baux n'est pas tentée et la réponse le DIT
    # (#633) : null + raison, jamais un 0 fabriqué.
    assert out == {"ok": True, "status": "done",
                   "run_id": None, "rows_released": None, "release": "no_run"}


def test_prolonger_un_bail_perdu_rend_job_inconnu(espion):
    # Le bail a expiré, un autre worker a re-claimé : extend rend rowcount 0.
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="extend", job_id=7)
    assert e.value.status == 404, \
        "un worker dont le bail est mort ne garde aucune prise sur le job"


# ── le résultat déclaré (R5, garde budget de flotte) ─────────────────────────

def test_complete_transporte_le_resultat_declare(espion):
    _appel(_ctx(), op="complete", job_id=7, ok=True,
           result={"usage_tokens": 31500, "stopped": "end_turn", "steps": 18})
    assert espion["result"] == {"usage_tokens": 31500, "stopped": "end_turn",
                                "steps": 18}, \
        "le coût d'un job doit être LISIBLE par l'ordonnanceur de flotte"


def test_un_resultat_obese_est_refuse(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="complete", job_id=7, ok=True,
               result={"note": "x" * 5000})
    assert e.value.code == "result_too_large", \
        "result est un résumé, jamais un contenu de fil"


def test_la_liste_est_scopee_a_lorg_et_filtrable(live):
    """La surveillance (page Automatisations) : la file de MON org seulement,
    du plus récent au plus ancien, filtrable par statut."""
    from oto_mcp import db as d

    a = d.enqueue_job(310, "start", payload={"procedure": "p1"})
    d.enqueue_job(311, "start", payload={"procedure": "autrui"})
    job = d.claim_next_job(310, "w-list", lease_seconds=60)
    d.complete_job(job["id"], "w-list", False, error="boom")   # pending, attempt 1

    jobs = d.list_jobs(310)
    assert all("autrui" not in str(j.get("payload")) for j in jobs), \
        "la file d'une autre org ne doit JAMAIS apparaître"
    assert any(j["id"] == a["id"] for j in jobs)
    en_attente = d.list_jobs(310, status="pending")
    assert {j["status"] for j in en_attente} == {"pending"}


def test_le_resultat_fait_l_aller_retour_en_base(live):
    """Le round-trip RÉEL : complete écrit `result`, get le rend — c'est ce que
    l'ordonnanceur de flotte lira pour sa garde budget. Un stub ne prouve ni la
    colonne, ni le COALESCE, ni le SELECT."""
    from oto_mcp import db as d

    j = d.enqueue_job(226, "start", payload={"procedure": "p"})
    job = d.claim_next_job(226, "worker-live", lease_seconds=60)
    assert job and job["id"] == j["id"]
    out = d.complete_job(job["id"], "worker-live", True,
                         result={"usage_tokens": 12345, "stopped": "end_turn"})
    assert {k: out[k] for k in ("status", "run_id")} == {"status": "done", "run_id": None}, \
        "complete rend le run connu du job (#633) — aucun ici"
    relu = d.get_job(job["id"], 226)
    assert relu["result"] == {"usage_tokens": 12345, "stopped": "end_turn"}
    assert relu["status"] == "done"


# ── La campagne produit son travail au SONDAGE, sans ordonnanceur ────────────
# Un worker ne connaît pas la notion de campagne : il demande du travail. Quand
# la file est vide, c'est ICI qu'on décide s'il y en a un à fabriquer. Ce qui
# remplace un ordonnanceur externe qu'un humain lançait à la main, qui prenait
# la campagne, la découpait, et battait pour dire qu'il vivait.

CAMPAGNE = {"id": 12, "org_id": 7, "sub": "celui-qui-a-declare", "label": "passage-editeurs",
            "procedure": "enrichissement", "project_id": 220,
            # #1067 : une campagne garde l'IDENTIFIANT de son tableau, jamais son nom —
            # un nom déclencherait sa reprise (lecture de base) dans ce banc sans base.
            "namespace": "77",
            "tools": ["data_claim_next", "data_write"], "input": "file {namespace}",
            "row_filter": {"statut": "a_enrichir"}, "max_steps": 40,
            "max_tokens_per_row": 80000}


@pytest.fixture
def campagne(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id, _ordonner: espion.update(cherchee=org_id) or CAMPAGNE)
    monkeypatch.setattr(RJ.db, "marquer_demarree",
                        lambda fid: espion.update(demarree=fid))
    return espion


def test_une_file_vide_fait_produire_le_travail_de_la_campagne(campagne):
    _appel(_ctx(), op="claim")
    assert campagne["cherchee"] == 226, "la campagne se cherche dans l'org du worker"
    assert campagne["fleet"] == 12, "le travail doit être rattaché à sa campagne"
    assert campagne["kind"] == "start"


def test_la_consigne_commandee_ne_porte_AUCUN_marqueur_en_litteral(campagne, monkeypatch):
    """La couture, pas le module : le travail enfilé pour une campagne porte la
    consigne COMPOSÉE. Le 12/09/2026, `{date_du_jour}` partait tel quel."""
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id, _ordonner: dict(
        CAMPAGNE, input="file {namespace} filtre {filter} du {date_du_jour}"))
    _appel(_ctx(), op="claim")
    servi = campagne["payload"]["input"]
    assert "{" not in servi.replace('{"statut"', ""), f"marqueur resté en littéral : {servi!r}"
    assert servi.startswith("file 77 filtre ")


def test_le_travail_porte_l_identite_du_DECLARANT_pas_du_worker(campagne):
    """C'est la garde qui compte. Le worker n'est pas un pouvoir : il portera un
    jeton émis au nom de quelqu'un d'autre. Prendre son propre `sub` ici ferait
    agir la campagne sous l'identité de l'infrastructure.

    ⚠️ Ce banc pose LUI-MÊME tout ce qu'il vérifie (cf. la doublure
    `accuser_arrets_effectifs` de l'espion) et regarde `campaign_error` : la
    production de travail est fail-open, elle avale toute exception et la rend
    dans ce champ. Sans ce regard, une lecture de base non doublée fait échouer
    la production en silence, et le banc tombe sur un `KeyError` qui accuse
    l'espion au lieu de nommer la cause — ou pire, passe par voisinage."""
    rendu = _appel(_ctx(sub="worker-campagne"), op="claim")
    assert "campaign_error" not in rendu, rendu.get("campaign_error")
    assert campagne["sub"] == "celui-qui-a-declare"
    assert campagne["sub"] != "worker-campagne"


def test_le_claim_passe_au_filtre_le_depot_que_nomme_le_worker(espion):
    """Le dépôt est aussi la FAMILLE que le worker sert : c'est lui qui filtre la
    file (`claim_next_job`). Oublié ici, un worker Anthropic recevrait un travail
    Mistral comme une commande valide.

    ⚠️ CHAQUE sondage de l'appel : sur file vide, le claim est rejoué après la
    production d'une campagne. N'en regarder qu'un laisserait l'autre partir sans
    filtre — c'est la mutation que ce banc laissait passer."""
    _appel(_ctx(), op="claim", provider="mistral")
    assert len(espion["depots"]) == 2, "file vide : sondage, production, re-sondage"
    assert set(espion["depots"]) == {"mistral"}


def test_le_travail_d_une_campagne_emporte_son_modele_et_sa_famille(monkeypatch, campagne):
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id, _ordonner: {**CAMPAGNE, "model": "mistral-large-2512"})
    rendu = _appel(_ctx(), op="claim")
    assert "campaign_error" not in rendu, rendu.get("campaign_error")
    assert campagne["payload"]["model"] == "mistral-large-2512"
    assert campagne["payload"]["model_family"] == "mistral"


def test_une_campagne_SANS_modele_produit_le_travail_d_avant(campagne):
    _appel(_ctx(), op="claim")
    assert "model" not in campagne["payload"]
    assert "model_family" not in campagne["payload"]


def test_ce_que_l_agent_lit_des_outils_part_avec_le_travail(monkeypatch, campagne):
    """oto#241 : le réglage déclaré sur la campagne voyage ; sans lui, rien ne part."""
    reglage = {"defaut": 1024, "entieres": ["data_write", "data_claim_next"]}
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id, _ordonner: {**CAMPAGNE, "descriptions_outils": reglage})
    _appel(_ctx(), op="claim")
    assert campagne["payload"]["descriptions_outils"] == reglage


def test_sans_reglage_de_descriptions_le_travail_n_en_porte_aucun(campagne):
    _appel(_ctx(), op="claim")
    assert "descriptions_outils" not in campagne["payload"]


def test_la_borne_PAR_LIGNE_part_avec_le_travail(campagne):
    """⚠️ C'est ICI que la borne par ligne devient réelle, et nulle part ailleurs.

    Le serveur ne refuse plus rien à la déclaration (arbitrage du 09/09/2026 :
    « une borne doit pouvoir être posée, si pas de borne, tant pis »). Une borne
    posée n'est donc plus qu'une valeur en base — sauf si elle VOYAGE : elle part
    dans `payload["max_tokens"]`, et c'est l'agent qui s'arrête dessus
    (`stopped=max_tokens`). Sans ce transport, `max_tokens_per_row` serait un
    champ décoratif que le dashboard affiche et que rien n'applique.

    Le nom change au passage — `max_tokens_per_row` côté campagne, `max_tokens`
    côté travail — et un renommage silencieux est exactement ce qui se casse sans
    qu'un test le voie."""
    _appel(_ctx(), op="claim")
    assert campagne["payload"]["max_tokens"] == 80000, (
        "la borne déclarée par la campagne doit atteindre le travail — sinon "
        "elle ne borne rien")


def test_sans_borne_le_travail_ne_porte_AUCUN_plafond_de_jetons(monkeypatch, espion):
    """Le pendant, et il est assumé : sans borne, `max_tokens` vaut `None` et
    l'agent n'a pas de plafond de jetons — il s'arrêtera sur sa fenêtre de
    contexte, ou sur `max_steps`. Poser un défaut ici fabriquerait une borne que
    personne n'a déclarée."""
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id, _ordonner: {**CAMPAGNE, "max_tokens_per_row": None})
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    _appel(_ctx(), op="claim")
    assert espion["payload"]["max_tokens"] is None


def test_la_campagne_passe_a_running_au_premier_travail(campagne):
    _appel(_ctx(), op="claim")
    assert campagne["demarree"] == 12


def test_sans_campagne_le_sondage_rend_simplement_rien(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id, _ordonner: None)
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    assert _appel(_ctx(), op="claim") == {"job": None}
    assert "fleet" not in espion, "aucun travail ne doit être fabriqué"


def test_une_campagne_illisible_ne_casse_PAS_le_sondage(monkeypatch, espion):
    """Fail-open : le sondage des workers est le chemin le plus fréquent de toute
    la plateforme. Une campagne mal formée ne doit pas l'arrêter pour l'org —
    le passage attendra le sondage suivant."""
    def _explose(org_id, _ordonner):
        raise RuntimeError("colonne manquante")
    monkeypatch.setattr(RJ.db, "campagne_a_servir", _explose)

    # Ne CASSE pas — mais ne se tait pas non plus. Ce test affirmait
    # `== {"job": None}`, c'est-à-dire exactement le silence qui a laissé le
    # sondage répondre « rien à faire » pendant des jours alors qu'il n'avait
    # jamais réussi à regarder (07/09/2026). Le worker reçoit la cause.
    rendu = _appel(_ctx(), op="claim")

    assert rendu["job"] is None, "une campagne illisible ne casse pas le sondage"
    assert "RuntimeError" in rendu["campaign_error"], (
        "et elle se DIT au worker : « rien à faire » et « je n'ai pas pu "
        "regarder » ne sont pas la même réponse")


def test_file_vide_ne_porte_aucune_panne(monkeypatch, espion):
    """Le pendant, sans lequel le champ ne prouve rien : une file réellement
    vide ne doit porter AUCUN signalement. Un champ toujours présent redevient
    du bruit, et on aurait juste déplacé le silence."""
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id, _ordonner: None)
    assert _appel(_ctx(), op="claim") == {"job": None}


def test_une_campagne_cassee_ne_journalise_QU_UNE_fois(monkeypatch, espion, caplog):
    """Le sondage tourne en boucle sur chaque worker. Journaliser à chaque tour
    noierait le journal sous des milliers de lignes identiques — et un journal
    noyé ne se lit pas, ce qui revient à ne rien dire. On veut le contraire :
    une ligne qui se voit."""
    RJ._CAMPAGNE_MUETTE.clear()
    def _explose(org_id, _ordonner):
        raise RuntimeError("colonne manquante")
    monkeypatch.setattr(RJ.db, "campagne_a_servir", _explose)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            _appel(_ctx(), op="claim")
    lignes = [r for r in caplog.records if "production de travail impossible" in r.message]
    assert len(lignes) == 1, f"5 sondages ont produit {len(lignes)} lignes de journal"


def test_une_cause_DIFFERENTE_se_dit(monkeypatch, espion, caplog):
    """Ne pas répéter n'est pas se taire : une panne qui change de nature est une
    information neuve, et l'étouffer ferait manquer la seconde."""
    RJ._CAMPAGNE_MUETTE.clear()
    causes = iter(["colonne manquante", "colonne manquante", "table absente"])
    def _explose(org_id, _ordonner):
        raise RuntimeError(next(causes))
    monkeypatch.setattr(RJ.db, "campagne_a_servir", _explose)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            _appel(_ctx(), op="claim")
    lignes = [r for r in caplog.records if "production de travail impossible" in r.message]
    assert len(lignes) == 2, "deux causes distinctes, deux lignes"


def test_les_campagnes_epuisees_sont_arretees_AVANT_d_en_servir_une(monkeypatch, espion, caplog):
    """L'ordonnanceur qui portait cette borne s'arrêtait quand personne ne le
    lançait. Le sondage, lui, ne s'arrête jamais : une campagne qui échoue en
    boucle régénérerait du travail toute la nuit. Et il faut l'ARRÊTER, pas
    seulement la sauter — sinon elle reste `running` sans avancer."""
    ordre = []
    monkeypatch.setattr(RJ.db, "arreter_campagnes_epuisees",
                        lambda org_id: ordre.append("arret") or [77])
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id, _ordonner: ordre.append("service") or None)
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    with caplog.at_level("WARNING"):
        _appel(_ctx(), op="claim")
    assert ordre == ["arret", "service"], "arrêter d'abord, servir ensuite"
    assert any("campagne 77 arrêtée" in r.message for r in caplog.records), \
        "un arrêt automatique qui ne se dit pas est un silence de plus"



# ── La procédure se LIT par MCP : aucune copie injectée (13/09/2026) ─────────
# L'instruction dit « lis la procédure X » et l'agent la lit par `oto_procedure`,
# avec les droits de son porteur — comme un agent qui travaille avec le connecteur
# branché. La plateforme servait EN PLUS le texte dans le cadre (`system`, de la
# v1.244.0 au 13/09/2026) : une seconde copie, lue depuis un magasin et une portée
# qui n'étaient pas forcément celles que l'agent relisait. Retirée. Ces bancs
# prouvent l'absence de copie ET la présence de l'outil de lecture — sans lui, le
# worker (fail-closed sur `payload.tools`) laisserait l'agent sans consigne.

@pytest.fixture
def magasins_interdits(monkeypatch):
    """Toute lecture de procédure ou de guide au claim fait tomber le banc."""
    for cible in ("oto_mcp.org_store.get_instruction", "oto_mcp.db.get_guide_db"):
        monkeypatch.setattr(cible, lambda *a, **k: pytest.fail(
            "aucune lecture de magasin au claim : l'agent lit la procédure par MCP"))


@pytest.fixture
def reserve(monkeypatch, espion, magasins_interdits):
    """Pose le travail que la base rend au claim ; capte ce que reçoit la délégation."""
    vu = {}

    def _poser(payload):
        stocke = {"id": 7, "org_id": 226, "sub": "demandeur", "payload": payload}
        monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: stocke)
        return stocke

    def _delegue(job, bail, claimant):
        vu["delegue"] = job
        return {**job, "delegated_token": "jeton"}
    monkeypatch.setattr(RJ, "_delegue", _delegue)
    vu["poser"] = _poser
    return vu


def test_un_travail_qui_declare_une_procedure_n_en_recoit_AUCUNE_copie(reserve):
    consigne = "Lis la procédure `passe-registre` et applique-la."
    reserve["poser"]({"procedure": "passe-registre", "tools": ["data_write"],
                      "input": consigne})

    job = _appel(_ctx(), op="claim")["job"]

    assert "system" not in job, "la procédure se lit par MCP, jamais injectée"
    assert (job["payload"]["procedure"], job["payload"]["input"]) == (
        "passe-registre", consigne), "la référence et l'instruction restent intactes"


def test_l_outil_de_lecture_est_SERVI_quand_la_liste_ne_le_cite_pas(reserve):
    stocke = reserve["poser"]({"procedure": "passe-registre",
                               "tools": ["data_claim_next", "data_write"]})

    job = _appel(_ctx(), op="claim")["job"]

    assert job["payload"]["tools"] == ["data_claim_next", "data_write", "oto_procedure"]
    assert reserve["delegue"]["payload"]["tools"] == job["payload"]["tools"], (
        "complété AVANT la délégation, pas après")
    assert stocke["payload"]["tools"] == ["data_claim_next", "data_write"], (
        "la liste stockée — écrite ou déduite par l'auteur — ne bouge pas")


def test_une_procedure_sans_liste_d_outils_recoit_l_outil_de_lecture(reserve):
    reserve["poser"]({"procedure": "passe-registre"})
    assert _appel(_ctx(), op="claim")["job"]["payload"]["tools"] == ["oto_procedure"]


def test_l_outil_de_lecture_deja_cite_n_est_pas_DOUBLE(reserve):
    reserve["poser"]({"procedure": "passe-registre",
                      "tools": ["oto_procedure", "data_write"]})
    assert _appel(_ctx(), op="claim")["job"]["payload"]["tools"] == [
        "oto_procedure", "data_write"]


def test_un_travail_SANS_procedure_ne_recoit_que_l_org_du_travail(reserve):
    stocke = reserve["poser"]({"tools": ["data_write"], "input": "fais ceci"})

    job = _appel(_ctx(), op="claim")["job"]

    assert job == {**stocke, "payload": {**stocke["payload"], "org_id": 226},
                   "delegated_token": "jeton"}
    assert stocke["payload"] == {"tools": ["data_write"], "input": "fais ceci"}


# ── L'org DU TRAVAIL est servie, le worker l'impose en `_org` (13/09/2026) ───
# Sans elle, chaque appel de l'agent se résout dans l'org ACTIVE de son porteur, et
# `run_start` y ouvre le run : l'agent d'un déclencheur de l'org B lisait — et
# écrivait — dans l'org A d'un porteur de deux orgs. Preuve de bout en bout, sur
# vraie base et par le middleware : `test_procedure_lue_par_mcp.py`.

def test_l_org_du_travail_est_SERVIE_quand_la_charge_n_en_porte_pas(reserve):
    stocke = reserve["poser"]({"procedure": "veille", "tools": ["data_write"],
                               "trigger_id": 9})

    job = _appel(_ctx(), op="claim")["job"]

    assert job["payload"]["org_id"] == 226
    assert reserve["delegue"]["payload"]["org_id"] == 226, "servie AVANT la délégation"
    assert "org_id" not in stocke["payload"], "le travail stocké ne change pas"


def test_une_org_CONTRADICTOIRE_de_la_charge_est_remplacee_par_celle_du_travail(
        reserve, caplog):
    stocke = reserve["poser"]({"input": "fais ceci", "org_id": 999})

    with caplog.at_level("WARNING", logger=RJ.logger.name):
        job = _appel(_ctx(), op="claim")["job"]

    assert job["payload"]["org_id"] == 226
    assert stocke["payload"]["org_id"] == 999, "le travail stocké ne change pas"
    assert [r for r in caplog.records
            if r.name == RJ.logger.name and "999" in r.getMessage()], (
        "remplacée, et DITE — jamais gardée ni corrigée en silence")


def test_une_charge_de_CAMPAGNE_est_servie_inchangee(reserve):
    """Une campagne pose déjà l'org du travail ; si sa liste cite l'outil de lecture,
    rien ne change à l'octet près."""
    stocke = reserve["poser"]({"procedure": "passe", "org_id": 226, "namespace": "file",
                               "tools": ["data_claim_next", "oto_procedure"],
                               "input": "consigne de file"})
    assert _appel(_ctx(), op="claim")["job"]["payload"] == stocke["payload"]


# ── Le worker de PLATEFORME : aucune org, et ce n'est pas un manque ──────────
# Ce que ces bancs ferment : un worker qui devait « nommer son org » sondait en
# fait l'org ACTIVE du compte dont il portait le jeton — un compte personnel,
# admin de quatorze organisations. Un worker n'a plus de compte : il
# est un secret de machine, et `org_id=None` est le fait que la règle pose.

def _worker():
    return ResolvedCtx(sub="worker:ab12cd34", org_id=None,
                       role="platform_worker", platform_worker=True)


def test_un_worker_de_plateforme_sonde_SANS_org_et_le_backend_choisit(espion, monkeypatch):
    monkeypatch.setattr(RJ, "_produire_pour_une_campagne", lambda org_id, bail: None)
    out = _appel(_worker(), op="claim")
    assert out == {"job": None}
    assert espion["claim"][0] is None, (
        "org None : c'est le backend qui choisit parmi TOUTES les orgs, pas le worker")
    assert espion["claim"][1] == "worker:ab12cd34"


def test_un_worker_ne_fait_que_les_verbes_du_bail(espion):
    for op, kw in (("enqueue", {"kind": "start"}), ("list", {}), ("get", {"job_id": 1})):
        with pytest.raises(AuthzDenied) as e:
            _appel(_worker(), op=op, **kw)
        assert e.value.code == "worker_verbs_only", op
        assert "geste d'organisation" in e.value.message


def test_un_membre_sans_org_est_TOUJOURS_refuse(espion):
    """La voie du worker n'ouvre rien au membre : sans org, un membre reste au
    refus d'avant, avec le même mot pour s'en sortir."""
    with pytest.raises(AuthzDenied) as e:
        _appel(ResolvedCtx(sub="u", org_id=None), op="claim")
    assert e.value.code == "org_required"


def test_les_verbes_du_bail_d_un_worker_passent_par_son_sub(espion, monkeypatch):
    """`bind_run`/`extend`/`complete` filtrent par `claimed_by` : le worker
    conclut ce qu'il a réservé, sans org — c'est tout ce que ces verbes exigent."""
    monkeypatch.setattr(RJ.db, "bind_job_run", lambda j, s, r: s == "worker:ab12cd34")
    assert _appel(_worker(), op="bind_run", job_id=3, run_id="r1") == {"ok": True}


# ── summary : la page Agents en un appel ─────────────────────────────────────

def test_summary_rend_heures_total_et_derniere_panne_des_agents_vus(monkeypatch):
    """Par agent visible : ses heures non vides (triées), son total, sa dernière
    panne. Un agent que l'appelant ne voit pas n'apparaît pas — pas même par son
    nombre de travaux."""
    # En TEXTE, comme `_str_dict_row` les rend : des `datetime` ici masquaient le 500.
    h = lambda d, hh: f"2026-10-{d:02d} {hh:02d}:00:00"  # noqa: E731
    vu = {}
    monkeypatch.setattr(RJ.db, "resume_des_declencheurs", lambda org, jours: vu.update(org=org, jours=jours) or (
        [{"trigger_id": 5, "heure": h(2, 8), "status": "done", "n": 3},
         {"trigger_id": 5, "heure": h(1, 8), "status": "failed", "n": 1},
         {"trigger_id": 9, "heure": h(1, 8), "status": "done", "n": 7}],
        [{"trigger_id": 5, "due_at": h(1, 8), "last_error": "refusé"},
         {"trigger_id": 9, "due_at": h(1, 8), "last_error": "secret"}],
    ))
    monkeypatch.setattr(RJ.db, "list_triggers", lambda org: [{"id": 5, "sub": "u"}, {"id": 9, "sub": "autre"}])
    monkeypatch.setattr(RJ._acces_agent, "niveaux", lambda sub, org, agents: {5: "owner", 9: None})
    monkeypatch.setattr(RJ.db, "minutes_gagnees_de_l_org", lambda org: {})
    monkeypatch.setattr(RJ.db, "formes_des_runs_conclus",
                        lambda org, jours, agents, classer: vu.update(agents=agents) or ([], []))
    rep = _appel(_ctx(sub="u"), op="summary", days=7)
    # Le temps gagné ne se compte que pour les agents VUS : l'invisible n'est pas lu.
    assert vu.pop("agents") == [5]
    assert vu == {"org": 226, "jours": 7}
    assert [a["trigger_id"] for a in rep["summary"]] == [5]
    a = rep["summary"][0]
    assert a["total"] == 4
    assert [x["status"] for x in a["hours"]] == ["failed", "done"]   # triées par heure
    assert a["last_failure"]["error"] == "refusé"
    assert a["hours"][0]["hour"] == "2026-10-01 08:00:00"
    assert a["last_failure"]["due_at"] == "2026-10-01 08:00:00"
    assert rep["since"].endswith("Z")


def test_summary_borne_la_fenetre():
    with pytest.raises(Exception):
        RJ.JobsInput(op="summary", days=40)


def test_summary_est_un_geste_de_membre_pas_de_worker():
    ctx = ResolvedCtx(sub="w", org_id=None, platform_worker=True)
    with pytest.raises(AuthzDenied) as e:
        _appel(ctx, op="summary")
    assert e.value.code == "worker_verbs_only"


def _effets_servis():
    """Les effets que le registre boot calcule, depuis les outils réellement servis."""
    import asyncio
    from oto_mcp import tool_registry
    from _mcp_app import static_mcp
    outils = asyncio.run(static_mcp().list_tools(run_middleware=False))
    return {t.name: tool_registry._effet(t) for t in outils}


def _un_agent_vu(monkeypatch, conclus, appels, surcharges=None):
    from oto_mcp import tool_registry
    monkeypatch.setattr(tool_registry, "_EFFETS", _effets_servis())
    h = "2026-10-01 08:00:00"
    monkeypatch.setattr(RJ.db, "resume_des_declencheurs", lambda org, jours: (
        [{"trigger_id": 5, "heure": h, "status": "done", "n": 2},
         {"trigger_id": 9, "heure": h, "status": "done", "n": 1}], []))
    monkeypatch.setattr(RJ.db, "list_triggers", lambda org: [{"id": 5, "sub": "u"}, {"id": 9, "sub": "autre"}])
    monkeypatch.setattr(RJ._acces_agent, "niveaux", lambda sub, org, agents: {5: "owner", 9: None})
    monkeypatch.setattr(RJ.db, "minutes_gagnees_de_l_org", lambda org: surcharges or {})
    monkeypatch.setattr(RJ.db, "formes_des_runs_conclus", _formes_en_memoire(conclus, appels))
    return _appel(_ctx(sub="u"), op="summary", days=7)


def _formes_en_memoire(conclus, appels):
    """`db.formes_des_runs_conclus` rejoué en mémoire depuis des lignes de travaux et
    d'appels, par la référence (`time_saved.formes_des_runs`) — la parité de la
    requête avec elle se prouve contre une vraie base (`test_temps_gagne_borne_live`)."""
    from oto_mcp import time_saved

    def _formes(org, jours, agents, classer):
        par_run: dict = {}
        for c in appels:
            par_run.setdefault(c["run_id"], []).append(time_saved.Call(
                tool=c["tool"], op=c["op"], ok=bool(c["ok"]), key=c["cle"], day=c["jour"]))
        runs: dict = {}
        sans: dict = {}
        for r in conclus:
            tid = int(r["trigger_id"])
            if tid not in agents:
                continue
            a = par_run.get(r.get("run_id") or "")
            if a:
                runs.setdefault(tid, []).append(a)
            else:
                sans[tid] = sans.get(tid, 0) + 1
        formes = [{"trigger_id": tid, "actions": f.actions, "records": f.records,
                   "research": f.research, "runs": f.runs}
                  for tid, rs in runs.items() for f in time_saved.formes_des_runs(rs)]
        return formes, [{"trigger_id": t, "runs": n} for t, n in sans.items()]
    return _formes


def _appel_journal(run_id, tool, op=None, ok=True, cle=None):
    return {"run_id": run_id, "tool": tool, "op": op, "ok": ok, "cle": cle, "jour": "2026-10-01"}


def test_summary_rend_le_temps_gagne_de_chaque_agent_vu(monkeypatch):
    """Les runs `done` d'un agent vu, leurs appels du journal rangés par type aux
    minutes de l'org ; un agent invisible n'apporte rien, pas même son temps gagné.
    `trigger_id` arrive en texte, tel que le payload le porte."""
    from oto_mcp import time_saved
    rep = _un_agent_vu(monkeypatch, [
        {"trigger_id": "5", "run_id": "r1"},
        {"trigger_id": "9", "run_id": "r9"},
    ], [
        _appel_journal("r1", "data_write", cle="p1"),
        _appel_journal("r1", "data_write", cle="p2"),
        _appel_journal("r1", "whatsapp_act", "send"),
        _appel_journal("r1", "hubspot_object", "update", cle="c1"),   # ne déclare rien
        _appel_journal("r9", "data_write", cle="z"),
    ], surcharges={"record": 5})
    (a,) = rep["summary"]
    assert a["time_saved_minutes"] == pytest.approx(2 * 5 + time_saved.KINDS["action"]["minutes"])
    assert [(r["kind"], r["count"], r["minutes_each"]) for r in a["time_saved"]] == [
        ("action", 1, time_saved.KINDS["action"]["minutes"]), ("record", 2, 5.0)]
    assert a["time_saved_not_counted"] == []
    assert rep["time_standards"]["record"] == 5.0


def test_summary_ne_compte_pas_un_run_sans_journal_et_le_dit(monkeypatch):
    """Un run conclu sans appel au journal (pas de `run_id`, ou rien sous lui) n'est
    PAS compté — ni estimé : il est rendu dans `time_saved_not_counted`."""
    rep = _un_agent_vu(monkeypatch, [
        {"trigger_id": "5", "run_id": "r1"},
        {"trigger_id": "5", "run_id": None},
        {"trigger_id": "5", "run_id": "r2"},          # lié, mais rien de journalisé
    ], [
        _appel_journal("r1", "serper_search"),
        _appel_journal("r1", "data_write", cle="p1"),
        _appel_journal("r1", "data_write", ok=False, cle="p2"),
    ])
    (a,) = rep["summary"]
    assert [(r["kind"], r["count"]) for r in a["time_saved"]] == [("record", 1), ("research", 1)]
    assert a["time_saved_not_counted"] == [{"reason": "no_logged_calls", "runs": 2}]
