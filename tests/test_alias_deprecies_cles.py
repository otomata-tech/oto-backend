"""Une clé de réponse renommée est servie sous SES DEUX noms (#519, lot B3).

Renommer une clé de réponse sec est la panne la plus chère de tout ce chantier, et
la plus silencieuse : le client lit `null` là où il attendait une valeur — pas
d'erreur, pas de log, rien qui s'allume. Il affiche un readme vide, ou range une
liste vide dans son cache. On l'apprend par un utilisateur, des jours plus tard.

Alors chaque clé est servie deux fois pendant le préavis, et ces tests gardent :

1. **Les deux noms portent la MÊME valeur.** Deux valeurs qui divergent seraient
   pires qu'une clé manquante — un client migré et un client resté en arrière
   liraient deux vérités.
2. **Le doublage est ADDITIF.** Aucune clé d'hier ne disparaît, sur aucune forme de
   réponse. C'est ce que le lot promet, et la seule chose qu'un consommateur peut
   vérifier de l'extérieur.
3. **L'ancien nom est encore accepté EN ENTRÉE** là où il en était un
   (`doctrine_id`, le paramètre `doctrine` de `run_start`) — sinon la clé doublée
   serait un piège : on lit `guide_id` dans la réponse, on le repasse, et ça casse.
4. **Le helper ne touche que ce qu'on lui donne**, et jamais la donnée de
   l'utilisateur. Un passage automatique sur toute réponse inventerait une colonne
   dans le tableau de quelqu'un.
5. **Le code d'erreur d'hier survit dans `details.legacy_code`** — un code ne se
   double pas, il n'y a qu'un champ `error`.
6. **Le nom de schéma d'hier résout encore** : un `$ref` cassé ne fait pas rater un
   modèle, il fait rater la génération ENTIÈRE du client.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import pytest

from oto_mcp import deprecations, openapi


# ── 1 & 2. Les deux noms, la même valeur ────────────────────────────────────

def test_le_helper_sert_les_deux_noms_quel_que_soit_celui_ecrit():
    """Bidirectionnel à dessein : un handler neuf écrit le nom d'aujourd'hui, une
    ligne SQL porte encore celui d'hier (la COLONNE ne se renomme qu'au lot B4)."""
    marqueur = object()
    for ancienne, actuelle in deprecations.CLES.items():
        depuis_neuf = deprecations.avec_les_deux_noms({actuelle: marqueur})
        assert depuis_neuf[ancienne] is depuis_neuf[actuelle] is marqueur, ancienne
        depuis_vieux = deprecations.avec_les_deux_noms({ancienne: marqueur})
        assert depuis_vieux[ancienne] is depuis_vieux[actuelle] is marqueur, ancienne


def test_le_producteur_garde_le_dernier_mot():
    """Une clé déjà posée n'est jamais écrasée — sinon le helper pourrait effacer
    une valeur que le handler avait voulue différente."""
    out = deprecations.avec_les_deux_noms({"guide": "neuf", "doctrine": "vieux"})
    assert out == {"guide": "neuf", "doctrine": "vieux"}


def test_le_helper_ne_touche_pas_ce_quon_ne_lui_donne_pas():
    """La garde qui protège la donnée de l'utilisateur : le helper ne descend PAS
    dans les valeurs. Une ligne de tableau dont une colonne s'appelle « doctrine »
    ne doit pas gagner une colonne fantôme parce qu'elle voyage dans une réponse."""
    ligne = {"colonnes": {"doctrine": "texte du client"}, "guide": "x"}
    out = deprecations.avec_les_deux_noms(ligne)
    assert out["colonnes"] == {"doctrine": "texte du client"}
    assert "guide" not in out["colonnes"]


def test_aucune_cle_dhier_ne_disparait_dune_reponse():
    """Le contrat visible de l'extérieur : additif, jamais un renommage sec."""
    payload = {actuelle: i for i, actuelle in enumerate(deprecations.CLES.values())}
    out = deprecations.avec_les_deux_noms(payload)
    manquantes = sorted(a for a in deprecations.CLES if a not in out)
    assert not manquantes, f"clés d'hier perdues : {manquantes}"


# ── 3. L'ancien nom est encore accepté en ENTRÉE ────────────────────────────

def test_le_guide_se_lit_par_lancien_ET_le_nouveau_nom_de_parametre():
    """Le piège qu'on évite : servir `guide_id` en réponse sans l'accepter en
    entrée. L'agent recopie ce qu'il lit — il doit aboutir."""
    from oto_mcp.capabilities.orgs import instructions as oi
    champs = oi.GuideGetInput.model_fields
    assert "guide_id" in champs and "doctrine_id" in champs
    assert oi.GuideGetInput(doctrine_id=7).doctrine_id == 7
    assert oi.GuideGetInput(guide_id=7).guide_id == 7


def test_la_console_de_procedure_accepte_les_deux_noms():
    from oto_mcp.capabilities import procedure_console as pc
    champs = pc.ProcedureInput.model_fields
    assert "guide_id" in champs and "doctrine_id" in champs


@pytest.mark.asyncio
async def test_run_start_accepte_les_deux_noms_de_parametre():
    """`run_start(doctrine=…)` est cité dans des procédures écrites il y a des mois,
    que personne ne réécrira d'un coup."""
    from oto_mcp import server
    tool = await _test_mcp().get_tool("run_start")
    props = (tool.parameters or {}).get("properties", {})
    assert "guide" in props and "doctrine" in props
    requis = set((tool.parameters or {}).get("required", []))
    assert "guide" not in requis and "doctrine" not in requis


# ── 4. Ce que le SERVEUR sert vraiment ──────────────────────────────────────

def test_les_modeles_de_sortie_declarent_les_deux_noms():
    """Un `Output` DÉCRIT la réponse : s'il ne déclare que le nom d'aujourd'hui,
    `/openapi.json` promet une forme que le serveur n'envoie pas (et l'inverse)."""
    from oto_mcp.capabilities import org_monitoring, guide_library
    from oto_mcp.capabilities.connectors import selection
    from oto_mcp.capabilities.groups import guide as groupe_guide
    from oto_mcp.capabilities.orgs import instructions as oi

    paires = [
        (oi.GuideView, ("guide_id", "doctrine_id")),
        (oi.GuideView, ("guide", "doctrine")),
        (oi.GuideView, ("group_guide", "group_doctrine")),
        (oi.GuideView, ("guides", "doctrines")),
        (oi.InstructionsBundle, ("guide", "doctrine")),
        (groupe_guide.GroupInstructionsBundle, ("guide", "doctrine")),
        (groupe_guide.GroupInstructionsBundle, ("guide_version", "doctrine_version")),
        (guide_library.LibraryList, ("guides", "doctrines")),
        (selection.MyConnectorRow, ("guide_ref_count", "doctrine_ref_count")),
        (org_monitoring.RunRow, ("guide", "doctrine")),
    ]
    for modele, (actuelle, ancienne) in paires:
        champs = modele.model_fields
        assert actuelle in champs, f"{modele.__name__} ne déclare pas `{actuelle}`"
        assert ancienne in champs, (
            f"{modele.__name__} a perdu `{ancienne}` avant sa date de retrait")


def test_le_bundle_vide_dune_org_absente_porte_les_deux_noms():
    """Le chemin le plus lu du domaine — un début de session sans org — et le seul
    qui répond 200 avec tout à vide. S'il perdait une clé, personne ne verrait
    d'erreur : le client afficherait « pas de readme »."""
    import asyncio
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import instructions as oi
    out = asyncio.run(oi._get_guide(ResolvedCtx(sub="u1", org_id=None),
                                    oi.GuideGetInput()))
    for actuelle, ancienne in (("guide", "doctrine"), ("guides", "doctrines"),
                               ("group_guide", "group_doctrine")):
        assert actuelle in out and ancienne in out
        assert out[actuelle] == out[ancienne]


# ── 5. Le code d'erreur d'hier ──────────────────────────────────────────────

def test_le_code_dhier_survit_dans_les_details():
    for ancien, actuel in deprecations.CODES.items():
        details = deprecations.details_avec_code_dhier(actuel)
        assert details["legacy_code"] == ancien


def test_un_code_sans_ancetre_nen_invente_pas():
    assert deprecations.details_avec_code_dhier("unknown_run") == {}
    assert deprecations.details_avec_code_dhier("unknown_run", {"a": 1}) == {"a": 1}


# ── 6. Le nom de schéma d'hier résout encore ────────────────────────────────

def test_les_noms_de_schema_dhier_resolvent():
    doc = openapi.build()
    schemas = doc["components"]["schemas"]
    for ancien, actuel in deprecations.SCHEMAS.items():
        assert actuel in schemas, f"`{actuel}` absent de components.schemas"
        assert schemas[ancien]["$ref"] == f"#/components/schemas/{actuel}", (
            f"`{ancien}` ne résout plus : un $ref cassé fait rater la génération "
            "ENTIÈRE d'un client, pas seulement le modèle visé.")
        assert schemas[ancien]["deprecated"] is True
        assert deprecations.date_de_retrait() in schemas[ancien]["description"]


def test_la_table_des_schemas_ne_porte_que_de_vrais_composants():
    """⚠️ Un modèle `Output` de premier niveau n'est PAS un composant : son schéma est
    inline dans la 200, et son nom n'y apparaît que comme `title`, ce qu'aucun `$ref`
    ne peut viser. Aliaser un nom pareil publierait un composant qui n'existait pas
    — et laisserait croire à un contrat là où il n'y en avait jamais eu."""
    doc = openapi.build()
    schemas = doc["components"]["schemas"]
    for actuel in deprecations.SCHEMAS.values():
        assert actuel in schemas, (
            f"`{actuel}` n'est pas un composant : cette entrée n'a rien à faire dans "
            "`deprecations.SCHEMAS`.")
