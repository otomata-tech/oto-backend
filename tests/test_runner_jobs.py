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


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os
    import uuid as _uuid

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_rjobs_" + _uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


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
    assert out == {"status": "done", "run_id": None}, \
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
            "procedure": "enrichissement", "project_id": 220, "namespace": "tableau",
            "tools": ["data_claim_next", "data_write"], "input": "file {namespace}",
            "row_filter": {"statut": "a_enrichir"}, "max_steps": 40,
            "max_tokens_per_row": 80000}


@pytest.fixture
def campagne(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id: espion.update(cherchee=org_id) or CAMPAGNE)
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
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id: dict(
        CAMPAGNE, input="file {namespace} filtre {filter} du {date_du_jour}"))
    _appel(_ctx(), op="claim")
    servi = campagne["payload"]["input"]
    assert "{" not in servi.replace('{"statut"', ""), f"marqueur resté en littéral : {servi!r}"
    assert servi.startswith("file tableau filtre ")


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
                        lambda org_id: {**CAMPAGNE, "model": "mistral-large-2512"})
    rendu = _appel(_ctx(), op="claim")
    assert "campaign_error" not in rendu, rendu.get("campaign_error")
    assert campagne["payload"]["model"] == "mistral-large-2512"
    assert campagne["payload"]["model_family"] == "mistral"


def test_une_campagne_SANS_modele_produit_le_travail_d_avant(campagne):
    _appel(_ctx(), op="claim")
    assert "model" not in campagne["payload"]
    assert "model_family" not in campagne["payload"]


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
                        lambda org_id: {**CAMPAGNE, "max_tokens_per_row": None})
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    _appel(_ctx(), op="claim")
    assert espion["payload"]["max_tokens"] is None


def test_la_campagne_passe_a_running_au_premier_travail(campagne):
    _appel(_ctx(), op="claim")
    assert campagne["demarree"] == 12


def test_sans_campagne_le_sondage_rend_simplement_rien(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id: None)
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    assert _appel(_ctx(), op="claim") == {"job": None}
    assert "fleet" not in espion, "aucun travail ne doit être fabriqué"


def test_une_campagne_illisible_ne_casse_PAS_le_sondage(monkeypatch, espion):
    """Fail-open : le sondage des workers est le chemin le plus fréquent de toute
    la plateforme. Une campagne mal formée ne doit pas l'arrêter pour l'org —
    le passage attendra le sondage suivant."""
    def _explose(org_id):
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
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id: None)
    assert _appel(_ctx(), op="claim") == {"job": None}


def test_une_campagne_cassee_ne_journalise_QU_UNE_fois(monkeypatch, espion, caplog):
    """Le sondage tourne en boucle sur chaque worker. Journaliser à chaque tour
    noierait le journal sous des milliers de lignes identiques — et un journal
    noyé ne se lit pas, ce qui revient à ne rien dire. On veut le contraire :
    une ligne qui se voit."""
    RJ._CAMPAGNE_MUETTE.clear()
    def _explose(org_id):
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
    def _explose(org_id):
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
                        lambda org_id: ordre.append("service") or None)
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    with caplog.at_level("WARNING"):
        _appel(_ctx(), op="claim")
    assert ordre == ["arret", "service"], "arrêter d'abord, servir ensuite"
    assert any("campagne 77 arrêtée" in r.message for r in caplog.records), \
        "un arrêt automatique qui ne se dit pas est un silence de plus"



# ── La consigne servie DANS le cadre, pas chargée par l'agent ────────────────
# Mesuré le 08/09/2026 sur une passe réelle : la consigne que l'agent charge au
# premier tour est FACTURÉE plein tarif au deuxième — 20 603 jetons sur 41 204,
# la moitié du déroulé, avec un cache à zéro sur ce tour-là. Jointe au travail à
# la réservation, elle entre dans le préfixe stable : lue en cache dès le
# premier tour, et le tour de chargement disparaît.
#
# ⚠️ Jointe, JAMAIS stockée : une consigne pèse ~20 000 caractères, et cent
# travaux la porteraient cent fois en base. Même raison que la clé de modèle.

# ⚠️ Doublée sur `org_store.get_instruction`, la lecture de `oto_procedure`. Ces
# bancs doublaient `get_guide_db` — la mauvaise table — et restaient verts : ils
# prouvaient que la jonction pose ce qu'on lui rend, jamais qu'elle lit là où
# vivent les procédures. Mesuré le 12/09/2026 en production : rien de joint.

def test_le_travail_reserve_porte_le_TEXTE_de_sa_procedure(monkeypatch, espion):
    monkeypatch.setattr(RJ.org_store, "get_instruction",
                        lambda owner_type, owner_id, slug, version=None:
                        {"body_md": "LA CONSIGNE", "archived_at": None})
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "passe-registre"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert job["system"] == "LA CONSIGNE", (
        "le worker reçoit du TEXTE à poser en cadre — il ne va rien chercher, "
        "et il ignore ce qu'est une procédure")


def test_une_procedure_ABSENTE_ne_fait_pas_echouer_la_reservation(monkeypatch, espion):
    """L'agent la chargera lui-même, comme avant. C'est une accélération,
    jamais une condition — un travail ne se perd pas parce qu'un texte manque."""
    monkeypatch.setattr(RJ.org_store, "get_instruction",
                        lambda owner_type, owner_id, slug, version=None: None)
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "jamais-posee"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert "system" not in job
    assert job["id"] == 7, "le travail est servi quand même"


def test_une_lecture_de_procedure_qui_LEVE_ne_perd_pas_le_travail(monkeypatch, espion):
    """Le pendant du précédent, et le plus important des trois : la base peut
    tousser. Un travail réservé qui se perdrait ici laisserait sa ligne sous
    bail jusqu'à expiration."""
    def _explose(owner_type, owner_id, slug, version=None):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(RJ.org_store, "get_instruction", _explose)
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "passe-registre"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert "system" not in job and job["id"] == 7


def test_une_procedure_ARCHIVEE_n_est_pas_jointe(monkeypatch, espion):
    """La lecture par slug sert une procédure retirée comme une procédure en
    service (#857) : la jonction ne la pose pas en cadre."""
    monkeypatch.setattr(RJ.org_store, "get_instruction",
                        lambda owner_type, owner_id, slug, version=None:
                        {"body_md": "RETIRÉE", "archived_at": "2026-09-01"})
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "passe-retiree"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert "system" not in job and job["id"] == 7


def test_la_jonction_lit_la_table_des_PROCEDURES_pas_les_guides(monkeypatch, espion):
    """La couture elle-même : slug et org du TRAVAIL, portée org, table de
    `oto_procedure`. Un retour à `get_guide_db` lirait la vraie base depuis ce
    banc et ne joindrait rien."""
    vu = {}
    def _lue(owner_type, owner_id, slug, version=None):
        vu.update(owner_type=owner_type, owner_id=owner_id, slug=slug)
        return {"body_md": "CONSIGNE", "archived_at": None}
    monkeypatch.setattr(RJ.org_store, "get_instruction", _lue)
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "passe-registre"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert vu == {"owner_type": "org", "owner_id": 226, "slug": "passe-registre"}
    assert job["system"] == "CONSIGNE"


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
