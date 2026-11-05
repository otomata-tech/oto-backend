"""Tools `outlook_*` du connecteur Outlook (service du porteur `microsoft`).

Ce que ce fichier verrouille :
- la SURFACE (2 tools) et le jeton : celui de l'appelant pour le service `outlook`
  (`auth/microsoft.access_token_for`), seul passé au client ; un compte qui n'a pas
  autorisé Outlook → le refus nommé du porteur, avant de construire le client ;
- le dispatch des ops de `outlook` et `outlook_act` vers les VRAIES méthodes de `MailClient`
  (client `create_autospec` sur la classe de la lib : une signature fausse casse ici) ;
- la vue resserrée par défaut, le brut avec `full=True`, `fields` ;
- les déplacements (archive, trash, move) : nouvel id rendu, échec partiel nommé ;
- `outlook(op="draft")` écrit un BROUILLON, `outlook_act(op="send")` l'envoie (par son
  `draft_id`, tel quel) ou envoie un message complet = brouillon puis
  `send_draft` puis relecture de la copie envoyée (son `webLink`, ou `null` et une
  `note` si elle n'est pas encore visible), `kind` qui dit l'acte, réponse sans
  destinataires mais avec pièces, pièces jointes résolues en octets et bornées à
  3 Mo avant toute écriture ;
- le corps : exactement un de `body` (markdown), `html` ou `html_source` (HTML
  transmis À L'IDENTIQUE jusqu'à la requête Graph, aucun passage markdown), un
  `html_source` borné à 1 Mo ; l'image en ligne (`content_id` → `isInline`) ; le
  `webLink` du brouillon rendu.
"""
import asyncio
import base64
from unittest.mock import create_autospec

import pytest
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    """Le faux `MailClient` (autospec de la vraie classe) et les jetons reçus."""
    import oto.tools.microsoft as coeur

    inst = create_autospec(coeur.MailClient, instance=True)
    jetons = []

    def fabrique(jeton):
        jetons.append(jeton)
        return inst

    monkeypatch.setattr(coeur, "MailClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")

    def jeton(sub, service):
        assert service == "outlook", "le jeton se demande pour CE service"
        return f"AT-{sub}"

    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for", jeton)
    inst.jetons = jetons
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import outlook as X

    m = FastMCP("t")
    X.register(m)
    return m


def _tool(name):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, {"error": {"message": "nope"}}, service="microsoft")


MSG = {"id": "m1", "subject": "Facture", "bodyPreview": "Bonjour",
       "from": {"emailAddress": {"name": "Jane", "address": "jane@contoso.example"}},
       "toRecipients": [{"emailAddress": {"address": "moi@contoso.example"}}],
       "isRead": False, "hasAttachments": True, "receivedDateTime": "2026-10-08T09:00:00Z",
       "internetMessageHeaders": ["lourd"]}


# --- surface & jeton ------------------------------------------------------------

def test_surface(client):
    assert {t.name for t in asyncio.run(_mcp().list_tools())} == {"outlook", "outlook_act"}


def test_le_client_porte_le_jeton_de_l_appelant(client):
    client.search_messages.return_value = []
    _tool("outlook")()
    assert client.jetons == ["AT-sub-1"]


def test_un_compte_sans_outlook_recoit_le_refus_du_porteur(client, monkeypatch):
    def refus(sub, service):
        raise RuntimeError("The Microsoft account jane@contoso.example has not yet "
                           "authorized Outlook: connect Outlook from its card")

    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for", refus)
    with pytest.raises(McpError, match="has not yet authorized Outlook"):
        _tool("outlook")()
    assert client.jetons == []


def test_op_inconnue_refusee_avant_le_jeton(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("outlook")(op="delete")
    assert client.jetons == []


# --- lecture --------------------------------------------------------------------

def test_search_vue_resserree_brut_et_fields(client):
    client.search_messages.return_value = [dict(MSG)]
    out = _tool("outlook")(query="from:jane", folder="inbox", limit=5)
    client.search_messages.assert_called_once_with("from:jane", folder="inbox", top=5,
                                                   unread=None)
    m = out["messages"][0]
    assert m["from"] == "Jane <jane@contoso.example>" and m["preview"] == "Bonjour"
    assert "internetMessageHeaders" not in m and out["count"] == 1
    assert _tool("outlook")(full=True)["messages"] == [MSG]
    assert _tool("outlook")(fields=["id", "subject"])["messages"] == [
        {"id": "m1", "subject": "Facture"}]


def test_query_et_unread_s_excluent_avec_le_message_de_la_lib(client):
    # La VRAIE règle de la lib, rejouée sur un client réel : `$search` et `$filter`.
    from oto.tools.microsoft.mail import MailClient

    client.search_messages.side_effect = lambda *a, **k: MailClient.search_messages(
        MailClient("AT"), *a, **k)
    with pytest.raises(McpError, match="unread cannot be combined with query"):
        _tool("outlook")(query="facture", unread=True)


def test_drafts_et_folders(client):
    client.search_messages.return_value = []
    _tool("outlook")(op="drafts", limit=3)
    client.search_messages.assert_called_once_with(folder="drafts", top=3)
    client.list_folders.return_value = [{"id": "f1", "displayName": "Inbox",
                                         "unreadItemCount": 2, "x": 1}]
    out = _tool("outlook")(op="folders")
    assert out["folders"] == [{"id": "f1", "displayName": "Inbox", "totalItemCount": None,
                               "unreadItemCount": 2, "childFolderCount": None}]


def test_get_rend_le_corps_et_les_pieces(client):
    client.get_message.return_value = {**MSG, "body": {"content": "Bonjour Jane"}}
    client.list_attachments.return_value = [
        {"id": "a1", "name": "f.pdf", "contentType": "application/pdf", "size": 9,
         "lastModifiedDateTime": "x"}]
    out = _tool("outlook")(op="get", message_id="m1")
    client.get_message.assert_called_once_with("m1")
    assert out["body"] == "Bonjour Jane"
    assert out["attachments"] == [{"id": "a1", "name": "f.pdf",
                                   "contentType": "application/pdf", "size": 9,
                                   "isInline": None}]


def test_piece_jointe_par_nom_rendue_pour_l_agent(client, monkeypatch):
    client.list_attachments.return_value = [{"id": "a1", "name": "f.pdf"}]
    client.get_attachment.return_value = {
        "name": "f.pdf", "contentType": "application/pdf",
        "contentBytes": base64.b64encode(b"%PDF").decode()}
    vus = []
    monkeypatch.setattr("oto_mcp.file_content.render_for_agent",
                        lambda data, nom, mime, **kw: vus.append((data, nom, mime, kw))
                        or {"encoding": "text"})
    _tool("outlook")(op="attachment", message_id="m1", filename="f.pdf")
    client.get_attachment.assert_called_once_with("m1", "a1")
    data, nom, mime, kw = vus[0]
    assert (data, nom, mime) == (b"%PDF", "f.pdf", "application/pdf")
    assert kw["prefix"] == "outlook-attachments" and kw["sub"] == "sub-1"


def test_piece_jointe_image_revient_en_image_sans_url(client, monkeypatch):
    """#1522 — une image jointe revient en bloc image MCP, sans dépôt ni URL signée
    (injoignable d'un bac à sable) ; les autres ops gardent leur dict."""
    from _pdf_texte import jpeg_minimal
    from fastmcp.tools.tool import ToolResult
    from mcp.types import ImageContent

    jpeg = jpeg_minimal()
    client.get_attachment.return_value = {
        "name": "photo.jpg", "contentType": "image/jpeg",
        "contentBytes": base64.b64encode(jpeg).decode()}
    monkeypatch.setattr("oto_mcp.media_store.upload_private",
                        lambda *a, **k: pytest.fail("aucun dépôt attendu"))
    out = _tool("outlook")(op="attachment", message_id="m1", attachment_id="a1")
    assert isinstance(out, ToolResult)
    texte, image = out.content
    assert "photo.jpg" in texte.text
    assert isinstance(image, ImageContent) and image.mimeType == "image/jpeg"
    assert base64.b64decode(image.data) == jpeg


def test_piece_jointe_ambigue_ou_non_fichier_refusee(client):
    client.list_attachments.return_value = [{"id": "a1", "name": "x"}, {"id": "a2",
                                                                        "name": "x"}]
    with pytest.raises(McpError, match="attachment_id"):
        _tool("outlook")(op="attachment", message_id="m1", filename="x")
    client.get_attachment.return_value = {"name": "Fwd", "@odata.type":
                                          "#microsoft.graph.itemAttachment"}
    with pytest.raises(McpError, match="not a file attachment"):
        _tool("outlook")(op="attachment", message_id="m1", attachment_id="a1")


def test_argument_d_une_autre_op_refuse(client):
    with pytest.raises(McpError, match="does not use `attachment_id`"):
        _tool("outlook")(attachment_id="a1")
    with pytest.raises(McpError, match="only apply when composing"):
        _tool("outlook")(to=["a@b.example"])
    with pytest.raises(McpError, match="op='attachment'"):
        _tool("outlook")(sheet=0)


# --- déplacements ---------------------------------------------------------------

@pytest.mark.parametrize("op, cible", [("archive", "archive"), ("trash", "deleteditems")])
def test_archive_et_trash_deplacent_et_rendent_l_id_de_graph(client, op, cible):
    # Ids immuables : Graph rend le même id ; un id à l'ancien format revient converti.
    client.move.side_effect = lambda mid, dest: {"id": {"m1": "m1", "rest-m2": "imm-m2"}[mid]}
    out = _tool("outlook_act")(op=op, message_ids=["m1", "rest-m2"])
    assert [c.args for c in client.move.call_args_list] == [("m1", cible),
                                                            ("rest-m2", cible)]
    assert out == {"destination": cible, "moved": [
        {"id": "m1", "new_id": "m1"}, {"id": "rest-m2", "new_id": "imm-m2"}]}
    client.delete.assert_not_called()


def test_move_exige_sa_destination_et_un_echec_partiel_se_nomme(client):
    with pytest.raises(McpError, match="requires `destination`"):
        _tool("outlook_act")(op="move", message_ids=["m1"])
    with pytest.raises(McpError, match="requires `message_ids`"):
        _tool("outlook_act")(op="move", message_ids=[], destination="f1")
    client.move.side_effect = [{"id": "n1"}, _upstream(404), {"id": "n3"}]
    with pytest.raises(McpError) as e:
        _tool("outlook_act")(op="move", message_ids=["m1", "m2", "m3"],
                                 destination="f1")
    assert "PARTIAL" in str(e.value) and "'n1'" in str(e.value) and "['m3']" in str(e.value)
    assert client.move.call_count == 2


def test_un_403_se_dit_en_droits_un_429_reste_reessayable(client):
    from oto.tools.common.errors import UpstreamHTTPError

    client.search_messages.side_effect = _upstream(403)
    with pytest.raises(McpError, match="not have rights"):
        _tool("outlook")()
    client.search_messages.side_effect = _upstream(429)
    with pytest.raises(UpstreamHTTPError):
        _tool("outlook")()


# --- composer -------------------------------------------------------------------

def test_brouillon_par_defaut_markdown_en_html(client):
    client.create_draft.return_value = {"id": "d1", "subject": "Point",
                                        "toRecipients": [{"emailAddress": {
                                            "address": "marc@fabrikam.example"}}]}
    out = _tool("outlook")(op="draft", body="**Bonjour**", to=["marc@fabrikam.example"],
                                   subject="Point", cc=["a@b.example"])
    kw = client.create_draft.call_args.kwargs
    assert kw["to"] == ["marc@fabrikam.example"] and kw["cc"] == ["a@b.example"]
    assert "<strong>Bonjour</strong>" in kw["body_html"] and kw["attachments"] == []
    client.send_draft.assert_not_called()
    assert out["kind"] == "draft" and out["id"] == "d1"
    assert out["to"] == ["marc@fabrikam.example"]


def test_send_explicite_passe_par_le_brouillon_et_rend_le_lien_envoye(client):
    client.create_draft.return_value = {"id": "d1", "webLink": "https://o/brouillon"}
    client.send_draft.return_value = "d1"
    client.sent_message.return_value = {"id": "d1", "webLink": "https://o/envoye",
                                        "sentDateTime": "2026-10-13T10:00:00Z"}
    out = _tool("outlook_act")(op="send", body="x", to=["marc@fabrikam.example"])
    client.create_draft.assert_called_once()
    client.send_draft.assert_called_once_with("d1")
    client.sent_message.assert_called_once_with("d1")
    assert out["kind"] == "sent" and out["id"] == "d1"
    assert out["webLink"] == "https://o/envoye" and "note" not in out


def test_envoye_pas_encore_visible_lien_nul_et_note(client):
    client.create_draft.return_value = {"id": "d1", "webLink": "https://o/brouillon"}
    client.send_draft.return_value = "d1"
    client.sent_message.return_value = None
    out = _tool("outlook_act")(op="send", body="x", to=["m@f.example"])
    assert out["kind"] == "sent" and out["id"] == "d1" and out["webLink"] is None
    assert "LEFT" in out["note"] and "not visible yet" in out["note"]
    client.sent_message.side_effect = _upstream(403)
    out = _tool("outlook_act")(op="send", body="x", to=["m@f.example"])
    assert out["kind"] == "sent" and out["webLink"] is None
    assert "LEFT" in out["note"] and "failed" in out["note"]


def test_envoi_echoue_le_brouillon_reste_et_c_est_dit(client):
    client.create_draft.return_value = {"id": "d1"}
    client.send_draft.side_effect = _upstream(400)
    with pytest.raises(McpError, match="d1 was SAVED"):
        _tool("outlook_act")(op="send", body="x", to=["m@f.example"])


def test_reponse_sans_destinataires(client):
    client.create_reply_draft.return_value = {"id": "r1"}
    out = _tool("outlook")(op="draft", body="Merci", reply_to="m1", reply_all=True)
    args, kw = client.create_reply_draft.call_args
    assert args == ("m1",) and kw["reply_all"] is True and "Merci" in kw["body_html"]
    assert kw["attachments"] == [] and out["kind"] == "draft"
    for extra in ({"to": ["a@b.example"]}, {"cc": ["a@b.example"]},
                  {"bcc": ["a@b.example"]}, {"subject": "x"}):
        with pytest.raises(McpError, match="not accepted on a reply"):
            _tool("outlook")(op="draft", body="x", reply_to="m1", **extra)
    with pytest.raises(McpError, match="`to` is required"):
        _tool("outlook")(op="draft", body="x")
    with pytest.raises(McpError, match="reply_all"):
        _tool("outlook")(op="draft", body="x", to=["a@b.example"], reply_all=True)


def test_pieces_jointes_en_octets_et_bornees_avant_ecriture(client, monkeypatch):
    from oto_mcp import file_source
    from oto.tools.microsoft.mail import MAX_INLINE_ATTACHMENT

    plafonds = []

    def resolve(src, max_bytes):
        plafonds.append(max_bytes)
        if src["url"].endswith("gros"):
            raise file_source.FileSourceError("fichier de 9999999 octets > plafond")
        return file_source.ResolvedFile(b"%PDF", "../../contrat.pdf", "application/pdf")

    monkeypatch.setattr(file_source, "resolve", resolve)
    client.create_draft.return_value = {"id": "d1"}
    _tool("outlook")(op="draft", body="x", to=["m@f.example"],
                             attachments=[{"kind": "url", "url": "https://a/ok"}])
    assert client.create_draft.call_args.kwargs["attachments"] == [
        {"name": "contrat.pdf", "content_type": "application/pdf",
         "content_bytes": b"%PDF"}]
    assert plafonds == [MAX_INLINE_ATTACHMENT]
    client.create_draft.reset_mock()
    with pytest.raises(McpError, match="up to 3 MB"):
        _tool("outlook")(op="draft", body="x", to=["m@f.example"],
                                 attachments=[{"kind": "url", "url": "https://a/gros"}])
    client.create_draft.assert_not_called()


def test_la_description_dit_brouillon_par_defaut_et_pas_de_signature():
    import oto.tools.microsoft  # noqa: F401 — le vrai cœur, pour l'enregistrement

    desc = asyncio.run(_mcp().get_tool("outlook")).description
    assert "WRITE A DRAFT" in desc and "does NOT" in desc and "No signature" in desc
    assert "Nothing here\nsends" in desc


# --- corps HTML, images en ligne ------------------------------------------------

#: Ce qu'un rendu markdown abîmerait : une `<img>` isolée (Python-Markdown l'enveloppe
#: dans `<p>`), les commentaires conditionnels d'Outlook, des blancs significatifs.
HTML_OUTLOOK = (
    '<!DOCTYPE html>\n<html><head><meta charset="utf-8">\n'
    '<!--[if mso]><style>td{font-family:Arial}</style><![endif]--></head>\n'
    '<body>\n<img src="cid:logo" width="120" alt="Logo">\n\n'
    '<table role="presentation"><tr><td style="padding:0 8px">Été &amp; *pas* '
    'du markdown_</td></tr></table>\n  \n</body></html>\n')


def test_html_transmis_tel_quel_sans_passage_markdown(client):
    client.create_draft.return_value = {"id": "d1"}
    _tool("outlook")(op="draft", html=HTML_OUTLOOK, to=["m@f.example"], subject="Lettre")
    assert client.create_draft.call_args.kwargs["body_html"].encode() == HTML_OUTLOOK.encode()
    client.create_reply_draft.return_value = {"id": "r1"}
    _tool("outlook")(op="draft", html=HTML_OUTLOOK, reply_to="m1")
    assert client.create_reply_draft.call_args.kwargs["body_html"] == HTML_OUTLOOK


@pytest.mark.parametrize("source", [
    {"kind": "project_file", "project_id": 7, "file_id": 3},
    {"kind": "url", "url": "https://files.example/lettre.html?sig=x"}])
def test_html_source_lu_et_transmis_tel_quel(client, monkeypatch, source):
    from oto_mcp import file_source

    lus = []

    def resolve(src, max_bytes):
        lus.append((src, max_bytes))
        return file_source.ResolvedFile(HTML_OUTLOOK.encode("utf-8"), "lettre.html",
                                        "text/html")

    monkeypatch.setattr(file_source, "resolve", resolve)
    client.create_draft.return_value = {"id": "d1"}
    _tool("outlook")(op="draft", html_source=source, to=["m@f.example"])
    assert lus == [(source, 1024 * 1024)]
    assert client.create_draft.call_args.kwargs["body_html"] == HTML_OUTLOOK


def test_html_source_au_dela_du_plafond_ou_illisible_refuse_avant_ecriture(
        client, monkeypatch):
    from oto_mcp import file_source

    def trop_gros(src, max_bytes):
        raise file_source.FileSourceError(f"fichier de 2000000 octets > plafond {max_bytes}")

    monkeypatch.setattr(file_source, "resolve", trop_gros)
    with pytest.raises(McpError, match="`html_source` could not be read.*limited to 1 MB"):
        _tool("outlook")(op="draft", html_source={"kind": "url", "url": "https://x/a"},
                                 to=["m@f.example"])
    monkeypatch.setattr(file_source, "resolve", lambda src, max_bytes: file_source.
                        ResolvedFile(b"\xff\xfe%PDF", "devis.pdf", "application/pdf"))
    with pytest.raises(McpError, match="not UTF-8 text"):
        _tool("outlook")(op="draft", html_source={"kind": "url", "url": "https://x/a"},
                                 to=["m@f.example"])
    client.create_draft.assert_not_called()


@pytest.mark.parametrize("corps", [
    {}, {"body": "x", "html": "<p>x</p>"},
    {"html": "<p>x</p>", "html_source": {"kind": "url", "url": "https://x"}},
    {"body": "x", "html": "<p>x</p>", "html_source": {"kind": "url", "url": "https://x"}}])
def test_exactement_un_corps(client, corps):
    with pytest.raises(McpError, match="exactly one of `body`"):
        _tool("outlook")(op="draft", to=["m@f.example"], **corps)
    with pytest.raises(McpError, match="exactly one of `body`"):
        _tool("outlook")(op="draft", reply_to="m1", **corps)
    client.create_draft.assert_not_called()
    client.create_reply_draft.assert_not_called()


def test_html_vide_refuse(client):
    with pytest.raises(McpError, match="`html` is empty"):
        _tool("outlook")(op="draft", html="  \n", to=["m@f.example"])
    client.create_draft.assert_not_called()


def test_le_brouillon_rend_son_weblink(client):
    client.create_draft.return_value = {"id": "d1", "webLink": "https://outlook.office.com/d1"}
    out = _tool("outlook")(op="draft", html="<p>x</p>", to=["m@f.example"])
    assert out["kind"] == "draft" and out["webLink"] == "https://outlook.office.com/d1"


def test_image_en_ligne_jusqu_a_la_requete_graph(monkeypatch):
    """Le VRAI `MailClient` (HTTP doublé) : le HTML arrive à Graph octet pour octet, la
    pièce au `content_id` part en ligne, le PDF reste une pièce ordinaire."""
    import json

    from oto_mcp import file_source

    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for",
                        lambda sub, service: "AT")
    fichiers = {"https://a/logo.png": (b"\x89PNG", "logo.png", "image/png"),
                "https://a/devis.pdf": (b"%PDF", "devis.pdf", "application/pdf")}
    monkeypatch.setattr(file_source, "resolve", lambda src, max_bytes: file_source.
                        ResolvedFile(*fichiers[src["url"]]))
    envoyes = []

    class _Resp:
        status_code = 201

        def __init__(self, payload):
            self.content = json.dumps(payload).encode()

        def json(self):
            return json.loads(self.content)

    def request(self, method, url, **kw):
        envoyes.append((method, url, kw.get("json")))
        return _Resp({"id": "d1", "webLink": "https://outlook.office.com/d1"})

    monkeypatch.setattr("requests.Session.request", request)
    out = _tool("outlook")(op="draft",
        html=HTML_OUTLOOK, to=["m@f.example"], subject="Lettre",
        attachments=[{"kind": "url", "url": "https://a/logo.png", "content_id": "logo"},
                     {"kind": "url", "url": "https://a/devis.pdf"}])
    (_, url, message), (_, _, logo), (_, _, devis) = envoyes
    assert url.endswith("/me/messages")
    assert message["body"] == {"contentType": "HTML", "content": HTML_OUTLOOK}
    assert logo["isInline"] is True and logo["contentId"] == "logo"
    assert logo["contentBytes"] == base64.b64encode(b"\x89PNG").decode()
    assert "isInline" not in devis and "contentId" not in devis
    assert out["webLink"] == "https://outlook.office.com/d1"
    envoyes.clear()
    with pytest.raises(McpError, match="content_id"):
        _tool("outlook")(op="draft",
            html=HTML_OUTLOOK, to=["m@f.example"],
            attachments=[{"kind": "url", "url": "https://a/logo.png",
                          "content_id": "<logo>"}])
    assert envoyes == [], "un content_id refusé ne laisse aucun brouillon"


def test_la_description_dit_qu_un_message_deplace_garde_son_id():
    import oto.tools.microsoft  # noqa: F401

    desc = asyncio.run(_mcp().get_tool("outlook_act")).description
    assert "KEEPS its id" in desc and "NEW id" not in desc


def test_schema_le_corps_se_donne_d_une_des_trois_facons():
    import oto.tools.microsoft  # noqa: F401

    for nom in ("outlook", "outlook_act"):
        schema = asyncio.run(_mcp().get_tool(nom)).parameters
        assert {"body", "html", "html_source"} <= set(schema["properties"]), nom
        assert "body" not in schema.get("required", []), nom
    desc = asyncio.run(_mcp().get_tool("outlook")).description
    for attendu in ("exactly one of three", "AS IS", "slug=outlook-html", "cid:logo",
                    "(inline images included)", 'outlook_act(op="send", draft_id'):
        assert attendu in desc, attendu
    desc = asyncio.run(_mcp().get_tool("outlook_act")).description
    for attendu in ("draft_id", "webLink", "keeps the same `id`",
                    'the rules of `outlook(op="draft")`'):
        assert attendu in desc, attendu


def test_reponse_avec_image_en_ligne_et_piece(client, monkeypatch):
    from oto_mcp import file_source

    fichiers = {"https://a/logo.png": (b"\x89PNG", "logo.png", "image/png"),
                "https://a/devis.pdf": (b"%PDF", "devis.pdf", "application/pdf")}
    monkeypatch.setattr(file_source, "resolve", lambda src, max_bytes: file_source.
                        ResolvedFile(*fichiers[src["url"]]))
    client.create_reply_draft.return_value = {"id": "r1", "webLink": "https://o/r1"}
    out = _tool("outlook")(op="draft",
        html='<p>Merci</p><img src="cid:logo">', reply_to="m1",
        attachments=[{"kind": "url", "url": "https://a/logo.png", "content_id": "logo"},
                     {"kind": "url", "url": "https://a/devis.pdf"}])
    kw = client.create_reply_draft.call_args.kwargs
    assert kw["body_html"] == '<p>Merci</p><img src="cid:logo">'
    assert kw["attachments"] == [
        {"name": "logo.png", "content_type": "image/png", "content_bytes": b"\x89PNG",
         "content_id": "logo"},
        {"name": "devis.pdf", "content_type": "application/pdf", "content_bytes": b"%PDF"}]
    assert out["kind"] == "draft" and out["webLink"] == "https://o/r1"


def test_envoi_relu_jusqu_a_la_requete_graph(monkeypatch):
    """Le VRAI `MailClient` (HTTP doublé) : id immuable demandé à chaque écriture, la
    copie envoyée relue par le même id après un 404, son `webLink` rendu."""
    import json

    from oto.tools.microsoft import mail

    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for",
                        lambda sub, service: "AT")
    monkeypatch.setattr(mail.time, "sleep", lambda d: None)
    vus = []

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.content = b"" if payload is None else json.dumps(payload).encode()
            self.text = self.content.decode()

        def json(self):
            return json.loads(self.content)

    reponses = [_Resp(201, {"id": "IMM1", "webLink": "https://o/brouillon"}),
                _Resp(202), _Resp(404, {"error": {"code": "ErrorItemNotFound"}}),
                _Resp(200, {"id": "IMM1", "webLink": "https://o/envoye",
                            "sentDateTime": "2026-10-13T10:00:00Z"})]

    def request(self, method, url, **kw):
        vus.append((method, url.split("/v1.0")[-1], (kw.get("headers") or {}).get("Prefer")))
        return reponses.pop(0)

    monkeypatch.setattr("requests.Session.request", request)
    out = _tool("outlook_act")(op="send", html="<p>x</p>", to=["m@f.example"])
    imm = 'IdType="ImmutableId"'
    assert vus == [("POST", "/me/messages", imm), ("POST", "/me/messages/IMM1/send", imm),
                   ("GET", "/me/messages/IMM1", imm), ("GET", "/me/messages/IMM1", imm)]
    assert out == {"kind": "sent", "id": "IMM1", "subject": None, "to": [], "cc": [],
                   "webLink": "https://o/envoye", "sentDateTime": "2026-10-13T10:00:00Z"}


# --- lire ou simuler / agir (17/10/2026) ------------------------------------------

def test_un_brouillon_existant_s_envoie_tel_quel_par_son_id(client):
    client.send_draft.return_value = "d1"
    client.sent_message.return_value = {"id": "d1", "webLink": "https://o/envoye",
                                        "subject": "Point", "toRecipients": [
                                            {"emailAddress": {"address": "m@f.example"}}],
                                        "sentDateTime": "2026-10-17T10:00:00Z"}
    out = _tool("outlook_act")(op="send", draft_id="d1")
    client.create_draft.assert_not_called()
    client.send_draft.assert_called_once_with("d1")
    assert out["kind"] == "sent" and out["id"] == "d1" and out["subject"] == "Point"
    assert out["to"] == ["m@f.example"] and out["webLink"] == "https://o/envoye"


@pytest.mark.parametrize("extra", [{"body": "x"}, {"to": ["a@b.example"]},
                                   {"subject": "s"}, {"reply_all": True}])
def test_a_cote_d_un_draft_id_un_argument_de_redaction_est_refuse(client, extra):
    with pytest.raises(McpError, match="only apply when composing"):
        _tool("outlook_act")(op="send", draft_id="d1", **extra)
    assert client.jetons == []


def test_le_brouillon_ne_part_jamais_depuis_l_outil_qui_lit(client):
    client.create_draft.return_value = {"id": "d1"}
    _tool("outlook")(op="draft", body="x", to=["m@f.example"])
    client.send_draft.assert_not_called()
    client.move.assert_not_called()


@pytest.mark.parametrize("op", ["archive", "trash", "move"])
def test_un_deplacement_refuse_la_redaction_et_le_draft_id(client, op):
    with pytest.raises(McpError):
        _tool("outlook_act")(op=op, message_ids=["m1"], destination="f1", body="x")
    with pytest.raises(McpError, match="does not use `draft_id`"):
        _tool("outlook_act")(op=op, message_ids=["m1"], draft_id="d1")
    client.move.assert_not_called()
