"""Cliquet : `ConnectorInstance.via` nomme EXACTEMENT ce que le module produit.

`via` était un `str` nu jusqu'au 2026-09-01 — un front qui l'affiche devait deviner le
jeu de valeurs en l'observant, et le docstring qui prétendait l'énumérer en oubliait
une (`tenant_key`, posée depuis les clés de tenant). Le contrat le déclare désormais
en `Literal`, ce qui pose un risque connu et documenté ailleurs dans le même dépôt :
un énuméré au contrat fait échouer la génération de client d'un tiers le jour où le
serveur rend une valeur de plus (c'est l'argument qui garde `AuthDescriptor.method` en
`str`).

**Ce fichier est ce qui rend l'énuméré tenable.** Il lit l'AST du module producteur et
compare, dans les deux sens, les littéraux réellement posés au jeu déclaré. Ajouter un
sixième `via` sans toucher au modèle fait rougir la CI ici, au lieu de casser un
client généré, en silence, chez quelqu'un d'autre.

⚠️ Pourquoi l'AST et pas un appel : produire les cinq `via` demanderait cinq montages
(un coffre, un tenant, deux grants, une clé ouverte). Un banc
qui ne monterait que les cas faciles certifierait une couverture qu'il n'a pas — c'est
le mode d'échec qu'on veut éviter, pas celui qu'on veut reproduire.

Éprouvé rouge le 2026-09-01 avant d'être posé : `tenant_key` retiré du `Literal` ⟹
échec nommant `tenant_key`.
"""
from __future__ import annotations

import ast
import pathlib
import typing

import pytest

from oto_mcp.capabilities.connectors.instances import ConnectorInstance

MODULE = (pathlib.Path(__file__).resolve().parents[1]
          / "oto_mcp" / "capabilities" / "connectors" / "instances.py")

# La fabrique des instances plateforme prend son `via` en 3e position — les paliers
# (`user_grant`, `org_grant`, `free_tier`) ne sont donc écrits nulle part ailleurs.
FABRIQUE = "_platform_instance"
RANG_VIA = 2


def _poses() -> set[str]:
    """Toute chaîne littérale qui devient un `via` dans le module producteur.

    Trois formes, et les trois existent : la clé d'un dict construit d'un bloc, une
    affectation par indice sur une instance déjà bâtie, et l'argument de la fabrique
    des instances plateforme."""
    arbre = ast.parse(MODULE.read_text(encoding="utf-8"))
    trouvees: set[str] = set()
    for noeud in ast.walk(arbre):
        # 1. {"via": "credential", ...}
        if isinstance(noeud, ast.Dict):
            for cle, valeur in zip(noeud.keys, noeud.values):
                if (isinstance(cle, ast.Constant) and cle.value == "via"
                        and isinstance(valeur, ast.Constant)
                        and isinstance(valeur.value, str)):
                    trouvees.add(valeur.value)
        # 2. inst["via"] = "tenant_key"
        elif isinstance(noeud, ast.Assign) and isinstance(noeud.value, ast.Constant):
            for cible in noeud.targets:
                if (isinstance(cible, ast.Subscript)
                        and isinstance(cible.slice, ast.Constant)
                        and cible.slice.value == "via"
                        and isinstance(noeud.value.value, str)):
                    trouvees.add(noeud.value.value)
        # 3. _platform_instance(provider, label, "user_grant", {...})
        elif isinstance(noeud, ast.Call):
            nom = getattr(noeud.func, "id", None) or getattr(noeud.func, "attr", None)
            if nom == FABRIQUE and len(noeud.args) > RANG_VIA:
                arg = noeud.args[RANG_VIA]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    trouvees.add(arg.value)
    return trouvees


def _declarees() -> set[str]:
    return set(typing.get_args(ConnectorInstance.model_fields["via"].annotation))


def test_le_banc_voit_bien_les_trois_formes_de_pose():
    """Témoin. Un collecteur muet (une forme d'AST qui change, un renommage de la
    fabrique) rendrait les deux tests suivants verts sans avoir rien lu."""
    poses = _poses()
    assert len(poses) >= 5, sorted(poses)
    assert "credential" in poses          # forme 1 : dict d'un bloc
    assert "tenant_key" in poses          # forme 2 : affectation par indice
    assert "user_grant" in poses          # forme 3 : argument de la fabrique


def test_toute_valeur_posee_est_declaree():
    """Le sens qui protège le client : une valeur produite et non déclarée tombe dans
    la branche `default` d'un `switch`, ou fait échouer un client généré."""
    manquantes = _poses() - _declarees()
    assert not manquantes, (
        f"posées par le module mais absentes du Literal : {sorted(manquantes)}")


def test_aucune_valeur_declaree_n_a_disparu_du_code():
    """L'autre sens : une valeur au contrat que plus rien ne produit est une branche
    morte chez tous ceux qui l'ont implémentée — le défaut exact du docstring qui
    énumérait six valeurs pour sept."""
    fantomes = _declarees() - _poses()
    assert not fantomes, (
        f"déclarées mais plus produites : {sorted(fantomes)}")


@pytest.mark.parametrize("valeur", ["shared_with_me", "personal_cross_org"])
def test_le_compte_d_autrui_et_la_cle_d_une_autre_org_ne_sont_plus_des_provenances(valeur):
    """La bascule des comptes (#1499) retire le prêt nominatif et la clé personnelle
    suivie d'une org à l'autre : le compte d'une autre personne ne se liste plus (il se
    NOMME, au niveau que ses règles donnent), et la clé d'une autre org n'est plus vue
    depuis celle-ci."""
    assert valeur not in _declarees()


def test_un_dict_d_appoint_ne_peut_pas_glisser_un_huitieme_via():
    """La faille que l'AST ne voit pas : `_platform_instance` fusionne un dict
    d'appoint (`daily_quota`, `set_at`). Tant qu'il était fusionné APRÈS `via`, un
    appelant pouvait y poser une valeur hors énuméré — et un `Literal` faux est pire
    qu'un `str` honnête : il fait échouer la génération de client sur une valeur que
    le serveur a réellement produite, sans que rien n'ait rougi ici."""
    from oto_mcp.capabilities.connectors import instances

    inst = instances._platform_instance(
        "acme", "clé", "free_tier", {"set_at": "2026-09-01", "via": "inventé"})

    assert inst["via"] == "free_tier"
