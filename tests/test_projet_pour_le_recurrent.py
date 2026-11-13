"""Un projet se crée pour un travail récurrent ou à la demande — jamais pour une question ponctuelle.

Le socle injecté et la notice disaient « tâche ad-hoc = crée un projet, jamais hors-sol »
(« For an ad-hoc task with no existing project, **create a project** »). Les agents l'ont
appliqué à la lettre : chez un client, une session par affaire a ouvert un projet personnel
vide (un brief d'une ligne, rien d'autre), cinq en un mois, que l'org a vus comme du bruit,
archivés à la main, puis neutralisés par une règle de son propre readme (usage signal 1734).

La règle « travaille dans un projet » reste : `_project=<id>` sur chaque appel quand on agit
POUR un projet. Ce qui change est le déclencheur de la CRÉATION : un travail récurrent, ou
une demande de l'utilisateur. Une question ponctuelle se traite, dans un projet existant
quand il couvre le sujet, sinon sans projet.

⚠️ Le texte servi en production est l'override DB du bloc A et la copie DB de la notice
(toutes deux divergentes du code) : ce banc tient la SOURCE versionnée, le geste en base
suit le déploiement (docs/conventions.md, override du bloc A).
"""
from __future__ import annotations

import re

from oto_mcp import guide_store, instructions


def _section_projet(body: str) -> str:
    m = re.search(r"^## Work in a project\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    assert m, "la notice n'a plus de section « Work in a project »"
    return m.group(1)


def test_le_socle_ne_fait_plus_creer_un_projet_pour_une_tache_ad_hoc():
    socle = instructions._SECRET_SAUCE
    assert "tâche ad-hoc = crée un projet" not in socle
    assert "jamais hors-sol" not in socle


def test_le_socle_dit_quand_un_projet_se_cree():
    ligne = next(l for l in instructions._SECRET_SAUCE.splitlines()
                 if "Travaille dans un projet" in l)
    assert "`_project=<id>` sur chaque appel" in ligne, "la règle d'usage du projet reste"
    assert "ponctuel" in ligne, "une question ponctuelle se traite sans projet neuf"
    assert "récurrent" in ligne and "demande" in ligne, (
        "les deux déclencheurs de création : travail récurrent, demande de l'utilisateur")


def test_la_notice_ne_fait_plus_creer_un_projet_pour_une_tache_ad_hoc():
    section = _section_projet(guide_store.file_guide("notice")["body_md"])
    assert "**create a project** to host" not in section
    assert "ad-hoc task with no existing project" not in section


def test_la_notice_dit_quand_un_projet_se_cree_et_garde_le_reste():
    section = _section_projet(guide_store.file_guide("notice")["body_md"])
    assert "recurring work" in section and "when the user asks" in section
    assert "one-off" in section, "la question ponctuelle est nommée et se traite"
    assert "`oto_project op=list`" in section, "chercher le projet existant d'abord"
    # Le reste de la règle « travaille dans un projet » ne bouge pas.
    for garde in ("`_project=<id>` token on EVERY work call",
                  "`oto_project(op=link, target_type=tableau)`",
                  "do not create resources specific to the procedure"):
        assert garde in section, f"« {garde} » a disparu de la section"
