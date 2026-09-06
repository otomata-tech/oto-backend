"""Archive les « Base de connaissance » VIDES posées par une simple lecture.

Jusqu'au correctif de `capabilities/kb.py` (`op="get"` créait), le seul fait
d'ouvrir un front — qui monte le provider Documents à la racine — suffisait à
créer un projet « Base de connaissance » dans l'org du client. Résultat : des
orgs entières portent un projet vide que personne n'a demandé, et que personne
ne reconnaît. Le correctif arrête l'hémorragie ; ce script nettoie l'existant.

**Ne touche QUE ce qui est indiscutablement un fantôme** : le projet ancré par
`orgs.kb_project_id`, vivant, possédé par l'org, et SANS AUCUNE PAGE. Une KB qui
porte ne serait-ce qu'un document est laissée intacte — un client a pu s'en
servir, et une page est la preuve qu'il l'a fait.

Archive (pas de delete dur — même règle que kb.py sur les doublons) puis lève
l'ancre, pour que le prochain `op="ensure"` reparte d'une KB neuve si l'org en
veut vraiment une un jour.

    # sur la box, après déploiement :
    ssh -i ~/.ssh/<clé> root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.archive_empty_kb_projects"
    #   ^ dry-run par défaut : compte et liste, n'écrit rien
    #     --apply pour exécuter
"""
from __future__ import annotations

import sys

from oto_mcp import db, org_store
from oto_mcp.capabilities.kb import KB_NAME, KB_NAME_LEGACY_FR
from oto_mcp.db import _connect


def _anchored_orgs() -> list[dict]:
    """(org_id, nom d'org, kb_project_id) de toutes les orgs vivantes ancrées."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id AS org_id, name, kb_project_id
              FROM orgs
             WHERE kb_project_id IS NOT NULL AND archived_at IS NULL
             ORDER BY id
            """
        ).fetchall()
        return [dict(r) for r in rows]


def main(apply: bool) -> int:
    orgs = _anchored_orgs()
    print(f"{len(orgs)} org(s) avec une ancre KB\n")

    phantoms: list[tuple[dict, dict]] = []
    kept: list[tuple[dict, dict, int]] = []
    for org in orgs:
        project = db.get_project_by_id(int(org["kb_project_id"]))
        if project is None or project.get("archived_at") is not None:
            continue                      # déjà partie : kb.py répare l'ancre
        if project.get("owner_type") != "org" or str(project.get("owner_id")) != str(org["org_id"]):
            continue                      # transférée hors org : pas à nous
        pages = db.list_docs_for_project(int(project["id"]))
        if pages:
            kept.append((org, project, len(pages)))
        else:
            phantoms.append((org, project))

    for org, project, n in kept:
        # Les deux libellés semés dans l'histoire du produit (français jusqu'au
        # 2026-09-03, anglais depuis) : ni l'un ni l'autre n'est un renommage.
        semes = (KB_NAME, KB_NAME_LEGACY_FR)
        renamed = "" if project["name"] in semes else f" (renommée « {project['name']} »)"
        print(f"  GARDÉE   org {org['org_id']:>5} « {org['name']} » — "
              f"projet {project['id']}{renamed} : {n} page(s)")
    for org, project in phantoms:
        print(f"  FANTÔME  org {org['org_id']:>5} « {org['name']} » — "
              f"projet {project['id']} « {project['name']} » : 0 page")

    print(f"\n{len(phantoms)} fantôme(s) à archiver, {len(kept)} KB réellement utilisée(s)")
    if not apply:
        print("dry-run — rien n'a été écrit (--apply pour exécuter)")
        return 0

    for org, project in phantoms:
        db.archive_project(int(project["id"]))
        org_store.clear_kb_project(int(org["org_id"]), int(project["id"]))
        print(f"  archivé projet {project['id']} + ancre levée sur org {org['org_id']}")
    print(f"{len(phantoms)} fantôme(s) archivé(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
