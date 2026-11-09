"""@<app> in Slack — the in-app agent with Slack as its screen (`slack_copilot.py`).

What it guarantees:
1. **Who**: only the REAL member the Slack email names; a mention or a DM from anybody else
   is told why (ephemeral in a channel), a follow-up from a stranger is not answered at all;
   the agent must be open to them and under today's ceiling.
2. **Once**: the two events of one mention, and Slack's retries, make ONE turn.
3. **Where the conversation lives**: a thread, per person; follow-ups continue it without
   another mention; a DM rolls and renews after 30 idle minutes.
4. **What Slack shows**: a working message, edited at a bounded rate, then the answer with
   Slack's own markup — never a mention or a channel link the model could write.
5. **Changes ask first**: a card with every argument and Approve / Cancel, answerable by the
   asker only, replay-safe; a call too long to read is not approvable here; Undo is the
   panel's undo.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import types
from urllib.parse import urlencode

import pytest

from oto_mcp import slack_copilot, slack_events
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.copilot import boucle, cle as cles, routes

ORG = 77
SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
TEAM, APP = "T0TEAM", "A0APP"


# ── Slack text ⇄ agent text ───────────────────────────────────────────────────

def test_la_demande_perd_la_mention_de_l_app_et_garde_le_reste():
    t = "<@U0BOT> what ran in <#C0X|ops> for <@U0ADA>? see <https://a.test/x?a=1&amp;b=2|the doc>"
    assert slack_copilot.texte_de_demande(t, "U0BOT") == \
        "what ran in #ops for @U0ADA? see the doc (https://a.test/x?a=1&b=2)"


def test_sans_id_d_app_c_est_la_mention_de_tete_qui_s_en_va():
    assert slack_copilot.texte_de_demande("<@U0BOT> hello <@U0OTHER>") == "hello @U0OTHER"
    assert slack_copilot.texte_de_demande("&lt;b&gt; &amp; <!here>") == "<b> & @here"


def test_la_reponse_du_modele_ne_peut_pas_devenir_une_mention_Slack():
    """`<`, `>`, `&` sont échappés AVANT le reste : `<!channel>` écrit par le modèle (ou lu
    dans une ligne) sort en texte, jamais en appel à tout le canal."""
    out = slack_copilot.vers_mrkdwn("hi <!channel> and <@U1> & <#C1|x> [ok](https://a.test)")
    assert "<!channel>" not in out and "<@U1>" not in out and "<#C1|x>" not in out
    assert "&lt;!channel&gt;" in out and "&amp;" in out
    assert "<https://a.test|ok>" in out


def test_le_markdown_du_modele_devient_du_mrkdwn():
    out = slack_copilot.vers_mrkdwn("## Result\n**3 agents** paused\n* one\n+ two")
    assert out == "*Result*\n*3 agents* paused\n- one\n- two"


# ── Block Kit ─────────────────────────────────────────────────────────────────

def _carte(**k):
    return {"type": "approval_required", "id": "tu_1", "title": "Delete table 12.",
            "steps": [{"title": "Delete table", "detail": json.dumps({"datastore": 12})}],
            "destructive": False, **k}


def _texte(blocs):
    return json.dumps(blocs, ensure_ascii=False)


def _corps(blocs):
    """The words of the blocks, as Slack shows them (no JSON escaping in the way)."""
    out = []
    for b in blocs:
        if b.get("text"):
            out.append(b["text"]["text"])
        out += [e["text"] for e in b.get("elements") or () if isinstance(e.get("text"), str)]
    return "\n".join(out)


def test_la_carte_montre_la_phrase_et_CHAQUE_argument_puis_deux_boutons():
    blocs = slack_copilot.blocs_carte("thr_a", _carte())
    assert "Delete table 12." in _corps(blocs) and '"datastore": 12' in _corps(blocs)
    boutons = blocs[-1]["elements"]
    assert [b["action_id"] for b in boutons] == ["copilot_approve", "copilot_reject"]
    assert {b["value"] for b in boutons} == {"thr_a|tu_1"}
    assert boutons[0]["style"] == "primary" and "confirm" not in boutons[0]


def test_une_carte_destructive_demande_confirmation():
    b = slack_copilot.blocs_carte("thr_a", _carte(destructive=True))[-1]["elements"][0]
    assert b["style"] == "danger" and b["confirm"]["confirm"]["text"] == "Yes, do it"


def test_un_appel_trop_long_pour_etre_lu_en_entier_n_a_PAS_de_bouton_Approuver():
    """Rien ne part qu'on n'ait pu lire : sans la place de tout montrer, on n'approuve pas
    depuis Slack."""
    long = json.dumps({"rows": "x" * (slack_copilot.DETAIL_MAX + 10)})
    blocs = slack_copilot.blocs_carte("thr_a", _carte(steps=[{"title": "t", "detail": long}]))
    assert [b["action_id"] for b in blocs[-1]["elements"]] == ["copilot_reject"]
    assert "can't be approved from Slack" in _texte(blocs)


def test_un_detail_long_mais_lisible_est_montre_ENTIER_en_plusieurs_blocs():
    detail = "\n".join(f"line {i} " + "y" * 60 for i in range(150))
    blocs = slack_copilot.blocs_carte("thr_a", _carte(steps=[{"title": "t", "detail": detail}]))
    corps = "".join(b["text"]["text"] for b in blocs if b["type"] == "section")
    assert "line 0 " in corps and "line 149 " in corps
    assert all(len(b["text"]["text"]) <= 3000 for b in blocs if b["type"] == "section")
    assert blocs[-1]["elements"][0]["action_id"] == "copilot_approve"


def test_un_detail_ne_peut_pas_fermer_son_bloc_de_code():
    blocs = slack_copilot.blocs_carte("t", _carte(steps=[
        {"title": "t", "detail": "a ``` <!channel> b"}]))
    corps = _corps(blocs)
    assert "<!channel>" not in corps and "a `\u200b`` &lt;!channel&gt; b" in corps


def test_la_reponse_finale_porte_le_texte_les_annulations_et_la_carte():
    blocs = slack_copilot.blocs_final(
        "Done **it**", [("rv_1", "Added 3 rows")], _carte(), "oops", "thr_a",
        prefixe="✅ Approved by <@U1>")
    types = [b["type"] for b in blocs]
    assert blocs[0]["elements"][0]["text"] == "✅ Approved by <@U1>"
    assert "*it*" in _texte(blocs) and ":warning: oops" in _texte(blocs)
    undo = next(b for b in blocs if b["type"] == "actions"
                and b["elements"][0]["action_id"].startswith("copilot_undo"))
    assert undo["elements"][0]["value"] == "rv_1"
    assert types.count("actions") == 2    # the undo, then the card's buttons


def test_une_reponse_vide_dit_fait():
    assert "Done." in _texte(slack_copilot.blocs_final("", [], None, None, None))


def test_pas_plus_de_blocs_que_Slack_n_en_accepte():
    blocs = slack_copilot.blocs_final("z\n" * 20_000, [(f"rv{i}", "x") for i in range(9)],
                                      _carte(), None, "thr_a")
    assert len(blocs) <= 50


# ── a fake Slack, a fake loop ─────────────────────────────────────────────────

class _Slack:
    """The bot's Slack client: what it was asked to say, edit, react."""

    def __init__(self, user):
        self.user = user
        self.posts, self.edits, self.ephemeres, self.reactions, self.requetes = [], [], [], [], []

    def get_user_info(self, uid):
        return {"ok": True, "user": self.user}

    def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "blocks": blocks,
                           "thread_ts": thread_ts})
        return {"ok": True, "ts": f"90{len(self.posts)}.00"}

    def update_message(self, channel, ts, text=None, blocks=None):
        self.edits.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True}

    def post_ephemeral(self, channel, user, text=None, blocks=None):
        self.ephemeres.append((channel, user, text))

    def add_reaction(self, channel, ts, name):
        self.reactions.append((channel, ts, name))

    def _request(self, method, endpoint, **kw):
        self.requetes.append((endpoint, kw.get("json")))
        return {"ok": True}


_MEMBRE = {"id": "U0ASKER", "team_id": TEAM, "is_bot": False, "is_email_confirmed": True,
           "profile": {"email": "Ada@Example.test", "real_name": "Ada"}}


class _Base:
    """The store's four Slack functions, in memory."""

    def __init__(self):
        self.pris, self.fils, self.liens = set(), {}, []

    def reclamer(self, channel, ts):
        if (channel, ts) in self.pris:
            return False
        self.pris.add((channel, ts))
        return True

    def fil(self, team, channel, ts, sub):
        return self.fils.get((team, channel, ts, sub))

    def existe(self, team, channel, ts):
        return any(k[:3] == (team, channel, ts) for k in self.fils)

    def lier(self, thread_id, team, channel, ts, sub):
        self.liens.append((thread_id, team, channel, ts, sub))
        self.fils[(team, channel, ts, sub)] = {"thread_id": thread_id, "inactif_min": 0.0}

    def de(self, thread_id):
        for (team, channel, ts, sub), v in self.fils.items():
            if v["thread_id"] == thread_id:
                return {"team_id": team, "channel": channel, "thread_ts": ts, "sub": sub}
        return None


@pytest.fixture
def mur(monkeypatch):
    """One install, one member, one in-memory store, a recorded runner."""
    vu = {"user": dict(_MEMBRE), "membres": {"ada-sub"}, "tours": [], "reprises": [],
          "base": _Base(), "client": None, "plafond": False, "identite": None,
          "org_suspendue": None}
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: SECRET)
    monkeypatch.setattr(slack_events.credentials_store, "slack_workspace_installs",
                        lambda team, app: [{"entity_id": str(ORG), "account": "Acme"}]
                        if (team, app) == (TEAM, APP) else [])
    monkeypatch.setattr(slack_events.credentials_store, "get_credential",
                        lambda et, eid, c, account="": json.dumps({"bot_token": "xoxb-1"}))
    monkeypatch.setattr(slack_events.credentials_store, "unpack_secret",
                        lambda c, s: json.loads(s))
    monkeypatch.setattr(slack_events.org_store, "membres_par_email",
                        lambda org, email: sorted(vu["membres"])
                        if (org, email) == (ORG, "ada@example.test") else [])

    def _client(bot_token=None, **kw):
        assert bot_token == "xoxb-1"
        if vu["client"] is None:
            vu["client"] = _Slack(vu["user"])
        return vu["client"]

    monkeypatch.setattr("oto.tools.slack.client.SlackClient", _client)
    b = vu["base"]
    monkeypatch.setattr(slack_copilot.store, "slack_reclamer", b.reclamer)
    monkeypatch.setattr(slack_copilot.store, "slack_fil", b.fil)
    monkeypatch.setattr(slack_copilot.store, "slack_fil_existe", b.existe)
    monkeypatch.setattr(slack_copilot.store, "slack_lier", b.lier)
    monkeypatch.setattr(slack_copilot.store, "slack_de", b.de)
    monkeypatch.setenv("OTO_COPILOT_SUBS", "*")
    monkeypatch.setenv("OTO_BRAND_NAME", "Acme")
    monkeypatch.setattr("oto_mcp.capabilities.copilot.depense.atteint",
                        lambda sub: vu["plafond"])
    # The identity guard and the org's suspension (`slack_copilot._coupe`): open unless a
    # test closes them.
    monkeypatch.setattr("oto_mcp.garde_identite.refus", lambda sub: vu["identite"])
    monkeypatch.setattr("oto_mcp.org_suspension.refus", lambda org: vu["org_suspendue"])

    def _executer(fn, *args):
        vu["tours"].append((fn.__name__, args))

    monkeypatch.setattr(slack_copilot, "_executer", _executer)
    return vu


def _d(**k):
    base = dict(event_id="Ev1", team_id=TEAM, app_id=APP, user="U0ASKER", channel="C0CHAN",
                ts="1700.01", thread_ts="1700.01", texte="<@U0BOT> what ran?", dm=False,
                mention=True, bot_user="U0BOT")
    base.update(k)
    return slack_events.Demande(**base)


def _traiter(mur, d):
    from oto.tools.slack.client import SlackClient
    inst = slack_events.installation(TEAM, APP)
    return slack_copilot.traiter(SlackClient(bot_token="xoxb-1"), inst, d)


# ── 1. who ───────────────────────────────────────────────────────────────────

def test_une_mention_d_un_membre_lance_un_tour_dans_son_fil_avec_le_texte_nettoye(mur):
    out = _traiter(mur, _d())
    assert out == {"ok": True, "answered": True}
    (nom, args), = mur["tours"]
    inst, client, d, m, texte, thread_id, cle = args
    assert nom == "_repondre" and (m.sub, texte, thread_id, cle) == (
        "ada-sub", "what ran?", None, "1700.01")
    assert ("C0CHAN", "1700.01", "eyes") in mur["client"].reactions
    assert mur["client"].requetes[0][0] == "reactions.remove", "👀 comes off when done"


def test_un_inconnu_qui_mentionne_apprend_pourquoi_en_prive_et_rien_ne_part(mur):
    mur["membres"] = set()
    assert _traiter(mur, _d())["ignored"] == "unknown_member"
    (canal, user, texte), = mur["client"].ephemeres
    assert (canal, user) == ("C0CHAN", "U0ASKER") and "exactly one member" in texte
    assert mur["tours"] == [] and mur["base"].pris == set()


def test_un_invite_qui_mentionne_est_refuse_aussi(mur):
    mur["user"]["is_restricted"] = True
    assert _traiter(mur, _d())["ignored"] == "unknown_member"
    assert "guests" in mur["client"].ephemeres[0][2] and mur["tours"] == []


def test_un_utilisateur_sans_team_id_qui_mentionne_est_refuse(mur):
    mur["user"].pop("team_id")
    assert _traiter(mur, _d())["ignored"] == "unknown_member"
    assert mur["tours"] == []


def test_en_Grid_la_mention_d_un_espace_frere_de_la_meme_entreprise_part(mur):
    mur["user"].update(team_id="T0FRERE", enterprise_user={"enterprise_id": "E0GRID"})
    assert _traiter(mur, _d(enterprise_id="E0GRID"))["answered"] is True


def test_l_appui_garde_l_entreprise_de_Grid_pour_reconnaitre_le_membre(mur):
    mur["user"].update(team_id="T0FRERE", enterprise_user={"enterprise_id": "E0GRID"})
    mur["base"].fils[(TEAM, "C0CHAN", "1700.01", "ada-sub")] = {"thread_id": "thr_a"}
    assert slack_copilot.traiter_action(_action(mur, enterprise_id="E0GRID"))["answered"]
    assert slack_copilot.traiter_action(
        _action(mur, action_ts="1800.6"))["ignored"] == "unknown_member"


# ── places : un tour de trop est refusé, pas mis en attente ───────────────────

@pytest.fixture
def places(monkeypatch):
    from oto_mcp import slack_copilot_places as p
    monkeypatch.setattr(p, "_par_compte", {})
    monkeypatch.setattr(p, "_total", 0)
    p.plafond.cache_clear()
    yield p
    p.plafond.cache_clear()


def test_un_second_tour_du_meme_compte_pendant_le_premier_est_dit_occupe(mur, places):
    with places.place("ada-sub"):
        out = _traiter(mur, _d())
        assert places.en_cours() == 1, "le refus ne prend ni ne rend aucune place"
    assert out == {"ok": True, "refused": "busy"} and mur["tours"] == []
    (canal, user, texte), = mur["client"].ephemeres
    assert texte == places.MESSAGE and "try again in a moment" in texte
    assert mur["client"].reactions == [], "pas de 👀 sur ce qui ne tourne pas"
    assert places.en_cours() == 0


def test_le_plafond_du_processus_refuse_le_tour_de_trop(mur, places, monkeypatch):
    monkeypatch.setenv(places.ENV_MAX, "1")
    places.plafond.cache_clear()
    with places.place("quelqu-un-d-autre"):
        assert _traiter(mur, _d())["refused"] == "busy"
    assert _traiter(mur, _d(ts="1700.02", thread_ts="1700.02"))["answered"] is True
    assert places.en_cours() == 0, "la place est rendue après le tour"


def test_la_place_est_rendue_meme_si_le_tour_leve(mur, places, monkeypatch):
    def _panne(fn, *args):
        raise RuntimeError("boum")
    monkeypatch.setattr(slack_copilot, "_executer", _panne)
    with pytest.raises(RuntimeError):
        _traiter(mur, _d())
    assert places.en_cours() == 0


def test_un_appui_pendant_un_tour_du_meme_compte_est_dit_occupe(mur, places):
    mur["base"].fils[(TEAM, "C0CHAN", "1700.01", "ada-sub")] = {"thread_id": "thr_a"}
    with places.place("ada-sub"):
        out = slack_copilot.traiter_action(_action(mur))
    assert out["refused"] == "busy" and mur["tours"] == []
    assert mur["client"].ephemeres[-1][2] == places.MESSAGE


def test_l_agent_ferme_a_cette_personne_le_lui_dit(mur, monkeypatch):
    monkeypatch.setenv("OTO_COPILOT_SUBS", "someone-else")
    assert _traiter(mur, _d())["refused"] == "copilot_not_enabled"
    assert "isn't switched on" in mur["client"].ephemeres[0][2] and mur["tours"] == []


def test_le_plafond_du_jour_est_dit_et_rien_ne_part(mur):
    mur["plafond"] = True
    assert _traiter(mur, _d())["refused"] == "copilot_daily_cap"
    assert "limit" in mur["client"].ephemeres[0][2] and mur["tours"] == []


def test_une_mention_vide_recoit_un_exemple_pas_un_tour(mur):
    assert _traiter(mur, _d(texte="<@U0BOT>"))["ignored"] == "empty"
    assert "Tell me what you need" in mur["client"].ephemeres[0][2] and mur["tours"] == []


def test_un_compte_en_pause_ou_d_un_tenant_desactive_est_refuse_et_rien_ne_part(mur):
    """La porte Slack nomme le compte par l'e-mail, sans requête REST : elle traverse la
    garde d'identité elle-même, comme la porte du panneau (`api.base`)."""
    mur["identite"] = ("account_suspended", "This account is paused.")
    assert _traiter(mur, _d())["refused"] == "account_cut"
    assert mur["client"].ephemeres[0][2] == "This account is paused."
    assert mur["tours"] == [] and mur["base"].pris == set()


def test_une_org_suspendue_est_refusee_et_rien_ne_part(mur):
    mur["org_suspendue"] = "Suspended."
    assert _traiter(mur, _d())["refused"] == "account_cut"
    assert mur["tours"] == []


# ── 2. once ──────────────────────────────────────────────────────────────────

def test_les_deux_evenements_d_une_mention_font_UN_tour(mur):
    """`app_mention` puis `message` du même message (ou un retry) : le premier prend."""
    assert _traiter(mur, _d())["answered"] is True
    assert _traiter(mur, _d(event_id="Ev2"))["duplicate"] is True
    assert len(mur["tours"]) == 1


# ── 3. where the conversation lives ──────────────────────────────────────────

def test_la_mention_du_meme_membre_dans_son_fil_continue_sa_conversation(mur):
    """Hors DM, la suite se demande en taguant l'app (décision du 09/11/2026) : la mention
    dans son fil reprend SA conversation."""
    mur["base"].lier("thr_old", TEAM, "C0CHAN", "1600.00", "ada-sub")
    d = _d(ts="1601.00", thread_ts="1600.00", texte="<@U0BOT> and table 2?")
    _traiter(mur, d)
    (_, args), = mur["tours"]
    assert args[4:6] == ("and table 2?", "thr_old")


def test_la_mention_d_un_autre_membre_dans_le_meme_fil_ouvre_SA_conversation(mur):
    mur["base"].lier("thr_old", TEAM, "C0CHAN", "1600.00", "someone-else")
    _traiter(mur, _d(thread_ts="1600.00"))
    (_, args), = mur["tours"]
    assert args[5] is None, "nothing is shared between two people"


def test_un_DM_continue_la_conversation_courante_puis_en_ouvre_une_neuve_apres_30_min(mur):
    mur["base"].fils[(TEAM, "D0DM", "", "ada-sub")] = {"thread_id": "thr_dm",
                                                      "inactif_min": 5.0}
    dm = _d(channel="D0DM", dm=True, mention=False, bot_user="", texte="and now?")
    _traiter(mur, dm)
    assert mur["tours"][-1][1][5] == "thr_dm"
    mur["base"].fils[(TEAM, "D0DM", "", "ada-sub")]["inactif_min"] = 45.0
    _traiter(mur, _d(channel="D0DM", dm=True, mention=False, bot_user="", ts="1701.01",
                     thread_ts="1701.01", texte="again"))
    assert mur["tours"][-1][1][5] is None


# ── 4. what Slack shows ──────────────────────────────────────────────────────

async def _flux(*evenements):
    for e in evenements:
        yield e


def _fausse_tour(monkeypatch, *evenements, vus=None):
    def _tour(ctx, inp, marques, canal="app"):
        if vus is not None:
            vus.append((ctx, inp, marques, canal))
        return _flux(*evenements)

    monkeypatch.setattr(routes, "_tour", _tour)


def _repondre(mur, d=None, thread_id=None, cle="1700.01"):
    inst = slack_events.installation(TEAM, APP)
    from oto.tools.slack.client import SlackClient
    client = SlackClient(bot_token="xoxb-1")
    m = slack_events.Membre(sub="ada-sub", nom="Ada", email="ada@example.test")
    asyncio.run(slack_copilot._repondre(inst, client, d or _d(), m, "what ran?",
                                        thread_id, cle))
    return client


def test_le_tour_ouvre_un_message_dans_le_fil_puis_le_remplace_par_la_reponse(mur, monkeypatch):
    vus = []
    _fausse_tour(monkeypatch,
                 {"type": "thread", "thread_id": "thr_new"},
                 {"type": "text_delta", "text": "3 agents "}, {"type": "text_delta", "text": "ran."},
                 {"type": "suggestions", "items": ["Show me"]}, {"type": "done"}, vus=vus)
    c = _repondre(mur)
    assert c.posts[0]["thread_ts"] == "1700.01" and "Working" in c.posts[0]["text"]
    (e,) = c.edits
    assert e["ts"] == "901.00" and "3 agents ran." in _texte(e["blocks"])
    ctx, inp, marques, canal = vus[0]
    assert (ctx.sub, ctx.org_id, canal, inp.mode, inp.client_tools) == (
        "ada-sub", ORG, "slack", "ask", [])
    assert inp.thread_id is None and inp.page_context == {"surface": "slack", "kind": "channel"}
    assert mur["base"].liens == [("thr_new", TEAM, "C0CHAN", "1700.01", "ada-sub")]


def test_une_conversation_deja_liee_n_est_pas_reliee(mur, monkeypatch):
    _fausse_tour(monkeypatch, {"type": "thread", "thread_id": "thr_old"}, {"type": "done"})
    _repondre(mur, thread_id="thr_old")
    assert mur["base"].liens == []


def test_un_DM_se_poste_hors_fil_et_se_range_sous_la_cle_vide(mur, monkeypatch):
    _fausse_tour(monkeypatch, {"type": "thread", "thread_id": "thr_dm"},
                 {"type": "text_delta", "text": "hi"}, {"type": "done"})
    c = _repondre(mur, d=_d(channel="D0DM", dm=True, mention=False), cle="")
    assert c.posts[0]["thread_ts"] is None
    assert mur["base"].liens[0][3] == ""


def test_les_notes_de_progres_sont_espacees(mur, monkeypatch):
    """Slack limite `chat.update` : une note toutes les 1,5 s au plus."""
    t = {"now": 100.0}
    # The module's own clock, never the process's: the event loop reads `time.monotonic`.
    monkeypatch.setattr(slack_copilot, "time",
                        types.SimpleNamespace(monotonic=lambda: t["now"]))

    async def _tour(ctx, inp, marques, canal="app"):
        for avance, e in ((0.0, {"type": "progress", "text": "Looking at agents"}),
                          (0.2, {"type": "progress", "text": "Looking at agents and tables"}),
                          (1.8, {"type": "tool_call", "id": "a", "label": "List agents"}),
                          (0.1, {"type": "progress", "text": "Counting"}),
                          (9.0, {"type": "progress", "text": "Counting rows"}),
                          (0.0, {"type": "text_delta", "text": "Done."}),
                          (0.0, {"type": "done"})):
            t["now"] += avance
            yield e

    monkeypatch.setattr(routes, "_tour", _tour)
    c = _repondre(mur)
    notes = [e["text"] for e in c.edits[:-1]]
    assert notes == ["Looking at agents", "List agents…", "Counting rows"]
    assert "Done." in _texte(c.edits[-1]["blocks"])


def test_le_texte_d_avant_et_d_apres_un_outil_ne_se_colle_pas(mur, monkeypatch):
    _fausse_tour(monkeypatch, {"type": "text_delta", "text": "I'll check."},
                 {"type": "tool_call", "id": "a", "label": "x"},
                 {"type": "tool_result", "id": "a", "ok": True, "summary": "ok"},
                 {"type": "text_delta", "text": "Found 2."}, {"type": "done"})
    c = _repondre(mur)
    assert "I'll check.\n\nFound 2." in _texte(c.edits[-1]["blocks"]).replace("\\n", "\n")


def test_un_changement_defaisable_porte_un_bouton_Defaire(mur, monkeypatch):
    _fausse_tour(monkeypatch,
                 {"type": "tool_result", "id": "a", "ok": True, "summary": "Added 3 rows",
                  "revert_id": "rv_9"},
                 {"type": "text_delta", "text": "Added."}, {"type": "done"})
    c = _repondre(mur)
    undo = next(b for b in c.edits[-1]["blocks"] if b["type"] == "actions")
    assert undo["elements"][0]["value"] == "rv_9"


def test_la_boucle_qui_s_arrete_sur_une_carte_la_montre_avec_les_boutons(mur, monkeypatch):
    _fausse_tour(monkeypatch, {"type": "thread", "thread_id": "thr_new"},
                 {"type": "text_delta", "text": "I'll delete it."}, _carte())
    c = _repondre(mur)
    blocs = c.edits[-1]["blocks"]
    assert blocs[-1]["elements"][0]["value"] == "thr_new|tu_1"
    assert "I'll delete it." in _texte(blocs)


def test_une_erreur_du_tour_est_dite_dans_le_message(mur, monkeypatch):
    _fausse_tour(monkeypatch, {"type": "error", "message": "Too many open conversations"},
                 {"type": "done"})
    c = _repondre(mur)
    assert ":warning: Too many open conversations" in _texte(c.edits[-1]["blocks"])


def test_une_panne_du_tour_ne_fuit_rien_et_le_message_ne_reste_pas_en_attente(mur, monkeypatch):
    async def _casse(*a, **k):
        raise RuntimeError("secret detail")
        yield  # pragma: no cover

    monkeypatch.setattr(routes, "_tour", lambda *a, **k: _casse())
    c = _repondre(mur)
    corps = _texte(c.edits[-1]["blocks"])
    assert "went wrong" in corps and "secret detail" not in corps


def test_pas_de_cle_de_modele_est_dit_en_clair(mur, monkeypatch):
    async def _sans_cle(*a, **k):
        raise cles.SansCle("no_model_key", "Your organization has no model key.", 402)
        yield  # pragma: no cover

    monkeypatch.setattr(routes, "_tour", lambda *a, **k: _sans_cle())
    c = _repondre(mur)
    assert "no model key" in _texte(c.edits[-1]["blocks"])


def test_si_Slack_refuse_d_ouvrir_le_message_rien_ne_tourne(mur, monkeypatch):
    vus = []
    _fausse_tour(monkeypatch, {"type": "done"}, vus=vus)
    monkeypatch.setattr(_Slack, "post_message", lambda self, *a, **k: {"ok": True})
    _repondre(mur)
    assert vus == []


# ── 5. the buttons ───────────────────────────────────────────────────────────

def _signe_form(payload: dict, *, secret=SECRET, ts=None) -> tuple[bytes, dict]:
    brut = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + brut,
                           hashlib.sha256).hexdigest()
    return brut, {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def _appui(action_id="copilot_approve", valeur="thr_a|tu_1", **k):
    p = {"type": "block_actions", "api_app_id": APP, "user": {"id": "U0ASKER"},
         "team": {"id": TEAM}, "channel": {"id": "C0CHAN"},
         "message": {"ts": "901.00", "blocks": [
             {"type": "section", "text": {"type": "mrkdwn", "text": "x"}},
             {"type": "actions", "elements": [
                 {"type": "button", "action_id": "copilot_undo:0", "value": "rv_1"}]}]},
         "actions": [{"action_id": action_id, "value": valeur, "action_ts": "1800.5"}]}
    p.update(k)
    return p


def test_un_appui_signe_est_lu_et_rendu_pour_APRES_l_accuse(monkeypatch):
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: SECRET)
    brut, entetes = _signe_form(_appui())
    a = slack_copilot.recevoir_interaction(brut, entetes)
    assert (a.kind, a.valeur, a.user, a.channel, a.message_ts) == (
        "approve", "thr_a|tu_1", "U0ASKER", "C0CHAN", "901.00")


def test_un_appui_mal_signe_est_refuse_sans_rien_lire(monkeypatch):
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: SECRET)
    brut, entetes = _signe_form(_appui(), secret="un-autre")
    with pytest.raises(slack_events.Refus) as e:
        slack_copilot.recevoir_interaction(brut, entetes)
    assert (e.value.statut, e.value.code) == (401, "invalid_signature")


def test_un_corps_qui_n_est_pas_un_formulaire_signe_est_un_400(monkeypatch):
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: SECRET)
    for brut in (b"payload=%7Bnot-json", b"nothing", b"payload=%5B1%5D"):
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + brut,
                               hashlib.sha256).hexdigest()
        with pytest.raises(slack_events.Refus) as e:
            slack_copilot.recevoir_interaction(brut, {"x-slack-request-timestamp": ts,
                                                      "x-slack-signature": sig})
        assert e.value.statut == 400


def test_un_bouton_qui_n_est_pas_a_nous_ou_un_autre_type_n_est_rien(monkeypatch):
    monkeypatch.setattr(slack_events.slack_auth, "signing_secret", lambda: SECRET)
    for p in (_appui("someone_elses_button"), _appui(type="view_submission"),
              _appui(valeur="")):
        brut, entetes = _signe_form(p)
        assert slack_copilot.recevoir_interaction(brut, entetes) is None


def test_la_route_repond_200_vide_puis_traite_apres_l_accuse(mur, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import slack as api_slack

    vus = []
    monkeypatch.setattr(slack_copilot, "traiter_action", lambda a: vus.append(a))
    app = Starlette(routes=api_slack.make_routes(None, None, None, None, None))
    brut, entetes = _signe_form(_appui())
    with TestClient(app) as c:
        r = c.post(api_slack.INTERACTIONS_PATH, content=brut,
                   headers={**entetes, "content-type": "application/x-www-form-urlencoded"})
        mauvais = c.post(api_slack.INTERACTIONS_PATH, content=brut,
                         headers={**entetes, "x-slack-signature": "v0=bad"})
    assert r.status_code == 200 and r.content == b""
    assert [a.kind for a in vus] == ["approve"]
    assert mauvais.status_code == 401 and mauvais.headers["x-slack-no-retry"] == "1"


def _action(mur, kind="approve", **k):
    base = dict(kind=kind, valeur="thr_a|tu_1", action_id=f"copilot_{kind}", action_ts="1800.5",
                user="U0ASKER", team_id=TEAM, app_id=APP, channel="C0CHAN", message_ts="901.00")
    base.update(k)
    return slack_copilot.Action(**base)


def test_seul_celui_qui_a_demande_peut_repondre_a_la_carte(mur):
    mur["base"].fils[(TEAM, "C0CHAN", "1700.01", "someone-else")] = {"thread_id": "thr_a"}
    out = slack_copilot.traiter_action(_action(mur))
    assert out["refused"] == "not_the_asker" and mur["tours"] == []
    assert mur["client"].ephemeres[0][2] == "Only the person who asked can answer this."


def test_une_carte_d_un_autre_canal_n_est_pas_honoree(mur):
    mur["base"].fils[(TEAM, "C0AUTRE", "1700.01", "ada-sub")] = {"thread_id": "thr_a"}
    assert slack_copilot.traiter_action(_action(mur))["refused"] == "not_the_asker"
    assert mur["tours"] == []


def test_l_appui_de_celui_qui_a_demande_reprend_la_conversation(mur):
    mur["base"].fils[(TEAM, "C0CHAN", "1700.01", "ada-sub")] = {"thread_id": "thr_a"}
    out = slack_copilot.traiter_action(_action(mur))
    assert out["answered"] is True
    (nom, args), = mur["tours"]
    assert nom == "_reprendre" and args[3:] == (
        slack_events.Membre("ada-sub", "Ada", "ada@example.test"), "thr_a", "tu_1")


def test_le_meme_appui_livre_deux_fois_ne_fait_qu_une_reprise(mur):
    mur["base"].fils[(TEAM, "C0CHAN", "1700.01", "ada-sub")] = {"thread_id": "thr_a"}
    assert slack_copilot.traiter_action(_action(mur))["answered"] is True
    assert slack_copilot.traiter_action(_action(mur))["duplicate"] is True
    assert len(mur["tours"]) == 1


def test_un_appui_d_un_compte_en_pause_n_approuve_ni_ne_defait_rien(mur, monkeypatch):
    mur["base"].fils[(TEAM, "C0CHAN", "1700.01", "ada-sub")] = {"thread_id": "thr_a"}
    mur["identite"] = ("account_suspended", "This account is paused.")
    monkeypatch.setattr(routes, "revert_pour",
                        lambda *a: pytest.fail("an undo ran for a paused account"))
    assert slack_copilot.traiter_action(_action(mur))["refused"] == "account_cut"
    out = slack_copilot.traiter_action(_action(mur, "undo", valeur="rv_1",
                                               action_ts="1800.6"))
    assert out["refused"] == "account_cut" and mur["tours"] == []
    assert mur["client"].ephemeres[0][2] == "This account is paused."


def test_un_appui_d_un_inconnu_est_refuse_en_prive(mur):
    mur["membres"] = set()
    assert slack_copilot.traiter_action(_action(mur))["ignored"] == "unknown_member"
    assert mur["tours"] == [] and mur["client"].ephemeres


def _reprendre(mur, monkeypatch, kind, evenements):
    """Runs `_reprendre`, returning the client and the `avant` the loop was handed."""
    saisi = {}

    def _reprise(ctx, thread_id, avant):
        saisi.update(ctx=ctx, thread_id=thread_id, avant=avant)
        return _flux(*evenements)

    monkeypatch.setattr(routes, "_reprise", _reprise)
    inst = slack_events.installation(TEAM, APP)
    from oto.tools.slack.client import SlackClient
    client = SlackClient(bot_token="xoxb-1")
    m = slack_events.Membre(sub="ada-sub", nom="Ada", email="ada@example.test")
    asyncio.run(slack_copilot._reprendre(inst, client, _action(mur, kind), m, "thr_a", "tu_1"))
    return client, saisi


def _fil_en_attente():
    bloc = {"id": "tu_1", "name": "data_write", "input": {"datastore": 1}}
    return {"id": "thr_a", "sub": "ada-sub", "org_id": ORG, "queue": [bloc], "results": [],
            "pending": {"kind": "approval", "block": bloc, "sorte": "write"},
            "remembered": [], "model": "m", "messages": []}


def test_approuver_valide_l_appel_en_attente_et_signe_le_message(mur, monkeypatch):
    c, saisi = _reprendre(mur, monkeypatch, "approve",
                          [{"type": "text_delta", "text": "Created."}, {"type": "done"}])
    assert saisi["thread_id"] == "thr_a" and saisi["ctx"].sub == "ada-sub"
    fil = _fil_en_attente()
    assert saisi["avant"](fil) is None and fil["queue"][0]["approved"] is True
    (e,) = c.edits
    assert e["ts"] == "901.00" and "✅ Approved by <@U0ASKER>" in _texte(e["blocks"])
    assert "Created." in _texte(e["blocks"])


def test_annuler_refuse_l_appel_et_le_dit(mur, monkeypatch):
    c, saisi = _reprendre(mur, monkeypatch, "reject",
                          [{"type": "text_delta", "text": "Okay."}, {"type": "done"}])
    fil = _fil_en_attente()
    assert saisi["avant"](fil) is None
    assert fil["queue"] == [] and fil["results"][0]["content"] == boucle.REFUS
    assert "✖️ Cancelled by <@U0ASKER>" in _texte(c.edits[0]["blocks"])


def test_une_reponse_a_une_carte_qui_n_attend_plus_est_dite_en_prive_sans_toucher_au_message(
        mur, monkeypatch):
    c, saisi = _reprendre(mur, monkeypatch, "approve",
                          [{"type": "error", "message": boucle_msg()}, {"type": "done"}])
    assert c.edits == [] and c.ephemeres == [("C0CHAN", "U0ASKER", boucle_msg())]
    fil = _fil_en_attente()
    fil["pending"] = None
    assert "no longer waiting" in saisi["avant"](fil)


def boucle_msg():
    return "That answer is for something no longer waiting."


def test_Defaire_defait_une_fois_et_retire_le_bouton(mur, monkeypatch):
    vus = []
    monkeypatch.setattr(routes, "revert_pour",
                        lambda sub, rid: vus.append((sub, rid)) or
                        {"ok": True, "undone": "Added 3 rows", "skipped": 1})
    out = slack_copilot.traiter_action(_action(mur, "undo", valeur="rv_1",
                                               action_id="copilot_undo:0",
                                               blocs=tuple(_appui()["message"]["blocks"])))
    assert out["undone"] is True and vus == [("ada-sub", "rv_1")]
    (e,) = mur["client"].edits
    assert all(b["type"] != "actions" for b in e["blocks"])
    assert "Undone by <@U0ASKER>: Added 3 rows (1 left alone" in _texte(e["blocks"])


def test_Defaire_refuse_est_dit_en_prive(mur, monkeypatch):
    def _refuse(sub, rid):
        raise AuthzDenied(404, "nothing_to_undo", "Nothing to undo: already undone.")

    monkeypatch.setattr(routes, "revert_pour", _refuse)
    out = slack_copilot.traiter_action(_action(mur, "undo", valeur="rv_1",
                                               action_id="copilot_undo:0"))
    assert out["refused"] == "nothing_to_undo" and mur["client"].edits == []
    assert "already undone" in mur["client"].ephemeres[0][2]


def test_une_panne_apres_l_accuse_est_journalisee(monkeypatch, caplog):
    def _casse(a):
        raise RuntimeError("boom")

    monkeypatch.setattr(slack_copilot, "traiter_action", _casse)
    with caplog.at_level("ERROR", logger="oto_mcp.slack_copilot"):
        slack_copilot.apres_l_accuse_action(_action(None))
    assert "failed after the ACK" in caplog.text


# ── the prompt and the undo door ─────────────────────────────────────────────

def test_la_consigne_slack_dit_qu_il_n_y_a_ni_page_ni_ecran():
    from oto_mcp.capabilities.copilot import consigne
    assert "answering in Slack" not in consigne.consigne("")
    assert "You are answering in Slack" in consigne.consigne("", "slack")
    assert "no page tools" in consigne.consigne("", "slack")


def test_revert_pour_est_l_annulation_du_panneau_sans_sa_porte(monkeypatch):
    monkeypatch.setattr(routes.store, "consommer", lambda rid, sub: None)
    with pytest.raises(AuthzDenied) as e:
        routes.revert_pour("ada-sub", "rv_x")
    assert e.value.code == "nothing_to_undo"
    vus = []
    monkeypatch.setattr(routes.store, "consommer", lambda rid, sub: {
        "org_id": None, "label": "Added rows", "id": rid})
    monkeypatch.setattr(routes.annulation, "defaire",
                        lambda sub, ligne: vus.append(sub) or {"skipped": 0})
    assert routes.revert_pour("ada-sub", "rv_x") == {
        "ok": True, "undone": "Added rows", "skipped": 0} and vus == ["ada-sub"]
