"""Connecteur Minari — prospection téléphonique (api.minari.ai/v1).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la doc
how-to, la surface MCP (6 tools, chacun avec une description), la sonde « tester
la connexion », la jointure tool↔client oto-core (garde version-skew), le
dispatch `op=` — et les trois BUDGETS qui sont la raison d'être de ce module :
le transcript retiré de `op="get"`, les contacts plafonnés sur `op="get"`, les
répliques plafonnées sur `op="transcript"`.

⚠️ `test_les_tools_existent_apres_le_boot_reel` n'est pas décoratif : `register_all`
importe chaque module en try/except, donc une ImportError dans `tools/minari.py`
laisserait TOUTE la suite verte avec un connecteur qui ne se charge jamais. C'est
arrivé pendant l'écriture de ce module (`from .. import connector_verify`, dont le
domicile est `..connectors.verify` depuis le découpage) : 284 tests de garde
passaient sur un module qui n'existait pas au boot.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import minari

EXPECTED_TOOLS = {
    "minari_call", "minari_user", "minari_list",
    "minari_contact", "minari_custom_field", "minari_analytics",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


def _fn_with_mock_client():
    """Enregistre le module avec `MinariClient` mocké, DANS le patch (sinon le
    `from ... import MinariClient` de `register()` capture la vraie classe)."""
    from fastmcp import FastMCP

    patcher = patch("oto.tools.minari.client.MinariClient")
    cls = patcher.start()
    m = FastMCP("t")
    minari.register(m)
    return m, cls, patcher


def _tool(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- registre -----------------------------------------------------------------

def test_minari_est_un_connecteur_keyed_byo_only():
    c = providers.REGISTRY["minari"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert c.default_active is False
    assert c.default_quota == 0
    assert "minari" in providers.KEY_PROVIDERS


def test_pas_de_cle_plateforme_le_journal_dappels_est_celui_du_client():
    """Une clé Minari porte les droits de TOUTE une entreprise et donne accès aux
    conversations de ses commerciaux. La mutualiser entre orgs n'a aucun sens —
    même principe que `stripe`."""
    c = providers.REGISTRY["minari"]
    assert "platform" not in c.auth_modes
    assert c.platform_key_open is False


def test_les_recouvrements_de_catalogue_sont_cures():
    c = providers.REGISTRY["minari"]
    assert c.category == "Calls & meetings"
    assert c.publisher_name == "Minari"
    assert c.label == "Minari"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["minari"] == "minari.ai"
    assert "minari" not in providers._SANS_LOGO_DE_MARQUE


def test_le_namespace_est_un_seul_token():
    """`namespace_of` résout au préfixe déclaré ; un namespace multi-mot ferait
    fail-open le gate d'activation (#24)."""
    assert providers.connector_for_namespace("minari").name == "minari"
    for name in EXPECTED_TOOLS:
        assert namespace_of(name) == "minari"


# --- doc how-to ---------------------------------------------------------------

def test_la_doc_how_to_est_servie():
    sections = providers.REGISTRY["minari"].doc_sections
    assert sections, "la fiche du connecteur s'afficherait sans mode d'emploi"
    kinds = {s.kind for s in sections}
    assert "prerequisite" in kinds and "usage" in kinds


def test_la_doc_previent_du_piege_de_portee_csv():
    """C'est le piège n°1 de cette API et il est SILENCIEUX : un compte dont les
    contacts viennent d'un CRM a des listes que ces endpoints ne rendent jamais."""
    corps = "\n".join(s.body_md for s in providers.REGISTRY["minari"].doc_sections)
    assert "CSV" in corps
    assert 'minari_analytics(op="lists")' in corps


def test_la_doc_ne_revendique_pas_une_verification_live_inexistante():
    """Le connecteur est écrit sur contrat, sans clé. Prétendre l'inverse est le
    genre d'attestation que les fiches voisines portent à juste titre, et qui ne
    vaut que si elle est vraie."""
    corps = "\n".join(s.body_md for s in providers.REGISTRY["minari"].doc_sections)
    assert "without a probe against a real account" in corps


# --- surface MCP ---------------------------------------------------------------

def test_les_tools_existent_apres_le_boot_reel(all_tools):
    """Sur le montage RÉEL (`register_all`), pas sur une fixture partielle :
    `register_all` avale les ImportError, donc c'est le seul test qui prouve que
    le module se charge vraiment au boot."""
    assert EXPECTED_TOOLS <= set(all_tools)


def test_chaque_tool_porte_une_description(all_tools):
    for name in EXPECTED_TOOLS:
        assert (all_tools[name].description or "").strip(), f"{name} sans description"


def test_la_surface_est_exactement_celle_declaree(all_tools):
    servis = {n for n in all_tools if n.startswith("minari")}
    assert servis == EXPECTED_TOOLS


def test_aucune_prose_nest_echouee_apres_le_bloc_args(all_tools):
    """FastMCP ne sert au modèle que ce qui PRÉCÈDE `Args:` — le bloc devient les
    descriptions de paramètres, et toute la prose qui suit est jetée. Un
    avertissement écrit après (l'ordre naturel : résumé, Args, mises en garde) est
    donc invisible, sans que rien ne le signale. Vécu ici sur `minari_call`, dont
    la note « op=recording ne rend PAS l'audio » a été écrite après `Args:` puis
    déplacée."""
    import inspect

    for name in sorted(EXPECTED_TOOLS):
        doc = inspect.getdoc(all_tools[name].fn) or ""
        if "Args:" not in doc:
            continue
        apres = doc.split("Args:", 1)[1]
        echouee = [l for l in apres.splitlines()
                   if l.strip() and not l.startswith("    ")]
        assert not echouee, (
            f"{name} : prose après le bloc `Args:`, jamais servie au modèle — "
            f"remonte-la entre le résumé et `Args:` :\n" + "\n".join(echouee))


@pytest.mark.parametrize("name, attendu", [
    # Chaque chaîne est un piège qui coûte un appel faux si le modèle l'ignore.
    ("minari_call", "NOT the audio"),
    ("minari_call", "public_call_link"),
    ("minari_call", "OMITS the transcript"),
    ("minari_list", "CSV-import source ONLY"),
    ("minari_list", "PERMANENT"),
    ("minari_contact", "skippedCount"),
    ("minari_analytics", "DEFAULT WINDOWS DIFFER"),
    ("minari_analytics", "all-sources view"),
])
def test_les_avertissements_atteignent_vraiment_le_modele(all_tools, name, attendu):
    """Un test qui vérifie seulement que `description` est non vide laisse passer
    exactement le défaut ci-dessus. On cherche donc la chaîne."""
    assert attendu in (all_tools[name].description or ""), (
        f"{name} : « {attendu} » n'atteint pas le modèle")


# --- sonde « tester la connexion » ----------------------------------------------

def test_la_sonde_est_enregistree(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("minari")


def test_la_sonde_passe_quand_lespace_a_des_membres():
    with patch("oto.tools.minari.client.MinariClient") as cls:
        cls.return_value.list_users.return_value = {"data": [{"id": 1}]}
        minari._verify({"key": "k"})


def test_un_annuaire_vide_ne_fait_PAS_echouer_la_sonde():
    """La sonde tourne AVANT la persistance (#106) : ce qu'elle refuse n'est
    jamais enregistré. Une version antérieure levait sur un annuaire vide en
    croyant y lire « clé du mauvais espace » — raisonnement faux (la clé d'un
    autre espace rend les membres de CET espace, pas une liste vide) dont le coût
    était réel : elle empêchait d'ENREGISTRER une clé qui marche. Une sonde
    répond « cette clé authentifie-t-elle ? », rien de plus."""
    with patch("oto.tools.minari.client.MinariClient") as cls:
        cls.return_value.list_users.return_value = {"data": []}
        minari._verify({"key": "k"})


def test_la_sonde_remonte_le_refus_de_lamont():
    """Ce qu'elle DOIT refuser : une clé que Minari rejette."""
    from oto.tools.common.errors import UpstreamHTTPError
    with patch("oto.tools.minari.client.MinariClient") as cls:
        cls.return_value.list_users.side_effect = UpstreamHTTPError(
            401, {"error": {"code": "INVALID_API_KEY"}}, service="minari")
        with pytest.raises(UpstreamHTTPError):
            minari._verify({"key": "k"})


# --- jointure tool ↔ client oto-core (garde version-skew) ----------------------

@pytest.mark.parametrize("method", [
    "list_calls", "get_call", "get_call_transcript", "call_recording_status",
    "list_users", "list_lists", "get_list", "create_list", "delete_list",
    "add_contacts", "remove_contacts", "list_custom_fields",
    "create_custom_field", "delete_custom_field", "analytics_overview",
    "analytics_users", "analytics_objections", "analytics_lists",
])
def test_chaque_methode_appelee_existe_sur_le_client(method):
    from oto.tools.minari.client import MinariClient
    assert callable(getattr(MinariClient, method, None))


# --- dispatch `op=` -------------------------------------------------------------

def test_un_required_manquant_est_refuse():
    m, _cls, p = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="call_id"):
            _tool(m, "minari_call")(op="get")
        with pytest.raises(McpError, match="assigned_to"):
            _tool(m, "minari_list")(op="create", name="x", contacts=[{"email": "a@b.c"}])
        with pytest.raises(McpError, match="period"):
            _tool(m, "minari_analytics")(op="lists")
        with pytest.raises(McpError, match="call_limit"):
            _tool(m, "minari_analytics")(op="lists", period="week")
    finally:
        p.stop()


def test_un_argument_non_pertinent_pour_cet_op_est_refuse():
    """Sinon `minari_call(op="list", call_id=…)` rendrait TOUS les appels en
    laissant croire qu'on en a ciblé un."""
    m, _cls, p = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="call_id"):
            _tool(m, "minari_call")(op="list", call_id="C1")
        with pytest.raises(McpError, match="search"):
            _tool(m, "minari_call")(op="get", call_id="C1", search="acme")
        with pytest.raises(McpError, match="conversation_threshold"):
            _tool(m, "minari_analytics")(op="lists", period="week", call_limit=3,
                                         conversation_threshold=30)
        with pytest.raises(McpError, match="contact_ids"):
            _tool(m, "minari_contact")(op="add", list_id="L1",
                                       contacts=[{"email": "a@b.c"}], contact_ids=[1])
    finally:
        p.stop()


def test_op_lists_refuse_une_fenetre_en_dates():
    """`GET /analytics/lists` n'a PAS de `start_date`/`end_date` (vérifié dans
    l'OpenAPI) : sa fenêtre est `period`. Les accepter en silence rendrait un
    « depuis janvier » calculé sur la semaine, sans que rien ne le signale."""
    m, _cls, p = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="has no date window"):
            _tool(m, "minari_analytics")(op="lists", period="week", call_limit=3,
                                         start_date="2026-01-01", end_date="2026-08-01")
    finally:
        p.stop()


def test_une_date_sans_son_pendant_est_refusee():
    """Minari exige `start_date` et `end_date` ensemble ; envoyer l'une seule
    rendrait la fenêtre par défaut sans le dire."""
    m, _cls, p = _fn_with_mock_client()
    try:
        with pytest.raises(McpError, match="go together"):
            _tool(m, "minari_analytics")(op="overview", start_date="2026-08-01")
    finally:
        p.stop()


# --- les trois budgets : la raison d'être de ce module -------------------------

def test_op_get_retire_le_transcript_et_le_dit():
    """Une fiche d'appel embarque chaque réplique d'un appel de 45 minutes : le
    résumé et les objections qu'on a demandés seraient noyés. Le retrait est
    NOMMÉ dans la réponse — rien ne disparaît en silence."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.get_call.return_value = {"data": {
            "call_id": "C1", "summary": "s", "objections": [],
            "transcript": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}}
        out = _tool(m, "minari_call")(op="get", call_id="C1")
        assert "transcript" not in out["data"]
        assert out["data"]["transcript_utterances"] == 3
        assert "op=\"transcript\"" in out["note"]
        assert out["data"]["summary"] == "s"
    finally:
        p.stop()


def test_op_transcript_plafonne_et_annonce_la_troncature():
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.get_call_transcript.return_value = {"data": {
            "call_id": "C1", "transcript": [{"text": str(i)} for i in range(500)]}}
        out = _tool(m, "minari_call")(op="transcript", call_id="C1", max_utterances=10)
        assert len(out["data"]["transcript"]) == 10
        assert out["data"]["transcript_utterances"] == 500
        assert out["data"]["truncated"] is True
    finally:
        p.stop()


def test_op_get_distingue_transcription_en_cours_de_zero_replique():
    """`transcript: null` ≠ zéro réplique. Rendre `transcript_utterances: 0`
    ferait passer « transcription en cours » pour « appel muet », et enverrait
    l'agent chercher un texte qui n'existe pas."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.get_call.return_value = {"data": {
            "call_id": "C1", "summary": None, "transcript": None}}
        out = _tool(m, "minari_call")(op="get", call_id="C1")
        assert out["data"]["transcript_utterances"] == 0
        assert "transcription" in out["note"]
        assert "will return nothing more" in out["note"], (
            "un appel sans transcript ne doit pas coûter un second appel")
    finally:
        p.stop()


def test_un_transcript_absent_nest_pas_une_erreur():
    """`transcript: null` = appel non abouti ou transcription en cours. Le
    présenter comme une panne provoquerait une relance inutile."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.get_call_transcript.return_value = {
            "data": {"call_id": "C1", "transcript": None}}
        out = _tool(m, "minari_call")(op="transcript", call_id="C1")
        assert "transcription" in out["note"]
    finally:
        p.stop()


def test_op_get_dune_liste_plafonne_ses_contacts_et_dit_le_total():
    """Une liste rend ses 1500 contacts d'un bloc, sans pagination."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.get_list.return_value = {"data": {
            "listId": "L1", "name": "Q3",
            "contacts": [{"contactId": str(i)} for i in range(1500)]}}
        out = _tool(m, "minari_list")(op="get", list_id="L1", max_contacts=5)
        assert len(out["data"]["contacts"]) == 5
        assert out["data"]["total_contacts"] == 1500
        assert out["data"]["truncated"] is True
    finally:
        p.stop()


@pytest.mark.parametrize("rows, cas", [
    ([], "page vide — l'agent conclurait que le compte n'a pas de liste"),
    ([{"listId": "L1"}], "page PARTIELLE — quelques listes CSV à côté de "
                         "beaucoup de listes CRM : sous-déclare tout autant, "
                         "et ne se signale par rien"),
])
def test_la_portee_csv_est_dite_sur_toute_premiere_page(rows, cas):
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_lists.return_value = {
            "data": rows, "has_more": False, "next_url": None}
        out = _tool(m, "minari_list")(op="list")
        assert "CSV" in out["note"], cas
    finally:
        p.stop()


def test_la_note_de_portee_ne_se_repete_pas_a_chaque_page():
    """Elle énonce une portée une fois, elle ne ponctue pas la pagination."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_lists.return_value = {
            "data": [{"listId": "L2"}], "has_more": False, "next_url": None}
        out = _tool(m, "minari_list")(op="list", cursor="page2")
        assert "note" not in out
    finally:
        p.stop()


def test_max_contacts_est_plafonne_et_le_dit():
    """Un `max_contacts=1500` demandé de bonne foi rendrait 1500 contacts portant
    chacun une note de 5 000 caractères. Le plafond est ANNONCÉ, pas silencieux."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.get_list.return_value = {"data": {
            "listId": "L1", "contacts": [{"contactId": str(i)} for i in range(1500)]}}
        out = _tool(m, "minari_list")(op="get", list_id="L1", max_contacts=1500)
        assert len(out["data"]["contacts"]) == minari._CEILING_MAX_CONTACTS
        assert "reduced to" in out["note"]
        assert out["data"]["total_contacts"] == 1500
    finally:
        p.stop()


# --- pagination ----------------------------------------------------------------

def test_le_curseur_suivant_est_extrait_de_lurl_amont():
    """L'agent reçoit un curseur à repasser tel quel, pas une URL à parser (ni à
    nous renvoyer, ce qui nous obligerait à la valider comme une URL amont)."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_calls.return_value = {
            "data": [], "has_more": True,
            "next_url": "https://api.minari.ai/v1/calls?cursor=abc123"}
        out = _tool(m, "minari_call")(op="list")
        assert out["next_cursor"] == "abc123"
    finally:
        p.stop()


def test_pas_de_curseur_quand_il_ny_a_pas_de_suite():
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_calls.return_value = {
            "data": [], "has_more": False, "next_url": None}
        out = _tool(m, "minari_call")(op="list")
        assert "next_cursor" not in out
    finally:
        p.stop()


def test_le_chemin_de_rattrapage_de_next_cursor_est_reellement_emprunte():
    """`urlparse` ne lève presque jamais : une chaîne quelconque est parsée sans
    erreur, donc un test avec « ::pas une url:: » emprunte le chemin NORMAL et
    ne couvre pas l'`except`. Une clause jamais exercée peut dormir avec un nom
    non importé dedans — verte en test, NameError en prod. `http://[::1` lève
    vraiment (`Invalid IPv6 URL`)."""
    assert minari._next_cursor({"next_url": "http://[::1"}) is None

    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_calls.return_value = {
            "data": [{"call_id": "C1"}], "has_more": True, "next_url": "http://[::1"}
        out = _tool(m, "minari_call")(op="list")
        assert out["data"] == [{"call_id": "C1"}]
        assert "next_cursor" not in out
    finally:
        p.stop()


# --- traduction des refus amont -------------------------------------------------

def test_un_401_dit_ou_recreer_la_cle():
    from oto.tools.common.errors import UpstreamHTTPError
    msg = minari._upstream_message(
        UpstreamHTTPError(401, {"error": {"code": "INVALID_API_KEY"}}, service="minari"))
    assert "Settings" in msg and "API & webhook" in msg


def test_un_429_dit_que_le_budget_est_partage_par_lentreprise():
    """60 requêtes/minute PAR ENTREPRISE : le lecteur doit comprendre que ce n'est
    pas SON rythme qui est en cause, mais celui de tout l'espace."""
    from oto.tools.common.errors import UpstreamHTTPError
    msg = minari._upstream_message(UpstreamHTTPError(429, {}, service="minari"))
    assert "60 requests/minute" in msg
    assert "the WHOLE company" in msg and "under the same key" in msg


def test_un_corps_derreur_non_json_nest_pas_effacé():
    """Un 502 de proxy rend du HTML, pas le contrat `{"error": …}`. Le réduire à
    `{}` effacerait la seule information disponible pour diagnostiquer."""
    from oto.tools.common.errors import UpstreamHTTPError
    msg = minari._upstream_message(
        UpstreamHTTPError(502, "<html>Bad Gateway</html>", service="minari"))
    assert "Bad Gateway" in msg


def test_un_corps_vide_ne_laisse_pas_de_residu():
    """Ni `{}` ni `None` ne doivent finir affichés à l'utilisateur."""
    from oto.tools.common.errors import UpstreamHTTPError
    msg = minari._upstream_message(UpstreamHTTPError(404, {}, service="minari"))
    assert not msg.rstrip().endswith("{}")
    assert "None" not in msg


def test_le_message_derreur_ne_porte_jamais_la_cle():
    from oto.tools.common.errors import UpstreamHTTPError
    for status in (400, 401, 404, 409, 429, 500):
        msg = minari._upstream_message(
            UpstreamHTTPError(status, {"error": {"message": "boom"}}, service="minari"))
        assert "Bearer" not in msg and "api_key" not in msg


# --- une seule source pour le contrat, pas deux ------------------------------

def test_le_filtre_status_du_tool_colle_a_celui_du_client():
    """Le contrat est écrit deux fois — `Literal` côté tool, `CALL_STATUSES` côté
    oto-core — et rien ne les relie : ils sont libres de diverger au prochain
    changement de l'API. C'est déjà arrivé une fois dans l'autre sens (les huit
    valeurs de la RÉPONSE recopiées dans le FILTRE, qui en compte neuf), au prix
    de la seule façon de demander les appels ayant donné un rendez-vous."""
    import typing
    from oto.tools.minari.client import CALL_STATUSES

    m, _cls, p = _fn_with_mock_client()
    try:
        fn = _tool(m, "minari_call")
        hints = typing.get_type_hints(fn)
        # Optional[List[Literal[...]]] → on redescend jusqu'au Literal
        literal = hints["status"]
        while typing.get_args(literal):
            args = typing.get_args(literal)
            lit = [a for a in args if typing.get_origin(a) is typing.Literal]
            if lit:
                literal = lit[0]
                break
            literal = args[0]
        assert set(typing.get_args(literal)) == set(CALL_STATUSES)
    finally:
        p.stop()


def test_la_cle_hors_bande_porte_le_nom_de_la_maison():
    """Les connecteurs voisins (posthog, stripe) attachent leurs remarques sous
    `note`. Un agent qui a appris à regarder `note` ne cherchera pas
    `scope_note` — et la page de listes vide est justement le cas où il DOIT la
    lire."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_lists.return_value = {"data": [], "has_more": False,
                                                    "next_url": None}
        assert "note" in _tool(m, "minari_list")(op="list")
        cls.return_value.get_call.return_value = {"data": {"call_id": "C1",
                                                           "transcript": [{"text": "a"}]}}
        assert "note" in _tool(m, "minari_call")(op="get", call_id="C1")
    finally:
        p.stop()


# --- le curseur est une POSITION, pas une requête -----------------------------

def test_tourner_la_page_rappelle_de_repasser_les_filtres():
    """Le curseur de Minari se décode en `{"s": started_at, "c": call_id}` : une
    position, sans le moindre filtre. Un agent qui rejouerait `cursor` SEUL
    recevrait la page suivante du journal ENTIER et la fondrait dans une réponse
    qu'il croit filtrée — faux, sans la moindre erreur. La remarque tombe au seul
    moment où elle est lue : celui où l'on s'apprête à tourner la page."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_calls.return_value = {
            "data": [], "has_more": True,
            "next_url": "https://api.minari.ai/v1/calls?transcript_search=prix&cursor=abc"}
        out = _tool(m, "minari_call")(op="list", transcript_search="prix")
        assert out["next_cursor"] == "abc"
        assert "same filters" in out["note"]
    finally:
        p.stop()


def test_le_parametre_cursor_le_dit_aussi_au_modele(all_tools):
    """Règle #517 : ce que le préambule pose se répète sur le PARAMÈTRE — l'agent
    lit le paramètre, pas seulement l'introduction. Le bloc `Args:` ne va pas
    dans `description` (FastMCP l'en retire) mais dans le schéma, donc c'est là
    qu'il faut chercher."""
    cursor = all_tools["minari_call"].parameters["properties"]["cursor"]
    assert "RE-SEND YOUR FILTERS" in cursor["description"]


def test_deux_remarques_se_cumulent_au_lieu_de_secraser():
    """Une page de listes CSV qui a une suite porte DEUX choses à dire : la portée
    du connecteur et le rappel de filtres. Écraser l'une par l'autre ferait
    disparaître celle qui compte, sans rien signaler."""
    m, cls, p = _fn_with_mock_client()
    try:
        cls.return_value.list_lists.return_value = {
            "data": [{"listId": "L1"}], "has_more": True,
            "next_url": "https://api.minari.ai/v1/lists?cursor=xyz"}
        out = _tool(m, "minari_list")(op="list")
        assert "CSV" in out["note"]
        assert "cursor" in out["note"]
    finally:
        p.stop()
