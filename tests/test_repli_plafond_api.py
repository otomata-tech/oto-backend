"""Le REPLI plafond → clé API, EN BASE (OTO-130, 27/09/2026 ; décisions du 28/09).

Un travail d'abonnement dont le SEUL obstacle est un forfait ÉPUISÉ à échéance
FUTURE peut rejouer sur la clé API de son org plutôt qu'attendre — si l'org l'a
ne l'a pas COUPÉ (`repli_api`, ouvert par défaut depuis le 08/10/2026). Ce que ces bancs tiennent, contre une vraie
base :

1. forfait épuisé (mode personnel) → repli, avec le stamp `_plateforme.repli` ;
2. `needs_login` / `disconnected` → JAMAIS de repli (l'abonnement doit se
   reconnecter, ce n'est pas ce que ce chemin répare) ;
3. pool VIDE → jamais de repli (personne ne prête, ce n'est pas une pause) ;
4. pool INTÉGRALEMENT plafonné (échéance future) → repli, stamp `mode: pool` ;
5. pool MÉLANGÉ (un plafonné, un déconnecté) → jamais de repli (pas
   « intégralement » en pause) ;
6. clé API exigée et NON déposée → jamais de repli, jamais un échec dur : le
   travail reste `pending` ;
7. aucune clé DÉPOSÉE par l'org → jamais de repli : ni la clé de plateforme, ni
   celle du worker ne paient ; la clé déposée suffit, sans autre réglage ;
8. un dépôt qui n'a pas d'équivalent d'abonnement (`mistral`) → jamais de repli ;
9. à 16. les décisions du 28/09 : repli FERMÉ par défaut, déclenché par
   l'épuisement réel ou le seuil de l'org du TRAVAIL (jamais celui d'une autre org),
   borné en jetons et à un repli en vol par forfait ; Haiku part sans effort.

Le mode personnel et le mode pool ont leurs bancs de RÉSERVATION ordinaire
ailleurs (`test_abonnement_personnel*.py`, `test_pool_abonnements_db.py`),
inchangés : ce fichier ne couvre que le CHEMIN NEUF.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_F = "claude_subscription"
_API = "anthropic"


@pytest.fixture(scope="module", autouse=True)
def _cle_maitre():
    """Le coffre chiffre les clés déposées : une clé maîtresse de banc, remise à la
    sortie. La base, elle, vient de `live` (conftest, `pg_module_dsn`)."""
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "5" * 64
    yield
    if avant is None:
        os.environ.pop("OTO_MCP_MASTER_KEY", None)
    else:
        os.environ["OTO_MCP_MASTER_KEY"] = avant


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _org(nom, *membres, mode=None, repli=True, limite_pct=None):
    """Une org, ses membres, son mode — et le repli OUVERT explicitement dans ces
    bancs (`repli=False` : aucun réglage, le défaut de l'INSTANCE s'applique)."""
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_limits as L
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
    if limite_pct is not None:
        L.poser_limite(oid, _F, limite_pct, admin)
    return oid


def _abonne(sub, *, statut="connected", reset=None, pret_a=(), epuise=True,
            utilisation=None):
    """Un abonné et son état. Un `paused_limit` est par défaut un forfait ÉPUISÉ
    (refus du fournisseur, `epuise=True`) ; `epuise=False, utilisation=…` fait une
    pause posée par un SEUIL d'org."""
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected",
                      epuise=epuise, utilisation=utilisation)
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _ouvrir(sub, org, famille=_F):
    """L'abonnement personnel de `sub` OUVERT dans `org` (08/10/2026)."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO user_model_subscription_orgs (sub, famille, org_id) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sub, famille, org))


def _travail(org, sub, model="sub:sonnet", famille=_F, ouvert=True):
    """Un travail de `sub` dans `org` — son abonnement y est ouvert, sauf
    `ouvert=False` (alors il ATTEND, et ne se replie pas)."""
    from oto_mcp import db
    if ouvert:
        _ouvrir(sub, org, famille)
    return db.enqueue_job(org, "start", sub=sub,
                          payload={"procedure": "p", "model": model,
                                   "model_family": famille})["id"]


def _cle_org(org_id, *, connector=_API, exigee=True, deposee=True):
    from oto_mcp.db import connector_settings as CS
    from oto_mcp import credentials_store as CR
    if exigee:
        CS.set_connector_setting("org", str(org_id), connector,
                                 "runner.org_key_required", "true")
    if deposee:
        CR.set_credential("org", str(org_id), connector, "sk-test-secret")


def _repli(org, *, worker="w-api", depot=_API, org_ids=None, ferme=False):
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement
    return db.repli_disponible(org, org_ids, worker, depot,
                               _abonnement.DEFAUT_LIMITE_PCT, lease_seconds=60,
                               ferme=ferme)


def _etat(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute("SELECT status, attempts, payload FROM runner_jobs "
                                 "WHERE id = %s", (job_id,)).fetchone())


def _futur(heures=1):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


# ── 1. plafond personnel futur → repli ───────────────────────────────────────

def test_repli_personnel_plafond_futur(live):
    oid = _org("p1", "p1-dem")
    _abonne("p1-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p1-dem")
    _cle_org(oid)

    row = _repli(oid)

    assert row is not None, "un plafond FUTUR, sans autre obstacle, se replie"
    assert row["id"] == jid
    assert row["payload"]["model"] == "claude-sonnet-5"
    assert row["payload"]["model_family"] == "anthropic"
    repli = row["payload"]["_plateforme"]["repli"]
    assert repli["from_model"] == "sub:sonnet"
    assert repli["from_family"] == "claude_subscription"
    assert repli["to_model"] == "claude-sonnet-5"
    assert repli["to_family"] == "anthropic"
    assert repli["mode"] == "personnel"
    assert repli["reason"] == "paused_limit"
    assert repli["reset_at"] is not None
    assert _etat(jid)["status"] == "claimed"


def test_pas_de_repli_dans_une_org_ou_l_abonnement_n_est_pas_ouvert(live):
    """Revue du 08/10/2026 : ouvert dans A seulement, plafond atteint (par son usage
    dans A), un travail qui ATTEND dans B parce que l'abonnement n'y est pas ouvert
    ne part pas en repli sur la clé de B — il n'est pas bloqué par un plafond, il
    attend une ouverture. Dans A, le même plafond se replie."""
    oid_a = _org("p1o-a", "p1o-dem")
    oid_b = _org("p1o-b", "p1o-dem")
    _abonne("p1o-dem", statut="paused_limit", reset=_futur())
    _ouvrir("p1o-dem", oid_a)
    jid_b = _travail(oid_b, "p1o-dem", ouvert=False)
    _cle_org(oid_b)

    assert _repli(oid_b) is None, "B : l'abonnement n'y est pas ouvert, pas de repli"
    assert _etat(jid_b)["status"] == "pending"

    jid_a = _travail(oid_a, "p1o-dem")
    _cle_org(oid_a)
    row = _repli(oid_a)
    assert row is not None and row["id"] == jid_a, "A : ouvert, le plafond se replie"


def test_repli_personnel_plafond_ECHU_ne_replie_pas(live):
    """Une échéance PASSÉE n'est plus un plafond qui bloque : la réservation
    ordinaire sert déjà ce travail (`_abonnement.servable`) — le repli n'a rien
    à faire là, `repli_disponible` ne doit donc rien trouver à replier."""
    oid = _org("p1b", "p1b-dem")
    _abonne("p1b-dem", statut="paused_limit", reset=_futur(heures=-1))
    _travail(oid, "p1b-dem")
    _cle_org(oid)

    assert _repli(oid) is None


# ── 2. needs_login / disconnected → jamais de repli ─────────────────────────

@pytest.mark.parametrize("statut", ["needs_login", "disconnected"])
def test_pas_de_repli_needs_login_ou_disconnected(live, statut):
    oid = _org(f"p2-{statut}", f"p2-{statut}-dem")
    _abonne(f"p2-{statut}-dem", statut=statut)
    jid = _travail(oid, f"p2-{statut}-dem")
    _cle_org(oid)

    assert _repli(oid) is None, (
        f"`{statut}` demande une reconnexion, pas une clé API — jamais un repli")
    assert _etat(jid)["status"] == "pending", "le travail continue d'ATTENDRE"


# ── 3. pool vide → jamais de repli ──────────────────────────────────────────

def test_pas_de_repli_pool_vide(live):
    oid = _org("p3", "p3-dem", mode="pool")
    jid = _travail(oid, "p3-dem")
    _cle_org(oid)

    assert _repli(oid) is None, "personne ne prête : ce n'est pas une pause"
    assert _etat(jid)["status"] == "pending"


# ── 4. pool intégralement plafonné (échéance future) → repli ───────────────

def test_repli_pool_integralement_plafonne(live):
    oid = _org("p4", "p4-dem", "p4-preteur", mode="pool")
    _abonne("p4-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    jid = _travail(oid, "p4-dem")
    _cle_org(oid)

    row = _repli(oid)

    assert row is not None
    assert row["id"] == jid
    assert row["payload"]["model_family"] == "anthropic"
    repli = row["payload"]["_plateforme"]["repli"]
    assert repli["mode"] == "pool"
    assert repli["reason"] == "paused_limit"


def test_la_ferme_prend_le_repli_d_un_pool_plafonne_d_une_org_non_routee_avec_sa_cle(live):
    """Le worker de ferme sert le repli d'une org NON routée qui a sa clé d'org
    `anthropic` (21/10/2026) : c'est sur cette clé que le repli se paie."""
    oid = _org("p4f", "p4f-dem", "p4f-preteur", mode="pool")
    _abonne("p4f-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    jid = _travail(oid, "p4f-dem")
    _cle_org(oid)

    row = _repli(oid, worker="w-ferme", ferme=True)

    assert row is not None, "la ferme a laissé le repli d'une org qui a sa clé d'org"
    assert row["id"] == jid
    assert row["payload"]["model_family"] == "anthropic"


def test_la_ferme_ne_prend_pas_le_repli_d_une_org_non_routee_sans_cle(live):
    oid = _org("p4g", "p4g-dem", "p4g-preteur", mode="pool")
    _abonne("p4g-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    jid = _travail(oid, "p4g-dem")

    assert _repli(oid, worker="w-ferme", ferme=True) is None
    etat = _etat(jid)
    assert (etat["status"], etat["attempts"]) == ("pending", 0)


# ── 5. pool mélangé (un plafonné, un déconnecté) → jamais de repli ─────────

def test_pas_de_repli_pool_melange(live):
    oid = _org("p5", "p5-dem", "p5-a", "p5-b", mode="pool")
    _abonne("p5-a", statut="paused_limit", reset=_futur(), pret_a=[oid])
    _abonne("p5-b", statut="disconnected", pret_a=[oid])
    jid = _travail(oid, "p5-dem")
    _cle_org(oid)

    assert _repli(oid) is None, (
        "un prêteur DÉCONNECTÉ n'est pas un prêteur en pause : le pool n'est pas "
        "\"intégralement\" plafonné, et l'état du demandeur ne compte jamais ici")
    assert _etat(jid)["status"] == "pending"


# ── 6. clé exigée et non déposée → jamais de repli, jamais un échec dur ────

def test_pas_de_repli_sans_cle_deposee(live):
    oid = _org("p6", "p6-dem")
    _abonne("p6-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p6-dem")
    _cle_org(oid, exigee=True, deposee=False)

    assert _repli(oid) is None, (
        "une clé EXIGÉE et absente arrêterait le travail à `_avec_cle` — le repli "
        "doit se voir refuser la clé AVANT de prendre, pas après")
    etat = _etat(jid)
    assert etat["status"] == "pending", "jamais un échec dur : le travail attend encore"


# ── 7. aucune clé déposée par l'org → jamais de repli ──────────────────────

def test_pas_de_repli_sans_cle_deposee_par_l_org(live):
    """Une org qui n'a RIEN déposé ne se fait pas déplacer sa dépense : le travail
    continue d'ATTENDRE la réinitialisation du forfait.

    ⚠️ C'est le cœur de la règle, et l'inverse de ce que faisait la première
    version : elle lisait `runner.org_key_required` (à `false` par défaut) et en
    concluait « aucune clé exigée, donc rien à vérifier » — or « non exigée » veut
    dire « la clé d'ENV du worker fera l'affaire », c'est-à-dire que la PLATEFORME
    aurait payé le repli de toutes les orgs sans clé. Un travail posé sur un
    abonnement est gratuit pour son org ; le déplacer vers des jetons facturés
    n'est légitime que si cette org a elle-même dit « je paie », et la seule façon
    de l'avoir dit est d'avoir déposé SA clé."""
    oid = _org("p7", "p7-dem")
    _abonne("p7-dem", statut="paused_limit", reset=_futur())
    _travail(oid, "p7-dem")
    # Aucun `_cle_org` : rien n'est réglé, rien n'est déposé.

    assert _repli(oid) is None, "sans clé de l'org, on attend — on ne dépense pas"


def test_repli_sur_la_seule_cle_deposee_sans_reglage(live):
    """Et l'inverse : la clé DÉPOSÉE suffit, sans avoir à régler quoi que ce soit.
    `runner.org_key_required` ne participe plus à cette décision — il répond à
    « qui peut tourner ? », pas à « qui PAIE ? »."""
    oid = _org("p7b", "p7b-dem")
    _abonne("p7b-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p7b-dem")
    _cle_org(oid, exigee=False, deposee=True)

    row = _repli(oid)

    assert row is not None, "une clé déposée, même sans réglage, paie le repli"
    assert row["id"] == jid


# ── 8. un dépôt sans équivalent d'abonnement → jamais de repli ─────────────

def test_pas_de_repli_pour_un_depot_sans_equivalent(live):
    oid = _org("p8", "p8-dem")
    _abonne("p8-dem", statut="paused_limit", reset=_futur())
    _travail(oid, "p8-dem")
    _cle_org(oid, connector="mistral")

    assert _repli(oid, depot="mistral") is None, (
        "aucun modèle `claude_subscription` n'a d'équivalent `mistral`")


# ── 9. le repli ne casse pas la sérialisation ni le rapport de forfait ─────

def test_apres_repli_le_travail_n_est_plus_lu_comme_un_abonnement(live):
    """Une fois reroute, `porteur_et_famille` doit voir `anthropic` — sinon le
    rapport de forfait (`_abonnement.noter_rapport`) lèverait la pause d'un
    abonnement qui n'a pourtant pas tourné (OTO-130, garde du 27/09/2026)."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement

    oid = _org("p9", "p9-dem")
    _abonne("p9-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p9-dem")
    _cle_org(oid)

    row = _repli(oid)
    assert row is not None

    conclu = db.porteur_et_famille(jid)
    assert conclu["model_family"] == "anthropic"
    assert not _abonnement.est_abonnement(conclu["model_family"])


# ── 10. l'interrupteur d'org, et le défaut de l'INSTANCE ────────────────────

@pytest.fixture
def defaut_instance(monkeypatch):
    """Pose (ou retire) `OTO_REPLI_API_PAR_DEFAUT` pour un banc."""
    def poser(valeur):
        if valeur is None:
            monkeypatch.delenv("OTO_REPLI_API_PAR_DEFAUT", raising=False)
        else:
            monkeypatch.setenv("OTO_REPLI_API_PAR_DEFAUT", valeur)
    return poser


def test_sans_declaration_d_instance_une_org_sans_reglage_attend(live, defaut_instance):
    """Décision du 08/10/2026 : le défaut est un choix de l'instance. Rien de déclaré,
    aucune ligne de mode : le repli est FERMÉ — une clé déposée pour des agents API ne
    paie pas des travaux d'abonnement sans qu'on l'ait demandé."""
    from oto_mcp.db import org_subscription_pool as P
    defaut_instance(None)

    oid = _org("p10", "p10-dem", repli=False)
    _abonne("p10-dem", statut="paused_limit", reset=_futur())
    _travail(oid, "p10-dem")
    _cle_org(oid)

    assert P.repli_api_actif(oid, "claude_subscription") is False
    assert _repli(oid) is None, "défaut d'instance fermé : on attend le forfait"


def test_une_instance_qui_le_declare_ouvre_le_repli_sans_reglage(live, defaut_instance):
    """Sur une instance qui le déclare, une org sans réglage replie sur sa clé — la
    même règle pour l'écran et pour la réservation."""
    from oto_mcp.db import org_subscription_pool as P
    defaut_instance("yes")

    oid = _org("p10o", "p10o-dem", repli=False)
    _abonne("p10o-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p10o-dem")
    _cle_org(oid)

    assert P.repli_api_actif(oid, "claude_subscription") is True
    row = _repli(oid)
    assert row is not None and row["id"] == jid, "ouvert par l'instance : la clé de l'org paie"


@pytest.mark.parametrize("declare", [None, "1"])
def test_une_ligne_nee_du_mode_suit_le_defaut_de_l_instance(live, defaut_instance, declare):
    """Régler le MODE fait naître la ligne : elle n'a rien choisi du repli, elle naît
    donc au défaut de l'instance — passer en pool ne doit ni ouvrir ni fermer le repli
    par effet de bord."""
    from oto_mcp.db import org_subscription_pool as P
    defaut_instance(declare)

    oid = _org(f"p10b{declare}", f"p10b{declare}-dem", mode="pool", repli=False)

    attendu = declare is not None
    assert P.get_mode(oid, "claude_subscription")["repli_api"] is attendu
    assert P.repli_api_actif(oid, "claude_subscription") is attendu


@pytest.mark.parametrize("declare", [None, "on"])
def test_l_org_qui_coupe_le_repli_attend_son_forfait(live, defaut_instance, declare):
    """L'org a une clé, et refuse quand même qu'on la dépense : ses travaux
    plafonnés ATTENDENT la réinitialisation, quel que soit le défaut de l'instance.
    La clé dit « je peux payer », pas « je veux payer ici »."""
    from oto_mcp.db import org_subscription_pool as P
    defaut_instance(declare)

    oid = _org(f"p11{declare}", f"p11{declare}-dem")
    _abonne(f"p11{declare}-dem", statut="paused_limit", reset=_futur())
    _travail(oid, f"p11{declare}-dem")
    _cle_org(oid)
    P.set_repli_api(oid, "claude_subscription", False, f"p11{declare}-dem")

    assert P.repli_api_actif(oid, "claude_subscription") is False
    assert _repli(oid) is None, "repli coupé : on attend le forfait, on ne dépense pas"


@pytest.mark.parametrize("illisible", ["oui", "2", "enabled"])
def test_un_defaut_d_instance_illisible_leve(defaut_instance, illisible):
    """Un `yes` mal écrit ne tranche pas en silence : il lève, en nommant la
    variable."""
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.interrupteurs import InterrupteurIllisible
    defaut_instance(illisible)

    with pytest.raises(InterrupteurIllisible, match="OTO_REPLI_API_PAR_DEFAUT"):
        P.repli_api_par_defaut()


def test_couper_le_repli_ne_change_pas_le_mode(live):
    """Couper le repli sur une org qui n'a jamais réglé de mode fait naître la
    ligne SANS mode (19/10/2026) : l'interrupteur ne bascule pas l'org en pool, et
    il ne la FIGE pas non plus en personnel — le pool continue de compléter."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p12", "p12-dem", repli=False)
    assert P.get_mode(oid, "claude_subscription") is None

    P.set_repli_api(oid, "claude_subscription", False, "p12-dem")

    assert P.en_pool(oid, "claude_subscription") is False
    assert P.get_mode(oid, "claude_subscription")["mode"] is None


def test_le_repli_se_rouvre(live):
    """Et se rouvre : l'interrupteur n'est pas un aller simple."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p13", "p13-dem")
    _abonne("p13-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p13-dem")
    _cle_org(oid)
    P.set_repli_api(oid, "claude_subscription", False, "p13-dem")
    assert _repli(oid) is None

    P.set_repli_api(oid, "claude_subscription", True, "p13-dem")

    row = _repli(oid)
    assert row is not None and row["id"] == jid


# ── 11. de bout en bout, par le CHEMIN DE RÉSERVATION réel ────────────────

def test_le_travail_replie_est_SERVI_avec_la_cle_de_l_org(live):
    """La garantie que tout le reste promet, prise par le vrai chemin : un worker de
    plateforme qui réclame `anthropic` reçoit le travail d'abonnement plafonné,
    reroute, **avec la clé que l'ORG a déposée**.

    Les autres bancs appellent `db.repli_disponible` en direct : ils prouvent la
    règle, pas le câblage. Celui-ci passe par `capabilities.runner_jobs._jobs`, donc
    par la garde `ctx.platform_worker and inp.provider` et par la remise de clé — le
    seul endroit où « la clé du client » cesse d'être une intention pour devenir ce
    que le worker tient dans la main."""
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx

    oid = _org("p14", "p14-dem")
    _abonne("p14-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p14-dem")
    _cle_org(oid)  # dépôt `anthropic` de CETTE org : "sk-test-secret"

    rendu = RJ._jobs(ResolvedCtx(sub="worker:banc", org_id=None, platform_worker=True),
                     RJ.JobsInput(op="claim", provider=_API))

    job = rendu["job"]
    assert job is not None, "le repli doit passer par le chemin de réservation réel"
    assert job["id"] == jid
    assert job["payload"]["model_family"] == "anthropic"
    assert job["model_key"] == "sk-test-secret", (
        "servi avec la clé DÉPOSÉE PAR L'ORG — c'est toute la promesse du lot")


def test_sans_cle_de_l_org_le_chemin_reel_ne_sert_RIEN(live):
    """Et son négatif, par le même chemin : pas de clé d'org, pas de service. Le
    travail reste en file, il n'est ni servi sur la clé d'un autre, ni arrêté."""
    from oto_mcp import db
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx

    oid = _org("p15", "p15-dem")
    _abonne("p15-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p15-dem")
    # Aucun dépôt pour cette org.

    rendu = RJ._jobs(ResolvedCtx(sub="worker:banc", org_id=None, platform_worker=True),
                     RJ.JobsInput(op="claim", provider=_API))

    assert rendu["job"] is None
    assert db.get_job(jid, oid)["status"] == "pending", "il attend, il n'est pas arrêté"


# ── 12. la clé qui disparaît ENTRE la décision et la remise ───────────────

def test_une_cle_illisible_a_la_remise_DEFAIT_le_repli(live, monkeypatch):
    """Le trou que la revue de déploiement a trouvé, et qui n'était PAS une course :
    le repli se décidait sur `has_credential` (présence de la ligne, sans déchiffrer)
    alors que la remise lit le coffre pour de vrai. Un coffre qui ne rend pas la clé
    laisse la ligne en place — donc « l'org paie » était décidé, puis la remise
    retombait sur la clé de la PLATEFORME. Nous, silencieusement.

    Ici on force ce désaccord (la remise ne trouve rien) et on exige la troisième
    issue : ni servi sur notre clé, ni arrêté — le repli est DÉFAIT et le travail
    rendu à la file, sur son abonnement, tel qu'il était."""
    from oto_mcp import db
    from oto_mcp.capabilities import runner_jobs as RJ
    from oto_mcp.capabilities._types import ResolvedCtx

    oid = _org("p16", "p16-dem")
    _abonne("p16-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p16-dem")
    _cle_org(oid)  # la LIGNE existe : le repli sera décidé

    # ... mais le coffre ne rend rien à la REMISE (master key indisponible, ligne
    # illisible) — sans toucher à la décision, qui lit le coffre de son côté.
    vraie_lecture = RJ._cle_de_modele
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: (None, None))

    rendu = RJ._jobs(ResolvedCtx(sub="worker:banc", org_id=None, platform_worker=True),
                     RJ.JobsInput(op="claim", provider=_API))

    assert rendu["job"] is None, "rien n'est servi — surtout pas sur notre clé"

    remis = db.get_job(jid, oid)
    assert remis["status"] == "pending", "rendu à la file, pas arrêté"
    assert remis["payload"]["model"] == "sub:sonnet", "son abonnement lui est rendu"
    assert remis["payload"]["model_family"] == "claude_subscription"
    assert "repli" not in remis["payload"]["_plateforme"], "le repli est défait"
    defait = remis["payload"]["_plateforme"]["repli_defait"]
    assert defait["to_model"] == "claude-sonnet-5", "mais il reste TRAÇABLE"
    assert "illisible" in defait["raison"]
    assert vraie_lecture is not None


def test_un_coffre_illisible_ne_DECIDE_plus_le_repli(live, monkeypatch):
    """Et la cause racine, fermée en amont : si le coffre ne rend pas la clé, le
    repli n'est même plus décidé. Le travail attend son forfait, sans être touché."""
    from oto_mcp import credentials_store

    oid = _org("p17", "p17-dem")
    _abonne("p17-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p17-dem")
    _cle_org(oid)

    def _coffre_muet(entity_type, entity_id, connector, *a, **k):
        raise RuntimeError("master key indisponible")
    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _coffre_muet)

    assert _repli(oid) is None, "un coffre muet ne fait pas rerouter"

    from oto_mcp import db
    reste = db.get_job(jid, oid)
    assert reste["status"] == "pending"
    assert reste["payload"]["model"] == "sub:sonnet", "jamais touché"


# ── 13. Haiku part sans effort, et le repli défait le lui retire (D3) ─────

def test_un_repli_haiku_emporte_la_charge_de_son_modele(live):
    """`claude-haiku-4-5` répond 400 à `output_config.effort` (mesuré le 14/09) : le
    catalogue le déclare `effort="none"`, et le travail replié doit l'emporter — la
    prise réécrit toute la charge du modèle d'arrivée, pas seulement son nom."""
    oid = _org("p18", "p18-dem")
    _abonne("p18-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p18-dem", model="sub:haiku")
    _cle_org(oid)

    row = _repli(oid)

    assert row is not None and row["id"] == jid
    assert row["payload"]["model"] == "claude-haiku-4-5"
    assert row["payload"]["effort"] == "none", "sans lui, Anthropic répond 400"


def test_defaire_un_repli_rend_la_charge_d_avant_a_l_octet(live):
    """Défaire rend le travail TEL QU'IL ÉTAIT : ni l'effort du modèle d'arrivée, ni
    le plafond de jetons posé par le repli ne restent sur un travail d'abonnement."""
    from oto_mcp import db

    oid = _org("p19", "p19-dem")
    _abonne("p19-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p19-dem", model="sub:haiku")
    _cle_org(oid)
    avant = _etat(jid)["payload"]

    assert _repli(oid, worker="w-p19") is not None
    assert db.defaire_le_repli(jid, "w-p19", "banc")

    apres = _etat(jid)["payload"]
    assert {k: v for k, v in apres.items() if k != "_plateforme"} == \
        {k: v for k, v in avant.items() if k != "_plateforme"}
    assert apres["_plateforme"]["repli_defait"]["raison"] == "banc"


# ── 14. un prêteur en pause SANS échéance n'est pas « tout plafonné » (D4) ─

def test_pool_avec_un_preteur_sans_echeance_ne_replie_pas(live):
    """Un prêteur `paused_limit` sans échéance est SERVABLE pour la réservation
    (`PRETEUR_SERVABLE`) : le pool n'est pas intégralement en pause, et `bool_and`
    ne doit pas lire son NULL comme un « oui »."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p20", "p20-dem", "p20-a", "p20-b", mode="pool")
    _abonne("p20-a", statut="paused_limit", reset=_futur(), pret_a=[oid])
    _abonne("p20-b", statut="paused_limit", reset=None, pret_a=[oid])
    jid = _travail(oid, "p20-dem")
    _cle_org(oid)

    assert P.taille_du_pool(oid, _F) == 1, "le prêteur sans échéance peut servir"
    assert _repli(oid) is None
    assert _etat(jid)["status"] == "pending"


# ── 15. le déclencheur : l'épuisement, ou le seuil de l'org du TRAVAIL ────

def test_une_pause_posee_par_le_seuil_d_une_AUTRE_org_ne_la_fait_pas_payer(live):
    """Décision du 28/09/2026. La pause est portée par la PERSONNE, pas par l'org :
    à 85 % d'usage, le seuil de 80 % de l'org A la met en pause — mais l'org B, qui
    tolère 95 %, a encore de la marge sur ce forfait. B ne paie pas le repli."""
    oid_a = _org("p21a", "p21-dem", limite_pct=80)
    oid_b = _org("p21b", "p21-dem", limite_pct=95)
    _abonne("p21-dem", statut="paused_limit", reset=_futur(), epuise=False,
            utilisation=0.85)
    jid_b = _travail(oid_b, "p21-dem")
    _cle_org(oid_b)

    assert _repli(oid_b) is None, "le seuil de A ne fait pas payer B"
    assert _etat(jid_b)["status"] == "pending"

    jid_a = _travail(oid_a, "p21-dem")
    _cle_org(oid_a)
    row = _repli(oid_a)
    assert row is not None and row["id"] == jid_a, "A a posé la pause : A peut replier"


def test_un_forfait_REELLEMENT_epuise_replie_dans_toute_org(live):
    """Refusé par le fournisseur : le forfait ne sert plus personne, quel que soit le
    seuil de l'org du travail."""
    oid = _org("p22", "p22-dem", limite_pct=100)
    _abonne("p22-dem", statut="paused_limit", reset=_futur(), epuise=True,
            utilisation=0.5)
    jid = _travail(oid, "p22-dem")
    _cle_org(oid)

    row = _repli(oid)
    assert row is not None and row["id"] == jid


def test_une_pause_d_avant_ces_colonnes_n_ouvre_aucun_repli(live):
    """Une pause écrite sans sa cause (avant la révision `0025`) : on ne sait pas qui
    l'a posée, donc on attend — jamais une dépense sur une supposition."""
    oid = _org("p23", "p23-dem")
    _abonne("p23-dem", statut="paused_limit", reset=_futur(), epuise=False,
            utilisation=None)
    _travail(oid, "p23-dem")
    _cle_org(oid)

    assert _repli(oid) is None


def test_la_cause_s_efface_avec_la_pause(live):
    """Un autre statut remet la cause à zéro : une vieille cause ne doit pas ressurgir
    à la pause suivante."""
    from oto_mcp.db import user_subscriptions as US
    from oto_mcp.db._conn import _connect

    _abonne("p24-dem", statut="paused_limit", reset=_futur(), epuise=True,
            utilisation=0.99)
    US.marquer_statut("p24-dem", _F, US.CONNECTE, ok=True)
    with _connect() as conn:
        ligne = conn.execute(
            "SELECT limit_epuise, limit_utilisation FROM user_model_subscriptions "
            "WHERE sub = %s", ("p24-dem",)).fetchone()
    assert (ligne["limit_epuise"], ligne["limit_utilisation"]) == (False, None)


def test_le_rapport_de_forfait_ecrit_la_cause(live):
    """`noter_rapport` distingue le refus du fournisseur (épuisé) du seuil de l'org."""
    from oto_mcp.capabilities import _abonnement
    from oto_mcp.db._conn import _connect

    oid = _org("p25", "p25-dem", limite_pct=80)
    _abonne("p25-dem")
    fin = int(_futur().timestamp())

    def cause():
        with _connect() as conn:
            return tuple(conn.execute(
                "SELECT statut, limit_epuise, limit_utilisation FROM "
                "user_model_subscriptions WHERE sub = %s", ("p25-dem",)).fetchone().values())

    _abonnement.noter_rapport(
        {"model_family": _F, "sub": "p25-dem", "org_id": oid}, True,
        {"abonnement": {"etat": "allowed", "fenetres": {
            "five_hour": {"utilization": 0.9, "resetsAt": fin},
            "seven_day": {"utilization": 0.3, "resetsAt": fin}}}})
    assert cause() == ("paused_limit", False, 0.9)

    _abonnement.noter_rapport(
        {"model_family": _F, "sub": "p25-dem", "org_id": oid}, False,
        {"abonnement": {"etat": "rejected", "fenetres": {
            "five_hour": {"utilization": 1.0, "resetsAt": fin}}}})
    assert cause() == ("paused_limit", True, 1.0)


# ── 16. la borne de coût : un plafond de jetons, un repli en vol par forfait ─

def test_un_travail_replie_part_avec_un_plafond_de_jetons(live):
    from oto_mcp import db

    oid = _org("p26", "p26-dem")
    _abonne("p26-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p26-dem")
    _cle_org(oid)

    row = _repli(oid)
    assert row is not None and row["id"] == jid
    assert row["payload"]["max_tokens"] == db.REPLI_MAX_TOKENS


def test_le_plafond_declare_par_l_agent_l_emporte(live):
    """Surchargeable : un agent qui a déclaré son `max_tokens` le garde."""
    from oto_mcp import db

    oid = _org("p27", "p27-dem")
    _abonne("p27-dem", statut="paused_limit", reset=_futur())
    _ouvrir("p27-dem", oid)
    jid = db.enqueue_job(oid, "start", sub="p27-dem",
                         payload={"procedure": "p", "model": "sub:sonnet",
                                  "model_family": _F, "max_tokens": 2_000_000})["id"]
    _cle_org(oid)

    row = _repli(oid)
    assert row is not None and row["id"] == jid
    assert row["payload"]["max_tokens"] == 2_000_000


def test_un_seul_repli_en_vol_par_forfait(live):
    """Le chemin abonnement ne sert qu'un travail à la fois par forfait
    (`_deja_en_vol`) ; son repli non plus — sinon l'org paierait autant de runs en
    parallèle qu'il y a de workers de plateforme."""
    oid = _org("p28", "p28-dem", "p28-autre")
    _abonne("p28-dem", statut="paused_limit", reset=_futur())
    _abonne("p28-autre", statut="paused_limit", reset=_futur())
    j1 = _travail(oid, "p28-dem")
    j2 = _travail(oid, "p28-dem")
    j3 = _travail(oid, "p28-autre")
    _cle_org(oid)

    premier = _repli(oid, worker="w-1")
    assert premier is not None and premier["id"] == j1
    second = _repli(oid, worker="w-2")
    assert second is not None and second["id"] == j3, (
        "le deuxième travail du MÊME forfait attend ; celui d'un autre forfait part")
    assert _repli(oid, worker="w-3") is None
    assert _etat(j2)["status"] == "pending"


def test_un_seul_repli_en_vol_par_pool(live):
    """En pool, le forfait est celui de l'org : un seul repli en vol pour tout le pool."""
    oid = _org("p29", "p29-a", "p29-b", "p29-preteur", mode="pool")
    _abonne("p29-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    j1 = _travail(oid, "p29-a")
    j2 = _travail(oid, "p29-b")
    _cle_org(oid)

    premier = _repli(oid, worker="w-1")
    assert premier is not None and premier["id"] == j1
    assert _repli(oid, worker="w-2") is None
    assert _etat(j2)["status"] == "pending"


# ── 17. la tête de file ne bloque pas une org qui peut payer (D6) ─────────

def test_des_travaux_anciens_sans_cle_ne_bloquent_pas_une_org_qui_paie(live):
    """Les candidats sont filtrés sur l'interrupteur et la présence d'une clé d'org
    AVANT la limite : 30 travaux plus anciens d'orgs sans clé ne doivent pas cacher
    celui d'une org qui peut payer."""
    from oto_mcp.db._conn import _connect

    muette = _org("p30m", *[f"p30m-{i}" for i in range(30)])
    for i in range(30):
        _abonne(f"p30m-{i}", statut="paused_limit", reset=_futur())
        _travail(muette, f"p30m-{i}")
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET due_at = NOW() - interval '1 hour' "
                     "WHERE org_id = %s", (muette,))
    payeuse = _org("p30p", "p30p-dem")
    _abonne("p30p-dem", statut="paused_limit", reset=_futur())
    jid = _travail(payeuse, "p30p-dem")
    _cle_org(payeuse)

    # Les deux orgs seules (`org_ids`) : les travaux laissés par les bancs voisins
    # ne s'en mêlent pas.
    row = _repli(None, org_ids=[muette, payeuse])
    assert row is not None and row["id"] == jid



# ── 18. sans mode réglé, le repli suit la voie du travail (revue du 19/10/2026) ─

def test_sans_mode_regle_le_repli_suit_la_voie_du_travail(live):
    """Le pool complète : un demandeur au plafond passe par le pool, et c'est le
    POOL qui doit être en pause pour replier (forfait de l'org) ; un prêteur libre
    sert le travail, aucun repli."""
    oid = _org("p31", "p31-dem", "p31-p")
    _abonne("p31-dem", statut="paused_limit", reset=_futur())
    _abonne("p31-p", pret_a=[oid])
    jid = _travail(oid, "p31-dem")
    _cle_org(oid)
    assert _repli(oid) is None, "un prêteur sert : on ne paie pas de jetons"

    _abonne("p31-p", statut="paused_limit", reset=_futur(), pret_a=[oid])
    row = _repli(oid)
    assert row is not None and row["id"] == jid
    repli = row["payload"]["_plateforme"]["repli"]
    assert (repli["mode"], repli["forfait"]) == ("pool", f"{_F}:org:{oid}")
