"""Slack — lire les RÉPONSES d'un fil, et rejoindre ce qui est joignable.

Cinq signaux d'usage, deux manques.

**Le manque principal (#567, #576, #584, #592 — trois personnes en huit jours)** :
`slack_read_history` lit `conversations.history`, qui ne rend que le PREMIER NIVEAU.
Sur un parent, Slack annonce `reply_count`/`reply_users`/`latest_reply` — et jamais un
corps de réponse. L'ingestion quotidienne existe pour attraper les décisions d'équipe ;
or une décision ou un désaccord vit presque toujours dans le fil. #592 le résume : « the
run can prove the reply exists but cannot read a word of it ».

**Le second (#549) est d'une autre nature** : l'app n'est membre d'aucun canal, donc
`not_in_channel` sur cinq canaux sur huit et sur la remise du digest. Slack sait rejoindre
un canal PUBLIC (`conversations.join`) ; un canal PRIVÉ exige une invitation humaine, et
**aucune API ne la remplace**. D'où la ligne de conduite figée ici : ce qu'oto ne peut pas
faire, il le REFUSE en nommant le geste humain — il ne le simule pas et ne laisse pas
croire à une panne.

Le contrat Slack encodé ici a été SONDÉ en direct le 2026-08-28 (workspace Otomata
Community), pas déduit : cf. `oto-core/tests/test_slack_thread_replies.py` pour le détail
des différentiels (Slack AVALE un paramètre inconnu en rendant `ok:true`).
"""
import asyncio
from unittest.mock import patch

import pytest

from oto.tools.slack.client import SlackError

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import slack as slack_tools


def _err(code: str, needed=None, provided=None) -> SlackError:
    """SlackError portant `needed`/`provided`. Posés en attribut plutôt qu'au
    constructeur : le venv local traîne une COPIE d'oto-core en retard sur le pin,
    et ce test doit mordre des deux côtés du bump."""
    e = SlackError(code)
    e.needed, e.provided = needed, provided
    return e


def _mount():
    """Monte les tools Slack avec le client oto-core mocké. Le patch doit être
    actif AVANT `register()` : celui-ci fait `from … import SlackClient` et
    capterait sinon la vraie classe."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.slack.client.SlackClient")
    cls = patcher.start()
    m = FastMCP("t")
    slack_tools.register(m)
    return m, cls.return_value, patcher


@pytest.fixture
def slack(monkeypatch):
    class _RC:
        fields = {"bot_token": "xoxb-1", "user_token": None}
        key = None
        is_platform = False

    monkeypatch.setattr(slack_tools.access, "resolve_credential", lambda *a, **k: _RC())
    m, client, patcher = _mount()
    try:
        yield m, client
    finally:
        patcher.stop()


def _fn(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- le manque principal : lire une réponse de fil ----------------------------

# Un fil réel, tel que `conversations.replies` le rend (sondé) : le PARENT est
# toujours `messages[0]`, les réponses suivent.
FIL = {"ok": True, "has_more": False, "messages": [
    {"ts": "1787812434.744429", "thread_ts": "1787812434.744429",
     "text": "Recap du sweep", "reply_count": 2, "user": "U0BOT"},
    {"ts": "1787813698.937919", "thread_ts": "1787812434.744429",
     "text": "On arrête Snitcher, trop cher", "user": "U0BSRB4KWTY"},
    {"ts": "1787813999.000000", "thread_ts": "1787812434.744429",
     "text": "+1, on bascule sur l'ICP interne", "user": "U0BFOUNDER"},
]}


def test_le_fil_rend_les_CORPS_des_reponses(slack):
    """#592 : la boucle de feedback humaine passe par le fil. Sans ça, une
    procédure plafonne à « je vois que tu as répondu »."""
    m, client = slack
    client.replies.return_value = FIL
    out = _fn(m, "slack_read_thread")(channel="C0BM0BM1BT9", thread_ts="1787812434.744429")
    assert [r["text"] for r in out["replies"]] == [
        "On arrête Snitcher, trop cher", "+1, on bascule sur l'ICP interne"]
    client.replies.assert_called_once_with(
        "C0BM0BM1BT9", "1787812434.744429", limit=50, cursor=None,
        oldest=None, latest=None, inclusive=False)


def test_le_parent_est_rendu_a_part_et_JAMAIS_compte_comme_une_reponse(slack):
    """Slack répète le parent en `messages[0]` de CHAQUE page (sondé) :
    concaténer deux pages de `messages` le compte deux fois. On le sort du tas
    plutôt que d'écrire un avertissement en docstring."""
    m, client = slack
    client.replies.return_value = FIL
    out = _fn(m, "slack_read_thread")(channel="C1", thread_ts="1787812434.744429")
    assert out["parent"]["ts"] == "1787812434.744429"
    assert out["parent"]["reply_count"] == 2
    assert all(r["ts"] != out["parent"]["ts"] for r in out["replies"])


def test_un_message_sans_fil_rend_zero_reponse_sans_erreur(slack):
    """Sondé : un `ts` sans réponse rend `ok:true` avec le seul message. Ce n'est
    pas une panne — l'ingestion doit pouvoir le constater."""
    m, client = slack
    client.replies.return_value = {"ok": True, "messages": [
        {"ts": "1787848395.878659", "text": "note isolée"}]}
    out = _fn(m, "slack_read_thread")(channel="C1", thread_ts="1787848395.878659")
    assert out["replies"] == [] and out["parent"]["ts"] == "1787848395.878659"


# Une réponse de FIL, et Slack tel qu'il répond sur son ts (sondé le 28/08) : le
# « fil » d'une réponse est ce seul message, `ok:true`.
PARENT_TS, REPONSE_TS = "1787812434.744429", "1787813698.937919"
DERNIERE_TS = "1787813999.000000"
UNE_REPONSE = {"ts": REPONSE_TS, "thread_ts": PARENT_TS, "text": "On arrête Snitcher, trop cher"}


def _slack_fidele(fenetre_vide_la_reponse: bool):
    """`conversations.replies` modélisé par ses arguments. Sur le ts d'une réponse :
    ce seul message — ou RIEN quand une fenêtre `oldest` ≥ son ts l'exclut (la forme
    non sondée, que le correctif doit couvrir aussi). Sur le parent : le fil."""
    def replies(channel, ts, limit=50, cursor=None, oldest=None, latest=None,
                inclusive=False):
        if ts == REPONSE_TS:
            if fenetre_vide_la_reponse and oldest and oldest >= REPONSE_TS and not inclusive:
                return {"ok": True, "messages": []}
            return {"ok": True, "messages": [UNE_REPONSE]}
        if ts == PARENT_TS:
            msgs = [FIL["messages"][0]] + [
                m for m in FIL["messages"][1:] if not oldest or m["ts"] > oldest]
            return {"ok": True, "has_more": False, "messages": msgs}
        raise SlackError("thread_not_found")
    return replies


def _run(m, **args):
    """Par le CHEMIN SERVI (`Tool.run` : validation, threadpool) et non par la seule
    fonction : c'est là qu'une exception se change en « Internal server error »."""
    tool = asyncio.run(m.get_tool("slack_read_thread"))
    return asyncio.run(tool.run(args))


def _structure(res) -> dict:
    return res.structured_content.get("result", res.structured_content)


def test_le_ts_d_une_REPONSE_lit_le_fil_de_son_parent_et_le_dit(slack):
    """Piège sondé le 28/08 : le `ts` d'une RÉPONSE ne rend pas le fil — Slack rend ce
    seul message, `ok:true`. Rendu tel quel, ça dit « ce fil n'a aucune réponse ».
    Slack nomme le parent (`thread_ts`) : on lit SON fil, et la réponse le dit."""
    m, client = slack
    client.replies.side_effect = _slack_fidele(fenetre_vide_la_reponse=False)
    out = _fn(m, "slack_read_thread")(channel="C1", thread_ts=REPONSE_TS)
    assert out["parent"]["ts"] == PARENT_TS
    assert [r["ts"] for r in out["replies"]] == [REPONSE_TS, DERNIERE_TS]
    assert out["thread_ts"] == PARENT_TS and out["requested_ts"] == REPONSE_TS
    assert PARENT_TS in out["note"] and REPONSE_TS in out["note"]
    assert [c.args for c in client.replies.call_args_list] == [
        ("C1", REPONSE_TS), ("C1", PARENT_TS)]


def test_un_vrai_parent_ne_porte_aucune_note_de_redirection(slack):
    m, client = slack
    client.replies.side_effect = _slack_fidele(fenetre_vide_la_reponse=False)
    out = _fn(m, "slack_read_thread")(channel="C1", thread_ts=PARENT_TS)
    assert set(out) == {"parent", "replies", "has_more", "next_cursor"}
    assert client.replies.call_count == 1


@pytest.mark.parametrize("fenetre_vide_la_reponse", [False, True],
                         ids=["slack-rend-la-reponse", "slack-rend-un-fil-vide"])
def test_reponse_en_thread_ts_oldest_egal_limit_1(slack, fenetre_vide_la_reponse):
    """La forme EXACTE : une ronde qui ne garde que les `latest_reply`
    repart avec le ts d'une réponse, `oldest` = ce même ts, `limit=1`. Servi
    « Internal server error », sans le ts du parent : ça se lisait comme une panne.
    Quelle que soit la façon dont Slack répond sous la fenêtre (la réponse seule, ou
    rien), l'appel lit le fil du parent avec la même fenêtre : les réponses APRÈS."""
    m, client = slack
    client.replies.side_effect = _slack_fidele(fenetre_vide_la_reponse)
    out = _structure(_run(m, channel="C1", thread_ts=REPONSE_TS,
                          oldest=REPONSE_TS, limit=1))
    assert out["parent"]["ts"] == PARENT_TS and out["thread_ts"] == PARENT_TS
    assert [r["ts"] for r in out["replies"]] == [DERNIERE_TS]
    assert client.replies.call_count == (3 if fenetre_vide_la_reponse else 2)
    dernier = client.replies.call_args_list[-1]
    assert dernier.args == ("C1", PARENT_TS)
    assert dernier.kwargs["oldest"] == REPONSE_TS and dernier.kwargs["limit"] == 1


@pytest.mark.parametrize("slack_vide_sous_fenetre", [False, True],
                         ids=["slack-rend-toujours-le-message", "slack-vide-sous-fenetre"])
def test_ronde_sans_nouveaute_depuis_la_DERNIERE_reponse(slack, slack_vide_sous_fenetre):
    """Le cas le plus fréquent d'une ronde : `latest_reply` = la dernière réponse, et
    rien de neuf depuis. Ts de réponse, `oldest` = ce ts, `limit=1` : le parent, ZÉRO
    réponse et la note — jamais un refus, que Slack vide ou non la page sous la
    fenêtre (pour la réponse comme pour le parent)."""
    m, client = slack
    derniere = FIL["messages"][2]

    def replies(channel, ts, limit=50, cursor=None, oldest=None, latest=None,
                inclusive=False):
        if ts == DERNIERE_TS:
            if slack_vide_sous_fenetre and oldest:
                return {"ok": True, "messages": []}
            return {"ok": True, "messages": [derniere]}
        assert ts == PARENT_TS
        nouvelles = [r for r in FIL["messages"][1:] if not oldest or r["ts"] > oldest]
        if slack_vide_sous_fenetre and oldest and not nouvelles:
            return {"ok": True, "messages": []}
        return {"ok": True, "has_more": False,
                "messages": [FIL["messages"][0]] + nouvelles}
    client.replies.side_effect = replies
    out = _structure(_run(m, channel="C1", thread_ts=DERNIERE_TS,
                          oldest=DERNIERE_TS, limit=1))
    assert out["parent"]["ts"] == PARENT_TS and out["replies"] == []
    assert out["thread_ts"] == PARENT_TS and out["requested_ts"] == DERNIERE_TS
    assert PARENT_TS in out["note"]
    assert out["has_more"] is False and out["next_cursor"] is None


def test_un_vrai_parent_sans_reponse_dans_la_fenetre_rend_zero_reponse(slack):
    """Si Slack vidait la page d'un VRAI parent sous la fenêtre, le regard sans
    fenêtre le dit parent : zéro réponse, pas un refus — une ronde sans nouveauté
    n'est pas une erreur."""
    m, client = slack

    def replies(channel, ts, limit=50, cursor=None, oldest=None, latest=None,
                inclusive=False):
        return ({"ok": True, "messages": []} if oldest
                else {"ok": True, "messages": [FIL["messages"][0]]})
    client.replies.side_effect = replies
    out = _fn(m, "slack_read_thread")(channel="C1", thread_ts=PARENT_TS, oldest="9.9")
    assert out == {"parent": FIL["messages"][0], "replies": [], "has_more": False,
                   "next_cursor": None}


def test_fil_du_parent_illisible_refus_qui_NOMME_le_parent(slack):
    """Le parent nommé par Slack ne se lit pas comme un parent (paranoïa : jamais
    observé) : pas de boucle, un refus d'entrée qui donne le ts à reprendre —
    `invalid_input` par la taxonomie, jamais « Internal server error » (#1350)."""
    m, client = slack
    client.replies.return_value = {"ok": True, "messages": [UNE_REPONSE]}
    with pytest.raises(McpError) as e:
        _run(m, channel="C1", thread_ts=REPONSE_TS)
    from oto_mcp import error_taxonomy
    info = error_taxonomy.classify(e.value)
    assert info.code == "invalid_input" and PARENT_TS in info.message
    assert client.replies.call_count == 2


def test_un_fil_vide_sans_fenetre_est_un_refus_d_entree(slack):
    m, client = slack
    client.replies.return_value = {"ok": True, "messages": []}
    with pytest.raises(McpError) as e:
        _run(m, channel="C1", thread_ts="1.1")
    from oto_mcp import error_taxonomy
    assert error_taxonomy.classify(e.value).code == "invalid_input"
    assert client.replies.call_count == 1


def test_la_fenetre_et_la_pagination_du_fil_sont_transmises(slack):
    m, client = slack
    client.replies.return_value = FIL
    out = _fn(m, "slack_read_thread")(
        channel="C1", thread_ts="1.1", limit=10, cursor="cur",
        oldest="2.2", latest="3.3", inclusive=True)
    client.replies.assert_called_once_with(
        "C1", "1.1", limit=10, cursor="cur", oldest="2.2", latest="3.3", inclusive=True)
    assert out["has_more"] is False


def test_la_pagination_du_fil_est_remontee(slack):
    m, client = slack
    client.replies.return_value = {
        "ok": True, "has_more": True, "messages": FIL["messages"],
        "response_metadata": {"next_cursor": "dXNlcjpV"}}
    out = _fn(m, "slack_read_thread")(channel="C1", thread_ts="1.1")
    assert out["has_more"] is True and out["next_cursor"] == "dXNlcjpV"


# --- #549 : la fenêtre de lecture, pour cesser de tirer 100 messages ----------

def test_read_history_expose_la_fenetre_oldest_latest(slack):
    """#549, observation mineure : « slack_read_history offers no oldest/latest
    window filter … so a full 100-message page must be pulled and filtered
    client-side for every channel ». Le client oto-core savait déjà le faire ;
    seule la surface ne l'exposait pas."""
    m, client = slack
    client.history.return_value = {"ok": True, "messages": []}
    _fn(m, "slack_read_history")(channel="C1", limit=30, oldest="1786.0",
                                 latest="1787.0", inclusive=True)
    client.history.assert_called_once_with(
        "C1", limit=30, cursor=None, oldest="1786.0", latest="1787.0", inclusive=True)


# --- #549 : rejoindre ce qui est joignable, refuser le reste ------------------

def test_rejoindre_un_canal_public(slack):
    m, client = slack
    client.channel_info.return_value = {"channel": {
        "id": "C0BPJ1KV8A2", "name": "brain-log", "is_private": False,
        "is_member": False, "is_archived": False}}
    client.join_channel.return_value = {"ok": True, "channel": {"id": "C0BPJ1KV8A2"}}
    out = _fn(m, "slack_join_channel")(channel="C0BPJ1KV8A2")
    assert out["joined"] is True and out["channel"]["name"] == "brain-log"
    client.join_channel.assert_called_once_with("C0BPJ1KV8A2")


def test_un_canal_PRIVE_est_refuse_SANS_meme_tenter_de_le_rejoindre(slack):
    """LE point du lot : `conversations.join` ne vaut que pour les canaux publics.
    Sur un privé, oto ne peut RIEN — le dire, nommer le geste humain, et ne pas
    laisser croire à une panne. Tenter l'appel serait déjà faire semblant."""
    m, client = slack
    client.channel_info.return_value = {"channel": {
        "id": "C0BHBS9CCD6", "name": "product-core", "is_private": True,
        "is_member": False, "is_archived": False}}
    with pytest.raises(McpError) as e:
        _fn(m, "slack_join_channel")(channel="C0BHBS9CCD6")
    msg = str(e.value)
    assert "private" in msg and "/invite" in msg
    assert "product-core" in msg
    client.join_channel.assert_not_called()


def test_deja_membre_ne_pretend_pas_avoir_rejoint(slack):
    m, client = slack
    client.channel_info.return_value = {"channel": {
        "id": "C1", "name": "general", "is_private": False,
        "is_member": True, "is_archived": False}}
    out = _fn(m, "slack_join_channel")(channel="C1")
    assert out["joined"] is False and out["already_member"] is True
    client.join_channel.assert_not_called()


def test_un_canal_archive_est_refuse(slack):
    m, client = slack
    client.channel_info.return_value = {"channel": {
        "id": "C1", "name": "vieux-projet", "is_private": False,
        "is_member": False, "is_archived": True}}
    with pytest.raises(McpError, match="archiv"):
        _fn(m, "slack_join_channel")(channel="C1")
    client.join_channel.assert_not_called()


def test_un_canal_introuvable_dit_les_DEUX_causes_possibles(slack):
    """Sondé : un ID faux et un canal PRIVÉ où l'app n'est pas rendent le MÊME
    `channel_not_found`. Ne pas trancher à la place de Slack — dire les deux."""
    m, client = slack
    client.channel_info.side_effect = _err("channel_not_found")
    with pytest.raises(ValueError) as e:
        _fn(m, "slack_join_channel")(channel="C000000DEAD")
    msg = str(e.value)
    assert "private" in msg and "/invite" in msg and "slack_list_channels" in msg


# --- les refus : actionnables, et hors du bruit Sentry -------------------------

def test_missing_scope_nomme_le_droit_que_SLACK_dit_manquer(slack):
    """Deux orgs bloquées là-dessus (#510, #532). Slack NOMME lui-même le droit
    manquant dans `needed` (sondé : `needed=groups:history`) — le refus le relaie
    au lieu de le deviner, et dit où le donner."""
    m, client = slack
    client.replies.side_effect = _err("missing_scope", needed="groups:history",
                                      provided="chat:write,im:history")
    with pytest.raises(ValueError) as e:
        _fn(m, "slack_read_thread")(channel="C1", thread_ts="1.1")
    msg = str(e.value)
    assert "groups:history" in msg          # le droit exact, nommé
    assert "OAuth & Permissions" in msg     # où le donner
    assert "reinstallation" in msg.lower()       # et pourquoi ça ne suffit pas de l'ajouter
    assert "chat:write,im:history" in msg   # ce que Slack a vu, pour lever le doute


def test_missing_scope_sans_needed_n_invente_aucun_scope(slack):
    """Si Slack ne nomme pas le droit, on ne le fabrique pas : un nom de scope
    inventé enverrait l'admin donner le mauvais droit."""
    m, client = slack
    client.replies.side_effect = _err("missing_scope")
    with pytest.raises(ValueError) as e:
        _fn(m, "slack_read_thread")(channel="C1", thread_ts="1.1")
    msg = str(e.value)
    assert "does not name which" in msg and "history" not in msg


def test_not_in_channel_donne_les_deux_gestes_selon_le_type_de_canal(slack):
    """#549 : cinq canaux sur huit rendaient ce code. Le refus doit porter la
    sortie — publique (un outil) ou privée (un humain)."""
    m, client = slack
    client.history.side_effect = _err("not_in_channel")
    with pytest.raises(ValueError) as e:
        _fn(m, "slack_read_history")(channel="C0BKTTN8F6D")
    msg = str(e.value)
    assert "slack_join_channel" in msg      # canal public : oto sait le faire
    assert "/invite" in msg                 # canal privé : seul un humain le peut
    assert "C0BKTTN8F6D" in msg


def test_un_refus_slack_garde_son_4xx_dans_la_chaine(slack):
    """Sans ça, un refus de credential/scope remonte à Sentry comme un bug
    backend : `error_taxonomy` cherche `.status` en REMONTANT la chaîne de causes.
    Un `raise … from None` la couperait."""
    from oto_mcp import error_taxonomy

    m, client = slack
    client.replies.side_effect = _err("missing_scope", needed="channels:history")
    with pytest.raises(ValueError) as e:
        _fn(m, "slack_read_thread")(channel="C1", thread_ts="1.1")
    assert error_taxonomy.upstream_status_in_chain(e.value) == 403


def test_thread_not_found_reste_lisible(slack):
    m, client = slack
    client.replies.side_effect = _err("thread_not_found")
    with pytest.raises(ValueError, match="thread_not_found"):
        _fn(m, "slack_read_thread")(channel="C1", thread_ts="1000000000.000000")


def test_un_refus_sans_canal_n_invente_pas_de_canal(slack):
    """`slack_find_user_by_email` ne vise aucun canal : lui répondre « sur ce
    canal » enverrait chercher au mauvais endroit. Le refus par défaut nomme le
    code Slack, et rien de plus."""
    m, client = slack
    client.find_user_by_email.side_effect = _err("users_not_found")
    with pytest.raises(ValueError) as e:
        _fn(m, "slack_find_user_by_email")(email="jean@exemple.fr")
    msg = str(e.value)
    assert "users_not_found" in msg and "canal" not in msg


# --- surface --------------------------------------------------------------------

def test_les_nouveaux_tools_sont_montes_et_decrits(slack):
    m, _ = slack
    tools = asyncio.run(m._list_tools())
    by_name = {t.name: t for t in tools}
    assert {"slack_read_thread", "slack_join_channel"} <= set(by_name)
    for n in ("slack_read_thread", "slack_join_channel"):
        assert by_name[n].description, f"{n} sans description"


def test_les_appels_slack_restent_VUS_par_la_sonde_version_skew():
    """Non-régression sur un piège rencontré en écrivant ce lot.

    Les appels Slack passent par un seam de traduction des refus. Écrit en
    **fonction** (`_wrap(client.replies, …)`), il aurait fait disparaître le module
    de la sonde version-skew : `test_tools_client_methods_exist` ne compte que les
    attributs **APPELÉS** sur le client, jamais les références nues. La sonde serait
    restée VERTE en ne vérifiant plus rien — le trou vécu sur apollo, en silence.
    D'où le seam en **contexte** (`with _traduit(...)`), qui laisse les appels
    littéraux. Ce test vérifie que la sonde voit bien les méthodes du lot.
    """
    import ast
    from pathlib import Path
    import tests.test_tools_client_methods_exist as guard

    tree = ast.parse(Path(guard._TOOLS_DIR / "slack.py").read_text())
    vues = guard._methods_called_on_client(tree)
    assert {"replies", "join_channel", "channel_info", "history",
            "post_message"} <= vues, (
        "la sonde version-skew ne voit plus ces appels : le seam de refus est "
        f"redevenu une fonction d'enrobage ? vues={sorted(vues)}")


def test_le_budget_de_handshake_des_deux_tools_reste_dense(slack):
    """Une description est recopiée dans CHAQUE session, ~470 outils au total.
    Ce qui est long (la marche à suivre d'un refus) va dans le message d'erreur,
    payé une fois, au moment où il sert."""
    m, _ = slack
    tools = {t.name: t for t in asyncio.run(m._list_tools())}
    for n in ("slack_read_thread", "slack_join_channel"):
        assert len(tools[n].description) < 800, (
            f"{n} : {len(tools[n].description)} c. — la prose longue va dans le refus")
