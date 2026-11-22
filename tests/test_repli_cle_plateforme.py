"""Le REPLI plafond → clé de PLATEFORME, EN BASE (19/11/2026, `docs/repli-cle-plateforme.md`).

Le repli d'un abonnement épuisé n'avait qu'une cible : la clé que l'org a déposée
(`test_repli_plafond_api.py`, inchangé). Une org peut maintenant dire « je paie » d'une
SECONDE manière, explicite : le réglage `repli_cle_plateforme`, sur une instance qui le
permet (`OTO_REPLI_CLE_PLATEFORME_PERMIS`). Ce que ces bancs tiennent, contre une vraie
base et par le vrai chemin de réservation (`capabilities.runner_jobs._jobs`) :

1. plafond futur + `repli_api` + réglage + instance qui permet + aucune clé d'org + clé
   partagée ACCORDÉE → servi sur la clé de plateforme, `meter_usage`, payeur stampé
   `cle_plateforme` ;
2. sans réglage, sur une instance qui ne permet pas, sans accord, avec `repli_api`
   coupé, à reconnecter, pool vide, worker qui ne mesure pas, ferme → le travail ATTEND,
   jamais arrêté ;
3. l'org a SA clé → sa clé paie (`cle_org`), quoi que dise le réglage ;
4. un seul repli en vol par forfait, la prise revérifie le réglage ;
5. la remise défait (jamais n'arrête, jamais ne sert sans mesure) ce qu'elle ne peut
   plus servir comme la prise l'avait décidé ;
6. LA PREUVE DE FACTURATION : un run replié, réservé, réglé et conclu, se lit
   `cle_plateforme` pour l'org du travail — au grand livre de crédits (le payeur
   `org:<id>`, le rang `platform`), au relevé de la clé de plateforme
   (`jetons_du_jour`) et à la lentille `org.usage.tokens`.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_F = "claude_subscription"
_API = "anthropic"
_CLE_PLATEFORME = "sk-ant-api03-plateforme-banc"
_WORKER = "worker:banc"


@pytest.fixture(scope="module", autouse=True)
def _cle_maitre():
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
    yield
    if avant is None:
        os.environ.pop("OTO_MCP_MASTER_KEY", None)
    else:
        os.environ["OTO_MCP_MASTER_KEY"] = avant


@pytest.fixture(scope="module", autouse=True)
def bareme(live):
    """Un barème RÉEL (en base), le seul de la base du module : le modèle replié de
    `sub:sonnet` y a ses quatre lignes (1 unité par 10 jetons), celui de `sub:opus` NON —
    un repli vers un modèle sans prix ne se facture pas, il attend. Relu tout de suite
    (`credits.oublier`)."""
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


@pytest.fixture(scope="module")
def cle_de_plateforme(live):
    """La clé `anthropic` de PLATEFORME, posée FERMÉE (elle ne sert que les orgs
    accordées) — une fois pour le module."""
    from oto_mcp import credentials_store
    credentials_store.set_platform_key("plateforme", _API, _CLE_PLATEFORME, set_by="banc")
    return _CLE_PLATEFORME


@pytest.fixture(autouse=True)
def permis(monkeypatch):
    """L'instance PERMET le repli sur la clé de plateforme, sauf banc qui le retire."""
    monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", "1")
    monkeypatch.delenv("OTO_REPLI_API_PAR_DEFAUT", raising=False)

    def poser(valeur):
        if valeur is None:
            monkeypatch.delenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", raising=False)
        else:
            monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", valeur)
    return poser


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _nom():
    return "rp-" + uuid.uuid4().hex[:8]


def _org(nom, *membres, mode=None, repli=True, plateforme=True, accordee=True):
    """Une org, ses membres, son mode ; le repli API OUVERT, le repli sur la clé de
    plateforme CHOISI par son admin, et la clé de plateforme ACCORDÉE — chaque banc
    retire ce qu'il juge."""
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
    if plateforme:
        P.set_repli_cle_plateforme(oid, _F, True, admin)
    if accordee:
        credentials_store.platform_grant(_API, f"org:{oid}")
    return oid


def _abonne(sub, *, statut="paused_limit", reset="futur", pret_a=()):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    reset = _futur() if reset == "futur" else reset
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected",
                      epuise=True, utilisation=None)
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _travail(org, sub, model="sub:sonnet"):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO user_model_subscription_orgs (sub, famille, org_id) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sub, _F, org))
    return db.enqueue_job(org, "start", sub=sub,
                          payload={"procedure": "p", "model": model,
                                   "model_family": _F})["id"]


def _cle_org(org_id, secret="sk-ant-api03-de-l-org"):
    from oto_mcp import credentials_store
    credentials_store.set_credential("org", str(org_id), _API, secret)


def _futur(heures=1):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


def _reserver(org_ids, *, mesure=True):
    """Le VRAI chemin : un worker de plateforme qui réclame `anthropic` (et promet, ou
    non, de mesurer chaque appel), borné aux orgs du banc."""
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    return RJ._jobs(ResolvedCtx(sub=_WORKER, org_id=None, platform_worker=True),
                    RJ.JobsInput(op="claim", provider=_API, org_ids=list(org_ids),
                                 meters_usage=mesure))["job"]


def _etat(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute("SELECT status, attempts, payload, last_error "
                                 "FROM runner_jobs WHERE id = %s", (job_id,)).fetchone())


def _attend(job_id):
    """Le travail ATTEND : en file, jamais pris, jamais arrêté, sa charge intacte."""
    e = _etat(job_id)
    assert (e["status"], e["attempts"]) == ("pending", 0), e
    assert e["payload"]["model"] == "sub:sonnet" and e["payload"]["model_family"] == _F
    assert "repli" not in (e["payload"].get("_plateforme") or {})


# ── 1. le chemin neuf, servi ────────────────────────────────────────────────

def test_sans_cle_d_org_le_repli_est_SERVI_sur_la_cle_de_plateforme(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    job = _reserver([oid])

    assert job is not None and job["id"] == jid
    assert job["model_key"] == cle_de_plateforme, "la clé PARTAGÉE de la plateforme"
    assert job["meter_usage"] is True, "chaque appel se réserve et se règle à l'org"
    assert job["payload"]["model"] == "claude-sonnet-5-5"
    assert job["payload"]["model_family"] == _API
    plateforme = _etat(jid)["payload"]["_plateforme"]
    assert plateforme["repli"]["payeur"] == "cle_plateforme"
    assert plateforme["repli"]["mode"] == "personnel"
    assert plateforme["repli"]["reason"] == "paused_limit"
    assert plateforme["cle_partagee"] == {"depot": _API, "mode": "platform",
                                          "tentative": 1}
    import oto_mcp.db as db
    assert job["payload"]["max_tokens"] == db.REPLI_MAX_TOKENS, "mêmes bornes"


def test_un_pool_integralement_plafonne_replie_sur_la_plateforme(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-p", mode="pool")
    _abonne(f"{n}-p", pret_a=[oid])
    jid = _travail(oid, f"{n}-dem")

    job = _reserver([oid])

    assert job is not None and job["id"] == jid and job["model_key"] == cle_de_plateforme
    repli = _etat(jid)["payload"]["_plateforme"]["repli"]
    assert (repli["mode"], repli["payeur"], repli["forfait"]) == (
        "pool", "cle_plateforme", f"{_F}:org:{oid}")


# ── 2. ce qui ATTEND ────────────────────────────────────────────────────────

def test_sans_le_reglage_de_l_org_le_travail_attend(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem", plateforme=False)
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)


@pytest.mark.parametrize("valeur", [None, "0", "off"])
def test_une_instance_qui_ne_le_permet_pas_ignore_un_reglage_ouvert(cle_de_plateforme,
                                                                   permis, valeur):
    n = _nom()
    oid = _org(n, f"{n}-dem")          # réglage ouvert pendant que l'instance le permettait
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    permis(valeur)

    assert _reserver([oid]) is None, "l'instance ne le permet plus : le réglage est ignoré"
    _attend(jid)


def test_une_valeur_d_instance_illisible_leve(permis):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.interrupteurs import InterrupteurIllisible
    permis("peut-etre")
    with pytest.raises(InterrupteurIllisible, match="OTO_REPLI_CLE_PLATEFORME_PERMIS"):
        P.repli_cle_plateforme_permis()


def test_sans_accord_de_la_cle_de_plateforme_le_travail_attend(cle_de_plateforme):
    """Le réglage choisit le PAYEUR, il n'ouvre pas la clé : la clé de plateforme est
    fermée, et l'accord de l'org en est le droit. Sans lui, on attend — on n'arrête pas
    (la remise ordinaire arrêterait un travail sans accès)."""
    n = _nom()
    oid = _org(n, f"{n}-dem", accordee=False)
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)


def test_repli_api_coupe_rien_ne_replie_meme_avec_le_reglage(cle_de_plateforme):
    """`repli_api` reste la porte : le nouveau réglage choisit qui paie, pas s'il faut
    se replier."""
    n = _nom()
    oid = _org(n, f"{n}-dem", repli=False)
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)


@pytest.mark.parametrize("statut", ["needs_login", "disconnected"])
def test_une_reconnexion_ne_replie_jamais(cle_de_plateforme, statut):
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", statut=statut, reset=None)
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)


def test_un_pool_vide_ne_replie_jamais(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem", mode="pool")
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid]) is None
    _attend(jid)


def test_une_echeance_passee_ne_replie_pas(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem", reset=_futur(-1))
    _travail(oid, f"{n}-dem")

    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.capabilities import runner_jobs as RJ
    assert db.repli_disponible(
        None, [oid], "w-echu", _API, _abonnement.DEFAUT_LIMITE_PCT,
        plateforme_paie=lambda c: RJ.plateforme_paie_le_repli(
            c, _API, metre=True, ferme=False)) is None


def test_un_worker_qui_ne_mesure_pas_ne_prend_rien(cle_de_plateforme):
    """Il ne recevrait pas la clé refacturée : il ne prend pas le travail non plus —
    sinon il le défairait à chaque sondage."""
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    assert _reserver([oid], mesure=False) is None
    _attend(jid)


def test_la_ferme_ne_prend_jamais_le_repli_sur_la_plateforme(cle_de_plateforme):
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.capabilities import runner_jobs as RJ
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    assert db.repli_disponible(
        None, [oid], "w-ferme", _API, _abonnement.DEFAUT_LIMITE_PCT, ferme=True,
        plateforme_paie=lambda c: RJ.plateforme_paie_le_repli(
            c, _API, metre=True, ferme=True)) is None
    _attend(jid)


def test_un_modele_replie_sans_prix_au_bareme_attend(cle_de_plateforme):
    """`sub:opus` se replie sur un modèle que le barème ne tarife pas : servi, il ne se
    réglerait pas (`report`) ou s'arrêterait au premier tour (`enforce`). Il attend."""
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem", model="sub:opus")

    assert _reserver([oid]) is None
    e = _etat(jid)
    assert (e["status"], e["attempts"], e["payload"]["model"]) == ("pending", 0, "sub:opus")


def test_une_org_qui_regle_chaque_personne_attend(cle_de_plateforme):
    """Mode de payeur `person` : le run replié se réglerait sur le solde du DEMANDEUR,
    que le réglage d'un org_admin n'engage pas. Il attend."""
    from oto_mcp.access import credits
    from oto_mcp.db import credits as db_credits
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    db_credits.poser_mode(oid, "person")
    credits.oublier()
    try:
        assert _reserver([oid]) is None
        _attend(jid)
    finally:
        db_credits.poser_mode(oid, "org")
        credits.oublier()


def test_sans_juge_de_la_plateforme_la_porte_reste_fermee(cle_de_plateforme):
    """Tout autre appelant de `repli_disponible` (sans `plateforme_paie`) garde la
    règle d'avant : seule la clé de l'org paie."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")

    assert db.repli_disponible(None, [oid], "w-sans-juge", _API,
                               _abonnement.DEFAUT_LIMITE_PCT) is None
    _attend(jid)


# ── 3. l'org qui a SA clé paie sur elle ─────────────────────────────────────

def test_une_org_qui_a_sa_cle_paie_sur_sa_cle(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem")           # réglage ouvert ET clé accordée : sans effet
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    _cle_org(oid)

    job = _reserver([oid])

    assert job is not None and job["id"] == jid
    assert job["model_key"] == "sk-ant-api03-de-l-org"
    assert "meter_usage" not in job, "sa clé ne se refacture pas"
    plateforme = _etat(jid)["payload"]["_plateforme"]
    assert plateforme["repli"]["payeur"] == "cle_org"
    assert "cle_partagee" not in plateforme


def test_une_cle_d_org_illisible_ne_bascule_jamais_sur_la_plateforme(cle_de_plateforme,
                                                                     monkeypatch):
    """La ligne existe, le coffre ne la rend pas : l'org paie déjà son fournisseur, la
    facturer sur la nôtre la ferait payer deux fois. Sauté, il attend."""
    from oto_mcp import credentials_store
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    _cle_org(oid)
    vraie = credentials_store.get_credential_with_meta

    def muet(entity_type, entity_id, connector, *a, **k):
        if entity_type == "org" and entity_id == str(oid):
            raise RuntimeError("master key indisponible")
        return vraie(entity_type, entity_id, connector, *a, **k)
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", muet)

    assert _reserver([oid]) is None
    _attend(jid)


# ── 4. les bornes : un repli en vol par forfait, la prise revérifie ─────────

def test_des_opt_in_sans_acces_ne_cachent_pas_une_org_qui_paie_sur_sa_cle(cle_de_plateforme):
    """La tête de file (`LIMIT`), avec l'instance qui permet : trente travaux plus
    anciens d'une org opt-in à qui la clé de plateforme n'est PAS accordée (rejetés
    après la limite, à chaque sondage) ne cachent ni le travail d'une org qui paie sur
    SA clé, ni celui d'une org opt-in accordée."""
    from oto_mcp.db._conn import _connect
    n = _nom()
    membres = [f"{n}-m{i}" for i in range(30)]
    muette = _org(n, *membres, accordee=False)
    for m in membres:
        _abonne(m)
        _travail(muette, m)
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = NOW() - interval '1 hour' "
                     "WHERE org_id = %s", (muette,))
    p = _nom()
    payeuse = _org(p, f"{p}-dem", plateforme=False, accordee=False)
    _abonne(f"{p}-dem")
    j_cle = _travail(payeuse, f"{p}-dem")
    _cle_org(payeuse)

    job = _reserver([muette, payeuse])
    assert job is not None and job["id"] == j_cle, "l'org qui paie sur sa clé passe"


def test_le_plus_ancien_par_forfait_seulement_cote_plateforme(cle_de_plateforme):
    """Côté plateforme, un seul repli vole par forfait : les candidats n'en gardent que
    le plus ancien par forfait, demandeur et modèle — trente travaux du MÊME forfait
    ne remplissent pas la limite, et un autre forfait accordé passe."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.db._conn import _connect
    n = _nom()
    muette = _org(n, f"{n}-dem", accordee=False)
    _abonne(f"{n}-dem")
    for _ in range(30):
        _travail(muette, f"{n}-dem")
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = NOW() - interval '1 hour' "
                     "WHERE org_id = %s", (muette,))
    a = _nom()
    accordee = _org(a, f"{a}-dem")
    _abonne(f"{a}-dem")
    j_plat = _travail(accordee, f"{a}-dem")

    cands = db.candidats_repli_abonnement(None, [muette, accordee], frozenset({_F}), _API,
                                          _abonnement.DEFAUT_LIMITE_PCT, plateforme=True)
    assert [c["org_id"] for c in cands].count(muette) == 1
    job = _reserver([muette, accordee])
    assert job is not None and job["id"] == j_plat



def test_un_seul_repli_en_vol_par_forfait(cle_de_plateforme):
    n = _nom()
    oid = _org(n, f"{n}-dem", f"{n}-autre")
    _abonne(f"{n}-dem")
    _abonne(f"{n}-autre")
    j1 = _travail(oid, f"{n}-dem")
    j2 = _travail(oid, f"{n}-dem")
    j3 = _travail(oid, f"{n}-autre")

    assert _reserver([oid])["id"] == j1
    second = _reserver([oid])
    assert second is not None and second["id"] == j3, (
        "le deuxième travail du MÊME forfait attend ; celui d'un autre forfait part")
    assert _reserver([oid]) is None
    _attend(j2)


def test_la_prise_revérifie_le_reglage_de_l_org(cle_de_plateforme):
    """Un admin qui referme le réglage entre la lecture des candidats et la prise
    gagne : la prise relit le réglage dans son WHERE."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.db import org_subscription_pool as P
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    (cand,) = db.candidats_repli_abonnement(None, [oid], frozenset({_F}), _API,
                                            _abonnement.DEFAUT_LIMITE_PCT,
                                            plateforme=True)
    assert cand["id"] == jid and cand["deposee"] is False

    P.set_repli_cle_plateforme(oid, _F, False, f"{n}-admin")

    assert db.claim_fallback_job(jid, "w-course", "claude-sonnet-5-5", _API,
                                 _abonnement.DEFAUT_LIMITE_PCT,
                                 payeur="cle_plateforme") is None
    _attend(jid)


def test_un_payeur_hors_contrat_leve():
    from oto_mcp import db
    with pytest.raises(ValueError, match="payeur"):
        db.claim_fallback_job(1, "w", "claude-sonnet-5-5", _API, 80, payeur="tenant")


def test_un_repli_d_avant_le_champ_se_lit_paye_par_l_org():
    from oto_mcp import db
    assert db.payeur_du_repli({"forfait": "x"}) == "cle_org"
    assert db.payeur_du_repli({"payeur": "cle_plateforme"}) == "cle_plateforme"
    assert db.payeur_du_repli(None) is None


# ── 5. la remise : servi comme décidé, ou DÉFAIT ────────────────────────────

def _prendre_sur_la_plateforme(oid, worker):
    """Une prise payée par la plateforme, sans passer par la remise (la course que la
    remise doit rattraper)."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    row = db.repli_disponible(None, [oid], worker, _API, _abonnement.DEFAUT_LIMITE_PCT,
                              plateforme_paie=lambda c: True)
    assert row is not None
    assert row["payload"]["_plateforme"]["repli"]["payeur"] == "cle_plateforme"
    return row


def _remettre(row, worker, **kw):
    from oto_mcp.capabilities import runner_jobs as RJ
    kw.setdefault("metre", True)
    return RJ._avec_cle(RJ._charge_servie(row), _API, worker, worker=True, **kw)


def _defait(job_id, *mots):
    e = _etat(job_id)
    assert (e["status"], e["attempts"]) == ("pending", 0), e
    assert e["payload"]["model"] == "sub:sonnet", "son abonnement lui est rendu"
    assert "repli" not in e["payload"]["_plateforme"]
    defait = e["payload"]["_plateforme"]["repli_defait"]
    assert defait["payeur"] == "cle_plateforme", "et le repli défait reste traçable"
    for mot in mots:
        assert mot in defait["raison"], defait["raison"]


def test_l_acces_retire_avant_la_remise_DEFAIT_le_repli(cle_de_plateforme):
    """Accordée à la prise, retirée à la remise : servi sans accès, il tournerait sur
    la clé du worker (ou s'arrêterait, `raison_sans_acces`) ; il est défait."""
    from oto_mcp import credentials_store
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    row = _prendre_sur_la_plateforme(oid, "w-retire")
    credentials_store.platform_revoke(_API, f"org:{oid}")

    assert _remettre(row, "w-retire") is None, "rien n'est servi"
    _defait(jid, "pas accordée")


def test_une_cle_deposee_depuis_la_prise_DEFAIT_le_repli(cle_de_plateforme):
    """Servi sur la clé neuve de l'org, le stamp mentirait : défait, et le sondage
    suivant le rejoue sur elle."""
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    row = _prendre_sur_la_plateforme(oid, "w-depot")
    _cle_org(oid)

    assert _remettre(row, "w-depot") is None
    _defait(jid, "déposé sa clé")
    from oto_mcp.db._conn import _connect
    with _connect() as conn:   # le défaire le rend dans 60 s : on avance l'horloge
        conn.execute("UPDATE runner_jobs SET due_at = NOW() WHERE id = %s", (jid,))
    rejoue = _reserver([oid])
    assert rejoue["id"] == jid and rejoue["model_key"] == "sk-ant-api03-de-l-org"
    assert _etat(jid)["payload"]["_plateforme"]["repli"]["payeur"] == "cle_org"


@pytest.mark.parametrize("kw, mot", [({"metre": False}, "mesure"),
                                     ({"ferme": True}, "ferme")])
def test_un_worker_qui_ne_peut_pas_servir_DEFAIT_au_lieu_d_arreter(cle_de_plateforme,
                                                                    kw, mot):
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    row = _prendre_sur_la_plateforme(oid, f"w-{mot}")

    assert _remettre(row, f"w-{mot}", **kw) is None
    _defait(jid, mot)


def test_une_instance_qui_retire_la_permission_DEFAIT_a_la_remise(cle_de_plateforme,
                                                                   permis):
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    row = _prendre_sur_la_plateforme(oid, "w-permis")
    permis("0")

    assert _remettre(row, "w-permis") is None
    _defait(jid, "ne permet plus")


def test_sous_le_plancher_de_credits_DEFAIT_au_lieu_d_arreter(cle_de_plateforme,
                                                              monkeypatch):
    """Un travail ordinaire sur la clé partagée s'ARRÊTE sous le plancher ; replié, il
    avait de quoi attendre son forfait : défait. Et la prise ne le prend même pas."""
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    row = _prendre_sur_la_plateforme(oid, "w-plancher")
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    monkeypatch.setenv("OTO_CREDITS_JOB_FLOOR", "1000")

    assert _remettre(row, "w-plancher") is None
    _defait(jid, "plancher")
    assert _reserver([oid]) is None, "sous le plancher, la prise ne le reprend pas"
    assert _etat(jid)["status"] == "pending"


# ── 6. LA PREUVE DE FACTURATION ─────────────────────────────────────────────

@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")


def test_PREUVE_le_run_replie_est_compte_cle_plateforme_pour_l_org_du_travail(
        cle_de_plateforme, enforce):
    """De bout en bout, par les verbes du worker (`claim`, `reserve`, `meter`,
    `complete`), sans rien doubler de ce qui facture :

    - le grand livre de crédits porte le règlement du tour au payeur `org:<org du
      travail>`, rang `platform`, rapproché du travail (`runner_job_id`) — c'est ce que
      le commerce facture ;
    - le relevé des jetons de la clé de plateforme (`jetons_du_jour`, ce que
      `platform_model_spend` lit) les compte sous le rang `platform` ;
    - la lentille `org.usage.tokens` lit le travail conclu `cle_plateforme` — sans que
      le worker l'ait déclaré : le payeur est celui que la PRISE a stampé."""
    from oto_mcp import db
    from oto_mcp.access import credits_tours as tours
    from oto_mcp.capabilities import org_monitoring as OM
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.db import credits as db_credits
    from oto_mcp.db._conn import _connect

    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    payeur = f"org:{oid}"
    db_credits.verser(payeur, 100_000, "banc", uuid.uuid4().hex)
    avant = db_credits.solde(payeur)["balance"]

    job = _reserver([oid])
    assert job["id"] == jid and job["model_key"] == cle_de_plateforme
    assert job["meter_usage"] is True

    worker = ResolvedCtx(sub=_WORKER, org_id=None, platform_worker=True)
    r = RJ._jobs(worker, RJ.JobsInput(op="reserve", job_id=jid, turn=0, max_tokens=2000))
    assert (r["held"], r["reserved"]) == (True, 200), r
    m = RJ._jobs(worker, RJ.JobsInput(op="meter", job_id=jid, turn=0, result={
        "model": "claude-sonnet-5-5", "usage_input": 1200, "usage_output": 300,
        "usage_cache_write": 0, "usage_cache_read": 500}))
    assert m["metered"] is True and m["amount"] == 120 + 30 + 0 + 50, m
    fin = RJ._jobs(worker, RJ.JobsInput(op="complete", job_id=jid, ok=True, result={
        "usage_input": 1200, "usage_output": 300, "usage_cache_read": 500,
        "usage_cache_write": 0, "model": "claude-sonnet-5-5"}))
    assert fin["status"] == "done"

    # Le grand livre : l'org du travail paie, au rang de la clé de plateforme.
    with _connect() as conn:
        mouvements = conn.execute(
            "SELECT payer, kind, amount, function, meta FROM credit_movements "
            "WHERE kind = 'settle' AND (meta->>'runner_job_id')::bigint = %s",
            (jid,)).fetchall()
    assert len(mouvements) == 1, mouvements
    (mv,) = mouvements
    assert mv["payer"] == payeur, "compté pour l'ORG du travail"
    assert mv["meta"]["key_mode"] == "platform", "au rang de la clé de plateforme"
    assert mv["function"] == "messages:claude-sonnet-5-5"
    assert mv["meta"]["tokens"]["output_tokens"] == 300
    assert db_credits.solde(payeur)["balance"] == avant - 200

    # Le relevé de la clé de plateforme (ce que `platform_model_spend` rapproche de
    # l'Admin API d'Anthropic, `anthropic_usage`).
    releve = db_credits.jetons_du_jour(tours.MODELE_CONNECTOR, tours.PREFIXE_DES_MODELES,
                                       "platform", datetime.now(timezone.utc).date(),
                                       tours.UNITES_DU_MODELE)
    assert releve["messages:claude-sonnet-5-5"]["output_tokens"] >= 300

    # La lentille d'usage : le travail conclu, payé par la clé de plateforme.
    lignes = [l for l in db.jetons_des_travaux(oid, 1)]
    assert [(l["paye_par"], l["modele"], l["travaux"], l["sortie"]) for l in lignes] == [
        ("cle_plateforme", "claude-sonnet-5-5", 1, 300)], lignes
    lu = OM._tokens(ResolvedCtx(sub=f"{n}-admin", org_id=oid),
                    OM.OrgTokensInput(org_id=oid, days=1))
    assert [(r["paid_by"], r["jobs"]) for r in lu["rows"]] == [("cle_plateforme", 1)]


def test_un_repli_sur_la_cle_de_l_org_se_lit_cle_org_a_la_lentille(cle_de_plateforme):
    """Le même stamp, l'autre payeur : sa clé, rien au grand livre."""
    from oto_mcp import db
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.db._conn import _connect
    n = _nom()
    oid = _org(n, f"{n}-dem")
    _abonne(f"{n}-dem")
    jid = _travail(oid, f"{n}-dem")
    _cle_org(oid)

    assert _reserver([oid])["id"] == jid
    RJ._jobs(ResolvedCtx(sub=_WORKER, org_id=None, platform_worker=True),
             RJ.JobsInput(op="complete", job_id=jid, ok=True,
                          result={"usage_input": 10, "usage_output": 5}))

    assert [l["paye_par"] for l in db.jetons_des_travaux(oid, 1)] == ["cle_org"]
    with _connect() as conn:
        assert conn.execute("SELECT 1 FROM credit_movements WHERE "
                            "(meta->>'runner_job_id')::bigint = %s", (jid,)).fetchone() is None


# ── le réglage lui-même ─────────────────────────────────────────────────────

def test_le_reglage_dit_qui_et_quand_sans_toucher_au_mode(live):
    from oto_mcp.db import org_subscription_pool as P
    n = _nom()
    oid = _org(n, plateforme=False, repli=False, accordee=False)

    ligne = P.set_repli_cle_plateforme(oid, _F, True, f"{n}-admin")

    assert ligne["repli_cle_plateforme"] is True
    assert ligne["repli_cle_plateforme_by"] == f"{n}-admin"
    assert ligne["repli_cle_plateforme_at"] is not None
    assert ligne["mode"] is None, "le pool continue de compléter"
    lu = P.get_mode(oid, _F)
    assert lu["updated_by"] is None, "`updated_by` appartient au mode"


@pytest.mark.parametrize("declare, attendu", [(None, False), ("1", True)])
def test_une_ligne_nee_du_reglage_suit_le_defaut_de_repli_de_l_instance(
        live, monkeypatch, declare, attendu):
    """Née ici, la ligne n'a rien choisi du repli API : elle naît au défaut de
    l'instance. Naître à `FALSE` sur une instance ouverte FERMERAIT le repli."""
    from oto_mcp.db import org_subscription_pool as P
    if declare:
        monkeypatch.setenv("OTO_REPLI_API_PAR_DEFAUT", declare)
    n = _nom()
    oid = _org(n, plateforme=False, repli=False, accordee=False)

    P.set_repli_cle_plateforme(oid, _F, True, f"{n}-admin")

    assert P.get_mode(oid, _F)["repli_api"] is attendu
    assert P.repli_api_actif(oid, _F) is attendu


def test_le_reglage_ne_touche_pas_au_repli_api_deja_regle(live):
    from oto_mcp.db import org_subscription_pool as P
    n = _nom()
    oid = _org(n, repli=False, plateforme=False, accordee=False)
    P.set_repli_api(oid, _F, False, f"{n}-admin")

    P.set_repli_cle_plateforme(oid, _F, True, f"{n}-admin")
    P.set_repli_cle_plateforme(oid, _F, False, f"{n}-admin")

    ligne = P.get_mode(oid, _F)
    assert (ligne["repli_api"], ligne["repli_cle_plateforme"]) == (False, False)
