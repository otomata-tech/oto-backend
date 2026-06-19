"""Seed des logos d'éditeur de connecteurs sur Scaleway Object Storage (oto-media).

One-shot idempotent. Uploade chaque asset local `assets/connector-logos/<name>.<ext>`
sous la clé conventionnelle `connector-logos/<name>.png` (public-read), que
`media_store.connector_logo_url(name)` / `Connector.logo_url_for()` servent au
catalogue. Aucune écriture DB : la présence de l'objet EST le câblage.

Un connecteur sans asset → simplement pas de logo (placeholder côté UI). Les noms
de connecteurs viennent du registre source unique (`providers.REGISTRY`).

Lancer (sur la box, env S3 dans le process) :
    cd /opt/oto-mcp && ./.venv/bin/python -m scripts.seed_connector_logos
"""
from __future__ import annotations

import os
import sys

from oto_mcp import media_store, providers

# Répertoire des assets committé dans le repo (à côté du package).
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "assets", "connector-logos")
_EXT_CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".webp": "image/webp"}


def _find_asset(name: str) -> str | None:
    for ext in (".png", ".webp", ".jpg", ".jpeg"):
        path = os.path.join(_ASSETS_DIR, f"{name}{ext}")
        if os.path.exists(path):
            return path
    return None


def main() -> None:
    if not os.path.isdir(_ASSETS_DIR):
        print(f"⚠ aucun répertoire d'assets ({_ASSETS_DIR}) — rien à seeder.",
              file=sys.stderr)
        return
    uploaded, skipped = 0, 0
    for name in sorted(providers.REGISTRY):
        path = _find_asset(name)
        if not path:
            skipped += 1
            continue
        with open(path, "rb") as f:
            data = f.read()
        # Clé canonique connector-logos/<name>.png (lue par connector_logo_url).
        # upload_image valide + hashe ; on veut la clé STABLE → put_object direct.
        ct = _EXT_CT.get(os.path.splitext(path)[1].lower(), "image/png")
        key = f"connector-logos/{name}.png"
        media_store._get_client().put_object(
            Bucket=media_store._bucket(), Key=key, Body=data, ContentType=ct,
            ACL="public-read", CacheControl="public, max-age=86400",
        )
        print(f"  ✓ {name} → {media_store.public_url(key)}")
        uploaded += 1
    print(f"\n{uploaded} logo(s) uploadé(s), {skipped} connecteur(s) sans asset.")


if __name__ == "__main__":
    main()
