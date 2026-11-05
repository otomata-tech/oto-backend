"""Le NUMÉRO du tableau part avec la remise, et `datastore` cesse d'être un écho.

Deux défauts, une seule clé de réponse.

**Rien ne donnait le numéro à l'agent.** Le tableau s'adresse indifféremment par son
nom ou par son numéro (`db.resolve_datastore_ns`, même prédicat de visibilité pour les
deux), mais aucune remise ne rendait le numéro : un agent qui ne l'avait pas lu dans
`data_list_datastores` ne pouvait employer que le nom. Or un agent recopie la forme
qu'on lui montre — mesuré : un exemple portant une forme a produit cinquante emplois de
cette forme et zéro de la forme équivalente non montrée. Le nom part en retrait ;
l'usage ne bascule que si le numéro arrive DANS la réponse.

**`datastore` répétait la question.** Lire par numéro rendait `datastore: "600"` : la
clé faisait l'écho de l'adresse reçue au lieu de nommer le tableau touché. Deux
appelants sur le même tableau y lisaient donc deux valeurs différentes, sans un mot.

Ces bancs éprouvent les DEUX faces d'un même geste (MCP et REST), parce que c'est là
que le datastore a déjà divergé en silence, et ils éprouvent la remise telle qu'un
agent la reçoit — pas la fonction qui la met en forme.

**Le CATALOGUE rendait le nombre sous un AUTRE nom** (oto#176). `data_list_datastores`,
la création et le renommage rendaient `id` là où toutes les autres remises rendent
`ns_id` — et la description de `data_rows` prescrit `ns_id` comme « la forme à
employer ». L'agent cherchait donc la clé prescrite dans la seule remise qui ne
l'avait pas : deux confusions d'identifiant en deux jours, dont une demande de
SUPPRESSION visant un tableau étranger à la mission. `id` reste (le dashboard bâtit
`/data/<id>` dessus) ; `ns_id` s'y ajoute — le prix d'un nombre, contre une rupture.

⚠️ Ce qui n'est PAS ici, et qui est voulu : `data_rows(id=…)` ne porte pas `ns_id`.
Son corps EST la ligne, et c'est l'objet même que la plateforme invite à relire puis à
republier tel quel (promotion de `_id`, #354/#390) : une clé de réponse posée dedans
reviendrait en écriture et y ferait une colonne fantôme. Le dernier banc fige ce choix
pour qu'il ne se perde pas en « oubli ».
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.datastore import claim as CAP_CLAIM
from oto_mcp.capabilities.datastore import rows as CAP_ROWS
from oto_mcp.capabilities.datastore import schema as CAP_SCHEMA
from oto_mcp.datastore import identite


# Le tableau du banc : un numéro, et un nom qui n'est PAS la chaîne adressée.
NS_ID = 174
NOM = "edition-vivier"
RELEVE = {"ns_id": NS_ID, "datastore": NOM}
ROW = {"_id": "r1", "nom": "ACME"}


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import datastore as D
    m = FastMCP("t")
    D.register(m)
    return asyncio.run(m.get_tool(name))


class _Store:
    """Un store qui a RÉSOLU le tableau — c'est tout ce dont la remise a besoin.

    `dernier_tableau` est le relevé que `DatastorePg._resolve` pose dans la ligne
    `user_datastores` qu'il vient de lire : le numéro et le nom canonique, sans une
    requête de plus.
    """

    dernier_tableau = RELEVE

    def __init__(self, **verdicts):
        self.v = verdicts
        self.vu: dict = {}

    # --- file de travail
    def claim_next(self, datastore, **k):
        self.vu = {"datastore": datastore, **k}
        return self.v.get("claim_next", ROW)

    def claim_row(self, datastore, row_id, **k):
        self.vu = {"datastore": datastore, "row_id": row_id, **k}
        return ROW

    def release_claim(self, datastore, row_id, **k):
        self.vu = {"datastore": datastore, "row_id": row_id, **k}
        return {"released": True, "reason": None, "lease": None}

    # --- écriture
    def append_row(self, datastore, row, **k):
        self.vu = {"datastore": datastore}
        return dict(ROW)

    def update_row(self, datastore, row_id, patch, **k):
        self.vu = {"datastore": datastore, "row_id": row_id}
        return dict(ROW)

    def write_rows(self, datastore, rows, **k):
        self.vu = {"datastore": datastore}
        return {"inserted": len(rows), "updated": 0, "count": len(rows),
                "key": None, "ids": ["r1"]}

    def off_schema_report(self):
        return {}

    off_forced: list = []

    # --- lecture
    def cursor_rows(self, datastore, **k):
        self.vu = {"datastore": datastore}
        return {"rows": [dict(ROW)], "next_cursor": None}

    def page_rows(self, datastore, **k):
        self.vu = {"datastore": datastore}
        return {"rows": [dict(ROW)], "total": 1, "offset": 0, "limit": 50}

    def count_rows(self, datastore, **k):
        return 7

    def get_row(self, datastore, row_id, **k):
        return dict(ROW)

    def get_schema(self, datastore):
        return {"fields": []}

    def schema_servi_et_masquees(self, ns):
        # La lecture SERVIE de la capacité (oto#94) : rien de masqué ici.
        return self.get_schema(ns), 0


@pytest.fixture()
def store(monkeypatch):
    s = _Store()
    from oto_mcp.tools import datastore as D
    monkeypatch.setattr(D, "_acting_store", lambda: s)
    monkeypatch.setattr(D, "_project_hint", lambda ns: None)
    # Un claim d'agent se fait dans un run (#727) — ce banc n'est pas sur ce point.
    monkeypatch.setattr(D, "_current_run", lambda: "run-numero")
    monkeypatch.setattr(CAP_CLAIM, "make_store", lambda sub: s)
    monkeypatch.setattr(CAP_ROWS, "make_store", lambda sub: s)
    monkeypatch.setattr(CAP_SCHEMA, "make_store", lambda sub: s)
    monkeypatch.setattr(CAP_CLAIM.datastore_journal, "record", lambda *a, **k: None)
    monkeypatch.setattr(CAP_ROWS.datastore_journal, "record", lambda *a, **k: None)
    return s


# ── ① Le numéro part avec la remise ──────────────────────────────────────────

def test_la_reservation_rend_le_numero(store):
    """LA remise qui compte : c'est d'ici que part la boucle d'un agent de flotte."""
    out = _tool("data_claim_next").fn(datastore=NOM, worker="w1")
    assert out["ns_id"] == NS_ID, (
        "la réservation ne rend pas le numéro du tableau — l'agent qui vit dans "
        f"cette boucle n'a alors que le nom pour adresser : {out}")
    assert out["row"] == ROW


def test_les_autres_remises_rendent_le_numero_aussi(store):
    """Écriture, libération, page, schéma — le nom de clé est le MÊME partout.

    Une clé qui change de nom d'un geste à l'autre ne s'apprend pas : l'agent la
    relit à chaque appel, et deux orthographes valent zéro."""
    D = _tool
    remises = {
        "data_write":       D("data_write").fn(datastore=NOM, row={"nom": "A"}),
        "data_write(lot)":  D("data_write").fn(datastore=NOM, rows=[{"nom": "A"}]),
        "data_release":     D("data_release").fn(datastore=NOM, id="r1", worker="w1"),
        "data_rows":        D("data_rows").fn(datastore=NOM),
        "data_rows(count)": D("data_rows").fn(datastore=NOM, count_only=True),
        "data_get_schema":  CAP_SCHEMA._get_schema(
            ResolvedCtx(sub="u-1"), CAP_SCHEMA.GetSchemaInput(datastore=NOM)),
    }
    manquantes = {g: o for g, o in remises.items()
                  if o.get(identite.CLE) != NS_ID}
    assert not manquantes, f"remises sans `{identite.CLE}` : {manquantes}"


def test_la_face_REST_rend_le_meme_numero_sous_le_meme_nom(store):
    """Les deux faces ont déjà divergé en silence sur le datastore : on l'éprouve."""
    ctx = ResolvedCtx(sub="u-1")
    remises = {
        "claim_next": CAP_CLAIM._claim_next(
            ctx, CAP_CLAIM.ClaimNextInput(datastore=NOM, worker="w1")),
        "claim_row": CAP_CLAIM._claim_row(
            ctx, CAP_CLAIM.ClaimRowInput(datastore=NOM, row_id="r1", worker="w1")),
        "append_row": CAP_ROWS._append_row(
            ctx, CAP_ROWS.AppendRowInput(datastore=NOM, row={"nom": "A"})),
        "update_row": CAP_ROWS._update_row(
            ctx, CAP_ROWS.UpdateRowInput(datastore=NOM, row_id="r1", patch={"nom": "A"})),
        "release": CAP_ROWS._release_claim(
            ctx, CAP_ROWS.ReleaseInput(datastore=NOM, row_id="r1", worker="w1")),
        "list_rows": CAP_ROWS._list_rows(
            ctx, CAP_ROWS.ListRowsInput(datastore=NOM)),
    }
    manquantes = {g: o for g, o in remises.items() if o.get(identite.CLE) != NS_ID}
    assert not manquantes, f"remises REST sans `{identite.CLE}` : {manquantes}"


def test_le_numero_est_declare_au_contrat_pas_seulement_tolere():
    """Une intégration qui génère son client depuis l'OpenAPI doit VOIR la clé.

    `extra="allow"` la laisserait passer sans jamais la publier : elle existerait
    pour qui la connaît déjà, c'est-à-dire pour personne."""
    modeles = {
        "ClaimResult": CAP_CLAIM.ClaimResult,
        "WrittenRow": CAP_ROWS.WrittenRow,
        "RowPage": CAP_ROWS.RowPage,
        "ReleasedRow": CAP_ROWS.ReleasedRow,
        "SchemaOut": CAP_SCHEMA.SchemaOut,
    }
    absents = [n for n, m in modeles.items()
               if identite.CLE not in m.model_fields]
    assert not absents, f"`{identite.CLE}` non déclaré au contrat de : {absents}"


# ── ② `datastore` est l'identité, pas l'écho ─────────────────────────────────

@pytest.mark.parametrize("adresse", [NOM, "174", "slot:vivier"])
def test_le_nom_rendu_est_celui_du_tableau_quelle_que_soit_l_adresse(store, adresse,
                                                                    monkeypatch):
    """Adresser `174` répondait `datastore: "600"`-style — la clé répétait la question.

    Un lecteur qui relisait `datastore` pour savoir OÙ il venait d'écrire recevait sa
    propre chaîne : deux appelants sur le même tableau y lisaient deux valeurs."""
    from oto_mcp import access
    monkeypatch.setattr(access, "resolve_datastore_ref", lambda ref: (
        NOM if str(ref).startswith("slot:") else str(ref)))
    out = _tool("data_claim_next").fn(datastore=adresse, worker="w1")
    assert out["datastore"] == NOM, (
        f"adressé par « {adresse} », la réponse rend {out['datastore']!r} au lieu du "
        f"nom du tableau ({NOM!r}) — c'est un écho, pas une identité")
    assert out["ns_id"] == NS_ID


def test_le_lot_a_une_enveloppe_donc_il_rend_les_deux(store):
    """Le LOT n'est pas une ligne : son corps est un récap, il peut donc porter le nom
    canonique en plus du numéro — là où l'écriture d'une ligne seule ne prend que le
    numéro."""
    out = _tool("data_write").fn(datastore="174", rows=[{"nom": "A"}])
    assert (out["datastore"], out["ns_id"]) == (NOM, NS_ID)
    assert out["count"] == 1


def test_lecriture_dune_ligne_seule_ne_prend_PAS_de_cle_datastore(store):
    """`datastore` est un nom de colonne parfaitement plausible : le poser à côté des
    colonnes de l'utilisateur les mettrait en collision. Le numéro, lui, y tient."""
    out = _tool("data_write").fn(datastore=NOM, row={"nom": "A"})
    assert out["ns_id"] == NS_ID
    assert "datastore" not in out, f"clé `datastore` dans le corps d'une ligne : {out}"


def test_le_schema_rend_lidentite_et_pas_ladresse(store):
    out = CAP_SCHEMA._get_schema(ResolvedCtx(sub="u-1"),
                                 CAP_SCHEMA.GetSchemaInput(datastore="174"))
    assert (out["datastore"], out["ns_id"]) == (NOM, NS_ID)


def test_sans_resolution_la_remise_rend_ce_qu_elle_rendait(store):
    """Dégradation NOMMÉE : un chemin qui n'a rien résolu rend l'adresse reçue et
    `ns_id: None`. La présence du numéro est donc la preuve que le tableau a été
    atteint — jamais un `0` ni un nom inventé."""
    # ⚠️ Les DEUX noms, y compris ici. Le repli sert quand rien n'a été relevé —
    # c'est-à-dire exactement le moment où l'appelant cherche à comprendre ce qui a
    # échoué. Lui rendre `undefined` sous l'ancien nom dans ce cas-là serait doubler
    # l'obscurité. La clé s'en va avec les alias de chemin, au 08/11/2026.
    assert identite.de_releve(None, "quoi-que-ce-soit") == {
        "datastore": "quoi-que-ce-soit",
        identite.CLE: None}


# ── Ce qui ne bouge pas ──────────────────────────────────────────────────────

def test_le_nom_reste_une_adresse_valide(store):
    """Le nom est en RETRAIT, pas retiré : un texte qui laisserait croire qu'il ne
    marche plus mentirait. Le banc le dit à la place d'une date qu'on n'a pas."""
    _tool("data_claim_next").fn(datastore=NOM, worker="w1")
    assert store.vu["datastore"] == NOM, (
        "le nom n'atteint plus le store : ce lot n'avait pas à toucher la résolution")


def test_la_ligne_seule_ne_gagne_aucune_cle(store):
    """`data_rows(id=…)` rend la ligne NUE, et doit continuer.

    C'est l'objet que la plateforme invite à relire puis republier tel quel : une clé
    de réponse posée dedans reviendrait en écriture — colonne fantôme sur un tableau
    libre, ligne entière perdue sur un tableau qui refuse l'inconnu."""
    out = _tool("data_rows").fn(datastore=NOM, id="r1")
    assert out == ROW, f"la ligne nue a gagné une clé de réponse : {out}"


# ── ④ Le CATALOGUE rend le nombre sous les deux noms (oto#176) ───────────────
#
# On éprouve le REGISTRE lui-même, pas un faux : c'est lui qui met la remise en
# forme, et le défaut d'oto#176 était précisément qu'une couche au-dessus (le
# modèle `DatastoreEntry`) PROMETTAIT la clé que le registre ne posait pas.

CATALOGUE_NS = 174


@pytest.fixture()
def registre(monkeypatch):
    """Un store réel dont seuls les seams de base sont simulés."""
    from oto_mcp import access, group_store, links, ownership, roles
    from oto_mcp.datastore import core as D

    ligne = {"id": CATALOGUE_NS, "datastore": NOM, "owner_type": "user",
             "owner_id": "u-1", "created_at": "2026-09-01", "schema": None}
    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(links, "patron_reclame", lambda *a, **k: False)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: False)
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])
    monkeypatch.setattr(D.db, "list_datastores_for_owners", lambda owners: [ligne])
    monkeypatch.setattr(D.db, "list_datastores_granted_to",
                        lambda sub, orgs, groups: [])
    # L'org 99 est l'org PERSO de u-1 : le tableau perso s'y liste et une création
    # sans propriétaire y reste à la personne (décision du 28/09/2026).
    monkeypatch.setattr(ownership.org_store, "home_org", lambda sub: 99)
    monkeypatch.setattr(D.db, "list_datastores_shared_to_user", lambda sub: [])
    monkeypatch.setattr(D.ownership, "can_govern", lambda sub, t, rid: True)
    monkeypatch.setattr(ownership, "can_govern", lambda sub, t, rid: True)
    monkeypatch.setattr(ownership, "can_govern_many",
                        lambda sub, t, owners: {rid: True for rid in owners})
    monkeypatch.setattr(D.db, "create_datastore",
                        lambda ot, oid, nom, *, context_org_id: CATALOGUE_NS)
    monkeypatch.setattr(D.db, "rename_datastore_by_id", lambda ns, nom: None)
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda *a, **k: {"id": CATALOGUE_NS, "datastore": NOM,
                                         "owner_type": "user", "owner_id": "u-1",
                                         "schema": None})
    return D.make_store("u-1")


def test_le_catalogue_rend_le_numero_sous_LES_DEUX_noms(registre):
    """Lister, créer, renommer — les trois remises du registre, la même clé.

    `id` doit RESTER : le dashboard construit `/data/<id>` dessus. C'est un ajout,
    pas un renommage, et ce banc fige les deux moitiés."""
    remises = {
        "list": registre.list_datastores()[0],
        "create": registre.create_datastore("edition-vivier-2"),
        "rename": registre.rename_datastore(NOM, "edition-vivier-3"),
    }
    sans_numero = {g: o for g, o in remises.items()
                   if o.get(identite.CLE) != CATALOGUE_NS}
    assert not sans_numero, (
        f"remises du catalogue sans `{identite.CLE}` — l'agent qui suit la "
        f"description de `data_rows` cherche cette clé-là : {sans_numero}")
    sans_id = {g: o for g, o in remises.items() if o.get("id") != CATALOGUE_NS}
    assert not sans_id, (
        f"`id` a disparu : le dashboard bâtit `/data/<id>` dessus, c'est un "
        f"AJOUT qui était demandé, pas un renommage : {sans_id}")


def test_le_catalogue_declare_le_numero_au_contrat(registre):
    """Le défaut d'oto#176 en creux : `DatastoreEntry` déclarait `ns_id` depuis le
    07/09/2026 et rien ne le servait — un `Output` DÉCRIT, il ne sérialise pas. La
    déclaration ne prouve donc rien seule ; c'est le couple qui prouve."""
    from oto_mcp.capabilities.datastore import datastores as CAP

    modeles = {"DatastoreEntry": CAP.DatastoreEntry,
               "CreatedDatastore": CAP.CreatedDatastore,
               "RenamedDatastore": CAP.RenamedDatastore}
    absents = [n for n, m in modeles.items() if identite.CLE not in m.model_fields]
    assert not absents, f"`{identite.CLE}` non déclaré au contrat de : {absents}"
    # Et le contrat ne ment pas : la remise réelle porte la clé déclarée.
    assert identite.CLE in registre.list_datastores()[0]


def test_la_face_REST_du_renommage_rend_le_numero_aussi(registre, monkeypatch):
    """Le renommage REST ne passe PAS par le store : il a son propre handler, et
    c'est exactement la forme sous laquelle les deux faces ont déjà divergé."""
    from oto_mcp.capabilities.datastore import datastores as CAP

    monkeypatch.setattr(CAP, "govern_ns", lambda sub, ns: CATALOGUE_NS)
    monkeypatch.setattr(CAP.db, "rename_datastore_by_id", lambda ns, nom: None)
    out = CAP._rename_datastore(ResolvedCtx(sub="u-1"),
                                CAP.RenameDatastoreInput(datastore=NOM, name="neuf"))
    assert out[identite.CLE] == CATALOGUE_NS and out["datastore"] == "neuf"


def test_le_texte_servi_du_catalogue_NOMME_la_cle(registre):
    """Une clé qu'aucun texte n'annonce n'existe que pour qui la connaît déjà — et
    c'est justement ce que le modèle REST a fait pendant dix jours."""
    doc = _tool("data_list_datastores").fn.__doc__ or ""
    assert identite.CLE in doc, (
        "la description servie de `data_list_datastores` ne nomme pas la clé "
        "qu'elle rend désormais")
