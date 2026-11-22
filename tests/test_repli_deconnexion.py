"""QUAND un abonnement se replie : au plafond seulement, ou aussi DÉCONNECTÉ — EN BASE
(22/11/2026, `docs/repli-cle-plateforme.md` §Quand se replier).

Le repli d'un abonnement ne partait que d'un forfait au plafond
(`test_repli_plafond_api.py`, `test_repli_cle_plateforme.py` : inchangés, ils tiennent le
défaut). Une org peut maintenant choisir `repli_quand = plafond_ou_deconnexion`. Ce que
ces bancs tiennent, contre une vraie base et par le vrai chemin de réservation
(`capabilities.runner_jobs._jobs`, un worker de plateforme qui mesure) :

1. ce qui devient repliable — le demandeur déconnecté, un pool tout déconnecté, un pool
   mélangé plafond + déconnexion, et (sans mode réglé) le pool d'un demandeur sans
   abonnement — sur la clé de l'org ET sur la clé de plateforme, cause stampée ;
2. ce qui attend toujours : le réglage `plafond`, un pool sans prêteur, un demandeur sans
   abonnement, un prêteur servable, `repli_api` fermé, une org sans payeur, un modèle
   éteint (des deux côtés), une déconnexion trop récente ou sans sandbox ;
3. la prise revérifie le réglage et l'état, pour tout payeur ;
4. reconnecté, l'abonnement reprend ; un repli déjà pris finit, et se défait à l'octet.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_F = "claude_subscription"
_API = "anthropic"
_CLE_PLATEFORME = "sk-ant-api03-plateforme-deconnexion"
_CLE_ORG = "sk-ant-api03-de-l-org-deconnexion"
_WORKER = "worker:banc-deconnexion"


@pytest.fixture(scope="module", autouse=True)
def _cle_maitre():
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "8" * 64
    yield
    if avant is None:
        os.environ.pop("OTO_MCP_MASTER_KEY", None)
    else:
        os.environ["OTO_MCP_MASTER_KEY"] = avant


@pytest.fixture(scope="module", autouse=True)
def bareme(live):
    """Un barème RÉEL : le modèle replié de `sub:sonnet` a ses quatre lignes, la clé de
    plateforme peut donc le refacturer (`test_repli_cle_plateforme.py`, même barème)."""
    from oto_mcp.access import credits
    from oto_mcp.access import credits_tours as tours
    from oto_mcp.db import credits as db_credits
    db_credits.poser_bareme(1, [
        {"connector": tours.MODELE_CONNECTOR,
         "function": tours.fonction_du_modele("claude-sonnet-5-5"),
         "unit": u, "budget": 1, "per_n": 10} for u in tours.UNITES_DU_MODELE])
    credits.oublier()
    yield 1
    credits.oublier()


@pytest.fixture(scope="module", autouse=True)
def cle_de_plateforme(live):
    from oto_mcp import credentials_store
    credentials_store.set_platform_key("plateforme", _API, _CLE_PLATEFORME, set_by="banc")
    return _CLE_PLATEFORME


@pytest.fixture(autouse=True)
def permis(monkeypatch):
    monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", "1")
    monkeypatch.delenv("OTO_REPLI_API_PAR_DEFAUT", raising=False)


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _nom():
    return "rd-" + uuid.uuid4().hex[:8]


def _org(nom, *membres, mode=None, quand="plafond_ou_deconnexion", repli=True,
         payeur="cle_org"):
    """Une org, ses membres, son mode ; le repli API OUVERT ; `quand` choisi par son
    admin ; et son PAYEUR : sa clé déposée (`cle_org`), la clé de plateforme choisie et
    accordée (`cle_plateforme`), ou rien (None)."""
    from oto_mcp import credentials_store, org_store
    from oto_mcp.db import org_subscription_pool as P
    admin = _personne(f"{nom}-admin")
    oid = org_store.create_org(f"Org {nom}", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, _personne(m), "org_member")
    if mode:
        P.poser_mode(oid, _F, mode, admin)
    if repli:
        P.set_repli_api(oid, _F, True, admin)
    if quand:
        P.set_repli_quand(oid, _F, quand, admin)
    if payeur == "cle_org":
        credentials_store.set_credential("org", str(oid), _API, _CLE_ORG)
    elif payeur == "cle_plateforme":
        P.set_repli_cle_plateforme(oid, _F, True, admin)
        credentials_store.platform_grant(_API, f"org:{oid}")
    return oid


def _abonne(sub, statut="needs_login", *, pret_a=(), vieux=True):
    """Un abonné et son état. Une déconnexion est par défaut VIEILLE (au-delà de
    `DECONNEXION_TENUE_S`) ; un `paused_limit` a une échéance future."""
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    from oto_mcp.db._conn import _connect
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    reset = _futur() if statut == "paused_limit" else None
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected",
                      epuise=True)
    if vieux:
        with _connect() as conn:
            conn.execute("UPDATE user_model_subscriptions SET updated_at = NOW() - "
                         "interval '1 hour' WHERE sub = %s AND famille = %s", (sub, _F))
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _ouvrir(sub, org):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO user_model_subscription_orgs (sub, famille, org_id) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sub, _F, org))


def _travail(org, sub, model="sub:sonnet", ouvert=True):
    from oto_mcp import db
    if ouvert:
        _ouvrir(sub, org)
    return db.enqueue_job(org, "start", sub=sub,
                          payload={"procedure": "p", "model": model,
                                   "model_family": _F})["id"]


def _futur(heures=1):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


def _reserver(org_ids, worker=_WORKER):
    """Le VRAI chemin : un worker de plateforme qui réclame `anthropic` et mesure."""
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    return RJ._jobs(ResolvedCtx(sub=worker, org_id=None, platform_worker=True),
                    RJ.JobsInput(op="claim", provider=_API, org_ids=list(org_ids),
                                 meters_usage=True))["job"]


def _etat(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute("SELECT status, attempts, payload FROM runner_jobs "
                                 "WHERE id = %s", (job_id,)).fetchone())


def _attend(job_id):
    """Le travail ATTEND : en file, jamais pris, sa charge d'abonnement intacte."""
    e = _etat(job_id)
    assert (e["status"], e["attempts"]) == ("pending", 0), e
    assert e["payload"]["model_family"] == _F
    assert "repli" not in (e["payload"].get("_plateforme") or {})


def _servi(job, jid, payeur):
    """Servi sur le payeur attendu, et le repli stampé : rend le stamp."""
    assert job is not None and job["id"] == jid
    assert job["payload"]["model"] == "claude-sonnet-5-5"
    assert job["payload"]["model_family"] == _API
    if payeur == "cle_org":
        assert job["model_key"] == _CLE_ORG and "meter_usage" not in job
    else:
        assert job["model_key"] == _CLE_PLATEFORME and job["meter_usage"] is True
    repli = _etat(jid)["payload"]["_plateforme"]["repli"]
    assert repli["payeur"] == payeur
    return repli


_PAYEURS = ["cle_org", "cle_plateforme"]


# ── 1. ce qui devient repliable ─────────────────────────────────────────────

@pytest.mark.parametrize("payeur", _PAYEURS)
@pytest.mark.parametrize("statut", ["needs_login", "disconnected"])
def test_le_demandeur_deconnecte_se_replie(statut, payeur):
    n = _nom()
    oid = _org(n, f"{n}-dem", payeur=payeur)
    _abonne(f"{n}-dem", statut)
    jid = _travail(oid, f"{n}-dem")

    repli = _servi(_reserver([oid]), jid, payeur)

    assert (repli["cause"], repli["reason"], repli["reset_at"]) == (
        "deconnexion", "signed_out", None), "une déconnexion n'a pas d'échéance"
    assert (repli["mode"], repli["forfait"]) == ("personnel", f"{_F}:sub:{n}-dem")


@pytest.mark.parametrize("payeur", _PAYEURS)
def test_un_pool_tout_deconnecte_se_replie(payeur):
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-a", f"{n}-b", mode="pool", payeur=payeur)
    _abonne(f"{n}-a", "needs_login", pret_a=[oid])
    _abonne(f"{n}-b", "disconnected", pret_a=[oid])
    jid = _travail(oid, f"{n}-dem")

    repli = _servi(_reserver([oid]), jid, payeur)

    assert (repli["cause"], repli["mode"], repli["forfait"]) == (
        "deconnexion", "pool", f"{_F}:org:{oid}")
    assert repli["reset_at"] is None


@pytest.mark.parametrize("payeur", _PAYEURS)
def test_un_pool_melange_plafond_et_deconnexion_se_replie(payeur):
    """Le pool mélangé que la règle d'avant laissait attendre
    (`test_pas_de_repli_pool_melange`) : chacun est en pause OU déconnecté."""
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-a", f"{n}-b", mode="pool", payeur=payeur)
    _abonne(f"{n}-a", "paused_limit", pret_a=[oid])
    _abonne(f"{n}-b", "disconnected", pret_a=[oid])
    jid = _travail(oid, f"{n}-dem")

    repli = _servi(_reserver([oid]), jid, payeur)

    assert (repli["cause"], repli["reason"], repli["mode"]) == (
        "deconnexion", "signed_out", "pool"), "seule la déconnexion l'a rendu repliable"


@pytest.mark.parametrize("payeur", _PAYEURS)
def test_sans_mode_regle_le_pool_d_un_demandeur_sans_abonnement_se_replie(payeur):
    """Sans mode réglé, le pool complète — un membre sans abonnement à lui n'a que le
    seul abonnement prêté. Déconnecté, la réservation le
    renvoie sur une voie personnelle où il n'a rien : la règle d'avant n'y voyait rien à
    remplacer. Le repli remplace le pool qu'il aurait emprunté."""
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-p", payeur=payeur)
    _abonne(f"{n}-p", "needs_login", pret_a=[oid])
    jid = _travail(oid, f"{n}-dem", ouvert=False)

    repli = _servi(_reserver([oid]), jid, payeur)

    assert (repli["cause"], repli["mode"], repli["forfait"]) == (
        "deconnexion", "pool", f"{_F}:org:{oid}")


def test_sans_mode_regle_un_pool_au_plafond_se_replie_aussi_sous_le_nouveau_reglage():
    """Même voie, au plafond : sous `plafond`, ce demandeur attendait la réinitialisation
    (il n'a rien à replier sur la voie personnelle) ; l'org qui a choisi d'élargir le
    voit replié, cause `plafond`, avec son échéance."""
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-p", quand="plafond")
    _abonne(f"{n}-p", "paused_limit", pret_a=[oid])
    jid = _travail(oid, f"{n}-dem", ouvert=False)
    assert _reserver([oid]) is None, "sous `plafond`, inchangé : il attend"
    _attend(jid)

    from oto_mcp.db import org_subscription_pool as P
    P.set_repli_quand(oid, _F, "plafond_ou_deconnexion", f"{n}-admin")
    repli = _servi(_reserver([oid]), jid, "cle_org")
    assert (repli["cause"], repli["reason"], repli["mode"]) == (
        "plafond", "paused_limit", "pool")
    assert repli["reset_at"] is not None


def test_un_plafond_sous_le_nouveau_reglage_garde_sa_cause_et_son_echeance():
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", "paused_limit")
    jid = _travail(oid, f"{n}-dem")

    repli = _servi(_reserver([oid]), jid, "cle_org")
    assert (repli["cause"], repli["reason"]) == ("plafond", "paused_limit")
    assert repli["reset_at"] is not None


def test_sous_le_defaut_un_plafond_se_stampe_plafond():
    """Le stamp s'enrichit sans rien changer d'autre : `reason` reste `paused_limit`."""
    n = _nom()
    oid = _org(n, f"{n}-dem", quand=None)
    _abonne(f"{n}-dem", "paused_limit")
    jid = _travail(oid, f"{n}-dem")

    repli = _servi(_reserver([oid]), jid, "cle_org")
    assert (repli["cause"], repli["reason"]) == ("plafond", "paused_limit")


def test_un_repli_d_avant_le_champ_se_lit_plafond():
    from oto_mcp import db
    assert db.cause_du_repli({"forfait": "x"}) == "plafond"
    assert db.cause_du_repli({"cause": "deconnexion"}) == "deconnexion"
    assert db.cause_du_repli(None) is None


# ── 2. ce qui attend toujours ───────────────────────────────────────────────

@pytest.mark.parametrize("quand", [None, "plafond"])
def test_sous_plafond_une_deconnexion_attend(quand):
    """Rien réglé, ou `plafond` posé : la règle d'avant, à l'identique — demandeur
    déconnecté, pool tout déconnecté, pool mélangé, pool d'un demandeur sans abonnement."""
    n = _nom()
    perso = _org(f"{n}a", f"{n}a-dem", quand=quand)
    _abonne(f"{n}a-dem", "needs_login")
    j1 = _travail(perso, f"{n}a-dem")
    pool = _org(f"{n}b", f"{n}b-dem", f"{n}b-x", f"{n}b-y", mode="pool", quand=quand)
    _abonne(f"{n}b-x", "paused_limit", pret_a=[pool])
    _abonne(f"{n}b-y", "disconnected", pret_a=[pool])
    j2 = _travail(pool, f"{n}b-dem")
    complete = _org(f"{n}c", f"{n}c-dem", f"{n}c-p", quand=quand)
    _abonne(f"{n}c-p", "needs_login", pret_a=[complete])
    j3 = _travail(complete, f"{n}c-dem", ouvert=False)

    assert _reserver([perso, pool, complete]) is None
    for jid in (j1, j2, j3):
        _attend(jid)


def test_un_pool_sans_preteur_attend():
    n = _nom()
    pool = _org(f"{n}a", f"{n}a-dem", mode="pool")
    j1 = _travail(pool, f"{n}a-dem")
    sans_mode = _org(f"{n}b", f"{n}b-dem")
    j2 = _travail(sans_mode, f"{n}b-dem", ouvert=False)

    assert _reserver([pool, sans_mode]) is None
    _attend(j1)
    _attend(j2)


def test_un_demandeur_sans_abonnement_attend_hors_du_pool():
    """Mode `personnel` : il n'y a rien à reconnecter ni à remplacer — « jamais
    connecté » attend. Une ligne sans sandbox non plus."""
    from oto_mcp.db._conn import _connect
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-sans", f"{n}-p", mode="personnel")
    _abonne(f"{n}-p", "needs_login", pret_a=[oid])
    j1 = _travail(oid, f"{n}-dem")
    with _connect() as conn:
        conn.execute("INSERT INTO user_model_subscriptions (sub, famille, statut, "
                     "updated_at) VALUES (%s, %s, 'disconnected', NOW() - interval "
                     "'1 hour')", (f"{n}-sans", _F))
    j2 = _travail(oid, f"{n}-sans")

    assert _reserver([oid]) is None
    _attend(j1)
    _attend(j2)


def test_un_preteur_servable_empeche_le_repli():
    """Un pool qui a un prêteur servable n'est pas bloqué : il sert."""
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-a", f"{n}-b", mode="pool")
    _abonne(f"{n}-a", "needs_login", pret_a=[oid])
    _abonne(f"{n}-b", "connected", pret_a=[oid])
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)


def test_repli_api_coupe_ou_sans_payeur_attend():
    n = _nom()
    ferme = _org(f"{n}a", f"{n}a-dem", repli=False)
    _abonne(f"{n}a-dem", "needs_login")
    j1 = _travail(ferme, f"{n}a-dem")
    sans_payeur = _org(f"{n}b", f"{n}b-dem", payeur=None)
    _abonne(f"{n}b-dem", "needs_login")
    j2 = _travail(sans_payeur, f"{n}b-dem")

    assert _reserver([ferme, sans_payeur]) is None
    _attend(j1)
    _attend(j2)


@pytest.mark.parametrize("quand", ["plafond", "plafond_ou_deconnexion"])
@pytest.mark.parametrize("eteint", ["sub:sonnet", "claude-sonnet-5-5"])
def test_un_modele_eteint_ne_se_replie_jamais(quand, eteint):
    """#1778 : un modèle éteint s'arrête, il ne se replie pas. Ni le modèle du travail
    (rerouté, il tournerait sur un modèle allumé), ni celui d'arrivée (replié, il serait
    arrêté à la remise au lieu d'attendre) — sous les deux réglages."""
    from oto_mcp.db import modeles_eteints
    n = _nom()
    oid = _org(n, f"{n}-dem", quand=quand)
    _abonne(f"{n}-dem", "paused_limit" if quand == "plafond" else "needs_login")
    jid = _travail(oid, f"{n}-dem")
    assert modeles_eteints.poser(oid, eteint, allume=False)

    assert _reserver([oid]) is None
    _attend(jid)

    assert modeles_eteints.poser(oid, eteint, allume=True)
    assert _servi(_reserver([oid]), jid, "cle_org"), "rallumé, il se replie"


def test_des_travaux_au_modele_eteint_ne_cachent_pas_une_org_qui_peut_replier():
    """Filtré AVANT la limite des candidats : trente travaux plus anciens d'une org dont
    le modèle d'arrivée est éteint ne cachent pas celui d'une org qui peut replier."""
    from oto_mcp.db import modeles_eteints
    from oto_mcp.db._conn import _connect
    n = _nom()
    muette = _org(f"{n}m", *[f"{n}m-{i}" for i in range(30)])
    assert modeles_eteints.poser(muette, "claude-sonnet-5-5", allume=False)
    for i in range(30):
        _abonne(f"{n}m-{i}", "needs_login")
        _travail(muette, f"{n}m-{i}")
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = NOW() - interval '1 hour' "
                     "WHERE org_id = %s", (muette,))
    payeuse = _org(f"{n}p", f"{n}p-dem")
    _abonne(f"{n}p-dem", "needs_login")
    jid = _travail(payeuse, f"{n}p-dem")

    assert _servi(_reserver([muette, payeuse]), jid, "cle_org")


def test_une_deconnexion_trop_recente_attend():
    """`DECONNEXION_TENUE_S` : un `needs_login` le temps d'un jeton qui se renouvelle
    n'achète pas un run."""
    from oto_mcp.db._conn import _connect
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", "needs_login", vieux=False)
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)
    with _connect() as conn:
        conn.execute("UPDATE user_model_subscriptions SET updated_at = NOW() - interval "
                     "'11 minutes' WHERE sub = %s", (f"{n}-dem",))
    assert _servi(_reserver([oid]), jid, "cle_org")


# ── 3. la prise revérifie ───────────────────────────────────────────────────

def _candidat(oid, plateforme=False):
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    return db.candidats_repli_abonnement(None, [oid], frozenset({_F}), _API,
                                         _abonnement.DEFAUT_LIMITE_PCT,
                                         plateforme=plateforme)


def _prendre(jid, payeur):
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    return db.claim_fallback_job(jid, "w-course", "claude-sonnet-5-5", _API,
                                 _abonnement.DEFAUT_LIMITE_PCT, payeur=payeur)


@pytest.mark.parametrize("payeur", _PAYEURS)
def test_la_prise_revérifie_le_reglage_pour_tout_payeur(payeur):
    """Un admin qui repasse à `plafond` entre la lecture des candidats et la prise gagne
    — pour la clé de l'org aussi, que la prise ne revérifiait pas ailleurs."""
    from oto_mcp.db import org_subscription_pool as P
    n = _nom()
    oid = _org(n, f"{n}-dem", payeur=payeur)
    _abonne(f"{n}-dem", "needs_login")
    jid = _travail(oid, f"{n}-dem")
    (cand,) = _candidat(oid, plateforme=payeur == "cle_plateforme")
    assert (cand["id"], cand["cause"]) == (jid, "deconnexion")

    P.set_repli_quand(oid, _F, "plafond", f"{n}-admin")

    assert _prendre(jid, payeur) is None
    _attend(jid)


def test_une_reconnexion_entre_la_lecture_et_la_prise_gagne():
    from oto_mcp.db import user_subscriptions as US
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", "needs_login")
    jid = _travail(oid, f"{n}-dem")
    assert [c["id"] for c in _candidat(oid)] == [jid]

    US.marquer_statut(f"{n}-dem", _F, US.CONNECTE, ok=True)

    assert _prendre(jid, "cle_org") is None
    _attend(jid)


def test_un_modele_eteint_entre_la_lecture_et_la_prise_gagne():
    from oto_mcp.db import modeles_eteints
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", "needs_login")
    jid = _travail(oid, f"{n}-dem")
    assert [c["id"] for c in _candidat(oid)] == [jid]

    assert modeles_eteints.poser(oid, "claude-sonnet-5-5", allume=False)

    assert _prendre(jid, "cle_org") is None
    _attend(jid)


# ── 4. reconnecté : l'abonnement reprend, le repli pris finit ───────────────

def test_reconnecte_les_suivants_attendent_l_abonnement_et_le_replie_finit():
    from oto_mcp import db
    from oto_mcp.db import user_subscriptions as US
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", "needs_login")
    j1 = _travail(oid, f"{n}-dem")
    j2 = _travail(oid, f"{n}-dem")
    assert _servi(_reserver([oid]), j1, "cle_org")

    US.marquer_statut(f"{n}-dem", _F, US.CONNECTE, ok=True)

    assert _reserver([oid], worker="worker:autre") is None, (
        "reconnecté : le suivant n'est plus candidat, l'abonnement le servira")
    _attend(j2)
    e = _etat(j1)
    assert (e["status"], e["payload"]["model_family"]) == ("claimed", _API), (
        "le travail déjà replié n'est pas touché : il finit sur son payeur")
    conclu = db.complete_job(j1, _WORKER, True, result={"ok": True})
    assert conclu is not None and conclu["status"] == "done"


def test_defaire_un_repli_de_deconnexion_rend_la_charge_a_l_octet():
    from oto_mcp import db
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", "disconnected")
    jid = _travail(oid, f"{n}-dem", model="sub:haiku")
    avant = _etat(jid)["payload"]
    from oto_mcp.capabilities import _abonnement
    assert db.repli_disponible(None, [oid], "w-defaire", _API,
                               _abonnement.DEFAUT_LIMITE_PCT) is not None

    assert db.defaire_le_repli(jid, "w-defaire", "banc")

    apres = _etat(jid)
    assert (apres["status"], apres["attempts"]) == ("pending", 0)
    assert {k: v for k, v in apres["payload"].items() if k != "_plateforme"} == \
        {k: v for k, v in avant.items() if k != "_plateforme"}
    defait = apres["payload"]["_plateforme"]["repli_defait"]
    assert (defait["cause"], defait["raison"]) == ("deconnexion", "banc")


# ── le réglage, en base ─────────────────────────────────────────────────────

@pytest.mark.parametrize("declare, attendu", [(None, False), ("1", True)])
def test_une_ligne_nee_du_reglage_suit_le_defaut_de_repli_de_l_instance(
        live, monkeypatch, declare, attendu):
    from oto_mcp.db import org_subscription_pool as P
    if declare:
        monkeypatch.setenv("OTO_REPLI_API_PAR_DEFAUT", declare)
    n = _nom()
    oid = _org(n, repli=False, quand=None, payeur=None)

    ligne = P.set_repli_quand(oid, _F, "plafond_ou_deconnexion", f"{n}-admin")

    assert (ligne["mode"], ligne["repli_api"]) == (None, attendu)
    assert (ligne["repli_quand"], ligne["repli_quand_by"]) == (
        "plafond_ou_deconnexion", f"{n}-admin")
    assert P.get_mode(oid, _F)["updated_by"] is None, "`updated_by` appartient au mode"


def test_le_reglage_ne_touche_ni_au_repli_api_ni_au_payeur(live):
    from oto_mcp.db import org_subscription_pool as P
    n = _nom()
    oid = _org(n, quand=None, payeur="cle_plateforme")
    P.set_repli_api(oid, _F, False, f"{n}-admin")

    P.set_repli_quand(oid, _F, "plafond_ou_deconnexion", f"{n}-admin")
    P.set_repli_quand(oid, _F, "plafond", f"{n}-admin")

    ligne = P.get_mode(oid, _F)
    assert (ligne["repli_api"], ligne["repli_cle_plateforme"], ligne["repli_quand"]) == (
        False, True, "plafond")
    with pytest.raises(ValueError):
        P.set_repli_quand(oid, _F, "toujours", f"{n}-admin")
