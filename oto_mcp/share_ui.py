"""UI web NAVIGABLE d'un projet partagé — face navigateur des sous-domaines par projet
(`<slug>.share.oto.cx`, ADR 0032). **Lecture seule**, rendue SERVER-SIDE (lisible par un
humain ET par un agent via WebFetch, contrairement à l'ex-partage chiffré SPA `/p/p`). Le
MCP (agir) reste au path `/mcp` ; ici on ne fait que CONSULTER.

Quatre pages, toutes gatées par l'appartenance au projet (fail-closed) :
- `/`               index : brief + liens vers procédures / tableaux / docs + carte « brancher »
- `/procedures/<id>`  prose d'une procédure liée (markdown sûr)
- `/data/<id>`        lignes d'un tableau lié (table HTML), gaté par `mcp_expose_datastore`
- `/docs/<id>`        page Documents (liée au projet ou de son arbre)

Auto-portée (tokens Otomata inline + Google Fonts), aucune dépendance front. Tout contenu
utilisateur est ÉCHAPPÉ ; la prose markdown est rendue en mode sûr (`html=False` → HTML brut
échappé, `javascript:` neutralisé), même posture que `public_doc_page`.

`build_page` fait les lectures DB (SYNC) → l'appeler dans un threadpool (le serveur est
mono-loop, cf. CLAUDE.md §PERF). Il rend `(html, status)`, ou `(None, 0)` quand le path n'est
PAS une route UI (→ le dispatch retombe sur le MCP : `/mcp`, `/.well-known/*`…).
"""
from __future__ import annotations

import html
from typing import Optional

from markdown_it import MarkdownIt

# Rendu CommonMark SÛR (html=False = HTML brut échappé, pas exécuté), réutilisé (stateless).
_MD = MarkdownIt("commonmark", {"html": False})

# Plafond de lignes affichées par page de tableau (pagination par `?offset=`).
_DATA_PAGE = 100


# ── Shell HTML charté (mêmes tokens que public_doc_page) ──────────────────────
def _shell(*, title: str, inner: str, home_url: Optional[str] = None) -> str:
    safe_title = html.escape(title or "Projet")
    crumb = (f'<a class=back href="{html.escape(home_url)}">← Retour au projet</a>'
             if home_url else "")
    return f"""<!DOCTYPE html>
<html lang=fr><head>
<meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>{safe_title} · Oto</title>
<meta name=description content="Projet partagé via Oto.">
<meta name=robots content="noindex">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=Hanken+Grotesk:wght@400..700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  :root{{--bg:#fefcf5;--surface:#fff;--paper2:#f4ecd2;--ink:#2c2112;--ink-soft:#4a3a23;
    --mute:#6c5e44;--hair:#dccfa8;--primary:#f0b41e;--primary-soft:#fbe7a8;--primary-ink:#5a3b03;--accent:#2a87d8}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:'Hanken Grotesk',system-ui,sans-serif;
    line-height:1.6;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:820px;margin:0 auto;padding:48px 24px 40px}}
  .eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);font-weight:600}}
  h1{{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:34px;line-height:1.15;
    margin:8px 0 4px;letter-spacing:-.01em}}
  .back{{display:inline-block;margin-bottom:18px;color:var(--accent);text-decoration:none;font-size:13.5px}}
  .lede{{color:var(--ink-soft);font-size:16px;margin:6px 0 8px}}
  .lede p{{margin:.5em 0}}
  .card{{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:24px 28px;margin:22px 0}}
  .card h2{{font-family:'Bricolage Grotesque',sans-serif;font-size:16px;margin:0 0 12px;font-weight:700}}
  .nav{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
  .nav a{{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--ink);
    background:var(--paper2);border:1px solid var(--hair);border-radius:10px;padding:11px 14px;font-size:14.5px}}
  .nav a:hover{{border-color:var(--primary)}}
  .nav .k{{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--primary-ink);
    background:var(--primary-soft);border-radius:5px;padding:2px 7px;white-space:nowrap}}
  .empty{{color:var(--mute);font-size:14px}}
  .chips{{display:flex;flex-wrap:wrap;gap:6px}}
  .chip{{font-family:'JetBrains Mono',monospace;font-size:11.5px;background:var(--primary-soft);
    color:var(--primary-ink);border-radius:6px;padding:3px 8px}}
  article{{color:var(--ink-soft);font-size:15.5px}}
  article h1,article h2,article h3{{font-family:'Bricolage Grotesque',sans-serif;color:var(--ink)}}
  article h2{{font-size:22px;margin:1.4em 0 .4em}} article h3{{font-size:18px;margin:1.2em 0 .3em}}
  article p{{margin:.7em 0}} article a{{color:var(--accent)}}
  article ul,article ol{{margin:.6em 0;padding-left:22px}} article li{{margin:.25em 0}}
  article code{{font-family:'JetBrains Mono',monospace;font-size:.88em;background:var(--paper2);border-radius:5px;padding:1px 5px}}
  article pre{{background:var(--paper2);border:1px solid var(--hair);border-radius:10px;padding:14px 16px;overflow-x:auto}}
  article pre code{{background:none;padding:0}}
  article blockquote{{margin:.8em 0;padding:.2em 16px;border-left:3px solid var(--hair);color:var(--mute)}}
  .tablewrap{{overflow-x:auto;border:1px solid var(--hair);border-radius:12px}}
  table{{border-collapse:collapse;font-size:13.5px}}
  th,td{{border-bottom:1px solid var(--hair);padding:8px 12px;text-align:left;vertical-align:top;
    min-width:90px;max-width:340px;overflow-wrap:anywhere}}
  th{{background:var(--paper2);font-family:'JetBrains Mono',monospace;font-size:11.5px;
    text-transform:uppercase;letter-spacing:.04em;color:var(--mute);position:sticky;top:0;white-space:nowrap}}
  tr:last-child td{{border-bottom:none}}
  .pager{{display:flex;justify-content:space-between;align-items:center;margin-top:14px;font-size:13.5px}}
  .pager a{{color:var(--accent);text-decoration:none}} .pager span{{color:var(--mute)}}
  .url{{display:flex;gap:8px;align-items:center;background:var(--paper2);border:1px solid var(--hair);
    border-radius:9px;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:12.5px;word-break:break-all}}
  .url code{{flex:1;color:var(--ink)}}
  button{{font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;border:1px solid var(--primary);
    background:var(--primary);color:var(--primary-ink);border-radius:8px;padding:7px 12px}}
  footer{{margin-top:30px;padding-top:16px;border-top:1px solid var(--hair);font-size:12.5px;color:var(--mute)}}
  footer a{{color:var(--accent);text-decoration:none}}
</style></head>
<body><div class=wrap>
{crumb}
{inner}
  <footer>Partagé via <a href="https://oto.ninja">Oto</a> — la boîte à outils d'automatisation.</footer>
</div></body></html>"""


def _nav_section(title: str, items: list[dict]) -> str:
    """Une carte « section » avec une liste de liens navigables (ou rien si vide)."""
    if not items:
        return ""
    lis = "".join(
        f'<a href="{html.escape(it["href"])}">'
        f'<span class=k>{html.escape(it["kind"])}</span>'
        f'<span>{html.escape(it["label"])}</span></a>'
        for it in items
    )
    return f'<div class=card><h2>{html.escape(title)}</h2><div class=nav>{lis}</div></div>'


# ── Rendus de page ────────────────────────────────────────────────────────────
def render_index(*, name: str, brief_md: str, procedures: list[dict], tables: list[dict],
                 docs: list[dict], connect_url: str, tools: Optional[list[str]] = None) -> str:
    brief_html = (f'<div class=card><article>{_MD.render(brief_md)}</article></div>'
                  if (brief_md or "").strip()
                  else '<p class="empty">Projet partagé, en lecture seule.</p>')
    sections = (
        _nav_section("Procédures", [
            {"href": f"/procedures/{p['id']}", "kind": "procédure", "label": p["label"]}
            for p in procedures])
        + _nav_section("Tableaux", [
            {"href": f"/data/{t['id']}", "kind": "tableau", "label": t["label"]}
            for t in tables])
        + _nav_section("Documents", [
            {"href": f"/docs/{d['id']}", "kind": "doc", "label": d["label"]}
            for d in docs])
    )
    if not sections:
        sections = ('<div class=card><p class="empty">Ce projet n\'expose encore aucune '
                    'procédure, tableau ni document.</p></div>')
    url = html.escape(connect_url)
    connect = (
        '<div class=card><h2>Brancher dans Claude ou Mistral</h2>'
        f'<div class=url><code id=u>{url}</code>'
        '<button onclick="navigator.clipboard.writeText(document.getElementById(\'u\')'
        '.textContent).then(()=>{this.textContent=\'copié ✓\'})">copier</button></div></div>')
    chips = "".join(f"<span class=chip>{html.escape(t)}</span>" for t in (tools or []))
    tools_card = (f'<div class=card><h2>Outils exposés</h2><div class=chips>{chips}</div></div>'
                  if chips else "")
    inner = (f'  <div class=eyebrow>Projet partagé · Oto</div>\n'
             f'  <h1>{html.escape(name or "Projet")}</h1>\n'
             f'  {brief_html}\n'
             f'  {sections}\n  {connect}\n  {tools_card}')
    return _shell(title=name, inner=inner)


def render_prose(*, name: str, title: str, body_md: str, kind_label: str) -> str:
    body_html = _MD.render(body_md or "")
    inner = (f'  <div class=eyebrow>{html.escape(kind_label)} · {html.escape(name or "Projet")}</div>\n'
             f'  <h1>{html.escape(title or kind_label)}</h1>\n'
             f'  <div class=card><article>{body_html}</article></div>')
    return _shell(title=title, inner=inner, home_url="/")


def render_data(*, name: str, namespace: str, columns: list[str], rows: list[dict],
                total: int, offset: int) -> str:
    if columns and rows:
        head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
        body_rows = []
        for r in rows:
            data = r.get("data") or {}
            cells = "".join(
                f"<td>{html.escape(_cell(data.get(c)))}</td>" for c in columns)
            body_rows.append(f"<tr>{cells}</tr>")
        table = (f'<div class=tablewrap><table><thead><tr>{head}</tr></thead>'
                 f'<tbody>{"".join(body_rows)}</tbody></table></div>')
    else:
        table = '<div class=card><p class="empty">Ce tableau est vide.</p></div>'
    start = offset + 1 if rows else 0
    end = offset + len(rows)
    pager_bits = [f"<span>{start}–{end} sur {total}</span>"]
    if offset > 0:
        pager_bits.insert(0, f'<a href="?offset={max(0, offset - _DATA_PAGE)}">← précédent</a>')
    if end < total:
        pager_bits.append(f'<a href="?offset={offset + _DATA_PAGE}">suivant →</a>')
    pager = f'<div class=pager>{"".join(pager_bits)}</div>' if total else ""
    inner = (f'  <div class=eyebrow>Tableau · {html.escape(name or "Projet")}</div>\n'
             f'  <h1>{html.escape(namespace)}</h1>\n  {table}\n  {pager}')
    return _shell(title=namespace, inner=inner, home_url="/")


def render_not_found(*, name: str = "") -> str:
    inner = ('  <div class=eyebrow>Oto</div>\n  <h1>Introuvable</h1>\n'
             '  <div class=card><p class="empty">Cette page n\'existe pas ou n\'est plus '
             'partagée dans ce projet.</p></div>')
    return _shell(title="Introuvable", inner=inner, home_url="/")


def _cell(v: object) -> str:
    """Représentation texte d'une valeur de cellule (dict/list → JSON compact court)."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 300 else s[:297] + "…"
    return str(v)


# ── Routeur (lectures DB SYNC → appeler en threadpool) ────────────────────────
def build_page(project: dict, path: str, *, offset: int = 0,
               connect_url: str = "") -> tuple[Optional[str], int]:
    """Rend la page UI pour ce (projet, path), ou `(None, 0)` si le path n'est PAS une
    route UI (le dispatch retombe alors sur le MCP). Fail-closed : une entité non liée au
    projet → 404, jamais une lecture hors périmètre. Les TABLEAUX (datastore, lecture seule)
    ne sont navigables que sur un partage `secret` (mode « partage de projet ») ; le flag
    `mcp_expose_datastore`, lui, gate les OUTILS `data_*` MCP, pas cette vue humaine."""
    from . import db, org_store

    pid = int(project["id"])
    p = (path or "/").rstrip("/") or "/"
    # Tableaux navigables (lecture seule) uniquement sur un partage `secret` : l'owner a
    # lié le tableau ET publié le projet en partage → consentement explicite. `anonymous`
    # (endpoint-outil listé publiquement) ne montre pas les lignes du datastore.
    show_data = (project.get("mcp_access") == "secret")

    if p == "/":
        links = db.list_project_links(pid)
        procedures = [
            {"id": int(l["target_ref"]), "label": l.get("label") or l.get("title") or f"#{l['target_ref']}"}
            for l in links
            if l.get("target_type") == "procedure" and str(l.get("target_ref", "")).isdigit()]
        tables = ([
            {"id": int(l["target_ref"]), "label": l.get("label") or l.get("namespace") or f"#{l['target_ref']}"}
            for l in links
            if l.get("target_type") == "tableau" and str(l.get("target_ref", "")).isdigit()]
            if show_data else [])
        # Docs : pages de l'arbre du projet + docs explicitement liés.
        docs = [{"id": int(d["id"]), "label": d.get("title") or f"#{d['id']}"}
                for d in db.list_docs_for_project(pid)]
        seen = {d["id"] for d in docs}
        for l in links:
            if l.get("target_type") == "doc" and str(l.get("target_ref", "")).isdigit():
                did = int(l["target_ref"])
                if did not in seen:
                    docs.append({"id": did, "label": l.get("label") or l.get("title") or f"#{did}"})
                    seen.add(did)
        return render_index(
            name=project.get("name") or "", brief_md=project.get("brief_md") or "",
            procedures=procedures, tables=tables, docs=docs, connect_url=connect_url,
            tools=list(project.get("mcp_tools") or [])), 200

    parts = p.strip("/").split("/")
    if len(parts) == 2 and parts[1].isdigit():
        section, rid = parts[0], int(parts[1])
        links = db.list_project_links(pid)

        if section == "procedures":
            allowed = {int(l["target_ref"]) for l in links
                       if l.get("target_type") == "procedure" and str(l.get("target_ref", "")).isdigit()}
            instr = org_store.get_instruction_by_id(rid) if rid in allowed else None
            if not instr:
                return render_not_found(), 404
            return render_prose(name=project.get("name") or "", title=instr.get("title") or "",
                                body_md=instr.get("body_md") or "", kind_label="Procédure"), 200

        if section == "data":
            allowed = {int(l["target_ref"]) for l in links
                       if l.get("target_type") == "tableau" and str(l.get("target_ref", "")).isdigit()}
            ns = db.get_datastore_namespace_by_id(rid) if (show_data and rid in allowed) else None
            if not ns:
                return render_not_found(), 404
            total = db.datastore_count_rows(rid)
            rows = db.datastore_list_rows(rid, offset=max(0, offset), limit=_DATA_PAGE)
            columns = _derive_columns(ns.get("schema"), rows)
            return render_data(name=project.get("name") or "", namespace=ns.get("namespace") or "tableau",
                               columns=columns, rows=rows, total=total, offset=max(0, offset)), 200

        if section == "docs":
            linked = {int(l["target_ref"]) for l in links
                      if l.get("target_type") == "doc" and str(l.get("target_ref", "")).isdigit()}
            doc = db.get_doc_by_id(rid)
            # Autorisé si le doc appartient à CE projet (héritage d'accès) ou lui est lié.
            if not doc or (int(doc.get("project_id") or 0) != pid and rid not in linked):
                return render_not_found(), 404
            return render_prose(name=project.get("name") or "", title=doc.get("title") or "",
                                body_md=doc.get("body_md") or "", kind_label="Document"), 200

        return render_not_found(), 404

    return None, 0


def _derive_columns(schema: object, rows: list[dict]) -> list[str]:
    """Colonnes du tableau : celles du schéma typé si présent, sinon l'union des clés des
    rows (ordre de première apparition). Défensif — le schéma peut manquer/varier."""
    if isinstance(schema, dict):
        fields = schema.get("fields") or schema.get("columns")
        if isinstance(fields, list) and fields:
            cols = [f.get("name") if isinstance(f, dict) else f for f in fields]
            cols = [c for c in cols if isinstance(c, str) and c]
            if cols:
                return cols
    cols: list[str] = []
    for r in rows:
        for k in (r.get("data") or {}).keys():
            if k not in cols:
                cols.append(k)
    return cols
