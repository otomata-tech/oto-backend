"""Poser un schéma et partager un tableau, en capacités (#302).

Les quatre derniers chemins datastore écrits à la main (`PUT …/schema`, et les trois
verbes de `…/share`). Deux points valaient un test à eux seuls, parce qu'ils cassent
en silence quand on migre sans y penser :

1. le corps d'un `DELETE …/share` : un DELETE ne porte pas de corps d'ordinaire, et
   l'adaptateur ne le lit que sur déclaration. Sans elle, chaque retrait de partage
   serait devenu `email_required` — pour un chemin dont le client vit hors du dépôt
   (`oto-core.DatastoreClient.unshare`) ;
2. le champ d'entrée du schéma s'appelle `schema` sur le fil, et la garde de champ
   inconnu compare des noms PYTHON : un alias aurait fait refuser le corps que le
   dashboard envoie depuis toujours.
"""
from __future__ import annotations

import pytest

from _datastore_rest import Boom, call, cap, stub_authz

from oto_mcp.capabilities.datastore import schema as dss
from oto_mcp.capabilities.datastore import sharing as dsh
from oto_mcp.datastore.core import NamespaceNotFound, NamespaceReadOnly

NS = {"namespace": "vivier"}
SCHEMA = {"fields": [{"key": "societe", "role": "title"}], "key": "siren"}


@pytest.fixture(autouse=True)
def _sans_db(monkeypatch):
    stub_authz(monkeypatch)


# --- poser le schéma --------------------------------------------------------------

class _SchemaStore:
    def __init__(self, out=None):
        self.calls: list = []
        self.out = out

    def set_schema(self, namespace, schema):
        self.calls.append((namespace, schema))
        return self.out or {"namespace": namespace, "schema": schema}


def test_le_schema_part_tel_quel_et_revient_tel_quel(monkeypatch):
    store = _SchemaStore()
    monkeypatch.setattr(dss, "make_store", lambda sub: store)
    status, corps = call("me.datastore.set_schema", path_params=NS,
                         body={"schema": SCHEMA})
    assert status == 200
    # `unknown_keys_warning` s'ajoute au corps depuis oto#56, TOUJOURS présent et à
    # `None` quand il n'y a rien à dire : la clé constante est ce qui permet à un
    # client de distinguer « rien à signaler » d'un serveur trop vieux. Le schéma,
    # lui, part et revient inchangé — c'est ce que ce banc garde.
    assert corps == {"namespace": "vivier", "schema": SCHEMA,
                     "unknown_keys_warning": None}
    assert store.calls == [("vivier", SCHEMA)]


def test_un_schema_nul_repasse_le_tableau_en_table_libre(monkeypatch):
    """`{schema: null}` et un corps vide se confondent — c'est le comportement de la
    route d'avant (`body.get("schema")`), et le seul geste qui RETIRE un schéma."""
    store = _SchemaStore()
    monkeypatch.setattr(dss, "make_store", lambda sub: store)
    call("me.datastore.set_schema", path_params=NS, body={"schema": None})
    call("me.datastore.set_schema", path_params=NS, body={})
    assert store.calls == [("vivier", None), ("vivier", None)]


def test_lavertissement_de_configuration_remonte_a_lauteur(monkeypatch):
    """Un statut sans état terminal rend la file de travail incapable de libérer :
    ça se dit à celui qui pose le schéma, au moment où il le pose."""
    monkeypatch.setattr(dss, "make_store", lambda sub: _SchemaStore(
        {"namespace": "vivier", "schema": SCHEMA, "warning": "statut sans état terminal"}))
    corps = call("me.datastore.set_schema", path_params=NS, body={"schema": SCHEMA})[1]
    assert corps["warning"] == "statut sans état terminal"


@pytest.mark.parametrize("exc,status,code", [
    (NamespaceNotFound("v"), 404, "namespace_not_found"),
    (NamespaceReadOnly("v"), 403, "namespace_read_only"),
    (ValueError("schema.key='siren' refusée : 3 valeurs en DOUBLON"), 400,
     "invalid_schema"),
])
def test_les_refus_de_pose_gardent_leur_code(monkeypatch, exc, status, code):
    monkeypatch.setattr(dss, "make_store", lambda sub: Boom(exc))
    assert call("me.datastore.set_schema", path_params=NS,
                body={"schema": SCHEMA})[1]["error"] == code


def test_le_detail_dun_schema_refuse_reste_tu(monkeypatch):
    """Inchangé : le message du store cite des VALEURS de données (échantillon de
    doublons). L'ouvrir serait un choix, pas une migration."""
    monkeypatch.setattr(dss, "make_store", lambda sub: Boom(ValueError("siren=111 ×3")))
    assert call("me.datastore.set_schema", path_params=NS,
                body={"schema": SCHEMA})[1]["detail"] is None


def test_le_champ_dentree_sappelle_bien_schema():
    """La garde de champ inconnu compare des noms python : un alias aurait fait
    refuser le corps que le dashboard envoie depuis toujours."""
    assert "schema" in cap("me.datastore.set_schema").Input.model_fields


# --- partager ---------------------------------------------------------------------

@pytest.fixture
def gouvernance(monkeypatch):
    """Le tableau existe et l'acteur le gouverne — sauf si le test dit l'inverse."""
    etat = {"can_govern": True}
    monkeypatch.setattr("oto_mcp.capabilities.datastore.common.make_store",
                        lambda sub: type("S", (), {"resolve_ns_id": lambda self, ns: 42})())
    monkeypatch.setattr("oto_mcp.capabilities.datastore.common.ownership.can_govern",
                        lambda *a: etat["can_govern"])
    monkeypatch.setattr(dsh.db, "get_user_by_email",
                        lambda email: {"sub": "u-2"} if email == "sarah@x.fr" else None)
    return etat


def test_le_partage_rend_ok_et_ce_qui_a_ete_accorde(monkeypatch, gouvernance):
    vus: list = []
    monkeypatch.setattr(dsh.ownership, "grant",
                        lambda *a, **k: vus.append((a, k)))
    assert call("me.datastore.share", path_params=NS,
                body={"email": "sarah@x.fr", "permission": "read"}) == (
        200, {"ok": True, "namespace": "vivier", "shared_with": "sarah@x.fr",
              "permission": "read"})
    (rt, rid, ptype, pid, perm), kw = vus[0]
    assert (rt, rid, ptype, pid, perm) == ("datastore_namespace", "42", "user",
                                           "u-2", "read")
    assert kw["granted_by"] == "u-1"


def test_le_partage_defaut_est_la_LECTURE(monkeypatch, gouvernance):
    """ADR 0068 — ce banc enregistrait `"write"` : partager sans rien préciser donnait
    l'ÉCRITURE. Il n'a jamais défendu ce défaut, il l'a constaté (aucune raison écrite,
    contrairement au cliquet de `oto_resource`, qui lui reste à `"write"` parce que son
    schéma servi est figé sur des appelants mesurés).

    « Partager », pour qui le demande, veut dire « qu'il puisse le lire ». Donner
    l'écriture en plus, c'est accorder ce qu'on n'a pas demandé — et sur un tableau,
    l'écriture n'ajoute pas un droit, elle en retire un à son propriétaire : celui
    d'être seul à décider de ce qu'il contient.

    ⚠️ C'est une rupture de comportement pour les appelants de `data_share` et de
    `POST /api/datastore/namespaces/{ns}/share` : un partage cessera de permettre
    l'écriture. Le sens du changement est RESTRICTIF — l'effet se voit (un refus
    d'écriture), il ne se cache pas."""
    monkeypatch.setattr(dsh.ownership, "grant", lambda *a, **k: None)
    assert call("me.datastore.share", path_params=NS,
                body={"email": "sarah@x.fr"})[1]["permission"] == "read"
    # Et l'écriture reste accessible, en le disant.
    assert call("me.datastore.share", path_params=NS,
                body={"email": "sarah@x.fr", "permission": "write"})[1]["permission"] == "write"


def test_un_email_manquant_est_un_refus_nomme(gouvernance):
    assert call("me.datastore.share", path_params=NS, body={})[1]["error"] == "email_required"
    assert call("me.datastore.unshare", path_params=NS,
                body={})[1]["error"] == "email_required"


def test_une_permission_inconnue_est_refusee(gouvernance):
    status, corps = call("me.datastore.share", path_params=NS,
                         body={"email": "sarah@x.fr", "permission": "admin"})
    assert status == 400
    assert corps["error"] == "permission must be 'read' or 'write'"


def test_un_destinataire_sans_compte_oto_est_un_404_qui_le_dit(gouvernance):
    status, corps = call("me.datastore.share", path_params=NS,
                         body={"email": "inconnu@x.fr"})
    assert (status, corps["error"]) == (404, "no oto user with email inconnu@x.fr")


def test_partager_exige_la_gouvernance_pas_un_role(monkeypatch, gouvernance):
    """ADR 0030/0048 : propriétaire ∪ gérant ∪ escalade — jamais « admin de l'org »."""
    gouvernance["can_govern"] = False
    monkeypatch.setattr(dsh.ownership, "grant", lambda *a, **k: pytest.fail("accordé !"))
    assert call("me.datastore.share", path_params=NS,
                body={"email": "sarah@x.fr"})[1]["error"] == "forbidden"


def test_le_retrait_lit_bien_le_corps_du_delete(monkeypatch, gouvernance):
    """LE piège de ce lot : sans `reads_body`, ce corps serait ignoré et chaque appel
    d'`oto-core.unshare` serait devenu `email_required`."""
    monkeypatch.setattr(dsh.ownership, "revoke", lambda *a: True)
    assert call("me.datastore.unshare", path_params=NS,
                body={"email": "sarah@x.fr"}) == (
        200, {"ok": True, "namespace": "vivier", "removed": "sarah@x.fr"})


def test_retirer_un_partage_inexistant_est_un_404_qui_le_dit(monkeypatch, gouvernance):
    monkeypatch.setattr(dsh.ownership, "revoke", lambda *a: False)
    status, corps = call("me.datastore.unshare", path_params=NS,
                         body={"email": "sarah@x.fr"})
    assert (status, corps["error"]) == (
        404, "no active share for sarah@x.fr on vivier")


def test_la_liste_des_partages_aplatit_les_grants(monkeypatch, gouvernance):
    monkeypatch.setattr(dsh.ownership, "list_grants", lambda rt, rid: [
        {"email": "sarah@x.fr", "permission": "write", "principal_type": "user",
         "principal_id": "u-2", "granted_at": "2026-08-12 09:00:00", "role": "editor"}])
    assert call("me.datastore.list_shares", path_params=NS) == (200, {"shares": [
        {"email": "sarah@x.fr", "permission": "write", "principal_type": "user",
         "principal_id": "u-2", "created_at": "2026-08-12 09:00:00"}]})


def test_lister_les_partages_exige_la_gouvernance(gouvernance):
    gouvernance["can_govern"] = False
    assert call("me.datastore.list_shares", path_params=NS)[1]["error"] == "forbidden"


# --- le contrat --------------------------------------------------------------------

def test_les_quatre_capacites_declarent_leur_sortie_et_restent_rest_only():
    for cle in ("set_schema", "list_shares", "share", "unshare"):
        c = cap(f"me.datastore.{cle}")
        assert c.Output is not None, cle
        assert c.mcp is None, f"{cle} : ce lot ne migre que la face REST"


def test_le_datastore_na_plus_aucune_route_ecrite_a_la_main():
    """La preuve de fin de lot : `api/datastore` ne sert plus un seul chemin
    `/api/datastore/*` — ils sont tous dérivés d'un descripteur."""
    from oto_mcp.api import datastore as ard

    async def _noop(request):
        return None

    routes = ard.make_routes(None, _noop, lambda *a, **k: None, lambda *a, **k: None,
                             lambda o: {}, _noop)
    assert not [r for r in routes if r.path.startswith("/api/datastore/")]
