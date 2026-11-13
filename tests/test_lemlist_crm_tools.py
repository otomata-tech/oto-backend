"""Les tools de la moitié non-campagne (CRM, inbox, désinscriptions, signaux…).

`test_lemlist_surface_coverage.py` prouve que TOUT est atteignable ; ce fichier
ne re-teste donc pas les 13 familles op par op — il verrouille la poignée de
décisions qu'une réécriture casse sans bruit : quel op tape quelle méthode quand
DEUX gestes partagent une route, ce qu'un argument omis vaut, et le refus local
d'un appel incomplet (aucun aller-retour, donc rien qui parte).
"""
import asyncio
from unittest.mock import patch

import pytest


def _tool(name, module="lemlist_crm"):
    from fastmcp import FastMCP
    from oto_mcp.tools import lemlist, lemlist_crm

    m = FastMCP("t")
    {"lemlist": lemlist, "lemlist_crm": lemlist_crm}[module].register(m)
    return asyncio.run(m.get_tool(name))


def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.lemlist.LemlistClient")
    return key, cls


CRM_TOOLS = {
    "lemlist_contact", "lemlist_company", "lemlist_inbox", "lemlist_act",
    "lemlist_unsubscribe", "lemlist_task", "lemlist_watchlist",
    "lemlist_database", "lemlist_team", "lemlist_mailbox",
    "lemlist_deliverability", "lemlist_webhook",
    "lemlist_delete_activity_transcript",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


def test_le_second_module_est_charge_par_le_boot_reel(all_tools):
    """Un fichier de tools posé mais non déclaré au registre dort invisible :
    ce test monte le VRAI `register_all`, pas le module isolé."""
    from oto_mcp.tool_visibility import namespace_of

    assert CRM_TOOLS <= all_tools
    # Le namespace vient du NOM du tool, pas du fichier — un `lemlist_crm_*`
    # tomberait dans un namespace `lemlist_crm` inexistant et ouvrirait le gate.
    assert all(namespace_of(t) == "lemlist" for t in CRM_TOOLS)


@pytest.mark.parametrize("name,module,extra", [
    ("lemlist_contact", "lemlist_crm", {}),
    ("lemlist_company", "lemlist_crm", {}),
    ("lemlist_inbox", "lemlist_crm", {}),
    ("lemlist_unsubscribe", "lemlist_crm", {}),
    ("lemlist_task", "lemlist_crm", {}),
    ("lemlist_watchlist", "lemlist_crm", {}),
    ("lemlist_database", "lemlist_crm", {}),
    ("lemlist_team", "lemlist_crm", {}),
    ("lemlist_mailbox", "lemlist_crm", {}),
    ("lemlist_deliverability", "lemlist_crm", {}),
    ("lemlist_webhook", "lemlist_crm", {}),
    ("lemlist_act", "lemlist_crm",
     {"message": "x", "send_user_id": "usr_1"}),
    ("lemlist_lead", "lemlist", {}),
])
def test_un_op_inconnu_est_refuse_avant_tout_appel(name, module, extra):
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        with pytest.raises(Exception, match="unknown op"):
            _tool(name, module).fn(op="teleport", **extra)
        assert not inst.method_calls


# --- Les routes que DEUX gestes partagent -------------------------------------

def test_supprimer_et_desinscrire_un_lead_se_departagent_par_action():
    """Une seule route lemlist, deux gestes, et son défaut est le DOUX."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_lead", "lemlist")

        tool.fn(op="delete", campaign_id="cam_1", lead_id="lea_1")
        assert inst.delete_lead.call_args.kwargs == {"action": "remove"}

        tool.fn(op="unsubscribe", campaign_id="cam_1", email="a@acme.fr")
        assert inst.delete_lead.call_args.kwargs == {"action": None}
        assert inst.delete_lead.call_args.args == ("cam_1", "a@acme.fr")


def test_pause_dun_lead_sans_campagne_vise_toutes_les_campagnes():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_lead", "lemlist")

        tool.fn(op="pause", lead_id="lea_1")
        assert inst.pause_lead.call_args.kwargs == {"campaign_id": None}

        tool.fn(op="pause", lead_id="lea_1", campaign_id="cam_1")
        assert inst.pause_lead.call_args.kwargs == {"campaign_id": "cam_1"}


def test_les_trois_listes_de_desinscription_ne_se_confondent_pas():
    """v1 emails, v2 variables, v2 contacts : écrire dans l'une n'écrit pas
    dans les autres, et le dispatch doit le refléter."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_unsubscribe")

        tool.fn(op="add", email="a@acme.fr")
        inst.add_unsubscribe.assert_called_once_with("a@acme.fr")

        tool.fn(op="var_add", value="acme.fr")
        inst.unsubscribe_variable.assert_called_once_with("acme.fr")
        inst.add_unsubscribe.assert_called_once()      # toujours une seule fois

        tool.fn(op="contact_add", contact_id="ctc_1")
        inst.unsubscribe_contact.assert_called_once_with("ctc_1")


@pytest.mark.parametrize("op,method,key", [
    ("list", "list_unsubscribes", "unsubscribes"),
    ("var_list", "list_unsubscribed_variables", "variables"),
])
def test_une_liste_de_desinscription_omise_ne_rend_quune_page(op, method, key):
    """Sans `limit`, lemlist rend la liste ENTIÈRE (2 439 lignes, 325 Ko vus sur
    un compte réel) : l'agent qui la lit pour vérifier une valeur se noie. Le
    tool sert une page de 100 et dit où reprendre."""
    key_patch, cls = _with_fake_client()
    with key_patch, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_unsubscribe")

        getattr(inst, method).return_value = [{"value": f"v{i}"} for i in range(100)]
        out = tool.fn(op=op)
        assert getattr(inst, method).call_args.kwargs == {"offset": None, "limit": 100}
        assert len(out[key]) == 100
        assert (out["limit"], out["offset"], out["next_offset"]) == (100, 0, 100)

        # Une page incomplète est la dernière ; un `limit` explicite est tenu.
        getattr(inst, method).return_value = [{"value": "v"}] * 3
        out = tool.fn(op=op, limit=20, offset=40)
        assert getattr(inst, method).call_args.kwargs == {"offset": 40, "limit": 20}
        assert (out["limit"], out["offset"], out["next_offset"]) == (20, 40, None)

        # Un `limit` au-delà du plafond lemlist (100) peut revenir écrêté : la
        # page pleine n'est pas la dernière, et la suite part des lignes reçues.
        getattr(inst, method).return_value = [{"value": "v"}] * 100
        out = tool.fn(op=op, limit=500, offset=200)
        assert (out["limit"], out["offset"], out["next_offset"]) == (500, 200, 300)


def test_ajouter_ou_retirer_dune_liste_de_contacts_partage_une_route():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_contact")

        tool.fn(op="list_manage", list_id="lst_1", contact_ids=["ctc_1"])
        assert inst.manage_contact_list.call_args.kwargs == {"action": None}

        tool.fn(op="list_manage", list_id="lst_1", contact_ids=["ctc_1"],
                action="remove")
        assert inst.manage_contact_list.call_args.kwargs == {"action": "remove"}


# --- Ce qui envoie : refus local, et masquage ----------------------------------

def test_un_envoi_incomplet_est_refuse_sans_aller_retour():
    """Le refus AU BORD compte plus qu'ailleurs ici : c'est le seul tool qui
    parle à une personne réelle sans campagne ni revue devant lui."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_act")

        with pytest.raises(Exception, match="send_user_mailbox_id"):
            tool.fn(op="send_email", message="hi", send_user_id="usr_1",
                    send_user_email="me@x.fr")
        with pytest.raises(Exception, match="lead_id"):
            tool.fn(op="send_linkedin", message="hi", send_user_id="usr_1")
        assert not inst.method_calls

        tool.fn(op="send_email", message="hi", send_user_id="usr_1",
                send_user_email="me@x.fr", send_user_mailbox_id="mbx_1")
        assert inst.send_inbox_email.call_args.kwargs["send_user_mailbox_id"] == "mbx_1"


def test_auto_review_a_son_propre_tool_et_construit_le_patch():
    """Le champ reste atteignable — c'est le GESTE qui devient explicite."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign_auto_review", "lemlist")

        tool.fn(campaign_id="cam_1", enabled=True, conditions=["deliverable"])
        inst.update_campaign.assert_called_once_with("cam_1", {
            "autoReview": True, "autoReviewConditions": ["deliverable"]})

        tool.fn(campaign_id="cam_1", enabled=False)
        assert inst.update_campaign.call_args.args[1] == {"autoReview": False}


def test_le_dict_de_reglages_de_campagne_ne_peut_pas_armer_lenvoi():
    """L'autre moitié du même contrat : la porte de service reste fermée."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign", "lemlist")

        with pytest.raises(Exception, match="lemlist_campaign_auto_review"):
            tool.fn(op="update", campaign_id="cam_1",
                    settings={"autoReview": True})
        inst.update_campaign.assert_not_called()


# --- L'audio d'une note vocale : la seule entrée de FICHIER du connecteur -------

def test_les_activites_savent_marcher_les_pages():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.sync_activities.return_value = [{"type": "emailsSent"}]
        tool = _tool("lemlist_get_activities", "lemlist")

        out = tool.fn(campaign_id="cam_1", all_pages=True, since="2026-08-01")
        inst.sync_activities.assert_called_once_with(
            campaign_id="cam_1", since="2026-08-01", max_pages=50)
        inst.get_activities.assert_not_called()
        assert out["count"] == 1


# --- Les activités : une page lisible (signal d'usage 1464) -----------------------
# Chaque activité porte son lead entier (toutes les copies de la séquence, en HTML) :
# 100 activités dépassent ce qu'un run d'agent lit. Le client d'oto-core façonne la
# liste (`fields`, `compact`) ; le tool transmet, et rend en paramètre invalide ce que
# le client refuse.

@pytest.mark.parametrize("all_pages,method", [(False, "get_activities"),
                                              (True, "sync_activities")])
def test_la_vue_des_activites_est_transmise_au_client(all_pages, method):
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        getattr(inst, method).return_value = [{"type": "emailsSent"}]
        out = _tool("lemlist_get_activities", "lemlist").fn(
            campaign_id="cam_1", all_pages=all_pages, compact=True,
            fields=["lead.variables.email"])
        kwargs = getattr(inst, method).call_args.kwargs
        assert kwargs["compact"] is True
        assert kwargs["fields"] == ["lead.variables.email"]
        assert kwargs["campaign_id"] == "cam_1"
        assert out["count"] == 1


@pytest.mark.parametrize("all_pages,method", [(False, "get_activities"),
                                              (True, "sync_activities")])
def test_sans_vue_l_appel_des_activites_est_inchange(all_pages, method):
    """Ni `fields` ni `compact` : rien de neuf ne part au client — l'appel est celui
    d'avant, quel que soit l'oto-core installé."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        getattr(inst, method).return_value = []
        _tool("lemlist_get_activities", "lemlist").fn(all_pages=all_pages)
        kwargs = getattr(inst, method).call_args.kwargs
        assert "fields" not in kwargs and "compact" not in kwargs


def test_une_vue_des_activites_refusee_par_le_client_est_un_parametre_invalide():
    from mcp.types import INVALID_PARAMS
    from oto_mcp.mcp_errors import McpError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.get_activities.side_effect = ValueError(
            "fields is a non-empty list of field names")
        with pytest.raises(McpError, match="non-empty list") as exc:
            _tool("lemlist_get_activities", "lemlist").fn(fields=[])
    assert exc.value.error.code == INVALID_PARAMS


def test_une_erreur_de_transport_des_activites_n_est_pas_un_parametre_invalide():
    """`requests` a ses propres ValueError (corps non JSON, URL invalide) : elles ne
    disent rien des arguments. Et sans vue demandée, une ValueError reste la sienne."""
    import requests

    from oto_mcp.mcp_errors import McpError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_activities.side_effect = requests.exceptions.InvalidJSONError("not json")
        with pytest.raises(requests.exceptions.InvalidJSONError):
            _tool("lemlist_get_activities", "lemlist").fn(compact=True)
        inst.get_activities.side_effect = ValueError("x")
        with pytest.raises(ValueError) as exc:
            _tool("lemlist_get_activities", "lemlist").fn()
        assert not isinstance(exc.value, McpError)


def _activites_doublees(monkeypatch, body):
    """Le vrai client d'oto-core, seul `requests` doublé : ce qui part à lemlist."""
    import json

    import requests

    sent = []
    texte = json.dumps(body)

    class _Rep:
        status_code = 200
        ok = True
        headers = {}
        text = texte
        content = texte.encode()

        def json(self):
            return body

    def _request(method, url, **kw):
        sent.append((method, url, kw.get("params")))
        return _Rep()

    monkeypatch.setattr(requests, "request", _request)
    return sent


def test_compact_des_activites_atteint_le_vrai_client(monkeypatch):
    """Les tests ci-dessus doublent le client et ne voient pas qu'un oto-core sans la
    vue lève `TypeError` au premier vrai appel. Sauté tant que l'oto-core installé n'a
    pas `activity_views` (lemlist 1.4.0) ; il mord dès que le pin l'inclut."""
    pytest.importorskip("oto.tools.lemlist.activity_views")
    activity = {
        "_id": "act_1", "type": "emailsSent", "createdAt": "2026-01-02T10:00:00Z",
        "campaignId": "cam_1", "leadEmail": "ada@example.test", "subject": "An idea",
        "messagePreview": "Hello Ada,",
        "lead": {"variables": {f"h_e{n}": "<p>copy</p>" * 500 for n in range(1, 6)}},
        "metaData": {"teamId": "tea_1"},
    }
    sent = _activites_doublees(monkeypatch, [activity])
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)):
        out = _tool("lemlist_get_activities", "lemlist").fn(compact=True)

    assert len(sent) == 1 and sent[0][0] == "GET"
    assert "compact" not in (sent[0][2] or {}), "la vue ne part jamais à lemlist"
    (event,) = out["activities"]
    assert event == {"_id": "act_1", "type": "emailsSent",
                     "createdAt": "2026-01-02T10:00:00Z", "campaignId": "cam_1",
                     "leadEmail": "ada@example.test", "subject": "An idea",
                     "excerpt": "Hello Ada,"}


def test_des_fields_d_activites_malformes_sont_refuses_avant_toute_requete(monkeypatch):
    from mcp.types import INVALID_PARAMS
    from oto_mcp.mcp_errors import McpError

    pytest.importorskip("oto.tools.lemlist.activity_views")
    sent = _activites_doublees(monkeypatch, [])
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)):
        with pytest.raises(McpError) as exc:
            _tool("lemlist_get_activities", "lemlist").fn(fields=["a..b"])
    assert exc.value.error.code == INVALID_PARAMS
    assert not sent


def test_les_credentials_de_boite_mail_ne_partent_pas_en_clair_au_journal():
    """`tool_calls` est lue par les surfaces de supervision : un mot de passe
    SMTP qui y arrive en clair y reste. Le masquage traverse le sous-dict."""
    from oto_mcp.calllog import truncated_args

    out = truncated_args(
        {"op": "connect", "smtp_imap": {
            "sender_email": "me@x.fr", "smtp_password": "hunter2",
            "imap_password": "hunter2", "smtp_host": "smtp.x.fr"}},
        tool="lemlist_mailbox")

    ligne = str(out)
    assert "hunter2" not in ligne
    assert "smtp.x.fr" in ligne          # le non-secret reste lisible
    assert "me@x.fr" in ligne

    secret = truncated_args({"secret": "sig-key"}, tool="lemlist_webhook")
    assert "sig-key" not in str(secret)

    # Et le masquage reste NOMINATIF : un `secret` sur un autre outil passe.
    assert truncated_args({"secret": "sig-key"}, tool="lemlist_campaign") == {
        "secret": "sig-key"}


def test_laudio_passe_par_le_seam_partage_et_sa_garde_ssrf():
    """Écrit à la main, ce chemin refaisait la borne de taille mais PAS
    l'anti-SSRF — une URL d'agent pouvait viser localhost ou l'IMDS."""
    from oto_mcp.tools import lemlist

    with patch("oto_mcp.file_source.resolve") as resolve:
        resolve.return_value.data = b"\x00\x01"
        resolve.return_value.filename = "voix.mp3"
        # Le nom de fichier vient de la SOURCE et part en multipart : le perdre
        # ferait arriver toutes les notes vocales sous le même nom générique.
        assert lemlist._fetch_audio("https://x.fr/a.mp3") == (b"\x00\x01", "voix.mp3")
        assert resolve.call_args.args[0] == {"kind": "url", "url": "https://x.fr/a.mp3"}
        assert resolve.call_args.kwargs["max_bytes"] == 20 * 1024 * 1024

    # Une source Drive/Gmail passe telle quelle — le seam les gère aussi.
    with patch("oto_mcp.file_source.resolve") as resolve:
        resolve.return_value.data = b""
        resolve.return_value.filename = None
        assert lemlist._fetch_audio({"kind": "drive", "file_id": "f1"})[1] == "audio.mp3"
        assert resolve.call_args.args[0] == {"kind": "drive", "file_id": "f1"}

    from oto_mcp import file_source
    with patch("oto_mcp.file_source.resolve",
               side_effect=file_source.FileSourceError("cible non autorisée")):
        with pytest.raises(Exception, match="audio illisible"):
            lemlist._fetch_audio("https://169.254.169.254/latest")


def test_le_masquage_des_credentials_survit_a_un_registre_illisible():
    """Le masquage dérivé (capacités) et le masquage déclaré (connecteurs) ne
    doivent pas partager un sort : si le registre ne charge pas, les mots de
    passe SMTP ne peuvent pas repartir en clair pour autant — le seul signal
    serait un warning qui parle d'autre chose."""
    from oto_mcp import journal_secrets

    journal_secrets._ARG_NAMES = None
    try:
        with patch("oto_mcp.capabilities.registry.caps_with_mcp",
                   side_effect=RuntimeError("registre cassé")):
            table = journal_secrets.secret_arg_names("lemlist_mailbox")
        assert "smtp_password" in table and "imap_password" in table
    finally:
        journal_secrets._ARG_NAMES = None


# --- Écarts doc↔API relevés en LIVE le 2026-08-31 ------------------------------

def test_creer_une_campagne_annonce_quelle_tourne_deja():
    """Le retour de lemlist porte `state: running` ET un `status: draft` :
    l'agent qui lit le second croit la campagne à l'arrêt. Le tool le dit."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.create_campaign.return_value = {
            "_id": "cam_1", "state": "running", "status": "draft"}
        out = _tool("lemlist_campaign", "lemlist").fn(op="create", name="Q4")

        assert "pause" in out["warning"]
        assert out["_id"] == "cam_1"          # la charge d'origine est gardée


def test_creer_une_tache_exige_le_record_id():
    """`recordId` est documenté optionnel et l'API le refuse (400)."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_task")

        with pytest.raises(Exception, match="record_id"):
            tool.fn(op="create", task_type="manual", assigned_to="usr_1",
                    due_date="2026-09-05")
        inst.create_task.assert_not_called()

        tool.fn(op="create", task_type="manual", assigned_to="usr_1",
                due_date="2026-09-05", record_id="ctc_1")
        assert inst.create_task.call_args.kwargs["record_id"] == "ctc_1"


def test_creer_une_watchlist_minimale_nenvoie_pas_de_None():
    """Le client a des défauts parce que l'API EXIGE ces champs ; les passer à
    None depuis le tool rétablirait le 400 qu'ils évitent. Vérifié sur l'appel
    SORTANT, seul endroit où ça se voit — le live est passé par le client."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        _tool("lemlist_watchlist").fn(op="create", name="Levées",
                                      watch_type="companyRaisedFunds")

        kwargs = inst.create_watch_list.call_args.kwargs
        for absent in ("segment_type", "signal_processing_type", "activate",
                       "filters", "emoji", "signal_opportunity_template"):
            assert absent not in kwargs, f"{absent} passé à None"
        assert kwargs == {"type": "companyRaisedFunds"}


def test_une_watchlist_visiteurs_transmet_persona_et_campagne():
    """Un visiteur du site arrive surtout comme ENTREPRISE : pour alimenter une
    campagne, lemlist veut le persona et la campagne dès la création."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        _tool("lemlist_watchlist").fn(
            op="create", name="Visiteurs",
            watch_type="companyEmployeeVisitedMyWebsite",
            signal_processing_type="push_to_campaign",
            persona_id="pdp_1", campaign_id="cam_1")

        kwargs = inst.create_watch_list.call_args.kwargs
        assert kwargs["persona_id"] == "pdp_1"
        assert kwargs["campaign_id"] == "cam_1"
        assert "activate" not in kwargs


def test_une_watchlist_visiteurs_atteint_le_vrai_client(monkeypatch):
    """Le VRAI client d'oto-core, seul `requests` doublé : le test ci-dessus double
    le client et ne voyait pas qu'un oto-core qui ignore `persona_id` lève
    `TypeError` au premier vrai appel."""
    import requests

    sent = {}

    class _Rep:
        status_code = 200
        ok = True
        headers = {}
        text = '{"_id": "wat_1"}'
        content = text.encode()

        def json(self):
            return {"_id": "wat_1"}

    def _request(method, url, **kw):
        sent.update(method=method, url=url, json=kw.get("json"))
        return _Rep()

    monkeypatch.setattr(requests, "request", _request)
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)):
        _tool("lemlist_watchlist").fn(
            op="create", name="Visiteurs",
            watch_type="companyEmployeeVisitedMyWebsite",
            signal_processing_type="push_to_campaign",
            persona_id="pdp_1", campaign_id="cam_1")

    assert sent["method"] == "POST"
    assert sent["json"]["personaId"] == "pdp_1"
    assert sent["json"]["campaignId"] == "cam_1"


@pytest.mark.parametrize("extra", [{"persona_id": "pdp_1"}, {"campaign_id": "cam_1"}])
def test_persona_ou_campagne_hors_push_to_campaign_refuse_au_bord(extra):
    """Le client lève `ValueError`, qui arriverait à l'agent en erreur interne :
    le tool refuse avant, en paramètre invalide, sans aller-retour."""
    from oto_mcp.mcp_errors import McpError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        with pytest.raises(McpError, match="push_to_campaign"):
            _tool("lemlist_watchlist").fn(
                op="create", name="Visiteurs",
                watch_type="companyEmployeeVisitedMyWebsite", **extra)
        client_cls.return_value.create_watch_list.assert_not_called()


def test_exporter_les_leads_nenvoie_pas_state_None():
    """Même piège : `state=None` écraserait le défaut `all` du client, et
    l'export rendrait la liste vide qui se lit « pas de leads »."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.export_campaign_leads.return_value = []
        _tool("lemlist_campaign", "lemlist").fn(op="export_leads",
                                                campaign_id="cam_1")

        assert inst.export_campaign_leads.call_args.kwargs == {}

        _tool("lemlist_campaign", "lemlist").fn(
            op="export_leads", campaign_id="cam_1", state="scanned")
        assert inst.export_campaign_leads.call_args.kwargs == {"state": "scanned"}


# --- Un message trop lourd pour être lu se lit en texte -------------------------

_IMAGE = "data:image/png;base64," + "iVBORw0KGgo" * 30000


def test_les_messages_bruts_sont_rendus_tels_quels_par_defaut():
    """Opt-in : qui lit le HTML brut aujourd'hui le garde."""
    key, cls = _with_fake_client()
    brut = [{"html": f"<p>Bonjour</p><img src=\"{_IMAGE}\">"}]
    with key, cls as client_cls:
        client_cls.return_value.get_contact_messages.return_value = brut
        out = _tool("lemlist_inbox").fn(op="messages", contact_id="ctc_1")
    assert out == {"messages": brut}


def test_text_only_rend_le_texte_sans_les_images_collees():
    """Un email avec une signature en base64 pesait 300 000 caractères pour une
    ligne de texte : c'est ce texte que l'agent venait chercher."""
    key, cls = _with_fake_client()
    brut = [{"id": "m1", "html": "<html><head><style>p{}</style></head><body>"
             "<p>Bonjour,</p><p>Oui, appelons-nous jeudi&nbsp;!</p>"
             f"<img src=\"{_IMAGE}\"></body></html>"}]
    with key, cls as client_cls:
        client_cls.return_value.get_contact_messages.return_value = brut
        out = _tool("lemlist_inbox").fn(op="messages", contact_id="ctc_1",
                                        text_only=True)
    assert out["messages"] == [{"id": "m1",
                                "html": "Bonjour,\nOui, appelons-nous jeudi !"}]
    assert out["truncated_fields"] == 0


def test_max_chars_coupe_et_dit_combien_de_textes_sont_coupes():
    key, cls = _with_fake_client()
    brut = [{"id": "m1", "text": "a" * 50}, {"id": "m2", "text": "court"}]
    with key, cls as client_cls:
        client_cls.return_value.get_contact_messages.return_value = brut
        out = _tool("lemlist_inbox").fn(op="messages", contact_id="ctc_1",
                                        max_chars=10)
    assert out["messages"][0]["text"].startswith("a" * 10 + " [... truncated, 40")
    assert out["messages"][1]["text"] == "court"
    assert out["truncated_fields"] == 1


@pytest.mark.parametrize("max_chars", [0, -5])
def test_max_chars_nul_est_refuse(max_chars):
    """0 n'est pas « pas de limite » : rendu brut sans un mot, l'agent croirait
    avoir demandé une coupe."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.get_contact_messages.return_value = []
        with pytest.raises(Exception, match="max_chars"):
            _tool("lemlist_inbox").fn(op="messages", contact_id="ctc_1",
                                      max_chars=max_chars)


# --- La base partagée : elle lit, ses personas passent à `lemlist_act` (23/10/2026) ----

def test_la_base_partagee_se_declare_en_lecture_et_ses_personas_vont_a_l_acteur():
    """Une recette n'appelle que des outils déclarés en lecture : la recherche dans la
    base partagée doit l'être ; créer et supprimer un persona est un acte, celui de
    l'acteur unique du connecteur."""
    from oto_mcp.tools.lecture import en_lecture

    lecture = _tool("lemlist_database")
    assert en_lecture(lecture)
    ops = lecture.parameters["properties"]["op"]["enum"]
    assert ops == ["people", "companies", "filters", "personas"]
    assert not {"name", "persona_id"} & set(lecture.parameters["properties"])
    act = _tool("lemlist_act")
    assert not en_lecture(act)
    assert {"create_persona", "delete_persona"} <= set(
        act.parameters["properties"]["op"]["enum"])


@pytest.mark.parametrize("op,acte", [("persona_create", "create_persona"),
                                     ("persona_delete", "delete_persona")])
def test_une_op_de_persona_sur_la_lecture_refuse_en_nommant_l_acteur(op, acte):
    """Le vrai chemin : la signature refuse l'op, la taxonomie nomme le nouvel appel
    (`outils_retires.OPS_DEPLACEES`), sans appeler lemlist."""
    from fastmcp import FastMCP
    from oto_mcp import error_taxonomy
    from oto_mcp.tools import lemlist_crm

    m = FastMCP("t")
    lemlist_crm.register(m)
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        try:
            asyncio.run(m.call_tool("lemlist_database", {"op": op}))
        except Exception as e:  # noqa: BLE001 — la taxonomie la classe ci-dessous
            info = error_taxonomy.classify(e)
        else:
            pytest.fail(f"lemlist_database(op={op!r}) ne doit plus rien servir")
        assert not client_cls.called
    assert info.code == "invalid_input"
    assert f'`lemlist_act(op="{acte}", …)`' in info.message


def test_les_personas_passent_par_l_acteur():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        act = _tool("lemlist_act")
        act.fn(op="create_persona", name="CX leads", mode="leads",
               filters=[{"filterId": "department", "in": ["Customer service"]}])
        assert inst.create_persona.call_args.args == ("CX leads",)
        assert inst.create_persona.call_args.kwargs["mode"] == "leads"
        act.fn(op="delete_persona", persona_id="pdp_1")
        assert inst.delete_persona.call_args.args == ("pdp_1",)
        assert not inst.send_inbox_email.called


def test_un_acte_incomplet_est_refuse_sans_aller_retour():
    """Les arguments d'un envoi ne sont plus exigés par la signature (un persona n'en a
    pas) : le bord les exige, op par op."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        act = _tool("lemlist_act")
        with pytest.raises(Exception, match="message"):
            act.fn(op="send_email", send_user_id="usr_1", send_user_email="me@x.fr",
                   send_user_mailbox_id="mbx_1")
        with pytest.raises(Exception, match="mode"):
            act.fn(op="create_persona", name="CX leads")
        with pytest.raises(Exception, match="persona_id"):
            act.fn(op="delete_persona")
        assert not inst.method_calls


def test_dupliquer_une_campagne_annonce_que_la_copie_tourne():
    """#1348 : la copie revient `status: draft` et `op=reports` la montre `running`.
    La description promettait un brouillon ; la réponse dit de mettre en pause."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.duplicate_campaign.return_value = {"_id": "cam_2", "status": "draft"}
        tool = _tool("lemlist_campaign", "lemlist")
        out = tool.fn(op="duplicate", campaign_id="cam_1")

        assert "pause" in out["warning"] and "running" in out["warning"]
        assert out["_id"] == "cam_2"
        assert "fresh DRAFT" not in tool.description
        assert "duplicated campaign lands in DRAFT" not in " ".join(tool.description.split())
