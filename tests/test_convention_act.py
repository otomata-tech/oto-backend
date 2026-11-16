"""Un connecteur sert DEUX outils : `<ns>` lit ou simule, `<ns>_act` agit (17/10/2026).

Décision d'Alexis, pour la sécurité : un client MCP autorise des OUTILS, pas des
arguments. `gmail_compose(mode="send")` envoyait sous l'autorisation du brouillon,
`gmail_message(op="trash")` vidait une boîte sous celle de la lecture. Cette garde rend la
règle mécanique, connecteur par connecteur :

- **nommage** : un connecteur migré ne sert que `<ns>` et `<ns>_act` ;
- **annotations** : `<ns>` est `LECTURE`, ou `SIMULATION` s'il prépare un brouillon ;
  `<ns>_act` écrit `destructiveHint` et `openWorldHint` — c'est lui que le client fait
  confirmer ;
- **ops** : aucune op de `<ns>` ne porte un verbe à effet (hors simulations nommées), et
  `<ns>_act` n'a que des ops à effet ; aucun paramètre qui déclenche (`mode`, `notify`,
  `send_*`) sur `<ns>` ;
- **connecteurs servis par le lecteur générique** (`tools/decrits.py`) : la garde lit leur
  DESCRIPTION, pas un verbe — `<ns>` sert exactement leurs fonctions `act: false`, `<ns>_act`
  leurs fonctions `act: true`, et l'annotation suit la classe ;
- **retraits** : chaque ancien nom de `outils_retires.RENOMMAGES` n'est plus servi, et
  son remplaçant sert bien l'op annoncée ; chaque op de `outils_retires.OPS_DEPLACEES`
  a quitté le lecteur qui reste servi, et son acteur la sert.

`A_MIGRER` est la liste FERMÉE des connecteurs pas encore passés à la paire. Elle ne peut
que rétrécir : un connecteur qui devient conforme, ou qui n'existe plus, y rougit jusqu'à
ce qu'on l'en retire.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from oto_mcp import outils_retires, providers, tool_visibility
from oto_mcp.connectors import descriptions
from oto_mcp.tools.lecture import ACTE, LECTURE, SIMULATION

from _mcp_app import static_mcp

#: Les connecteurs pas encore migrés vers `<ns>` / `<ns>_act` (17/10/2026). Ne fait que
#: rétrécir.
A_MIGRER = frozenset({
    "affinity", "ahrefs", "aircall", "airtable", "amplitude", "api", "apify", "apollo",
    "ashby", "attio", "bigquery", "bls", "bodacc", "boondmanager", "brevo", "brevoauto",
    "browser", "calendar", "ccn", "claap", "clay", "cloro", "cognism",
    "crunchbase", "culture", "drive", "dropcontact", "figma", "finkare",
    "firecrawl", "fireflies", "folk", "foncier", "forager", "fr", "frenchtech",
    "fullenrich", "ga4", "github", "gocardless", "google", "google_ads", "gr", "grain",
    "granola", "greenhouse", "hellostock", "hithorizons", "hubspot", "hunter",
    "infosec", "inqom", "instagram_meta", "jev", "juris", "kaspr",
    "klaviyo", "leexi", "lemlist", "lever", "lightfield", "lighton", "linear",
    "linkedin_aiark", "linkedin_unipile", "loi", "lucca", "luma", "lusha", "mailpool",
    "make", "meta_ads", "microsoft", "minari", "modjo", "monid", "n8n", "nextmotion",
    "notion", "onet", "origami", "osm", "outlook_calendar", "payfit", "pennylane",
    "pennylane_firm", "pennylaneged", "phantombuster", "pipedrive", "planity",
    "posthog", "productlane", "promptwatch", "recruitee", "reddit", "resend", "routine",
    "salesforce", "sante", "searchapi", "sellsy", "serpapi", "serper", "sharepoint",
    "sheets", "signwell", "silae", "sivan", "slack", "snitcher", "spott", "stripe",
    "supabase", "tally", "tasks", "tavily", "teamtailor",
    "theirstack", "threecx", "topograph", "transcription", "typeform", "ubersuggest",
    "unipile", "urba", "waalaxy", "web", "webflow", "wordpress", "wttj",
    "yousign", "zapier", "zerobounce", "zoho", "zohoanalytics", "zohodesk",
})

#: Un verbe à effet dans le nom d'une op : il agit chez un tiers ou détruit.
VERBES_EFFET = re.compile(
    r"^(send|reply|forward|publish|broadcast|invite|remind|trash|archive|move|delete|"
    r"remove|rm|create|update|upsert|set|add|cancel|trigger|activate|post|react|"
    r"comment|review|push|upload|share|unshare|lend|revoke|disable|enable|call|request|"
    r"import|accept|decline|endorse)(_|$)")
#: Les ops qui SIMULENT : elles écrivent ce que personne ne voit et que rien ne détruit.
SIMULATIONS = frozenset({"draft"})
#: Les paramètres qui feraient agir l'outil qui lit.
PARAMETRES_DECLENCHEURS = re.compile(r"^(mode|notify|send_.*)$")


def _par_connecteur() -> dict[str, dict]:
    outils = asyncio.run(static_mcp().list_tools(run_middleware=False))
    par: dict[str, dict] = {}
    for t in outils:
        ns = tool_visibility.namespace_of(t.name)
        if providers.connector_for_namespace(ns) is not None:
            par.setdefault(ns, {})[t.name] = t
    return par


def _ops(outil) -> list[str]:
    op = (outil.parameters.get("properties") or {}).get("op") or {}
    if "enum" in op:
        return list(op["enum"])
    if "const" in op:
        # Un seul geste (`Literal["update_rows"]`) : le schéma le dit en `const`.
        return [op["const"]]
    return [v for branche in op.get("anyOf", ()) for v in branche.get("enum", ())]


@pytest.fixture(scope="module")
def par_connecteur():
    return _par_connecteur()


def _servis_decrits() -> dict[str, dict]:
    """`{ns: description}` des connecteurs servis par le lecteur générique."""
    out = {}
    for c in descriptions.inscrits():
        d = descriptions.charger(c.name)
        out[descriptions.espace(d)] = d
    return out


def _migres(par_connecteur):
    return {ns: outils for ns, outils in par_connecteur.items() if ns not in A_MIGRER}


def _migres_par_verbe(par_connecteur):
    """Les connecteurs migrés dont la garde lit le VERBE des ops : pas ceux que le lecteur sert
    (leur garde lit la description, `test_un_connecteur_servi_suit_sa_description`)."""
    servis = _servis_decrits()
    return {ns: outils for ns, outils in _migres(par_connecteur).items() if ns not in servis}


def test_un_connecteur_servi_suit_sa_description(par_connecteur):
    for ns, d in _servis_decrits().items():
        outils = par_connecteur[ns]
        attendu = descriptions.outils(d)
        assert set(outils) == set(attendu), (ns, sorted(outils))
        for nom, fonctions in attendu.items():
            assert _ops(outils[nom]) == [f["name"] for f in fonctions], nom
            agit = nom != ns
            assert all(descriptions.agit(f) is agit for f in fonctions), nom
            annotation = (ACTE if agit else SIMULATION if descriptions.lecteur_simule(fonctions) else LECTURE)
            # Le titre est propre à chaque outil (`tool_alias.poser_les_titres`) : la
            # convention porte sur les indices, pas sur lui.
            assert outils[nom].annotations.model_copy(update={"title": None}) == annotation, nom


def test_un_connecteur_migre_ne_sert_que_sa_paire(par_connecteur):
    for ns, outils in _migres(par_connecteur).items():
        assert set(outils) <= {ns, f"{ns}_act"}, (ns, sorted(outils))


def test_a_migrer_ne_fait_que_retrecir(par_connecteur):
    for ns in A_MIGRER:
        assert ns in par_connecteur, f"`{ns}` n'existe plus : retire-le de A_MIGRER"
        assert not set(par_connecteur[ns]) <= {ns, f"{ns}_act"}, (
            f"`{ns}` sert sa paire : retire-le de A_MIGRER")


def test_les_annotations_disent_qui_lit_et_qui_agit(par_connecteur):
    for ns, outils in _migres_par_verbe(par_connecteur).items():
        if ns in outils:
            a = outils[ns].annotations
            assert a is not None, ns
            if a.readOnlyHint is not True:
                assert (a.readOnlyHint, a.destructiveHint, a.openWorldHint) == (
                    False, False, False), f"`{ns}` : LECTURE ou SIMULATION, rien d'autre"
                assert set(_ops(outils[ns])) & SIMULATIONS, (
                    f"`{ns}` n'est pas en lecture seule sans simuler quoi que ce soit")
        act = outils.get(f"{ns}_act")
        if act is not None:
            a = act.annotations
            assert a is not None and a.readOnlyHint is False, ns
            ecrits = a.model_dump(exclude_unset=True)
            assert "destructiveHint" in ecrits and "openWorldHint" in ecrits, (
                f"`{ns}_act` : destructiveHint et openWorldHint s'ÉCRIVENT")


def test_l_outil_qui_lit_n_a_aucune_op_ni_aucun_parametre_qui_agit(par_connecteur):
    for ns, outils in _migres_par_verbe(par_connecteur).items():
        if ns not in outils:
            continue
        for op in _ops(outils[ns]):
            assert op in SIMULATIONS or not VERBES_EFFET.match(op), (ns, op)
        for p in outils[ns].parameters.get("properties") or {}:
            assert not PARAMETRES_DECLENCHEURS.match(p), (ns, p)


def test_l_outil_qui_agit_n_a_que_des_ops_a_effet(par_connecteur):
    for ns, outils in _migres_par_verbe(par_connecteur).items():
        act = outils.get(f"{ns}_act")
        if act is None:
            continue
        ops = _ops(act)
        assert ops, f"`{ns}_act` sans `op` : son geste n'est pas nommé"
        for op in ops:
            assert VERBES_EFFET.match(op), (f"{ns}_act", op)


def test_les_anciens_noms_ne_sont_plus_servis_et_leur_remplacant_sert_l_op(par_connecteur):
    servis = {nom: t for outils in par_connecteur.values() for nom, t in outils.items()}
    for r in outils_retires.RENOMMAGES:
        assert r.ancien not in servis, r.ancien
        assert r.outil in servis, r
        assert r.op in _ops(servis[r.outil]), r
        assert r.ancien in outils_retires.RETIRES, r.ancien
        assert f'`{r.outil}(op="{r.op}"' in outils_retires.RETIRES[r.ancien].message, r


def test_un_ancien_nom_garde_tout_son_sens_dans_les_listes():
    """Ce qu'un ancien outil savait faire, ses remplaçants le savent : une liste
    d'outils MASQUÉS réécrite garde tout son sens (et, au lot 0062, une liste autorisée
    aussi)."""
    assert outils_retires.remplacants("gmail_compose") == ("gmail", "gmail_act")
    assert outils_retires.remplacants("gmail_message") == ("gmail", "gmail_act")
    assert outils_retires.remplacants("gmail_list_accounts") == ("gmail",)
    assert outils_retires.remplacants("outlook_compose") == ("outlook", "outlook_act")
    assert outils_retires.remplacants("whatsapp_chat") == ("whatsapp", "whatsapp_act")
    assert outils_retires.remplacants("inconnu") == ()
    # Un lecteur qui RESTE servi (lot des envois) : lui-même, et l'acteur de ses ops.
    assert outils_retires.remplacants("apollo_email") == ("apollo_email", "apollo_act")
    assert outils_retires.remplacants("linkedin_unipile_publish") == (
        "linkedin_unipile_act",)


def test_autoriser_un_ancien_lecteur_n_autorise_que_la_lecture():
    """Décision du 21/10/2026 : autoriser reste étroit — jamais plus qu'avant. Un ancien
    outil MIXTE (le lecteur qui envoyait encore en transition) ne donne que le nouveau
    lecteur ; un ancien outil d'envoi dédié donne `<ns>_act` seulement si `<ns>_act` ne
    fait rien de plus que lui, sinon RIEN."""
    r = outils_retires.remplacants_autorises
    assert r("whatsapp_chat") == ("whatsapp",)
    assert r("whatsapp_send") == ("whatsapp_act",)
    assert r("linkedin_unipile_chat") == ("linkedin_unipile",)
    # `linkedin_unipile_act` publie, invite et envoie : aucun outil dédié ne le donne.
    assert r("linkedin_unipile_send") == ()
    assert r("teams_message") == ("teams",) and r("teams_spaces") == ("teams",)
    assert r("teams_send") == ("teams_act",)
    assert r("chat_message") == ("chat",) and r("chat_send") == ("chat_act",)
    # Lot des envois : le lecteur mixte reste lui-même, l'outil d'envoi dédié → acteur.
    assert r("apollo_email") == ("apollo_email",)
    assert r("linkedin_unipile_post") == ("linkedin_unipile_post",)
    assert r("linkedin_unipile_publish") == () and r("linkedin_unipile_invite") == ()
    assert r("lemlist_inbox_send") == ("lemlist_act",)
    # Lot des personas (23/10/2026) : le lecteur reste lui-même ; `lemlist_act` porte aussi
    # les personas, et `lemlist_inbox_send` le donne toujours (choix assumé).
    assert r("lemlist_database") == ("lemlist_database",)
    assert outils_retires.remplacants("lemlist_database") == ("lemlist_database",
                                                              "lemlist_act")
    # Masquer reste large : masquer l'ancien outil dédié masque l'acteur.
    assert outils_retires.remplacants("linkedin_unipile_invite") == ("linkedin_unipile_act",)
    perdus = {"linkedin_unipile_send", "linkedin_unipile_publish", "linkedin_unipile_invite"}
    for ancien in dict.fromkeys(x.ancien for x in outils_retires.RENOMMAGES_MESSAGERIES
                                + outils_retires.RENOMMAGES_ENVOIS
                                + outils_retires.OPS_DEPLACEES):
        nouveaux = r(ancien)
        assert bool(nouveaux) == (ancien not in perdus), (ancien, nouveaux)
        for acteur in (n for n in nouveaux if n.endswith("_act")):
            assert not outils_retires.ops_en_plus(ancien, acteur), (ancien, acteur)
        assert all(n.endswith("_act") for n in nouveaux) or not any(
            n.endswith("_act") for n in nouveaux), (ancien, nouveaux)


def test_les_tables_donnent_a_chaque_acteur_toutes_ses_ops(par_connecteur):
    """`remplacants_autorises` compare ce que l'ancien outil faisait aux ops de l'acteur
    lues dans les tables : elles doivent être TOUTES celles de sa signature, sinon un
    acteur qui fait plus passerait pour équivalent."""
    servis = {nom: t for outils in par_connecteur.values() for nom, t in outils.items()}
    acteurs = {r.outil for r in outils_retires.RENOMMAGES_MESSAGERIES
               + outils_retires.RENOMMAGES_ENVOIS + outils_retires.OPS_DEPLACEES
               if r.outil.endswith("_act")}
    for acteur in acteurs:
        assert outils_retires.ops_de_l_acteur(acteur) == set(_ops(servis[acteur])), acteur


def test_le_lot_0062_est_fige():
    """0062 est jouée : sa table ne reçoit plus aucune ligne (un lot neuf a la sienne)."""
    assert {r.ancien for r in outils_retires.RENOMMAGES_ACT} == {
        "gmail_list_accounts", "gmail_message", "gmail_compose", "outlook_message",
        "outlook_compose"}


def test_le_refus_d_un_ancien_nom_dit_son_jour_et_sa_paire():
    """Le refus nomme le lecteur et l'acteur de la PAIRE, pas un préfixe deviné du nom
    retiré (`linkedin_unipile_chat` n'est pas `linkedin`), et le jour de son lot."""
    m = outils_retires.RETIRES["linkedin_unipile_chat"].message
    assert "(21/10/2026, security)" in m
    assert "`linkedin_unipile` reads" in m and "`linkedin_unipile_act` acts" in m
    assert "(17/10/2026, security)" in outils_retires.RETIRES["gmail_compose"].message
    # Un outil d'envoi du lot des envois n'a pas de lecteur `<ns>` à nommer.
    m = outils_retires.RETIRES["lemlist_inbox_send"].message
    assert "`lemlist` reads" not in m and "`lemlist_act` acts" in m
    assert "`linkedin_unipile` reads" not in outils_retires.RETIRES[
        "linkedin_unipile_invite"].message
    # L'autorisation ne passe pas à un acteur qui fait plus : le refus le dit.
    m = outils_retires.RETIRES["linkedin_unipile_publish"].message
    assert "Allowing `linkedin_unipile_publish` does NOT allow `linkedin_unipile_act`" in m
    assert "op=send" in m and "op=invite" in m and "op=publish_post" not in m.split(
        "does more")[1]
    assert "does NOT allow" not in outils_retires.RETIRES["whatsapp_send"].message
    assert "does NOT allow" not in outils_retires.RETIRES["lemlist_inbox_send"].message


def test_une_op_deplacee_a_quitte_son_lecteur_et_son_acteur_la_sert(par_connecteur):
    """Lot des envois : le lecteur reste servi, l'op d'envoi n'y est plus, l'acteur la
    sert, et l'appel de l'ancienne op refuse en nommant le nouveau."""
    servis = {nom: t for outils in par_connecteur.values() for nom, t in outils.items()}
    for r in outils_retires.OPS_DEPLACEES:
        ancienne = r.selecteur.split(", ")[0].removeprefix("op=")
        assert r.ancien in servis, r
        assert ancienne not in _ops(servis[r.ancien]), r
        assert r.op in _ops(servis[r.outil]), r
        assert r.outil == f"{tool_visibility.namespace_of(r.ancien)}_act", r
        refus = outils_retires.op_deplacee(r.ancien, ancienne)
        assert refus is not None and f'`{r.outil}(op="{r.op}"' in refus.message, r
    assert outils_retires.op_deplacee("apollo_email", "search") is None
    assert not {r.ancien for r in outils_retires.OPS_DEPLACEES} & set(outils_retires.RETIRES)


def test_l_appel_d_une_op_deplacee_refuse_en_nommant_l_acteur():
    """Le vrai chemin : la signature refuse l'op, la taxonomie nomme le nouvel appel."""
    from oto_mcp import error_taxonomy

    try:
        asyncio.run(static_mcp().call_tool("apollo_email", {"op": "send",
                                                            "message_id": "m1"}))
    except Exception as e:  # noqa: BLE001 — la taxonomie la classe ci-dessous
        info = error_taxonomy.classify(e)
    else:
        pytest.fail("apollo_email(op='send') ne doit plus rien servir")
    assert info.code == "invalid_input"
    assert '`apollo_act(op="send_email", …)`' in info.message


def test_l_annonce_se_sert_jusqu_a_sa_date_puis_se_tait():
    import datetime

    fin = outils_retires.ANNONCE_ACT_JUSQU_AU
    annonce = outils_retires.annonce_act_du_jour(fin)
    for r in outils_retires.RENOMMAGES:
        assert f"`{r.ancien}`" in annonce and f"`{r.outil}`" in annonce, r
    assert outils_retires.annonce_act_du_jour(fin + datetime.timedelta(days=1)) is None


def test_la_migration_reecrit_sur_place_sans_doublon():
    import importlib.util
    from pathlib import Path

    import oto_mcp

    chemin = (Path(oto_mcp.__file__).parent / "db" / "migrations" / "versions"
              / "20261017_0062_outils_act.py")
    spec = importlib.util.spec_from_file_location("revision_0062", chemin)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    table = m.correspondance()
    assert m.reecrire(["serper_search", "gmail_message", "gmail_compose", "outlook_compose"],
                      table) == ["serper_search", "gmail", "gmail_act", "outlook",
                                 "outlook_act"]
    with pytest.raises(ValueError):
        m.reecrire([{"name": "gmail_message"}], table)


def test_la_revision_des_messageries_autorise_etroit_et_masque_large():
    import importlib.util
    from pathlib import Path

    import oto_mcp

    chemin = (Path(oto_mcp.__file__).parent / "db" / "migrations" / "versions"
              / "20261021_1219_outils_act_msg.py")
    spec = importlib.util.spec_from_file_location("revision_messageries", chemin)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.down_revision == "0072_triggers_hook_sources"
    autorise, masque = m.correspondances(outils_retires.RENOMMAGES_MESSAGERIES)
    reecrire = m._revision_0062().reecrire
    assert reecrire(["whatsapp_chat"], autorise) == ["whatsapp"]
    assert reecrire(["whatsapp_chat", "whatsapp_send", "teams_spaces", "teams_message",
                     "fr_get"], autorise) == ["whatsapp", "whatsapp_act", "teams", "fr_get"]
    assert reecrire(["whatsapp_chat"], masque) == ["whatsapp", "whatsapp_act"]
    assert not set(autorise) & {r.ancien for r in outils_retires.RENOMMAGES_ACT}


def test_la_revision_des_envois_garde_le_lecteur_et_n_autorise_pas_l_acteur():
    import importlib.util
    from pathlib import Path

    import oto_mcp

    chemin = (Path(oto_mcp.__file__).parent / "db" / "migrations" / "versions"
              / "20261021_1352_outils_act_envois.py")
    spec = importlib.util.spec_from_file_location("revision_envois", chemin)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.down_revision == "20261021_1219_outils_act_msg"
    autorise, masque = m.correspondances()
    reecrire = m._revision_des_messageries()._revision_0062().reecrire
    assert reecrire(["apollo_email", "linkedin_unipile_publish", "fr_get"], autorise) == [
        "apollo_email", "fr_get"]
    assert reecrire(["lemlist_inbox_send"], autorise) == ["lemlist_act"]
    # Joué seul avant, le lot des messageries avait donné l'acteur LinkedIn pour la
    # messagerie : ce lot l'élargit, l'autorisation se perd ; le masquage reste.
    assert m.acteurs_elargis() == ("linkedin_unipile_act",)
    assert reecrire(["linkedin_unipile_act", "fr_get"], autorise) == ["fr_get"]
    assert reecrire(["linkedin_unipile_act"], masque) == ["linkedin_unipile_act"]
    assert reecrire(["linkedin_unipile_post"], masque) == ["linkedin_unipile_post",
                                                           "linkedin_unipile_act"]
    assert reecrire(["linkedin_unipile_publish"], masque) == ["linkedin_unipile_act"]
    assert not set(autorise) & {r.ancien for r in outils_retires.RENOMMAGES_ACT
                                + outils_retires.RENOMMAGES_MESSAGERIES}
    # Le lot des personas (23/10/2026) n'entre pas dans cette révision déjà jouée.
    assert "lemlist_database" not in autorise and "lemlist_database" not in masque


def test_la_revision_des_personas_masque_l_acteur_et_garde_l_envoi():
    """Lot des personas (23/10/2026) : `lemlist_database` ne fait plus que lire. Masqué,
    il masque aussi `lemlist_act` ; autorisé, il reste lui-même ; `lemlist_act` autorisé
    n'est pas touché (choix assumé : il garde l'envoi et reçoit les personas)."""
    import importlib.util
    from pathlib import Path

    import oto_mcp

    chemin = (Path(oto_mcp.__file__).parent / "db" / "migrations" / "versions"
              / "20261023_0713_lemlist_personas.py")
    spec = importlib.util.spec_from_file_location("revision_personas", chemin)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.down_revision == "20261022_2112_forfait_fenetre"
    autorise, masque = m.correspondances()
    assert set(autorise) == set(masque) == {"lemlist_database"}
    reecrire = m._revision_des_messageries()._revision_0062().reecrire
    assert reecrire(["lemlist_database", "fr_get"], autorise) == ["lemlist_database",
                                                                  "fr_get"]
    assert reecrire(["lemlist_act", "lemlist_inbox"], autorise) == ["lemlist_act",
                                                                    "lemlist_inbox"]
    assert reecrire(["lemlist_database"], masque) == ["lemlist_database", "lemlist_act"]


#: Les connecteurs encore dans `A_MIGRER` dont la PAIRE est déjà née : leurs outils
#: d'avant restent, mais `<ns>` et `<ns>_act` ont la forme cible dès aujourd'hui.
PAIRES_NEES = ("api", "slack", "linkedin_unipile")
#: Les connecteurs encore dans `A_MIGRER` dont l'ACTEUR est né (lot des envois,
#: 21/10/2026) avant leur lecteur `<ns>` : leurs lecteurs gardent leur nom, ce qui atteint
#: un tiers est dans `<ns>_act`, qui a déjà la forme cible.
ACTEURS_NES = ("apollo", "lightfield", "luma", "pennylane", "signwell", "productlane",
               "ahrefs", "lemlist")


@pytest.mark.parametrize("ns", PAIRES_NEES)
def test_une_paire_nee_avant_la_migration_a_deja_la_forme_cible(par_connecteur, ns):
    outils = {n: t for n, t in par_connecteur[ns].items() if n in (ns, f"{ns}_act")}
    assert set(outils) == {ns, f"{ns}_act"}, (ns, sorted(par_connecteur[ns]))
    lecteur, acteur = outils[ns], outils[f"{ns}_act"]
    assert lecteur.annotations is not None and lecteur.annotations.readOnlyHint is True
    for op in _ops(lecteur):
        assert not VERBES_EFFET.match(op), (ns, op)
    for p in lecteur.parameters.get("properties") or {}:
        assert not PARAMETRES_DECLENCHEURS.match(p), (ns, p)
    a = acteur.annotations
    assert a is not None and a.readOnlyHint is False
    assert {"destructiveHint", "openWorldHint"} <= set(a.model_dump(exclude_unset=True))
    assert _ops(acteur) and all(VERBES_EFFET.match(op) for op in _ops(acteur)), (
        f"{ns}_act", _ops(acteur))


@pytest.mark.parametrize("ns", ACTEURS_NES)
def test_un_acteur_ne_avant_son_lecteur_a_deja_la_forme_cible(par_connecteur, ns):
    acteur = par_connecteur[ns].get(f"{ns}_act")
    assert acteur is not None, ns
    a = acteur.annotations
    assert a is not None and a.readOnlyHint is False
    assert {"destructiveHint", "openWorldHint"} <= set(a.model_dump(exclude_unset=True))
    assert _ops(acteur) and all(VERBES_EFFET.match(op) for op in _ops(acteur)), (
        f"{ns}_act", _ops(acteur))
