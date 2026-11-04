"""Quel droit ouvre l'éditeur du readme d'un périmètre — la question posée le 31/08.

Le README injecté d'une org ou d'une équipe s'écrit sur la surface guide
(`PUT …/guides/{scope}/{slug}`). Le dashboard produit demandait s'il relève de
`can_write_instructions` ou de `can_edit`. **Mesuré sur la garde, pas sur ce qui serait
cohérent** : c'est `can_edit`.

⚠️ **Et le piège est réel, pas théorique.** Depuis #695, `can_write_instructions` est
vrai pour **tout membre** d'une équipe — c'est le droit d'écrire une PROCÉDURE, geste
réversible et versionné. Le readme, lui, exige le **chef d'équipe**. Un écran qui
conditionne son éditeur de readme sur `can_write_instructions` affiche donc un bouton
que le serveur refusera par 403, à tout membre simple. Les deux champs se ressemblent,
portent tous deux sur de la prose, et ne gardent pas la même chose.

Ce fichier tient trois choses :

1. la garde réelle du readme, scope par scope — c'est elle la source, tout le reste en
   découle ;
2. le fait que le contrat servi la DISE, et nomme le bon champ ;
3. que les deux droits restent DISTINCTS au palier équipe — le jour où l'un rejoindrait
   l'autre, cette documentation deviendrait fausse en silence.

Éprouvé rouge le 2026-09-01 : mention retirée de la description ⟹ deuxième test.
"""
from __future__ import annotations

import inspect

import pytest

from oto_mcp.capabilities import guides
from oto_mcp.capabilities._types import AuthzDenied
from oto_mcp.capabilities.registry import CAPABILITIES


def _capacite(cle: str):
    trouvees = [c for c in CAPABILITIES if c.key == cle]
    assert trouvees, f"capacité {cle} introuvable"
    return trouvees[0]


@pytest.mark.parametrize("scope, garde", [
    ("org", "is_org_admin"),
    ("group", "can_admin_group"),
    ("platform", "is_platform_admin"),
    ("tenant", "is_platform_admin"),
])
def test_la_garde_du_readme_est_celle_de_l_ADMINISTRATION(scope, garde):
    """La source de vérité : ce que le serveur exécute. `user` n'y figure pas — il n'y
    a pas de garde à porter, on n'écrit jamais pour un autre."""
    source = inspect.getsource(guides._owner_for_write)
    bloc = source.split(f'scope == "{scope}"')[1]
    assert garde in bloc.split("if scope ==")[0], (
        f"le scope {scope} ne passe plus par `{garde}`")


def test_le_contrat_nomme_le_bon_champ_et_ecarte_le_mauvais():
    """Un client ne peut pas déduire la garde : elle vit dans le serveur. Le contrat
    doit donc la dire, ET écarter nommément le champ voisin qui se lit à sa place."""
    description = _capacite("me.guides.set").description or ""
    assert "can_edit" in description, "le contrat ne nomme pas le droit à lire"
    assert "can_write_instructions" in description, (
        "le contrat n'écarte pas le champ voisin, celui qu'un écran lit par erreur")


def test_le_scope_tenant_nest_plus_tu():
    """Il est accepté par la garde depuis toujours et manquait à l'énumération servie :
    un client qui lit le contrat croit à quatre scopes, le serveur en accepte cinq."""
    assert "tenant" in (_capacite("me.guides.set").description or "")


def test_les_deux_droits_restent_distincts_au_palier_equipe():
    """Le contre-test qui garde la phrase honnête. Si l'écriture d'une procédure
    d'équipe remontait un jour au chef, la mise en garde ci-dessus n'aurait plus
    d'objet — et une mise en garde périmée envoie chercher un défaut qui n'existe
    plus."""
    from oto_mcp.capabilities.groups import guide as groupe_guide

    assert (groupe_guide._DROITS_SERVIS["can_write_instructions"]
            == "group.instruction.set")
    # L'écriture d'une procédure s'autorise sur l'APPARTENANCE, au seuil de la
    # capacité : tout membre passe.
    ecriture = _capacite("group.instruction.set")
    assert "GROUP_MEMBER_OF" in repr(ecriture.authz), repr(ecriture.authz)
    # Le readme s'autorise sur l'ADMINISTRATION, en aval, dans la garde par scope.
    assert "can_admin_group" in inspect.getsource(guides._owner_for_write)


def test_la_garde_S_EXECUTE_sur_le_chemin_du_readme(monkeypatch):
    """Le test décisif, et il regarde ailleurs que les trois précédents.

    Ceux-ci lisent la SOURCE de `_owner_for_write`. Ils resteraient verts le jour où
    l'écriture d'un readme cesserait d'y passer — c'est exactement le défaut qu'on
    venait de mesurer sur le journal : une seconde voie, sous une autre règle, que le
    banc ne voit pas parce qu'il regarde la première. Le readme (`delivery='init'`)
    n'appelle d'ailleurs pas `_owner_for_write` en direct : il passe par `_init_ref`.
    On exécute donc le geste servi, membre puis chef.

    Éprouvé rouge le 2026-09-01 : `_init_ref` branché sur le résolveur de LECTURE ⟹ le
    membre écrit le readme de son équipe, et les trois tests ci-dessus restent verts.
    """
    from oto_mcp import roles

    GID, CHEF, MEMBRE = 42, "u-chef", "u-membre"
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: sub == CHEF)
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: sub in (CHEF, MEMBRE))
    ecrits: dict = {}

    def _pose(scope, ident, body, *, expected_rev=None, **_):
        ecrits[(scope, ident)] = body
        return {"body_md": body, "updated_at": "2026-09-01T00:00:00Z"}

    monkeypatch.setattr(guides.guide_store, "set_init_guide", _pose)

    class Ctx:
        def __init__(self, sub): self.sub = sub

    def _ecrire(sub):
        return guides._set(Ctx(sub), guides.GuideSetInput(
            scope="group", delivery="init", owner_id=str(GID), body_md="# readme"))

    with pytest.raises(AuthzDenied) as refus:
        _ecrire(MEMBRE)
    assert refus.value.status == 403, refus.value
    assert not ecrits, "un membre simple a écrit le readme de son équipe"

    assert _ecrire(CHEF)["delivery"] == "init"
    assert ecrits, "le chef d'équipe ne peut plus écrire le readme"
