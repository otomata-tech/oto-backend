"""Les alertes de capacité d'une org (`alertes_capacite.py`), sur une vraie base, Slack et
courriel doublés.

Ce qu'on prouve, réglage d'instance allumé :

- **le constat** suit la réservation, pas l'âge : un pool dont l'unique prêteur est au
  plafond ouvre un incident APRÈS `DELAI_MIN`, pas avant ; un prêteur servable (même
  occupé), un travail que le repli rejouera (interrupteur ouvert, clé d'org lisible), un
  abonnement pas ouvert ici ne sont pas des incidents ; `needs_login` → `reconnexion`,
  un pool sans prêt → `pool_vide`, le propriétaire au plafond en voie personnelle →
  `plafond` ; un pool mêlé est `plafond`, comme le texte de péremption ; sans mode
  réglé, des prêteurs tous au plafond font un incident du pool ;
- **le cycle** : ouvert une fois, clos une passe après avoir cédé, avec les occurrences
  périmées pendant l'arrêt ; une passe qui tombe pendant la sonde d'un forfait ne clôt
  rien, et rien ne repart deux fois ; une org suspendue n'est ni libre ni close ; plus
  rien qui attend alors que la cause tient n'est pas une reprise ;
- **les envois** : une alerte par incident sur le Slack de l'org, sinon (Slack absent ou
  qui refuse) le courriel à l'admin ; rien de parti = rien de marqué, et le passage
  suivant rejoue ; le titulaire reçoit SON état, une fois ; la ligne de reprise ;
- **la discrétion** : le message à l'org ne porte jamais l'identité du prêteur (sub, nom,
  adresse, palier) ;
- éteint (absent, vide, non) : rien ; illisible : lève ; une org d'un tenant tiers : rien.

Et, sans base : les textes, en français et en anglais.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from oto_mcp import alertes_capacite as A

_F = "claude_subscription"
_API = "anthropic"
BASE = "https://app.example.test"
CHEMINS = json.dumps({"agent_models": "/org/{org}/agents/models?section={section}"})
#: Les orgs que le banc fait passer pour celles d'un tenant tiers.
_TIERS: set[int] = set()


@pytest.fixture(scope="module", autouse=True)
def _cle_maitre():
    """Le coffre chiffre les clés déposées (le repli les déchiffre) : une clé maîtresse
    de banc, remise à la sortie."""
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "5" * 64
    yield
    if avant is None:
        os.environ.pop("OTO_MCP_MASTER_KEY", None)
    else:
        os.environ["OTO_MCP_MASTER_KEY"] = avant


class _Slack:
    def __init__(self):
        self.posts, self.refuse = [], False

    def client(self, bot_token):
        slack = self

        class _Client:
            def post_message(self, channel, text, blocks=None):
                if slack.refuse:
                    raise RuntimeError("channel_not_found")
                slack.posts.append({"channel": channel, "text": text, "blocks": blocks})
                return {"ok": True}
        return _Client()


class _Courrier:
    def __init__(self):
        self.envois, self.refuse = [], set()

    def __call__(self, to, subject, html, **kw):
        if to in self.refuse:
            return False
        self.envois.append({"to": to, "subject": subject, "html": html})
        return True


@pytest.fixture(autouse=True)
def base(live, monkeypatch):
    """La base est celle du MODULE, et le constat lit TOUTE la file : chaque cas part
    d'une file et d'une table d'incidents vides. Réglage d'instance allumé, hors heures
    calmes ; Slack et courriel doublés."""
    from oto.tools.slack import client as slack_client
    from oto_mcp import alerte_agents_bloques, alertes_slack_agents
    from oto_mcp import email as E
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM runner_jobs")
        conn.execute("DELETE FROM org_incidents_capacite")
    _TIERS.clear()
    monkeypatch.setenv("OTO_ALERTES_CAPACITE_ENABLED", "1")
    monkeypatch.setenv("OTO_APP_URL", BASE)
    monkeypatch.setenv("OTO_DASHBOARD_LINK_PATHS", CHEMINS)
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous",
                        lambda org, qui: int(org) not in _TIERS)
    monkeypatch.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: False)
    s = _Slack()
    monkeypatch.setattr(slack_client, "SlackClient", s.client)
    monkeypatch.setattr(alertes_slack_agents, "bot_de_l_org",
                        lambda org: ("xoxb-test", "Acme"))
    monkeypatch.setattr(alertes_slack_agents, "langue", lambda org: "fr")
    monkeypatch.setattr(alertes_slack_agents, "marque_de_l_org",
                        lambda org: SimpleNamespace(nom="Acme", langue=""))
    c = _Courrier()
    monkeypatch.setattr(E, "_send", c)
    return SimpleNamespace(slack=s, courrier=c, env=monkeypatch)


# --- Les données ---------------------------------------------------------------------

def _personne(sub, email=None, nom=None):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub, email, name) VALUES (%s, %s, %s) "
                     "ON CONFLICT (sub) DO UPDATE SET email = EXCLUDED.email, "
                     "name = EXCLUDED.name", (sub, email, nom))
    return sub


def _org(*membres, mode=None, repli=False, admin_email=None):
    """Une org neuve, son admin (sans adresse, sauf `admin_email`), ses membres, son mode,
    son repli (FERMÉ par défaut dans ces bancs : l'interrupteur explicite)."""
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_pool as P
    nom = uuid.uuid4().hex[:8]
    admin = _personne(f"admin-{nom}", admin_email)
    oid = org_store.create_org(f"Org {nom}", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, m, "org_member")
    if mode:
        P.poser_mode(oid, _F, mode, admin)
    P.set_repli_api(oid, _F, bool(repli), admin)
    return oid


def _futur(heures=6):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


def _abonne(sub, *, statut="connected", reset=None, pret_a=(), email=None, nom=None):
    """Un abonné et son état ; un `paused_limit` est un forfait épuisé (refus du
    fournisseur)."""
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub, email, nom)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected",
                      epuise=True, plan="Max")
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _ouvrir(sub, org):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO user_model_subscription_orgs (sub, famille, org_id) "
                     "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sub, _F, org))


def _agent(org, sub, label="Liste d'appels"):
    from oto_mcp.db import runner_triggers as rt
    return rt.create_trigger(org, sub, procedure="appels", tz="UTC", tools=["a"],
                             cron="45 7 * * *", next_due="2999-01-01 00:00:00+00",
                             label=label)["id"]


def _travail(org, sub, *, agent=None, ouvert=True):
    from oto_mcp import db
    if ouvert:
        _ouvrir(sub, org)
    charge = {"procedure": "p", "model": "sub:sonnet", "model_family": _F}
    if agent:
        charge["trigger_id"] = agent
    return db.enqueue_job(org, "start", sub=sub, payload=charge)["id"]


def _cle_org(org):
    from oto_mcp import credentials_store as CR
    CR.set_credential("org", str(org), _API, "sk-test-secret")


def _vieillir(org, minutes):
    """Recule l'horloge de l'incident vivant de l'org (sa première passe, et sa reprise
    s'il a cédé) de `minutes`."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE org_incidents_capacite "
                     "SET vu_at = vu_at - make_interval(mins => %s), "
                     "    libre_at = libre_at - make_interval(mins => %s) "
                     "WHERE org_id = %s AND clos_at IS NULL", (minutes, minutes, org))


def _slack_de(org):
    from oto_mcp.db import alertes_slack
    alertes_slack.activer_a_la_connexion(org, dm_user="U0ADMIN", dm_name="Admin")


def _incident(org):
    from oto_mcp.db import incidents_capacite
    return incidents_capacite.dernier(org, _F)


def _pool_au_plafond(**kw):
    """Une org en pool dont l'unique prêteur est au plafond, un agent, un travail dû."""
    preteur = f"preteur-{uuid.uuid4().hex[:6]}"
    _personne(preteur)
    org = _org(preteur, mode="pool", **kw)
    _abonne(preteur, statut="paused_limit", reset=_futur(), pret_a=[org])
    demandeur = _personne(f"demandeur-{uuid.uuid4().hex[:6]}")
    from oto_mcp import org_store
    org_store.add_org_member(org, demandeur, "org_member")
    agent = _agent(org, demandeur)
    job = _travail(org, demandeur, agent=agent, ouvert=False)
    return SimpleNamespace(org=org, preteur=preteur, demandeur=demandeur, agent=agent,
                           job=job)


# --- Le constat ----------------------------------------------------------------------

def test_un_pool_au_plafond_s_ouvre_apres_le_delai_pas_avant(base):
    p = _pool_au_plafond()
    _slack_de(p.org)
    A.balayer()
    inc = _incident(p.org)
    assert inc["ouvert_at"] is None, "vu une fois : un candidat, pas encore un incident"
    assert (inc["cause"], inc["par_le_pool"], inc["agents"]) == ("plafond", True, 1)
    assert base.slack.posts == []
    _vieillir(p.org, A.DELAI_MIN - 2)
    A.balayer()
    assert _incident(p.org)["ouvert_at"] is None and base.slack.posts == []
    _vieillir(p.org, 3)
    assert A.balayer()["alertes"] == 1
    assert A.balayer()["alertes"] == 0, "une alerte par incident"
    (post,) = base.slack.posts
    assert post["channel"] == "U0ADMIN"
    assert "Vos agents ne démarrent plus" in post["text"]
    assert "1 agent allumé attend." in post["text"]
    assert "forfait partagé avec l'organisation est au plafond" in post["text"]
    assert "Ils repartiront seuls le " in post["text"]
    boutons = [b["url"] for b in post["blocks"][1]["elements"]]
    assert boutons == [f"{BASE}/org/{p.org}/agents/models?section=plans",
                       f"{BASE}/org/{p.org}/agents/models?section=key"]
    assert _incident(p.org)["alerte_par"] == "slack"


def test_un_preteur_servable_meme_occupe_n_est_pas_un_incident(base):
    """Une file d'une place derrière un forfait servable avance : pas une panne, quel que
    soit l'âge du travail qui attend son tour."""
    from oto_mcp.db._conn import _connect
    preteur = _personne(f"preteur-{uuid.uuid4().hex[:6]}")
    org = _org(preteur, mode="pool")
    _abonne(preteur, pret_a=[org])
    demandeur = _personne(f"demandeur-{uuid.uuid4().hex[:6]}")
    from oto_mcp import org_store
    org_store.add_org_member(org, demandeur, "org_member")
    en_vol, attend = (_travail(org, demandeur, ouvert=False) for _ in range(2))
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET status = 'claimed', claimed_by = 'w', "
                     "lease_until = NOW() + INTERVAL '10 minutes', "
                     "payload = payload || jsonb_build_object('_plateforme', "
                     "jsonb_build_object('abonnement', %s::text, 'pool', true)) "
                     "WHERE id = %s", (preteur, en_vol))
        conn.execute("UPDATE runner_jobs SET created_at = NOW() - INTERVAL '3 hours', "
                     "due_at = NOW() - INTERVAL '3 hours' WHERE id = %s", (attend,))
    A.balayer()
    assert _incident(org) is None


def test_un_travail_que_le_repli_rejoue_n_est_pas_un_incident(base):
    p = _pool_au_plafond(repli=True)
    _cle_org(p.org)
    A.balayer()
    assert _incident(p.org) is None


def test_repli_ouvert_sans_cle_d_org_reste_un_incident(base):
    """Le repli n'a rien sur quoi rejouer : c'est voulu (`_cle_ok_pour_repli`), et c'est
    exactement la panne que ce dispositif dit."""
    p = _pool_au_plafond(repli=True)
    A.balayer()
    assert _incident(p.org)["cause"] == "plafond"


def test_un_preteur_a_reconnecter_donne_reconnexion(base):
    preteur = _personne(f"preteur-{uuid.uuid4().hex[:6]}")
    org = _org(preteur, mode="pool")
    _abonne(preteur, statut="needs_login", pret_a=[org])
    _travail(org, preteur, ouvert=False)
    A.balayer()
    assert (_incident(org)["cause"], _incident(org)["par_le_pool"]) == ("reconnexion", True)


def test_un_pool_sans_pret_donne_pool_vide(base):
    membre = _personne(f"membre-{uuid.uuid4().hex[:6]}")
    org = _org(membre, mode="pool")
    _travail(org, membre, ouvert=False)
    A.balayer()
    assert _incident(org)["cause"] == "pool_vide"


def test_un_pool_mele_est_au_plafond_comme_le_texte_de_peremption(base):
    """Un prêteur au plafond et un à reconnecter : `plafond` (le classement de
    `_abonnement.cause_du_forfait`) — et chacun reçoit SON état par courriel."""
    au_plafond = f"plafond-{uuid.uuid4().hex[:6]}"
    a_reconnecter = f"reco-{uuid.uuid4().hex[:6]}"
    _personne(au_plafond, f"{au_plafond}@exemple.invalid")
    _personne(a_reconnecter, f"{a_reconnecter}@exemple.invalid")
    org = _org(au_plafond, a_reconnecter, mode="pool")
    _abonne(au_plafond, statut="paused_limit", reset=_futur(), pret_a=[org],
            email=f"{au_plafond}@exemple.invalid")
    _abonne(a_reconnecter, statut="needs_login", pret_a=[org],
            email=f"{a_reconnecter}@exemple.invalid")
    _travail(org, au_plafond, ouvert=False)
    A.balayer()
    _vieillir(org, A.DELAI_MIN + 1)
    A.balayer()
    assert _incident(org)["cause"] == "plafond"
    par_qui = {e["to"]: e for e in base.courrier.envois}
    assert "au plafond" in par_qui[f"{au_plafond}@exemple.invalid"]["subject"]
    assert "Reconnectez" in par_qui[f"{a_reconnecter}@exemple.invalid"]["subject"]


def test_mode_personnel_le_proprietaire_au_plafond(base):
    proprio = f"proprio-{uuid.uuid4().hex[:6]}"
    _personne(proprio, f"{proprio}@exemple.invalid")
    org = _org(proprio, mode="personnel")
    _abonne(proprio, statut="paused_limit", reset=_futur(3),
            email=f"{proprio}@exemple.invalid")
    _travail(org, proprio, agent=_agent(org, proprio))
    _slack_de(org)
    A.balayer()
    _vieillir(org, A.DELAI_MIN + 1)
    bilan = A.balayer()
    assert (bilan["alertes"], bilan["titulaires"]) == (1, 1)
    inc = _incident(org)
    assert (inc["cause"], inc["par_le_pool"]) == ("plafond", False)
    assert "Le forfait de leur propriétaire est au plafond" in base.slack.posts[0]["text"]
    (courriel,) = base.courrier.envois
    assert courriel["to"] == f"{proprio}@exemple.invalid"
    assert "Il se réinitialise le " in courriel["html"]
    assert f"{BASE}/org/{org}/agents/models?section=plans" in courriel["html"]
    A.balayer()
    assert len(base.courrier.envois) == 1, "un courriel par titulaire et par incident"


def test_un_abonnement_pas_ouvert_ici_n_est_pas_un_incident(base):
    """Il attend un geste d'un autre ordre (l'ouvrir dans l'org), que le texte de
    péremption nomme : ce n'est pas une panne de capacité."""
    proprio = _personne(f"proprio-{uuid.uuid4().hex[:6]}")
    org = _org(proprio, mode="personnel")
    _abonne(proprio, statut="needs_login")
    _travail(org, proprio, ouvert=False)
    A.balayer()
    assert _incident(org) is None


def test_le_travail_d_un_agent_eteint_ne_compte_pas(base):
    from oto_mcp.db._conn import _connect
    p = _pool_au_plafond()
    with _connect() as conn:
        conn.execute("UPDATE runner_triggers SET enabled = FALSE WHERE id = %s", (p.agent,))
    A.balayer()
    assert _incident(p.org) is None


# --- Le cycle ------------------------------------------------------------------------

def _ouvert(p):
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    A.balayer()
    assert _incident(p.org)["ouvert_at"] is not None


def _perimer(org, agent, il_y_a_min, famille=_F):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runner_jobs (org_id, kind, payload, status, sub, finished_at) "
            "VALUES (%s, 'start', %s::jsonb, 'expired', 'x', "
            "NOW() - make_interval(mins => %s))",
            (org, json.dumps({"trigger_id": agent, "model_family": famille}), il_y_a_min))


def test_le_cycle_ouvre_clot_et_compte_les_occurrences_manquees(base):
    from oto_mcp.db import user_subscriptions as US
    p = _pool_au_plafond()
    _slack_de(p.org)
    _ouvert(p)
    US.marquer_statut(p.preteur, _F, "connected", ok=True)
    A.balayer()
    inc = _incident(p.org)
    assert inc["libre_at"] is not None and inc["clos_at"] is None, (
        "une passe qui ne voit plus rien n'en conclut pas encore la reprise")
    assert len(base.slack.posts) == 1
    _vieillir(p.org, 6)
    # L'incident court de -23 min (vu) à -6 min (libre) : deux péremptions pendant, une
    # avant, une après, une d'une autre famille — seules les deux premières comptent.
    for il_y_a in (20, 10, 60, 2):
        _perimer(p.org, p.agent, il_y_a)
    _perimer(p.org, p.agent, 10, famille="anthropic")
    assert A.balayer()["reprises"] == 1
    inc = _incident(p.org)
    assert (inc["clos_at"], inc["perimees"]) == (inc["libre_at"], 2)
    heure = A._date(inc["clos_at"]).strftime("%H:%M")
    assert base.slack.posts[-1]["text"] == (
        f"✅ Les agents ont repris à {heure} UTC. 2 occurrences manquées pendant l'arrêt.")
    assert A.balayer()["reprises"] == 0
    assert len(base.slack.posts) == 2


def test_une_passe_pendant_la_sonde_ne_clot_rien(base):
    """La sonde prend le travail, trouve le forfait encore au plafond et le rend à la file
    à +`VERIFICATION_S` : il attend toujours, l'incident continue sans second message."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    p = _pool_au_plafond()
    _slack_de(p.org)
    _ouvert(p)
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET status = 'claimed', claimed_by = 'w-sonde', "
                     "attempts = attempts + 1, lease_until = NOW() + INTERVAL '10 minutes' "
                     "WHERE id = %s", (p.job,))
    A.balayer()
    assert _incident(p.org)["libre_at"] is not None, "pendant la sonde, rien n'attend"
    from oto_mcp.db import user_subscriptions as US
    assert db.rendre_a_la_file(p.job, "w-sonde", "au plafond",
                               delai_s=US.VERIFICATION_S)
    A.balayer()
    inc = _incident(p.org)
    assert (inc["libre_at"], inc["clos_at"]) == (None, None)
    _vieillir(p.org, 30)
    A.balayer()
    assert _incident(p.org)["clos_at"] is None
    assert len(base.slack.posts) == 1


def test_un_candidat_qui_cede_s_efface_sans_rien_dire(base):
    from oto_mcp.db import user_subscriptions as US
    p = _pool_au_plafond()
    _slack_de(p.org)
    A.balayer()
    US.marquer_statut(p.preteur, _F, "connected", ok=True)
    A.balayer()
    _vieillir(p.org, 6)
    assert A.balayer()["effaces"] == 1
    assert _incident(p.org) is None and base.slack.posts == []


def test_une_org_suspendue_n_est_ni_libre_ni_close(base):
    from oto_mcp import org_store
    p = _pool_au_plafond()
    _ouvert(p)
    org_store.suspend_org(p.org, by="ops", reason="banc")
    A.balayer()
    _vieillir(p.org, 30)
    A.balayer()
    inc = _incident(p.org)
    assert (inc["libre_at"], inc["clos_at"]) == (None, None)


# --- Les envois ----------------------------------------------------------------------

def test_slack_qui_refuse_sans_admin_joignable_ne_marque_rien_et_rejoue(base):
    p = _pool_au_plafond()
    _slack_de(p.org)
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    base.slack.refuse = True
    assert A.balayer()["rates"] == 1
    assert _incident(p.org)["alerte_at"] is None
    base.slack.refuse = False
    assert A.balayer()["alertes"] == 1
    assert len(base.slack.posts) == 1


def test_slack_qui_refuse_le_courriel_aux_admins_prend_le_relais(base):
    p = _pool_au_plafond(admin_email="admin-org@exemple.invalid")
    _slack_de(p.org)
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    base.slack.refuse = True
    assert A.balayer()["alertes"] == 1
    assert _incident(p.org)["alerte_par"] == "courriel"
    (courriel,) = [e for e in base.courrier.envois if e["to"] == "admin-org@exemple.invalid"]
    assert courriel["subject"] == "Vos agents ne démarrent plus"
    assert "au plafond de consommation" in courriel["html"]
    assert f"{BASE}/org/{p.org}/agents/models?section=key" in courriel["html"]


def test_sans_slack_regle_le_courriel_part_et_la_reprise_aussi(base):
    from oto_mcp.db import user_subscriptions as US
    p = _pool_au_plafond(admin_email="admin-org@exemple.invalid")
    _ouvert(p)
    US.marquer_statut(p.preteur, _F, "connected", ok=True)
    A.balayer()
    _vieillir(p.org, 6)
    A.balayer()
    a_l_admin = [e for e in base.courrier.envois if e["to"] == "admin-org@exemple.invalid"]
    assert [e["subject"] for e in a_l_admin] == ["Vos agents ne démarrent plus",
                                                "Vos agents ont repris"]
    assert "Aucune occurrence manquée" in a_l_admin[1]["html"]


def test_un_courriel_de_titulaire_rate_se_rejoue(base):
    p = _pool_au_plafond()
    _personne(p.preteur, f"{p.preteur}@exemple.invalid")
    base.courrier.refuse.add(f"{p.preteur}@exemple.invalid")
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    A.balayer()
    assert _incident(p.org)["titulaires_prevenus"] == []
    base.courrier.refuse.clear()
    assert A.balayer()["titulaires"] == 1
    assert _incident(p.org)["titulaires_prevenus"] == [p.preteur]


def test_le_message_a_l_org_ne_nomme_jamais_le_preteur(base):
    p = _pool_au_plafond(admin_email="admin-org@exemple.invalid")
    _personne(p.preteur, "preteuse-temoin@exemple.invalid", "Prêteuse Témoin")
    _slack_de(p.org)
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    A.balayer()
    base.slack.refuse = True          # la reprise part par courriel à l'admin
    from oto_mcp.db import user_subscriptions as US
    US.marquer_statut(p.preteur, _F, "connected", ok=True)
    A.balayer()
    _vieillir(p.org, 6)
    A.balayer()
    a_l_org = [json.dumps(post, ensure_ascii=False) for post in base.slack.posts]
    a_l_org += [e["subject"] + e["html"] for e in base.courrier.envois
                if e["to"] == "admin-org@exemple.invalid"]
    assert len(a_l_org) == 2
    for message in a_l_org:
        for trace in (p.preteur, "preteuse-temoin", "Prêteuse Témoin", "Max",
                      f"sandbox-{p.preteur}"):
            assert trace not in message, (trace, message)
    # Le titulaire, lui, reçoit SON état — à son adresse.
    assert [e["to"] for e in base.courrier.envois].count(
        "preteuse-temoin@exemple.invalid") == 1


# --- Le réglage, le tenant ----------------------------------------------------------

@pytest.mark.parametrize("valeur", ["absent", "", "0", "no", "off", "false"])
def test_eteint_la_boucle_n_est_pas_armee_et_rien_ne_s_ecrit(base, valeur):
    from oto_mcp import boucles_de_fond
    if valeur == "absent":
        base.env.delenv("OTO_ALERTES_CAPACITE_ENABLED")
    else:
        base.env.setenv("OTO_ALERTES_CAPACITE_ENABLED", valeur)
    p = _pool_au_plafond()
    assert boucles_de_fond._alertes_capacite_armee() is False
    with pytest.raises(A.AlertesCapaciteEteintes):
        A.balayer()
    assert _incident(p.org) is None


def test_allume_la_boucle_est_armee(base):
    from oto_mcp import boucles_de_fond
    assert boucles_de_fond._alertes_capacite_armee() is True


def test_une_valeur_illisible_leve(base):
    from oto_mcp import interrupteurs
    base.env.setenv("OTO_ALERTES_CAPACITE_ENABLED", "peut-etre")
    with pytest.raises(interrupteurs.InterrupteurIllisible):
        A.actives()


def test_une_org_d_un_tenant_tiers_est_ecartee(base):
    p = _pool_au_plafond()
    _slack_de(p.org)
    _TIERS.add(p.org)
    assert A.balayer()["tenant_tiers_ecartes"] == 1
    assert _incident(p.org) is None and base.slack.posts == []


def test_les_heures_calmes_retiennent_les_envois_pas_le_constat(base):
    from oto_mcp import alerte_agents_bloques
    p = _pool_au_plafond()
    _slack_de(p.org)
    base.env.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: True)
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    assert A.balayer()["heures_calmes"] is True
    assert _incident(p.org)["ouvert_at"] is not None and base.slack.posts == []
    base.env.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: False)
    assert A.balayer()["alertes"] == 1


# --- Les textes, sans base -----------------------------------------------------------

def _inc(**kw):
    return {"id": 1, "org_id": 77, "famille": _F, "cause": "plafond", "par_le_pool": True,
            "vu_at": "2026-11-19 13:05:00", "reprise_at": "2026-11-21 09:00:00",
            "agents": 3, "travaux": 4, "clos_at": None, "perimees": None, **kw}


@pytest.mark.parametrize("cause, pool, attendu", [
    ("plafond", True, "Le forfait partagé avec l'organisation est au plafond"),
    ("reconnexion", True, "doit être reconnecté par la personne qui le prête"),
    ("pool_vide", True, "Plus aucun forfait n'est partagé avec l'organisation."),
    ("plafond", False, "Le forfait de leur propriétaire est au plafond"),
    ("reconnexion", False, "Le forfait de leur propriétaire doit être reconnecté."),
])
def test_les_causes_en_francais(cause, pool, attendu):
    lignes = A.lignes_de_l_alerte(_inc(cause=cause, par_le_pool=pool), "fr", marque="Acme")
    assert lignes[0] == "⏸️ *Vos agents ne démarrent plus depuis le 19 nov. à 13:05 UTC*"
    assert lignes[1] == "3 agents allumés attendent."
    assert attendu in lignes[2]
    assert ("Ils repartiront seuls le 21 nov. à 09:00 UTC." in lignes) == (cause == "plafond")
    assert "Acme › Agents › Modèles" in lignes[-1]


def test_les_causes_en_anglais():
    lignes = A.lignes_de_l_alerte(_inc(agents=1), "en", marque="Acme")
    assert lignes == [
        "⏸️ *Your agents have not been able to start since Nov 19 at 13:05 UTC*",
        "1 enabled agent is waiting.",
        "The plan shared with the organization has reached its usage cap.",
        "They will restart on their own on Nov 21 at 09:00 UTC.",
        "To restart them sooner, in Acme › Agents › Models: share another plan with "
        "the organization, or run them on an API key."]
    assert A.lignes_de_l_alerte(_inc(cause="pool_vide", agents=0, travaux=1), "en",
                                marque="Acme")[1] == "1 run is waiting."


@pytest.mark.parametrize("langue_, n, attendu", [
    ("fr", 0, "✅ Les agents ont repris à 21:40 UTC. Aucune occurrence manquée pendant "
              "l'arrêt."),
    ("fr", 1, "✅ Les agents ont repris à 21:40 UTC. 1 occurrence manquée pendant l'arrêt."),
    ("fr", 5, "✅ Les agents ont repris à 21:40 UTC. 5 occurrences manquées pendant "
              "l'arrêt."),
    ("en", 0, "✅ Agents running again since 21:40 UTC. No run missed during the outage."),
    ("en", 2, "✅ Agents running again since 21:40 UTC. 2 runs missed during the outage."),
])
def test_la_ligne_de_reprise(langue_, n, attendu):
    assert A.ligne_de_reprise(_inc(clos_at="2026-11-19 21:40:00", perimees=n),
                              langue_) == attendu


def test_sans_page_des_modeles_declaree_aucun_bouton(monkeypatch):
    monkeypatch.setenv("OTO_APP_URL", BASE)
    monkeypatch.delenv("OTO_DASHBOARD_LINK_PATHS", raising=False)
    texte, blocs = A.composer_alerte(_inc(), "fr", marque="Acme")
    assert len(blocs) == 1 and "au plafond" in texte


def test_une_reconnexion_ne_propose_pas_de_cle_d_api(monkeypatch):
    """Le seul repli qui existe ne prend qu'un forfait au plafond : proposer une clé
    pour une reconnexion enverrait faire un geste qui ne relance rien."""
    monkeypatch.setenv("OTO_APP_URL", BASE)
    monkeypatch.setenv("OTO_DASHBOARD_LINK_PATHS", CHEMINS)
    _texte, blocs = A.composer_alerte(_inc(cause="reconnexion"), "fr", marque="Acme")
    assert [b["text"]["text"] for b in blocs[1]["elements"]] == ["Partager un forfait"]


def test_la_page_des_modeles_est_un_type_declare_seulement(monkeypatch):
    from oto_mcp import links
    monkeypatch.setenv("OTO_APP_URL", BASE)
    monkeypatch.delenv("OTO_DASHBOARD_LINK_PATHS", raising=False)
    assert "agent_models" in links.DECLARED_ONLY_KINDS
    assert links.link_for("agent_models", org=7, section="plans") is None
    monkeypatch.setenv("OTO_DASHBOARD_LINK_PATHS",
                       json.dumps({"agent_models": "/org/{org}/agents/models"}))
    assert links.link_for("agent_models", org=7, section="key") == (
        f"{BASE}/org/7/agents/models")


def test_un_incident_qui_cede_n_annonce_plus_la_panne(base):
    """Ouvert pendant les heures calmes, il cède avant le matin : la passe du matin ne
    dit pas « vos agents ne démarrent plus », et la reprise ne part pas sans alerte."""
    from oto_mcp import alerte_agents_bloques
    from oto_mcp.db import user_subscriptions as US
    p = _pool_au_plafond()
    _slack_de(p.org)
    base.env.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: True)
    _ouvert(p)
    US.marquer_statut(p.preteur, _F, "connected", ok=True)
    base.env.setattr(alerte_agents_bloques, "_heures_calmes", lambda m: False)
    assert A.balayer()["alertes"] == 0
    _vieillir(p.org, 6)
    bilan = A.balayer()
    assert (bilan["clos"], bilan["reprises"]) == (1, 0)
    assert base.slack.posts == []


def test_personne_a_qui_le_dire_n_est_pas_un_envoi_rate(base):
    p = _pool_au_plafond()
    A.balayer()
    _vieillir(p.org, A.DELAI_MIN + 1)
    bilan = A.balayer()
    assert (bilan["alertes"], bilan["rates"], bilan["sans_destinataire"]) == (0, 0, 1)
    assert _incident(p.org)["alerte_at"] is None


# --- Sans mode réglé : le pool qui complète -----------------------------------------

def test_sans_mode_des_preteurs_tous_au_plafond_sont_un_incident_du_pool(base):
    """Le demandeur n'a pas d'abonnement à lui : sans mode réglé, la prise le fait passer
    par le pool dès qu'un prêteur est servable, et ATTENDRE tant qu'aucun ne l'est."""
    p = SimpleNamespace(preteur=f"preteur-{uuid.uuid4().hex[:6]}",
                        demandeur=f"demandeur-{uuid.uuid4().hex[:6]}")
    _personne(p.preteur)
    _personne(p.demandeur)
    org = _org(p.preteur, p.demandeur)
    reset = _futur(5)
    _abonne(p.preteur, statut="paused_limit", reset=reset, pret_a=[org])
    _travail(org, p.demandeur, agent=_agent(org, p.demandeur), ouvert=False)
    A.balayer()
    inc = _incident(org)
    assert (inc["cause"], inc["par_le_pool"], inc["titulaires"]) == (
        "plafond", True, [p.preteur])
    assert A._date(inc["reprise_at"]) == reset.replace(microsecond=0)


def test_sans_mode_un_preteur_servable_n_est_pas_un_incident(base):
    preteur = _personne(f"preteur-{uuid.uuid4().hex[:6]}")
    demandeur = _personne(f"demandeur-{uuid.uuid4().hex[:6]}")
    org = _org(preteur, demandeur)
    _abonne(preteur, pret_a=[org])
    _travail(org, demandeur, ouvert=False)
    A.balayer()
    assert _incident(org) is None


def test_sans_mode_ni_pret_un_abonnement_pas_ouvert_ici_n_est_pas_un_incident(base):
    demandeur = _personne(f"demandeur-{uuid.uuid4().hex[:6]}")
    org = _org(demandeur)
    _abonne(demandeur, statut="paused_limit", reset=_futur())
    _travail(org, demandeur, ouvert=False)
    A.balayer()
    assert _incident(org) is None


def test_sans_mode_le_forfait_du_proprietaire_au_plafond(base):
    proprio = _personne(f"proprio-{uuid.uuid4().hex[:6]}")
    org = _org(proprio)
    _abonne(proprio, statut="paused_limit", reset=_futur())
    _travail(org, proprio)
    A.balayer()
    inc = _incident(org)
    assert (inc["cause"], inc["par_le_pool"], inc["titulaires"]) == (
        "plafond", False, [proprio])


def test_mode_personnel_le_proprietaire_a_reconnecter(base):
    proprio = _personne(f"proprio-{uuid.uuid4().hex[:6]}")
    org = _org(proprio, mode="personnel")
    _abonne(proprio, statut="needs_login")
    _travail(org, proprio)
    A.balayer()
    assert (_incident(org)["cause"], _incident(org)["par_le_pool"]) == (
        "reconnexion", False)


# --- Plus rien n'attend, la cause tient ----------------------------------------------

def test_un_travail_perime_alors_que_la_cause_tient_n_annonce_pas_de_reprise(base):
    """Un webhook périme à sa fraîcheur pendant que le forfait est toujours au plafond :
    l'incident se clôt (rien n'attend plus), sans « les agents ont repris »."""
    from oto_mcp.db._conn import _connect
    p = _pool_au_plafond()
    _slack_de(p.org)
    _ouvert(p)
    with _connect() as conn:
        conn.execute("UPDATE runner_jobs SET status = 'expired', finished_at = NOW() "
                     "WHERE id = %s", (p.job,))
    A.balayer()
    _vieillir(p.org, 6)
    bilan = A.balayer()
    assert (bilan["clos"], bilan["reprises"]) == (1, 0)
    inc = _incident(p.org)
    assert inc["repris"] is False and inc["clos_at"] is not None
    assert [m["text"] for m in base.slack.posts if "repris" in m["text"]] == []
    # Le webhook suivant, toujours sans forfait : un incident NEUF, qui s'annoncera.
    _travail(p.org, p.demandeur, ouvert=False)
    A.balayer()
    neuf = _incident(p.org)
    assert neuf["id"] != inc["id"] and neuf["ouvert_at"] is None
