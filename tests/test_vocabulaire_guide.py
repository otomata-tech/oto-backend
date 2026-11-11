"""CLIQUET de vocabulaire : « doctrine » ne peut que RECULER dans `oto_mcp/`.

Décision d'Alexis du 28/08/2026 (#519) : le produit ne dit plus « doctrine » — il
dit **guide** (ADR 0042, le guide = primitive unique d'instruction) et
**procédure** pour ce qui s'exécute. Le lot A a retiré le mot de l'INTERNE :
modules, symboles, variables, commentaires, docstrings non servies, docs.

Ce qui reste ne reste PAS par oubli. Chaque occurrence encore là est un **alias de
compatibilité** : un nom qui SORT du serveur et qu'on ne peut pas renommer sans
casser un appelant qui vit hors de ce dépôt (dashboard, extension, CLI, fronts
partenaires, flotte d'agents) ou sans toucher une base PARTAGÉE prod/preprod.
Leur retrait est le lot B, avec préavis et date écrite.

Pourquoi un cliquet et pas une règle de revue. Parce qu'une règle de vocabulaire
ne survit pas à six mois : le mot revient par un copier-coller depuis un fichier
voisin, ou par un module neuf qui imite son aîné. Le compte, lui, ne discute pas.

Ce que le cliquet garde exactement :

1. **Aucun fichier hors allowlist ne porte le mot.** Un module neuf qui l'emploie
   rougit tout de suite — c'est le cas le plus probable, et le seul qui ferait
   repartir la dérive à zéro.
2. **Un fichier de l'allowlist ne peut pas en porter PLUS.** Le plafond est ce
   qui était mesuré à la fin du lot A. Ajouter une occurrence à un fichier qui en
   a déjà, c'est agrandir la dette qu'on est en train de rembourser.
3. **Un plafond qui n'est plus atteint est PÉRIMÉ.** Quand le lot B retire des
   alias, il baisse le plafond dans le même commit — sinon la marge libérée se
   remplirait en silence. Le test le dit et nomme le nouveau nombre.

⚠️ Le compte est en `doctrine` INSENSIBLE À LA CASSE, sur le fichier ENTIER
(`DoctrineView` compte, `oto_admin_doctrine` compte, `-- doctrine` en SQL compte).
Pas de subtilité de tokenisation : un cliquet qu'on peut contourner en changeant
la casse ou en passant par un commentaire ne garde rien.

Régénérer les plafonds après un lot qui en RETIRE :

    python - <<'EOF'
    import pathlib, re
    for p in sorted(pathlib.Path("oto_mcp").rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        try:
            n = len(re.findall("doctrine", p.read_text(encoding="utf-8"), re.I))
        except (UnicodeDecodeError, OSError):
            continue
        if n:
            print(f'    "{p}": {n},')
    EOF
"""
from __future__ import annotations

import pathlib
import re

RACINE = pathlib.Path(__file__).resolve().parents[1] / "oto_mcp"
MOT = re.compile("doctrine", re.I)

# ── Plafonds relevés à la fin du lot B5 de #519 (2026-08-29) ─────────────────
# Chaque entrée porte la RAISON pour laquelle le mot y survit. Une entrée sans
# raison servie n'a rien à faire ici : elle se renomme.
#
# Total : 267 (fin du lot A) → 266 (B1) → 262 (B2) → 226 (B3) → 217 (B4) → 162 (B5)
# → 164 (#659, 2026-09-01) → 145 (otomata-tech/oto#65, 2026-09-23)
# → 139 (2026-10-12, la lecture `recent_runs` sert `guide`).
# Le compte descend à chaque PR, jamais l'inverse. Zéro au lot D (#526).
#
# ⚠️ **La seule remontée, et pourquoi elle n'est pas une dérive** : #659 a DÉCLARÉ le
# contrat de `POST /api/resources`, ce qui écrit une seconde fois — dans un modèle
# pydantic — l'énuméré `resource_type` que `resources.py` servait déjà. Ce sont deux
# occurrences de la VALEUR (famille 2 ci-dessus), pas deux usages du mot : le lot D
# les emporte avec le kind d'ownership, d'un seul geste. Si une PR fait remonter ce
# total pour une autre raison, c'est la dérive que ce cliquet existe pour attraper.
#
# ⚠️ **Le lot B est fini : ce qui reste ne relève plus du vocabulaire.** Trois
# familles, et une seule d'entre elles se lit encore comme un « mot à changer » :
#
#   1. **Des alias SERVIS, datés** — clés de réponse, paramètres d'entrée, codes
#      d'erreur. Ils s'en vont le 30/11/2026 avec le reste, en retirant les appels à
#      `deprecations.avec_les_deux_noms` (lot D, #526).
#   2. **Des DONNÉES déjà écrites en base** — colonne `runs.doctrine`, valeur
#      `missing_doctrine` d'un CHECK, kind d'ownership `doctrine` dans
#      `resource_grants.resource_type`, clé `doctrine_version` des `props` d'un nœud,
#      DDL de la table. Aucune vue ne les renomme : il faut une migration nommée
#      (ADR 0065 étage 2), sur une base PARTAGÉE prod/preprod. Ce n'est pas un
#      renommage, c'est un lot.
#   3. **La table des alias elle-même** (`deprecations.py`), qui disparaît en entier.
#
# ⚠️ Le compte ne regarde QUE `oto_mcp/`. Deux scripts d'exploitation portent encore
# le mot dans leur NOM de fichier (`scripts/seed_doctrine_library.py`,
# `smoke_capability_doctrine_library.py`) : hors du radar, et volontairement pas
# renommés ici — un opérateur a ces commandes dans ses runbooks. Suivi dans #526.
# (Il y en avait trois : `seed_talent_doctrines.py` est parti le 23/09/2026 avec les
# guides qu'il publiait — otomata-tech/oto#240.)
PLAFONDS: dict[str, int] = {
    # — Clé de réponse `doctrine`, servie à côté de `guide`.
    "oto_mcp/capabilities/agent_context.py": 3,
    "oto_mcp/capabilities/org_monitoring.py": 2,
    "oto_mcp/capabilities/projects.py": 2,
    # — Clé de réponse `doctrine_ref_count`, servie à côté de `guide_ref_count`.
    "oto_mcp/capabilities/connectors/selection.py": 2,
    # — Champ `run_doctrine` d'un `Output` qui NE CORRESPOND PAS au payload servi
    #   (il porte `doctrine`/`guide`) : divergence antérieure à #519, laissée telle
    #   quelle pour ne pas mêler une correction de contrat à un lot de vocabulaire.
    "oto_mcp/capabilities/datastore/activity.py": 3,
    # — Clés de réponse `doctrine`/`doctrine_version`, servies à côté des `guide*`.
    #   (Les trois descriptions servies qui disaient « base doctrine » sont passées à
    #   « base guide (readme) » le 23/09/2026, oto#65.)
    "oto_mcp/capabilities/groups/guide.py": 5,
    # — Clé de réponse `doctrines`, servie à côté de `guides`.
    "oto_mcp/capabilities/guide_library.py": 2,
    # — Clés de réponse `doctrine`/`doctrines`/`doctrine_id`/`group_doctrine` servies
    #   à côté des `guide*` ; paramètre `doctrine_id` toujours accepté ; code d'erreur
    #   d'hier ; kind d'ownership `doctrine` (VALEUR en base, lot D).
    "oto_mcp/capabilities/orgs/instructions.py": 11,
    "oto_mcp/capabilities/procedure_console.py": 3,
    # (`capabilities/resources*.py` sont sortis le 23/09/2026 — otomata-tech/oto#65,
    #  arbitrage d'Alexis : « doctrine est l'ancien terme, ne doit plus apparaître ».
    #  La famille publique s'appelle `procedure` ; sa valeur STOCKÉE est nommée une
    #  seule fois, `ownership.TYPE_RESSOURCE_PROCEDURE`, et traduite à la frontière
    #  par `resources_contract.KIND_OF`. `org_store/instruction_ownership.py` est
    #  tombé à 0 du même geste.)
    # — Valeur d'énumération servie `missing_doctrine` (contrainte CHECK en base).
    "oto_mcp/capabilities/usage.py": 2,
    # — La même valeur, nommée à l'agent intégré pour qu'il la passe à `feedback` :
    #   la renommer ici seul ferait refuser son signalement par le CHECK (lot D).
    "oto_mcp/capabilities/copilot/consigne.py": 1,
    # — DDL et migration de colonne : le SEUL endroit qui nomme encore la table. La
    #   vue `guide_library` porte le nom d'aujourd'hui et tout le code passe par elle
    #   (garde : `tests/test_guide_library_view.py`).
    "oto_mcp/db/_init.py": 2,
    "oto_mcp/db/schema/procedures.py": 14,
    # — DDL FIGÉ (base partagée prod/preprod) : colonne `runs.doctrine`, valeur
    #   `missing_doctrine`, et les commentaires qui les décrivent.
    "oto_mcp/db/schema/connectors.py": 1,
    "oto_mcp/db/schema/orgs.py": 1,
    "oto_mcp/db/schema/projects.py": 1,
    "oto_mcp/db/schema/runs.py": 4,
    "oto_mcp/db/schema/usage.py": 2,
    # — `resource_type = 'doctrine'` + clé `doctrine_version` d'un JSON de nœud :
    #   des DONNÉES écrites, pas des noms.
    "oto_mcp/db/nodes.py": 2,
    "oto_mcp/db/shell.py": 2,
    "oto_mcp/ownership.py": 1,
    # — Colonne `runs.doctrine`, clé `doctrine_version` des args journalisés, alias
    #   SQL `AS doctrine`/`AS doctrines` (donc clés de réponse).
    "oto_mcp/db/usage.py": 27,
    "oto_mcp/project_audit.py": 2,
    # — L'inventaire des colonnes porteuses d'un `sub`, vérifié CONTRE LE DDL : une
    #   vue n'y apparaît pas, donc cette entrée reste sur la TABLE (sinon le
    #   garde-fou devient aveugle à une entrée morte). Elle suit la table au lot D.
    "oto_mcp/db/users.py": 1,
    # — LA table des noms SERVIS dépréciés (lot B, retrait daté au lot D #526).
    #   Le seul fichier où le mot est une DONNÉE et non un usage : il y entre au
    #   moment où une surface est renommée, et le fichier entier disparaît au retrait.
    #   ⚠️ **Remonté de 30 à 32 le 23/09/2026, et c'est l'inverse d'une dérive** : la
    #   valeur `resource_type` d'hier (oto#65) entre dans la table des VALEURS
    #   renommées (+1) avec le nom de l'écran qui l'envoie encore (+1). Ailleurs, le
    #   même lot retire 18 occurrences ; le total baisse de 161 à 145.
    "oto_mcp/deprecations.py": 32,
    # — Le paramètre `doctrine` de `run_start` (accepté à côté de `guide`), ses clés
    #   de réponse, et l'arg tracé `doctrine_version` (écrit dans `tool_calls.args`).
    "oto_mcp/guide_run.py": 1,
    "oto_mcp/tools/guide_run.py": 6,
    "oto_mcp/server.py": 3,
}


def _comptes() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(RACINE.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        try:
            texte = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue          # binaire : rien à lire, rien à compter
        n = len(MOT.findall(texte))
        if n:
            out[p.relative_to(RACINE.parent).as_posix()] = n
    return out


def test_aucun_fichier_neuf_ne_reprend_le_mot():
    intrus = sorted(set(_comptes()) - set(PLAFONDS))
    assert not intrus, (
        f"« doctrine » apparaît dans {len(intrus)} fichier(s) qui n'en portaient pas : "
        f"{intrus}. Le produit dit **guide** (ADR 0042) et **procédure** pour ce qui "
        "s'exécute — cf. #519. Si ce fichier sert VRAIMENT un nom historique à un "
        "client hors dépôt, ajoute-le à `PLAFONDS` avec la raison ; sinon, renomme.")


def test_aucun_fichier_nen_porte_davantage():
    comptes = _comptes()
    hausses = {f: (comptes[f], PLAFONDS[f])
               for f in comptes if f in PLAFONDS and comptes[f] > PLAFONDS[f]}
    assert not hausses, (
        f"Le mot regagne du terrain : {hausses} (actuel, plafond). Ces fichiers "
        "portent des alias de compatibilité qu'on rembourse au lot B — on n'en "
        "ajoute pas. Emploie « guide » ou « procédure », ou passe par une constante "
        "nommée si tu dois écrire le nom SERVI une fois de plus.")


def test_un_plafond_devenu_trop_haut_se_rabaisse():
    """Le cliquet ne tient que si la marge libérée ne peut pas se remplir."""
    comptes = _comptes()
    perimes = {f: (comptes.get(f, 0), PLAFONDS[f])
               for f in PLAFONDS if comptes.get(f, 0) < PLAFONDS[f]}
    assert not perimes, (
        f"{len(perimes)} plafond(s) ne sont plus atteints : {perimes} (actuel, "
        "plafond). Baisse-les dans CE commit — un plafond au-dessus du réel est de "
        "la place libre pour la prochaine occurrence. Un fichier tombé à 0 sort de "
        "`PLAFONDS`. Recette dans le docstring.")
