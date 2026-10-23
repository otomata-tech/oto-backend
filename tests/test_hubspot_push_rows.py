"""`hubspot_push_rows` — des lignes poussées en contacts PAR RÉFÉRENCE, sur une vraie base.

Le vrai store et un client HubSpot simulé à la CLASSE. Ce qu'on éprouve :

1. Rapprocher sans deviner : UNE recherche `IN` pour tout le lot ; l'existant n'est
   pas touché par défaut (mis à jour sur demande), le reste créé ; deux
   enregistrements pour une valeur font échouer la ligne ; deux lignes à la même
   valeur ne créent qu'un enregistrement.
2. Associer et ranger dans une liste, par ids lus côté serveur.
3. L'échec partiel : chaque ligne a son code, les autres passent.
4. **Aucune donnée personnelle dans la réponse.**
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import MagicMock

import pytest

SUB = "sub-push-hubspot"
_GENS = [
    {"email": "Ada@Acme.test", "prenom": "Ada", "societe_id": "900"},
    {"email": "alan@acme.test", "prenom": "Alan", "societe_id": "901"},
    {"email": "grace@acme.test", "prenom": "Grace"},
]
_MAPPING = {"email": "email", "firstname": "prenom"}
_PERSONNEL = ["Ada@Acme.test", "ada@acme.test", "alan@acme.test", "grace@acme.test",
              "Ada", "Alan", "Grace"]


@pytest.fixture
def banc(live, monkeypatch):
    import oto.tools.hubspot.client as hubspot_client
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import hubspot_lignes
    client = MagicMock()
    # alan existe déjà chez HubSpot ; personne d'autre.
    client.search_objects.return_value = {"results": [
        {"id": "501", "properties": {"email": "alan@acme.test"}}]}
    client.create_object.side_effect = lambda t, props, **kw: {
        "id": "7" + str(client.create_object.call_count)}
    client.update_object.return_value = {}
    client.get_list.return_value = {"list": {"processingType": "MANUAL"}}
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr("oto_mcp.auth.hubspot.bearer", lambda *a, **k: "k")
    monkeypatch.setattr(hubspot_client, "HubSpotClient", lambda **kw: client)
    m = FastMCP("t")
    hubspot_lignes.register(m)
    return asyncio.run(m.get_tool("hubspot_push_rows")).fn, client


def _table(rows=_GENS) -> tuple[str, list[str]]:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "contacts-" + uuid.uuid4().hex[:6]
    db.create_datastore("user", SUB, ns)
    st = make_store(SUB)
    # Les colonnes lues ET celles où l'outil réécrit l'id et l'état, déclarées.
    st.set_schema(ns, {"fields": [{"key": k} for k in (
        "email", "prenom", "societe_id", "hubspot_id", "hubspot_status")]})
    return ns, [str(st.append_row(ns, dict(r))["_id"]) for r in rows]


def _ligne(ns, rid) -> dict:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).get_row(ns, rid)


def _sans_donnee_personnelle(out: dict) -> None:
    texte = json.dumps(out, ensure_ascii=False)
    for v in _PERSONNEL:
        assert v not in texte, f"la réponse porte une donnée de la personne : {v!r}"


def test_l_existant_est_mis_a_jour_le_reste_cree_en_une_recherche(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
             on_existing="update")

    assert client.search_objects.call_count == 1, "UNE recherche pour tout le lot"
    filtre = client.search_objects.call_args.kwargs["filters"][0]
    assert filtre["operator"] == "IN"
    assert sorted(filtre["values"]) == ["ada@acme.test", "alan@acme.test",
                                        "grace@acme.test"], "comparé sans casse"
    assert out["created"] == 2 and out["updated"] == 1 and out["errors"] == []
    client.update_object.assert_called_once_with(
        "contacts", "501", {"email": "alan@acme.test", "firstname": "Alan"})
    assert _ligne(ns, ids[1])["hubspot_id"] == "501"
    assert _ligne(ns, ids[1])["hubspot_status"] == "updated"
    assert _ligne(ns, ids[0])["hubspot_status"] == "created"
    _sans_donnee_personnelle(out)


def test_une_ligne_qui_porte_son_id_est_mise_a_jour_sans_recherche(banc):
    fn, client = banc
    ns, ids = _table([{"email": "ada@acme.test", "prenom": "Ada", "hubspot_id": "42"}])
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
             on_existing="update")
    assert not client.search_objects.called
    assert out["updated"] == 1
    assert client.update_object.call_args.args[1] == "42"


def test_deux_enregistrements_pour_une_valeur_font_echouer_la_ligne(banc):
    fn, client = banc
    client.search_objects.return_value = {"results": [
        {"id": "1", "properties": {"email": "alan@acme.test"}},
        {"id": "2", "properties": {"email": "alan@acme.test"}}]}
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids)
    assert {e["row_id"]: e["code"] for e in out["errors"]} == {
        ids[1]: "hubspot_ambiguous_match"}
    assert out["created"] == 2 and out["failed"] == 1
    assert not client.update_object.called, "aucun des deux n'est choisi"
    _sans_donnee_personnelle(out)


def test_associer_et_ranger_dans_une_liste(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
             on_existing="update",
             associate_with="companies", associate_id_column="societe_id",
             list_id="L9")
    chemins = [c.args[1] for c in client._request.call_args_list]
    assert any(c.endswith("/associations/default/companies/900") for c in chemins)
    assert any(c.endswith("/associations/default/companies/901") for c in chemins)
    assert out["associated"] == 2, "la ligne sans société n'est pas associée"
    ajoutes = client.add_list_memberships.call_args.args
    assert ajoutes[0] == "L9" and len(ajoutes[1]) == 3
    assert out["added_to_list"] == 3


def test_une_liste_dynamique_est_refusee_avant_toute_ecriture(banc):
    from oto_mcp.mcp_errors import McpError
    fn, client = banc
    client.get_list.return_value = {"list": {"processingType": "DYNAMIC"}}
    ns, ids = _table()
    with pytest.raises(McpError) as e:
        fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
           list_id="L9")
    assert e.value.error.data["code"] == "hubspot_list_dynamic"
    assert not client.create_object.called and not client.update_object.called


def test_echec_partiel_une_ligne_refusee_les_autres_passent(banc):
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client = banc
    client.search_objects.return_value = {"results": []}

    def create(t, props, **kw):
        if props["email"] == "grace@acme.test":
            raise UpstreamHTTPError(400, {"message": "Property values were not valid"},
                                    service="hubspot")
        return {"id": "8" + props["firstname"][0]}
    client.create_object.side_effect = create

    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids)
    assert out["created"] == 2 and out["failed"] == 1
    assert out["errors"] == [{"row_id": ids[2], "code": "hubspot_http_400"}]
    assert _ligne(ns, ids[2])["hubspot_status"] == "failed"
    assert _ligne(ns, ids[2])["hubspot_status.comment"] == "hubspot_http_400"
    _sans_donnee_personnelle(out)


def test_une_propriete_inconnue_arrete_le_lot_au_lieu_d_echouer_chaque_ligne(banc):
    """Une colonne mappée vers une propriété que le portail n'a pas : TOUTES les
    lignes échoueraient pareil. Un arrêt, qui nomme la propriété — pas N
    `hubspot_http_400` muets."""
    from oto.tools.common.errors import UpstreamHTTPError
    fn, client = banc
    client.search_objects.return_value = {"results": []}
    client.create_object.side_effect = UpstreamHTTPError(400, {
        "status": "error", "category": "VALIDATION_ERROR",
        "errors": [{"code": "PROPERTY_DOESNT_EXIST",
                    "message": 'Property "firstname" does not exist'}]},
        service="hubspot")

    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids)
    assert out["stopped"] == "hubspot_property_doesnt_exist"
    assert out["unknown_properties"] == ["firstname"]
    assert client.create_object.call_count == 1, "arrêté à la première ligne"
    assert out["errors"] == [] and "failed" not in out
    _sans_donnee_personnelle(out)


def test_la_propriete_de_rapprochement_doit_etre_mappee(banc):
    from oto_mcp.mcp_errors import McpError
    fn, client = banc
    ns, ids = _table()
    with pytest.raises(McpError) as e:
        fn(datastore=ns, object_type="contacts", field_mapping={"firstname": "prenom"},
           row_ids=ids)
    assert e.value.error.data["code"] == "hubspot_match_unmapped"
    assert not client.search_objects.called


def test_dry_run_ne_touche_pas_a_hubspot(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
             dry_run=True)
    assert out["would_push"] == 3
    assert not (client.search_objects.called or client.create_object.called)
    _sans_donnee_personnelle(out)


# ── les propriétés fixes, et les existants qu'on ne touche pas ───────────────

def test_les_proprietes_fixes_vont_sur_chaque_enregistrement_ecrit(banc):
    """`lifecyclestage`, un tag de lot : ce ne sont pas des colonnes, et une
    procédure qui crée des contacts en a besoin sur chacun."""
    fn, client = banc
    ns, ids = _table()
    fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
       on_existing="update",
       constants={"lifecyclestage": "lead", "oqy_batch": "be-02"})
    cree = client.create_object.call_args_list[0].args[1]
    assert cree["lifecyclestage"] == "lead" and cree["oqy_batch"] == "be-02"
    assert client.update_object.call_args.args[2]["lifecyclestage"] == "lead"


def test_une_propriete_a_la_fois_fixe_et_mappee_est_refusee(banc):
    from oto_mcp.mcp_errors import McpError
    fn, client = banc
    ns, ids = _table()
    with pytest.raises(McpError) as e:
        fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
           constants={"firstname": "X"})
    assert e.value.error.data["code"] == "hubspot_constant_mapped"
    assert not client.search_objects.called


def test_on_existing_skip_ne_touche_pas_l_existant(banc):
    """La règle d'une procédure réelle : un contact qui existe déjà n'est pas
    modifié. Il n'est ni mis à jour, ni associé, ni rangé dans la liste — mais sa
    ligne apprend son id, pour ne pas le rechercher au tour suivant."""
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
             list_id="L9", on_existing="skip",
             associate_with="companies", associate_id_column="societe_id")
    assert not client.update_object.called
    assert out["exists"] == 1 and out["created"] == 2
    assert out["added_to_list"] == 2, "seuls les créés rejoignent la liste"
    assert not any(c.args[1].startswith("/crm/v4/objects/contacts/501/")
                   for c in client._request.call_args_list), "pas d'association"
    assert _ligne(ns, ids[1])["hubspot_status"] == "exists"
    assert _ligne(ns, ids[1])["hubspot_id"] == "501"


def test_on_existing_list_only_range_l_existant_sans_le_modifier(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids,
             list_id="L9", on_existing="list_only")
    assert not client.update_object.called
    assert out["exists"] == 1 and out["added_to_list"] == 3
    assert "501" in client.add_list_memberships.call_args.args[1]


# ── l'existant n'est pas touché par défaut ; un lot ne crée pas deux fois ─────

def test_par_defaut_l_existant_n_est_pas_touche_et_la_reponse_le_dit(banc):
    fn, client = banc
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids)
    assert not client.update_object.called, "un CRM ne se réécrit pas sans le demander"
    assert out["exists"] == 1 and out["created"] == 2
    assert out["on_existing"] == "skip"
    assert "on_existing='update'" in out["existing_left_untouched"]
    assert _ligne(ns, ids[1])["hubspot_status"] == "exists"
    _sans_donnee_personnelle(out)


def test_rien_d_existant_rien_a_dire(banc):
    fn, client = banc
    client.search_objects.return_value = {"results": []}
    ns, ids = _table()
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING, row_ids=ids)
    assert out["created"] == 3 and "existing_left_untouched" not in out


@pytest.mark.parametrize("on_existing,attendu", [("skip", "exists"),
                                                 ("update", "updated")])
def test_deux_lignes_a_la_meme_valeur_ne_creent_qu_un_enregistrement(
        banc, on_existing, attendu):
    """Même email deux fois dans un lot : la première crée, la seconde retrouve
    l'enregistrement créé — au lieu d'un doublon (entreprise) ou d'un 409 (contact)."""
    fn, client = banc
    client.search_objects.return_value = {"results": []}
    ns, ids = _table([{"email": "ada@acme.test", "prenom": "Ada"},
                      {"email": "ADA@acme.test", "prenom": "Ada L."}])
    out = fn(datastore=ns, object_type="contacts", field_mapping=_MAPPING,
             row_ids=ids, on_existing=on_existing)
    assert client.create_object.call_count == 1
    assert out["created"] == 1 and out.get(attendu) == 1
    assert _ligne(ns, ids[1])["hubspot_id"] == _ligne(ns, ids[0])["hubspot_id"]
    assert _ligne(ns, ids[1])["hubspot_status"] == attendu
