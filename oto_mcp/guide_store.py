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


# Prose INIT dans `guides` (delivery='init'). Slugs canoniques par scope :
# platform = la clé passée (secret_sauce) ; org/group/user = 'readme'.
PLATFORM_OWNER = "platform"
PLATFORM_SLUG = "secret_sauce"
INIT_SLUG = "readme"
# Scopes dont la prose INIT vit dans `guides` (ADR 0042). TOUS depuis le barreau 2 :
# le readme d'org/group est sorti de `*_instructions[claude_md]` (split readme↔procédure,
# les procédures gardent leur table + versioning). Owner = org.id/group.id::text.
_INIT_IN_GUIDES = ("platform", "user", "org", "group")


def _init_ref(scope: str, ident: Optional[str]) -> tuple[str, str]:
    """(owner_id de colonne, slug) d'un readme init dans `guides`. Pour platform,
    `ident` EST le slug (la clé, ex. secret_sauce), l'owner est constant."""
    if scope == "platform":
        return PLATFORM_OWNER, (ident or PLATFORM_SLUG)
    return str(ident), INIT_SLUG


def init_guide_body(scope: str, owner_id: Optional[str] = None) -> Optional[str]:
    """Corps BRUT (stripped) de la prose « init » d'un scope. None si absent/vide/erreur
    (**fail-open** ; le rendu — header, variables, ordre, seed plateforme — reste chez
    l'appelant `instructions.py`).

    Tous les scopes (platform/user/org/group) = `guides` delivery='init' (ADR 0042)."""
    try:
        if scope in _INIT_IN_GUIDES:
            from . import db
            owner, slug = _init_ref(scope, owner_id)
            row = db.get_init_guide_db(scope, owner, slug)
        else:
            return None
    except Exception:  # noqa: BLE001
        logger.warning("init_guide_body(%s, %s) échec (fail-open)", scope, owner_id,
                       exc_info=True)
        return None
    body = ((row or {}).get("body_md") or "").strip()
    return body or None


def get_init_guide(scope: str, owner_id: Optional[str] = None) -> dict:
    """État d'un readme init (scopes dans `guides`) : `{body_md, updated_at}`. Jamais
    None — un owner sans ligne renvoie l'état vide. Sert les vues d'édition."""
    if scope not in _INIT_IN_GUIDES:
        raise GuideError(f"get_init_guide: scope `{scope}` pas encore dans guides.")
    from . import db
    owner, slug = _init_ref(scope, owner_id)
    row = db.get_init_guide_db(scope, owner, slug)
    return {"body_md": (row or {}).get("body_md") or "",
            "updated_at": (row or {}).get("updated_at")}


def set_init_guide(scope: str, owner_id: Optional[str], body_md: str) -> dict:
    """Écrit un readme init (upsert). Renvoie `{body_md, updated_at}`. Scopes dans
    `guides` seulement (platform/user au barreau 1)."""
    if scope not in _INIT_IN_GUIDES:
        raise GuideError(f"set_init_guide: scope `{scope}` pas encore dans guides.")
    from . import db
    owner, slug = _init_ref(scope, owner_id)
    row = db.set_init_guide_db(scope, owner, slug, body_md)
    return {"body_md": row.get("body_md") or "", "updated_at": row.get("updated_at")}


def seed_init_guide(scope: str, ident: Optional[str], body_md: str) -> None:
    """Pose le défaut d'un readme init s'il n'existe pas (boot, idempotent)."""
    from . import db
    owner, slug = _init_ref(scope, ident)
    db.seed_init_guide_db(scope, owner, slug, body_md)


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
