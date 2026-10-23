"""Tulina in Slack — `POST /api/slack/events` (`slack_events.py`).

What the bot guarantees:
1. **Proof**: only a request signed with the app's signing secret, within five
   minutes, is read; no secret on the instance is OUR fault (503, Slack retries).
2. **Who answers**: the org that installed THIS app in THIS workspace, its agent in
   the `slack` preset — at most one per org, chosen by an org admin, with an ORG
   procedure.
3. **As whom**: the member whose email the Slack user carries — exactly one; the run
   carries THAT member's identity, never the agent owner's. Guests, other
   workspaces and unknown emails are told why, in Slack, and nothing runs.
4. **Through the webhook queue**: the agent's pause and caps apply, a retry of the
   same `event_id` is a duplicate, the bot never answers its own messages.
5. **Uninstall**: the workspace's rows written by this app are deleted.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest

from oto_mcp import runner_hook, slack_events
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 77
SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
TEAM, APP = "T0TEAM", "A0APP"


def _signe(corps: dict, *, secret=SECRET, ts=None) -> tuple[bytes, dict]:
    brut = json.dumps(corps).encode()
    ts = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + brut,
                           hashlib.sha256).hexdigest()
    return brut, {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def _mention(text="<@U0BOT> find 20 CTOs", **ev):
    return {"type": "event_callback", "event_id": "Ev1", "team_id": TEAM,
            "api_app_id": APP,
            "event": {"type": "app_mention", "user": "U0ASKER", "channel": "C0CHAN",
                      "ts": "1700.01", "text": text, **ev}}


class _Slack:
    """The bot's Slack client: what it was asked to say and react."""

    def __init__(self, user: dict):
        self.user, self.dit, self.reactions = user, [], []

    def get_user_info(self, uid):
        return {"ok": True, "user": self.user}

    def post_ephemeral(self, channel, user, text):
        self.dit.append(("ephemeral", channel, user, text))

    def post_message(self, channel, text, thread_ts=None):
        self.dit.append(("message", channel, thread_ts, text))

    def add_reaction(self, channel, ts, name):
        self.reactions.append((channel, ts, name))


_MEMBRE = {"id": "U0ASKER", "team_id": TEAM, "is_bot": False,
           "profile": {"email": "Ada@Example.test", "real_name": "Ada"}}


@pytest.fixture
def slack(monkeypatch):
    """One install, one Slack agent, one member — each piece replaceable."""
    vu = {"user": dict(_MEMBRE), "installs": [{"entity_id": str(ORG), "account": "Acme"}],
          "agent": {"id": 5, "org_id": ORG, "sub": "owner", "procedure": "assistant",
                    "tools": ["serper_search"], "input": "You are the assistant.",
                    "hook_auth": "slack", "kind": "webhook", "enabled": True},
          "membres": {"ada-sub"}, "enfile": None, "supprimes": []}
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: SECRET)
    monkeypatch.setattr(slack_events.credentials_store, "slack_workspace_installs",
                        lambda team, app: list(vu["installs"])
                        if (team, app) == (TEAM, APP) else [])
    monkeypatch.setattr(slack_events.credentials_store, "get_credential",
                        lambda et, eid, c, account="": json.dumps({"bot_token": "xoxb-1"}))
    monkeypatch.setattr(slack_events.credentials_store, "unpack_secret",
                        lambda c, s: json.loads(s))
    monkeypatch.setattr(slack_events.credentials_store, "clear_credential",
                        lambda et, eid, c, account="": vu["supprimes"].append(
                            (et, eid, account)) or True)
    monkeypatch.setattr(slack_events.db, "trigger_slack_de_l_org",
                        lambda org: dict(vu["agent"]) if vu["agent"] else None)
    monkeypatch.setattr(slack_events.db, "get_users_by_email",
                        lambda email: [{"sub": s} for s in ("ada-sub", "other-org-sub")]
                        if email == "ada@example.test" else [])
    monkeypatch.setattr(slack_events.roles, "is_org_member",
                        lambda sub, org: org == ORG and sub in vu["membres"])
    client = {}

    def _client(bot_token=None, **kw):
        assert bot_token == "xoxb-1"
        client["c"] = _Slack(vu["user"])
        return client["c"]

    monkeypatch.setattr("oto.tools.slack.client.SlackClient", _client)

    def _enfiler(t, externe, source, **kw):
        if vu.get("refus"):
            raise vu["refus"]
        vu["enfile"] = {"t": t, "externe": externe, "source": source, **kw}
        return {"ok": True, "job_id": 900, "trigger_id": t["id"],
                "duplicate": vu.get("duplicate", False)}

    monkeypatch.setattr(slack_events.runner_hook, "enfiler", _enfiler)
    monkeypatch.setattr("oto_mcp.capabilities._outils_manquants.manquants",
                        lambda org, sub, tools: [])
    monkeypatch.setenv("OTO_BRAND_NAME", "Acme")
    vu["client"] = client
    return vu


def _recevoir(corps, **kw):
    brut, entetes = _signe(corps, **kw)
    return slack_events.recevoir(brut, entetes, corps)


# ── 1. la PREUVE ──────────────────────────────────────────────────────────────

def test_le_defi_d_URL_est_rendu_une_fois_la_signature_verifiee(slack):
    assert _recevoir({"type": "url_verification", "challenge": "abc"}) == {"challenge": "abc"}


def test_une_signature_FAUSSE_est_refusee_sans_rien_lire(slack):
    corps = _mention()
    brut, entetes = _signe(corps, secret="un-autre-secret")
    with pytest.raises(slack_events.Refus) as e:
        slack_events.recevoir(brut, entetes, corps)
    assert (e.value.statut, e.value.code) == (401, "invalid_signature")
    assert slack["enfile"] is None


def test_un_corps_MODIFIE_apres_signature_est_refuse(slack):
    brut, entetes = _signe(_mention())
    with pytest.raises(slack_events.Refus):
        slack_events.recevoir(brut.replace(b"CTOs", b"CFOs"), entetes, _mention())


def test_une_signature_VALIDE_mais_vieille_de_plus_de_5_min_est_refusee(slack):
    with pytest.raises(slack_events.Refus) as e:
        _recevoir(_mention(), ts=int(time.time()) - 301)
    assert e.value.code == "stale_timestamp"


def test_sans_secret_sur_l_instance_c_est_NOTRE_panne_503(slack, monkeypatch):
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: None)
    with pytest.raises(slack_events.Refus) as e:
        _recevoir(_mention())
    assert e.value.statut == 503


# ── 2-3. qui répond, sous quelle identité ─────────────────────────────────────

def test_une_mention_part_vers_l_agent_Slack_SOUS_l_identite_du_membre(slack):
    out = _recevoir(_mention())
    assert out["job_id"] == 900
    e = slack["enfile"]
    assert e["sub"] == "ada-sub", "le run porte le membre qui demande, pas le propriétaire"
    assert e["externe"] == "Ev1" and e["source"] == "slack"
    assert set(slack_events.OUTILS_DE_REPONSE) <= set(e["outils_en_plus"])
    assert "find 20 CTOs" in e["entree"] and "<@U0BOT>" not in e["entree"]
    assert e["entree"].startswith("You are the assistant.")
    assert 'thread_ts="1700.01"' in e["entree"] and 'channel="C0CHAN"' in e["entree"]
    assert '_instance="org:77:slack:Acme"' in e["entree"]
    assert slack["client"]["c"].reactions == [("C0CHAN", "1700.01", "eyes")]


def test_une_reponse_dans_un_FIL_reste_dans_ce_fil(slack):
    _recevoir(_mention(thread_ts="1600.00"))
    assert 'thread_ts="1600.00"' in slack["enfile"]["entree"]


def test_un_espace_que_PERSONNE_n_a_installe_est_ignore_sans_bruit(slack):
    slack["installs"] = []
    assert _recevoir(_mention())["ignored"] == "no_install"
    assert slack["enfile"] is None and "c" not in slack["client"]


def test_sans_agent_Slack_la_personne_l_apprend(slack):
    slack["agent"] = None
    assert _recevoir(_mention())["ignored"] == "no_agent"
    (genre, _, user, texte), = slack["client"]["c"].dit
    assert genre == "ephemeral" and user == "U0ASKER" and "admin" in texte


@pytest.mark.parametrize("champ", [{"is_restricted": True}, {"is_ultra_restricted": True},
                                   {"team_id": "T0AUTRE"}])
def test_un_INVITE_ou_un_membre_d_un_AUTRE_espace_ne_lance_rien(slack, champ):
    slack["user"].update(champ)
    assert _recevoir(_mention())["ignored"] == "unknown_member"
    assert slack["enfile"] is None
    assert "guests" in slack["client"]["c"].dit[0][3]


def test_un_email_sans_membre_dans_l_org_ne_lance_rien(slack):
    slack["membres"] = set()
    assert _recevoir(_mention())["ignored"] == "unknown_member"
    assert slack["enfile"] is None
    assert "invite you" in slack["client"]["c"].dit[0][3]


def test_un_email_porte_par_DEUX_membres_est_refuse_plutot_que_devine(slack):
    slack["membres"] = {"ada-sub", "other-org-sub"}
    assert _recevoir(_mention())["ignored"] == "unknown_member"
    assert slack["enfile"] is None


# ── 4. la file des webhooks ───────────────────────────────────────────────────

def test_un_agent_en_PAUSE_le_dit_a_la_personne(slack):
    slack["refus"] = runner_hook.HookRefus(409, "trigger_paused", "This agent is paused.")
    assert _recevoir(_mention())["refused"] == "trigger_paused"
    assert slack["client"]["c"].dit[0][3] == "This agent is paused."
    assert slack["client"]["c"].reactions == []


def test_une_RETENTATIVE_de_Slack_ne_remet_pas_de_reaction(slack):
    slack["duplicate"] = True
    _recevoir(_mention())
    assert slack["client"]["c"].reactions == []


@pytest.mark.parametrize("ev", [{"bot_id": "B1"}, {"subtype": "message_changed"}])
def test_le_bot_ne_se_repond_JAMAIS_a_lui_meme(slack, ev):
    corps = _mention()
    corps["event"].update(type="message", channel_type="im", **ev)
    assert _recevoir(corps)["ignored"] == "event"


def test_un_DM_au_bot_part_et_la_reponse_est_un_message_ordinaire(slack):
    corps = _mention(text="hello")
    corps["event"].update(type="message", channel_type="im", channel="D0DM")
    _recevoir(corps)
    assert "a direct message to you" in slack["enfile"]["entree"]


def test_un_message_ordinaire_dans_un_canal_n_est_PAS_une_demande(slack):
    corps = _mention()
    corps["event"].update(type="message", channel_type="channel")
    assert _recevoir(corps)["ignored"] == "event"


# ── 5. désinstallation ────────────────────────────────────────────────────────

def test_desinstaller_l_app_supprime_les_lignes_de_cet_espace(slack):
    corps = {"type": "event_callback", "team_id": TEAM, "api_app_id": APP,
             "event": {"type": "app_uninstalled"}}
    assert _recevoir(corps)["removed"] == 1
    assert slack["supprimes"] == [("org", str(ORG), "Acme")]


def test_un_jeton_UTILISATEUR_revoque_ne_supprime_pas_le_bot(slack):
    corps = {"type": "event_callback", "team_id": TEAM, "api_app_id": APP,
             "event": {"type": "tokens_revoked", "tokens": {"oauth": ["U1"]}}}
    assert _recevoir(corps)["ignored"] == "user_token_only"
    assert slack["supprimes"] == []


# ── la pose du préréglage `slack` ──────────────────────────────────────────────

@pytest.fixture
def pose(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda portee, ident, fournisseur, cle: None)
    for nom, rendu in (("comptage_livraisons", {"recues_24h": 0, "refusees_24h": 0,
                                                "derniere": None}),
                       ("file_du_declencheur", {"pending": 0, "held": 0})):
        monkeypatch.setattr(RT.db, nom, lambda *a, _r=rendu: dict(_r))
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda o, t: {"expired_count": 0, "expired_since": None,
                                      "expired_last": None})
    ligne = {"id": 5, "org_id": ORG, "sub": "alexis", "kind": "webhook",
             "procedure": "assistant", "tools": ["a"], "enabled": True,
             "payload_mode": "ignore", "payload_fields": None,
             "model": "claude-sonnet-5", "hook_auth": "bearer",
             "signing_secret_set": True, "hook_slug": None, "max_per_day": None}
    etat = {"ligne": ligne, "hash": "h0", "admin": True, "autre": None,
            "procedures_org": {"assistant"}}

    def _poser_auth(tid, org, mode, secret_enc=None, effacer_le_secret=False):
        ligne["hook_auth"] = mode
        if effacer_le_secret:
            ligne["signing_secret_set"] = False
        return True

    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "poser_auth_de_hook", _poser_auth)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda t, o, h: etat.update(hash=h) or True)
    monkeypatch.setattr(RT.db, "trigger_slack_de_l_org", lambda org: etat["autre"])
    monkeypatch.setattr(RT.roles, "is_org_admin", lambda sub, org: etat["admin"])
    monkeypatch.setattr(RT.org_store, "get_instruction",
                        lambda ot, oid, slug, *a, **k: {"slug": slug}
                        if ot == "org" and slug in etat["procedures_org"] else None)
    return etat


def _poser_slack():
    return asyncio.run(RT._hook_auth(ResolvedCtx(sub="alexis", org_id=ORG),
                                     RT.HookAuthInput(trigger_id=5, hook_auth="slack")))


def test_un_admin_fait_repondre_l_agent_dans_Slack_et_ses_PORTES_se_ferment(pose):
    out = _poser_slack()
    assert out["trigger"]["hook_auth"] == "slack"
    assert pose["hash"] is None and pose["ligne"]["signing_secret_set"] is False
    assert out["hook_secret"] is None and out["hook_url"] is None


def test_le_PROPRIETAIRE_non_admin_ne_peut_pas_le_poser(pose):
    pose["admin"] = False
    with pytest.raises(AuthzDenied) as e:
        _poser_slack()
    assert e.value.code == "org_admin_required"


def test_UN_seul_agent_Slack_par_org(pose):
    pose["autre"] = {"id": 9}
    with pytest.raises(AuthzDenied) as e:
        _poser_slack()
    assert e.value.code == "slack_agent_exists"


def test_une_procedure_PERSONNELLE_est_refusee(pose):
    pose["procedures_org"] = set()
    with pytest.raises(AuthzDenied) as e:
        _poser_slack()
    assert e.value.code == "slack_agent_needs_org_procedure"


def test_une_adresse_d_agent_Slack_n_ouvre_rien_par_api_hooks(monkeypatch):
    """Aucune preuve de `/api/hooks/{id}` ne trouve un agent `slack` : le porteur
    est cherché sous SON préréglage (garde SQL), et rien n'est sans preuve."""
    vus = []
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret",
                        lambda i, h, hook_auth="bearer": vus.append(hook_auth))
    monkeypatch.setattr(runner_hook.db, "trigger_sans_preuve", lambda i: None)
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_x", {"secret": "otoh_x"},
                               par_adresse_privee=True)
    assert e.value.statut == 404 and "slack" not in vus


def test_enfiler_porte_l_IDENTITE_et_les_OUTILS_du_run_Slack(monkeypatch):
    """`runner_hook.enfiler` : `sub` et `outils_en_plus` vont au travail, l'entrée
    remplace l'instruction augmentée, et le travail reste celui de l'agent."""
    vu = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    for nom, fn in (("_connect", lambda: _Conn()),
                    ("verrouiller_le_declencheur", lambda c, i: None),
                    ("retard_de_lissage", lambda c, t, d, f: 0),
                    ("livraison_acceptee", lambda c, t, e: None),
                    ("enregistrer", lambda *a, **k: 1),
                    ("enqueue_job", lambda org, kind, **kw: vu.update(kw) or {"id": 1})):
        monkeypatch.setattr(runner_hook.db, nom, fn)
    monkeypatch.setattr(runner_hook.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_hook.procedure_retiree, "archivee", lambda *a: None)
    t = {"id": 5, "org_id": ORG, "sub": "owner", "procedure": "assistant",
         "tools": ["serper_search", "slack_post_message"], "input": "x", "enabled": True,
         "model": "claude-sonnet-5", "hook_auth": "slack", "max_per_day": None}
    runner_hook.enfiler(t, "Ev1", "slack", entree="the request", sub="ada-sub",
                        outils_en_plus=slack_events.OUTILS_DE_REPONSE)
    assert vu["sub"] == "ada-sub"
    assert vu["payload"]["input"] == "the request"
    assert vu["payload"]["tools"] == ["serper_search", "slack_post_message",
                                      "slack_add_reaction"]
    assert vu["payload"]["trigger_id"] == 5 and vu["payload"]["procedure"] == "assistant"


def test_un_outil_que_le_MEMBRE_n_a_pas_active_est_dit_avant_d_enfiler(slack, monkeypatch):
    """Le run voit la boîte à outils du membre : un connecteur masqué chez lui,
    et l'agent ne pourrait même pas répondre."""
    monkeypatch.setattr("oto_mcp.capabilities._outils_manquants.manquants",
                        lambda org, sub, tools: ["slack_post_message"]
                        if sub == "ada-sub" else [])
    assert _recevoir(_mention())["refused"] == "missing_tools"
    assert slack["enfile"] is None
    assert "slack_post_message" in slack["client"]["c"].dit[0][3]


def test_un_agent_sur_ABONNEMENT_PERSONNEL_ne_passe_pas_en_slack(pose):
    pose["ligne"]["model"] = RT.runner_models.MODELES_PERSONNELS[0]
    with pytest.raises(AuthzDenied) as e:
        _poser_slack()
    assert e.value.code == "slack_agent_on_personal_subscription"
