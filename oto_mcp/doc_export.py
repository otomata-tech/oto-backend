"""Export d'un projet (KB) en ARBORESCENCE markdown (oto/#6 B2 — réversibilité).

Transforme la liste plate des pages (avec `parent_id`) en un zip de fichiers `.md`
reflétant l'arbre : une page AVEC enfants → dossier `<slug>/` + `_index.md` (son
corps) ; une feuille → `<slug>.md`. Fonction PURE (pas d'I/O DB) → testable ;
le caller charge `list_docs_for_project` et sert les bytes en REST."""
from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict


# Repli d'accents FR → ASCII (noms de fichiers portables), même jeu que db/projects.
_ACCENTS = "àâäáãéèêëïîíôöóòõùûüúçñýÿÀÂÄÁÃÉÈÊËÏÎÍÔÖÓÒÕÙÛÜÚÇÑÝŸ"
_PLAIN = "aaaaaeeeeiiiooooouuuucnyyAAAAAEEEEIIIOOOOOUUUUCNYY"
_FOLD = str.maketrans(_ACCENTS, _PLAIN)


def _slug(title: str, fallback_id) -> str:
    """Nom de fichier SÛR à partir d'un titre : ASCII, sans séparateur de chemin."""
    s = (title or "").translate(_FOLD).strip().lower()
    s = re.sub(r"[^a-z0-9\s.-]", "", s)                 # ASCII : lettres/chiffres/_/-/.
    s = re.sub(r"[\s]+", "-", s).strip("-.")
    return s or f"page-{fallback_id}"


def build_export(docs: list[dict], root_name: str = "kb") -> bytes:
    """Zip (bytes) de l'arborescence markdown des `docs` (mêmes champs que
    `list_docs_for_project` : id, parent_id, title, body_md, position)."""
    by_parent: dict = defaultdict(list)
    for d in docs:
        by_parent[d.get("parent_id")].append(d)
    for v in by_parent.values():
        v.sort(key=lambda d: (d.get("position") if d.get("position") is not None else 1 << 30,
                              (d.get("title") or "").lower()))

    files: dict[str, str] = {}

    def _header(d: dict) -> str:
        title = (d.get("title") or "").replace("\n", " ")
        return f"# {title}\n\n" if title else ""

    def walk(parent_id, prefix: str) -> None:
        used: set[str] = set()
        for d in by_parent.get(parent_id, []):
            slug = _slug(d.get("title"), d.get("id"))
            # dé-dup entre frères (deux pages de même titre).
            base, n = slug, 2
            while slug in used:
                slug = f"{base}-{n}"; n += 1
            used.add(slug)
            body = _header(d) + (d.get("body_md") or "")
            if by_parent.get(d.get("id")):
                files[f"{prefix}{slug}/_index.md"] = body
                walk(d.get("id"), f"{prefix}{slug}/")
            else:
                files[f"{prefix}{slug}.md"] = body

    walk(None, "")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if not files:
            z.writestr(f"{root_name}/README.md", "# (aucune page)\n")
        for path, content in sorted(files.items()):
            z.writestr(f"{root_name}/{path}", content)
    return buf.getvalue()
