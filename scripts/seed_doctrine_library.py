"""Seed de la bibliothèque publique de doctrines avec les skills d'Otomata.

Publie les skills nommés d'une org (par défaut l'org Otomata) dans
`doctrine_library` en tant qu'auteur **Otomata** (author_kind='otomata',
visibility='public'). Idempotent : `publish_doctrine` fait un upsert par slug
(ré-exécuter incrémente la version sans dupliquer).

Usage (sur la box) :
    cd /opt/oto-mcp && ./.venv/bin/python -m scripts.seed_doctrine_library <org_id> [slug ...]

Sans `slug`, publie TOUS les skills nommés de l'org (hors doctrine de base).
"""
from __future__ import annotations

import sys

from oto_mcp import db, org_store


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: seed_doctrine_library <org_id> [slug ...]", file=sys.stderr)
        sys.exit(2)
    org_id = int(sys.argv[1])
    only = set(sys.argv[2:])
    db.init_db()

    skills = org_store.list_instructions(org_id, include_base=False)
    if only:
        skills = [s for s in skills if s["slug"] in only]
    if not skills:
        print("aucun skill à publier.", file=sys.stderr)
        return

    for meta in skills:
        slug = meta["slug"]
        full = org_store.get_instruction(org_id, slug)
        if not full:
            continue
        row = org_store.publish_doctrine(
            slug=slug, title=full.get("title") or "", description=full.get("description") or "",
            body_md=full["body_md"], author_kind="otomata", author_org_id=None,
            author_display="Otomata", category=meta.get("category") or "",
            visibility="public", source_org_id=org_id, source_slug=slug,
            published_by="seed",
        )
        print(f"  ✓ {slug} → bibliothèque (entrée #{row['id']} v{row['version']})")

    print(f"\n{len(skills)} doctrine(s) publiée(s) sous l'auteur Otomata.")


if __name__ == "__main__":
    main()
