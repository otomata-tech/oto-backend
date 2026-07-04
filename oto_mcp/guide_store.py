"""Guides d'usage d'oto — how-to PLATEFORME chargés à la demande (oto-backend#111).

Un guide = un fichier markdown `oto_mcp/guides/<slug>.md` avec un front-matter
minimal :

    ---
    title: Charger un gros volume (réseau, export…)
    description: déléguer à un sous-agent, reçu léger, oto_call
    ---
    <corps markdown>

Distinct des **doctrines nommées** (procédures d'ORG, per-org DB, `oto_get_doctrine`,
avec slots/versions/publish) : les guides sont **transverses, plateforme, read-only,
versionnés avec le code** (revus en PR). C'est le pendant des « claude docs » : la
notion d'« instructions server » a deux étages — toujours-injecté (bloc A/C) vs
chargé-à-la-demande (guides / doctrines), découvert par un index sans coût de prompt.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_GUIDES_DIR = Path(__file__).resolve().parent / "guides"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse(text: str) -> tuple[dict, str]:
    """(front-matter dict, corps). Front-matter absent → ({}, texte entier)."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2).strip()


def _path_for(slug: str) -> Optional[Path]:
    if not _SLUG_RE.match(slug or ""):
        return None                       # anti-traversal : slug strict, jamais de `/`
    p = _GUIDES_DIR / f"{slug}.md"
    return p if p.is_file() else None


def list_guides() -> list[dict]:
    """`[{slug, title, description}]` trié par slug — sans les corps. '' si dossier absent."""
    if not _GUIDES_DIR.is_dir():
        return []
    out = []
    for p in sorted(_GUIDES_DIR.glob("*.md")):
        try:
            meta, _ = _parse(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("guide illisible: %s", p.name, exc_info=True)
            continue
        out.append({"slug": p.stem,
                    "title": meta.get("title") or p.stem,
                    "description": meta.get("description") or ""})
    return out


def read_guide(slug: str) -> Optional[dict]:
    """`{slug, title, description, body_md}` ou None si slug inconnu/invalide."""
    p = _path_for(slug)
    if p is None:
        return None
    meta, body = _parse(p.read_text(encoding="utf-8"))
    return {"slug": slug, "title": meta.get("title") or slug,
            "description": meta.get("description") or "", "body_md": body}


def init_guide_body(scope: str, owner_id: Optional[str] = None) -> Optional[str]:
    """Corps BRUT (stripped) de la prose « init » d'un scope — mirror **lecture-seule**
    des sources existantes (ADR 0042, barreau 1 : centraliser la lecture dans le store,
    sans toucher composition/DB). None si absent/vide/erreur (**fail-open** ; le rendu —
    header, variables, ordre, seed plateforme — reste chez l'appelant `instructions.py`).

    Aucune table neuve : `platform` = `platform_instructions[owner_id|'secret_sauce']` ;
    `org`/`group` = `*_instructions` slug `claude_md` ; `user` = `user_agent_readme`."""
    try:
        if scope == "platform":
            from . import db
            row = db.get_platform_instruction(owner_id or "secret_sauce")
        elif scope == "org":
            from . import org_store
            row = org_store.get_instruction(int(owner_id), org_store.BASE_SLUG)
        elif scope == "group":
            from . import group_store, org_store
            row = group_store.get_group_instruction(int(owner_id), org_store.BASE_SLUG)
        elif scope == "user":
            from . import db
            row = db.get_user_readme(str(owner_id))
        else:
            return None
    except Exception:  # noqa: BLE001
        logger.warning("init_guide_body(%s, %s) échec (fail-open)", scope, owner_id,
                       exc_info=True)
        return None
    body = ((row or {}).get("body_md") or "").strip()
    return body or None


def guides_index_md() -> str:
    """Index markdown des guides — enrichit la description de `oto_guide` au `tools/list`
    (même pattern que `skills_index_md` pour les doctrines). '' si aucun guide."""
    guides = list_guides()
    if not guides:
        return ""
    lines = ["Guides disponibles (charge le corps avec `oto_guide(op=read, slug=…)`) :"]
    for g in guides:
        lines.append(f"- {g['slug']} — {g['title']}"
                     + (f" : {g['description']}" if g["description"] else ""))
    return "\n".join(lines)
