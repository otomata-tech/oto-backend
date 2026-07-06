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


# --- Guides ON-DEMAND scopés (ADR 0042 B5) : platform=fichiers, org/user=DB ---------

class GuideError(ValueError):
    """Écriture de guide invalide (slug mal formé, scope non éditable…)."""


def _slug_ok(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def list_guides_for(sub: Optional[str] = None, org_id: Optional[int] = None) -> list[dict]:
    """Guides on-demand VISIBLES par le caller : plateforme (fichiers) ∪ org active (DB)
    ∪ user (DB). Chaque entrée porte son `scope`. Sans les corps."""
    out = [{**g, "scope": "platform"} for g in list_guides()]
    from . import db
    if org_id is not None:
        out += [{"slug": g["slug"], "scope": "org", "title": g["title"],
                 "description": g["description"]} for g in db.list_guides_db("org", str(org_id))]
    if sub:
        out += [{"slug": g["slug"], "scope": "user", "title": g["title"],
                 "description": g["description"]} for g in db.list_guides_db("user", sub)]
    return out


def read_guide_scoped(slug: str, *, scope: Optional[str] = None,
                      org_id: Optional[int] = None, sub: Optional[str] = None) -> Optional[dict]:
    """Lit un guide on-demand. `scope` explicite, sinon cherche plateforme → org → user
    (1er match). Renvoie `{slug, scope, title, description, body_md}` ou None."""
    from . import db
    for sc in ([scope] if scope else ["platform", "org", "user"]):
        if sc == "platform":
            g = read_guide(slug)                       # fichiers
            if g:
                return {**g, "scope": "platform"}
        elif sc == "org" and org_id is not None:
            g = db.get_guide_db("org", str(org_id), slug)
            if g:
                return {"slug": slug, "scope": "org", "title": g["title"],
                        "description": g["description"], "body_md": g["body_md"]}
        elif sc == "user" and sub:
            g = db.get_guide_db("user", sub, slug)
            if g:
                return {"slug": slug, "scope": "user", "title": g["title"],
                        "description": g["description"], "body_md": g["body_md"]}
    return None


def set_guide(scope: str, owner_id: str, slug: str, body_md: str,
              title: str = "", description: str = "") -> dict:
    """Crée/met à jour un guide on-demand (scope `org`|`user` seulement — `platform` =
    fichiers, édités en PR). Slug strict. Renvoie `{slug, scope, title, description}`."""
    if scope not in ("org", "user"):
        raise GuideError("scope éditable = org | user (platform = fichiers, PR).")
    if not _slug_ok(slug):
        raise GuideError("slug invalide (min. `^[a-z0-9][a-z0-9-]*$`).")
    if not (body_md or "").strip():
        raise GuideError("body_md requis.")
    from . import db
    row = db.set_guide_db(scope, str(owner_id), slug, body_md.strip(),
                          (title or "").strip(), (description or "").strip())
    return {"slug": slug, "scope": scope, "title": row["title"],
            "description": row["description"]}


def delete_guide(scope: str, owner_id: str, slug: str) -> bool:
    if scope not in ("org", "user"):
        raise GuideError("scope éditable = org | user.")
    from . import db
    return db.delete_guide_db(scope, str(owner_id), slug)


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
