"""`POST /api/resources` : la 200 déclarée, le discriminant, et les deux pièges d'entrée.

Un front tiers, consommateur pur du REST, a signalé le 2026-09-01 (#659) que cette
route répondait `200 OK` **sans schéma** : rien à extraire, rien à deviner. Trois
familles de ressource y passent, et elles ne rendent pas les mêmes clés — d'où une
union DISCRIMINÉE sur `resource_type` (`capabilities/resources_contract.py`) plutôt
qu'une union plate, qui déclarerait `row_count` sur un projet.

Ce qui est gardé ici, dans l'ordre de ce qu'une erreur coûterait :

1. **le contrat ne ment pas** — chaque modèle est confronté aux clés que le
   `_enrich_*` PRODUIT vraiment, et `PublishedProject` aux clés de
   `projects._view()`. `Capability.Output` décrit sans valider : une déclaration
   fausse ne se voit donc JAMAIS à l'exécution, seulement chez l'intégrateur qui
   s'y est branché. C'est la seule couche qui puisse l'attraper ;
2. **le contrat et le dispatch ne divergent pas** — `ResourceType` (publié) contre
   les clés de `_OPS` (servi) ;
3. **les deux pièges d'entrée** que #659 a fait tomber avec le schéma : le défaut
   implicite de `resource_type`, et l'identifiant non numérique qui sortait en 500.
"""
from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from oto_mcp import openapi, ownership
from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities import resources_contract as C
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=None)

_NS_ROW = {"id": 3, "namespace": "clients", "owner_type": "user", "owner_id": "u1",
           "created_at": "2026-06-01 10:00:00"}
_PROJ_ROW = {"id": 7, "name": "Proj", "owner_type": "user", "owner_id": "u1",
             "archived_at": None, "created_at": "2026-06-30 09:00:00"}
_GUIDE_ROW = {"id": 11, "slug": "onboarding", "title": "Onboarding",
              "owner_type": "group", "owner_id": "4", "version": 2,
              "updated_at": "2026-08-31 18:00:00"}


@pytest.fixture
def enrichable(monkeypatch):
    """Ce qu'il faut pour appeler les `_enrich_*` sans base : le compte des lignes
    d'un tableau et la résolution d'un libellé de propriétaire."""
    monkeypatch.setattr(R.db, "count_datastore_rows_for_ns", lambda i: 42)
    monkeypatch.setattr(R.db, "get_user", lambda sub: {"email": "u1@x.co"})
    monkeypatch.setattr(R.group_store, "get_group", lambda gid: {"name": "pôle data"})


# ── 1. Le contrat ne ment pas ────────────────────────────────────────────────

@pytest.mark.parametrize("enrich, row, modele", [
    (R._enrich_datastore, _NS_ROW, C.DatastoreResource),
    (R._enrich_project, _PROJ_ROW, C.ProjectResource),
    (R._enrich_guide, _GUIDE_ROW, C.GuideResource),
])
def test_chaque_modele_decrit_exactement_ce_que_son_enrich_produit(
        enrich, row, modele, enrichable):
    """Égalité STRICTE, dans les deux sens, et les deux sens comptent.

    Une clé produite et non déclarée, c'est de la donnée servie qu'aucun client
    généré ne saura lire. Une clé déclarée et non produite, c'est une promesse que
    le serveur ne tient pas — et celle-là est pire : l'intégrateur écrit du code qui
    la lit, et découvre `undefined` en production.
    """
    assert set(enrich(row)) == set(modele.model_fields)


@pytest.mark.parametrize("detail, base", [
    (C.DatastoreResourceDetail, C.DatastoreResource),
    (C.ProjectResourceDetail, C.ProjectResource),
    (C.GuideResourceDetail, C.GuideResource),
])
def test_la_fiche_est_la_liste_plus_les_beneficiaires(detail, base):
    """`op=get` = `op=list` + `grants`. Rien d'autre ne s'ajoute au passage — c'est
    ce que le handler fait (`out = enrich(row)` puis `out["grants"] = …`)."""
    assert set(detail.model_fields) - set(base.model_fields) == {"grants"}


def test_publication_declare_la_vue_projet_sans_deriver(monkeypatch):
    """`op=share` en audience public/secret/private rend la vue PROJET, qui vit dans
    un AUTRE module (`projects._view`). La recopie dans `PublishedProject` est le
    prix du cycle d'import ; ce test en est la contrepartie — un champ ajouté à
    `_view()` et pas au modèle fait rougir ici, au lieu de faire mentir le document
    en silence.

    Les trois clés en plus sont les ajouts CONDITIONNELS de `publish_project_mcp`
    (elles ne sont posées que quand il y a quelque chose à dire) : elles sont
    déclarées facultatives, donc absentes de `_view()` par construction.
    """
    monkeypatch.setattr("oto_mcp.links.link_for", lambda *a, **k: None)
    servi = set(P._view(_PROJ_ROW, "u1"))
    declare = set(C.PublishedProject.model_fields)
    conditionnels = {"logto_resource_registered", "mcp_unresolvable_tools", "warnings"}
    assert servi - declare == set(), f"clés servies non déclarées : {servi - declare}"
    assert declare - servi == conditionnels, (
        f"clés déclarées que `_view()` ne rend pas : {declare - servi - conditionnels}")


def test_l_union_couvre_les_cinq_verbes():
    """Une forme oubliée dans l'union = une réponse servie qu'aucun client généré ne
    sait ranger. Les six branches pour cinq verbes : `op=share` en a DEUX (grant vs
    publication), et c'est précisément ce qui rendait l'enveloppe commune vide."""
    branches = set(get_args(C.ResourceOut.model_fields["root"].annotation))
    attendues = {C.ResourceList, C.ResourceTransferred, C.ResourceShared,
                 C.ResourceUnshared, C.PublishedProject}
    assert attendues <= branches
    # La 6e branche est l'union discriminée d'`op=get` (un Annotated, pas une classe).
    assert len(branches) == 6


# ── 2. Contrat et dispatch ne divergent pas ──────────────────────────────────

def test_l_enumere_publie_est_exactement_le_dispatch():
    """`ResourceType` est ce que le document promet ; `_OPS` est ce que le handler
    sait router. Les faire diverger donne l'un des deux mensonges : une famille
    annoncée qui lève `KeyError` (500), ou une famille routée qu'aucun contrat ne
    nomme — et le `Literal` de l'entrée la refuserait avant le handler."""
    assert set(get_args(C.ResourceType)) == set(R._OPS)


def test_les_trois_familles_ont_leur_forme_discriminee():
    """Le discriminant doit couvrir le même ensemble, sinon l'union laisse une
    famille sans forme déclarée."""
    formes = get_args(get_args(C.GovernedResource)[0])
    portes = {m.model_fields["resource_type"].annotation for m in formes}
    assert {get_args(p)[0] for p in portes} == set(get_args(C.ResourceType))


def test_la_200_porte_le_discriminant_dans_le_document():
    """Le garde-fou ne vaut que si la déclaration ATTEINT le document servi — sinon
    on collectionne des modèles décoratifs (même raison que
    `test_declared_output_reaches_the_openapi_document`)."""
    doc = openapi.build()
    op = doc["paths"]["/api/resources"]["post"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]

    # 1. `op=get` — branche directe de l'`anyOf`.
    discrimines = [b for b in schema["anyOf"] if "discriminator" in b]
    assert len(discrimines) == 1, "l'union d'`op=get` n'est plus discriminée"
    fiche = discrimines[0]["discriminator"]
    assert fiche["propertyName"] == "resource_type"
    assert set(fiche["mapping"]) == set(get_args(C.ResourceType))

    # 2. Les ENTRÉES d'`op=list`, un cran plus bas — l'endroit qu'on oublie, et
    #    celui qui déclarerait `row_count` sur un projet si on l'aplatissait. Un
    #    contrôle qui ne regarde que le premier niveau laisserait passer ça.
    items = doc["components"]["schemas"]["ResourceList"]["properties"]["resources"]["items"]
    assert set(items["discriminator"]["mapping"]) == set(get_args(C.ResourceType))

    assert "ResourceGrant" in doc["components"]["schemas"]


# ── 3. Les deux pièges d'entrée ──────────────────────────────────────────────

# ⚠️ Les deux pièges d'entrée sont corrigés sur la surface STRICTE, pas ici. #756 les
# avait corrigés sur la surface héritée : les tests d'alors passaient au vert sur une
# rupture (mesurée au journal des appels, revert #774), parce qu'ils affirmaient
# l'intention du lot au lieu de décrire ce qu'un appelant reçoit. Le comportement des
# DEUX surfaces, et le cliquet qui fige l'héritée, vivent dans
# `tests/test_resources_deux_surfaces.py`.


def test_le_motif_est_publie_dans_le_contrat_de_la_surface_stricte():
    """Le front dérive sa garde de saisie du contrat (même raison que la borne du
    corps d'un guide) : la contrainte doit être LISIBLE, pas seulement appliquée.

    Publiée sur `/api/resources/v2` — la surface héritée, elle, n'a rien resserré, et
    c'est ce qui lui permet de continuer à servir les appelants d'aujourd'hui.
    """
    op = openapi.build()["paths"]["/api/resources/v2"]["post"]
    champ = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert any(v.get("pattern") == r"^\d+$"
               for v in champ["resource_id"]["anyOf"])
    assert champ["resource_type"]["enum"] == list(get_args(C.ResourceType))
    assert "resource_type" in \
        op["requestBody"]["content"]["application/json"]["schema"]["required"]


def test_la_surface_heritee_ne_publie_AUCUNE_de_ces_deux_contraintes():
    """Le contre-test qui donne son sens au précédent. Sans lui, publier le motif des
    deux côtés passerait — et ce serait exactement la rupture qu'on vient d'annuler."""
    op = openapi.build()["paths"]["/api/resources"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    champ = schema["properties"]
    assert not any(v.get("pattern") for v in champ["resource_id"]["anyOf"])
    assert "enum" not in champ["resource_type"]
    assert champ["resource_type"]["default"] == "datastore_namespace"
    assert "resource_type" not in schema.get("required", [])


# ── 4. Les refus déclarés se rejouent ────────────────────────────────────────
#
# `DeclaredError` DÉCRIT, il ne fait rien : une déclaration sans rejeu est
# décorative, ce qui est pire qu'une absence (le document promet un refus que le
# serveur ne rend pas). Les six autres refus déclarés sont déjà rejoués ailleurs —
# `confirm_loss_of_control`, `not_org_member`, `publication_unsupported` dans
# `test_resources_project.py`, `group_not_visible` et `unknown_group` dans
# `test_resources_group_share.py`, `unknown_org` dans `test_project_delivery.py`.

def _wire(monkeypatch):
    monkeypatch.setattr(R.access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(R.ownership, "accessor_scope",
                        lambda sub: ownership.AccessorScope(sub, [], []))
    monkeypatch.setattr(R.roles, "is_platform_admin", lambda sub: False)
    monkeypatch.setattr(R.ownership, "can_transfer", lambda sub, rt, rid: True)
    monkeypatch.setattr(R.ownership, "would_retain_control", lambda *a: True)


def _get(op: str, **kw):
    return R.ResourceInput(op=op, resource_type="project", resource_id="7", **kw)


def test_refus_forbidden_le_gerant_ne_cede_pas_la_propriete(monkeypatch):
    """`RESOURCE_GOVERN` laisse passer un GÉRANT (il gouverne) ; le handler re-garde
    la structure, parce que la cession de propriété, elle, lui est fermée."""
    _wire(monkeypatch)
    monkeypatch.setattr(R.ownership, "can_transfer", lambda sub, rt, rid: False)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, _get("transfer", new_owner_email="u2@x.co"))
    assert (e.value.status, e.value.code) == (403, "forbidden")


def test_refus_unknown_user(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_users_by_email", lambda e: [])
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, _get("share", email="fantome@x.co"))
    assert (e.value.status, e.value.code) == (404, "unknown_user")


def test_refus_email_required_quand_aucun_principal(monkeypatch):
    """`share` sans `email`, ni `org_id`, ni `group_id` : il n'y a personne à qui
    donner l'accès."""
    _wire(monkeypatch)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, _get("share"))
    assert (e.value.status, e.value.code) == (400, "email_required")


def test_refus_not_group_member(monkeypatch):
    """Transférer VERS une équipe exige d'en être : on n'envoie pas une ressource
    dans un scope où l'on n'est pas."""
    _wire(monkeypatch)
    monkeypatch.setattr(R.roles, "can_read_group", lambda sub, gid: False)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, _get("transfer", new_owner_group=5))
    assert (e.value.status, e.value.code) == (403, "not_group_member")


def test_refus_transfer_failed(monkeypatch):
    """Le store refuse la re-parentalisation : `ValueError` traduite en 409, et non
    laissée remonter — sinon c'est un 500."""
    _wire(monkeypatch)
    monkeypatch.setattr(R.db, "get_users_by_email", lambda e: [{"sub": "u2", "email": e}])

    def _boom(*a):
        raise ValueError("owner introuvable")
    monkeypatch.setattr(R.ownership, "transfer", _boom)
    with pytest.raises(AuthzDenied) as e:
        R._resources(CTX, _get("transfer", new_owner_email="u2@x.co"))
    assert (e.value.status, e.value.code) == (409, "transfer_failed")


_REJOUES = {
    "email_required", "publication_unsupported", "forbidden", "group_not_visible",
    "not_group_member", "not_org_member", "unknown_user", "unknown_org",
    "unknown_group", "confirm_loss_of_control", "transfer_failed",
}


def test_chaque_refus_declare_est_rejoue_quelque_part():
    """Inventaire NOMMÉ, pas un plafond tu : si un refus est ajouté à `errors=` sans
    rejeu, la liste ci-dessous le dit — c'est le seul endroit qui relie les deux.

    L'héritée déclare UN refus de plus, `unsupported_resource_type` : son
    `resource_type` est un `str` libre, donc la famille inconnue atteint le handler.
    Il est rejoué dans `tests/test_resources_project.py::test_unknown_type`.
    """
    cap = next(c for c in R.CAPABILITIES if c.key == "resources.govern")
    assert {e.code for e in cap.errors} == _REJOUES | {"unsupported_resource_type"}


def test_la_surface_stricte_declare_exactement_les_refus_qu_elle_peut_rendre():
    """Elle NE déclare PAS `unsupported_resource_type` : son `Literal` refuse la
    famille inconnue à la validation, donc le handler n'est jamais atteint. Déclarer
    un refus qu'on ne sait pas rejouer ferait promettre au document ce que le serveur
    ne rend pas — un client généré s'y branche."""
    cap = next(c for c in R.CAPABILITIES if c.key == "resources.govern.v2")
    assert {e.code for e in cap.errors} == _REJOUES
